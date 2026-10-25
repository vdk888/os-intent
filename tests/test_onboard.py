"""W22: `oi onboard` (sources, privacy gate, fake Claude, validation, files) and the starter catalogue in
`oi live` (fit, blend + decay, picks / wishes, day-one show). Synthetic data only; Claude is always a fake."""
import io
import json
import os
import plistlib
import stat
from datetime import datetime

import pytest
import yaml

from oi import cli, live, onboard, paths, settings, starter

CFG = {"enabled": True, "local_only_domains": ["acme-conseil.example"]}
APPS = {"SketchUp", "Mail", "Safari", "Telegram", "Notion", "Enscape", "Calendar"}
AGENTS = [{"slug": "orion", "name": "Orion", "ask_for": "the fund"}, {"slug": "nova", "name": "Nova", "ask_for": "priorities"}]


def tok(ts, token):
    return {"ts": ts, "token": token, "dur": 1, "app": None, "src": "aw-window", "raw_id": None}


def item(i, prim="app.open", prior=0.3, **kw):
    params = kw.pop("params", None) or ({"app": "SketchUp"} if prim == "app.open" else {})
    return {"id": i, "label": i, "primitive": prim, "params": params, "trigger": kw.pop("trigger", {}),
            "prior": prior, **kw}


def write_catalogue(items):
    paths.ensure_private_root()
    (paths.data_dir() / starter.CATALOGUE).write_text(yaml.safe_dump({"items": items}), encoding="utf-8")


class FakeClaude:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def __call__(self, prompt, model, timeout=None, **kw):
        assert model == "sonnet"
        self.prompts.append(prompt)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


# ---- sources ---------------------------------------------------------------------------------------------

def test_read_apps_names_only_without_system_and_denylisted(tmp_path):
    for a in ("SketchUp", "1Password", "Finder", "Enscape"):
        (tmp_path / f"{a}.app").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    r = onboard.read_apps(["1Password"], dirs=[str(tmp_path)])
    assert r == {"names": ["Enscape", "SketchUp"], "count": 2}


def test_summarise_tokens_counts_hours_and_predecessor():
    rows = []
    for d in range(3):
        rows += [tok(f"2026-01-0{d + 5}T08:00:00+00:00", "activate:Mail"),
                 tok(f"2026-01-0{d + 5}T08:05:00+00:00", "activate:SketchUp"),
                 tok(f"2026-01-0{d + 5}T08:06:00+00:00", "url:www.render.example/job"),
                 tok(f"2026-01-0{d + 5}T08:07:00+00:00", "activate:1Password"),
                 tok(f"2026-01-0{d + 5}T08:08:00+00:00", "title:Mail/secret-subject")]
    s = onboard.summarise_tokens(rows, ["1Password"])
    assert s["days"] == 3
    assert dict(s["top_apps"]) == {"Mail": 3, "SketchUp": 3}            # denylisted app gone
    assert s["top_hosts"] == [("render.example", 3)]                    # host only, www. stripped
    assert s["after"]["app:SketchUp"] == "Mail" and s["after"]["host:render.example"] == "SketchUp"
    assert s["hours"]["app:Mail"][1] - s["hours"]["app:Mail"][0] == onboard.MIN_WINDOW_H
    assert "secret" not in json.dumps(s)                                # titles are never read


def test_peak_window():
    from collections import Counter
    assert onboard.peak_window(Counter({9: 5, 10: 5, 23: 1})) == [9, 11]
    assert onboard.peak_window(Counter({14: 1})) == [14, 15]


def test_read_activity_unavailable_when_no_tokens_and_no_server():
    def down(days):
        raise OSError("refused")
    assert onboard.read_activity([], fetch=down)["from"] == "unavailable"


def test_read_safari_hosts_only(tmp_path):
    saf = tmp_path / "Library" / "Safari"
    saf.mkdir(parents=True)
    with open(saf / "TopSites.plist", "wb") as f:
        plistlib.dump({"TopSites": [{"TopSiteURLString": "https://www.archdaily.com/123?x=1", "TopSiteTitle": "T"}]}, f)
    with open(saf / "Bookmarks.plist", "wb") as f:
        plistlib.dump({"Children": [{"Children": [{"URLString": "https://cloud.render.example/jobs/9", "URIDictionary": {"title": "secret"}}]}]}, f)
    r = onboard.read_safari(tmp_path)
    assert r == {"hosts": ["archdaily.com", "cloud.render.example"]}


