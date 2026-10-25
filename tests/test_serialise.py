import copy

from oi import forms, serialise
from oi.tokcount import count_tokens


def _long_moment():
    hist = [{"token": f"url:docs.example.com/long-synthetic-page-title-number-{i:02d}", "ago_s": 40.0 * i,
             "dur_s": 35.0} for i in range(20)]
    hist[0]["dur_s"] = None
    return {"ts": "2026-10-12T12:00:00Z", "label": "nothing",
            "context": {"weekday": "Mon", "hhmm": "14:00", "daypart": "afternoon", "hour_bucket": 3,
                        "space": 2, "spaces": 4, "app": "Safari",
                        "url": "docs.example.com/long-synthetic-page-title-number-00",
                        "title": None, "token": hist[0]["token"]},
            "history": hist,
            "day_summary": [[f"SyntheticApplicationName{i}", 3600 - i] for i in range(5)]}


def test_render_matches_design_shape(moments):
    m = next(x for x in moments if x["context"]["app"] == "Safari" and x["context"]["url"])
    text, n, trunc = serialise.render(forms.load_form("C"), m)
    lines = text.splitlines()
    assert lines[0].startswith("time: ") and "| desk " in lines[0]
    assert lines[1].startswith("now: Safari · example.org")
    assert lines[2].startswith("recent: 0s: ")
    assert n == count_tokens(text) and n <= 256 and trunc == []


def test_all_synthetic_moments_fit_L256(moments):
    for name in ["A", "C"]:
        f = forms.load_form(name)
        for m in moments:
            _, n, trunc = serialise.render(f, m)
            assert n <= 256 and "over_budget" not in trunc


def test_long_history_truncates_in_order():
    form = copy.deepcopy(forms.load_form("C"))
    form["state_template"] = form["state_template"].replace("{history:5}", "{history:20}")
    m = _long_moment()
    full, n_full, t_full = serialise.render(form, m, budget=10_000)
    assert t_full == [] and n_full > 256
    text, n, trunc = serialise.render(form, m)
    assert n <= 256
    assert trunc[0] == "day_summary" and trunc[1].startswith("recent_oldest:")
    assert "today:" not in text
    assert text.splitlines()[0] == full.splitlines()[0]          # current context kept
    assert text.splitlines()[1] == full.splitlines()[1]
    assert "number-00" in text.splitlines()[2]                    # newest event kept
    assert "number-19" not in text                                # oldest dropped


def test_tiny_budget_records_over_budget_but_keeps_context():
    m = _long_moment()
    text, n, trunc = serialise.render(forms.load_form("A"), m, budget=10)
    assert trunc[-1] == "over_budget"
    assert text.startswith("time: Mon 14:00") and "now: Safari" in text
    assert text.count(" | ") == 1  # time line's desk separator only: one recent event left
