"""Screenshots and layout heuristics for the design review.

    python tools/shots.py PAGE.html OUT_DIR [--only main|options] [--sections overview,fx]

For dark desktop, light desktop and dark phone it saves one PNG per section of the main page and one per
card of the options desk into OUT_DIR, and writes OUT_DIR/layout.md listing machine-found suspects:
  - element wider than its card (cut off on the right)
  - text clipped by its own box (overflow hidden and content taller or wider)
  - two labels overlapping inside an SVG chart
  - horizontal page scroll on phone
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
  root.querySelectorAll('.card').forEach(card => {
    if (!vis(card) || (card.tagName === 'DETAILS' && !card.open)) return;
    const cr = card.getBoundingClientRect(), title = (card.querySelector('h3') || {}).textContent || name(card);
    card.querySelectorAll('*').forEach(e => {
      if (!vis(e) || e.closest('.scroll, .odgrid, .mxs, .wl, svg, canvas, select, .newsbox, .tip, .sr-only') || e.classList.contains('sr-only')) return;
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("page")
    ap.add_argument("out")
    ap.add_argument("--only", choices=["main", "options"])
    ap.add_argument("--sections", default="")
    a = ap.parse_args()
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
    report = ["# layout suspects", ""]
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME) if CHROME else p.chromium.launch()
        for theme, w in (("dark", 1440), ("light", 1440), ("dark", 390)):
            for page_name, frag in (("main", ""), ("options", "#options")):
                if a.only and a.only != page_name:
                    continue
                pg = b.new_page(viewport={"width": w, "height": 1000}, color_scheme=theme)
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
                    cards = pg.locator("#odesk > .card")
                    for i in range(cards.count()):
                        if cards.nth(i).is_visible():
                            h = cards.nth(i).locator("h3")
                            title = h.first.inner_text() if h.count() else "controls"
                            title = re.sub(r"^\s*\d+[\s.:)\-]*", "", title)   # the file name already carries the number
                            slug = "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")[:40] or "card"
                            cards.nth(i).screenshot(path=str(out / f"{tag}-options-{i:02d}-{slug}.png"))
                sus = pg.evaluate(SUSPECTS_JS, pg.query_selector("main"))
                report.append(f"## {tag} {page_name}: {len(sus)} suspect(s)")
                report += [f"- {s}" for s in sus[:40]]
                pg.close()
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
