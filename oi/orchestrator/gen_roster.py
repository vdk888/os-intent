"""W25: regenerate oi/orchestrator/roster.yaml from the fleet registry (read-only).

  python -m oi.orchestrator.gen_roster [--out PATH] [--check]

Sources (each optional; a missing one only drops its fields):
  - the canonical roster: $OI_FLEET_DIR/team-roster.md (agents table: first name, role, machine);
  - the agents' dept.yaml (`department.display_name`, `mandate`, `host`): $OI_FLEET_DIR/*/dept.yaml;
  - the business-unit rooms: $OI_FLEET_DIR/rooms.yaml (bu, chair, members);
  ($OI_FLEET_DIR defaults to ~/agents)
  - how to reach them: oi.vocal.recipients() (the W12 path the HUD sends vocals through; kind + host only);
  - the curated half: roster_overlay.yaml (ask_for, topics, bu).
Only the fields above are read; nothing else from those files is copied (no paths to credentials, no tokens).
The roster lists the agents the HUD can reach (the vocal recipients). `--check` exits 1 when roster.yaml is stale.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .. import vocal

HERE = Path(__file__).parent
OUT = HERE / "roster.yaml"
OVERLAY = HERE / "roster_overlay.yaml"
HOME = Path.home()
WS = Path(os.environ.get("OI_FLEET_DIR") or HOME / "agents").expanduser()
TEAM_ROSTER = WS / "team-roster.md"
ROOMS = WS / "rooms.yaml"
DEPT_GLOBS = [(WS, "*/dept.yaml")]
SLUG_ALIASES: dict[str, str] = {}   # e.g. {"rnd": "atlas"}: a code name in your registry -> the recipient slug
MANDATE_MAX = 220
BU_NAMES = {"fund": "Portfolio BU", "ai_methods": "Methods BU", "pro_clients": "Client BU"}


def _slug(name: str) -> str:
    s = re.sub(r"[*`]", "", name or "").strip().lower()
    s = s.replace("é", "e").replace("è", "e")
    return SLUG_ALIASES.get(s, s)


def _one_line(text, n: int) -> str:
    t = " ".join(str(text or "").split())
    if len(t) <= n:
        return t
    cut = t[:n]
    dot = cut.rfind(". ")
    return (cut[:dot + 1] if dot > n // 2 else cut.rsplit(" ", 1)[0] + " …")


def team_roster(path: Path = TEAM_ROSTER) -> dict[str, dict]:
    """slug -> {name, role, machine} from the agents table of the canonical roster page."""
    out: dict[str, dict] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    m = re.search(r"^## Agents.*?$(.*?)(?=^## |\Z)", text, re.S | re.M)
    for line in (m.group(1) if m else "").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or cells[0].startswith(("First", "-")):
            continue
        name = re.sub(r"[*`]", "", cells[0]).strip()
        if not name:
            continue
        out[_slug(name)] = {"name": name, "role": _one_line(cells[2], 120), "machine": _one_line(cells[3], 60)}
    return out


def dept_mandates(globs=DEPT_GLOBS) -> dict[str, dict]:
    """slug -> {mandate, host} from the dept.yaml files found (first found wins per slug; worktrees skipped)."""
    out: dict[str, dict] = {}
    for base, pattern in globs:
        for f in sorted(base.glob(pattern)) if base.is_dir() else []:
            if "worktree" in str(f) or "scratch" in str(f):
                continue
            try:
                d = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("department") or {}
            except (OSError, yaml.YAMLError, AttributeError):
                continue
            if not isinstance(d, dict):
                continue
            slug = _slug(str(d.get("display_name") or d.get("slug") or ""))
            if slug and slug not in out and d.get("mandate"):
                out[slug] = {"mandate": _one_line(d["mandate"], MANDATE_MAX), "host": d.get("host")}
    return out


def rooms(path: Path = ROOMS) -> dict[str, list[str]]:
    """slug -> [bu, ...] (+ "chair" marks) from the meeting-room registry."""
    out: dict[str, list[str]] = {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return out
    for r in data.get("rooms") or []:
        if not isinstance(r, dict) or not r.get("bu"):
            continue
        for m in r.get("members") or []:
            out.setdefault(_slug(str(m)), []).append(str(r["bu"]))
    return out


def reach(spec: dict) -> dict:
    """How the HUD reaches the agent: the W12 kind and where (a host name, never a path or a credential)."""
    if spec.get("kind") == "inject":
        return {"via": "vocal", "kind": "inject", "where": "this Mac"}
    return {"via": "vocal", "kind": "a2a", "where": spec.get("to") or "?"}


def build(*, recipients: dict | None = None, overlay_path: Path = OVERLAY, roster_path: Path = TEAM_ROSTER,
          rooms_path: Path = ROOMS, globs=DEPT_GLOBS) -> dict:
    recips = recipients if recipients is not None else vocal.recipients(vocal._raw_settings())
    try:
        overlay = (yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}).get("agents") or {}
    except (OSError, yaml.YAMLError):
        overlay = {}
    team, depts, bus = team_roster(roster_path), dept_mandates(globs), rooms(rooms_path)
    agents = []
    for slug in sorted(recips):
        spec, ov = recips[slug], overlay.get(slug) or {}
        t, d = team.get(slug, {}), depts.get(slug, {})
        bu = sorted(set(bus.get(slug, []) or ov.get("bu") or []))
        agents.append({
            "slug": slug,
            "name": spec.get("name") or t.get("name") or slug.title(),
            "role": t.get("role") or "",
            "bu": [BU_NAMES.get(b, b) for b in bu],
            "mandate": d.get("mandate") or "",
            "ask_for": _one_line(ov.get("ask_for"), 160),
            "topics": [str(x).lower() for x in ov.get("topics") or []],
            "reach": reach(spec),
        })
    return {"format": "oi-roster/1",
            "sources": {"team_roster": bool(team), "dept_yaml": sorted(depts), "rooms": bool(bus),
                        "overlay": bool(overlay)},
            "agents": agents}


def dump(roster: dict) -> str:
    head = ("# GENERATED by `python -m oi.orchestrator.gen_roster` (W25) from the fleet registry + "
            "roster_overlay.yaml.\n# Edit roster_overlay.yaml (ask_for / topics), then regenerate. "
            f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}.\n")
    return head + yaml.safe_dump(roster, allow_unicode=True, sort_keys=False, width=120, default_flow_style=None)


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m oi.orchestrator.gen_roster", description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--check", action="store_true", help="exit 1 when the file differs from a fresh build")
    a = ap.parse_args(argv)
    roster = build()
    if a.check:
        try:
            cur = yaml.safe_load(Path(a.out).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            cur = None
        stale = cur != roster
        print("stale" if stale else "fresh")
        return 1 if stale else 0
    Path(a.out).write_text(dump(roster), encoding="utf-8")
    print(f"{a.out}: {len(roster['agents'])} agents, dept.yaml for {roster['sources']['dept_yaml']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
