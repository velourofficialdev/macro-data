"""Fed Rate Odds: market-implied probability of a Fed hike, hold or cut at each upcoming FOMC meeting,
calculated from CME 30-Day Fed Funds futures (ZQ) and the current Effective Federal Funds Rate (EFFR).

Method (per meeting, using the ZQ contract for the month that contains the decision):
    implied_average_rate      = 100 - ZQ price
    days_before               = decision day of month (the new rate takes effect the day after the decision)
    days_after                = days in month - days_before
    implied_post_meeting_rate = (implied_average_rate * days_in_month - days_before * EFFR) / days_after
    implied_rate_change       = EFFR - implied_post_meeting_rate      (positive = cut priced, negative = hike)
    cut  probability          = change / 0.25 (when change > 0), hold = 1 - cut, hike = 0
    hike probability          = |change| / 0.25 (when change < 0), hold = 1 - hike, cut = 0
    probabilities capped to 0..100%; a move larger than 25 bp is flagged (50 bp or several moves priced)

If the decision falls on the last day of its month (days_after = 0) the month's average cannot reveal the new rate,
so the next month's contract is used instead (its whole month is at the post-meeting rate).

Modular pieces:
    FOMC calendar  -> fetch_fomc_dates()      (Federal Reserve website, cached to data/fed/fomc_dates.csv)
    EFFR           -> fetch_effr()            (FRED series EFFR, no key)
    ZQ prices      -> PriceProvider classes   (YahooZQ, ManualZQ, ChainProvider); swap in a live feed by adding a class
    calculation    -> implied_odds()
    storage        -> update_snapshots()      (CSV: data/fed/zq_odds.csv, one row per snapshot_date x meeting_date)
"""
from __future__ import annotations

import calendar
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
FED_DIR = ROOT / "data" / "fed"
SNAPSHOT_CSV = FED_DIR / "zq_odds.csv"
FOMC_CACHE = FED_DIR / "fomc_dates.csv"
MANUAL_CSV = FED_DIR / "zq_manual.csv"          # optional: date,contract,price  (manually entered prices win)
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
HEADERS = {"User-Agent": "Mozilla/5.0 (macro-dashboard; personal research)"}
MONTH_CODES = "FGHJKMNQUVXZ"
STEP = 0.25                                     # a standard Fed move, in percentage points
MIN_DAYS_AFTER = 7                              # fewer days than this after a decision -> use the next month's contract
BACKFILL_DAYS = 180                             # history rebuilt from daily ZQ closes on each run
SNAPSHOT_FIELDS = ["snapshot_date", "meeting_date", "zq_contract", "zq_price", "current_effr", "pre_meeting_rate", "implied_average_rate",
                   "implied_post_meeting_rate", "implied_rate_change_bp", "cumulative_change_vs_effr_bp", "cut_probability", "hold_probability",
                   "hike_probability", "cut_probability_change_1d", "hike_probability_change_1d",
                   "cut_probability_change_1w", "hike_probability_change_1w", "large_move_flag", "note"]


class FedOddsError(RuntimeError):
    """Raised when an input the calculation needs (EFFR, FOMC dates, a ZQ price) is missing."""


# --------------------------------------------------------------------------- FOMC calendar
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})