def test_read_safari_without_full_disk_access(tmp_path, monkeypatch):
    saf = tmp_path / "Library" / "Safari"
    saf.mkdir(parents=True)
    (saf / "TopSites.plist").write_bytes(b"x")
    real_open = open

    def guarded(p, *a, **k):
        if str(p).endswith(".plist"):
            raise PermissionError(p)
        return real_open(p, *a, **k)
    monkeypatch.setattr("builtins.open", guarded)
    r = onboard.read_safari(tmp_path)
    assert r["hosts"] == [] and "Full Disk Access" in r["why"]


def test_read_drive_two_levels_of_folder_names(tmp_path):
    base = tmp_path / "Library" / "CloudStorage" / "GoogleDrive-l@x.example" / "My Drive"
    (base / "Projets" / "Villa Rossi").mkdir(parents=True)
    (base / "Projets" / "Villa Rossi" / "deep").mkdir()
    (base / "Projets" / "plan.pdf").write_text("BODY")
    r = onboard.read_drive(tmp_path)
    assert r == {"folders": [{"name": "Projets", "sub": ["Villa Rossi"]}]}


def test_read_mail_mailbox_names(tmp_path):
    out = "Work\n  INBOX\n  Clients\nPerso\n  INBOX\n"
    r = onboard.read_mail(lambda script, args, timeout: (0, out))
    assert r["accounts"][0] == {"name": "Work", "mailboxes": ["INBOX", "Clients"]}
    assert onboard.read_mail(lambda *a: (1, ""))["accounts"] == []


# ---- privacy gate ----------------------------------------------------------------------------------------

def test_gate_drops_local_only_names_and_answers():
    src = {"apps": {"names": ["SketchUp"], "count": 1},
           "activity": {"top_apps": [("SketchUp", 3)], "top_hosts": [("portal.acme-conseil.example", 4), ("archdaily.com", 2)],
                        "hours": {"host:portal.acme-conseil.example": [9, 10]}, "after": {}},
           "drive": {"folders": [{"name": "Acme Conseil", "sub": []}, {"name": "Projets", "sub": ["AcmeConseil 2026"]}]}}
    g = onboard.gate_sources(src, CFG)
    assert g["activity"]["top_hosts"] == [["archdaily.com", 2]] and g["activity"]["hours"] == {}
    assert g["drive"]["folders"] == [{"name": "Projets", "sub": []}]
    ans, n = onboard.gate_answers({"clients": "Villa Rossi. Acme-Conseil for audits. Musée X."}, CFG)
    assert ans["clients"] == "Villa Rossi. Musée X." and n == 1


# ---- validation ------------------------------------------------------------------------------------------

def _v(it):
    from oi.orchestrator import pack
    return onboard.validate_item(it, apps=APPS, agents={"orion", "nova"}, cfg=CFG, terms=pack.deny_terms(CFG))


def test_validate_item_rules():
    ok = _v({"id": "Open SketchUp!", "label": "Open the SketchUp project now please", "primitive": "app.open",
             "params": {"app": "SketchUp", "deeplink": "sketchup://open?file=x"}, "prior": 0.9,
             "trigger": {"apps": ["Mail", "Photoshop"], "hours": [8, 12], "days": ["mon", "xyz"]}})
    assert ok["label"] == "Open the SketchUp proj" and ok["prior"] == 0.6 and ok["id"] == "open-sketchup"
    assert ok["trigger"] == {"apps": ["Mail"], "hours": [8, 12], "days": ["mon"]}
    assert ok["params"]["deeplink"].startswith("sketchup://")
    assert _v({"label": "x", "primitive": "app.open", "params": {"app": "Photoshop"}}) is None        # not installed
    assert _v({"label": "x", "primitive": "app.open", "params": {"app": "Mail", "deeplink": "file:///etc"}})["params"] == {"app": "Mail"}
    assert _v({"label": "x", "primitive": "shell.run", "params": {}}) is None
    assert _v({"label": "x", "primitive": "agent.ask", "params": {"to": "bob"}}) is None
    assert _v({"label": "Ask Orion", "primitive": "agent.ask", "params": {"to": "orion"}})["params"] == {"to": "orion"}
    assert _v({"label": "x", "primitive": "url.open", "params": {"href": "http://cloud.render.example/new"}})["params"] == \
        {"href": "https://cloud.render.example/new"}
    assert _v({"label": "x", "primitive": "url.open", "params": {"href": "https://a.example/?token=1"}}) is None
    assert _v({"label": "x", "primitive": "url.open", "params": {"href": "javascript:alert(1)"}}) is None
    assert _v({"label": "x", "primitive": "url.open", "params": {"href": "https://portal.acme-conseil.example/"}}) is None
    assert _v({"label": "Audit Acme Conseil", "primitive": "app.open", "params": {"app": "Mail"}}) is None   # leaks
    m = _v({"label": "Weekly report", "primitive": "mail.draft", "params": {"to": "not an address", "subject": "Weekly",
                                                                           "body": "Hi,\n"}})
    assert m["params"] == {"to": "", "subject": "Weekly", "body": "Hi,\n"}
    assert _v({"label": "x", "primitive": "mail.draft", "params": {"to": "a@acme-conseil.example"}}) is None
    assert _v({"label": "x", "primitive": "folder.open", "params": {"path": "/"}}) is None


