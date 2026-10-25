"""Every embedded AppleScript / JXA constant compiles (osacompile only: nothing runs, no app is touched).

A script that does not compile fails silently at run time (osascript rc 1 reads as "app not running"): the W24 mail
sensor never produced an event because `rd` / `st` collide with Mail's dictionary terms. Skipped off macOS."""
import shutil
import sys

import pytest

from oi import health

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("osacompile"),
                                reason="osacompile (macOS) needed")

SCRIPTS = health.embedded_scripts()


def test_scripts_found():
    names = {n for n, _, _ in SCRIPTS}
    assert {"oi.proactive.sensors.MAIL_NEW", "oi.proactive.sensors.MAIL_STATE",
            "oi.proactive.sensors.TELEGRAM_UNREAD_JS", "oi.claude_actions.READ_MAIL",
            "oi.calendar_slots.BUSY_JS"} <= names


@pytest.mark.parametrize("name,lang,src", SCRIPTS, ids=[n for n, _, _ in SCRIPTS])
def test_compiles(name, lang, src):
    ok, err = health.compile_script(lang, src)
    assert ok, f"{name}: {err}"


def test_a_colliding_variable_is_caught():
    ok, err = health.compile_script("AppleScript", 'tell application "Mail"\n\tset rd to "0"\nend tell\n')
    assert not ok and "rd" in err
