"""Reply drafts read the calendar (read-only) to pick a free proposed slot. A fake calendar only."""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from oi import calendar_slots as cs, claude_actions as ca

NOW = datetime(2026, 10, 22, 15, 0)            # a Thursday, 15:00 local
RUN_BUSY = cs.run_busy                         # the real reader (conftest guards the module attribute)


def slots(text, now=NOW):
    return [(s.strftime("%a %d/%m %H:%M"), e.strftime("%H:%M")) for s, e in cs.extract_slots(text, now)]


def busy(*ranges):
    """[[start, end]] epoch from ("Fri 23/10 14:00", minutes) pairs, as the EventKit reader returns them."""
    out = []
    for when, minutes in ranges:
        d = datetime.strptime(f"2026 {when[4:]}", "%Y %d/%m %H:%M")
        out.append([d.timestamp(), d.timestamp() + minutes * 60])
    return out


# ---- extraction ------------------------------------------------------------------------------------------------

def test_french_and_english_proposals():
    assert slots("Seriez-vous disponible vendredi à 10h ou 14h ?") == [("Fri 23/10 10:00", "11:00"),
                                                                      ("Fri 23/10 14:00", "15:00")]
    assert slots("Could we talk Friday 10:00 or 2pm?") == [("Fri 23/10 10:00", "11:00"), ("Fri 23/10 14:00", "15:00")]
    assert slots("10h ou 14h vendredi ?") == [("Fri 23/10 10:00", "11:00"), ("Fri 23/10 14:00", "15:00")]


def test_dates_ranges_durations_and_relative_days():
    assert slots("On se voit lundi 26/10 de 9h30 à 10h15") == [("Mon 26/10 09:30", "10:15")]
    assert slots("Mardi 27 octobre à 11h, 30 min") == [("Tue 27/10 11:00", "11:30")]
    assert slots("October 30 at 9am") == [("Fri 30/10 09:00", "10:00")]
    assert slots("demain 14h-15h") == [("Fri 23/10 14:00", "15:00")]
    assert slots("tomorrow at noon") == [("Fri 23/10 12:00", "13:00")]


def test_no_slot_without_a_day_past_or_far_or_not_a_time():
    assert slots("Can we talk at 3pm?") == []                      # no day in the clause
    assert slots("Results for 2026 at 10:00") == []
    assert slots("Lundi 19/10 à 10h") == []                        # past
    assert slots("Le 26/10/2027 à 10h") == []                      # beyond the horizon
    assert slots("Thursday 10h") == [("Thu 29/10 10:00", "11:00")]  # today's 10:00 has passed: next week
    assert slots("") == [] and slots("Merci, à bientôt") == []


def test_at_most_max_slots():
    text = "lundi 9h, 10h, 11h, 12h, 13h, 14h, 15h, 16h"
    assert len(cs.extract_slots(text, NOW)) == cs.MAX_SLOTS


# ---- free / busy -----------------------------------------------------------------------------------------------

def test_slot_lines_mark_overlaps_busy():
    s = cs.extract_slots("vendredi à 10h ou 14h", NOW)
    assert cs.slot_lines(s, busy(("Fri 23/10 14:30", 30))) == ["Fri 23/10 10:00-11:00: free",
                                                               "Fri 23/10 14:00-15:00: busy"]
    assert cs.slot_lines(s, busy(("Fri 23/10 11:00", 60))) == ["Fri 23/10 10:00-11:00: free",   # touching = free
                                                               "Fri 23/10 14:00-15:00: free"]


def test_calendar_context_reads_only_the_proposed_window():
    asked = []

    def reader(t0, t1):
        asked.append((t0, t1))
        return "ok", busy(("Fri 23/10 14:00", 60))
    mail = {"subject": "Point", "body": "Vendredi à 10h ou 14h ?", "previous": None}
    assert cs.calendar_context(mail, now=NOW, reader=reader) == ("Fri 23/10 10:00-11:00: free\n"
                                                                 "Fri 23/10 14:00-15:00: busy")
    assert asked == [(datetime(2026, 10, 23, 10).timestamp(), datetime(2026, 10, 23, 15).timestamp())]


def test_calendar_context_fails_soft_and_reads_nothing_without_a_proposal(no_real_calendar):
    def boom(t0, t1):
        raise RuntimeError("x")
    mail = {"subject": "Point", "body": "Vendredi à 10h ?"}
    logs = []
    assert cs.calendar_context(mail, now=NOW, reader=lambda a, b: ("permission", None), log=logs.append) == "(none)"
    assert logs == ["calendar: 1 slots, permission"]                # counts only, never a time
    assert cs.calendar_context(mail, now=NOW, reader=boom) == "(none)"
    assert cs.calendar_context(mail, now=NOW, reader=lambda a, b: ("osa_timeout", None)) == "(none)"
    assert cs.calendar_context({"body": "Merci pour le devis"}, now=NOW,
                               reader=lambda a, b: (_ for _ in ()).throw(AssertionError("read"))) == "(none)"
    assert cs.calendar_context(None) == "(none)"
    assert cs.calendar_context(mail, now=NOW) == "(none)" and len(no_real_calendar) == 1   # the guarded default