def test_attention_validation_and_prior():
    a = onboard.validate_attention({"sources": {"mail": 0.8, "markets": 3, "spam": 1},
                                    "important": [{"who": "Nova", "class": "agent", "weight": 0.95}, {"who": ""}],
                                    "noise": [{"who": "newsletters", "class": "weird"}]})
    assert a == {"sources": {"mail": 0.8, "markets": 1.0}, "important": [{"who": "Nova", "class": "agent", "weight": 0.95}],
                 "noise": [{"who": "newsletters", "class": "topic", "weight": 0.1}]}
    prof = {"attention": a}
    assert onboard.attention_prior(prof, "telegram", "Nova - CEO") == 0.95
    assert onboard.attention_prior(prof, "mail", "Weekly newsletters") == 0.1
    assert onboard.attention_prior(prof, "mail") == 0.8 and onboard.attention_prior(prof, "news") == 0.5


# ---- build with a fake Claude ----------------------------------------------------------------------------

def _sources():
    return {"apps": {"names": sorted(APPS), "count": len(APPS)},
            "activity": {"from": "tokens", "days": 5, "top_apps": [("SketchUp", 40), ("Mail", 30), ("Telegram", 5)],
                         "top_hosts": [("archdaily.com", 9), ("portal.acme-conseil.example", 7)],
                         "hours": {"app:SketchUp": [9, 13]}, "after": {"app:SketchUp": "Mail"}}}


ANSWERS = {"job": "Architect, small studio.", "clients": "Villa Rossi. Acme-Conseil audit.",
           "other": "BODY-SENTINEL this is a pasted document paragraph."}


def _claude_answer(n=18):
    cat = [{"id": f"render-{i}", "label": f"Render {i}", "primitive": "url.open",
            "params": {"href": f"https://cloud.render.example/new/{i}"}, "trigger": {"apps": ["SketchUp"]}, "prior": 0.3}
           for i in range(n)]
    cat.append({"id": "bad", "label": "x", "primitive": "app.open", "params": {"app": "Photoshop"}})
    return {"profile": {"job": "Architect", "tools": ["SketchUp"], "domain_vocabulary": ["permis de construire"],
                        "counterparts": [{"name": "Render cloud", "kind": "service"}],
                        "attention": {"sources": {"mail": 0.7}}, "summary": "Acme-Conseil client"},
            "catalogue": cat}


def test_build_with_fake_claude_gates_prompt_and_validates():
    fake = FakeClaude(_claude_answer())
    profile, cat, meta = onboard.build(ANSWERS, _sources(), team=False, use_claude=True, cfg=CFG, run=fake,
                                       confirmed=onboard.source_names(_sources()))
    p = fake.prompts[0]
    assert "acme" not in p.lower() and "Villa Rossi" in p and "archdaily.com" in p
    assert "HUD orchestrator on the user" not in p                        # W22 prompts are not the user-wrapped
    assert meta["claude"] == "ok" and meta["dropped_items"] == 1 and meta["withheld_answer_parts"] == 1
    assert onboard.MIN_ITEMS <= len(cat["items"]) <= onboard.MAX_ITEMS
    assert cat["items"][0]["source"] == "claude"
    assert "summary" not in profile and profile["job"] == "Architect"     # the leaking summary was dropped
    assert profile["attention"]["sources"] == {"mail": 0.7}


