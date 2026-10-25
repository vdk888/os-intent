"""State serialiser (design §1): form.state_template + moment -> compact text <= budget tokens.

The returned text is the Laya `state.body` (the "state:" in the design examples is the
field label, not part of the text). Template placeholders:
  {weekday} {hhmm} {daypart} {space} {spaces} {app} {host} {page:N} {slug:N}
  {history:N}      newest first, "{ago}: {token} ({dur})", " | "-joined, N events max
  {day_summary:N}  top N apps today, "Mail 2h05m, Safari 40m"
a moment whose context has `live` (["shot"]: a screenshot still on screen) gets a last line
"live: shot (on screen now)"; without it the text is unchanged.
Truncation order when over budget (design §1): drop the day-summary line, then the
oldest recent events one by one (the current token is kept), never the current context
(time/now lines). If it still does not fit, "over_budget" is recorded.
"""
from __future__ import annotations

import re

from .tokcount import count_tokens

BUDGET = 256
_PH = re.compile(r"\{(\w+)(?::(\d+))?\}")
MIN_DUR_SHOWN_S = 30


def fmt_dur(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _words(slug: str) -> str:
    return slug.replace("-", " ").strip()


def _clip(text: str, n: int | None) -> str:
    return text if n is None or len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def history_text(history: list[dict], n: int) -> str:
    parts = []
    for h in history[:n]:
        s = f"{fmt_dur(h['ago_s'])}: {h['token']}"
        if h.get("dur_s") is not None and h["dur_s"] >= MIN_DUR_SHOWN_S:
            s += f" ({fmt_dur(h['dur_s'])})"
        parts.append(s)
    return " | ".join(parts)


def _fields(moment: dict) -> dict:
    c = moment["context"]
    url = c.get("url")
    host, slug = (url.split("/", 1) + [""])[:2] if url else ("-", "")
    if url and c.get("app") == "Safari":
        page = f'{host} "{_words(slug)}"'
    elif c.get("title"):
        page = f'"{_words(c["title"])}"'
    else:
        page = "-"
    return {
        "weekday": c.get("weekday", "?"), "hhmm": c.get("hhmm", "?"),
        "daypart": c.get("daypart", "?"),
        "space": "?" if c.get("space") is None else str(c["space"]),
        "spaces": "?" if c.get("spaces") is None else str(c["spaces"]),
        "app": c.get("app") or "?", "host": host, "slug": _words(slug), "page": page,
    }


def _render_line(line: str, moment: dict, fields: dict, n_hist: int | None) -> str:
    def sub(m):
        name, n = m.group(1), m.group(2)
        n = int(n) if n else None
        if name == "history":
            return history_text(moment.get("history") or [], n_hist if n_hist is not None else (n or 5))
        if name == "day_summary":
            top = (moment.get("day_summary") or [])[: n or 3]
            return ", ".join(f"{a} {fmt_dur(s)}" for a, s in top) or "-"
        if name not in fields:
            raise KeyError(f"unknown placeholder {{{name}}}")
        return _clip(fields[name], n)
    return _PH.sub(sub, line)


def render(form: dict, moment: dict, budget: int = BUDGET, count=count_tokens
           ) -> tuple[str, int, list[str]]:
    lines = [ln for ln in form["state_template"].splitlines() if ln.strip()]
    live = [x for x in moment["context"].get("live") or [] if isinstance(x, str) and x.isidentifier()]
    if live:      # a live context state (the screenshot on screen) conditions Laya too; never truncated
        lines.append("live: " + ", ".join(f"{x} (on screen now)" for x in live))
    fields = _fields(moment)
    hist_line = next((i for i, ln in enumerate(lines) if "{history" in ln), None)
    hist_n = None
    if hist_line is not None:
        m = re.search(r"\{history(?::(\d+))?\}", lines[hist_line])
        hist_n = min(int(m.group(1) or 5), len(moment.get("history") or []))
    drop_summary = False
    truncations: list[str] = []

    def build():
        out = []
        for ln in lines:
            if drop_summary and "{day_summary" in ln:
                continue
            out.append(_render_line(ln, moment, fields, hist_n if "{history" in ln else None))
        return "\n".join(out)

    text = build()
    n = count(text)
    if n > budget and any("{day_summary" in ln for ln in lines):
        drop_summary = True
        truncations.append("day_summary")
        text = build(); n = count(text)
    dropped = 0
    while n > budget and hist_n is not None and hist_n > 1:
        hist_n -= 1
        dropped += 1
        text = build(); n = count(text)
    if dropped:
        truncations.append(f"recent_oldest:{dropped}")
    if n > budget:
        truncations.append("over_budget")
    return text, n, truncations
