"""W14: Claude in the app. Synthetic mails only; the `claude` binary is a fake on PATH."""
from __future__ import annotations

import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from oi import candidates, claude_actions as ca, forms, live

MAIL = {"sender_name": "Sam Martin", "sender_address": "sam@acme-conseil.example",
        "subject": "Re: devis formation", "date": "2026-01-05T09:00:00", "message_id": "<m1@acme>",
        "body": "Bonjour Alex, pouvez-vous nous envoyer le devis ?", "previous": None}


# ---- settings, privacy -------------------------------------------------------------------------------

def test_claude_settings_defaults_and_overrides(data_dir):
    assert ca.claude_settings() == {"enabled": True, "enrich": True, "local_only_domains": [], "debug": False,
                                    "orchestrator": True}
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"claude": {
        "enabled": False, "enrich": "yes", "local_only_domains": [" Client.EXAMPLE ", "@other.example", 3]}}))
    s = ca.claude_settings()
    assert s["enabled"] is False and s["enrich"] is True            # wrongly typed -> default
    assert s["local_only_domains"] == ["client.example", "other.example"]


def test_local_only_gate_matches_domain_and_subdomains():
    cfg = {"local_only_domains": ["client.example"]}
    assert ca.local_only("client.example", cfg) and ca.local_only("mail.client.example", cfg)
    assert ca.local_only("www.client.example", cfg)
    assert not ca.local_only("notclient.example", cfg) and not ca.local_only("", cfg)
    assert ca.domain_of("Sam <g@Client.EXAMPLE>") == "client.example"


def test_offers_only_supported_primitives_when_enabled():
    on, off = {"enabled": True}, {"enabled": False}
    assert all(ca.offers(p, on) for p in ("mail.reply", "mail.forward", "url.open", "app.open"))
    assert not ca.offers("space.move", on) and not ca.offers("mail.reply", off)


# ---- Mail form + candidates --------------------------------------------------------------------------

def test_mail_form_validates_and_pins_reply_and_forward(moments, model):
    f = forms.load_form("B")
    assert forms.validate(f) == []
    assert {k for k, o in f["options"].items() if o.get("enrich")} == {"reply_sender", "forward_to"}
    m = dict(moments[-1], context=dict(moments[-1]["context"], app="Mail"))
    menu = candidates.build(f, m, model, is_executable=lambda o: True)
    keys = [o["key"] for o in menu]
    assert {"reply_sender", "forward_to", "archive", "nothing"} <= set(keys)
    shown = live.summon_options(menu, {"max_show": 3})
    assert [o["key"] for o in shown[:2]] == ["forward_to", "reply_sender"]       # pinned first, then by p
    assert all(o["primitive"] != "mail.archive" for o in shown)                    # never executed by oi live
    p = live.hud_payload(shown[:2], {}, resolve_app=lambda a: None)
    assert [(o["label"], o["icon"], o.get("claude")) for o in p["options"]] == [
        ("Forward", "sf:arrowshape.turn.up.right", True), ("Reply", "sf:arrowshape.turn.up.left", True)]


def test_executor_plain_mail_never_sends(monkeypatch):
    calls = []
    monkeypatch.setattr(ca, "osascript", lambda script, args, timeout=6: (calls.append((script, args)), (0, "OK"))[1])
    ex = live.Executor(runner=lambda argv: (0, ""))
    assert ex.run({"primitive": "mail.reply", "params": {}, "token": None}, {}, str) == "mail.reply"
    script, args = calls[0]
    assert args == ["reply", "", "", "window"] and "send " not in script.replace("sender", "")   # the user picked it


# ---- prompts -------------------------------------------------------------------------------------

