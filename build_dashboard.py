#!/usr/bin/env python3
"""Build docs/index.html from data/*.csv and playbook.json.

    python build_dashboard.py [--data-dir data] [--out docs/index.html] [--today YYYY-MM-DD]

Everything shown is derived from the CSVs; nothing is hard-coded. The meaning
text next to each reading comes from playbook.json, so edit that file (not this
one) to change interpretations or thresholds.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DAILY_WINDOW = 252
COT_WINDOW = 52

YIELD_CODES = ["DGS3MO", "DGS2", "DGS5", "DGS10", "DGS20", "DGS30", "DFII10"]
TENORS = [("3M", "DGS3MO"), ("2Y", "DGS2"), ("5Y", "DGS5"), ("10Y", "DGS10"), ("20Y", "DGS20"), ("30Y", "DGS30")]
SPREADS = {  # name: (long leg, short leg); order = order on the page and in the matrix
    "10y-2y": ("DGS10", "DGS2"),
    "10y-3m": ("DGS10", "DGS3MO"),
    "20y-10y": ("DGS20", "DGS10"),
    "30y-5y": ("DGS30", "DGS5"),
    "30y-10y": ("DGS30", "DGS10"),
    "30y-2y": ("DGS30", "DGS2"),
    "30y-3m": ("DGS30", "DGS3MO"),
    "10y-5y": ("DGS10", "DGS5"),
    "5y-2y": ("DGS5", "DGS2"),
    "5y-3m": ("DGS5", "DGS3MO"),
    "2y-3m": ("DGS2", "DGS3MO"),
}
COUNTRIES = {"US": "United States", "CA": "Canada", "DE": "Germany", "FR": "France", "IT": "Italy", "JP": "Japan",
             "AU": "Australia", "CN": "China", "IN": "India"}
INTL_TENORS = ["2Y", "5Y", "10Y", "30Y"]
INTL_PAIRS = ["30y-10y", "30y-5y", "30y-2y", "10y-5y", "10y-2y", "5y-2y"]
MATRIX_ORDER = ["30y-10y", "30y-5y", "30y-2y", "30y-3m", "20y-10y", "10y-5y", "10y-2y", "10y-3m", "5y-2y", "5y-3m", "2y-3m"]
YIELD_LABELS = {"DGS3MO": "3M", "DGS2": "2Y", "DGS5": "5Y", "DGS10": "10Y", "DGS20": "20Y", "DGS30": "30Y", "DFII10": "10Y real"}
# US macro from FRED. kind: yoy = shown as % change from a year earlier; rate = level in %; k = level in thousands.
ECON = [
    ("CPIAUCSL", "CPI, all items", "Inflation", "yoy", "Consumer prices paid by urban households for a broad basket of goods and services."),
    ("CPILFESL", "Core CPI (ex food and energy)", "Inflation", "yoy", "CPI without the volatile food and energy parts; the cleaner read on underlying inflation."),
    ("PCEPI", "PCE price index", "Inflation", "yoy", "Prices of everything households consume, including what is paid on their behalf. The Fed's 2% target uses this measure."),
    ("PCEPILFE", "Core PCE (ex food and energy)", "Inflation", "yoy", "The Fed's preferred underlying inflation gauge."),
    ("GDPDEF", "GDP implicit price deflator", "Inflation", "yoy", "Price level of everything produced in the US (quarterly)."),
    ("GDP", "GDP, nominal", "Economic growth", "yoy", "Total value of US output in current dollars (quarterly, annualised)."),
    ("GDPC1", "Real GDP", "Economic growth", "yoy", "Inflation-adjusted output (quarterly)."),
    ("GDIC1", "Real GDI", "Economic growth", "yoy", "Inflation-adjusted income earned from production; should match real GDP in theory (quarterly)."),
    ("INDPRO", "Industrial production", "Economic growth", "yoy", "Output of factories, mines and utilities."),
    ("RRSFS", "Real retail and food services sales", "Economic growth", "yoy", "Retail spending adjusted for inflation."),
    ("PCE", "Personal consumption expenditures", "Economic growth", "yoy", "Total household spending in current dollars."),
    ("UNRATE", "Unemployment rate", "Labor force and employment", "rate", "Share of the labour force without a job and looking for one."),
    ("CIVPART", "Labor force participation rate", "Labor force and employment", "rate", "Share of the population aged 16+ that is working or looking for work."),
    ("EMRATIO", "Employment-population ratio", "Labor force and employment", "rate", "Share of the population aged 16+ with a job."),
    ("CLF16OV", "Civilian labor force", "Labor force and employment", "k", "People working or looking for work, thousands."),
    ("CE16OV", "Employment level", "Labor force and employment", "k", "Employed people (household survey), thousands."),
    ("UNEMPLOY", "Unemployment level", "Labor force and employment", "k", "Unemployed people, thousands."),
    ("PAYEMS", "Nonfarm payrolls", "Labor force and employment", "k", "Jobs on business payrolls (establishment survey), thousands. The monthly change is the headline jobs number."),
    ("CES0500000003", "Average hourly earnings", "Labor force and employment", "yoy", "Average hourly pay, private sector; a wage-inflation gauge."),
    ("ICSA", "Initial jobless claims", "Labor force and employment", "k", "New unemployment-insurance claims per week, thousands. Rising claims are an early sign of layoffs."),
    ("CCSA", "Continued claims", "Labor force and employment", "k", "People still claiming unemployment benefits, thousands. Rising = harder to find a new job."),
    ("JTSJOL", "Job openings (JOLTS)", "Labor force and employment", "k", "Unfilled job openings, thousands."),
    ("JTSQUR", "Quits rate (JOLTS)", "Labor force and employment", "rate", "Share of workers quitting each month; high = workers confident they can find a better job."),
]
ECON_FAMILY = {"yoy": "e_yoy", "rate": "e_rate", "k": "e_k"}
COT_FILES = {
    "gold": ["COT_GOLD_LEGACY.csv"],
    "usd_index": ["COT_USD_INDEX.csv"],
}


# ----------------------------------------------------------------- helpers
def num(x):
    """JSON-safe float (NaN/inf -> None)."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(x) or math.isinf(x)) else x


def r(x, nd=3):
    x = num(x)
    return None if x is None else round(x, nd)


def pick_band(bands: list[dict], value: float | None) -> dict | None:
    if value is None:
        return None
    for b in bands:
        if "lt" not in b or value < b["lt"]:
            return b
    return bands[-1] if bands else None


def read_series(path: Path, date_col: str, val_col: str) -> pd.Series | None:
    if not path.exists():
        return None
    df = pd.read_csv(path)
    if date_col not in df or val_col not in df:
        return None
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    s = pd.to_numeric(df[val_col], errors="coerce")
    s.index = df[date_col]
    s = s[s.index.notna()].dropna().sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return s if len(s) else None


def roll_z(s: pd.Series, window: int) -> pd.Series:
    w = s.rolling(window, min_periods=window)
    return (s - w.mean()) / w.std(ddof=1).replace(0, np.nan)


def yield_metrics(s: pd.Series) -> dict:
    z = roll_z(s, DAILY_WINDOW)
    return {
        "value": num(s.iloc[-1]),
        "chg5": num(s.diff(5).iloc[-1]),
        "chg20": num(s.diff(20).iloc[-1]),
        "z": num(z.iloc[-1]),
        "date": s.index[-1].strftime("%Y-%m-%d"),
    }


def classify_dynamics(long_s: pd.Series, short_s: pd.Series, min_move: float) -> str | None:
    both = pd.concat([long_s, short_s], axis=1, keys=["l", "s"]).dropna()
    if len(both) < 21:
        return None
    dl = both["l"].iloc[-1] - both["l"].iloc[-21]
    ds = both["s"].iloc[-1] - both["s"].iloc[-21]
    dspread = dl - ds
    if abs(dspread) < min_move:
        return "stable"
    falling = (dl + ds) < 0   # yields broadly falling -> "bull"
    if dspread > 0:
        return "bull_steepener" if falling else "bear_steepener"
    return "bull_flattener" if falling else "bear_flattener"


def load_cot(data_dir: Path, names: list[str]) -> dict | None:
    for fname in names:
        p = data_dir / fname
        if not p.exists():
            continue
        df = pd.read_csv(p)
        if not {"Date", "Long", "Short"} <= set(df.columns):
            continue
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.dropna(subset=["Date", "Long", "Short"]).sort_values("Date").drop_duplicates("Date", keep="last")
        df = df.set_index("Date")
        net = pd.to_numeric(df["Long"]) - pd.to_numeric(df["Short"])
        w = net.rolling(COT_WINDOW, min_periods=COT_WINDOW)
        z = (net - w.mean()) / w.std(ddof=1).replace(0, np.nan)
        rng = (w.max() - w.min()).replace(0, np.nan)
        idx = 100 * (net - w.min()) / rng
        # The legacy export already carries 52-week stats; use them where we cannot compute our own.
        if "COT_Index" in df:
            idx = idx.combine_first(pd.to_numeric(df["COT_Index"], errors="coerce"))
        if "COT_Zscore" in df:
            z = z.combine_first(pd.to_numeric(df["COT_Zscore"], errors="coerce"))
        longs = pd.to_numeric(df["Long"]); shorts = pd.to_numeric(df["Short"])
        oi = pd.to_numeric(df["Open_Interest"], errors="coerce") if "Open_Interest" in df else pd.Series(np.nan, index=df.index)
        oi = oi.where(oi > 0)
        net_oi = 100 * net / oi                       # speculator net as % of total open interest
        wo = net_oi.rolling(COT_WINDOW, min_periods=COT_WINDOW)
        oi_z = (net_oi - wo.mean()) / wo.std(ddof=1).replace(0, np.nan)
        oi_idx = 100 * (net_oi - wo.min()) / (wo.max() - wo.min()).replace(0, np.nan)   # 0-100 within 52 reports
        return {
            "file": fname,
            "net": net, "z": z, "index": idx,
            "long": longs, "short": shorts, "oi": oi,
            "wchg": net.diff(), "long_chg": longs.diff(), "short_chg": shorts.diff(),
            "oi_chg": oi.diff(), "oi_chg_pct": 100 * oi.pct_change(),
            "net_oi": net_oi, "net_oi_chg": net_oi.diff(), "net_oi_z": oi_z, "net_oi_idx": oi_idx,
            "net_pct": net_oi,
        }
    return None


def friday_close(price: pd.Series | None) -> pd.DataFrame | None:
    """Friday close per week (last trading day of the week) and its % change from the previous Friday."""
    if price is None or len(price) < 10:
        return None
    wk = price.resample("W-FRI").last().dropna()
    last_week_end = wk.index[-1]
    if price.index.max() < last_week_end:      # current week not finished: drop the partial week
        wk = wk.iloc[:-1]
    return pd.DataFrame({"close": wk, "chg_pct": wk.pct_change() * 100})


def attach_price(c: dict | None, price: pd.Series | None) -> None:
    if c is None:
        return
    wk = friday_close(price)
    idx = c["net"].index
    close = pd.Series(np.nan, index=idx); chg = pd.Series(np.nan, index=idx)
    if wk is not None:
        fri = pd.DatetimeIndex([d + pd.Timedelta(days=(4 - d.weekday()) % 7) for d in idx])   # report is Tuesday; same week's Friday
        close = pd.Series(wk["close"].reindex(fri).values, index=idx)
        chg = pd.Series(wk["chg_pct"].reindex(fri).values, index=idx)
    c["price"] = close; c["price_chg"] = chg
    c["friday"] = pd.Series(fri if wk is not None else idx, index=idx)


# ----------------------------------------------------------------- data
def load_fx_drivers_data(data_dir: Path) -> dict:
    """Extra inputs for the FX macro-drivers card: 2Y/5Y yields of the pairs' other currencies, policy rates, currency COT."""
    out: dict = {"yc": {}, "pol": None, "cot": {}}
    for cc in ("DE", "GB", "JP", "CH", "CA", "AU", "NZ"):
        p = data_dir / f"YC_{cc}.csv"
        if p.exists():
            df = pd.read_csv(p, parse_dates=["date"]).set_index("date").sort_index()
            out["yc"][cc] = {t: pd.to_numeric(df[t], errors="coerce").dropna() for t in ("2Y", "5Y") if t in df}
    p = data_dir / "POLICY_RATES.csv"
    if p.exists():
        out["pol"] = pd.read_csv(p, parse_dates=["date"]).set_index("date").sort_index().apply(pd.to_numeric, errors="coerce")
    for cur, fname in (("EUR", "COT_EUR"), ("JPY", "COT_JPY"), ("GBP", "COT_GBP"), ("CHF", "COT_CHF"), ("CAD", "COT_CAD"), ("AUD", "COT_AUD"),
                       ("NZD", "COT_NZD"), ("ES", "COT_ES"), ("NQ", "COT_NQ"), ("US30", "COT_US30"), ("GOLD", "COT_GOLD_LEGACY")):
        p = data_dir / f"{fname}.csv"
        if p.exists():
            df = pd.read_csv(p, parse_dates=["Date"]).dropna(subset=["Long", "Short"]).set_index("Date").sort_index()
            out["cot"][cur] = (pd.to_numeric(df["Long"]) - pd.to_numeric(df["Short"])).astype(float)
    return out


