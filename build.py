#!/usr/bin/env python3
"""
build.py — the daily job for the personal trading events calendar.

Assembles every date that matters to an intraday options trader into one sorted
list and writes:
  docs/data.json   — flat, datetime-sorted array + metadata
  docs/events.ics  — RFC 5545 subscribable feed with stable UIDs

Design rules (see BRIEF.md):
  * Deterministic dates (OPEX, VIX expiry, period ends) are COMPUTED from a real
    US market-holiday calendar (XNYS), never fetched.
  * Macro events are fetched ONCE per run, defensively. A rate-limited "Request
    Denied" HTML page (served with HTTP 200) must never overwrite good data.
  * Earnings prefer confirmed dates; unconfirmed/estimated dates are flagged.
  * All display output is in Europe/London.
"""

from __future__ import annotations

import hashlib
import json
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — everything a human might want to tune lives in this block.
# ─────────────────────────────────────────────────────────────────────────────

TZ_LONDON = ZoneInfo("Europe/London")
TZ_NY = ZoneInfo("America/New_York")  # US market local time (handles DST)

HORIZON_YEARS = 3  # generate deterministic dates from today to +3 years

# --- Earnings ---------------------------------------------------------------
TICKERS = ["AAPL", "NVDA", "AMD", "NFLX", "AMZN", "META", "TSLA", "MSFT", "GOOGL", "COIN"]
# Release times expressed in US Eastern (America/New_York), converted to London.
EARNINGS_TIME_ET = {
    "BMO": (7, 0),    # before market open  → 07:00 ET
    "AMC": (16, 30),  # after market close  → 16:30 ET (US close is 16:00 ET)
    # "unknown" → rendered as an all-day event
}
ENABLE_EARNINGS_API = True  # best-effort API fill; failure is non-fatal (YAML is truth)
# Nasdaq's public earnings calendar is queried per-date. We scan forward from
# today to find each ticker's next report. Confirmed YAML dates are never
# overridden and are skipped entirely (no request made for them).
EARNINGS_API_SCAN_DAYS = 45          # how far ahead to look; firms publish ~3-6wk out
EARNINGS_API_BLOCK_STREAK = 3        # consecutive bad responses ⇒ assume blocked, stop
EARNINGS_API_MAX_SECONDS = 60        # hard wall-clock budget for the whole scan
NASDAQ_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)
NASDAQ_TIME_MAP = {"time-pre-market": "BMO", "time-after-hours": "AMC"}

# --- Macro (ForexFactory / faireconomy) -------------------------------------
MACRO_FEEDS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",  # optional; skipped if it fails
]
MACRO_USER_AGENT = (
    "trading-events-calendar/1.0 (personal single-user tool; "
    "contact via GitHub repo owner)"
)
MACRO_CURRENCIES = {"USD"}          # feed's `country` field is the currency code
MACRO_HIGH_IMPACT = {"High"}        # always keep these
# Medium-impact titles worth keeping, matched as case-insensitive substrings.
MACRO_MEDIUM_ALLOWLIST = [
    "CPI", "Core CPI", "PPI", "PCE", "Core PCE", "Non-Farm", "Unemployment Rate",
    "FOMC", "Fed Funds", "Powell", "ISM Manufacturing", "ISM Services",
    "Retail Sales", "Advance GDP", "Initial Jobless Claims", "Consumer Confidence",
    "Michigan Sentiment",
]

# --- Treasury auctions (TreasuryDirect) --------------------------------------
ENABLE_TREASURY = True
TREASURY_URL = "https://www.treasurydirect.gov/TA_WS/securities/upcoming?format=json"
# Coupon auctions worth watching intraday; bills are noise. One edit to widen.
TREASURY_TERMS = {"3-Year", "10-Year", "30-Year"}
TREASURY_TYPES = {"Note", "Bond"}   # excludes Bill / TIPS / FRN / CMB
TREASURY_TIME_ET = (13, 0)          # standard 13:00 ET close for notes/bonds

# --- Output paths -----------------------------------------------------------
ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
DATA_JSON = DOCS / "data.json"
EVENTS_ICS = DOCS / "events.ics"
EARNINGS_YAML = ROOT / "earnings.yaml"
MACRO_SCHEDULE_YAML = ROOT / "macro_schedule.yaml"
# Warn (not fail) when the hand-maintained schedule is running out of runway.
MACRO_SCHEDULE_MIN_RUNWAY_DAYS = 45

