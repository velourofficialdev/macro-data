#!/usr/bin/env python3
"""Download the latest market data into data/*.csv.

Sources (currently all free and key-less; keyed APIs are allowed, see CLAUDE.md):
  - FRED  : US Treasury yields + 10Y real yield   -> data/<CODE>.csv
            US macro (inflation, growth, labor)    -> data/econ/<CODE>.csv
  - CFTC  : Gold and USD Index COT, both from the Legacy Futures Only report (Non-Commercial)
            -> data/COT_GOLD_LEGACY.csv, data/COT_USD_INDEX.csv
  - Canada (Bank of Canada), Germany (Bundesbank), Japan (MoF), Australia (RBA), China (ChinaBond, EastMoney fallback),
    India (FBIL par yields), France and Italy (CNBC benchmark yields; no free official daily feed): 2y/5y/10y/30y -> data/YC_<CC>.csv
  - ECB     : euro reference rates (official, daily ~16:00 CET) -> 7 major pairs in data/fx/<PAIR>.csv (crosses via EUR)
            and the US Dollar Index computed with ICE's published DXY formula -> data/DXY.csv
            (fallbacks: Yahoo, then FRED H.10 for pairs / Stooq for DXY). XAUUSD uses the gold futures series
  - Kalshi  : Fed decision market odds (next meetings) + daily history -> data/fed/
  - FX dashboard feed -> data/fx_macro.json: built here from FRED fredgraph.csv (markets, 7 H.10 pairs, econ, 2y/10y) plus
    Google News RSS headlines; the old velourofficialdev/fx-macro-confluence data.json is only a fallback if that build fails
  - Yahoo : Gold futures price (GC=F)            -> data/GOLD.csv   (used for the weekly price change next to Gold COT)

A failed source never wipes existing data: the old file is kept and the error
is written to data/status.json, which the dashboard shows in its footer.
Exit code is 1 only if every source failed.
"""
from __future__ import annotations

import html
import io
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import numpy as np
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
MARKET_CODES = ["SP500", "DCOILWTICO", "VIXCLS", "GVZCLS", "CBBTCUSD"]   # GVZCLS = Cboe Gold ETF Volatility Index (GVZ)   # tiles in the overview (FRED, daily) -> data/econ/<CODE>.csv
LONG_MARKET = {"SP500", "VIXCLS"}
FRED_ALIAS = {"GDIC1": "A261RX1Q020SBEA"}   # FRED has no GDIC1 id; this is Real Gross Domestic Income (chained 2017 $)
FRED_CODES = ["DGS3MO", "DGS2", "DGS5", "DGS10", "DGS20", "DGS30", "DFII10"]
KEEP_DAILY = 2700     # rows kept per daily series (about 10 years; the 252-day window needs only 252)
HISTORY_YEARS = 10
CFTC_LIMIT = 180      # weekly reports fetched (52 window + history)
HEADERS = {"User-Agent": "Mozilla/5.0 (macro-dashboard; personal research)"}
TIMEOUT = 60
FX_FEED_URL = "https://velourofficialdev.github.io/fx-macro-confluence/data.json"

CFTC_SOURCES = {
    # name: (dataset id, contract code, long field, short field, exchange keyword)
    "COT_GOLD_LEGACY": ("6dca-aqww", "088691", "noncomm_positions_long_all",
                        "noncomm_positions_short_all", "COMMODITY EXCHANGE"),
    "COT_USD_INDEX": ("6dca-aqww", "098662", "noncomm_positions_long_all",
                      "noncomm_positions_short_all", "ICE FUTURES"),
}
# CME currency futures, same legacy report (non-commercial = speculators), keyed by the currency they hold
for _cur, _code in (("EUR", "099741"), ("JPY", "097741"), ("GBP", "096742"), ("CHF", "092741"),
                    ("CAD", "090741"), ("AUD", "232741"), ("NZD", "112741")):
    CFTC_SOURCES[f"COT_{_cur}"] = ("6dca-aqww", _code, "noncomm_positions_long_all",
                                   "noncomm_positions_short_all", "CHICAGO MERCANTILE EXCHANGE")

for _k, _code, _kw in (("ES", "13874A", "CHICAGO MERCANTILE EXCHANGE"), ("NQ", "209742", "CHICAGO MERCANTILE EXCHANGE"),
                       ("US30", "124603", "CHICAGO BOARD OF TRADE")):
    CFTC_SOURCES[f"COT_{_k}"] = ("6dca-aqww", _code, "noncomm_positions_long_all", "noncomm_positions_short_all", _kw)

# US index levels (cash indices, so there are no futures roll jumps): symbol -> (Yahoo, FRED fallback)
INDEX_SYMBOLS = {"ES": ("^GSPC", "SP500"), "NQ": ("^NDX", "NASDAQ100"), "US30": ("^DJI", "DJIA")}

