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
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DAILY_WINDOW = 252
COT_WINDOW = 52

YIELD_CODES = ["DGS3MO", "DGS2", "DGS5", "DGS10", "DGS20", "DGS30", "DFII10"]
TENORS = [("3M", "DGS3MO"), ("2Y", "DGS2"), ("5Y", "DGS5"), ("10Y", "DGS10"), ("20Y", "DGS20"), ("30Y", "DGS30")]
SPREADS = {  # name: (long leg, short leg)
    "10y-2y": ("DGS10", "DGS2"),
    "10y-3m": ("DGS10", "DGS3MO"),
    "20y-10y": ("DGS20", "DGS10"),
    "30y-5y": ("DGS30", "DGS5"),
}
COT_FILES = {
    "gold": ["COT_GOLD.csv", "CFTC_Gold_52_Weeks.csv"],
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
def load_everything(data_dir: Path) -> dict:
    yields = {c: s for c in YIELD_CODES if (s := read_series(data_dir / f"{c}.csv", "date", c)) is not None}

    dxy = read_series(data_dir / "DXY.csv", "Date", "DXY")
    if dxy is None:
        dxy = read_series(data_dir / "DXY_52_Weeks.csv", "Date", "DXY")

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
    return {"yields": yields, "dxy": dxy, "gold_px": gold_px, "cot": cot, "status": status}


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
        f'<article class="card"><h3>{esc(title)}</h3>'
        f'<div class="big">{esc(big)}<span class="sub">{esc(sub)}</span></div>'
        f"{gauge_html}<dl>{rows}</dl>{band_html}{dyn_html}"
        f'<details><summary>What is this?</summary><p>{esc(what)}</p></details></article>'
    )


def build_cards(playbook: dict, metrics: dict) -> dict[str, str]:
    out: dict[str, list[str]] = {"spreads": [], "levels": [], "cot": []}

    for name, spec in playbook["spreads"].items():
        m = metrics.get(f"S:{name}")
        if not m:
            out["spreads"].append(
                f'<article class="card muted"><h3>{esc(spec["title"])}</h3>'
                f'<p>Waiting for data. This spread appears after the first successful data update.</p></article>')
            continue
        band = pick_band(spec["bands"], m["value"])
        dyn = playbook["dynamics"]["types"].get(m.get("dynamics") or "")
        zb = pick_band(playbook["zscore"]["bands"], m["z"])
        out["spreads"].append(card(
            spec["title"], f'{fmt(m["value"], 2)} pp', f' as of {m["date"]}', band,
            [("5D change", f'{fmt(m["chg5"], 2, True)} pp'), ("20D change", f'{fmt(m["chg20"], 2, True)} pp'),
             ("Z-score (1y)", f'{fmt(m["z"], 2, True)}' + (f' · {zb["label"]}' if zb else ""))],
            spec["what"], dyn))

    ry = metrics.get("Y:DFII10")
    if ry:
        spec = playbook["real_yield"]
        band = pick_band(spec["bands"], ry["z"])
        out["levels"].append(card(
            spec["title"], f'{fmt(ry["value"], 2)}%', f' as of {ry["date"]}', band,
            [("5D change", f'{fmt(ry["chg5"], 2, True)} pp'), ("20D change", f'{fmt(ry["chg20"], 2, True)} pp'),
             ("Z-score (1y)", fmt(ry["z"], 2, True))], spec["what"]))
    dx = metrics.get("DXY")
    if dx:
        spec = playbook["dxy"]
        band = pick_band(spec["bands"], dx["z"])
        out["levels"].append(card(
            spec["title"], fmt(dx["value"], 2), f' as of {dx["date"]}', band,
            [("5D change", f'{fmt(dx["pct5"], 2, True)}%'), ("20D change", f'{fmt(dx["pct20"], 2, True)}%'),
             ("Z-score (1y)", fmt(dx["z"], 2, True))], spec["what"]))
    for code, label in (("DGS2", "US 2Y yield"), ("DGS10", "US 10Y yield")):
        m = metrics.get(f"Y:{code}")
        if m:
            zb = pick_band(playbook["zscore"]["bands"], m["z"])
            out["levels"].append(card(
                label, f'{fmt(m["value"], 2)}%', f' as of {m["date"]}',
                {"label": zb["label"], "meaning": f'{label} is {zb["label"].lower()} (z-score {fmt(m["z"], 2, True)}).',
                 "so_what": "Use the level together with the spreads above: a rising yield with a widening spread reads differently from a rising yield with a flat curve."} if zb else None,
                [("5D change", f'{fmt(m["chg5"], 2, True)} pp'), ("20D change", f'{fmt(m["chg20"], 2, True)} pp')],
                f"{label} from FRED ({code})."))

    for key in ("usd_index", "gold"):
        spec = playbook["cot"][key]
        m = metrics.get(f"COT:{key}")
        if not m:
            out["cot"].append(
                f'<article class="card muted"><h3>{esc(spec["title"])}</h3>'
                f'<p>No data yet. It appears after the first successful CFTC update.</p></article>')
            continue
        band = pick_band(spec["bands"], m["index"])
        out["cot"].append(card(
            spec["title"], f'{fmt(m["net"], 0, True)}', f' net contracts · {m["date"]}', band,
            [("Net change, week", f'{fmt(m["wchg"], 0, True)}'),
             ("Longs / shorts change", f'{fmt(m["long_chg"], 0, True)} / {fmt(m["short_chg"], 0, True)}'),
             ("Open interest", f'{fmt(m["oi"], 0)} ({fmt(m["oi_chg"], 0, True)})'),
             ("Net % of open interest", f'{fmt(m["net_pct"], 1, True)}%'),
             ("Net % of OI, 0-100 (52w)", fmt(m["net_oi_idx"], 1)),
             ("COT Index (0-100)", fmt(m["index"], 1)),
             ("Z-score (52w)", fmt(m["z"], 2, True)),
             ("Friday close, week change", f'{fmt(m["price"], 2)} ({fmt(m["price_chg"], 2, True)}%)')],
            spec["what"], gauge_html=gauge(m["index"])))
    return {k: "\n".join(v) for k, v in out.items()}



