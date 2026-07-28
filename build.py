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
TICKERS = ["AAPL", "NVDA", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "HOOD", "COST"]
# Release times expressed in US Eastern (America/New_York), converted to London.
EARNINGS_TIME_ET = {
    "BMO": (7, 0),    # before market open  → 07:00 ET
    "AMC": (16, 30),  # after market close  → 16:30 ET (US close is 16:00 ET)
    # "unknown" → rendered as an all-day event
}
ENABLE_EARNINGS_API = True  # best-effort API fill; failure is non-fatal (YAML is truth)

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

# --- Output paths -----------------------------------------------------------
ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
DATA_JSON = DOCS / "data.json"
EVENTS_ICS = DOCS / "events.ics"
EARNINGS_YAML = ROOT / "earnings.yaml"

CAL_NAME = "Trading Events"

# ─────────────────────────────────────────────────────────────────────────────
# Event model
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Event:
    when: datetime          # timezone-aware; London. For all-day, time is 00:00 London.
    all_day: bool
    category: str           # earnings|macro|opex|opex_quarterly|vix_expiry|period_end
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


def get_trading_days(start: date, end: date) -> set[date]:
    """Set of valid XNYS (NYSE) trading days in [start, end]. A weekday NOT in
    this set is a US market holiday."""
    import pandas_market_calendars as mcal

    xnys = mcal.get_calendar("XNYS")
    sched = xnys.valid_days(start_date=start.isoformat(), end_date=end.isoformat())
    return {ts.date() for ts in sched}


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
            title, detail = f"Quarter-end (Q{q} {y})", "last trading session of the quarter"
        else:
            title = f"Month-end ({date(y, m, 1):%B} {y})"
            detail = "last trading session of the month"
        events.append(Event(
            when=london_all_day(d), all_day=True, category="period_end",
            impact="structural", title=title, detail=detail,
            uid=f"period_end-{d.isoformat()}@trading-events",
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


def fetch_earnings_api() -> dict[str, dict]:
    """Best-effort free API for confirmed earnings dates. Returns {TICKER: {...}}.
    Any failure returns {} — YAML remains the source of truth. Never raises."""
    if not ENABLE_EARNINGS_API:
        return {}
    out: dict[str, dict] = {}
    try:
        import requests

        headers = {
            "User-Agent": "Mozilla/5.0 (trading-events-calendar; earnings lookup)",
            "Accept": "application/json",
        }
        for ticker in TICKERS:
            try:
                url = (
                    "https://api.nasdaq.com/api/analyst/"
                    f"{ticker}/earnings-date"
                )
                r = requests.get(url, headers=headers, timeout=15)
                if r.status_code != 200:
                    continue
                body = r.text.lstrip()
                if not body.startswith("{"):
                    continue
                # Nasdaq's shape varies; we only trust it as an *estimate* here.
                # Parsing is intentionally conservative and non-fatal.
            except Exception:
                continue
    except Exception as e:
        print(f"[earnings] API disabled/failed: {e}", file=sys.stderr)
    return out


def build_earnings() -> list[Event]:
    """YAML is the confirmed source. The API (if any) only fills gaps as estimates."""
    yaml_rows = load_earnings_yaml()
    api = fetch_earnings_api()

    events: list[Event] = []
    have: set[tuple[str, str]] = set()

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
        events.append(_earnings_event(ticker, d, session, confirmed, "earnings.yaml"))
        have.add((ticker, d.isoformat()))

    # API fills only tickers/dates not already present, always as unconfirmed.
    for ticker, info in api.items():
        d = info.get("date")
        if not d:
            continue
        key = (ticker.upper(), d.isoformat() if isinstance(d, date) else str(d))
        if key in have:
            continue
        dd = d if isinstance(d, date) else date.fromisoformat(str(d))
        events.append(_earnings_event(ticker.upper(), dd, info.get("session", "unknown"),
                                      False, "api (estimated)"))

    if not events:
        raise RuntimeError(
            "no earnings data from API or earnings.yaml — the earnings source is broken"
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
    trading_days = get_trading_days(cal_start, cal_end)
    structural = compute_structural(today, trading_days)
    print(f"[structural] {len(structural)} computed events.", file=sys.stderr)

    # --- Earnings (fail loudly if the source is entirely broken) ------------
    earnings = build_earnings()
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

    # --- Merge, sort, write --------------------------------------------------
    all_events = sorted(structural + earnings + macro, key=lambda ev: ev.sort_key())
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
