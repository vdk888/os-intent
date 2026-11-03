"""Screenshot -> forward to a teammate with a typed or dictated note: a new screenshot opens the team picker
at once (one tap), then the note panel (type or dictate), Enter = ONE delivery; an agent on another host gets the image
in its channel inbox over ssh. Synthetic data only: fake HUD (scripted replies), fake ssh / A2A sender; the
dictation is oi-hud's (its pure part is tested in hud/Tests, ShotOfferTests): here the HUD's reply says `dictated`."""
import json

import pytest

from oi import shots, vocal
from test_shots import (CROSS, TABLE, WHEN, FakeHUD, fake_suggest, labels, live_with, make, rows_of, shot_ev,
                        state_ev)
from test_shots import png  # noqa: F401  (the fixture)


def key_of(slug, first="atlas", page=0):
    _, keys = shots.picker_options(shots.roster(TABLE, first), page)
    return next(k for k, v in keys.items() if v == slug)


def picked(slug, first="atlas"):
    return {"event": "pressed", "key": key_of(slug, first), "via": "key"}


def shot_of(so, png):
    s = so.apply(shot_ev(png))
    assert s is not None
    return s


# ---- the picker first --------------------------------------------------------------------------------------

def test_settings_default_on_and_switchable():
    d = shots.shot_settings({})
    assert d["picker_first"] is True and d["note_dictation"] is True and d["remote_image"] is True
    off = shots.shot_settings({"shots": {"picker_first": False, "note_dictation": False, "remote_image": False}})
    assert not (off["picker_first"] or off["note_dictation"] or off["remote_image"])
    assert shots.shot_settings({"shots": {"picker_first": "no"}})["picker_first"] is True   # wrongly typed = default


def test_roster_puts_the_contact_first_and_keeps_everyone():
    r = shots.roster(TABLE, "nova")
    assert list(r)[0] == "nova" and sorted(r) == sorted(TABLE)
    assert list(shots.roster(TABLE, None)) == list(TABLE) and list(shots.roster(TABLE, "ghost")) == list(TABLE)


def test_a_new_screenshot_opens_the_team_picker_at_once(rows, model, png, data_dir):
    lv, sender = live_with(rows, model, raw={}, suggest=fake_suggest())
    hud = FakeHUD()                                          # no answer: the picker times out, nothing sent
    lv.shots.request = hud
    lv.shots.apply(state_ev("live"))
    lv.shots.apply(shot_ev(png))
    lv.wait_replies(5)
    for t in lv.shots.threads:
        t.join(5)
    first = hud.sent[0]
    assert first["cmd"] == "shot_show"                      # the picker, not the "→ Atlas / → other / Annotate" cross
    assert labels(first) == ["Atlas", "Juno", "Nova", "More 1/3"]   # the current contact (fallback atlas) first
    assert sender.sent == []                                # Claude's content actions never replace the picker
    assert [p["cmd"] for p in hud.sent] == ["shot_show"]
    r = rows_of(event="press")[-1]
    assert r["action"] == "forward" and r["outcome"] == "forward:dismissed" and r["ok"] is False
    summoned = lv.summon()["payload"]                       # ⌥⌘ afterwards: the cross with the content actions
    assert summoned["live"] == "shot" and "Add Demo Thu 14:00" in labels(summoned)


def test_the_current_contact_is_on_the_first_key(png, data_dir):
    rows = [{"ts": "2026-10-20T16:12:00.000Z", "dur": 0, "token": "vocal:nova", "app": "Telegram",
             "src": "aw-window", "raw_id": None}]
    hud = FakeHUD()
    so, _, _ = make(hud, rows=rows)
    so.forward(shot_of(so, png))
    assert labels(hud.sent[0])[0] == "Nova"


def test_every_agent_is_one_tap_away_through_more(png, data_dir):
    more = {"event": "pressed", "key": 1, "via": "key"}      # fn = "More" (fill order ⌃ ⌥ ⌘ for the agents)
    hud = FakeHUD(more, more, {"event": "pressed", "key": key_of("echo", page=2)}, {"event": "note", "text": ""})
    so, delivered, _ = make(hud)
    so.forward(shot_of(so, png))
    pages = [labels(p) for p in hud.sent if p["cmd"] == "shot_show"]
    assert sorted(x for p in pages for x in p if not x.startswith("More")) == sorted(v["name"] for v in TABLE.values())
    assert delivered == [{"to": "echo", "note": "", "dry_run": False}]