def load_everything(data_dir: Path) -> dict:
    yields = {c: s for c in YIELD_CODES if (s := read_series(data_dir / f"{c}.csv", "date", c)) is not None}

    dxy = read_series(data_dir / "DXY.csv", "Date", "DXY")

    intl = {}
    for cc in COUNTRIES:
        if cc == "US":
            continue
        p = data_dir / f"YC_{cc}.csv"
        if p.exists():
            df = pd.read_csv(p, parse_dates=["date"]).set_index("date").sort_index()
            ser = {t: pd.to_numeric(df[t], errors="coerce").dropna() for t in INTL_TENORS if t in df}
            intl[cc] = {t: v for t, v in ser.items() if len(v) > 30}

    cot = {k: load_cot(data_dir, v) for k, v in COT_FILES.items()}
    gold_px = read_series(data_dir / "GOLD.csv", "Date", "Gold")
    attach_price(cot.get("gold"), gold_px)
    attach_price(cot.get("usd_index"), dxy)
    status = {}
    sp = data_dir / "status.json"
    if sp.exists():
        try:
            status = json.loads(sp.read_text())
        except json.JSONDecodeError:
            status = {}
    econ = {}
    for code, *_ in ECON:
        es = read_series(data_dir / "econ" / f"{code}.csv", "date", code)
        if es is not None and len(es) > 8:
            econ[code] = es
    fed = None
    fp = data_dir / "fed" / "fed_odds.json"
    if fp.exists():
        try:
            fed = json.loads(fp.read_text())
            fed["hist"] = {}
            for m in fed["meetings"]:
                hp = data_dir / "fed" / f"{m['event']}.csv"
                if hp.exists():
                    fed["hist"][m["event"]] = pd.read_csv(hp, parse_dates=["date"]).set_index("date").sort_index()
        except (json.JSONDecodeError, KeyError):
            fed = None
    fxp = {}
    for pair in ("EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "USDCHF", "NZDUSD"):
        x = read_series(data_dir / "fx" / f"{pair}.csv", "date", "Close")
        if x is not None and len(x) > 60:
            fxp[pair] = x
    idx = {k: x for k in ("ES", "NQ", "US30") if (x := read_series(data_dir / "idx" / f"{k}.csv", "date", "Close")) is not None and len(x) > 60}
    mkt = {c: x for c in ("SP500", "DCOILWTICO", "VIXCLS", "GVZCLS", "CBBTCUSD") if (x := read_series(data_dir / "econ" / f"{c}.csv", "date", c)) is not None}
    zq = pd.read_csv(data_dir / "fed" / "zq_odds.csv") if (data_dir / "fed" / "zq_odds.csv").exists() else None
    events = pd.read_csv(data_dir / "fed" / "events.csv") if (data_dir / "fed" / "events.csv").exists() else None
    drv = load_fx_drivers_data(data_dir)
    return {"idx": idx, "drv": drv, "zq": zq, "events": events, "fxp": fxp, "fed": fed, "mkt": mkt, "econ": econ, "intl": intl, "yields": yields, "dxy": dxy, "gold_px": gold_px, "cot": cot, "status": status, "options": load_options(data_dir)}


def compute(data: dict, playbook: dict) -> dict:
    """Returns {'metrics': {key: {field: value}}, 'spreads': {...}, ...}."""
    metrics: dict[str, dict] = {}
    spreads: dict[str, dict] = {}
    spread_series: dict[str, pd.Series] = {}
    min_move = playbook.get("dynamics", {}).get("min_move_pp", 0.05)

    for code, s in data["yields"].items():
        metrics[f"Y:{code}"] = yield_metrics(s)

    for name, (lg, sh) in SPREADS.items():
        if lg not in data["yields"] or sh not in data["yields"]:
            continue
        s = (data["yields"][lg] - data["yields"][sh]).dropna()
        if len(s) < 21:
            continue
        m = yield_metrics(s)
        m["dynamics"] = classify_dynamics(data["yields"][lg], data["yields"][sh], min_move)
        metrics[f"S:{name}"] = m
        spread_series[name] = s
        spreads[name] = m

    for cc, ser in data.get("intl", {}).items():
        for name in INTL_PAIRS:
            lg, sh = [x.upper() for x in name.split("-")]
            if lg not in ser or sh not in ser:
                continue
            s = (ser[lg] - ser[sh]).dropna()
            if len(s) < 252:
                continue
            m = yield_metrics(s)
            m["dynamics"] = classify_dynamics(ser[lg], ser[sh], min_move)
            metrics[f"S:{cc}:{name}"] = m
            spread_series[f"{cc}:{name}"] = s

    dxy = data["dxy"]
    if dxy is not None and len(dxy) > 21:
        z = roll_z(dxy, DAILY_WINDOW)
        metrics["DXY"] = {
            "value": num(dxy.iloc[-1]), "z": num(z.iloc[-1]),
            "pct5": num(dxy.pct_change(5).iloc[-1] * 100),
            "pct20": num(dxy.pct_change(20).iloc[-1] * 100),
            "date": dxy.index[-1].strftime("%Y-%m-%d"),
        }

    for key, c in data["cot"].items():
        if c is None:
            continue
        metrics[f"COT:{key}"] = {
            "net": num(c["net"].iloc[-1]), "wchg": num(c["wchg"].iloc[-1]),
            "z": num(c["z"].iloc[-1]), "index": num(c["index"].iloc[-1]),
            "net_pct": num(c["net_pct"].iloc[-1]),
            "long_chg": num(c["long_chg"].iloc[-1]), "short_chg": num(c["short_chg"].iloc[-1]),
            "oi": num(c["oi"].iloc[-1]), "oi_chg": num(c["oi_chg"].iloc[-1]), "oi_chg_pct": num(c["oi_chg_pct"].iloc[-1]),
            "net_oi_chg": num(c["net_oi_chg"].iloc[-1]), "net_oi_idx": num(c["net_oi_idx"].iloc[-1]),
            "net_oi_z": num(c["net_oi_z"].iloc[-1]),
            "price": num(c["price"].iloc[-1]), "price_chg": num(c["price_chg"].iloc[-1]),
            "date": c["net"].index[-1].strftime("%Y-%m-%d"),
        }
    return {"metrics": metrics, "spread_series": spread_series}


OPS = {">": lambda a, b: a > b, "<": lambda a, b: a < b, ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b}


def eval_cross_signals(playbook: dict, metrics: dict) -> list[dict]:
    fired = []
    for sig in playbook.get("cross_signals", []):
        ok = True
        for cond in sig["when"]:
            v = (metrics.get(cond["metric"]) or {}).get(cond["field"])
            if v is None or not OPS[cond["op"]](v, cond["value"]):
                ok = False
                break
        if ok:
            fired.append(sig)
    return fired


# ----------------------------------------------------------------- html
def fmt(v, nd=2, sign=False):
    if v is None:
        return "n/a"
    if round(v, nd) == 0:
        v = 0.0   # avoid "-0.00"
    return f"{v:+,.{nd}f}" if sign else f"{v:,.{nd}f}"


def spread_title(name: str) -> str:
    lg, sh = name.upper().split("-")
    return f"{lg} vs {sh}"


def esc(s):
    return html.escape(str(s))


def badge(text, tone="neutral"):
    return f'<span class="badge {tone}">{esc(text)}</span>'


def tone_for(label: str) -> str:
    l = label.lower()
    if "inverted" in l or "extreme" in l or l.startswith("very") or "crowded" in l:
        return "warn"
    return "neutral"


def gauge(index):
    """0-100 bar with a marker; 0 = most short in 52 weeks, 100 = most long."""
    if index is None:
        return ""
    pos = max(0.0, min(100.0, float(index)))
    return ('<div class="gauge" role="img" aria-label="COT index %.0f out of 100"><i style="left:%.1f%%"></i></div>'
            '<div class="gaugelabels"><span>Most short</span><span>Most long</span></div>') % (pos, pos)


def card(title, big, sub, band, extra_rows, what, dyn=None, gauge_html=""):
    rows = "".join(f"<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>" for k, v in extra_rows)
    band_html = ""
    if band:
        band_html = (
            f'<div class="reading">{badge(band["label"], tone_for(band["label"]))}</div>'
            f'<p class="meaning">{esc(band.get("meaning", ""))}</p>'
            f'<p class="sowhat"><strong>So what:</strong> {esc(band.get("so_what", ""))}</p>'
        )
    dyn_html = ""
    if dyn:
        dyn_html = (
            f'<div class="dyn"><span class="dynlabel">Last 20 days:</span> {badge(dyn["label"])}'
            f'<p class="meaning">{esc(dyn["meaning"])}</p>'
            f'<p class="sowhat"><strong>So what:</strong> {esc(dyn["so_what"])}</p></div>'
        )
    return (
        f'<article class="cotinner"><h3 class="sr-only">{esc(title)}</h3>'
        f'<div class="big">{esc(big)}<span class="sub">{esc(sub)}</span></div>'
        f"{gauge_html}<dl>{rows}</dl>{band_html}{dyn_html}"
        f'<details><summary>What is this?</summary><p>{esc(what)}</p></details></article>'
    )


def spread_spec(playbook: dict, name: str) -> dict:
    spec = playbook["spreads"].get(name)
    if spec:
        return spec
    d = playbook["spread_default"]
    return {"title": spread_title(name).replace(" vs ", " - "), "what": d["what"], "bands": d["bands"]}


def band_info(band: dict | None) -> dict:
    if not band:
        return {"label": "", "meaning": "", "so_what": ""}
    return {"label": band["label"], "meaning": band.get("meaning", ""), "so_what": band.get("so_what", "")}


def delta(s: pd.Series, n: int, pct: bool = False):
    if len(s) <= n:
        return None
    return num((s.iloc[-1] / s.iloc[-1 - n] - 1) * 100) if pct else num(s.iloc[-1] - s.iloc[-1 - n])


INTL_SOURCE = {"CA": "Bank of Canada", "DE": "Deutsche Bundesbank (Svensson curve)", "FR": "CNBC benchmark yield, European close",
               "IT": "CNBC benchmark yield, European close", "JP": "Japan Ministry of Finance", "AU": "Reserve Bank of Australia",
               "CN": "ChinaBond government bond yield curve", "IN": "FBIL G-Sec par yield (semi-annual)"}


def yield_row(rid: str, cc: str, ten: str, s: pd.Series, z, band, what: str, title: str, date_: str) -> dict:
    """Watchlist row for one yield: level in %, changes in bp (same scale as the spreads)."""
    bp = lambda n: None if delta(s, n) is None else delta(s, n) * 100
    label = f"{COUNTRIES[cc]} {ten} yield"
    return {"id": rid, "group": COUNTRIES[cc], "family": "yields", "label": label, "ticker": f"{cc}{ten.replace(' real', 'R').upper()}",
            "latest": fmt(s.iloc[-1], 3) + "%", "d1": fmt(bp(1), 1, True), "d5": fmt(bp(5), 1, True),
            "d20": fmt(bp(21), 1, True), "d252": fmt(bp(252), 1, True), "z": fmt(z, 2, True), "date": date_,
            "info": {"title": title, "what": what, "band": band_info(band), "dyn": None, "z": z}}


def build_watch(playbook: dict, data: dict, computed: dict) -> tuple[list[dict], dict]:
    """Rows for the watchlist (left panel) and the daily series for the chart (right panel)."""
    metrics = computed["metrics"]
    rows: list[dict] = []
    fam: dict[str, dict] = {}

    def family(name: str, series: dict[str, pd.Series], dec: int):
        if not series:
            return
        frame = pd.concat(series, axis=1).sort_index()
        fam[name] = {"dates": [d.strftime("%Y-%m-%d") for d in frame.index],
                     "series": {k: [r(v, dec) for v in frame[k]] for k in frame.columns}}

    # individual yields, in each country's group ahead of its spreads (one "yields" family, so any yields can be compared)
    ys = {}
    for code in YIELD_CODES:
        s = data["yields"].get(code)
        if s is None:
            continue
        m = metrics[f"Y:{code}"]
        if code == "DFII10":
            band = pick_band(playbook["real_yield"]["bands"], m["z"]); what = playbook["real_yield"].get("what", "")
        else:
            band = pick_band(playbook["zscore"]["bands"], m["z"]); what = "US Treasury constant-maturity yield from FRED."
        rows.append(yield_row(f"Y:{code}", "US", YIELD_LABELS[code], s, m["z"], band, what,
                              YIELD_LABELS[code] + (" yield" if code != "DFII10" else " Treasury yield (TIPS)"), m["date"]))
        ys[f"Y:{code}"] = s
    for cc, ser in data.get("intl", {}).items():
        for ten in INTL_TENORS:
            s = ser.get(ten)
            if s is None or len(s) < 30:
                continue
            z = num(roll_z(s, DAILY_WINDOW).iloc[-1])
            rows.append(yield_row(f"Y:{cc}:{ten}", cc, ten, s, z, pick_band(playbook["zscore"]["bands"], z),
                                  f"{COUNTRIES[cc]} {ten} government bond yield. Source: {INTL_SOURCE.get(cc, '')}.",
                                  f"{COUNTRIES[cc]} {ten} yield", s.index[-1].strftime("%Y-%m-%d")))
            ys[f"Y:{cc}:{ten}"] = s
    family("yields", ys, 3)

    # spreads, grouped by country (US first); every country's spreads can be overlaid on one chart
    ss = computed["spread_series"]
    order = [(cc, n) for cc in COUNTRIES for n in (list(SPREADS) if cc == "US" else INTL_PAIRS)]
    for cc, name in order:
        key = name if cc == "US" else f"{cc}:{name}"
        s = ss.get(key)
        if s is None:
            continue
        m = metrics[f"S:{key}"]
        spec = spread_spec(playbook, name)
        dyn = playbook["dynamics"]["types"].get(m.get("dynamics") or "")
        lg, sh = name.upper().split("-")
        title = f"{COUNTRIES[cc]} {lg} vs {sh}"
        bp = lambda n: None if delta(s, n) is None else delta(s, n) * 100
        rows.append({"id": f"S:{key}", "group": COUNTRIES[cc], "family": "spreads", "label": title, "ticker": f"{cc}YC{lg}{sh}",
                     "latest": fmt(s.iloc[-1], 3) + "%", "d1": fmt(bp(1), 1, True), "d5": fmt(bp(5), 1, True),
                     "d20": fmt(bp(21), 1, True), "d252": fmt(bp(252), 1, True), "z": fmt(m["z"], 2, True), "date": m["date"],
                     "info": {"title": title + " spread", "what": spec["what"], "band": band_info(pick_band(spec["bands"], m["value"])),
                              "dyn": band_info(dyn) if dyn else None, "z": m["z"]}})
    family("spreads", {f"S:{k}": v for k, v in ss.items()}, 3)

    # dollar and gold
    for key, series, label, dec, spec in (("dxy", data["dxy"], "DXY", 2, playbook["dxy"]), ("gold", data.get("gold_px"), "Gold futures", 2, None)):
        if series is None or len(series) < 30:
            continue
        z = roll_z(series, DAILY_WINDOW).iloc[-1]
        band = pick_band((spec or playbook["zscore"])["bands"], num(z))
        rows.append({"id": f"P:{key}", "group": "Dollar and gold", "family": key, "label": label, "ticker": {"dxy": "DXY", "gold": "GC1"}[key],
                     "latest": fmt(series.iloc[-1], dec), "d1": fmt(delta(series, 1, True), 2, True) + "%",
                     "d5": fmt(delta(series, 5, True), 2, True) + "%", "d20": fmt(delta(series, 20, True), 2, True) + "%",
                     "z": fmt(num(z), 2, True), "date": series.index[-1].strftime("%Y-%m-%d"),
                     "info": {"title": label, "what": (spec or {}).get("what", "COMEX gold futures (GC=F, continuous front month) from Yahoo Finance."),
                              "band": band_info(band), "dyn": None, "z": num(z)}})
        family(key, {f"P:{key}": series}, dec)

    # positioning rows (link to the Positioning section)
    for key, title in (("gold", "Gold COT"), ("usd_index", "USD Index COT")):
        m = metrics.get(f"COT:{key}")
        if not m:
            continue
        spec = playbook["cot"][key]
        rows.append({"id": f"C:{key}", "group": "Positioning", "family": None, "label": title, "ticker": {"gold": "COT-GC", "usd_index": "COT-DX"}[key],
                     "latest": fmt(m["net"], 0, True), "d1": "", "d5": "wk " + fmt(m["wchg"], 0, True),
                     "d20": "idx " + fmt(m["index"], 0), "z": fmt(m["z"], 2, True), "date": m["date"],
                     "info": {"title": spec["title"], "what": spec["what"], "band": band_info(pick_band(spec["bands"], m["index"])),
                              "dyn": None, "z": m["z"]}})
    return rows, fam


def build_watch_html(rows: list[dict]) -> str:
    """Left panel of the Rates block: each country's yields, then its spreads (same layout as the reference terminal)."""
    rows = [r_ for r_ in rows if r_["family"] in ("yields", "spreads")]
    cols = ('<th class="cb"><span class="sr-only">Select</span></th><th>Name</th><th class="c-tk">Ticker</th><th>Level <small>% / pp</small></th>'
            '<th>1D <small>bp</small></th><th class="c-d5">1W <small>bp</small></th><th class="c-d20">1M <small>bp</small></th>'
            '<th class="c-d252">1Y <small>bp</small></th><th class="c-z">Z</th>')
    out = [f'<table class="watch" id="watch"><thead><tr>{cols}</tr></thead>']
    groups: list[str] = []
    for r_ in rows:
        if r_["group"] not in groups:
            groups.append(r_["group"])
    for g in groups:
        members = [r_ for r_ in rows if r_["group"] == g]
        out.append(f'<tbody data-group="{esc(g)}"><tr class="grp"><td class="cb"><span class="gbox" role="checkbox" aria-checked="false" tabindex="0" aria-label="Plot {esc(g)} yields or spreads"></span></td>'
                   f'<th colspan="7"><button type="button" class="gt" aria-expanded="true"><span class="car"></span>{esc(g)}</button></th>'
                   f'<td class="gmove"><button type="button" class="mv up" aria-label="Move {esc(g)} up">&#9650;</button><button type="button" class="mv dn" aria-label="Move {esc(g)} down">&#9660;</button></td></tr>')
        for r_ in members:
            out.append(
                f'<tr class="row{" yl" if r_["family"] == "yields" else ""}" tabindex="0" data-id="{esc(r_["id"])}" data-family="{r_["family"]}">'
                f'<td class="cb"><span class="pick" aria-hidden="true"></span></td>'
                f'<th scope="row"><span class="plus">+</span>{esc(r_["label"])}</th><td class="c-tk tk"><span class="plus">+</span>{esc(r_["ticker"])}</td>'
                f'<td class="yd">{esc(r_["latest"][:-1])}<small>{"%" if r_["family"] == "yields" else "pp"}</small></td><td class="{chg_class(r_["d1"])}{hm(r_["d1"], 10)}">{esc(r_["d1"])}</td>'
                f'<td class="c-d5 {chg_class(r_["d5"])}{hm(r_["d5"], 20)}">{esc(r_["d5"])}</td><td class="c-d20 {chg_class(r_["d20"])}{hm(r_["d20"], 30)}">{esc(r_["d20"])}</td>'
                f'<td class="c-d252 {chg_class(r_["d252"])}{hm(r_["d252"], 80)}">{esc(r_["d252"])}</td><td class="c-z{hm(r_["z"], 2.5)}">{esc(r_["z"])}</td></tr>')
        out.append("</tbody>")
    out.append("</table>")
    return "".join(out)


def hm(text: str, scale: float) -> str:
    """Soft heatmap tint for a signed number cell: class + intensity (0-100) as a CSS variable."""
    try:
        v = float(str(text).replace("+", "").replace(",", "").strip())
    except ValueError:
        return ""
    if v == 0:
        return ""
    return f' hm {"up" if v > 0 else "down"}" style="--i:{min(100, round(abs(v) / scale * 100))}'


def chg_class(text: str) -> str:
    t = str(text).strip()
    if t.startswith("+") or t.startswith("wk +"):
        return "up"
    if t.startswith("-") or t.startswith("wk -"):
        return "down"
    return ""


HORIZONS = [("Now", 0), ("1D ago", 1), ("1W ago", 5), ("1M ago", 21), ("3M ago", 63), ("6M ago", 126), ("1Y ago", 252)]


def matrix_payload(computed: dict, data: dict | None = None) -> dict:
    """Spread levels in bp for every country x pair x horizon (the JS draws and re-draws the heat map)."""
    ss = computed["spread_series"]
    cols = [cc for cc in COUNTRIES if any((n if cc == "US" else f"{cc}:{n}") in ss for n in MATRIX_ORDER)]
    vals = []
    for _, n in HORIZONS:
        grid = []
        for name in MATRIX_ORDER:
            row = []
            for cc in cols:
                s_ = ss.get(name if cc == "US" else f"{cc}:{name}")
                row.append(num(round(s_.iloc[-1 - n] * 100, 1)) if s_ is not None and len(s_) > n else None)
            grid.append(row)
        vals.append(grid)
    out = {"cols": cols, "rows": [" vs ".join(x.upper() for x in nm.split("-")) for nm in MATRIX_ORDER],
           "horizons": [h for h, _ in HORIZONS], "vals": vals}
    if data is not None:
        out["y"] = yields_matrix(data, cols)
    return out


MATRIX_YIELDS = [("3M", "DGS3MO"), ("2Y", "DGS2"), ("5Y", "DGS5"), ("10Y", "DGS10"), ("20Y", "DGS20"), ("30Y", "DGS30"), ("10Y real", "DFII10")]


def yields_matrix(data: dict, cols: list[str]) -> dict:
    """Yield levels (%) for every country x tenor x horizon, plus the change from that date to today in bp
    (for "Now", the 1-day change), which the JS uses for the shading."""
    def series(cc: str, ten: str, code: str):
        if cc == "US":
            return data["yields"].get(code)
        return (data.get("intl", {}).get(cc) or {}).get(ten)
    lv, ch = [], []
    for _, n in HORIZONS:
        g_lv, g_ch = [], []
        for ten, code in MATRIX_YIELDS:
            r_lv, r_ch = [], []
            for cc in cols:
                s_ = series(cc, ten, code)
                if s_ is None or len(s_) <= max(n, 1):
                    r_lv.append(None); r_ch.append(None); continue
                then = s_.iloc[-1 - n]
                r_lv.append(num(round(then, 3)))
                r_ch.append(num(round((s_.iloc[-1] - (s_.iloc[-2] if n == 0 else then)) * 100, 1)))
            g_lv.append(r_lv); g_ch.append(r_ch)
        lv.append(g_lv); ch.append(g_ch)
    return {"rows": [t for t, _ in MATRIX_YIELDS], "vals": lv, "chg": ch}


def build_matrix(data: dict, computed: dict) -> str:
    """US spreads in basis points across time horizons, with heat shading."""
    ss = computed["spread_series"]
    table = []
    for name in MATRIX_ORDER:
        s = ss.get(name)
        if s is None:
            continue
        vals = [num(s.iloc[-1 - n] * 100) if len(s) > n else None for _, n in HORIZONS]
        table.append((name, vals, computed["metrics"][f"S:{name}"]["z"]))
    if not table:
        return ""
    peak = max((abs(v) for _, vals, _ in table for v in vals if v is not None), default=1) or 1
    head = "".join(f"<th>{h}</th>" for h, _ in HORIZONS)
    body = []
    for name, vals, z in table:
        cells = ""
        for v in vals:
            if v is None:
                cells += '<td class="na">-</td>'
                continue
            i = min(100, round(100 * (abs(v) / peak) ** 0.7))
            cells += f'<td class="heat {"neg" if v < 0 else "pos"}" style="--i:{i}">{v:,.1f}</td>'
        body.append(f'<tr><th scope="row">{esc(spread_title(name))}</th>{cells}<td class="zc">{esc(fmt(z, 2, True))}</td></tr>')
    spreads_tbl = ('<table class="matrix" data-kind="spreads"><thead><tr><th>US spread (bp)</th>' + head + '<th>Z (1y)</th></tr></thead><tbody>'
                   + "".join(body) + "</tbody></table>")
    yrows = []
    for ten, code in MATRIX_YIELDS:
        s = data["yields"].get(code)
        if s is None:
            continue
        vals = [num(s.iloc[-1 - n]) if len(s) > n else None for _, n in HORIZONS]
        chg = [None if v is None else (s.iloc[-1] - (s.iloc[-2] if n == 0 else v)) * 100 for v, (_, n) in zip(vals, HORIZONS)]
        yrows.append((ten, vals, chg, computed["metrics"].get(f"Y:{code}", {}).get("z")))
    ypeak = [max((abs(cs[k]) for _, _, cs, _ in yrows if cs[k] is not None), default=1) or 1 for k in range(len(HORIZONS))]   # per column
    ybody = []
    for ten, vals, chg, z in yrows:
        cells = ""
        for k, ((h, n), v, c) in enumerate(zip(HORIZONS, vals, chg)):
            if v is None:
                cells += '<td class="na">-</td>'
                continue
            i = min(100, round(100 * (abs(c) / ypeak[k]) ** 0.7))
            tip = f"1-day change: {c:+.1f} bp" if n == 0 else f"Change from {h} to now: {c:+.1f} bp"
            cells += f'<td class="heat {"neg" if c < 0 else "pos"}" style="--i:{i}" title="{esc(tip)}">{v:.2f}</td>'
        ybody.append(f'<tr><th scope="row">{esc(ten)} yield</th>{cells}<td class="zc">{esc(fmt(z, 2, True))}</td></tr>')
    yields_tbl = ('<table class="matrix" data-kind="yields" hidden><thead><tr><th>US yield (%)</th>' + head + '<th>Z (1y)</th></tr></thead><tbody>'
                  + "".join(ybody) + "</tbody></table>")
    toggle = ('<div class="ranges kindsw" role="group" aria-label="Show yields or spreads">'
              '<button type="button" data-kind="yields" aria-pressed="false">Yields</button>'
              '<button type="button" data-kind="spreads" aria-pressed="true">Spreads</button></div>')
    note = ('<p class="mxnote" data-kind="yields" hidden>Shading shows the move from that date to today (for Now, the last day): '
            'blue = higher now, red = lower now. Hover a cell for the change in bp.</p>')
    return toggle + '<div class="scroll">' + spreads_tbl + yields_tbl + "</div>" + note


# ----------------------------------------------------------------- FX Macro Confluence (merged from the user's own dashboard)
def spark(vals: list, up: bool | None) -> str:
    v = [x for x in (vals or []) if isinstance(x, (int, float))]
    if len(v) < 2:
        return ""
    lo, hi = min(v), max(v)
    span = (hi - lo) or 1
    pts = " ".join(f"{i * 100 / (len(v) - 1):.1f},{26 - (x - lo) * 24 / span:.1f}" for i, x in enumerate(v))
    cls = "flat" if up is None else ("up" if up else "down")
    return f'<svg class="spark {cls}" viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true"><polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>'


def signed(v, nd=2, suffix=""):
    return "n/a" if v is None else f"{fmt(v, nd, True)}{suffix}"


def tile(label, display, chg_pct, vals, sub=""):  # sub = as-of date
    up = None if chg_pct is None else chg_pct >= 0
    cls = "" if chg_pct is None else ("up" if chg_pct >= 0 else "down")
    return (f'<div class="tile"><div class="tl">{esc(label)}</div><div class="tv">{esc(display)}</div>'
            f'<div class="tc {cls}">{esc(signed(chg_pct, 2, "%"))}{(" · " + esc(sub)) if sub else ""}</div>{spark(vals, up)}</div>')


def gauge_ring(score: float, cls: str) -> str:
    rad = 24; c = 2 * math.pi * rad; off = c * (1 - max(0, min(100, score)) / 100)
    return (f'<div class="ring {cls}"><svg width="60" height="60" viewBox="0 0 56 56" aria-hidden="true"><circle cx="28" cy="28" r="{rad}" class="rt"/>'
            f'<circle cx="28" cy="28" r="{rad}" class="rf" stroke-dasharray="{c:.1f}" stroke-dashoffset="{off:.1f}"/></svg>'
            f'<div class="rn"><b>{score:.0f}</b><small>/100</small></div></div>')


FX_DEC = {"USDJPY": 3, "XAUUSD": 2, "ES": 2, "NQ": 2, "US30": 2}


# FX confluence: a line-for-line port of the original engine (velourofficialdev/fx-macro-engine, formerly deiruuuu, fetch-data.js),
# so scores match the old fx-macro-confluence page. That engine pulled the latest 400 daily FRED closes per pair.
FX_OBS = 400


def ema_seeded(a: list[float], n: int) -> float | None:
    """EMA seeded with the simple average of the first n values (as in fetch-data.js)."""
    if len(a) < n:
        return None
    k = 2 / (n + 1)
    e = sum(a[:n]) / n
    for v in a[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(a: list[float], n: int = 14) -> float | None:
    """Wilder RSI seeded with the simple average gain/loss of the first n changes."""
    if len(a) < n + 1:
        return None
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = a[i] - a[i - 1]
        if d >= 0:
            gain += d
        else:
            loss -= d
    ag, al = gain / n, loss / n
    for i in range(n + 1, len(a)):
        d = a[i] - a[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def zscore_last(a: list[float], w: int = 200) -> float | None:
    """Z-score of the last close vs the last w closes (population SD); None with fewer than 20 values."""
    s = a[-w:]
    if len(s) < 20:
        return None
    mean = sum(s) / len(s)
    sd = math.sqrt(sum((x - mean) ** 2 for x in s) / len(s))
    if sd == 0:
        return None
    return (a[-1] - mean) / sd


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def sr_levels(a: list[float], current: float, dec: int) -> list[dict]:
    """120-day high and low (Strong) plus moderate levels 40% of the way from price to each."""
    win = a[-120:]
    hi, lo = max(win), min(win)
    rows = [(hi, "Strong"), (current + (hi - current) * 0.4, "Moderate"),
            (current - (current - lo) * 0.4, "Moderate"), (lo, "Strong")]
    return [{"level": f"{lvl:,.{dec}f}", "strength": st, "distPct": round((lvl - current) / current * 100, 2)} for lvl, st in rows]


def fx_confluence(sym: str, s: pd.Series) -> dict:
    """Score starts at 50: +-12.5 day-on-day momentum, + clamp((RSI14-50)*0.6, +-25), +-15 price vs EMA(50),
    + clamp(z*10, +-20) for the 200-day Z-score; clamped 0-100. 60+ bullish, 40 or less bearish."""
    a = [float(v) for v in s.dropna().tail(FX_OBS)]
    current = a[-1]
    prev = a[-2] if len(a) > 1 else current
    day_pct = (current - prev) / prev * 100 if prev else 0.0
    r, e, z = rsi(a, 14), ema_seeded(a, 50), zscore_last(a, 200)
    above = e is not None and current >= e
    score = 50.0
    score += 12.5 if day_pct > 0 else -12.5
    if r is not None:
        score += clamp((r - 50) * 0.6, -25, 25)
    score += 15 if above else -15
    if z is not None:
        score += clamp(z * 10, -20, 20)
    score = math.floor(clamp(score, 0, 100) + 0.5)          # JS Math.round
    label = "Bullish confluence" if score >= 60 else "Bearish confluence" if score <= 40 else "Neutral confluence"
    dec = FX_DEC.get(sym, 5)
    return {"score": score, "label": label,
            "signals": {"momentum": {"pct": round(day_pct, 2), "bull": day_pct >= 0},
                        "rsi": None if r is None else {"value": round(r, 2), "bull": r >= 50},
                        "ema": {"above": above},
                        "zscore": None if z is None else {"value": round(z, 2), "bull": z >= 0}},
            "sr": sr_levels(a, current, dec)}


def fx_from_data(data: dict) -> dict:
    out = {}
    series = dict(data.get("fxp", {}))
    if data.get("gold_px") is not None and len(data["gold_px"]) > 60:
        series["XAUUSD"] = data["gold_px"]
    series.update(data.get("idx", {}))
    for sym, s in series.items():
        dec = FX_DEC.get(sym, 5)
        price, prev = float(s.iloc[-1]), float(s.iloc[-2])
        out[sym] = {"display": f"{price:,.{dec}f}", "value": price, "changePct": round((price / prev - 1) * 100, 3),
                    "date": s.index[-1].strftime("%Y-%m-%d"), "spark": [round(float(v), 6) for v in s.tail(30)],
                    "confluence": fx_confluence(sym, s), "range": week_range(s)}
    return out


def build_fx(feed: dict | None, data: dict) -> dict[str, str]:
    empty = '<p class="muted-text">No FX feed saved yet. It appears after the first successful update.</p>'
    out = {"picker": "", "tiles": "", "fx": empty, "conf": empty, "econ": empty, "news": empty, "upd": "n/a"}
    tiles = []
    # our own, correctly labelled DXY and gold futures
    for series, label, dec in ():  # DXY and gold now live in the overview KPI strip
        if series is not None and len(series) > 30:
            ch = delta(series, 1, True)
            tiles.append(tile(label, fmt(series.iloc[-1], dec), ch, [round(x, 4) for x in series.tail(30)], series.index[-1].strftime("%m-%d")))
    if not feed:
        feed = {}
    out["upd"] = (str(feed.get("updated", ""))[:16].replace("T", " ") + " UTC") if feed.get("updated") else "n/a"
    for key in ("sp500", "wti", "vix", "gvz", "btc"):
        code, label, dec = {"sp500": ("SP500", "S&P 500", 0), "wti": ("DCOILWTICO", "WTI oil", 2), "vix": ("VIXCLS", "VIX", 2),
                            "gvz": ("GVZCLS", "Gold vol (GVZ)", 2), "btc": ("CBBTCUSD", "BTC", 0)}[key]
        ms = data.get("mkt", {}).get(code)
        d = (feed.get("markets") or {}).get(key) or {}
        if ms is not None and len(ms) > 5:      # FRED copy (always available) wins over the feed
            tiles.append(tile(label, fmt(ms.iloc[-1], dec), delta(ms, 1, True), [round(x, 4) for x in ms.tail(30)], ms.index[-1].strftime("%m-%d")))
        elif d.get("value") is not None:
            tiles.append(tile(d["label"], d.get("display", ""), d.get("changePct"), d.get("spark"), (d.get("date") or "")[5:]))
    out["tiles"] = "".join(tiles)

    own = fx_from_data(data)                     # our own pairs + XAUUSD, computed from saved closes
    fx = own if own else (feed.get("fx") or {})
    if own:
        dates = sorted({d["date"] for d in own.values()})
        out["upd"] = dates[-1]
    cards, confs = [], []
    live = 0
    drivers = build_fx_drivers(data)
    irows: list[dict] = []
    for sym, d in fx.items():
        cf = d.get("confluence")
        tone = "flat" if not cf else ("up" if cf["score"] >= 60 else "down" if cf["score"] <= 40 else "mid")
        has = d.get("value") is not None and str(d.get("display", "")).strip() not in ("", "-")
        live += has
        tag = f'<span class="tag {tone}">{esc(cf["label"].split(" ")[0].upper())}</span>' if cf else ""
        ch = d.get("changePct")
        cards.append(
            f'<div class="tile fxc{"" if has else " empty"}" data-sym="{esc(sym)}" title="{esc(INSTR_NAMES.get(sym, sym))}"><div class="tl"><span>{esc(sym)}</span>{tag}</div>'
            f'<div class="tv">{esc(d.get("display", "")) if has else "&ndash;"}</div>'
            f'<div class="tc {"" if ch is None else ("up" if ch >= 0 else "down")}">{esc(signed(ch, 3, "%")) if ch is not None else "&nbsp;"}</div>'
            f'{spark(d.get("spark"), None if ch is None else ch >= 0) if has else ""}</div>')
        if cf:
            sg = cf.get("signals", {})
            rows = [("Daily price momentum", "D1", signed((sg.get("momentum") or {}).get("pct"), 1, "%"), (sg.get("momentum") or {}).get("bull")),
                    ("RSI reading", "D1", ("RSI %s" % sg["rsi"]["value"]) if sg.get("rsi") else "n/a", (sg.get("rsi") or {}).get("bull")),
                    ("Price vs EMA", "D1", "Above EMA" if (sg.get("ema") or {}).get("above") else "Below EMA", (sg.get("ema") or {}).get("above")),
                    ("Price Z-score", "Stat", ("Z=" + fmt(sg["zscore"]["value"], 2, True)) if sg.get("zscore") else "n/a", (sg.get("zscore") or {}).get("bull"))]
            sig_html = "".join(
                f'<div class="sig"><span class="dot {"up" if b else "down"}" title="{"bullish" if b else "bearish"}"></span><span class="sn">{esc(n)}</span>'
                f'<span class="st">{esc(t)}</span><span class="sv">{esc(v)}</span></div>' for n, t, v, b in rows)
            sr_html = "".join(
                f'<div class="sr {"above" if x["distPct"] >= 0 else "below"}"><span>{esc(x["level"])}</span><span class="ss">{esc(x["strength"])}</span>'
                f'<span class="sd">{esc(signed(x["distPct"], 2, "%"))}</span></div>' for x in cf.get("sr", []))
            rg = d.get("range")
            evs = week_events(sym)
            rng_html = ""
            if rg:
                rng_html = (f'<div class="xr"><span>Likely range this week</span><b>&plusmn;{rg["pct"]:.2f}%</b>'
                            f'</div><span class="xv">{rg["lo"]:,.{FX_DEC.get(sym, 5)}f} to {rg["hi"]:,.{FX_DEC.get(sym, 5)}f}</span>'
                            f'<div class="xe">{(str(len(evs)) + " event" + ("s" if len(evs) != 1 else "") + " in the next 7 days: " + ", ".join(evs)) if evs else "No scheduled events in the next 7 days"}</div>')
            confs.append(
                f'<article class="conf" data-sym="{esc(sym)}"><div class="ctop"><b>{esc(sym)}{f" <small>{esc(INSTR_NAMES[sym])}</small>" if sym in INSTR_NAMES else ""}</b><span>{esc(d.get("display", ""))}</span></div>'
                f'<div class="chead">{gauge_ring(cf["score"], tone)}<div class="verdict {tone}">{esc(cf["label"].split(" ")[0])} confluence<small>Confluence score</small></div></div>{rng_html}'
                f'<h5>Signals</h5>{sig_html}{(drivers.get(sym) or {}).get("html", "")}<h5>Support / resistance</h5>{sr_html}</article>')
            rg_ = d.get("range")
            irows.append({"id": f"FX:{sym}", "label": sym, "ticker": INSTR_DESC.get(sym, ""), "group": instr_group(sym), "latest": d.get("display", ""),
                          "d1": signed(d.get("changePct"), 2, "%") if d.get("changePct") is not None else "", "score": str(cf["score"]), "tone": tone,
                          "lean": (drivers.get(sym) or {}).get("label", "").capitalize(), "range": f"±{rg_['pct']:.2f}%" if rg_ else "",
                          "info": {"title": sym, "html": confs[-1]}})
    if cards:
        note = "" if live else '<p class="emptystate">No FX feed saved yet. It appears after the first successful update.</p>'
        out["fx"] = note + '<div class="tiles fxtiles">' + "".join(cards) + "</div>"
    out["irows"] = irows
    out["drivers"] = drivers
    out["picker"] = ('<div class="picker" id="fxpick" role="group" aria-label="Choose which instruments to show">'
                     '<span class="pl">My view</span>' + "".join(f'<button type="button" class="fxc" data-sym="{esc(k)}" aria-pressed="true" title="{esc(INSTR_NAMES.get(k, k))}">{esc(k)}</button>' for k in fx)
                     + '<button type="button" class="fxc ghost" data-all="1">All</button><button type="button" class="fxc ghost" data-none="1">None</button></div>') if fx else ""
    if confs:
        out["conf"] = ('<div class="confgrid">' + "".join(confs) + "</div>"
                       '<p class="note">Context, not a forecast. The likely range is the 68% range (+-1 sigma) from the last 20 and 60 days of price swings; in a 2001-2026 test on 7 pairs the weekly move stayed inside it 68.3% of weeks. '
                       'The macro read (leans higher, lower or mixed) was right only about half of weeks in the same test, so treat it as background, not a signal. '
                       'Rows marked info are shown but not counted. Choose which instruments to show with the buttons above (or My view in the Terminal); your choice is saved in this browser and shared by both views.</p>')
    else:
        out["conf"] = ('<p class="emptystate">No FX feed saved yet. It appears after the first successful update.</p>'
                       '<p class="note">Each pair will show a confluence score (0-100), its momentum, RSI, trend (price vs EMA) and Z-score signals, and nearby support and resistance.</p>')

    econ = feed.get("econ") or {}
    items = []
    for d in econ.values():
        sfx = d.get("suffix") or ""
        ch = d.get("change")
        chs = "" if ch is None else f'<span class="{"up" if ch > 0 else "down" if ch < 0 else ""}">{esc(signed(ch, 2 if sfx == "%" else 1, d.get("unit") or ""))}</span>'
        items.append(f'<div class="econ"><div class="tl">{esc(d["label"])}</div><div class="tv">{esc(d.get("display", "n/a"))}{esc(sfx)}</div>'
                     f'<div class="tc">{chs}</div><div class="ed">{esc(d.get("date", ""))}</div></div>')
    if items:
        out["econ"] = '<div class="econgrid">' + "".join(items) + "</div>"

    news = feed.get("news") or []
    if not news and feed:
        chk = (str(feed.get("updated", ""))[:16].replace("T", " ") + " UTC") if feed.get("updated") else "n/a"
        out["news"] = '<p class="muted-text news-empty">No headlines right now. Checked ' + esc(chk) + '.</p>'
    if news:
        # Newest first, undated items last, same headline shown once (the feed arrives in search-relevance order).
        seen, ordered = set(), []
        for n in sorted(news, key=lambda x: str(x.get("publishedAt") or ""), reverse=True):
            key = " ".join(str(n.get("title", "")).lower().split())
            if key and key not in seen:
                seen.add(key)
                ordered.append(n)
        news = ordered
        nrows = []
        for n in news:
            url = str(n.get("url", ""))
            if not url.startswith("https://"):
                url = ""
            t = (n.get("publishedAt") or "")[:16].replace("T", " ")
            title = esc(n.get("title", ""))
            link = f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{title}</a>' if url else title
            nrows.append(f'<li><span class="src">{esc((n.get("source") or "News").split(" ")[0][:10])}</span><div>{link}<small>{esc(t)} UTC</small></div></li>')
        out["news"] = '<ul class="news">' + "".join(nrows) + "</ul>"
    return out


# pair -> (other currency, its yield country, tenor, its policy-rate column, side); side +1 = pair rises when that currency rises
DRV_PAIRS = {
    "EURUSD": ("EUR", "DE", "2Y", "EA", 1), "GBPUSD": ("GBP", "GB", "2Y", "GB", 1), "AUDUSD": ("AUD", "AU", "2Y", "AU", 1),
    "NZDUSD": ("NZD", "NZ", "5Y", "NZ", 1), "USDJPY": ("JPY", "JP", "2Y", "JP", -1), "USDCHF": ("CHF", "CH", "2Y", "CH", -1),
    "USDCAD": ("CAD", "CA", "2Y", "CA", -1),
}
DRV_BANK = {"US": "Fed", "EA": "ECB", "GB": "BoE", "JP": "BoJ", "CH": "SNB", "CA": "BoC", "AU": "RBA", "NZ": "RBNZ"}
DRV_RISK = {"AUD": 1, "NZD": 1, "CAD": 1, "JPY": -1, "CHF": -1}     # +1 gains when risk appetite rises, -1 gains when it falls
INSTR_NAMES = {"XAUUSD": "Gold futures", "ES": "S&P 500", "NQ": "Nasdaq 100", "US30": "Dow 30"}
INDEX_SYMS = ("ES", "NQ", "US30")
US_EVENT_SYMS = {"EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "USDCHF", "NZDUSD", "XAUUSD", *INDEX_SYMS}   # instruments that react to US events


def week_range(s: pd.Series) -> dict | None:
    """Likely range for the next 5 trading days, +-1 sigma, from recent daily swings (last 20 and 60 days, equal weight).
    Backtest 2001-2026 on 7 pairs: the weekly move stayed inside this range in 68.3% of weeks (68.3% expected)."""
    r = np.log(s.astype(float)).diff().dropna()
    if len(r) < 70:
        return None
    sig = math.sqrt(5) * (0.5 * float(r.tail(20).std()) + 0.5 * float(r.tail(60).std()))
    px = float(s.iloc[-1])
    return {"pct": round(sig * 100, 2), "lo": px * math.exp(-sig), "hi": px * math.exp(sig)}


def week_events(sym: str) -> list[str]:
    """Names of calendar events in the next 7 days that move this instrument."""
    p = Path(__file__).parent / "calendar.json"
    if not p.exists():
        return []
    today = datetime.now(timezone.utc).date()
    out = []
    for e in json.loads(p.read_text()).get("events", []):
        n = (date.fromisoformat(e["date"]) - today).days
        if 0 <= n <= 7 and (sym in e["pairs"] or ("USD pairs" in e["pairs"] and sym in US_EVENT_SYMS)):
            out.append(e["name"])
    return out


def _chg(s: pd.Series | None, n: int = 21) -> float | None:
    return None if s is None or len(s) <= n else float(s.iloc[-1] - s.iloc[-n])


def _stance(pol: pd.DataFrame, col: str) -> str:
    ch = float(pol[col].iloc[-1] - pol[col].iloc[max(len(pol) - 183, 0)])      # direction over the last 6 months
    return "hiking" if ch >= 0.25 else "cutting" if ch <= -0.25 else "holding"


def _cot_row(net: pd.Series | None, name: str, side: int) -> dict:
    if net is None or len(net) < 52:
        return {"name": name, "value": "n/a", "state": 0, "counted": True}
    z = float((net.iloc[-1] - net.tail(52).mean()) / net.tail(52).std(ddof=1))
    crowded = abs(z) >= 2      # extreme positioning is as often a reversal risk as a trend, so it is shown but not counted
    return {"name": name, "value": f"crowded {'long' if z > 0 else 'short'} (Z {z:+.1f})" if crowded else f"Z {z:+.1f}",
            "state": 0 if (crowded or abs(z) < 0.5) else (1 if z * side > 0 else -1), "counted": True}


def _drivers_for(sym: str, data: dict) -> list[dict]:
    d = data.get("drv") or {}
    pol = d.get("pol")
    yields = data.get("yields", {})
    vix = data.get("mkt", {}).get("VIXCLS")
    vz = None if vix is None or len(vix) < 120 else float((vix.iloc[-1] - vix.tail(252).mean()) / vix.tail(252).std())
    vix_txt = "n/a" if vz is None else f"VIX {vix.iloc[-1]:.1f}" + (" stressed" if vz > 0.5 else " calm" if vz < -0.5 else "")
    rows: list[dict] = []
    if sym in DRV_PAIRS:
        cur, cc, ten, pcol, side = DRV_PAIRS[sym]
        fy = (d.get("yc", {}).get(cc) or {}).get(ten)
        uy = yields.get("DGS2" if ten == "2Y" else "DGS5")
        yname = f"{ten} yield gap, 4-week"
        if fy is not None and uy is not None and len(fy) > 60 and len(uy) > 60:
            j = pd.concat([fy.rename("f"), uy.rename("u")], axis=1).sort_index().ffill().dropna()
            g = (j["f"] - j["u"]) * side
            chg = float(g.iloc[-1] - g.iloc[-21])
            rows.append({"name": yname, "value": f"{chg:+.2f} pp", "state": 0 if abs(chg) < 0.05 else (1 if chg > 0 else -1), "counted": True})
        else:
            rows.append({"name": yname, "value": "n/a", "state": 0, "counted": True})
        if pol is not None and pcol in pol and "US" in pol:
            f, u = float(pol[pcol].iloc[-1]), float(pol["US"].iloc[-1])
            gp = (f - u) * side
            rows.append({"name": "Policy rates", "value": f"{u:.2f} vs {f:.2f}", "state": 0 if abs(gp) < 0.25 else (1 if gp > 0 else -1), "counted": True,
                         "sub": f"{DRV_BANK['US']} {_stance(pol, 'US')} · {DRV_BANK[pcol]} {_stance(pol, pcol)}"})
        else:
            rows.append({"name": "Policy rates", "value": "n/a", "state": 0, "counted": True})
        rows.append(_cot_row((d.get("cot") or {}).get(cur), "Positioning (Z)", side))
        sens = DRV_RISK.get(cur, 0)
        rk = 0 if (vz is None or not sens) else (-1 if vz > 0.5 else 1 if vz < -0.5 else 0) * sens * side
        rows.append({"name": "Risk appetite", "value": vix_txt if sens else "n/a", "state": rk, "counted": False})
        return rows
    ry = yields.get("DFII10")
    rchg = _chg(ry)
    rrow = {"name": "10Y real yield, 4-week", "value": "n/a" if rchg is None else f"{rchg:+.2f} pp",
            "state": 0 if rchg is None or abs(rchg) < 0.05 else (-1 if rchg > 0 else 1), "counted": True}      # falling real yields support gold and stocks
    fed = {"name": "Fed policy (6 months)", "value": "n/a", "state": 0, "counted": True}
    if pol is not None and "US" in pol:
        st = _stance(pol, "US")
        fed = {"name": "Fed policy (6 months)", "value": f"{float(pol['US'].iloc[-1]):.2f}% {st}", "state": {"cutting": 1, "hiking": -1}.get(st, 0), "counted": True}
    if sym == "XAUUSD":
        dxy = data.get("dxy")
        dch = None if dxy is None or len(dxy) < 25 else float(dxy.iloc[-1] / dxy.iloc[-21] - 1) * 100
        rows = [rrow, fed, _cot_row((d.get("cot") or {}).get("GOLD"), "Positioning (Z)", 1),
                {"name": "Dollar (DXY), 4-week", "value": "n/a" if dch is None else f"{dch:+.1f}%", "state": 0 if dch is None or abs(dch) < 0.5 else (-1 if dch > 0 else 1), "counted": True}]
    else:
        rows = [rrow, fed, _cot_row((d.get("cot") or {}).get(sym), "Positioning (Z)", 1),
                {"name": "Volatility", "value": vix_txt, "state": 0, "counted": False}]
    return rows


def build_fx_drivers(data: dict) -> dict[str, dict]:
    """Macro drivers per instrument. Each counted driver says whether it supports the instrument higher (+1), lower (-1) or neither (0);
    a lean needs a net of 2. Context only: a 2001-2026 backtest found the read right about half of weeks (no reliable edge)."""
    d = data.get("drv") or {}
    if not d or (not d.get("yc") and d.get("pol") is None):
        return {}
    out: dict[str, dict] = {}
    for sym in [*DRV_PAIRS, "XAUUSD", *INDEX_SYMS]:
        rows = _drivers_for(sym, data)
        counted = [r for r in rows if r["counted"]]
        ups = sum(1 for r in counted if r["state"] > 0)
        dns = sum(1 for r in counted if r["state"] < 0)
        if ups - dns >= 2:
            label, count = "LEANS HIGHER", f"{ups} of {len(counted)} lean higher"
        elif dns - ups >= 2:
            label, count = "LEANS LOWER", f"{dns} of {len(counted)} lean lower"
        else:
            label, count = "MIXED", f"{ups} up, {dns} down"
        def row(r: dict) -> str:
            st = r["state"]
            sub = f'<small class="ssub">{esc(r["sub"])}</small>' if r.get("sub") else ""
            title = ("supports it higher" if st > 0 else "supports it lower" if st < 0 else "neutral") + ("" if r["counted"] else " (shown, not counted)")
            return (f'<div class="sig m"><span class="dot {"up" if st > 0 else "down" if st < 0 else "flat"}" title="{title}"></span>'
                    f'<span class="sn">{esc(r["name"])}{"" if r["counted"] else " <i>info</i>"}</span><span class="sv">{esc(r["value"])}</span>{sub}</div>')
        out[sym] = {"html": f'<h5>Macro drivers <span class="tag lean">{label}</span><small class="dcount">{esc(count)}</small></h5>' + "".join(row(r) for r in rows),
                    "rows": rows, "label": label, "count": count}
    return out


INSTR_DESC = {"EURUSD": "Euro / US dollar", "GBPUSD": "Pound / US dollar", "USDJPY": "US dollar / yen", "USDCAD": "US dollar / Canadian dollar",
              "AUDUSD": "Australian dollar / US dollar", "USDCHF": "US dollar / Swiss franc", "NZDUSD": "New Zealand dollar / US dollar",
              "XAUUSD": "Gold futures", "ES": "S&P 500", "NQ": "Nasdaq 100", "US30": "Dow 30"}


def instr_group(sym: str) -> str:
    return "Gold" if sym == "XAUUSD" else "US indices" if sym in INDEX_SYMS else "FX pairs"


def build_instr_watch_html(rows: list[dict]) -> str:
    cols = ('<th class="cb"><span class="sr-only">Select</span></th><th>Name</th><th class="c-tk">Market</th><th>Price</th><th>1D</th>'
            '<th class="c-cf">Score</th><th class="c-ln">Macro lean</th><th class="c-rg">Range</th>')
    out = [f'<table class="watch instr hide-c-tk"><thead><tr>{cols}</tr></thead>']
    groups: list[str] = []
    for r_ in rows:
        if r_["group"] not in groups:
            groups.append(r_["group"])
    for g in groups:
        out.append(f'<tbody data-group="{esc(g)}"><tr class="grp"><td class="cb"><span class="gbox" role="checkbox" aria-checked="false" tabindex="0" aria-label="Plot {esc(g)}"></span></td>'
                   f'<th colspan="6"><button type="button" class="gt" aria-expanded="true"><span class="car"></span>{esc(g)}</button></th>'
                   f'<td class="gmove"><button type="button" class="mv up" aria-label="Move {esc(g)} up">&#9650;</button><button type="button" class="mv dn" aria-label="Move {esc(g)} down">&#9660;</button></td></tr>')
        for r_ in (x for x in rows if x["group"] == g):
            out.append(
                f'<tr class="row" tabindex="0" data-id="{esc(r_["id"])}" data-family="fx">'
                f'<td class="cb"><span class="pick" aria-hidden="true"></span></td>'
                f'<th scope="row"><span class="plus">+</span>{esc(r_["label"])}</th><td class="c-tk tk">{esc(r_["ticker"])}</td>'
                f'<td class="yd">{esc(r_["latest"])}</td><td class="{chg_class(r_["d1"])}">{esc(r_["d1"])}</td>'
                f'<td class="c-cf {r_["tone"]}">{esc(r_["score"])}</td><td class="c-ln tk">{esc(r_["lean"])}</td><td class="c-rg tk">{esc(r_["range"])}</td></tr>')
        out.append("</tbody>")
    out.append("</table>")
    return "".join(out)


def build_drivers_matrix(rows: list[dict], drivers: dict) -> str:
    """One row per instrument; arrows show whether each driver supports it higher, lower or neither."""
    def cell(r: dict) -> str:
        st = r["state"]
        arrow = "&#9650;" if st > 0 else "&#9660;" if st < 0 else "&ndash;"
        return (f'<td class="dc {"up" if st > 0 else "down" if st < 0 else "flat"}" title="{esc(r["name"])}: {"supports it higher" if st > 0 else "supports it lower" if st < 0 else "neutral"}{"" if r["counted"] else " (shown, not counted)"}">'
                f'<span class="da">{arrow}</span><small>{esc(r["value"])}</small></td>')
    body = []
    for r_ in rows:
        dv = drivers.get(r_["label"])
        if not dv:
            continue
        body.append(f'<tr data-id="{esc(r_["id"])}"><th scope="row">{esc(r_["label"])}</th>' + "".join(cell(x) for x in dv["rows"][:4]) +
                    f'<td class="dread"><span class="tag lean">{esc(dv["label"])}</span><small>{esc(dv["count"])}</small></td></tr>')
    if not body:
        return '<p class="muted-text pad">Macro drivers appear after the next data update.</p>'
    return ('<table class="drv"><thead><tr><th>Instrument</th><th>Yields<small>FX gap trend / real yield</small></th><th>Policy<small>central banks</small></th>'
            '<th>Positioning<small>speculators, Z</small></th><th>Risk / dollar<small>info for FX, indices</small></th><th>Macro lean</th></tr></thead><tbody>'
            + "".join(body) + '</tbody></table>')


def fx_family(data: dict) -> dict:
    """Price history for the terminal chart: union of dates, one list per instrument (null where a market was closed)."""
    ser: dict[str, pd.Series] = {}
    for sym, s_ in {**data.get("fxp", {}), **({"XAUUSD": data["gold_px"]} if data.get("gold_px") is not None else {}), **data.get("idx", {})}.items():
        ser[sym] = s_.dropna().tail(780)
    if not ser:
        return {"dates": [], "series": {}}
    idx = sorted(set().union(*[set(x.index) for x in ser.values()]))
    idx = idx[-780:]
    out = {}
    for sym, x in ser.items():
        dec = 3 if sym == "USDJPY" else 2 if sym in INSTR_NAMES else 5
        out[f"FX:{sym}"] = [None if d not in x.index else round(float(x[d]), dec) for d in idx]
    return {"dates": [d.strftime("%Y-%m-%d") for d in idx], "series": out}


def build_week(data: dict) -> str:
    """Upcoming central bank decisions and key US releases from the hand-maintained calendar.json, plus a one-line regime read."""
    empty = '<p class="muted-text pad">No calendar saved yet.</p>'
    p = Path(__file__).parent / "calendar.json"
    if not p.exists():
        return empty
    cal = json.loads(p.read_text())
    today = datetime.now(timezone.utc).date()
    ev = [e for e in cal.get("events", []) if 0 <= (date.fromisoformat(e["date"]) - today).days <= 14][:8]
    bits = []
    vix = data.get("mkt", {}).get("VIXCLS")
    if vix is not None and len(vix) > 120:
        vz = float((vix.iloc[-1] - vix.tail(252).mean()) / vix.tail(252).std())
        bits.append(f"Risk appetite: {'stressed' if vz > 0.5 else 'calm' if vz < -0.5 else 'normal'} (VIX {vix.iloc[-1]:.1f})")
    dxy = data.get("dxy")
    if dxy is not None and len(dxy) > 60:
        ch = float(dxy.iloc[-1] / dxy.iloc[-21] - 1) * 100
        bits.append(f"Dollar (DXY): {'firming' if ch > 0.5 else 'softening' if ch < -0.5 else 'flat'} ({ch:+.1f}% in 4 weeks, {'above' if dxy.iloc[-1] >= dxy.tail(50).mean() else 'below'} its 50-day average)")
    fed = data.get("fed") or {}
    fedline = ""
    for m in fed.get("meetings", []):
        if m.get("date") and 0 <= (date.fromisoformat(m["date"]) - today).days <= 14:
            raw = m["raw"]
            hold = float(raw.get("H0") or 0); cut = float((raw.get("C25") or 0) + (raw.get("C26") or 0)); hike = float((raw.get("H25") or 0) + (raw.get("H26") or 0))
            fedline = f"market odds: hold {hold * 100:.0f}%, cut {cut * 100:.0f}%, hike {hike * 100:.0f}%"
            fed_date = m["date"]
            break
    rows = []
    for e in ev:
        d = date.fromisoformat(e["date"]); n = (d - today).days
        when = "today" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"
        extra = fedline if (e["name"].startswith("Fed decision") and fedline and e["date"] == fed_date) else ""
        rows.append(f'<li><span class="wkd">{d.strftime("%a %d %b")}</span><span class="wkn">{esc(e["name"])}</span>'
                    f'<span class="wkp">{esc(e["pairs"])}</span><span class="wkx">{esc(extra) if extra else when}</span></li>')
    reg = f'<p class="wkreg">{" &middot; ".join(esc(b) for b in bits)}</p>' if bits else ""
    body = f'<ul class="wkl">{"".join(rows)}</ul>' if rows else '<p class="muted-text pad">No scheduled central bank decisions or key US releases in the next 14 days.</p>'
    return reg + body + f'<p class="note">Dates from each central bank\'s own calendar (checked {esc(cal.get("checked", ""))}); the calendar is maintained by hand. RBNZ dates are not yet published.</p>'


def build_cot_cards(playbook: dict, metrics: dict, split: bool = False):
    cards = []
    for key in ("gold", "usd_index"):
        spec = playbook["cot"][key]
        m = metrics.get(f"COT:{key}")
        if not m:
            cards.append(f'<article class="cotinner muted"><h3>{esc(spec["title"])}</h3><p>No data yet. It appears after the first successful CFTC update.</p></article>')
            continue
        band = pick_band(spec["bands"], m["index"])
        band = dict(band, label="COT Index: " + band["label"]) if band else band   # names the measure, so it is not read as the Z reading
        cards.append(card(spec["title"], fmt(m["net"], 0, True), f' net contracts · {m["date"]}', band,
                          [("COT Index (0-100, 52w)", fmt(m["index"], 1)), ("Z-score (52w)", fmt(m["z"], 2, True)),
                           ("Spec net % of OI", fmt(m["net_pct"], 1) + "%"), ("Weekly change", fmt(m["wchg"], 0, True))], spec["what"], gauge_html=gauge(m["index"])))
    return cards if split else "".join(cards)


def build_cot_charts(data: dict) -> dict:
    charts: dict = {}
    for ckey, field, name_map, dec in (("cot", "index", (("usd_index", "USD Index (Non-Comm)"), ("gold", "Gold (Non-Comm)")), 1),
                                       ("cotoi", "net_oi", (("usd_index", "USD Index (Non-Comm)"), ("gold", "Gold (Non-Comm)")), 2),
                                       ("cotpx", "price_chg", (("usd_index", "DXY"), ("gold", "Gold futures")), 2)):
        sets = []
        for key, nm in name_map:
            c = data["cot"].get(key)
            if c is not None and c[field].notna().any():
                sets.append((nm, c[field].tail(COT_WINDOW)))
        if sets:
            lab = sorted({i.strftime("%Y-%m-%d") for _, s_ in sets for i in s_.index})
            charts[ckey] = {"labels": lab, "sets": [{"name": n, "data": [r(s_.get(pd.Timestamp(l)), dec) for l in lab]} for n, s_ in sets]}
    return charts


def cot_view(c: dict) -> pd.DataFrame:
    """The columns of the user's Excel COT sheet, oldest first."""
    frac = c["net_oi"] / 100
    return pd.DataFrame({
        "Open interest": c["oi"], "Long": c["long"], "Short": c["short"],
        "Net pos": c["net"], "Spec WoW change": c["wchg"], "OI change": c["oi_chg"],
        "OI normalized net": frac, "Normalized WoW change": frac.diff(), "Spec net % of OI": c["net_oi"],
        "Friday close": c["price"], "Price chg %": c["price_chg"],
    })


COT_DEC = {"Open interest": (0, False), "Long": (0, False), "Short": (0, False), "Net pos": (0, True), "Spec WoW change": (0, True),
           "OI change": (0, True), "OI normalized net": (3, False), "Normalized WoW change": (3, True), "Spec net % of OI": (1, False),
           "Friday close": (2, False), "Price chg %": (2, True)}
COT_READ = ["Net pos", "Spec WoW change", "OI change", "OI normalized net", "Normalized WoW change", "Spec net % of OI", "Price chg %"]


def z_band(z) -> tuple[str, str]:
    """Z-score scale used everywhere on the page:
    below -2 extremely low | -2 to -1 below normal | -1 to +1 normal | +1 to +2 above normal | above +2 extremely high."""
    if z is None or (isinstance(z, float) and math.isnan(z)):
        return "", ""
    if z <= -2:
        return "Extremely low", "down"
    if z < -1:
        return "Below normal", "down"
    if z <= 1:
        return "Normal", "flat"
    if z < 2:
        return "Above normal", "up"
    return "Extremely high", "up"


def build_cot_tables(data: dict) -> dict[str, str]:
    """One 52-week table per instrument (newest first) with the Excel-style statistics block under it."""
    out: dict[str, str] = {}
    for key in ("gold", "usd_index"):
        c = data["cot"].get(key)
        if c is None:
            out[key] = '<p class="muted-text pad">No data yet.</p>'
            continue
        v = cot_view(c).tail(COT_WINDOW)
        rows = []
        for d, row in v.iloc[::-1].iterrows():
            tds = ""
            for col in v.columns:
                nd, sg = COT_DEC[col]
                txt = fmt(num(row[col]), nd, sg)
                cls = chg_class(txt) if sg else ""
                tds += f'<td class="{cls}">{esc(txt)}</td>'
            dot = '<span class="curdot" title="Latest report"><span class="sr-only">Latest report: </span></span>' if not rows else ""
            rows.append(f'<tr{" class=\"cur\"" if not rows else ""}><th scope="row">{dot}{d.strftime("%Y-%m-%d")}</th>{tds}</tr>')

        def zval(s_):
            s_ = s_.dropna()
            sd = s_.std(ddof=1)
            return num((s_.iloc[-1] - s_.mean()) / sd) if len(s_) > 2 and sd else None

        def stat_row(label, fn, nd=None, sign=False, cls=""):
            cells = ""
            for col in v.columns:
                d_, _ = COT_DEC[col]
                val = num(fn(v[col].dropna()))
                tcls = ""
                if cls == "st-z" and val is not None and abs(val) >= 1:
                    tcls = f' class="zhi {"up" if val > 0 else "down"}"'
                cells += f"<td{tcls}>{esc(fmt(val, d_ if nd is None else nd, sign))}</td>"
            return f'<tr class="{cls}"><th scope="row">{label}</th>{cells}</tr>'
        stats = [stat_row("Average", lambda s_: s_.mean(), cls="st-avg"), stat_row("Std dev", lambda s_: s_.std(ddof=1), cls="st-sd"),
                 stat_row("Min", lambda s_: s_.min(), cls="st-mm"), stat_row("Max", lambda s_: s_.max(), cls="st-mm"),
                 stat_row("Z-score", lambda s_: zval(s_) if zval(s_) is not None else float("nan"), 2, True, cls="st-z")]
        read = ""
        for col in v.columns:
            if col in COT_READ:
                lab, tone = z_band(zval(v[col]))
                read += f'<td><span class="pill {tone}">{esc(lab)}</span></td>' if lab else "<td></td>"
            else:
                read += "<td></td>"
        stats.append(f'<tr class="readrow"><th scope="row" title="Reading of each column\'s Z-score">Z reading</th>{read}</tr>')
        ci = num(c["index"].iloc[-1])
        cot_cells = (f'<td colspan="{len(v.columns)}" class="lefttd"><span class="cimeter"><span class="cifill" style="width:{0 if ci is None else max(0, min(100, ci)):.1f}%"></span></span>'
                     f'<b class="cival">{esc(fmt(ci, 1))}</b> <span class="muted-text">0 = most short, 100 = most long over 52 reports</span></td>')
        stats.append(f'<tr class="st-ci"><th scope="row">COT Index</th>{cot_cells}</tr>')
        heads = ["Report date"] + list(v.columns)
        cols = '<colgroup><col class="cd">' + "<col>" * len(v.columns) + "</colgroup>"
        out[key] = ('<div class="cotpair"><div class="scroll tall"><table class="weekly">' + cols + "<thead><tr>" + "".join(f"<th>{h}</th>" for h in heads)
                    + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
                    + f'<div class="stathead">Statistics, last {COT_WINDOW} reports</div>'
                    + '<div class="statwrap"><table class="weekly stats">' + cols + "<tbody>" + "".join(stats) + "</tbody></table></div></div>")
    return out


def build_kpis(data: dict, computed: dict) -> str:
    """Headline tiles for the overview strip."""
    ys, ss = data["yields"], computed["spread_series"]
    items = [("US 10Y yield", ys.get("DGS10"), 2, "%", False), ("US 2Y yield", ys.get("DGS2"), 2, "%", False),
             ("10Y vs 2Y spread", ss.get("10y-2y"), 2, " pp", False), ("10Y real yield", ys.get("DFII10"), 2, "%", False),
             ("DXY", data.get("dxy"), 2, "", True), ("Gold futures", data.get("gold_px"), 1, "", True)]
    html_ = []
    for label, s_, nd, unit, pct in items:
        if s_ is None or len(s_) < 30:
            continue
        ch = delta(s_, 1, pct)
        up = None if ch is None else ch >= 0
        chs = signed(ch, 2, "%") if pct else signed(None if ch is None else ch * 100, 1, " bp")   # yields and spreads move in basis points, as in the tables
        html_.append(f'<div class="kpi"><div class="kl">{esc(label)}</div><div class="kv">{esc(fmt(s_.iloc[-1], nd))}<small>{esc(unit.strip())}</small></div>'
                     f'<div class="kc"><span class="chip-chg {"" if up is None else ("up" if up else "down")}">{"&#9650;" if up else "&#9660;" if up is not None else ""} {esc(chs)} <span>1D</span></span></div>{spark([round(x, 4) for x in s_.tail(60)], up)}</div>')
    return "".join(html_)


def curve_payload(data: dict) -> dict | None:
    ys = data["yields"]
    tenors = [(lab, code) for lab, code in TENORS if code in ys]
    if len(tenors) < 3:
        return None
    sets = []
    for name, back in (("Now", 0), ("1M ago", 21), ("1Y ago", 252)):
        vals = [r(ys[c].iloc[-1 - back], 3) if len(ys[c]) > back else None for _, c in tenors]
        sets.append({"name": name, "data": vals})
    return {"labels": [t for t, _ in tenors], "sets": sets}


FRED_NOTICE = "This product uses the FRED\u00ae API but is not endorsed or certified by the Federal Reserve Bank of St. Louis."
_FRED_KEYED = re.compile(r"(?:route: |, )keyed x\d+")


FRED_TERMS_URL = "https://fred.stlouisfed.org/docs/api/terms_of_use.html"


def build_footer(data: dict) -> str:
    """FRED's required notice + Terms link, shown only when the keyed API route was used (FX_MACRO note or error); never raises."""
    try:
        st = (data.get("status") or {}).get("FX_MACRO") or {}
        text = f'{st.get("note") or ""} {st.get("error") or ""}'
    except Exception:  # noqa: BLE001
        text = ""
    if not _FRED_KEYED.search(text):
        return ""
    return (f'<footer class="site-foot wrap"><p class="muted-text">{esc(FRED_NOTICE)} '
            f'By using this page you agree to be bound by the '
            f'<a class="muted-text" href="{FRED_TERMS_URL}" target="_blank" rel="noopener noreferrer">FRED API Terms of Use</a>.</p></footer>')


def build_news_checked(feed: dict | None) -> str:
    """Small 'Checked <time> UTC' line for the Headlines card (news_updated, else feed updated); empty when unknown."""
    try:
        feed = feed or {}
        ts = str(feed.get("news_updated") or feed.get("updated") or "")[:16].replace("T", " ")
    except Exception:  # noqa: BLE001
        ts = ""
    return f'<span class="note">Checked {esc(ts)} UTC</span>' if ts else ""


def build_status(data: dict, today: date) -> str:
    rows = []
    st = data["status"]
    for name, info in sorted(st.items()):
        last = info.get("last_data_date") or "-"
        if name == "RUN_MODE":
            mode = info.get("mode") or ("fast" if str(info.get("note") or "").startswith("fast") else "full")
            rows.append(f'<tr><td>Last run</td><td>{esc(mode)} run</td><td>-</td><td>{esc(info.get("checked_utc", ""))}</td></tr>')
            continue
        stale = ""
        if info.get("last_data_date"):
            age = (today - datetime.strptime(info["last_data_date"], "%Y-%m-%d").date()).days
            limit = 12 if name.startswith("COT") else (10 if name in ("FX_MACRO", "FX_NEWS") else 6)
            if age > limit:
                stale = ' <span class="badge warn">stale</span>'
        state = "ok" if info.get("ok") else '<span class="badge warn">last update failed</span>'
        err = f' <span class="err">{esc(info["error"])}</span>' if info.get("error") else ""
        rows.append(f"<tr><td>{esc(name)}</td><td>{state}{stale}{err}</td><td>{esc(last)}</td><td>{esc(info.get('checked_utc', ''))}</td></tr>")
    if not rows:
        return "<p>No update has been recorded yet (data/status.json is missing). The numbers above come from the CSV files committed in the repo.</p>"
    return ('<table><thead><tr><th>Source</th><th>Status</th><th>Latest data date</th><th>Checked (UTC)</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table>")



# ---------------------------------------------------------------- options positioning (data/options/*.json from options_levels.py)
OPT_META = {
    "SPX": {"title": "S&amp;P 500", "src": "SPX options", "map": "ES", "mode": "add", "dec": 0, "mdec": 2},
    "NDX": {"title": "Nasdaq 100", "src": "NDX options", "map": "NQ", "mode": "add", "dec": 0, "mdec": 2},
    "DJX": {"title": "Dow", "src": "DJX options", "map": "YM", "mode": "mul", "dec": 2, "mdec": 0},
    "RUT": {"title": "Russell 2000", "src": "RUT options", "map": "RTY", "mode": "add", "dec": 0, "mdec": 1},
    "GOLD": {"title": "Gold", "src": "GLD options", "map": "Gold (GC)", "mode": "mul", "dec": 2, "mdec": 1},
}


def load_options(data_dir: Path) -> dict:
    out = {}
    ip = data_dir / "options" / "VOL_INDICES.csv"
    if ip.exists():
        try:
            out["_vol_idx"] = pd.read_csv(ip, parse_dates=["date"]).set_index("date").sort_index()
        except Exception:  # noqa: BLE001
            pass
    vp = data_dir / "options" / "VIX_TERM.csv"
    if vp.exists():
        try:
            out["_vix_term"] = pd.read_csv(vp, parse_dates=["date"]).set_index("date").sort_index().dropna()
        except Exception:  # noqa: BLE001
            pass
    for key in OPT_META:
        p = data_dir / "options" / f"{key}.json"
        if p.exists():
            try:
                snap = json.loads(p.read_text())
                hp = data_dir / "options" / f"{key}_history.csv"
                hist = pd.read_csv(hp) if hp.exists() else pd.DataFrame()
                snap["history"] = hist.tail(10).iloc[::-1].to_dict("records") if len(hist) else []
                snap["history_full"] = json.loads(hist.tail(260).to_json(orient="records")) if len(hist) else []
                out[key] = snap
            except Exception:  # noqa: BLE001
                pass
    return out


def _opt_mapper(s: dict, meta: dict):
    mp, spot = s.get("mapped_price"), s.get("spot")
    if not mp or not spot:
        return None
    if meta["mode"] == "add":
        basis = mp - spot
        return lambda k: None if k is None else k + basis
    ratio = mp / spot
    return lambda k: None if k is None else k * ratio


def opt_payload(data: dict) -> dict:
    out = {}
    for key, meta in OPT_META.items():
        s = (data.get("options") or {}).get(key)
        if not s or not s.get("surface"):
            continue
        mp, spot = s.get("mapped_price"), s.get("spot")
        mapping = None
        if mp and spot:
            mapping = {"mode": meta["mode"], "basis": round(mp - spot, 4), "ratio": round(mp / spot, 6), "label": meta["map"].split(" (")[0], "dec": meta["mdec"]}
        out[key] = {"title": meta["title"].replace("&amp;", "&"), "date": s.get("date"), "updated_utc": s.get("updated_utc"), "source_time": s.get("source_time"), "spot": spot, "flip": s.get("gamma_flip"), "cw": s.get("call_wall"), "pw": s.get("put_wall"),
                    "atm_iv": s.get("atm_iv"), "day_move": s.get("day_move"), "pcr_oi": s.get("pcr_oi"), "pcr_vol": s.get("pcr_vol"), "dec": meta["dec"],
                    "net_gex_bn": s.get("net_gex_bn"), "max_pain": s.get("max_pain"), "front_move": s.get("front_move"), "king": s.get("king"),
                    "oi_changes": s.get("oi_changes") or [], "vol": s.get("vol") or {}, "history": s.get("history_full") or [],
                    "map": mapping, "surface": s["surface"]}
    vi = (data.get("options") or {}).get("_vol_idx")
    if out and vi is not None and len(vi) > 60:
        idx_map = {"SPX": ("VIXCLS", "VIX"), "NDX": ("VXNCLS", "VXN"), "DJX": ("VXDCLS", "VXD"), "RUT": ("RVXCLS", "RVX"), "GOLD": ("GVZCLS", "GVZ")}
        pay = {}
        for k, (col, name) in idx_map.items():
            if col not in vi:
                continue
            ser = vi[col].dropna()
            if len(ser) < 60:
                continue
            yr = ser[ser.index > ser.index[-1] - pd.Timedelta(days=365)]
            last = float(ser.iloc[-1])
            chg5 = float(last - ser.iloc[-6]) if len(ser) > 5 else None
            pct = float((yr < last).mean() * 100)
            t260 = ser.tail(260)
            pay[k] = {"name": name, "last": r(last, 2), "date": ser.index[-1].strftime("%Y-%m-%d"), "chg5": r(chg5, 2) if chg5 is not None else None,
                      "pct1y": r(pct, 0), "lo1y": r(float(yr.min()), 2), "hi1y": r(float(yr.max()), 2), "avg1y": r(float(yr.mean()), 2),
                      "dates": [d.strftime("%Y-%m-%d") for d in t260.index], "vals": [r(v, 2) for v in t260]}
        out["_volidx"] = pay
    vt = (data.get("options") or {}).get("_vix_term")
    if out and vt is not None and len(vt) > 20:
        t = vt.tail(260)
        out["_vix"] = {"dates": [d.strftime("%Y-%m-%d") for d in t.index], "vix": [r(v, 2) for v in t["VIXCLS"]], "vix3m": [r(v, 2) for v in t["VXVCLS"]],
                       "ratio": [r(v, 3) for v in t["VXVCLS"] / t["VIXCLS"]]}
    return out


def build_fingerprint(data: dict) -> str:
    """Regime fingerprint: today's market profile vs every past day, with what followed the closest matches.
    Features (z-scored over the whole history): 10y-2y curve, 10y real yield, VIX (log), and 21-day % change in
    the S&P 500, gold and DXY."""
    y, mk = data.get("yields", {}), data.get("mkt", {})
    need = {"curve": (y.get("DGS10") is not None and y.get("DGS2") is not None), "real": y.get("DFII10") is not None,
            "vix": mk.get("VIXCLS") is not None, "spx": mk.get("SP500") is not None, "gold": data.get("gold_px") is not None, "dxy": data.get("dxy") is not None}
    if not all(need.values()):
        return '<p class="quiet">Needs the curve, real yield, VIX, S&amp;P 500, gold and DXY series.</p>'
    idx = pd.bdate_range(end=max(s.index.max() for s in (mk["SP500"], data["gold_px"], data["dxy"])), periods=2700)
    al = lambda s: s.reindex(s.index.union(idx)).sort_index().ffill(limit=5).reindex(idx)
    spx, gold, dxy, vix = al(mk["SP500"]), al(data["gold_px"]), al(data["dxy"]), al(mk["VIXCLS"])
    F = pd.DataFrame({"10y-2y curve": al(y["DGS10"]) - al(y["DGS2"]), "10y real yield": al(y["DFII10"]), "VIX (log)": np.log(vix),
                      "S&P 500 21d %": spx.pct_change(21) * 100, "Gold 21d %": gold.pct_change(21) * 100, "DXY 21d %": dxy.pct_change(21) * 100}).dropna()
    if len(F) < 160:
        return '<p class="quiet">Not enough overlapping history yet.</p>'
    Z = (F - F.mean()) / F.std()
    today = Z.iloc[-1]
    cand = Z.iloc[:-63]
    dist = np.sqrt(((cand - today) ** 2).sum(axis=1)).sort_values()
    picks = []
    for d in dist.index:
        if all(abs((d - q).days) > 30 for q in picks):
            picks.append(d)
        if len(picks) == 5:
            break
    fwd = lambda s, d: (s.loc[s.index > d].iloc[20] / s.loc[d] - 1) * 100 if (s.index > d).sum() > 20 and pd.notna(s.loc[d]) else None
    n = len(today)
    rows, outs = [], {"S&P 500": [], "Gold": [], "DXY": []}
    for d in picks:
        sim = max(0.0, 1 - dist[d] / (2 * np.sqrt(n))) * 100
        res = {"S&P 500": fwd(spx, d), "Gold": fwd(gold, d), "DXY": fwd(dxy, d)}
        for k, v in res.items():
            if v is not None:
                outs[k].append(v)
        cell = lambda v: '<td class="muted-text">n/a</td>' if v is None else f'<td class="{"up" if v > 0 else "down"}">{v:+.2f}%</td>'
        rows.append(f'<tr><th scope="row">{d.strftime("%Y-%m-%d")}</th><td>{sim:.0f}%</td>' + "".join(cell(res[k]) for k in ("S&P 500", "Gold", "DXY")) + "</tr>")
    med = lambda a: (f'{np.median(a):+.2f}%' if a else "n/a")
    prof = "".join(f'<span class="fpz"><b>{esc(k)}</b> {F[k].iloc[-1]:+.2f} <i class="{"up" if v > 0.5 else "down" if v < -0.5 else "muted-text"}">z {v:+.1f}</i></span>' for k, v in today.items())
    return (f'<div class="fpprof">{prof}</div>'
            '<table class="opttab"><thead><tr><th>Closest past day</th><th>Similarity</th><th>S&amp;P 500 next 21d</th><th>Gold next 21d</th><th>DXY next 21d</th></tr></thead><tbody>'
            + "".join(rows) + f'<tr class="optnow"><th scope="row">Median of matches</th><td></td><td>{med(outs["S&P 500"])}</td><td>{med(outs["Gold"])}</td><td>{med(outs["DXY"])}</td></tr></tbody></table>'
            f'<p class="note">History used: {F.index[0].strftime("%b %Y")} to {F.index[-1].strftime("%d %b %Y")}, excluding the last 3 months. Five matches at least a month apart. '
            'What followed past look-alikes is context, not a forecast; five cases is a small sample.</p>')

def build_opt_stale(data: dict, today: date) -> str:
    """Banner for the options desk when a Cboe chain source (OPT_SPX/NDX/DJX/RUT/GOLD) failed its last check.
    Names each failing instrument with its own as-of date and age. Empty when all five are ok."""
    st = data.get("status") or {}
    opts = data.get("options") or {}
    bad = [k for k in OPT_META if isinstance((st.get(f"OPT_{k}")), dict) and st[f"OPT_{k}"].get("ok") is False]
    if not bad:
        return ""
    parts, n403 = [], 0
    for k in bad:
        err = str(st[f"OPT_{k}"].get("error") or "")
        n403 += bool(re.search(r"\b403\b", err))
        name = f"{re.sub('&amp;', '&', OPT_META[k]['title'])} ({OPT_META[k]['src'].replace(' options', '')})"
        s = opts.get(k) if isinstance(opts.get(k), dict) else {}
        raw = str(s.get("source_time") or s.get("date") or "")[:10]
        try:
            d = datetime.strptime(raw, "%Y-%m-%d").date()
            n = (today - d).days
            parts.append(f"{name} options data is as of {d.strftime('%d %b %Y')}" + (f", {n} day{'s' if n != 1 else ''} old" if n >= 0 else ""))
        except ValueError:
            parts.append(f"{name} has no saved options data yet")
    refused = n403 == len(bad)   # wording only when every failing source reports HTTP 403
    whole = len(bad) == len(OPT_META)
    head = "Options data is not updating." if whole else "Some options data is not updating."
    why = ("The data provider is currently refusing automated downloads" if refused else "They could not be refreshed on the latest update")
    tail = ("Levels below show where positioning stood on those dates, not today." if whole
            else "Other instruments passed their latest check; levels for the ones listed show where positioning stood on their date, not today.")
    return (f'<div class="optstale" id="opt-stale" role="status"><b>{head}</b> '
            + esc("; ".join(parts)) + f". {why}. {tail}</div>")


def build_options(data: dict, today: date) -> str:
    opts = data.get("options") or {}
    if not any(not str(k).startswith("_") for k in opts):
        return ('<div class="card" style="--w:12"><div class="card-b"><p class="quiet">No options data yet. It appears after the next scheduled update '
                '(weekdays 22:30 UTC) downloads the Cboe chains.</p></div></div>')
    html = []
    for key, meta in OPT_META.items():
        s = opts.get(key)
        if not s:
            continue
        conv = _opt_mapper(s, meta)
        spot = s["spot"]
        d0, dm = meta["dec"], meta["mdec"]

        def f(v, d):
            return "n/a" if v is None else f"{v:,.{d}f}"

        def dist(v):
            if v is None:
                return ""
            pct = (v / spot - 1) * 100
            return f'<span class="{"up" if pct > 0 else "down" if pct < 0 else "muted-text"}">{pct:+.2f}%</span>'
        fm = s.get("front_move") or {}
        lv = [("Call wall", s.get("call_wall"), "largest call gamma above price"),
              ("Expected move, high", fm.get("high"), f"front expiry {fm.get('expiry', '')} straddle"),
              ("Price", spot, s.get("date", "")),
              ("Gamma flip", s.get("gamma_flip"), "total gamma crosses zero"),
              ("Expected move, low", fm.get("low"), ""),
              ("Put wall", s.get("put_wall"), "largest put gamma below price"),
              ("Max pain", s.get("max_pain"), f"expiry {s.get('max_pain_expiry') or ''}")]
        lv = [x for x in lv if x[1] is not None]
        lv.sort(key=lambda x: -x[1])
        mapped_head = f"<th>{esc(meta['map'])}</th>" if conv else ""
        trs = "".join(f'<tr{" class=\"optnow\"" if n == "Price" else ""}><th scope="row">{n}</th><td>{f(v, d0)}</td>'
                      + (f"<td><b>{f(conv(v), dm)}</b></td>" if conv else "")
                      + f'<td>{"" if n == "Price" else dist(v)}</td><td class="muted-text">{esc(why)}</td></tr>' for n, v, why in lv)
        pos = s.get("regime") == "positive"
        badge_ = (f'<span class="badge" style="color:var(--up);background:color-mix(in srgb,var(--up) 14%,transparent)">Positive gamma</span>' if pos
                  else f'<span class="badge" style="color:var(--down);background:color-mix(in srgb,var(--down) 14%,transparent)">Negative gamma</span>')
        flip = s.get("gamma_flip")
        if flip:
            where = "above" if spot > flip else "below"
            read = (f"Price is {where} the gamma flip ({f(flip, d0)}{', ' + meta['map'] + ' ' + f(conv(flip), dm) if conv else ''}). "
                    + ("In this zone dealer hedging tends to lean against moves, so ranges between the walls have tended to hold more often and realised volatility tends to be lower."
                       if pos else "In this zone dealer hedging tends to push in the same direction as the move, so moves can extend further and volatility tends to be higher."))
        else:
            read = "No gamma flip within ±12% of price; total gamma is " + ("positive" if pos else "negative") + " across that range."
        dm_ = s.get("day_move")
        stats = (f'<div class="optstats"><span><b>{f(s.get("net_gex_bn"), 2)}</b> bn $ net gamma per 1%</span>'
                 f'<span>1-day move ±<b>{f(dm_, d0 if d0 else 0)}</b>{f" ({meta["map"]} ±{conv(dm_) - conv(0):,.{dm}f})" if conv and dm_ and meta["mode"] == "mul" else ""} <small>ATM IV {f(s.get("atm_iv"), 1)}%</small></span>'
                 f'<span>Put/call OI <b>{f(s.get("pcr_oi"), 2)}</b> · volume <b>{f(s.get("pcr_vol"), 2)}</b></span></div>')
        ch = s.get("oi_changes") or []
        if ch:
            chg = "".join(f'<tr><td>{f(c["k"], d0)}{f" <span class=muted-text>({f(conv(c["k"]), dm)})</span>" if conv else ""}</td>'
                          f'<td class="{"up" if c["dc"] > 0 else "down" if c["dc"] < 0 else ""}">{c["dc"]:+,}</td>'
                          f'<td class="{"up" if c["dp"] > 0 else "down" if c["dp"] < 0 else ""}">{c["dp"]:+,}</td></tr>' for c in ch)
            chg = f'<table class="opttab"><thead><tr><th>Strike</th><th>Call OI chg</th><th>Put OI chg</th></tr></thead><tbody>{chg}</tbody></table>'
        else:
            chg = '<p class="quiet">Needs a snapshot from an earlier day; shows from the next day&#39;s first update.</p>'
        hist = s.get("history") or []
        hrows = "".join(f'<tr><td>{esc(h.get("date"))}</td><td>{f(h.get("spot"), d0)}</td><td>{f(h.get("gamma_flip"), d0)}</td><td>{f(h.get("call_wall"), d0)}</td>'
                        f'<td>{f(h.get("put_wall"), d0)}</td><td>{f(h.get("net_gex_bn"), 2)}</td></tr>' for h in hist)
        htab = (f'<table class="opttab"><thead><tr><th>Date</th><th>Price</th><th>Flip</th><th>Call wall</th><th>Put wall</th><th>Net GEX bn</th></tr></thead><tbody>{hrows}</tbody></table>'
                if hrows else "")
        age = (today - datetime.strptime(s["date"], "%Y-%m-%d").date()).days if s.get("date") else 0
        stale = ' <span class="badge warn">stale</span>' if age > 4 else ""
        mapnote = (f"{meta['map']} levels = {meta['src'].split()[0]} level {'+ basis ' + format(s['mapped_price'] - spot, '+,.2f') if meta['mode'] == 'add' else '× ' + format(s['mapped_price'] / spot, ',.3f')} "
                   f"(last prices: {meta['map']} {f(s['mapped_price'], dm)}, {meta['src'].split()[0]} {f(spot, d0)}). The basis drifts during the day; re-check it against your chart."
                   if conv else "Mapped price unavailable this run, so levels are in option units only.")
        first = not html
        html.append(
            f'<details class="card collapse optgrp" style="--w:12"{" open" if first else ""}><summary class="card-h"><span class="ttl"><span class="chev"></span><h3>{meta["title"]} · level table, open-interest changes, history</h3></span>'
            f'<span class="note">{badge_}{stale}</span></summary><div class="grid optin">'
            f'<div class="card" style="--w:7"><div class="card-h"><h3>{meta["title"]} key levels <span class="muted-text">· {esc(meta["src"])}</span> {badge_}{stale}</h3></div>'
            f'<div class="card-b">{stats}<div class="scroll"><table class="opttab"><thead><tr><th>Level</th><th>{esc(meta["src"].split()[0])}</th>{mapped_head}<th>vs price</th><th></th></tr></thead><tbody>{trs}</tbody></table></div>'
            f'<p class="optread">{esc(read)}</p><p class="note">{esc(mapnote)}</p></div></div>'
            f'<div class="card" style="--w:5"><div class="card-h"><h3>{meta["title"]}: biggest open-interest changes</h3><p class="note">Contracts, vs the previous update</p></div><div class="card-b scroll">{chg}</div></div>'
            f'<div class="card" style="--w:12"><div class="card-h"><h3>{meta["title"]}: levels over the last 10 updates</h3><p class="note">In {esc(meta["src"].split()[0])} units</p></div><div class="card-b scroll">{htab or "<p class=quiet>Builds up one row per update.</p>"}</div></div></div></details>')
    return "".join(html)

TEMPLATE = (ROOT / "template.html").read_text(encoding="utf-8") if (ROOT / "template.html").exists() else None


def build_quick(playbook: dict, metrics: dict, fedq: str | None = None) -> str:
    """One-line regime reads for the overview: value plus the playbook band it falls in."""
    rows = []
    def add(label, value, band):
        if band:
            rows.append(f'<li><span class="ql">{esc(label)}</span><span class="qv">{esc(value)}</span><span class="qb">{esc(band["label"])}</span></li>')
    for nm in ("10y-2y", "10y-3m"):
        m = metrics.get(f"S:{nm}")
        if m:
            add(spread_title(nm), fmt(m["value"], 2) + " pp", pick_band(spread_spec(playbook, nm)["bands"], m["value"]))
    m = metrics.get("Y:DFII10")
    if m:
        add("10Y real yield", fmt(m["value"], 2) + "%", pick_band(playbook["real_yield"]["bands"], m["z"]))
    m = metrics.get("DXY")
    if m:
        add("DXY", fmt(m["value"], 2), pick_band(playbook["dxy"]["bands"], m["z"]))
    for key, label in (("gold", "Gold COT"), ("usd_index", "USD COT")):
        m = metrics.get(f"COT:{key}")
        if m:
            add(label, "idx " + fmt(m["index"], 0), pick_band(playbook["cot"][key]["bands"], m["index"]))
    if fedq:
        a, b, c = fedq.split("|")
        rows.insert(0, f'<li><span class="ql">{esc(a)}</span><span class="qv">{esc(b)}</span><span class="qb">{esc(c)}</span></li>')
    return f'<ul class="quick">{"".join(rows)}</ul>' if rows else ""


def econ_view(code: str, kind: str, s: pd.Series) -> tuple[pd.Series, int]:
    """The series as it is shown: % y/y for indexes and dollar amounts, the level otherwise (claims in thousands)."""
    gaps = s.index.to_series().diff().dt.days.dropna()
    step = gaps.median() if len(gaps) else 30
    ppy = 52 if step < 10 else (12 if step < 40 else 4)
    if kind == "yoy":
        return (100 * (s / s.shift(ppy) - 1)).dropna(), ppy
    if code in ("ICSA", "CCSA"):
        return s / 1000, ppy
    return s, ppy


def build_econ(data: dict) -> tuple[list[dict], dict, str]:
    rows, fam_series, table = [], {}, []
    for code, name, group, kind, what in ECON:
        raw = data.get("econ", {}).get(code)
        if raw is None:
            continue
        d, ppy = econ_view(code, kind, raw)
        if len(d) < 3:
            continue
        unit = {"yoy": "%", "rate": "%", "k": "k"}[kind]
        nd = 1 if kind != "k" else 0
        last, prev = d.iloc[-1], d.iloc[-2]
        y_ago = d.iloc[-1 - ppy] if len(d) > ppy else None
        win = d.tail(5 * ppy)
        z = num((last - win.mean()) / win.std(ddof=1)) if len(win) > 4 and win.std(ddof=1) else None
        freq = {52: "W", 12: "M", 4: "Q"}[ppy]
        per = raw.index[-1]
        period = per.strftime("%d %b %Y") if freq == "W" else (f"Q{(per.month - 1) // 3 + 1} {per.year}" if freq == "Q" else per.strftime("%b %Y"))
        lab, tone = z_band(z)
        rid = f"E:{code}"
        fam = ECON_FAMILY[kind]
        chg_nd = 2 if kind != "k" else 0
        rows.append({"id": rid, "group": group, "family": fam, "label": name, "ticker": code,
                     "cu": "k" if kind == "k" else "pp",        # unit of the change columns (pp of the shown % value, or thousands)
                     "latest": fmt(last, nd) + unit, "d1": fmt(num(last - prev), chg_nd, True),
                     "d252": "" if y_ago is None else fmt(num(last - y_ago), chg_nd, True), "z": fmt(z, 2, True), "period": period,
                     "info": {"title": f"{name} ({code})", "what": what + (" Shown as % change from a year earlier." if kind == "yoy" else ""),
                              "band": {"label": lab, "meaning": f"Latest reading is {fmt(z, 2, True)} standard deviations from its 5-year average." if z is not None else "", "so_what": ""},
                              "dyn": None, "z": z}})
        fam_series.setdefault(fam, {})[rid] = d
        # release table (raw levels)
        rl, rp = raw.iloc[-1], raw.iloc[-2]
        raw_ago = raw.iloc[-1 - ppy] if len(raw) > ppy else None
        rnd = 1 if raw.abs().median() < 1000 else 0
        heat = "" if z is None else f' class="heat {"neg" if z < 0 else "pos"}" style="--i:{min(100, round(abs(z) / 2.5 * 100))}"'
        table.append(f'<tr><th scope="row">{esc(name)}<small>{esc(code)}</small></th><td class="lefttd">{esc(period)}</td>'
                     f'<td>{esc(fmt(num(rl), rnd))}</td><td>{esc(fmt(num(rp), rnd))}</td>'
                     f'<td class="{chg_class(fmt(num(rl - rp), rnd, True))}">{esc(fmt(num(rl - rp), rnd, True))}</td>'
                     f'<td>{esc("" if raw_ago is None else fmt(num(100 * (rl / raw_ago - 1)), 1, True) + "%")}</td>'
                     f'<td>{esc(fmt(num(win.mean()), nd))}{unit}</td><td{heat}>{esc(fmt(z, 2, True))}</td>'
                     f'<td>{f"<span class='pill {tone}'>{esc(lab)}</span>" if lab else ""}</td></tr>')
    fams = {}
    for fam, ser in fam_series.items():
        frame = pd.concat(ser, axis=1).sort_index()
        fams[fam] = {"dates": [x.strftime("%Y-%m-%d") for x in frame.index],
                     "series": {k: [r(v, 3) for v in frame[k]] for k in frame.columns}}
    html_table = ('<table class="matrix etab"><thead><tr><th>Series</th><th>Period</th><th>Latest</th><th>Prior</th><th>Change</th>'
                  '<th>Y/Y % <small>of published level</small></th><th>5Y avg <small>(as charted)</small></th><th>Z (5y)</th><th>Reading</th></tr></thead><tbody>' + "".join(table) + "</tbody></table>")
    return rows, fams, html_table


def build_econ_watch_html(rows: list[dict]) -> str:
    cols = ('<th class="cb"><span class="sr-only">Select</span></th><th>Name</th><th class="c-tk">Ticker</th><th>Latest</th>'
            '<th>Chg</th><th class="c-d252">1Y chg</th><th class="c-per">Period</th><th class="c-z">Z</th>')
    out = [f'<table class="watch econ"><thead><tr>{cols}</tr></thead>']
    groups: list[str] = []
    for r_ in rows:
        if r_["group"] not in groups:
            groups.append(r_["group"])
    for g in groups:
        out.append(f'<tbody data-group="{esc(g)}"><tr class="grp"><td class="cb"><span class="gbox" role="checkbox" aria-checked="false" tabindex="0" aria-label="Plot {esc(g)}"></span></td>'
                   f'<th colspan="6"><button type="button" class="gt" aria-expanded="true"><span class="car"></span>{esc(g)}</button></th>'
                   f'<td class="gmove"><button type="button" class="mv up" aria-label="Move {esc(g)} up">&#9650;</button><button type="button" class="mv dn" aria-label="Move {esc(g)} down">&#9660;</button></td></tr>')
        for r_ in (x for x in rows if x["group"] == g):
            out.append(
                f'<tr class="row" tabindex="0" data-id="{esc(r_["id"])}" data-family="{esc(r_["family"])}">'
                f'<td class="cb"><span class="pick" aria-hidden="true"></span></td>'
                f'<th scope="row"><span class="plus">+</span>{esc(r_["label"])}</th><td class="c-tk tk"><span class="plus">+</span>{esc(r_["ticker"])}</td>'
                f'<td class="yd">{esc(r_["latest"])}</td><td class="{chg_class(r_["d1"])}">{esc(r_["d1"])}<small class="cu">{r_["cu"]}</small></td>'
                f'<td class="c-d252 {chg_class(r_["d252"])}">{esc(r_["d252"])}{"<small class=cu>" + r_["cu"] + "</small>" if r_["d252"] else ""}</td><td class="c-per tk">{esc(r_["period"])}</td><td class="c-z">{esc(r_["z"])}</td></tr>')
        out.append("</tbody>")
    out.append("</table>")
    return "".join(out)


FED_OUT = ["C26", "C25", "H0", "H25", "H26"]
FED_NAMES = {"C26": "Cut >25bp", "C25": "Cut 25bp", "H0": "Hold", "H25": "Hike 25bp", "H26": "Hike >25bp"}


def fed_norm(raw: dict) -> dict | None:
    vals = {k: float(raw[k]) for k in FED_OUT if raw.get(k) is not None and not (isinstance(raw[k], float) and math.isnan(raw[k]))}
    tot = sum(vals.values())
    if tot <= 0:
        return None
    p = {k: 100 * vals.get(k, 0) / tot for k in FED_OUT}
    p["cut"], p["hold"], p["hike"] = p["C26"] + p["C25"], p["H0"], p["H25"] + p["H26"]
    return p


def fed_hist_norm(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reindex(columns=FED_OUT).ffill()
    tot = df.sum(axis=1).replace(0, np.nan)
    out = df.div(tot, axis=0) * 100
    out["cut"], out["hold"], out["hike"] = out["C26"] + out["C25"], out["H0"], out["H25"] + out["H26"]
    return out.dropna(subset=["hold"])


def build_fed(data: dict, today: date) -> tuple[dict, dict | None, str | None]:
    fed = data.get("fed")
    empty = '<p class="muted-text pad">No Fed odds saved yet. They appear after the next data update.</p>'
    out = {"next": empty, "table": empty}
    if not fed or not fed.get("meetings"):
        return out, None, None
    meets = []
    for m in fed["meetings"]:
        p = fed_norm(m["raw"])
        if p:
            d = datetime.strptime(m["date"], "%Y-%m-%d").date()
            meets.append({**m, "p": p, "d": d, "label": d.strftime("%b %Y"), "long": d.strftime("%d %b %Y"), "short": f"{d.day} {d.strftime('%b')}"})
    if not meets:
        return out, None, None
    hists = {e: fed_hist_norm(h) for e, h in fed.get("hist", {}).items() if len(h)}

    def ago(event, days, key):
        h = hists.get(event)
        if h is None or not len(h):
            return None
        cut = pd.Timestamp(today) - pd.Timedelta(days=days)
        h2 = h[h.index <= cut]
        return num(h2[key].iloc[-1]) if len(h2) else None

    nx = meets[0]
    lean = max(("cut", "hold", "hike"), key=lambda k: nx["p"][k])
    rows = []
    for key, lab, cls in (("cut", "Cut", "down"), ("hold", "Hold", "flat"), ("hike", "Hike", "up")):
        v = nx["p"][key]
        cells = ""
        for days in (1, 7, 30):
            a = ago(nx["event"], days, key)
            if a is None:
                cells += '<td class="muted-text">n/a</td>'
            else:
                ch = v - a
                cells += f'<td><span class="was">{a:.0f}%</span> <span class="{"up" if ch > 0.5 else "down" if ch < -0.5 else "muted-text"}">{int(round(ch)):+d}</span></td>'
        rows.append(f'<tr><th scope="row"><span class="fdot {cls}"></span>{lab}</th><td class="fbar"><div class="bar {cls}" style="--v:{v:.1f}%"><b>{v:.0f}%</b></div></td>{cells}</tr>')
    days_to = (nx["d"] - today).days
    p = nx["p"]
    stack = ('<div class="stack3" role="img" aria-label="Cut {:.0f}%, hold {:.0f}%, hike {:.0f}%">'.format(p["cut"], p["hold"], p["hike"])
             + "".join(f'<span class="seg {k}" style="flex-basis:{p[k]:.2f}%" title="{k.capitalize()} {p[k]:.0f}%">{(k.capitalize() + " " + format(p[k], ".0f") + "%") if p[k] >= 9 else ""}</span>' for k in ("cut", "hold", "hike"))
             + '</div><div class="stacklegend">' + "".join(f'<span><i class="seg {k}"></i>{k.capitalize()} {p[k]:.0f}%</span>' for k in ("cut", "hold", "hike")) + "</div>")
    out["next"] = (f'<div class="fnext"><div class="fmeet"><div class="kl">Next FOMC decision</div><div class="kv">{esc(nx["long"])}</div>'
                   f'<div class="kc">{"today" if days_to == 0 else f"in {days_to} days" if days_to > 0 else ""} · market leans <b>{lean.capitalize()} {nx["p"][lean]:.0f}%</b></div></div>{stack}'
                   '<table class="ftab"><thead><tr><th></th><th>Odds now</th><th>1 day ago <small>(chg pp)</small></th><th>1 week ago</th><th>1 month ago</th></tr></thead><tbody>'
                   + "".join(rows) + "</tbody></table></div>")
    trs = []
    for m in meets[:8]:
        p = m["p"]
        cells = "".join(f'<td class="heat pos" style="--i:{min(100, round(p[k]))}">{p[k]:.0f}%</td>' for k in FED_OUT)
        w, mo = ago(m["event"], 7, "hike"), ago(m["event"], 30, "hike")
        w2, mo2 = ago(m["event"], 7, "cut"), ago(m["event"], 30, "cut")
        lk = max(FED_OUT, key=lambda k: p[k])
        fmt_ch = lambda now, then: "n/a" if then is None else f'{then:.0f}% <span class="{"up" if now - then > 0.5 else "down" if now - then < -0.5 else "muted-text"}">({int(round(now - then)):+d})</span>'
        trs.append(f'<tr><th scope="row">{esc(m["label"])} <span class="muted-text">&middot; {esc(m["short"])}</span></th>{cells}'
                   f'<td>{p["hike"]:.0f}%</td><td>{fmt_ch(p["hike"], w)}</td><td>{fmt_ch(p["hike"], mo)}</td>'
                   f'<td>{p["cut"]:.0f}%</td><td>{fmt_ch(p["cut"], w2)}</td><td>{fmt_ch(p["cut"], mo2)}</td><td class="lefttd"><b>{FED_NAMES[lk]}</b></td></tr>')
    out["table"] = ('<table class="matrix ftable"><thead><tr><th>Meeting</th>' + "".join(f"<th>{FED_NAMES[k]}</th>" for k in FED_OUT)
                    + '<th>Hike total</th><th>Hike 1W ago</th><th>Hike 1M ago</th><th>Cut total</th><th>Cut 1W ago</th><th>Cut 1M ago</th><th>Most likely</th></tr></thead><tbody>'
                    + "".join(trs) + "</tbody></table>")
    pay = {"meetings": [], "hist": {}}
    for m in meets[:4]:
        h = hists.get(m["event"])
        if h is None or not len(h):
            continue
        pay["meetings"].append({"event": m["event"], "label": m["label"]})
        pay["hist"][m["event"]] = {"dates": [x.strftime("%Y-%m-%d") for x in h.index],
                                   "cut": [r(v, 1) for v in h["cut"]], "hold": [r(v, 1) for v in h["hold"]], "hike": [r(v, 1) for v in h["hike"]]}
    quick = f'Next FOMC ({nx["d"].strftime("%d %b")})|{lean.capitalize()} {nx["p"][lean]:.0f}%|Hike {nx["p"]["hike"]:.0f}% · Cut {nx["p"]["cut"]:.0f}%'
    return out, pay, quick


ZQ_BIG_MOVE = 10.0     # percentage points in one day = "big move" marker


def build_zq(data: dict, today: date) -> tuple[dict, dict | None]:
    """Fed Rate Odds (30-Day Fed Funds futures) block: next-meeting card, meeting table, chart payload with big-move markers."""
    empty = '<p class="emptystate">No futures-based odds saved yet. They appear after the next data update (needs EFFR, the FOMC calendar and ZQ prices).</p>'
    out = {"next": empty, "table": empty, "upd": "n/a"}
    z = data.get("zq")
    if z is None or z.empty:
        return out, None
    z = z.copy()
    z = z[z["meeting_date"] >= today.isoformat()]
    if z.empty:
        return out, None
    last_day = z["snapshot_date"].max()
    cur = z[z["snapshot_date"] == last_day].sort_values("meeting_date")
    out["upd"] = last_day

    def pp(v):
        v = num(v)
        return '<span class="muted-text">n/a</span>' if v is None else f'<span class="{"up" if v > 0.5 else "down" if v < -0.5 else "muted-text"}">{v:+.1f}</span>'

    nx = cur.iloc[0]
    md = datetime.strptime(nx["meeting_date"], "%Y-%m-%d").date()
    days_to = (md - today).days
    p = {"cut": float(nx["cut_probability"]), "hold": float(nx["hold_probability"]), "hike": float(nx["hike_probability"])}
    lean = max(p, key=p.get)
    stack = ('<div class="stack3" role="img" aria-label="Cut {:.0f}%, hold {:.0f}%, hike {:.0f}%">'.format(p["cut"], p["hold"], p["hike"])
             + "".join(f'<span class="seg {k}" style="flex-basis:{p[k]:.2f}%" title="{k.capitalize()} {p[k]:.0f}%">{(k.capitalize() + " " + format(p[k], ".0f") + "%") if p[k] >= 9 else ""}</span>' for k in ("cut", "hold", "hike"))
             + '</div><div class="stacklegend">' + "".join(f'<span><i class="seg {k}"></i>{k.capitalize()} {p[k]:.0f}%</span>' for k in ("cut", "hold", "hike")) + "</div>")
    rows = [("Contract used", esc(nx["zq_contract"])), ("ZQ price", f'{float(nx["zq_price"]):.3f}'), ("Current EFFR", f'{float(nx["current_effr"]):.2f}%'),
            ("Implied average rate", f'{float(nx["implied_average_rate"]):.3f}%'), ("Implied post-meeting rate", f'{float(nx["implied_post_meeting_rate"]):.3f}%'),
            ("Implied rate change (+ = cut, − = hike)", f'{float(nx["implied_rate_change_bp"]):+.1f} bp'),
            ("Cut odds, 1 day / 1 week", f'{pp(nx["cut_probability_change_1d"])} / {pp(nx["cut_probability_change_1w"])} pp'),
            ("Hike odds, 1 day / 1 week", f'{pp(nx["hike_probability_change_1d"])} / {pp(nx["hike_probability_change_1w"])} pp')]
    dl = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in rows)
    note = f'<p class="note">{esc(nx["note"])}</p>' if isinstance(nx.get("note"), str) and nx["note"] else ""
    out["next"] = (f'<div class="fmeet"><div class="kl">Next FOMC decision</div><div class="kv">{md.day} {md.strftime("%b %Y")}</div>'
                   f'<div class="kc">{"today" if days_to == 0 else f"in {days_to} days"} · futures lean <b>{lean.capitalize()} {p[lean]:.0f}%</b></div></div>'
                   f'{stack}<dl>{dl}</dl>{note}')
    heads = ["Meeting", "Contract", "ZQ price", "EFFR", "Implied avg", "Rate into meeting", "Implied post-meeting", "Change <small>bp, + cut / − hike</small>",
             "Cut 25bp", "Hold", "Hike 25bp", "Cut 1D", "Hike 1D", "Cut 1W", "Hike 1W"]
    trs = []
    for _, r_ in cur.iterrows():
        d = datetime.strptime(r_["meeting_date"], "%Y-%m-%d").date()
        flag = ' <span class="badge warn" title="' + esc(r_["note"]) + '">&gt;25 bp</span>' if str(r_.get("large_move_flag")) in ("True", "true", "1") else ""
        cells = (f'<td>{esc(r_["zq_contract"])}</td><td>{float(r_["zq_price"]):.3f}</td><td>{float(r_["current_effr"]):.2f}%</td>'
                 f'<td>{float(r_["implied_average_rate"]):.3f}%</td><td>{float(r_["pre_meeting_rate"]):.3f}%</td><td>{float(r_["implied_post_meeting_rate"]):.3f}%</td>'
                 f'<td>{float(r_["implied_rate_change_bp"]):+.1f}{flag}</td>'
                 + "".join(f'<td class="heat pos" style="--i:{min(100, round(float(r_[k])))}">{float(r_[k]):.0f}%</td>' for k in ("cut_probability", "hold_probability", "hike_probability"))
                 + "".join(f"<td>{pp(r_[k])}</td>" for k in ("cut_probability_change_1d", "hike_probability_change_1d", "cut_probability_change_1w", "hike_probability_change_1w")))
        note = f' title="{esc(r_["note"])}"' if isinstance(r_.get("note"), str) and r_["note"] else ""
        trs.append(f'<tr{note}><th scope="row">{d.strftime("%b %Y")} <span class="muted-text">&middot; {d.day} {d.strftime("%b")}</span></th>{cells}</tr>')
    out["table"] = ('<table class="matrix ftable zqtable"><thead><tr>' + "".join(f"<th>{h}</th>" for h in heads) + "</tr></thead><tbody>"
                    + "".join(trs) + "</tbody></table>")

    # chart payload: next 4 meetings, cut and hike odds over time, plus big-move markers labelled with same-day events
    ev = data.get("events")
    evmap: dict[str, list[str]] = {}
    if ev is not None:
        for _, e in ev.iterrows():
            evmap.setdefault(str(e["date"]), []).append(str(e["event"]))
    meets = list(cur["meeting_date"])[:4]
    hist = z[z["meeting_date"].isin(meets)]
    dates = sorted(hist["snapshot_date"].unique())
    pay = {"dates": dates, "meetings": [], "marks": []}
    big = {}
    for mi, mdate in enumerate(meets):
        g = hist[hist["meeting_date"] == mdate].set_index("snapshot_date")
        d = datetime.strptime(mdate, "%Y-%m-%d").date()
        pay["meetings"].append({"label": f'{d.strftime("%b %Y")}', "cut": [r(g["cut_probability"].get(x), 1) for x in dates],
                                "hike": [r(g["hike_probability"].get(x), 1) for x in dates]})
        for x, row in (g.iterrows() if mi == 0 else []):     # markers follow the next meeting, the most liquid contract
            for k in ("cut_probability_change_1d", "hike_probability_change_1d"):
                v = num(row.get(k))
                if v is not None and abs(v) >= ZQ_BIG_MOVE:
                    big.setdefault(x, []).append(f'{d.strftime("%b")} {"cut" if k.startswith("cut") else "hike"} {v:+.0f} pp')
    for x in sorted(big):
        order = ("FOMC", "CPI", "Jobs", "PCE", "Fed")
        evs = sorted(evmap.get(x, []), key=lambda e: next((i for i, o in enumerate(order) if e.startswith(o)), 9))
        short = {"FOMC": "FOMC", "CPI": "CPI", "Jobs": "Jobs", "PCE": "PCE", "Fed": "Fed"}
        pay["marks"].append({"date": x, "label": next((short[o] for o in order if evs and evs[0].startswith(o)), "") if evs else "", "title": f'{x}: ' + ", ".join(big[x]) + (f' ({", ".join(evs)})' if evs else "")})
    return out, pay


def read_fx_feed(data_dir: Path) -> dict | None:
    p = data_dir / "fx_macro.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def render(playbook: dict, data: dict, computed: dict, today: date, feed: dict | None) -> str:
    metrics = computed["metrics"]
    fired = eval_cross_signals(playbook, metrics)
    rows, fam = build_watch(playbook, data, computed)
    erows, efam, etable = build_econ(data)
    fedh, fedp, fedq = build_fed(data, today)
    zqh, zqp = build_zq(data, today)
    fam.update(efam)
    fx = build_fx(feed, data)
    fam["fx"] = fx_family(data)
    irows = fx.get("irows", [])
    cot_t = build_cot_tables(data)
    cot_cards = build_cot_cards(playbook, metrics, split=True)

    if fired:
        sig_html = "".join(f'<li><strong>{esc(s["label"])}</strong><span>{esc(s["meaning"])}</span></li>' for s in fired)
        sig_html = f'<ul class="signals">{sig_html}</ul>'
    else:
        sig_html = '<p class="quiet">No cross-indicator signals active right now.</p>'
    sig_html = build_quick(playbook, metrics, fedq) + sig_html

    dates = [m["date"] for m in metrics.values() if m.get("date")]
    as_of = max(dates) if dates else "n/a"
    default_sel = [x for x in ("S:10y-2y", "S:10y-3m", "S:30y-5y", "S:30y-10y", "S:5y-2y") if any(r_["id"] == x for r_ in rows)]
    payload = {"families": fam, "rows": {r_["id"]: {"label": r_["label"], "family": r_.get("family", "fx"), "ticker": r_.get("ticker", ""), "info": r_["info"]} for r_ in rows + erows + irows},
               "fxm": {"horizons": ["Now", "1D", "1W", "1M", "3M", "6M", "1Y"], "bars": [1, 1, 5, 21, 63, 126, 252],
                       "cols": [{"id": r_["id"], "sym": r_["label"], "name": r_["ticker"], "group": r_["group"], "score": r_["score"], "lean": r_["lean"], "range": r_["range"],
                                 "dec": 3 if r_["label"] == "USDJPY" else 2 if r_["label"] in INSTR_NAMES else 5} for r_ in irows]},
               "fxdefault": [x for x in ("FX:EURUSD", "FX:USDJPY", "FX:XAUUSD", "FX:ES", "FX:NQ") if any(r_["id"] == x for r_ in irows)],
               "edefault": [x for x in ("E:CPIAUCSL", "E:CPILFESL", "E:PCEPI", "E:PCEPILFE") if any(e["id"] == x for e in erows)],
               "default": default_sel, "fed": fedp, "zq": zqp, "matrix": matrix_payload(computed, data), "curve": curve_payload(data), "cot": build_cot_charts(data), "opt": opt_payload(data),
               "dec": {"fx": 2, "spreads": 3, "yields": 3, "dxy": 2, "gold": 2, "e_yoy": 2, "e_rate": 1, "e_k": 0},
               "unit": {"fx": "%", "spreads": " pp", "yields": "%", "dxy": "", "gold": "", "e_yoy": "%", "e_rate": "%", "e_k": "k"}}

    page = TEMPLATE
    rep = {
        "__ASOF__": esc(as_of), "__BUILT__": esc(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        "__TILES__": fx["tiles"], "__SIGNALS__": sig_html, "__WATCHLIST__": build_watch_html(rows), "__MATRIX__": build_matrix(data, computed),
        "__COT_CARDS__": build_cot_cards(playbook, metrics), "__COT_GOLD_TABLE__": cot_t["gold"], "__COT_USD_TABLE__": cot_t["usd_index"],
        "__KPIS__": build_kpis(data, computed), "__COT_GOLD_CARD__": cot_cards[0], "__COT_USD_CARD__": cot_cards[1],
        "__FXWATCH__": build_instr_watch_html(irows), "__FXMATRIX__": build_drivers_matrix(irows, fx.get("drivers", {})), "__WEEK__": build_week(data), "__CONFLUENCE__": fx["conf"], "__FXPICK__": fx.get("picker", ""), "__ECON__": fx["econ"], "__EWATCH__": build_econ_watch_html(erows), "__FED_NEXT__": fedh["next"], "__ZQ_NEXT__": zqh["next"], "__ZQ_TABLE__": zqh["table"], "__ZQ_UPD__": esc(zqh["upd"]), "__FED_TABLE__": fedh["table"],
        "__FED_UPD__": esc((data.get("fed") or {}).get("updated", "n/a")), "__ETABLE__": etable, "__NEWS__": fx["news"], "__FXUPD__": esc(fx["upd"]), "__NEWSCHK__": build_news_checked(feed),
        "__STATUS__": build_status(data, today), "__FOOTER__": build_footer(data), "__OPTIONS__": build_options(data, today), "__OPTSTALE__": build_opt_stale(data, today), "__FINGERPRINT__": build_fingerprint(data),
    }
    for k, v in rep.items():
        page = page.replace(k, v)
    return page.replace("__PAYLOAD__", json.dumps(payload, separators=(",", ":")).replace("</", "<\\/"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "index.html"))
    ap.add_argument("--playbook", default=str(ROOT / "playbook.json"))
    ap.add_argument("--today", default=None, help="override today's date (YYYY-MM-DD) for staleness checks")
    a = ap.parse_args()

    if TEMPLATE is None:
        raise SystemExit("template.html is missing")
    playbook = json.loads(Path(a.playbook).read_text(encoding="utf-8"))
    data = load_everything(Path(a.data_dir))
    if not data["yields"] and data["dxy"] is None and not any(data["cot"].values()):
        raise SystemExit(f"No data found in {a.data_dir}")
    computed = compute(data, playbook)
    today = datetime.strptime(a.today, "%Y-%m-%d").date() if a.today else datetime.now(timezone.utc).date()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(playbook, data, computed, today, read_fx_feed(Path(a.data_dir))), encoding="utf-8")
    try:   # Excel copy of every number on the page; the page itself never depends on this
        import export_excel
        export_excel.export(out.parent / "Macro_Dashboard.xlsx", playbook, data, computed)
        print(f"wrote {out.parent / 'Macro_Dashboard.xlsx'}")
    except Exception as exc:  # noqa: BLE001
        print(f"WARN Excel export skipped: {type(exc).__name__}: {exc}")
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB); metrics: {', '.join(sorted(computed['metrics']))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
