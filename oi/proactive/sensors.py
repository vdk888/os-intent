"""W24: the state sensors. Local, READ-ONLY, each opt-in (settings `proactive.sources`).

Context is STATE, not timers (docs/DESIGN.md design rule): every sensor reads a STATE and reports its edges. `poll(now, cfg)
-> [event]` returns an appear row when a state starts (an unread mail, an unread agent chat, a new unopened file, an
open alert, a metric / market beyond its threshold) and a gone row (store.gone, token `evt:…:gone`) when that same
state ends (read / replied, chat read, file opened or removed, alert closed, back inside the threshold). An event
never expires by itself. What is already on at the first poll is live state too (agents, cockpit alerts, metrics,
markets: an event on the first poll), except mail and files, which PRIME (unread mail / files already there at
start are a backlog, not news: never an event, and no gone for them). An agent's live event is refreshed when its
pending count grows (a new message: same id, new n / since, through the gate and its rate limit again).
A read that fails (Mail / Telegram not running, a folder or a feed unreadable, a source enabled but not configured)
is "unknown" for the STATE: nothing starts, nothing ends. It is never "nothing" for HEALTH (docs/DESIGN.md « Behaviour
contract »): every such poll sets `last_error` (a short kind: osa_timeout, osa_error, bad_output, badge_unreadable,
chats_unreadable, folder_unreadable, folder_missing, aw_unreadable, feed_unreadable, alerts_unreadable,
not_configured) -> health/sensors.json,
`oi health`. Except `not_running` (Mail / Telegram closed: the user's choice, not a broken connector): health records
it as INFO (health.INFO_KINDS), never a failure; only an unreadable read while the app RUNS is a failure (same for
`watcher_not_running`: files are sensed by oi-watcher, whose own service health `oi health` counts).
A sensor never sends, never acts, never reads a body: titles / subjects / names / counts only. Collaborators are
injectable (tests use fakes; no real Mail, Telegram, network or folder is touched by the test suite).

  MailSensor     Mail's inbox per account (newest MAIL_SCAN messages): message id, sender, subject, date, read and
                 replied flags (AppleScript, Automation → Mail, only while Mail RUNS: it is never launched). A new
                 unread mail = `evt:mail:<agent|other>` (the engine's wiki judge makes a client's `client`), live until it is read or replied (or leaves the inbox;
                 a live mail beyond the scan is looked up by its id, MAIL_STATE). Both reads are bounded by MAIL_BUDGET_S: an account
                 too slow to answer is skipped and stays unknown, never fails the whole poll.
  AgentSensor    Telegram's Dock badge (the Dock's AX status label of its tile, DOCK_BADGE_JS; lsappinfo no longer
                 reports it on macOS 27) = the total unread count, 0 = every chat read. WHO wrote: the unread count of
                 each chat row (Accessibility, the same chat list the W23 reader walks: the row's title and its numeric
                 badge only, never a message text) when the client exposes its chat list; Telegram for macOS 13
                 (ru.keepcoder.Telegram) exposes NO window children at all (AXChildren = kAXErrorFailure, 2026-10-21),
                 so then the Mac agents' OWN local record of what they sent the user (OutboundLog: the telegram reply
                 tool calls in their session transcripts, after the user's last message to that bot in its delivery
                 ledger, and after the last moment the badge was seen at 0). An agent chat (settings `contact_map`,
                 oi/contact.py) with unread / unanswered messages = `evt:agent:<slug>` (ref: chat, n, since), live
                 until the badge is gone or the user writes to that agent. Telegram running but the state unreadable
                 (no badge, or neither the chat list nor any outbound record) = a failed poll (`last_error`, health).
  FolderSensor   new top-level entries of chosen folders (`proactive.files.folders`: Desktop, a Drive folder…), SENSED
                 BY oi-watcher (TCC denies ~/Desktop to this launchd Python; the signed watcher app has it): this
                 sensor reads its bucket `oi-watcher-file_<host>` (live / gone edges, path + class + mtime + size,
                 never content) and its read state per folder (status/oi-watcher-files.json). Hidden / partial
                 downloads / screenshots (W17 owns those) are skipped. `evt:file:<drive|desktop|downloads|documents|
                 folder slug>`, live until the file is opened (Spotlight's kMDItemLastUsedDate changes) or removed.
                 Watcher not running = INFO (`watcher_not_running`); a folder it cannot read = folder_unreadable /
                 folder_missing; aw-server unreadable = aw_unreadable.
  CockpitSensor  your own ops dashboard (optional, `proactive.cockpit.url`). outage = its unauthenticated liveness
                 probe (<url>/health-noauth) fails twice in a row (`evt:cockpit:outage`, live until the probe answers
                 again); alerts = the ids of a JSON feed file (`proactive.cockpit.alerts_file`, [{id, title, kind?,
                 path?, open?}]), live while listed and not `"open": false`. The feed is a local file so that no
                 dashboard credential is needed here (fixtures in tests).
  MetricsSensor  a local KPI feed file. Reads `proactive.metrics.file` ({"metrics": [{id, label, value, threshold, url?, owner?}]}): a KPI
                 below its threshold = `evt:metric:<id>`, live until it is back above (or no longer reported).
  MarketsSensor  a local market brief / price feed file. Reads
                 `proactive.markets.file` ({"brief": {date, url?, read?}, "moves": [{symbol, pct}]}): the current
                 brief while not read = `evt:market:brief`; a symbol beyond ±move_pct = `evt:market:move`, fired on
                 entering that state, live while it lasts, fired again only after it ended. Never a trading action.
  DecisionsSensor  the dashboard's decision queue (approval gates, cards waiting for a human, PRs waiting for a merge
                 decision) from GET <cockpit.url>/a-decider.json, a scoped read-only token from the Keychain
                 (config.TOKEN_KEYCHAIN):
                 `evt:cockpit:decide-gate|board|pr`, live while listed; a part the cockpit could not read is unknown.
  CalendarSensor   meetings starting within `calendar.horizon_min` or under way (EventKit via JXA, read-only, never
                 asks for access): `evt:calendar:meeting`, live until it ends; only a join link leaves the notes (no query: passcodes).
  PullRequestSensor  open PRs whose review is requested of the user + the open PRs of `prs.repos` (local `gh`,
                 read-only): `evt:github:<repo>`, live while open; level 3 = summary line + independent verdict.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
import urllib.error
import urllib.parse
from urllib.error import URLError

from .. import claude_actions as ca, contact, drill, vocab
from ..orchestrator import pack
from . import store

MAIL_SCAN = 8                 # newest messages read per account inbox per poll
MAIL_LOOKUP = 20              # live mails beyond the scan looked up by id per poll
MAIL_TIMEOUT_S = 25           # the bulk inbox scan takes ~5-6 s on the user's Mac (5 accounts), spikes to ~20 s when Mail is busy
MAIL_BUDGET_S = 20            # the scripts' own deadline (< MAIL_TIMEOUT_S): a slow account / lookup is skipped, the rest returns
MAIL_BACKOFF_S = 60           # after a timed-out Mail read, no Mail read for this long, doubled per timeout in a row ...
MAIL_BACKOFF_MAX_S = 600      # ... up to this; a read that answers resets it (a slow Mail is not hammered every poll_s)
MAIL_ERR_LOG_S = 3600         # a failing Mail read (Mail running) is logged at most once per hour
FILES_LIVE = 20               # new files followed per sensor (the oldest beyond are let go: gone)
SEEN_MAX = 2000
SEP, RS = "\x1f", "\x1e"
SKIP_PREFIXES = (".", "~$", "Screenshot", "Capture d’écran", "Capture d'écran")
PARTIAL_EXT = (".download", ".crdownload", ".part", ".partial", ".tmp")

# Read-only: never launches Mail (checked with `application "Mail" is running`), never marks / moves / sends.
# BULK per account: one Apple event per property for the newest maxN messages (`message id of (messages 1 thru n)`,
# ...), and the sender is split into name / address in the script (fromParts, no `extract name from` event per
# message). The former one-event-per-property-per-message loop (~280 events) took 33-37 s on the user's Mac and hit
# MAIL_TIMEOUT_S on every poll (2026-10-21 `oi health`: 6 failed polls in a row, osa_timeout); this takes ~5-6 s.
# BOUNDED (the whole scan timed out for hours in a row while Mail was open): only `messages 1 thru maxN` is resolved
# (no `count of messages` that walks the whole inbox; counted only when the range fails = an inbox under maxN), and
# every Apple event runs `with timeout of` what is left of the script's budget (argv 2, MAIL_BUDGET_S): an account
# that does not answer in time is SKIPPED, the others still return. A trailing "SKIPPED US <skipped> US <accounts>"
# record says how many; "TIMEOUT" = even the account list did not answer.
MAIL_NEW = r'''on run argv
	set maxN to (item 1 of argv) as integer
	set budget to (item 2 of argv) as integer
	set sep to character id 31
	set rs to character id 30
	if application "Mail" is not running then return "NOTRUNNING"
	set t0 to current date
	set out to ""
	set nSkip to 0
	set nAccts to 0
	tell application "Mail"
		try
			with timeout of budget seconds
				set vAccts to every account
			end timeout
		on error number en
			if en is -1712 then return "TIMEOUT"
			error number en
		end try
		set nAccts to count of vAccts
		repeat with a in vAccts
			set vLeft to my leftS(t0, budget)
			if vLeft < 1 then
				set nSkip to nSkip + 1
			else
				try
					with timeout of vLeft seconds
						set mb to mailbox "INBOX" of a
						set vIds to {}
						try
							set vMsgs to a reference to (messages 1 thru maxN of mb)
							set vIds to message id of vMsgs
						on error number en
							if en is -1712 then error number en
							set n to count of messages of mb
							if n > maxN then set n to maxN
							if n > 0 then
								set vMsgs to a reference to (messages 1 thru n of mb)
								set vIds to message id of vMsgs
							end if
						end try
						if (count of vIds) > 0 then
							set vFroms to {}
							try
								set vFroms to sender of vMsgs
							on error number en
								if en is -1712 then error number en
							end try
							set vSubjs to {}
							try
								set vSubjs to subject of vMsgs
							on error number en
								if en is -1712 then error number en
							end try
							set vWhens to {}
							try
								set vWhens to date received of vMsgs
							on error number en
								if en is -1712 then error number en
							end try
							set vReads to {}
							try
								set vReads to read status of vMsgs
							on error number en
								if en is -1712 then error number en
							end try
							set vReplies to {}
							try
								set vReplies to was replied to of vMsgs
							on error number en
								if en is -1712 then error number en
							end try
							set vRows to ""
							repeat with i from 1 to (count of vIds)
								set nm to ""
								set ad to ""
								try
									set vParts to my fromParts(item i of vFroms)
									set nm to item 1 of vParts
									set ad to item 2 of vParts
								end try
								set subj to ""
								try
									set subj to item i of vSubjs
									if subj is missing value then set subj to ""
								end try
								set vDate to ""
								try
									set vDate to ((item i of vWhens) as «class isot» as string)
								end try
								set vRead to "0"
								try
									if item i of vReads then set vRead to "1"
								end try
								set vReplied to "0"
								try
									if item i of vReplies then set vReplied to "1"
								end try
								set vRows to vRows & (item i of vIds) & sep & nm & sep & ad & sep & subj & sep & vDate & sep & vRead & sep & vReplied & rs
							end repeat
							set out to out & vRows
						end if
					end timeout
				on error number en
					if en is -1712 then set nSkip to nSkip + 1
				end try
			end if
		end repeat
	end tell
	return "OK" & rs & out & "SKIPPED" & sep & nSkip & sep & nAccts & rs
end run

on leftS(t0, budget)
	return budget - ((current date) - t0)
end leftS

on trimText(t)
	set ws to {space, tab, return, linefeed}
	repeat while t is not "" and (character 1 of t) is in ws
		if (count of t) = 1 then return ""
		set t to text 2 thru -1 of t
	end repeat
	repeat while t is not "" and (character -1 of t) is in ws
		if (count of t) = 1 then return ""
		set t to text 1 thru -2 of t
	end repeat
	return t
end trimText

on fromParts(s)
	set s to s as string
	set vLt to 0
	repeat with k from (count of s) to 1 by -1
		if character k of s is "<" then
			set vLt to k
			exit repeat
		end if
	end repeat
	if vLt > 0 and s ends with ">" then
		if vLt + 1 > (count of s) - 1 then
			set vAddr to ""
		else
			set vAddr to text (vLt + 1) thru -2 of s
		end if
		if vLt > 1 then
			set vName to my trimText(text 1 thru (vLt - 1) of s)
		else
			set vName to ""
		end if
		if (count of vName) > 1 and vName starts with "\"" and vName ends with "\"" then set vName to text 2 thru -2 of vName
		if vName is "" then set vName to vAddr
		return {vName, my trimText(vAddr)}
	end if
	set s to my trimText(s)
	return {s, s}
end fromParts
'''

# The state of given Message-IDs in the inbox (read-only): "<id> US <unread|read|replied|gone>" per record.
MAIL_STATE = r'''on run argv
	set budget to (item 1 of argv) as integer
	set sep to character id 31
	set rs to character id 30
	if application "Mail" is not running then return "NOTRUNNING"
	set t0 to current date
	set out to ""
	tell application "Mail"
		repeat with mid in (rest of argv)
			set vLeft to my leftS(t0, budget)
			if vLeft < 1 then exit repeat
			set vState to "gone"
			try
				with timeout of vLeft seconds
					set ms to (messages of inbox whose message id is (mid as string))
					if (count of ms) > 0 then
						set m to item 1 of ms
						set vState to "unread"
						if read status of m then set vState to "read"
						if was replied to of m then set vState to "replied"
					end if
				end timeout
			on error number en
				if en is -1712 then exit repeat
			end try
			set out to out & (mid as string) & sep & vState & rs
		end repeat
	end tell
	return "OK" & rs & out
end run

on leftS(t0, budget)
	return budget - ((current date) - t0)
end leftS
'''


def parse_mail_list(out: str) -> list[dict] | None:
    """MAIL_NEW's output -> [{message_id, name, address, subject, date, read, replied}] (None: Mail not running /
    error)."""
    parts = (out or "").split(RS)
    if not parts or parts[0].strip() != "OK":
        return None
    rows = []
    for rec in parts[1:]:
        f = rec.split(SEP)
        if len(f) < 6 or not f[0].strip():               # (the SKIPPED record too: mail_skipped)
            continue
        rows.append({"message_id": f[0].strip(), "name": f[1].strip(), "address": f[2].strip().lower(),
                     "subject": " ".join(f[3].split()), "date": f[4].strip(), "read": f[5].strip() == "1",
                     "replied": len(f) > 6 and f[6].strip() == "1"})
    return rows


def mail_skipped(out: str) -> tuple[int, int]:
    """MAIL_NEW's trailing record -> (accounts skipped past the budget, accounts); (0, 0) when absent."""
    for rec in (out or "").split(RS)[1:]:
        f = rec.split(SEP)
        if f[0].strip() == "SKIPPED" and len(f) >= 3:
            try:
                return int(f[1]), int(f[2])
            except ValueError:
                return 0, 0
    return 0, 0


