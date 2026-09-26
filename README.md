# Seatwatch

Background award-seat alerts and deal finding on the seats.aero API.

- **Dashboard:** http://127.0.0.1:8765 (Deals, Watches, Travel windows, Points, Search, Activity, Settings)
- **CLI:** `bin/seatwatch -h`. Claude uses this through the `seatwatch` skill, so you can just describe what you want.
- **Alerts:** Mac notifications, plus phone push through ntfy. Subscribe to the topic shown in Settings.

## How the 1,000 daily calls get spent
The quota resets at midnight UTC. On each 30-second tick the scheduler reads the real remaining count from
seats.aero's response headers. It holds back an on-demand reserve for you and Claude, which shrinks toward
zero as the reset approaches, and spreads the rest evenly over the time left. Unused calls raise the pace later
in the day, so the quota gets used up by the reset. Each call runs the most overdue job, weighted by priority:

| Job | What it fetches | Priority | Target / minimum interval |
|---|---|---|---|
| Watch leg | Cached Search for your exact airports and dates | 3 (you can set 1–5) | 45 min / 15 min |
| Window sweep | Cached Search from home airports to the window's destinations | 2 | 90 min / 30 min |
| Region scan | Bulk Availability for each program you can pay with, per region pair | 1 | 6 h / 2 h |

When every job has been checked within its minimum interval, the service waits rather than re-fetching data
that seats.aero itself only refreshes a few times a day. Adding watches or windows gives it more to cover.

## Deals
A deal is an award in one of your travel windows whose one-way price per person is at or below the threshold for
its cabin and distance (editable in Settings). Taxes above the allowance are converted to points and added to the
price. It also has to be affordable from your balances plus transfer partners. Score = threshold ÷ effective
price; 1.0 is "good" and 1.25 or more is "great" by default.

## Cloud mode: Claude judges, and it runs with the Mac off
An hourly Claude routine (Opus 5) in Anthropic's cloud pulls this repo, spends that hour's share of the quota, and
judges each new fare great, good, or pass with a one-line reason. Identical fares on different dates are judged together.
Then it sends the alerts and saves state to the private repo's `data` branch. Watch matches always alert you with
Claude's take attached. Travel-window deals only alert you when Claude rates them good or great. Tell Claude what you
value under Settings → "What Claude should know about you". The Mac dashboard mirrors the cloud every 5 minutes.
Commands: `seatwatch cloud setup|sync|status|add-key|disable`.

## Service
`bin/seatwatch install` copies the code to `~/Library/Application Support/seatwatch/app` and starts a launchd
agent. Re-run it after changing code. `bin/seatwatch uninstall` stops it; your data stays. The service only runs
while the Mac is awake. It catches up after sleep by re-computing the pace from what's left.