def build_summary(playbook: dict, metrics: dict) -> str:
    """One compact table with every headline reading (the notebook's summary table, plus a plain-English label)."""
    rows: list[tuple] = []

    def add(name, latest, c5, c20, z, label):
        rows.append((name, latest, c5, c20, z, label))

    for name, spec in playbook["spreads"].items():
        m = metrics.get(f"S:{name}")
        if m:
            b = pick_band(spec["bands"], m["value"])
            add(spec["title"] + " spread", f'{fmt(m["value"], 2)} pp', fmt(m["chg5"], 2, True), fmt(m["chg20"], 2, True),
                fmt(m["z"], 2, True), b["label"] if b else "")
    for code, label in (("DGS2", "US 2Y yield"), ("DGS10", "US 10Y yield")):
        m = metrics.get(f"Y:{code}")
        if m:
            b = pick_band(playbook["zscore"]["bands"], m["z"])
            add(label, f'{fmt(m["value"], 2)}%', fmt(m["chg5"], 2, True), fmt(m["chg20"], 2, True), fmt(m["z"], 2, True), b["label"] if b else "")
    m = metrics.get("Y:DFII10")
    if m:
        b = pick_band(playbook["real_yield"]["bands"], m["z"])
        add("10Y real yield", f'{fmt(m["value"], 2)}%', fmt(m["chg5"], 2, True), fmt(m["chg20"], 2, True), fmt(m["z"], 2, True), b["label"] if b else "")
    m = metrics.get("DXY")
    if m:
        b = pick_band(playbook["dxy"]["bands"], m["z"])
        add("DXY", fmt(m["value"], 2), fmt(m["pct5"], 2, True) + "%", fmt(m["pct20"], 2, True) + "%", fmt(m["z"], 2, True), b["label"] if b else "")
    for key, label in (("usd_index", "USD Index COT"), ("gold", "Gold COT")):
        m = metrics.get(f"COT:{key}")
        if m:
            b = pick_band(playbook["cot"][key]["bands"], m["index"])
            add(label + " (net)", fmt(m["net"], 0, True), "wk " + fmt(m["wchg"], 0, True), "idx " + fmt(m["index"], 0),
                fmt(m["z"], 2, True), b["label"] if b else "")
    if not rows:
        return ""
    body = "".join(
        f'<tr><th scope="row">{esc(n)}</th><td>{esc(a)}</td><td>{esc(b)}</td><td>{esc(c)}</td><td>{esc(d)}</td>'
        f'<td>{badge(e, tone_for(e))}</td></tr>' for n, a, b, c, d, e in rows)
    return ('<div class="scroll"><table class="summary"><thead><tr><th>Indicator</th><th>Latest</th><th>5D</th><th>20D</th>'
            '<th>Z-score</th><th>Reading</th></tr></thead><tbody>' + body + "</tbody></table></div>")