def test_build_caps_at_30_and_tops_up_to_15():
    _, cat, _ = onboard.build({}, _sources(), team=False, use_claude=True, cfg=CFG, run=FakeClaude(_claude_answer(40)))
    assert len(cat["items"]) == 30
    _, cat, _ = onboard.build({}, _sources(), team=False, use_claude=True, cfg=CFG, run=FakeClaude(_claude_answer(3)))
    assert len(cat["items"]) >= 9 and {i["source"] for i in cat["items"]} >= {"claude", "activity", "apps"}


def test_build_claude_unavailable_falls_back_to_local_with_roster(monkeypatch):
    from oi import claude_actions as ca
    from oi import orchestrator
    monkeypatch.setattr(orchestrator, "roster", lambda path=None: AGENTS)
    profile, cat, meta = onboard.build(ANSWERS, _sources(), team=True, use_claude=True, cfg=CFG,
                                       run=FakeClaude(ca.ClaudeUnavailable("no_login")))
    assert meta["claude"] == "unavailable:no_login"
    asks = [i for i in cat["items"] if i["primitive"] == "agent.ask"]
    assert {i["params"]["to"] for i in asks} == {"orion", "nova"} and asks[0]["label"] == "Ask Orion"
    sk = next(i for i in cat["items"] if i["id"] == "app-sketchup")
    assert sk["trigger"] == {"hours": [9, 13], "apps": ["Mail"]}
    assert not any("acme" in json.dumps(i) for i in cat["items"])
    assert [c["slug"] for c in profile["counterparts"]] == ["orion", "nova"]


def test_written_files_are_private_and_hold_no_document_text():
    profile, cat, _ = onboard.build(ANSWERS, _sources(), team=False, use_claude=True, cfg=CFG,
                                    run=FakeClaude(_claude_answer()))
    pp, cp = onboard.write(profile, cat)
    for p in (pp, cp):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
        assert "BODY-SENTINEL" not in p.read_text()
    assert starter.load_catalogue() and starter.load_catalogue()[0]["id"] == "render-0"


# ---- the conversation ------------------------------------------------------------------------------------

def test_converse_opt_in_followups_and_team(monkeypatch):
    from oi import claude_actions as ca
    monkeypatch.setattr(ca, "claude_settings", lambda path=None: dict(CFG))
    read = []
    replies = iter(["n", "y", "n", "n", "n",            # activity no, safari yes, drive / mail / notion no
                    "Architect", "", "", "", "", "", "",   # 7 questions
                    "y",                                    # team
                    "Renders go to the cloud on Fridays"])  # 1 follow-up
    out = io.StringIO()
    fake = FakeClaude({"questions": ["When do you send renders?"]})
    answers, sources, team, confirmed = onboard.converse(onboard.IO(lambda p: next(replies), out), run=fake,
                                              read=lambda k, deny: read.append(k) or {"names": [], "count": 0},
                                              has_roster=True)
    assert read == ["apps", "safari"] and set(sources) == {"apps", "safari"}
    assert team is True and answers["job"] == "Architect" and answers["followup_1"].startswith("Renders")
    assert confirmed == set()                                       # no name read: nothing to confirm
    text = out.getvalue()
    assert text.count("·") == 6 and "Full Disk Access" in text and "local-only" in text   # a privacy line per source


def test_cli_onboard_end_to_end_no_claude(tmp_path, capsys, data_dir):
    ans = tmp_path / "a.yaml"
    ans.write_text(yaml.safe_dump({"answers": {k: "" for k, _ in onboard.QUESTIONS}, "sources": [], "team": False}))
    src = tmp_path / "s.yaml"
    src.write_text(yaml.safe_dump(json.loads(json.dumps(_sources()))))
    assert cli.main(["onboard", "--answers", str(ans), "--sources-file", str(src), "--no-claude"]) == 0
    out = capsys.readouterr().out
    assert "Starter catalogue:" in out and "Claude: off" in out
    assert (data_dir / "starter_catalogue.yaml").exists() and (data_dir / "profile.yaml").exists()
    assert cli.main(["onboard", "--answers", str(ans), "--sources-file", str(src), "--no-claude", "--dry-run",
                     "--out", str(tmp_path / "o")]) == 0
    assert not (tmp_path / "o").exists()


