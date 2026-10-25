"""Reply drafts that know your calendar: when a mail proposes times ("vendredi à 10h ou 14h", "Friday 2pm"), the
proposed slots are extracted locally, checked against the macOS calendars (EventKit, read-only, on this Mac, no
cloud API) and their free / busy state goes into the reply prompt, so the draft picks a slot that is actually free.

  extract_slots(text, now)  -> [(start, end)] naive local datetimes, at most MAX_SLOTS, in the order of the mail
  run_busy(t0, t1)          -> ("ok", [[start, end], ...]) epoch seconds | (error kind, None)
  slot_lines(slots, busy)   -> ["Fri 23/10 10:00-11:00: free", ...]
  calendar_context(mail)    -> the prompt's <calendar> block, "(none)" when nothing applies

Only busy intervals leave EventKit: never a title, an attendee, a location or a note. The reader never asks for
access (the same JXA path as the proactive calendar sensor: authorization status first, PERM<status> when not
granted; Calendars access is granted once at install), never writes. Fail soft: no proposed time, no access, a
timeout or any error = "(none)", the draft is made exactly as without a calendar.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta
from typing import Callable

MAX_SLOTS = 6
DEFAULT_MIN = 60           # a proposed time without an end or a duration is checked as a one-hour slot
HORIZON_DAYS = 60          # a slot further away is not checked
BUSY_TIMEOUT_S = 8

_WD = {"lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6,
       "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
       "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
_MON = {"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
        "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10,
        "nov": 11, "dec": 12}
_REL = {"aujourd'hui": 0, "aujourd’hui": 0, "today": 0, "demain": 1, "tomorrow": 1}
_WD_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_wd = "|".join(sorted(_WD, key=len, reverse=True))
_mon = "|".join(sorted(_MON, key=len, reverse=True))
# a day: [weekday] dd/mm[/yyyy] | [weekday] [le] dd month | [weekday] month dd | weekday | today / tomorrow
_DAY = re.compile(
    rf"(?:\b(?P<wd1>{_wd})\.?,?\s+)?(?:le\s+)?\b(?P<d1>[0-3]?\d)/(?P<m1>[01]?\d)(?:/(?P<y1>\d{{2,4}}))?\b"
    rf"|(?:\b(?P<wd2>{_wd})\.?,?\s+)?(?:le\s+|the\s+)?\b(?P<d2>[0-3]?\d)(?:er|st|nd|rd|th)?\s+(?:of\s+)?(?P<m2>{_mon})\b\.?"
    rf"|(?:\b(?P<wd3>{_wd})\.?,?\s+)?\b(?P<m3>{_mon})\.?\s+(?P<d3>[0-3]?\d)(?:st|nd|rd|th)?\b"
    rf"|\b(?P<wd>{_wd})\b\.?"
    rf"|(?P<rel>aujourd['’]hui|\btoday\b|\bdemain\b|\btomorrow\b)", re.I)
_T = r"(?:(?P<{p}h>[01]?\d|2[0-3])\s*(?:h|:)\s*(?P<{p}m>[0-5]\d)?(?!\d)(?:\s*(?P<{p}ap>am|pm|a\.m\.|p\.m\.))?" \
     r"|(?P<{p}h2>1[0-2]|0?[1-9])\s*(?P<{p}ap2>am|pm|a\.m\.|p\.m\.)|(?P<{p}noon>midi|noon))"
_TIME = re.compile(r"(?<![\d/:.])(?:" + _T.format(p="a") + r")(?:\s*(?:-|–|to|à|a)\s*(?:" + _T.format(p="b") + r"))?",
                   re.I)
_DUR = re.compile(r"\b(\d{2,3})\s*(?:min|mn|minutes)\b", re.I)
_CLAUSE = re.compile(r"[.!?;\n]+")


def _hm(m: re.Match, p: str) -> tuple[int, int] | None:
    if m.group(f"{p}noon"):
        return 12, 0
    if m.group(f"{p}h") is not None:
        h, mi, ap = int(m.group(f"{p}h")), int(m.group(f"{p}m") or 0), m.group(f"{p}ap")
    elif m.group(f"{p}h2") is not None:
        h, mi, ap = int(m.group(f"{p}h2")), 0, m.group(f"{p}ap2")
    else:
        return None
    if ap:
        if h > 12:
            return None
        pm = ap.lower().startswith("p")
        h = (h % 12) + (12 if pm else 0)
    return h, mi


def _day(m: re.Match, now: datetime) -> datetime | None:
    """The date a day match names (midnight, naive local); a weekday = its next occurrence from today."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    g = m.groupdict()
    if g.get("rel"):
        return today + timedelta(days=_REL[g["rel"].lower().replace("’", "'")])
    if g.get("wd"):
        return today + timedelta(days=(_WD[g["wd"].lower()] - today.weekday()) % 7)
    for i in ("1", "2", "3"):
        if g.get(f"d{i}"):
            d = int(g[f"d{i}"])
            mo = int(g[f"m{i}"]) if i == "1" else _MON[g[f"m{i}"].lower()]
            y = g.get("y1") if i == "1" else None
            year = (int(y) + 2000 if len(y) == 2 else int(y)) if y else today.year
            try:
                day = today.replace(year=year, month=mo, day=d)
            except ValueError:
                return None
            if not y and day < today - timedelta(days=1):
                try:
                    day = day.replace(year=year + 1)
                except ValueError:
                    return None
            return day
    return None


