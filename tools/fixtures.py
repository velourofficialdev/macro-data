"""Offline test data for the options feeds.

The sandbox that agents work in usually cannot reach Tradier (no token), Yahoo or FRED. This file stands in for them:
it builds realistic Tradier-shaped option chains (expirations / chains / quotes) for every desk instrument (SPX, NDX, DJX, RUT, GLD), futures
and index closes in Yahoo's shape and VIX-family series in FRED's CSV shape, then runs the real
`options_levels.update()` against them twice (the second run moves some open interest, so the
open-interest-change table and history are exercised too).

    python tools/fixtures.py OUT_DIR          # writes OUT_DIR/options/*  (copy the real data/ in first if you want a full page)

Numbers here are made up. Never publish them and never describe them as market data.
"""
from __future__ import annotations

import argparse
import contextlib
import io
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
    """Tradier-shaped option rows (chains endpoint: symbol, root_symbol, strike, option_type, expiration_date, open_interest,
    volume, bid, ask, last, greeks.mid_iv / smv_vol) plus the underlying price for the quotes endpoint."""
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
                opts.append({"symbol": sym, "root_symbol": root, "strike": float(k), "option_type": "call" if cp == "C" else "put",
                             "expiration_date": exp.strftime("%Y-%m-%d"), "open_interest": oi, "volume": int(oi * 0.2),
                             "bid": round(px * 0.98, 2), "ask": round(px * 1.02, 2), "last": round(px, 2),
                             "greeks": {"mid_iv": round(iv, 4), "smv_vol": round(iv * 1.01, 4)}})
    return {"spot": spot, "options": opts}


CHAINS = {   # Tradier symbol -> fake_chain args
    "SPX": ("SPXW", 5800, 5, 5000, 6600, 6000, 5600, 0.15, 1),
    "NDX": ("NDXP", 21000, 10, 18000, 24000, 21800, 20200, 0.19, 3),
    "DJX": ("DJX", 430, 1, 380, 480, 440, 420, 0.13, 4),
    "RUT": ("RUTW", 2250, 5, 1900, 2600, 2350, 2150, 0.22, 5),
    "GLD": ("GLD", 380, 1, 320, 440, 395, 365, 0.18, 2),
}
FAKE_TOKEN = "fx-not-real-token"   # check: not-a-secret
MAPPED = {"ES=F": 5832.25, "GC=F": 4140.0, "NQ=F": 21110.5, "YM=F": 43120.0, "RTY=F": 2258.4}


class _Resp:
    def __init__(self, js=None, text=None, status=200):
        self._js, self.text, self.status_code = js, text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._js


def _tradier_fake(chains: dict, url: str, params: dict, headers: dict, log: list | None, fail: dict | None):
    """Answer Tradier sandbox calls the way the real API shapes them. Missing/wrong Authorization header -> 401.
    `fail` can force a status for a path suffix ({"/chains": 429}); an empty chain list gives {"options": null}."""
    params, headers = params or {}, headers or {}
    if log is not None:
        log.append((url, dict(params), dict(headers)))
    if headers.get("Authorization") != f"Bearer {FAKE_TOKEN}":
        return _Resp({"fault": {"faultstring": "Invalid Access Token"}}, status=401)
    path = url.split("/v1", 1)[1]
    for suffix, code in (fail or {}).items():
        if path.endswith(suffix):
            return _Resp({}, status=code)
    if path == "/markets/quotes":
        sym = params["symbols"]
        if sym not in chains or not chains[sym]["options"] and not chains[sym].get("spot"):
            return _Resp({"quotes": {"unmatched_symbols": {"symbol": sym}}})
        q = {"symbol": sym, "last": chains[sym]["spot"], "trade_date": int(pd.Timestamp.now("UTC").timestamp() * 1000)}
        return _Resp({"quotes": {"quote": q}})
    sym = params.get("symbol")
    rows = chains.get(sym, {}).get("options", [])
    if path == "/markets/options/expirations":
        dates = sorted({o["expiration_date"] for o in rows})
        return _Resp({"expirations": {"date": dates if len(dates) != 1 else dates[0]} if dates else None})
    if path == "/markets/options/chains":
        sel = [o for o in rows if o["expiration_date"] == params["expiration"]]
        return _Resp({"options": {"option": sel if len(sel) != 1 else sel[0]} if sel else None})
    raise RuntimeError(f"fixtures: no fake Tradier route {path}")


