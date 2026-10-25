"""an APP direction's next level lists that app's LIVE items as one-click actions, in judge
order, each with its own level-2/3 card; no model call for the list. Fakes only (no Telegram, Mail or Claude)."""
from __future__ import annotations

from oi import glide, live
from test_glide import CHATS, Rig

TG = {1: {"key": "open_app", "primitive": "app.open", "params": {"app": "Telegram"}, "token": "activate:Telegram"}}
MAILAPP = {1: {"key": "open_app", "primitive": "app.open", "params": {"app": "Mail"}, "token": "activate:Mail"}}
SAFARI = {1: {"key": "open_app", "primitive": "app.open", "params": {"app": "Safari"}, "token": "activate:Safari"}}
CTX = {"app": "Finder", "space": 1}


def agent_ev(slug, who, n, ts, *, bot=None, wiki=None):
    ref = {"slug": slug, "chat": f"{who} - bot", "n": n, "since": ts, **({"bot": bot} if bot else {})}
    ev = {"ts": ts, "id": f"agent:{slug}", "source": "agent", "class": "waiting", "who": who, "subject": None,
          "ref": ref, "private": False, "state": "live"}
    return dict(ev, wiki=wiki) if wiki else ev


def mail_ev(mid, who, subject, ts, *, wiki=None, private=False):
    ev = {"ts": ts, "id": f"mail:{mid}", "source": "mail", "class": "client" if wiki else "other", "who": who,
          "subject": None if private else subject, "ref": {"message_id": mid, "address": "x@y.example"},
          "private": private, "state": "live"}
    return dict(ev, wiki=wiki) if wiki else ev


ATLAS = agent_ev("atlas", "Atlas", 8, "2026-10-21T12:05:00Z", bot="AtlasAgentBot",
                wiki={"page": "atlas_rnd/hot.md", "snippet": "R&D builds"})
JUNO = agent_ev("juno", "Juno", 3, "2026-10-21T14:00:00Z")          # newer, but not judged important


def test_telegram_direction_lists_waiting_agents_in_judge_order_one_click_with_cards():
    r = Rig()
    s = r.session(keymap=dict(TG), ctx=dict(CTX), live_events=[JUNO, ATLAS], bots={"juno": "JunoBot"})
    p = s.expand([1])
    opts = p["options"]
    assert [o["label"] for o in opts[:2]] == ["Atlas · 8", "Juno · 3"]    # the judge's important one first
    assert [o["label"] for o in opts[2:]] == CHATS[:2]                   # today's chats below them (4 at most)
    assert r.prompts == []                                                # no model call for the list
    assert opts[0]["card"] == {"key": 1, "what": "Atlas t'attend · 8 messages · depuis 14:05",
                               "why": {"page": "atlas_rnd/hot.md", "snippet": "R&D builds"}}
    assert opts[1]["card"]["key"] == 2 and "why" not in opts[1]["card"]
    ran = []
    s.execute([1, 2], run_primitive=lambda n: (ran.append(n), n["token"])[1], press=None, send_vocal=None)
    assert ran[0]["params"] == {"chat": "Juno - bot", "bot": "JunoBot"}   # the bot from settings proactive.agents.bots
    runs = []
    ex = live.Executor(lambda argv: (runs.append(argv), (0, ""))[1])
    assert ex.run(s.node([1, 1]), {}, lambda u: u) and runs == [["open", "tg://resolve?domain=AtlasAgentBot"]]
    assert s.expand([1, 1])["options"] == []                              # a chat is a leaf


def test_mail_direction_lists_important_unread_mails_then_claude_follow_ups():
    r = Rig()
    acme = mail_ev("<a@acme>", "Sam", "Devis formation", "2026-10-21T09:00:00Z",
                   wiki={"page": "clients/acme.md", "snippet": "client"})
    news = mail_ev("<n@news>", "Newsletter", "Weekly", "2026-10-21T11:00:00Z")
    priv = mail_ev("<p@bank>", "Banque", "secret", "2026-10-21T10:00:00Z", private=True)
    s = r.session(keymap=dict(MAILAPP), ctx=dict(CTX), live_events=[news, acme, priv, JUNO])
    opts = s.expand([1])["options"]
    assert [o["label"] for o in opts[:3]] == ["Sam · Devis formation", "Newsletter · Weekly", "Banque"]
    assert all(o["action"] == "mail.show" for o in opts[:3])
    assert opts[2]["card"] == {"key": 3, "what": "Banque · depuis 12:00"}   # private: no subject, no why
    assert opts[0]["card"]["why"]["page"] == "clients/acme.md"
    assert s.node([1, 1])["params"] == {"message_id": "<a@acme>"}
    assert len(r.prompts) == 1 and len(opts) == 4                        # Claude's follow-ups stay below them
    reply = s.expand([1, 1])["options"]
    assert [o["action"] for o in reply] == ["mail.reply"] and s.node([1, 1, 1])["params"] == {"message_id": "<a@acme>"}


def test_no_live_items_is_todays_behaviour():
    r = Rig()
    assert [o["label"] for o in r.session(keymap=dict(TG), ctx=dict(CTX)).expand([1])["options"]] == CHATS
    r2 = Rig()
    got = r2.session(keymap=dict(SAFARI), ctx=dict(CTX), live_events=[ATLAS]).expand([1])["options"]
    assert not any(o.get("card") for o in got) and not any(o["label"].startswith("Atlas") for o in got)
    assert glide.live_items("Calendar", {"live_events": [ATLAS]}) == []    # no calendar connector yet: nothing


def test_a_new_live_state_is_not_served_from_the_cache():
    r = Rig()
    assert [o["label"] for o in r.session(keymap=dict(TG), ctx=dict(CTX), live_events=[ATLAS]).expand([1])["options"]][:1] == ["Atlas · 8"]
    more = dict(ATLAS, ref=dict(ATLAS["ref"], n=9))
    got = r.session(keymap=dict(TG), ctx=dict(CTX), live_events=[more, JUNO]).expand([1])["options"]
    assert [o["label"] for o in got[:2]] == ["Atlas · 9", "Juno · 3"]


def test_glide_env_carries_the_live_events(data_dir):
    lv = live.Live(pull=False, sender=lambda *a: None, resolve_app=lambda a: f"/Applications/{a}.app")
    lv.proactive.current.apply(ATLAS)
    e = lv.glide_env({"app": "Safari"})
    assert [x["id"] for x in e["live_events"]] == ["agent:atlas"] and isinstance(e["bots"], dict)
