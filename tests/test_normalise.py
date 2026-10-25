import json

from conftest import AFK, WIN, ev

from oi import aw, normalise, paths
from oi.vocab import validate_row

SAF = "oi-watcher-safari_host-a"
SPC = "oi-watcher-space"
KEY = "oi-watcher-key"


def toks(raw, **kw):
    return [r["token"] for r in normalise.tokens_from_raw(raw, **kw)]


def test_activate_on_app_change_only():
    raw = {WIN: [ev(1, 0, 5, app="AppA", title="doc-1"), ev(2, 5, 5, app="AppA", title="doc-2"),
                 ev(3, 10, 5, app="AppB", title="x"), ev(4, 15, 5, app="AppA", title="doc-1")]}
    rows = normalise.tokens_from_raw(raw)
    assert [r["token"] for r in rows] == ["activate:AppA", "activate:AppB", "activate:AppA"]
    assert rows[0]["dur"] == 10.0 and rows[0]["raw_id"] == 1 and rows[0]["src"] == "aw-window"
    assert all(validate_row(r) == [] for r in rows)


def test_flicker_dropped_both_events():
    raw = {WIN: [ev(1, 0, 5, app="AppA"), ev(2, 5, 0.4, app="AppB"), ev(3, 5.4, 5, app="AppA"),
                 ev(4, 10.4, 5, app="AppC")]}
    assert toks(raw) == ["activate:AppA", "activate:AppC"]


def test_no_flicker_when_not_reverted_or_slow():
    raw = {WIN: [ev(1, 0, 5, app="AppA"), ev(2, 5, 0.4, app="AppB"), ev(3, 5.4, 5, app="AppC")]}
    assert toks(raw) == ["activate:AppA", "activate:AppB", "activate:AppC"]
    raw = {WIN: [ev(1, 0, 5, app="AppA"), ev(2, 5, 1.2, app="AppB"), ev(3, 6.2, 5, app="AppA")]}
    assert toks(raw) == ["activate:AppA", "activate:AppB", "activate:AppA"]


def test_repeated_flicker_collapses():
    raw = {WIN: [ev(1, 0, 5, app="AppA"), ev(2, 5, 0.2, app="AppB"), ev(3, 5.2, 0.2, app="AppA"),
                 ev(4, 5.4, 0.2, app="AppB"), ev(5, 5.6, 5, app="AppA")]}
    assert toks(raw) == ["activate:AppA"]


def test_title_tokens_for_title_apps():
    raw = {WIN: [ev(1, 0, 5, app="AppT", title="Doc 1 — Notes"), ev(2, 5, 5, app="AppT", title="Doc 1 — Notes"),
                 ev(3, 10, 5, app="AppT", title="Doc 2"), ev(4, 15, 5, app="AppX", title="hidden")]}
    out = toks(raw, title_apps={"AppT"})
    assert out == ["activate:AppT", "title:AppT/doc-1-notes", "title:AppT/doc-2", "activate:AppX"]


def test_default_title_apps_and_slug_cap():
    raw = {WIN: [ev(1, 0, 5, app="Terminal", title="session " + "z" * 60)]}
    out = toks(raw)
    assert out[1].startswith("title:Terminal/session-") and len(out[1].split("/", 1)[1]) == 30


def test_denylist_activate_only():
    raw = {WIN: [ev(1, 0, 5, app="Passwords", title="secret-1"), ev(2, 5, 5, app="Passwords", title="secret-2")]}
    out = toks(raw, title_apps={"Passwords"})
    assert out == ["activate:Passwords"]
    assert not any("secret" in t for t in out)


def test_afk_transitions():
    raw = {AFK: [ev(1, 0, 60, status="not-afk"), ev(2, 60, 30, status="not-afk"), ev(3, 90, 300, status="afk"),
                 ev(4, 390, 50, status="not-afk"), ev(5, 440, 10, status="afk")]}
    rows = normalise.tokens_from_raw(raw)
    assert [r["token"] for r in rows] == ["afk:start", "afk:end", "afk:start"]
    assert rows[0]["src"] == "aw-afk" and rows[0]["app"] is None and rows[0]["dur"] == 300


