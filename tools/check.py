"""The standard quality gate. Every builder runs it before handing work over, and every checker starts from it.

    python tools/check.py [--root DIR] [--out DIR] [--base origin/main] [--quick] [--selftest]

It runs, in order, and writes DIR/check.json plus DIR/check.md (default DIR: a fresh temp dir, printed):
  1. rules     - the working tree (committed + staged + unstaged + untracked) against the merge-base with BASE: no workflow, docs/ or data/ changes committed, no secrets or keys in
                 the code, no "buy"/"sell"/price-target wording added, playbook disclaimer kept
  2. playbook  - playbook.json loads
  3. build     - build_dashboard.py with the real data/ (into DIR, never into docs/)
  4. fixtures  - tools/fixtures.py into a copy of data/, then a second build, so the options desk is
                 exercised even when the sandbox cannot reach Cboe / Yahoo / FRED
  5. browser   - headless Chromium on every build: main page and #options, dark + light + phone,
                 console errors, empty page, horizontal scroll on phone; internal builds only: page switching,
                 every instrument x mode x Greek on the desk, desk grid persistence, phone with all panels open
  6. public    - public builds (options desk off, the default) must hold no trace of the desk: HTML strings, nav,
                 P.opt, status rows, fixture numbers, xlsx sheet names; #options shows the normal dashboard
  7. chart     - Velour chart tab on the internal fixture: nothing third-party before the card opens, the pinned
                 KLineChart (SRI-verified copy cached under OUT/cache) draws, persists, exports/imports, follows themes.
                 CDN unreachable = WARN "chart: SKIPPED (CDN unreachable)", not FAIL
Builds: real/ (public, real data), fixture-public/, fixture/ (internal), fixture-gold-stale/ (internal).
--root DIR checks that repo instead of the one holding this file (for a copy of this script saved elsewhere).
--selftest checks the secret patterns only (also run as part of step 1).
--quick skips step 4 and the click-through (for a fast loop while building).
Exit code 0 only when everything passed. Never report a check as passed unless this printed PASS.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHROME = next((str(p) for p in Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome")), None)
BAD_WORDS = re.compile(r"\b(buy|sell|price target|go long|go short)\b", re.I)
MARKER = "# check: not-a-secret"
SECRETS = [   # (a) assignment / JSON / header, (b) bearer token, (c) URL query
    re.compile(r"""(?:api[_-]?key|apikey|(?<![a-z])key(?![a-z])|token|secret|password|passwd|bearer|authorization)[\w-]*["']?\s*[=:]\s*["']([A-Za-z0-9._+/=\-]{16,})["']""", re.I),
    re.compile(r"""(?:api[_-]?key|apikey|secret|token|password|passwd)[\w-]*["']?\s*[=:]\s*["']([A-Za-z0-9._+/=\-]{12,})["']""", re.I),   # short values, clear key names only
    re.compile(r"bearer\s+[A-Za-z0-9._+/=\-]{20,}", re.I),
    re.compile(r"[?&](?:api[_-]?key|apikey|key|token|access_token)=[A-Za-z0-9._+/\-]{12,}", re.I),
]
MARK_FILE = ".macro-check"


def is_secret(line: str) -> bool:
    """Only the secret patterns honour the marker; path rules and wording never do."""
    if line.rstrip().endswith(MARKER):
        return False
    return any(p.search(line) for p in SECRETS)


def selftest() -> list[str]:
    h32, h20, c20, c30 = "a1b2c3d4" * 4, "0123456789abcdef0123", "Zx9Qw3Er5Ty7Ui1Op2As", "Zx9Qw3Er5Ty7Ui1Op2As4Df6Gh8Jk"
    must = [
        f'FRED_API_KEY = "{h32}"', f'KEY = "{h32}"', f'FRED_API_KEY_HARD = "{h20}"',
        f'url = "https://api.stlouisfed.org/fred?api_key={h32}"', f'"apiKey": "{c20}"',
        '{"x-rapidapi-key": "' + c20 + '"}', f"Authorization: Bearer {c30}",
        'secret = "ab.cd+ef/gh1234567890=="',  # check: not-a-secret
        'token = "abc123abc123abc"',  # check: not-a-secret
        'api_key = "abc123abc123"',  # check: not-a-secret
        'password: "hunter2hunter2"',  # check: not-a-secret
    ]
    must_not = [
        'key = os.environ.get("FRED_API_KEY")', 'api_key = os.environ.get("FRED_API_KEY", "")',
        '"series_id": "DGS10"', "token_count = 3",
        'author = "abcdefghijklmnop"', 'self.keyboard = "abcdefghijkl"', 'author = "abcdefghijklmnopqrstuv"',
        'keyboard = "abcdefghijklmnopqrstuv"', f'KEY = "{h32}"  {MARKER}',
    ]
    errs = [f"selftest: secret NOT detected: {l[:60]}" for l in must if not is_secret(l)]
    errs += [f"selftest: false positive: {l[:60]}" for l in must_not if is_secret(l)]
    if not bad_wording(f'"Buy now"  {MARKER}'):
        errs.append("selftest: the marker must not exempt advice-like wording")
    return errs


def bad_wording(line: str) -> list[str]:
    """Reader-visible text in LINE (strings, markup, backticks) containing advice-like words."""
    out = []
    for txt in re.findall(r'"([^"]{3,})"|>([^<]{3,})<|`([^`]{3,})`', line):
        t = next(x for x in txt if x)
        if BAD_WORDS.search(t) and "option writers" not in t:
            out.append(t[:100])
    return out


def allowed_roots() -> list[Path]:
    roots = [Path("/tmp").resolve(), Path(tempfile.gettempdir()).resolve()]
    for var in ("TEAM_DIR", "CLAUDE_SCRATCHPAD"):
        if os.environ.get(var):
            roots.append(Path(os.environ[var]).resolve())
    return roots


def guard_out(path: Path, wipe: bool) -> Path:
    """Exit 2 unless PATH is a safe output dir: under /tmp, the scratchpad or $TEAM_DIR (never $TEAM_DIR itself),
    and, when it will be wiped, empty / missing / carrying the marker file a previous run wrote."""
    p = path.resolve()
    team = Path(os.environ["TEAM_DIR"]).resolve() if os.environ.get("TEAM_DIR") else None
    ok = any(p != r and (r in p.parents) for r in allowed_roots()) and p != team and p != ROOT
    if ok and wipe and p.exists() and any(p.iterdir()) and not (p / MARK_FILE).exists():
        ok = False
    if not ok:
        print(f"refusing to delete {p}" if wipe else f"refusing to write to {p}", file=sys.stderr)
        raise SystemExit(2)
    return p


def sh(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True, env=env)


def git(args: list[str]) -> subprocess.CompletedProcess:
    return sh(["git", "--literal-pathspecs", "-c", "core.quotepath=off", *args])


def first_err(r: subprocess.CompletedProcess) -> str:
    return (r.stderr.strip().splitlines() or ["no output"])[0]


def check_rules(base: str) -> tuple[list[str], str]:
    errs: list[str] = selftest()
    r = git(["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"])
    mb = git(["merge-base", base, "HEAD"]) if r.returncode == 0 else r
    if r.returncode != 0 or mb.returncode != 0:
        return errs + [f"cannot diff against {base} (fetch it first)"], "rules: not run"
    mbs = mb.stdout.strip()
    r = git(["diff", "--name-only", "-z", mbs])
    u = git(["ls-files", "-o", "--exclude-standard", "-z"])
    for x in (r, u):
        if x.returncode != 0:
            return errs + ["git failed: " + first_err(x)], "rules: not run"
    tracked = [n for n in r.stdout.split("\0") if n]
    untracked = [n for n in u.stdout.split("\0") if n]
    names = tracked + untracked
    h = git(["diff", "--name-only", "-z", "HEAD"])
    if h.returncode != 0:
        return errs + ["git failed: " + first_err(h)], "rules: not run"
    uncommitted = len({n for n in h.stdout.split("\0") if n} | set(untracked))
    note = f"rules: scanned {len(names)} files ({uncommitted} uncommitted/untracked)"
    st = git(["status", "--porcelain"])
    if st.returncode != 0:
        return errs + ["git failed: " + first_err(st)], note
    if not names and st.stdout.strip():
        errs.append("rules scanned 0 files but git status is not clean (vacuous run)")

    added: dict[str, list[str]] = {}   # file -> added lines
    for n in tracked:   # one diff per file, name passed literally: no git-quoted headers to parse
        d = git(["diff", "-U0", "--no-renames", mbs, "--", n])
        if d.returncode != 0:
            return errs + ["git failed: " + first_err(d)], note
        in_hunk = False
        for line in d.stdout.splitlines():
            if line.startswith("@@"):
                in_hunk = True
            elif in_hunk and line.startswith("+"):
                added.setdefault(n, []).append(line[1:])
    for n in untracked:
        try:
            added.setdefault(n, []).extend((ROOT / n).read_text(errors="ignore").splitlines())
        except OSError as exc:
            errs.append(f"cannot read untracked file {n}: {exc}")

    for n in names:
        if n.startswith(".github/workflows/"):
            errs.append(f"workflow changed: {n} (owner-only)")
        if n.startswith("docs/") or (n.startswith("data/") and not n.startswith("data/README")):
            errs.append(f"build output or data committed or left untracked: {n}")
    for n, lines in added.items():
        for line in lines:
            if is_secret(line):
                errs.append(f"possible secret in {n} (line text withheld)")
    for f in ("playbook.json", "template.html", "build_dashboard.py", "chart_kline.js", "chart_kline.css"):
        for l in added.get(f, []):   # only text a reader sees: strings and markup, not code identifiers
            for t in bad_wording(l):
                errs.append(f"advice-like wording in {f}: {t}")
    for f in ("chart_kline.js", "chart_kline.css"):   # the desk and chart use --call/--put (brand tokens), never the page's up/down colours
        try:
            txt = (ROOT / f).read_text(errors="ignore")
        except OSError:
            continue
        if re.search(r"var\(\s*--(up|down)\b", txt):
            errs.append(f"{f} uses var(--up)/var(--down); use var(--primary)/var(--secondary) (or --call/--put)")
    try:
        pb = json.loads((ROOT / "playbook.json").read_text())
        if not (pb.get("meta") or {}).get("disclaimer"):
            errs.append("playbook.json meta.disclaimer missing")
    except Exception:  # noqa: BLE001 - reported by the playbook step
        pass
    return errs, note


def desk_flag_supported() -> bool:
    try:
        return "--options-desk" in (ROOT / "build_dashboard.py").read_text()
    except OSError:
        return False


def build(data_dir: Path, out_html: Path, desk: bool | None = None) -> tuple[bool, str]:
    """desk=True/False passes --options-desk on/off (internal / public build); None leaves the default (public).
    OPTIONS_DESK_PUBLIC is removed from the environment so only the flag decides."""
    cmd = [sys.executable, "build_dashboard.py", "--data-dir", str(data_dir), "--out", str(out_html)]
    if desk is not None and desk_flag_supported():
        cmd += ["--options-desk", "on" if desk else "off"]
    env = {k: v for k, v in os.environ.items() if k != "OPTIONS_DESK_PUBLIC"}
    r = sh(cmd, env=env)
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-6:])
    ok = r.returncode == 0 and out_html.exists() and "Traceback" not in r.stderr
    return ok, tail


def stale_banner_checks() -> list[str]:
    """Direct tests of build_opt_stale wording (no browser needed)."""
    import importlib
    from datetime import date
    sys.path.insert(0, str(ROOT))
    try:
        bd = importlib.import_module("build_dashboard")
    except Exception as exc:  # noqa: BLE001
        return [f"stale banner: cannot import build_dashboard: {exc}"]
    errs, today = [], date(2026, 11, 21)
    ok = {"ok": True, "error": None}
    e403 = {"ok": False, "error": "HTTPError: 403 Client Error: Forbidden for url: x"}
    eto = {"ok": False, "error": "ReadTimeout: HTTPSConnectionPool timed out"}
    opts = {k: {"date": "2026-11-20", "source_time": "2026-11-20 01:00:00"} for k in ("SPX", "NDX", "DJX", "RUT")}
    opts["GOLD"] = {"date": "2026-11-19", "source_time": "2026-11-19 01:05:07"}
    base = {f"OPT_{k}": dict(ok) for k in ("SPX", "NDX", "DJX", "RUT", "GOLD")}
    base["OPT_VIX_TERM"] = dict(e403)   # not a chain source: must be ignored
    def run(over):
        return bd.build_opt_stale({"status": {**base, **over}, "options": opts}, today)
    h = run({})
    if h:
        errs.append("stale banner: shown although all five chain sources are ok")
    h = run({"OPT_GOLD": e403})
    if not h or "Gold" not in h or "19 Nov 2026" not in h or "2 days old" not in h:
        errs.append("stale banner: gold-only failure must name Gold with 19 Nov 2026 and 2 days old")
    if h and ("S&amp;P" in h or "Nasdaq" in h or "20 Nov" in h or "is not updating." in h.replace("Some options data is not updating.", "")):
        errs.append("stale banner: gold-only failure names other instruments or claims the whole desk is stale")
    if h and "refusing automated downloads" not in h:
        errs.append("stale banner: a 403 error must use the refusing wording")
    h = run({"OPT_GOLD": eto})
    if not h or "refusing" in h or "could not be refreshed" not in h:
        errs.append("stale banner: a timeout error must use neutral wording, not the refusing wording")
    if "Gold could not be refreshed" not in run({"OPT_GOLD": eto}):
        errs.append("stale banner: partial-failure wording must name the instrument, not a dangling 'They'")
    h = run({"OPT_SPX": eto, "OPT_NDX": eto, "OPT_DJX": eto, "OPT_RUT": eto})
    if h.count("as of 20 Nov 2026") != 1:
        errs.append("stale banner: instruments with the same date must share one sentence")
    h = run({"OPT_GOLD": e403})
    if 'data-stale="GOLD"' not in h:
        errs.append("stale banner: data-stale must list exactly the failing instrument keys")
    h = run({f"OPT_{k}": e403 for k in ("SPX", "NDX", "DJX", "RUT", "GOLD")})
    if "Options data is not updating." not in h:
        errs.append("stale banner: all five failing must say the desk is not updating")
    return errs


def gold_only_stale_browser(html: Path) -> list[str]:
    """A gold-only failure must mark Gold (feed line and note) as stale and leave SPX alone."""
    from playwright.sync_api import sync_playwright
    errs = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME) if CHROME else p.chromium.launch()
        try:
            pg = b.new_page(viewport={"width": 1440, "height": 900})
            pg.goto(html.as_uri() + "#options")
            pg.wait_for_timeout(900)
            if not pg.locator("#od-inst button[data-v='SPX']").count() or not pg.locator("#od-inst button[data-v='GOLD']").count():
                return ["gold-only stale: fixture has no SPX/GOLD buttons"]
            for inst, want in (("SPX", False), ("GOLD", True)):
                pg.click(f"#od-inst button[data-v='{inst}']")
                pg.wait_for_timeout(200)
                stale = "stale" in (pg.get_attribute("#od-feed", "class") or "").split()
                note = "last price on the desk" in pg.inner_text("#od-mnote")
                if stale != want or note != want:
                    errs.append(f"{inst}: feed stale={stale}, note stale={note}; expected {want}")
        finally:
            b.close()
    return errs


def browser(html: Path, full: bool, stale: bool | None = None, desk: bool = True) -> list[str]:
    """desk=False is a public build: #options must show the normal dashboard, and none of the desk checks run."""
    from playwright.sync_api import sync_playwright
    errs = []
    url = html.as_uri()
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME) if CHROME else p.chromium.launch()
        try:
            for theme, w in (("dark", 1440), ("light", 1440), ("dark", 390)):
                for frag in ("", "#options"):
                    tag = f"{theme} {w}px {frag or 'main'}"
                    pg = b.new_page(viewport={"width": w, "height": 900}, color_scheme=theme)
                    log = []
                    try:
                        pg.on("pageerror", lambda e, log=log: log.append(str(e)))
                        pg.on("console", lambda m, log=log: log.append(m.text) if m.type == "error" and "fonts.g" not in m.text else None)
                        pg.goto(url + frag)
                        pg.wait_for_timeout(900)
                        if len(pg.inner_text("body").strip()) < 500:
                            errs.append(f"{tag}: page nearly empty")
                        if w < 500 and pg.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
                            errs.append(f"{tag}: horizontal scroll on phone")
                        if frag == "#options" and desk and pg.locator("#odesk").count() and pg.is_visible("#overview"):
                            errs.append(f"{tag}: dashboard sections visible on the options page")
                        if frag == "#options" and not desk and not pg.is_visible("#overview"):
                            errs.append(f"{tag}: public build: #options did not fall back to the normal dashboard (blank page?)")
                        if not desk and pg.locator("#options, #odesk").count():
                            errs.append(f"{tag}: public build contains the options desk")
                        if frag == "#options" and stale is not None and w > 1000 and theme == "dark":
                            shown = pg.locator("#opt-stale").count() > 0 and pg.is_visible("#opt-stale")
                            if stale and not shown:
                                errs.append(f"{tag}: stale-data banner missing although an OPT_ source is failing")
                            if not stale and pg.locator("#opt-stale").count():
                                errs.append(f"{tag}: stale-data banner present although all OPT_ sources are ok")
                            if stale and shown and not re.search(r"\d+ days? old|as of", pg.inner_text("#opt-stale")) and "no saved options data" not in pg.inner_text("#opt-stale"):
                                errs.append(f"{tag}: stale-data banner shows no age in days")
                        if frag == "" and desk and pg.locator("#options").count() and pg.is_visible("#options"):
                            errs.append(f"{tag}: options desk visible on the main page")
                        if full and desk and frag == "#options" and w > 1000 and theme == "dark" and pg.locator("#od-inst button").count():
                            for inst in pg.eval_on_selector_all("#od-inst button", "bs => bs.map(b => b.dataset.v)"):
                                pg.click(f"#od-inst button[data-v='{inst}']")
                                for g in ("gamma", "delta", "vanna", "charm", "theta", "vega"):
                                    if pg.locator(f"#od-greek button[data-v='{g}']:not([disabled])").count():
                                        pg.click(f"#od-greek button[data-v='{g}']")
                                for m in ("eff", "shadow", "trad"):
                                    if pg.locator(f"#od-mode button[data-v='{m}']:not([disabled])").count():
                                        pg.click(f"#od-mode button[data-v='{m}']")
                                pg.select_option("#od-ex", index=min(2, pg.locator("#od-ex option").count() - 1))
                            if pg.locator("nav.sections a[href='#fx']").count():
                                pg.click("nav.sections a[href='#fx']")
                                pg.wait_for_timeout(400)
                                if pg.is_visible("#odesk") or not pg.is_visible("#fx"):
                                    errs.append(f"{tag}: nav from options page to FX did not switch pages")
                        errs += [f"{tag}: console: {m[:160]}" for m in log]
                    except Exception as exc:  # noqa: BLE001 - one broken view must not hide the others
                        errs.append(f"{tag}: view crashed: {type(exc).__name__}: {str(exc)[:160]}")
                    finally:
                        pg.close()
        finally:
            b.close()
    return errs


