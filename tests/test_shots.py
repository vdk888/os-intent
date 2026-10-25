"""W17: screenshot -> send to the current contact. Synthetic data only: fake HUD socket (a request
function), fake OCR, fake claude, dry-run / temp-dir delivery. Nothing is ever sent to a real agent."""
import io
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from oi import claude_actions, contact, live, normalise, settings, shots, vocab, vocal

NOW = datetime(2026, 10, 20, 16, 13, 0, tzinfo=timezone.utc).timestamp()   # 18:13 Paris
WHEN = datetime.fromtimestamp(NOW, timezone.utc)
TABLE = vocal.recipients({})


def row(sec_before: float, token: str) -> dict:
    return {"ts": vocab.iso_utc(datetime.fromtimestamp(NOW - sec_before, timezone.utc)), "dur": 0, "token": token,
            "app": None, "src": "aw-window", "raw_id": None}


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "Screenshot 2026-10-20 at 18.13.00.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return p


# ---- vocabulary / tokens -------------------------------------------------------------------------------

def test_shot_token_in_grammar_and_built_from_the_bucket():
    assert vocab.validate("shot:new") and not vocab.validate("shot:old")
    raw = {"oi-watcher-shot_host-a": [
        {"id": 1, "timestamp": "2026-10-20T16:13:00.000Z", "duration": 0,
         "data": {"kind": "shot", "src": "file", "path": "/Users/you/Desktop/Screenshot 1.png"}},
        {"id": 2, "timestamp": "2026-10-20T16:13:05.000Z", "duration": 0, "data": {"kind": "other"}}]}
    toks = normalise.tokens_from_raw(raw, denylist=frozenset())
    assert [(t["token"], t["src"], t["raw_id"]) for t in toks] == [("shot:new", "oi-shot", 1)]
    assert not vocab.validate_row(toks[0])
    assert "Screenshot" not in json.dumps(toks)                 # the path never reaches a token


# ---- contact resolver ----------------------------------------------------------------------------------

def test_contact_map_defaults_and_overrides():
    m = contact.contact_map({})
    assert m["atlas-r-d"] == "atlas" and m["nova-ceo"] == "nova" and m["juno-ext-r-d"] == "juno"
    m = contact.contact_map({"contact_map": {"Atlas - R&D": None, "Sky…": "echo", "Bad": "Not A Slug", 3: "x"}})
    assert "atlas-r-d" not in m and m["sky"] == "echo" and "bad" not in m


def test_resolver_telegram_title_vocal_window_and_unknown():
    rows = [row(600, "title:Telegram/atlas-r-d"), row(300, "activate:Safari")]
    assert contact.resolve(rows, NOW)["slug"] == "atlas"
    assert contact.resolve(rows, NOW)["via"] == "telegram"
    # newest wins: a vocal to Nova after the Atlas chat
    rows2 = rows + [row(100, "vocal:nova")]
    assert contact.resolve(rows2, NOW) == {"slug": "nova", "via": "vocal", "ts": rows2[-1]["ts"]}
    # truncated / suffixed titles match as a prefix ("Nova - CEO…", unread counts)
    assert contact.resolve([row(60, "title:Telegram/nova-ceo-acme-3")], NOW)["slug"] == "nova"
    # older than 15 min: nobody
    assert contact.resolve([row(16 * 60, "title:Telegram/atlas-r-d")], NOW) is None
    # the generic window title "Telegram" or a non-agent chat: nobody
    assert contact.resolve([row(60, "title:Telegram/telegram"), row(30, "title:Telegram/mum")], NOW) is None
    # not Telegram
    assert contact.resolve([row(60, "title:Notion/atlas-r-d")], NOW) is None
    # a slug that cannot be delivered to is skipped
    assert contact.resolve([row(60, "vocal:ghost")], NOW, known=set(TABLE)) is None


# ---- settings / options ---------------------------------------------------------------------------------

def test_shot_settings_defaults_and_types():
    assert shots.shot_settings({}) == shots.SHOT_DEFAULTS
    s = shots.shot_settings({"shots": {"enabled": False, "clipboard": "no", "fallback_contact": None, "ttl_ms": 99}})
    assert s["enabled"] is False and s["clipboard"] is True and s["fallback_contact"] is None and s["ttl_ms"] == 15000


def test_shot_options_with_and_without_contact(monkeypatch):
    monkeypatch.setattr(shots.os.path, "isdir", lambda p: p == shots.TELEGRAM_APP)
    shot = {"id": "s1", "table": TABLE, "who": {"slug": "atlas", "via": "telegram"}, "content": None, "claude": True}
    opts = shots.shot_options(shot, telegram_icon=True)
    assert [o["label"] for o in opts] == ["→ Atlas", "→ other", "Annotate"]
    assert opts[0]["icon"] == "app:/Applications/Telegram.app" and opts[0]["claude"] is True
    assert not opts[1]["claude"] and not opts[2]["claude"]
    assert [o["shot"]["act"] for o in opts] == [("send", "atlas"), ("pick",), ("annotate", "atlas")]
    assert {o["shot"]["id"] for o in opts} == {"s1"} and all(o["live"] == "shot" for o in opts)
    opts = shots.shot_options(dict(shot, who={"slug": "nova", "via": "default"}, claude=False), telegram_icon=False)
    assert opts[0]["icon"] == "initial:N" and not opts[0]["claude"]
    opts = shots.shot_options(dict(shot, who=None))
    assert [o["label"] for o in opts] == ["→ other"] and opts[0]["shot"]["act"] == ("pick",)


def test_picker_pages_exclude_and_wrap():
    seen = []
    for page in range(3):
        opts, keys = shots.picker_options(TABLE, page, exclude="atlas")
        assert len(opts) <= 4 and keys[1] == "more" and opts[-1]["label"].startswith("More ")
        seen += [v for k, v in keys.items() if v != "more"]
    assert sorted(seen) == sorted(s for s in TABLE if s != "atlas")
    assert shots.picker_options(TABLE, 3, exclude="atlas")[1] == shots.picker_options(TABLE, 0, exclude="atlas")[1]
    small = {k: TABLE[k] for k in ("atlas", "nova")}
    opts, keys = shots.picker_options(small, 0)
    assert keys == {2: "atlas", 3: "nova"} and opts[0]["icon"] == "initial:A"   # W21: no "More": fn empty


# ---- tier 1 payload ---------------------------------------------------------------------------------------