def parse_mail_state(out: str) -> dict[str, str] | None:
    """MAIL_STATE's output -> {message_id: unread|read|replied|gone} (None: Mail not running / error)."""
    parts = (out or "").split(RS)
    if not parts or parts[0].strip() != "OK":
        return None
    got = {}
    for rec in parts[1:]:
        f = rec.split(SEP)
        if len(f) >= 2 and f[0].strip() and f[1].strip() in ("unread", "read", "replied", "gone"):
            got[f[0].strip()] = f[1].strip()
    return got


def private_mail(name: str, address: str, subject: str, ccfg: dict) -> bool:
    """W14 local-only domain or the W25 leaks gate on the name / subject."""
    if ca.local_only(ca.domain_of(address), ccfg):
        return True
    return pack.leaks(f"{name} {subject}", ccfg)


class States:
    """key -> the live event of a state that is on. `step(now, on, off, rev)`: `on` = {key: make()} for the keys whose
    state is on now, `off` = the keys known to be off (None: every key not in `on` is off; a key in neither is
    unknown and unchanged). A key that turns on = its event (make()), a live key that turns off = its gone row.
    `rev` = {key: a number that grows while the state stays on} (an agent's pending message count): a live key whose
    rev grew is REFRESHED = make() again under the live event's id (the same state, its ref updated, hinted again
    through the gate and its rate limit). The first step primes (its on keys are never events and never end in a
    gone) unless `primed` (the state at start is live state: shown on the first poll)."""

    def __init__(self, primed: bool = False):
        self.live: dict = {}
        self.rev: dict = {}
        self.pre: set | None = set() if primed else None

    def step(self, now: float, on: dict, off: set | None = None, rev: dict | None = None) -> list[dict]:
        rev = rev or {}

        def ended(k) -> bool:
            return k not in on and (off is None or k in off)
        if self.pre is None:
            self.pre = set(on)
            return []
        evs = []
        for k in [k for k in self.live if ended(k)]:
            self.rev.pop(k, None)
            evs.append(store.gone(self.live.pop(k), ts=now))
        self.pre = {k for k in self.pre if not ended(k)}
        for k, make in on.items():
            if k in self.pre:
                continue
            if k in self.live:
                old, new = self.rev.get(k), rev.get(k)
                if old is None or new is None or new <= old:
                    continue                                 # the same state, nothing new
                ev = make()
                if ev:                                       # the same state refreshed: the live event's id
                    ev["id"] = self.live[k]["id"]
                    self.live[k], self.rev[k] = ev, new
                    evs.append(ev)
                continue
            ev = make()
            if ev:
                self.live[k] = ev
                if k in rev:
                    self.rev[k] = rev[k]
                evs.append(ev)
        return evs