def install(chains: dict, log: list | None = None, fail: dict | None = None) -> None:
    """Replace network calls inside options_levels with the fixtures above (and set the fake token + no throttle)."""
    os.environ["TRADIER_TOKEN"] = FAKE_TOKEN
    ol.MIN_INTERVAL = 0.0

    def fake_get(url, params=None, headers=None, timeout=None):
        if "tradier.com" in url:
            return _tradier_fake(chains, url, params, headers, log, fail)
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
    for hp in (out / "options").glob("*_history.csv"):   # the first run counts as the previous UTC day's, so day 2 diffs against its baseline
        h = pd.read_csv(hp)
        h["date"] = (pd.Timestamp.now("UTC").normalize().tz_localize(None) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        h.to_csv(hp, index=False)
    for o in chains["SPX"]["options"]:   # day 2: open interest moves at one strike
        if "C05900000" in o["symbol"]:
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
        if want and "agree to be bound by" not in bd.build_footer({"status": {"FX_MACRO": {"note": note}}}):
            bad[k + " agree"] = "agreement sentence missing"
        if not want and "agree to be bound by" in bd.build_footer({"status": {"FX_MACRO": {"note": note}}}):
            bad[k + " agree"] = "agreement sentence shown without keyed route"
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


def _tree_hashes(root: Path, skip_dirs: tuple, skip_files: tuple) -> dict:
    import hashlib
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not rel.startswith(skip_dirs) and rel not in skip_files:
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def fast_mode_checks(work: Path) -> dict:
    """Fast (hourly) run: slow sources and files untouched, fast sources fail soft and update, status merged, OI baseline held,
    then the page still builds (also with no fast files at all). All network is faked."""
    import shutil
    import subprocess
    import requests
    import fed_rate_odds
    bad = {}
    for argv, env, want in (([], {}, False), (["--fast"], {}, True), ([], {"FAST_RUN": "1"}, True), ([], {"MODE": "Fast"}, True),
                            ([], {"MODE": "full"}, False), ([], {"FAST_RUN": "0"}, False), ([], {"FAST_RUN": ""}, False)):
        if ud.is_fast(argv, env) is not want:
            bad["is_fast"] = f"{argv} {env} should be {want}"
    data = work / "data"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(ROOT / "data", data)
    shutil.rmtree(data / "options", ignore_errors=True)
    for p in ("fx_macro.json",):
        if not (data / p).exists():
            (data / p).write_text(json.dumps({"updated": "2026-11-10T10:00:00.000Z", "markets": {"sp500": {"value": 1}}, "fx": {"EURUSD": {"price": 1.1}}, "econ": {}, "curve": {}, "news": [{"title": "old headline"}]}))
    run(data)                                         # seeds data/options with two full option runs
    fxm = json.loads((data / "fx_macro.json").read_text())
    fxm["news"] = [{"title": "old headline", "url": "https://x.example/o", "source": "Reuters", "publishedAt": None}]
    (data / "fx_macro.json").write_text(json.dumps(fxm))
    slow_status = {"DGS10": {"ok": True, "checked_utc": "2026-11-17 06:00 UTC", "last_data_date": "2026-11-16", "error": None, "note": None},
                   "FX_MACRO": {"ok": True, "checked_utc": "2026-11-17 06:00 UTC", "last_data_date": "2026-11-17", "error": None, "note": "source: own FRED build; FRED route: keyed"},
                   "VIX_TERM": {"ok": True, "checked_utc": "2026-11-17 06:00 UTC", "last_data_date": "2026-11-16", "error": None, "note": "FRED VIXCLS"},
                   "FX_EURUSD": {"ok": False, "checked_utc": "2026-11-17 06:00 UTC", "last_data_date": None, "error": "old failure", "note": None}}
    (data / "status.json").write_text(json.dumps({**slow_status, "GOLD_PRICE": {"ok": True, "checked_utc": "old", "last_data_date": "2026-11-01", "error": None, "note": None}}))
    FAST_DIRS, FAST_FILES = ("options/", "fed/", "idx/"), ("fx_macro.json", "GOLD.csv")
    slow0 = _tree_hashes(data, FAST_DIRS, FAST_FILES + ("status.json",))
    fast0 = _tree_hashes(data, (), ("status.json",))
    oi0 = (data / "options" / "SPX_oi.csv").read_bytes()
    oich0 = json.loads((data / "options" / "SPX.json").read_text())["oi_changes"]
    st = lambda: json.loads((data / "status.json").read_text())
    saved = (ud.DATA, requests.get, fed_rate_odds.update_snapshots, fed_rate_odds.fetch_event_calendar, ud.fetch_fed_odds, ud.fetch_news, ud.fetch_yahoo)
    ud.DATA = data
    import contextlib
    import io
    quiet = lambda: contextlib.redirect_stdout(io.StringIO())
    try:
        def boom(*a, **k): raise OSError("network down (fixture)")
        # A: every fast source fails -> old files kept, slow untouched, exit 1
        requests.get = boom
        fed_rate_odds.update_snapshots = fed_rate_odds.fetch_event_calendar = ud.fetch_fed_odds = ud.fetch_news = ud.fetch_yahoo = boom
        with quiet():
            rc = ud.main(fast=True)
        s1 = st()
        if rc != 1: bad["fast all fail exit"] = f"exit {rc}, want 1 when every fast source failed"
        if _tree_hashes(data, (), ("status.json",)) != fast0: bad["fast all fail files"] = "a file changed although every fetch failed"
        if any(s1.get(k) != v for k, v in slow_status.items()): bad["fast status slow"] = "slow status entries changed"
        if s1.get("RUN_MODE", {}).get("note", "")[:4] != "fast" or s1["RUN_MODE"].get("mode") != "fast": bad["run mode"] = str(s1.get("RUN_MODE"))
        if s1["GOLD_PRICE"]["ok"] or not s1["OPT_SPX"]["error"] or s1["FX_NEWS"]["ok"]: bad["fast status fail"] = "failed fast sources must record ok False + error"
        # B: fixtures -> fast sources update, slow files/status untouched
        chains = {k: fake_chain(*v) for k, v in CHAINS.items()}
        install(chains)
        rows = pd.DataFrame({"Date": pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=300)})
        ud.fetch_yahoo = lambda sym, col: rows.assign(**{col: np.linspace(100, 130, 300)})
        ud.fetch_news = lambda: ud.parse_news(FAKE_RSS)
        ud.fetch_fed_odds = lambda: ({"updated": pd.Timestamp.now("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"), "meetings": [{"event": "FIXT-1", "raw": {}}]},
                                     {"FIXT-1": pd.DataFrame({"date": ["2026-11-17"], "v": [1.0]})})
        fed_rate_odds.update_snapshots = lambda: (pd.DataFrame({"snapshot_date": ["2026-11-18"]}), [])
        fed_rate_odds.fetch_event_calendar = lambda: None
        with quiet():
            rc = ud.main(fast=True)
        s2 = st()
        if rc != 0: bad["fast exit"] = f"exit {rc}"
        if _tree_hashes(data, FAST_DIRS, FAST_FILES + ("status.json",)) != slow0: bad["fast slow files"] = "a slow file changed in a fast run"
        if any(s2.get(k) != v for k, v in slow_status.items()): bad["fast status merge"] = "slow status entries not preserved"
        for k in ("GOLD_PRICE", "IDX_ES", "IDX_NQ", "IDX_US30", "FED_ZQ", "FED_ODDS", "FX_NEWS", "OPT_SPX", "OPT_GOLD", "RUN_MODE"):
            if not s2.get(k, {}).get("ok") or s2[k]["checked_utc"] == "old": bad["fast status " + k] = str(s2.get(k))
        if any(k in s2 for k in ("VOL_INDICES",)) and s2["VOL_INDICES"].get("checked_utc") != s1.get("VOL_INDICES", {}).get("checked_utc"): bad["vol idx"] = "FRED vol-index entry must not be touched"
        if (data / "options" / "VIX_TERM.json").exists() and s2.get("VIX_TERM") != slow_status["VIX_TERM"]: bad["vix term"] = "VIX_TERM touched"
        f2 = json.loads((data / "fx_macro.json").read_text())
        if [n["title"] for n in f2["news"]][:1] != ["Fed & ECB weigh rates 'again'"] or not f2.get("news_updated"): bad["fast news"] = "news not replaced / news_updated missing"
        if any(f2[k] != fxm[k] for k in ("markets", "fx", "econ", "curve", "updated")): bad["fast fx_macro numbers"] = "markets/fx/econ/curve/updated changed in a fast run"
        if (data / "options" / "SPX_oi.csv").read_bytes() != oi0: bad["oi baseline"] = "OI baseline rewritten on a non-first run of the day"
        if json.loads((data / "options" / "SPX.json").read_text())["oi_changes"] != oich0: bad["oi changes"] = "OI change table not carried"
        if "held" not in (s2["OPT_SPX"]["note"] or ""): bad["oi note"] = "status note must say the OI change is held"
        if pd.read_csv(data / "GOLD.csv")["Date"].iloc[-1] != rows["Date"].iloc[-1].strftime("%Y-%m-%d"): bad["gold"] = "gold not updated"
        if s2["FX_NEWS"]["last_data_date"] != "2026-11-16": bad["news date"] = f"FX_NEWS last_data_date {s2['FX_NEWS']['last_data_date']}, want the newest headline date 2026-11-16"
        if not oich0: bad["oi seed"] = "fixture OI change table is empty, the carry checks below prove nothing"
        # B2: fast, fast, full on the same UTC day: OI change table and baseline stay as the day's first run made them
        with quiet():
            ud.main(fast=True)
            ud.main(fast=False)
        oich1 = json.loads((data / "options" / "SPX.json").read_text())["oi_changes"]
        if oich1 != oich0 or not oich1: bad["oi full same day"] = f"a later full run the same day changed the OI table: {oich1} vs {oich0}"
        if (data / "options" / "SPX_oi.csv").read_bytes() != oi0: bad["oi baseline full"] = "OI baseline rewritten by a later full run of the day"
        if st()["RUN_MODE"].get("mode") != "full": bad["run mode full"] = str(st().get("RUN_MODE"))
        # B3: next day (history last date moved back): the first run diffs against the held baseline, then replaces it
        n5900 = 0
        for o in chains["SPX"]["options"]:
            if "C05900000" in o["symbol"]:
                o["open_interest"] += 300
                n5900 += 1
        install(chains)
        hp = data / "options" / "SPX_history.csv"
        h = pd.read_csv(hp); h["date"] = "2000-01-01"; h.to_csv(hp, index=False)
        with quiet():
            ud.main(fast=True)
        nd = json.loads((data / "options" / "SPX.json").read_text())["oi_changes"]
        if [x["dc"] for x in nd if x["k"] == 5900.0] != [-200 * n5900]: bad["oi next day"] = f"next-day change should be -200 x {n5900} contracts at 5900 (chain base+300 vs baseline base+500): {nd}"
        if (data / "options" / "SPX_oi.csv").read_bytes() == oi0: bad["oi next day baseline"] = "next day's first run must replace the baseline"
        f2 = json.loads((data / "fx_macro.json").read_text())   # B2/B3 ran more fast runs: re-read the saved feed
        # C: empty news keeps the previous headlines and news_updated
        ud.fetch_news = lambda: []
        with quiet():
            ud.main(fast=True)
        f3 = json.loads((data / "fx_macro.json").read_text())
        if f3["news"] != f2["news"] or f3["news_updated"] != f2["news_updated"]: bad["empty news"] = "empty news must keep previous headlines"
        # D: no saved fx_macro.json -> fast news fails soft, nothing created
        (data / "fx_macro.json").unlink()
        with quiet():
            ud.main(fast=True)
        if (data / "fx_macro.json").exists() or st()["FX_NEWS"]["ok"]: bad["no saved feed"] = "fast run must not invent fx_macro.json"
        (data / "fx_macro.json").write_text(json.dumps(f2))
    finally:
        (ud.DATA, requests.get, fed_rate_odds.update_snapshots, fed_rate_odds.fetch_event_calendar, ud.fetch_fed_odds, ud.fetch_news, ud.fetch_yahoo) = saved
    # E: the page builds on the result, and with no fast files at all
    for name, prep in (("after fast run", None), ("no fast files", True)):
        d = data
        if prep:
            d = work / "data-empty"
            shutil.copytree(data, d)
            for sub in ("options", "fed", "idx"): shutil.rmtree(d / sub, ignore_errors=True)
            for f in ("fx_macro.json", "GOLD.csv"): (d / f).unlink(missing_ok=True)
        r = subprocess.run([sys.executable, "build_dashboard.py", "--data-dir", str(d), "--out", str(work / f"{name.replace(' ', '-')}.html")], cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0 or "Traceback" in r.stderr: bad["build " + name] = (r.stdout + r.stderr)[-300:]
    return bad


def tradier_checks(work: Path) -> dict:
    """Tradier source: parse into the columns analyse() needs, source/proxy fields, ETF proxy rescale, no-token skip,
    401 and 429 stop early, the token only ever in the Authorization header. All network is faked."""
    import shutil
    import requests
    bad = {}
    saved_get, saved_tok, saved_iv = requests.get, os.environ.get("TRADIER_TOKEN"), ol.MIN_INTERVAL
    quiet = lambda: contextlib.redirect_stdout(io.StringIO())
    chains = {k: fake_chain(*v) for k, v in CHAINS.items()}
    shutil.rmtree(work, ignore_errors=True)
    try:
        # 1. parse + fields (token present)
        log: list = []
        install(chains, log)
        df, spot, stamp = ol.fetch_chain("SPX")
        need = {"root", "expiry", "type", "strike", "oi", "volume", "iv", "bid", "ask", "last"}
        if not need <= set(df.columns) or set(df["type"]) != {"C", "P"} or not (df["iv"] > 0).all() or spot != 5800 or not stamp.endswith("UTC"):
            bad["parse"] = f"columns {sorted(df.columns)} types {set(df['type'])} spot {spot} stamp {stamp!r}"
        if abs(df["iv"].iloc[0] - chains["SPX"]["options"][0]["greeks"]["smv_vol"]) > 1e-9: bad["iv field"] = "iv must come from greeks.smv_vol (mid_iv as fallback)"
        d1 = work / "d1"
        with quiet():
            res = ol.update(d1, fast=True)
        snap = json.loads((d1 / "options" / "SPX.json").read_text())
        if not res["SPX"][0] or snap.get("source") != "Tradier sandbox, 15 min delayed" or snap.get("source_delay") != "15 min": bad["source"] = f"{res['SPX']} {snap.get('source')!r} {snap.get('source_delay')!r}"
        if "proxy" not in snap or snap["proxy"] is not None or snap.get("proxy_ratio") is not None: bad["proxy null"] = f"proxy {snap.get('proxy')!r} ratio {snap.get('proxy_ratio')!r}"
        if "Tradier" not in (res["SPX"][3] or "") or "proxy" in (res["SPX"][3] or ""): bad["note"] = str(res["SPX"][3])
        # token only in the Authorization header: never in a URL, a query parameter, another header, status text or the saved files
        if not log: bad["calls"] = "no Tradier calls were made"
        for url, prm, hdr in log:
            if FAKE_TOKEN in url or FAKE_TOKEN in json.dumps(prm) or [k for k, v in hdr.items() if FAKE_TOKEN in str(v) and k != "Authorization"]:
                bad["token leak"] = "token found outside the Authorization header"
            if not url.startswith("https://sandbox.tradier.com/v1/") or "User-Agent" in hdr: bad["request shape"] = f"{url} {sorted(hdr)}"
        blob = "".join(p_.read_text() for p_ in (d1 / "options").glob("*") if p_.suffix in (".json", ".csv")) + json.dumps({k: list(v) for k, v in res.items()})
        if FAKE_TOKEN in blob: bad["token in files"] = "token text found in options files or status tuples"
        # 2. proxy: SPX chain empty -> SPY chain rescaled by index price / ETF price, labelled
        ch2 = {k: v for k, v in chains.items()}
        ch2["SPX"] = {"spot": 5800.0, "options": []}
        ch2["SPY"] = fake_chain("SPY", 580, 0.5, 500, 660, 600, 560, 0.15, 7)
        install(ch2)
        d2 = work / "d2"
        with quiet():
            res2 = ol.update(d2, fast=True)
        s2 = json.loads((d2 / "options" / "SPX.json").read_text())
        if not res2["SPX"][0] or s2.get("proxy") != "SPY" or abs((s2.get("proxy_ratio") or 0) - 10.0) > 1e-6: bad["proxy"] = f"{res2['SPX']} proxy {s2.get('proxy')} ratio {s2.get('proxy_ratio')}"
        if abs(s2["spot"] - 5800) > 1e-6 or not (5000 <= (s2.get("call_wall") or 0) <= 6600) or "proxy" not in (res2["SPX"][3] or ""): bad["proxy scale"] = f"spot {s2['spot']} wall {s2.get('call_wall')} note {res2['SPX'][3]}"
        if s2.get("source") != ol.SOURCE: bad["proxy source"] = str(s2.get("source"))
        if json.loads((d2 / "options" / "GOLD.json").read_text()).get("proxy") is not None: bad["gold proxy"] = "GLD is its own chain, never a proxy"
        ch2["SPX"] = {"options": []}   # index has no quote either: the last index close from Yahoo scales the proxy
        install(ch2)
        with quiet():
            res2b = ol.update(work / "d2b", fast=True)
        if not res2b["SPX"][0] or json.loads((work / "d2b" / "options" / "SPX.json").read_text()).get("proxy") != "SPY": bad["proxy no quote"] = str(res2b["SPX"])
        del ch2["SPY"]   # empty index chain and no proxy chain: fails soft, others still run
        install(ch2)
        with quiet():
            res2c = ol.update(work / "d2c", fast=True)
        if res2c["SPX"][0] or not res2c["GOLD"][0]: bad["no proxy fail soft"] = str({k: v[:1] for k, v in res2c.items()})
        # 3. no token: skip, nothing fetched, previous files kept, status says what to add
        install(chains)
        before = _tree_hashes(d1, (), ())
        os.environ.pop("TRADIER_TOKEN", None)
        calls: list = []
        requests.get = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(OSError("no network expected"))
        with quiet():
            res3 = ol.update(d1, fast=True)
        if calls: bad["no token calls"] = f"{len(calls)} requests made without a token"
        if _tree_hashes(d1, (), ()) != before: bad["no token files"] = "files changed in a skipped run"
        if any(v[0] or "not configured: add secret TRADIER_TOKEN" not in (v[2] or "") for v in res3.values()): bad["no token status"] = str({k: v[:3] for k, v in res3.items()})
        if res3["SPX"][1] != snap["date"]: bad["no token last date"] = f"last_data_date {res3['SPX'][1]} want the kept snapshot date {snap['date']}"
        # 4. 401: stop at the first call, other instruments not tried, files kept
        log4: list = []
        install(chains, log4)
        os.environ["TRADIER_TOKEN"] = "wrong-" + FAKE_TOKEN
        before = _tree_hashes(d1, (), ())
        with quiet():
            res4 = ol.update(d1, fast=True)
        if len(log4) != 1 or any(v[0] for v in res4.values()) or "rejected" not in (res4["SPX"][2] or ""): bad["401"] = f"calls {len(log4)} {str({k: v[:3] for k, v in res4.items()})[:200]}"
        if _tree_hashes(d1, (), ()) != before: bad["401 files"] = "files changed after a 401"
        if "wrong-" in json.dumps({k: list(v) for k, v in res4.items()}): bad["401 leak"] = "token text in status tuples"
        # 5. 429 on the first chains call: stop, the rest skipped
        log5: list = []
        install(chains, log5, {"/chains": 429})
        with quiet():
            res5 = ol.update(d1, fast=True)
        n_chain = sum(1 for u, _, _ in log5 if u.endswith("/chains"))
        if n_chain != 1 or any(v[0] for v in res5.values()) or "429" not in (res5["SPX"][2] or "") or "skipped" not in (res5["NDX"][2] or ""): bad["429"] = f"chain calls {n_chain} {str({k: v[:3] for k, v in res5.items()})[:240]}"
        if _tree_hashes(d1, (), ()) != before: bad["429 files"] = "files changed after a 429"
    finally:
        requests.get, ol.MIN_INTERVAL = saved_get, saved_iv
        if saved_tok is None:
            os.environ.pop("TRADIER_TOKEN", None)
        else:
            os.environ["TRADIER_TOKEN"] = saved_tok
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
    ap = argparse.ArgumentParser(description="Write fake Tradier-shaped option feeds into OUT_DIR/options (offline test data, never market data).")
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
    for k, v in tradier_checks(out / "tradier-run").items():
        bad["tradier " + k] = v
        print("FAIL tradier " + k + ": " + v)
    if not any(k.startswith("tradier") for k in bad):
        print("OK   tradier (parse, source/proxy fields, ETF proxy rescale, no-token skip, 401/429 stop early, token header-only)")
    for k, v in fast_mode_checks(out / "fast-run").items():
        bad["fast_mode " + k] = v
        print("FAIL fast_mode " + k + ": " + v)
    if not any(k.startswith("fast_mode") for k in bad):
        print("OK   fast_mode (slow files/status untouched, fast sources fail soft, OI baseline held, page builds)")
    if not any(k.startswith("fred_key") for k in bad):
        print("OK   fred_key routes + redaction")
    if not any(k.startswith("fx_macro") for k in bad):
        print("OK   fx_macro build + RSS parser")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
