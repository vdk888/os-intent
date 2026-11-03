"""The weekly-improve skill (skills/os-intent-weekly-improve): frontmatter, the launchd template, and the report
script, run against a stub `oi` (no real data, no real oi, no notification)."""
from __future__ import annotations

import os
import plistlib
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

SKILL = Path(__file__).resolve().parent.parent / "skills" / "os-intent-weekly-improve"
SCRIPT = SKILL / "schedule" / "weekly-reports.sh"

STUB = """#!/bin/sh
echo "$*" >> "$STUB_LOG"
case "$1 $2" in
  "relevance-eval --help") [ -n "${STUB_NO_REL:-}" ] && exit 2; exit 0 ;;
  relevance-eval*) echo '{"kind": "relevance"}' ;;
esac
exit 0
"""


def _frontmatter(text: str) -> dict:
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n", 2)[1])


def test_skill_frontmatter():
    fm = _frontmatter((SKILL / "SKILL.md").read_text(encoding="utf-8"))
    assert fm["name"] == SKILL.name
    assert 50 < len(fm["description"]) <= 1024
    assert (SKILL / "reference" / "levers.md").is_file()


def test_plist_template_is_report_only_friday():
    raw = (SKILL / "schedule" / "com.osintent.weekly-improve.plist.example").read_text(encoding="utf-8")
    filled = (raw.replace("__REPO__", "/x/os-intent").replace("__HOME__", "/x/home")
              .replace("__HOUR__", "17").replace("__MINUTE__", "0"))
    p = plistlib.loads(filled.encode())
    assert p["Label"] == "com.osintent.weekly-improve"
    assert p["StartCalendarInterval"] == {"Weekday": 5, "Hour": 17, "Minute": 0}
    assert p["RunAtLoad"] is False
    assert p["ProgramArguments"] == ["/bin/sh", "/x/os-intent/skills/os-intent-weekly-improve/schedule/weekly-reports.sh",
                                     "--notify"]


def _run(tmp_path: Path, days_back: list[int], no_rel: bool = False) -> tuple[str, list[str], Path]:
    data = tmp_path / "data"
    (data / "tokens").mkdir(parents=True)
    today = datetime.now(timezone.utc).date()
    for n in days_back:
        (data / "tokens" / f"{(today - timedelta(days=n)).isoformat()}.jsonl").write_text("")
    (data / "tokens" / "notes.txt").write_text("")          # not a day file: ignored
    stub = tmp_path / "oi"
    stub.write_text(STUB)
    stub.chmod(0o755)
    log = tmp_path / "calls.log"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "OI": str(stub),
           "OI_DATA_DIR": str(data), "STUB_LOG": str(log)}
    if no_rel:
        env["STUB_NO_REL"] = "1"
    out = subprocess.run(["/bin/sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30, check=True)
    return out.stdout, log.read_text().splitlines(), data


def test_script_reports_on_existing_days_only(tmp_path):
    out, calls, data = _run(tmp_path, [0, *range(1, 15), 30])   # 0 = today (partial): excluded
    assert calls[0] == "improve-report --last 7"
    assert calls[1] == "relevance-eval --help"
    rel = calls[2].split()
    days = rel[rel.index("--days") + 1:rel.index("--test-last")]
    today = datetime.now(timezone.utc).date()
    expect = sorted((today - timedelta(days=n)).isoformat() for n in [*range(1, 15), 30])
    assert days == expect
    assert rel[rel.index("--test-last") + 1] == "7"
    assert len(calls) == 3                                     # nothing else is run
    res = data / "weekly-improve" / "relevance-latest.json"
    assert res.read_text().strip() == '{"kind": "relevance"}'
    assert stat.S_IMODE(res.stat().st_mode) == 0o600
    assert stat.S_IMODE(res.parent.stat().st_mode) == 0o700
    assert "test days: 7" in out


def test_script_falls_back_to_two_test_days(tmp_path):
    out, calls, _ = _run(tmp_path, list(range(1, 14)))
    assert "--test-last 2" in calls[2]
    assert "test days: 2" in out


def test_script_skips_without_enough_days_or_command(tmp_path):
    out, calls, data = _run(tmp_path / "a", [1, 2])
    assert calls == ["improve-report --last 7", "relevance-eval --help"]
    assert "skipped (only 2 token days)" in out
    assert not (data / "weekly-improve" / "relevance-latest.json").exists()
    out, calls, _ = _run(tmp_path / "b", [1, 2, 3, 4, 5, 6, 7, 8, 9], no_rel=True)
    assert calls == ["improve-report --last 7", "relevance-eval --help"]
    assert "not available" in out
