"""W6: wish rows from oi-hud -> `wish:` tokens -> attached to the preceding moment."""
import json
import os

from conftest import TZ, WIN, ev

from oi import aw, cli, normalise, paths, vocab
from oi.markov import Markov
from oi.moments import NOTHING, build_moments


def _write_wishes(day, rows):
    d = paths.wishes_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def _wish(ts, text, app="Safari", source="typed"):
    return {"ts": ts, "text": text, "source": source,
            "context": {"app": app, "space": None, "hud_visible": False, "hud_options": []}}


def _row(ts, token, dur=10, app=None, src="aw-window"):
    return {"ts": ts, "dur": dur, "token": token, "app": app, "src": src, "raw_id": None}


def test_vocab_wish_kind():
    assert vocab.parse("wish:ouvrir-le-mail-de-paul") == ("wish", {"slug": "ouvrir-le-mail-de-paul"})
    assert vocab.validate("wish:" + "a" * 30)
    assert not vocab.validate("wish:" + "a" * 31)
    assert not vocab.validate("wish:Not A Slug")
    assert not vocab.validate("wish:")
    assert vocab.validate_row(_row("2026-01-05T09:00:00.000Z", "wish:x", 0, "Safari", "oi-wish")) == []


def test_wish_tokens_from_rows():
    rows = normalise.wish_tokens([
        _wish("2026-01-05T09:00:01.500Z", "Ouvrir le mail de Paul et répondre", source="dictated"),
        _wish("2026-01-05T09:00:02Z", "   "),                      # empty: skipped
        {"ts": "not a date", "text": "x"},                          # bad ts: skipped
        _wish("2026-01-05T09:00:03Z", "日本語", app=None),           # nothing survives the slug
    ])
    assert [r["token"] for r in rows] == ["wish:ouvrir-le-mail-de-paul-et-repo", "wish:untitled"]
    assert rows[0] == {"ts": "2026-01-05T09:00:01.500Z", "dur": 0.0, "token": "wish:ouvrir-le-mail-de-paul-et-repo",
                       "app": "Safari", "src": "oi-wish", "raw_id": None}
    assert rows[1]["app"] is None
    assert all(vocab.validate_row(r) == [] for r in rows)


def test_build_day_emits_wish_tokens_in_order():
    aw.write_jsonl(paths.raw_dir() / WIN / "2026-01-05.jsonl",
                   [ev(1, 0, 5, app="AppA"), ev(2, 10, 5, app="AppB")])
    _write_wishes("2026-01-05", [_wish("2026-01-05T09:00:07Z", "open the invoice")])
    _write_wishes("2026-01-06", [_wish("2026-01-06T09:00:00Z", "another day")])   # neighbour day: filtered
    (paths.wishes_dir() / "2026-01-05.jsonl").open("a").write("{corrupt\n")
    _, rows = normalise.build_day("2026-01-05")
    assert [r["token"] for r in rows] == ["activate:AppA", "wish:open-the-invoice", "activate:AppB"]
    assert all(vocab.validate_row(r) == [] for r in rows)


def test_wish_is_never_a_moment_nor_a_label_and_attaches_to_previous():
    rows = [_row("2026-01-05T09:00:00Z", "activate:Mail", 30, "Mail"),
            _row("2026-01-05T09:00:05Z", "wish:reply-to-paul", 0, "Mail", "oi-wish"),
            _row("2026-01-05T09:00:10Z", "activate:Safari", 30, "Safari"),
            _row("2026-01-05T09:00:20Z", "key:⌘C", 0, "Safari"),
            _row("2026-01-05T09:00:21Z", "wish:first", 0, "Safari", "oi-wish"),
            _row("2026-01-05T09:00:22Z", "wish:second", 0, "Safari", "oi-wish")]
    ms = build_moments(rows, tz=TZ)
    assert [m["context"]["token"] for m in ms] == ["activate:Mail", "activate:Safari", "key:⌘C"]
    assert ms[0]["label"] == "activate:Safari"            # the wish is skipped, not the label
    assert ms[0]["wish"] == "reply-to-paul"
    assert "wish" not in ms[1]
    assert ms[2]["wish"] == "second" and ms[2]["label"] == NOTHING   # last wish wins
    assert all(not h["token"].startswith("wish:") for m in ms for h in m["history"])


def test_wish_before_first_moment_is_dropped():
    rows = [_row("2026-01-05T08:59:00Z", "wish:early", 0, None, "oi-wish"),
            _row("2026-01-05T09:00:00Z", "activate:Mail", 30, "Mail")]
    ms = build_moments(rows, tz=TZ)
    assert len(ms) == 1 and "wish" not in ms[0]


def test_markov_never_proposes_a_wish():
    rows = []
    for i in range(12):
        rows.append(_row(f"2026-01-05T09:{i:02d}:00Z", "activate:Mail", 20, "Mail"))
        rows.append(_row(f"2026-01-05T09:{i:02d}:10Z", "wish:do-the-thing", 0, "Mail", "oi-wish"))
        rows.append(_row(f"2026-01-05T09:{i:02d}:20Z", "activate:Safari", 20, "Safari"))
    m = Markov.fit(build_moments(rows, tz=TZ), min_count=2)
    assert not any(t.startswith("wish:") for t in m.catalogue)


def test_stats_counts_wishes(capsys):
    aw.write_jsonl(paths.raw_dir() / WIN / "2026-01-05.jsonl", [ev(1, 0, 5, app="AppA")])
    _write_wishes("2026-01-05", [_wish("2026-01-05T09:00:02Z", "secret wish text"),
                                 _wish("2026-01-05T09:00:03Z", "another")])
    assert cli.main(["tokens", "--day", "2026-01-05"]) == 0
    assert cli.main(["stats", "--day", "2026-01-05"]) == 0
    out = capsys.readouterr().out
    assert "2 wishes" in out and "wish=2" in out
    assert "secret" not in out                             # never the text
    assert cli.main(["stats", "--day", "2026-01-05", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["wishes"] == 2
