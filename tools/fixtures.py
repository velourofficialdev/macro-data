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
import update_data as ud  # noqa: E402


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


FAKE_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>x</title>
<item><title>Fed &amp;amp; ECB weigh &lt;b&gt;rates&lt;/b&gt; &#39;again&#39; - Reuters</title><link>http://news.example/a?oc=5</link><pubDate>Mon, 16 Nov 2026 01:47:00 GMT</pubDate></item>
<item><title>Dollar &quot;slips&quot; as yields ease - Reuters</title><link>https://news.example/b</link></item>
<item><title></title><link>https://news.example/empty</link></item>
""" + "".join(f"<item><title>Filler {i} - Reuters</title><link>https://news.example/f{i}</link><pubDate>Mon, 16 Nov 2026 0{i}:00:00 GMT</pubDate></item>" for i in range(8)) + "</channel></rss>"


def fx_macro_checks() -> dict:
    """Offline tests of the own FX feed build: RSS parser, FRED partial failure, news failure, total FRED failure."""
    bad = {}
    n = ud.parse_news(FAKE_RSS)
    if len(n) != 8: bad["rss count"] = f"{len(n)} items, want 8"
    elif n[0]["title"] != "Fed & ECB weigh rates 'again'": bad["rss title"] = n[0]["title"]
    elif n[0]["url"] != "https://news.example/a?oc=5" or n[0]["source"] != "Reuters" or n[0]["publishedAt"] != "2026-11-16T01:47:00.000Z": bad["rss item0"] = str(n[0])
    elif n[1]["title"] != 'Dollar "slips" as yields ease' or n[1]["publishedAt"] is not None: bad["rss item1"] = str(n[1])
    if ud.parse_news("<rss><channel></channel></rss>") != [] or ud.parse_news("not xml") != []: bad["rss empty"] = "empty/garbage feed must give []"
    rng = np.random.default_rng(7)
    def fred(code):
        if code == "DGS10": raise RuntimeError("boom")
        v = 100 + np.cumsum(rng.normal(0, 0.5, 450)) + (code == "GDP") * 0
        return pd.DataFrame({"date": pd.bdate_range("2025-01-01", periods=450).strftime("%Y-%m-%d"), code: v})
    feed, probs = ud.build_fx_macro(None, fred=fred, news=lambda: ud.parse_news(FAKE_RSS))
    if len(feed["markets"]) != 6 or len(feed["fx"]) != 7 or len(feed["econ"]) != 12 or set(feed["curve"]) != {"y2", "y10"}: bad["shape"] = "wrong group sizes"
    if feed["curve"]["y10"]["value"] is not None or feed["curve"]["y10"]["display"] != "-" or "DGS10" not in probs: bad["partial"] = "failed series must be value None / '-' and noted: " + probs
    if len(feed["markets"]["dxy"]["spark"]) != 30 or "confluence" not in feed["fx"]["EURUSD"] or len(feed["news"]) != 8: bad["fields"] = "spark/confluence/news missing"
    old = {"curve": {"y10": {"value": 5.0, "display": "5.00"}}, "news": [{"title": "old"}]}
    f2, _ = ud.build_fx_macro(old, fred=fred, news=lambda: (_ for _ in ()).throw(OSError("rss down")))
    if f2["curve"]["y10"]["value"] != 5.0 or f2["news"] != [{"title": "old"}]: bad["keep old"] = "previous values not kept on failure"
    try:
        ud.build_fx_macro(None, fred=lambda c: (_ for _ in ()).throw(OSError("down")), news=lambda: [])
        bad["all fail"] = "must raise so the fallback feed is used"
    except RuntimeError:
        pass
    f3, _ = ud.build_fx_macro(None, fred=fred, news=lambda: [])
    if f3["curve"]["y10"]["date"] != "" or f3["curve"]["y10"]["date"] is None: bad["empty date"] = "failed series without history must have date ''"
    if f3["markets"]["dxy"]["label"] != "USD BROAD" or f3["econ"]["boe_rate"]["label"] != "UK 3M RATE" or f3["econ"]["boj_rate"]["label"] != "BOJ CALL RATE" or f3["markets"]["gold"]["label"] != "GOLD IDX": bad["labels"] = "honest labels missing"
    ticks = iter(range(0, 10000, 50))   # fake clock: each call advances 50 s, budget 120 s
    calls = []
    def fred2(code):
        calls.append(code); return fred(code)
    try:
        ud.build_fx_macro(None, fred=fred2, news=lambda: [], budget=120, clock=lambda: next(ticks))
        bad["budget"] = "must raise once most series are cut by the time budget"
    except RuntimeError:
        pass
    if len(calls) > 4: bad["budget"] = f"{len(calls)} FRED fetches after budget, want <= 4"
    a, b = {"updated": "2026-11-17T10:00:00.000Z"}, {"updated": "2026-11-16T10:00:00.000Z"}
    if ud.fx_pick(a, b) != (a, True) or ud.fx_pick(b, a) != (a, False) or ud.fx_pick(None, a) != (a, False): bad["pick newer"] = "fallback must not overwrite a fresher previous feed"
    return bad


def fred_notice_checks() -> dict:
    """Footer notice: only for the keyed route note; csv-only, keyed-failed-only and missing status must not show it or crash."""
    import build_dashboard as bd
    bad = {}
    cases = {"keyed": ("FRED route: keyed x61", True), "keyed+csv": ("FRED route: keyed x50, csv, keyed failed x11", True),
             "csv": ("FRED route: csv, no key x61", False), "csv+failed": ("FRED route: csv, keyed failed x11", False)}
    for k, (note, want) in cases.items():
        got = bd.FRED_NOTICE in bd.build_footer({"status": {"FX_MACRO": {"note": note}}})
        if got != want: bad[k] = f"notice shown={got}, want {want}"
        if want and ("FRED API Terms of Use" not in bd.build_footer({"status": {"FX_MACRO": {"note": note}}})):
            bad[k + " link"] = "terms link missing"
    for k, (note, err, want) in {"keyed in error": ("", "FRED route: keyed x5, csv, keyed failed x2", True), "csv in error": ("", "FRED route: csv, no key x5", False)}.items():
        f = bd.build_footer({"status": {"FX_MACRO": {"note": note, "error": err}}})
        if (bd.FRED_NOTICE in f) != want or (want and "FRED API Terms of Use" not in f): bad[k] = "error route text mishandled"
    for k, d in {"no status": {"status": {}}, "no key": {}, "null note": {"status": {"FX_MACRO": {"note": None}}}}.items():
        try:
            if bd.build_footer(d) != "": bad[k] = "must be empty"
        except Exception as e:  # noqa: BLE001
            bad[k] = f"crashed: {e}"
    return bad


def fred_key_checks() -> dict:
    """Offline tests of the keyed FRED route: parse, no-key path, rejected key fallback, key redaction."""
    import contextlib
    import io
    import requests
    bad = {}
    KEY = "fakekey0123456789abcdef"  # check: not-a-secret
    saved_env, saved_get = os.environ.get("FRED_API_KEY"), requests.get
    CSV = "observation_date,DGS10\n2026-11-12,4.10\n2026-11-13,.\n2026-11-16,4.20\n"
    mode = {"m": "ok"}

    class R:
        def __init__(s, url, status=200, js=None, text=""):
            s.url, s.status_code, s._js, s.text = url, status, js, text

        def raise_for_status(s):
            if s.status_code >= 400:
                raise requests.HTTPError(f"{s.status_code} Client Error: Bad Request for url: {s.url}")

        def json(s):
            return s._js

    calls = []

    def fake_get(url, params=None, **kw):
        calls.append(url)
        if "api.stlouisfed.org" in url:
            full = url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
            if mode["m"] == "reject":
                return R(full, 400)
            if mode["m"] == "raise":
                raise requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {full}")
            return R(full, 200, {"observations": [{"date": "2026-11-12", "value": "4.10"}, {"date": "2026-11-13", "value": "."}, {"date": "2026-11-16", "value": "4.20"}]})
        return R(url, 200, text=CSV)

    requests.get = fake_get
    try:
        def routes():
            return {k: v for k, v in ud.FRED_ROUTES.items() if v}

        def reset():
            for k in ud.FRED_ROUTES:
                ud.FRED_ROUTES[k] = 0
            calls.clear()

        def shape(df):
            return list(df.columns) == ["date", "DGS10"] and list(zip(df["date"], df["DGS10"])) == [("2026-11-12", 4.10), ("2026-11-16", 4.20)]

        os.environ.pop("FRED_API_KEY", None); reset()
        df = ud.fetch_fred("DGS10")
        if not shape(df) or any("api.stlouisfed" in c for c in calls) or routes() != {"csv, no key": 1}:
            bad["no key"] = f"{routes()} {calls}"
        os.environ["FRED_API_KEY"] = KEY; reset()
        df = ud.fetch_fred("DGS10")
        if not shape(df) or routes() != {"keyed": 1} or any("fredgraph" in c for c in calls):
            bad["keyed parse"] = f"{df.to_dict('list')} {routes()}"
        for m in ("reject", "raise"):
            mode["m"] = m; reset()
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
                df = ud.fetch_fred("DGS10")
            if not shape(df) or routes() != {"csv, keyed failed": 1}:
                bad["fallback " + m] = str(routes())
            if KEY in buf.getvalue():
                bad["leak " + m] = "key printed"
        mode["m"] = "ok"
        # redaction: known key, unknown key in api_key=, and a real HTTPError text
        for t in (f"400 Client Error for url: https://api.stlouisfed.org/x?series_id=A&api_key={KEY}&file_type=json", f"k={KEY}", "api_key=someotherkey&x=1"):
            o = ud.scrub(t)
            if KEY in o or "someotherkey" in o or "***" not in o:
                bad["scrub"] = o
        try:
            R(f"{ud.FRED_API_URL}?api_key={KEY}", 400).raise_for_status()
            bad["scrub exc"] = "fake did not raise"
        except Exception as e:  # noqa: BLE001
            if KEY not in str(e):
                bad["scrub exc"] = "fixture error should contain the key before scrubbing"
            if KEY in ud.scrub(e):
                bad["scrub exc"] = "key survived scrub of exception text"
        ud.FRED_ROUTES["csv, keyed failed"] = 3
        note = ud.fred_route_note()
        if KEY in note or "csv, keyed failed x3" not in note:
            bad["route note"] = note
        src = (ROOT / "update_data.py").read_text()
        if "error = scrub(error)" not in src or "note = scrub(note)" not in src:
            bad["record scrub"] = "record() must scrub error and note"
        reset()
    finally:
        requests.get = saved_get
        if saved_env is None:
            os.environ.pop("FRED_API_KEY", None)
        else:
            os.environ["FRED_API_KEY"] = saved_env
    return bad


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
    for k, v in fx_macro_checks().items():
        bad["fx_macro " + k] = v
        print("FAIL fx_macro " + k + ": " + v)
    for k, v in fred_notice_checks().items():
        bad["fred_notice " + k] = v
        print("FAIL fred_notice " + k + ": " + v)
    for k, v in fred_key_checks().items():
        bad["fred_key " + k] = v
        print("FAIL fred_key " + k + ": " + v)
    if not any(k.startswith("fred_key") for k in bad):
        print("OK   fred_key routes + redaction")
    if not any(k.startswith("fx_macro") for k in bad):
        print("OK   fx_macro build + RSS parser")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