# ---- list and confirm the source names before any Claude prompt --------------------------------------------

def _named_sources():
    return {"apps": {"names": ["SketchUp", "Mail"], "count": 2},
            "drive": {"folders": [{"name": "Clients", "sub": ["Villa Rossi", "Maison Durand", "Acme-Conseil"]}]},
            "mail": {"accounts": [{"name": "studio@example.org", "mailboxes": ["INBOX", "Durand litige"]}]}}


def _converse(monkeypatch, replies, interactive=True, sources=None):
    from oi import claude_actions as ca
    monkeypatch.setattr(ca, "claude_settings", lambda path=None: dict(CFG))
    src = sources or _named_sources()
    replies = iter(["y", "n", "y", "y", "n"] + [""] * 7 + replies + [""] * 3)   # activity, drive, mail chosen
    out = io.StringIO()
    shown_before = []
    fake = FakeClaude({"questions": ["Which client first?"]})
    orig = fake.__call__

    class Spy(FakeClaude):
        def __call__(self, prompt, model, timeout=None, **kw):
            shown_before.append(out.getvalue())
            return orig(prompt, model, timeout)
    spy = Spy(None)
    res = onboard.converse(onboard.IO(lambda p: next(replies), out, interactive=interactive), run=spy,
                           read=lambda k, deny: src.get(k, {}), has_roster=False)
    return res, fake.prompts, shown_before, out.getvalue()


def test_confirm_lists_names_and_removals_never_reach_claude(monkeypatch):
    names = onboard.source_names(onboard.gate_sources(_named_sources(), CFG))
    assert ("drive folder", "Clients/Acme-Conseil") not in names                 # local-only: never even listed
    i_durand = names.index(("drive folder", "Clients/Maison Durand")) + 1
    i_box = names.index(("mailbox", "studio@example.org/Durand litige")) + 1
    (answers, sources, team, confirmed), prompts, shown_before, text = _converse(
        monkeypatch, ["x", f"{i_durand} {i_box}"])                             # a bad entry is asked again
    assert len(prompts) == 1
    assert "Durand litige" in shown_before[0] and "Villa Rossi" in shown_before[0]   # listed BEFORE the prompt
    assert "numbers 1-" in text
    assert "Maison Durand" not in prompts[0] and "Durand litige" not in prompts[0]
    assert "Villa Rossi" in prompts[0] and "INBOX" in prompts[0] and "acme" not in prompts[0].lower()
    fake = FakeClaude(_claude_answer())
    onboard.build(answers, sources, team=team, use_claude=True, cfg=CFG, run=fake, confirmed=confirmed)
    assert "Maison Durand" not in fake.prompts[0] and "Durand litige" not in fake.prompts[0]
    assert "Villa Rossi" in fake.prompts[0] and "acme" not in fake.prompts[0].lower()


def test_confirm_all_sends_no_name(monkeypatch):
    (answers, sources, team, confirmed), prompts, _, _ = _converse(monkeypatch, ["all"])
    assert confirmed == set()
    for name in ("Villa Rossi", "INBOX", "studio@example.org", "SketchUp"):
        assert name not in prompts[0]


def test_non_interactive_sends_no_source_name_but_local_builder_uses_them(monkeypatch):
    (answers, sources, team, confirmed), prompts, _, text = _converse(monkeypatch, [], interactive=False)
    assert confirmed == set() and "not interactive" in text
    assert "Villa Rossi" not in prompts[0] and "studio@example.org" not in prompts[0]
    fake = FakeClaude(_claude_answer())
    _, cat, meta = onboard.build(ANSWERS, _sources(), team=False, use_claude=True, cfg=CFG, run=fake)  # default: none
    assert meta["names_sent"] == 0 and "archdaily.com" not in fake.prompts[0] and "SketchUp" not in fake.prompts[0]
    assert any(i["id"] == "site-archdaily-com" for i in cat["items"])          # still proposed, from this Mac


