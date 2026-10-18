#!/usr/bin/env python3
"""Download the latest market data into data/*.csv.

Sources (all free, no API key):
  - FRED  : US Treasury yields + 10Y real yield   -> data/<CODE>.csv
  - CFTC  : Gold and USD Index COT, both from the Legacy Futures Only report (Non-Commercial)
            -> data/COT_GOLD_LEGACY.csv, data/COT_USD_INDEX.csv
  - FX dashboard feed (deiruuuu/fx-macro-confluence data.json) -> data/fx_macro.json
  - Yahoo : DXY price (fallback: Stooq)          -> data/DXY.csv
            Gold futures price (GC=F)            -> data/GOLD.csv   (used for the weekly price change next to Gold COT)

A failed source never wipes existing data: the old file is kept and the error
is written to data/status.json, which the dashboard shows in its footer.
Exit code is 1 only if every source failed.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

FRED_CODES = ["DGS3MO", "DGS2", "DGS5", "DGS10", "DGS20", "DGS30", "DFII10"]
KEEP_DAILY = 2700     # rows kept per daily series (about 10 years; the 252-day window needs only 252)
HISTORY_YEARS = 10
CFTC_LIMIT = 180      # weekly reports fetched (52 window + history)
HEADERS = {"User-Agent": "Mozilla/5.0 (macro-dashboard; personal research)"}
TIMEOUT = 60
FX_FEED_URL = "https://deiruuuu.github.io/fx-macro-confluence/data.json"

CFTC_SOURCES = {
    # name: (dataset id, contract code, long field, short field, exchange keyword)
    "COT_GOLD_LEGACY": ("6dca-aqww", "088691", "noncomm_positions_long_all",
                        "noncomm_positions_short_all", "COMMODITY EXCHANGE"),
    "COT_USD_INDEX": ("6dca-aqww", "098662", "noncomm_positions_long_all",
                      "noncomm_positions_short_all", "ICE FUTURES"),
}


def merge_save(path: Path, new: pd.DataFrame, key: str, keep: int) -> pd.DataFrame:
    """Union with any existing file (new rows win), sort, trim, write."""
    if path.exists():
        old = pd.read_csv(path)
        old[key] = pd.to_datetime(old[key], errors="coerce")
        new = pd.concat([old[new.columns.intersection(old.columns)], new], ignore_index=True)
    new[key] = pd.to_datetime(new[key]).dt.normalize()
    new = (new.dropna(subset=[key]).drop_duplicates(key, keep="last")
              .sort_values(key).tail(keep).reset_index(drop=True))
    new[key] = new[key].dt.strftime("%Y-%m-%d")
    new.to_csv(path, index=False)
    return new


def fetch_fred(code: str) -> pd.DataFrame:
    # FRED drops connections that send a browser-style User-Agent, so use the library default here.
    start = (pd.Timestamp.today().normalize() - pd.Timedelta(days=HISTORY_YEARS * 365)).strftime("%Y-%m-%d")
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                     params={"id": code, "cosd": start}, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    if df.shape[1] != 2:
        raise ValueError(f"unexpected FRED columns: {df.columns.tolist()}")
    df.columns = ["date", code]
    df[code] = pd.to_numeric(df[code], errors="coerce")   # FRED uses "." for holidays
    df = df.dropna()
    if df.empty:
        raise ValueError("no rows returned")
    return df


def fetch_cftc(dataset: str, contract: str, long_f: str, short_f: str, keyword: str) -> pd.DataFrame:
    r = requests.get(
        f"https://publicreporting.cftc.gov/resource/{dataset}.json",
        params={"$where": f"cftc_contract_market_code = '{contract}'",
                "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": CFTC_LIMIT},
        headers={**HEADERS, "Accept": "application/json"}, timeout=90)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        raise ValueError("no CFTC records returned")
    f = pd.DataFrame(rows)
    missing = {long_f, short_f, "report_date_as_yyyy_mm_dd"} - set(f.columns)
    if missing:
        raise KeyError(f"missing CFTC fields: {sorted(missing)}")
    if "market_and_exchange_names" in f:
        f = f[f["market_and_exchange_names"].astype(str).str.contains(keyword, case=False, na=False)]
    if f.empty:
        raise ValueError("no rows after exchange check")

    def num(col):
        return pd.to_numeric(f[col].astype(str).str.replace(",", ""), errors="coerce")

    out = pd.DataFrame({
        "Date": pd.to_datetime(f["report_date_as_yyyy_mm_dd"], errors="coerce").dt.tz_localize(None),
        "Long": num(long_f),
        "Short": num(short_f),
        "Open_Interest": num("open_interest_all") if "open_interest_all" in f else float("nan"),
    }).dropna(subset=["Date", "Long", "Short"])
    return out


def fetch_yahoo(symbol: str, col: str) -> pd.DataFrame:
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     params={"range": f"{HISTORY_YEARS}y", "interval": "1d"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    close = res["indicators"]["quote"][0]["close"]
    df = pd.DataFrame({
        "Date": pd.to_datetime(res["timestamp"], unit="s").normalize(),
        col: pd.to_numeric(close, errors="coerce"),
    }).dropna()
    if df.empty:
        raise ValueError("Yahoo returned no closes")
    return df


def fetch_dxy_yahoo() -> pd.DataFrame:
    return fetch_yahoo("DX-Y.NYB", "DXY")


def fetch_dxy_stooq() -> pd.DataFrame:
    r = requests.get("https://stooq.com/q/d/l/", params={"s": "dx.f", "i": "d"},
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    if "Close" not in df.columns or df.empty:
        raise ValueError("Stooq returned no data")
    return pd.DataFrame({"Date": pd.to_datetime(df["Date"]), "DXY": pd.to_numeric(df["Close"], errors="coerce")}).dropna()


def main() -> int:
    status: dict[str, dict] = {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def record(name: str, ok: bool, last: str | None = None, error: str | None = None, note: str | None = None):
        status[name] = {"ok": ok, "checked_utc": now, "last_data_date": last, "error": error, "note": note}
        print(("OK   " if ok else "FAIL ") + name + (f"  last={last}" if last else "") + (f"  {error}" if error else ""))

    for code in FRED_CODES:
        try:
            saved = merge_save(DATA / f"{code}.csv", fetch_fred(code).rename(columns={"date": "date"}), "date", KEEP_DAILY)
            record(code, True, saved["date"].iloc[-1])
        except Exception as exc:  # noqa: BLE001 - keep going, report in status
            record(code, False, error=f"{type(exc).__name__}: {exc}"[:200])

    for name, (ds, code, lf, sf, kw) in CFTC_SOURCES.items():
        try:
            saved = merge_save(DATA / f"{name}.csv", fetch_cftc(ds, code, lf, sf, kw), "Date", CFTC_LIMIT)
            record(name, True, saved["Date"].iloc[-1])
        except Exception as exc:  # noqa: BLE001
            record(name, False, error=f"{type(exc).__name__}: {exc}"[:200])

    for label, fn in (("Yahoo", fetch_dxy_yahoo), ("Stooq", fetch_dxy_stooq)):
        try:
            saved = merge_save(DATA / "DXY.csv", fn(), "Date", KEEP_DAILY)
            record("DXY", True, saved["Date"].iloc[-1], note=f"source: {label}")
            break
        except Exception as exc:  # noqa: BLE001
            err = f"{label}: {type(exc).__name__}: {exc}"[:200]
            if label == "Stooq":
                record("DXY", False, error=err)
            else:
                print(f"WARN DXY via {label} failed ({err}); trying fallback")

    try:
        saved = merge_save(DATA / "GOLD.csv", fetch_yahoo("GC=F", "Gold"), "Date", KEEP_DAILY)
        record("GOLD_PRICE", True, saved["Date"].iloc[-1], note="source: Yahoo GC=F")
    except Exception as exc:  # noqa: BLE001
        record("GOLD_PRICE", False, error=f"{type(exc).__name__}: {exc}"[:200])

    try:
        r = requests.get(FX_FEED_URL, timeout=30)
        r.raise_for_status()
        feed = r.json()
        if not isinstance(feed, dict) or "fx" not in feed:
            raise ValueError("unexpected feed shape")
        (DATA / "fx_macro.json").write_text(json.dumps(feed))
        record("FX_MACRO", True, str(feed.get("updated", ""))[:10], note="source: fx-macro-confluence data.json")
    except Exception as exc:  # noqa: BLE001
        record("FX_MACRO", False, error=f"{type(exc).__name__}: {exc}"[:200])

    (DATA / "status.json").write_text(json.dumps(status, indent=2))
    return 0 if any(s["ok"] for s in status.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