CAL_NAME = "Trading Events"

# ─────────────────────────────────────────────────────────────────────────────
# Event model
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Event:
    when: datetime          # timezone-aware; London. For all-day, time is 00:00 London.
    all_day: bool
    category: str           # earnings|macro|opex|opex_quarterly|vix_expiry|period_end|quarter_end|holiday|early_close|auction
    impact: str             # high|medium|structural
    title: str
    detail: str = ""
    confirmed: bool = True
    uid: str = ""           # stable ICS UID

    def sort_key(self):
        # All-day events sort to the start of their day (00:00) so they lead.
        return (self.when.date(), 0 if self.all_day else 1, self.when, self.title)

    def to_json(self) -> dict:
        return {
            "datetime_london": self.when.isoformat(),
            "all_day": self.all_day,
            "category": self.category,
            "impact": self.impact,
            "title": self.title,
            "detail": self.detail,
            "confirmed": self.confirmed,
        }


def london_all_day(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 0, 0, tzinfo=TZ_LONDON)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Deterministic dates — computed from the XNYS holiday calendar
# ─────────────────────────────────────────────────────────────────────────────


def get_calendar():
    """The XNYS (NYSE) market calendar object."""
    import pandas_market_calendars as mcal

    return mcal.get_calendar("XNYS")


def get_trading_days(xnys, start: date, end: date) -> set[date]:
    """Set of valid XNYS (NYSE) trading days in [start, end]. A weekday NOT in
    this set is a full-day US market closure (early-close half days ARE trading
    days and are not included here)."""
    sched = xnys.valid_days(start_date=start.isoformat(), end_date=end.isoformat())
    return {ts.date() for ts in sched}


# Nicer display names for a few of the calendar's holiday labels.
HOLIDAY_RENAME = {
    "July 4th": "Independence Day",
    "President's Day": "Presidents' Day",
    "Dr. Martin Luther King Jr. Day": "Martin Luther King Jr. Day",
}


def compute_holidays(xnys, today: date, end: date, trading_days: set[date]) -> list[Event]:
    """Full-day US stock-market closures (Independence Day, Thanksgiving, etc.).

    Uses the XNYS calendar's named regular-holiday rules for labels, and also
    sweeps for any weekday that is closed but unnamed (one-off adhoc closures,
    e.g. a national day of mourning) so nothing slips through unlabelled.
    Early-close half-days are NOT closures and are intentionally excluded.
    """
    events: list[Event] = []
    named_dates: set[date] = set()

    named = xnys.regular_holidays.holidays(
        start=today.isoformat(), end=end.isoformat(), return_name=True
    )
    for ts, name in named.items():
        d = ts.date()
        # Skip past dates, weekends (market is already shut; an unobserved
        # weekend holiday like a Saturday New Year's Day is not a closure), and
        # anything that is somehow still a trading day.
        if d < today or d.weekday() >= 5 or d in trading_days:
            continue
        named_dates.add(d)
        nm = HOLIDAY_RENAME.get(name, name)
        events.append(Event(
            when=london_all_day(d), all_day=True, category="holiday",
            impact="structural", title=f"US market closed — {nm}",
            detail="NYSE full-day closure",
            uid=f"holiday-{d.isoformat()}@trading-events",
        ))

    # Adhoc / unscheduled closures: a weekday that isn't a trading day and wasn't
    # named above.
    d = today
    while d <= end:
        if d.weekday() < 5 and d not in trading_days and d not in named_dates:
            events.append(Event(
                when=london_all_day(d), all_day=True, category="holiday",
                impact="structural", title="US market closed — holiday",
                detail="NYSE closure (unscheduled)",
                uid=f"holiday-{d.isoformat()}@trading-events",
            ))
        d += timedelta(days=1)

    return events