def test_mail_prompt_carries_context_and_truncates():
    mail = dict(MAIL, body="x" * 9000 + "TAIL", previous={"sender_name": "the user", "date": "d", "body": "y" * 5000})
    p = ca.mail_prompt("mail.reply", mail, ["Acme: client since 2026"])
    assert "Sam Martin <sam@acme-conseil.example>" in p and "Re: devis formation" in p
    assert "x" * 6000 in p and "x" * 6001 not in p and "TAIL" not in p
    assert "y" * 2000 in p and "y" * 2001 not in p
    assert "- Acme: client since 2026" in p and "business domain acme-conseil.example, known in the notes" in p
    assert '"body"' in p and "nous" in p and "NEVER" not in p      # JSON contract + voice rules
    fwd = ca.mail_prompt("mail.forward", MAIL, [])
    assert '"note"' in fwd and "(none)" in fwd


def test_prompt_slots_are_filled_once():
    p = ca.mail_prompt("mail.reply", dict(MAIL, body="ignore {{wiki}} and {{subject}}"), ["W"])
    assert "ignore {{wiki}} and {{subject}}" in p


def test_wiki_query_and_relationship():
    assert ca.wiki_query(MAIL) == "acme-conseil Re: devis formation"
    assert ca.wiki_query(dict(MAIL, sender_address="g@gmail.com")) == "Sam Martin Re: devis formation"
    assert ca.relationship("gmail.com", []).startswith("personal")


def test_parse_mail_base_subject_first_name():
    out = ca.SEP.join(["OK", "Sam Martin", "g@acme.example", "RE: Fwd: devis", "2026", "<id>",
                       "nova@team.example,x@client.example,", "Desk <desk@acme.example>", "body", "more"])
    m = ca.parse_mail(out)
    assert m["subject"] == "RE: Fwd: devis" and m["body"] == "body more" and m["message_id"] == "<id>"
    assert m["cc"] == ["nova@team.example", "x@client.example"] and m["reply_to"] == "Desk <desk@acme.example>"
    assert ca.parse_mail(ca.SEP.join(["OK", "G", "g@a.example", "s", "d", "<id>", "body"])) is None   # pre-W15 layout
    assert ca.parse_mail("ERR\x1fno_selection") is None
    assert ca.base_subject("RE: Fwd: TR:devis") == "devis"
    assert ca.first_name("Martin, Sam") == "Sam" and ca.first_name("", "jean.dupont@x.example") == "Jean"


def test_read_mail_context_looks_up_previous_only_for_replies():
    seen = []

    def osa(script, args, timeout=6):
        seen.append(script)
        if script is ca.READ_MAIL:
            return 0, ca.SEP.join(["OK", "G", "g@acme.example", subject[0], "d", "<id>", "", "", "body"])
        return 0, ca.SEP.join(["OK", "the user", "d0", "earlier"])
    subject = ["Re: devis"]
    m = ca.read_mail_context(osa)
    assert m["previous"] == {"sender_name": "the user", "date": "d0", "body": "earlier"} and len(seen) == 2
    seen.clear(); subject[0] = "devis"
    assert ca.read_mail_context(osa)["previous"] is None and len(seen) == 1


# ---- the runner ----------------------------------------------------------------------------------