def test_picker_first_off_keeps_the_cross(rows, model, png, data_dir):
    lv, sender = live_with(rows, model, raw=CROSS)
    lv.shots.apply(shot_ev(png))
    assert labels(sender.sent[-1]) == ["→ Atlas", "→ other", "Annotate"]


# ---- the note panel: type, dictate, empty, cancel ---------------------------------------------------------------

def test_type_path_sends_image_and_note_once(png, data_dir):
    hud = FakeHUD(picked("nova"), {"event": "note", "text": "regarde le NAV"})
    so, delivered, _ = make(hud)
    assert so.forward(shot_of(so, png)) == "shot.send:nova"
    note = hud.sent[1]
    assert note["cmd"] == "shot_note" and note["title"] == "→ Nova · your note" and note["text"] == ""
    assert note["dictate"] is True                         # the panel offers dictation (Tab / mic)
    assert delivered == [{"to": "nova", "note": "regarde le NAV", "dry_run": False}]
    r = rows_of(event="press")[-1]
    assert r["ok"] and r["to"] == "nova" and r["via"] == "picked" and r["note_len"] == 14 and "dictated" not in r
    assert "regarde" not in json.dumps(rows_of())          # the note is never logged


def test_dictate_path_marks_the_note_dictated(png, data_dir, tmp_path):
    """The HUD's dictation (mocked: its reply) wrote the note: the delivered line says so."""
    sent = []
    hud = FakeHUD(picked("atlas"), {"event": "note", "text": "le bug du login", "dictated": True})
    so, _, _ = make(hud)
    chan = tmp_path / "chan"
    chan.mkdir()
    (chan / "inject").write_text("")
    table = {"atlas": {"name": "Atlas", "kind": "inject", "path": str(chan / "inject")}}
    so.deliver_fn = lambda to, image, note, **kw: shots.deliver(
        to, image, note, **{**kw, "table": table}, inject=lambda p, line: sent.append(line), copy=lambda s, d: None)
    so.forward(shot_of(so, png))
    assert len(sent) == 1 and ", note dictated]" in sent[0] and "le bug du login" in sent[0]
    assert rows_of(event="press")[-1]["dictated"] is True


def test_note_dictation_setting_off_hides_the_mic(png, data_dir):
    hud = FakeHUD(picked("atlas"), {"event": "note", "text": "x"})
    so, _, _ = make(hud, settings_raw={"shots": {"note_dictation": False}})
    so.forward(shot_of(so, png))
    assert "dictate" not in hud.sent[1]


def test_empty_note_sends_the_image_alone(png, data_dir):
    hud = FakeHUD(picked("juno"), {"event": "note", "text": ""})
    so, delivered, _ = make(hud)
    assert so.forward(shot_of(so, png)) == "shot.send:juno"
    assert delivered == [{"to": "juno", "note": "", "dry_run": False}]
    assert shots.message({}, WHEN, "", "(image: /x.png)", dictated=True) == "[Screenshot from the user, 18:13] (image: /x.png)"


@pytest.mark.parametrize("replies,outcome", [
    (({"event": "dismissed", "reason": "esc"},), "forward:dismissed"),                       # Esc on the picker
    ((picked("nova"), {"event": "dismissed", "reason": "esc"}), "forward:note_cancelled"),   # Esc in the note panel
    ((picked("nova"), {"event": "dismissed", "reason": "click_outside"}), "forward:note_cancelled"),
    ((picked("nova"),), "forward:note_cancelled"),                                           # no HUD answer
])
def test_cancel_sends_nothing(replies, outcome, png, data_dir):
    so, delivered, _ = make(FakeHUD(*replies))
    assert so.forward(shot_of(so, png)) is None
    assert delivered == [] and rows_of(event="press")[-1]["outcome"] == outcome


def test_dry_run_prints_the_picker_and_shows_nothing(png, data_dir):
    hud = FakeHUD()
    so, delivered, _ = make(hud)
    so.live.dry_run = True
    so.forward(shot_of(so, png))
    assert hud.sent == [] and delivered == [] and '"shot_forward"' in so.out.getvalue()


# ---- the image to an agent on another host (mock ssh) --------------------------------------------------------------

class Wire:
    """Records the ssh copy and the A2A line, in order."""

    def __init__(self, copy_rc=0):
        self.calls, self.copy_rc = [], copy_rc

    def copy(self, argv, data):
        self.calls.append(("copy", argv, data))
        return self.copy_rc, "" if self.copy_rc == 0 else "Permission denied"

    def a2a(self, argv, data):
        self.calls.append(("a2a", argv, data))
        return 0, ""