def parse_fomc_calendar(html: str) -> list[date]:
    """Decision dates (second day of each two-day meeting) from the Fed's FOMC calendar page."""
    out: list[date] = []
    parts = re.split(r"(\d{4}) FOMC Meetings", html)
    for k in range(1, len(parts) - 1, 2):
        year, body = int(parts[k]), parts[k + 1]
        months = re.findall(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>', body)
        days = re.findall(r'fomc-meeting__date[^>]*>([^<]+)<', body)
        for mon, day in zip(months, days):
            if "notation" in day.lower() or "unscheduled" in day.lower():
                continue
            mon_parts = [m.strip().lower() for m in mon.split("/")]
            nums = [int(x) for x in re.findall(r"\d+", day)]
            if not nums or mon_parts[-1] not in MONTHS:
                continue
            last_month = MONTHS[mon_parts[-1]]
            yy = year + 1 if (len(mon_parts) == 2 and MONTHS.get(mon_parts[0]) == 12 and last_month == 1) else year
            try:
                out.append(date(yy, last_month, nums[-1]))
            except ValueError:
                continue
    return sorted(set(out))


def fetch_fomc_dates() -> list[date]:
    """Decision dates from the Fed website; falls back to the last saved copy. Raises FedOddsError if neither works."""
    try:
        r = requests.get(FOMC_URL, headers=HEADERS, timeout=60)
        r.raise_for_status()
        dates = parse_fomc_calendar(r.text)
        if len(dates) < 4:
            raise FedOddsError("FOMC calendar page parsed to fewer than 4 meetings")
        FED_DIR.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"decision_date": [d.isoformat() for d in dates]}).to_csv(FOMC_CACHE, index=False)
        return dates
    except Exception as exc:  # noqa: BLE001
        if FOMC_CACHE.exists():
            return [date.fromisoformat(x) for x in pd.read_csv(FOMC_CACHE)["decision_date"]]
        raise FedOddsError(f"FOMC dates unavailable ({type(exc).__name__}: {exc}) and no cached calendar") from exc


# --------------------------------------------------------------------------- EFFR
def fetch_effr(years: int = 1) -> pd.Series:
    start = (date.today() - timedelta(days=365 * years + 10)).isoformat()
    try:
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": "EFFR", "cosd": start}, timeout=30)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        df.columns = ["date", "EFFR"]
        s = pd.to_numeric(df["EFFR"], errors="coerce")
        s.index = pd.to_datetime(df["date"])
        s = s.dropna()
    except Exception as exc:  # noqa: BLE001
        raise FedOddsError(f"EFFR unavailable from FRED ({type(exc).__name__}: {exc})") from exc
    if s.empty:
        raise FedOddsError("EFFR series is empty")
    return s


def effr_on(effr: pd.Series, day: date) -> float:
    s = effr[effr.index <= pd.Timestamp(day)]
    if s.empty:
        raise FedOddsError(f"no EFFR value on or before {day}")
    return float(s.iloc[-1])


# --------------------------------------------------------------------------- ZQ contracts and prices
def contract_code(year: int, month: int) -> str:
    return f"ZQ{MONTH_CODES[month - 1]}{year % 100:02d}"


def contract_for_meeting(meeting: date) -> tuple[str, int, int]:
    """(contract, year, month) used for this meeting: the decision month, or the next month if the decision is on its last day."""
    days = calendar.monthrange(meeting.year, meeting.month)[1]
    if meeting.day >= days:
        y, m = (meeting.year + 1, 1) if meeting.month == 12 else (meeting.year, meeting.month + 1)
        return contract_code(y, m), y, m
    return contract_code(meeting.year, meeting.month), meeting.year, meeting.month


class PriceProvider:
    """Interface: daily ZQ settlement/close history for one contract, as a date-indexed Series of prices."""
    name = "base"

    def history(self, contract: str) -> pd.Series:  # pragma: no cover - interface
        raise NotImplementedError


class YahooZQ(PriceProvider):
    """Daily closes from Yahoo Finance (ticker like ZQZ26.CBT). Delayed data; good for end-of-day snapshots."""
    name = "Yahoo Finance"

    def history(self, contract: str) -> pd.Series:
        for attempt in range(4):
            r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{contract}.CBT",
                             params={"range": "1y", "interval": "1d"}, headers=HEADERS, timeout=30)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            res = (r.json().get("chart") or {}).get("result")
            if not res:
                return pd.Series(dtype=float)
            res = res[0]
            closes = res["indicators"]["quote"][0].get("close") or []
            s = pd.Series(closes, index=pd.to_datetime(res.get("timestamp") or [], unit="s").normalize(), dtype=float).dropna()
            return s[~s.index.duplicated(keep="last")]
        raise FedOddsError(f"Yahoo rate limit while fetching {contract}")


