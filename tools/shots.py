"""Screenshots and layout heuristics for the design review.

    python tools/shots.py PAGE.html OUT_DIR [--only main|options] [--sections overview,fx] [--klib PATH_TO_klinecharts.min.js]

For dark desktop, light desktop and dark phone it saves one PNG per section of the main page, one per
sidebar view of the options desk (internal build only; the public build has no desk) and the centre chart panel on both
tabs (Chart, Live) into OUT_DIR, and writes OUT_DIR/layout.md listing machine-found suspects:
  - element wider than its card (cut off on the right)
  - text clipped by its own box (overflow hidden and content taller or wider)
  - two labels overlapping inside an SVG chart
  - horizontal page scroll on phone
For the internal build it also saves the whole options terminal block (#dterm) at 1440x900 and 1600x900, dark and light
(terminal-<theme>-<width>.png). --klib answers the chart library request from a local copy so the price chart draws offline.
The suspects are hints, not verdicts: the design reviewer looks at the PNGs and decides.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

CHROME = next((str(p) for p in Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome")), None)

SUSPECTS_JS = r"""
(root) => {
  const out = [], vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const name = e => (e.id ? '#' + e.id : e.tagName.toLowerCase() + (e.className && typeof e.className === 'string' ? '.' + e.className.split(' ')[0] : ''));
  root.querySelectorAll('.card, .dpanel').forEach(card => {
    if (!vis(card) || (card.tagName === 'DETAILS' && !card.open)) return;
    const cr = card.getBoundingClientRect(), title = (card.querySelector('h3') || {}).textContent || name(card);
    card.querySelectorAll('*').forEach(e => {
      if (!vis(e) || e.closest('.scroll, .odgrid, .dlad, .mxs, .wl, svg, canvas, select, .newsbox, .tip, .sr-only') || e.classList.contains('sr-only')) return;
      for (let a = e.parentElement; a && a !== card; a = a.parentElement) { const o = getComputedStyle(a).overflowX; if (o === 'auto' || o === 'scroll') return; }
      const r = e.getBoundingClientRect();
      if (r.right > cr.right + 2 && e.children.length === 0 && (e.textContent || '').trim()) out.push(`cut off on the right: "${(e.textContent||'').trim().slice(0,40)}" in card "${title.trim()}"`);
      const cs = getComputedStyle(e);
      if ((cs.overflow === 'hidden' || cs.overflowX === 'hidden' || cs.overflowY === 'hidden') && e.children.length < 6 &&
          (e.scrollHeight > e.clientHeight + 3 || e.scrollWidth > e.clientWidth + 3) && (e.textContent || '').trim().length > 2 &&
          !['kl'].some(c => e.classList.contains(c)))
        out.push(`clipped text in ${name(e)} in card "${title.trim()}": "${(e.textContent||'').trim().slice(0,40)}"`);
    });
    card.querySelectorAll('svg').forEach(svg => {
      const ts = [...svg.querySelectorAll('text')].filter(vis).map(t => ({ t: t.textContent, r: t.getBoundingClientRect() }));
      for (let i = 0; i < ts.length; i++) for (let j = i + 1; j < ts.length; j++) {
        const a = ts[i].r, b = ts[j].r, ox = Math.min(a.right, b.right) - Math.max(a.left, b.left), oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (ox > 3 && oy > 3 && ts[i].t.trim() && ts[j].t.trim()) { out.push(`overlapping chart labels "${ts[i].t.slice(0,25)}" / "${ts[j].t.slice(0,25)}" in card "${title.trim()}"`); return; }
      }
    });
  });
  if (document.documentElement.scrollWidth > window.innerWidth + 1) out.push('horizontal page scroll');
  return [...new Set(out)];
}
"""


def chart_shots(pg, out: Path, tag: str) -> None:
    """Desk Terminal: the centre panel on both tabs (Chart, Live). The Live tab needs the internet; offline it shows its fallback note."""
    try:
        pg.click("#dside-l button[data-tab='terminal']")
        for tab in ("kline", "live"):
            btn = pg.locator(f'#pc-tabs button[data-v="{tab}"]')
            if not btn.count():
                return
            btn.click()
            pg.wait_for_timeout(2500 if tab == "kline" else 1500)
            pg.locator('.dpanel[data-panel="pchart"]').screenshot(path=str(out / f"{tag}-options-chart-{tab}.png"))
        pg.locator('#pc-tabs button[data-v="kline"]').click()
    except Exception as exc:  # noqa: BLE001
        print(f"chart screenshots skipped: {type(exc).__name__}: {exc}", file=sys.stderr)


def route_lib(pg, klib: Path | None) -> None:
    if klib and klib.is_file():
        pg.route("**/klinecharts*.js", lambda r: r.fulfill(path=str(klib), content_type="application/javascript"))


def terminal_shots(b, url: str, out: Path, klib: Path | None) -> list[str]:
    """The options terminal block as one picture at the sizes the owner uses; returns notes for the report."""
    notes = []
    for theme in ("dark", "light"):
        for w in (1440, 1600):
            pg = b.new_page(viewport={"width": w, "height": 900}, color_scheme=theme)
            try:
                route_lib(pg, klib)
                pg.goto(url + "#options")
                pg.wait_for_timeout(2200)
                pg.evaluate("(() => { const h = document.querySelector('.stickyhead'); if (h) h.style.position = 'static' })()")
                blk = pg.locator("#odesk")
                if not blk.count() or not blk.first.is_visible():
                    notes.append(f"terminal {theme} {w}: no #dterm block")
                    continue
                blk.first.screenshot(path=str(out / f"terminal-{theme}-{w}.png"))
                bb = blk.first.bounding_box() or {}
                notes.append(f"terminal {theme} {w}: block {round(bb.get('width', 0))}x{round(bb.get('height', 0))} px")
            finally:
                pg.close()
    return notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("page")
    ap.add_argument("out")
    ap.add_argument("--only", choices=["main", "options"])
    ap.add_argument("--sections", default="")
    ap.add_argument("--klib", default="", help="local klinecharts.min.js used to answer the CDN request (offline price chart shots)")
    a = ap.parse_args()
    klib = Path(a.klib).resolve() if a.klib else None
    from playwright.sync_api import sync_playwright
    out = Path(a.out).resolve()
    roots = [Path("/tmp").resolve()] + [Path(os.environ[v]).resolve() for v in ("TEAM_DIR", "CLAUDE_SCRATCHPAD") if os.environ.get(v)]
    team = Path(os.environ["TEAM_DIR"]).resolve() if os.environ.get("TEAM_DIR") else None
    if not any(out != r and r in out.parents for r in roots) or out == team:
        print(f"refusing to write to {out}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    (out / ".macro-check").write_text("written by tools/shots.py\n")
    url = Path(a.page).resolve().as_uri()
    want = set(filter(None, a.sections.split(",")))
    report = ["# layout suspects", "", "Internal build (options desk on) is needed for the desk panels: build with --options-desk on or OPTIONS_DESK_PUBLIC=1. A public build has no #odesk.", ""]
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME) if CHROME else p.chromium.launch()
        for theme, w in (("dark", 1440), ("light", 1440), ("dark", 390)):
            for page_name, frag in (("main", ""), ("options", "#options")):
                if a.only and a.only != page_name:
                    continue
                pg = b.new_page(viewport={"width": w, "height": 1000}, color_scheme=theme)
                route_lib(pg, klib)
                pg.goto(url + frag)
                pg.wait_for_timeout(1200)
                pg.evaluate("(() => { const h = document.querySelector('.stickyhead'); if (h) h.style.position = 'static' })()")
                tag = f"{theme}-{w}"
                if page_name == "main":
                    for sec in pg.eval_on_selector_all("main > section.sec", "s => s.filter(x => x.offsetParent).map(x => x.id)"):
                        if want and sec not in want:
                            continue
                        pg.locator(f"#{sec}").screenshot(path=str(out / f"{tag}-main-{sec}.png"))
                else:
                    ban = pg.locator("#opt-stale")   # sits outside the #odesk cards
                    if ban.count() and ban.first.is_visible():
                        ban.first.screenshot(path=str(out / f"{tag}-options-banner.png"))
                    # one picture per sidebar view (the desk shows one view at a time)
                    for v in pg.eval_on_selector_all("#dside-l button[data-tab]", "bs => bs.map(b => b.dataset.tab)"):
                        pg.click(f"#dside-l button[data-tab='{v}']")
                        pg.wait_for_timeout(500)
                        pg.locator("#odesk").screenshot(path=str(out / f"{tag}-options-view-{v}.png"))
                    pg.click("#dside-l button[data-tab='terminal']")
                    if pg.locator("#pc-tabs").count() and w > 700:
                        chart_shots(pg, out, tag)
                if page_name == "options" and not pg.locator("#odesk").count():
                    report.append(f"## {tag} options: no desk in this page (public build; use --options-desk on for the internal build)")
                    pg.close()
                    continue
                sus = pg.evaluate(SUSPECTS_JS, pg.query_selector("main"))
                report.append(f"## {tag} {page_name}: {len(sus)} suspect(s)")
                report += [f"- {s}" for s in sus[:40]]
                pg.close()
        if a.only != "main":
            report += ["", "## terminal block"] + [f"- {n}" for n in terminal_shots(b, url, out, klib)]
        b.close()
    (out / "layout.md").write_text("\n".join(report))
    print("\n".join(report))
    n_png = len(list(out.glob("*.png")))
    print(f"\nscreenshots: {n_png} in {out}")
    if n_png == 0:
        print("no screenshots written", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
