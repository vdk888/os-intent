"""W24: proactive — sensors -> relevance gate -> ring hint. Synthetic data only: fake sensors (fake Mail
AppleScript, fake Telegram titles, temp folders, fake probe / feed files), fake HUD sender, fake Claude runner,
temp OI_DATA_DIR, no wiki (conftest). Nothing real is read, launched, sent or called."""
import io
import json
import os
import threading
import time
from datetime import datetime, timezone

import pytest

from oi import claude_actions, drill, glide, live, normalise, vocab
from oi.markov import Markov
from oi.moments import build_moments
from oi.orchestrator import pack
from oi.proactive import config, engine, relevance, sensors, store

from file_watcher_fake import FakeFileWatcher

NOW = datetime(2026, 10, 20, 14, 0, 0, tzinfo=timezone.utc).timestamp()      # 16:00 Paris (outside quiet hours)
NIGHT = datetime(2026, 10, 20, 21, 30, 0, tzinfo=timezone.utc).timestamp()   # 23:30 Paris

DASH = "https://dashboard.example"          # a configured ops dashboard (the default has none)
J = "111111111"                            # the user's (fictional) Telegram id
ATLAS_OB = {"transcripts": "~/.claude/projects/-Users-you-agents-atlas", "state_dir": "~/.claude/channels/atlas"}
JUNO_OB = {"transcripts": "~/.claude/projects/-Users-you-agents-juno", "state_dir": "~/.claude/channels/juno"}
# an example setup on top of the defaults (which configure no dashboard, no chat id, no agent)
EXAMPLE = {"cockpit": {"url": DASH}, "agents": {"chat_ids": [J], "outbound": {"atlas": ATLAS_OB, "juno": JUNO_OB}}}


def _cfg(raw: dict | None = None) -> dict:
    """proactive_settings of EXAMPLE overlaid by `raw` (merged per source block, and per agent in outbound)."""
    raw = dict(raw or {})
    p = {k: (dict(v) if isinstance(v, dict) else v) for k, v in EXAMPLE.items()}
    for k, v in (raw.get("proactive") or {}).items():
        if isinstance(v, dict) and isinstance(p.get(k), dict):
            merged = dict(p[k], **v)
            if k == "agents" and isinstance(v.get("outbound"), dict):
                merged["outbound"] = dict(p[k]["outbound"], **v["outbound"])
            p[k] = merged
        else:
            p[k] = v
    return config.proactive_settings(dict(raw, proactive=p))



def iso(t: float) -> str:
    return vocab.iso_utc(datetime.fromtimestamp(t, timezone.utc))


def trow(t: float, token: str, src: str = "aw-window") -> dict:
    return {"ts": iso(t), "dur": 0.0, "token": token, "app": None, "src": src, "raw_id": None}


def history_rows(n: int = 10, end: float = NOW - 600, follow: str = "activate:Mail") -> list[dict]:
    """n times: Safari, then a client mail event, then (20 s later) `follow` — every 5 minutes, ending at `end`."""
    out = []
    for i in range(n):
        t = end - (n - i) * 300
        out += [trow(t, "activate:Safari"), trow(t + 60, "evt:mail:client", "oi-evt"), trow(t + 80, follow)]
    return out


def fitted(rows):
    return Markov.fit(build_moments(rows), tokens=[r["token"] for r in rows])


def write_settings(data_dir, proactive: dict, **extra):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"proactive": proactive, **extra}))


# ---- settings ----------------------------------------------------------------------------------------------

def test_settings_default_every_source_off():
    c = _cfg({})
    assert c["sources"] == {s: False for s in config.SOURCES} and config.enabled_sources(c) == []
    assert c["min_p"] == 0.5 and c["min_interval_min"] == 10 and c["quiet_hours"] == {"start": "22:00", "end": "08:00"}
    assert c["priorities"] == ["mail:client", "cockpit:outage"] and c["respect_focus"] is True
    assert "max_age_s" not in c                   # no relevance window: an event is valid while its state is live
    assert c["mail"]["poll_s"] + 2 <= 10          # a new mail -> a hint within 10 s (sensor poll + oi live's 2 s loop)


def test_settings_parse_and_reject_bad_values():
    c = _cfg({"proactive": {
        "sources": {"mail": True, "agents": "yes", "files": True}, "min_p": 0.7, "min_interval_min": 999,
        "quiet_hours": None, "priorities": ["agent:*", "bogus", "mail:client"], "respect_focus": False,
        "mail": {"poll_s": 1, "agent_senders": ["Bot@X.example"], "client_domains": ["acme.example"]}, "files": {"folders": ["~/Desktop", 3]},
        "cockpit": {"url": "http://insecure", "alerts_file": "/tmp/a.json"}, "markets": {"move_pct": -2}}})
    assert config.enabled_sources(c) == ["mail", "files"]
    assert c["min_p"] == 0.7 and c["min_interval_min"] == 10 and c["quiet_hours"] is None
    assert c["priorities"] == ["agent:*", "mail:client"] and c["respect_focus"] is False
    assert c["mail"] == {"poll_s": 6, "agent_senders": ["bot@x.example"], "team_domains": []}          # who is a client: the wiki judge
    assert "pipeline_file" not in c
    assert c["files"]["folders"] == ["~/Desktop"] and c["cockpit"]["url"] is None          # not https: refused
    assert c["cockpit"]["alerts_file"] == "/tmp/a.json" and c["markets"]["move_pct"] == 3.0
    bad = _cfg({"proactive": {"quiet_hours": {"start": "25:00", "end": "08:00"}}})
    assert bad["quiet_hours"] == {"start": "22:00", "end": "08:00"}


# ---- tokens, store, normalise, moments ---------------------------------------------------------------------

def test_evt_tokens_grammar_and_rows():
    assert vocab.validate("evt:mail:client") and vocab.validate("evt:agent:orion") and vocab.validate("evt:cockpit:outage")
    assert not vocab.validate("evt:bank:x") and not vocab.validate("evt:mail:Sam Dupont")
    assert vocab.validate("evt:mail:client:gone") and not vocab.validate("evt:mail:client:later")
    ev = store.make("mail", "Client", ts=NOW, key="<m1@x>", who="Sam", subject="Re: NAV", ref={"message_id": "<m1@x>"})
    assert store.token(ev) == "evt:mail:client" and not vocab.validate_row(store.token_row(ev))
    assert "Sam" not in json.dumps(store.token_row(ev)) and "NAV" not in json.dumps(store.token_row(ev))
    priv = store.make("mail", "client", ts=NOW, key="<m2@x>", who="G", subject="secret", private=True)
    assert priv["subject"] is None and priv["private"] is True
    g = store.gone(ev, ts=NOW + 5)
    assert g == {"ts": iso(NOW + 5), "id": ev["id"], "source": "mail", "class": "client", "state": "gone"}
    assert store.token(g) == "evt:mail:client:gone" and not vocab.validate_row(store.token_row(g))


def test_events_file_private_and_in_the_day_tokens(data_dir):
    ev = store.make("agent", "orion", ts=NOW, key=["orion", 1], who="Orion", ref={"slug": "orion"})
    p = store.append(ev)
    assert oct(p.stat().st_mode & 0o777) == "0o600" and oct(p.parent.stat().st_mode & 0o777) == "0o700"
    store.append(store.gone(ev, ts=NOW + 40))
    _, rows = normalise.build_day(iso(NOW)[:10])
    assert [r["token"] for r in rows] == ["evt:agent:orion", "evt:agent:orion:gone"] and rows[0]["src"] == "oi-evt"


def test_live_set_is_state_not_a_window():
    """The Claude "Events" context = what is live now: an event stays however old it is, and leaves on its gone."""
    cur = store.Live()
    a = store.make("mail", "client", ts=NOW, key="a", who="A")
    b = store.make("agent", "orion", ts=NOW + 10, key="b", who="Orion")
    cur.apply(a)
    cur.apply(b)
    assert [e["id"] for e in cur.current()] == [b["id"], a["id"]]          # newest first, no age anywhere
    cur.apply(store.gone(a, ts=NOW + 99999))
    assert [e["id"] for e in cur.current()] == [b["id"]] and not cur.is_live(a["id"])


def test_an_event_is_a_moment_never_a_label():
    rows = [trow(NOW, "activate:Safari"), trow(NOW + 10, "evt:mail:client", "oi-evt"), trow(NOW + 30, "activate:Mail")]
    ms = build_moments(rows)
    assert [m["context"]["token"] for m in ms] == ["activate:Safari", "evt:mail:client", "activate:Mail"]
    assert ms[0]["label"] == "activate:Mail" and ms[1]["label"] == "activate:Mail"   # skips the event as a label
    assert ms[1]["history"][0]["token"] == "evt:mail:client"


def test_markov_learns_what_follows_an_event():
    m = fitted(history_rows(10))
    n, p = relevance.history(m, "evt:mail:client")
    assert n == 10 and p == 1.0
    assert all(y != "evt:mail:client" for y, _ in m.top_k({"history": [{"token": "activate:Safari"}],
                                                            "context": {"token": "activate:Safari"}}, 20))
    assert "Mail 100%" in engine.habit_line(m, "evt:mail:client")


def test_live_tail_keeps_event_tokens(rows, model):
    lv = live.Live(pull=False, sender=lambda *a: None)
    lv.day = rows[-1]["ts"][:10]
    lv.rows = list(rows)
    last = vocab.parse_ts(rows[-1]["ts"]).timestamp()
    lv.rows.append(trow(last + 1, "evt:file:desktop", "oi-evt"))
    lv.retokenise_tail()
    assert lv.rows[-1]["token"] == "evt:file:desktop"
    assert sum(r["token"] == "evt:file:desktop" for r in lv.rows) == 1


# ---- relevance gate ----------------------------------------------------------------------------------------

def _ev(source="mail", cls="other", t=NOW, **kw):
    return store.make(source, cls, ts=t, key=[source, cls, t], **kw)