class ManualZQ(PriceProvider):
    """Prices typed into data/fed/zq_manual.csv (columns: date, contract, price)."""
    name = "manual entry"

    def history(self, contract: str) -> pd.Series:
        if not MANUAL_CSV.exists():
            return pd.Series(dtype=float)
        df = pd.read_csv(MANUAL_CSV)
        df = df[df["contract"].str.upper() == contract.upper()]
        return pd.Series(pd.to_numeric(df["price"], errors="coerce").values, index=pd.to_datetime(df["date"])).dropna().sort_index()


class ChainProvider(PriceProvider):
    """Combine providers: manual prices override, the first live provider fills the rest."""
    def __init__(self, *providers: PriceProvider):
        self.providers = providers
        self.name = " + ".join(p.name for p in providers)

    def history(self, contract: str) -> pd.Series:
        merged = pd.Series(dtype=float)
        for p in reversed(self.providers):          # earlier providers win
            try:
                s = p.history(contract)
            except Exception:  # noqa: BLE001 - one provider failing must not hide the others
                continue
            if len(s):
                merged = s.combine_first(merged) if len(merged) else s
        return merged.sort_index()


# --------------------------------------------------------------------------- calculation
@dataclass
class Odds:
    meeting_date: date
    zq_contract: str
    zq_price: float
    current_effr: float
    pre_meeting_rate: float
    implied_average_rate: float
    implied_post_meeting_rate: float
    implied_rate_change_bp: float
    cumulative_change_vs_effr_bp: float
    cut_probability: float
    hold_probability: float
    hike_probability: float
    large_move_flag: bool
    note: str


def probabilities(change: float) -> tuple[float, float, float, bool]:
    """change = pre-meeting rate - post-meeting rate (positive = cut). Returns cut, hold, hike in % and the >25 bp flag."""
    cut = change / STEP if change > 0 else 0.0
    hike = -change / STEP if change < 0 else 0.0
    cut, hike = min(max(cut, 0.0), 1.0), min(max(hike, 0.0), 1.0)
    return round(cut * 100, 1), round((1 - cut - hike) * 100, 1), round(hike * 100, 1), abs(change) > STEP + 1e-9


def implied_odds(meeting: date, zq_price: float, effr: float, pre_rate: float | None = None,
                 next_month_price: float | None = None) -> Odds:
    """One meeting. pre_rate = rate going into the meeting (current EFFR for the next meeting; the previous meeting's
    implied post-meeting rate for later ones). next_month_price = ZQ price of the following month when that month has no
    meeting; used (standard CME practice) when the decision leaves fewer than MIN_DAYS_AFTER days in its own month."""
    pre = effr if pre_rate is None else pre_rate
    contract, y, m = contract_for_meeting(meeting)
    total = calendar.monthrange(y, m)[1]
    avg = 100.0 - zq_price
    notes = []
    if (y, m) != (meeting.year, meeting.month):
        post = avg
        notes.append(f"decision on the last day of the month, so {contract} (whole month after the decision) is used")
    else:
        days_before = meeting.day
        days_after = total - days_before
        if days_after < MIN_DAYS_AFTER and next_month_price is not None:
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            contract, avg, post = contract_code(ny, nm), 100.0 - next_month_price, 100.0 - next_month_price
            zq_price = next_month_price
            notes.append(f"only {days_after} days after the decision, so the next month's contract {contract} is used")
        else:
            post = (avg * total - days_before * pre) / days_after
            if days_after < MIN_DAYS_AFTER:
                notes.append(f"only {days_after} days after the decision in {contract}; sensitive to small price moves")
    change = pre - post
    cut, hold, hike, large = probabilities(change)
    if large:
        notes.append(f"implied move {abs(change) * 100:.0f} bp > 25 bp: market may be pricing a 50 bp move or more than one move")
    return Odds(meeting, contract, round(zq_price, 4), effr, round(pre, 4), round(avg, 4), round(post, 4), round(change * 100, 1),
                round((effr - post) * 100, 1), cut, hold, hike, large, "; ".join(notes))


