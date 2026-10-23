"""The standard quality gate. Every builder runs it before handing work over, and every checker starts from it.

    python tools/check.py [--out DIR] [--base origin/main] [--quick] [--selftest]

It runs, in order, and writes DIR/check.json plus DIR/check.md (default DIR: a fresh temp dir, printed):
  1. rules     - the working tree (committed + staged + unstaged + untracked) against the merge-base with BASE: no workflow, docs/ or data/ changes committed, no secrets or keys in
                 the code, no "buy"/"sell"/price-target wording added, playbook disclaimer kept
  2. playbook  - playbook.json loads
  3. build     - build_dashboard.py with the real data/ (into DIR, never into docs/)
  4. fixtures  - tools/fixtures.py into a copy of data/, then a second build, so the options desk is
                 exercised even when the sandbox cannot reach Cboe / Yahoo / FRED
  5. browser   - headless Chromium on both builds: main page and #options, dark + light + phone,
                 console errors, empty page, horizontal scroll on phone, page switching, every
                 instrument x mode x Greek on the desk
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
    re.compile(r"""[\w-]*(?:api[_-]?key|apikey|key|token|secret|password|passwd|bearer|auth)[\w-]*["']?\s*[=:]\s*["']([A-Za-z0-9._+/=\-]{16,})["']""", re.I),
    re.compile(r"bearer\s+[A-Za-z0-9._+/=\-]{20,}", re.I),
    re.compile(r"[?&](?:api[_-]?key|apikey|key|token|access_token)=[A-Za-z0-9._+/\-]{12,}", re.I),
]
MARK_FILE = ".macro-check"


def is_secret(line: str) -> bool:
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
    ]
    must_not = [
        'key = os.environ.get("FRED_API_KEY")', 'api_key = os.environ.get("FRED_API_KEY", "")',
        '"series_id": "DGS10"', "token_count = 3", f'KEY = "{h32}"  {MARKER}',
    ]
    errs = [f"selftest: secret NOT detected: {l[:60]}" for l in must if not is_secret(l)]
    errs += [f"selftest: false positive: {l[:60]}" for l in must_not if is_secret(l)]
    return errs


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


def sh(cmd: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def git(args: list[str]) -> subprocess.CompletedProcess:
    return sh(["git", *args])


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
    d = git(["diff", "-U0", mbs, "--", "."])
    if d.returncode != 0:
        return errs + ["git failed: " + first_err(d)], note
    cur = None
    for line in d.stdout.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("+") and cur:
            added.setdefault(cur, []).append(line[1:])
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
    for f in ("playbook.json", "template.html", "build_dashboard.py"):
        for l in added.get(f, []):   # only text a reader sees: strings and markup, not code identifiers
            for txt in re.findall(r'"([^"]{3,})"|>([^<]{3,})<|`([^`]{3,})`', l):
                t = next(x for x in txt if x)
                if BAD_WORDS.search(t) and "option writers" not in t:
                    errs.append(f"advice-like wording in {f}: {t[:100]}")
    try:
        pb = json.loads((ROOT / "playbook.json").read_text())
        if not (pb.get("meta") or {}).get("disclaimer"):
            errs.append("playbook.json meta.disclaimer missing")
    except Exception:  # noqa: BLE001 - reported by the playbook step
        pass
    return errs, note


def build(data_dir: Path, out_html: Path) -> tuple[bool, str]:
    r = sh([sys.executable, "build_dashboard.py", "--data-dir", str(data_dir), "--out", str(out_html)])
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-6:])
    ok = r.returncode == 0 and out_html.exists() and "Traceback" not in r.stderr
    return ok, tail


def browser(html: Path, full: bool) -> list[str]:
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
                        if frag == "#options" and pg.locator("#odesk").count() and pg.is_visible("#overview"):
                            errs.append(f"{tag}: dashboard sections visible on the options page")
                        if frag == "" and pg.locator("#options").count() and pg.is_visible("#options"):
                            errs.append(f"{tag}: options desk visible on the main page")
                        if full and frag == "#options" and w > 1000 and theme == "dark" and pg.locator("#od-inst button").count():
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="output dir (default: a fresh temp dir)")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="check the secret patterns and exit")
    a = ap.parse_args()
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

    ok, tail = build(ROOT / "data", out / "real" / "index.html")
    res["build"] = {"errors": [] if ok else ["real-data build failed:\n" + tail]}

    htmls = [out / "real" / "index.html"] if ok else []
    if not a.quick:
        fx_data = out / "fixture-data"
        shutil.copytree(ROOT / "data", fx_data)
        shutil.rmtree(fx_data / "options", ignore_errors=True)
        r = sh([sys.executable, "tools/fixtures.py", str(fx_data)])
        errs = [] if r.returncode == 0 else ["fixtures failed:\n" + (r.stdout + r.stderr)[-800:]]
        ok2, tail2 = build(fx_data, out / "fixture" / "index.html")
        if not ok2:
            errs.append("fixture build failed:\n" + tail2)
        else:
            htmls.append(out / "fixture" / "index.html")
        res["fixtures"] = {"errors": errs}

    berrs = []
    for h in htmls:
        try:
            berrs += [f"[{h.parent.name}] {e}" for e in browser(h, full=not a.quick)]
        except Exception as exc:  # noqa: BLE001
            berrs.append(f"[{h.parent.name}] browser check crashed: {type(exc).__name__}: {exc}")
    res["browser"] = {"errors": berrs}

    passed = all(not v["errors"] for v in res.values())
    (out / "check.json").write_text(json.dumps({"passed": passed, "steps": res}, indent=1))
    md = [f"# check: {'PASS' if passed else 'FAIL'}", ""]
    for k, v in res.items():
        md.append(f"- **{k}**: {'ok' if not v['errors'] else str(len(v['errors'])) + ' problem(s)'}")
        md += [f"  - {e}" for e in v["errors"][:20]]
    md.append(f"\n{rules_note}")
    md.append(f"Built pages: {', '.join(str(h) for h in htmls) or 'none'}")
    (out / "check.md").write_text("\n".join(md))
    print("\n".join(md))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
