"""Battery drain 2026-11-03 (`oi live` 17 h up: System Events ~20 % CPU, osascript ~21 %, on battery): every
osascript spawn of the proactive sensor thread, counted over simulated polls. The same harness runs the cadence as it
was (no shared Dock read, the chat-list walk every poll, agents every 8 s, no battery factor) and as it is now."""
import pytest

from oi import drill
from oi.proactive import config, engine, sensors

MIN = 60
SIM_S = 30 * MIN            # half an hour of the 1 s sensor tick
NOW = 1_800_000_000.0
REAL_MAIL_BADGE = sensors.mail_badge          # taken at import, before conftest stubs it (the Dock read is injected)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeLive:
    def _hud_cmd(self, payload):
        return {}


class Outbound:
    def pending(self, spec, ids, after=None):
        return {"n": 0, "first": None, "last": None}                       # the records read: nobody waiting


def simulate(*, battery: bool, as_before: bool = False, telegram_list: bool = False, badge_moves: bool = False):
    """Run the engine's sensor tick over SIM_S with mail + agents enabled (a typical setup), Telegram badged and
    Mail open. Returns the osascript spawns by kind."""
    clock = Clock()
    spawns = {"dock": 0, "walk": 0, "mail": 0}
    tg = [9]

    def dock_read():
        spawns["dock"] += 1
        return {"Telegram": str(tg[0]), "Mail": "3"}

    def walk():
        spawns["walk"] += 1
        return {"Some chat": 1} if telegram_list else None              # Telegram 13: no chat list

    def mail_osa(script, args, timeout):
        spawns["mail"] += 1
        return 0, "OK" + sensors.RS

    power = sensors.Power(read=lambda: battery, clock=clock)
    if as_before:                                                          # the code before this change
        power = sensors.Power(read=lambda: False, clock=clock)
        badge_ttl = lambda: 0.0                                            # noqa: E731  no cache: one read per consumer
        dock = sensors.DockBadges(read=dock_read, clock=clock, factor=badge_ttl)
        unread = walk
        agents_poll = 8
    else:
        dock = sensors.DockBadges(read=dock_read, clock=clock, factor=power.factor)
        unread = sensors.GatedUnread(walk=walk, badge=lambda: dock.get("Telegram"), clock=clock, factor=power.factor)
        agents_poll = config.proactive_settings({})["agents"]["poll_s"]
    s_agents = sensors.AgentSensor(unread=unread, dock_badge=lambda: dock.get("Telegram"), running=lambda: True,
                                   outbound=Outbound(), table=lambda: {}, titles=lambda: {})
    s_mail = sensors.MailSensor(osa=mail_osa, running=lambda: True, cpu=lambda: None,
                                badge=lambda: dock.get("Mail", fresh=not as_before), log=lambda m: None)
    from oi import health as health_mod
    eng = engine.Engine(FakeLive(), sensors={"agents": s_agents, "mail": s_mail}, judge=lambda ev: {},
                        health=health_mod.SensorHealth(path=lambda: None), power=power.factor,
                        raw_settings=lambda: {"proactive": {"sources": {"agents": True, "mail": True},
                                                            "files": {"folders": []},
                                                            "agents": {"poll_s": agents_poll}}})
    for sec in range(SIM_S):
        clock.t = float(sec)
        if badge_moves and sec % 45 == 0:
            tg[0] += 1                                                     # a new Telegram message every 45 s
        eng.poll_once(NOW + sec)
    return spawns


def per_min(spawns: dict) -> float:
    return sum(spawns.values()) / (SIM_S / MIN)


def test_spawn_rate_before_the_change_documents_the_drain():
    before = simulate(battery=True, as_before=True)
    assert before["walk"] == SIM_S // 8 and before["dock"] >= SIM_S // 8  # dock + walk on every 8 s poll
    assert per_min(before) >= 15                                           # ~15 osascript / min, on battery too


@pytest.mark.parametrize("battery, max_per_min", [(False, 6.5), (True, 2.3)])
def test_spawn_rate_is_bounded_on_ac_and_on_battery(battery, max_per_min):
    got = simulate(battery=battery)
    factor = sensors.BATTERY_FACTOR if battery else 1.0
    mail_polls = SIM_S / (180 * factor) + 1                               # Mail reads its badge fresh, per its poll
    assert got["dock"] <= SIM_S / (sensors.BADGE_MIN_S * factor) + 1 + mail_polls   # ONE shared Dock read per BADGE_MIN_S
    assert got["walk"] <= sensors.UNREAD_MISSES + SIM_S / (sensors.UNREAD_RETRY_S * factor)   # no list: 10 (30) min
    assert per_min(got) <= max_per_min
    assert per_min(got) * 2 < per_min(simulate(battery=battery, as_before=True))


def test_walk_follows_the_badge_when_the_chat_list_is_readable():
    """A readable list is walked again when the badge changes (a new message: the edge is not delayed past the
    next Dock read), else every UNREAD_TTL_S."""
    still = simulate(battery=False, telegram_list=True)
    assert still["walk"] <= SIM_S / sensors.UNREAD_TTL_S + 1
    moving = simulate(battery=False, telegram_list=True, badge_moves=True)
    assert moving["walk"] >= SIM_S // 45                                   # every badge change walked
    assert moving["walk"] <= SIM_S // 45 + SIM_S / sensors.UNREAD_TTL_S + 1


