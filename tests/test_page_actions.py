"""W16: page actions — detection table, label matching, click tokens, live wiring, press via oi-hud.
Synthetic pages only; no real Safari, no real oi-hud (fake page client, fake unix socket)."""
import io
import json
import socket
import threading

from oi import candidates, forms, live, normalise, page_actions as pa, paths, settings, vocab
from oi.normalise import Ev

GH = "github.com"


def page(*els, host=GH, trusted=True):
    return {"trusted": trusted, "host": host,
            "elements": [{"role": r, "label": l, "slug": vocab.slug(l), "frame": [100.0 + i, 200.0, 140.0, 32.0]}
                         for i, (r, l) in enumerate(els)]}


PR_OPEN = page(("link", "Files changed"), ("button", "Squash and merge"), ("button", "Close pull request"),
               ("button", "Copy"))
SAFARI_CTX = {"app": "Safari", "url": "github.com/octocat"}


# ---- detection table --------------------------------------------------------------------------------

def test_github_rules_best_first_and_capped():
    opts = pa.rule_options(PR_OPEN, GH)
    assert [o["params"]["slug"] for o in opts] == ["squash-and-merge"]          # Close PR: destructive, never offered
    assert all(o["primitive"] == "ui.press" and o["pin"] for o in opts)
    assert opts[0]["token"] == "click:github.com/button:squash-and-merge"
    confirm = page(("button", "Confirm squash and merge"), ("button", "Squash and merge"), ("button", "Delete branch"))
    assert [o["params"]["slug"] for o in pa.rule_options(confirm, GH)] == ["confirm-squash-and-merge", "squash-and-merge"]
    merged = page(("button", "Delete branch"))
    assert pa.rule_options(merged, GH) == []                                      # destructive: never offered


def test_rules_need_the_element_role_and_a_known_site():
    assert pa.rule_options(page(("link", "Squash and merge")), GH) == []          # a link is not the button
    assert pa.rule_options(page(("button", "Squash and merge")), "example.com") == []
    assert pa.rule_options(page(("button", "Squash and merge")), "www.github.com")    # www. folded
    for later in ("mail.google.com", "www.notion.so", "linear.app"):              # tables left for later
        assert later in pa.RULES and pa.RULES[later] == []


def test_irreversible_labels_need_confirm():
    for s in ("squash-and-merge", "delete-branch", "send", "pay-now", "publish", "close-pull-request", "remove"):
        assert pa.irreversible(s), s
    for s in ("approve", "copy", "files-changed", "merged-by", "sender-info", ""):
        assert not pa.irreversible(s), s       # whole words only: "merged", "sender" are not verbs here


# ---- snapshot parsing / label matching ----------------------------------------------------------------

def test_parse_page_keeps_buttons_and_links_only_and_slugs_labels():
    reply = {"event": "page", "trusted": True, "host": "GitHub.com",
             "elements": [{"role": "button", "label": "Squash and merge", "frame": [1, 2, 3, 4]},
                          {"role": "textfield", "label": "secret"}, {"role": "link", "label": ""},
                          {"role": "link", "label": "Écrire", "frame": "bad"}, "junk"]}
    p = pa.parse_page(reply)
    assert p["trusted"] and p["host"] == "github.com"
    assert [(e["role"], e["slug"], e["frame"]) for e in p["elements"]] == [
        ("button", "squash-and-merge", [1.0, 2.0, 3.0, 4.0]), ("link", "ecrire", None)]
    assert pa.parse_page(None)["elements"] == [] and pa.parse_page({"event": "error"})["trusted"] is False
    untrusted = pa.parse_page({"event": "page", "trusted": False, "reason": "not_trusted", "elements": []})
    assert untrusted["elements"] == [] and untrusted["reason"] == "not_trusted"


def test_find_matches_role_and_slug_first_in_page_order():
    p = page(("button", "Squash and merge"), ("button", "Squash  and merge!"))
    assert pa.find(p, "button", "squash-and-merge")["frame"][0] == 100.0
    assert pa.find(p, "link", "squash-and-merge") is None


