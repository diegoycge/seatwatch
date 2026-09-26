# seatwatch

Personal award-flight alert service on top of the seats.aero partner API (Pro key, 1,000 calls/day,
quota resets 00:00 UTC; `x-ratelimit-remaining`/`x-ratelimit-reset` headers are the source of truth).

- How to *use* it (turn requests into watches/windows, CLI reference): see `SKILL.md`
  (also installed at `~/.claude/skills/seatwatch/SKILL.md`).
- Python stdlib only, runs on /Users/Diego/Miniforge3/bin/python3 (keep 3.9-compatible syntax).
- Runtime data (SQLite, logs, installed copy of the code) lives in
  `~/Library/Application Support/seatwatch/` — background agents can't read ~/Documents (TCC) and
  iCloud-synced folders corrupt SQLite. **After editing code, run `bin/seatwatch install`** to copy it
  there and restart the launchd agent (`com.seatwatch.agent`).
- For experiments use `SEATWATCH_DATA=/tmp/somewhere SEATWATCH_PORT=8799 bin/seatwatch serve` — note
  any real API call still spends the shared daily quota.

## Cloud mode (engine = "cloud")
An hourly Claude routine (claude.ai/code/routines, model Opus 5) clones `main`, follows `ROUTINE.md`:
`bin/seatwatch-cloud poll` → Claude judges `.cloud/pending.json` → `bin/seatwatch-cloud finish --verdicts`.
Config/state travel on the `data` branch (single squashed commit, force-with-lease; see `datasync.py`):
`config/*.json` written by the Mac, `state/seatwatch.db` written by the cloud, `secrets/seats_aero_key`
uploaded by the user (`seatwatch cloud add-key`). The Mac's scheduler stops polling and pulls state every
5 min; config edits push immediately. The cloud sandbox needs seats.aero and ntfy.sh allowed in the
environment's network settings. Push code changes to `main` so the routine runs them.

## Layout
- `seatwatch/api.py` – HTTP client, records every call + quota headers in `api_calls`.
- `seatwatch/matching.py` – row compaction, `Criteria` filtering, deal scoring, transfer funding.
- `seatwatch/jobs.py` – job derivation (watch / window / explore), `run_job`, diffing, alerts, cached views.
- `seatwatch/scheduler.py` – pacing loop (credit = spendable / seconds-to-reset).
- `seatwatch/ops.py` – validation + operations shared by `server.py` (web UI/API) and `cli.py`.
- `seatwatch/web/index.html` – single-file dashboard (vanilla JS). Writes need header `X-Seatwatch: 1`.
- `seatwatch/cloud.py` – cloud poll (hour's quota share, queue candidates), fare grouping for Claude, finish (verdicts → alerts → push).
- `seatwatch/datasync.py` – git data-branch sync + config JSON import/export + Mac-side state pull.
- `seatwatch/data/partners.json` – bank → airline transfer ratios (researched Sept 2026; edit as they change).