def test_gate_reasons_in_order():
    cfg = _cfg({})
    ev = _ev(cls="client")
    g = lambda **kw: relevance.gate(ev, cfg, now=NOW + 5, last_hint_at=None, **kw)["reason"]  # noqa: E731
    assert g(paused=True) == "paused" and g(hud_enabled=False) == "hud_disabled" and g(auto_mode="off") == "auto_off"
    assert g(live=False) == "gone"                                              # its state ended: never hinted
    assert relevance.gate(ev, cfg, now=NOW + 30 * 86400, last_hint_at=None)["reason"] == "hint"   # no age limit
    assert g(front_denied=True) == "denylisted"
    assert g(focus=True) == "focus" and g(focus=None) == "hint"
    assert g() == "hint"                                                        # client mail = a priority (prior 0.4)
    other = _ev(cls="other")
    assert relevance.gate(other, cfg, now=NOW + 5, last_hint_at=None)["reason"] == "below_threshold"
    assert relevance.gate(ev, cfg, now=NOW + 5, last_hint_at=NOW - 120)["reason"] == "hint"   # a priority skips it
    assert relevance.gate(ev, cfg, now=NOW + 5, last_hint_at=NOW - 120, focus=True)["reason"] == "focus"   # not Focus
    ag = _ev("agent", "orion")
    assert relevance.gate(ag, cfg, now=NOW + 5, last_hint_at=NOW - 120)["reason"] == "rate_limited"
    assert relevance.gate(ag, cfg, now=NOW + 5, last_hint_at=NOW - 601)["reason"] == "hint"
    outage = _ev("cockpit", "outage")
    assert relevance.gate(outage, cfg, now=NOW + 5, last_hint_at=NOW - 1)["reason"] == "hint"


def test_quiet_hours_wrap_and_priorities_break_quiet():
    cfg = _cfg({})
    ev = _ev(cls="client", t=NIGHT)
    assert relevance.gate(ev, cfg, now=NIGHT + 1, last_hint_at=None)["reason"] == "quiet_hours"
    loud = _cfg({"proactive": {"priorities_break_quiet": True}})
    assert relevance.gate(ev, loud, now=NIGHT + 1, last_hint_at=None)["reason"] == "hint"
    other = _ev(cls="other", t=NIGHT)
    assert relevance.gate(other, loud, now=NIGHT + 1, last_hint_at=None)["reason"] == "quiet_hours"
    loc = datetime(2026, 1, 1, 7, 59)
    assert relevance.in_quiet(loc, {"start": "22:00", "end": "08:00"})
    assert not relevance.in_quiet(loc.replace(hour=8, minute=0), {"start": "22:00", "end": "08:00"})
    assert relevance.in_quiet(loc.replace(hour=13), {"start": "12:00", "end": "14:00"})
    assert not relevance.in_quiet(loc, None)


def test_history_and_profile_priors_blend():
    cfg = _cfg({})
    ev = _ev(cls="other", who="Newsletter", ref={"address": "news@shop.example"})
    prof = {"attention": {"sources": {"mail": 0.8}, "important": [], "noise": [{"who": "newsletter", "class": "sender",
                                                                                  "weight": 0.05}]}}
    assert relevance.gate(ev, cfg, now=NOW + 1, last_hint_at=None, profile=prof)["prior"] == 0.05   # noise
    ev2 = _ev(cls="other", who="Somebody")
    assert relevance.gate(ev2, cfg, now=NOW + 1, last_hint_at=None, profile=prof)["reason"] == "hint"   # source 0.8
    m = Markov.fit(build_moments(history_rows(10)), tokens=[r["token"] for r in history_rows(10)])
    ev3 = _ev(cls="client")
    r = relevance.gate(ev3, dict(cfg, priorities=[]), now=NOW + 1, last_hint_at=None, model=m)
    assert r["hist_n"] == 10 and r["p"] == round(10 / 15 * 1.0 + 5 / 15 * 0.4, 3) and r["reason"] == "hint"


# ---- sensors -----------------------------------------------------------------------------------------------

def mail_out(*msgs):
    return "OK" + sensors.RS + "".join(sensors.SEP.join(m) + sensors.RS for m in msgs)


def test_sender_class_is_agent_or_other_the_wiki_says_client():
    cfg = _cfg({"proactive": {"mail": {"agent_senders": ["bot@x.example"]}}})["mail"]
    assert sensors.sender_class("bot@x.example", cfg) == "agent"
    assert sensors.sender_class("lyra@team.example", cfg, agents={"lyra"}) == "agent"
    assert sensors.sender_class("alex@team.example", cfg, agents={"lyra"}) == "other"
    assert sensors.sender_class("sam@dupont-gestion.example", cfg) == "other"     # client: judge.py, not here


def test_mail_sensor_primes_then_reports_new_unread_with_privacy():
    outs = [mail_out(("<a@x>", "Old", "old@x.example", "Old one", "2026-10-20T15:00:00", "0"))]
    calls = []

    def osa(script, args, timeout=6):
        calls.append(args)
        low = script.lower().replace("sender", "")
        assert script == sensors.MAIL_NEW and "send" not in low and "delete" not in low and "set read status" not in low
        return 0, outs[-1]
    ccfg = {"local_only_domains": ["secret-client.example"], "enabled": True}
    s = sensors.MailSensor(osa=osa, claude_cfg=lambda: ccfg)
    cfg = _cfg({})
    assert s.poll(NOW, cfg) == []                                                     # primed: old mail is no event
    outs.append(mail_out(("<a@x>", "Old", "old@x.example", "Old one", "", "0"),
                         ("<b@x>", "Sam Dupont", "sam@dupont-gestion.example", "NAV question", "", "0"),
                         ("<c@x>", "Read", "r@x.example", "Already read", "", "1"),
                         ("<d@x>", "Hidden", "h@secret-client.example", "Deal terms", "", "0"),
                         ("<e@x>", "Pat", "pat@x.example", "About Secret-Client SAS", "", "0")))
    evs = s.poll(NOW + 6, cfg)
    assert [(e["class"], e["who"], e["private"]) for e in evs] == [("other", "Sam", False), ("other", "Hidden", True),
                                                                   ("other", "Pat", True)]
    assert evs[0]["subject"] == "NAV question" and evs[1]["subject"] is None and evs[2]["subject"] is None
    assert evs[0]["ref"]["message_id"] == "<b@x>" and evs[0]["ref"]["name"] == "Sam Dupont"   # the judge's query
    assert s.poll(NOW + 12, cfg) == []                                                # seen, still unread: no change
    outs.append("NOTRUNNING")
    assert s.poll(NOW + 18, cfg) == [] and calls[0] == [str(sensors.MAIL_SCAN), str(sensors.MAIL_BUDGET_S)]     # unknown: nothing ends


def test_mail_read_failure_is_logged_once_per_hour_never_when_not_running():
    res, logged = [(1, "")], []
    s = sensors.MailSensor(osa=lambda script, args, timeout=6: res[-1], claude_cfg=lambda: {}, log=logged.append,
                           running=lambda: True)
    cfg = _cfg({})
    assert s.poll(NOW, cfg) == [] and s.poll(NOW + 6, cfg) == []                  # failed twice: one line
    assert len(logged) == 1 and "rc=1" in logged[0]
    assert s.poll(NOW + sensors.MAIL_ERR_LOG_S + 1, cfg) == [] and len(logged) == 2  # an hour later: again
    res.append((0, "NOTRUNNING"))
    assert s.poll(NOW + 3 * sensors.MAIL_ERR_LOG_S, cfg) == [] and len(logged) == 2  # Mail closed: no log


def test_mail_event_ends_when_read_or_replied_or_gone():
    state = {"out": mail_out(), "lookup": {}}
    looked = []

    def osa(script, args, timeout=6):
        if script == sensors.MAIL_STATE:
            looked.append(list(args))
            return 0, "OK" + sensors.RS + "".join(f"{k}{sensors.SEP}{v}{sensors.RS}" for k, v in state["lookup"].items())
        return 0, state["out"]
    s = sensors.MailSensor(osa=osa, claude_cfg=lambda: {"local_only_domains": []})
    cfg = _cfg({})
    m = lambda mid, rd="0", rp="0": (mid, "N", "n@x.example", "Hi", "", rd, rp)  # noqa: E731
    assert s.poll(NOW, cfg) == []
    state["out"] = mail_out(m("<1>"), m("<2>"), m("<3>"))
    evs = s.poll(NOW + 6, cfg)
    ids = {e["ref"]["message_id"]: e["id"] for e in evs}
    assert sorted(ids) == ["<1>", "<2>", "<3>"] and all(e["state"] == "live" for e in evs)
    state["out"] = mail_out(m("<1>", rd="1"), m("<2>", rp="1"), m("<3>"))             # read, replied, still unread
    ended = s.poll(NOW + 12, cfg)
    assert sorted((e["id"], e["state"]) for e in ended) == sorted([(ids["<1>"], "gone"), (ids["<2>"], "gone")])
    assert [store.token(e) for e in ended] == ["evt:mail:other:gone"] * 2
    state["out"] = mail_out(*[m(f"<n{i}>", rd="1") for i in range(8)])               # <3> left the scan: looked up
    state["lookup"] = {"<3>": "unread"}
    assert s.poll(NOW + 10 ** 5, cfg) == [] and looked[-1] == [str(sensors.MAIL_BUDGET_S), "<3>"]                # still unread: stays, at any age
    state["lookup"] = {}                                                               # Mail could not say: unknown
    assert s.poll(NOW + 10 ** 5 + 6, cfg) == []
    state["lookup"] = {"<3>": "gone"}                                                  # archived / deleted
    assert [(e["id"], e["state"]) for e in s.poll(NOW + 10 ** 5 + 12, cfg)] == [(ids["<3>"], "gone")]
    assert "was replied to" in sensors.MAIL_STATE and "is not running" in sensors.MAIL_STATE


