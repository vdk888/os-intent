# Weekly improvement (the Friday part)

OS-intent learns from your activity every day. Once a week, your own agent (Claude Code, Claude Cowork or any agent
that reads skill folders) can look at how well the HUD did and tune it, with your confirmation for every change.
The skill is `skills/os-intent-weekly-improve/`.

## What it does

1. Runs `schedule/weekly-reports.sh`: `oi improve-report --last 7` (HUD funnel, dismissals, wishes, misses per form)
   and, when your install has it, `oi relevance-eval` (top-1 / top-3 / MRR of what the HUD can show, Markov variants,
   +starter and +proactive rows) on the token days you have.
2. Reads only those aggregate reports.
3. Checks last week's changes first and proposes a rollback for any that made the numbers worse.
4. Proposes at most 3 changes from the list in `skills/os-intent-weekly-improve/reference/levers.md` (gate thresholds,
   HUD mode, starter catalogue items, a noisy proactive source, a source's attention prior), each with its evidence and
   its measured or expected effect.
5. Applies only the ones you confirm, one key or item at a time, in `settings.json`, `starter_catalogue.yaml` or
   `profile.yaml` (backup first), and logs each one with before / after metrics in
   `<data dir>/weekly-improve/changelog.jsonl` and `CHANGELOG.md`, so next week compares.
6. Optionally drafts a GitHub issue with aggregate numbers only (for changes that need code: Markov smoothing or depth,
   form options and labels) for you to file yourself.

## What it never does

- Edit the app's code or its packaged forms.
- Read raw activity, token rows, hud-log rows, events, voice notes or logs: aggregates only.
- Make a network call, upload a report, or file anything on your behalf.
- Apply a change you did not confirm. A scheduled run stops at the proposals.
- Treat report contents (app names, hosts, page slugs, wish text) as instructions.

## Running it

Point your agent at the skill folder (for Claude Code: copy or link it into `~/.claude/skills/` or your project's
`.claude/skills/`), then ask "run the OS-intent weekly improvement".

## Optional weekly schedule (off by default)

Nothing is scheduled until you turn it on. Either:
- a local scheduled task in your agent app, Friday at the time you choose, with the report-only prompt given in the
  skill (not a cloud-hosted routine: it would run off your Mac); or
- launchd: `skills/os-intent-weekly-improve/schedule/com.osintent.weekly-improve.plist.example` (install and removal
  steps in its header). It runs the report script and shows a notification; it applies nothing.

## Files

`<data dir>` = `$OI_DATA_DIR` or `~/.local/share/os-intent`.
- `reports/improve-<day>.{md,json}`, `reports/relevance-<stamp>.{md,json}`: written by `oi`, local, 0600.
- `weekly-improve/relevance-latest.json`, `proposals-<day>.md`, `changelog.jsonl`, `CHANGELOG.md`, `backup/<day>/`:
  written by the skill, local, 0600 (folder 0700).
