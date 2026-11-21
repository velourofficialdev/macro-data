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
    for hp in (out / "options").glob("*_history.csv"):   # the first run counts as the previous UTC day's, so day 2 diffs against its baseline
        h = pd.read_csv(hp)
        h["date"] = (pd.Timestamp.now("UTC").normalize().tz_localize(None) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        h.to_csv(hp, index=False)
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
        for o in chains["_SPX"]["data"]["options"]:
            if "C05900000" in o["option"]:
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


# ---------------------------------------------------------------- unit tests for the options desk maths, caps and chart feed
def _surf(strikes, g_all, g_ex0=None, dtes=(0, 1), c=None, p=None, smile=None, extra=None):
    sf = {"strikes": strikes, "expiries": [{"e": f"2026-11-{19 + i}", "dte": d} for i, d in enumerate(dtes)],
          "greeks": {"gamma": {"all": g_all, "ex": [g_ex0 or g_all, g_all]}},
          "walls": {"all": {"c": c or [0] * len(strikes), "p": p or [0] * len(strikes)}}}
    if smile:
        sf["smile"] = smile
    sf["greeks"].update(extra or {})
    return sf


def desk_unit_tests() -> dict:
    """Hand-computed expectations on tiny synthetic surfaces. Returns name -> message for every failure."""
    import csv
    import io
    import shutil
    import tempfile
    from datetime import date
    bad: dict = {}

    def check(name, cond, msg=""):
        if not cond:
            bad[name] = msg or "failed"

    K = [90.0, 95.0, 100.0, 105.0, 110.0]
    # hedging pressure: all positive -> b=1 score 0; all negative -> b=-1 score 100; cancelling -> 0 / 50; zeros -> 50
    h = ol.hedging_pressure(_surf(K, [1, 1, 1, 1, 1]), 100.0, 5.0)
    check("hp +1", h and h["balance"] == 1 and h["score"] == 0 and h["n"] == 5, str(h))
    h = ol.hedging_pressure(_surf(K, [-1, -1, -1, -1, -1]), 100.0, 5.0)
    check("hp -1", h and h["balance"] == -1 and h["score"] == 100, str(h))
    h = ol.hedging_pressure(_surf(K, [1, -1, 0, 1, -1]), 100.0, 5.0)
    check("hp 0", h and h["balance"] == 0 and h["score"] == 50, str(h))
    h = ol.hedging_pressure(_surf(K, [0, 0, 0, 0, 0]), 100.0, 5.0)
    check("hp zero denominator", h and h["balance"] == 0 and h["score"] == 50, str(h))
    h = ol.hedging_pressure(_surf(K, [-50.5, 49.5, 0, 0, 0]), 100.0, 5.0)   # b=-0.01 -> 50.5 -> 51 (half up; round-half-even would give 50)
    check("hp half-up", h and h["score"] == 51, str(h))
    h = ol.hedging_pressure(_surf(K, [-9, -9, 4, 9, 9]), 100.0, 2.0)   # window 96..104 keeps only strike 100 (G=4): b=1
    check("hp window", h and h["n"] == 1 and h["score"] == 0 and h["lo"] == 96 and h["hi"] == 104, str(h))
    h = ol.hedging_pressure(_surf(K, [1, 1, 1, 1, 1]), 100.0, None)   # fallback sigma = 2% of spot = 2 -> window 96..104
    check("hp fallback sigma", h and h["n"] == 1, str(h))
    check("hp none without strikes", ol.hedging_pressure({}, 100.0, 5.0) is None)

    # 0DTE magnet
    sf = _surf(K, [0, 0, 0, 0, 0], g_ex0=[1, -5, 2, 3, -1])
    z = ol.zero_dte(sf, 100.0, 16.0, 5.0)
    check("magnet pick", z and z["magnet"] == 95.0 and z["dist"] == -5.0 and z["dist_pct"] == -5.0 and z["dte"] == 0 and z["points"] == [90.0, 95.0, 100.0, 105.0, 110.0] and z["basis"] == "ATM IV, 1 trading day", str(z))
    z = ol.zero_dte(sf, 100.0, 16.0, 2.0)
    check("magnet window", z and z["magnet"] == 100.0, str(z))
    z = ol.zero_dte(sf, 100.0, 16.0, None)   # sigma = 100 * 0.16 * sqrt(1/252) = 1.008
    check("magnet sigma from IV", z and z["magnet"] == 100.0 and abs(z["sigma"] - 1.01) < 0.005, str(z))
    check("magnet none", ol.zero_dte(sf, 100.0, None, None) is None)
    sf2 = _surf(K, [0] * 5, g_ex0=[9, 9, 9, 9, 9], dtes=(2, 1))
    sf2["greeks"]["gamma"]["ex"] = [[9, 9, 9, 9, 9], [1, -5, 2, 3, -1]]   # nearest expiry = smallest dte = index 1
    z = ol.zero_dte(sf2, 100.0, 16.0, 5.0)
    check("magnet nearest expiry", z and z["dte"] == 1 and z["magnet"] == 95.0, str(z))

    # IV rank
    r = ol.iv_rank(list(range(1, 101)))
    check("rank top", r and r["rank"] == 100.0 and r["pct"] == 99.0 and r["n"] == 100 and r["lo"] == 1 and r["hi"] == 100, str(r))
    check("rank n<60", ol.iv_rank(list(range(59))) is None)
    check("rank n=60", ol.iv_rank(list(range(60))) is not None)
    r = ol.iv_rank([5.0] * 80)
    check("rank flat", r and r["rank"] == 50.0 and r["pct"] == 0.0, str(r))
    r = ol.iv_rank(list(range(300)))   # window 252 -> values 48..299
    check("rank window", r and r["n"] == 252 and r["lo"] == 48 and r["hi"] == 299 and r["rank"] == 100.0, str(r))
    r = ol.iv_rank([30.0] + [20.0 + (i % 5) for i in range(100)] + [float("nan"), 10.0])   # NaN dropped; last is the min
    check("rank bottom", r and r["rank"] == 0.0 and r["pct"] == 0.0, str(r))

    # skew and surface (OTM smile: puts below spot, calls at/above)
    sm1 = {"p": [[90, 30], [95, 25], [100, 22], [105, 99]], "c": [[90, 77], [100, 20], [105, 18], [110, 17]]}
    sm2 = {"p": [[96, 24]], "c": [[100, 20], [104, 19]]}
    sf = _surf(K, [0] * 5, smile=[sm1, sm2])
    sk = ol.skew_by_expiry(sf, 100.0)
    check("skew values", sk and sk[0]["iv"] == [30.0, 25.0, 20.0, 18.0, 17.0] and sk[0]["skew_95_105"] == 7.0, str(sk and sk[0]))
    check("skew none outside", sk and sk[1]["iv"] == [None, None, 20.0, None, None] and sk[1]["skew_95_105"] is None, str(sk and sk[1]))
    sx = ol.iv_surface(sf, 100.0, 0.96, 1.04, 0.02)
    check("surface interp", sx and sx["m"] == [0.96, 0.98, 1.0, 1.02, 1.04] and sx["iv"][1] == [24.0, 22.0, 20.0, 19.5, 19.0] and sx["dte"] == [0, 1], str(sx))
    sx = ol.iv_surface(sf, 100.0)
    check("surface grid", sx and len(sx["m"]) == 31 and sx["iv"][0][0] is None and sx["iv"][0][15] == 20.0, str(sx and sx["iv"][0][:3]))

    # walls
    sf = _surf(K, [0] * 5, c=[1, 3, 5, 2, 0], p=[-4, -1, 0, 0, -2], extra={"delta": {"all": [3, -6, 0, 2, 1]}})
    w = ol.major_walls(sf, "gamma", 2)
    check("walls gamma", w and [x["k"] for x in w["pos"]] == [100.0, 95.0] and [x["k"] for x in w["neg"]] == [90.0, 110.0]
          and abs(w["pos"][0]["share"] - 5 / 18) < 1e-3, str(w))
    check("walls share<=1", w and sum(x["share"] for x in w["pos"] + w["neg"]) <= 1.0 + 1e-9)
    w = ol.major_walls(sf, "delta", 5)
    check("walls delta", w and [x["k"] for x in w["pos"]] == [90.0, 105.0, 110.0] and [x["k"] for x in w["neg"]] == [95.0]
          and sum(x["share"] for x in w["pos"] + w["neg"]) <= 1.0 + 1e-9, str(w))
    w = ol.major_walls(sf, "conc", 1)
    check("walls conc", w and w["pos"][0]["k"] == 90.0 and w["pos"][0]["v"] == 5.0 and w["neg"] == [], str(w))   # gross 5,4,5,2,2: tie goes to the lowest strike
    check("walls none", ol.major_walls({}, "gamma") is None)
    ex = ol.desk_extras({"spot": 100.0, "surface": {}}, date(2026, 11, 21))
    check("desk_extras no raise", isinstance(ex, dict) and ex["hp"] is None and ex["walls"] is None and ex["ivr"] is None, str(ex))
    check("desk_extras garbage", isinstance(ol.desk_extras({"spot": "x", "surface": 5}, date(2026, 11, 21)), dict))

    # source_info: Cboe fallback vs Tradier fields
    today = date(2026, 11, 21)
    old = {"key": "SPX", "label": "SPX", "date": "2026-11-19", "updated_utc": "2026-11-19 03:45 UTC", "source_time": "x"}
    si = ol.source_info(old, "SPX", today)
    check("source fallback", si["text"] == "Cboe delayed chain · delayed · fetched 2026-11-19 03:45 UTC · 2 days old" and not si["approx"] and si["proxy"] is None and si["age_days"] == 2, si["text"])
    check("source licence default", "permission for public display pending" in si["licence_note"] and si["iv_source"] == "IV as quoted by Cboe")
    gold = {**old, "key": "GOLD", "mapped_ratio": 10.9}
    si = ol.source_info(gold, "GOLD", today)
    check("source gold proxy", si["approx"] and si["proxy"] == "GLD" and si["proxy_ratio"] == 10.9 and "GLD proxy scaled to COMEX gold" in si["text"] and "(approximate)" in si["text"], si["text"])
    tr = {**old, **TRADIER_FIELDS}
    si = ol.source_info(tr, "SPX", today)
    check("source tradier", si["approx"] and all(x in si["text"] for x in ("Tradier sandbox", "15 min delayed", "SPY proxy scaled to SPX ×10.02 (approximate)", "computed from option mid prices", "fetched 2026-11-19 03:45 UTC"))
          and "personal/internal" in si["licence_note"], si["text"])
    check("source never raises", isinstance(ol.source_info({}, "SPX", today)["text"], str) and isinstance(ol.source_info(None, "SPX", today), dict))
    sfld = ol.source_fields("SPX", {}, {"source": "Tradier sandbox"})
    check("source_fields", sfld["source"] == "Tradier sandbox" and sfld["source_delay"] == "delayed")

    # Pine and CSV
    snap = {"key": "SPX", "label": "SPX", "date": "2026-11-19", "updated_utc": "2026-11-19 03:45 UTC", "call_wall": 7400.0, "put_wall": 7000.0,
            "gamma_flip": None, "max_pain": 7515.0, "mapped_ratio": 1.007014}
    tv = ol.tv_levels("SPX", snap, {"symbol": "ES1!", "label": "ES (E-mini S&P 500 future)"})
    pine, rows = tv["pine"], list(csv.reader(io.StringIO(tv["csv"])))
    check("pine header", pine.startswith("//@version=5\n") and 'indicator("Velour options levels SPX", overlay=true)' in pine
          and 'input.bool(false, "Show in ES (E-mini S&P 500 future) units")' in pine and "Educational, not advice" in pine and "Cboe delayed chain" in pine, pine[:300])
    check("pine levels", pine.count("hline(") == 3 and "#90BFF9" in pine and "#B96AC9" in pine and "color.gray" in pine and "hline.style_dotted" in pine
          and "Gamma flip" not in pine.split("indicator(")[1], pine)
    check("pine ratio", "ratio = 1.00701" in pine and "7400.00 * ratio" in pine)
    check("csv", rows[0] == ["level", "instrument", "value", "mapped_symbol", "mapped_value", "source", "fetched_utc"]
          and len(rows) == 4 and rows[1] == ["call_wall", "SPX", "7400.00", "ES1!", "7451.90", "Cboe delayed chain", "2026-11-19 03:45 UTC"]
          and all(r[0] != "gamma_flip" for r in rows), str(rows))
    tv0 = ol.tv_levels("SPX", {**snap, "mapped_ratio": None}, None)
    check("pine no ratio", "ratio = 1.0" in tv0["pine"] and tv0["csv"].splitlines()[1].split(",")[3] == "", tv0["pine"])
    check("tv_levels empty", ol.tv_levels("GOLD", {}, None)["pine"].count("hline(") == 0)

    # snapshot caps on a temp dir
    tmp = Path(tempfile.mkdtemp(prefix="caps-"))
    try:
        opt = tmp / "options"
        (opt / "snapshots" / "sub").mkdir(parents=True)
        (tmp / "chart").mkdir()
        days = pd.date_range("2024-01-01", periods=600).strftime("%Y-%m-%d")
        pd.DataFrame({"date": list(days)[::-1], "spot": range(600)}).to_csv(opt / "X_history.csv", index=False)   # unsorted on purpose
        pd.DataFrame({"date": days[:100], "spot": range(100)}).to_csv(opt / "Y_history.csv", index=False)
        pd.DataFrame({"strike": [1, 2, 2, 3, 3], "call_oi": [1, 2, 3, 4, 5], "put_oi": [0] * 5}).to_csv(opt / "X_oi.csv", index=False)
        import os as _os
        import time as _t
        now = _t.time()
        for i in range(520):
            f = opt / "snapshots" / ("sub" if i % 2 else ".") / f"s{i:03d}.json"
            f.write_text("{}")
            _os.utime(f, (now - i * 60, now - i * 60))
        old_f = opt / "snapshots" / "old.json"
        old_f.write_text("{}")
        _os.utime(old_f, (now - 40 * 86400,) * 2)
        (tmp / "chart" / "a.json").write_text(json.dumps(list(range(3500))))
        dd = [0] + [1] * 3499
        (tmp / "chart" / "b.json").write_text(json.dumps({"symbols": [{"t0": "2020-01-01", "dd": dd, "c": list(range(3500))}]}))
        rem = ud.cap_snapshots(tmp)
        hx = pd.read_csv(opt / "X_history.csv")
        check("cap history rows", len(hx) == 400 and hx["date"].is_monotonic_increasing and hx["date"].iloc[-1] == days[-1] and hx["date"].iloc[0] == days[200] and rem["history_rows"] == 200, str(rem))
        check("cap history untouched", len(pd.read_csv(opt / "Y_history.csv")) == 100)
        oi = pd.read_csv(opt / "X_oi.csv")
        check("cap oi dedup", list(oi["strike"]) == [1, 2, 3] and list(oi["call_oi"]) == [1, 3, 5] and rem["oi_rows"] == 2, str(oi))
        left = [f for f in (opt / "snapshots").rglob("*") if f.is_file()]
        check("cap snapshot files", len(left) == 500 and not old_f.exists() and (opt / "snapshots" / "s000.json").exists() and rem["snapshot_files"] == 21, f"{len(left)} {rem}")
        check("cap chart list", json.loads((tmp / "chart" / "a.json").read_text()) == list(range(500, 3500)))
        sb = json.loads((tmp / "chart" / "b.json").read_text())["symbols"][0]
        d_first = pd.Timestamp(sb["t0"])
        check("cap chart dd", len(sb["c"]) == 3000 and len(sb["dd"]) == 3000 and sb["c"][0] == 500 and d_first == pd.Timestamp("2020-01-01") + pd.Timedelta(days=500)
              and sb["c"][-1] == 3499 and (d_first + pd.Timedelta(days=int(sum(sb["dd"])))) == pd.Timestamp("2020-01-01") + pd.Timedelta(days=3499), str(sb["t0"]))
        rem2 = ud.cap_snapshots(tmp)
        check("cap idempotent", not any(rem2.values()), str(rem2))
        check("cap missing dir", ud.cap_snapshots(tmp / "nope") == {"history_rows": 0, "oi_rows": 0, "snapshot_files": 0, "chart_points": 0})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # chart feed round trip
    import chart_feed as cf
    tmp = Path(tempfile.mkdtemp(prefix="feed-"))
    try:
        (tmp / "idx").mkdir()
        (tmp / "fx").mkdir()
        dts = ["2026-11-02", "2026-11-03", "2026-11-06", "2026-11-09", "2026-11-10"]
        pd.DataFrame({"Date": dts, "Gold": [4000.123, 4010.456, 4020.5, 4030.0, 4040.999]}).to_csv(tmp / "GOLD.csv", index=False)
        pd.DataFrame({"date": dts, "Close": [5000.5, 5001, 5002, 5003, 5004.25], "source": "Yahoo ^GSPC"}).to_csv(tmp / "idx" / "ES.csv", index=False)
        pd.DataFrame({"date": dts[::-1], "DGS10": [4.1, 4.2, "", 4.4, 4.5]}).to_csv(tmp / "DGS10.csv", index=False)   # unsorted + a blank
        (tmp / "status.json").write_text(json.dumps({"GOLD_PRICE": {"note": "source: Yahoo GC=F"}}))
        feed = cf.build_chart_feed(tmp)
        ids = [x["id"] for x in feed["symbols"]]
        check("feed ids", ids == ["GOLD", "SPX", "US10Y"], str(ids))
        def decode(sy):
            d, out = pd.Timestamp(sy["t0"]), []
            for g in sy["dd"]:
                d = d + pd.Timedelta(days=g)
                out.append(d.strftime("%Y-%m-%d"))
            return out
        g, spx, us = feed["symbols"]
        check("feed dates", decode(g) == dts and decode(spx) == dts and decode(us) == [d for d in dts if d != "2026-11-06"], str(decode(us)))
        check("feed values", g["c"] == [4000.12, 4010.46, 4020.5, 4030.0, 4041.0] and g["src"] == "Yahoo GC=F" and spx["src"] == "Yahoo ^GSPC" and us["src"] == "FRED DGS10, daily", str(g["c"]))
        check("feed shape", all(x["ohlc"] is False and x["intervals"] == ["D", "W"] and x["delay"] and len(x["dd"]) == len(x["c"]) for x in feed["symbols"]) and g["opt_key"] == "GOLD" and g["opt_units"] == "mapped" and spx["opt_units"] == "index" and feed["v"] == 1)
        check("feed max_points", len(cf.build_chart_feed(tmp, max_points=2)["symbols"][0]["c"]) == 2)
        check("feed empty dir", cf.build_chart_feed(tmp / "nope") ["symbols"] == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return bad


TRADIER_FIELDS = {"source": "Tradier sandbox", "source_delay": "15 min delayed", "proxy": "SPY", "proxy_ratio": 10.02,
                  "iv_source": "computed from option mid prices", "licence_note": "Tradier sandbox: personal/internal use only"}


def tradier_variant(out: Path) -> str:
    """Make the SPX fixture snapshot look like a Tradier sandbox one (fields only; numbers stay fake) so the labels are exercised."""
    p = out / "options" / "SPX.json"
    snap = json.loads(p.read_text())
    snap.update(TRADIER_FIELDS)
    p.write_text(json.dumps(snap, separators=(",", ":")))
    return "SPX fixture snapshot carries Tradier sandbox source fields"


def surface_checks(out: Path) -> dict:
    """The saved fixture snapshots must carry the new fields and feed every desk function."""
    from datetime import date
    bad = {}
    for k in ol.INSTRUMENTS:
        s = json.loads((out / "options" / f"{k}.json").read_text())
        sf = s.get("surface") or {}
        n = len(sf.get("strikes", []))
        for f in ("oi", "volu"):
            v = sf.get(f) or {}
            if not (len(v.get("c", [])) == n and len(v.get("p", [])) == n and n):
                bad[f"{k} surface.{f}"] = "missing or not aligned with strikes"
        if not all(s.get(f) is not None for f in ("source", "source_delay", "iv_source", "licence_note")):
            bad[f"{k} source fields"] = "not written to the snapshot"
        ex = ol.desk_extras(s, date.today())
        for part in ("hp", "magnet", "skew", "ivs", "walls"):
            if ex.get(part) is None:
                bad[f"{k} desk_extras.{part}"] = "None on a full fixture surface"
        if ex.get("walls"):
            for g, w in ex["walls"].items():
                if w and sum(x["share"] for x in w["pos"] + w["neg"]) > 1 + 1e-6:
                    bad[f"{k} walls {g}"] = "shares exceed 1"
        tv = ol.tv_levels(k, s, None)
        if "//@version=5" not in tv["pine"] or not tv["csv"].startswith("level,instrument"):
            bad[f"{k} tv_levels"] = "bad output"
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
    print("OK   " + tradier_variant(out))
    for k, v in surface_checks(out).items():
        bad["surface " + k] = v
        print("FAIL surface " + k + ": " + v)
    unit = desk_unit_tests()
    for k, v in unit.items():
        bad["unit " + k] = v
        print("FAIL unit " + k + ": " + v[:300])
    if not unit:
        print("OK   desk unit tests (hedging, magnet, rank, skew, surface, walls, source_info, Pine/CSV, caps, chart feed)")
    for k, v in fx_macro_checks().items():
        bad["fx_macro " + k] = v
        print("FAIL fx_macro " + k + ": " + v)
    for k, v in fred_notice_checks().items():
        bad["fred_notice " + k] = v
        print("FAIL fred_notice " + k + ": " + v)
    for k, v in fred_key_checks().items():
        bad["fred_key " + k] = v
        print("FAIL fred_key " + k + ": " + v)
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
