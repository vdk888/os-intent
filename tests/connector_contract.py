"""The reusable connector contract check. A connector test builds
its sensor on fakes, gives a `world(state)` that sets the fake world to "off" | "on" | "broken", and calls
`check_connector`. It proves, for that connector:

  schema    every row it emits complies with oi.proactive.contract (live and gone rows)
  live      off -> on = ONE appear row; on again = nothing new (a state, not a timer)
  failure   broken while live = no row (nothing starts, nothing ends) AND `last_error` set (health counts it)
  gone      on -> off = ONE gone row with the live row's id
  levels    level 1 = at least one action (engine.event_options), level 2 = a `what` line (glide.event_card),
            level 3 = the provider declared in contract.LEVEL3 ("local": the card carries its summary lines)

Not a test module itself (no test_ prefix): imported by tests/test_connector_contract.py and any connector test.
"""
from oi import glide
from oi.proactive import contract, engine, store

NAMES = {"atlas": "Atlas", "orion": "Orion", "nova": "Nova", "lyra": "Lyra"}


def assert_schema(ev: dict) -> None:
    assert contract.problems(ev) == [], (ev.get("source"), contract.problems(ev))


def levels(ev: dict, names: dict | None = None) -> tuple[list[dict], dict]:
    """(level-1 actions, level-2/3 card) of a live event, as the engine and the glide render them."""
    opts = engine.event_options(ev, 0.5, names=names or NAMES, route=lambda text: (None, 0.0))
    return opts, glide.event_card(ev)


def check_connector(sensor, cfg: dict, world, *, now: float, step: float = 10.0, names: dict | None = None) -> dict:
    """Runs the contract sequence on one connector; returns its live event (for extra, source-specific asserts)."""
    t = [now]

    def poll(state: str) -> list[dict]:
        world(state)
        t[0] += step
        evs = sensor.poll(t[0], cfg)
        for e in evs:
            assert_schema(e)
        return evs

    assert poll("off") == [], "nothing is on: no row"
    assert getattr(sensor, "last_error", None) is None, "a readable source is healthy"
    on = poll("on")
    assert [e["state"] for e in on] == ["live"], f"off -> on must be ONE live row, got {on}"
    ev = on[0]
    opts, card = levels(ev, names)
    assert opts, "level 1: at least one action"
    assert all(o["evt"] and o["primitive"] for o in opts)
    assert card.get("what"), "level 2: who / what / since"
    l3 = contract.LEVEL3.get(ev["source"])
    if l3 == "local":
        assert card.get("summary"), "level 3: the local provider gives summary lines"
    private = dict(ev, private=True, subject=None)
    assert "summary" not in glide.event_card(private) and glide.event_card(private)["why"] is None
    assert poll("on") == [], "the same state again: nothing new"

    assert poll("broken") == [], "an unreadable source: nothing starts, nothing ends"
    assert getattr(sensor, "last_error", None), "an unreadable source is a failure (health), never 'nothing'"
    assert poll("on") == [], "readable again, the state unchanged: still the same live event"
    assert getattr(sensor, "last_error", None) is None

    gone = poll("off")
    assert [(e["id"], e["state"]) for e in gone] == [(ev["id"], "gone")], f"on -> off must end it, got {gone}"
    assert store.token(gone[0]) == store.token(ev) + ":gone"

    return ev
