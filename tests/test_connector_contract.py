"""Every proactive connector against the behaviour contract (docs/DESIGN.md « Behaviour contract » / « Connector
compliance »), through the shared helper tests/connector_contract.py. Fakes only: no Mail, Telegram, folder, network
or feed is touched."""
import os
from datetime import datetime, timezone

import pytest

from connector_contract import NAMES, check_connector, levels
from file_watcher_fake import FakeFileWatcher
from oi import glide
from oi.proactive import config, contract, engine, sensors, store

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc).timestamp()     # 14:00 Paris

DASH = "https://dashboard.example"          # a configured ops dashboard (the default has none)


def _cfg(raw: dict | None = None) -> dict:
    """proactive_settings with DASH as cockpit.url (unless `raw` sets its own cockpit block)."""
    raw = dict(raw or {})
    p = dict(raw.get("proactive") or {})
    p.setdefault("cockpit", {"url": DASH})
    return config.proactive_settings(dict(raw, proactive=p))



def mail_out(*msgs):
    return "OK" + sensors.RS + "".join(sensors.SEP.join(m) + sensors.RS for m in msgs)


def test_mail_connector():
    w = {"state": "off"}

    def osa(script, args, timeout=6):
        if w["state"] == "broken":
            return 1, ""
        if script == sensors.MAIL_STATE:
            return 0, "OK" + sensors.RS
        read = "1" if w["state"] == "off" and w.get("was_on") else "0"
        if w["state"] == "on":
            w["was_on"] = True
        rows = [("<m1@acme>", "Sam Martin", "g@acme.example", "Devis formation", "2026-10-21T13:59:00", read, "0")]
        return 0, mail_out(*rows) if w.get("was_on") else mail_out()
    s = sensors.MailSensor(osa=osa, claude_cfg=lambda: {"local_only_domains": []}, log=lambda m: None,
                           running=lambda: True)
    ev = check_connector(s, _cfg({}), lambda st: w.update(state=st), now=NOW)
    assert ev["ref"]["message_id"] == "<m1@acme>"
    opts, card = levels(ev)
    assert [o["primitive"] for o in opts][:2] == ["mail.reply", "mail.show"]
    assert card["what"].startswith("Sam · Devis formation · depuis ")
    assert contract.LEVEL3["mail"] == "model"                 # the body by message id (glide.material)


def test_mail_closed_is_unknown_state_and_health_info():
    s = sensors.MailSensor(osa=lambda *a, **k: (0, "NOTRUNNING"), claude_cfg=lambda: {}, log=lambda m: None,
                           running=lambda: False)
    assert s.poll(NOW, {}) == [] and s.last_error == "not_running"


def test_agents_connector():
    w = {"state": "off"}
    dock = {"off": 0, "on": 2, "broken": None}
    s = sensors.AgentSensor(unread=lambda: {"Atlas - R&D": 2, "Nova - CEO": 0}, dock_badge=lambda: dock[w["state"]],
                            running=lambda: True, outbound=None, names=lambda: NAMES)
    ev = check_connector(s, _cfg({}), lambda st: w.update(state=st), now=NOW)
    assert store.token(ev) == "evt:agent:atlas"
    opts, card = levels(ev)
    assert opts[0]["primitive"] == "telegram.chat" and card["what"].startswith("Atlas t'attend · 2 messages")
    assert card["agent"] == "atlas"                            # level 3: its pending messages (Mac agents' record)


def test_agents_closed_is_health_info():
    s = sensors.AgentSensor(unread=lambda: None, dock_badge=lambda: None, running=lambda: False, outbound=None)
    assert s.poll(NOW, _cfg({})) == [] and s.last_error == "not_running"


