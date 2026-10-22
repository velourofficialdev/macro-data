#!/usr/bin/env python3
"""Download the latest market data into data/*.csv.

Sources (all free, no API key):
  - FRED  : US Treasury yields + 10Y real yield   -> data/<CODE>.csv
            US macro (inflation, growth, labor)    -> data/econ/<CODE>.csv
  - CFTC  : Gold and USD Index COT, both from the Legacy Futures Only report (Non-Commercial)
            -> data/COT_GOLD_LEGACY.csv, data/COT_USD_INDEX.csv
  - Canada (Bank of Canada), Germany (Bundesbank), Japan (MoF), Australia (RBA): 2y/5y/10y/30y yields -> data/YC_<CC>.csv
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

ECON_CODES = [   # US macro releases from FRED -> data/econ/<CODE>.csv (12 years so year-on-year changes cover the 10-year chart)
    "CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE", "GDPDEF",
    "GDP", "GDPC1", "GDIC1", "INDPRO", "RRSFS", "PCE",
    "UNRATE", "CIVPART", "EMRATIO", "CLF16OV", "CE16OV", "UNEMPLOY", "PAYEMS", "CES0500000003",
    "ICSA", "CCSA", "JTSJOL", "JTSQUR",
]
MARKET_CODES = ["SP500", "DCOILWTICO", "VIXCLS", "CBBTCUSD"]   # tiles in the overview (FRED, daily) -> data/econ/<CODE>.csv
FRED_ALIAS = {"GDIC1": "A261RX1Q020SBEA"}   # FRED has no GDIC1 id; this is Real Gross Domestic Income (chained 2017 $)
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


def fetch_fred(code: str, years: int = HISTORY_YEARS) -> pd.DataFrame:
    # FRED drops connections that send a browser-style User-Agent, so use the library default here.
    start = (pd.Timestamp.today().normalize() - pd.Timedelta(days=years * 365)).strftime("%Y-%m-%d")
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


# ---- other countries: government yield curves from central-bank / ministry sources (daily, free, no key)
INTL = {"CA": "Canada", "DE": "Germany", "JP": "Japan", "AU": "Australia"}
INTL_KEEP = 2700


def _start() -> str:
    return (datetime.now(timezone.utc) - pd.Timedelta(days=365 * HISTORY_YEARS + 10)).strftime("%Y-%m-%d")


def fetch_yc_ca() -> pd.DataFrame:
    """Bank of Canada Valet: benchmark bond yields (2y, 5y, 10y, long = about 30y)."""
    ids = {"BD.CDN.2YR.DQ.YLD": "2Y", "BD.CDN.5YR.DQ.YLD": "5Y", "BD.CDN.10YR.DQ.YLD": "10Y", "BD.CDN.LONG.DQ.YLD": "30Y"}
    r = requests.get("https://www.bankofcanada.ca/valet/observations/group/bond_yields_benchmark/json",
                     params={"start_date": _start()}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    rows = []
    for o in r.json()["observations"]:
        row = {"date": o["d"]}
        for k, col in ids.items():
            v = (o.get(k) or {}).get("v")
            row[col] = pd.to_numeric(v, errors="coerce")
        rows.append(row)
    return pd.DataFrame(rows)


def fetch_yc_de() -> pd.DataFrame:
    """Deutsche Bundesbank: Svensson zero-coupon curve of listed federal securities (2y, 5y, 10y, 30y)."""
    out = None
    for yrs, col in ((2, "2Y"), (5, "5Y"), (10, "10Y"), (30, "30Y")):
        key = f"D.I.ZST.ZI.EUR.S1311.B.A604.R{yrs:02d}XX.R.A.A._Z._Z.A"
        r = requests.get(f"https://api.statistiken.bundesbank.de/rest/data/BBSIS/{key}",
                         params={"startPeriod": _start(), "format": "csv"}, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        recs = []
        for ln in r.text.splitlines():
            p = ln.split(";")
            if len(p) >= 2 and len(p[0]) == 10 and p[0][4] == "-" and p[0][:4].isdigit():
                recs.append((p[0], pd.to_numeric(p[1].replace(",", "."), errors="coerce")))
        d = pd.DataFrame(recs, columns=["date", col])
        out = d if out is None else out.merge(d, on="date", how="outer")
    if out is None or out.empty:
        raise ValueError("Bundesbank returned no data")
    return out


def fetch_yc_jp() -> pd.DataFrame:
    """Japan Ministry of Finance: JGB benchmark yields (2y, 5y, 10y, 30y), full history file plus current month."""
    base = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
    frames = []
    for url in (base + "historical/jgbcme_all.csv", base + "jgbcme.csv"):
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text), skiprows=1, na_values=["-"])
        df = df.rename(columns=str.strip)
        df["date"] = pd.to_datetime(df["Date"], format="%Y/%m/%d", errors="coerce")
        frames.append(df[["date", "2Y", "5Y", "10Y", "30Y"]])
    out = pd.concat(frames).dropna(subset=["date"]).drop_duplicates("date", keep="last")
    out = out[out["date"] >= pd.Timestamp(_start())].copy()
    for c in ("2Y", "5Y", "10Y", "30Y"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def fetch_yc_au() -> pd.DataFrame:
    """Reserve Bank of Australia table F2: interpolated government bond yields (2y, 5y, 10y; no 30y published)."""
    r = requests.get("https://www.rba.gov.au/statistics/tables/csv/f2-data.csv", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    lines = r.text.lstrip("\ufeff").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Series ID")) + 1
    df = pd.read_csv(StringIO("\n".join(lines[start:])), header=None, usecols=range(5), engine="python", on_bad_lines="skip")
    out = pd.DataFrame({"date": pd.to_datetime(df[0], format="%d-%b-%Y", errors="coerce"),
                        "2Y": pd.to_numeric(df[1], errors="coerce"), "5Y": pd.to_numeric(df[3], errors="coerce"),
                        "10Y": pd.to_numeric(df[4], errors="coerce")})
    out["30Y"] = float("nan")
    return out.dropna(subset=["date"])


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

    (DATA / "econ").mkdir(exist_ok=True)
    econ_fail = []
    for code in ECON_CODES + MARKET_CODES:
        try:
            src = FRED_ALIAS.get(code, code)
            df = fetch_fred(src, 2 if code in MARKET_CODES else 12).rename(columns={src: code})
            saved = merge_save(DATA / "econ" / f"{code}.csv", df, "date", 4000)
            record(f"ECON_{code}", True, saved["date"].iloc[-1])
        except Exception as exc:  # noqa: BLE001
            econ_fail.append(code)
            record(f"ECON_{code}", False, error=f"{type(exc).__name__}: {exc}"[:200])

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

    for cc, fn in (("CA", fetch_yc_ca), ("DE", fetch_yc_de), ("JP", fetch_yc_jp), ("AU", fetch_yc_au)):
        try:
            df = fn().dropna(subset=["2Y", "5Y", "10Y", "30Y"], how="all")
            saved = merge_save(DATA / f"YC_{cc}.csv", df, "date", INTL_KEEP)
            record(f"YC_{cc}", True, saved["date"].iloc[-1], note=f"{INTL[cc]} government yields")
        except Exception as exc:  # noqa: BLE001
            record(f"YC_{cc}", False, error=f"{type(exc).__name__}: {exc}"[:200])

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
