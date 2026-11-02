"""proactive hints — state sensors -> relevance gate -> hint.

Goal: the mouse did not move, but the state changed (a mail arrived, an agent's message, the Drive, the markets,
a metric that dropped, an outage on the dashboard): propose the most likely next action.

  sensors.py    local, READ-ONLY STATE sensors, each opt-in (settings.json `proactive.sources`, all off by default):
                each reports an appear row when a state starts and a gone row when it ends (context is state, not
                timers: docs/DESIGN.md design rule). Mail (a new unread mail, until read / replied; AppleScript, only while
                Mail runs), agents (Telegram's Dock badge on + who wrote: the chat list when readable, else the
                Mac agents' own outbound record; until read or answered), files (a new entry in a chosen
                folder: Desktop, Drive, Downloads, until opened / removed), cockpit (your own ops dashboard, optional: outage
                while its liveness probe fails; alerts while open in a local JSON feed; its decision queue), calendar
                (a meeting about to start), PRs (review requested of you), metrics and markets (local JSON feeds:
                beyond a threshold while it lasts). Never a body, never an action, never a send.
  judge.py      the ONE relevance judge: any event -> a query of its non-private fields -> the warm
                local wiki search (oi/rag.py) -> important when an active client / prospect / current project / known
                person page is named by it (or matches strongly); {important, reason, page, snippet}. A mail it ties
                to a client / prospect page is class `client`.
  store.py      events/<UTC day>.jsonl (0600, local only: the subject / name stay here) and the tokens
                `evt:<source>:<class>` (+ `:gone` when its state ends; src oi-evt) that oi.normalise adds to the
                stream, and `Live` (the events live now): Markov / Laya learn "after
                event E the user usually does A" with the same training loop (an event is a moment and a history token,
                never a label: moments.py).
  relevance.py the gate per event: gone / paused / HUD off / denylisted front app, quiet hours and Focus, hard
                priorities (client mail, cockpit outage), the history probability of a follow-up (Markov counts at
                the event token, blended with the attention priors of profile.yaml while history is thin) >=
                `min_p`, at most one proactive hint per `min_interval_min` (display; priorities skip it).
  engine.py     run by `oi live`: a sensor thread queues events, the driver's loop drains them (token in the stream,
                gate, hint). ADDENDUM 2: an IMPORTANT event (relevance.importance: priority,
                the wiki judge, a profile counterpart / topic) OPENS
                the full cross with its actions, no window change or pointer move; sends stay behind the outward
                validation gate (engine.outward_gate marks them; oi.live asks oi/gate.py at the pick). Any other hint = the dots with `"marker":"ring"` (oi-hud draws a thin ring = "something
                changed"), withdrawn when the event's state ends; ⌥⌘ develops it into the event's own actions ("Reply to Sam", "Open the alert", "Ask
                Orion"), the glide and the long-press work on them (the event's mail is the mail).
Privacy: local_only_domains + the leaks gate are applied when the event is made: a gated event keeps no
subject, is never enriched, never reaches Claude (its long-press is the plain action), and its pack line says
"details withheld". Logs and tokens carry the source and class only, never a name, a subject or a path.
"""
from .config import DEFAULTS, SOURCES, proactive_settings   # noqa: F401
