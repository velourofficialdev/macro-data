"""The standard quality gate. Every builder runs it before handing work over, and every checker starts from it.

    python tools/check.py [--out DIR] [--base origin/main] [--quick]

It runs, in order, and writes DIR/check.json plus DIR/check.md (default DIR: /tmp/macro-check):
  1. rules     - the diff against BASE: no workflow, docs/ or data/ changes committed, no secrets or keys in
                 the code, no "buy"/"sell"/price-target wording added, playbook disclaimer kept
  2. playbook  - playbook.json loads
  3. build     - build_dashboard.py with the real data/ (into DIR, never into docs/)
  4. fixtures  - tools/fixtures.py into a copy of data/, then a second build, so the options desk is
                 exercised even when the sandbox cannot reach Cboe / Yahoo / FRED
  5. browser   - headless Chromium on both builds: main page and #options, dark + light + phone,
                 console errors, empty page, horizontal scroll on phone, page switching, every
                 instrument x mode x Greek on the desk
--quick skips step 4 and the click-through (for a fast loop while building).
Exit code 0 only when everything passed. Never report a check as passed unless this printed PASS.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHROME = next((str(p) for p in Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome")), None)
BAD_WORDS = re.compile(r"\b(buy|sell|price target|go long|go short)\b", re.I)
SECRET = re.compile(r"(api[_-]?key|token|secret|password)\s*[=:]\s*['\"][A-Za-z0-9_\-]{12,}['\"]", re.I)


def sh(cmd: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def check_rules(base: str) -> list[str]:
    errs = []
    names = sh(["git", "diff", "--name-only", f"{base}...HEAD"]).stdout.split()
    for n in names:
        if n.startswith(".github/workflows/"):
            errs.append(f"workflow changed: {n} (owner-only)")
        if n.startswith("docs/") or (n.startswith("data/") and not n.startswith("data/README")):
            errs.append(f"build output or data committed: {n}")
    diff = sh(["git", "diff", "-U0", f"{base}...HEAD", "--", ".", ":(exclude)tools/check.py"]).stdout
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if SECRET.search(line):
            errs.append("possible secret in diff: " + line[:120])
    for f in ("playbook.json", "template.html", "build_dashboard.py"):
        added = [l[1:] for l in sh(["git", "diff", "-U0", f"{base}...HEAD", "--", f]).stdout.splitlines() if l.startswith("+") and not l.startswith("+++")]
        for l in added:   # only text a reader sees: strings and markup, not code identifiers
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
    return errs


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
        for theme, w in (("dark", 1440), ("light", 1440), ("dark", 390)):
            for frag in ("", "#options"):
                pg = b.new_page(viewport={"width": w, "height": 900}, color_scheme=theme)
                log = []
                pg.on("pageerror", lambda e, log=log: log.append(str(e)))
                pg.on("console", lambda m, log=log: log.append(m.text) if m.type == "error" and "fonts.g" not in m.text else None)
                pg.goto(url + frag)
                pg.wait_for_timeout(900)
                tag = f"{theme} {w}px {frag or 'main'}"
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
                pg.close()
        b.close()
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/macro-check")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    res: dict[str, dict] = {}

    res["rules"] = {"errors": check_rules(a.base)}
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
    md.append(f"\nBuilt pages: {', '.join(str(h) for h in htmls) or 'none'}")
    (out / "check.md").write_text("\n".join(md))
    print("\n".join(md))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