def test_apply_keeps_learned_press_only_when_on_this_page():
    learned = {"key": "press_button_button_copy", "primitive": "ui.press", "token": "click:github.com/button:copy",
               "params": {"host": GH, "role": "button", "slug": "copy"}, "p_markov": 0.4}
    gone = {**learned, "key": "x", "params": {"host": GH, "role": "button", "slug": "rerun-jobs"}}
    other = {**learned, "key": "y", "params": {"host": "example.com", "role": "button", "slug": "copy"}}
    app = {"key": "switch_app_mail", "primitive": "app.open", "params": {"app": "Mail"}, "p_markov": 0.2}
    out = pa.apply([learned, gone, other, app], PR_OPEN, SAFARI_CTX, with_rules=False)
    assert [o["key"] for o in out] == ["press_button_button_copy", "switch_app_mail"]
    assert out[0]["_frame"] == [103.0, 200.0, 140.0, 32.0]
    # with rules (summon): the key buttons first, a learned duplicate of a rule is not offered twice
    dup = {**learned, "key": "press_button_button_squash_and_merge",
           "params": {"host": GH, "role": "button", "slug": "squash-and-merge"}}
    out = pa.apply([dup, app], PR_OPEN, SAFARI_CTX, with_rules=True)
    assert [o["key"] for o in out] == ["page1_squash_and_merge", "switch_app_mail"]   # Close PR: never offered
    # no snapshot (no Accessibility / oi-hud down) or a snapshot of another host: no ui.press at all
    assert [o["key"] for o in pa.apply([learned, app], None, SAFARI_CTX, with_rules=True)] == ["switch_app_mail"]
    assert [o["key"] for o in pa.apply([learned, app], page(("button", "Copy"), host="example.com"), SAFARI_CTX,
                                       with_rules=True)] == ["switch_app_mail"]


# ---- HUD fields / hint mapping --------------------------------------------------------------------------

def test_hud_fields_label_target_and_element_hint():
    o = pa.apply([], PR_OPEN, SAFARI_CTX, with_rules=True)[0]
    f = pa.hud_fields(o)
    assert f == {"label": "Squash & merge", "icon": "sf:arrow.triangle.merge", "target": "button:squash-and-merge",
                 "hint": {"kind": "element", "rect": [101.0, 200.0, 140.0, 32.0]}}   # no two-tap: the gate confirms
    learned = {"primitive": "ui.press", "params": {"host": GH, "role": "link", "slug": "files-changed"},
               "_label": "Files changed", "_frame": [0, 0, 0, 0]}
    f = pa.hud_fields(learned)
    assert f["label"] == "Files changed" and "confirm" not in f and "hint" not in f   # empty frame: no hint
    payload = live.hud_payload([o], SAFARI_CTX)
    assert payload["options"][0]["key"] == 2 and "confirm" not in payload["options"][0]


# ---- click tokens: watcher rows -> tokens (denylist) -> candidates --------------------------------------

def _ev(i, label, role="button", host="github.com"):
    return Ev.of({"id": i, "timestamp": f"2026-01-05T09:00:{i:02d}+00:00", "duration": 0,
                  "data": {"host": host, "role": role, "label": label}})


def test_click_tokens_and_denylist():
    rows = normalise.click_tokens([_ev(1, "squash-and-merge"), _ev(2, "files-changed", "link"),
                                   _ev(3, "x", role="textfield"), _ev(4, "", host="github.com"),
                                   _ev(5, "copy", host="")])
    assert [r["token"] for r in rows] == ["click:github.com/button:squash-and-merge", "click:github.com/link:files-changed"]
    assert all(r["src"] == "oi-click" and r["app"] == "Safari" and not vocab.validate_row(r) for r in rows)
    assert normalise.click_tokens([_ev(1, "copy")], denylist=frozenset({"safari"})) == []
    raw = {"oi-watcher-click_host-a": [{"id": 1, "timestamp": "2026-01-05T09:00:01+00:00", "duration": 0,
                                        "data": {"host": "github.com", "role": "button", "label": "copy"}}]}
    assert [r["token"] for r in normalise.tokens_from_raw(raw, denylist=frozenset())] == ["click:github.com/button:copy"]
    assert normalise.tokens_from_raw(raw, denylist=frozenset({"Safari"})) == []