def compute_early_closes(xnys, today: date, end: date) -> list[Event]:
    """Half-day sessions where the NYSE closes early (typically 13:00 ET) — e.g.
    Black Friday, Christmas Eve, the day before Independence Day. These trade on
    thin volume and are often rangebound, so they're worth flagging."""
    events: list[Event] = []

    names: dict[date, str] = {}
    for _time, cal in xnys.special_closes:
        s = cal.holidays(start=today.isoformat(), end=end.isoformat(), return_name=True)
        for ts, nm in s.items():
            names[ts.date()] = nm

    sched = xnys.schedule(start_date=today.isoformat(), end_date=end.isoformat())
    early = xnys.early_closes(sched)
    for ts, row in early.iterrows():
        d = ts.date()
        if d < today:
            continue
        close = row["market_close"]
        et = close.tz_convert(TZ_NY).strftime("%H:%M")
        lon = close.tz_convert(TZ_LONDON).strftime("%H:%M")
        label = names.get(d, "half day")
        if "Independence Day" in label:  # pmc's raw rule name is a mouthful
            label = "day before Independence Day"
        events.append(Event(
            when=london_all_day(d), all_day=True, category="early_close",
            impact="structural", title=f"US market half day — {label}",
            detail=f"Early close {et} ET / {lon} London — thin, often rangebound",
            uid=f"early_close-{d.isoformat()}@trading-events",
        ))

    return events


def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    # weekday(): Mon=0 .. Sun=6; Friday=4
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + timedelta(days=14)


def prev_trading_day(d: date, trading_days: set[date]) -> date:
    cur = d - timedelta(days=1)
    for _ in range(15):  # generous bound; holidays never cluster this long
        if cur in trading_days:
            return cur
        cur -= timedelta(days=1)
    return d  # give up gracefully; should never happen


def month_iter(start: date, months: int):
    """Yield (year, month) starting at start's month for `months` steps."""
    y, m = start.year, start.month
    for _ in range(months):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def compute_structural(today: date, trading_days: set[date]) -> list[Event]:
    events: list[Event] = []
    horizon_end = date(today.year + HORIZON_YEARS, today.month, 1)
    n_months = (horizon_end.year - today.year) * 12 + (horizon_end.month - today.month) + 1

    quarter_months = {3, 6, 9, 12}

    for y, m in month_iter(today, n_months + 2):
        # --- Monthly / quarterly OPEX (third Friday, holiday → preceding Thursday)
        tf = third_friday(y, m)
        opex_day = tf
        moved = False
        if tf not in trading_days:
            opex_day = tf - timedelta(days=1)  # preceding Thursday
            moved = True
        if opex_day >= today:
            if m in quarter_months:
                cat, label = "opex_quarterly", "Quarterly OPEX / triple witching"
            else:
                cat, label = "opex", "Monthly OPEX"
            detail = "third Friday" + (" (moved to Thu; Fri was a holiday)" if moved else "")
            events.append(Event(
                when=london_all_day(opex_day), all_day=True, category=cat,
                impact="structural", title=label, detail=detail,
                uid=f"{cat}-{opex_day.isoformat()}@trading-events",
            ))

        # --- VIX monthly expiry: Wednesday 30 days before third Friday of the
        #     FOLLOWING month; if a US holiday, step back to previous business day.
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        vix_day = third_friday(ny, nm) - timedelta(days=30)
        if vix_day not in trading_days:
            vix_day = prev_trading_day(vix_day, trading_days)
        if vix_day >= today:
            events.append(Event(
                when=london_all_day(vix_day), all_day=True, category="vix_expiry",
                impact="structural", title="VIX monthly expiry",
                detail="30 days before next month's third Friday",
                uid=f"vix_expiry-{vix_day.isoformat()}@trading-events",
            ))

    # --- Month-end / quarter-end sessions (last trading day of each month) -----
    horizon_last = today + timedelta(days=365 * HORIZON_YEARS)
    by_month: dict[tuple[int, int], date] = {}
    for d in trading_days:
        if today <= d <= horizon_last:
            key = (d.year, d.month)
            if key not in by_month or d > by_month[key]:
                by_month[key] = d
    for (y, m), d in by_month.items():
        if m in quarter_months:
            q = (m - 1) // 3 + 1
            category = "quarter_end"
            title, detail = f"Quarter-end (Q{q} {y})", "last trading session of the quarter"
        else:
            category = "period_end"
            title = f"Month-end ({date(y, m, 1):%B} {y})"
            detail = "last trading session of the month"
        events.append(Event(
            when=london_all_day(d), all_day=True, category=category,
            impact="structural", title=title, detail=detail,
            uid=f"{category}-{d.isoformat()}@trading-events",
        ))

    return events


