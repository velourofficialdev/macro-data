"""Daily-close chart feed for the Velour chart tab (KLineChart). Pure: reads the CSVs already in data/, no network.

build_chart_feed(data_dir) -> {"v":1,"built":"YYYY-MM-DD","symbols":[...]}; each symbol carries close-only data
(`ohlc` false) as day gaps `dd` from `t0` and closes `c` rounded to `dec`. A missing or unreadable file skips that symbol.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# id, name, file, value column, unit, decimals, fixed source label (None = read the `source` column), opt_key, opt_units
SYMBOLS = [
    ("GOLD", "Gold (COMEX futures)", "GOLD.csv", "Gold", "$", 2, None, "GOLD", "mapped"),
    ("SPX", "S&P 500", "idx/ES.csv", "Close", "pts", 2, None, "SPX", "index"),
    ("NDX", "Nasdaq 100", "idx/NQ.csv", "Close", "pts", 2, None, "NDX", "index"),
    ("DOW", "Dow Jones Industrial Average", "idx/US30.csv", "Close", "pts", 2, None, "DJX", "x100"),
    ("DXY", "US Dollar index (DXY)", "DXY.csv", "DXY", "", 3, None, None, None),
    ("EURUSD", "EUR/USD", "fx/EURUSD.csv", "Close", "$", 4, None, None, None),
    ("US10Y", "US 10-year Treasury yield", "DGS10.csv", "DGS10", "%", 2, "FRED DGS10, daily", None, None),
]


def _gold_label(data_dir: Path) -> str:
    try:
        note = (json.loads((data_dir / "status.json").read_text()).get("GOLD_PRICE") or {}).get("note") or ""
        note = note.replace("source:", "").strip()
        return note or "Yahoo GC=F"
    except Exception:  # noqa: BLE001
        return "Yahoo GC=F"


def build_chart_feed(data_dir: Path, max_points: int = 2600) -> dict:
    data_dir = Path(data_dir)
    out = []
    for sid, name, rel, col, unit, dec, fixed, opt_key, opt_units in SYMBOLS:
        try:
            df = pd.read_csv(data_dir / rel)
            dcol = "Date" if "Date" in df.columns else "date"
            df = df[[c for c in (dcol, col, "source") if c in df.columns]].copy()
            df[col] = pd.to_numeric(df[col], errors="coerce")
            df[dcol] = pd.to_datetime(df[dcol], errors="coerce")
            df = df.dropna(subset=[dcol, col]).drop_duplicates(dcol, keep="last").sort_values(dcol).tail(max_points)
            if df.empty:
                continue
            if sid == "GOLD":
                src = _gold_label(data_dir)
            elif fixed:
                src = fixed
            elif "source" in df.columns and df["source"].notna().any():
                src = str(df["source"].dropna().iloc[-1])
            else:
                src = name
            days = df[dcol].dt.normalize()
            t0 = days.iloc[0]
            dd = [0] + [int(x) for x in days.diff().dt.days.iloc[1:]]
            out.append({"id": sid, "name": name, "src": src, "delay": "Daily close", "unit": unit, "dec": dec,
                        "ohlc": False, "intervals": ["D", "W"], "t0": t0.strftime("%Y-%m-%d"), "dd": dd,
                        "c": [round(float(x), dec) for x in df[col]], "opt_key": opt_key, "opt_units": opt_units})
        except Exception:  # noqa: BLE001 - a missing or broken file only drops that symbol
            continue
    return {"v": 1, "built": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "symbols": out}