def test_build_command_per_auth_mode(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    env = {"PATH": "/bin", "ANTHROPIC_API_KEY": "k", "GH_TOKEN": "g", "TELEGRAM_BOT_TOKEN": "t", "KEEP": "1"}
    t1 = tmp_path / "t1"; t1.mkdir()
    argv, child = ca.build_command("sonnet", "keychain", t1, env, home, "claude")
    assert argv[:7] == ["claude", "-p", ca.INSTRUCTION, "--model", "sonnet", "--output-format", "json"]
    assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv and argv[-2:] == ["--setting-sources", "project"]
    assert "CLAUDE_CONFIG_DIR" not in child and child["TELEGRAM_STATE_DIR"] == str(t1)
    assert not {"ANTHROPIC_API_KEY", "GH_TOKEN", "TELEGRAM_BOT_TOKEN"} & set(child) and child["KEEP"] == "1"
    (home / ".claude/.credentials.json").write_text("{}")
    t2 = tmp_path / "t2"; t2.mkdir()
    argv, child = ca.build_command("haiku", "file", t2, env, home, "claude")
    assert child["CLAUDE_CONFIG_DIR"] == str(t2) and (t2 / ".credentials.json").exists()
    assert stat.S_IMODE((t2 / ".credentials.json").stat().st_mode) == 0o600
    assert json.loads((t2 / ".claude.json").read_text())["hasCompletedOnboarding"] is True
    assert "--setting-sources" not in argv


def test_auth_mode_order(tmp_path):
    home = tmp_path
    assert ca.auth_mode({"CLAUDE_CODE_OAUTH_TOKEN": "x"}, home) == "env"
    assert ca.auth_mode({}, home, platform="darwin") == "keychain"
    assert ca.auth_mode({}, home, platform="linux") is None           # never an API key
    assert ca.auth_mode({"ANTHROPIC_API_KEY": "k"}, home, platform="linux") is None
    (home / ".claude").mkdir(); (home / ".claude/.credentials.json").write_text("{}")
    assert ca.auth_mode({}, home, platform="linux") == "file"


def _fake_claude(tmp_path, body: str) -> Path:
    d = tmp_path / "bin"; d.mkdir(exist_ok=True)
    f = d / "claude"
    f.write_text("#!/bin/sh\n" + body)
    f.chmod(0o755)
    return d


def _env(bindir, **extra):
    return {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": os.environ["HOME"], **extra}


def test_run_claude_with_a_fake_binary_on_path(tmp_path):
    log = tmp_path / "seen"
    bindir = _fake_claude(tmp_path, f'''cat > "{log}.stdin"
printf '%s\\n' "$@" > "{log}.args"
env > "{log}.env"
pwd > "{log}.cwd"
printf '%s\\n' '{{"type":"result","is_error":false,"result":"```json\\n{{\\"body\\": \\"Bonjour\\"}}\\n```"}}'
''')
    out = ca.run_claude("PROMPT TEXT", "sonnet", env=_env(bindir, ANTHROPIC_API_KEY="nope"),
                        home=tmp_path, binary=str(bindir / "claude"))
    assert out == {"body": "Bonjour"}
    assert Path(f"{log}.stdin").read_text() == "PROMPT TEXT"                   # the prompt never in argv
    args = Path(f"{log}.args").read_text().splitlines()
    assert "PROMPT TEXT" not in args and "--model" in args and "sonnet" in args
    assert "ANTHROPIC_API_KEY" not in Path(f"{log}.env").read_text()
    cwd = Path(f"{log}.cwd").read_text().strip()
    assert "oi-claude-" in cwd and not Path(cwd).exists()                       # temp dir removed


@pytest.mark.parametrize("body,code", [
    ("echo 'Not logged in · Please run /login'; exit 1", "stub"),
    ('echo \'{"type":"result","is_error":true,"result":"boom"}\'', "error_result"),
    ('echo \'{"type":"result","is_error":false,"result":"no json here"}\'', "bad_json"),
    ("exit 3", "rc_3"),
    ("sleep 5", "timeout"),
])
def test_run_claude_failures_are_unavailable(tmp_path, body, code):
    bindir = _fake_claude(tmp_path, "cat >/dev/null\n" + body + "\n")
    t0 = time.monotonic()
    with pytest.raises(ca.ClaudeUnavailable) as e:
        ca.run_claude("p", "haiku", env=_env(bindir), home=tmp_path, binary=str(bindir / "claude"), timeout=1)
    assert e.value.code == code and time.monotonic() - t0 < 4


def test_run_claude_no_binary(tmp_path):
    with pytest.raises(ca.ClaudeUnavailable) as e:
        ca.run_claude("p", "haiku", env=_env(tmp_path), home=tmp_path, binary=str(tmp_path / "missing"))
    assert e.value.code == "no_binary"


# ---- the actions ---------------------------------------------------------------------------------

class Rig:
    """ClaudeActions with every collaborator faked."""

    def __init__(self, *, answer=None, fail=None, ack=True, mail=MAIL, settings=None, wiki=("Acme: client",)):
        self.calls, self.sent, self.osa_calls, self.plain_calls, self.wiki_q = [], [], [], [], []
        self.answer, self.fail, self.ack = answer, fail, ack

        def runner(prompt, model):
            self.calls.append((prompt, model))
            if self.fail:
                raise ca.ClaudeUnavailable(self.fail)
            return self.answer

        def sender(payload):
            self.sent.append(payload)
            if payload["cmd"] == "result" and not self.ack:
                return {"event": "dropped", "reason": "cancelled"}
            return {"event": "ok"}

        def osa(script, args, timeout=6):
            self.osa_calls.append((script, args))
            return 0, ("OK:42" if script is ca.OPEN_DRAFT else "OK")

        self.acts = ca.ClaudeActions(Path("/nonexistent.sock"), runner=runner, sender=sender, osa=osa,
                                     wiki=lambda q: (self.wiki_q.append(q), list(wiki))[1],
                                     mail_reader=lambda **kw: dict(mail) if mail else None,
                                     safari_reader=lambda **kw: {"title": "Doc", "url": "https://docs.example.org/a",
                                                                 "text": "page text", "js": True},
                                     settings_loader=lambda: dict(ca.CLAUDE_DEFAULTS, **(settings or {})))

    def plain(self):
        self.plain_calls.append(1)
        return "plain"


def test_mail_reply_drafts_with_sonnet_in_place_and_shows_it_on_the_card():
    r = Rig(answer={"language": "fr", "body": "Bonjour Sam,\nJe vous envoie le devis.\nAlex"})
    executed, meta = r.acts.press({"primitive": "mail.reply", "params": {}}, {"app": "Mail"}, 2, r.plain)
    assert executed == "mail.reply" and r.plain_calls == []
    assert meta["action"] == "mail.reply" and meta["model"] == "sonnet" and meta["ok"] is True
    assert set(meta) == {"action", "model", "latency_ms", "ok"}                   # nothing else is logged
    prompt, model = r.calls[0]
    assert "pouvez-vous nous envoyer le devis" in prompt and "- Acme: client" in prompt
    assert r.wiki_q == ["acme-conseil Re: devis formation"]
    script, args = r.osa_calls[0]                 # the draft first, in place (no window, Mail not fronted)
    assert script is ca.OPEN_DRAFT and args == ["reply", "<m1@acme>", "Bonjour Sam,\nJe vous envoie le devis.\nAlex",
                                                 "place"]
    assert r.sent == [{"cmd": "result", "key": 2, "ok": True, "ttl_ms": ca.DRAFT_SHOW_TTL_MS, "open_draft": 42,
                       "text": "Draft saved in Mail Drafts (not sent):\nBonjour Sam,\nJe vous envoie le devis.\nAlex"}]
    assert "send " not in ca.OPEN_DRAFT.replace("sender", "")


def test_open_draft_script_never_fronts_mail_in_place():
    """the in-place branch has no window and no `activate` (the window branch, the plain reply, has both);
    the mail is looked up by message id; Mail is never launched; SHOW_DRAFT (the « Open in Mail » press) activates."""
    s = ca.OPEN_DRAFT
    assert 'if application "Mail" is not running then return "ERR:not_running"' in s
    assert s.index("is not running") < s.index('tell application "Mail"')
    assert "messages of inbox whose message id is mid" in s and "opening window win" in s
    place = s[s.index("\t\tsave r"):]
    assert "activate" not in place and "save r" in place and 'return "OK:" & ((id of r) as string)' in place
    assert s.count("activate") == 1 and s.index("activate") < s.index("save r")      # only in the `if win` branch
    for script in (ca.READ_MAIL, ca.READ_PREVIOUS):
        assert "is not running" in script and "activate" not in script and "message id is mid" in script
    assert "activate" in ca.SHOW_DRAFT and "set visible of (outgoing message id did) to true" in ca.SHOW_DRAFT
    assert "send " not in ca.SHOW_DRAFT.replace("sender", "")
    assert ca.draft_id("OK:17") == 17 and ca.draft_id("OK") is None and ca.draft_id("ERR:x") is None
    calls = []
    assert ca.show_draft(17, lambda sc, a, timeout=6: (calls.append((sc, a)), (0, "OK"))[1])
    assert calls == [(ca.SHOW_DRAFT, ["17"])]


def test_mail_reply_by_message_id_reads_that_mail_not_the_selection():
    seen = []
    r = Rig(answer={"body": "x"})
    r.acts.mail_reader = lambda **kw: (seen.append(kw), dict(MAIL))[1]
    executed, _ = r.acts.press({"primitive": "mail.reply", "params": {}}, {}, 2, r.plain, expect_mid="<m1@acme>")
    assert executed == "mail.reply" and seen == [{"previous": True, "message_id": "<m1@acme>"}]
    args = []
    ca.read_mail_context(lambda sc, a, timeout=6: (args.append(a), (0, ""))[1], message_id="<m1@acme>")
    assert args == [[str(ca.BODY_MAX), "<m1@acme>"]]


def test_draft_not_saved_says_so():
    r = Rig(answer={"body": "x"})
    r.acts.osa = lambda script, args, timeout=6: (0, "ERR:not_running")
    executed, _ = r.acts.press({"primitive": "mail.reply", "params": {}}, {}, 2, r.plain)
    assert executed is None and r.sent[-1]["ok"] is False and "open_draft" not in r.sent[-1]


def test_mail_forward_uses_the_note():
    r = Rig(answer={"note": "Bonjour,\nPour info.\nAlex"})
    executed, meta = r.acts.press({"primitive": "mail.forward", "params": {}}, {}, 1, r.plain)
    assert executed == "mail.forward" and r.osa_calls[0][1][0] == "forward"
    assert '"note"' in r.calls[0][0]


def test_esc_while_claude_works_leaves_only_the_saved_draft():
    """the draft is made in place before the card (its id rides on the card): Esc = no card, no window,
    nothing fronted; the draft stays in Drafts, never sent."""
    r = Rig(answer={"body": "x"}, ack=False)
    executed, _ = r.acts.press({"primitive": "mail.reply", "params": {}}, {}, 1, r.plain)
    assert executed == "mail.reply" and r.plain_calls == []
    assert [a[3] for s, a in r.osa_calls if s is ca.OPEN_DRAFT] == ["place"] and len(r.osa_calls) == 1


def test_unavailable_falls_back_to_the_plain_action():
    r = Rig(fail="timeout")
    executed, meta = r.acts.press({"primitive": "mail.reply", "params": {}}, {}, 1, r.plain)
    assert executed == "plain" and meta["ok"] is False and r.osa_calls == []
    assert r.sent[0]["ok"] is False and "Claude unavailable" in r.sent[0]["text"]


def test_privacy_gate_keeps_local_only_senders_away_from_claude():
    r = Rig(answer={"body": "x"}, settings={"local_only_domains": ["acme-conseil.example"]})
    executed, meta = r.acts.press({"primitive": "mail.reply", "params": {}}, {}, 1, r.plain)
    assert executed == "plain" and meta is None and r.calls == [] and r.wiki_q == []
    assert "Local-only" in r.sent[0]["text"]


def test_no_selected_mail():
    r = Rig(mail=None)
    executed, _ = r.acts.press({"primitive": "mail.reply", "params": {}}, {}, 1, r.plain)
    assert executed is None and r.sent[0]["ok"] is False and r.calls == []


def test_dry_run_prints_the_applescript_and_runs_nothing(capsys):
    r = Rig(answer={"body": "Bonjour"})
    r.acts.dry_run = True
    executed, _ = r.acts.press({"primitive": "mail.reply", "params": {}}, {}, 1, r.plain)
    out = json.loads(capsys.readouterr().out)
    assert executed == "mail.reply" and r.osa_calls == []
    assert out["dry_run"] == "osascript" and out["args"] == ["reply", "<m1@acme>", "Bonjour", "place"]


def test_unsupported_or_disabled_releases_the_hud_then_plain():
    r = Rig(settings={"enabled": False})
    executed, meta = r.acts.press({"primitive": "mail.reply", "params": {}}, {}, 3, r.plain)
    assert executed == "plain" and meta is None and r.sent == [{"cmd": "result", "key": 3, "ok": True, "text": "",
                                                                "ttl_ms": ca.CARD_TTL_MS}]
    r = Rig()
    assert r.acts.press({"primitive": "space.move"}, {}, 1, r.plain)[0] == "plain" and r.calls == []


def test_url_open_long_press_opens_then_summarises_with_haiku():
    r = Rig(answer={"lines": ["Docs page", "  about   X  ", "nothing urgent", "extra"]})
    executed, meta = r.acts.press({"primitive": "url.open", "params": {"url": "docs.example.org/a"}}, {}, 1, r.plain)
    assert executed == "plain" and r.plain_calls == [1] and meta["model"] == "sonnet"
    assert r.sent[-1]["text"] == "Docs page\nabout X\nnothing urgent"
    assert "page text" in r.calls[0][0] and "https://docs.example.org/a" in r.calls[0][0]


def test_url_summary_respects_local_only_hosts():
    r = Rig(answer={"lines": ["a"]}, settings={"local_only_domains": ["example.org"]})
    executed, meta = r.acts.press({"primitive": "url.open", "params": {"url": "docs.example.org/a"}}, {}, 1, r.plain)
    assert executed == "plain" and r.calls == [] and "Local-only" in r.sent[-1]["text"]


def test_app_open_summary_uses_the_window_title():
    r = Rig(answer={"lines": ["Telegram chat"]})
    executed, _ = r.acts.press({"primitive": "app.open", "params": {"app": "Telegram"}}, {}, 1, r.plain)
    assert executed == "plain" and r.osa_calls[0][0] is ca.READ_WINDOW_TITLE and "App: Telegram" in r.calls[0][0]


def test_debug_files_only_with_the_flag(data_dir):
    r = Rig(answer={"body": "x"})
    r.acts.press({"primitive": "mail.reply", "params": {}}, {}, 1, r.plain)
    assert not (data_dir / "claude-debug").exists()
    r = Rig(answer={"body": "x"}, settings={"debug": True})
    r.acts.press({"primitive": "mail.reply", "params": {}}, {}, 1, r.plain)
    files = list((data_dir / "claude-debug").iterdir())
    assert len(files) == 1 and stat.S_IMODE(files[0].stat().st_mode) == 0o600


# ---- enrichment ----------------------------------------------------------------------------------

def _mail_show():
    shown = [{"key": "reply_sender", "primitive": "mail.reply", "params": {}, "token": None},
             {"key": "open_app_x", "primitive": "app.open", "params": {"app": "X"}, "token": "activate:X"},
             {"key": "forward_to", "primitive": "mail.forward", "params": {}, "token": None}]
    payload = live.hud_payload(shown, {}, resolve_app=lambda a: None)
    return shown, payload


def test_enrichment_local_label_then_haiku_then_cache():
    r = Rig(answer={"labels": {"reply_sender": "Reply: devis formation Claude extra", "forward_to": "Forward: devis"}})
    shown, payload = _mail_show()
    r.acts.after_show("B", shown, payload).join(2)
    assert r.sent[0] == {"cmd": "update", "options": [{"key": 2, "label": "Reply to Sam", "expect": "Reply"}]}
    assert r.sent[1] == {"cmd": "update", "options": [
        {"key": 2, "label": "Reply: devis formation"[:22], "expect": "Reply to Sam"},
        {"key": 4, "label": "Forward: devis", "expect": "Forward"}]}
    assert len(r.calls) == 1 and r.calls[0][1] == "sonnet"
    r.acts.after_show("B", shown, payload).join(2)                         # same mail: cached, no new call
    assert len(r.calls) == 1 and len(r.sent) == 4


def test_enrichment_skips_claude_for_local_only_and_when_off():
    for settings in ({"local_only_domains": ["acme-conseil.example"]}, {"enrich": False}):
        r = Rig(answer={"labels": {"reply_sender": "x"}}, settings=settings)
        shown, payload = _mail_show()
        r.acts.after_show("B", shown, payload).join(2)
        assert r.calls == [] and len(r.sent) == 1 and r.sent[0]["options"][0]["label"] == "Reply to Sam"


def test_enrichment_nothing_for_forms_without_enrich_options():
    r = Rig()
    shown, payload = _mail_show()
    assert r.acts.after_show("A", shown, payload) is None and r.acts.after_show(None, [], {}) is None


# ---- socket commands -----------------------------------------------------------------------------

def test_send_command_reads_the_first_reply_line():
    d = tempfile.mkdtemp(prefix="oiw14")
    path = Path(d) / "s.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path)); srv.listen(1)
    got = []

    def serve():
        c, _ = srv.accept()
        got.append(c.recv(4096))
        c.sendall(b'{"event":"summon","ts":"x"}\n{"event":"dropped","reason":"cancelled"}\n')
        c.close()
    threading.Thread(target=serve, daemon=True).start()
    r = ca.send_command(path, {"cmd": "result", "key": 1, "ok": True, "text": "é"})
    assert r == {"event": "dropped", "reason": "cancelled"}
    assert json.loads(got[0]) == {"cmd": "result", "key": 1, "ok": True, "text": "é"}
    assert ca.send_command(Path(d) / "none.sock", {"cmd": "x"})["reason"] == "socket_error"
    srv.close()


# ---- oi live integration --------------------------------------------------------------------------

def test_live_routes_a_claude_press_and_logs_only_the_meta(rows, model, data_dir):
    class FakeClaude:
        def __init__(self):
            self.pressed, self.shows = [], []

        def press(self, opt, ctx, key, plain):
            self.pressed.append((opt["key"], key))
            return "mail.reply", {"action": "mail.reply", "model": "sonnet", "latency_ms": 5, "ok": True}

        def after_show(self, *a):
            self.shows.append(a)

    fc = FakeClaude()
    lv = live.Live(sender=lambda p, cb: None, claude=fc, pull=False)
    opt = {"key": "reply_sender", "primitive": "mail.reply", "params": {}, "token": None}
    row = {"ts": "2026-01-05T09:00:00.000Z", "form": "B", "options": [], "shown": False, "outcome": "x",
           "executed": None, "latency_ms": None}
    lv.on_reply({"event": "pressed", "key": 1, "via": "key", "mode": "claude"}, row, {1: opt}, {}, time.monotonic())
    lv.on_reply({"event": "pressed", "key": 1, "via": "key"}, row, {1: dict(opt, primitive="space.move")}, {},
                time.monotonic())
    lines = [json.loads(x) for x in (data_dir / "hud-log" / "2026-01-05.jsonl").read_text().splitlines()]
    assert lines[0]["mode"] == "claude" and lines[0]["executed"] == "mail.reply"
    assert lines[0]["claude"] == {"action": "mail.reply", "model": "sonnet", "latency_ms": 5, "ok": True}
    assert "mode" not in lines[1] and "claude" not in lines[1] and fc.pressed == [("reply_sender", 1)]


def test_cli_mail_open_draft_dry_run(capsys):
    """what oi-hud runs on the card's « Open in Mail » press (dry run: nothing touched)."""
    from oi import cli
    args = cli.build_parser().parse_args(["mail-open-draft", "--id", "42", "--dry-run"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out) == {"dry_run": "osascript", "script": "SHOW_DRAFT", "args": ["42"]}
