import pytest

from oi.vocab import parse, slug, validate, validate_row

VALID = [
    ("activate:AppA", "activate", {"app": "AppA"}),
    ("activate:App With Spaces", "activate", {"app": "App With Spaces"}),
    ("title:AppA/doc-1", "title", {"app": "AppA", "slug": "doc-1"}),
    ("url:example.test/page-2", "url", {"host": "example.test", "slug": "page-2"}),
    ("url:localhost:8080/home", "url", {"host": "localhost:8080", "slug": "home"}),
    ("space:+1", "space", {"move": "+1"}),
    ("space:-1", "space", {"move": "-1"}),
    ("space:=3", "space", {"move": "=3"}),
    ("key:⌘C", "key", {"chord": "⌘C"}),
    ("key:⌘⇧4", "key", {"chord": "⌘⇧4"}),
    ("afk:start", "afk", {"state": "start"}),
    ("afk:end", "afk", {"state": "end"}),
    ("clip:changed", "clip", {"event": "changed"}),
    ("hud:pressed:2", "hud", {"event": "pressed:2", "k": "2"}),
]

INVALID = ["", "activate:", "activate: AppA", "title:AppA/Doc", "title:AppA/" + "a" * 31,
           "url:Example.test/x", "url:example.test/", "space:+2", "space:=x", "key:C", "key:⇧C",
           "key:⌘", "afk:maybe", "nope:x", "activate:a\nb"]


@pytest.mark.parametrize("tok,kind,payload", VALID)
def test_parse_valid(tok, kind, payload):
    assert validate(tok)
    assert parse(tok) == (kind, payload)


@pytest.mark.parametrize("tok", INVALID)
def test_invalid(tok):
    assert not validate(tok)
    with pytest.raises(ValueError):
        parse(tok)


def test_round_trip_rebuilds_token():
    for tok, kind, p in VALID:
        rebuilt = {
            "activate": lambda: f"activate:{p.get('app')}",
            "title": lambda: f"title:{p.get('app')}/{p.get('slug')}",
            "url": lambda: f"url:{p.get('host')}/{p.get('slug')}",
            "space": lambda: f"space:{p.get('move')}",
            "key": lambda: f"key:{p.get('chord')}",
            "afk": lambda: f"afk:{p.get('state')}",
            "clip": lambda: f"clip:{p.get('event')}",
            "hud": lambda: f"hud:{p.get('event')}",
        }[kind]()
        assert rebuilt == tok


def test_slug():
    assert slug("Doc 1 — Été Notes!") == "doc-1-ete-notes"
    assert slug("x" * 50) == "x" * 30
    assert slug("abc " * 10, n=10) == "abc-abc-ab"
    assert not slug("a" * 29 + " b").endswith("-")
    assert slug("") == "" and slug(None) == "" and slug("!!!") == ""


def test_validate_row():
    good = {"ts": "2026-01-05T09:00:00.000Z", "dur": 1.5, "token": "activate:AppA", "app": "AppA",
            "src": "aw-window", "raw_id": 3}
    assert validate_row(good) == []
    assert validate_row({**good, "ts": "2026-01-05T09:00:00+02:00"})
    assert validate_row({**good, "dur": -1})
    assert validate_row({**good, "src": "chrome"})
    assert validate_row({**good, "token": "bad"})
    assert validate_row({**good, "raw_id": "3"})
    assert validate_row({k: v for k, v in good.items() if k != "app"})