def extract_slots(text: str, now: datetime, *, default_min: int = DEFAULT_MIN) -> list[tuple[datetime, datetime]]:
    """Proposed times in `text` (French / English): in each clause, a time belongs to the closest day named before
    it (else the first one after it); a time with no day in its clause is not a slot. Past slots and slots beyond
    HORIZON_DAYS are dropped; a time already passed today on a bare weekday moves to next week."""
    slots: list[tuple[datetime, datetime]] = []
    dur = _DUR.search(text or "")
    minutes = int(dur.group(1)) if dur and 10 <= int(dur.group(1)) <= 240 else default_min
    for clause in _CLAUSE.split(text or ""):
        days = [(m.start(), m.end(), m) for m in _DAY.finditer(clause)]
        if not days:
            continue
        for t in _TIME.finditer(clause):
            if any(a <= t.start() < b for a, b, _ in days):
                continue                                  # "26/10" / "23 oct" are dates, not times
            a = _hm(t, "a")
            if a is None:
                continue
            before = [d for d in days if d[1] <= t.start()]
            dm = (before[-1] if before else days[0])[2]
            day = _day(dm, now)
            if day is None:
                continue
            start = day.replace(hour=a[0], minute=a[1])
            if dm.group("wd") and start <= now:
                start += timedelta(days=7)
            b = _hm(t, "b") if t.group("bh") or t.group("bh2") or t.group("bnoon") else None
            end = start.replace(hour=b[0], minute=b[1]) if b else None
            if not end or end <= start:
                end = start + timedelta(minutes=minutes)
            if start <= now or start > now + timedelta(days=HORIZON_DAYS) or (start, end) in slots:
                continue
            slots.append((start, end))
            if len(slots) >= MAX_SLOTS:
                return slots
    return slots


# ---- EventKit (read-only, busy intervals only) ------------------------------------------------------------------

# The busy intervals between argv[0] and argv[1] (epoch s) over every calendar: [start, end] only. Skipped: cancelled
# events, events marked free, events the user declined, all-day events unless marked unavailable (out of office).
BUSY_JS = r"""
ObjC.import('EventKit');
function run(argv){
 const st=$.EKEventStore.authorizationStatusForEntityType(0);
 if(st!==3)return 'PERM'+st;
 const store=$.EKEventStore.alloc.init;
 const t0=parseFloat(argv[0]), t1=parseFloat(argv[1]);
 const pred=store.predicateForEventsWithStartDateEndDateCalendars($.NSDate.dateWithTimeIntervalSince1970(t0),
   $.NSDate.dateWithTimeIntervalSince1970(t1), store.calendarsForEntityType(0));
 const evs=store.eventsMatchingPredicate(pred), out=[];
 for(let i=0;i<evs.count;i++){
  const e=evs.objectAtIndex(i);
  if(e.status===3||e.availability===1)continue;
  if(e.allDay&&e.availability!==3)continue;
  let declined=false;const at=e.attendees;
  if(at&&!at.isNil())for(let j=0;j<at.count;j++){const p=at.objectAtIndex(j);if(p.isCurrentUser&&p.participantStatus===3)declined=true;}
  if(declined)continue;
  out.push([e.startDate.timeIntervalSince1970, e.endDate.timeIntervalSince1970]);
 }
 return 'OK'+JSON.stringify(out);
}
"""


def run_busy(t0: float, t1: float, run: Callable = subprocess.run) -> tuple[str, list | None]:
    """("ok", [[start, end], ...]) | (error kind, None): permission, osa_timeout, osa_error, bad_output."""
    try:
        p = run(["osascript", "-l", "JavaScript", "-e", BUSY_JS, str(int(t0)), str(int(t1))],
                capture_output=True, text=True, timeout=BUSY_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "osa_timeout", None
    except OSError:
        return "osa_error", None
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        return "osa_error", None
    if out.startswith("PERM"):
        return "permission", None
    try:
        got = json.loads(out[2:]) if out.startswith("OK") else None
    except ValueError:
        got = None
    if not isinstance(got, list):
        return "bad_output", None
    busy = [[float(x[0]), float(x[1])] for x in got
            if isinstance(x, list) and len(x) == 2 and all(isinstance(v, (int, float)) for v in x)]
    return "ok", busy


def slot_lines(slots: list[tuple[datetime, datetime]], busy: list) -> list[str]:
    """One line per proposed slot: "Fri 23/10 10:00-11:00: free" / "...: busy" (busy = any overlap)."""
    out = []
    for s, e in slots:
        a, b = s.timestamp(), e.timestamp()
        taken = any(x < b and y > a for x, y in busy)
        out.append(f"{_WD_NAMES[s.weekday()]} {s:%d/%m} {s:%H:%M}-{e:%H:%M}: {'busy' if taken else 'free'}")
    return out


def calendar_context(mail: dict | None, *, now: datetime | None = None,
                     reader: Callable[[float, float], tuple[str, list | None]] | None = None,
                     log: Callable[[str], None] | None = None) -> str:
    """The <calendar> block for a reply prompt: the slots proposed in the mail (and the previous message of the
    thread) with their free / busy state; "(none)" when no time is proposed or the calendar cannot be read."""
    if not mail:
        return "(none)"
    now = now or datetime.now()
    prev = mail.get("previous") or {}
    text = "\n".join(x for x in (mail.get("subject"), mail.get("body"), prev.get("body")) if isinstance(x, str))
    slots: list = []
    try:
        slots = extract_slots(text, now)
        if not slots:
            return "(none)"
        kind, busy = (reader or run_busy)(min(s for s, _ in slots).timestamp(), max(e for _, e in slots).timestamp())
    except Exception as e:                       # fail soft: the draft is made as without a calendar
        kind, busy = type(e).__name__, None
    if log:
        log(f"calendar: {len(slots)} slots, {kind}")      # counts only, never a time or a title
    if kind != "ok" or busy is None:
        return "(none)"
    return "\n".join(slot_lines(slots, busy))
