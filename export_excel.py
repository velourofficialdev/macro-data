"""Write docs/Macro_Dashboard.xlsx: every number on the dashboard as plain Excel sheets (values, not formulas).

Called from build_dashboard.py after the page is built. Sheets:
  Summary        latest headline readings with their plain-English label
  Yields         daily Treasury yields, spreads, and 252-day z-scores / 5D / 20D changes
  DXY            daily DXY with 252-day z-score and 5D / 20D % changes
  Gold_Price     gold futures, last daily bar (intraday during the US session)
  COT_Gold       weekly Gold COT (Legacy, Non-Commercial): positions, weekly changes, open interest, net % of OI, 52-report stats, Friday price
  COT_USD_Index  same for the USD Index COT (Non-Commercial), with DXY as the price
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

import build_dashboard as bd


def _daily_block(s: pd.Series, name: str) -> pd.DataFrame:
    z = bd.roll_z(s, bd.DAILY_WINDOW)
    return pd.DataFrame({name: s, f"{name} z252": z, f"{name} 5D chg": s.diff(5), f"{name} 20D chg": s.diff(20)})


def yields_sheet(data: dict) -> pd.DataFrame | None:
    y = data["yields"]
    if not y:
        return None
    levels = pd.concat({c: y[c] for c in bd.YIELD_CODES if c in y}, axis=1)
    names = {"DGS3MO": "3M", "DGS2": "2Y", "DGS5": "5Y", "DGS10": "10Y", "DGS20": "20Y", "DGS30": "30Y", "DFII10": "10Y real"}
    levels = levels.rename(columns=names)
    parts = [levels]
    for code, nm in names.items():
        if code in y:
            parts.append(pd.DataFrame({f"{nm} z252": bd.roll_z(y[code], bd.DAILY_WINDOW)}))
    for sp, (lg, sh) in bd.SPREADS.items():
        if lg in y and sh in y:
            s = (y[lg] - y[sh]).dropna()
            parts.append(_daily_block(s, f"{sp.upper()} spread"))
    out = pd.concat(parts, axis=1).sort_index()
    out.index.name = "Date"
    return out.reset_index()


def price_sheet(s: pd.Series | None, name: str, pct: bool) -> pd.DataFrame | None:
    if s is None:
        return None
    df = pd.DataFrame({name: s, f"{name} z252": bd.roll_z(s, bd.DAILY_WINDOW)})
    df[f"{name} 5D %"] = s.pct_change(5) * 100
    df[f"{name} 20D %"] = s.pct_change(20) * 100
    df.index.name = "Date"
    return df.reset_index()


def cot_sheet(c: dict | None, price_name: str) -> pd.DataFrame | None:
    if c is None:
        return None
    df = pd.DataFrame({
        "Report date": c["net"].index, "Long": c["long"].values, "Short": c["short"].values, "Net": c["net"].values,
        "Net chg": c["wchg"].values, "Long chg": c["long_chg"].values, "Short chg": c["short_chg"].values,
        "Open interest": c["oi"].values, "OI chg": c["oi_chg"].values, "OI chg %": c["oi_chg_pct"].values,
        "Net % of OI": c["net_oi"].values, "Net % of OI chg (pp)": c["net_oi_chg"].values,
        "Net % of OI 0-100 (52)": c["net_oi_idx"].values, "Net % of OI z (52)": c["net_oi_z"].values,
        "COT Index 0-100 (52)": c["index"].values, "Net z (52)": c["z"].values,
        "Friday": c["friday"].values, f"{price_name} Friday close": c["price"].values,
        f"{price_name} weekly chg %": c["price_chg"].values,
    })
    return df


def summary_sheet(playbook: dict, metrics: dict) -> pd.DataFrame:
    rows = []

    def add(name, latest, c5, c20, z, label, date):
        rows.append({"Indicator": name, "Latest": latest, "5D change": c5, "20D change": c20, "Z-score": z, "Reading": label, "As of": date})

    for name, spec in playbook["spreads"].items():
        m = metrics.get(f"S:{name}")
        if m:
            b = bd.pick_band(spec["bands"], m["value"])
            add(spec["title"] + " spread (pp)", m["value"], m["chg5"], m["chg20"], m["z"], b["label"] if b else "", m["date"])
    for code, label in (("DGS2", "US 2Y yield (%)"), ("DGS10", "US 10Y yield (%)")):
        m = metrics.get(f"Y:{code}")
        if m:
            b = bd.pick_band(playbook["zscore"]["bands"], m["z"])
            add(label, m["value"], m["chg5"], m["chg20"], m["z"], b["label"] if b else "", m["date"])
    m = metrics.get("Y:DFII10")
    if m:
        b = bd.pick_band(playbook["real_yield"]["bands"], m["z"])
        add("10Y real yield (%)", m["value"], m["chg5"], m["chg20"], m["z"], b["label"] if b else "", m["date"])
    m = metrics.get("DXY")
    if m:
        b = bd.pick_band(playbook["dxy"]["bands"], m["z"])
        add("DXY (5D/20D are % changes)", m["value"], m["pct5"], m["pct20"], m["z"], b["label"] if b else "", m["date"])
    for key, label in (("usd_index", "USD Index COT net"), ("gold", "Gold COT net")):
        m = metrics.get(f"COT:{key}")
        if m:
            b = bd.pick_band(playbook["cot"][key]["bands"], m["index"])
            add(label + " (weekly chg / COT Index)", m["net"], m["wchg"], m["index"], m["z"], b["label"] if b else "", m["date"])
    return pd.DataFrame(rows)


def _format(ws, df: pd.DataFrame) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    head_fill = PatternFill("solid", fgColor="DCE9FB")   # light tint of primary #90BFF9
    for c in ws[1]:
        c.font = Font(bold=True, color="1E293B")             # slate text, AA on the tint
        c.fill = head_fill
        c.alignment = Alignment(wrap_text=True, vertical="center")
    ws.freeze_panes = "B2"
    ws.row_dimensions[1].height = 34
    for i, col in enumerate(df.columns, start=1):
        letter = get_column_letter(i)
        ws.column_dimensions[letter].width = 22 if i == 1 and ("Indicator" in str(col)) else 13
        lc = str(col).lower()
        fmt = None
        if "date" in lc or lc in ("friday", "as of"):
            fmt = "yyyy-mm-dd"
        elif any(k in lc for k in ("long", "short", "net", "open interest", "oi chg")) and "%" not in lc and "z" not in lc.split()[-1:] and "index" not in lc and "0-100" not in lc and "pp" not in lc:
            fmt = "#,##0;-#,##0"
        else:
            fmt = "0.00"
        if fmt:
            for cell in ws[letter][1:]:
                cell.number_format = fmt
    if "Indicator" in df.columns:
        ws.column_dimensions["A"].width = 34
        ws.column_dimensions["F"].width = 22


def options_levels_sheet(data: dict, today=None) -> pd.DataFrame | None:
    """Internal build only: one row per instrument and level (option units and the mapped futures units), with source text and fetch time."""
    rows = []
    try:
        import options_levels as ol
    except Exception:  # noqa: BLE001
        ol = None
    for key, meta in bd.OPT_META.items():
        s = (data.get("options") or {}).get(key)
        if not isinstance(s, dict) or not s.get("spot"):
            continue
        conv = bd._opt_mapper(s, meta)
        src, fetched = "Delayed option chain", s.get("updated_utc") or s.get("date")
        try:
            if ol is not None and hasattr(ol, "source_info"):
                si = ol.source_info(s, key, today or pd.Timestamp.utcnow().date())
                src, fetched = (bd.neutral_src(si) or {}).get("text") or src, si.get("fetched_utc") or fetched
        except Exception:  # noqa: BLE001
            pass
        for name, v in (("Price", s.get("spot")), ("Call wall", s.get("call_wall")), ("Put wall", s.get("put_wall")), ("Gamma flip", s.get("gamma_flip")), ("Max pain", s.get("max_pain"))):
            if v is None:
                continue
            rows.append({"Instrument": key, "Level": name, "Value": v, "Mapped value": conv(v) if conv else None, "Mapped to": meta["map"] if conv else None, "Source": src, "Fetched (UTC)": fetched})
    return pd.DataFrame(rows) if rows else None


def export(path: Path, playbook: dict, data: dict, computed: dict, options_desk: bool = False, today=None) -> None:
    sheets = {
        "Summary": summary_sheet(playbook, computed["metrics"]),
        "Yields": yields_sheet(data),
        "DXY": price_sheet(data["dxy"], "DXY", True),
        "Gold_Price": price_sheet(data.get("gold_px"), "Gold", True),
        "COT_Gold": cot_sheet(data["cot"].get("gold"), "Gold"),
        "COT_USD_Index": cot_sheet(data["cot"].get("usd_index"), "DXY"),
        "Fed_Rate_Odds": data.get("zq"),          # daily snapshots from 30-Day Fed Funds futures (fed_rate_odds.py)
    }
    if options_desk:   # internal build only; the public workbook carries no options content
        sheets["Options_Levels"] = options_levels_sheet(data, today)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets.items():
            if df is None or df.empty:
                continue
            df = df.replace([np.inf, -np.inf], np.nan).round(4)
            df.to_excel(xw, sheet_name=name, index=False)
            _format(xw.sheets[name], df)