def test_click_token_expands_to_form_c_press_button():
    form = forms.route({"app": "Safari"})
    assert not forms.validate(form) and form["_version"] == "safari.next_page@v2"

    class M:
        def top_k(self, moment, k, include_nothing=False):
            return [("click:github.com/button:copy", 0.5)]
    mo = {"context": {"app": "Safari", "url": "github.com/x"}, "history": []}
    menu = candidates.build(form, mo, M(), is_executable=lambda o: True)
    press = [o for o in menu if o["primitive"] == "ui.press"]
    assert press == [{"key": "press_button_button_copy", "criteria": "click copy", "primitive": "ui.press",
                      "params": {"host": "github.com", "role": "button", "slug": "copy"},
                      "token": "click:github.com/button:copy", "p_markov": 0.5}]
    assert pa.click_token("github.com", "button", "Squash and merge") == "click:github.com/button:squash-and-merge"
    assert pa.click_token("github.com", "field", "x") is None


# ---- live wiring ---------------------------------------------------------------------------------------

class FakePage:
    def __init__(self, snap, press_reply=None):
        self.snap, self.press_reply, self.snapshots, self.presses = snap, press_reply or {"ok": True}, 0, []

    def snapshot(self):
        self.snapshots += 1
        return self.snap

    def press(self, params, *, dry_run=False):
        self.presses.append((params, dry_run))
        return self.press_reply


class FakeSender:
    def __init__(self, reply):
        self.reply, self.sent = reply, []

    def __call__(self, payload, on_reply):
        self.sent.append(payload)
        t = threading.Thread(target=on_reply, args=(self.reply,))
        t.start()
        return t


def _safari_rows():
    base = "2026-01-05T09:00:0{}.000Z"
    return [{"ts": base.format(1), "dur": 5, "token": "activate:Safari", "app": "Safari", "src": "aw-window", "raw_id": 1},
            {"ts": base.format(2), "dur": 5, "token": "url:github.com/octocat", "app": "Safari", "src": "oi-safari",
             "raw_id": 2}]


def _live(reply, page_snap, press_reply=None, **kw):
    lv = live.Live(pull=False, sender=FakeSender(reply), runner=lambda a: (0, ""), out=io.StringIO(),
                   resolve_app=lambda a: None, is_executable=lambda o: True, **kw)
    lv.rows, lv.model, lv.day = _safari_rows(), None, "2026-01-05"
    lv.page = FakePage(page_snap, press_reply)
    return lv


def test_summon_on_a_pr_page_offers_squash_and_merge_with_outline_then_presses(gate_asks):
    lv = _live({"event": "pressed", "key": 2, "via": "key"}, PR_OPEN)
    r = lv.summon(now=1000.0)
    o1 = r["payload"]["options"][0]
    assert r["reason"] == "show" and o1["label"] == "Squash & merge" and "confirm" not in o1   # one tap picks
    assert o1["hint"] == {"kind": "element", "rect": [101.0, 200.0, 140.0, 32.0]} and o1["target"] == "button:squash-and-merge"
    lv.wait_replies(5)
    assert lv.page.presses == [({"host": GH, "role": "button", "slug": "squash-and-merge",
                                 "short": "Squash & merge", "icon": "arrow.triangle.merge"}, False)]
    assert [a["target"] for a in gate_asks.asked] == ["button:squash-and-merge"]   # pick -> gate panel -> validate
    row = normalise.read_hud_log(live.now_iso(1000.0)[:10])[-1]
    assert row["executed"] == "click:github.com/button:squash-and-merge" and row["ui_press"] == {"ok": True, "reason": None}
    assert row["options"][0]["hint"] == "element"


def test_stale_page_presses_nothing():
    lv = _live({"event": "pressed", "key": 2}, PR_OPEN, press_reply={"ok": False, "reason": "gone"})
    lv.summon(now=1000.0)
    lv.wait_replies(5)
    row = normalise.read_hud_log(live.now_iso(1000.0)[:10])[-1]
    assert row["executed"] is None and row["ui_press"] == {"ok": False, "reason": "gone"}


def test_dry_run_never_presses_for_real():
    lv = _live({"event": "pressed", "key": 2}, PR_OPEN, press_reply={"ok": True, "dry_run": True}, dry_run=True)
    assert lv.summon(now=1000.0)["reason"] == "dry_run"
    assert lv.page.presses == []          # a dry-run live prints the show, it never gets a press