def _num_forms(v: float) -> list[str]:
    forms = {f"{v:,.2f}", f"{v:.2f}", f"{v:,.1f}", f"{v:,.0f}", f"{v:.0f}"} if abs(v - round(v)) < 1e-9 else {f"{v:,.2f}", f"{v:.2f}", f"{v:,.1f}"}
    return sorted(forms)


def public_absence(html: Path, label: str, fixture_snap: dict | None) -> list[str]:
    """AC1: a public build (options desk off) holds no trace of the desk."""
    errs = []
    h = html.read_text(errors="ignore")
    for pat, what in ((r'id="options"', 'id="options"'), (r'href="#options"', 'href="#options" nav link'), (r'"opt"\s*:', '"opt": key in the payload'),
                      (r"odesk", "odesk"), (r'id="od-', 'id="od-..." elements'), (r"OPT_[A-Z_]+", "OPT_* status rows"), (r"\.dpanel|data-panel=", "desk panels"),
                      (r"(?i)options levels", "'options levels' text"), (r"(?i)opt_key|opt_units", "opt_key/opt_units in the chart feed")):
        if re.search(pat, h):
            errs.append(f"[{label}] public build still contains {what}")
    if fixture_snap:   # fixture numbers must not be in the visible text (tags and scripts stripped)
        vis = re.sub(r"<(script|style)\b.*?</\1>", " ", h, flags=re.S | re.I)
        vis = re.sub(r"<[^>]+>", " ", vis)
        king = (fixture_snap.get("king") or {}).get("k")
        for name, v in (("call wall", fixture_snap.get("call_wall")), ("put wall", fixture_snap.get("put_wall")), ("gamma flip", fixture_snap.get("gamma_flip")),
                        ("max pain", fixture_snap.get("max_pain")), ("king strike", king)):
            if isinstance(v, (int, float)):
                for f in _num_forms(float(v)):
                    if re.search(r"(?<![\d.,])" + re.escape(f) + r"(?![\d,]|\.\d)", vis):
                        errs.append(f"[{label}] fixture options number {name} {f} appears in the public page text")
                        break
    xl = html.parent / "Macro_Dashboard.xlsx"
    if not xl.exists():
        errs.append(f"[{label}] Macro_Dashboard.xlsx was not written next to the page")
    else:
        try:
            import openpyxl
            names = openpyxl.load_workbook(xl, read_only=True).sheetnames
            if any("option" in n.lower() for n in names):
                errs.append(f"[{label}] xlsx has an options sheet: {names}")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"[{label}] xlsx unreadable: {type(exc).__name__}: {exc}")
    return errs


