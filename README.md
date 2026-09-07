# fantasy-gm

An autonomous ESPN fantasy football GM: it drafts, sets lineups, evaluates
waivers, and researches real-world news about your players — all backed by
a durable evaluation cache instead of one-off LLM calls per decision.

[![Security](https://github.com/tadhatch/fantasy-gm/actions/workflows/security.yml/badge.svg)](https://github.com/tadhatch/fantasy-gm/actions/workflows/security.yml)

## How it thinks

Every decision starts from a deterministic, quantitative baseline (ESPN's
own projections, VOR, usage) and then applies a bounded adjustment from an
off-field "context" signal on top — contract disputes, injuries to key
personnel, legal situations, locker-room drama, or a genuinely unusual
real-world event a normal projection wouldn't catch. The two are kept
separate on purpose: stats answer "how good is this player on the field,"
context answers "is something about to make that stale."

That context signal is produced by a recurring **evaluation worker**, not
by every consumer researching players itself. It runs one broad web-search
scan across your roster and the top free agents, escalating to a real
per-player deep dive only for whatever it flags as significant or unclear
— most players most days have nothing going on, so a scan-then-escalate
strategy spends the OpenAI call budget on genuine findings instead of
confirming silence one player at a time. Results land in Postgres as a
shared, append-only cache that the lineup worker, waiver pipeline, and
chatbot all read from.

Team defenses (D/ST) go through a separate scan with its own questions,
since a team isn't a person: key starting-defender injuries, personnel or
coordinator changes, and truly unusual off-field/environmental
circumstances outside the team's control — not contract holdouts or
personal life, which don't apply to a unit.

**Safety model:** every transaction (lineup change, add/drop, trade) goes
through a shadow-first gate. `FANTASY_GM_TRANSACTIONS_MODE=shadow` (the
default) logs exactly what would be submitted without ever calling ESPN's
write endpoints. Nothing submits for real unless that mode is `live` *and*
the specific call also passes `--confirm` / `confirm=True` — the waiver
pipeline in particular never passes `confirm=True` itself, so it always
shadows regardless of the global mode.

## Architecture

- **Supervisor** (`fantasy-gm run`) — the one long-running process. Polls
  on an interval, keeps the chatbot alive, sweeps up any finished
  disposable workers, and dispatches recurring workers on schedule (the
  lineup worker fires ahead of each wave of kickoffs, read from the real
  NFL schedule via `nflreadpy` — not a fixed interval, since kickoff
  windows now span most weekdays in a season).
- **Disposable workers** — each scheduled job (evaluation, lineup, waiver)
  runs as a short-lived Railway service that does its work and deletes
  itself, rather than living inside the supervisor process.
- **Postgres** — the durable store behind all of it: the evaluation cache,
  waiver decision audit trail, and schedule/task-run bookkeeping the
  supervisor uses to avoid double-dispatching work.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# Fill in ESPN_SWID, ESPN_S2, ESPN_LEAGUE_ID, ESPN_TEAM_ID, OPENAI_API_KEY, DATABASE_URL
```

See `.env.example` for the full set of tunables (evaluation call budgets,
freshness windows, chatbot spam controls, etc.) — each one is documented
inline there.

## Commands

### Draft

```bash
fantasy-gm league                  # league summary + your upcoming picks
fantasy-gm players --limit 50      # ESPN player pool by ownership
fantasy-gm board --top 50          # valuation board (VOR + fandom weighting)
fantasy-gm draft status
fantasy-gm draft watch             # read-only: polls and prints new picks
fantasy-gm draft run               # autonomous draft-day picker
```

### Roster / transactions

```bash
fantasy-gm roster show
fantasy-gm roster set-lineup --move playerId:fromSlot:toSlot [--confirm]
fantasy-gm roster add-drop --add <id> --drop <id> [--waiver --bid N] [--confirm]
fantasy-gm roster propose-trade --to <teamId> --offer <id> --request <id> [--confirm]
```

### Workers (one-shot; the supervisor schedules these automatically)

```bash
fantasy-gm worker evaluate [--pool-size 50] [--max-calls 5] [--dst-max-calls 2]
fantasy-gm worker lineup [--week N] [--confirm]
fantasy-gm worker waiver [--pool-size 60] [--shortlist-size 15] [--no-submit]
```

### Context / chatbot

```bash
fantasy-gm context refresh
fantasy-gm context show --limit 50
fantasy-gm chatbot run
fantasy-gm chatbot watch
```

### Supervisor

```bash
fantasy-gm run    # the always-on process; deploy this as the long-running service
```
