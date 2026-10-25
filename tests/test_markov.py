import json

from oi.markov import MIN_COUNT, OTHER, Markov, latest_model_path, mine_catalogue
from oi.moments import NOTHING


def test_catalogue_threshold(rows):
    cat = mine_catalogue([r["token"] for r in rows])
    assert all(n >= MIN_COUNT for n in cat.values())
    assert "activate:Finder" not in cat            # 4 occurrences in the seeded stream
    assert not any(t.startswith("url:rare.example/") for t in cat)
    assert not any(t.startswith("afk:") for t in cat)
    assert "url:example.org/prr-approuve" in cat   # parameterised token kept as-is
    assert mine_catalogue(["a:x"] * 4 + ["a:y"] * 5) == {"a:y": 5}


def test_top_k_learns_the_pattern(model, moments):
    m = next(x for x in moments if x["context"]["token"] == "activate:Safari")
    top = model.top_k(m, 3)
    assert top[0][0] in {"url:example.org/prr-approuve", NOTHING}
    assert "url:example.org/prr-approuve" in [t for t, _ in top]
    assert all(t != OTHER for t, _ in model.top_k(m, 50))


def test_top_k_reproducible_and_roundtrip(rows, moments, model, tmp_path):
    again = Markov.fit(moments, tokens=[r["token"] for r in rows])
    assert json.dumps(again.to_json(), sort_keys=True) == json.dumps(model.to_json(), sort_keys=True)
    path = model.save()
    assert path == latest_model_path() and path.name.startswith("markov-")
    assert str(path).startswith(str(tmp_path))
    loaded = Markov.load(path)
    for m in moments[:200]:
        assert loaded.top_k(m, 10) == model.top_k(m, 10)


def test_probabilities_sum_to_one(model, moments):
    for m in moments[::25]:
        probs, _ = model.distribution(m)
        assert abs(sum(probs.values()) - 1) < 1e-9


def _moment(h1, h2=None, hb=2, space=None):
    hist = [{"token": h1, "ago_s": 0, "dur_s": None}]
    if h2:
        hist.append({"token": h2, "ago_s": 30, "dur_s": 30})
    return {"history": hist, "context": {"token": h1, "hour_bucket": hb, "space": space}}


def test_back_off_to_order1_and_prior():
    train = []
    for _ in range(6):
        train.append(dict(_moment("k:a", "k:b", hb=2), label="k:c"))
        train.append(dict(_moment("k:a", "k:z", hb=2), label="k:d"))
        train.append(dict(_moment("k:q", "k:b", hb=2), label="k:d"))
    tokens = ["k:a", "k:b", "k:c", "k:d", "k:q", "k:z"] * 6
    m = Markov.fit(train, tokens=tokens)
    # seen order-2 context: c dominates
    assert m.top_k(_moment("k:a", "k:b"), 1)[0][0] == "k:c"
    _, lvl = m.distribution(_moment("k:a", "k:b"))
    assert lvl == "o2h"
    # unseen order-2 history, seen order-1: a -> c|d 50/50 at o1/o1h, blended with the prior
    probs, lvl = m.distribution(_moment("k:a", "k:q"))
    p_seen, _ = m.distribution(_moment("k:a", "k:b"))
    assert lvl == "o1h" and 0.25 < probs["k:c"] < p_seen["k:c"] and probs["k:d"] > probs["k:c"]
    # unseen hour bucket -> o1
    _, lvl = m.distribution(_moment("k:a", "k:q", hb=5))
    assert lvl == "o1"
    # token never seen as history -> prior (d is 2/3 of labels)
    top, lvl = m.top_k(_moment("k:new"), 2), m.distribution(_moment("k:new"))[1]
    assert lvl == "prior" and top[0][0] == "k:d"
    # space conditioning only when known
    _, lvl = m.distribution(_moment("k:a", "k:b", space=3))
    assert lvl == "o2h"