def test_cli_sources_file_sends_names_only_with_the_flag(tmp_path, monkeypatch, data_dir):
    from oi import claude_actions as ca
    monkeypatch.setattr(ca, "claude_settings", lambda path=None: dict(CFG))
    fake = FakeClaude(_claude_answer())
    monkeypatch.setattr(ca, "run_claude", fake)
    monkeypatch.setattr(onboard.build, "__kwdefaults__", {**onboard.build.__kwdefaults__, "run": fake})
    ans = tmp_path / "a.yaml"
    ans.write_text(yaml.safe_dump({"answers": {"job": "Architect"}, "team": False}))
    src = tmp_path / "s.yaml"
    src.write_text(yaml.safe_dump(json.loads(json.dumps(_named_sources()))))
    assert cli.main(["onboard", "--answers", str(ans), "--sources-file", str(src), "--dry-run"]) == 0
    assert "Villa Rossi" not in fake.prompts[-1]
    assert cli.main(["onboard", "--answers", str(ans), "--sources-file", str(src), "--dry-run",
                     "--send-source-names"]) == 0
    assert "Villa Rossi" in fake.prompts[-1] and "acme" not in fake.prompts[-1].lower()


def test_keep_names_prunes_activity_keys():
    src = _sources()
    kept = onboard.keep_names(src, [("app", "SketchUp"), ("site", "archdaily.com")])
    a = kept["activity"]
    assert a["top_apps"] == [["SketchUp", 40]] and a["top_hosts"] == [["archdaily.com", 9]]
    assert a["hours"] == {"app:SketchUp": [9, 13]} and a["after"] == {}            # "after Mail": Mail not confirmed
    assert kept["apps"] == {"names": ["SketchUp"], "count": 1}


# ---- noise: what the person called noise is never proposed ----------------------------------------------

def test_noise_dropped_from_local_and_claude_items():
    src = _sources()
    src["activity"]["top_hosts"].insert(0, ("pinterest.com", 50))
    src["activity"]["top_apps"].append(("Telegram", 4))
    ans = _claude_answer()
    ans["profile"]["attention"] = {"noise": [{"who": "Pinterest", "class": "topic"},
                                             {"who": "https://www.linkedin.com/", "class": "alert"},
                                             {"who": "telegram", "class": "sender"}]}
    ans["catalogue"] += [
        {"id": "pins", "label": "Pins", "primitive": "url.open", "params": {"href": "https://fr.pinterest.com/me"},
         "trigger": {"apps": ["SketchUp"]}, "prior": 0.5},
        {"id": "sk-li", "label": "SketchUp", "primitive": "app.open", "params": {"app": "SketchUp"},
         "trigger": {"hosts": ["linkedin.com"]}, "prior": 0.5},
        {"id": "tg", "label": "Telegram", "primitive": "app.open", "params": {"app": "Telegram"}, "prior": 0.5}]
    profile, cat, meta = onboard.build({}, src, team=False, use_claude=True, cfg=CFG, run=FakeClaude(ans))
    dump = json.dumps(cat["items"]).lower()
    assert "pinterest" not in dump and "linkedin" not in dump and "telegram" not in dump
    assert meta["noise_dropped"] >= 4 and len(cat["items"]) >= onboard.MIN_ITEMS
    assert any(i["id"] == "site-archdaily-com" for i in cat["items"])
    assert not onboard.is_noise({"primitive": "url.open", "params": {"href": "https://mail.google.com/"}}, {"mail"})


def test_fits_ignores_a_broken_hand_edited_trigger():
    assert starter.fits(item("a", trigger={"hours": ["x", None]}), {}, WHEN) == 0.0
    assert _st().p_item({"id": "z", "prior": "high"}) == pytest.approx(0.1)


# ---- starter catalogue in the live driver ----------------------------------------------------------------

WHEN = datetime(2026, 1, 5, 10, 0)    # a Monday, 10:00 local


def test_fits_trigger():
    assert starter.fits(item("a", trigger={"apps": ["SketchUp"]}), {"app": "SketchUp"}, WHEN) == 1.0
    assert starter.fits(item("a", trigger={"apps": ["SketchUp"]}), {"app": "Mail"}, WHEN) == 0.0
    assert starter.fits(item("a", trigger={"hosts": ["render.example"]}), {"url": "cloud.render.example/x"}, WHEN) == 1.0
    assert starter.fits(item("a", trigger={"hours": [11, 12]}), {}, WHEN) == 0.0
    assert starter.fits(item("a", trigger={"hours": [22, 11]}), {}, WHEN) == starter.ANYWHERE   # wraps midnight
    assert starter.fits(item("a", trigger={"days": ["fri"]}), {}, WHEN) == 0.0