def test_context_and_tier1_message():
    ctx = shots.context_of({"app": "Safari", "url": "github.com/pulls", "space": 2, "title": "Secret title"})
    assert ctx == {"app": "Safari", "host": "github.com", "desk": 2}
    m = shots.message(ctx, WHEN, None, "(image: /x/inbox/a.png)")
    assert m == "[Screenshot from the user, 18:13, context: Safari · github.com · desk 2] (image: /x/inbox/a.png)"
    m = shots.message({"app": None, "host": None, "desk": None}, WHEN, "line one\n\nline two  ", "(image: p)")
    assert m == "[Screenshot from the user, 18:13] line one / line two (image: p)"
    assert "\n" not in shots.message(ctx, WHEN, "a\nb\nc", "(image: p)")


# ---- tier 2 prompt -----------------------------------------------------------------------------------

def test_recent_actions_and_note_prompt():
    rows = [row(900, "activate:Old"), row(120, "activate:Safari"), row(100, "title:Safari/private-title"),
            row(90, "url:github.com/pulls"), row(60, "key:⌘C"), row(30, "space:+1"), row(10, "hud:shown")]
    acts = shots.recent_actions(rows, NOW)
    assert acts == ["18:11 activate:Safari", "18:11 url:github.com/pulls", "18:12 key:⌘C", "18:12 space:+1"]
    p = shots.note_prompt("Atlas", {"app": "Safari", "host": "github.com", "desk": 2}, WHEN, acts, "Error 502 Bad Gateway")
    assert "Atlas" in p and "Error 502 Bad Gateway" in p and "url:github.com/pulls" in p and "18:13" in p
    assert "private-title" not in p and "{{" not in p
    assert "(no text read on the image)" in shots.note_prompt("Atlas", {}, WHEN, [], "")


def test_note_lines_cleaning():
    assert shots.note_lines({"lines": [" a  b ", "", 3, "c", "d", "e", "f", "g"]}) == "a b\nc\nd\ne\nf"
    assert shots.note_lines(None) == "" and shots.note_lines({"lines": "x"}) == ""


# ---- delivery ------------------------------------------------------------------------------------------

def test_deliver_dry_run_inject_and_a2a(png):
    out = io.StringIO()
    r = shots.deliver("atlas", str(png), "what\nwhy", ctx={"app": "Telegram", "host": None, "desk": 1}, when=WHEN,
                      dry_run=True, table=TABLE, out=out)
    assert r == {"ok": True, "to": "atlas", "name": "Atlas", "dry_run": True}
    plan = json.loads(out.getvalue())
    assert plan["kind"] == "inject" and plan["append_to"] == os.path.expanduser("~/.claude/channels/atlas/inject")
    src, dst = plan["copy"]
    assert src == str(png) and dst.startswith(os.path.expanduser("~/.claude/channels/atlas/inbox/oi-shot-20261020T161300Z-"))
    assert dst.endswith(".png") and plan["line"].endswith(f"(image: {dst})") and "what / why" in plan["line"]
    out = io.StringIO()
    r = shots.deliver("nova", str(png), None, ctx={}, when=WHEN, dry_run=True, table=TABLE, out=out)
    plan = json.loads(out.getvalue())
    assert r["ok"] and plan["kind"] == "a2a" and plan["argv"] == vocal.command(TABLE["nova"])
    assert plan["stdin"].endswith(f"(image on the user's Mac at {png})") and "GH_TOKEN" in plan["env_unset"]
    assert shots.deliver("ghost", str(png), None, ctx={}, when=WHEN, dry_run=True, table=TABLE)["error"]
    assert shots.deliver("atlas", "/nope.png", None, ctx={}, when=WHEN, dry_run=True, table=TABLE)["error"] == "image missing"


def test_deliver_inject_into_a_temp_channel(tmp_path, png):
    chan = tmp_path / "chan"
    table = {"atlas": {"name": "Atlas", "kind": "inject", "path": str(chan / "inject")}}
    r = shots.deliver("atlas", str(png), None, ctx={}, when=WHEN, table=table)
    assert r["error"] == "no live session (inject file missing)" and not (chan / "inbox").exists()
    chan.mkdir()
    (chan / "inject").write_text("")
    r = shots.deliver("atlas", str(png), "note", ctx={"app": "Telegram"}, when=WHEN, table=table)
    assert r["ok"]
    [copied] = list((chan / "inbox").iterdir())
    assert copied.read_bytes() == png.read_bytes() and oct(copied.stat().st_mode & 0o777) == "0o600"
    line = (chan / "inject").read_text()
    assert line.count("\n") == 1 and line.strip().endswith(f"(image: {copied})") and "note" in line


def test_deliver_a2a_runner_gets_the_line_on_stdin(png):
    calls = []
    r = shots.deliver("orion", str(png), "n", ctx={}, when=WHEN, table=TABLE,
                      runner=lambda argv, data: calls.append((argv, data)) or (0, ""))
    assert r["ok"] and calls[0][0] == vocal.command(TABLE["orion"]) and str(png) not in " ".join(calls[0][0])
    assert calls[0][1].decode().endswith(f"(image on the user's Mac at {png})\n")


def test_shot_send_cli_dry_run(png, capsys, data_dir):
    from oi import cli
    note = png.parent / "note.txt"
    note.write_text("hello")
    rc = cli.main(["shot-send", "--to", "juno", "--image", str(png), "--note-file", str(note), "--dry-run"])
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 0 and json.loads(out[0])["append_to"] == os.path.expanduser("~/.claude/channels/juno/inject")
    assert json.loads(out[-1])["dry_run"] is True
    logged = normalise.read_hud_log(datetime.now(timezone.utc).date().isoformat())
    assert logged[-1]["kind"] == "shot" and "hello" not in json.dumps(logged)


# ---- the live screenshot (context is state; fake HUD, fake OCR, fake claude) -------------------------

class FakeLive:
    dry_run = False
    socket_path = Path("/nonexistent.sock")

    def __init__(self, rows=None, ctx=None):
        self.rows = rows or []
        self._ctx = ctx if ctx is not None else {"app": "Telegram", "space": 1}
        self.states = []

    def current_moment(self):
        return {"context": self._ctx}

    def on_live_state(self, what, shot):
        self.states.append(what)


