from conftest import AFK, WIN, ev

from oi import cli


def test_cli_end_to_end(fake_aw, capsys):
    fake_aw.buckets[WIN] = [ev(1, 0, 5, app="AppA"), ev(2, 5, 5, app="Mail", title="Secret Subject 1")]
    fake_aw.buckets[AFK] = [ev(1, 0, 10, status="not-afk")]
    assert cli.main(["pull"]) == 0
    assert cli.main(["tokens", "--day", "2026-01-05"]) == 0
    assert cli.main(["stats", "--day", "2026-01-05"]) == 0
    out = capsys.readouterr().out
    assert "kinds: activate=2, title=1" in out
    assert "Mail: 1" in out
    assert "secret" not in out.lower()  # nothing personal printed by default


def test_stats_missing_day(capsys):
    assert cli.main(["stats", "--day", "2026-01-01"]) == 1
