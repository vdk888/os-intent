import json

import pytest

from oi import cli_markov, tokcount
from oi.tokcount import count_tokens, fallback_count


def test_tokenizer_name_reported():
    assert tokcount.TOKENIZER.startswith(("laya:", "fallback:"))
    assert count_tokens("time: Mon 08:40 (morning) | desk 3/4") > 5


def test_fallback_is_conservative_vs_laya(moments):
    if not tokcount.tokenizer_name().startswith("laya:"):
        pytest.skip("Laya tokenizer not available here")
    from oi import forms, serialise
    for m in moments[::20]:
        text, n, _ = serialise.render(forms.load_form("A"), m)
        assert fallback_count(text) >= n


def test_cli_markov_and_render(rows, moments, tmp_path, monkeypatch, capsys):
    tdir = tmp_path / "oi-data" / "tokens"
    tdir.mkdir(parents=True)
    for day in ("2026-10-12", "2026-10-13"):
        (tdir / f"{day}.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows if r["ts"].startswith(day)))
    assert cli_markov.main(["markov", "--days", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["days"] == 2 and out["moments"] == len(moments)
    m = next(x for x in moments if x["context"]["app"] == "Safari")
    mf = tmp_path / "moment.json"
    mf.write_text(json.dumps(m))
    assert cli_markov.main(["render", "--moment", str(mf), "--no-exec-check"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["form"] == "safari.next_page@v2" and r["n_tokens"] <= 256
    crit = r["questions"]["next"]["criteria"]
    assert "nothing" in crit and len(crit) <= 16