def test_executor_press_needs_valid_params_and_the_hook():
    opt = {"primitive": "ui.press", "token": "click:github.com/button:copy",
           "params": {"host": GH, "role": "button", "slug": "copy"}}
    calls = []
    ex = live.Executor(runner=lambda a: (0, ""), ui_press=lambda p: calls.append(p) or True)
    assert ex.run(opt, {}, str) == "click:github.com/button:copy" and len(calls) == 1
    assert ex.run({**opt, "params": {"role": "field", "slug": "x"}}, {}, str) is None and len(calls) == 1
    assert live.Executor(runner=lambda a: (0, "")).run(opt, {}, str) is None       # no hook: never pressed


def test_no_page_snapshot_outside_safari():
    lv = _live({"event": "dismissed", "reason": "ttl"}, PR_OPEN)
    lv.rows = [{**_safari_rows()[0], "token": "activate:Mail", "app": "Mail"}]
    assert lv.page_menu([{"primitive": "ui.press", "params": {}}], {"app": "Mail"}, summoned=True) == []
    lv.summon(now=1000.0)
    assert lv.page.snapshots == 0


def test_untrusted_snapshot_offers_no_page_action():
    lv = _live({"event": "dismissed", "reason": "ttl"}, pa.parse_page({"event": "page", "trusted": False, "elements": []}))
    assert lv.summon(now=1000.0)["payload"]["options"] == []


# ---- PageClient over a fake hud.sock -------------------------------------------------------------------

def test_page_client_round_trip():
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="oi-", dir="/tmp")          # AF_UNIX paths are short (104 bytes)
    path = f"{d}/h.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(2)
    got = []

    def serve():
        for _ in range(2):
            c, _ = srv.accept()
            cmd = json.loads(c.makefile().readline())
            got.append(cmd)
            c.sendall(b'{"event":"summon","ts":"x"}\n')            # a broadcast is skipped
            if cmd["cmd"] == "page":
                c.sendall(json.dumps({"event": "page", "trusted": True, "host": "github.com", "elements": [
                    {"role": "button", "label": "Squash and merge", "frame": [1, 2, 3, 4]}]}).encode() + b"\n")
            else:
                c.sendall(b'{"event":"ax_press","ok":false,"reason":"gone"}\n')
            c.close()
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    client = pa.PageClient(path)
    snap = client.snapshot()
    assert snap["elements"][0]["slug"] == "squash-and-merge"
    assert client.press({"role": "button", "slug": "squash-and-merge", "host": GH}) == {
        "ok": False, "reason": "gone", "dry_run": False}
    assert got[1] == {"cmd": "ax_press", "role": "button", "slug": "squash-and-merge", "host": GH, "dry_run": False}
    t.join(2)
    srv.close()
    assert pa.PageClient(f"{d}/none.sock").snapshot()["elements"] == []
    shutil.rmtree(d, ignore_errors=True)


# ---- W18 wish -> action: page buttons in the wish catalogue ------------------------------------------------

def _wish_engine():
    from test_intent_resolve import engine
    eng = engine(claude_on=False)
    eng.page_items = lambda ctx: pa.wish_items(PR_OPEN, ctx)
    return eng


SAFARI_WISH = {"app": "Safari", "space": 1}


def test_wish_items_from_the_page():
    items = {it["id"]: it for it in pa.wish_items(PR_OPEN, {"app": "Safari", "host": GH})}
    sq = items["page:button:squash-and-merge"]
    assert sq["primitive"] == "ui.press" and sq["kind"] == "page" and sq["press"] == "button:squash-and-merge"
    assert {"merge", "fusionne la pr", "squash merge"} <= set(sq["names"]) and sq["uses"] > items["page:button:copy"]["uses"]
    assert "page:link:files-changed" not in items                      # links stay out of the wish catalogue
    assert pa.wish_items(PR_OPEN, {"app": "Safari", "host": "example.com"}) == []   # another page: nothing
    assert pa.wish_items(None, {"app": "Safari", "host": GH}) == []