FLEET_DOMAINS = ("team.example",)   # fallback for settings `proactive.mail.team_domains`: your agents' own mail domain(s)


def sender_class(address: str, mail_cfg: dict, agents: set[str] | None = None) -> str:
    """"agent" (settings `proactive.mail.agent_senders`, or the fleet domain with a roster slug) or "other". Whether
    the sender is a client is the wiki judge's call (judge.py: it turns "other" into "client")."""
    a = (address or "").strip().lower()
    if a and (a in (mail_cfg.get("agent_senders") or []) or
              (ca.domain_of(a) in (tuple(mail_cfg.get("team_domains") or ()) or FLEET_DOMAINS)
               and a.split("@", 1)[0] in (agents or set()))):
        return "agent"
    return "other"


def mail_running() -> bool:
    """Mail.app runs (pgrep: no Apple Event, never launches it)."""
    try:
        return subprocess.run(["pgrep", "-xq", "Mail"], timeout=2).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _log(msg: str) -> None:
    from .engine import log                                  # the engine's log line (imported late: no cycle)
    log(msg)


class MailSensor:
    source = "mail"

    def __init__(self, *, osa: Callable = ca.osascript, claude_cfg: Callable[[], dict] = ca.claude_settings,
                 agents: Callable[[], set[str]] = lambda: set(), log: Callable[[str], None] = _log,
                 running: Callable[[], bool] = mail_running):
        self.osa, self.claude_cfg, self.agents = osa, claude_cfg, agents
        self.log, self.running = log, running
        self.last_error: str | None = None                   # the last poll's error kind (engine health), None = ok
        self.states = States()
        self.seen: dict[str, float] = {}
        self.err_logged: float | None = None
        self.backoff_s = 0.0                                 # current back-off after timed-out reads (0 = none)
        self.quiet_until = float("-inf")                     # no Mail read before this (back-off)
        self.skipped = False                                 # the last poll did not read (back-off): engine records no health

    def _read_failed(self, now: float, rc: int, out: str) -> None:
        """A read that is not OK while Mail runs = a failure (`last_error`, engine health) and one log line per
        MAIL_ERR_LOG_S (the return code only, never a subject or a name). Without it a script that stops compiling
        looks exactly like Mail closed. Mail not running: the state stays unknown, health shows `not_running` as
        info, no log line."""
        if (out or "").strip() == "NOTRUNNING" or (rc != 0 and not self.running()):
            self.last_error = "not_running"                  # Mail closed: health info (not a failure), no log, no back-off
            return
        if (out or "").strip() == "TIMEOUT":                 # the script's own deadline: a slow Mail, as rc -1
            rc = -1
        self.last_error = "osa_timeout" if rc == -1 else "osa_error" if rc != 0 else "bad_output"
        if rc == -1:                                         # a slow Mail: back off, doubled per timeout in a row
            self.backoff_s = min(MAIL_BACKOFF_MAX_S, self.backoff_s * 2 or MAIL_BACKOFF_S)
            self.quiet_until = now + self.backoff_s
        if self.err_logged is not None and now - self.err_logged < MAIL_ERR_LOG_S:
            return
        self.err_logged = now
        self.log(f"mail sensor: Mail read failed (rc={rc}); no mail event until it reads again")

    def _partial(self, now: float, skipped: int, accounts: int) -> None:
        """Some accounts did not answer within MAIL_BUDGET_S: not a failed poll (the others read), one log line per
        MAIL_ERR_LOG_S. Their mails stay unknown this poll (nothing starts or ends for them)."""
        if self.err_logged is not None and now - self.err_logged < MAIL_ERR_LOG_S:
            return
        self.err_logged = now
        self.log(f"mail sensor: {skipped}/{accounts} Mail account(s) skipped (slower than {MAIL_BUDGET_S} s)")

    def _remember(self, k, now: float) -> None:
        self.seen[k] = now
        if len(self.seen) > SEEN_MAX:
            for old, _ in sorted(self.seen.items(), key=lambda kv: kv[1])[: SEEN_MAX // 2]:
                if old not in self.states.live:
                    del self.seen[old]

    def lookup(self, ids: list[str], now: float) -> dict[str, str]:
        """{message_id: unread|read|replied|gone} for live mails outside the scan ({} when Mail cannot say). A failed
        read counts like a failed scan (`last_error`, engine health)."""
        if not ids:
            return {}
        rc, out = self.osa(MAIL_STATE, [str(MAIL_BUDGET_S), *ids[:MAIL_LOOKUP]], MAIL_TIMEOUT_S)
        got = parse_mail_state(out) if rc == 0 else None
        if got is None:
            self._read_failed(now, rc, out)
        return got or {}

    def poll(self, now: float, cfg: dict) -> list[dict]:
        self.skipped = now < self.quiet_until
        if self.skipped:                                     # backing off a slow Mail: no read, nothing starts or ends
            return []
        self.last_error = None
        rc, out = self.osa(MAIL_NEW, [str(MAIL_SCAN), str(MAIL_BUDGET_S)], MAIL_TIMEOUT_S)
        rows = parse_mail_list(out) if rc == 0 else None
        skipped, accounts = mail_skipped(out) if rows is not None else (0, 0)
        if rows is not None and accounts and skipped >= accounts:
            rows, rc, out = None, -1, ""                     # no account answered in time: a timed-out read
        if rows is None:
            self._read_failed(now, rc, out)
            return []                                        # Mail not running / read failed: unknown, nothing starts or ends
        self.backoff_s = 0.0                                 # Mail answered: the back-off resets
        if skipped:                                          # a partial read: what answered counts, the rest is unknown
            self._partial(now, skipped, accounts)
        first = self.states.pre is None
        if first and skipped:
            return []                                        # never prime on a partial read (its old mails would read as new)
        state = {r["message_id"]: ("replied" if r["replied"] else "read" if r["read"] else "unread") for r in rows}
        if not skipped:                                      # a slow account would only time the lookup out too
            state.update(self.lookup([m for m in self.states.live if m not in state], now))
        ccfg = None
        on: dict = {}
        for r in rows:
            mid = r["message_id"]
            new = mid not in self.seen
            self._remember(mid, now)
            if state[mid] != "unread" or not (new or first or mid in self.states.live or mid in self.states.pre):
                continue                                     # an old mail scrolled into the scan is not news

            def make(r=r):
                nonlocal ccfg
                ccfg = ccfg if ccfg is not None else self.claude_cfg()
                cls = sender_class(r["address"], cfg.get("mail") or {}, self.agents())
                return store.make("mail", cls, ts=now, key=r["message_id"], who=ca.first_name(r["name"], r["address"]),
                                  subject=r["subject"], ref={"message_id": r["message_id"], "address": r["address"],
                                                             "name": r["name"][:60]},
                                  private=private_mail(r["name"], r["address"], r["subject"], ccfg))
            on[mid] = make
        for mid in self.states.live:                         # live beyond the scan and still unread
            if mid not in on and state.get(mid) == "unread":
                on[mid] = lambda: None
        off = {m for m, st in state.items() if st != "unread"}
        return self.states.step(now, on, off)


# Telegram's chat list, read-only: [[title, unread]] per row. The title = the row's first text (as the W23 reader);
# unread = the row's badge: its last short numeric text after the title ("3", "99+", "1.2K") or a count in the
# row's description ("3 unread"). Nothing else of a row leaves the script (never a preview / message text).
TELEGRAM_UNREAD_JS = r"""
function kids(e){try{return e.uiElements();}catch(_){return [];}}
function role(e){try{return e.role();}catch(_){return '';}}
function texts(e,d,out){if(d>5)return out;for(const k of kids(e)){if(role(k)==='AXStaticText'){try{const v=k.value();if(v)out.push(String(v));}catch(_){}}texts(k,d+1,out);}return out;}
function rows(p){if(p.windows.length===0)return null;let q=[p.windows[0]];for(let d=0;d<8&&q.length;d++){const nq=[];for(const e of q){for(const k of kids(e)){const r=role(k);if(r==='AXTable'||r==='AXOutline'||r==='AXList'){const rs=kids(k).filter(x=>['AXRow','AXCell','AXGroup'].includes(role(x)));if(rs.length>=3)return rs;}nq.push(k);}}q=nq;}return null;}
function count(s){s=String(s).trim();let m=s.match(/^(\d{1,4})\+?$/);if(m)return parseInt(m[1]);m=s.match(/^(\d+(?:[.,]\d)?)[Kk]$/);if(m)return Math.round(parseFloat(m[1].replace(',','.'))*1000);return null;}
function run(argv){const se=Application('System Events');if(!se.processes.whose({name:'Telegram'}).length)return 'null';
const p=se.processes.byName('Telegram');const rs=rows(p);if(!rs)return 'null';const out=[];
for(const r of rs.slice(0,40)){const t=texts(r,0,[]);if(!t.length)continue;let n=0;
 for(let i=t.length-1;i>=1;i--){const c=count(t[i]);if(c!==null){n=c;break;}}
 if(!n){try{const m=String(r.description()||'').match(/(\d+)\s+unread/i);if(m)n=parseInt(m[1]);}catch(_){}}
 out.push([t[0].slice(0,40),n]);}
return JSON.stringify(out);}
"""


def telegram_unread() -> dict[str, int] | None:
    """{chat title: unread count} of Telegram's chat list rows (None: Telegram not running / no list readable)."""
    try:
        p = subprocess.run(["osascript", "-l", "JavaScript", "-e", TELEGRAM_UNREAD_JS],
                           capture_output=True, text=True, timeout=4)
        got = json.loads(p.stdout.strip() or "null") if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(got, list):
        return None
    out: dict[str, int] = {}
    for x in got:
        if isinstance(x, list) and len(x) == 2 and isinstance(x[0], str) and isinstance(x[1], int):
            out.setdefault(x[0], x[1])
    return out


_LABEL = re.compile(r'"label"\s*=\s*"([^"]*)"')


def _badge_count(label: str | None) -> int:
    """A Dock badge label -> a count: "" / none = 0, "12" = 12, "•" (a dot badge) = 1."""
    lab = (label or "").strip()
    if not lab:
        return 0
    digits = re.sub(r"\D", "", lab)
    return int(digits) if digits else 1


def telegram_dock_badge(run: Callable = subprocess.run) -> int | None:
    """Telegram's Dock badge from lsappinfo: 0 = no badge, None = Telegram not running / unreadable. On macOS 27
    lsappinfo prints no StatusLabel key at all (even for a badged app: Mail 337 read as 0 on 2026-10-21): no key =
    unreadable (None), never "no badge"."""
    try:
        p = run(["lsappinfo", "info", "-only", "StatusLabel", "Telegram"], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = p.stdout or ""
    if p.returncode != 0 or '"StatusLabel"' not in out:
        return None
    m = _LABEL.search(out)
    return _badge_count(m.group(1) if m else "")


# The Dock's own tile of Telegram, read-only: its AX status label (the badge text) only; 'null' = no tile.
DOCK_BADGE_JS = r"""
function run(argv){const se=Application('System Events');let items=[];
try{items=se.processes.byName('Dock').lists[0].uiElements();}catch(_){return 'null';}
for(const it of items){let n='';try{n=it.name();}catch(_){continue;}if(n!=='Telegram')continue;
 let s=null;try{s=it.attributes.byName('AXStatusLabel').value();}catch(_){s=null;}
 return JSON.stringify({label:(s===null||s===undefined)?'':String(s).slice(0,8)});}
return 'null';}
"""


def dock_tile_badge(run: Callable = subprocess.run) -> int | None:
    """Telegram's Dock badge from the Dock's AX tile: 0 = no badge, None = no tile / unreadable."""
    try:
        p = run(["osascript", "-l", "JavaScript", "-e", DOCK_BADGE_JS], capture_output=True, text=True, timeout=4)
        got = json.loads((p.stdout or "").strip() or "null") if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(got, dict) or not isinstance(got.get("label"), str):
        return None
    return _badge_count(got["label"])


def telegram_badge() -> int | None:
    """The Dock tile's badge, else lsappinfo's (when a macOS still reports it)."""
    n = dock_tile_badge()
    return n if n is not None else telegram_dock_badge()


def telegram_running() -> bool:
    """Telegram runs (pgrep: no Apple Event, never launches it)."""
    try:
        return subprocess.run(["pgrep", "-xq", "Telegram"], timeout=2).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


REPLY_TOOL = "mcp__plugin_telegram_telegram__reply"
OUT_TAIL = 4 << 20            # a transcript is first read from its last 4 MB (then only what was appended)
OUT_FILES = 2                 # newest transcripts per agent followed
OUT_KEEP_S = 2 * 86400        # sent timestamps kept this long
LEDGER_TAIL = 256 << 10
TEXT_MAX = 600                # one message's text for a level-3 summary (read only then, never kept)


def _ts(s) -> float | None:
    try:
        return vocab.parse_ts(str(s)).timestamp()
    except Exception:
        return None


def _tail_lines(path: Path, nbytes: int) -> list[str]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        data = f.read()
    lines = data.decode("utf-8", "replace").split("\n")
    return lines[1:] if size > nbytes else lines              # the first line of a tail is partial


def _replies(line: str, ids: set[str]) -> list[tuple[float, str]]:
    """The telegram replies to `ids` in one transcript line: [(ts, text)] (the main session only, no subagent)."""
    if REPLY_TOOL not in line:
        return []
    try:
        r = json.loads(line)
    except ValueError:
        return []
    if not isinstance(r, dict) or r.get("isSidechain") or r.get("type") != "assistant":
        return []
    t = _ts(r.get("timestamp"))
    content = (r.get("message") or {}).get("content") if isinstance(r.get("message"), dict) else None
    if t is None or not isinstance(content, list):
        return []
    out = []
    for c in content:
        inp = c.get("input") if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == REPLY_TOOL else None
        if isinstance(inp, dict) and str(inp.get("chat_id")) in ids:
            out.append((t, inp.get("text") if isinstance(inp.get("text"), str) else ""))
    return out


class OutboundLog:
    """What the Mac agents sent the user on Telegram, from their OWN local records, read-only (no network, no bot
    token): the `reply` tool calls of their Claude session transcripts (settings `proactive.agents.outbound`:
    {slug: {transcripts: <~/.claude/projects/... dir>, state_dir: <~/.claude/channels/telegram-... dir>}}), and
    The user's last message to that bot (the plugin's delivery-ledger.jsonl in its state dir: update ids, user ids and
    timestamps only). VPS agents have no record on this Mac (their chats are only known through the Dock badge).
    The sensor keeps timestamps only; `texts` reads the message texts again, only for a level-3 summary (never kept,
    never logged)."""

    def __init__(self, *, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.files: dict[str, tuple[int, float, list[float]]] = {}   # path -> (offset read, mtime, sent ts)

    @staticmethod
    def _dir(p) -> Path | None:
        return Path(os.path.expanduser(p)) if isinstance(p, str) and p else None

    def transcripts(self, spec: dict) -> list[Path] | None:
        d = self._dir(spec.get("transcripts"))
        if d is None or not d.is_dir():
            return None
        try:
            fs = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:OUT_FILES]
        except OSError:
            return None
        return fs

    def last_inbound(self, spec: dict, ids: set[str]) -> float | None:
        """The user's last message to that agent's bot (its delivery ledger), None = none recorded / unreadable."""
        d = self._dir(spec.get("state_dir"))
        if d is None:
            return None
        best = None
        try:
            lines = _tail_lines(d / "delivery-ledger.jsonl", LEDGER_TAIL)
        except OSError:
            return None
        for ln in lines:
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and str(r.get("user_id")) in ids:
                t = _ts(r.get("ts"))
                best = t if t is not None and (best is None or t > best) else best
        return best

    def _sent(self, path: Path, ids: set[str]) -> list[float]:
        """The reply timestamps of one transcript, read incrementally (only what was appended since the last poll)."""
        st = path.stat()
        key = str(path)
        off, _, got = self.files.get(key, (-1, 0.0, []))
        if off < 0 or st.st_size < off:                     # new (or rewritten) file: its tail only
            off, got = max(0, st.st_size - OUT_TAIL), []
            skip_first = off > 0
        else:
            skip_first = False
        if st.st_size > off:
            with open(path, "rb") as f:
                f.seek(off)
                data = f.read(st.st_size - off)
            end = data.rfind(b"\n")
            if end >= 0:
                lines = data[:end].decode("utf-8", "replace").split("\n")
                if skip_first:
                    lines = lines[1:]
                for ln in lines:
                    got.extend(t for t, _ in _replies(ln, ids))
                off += end + 1
        cut = self.clock() - OUT_KEEP_S
        got = [t for t in got if t >= cut]
        self.files[key] = (off, st.st_mtime, got)
        return got

    def pending(self, spec: dict, ids: set[str], after: float | None = None) -> dict | None:
        """{"n", "first", "last"} of the messages sent to the user after his last message to that bot and after `after`
        (the badge last seen at 0); None = no record readable for that agent."""
        fs = self.transcripts(spec)
        if not fs:
            return None
        sent: list[float] = []
        for f in fs:
            try:
                sent += self._sent(f, ids)
            except OSError:
                continue
        floor = max(x for x in (self.last_inbound(spec, ids), after, float("-inf")) if x is not None)
        new = sorted(t for t in sent if t > floor)
        return {"n": len(new), "first": new[0] if new else None, "last": new[-1] if new else None}

    def texts(self, spec: dict, ids: set[str], after: float | None = None, limit: int = 3) -> list[str]:
        """The texts of the pending messages (newest `limit`, oldest first), read now from the transcripts' tails:
        only for a level-3 summary the user asked for by holding; nothing is cached."""
        fs = self.transcripts(spec) or []
        floor = max(x for x in (self.last_inbound(spec, ids), after, float("-inf")) if x is not None)
        got: list[tuple[float, str]] = []
        for f in fs:
            try:
                for ln in _tail_lines(f, OUT_TAIL):
                    got += [(t, x) for t, x in _replies(ln, ids) if t > floor and x]
            except OSError:
                continue
        got.sort(key=lambda r: r[0])
        return [" ".join(x.split())[:TEXT_MAX] for _, x in got[-limit:]]


class AgentSensor:
    source = "agents"

    def __init__(self, *, unread: Callable[[], dict[str, int] | None] = telegram_unread,
                 dock_badge: Callable[[], int | None] = telegram_badge,
                 table: Callable[[], dict[str, str]] = lambda: contact.contact_map({}),
                 titles: Callable[[], dict[str, str]] = lambda: contact.chat_titles({}),
                 names: Callable[[], dict[str, str]] = lambda: {},
                 outbound: "OutboundLog | None" = None, running: Callable[[], bool] = telegram_running):
        self.unread, self.dock_badge, self.table, self.titles, self.names = unread, dock_badge, table, titles, names
        self.outbound = outbound if outbound is not None else OutboundLog()
        self.running = running
        self.last_error: str | None = None                   # the last poll's error kind (engine health), None = ok
        self.read_at: float | None = None                    # the last poll that saw no badge: all read before it
        self.states = States(primed=True)                   # an agent already waiting at start is live state

    def agent_of(self, title: str, table: dict[str, str]) -> str | None:
        s = vocab.slug(title)
        best = max((k for k in table if s.startswith(k)), key=len, default=None)
        return table[best] if best else None

    def poll(self, now: float, cfg: dict) -> list[dict]:
        self.last_error = None
        if not self.running():
            self.last_error = "not_running"                  # Telegram closed: state unknown, health info (not a failure)
            return []
        dock = self.dock_badge()
        if dock is None:
            self.last_error = "badge_unreadable"             # Telegram runs, its badge cannot be read
            return []
        if dock == 0:                                        # no badge: every chat is read
            self.read_at = now
            return self.states.step(now, {}, None)
        chats = self.unread()
        if chats:
            return self._from_list(now, chats)
        return self._from_outbound(now, cfg)

    def _from_list(self, now: float, chats: dict[str, int]) -> list[dict]:
        table = self.table()
        on, off, rev = {}, set(), {}
        for title, n in chats.items():
            slug = self.agent_of(title, table)
            if not slug:
                continue
            if n > 0:
                on.setdefault(slug, lambda slug=slug, title=title, n=n: store.make(
                    "agent", slug, ts=now, key=[slug, now], who=self.names().get(slug) or slug.title(),
                    ref={"chat": title[:40], "slug": slug, "n": n}))
                rev.setdefault(slug, n)
            else:
                off.add(slug)
        return self.states.step(now, on, off - set(on), rev)  # a chat not in the list read: unknown

    def _from_outbound(self, now: float, cfg: dict) -> list[dict]:
        """The chat list is unreadable: who is waiting = the Mac agents' own record of what they sent the user."""
        a = cfg.get("agents") or {}
        ids = {str(x) for x in a.get("chat_ids") or []}
        titles = self.titles()
        on, off, rev, known = {}, set(), {}, False
        for slug, spec in (a.get("outbound") or {}).items():
            p = self.outbound.pending(spec, ids, after=self.read_at) if ids else None
            if p is None:
                continue                                     # no record for that agent: unknown
            known = True
            if not p["n"]:
                off.add(slug)
                continue
            ref = {"chat": (titles.get(slug) or slug.title())[:40], "slug": slug, "n": p["n"],
                   "since": vocab.iso_utc(datetime.fromtimestamp(p["first"], timezone.utc))}
            if isinstance((a.get("bots") or {}).get(slug), str):
                ref["bot"] = a["bots"][slug]
            on[slug] = lambda slug=slug, ref=ref, first=p["first"]: store.make(
                "agent", slug, ts=now, key=[slug, first], who=self.names().get(slug) or slug.title(), ref=ref)
            rev[slug] = p["n"]
        if not known:
            self.last_error = "chats_unreadable"             # badge on, but no chat list and no outbound record
            return []
        return self.states.step(now, on, off, rev)


def folder_class(path: str) -> str:
    p = os.path.realpath(os.path.expanduser(path))
    home = os.path.expanduser("~")
    if "/Library/CloudStorage/GoogleDrive" in p or "/Library/CloudStorage/" in p:
        return "drive"
    for name in ("Desktop", "Downloads", "Documents"):
        if p == os.path.join(home, name) or p.startswith(os.path.join(home, name) + "/"):
            return name.lower()
    return vocab.slug(os.path.basename(p), 24) or "folder"


FILES_BUCKET = "oi-watcher-file"   # oi-watcher's « files » bucket: oi-watcher-file_<host>
FILES_STATUS = "oi-watcher-files.json"   # status/: the watcher's read state per folder (FilesStatus)
FILES_STATUS_STALE_S = 90          # the watcher rewrites it every 30 s: older = the watcher is not running
FILES_BUCKETS_REFRESH_S = 30


def watcher_files_status() -> dict | None:
    """status/oi-watcher-files.json ({ts, pid, enabled, folders: {path: ok|unreadable|missing}}), None if unreadable."""
    from .. import paths
    st = read_json(str(paths.status_dir() / FILES_STATUS))
    return st if isinstance(st, dict) else None


class FolderSensor:
    """A thin reader of oi-watcher's file events (health check 2026-10-21: TCC denies ~/Desktop to this launchd
    Python, 148 failed polls in a row, last_ok null; the signed oi-watcher.app reads it). The
    watcher lists the folders, primes, and posts {"kind": "file", "state": "live" | "gone", "path", ...} to
    `oi-watcher-file_<host>`; this sensor turns those edges into the contract's live / gone rows. Health:
    status/oi-watcher-files.json says each folder's read state (unreadable / missing = a failed poll, its files
    unknown: nothing starts, nothing ends); no fresh status = the watcher is not running (INFO, not a failure:
    `oi health` counts the oi-watcher service itself); aw-server unreadable = a failed poll."""
    source = "files"

    def __init__(self, *, claude_cfg: Callable[[], dict] = ca.claude_settings,
                 buckets: Callable[[], list[str]] | None = None, fetch: Callable[[str, str], list[dict]] | None = None,
                 status: Callable[[], dict | None] = watcher_files_status):
        from .. import aw
        self.claude_cfg, self.status = claude_cfg, status
        self.buckets_fn, self.fetch = buckets or aw.list_buckets, fetch or aw.fetch_events
        self.since: str | None = None                        # primes: only the watcher's events from our start
        self.seen: set = set()                               # (bucket, event key) already applied at `since`
        self.files: dict[str, dict] = {}                     # path -> {"ts", "folder"}: live per the watcher
        self._buckets: list[str] = []
        self._buckets_at = float("-inf")
        self.states = States(primed=True)                   # priming is `since`: nothing live at the first poll
        self.last_error: str | None = None                   # the last poll's error kind (engine health), None = ok

    @staticmethod
    def wanted(name: str) -> bool:
        return not name.startswith(SKIP_PREFIXES) and not name.lower().endswith(PARTIAL_EXT)

    def _read(self, now: float) -> list[dict]:
        from .. import aw
        if self.since is None:
            self.since = vocab.iso_utc(datetime.fromtimestamp(now, timezone.utc))
        if now - self._buckets_at >= FILES_BUCKETS_REFRESH_S or not self._buckets:
            self._buckets_at = now
            self._buckets = [b for b in self.buckets_fn() if b.startswith(FILES_BUCKET + "_")]
        fresh = []                                           # (bucket key, event)
        for b in self._buckets:
            for ev in self.fetch(b, self.since):
                k = (b, aw._key(ev))
                if k not in self.seen:
                    self.seen.add(k)
                    fresh.append((k, ev))
        fresh.sort(key=lambda x: aw._sort_key(x[1]))
        if fresh and fresh[-1][1]["timestamp"] != self.since:   # the next read starts at the newest event
            self.since = fresh[-1][1]["timestamp"]               # (inclusive): only the events AT it are remembered
            self.seen = {k for k, e in fresh if e["timestamp"] == self.since}
        return [e for _, e in fresh]

    def poll(self, now: float, cfg: dict) -> list[dict]:
        folders = [os.path.expanduser(f) for f in ((cfg.get("files") or {}).get("folders") or [])]
        if not folders:
            self.last_error = "not_configured"
            return []
        st = self.status()
        try:
            age = now - vocab.parse_ts(str((st or {}).get("ts"))).timestamp()
        except (TypeError, ValueError):
            age = None
        if age is None or age > FILES_STATUS_STALE_S or (st or {}).get("enabled") is not True:
            self.last_error = "watcher_not_running"          # info: unknown, nothing starts or ends
            return []
        read = st.get("folders") if isinstance(st.get("folders"), dict) else {}
        failed = {d for d in folders if read.get(d) != "ok"}
        self.last_error = None
        if failed:
            states = {read.get(d) for d in failed}
            self.last_error = "folder_missing" if states == {"missing"} else "folder_unreadable"
        try:
            evs = self._read(now)
        except (URLError, OSError, ValueError):
            self.last_error = "aw_unreadable"                # unknown: nothing starts or ends
            return []
        for e in evs:
            d = e.get("data") or {}
            path = d.get("path")
            if d.get("kind") != "file" or not isinstance(path, str):
                continue
            if d.get("state") == "gone":
                self.files.pop(path, None)
            elif d.get("state") == "live" and os.path.dirname(path) in folders and self.wanted(os.path.basename(path)):
                self.files[path] = {"ts": vocab.parse_ts(e["timestamp"]).timestamp(), "folder": os.path.dirname(path)}
        self.files = {p: f for p, f in self.files.items() if f["folder"] in folders}   # a folder no longer chosen
        keep = sorted(self.files, key=lambda p: self.files[p]["ts"])[-FILES_LIVE:]
        ccfg = None
        on: dict = {}
        for path in keep:
            if path in self.states.live:
                on[path] = lambda: None                      # already live: the same state
                continue
            ccfg = ccfg if ccfg is not None else self.claude_cfg()
            f, n = self.files[path], os.path.basename(path)
            on[path] = lambda path=path, n=n, f=f, ccfg=ccfg: store.make(
                "file", folder_class(f["folder"]), ts=f["ts"], key=[path, f["ts"]], subject=n, ref={"path": path},
                private=pack.leaks(n, ccfg))
        for path in self.states.live:                        # a live file of an unreadable folder: unknown
            if path not in on and os.path.dirname(path) in failed:
                on[path] = lambda: None
        return self.states.step(now, on, None)


def http_ok(url: str, timeout: float = 4.0) -> bool:
    """GET <url> (no credential): True on HTTP 200."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout,
                                    context=ssl.create_default_context()) as r:
            return r.status == 200
    except (URLError, OSError, ValueError):
        return False


def read_json(path: str | None):
    if not path:
        return None
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class CockpitSensor:
    source = "cockpit"
    FAILS = 2

    def __init__(self, *, probe: Callable[[str], bool] = http_ok, reader: Callable = read_json):
        self.probe, self.reader = probe, reader
        self.fails = 0
        self.outage = States(primed=True)                   # an outage counts from start (two failed probes)
        self.alerts = States(primed=True)                   # an alert open at start is live state
        self.last_error: str | None = None                   # the last poll's error kind (engine health), None = ok

    def poll(self, now: float, cfg: dict) -> list[dict]:
        c = cfg.get("cockpit") or {}
        base = c.get("url") or ""
        self.last_error = None if base or c.get("alerts_file") else "not_configured"
        evs = []
        if base:
            self.fails = 0 if self.probe(base + "/health-noauth") else self.fails + 1
            on = {"outage": lambda: store.make("cockpit", "outage", ts=now, key=["outage", now], who="Cockpit",
                                               subject="Cockpit unreachable", ref={"url": base + "/"})} \
                if self.fails >= self.FAILS else {}
            evs += self.outage.step(now, on, None if self.fails == 0 else set())   # 1 failure: unknown
        if not c.get("alerts_file"):
            return evs                                       # no alert feed configured: the outage probe only
        feed = self.reader(c.get("alerts_file"))
        if not isinstance(feed, list):
            self.last_error = "alerts_unreadable"            # alerts unknown: nothing starts or ends, health says so
            return evs
        on = {}
        for a in feed:
            if not isinstance(a, dict) or not a.get("id") or a.get("open") is False:
                continue
            path = a.get("path") if isinstance(a.get("path"), str) and a["path"].startswith("/") else "/"
            on.setdefault(str(a["id"]), lambda a=a, path=path: store.make(
                "cockpit", a.get("kind") or "alert", ts=now, key=str(a["id"]), who="Cockpit",
                subject=str(a.get("title") or "")[:120], ref={"url": base + path, "id": str(a["id"])[:60],
                                                              "kind": str(a.get("kind") or "alert")[:24]}))
        return evs + self.alerts.step(now, on, None)


class MetricsSensor:
    """STUB (see the module doc): a local JSON feed until a read-only KPI source exists."""
    source = "metrics"

    def __init__(self, *, reader: Callable = read_json):
        self.reader = reader
        self.states = States(primed=True)                   # a KPI already below its threshold at start is live
        self.last_error: str | None = None

    def poll(self, now: float, cfg: dict) -> list[dict]:
        f = (cfg.get("metrics") or {}).get("file")
        feed = self.reader(f) if f else None
        self.last_error = None if isinstance(feed, dict) else "feed_unreadable" if f else "not_configured"
        if not isinstance(feed, dict):
            return []                                        # unknown: nothing starts or ends
        on = {}
        for m in feed.get("metrics") or []:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            v, th = m.get("value"), m.get("threshold")
            if not (isinstance(v, (int, float)) and isinstance(th, (int, float)) and v < th):
                continue
            k = str(m["id"])
            ref = {"slug": vocab.slug(k, 24), "value": v, "threshold": th}
            if isinstance(m.get("url"), str):
                ref["url"] = m["url"]
            if isinstance(m.get("owner"), str):
                ref["owner"] = m["owner"]
            on[k] = lambda k=k, m=m, ref=ref: store.make(
                "metric", k, ts=now, key=[k, now], who=str(m.get("label") or k)[:40],
                subject=f"{m.get('label') or k} below {m.get('threshold')}", ref=ref)
        return self.states.step(now, on, None)


class MarketsSensor:
    """STUB (see the module doc): a local JSON feed; never a trading action."""
    source = "markets"

    def __init__(self, *, reader: Callable = read_json):
        self.reader = reader
        self.states = States(primed=True)                   # an unread brief / a move beyond at start is live
        self.last_error: str | None = None

    def poll(self, now: float, cfg: dict) -> list[dict]:
        m = cfg.get("markets") or {}
        feed = self.reader(m.get("file")) if m.get("file") else None
        self.last_error = None if isinstance(feed, dict) else "feed_unreadable" if m.get("file") else "not_configured"
        if not isinstance(feed, dict):
            return []                                        # unknown: nothing starts or ends
        thr = float(m.get("move_pct") or 3.0)
        on = {}
        brief = feed.get("brief") if isinstance(feed.get("brief"), dict) else {}
        bdate = str(brief.get("date") or "")
        if bdate and brief.get("read") is not True:
            ref = {"slug": "orion", "date": bdate[:10]}
            if isinstance(brief.get("url"), str):
                ref["url"] = brief["url"]
            on[("brief", bdate)] = lambda ref=ref: store.make("market", "brief", ts=now, key=["brief", bdate], who="Orion",
                                                              subject="Daily brief", ref=ref)
        # the state = a symbol beyond ±move_pct (its direction); a move fires on entering it, again only after it ended
        for x in feed.get("moves") or []:
            if not (isinstance(x, dict) and x.get("symbol") and isinstance(x.get("pct"), (int, float))
                    and abs(x["pct"]) >= thr):
                continue
            s, way = str(x["symbol"]), "up" if x["pct"] > 0 else "down"
            on[("move", s, way)] = lambda s=s, way=way, pct=x["pct"]: store.make(
                "market", "move", ts=now, key=["move", s, way, now], who=s[:12], subject=f"{s} moved {way}",
                ref={"slug": "orion", "pct": pct, "move_pct": thr})
        return self.states.step(now, on, None)


# ---- the dashboard's decision queue (« À décider ») -------------------------------

DECIDE_CLASS = {"gate": "decide-gate", "board": "decide-board", "pr": "decide-pr"}
DETAIL_LINES, DETAIL_MAX = 4, 160
_ITEM_SLUG = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def keychain_secret(service: str, account: str, run: Callable = subprocess.run) -> str | None:
    """A generic password from the login Keychain (`security … -w`), None when absent. Never logged or printed."""
    try:
        p = run(["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    v = (p.stdout or "").strip() if p.returncode == 0 else ""
    return v or None


def http_json(url: str, token: str, timeout: float = 10.0) -> tuple[int | None, object]:
    """GET <url> with the bearer in a header (never in the URL): (HTTP status | None on a network error, JSON | None)."""
    req = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {token}",
                                                             "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            return r.status, json.loads(r.read(4 << 20).decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except (URLError, OSError, ValueError):
        return None, None


def _lines(v, n: int = DETAIL_LINES) -> list[str]:
    return [" ".join(x.split())[:DETAIL_MAX] for x in (v if isinstance(v, list) else []) if isinstance(x, str) and x.strip()][:n]


_SUJET_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")


def _default_wiki():
    from .. import rag                                       # lazy: the warm local wiki index (oi/rag.py)
    return rag.default()


def sujet_page(slug: str, wiki: Callable = _default_wiki) -> tuple[str, str] | None:
    """(path, title) of the page of the Mac's OWN wiki copy whose frontmatter declares `sujet: <slug>`, or None."""
    try:
        w = wiki()
        meta = w.meta() if w is not None else {}
    except Exception:
        return None
    for p in sorted(meta):
        if str(meta[p].get("sujet") or "").strip().lower() == slug:
            return p, str(meta[p].get("title") or p)
    return None


def sujet_ref(s, ccfg: dict, wiki: Callable = _default_wiki) -> dict | None:
    """The item's « sujet » (the board's sujet:<slug>) as {slug, label, inferred, wiki?}. The
    dashboard sends the SLUG only (it never ships page content): the label is resolved HERE from the
    Mac's own wiki copy (the page declaring `sujet: <slug>`, `wiki` = its path), else it is the slug. A label that
    would leak (pack.leaks) falls back to the slug; a slug that would leak = no sujet. None when absent / malformed."""
    if not isinstance(s, dict) or not isinstance(s.get("slug"), str) or not _SUJET_SLUG.match(s["slug"]):
        return None
    slug = s["slug"]
    if pack.leaks(slug.replace("-", " "), ccfg):
        return None
    out = {"slug": slug, "label": slug, "inferred": bool(s.get("inferred"))}
    page = sujet_page(slug, wiki)
    if page:
        out["wiki"] = page[0][:200]
        title = " ".join(page[1].split())[:80]
        if title and not pack.leaks(title, ccfg):
            out["label"] = title
    return out


class DecisionsSensor:
    """The dashboard's decision queue (approval gates, board cards waiting for a human, PRs waiting for a merge
    decision) read from GET <cockpit.url>/a-decider.json with a SCOPED read-only token (that route only, 403
    elsewhere; never an admin bearer), read from the Keychain
    (config.TOKEN_KEYCHAIN; never in settings, never logged). The JSON carries titles only (+ each item's « sujet »:
    `ref.sujet`, see sujet_ref). One live event per waiting item (`evt:cockpit:decide-
    gate|board|pr`), live while the cockpit lists it; what is waiting at start shows. A part the cockpit could not
    read (structural PRs not known, the board unreadable, a dept's gates unreadable) is UNKNOWN: its live items
    neither start nor end. No credential = `not_configured`, a refused one = `auth` (health failures)."""
    source = "decisions"

    def __init__(self, *, token: Callable[[], str | None] | None = None, fetch: Callable = http_json,
                 claude_cfg: Callable[[], dict] = ca.claude_settings, wiki: Callable = _default_wiki):
        from .config import TOKEN_KEYCHAIN
        self.wiki = wiki                                    # the Mac's wiki copy: a sujet slug -> its page title
        self.token = token or (lambda: keychain_secret(TOKEN_KEYCHAIN["service"], TOKEN_KEYCHAIN["account"]))
        self.fetch, self.claude_cfg = fetch, claude_cfg
        self.states = States(primed=True)                   # a decision already waiting at start is live state
        self.last_error: str | None = None
        self._tok: str | None = None

    @staticmethod
    def unknown(key: str, d: dict) -> bool:
        kind = key.split(":", 1)[0]
        if kind == "pr":
            return not d.get("prs_known") or bool(d.get("prs_unavailable"))
        if kind == "board":
            return bool(d.get("board_unavailable"))
        parts = key.split(":")
        return kind == "gate" and len(parts) > 2 and parts[1] in (d.get("gates_unreadable") or [])

    def poll(self, now: float, cfg: dict) -> list[dict]:
        base = ((cfg.get("cockpit") or {}).get("url") or "").rstrip("/")
        self._tok = self._tok or (self.token() if base else None)   # held in memory only, never logged
        if not base or not self._tok:
            self.last_error = "not_configured"               # no cockpit URL, or no credential in the Keychain
            return []
        status, d = self.fetch(base + "/a-decider.json", self._tok)
        if status in (401, 403):
            self._tok, self.last_error = None, "auth"        # re-read the Keychain next poll (a rotated token)
            return []
        if status != 200 or not isinstance(d, dict) or not isinstance(d.get("items"), list):
            self.last_error = "feed_unreadable"
            return []
        self.last_error = None
        ccfg = self.claude_cfg()
        bots = (cfg.get("agents") or {}).get("bots") or {}
        on: dict = {}
        for it in d["items"]:
            if not isinstance(it, dict) or it.get("type") not in DECIDE_CLASS or not isinstance(it.get("id"), str):
                continue
            key = it["id"][:200]
            title = str(it.get("title") or "")[:160]
            slug = it.get("slug") if isinstance(it.get("slug"), str) and _ITEM_SLUG.match(it["slug"]) else None
            priv = pack.leaks(f"{it.get('agent') or ''} {title}", ccfg)
            ref = {"url": base + it["href"] if isinstance(it.get("href"), str) and it["href"].startswith("/") else base + "/a-decider",
                   "id": key, "kind": it["type"]}
            for k in ("risk", "verdict", "since"):
                if isinstance(it.get(k), str) and it[k]:
                    ref[k] = it[k][:40]
            if slug:
                ref["slug"] = slug
                if isinstance(bots.get(slug), str):
                    ref["bot"] = bots[slug]
            if not priv:
                ref["detail"] = _lines(it.get("detail"))
                sj = sujet_ref(it.get("sujet"), ccfg, self.wiki)
                if sj:
                    ref["sujet"] = sj
            on[key] = lambda it=it, key=key, title=title, ref=ref, priv=priv: store.make(
                "cockpit", DECIDE_CLASS[it["type"]], ts=now, key=["decide", key],
                who=str(it.get("agent") or "Cockpit")[:60], subject=title, ref=ref, private=priv)
        off = {k for k in self.states.live if k not in on and not self.unknown(k, d)}
        return self.states.step(now, on, off)


# ---- Calendar (EventKit, read-only) ---------------------------------------------------------------------------

# The meetings that start within `horizon` seconds or are under way: id, title, start / end (epoch s), the other
# attendees' names, a join link (zoom / meet / teams / whereby / webex) found in its URL, location or notes — only
# that link leaves the script, never the notes — and the calendar's name. EventKit through JXA (no pyobjc): it never
# asks for access (PERM<status> when not granted: the install step grants Calendars access once), never writes.
CALENDAR_JS = r"""
ObjC.import('EventKit');
function s(x){try{const v=ObjC.unwrap(x);return v?String(v):'';}catch(_){return '';}}
function run(argv){
 const st=$.EKEventStore.authorizationStatusForEntityType(0);
 if(st!==3)return 'PERM'+st;
 const store=$.EKEventStore.alloc.init;
 const now=$.NSDate.date, t0=now.timeIntervalSince1970, h=parseFloat(argv[0])||7200;
 const pred=store.predicateForEventsWithStartDateEndDateCalendars(now.dateByAddingTimeInterval(-43200),
   now.dateByAddingTimeInterval(h), store.calendarsForEntityType(0));
 const evs=store.eventsMatchingPredicate(pred), out=[];
 const re=/https:\/\/[^\s<>"']*(zoom\.us|meet\.google\.com|teams\.microsoft\.com|teams\.live\.com|whereby\.com|webex\.com)[^\s<>"']*/;
 for(let i=0;i<evs.count;i++){
  const e=evs.objectAtIndex(i);
  if(e.allDay||e.status===3)continue;
  const a=e.startDate.timeIntervalSince1970, b=e.endDate.timeIntervalSince1970;
  if(b<=t0||a>t0+h)continue;
  const who=[];const at=e.attendees;
  if(at&&!at.isNil())for(let j=0;j<at.count&&who.length<8;j++){const p=at.objectAtIndex(j);if(p.isCurrentUser)continue;const n=s(p.name);if(n)who.push(n);}
  const u=(e.URL&&!e.URL.isNil())?s(e.URL.absoluteString):'';
  const m=(s(e.location)+' '+u+' '+s(e.notes)).match(re);
  out.push({id:s(e.calendarItemExternalIdentifier)||s(e.eventIdentifier),title:s(e.title),start:a,end:b,who:who,
            join:m?m[0]:'',cal:s(e.calendar.title)});
 }
 return 'OK'+JSON.stringify(out);
}
"""


def run_calendar(horizon_s: float, run: Callable = subprocess.run) -> tuple[str, list | None]:
    """("ok", meetings) | (error kind, None): permission, osa_timeout, osa_error, bad_output."""
    try:
        p = run(["osascript", "-l", "JavaScript", "-e", CALENDAR_JS, str(int(horizon_s))],
                capture_output=True, text=True, timeout=15)
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
    return ("ok", got) if isinstance(got, list) else ("bad_output", None)


def join_link(url) -> str | None:
    """A meeting's join link without its query / fragment (a passcode, e.g. Zoom's ?pwd=, lives there): kept in the
    event's ref only (local, 0600), never logged; the hud-log carries the option token `url.open:meeting` only."""
    try:
        u = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    except ValueError:
        return None
    if not u or u.scheme != "https" or not u.hostname:
        return None
    return urllib.parse.urlunsplit(("https", u.netloc, u.path or "/", "", ""))[:300]


def _iso(epoch) -> str | None:
    try:
        return vocab.iso_utc(datetime.fromtimestamp(float(epoch), timezone.utc))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class CalendarSensor:
    """Upcoming meetings (macOS Calendar, every account it syncs): a meeting starting within `calendar.horizon_min`
    (default 2 h) or under way = `evt:calendar:meeting`, live until it ends (or is cancelled / moved beyond the
    horizon); a meeting moved to another time is a new state. Live at start (an upcoming meeting shows)."""
    source = "calendar"

    def __init__(self, *, reader: Callable[[float], tuple[str, list | None]] = run_calendar,
                 claude_cfg: Callable[[], dict] = ca.claude_settings):
        self.reader, self.claude_cfg = reader, claude_cfg
        self.states = States(primed=True)
        self.last_error: str | None = None

    def poll(self, now: float, cfg: dict) -> list[dict]:
        h = float((cfg.get("calendar") or {}).get("horizon_min") or 120) * 60
        kind, got = self.reader(h)
        if kind != "ok":
            self.last_error = kind                           # unknown: nothing starts or ends
            return []
        self.last_error = None
        ccfg = self.claude_cfg()
        on: dict = {}
        for m in got:
            if not isinstance(m, dict) or not m.get("id") or not isinstance(m.get("start"), (int, float)) \
                    or not isinstance(m.get("end"), (int, float)) or m["end"] <= now or m["start"] > now + h:
                continue
            who = [str(x)[:60] for x in (m.get("who") or []) if isinstance(x, str) and x.strip()][:8]
            title = str(m.get("title") or "")[:160]
            priv = pack.leaks(f"{title} {' '.join(who)}", ccfg)
            ref = {"start": _iso(m["start"]), "end": _iso(m["end"])}
            join = join_link(m.get("join"))
            if join and not priv:
                ref["join"] = join
            if not priv:
                ref["attendees"] = who
                if isinstance(m.get("cal"), str) and m["cal"]:
                    ref["calendar"] = m["cal"][:40]
            key = (str(m["id"])[:200], round(float(m["start"])))
            on[key] = lambda who=who, title=title, ref=ref, priv=priv, key=key: store.make(
                "calendar", "meeting", ts=now, key=list(key), who=(", ".join(who) or None) if not priv else None,
                subject=title or "Réunion", ref=ref, private=priv)
        return self.states.step(now, on, None)


# ---- GitHub pull requests awaiting the user (local `gh`, read-only) ----------------------------------------------

PR_FIELDS = "number,title,url,createdAt,author,isDraft"
_REVIEW = re.compile(r"Independent review:?\s*\**\s*(APPROVE|REQUEST[_ ]CHANGES|CHANGES[_ ]REQUESTED|COMMENT)", re.I)


def gh_json(args: list[str], run: Callable = subprocess.run, timeout: float = 25.0):
    """`gh <args>` -> parsed JSON, or None on any failure (not installed, not authenticated, network, bad output)."""
    try:
        p = run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout or "null")
    except ValueError:
        return None


def pr_digest(body: str | None, comments) -> dict:
    """Level 3 of a PR: its summary's first line and the last independent review verdict (APPROVE / REQUEST_CHANGES
    / COMMENT), from the description and the PR comments (an "Independent review: <VERDICT>" comment). Nothing else is kept."""
    line = None
    for ln in (body or "").splitlines():
        t = ln.strip().lstrip("-*• ").strip()
        if t and not t.startswith(("#", "<!--", "|", "```")):
            line = " ".join(t.split())[:DETAIL_MAX]
            break
    verdict = None
    for c in comments if isinstance(comments, list) else []:
        m = _REVIEW.search(str((c or {}).get("body") or "")) if isinstance(c, dict) else None
        if m:
            verdict = m.group(1).upper().replace(" ", "_").replace("CHANGES_REQUESTED", "REQUEST_CHANGES")
    return {k: v for k, v in (("summary", line), ("verdict", verdict)) if v}


class PullRequestSensor:
    """Open pull requests awaiting the user, through the local `gh` (authenticated as them; read-only): those whose
    review is requested of them (any owner) and the open ones of `prs.repos` (e.g. a knowledge-base repo where
    proposals wait for their merge). Drafts skipped. `evt:github:<repo>`, live while the PR is open and listed;
    what is open at start shows. Level 1 opens it (the dashboard's PR page for an owner listed in
    `cockpit.pr_owners`, else GitHub); level 3 =
    its summary line + the independent review verdict (read once when it starts). A failed `gh` read = unknown."""
    source = "prs"

    def __init__(self, *, gh: Callable = gh_json, claude_cfg: Callable[[], dict] = ca.claude_settings):
        self.gh, self.claude_cfg = gh, claude_cfg
        self.states = States(primed=True)
        self._owners: set[str] = set()                     # cockpit.pr_owners: PRs opened on the dashboard
        self.last_error: str | None = None

    def poll(self, now: float, cfg: dict) -> list[dict]:
        repos = (cfg.get("prs") or {}).get("repos") or []
        lists: list[tuple[str | None, object]] = [(None, self.gh(["search", "prs", "--review-requested=@me", "--state=open",
                                                                 "--json", PR_FIELDS + ",repository", "--limit", "50"]))]
        lists += [(r, self.gh(["pr", "list", "-R", r, "--state", "open", "--json", PR_FIELDS, "--limit", "50"]))
                  for r in repos]
        if any(not isinstance(got, list) for _, got in lists):
            self.last_error = "gh_unreadable"                # unknown: nothing starts or ends
            return []
        self.last_error = None
        ccfg = self.claude_cfg()
        base = ((cfg.get("cockpit") or {}).get("url") or "").rstrip("/")
        self._owners = set((cfg.get("cockpit") or {}).get("pr_owners") or [])
        on: dict = {}
        for repo, got in lists:
            for p in got:
                if not isinstance(p, dict) or p.get("isDraft") or not isinstance(p.get("number"), int):
                    continue
                full = repo or ((p.get("repository") or {}).get("nameWithOwner") if isinstance(p.get("repository"), dict) else None)
                if not isinstance(full, str) or "/" not in full:
                    continue
                key = f"{full}#{p['number']}"
                if key in on:
                    continue
                on[key] = lambda p=p, full=full, key=key: self._make(now, p, full, key, base, ccfg)
        return self.states.step(now, on, None)

    def _make(self, now: float, p: dict, full: str, key: str, base: str, ccfg: dict) -> dict | None:
        owner, name = full.split("/", 1)
        title = str(p.get("title") or "")[:160]
        priv = pack.leaks(title, ccfg)
        url = (f"{base}/pr/{owner}/{name}/{p['number']}" if owner in self._owners and base else
               p.get("url") if isinstance(p.get("url"), str) and p["url"].startswith("https://") else
               f"https://github.com/{full}/pull/{p['number']}")
        ref = {"url": url, "repo": full, "number": p["number"]}
        if isinstance(p.get("createdAt"), str):
            ref["since"] = p["createdAt"][:25]
        if not priv:                                         # level 3, read once when the state starts
            v = self.gh(["pr", "view", str(p["number"]), "-R", full, "--json", "body,comments"])
            if isinstance(v, dict):
                ref.update(pr_digest(v.get("body"), v.get("comments")))
        author = (p.get("author") or {}).get("login") if isinstance(p.get("author"), dict) else None
        return store.make("github", vocab.slug(name, 24) or "pr", ts=now, key=key, who=author or name,
                          subject=f"#{p['number']} {title}", ref=ref, private=priv)


def focus_on(path: Path | None = None) -> bool | None:
    """A macOS Focus is active (True / False), None when unreadable (Full Disk Access needed: then ignored)."""
    p = path or Path.home() / "Library/DoNotDisturb/DB/Assertions.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return any(bool(x.get("storeAssertionRecords")) for x in d.get("data") or [] if isinstance(x, dict))
    except AttributeError:
        return None


def default_sensors(live=None) -> dict[str, object]:
    """One sensor per settings source (built lazily by the engine; only enabled ones are ever polled)."""
    def agents():
        try:
            from .. import vocal
            return set(vocal.recipients(vocal._raw_settings()))
        except Exception:
            return set()

    def table():
        try:
            from .. import vocal
            return contact.contact_map(vocal._raw_settings())
        except Exception:
            return contact.contact_map({})
    def titles():
        try:
            from .. import vocal
            return contact.chat_titles(vocal._raw_settings())
        except Exception:
            return contact.chat_titles({})

    def names():
        try:
            from .. import vocal
            return {s: spec.get("name") or s.title() for s, spec in vocal.recipients(vocal._raw_settings()).items()}
        except Exception:
            return {}
    return {"mail": MailSensor(agents=agents), "agents": AgentSensor(table=table, titles=titles, names=names),
            "files": FolderSensor(), "cockpit": CockpitSensor(), "metrics": MetricsSensor(), "markets": MarketsSensor(),
            "decisions": DecisionsSensor(), "calendar": CalendarSensor(), "prs": PullRequestSensor()}