def test_url_tokens():
    raw = {SAF: [ev(1, 0, 3, url="https://WWW.Example.test/Page-One/sub?q=1", title="t"),
                 ev(2, 3, 3, url="https://www.example.test/page-one/other", title="t"),
                 ev(3, 6, 3, url="https://site.test/", title="Home Title"),
                 ev(4, 9, 3, url="https://private.test/x", title="p", incognito=True),
                 ev(5, 12, 3, url="about:blank", title="")]}
    rows = normalise.tokens_from_raw(raw)
    assert [r["token"] for r in rows] == ["url:www.example.test/page-one", "url:site.test/home-title"]
    assert rows[0]["app"] == "Safari" and rows[0]["src"] == "oi-safari"


def test_space_and_key_tokens():
    raw = {SPC: [ev(1, 0, 0, space=None, delta=1), ev(2, 1, 0, space=None, delta=-1),
                 ev(3, 2, 0, space=3, delta=0), ev(4, 3, 0, space=None, delta=0)],
           KEY: [ev(1, 0.5, 0, chord="⌘C"), ev(2, 1.5, 0, chord="⇧A"), ev(3, 2.5, 0, chord="⌘⇧4")],
           "some-other-bucket": [ev(1, 0, 1, foo="bar")]}
    rows = normalise.tokens_from_raw(raw)
    assert [r["token"] for r in rows] == ["space:+1", "key:⌘C", "space:-1", "space:=3", "key:⌘⇧4"]
    assert {r["src"] for r in rows} == {"oi-space", "oi-key"}
    assert all(validate_row(r) == [] for r in rows)


def _write_raw(bucket, day, rows):
    aw.write_jsonl(paths.raw_dir() / bucket / f"{day}.jsonl", rows)


def test_build_day_idempotent_and_valid():
    _write_raw(WIN, "2026-01-05", [ev(1, 0, 5, app="AppA"), ev(2, 5, 5, app="Notion", title="doc-1")])
    _write_raw(AFK, "2026-01-05", [ev(1, 0, 10, status="not-afk"), ev(2, 10, 10, status="afk")])
    p1, rows1 = normalise.build_day("2026-01-05")
    first = p1.read_text()
    p2, rows2 = normalise.build_day("2026-01-05")
    assert p1 == p2 and p2.read_text() == first and rows1 == rows2
    assert [r["token"] for r in rows1] == ["activate:AppA", "activate:Notion", "title:Notion/doc-1", "afk:start"]
    assert all(validate_row(json.loads(l)) == [] for l in first.splitlines())


def test_build_day_uses_previous_day_context():
    # AppA runs across midnight: no new activate on the next day
    _write_raw(WIN, "2026-01-05", [ev(1, 14 * 3600 + 59 * 60, 120, app="AppA")])
    _write_raw(WIN, "2026-01-06", [ev(2, 15 * 3600 + 1, 60, app="AppA"), ev(3, 15 * 3600 + 61, 60, app="AppB")])
    _, rows = normalise.build_day("2026-01-06")
    assert [r["token"] for r in rows] == ["activate:AppB"]


def test_build_day_merges_clipped_copy_across_days():
    _write_raw(WIN, "2026-01-05", [ev(1, 14 * 3600 + 50 * 60, 1200, app="AppA")])
    _write_raw(WIN, "2026-01-06", [ev(1, 15 * 3600, 600, app="AppA"), ev(2, 15 * 3600 + 600, 60, app="AppB")])
    _, rows = normalise.build_day("2026-01-05")
    assert [r["token"] for r in rows] == ["activate:AppA"] and rows[0]["dur"] == 1200.0
