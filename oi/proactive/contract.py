"""The behaviour contract in code: the ONE event
schema every connector emits, and which provider gives each source its level 3. The connector contract test
(tests/connector_contract.py) checks every sensor against it.

  live row  {ts, id, source, class, who, subject, ref, private, state: "live"}   (store.make)
  gone row  {ts, id, source, class, state: "gone"}                               (store.gone, same id)

`ts` = since when the state is known live (an agent's `ref.since` when the source knows an earlier start), `id` =
`<source>:<hash of the connector's key>` (stable for the life of the state), `who` / `subject` = who and what
(`subject` is None for a private event), `ref` = what the level-1 actions act on (message id, chat, path, url...).
"""
from __future__ import annotations

from .. import vocab
from . import store

LIVE_FIELDS = ("ts", "id", "source", "class", "who", "subject", "ref", "private", "state")
GONE_FIELDS = ("ts", "id", "source", "class", "state")
OPTIONAL_FIELDS = ("wiki",)            # the judge's verdict, added by the engine

# Level 3 (summary + context + why it matters) per source: "model" = a summary of local material by Claude (mail
# body read by message id; a Mac agent's pending messages from its own record), "local" = lines built from the
# event's own local data (no model), None = no level-3 provider yet (docs/DESIGN.md « Connector compliance »).
LEVEL3 = {"mail": "model", "agent": "model", "cockpit": "local", "metric": "local", "market": "local", "file": None,
          "calendar": "local", "github": "local"}


def problems(ev) -> list[str]:
    """What makes `ev` break the schema ([] = it complies). Field names only, never a value."""
    if not isinstance(ev, dict):
        return ["not a dict"]
    gone = ev.get("state") == "gone"
    want = GONE_FIELDS if gone else LIVE_FIELDS
    out = [f"missing {k}" for k in want if k not in ev]
    extra = set(ev) - set(want) - (set() if gone else set(OPTIONAL_FIELDS))
    out += [f"unexpected {k}" for k in sorted(extra)]
    if ev.get("state") not in ("live", "gone"):
        out.append("bad state")
    if ev.get("source") not in store.TOKEN_SOURCES:
        out.append("bad source")
    if not (isinstance(ev.get("id"), str) and ev["id"].startswith(f"{ev.get('source')}:")):
        out.append("bad id")
    try:
        vocab.parse_ts(ev.get("ts"))
    except Exception:
        out.append("bad ts")
    if not vocab.validate(f"evt:{ev.get('source')}:{ev.get('class')}"):
        out.append("bad class")
    if gone:
        return out
    if not isinstance(ev.get("ref"), dict):
        out.append("bad ref")
    if not isinstance(ev.get("private"), bool):
        out.append("bad private")
    if ev.get("private") and ev.get("subject") is not None:
        out.append("private event carries a subject")
    for k in ("who", "subject"):
        if ev.get(k) is not None and not isinstance(ev[k], str):
            out.append(f"bad {k}")
    return out