def meeting_path(meetings: list[date], prices: dict[str, float], effr: float) -> tuple[list[Odds], list[str]]:
    """All meetings for one day, chained: each meeting starts from the previous meeting's implied post-meeting rate."""
    out, warns = [], []
    meeting_months = {(d.year, d.month) for d in meetings}
    pre = effr
    for mt in meetings:
        contract = contract_for_meeting(mt)[0]
        if contract not in prices:
            warns.append(f"{mt.isoformat()}: no price for {contract}; later meetings skipped (they chain off this one)")
            break
        ny, nm = (mt.year + 1, 1) if mt.month == 12 else (mt.year, mt.month + 1)
        nxt = prices.get(contract_code(ny, nm)) if (ny, nm) not in meeting_months else None
        o = implied_odds(mt, prices[contract], effr, pre, nxt)
        out.append(o)
        pre = o.implied_post_meeting_rate
    return out, warns


# --------------------------------------------------------------------------- snapshots
def _changes(df: pd.DataFrame) -> pd.DataFrame:
    """1-day (previous snapshot) and 1-week (latest snapshot at least 7 days earlier) changes, per meeting, in pp."""
    df = df.sort_values(["meeting_date", "snapshot_date"]).copy()
    for col in ("cut_probability_change_1d", "hike_probability_change_1d", "cut_probability_change_1w", "hike_probability_change_1w"):
        df[col] = float("nan")
    for _, g in df.groupby("meeting_date"):
        dates = pd.to_datetime(g["snapshot_date"])
        for i, (idx, row) in enumerate(g.iterrows()):
            if i > 0:
                prev = g.iloc[i - 1]
                df.at[idx, "cut_probability_change_1d"] = round(row["cut_probability"] - prev["cut_probability"], 1)
                df.at[idx, "hike_probability_change_1d"] = round(row["hike_probability"] - prev["hike_probability"], 1)
            wk = g[dates <= dates.iloc[i] - pd.Timedelta(days=7)]
            if len(wk):
                w = wk.iloc[-1]
                df.at[idx, "cut_probability_change_1w"] = round(row["cut_probability"] - w["cut_probability"], 1)
                df.at[idx, "hike_probability_change_1w"] = round(row["hike_probability"] - w["hike_probability"], 1)
    return df