# ─────────────────────────────────────────────────────────────────────────────
# 2. Macro events — fetched once per run, defensively
# ─────────────────────────────────────────────────────────────────────────────


class MacroDenied(Exception):
    """Raised when the feed returns the rate-limit HTML page (HTTP 200 + '<')."""


def _fetch_url(url: str) -> str:
    import requests

    resp = requests.get(url, headers={"User-Agent": MACRO_USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


def macro_title_allowed(title: str, impact: str) -> bool:
    if impact in MACRO_HIGH_IMPACT:
        return True
    if impact == "Medium":
        low = title.lower()
        return any(term.lower() in low for term in MACRO_MEDIUM_ALLOWLIST)
    return False


def fetch_macro() -> list[Event]:
    """Fetch and filter macro events. Raises MacroDenied if the primary feed is
    rate-limited (so the caller can keep the previous data.json untouched)."""
    raw_items: list[dict] = []
    for i, url in enumerate(MACRO_FEEDS):
        is_primary = i == 0
        try:
            body = _fetch_url(url).lstrip()
        except Exception as e:  # network/HTTP error
            if is_primary:
                raise MacroDenied(f"primary macro feed failed to fetch: {e}") from e
            print(f"[macro] optional feed {url} failed ({e}); skipping.", file=sys.stderr)
            continue

        # Rate-limit sentinel: an HTML "Request Denied" page served with 200.
        if not body.startswith(("[", "{")):
            preview = body[:80].replace("\n", " ")
            if is_primary:
                raise MacroDenied(
                    f"primary macro feed returned non-JSON (rate limited?): {preview!r}"
                )
            print(f"[macro] optional feed {url} non-JSON; skipping: {preview!r}",
                  file=sys.stderr)
            continue

        try:
            items = json.loads(body)
        except json.JSONDecodeError as e:
            if is_primary:
                raise MacroDenied(f"primary macro feed JSON decode failed: {e}") from e
            print(f"[macro] optional feed {url} bad JSON; skipping ({e}).", file=sys.stderr)
            continue

        raw_items.extend(items)
        print(f"[macro] {url}: {len(items)} raw rows.", file=sys.stderr)

    events: list[Event] = []
    seen: set[str] = set()
    for it in raw_items:
        currency = it.get("country", "")
        impact = it.get("impact", "")
        title = it.get("title", "").strip()
        if currency not in MACRO_CURRENCIES:
            continue
        if not macro_title_allowed(title, impact):
            continue

        raw_date = it.get("date", "")
        try:
            dt = datetime.fromisoformat(raw_date)  # carries the feed's UTC offset
        except ValueError:
            print(f"[macro] unparseable date {raw_date!r} for {title!r}; skipping.",
                  file=sys.stderr)
            continue
        when_london = dt.astimezone(TZ_LONDON)

        # The feed has no explicit all-day flag; a 00:00 local time is our best signal.
        all_day = dt.hour == 0 and dt.minute == 0
        if all_day:
            when_london = london_all_day(when_london.date())

        forecast = str(it.get("forecast", "")).strip()
        previous = str(it.get("previous", "")).strip()
        bits = []
        if forecast:
            bits.append(f"forecast {forecast}")
        if previous:
            bits.append(f"previous {previous}")
        detail = " / ".join(bits)

        mapped_impact = "high" if impact in MACRO_HIGH_IMPACT else "medium"
        # Deterministic UID so subscribers don't get duplicates on rebuild.
        # (Python's built-in hash() is per-process randomized — must not use it.)
        digest = hashlib.md5(f"{title}|{raw_date}".encode("utf-8")).hexdigest()[:10]
        uid = f"macro-{when_london.date().isoformat()}-{digest}@trading-events"
        dedup = (title, when_london.isoformat())
        if dedup in seen:
            continue
        seen.add(dedup)

        events.append(Event(
            when=when_london, all_day=all_day, category="macro", impact=mapped_impact,
            title=title, detail=detail, confirmed=True, uid=uid,
        ))

    print(f"[macro] kept {len(events)} filtered events.", file=sys.stderr)
    return events


def load_macro_schedule(today: date, ff_events: list[Event]) -> list[Event]:
    """Forward macro visibility from macro_schedule.yaml (official Fed/BLS/BEA
    release schedules, hand-refreshed yearly).

    The ForexFactory feed only covers the current week; this file carries the
    known-in-advance releases (FOMC, CPI, NFP, PPI, GDP, PCE) months out so the
    ICS subscriber can actually plan ahead. When FF already has a matching event
    (same date, title contains a `dedup` keyword), the schedule row is dropped —
    the FF row wins because it carries forecast/previous numbers.
    """
    import yaml

    if not MACRO_SCHEDULE_YAML.exists():
        print("[schedule] macro_schedule.yaml missing; skipping", file=sys.stderr)
        return []
    with MACRO_SCHEDULE_YAML.open() as f:
        rows = yaml.safe_load(f) or []
    if not isinstance(rows, list):
        raise ValueError("macro_schedule.yaml must be a list of records")

    # FF titles by date, for dedup.
    ff_by_date: dict[date, list[str]] = {}
    for e in ff_events:
        ff_by_date.setdefault(e.when.date(), []).append(e.title.lower())

    events: list[Event] = []
    latest = today
    for row in rows:
        try:
            d = row["date"]
            if isinstance(d, str):
                d = date.fromisoformat(d)
            hh, mm = (int(x) for x in str(row["time_et"]).split(":"))
            title = str(row["title"])
            detail = str(row.get("detail", ""))
            keywords = [str(k).lower() for k in row.get("dedup", [])]
        except (KeyError, ValueError, TypeError) as e:
            print(f"[schedule] skipping malformed row {row!r}: {e}", file=sys.stderr)
            continue
        latest = max(latest, d)
        if d < today:
            continue
        when = datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ_NY).astimezone(TZ_LONDON)
        if any(k in t for t in ff_by_date.get(when.date(), []) for k in keywords):
            continue  # FF already carries this release with forecast numbers
        digest = hashlib.md5(f"{title}|{d.isoformat()}".encode("utf-8")).hexdigest()[:10]
        events.append(Event(
            when=when, all_day=False, category="macro", impact="high",
            title=title, detail=detail, confirmed=True,
            uid=f"sched-{d.isoformat()}-{digest}@trading-events",
        ))

    runway = (latest - today).days
    if runway < MACRO_SCHEDULE_MIN_RUNWAY_DAYS:
        print(f"[schedule] WARNING: macro_schedule.yaml has only {runway} days of "
              "coverage left — refresh it from the official Fed/BLS/BEA schedules",
              file=sys.stderr)
    print(f"[schedule] {len(events)} scheduled macro events (after FF dedup).",
          file=sys.stderr)
    return events


def fetch_treasury_auctions(today: date) -> list[Event]:
    """Upcoming 3y/10y/30y coupon auctions from TreasuryDirect's free JSON API
    (no key). Auctions close 13:00 ET; results hit the tape moments later.
    Best-effort: any failure logs and returns [] without failing the run."""
    if not ENABLE_TREASURY:
        return []
    try:
        import requests

        r = requests.get(TREASURY_URL, timeout=20,
                         headers={"User-Agent": MACRO_USER_AGENT})
        r.raise_for_status()
        body = r.text.lstrip()
        if not body.startswith("["):
            raise ValueError("response is not a JSON array")
        rows = r.json()
    except Exception as e:
        print(f"[treasury] fetch failed ({e}); skipping auctions this run",
              file=sys.stderr)
        return []

    events: list[Event] = []
    seen: set[str] = set()
    hh, mm = TREASURY_TIME_ET
    for row in rows:
        try:
            if row.get("securityType") not in TREASURY_TYPES:
                continue
            term = row.get("securityTerm", "")
            if term not in TREASURY_TERMS:
                continue
            d = date.fromisoformat(str(row.get("auctionDate", ""))[:10])
        except (ValueError, TypeError):
            continue
        if d < today:
            continue
        key = f"{term}-{d.isoformat()}"
        if key in seen:  # reopenings can list twice
            continue
        seen.add(key)
        when = datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ_NY).astimezone(TZ_LONDON)
        kind = row.get("securityType", "Note")
        events.append(Event(
            when=when, all_day=False, category="auction", impact="medium",
            title=f"{term} Treasury {kind.lower()} auction",
            detail="bidding closes 13:00 ET; results moments later",
            confirmed=True,
            uid=f"auction-{key}@trading-events",
        ))

    print(f"[treasury] {len(events)} upcoming coupon auctions.", file=sys.stderr)
    return events