def test_files_connector(tmp_path, monkeypatch):
    """Files are sensed by oi-watcher (TCC: oi live's Python cannot read ~/Desktop): the connector reads its bucket
    and its per-folder read state (tests/file_watcher_fake.py mirrors what FolderSource.swift posts)."""
    home = tmp_path / "home"
    desk = home / "Desktop"
    desk.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    f = desk / "devis.pdf"
    fw = FakeFileWatcher([str(desk)], now=NOW)
    w = {"n": 0, "live": False}

    def world(st):                                            # called before the poll at NOW + 10 * n
        w["n"] += 1
        t = NOW + 10 * w["n"]
        fw.now = t
        fw.folders[str(desk)] = "unreadable" if st == "broken" else "ok"   # the watcher cannot list it (TCC)
        if st == "on" and not w["live"]:
            f.write_text("x")
            fw.live(str(f), t)
            w["live"] = True
        elif st == "off" and w["live"]:
            f.unlink()
            fw.gone(str(f), t)
            w["live"] = False
    s = fw.sensor()
    cfg = _cfg({"proactive": {"files": {"folders": [str(desk)]}}})
    ev = check_connector(s, cfg, world, now=NOW)
    assert ev["subject"] == "devis.pdf" and ev["class"] == "desktop" and ev["ts"] == "2026-10-21T12:00:20.000Z"
    f.write_text("x")                                         # level 1 is rendered while the file is there
    opts, card = levels(ev)
    assert [o["primitive"] for o in opts] == ["file.open", "folder.open"]
    assert card["what"].startswith("devis.pdf · Desktop · depuis ")
    assert contract.LEVEL3["file"] is None                    # gap: no level-3 provider for files yet


def test_files_unconfigured_is_a_health_failure():
    s = sensors.FolderSensor(claude_cfg=lambda: {}, buckets=lambda: [], fetch=lambda b, s: [], status=lambda: None)
    assert s.poll(NOW, {"files": {"folders": []}}) == [] and s.last_error == "not_configured"


def test_files_watcher_not_running_is_health_info(tmp_path):
    fw = FakeFileWatcher([str(tmp_path)], now=NOW)
    fw.running = False
    s = fw.sensor()
    cfg = _cfg({"proactive": {"files": {"folders": [str(tmp_path)]}}})
    assert s.poll(NOW, cfg) == [] and s.last_error == "watcher_not_running"
    from oi import health
    assert "watcher_not_running" in health.INFO_KINDS


def test_cockpit_alerts_connector():
    w = {"state": "off"}
    feeds = {"off": [], "on": [{"id": "a7", "title": "NAV stale", "kind": "nav", "path": "/dept/orion"}], "broken": None}
    s = sensors.CockpitSensor(probe=lambda url: True, reader=lambda p: feeds[w["state"]])
    cfg = _cfg({"proactive": {"cockpit": {"alerts_file": "/fake/alerts.json"}}})
    ev = check_connector(s, cfg, lambda st: w.update(state=st), now=NOW)
    assert store.token(ev) == "evt:cockpit:nav"
    _, card = levels(ev)
    assert card["summary"] == ["NAV stale", "Alerte nav ouverte depuis 14:00"]


def test_cockpit_outage_level3_and_unconfigured():
    s = sensors.CockpitSensor(probe=lambda url: False, reader=lambda p: None)
    cfg = _cfg({})
    s.poll(NOW, cfg)
    out = s.poll(NOW + 60, cfg)
    assert [store.token(e) for e in out] == ["evt:cockpit:outage"] and s.last_error is None   # no alert feed set
    assert levels(out[0])[1]["summary"][0].startswith("La sonde de santé du cockpit échoue")
    bare = sensors.CockpitSensor(probe=lambda url: True, reader=lambda p: None)
    assert bare.poll(NOW, {"cockpit": {"url": "", "alerts_file": None}}) == [] and bare.last_error == "not_configured"


def test_metrics_connector():
    w = {"state": "off"}
    val = {"off": 12, "on": 4}
    s = sensors.MetricsSensor(reader=lambda p: None if w["state"] == "broken" else
                              {"metrics": [{"id": "aum", "label": "AUM", "value": val[w["state"]], "threshold": 5,
                                            "owner": "orion"}]})
    cfg = _cfg({"proactive": {"metrics": {"file": "/fake/m.json"}}})
    ev = check_connector(s, cfg, lambda st: w.update(state=st), now=NOW)
    _, card = levels(ev)
    assert card["summary"] == ["AUM: 4 (seuil 5)", "Sous le seuil depuis 14:00", "Responsable : orion"]


def test_markets_connector():
    w = {"state": "off"}
    pct = {"off": 0.5, "on": -4.2}
    s = sensors.MarketsSensor(reader=lambda p: None if w["state"] == "broken" else
                              {"brief": {}, "moves": [{"symbol": "CW8", "pct": pct[w["state"]]}]})
    cfg = _cfg({"proactive": {"markets": {"file": "/fake/k.json", "move_pct": 3}}})
    ev = check_connector(s, cfg, lambda st: w.update(state=st), now=NOW)
    _, card = levels(ev)
    assert card["summary"] == ["CW8 -4.2 %", "Au-delà de ±3 % depuis 14:00"]