# BIS central bank policy rates (daily, official): area code -> label used on the dashboard
POLICY_AREAS = {"US": "US", "XM": "EA", "GB": "GB", "JP": "JP", "CH": "CH", "CA": "CA", "AU": "AU", "NZ": "NZ"}


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
INTL = {"GB": "United Kingdom", "CH": "Switzerland", "NZ": "New Zealand", "CA": "Canada", "DE": "Germany", "FR": "France", "IT": "Italy", "JP": "Japan", "AU": "Australia", "CN": "China", "IN": "India"}
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


BROWSER = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def _cnbc_curve(cc: str) -> pd.DataFrame:
    """France / Italy benchmark yields from CNBC's chart API (daily closes, about 2 years per call).
    No central bank publishes these daily for free. Checks done when this was added (Oct 2026):
      - monthly means vs the official OECD series (FRED IRLTLT01FRM156N / IRLTLT01ITM156N): within ~1 bp (FR), ~4 bp (IT)
      - the same CNBC feed for US 10Y vs FRED DGS10: daily levels within ~1 bp, daily changes correlate 0.96
      - daily bar close = the price at about 17:30 CET (European close), confirmed against CNBC's 5-minute bars"""
    cols = {}
    for t in (2, 5, 10, 30):
        r = requests.get("https://ts-api.cnbc.com/harmony/app/charts/1Y.json", params={"symbol": f"{cc}{t}Y-{cc}"},
                         headers=BROWSER, timeout=TIMEOUT)
        r.raise_for_status()
        bars = (r.json().get("barData") or {}).get("priceBars") or []
        if not bars:   # CNBC has no benchmark for this tenor (e.g. Switzerland 5Y/30Y, New Zealand 2Y/30Y)
            continue
        s = pd.Series([float(b["close"]) for b in bars], index=pd.to_datetime([b["tradeTime"][:8] for b in bars]))
        s = s[~s.index.duplicated(keep="last")]
        cols[f"{t}Y"] = s[s.index.dayofweek < 5]   # weekday bars close at the local close; weekend bars are late Friday trades
    if not cols:
        raise ValueError(f"CNBC has no benchmark yields for {cc}")
    df = pd.DataFrame(cols).dropna(how="all")
    if len(df) < 100:
        raise ValueError(f"CNBC returned only {len(df)} rows for {cc}")
    df = df.reindex(columns=["2Y", "5Y", "10Y", "30Y"])   # tenors CNBC does not publish stay empty
    return df.rename_axis("date").reset_index()


def fetch_yc_gb() -> pd.DataFrame:
    return _cnbc_curve("GB")


def fetch_yc_ch() -> pd.DataFrame:
    return _cnbc_curve("CH")


def fetch_yc_nz() -> pd.DataFrame:
    return _cnbc_curve("NZ")