def desk_grid_browser(html: Path) -> list[str]:
    """Internal build: move one panel, reload, the order persisted, Reset restores it; phone has no horizontal scroll."""
    from playwright.sync_api import sync_playwright
    errs = []
    order_js = "Array.from(document.querySelectorAll('.dpanel[data-panel]')).map(e => e.dataset.panel)"
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME) if CHROME else p.chromium.launch()
        try:
            pg = b.new_page(viewport={"width": 1440, "height": 900}, color_scheme="dark")
            log = []
            pg.on("pageerror", lambda e: log.append(str(e)))
            pg.on("console", lambda m: log.append(m.text) if m.type == "error" and "fonts.g" not in m.text else None)
            try:
                pg.goto(html.as_uri() + "#options")
                pg.evaluate("try { localStorage.removeItem('macro-desk-layout') } catch (e) {}")
                pg.reload()
                pg.wait_for_timeout(900)
                before = pg.evaluate(order_js)
                if len(before) < 5:
                    return [f"desk grid: only {len(before)} .dpanel[data-panel] elements"]
                first = before[0]
                btn = pg.locator(f".dpanel[data-panel='{first}'] button").filter(has_text=re.compile(r"[▼↓v]|down", re.I))
                if not btn.count():   # accept an aria-label / title instead of visible text
                    btn = pg.locator(f".dpanel[data-panel='{first}'] button[aria-label*='down' i], .dpanel[data-panel='{first}'] button[title*='down' i], .dpanel[data-panel='{first}'] [data-act='down']")
                if not btn.count():
                    return ["desk grid: no move-down button found in the first panel header (text/aria-label/title with 'down' or data-act='down')"]
                btn.first.click()
                pg.wait_for_timeout(300)
                moved = pg.evaluate(order_js)
                if moved == before:
                    errs.append("desk grid: move-down did not change the order")
                pg.reload()
                pg.wait_for_timeout(900)
                after = pg.evaluate(order_js)
                if after != moved:
                    errs.append(f"desk grid: order did not survive a reload ({moved[:4]} -> {after[:4]})")
                saved = pg.evaluate("(() => { try { return JSON.parse(localStorage.getItem('macro-desk-layout')) } catch (e) { return null } })()")
                if not saved or saved.get("v") != 1 or not isinstance(saved.get("order"), list):
                    errs.append(f"desk grid: localStorage macro-desk-layout is not {{v:1, order:[...]}}: {str(saved)[:80]}")
                rb = pg.get_by_role("button", name=re.compile("reset layout", re.I))
                if not rb.count():
                    errs.append("desk grid: no 'Reset layout' button")
                else:
                    rb.first.click()
                    pg.wait_for_timeout(300)
                    if pg.evaluate(order_js) != before:
                        errs.append("desk grid: Reset layout did not restore the default order")
                    pg.reload()
                    pg.wait_for_timeout(600)
                    if pg.evaluate(order_js) != before:
                        errs.append("desk grid: default order not kept after Reset and reload")
                errs += [f"desk grid: console: {m[:160]}" for m in log]
            except Exception as exc:  # noqa: BLE001
                errs.append(f"desk grid: crashed: {type(exc).__name__}: {str(exc)[:160]}")
            finally:
                pg.close()
            # phone, every panel open: no horizontal page scroll
            pg = b.new_page(viewport={"width": 390, "height": 900}, color_scheme="dark")
            try:
                pg.goto(html.as_uri() + "#options")
                pg.evaluate("try { localStorage.removeItem('macro-desk-layout') } catch (e) {}")
                pg.reload()
                pg.wait_for_timeout(900)
                for _ in range(3):   # open every collapsed panel (a collapsed panel has aria-expanded=false on its toggle)
                    t = pg.locator(".dpanel button[aria-expanded='false']")
                    if not t.count():
                        break
                    t.first.click()
                    pg.wait_for_timeout(120)
                if pg.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
                    errs.append("desk grid: horizontal scroll on a 390 px phone with all panels open")
            except Exception as exc:  # noqa: BLE001
                errs.append(f"desk phone: crashed: {type(exc).__name__}: {str(exc)[:160]}")
            finally:
                pg.close()
        finally:
            b.close()
    return errs