def build_chart_data(data: dict, spread_series: dict[str, pd.Series]) -> dict:
    charts: dict = {}

    # Curve snapshot: now vs ~1 month vs ~1 year ago
    cols = [(lab, code) for lab, code in TENORS if code in data["yields"]]
    if len(cols) >= 3:
        frame = pd.concat({lab: data["yields"][code] for lab, code in cols}, axis=1).dropna()
        if len(frame) > 1:
            def at(offset):
                i = max(0, len(frame) - 1 - offset)
                return frame.index[i].strftime("%Y-%m-%d"), [r(v, 3) for v in frame.iloc[i].tolist()]
            now_d, now_v = at(0)
            m_d, m_v = at(20)
            y_d, y_v = at(DAILY_WINDOW - 1)
            charts["curve"] = {"labels": [c[0] for c in cols],
                               "sets": [{"name": f"Now ({now_d})", "data": now_v},
                                        {"name": f"~1 month ago ({m_d})", "data": m_v},
                                        {"name": f"~1 year ago ({y_d})", "data": y_v}]}

    if spread_series:
        df = pd.concat(spread_series, axis=1).tail(DAILY_WINDOW)
        charts["spreads"] = {"labels": [d.strftime("%Y-%m-%d") for d in df.index],
                             "sets": [{"name": n.replace("y", "Y").replace("m", "M"), "data": [r(v, 3) for v in df[n]]} for n in df.columns]}

    if data["dxy"] is not None:
        d = data["dxy"].tail(DAILY_WINDOW)
        charts["dxy"] = {"labels": [i.strftime("%Y-%m-%d") for i in d.index], "data": [r(v, 3) for v in d]}

    if "DFII10" in data["yields"]:
        d = data["yields"]["DFII10"].tail(DAILY_WINDOW)
        charts["real"] = {"labels": [i.strftime("%Y-%m-%d") for i in d.index], "data": [r(v, 3) for v in d]}

    cot_sets = []
    labels: set[str] = set()
    for key, name in (("usd_index", "USD Index (Non-Comm)"), ("gold", "Gold (Managed Money)")):
        c = data["cot"].get(key)
        if c is None:
            continue
        s = c["index"].tail(COT_WINDOW)
        labels.update(i.strftime("%Y-%m-%d") for i in s.index)
        cot_sets.append((name, s))
    oi_sets = []
    for key, name in (("usd_index", "USD Index (Non-Comm)"), ("gold", "Gold (Managed Money)")):
        c = data["cot"].get(key)
        if c is not None and c["net_oi"].notna().any():
            oi_sets.append((name, c["net_oi"].tail(COT_WINDOW)))
    if oi_sets:
        lab = sorted({i.strftime("%Y-%m-%d") for _, s_ in oi_sets for i in s_.index})
        charts["cotoi"] = {"labels": lab, "sets": [
            {"name": n, "data": [r(s_.get(pd.Timestamp(l)), 2) for l in lab]} for n, s_ in oi_sets]}
    pr_sets = []
    for key, name in (("usd_index", "DXY"), ("gold", "Gold futures")):
        c = data["cot"].get(key)
        if c is not None and c["price_chg"].notna().any():
            pr_sets.append((name, c["price_chg"].tail(COT_WINDOW)))
    if pr_sets:
        lab = sorted({i.strftime("%Y-%m-%d") for _, s_ in pr_sets for i in s_.index})
        charts["cotpx"] = {"labels": lab, "sets": [
            {"name": n, "data": [r(s_.get(pd.Timestamp(l)), 2) for l in lab]} for n, s_ in pr_sets]}
    if cot_sets:
        all_labels = sorted(labels)
        charts["cot"] = {"labels": all_labels, "sets": [
            {"name": n, "data": [r(s.get(pd.Timestamp(l)), 1) for l in all_labels]} for n, s in cot_sets]}
    return charts