def update_snapshots(provider: PriceProvider | None = None, meetings_ahead: int = 8, today: date | None = None) -> tuple[pd.DataFrame, list[str]]:
    """Fetch inputs, (re)compute daily snapshots for the upcoming meetings over the last BACKFILL_DAYS, merge with the
    stored CSV, recompute changes and save. Returns (all snapshots, warnings)."""
    provider = provider or ChainProvider(ManualZQ(), YahooZQ())
    today = today or datetime.now(timezone.utc).date()
    warnings: list[str] = []
    effr = fetch_effr()
    all_meetings = fetch_fomc_dates()
    meetings = [d for d in all_meetings if d >= today][:meetings_ahead]
    past = [d for d in all_meetings if d < today]
    if not meetings:
        raise FedOddsError("no upcoming FOMC meetings in the calendar")
    # Backfill only since the last decision: before it, that meeting was still ahead and the chain would be different.
    start = pd.Timestamp(max(today - timedelta(days=BACKFILL_DAYS), (past[-1] + timedelta(days=1)) if past else today))
    needed = set()
    for mt in meetings:
        needed.add(contract_for_meeting(mt)[0])
        ny, nm = (mt.year + 1, 1) if mt.month == 12 else (mt.year, mt.month + 1)
        needed.add(contract_code(ny, nm))
    series = {}
    for c in sorted(needed):
        try:
            px = provider.history(c)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{c}: price fetch failed ({type(exc).__name__}: {exc})")
            continue
        if len(px):
            series[c] = px
        elif any(contract_for_meeting(mt)[0] == c for mt in meetings):
            warnings.append(f"{c}: no ZQ price available")
    if not series:
        raise FedOddsError("no ZQ prices for any upcoming meeting: " + "; ".join(warnings))
    frame = pd.DataFrame(series).sort_index().ffill(limit=3)
    frame = frame[frame.index >= start]
    rows = []
    for d, prices_row in frame.iterrows():
        prices = {c: float(v) for c, v in prices_row.items() if pd.notna(v)}
        try:
            path, warns = meeting_path(meetings, prices, effr_on(effr, d.date()))
        except FedOddsError as exc:
            warnings.append(f"{d.date()}: {exc}")
            continue
        if d == frame.index[-1]:
            warnings += warns
        for o in path:
            row = {k: getattr(o, k) for k in o.__dataclass_fields__}
            row["snapshot_date"] = d.strftime("%Y-%m-%d")
            row["meeting_date"] = o.meeting_date.isoformat()
            rows.append(row)
    new = pd.DataFrame(rows)
    if new.empty:
        raise FedOddsError("no ZQ prices for any upcoming meeting: " + "; ".join(warnings))
    FED_DIR.mkdir(parents=True, exist_ok=True)
    if SNAPSHOT_CSV.exists():
        old = pd.read_csv(SNAPSHOT_CSV)
        new = pd.concat([old, new], ignore_index=True)
    new = new.drop_duplicates(["snapshot_date", "meeting_date"], keep="last")
    new = _changes(new)
    new = new.reindex(columns=SNAPSHOT_FIELDS).sort_values(["snapshot_date", "meeting_date"])
    new.to_csv(SNAPSHOT_CSV, index=False)
    return new, warnings


# --------------------------------------------------------------------------- event calendar (for labelling big moves)
EVENTS_CSV = FED_DIR / "events.csv"
FRED_RELEASES = {10: "CPI", 50: "Jobs report (NFP)", 54: "PCE"}


def fetch_event_calendar(years: list[int] | None = None) -> pd.DataFrame:
    """Release dates for CPI, the jobs report and PCE (FRED release calendar pages) plus FOMC decision dates.
    Falls back to the saved copy if the pages cannot be read."""
    years = years or [date.today().year - 1, date.today().year]
    rows = []
    try:
        for rid, name in FRED_RELEASES.items():
            for y in years:
                r = requests.get("https://fred.stlouisfed.org/releases/calendar", params={"rid": rid, "y": y}, timeout=30)   # FRED stalls browser-style User-Agents
                r.raise_for_status()
                for txt in set(re.findall(r"(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, " + str(y), r.text)):
                    rows.append({"date": datetime.strptime(txt, "%B %d, %Y").date().isoformat(), "event": name})
        for d in fetch_fomc_dates():
            rows.append({"date": d.isoformat(), "event": "FOMC decision"})
        df = pd.DataFrame(rows).drop_duplicates().sort_values("date")
        FED_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(EVENTS_CSV, index=False)
        return df
    except Exception:  # noqa: BLE001
        if EVENTS_CSV.exists():
            return pd.read_csv(EVENTS_CSV)
        return pd.DataFrame(columns=["date", "event"])


if __name__ == "__main__":
    df, warns = update_snapshots()
    last = df[df["snapshot_date"] == df["snapshot_date"].max()]
    print(last[["meeting_date", "zq_contract", "zq_price", "pre_meeting_rate", "implied_post_meeting_rate", "implied_rate_change_bp",
                "cut_probability", "hold_probability", "hike_probability", "cut_probability_change_1d", "hike_probability_change_1w", "note"]].to_string(index=False))
    for w in warns:
        print("WARN", w)