def test_gated_unread_semantics():
    clock, badge, walks = Clock(), [3], []
    g = sensors.GatedUnread(walk=lambda: walks.append(1) or {"Atlas - Tools": badge[0]}, badge=lambda: badge[0],
                            clock=clock, factor=lambda: 1.0)
    assert g() == {"Atlas - Tools": 3} and len(walks) == 1
    clock.t = 30
    assert g() == {"Atlas - Tools": 3} and len(walks) == 1                    # same badge: the last answer stands
    badge[0] = 4
    assert g() == {"Atlas - Tools": 4} and len(walks) == 2                    # the badge moved: walked at once
    clock.t = 30 + sensors.UNREAD_TTL_S
    g()
    assert len(walks) == 3                                                 # TTL: walked again
    g.forget()                                                             # badge 0 (all read), then 4 again
    assert g() == {"Atlas - Tools": 4} and len(walks) == 4                    # never the list from before the 0


def test_gated_unread_retries_a_failed_walk_and_backs_off_a_blind_client():
    clock, badge, answers, walks = Clock(), [3], [], []

    def walk():
        walks.append(1)
        return answers.pop(0) if answers else None
    g = sensors.GatedUnread(walk=walk, badge=lambda: badge[0], clock=clock, factor=lambda: 1.0)
    answers[:] = [None, {"Atlas - Tools": 3}]
    assert g() is None and g() == {"Atlas - Tools": 3} and len(walks) == 2   # one failure: retried at the next poll
    clock.t = 100.0
    g.last, g.misses = None, 0
    for _ in range(sensors.UNREAD_MISSES + 3):
        g()
    assert len(walks) == 2 + sensors.UNREAD_MISSES                         # Telegram 13: no list, then the back-off
    badge[0] = 9
    g.forget()
    assert g() is None and len(walks) == 2 + sensors.UNREAD_MISSES         # a blind client stays on its retry
    clock.t += sensors.UNREAD_RETRY_S
    g()
    assert len(walks) == 3 + sensors.UNREAD_MISSES


def test_agent_sensor_badge_back_at_the_same_count_is_walked_again():
    """Review finding: badge 3 (A) -> 0 (all read) -> 3 (B) within the TTL must not bring A's list back."""
    from oi.proactive import store
    clock, dock, lists = Clock(), [3], [{"Lyra - Sales": 3}]
    g = sensors.GatedUnread(walk=lambda: lists[-1], badge=lambda: dock[-1], clock=clock, factor=lambda: 1.0)
    s = sensors.AgentSensor(unread=g, dock_badge=lambda: dock[-1], running=lambda: True, outbound=Outbound())
    cfg = config.proactive_settings({})
    assert [store.token(e) for e in s.poll(NOW, cfg)] == ["evt:agent:lyra"]
    dock.append(0)
    clock.t = 10
    assert [store.token(e) for e in s.poll(NOW + 10, cfg)] == ["evt:agent:lyra:gone"]
    dock.append(3)
    lists.append({"Lyra - Sales": 0, "Orion - Finance": 3})
    clock.t = 20
    assert [store.token(e) for e in s.poll(NOW + 20, cfg)] == ["evt:agent:orion"]


def test_a_truncated_walk_is_unknown_never_partial():
    js = sensors.TELEGRAM_UNREAD_JS
    assert js.startswith(f"const NODE_MAX={sensors.NODE_MAX};") and "if(N>=NODE_MAX)return 'null';" in js


def test_mail_badge_is_read_fresh_for_its_change_detector(monkeypatch):
    reads = []
    dock = sensors.DockBadges(read=lambda: reads.append(1) or {"Mail": "4"}, factor=lambda: 1.0)
    monkeypatch.setattr(sensors, "DOCK", dock)
    assert sensors.dock_tile_badge("Mail") == 4 and REAL_MAIL_BADGE() == 4 and len(reads) == 2
    assert sensors.dock_tile_badge("Telegram") is None and len(reads) == 2    # the others share the fresh read


def test_power_source_is_read_at_most_every_minute():
    clock, reads = Clock(), []

    def read():
        reads.append(1)
        return True
    p = sensors.Power(read=read, clock=clock)
    assert p.factor() == sensors.BATTERY_FACTOR and p.factor() == sensors.BATTERY_FACTOR and len(reads) == 1
    clock.t = sensors.POWER_CHECK_S
    p.factor()
    assert len(reads) == 2
    assert sensors.Power(read=lambda: None).factor() == 1.0                # unknown = AC

    class P:
        def __init__(self, out, rc=0):
            self.stdout, self.returncode = out, rc
    ob = lambda out, rc=0: sensors.on_battery(run=lambda *a, **k: P(out, rc))  # noqa: E731
    assert ob("Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t75%; discharging;") is True
    assert ob("Now drawing from 'AC Power'\n") is False
    assert ob("") is None and ob("x", rc=1) is None


def test_telegram_titles_walk_is_on_demand_bounded_and_cached():
    js = drill.TELEGRAM_TITLES_JS
    assert f"NODE_MAX={drill.TITLES_NODE_MAX}" in js and "N>=NODE_MAX" in js and "CAP=false" in js
    assert "N>=NODE_MAX" in sensors.TELEGRAM_UNREAD_JS
    clock, calls = Clock(), []
    pr = drill.Providers(telegram_titles=lambda: calls.append(1) or ["Atlas - Tools", "Nova - Planning"], clock=clock)
    scope = {"kind": "telegram"}
    pr.items(scope, {})
    clock.t = drill.PROVIDER_TTL_S + 1                                     # past the generic TTL, inside Telegram's
    pr.items(scope, {})
    assert len(calls) == 1 and drill.TELEGRAM_TTL_S >= 60
    clock.t = drill.TELEGRAM_TTL_S
    pr.items(scope, {})
    assert len(calls) == 2
