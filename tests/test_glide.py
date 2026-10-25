"""W15: hold ⌥⌘ + glide + dwell = Claude sub-rings. Synthetic context only; Claude is a fake runner."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

from oi import claude_actions as ca, drill, glide, live, normalise

MAIL = {"sender_name": "Sam Martin", "sender_address": "sam@acme-conseil.example", "subject": "Re: devis formation",
        "date": "2026-01-05T09:00:00", "message_id": "<m1@acme>", "cc": ["nova@team.example"], "reply_to": "",
        "body": "Bonjour Alex, pouvez-vous nous envoyer le devis ?", "previous": None}
AGENTS = {"atlas": "Atlas", "nova": "Nova", "orion": "Orion"}
TOPS = {1: {"key": "open_app", "primitive": "app.open", "params": {"app": "Telegram"}, "token": "activate:Telegram"},
        2: {"key": "reply_sender", "primitive": "mail.reply", "params": {}, "token": None},
        3: {"key": "open_url", "primitive": "url.open", "params": {"url": "notion.so/prr"}, "token": "url:notion.so/prr"},
        4: {"key": "space", "primitive": "space.move", "params": {}, "token": "space:+1"}}
ANSWER = {"followups": {
    "1": [{"label": "Send last copy", "action": "vocal:nova:Here is the last copy"}, {"label": "Ask status", "action": "vocal:nova:Status?"}],
    "2": [{"label": "Reply to Sam", "action": "draft:mail.reply"}, {"label": "Tell Nova", "action": "vocal:nova:Here is the devis"},
          {"label": "Archive", "action": "mail.archive"}, {"label": "Archive", "action": "ask:Archive this mail"}],
    "3": [{"label": "PRR page", "action": "url.open:notion.so"}, {"label": "Summary", "action": "summarise"},
          {"label": "Telegram", "action": "app.open:Telegram"}],
    "4": [{"label": "Back", "action": "space.move:-1"}]}}
CHATS = ["Pat Lee", "Team Ops", "Maman"]


def env(**kw) -> dict:
    base = {"apps": ["Telegram", "Safari", "Mail"], "urls": {"notion.so": "notion.so/prr", "github.com": "github.com/x"},
            "agents": dict(AGENTS), "resolve_app": lambda a: f"/Applications/{a}.app" if a in ("Telegram", "Safari", "Mail") else None,
            "host": "notion.so", "mail": dict(MAIL), "claude_ok": True}
    base.update(kw)
    return base


class Rig:
    def __init__(self, answer=ANSWER, *, mail=MAIL, settings=None, fail=None, delay=0.0):
        self.prompts, self.sent, self.mail_reads, self.logged, self.wiki_q = [], [], 0, [], []
        self.answer, self.fail, self.delay = answer, fail, delay
        self.mails = [mail] if not isinstance(mail, list) else mail

        def runner(prompt, model):
            self.prompts.append((prompt, model))
            if self.delay:
                time.sleep(self.delay)
            if self.fail:
                raise ca.ClaudeUnavailable(self.fail)
            return self.answer

        def mail_reader():
            m = self.mails[min(self.mail_reads, len(self.mails) - 1)]
            self.mail_reads += 1
            return dict(m) if m else None

        self.providers = drill.Providers(mdfind=lambda: "", telegram_titles=lambda: list(CHATS),
                                         music_playlists=lambda: [], music_tracks=lambda p: [], ok=lambda p: False)
        self.engine = glide.GlideEngine(lambda p: (self.sent.append(p), {"event": "ok"})[1], runner=runner,
                                        mail_reader=mail_reader,
                                        settings_loader=lambda: dict(ca.CLAUDE_DEFAULTS, **(settings or {})),
                                        providers=self.providers, pairs_reader=lambda: [],
                                        pair_logger=lambda *a: self.logged.append(a),
                                        wiki=lambda q: (self.wiki_q.append(q), ["Nova: CEO notes"])[1])

    def session(self, keymap=None, ctx=None, **envkw):
        e = env(**envkw)
        e.pop("mail"); e.pop("claude_ok")
        return self.engine.session(keymap or dict(TOPS), ctx or {"app": "Mail", "url": "notion.so/prr", "space": 2}, e)


# ---- prefetch batching + cache ---------------------------------------------------------------------------

def test_prefetch_is_one_batched_haiku_call_and_the_cache_serves_expand():
    r = Rig()
    s = r.session()
    s.prefetch()
    assert len(r.prompts) == 1 and r.prompts[0][1] == "sonnet"
    prompt = r.prompts[0][0]
    # W23: only the generative scopes go to Claude (the mail); Telegram, the site and the desk move are local
    assert "2. Path: reply to the selected mail" in prompt
    assert "1. Path" not in prompt and "3. Path" not in prompt and "4. Path" not in prompt
    assert "Sam Martin" in prompt and "draft:mail.reply" in prompt and "nova (Nova)" in prompt
    t0 = time.monotonic()
    payload = s.expand([2])
    assert (time.monotonic() - t0) < 0.2 and len(r.prompts) == 1          # from the cache: no second call
    assert payload["cmd"] == "subring" and payload["path"] == [2]
    assert [o["label"] for o in payload["options"]] == ["Reply to Sam", "Tell Nova", "Ask Atlas: Archive", "Forward"]
    assert payload["options"][0]["claude"] is True and "claude" not in payload["options"][1]
    assert not any("confirm" in o for o in payload["options"])     # a send: the outward gate confirms, no "Send ✓"
    assert [p for p in r.sent if p["cmd"] == "subring"] == [payload]
    assert r.sent[0]["cmd"] == "context"                                 # the cards come first
    assert s.expansions[0]["path"] == "*" and s.expansions[-1] == {"path": "2", "ms": s.expansions[-1]["ms"],
                                                                   "cache": True, "ok": True, "n": 4, "claude": False}
    assert [o["label"] for o in s.expand([1])["options"]] == CHATS        # Telegram -> its chats, no call
    assert [o["label"] for o in s.expand([4])["options"]] == ["Back"] and len(r.prompts) == 1
    assert [o["label"] for o in s.expand([3])["options"]] == ["Summary"] and len(r.prompts) == 1
    # a second show in the same context: everything cached, no call at all
    s2 = r.session()
    s2.prefetch()
    s2.expand([3])
    assert len(r.prompts) == 1


def test_expand_waits_for_the_running_prefetch_instead_of_calling_again():
    r = Rig(delay=0.3)
    s = r.session()
    t = threading.Thread(target=s.prefetch)
    t.start()
    time.sleep(0.05)
    payload = s.expand([2])
    t.join()
    assert len(r.prompts) == 1
    assert [o["action"] for o in payload["options"]] == ["draft:mail.reply", "vocal:nova", "ask", "mail.forward"]
    assert payload["options"][1]["icon"] == "initial:N"


def test_cache_miss_calls_for_that_option_and_deeper_paths_carry_the_chain():
    r = Rig(answer={"followups": {"1": [{"label": "Kanban", "action": "url.open:github.com"},          # another site
                                        {"label": "Tell Nova", "action": "vocal:nova:Can you look at the devis?"},
                                        {"label": "Open Claude", "action": "app.open:Claude"},
                                        {"label": "Forward", "action": "i1"}]}})
    s = r.session(pages={"notion.so": ["notion.so/prr", "notion.so/roadmap"]})
    p3 = s.expand([3])                                                   # a site: its pages + Summary, no call
    assert [o["label"] for o in p3["options"]] == ["roadmap", "Summary"] and r.prompts == []   # never the page itself
    p1 = s.expand([2])
    assert len(r.prompts) == 1 and "2. Path" not in r.prompts[0][0] and "1. Path: reply to the selected mail" in r.prompts[0][0]
    assert 'i1 = mail "Forward"' in r.prompts[0][0] and 'mail "Reply"' not in r.prompts[0][0]   # not Reply under Reply
    assert [o["label"] for o in p1["options"]] == ["Tell Nova", "Forward"]            # out of scope dropped
    p2 = s.expand([2, 1])
    assert len(r.prompts) == 2 and "1. Path: reply to the selected mail > Tell Nova" in r.prompts[1][0]
    assert p2["path"] == [2, 1] and all(o["label"] != "Send ✓" for o in p2["options"])
    s.expand([2, 1])                                                     # cached per path
    assert len(r.prompts) == 2
    assert s.expand([3, 1, 1, 1])["options"] == []                       # deeper than 3: a leaf, no call
    assert s.expand([9])["options"] == []                                # no such option


def test_claude_off_or_unavailable_gives_a_leaf():
    r = Rig(settings={"enabled": False})
    s = r.session()
    s.prefetch()
    assert s.expand([2])["options"] == [] and r.prompts == [] and r.mail_reads == 0
    assert [o["label"] for o in s.expand([1])["options"]] == CHATS        # W23: local providers need no Claude
    r = Rig(fail="timeout")
    s = r.session()
    s.prefetch()
    p = s.expand([2])
    assert [o["label"] for o in p["options"]] == ["Forward"] and s.expansions[-1]["ok"] is False
    assert len(r.prompts) == 2                                           # the prefetch failed: not cached, expand retried


# ---- follow-up -> executable primitive --------------------------------------------------------------------

def test_followups_map_to_executable_primitives_or_ask_atlas():
    e = env()
    top_url, top_space = TOPS[3], TOPS[4]

    def r(label, action, parent=top_url, **kw):
        return glide.resolve({"label": label, "action": action}, parent, dict(e, **kw))
    n = r("Telegram", "app.open:telegram")
    assert (n["kind"], n["primitive"], n["params"], n["token"], n["icon"]) == \
        ("primitive", "app.open", {"app": "Telegram"}, "activate:Telegram", "app:/Applications/Telegram.app")
    assert r("Figma", "app.open:Figma")["action"] == "ask"               # not installed
    n = r("PRR", "url.open:https://www.notion.so/")
    assert (n["primitive"], n["params"], n["token"]) == ("url.open", {"url": "notion.so/prr"}, "url:notion.so/prr")
    assert r("Evil", "url.open:evil.example")["label"] == "Ask Atlas: Evil"   # never a site the model invented
    assert r("Back", "space.move:-1")["token"] == "space:-1" and r("x", "space.move:+99")["action"] == "ask"
    assert r("Forward", "mail.forward")["primitive"] == "mail.forward"
    assert r("Forward", "mail.forward", mail=None)["action"] == "ask"      # no mail selected
    n = r("Reply to G", "draft:mail.reply")
    assert n["kind"] == "draft" and n["claude"] is True and n["primitive"] == "mail.reply"
    assert r("Reply", "draft:mail.reply", claude_ok=False)["action"] == "ask"
    n = r("Summary", "summarise")
    assert n["kind"] == "summary" and n["primitive"] == "url.open" and n["params"] == {"url": "notion.so/prr"}
    assert r("Summary", "summarise", parent=top_space)["action"] == "ask"
    n = r("Ask status", "vocal:Nova:Status of the PRR?")
    assert n["kind"] == "vocal" and n["params"] == {"to": "nova", "text": "Status of the PRR?"} and n["action"] == "vocal:nova"
    n = r("Ping", "vocal:nobody:hi")
    assert n["action"] == "ask" and n["params"]["to"] == "atlas"
    n = r("Plan the demo", "ask:Plan the client demo for Friday")
    assert n["label"] == "Ask Atlas: Plan the demo" and n["params"]["text"] == "Plan the client demo for Friday"
    assert r("Do a thing", "launch.rocket")["action"] == "ask"
    assert glide.resolve("nonsense", top_url, e) is None
    assert r("x", "ask:hi", agents={}) is None                            # no atlas recipient: nothing
    many = [{"label": f"L{i}", "action": "space.move:+1"} for i in range(6)] + [{"label": "L0", "action": "space.move:+1"}]
    assert len(glide.resolve_list(many, top_space, e)) == 4
    assert glide.resolve_list(None, top_space, e) == []


# ---- privacy + request-time context -----------------------------------------------------------------------

def test_mail_local_only_covers_sender_cc_and_reply_to():
    cfg = {"local_only_domains": ["client.example"]}
    assert ca.mail_local_only(dict(MAIL, sender_address="a@client.example"), cfg)
    assert ca.mail_local_only(dict(MAIL, cc=["x@team.example", "boss@sub.client.example"]), cfg)
    assert ca.mail_local_only(dict(MAIL, reply_to="Desk <desk@client.example>"), cfg)
    assert not ca.mail_local_only(MAIL, cfg) and not ca.mail_local_only(None, cfg)


def test_privacy_gate_on_cc_withholds_the_mail_from_the_prompt():
    r = Rig(mail=dict(MAIL, cc=["boss@client.example"]), settings={"local_only_domains": ["client.example", "notion.so"]})
    s = r.session()
    s.prefetch()
    prompt = r.prompts[0][0]
    context = prompt.split("<context>\n")[1].split("</context>")[0]
    assert "details withheld" in context and "Sam" not in context and "devis" not in context
    assert "acme" not in prompt and "boss@" not in prompt
    assert "notion.so" not in prompt                                     # a local-only site never goes to Claude


def test_w14_press_and_labels_use_the_cc_gate():
    sent, calls = [], []
    acts = ca.ClaudeActions(Path("/nonexistent.sock"), runner=lambda p, m: calls.append(m) or {"body": "x"},
                            sender=lambda p: (sent.append(p), {"event": "ok"})[1], osa=lambda s, a, timeout=6: (0, "OK"),
                            wiki=lambda q: [], mail_reader=lambda **kw: dict(MAIL, reply_to="r@client.example"),
                            settings_loader=lambda: dict(ca.CLAUDE_DEFAULTS, local_only_domains=["client.example"]))
    executed, meta = acts.press({"primitive": "mail.reply", "params": {}}, {}, 1, lambda: "plain")
    assert executed == "plain" and calls == [] and "Local-only" in sent[0]["text"]


def test_mail_selection_is_read_at_request_time():
    other = dict(MAIL, message_id="<m2@acme>", sender_name="Dana Dubois", subject="Facture")
    r = Rig(mail=[MAIL, other])
    s = r.session()
    s.prefetch()                                       # hold start: <m1>
    assert s.mail_id == "<m1@acme>" and "Sam" in r.prompts[0][0]
    s.expand([2])                                      # The user selected another mail before resting on Reply
    assert r.mail_reads == 2 and s.mail_id == "<m2@acme>"
    assert len(r.prompts) == 2 and "Dana Dubois" in r.prompts[1][0]   # a new context: not the <m1> cache


def test_press_refuses_when_the_selection_changed_since_the_followup():
    sent = []
    acts = ca.ClaudeActions(Path("/nonexistent.sock"), runner=lambda p, m: {"body": "x"},
                            sender=lambda p: (sent.append(p), {"event": "ok"})[1], osa=lambda s, a, timeout=6: (0, "OK"),
                            wiki=lambda q: [], mail_reader=lambda **kw: dict(MAIL, message_id="<other>"),
                            settings_loader=lambda: dict(ca.CLAUDE_DEFAULTS))
    executed, _ = acts.press({"primitive": "mail.reply", "params": {}}, {}, 2, lambda: "plain", expect_mid="<m1@acme>")
    assert executed is None and "changed" in sent[0]["text"]
    calls = []
    assert ca.plain_mail("mail.forward", lambda s, a, timeout=6: (calls.append(a), (0, "OK"))[1], "<m1@acme>")
    assert calls == [["forward", "<m1@acme>", "", "window"]]


# ---- execution -----------------------------------------------------------------------------------------------

def test_execute_routes_each_kind():
    r = Rig()
    s = r.session()
    s.prefetch()
    s.expand([1]); s.expand([2]); s.expand([3]); s.expand([4])
    ran, pressed, vocals = [], [], []
    kw = {"run_primitive": lambda n: (ran.append(n["primitive"]), n["token"] or n["primitive"])[1],
          "press": lambda n, mid: (pressed.append((n["kind"], n["primitive"], mid)), ("draft-done", {"ok": True}))[1],
          "send_vocal": lambda to, text: (vocals.append((to, text)), {"ok": True})[1]}
    assert s.execute([2, 4], **kw) == ("mail.forward", "mail.forward", None)
    assert s.execute([2, 1], **kw) == ("draft-done", "draft:mail.reply", {"ok": True})
    assert pressed[-1] == ("draft", "mail.reply", "<m1@acme>")
    assert s.execute([3, 1], **kw)[1] == "summarise" and pressed[-1] == ("summary", "url.open", None)
    tok, action, _ = s.execute([1, 1], **kw)
    assert action == "telegram.chat" and tok.startswith("telegram.chat#") and "eric" not in tok.lower()
    # a send runs on release (oi.vocal.send then asks the outward gate's panel: the one confirmation)
    assert s.execute([2, 2], **kw) == ("vocal:nova", "vocal:nova", None) and vocals == [("nova", "Here is the devis")]
    p = s.expand([2, 2])
    assert [o["label"] for o in p["options"]] == ["Send last copy", "Ask status"] and r.wiki_q          # depth 2 under an agent: wiki
    assert "Nova: CEO notes" in r.prompts[-1][0] and "1. Path: reply to the selected mail > Tell Nova" in r.prompts[-1][0]
    assert s.execute([2, 2, 1], **kw) == ("vocal:nova", "vocal:nova", None) and vocals[-1] == ("nova", "Here is the last copy")
    assert s.execute([2, 3], **kw) == ("vocal:atlas", "ask", None) and vocals[-1][0] == "atlas"     # ask: on release
    assert s.execute([4, 1], **kw)[0] == "space:-1"
    assert s.execute([4, 4], **kw) == (None, None, None)
    assert r.logged[-1][0] == "space:+1" and r.logged[-1][2] == "space:-1"            # chosen branches -> pairs
    assert ("reply_sender" not in str(r.logged)) and all("Nova" not in str(x) for x in r.logged)


# ---- oi live: wiring, logging, tokens --------------------------------------------------------------------------

class EventSender:
    """Plays the HUD: on show, the glide start and an expand (on_event), then the release (on_reply)."""

    def __init__(self, reply, expand=(2,)):
        self.reply, self.sent = reply, []
        self.expand = [list(p) for p in expand] if expand and isinstance(expand[0], (list, tuple)) else [list(expand)]

    def __call__(self, payload, on_reply, on_event=None):
        self.sent.append(payload)

        def work():
            on_event({"event": "glide", "phase": "start"})
            for p in self.expand:
                on_event({"event": "expand", "path": p})
            on_reply(self.reply)
        t = threading.Thread(target=work)
        t.start()
        return t


def _live(sender, rig, **kw):
    runs = []

    def runner(argv):
        runs.append(argv)
        return 0, ""
    lv = live.Live(pull=False, sender=sender, runner=runner, resolve_app=lambda a: f"/Applications/{a}.app",
                   glide_engine=rig.engine, out=open(os.devnull, "w"), **kw)
    lv.rows = [{"ts": "2026-01-05T08:59:00Z", "token": "activate:Telegram"},
               {"ts": "2026-01-05T08:59:30Z", "token": "url:notion.so/prr"}]
    return lv, runs


def test_live_glide_prefetch_expand_execute_and_log(data_dir):
    rig = Rig(answer={"followups": {"3": [{"label": "Ask Nova", "action": "vocal:nova:secret plan"}]}})
    glog = {"visited": [{"path": [3], "ms": 640}, {"path": [3, 1], "ms": 210}], "expanded": 1, "final": [3, 1], "depth": 1}
    sender = EventSender({"event": "pressed", "key": 3, "via": "glide", "path": [3, 1], "glide": glog}, expand=(3,))
    lv, runs = _live(sender, rig)
    shown = [dict(TOPS[4], p_markov=0.4), dict(TOPS[1], p_markov=0.3)]
    row = {"ts": "2026-01-05T09:00:00.000Z", "form": "A", "options": [], "shown": False, "outcome": "x",
           "executed": None, "latency_ms": None, "summoned": True}
    lv.dispatch(shown, row, {"app": "Safari", "url": "notion.so/prr", "space": 2}, {"ttl_ms": 15000}, time.time())
    lv.wait_replies(5)
    assert rig.prompts == []                                  # W23: a desk move and Telegram are local scopes
    sub = [p for p in rig.sent if p["cmd"] == "subring"]
    assert sub[0]["path"] == [3] and [o["label"] for o in sub[0]["options"]] == CHATS
    assert len(runs) == 1 and runs[0][:4] == ["osascript", "-l", "JavaScript", "-e"] and runs[0][-1] == "Pat Lee"
    hl = (data_dir / "hud-log" / "2026-01-05.jsonl").read_text()
    out = json.loads(hl.splitlines()[-1])
    assert out["outcome"] == "pressed:3" and out["via"] == "glide" and out["executed"].startswith("telegram.chat#")
    g = out["glide"]
    assert g["visited"] == [{"path": "3", "ms": 640}, {"path": "3.1", "ms": 210}] and g["final"] == "3.1"
    assert g["depth"] == 1 and g["action"] == "telegram.chat" and g["expanded"] == 1
    assert {e["path"] for e in g["expansions"]} == {"*", "3"}
    assert "Eric" not in hl and "secret plan" not in hl                 # no chat title / text logged
    assert rig.logged and rig.logged[-1][0] == "activate:Telegram" and rig.logged[-1][2] == out["executed"]
    toks = [t["token"] for t in normalise.hud_tokens([out])]
    assert toks == ["hud:shown", "hud:pressed:3", "hud:glide:1"]


def test_live_glide_cancel_is_logged_with_its_depth(data_dir):
    rig = Rig()
    glog = {"visited": [{"path": [2], "ms": 700}], "expanded": 1, "depth": 0}
    lv, runs = _live(EventSender({"event": "dismissed", "reason": "glide_cancel", "glide": glog}, expand=(2,)), rig)
    row = {"ts": "2026-01-05T09:00:00.000Z", "form": "A", "options": [], "shown": False, "outcome": "x",
           "executed": None, "latency_ms": None}
    lv.dispatch([dict(TOPS[1], p_markov=0.4)], row, {"app": "Safari"}, {"ttl_ms": 15000}, time.time())
    lv.wait_replies(5)
    out = json.loads((data_dir / "hud-log" / "2026-01-05.jsonl").read_text().splitlines()[-1])
    assert out["outcome"] == "dismissed:glide_cancel" and out["glide"]["final"] is None and out["glide"]["action"] is None
    assert runs == []
    assert [t["token"] for t in normalise.hud_tokens([out])] == ["hud:shown", "hud:dismissed", "hud:glide:0"]
    assert normalise.validate("hud:glide:3") and not normalise.validate("hud:glide:4")


def test_live_glide_vocal_followup_dry_exec_sends_nothing(data_dir):
    rig = Rig(answer={"followups": {"2": [{"label": "Ask Nova", "action": "vocal:nova:status of the PRR?"}],
                                    "1": []}})
    seen = []

    def vocal_send(to, text, **kw):
        seen.append((to, text, kw["dry_run"]))
        return {"ok": True, "to": to, "dry_run": kw["dry_run"]}
    lv, runs = _live(EventSender({"event": "pressed", "key": 2, "via": "glide", "path": [2, 1],
                                  "glide": {"visited": [], "expanded": 1, "final": [2, 1], "depth": 1}},
                                 expand=[(2,)]),
                     rig, exec_dry_run=True, vocal_send=vocal_send)
    row = {"ts": "2026-01-05T09:00:00.000Z", "form": "A", "options": [], "shown": False, "outcome": "x",
           "executed": None, "latency_ms": None}
    lv.dispatch([dict(TOPS[2], p_markov=0.4)], row, {"app": "Mail", "space": 1}, {"ttl_ms": 15000}, time.time())
    lv.wait_replies(5)
    assert rig.sent[-1]["options"][0]["label"] == "Ask Nova"               # released on the send itself: no "Send ✓"
    assert seen == [("nova", "(via the HUD glide) status of the PRR?", True)] and runs == []
    out = json.loads((data_dir / "hud-log" / "2026-01-05.jsonl").read_text().splitlines()[-1])
    assert out["executed"] == "vocal:nova" and out["glide"]["action"] == "vocal:nova"


def test_dry_exec_executor_prints_and_runs_nothing(capsys):
    lv = live.Live(pull=False, sender=lambda *a: None, exec_dry_run=True)
    assert lv.executor.run({"primitive": "app.open", "params": {"app": "Telegram"}, "token": "activate:Telegram"},
                           {}, lv.url_for) == "activate:Telegram"
    assert lv.executor.run({"primitive": "mail.reply", "params": {}}, {}, lv.url_for, message_id="<m1>") == "mail.reply"
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert out[0] == {"dry_exec": ["open", "-a", "Telegram"]} and out[1]["args"] == ["reply", "<m1>", "", "window"]


def test_socket_sender_routes_glide_and_expand_lines_to_on_event():
    d = tempfile.mkdtemp(prefix="oi-", dir="/tmp")
    path = Path(d) / "h.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path)); srv.listen(1)

    def hud():
        c, _ = srv.accept()
        c.recv(65536)
        c.sendall(b'{"event":"glide","phase":"start"}\n{"event":"expand","path":[2]}\n'
                  b'{"event":"pressed","key":2,"path":[2,1],"via":"glide"}\n')
        time.sleep(0.2); c.close()
    threading.Thread(target=hud, daemon=True).start()
    events, replies = [], []
    t = live.socket_sender(path, 5)({"cmd": "show", "options": []}, replies.append, on_event=events.append)
    t.join(5)
    time.sleep(0.1)
    assert replies == [{"event": "pressed", "key": 2, "path": [2, 1], "via": "glide"}]
    assert sorted(e["event"] for e in events) == ["expand", "glide"]
    srv.close()


def test_prefetch_latency_with_a_fake_runner_is_tiny():
    r = Rig()
    s = r.session()
    t0 = time.monotonic()
    s.prefetch()
    t1 = time.monotonic()
    s.expand([1])
    t2 = time.monotonic()
    assert t1 - t0 < 0.5 and t2 - t1 < 0.2


# ---- W15 follow-up (W18 PR): a summon prefetches the glide follow-ups ----------------------------------------

class SilentSender:
    """Plays a HUD that shows and waits: no glide, no reply until released."""

    def __init__(self):
        self.release = threading.Event()

    def __call__(self, payload, on_reply, on_event=None):
        t = threading.Thread(target=lambda: (self.release.wait(5), on_reply({"event": "dismissed", "reason": "ttl"})))
        t.start()
        return t


def _wait(cond, s=3.0):
    end = time.time() + s
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_summon_prefetches_the_glide_without_a_hold(data_dir):
    rig = Rig()
    sender = SilentSender()
    lv, _ = _live(sender, rig)
    row = {"ts": "2026-01-05T09:00:00.000Z", "form": "A", "options": [], "shown": False, "outcome": "x",
           "executed": None, "latency_ms": None, "summoned": True}
    lv.dispatch([dict(TOPS[2], p_markov=0.4)], row, {"app": "Mail"}, {"ttl_ms": 15000}, time.time())
    assert _wait(lambda: len(rig.prompts) == 1)          # the batched prefetch ran while the HUD is up
    sender.release.set()
    lv.wait_replies(5)
    assert len(rig.prompts) == 1


def test_gated_show_never_prefetches(data_dir):
    rig = Rig()
    sender = SilentSender()
    lv, _ = _live(sender, rig)
    row = {"ts": "2026-01-05T09:00:00.000Z", "form": "A", "options": [], "shown": False, "outcome": "x",
           "executed": None, "latency_ms": None}
    lv.dispatch([dict(TOPS[2], p_markov=0.4)], row, {"app": "Mail"}, {"ttl_ms": 15000}, time.time())
    time.sleep(0.3)
    sender.release.set()
    lv.wait_replies(5)
    assert rig.prompts == []


def test_empty_summon_never_prefetches(data_dir):
    rig = Rig()
    sender = SilentSender()
    lv, _ = _live(sender, rig)
    row = {"ts": "2026-01-05T09:00:00.000Z", "form": "A", "options": [], "shown": False, "outcome": "x",
           "executed": None, "latency_ms": None, "summoned": True}
    lv.dispatch([], row, {"app": "Safari"}, {"ttl_ms": 15000}, time.time())
    time.sleep(0.2)
    sender.release.set()
    lv.wait_replies(5)
    assert rig.prompts == []


# ---- W21b review note: sub-ring keys are scoped by their parent path ------------------------------------------

def test_subring_keys_are_one_based_within_the_parent_and_never_root_keys():
    # W21 root keys are modifier numbers: only ⌃ 2 and ⌥ 3 on screen; the sub-ring of 3 numbers its children 1..n
    r = Rig()
    s = r.session(keymap={2: dict(TOPS[1]), 3: dict(TOPS[3])}, pages={"notion.so": ["notion.so/prr", "notion.so/x"]})
    s.prefetch()
    p = s.expand([3])
    assert p["path"] == [3] and [o["key"] for o in p["options"]] == [1, 2]
    assert all(1 <= o["key"] <= glide.MAX_FOLLOWUPS for o in p["options"])
    assert s.node([3, 2])["label"] == p["options"][1]["label"]           # child 2 of 3, not the root 2
    assert s.node([2])["primitive"] == "app.open"                         # the root 2 is untouched
    assert s.node([2, 1]) is None and s.node([1]) is None                 # no ring under 2 yet; no root 1


# ---- context cards -----------------------------------------------------------------------

CARD = {"summary": ["Sam asks for the training quote", "Deadline: Friday", "Cc Nova"],
        "excerpt": "pouvez-vous nous envoyer le devis ?"}
WIKI_EV = {"source": "mail", "class": "client", "who": "Sam", "subject": "Re: devis formation",
           "ref": {"message_id": "<m1@acme>", "address": "sam@acme-conseil.example"},
           "wiki": {"important": True, "page": "atlas_rnd/clients/acme.md", "kind": "client",
                    "snippet": "Acme Conseil: active client, training programme Q4"}}


def _cards(sent):
    return [p["cards"] for p in sent if p["cmd"] == "context"]


def test_cards_level2_at_once_then_level3_from_the_same_batched_call_and_cached():
    r = Rig(answer=dict(ANSWER, context=CARD))
    s = r.session()
    s.prefetch()
    assert len(r.prompts) == 1                                           # ONE call: follow-ups + the summary
    prompt = r.prompts[0][0]
    assert "<material>\nBonjour Alex, pouvez-vous nous envoyer le devis ?\n</material>" in prompt
    first, second = _cards(r.sent)
    mail2 = next(c for c in first if c["key"] == 2)
    assert mail2 == {"key": 2, "what": "Sam Martin · Re: devis formation", "pending": True}   # level 2, local
    assert {c["key"]: c["what"] for c in first} == {1: "Telegram", 2: "Sam Martin · Re: devis formation",
                                                    3: "notion.so/prr"}   # a desk move has no card
    mail2 = next(c for c in second if c["key"] == 2)
    assert mail2["summary"] == CARD["summary"] and mail2["excerpt"] == CARD["excerpt"] and "pending" not in mail2
    assert "summary" not in next(c for c in second if c["key"] == 1)    # only the mail direction is summarised
    assert s.expansions[0]["summary"] is True and s.expansions[0]["cards"] == 3
    # a second hold in the same context: the cards come with the summary at once, no call
    r.sent.clear()
    s2 = r.session()
    s2.prefetch()
    assert len(r.prompts) == 1 and len(_cards(r.sent)) == 1
    assert next(c for c in _cards(r.sent)[0] if c["key"] == 2)["summary"] == CARD["summary"]


def test_card_excerpt_must_be_verbatim_and_lines_are_bounded():
    mat = "Bonjour Alex,   pouvez-vous nous envoyer le devis ? Merci"
    c = glide.parse_card({"context": {"summary": ["a" * 200, "b", "", 3, "c", "d", "e"],
                                      "excerpt": "« Pouvez-vous nous envoyer le devis ? »"}}, mat)
    assert c["summary"] == ["a" * glide.SUMMARY_LINE_MAX, "b", "c", "d"] and c["excerpt"] == "Pouvez-vous nous envoyer le devis ?"
    c = glide.parse_card({"context": {"summary": "One line", "excerpt": "Please sign the contract today"}}, mat)
    assert c == {"summary": ["One line"]}                               # an invented excerpt is dropped
    assert glide.parse_card({"followups": {}}, mat) is None and glide.parse_card(None, mat) is None


def test_answer_without_a_card_is_cached_and_a_failed_call_is_not():
    r = Rig()                                                            # ANSWER has no "context"
    s = r.session()
    s.prefetch()
    second = _cards(r.sent)[-1]
    assert set(next(c for c in second if c["key"] == 2)) == {"key", "what"}   # no summary, no more "pending"
    r.session().prefetch()
    assert len(r.prompts) == 1
    r2 = Rig(fail="timeout")
    r2.session().prefetch()
    r2.session().prefetch()
    assert len(r2.prompts) == 2                                          # retried on the next hold


def test_local_only_mail_never_reaches_the_model_level2_only():
    r = Rig(answer=dict(ANSWER, context=CARD), settings={"local_only_domains": ["acme-conseil.example"]})
    s = r.session()
    s.prefetch()
    assert all("pouvez-vous" not in p for p, _ in r.prompts) and all("<material>\n(none)" in p for p, _ in r.prompts)
    for cards in _cards(r.sent):
        c = next(c for c in cards if c["key"] == 2)
        assert c == {"key": 2, "what": "Sam Martin · Re: devis formation"}   # shown locally, nothing more


def test_proactive_mail_card_reads_the_body_once_and_carries_the_judges_why():
    r = Rig(answer=dict(ANSWER, context=CARD))
    reads = []
    r.engine.body_reader = lambda mid: (reads.append(mid), "Bonjour Alex, pouvez-vous nous envoyer le devis ?")[1]
    evt = {2: dict(TOPS[2], evt=True)}
    s = r.session(keymap=evt, event_card=glide.event_card(WIKI_EV))
    s.mail_fixed = {"sender_name": "Sam", "sender_address": "sam@acme-conseil.example",
                    "subject": "Re: devis formation", "message_id": "<m1@acme>", "cc": [], "reply_to": "",
                    "body": "", "previous": None, "_private": False}
    s.prefetch()
    s.expand([2])
    assert reads == ["<m1@acme>"] and r.mail_reads == 0                 # the event's mail, never the selection
    card = _cards(r.sent)[-1][0]
    assert card["what"] == "Sam · Re: devis formation" and card["summary"] == CARD["summary"]
    assert card["why"] == {"page": "atlas_rnd/clients/acme.md", "snippet": "Acme Conseil: active client, training programme Q4"}
    assert all("Acme Conseil: active client" not in p for p, _ in r.prompts)   # the snippet is shown, never sent


def test_private_event_gets_no_why_no_summary_and_no_body_read():
    ev = dict(WIKI_EV, private=True)
    ev["wiki"] = dict(WIKI_EV["wiki"], snippet=None)
    card = glide.event_card(ev)
    assert card["why"] is None and card["private"] is True
    r = Rig(answer=dict(ANSWER, context=CARD))
    r.engine.body_reader = lambda mid: (_ for _ in ()).throw(AssertionError("no body read for a private mail"))
    s = r.session(keymap={2: dict(TOPS[2], evt=True)}, event_card=card)
    s.mail_fixed = {"sender_name": "Sam", "sender_address": "g@x.example", "subject": "", "message_id": "<p@x>",
                    "cc": [], "reply_to": "", "body": "", "previous": None, "_private": True}
    s.prefetch()
    assert all(c == [{"key": 2, "what": "Sam · Re: devis formation"}] for c in _cards(r.sent))
    assert all("<material>\n(none)" in p for p, _ in r.prompts)


def test_what_lines_for_the_other_directions():
    e = env(mail=None)
    ctx = {"app": "Safari", "url": "notion.so/prr", "title": "PRR roadmap"}
    assert glide.what_line({"primitive": "file.open", "params": {"path": "/Users/you/Docs/Acme/devis.pdf"}}, e, ctx) == "devis.pdf · Acme"
    assert glide.what_line({"primitive": "folder.open", "params": {"path": "/Users/you/Docs/Acme/"}}, e, ctx) == "Acme · Docs"
    assert glide.what_line({"primitive": "url.open", "params": {"url": "notion.so/prr"}}, e, ctx) == "PRR roadmap · notion.so"
    assert glide.what_line({"primitive": "url.open", "params": {"url": "github.com/x"}}, e, ctx) == "github.com/x"
    assert glide.what_line({"primitive": "space.move", "params": {}}, e, ctx) is None
    agent = glide.event_card({"source": "agent", "who": "Nova", "subject": "Board at 72, two PRs to merge",
                              "ref": {"slug": "nova", "chat": "Nova CEO"}})
    assert agent == {"what": "Nova: Board at 72, two PRs to merge", "why": None, "private": False, "agent": "nova",
                     "since": None}
    assert glide.event_card({"source": "agent", "who": "Orion", "ref": {"slug": "orion", "chat": "Orion Fund"}})["what"] == "Orion · Orion Fund"
    assert glide.event_card({"source": "file", "ref": {"path": "/Users/you/Downloads/contrat.pdf"}})["what"] == "contrat.pdf · Downloads"
    assert glide.what_line({"primitive": "telegram.chat", "params": {"chat": "Nova CEO"}, "evt": True}, dict(e, event_card=agent),
                           ctx) == "Nova: Board at 72, two PRs to merge"


def test_read_mail_body_is_read_only_and_tolerant():
    calls = []

    def osa(script, args, timeout=6):
        calls.append((script, args))
        return 0, "OK" + ca.SEP + "Hello\r\nworld"
    assert ca.read_mail_body("<m1@x>", osa, body_max=50) == "Hello\nworld"
    assert "content of" in calls[0][0] and "read status" not in calls[0][0] and "activate" not in calls[0][0]
    assert ca.read_mail_body("", osa) == "" and len(calls) == 1
    assert ca.read_mail_body("<m2@x>", lambda s, a, timeout=6: (0, "ERR:not_found")) == ""


AGENT_EV = {"source": "agent", "class": "atlas", "who": "Atlas",
            "ref": {"chat": "Atlas - R&D", "slug": "atlas", "n": 2, "since": "2026-10-21T12:05:00Z"}}
AGENT_CARD = {"summary": ["Atlas asks to merge PR #12", "Decision needed: yes", "No deadline"],
              "excerpt": "need your go on the deploy"}


def test_agent_event_level2_waiting_card():
    card = glide.event_card(AGENT_EV)
    assert card["what"] == "Atlas t'attend · 2 messages · depuis 14:05"   # Paris time
    assert card["agent"] == "atlas" and card["since"] == "2026-10-21T12:05:00Z"
    one = glide.event_card(dict(AGENT_EV, ref=dict(AGENT_EV["ref"], n=1, since="bad")))
    assert one["what"] == "Atlas t'attend · 1 message"


def test_agent_event_level3_summary_reads_the_agents_messages_once_at_hold():
    r = Rig(answer=dict(ANSWER, context=AGENT_CARD))
    reads = []
    r.engine.agent_reader = lambda slug: (reads.append(slug), "Atlas: PR #12 ready, merge?\n\nSecond: need your go on the deploy")[1]
    evt = {2: {"key": "evt_1", "primitive": "telegram.chat", "params": {"chat": "Atlas - R&D"}, "token": "telegram.chat:atlas",
               "label": "Ouvrir Telegram — Atlas", "evt": True}}
    s = r.session(keymap=evt, ctx={"app": "Safari", "url": "notion.so/prr", "space": 2}, event_card=glide.event_card(AGENT_EV))
    s.prefetch()
    s.expand([2])
    assert reads == ["atlas"] and r.mail_reads == 0                        # read once, at the hold, never the mail
    assert "<material>\nAtlas: PR #12 ready, merge?\n\nSecond: need your go on the deploy\n</material>" in r.prompts[0][0]
    first, last = _cards(r.sent)[0][0], _cards(r.sent)[-1][0]
    assert first == {"key": 2, "what": "Atlas t'attend · 2 messages · depuis 14:05", "pending": True}
    assert last["summary"] == AGENT_CARD["summary"] and last["excerpt"] == AGENT_CARD["excerpt"]


def test_agent_event_level3_gates():
    evt = {2: {"key": "evt_1", "primitive": "telegram.chat", "params": {"chat": "Atlas - R&D"}, "token": "t", "evt": True}}
    no_read = lambda slug: (_ for _ in ()).throw(AssertionError("no read"))  # noqa: E731
    # a private event: no read, level 2 only
    r = Rig(answer=dict(ANSWER, context=AGENT_CARD))
    r.engine.agent_reader = no_read
    s = r.session(keymap=evt, event_card=dict(glide.event_card(AGENT_EV), private=True))
    s.prefetch()
    assert all("<material>\n(none)" in p for p, _ in r.prompts) and all("summary" not in c[0] for c in _cards(r.sent))
    # a denied term in the messages (W25 leaks gate): never sent to the model
    r = Rig(answer=dict(ANSWER, context=AGENT_CARD), settings={"local_only_domains": ["acme-conseil.example"]})
    r.engine.agent_reader = lambda slug: "Acme Conseil wants the quote"
    s = r.session(keymap=evt, event_card=glide.event_card(AGENT_EV))
    s.prefetch()
    assert all("Acme" not in p for p, _ in r.prompts)
    # a VPS agent (no local record): nothing to summarise, level 2 only
    r = Rig(answer=dict(ANSWER, context=AGENT_CARD))
    r.engine.agent_reader = lambda slug: ""
    s = r.session(keymap=evt, event_card=glide.event_card(AGENT_EV))
    s.prefetch()
    assert all("summary" not in c[0] and "pending" not in c[0] for c in _cards(r.sent))


def test_local_level3_card_for_a_metric_event_needs_no_model():
    """Contract level 3 for the sources whose content is local (metric / market / cockpit): the card carries the
    summary lines at once, no model call reads anything for it."""
    ev = {"source": "metric", "class": "aum", "who": "AUM", "subject": "AUM below 5", "ts": "2026-10-21T12:00:00Z",
          "ref": {"slug": "aum", "value": 4, "threshold": 5, "url": "https://x.example/kpi"}, "private": False}
    card = glide.event_card(ev)
    assert card["what"] == "AUM · AUM below 5 · depuis 14:00"
    assert card["summary"] == ["AUM: 4 (seuil 5)", "Sous le seuil depuis 14:00"]
    r = Rig()
    evt = {2: {"key": "evt_1", "primitive": "url.open", "params": {"href": "https://x.example/kpi"}, "token": "t",
               "evt": True}}
    s = r.session(keymap=evt, event_card=card)
    s.prefetch()
    c = next(c for c in _cards(r.sent)[0] if c["key"] == 2)
    assert c["summary"] == card["summary"] and "pending" not in c
    assert all("AUM: 4" not in p for p, _ in r.prompts)                   # local only: never in a prompt
