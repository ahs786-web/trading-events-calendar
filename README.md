# Trading events calendar

A personal, single-user tool that assembles every date that matters to an intraday
options trader into one sorted list and publishes it as:

- a **subscribable calendar feed** (`docs/events.ics`), and
- a **single-glance web page** (`docs/index.html`, served by GitHub Pages).

All display times are **Europe/London**. No accounts, no server, no database.

## What's in the calendar

| Category | Source | How |
| --- | --- | --- |
| Monthly & quarterly OPEX (triple witching) | computed | third Friday, moved to preceding Thursday on a US holiday |
| VIX monthly expiry | computed | Wednesday 30 days before the *next* month's third Friday |
| Quarter-end sessions | computed | last XNYS trading day of Q1–Q4 (tagged distinctly from month-ends) |
| Month-end sessions | computed | last XNYS trading day of each month |
| US market holidays (full closures) | computed | days the NYSE is fully closed (Independence Day, Thanksgiving, …) |
| Early-close half-days | computed | thin, often-rangebound sessions closing 13:00 ET (Black Friday, Christmas Eve, July 3rd) |
| Macro, current week (with forecasts) | [ForexFactory feed](https://nfs.faireconomy.media/ff_calendar_thisweek.json) | fetched once/day, USD + high-impact + a medium allowlist |
| Macro, months ahead (FOMC, CPI, NFP, PPI, GDP, PCE) | [`macro_schedule.yaml`](macro_schedule.yaml) | official Fed/BLS/BEA release schedules, hand-refreshed yearly; deduped against the FF feed |
| Treasury auctions (3y / 10y / 30y) | [TreasuryDirect API](https://www.treasurydirect.gov/TA_WS/securities/upcoming?format=json) | free official JSON, no key; 13:00 ET close; best-effort |
| Earnings (Mag 7 + AMD, NFLX, COIN) | [`earnings.yaml`](earnings.yaml) + Nasdaq calendar | YAML is authoritative; Nasdaq refreshes *unconfirmed* dates only |

Deterministic dates are **computed from a real NYSE holiday calendar**
(`pandas_market_calendars`, XNYS) — never fetched, never hardcoded.

## Files

```
build.py                   # the daily job
earnings.yaml              # hand-maintained earnings dates (~quarterly edit)
macro_schedule.yaml        # official FOMC/CPI/NFP/PPI/GDP/PCE dates (~yearly edit)
requirements.txt
docs/index.html            # the page (GitHub Pages serves /docs)
docs/data.json             # written by build.py
docs/events.ics            # written by build.py
.github/workflows/build.yml
```

## How it stays honest (failure modes)

- The macro endpoint is rate-limited and returns an HTML *"Request Denied"* page
  with **HTTP 200** when throttled. `build.py` checks the body starts with `[`/`{`
  before parsing; on denial it **leaves the previous `data.json` untouched**,
  marks `feed_status: denied`, and **exits non-zero** so the scheduled run fails
  and GitHub emails you.
- The page shows a prominent **stale-data banner** if `feed_status` isn't `ok`
  or `generated_at` is over 36 h old. Silent staleness is the main risk.
- Earnings dates marked `confirmed: false` render faintly with an `est.` tag, in
  both the page and the `.ics` (prefixed `≈`).
- **Earnings sourcing:** `earnings.yaml` is the source of truth. Each run also
  does a best-effort scan of Nasdaq's public earnings calendar (~45 days ahead)
  to refresh the *unconfirmed* estimates. A `confirmed: true` YAML date is
  sacrosanct — never fetched, never overwritten. Nasdaq-derived dates stay
  flagged as estimates (you still verify before flipping to `confirmed: true`).
  The scan is fully defensive: if Nasdaq is blocked (common from CI IPs) or
  unreachable, it bails out quietly and the run still succeeds on YAML alone.
- **Scheduled macro needs a yearly refresh:** `macro_schedule.yaml` carries the
  official Fed/BLS/BEA dates (FOMC currently through Dec 2027, BLS/BEA through
  Dec 2026). When under ~45 days of runway remain, the build logs a loud
  warning. Refresh from the URLs in the file's header — BLS also publishes an
  auto-updating ICS (`bls.gov/schedule/news_release/bls.ics`) that makes the
  copy-paste quick.
- **Treasury auctions are best-effort:** a TreasuryDirect outage skips auctions
  for that run without failing the build (they're secondary context, not the
  thing that gets you caught out).

## Local run

```bash
pip install -r requirements.txt
python build.py            # writes docs/data.json and docs/events.ics
```

## One-time setup (see the build summary for the exact URLs)

1. `gh auth login` — needs `repo` and `workflow` scopes.
2. **Settings → Actions → General → Workflow permissions → Read and write**.
3. **Settings → Pages → Source: `main` branch, `/docs` folder**.
4. **Actions** tab → run *build calendar* once (`workflow_dispatch`).
5. Subscribe to the `events.ics` URL in iOS Calendar
   (Calendar → Add Account → Other → Add Subscribed Calendar).
6. Add the page URL to the iPhone home screen.

> iOS refreshes subscribed calendars on its own schedule and can lag by hours, so
> the feed is for **planning the week ahead**, not same-morning first notice.

## Tuning

Everything tunable lives in the `CONFIG` block at the top of `build.py`:
tickers, the macro currency/impact filter, the medium-impact allowlist, earnings
release times, and the 3-year horizon.
