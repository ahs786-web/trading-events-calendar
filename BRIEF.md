# Build brief: personal trading events calendar

Hand this whole file to Claude Code. Single-user tool, no auth, no server, no database.

---

## Goal

A GitHub repo that, once a day, assembles every date that matters to an intraday
options trader into one sorted list, and publishes it as (a) a subscribable
calendar feed and (b) a single-glance web page.

Timezone for all display output: **Europe/London**.

---

## Repo layout

```
/build.py              # the daily job
/earnings.yaml         # hand-maintained, updated ~quarterly
/docs/index.html       # the page (GitHub Pages serves /docs)
/docs/data.json        # written by build.py
/docs/events.ics       # written by build.py
/.github/workflows/build.yml
```

---

## 1. Deterministic dates — compute, never fetch

These are pure calendar rules. Generate from today to +3 years on every run.
Get these exactly right; they are the highest-value part of the tool.

**Monthly OPEX** — third Friday of every calendar month.
If that Friday is a US market holiday, expiry moves to the preceding Thursday.

**Quarterly OPEX / triple witching** — third Friday of March, June, September,
December. Same holiday rule. Tag these distinctly from ordinary monthly OPEX.

**VIX monthly expiry** — the Wednesday that falls 30 days before the third
Friday of the *following* calendar month. If that Wednesday is a US holiday,
move to the preceding business day.

Also emit: month-end and quarter-end sessions.

Use a US market holiday calendar (`pandas_market_calendars` or
`exchange_calendars`, XNYS). Do not hardcode holidays.

---

## 2. Macro events — fetch once daily, defensively

Source: `https://nfs.faireconomy.media/ff_calendar_thisweek.json`
and `https://nfs.faireconomy.media/ff_calendar_nextweek.json` if it resolves.

**Critical constraints:**

- This endpoint is rate limited. Fetch **once per run, one run per day**. Never
  fetch from the browser.
- When the limit is hit it returns an HTML "Request Denied" page with a 200
  status, not an error. Before parsing, check the body starts with `[` or `{`.
  If it starts with `<`, abort the fetch, log loudly, and keep the previous
  `data.json` untouched.
- Set a descriptive User-Agent.

**Filtering — this matters more than it sounds.** An unfiltered feed is ~15 rows
a day and the tool becomes unreadable. Default filter:

- Currency: `USD` only.
- Impact: `High` (red) only, PLUS a medium-impact allowlist matched on title:
  CPI, Core CPI, PPI, PCE, Core PCE, Non-Farm, Unemployment Rate, FOMC,
  Fed Funds, Powell, ISM Manufacturing, ISM Services, Retail Sales,
  Advance GDP, Initial Jobless Claims, Consumer Confidence, Michigan Sentiment.

Put the filter in a config block at the top of `build.py` so it is one edit to
change.

Convert all times to Europe/London. Mark all-day and tentative events clearly.

---

## 3. Earnings

Tickers: AAPL, NVDA, MSFT, AMZN, META, GOOGL, TSLA, HOOD, COST.

Try, in order:
1. A free API with confirmed earnings dates and before/after-market timing.
   Evaluate what is currently live and free — do not assume; check.
2. Fall back to `earnings.yaml`, a hand-maintained file:

```yaml
- ticker: AAPL
  date: 2026-07-30
  session: AMC   # BMO | AMC | unknown
  confirmed: true
```

Be honest in the output about which source a date came from — an unconfirmed
estimated date must render differently from a confirmed one. If an API can't
give confirmed status, prefer the YAML.

---

## 4. Outputs

### `data.json`
Flat array, sorted by datetime:

```json
{
  "datetime_london": "2026-07-30T21:30:00+01:00",
  "all_day": false,
  "category": "earnings|macro|opex|opex_quarterly|vix_expiry|period_end",
  "impact": "high|medium|structural",
  "title": "AAPL earnings (AMC)",
  "detail": "forecast / previous, if applicable",
  "confirmed": true
}
```

Include a `generated_at` sibling key and a `feed_status` key
(`ok` / `stale` / `denied`) so the page can show when data went stale.

### `events.ics`
Same events, RFC 5545. Stable UIDs so subscribers don't get duplicates on
rebuild. Timed events get a 30-minute-prior VALARM; OPEX and VIX expiry are
all-day. Set `X-PUBLISHED-TTL:PT6H` and a calendar name.

### `docs/index.html`
Single file, no build step, no framework, no CDN dependencies. It reads
`data.json` from its own origin — never fetch a third-party feed from the page.

Design constraints:
- Shows **the next 5 trading sessions only** by default, with a toggle to
  extend. Do not dump the full list.
- Today pinned at the top; past events hidden automatically.
- One chronological column. Date, London time, title, coloured category dot.
- Legible on a phone at a glance. Dark background.
- If `feed_status` is not `ok`, or `generated_at` is over 36h old, show a
  prominent stale-data banner. Silent staleness is the main failure mode.

---

## 5. The daily job

`.github/workflows/build.yml`:

- Cron at `0 5 * * *` UTC, plus `workflow_dispatch` for manual runs.
- `permissions: contents: write`.
- Runs `build.py`, commits `docs/data.json` and `docs/events.ics` only if
  changed, with a `[skip ci]`-style message.
- **Must fail loudly** if the macro fetch is denied or the earnings source
  breaks — a silent failure that leaves stale data is the thing that gets
  someone caught out by an FOMC. A failed run emails the repo owner; that is
  the alerting mechanism.

---

## 6. What the human must do

List these back at the end of the build, with the two final URLs:

1. `gh auth login` (needs `repo` and `workflow` scopes).
2. Settings → Actions → General → Workflow permissions → **Read and write**.
3. Settings → Pages → Source: `main` branch, `/docs` folder.
4. Actions tab → run the workflow manually once.
5. Subscribe to the `events.ics` URL in iOS Calendar
   (Calendar → Add Account → Other → Add Subscribed Calendar).
6. Add the page URL to the iPhone home screen.

Note honestly in the final summary: iOS refreshes subscribed calendars on its
own schedule and can lag by hours, so the feed is for planning the week ahead,
not for same-morning first notice.

---

## Non-goals

No user accounts, no multi-user, no database, no notifications service, no
mobile app, no analytics. If a step starts requiring any of these, stop and say
so rather than building it.