@pytest.mark.parametrize("cls,src", [(sensors.MetricsSensor, "metrics"), (sensors.MarketsSensor, "markets")])
def test_feed_stubs_unconfigured_or_unreadable_are_health_failures(cls, src):
    s = cls(reader=lambda p: None)
    assert s.poll(NOW, {src: {"file": None}}) == [] and s.last_error == "not_configured"
    assert s.poll(NOW, {src: {"file": "/fake/x.json"}}) == [] and s.last_error == "feed_unreadable"


def test_contract_rejects_off_schema_rows():
    ok = store.make("mail", "other", ts=NOW, key="k", who="A", subject="s", ref={})
    assert contract.problems(ok) == [] and contract.problems(store.gone(ok, ts=NOW)) == []
    assert "private event carries a subject" in contract.problems(dict(ok, private=True))
    assert "missing ref" in contract.problems({k: v for k, v in ok.items() if k != "ref"})
    assert "bad source" in contract.problems(dict(ok, source="shots"))
    assert "unexpected subject" in contract.problems(dict(store.gone(ok, ts=NOW), subject="leak"))
    assert set(contract.LEVEL3) == set(store.TOKEN_SOURCES)


def test_every_settings_source_has_a_contract_test():
    """A new connector (a settings source) must come with its contract test in this file."""
    here = open(__file__, encoding="utf-8").read()
    for src in config.SOURCES:
        assert f"def test_{src}_connector" in here or f"def test_{src}_alerts_connector" in here, src


def test_local_level3_is_withheld_for_a_private_event():
    ev = store.make("metric", "aum", ts=NOW, key="aum", who="AUM", subject="AUM below 5",
                    ref={"value": 4, "threshold": 5}, private=True)
    assert "summary" not in glide.event_card(ev)


# ---- step 2: cockpit decisions, calendar, pull requests -------------------------------------------

def _queue(*items, **flags):
    return {"items": list(items), "total": len(items), "prs_known": True, "prs_unavailable": False,
            "board_unavailable": False, "gates_unreadable": [], **flags}


GATE = {"id": "gate:lyra:g7", "type": "gate", "agent": "Lyra", "slug": "lyra", "title": "Relance Acme",
        "risk": "high", "since": "2026-10-21T08:00:00Z", "href": "/gate/lyra/g7",
        "detail": ["Mail sortant"]}                     # titles only


def test_decisions_connector(monkeypatch):
    monkeypatch.setattr(glide, "_now", lambda: datetime.fromtimestamp(NOW, timezone.utc))
    w = {"state": "off"}
    resp = {"off": (200, _queue()), "on": (200, _queue(GATE)), "broken": (None, None)}
    seen = []

    def fetch(url, token):
        seen.append((url, token))
        return resp[w["state"]]
    s = sensors.DecisionsSensor(token=lambda: "tok", fetch=fetch, claude_cfg=lambda: {})
    cfg = _cfg({"proactive": {"agents": {"bots": {"lyra": "LyraTeamBot"}}}})
    ev = check_connector(s, cfg, lambda st: w.update(state=st), now=NOW, names=dict(NAMES, lyra="Lyra"))
    assert seen[0] == (DASH + "/a-decider.json", "tok")      # the bearer goes in a header (http_json)
    assert store.token(ev) == "evt:cockpit:decide-gate" and ev["who"] == "Lyra"
    assert ev["ref"]["url"] == DASH + "/gate/lyra/g7"
    opts, card = levels(ev, dict(NAMES, lyra="Lyra"))
    assert [o["label"] for o in opts] == ["Ouvrir la décision", "Approuver (Telegram)", "Ask Lyra"]
    assert opts[1]["params"] == {"chat": "Lyra", "bot": "LyraTeamBot"}
    assert card["what"] == "Lyra · Relance Acme · depuis 10:00"
    assert card["summary"] == [*GATE["detail"], "risque élevé · en attente depuis 10:00"]