def _st(t=None):
    return starter.Starters(clock=lambda: 0.0, now_local=lambda t: WHEN)


def test_blend_day_one_then_decay_and_merge():
    write_catalogue([item("sketchup", trigger={"apps": ["Mail"]}, prior=0.4),
                     item("ask-orion", "agent.ask", params={"to": "orion"}, prior=0.3),
                     item("render", "url.open", params={"href": "https://cloud.render.example/new"},
                          trigger={"apps": ["Mail"]}, prior=0.2)])
    st = _st()
    menu, used = st.blend([], {"app": "Mail"}, support=0)
    assert used and [o["starter"] for o in menu] == ["sketchup", "render", "ask-orion"]
    assert [o["p_show"] for o in menu] == [0.4, 0.2, 0.15]               # anywhere = half
    assert menu[0]["token"] == "activate:SketchUp" and menu[2]["token"] == "starter:ask-orion"
    # 60 observations of the context: λ = 20/80; a Markov option on the same app is blended, not duplicated
    markov = [{"key": "a", "primitive": "app.open", "params": {"app": "SketchUp"}, "token": "activate:SketchUp", "p_markov": 0.1},
              {"key": "nothing", "primitive": None, "token": "nothing", "p_markov": None}]
    menu, _ = st.blend(markov, {"app": "Mail"}, support=60)
    sk = menu[0]
    assert sk["key"] == "a" and sk["starter"] == "sketchup" and sk["p_show"] == pytest.approx(0.75 * 0.1 + 0.25 * 0.4)
    assert menu[-1]["key"] == "nothing" and len(menu) == 4
    # the front app itself is never proposed
    assert not any(o.get("starter") == "sketchup" for o in st.blend([], {"app": "SketchUp"}, 0)[0])


def test_picks_raise_and_ignored_shows_sink(data_dir):
    write_catalogue([item("a", prior=0.3), item("b", "agent.ask", params={"to": "orion"}, prior=0.3)])
    st = _st()
    for _ in range(6):
        st.record(["a", "b"], "a")
    assert st.p_item({"id": "a", "prior": 0.3}) > 0.3 > st.p_item({"id": "b", "prior": 0.3})
    p = data_dir / starter.STATS
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert json.loads(p.read_text())["items"]["a"] == {"shown": 6, "picked": 6}
    assert starter.Starters().stats["items"]["b"]["shown"] == 6          # reloaded from disk