class FakeHUD:
    """Scripted replies per command; records every payload."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent = []

    def __call__(self, payload, accept, timeout):
        self.sent.append(payload)
        return self.replies.pop(0) if self.replies else {"event": "dropped", "reason": "no_reply"}


def make(hud=None, *, rows=None, ctx=None, ocr_text="Error 502", claude_out=None, claude_exc=False, delivered=None,
         now=NOW, settings_raw=None, actions=False, suggest=None, runner=None):
    delivered = delivered if delivered is not None else []

    def fake_claude(prompt, model):
        fake_claude.prompts.append((prompt, model))
        if claude_exc:
            raise claude_actions.ClaudeUnavailable("timeout")
        return claude_out if claude_out is not None else {"lines": ["Shows a 502", "Alex was deploying", "Can you check?"]}
    fake_claude.prompts = []

    def fake_deliver(to, image, note, **kw):
        delivered.append({"to": to, "note": note, "dry_run": kw["dry_run"]})
        return {"ok": True, "to": to, "dry_run": kw["dry_run"]}
    so = shots.Shots(FakeLive(rows, ctx), request=hud or FakeHUD(), buckets=lambda: [], fetch=lambda b, s: [],
                     ocr=lambda image: (ocr_text, 12), claude=fake_claude, deliver_fn=fake_deliver,
                     raw_settings=lambda: settings_raw or {}, clock=lambda: now, out=io.StringIO(),
                     actions=actions, suggest=suggest, runner=runner or (lambda argv, data: (0, "")))
    return so, delivered, fake_claude


def shot_ev(png, sec_before=1.0, i=7, src="file"):
    return {"id": i, "timestamp": vocab.iso_utc(datetime.fromtimestamp(NOW - sec_before, timezone.utc)), "duration": 0,
            "data": {"kind": "shot", "src": src, "path": str(png)}}


def state_ev(state, src="thumbnail", sec_before=1.5, i=6):
    return {"id": i, "timestamp": vocab.iso_utc(datetime.fromtimestamp(NOW - sec_before, timezone.utc)), "duration": 0,
            "data": {"kind": "shot_state", "state": state, "src": src}}


def live_shot(so, png, **kw):
    """The thumbnail appears, the image lands while it is up: one live screenshot (its content thread joined)."""
    so.apply(state_ev("live"))
    s = so.apply(shot_ev(png, **kw))
    assert s is not None
    for t in so.threads:
        t.join(5)
    return s


def option(so, label):
    return next(o for o in so.options() if o["label"] == label)


def press(so, label, **reply):
    return so.press(option(so, label), {"event": "pressed", "key": 2, "via": "key", **reply})


def rows_of(kind="shot", event=None):
    return [r for r in normalise.read_hud_log(WHEN.date().isoformat())
            if r.get("kind") == kind and (event is None or r.get("event") == event)]


def test_a_screenshot_is_in_the_context_exactly_while_its_thumbnail_is_up(png, data_dir):
    so, _, _ = make(rows=[row(120, "title:Telegram/atlas-r-d")])
    assert so.live_set() == set() and so.options() == []
    so.apply(state_ev("live"))
    assert so.live_set() == {"shot"} and so.options() == []          # live, its image not landed yet: no option
    s = so.apply(shot_ev(png))
    assert s["window"] and so.live.states == ["appear"]
    opts = so.options()
    assert [o["label"] for o in opts] == ["→ Atlas", "→ other", "Annotate"]
    assert all(o["live"] == "shot" and o["outward"] and o["primitive"].startswith("shot.") for o in opts)
    assert opts[0]["claude"] is True and opts[0]["token"] == "shot.send:atlas"
    assert so.token_rows() == [shots.token_row(shot_ev(png))] and so.token_rows()[0]["token"] == "shot:new"
    so.on_context([row(0, "activate:Safari")])                         # The user switches app: the thumbnail is still up
    assert so.live_set() == {"shot"}
    so.offered(opts[:2])
    so.apply(state_ev("gone", sec_before=0))                           # dragged out / clicked / dismissed / saved
    assert so.live_set() == set() and so.options() == [] and so.token_rows() == []
    assert so.live.states == ["appear", "gone"]
    gone, = rows_of(event="gone")
    assert gone["reason"] == "window_gone" and gone["slots"] == ["pick", "send"] and gone["to"] == "atlas"


def test_clipboard_shot_is_live_until_the_clipboard_changes(png, data_dir):
    so, _, _ = make(rows=[row(30, "activate:Safari")])
    so.apply(shot_ev(png, src="clipboard"))
    so.on_context([row(30, "activate:Safari"), row(0.5, "key:⌘V")])   # same app / page / desk: still live
    assert so.live_set() == {"shot"}
    so.apply(state_ev("gone", src="clipboard", sec_before=0))
    assert so.live_set() == set() and rows_of(event="gone")[0]["reason"] == "clipboard_changed"


def test_clipboard_shot_ends_when_the_context_changes(png, data_dir):
    """a copied screenshot stayed live 1h40; it ends on the next app / page / desk too."""
    so, _, _ = make(rows=[row(30, "activate:Safari")])
    so.apply(shot_ev(png, src="clipboard"))
    so.on_context([row(30, "activate:Safari"), row(0, "activate:Telegram")])
    assert so.live_set() == set() and so.options() == []
    gone, = rows_of(event="gone")
    assert gone["reason"] == "context_changed" and gone["src"] == "clipboard"


def test_clipboard_shot_does_not_end_while_a_thumbnail_window_is_up(png, data_dir):
    so, _, _ = make(rows=[row(30, "activate:Safari")])
    so.apply(state_ev("live"))                                         # a thumbnail / markup window on screen
    so.apply(shot_ev(png, src="clipboard"))
    so.on_context([row(30, "activate:Safari"), row(0, "activate:Telegram")])
    assert so.live_set() == {"shot"} and so.options() and rows_of(event="gone") == []


def test_a_shot_without_window_lives_until_the_context_changes(png, data_dir):
    so, _, _ = make(rows=[row(30, "activate:Safari")])
    so.apply(shot_ev(png))                                             # thumbnail off: the file, no window
    so.on_context([row(30, "activate:Safari"), row(0.5, "key:⌘⇧4")])  # no new app / page / desk
    assert so.live_set() == {"shot"}
    so.on_context([row(30, "activate:Safari"), row(0, "url:github.com/pulls")])
    assert so.live_set() == set() and rows_of(event="gone")[0]["reason"] == "context_changed"


def test_the_thumbnail_seen_after_the_file_binds_the_shot_to_it(png, data_dir):
    so, _, _ = make()
    so.apply(shot_ev(png))
    so.apply(state_ev("live", sec_before=0.5))
    so.on_context([row(0, "activate:Mail")])
    assert so.live_set() == {"shot"}                                   # the window decides now, not the context
    so.apply(state_ev("gone", sec_before=0))
    assert so.live_set() == set()


def test_tap_is_tier1_to_the_current_contact(png, data_dir):
    so, delivered, fc = make(rows=[row(120, "title:Telegram/atlas-r-d")])
    live_shot(so, png)
    assert press(so, "→ Atlas") == "shot.send:atlas"
    assert delivered == [{"to": "atlas", "note": None, "dry_run": False}] and not fc.prompts
    logged, = rows_of(event="press")
    assert logged["tier"] == 1 and logged["via"] == "telegram" and logged["ok"] and logged["picked"] == "send"
    assert logged["live"] is True


def test_long_press_is_tier2_ocr_sonnet_editable_note(png, data_dir):
    hud = FakeHUD({"event": "ok"}, {"event": "note", "text": "Shows a 502\nplease check"})
    so, delivered, fc = make(hud, rows=[row(60, "vocal:nova")])
    live_shot(so, png)
    press(so, "→ Nova", mode="claude")
    (prompt, model), = fc.prompts
    assert model == "sonnet" and "Error 502" in prompt and "Nova" in prompt
    assert hud.sent[0] == {"cmd": "result", "key": 2, "ok": True, "text": "", "ttl_ms": 1000}
    assert hud.sent[1]["cmd"] == "shot_note" and hud.sent[1]["text"] == "Shows a 502\nAlex was deploying\nCan you check?"
    assert delivered == [{"to": "nova", "note": "Shows a 502\nplease check", "dry_run": False}]
    logged = rows_of(event="press")[-1]
    text = json.dumps(logged)
    assert logged["tier"] == 2 and logged["claude"]["model"] == "sonnet" and logged["ocr_ms"] == 12
    assert "502" not in text and "please" not in text                       # no OCR text, no note in the log


def test_tier2_claude_down_still_offers_the_panel(png, data_dir):
    hud = FakeHUD({"event": "ok"}, {"event": "note", "text": ""})
    so, delivered, _ = make(hud, claude_exc=True)
    live_shot(so, png)
    press(so, "→ Atlas", mode="claude")
    assert hud.sent[0]["ok"] is False and hud.sent[1]["text"] == "" and "unavailable" in hud.sent[1]["placeholder"]
    assert delivered[0]["to"] == "atlas"                                     # fallback contact (settings default)


def test_tier2_esc_while_working_or_in_the_panel_sends_nothing(png, data_dir):
    hud = FakeHUD({"event": "dropped", "reason": "cancelled"})
    so, delivered, _ = make(hud)
    live_shot(so, png)
    assert press(so, "→ Atlas", mode="claude") is None and delivered == [] and len(hud.sent) == 1
    hud = FakeHUD({"event": "ok"}, {"event": "dismissed", "reason": "esc"})
    so, delivered, _ = make(hud)
    live_shot(so, png)
    press(so, "→ Atlas", mode="claude")
    assert delivered == [] and rows_of(event="press")[-1]["outcome"].endswith(":note_cancelled")


def test_tier2_local_only_host_skips_claude_and_ocr(png, data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"claude": {"local_only_domains": ["client.example"]}}))
    hud = FakeHUD({"event": "ok"}, {"event": "note", "text": "x"})
    so, delivered, fc = make(hud, ctx={"app": "Safari", "url": "app.client.example/doc"})
    calls = []
    so.ocr = lambda image: calls.append(image) or ("t", 1)
    live_shot(so, png)
    press(so, "→ Atlas", mode="claude")
    assert not fc.prompts and not calls and "Local-only" in hud.sent[1]["placeholder"] and delivered[0]["note"] == "x"


def test_a_send_carries_the_newest_live_shot(png, tmp_path, data_dir):
    """Annotate pressed on the cross of an old clipboard screenshot, a file screenshot lands while the
    note panel is up: the new one goes (11:27 sent the 09:45 image)."""
    newer = tmp_path / "Screenshot 2026-10-20 at 18.14.00.png"
    newer.write_bytes(png.read_bytes())
    clock = [NOW - 60]
    sent = []

    def hud(payload, accept, timeout):                                 # the file screenshot lands while typing
        clock[0] = NOW
        so.apply(shot_ev(newer, sec_before=0.5, i=9))
        return {"event": "note", "text": "this one"}
    so, _, _ = make(hud)
    so.clock = lambda: clock[0]
    so.deliver_fn = lambda to, image, note, **kw: sent.append((to, image, note)) or {"ok": True, "to": to}
    old = so.apply(shot_ev(png, sec_before=60, src="clipboard"))
    ann = option(so, "Annotate")
    assert ann["shot"]["id"] == old["id"]                               # bound to the only live shot when shown
    so.press(ann, {"event": "pressed", "key": 4, "via": "click"})
    assert sent == [("atlas", str(newer), "this one")]
    logged = rows_of(event="press")[-1]
    assert logged["ok"] and logged["newer"] is True and logged["src"] == "file"
    assert option(so, "Annotate")["shot"]["id"] != old["id"]             # the next cross is the newest one's


def test_a_send_keeps_its_shot_when_none_newer_is_live(png, data_dir):
    so, _, _ = make()
    sent = []
    so.deliver_fn = lambda to, image, note, **kw: sent.append(image) or {"ok": True, "to": to}
    live_shot(so, png)
    press(so, "→ Atlas")
    assert sent == [str(png)] and "newer" not in rows_of(event="press")[-1]


def test_other_opens_the_picker_with_paging(png, data_dir):
    hud = FakeHUD({"event": "pressed", "key": 1}, {"event": "pressed", "key": 3})
    so, delivered, _ = make(hud, rows=[row(60, "title:Telegram/atlas-r-d")])
    live_shot(so, png)
    press(so, "→ other")
    page2 = shots.picker_options(TABLE, 1, exclude="atlas")[1]
    assert [p["cmd"] for p in hud.sent] == ["shot_show"] * 2
    assert delivered == [{"to": page2[3], "note": None, "dry_run": False}]   # W21: ⌥ = 3, the 2nd agent
    assert rows_of(event="press")[-1]["via"] == "picked"


def test_annotate_sends_the_typed_text_and_cancel_sends_nothing(png, data_dir):
    hud = FakeHUD({"event": "note", "text": "look at the total"})
    so, delivered, _ = make(hud)
    live_shot(so, png)
    press(so, "Annotate")
    assert hud.sent[0]["cmd"] == "shot_note" and hud.sent[0]["text"] == ""
    assert delivered == [{"to": "atlas", "note": "look at the total", "dry_run": False}]
    so, delivered, _ = make(FakeHUD({"event": "dismissed", "reason": "esc"}))
    live_shot(so, png)
    press(so, "Annotate")
    assert delivered == []


def test_not_in_the_context_when_paused_disabled_denylisted_or_missing(png, data_dir):
    so, _, _ = make(ctx={"app": "1Password"})
    assert so.apply(shot_ev(png)) is None and so.options() == []
    so, _, _ = make(settings_raw={"shots": {"enabled": False}})
    assert so.apply(shot_ev(png)) is None
    so, _, _ = make()
    assert so.apply({"data": {"kind": "shot", "path": "/nope.png"}}) is None
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"paused": True}))
    assert so.apply(shot_ev(png)) is None and so.live.states == []


def test_env_dry_run_reaches_delivery(png, data_dir, monkeypatch):
    monkeypatch.setenv("OI_SHOT_DRY_RUN", "1")
    so, delivered, _ = make()
    live_shot(so, png)
    press(so, "→ Atlas")
    assert delivered[0]["dry_run"] is True


def test_a_sent_shot_makes_the_next_contact(png, data_dir):
    vocal.log_row({"ts": vocab.iso_utc(datetime.fromtimestamp(NOW - 30, timezone.utc)), "kind": "shot", "to": "lyra",
                   "ok": True, "dry_run": False})
    so, delivered, _ = make(rows=[row(120, "title:Telegram/atlas-r-d")])
    live_shot(so, png)
    assert so.options()[0]["label"] == "→ Lyra"
    press(so, "→ Lyra")
    assert delivered[0]["to"] == "lyra"


def test_new_events_once_and_only_the_shot_bucket(png):
    evs = [state_ev("live"), shot_ev(png, 1), dict(shot_ev(png, 0.5), id=9)]
    so, _, _ = make()
    so.buckets_fn = lambda: ["aw-watcher-window_h", "oi-watcher-shot_h"]
    fetched = []
    so.fetch = lambda b, since: fetched.append((b, since)) or evs
    assert [e["id"] for e in so.new_events()] == [6, 7, 9]
    assert so.new_events() == [] and {b for b, _ in fetched} == {"oi-watcher-shot_h"}


def test_live_owns_the_shots(data_dir):
    lv = live.Live(pull=False, sender=lambda *a: None)
    assert isinstance(lv.shots, shots.Shots) and lv.shots.live is lv


# ---- OCR CLI (on-device Vision): runs when oi-ocr is built (ocr/: swift build -c release) ---------------

OCR_BIN = os.environ.get("OI_OCR_BIN") or str(Path(__file__).resolve().parents[1] / "ocr/.build/release/oi-ocr")


@pytest.mark.skipif(not os.path.isfile(OCR_BIN), reason="oi-ocr not built")
def test_ocr_cli_reads_a_generated_png(tmp_path):
    p = tmp_path / "t.png"
    subprocess.run([OCR_BIN, "--render", "Invoice 4217 overdue", str(p)], check=True, timeout=20)
    text, ms = shots.run_ocr(str(p), OCR_BIN)
    assert "4217" in text and "overdue" in text.lower() and ms > 0
    assert shots.run_ocr(str(tmp_path / "missing.png"), OCR_BIN)[0] == ""
    assert shots.run_ocr(str(p), None) == ("", 0)


# ---- content actions (fake OCR, fake Claude): they join the live options when Claude answers ---------

import threading  # noqa: E402

SCREEN = ("Invitation: Demo Globex x Acme — jeudi 22 octobre 2026 14:00-15:00\n"
          "Lien: https://meet.example.com/abc-def\nFrom: paul.martin@example.com\n"
          "IBAN FR76 3000 6000 0112 3456 7890 189\nCan we also check the fund NAV for October?")
GOOD = {"actions": [
    {"type": "calendar.event", "label": "Add Demo Thu 14:00", "title": "Demo Globex x Acme", "start": "2026-10-22T14:00",
     "minutes": 60, "location": "https://meet.example.com/abc-def"},
    {"type": "url.open", "label": "Open meet link", "url": "https://meet.example.com/abc-def"},
    {"type": "agent.send", "label": "Ask Orion about NAV", "to": "orion", "message": "Paul asks for the October NAV: can you send it?"},
    {"type": "copy", "label": "Copy IBAN", "text": "FR76 3000 6000 0112 3456 7890 189"}]}


def fake_suggest(out=GOOD, *, exc=False, wait=None):
    def f(prompt, model):
        f.prompts.append((prompt, model))
        if wait is not None:
            wait.wait(5)
        if exc:
            raise claude_actions.ClaudeUnavailable("timeout")
        return out
    f.prompts = []
    return f


def test_content_actions_join_the_live_options_when_claude_answers(png, data_dir):
    ran = []
    sg = fake_suggest()
    so, delivered, fc = make(actions=True, ocr_text=SCREEN, suggest=sg,
                             runner=lambda argv, data: ran.append((argv, data)) or (0, ""))
    live_shot(so, png)
    assert so.live.states == ["appear", "content"]                     # Live redraws the cross still up
    opts = so.options()
    assert [o["label"] for o in opts] == ["→ Atlas", "Add Demo Thu 14:00", "Open meet link", "Ask Orion about NAV",
                                          "→ other", "Annotate"]
    assert opts[3]["icon"] == "initial:O" and opts[3]["outward"] and not opts[1]["outward"]
    assert [o["p_markov"] for o in opts] == [1.0, 0.95, 0.95, 0.95, 0.5, 0.4]
    assert press(so, "Open meet link") == "shot.content:url.open"
    assert ran == [(["open", "-a", "Safari", "https://meet.example.com/abc-def"], b"")] and delivered == []
    (prompt, model), = sg.prompts
    assert model == "sonnet" and "IBAN FR76" in prompt and "Atlas" in prompt and not fc.prompts
    r = rows_of(event="press")[-1]
    assert r["picked"] == "url.open" and r["content"] == {"type": "url.open", "slot": 1} and r["ok"] is True
    so.offered(opts[:4])
    so.apply(state_ev("gone", sec_before=0))
    g = rows_of(event="gone")[-1]
    assert g["slots"] == ["agent.send", "calendar.event", "send", "url.open"]
    assert g["suggest"]["ok"] and g["suggest"]["n"] == 3 and g["suggest"]["ocr_ms"] == 12
    assert "meet.example" not in json.dumps(rows_of()) and "FR76" not in json.dumps(rows_of())   # no OCR / action text


def test_agent_send_opens_the_prefilled_note_then_delivers(png, data_dir):
    hud = FakeHUD({"event": "note", "text": "Paul asks for the October NAV"})
    so, delivered, _ = make(hud, actions=True, ocr_text=SCREEN, suggest=fake_suggest())
    live_shot(so, png)
    press(so, "Ask Orion about NAV")
    note = hud.sent[0]
    assert note["title"] == "→ Orion · Claude note" and note["text"].startswith("Paul asks for the October NAV")
    assert delivered == [{"to": "orion", "note": "Paul asks for the October NAV", "dry_run": False}]
    assert rows_of(event="press")[-1]["picked"] == "agent.send" and rows_of(event="press")[-1]["to"] == "orion"


def test_claude_down_or_empty_leaves_the_send_options(png, data_dir):
    for sg in (fake_suggest(exc=True), fake_suggest({"actions": []}), fake_suggest({"lines": ["x"]})):
        so, _, _ = make(actions=True, ocr_text=SCREEN, suggest=sg)
        s = live_shot(so, png)
        assert [o["label"] for o in so.options()] == ["→ Atlas", "→ other", "Annotate"]
        assert so.live.states == ["appear"] and s["suggest"]["ok"] is False


def test_an_answer_after_the_shot_is_gone_is_dropped_and_logged_late(png, data_dir):
    release = threading.Event()
    so, _, _ = make(actions=True, ocr_text=SCREEN, suggest=fake_suggest(wait=release))
    so.apply(state_ev("live"))
    s = so.apply(shot_ev(png))
    so.apply(state_ev("gone", sec_before=0))
    release.set()
    for t in so.threads:
        t.join(5)
    assert s["suggest"]["late"] is True and s["content"] is None and so.live.states == ["appear", "gone"]


def test_privacy_gates_skip_claude(png, data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"claude": {"local_only_domains": ["example.com"]}}))
    for ctx, why in (({"app": "Safari", "url": "intranet.example.com/x"}, "local_only"), ({"app": "Mail"}, "local_only_text")):
        sg = fake_suggest()
        so, _, _ = make(actions=True, ocr_text=SCREEN, suggest=sg, ctx=ctx)
        s = live_shot(so, png)
        assert sg.prompts == [] and s["suggest"]["skipped"] == why
    so, _, _ = make(actions=True, ocr_text="", suggest=sg)
    assert live_shot(so, png)["suggest"]["skipped"] == "no_text"


def test_shots_actions_setting_off_means_no_call(png, data_dir):
    sg = fake_suggest()
    so, _, _ = make(actions=None, ocr_text=SCREEN, suggest=sg, settings_raw={"shots": {"actions": False}})
    s = live_shot(so, png)
    assert sg.prompts == [] and s["suggest"] is None
    assert shots.shot_settings({"shots": {"actions": False}})["actions"] is False


def test_clean_actions_only_keeps_what_is_on_the_screen(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "deck-q3.pdf").write_bytes(b"%PDF")
    screen = SCREEN + "\nSaved to ~/Documents/deck-q3.pdf"
    out = {"actions": [
        {"type": "url.open", "label": "Open", "url": "https://evil.example.org/x"},          # not on screen
        {"type": "copy", "label": "Copy", "text": "FR76 9999"},                                # not on screen
        {"type": "agent.send", "to": "atlas", "message": "hi"},                                # already the send slot
        {"type": "agent.send", "to": "nobody", "message": "hi"},                              # unknown agent
        {"type": "calendar.event", "title": "Demo", "start": "next thursday"},                # bad date
        {"type": "mail.draft", "to": "ghost@nowhere.io", "subject": "Re"},                    # address not on screen
        {"type": "shell.run", "cmd": "rm -rf ~"},                                              # unknown type
        {"type": "file.open", "label": "Open deck", "path": "~/Documents/deck-q3.pdf"},
        {"type": "mail.draft", "label": "Reply to Paul, a very long label", "to": "paul.martin@example.com",
         "subject": "Re: Demo", "body": "Bonjour Paul,\nOui pour jeudi.\nAlex"},
        {"type": "url.open", "url": "meet.example.com/abc-def?token=secret"},
        {"type": "copy", "text": "nope"}]}
    got = shots.clean_actions(out, screen, TABLE, exclude="atlas", now=NOW)
    assert [(g["type"], g["label"]) for g in got] == [
        ("file.open", "Open deck"), ("mail.draft", "Reply to Paul, a very"), ("url.open", "Open link")]
    assert got[0]["params"]["path"] == str(tmp_path / "Documents" / "deck-q3.pdf")
    assert got[2]["params"]["url"] == "https://meet.example.com/abc-def"                  # query dropped
    assert shots.clean_actions(GOOD, SCREEN, TABLE, exclude="orion", now=NOW)[2]["type"] == "copy"
    assert shots.clean_actions(None, SCREEN, TABLE) == [] and shots.clean_actions({"actions": "x"}, SCREEN, TABLE) == []


def test_action_commands_mail_calendar_copy(tmp_path):
    written = []

    def writer(text):
        written.append(text)
        return str(tmp_path / "e.ics")
    mail = {"type": "mail.draft", "params": {"to": "paul.martin@example.com", "subject": "Re: Demo", "body": "Oui, jeudi"}}
    argv, _ = shots.action_command(mail, NOW)
    assert argv == ["open", "mailto:paul.martin@example.com?subject=Re%3A%20Demo&body=Oui%2C%20jeudi"]
    cal = {"type": "calendar.event", "params": {"title": "Demo, Acme", "start": "2026-10-22T14:00", "minutes": 45, "location": ""}}
    assert shots.action_command(cal, NOW)[0][:3] == ["open", "-a", "Calendar"]
    argv, _ = shots.action_command(cal, NOW, writer)
    assert argv[-1] == str(tmp_path / "e.ics")
    ics = written[0]
    assert "DTSTART:20261022T120000Z" in ics and "DTEND:20261022T124500Z" in ics and "SUMMARY:Demo\\, Acme" in ics
    assert shots.action_command({"type": "copy", "params": {"text": "FR76 3000"}}, NOW) == (["pbcopy"], b"FR76 3000")
    p = shots.write_ics("BEGIN:VCALENDAR\r\n")
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    shutil.rmtree(os.path.dirname(p))


def test_content_action_dry_run_prints_and_runs_nothing(png, data_dir, monkeypatch):
    monkeypatch.setenv("OI_SHOT_DRY_RUN", "1")
    ran = []
    so, _, _ = make(actions=True, ocr_text=SCREEN, suggest=fake_suggest(),
                    runner=lambda argv, data: ran.append(argv) or (0, ""))
    live_shot(so, png)
    press(so, "Add Demo Thu 14:00")
    assert ran == [] and '"shot_action": "calendar.event"' in so.out.getvalue()
    r = rows_of(event="press")[-1]
    assert r["dry_run"] is True and r["picked"] == "calendar.event"


def test_past_picks_feed_the_next_prompt(png, data_dir):
    for picked in ("url.open", None):          # the rows of the first version (slots + picked on one row)
        vocal.log_row({"ts": vocab.iso_utc(WHEN), "kind": "shot", "slots": ["send", "url.open", "pick"], "picked": picked})
    assert shots.pick_history(NOW) == ("the user's picks on past screenshot offers (last 7 days): send to another agent picked "
                                       "0 of 2; send to the agent picked 0 of 2; url.open picked 1 of 2.")
    sg = fake_suggest({"actions": []})
    so, _, _ = make(actions=True, ocr_text=SCREEN, suggest=sg)
    live_shot(so, png)
    assert "url.open picked 1 of 2" in sg.prompts[0][0]
    press(so, "→ Atlas")                        # a press row (picked) + the gone row (slots offered)
    so.offered(so.options())
    so.apply(state_ev("gone", sec_before=0))
    assert "send to the agent picked 1 of 3" in shots.pick_history(NOW)


# ---- the live screenshot in Live's context (the predictor's ONE pool; fake window list = shot_state) -----

class HoldSender:
    """A HUD that keeps each show up (no reply) unless `reply` is set: records every payload."""

    def __init__(self, reply=None):
        self.reply, self.sent = reply, []

    def __call__(self, payload, on_reply):
        self.sent.append(payload)
        t = threading.Thread(target=on_reply, args=(self.reply,)) if self.reply else threading.Thread(target=lambda: None)
        t.start()
        return t


@pytest.fixture
def no_claude(monkeypatch):
    def blocked(*a, **kw):
        raise claude_actions.ClaudeUnavailable("blocked in tests")
    monkeypatch.setattr(claude_actions, "run_claude", blocked)


def live_with(rows, model, *, reply=None, suggest=None, delivered=None):
    delivered = delivered if delivered is not None else []
    lv = live.Live(pull=False, sender=HoldSender(reply), out=io.StringIO(), resolve_app=lambda a: f"/Applications/{a}.app",
                   is_executable=lambda o: True)
    lv.day, lv.rows, lv.model, lv.clock = rows[-1]["ts"][:10], list(rows), model, (lambda: NOW)
    lv.hud_cmds = []
    lv._hud_cmd = lambda payload: lv.hud_cmds.append(payload) or {"event": "ok"}
    so = lv.shots
    so.raw_settings, so.clock, so.ocr = (lambda: {}), (lambda: NOW), (lambda image: (SCREEN, 9))
    so.actions_on, so.suggest_fn = suggest is not None, suggest

    def fake_deliver(to, image, note, **kw):
        delivered.append({"to": to, "note": note})
        return {"ok": True, "to": to}
    so.deliver_fn = fake_deliver
    return lv, lv.sender


def labels(payload):
    return [o["label"] for o in payload["options"]]


def test_thumbnail_up_summon_gives_the_shot_options_gone_gives_the_normal_ones(rows, model, png, data_dir, no_claude):
    lv, sender = live_with(rows, model)
    normal = labels(lv.summon()["payload"])
    assert normal and not any(x.startswith("→") for x in normal)
    lv.shots.apply(state_ev("live"))                          # the watcher saw the thumbnail (window list)
    lv.shots.apply(shot_ev(png))                              # the image landed while it is up
    first = sender.sent[-1]                                   # shown at once: the full cross, bound to the shot
    assert first["live"] == "shot" and "mode" not in first and labels(first) == ["→ Atlas", "→ other", "Annotate"]
    assert first["ttl_ms"] == shots.SHOW_TTL_MS
    m = lv.current_moment()                                   # the shot is the latest action, before the 2 s pull
    assert m["context"]["live"] == ["shot"] and m["context"]["token"] == "shot:new"
    assert "live: shot (on screen now)" in live.serialise.render(live.forms.route(m["context"]), m)[0]   # Laya
    summoned = lv.summon()["payload"]                         # ⌥⌘ while it is live: the shot's options first
    assert labels(summoned)[0] == "→ Atlas" and summoned["live"] == "shot"
    lv.shots.apply(state_ev("gone", sec_before=0))            # dragged out / dismissed / saved
    assert lv.hud_cmds == [{"cmd": "hide"}]                   # the cross still up for it is taken down
    assert "live" not in (lv.current_moment() or {}).get("context", {})
    after = lv.summon()["payload"]
    assert labels(after) == normal and "live" not in after    # normal context resumes


def test_the_gate_ranks_a_live_shot_first_and_shows_it_full(rows, model, png, data_dir, no_claude, monkeypatch):
    lv, sender = live_with(rows, model)
    lv.shots.apply(shot_ev(png))                              # thumbnail off: live until the context changes
    sender.sent.clear()
    seen = {}

    def gate(menu, nothing_p, cfg, **kw):                     # the same gate, recording what it was given
        seen["hint"] = kw.get("hint")
        return live.summon_options(menu, cfg), "show"
    monkeypatch.setattr(live, "gate", gate)
    assert lv.evaluate(settings.load(), now=NOW)["reason"] == "show"
    p = sender.sent[-1]
    assert seen["hint"] is True and "mode" not in p and p["live"] == "shot" and labels(p)[0] == "→ Atlas"
    opts = [o for o in [{"key": "a", "p_markov": 0.99, "primitive": "app.open", "token": "activate:Mail", "params": {}}]
            + lv.shots.options()]
    assert live.summon_options(opts, settings.GATE_DEFAULTS)[0]["live"] == "shot"


def test_content_actions_redraw_the_cross_still_up(rows, model, png, data_dir, no_claude):
    lv, sender = live_with(rows, model, suggest=fake_suggest())
    lv.shots.apply(state_ev("live"))
    lv.shots.apply(shot_ev(png))
    for t in lv.shots.threads:
        t.join(5)
    first, second = sender.sent[-2:]
    assert labels(first) == ["→ Atlas", "→ other", "Annotate"]
    assert labels(second) == ["→ Atlas", "Add Demo Thu 14:00", "Open meet link"] and second["live"] == "shot"


def test_a_press_on_a_shot_option_goes_through_live(rows, model, png, data_dir, no_claude):
    delivered = []
    lv, sender = live_with(rows, model, reply={"event": "pressed", "key": 2, "via": "key"}, delivered=delivered)
    lv.shots.apply(state_ev("live"))
    lv.shots.apply(shot_ev(png))
    lv.wait_replies(5)
    assert delivered == [{"to": "atlas", "note": None}]
    show = [r for r in normalise.read_hud_log(WHEN.date().isoformat()) if r.get("trigger") == "shot"][-1]
    assert show["executed"] == "shot.send:atlas" and show["options"][0]["outward"] is True


# ---- review: every send from a shot option passes the outward gate once; free model text leak-checked ---

def real_deliver(so, tmp_path, sent):
    """The real shots.deliver (gate included) on inject recipients in tmp_path; the injects / copies recorded."""
    chan = tmp_path / "chan"
    chan.mkdir(exist_ok=True)
    (chan / "inject").write_text("")
    table = {s: {"name": spec["name"], "kind": "inject", "path": str(chan / "inject")} for s, spec in TABLE.items()}

    def fn(to, image, note, **kw):
        kw["table"] = table
        return shots.deliver(to, image, note, inject=lambda path, line: sent.append((to, line)),
                             copy=lambda s, d: None, runner=lambda *a: (_ for _ in ()).throw(AssertionError("a2a")), **kw)
    so.deliver_fn = fn


def picker_reply(exclude="atlas"):
    _, keys = shots.picker_options(TABLE, 0, exclude=exclude)
    return {"event": "pressed", "key": next(k for k, v in keys.items() if v != "more")}


PATHS = {   # label pressed, HUD replies before the gate, extra reply fields
    "send": ("→ Atlas", (), {}),
    "tier2": ("→ Atlas", ({"event": "ok"}, {"event": "note", "text": "regarde"}), {"mode": "claude"}),
    "pick": ("→ other", (picker_reply(),), {}),
    "annotate": ("Annotate", ({"event": "note", "text": "regarde"},), {}),
    "agent.send": ("Ask Orion about NAV", ({"event": "note", "text": "Paul asks for the October NAV"},), {}),
}


@pytest.mark.parametrize("path", list(PATHS))
@pytest.mark.parametrize("answer", ["cancelled", "timeout", "no_hud"])
def test_a_shot_send_not_validated_sends_nothing(path, answer, png, data_dir, tmp_path, gate_asks):
    from oi import gate
    label, replies, extra = PATHS[path]
    sent = []
    so, _, _ = make(FakeHUD(*replies), actions=True, ocr_text=SCREEN, suggest=fake_suggest())
    real_deliver(so, tmp_path, sent)
    live_shot(so, png)
    # no_hud: the real HUD round trip, on a socket nobody listens to (temp OI_DATA_DIR)
    gate_asks.answer = (lambda req: gate.hud_ask(req, 0.5)) if answer == "no_hud" else answer
    assert press(so, label, **extra) is None
    assert sent == [] and len(gate_asks.asked) == 1 and gate_asks.asked[0]["cls"] == "shot"
    r = rows_of(event="press")[-1]
    assert r["ok"] is False and r["error"].startswith("cancelled")
    g, = [x for x in map(json.loads, (data_dir / "gate-log" / f"{datetime.now(timezone.utc).date()}.jsonl")
                         .read_text().splitlines())]
    assert g["decision"] == "cancelled" and g["sent"] is False


@pytest.mark.parametrize("path", list(PATHS))
def test_a_validated_shot_send_is_delivered_after_one_confirmation(path, png, data_dir, tmp_path, gate_asks):
    label, replies, extra = PATHS[path]
    sent = []
    hud = FakeHUD(*replies)
    so, _, _ = make(hud, actions=True, ocr_text=SCREEN, suggest=fake_suggest())
    real_deliver(so, tmp_path, sent)
    live_shot(so, png)
    assert press(so, label, **extra) is not None
    assert len(sent) == 1 and len(gate_asks.asked) == 1                # the gate panel is the one confirmation
    assert not any(p.get("cmd") == "confirm" for p in hud.sent)       # no extra confirm step of our own


def test_content_actions_that_only_open_things_are_not_gated(png, data_dir, gate_asks):
    ran = []
    so, _, _ = make(actions=True, ocr_text=SCREEN, suggest=fake_suggest(),
                    runner=lambda argv, data: ran.append(argv) or (0, ""))
    live_shot(so, png)
    assert press(so, "Open meet link") and ran and gate_asks.asked == []


LEAKY = {"actions": [
    {"type": "agent.send", "label": "Ask Orion", "to": "orion", "message": "Paul from Hexa Conseil asks for the NAV"},
    {"type": "mail.draft", "label": "Reply Paul", "to": "paul.martin@example.com", "subject": "hexaconseil follow-up",
     "body": "Hi Paul, about the NAV"},
    {"type": "calendar.event", "label": "Demo", "title": "Demo Globex", "start": "2026-10-22T14:00",
     "location": "hexa-conseil.example office"}]}


def test_free_model_text_naming_a_local_only_term_is_scrubbed_or_dropped():
    cfg = {"local_only_domains": ["hexa-conseil.example"]}
    out = shots.clean_actions(LEAKY, SCREEN, TABLE, exclude="atlas", now=NOW, cfg=cfg)
    by = {a["type"]: a["params"] for a in out}
    assert "agent.send" not in by                                         # its message was the point: dropped
    assert by["mail.draft"] == {"to": "paul.martin@example.com", "subject": "", "body": "Hi Paul, about the NAV"}
    assert by["calendar.event"]["title"] == "Demo Globex" and by["calendar.event"]["location"] == ""
    assert "hexa" not in json.dumps(out).lower()
    # a draft left with nothing, a leaking calendar title, a leaking label (-> the default label)
    bad = {"actions": [{"type": "mail.draft", "subject": "Hexa Conseil", "body": "hexa-conseil.example"},
                       {"type": "calendar.event", "label": "Hexa call", "title": "Call Hexa", "start": "2026-10-22T14:00"},
                       {"type": "url.open", "label": "Open Hexa meet", "url": "https://meet.example.com/abc-def"}]}
    out = shots.clean_actions(bad, SCREEN, TABLE, now=NOW, cfg=cfg)
    assert [(a["type"], a["label"]) for a in out] == [("url.open", "Open link")]
    # no local-only domain configured: nothing is touched
    assert len(shots.clean_actions(LEAKY, SCREEN, TABLE, exclude="atlas", now=NOW, cfg={})) == 3


def test_suggest_passes_the_claude_settings_to_the_leak_check(png, data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"claude": {"local_only_domains": ["hexa-conseil.example"]}}))
    so, _, _ = make(actions=True, ocr_text=SCREEN, suggest=fake_suggest(LEAKY))
    live_shot(so, png)
    assert [o["label"] for o in so.options()] == ["→ Atlas", "Reply Paul", "Demo", "→ other", "Annotate"]
    assert "hexa" not in json.dumps([s.get("content") for s in so.shots.values()]).lower()


def test_a_tier2_claude_note_naming_a_local_only_term_is_withheld(png, data_dir, monkeypatch):
    monkeypatch.setattr(claude_actions, "claude_settings",
                        lambda: {"enabled": True, "local_only_domains": ["hexa-conseil.example"]})
    hud = FakeHUD({"event": "ok"}, {"event": "note", "text": ""})
    so, delivered, _ = make(hud, claude_out={"lines": ["From Hexa Conseil", "Paul asks for the NAV"]})
    live_shot(so, png)
    press(so, "→ Atlas", mode="claude")
    note = next(p for p in hud.sent if p.get("cmd") == "shot_note")
    assert note["text"] == "" and "withheld" in note["placeholder"] and hud.sent[0]["ok"] is False
