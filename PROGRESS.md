# Fantasy GM — session state / handoff

Written to survive a context compaction. Read this first in the next session.

## Architecture (current)

- **Supervisor** (`fantasy_gm/supervisor.py`) — long-running Railway service. Polls every `FANTASY_GM_SUPERVISOR_POLL_SECONDS` (default 60s). Ensures the chatbot exists, sweeps orphaned finished workers, and dispatches recurring workers on schedule.
- **Postgres** — durable shared store for everything (`DATABASE_URL`, set as a **direct** service variable, NOT a Shared Variable — Railway doesn't resolve `${{Service.VAR}}` when it's the value of a project Shared Variable, only when set directly on a service). Confirmed working value: `${{Postgres.DATABASE_URL}}` (no spaces).
- **Disposable workers** — created via `RailwayServiceManager._launch_disposable_worker()`, tracked via `deployment_stopped` + `status == SUCCESS` (`has_actually_finished()` in `railway/services.py` — `deploymentStopped` alone is true even for a deployment that was abandoned before ever running, so status must also be checked).
- **`fantasy_gm/nfl_schedule.py`** — single source of truth for real NFL schedule data (via `nflreadpy.load_schedules()`, confirmed live: fields are `game_id`, `week`, `gameday` (`YYYY-MM-DD`), `gametime` (`HH:MM`), `home_team`, `away_team`, all as expected). Matches games by **week number**, not a date-window guess (a day-window bug caused "everyone on bye" earlier tonight — fixed). Detects flex schedule changes by diffing against a Postgres snapshot (`schedule_snapshot` table).

## Workers built so far

1. **Evaluation worker** (`fantasy_gm/context/evaluation_worker.py`, `fantasy-gm worker evaluate`) — off-field "feeling" signal (contracts, holdouts, legal, personal life, distractions — explicitly NOT on-field stats). One broad web-search scan across roster + top free agents, escalates to a real per-player deep dive only for what it flags (budget default 5 calls total, was previously an expensive per-player loop — redesigned). Stores into Postgres `player_evaluations` (append-only, `PostgresContextStore`). Scheduled daily via `_evaluation_due()` (`FANTASY_GM_EVALUATION_INTERVAL_HOURS`, default 24h), tracked via `latest_researched_at()`.
2. **Lineup worker** (`fantasy_gm/lineup/`, `fantasy-gm worker lineup`) — **tested and working well as of this session's end.** Reads real per-week ESPN projections (`ESPNClient.get_players_by_id` + `extract_week_projection` in `valuation/projections.py`), blends in the evaluation cache's `weighted_delta()` on top (deliberately allowed to dominate — user's explicit preference, don't add damping), respects per-player locks (`nfl_schedule.is_team_locked`, pinned before the slot-fill loop in `optimizer.py`) and bye weeks (`nfl_schedule.is_team_on_bye`, schedule-based not inferred). CLI output shows full reasoning: a moves table and a full roster board, both with a `Total` column and an `espn + eval` breakdown column (mathematically consistent — back-computed from `projected_points - evaluation_delta`, not the possibly-unused raw figure). Scheduled off real kickoff times (`_lineup_due()` in supervisor.py, checkpoint = kickoff minus `LINEUP_CHECKPOINT_BUFFER_MINUTES`, default 75min), not a fixed interval — games spread across most weekdays some seasons. `submit_lineup_plan()` goes through the transactions client, shadow-gated by both `FANTASY_GM_TRANSACTIONS_MODE` and `FANTASY_GM_LINEUP_CONFIRM` (both default off/false).
3. **Waiver pipeline** (`fantasy_gm/waiver/`, `fantasy-gm worker waiver`) — 4-stage pipeline (roster evaluation → free-agent screening → deep evaluation/decision → GM decision), 4–7 OpenAI calls total. Built and committed; **needs a fresh test** now that the free-agent pool bug (was including globally-owned players not actually available in this league) and the evaluation cache (was roster-only, now covers free agents too) are both fixed. Records every run in `waiver_decisions` Postgres table as an audit trail. `confirm` hardcoded `False` in the pipeline itself — never auto-executes regardless of settings.
4. **Chatbot** — now reads the evaluation cache (`PostgresContextStore.notable_recent()`) into its existing context render, so it can reference real news in replies. NOT yet proactive (deliberately deferred — would need to watch every team's roster for changes, a separate larger feature). Also: `ensure_chatbot()` now auto-recreates the chatbot service if `FANTASY_GM_CHATBOT_VERSION` doesn't match what's recorded in Postgres (`ServiceVersionStore`) — bump that env var whenever the chatbot's provisioning needs to change.

## Known-good verified facts (don't re-litigate)

- `nflreadpy.load_schedules([season])` schema confirmed live (see above).
- ESPN's `set_lineup` transaction payload (`espn/transactions.py`) is **still unverified** against a real browser capture — asked the user for this twice, not yet provided. Currently only tested in shadow mode (safe, logs but never submits).
- Railway free-plan service limits were hit once tonight with just one extra worker running concurrently with chatbot+supervisor — worth watching as more workers start scheduling themselves concurrently (evaluation + lineup + soon waiver).
- `RAILWAY_API_TOKEN` must be an **Account Token** (not Project Token) — Project Tokens can create/configure/deploy/set-variables but are not authorized for `serviceDelete`.

## Immediate next task (where we left off)

**Defense (D/ST) evaluation/projection is broken.** In the last live lineup run, "Seahawks D/ST" showed `Total 0.0`, reason "no projection this week", and had to be force-started via the no-healthy-alternative fallback. Needs investigation:
- Does `ESPNClient.get_players_by_id()` / `extract_week_projection()` even return usable stat rows for D/ST entries (player id is a negative synthetic ID per `espn/constants.py`'s `DEFENSE_IDS` — this may not round-trip through the same `kona_player_info` filter the same way real players do)?
- Does `RosterEntry.pro_team_id` get populated correctly for a D/ST roster entry (needed for `is_team_on_bye`/`is_team_locked` to work for defenses too)?
- Worth checking with a targeted manual ESPN API call for that specific negative ID.

## Other open items (not urgent, in rough priority order)

1. Get a real browser-captured `set_lineup` POST payload from the user to verify/fix `espn/transactions.py` before ever trusting it beyond shadow mode.
2. Re-test the waiver pipeline (`fantasy-gm worker waiver --no-submit`) now that free-agent filtering + evaluation cache coverage are fixed.
3. Push + verify the chatbot changes (evaluation-cache read, version-based auto-recreation) — not yet deployed/tested.
4. Wire waiver worker into supervisor's recurring schedule (lineup and evaluation are already scheduled; waiver is still manual-only — this was the explicit "then move on to other workers" next step).
5. Trade worker — not started at all. Will need: other teams' rosters (`load_all_rosters()` already exists and returns all teams), a similar evaluation-cache-driven decision pipeline.
6. Consider whether Railway's free-plan resource limits need addressing (upgrade, or reduce concurrent worker overlap) as more scheduled workers come online.
7. Optional/deferred: proactive chatbot posting about league events (watch all rosters for changes, cross-reference against evaluation cache) — explicitly deferred earlier, not forgotten.

## Git state

All work this session is committed to `main` locally; user has been pushing manually after most commits (confirm current push state with `git log origin/main..HEAD` at the start of next session — don't assume it's clean).