def test_wishes_refine(data_dir):
    write_catalogue([item("sk", prior=0.3)])
    d = data_dir / "wish-actions"
    d.mkdir(parents=True)
    today = datetime.now().date().isoformat()
    rows = [{"state": "done", "token": "activate:SketchUp", "app": "Mail"},
            {"state": "done", "token": "url:www.archdaily.com/x", "app": "Safari"},
            {"state": "done", "token": "url:www.archdaily.com/y", "app": "Safari"},
            {"state": "noted", "token": "activate:Notes"},
            {"state": "done", "token": "activate:Notes", "dry_run": True}]
    (d / f"{today}.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    st = _st()
    assert st.refine_from_wishes() == 1
    learned = st.stats["learned"][0]
    assert learned["primitive"] == "url.open" and learned["trigger"] == {"apps": ["Safari"]}
    assert st.stats["items"]["sk"]["wished"] == 1 and st.stats["items"][learned["id"]]["wished"] == 2
    assert st.refine_from_wishes() == 0                                   # idempotent
    assert any(o.get("starter") == learned["id"] for o in st.blend([], {"app": "Safari"}, 0)[0])


def test_disabled_in_settings(data_dir):
    write_catalogue([item("a")])
    (data_dir / "settings.json").write_text(json.dumps({"starter": {"enabled": False}}))
    assert _st().blend([], {"app": "Mail"}, 0) == ([], False)


def test_hud_option_and_executor_for_starter_primitives():
    o = live.hud_option(1, {"primitive": "agent.ask", "params": {"to": "orion"}, "token": "starter:ask-orion",
                            "label": "Ask Orion", "p_show": 0.2}, {})
    assert o["label"] == "Ask Orion" and o["icon"] == "initial:O"
    o = live.hud_option(2, {"primitive": "mail.draft", "params": {}, "token": "starter:w", "label": "Weekly report"}, {})
    assert o["icon"] == "sf:square.and.pencil"
    o = live.hud_option(3, {"primitive": "url.open", "params": {"href": "https://x.example/"}, "token": "url:x.example/home",
                            "label": "Render request"}, {})
    assert o["label"] == "Render request"
    calls, cmds = [], []
    ex = live.Executor(lambda argv: calls.append(argv) or (0, ""), hud_cmd=lambda p: cmds.append(p) or {"event": "ok"})
    assert ex.run({"primitive": "mail.draft", "token": "starter:w",
                   "params": {"to": "a@b.example", "subject": "Weekly report", "body": "Hi & bye"}}, {}, str) == "starter:w"
    assert calls[-1] == ["open", "mailto:a@b.example?subject=Weekly%20report&body=Hi%20%26%20bye"]
    assert ex.run({"primitive": "url.open", "token": "url:x.example/home",
                   "params": {"href": "https://x.example/new?session=1"}}, {}, str)
    assert calls[-1] == ["open", "-a", "Safari", "https://x.example/new"]
    assert ex.run({"primitive": "app.open", "token": "starter:sk", "params": {"app": "SketchUp", "deeplink": "sketchup://x"}}, {}, str)
    assert calls[-1] == ["open", "-a", "SketchUp", "sketchup://x"]
    assert ex.run({"primitive": "agent.ask", "token": "starter:ask-orion", "params": {"to": "orion"}}, {}, str) == "starter:ask-orion"
    assert cmds == [{"cmd": "vocal_to", "pick": "orion"}]
    assert live.Executor(lambda a: (0, "")).run({"primitive": "agent.ask", "token": "t", "params": {"to": "orion"}}, {}, str) is None


class FakeSender:
    def __init__(self, reply):
        self.reply, self.sent = reply, []

    def __call__(self, payload, on_reply):
        import threading
        self.sent.append(payload)
        t = threading.Thread(target=on_reply, args=(self.reply,))
        t.start()
        return t


def test_day_one_show_without_markov_then_pick_is_recorded(data_dir):
    """No model, no history for the context: the starter priors alone pass the gate; the press runs and counts."""
    write_catalogue([item("sketchup", trigger={"apps": ["Mail"]}, prior=0.4)])
    calls = []
    lv = live.Live(pull=False, sender=FakeSender({"event": "pressed", "key": 2}), runner=lambda a: calls.append(a) or (0, ""),
                   out=io.StringIO(), resolve_app=lambda a: f"/Applications/{a}.app", is_executable=lambda o: True)
    lv.starters.now_local = lambda t: WHEN
    lv.day = "2026-01-05"
    lv.rows = [tok("2026-01-05T09:00:00+00:00", "activate:Mail") | {"app": "Mail", "dur": 30}]
    lv.model = None
    r = lv.evaluate(settings.load(), now=1000.0)
    assert r["reason"] == "show" and r["payload"]["options"][0]["label"] == "SketchUp"
    lv.wait_replies(5)
    assert calls == [["open", "-a", "SketchUp"]]
    assert lv.starters.stats["items"]["sketchup"] == {"shown": 1, "picked": 1}
    from oi import normalise
    row = normalise.read_hud_log(live.now_iso(1000.0)[:10])[-1]
    assert row["options"][0]["starter"] == "sketchup" and row["executed"] == "activate:SketchUp"


def test_merge_drops_local_sites_claude_already_covers():
    c = [{"id": "farm", "label": "Ferme de rendu", "primitive": "url.open", "params": {"href": "https://render.x.example/jobs/new"},
          "trigger": {}, "prior": 0.4, "source": "claude"}]
    loc = [{"id": "site-render", "label": "render.x.example", "primitive": "url.open", "params": {"href": "https://render.x.example/"},
            "trigger": {}, "prior": 0.4, "source": "activity"},
           {"id": "site-other", "label": "other.example", "primitive": "url.open", "params": {"href": "https://other.example/"},
            "trigger": {}, "prior": 0.2, "source": "activity"}]
    assert [i["id"] for i in onboard.merge_catalogue(c, loc)] == ["farm", "site-other"]
