"""Offline test data for the options feeds.

The sandbox that agents work in usually cannot reach Cboe, Yahoo or FRED. This file stands in for them:
it builds realistic Cboe-shaped option chains for every desk instrument (SPX, NDX, DJX, RUT, GLD), futures
and index closes in Yahoo's shape and VIX-family series in FRED's CSV shape, then runs the real
`options_levels.update()` against them twice (the second run moves some open interest, so the
open-interest-change table and history are exercised too).

    python tools/fixtures.py OUT_DIR          # writes OUT_DIR/options/*  (copy the real data/ in first if you want a full page)

Numbers here are made up. Never publish them and never describe them as market data.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import options_levels as ol  # noqa: E402


def fake_chain(root: str, spot: float, step: float, lo: float, hi: float, call_peak: float, put_peak: float, base_iv: float, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    today = pd.Timestamp.now("UTC").normalize().tz_localize(None)
    opts = []
    for d in list(range(0, 10)) + [14, 21, 30, 45, 60, 90]:
        exp = today + pd.Timedelta(days=d)
        if exp.weekday() >= 5:
            continue
        for k in np.arange(lo, hi + step, step):
            for cp in "CP":
                peak = call_peak if cp == "C" else put_peak
                oi = int(2000 * np.exp(-((k - peak) / (spot * 0.02)) ** 2) + 300 * np.exp(-((k - spot) / (spot * 0.05)) ** 2) + rng.integers(0, 50))
                iv = base_iv * (1 + 1.5 * max(0, (spot - k) / spot)) + 0.01
                intrinsic = max(0, (k - spot) if cp == "P" else (spot - k))
                px = intrinsic + spot * iv * np.sqrt(max(d, 1) / 365) * 0.4 * np.exp(-abs(k - spot) / (spot * 0.03))
                sym = f"{root}{exp.strftime('%y%m%d')}{cp}{int(round(k * 1000)):08d}"
                opts.append({"option": sym, "bid": round(px * 0.98, 2), "ask": round(px * 1.02, 2), "iv": round(iv, 4),
                             "open_interest": oi, "volume": int(oi * 0.2), "last_trade_price": round(px, 2)})
    return {"timestamp": "fixture", "data": {"current_price": spot, "options": opts}}


CHAINS = {
    "_SPX": ("SPXW", 5800, 5, 5000, 6600, 6000, 5600, 0.15, 1),
    "_NDX": ("NDXP", 21000, 10, 18000, 24000, 21800, 20200, 0.19, 3),
    "_DJX": ("DJX", 430, 1, 380, 480, 440, 420, 0.13, 4),
    "_RUT": ("RUTW", 2250, 5, 1900, 2600, 2350, 2150, 0.22, 5),
    "GLD": ("GLD", 380, 1, 320, 440, 395, 365, 0.18, 2),
}
MAPPED = {"ES=F": 5832.25, "GC=F": 4140.0, "NQ=F": 21110.5, "YM=F": 43120.0, "RTY=F": 2258.4}


class _Resp:
    def __init__(self, js=None, text=None):
        self._js, self.text = js, text

    def raise_for_status(self):
        pass

    def json(self):
        return self._js


def install(chains: dict) -> None:
    """Replace network calls inside options_levels with the fixtures above."""
    def fake_get(url, params=None, headers=None, timeout=None):
        for s, js in chains.items():
            if url.endswith(f"/{s}.json"):
                return _Resp(js)
        for y, px in MAPPED.items():
            if url.endswith(y):
                return _Resp({"chart": {"result": [{"indicators": {"quote": [{"close": [px - 5, px]}]}}]}})
        if "/chart/" in url:   # index closes for realised vol
            rng = np.random.default_rng(len(url)); n = 130
            closes = list(1000 * np.exp(np.cumsum(rng.normal(0, 0.01, n))))
            ts = [int((pd.Timestamp.today() - pd.Timedelta(days=n - i)).timestamp()) for i in range(n)]
            return _Resp({"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"close": closes}]}}]}})
        if "fredgraph" in url:
            code = params["id"]
            days = pd.bdate_range(end=pd.Timestamp.today(), periods=760)
            rng = np.random.default_rng(sum(map(ord, code)))
            base = 16 + np.cumsum(rng.normal(0, 0.4, len(days)))
            vals = np.clip(base * (1.08 if code == "VXVCLS" else 1.0) + (len(code) - 6), 9, 60)
            return _Resp(text="observation_date," + code + "\n" + "\n".join(f"{d:%Y-%m-%d},{v:.2f}" for d, v in zip(days, vals)))
        raise RuntimeError(f"fixtures: no fake for {url}")
    ol.requests.get = fake_get


def run(out: Path) -> dict:
    chains = {k: fake_chain(*v) for k, v in CHAINS.items()}
    install(chains)
    out.mkdir(parents=True, exist_ok=True)
    ol.update(out)
    for o in chains["_SPX"]["data"]["options"]:   # day 2: open interest moves at one strike
        if "C05900000" in o["option"]:
            o["open_interest"] += 500
    return ol.update(out)


def guard_out(path: Path) -> Path:
    """Only write under /tmp, the scratchpad or $TEAM_DIR (never $TEAM_DIR itself or the repo)."""
    p = path.resolve()
    roots = [Path("/tmp").resolve()] + [Path(os.environ[v]).resolve() for v in ("TEAM_DIR", "CLAUDE_SCRATCHPAD") if os.environ.get(v)]
    team = Path(os.environ["TEAM_DIR"]).resolve() if os.environ.get("TEAM_DIR") else None
    if not any(p != r and r in p.parents for r in roots) or p == team or p == ROOT:
        print(f"refusing to write to {p}", file=sys.stderr)
        raise SystemExit(2)
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description="Write fake Cboe-shaped option feeds into OUT_DIR/options (offline test data, never market data).")
    ap.add_argument("out_dir", help="directory to write options/ into (copy the real data/ in first for a full page)")
    out = guard_out(Path(ap.parse_args().out_dir))
    res = run(out)
    bad = {k: v for k, v in res.items() if not v[0]}
    for k, v in res.items():
        print(("OK   " if v[0] else "FAIL ") + k + "  " + (v[3] or v[2] or ""))
    for k in ol.INSTRUMENTS:
        s = json.loads((out / "options" / f"{k}.json").read_text())
        if s.get("surface_error"):
            bad[k + " surface"] = s["surface_error"]
            print("FAIL " + k + " surface: " + s["surface_error"])
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