# ─────────────────────────────────────────────────────────────────────────────
# 3. Earnings — confirmed dates preferred; estimates flagged
# ─────────────────────────────────────────────────────────────────────────────


def _earnings_event(ticker: str, d: date, session: str, confirmed: bool,
                    source: str) -> Event:
    session = (session or "unknown").upper()
    if session in EARNINGS_TIME_ET:
        hh, mm = EARNINGS_TIME_ET[session]
        when = datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ_NY).astimezone(TZ_LONDON)
        all_day = False
        label = f"{ticker} earnings ({session})"
    else:
        when = london_all_day(d)
        all_day = True
        label = f"{ticker} earnings (time TBD)"

    status = "confirmed" if confirmed else "estimated — verify against IR"
    detail = f"{status} · source: {source}"
    return Event(
        when=when, all_day=all_day, category="earnings", impact="high",
        title=label, detail=detail, confirmed=confirmed,
        uid=f"earnings-{ticker}-{d.isoformat()}@trading-events",
    )


def load_earnings_yaml() -> list[dict]:
    import yaml

    if not EARNINGS_YAML.exists():
        return []
    with EARNINGS_YAML.open() as f:
        data = yaml.safe_load(f) or []
    if not isinstance(data, list):
        raise ValueError("earnings.yaml must be a list of records")
    return data


