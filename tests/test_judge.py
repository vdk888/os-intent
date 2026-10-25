"""The ONE relevance judge: any event weighed against the wiki. A temp wiki db (the real
wiki_search.py schema) with invented pages and a fake bag-of-words embedder: no real wiki, no Ollama, no mail."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from oi import orchestrator, rag
from oi.proactive import engine, judge, relevance, store

REPO = Path(__file__).resolve().parents[1]          # the os-intent checkout holding tools/wiki/wiki_search.py
NOW = datetime(2026, 10, 21, 14, 0, 0, tzinfo=timezone.utc).timestamp()

PAGES = {   # path: (frontmatter, body) — invented
    "shared/people/delacroix-paul.md": ("title: Paul Delacroix — Delacroix Conseil\ntype: person\n"
                                        "tags: [people, client, cgp]", "Paul runs Delacroix Conseil, a client since June."),
    "shared/people/vieux-marc.md": ("title: Marc Vieux\ntype: person\nstatus: stale\ntags: [people, prospect]",
                                    "Marc Vieux, an old prospect."),
    "atlas_rnd/radial-hud-build.md": ("title: Radial HUD build\ntype: project\ntags: [os-intent]",
                               "The radial HUD project on the Mac: glide, wish, rings."),
    "shared/systems/fund-nav.md": ("title: Fund NAV\ntype: reference\ntags: [fund]", "How the fund NAV is computed."),
    "shared/people/orion.md": ("title: Orion\ntype: reference\ntags: [people, orion, fund]", "Orion, the fund agent."),
    "shared/systems/engine-room.md": ("title: Engine room\ntype: reference\nsujet: engine-room\n"
                                      "description: The team reference page", "The team's reference page."),
}


def fake_embed(text: str, dim: int = 64) -> list[float]:
    v = [0.0] * dim
    for w in orchestrator.fold(text).split():
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dim] += 1.0
    return v


@pytest.fixture
def warm(tmp_path):
    ws = rag.load_wiki_search(str(REPO))
    db = tmp_path / "wiki.db"
    conn = ws.init_db(db)
    for p, (fm, body) in PAGES.items():
        title = fm.splitlines()[0].split(":", 1)[1].strip()
        content = f"---\n{fm}\n---\n# {title}\n{body}"
        conn.execute(ws._UPSERT_PAGE_SQL, (p, title, body, content, p.split("/")[0],
                                           json.dumps(fake_embed(f"{title}\n{body}")).encode(), time.time()))
    conn.commit()
    conn.close()
    return rag.WarmWiki(db, repo_dir=str(REPO), embedder=fake_embed)


def mail(who="Paul", name="Paul Delacroix", address="paul@delacroix-conseil.example", subject="Question sur le devis",
         private=False, key="<m@x>"):
    return store.make("mail", "other", ts=NOW, key=key, who=who, subject=subject, private=private,
                      ref={"message_id": key, "address": address, "name": name})


def test_page_kinds_from_frontmatter():
    assert judge.page_kind("shared/people/a.md", {"tags": "[people, client, cgp]"}) == "client"
    assert judge.page_kind("x/b.md", {"tags": "[lead, linkedin]"}) == "prospect"
    assert judge.page_kind("x/c.md", {"type": "project"}) == "project"
    assert judge.page_kind("x/d.md", {"type": "person"}) == "person"
    assert judge.page_kind("x/e.md", {"type": "project", "status": "stale"}) is None        # not active
    assert judge.page_kind("archive/f.md", {"tags": "[client]"}) is None
    assert judge.page_kind("shared/people/orion.md", {"type": "reference", "tags": "[people, orion]"}) is None


def test_query_uses_non_private_fields_only():
    assert judge.query(mail()) == "Paul Delacroix delacroix conseil Question sur le devis"
    assert judge.query(mail(address="paul.delacroix@gmail.com")) == "Paul Delacroix paul delacroix Question sur le devis"
    priv = mail(private=True)
    assert priv["subject"] is None and "devis" not in judge.query(priv)            # store.make dropped it
    agent = store.make("agent", "orion", ts=NOW, key="k", who="Orion", subject="NAV", ref={"slug": "orion"})
    assert judge.query(agent) == "NAV"                                              # the agent's name is no signal


def test_mail_from_an_active_client_page_is_important_with_page_and_snippet(warm):
    jd = judge.Judge(lambda: warm)
    ev = mail()
    v = judge.apply(ev, jd)
    assert v["important"] is True and v["reason"] == "wiki" and v["page"] == "shared/people/delacroix-paul.md"
    assert v["kind"] == "client" and v["snippet"].startswith("Paul Delacroix") and len(v["snippet"]) <= 200
    assert ev["wiki"] == v and ev["class"] == "client"                             # evt:mail:client: the wiki says so
    assert relevance.importance(ev, {"priorities": []}, {}) == "wiki"


def test_unknown_sender_and_stale_page_are_not_important(warm):
    jd = judge.Judge(lambda: warm)
    ev = mail(who="Zoe", name="Zoe Inconnue", address="zoe@nowhere.example", subject="Promo de la semaine", key="<z@x>")
    assert judge.apply(ev, jd)["important"] is False and "wiki" not in ev and ev["class"] == "other"
    old = mail(who="Marc", name="Marc Vieux", address="marc@vieux.example", subject="Bonjour", key="<v@x>")
    assert jd(old)["important"] is False                                           # status: stale


def test_agent_message_about_a_current_project_is_important(warm):
    jd = judge.Judge(lambda: warm)
    ev = store.make("agent", "atlas", ts=NOW, key="r", who="Atlas", subject="radial HUD build: the rings are ready",
                    ref={"slug": "atlas"})
    v = judge.apply(ev, jd)
    assert v["important"] and v["page"] == "atlas_rnd/radial-hud-build.md" and v["kind"] == "project"
    assert ev["class"] == "atlas"                                                   # only a mail's class changes
    one = store.make("agent", "atlas", ts=NOW, key="r1", who="Atlas", subject="radial menu idea", ref={"slug": "atlas"})
    assert jd(one)["important"] is False                                           # a project page needs two words
    plain = store.make("agent", "orion", ts=NOW, key="b", who="Orion", subject="ok", ref={"slug": "orion"})
    assert jd(plain)["important"] is False


def test_timeout_is_not_important_and_not_cached(warm):
    slow = {"on": True}

    def wiki():
        if slow["on"]:
            time.sleep(0.4)
        return warm
    jd = judge.Judge(wiki, timeout_s=0.05)
    t = time.perf_counter()
    assert jd(mail())["important"] is False
    assert time.perf_counter() - t < 0.3                                           # never waits past the budget
    time.sleep(0.5)
    slow["on"] = False
    assert jd(mail())["important"] is True                                         # the timeout was not cached


def test_verdicts_are_cached_per_query_and_no_wiki_is_not_important(warm, monkeypatch):
    calls = []
    real = warm.search
    monkeypatch.setattr(warm, "search", lambda *a, **kw: calls.append(a) or real(*a, **kw))
    jd = judge.Judge(lambda: warm)
    assert jd(mail())["important"] and jd(mail(key="<m2@x>"))["important"]
    assert len(calls) == 1                                                         # same sender + subject: cached
    assert judge.Judge()(mail())["important"] is False                             # conftest: no wiki -> not important


def test_private_event_keeps_no_snippet_and_the_events_file_keeps_the_page_name_only(warm, data_dir):
    jd = judge.Judge(lambda: warm)
    priv = mail(private=True, key="<p@x>")
    v = judge.apply(priv, jd)
    assert v["important"] and v["page"] == "shared/people/delacroix-paul.md" and v["snippet"] is None
    ev = mail(key="<q@x>")
    judge.apply(ev, jd)
    assert ev["wiki"]["snippet"]                                                   # in memory: the HUD's "why" later
    store.append(ev)
    row = store.read_day(ev["ts"][:10])[0]
    assert row["wiki"] == {"page": "shared/people/delacroix-paul.md"} and "Delacroix Conseil, a client" not in json.dumps(row)


def test_engine_judges_on_the_sensor_thread_before_storing(warm, data_dir, monkeypatch):
    class Sensor:
        def poll(self, now, cfg):
            return [mail()]

    class FakeLive:
        model = None
    monkeypatch.setattr(engine, "_raw_settings", lambda: {"proactive": {"sources": {"mail": True}}})
    eng = engine.Engine(FakeLive(), sensors={"mail": Sensor()}, raw_settings=engine._raw_settings, clock=lambda: NOW,
                        judge=judge.Judge(lambda: warm))
    got = eng.poll_once()
    assert got[0]["class"] == "client" and got[0]["wiki"]["page"] == "shared/people/delacroix-paul.md"
    assert store.token(eng.q.get_nowait()) == "evt:mail:client"


def test_generic_business_words_never_name_a_page():
    assert judge.words("Formation Training: facture du projet Delacroix") == {"delacroix"}
    assert {"training", "formation", "reunion", "invoice", "newsletter", "offre", "projet", "rapport"} <= judge.STOPWORDS


def test_mail_needs_its_sender_to_name_the_page_not_a_subject_word(warm):
    jd = judge.Judge(lambda: warm)
    # a stranger whose SUBJECT names the client: not important (a subject word alone never does)
    ev = mail(who="Zoe", name="Zoe Promo", address="news@promo-mailer.example", subject="Delacroix webinar", key="<s@x>")
    assert jd(ev)["important"] is False
    # the same subject from the client's company domain: important
    ev2 = mail(who="Info", name="Accueil", address="accueil@delacroix-conseil.example", subject="Delacroix webinar",
               key="<s2@x>")
    assert jd(ev2)["page"] == "shared/people/delacroix-paul.md"
    # an agent / system event still matches on its subject
    a = store.make("agent", "atlas", ts=NOW, key="r", who="Atlas", subject="radial HUD build ready", ref={"slug": "atlas"})
    assert jd(a)["important"] is True


def test_strong_cosine_on_a_mail_also_needs_the_sender(warm):
    jd = judge.Judge(lambda: warm, min_cos=0.0)                     # every hit passes the cosine
    ev = mail(who="Zoe", name="Zoe Inconnue", address="zoe@nowhere.example", subject="Paul Delacroix Conseil client",
              key="<c@x>")
    assert jd(ev)["important"] is False
    assert jd(mail(key="<c2@x>"))["important"] is True


def test_circuit_breaker_skips_the_judge_after_consecutive_timeouts(warm):
    slow, t = {"on": True}, {"now": 0.0}
    calls = []

    def wiki():
        calls.append(1)
        if slow["on"]:
            time.sleep(0.2)
        return warm
    jd = judge.Judge(wiki, timeout_s=0.02, clock=lambda: t["now"])
    for i in range(judge.BREAK_AFTER):
        assert jd(mail(subject=f"s{i}", key=f"<t{i}@x>"))["important"] is False
    time.sleep(0.25 * judge.BREAK_AFTER)                            # the queued slow searches drain
    slow["on"], n = False, len(calls)
    t0 = time.perf_counter()
    assert jd(mail())["important"] is False                          # circuit open: answered at once, no search
    assert time.perf_counter() - t0 < 0.01 and len(calls) == n
    t["now"] = judge.BREAK_S + 1                                     # after BREAK_S: the judge runs again
    assert jd(mail())["important"] is True


def _decision(sujet, private=False):
    return store.make("cockpit", "decide-gate", ts=NOW, key=["decide", "gate:rnd:g1"], who="Atlas",
                      subject="Valider la page", private=private,
                      ref={"url": "https://cockpit.example/gate/rnd/g1", "id": "gate:rnd:g1", "kind": "gate",
                           **({"sujet": sujet} if sujet else {})})


def test_a_decision_is_judged_on_its_sujet_page_first(warm):
    """The board sujet's own wiki page (frontmatter `sujet:`), exact, before any search."""
    jd = judge.Judge(lambda: warm)
    ev = _decision({"slug": "engine-room", "label": "Engine room"})
    v = judge.apply(ev, jd)
    assert v["important"] and v["reason"] == "sujet" and v["page"] == "shared/systems/engine-room.md"
    assert v["snippet"].startswith("Engine room") and ev["wiki"] == v
    linked = _decision({"slug": "autre-sujet", "label": "x", "wiki": "atlas_rnd/radial-hud-build.md"})
    assert jd(linked)["page"] == "atlas_rnd/radial-hud-build.md"                   # else the page the cockpit linked
    none = _decision({"slug": "sans-page", "label": "sans-page"})
    assert jd(none)["reason"] != "sujet"                                          # no page: the normal search
    assert jd(_decision(None))["reason"] != "sujet"
    priv = _decision({"slug": "engine-room", "label": "x"}, private=True)
    assert jd(priv)["reason"] != "sujet"