def test_remote_agent_gets_the_image_in_its_inbox_before_the_line(png):
    w = Wire()
    r = shots.deliver("nova", str(png), "regarde", ctx={}, when=WHEN, table=TABLE, runner=w.a2a, remote_copy=w.copy,
                      remote_image=True)
    assert r["ok"] and [c[0] for c in w.calls] == ["copy", "a2a"]
    _, argv, data = w.calls[0]
    assert argv[:7] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", "agent-host"]
    assert data == png.read_bytes() and str(png) not in " ".join(argv)        # the pixels on stdin, never argv
    script = argv[7]
    inner = script
    dst = inner.split("cat > ", 1)[1].split(" ", 1)[0]
    assert dst.startswith("/opt/agents/nova/inbox/oi-shot-20261020T161300Z-")
    assert inner.startswith("umask 077; set -C; mkdir -p ") and inner.endswith(f"cat > {dst} && chmod 600 {dst}")
    line = w.calls[1][2].decode()
    assert line.endswith(f"regarde (image: {dst})\n") and "the user's Mac" not in line
    assert w.calls[1][1] == vocal.command(TABLE["nova"])


@pytest.mark.parametrize("slug,host,chan", [("vega", "studio-host", "/opt/agents/vega/inbox"),
                                            ("echo", "laptop-host", "/opt/agents/echo/inbox")])
def test_other_hosts_get_the_image_in_their_channel_inbox(slug, host, chan, png):
    w = Wire()
    assert shots.deliver(slug, str(png), None, ctx={}, when=WHEN, table=TABLE, runner=w.a2a, remote_copy=w.copy,
                         remote_image=True)["ok"]
    argv = w.calls[0][1]
    assert argv[6] == host and f"cat > {chan}/oi-shot-" in argv[7]
    assert f"(image: {chan}/oi-shot-" in w.calls[1][2].decode()


def test_a_failed_copy_sends_no_line(png):
    w = Wire(copy_rc=255)
    r = shots.deliver("orion", str(png), "n", ctx={}, when=WHEN, table=TABLE, runner=w.a2a, remote_copy=w.copy,
                      remote_image=True)
    assert not r["ok"] and r["error"] == "copy: exit 255: Permission denied" and [c[0] for c in w.calls] == ["copy"]


def test_remote_image_follows_the_setting(png, data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"shots": {"remote_image": False}}))
    w = Wire()
    assert shots.deliver("nova", str(png), None, ctx={}, when=WHEN, table=TABLE, runner=w.a2a, remote_copy=w.copy)["ok"]
    assert [c[0] for c in w.calls] == ["a2a"] and "(image on the user's Mac at" in w.calls[0][2].decode()


def test_tests_never_ssh_for_real(png):
    with pytest.raises(AssertionError, match="real ssh"):
        shots.deliver("nova", str(png), None, ctx={}, when=WHEN, table=TABLE, runner=Wire().a2a, remote_image=True)


# ---- the outward gate stays the one confirmation ---------------------------------------------------------------------

@pytest.mark.parametrize("answer", ["cancelled", "timeout"])
def test_gate_refused_copies_and_sends_nothing(answer, png, data_dir, gate_asks):
    w = Wire()
    gate_asks.answer = answer
    hud = FakeHUD(picked("nova"), {"event": "note", "text": "regarde"})
    so, _, _ = make(hud)
    so.deliver_fn = lambda to, image, note, **kw: shots.deliver(to, image, note, **kw, runner=w.a2a, remote_copy=w.copy)
    assert so.forward(shot_of(so, png)) is None
    assert w.calls == [] and len(gate_asks.asked) == 1 and gate_asks.asked[0]["cls"] == "shot"
    assert "regarde" in gate_asks.asked[0]["preview"][0]
    assert rows_of(event="press")[-1]["error"].startswith("cancelled")


def test_gate_validated_is_the_only_confirmation(png, data_dir, gate_asks):
    w = Wire()
    hud = FakeHUD(picked("nova"), {"event": "note", "text": "regarde", "dictated": True})
    so, _, _ = make(hud)
    so.deliver_fn = lambda to, image, note, **kw: shots.deliver(to, image, note, **kw, runner=w.a2a, remote_copy=w.copy)
    assert so.forward(shot_of(so, png)) == "shot.send:nova"
    assert len(gate_asks.asked) == 1 and [c[0] for c in w.calls] == ["copy", "a2a"]
    assert [p["cmd"] for p in hud.sent] == ["shot_show", "shot_note"]          # picker, note: no confirm of our own
    assert ", note dictated]" in w.calls[1][2].decode()