def test_decisions_unknown_parts_neither_start_nor_end():
    pr = {"id": "pr:acme-org/rocket#801", "type": "pr", "title": "Nav", "href": "/pr/acme-org/rocket/801"}
    card = {"id": "board:7", "type": "board", "agent": "Atlas", "slug": "rnd", "title": "Choisir", "href": "/kanban/card/7"}
    w = {"q": _queue(GATE, pr, card)}
    s = sensors.DecisionsSensor(token=lambda: "tok", fetch=lambda u, t: (200, w["q"]), claude_cfg=lambda: {})
    cfg = _cfg({})
    assert len(s.poll(NOW, cfg)) == 3                                       # waiting at start = live state
    w["q"] = _queue(prs_known=False, board_unavailable=True, gates_unreadable=["lyra"])
    assert s.poll(NOW + 120, cfg) == [] and s.last_error is None            # parts unknown: nothing ends
    w["q"] = _queue()
    assert sorted(e["state"] for e in s.poll(NOW + 240, cfg)) == ["gone"] * 3


def test_decisions_without_credential_or_refused_is_a_health_failure():
    s = sensors.DecisionsSensor(token=lambda: None, fetch=lambda u, t: (200, _queue()), claude_cfg=lambda: {})
    assert s.poll(NOW, _cfg({})) == [] and s.last_error == "not_configured"
    calls = []
    s = sensors.DecisionsSensor(token=lambda: calls.append(1) or "old", fetch=lambda u, t: (401, None), claude_cfg=lambda: {})
    assert s.poll(NOW, _cfg({})) == [] and s.last_error == "auth"
    s.poll(NOW + 120, _cfg({}))
    assert len(calls) == 2                                                 # a refused token is read again (rotated)


def test_decisions_private_item_carries_no_title_or_detail(monkeypatch):
    monkeypatch.setattr(sensors.pack, "leaks", lambda text, cfg: "Acme" in text)
    s = sensors.DecisionsSensor(token=lambda: "tok", fetch=lambda u, t: (200, _queue(GATE)), claude_cfg=lambda: {})
    ev = s.poll(NOW, _cfg({}))[0]
    assert ev["private"] and ev["subject"] is None and "detail" not in ev["ref"]
    assert "summary" not in glide.event_card(ev)


def test_keychain_secret_reads_without_printing():
    class P:
        returncode, stdout = 0, "s3cret\n"
    got = []
    assert sensors.keychain_secret("svc", "acct", run=lambda a, **k: got.append(a) or P()) == "s3cret"
    assert got[0][:2] == ["/usr/bin/security", "find-generic-password"] and got[0][-1] == "-w"
    P.returncode = 44
    assert sensors.keychain_secret("svc", "acct", run=lambda a, **k: P()) is None


def _meeting(start, end, **kw):
    return {"id": "E1", "title": "Point Acme", "start": start, "end": end, "who": ["Sam Martin", "Anne"],
            "join": "https://meet.google.com/abc-defg-hij", "cal": "Travail", **kw}


def test_calendar_connector(monkeypatch):
    monkeypatch.setattr(glide, "_now", lambda: datetime.fromtimestamp(NOW, timezone.utc))
    w = {"state": "off"}
    got = {"off": ("ok", []), "on": ("ok", [_meeting(NOW + 1800, NOW + 5400)]), "broken": ("permission", None)}
    s = sensors.CalendarSensor(reader=lambda h: got[w["state"]], claude_cfg=lambda: {})
    ev = check_connector(s, _cfg({}), lambda st: w.update(state=st), now=NOW)
    assert store.token(ev) == "evt:calendar:meeting" and ev["who"] == "Sam Martin, Anne"
    opts, card = levels(ev)
    assert [o["label"] for o in opts] == ["Rejoindre la réunion", "Ouvrir Calendrier"]
    assert opts[0]["params"] == {"href": "https://meet.google.com/abc-defg-hij"}
    assert card["what"] == "Point Acme · à 14:30 · Sam Martin, Anne"
    assert card["summary"] == ["Avec : Sam Martin, Anne", "14:30–15:30 · Travail", "Lien : meet.google.com"]
    from oi.proactive import judge
    assert "Sam Martin" in judge.query(ev)                            # level 3: the attendees' wiki pages


