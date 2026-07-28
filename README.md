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
| Month-end / quarter-end sessions | computed | last XNYS trading day of each month/quarter |
| US market holidays (full closures) | computed | days the NYSE is fully closed (Independence Day, Thanksgiving, …) |
| Early-close half-days | computed | thin, often-rangebound sessions closing 13:00 ET (Black Friday, Christmas Eve, July 3rd) |
| Macro (CPI, NFP, FOMC, PCE, …) | [ForexFactory feed](https://nfs.faireconomy.media/ff_calendar_thisweek.json) | fetched once/day, USD + high-impact + a medium allowlist |
| Earnings (9 mega-caps) | [`earnings.yaml`](earnings.yaml) | hand-maintained; confirmed vs. estimated flagged honestly |

Deterministic dates are **computed from a real NYSE holiday calendar**
(`pandas_market_calendars`, XNYS) — never fetched, never hardcoded.

## Files

```
build.py                   # the daily job
earnings.yaml              # hand-maintained earnings dates (~quarterly edit)
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