def fetch_earnings_api(today: date, wanted: set[str]) -> dict[str, dict]:
    """Best-effort lookup of the next earnings date for each ticker in `wanted`,
    using Nasdaq's public per-date earnings calendar.

    Scans forward from today and records the earliest upcoming report for each
    wanted ticker. Returns {TICKER: {"date": date, "session": "BMO|AMC|unknown"}}.

    Defensive by design: never raises. If `wanted` is empty, or the endpoint is
    blocked (common from cloud/CI IPs — it answers with an HTML error page), it
    returns whatever it found so far (possibly {}) and YAML stays authoritative.
    """
    if not ENABLE_EARNINGS_API or not wanted:
        return {}

    import time

    try:
        import requests
    except Exception as e:  # requests missing ⇒ silently fall back to YAML
        print(f"[earnings] API unavailable ({e}); using YAML only", file=sys.stderr)
        return {}

    out: dict[str, dict] = {}
    remaining = set(wanted)
    bad_streak = 0
    deadline = time.monotonic() + EARNINGS_API_MAX_SECONDS

    session = requests.Session()  # keep-alive: one TLS handshake, not one per day
    session.headers.update({"User-Agent": NASDAQ_UA, "Accept": "application/json"})

    for i in range(EARNINGS_API_SCAN_DAYS):
        if not remaining or time.monotonic() > deadline:
            break
        day = today + timedelta(days=i)
        if day.weekday() >= 5:  # skip weekends — market is closed
            continue
        url = f"https://api.nasdaq.com/api/calendar/earnings?date={day.isoformat()}"
        try:
            r = session.get(url, timeout=8)
            body = r.text.lstrip()
            if r.status_code != 200 or not body.startswith("{"):
                bad_streak += 1
                if bad_streak >= EARNINGS_API_BLOCK_STREAK:
                    print("[earnings] Nasdaq calendar looks blocked/unavailable; "
                          "abandoning API scan (YAML remains authoritative)",
                          file=sys.stderr)
                    break
                continue
            bad_streak = 0
            data = r.json().get("data") or {}
            rows = data.get("rows") or []
            for row in rows:
                sym = str(row.get("symbol", "")).upper()
                if sym in remaining:
                    session = NASDAQ_TIME_MAP.get(row.get("time", ""), "unknown")
                    out[sym] = {"date": day, "session": session}
                    remaining.discard(sym)
        except Exception:
            bad_streak += 1
            if bad_streak >= EARNINGS_API_BLOCK_STREAK:
                print("[earnings] Nasdaq calendar unreachable; abandoning API scan",
                      file=sys.stderr)
                break
        time.sleep(0.2)  # be polite to the endpoint

    if out:
        print(f"[earnings] Nasdaq calendar filled {len(out)} estimate(s): "
              + ", ".join(sorted(out)), file=sys.stderr)
    return out