def build_cot_tables(data: dict) -> str:
    """One scrollable 52-week table per instrument, newest first. Everything comes from the same CFTC report."""
    heads = ["Report date", "Net", "Net chg", "Long chg", "Short chg", "Open int.", "OI chg", "OI chg %",
             "Net % of OI", "Net % OI chg (pp)", "Net % OI 0-100", "COT Index", "Friday close", "Price chg %"]
    out = []
    for key, name, pname in (("gold", "Gold (Managed Money)", "Gold futures"), ("usd_index", "USD Index (Non-Commercial)", "DXY")):
        c = data["cot"].get(key)
        if c is None:
            continue
        n = COT_WINDOW
        rows = []
        for i in range(len(c["net"]) - 1, max(len(c["net"]) - 1 - n, -1), -1):
            d = c["net"].index[i]
            cells = [d.strftime("%Y-%m-%d"), fmt(c["net"].iloc[i], 0, True), fmt(c["wchg"].iloc[i], 0, True),
                     fmt(c["long_chg"].iloc[i], 0, True), fmt(c["short_chg"].iloc[i], 0, True),
                     fmt(c["oi"].iloc[i], 0), fmt(c["oi_chg"].iloc[i], 0, True), fmt(c["oi_chg_pct"].iloc[i], 2, True),
                     fmt(c["net_oi"].iloc[i], 1, True), fmt(c["net_oi_chg"].iloc[i], 1, True), fmt(c["net_oi_idx"].iloc[i], 0),
                     fmt(c["index"].iloc[i], 0), fmt(c["price"].iloc[i], 2), fmt(c["price_chg"].iloc[i], 2, True)]
            rows.append("<tr>" + f"<th scope=\"row\">{cells[0]}</th>" + "".join(f"<td>{esc(x)}</td>" for x in cells[1:]) + "</tr>")
        out.append(
            f'<div class="chartbox"><h4>{esc(name)}: last {n} weekly reports, newest first</h4>'
            f'<p class="lede small">Price is the {esc(pname)} close on the Friday of each report week and the change from the prior Friday. '
            f'Net % of OI = speculator long minus short, divided by total open interest. "0-100" rescales a value within its own 52-report range.</p>'
            f'<div class="scroll tall"><table class="weekly"><thead><tr>' + "".join(f"<th>{h}</th>" for h in heads)
            + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></div>")
    return "".join(out)


def build_status(data: dict, today: date) -> str:
    rows = []
    st = data["status"]
    for name, info in sorted(st.items()):
        last = info.get("last_data_date") or "-"
        stale = ""
        if info.get("last_data_date"):
            age = (today - datetime.strptime(info["last_data_date"], "%Y-%m-%d").date()).days
            limit = 12 if name.startswith("COT") else 6
            if age > limit:
                stale = ' <span class="badge warn">stale</span>'
        state = "ok" if info.get("ok") else '<span class="badge warn">last update failed</span>'
        err = f' <span class="err">{esc(info["error"])}</span>' if info.get("error") else ""
        rows.append(f"<tr><td>{esc(name)}</td><td>{state}{stale}{err}</td><td>{esc(last)}</td><td>{esc(info.get('checked_utc', ''))}</td></tr>")
    if not rows:
        return "<p>No update has been recorded yet (data/status.json is missing). The numbers above come from the CSV files committed in the repo.</p>"
    return ('<table><thead><tr><th>Source</th><th>Status</th><th>Latest data date</th><th>Checked</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table>")


TEMPLATE = (ROOT / "template.html").read_text(encoding="utf-8") if (ROOT / "template.html").exists() else None


def render(playbook: dict, data: dict, computed: dict, today: date) -> str:
    metrics = computed["metrics"]
    cards = build_cards(playbook, metrics)
    fired = eval_cross_signals(playbook, metrics)
    charts = build_chart_data(data, computed["spread_series"])

    if fired:
        sig_html = "".join(
            f'<li><strong>{esc(s["label"])}</strong><br><span>{esc(s["meaning"])}</span></li>' for s in fired)
        sig_html = f'<ul class="signals">{sig_html}</ul>'
    else:
        sig_html = '<p class="muted-text">No cross-indicator signals are active right now.</p>'

    dates = [m["date"] for m in metrics.values() if m.get("date")]
    as_of = max(dates) if dates else "n/a"

    page = TEMPLATE
    page = page.replace("__ASOF__", esc(as_of))
    page = page.replace("__BUILT__", esc(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")))
    page = page.replace("__SIGNALS__", sig_html)
    page = page.replace("__SUMMARY__", build_summary(playbook, metrics))
    page = page.replace("__CARDS_SPREADS__", cards["spreads"])
    page = page.replace("__CARDS_LEVELS__", cards["levels"])
    page = page.replace("__CARDS_COT__", cards["cot"])
    page = page.replace("__COT_TABLES__", build_cot_tables(data))
    page = page.replace("__STATUS__", build_status(data, today))
    page = page.replace("__DISCLAIMER__", esc(playbook["meta"]["disclaimer"]))
    page = page.replace("__CHARTS__", json.dumps(charts).replace("</", "<\\/"))
    return page


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
    out.write_text(render(playbook, data, computed, today), encoding="utf-8")
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
