# Levers the weekly pass may propose

`D` = the data dir (`$OI_DATA_DIR`, else `~/.local/share/os-intent`). Defaults are the app's current defaults; a
missing key means the default applies. Source of truth for every key: `docs/DESIGN.md` ("Settings", "Onboarding",
"Proactive hints") and `oi/settings.py`, `oi/proactive/config.py`, `oi/starter.py`.

Applied = the user can change it in a documented data file. Upstream = it needs a code change: never applied here,
only drafted as an issue (SKILL.md step 7).

## Applied levers

| Lever | File, key | Default | Signal in the reports | Step | Measured by |
|---|---|---|---|---|---|
| Show threshold | `settings.json` `gate.show_min_p` | 0.15 | dismiss rate high, press rate low → raise; `dropped.no_option` high and summon share high → lower | ±0.05 | next week's funnel |
| "Probably nothing" cut | `settings.json` `gate.none_max_p` | 0.6 | `dropped.nothing_likely` dominates while the user summons often → raise; many dismissals → lower | ±0.05 | next week's funnel |
| Options shown | `settings.json` `gate.max_show` | 3 (≤ 4) | rank miss (`outside_top3`) high, i.e. the next action is often in the menu but ranked 4th or lower → 4; dismissals high → 2 | ±1 | next week's funnel |
| Pause between shows | `settings.json` `gate.cooldown_s`, `gate.hint_cooldown_s` | 20, 8 | dismiss rate high with many shows per moment → raise; `dropped.cooldown` large with good press rate → lower | ×1.5 / ÷1.5 | next week's funnel |
| Thin-history guard | `settings.json` `gate.min_history` | 1 | early weeks: low press rate on rarely seen contexts → 2 or 3 | +1 | next week's funnel |
| How shows look | `settings.json` `hud.auto_mode` (`hint` / `full` / `off`) and `hud.hint_ttl_ms` (500-10000) | `hint`, 4000 | expiry rate high (`ttl`) with a good press rate on the shows that were used → longer `hint_ttl_ms`; the user finds shows distracting → `off` (⌥⌘ still summons) | one value | next week's funnel |
| Starter catalogue item | `D/starter_catalogue.yaml`: one item's `prior` (0.05-0.6), `trigger`, or remove the item | per item | relevance `per_source.starter` near 0 while the +starter row lowers top-3 → lower a prior / remove; a repeated wish slug no item covers → add an item (only from the documented primitives `app.open`, `url.open`, `mail.draft`, `agent.ask`; label ≤ 22 chars; https only) | one item | `oi relevance-eval --starters <candidate copy>` vs current |
| Starter blend strength | `settings.json` `starter.half_support`; `starter.enabled` | 20; true | +starter row worse than baseline overall → lower `half_support` (starters fade faster) | ±5 | next week's relevance +starter row |
| Noisy proactive source | `settings.json` `proactive.sources.<source>` → false | all false (opt-in) | +proactive row lowers top-1 / top-3 vs baseline and `per_source.proactive` ≈ 0 | one source | relevance +proactive row (an upper bound: the relevance gate is not replayed) |
| Proactive strictness | `settings.json` `proactive.min_p` (0-1), `proactive.min_interval_min` (0-240) | 0.5, 10 | ring hints dismissed often → raise `min_p` 0.05 or the interval | one step | next week's funnel |
| Source attention prior | `D/profile.yaml` `attention.sources.<mail/agents/files/alerts/metrics/markets/calendar>` (0-1) | per source (0.3-0.6) | one source's hints are mostly dismissed but the user wants to keep it on → lower its prior 0.1 | ±0.1 | next week's funnel |

Never propose: `paused`, `hud_enabled`, `denylist_apps` removals, hotkeys, `vocal_recipients`, anything under
`claude`, `gate.outward`, `proactive.agents` / `cockpit` / `prs` connection keys, poll intervals, `live.laya_*` or
`live.predictor` (Laya promotion has its own bar: `oi laya-report`, `oi laya-promote`), and `attention.noise` /
`important` entries (they name senders; only the user adds one, by name, when they ask).

Adding an app to `denylist_apps` is allowed when the USER asks for it (it only removes data); never propose removing one.

## Upstream levers (code; draft an issue, never apply)

| Lever | Where it lives | Signal | Measured by |
|---|---|---|---|
| Markov smoothing | `oi/markov.py` `ALPHA` (1) | relevance variant `alpha=3` / `10` beats baseline by ≥ 2 pts top-1 or MRR with top-3 not worse, two weeks in a row | `oi relevance-eval --alpha 1,3,10` |
| Markov depth | the context levels in `oi/markov.py` | a shallower `--depth` variant beats `o2hs` | `oi relevance-eval --depth o1h,o2h,o2hs` |
| Form options and labels | `oi/forms/*.yaml` (packaged with the app) | `not_in_menu` concentrated on one token kind (e.g. `key`, `space`) in one form → a missing option or a larger `candidates.k`; a criteria wording that never wins | `oi relevance-eval` `per_kind` and `label_in_menu` |
| Per-kind blind spots | the candidate builder | a `per_kind` row with `in_menu` < 70 % | `per_kind` |

## Reading the funnel

- press rate = pressed / shown; dismiss rate = active dismissals (`esc`, `click_outside`) / shown; expiry rate =
  `ttl` dismissals / shown; `wish` dismissals count as misses; summon share = summoned / (shown + summoned);
  miss rate = not_in_menu / moments; rank miss = outside_top3 / moments.
- Compare the same form week over week; totals hide forms with opposite trends.
- A week with a holiday, a trip or a new job looks worse for reasons the settings cannot fix: ask the user before
  calling a change "worse" on such a week.