def test_wish_merge_resolves_to_the_page_button_and_runs_on_enter():
    """No W18 ⇧ confirm: Enter runs the press, which asks the outward gate's panel (oi.live._wish_exec)."""
    eng = _wish_engine()
    eng.live_context = lambda: {"app": "Safari", "host": GH, "desk": 1}
    for text in ("merge", "fusionne la PR", "squash and merge"):
        r = ir_resolve_local(eng, text)
        assert r["id"] == "page:button:squash-and-merge" and r["confirm"] is False and r["press"] == "button:squash-and-merge", text
    msg = eng.handle({"event": "resolve", "id": 5, "text": "merge", "final": True, "context": SAFARI_WISH})
    assert msg["state"] == "done" and msg["token"] == "click:github.com/button:squash-and-merge"
    assert eng.executed[-1]["primitive"] == "ui.press" and eng.executed[-1]["params"]["slug"] == "squash-and-merge"


def test_cancelled_merge_wish_never_presses():
    """W18 follow-up: Esc while the merge wish is resolving drops it on the engine: never executed."""
    eng = _wish_engine()
    eng.live_context = lambda: {"app": "Safari", "host": GH, "desk": 1}
    eng.on_event({"event": "cancel", "id": 8}, lambda m: None)
    msg = eng.handle({"event": "resolve", "id": 8, "text": "merge", "final": True, "context": SAFARI_WISH})
    assert msg["state"] == "failed" and eng.executed == []


def test_a_wish_press_always_asks_the_gate_even_read_only_or_off(gate_asks, data_dir):
    """A wish has no tap: the panel's validate is the only source of oi-hud's press ticket (force)."""
    lv = _live({"event": "dismissed"}, PR_OPEN)
    paths.ensure_private_root()
    (paths.data_dir() / "settings.json").write_text(json.dumps({"gate": {"outward": False}}))
    opt = {"primitive": "ui.press", "params": {"host": GH, "role": "button", "slug": "copy"}, "token": "click:x"}
    assert lv._wish_exec(opt, {}) == "click:x" and gate_asks.asked[-1]["target"] == "button:copy"
    gate_asks.answer = "cancelled"
    assert lv._wish_exec(opt, {}) is None and len(lv.page.presses) == 1


def test_no_page_items_outside_safari():
    eng = _wish_engine()
    eng.live_context = lambda: {"app": "Mail", "host": None}
    assert not any(k.startswith("page:") for k in eng.catalogue({"app": "Mail"}))


def ir_resolve_local(eng, text):
    from oi import intent_resolve as ir
    return ir.resolve_local(text, eng.catalogue(eng.context(SAFARI_WISH)), None)


# ---- incident 2026-10-21 12:28Z: "Close PR" offered one-tap next to "Squash & merge", pressed, PR closed --------

INCIDENT_PAGE = page(("button", "Squash and merge"), ("button", "Close pull request"), ("button", "Approve"),
                     ("button", "Delete branch"), ("button", "Cancel"), ("button", "Supprimer"),
                     ("button", "Fermer la PR"), ("button", "Copy"))


def test_destructive_labels_en_and_fr():
    for s in ("close-pull-request", "delete-branch", "discard-changes", "remove", "cancel", "reject", "revert",
              "abandon", "archive", "unsubscribe", "leave-organization", "lock-conversation", "fermer-la-pr",
              "supprimer", "annuler", "rejeter", "retirer", "archiver", vocab.slug("Se désabonner"),
              vocab.slug("Supprimer la branche")):
        assert pa.destructive(s), s
    for s in ("squash-and-merge", "merge-pull-request", "confirm-squash-and-merge", "approve", "copy",
              "closed-by", "reopen-pull-request", "files-changed", ""):
        assert not pa.destructive(s), s           # whole words: "closed" is not "close"; merge / approve stay
    assert pa.destructive_token("click:github.com/button:close-pull-request")
    assert not pa.destructive_token("click:github.com/button:squash-and-merge")
    assert not pa.destructive_token("activate:Telegram") and not pa.destructive_token(None)


def test_summon_never_offers_a_destructive_rule_and_merge_ranks_first():
    out = pa.apply([], INCIDENT_PAGE, SAFARI_CTX, with_rules=True)
    assert [o["key"] for o in out] == ["page1_squash_and_merge", "page2_approve"]
    assert [o["rank"] for o in out] == [1, 2]
    # in the summon menu the pinned rules keep their rank even when a probability prefers the second one
    out[1]["p_show"] = 0.9
    app = {"key": "switch_app_telegram", "primitive": "app.open", "params": {"app": "Telegram"}, "p_markov": 0.16}
    cfg = {"max_show": 3}
    assert [o["key"] for o in live.summon_options(out + [app], cfg)] == [
        "page1_squash_and_merge", "page2_approve", "switch_app_telegram"]
    assert pa.hud_fields(out[0])["label"] == "Squash & merge"


