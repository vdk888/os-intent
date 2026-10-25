from oi import vocab
from oi.moments import NOTHING, build_moments
from oi.vocab import parse_ts
from conftest import TZ


def _row(ts, token, dur=10, app=None):
    return {"ts": ts, "dur": dur, "token": token, "app": app, "src": "aw-window", "raw_id": None}


def test_synthetic_rows_follow_the_vocabulary(rows):
    assert all(vocab.validate_row(r) == [] for r in rows)


def test_labels_are_next_token_within_120s_else_nothing(rows, moments):
    by_ts = {}
    for i, r in enumerate(rows):
        by_ts.setdefault(r["ts"], i)
    for m in moments:
        i = max(j for j, r in enumerate(rows) if r["ts"] == m["ts"] and r["token"] == m["context"]["token"])
        if i + 1 < len(rows):
            gap = (parse_ts(rows[i + 1]["ts"]) - parse_ts(rows[i]["ts"])).total_seconds()
            nxt = rows[i + 1]["token"]
            exp = nxt if gap <= 120 and not nxt.startswith("afk:") else NOTHING
        else:
            exp = NOTHING
        assert m["label"] == exp
    labels = {m["label"] for m in moments}
    assert NOTHING in labels and len(labels) > 5


def test_no_moment_while_afk():
    rows = [_row("2026-10-12T08:00:00Z", "activate:Mail"),
            _row("2026-10-12T08:00:30Z", "afk:start", 1170),
            _row("2026-10-12T08:10:00Z", "activate:Safari"),  # stray event while away
            _row("2026-10-12T08:20:00Z", "afk:end"),
            _row("2026-10-12T08:20:05Z", "activate:Code")]
    ms = build_moments(rows, tz=TZ)
    assert [m["context"]["token"] for m in ms] == ["activate:Mail", "afk:end", "activate:Code"]
    assert ms[0]["label"] == NOTHING  # next is afk:start
    assert ms[1]["label"] == "activate:Code"


def test_afk_span_ends_by_duration_when_afk_end_missing():
    rows = [_row("2026-10-12T08:00:00Z", "activate:Mail", 30, "Mail"),
            _row("2026-10-12T08:00:30Z", "afk:start", 600),          # away until 08:10:30
            _row("2026-10-12T08:05:00Z", "key:⌘C"),                  # inside the span: skipped
            _row("2026-10-12T08:12:00Z", "activate:Code", 60, "Code")]  # no afk:end emitted
    ms = build_moments(rows, tz=TZ)
    assert [m["context"]["token"] for m in ms] == ["activate:Mail", "activate:Code"]


def test_context_time_space_app_url_history():
    rows = [_row("2026-10-12T07:00:00Z", "space:=2", 1),
            _row("2026-10-12T07:00:01Z", "activate:Safari", 600, "Safari"),
            _row("2026-10-12T07:00:05Z", "url:example.org/kanban", 300, "Safari"),
            _row("2026-10-12T07:01:05Z", "space:+1", 5, "Safari")]
    m = build_moments(rows, tz=TZ)[-1]
    c = m["context"]
    assert (c["weekday"], c["hhmm"], c["daypart"], c["hour_bucket"]) == ("Mon", "09:01", "morning", 2)
    assert (c["space"], c["spaces"], c["app"], c["url"]) == (3, 3, "Safari", "example.org/kanban")
    h = m["history"]
    assert h[0] == {"token": "space:+1", "ago_s": 0.0, "dur_s": None}  # current: no future duration
    assert h[1]["token"] == "url:example.org/kanban" and h[1]["dur_s"] == 60.0  # clipped to moment
    assert m["day_summary"] == [["Safari", 64]]


def test_day_summary_resets_at_local_midnight():
    rows = [_row("2026-10-12T21:00:00Z", "activate:Mail"),
            _row("2026-10-12T21:30:00Z", "activate:Code"),   # 23:30 Paris
            _row("2026-10-12T22:30:00Z", "activate:Mail")]   # 00:30 next day Paris
    ms = build_moments(rows, tz=TZ)
    assert ms[1]["day_summary"] == [["Mail", 1800]]
    assert ms[2]["day_summary"] == [["Code", 1800]]