KL_URL = "https://cdn.jsdelivr.net/npm/klinecharts@10.1.1/dist/umd/klinecharts.min.js"
KL_REASON = {"why": ""}   # "mismatch" when the downloaded bytes failed the SRI check; "unreachable" otherwise
KL_SRI = "sha384-xkC4/84Bj5cSxBeqZuaHxfU6OGkQpaAtTHc5gwE/kJd4vt7vRtFvslHtVTEq11Zl"


def cached_klinecharts(out: Path) -> Path | None:
    """Download the pinned library once into OUT/cache and verify the SRI hash; None when it cannot be had."""
    import base64
    import hashlib
    cdir = out / "cache"
    cdir.mkdir(parents=True, exist_ok=True)
    f = cdir / "klinecharts-10.1.1.min.js"
    try:
        if not f.exists():
            import requests
            r = requests.get(KL_URL, timeout=30)
            r.raise_for_status()
            f.write_bytes(r.content)
        got = "sha384-" + base64.b64encode(hashlib.sha384(f.read_bytes()).digest()).decode()
        if got != KL_SRI:
            f.unlink(missing_ok=True)
            KL_REASON["why"] = "mismatch"
            return None
        return f
    except Exception:  # noqa: BLE001
        KL_REASON["why"] = "unreachable"
        return None