def test_learned_destructive_press_is_never_offered_automatic_or_summoned():
    def learned(slug, key):
        return {"key": key, "primitive": "ui.press", "token": f"click:github.com/button:{slug}",
                "params": {"host": GH, "role": "button", "slug": slug}, "p_markov": 0.9}
    menu = [learned("close-pull-request", "a"), learned("delete-branch", "b"), learned("cancel", "c"),
            learned("supprimer", "d"), learned("fermer-la-pr", "e"), learned("copy", "f")]
    for with_rules in (False, True):
        keys = [o["key"] for o in pa.apply(menu, INCIDENT_PAGE, SAFARI_CTX, with_rules=with_rules)]
        assert "f" in keys and not set(keys) & {"a", "b", "c", "d", "e"}, keys


def test_wish_still_reaches_close_through_the_gate_path():
    items = {i["id"]: i for i in pa.wish_items(INCIDENT_PAGE, SAFARI_CTX)}
    assert "page:button:close-pull-request" in items          # an explicit wish; oi.live always gates a wish press
    assert items["page:button:squash-and-merge"]["uses"] > items["page:button:close-pull-request"]["uses"]


def test_destructive_click_is_never_learnt():
    def click(label, ts):
        return Ev(ts=vocab.parse_ts(ts), end=vocab.parse_ts(ts), data={"host": GH, "role": "button", "label": label},
                  raw_id=1)
    toks = [r["token"] for r in normalise.click_tokens([click("Close pull request", "2026-10-21T12:28:06Z"),
                                                        click("Supprimer", "2026-10-21T12:29:00Z"),
                                                        click("Squash and merge", "2026-10-21T12:30:00Z")])]
    assert toks == ["click:github.com/button:squash-and-merge"]


def _hud_row(ts, executed, outcome="pressed:3", lat=712):
    return {"ts": ts, "form": "C", "shown": True, "outcome": outcome, "executed": executed, "latency_ms": lat}


def test_hud_press_on_destructive_button_gives_no_pressed_feedback():
    rows = [_hud_row("2026-10-21T12:28:06.869Z", "click:github.com/button:close-pull-request"),
            _hud_row("2026-10-21T12:30:00.000Z", "click:github.com/button:squash-and-merge", "pressed:2", 500)]
    toks = [r["token"] for r in normalise.hud_tokens(rows)]
    assert toks == ["hud:shown", "hud:shown", "hud:pressed:2"]   # the 12:28 close press is not positive feedback


def test_page_press_undone_within_window_is_dropped():
    hud = [_hud_row("2026-10-21T10:00:00.000Z", "click:github.com/button:squash-and-merge", "pressed:2", 0),
           _hud_row("2026-10-21T11:00:00.000Z", "activate:Mail", "pressed:2", 0)]
    presses = normalise.page_press_times(hud)
    assert presses == {"2026-10-21T10:00:00.000Z"}

    def row(ts, tok):
        return {"ts": ts, "dur": 0.0, "token": tok, "app": None, "src": "t", "raw_id": None}
    rows = [row("2026-10-21T10:00:00.000Z", "hud:pressed:2"),                       # page press, undone 2 min later
            row("2026-10-21T10:01:00.000Z", "click:github.com/button:mark-as-read"),  # click, undone 1 min later
            row("2026-10-21T10:02:00.000Z", "click:github.com/button:reopen-pull-request"),
            row("2026-10-21T10:30:00.000Z", "click:github.com/button:copy"),         # undo 31 min later: too late
            row("2026-10-21T11:00:00.000Z", "hud:pressed:2"),                       # not a page press: kept
            row("2026-10-21T11:01:00.000Z", "key:⌘Z")]
    kept = [r["ts"][11:16] + " " + r["token"] for r in normalise.drop_undone(rows, presses)]
    assert kept == ["10:02 click:github.com/button:reopen-pull-request", "10:30 click:github.com/button:copy",
                    "11:00 hud:pressed:2", "11:01 key:⌘Z"]
    assert normalise.drop_undone(rows[3:5], presses) == rows[3:5]               # no undo: unchanged
