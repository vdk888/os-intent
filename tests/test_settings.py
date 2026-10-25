"""settings.json (written by oi-menubar): tolerant reader + denylist merge in the normaliser."""
import json

from conftest import WIN, ev

from oi import cli, normalise, paths, settings

SAF = "oi-watcher-safari_host-a"
KEY = "oi-watcher-key_host-a"


def write_settings(obj) -> None:
    paths.ensure_private_root()
    settings.settings_path().write_text(json.dumps(obj) if not isinstance(obj, str) else obj)


def test_defaults_when_missing():
    s = settings.load()
    assert s["paused"] is False and s["hud_enabled"] is True
    assert s["denylist_apps"] == ["Passwords", "Keychain Access", "1Password"]


def test_corrupt_and_wrong_types_fall_back():
    write_settings("{not json")
    assert settings.load() == settings.load(paths.data_dir() / "absent.json")
    write_settings({"paused": "yes", "hud_enabled": False, "denylist_apps": ["  Mail ", 3, ""]})
    s = settings.load()
    assert s["paused"] is False and s["hud_enabled"] is False and s["denylist_apps"] == ["Mail"]


def test_effective_denylist_merges_builtin_and_settings():
    write_settings({"denylist_apps": ["Mail"]})
    d = normalise.effective_denylist()
    assert {"Mail", "Passwords", "Keychain Access", "1Password"} <= d


def test_settings_denylist_yields_activate_only():
    write_settings({"denylist_apps": ["Mail", "Safari"]})
    raw = {
        WIN: [ev(1, 0, 5, app="Mail", title="Secret Subject"), ev(2, 5, 5, app="Notion", title="Plan doc")],
        SAF: [ev(3, 6, 3, url="https://example.test/private-page", title="t")],
        KEY: [ev(4, 2, 0, chord="⌘C", app="Mail"), ev(5, 7, 0, chord="⌘V", app="Notion")],
    }
    out = [r["token"] for r in normalise.tokens_from_raw(raw)]
    assert "activate:Mail" in out and "title:Notion/plan-doc" in out and "key:⌘V" in out
    assert not any(t.startswith("title:Mail") for t in out)
    assert not any(t.startswith("url:") for t in out)   # Safari denylisted
    assert "key:⌘C" not in out                          # chord typed in a denylisted app
    # an explicit denylist argument still wins (tests / callers)
    out2 = [r["token"] for r in normalise.tokens_from_raw(raw, denylist=frozenset())]
    assert "title:Mail/secret-subject" in out2


def test_stats_json(fake_aw, capsys):
    fake_aw.buckets[WIN] = [ev(1, 0, 5, app="AppA"), ev(2, 5, 5, app="Notion", title="Doc")]
    assert cli.main(["pull"]) == 0
    assert cli.main(["tokens", "--day", "2026-01-05"]) == 0
    capsys.readouterr()
    assert cli.main(["stats", "--day", "2026-01-05", "--json"]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["tokens"] == 3 and s["kinds"] == {"activate": 2, "title": 1}
    assert {a["app"] for a in s["top_apps"]} == {"AppA", "Notion"}
    assert "doc" not in json.dumps(s).lower()  # no titles in stats


def test_settings_window_boundary_values_are_read_as_written():
    """W26: the extremes the Settings window can write (SettingsForm ranges) are used as written, not defaulted."""
    from oi import claude_actions, shots
    for v in ({"auto_mode": "off", "hint_ttl_ms": 500, "hint_cooldown_s": 0, "ttl_ms": 1500, "fc": None},
              {"auto_mode": "full", "hint_ttl_ms": 10000, "hint_cooldown_s": 300, "ttl_ms": 60000, "fc": "a-b_9"}):
        write_settings({"paused": True, "denylist_apps": ["Signal"],
                        "hud": {"auto_mode": v["auto_mode"], "hint_ttl_ms": v["hint_ttl_ms"],
                                "fill_order": ["cmd", "fn", "ctrl", "option"]},
                        "gate": {"hint_cooldown_s": v["hint_cooldown_s"], "ttl_ms": v["ttl_ms"]},
                        "claude": {"enabled": False, "enrich": False, "local_only_domains": ["client.example"]},
                        "shots": {"enabled": False, "clipboard": False, "fallback_contact": v["fc"]}})
        s = settings.load()
        assert s["paused"] is True and s["denylist_apps"] == ["Signal"]
        assert s["hud"] == {"auto_mode": v["auto_mode"], "hint_ttl_ms": v["hint_ttl_ms"],
                            "fill_order": ["cmd", "fn", "ctrl", "option"]}
        assert s["gate"]["hint_cooldown_s"] == v["hint_cooldown_s"] and s["gate"]["ttl_ms"] == v["ttl_ms"]
        c = claude_actions.claude_settings()
        assert c["enabled"] is False and c["enrich"] is False and c["local_only_domains"] == ["client.example"]
        sh = shots.shot_settings(json.loads(settings.settings_path().read_text()))
        assert sh["enabled"] is False and sh["clipboard"] is False and sh["fallback_contact"] == v["fc"]