def build_earnings(today: date) -> list[Event]:
    """YAML is the source of truth. A `confirmed: true` date is sacrosanct and is
    never touched by the API. For unconfirmed (estimated) or missing tickers, the
    Nasdaq calendar refreshes the date — but the result stays flagged as an
    estimate, so the human still verifies before flipping `confirmed: true`."""
    yaml_rows = load_earnings_yaml()

    parsed: list[dict] = []
    confirmed_tickers: set[str] = set()
    for row in yaml_rows:
        try:
            ticker = str(row["ticker"]).upper()
            d = row["date"]
            if isinstance(d, str):
                d = date.fromisoformat(d)
            session = str(row.get("session", "unknown"))
            confirmed = bool(row.get("confirmed", False))
        except (KeyError, ValueError, TypeError) as e:
            print(f"[earnings] skipping malformed YAML row {row!r}: {e}", file=sys.stderr)
            continue
        parsed.append({"ticker": ticker, "date": d, "session": session,
                       "confirmed": confirmed})
        if confirmed:
            confirmed_tickers.add(ticker)

    # Only look up tickers we don't already have a confirmed date for.
    yaml_tickers = {p["ticker"] for p in parsed}
    wanted = (set(TICKERS) | yaml_tickers) - confirmed_tickers
    api = fetch_earnings_api(today, wanted)

    events: list[Event] = []
    seen: set[str] = set()
    for p in parsed:
        ticker, d, session, confirmed = (
            p["ticker"], p["date"], p["session"], p["confirmed"])
        seen.add(ticker)
        if not confirmed and ticker in api:
            nd, ns = api[ticker]["date"], api[ticker]["session"]
            if nd != d or ns != session:
                print(f"[earnings] {ticker}: refreshed estimate "
                      f"{d} {session} → {nd} {ns} (Nasdaq)", file=sys.stderr)
            events.append(_earnings_event(ticker, nd, ns, False,
                                          "nasdaq calendar (estimate)"))
        else:
            events.append(_earnings_event(ticker, d, session, confirmed, "earnings.yaml"))

    # Tickers absent from YAML entirely but found on the calendar.
    for ticker, info in api.items():
        if ticker in seen:
            continue
        events.append(_earnings_event(ticker, info["date"],
                                      info.get("session", "unknown"), False,
                                      "nasdaq calendar (estimate)"))

    if not events:
        raise RuntimeError(
            "no earnings data from Nasdaq or earnings.yaml — the earnings source is broken"
        )
    return events


# ─────────────────────────────────────────────────────────────────────────────
# 4. Outputs
# ─────────────────────────────────────────────────────────────────────────────


def write_data_json(events: list[Event], generated_at: datetime, feed_status: str):
    payload = {
        "generated_at": generated_at.isoformat(),
        "feed_status": feed_status,  # ok | stale | denied
        "timezone": "Europe/London",
        "events": [e.to_json() for e in events],
    }
    DATA_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"[out] wrote {DATA_JSON} ({len(events)} events, status={feed_status}).",
          file=sys.stderr)