def test_previous_message_slots_are_checked_too():
    mail = {"subject": "Re: point", "body": "Ça me va", "previous": {"body": "Je propose mardi 27/10 à 9h"}}
    assert cs.calendar_context(mail, now=NOW, reader=lambda a, b: ("ok", [])) == "Tue 27/10 09:00-10:00: free"


# ---- the EventKit reader (subprocess faked) --------------------------------------------------------------------

def fake_run(stdout, rc=0, exc=None):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, kw=kw)
        if exc:
            raise exc
        return subprocess.CompletedProcess(argv, rc, stdout, "")
    return run, seen


def test_run_busy_parses_and_never_requests_access():
    run, seen = fake_run('OK[[1761210000,1761213600],["x",1],[1,2,3]]\n')
    assert RUN_BUSY(1761200000, 1761220000, run=run) == ("ok", [[1761210000.0, 1761213600.0]])
    assert seen["argv"][:4] == ["osascript", "-l", "JavaScript", "-e"] and seen["argv"][5:] == ["1761200000",
                                                                                                "1761220000"]
    js = seen["argv"][4]
    assert "authorizationStatusForEntityType" in js and "requestAccess" not in js and "requestFullAccess" not in js
    for w in ("save", "remove", "commit", "title", "notes", "location"):    # read-only, busy intervals only
        assert w not in js
    assert RUN_BUSY(0, 1, run=fake_run("PERM0")[0]) == ("permission", None)
    assert RUN_BUSY(0, 1, run=fake_run("", rc=1)[0]) == ("osa_error", None)
    assert RUN_BUSY(0, 1, run=fake_run("garbage")[0]) == ("bad_output", None)
    assert RUN_BUSY(0, 1, run=fake_run("", exc=subprocess.TimeoutExpired("osascript", 8))[0]) == ("osa_timeout", None)
    assert RUN_BUSY(0, 1, run=fake_run("", exc=OSError())[0]) == ("osa_error", None)


# ---- the reply draft -------------------------------------------------------------------------------------------

MAIL = {"sender_name": "Sam Lee", "sender_address": "sam@example.com", "subject": "Point", "date": "d",
        "message_id": "<m@x>", "body": "Seriez-vous disponible vendredi à 10h ou 14h ?", "previous": None, "cc": []}


def test_reply_prompt_carries_the_free_busy_slots_forward_does_not():
    p = ca.mail_prompt("mail.reply", MAIL, [], "Fri 23/10 10:00-11:00: free\nFri 23/10 14:00-15:00: busy")
    assert "<calendar>\nFri 23/10 10:00-11:00: free\nFri 23/10 14:00-15:00: busy\n</calendar>" in p
    assert "first one marked free" in p
    assert "<calendar>\n(none)\n</calendar>" in ca.mail_prompt("mail.reply", MAIL, [])
    assert "<calendar>" not in ca.mail_prompt("mail.forward", MAIL, [], "x: free")


def _acts(calendar, prompts):
    return ca.ClaudeActions(Path("/nonexistent.sock"), runner=lambda p, m: prompts.append(p) or {"body": "Bonjour"},
                            sender=lambda p: {"event": "ok"}, osa=lambda s, a, timeout=6: (0, "OK:7"),
                            wiki=lambda q: [], mail_reader=lambda **kw: dict(MAIL), calendar=calendar,
                            settings_loader=lambda: dict(ca.CLAUDE_DEFAULTS))


def test_long_press_reply_reads_the_calendar_forward_does_not():
    prompts, seen = [], []
    acts = _acts(lambda mail: seen.append(mail["message_id"]) or "Fri 23/10 10:00-11:00: free", prompts)
    assert acts.press({"primitive": "mail.reply", "params": {}}, {"app": "Mail"}, 1, lambda: "plain")[0] == "mail.reply"
    assert seen == ["<m@x>"] and "Fri 23/10 10:00-11:00: free" in prompts[0]
    acts.press({"primitive": "mail.forward", "params": {}}, {"app": "Mail"}, 2, lambda: "plain")
    assert seen == ["<m@x>"]


def test_default_calendar_without_access_keeps_todays_draft(no_real_calendar):
    prompts = []
    acts = _acts(None, prompts)
    assert acts.press({"primitive": "mail.reply", "params": {}}, {"app": "Mail"}, 1, lambda: "plain")[0] == "mail.reply"
    assert "<calendar>\n(none)\n</calendar>" in prompts[0] and len(no_real_calendar) == 1