def test_mail_slow_osascript_is_capped_and_fail_soft(tmp_path, monkeypatch):
    """A Mail that never answers (a fake `osascript` that sleeps) is cut at MAIL_TIMEOUT_S: no exception, no
    event, `osa_timeout`, then a back-off (no read hammering a slow Mail every poll)."""
    fake = tmp_path / "osascript"
    fake.write_text("#!/bin/sh\ncat >/dev/null\nexec sleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(sensors, "MAIL_TIMEOUT_S", 0.5)
    logged = []
    s = sensors.MailSensor(claude_cfg=lambda: {}, log=logged.append, running=lambda: True)   # the real ca.osascript
    cfg = config.proactive_settings({})
    t = time.monotonic()
    assert s.poll(NOW, cfg) == [] and time.monotonic() - t < 5
    assert s.last_error == "osa_timeout" and s.backoff_s == sensors.MAIL_BACKOFF_S and len(logged) == 1
    assert s.poll(NOW + 6, cfg) == [] and s.skipped                                # backing off: no read
    assert sensors.MAIL_BUDGET_S < 25                                             # the script ends before the cap


def test_mail_partial_read_keeps_what_answered():
    """An account slower than MAIL_BUDGET_S is skipped by the script: the others still count, the poll is not a
    failure, the slow account's live mails stay unknown (no lookup), and a partial first read never primes."""
    res, looked, logged = [], [], []

    def osa(script, args, timeout=6):
        if script == sensors.MAIL_STATE:
            looked.append(args)
            return 0, "OK" + sensors.RS
        return 0, res[-1]
    skip = lambda n, total=2: "SKIPPED" + sensors.SEP + str(n) + sensors.SEP + str(total) + sensors.RS  # noqa: E731
    m = lambda mid: (mid, "N", "n@x.example", "Hi", "", "0", "0")  # noqa: E731
    s = sensors.MailSensor(osa=osa, claude_cfg=lambda: {"local_only_domains": []}, log=logged.append,
                           running=lambda: True)
    cfg = config.proactive_settings({})
    res.append(mail_out(m("<old>")) + skip(1))
    assert s.poll(NOW, cfg) == [] and s.states.pre is None and s.last_error is None   # not primed on a partial read
    res.append(mail_out(m("<old>"), m("<old2>")))
    assert s.poll(NOW + 6, cfg) == [] and s.states.pre is not None                    # a full read primes
    res.append(mail_out(m("<old>"), m("<old2>"), m("<new>")) + skip(1))
    evs = s.poll(NOW + 12, cfg)
    assert [e["ref"]["message_id"] for e in evs] == ["<new>"] and s.last_error is None and s.backoff_s == 0
    res.append(mail_out() + skip(1))                                                  # <new>'s account is the slow one
    assert s.poll(NOW + 18, cfg) == [] and looked == []                              # unknown: stays live, no lookup
    assert len(logged) == 1 and "1/2" in logged[0]                                   # once per hour
    assert sensors.mail_skipped(mail_out() + skip(1)) == (1, 2) and sensors.mail_skipped(mail_out()) == (0, 0)


def test_mail_no_account_in_time_is_a_timeout():
    res = [mail_out() + "SKIPPED" + sensors.SEP + "3" + sensors.SEP + "3" + sensors.RS]
    s = sensors.MailSensor(osa=lambda script, args, timeout=6: (0, res[-1]), claude_cfg=lambda: {},
                           log=lambda m: None, running=lambda: True)
    cfg = config.proactive_settings({})
    assert s.poll(NOW, cfg) == [] and s.last_error == "osa_timeout" and s.backoff_s == sensors.MAIL_BACKOFF_S
    res.append("TIMEOUT")                                                             # the account list itself
    assert s.poll(NOW + 10 ** 4, cfg) == [] and s.last_error == "osa_timeout"
    assert s.backoff_s == 2 * sensors.MAIL_BACKOFF_S


def test_mail_scripts_are_bounded():
    new, st = sensors.MAIL_NEW, sensors.MAIL_STATE
    assert "messages 1 thru maxN of mb" in new and "with timeout of vLeft seconds" in new
    assert new.index("messages 1 thru maxN of mb") < new.index("count of messages of mb")   # counted only on fallback
    assert "with timeout of vLeft seconds" in st and "(rest of argv)" in st
    assert new.count("if en is -1712 then error number en") >= 6                    # a timeout is never swallowed
    assert sensors.MAIL_TIMEOUT_S == 25


def test_mail_script_never_launches_mail():
    assert 'if application "Mail" is not running then return "NOTRUNNING"' in sensors.MAIL_NEW
    assert sensors.MAIL_NEW.index("is not running") < sensors.MAIL_NEW.index('tell application "Mail"')
    assert sensors.parse_mail_list("NOTRUNNING") is None and sensors.parse_mail_list("OK" + sensors.RS) == []


def test_agent_sensor_unread_badge_state():
    """An agent chat with an unread badge = a live event; the badge gone (chat read) = its gone row."""
    chats = [{"Lyra - Sales": 2, "Nova - CEO": 0, "Atlas - R&D": 0}]        # Lyra already unread at start
    dock = [3]
    s = sensors.AgentSensor(unread=lambda: chats[-1], dock_badge=lambda: dock[-1], running=lambda: True,
                            outbound=FakeOutbound(None))
    cfg = _cfg({})
    start = s.poll(NOW, cfg)                                               # waiting at start = live state, shown
    assert [store.token(e) for e in start] == ["evt:agent:lyra"]
    chats.append({"Orion - Fund": 1, "Lyra - Sales": 2, "Nova - CEO": 0, "Some friend": 4})
    evs = s.poll(NOW + 8, cfg)
    assert [store.token(e) for e in evs] == ["evt:agent:orion"] and evs[0]["ref"] == {"chat": "Orion - Fund", "slug": "orion", "n": 1}
    chats.append({"Orion - Fund": 3, "Lyra - Sales": 2, "Nova - CEO": 0})
    ref = s.poll(NOW + 16, cfg)                                            # more messages: the same state, refreshed
    assert [(e["id"], store.token(e), e["ref"]["n"]) for e in ref] == [(evs[0]["id"], "evt:agent:orion", 3)]
    assert s.poll(NOW + 20, cfg) == []                                     # same count: nothing new
    chats.append({"Lyra - Sales": 2, "Nova - CEO": 0})
    assert s.poll(NOW + 24, cfg) == []                                     # Orion's row not in the list: unknown
    chats.append(None)
    assert s.poll(NOW + 32, cfg) == [] and s.last_error == "chats_unreadable"   # unreadable, Telegram runs: failed
    chats.append({"Orion - Fund": 0, "Lyra - Sales": 2})
    gone = s.poll(NOW + 10 ** 5, cfg)                                      # read (however late): the event ends
    assert [(e["id"], store.token(e)) for e in gone] == [(evs[0]["id"], "evt:agent:orion:gone")]
    chats.append({"Orion - Fund": 1, "Lyra - Sales": 0})
    again = s.poll(NOW + 10 ** 5 + 8, cfg)                                 # Lyra read: gone; Orion unread again: new
    assert [(e["id"], store.token(e)) for e in again[:1]] == [(start[0]["id"], "evt:agent:lyra:gone")]
    assert [store.token(e) for e in again[1:]] == ["evt:agent:orion"] and again[1]["id"] != evs[0]["id"]
    chats.append({"Orion - Fund": 1, "Lyra - Sales": 1})
    lyra = s.poll(NOW + 10 ** 5 + 16, cfg)                                 # unread again = a new event
    assert [store.token(e) for e in lyra] == ["evt:agent:lyra"] and lyra[0]["id"] != start[0]["id"]
    dock.append(0)                                                         # no Dock badge: every chat read
    assert sorted(store.token(e) for e in s.poll(NOW + 10 ** 5 + 24, cfg)) == ["evt:agent:lyra:gone", "evt:agent:orion:gone"]


class FakeOutbound:
    """sensors.OutboundLog stand-in: the same pending answer (dict | None) for every agent."""
    def __init__(self, result):
        self.result, self.calls = result, []

    def pending(self, spec, ids, after=None):
        self.calls.append((spec, frozenset(ids), after))
        return self.result


def _ob_cfg(**over):
    return _cfg({"proactive": {"agents": {"outbound": {
        "atlas": {"transcripts": "/x/atlas", "state_dir": "/x/rnd"}, "juno": None, **over}}}})


def test_agent_sensor_outbound_fallback_when_the_chat_list_is_unreadable():
    """Telegram for macOS exposes no chat list: the badge says something is unread, the Mac agents' own records
    say who wrote (n, since); a reply from the user (n = 0) or the badge gone ends it."""
    pend = {"atlas": None}
    dock = [2]

    class Ob(FakeOutbound):
        def pending(self, spec, ids, after=None):
            self.calls.append((spec["transcripts"], frozenset(ids), after))
            return pend["atlas"]
    ob = Ob(None)
    s = sensors.AgentSensor(unread=lambda: None, dock_badge=lambda: dock[-1], running=lambda: True, outbound=ob,
                            titles=lambda: {"atlas": "Atlas - R&D"}, names=lambda: {"atlas": "Atlas"})
    cfg = _ob_cfg()
    cfg["agents"]["bots"] = {"atlas": "AtlasAgentBot"}
    pend["atlas"] = {"n": 0, "first": None, "last": None}
    assert s.poll(NOW, cfg) == [] and s.last_error is None                 # primed (nothing pending)
    assert ob.calls[-1] == ("/x/atlas", frozenset({"111111111"}), None)    # juno removed by settings
    pend["atlas"] = {"n": 2, "first": NOW - 120, "last": NOW - 30}
    evs = s.poll(NOW + 8, cfg)
    assert [store.token(e) for e in evs] == ["evt:agent:atlas"] and evs[0]["who"] == "Atlas"
    assert evs[0]["ref"] == {"chat": "Atlas - R&D", "slug": "atlas", "n": 2, "since": vocab.iso_utc(
        datetime.fromtimestamp(NOW - 120, timezone.utc)), "bot": "AtlasAgentBot"}
    pend["atlas"] = {"n": 3, "first": NOW - 120, "last": NOW + 10}
    ref = s.poll(NOW + 16, cfg)                                            # one more message: the same state, refreshed
    assert [(e["id"], e["ref"]["n"], e["ts"]) for e in ref] == [(evs[0]["id"], 3, vocab.iso_utc(
        datetime.fromtimestamp(NOW + 16, timezone.utc)))]
    assert s.poll(NOW + 20, cfg) == []                                     # same count: nothing new
    pend["atlas"] = None
    assert s.poll(NOW + 24, cfg) == [] and s.last_error == "chats_unreadable"   # no record: unknown + failed
    pend["atlas"] = {"n": 0, "first": None, "last": None}                   # The user answered Atlas
    assert [store.token(e) for e in s.poll(NOW + 32, cfg)] == ["evt:agent:atlas:gone"]
    pend["atlas"] = {"n": 1, "first": NOW + 40, "last": NOW + 40}
    assert [store.token(e) for e in s.poll(NOW + 48, cfg)] == ["evt:agent:atlas"]
    dock.append(0)                                                         # Telegram all read
    assert [store.token(e) for e in s.poll(NOW + 56, cfg)] == ["evt:agent:atlas:gone"] and s.read_at == NOW + 56
    dock.append(1)
    s.poll(NOW + 64, cfg)
    assert ob.calls[-1][2] == NOW + 56                                     # only what came after the badge was 0


def test_agent_waiting_at_start_shows_on_the_first_poll():
    """Live test 2026-10-21 16:14Z: Atlas had written the user (badge 6, pending in his outbound record) before `oi live`
    started, and no event came: a waiting agent is live state (PLAN: context is state), it shows on the first poll,
    a new message refreshes it, the user reading it ends it."""
    pend = {"atlas": {"n": 1, "first": NOW - 600, "last": NOW - 600}}
    dock = [6]

    class Ob(FakeOutbound):
        def pending(self, spec, ids, after=None):
            return pend["atlas"]
    s = sensors.AgentSensor(unread=lambda: None, dock_badge=lambda: dock[-1], running=lambda: True, outbound=Ob(None),
                            titles=lambda: {"atlas": "Atlas - R&D"}, names=lambda: {"atlas": "Atlas"})
    cfg = _ob_cfg()
    first = s.poll(NOW, cfg)
    assert [(store.token(e), e["ref"]["n"]) for e in first] == [("evt:agent:atlas", 1)] and s.last_error is None
    pend["atlas"] = {"n": 2, "first": NOW - 600, "last": NOW + 5}
    ref = s.poll(NOW + 8, cfg)
    assert [(e["id"], store.token(e), e["ref"]["n"]) for e in ref] == [(first[0]["id"], "evt:agent:atlas", 2)]
    dock.append(0)                                                         # The user read it
    assert [(e["id"], store.token(e)) for e in s.poll(NOW + 16, cfg)] == [(first[0]["id"], "evt:agent:atlas:gone")]
    assert s.poll(NOW + 24, cfg) == []


def test_refreshed_agent_event_is_gated_again_and_stays_one_live_event():
    """The engine side: a refresh = the same id in `current` (one live event, its new ref), re-offered to the gate."""
    cur = store.Live()
    a = store.make("agent", "atlas", ts=NOW, key=["atlas", 1], who="Atlas", ref={"slug": "atlas", "n": 1})
    b = dict(store.make("agent", "atlas", ts=NOW + 8, key=["atlas", 1], who="Atlas", ref={"slug": "atlas", "n": 2}))
    cur.apply(a)
    cur.apply(b)
    assert [e["ref"]["n"] for e in cur.current()] == [2]


def test_agent_sensor_health_rule():
    """A read that fails while Telegram RUNS is a failed poll (like Mail); Telegram closed: the state is unknown and
    health shows `not_running` as info."""
    cfg = _ob_cfg()
    closed = sensors.AgentSensor(unread=lambda: None, dock_badge=lambda: None, running=lambda: False,
                                 outbound=FakeOutbound(None))
    assert closed.poll(NOW, cfg) == [] and closed.last_error == "not_running"
    nobadge = sensors.AgentSensor(unread=lambda: None, dock_badge=lambda: None, running=lambda: True,
                                  outbound=FakeOutbound(None))
    assert nobadge.poll(NOW, cfg) == [] and nobadge.last_error == "badge_unreadable"
    blind = sensors.AgentSensor(unread=lambda: None, dock_badge=lambda: 3, running=lambda: True,
                                outbound=FakeOutbound(None))
    assert blind.poll(NOW, cfg) == [] and blind.last_error == "chats_unreadable"
    ok = sensors.AgentSensor(unread=lambda: None, dock_badge=lambda: 3, running=lambda: True,
                             outbound=FakeOutbound({"n": 0, "first": None, "last": None}))
    assert ok.poll(NOW, cfg) == [] and ok.last_error is None               # no chat list, but the records read
    ok.dock_badge = lambda: 0
    assert ok.poll(NOW + 8, cfg) == [] and ok.last_error is None
    # the engine records it in health/sensors.json, so `oi health` flags it after FAILS_UNHEALTHY polls
    from oi import health as health_mod
    h = health_mod.SensorHealth(path=lambda: None)
    eng = engine.Engine(FakeLiveForHealth(), sensors={"agents": blind}, health=h, judge=lambda ev: {},
                        raw_settings=lambda: {"proactive": {"sources": {"agents": True},
                                                            "agents": {"poll_s": 2, "outbound": {"juno": None}}}})
    for i in range(3):
        eng.poll_once(NOW + i * 10)
    snap = h.snapshot(NOW + 30, ["agents"])["sources"]["agents"]
    assert snap["consecutive_failures"] == 3 and snap["last_error_kind"] == "chats_unreadable"


class FakeLiveForHealth:
    def _hud_cmd(self, payload):
        return {}


def test_outbound_log_reads_replies_to_the_user_after_their_last_message(tmp_path):
    """The Mac agents' own records: reply tool calls to the user in the main session (not a subagent, not another chat),
    after the user's last message in the bot's delivery ledger; read incrementally; texts only on demand."""
    tr, st = tmp_path / "proj", tmp_path / "chan"
    tr.mkdir(), st.mkdir()
    J = "111111111"

    def iso(t):
        return vocab.iso_utc(datetime.fromtimestamp(t, timezone.utc))

    def reply(t, chat, text, side=False):
        return json.dumps({"type": "assistant", "isSidechain": side, "timestamp": iso(t), "message": {"content": [
            {"type": "text", "text": "thinking"},
            {"type": "tool_use", "name": sensors.REPLY_TOOL, "id": "t", "input": {"chat_id": chat, "text": text}}]}})
    f = tr / "s1.jsonl"
    f.write_text("\n".join([
        json.dumps({"type": "user", "timestamp": iso(NOW - 900), "message": {"content": "hi"}}),
        reply(NOW - 800, J, "old, before the user wrote"),
        reply(NOW - 300, J, "Atlas: PR #12 ready, merge?"),
        reply(NOW - 250, "222222222", "to someone else"),
        reply(NOW - 200, J, "from a subagent", side=True),
        reply(NOW - 100, J, "Second: need your go on the deploy"),
    ]) + "\n")
    (st / "delivery-ledger.jsonl").write_text("\n".join([
        json.dumps({"update_id": 1, "kind": "text", "ts": iso(NOW - 600), "chat_id": J, "user_id": J}),
        json.dumps({"update_id": 2, "kind": "text", "ts": iso(NOW - 60), "chat_id": "222222222", "user_id": "222222222"}),
    ]) + "\n")
    spec = {"transcripts": str(tr), "state_dir": str(st)}
    ob = sensors.OutboundLog(clock=lambda: NOW)
    assert ob.pending(spec, {J}) == {"n": 2, "first": NOW - 300, "last": NOW - 100}
    assert ob.pending(spec, {J}, after=NOW - 200) == {"n": 1, "first": NOW - 100, "last": NOW - 100}
    with open(f, "a") as fh:                                              # appended: read incrementally
        fh.write(reply(NOW - 50, J, "Third") + "\n" + '{"partial": ')
    assert ob.pending(spec, {J})["n"] == 3
    assert ob.texts(spec, {J}) == ["Atlas: PR #12 ready, merge?", "Second: need your go on the deploy", "Third"]
    with open(st / "delivery-ledger.jsonl", "a") as fh:                   # The user answered
        fh.write(json.dumps({"update_id": 3, "kind": "text", "ts": iso(NOW - 10), "chat_id": J, "user_id": J}) + "\n")
    assert ob.pending(spec, {J}) == {"n": 0, "first": None, "last": None} and ob.texts(spec, {J}) == []
    assert ob.pending({"transcripts": str(tmp_path / "none"), "state_dir": str(st)}, {J}) is None   # no record
    assert all(isinstance(t, float) for v in ob.files.values() for t in v[2])   # timestamps only are kept


def test_agents_settings_parse():
    d = config.proactive_settings({})["agents"]
    assert d["chat_ids"] == [] and d["outbound"] == {} and d["bots"] == {}          # nothing configured by default
    a = _cfg({"proactive": {"agents": {
        "chat_ids": [123456, "abc", True], "bots": {"atlas": "@AtlasAgentBot", "bad slug": "x", "orion": "no spaces bot"},
        "outbound": {"juno": None, "lyra": {"transcripts": "/p", "state_dir": "/s", "x": 1}, "orion": {"transcripts": 3}}}}})["agents"]
    assert a["chat_ids"] == ["123456"] and a["bots"] == {"atlas": "AtlasAgentBot"}
    assert a["outbound"] == {"atlas": ATLAS_OB, "lyra": {"transcripts": "/p", "state_dir": "/s"}}
    assert drill.bot_name("AtlasAgentBot") and not drill.bot_name("evil&x=1bot") and not drill.bot_name("Atlas")


def test_telegram_unread_readers_are_read_only():
    js = sensors.TELEGRAM_UNREAD_JS
    assert "activate" not in js and "AXPress" not in js and "keystroke" not in js
    assert "slice(0,40),n]" in js                                          # title + count only leave the script

    class P:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out
    rd = lambda rc, out: sensors.telegram_dock_badge(run=lambda *a, **k: P(rc, out))  # noqa: E731
    assert rd(0, '"StatusLabel"={ "label"="5" }') == 5 and rd(0, '"StatusLabel"={ "label"="" }') == 0
    assert rd(0, '"StatusLabel"=[ NULL ]') == 0 and rd(0, "") is None and rd(1, "x") is None
    assert rd(0, '"StatusLabel"={ "label"="•" }') == 1
    # macOS 27: lsappinfo prints no StatusLabel key at all, even for a badged app -> unreadable, never "no badge"
    assert rd(0, '[ NULL ]  ASN:0x0-0x15a25a1: \n    bundleID=[ NULL ] \n') is None
    tile = lambda rc, out: sensors.dock_tile_badge(run=lambda *a, **k: P(rc, out))  # noqa: E731
    assert tile(0, '{"label":"4"}') == 4 and tile(0, '{"label":""}') == 0 and tile(0, '{"label":"•"}') == 1
    assert tile(0, "null") is None and tile(1, "") is None and tile(0, "garbage") is None
    dj = sensors.DOCK_BADGE_JS
    assert "AXStatusLabel" in dj and "activate" not in dj and "AXPress" not in dj and "keystroke" not in dj


def test_folder_sensor(tmp_path):
    """FolderSensor = a thin reader of oi-watcher's file bucket (TCC finding 2026-10-21)."""
    d = tmp_path / "Desk"
    fw = FakeFileWatcher([str(d)], now=NOW)
    fw.live(str(d / "before.pdf"), NOW - 50)                              # posted before oi live started: primed
    s = fw.sensor()
    cfg = _cfg({"proactive": {"files": {"folders": [str(d)]}}})
    assert s.poll(NOW, cfg) == [] and s.last_error is None
    for n in ("report.pdf", ".hidden", "big.zip.crdownload", "Screenshot 2026-10-20 at 18.00.00.png"):
        fw.live(str(d / n), NOW + 3)
    fw.live(str(tmp_path / "Other" / "x.pdf"), NOW + 3)                   # not a chosen folder
    fw.now = NOW + 5
    evs = s.poll(NOW + 5, cfg)
    assert [(e["class"], e["subject"], e["ts"]) for e in evs] == [("desk", "report.pdf", "2026-10-20T14:00:03.000Z")]
    assert evs[0]["ref"] == {"path": str(d / "report.pdf")}
    assert s.poll(NOW + 10, cfg) == []                                     # the same events again: nothing new
    assert sensors.folder_class(os.path.expanduser("~/Desktop")) == "desktop"
    assert sensors.folder_class(os.path.expanduser("~/Library/CloudStorage/GoogleDrive-x/My Drive")) == "drive"


def test_file_event_ends_when_opened_or_removed(tmp_path):
    d = tmp_path / "Drop"
    fw = FakeFileWatcher([str(d)], now=NOW)
    s = fw.sensor()
    cfg = _cfg({"proactive": {"files": {"folders": [str(d)]}}})
    assert s.poll(NOW, cfg) == []
    fw.live(str(d / "a.pdf"), NOW + 1)
    fw.live(str(d / "b.pdf"), NOW + 2)
    evs = {e["subject"]: e for e in s.poll(NOW + 5, cfg)}
    assert sorted(evs) == ["a.pdf", "b.pdf"]
    fw.now = NOW + 10 ** 5
    assert s.poll(NOW + 10 ** 5, cfg) == []                               # unopened: still new, however old
    fw.gone(str(d / "a.pdf"), NOW + 10 ** 5 + 1, "opened")               # the watcher saw kMDItemLastUsedDate change
    assert [(e["id"], e["state"]) for e in s.poll(NOW + 10 ** 5 + 5, cfg)] == [(evs["a.pdf"]["id"], "gone")]
    fw.gone(str(d / "b.pdf"), NOW + 10 ** 5 + 6)                          # removed
    assert [(e["id"], e["state"]) for e in s.poll(NOW + 10 ** 5 + 10, cfg)] == [(evs["b.pdf"]["id"], "gone")]
    assert sensors.folder_class(os.path.expanduser("~/Library/CloudStorage/GoogleDrive-x/My Drive")) == "drive"


def test_file_sensor_failures_are_unknown_and_counted(tmp_path):
    d = tmp_path / "Drop"
    fw = FakeFileWatcher([str(d)], now=NOW)
    s = fw.sensor()
    cfg = _cfg({"proactive": {"files": {"folders": [str(d)]}}})
    assert s.poll(NOW, cfg) == []
    fw.live(str(d / "a.pdf"), NOW + 1)
    [ev] = s.poll(NOW + 5, cfg)
    fw.folders[str(d)] = "unreadable"                                    # the watcher lost access: a failed poll
    assert s.poll(NOW + 10, cfg) == [] and s.last_error == "folder_unreadable"
    fw.folders[str(d)] = "missing"
    assert s.poll(NOW + 15, cfg) == [] and s.last_error == "folder_missing"
    fw.folders[str(d)] = "ok"
    fw.aw_down = True                                                     # aw-server unreadable: failed, unknown
    assert s.poll(NOW + 20, cfg) == [] and s.last_error == "aw_unreadable"
    fw.aw_down = False
    fw.running = False                                                    # the watcher stopped: info, unknown
    assert s.poll(NOW + 25, cfg) == [] and s.last_error == "watcher_not_running"
    fw.running, fw.now = True, NOW + 25
    fw.folders = {}                                                       # an old watcher: the folder not reported
    assert s.poll(NOW + 30, cfg) == [] and s.last_error == "folder_unreadable"
    fw.folders = {str(d): "ok"}
    assert s.poll(NOW + 35, cfg) == [] and s.last_error is None           # back: still the same live file
    other = _cfg({"proactive": {"files": {"folders": [str(tmp_path / "Else")]}}})
    fw.folders = {str(tmp_path / "Else"): "ok"}
    assert [(e["id"], e["state"]) for e in s.poll(NOW + 40, other)] == [(ev["id"], "gone")]   # folder no longer chosen


def test_file_sensor_keeps_the_newest_files(tmp_path):
    d = tmp_path / "Drop"
    fw = FakeFileWatcher([str(d)], now=NOW)
    s = fw.sensor()
    cfg = _cfg({"proactive": {"files": {"folders": [str(d)]}}})
    assert s.poll(NOW, cfg) == []
    for i in range(sensors.FILES_LIVE + 2):
        fw.live(str(d / f"f{i:02d}.pdf"), NOW + 1 + i)
    evs = s.poll(NOW + 60, cfg)
    assert [e["subject"] for e in evs] == [f"f{i:02d}.pdf" for i in range(2, sensors.FILES_LIVE + 2)]


def test_cockpit_sensor_outage_and_alert_feed(tmp_path):
    ok = [True]
    feed = tmp_path / "alerts.json"
    feed.write_text(json.dumps([{"id": "a1", "title": "old"}]))
    s = sensors.CockpitSensor(probe=lambda url: ok[-1])
    cfg = _cfg({"proactive": {"cockpit": {"alerts_file": str(feed)}}})
    a1 = s.poll(NOW, cfg)                                                  # open at start = live state, shown
    assert [store.token(e) for e in a1] == ["evt:cockpit:alert"]
    ok.append(False)
    assert s.poll(NOW + 60, cfg) == []                                     # one failure is not an outage
    evs = s.poll(NOW + 120, cfg)
    assert [store.token(e) for e in evs] == ["evt:cockpit:outage"] and evs[0]["ref"]["url"] == DASH + "/"
    assert s.poll(NOW + 180, cfg) == []                                    # still down: the same state
    ok.append(True)
    feed.write_text(json.dumps([{"id": "a1"}, {"id": "a2", "title": "NAV stale", "kind": "nav", "path": "/dept/orion"}]))
    got = s.poll(NOW + 240, cfg)
    assert [store.token(e) for e in got] == ["evt:cockpit:outage:gone", "evt:cockpit:nav"]   # back up = outage over
    assert got[0]["id"] == evs[0]["id"] and got[1]["ref"]["url"] == DASH + "/dept/orion"
    assert s.poll(NOW + 10 ** 5, cfg) == []                                # open alert: live, however old
    feed.write_text("not json")
    assert s.poll(NOW + 10 ** 5 + 60, cfg) == []                           # feed unreadable: unknown
    feed.write_text(json.dumps([{"id": "a1"}, {"id": "a2", "open": False}]))
    closed = s.poll(NOW + 10 ** 5 + 120, cfg)
    assert [(e["id"], store.token(e)) for e in closed] == [(got[1]["id"], "evt:cockpit:nav:gone")]
    feed.write_text(json.dumps([]))
    assert [(e["id"], store.token(e)) for e in s.poll(NOW + 10 ** 5 + 180, cfg)] == [(a1[0]["id"], "evt:cockpit:alert:gone")]


def test_metric_and_market_stubs(tmp_path):
    mf, kf = tmp_path / "m.json", tmp_path / "k.json"
    mf.write_text(json.dumps({"metrics": [{"id": "aum", "label": "AUM", "value": 10, "threshold": 5}]}))
    kf.write_text(json.dumps({"brief": {"date": "2026-10-19"}, "moves": []}))
    cfg = _cfg({"proactive": {"metrics": {"file": str(mf)}, "markets": {"file": str(kf)}}})
    ms, ks = sensors.MetricsSensor(), sensors.MarketsSensor()
    assert ms.poll(NOW, cfg) == []                                         # above its threshold: nothing
    old = ks.poll(NOW, cfg)                                                # an unread brief at start is live
    assert [store.token(e) for e in old] == ["evt:market:brief"]
    mf.write_text(json.dumps({"metrics": [{"id": "aum", "label": "AUM", "value": 4, "threshold": 5, "owner": "orion"}]}))
    kf.write_text(json.dumps({"brief": {"date": "2026-10-20"}, "moves": [{"symbol": "CW8", "pct": -4.2}, {"symbol": "X", "pct": 1}]}))
    below = ms.poll(NOW + 300, cfg)
    assert [store.token(e) for e in below] == ["evt:metric:aum"]
    assert [store.token(e) for e in ks.poll(NOW + 300, cfg)] == ["evt:market:brief:gone", "evt:market:brief",
                                                                 "evt:market:move"]
    assert ms.poll(NOW + 600, cfg) == [] and ks.poll(NOW + 600, cfg) == []
    mf.write_text(json.dumps({"metrics": [{"id": "aum", "label": "AUM", "value": 6, "threshold": 5}]}))
    assert [(e["id"], store.token(e)) for e in ms.poll(NOW + 900, cfg)] == [(below[0]["id"], "evt:metric:aum:gone")]


def test_market_move_is_a_state_fired_on_entering_it(tmp_path):
    """Keyed on the state (beyond ±move_pct), not on the hour: fires on entering, again only after it ended."""
    kf = tmp_path / "k.json"
    w = lambda moves, brief=None: kf.write_text(json.dumps({"brief": brief or {}, "moves": moves}))  # noqa: E731
    cfg = _cfg({"proactive": {"markets": {"file": str(kf), "move_pct": 3}}})
    ks = sensors.MarketsSensor()
    w([{"symbol": "CW8", "pct": 1.0}])
    assert ks.poll(NOW, cfg) == []
    w([{"symbol": "CW8", "pct": -4.2}])
    first = ks.poll(NOW + 300, cfg)
    assert [(store.token(e), e["subject"]) for e in first] == [("evt:market:move", "CW8 moved down")]
    for k in range(1, 30):                                                 # beyond for hours: never a second event
        w([{"symbol": "CW8", "pct": -4.2 - k / 10}])
        assert ks.poll(NOW + 300 + k * 3600, cfg) == []
    w([{"symbol": "CW8", "pct": -1.0}])
    assert [(e["id"], e["state"]) for e in ks.poll(NOW + 10 ** 6, cfg)] == [(first[0]["id"], "gone")]
    w([{"symbol": "CW8", "pct": -3.5}])
    again = ks.poll(NOW + 10 ** 6 + 300, cfg)                              # ended, then entered again: a new event
    assert [store.token(e) for e in again] == ["evt:market:move"] and again[0]["id"] != first[0]["id"]
    w([{"symbol": "CW8", "pct": -3.5}], {"date": "2026-10-21"})
    br = ks.poll(NOW + 10 ** 6 + 600, cfg)
    assert [store.token(e) for e in br] == ["evt:market:brief"]
    w([{"symbol": "CW8", "pct": -3.5}], {"date": "2026-10-21", "read": True})
    assert [(e["id"], e["state"]) for e in ks.poll(NOW + 10 ** 6 + 900, cfg)] == [(br[0]["id"], "gone")]
    w([{"symbol": "CW8", "pct": -3.5}], {"date": "2026-10-21", "read": True})
    kf.write_text("broken")
    assert ks.poll(NOW + 10 ** 6 + 1200, cfg) == []                       # unreadable: unknown, nothing ends


def test_focus_reader(tmp_path):
    p = tmp_path / "Assertions.json"
    assert sensors.focus_on(p) is None
    p.write_text(json.dumps({"data": [{"storeAssertionRecords": [{"x": 1}]}]}))
    assert sensors.focus_on(p) is True
    p.write_text(json.dumps({"data": [{"storeAssertionRecords": []}]}))
    assert sensors.focus_on(p) is False


# ---- actions -----------------------------------------------------------------------------------------------

NAMES = {"orion": "Orion", "atlas": "Atlas", "nova": "Nova"}


def test_event_options_per_source(tmp_path, monkeypatch):
    mail = _ev(cls="client", who="Sam", subject="NAV du fonds", ref={"message_id": "<b@x>"})
    o = engine.event_options(mail, 0.9, names=NAMES, route=lambda t: ("orion", 1.0))
    assert [(x["primitive"], x["label"]) for x in o] == [("mail.reply", "Reply to Sam"), ("mail.show", "Open the mail"),
                                                         ("agent.ask", "Ask Orion")]
    assert o[0]["params"] == {"message_id": "<b@x>"} and o[0]["claude"] is True
    priv = dict(mail, private=True, subject=None)
    po = engine.event_options(priv, 0.9, names=NAMES, route=lambda t: ("orion", 1.0))
    assert [x["primitive"] for x in po] == ["mail.reply", "mail.show"] and not any(x["claude"] for x in po)
    ag = _ev("agent", "orion", ref={"chat": "Orion - Fund", "slug": "orion"})
    assert [(x["primitive"], x["label"]) for x in engine.event_options(ag, 0.6, names=NAMES)] == [
        ("telegram.chat", "Telegram — Orion"), ("agent.ask", "Reply to Orion")]
    long = engine.event_options(_ev("agent", "vega", ref={"chat": "Vega - Marketing", "slug": "vega", "bot": "VegaBot"}),
                                0.6, names={"vega": "Vega Social"})
    assert long[0]["label"] == "Telegram — Vega Social"                  # « Ouvrir Telegram — Vega Social » > 22 chars
    assert long[0]["params"] == {"chat": "Vega - Marketing", "bot": "VegaBot"}
    out = _ev("cockpit", "outage", ref={"url": DASH + "/"})
    assert [(x["primitive"], x["label"]) for x in engine.event_options(out, 1, names=NAMES, route=lambda t: (None, 0))] == [
        ("url.open", "Open cockpit"), ("agent.ask", "Ask Atlas")]
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    f = home / "Desktop" / "deck.pdf"
    f.write_text("x")
    sh = home / "Desktop" / "run.command"
    sh.write_text("x")
    monkeypatch.setenv("HOME", str(home))
    fo = engine.event_options(_ev("file", "desktop", ref={"path": str(f)}), 0.5, names=NAMES)
    assert [x["primitive"] for x in fo] == ["file.open", "folder.open"]
    so = engine.event_options(_ev("file", "desktop", ref={"path": str(sh)}), 0.5, names=NAMES)
    assert [x["primitive"] for x in so] == ["folder.open"]                    # a script is never opened
    mk = engine.event_options(_ev("market", "move", ref={"slug": "orion"}), 0.5, names=NAMES)
    assert [(x["primitive"], x["params"]) for x in mk] == [("agent.ask", {"to": "orion"})]
    assert all(x["primitive"] not in ("trade", "order") for x in mk)


def test_drive_files_are_openable_other_library_paths_not(tmp_path):
    home = tmp_path / "h"
    drive = home / "Library" / "CloudStorage" / "GoogleDrive-j" / "My Drive"
    drive.mkdir(parents=True)
    (drive / "a.pdf").write_text("x")
    (home / "Library" / "Mail").mkdir(parents=True)
    (home / "Library" / "Mail" / "b.emlx").write_text("x")
    assert drill.safe_path(str(drive / "a.pdf"), home=str(home))
    assert not drill.safe_path(str(home / "Library" / "Mail" / "b.emlx"), home=str(home))
    assert not drill.safe_path(str(home / "Library" / "CloudStorage"), home=str(home))


def test_hud_option_for_event_actions():
    o = engine.event_options(_ev(cls="client", who="Sam", subject="s", ref={"message_id": "<m>"}), 0.9,
                             names=NAMES, route=lambda t: (None, 0))
    h = live.hud_option(2, o[0], {})
    assert h == {"key": 2, "label": "Reply to Sam", "icon": "sf:arrowshape.turn.up.left", "confidence": 0.9, "claude": True}
    priv = dict(o[0], private=True)
    assert "claude" not in live.hud_option(2, priv, {})


# ---- engine end to end ------------------------------------------------------------------------------------

class FakeSender:
    def __init__(self, reply=None):
        self.reply, self.sent = reply or {"event": "dismissed", "reason": "hint_ttl"}, []

    def __call__(self, payload, on_reply):
        self.sent.append(payload)
        t = threading.Thread(target=on_reply, args=(self.reply,))
        t.start()
        return t


class FakeSensor:
    def __init__(self, source, batches):
        self.source, self.batches, self.polls = source, list(batches), 0

    def poll(self, now, cfg):
        self.polls += 1
        return self.batches.pop(0) if self.batches else []


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return 0, ""


def make_live(data_dir, *, sensors_map, clock, reply=None, history=None, settings_extra=None, proactive=None):
    write_settings(data_dir, proactive or {"sources": {"mail": True, "agents": True}}, **(settings_extra or {}))
    rows = history if history is not None else history_rows(10)
    osa_calls = []

    def osa(script, args, timeout=6):
        osa_calls.append((script, args))
        return 0, "OK"
    lv = live.Live(pull=False, sender=FakeSender(reply), runner=FakeRunner(), out=io.StringIO(),
                   resolve_app=lambda a: f"/Applications/{a}.app", is_executable=lambda o: True, clock=clock)
    lv.executor.osa = osa
    lv.day = iso(clock())[:10]
    lv.rows, lv.model = list(rows) + [trow(clock() - 30, "activate:Safari")], fitted(rows)
    lv.hud_cmds = []
    lv.proactive = engine.Engine(lv, sensors=sensors_map, clock=clock, focus=lambda: None, profile=lambda: {},
                                 names=lambda: NAMES, hud_cmd=lambda c: lv.hud_cmds.append(c) or {"event": "ok"})
    return lv, osa_calls


class HoldSender:
    """oi-hud holding the hint (no reply until `answer`)."""
    def __init__(self):
        self.sent, self.pending = [], []

    def __call__(self, payload, on_reply):
        self.sent.append(payload)
        self.pending.append(on_reply)
        return threading.Thread(target=lambda: None)

    def answer(self, reply):
        self.pending.pop(0)(reply)


def _mail_ev(t, mid="<b@x>", cls="client"):
    return store.make("mail", cls, ts=t, key=mid, who="Sam", subject="NAV du fonds",
                      ref={"message_id": mid, "address": "g@dupont.example"})


def test_hint_is_withdrawn_when_its_state_ends(data_dir, monkeypatch):
    """Appear -> ring hint; the mail is read (gone row) -> the hint goes (oi-hud hide), no timer involved."""
    monkeypatch.setattr("oi.orchestrator.route", lambda text, agents=None, exclude=(): (None, 0.0))
    from oi import settings
    lv, _ = make_live(data_dir, sensors_map={}, clock=lambda: NOW)
    lv.sender = HoldSender()
    ev = _mail_ev(NOW)
    lv.proactive.q.put(ev)
    assert lv.proactive.drain(settings.load(), NOW + 2)[0]["reason"] == "hint" and len(lv.sender.sent) == 1
    assert lv.proactive.current.is_live(ev["id"])
    lv.proactive.q.put(store.gone(ev, ts=NOW + 40))
    res = lv.proactive.drain(settings.load(), NOW + 42)
    assert res == [{"reason": "state_ended", "source": "mail", "class": "client"}] and lv.hud_cmds == [{"cmd": "hide"}]
    assert not lv.proactive.current.is_live(ev["id"])
    assert any(store.token_row(store.gone(ev, ts=NOW + 40))["token"] == r["token"] for r in lv.rows)
    lv.sender.answer({"event": "dismissed", "reason": "hide_cmd"})
    logged = [r for r in normalise.read_hud_log(iso(NOW)[:10]) if r.get("kind") == "proactive"]
    assert logged[-1]["reason"] == "state_ended" and "Sam" not in json.dumps(logged)
    # a hint already answered (pressed / its display ttl): nothing to withdraw
    ev2 = _mail_ev(NOW + 50, "<c@x>")
    lv.proactive.q.put(ev2)
    lv.proactive.drain(settings.load(), NOW + 52)
    lv.sender.answer({"event": "pressed", "key": 1})
    lv.proactive.q.put(store.gone(ev2, ts=NOW + 60))
    assert lv.proactive.drain(settings.load(), NOW + 62) == [] and lv.hud_cmds == [{"cmd": "hide"}]


def test_gone_before_the_gate_is_never_hinted(data_dir):
    from oi import settings
    lv, _ = make_live(data_dir, sensors_map={}, clock=lambda: NOW)
    ev = _mail_ev(NOW)
    lv.proactive.q.put(ev)
    lv.proactive.q.put(store.gone(ev, ts=NOW + 1))                 # read within the same loop
    assert [r["reason"] for r in lv.proactive.drain(settings.load(), NOW + 2)] == ["gone"] and lv.sender.sent == []


def test_claude_events_context_is_the_live_events(data_dir, monkeypatch):
    monkeypatch.setattr("oi.orchestrator.route", lambda text, agents=None, exclude=(): (None, 0.0))
    from oi import settings
    lv, _ = make_live(data_dir, sensors_map={}, clock=lambda: NOW)
    envs = []
    orig = lv.dispatch
    monkeypatch.setattr(lv, "dispatch", lambda *a, **k: envs.append(k["env_extra"]) or orig(*a, **k))
    old = store.make("agent", "orion", ts=NOW - 5 * 86400, key="old", who="Orion", ref={"chat": "Orion", "slug": "orion"})
    read = store.make("file", "desktop", ts=NOW - 60, key="f", subject="a.pdf", ref={"path": "/x/a.pdf"})
    for e in (old, read, store.gone(read, ts=NOW - 30)):
        lv.proactive.current.apply(e)
    lv.proactive.q.put(_mail_ev(NOW))
    lv.proactive.drain(settings.load(), NOW + 2)
    lines = envs[0]["events"]
    assert len(lines) == 2 and "mail (client) from Sam" in lines[0] and "agent (orion)" in lines[1]   # 5 days old
    assert not any("a.pdf" in x for x in lines)                    # opened = out of the context


def test_held_event_is_hinted_later_while_live_and_dropped_once_gone(data_dir, monkeypatch):
    """The rate limit is a display concern: a live event waits for it; an ended one never comes back."""
    monkeypatch.setattr("oi.orchestrator.route", lambda text, agents=None, exclude=(): (None, 0.0))
    from oi import settings
    t = [NOW]
    lv, _ = make_live(data_dir, sensors_map={}, clock=lambda: t[0],
                      proactive={"sources": {"agents": True}, "min_p": 0.1})
    lv.proactive.last_hint_at = NOW - 60
    a = store.make("agent", "orion", ts=NOW, key="a", who="Orion", ref={"chat": "Orion - Fund", "slug": "orion"})
    b = store.make("agent", "nova", ts=NOW, key="b", who="Nova", ref={"chat": "Nova - CEO", "slug": "nova"})
    lv.proactive.q.put(a)
    lv.proactive.q.put(b)
    assert [r["reason"] for r in lv.proactive.drain(settings.load(), NOW + 2)] == ["rate_limited"] * 2
    lv.proactive.q.put(store.gone(b, ts=NOW + 100))                # Nova's chat read meanwhile
    assert lv.proactive.drain(settings.load(), NOW + 102) == []
    res = lv.proactive.drain(settings.load(), NOW + 541)           # the display limit is over, Orion still unread
    assert [(r["reason"], r["class"]) for r in res] == [("hint", "orion")] and len(lv.sender.sent) == 1
    assert lv.proactive.held == {}
    assert lv.proactive.drain(settings.load(), NOW + 2000) == [] and len(lv.sender.sent) == 1


def test_priorities_skip_the_rate_limit_not_focus(data_dir, monkeypatch):
    monkeypatch.setattr("oi.orchestrator.route", lambda text, agents=None, exclude=(): (None, 0.0))
    from oi import settings
    focus = [False]
    lv, _ = make_live(data_dir, sensors_map={}, clock=lambda: NOW, proactive={"sources": {"mail": True, "cockpit": True}})
    lv.proactive.focus = lambda: focus[-1]
    lv.proactive.last_hint_at = NOW - 30
    lv.proactive.q.put(_mail_ev(NOW))
    assert lv.proactive.drain(settings.load(), NOW + 2)[0]["reason"] == "hint"     # client mail 30 s after a hint
    out = store.make("cockpit", "outage", ts=NOW + 5, key="o", who="Cockpit", ref={"url": "https://c/"})
    lv.proactive.q.put(out)
    assert lv.proactive.drain(settings.load(), NOW + 8)[0]["reason"] == "hint"     # outage right after: still
    focus.append(True)
    lv.proactive.q.put(_mail_ev(NOW + 10, "<z@x>"))
    assert lv.proactive.drain(settings.load(), NOW + 12)[0]["reason"] == "focus"   # Focus is respected
    focus.append(False)
    assert [r["reason"] for r in lv.proactive.drain(settings.load(), NOW + 20)] == ["hint"]   # Focus off, still unread


def test_hud_busy_hint_waits_while_live(data_dir, monkeypatch):
    monkeypatch.setattr("oi.orchestrator.route", lambda text, agents=None, exclude=(): (None, 0.0))
    from oi import settings
    lv, _ = make_live(data_dir, sensors_map={}, clock=lambda: NOW)
    lv.sender = HoldSender()
    lv.proactive.q.put(_mail_ev(NOW))
    lv.proactive.drain(settings.load(), NOW + 2)
    lv.sender.answer({"event": "dropped", "reason": "visible"})    # the cross was up
    assert lv.proactive.last_hint_at is None                       # never drawn: not counted
    assert [r["reason"] for r in lv.proactive.drain(settings.load(), NOW + 10)] == ["hint"] and len(lv.sender.sent) == 2


def test_client_mail_opens_the_full_cross_and_the_stream_learns(data_dir, monkeypatch):
    t = [NOW]
    clock = lambda: t[0]  # noqa: E731
    mail = store.make("mail", "client", ts=NOW, key="<b@x>", who="Sam", subject="NAV du fonds",
                      ref={"message_id": "<b@x>", "address": "g@dupont.example"})
    fs = {"mail": FakeSensor("mail", [[mail]]), "agents": FakeSensor("agents", []), "files": FakeSensor("files", [])}
    lv, _ = make_live(data_dir, sensors_map=fs, clock=clock)
    monkeypatch.setattr("oi.orchestrator.route", lambda text, agents=None, exclude=(): ("orion", 1.0))
    got = lv.proactive.poll_once()
    assert got == [mail] and fs["files"].polls == 0                        # a disabled source is never polled
    assert store.read_day(iso(NOW)[:10])[0]["subject"] == "NAV du fonds"   # local event file
    t[0] = NOW + 2                                                          # oi live's next loop (2 s)
    from oi import settings
    res = lv.proactive.drain(settings.load(), t[0])
    assert res[0]["reason"] == "hint" and res[0]["priority"] is True
    payload = lv.sender.sent[0]
    # ADDENDUM 2: an important event (client mail) OPENS the cross with its actions — no dots, no ring
    assert payload["cmd"] == "show" and "mode" not in payload and "marker" not in payload
    assert res[0]["important"] == "priority" and res[0]["tier"] == "full"
    labels = [o["label"] for o in payload["options"]]
    assert labels[:3] == ["Reply to Sam", "Open the mail", "Ask Orion"]
    assert any(r["token"] == "evt:mail:client" and r["ts"] == iso(NOW) for r in lv.rows)   # in the stream at once
    lv.wait_replies(5)
    logged = normalise.read_hud_log(iso(NOW)[:10])
    row = [r for r in logged if r.get("form") == "proactive"][0]
    assert row["proactive"]["source"] == "mail" and row["proactive"]["class"] == "client"
    blob = json.dumps(logged)
    assert "Sam" not in blob and "NAV" not in blob and "<b@x>" not in blob   # no name / subject / id in logs
    # a second event 3 min later: rate limited (one per 10 min), logged with its reason only
    ag = store.make("agent", "orion", ts=NOW + 180, key=["orion", 1], who="Orion", ref={"chat": "Orion - Fund", "slug": "orion"})
    lv.proactive.q.put(ag)
    t[0] = NOW + 182
    assert lv.proactive.drain(settings.load(), t[0])[0]["reason"] == "rate_limited"
    gated = [r for r in normalise.read_hud_log(iso(NOW)[:10]) if r.get("kind") == "proactive"]
    assert gated == [{"kind": "proactive", "ts": iso(NOW + 182), "source": "agent", "class": "orion", "p": gated[0]["p"],
                      "priority": False, "important": None, "tier": "ring", "hist_n": 0, "prior": 0.5,
                      "reason": "rate_limited", "numbering": 2}]
    lv.load_counters()
    assert lv.moments_today == 1                                            # the gated event is not a moment row


def test_paused_or_hud_off_nothing(data_dir):
    t = [NOW]
    fs = {"mail": FakeSensor("mail", [[store.make("mail", "client", ts=NOW, key="k", ref={"message_id": "k"})]])}
    lv, _ = make_live(data_dir, sensors_map=fs, clock=lambda: t[0], settings_extra={"paused": True})
    assert lv.proactive.poll_once() == [] and fs["mail"].polls == 0         # paused: no sensing at all
    write_settings(data_dir, {"sources": {"mail": True}}, hud_enabled=False)
    assert len(lv.proactive.poll_once()) == 1
    from oi import settings
    assert lv.proactive.drain(settings.load(), NOW + 1)[0]["reason"] == "hud_disabled" and lv.sender.sent == []


def test_sensor_failure_is_logged_not_raised(data_dir, capsys):
    class Boom:
        def poll(self, now, cfg):
            raise RuntimeError("no automation")
    lv, _ = make_live(data_dir, sensors_map={"mail": Boom(), "agents": FakeSensor("agents", [])}, clock=lambda: NOW)
    assert lv.proactive.poll_once() == []
    lv.proactive.last_poll.clear()
    lv.proactive.poll_once(NOW + 10)
    assert capsys.readouterr().err.count("mail sensor failed") == 1        # at most once a minute


def test_pressing_a_proactive_mail_reply_finds_that_mail_by_id(data_dir):
    lv, osa_calls = make_live(data_dir, sensors_map={}, clock=lambda: NOW)
    opt = engine.event_options(_ev(cls="client", who="G", subject="s", ref={"message_id": "<b@x>"}), 0.9,
                               names=NAMES, route=lambda t: (None, 0))
    assert lv.executor.run(opt[1], {}, lv.url_for) == "mail.show"
    assert osa_calls[-1] == (claude_actions.SELECT_MAIL, ["<b@x>"])
    n = len(osa_calls)
    assert lv.executor.run(opt[0], {}, lv.url_for) == "mail.reply"
    assert osa_calls[n:] == [(claude_actions.OPEN_DRAFT, ["reply", "<b@x>", "", "window"])]   # no SELECT_MAIL
    for script, _ in osa_calls:
        assert "send" not in script.replace("sender", "").lower()          # never a send


def test_private_event_never_reaches_claude(data_dir):
    def runner(prompt, model):
        raise AssertionError("Claude called for a local-only event")
    sent = []
    ca = claude_actions.ClaudeActions(runner=runner, sender=lambda p: sent.append(p) or {"event": "ok"},
                                      settings_loader=lambda: dict(claude_actions.CLAUDE_DEFAULTS),
                                      mail_reader=lambda **kw: {"message_id": "<b@x>", "sender_address": "a@b.example"})
    opt = dict(engine.event_options(_ev(cls="client", ref={"message_id": "<b@x>"}), 0.9, names=NAMES,
                                    route=lambda t: (None, 0))[0], private=True)
    tok, meta = ca.press(opt, {}, 2, lambda: "mail.reply")
    assert tok == "mail.reply" and meta is None and sent[0]["cmd"] == "result"


def test_glide_uses_the_events_mail_not_the_selection():
    eng = glide.GlideEngine(lambda p: {"event": "ok"}, runner=lambda *a, **k: {},
                            mail_reader=lambda: (_ for _ in ()).throw(AssertionError("selection read")))
    eng.settings_loader = lambda: dict(claude_actions.CLAUDE_DEFAULTS)
    ev = _ev(cls="client", who="Sam", subject="NAV", ref={"message_id": "<b@x>", "address": "g@d.example"})
    s = eng.session({1: {"primitive": "mail.reply"}}, {"app": "Safari"}, {"agents": {}})
    s.mail_fixed = engine.mail_of(ev)
    _, env = s.read_context()
    assert env["mail"]["message_id"] == "<b@x>" and env["mail_gated"] is False and s.mail_id == "<b@x>"
    s2 = eng.session({1: {"primitive": "mail.reply"}}, {"app": "Safari"}, {"agents": {}})
    s2.mail_fixed = engine.mail_of(dict(ev, private=True))
    assert s2.read_context()[1]["mail_gated"] is True


def test_pack_gets_the_event_habit_and_recent_events():
    cfg = {"local_only_domains": ["acme-conseil.example"]}
    p = pack.Packer(wiki=lambda: None, habits=pack.Habits(today=lambda: datetime(2026, 1, 1).date()),
                    agents=lambda: [], armed=lambda: None, log=lambda m: None)
    lines = ["16:00 mail (client) from Sam: NAV du fonds", "16:01 mail (other): AcmeConseil SAS deal",
             "16:02 agent (orion): details withheld"]
    out = p.build({"app": "Mail", "events": lines, "event_habit": "After a mail event (client), the user usually: Mail 80%"}, cfg)
    assert "## After event\nAfter a mail event (client)" in out["text"]
    assert "Sam: NAV du fonds" in out["text"] and "details withheld" in out["text"]
    assert "AcmeConseil" not in out["text"]                                       # the W25 leaks gate


def test_event_lines_withhold_private_details():
    tz = relevance.default_tz()
    ev = _ev(cls="client", who="Sam", subject="NAV")
    assert engine.event_line(ev, tz) == "16:00 mail (client) from Sam: NAV"
    assert engine.event_line(dict(ev, private=True), tz) == "16:00 mail (client): details withheld"


def test_one_hint_within_ten_seconds_of_a_new_mail(data_dir):
    """The evaluation bar: sensor poll (mail poll_s 6) + oi live's loop (2 s) — measured on the fake clock."""
    t = [NOW]
    pending = []
    fs = {"mail": FakeSensor("mail", [[], []])}
    lv, _ = make_live(data_dir, sensors_map=fs, clock=lambda: t[0], proactive={"sources": {"mail": True}})
    from oi import settings
    arrived = NOW + 0.5
    hint_at = None
    while t[0] < NOW + 20 and hint_at is None:
        if t[0] >= arrived and not pending:
            pending.append(store.make("mail", "client", ts=t[0], key="<n@x>", who="G", ref={"message_id": "<n@x>"}))
            fs["mail"].batches = [pending]
        lv.proactive.poll_once(t[0])
        if int(t[0] - NOW) % 2 == 0:                                                 # oi live's 2 s loop
            if any(r["reason"] == "hint" for r in lv.proactive.drain(settings.load(), t[0])):
                hint_at = t[0]
        t[0] += 1
    assert hint_at is not None and hint_at - arrived <= 10


def test_probe_prints_tokens_only(data_dir, monkeypatch, capsys):
    class S:
        def __init__(self):
            self.n = 0

        def poll(self, now, cfg):
            self.n += 1
            return [] if self.n == 1 else [store.make("mail", "client", ts=now, key="k", who="Sam", subject="NAV",
                                                      ref={"message_id": "k"})]
    monkeypatch.setattr(sensors, "default_sensors", lambda live=None: {"mail": S()})
    from oi import cli
    assert cli.main(["proactive-probe", "--source", "mail"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"mail": ["evt:mail:client"]} and "Sam" not in out
    assert not (data_dir / "events").exists()


# ---- ADDENDUM 2: important -> the full cross; the rest -> a ring ------------------------------

def _other_mail(t, mid="<o@x>", who="Newsletter", address="news@shop.example", subject="Promo"):
    return store.make("mail", "other", ts=t, key=mid, who=who, subject=subject,
                      ref={"message_id": mid, "address": address})


def test_importance_tier_from_local_context():
    cfg = _cfg({})
    prof = {"attention": {"important": [{"who": "Marc", "class": "sender", "weight": 0.9},
                                        {"who": "levée de fonds", "class": "topic", "weight": 0.9}]}}
    imp = relevance.importance
    assert imp(_mail_ev(NOW), cfg, {}) == "priority"                             # mail:client is a hard priority
    no_prio = dict(cfg, priorities=[])
    assert imp(_mail_ev(NOW), no_prio, {}) is None                               # the class alone is not a verdict
    judged = dict(_other_mail(NOW), wiki={"important": True, "reason": "wiki", "page": "shared/people/x.md"})
    assert imp(judged, no_prio, {}) == "wiki"                                    # judge.py: the wiki said so
    assert imp(_other_mail(NOW, who="Marc Durand"), cfg, prof) == "profile"    # W22 counterpart
    assert imp(_other_mail(NOW, subject="Point levée de fonds"), cfg, prof) == "profile"
    priv = dict(_other_mail(NOW, subject="Point levée de fonds"), private=True)
    assert imp(priv, cfg, prof) is None                                          # a private subject is never read
    assert imp(_other_mail(NOW), cfg, prof) is None


def test_important_skips_threshold_and_rate_limit_but_not_quiet():
    cfg = dict(_cfg({}), priorities=[], min_p=0.99)
    prof = {"attention": {"important": [{"who": "Marc", "class": "sender", "weight": 0.9}]}}
    ev = _other_mail(NOW, who="Marc Durand")
    g = relevance.gate(ev, cfg, now=NOW, last_hint_at=NOW - 30, profile=prof, focus=False)
    assert g["ok"] and g["tier"] == "full" and g["important"] == "profile"
    assert relevance.gate(ev, cfg, now=NOW, last_hint_at=None, profile=prof, focus=True)["reason"] == "focus"
    plain = relevance.gate(_other_mail(NOW), cfg, now=NOW, last_hint_at=None, profile=prof)
    assert plain["reason"] == "below_threshold" and plain["tier"] == "ring"


def test_ordinary_mail_is_a_ring_important_one_opens_the_cross_and_read_withdraws(data_dir, monkeypatch):
    monkeypatch.setattr("oi.orchestrator.route", lambda text, agents=None, exclude=(): (None, 0.0))
    from oi import settings
    lv, _ = make_live(data_dir, sensors_map={}, clock=lambda: NOW, proactive={"sources": {"mail": True}, "min_p": 0.1})
    lv.proactive.profile = lambda: {"attention": {"important": [{"who": "Marc", "class": "sender", "weight": 0.9}]}}
    lv.sender = HoldSender()
    plain = _other_mail(NOW)
    lv.proactive.q.put(plain)
    r = lv.proactive.drain(settings.load(), NOW + 2)[0]
    assert r["reason"] == "hint" and r["tier"] == "ring"
    sent = lv.sender.sent[-1]
    assert sent["mode"] == "hint" and sent["marker"] == "ring"                    # ordinary: the subtle ring
    lv.proactive.q.put(store.gone(plain, ts=NOW + 20))                            # read -> the ring goes
    assert lv.proactive.drain(settings.load(), NOW + 22)[0]["reason"] == "state_ended"
    lv.sender.answer({"event": "dismissed", "reason": "hide_cmd"})
    imp = _other_mail(NOW + 30, "<i@x>", who="Marc Durand", address="b@durand.example", subject="Suivi")
    lv.proactive.q.put(imp)                                                       # 30 s later: no rate limit for it
    r = lv.proactive.drain(settings.load(), NOW + 32)[0]
    assert r["reason"] == "hint" and r["tier"] == "full" and r["important"] == "profile"
    sent = lv.sender.sent[-1]
    assert "mode" not in sent and "marker" not in sent                            # the cross opens, no pointer move
    assert [o["label"] for o in sent["options"]][:2] == ["Reply to Marc Durand", "Open the mail"]
    lv.proactive.q.put(store.gone(imp, ts=NOW + 40))                              # read -> the cross is withdrawn
    assert lv.proactive.drain(settings.load(), NOW + 42)[0]["reason"] == "state_ended"
    assert lv.hud_cmds == [{"cmd": "hide"}, {"cmd": "hide"}]
    lv.sender.answer({"event": "dismissed", "reason": "hide_cmd"})
    logged = [x for x in normalise.read_hud_log(iso(NOW)[:10]) if x.get("form") == "proactive"]
    assert [x["show_mode"] for x in logged] == ["hint", "full"] and "Marc" not in json.dumps(logged)


def test_send_capable_actions_are_marked_for_the_outward_gate():
    ev = _mail_ev(NOW)
    opts = engine.outward_gate(engine.event_options(ev, 0.8, names=NAMES, route=lambda s: ("orion", 1.0)))
    by = {o["primitive"]: o.get("outward") for o in opts}
    assert by["mail.reply"] is True and by["agent.ask"] is True and by["mail.show"] is None


def test_metric_below_and_market_move_at_start_are_live(tmp_path):
    """A KPI already below its threshold / a symbol already beyond ±move_pct when `oi live` starts is live state."""
    mf, kf = tmp_path / "m.json", tmp_path / "k.json"
    mf.write_text(json.dumps({"metrics": [{"id": "aum", "label": "AUM", "value": 4, "threshold": 5}]}))
    kf.write_text(json.dumps({"brief": {"date": "2026-10-21", "read": True}, "moves": [{"symbol": "CW8", "pct": 4}]}))
    cfg = _cfg({"proactive": {"metrics": {"file": str(mf)}, "markets": {"file": str(kf)}}})
    ms, ks = sensors.MetricsSensor(), sensors.MarketsSensor()
    assert [store.token(e) for e in ms.poll(NOW, cfg)] == ["evt:metric:aum"]
    assert [store.token(e) for e in ks.poll(NOW, cfg)] == ["evt:market:move"]
    assert ms.poll(NOW + 300, cfg) == [] and ks.poll(NOW + 300, cfg) == []    # still on: the same state, no refresh
