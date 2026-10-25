"""W22b: the agentic onboarding session.

  1. OPENING: the sources and the exact read roots, each with an explicit y/N (installed apps: always; each choice
     is remembered for this run only);
  2. STEP 1 (one `claude -p` agent run): it reads, drafts profile.yaml + starter_catalogue.yaml and asks ≤ 6
     questions (only what it could not infer) in questions.json;
  3. the QUESTIONS in the terminal (non-interactive: none asked);
  4. STEP 2 (a second run, only when something was answered): it revises the two drafts;
  5. VALIDATION by oi/onboard.py (primitive mapping, params, installed apps, https hosts, roster slugs, priors,
     triggers, W22 noise filter, local-only gate) + the local builder's top-up to 15-30;
  6. LAST EDIT: the catalogue, numbered; remove or rename; then install (0600) — `--dry-run`: nothing written.

The sandbox (one place, `agent_command`; reuses claude_actions.build_command, so the auth is the subscription
seat exactly as at runtime and an API key never reaches the child):
  --restricted                    file tools confined to the working dirs; no Bash unless --tools names it;
  --tools Read,Glob,Grep,Write,Edit[,Bash]   no WebFetch / WebSearch / Agent / MCP (--strict-mcp-config, none);
  --permission-mode dontAsk       anything not allowed below is denied (nobody to ask);
  --add-dir <root>                each opted-in read root (memory folders, wiki, custom roots);
  --allowedTools                  Read/Glob/Grep, each scoped to those roots + the context folder; Write/Edit under
                                  <work>/out only; Bash = the EXACT argument-free tool paths (oi-mail, oi-notion)
                                  of the sources the person opted into;
  --disallowedTools               WebFetch, WebSearch, Agent, Task, NotebookEdit; Edit/Write on every read root;
                                  Read/Glob/Grep on the auth config dir and on secret-looking files under
                                  every read root (readers.SECRET_GLOBS: *.env, *secret*, .credentials*,
                                  *token*, *.pem, *.key, .ssh, id_*, keychains, sops files).
cwd = <work> (a fresh 0700 temp dir); the credentials copy (file auth) lives in ANOTHER temp dir, outside every
working dir. Both are removed at the end. Single CLAUDE.md files (~/.claude/CLAUDE.md, ~/claude-workspaces/*/
CLAUDE.md) are copied into <work>/context/claude-md/ (never a secret-looking path) — ~/.claude itself is never a
read root.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from .. import claude_actions as ca, onboard, settings as oi_settings
from . import readers, tools

PROMPTS = Path(__file__).parent / "prompts"
AGENT_INSTRUCTION = "Do the onboarding task below, with the tools you have. Your instructions are in the system prompt."
TOOLS = ("mail", "notion")
DENIED_TOOLS = ("WebFetch", "WebSearch", "Agent", "Task", "NotebookEdit")
READ_TOOLS = ("Read", "Glob", "Grep")                      # each scoped to the read roots, secrets denied
MAX_QUESTIONS = 6
MIN_READS = 25                                              # memory + wiki files read before the profile is written
OUT_FILES = ("profile.yaml", "starter_catalogue.yaml")

PRIVACY = ("This onboarding is run by Claude (your subscription, never an API key): an agent READS the sources you\n"
           "choose below — content included (memory files, notes, mails, Notion pages) — to write your profile and\n"
           "starter catalogue. It can only read the folders listed, run the two read-only tools for Mail and Notion,\n"
           "and write into a temporary folder; no network besides Claude itself, nothing is sent, nothing is kept\n"
           "after the run except the two files you approve. Mails and pages of your local-only domains\n"
           "(Settings → Claude) are removed before the agent sees them.")


def log(msg: str) -> None:
    print(f"oi-onboard: {msg}", file=sys.stderr, flush=True)


# ---- the plan (what the person opted into) ------------------------------------------------------------------

@dataclass
class Plan:
    roots: list[Path] = field(default_factory=list)          # --add-dir read roots
    claude_md: list[Path] = field(default_factory=list)      # single files, copied into the context
    tools: list[str] = field(default_factory=list)           # mail, notion
    sources: dict = field(default_factory=dict)              # onboard source summaries (apps, activity, safari)
    team: bool = False
    chosen: list[str] = field(default_factory=list)          # the keys said yes to, for the profile


def _tilde(p: Path, home: Path) -> str:
    s = str(p)
    return "~" + s[len(str(home)):] if s.startswith(str(home) + "/") else s


def candidates(home: Path, cfg: dict, local_only: list[str]) -> list[dict]:
    """The opening screen rows: {key, title, line, roots/files, always}."""
    mds, mem = readers.claude_md_files(home), readers.memory_dirs(home)
    wiki = readers.wiki_dir(home)
    custom, refused = readers.custom_roots(cfg, home)
    rows = [
        {"key": "apps", "title": "Installed apps", "always": True,
         "line": "the NAMES of the apps in /Applications and ~/Applications."},
        {"key": "memory", "title": "Claude memory on this Mac",
         "line": f"{len(mds)} CLAUDE.md files (~/.claude/CLAUDE.md, <workspaces>/*/CLAUDE.md) and "
                 f"{len(mem)} memory folders (~/.claude/projects/*/memory, {sum(readers.count_files(d) for d in mem)} "
                 "files), content included.", "files": mds, "roots": mem},
    ]
    if wiki or custom:
        roots = ([wiki] if wiki else []) + custom
        rows.append({"key": "wiki", "title": "Wiki and memory roots",
                     "line": "; ".join(f"{_tilde(r, home)} ({readers.count_files(r, '*.md')} notes)" for r in roots)
                             + ", content included (settings onboarding.memory_roots adds more)."
                             + (f" Refused: {'; '.join(refused)}." if refused else ""), "roots": roots})
    excl = f" Mails from / to / naming {', '.join(local_only)} are removed first." if local_only else ""
    rows += [
        {"key": "mail", "title": "Mail",
         "line": f"the last {cfg['mail_limit']} messages of the inbox: sender, subject, date and the first "
                 f"{cfg['mail_body_max']} characters of the text (quoted replies cut).{excl} macOS may ask once for "
                 "Automation access."},
        {"key": "notion", "title": "Notion (desktop app cache, read-only)",
         "line": f"titles and text of the {cfg['notion_pages']} most recently edited pages (last {cfg['notion_days']} "
                 f"days) from {_tilde(readers.notion_db(home), home)}"
                 + ("" if readers.notion_db(home).is_file() else " — not found on this Mac") + "."},
        {"key": "activity", "title": "Recent activity (ActivityWatch, this Mac)",
         "line": f"counts of app switches and site HOSTS of the last {onboard.ACTIVITY_DAYS} days."},
        {"key": "safari", "title": "Safari top sites and bookmarks", "line": "their HOSTS (needs Full Disk Access)."},
    ]
    return rows


def opening(io: onboard.IO, home: Path, cfg: dict, ccfg: dict, *, preset: dict | None = None,
            read: Callable = onboard.read_source, has_roster: bool | None = None) -> Plan:
    preset = preset or {}
    chosen = preset.get("sources")
    deny = oi_settings.denylist_apps()
    io.say("OS-intent onboarding — Claude reads your context, asks a few questions, you check the result.")
    io.say(PRIVACY)
    io.say()
    plan = Plan()
    for row in candidates(home, cfg, ccfg.get("local_only_domains") or []):
        key = row["key"]
        if key == "notion" and not readers.notion_db(home).is_file():
            io.say(f"· {row['title']}: {row['line']}")
            continue
        if chosen is not None and not row.get("always") and key not in chosen:
            continue
        io.say(f"· {row['title']}: {row['line']}")
        for r in row.get("roots") or []:
            io.say(f"    {_tilde(r, home)}")
        if not (row.get("always") or (chosen is not None and key in chosen) or io.yes("  Use it?")):
            continue
        plan.chosen.append(key)
        plan.roots += row.get("roots") or []
        plan.claude_md += row.get("files") or []
        if key in TOOLS:
            plan.tools.append(key)
        elif key in ("apps", "activity", "safari"):
            plan.sources[key] = read(key, deny)
    roster_ok = has_roster if has_roster is not None else bool(onboard._agents_for(True))
    team = preset.get("team")
    if team is None:
        team = roster_ok and io.yes("Do you work with your agent team (the roster: Atlas, Nova, Orion, Lyra, …)?")
    plan.team = bool(team)
    return plan


# ---- the work dir ---------------------------------------------------------------------------------------------

def _private_write(p: Path, text: str, mode: int = 0o600) -> None:
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def prepare(work: Path, tools_dir: Path, plan: Plan, cfg: dict, ccfg: dict, home: Path,
            python: str = sys.executable) -> dict:
    """Context files, the tool wrappers and their config. Returns {tool name: exact command}."""
    ctx, out = work / "context", work / "out"
    for d in (ctx, out, ctx / "claude-md"):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    gated = onboard.gate_sources(plan.sources, ccfg)
    _private_write(ctx / "sources.md", "<!-- Summaries of this Mac's apps and activity (names and counts). -->\n\n"
                   + onboard._sources_text(gated) + "\n")
    for i, f in enumerate([f for f in plan.claude_md if not readers.is_secret(f)], 1):
        name = "home-claude" if f.parent.name == ".claude" else f.parent.name
        try:
            _private_write(ctx / "claude-md" / f"{i:02d}-{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}.md",
                           f"<!-- {_tilde(f, home)} -->\n\n" + f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    conf = {"tools": plan.tools, "work": str(work), "mail_limit": cfg["mail_limit"],
            "mail_body_max": cfg["mail_body_max"], "notion_db": str(readers.notion_db(home)),
            "notion_pages": cfg["notion_pages"], "notion_days": cfg["notion_days"],
            "local_only_domains": ccfg.get("local_only_domains") or []}
    tools_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_write(tools_dir / "config.json", json.dumps(conf))
    pkg_root = Path(__file__).resolve().parents[2]
    cmds = {}
    for t in plan.tools:
        p = tools_dir / f"oi-{t}"
        _private_write(p, "#!/bin/sh\n# W22b: read-only onboarding tool (no arguments).\n"
                          f"PYTHONPATH={_q(str(pkg_root))} exec {_q(python)} -m oi.onboard_agent.tools {t} "
                          f"--config {_q(str(tools_dir / 'config.json'))}\n", 0o700)
        cmds[t] = str(p)
    return cmds


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ---- the sandboxed command -------------------------------------------------------------------------------------

def _rule(tool: str, p: Path, glob: bool = True) -> str:
    return f"{tool}(/{p}{'/**' if glob else ''})"            # "//abs/path": an absolute path in a permission rule


def agent_command(*, model: str, mode: str, auth_tmp: Path, work: Path, roots: list[Path], tool_cmds: dict,
                  system_prompt: str, env: dict, home: Path, binary: str) -> tuple[list[str], dict]:
    """(argv, child env) of one agent run (the prompt goes on stdin)."""
    argv, child = ca.build_command(model, mode, auth_tmp, env, home, binary)
    i = argv.index("--tools")
    del argv[i:i + 2]                                           # claude_actions' `--tools ""`: replaced below
    argv[argv.index(ca.INSTRUCTION)] = AGENT_INSTRUCTION
    argv[argv.index("--output-format") + 1] = "stream-json"
    argv.append("--verbose")                                    # stream-json in -p needs it
    tools = ["Read", "Glob", "Grep", "Write", "Edit"] + (["Bash"] if tool_cmds else [])
    read_roots = [work / "context", work / "out"] + roots
    allow = [_rule(t, r) for r in read_roots for t in READ_TOOLS] \
        + [_rule(t, work / "out") for t in ("Write", "Edit")] + [f"Bash({c})" for c in tool_cmds.values()]
    deny = list(DENIED_TOOLS) + [_rule(t, r) for r in roots + [work / "context"] for t in ("Edit", "Write")] \
        + [_rule(t, auth_tmp) for t in READ_TOOLS] \
        + [f"{t}(/{r}/{g})" for r in read_roots for g in readers.SECRET_GLOBS for t in READ_TOOLS]
    argv += ["--restricted", "--tools", ",".join(tools), "--permission-mode", "dontAsk",
             "--append-system-prompt", system_prompt]
    for r in roots:
        argv += ["--add-dir", str(r)]
    argv += ["--allowedTools", ",".join(allow), "--disallowedTools", ",".join(deny)]
    child["BASH_DEFAULT_TIMEOUT_MS"] = "600000"                 # mails through AppleScript can take minutes
    child["BASH_MAX_TIMEOUT_MS"] = "600000"
    child["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return argv, child


# ---- one run ------------------------------------------------------------------------------------------------

def popen_runner(argv: list[str], env: dict, cwd: Path, stdin: str, timeout: float,
                 on_line: Callable[[str], None]) -> int:
    """Runs the agent, streaming stdout lines to `on_line`. Returns the exit code; raises ClaudeUnavailable."""
    try:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             cwd=cwd, env=env, start_new_session=True, text=True, bufsize=1)
    except OSError:
        raise ca.ClaudeUnavailable("no_binary") from None
    deadline = time.monotonic() + timeout
    try:
        p.stdin.write(stdin)
        p.stdin.close()
        for line in p.stdout:
            on_line(line)
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(argv, timeout)
        return p.wait(timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.wait()
        raise ca.ClaudeUnavailable("timeout") from None
    except KeyboardInterrupt:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        raise


def describe(ev: dict, home: Path, work: Path) -> str | None:
    """A stream-json event -> one progress line (tool and path only; never a content excerpt)."""
    if ev.get("type") != "assistant":
        return None
    out = []
    for c in (ev.get("message") or {}).get("content") or []:
        if not isinstance(c, dict) or c.get("type") != "tool_use":
            continue
        inp = c.get("input") or {}
        arg = inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("command") or ""
        arg = str(arg).replace(str(work) + "/", "").replace(str(home), "~")
        out.append(f"  · {c.get('name')} {arg[:110]}")
    return "\n".join(out) or None


def run_agent(prompt: str, *, model: str, work: Path, roots: list[Path], tool_cmds: dict, system_prompt: str,
              timeout: float, runner: Callable = popen_runner, say: Callable[[str], None] = print,
              env: dict | None = None, home: Path | None = None, binary: str | None = None) -> dict:
    """One agent run. Returns {turns, duration_s, tools: {name: n}}; raises ClaudeUnavailable."""
    env = dict(os.environ if env is None else env)
    home = home or Path.home()
    mode = ca.auth_mode(env, home)
    if mode is None:
        raise ca.ClaudeUnavailable("no_login")
    auth_tmp = Path(tempfile.mkdtemp(prefix="oi-onboard-auth-")).resolve()
    stats: dict = {"tools": {}, "result": None}
    t0 = time.monotonic()
    try:
        auth_tmp.chmod(0o700)
        argv, child = agent_command(model=model, mode=mode, auth_tmp=auth_tmp, work=work, roots=roots,
                                    tool_cmds=tool_cmds, system_prompt=system_prompt, env=env, home=home,
                                    binary=binary or ca.claude_bin())

        def on_line(line: str) -> None:
            try:
                ev = json.loads(line)
            except ValueError:
                return
            if not isinstance(ev, dict):
                return
            if ev.get("type") == "result":
                stats["result"] = ev
            for c in ((ev.get("message") or {}).get("content") or []) if ev.get("type") == "assistant" else []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    stats["tools"][c.get("name")] = stats["tools"].get(c.get("name"), 0) + 1
            d = describe(ev, home, work)
            if d:
                say(d)
        rc = runner(argv, child, work, prompt, timeout, on_line)
    finally:
        shutil.rmtree(auth_tmp, ignore_errors=True)
    res = stats["result"] or {}
    text = res.get("result") if isinstance(res.get("result"), str) else ""
    if rc != 0 or res.get("is_error") or not res:
        raise ca.ClaudeUnavailable("stub" if ca.STUB_RE.search(text or "") else
                                   (f"rc_{rc}" if rc else "error_result"))
    return {"turns": res.get("num_turns"), "duration_s": round(time.monotonic() - t0, 1), "tools": stats["tools"]}


# ---- prompts -----------------------------------------------------------------------------------------------

def render(name: str, values: dict) -> str:
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m.group(1), "")), text)


def sources_text(plan: Plan, work: Path, tool_cmds: dict, home: Path) -> str:
    lines = [f"- Summaries of apps / activity / sites: {work / 'context' / 'sources.md'}"]
    if plan.claude_md:
        lines.append(f"- CLAUDE.md files ({len(plan.claude_md)}), copied into {work / 'context' / 'claude-md'}/")
    for r in plan.roots:
        lines.append(f"- Folder (read-only): {r}")
    if "mail" in tool_cmds:
        lines.append(f"- Mail: run exactly `{tool_cmds['mail']}` (no arguments), then read the pages it lists")
    if "notion" in tool_cmds:
        lines.append(f"- Notion: run exactly `{tool_cmds['notion']}` (no arguments), then read the pages it lists")
    return "\n".join(lines)


def system_prompt(ccfg: dict) -> str:
    lo = ", ".join(ccfg.get("local_only_domains") or []) or "(none)"
    return render("system", {"local_only": lo, "min_items": onboard.MIN_ITEMS, "max_items": onboard.MAX_ITEMS})


def read_questions(out: Path) -> list[str]:
    try:
        d = json.loads((out / "questions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    qs = d.get("questions") if isinstance(d, dict) else d
    return [" ".join(q.split())[:onboard.TEXT_MAX] for q in qs or [] if isinstance(q, str) and q.strip()][:MAX_QUESTIONS]


def read_outputs(out: Path) -> dict:
    """The agent's two files -> the raw answer onboard.build validates: {profile, catalogue}."""
    raw: dict = {}
    for name, key in (("profile.yaml", "profile"), ("starter_catalogue.yaml", "catalogue")):
        try:
            d = yaml.safe_load((out / name).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            d = None
        if key == "catalogue" and isinstance(d, dict):
            d = d.get("items")
        raw[key] = d if isinstance(d, (dict, list)) else ({} if key == "profile" else [])
    return raw


# ---- last edit ----------------------------------------------------------------------------------------------

def last_edit(io: onboard.IO, catalogue: dict) -> dict:
    """The catalogue, numbered; the person removes or renames items. Non-interactive: as is."""
    if not io.interactive:
        return catalogue
    items = list(catalogue["items"])
    while True:
        io.say(f"\nYour starter catalogue ({len(items)} actions):")
        for i, line in enumerate(onboard.summary_lines({"items": items}), 1):
            io.say(f"{i:>3}.{line}")
        r = io.ask("Numbers to remove (e.g. 3 5-8), 'r N new label' to rename, Enter when it is right\n> ")
        if not r:
            break
        m = re.fullmatch(r"r\s+(\d+)\s+(.+)", r)
        if m and 1 <= int(m[1]) <= len(items):
            items[int(m[1]) - 1] = {**items[int(m[1]) - 1], "label": m[2].strip()[:onboard.LABEL_MAX]}
            continue
        drop = onboard._picks(r, len(items))
        if drop is None:
            io.say(f"  (numbers 1-{len(items)}, ranges like 5-8, 'r N label', or Enter)")
            continue
        items = [it for i, it in enumerate(items, 1) if i not in drop]
    return {**catalogue, "items": items}


# ---- the session --------------------------------------------------------------------------------------------

def session(io: onboard.IO, *, model: str | None = None, dry_run: bool = False, out_dir: Path | None = None,
            preset: dict | None = None, home: Path | None = None, runner: Callable = popen_runner,
            read: Callable = onboard.read_source, has_roster: bool | None = None, env: dict | None = None,
            osa: Callable | None = None) -> int:
    home = home or Path.home()
    cfg, ccfg = readers.settings(), ca.claude_settings()
    model = model or cfg["model"]
    if not ccfg.get("enabled"):
        io.say("Claude is off in Settings → Claude: run `oi onboard --offline` for the local builder.")
        return 1
    try:
        plan = opening(io, home, cfg, ccfg, preset=preset, read=read, has_roster=has_roster)
    except KeyboardInterrupt:
        io.say("\ncancelled: nothing read, nothing written")
        return 130
    agents = onboard._agents_for(plan.team)
    roster = onboard._roster_text(agents)
    work = Path(tempfile.mkdtemp(prefix="oi-onboard-")).resolve()
    tools_dir = Path(tempfile.mkdtemp(prefix="oi-onboard-tools-")).resolve()
    t0 = time.monotonic()
    stop_mail, mail_thread = threading.Event(), None
    try:
        work.chmod(0o700)
        cmds = prepare(work, tools_dir, plan, cfg, ccfg, home)
        if "mail" in cmds:      # AppleScript reads ~1 s per message: Mail is read in the background, page by page,
            conf = json.loads((tools_dir / "config.json").read_text(encoding="utf-8"))   # while the agent works
            (tools.mail_dir(conf)).mkdir(mode=0o700, parents=True, exist_ok=True)
            (tools.mail_dir(conf) / tools.STARTED).touch()          # before the agent can call oi-mail
            mail_thread = threading.Thread(target=tools.prefetch_mail, args=(conf,),
                                           kwargs={"osa": osa, "stop": stop_mail.is_set}, daemon=True)
            mail_thread.start()
        src_text = sources_text(plan, work, cmds, home)
        sysp = system_prompt(ccfg)
        kw = dict(model=model, work=work, roots=plan.roots, tool_cmds=cmds, system_prompt=sysp,
                  timeout=cfg["timeout_s"], runner=runner, say=io.say, env=env, home=home)
        io.say(f"\nStep 1/2: Claude ({model}) reads your sources — a few minutes. Ctrl-C stops it, nothing written.")
        runs = [run_agent(render("explore", {"sources": src_text, "context": work / "context", "out": work / "out",
                                             "max_questions": MAX_QUESTIONS, "roster": roster,
                                             "min_reads": MIN_READS}), **kw)]
        qs = read_questions(work / "out")
        answers = {}
        if qs and io.interactive:
            io.say(f"\nClaude has {len(qs)} question(s) (Enter skips one):")
            for i, q in enumerate(qs, 1):
                a = io.ask(f"{q}\n> ")
                if a:
                    answers[q] = a
        elif qs:
            io.say(f"(not interactive: {len(qs)} question(s) left unanswered)")
        if answers:
            io.say("\nStep 2/2: Claude revises the profile and the catalogue with your answers.")
            gated, withheld = onboard.gate_answers({str(i): a for i, a in enumerate(answers.values())}, ccfg)
            text = "\n\n".join(f"Q: {q}\nA: {gated[str(i)]}" for i, q in enumerate(answers) if gated.get(str(i)))
            runs.append(run_agent(render("finish", {"answers": text, "out": work / "out", "sources": src_text,
                                                    "roster": roster}), **kw))
        raw = read_outputs(work / "out")
    except ca.ClaudeUnavailable as e:
        io.say(f"\nClaude unavailable ({e}): nothing written. `oi onboard --offline` builds a catalogue locally.")
        return 1
    except KeyboardInterrupt:
        io.say("\ncancelled: nothing written")
        return 130
    finally:
        stop_mail.set()                     # the Mail reader stops after its current batch
        if mail_thread is not None:
            mail_thread.join(timeout=5)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(tools_dir, ignore_errors=True)
    profile, catalogue, meta = onboard.build({}, plan.sources, team=plan.team, use_claude=True, cfg=ccfg,
                                             raw=raw, by="oi onboard (W22b agent)")
    profile["sources"] = {**profile.get("sources", {}), **{k: {"read": True} for k in plan.chosen
                                                            if k not in profile.get("sources", {})}}
    meta.update({"model": model, "runs": len(runs), "questions": len(qs), "answered": len(answers),
                 "turns": sum(r.get("turns") or 0 for r in runs), "duration_s": round(time.monotonic() - t0, 1),
                 "agent_items": len(raw.get("catalogue") or [])})
    io.say(f"\nStarter catalogue: {meta['items']} actions ({', '.join(f'{k} {n}' for k, n in meta['by_source'].items())});"
           f" the agent proposed {meta['agent_items']}, validation dropped {meta['dropped_items']}, noise "
           f"{meta['noise_dropped']}; {meta['runs']} run(s), {meta['turns']} turns, {meta['duration_s']} s.")
    io.say("Profile: " + ", ".join(f"{k} ({len(v)})" if isinstance(v, (list, dict)) else k
                                   for k, v in profile.items() if k not in ("format", "created_at", "by")))
    catalogue = last_edit(io, catalogue)
    if not io.interactive:
        for line in onboard.summary_lines(catalogue):
            io.say(line)
    if dry_run:
        io.say(f"(dry run: nothing written; {len(catalogue['items'])} actions)")
        return 0
    pp, cp = onboard.write(profile, catalogue, out_dir)
    io.say(f"wrote {pp} and {cp} (0600). `oi live` picks the catalogue up at once; re-run `oi onboard` to rebuild.")
    return 0
