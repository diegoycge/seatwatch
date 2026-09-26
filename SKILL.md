---
name: seatwatch
description: Manage the user's seatwatch award-flight alerts (seats.aero). Use when the user describes a flight route/dates/cabin they want to be alerted about, asks to check award availability or deals, update points balances, add travel windows, or asks what seatwatch has found. Runs locally via the `seatwatch` CLI.
---

# seatwatch

`seatwatch` is the user's own background service that polls the seats.aero partner API
(1,000 calls/day) and sends Mac + phone (ntfy) notifications. Dashboard: http://127.0.0.1:8765

CLI: `~/Documents/seatwatch/bin/seatwatch` (add `--json` before the subcommand for machine output).
Always run commands with that absolute path. Nothing here needs the API key directly; never print
`~/.config/seats-aero/key`.

## Turning a request into a watch

"Tell me if anything opens up SFO→Tokyo business Nov 20–30, back Dec 5–12, under 90k, 2 people":

```
~/Documents/seatwatch/bin/seatwatch watch add --name "Tokyo Thanksgiving" \
  --description "SFO-Tokyo business Nov 20-30, back Dec 5-12, <90k, 2 pax" \
  --from SFO --to TYO --depart 2026-11-20:2026-11-30 --return 2026-12-05:2026-12-12 \
  --cabins business --max-miles 90000 --pax 2
```

- Places: IATA airports, metro codes (NYC, WAS, CHI, BAY, LON, PAR, TYO, OSA, SEL, BJS, SHA, MIL, ROM,
  BUE, RIO, SAO, YTO), or regions (North America, South America, Europe, Asia, Oceania, Africa).
  Prefer explicit airports; regions expand to ~20-45 major airports.
- Cabins: economy, premium, business, first (comma separated). `--max-miles` is per person, one way.
- `--direct` for nonstop only; `--sources aeroplan,united` to restrict programs (seats.aero source codes).
- `--priority 1-5` (default 3) and `--interval` minutes (default 45) steer how often it's checked.
- Adding a watch checks it immediately (1 call) and prints current matches. Summarize the best few
  for the user (date, route, cabin, miles, program, how to pay).
- Resolve relative dates ("Thanksgiving", "next March") to exact YYYY-MM-DD before calling. If the
  year or range is ambiguous, ask.

Other watch actions: `watch list`, `watch show ID` (cached, free), `watch run ID` (1 call per leg),
`watch edit ID --max-miles 70000`, `watch pause|resume|rm ID`.

## Travel windows (deal finding)

"I can travel anytime Mar 10–31 next year, somewhere in Europe or Japan, business":

```
~/Documents/seatwatch/bin/seatwatch window add --name "March trip" --dates 2027-03-10:2027-03-31 \
  --to "Europe,TYO" --cabins business
```

Windows search from the home airports setting (or `--from`), in both directions unless `--oneway`,
and also run region-wide Bulk Availability scans on every program the user can pay with
(`--no-explore` to disable). Deals = price at/below the threshold for that cabin and distance, taxes
penalised, and affordable from balances plus transfer partners. `--min-score 1.25` = great deals only.

`deals [--window ID] [--home-only]` lists the best cached deals (free).

## Points

`balance list`, `balance set amex_mr=150000 chase_ur=80k united=42000`. Account ids: amex_mr,
chase_ur, citi_ty, capone, bilt, hsbc_us, wells_fargo, and airline ids = seats.aero sources
(united, american, delta, alaska, jetblue, aeroplan, flyingblue, qatar = Avios pool incl.
BA/Iberia/Aer Lingus, virginatlantic, singapore, emirates, etihad, turkish, lifemiles, aeromexico,
qantas, velocity, lufthansa, eurobonus, finnair, ethiopian, saudia, connectmiles, smiles, azul),
plus cathay, ana, jal, eva, southwest (tracked only).

## Checking right now

- `search --from JFK --to LHR,CDG --dates 2026-12-01:2026-12-10 --cabins business` for a one-off search (1-2 calls).
- `trips AVAILABILITY_ID` for flight numbers, times, taxes (1 call). IDs are printed as `id=...`.
- `status` shows quota left, background pace, and each job's freshness.

## Budget etiquette

Background work paces itself to use the whole daily quota by the reset (midnight UTC) while keeping
an on-demand reserve (setting `ondemand_reserve`, default 100) for these commands. Don't loop
searches; prefer `watch show`/`deals` (cached, free) before spending calls.

## Cloud mode (Claude judges deals, works with the Mac off)

When `settings get` shows `"engine": "cloud"`, an hourly Claude routine (Opus 5) in Anthropic's cloud
does the background polling and judges each new candidate great/good/pass with a reason. Watch matches
always notify; window deals notify only when rated good/great. The Mac mirrors results every 5 minutes.

- Edits made through this CLI are pushed to the private repo's `data` branch automatically; the next
  hourly run picks them up. `cloud sync` forces a push + pull now; `cloud status` shows the last cloud run.
- `deals` shows Claude's picks with reasons (`--all` includes passes and unjudged).
- The user's preferences for judging live in the `judge_notes` setting — when they tell you what they
  value ("I only care about lie-flat", "we're two people", "avoid BA surcharges"), update it:
  `settings set judge_notes='"..."'`.
- On-demand commands (`search`, `watch run`, `trips`) still run locally and spend the same daily quota.

## Service

`install` (re-run after editing code), `uninstall`, `notify-test`, `settings get`,
`settings set home_airports=SFO,OAK ntfy_enabled=true`. Logs:
`~/Library/Application Support/seatwatch/logs/service.log`.