def test_calendar_meeting_ends_when_over_and_a_moved_one_is_new():
    w = {"m": [_meeting(NOW + 600, NOW + 1200)]}
    s = sensors.CalendarSensor(reader=lambda h: ("ok", w["m"]), claude_cfg=lambda: {})
    cfg = _cfg({})
    assert [e["state"] for e in s.poll(NOW, cfg)] == ["live"]              # upcoming at start shows
    assert s.poll(NOW + 900, cfg) == []                                     # under way: the same state
    assert [e["state"] for e in s.poll(NOW + 1300, cfg)] == ["gone"]       # over (no timer: its end)
    w["m"] = [_meeting(NOW + 3000, NOW + 3600)]
    s.poll(NOW + 1400, cfg)
    w["m"] = [_meeting(NOW + 4000, NOW + 4600)]                             # moved: gone + a new live
    assert sorted(e["state"] for e in s.poll(NOW + 1500, cfg)) == ["gone", "live"]


def test_calendar_reader_parses_and_never_asks():
    class P:
        returncode, stdout = 0, "PERM0"
    assert sensors.run_calendar(7200, run=lambda a, **k: P()) == ("permission", None)
    P.stdout = 'OK[{"id": "E1"}]'
    assert sensors.run_calendar(7200, run=lambda a, **k: P()) == ("ok", [{"id": "E1"}])
    P.stdout = "garbage"
    assert sensors.run_calendar(7200, run=lambda a, **k: P())[0] == "bad_output"
    assert "requestAccess" not in sensors.CALENDAR_JS and "save" not in sensors.CALENDAR_JS


def _gh_world(w):
    pr = {"number": 55, "title": "intent proposals", "url": "https://github.com/octocat/hello-world/pull/55",
          "createdAt": "2026-10-18T09:00:00Z", "author": {"login": "octocat"}, "isDraft": False}
    calls = []

    def gh(args):
        calls.append(args)
        if w["state"] == "broken":
            return None
        if args[:2] == ["search", "prs"]:
            return []
        if args[:2] == ["pr", "list"]:
            return [pr, dict(pr, number=56, isDraft=True)] if w["state"] == "on" else []
        if args[:2] == ["pr", "view"]:
            return {"body": "## Summary\n- Proposes 3 intents for the user\n", "comments": [
                {"body": "Independent review: REQUEST_CHANGES"}, {"body": "**Independent review:** APPROVE at abc123"}]}
    return gh, calls


def test_prs_connector():
    w = {"state": "off"}
    gh, calls = _gh_world(w)
    s = sensors.PullRequestSensor(gh=gh, claude_cfg=lambda: {})
    ev = check_connector(s, _cfg({"proactive": {"prs": {"repos": ["octocat/hello-world"]}}}), lambda st: w.update(state=st),
                         now=NOW)
    assert store.token(ev) == "evt:github:hello-world" and ev["subject"] == "#55 intent proposals"
    assert ev["ref"]["verdict"] == "APPROVE" and ev["ref"]["summary"] == "Proposes 3 intents for the user"
    assert sum(1 for a in calls if a[:2] == ["pr", "view"]) == 1            # level 3 read once, when it starts
    opts, card = levels(ev)
    assert opts[0]["params"] == {"href": "https://github.com/octocat/hello-world/pull/55"}
    assert card["summary"][:2] == ["Proposes 3 intents for the user", "Revue indépendante : validée"]


def test_prs_of_a_dashboard_owner_open_on_the_dashboard_page():
    pr = {"number": 9, "title": "x", "url": "https://github.com/acme-org/team-ops-nova/pull/9",
          "author": {"login": "octocat"}, "isDraft": False, "repository": {"nameWithOwner": "acme-org/team-ops-nova"}}
    s = sensors.PullRequestSensor(gh=lambda a: [pr] if a[0] == "search" else [] if a[1] == "list" else {},
                                  claude_cfg=lambda: {})
    ev = s.poll(NOW, _cfg({"proactive": {"prs": {"repos": []}, "cockpit": {"url": DASH, "pr_owners": ["acme-org"]}}}))[0]
    assert ev["ref"]["url"] == DASH + "/pr/acme-org/team-ops-nova/9"


def test_new_settings_sources_parse():
    c = _cfg({"proactive": {"calendar": {"horizon_min": 45},
                                                 "prs": {"repos": ["octocat/x", "bad repo", "a/b/c"]}}})
    assert c["calendar"]["horizon_min"] == 45 and c["prs"]["repos"] == ["octocat/x"]
    assert c["sources"]["decisions"] is False and config.TOKEN_SOURCE["decisions"] == "cockpit"
    assert set(sensors.default_sensors()) == set(config.SOURCES)