def _ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 line folding at 75 octets."""
    out = []
    while len(line.encode("utf-8")) > 75:
        # find a cut point <=75 bytes
        cut = 75
        while len(line[:cut].encode("utf-8")) > 75:
            cut -= 1
        out.append(line[:cut])
        line = " " + line[cut:]
    out.append(line)
    return "\r\n".join(out)


def write_ics(events: list[Event], generated_at: datetime):
    stamp = generated_at.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//trading-events-calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(CAL_NAME)}",
        f"NAME:{_ics_escape(CAL_NAME)}",
        "X-PUBLISHED-TTL:PT6H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
    ]

    for e in events:
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{e.uid}")
        lines.append(f"DTSTAMP:{stamp}")
        if e.all_day:
            d = e.when.date()
            lines.append(f"DTSTART;VALUE=DATE:{d:%Y%m%d}")
            lines.append(f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}")
        else:
            utc = e.when.astimezone(ZoneInfo("UTC"))
            end = e.when + timedelta(minutes=30)
            lines.append(f"DTSTART:{utc:%Y%m%dT%H%M%SZ}")
            lines.append(f"DTEND:{end.astimezone(ZoneInfo('UTC')):%Y%m%dT%H%M%SZ}")

        summary = e.title
        if not e.confirmed:
            summary = "≈ " + summary  # visually flag estimates in calendar clients
        lines.append(f"SUMMARY:{_ics_escape(summary)}")
        if e.detail:
            lines.append(f"DESCRIPTION:{_ics_escape(e.detail)}")
        lines.append(f"CATEGORIES:{_ics_escape(e.category.upper())}")
        lines.append("TRANSP:TRANSPARENT")

        # Timed events get a 30-minute-prior alarm; all-day events do not.
        if not e.all_day:
            lines.append("BEGIN:VALARM")
            lines.append("ACTION:DISPLAY")
            lines.append(f"DESCRIPTION:{_ics_escape(e.title)}")
            lines.append("TRIGGER:-PT30M")
            lines.append("END:VALARM")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    folded = "\r\n".join(_fold(ln) for ln in lines) + "\r\n"
    EVENTS_ICS.write_text(folded)
    print(f"[out] wrote {EVENTS_ICS} ({len(events)} VEVENTs).", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    DOCS.mkdir(exist_ok=True)
    now_london = datetime.now(TZ_LONDON)
    today = now_london.date()

    # --- Deterministic dates (must always succeed) --------------------------
    cal_start = today - timedelta(days=10)
    cal_end = today + timedelta(days=365 * HORIZON_YEARS + 90)
    xnys = get_calendar()
    trading_days = get_trading_days(xnys, cal_start, cal_end)
    horizon_last = today + timedelta(days=365 * HORIZON_YEARS)
    structural = compute_structural(today, trading_days)
    structural += compute_holidays(xnys, today, horizon_last, trading_days)
    structural += compute_early_closes(xnys, today, horizon_last)
    print(f"[structural] {len(structural)} computed events.", file=sys.stderr)

    # --- Earnings (fail loudly if the source is entirely broken) ------------
    earnings = build_earnings(today)
    print(f"[earnings] {len(earnings)} events.", file=sys.stderr)

    # --- Macro (defensive; a denied fetch preserves the previous data.json) --
    feed_status = "ok"
    macro: list[Event] = []
    macro_denied = False
    try:
        macro = fetch_macro()
    except MacroDenied as e:
        macro_denied = True
        print(f"[macro] DENIED: {e}", file=sys.stderr)

    if macro_denied:
        # Keep the previous data.json untouched; still refresh the deterministic
        # ICS? No — leaving both outputs untouched avoids partial/torn state.
        # But we MUST surface staleness. If a previous data.json exists, bump its
        # feed_status to "denied" without touching its events; otherwise write a
        # denied stub so the page can show the banner.
        if DATA_JSON.exists():
            try:
                prev = json.loads(DATA_JSON.read_text())
                prev["feed_status"] = "denied"
                DATA_JSON.write_text(json.dumps(prev, indent=2, ensure_ascii=False) + "\n")
                print("[macro] preserved previous data.json, marked feed_status=denied.",
                      file=sys.stderr)
            except Exception as e:
                print(f"[macro] could not update previous data.json: {e}", file=sys.stderr)
        else:
            write_data_json(
                sorted(structural + earnings, key=lambda ev: ev.sort_key()),
                now_london, "denied",
            )
        # Fail loudly so the scheduled run emails the owner.
        print("::error::macro fetch denied (rate limited); data left stale.",
              file=sys.stderr)
        return 2

    # --- Scheduled macro (official Fed/BLS/BEA dates) + Treasury auctions ----
    scheduled = load_macro_schedule(today, macro)
    auctions = fetch_treasury_auctions(today)

    # --- Merge, sort, write --------------------------------------------------
    all_events = sorted(structural + earnings + macro + scheduled + auctions,
                        key=lambda ev: ev.sort_key())
    write_data_json(all_events, now_london, feed_status)
    write_ics(all_events, now_london)
    print(f"[done] {len(all_events)} total events.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        print("::error::build.py failed — outputs may be stale.", file=sys.stderr)
        sys.exit(1)