def fetch_policy_rates() -> pd.DataFrame:
    """BIS central bank policy rates (daily series, official). One column per currency area."""
    r = requests.get("https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D." + "+".join(POLICY_AREAS),
                     params={"startPeriod": "2019-01-01", "format": "csv"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    f = pd.read_csv(io.StringIO(r.text), usecols=["REF_AREA", "TIME_PERIOD", "OBS_VALUE"])
    f["REF_AREA"] = f["REF_AREA"].map(POLICY_AREAS)
    f = f.dropna(subset=["REF_AREA"])
    wide = f.pivot_table(index="TIME_PERIOD", columns="REF_AREA", values="OBS_VALUE", aggfunc="last").sort_index()
    if wide.shape[1] < len(POLICY_AREAS) or len(wide) < 100:
        raise ValueError(f"BIS returned {wide.shape[1]} areas / {len(wide)} rows")
    wide = wide.ffill()   # the series only has a row on the days a rate was set or confirmed
    return wide.rename_axis("date").reset_index()


def fetch_yc_fr() -> pd.DataFrame:
    return _cnbc_curve("FR")


def fetch_yc_it() -> pd.DataFrame:
    return _cnbc_curve("IT")


def fetch_yc_cn() -> pd.DataFrame:
    """ChinaBond government bond yield curve (YTM), the official CGB curve. EastMoney (which republishes it) as fallback."""
    try:
        r = requests.post("https://yield.chinabond.com.cn/cbweb-mn/yc/queryYz",
                          params={"bjlx": "no", "dcq": "2,2y;5,5y;10,10y;30,30y;", "startTime": _start(),
                                  "endTime": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "qxlx": "0,", "yqqxN": "N",
                                  "yqqxK": "K", "par": "day", "ycDefIds": "2c9081e50a2f9606010a3068cae70001,", "locale": "en_US"},
                          headers=BROWSER, timeout=90)
        r.raise_for_status()
        cols = {}
        for s in r.json():
            ten = s["ycDefName"].rsplit("(", 1)[-1].rstrip(")").upper()          # "(10y)" -> "10Y"
            d = s["seriesData"]
            cols[ten] = pd.Series([v for _, v in d], index=pd.to_datetime([ts for ts, _ in d], unit="ms") + pd.Timedelta(hours=8))
        df = pd.DataFrame(cols)
        df.index = df.index.normalize()
        if not {"2Y", "5Y", "10Y", "30Y"} <= set(df.columns) or len(df) < 250:
            raise ValueError("ChinaBond curve incomplete")
        return df.rename_axis("date").reset_index()
    except Exception as exc:  # noqa: BLE001
        print(f"WARN China via ChinaBond failed ({type(exc).__name__}: {exc}); trying EastMoney")
    r = requests.get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                     params={"reportName": "RPTA_WEB_TREASURYYIELD", "columns": "ALL", "pageSize": 60,
                             "sortColumns": "SOLAR_DATE", "sortTypes": -1}, headers=BROWSER, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()["result"]["data"]
    return pd.DataFrame({"date": pd.to_datetime([x["SOLAR_DATE"][:10] for x in rows]),
                         "2Y": [x["EMM00588704"] for x in rows], "5Y": [x["EMM00166462"] for x in rows],
                         "10Y": [x["EMM00166466"] for x in rows], "30Y": [x["EMM00166469"] for x in rows]})


def _fbil_day(day: str) -> dict | None:
    """FBIL G-Sec par yield curve for one day (semi-annual YTM, the market quoting convention). None if not published."""
    r = requests.get("https://www.fbil.org.in/wasdm/gsec/downloadPublished", params={"date": day}, headers=BROWSER, timeout=TIMEOUT)
    if r.status_code != 200 or len(r.content) < 1000:
        return None
    v = pd.read_excel(io.BytesIO(r.content), sheet_name="Par Yield", header=None)
    hdr = v.index[v[0].astype(str).str.contains("Tenor", na=False)][0]
    t = v.iloc[hdr + 1:, :2].dropna()
    t.columns = ["tenor", "ytm"]
    t["tenor"] = pd.to_numeric(t["tenor"], errors="coerce")
    t = t.dropna().set_index("tenor")["ytm"].astype(float)
    out = {"date": day, **{f"{k}Y": t.get(float(k)) for k in (2, 5, 10, 30)}}
    return out if out["10Y"] is not None else None


def fetch_yc_in() -> pd.DataFrame:
    """India: FBIL G-Sec par yields, one published file per business day. Only days after the saved file are fetched
    (initial history was backfilled once). Checked against the OECD monthly 10Y (FRED INDIRLTLT01STM): within about 2 bp."""
    p = DATA / "YC_IN.csv"
    last = pd.to_datetime(pd.read_csv(p)["date"]).max() if p.exists() else pd.Timestamp.today() - pd.Timedelta(days=400)
    days = pd.bdate_range(last + pd.Timedelta(days=1), pd.Timestamp.today().normalize())[-60:]
    rows = [r for d in days if (r := _fbil_day(d.strftime("%Y-%m-%d")))]
    if not rows:
        if p.exists():
            return pd.DataFrame(columns=["date", "2Y", "5Y", "10Y", "30Y"])   # nothing new published yet
        raise ValueError("FBIL returned no par yield files")
    return pd.DataFrame(rows).assign(date=lambda d: pd.to_datetime(d["date"]))


# ---- Fed decision odds from Kalshi's public market data (CME FedWatch has no free API)
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
FED_SERIES = "KXFEDDECISION"
FED_OUTCOMES = ["C26", "C25", "H0", "H25", "H26"]   # cut >25bp, cut 25, hold, hike 25, hike >25
FED_HISTORY_MEETINGS = 4                          # daily odds history kept for the next N meetings
FED_HISTORY_DAYS = 180


def _kalshi(path: str, params: dict | None = None) -> dict:
    import time
    for attempt in range(5):
        r = requests.get(f"{KALSHI}{path}", params=params or {}, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        time.sleep(0.4)
        return r.json()
    raise RuntimeError("Kalshi rate limit")


def _mid(bid, ask, last):
    b, a, l = (float(x) if x not in (None, "") else None for x in (bid, ask, last))
    if b is not None and a is not None and a > 0 and a >= b:
        return (a + b) / 2
    return l


def fetch_fed_odds() -> tuple[dict, dict[str, pd.DataFrame]]:
    markets, cursor = [], None
    while True:
        p = {"series_ticker": FED_SERIES, "status": "open", "limit": 200}
        if cursor:
            p["cursor"] = cursor
        d = _kalshi("/markets", p)
        markets += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor:
            break
    events: dict[str, dict] = {}
    for m in markets:
        code = m["ticker"].split("-")[-1]
        if code not in FED_OUTCOMES:
            continue
        e = events.setdefault(m["event_ticker"], {"event": m["event_ticker"], "date": (m.get("expected_expiration_time") or m["close_time"])[:10], "raw": {}})
        e["raw"][code] = _mid(m.get("yes_bid_dollars"), m.get("yes_ask_dollars"), m.get("last_price_dollars"))
    meetings = sorted((e for e in events.values() if len(e["raw"]) >= 3), key=lambda e: e["date"])
    if not meetings:
        raise ValueError("no open Fed decision markets")
    hist: dict[str, pd.DataFrame] = {}
    end = int(datetime.now(timezone.utc).timestamp())
    start = end - FED_HISTORY_DAYS * 86400
    for e in meetings[:FED_HISTORY_MEETINGS]:
        cols = {}
        for code in FED_OUTCOMES:
            try:
                c = _kalshi(f"/series/{FED_SERIES}/markets/{e['event']}-{code}/candlesticks",
                            {"start_ts": start, "end_ts": end, "period_interval": 1440}).get("candlesticks", [])
            except Exception:  # noqa: BLE001 - one missing outcome should not drop the meeting
                continue
            vals = {}
            for k in c:
                day = datetime.fromtimestamp(k["end_period_ts"], timezone.utc).strftime("%Y-%m-%d")
                v = _mid((k.get("yes_bid") or {}).get("close_dollars"), (k.get("yes_ask") or {}).get("close_dollars"),
                         (k.get("price") or {}).get("close_dollars"))
                if v is not None:
                    vals[day] = v
            cols[code] = pd.Series(vals)
        if cols:
            df = pd.DataFrame(cols).sort_index()
            df.index.name = "date"
            hist[e["event"]] = df.reset_index()
    snap = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "source": "Kalshi KXFEDDECISION",
            "meetings": [{"event": e["event"], "date": e["date"], "raw": e["raw"]} for e in meetings]}
    return snap, hist


# ---- ECB euro foreign exchange reference rates (official, free, no key). Quoted as units of currency per 1 EUR;
# USD pairs are crossed through the euro, which is exact arithmetic on the same 14:15 CET fixing.
ECB_CCY = ("USD", "JPY", "GBP", "CHF", "CAD", "AUD", "NZD", "SEK")
_ECB: dict = {}


def fetch_ecb_rates() -> pd.DataFrame:
    """Daily ECB reference rates, one column per currency (per 1 EUR), indexed by date. Cached per run."""
    if "df" in _ECB:
        return _ECB["df"]
    r = requests.get("https://data-api.ecb.europa.eu/service/data/EXR/D." + "+".join(ECB_CCY) + ".EUR.SP00.A",
                     params={"startPeriod": _start(), "format": "csvdata"}, timeout=60)
    r.raise_for_status()
    raw = pd.read_csv(StringIO(r.text), usecols=["CURRENCY", "TIME_PERIOD", "OBS_VALUE"])
    df = raw.pivot_table(index="TIME_PERIOD", columns="CURRENCY", values="OBS_VALUE", aggfunc="last")
    df.index = pd.to_datetime(df.index)
    df = df.sort_index().dropna(subset=[c for c in ECB_CCY if c in df.columns])
    missing = [c for c in ECB_CCY if c not in df.columns]
    if missing or len(df) < 250:
        raise ValueError(f"ECB returned too little data (missing {missing}, rows {len(df)})")
    _ECB["df"] = df
    return df


def ecb_pairs() -> dict[str, pd.Series]:
    e = fetch_ecb_rates()
    usd = e["USD"]
    return {"EURUSD": usd, "GBPUSD": usd / e["GBP"], "USDJPY": e["JPY"] / usd, "USDCAD": e["CAD"] / usd,
            "AUDUSD": usd / e["AUD"], "USDCHF": e["CHF"] / usd, "NZDUSD": usd / e["NZD"]}


def fetch_dxy_ecb() -> pd.DataFrame:
    """US Dollar Index from ICE's published formula, using ECB reference rates (EUR, JPY, GBP, CAD, SEK, CHF)."""
    e = fetch_ecb_rates()
    usd = e["USD"]
    dxy = (50.14348112 * usd ** -0.576 * (e["JPY"] / usd) ** 0.136 * (usd / e["GBP"]) ** -0.119
           * (e["CAD"] / usd) ** 0.091 * (e["SEK"] / usd) ** 0.042 * (e["CHF"] / usd) ** 0.036)
    return pd.DataFrame({"Date": dxy.index, "DXY": dxy.values}).dropna()


# ---- FX pairs for the confluence panel: ECB reference rates, then Yahoo daily closes, then FRED H.10 (no key needed)
FX_PAIRS = {   # pair: (Yahoo symbol, FRED series)
    "EURUSD": ("EURUSD=X", "DEXUSEU"), "GBPUSD": ("GBPUSD=X", "DEXUSUK"), "USDJPY": ("JPY=X", "DEXJPUS"),
    "USDCAD": ("CAD=X", "DEXCAUS"), "AUDUSD": ("AUDUSD=X", "DEXUSAL"), "USDCHF": ("CHF=X", "DEXSZUS"),
    "NZDUSD": ("NZDUSD=X", "DEXUSNZ"),
}


def fetch_fx_pair(pair: str) -> tuple[pd.DataFrame, str]:
    ysym, fred = FX_PAIRS[pair]
    try:
        s = ecb_pairs()[pair]
        s = s[s.index >= pd.Timestamp.today() - pd.Timedelta(days=3 * 365)]
        if len(s) > 60:
            return pd.DataFrame({"date": s.index, "Close": s.values}), "ECB reference rate"
    except Exception as exc:  # noqa: BLE001 - fall back to Yahoo, then FRED
        print(f"WARN {pair} via ECB failed ({type(exc).__name__}: {exc}); trying Yahoo")
    try:
        df = fetch_yahoo(ysym, "Close").rename(columns={"Date": "date"})
        df = df[df["date"] >= pd.Timestamp.today() - pd.Timedelta(days=3 * 365)]
        if len(df) > 60:
            return df, f"Yahoo {ysym}"
    except Exception:  # noqa: BLE001 - fall back to FRED
        pass
    df = fetch_fred(fred, 3).rename(columns={fred: "Close"})
    return df, f"FRED {fred}"


def fetch_index(sym: str) -> tuple[pd.DataFrame, str]:
    ysym, fred = INDEX_SYMBOLS[sym]
    try:
        df = fetch_yahoo(ysym, "Close").rename(columns={"Date": "date"})
        df = df[df["date"] >= pd.Timestamp.today() - pd.Timedelta(days=3 * 365)]
        if len(df) > 200:
            return df, f"Yahoo {ysym}"
    except Exception as exc:  # noqa: BLE001 - fall back to FRED
        print(f"WARN {sym} via Yahoo failed ({type(exc).__name__}: {exc}); trying FRED")
    df = fetch_fred(fred, 3).rename(columns={fred: "Close"})
    return df, f"FRED {fred}"


# ---- FX dashboard feed built here (replaces the separate fx-macro-engine) --------------------------------------------
# (id, label, FRED series, decimals, thousands separator, unit, suffix)
FXM_MARKETS = [("dxy", "USD BROAD", "DTWEXBGS", 2, False, "", ""), ("sp500", "S&P 500", "SP500", 0, True, "", ""),
               ("gold", "GOLD IDX", "NASDAQQGLDI", 2, True, "", ""), ("wti", "WTI OIL", "DCOILWTICO", 2, False, "", ""),
               ("vix", "VIX", "VIXCLS", 2, False, "", ""), ("btc", "BTC", "CBBTCUSD", 0, True, "", "")]
FXM_PAIRS = [("EURUSD", "DEXUSEU", 5), ("GBPUSD", "DEXUSUK", 5), ("USDJPY", "DEXJPUS", 3), ("USDCAD", "DEXCAUS", 5),
             ("AUDUSD", "DEXUSAL", 5), ("USDCHF", "DEXSZUS", 5), ("NZDUSD", "DEXUSNZ", 5)]
FXM_ECON = [("fed_rate", "FED RATE", "FEDFUNDS", 2, False, "", "%"), ("cpi", "CPI", "CPIAUCSL", 1, False, "", ""),
            ("unemploy", "UNEMPLOY", "UNRATE", 1, False, "", "%"), ("gdp", "GDP", "GDP", 0, True, "B", ""),
            ("pce", "PCE INDEX", "PCEPI", 1, False, "", ""), ("m2", "M2 MONEY", "M2SL", 0, True, "B", ""),
            ("ppi", "PPI", "PPIACO", 1, False, "", ""), ("retail", "RETAIL SALES", "RSAFS", 0, True, "M", ""),
            ("ecb_rate", "ECB RATE", "ECBDFR", 2, False, "", "%"), ("boj_rate", "BOJ CALL RATE", "IRSTCI01JPM156N", 2, False, "", "%"),
            ("boe_rate", "UK 3M RATE", "IR3TIB01GBM156N", 2, False, "", "%"), ("vix_fred", "VIX (FRED)", "VIXCLS", 2, False, "", "")]
FXM_CURVE = [("y2", "2-Yr", "DGS2"), ("y10", "10-Yr", "DGS10")]
NEWS_QUERY = "site:reuters.com when:3d (markets OR economy OR forex OR dollar OR Fed OR ECB OR inflation OR rates)"
NEWS_URL = "https://news.google.com/rss/search?q=" + quote_plus(NEWS_QUERY) + "&hl=en-US&gl=US&ceid=US:en"
NEWS_UA = "Mozilla/5.0 (compatible; FXMacroDashboard/1.0)"


def _r(x: float, dp: int) -> float:
    return float(np.floor(x * 10 ** dp + 0.5) / 10 ** dp)   # half-up like JS toFixed on typical values, never half-even


def fxm_stats(s: pd.Series, dp: int, comma: bool) -> dict:
    """value/display/change/changePct/date/spark from a date-indexed series (newest 400 observations)."""
    s = s.dropna().tail(400)
    if len(s) < 2:
        raise ValueError("fewer than 2 observations")
    cur, prev = float(s.iloc[-1]), float(s.iloc[-2])
    return {"value": cur, "display": f"{cur:,.{dp}f}" if comma else f"{cur:.{dp}f}", "change": _r(cur - prev, 6),
            "changePct": _r((cur - prev) / prev * 100, 3) if prev else None, "date": str(s.index[-1])[:10],
            "spark": [float(v) for v in s.tail(30)]}


def parse_news(xml_text: str, limit: int = 8) -> list[dict]:
    """Google News RSS text -> up to `limit` {title, url, source, publishedAt}; any problem gives an empty list."""
    try:
        root = ET.fromstring(xml_text)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in root.iter("item"):
        title = html.unescape(re.sub(r"<[^>]+>", "", (it.findtext("title") or ""))).replace("\xa0", " ").strip()
        title = re.sub(r"\s+-\s+Reuters$", "", title).strip()
        if not title:
            continue
        url = (it.findtext("link") or "").strip()
        if url.startswith("http://"):
            url = "https://" + url[7:]
        pub = None
        try:
            pub = parsedate_to_datetime((it.findtext("pubDate") or "").strip()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except Exception:  # noqa: BLE001
            pass
        out.append({"title": title, "url": url, "source": "Reuters", "publishedAt": pub})
        if len(out) >= limit:
            break
    return out


def fetch_news() -> list[dict]:
    r = requests.get(NEWS_URL, headers={"User-Agent": NEWS_UA}, timeout=20)
    r.raise_for_status()
    return parse_news(r.text)


def build_fx_macro(prev: dict | None, fred=fetch_fred, news=fetch_news, budget: float = 120.0, clock=time.monotonic) -> tuple[dict, str]:
    """Own build of the dashboard FX feed. Returns (feed, problems). Raises if fewer than half the FRED series load."""
    prev = prev or {}
    cache: dict[str, pd.Series | Exception] = {}
    problems: list[str] = []
    t0 = clock()

    def series(code: str) -> pd.Series:
        if code not in cache:
            try:
                if clock() - t0 > budget:   # overall FRED wait is capped: the rest counts as failed
                    raise TimeoutError(f"FRED time budget {budget:.0f}s used up")
                df = fred(code)
                cache[code] = pd.Series(df[code].values, index=df["date"].astype(str).values)
            except Exception as exc:  # noqa: BLE001
                cache[code] = exc
        v = cache[code]
        if isinstance(v, Exception):
            raise v
        return v

    def item(group: str, key: str, label: str, code: str, dp: int, comma: bool, unit: str, suffix: str) -> dict:
        base = {"label": label, "series": code, "unit": unit, "suffix": suffix}
        try:
            return {**base, **fxm_stats(series(code), dp, comma)}
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{code}: {type(exc).__name__}")
            old = (prev.get(group) or {}).get(key)
            if old and old.get("value") is not None:
                return old
            return {**base, "value": None, "display": "-", "change": None, "changePct": None, "date": "", "spark": [],
                    "error": f"{type(exc).__name__}: {exc}"[:120]}

    feed: dict = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    feed["markets"] = {k: item("markets", k, *a) for k, *a in FXM_MARKETS}
    try:   # confluence reuses the existing port in build_dashboard (one copy of the maths)
        from build_dashboard import fx_confluence
    except Exception as exc:  # noqa: BLE001
        fx_confluence = None
        problems.append(f"confluence import: {type(exc).__name__}")
    fx = {}
    for sym, code, dp in FXM_PAIRS:
        try:
            s = series(code).dropna().tail(400)
            if len(s) < 30:
                raise ValueError("fewer than 30 observations")
            st = fxm_stats(s, dp, True)
            cur, prv = st["value"], float(s.iloc[-2])
            d = {"series": code, "price": cur, "display": f"{cur:.{dp}f}", "change": _r(cur - prv, dp), "changePct": st["changePct"],
                 "date": st["date"], "spark": st["spark"]}
            if fx_confluence:
                d["confluence"] = fx_confluence(sym, s)
            fx[sym] = d
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{code}: {type(exc).__name__}")
            old = (prev.get("fx") or {}).get(sym)
            fx[sym] = old if old and old.get("price") is not None else {"series": code, "price": None, "display": "-", "change": None, "changePct": None, "date": "", "spark": [], "error": f"{type(exc).__name__}: {exc}"[:120]}
    feed["fx"] = fx
    feed["econ"] = {k: item("econ", k, *a) for k, *a in FXM_ECON}
    feed["curve"] = {k: item("curve", k, lab, code, 2, False, "", "") for k, lab, code in FXM_CURVE}
    total = len(FXM_MARKETS) + len(FXM_PAIRS) + len(FXM_ECON) + len(FXM_CURVE)
    ok = sum(1 for g in (feed["markets"], feed["econ"], feed["curve"]) for v in g.values() if v.get("value") is not None) \
        + sum(1 for v in fx.values() if v.get("price") is not None)
    failed_fetch = len([e for e in cache.values() if isinstance(e, Exception)])
    if failed_fetch * 2 > len(cache):
        raise RuntimeError(f"FRED unavailable: {failed_fetch} of {len(cache)} series failed")
    try:
        feed["news"] = news()
        if not feed["news"]:
            problems.append("news: empty")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"news: {type(exc).__name__}")
        feed["news"] = prev.get("news") or []
    return feed, f"{ok}/{total} series ok" + ("; " + ", ".join(problems) if problems else "")


def fx_pick(prev: dict | None, new: dict) -> tuple[dict, bool]:
    """Keep the fresher of the previous stored feed and a fallback feed by 'updated'. Returns (feed, kept_previous)."""
    pu, nu = str((prev or {}).get("updated") or ""), str(new.get("updated") or "")
    if prev and pu and pu >= nu:
        return prev, True
    return new, False


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
            yrs = 10 if code in LONG_MARKET else (2 if code in MARKET_CODES else 12)   # long history feeds the regime fingerprint
            df = fetch_fred(src, yrs).rename(columns={src: code})
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

    for label, fn in (("ECB (ICE formula)", fetch_dxy_ecb), ("Yahoo", fetch_dxy_yahoo), ("Stooq", fetch_dxy_stooq)):
        try:
            df = fn()
            p = DATA / "DXY.csv"
            if p.exists():   # keep one source per file so the history stays consistent
                old = pd.read_csv(p)
                if (str(old["source"].iloc[-1]) if "source" in old else "Yahoo") != label:
                    p.unlink()
            df["source"] = label
            saved = merge_save(p, df, "Date", KEEP_DAILY)
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

    for cc, fn in (("GB", fetch_yc_gb), ("CH", fetch_yc_ch), ("NZ", fetch_yc_nz), ("CA", fetch_yc_ca), ("DE", fetch_yc_de), ("FR", fetch_yc_fr), ("IT", fetch_yc_it), ("JP", fetch_yc_jp),
                   ("AU", fetch_yc_au), ("CN", fetch_yc_cn), ("IN", fetch_yc_in)):
        try:
            df = fn().dropna(subset=["2Y", "5Y", "10Y", "30Y"], how="all")
            saved = merge_save(DATA / f"YC_{cc}.csv", df, "date", INTL_KEEP)
            record(f"YC_{cc}", True, saved["date"].iloc[-1], note=f"{INTL[cc]} government yields")
        except Exception as exc:  # noqa: BLE001
            record(f"YC_{cc}", False, error=f"{type(exc).__name__}: {exc}"[:200])

    try:
        saved = merge_save(DATA / "POLICY_RATES.csv", fetch_policy_rates(), "date", 3000)
        record("POLICY_RATES", True, saved["date"].iloc[-1], note="BIS central bank policy rates")
    except Exception as exc:  # noqa: BLE001
        record("POLICY_RATES", False, error=f"{type(exc).__name__}: {exc}"[:200])

    (DATA / "fx").mkdir(exist_ok=True)
    for pair in FX_PAIRS:
        try:
            df, src = fetch_fx_pair(pair)
            p = DATA / "fx" / f"{pair}.csv"
            if p.exists():   # keep one source per file so the history stays consistent
                old = pd.read_csv(p)
                if "source" in old and str(old["source"].iloc[-1]) != src:
                    p.unlink()
            df["source"] = src
            saved = merge_save(p, df, "date", 800)
            record(f"FX_{pair}", True, saved["date"].iloc[-1], note=src)
        except Exception as exc:  # noqa: BLE001
            record(f"FX_{pair}", False, error=f"{type(exc).__name__}: {exc}"[:200])

    (DATA / "idx").mkdir(exist_ok=True)
    for sym in INDEX_SYMBOLS:
        try:
            df, src = fetch_index(sym)
            p = DATA / "idx" / f"{sym}.csv"
            if p.exists():   # keep one source per file so the history stays consistent
                old = pd.read_csv(p)
                if "source" in old and str(old["source"].iloc[-1]) != src:
                    p.unlink()
            df["source"] = src
            saved = merge_save(p, df, "date", 800)
            record(f"IDX_{sym}", True, saved["date"].iloc[-1], note=src)
        except Exception as exc:  # noqa: BLE001
            record(f"IDX_{sym}", False, error=f"{type(exc).__name__}: {exc}"[:200])

    try:   # Fed Rate Odds from 30-Day Fed Funds futures (ZQ) + EFFR + Fed calendar; see fed_rate_odds.py
        import fed_rate_odds
        zq, zq_warn = fed_rate_odds.update_snapshots()
        fed_rate_odds.fetch_event_calendar()
        for w in zq_warn:
            print("WARN FED_ZQ", w)
        record("FED_ZQ", True, str(zq["snapshot_date"].max()), note="; ".join(zq_warn)[:300] or "ZQ via Yahoo Finance (manual prices in data/fed/zq_manual.csv override)")
    except Exception as exc:  # noqa: BLE001
        record("FED_ZQ", False, error=f"{type(exc).__name__}: {exc}"[:200])

    try:
        snap, hist = fetch_fed_odds()
        (DATA / "fed").mkdir(exist_ok=True)
        (DATA / "fed" / "fed_odds.json").write_text(json.dumps(snap, indent=1))
        for ev, df in hist.items():
            merge_save(DATA / "fed" / f"{ev}.csv", df, "date", 1000)
        # our own daily snapshot of every open meeting, so history keeps building even past the candle window
        rows = [{"date": snap["updated"][:10], "event": m["event"], **{k: m["raw"].get(k) for k in FED_OUTCOMES}} for m in snap["meetings"]]
        p = DATA / "fed" / "snapshots.csv"
        old = pd.read_csv(p) if p.exists() else pd.DataFrame()
        allr = pd.concat([old, pd.DataFrame(rows)], ignore_index=True).drop_duplicates(["date", "event"], keep="last")
        allr.to_csv(p, index=False)
        record("FED_ODDS", True, snap["updated"][:10], note=f"Kalshi, {len(snap['meetings'])} meetings")
    except Exception as exc:  # noqa: BLE001
        record("FED_ODDS", False, error=f"{type(exc).__name__}: {exc}"[:200])

    fxp = DATA / "fx_macro.json"
    try:
        prev_feed = json.loads(fxp.read_text()) if fxp.exists() else None
    except Exception:  # noqa: BLE001
        prev_feed = None
    try:
        feed, probs = build_fx_macro(prev_feed)
        fxp.write_text(json.dumps(feed))
        record("FX_MACRO", True, str(feed["updated"])[:10], note=("source: own FRED build (FRED csv + Google News RSS); " + probs)[:300])
    except Exception as exc:  # noqa: BLE001
        own_err = f"{type(exc).__name__}: {exc}"[:120]
        try:   # fallback only: the old published feed
            r = requests.get(FX_FEED_URL, timeout=30)
            r.raise_for_status()
            feed = r.json()
            if not isinstance(feed, dict) or "fx" not in feed:
                raise ValueError("unexpected feed shape")
            feed, kept = fx_pick(prev_feed, feed)
            if not kept:
                fxp.write_text(json.dumps(feed))
            what = "kept previous data/fx_macro.json (newer than the old feed)" if kept else "fallback: fx-macro-confluence data.json"
            record("FX_MACRO", True, str(feed.get("updated", ""))[:10], note=f"{what} (own build failed: {own_err})"[:300])
        except Exception as exc2:  # noqa: BLE001
            record("FX_MACRO", False, error=f"own build: {own_err}; fallback: {type(exc2).__name__}: {exc2}"[:300])

    try:   # options positioning levels (SPX -> ES, GLD -> gold) from Cboe's free delayed chains; see options_levels.py
        import options_levels
        for key, (ok, last, err, note) in options_levels.update(DATA).items():
            record(f"OPT_{key}", ok, last, error=err, note=note)
    except Exception as exc:  # noqa: BLE001
        record("OPT_SPX", False, error=f"{type(exc).__name__}: {exc}"[:200])

    (DATA / "status.json").write_text(json.dumps(status, indent=2))
    return 0 if any(s["ok"] for s in status.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
