"""Options positioning levels for SPX (mapped to ES) and gold (GLD options mapped to the gold future).

Source: Cboe's free delayed option chains (no key, no account):
    https://cdn.cboe.com/api/global/delayed_quotes/options/<SYMBOL>.json
Run from update_data.py after the US close, so open interest is the settled figure from the
start of that session and prices are the close.

What is computed (all from open interest, so it is end-of-day positioning, not a live trade tape):
- Net gamma exposure (GEX) by strike, in $ per 1% move of the underlying. Uses the common
  simplifying assumption that dealers are long the calls and short the puts that customers
  hold; real dealer books differ, so treat it as a map of where hedging pressure may sit.
- Gamma flip: the underlying price where total GEX crosses zero (re-priced across a grid).
- Call wall / put wall: the strikes with the largest call / put gamma above / below price.
- Max pain for the nearest expiry, put/call ratios, 1-day and front-expiry expected moves,
  and day-over-day open-interest changes by strike.

Output per instrument in data/options/:
    <KEY>.json          latest snapshot used by build_dashboard.py
    <KEY>_history.csv   one summary row per day (levels over time)
    <KEY>_oi.csv        open interest by strike for the last snapshot (for the next day's change)
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
HEADERS = {"User-Agent": "Mozilla/5.0 (macro-dashboard; personal research)"}
TIMEOUT = 60
RATE = 0.04          # flat risk-free rate for the gamma re-pricing; GEX is not sensitive to it
MAX_DTE = 60         # expiries further out than this add little gamma near price
OPT_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<ymd>\d{6})(?P<cp>[CP])(?P<k>\d{8})$")

RV_SOURCE = {   # realised vol of the underlying: (Yahoo symbol, fallback file under data/, column, label)
    "SPX": ("^GSPC", "econ/SP500.csv", "SP500", "S&P 500 index closes"),
    "NDX": ("^NDX", None, None, "Nasdaq-100 index closes"),
    "DJX": ("^DJI", None, None, "Dow Jones Industrial Average closes"),
    "RUT": ("^RUT", None, None, "Russell 2000 index closes"),
    "GOLD": ("GC=F", "GOLD.csv", "Gold", "COMEX gold future closes (GC=F)"),
}
INSTRUMENTS = {
    # key: (Cboe symbol, label, mapped instrument label, Yahoo symbol of the mapped instrument, strike window around price)
    "SPX": ("_SPX", "SPX", "ES (E-mini S&P 500 future)", "ES=F", 0.06),
    "NDX": ("_NDX", "NDX", "NQ (E-mini Nasdaq-100 future)", "NQ=F", 0.07),
    "DJX": ("_DJX", "DJX", "YM (E-mini Dow future)", "YM=F", 0.06),
    "RUT": ("_RUT", "RUT", "RTY (E-mini Russell 2000 future)", "RTY=F", 0.08),
    "GOLD": ("GLD", "GLD", "Gold future (GC)", "GC=F", 0.08),
}
MAX_STRIKES = 150   # bigger chains are grouped into wider strike buckets so the page stays light


# ---------------------------------------------------------------- fetch
def fetch_chain(sym: str) -> tuple[pd.DataFrame, float, str]:
    r = requests.get(CBOE_URL.format(sym=sym), headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    js = r.json()
    d = js.get("data") or {}
    spot = float(d.get("current_price") or d.get("close") or 0)
    if spot <= 0:
        raise ValueError("Cboe returned no underlying price")
    rows = []
    for o in d.get("options") or []:
        m = OPT_RE.match(str(o.get("option", "")))
        if not m:
            continue
        rows.append({
            "root": m["root"], "expiry": pd.Timestamp("20" + m["ymd"]), "type": m["cp"],
            "strike": int(m["k"]) / 1000.0,
            "oi": float(o.get("open_interest") or 0), "volume": float(o.get("volume") or 0),
            "iv": float(o.get("iv") or 0), "bid": float(o.get("bid") or 0), "ask": float(o.get("ask") or 0),
            "last": float(o.get("last_trade_price") or 0),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("Cboe returned no option rows")
    stamp = str(js.get("timestamp") or d.get("last_trade_time") or "")
    return df, spot, stamp


def fetch_last_close(sym: str) -> float | None:
    try:
        r = requests.get(YAHOO_URL.format(sym=sym), params={"range": "5d", "interval": "1d"}, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
        return float(closes[-1]) if closes else None
    except Exception:  # noqa: BLE001 - mapping is optional; levels still show in the option's own units
        return None


# ---------------------------------------------------------------- math
def bs_gamma(spot, strike, t, iv):
    """Black-Scholes gamma, vectorised. spot may be a scalar or a column vector for a grid."""
    spot = np.asarray(spot, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        sd = iv * np.sqrt(t)
        d1 = (np.log(spot / strike) + (RATE + 0.5 * iv * iv) * t) / sd
        g = np.exp(-0.5 * d1 * d1) / (np.sqrt(2 * np.pi) * spot * sd)
    return np.nan_to_num(g, nan=0.0, posinf=0.0, neginf=0.0)


def prepare(df: pd.DataFrame, asof: pd.Timestamp) -> pd.DataFrame:
    df = df.copy()
    # expiry at 16:00 New York (about 20:30 UTC); a floor keeps same-day expiries finite
    t = ((df["expiry"] + pd.Timedelta(hours=20, minutes=30)) - asof).dt.total_seconds() / (365.0 * 86400)
    df["t"] = t.clip(lower=1.0 / (365 * 24))
    df["dte"] = (df["expiry"] - asof.normalize()).dt.days
    df = df[(df["dte"] >= 0) & (df["dte"] <= MAX_DTE) & (df["oi"] > 0) & (df["iv"] > 0.01) & (df["iv"] < 5)]
    df["sign"] = np.where(df["type"] == "C", 1.0, -1.0)
    return df


def gex_at(df: pd.DataFrame, spot: float) -> np.ndarray:
    """$ gamma per 1% move for each row (calls positive, puts negative)."""
    g = bs_gamma(spot, df["strike"].values, df["t"].values, df["iv"].values)
    return df["sign"].values * g * df["oi"].values * 100.0 * spot * spot * 0.01


def gamma_flip(df: pd.DataFrame, spot: float) -> tuple[float | None, list[list[float]]]:
    grid = np.linspace(spot * 0.88, spot * 1.12, 121)
    g = bs_gamma(grid[:, None], df["strike"].values[None, :], df["t"].values[None, :], df["iv"].values[None, :])
    tot = (df["sign"].values[None, :] * g * df["oi"].values[None, :] * 100.0 * (grid[:, None] ** 2) * 0.01).sum(axis=1)
    profile = [[round(float(x), 2), round(float(y) / 1e9, 4)] for x, y in zip(grid, tot)]
    cross = [i for i in range(1, len(grid)) if np.sign(tot[i]) != np.sign(tot[i - 1]) and tot[i - 1] != 0]
    if not cross:
        return None, profile
    flips = []
    for i in cross:   # linear interpolation inside the grid step
        x0, x1, y0, y1 = grid[i - 1], grid[i], tot[i - 1], tot[i]
        flips.append(x0 - y0 * (x1 - x0) / (y1 - y0))
    return float(min(flips, key=lambda x: abs(x - spot))), profile


def max_pain(front: pd.DataFrame) -> float | None:
    if front.empty:
        return None
    ks = np.sort(front["strike"].unique())
    c = front[front["type"] == "C"]
    p = front[front["type"] == "P"]
    pain = [((np.clip(k - c["strike"].values, 0, None) * c["oi"].values).sum()
             + (np.clip(p["strike"].values - k, 0, None) * p["oi"].values).sum()) for k in ks]
    return float(ks[int(np.argmin(pain))])


def front_expected_move(raw: pd.DataFrame, spot: float, asof: pd.Timestamp) -> dict | None:
    """ATM straddle (mid) on the first expiry at least 1 day out."""
    fut = raw[(raw["expiry"] > asof.normalize())]
    if fut.empty:
        return None
    exp = fut["expiry"].min()
    ch = fut[fut["expiry"] == exp]
    k = ch["strike"].iloc[(ch["strike"] - spot).abs().argsort().iloc[0]]
    leg = ch[ch["strike"] == k]

    def mid(row):
        b, a = row["bid"], row["ask"]
        return (b + a) / 2 if a > 0 and b > 0 else row["last"]
    c = leg[leg["type"] == "C"]
    p = leg[leg["type"] == "P"]
    if c.empty or p.empty:
        return None
    straddle = float(mid(c.iloc[0]) + mid(p.iloc[0]))
    if straddle <= 0:
        return None
    return {"expiry": exp.strftime("%Y-%m-%d"), "strike": float(k), "move": round(straddle, 2),
            "low": round(spot - straddle, 2), "high": round(spot + straddle, 2)}


def analyse(raw: pd.DataFrame, spot: float, asof: pd.Timestamp, window: float) -> dict:
    df = prepare(raw, asof)
    if df.empty:
        raise ValueError("no usable contracts within the expiry window")
    df["gex"] = gex_at(df, spot)
    by = df.groupby(["strike", "type"])["gex"].sum().unstack(fill_value=0.0)
    for col in ("C", "P"):
        if col not in by:
            by[col] = 0.0
    by["net"] = by["C"] + by["P"]
    near = by[(by.index >= spot * (1 - window)) & (by.index <= spot * (1 + window))]
    above, below = near[near.index > spot], near[near.index < spot]
    call_wall = float(above["C"].idxmax()) if not above.empty and above["C"].max() > 0 else None
    put_wall = float(below["P"].idxmin()) if not below.empty and below["P"].min() < 0 else None
    flip, profile = gamma_flip(df, spot)
    net = float(by["net"].sum())

    # ATM implied vol of the first expiry at least 1 day out -> 1-day expected move
    fut = df[df["dte"] >= 1]
    atm_iv = None
    if not fut.empty:
        e0 = fut[fut["expiry"] == fut["expiry"].min()]
        near_k = e0.iloc[(e0["strike"] - spot).abs().argsort()[:4]]
        atm_iv = float(near_k["iv"].mean())
    day_move = spot * atm_iv * math.sqrt(1 / 252) if atm_iv else None

    front_exp = raw[raw["expiry"] >= asof.normalize()]["expiry"].min()
    mp = max_pain(raw[(raw["expiry"] == front_exp) & (raw["oi"] > 0)])
    oi_c, oi_p = raw.loc[raw["type"] == "C", "oi"].sum(), raw.loc[raw["type"] == "P", "oi"].sum()
    v_c, v_p = raw.loc[raw["type"] == "C", "volume"].sum(), raw.loc[raw["type"] == "P", "volume"].sum()

    strikes = [{"k": float(k), "c": round(float(r_["C"]) / 1e6, 2), "p": round(float(r_["P"]) / 1e6, 2)}
               for k, r_ in near.iterrows() if abs(r_["C"]) + abs(r_["P"]) > 0]
    return {
        "spot": round(spot, 2), "net_gex_bn": round(net / 1e9, 3),
        "regime": "positive" if (flip is not None and spot > flip) or (flip is None and net > 0) else "negative",
        "gamma_flip": round(flip, 2) if flip else None, "call_wall": call_wall, "put_wall": put_wall,
        "max_pain": mp, "max_pain_expiry": front_exp.strftime("%Y-%m-%d") if pd.notna(front_exp) else None,
        "atm_iv": round(atm_iv * 100, 2) if atm_iv else None,
        "day_move": round(day_move, 2) if day_move else None,
        "front_move": front_expected_move(raw, spot, asof),
        "pcr_oi": round(oi_p / oi_c, 3) if oi_c else None, "pcr_vol": round(v_p / v_c, 3) if v_c else None,
        "king": ({"k": float(near["net"].abs().idxmax()), "v": round(float(near.loc[near["net"].abs().idxmax(), "net"]) / 1e6, 2)}
                 if not near.empty and near["net"].abs().max() > 0 else None),
        "strikes": strikes, "profile": profile,
        "contracts": int(len(df)), "expiries": int(df["expiry"].nunique()),
    }


_erf = np.frompyfunc(math.erf, 1, 1)


def _ncdf(x):
    return 0.5 * (1.0 + _erf(np.asarray(x, dtype=float) / math.sqrt(2.0)).astype(float))


GREEKS = ("gamma", "delta", "vanna", "charm", "theta", "vega")
GREEK_UNITS = {   # what one unit of each exposure means, shown on the page
    "gamma": "$ of delta per 1% move", "delta": "$ delta notional", "vanna": "$ of delta per 1 vol point",
    "charm": "$ of delta per day", "theta": "$ per day", "vega": "$ per 1 vol point",
}
MAX_COLS = 12     # expiries shown one by one (the "All" view still uses every expiry up to MAX_DTE)


def greek_exposures(df: pd.DataFrame, spot: float) -> pd.DataFrame:
    """Dealer-book exposure per contract row under the same convention as GEX:
    calls counted +, puts counted - (dealers long customer calls, short customer puts)."""
    S, K, t, v = spot, df["strike"].values, df["t"].values, df["iv"].values
    sq = np.sqrt(t)
    d1 = (np.log(S / K) + (RATE + 0.5 * v * v) * t) / (v * sq)
    d2 = d1 - v * sq
    pdf = np.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    call = df["type"].values == "C"
    nd1, nd2 = _ncdf(d1), _ncdf(d2)
    delta = np.where(call, nd1, nd1 - 1.0)
    gamma = pdf / (S * v * sq)
    vega = S * pdf * sq / 100.0                                   # per vol point
    vanna = -pdf * d2 / v / 100.0                                 # d(delta)/d(vol point)
    charm = -pdf * (2 * RATE * t - d2 * v * sq) / (2 * t * v * sq) / 365.0   # d(delta)/day
    disc = np.exp(-RATE * t)
    theta = np.where(call, -S * pdf * v / (2 * sq) - RATE * K * disc * nd2,
                     -S * pdf * v / (2 * sq) + RATE * K * disc * (1 - nd2)) / 365.0
    w = df["sign"].values * df["oi"].values * 100.0
    out = pd.DataFrame({"strike": df["strike"].values, "expiry": df["expiry"].values, "type": df["type"].values})
    out["gamma"] = w * gamma * S * S * 0.01
    out["delta"] = w * delta * S
    out["vanna"] = w * vanna * S
    out["charm"] = w * charm * S
    out["theta"] = w * theta
    out["vega"] = w * vega
    return out.replace([np.inf, -np.inf], 0.0).fillna(0.0)


def _sig(x: float) -> float:
    """4 significant figures keeps the page small without changing what is drawn."""
    if not x or not math.isfinite(x):
        return 0
    return float(f"{x:.4g}")


def build_surface(raw: pd.DataFrame, spot: float, asof: pd.Timestamp, window: float) -> dict:
    df = prepare(raw, asof)
    df = df[(df["strike"] >= spot * (1 - window)) & (df["strike"] <= spot * (1 + window))]
    if df.empty:
        return {}
    ex = greek_exposures(df, spot)
    step = bucket_step(df["strike"].unique())
    if step:
        ex["strike"] = np.floor(ex["strike"] / step + 0.5) * step   # floor(x+0.5), not round(): banker's rounding makes uneven buckets
    strikes = sorted(ex["strike"].unique())
    exps = sorted(ex["expiry"].unique())
    oi_by_exp = df.groupby("expiry")["oi"].sum()
    cols = exps[:10]
    big = [e for e in oi_by_exp.sort_values(ascending=False).index if e not in cols][:MAX_COLS - len(cols)]
    cols = sorted(cols + big)
    kidx = {k: i for i, k in enumerate(strikes)}

    def vec(frame: pd.DataFrame, col: str) -> list:
        s = frame.groupby("strike")[col].sum()
        arr = [0] * len(strikes)
        for k, val in s.items():
            arr[kidx[k]] = _sig(val)
        return arr

    greeks = {}
    for g in GREEKS:
        greeks[g] = {"all": vec(ex, g), "ex": [vec(ex[ex["expiry"] == e], g) for e in cols]}
    calls, puts = ex[ex["type"] == "C"], ex[ex["type"] == "P"]
    walls = {"all": {"c": vec(calls, "gamma"), "p": vec(puts, "gamma")},
             "ex": [{"c": vec(calls[calls["expiry"] == e], "gamma"), "p": vec(puts[puts["expiry"] == e], "gamma")} for e in cols]}

    exp_meta = []
    for e in cols:
        sub = df[df["expiry"] == e]
        fl, _ = gamma_flip(sub, spot) if len(sub) else (None, None)
        exp_meta.append({"e": pd.Timestamp(e).strftime("%Y-%m-%d"), "dte": int(sub["dte"].iloc[0]) if len(sub) else None,
                         "oi": int(oi_by_exp.get(e, 0)), "flip": round(fl, 2) if fl else None})

    # IV smile: every quoted contract (open interest not required) on the shown expiries
    sm = raw[(raw["iv"] > 0.01) & (raw["iv"] < 5) & (raw["strike"] >= spot * (1 - window)) & (raw["strike"] <= spot * (1 + window))]
    smile = []
    for e in cols:
        se = sm[sm["expiry"] == e]
        c = se[se["type"] == "C"].groupby("strike")["iv"].mean()
        p = se[se["type"] == "P"].groupby("strike")["iv"].mean()
        smile.append({"c": [[float(k), round(float(v) * 100, 2)] for k, v in c.items()],
                      "p": [[float(k), round(float(v) * 100, 2)] for k, v in p.items()]})
    for sm_e in smile:   # thin very dense smiles
        for side in ("c", "p"):
            n = len(sm_e[side])
            if n > 120:
                sm_e[side] = sm_e[side][::math.ceil(n / 120)]
    return {"strikes": [float(k) for k in strikes], "step": step, "expiries": exp_meta, "greeks": greeks, "walls": walls,
            "smile": smile, "units": GREEK_UNITS}


# ---------------------------------------------------------------- scenario reprice (Effective / Shadow), vol term structure, VRP
LADDER = [-3.0, -2.0, -1.5, -1.0, -0.5, 0.5, 1.0, 1.5, 2.0, 3.0]   # % moves; the 1-sigma move is added per instrument


def _sticky_moneyness_iv(df: pd.DataFrame, spot: float, new_spot: float) -> np.ndarray:
    """IV each contract would carry if the smile moved with price (same moneyness, same expiry)."""
    out = df["iv"].values.copy()
    for e, idx in df.groupby("expiry").indices.items():
        sub = df.iloc[idx]
        curve = sub.groupby("strike")["iv"].mean().sort_index()
        if len(curve) < 3:
            continue
        look = sub["strike"].values * spot / new_spot
        out[idx] = np.interp(look, curve.index.values, curve.values)
    return out


def bucket_step(ks) -> float | None:
    ks = sorted(set(float(k) for k in ks))
    if len(ks) <= MAX_STRIKES:
        return None
    span = ks[-1] - ks[0]
    for st_ in (0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 250, 500):
        if span / st_ <= MAX_STRIKES:
            return st_
    return 1000.0


def fetch_closes(ysym: str) -> pd.Series | None:
    try:
        r = requests.get(YAHOO_URL.format(sym=ysym), params={"range": "6mo", "interval": "1d"}, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        s = pd.Series(res["indicators"]["quote"][0]["close"], index=pd.to_datetime(res["timestamp"], unit="s"), dtype="float64").dropna()
        return s if len(s) > 30 else None
    except Exception:  # noqa: BLE001
        return None


def rv_from_closes(px: pd.Series | None) -> dict:
    if px is None:
        return {}
    r = np.log(px).diff().dropna()
    return {f"rv{n}": round(float(r.tail(n).std() * math.sqrt(252) * 100), 2) for n in (10, 20, 60) if len(r) >= n}


def scenarios(df: pd.DataFrame, spot: float, strikes: list, cols: list, sigma_pct: float | None, step: float | None = None) -> dict:
    """Gamma exposure per strike after a price move.
    effective = repriced at the new price with each contract keeping its IV (sticky strike);
    shadow    = the extra change when IV instead slides along the smile with price (sticky moneyness)."""
    sig = round(sigma_pct, 2) if sigma_pct else None
    moves = sorted(set([m for m in LADDER if not sig or abs(abs(m) - sig) > 0.2] + ([sig, -sig] if sig else [])))
    kidx = {k: i for i, k in enumerate(strikes)}
    groups = [("all", df)] + [(i, df[df["expiry"] == e]) for i, e in enumerate(cols[:3])]
    res = {"moves": moves, "eff": {}, "shadow": {}}
    for name, sub in groups:
        if sub.empty:
            continue
        eff_rows, sh_rows = [], []
        for m in moves:
            s2 = spot * (1 + m / 100)
            g_ss = bs_gamma(s2, sub["strike"].values, sub["t"].values, sub["iv"].values)
            iv_sm = _sticky_moneyness_iv(sub, spot, s2)
            g_sm = bs_gamma(s2, sub["strike"].values, sub["t"].values, iv_sm)
            w = sub["sign"].values * sub["oi"].values * 100.0 * s2 * s2 * 0.01
            for rows, vals in ((eff_rows, w * g_ss), (sh_rows, w * (g_sm - g_ss))):
                arr = [0] * len(strikes)
                kk = np.floor(sub["strike"].values / step + 0.5) * step if step else sub["strike"].values
                for k, v in pd.Series(vals, index=kk).groupby(level=0).sum().items():
                    if k in kidx:
                        arr[kidx[k]] = _sig(float(v))
                rows.append(arr)
        res["eff"][str(name)] = eff_rows
        res["shadow"][str(name)] = sh_rows
    return res


def vol_term(raw: pd.DataFrame, spot: float, asof: pd.Timestamp) -> list[dict]:
    """Per expiry: ATM IV, 25-delta risk reversal (put IV - call IV) and butterfly (wings avg - ATM)."""
    out = []
    d = raw[(raw["iv"] > 0.01) & (raw["iv"] < 5) & (raw["expiry"] > asof.normalize())].copy()
    for e, sub in sorted(d.groupby("expiry"), key=lambda x: x[0]):
        dte = (e - asof.normalize()).days
        if dte > 120:
            break
        t = max(dte, 0.5) / 365.0
        sub = sub.copy()
        sub["d1"] = (np.log(spot / sub["strike"]) + (RATE + 0.5 * sub["iv"] ** 2) * t) / (sub["iv"] * math.sqrt(t))
        c, p = sub[sub["type"] == "C"].copy(), sub[sub["type"] == "P"].copy()
        if len(c) < 3 or len(p) < 3:
            continue
        c["delta"] = _ncdf(c["d1"].values)
        p["delta"] = _ncdf(p["d1"].values) - 1
        atm = sub.iloc[(sub["strike"] - spot).abs().argsort()[:4]]["iv"].mean()
        c25 = c.iloc[(c["delta"] - 0.25).abs().argsort()[:1]]["iv"].mean()
        p25 = p.iloc[(p["delta"] + 0.25).abs().argsort()[:1]]["iv"].mean()
        out.append({"e": e.strftime("%Y-%m-%d"), "dte": int(dte), "atm": round(float(atm) * 100, 2),
                    "rr25": round(float(p25 - c25) * 100, 2), "bf25": round(float((p25 + c25) / 2 - atm) * 100, 2)})
    return out


def iv_at(term: list[dict], days: int) -> float | None:
    pts = [(x["dte"], x["atm"]) for x in term if x["dte"] >= 1]
    if len(pts) < 2:
        return pts[0][1] if pts else None
    xs, ys = zip(*pts)
    return round(float(np.interp(days, xs, ys)), 2)


def realised_vol(path: Path, col_hint: str | None = None) -> dict:
    """Annualised close-to-close realised vol over 10 / 20 / 60 trading days from a saved price CSV."""
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    col = col_hint if col_hint in df.columns else df.columns[1]
    px = pd.to_numeric(df[col], errors="coerce").dropna()
    r = np.log(px).diff().dropna()
    return {f"rv{n}": round(float(r.tail(n).std() * math.sqrt(252) * 100), 2) for n in (10, 20, 60) if len(r) >= n}


def fetch_fred_series(code: str, years: int = 10) -> pd.DataFrame:
    start = (pd.Timestamp.today().normalize() - pd.Timedelta(days=years * 365)).strftime("%Y-%m-%d")
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": code, "cosd": start}, timeout=30)
    r.raise_for_status()
    from io import StringIO
    df = pd.read_csv(StringIO(r.text))
    df.columns = ["date", code]
    df[code] = pd.to_numeric(df[code], errors="coerce")
    return df.dropna()


def update_vix_term(outdir: Path) -> tuple[bool, str | None, str | None]:
    """VIX (30-day) and VIX3M (3-month) from FRED -> data/options/VIX_TERM.csv."""
    try:
        a, b = fetch_fred_series("VIXCLS"), fetch_fred_series("VXVCLS")
        df = a.merge(b, on="date", how="inner")
        df.to_csv(outdir / "VIX_TERM.csv", index=False)
        return True, str(df["date"].iloc[-1]), None
    except Exception as exc:  # noqa: BLE001
        return False, None, f"{type(exc).__name__}: {exc}"[:200]


VOL_INDEX = {   # Cboe's official 30-day implied vol index for each desk instrument, as republished by FRED
    "SPX": ("VIXCLS", "VIX"), "NDX": ("VXNCLS", "VXN"), "DJX": ("VXDCLS", "VXD"), "RUT": ("RVXCLS", "RVX"), "GOLD": ("GVZCLS", "GVZ"),
}


def update_vol_indices(outdir: Path) -> tuple[bool, str | None, str | None]:
    """VIX, VXN, VXD, RVX, GVZ (and VIX3M) from FRED -> data/options/VOL_INDICES.csv, 3 years."""
    try:
        frames = [fetch_fred_series(code, 3) for code, _ in VOL_INDEX.values()] + [fetch_fred_series("VXVCLS", 3)]
        df = frames[0]
        for f_ in frames[1:]:
            df = df.merge(f_, on="date", how="outer")
        df = df.sort_values("date")
        df.to_csv(outdir / "VOL_INDICES.csv", index=False)
        return True, str(df["date"].iloc[-1]), None
    except Exception as exc:  # noqa: BLE001
        return False, None, f"{type(exc).__name__}: {exc}"[:200]


def oi_changes(raw: pd.DataFrame, prev_path: Path, spot: float, window: float) -> tuple[list[dict], pd.DataFrame]:
    cur = raw.groupby(["strike", "type"])["oi"].sum().unstack(fill_value=0.0).rename(columns={"C": "call_oi", "P": "put_oi"})
    for col in ("call_oi", "put_oi"):
        if col not in cur:
            cur[col] = 0.0
    cur = cur[["call_oi", "put_oi"]].reset_index()
    out = []
    if prev_path.exists():
        prev = pd.read_csv(prev_path)
        m = cur.merge(prev[["strike", "call_oi", "put_oi"]], on="strike", how="outer", suffixes=("", "_prev")).fillna(0.0)
        m = m[(m["strike"] >= spot * (1 - window)) & (m["strike"] <= spot * (1 + window))]
        m["dc"], m["dp"] = m["call_oi"] - m["call_oi_prev"], m["put_oi"] - m["put_oi_prev"]
        m["tot"] = m["dc"].abs() + m["dp"].abs()
        for _, r_ in m.sort_values("tot", ascending=False).head(6).iterrows():
            if r_["tot"] > 0:
                out.append({"k": float(r_["strike"]), "dc": int(r_["dc"]), "dp": int(r_["dp"])})
    return out, cur


# ---------------------------------------------------------------- driver
def update(data_dir: Path, fast: bool = False) -> dict[str, tuple[bool, str | None, str | None, str | None]]:
    """Returns key -> (ok, last_data_date, error, note) for status.json.

    fast=True (hourly run) skips the daily FRED vol-index files. In every mode, the open-interest baseline (<KEY>_oi.csv) and
    the OI change table are written only by the first run of each UTC day; later runs that day (fast or full) reuse that table,
    so the change always means "since the previous UTC day's first run", never "since the last hourly run"."""
    outdir = data_dir / "options"
    outdir.mkdir(parents=True, exist_ok=True)
    asof = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
    result = {}
    for key, (sym, label, mlabel, ysym, window) in INSTRUMENTS.items():
        try:
            raw, spot, stamp = fetch_chain(sym)
            a = analyse(raw, spot, asof, window)
            hp = outdir / f"{key}_history.csv"
            day = asof.strftime("%Y-%m-%d")
            carry = None   # not the first run of the UTC day (fast or full): reuse today's OI change table and leave the baseline alone
            if hp.exists() and (outdir / f"{key}.json").exists():
                try:
                    if str(pd.read_csv(hp)["date"].iloc[-1]) == day:
                        carry = json.loads((outdir / f"{key}.json").read_text()).get("oi_changes")
                except Exception:  # noqa: BLE001
                    carry = None
            changes, oi_now = oi_changes(raw, outdir / f"{key}_oi.csv", spot, window)
            if carry is not None:
                changes = carry
            mapped = fetch_last_close(ysym)
            snap = {"key": key, "label": label, "mapped_label": mapped and mlabel, "mapped_price": mapped,
                    "mapped_ratio": round(mapped / spot, 6) if mapped else None,
                    "date": day, "source_time": stamp, "updated_utc": asof.strftime("%Y-%m-%d %H:%M UTC"),
                    "oi_changes": changes, **a}
            try:   # the interactive Greek views; the level table above still works if this part fails
                snap["surface"] = build_surface(raw, spot, asof, window)
                df_w = prepare(raw, asof)
                df_w = df_w[(df_w["strike"] >= spot * (1 - window)) & (df_w["strike"] <= spot * (1 + window))]
                cols = [pd.Timestamp(e["e"]) for e in snap["surface"]["expiries"]]
                sig = (a["day_move"] / spot * 100) if a.get("day_move") else None
                snap["surface"]["scen"] = scenarios(df_w, spot, snap["surface"]["strikes"], cols, sig, snap["surface"].get("step"))
            except Exception as exc:  # noqa: BLE001
                snap["surface"] = {}
                snap["surface_error"] = f"{type(exc).__name__}: {exc}"[:200]
            try:   # volatility: term structure, skew, implied vs realised
                term = vol_term(raw, spot, asof)
                ysym_rv, f_rv, c_rv, _ = RV_SOURCE[key]
                rv = rv_from_closes(fetch_closes(ysym_rv)) or (realised_vol(data_dir / f_rv, c_rv) if f_rv else {})
                iv30 = iv_at(term, 30)
                snap["vol"] = {"term": term, "iv30": iv30, **rv,
                               "vrp": round(iv30 - rv["rv20"], 2) if iv30 is not None and "rv20" in rv else None,
                               "rv_source": RV_SOURCE[key][3]}
            except Exception as exc:  # noqa: BLE001
                snap["vol"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            (outdir / f"{key}.json").write_text(json.dumps(snap, separators=(",", ":")))
            if carry is None:
                oi_now.to_csv(outdir / f"{key}_oi.csv", index=False)
            row = pd.DataFrame([{**{k: snap[k] for k in ("date", "spot", "net_gex_bn", "gamma_flip", "call_wall", "put_wall",
                                                        "max_pain", "atm_iv", "pcr_oi", "pcr_vol", "mapped_price")},
                                 "iv30": (snap.get("vol") or {}).get("iv30"), "rv20": (snap.get("vol") or {}).get("rv20")}])
            hist = pd.concat([pd.read_csv(hp), row], ignore_index=True) if hp.exists() else row
            hist.drop_duplicates("date", keep="last").sort_values("date").tail(400).to_csv(hp, index=False)
            note = f"Cboe delayed chain {sym}, {a['contracts']} contracts in {a['expiries']} expiries" + ("; OI change held from the first run of the day" if carry is not None else "") + ("" if mapped else f"; {ysym} price unavailable, levels shown in {label} units only")
            result[key] = (True, day, None, note)
        except Exception as exc:  # noqa: BLE001 - keep the previous files, report in status
            result[key] = (False, None, f"{type(exc).__name__}: {exc}"[:200], None)
    if fast:   # FRED daily series: left to the full run, status entries stay as saved
        return result
    ok, last, err = update_vix_term(outdir)
    result["VIX_TERM"] = (ok, last, err, "FRED VIXCLS and VXVCLS (VIX3M)" if ok else None)
    ok, last, err = update_vol_indices(outdir)
    result["VOL_INDICES"] = (ok, last, err, "FRED VIX, VXN, VXD, RVX, GVZ, VIX3M" if ok else None)
    return result


if __name__ == "__main__":
    for k, v in update(Path(__file__).resolve().parent / "data").items():
        print(k, v)