def chart_browser(html: Path, out: Path) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Internal fixture build, dark 1440."""
    from playwright.sync_api import sync_playwright
    errs, warns = [], []
    lib = cached_klinecharts(out)
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME) if CHROME else p.chromium.launch()
        try:
            ctx = b.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
            pg = ctx.new_page()
            reqs, log = [], []
            pg.on("request", lambda r: reqs.append(r.url))
            pg.on("pageerror", lambda e: log.append(str(e)))
            pg.on("console", lambda m: log.append(m.text) if m.type == "error" and "fonts.g" not in m.text else None)
            if lib:
                ctx.route("**/cdn.jsdelivr.net/npm/klinecharts@10.1.1/**", lambda route: route.fulfill(
                    status=200, body=lib.read_bytes(), headers={"content-type": "application/javascript", "access-control-allow-origin": "*"}))
            try:
                pg.goto(html.as_uri())
                pg.wait_for_timeout(1500)
                early = [u for u in reqs if "cdn.jsdelivr.net" in u or "tradingview" in u.lower()]
                if early:
                    errs.append(f"chart: third-party request before the card was opened: {early[0][:100]}")
                if pg.locator("#tvcard").count() == 0 or pg.locator("#kl-root").count() == 0:
                    return errs + ["chart: #tvcard / #kl-root not found in the internal build"], warns
                pg.click("#tvcard > summary")
                pg.wait_for_timeout(500)
                if pg.locator("#chart-tabs button[data-v='kline']").count() == 0 or pg.locator("#chart-tabs button[data-v='live']").count() == 0:
                    errs.append("chart: #chart-tabs must have kline and live buttons")
                pg.click("#chart-tabs button[data-v='kline']")
                pg.wait_for_timeout(500)
                if any("tradingview" in u.lower() for u in reqs):
                    errs.append("chart: TradingView was requested although only the Velour tab was chosen")
                if lib is None and KL_REASON["why"] == "mismatch":
                    errs.append("chart: pinned KLineChart failed its SRI hash check (downloaded bytes differ from KL_SRI): FAIL, not skipped")
                    return errs, warns
                if lib is None:
                    warns.append("chart: SKIPPED (CDN unreachable): pinned KLineChart could not be downloaded; draw/persist/export checks not run")
                    errs += [f"chart: console: {m[:160]}" for m in log if "jsdelivr" not in m.lower() and "klinecharts" not in m.lower() and "ERR_" not in m and "Failed to load resource" not in m]
                    return errs, warns
                get_state = """async () => {
                    await VelourChart.ready;
                    const c = VelourChart.chart();
                    return {ov: c.getOverlays().length, ind: c.getIndicators().map(i => i.name)};
                }"""
                open_js = "async () => { const d = document.getElementById('tvcard'); if (!d.open) d.querySelector('summary').click(); }"
                pg.evaluate("() => VelourChart.ready")
                draw = """async () => {
                    await VelourChart.ready;
                    const c = VelourChart.chart(), dl = c.getDataList(), b = dl[Math.max(0, dl.length - 20)];
                    c.createOverlay({name: 'horizontalStraightLine', points: [{timestamp: b.timestamp, value: b.close}]});
                    c.createIndicator('MA');
                    VelourChart.save();
                    return {ov: c.getOverlays().length, ind: c.getIndicators().map(i => i.name), sym: VelourChart.symbol()};
                }"""
                d = pg.evaluate(draw)
                if d["ov"] < 1 or "MA" not in d["ind"]:
                    errs.append(f"chart: overlay/indicator not created: {d}")
                pg.wait_for_timeout(600)   # debounced save
                pg.reload()
                pg.wait_for_timeout(800)
                pg.evaluate(open_js)
                pg.click("#chart-tabs button[data-v='kline']")
                st = pg.evaluate(get_state)
                if st["ov"] < 1:
                    errs.append("chart: drawing did not survive a reload")
                if "MA" not in st["ind"]:
                    errs.append("chart: indicator did not survive a reload")
                text = pg.evaluate("() => VelourChart.exportJSON()")
                try:
                    js = json.loads(text)
                    if js.get("v") != 1 or "symbols" not in js:
                        errs.append("chart: exportJSON is not {v:1, symbols:{...}}")
                except ValueError:
                    errs.append("chart: exportJSON did not return JSON")
                pg.evaluate("() => { try { localStorage.removeItem('macro-kline') } catch (e) {} }")
                pg.reload()
                pg.wait_for_timeout(800)
                pg.evaluate(open_js)
                pg.click("#chart-tabs button[data-v='kline']")
                cleared = pg.evaluate(get_state)
                if cleared["ov"] != 0:
                    errs.append(f"chart: storage cleared but {cleared['ov']} drawing(s) still shown")
                res = pg.evaluate("async (t) => { await VelourChart.ready; return VelourChart.importJSON(t); }", text)
                if not res or not res.get("ok"):
                    errs.append(f"chart: importJSON rejected the export: {res}")
                pg.wait_for_timeout(500)
                after = pg.evaluate(get_state)
                if after["ov"] < 1 or "MA" not in after["ind"]:
                    errs.append(f"chart: import did not restore drawing and indicator: {after}")
                bad = pg.evaluate("() => VelourChart.importJSON('{\"v\":99,\"symbols\":{\"NOPE\":{}}}')")
                if bad and bad.get("ok"):
                    errs.append("chart: importJSON accepted an unknown schema version / symbol")
                for t in ("light", "dark", "auto", "light"):
                    pg.click(f"[data-theme-btn='{t}']")
                    pg.wait_for_timeout(300)
                if pg.evaluate("() => { const r = document.getElementById('kl-root'); return !!r && r.clientWidth > 100; }") is False:
                    errs.append("chart: #kl-root has no width after theme switches")
                errs += [f"chart: console: {m[:160]}" for m in log]
            except Exception as exc:  # noqa: BLE001
                errs.append(f"chart: crashed: {type(exc).__name__}: {str(exc)[:200]}")
            finally:
                ctx.close()
        finally:
            b.close()
    return errs, warns


def empty_state_browser(html: Path) -> list[str]:
    """Internal build with no options JSON and no chart data: both empty states render and the Live tab still works."""
    from playwright.sync_api import sync_playwright
    errs = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME) if CHROME else p.chromium.launch()
        try:
            pg = b.new_page(viewport={"width": 1440, "height": 900}, color_scheme="dark")
            log = []
            pg.on("pageerror", lambda e: log.append(str(e)))
            pg.on("console", lambda m: log.append(m.text) if m.type == "error" and "fonts.g" not in m.text and "tradingview" not in m.text.lower() and "ERR_" not in m.text and "Failed to load resource" not in m.text else None)
            try:
                pg.goto(html.as_uri() + "#options")
                pg.wait_for_timeout(900)
                if "No options data yet" not in pg.inner_text("body"):
                    errs.append("empty state: 'No options data yet' card missing when there is no options JSON")
                pg.goto(html.as_uri() + "#overview")
                pg.wait_for_timeout(500)
                pg.click("#tvcard > summary")
                pg.click("#chart-tabs button[data-v='kline']")
                pg.wait_for_timeout(600)
                if "Chart data not available" not in pg.inner_text("#kl-root"):
                    errs.append("empty state: 'Chart data not available' missing in the Velour tab when there is no chart data")
                pg.click("#chart-tabs button[data-v='live']")
                pg.wait_for_timeout(400)
                if not pg.is_visible("#tv-live"):
                    errs.append("empty state: the Live tab does not show when there is no chart data")
                errs += [f"empty state: console: {m[:160]}" for m in log]
            except Exception as exc:  # noqa: BLE001
                errs.append(f"empty state: crashed: {type(exc).__name__}: {str(exc)[:160]}")
            finally:
                pg.close()
        finally:
            b.close()
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None, help="repo to check (default: the repo containing this file)")
    ap.add_argument("--out", default=None, help="output dir (default: a fresh temp dir)")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="check the secret patterns and exit")
    a = ap.parse_args()
    global ROOT
    if a.root:
        ROOT = Path(a.root).resolve()
        if not (ROOT / "build_dashboard.py").exists():
            print(f"--root {ROOT} is not the macro-data repo", file=sys.stderr)
            return 2
    if a.selftest:
        errs = selftest()
        print("selftest: " + ("PASS" if not errs else "FAIL\n" + "\n".join(errs)))
        return 1 if errs else 0
    if a.out:
        out = guard_out(Path(a.out), wipe=True)
        shutil.rmtree(out, ignore_errors=True)
    else:
        out = Path(tempfile.mkdtemp(prefix="macro-check-"))
    out.mkdir(parents=True, exist_ok=True)
    (out / MARK_FILE).write_text("written by tools/check.py; safe to delete\n")
    print(f"output dir: {out}")
    res: dict[str, dict] = {}

    rule_errs, rules_note = check_rules(a.base)
    res["rules"] = {"errors": rule_errs}
    try:
        json.loads((ROOT / "playbook.json").read_text())
        res["playbook"] = {"errors": []}
    except Exception as exc:  # noqa: BLE001
        res["playbook"] = {"errors": [f"playbook.json invalid: {exc}"]}

    flag_ok = desk_flag_supported()
    if not flag_ok:
        res["switch"] = {"errors": ["build_dashboard.py has no --options-desk flag yet (P2 not landed): public/internal builds cannot be told apart"]}
    ok, tail = build(ROOT / "data", out / "real" / "index.html", desk=False)
    res["build"] = {"errors": [] if ok else ["real-data build failed:\n" + tail]}

    pub = []       # (html, label, fixture snapshot or None): public builds
    internal = []  # internal builds with the desk
    if ok:
        pub.append((out / "real" / "index.html", "real-public", None))
    warns: list[str] = []
    fx_data = None
    if not a.quick:
        fx_data = out / "fixture-data"
        shutil.copytree(ROOT / "data", fx_data)
        shutil.rmtree(fx_data / "options", ignore_errors=True)
        sp = fx_data / "status.json"   # fixture = healthy options feed, so the stale banner must be off
        try:
            stj = json.loads(sp.read_text())
            for k in stj:
                if k.startswith("OPT_") and isinstance(stj[k], dict):
                    stj[k].update(ok=True, error=None)
            sp.write_text(json.dumps(stj))
        except (OSError, ValueError):
            pass   # no status.json in the checkout: fixture build then just has no banner data, which is fine
        r = sh([sys.executable, "tools/fixtures.py", str(fx_data)])
        errs = [] if r.returncode == 0 else ["fixtures failed:\n" + (r.stdout + r.stderr)[-800:]]
        ok2, tail2 = build(fx_data, out / "fixture" / "index.html", desk=True)
        if not ok2:
            errs.append("internal fixture build failed:\n" + tail2)
        else:
            internal.append(out / "fixture" / "index.html")
        ok2p, tail2p = build(fx_data, out / "fixture-public" / "index.html", desk=False)
        if not ok2p:
            errs.append("public fixture build failed:\n" + tail2p)
        else:
            pub.append((out / "fixture-public" / "index.html", "fixture-public", json.loads((fx_data / "options" / "SPX.json").read_text())))
        res["fixtures"] = {"errors": errs}
        if ok2:   # gold-only failure on top of the healthy fixture: only Gold may be shown as stale
            g_data = out / "fixture-gold-stale-data"
            shutil.copytree(fx_data, g_data)
            try:
                gj = json.loads((g_data / "status.json").read_text())
                gj["OPT_GOLD"] = {"ok": False, "error": "HTTPError: 403 Client Error: Forbidden", "checked_utc": "2026-11-21T00:00:00Z"}
                (g_data / "status.json").write_text(json.dumps(gj))
                okg, tailg = build(g_data, out / "fixture-gold-stale" / "index.html", desk=True)
                if okg:
                    res["fixtures"]["errors"] += [f"[gold-stale] {e}" for e in gold_only_stale_browser(out / "fixture-gold-stale" / "index.html")]
                else:
                    res["fixtures"]["errors"].append("gold-stale internal fixture build failed:\n" + tailg)
            except (OSError, ValueError) as exc:
                res["fixtures"]["errors"].append(f"gold-stale fixture setup failed: {exc}")
            e_data = out / "empty-data"   # no options JSON, no chart sources
            shutil.copytree(fx_data, e_data)
            shutil.rmtree(e_data / "options", ignore_errors=True)
            for rel in ("GOLD.csv", "DXY.csv", "DGS10.csv", "idx", "fx"):
                q = e_data / rel
                shutil.rmtree(q, ignore_errors=True) if q.is_dir() else q.unlink(missing_ok=True)
            oke, taile = build(e_data, out / "empty" / "index.html", desk=True)
            if oke:
                res["fixtures"]["errors"] += empty_state_browser(out / "empty" / "index.html")
            else:
                res["fixtures"]["errors"].append("empty-data internal build failed:\n" + taile)

    berrs = []
    berrs += stale_banner_checks()
    if flag_ok:
        for h, label, snap in pub:
            berrs += public_absence(h, label, snap)
    for h, label, _snap in pub:
        try:
            berrs += [f"[{label}] {e}" for e in browser(h, full=False, stale=None, desk=False)]
        except Exception as exc:  # noqa: BLE001
            berrs.append(f"[{label}] browser check crashed: {type(exc).__name__}: {exc}")
    for h in internal:
        try:
            berrs += [f"[internal-{h.parent.name}] {e}" for e in browser(h, full=True, stale=False, desk=True)]
            berrs += [f"[internal-{h.parent.name}] {e}" for e in desk_grid_browser(h)]
            cerrs, cwarns = chart_browser(h, out)
            berrs += [f"[internal-{h.parent.name}] {e}" for e in cerrs]
            warns += cwarns
        except Exception as exc:  # noqa: BLE001
            berrs.append(f"[internal-{h.parent.name}] browser check crashed: {type(exc).__name__}: {exc}")
    res["browser"] = {"errors": berrs}
    htmls = [h for h, _l, _s in pub] + internal

    passed = all(not v["errors"] for v in res.values())
    (out / "check.json").write_text(json.dumps({"passed": passed, "steps": res}, indent=1))
    md = [f"# check: {'PASS' if passed else 'FAIL'}" + (f" (with {len(warns)} WARN: " + "; ".join(w[:70] for w in warns) + ")" if warns else ""), ""]
    for k, v in res.items():
        md.append(f"- **{k}**: {'ok' if not v['errors'] else str(len(v['errors'])) + ' problem(s)'}")
        md += [f"  - {e}" for e in v["errors"][:20]]
    md += [f"WARN {w}" for w in warns]
    md.append(f"\n{rules_note}")
    md.append(f"Built pages: {', '.join(str(h) for h in htmls) or 'none'}")
    (out / "check.md").write_text("\n".join(md))
    print("\n".join(md))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