def test_decisions_token_is_the_scoped_read_keychain_item():
    assert config.TOKEN_KEYCHAIN == {"service": "os-intent-dashboard-read", "account": "oi"}   # never an admin bearer


def test_calendar_join_link_drops_its_passcode_and_is_never_logged(capsys):
    m = _meeting(NOW + 600, NOW + 1200, join="https://acme.zoom.us/j/123456789?pwd=SECRETPASS#success")
    s = sensors.CalendarSensor(reader=lambda h: ("ok", [m]), claude_cfg=lambda: {})
    ev = s.poll(NOW, _cfg({}))[0]
    assert ev["ref"]["join"] == "https://acme.zoom.us/j/123456789"
    s.reader = lambda h: ("osa_error", None)
    s.poll(NOW + 60, _cfg({}))
    out = capsys.readouterr()
    assert "zoom" not in out.out + out.err and "SECRETPASS" not in out.out + out.err
    assert sensors.join_link("http://x.example/j") is None and sensors.join_link("javascript:x") is None


class _Wiki:
    """The Mac's wiki copy, faked: {path: frontmatter}."""
    def __init__(self, meta):
        self._meta = meta

    def meta(self):
        return self._meta


WIKI = _Wiki({"shared/systems/engine-room.md": {"title": "Engine room", "sujet": "engine-room"},
              "shared/people/zorbec.md": {"title": "Zorbec Alma SARL", "sujet": "partenariat-z"}})


def test_decisions_carry_the_sujet_and_cards_show_it(monkeypatch):
    """The dashboard sends the sujet SLUG only; the label comes from the Mac's own wiki
    copy (the page declaring `sujet: <slug>`), else the slug; level 2 and 3 show « Sujet : <label> »."""
    monkeypatch.setattr(glide, "_now", lambda: datetime.fromtimestamp(NOW, timezone.utc))
    item = dict(GATE, sujet={"slug": "engine-room", "inferred": True})
    s = sensors.DecisionsSensor(token=lambda: "tok", fetch=lambda u, t: (200, _queue(item)), claude_cfg=lambda: {},
                                wiki=lambda: WIKI)
    ev = s.poll(NOW, _cfg({}))[0]
    assert ev["ref"]["sujet"] == {"slug": "engine-room", "label": "Engine room", "inferred": True,
                                  "wiki": "shared/systems/engine-room.md"}
    card = glide.event_card(ev)
    assert card["what"].endswith("Sujet : Engine room (déduit)")
    assert card["summary"][0] == "Sujet : Engine room (déduit)"
    nopage = dict(GATE, sujet={"slug": "boucle-nova", "inferred": False})
    s2 = sensors.DecisionsSensor(token=lambda: "tok", fetch=lambda u, t: (200, _queue(nopage)), claude_cfg=lambda: {},
                                 wiki=lambda: WIKI)
    assert s2.poll(NOW, _cfg({}))[0]["ref"]["sujet"] == {
        "slug": "boucle-nova", "label": "boucle-nova", "inferred": False}             # no page: the slug
    nowiki = sensors.sujet_ref({"slug": "engine-room"}, {}, lambda: None)
    assert nowiki["label"] == "engine-room" and "wiki" not in nowiki
    bad = dict(GATE, sujet={"slug": "Pas Un Slug"})
    s3 = sensors.DecisionsSensor(token=lambda: "tok", fetch=lambda u, t: (200, _queue(bad)), claude_cfg=lambda: {},
                                 wiki=lambda: WIKI)
    assert "sujet" not in s3.poll(NOW, _cfg({}))[0]["ref"]


def test_decisions_sujet_label_that_leaks_falls_back_to_the_slug(monkeypatch):
    monkeypatch.setattr(sensors.pack, "leaks", lambda text, cfg: "zorbec" in text.lower())
    item = dict(GATE, sujet={"slug": "partenariat-z"})
    s = sensors.DecisionsSensor(token=lambda: "tok", fetch=lambda u, t: (200, _queue(item)), claude_cfg=lambda: {},
                                wiki=lambda: WIKI)
    ev = s.poll(NOW, _cfg({}))[0]
    assert not ev["private"] and ev["ref"]["sujet"]["label"] == "partenariat-z"
    assert "zorbec" not in str(glide.event_card(ev)).lower()
    assert sensors.sujet_ref({"slug": "zorbec-deal"}, {}, lambda: WIKI) is None          # a leaking slug: no sujet
