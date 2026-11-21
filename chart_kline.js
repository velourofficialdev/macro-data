/* Velour chart: KLineChart 10.1.1 inside #kl-root. Plain ES2019, no build step.
   Loads the library only on the "kl:open" event. Colours come from the page tokens (--primary up, --secondary down). */
(function () {
  "use strict";
  var LIB_URL = "https://cdn.jsdelivr.net/npm/klinecharts@10.1.1/dist/umd/klinecharts.min.js";
  var LIB_SRI = "sha384-xkC4/84Bj5cSxBeqZuaHxfU6OGkQpaAtTHc5gwE/kJd4vt7vRtFvslHtVTEq11Zl";
  var KEY = "macro-kline", VER = 1, DAY = 86400000;
  var root = document.getElementById("kl-root");

  /* ---------- public object (exists before the card is opened) ---------- */
  var resolveReady, rejectReady;
  var ready = new Promise(function (res, rej) { resolveReady = res; rejectReady = rej; });
  ready.catch(function () {});   // a failed load is shown in the card; no "uncaught" noise
  var chart = null, curSym = null;
  var VC = window.VelourChart = {
    ready: ready,
    chart: function () { return chart; },
    symbol: function () { return curSym; },
    save: function () { saveNow(); },
    state: function () { snapshotCur(); return state; },
    exportJSON: function () { snapshotCur(); return JSON.stringify(exportObj(), null, 1); },
    importJSON: function (text) { return doImport(text); },
    cloudSave: function (/* state */) { /* Phase 2 hook: intentionally does nothing */ },
    levelsFor: function (symbol) { return levelsFor(symbol); },
    deskSymbol: function (optKey) { return deskSymbol(optKey); },
    mountDesk: function (host, symbol, levels, opts) { return mountDesk(host, symbol, levels, opts); }
  };
  if (!root) { rejectReady(new Error("no #kl-root")); return; }

  /* ---------- small helpers ---------- */
  function $(sel, el) { return (el || root).querySelector(sel); }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function btn(label, title, cls) { var b = el("button", cls === undefined ? "kl-btn" : cls, label); b.type = "button"; if (title) { b.title = title; b.setAttribute("aria-label", title); } return b; }
  function clamp(x, a, b) { return Math.min(b, Math.max(a, x)); }
  function isObj(o) { return o && typeof o === "object" && !Array.isArray(o); }
  function fin(x) { return typeof x === "number" && isFinite(x); }
  function iso(ts) { return new Date(ts).toISOString().slice(0, 10); }
  function fmtNum(x, dec) { return x.toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec }); }
  function sgn(x, dec, suffix) { return (x > 0 ? "+" : x < 0 ? "\u2212" : "") + Math.abs(x).toFixed(dec) + (suffix || ""); }
  function arrow(x) { return x > 0 ? "\u25B2 " : x < 0 ? "\u25BC " : ""; }

  var LABELS = { GOLD: "Gold", SPX: "S&P 500", NDX: "Nasdaq 100", DOW: "Dow", DXY: "Dollar index", EURUSD: "EUR/USD", US10Y: "US 10Y" };
  var RANGES = ["1M", "3M", "6M", "YTD", "1Y", "5Y", "All"];
  var TYPES = { area: "Area", candle_solid: "Candles", ohlc: "OHLC bars" };
  var SCALES = { normal: "normal", log: "logarithm", percent: "percentage" };
  var COLKEYS = ["primary", "secondary", "warn", "text", "muted"];
  var DASHES = ["solid", "dashed", "dotted"];

  /* overlays: id -> {label, kind, hint}. Allowlist for import. */
  var TOOLS = [
    { n: "segment", t: "Trend line", k: "Alt+T", code: "KeyT", ic: "M4 19L20 5M4 19h0M20 5h0" },
    { n: "rayLine", t: "Ray", ic: "M4 18L20 6M16 5l4 1-1 4" },
    { n: "straightLine", t: "Extended line", ic: "M2 21L22 3" },
    { n: "horizontalStraightLine", t: "Horizontal line", k: "Alt+H", code: "KeyH", ic: "M3 12h18" },
    { n: "verticalStraightLine", t: "Vertical line", k: "Alt+V", code: "KeyV", ic: "M12 3v18" },
    { n: "parallelStraightLine", t: "Parallel channel", ic: "M3 15L21 8M3 21L21 14" },
    { n: "fibonacciLine", t: "Fibonacci retracement", k: "Alt+F", code: "KeyF", ic: "M3 5h18M3 10h18M3 15h18M3 20h18" },
    { n: "priceLine", t: "Price line", ic: "M3 12h13M16 8h5v8h-5z" },
    { n: "vlRect", t: "Rectangle", k: "Alt+R", code: "KeyR", ic: "M5 6h14v12H5z" },
    { n: "vlArrow", t: "Arrow", ic: "M5 19L19 5M10 5h9v9" },
    { n: "vlMeasure", t: "Measure (change in price, percent, bars)", k: "Alt+M", code: "KeyM", ic: "M3 16L16 3l5 5L8 21zM8 11l2 2M11 8l2 2" },
    { n: "vlRange", t: "Range box (neutral zone between two prices)", ic: "M4 6h16v12H4zM4 12h16" },
    { n: "brush", t: "Freehand brush", ic: "M4 20c5-1 4-6 8-7s5-5 8-8" },
    { n: "simpleAnnotation", t: "Text note", ic: "M5 5h14v10h-8l-4 4v-4H5z" },
    { n: "simpleTag", t: "Marker (price tag)", ic: "M3 12h13M16 8h5v8h-5z" }
  ];
  var TOOLNAME = {}; TOOLS.forEach(function (t) { TOOLNAME[t.n] = t; });
  var NOTEY = { simpleAnnotation: 1, simpleTag: 1, vlRect: 1, vlMeasure: 1, vlRange: 1 };   // names whose label can be edited
  var FILLY = { vlRect: 1, vlRange: 1, vlMeasure: 1, parallelStraightLine: 1, fibonacciLine: 0 };

  /* indicators: name (KLineChart or registered), label, default params, pane */
  var INDS = {
    MA: { label: "SMA", params: [20], pane: "candle", tip: "Simple moving average of the close", pl: ["Period"] },
    EMA: { label: "EMA", params: [20], pane: "candle", tip: "Exponential moving average of the close", pl: ["Period"] },
    BOLL: { label: "BOLL", params: [20, 2], pane: "candle", tip: "Bollinger bands: moving average and a band of N standard deviations", pl: ["Period", "Width"] },
    RSI: { label: "RSI", params: [14], pane: "own", tip: "Relative strength index, 0 to 100", pl: ["Period"] },
    MACD: { label: "MACD", params: [12, 26, 9], pane: "own", tip: "Moving average convergence/divergence", pl: ["Fast", "Slow", "Signal"] },
    VL_ATR: { label: "ATR (close-to-close)", params: [14], pane: "own", tip: "Average true range from daily closes only", pl: ["Period"] },
    VL_CMP: { label: "Compare", params: [], pane: "candle", tip: "Second symbol rebased to the same start", pl: [] }
  };

  /* ---------- state + persistence ---------- */
  var mem = null;   // in-memory fallback when localStorage is blocked
  var state = freshState();
  function freshState() { return { v: VER, last: { symbol: null, tab: "kline" }, symbols: {}, theme: "auto" }; }
  function freshSym() { return { interval: "D", range: "1Y", type: "area", scale: "normal", overlays: [], indicators: [] }; }
  function loadState() {
    var raw = null;
    try { raw = localStorage.getItem(KEY); } catch (e) { raw = mem; }
    if (!raw) return;
    wrote = true;   // a stored layout existed when the page opened
    var v = validate(raw, true);   // same allowlist and caps as an import; anything wrong starts a fresh layout
    if (v.ok) { state = v.ok; } else { state = freshState(); }
  }
  function dropKid(k, v) { return k === "kid" ? undefined : v; }
  var saveT = 0, wrote = false;
  function saveSoon() { clearTimeout(saveT); saveT = setTimeout(saveNow, 400); }
  function saveNow() {
    clearTimeout(saveT); saveT = 0;
    try { snapshotCur(); } catch (e) { /* keep the previous snapshot */ }
    var s = JSON.stringify(state, dropKid), chk = validate(s, true);
    mem = s;
    if (!chk.ok) { say("Layout not saved on this device (" + chk.error + "). Use Export to keep a copy.", true); }
    else {
      try { localStorage.setItem(KEY, s); wrote = true; }
      catch (e) { say("Could not save the layout on this device (storage blocked or full). Use Export to keep a copy.", true); }
    }
    try { VC.cloudSave(state); } catch (e) { /* hook */ }
  }
  /* the last-chance saves (leaving the page, tab hidden) do not resurrect a layout somebody cleared from storage on purpose */
  function lastChance() {
    var gone = false;
    try { gone = wrote && localStorage.getItem(KEY) === null; } catch (e) { gone = false; }
    if (chart && !gone) saveNow();
  }
  window.addEventListener("pagehide", lastChance);
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") lastChance(); });

  /* ---------- feed ---------- */
  var feed = [], byId = Object.create(null);
  function parseFeed(force) {
    if (feed.length && !force) return;   // already parsed (the options desk chart may have done it before the card opened)
    var node = document.getElementById("chart-feed"), j = null;
    try { j = node ? JSON.parse(node.textContent || "{}") : null; } catch (e) { j = null; }
    feed = []; byId = Object.create(null);
    if (!j || !Array.isArray(j.symbols)) return;
    j.symbols.forEach(function (s) {
      if (!s || typeof s.id !== "string" || !Array.isArray(s.c) || !Array.isArray(s.dd) || s.c.length < 2 || s.c.length !== s.dd.length) return;
      var t0 = Date.parse(String(s.t0) + "T00:00:00Z"); if (!fin(t0)) return;
      var ts = new Array(s.c.length), t = t0, i, ok = true;
      for (i = 0; i < s.c.length; i++) { t += (+s.dd[i] || 0) * DAY; ts[i] = t; if (!fin(s.c[i])) ok = false; }
      if (!ok) return;
      var o = { id: s.id, name: s.name || s.id, src: s.src || "", delay: s.delay || "Daily close", partial: !!s.last_partial, unit: s.unit || "", dec: fin(s.dec) ? clamp(s.dec, 0, 6) : 2,
                optKey: typeof s.opt_key === "string" ? s.opt_key : "", optUnits: typeof s.opt_units === "string" ? s.opt_units : "", ohlc: !!s.ohlc, ts: ts, c: s.c, o: s.o, h: s.h, l: s.l, cache: {} };
      feed.push(o); byId[o.id] = o;
    });
  }
  function weekKey(ts) { var d = new Date(ts), dow = (d.getUTCDay() + 6) % 7; return ts - dow * DAY; }
  function barsFor(sym, iv) {
    if (sym.cache[iv]) return sym.cache[iv];
    var out = [], i, n = sym.ts.length;
    var hasO = sym.ohlc && sym.o && sym.h && sym.l && sym.o.length === n;
    if (iv === "W") {
      var cur = null, prevC = null;
      for (i = 0; i < n; i++) {
        var k = weekKey(sym.ts[i]), c = sym.c[i];
        if (!cur || cur.k !== k) {
          if (cur) { prevC = cur.close; out.push(cur.b); }
          cur = { k: k, close: c, b: { timestamp: sym.ts[i], open: prevC == null ? c : prevC, high: c, low: c, close: c } };
          cur.b.high = Math.max(cur.b.open, c); cur.b.low = Math.min(cur.b.open, c);
        } else {
          cur.close = c; cur.b.timestamp = sym.ts[i]; cur.b.close = c;
          cur.b.high = Math.max(cur.b.high, c); cur.b.low = Math.min(cur.b.low, c);
        }
      }
      if (cur) out.push(cur.b);
    } else {
      for (i = 0; i < n; i++) {
        var cl = sym.c[i], op = hasO ? sym.o[i] : (i ? sym.c[i - 1] : cl);
        out.push({ timestamp: sym.ts[i], open: op, high: hasO ? sym.h[i] : Math.max(op, cl), low: hasO ? sym.l[i] : Math.min(op, cl), close: cl });
      }
    }
    sym.cache[iv] = out; return out;
  }

  /* ---------- colour tokens ---------- */
  var T = {};
  function readTokens() {
    var cs = getComputedStyle(document.documentElement);
    function g(n, d) { var v = cs.getPropertyValue(n).trim(); return v || d; }
    T = { primary: g("--primary", "#90bff9"), secondary: g("--secondary", "#b96ac9"), warn: g("--warn", "#d9b26a"), text: g("--text", "#e6eaf0"),
          muted: g("--muted", "#8e98a9"), grid: g("--grid", "rgba(143,156,178,.12)"), surface: g("--surface", "#1a1e25"), surface2: g("--surface-2", "#20252d"),
          border: g("--border", "#252b34"), strong: g("--border-strong", "#2d343f"), onp: g("--on-primary", "#12151a"), bg: g("--background", "#12151a"),
          font: getComputedStyle(root).fontFamily || "sans-serif" };
  }
  function rgba(c, a) {
    var m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec((c || "").trim());
    if (m) { var h = m[1]; if (h.length === 3) h = h.replace(/./g, "$&$&"); return "rgba(" + parseInt(h.slice(0, 2), 16) + "," + parseInt(h.slice(2, 4), 16) + "," + parseInt(h.slice(4), 16) + "," + a + ")"; }
    m = /^rgba?\(([^)]+)\)/.exec(c || "");
    if (m) { var p = m[1].split(",").map(function (x) { return parseFloat(x); }); return "rgba(" + p[0] + "," + p[1] + "," + p[2] + "," + a + ")"; }
    return c;
  }
  function pageTheme() {
    var t = document.documentElement.getAttribute("data-theme");
    return t === "light" || t === "dark" ? t : "auto";
  }
  function themeStyles() {
    var p = T.primary, s = T.secondary, m = T.muted, f = T.font, line = rgba(T.text, 0.22);
    var pal = [p, T.warn, s, T.text, m, T.strong];
    return {
      grid: { horizontal: { color: T.grid }, vertical: { color: T.grid } },
      candle: {
        bar: { upColor: p, downColor: s, noChangeColor: m, upBorderColor: p, downBorderColor: s, noChangeBorderColor: m, upWickColor: p, downWickColor: s, noChangeWickColor: m },
        area: { lineColor: p, backgroundColor: [{ offset: 0, color: rgba(p, 0.02) }, { offset: 1, color: rgba(p, 0.22) }], point: { show: false, color: p, rippleColor: rgba(p, 0.3) } },
        priceMark: { high: { color: m, textFamily: f }, low: { color: m, textFamily: f },
                     last: { upColor: p, downColor: s, noChangeColor: m, text: { color: T.onp, family: f } } },
        tooltip: { showRule: "none" }
      },
      indicator: {
        ohlc: { upColor: p, downColor: s, noChangeColor: m },
        bars: [{ upColor: rgba(p, 0.7), downColor: rgba(s, 0.7), noChangeColor: m }],
        lines: pal.map(function (c) { return { color: c }; }),
        tooltip: { title: { color: m, family: f }, legend: { color: m, family: f } }
      },
      xAxis: { axisLine: { color: line }, tickLine: { color: line }, tickText: { color: m, family: f } },
      yAxis: { axisLine: { color: line }, tickLine: { color: line }, tickText: { color: m, family: f } },
      separator: { color: line, activeBackgroundColor: rgba(p, 0.1) },
      crosshair: {
        horizontal: { line: { color: m }, text: { color: T.onp, backgroundColor: m, borderColor: m, family: f } },
        vertical: { line: { color: m }, text: { color: T.onp, backgroundColor: m, borderColor: m, family: f } }
      },
      overlay: ovStyles({ ck: "primary", w: 1, dash: "solid", fill: true })
    };
  }
  function ovStyles(m) {
    var col = T[m.ck] || T.primary, a = rgba(col, 0.16), w = m.w || 1, ls = m.dash === "solid" || !m.dash ? "solid" : "dashed", dv = m.dash === "dotted" ? [2, 4] : [6, 4];
    return {
      point: { color: col, borderColor: rgba(col, 0.35), activeColor: col, activeBorderColor: rgba(col, 0.35) },
      line: { color: col, size: w, style: ls, dashedValue: dv },
      rect: { style: m.fill ? "stroke_fill" : "stroke", color: a, borderColor: col, borderSize: w, borderStyle: ls, borderDashedValue: dv },
      polygon: { style: m.fill ? "stroke_fill" : "stroke", color: a, borderColor: col, borderSize: w, borderStyle: ls, borderDashedValue: dv },
      circle: { style: m.fill ? "stroke_fill" : "stroke", color: a, borderColor: col, borderSize: w, borderStyle: ls, borderDashedValue: dv },
      arc: { color: col, size: w, style: ls, dashedValue: dv },
      text: { color: T.onp, backgroundColor: col, borderColor: col, family: T.font, size: 12 }
    };
  }

  /* ---------- load the library on first open ---------- */
  var phase = 0;   // 0 idle, 1 loading, 2 ready, 3 failed
  var libPromise = null;
  function loadLib() {
    if (window.klinecharts) return Promise.resolve();
    if (libPromise) return libPromise;
    libPromise = new Promise(function (res, rej) {
      var sc = document.createElement("script");
      sc.src = LIB_URL; sc.integrity = LIB_SRI; sc.crossOrigin = "anonymous"; sc.async = true;
      sc.onload = function () { window.klinecharts ? res() : rej(new Error("klinecharts missing")); };
      sc.onerror = function () { sc.remove(); libPromise = null; rej(new Error("load failed")); };
      document.head.appendChild(sc);
    });
    return libPromise;
  }
  function onOpen() {
    if (phase === 1 || phase === 2) { if (phase === 2 && chart) resizeSoon(); return; }
    phase = 1;
    parseFeed(); loadState();
    if (!feed.length) { phase = 3; root.textContent = ""; var nd = el("div", "kl-nodata"); nd.appendChild(el("strong", "", "Chart data not available")); nd.appendChild(el("p", "note", "The dashboard has no saved daily series to draw. The Live tab still works. Charting tool, not advice.")); root.appendChild(nd); rejectReady(new Error("no data")); return; }
    buildShell();
    showState("Loading chart library\u2026");
    loadLib().then(function () { return init(); }).then(function () { phase = 2; }, function (err) {
      phase = 3;
      if (!stage) buildShell();
      showFail();
      rejectReady(err);
    });
  }
  function onHide() { exitFull(); closePops(); }
  window.addEventListener("kl:open", onOpen);
  window.addEventListener("kl:hide", onHide);

  /* ---------- UI shell ---------- */
  var stage, chartEl, readEl, symBar, ivBar, rgBar, typeSel, scaleBar, chipsEl, styleEl, msgEl, srcEl, toolsEl, indBtn, undoB, redoB, fullB, indPop, helpPop, stateEl, hideB, lockB, magB;
  var toolBtns = {}, ivBtns = {}, rgBtns = {}, scBtns = {};
  var builtShell = false;
  function ic(path) { return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="' + path + '"/></svg>'; }
  function buildShell() {
    if (builtShell) return; builtShell = true;
    root.textContent = "";
    var top = el("div", "kl-row");
    symBar = el("div", "ranges"); symBar.setAttribute("role", "group"); symBar.setAttribute("aria-label", "Chart symbol");
    top.appendChild(symBar); top.appendChild(el("span", "kl-sp"));
    fullB = btn("Expand", "Expand the chart to full screen (Esc closes)"); fullB.setAttribute("aria-expanded", "false"); top.appendChild(fullB);
    root.appendChild(top);

    var r2 = el("div", "kl-row");
    ivBar = el("div", "ranges"); ivBar.setAttribute("role", "group"); ivBar.setAttribute("aria-label", "Interval");
    ["1m", "5m", "15m", "1h", "4h"].forEach(function (x) { var b = btn(x, "Needs intraday data the dashboard does not store", ""); b.disabled = true; b.setAttribute("aria-pressed", "false"); ivBar.appendChild(b); });
    [["D", "Daily"], ["W", "Weekly (last close of each week)"]].forEach(function (x) { var b = btn(x[0], x[1], ""); b.dataset.i = x[0]; b.setAttribute("aria-pressed", "false"); ivBar.appendChild(b); ivBtns[x[0]] = b; });
    r2.appendChild(ivBar);
    rgBar = el("div", "ranges"); rgBar.setAttribute("role", "group"); rgBar.setAttribute("aria-label", "Range");
    RANGES.forEach(function (x) { var b = btn(x, "Show " + x, ""); b.dataset.r = x; b.setAttribute("aria-pressed", "false"); rgBar.appendChild(b); rgBtns[x] = b; });
    r2.appendChild(rgBar);
    typeSel = el("select", "kl-sel"); typeSel.setAttribute("aria-label", "Chart type");
    Object.keys(TYPES).forEach(function (k) { var o = el("option", "", TYPES[k]); o.value = k; typeSel.appendChild(o); });
    r2.appendChild(typeSel);
    scaleBar = el("div", "ranges"); scaleBar.setAttribute("role", "group"); scaleBar.setAttribute("aria-label", "Price scale");
    [["log", "Log", "Logarithmic price scale"], ["percent", "%", "Percent change from the first visible bar"]].forEach(function (x) { var b = btn(x[1], x[2], ""); b.dataset.s = x[0]; b.setAttribute("aria-pressed", "false"); scaleBar.appendChild(b); scBtns[x[0]] = b; });
    r2.appendChild(scaleBar);
    root.appendChild(r2);

    var r3 = el("div", "kl-row");
    indBtn = btn("Indicators", "Add or edit indicators"); indBtn.setAttribute("aria-haspopup", "true"); indBtn.setAttribute("aria-expanded", "false"); r3.appendChild(indBtn);
    undoB = btn("Undo", "Undo the last drawing change (Ctrl+Z)"); redoB = btn("Redo", "Redo (Ctrl+Y)"); r3.appendChild(undoB); r3.appendChild(redoB);
    var exB = btn("Export", "Download the saved drawings and indicators as a JSON file"), imB = btn("Import", "Load a layout JSON file exported from this chart");
    r3.appendChild(exB); r3.appendChild(imB);
    var fileIn = el("input"); fileIn.type = "file"; fileIn.accept = "application/json,.json"; fileIn.hidden = true; fileIn.setAttribute("aria-label", "Layout file"); r3.appendChild(fileIn);
    var helpB = btn("Shortcuts", "Keyboard shortcuts"); helpB.setAttribute("aria-haspopup", "true"); helpB.setAttribute("aria-expanded", "false"); r3.appendChild(helpB);
    root.appendChild(r3);

    chipsEl = el("div", "kl-chips"); root.appendChild(chipsEl);

    var main = el("div", "kl-main");
    toolsEl = el("div", "kl-tools"); toolsEl.setAttribute("role", "toolbar"); toolsEl.setAttribute("aria-label", "Drawing tools");
    var cur = btn("", "Cursor (stop drawing)", "kl-tool"); cur.innerHTML = ic("M5 3l6 16 2.5-6.5L20 10z"); cur.setAttribute("aria-pressed", "true"); toolBtns[""] = cur; toolsEl.appendChild(cur);
    TOOLS.forEach(function (t) {
      var b = btn("", t.t + (t.k ? " (" + t.k + ")" : ""), "kl-tool"); b.innerHTML = ic(t.ic); b.setAttribute("aria-pressed", "false"); b.dataset.n = t.n; toolBtns[t.n] = b; toolsEl.appendChild(b);
      b.addEventListener("click", function () { pickTool(activeTool === t.n ? "" : t.n); });
    });
    cur.addEventListener("click", function () { pickTool(""); });
    magB = btn("Magnet: off", "Magnet: off (click to cycle weak, strong)"); magB.setAttribute("aria-pressed", "false");
    lockB = btn("Lock", "Lock all drawings"); lockB.setAttribute("aria-pressed", "false");
    hideB = btn("Hide", "Hide all drawings"); hideB.setAttribute("aria-pressed", "false");
    var delSel = btn("Delete selected", "Delete the selected drawing (Del)");
    var delAll = btn("Delete all", "Delete all drawings on this symbol");
    [magB, lockB, hideB, delSel, delAll].forEach(function (b) { r3.appendChild(b); });
    readEl = el("div", "kl-read"); readEl.setAttribute("aria-live", "off");
    root.appendChild(readEl);
    main.appendChild(toolsEl);

    stage = el("div", "kl-stage");
    chartEl = el("div", "kl-chart"); chartEl.setAttribute("role", "img"); chartEl.setAttribute("aria-label", "Price chart drawn from the dashboard's daily closes");
    stage.appendChild(chartEl);
    stage.appendChild(el("div", "kl-wm", "VELOUR"));
    styleEl = el("div", "kl-style"); styleEl.hidden = true; styleEl.setAttribute("role", "group"); styleEl.setAttribute("aria-label", "Selected drawing style"); stage.appendChild(styleEl);
    stateEl = el("div", "kl-state"); stateEl.hidden = true; stage.appendChild(stateEl);
    main.appendChild(stage);
    root.appendChild(main);

    srcEl = el("p", "kl-src"); root.appendChild(srcEl);
    msgEl = el("p", "kl-msg"); msgEl.setAttribute("role", "status"); root.appendChild(msgEl);
    var notes = el("div", "kl-notes");
    [
      "Opening this tab loads the KLineChart library from jsDelivr, which sees your IP address. Nothing else from the page is sent.",
      "Data: daily closes stored by the dashboard; not live. Candles and bars are drawn close-to-close, so there is no intraday high or low. Intraday intervals need data the dashboard does not store.",
      "Drawings and indicators are saved on this device; use Export to keep a copy.",
      "Charting tool, not advice."
    ].forEach(function (t, i) { var np = el("p", "note", t); if (i === 1) dataNote = np; notes.appendChild(np); });
    root.appendChild(notes);

    indPop = el("div", "kl-pop"); indPop.hidden = true; root.appendChild(indPop);
    helpPop = el("div", "kl-pop"); helpPop.hidden = true; root.appendChild(helpPop);

    /* symbol buttons */
    feed.forEach(function (s) { var b = btn(LABELS[s.id] || s.name, s.name, ""); b.dataset.s = s.id; b.setAttribute("aria-pressed", "false"); symBar.appendChild(b); b.addEventListener("click", function () { showSymbol(s.id); }); });

    /* wiring */
    ivBar.addEventListener("click", function (e) { var b = e.target.closest("button[data-i]"); if (b) setInterval_(b.dataset.i); });
    rgBar.addEventListener("click", function (e) { var b = e.target.closest("button[data-r]"); if (b) setRange(b.dataset.r, true); });
    scaleBar.addEventListener("click", function (e) { var b = e.target.closest("button[data-s]"); if (b) setScale(sc().scale === b.dataset.s ? "normal" : b.dataset.s); });
    typeSel.addEventListener("change", function () { setType(typeSel.value); });
    fullB.addEventListener("click", function () { root.classList.contains("kl-full") ? exitFull() : enterFull(); });
    undoB.addEventListener("click", undo); redoB.addEventListener("click", redo);
    exB.addEventListener("click", doExport);
    imB.addEventListener("click", function () { fileIn.value = ""; fileIn.click(); });
    fileIn.addEventListener("change", function () {
      var f = fileIn.files && fileIn.files[0]; if (!f) return;
      if (f.size > 2e6) { say("Import rejected: file is larger than 2 MB.", true); return; }
      var rd = new FileReader();
      rd.onload = function () { var r = doImport(String(rd.result || "")); say(r.ok ? "Layout imported." : "Import rejected: " + r.error, !r.ok); };
      rd.onerror = function () { say("Import rejected: the file could not be read.", true); };
      rd.readAsText(f);
    });
    magB.addEventListener("click", cycleMagnet);
    lockB.addEventListener("click", function () { locked = !locked; applyLock(); saveSoon(); });
    hideB.addEventListener("click", function () { hidden_ = !hidden_; applyHide(); });
    delSel.addEventListener("click", deleteSelected);
    delAll.addEventListener("click", function () { if (!chart) return; clearOverlays(); histPush(); saveSoon(); });
    indBtn.addEventListener("click", function () { togglePop(indPop, indBtn, buildIndPop); });
    helpB.addEventListener("click", function () { togglePop(helpPop, helpB, buildHelp); });
    document.addEventListener("click", function (e) {
      if (!indPop.hidden && !indPop.contains(e.target) && e.target !== indBtn) closePop(indPop, indBtn);
      if (!helpPop.hidden && !helpPop.contains(e.target) && e.target !== helpB) closePop(helpPop, helpB);
    });
    window.addEventListener("hashchange", exitFull);
    document.addEventListener("keydown", onKey);
    if (window.ResizeObserver) new ResizeObserver(resizeSoon).observe(stage);
    window.addEventListener("resize", resizeSoon);
    root._helpB = helpB;
  }
  var dataNote = null, dataNoteBase = "";
  function hasDesk() { try { return !!(window.P && window.P.opt); } catch (e) { return false; } }
  function say(t, err) { if (!msgEl) return; msgEl.textContent = t; msgEl.className = "kl-msg" + (err ? " err" : ""); }
  function showState(title, sub, quiet) {
    if (!stateEl) return;
    stateEl.hidden = false; stateEl.textContent = "";
    stateEl.appendChild(el("strong", "", title)); if (sub) stateEl.appendChild(el("span", "", sub));
    if (quiet && stage) { stage.style.height = "160px"; }
  }
  function showFail() {
    showState("The chart library could not load (blocked or offline). The Live tab still works.");
    var b = btn("Open the Live tab", "Switch to the TradingView Live tab"); stateEl.appendChild(b);
    b.addEventListener("click", function () { var lb = document.querySelector('#chart-tabs [data-v="live"]'); if (lb) lb.click(); });
  }
  var rsT = 0;
  function resizeSoon() { clearTimeout(rsT); rsT = setTimeout(function () { if (chart) { try { chart.resize(); } catch (e) {} } }, 60); }

  function togglePop(pop, anchor, fill) {
    if (!pop.hidden) { closePop(pop, anchor); return; }
    closePops(); fill(pop); pop.hidden = false; anchor.setAttribute("aria-expanded", "true");
    var rr = root.getBoundingClientRect(), ar = anchor.getBoundingClientRect();
    pop.style.top = Math.round(ar.bottom - rr.top + 4) + "px";
    pop.style.left = Math.round(clamp(ar.left - rr.left, 0, Math.max(0, rr.width - pop.offsetWidth))) + "px";
  }
  function closePop(pop, anchor) { pop.hidden = true; if (anchor) anchor.setAttribute("aria-expanded", "false"); }
  function closePops() { if (indPop) closePop(indPop, indBtn); if (helpPop) closePop(helpPop, root._helpB); }

  function buildHelp(pop) {
    pop.textContent = "";
    pop.appendChild(el("h4", "", "Keyboard shortcuts"));
    var tb = el("table");
    [["Alt+T", "Trend line"], ["Alt+H", "Horizontal line"], ["Alt+V", "Vertical line"], ["Alt+F", "Fibonacci retracement"], ["Alt+R", "Rectangle"], ["Alt+M", "Measure"],
     ["Del", "Delete the selected drawing"], ["Ctrl+Z", "Undo"], ["Ctrl+Y", "Redo"], ["Esc", "Cancel drawing, close menus, leave full screen"]].forEach(function (r) {
      var tr = el("tr"), a = el("td"), k = el("kbd", "", r[0]); a.appendChild(k); tr.appendChild(a); tr.appendChild(el("td", "", r[1])); tb.appendChild(tr);
    });
    pop.appendChild(tb);
    pop.appendChild(el("p", "kl-why", "On a touch screen: tap a tool, then tap the chart. Drag a handle to move a point. Pinch to zoom."));
  }

  /* ---------- full screen (same node, CSS lift) ---------- */
  function enterFull() {
    var head = document.querySelector(".stickyhead");
    root.style.setProperty("--kltop", (head ? Math.max(0, Math.round(head.getBoundingClientRect().bottom)) : 0) + "px");
    root.classList.add("kl-full"); document.body.classList.add("tvlock");
    root.setAttribute("role", "dialog"); root.setAttribute("aria-modal", "true"); root.setAttribute("aria-label", "Chart");
    fullB.textContent = "Close"; fullB.setAttribute("aria-expanded", "true"); fullB.focus({ preventScroll: true }); resizeSoon();
  }
  function exitFull() {
    if (!root.classList.contains("kl-full")) return;
    root.classList.remove("kl-full"); document.body.classList.remove("tvlock");
    root.removeAttribute("role"); root.removeAttribute("aria-modal"); root.removeAttribute("aria-label");
    if (fullB) { fullB.textContent = "Expand"; fullB.setAttribute("aria-expanded", "false"); fullB.focus({ preventScroll: true }); }
    resizeSoon();
  }

  /* ---------- chart init ---------- */
  var activeTool = "", pendingId = null, selId = null, metas = {}, locked = false, hidden_ = false, magnet = 0, restoring = false, needRestore = false, firstReady = false;
  var hist = [], hidx = -1;
  function sc() { return state.symbols[curSym] || (state.symbols[curSym] = freshSym()); }

  function init() {
    var k = window.klinecharts;
    readTokens(); registerCustom(k);
    chart = k.init(chartEl, { timezone: "UTC", styles: themeStyles(), layout: { barSpaceLimit: { min: 0.05, max: 60 } } });
    if (!chart) throw new Error("init failed");
    try { chart.setOffsetRightDistance(24); } catch (e) {}
    chart.setDataLoader({
      getBars: function (p) {
        var s = byId[p.symbol.ticker]; if (!s) { p.callback([], false); return; }
        p.callback(barsFor(s, p.period.type === "week" ? "W" : "D"), false);
        setTimeout(afterLoad, 0);
      }
    });
    chart.subscribeAction("onCrosshairChange", onCross);
    var want = state.last && byId[state.last.symbol] ? state.last.symbol : feed[0].id;
    if (!state.last) state.last = { symbol: want, tab: "kline" };
    watchTheme();
    showSymbol(want, true);
    return ready;
  }
  function periodFor(iv) { return iv === "W" ? { type: "week", span: 1 } : { type: "day", span: 1 }; }
  function showSymbol(id, first) {
    if (!chart || !byId[id]) return;
    if (!first) { saveNow(); }
    curSym = id; state.last.symbol = id; state.last.tab = "kline";
    var s = byId[id], st = sc();
    restoring = true; clearOverlays(true); clearIndicators(); restoring = false;
    metas = {}; selId = null; pendingId = null; activeTool = ""; paintTools(); hideStyle();
    needRestore = true;
    chart.setSymbol({ ticker: id, pricePrecision: s.dec, volumePrecision: 0 });
    chart.setPeriod(periodFor(st.interval));
    paintTop();
    if (!stateEl.hidden && first) { /* the loading state is removed in afterLoad */ }
  }
  function afterLoad() {
    if (!chart || !curSym) return;
    var st = sc();
    if (needRestore) {
      needRestore = false;
      restoring = true;
      try {
        applyType(st.type); applyScale(st.scale);
        st.indicators.forEach(function (r) { try { mkInd(r); } catch (e) { /* skip one bad item */ } });
        st.overlays.forEach(function (r) { try { mkOv(r); } catch (e) { /* skip one bad item */ } });
        applyLock(true); applyHide(true);
      } catch (e) { /* the chart still opens without the saved extras */ }
      finally { restoring = false; }
      hist = [snapOv()]; hidx = 0; paintHist();
      renderChips();
    }
    try { setRange(st.range, false); } catch (e) { /* keep going */ }
    finally {
      if (stateEl) stateEl.hidden = true;
      try { paintTop(); readLast(); } catch (e) { /* keep going */ }
      if (!firstReady) { firstReady = true; resolveReady(); }
    }
  }
  function paintTop() {
    if (!chart || !curSym) return;
    var st = sc(), s = byId[curSym];
    [].forEach.call(symBar.children, function (b) { b.setAttribute("aria-pressed", String(b.dataset.s === curSym)); });
    Object.keys(ivBtns).forEach(function (k) { ivBtns[k].setAttribute("aria-pressed", String(k === st.interval)); });
    Object.keys(rgBtns).forEach(function (k) { rgBtns[k].setAttribute("aria-pressed", String(k === st.range)); });
    Object.keys(scBtns).forEach(function (k) { scBtns[k].setAttribute("aria-pressed", String(k === st.scale)); });
    typeSel.value = st.type;
    var last = s.ts[s.ts.length - 1];
    var line = (st.interval === "W" ? "Weekly last close of the daily series" : (s.delay || "Daily close")) + " \u00B7 " + (s.src || "source not stated") + " \u00B7 last point " + iso(last) + " \u00B7 " + s.ts.length.toLocaleString("en-US") + " points, saved by the dashboard (not live)";
    if (s.partial && !/intraday/i.test(s.delay || "")) line += " \u00B7 latest point may be intraday until the close";
    if (dataNote) { if (!dataNoteBase) dataNoteBase = dataNote.textContent; dataNote.textContent = dataNoteBase + (s.partial ? " The latest point may be an intraday price until the close." : ""); }
    if (st.type !== "area") line += " \u00B7 drawn close-to-close: daily closes only, no intraday high/low";
    srcEl.textContent = line;
    lockB.setAttribute("aria-pressed", String(locked)); hideB.setAttribute("aria-pressed", String(hidden_));
    magB.setAttribute("aria-pressed", String(magnet > 0));
    magB.textContent = "Magnet: " + ["off", "weak", "strong"][magnet]; magB.title = magB.textContent + " (click to cycle)";
  }

  /* ---------- interval, range, type, scale ---------- */
  function setInterval_(iv) {
    if (!chart || sc().interval === iv) return;
    sc().interval = iv; chart.setPeriod(periodFor(iv)); paintTop(); saveSoon();
  }
  function rangeStart(r, lastTs) {
    var d = new Date(lastTs);
    if (r === "All") return -Infinity;
    if (r === "YTD") return Date.UTC(d.getUTCFullYear(), 0, 1);
    var days = { "1M": 30, "3M": 91, "6M": 182, "1Y": 365, "5Y": 1826 }[r] || 365;
    return lastTs - days * DAY;
  }
  function setRange(r, user) {
    if (!chart) return;
    var st = sc(); if (RANGES.indexOf(r) < 0) r = "1Y";
    st.range = r;
    var data = chart.getDataList(); if (!data.length) return;
    var from = rangeStart(r, data[data.length - 1].timestamp), lo = 0, hi = data.length - 1;
    while (lo < hi) { var mid = (lo + hi) >> 1; if (data[mid].timestamp < from) lo = mid + 1; else hi = mid; }
    var n = Math.max(8, data.length - lo), size = chart.getSize("candle_pane", "main"), w = size ? size.width : chartEl.clientWidth;
    try { chart.setBarSpace(clamp((w - 60) / n, 0.05, 60)); chart.scrollToRealTime(); } catch (e) {}
    paintTop(); if (user) saveSoon();
  }
  function applyType(t) { if (!TYPES[t]) t = "area"; try { chart.setStyles({ candle: { type: t } }); } catch (e) {} }
  function setType(t) { if (!chart) return; sc().type = TYPES[t] ? t : "area"; applyType(sc().type); paintTop(); saveSoon(); }
  function applyScale(s) { try { chart.overrideYAxis({ name: SCALES[s] || "normal", paneId: "candle_pane" }); } catch (e) {} }
  function setScale(s) { if (!chart) return; sc().scale = SCALES[s] ? s : "normal"; applyScale(sc().scale); paintTop(); saveSoon(); }

  /* crosshair readout: date and close; O/H/L only when the feed really has them */
  function onCross(c) {
    var s = byId[curSym]; if (!s) return;
    var d = c && c.kLineData; if (!d) { readLast(); return; }
    showRead(d, c.dataIndex, s);
  }
  function showRead(d, idx, s) {
    var data = chart.getDataList(), prev = idx > 0 ? data[idx - 1] : null, ch = prev ? d.close - prev.close : 0, pc = prev && prev.close ? ch / prev.close * 100 : 0;
    var dec = s.dec, u = s.unit === "$" ? "$" : "";
    readEl.textContent = "";
    readEl.appendChild(document.createTextNode(iso(d.timestamp) + " \u00B7 " + (s.partial && idx === chart.getDataList().length - 1 ? "Latest " : "Close ")));
    readEl.appendChild(el("b", "", u + fmtNum(d.close, dec)));
    if (s.ohlc) readEl.appendChild(document.createTextNode(" \u00B7 O " + fmtNum(d.open, dec) + " H " + fmtNum(d.high, dec) + " L " + fmtNum(d.low, dec)));
    if (prev) { var sp = el("span", ch > 0 ? "kl-pos" : ch < 0 ? "kl-neg" : "", " \u00B7 " + arrow(ch) + sgn(ch, dec) + " (" + sgn(pc, 2, "%") + ")"); readEl.appendChild(sp); }
  }
  function readLast() { var s = byId[curSym], data = chart && chart.getDataList(); if (s && data && data.length) showRead(data[data.length - 1], data.length - 1, s); }

  /* ---------- overlays ---------- */
  function defaultMeta(n) { return { ck: "primary", w: n === "brush" ? 2 : 1, dash: "solid", fill: !!(n === "vlRect" || n === "vlRange" || n === "vlMeasure") }; }
  function defaultText(n) { return n === "simpleAnnotation" ? "Note" : n === "simpleTag" ? "Marker" : n === "vlRange" ? "Range box" : ""; }
  function modeName() { return ["normal", "weak_magnet", "strong_magnet"][magnet]; }
  function evCbs() {
    return {
      onDrawEnd: function (e) { onDrawEnd(e.overlay.id); },
      onPressedMoveEnd: function () { if (!restoring) { histPush(); saveSoon(); } },
      onRemoved: function (e) { delete metas[e.overlay.id]; if (selId === e.overlay.id) { selId = null; hideStyle(); } if (!restoring) { histPush(); saveSoon(); } },
      onSelected: function (e) { select(e.overlay.id); },
      onDeselected: function () { select(null); },
      onDoubleClick: function (e) { select(e.overlay.id); }
    };
  }
  function mkOv(rec) {
    if (!rec || !TOOLNAME[rec.n]) return null;
    var m = rec.m || defaultMeta(rec.n), cfg = evCbs();
    cfg.name = rec.n; cfg.lock = !!rec.lock; cfg.visible = true; cfg.mode = "normal"; cfg.styles = ovStyles(m);
    cfg.points = rec.p.map(function (q) { return { timestamp: q.t, value: q.v }; });
    var txt = rec.x != null ? rec.x : defaultText(rec.n); if (NOTEY[rec.n]) cfg.extendData = txt;
    var id = chart.createOverlay(cfg);
    if (typeof id === "string") metas[id] = { ck: m.ck, w: m.w, dash: m.dash, fill: m.fill };
    return id;
  }
  function pickTool(n) {
    if (!chart) return;
    if (pendingId) { try { chart.removeOverlay({ id: pendingId }); } catch (e) {} pendingId = null; }
    activeTool = n; paintTools();
    if (!n) return;
    if (locked) { locked = false; applyLock(); }
    if (hidden_) { hidden_ = false; applyHide(); }
    var m = defaultMeta(n), cfg = evCbs();
    cfg.name = n; cfg.mode = modeName(); cfg.styles = ovStyles(m); if (NOTEY[n]) cfg.extendData = defaultText(n);
    var id = chart.createOverlay(cfg);
    if (typeof id === "string") { pendingId = id; metas[id] = m; }
    else { activeTool = ""; paintTools(); }
  }
  function onDrawEnd(id) {
    if (pendingId === id) pendingId = null;
    var n = activeTool; activeTool = ""; paintTools();
    if (restoring) return;
    histPush(); saveSoon();
    select(id);
    if (n === "simpleAnnotation" || n === "simpleTag") { var inp = styleEl.querySelector("input.kl-in"); if (inp) { inp.focus(); inp.select(); } }
  }
  function paintTools() { Object.keys(toolBtns).forEach(function (k) { toolBtns[k].setAttribute("aria-pressed", String(k === activeTool)); }); }
  function cycleMagnet() { magnet = (magnet + 1) % 3; state.last.magnet = magnet; paintTop(); if (chart) chart.getOverlays().forEach(function (o) { chart.overrideOverlay({ id: o.id, mode: modeName() }); }); }
  function applyLock(quiet) { if (!chart) return; chart.getOverlays().forEach(function (o) { if (o.id !== pendingId) chart.overrideOverlay({ id: o.id, lock: locked }); }); if (!quiet) paintTop(); }
  function applyHide(quiet) {
    if (!chart) return; chart.getOverlays().forEach(function (o) { chart.overrideOverlay({ id: o.id, visible: !hidden_ }); });
    if (!quiet) { paintTop(); if (hidden_) hideStyle(); }
  }
  function clearOverlays(silent) {
    if (!chart) return; var was = restoring; restoring = true;
    try { chart.removeOverlay(); } catch (e) {}
    restoring = was; metas = {}; selId = null; pendingId = null; hideStyle();
  }
  function deleteSelected() { if (chart && selId) { try { chart.removeOverlay({ id: selId }); } catch (e) {} } }
  function isComplete(o) { return !!TOOLNAME[o.name] && o.id !== pendingId && o.currentStep === -1 && o.points && o.points.length > 0; }
  function snapOv() {
    if (!chart) return [];
    var out = [];
    chart.getOverlays().forEach(function (o) {
      if (!metas[o.id] || !isComplete(o)) return;
      var pts = o.points.filter(function (q) { return fin(q.timestamp) && fin(q.value); }).map(function (q) { return { t: q.timestamp, v: +q.value.toFixed(6) }; });
      if (!pts.length) return;
      var r = { n: o.name, p: pts, m: metas[o.id] };
      if (o.lock) r.lock = true;
      if (typeof o.extendData === "string") r.x = o.extendData;
      out.push(r);
    });
    return out;
  }
  /* undo / redo: snapshots of the overlay list */
  function histPush() {
    if (restoring) return;
    var s = JSON.stringify(snapOv()); if (hist[hidx] && JSON.stringify(hist[hidx]) === s) return;
    hist = hist.slice(0, hidx + 1); hist.push(JSON.parse(s)); if (hist.length > 60) hist.shift(); hidx = hist.length - 1; paintHist();
  }
  function paintHist() { if (undoB) { undoB.disabled = hidx <= 0; redoB.disabled = hidx >= hist.length - 1; } }
  function restoreHist(i) {
    if (!chart || i < 0 || i >= hist.length) return;
    hidx = i; restoring = true; clearOverlays(true);
    hist[i].forEach(function (r) { mkOv(r); });
    applyLock(true); applyHide(true); restoring = false; paintHist(); saveSoon();
  }
  function undo() { restoreHist(hidx - 1); }
  function redo() { restoreHist(hidx + 1); }

  /* selected-object style bar */
  function select(id) {
    selId = id;
    if (!id || !metas[id] || hidden_) { hideStyle(); return; }
    var o = chart.getOverlays({ id: id })[0]; if (!o) { hideStyle(); return; }
    var m = metas[id]; styleEl.textContent = ""; styleEl.hidden = false;
    var tl = TOOLNAME[o.name];
    styleEl.appendChild(el("span", "nm", tl ? tl.t.split(" (")[0] : o.name));
    COLKEYS.forEach(function (c) {
      var b = el("button", "kl-sw"); b.type = "button"; b.dataset.c = c; b.title = c === "primary" ? "Primary (blue)" : c === "secondary" ? "Secondary (purple)" : c === "warn" ? "Warning colour" : c === "text" ? "Text colour" : "Muted";
      b.setAttribute("aria-label", b.title); b.setAttribute("aria-pressed", String(m.ck === c));
      b.addEventListener("click", function () { m.ck = c; applyMeta(id); select(id); });
      styleEl.appendChild(b);
    });
    var ws = el("select", "kl-sel"); ws.setAttribute("aria-label", "Line width");
    [1, 2, 3, 4].forEach(function (w) { var op = el("option", "", w + " px"); op.value = w; if (w === m.w) op.selected = true; ws.appendChild(op); });
    ws.addEventListener("change", function () { m.w = +ws.value; applyMeta(id); });
    var ds = el("select", "kl-sel"); ds.setAttribute("aria-label", "Line dash");
    DASHES.forEach(function (d) { var op = el("option", "", d); op.value = d; if (d === m.dash) op.selected = true; ds.appendChild(op); });
    ds.addEventListener("change", function () { m.dash = ds.value; applyMeta(id); });
    styleEl.appendChild(ws); styleEl.appendChild(ds);
    if (o.name in FILLY) {
      var fb = btn("Fill", "Fill the shape"); fb.setAttribute("aria-pressed", String(!!m.fill));
      fb.addEventListener("click", function () { m.fill = !m.fill; fb.setAttribute("aria-pressed", String(m.fill)); applyMeta(id); }); styleEl.appendChild(fb);
    }
    if (NOTEY[o.name]) {
      var inp = el("input", "kl-in wide"); inp.type = "text"; inp.maxLength = 60; inp.value = typeof o.extendData === "string" ? o.extendData : ""; inp.setAttribute("aria-label", "Label");
      inp.addEventListener("input", function () { chart.overrideOverlay({ id: id, extendData: inp.value }); });
      inp.addEventListener("change", function () { histPush(); saveSoon(); });
      inp.addEventListener("keydown", function (e) { if (e.key === "Enter") inp.blur(); e.stopPropagation(); });
      styleEl.appendChild(inp);
    }
    var lk = btn(o.lock ? "Unlock" : "Lock", "Lock or unlock this drawing");
    lk.addEventListener("click", function () { chart.overrideOverlay({ id: id, lock: !o.lock }); histPush(); saveSoon(); select(id); });
    var dl = btn("Delete", "Delete this drawing (Del)"); dl.addEventListener("click", deleteSelected);
    styleEl.appendChild(lk); styleEl.appendChild(dl);
  }
  function hideStyle() { if (styleEl) styleEl.hidden = true; }
  function applyMeta(id) { chart.overrideOverlay({ id: id, styles: ovStyles(metas[id]) }); histPush(); saveSoon(); }

  /* ---------- custom overlays ---------- */
  function regOverlay(k, def) { try { k.registerOverlay(def); } catch (e) { /* already registered */ } }
  function dataIdx(chartRef, ts) {
    var d = chartRef.getDataList(), lo = 0, hi = d.length - 1;
    while (lo < hi) { var mid = (lo + hi) >> 1; if (d[mid].timestamp < ts) lo = mid + 1; else hi = mid; }
    return lo;
  }
  function labelFig(x, y, text, align) { return { type: "text", attrs: { x: x, y: y, text: text, align: align || "left", baseline: "bottom" }, ignoreEvent: true }; }
  function registerCustom(k) {
    function box(name, label) {
      regOverlay(k, {
        name: name, totalStep: 3, needDefaultPointFigure: true, needDefaultXAxisFigure: true, needDefaultYAxisFigure: true,
        createPointFigures: function (p) {
          var c = p.coordinates; if (c.length < 2) return [];
          var x = Math.min(c[0].x, c[1].x), y = Math.min(c[0].y, c[1].y), w = Math.abs(c[1].x - c[0].x), h = Math.abs(c[1].y - c[0].y), figs = [{ type: "rect", attrs: { x: x, y: y, width: w, height: h } }];
          var txt = typeof p.overlay.extendData === "string" ? p.overlay.extendData : label;
          if (name === "vlRange") {
            var pts = p.overlay.points, a = pts[0].value, b = pts[1] && pts[1].value;
            if (fin(a) && fin(b)) {
              var dec = (p.chart.getSymbol() || {}).pricePrecision || 2, hi = Math.max(a, b), lo = Math.min(a, b);
              txt = (txt || "Range box") + ": " + lo.toFixed(dec) + " to " + hi.toFixed(dec) + " (" + (hi - lo).toFixed(dec) + ", " + (lo ? ((hi - lo) / lo * 100).toFixed(2) : "0") + "%)";
            }
          }
          if (txt) figs.push(labelFig(x + 4, y - 2, txt));
          return figs;
        }
      });
    }
    box("vlRect", ""); box("vlRange", "Range box");
    regOverlay(k, {
      name: "vlArrow", totalStep: 3, needDefaultPointFigure: true, needDefaultXAxisFigure: true, needDefaultYAxisFigure: true,
      createPointFigures: function (p) {
        var c = p.coordinates; if (c.length < 2) return [];
        var a = c[0], b = c[1], ang = Math.atan2(b.y - a.y, b.x - a.x), L = 12, s = 0.45;
        return [{ type: "line", attrs: { coordinates: [a, b] } },
                { type: "line", attrs: { coordinates: [{ x: b.x - L * Math.cos(ang - s), y: b.y - L * Math.sin(ang - s) }, b, { x: b.x - L * Math.cos(ang + s), y: b.y - L * Math.sin(ang + s) }] } }];
      }
    });
    regOverlay(k, {
      name: "vlMeasure", totalStep: 3, needDefaultPointFigure: true, needDefaultXAxisFigure: true, needDefaultYAxisFigure: true,
      createPointFigures: function (p) {
        var c = p.coordinates; if (c.length < 2) return [];
        var x = Math.min(c[0].x, c[1].x), y = Math.min(c[0].y, c[1].y), w = Math.abs(c[1].x - c[0].x), h = Math.abs(c[1].y - c[0].y);
        var pts = p.overlay.points, a = pts[0], b = pts[1], figs = [{ type: "rect", attrs: { x: x, y: y, width: w, height: h } }, { type: "line", attrs: { coordinates: [c[0], c[1]] } }];
        if (a && b && fin(a.value) && fin(b.value)) {
          var dec = (p.chart.getSymbol() || {}).pricePrecision || 2, dv = b.value - a.value, pc = a.value ? dv / a.value * 100 : 0;
          var bars = Math.abs(dataIdx(p.chart, b.timestamp) - dataIdx(p.chart, a.timestamp));
          figs.push(labelFig(x + 4, y - 2, arrow(dv) + sgn(dv, dec) + " (" + sgn(pc, 2, "%") + ") \u00B7 " + bars + " bars"));
        }
        return figs;
      }
    });
    /* indicators */
    try {
      k.registerIndicator({
        name: "VL_ATR", shortName: "ATR (close-to-close)", calcParams: [14], precision: 4,
        figures: [{ key: "atr", title: "ATR: ", type: "line" }],
        calc: function (list, ind) {
          var n = Math.max(1, ind.calcParams[0] | 0), out = [], prev = null, sum = 0, i, tr;
          for (i = 0; i < list.length; i++) {
            tr = i ? Math.abs(list[i].close - list[i - 1].close) : 0;
            if (i === 0) { out.push({}); prev = null; continue; }
            if (i < n) { sum += tr; out.push({}); if (i === n - 1) { prev = sum / n; out[i] = { atr: prev }; } continue; }
            if (prev == null) { sum += tr; prev = sum / n; } else prev = (prev * (n - 1) + tr) / n;
            out.push({ atr: prev });
          }
          return out;
        }
      });
      k.registerIndicator({
        name: "VL_CMP", shortName: "Compare", calcParams: [], precision: 2, shouldOhlc: false,
        figures: [{ key: "cmp", title: "", type: "line" }],
        calc: function (list, ind) {
          var ex = ind.extendData || {}, o = byId[ex.sym], out = [], j = 0, i;
          if (!o) return list.map(function () { return {}; });
          var ob = barsFor(o, chart && chart.getPeriod() && chart.getPeriod().type === "week" ? "W" : "D"), base = null, fac = null;
          for (i = 0; i < list.length; i++) {
            while (j + 1 < ob.length && ob[j + 1].timestamp <= list[i].timestamp) j++;
            if (!ob.length || ob[j].timestamp > list[i].timestamp) { out.push({}); continue; }
            if (base == null) { base = i; fac = list[i].close / ob[j].close; }
            out.push({ cmp: ob[j].close * fac, chg: 0, ref: ob[j].close });
          }
          var first = out[base]; if (first) out.forEach(function (r) { if (r.cmp != null) { r.chg = (r.ref / first.ref - 1) * 100; } });
          return out;
        },
        createTooltipDataSource: function (p) {
          var ex = p.indicator.extendData || {}, o = byId[ex.sym], idx = p.crosshair.dataIndex, r = (p.indicator.result || [])[idx == null ? (p.indicator.result || []).length - 1 : idx] || {};
          var txt = r.chg == null ? "n/a" : arrow(r.chg) + sgn(r.chg, 2, "%");
          return { name: "Compare " + (o ? (LABELS[o.id] || o.id) : ""), calcParamsText: "(rebased to the start of the visible data)", features: [], legends: [{ title: "Change since start: ", value: { text: txt, color: r.chg < 0 ? T.secondary : T.primary } }] };
        }
      });
    } catch (e) { /* already registered */ }
  }

  /* ---------- indicators ---------- */
  var indRecs = [];
  function stackedPane(name) { return INDS[name] && INDS[name].pane === "candle"; }
  function mkInd(rec) {
    var d = INDS[rec.n]; if (!d || !chart) return null;
    var cfg = { name: rec.n, calcParams: rec.p && rec.p.length ? rec.p : d.params.slice(), visible: rec.vis !== false, precision: rec.n === "VL_ATR" || rec.n === "VL_CMP" ? (byId[curSym] || { dec: 2 }).dec : 2 };
    if (d.params.length <= 1) { var pal = [T.warn, T.secondary, T.text, T.primary, T.muted], ix = Math.max(0, sc().indicators.indexOf(rec)); cfg.styles = { lines: [{ color: pal[ix % pal.length] }] }; }
    if (rec.n === "VL_CMP") { if (!byId[rec.sym]) return null; cfg.extendData = { sym: rec.sym }; }
    if (stackedPane(rec.n)) cfg.paneId = "candle_pane";
    var id = chart.createIndicator(cfg, stackedPane(rec.n));
    if (id) rec.kid = id;
    return id;
  }
  function clearIndicators() { if (!chart) return; try { chart.removeIndicator(); } catch (e) {} }
  function addInd(n, extra) {
    if (!chart) return;
    var d = INDS[n], rec = { n: n, p: d.params.slice(), vis: true };
    if (extra) Object.keys(extra).forEach(function (k) { rec[k] = extra[k]; });
    var st = sc(); if (st.indicators.length >= 12) { say("At most 12 indicators per symbol.", true); return; }
    if (mkInd(rec)) { st.indicators.push(rec); renderChips(); saveSoon(); }
  }
  function renderChips() {
    chipsEl.textContent = "";
    var st = sc();
    st.indicators.forEach(function (r, i) {
      var d = INDS[r.n]; if (!d) return;
      var c = el("span", "kl-chip" + (r.vis === false ? " off" : "")); c.appendChild(el("span", "", d.label + (r.n === "VL_CMP" ? " " + (LABELS[r.sym] || r.sym) : "")));
      (d.pl || []).forEach(function (lab, j) {
        var inp = el("input", "kl-in"); inp.type = "number"; inp.min = 1; inp.max = 400; inp.step = 1; inp.value = r.p[j]; inp.title = lab; inp.setAttribute("aria-label", d.label + " " + lab.toLowerCase());
        inp.addEventListener("change", function () {
          var v = clamp(Math.round(+inp.value) || d.params[j], 1, 400); inp.value = v; r.p[j] = v;
          if (r.kid) chart.overrideIndicator({ id: r.kid, calcParams: r.p.slice() }); saveSoon();
        });
        inp.addEventListener("keydown", function (e) { e.stopPropagation(); });
        c.appendChild(inp);
      });
      var eye = btn(r.vis === false ? "Show" : "Hide", "Show or hide this indicator");
      eye.addEventListener("click", function () { r.vis = r.vis === false; if (r.kid) chart.overrideIndicator({ id: r.kid, visible: r.vis }); renderChips(); saveSoon(); });
      var rm = btn("\u00D7", "Remove this indicator"); rm.setAttribute("aria-label", "Remove " + d.label);
      rm.addEventListener("click", function () { if (r.kid) chart.removeIndicator({ id: r.kid }); st.indicators.splice(i, 1); renderChips(); saveSoon(); });
      c.appendChild(eye); c.appendChild(rm); chipsEl.appendChild(c);
    });
  }
  function buildIndPop(pop) {
    pop.textContent = "";
    pop.appendChild(el("h4", "", "On the price"));
    ["MA", "EMA", "BOLL"].forEach(function (n) { addRow(pop, n, INDS[n].label, INDS[n].tip); });
    pop.appendChild(el("h4", "", "Own pane"));
    ["RSI", "MACD", "VL_ATR"].forEach(function (n) { addRow(pop, n, INDS[n].label, INDS[n].tip); });
    pop.appendChild(el("h4", "", "Compare"));
    var li = el("div", "kl-li"), sel = el("select", "kl-sel"); sel.setAttribute("aria-label", "Compare symbol");
    feed.forEach(function (s) { if (s.id !== curSym) { var o = el("option", "", LABELS[s.id] || s.name); o.value = s.id; sel.appendChild(o); } });
    var ab = btn("Add", "Overlay this symbol rebased to the same start, shown as a % change in the legend");
    ab.addEventListener("click", function () { if (sel.value) { addInd("VL_CMP", { sym: sel.value }); closePop(pop, indBtn); } });
    li.appendChild(sel); li.appendChild(ab); pop.appendChild(li);
    pop.appendChild(el("h4", "", "Needs data we do not have"));
    ["VOL", "VWAP"].forEach(function (n) { var r = el("div", "kl-li"); r.appendChild(el("span", "", n)); r.appendChild(el("span", "kl-why", "needs volume data the dashboard does not have")); r.setAttribute("aria-disabled", "true"); pop.appendChild(r); });
  }
  function addRow(pop, n, label, tip) {
    var r = el("div", "kl-li"), s = el("span"); s.appendChild(document.createTextNode(label)); s.appendChild(el("div", "kl-why", tip)); r.appendChild(s);
    var b = btn("Add", "Add " + label); b.addEventListener("click", function () { addInd(n); closePop(pop, indBtn); }); r.appendChild(b); pop.appendChild(r);
  }
  /* Phase 2 hook: desk levels for a symbol (internal build only). Empty until Phase 2. */
  function levelsFor(symbol) {
    try { if (!(window.P && window.P.opt)) return []; } catch (e) { return []; }
    return [];   // Phase 2 fills this from the internal desk payload
  }


  /* ---------- options desk price chart (internal build): a second, separate chart instance. It never touches the main chart's
     state, drawings or saved layout. Levels are drawn as labelled horizontal lines in the chart's own price units. ---------- */
  function deskSymbol(optKey) {
    try {
      parseFeed();
      for (var i = 0; i < feed.length; i++) {
        var o = feed[i]; if (o.optKey && o.optKey === optKey) {
          var n = o.c.length; return { id: o.id, name: o.name, src: o.src, delay: o.delay, unit: o.unit, dec: o.dec, optUnits: o.optUnits, n: n, lastDate: iso(o.ts[n - 1]), lastClose: o.c[n - 1], partial: o.partial };
        }
      }
    } catch (e) { /* no series */ }
    return null;
  }
  var DESK_BARS = { "1M": 22, "3M": 66, "6M": 132, "1Y": 264, "All": 1e9 };
  var DESK_SLOT = { cw: 0, pw: 1, flip: 2, mp: 3 };
  function registerLevel(k) {
    regOverlay(k, {
      name: "vlLevel", totalStep: 2, lock: true, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: true,
      createPointFigures: function (p) {
        var c = p.coordinates[0], ex = p.overlay.extendData || {}; if (!c || !p.bounding) return [];
        var w = p.bounding.width, h = p.bounding.height, y = c.y, off = "";
        if (y < 9) { y = 9; off = " ▲"; } else if (y > h - 9) { y = h - 9; off = " ▼"; }
        var col = T[ex.ck] || T.text, solid = ex.dash === "solid", dot = ex.dash === "dot", slot = DESK_SLOT[ex.id], tx = slot == null ? 6 : w - 6 - slot * 74;
        var label = String(ex.label || "") + off;
        return [
          { type: "line", attrs: { coordinates: [{ x: 0, y: y }, { x: w, y: y }] }, styles: { style: solid ? "solid" : "dashed", dashedValue: dot ? [2, 3] : [7, 4], color: col, size: ex.w || 1 }, ignoreEvent: true },
          { type: "text", attrs: { x: tx, y: y - 2, text: label, align: slot == null ? "left" : "right", baseline: "bottom" }, styles: { style: "fill", color: col, size: 10, family: T.font, backgroundColor: "transparent", borderSize: 0, paddingLeft: 0, paddingRight: 0, paddingTop: 0, paddingBottom: 0 }, ignoreEvent: true }
        ];
      }
    });
  }
  function mountDesk(host, symbol, levels, opts) {
    var h = { dead: false, chart: null, sym: symbol, levels: levels || [], range: (opts && opts.range) || "6M", ids: [], tries: 0, err: null };
    h.ready = new Promise(function (res, rej) { h._res = res; h._rej = rej; }); h.ready.catch(function () {});
    function lastBars() { return h.chart ? h.chart.getDataList() : []; }
    function drawLevels() {
      if (!h.chart) return;
      h.ids.forEach(function (id) { try { h.chart.removeOverlay({ id: id }); } catch (e) {} }); h.ids = [];
      var d = lastBars(); if (!d.length) return;
      var ts = d[d.length - 1].timestamp;
      readTokens();
      h.levels.forEach(function (L) {
        if (!fin(L.value)) return;
        try {
          var id = h.chart.createOverlay({ name: "vlLevel", lock: true, visible: true, points: [{ timestamp: ts, value: L.value }], extendData: { id: L.id, label: L.label, ck: L.ck, dash: L.dash, w: L.w },
            styles: { text: { color: T.onp, backgroundColor: T[L.ck] || T.text, borderColor: T[L.ck] || T.text, family: T.font, size: 11 } } });
          if (id) h.ids.push(id);
        } catch (e) { /* skip one level */ }
      });
    }
    function applyRange() {
      var d = lastBars(); if (!d.length || !h.chart) return;
      var n = Math.min(d.length, DESK_BARS[h.range] || DESK_BARS["6M"]), w = Math.max(120, host.clientWidth - 70);
      try { h.chart.setBarSpace(Math.max(1.5, Math.min(30, w / n))); h.chart.scrollToRealTime(); } catch (e) {}
    }
    function afterData() {
      if (h.dead || !h.chart) return;
      if (!lastBars().length) { if (++h.tries < 20) setTimeout(afterData, 50); return; }
      applyRange(); drawLevels(); h._res(h);
    }
    h.update = function (sym, lv, o) {
      if (h.dead || !h.chart) { h.sym = sym; h.levels = lv || []; if (o && o.range) h.range = o.range; return; }
      try { h.chart.resize(); } catch (e) {}
      var changed = sym !== h.sym; h.sym = sym; h.levels = lv || []; if (o && o.range) h.range = o.range;
      readTokens(); try { h.chart.setStyles(themeStyles()); h.chart.setStyles({ candle: { type: "area" } }); } catch (e) {}
      if (changed) { var s = byId[sym]; if (!s) return; h.tries = 0; try { h.chart.setSymbol({ ticker: sym, pricePrecision: s.dec, volumePrecision: 0 }); h.chart.setPeriod({ type: "day", span: 1 }); } catch (e) {} setTimeout(afterData, 0); }
      else { applyRange(); drawLevels(); }
    };
    h.resize = function () { if (h.chart && !h.dead) { try { h.chart.resize(); } catch (e) {} applyRange(); drawLevels(); } };
    h.destroy = function () { h.dead = true; ids_clear(); try { if (h.chart && window.klinecharts) window.klinecharts.dispose(host); } catch (e) {} h.chart = null; };
    function ids_clear() { h.ids = []; }
    h.getChart = function () { return h.chart; };
    parseFeed();
    if (!byId[symbol]) { h.err = "no series"; h._rej(new Error("no series")); return h; }
    loadLib().then(function () {
      if (h.dead) return;
      var k = window.klinecharts; readTokens(); registerCustom(k); registerLevel(k);
      var c = k.init(host, { timezone: "UTC", styles: themeStyles() }); if (!c) throw new Error("init failed");
      h.chart = c;
      try { c.setStyles({ candle: { type: "area" } }); c.setOffsetRightDistance(16); } catch (e) {}
      c.setDataLoader({ getBars: function (p) { var s = byId[p.symbol.ticker]; p.callback(s ? barsFor(s, "D") : [], false); } });
      var s0 = byId[h.sym]; c.setSymbol({ ticker: h.sym, pricePrecision: s0.dec, volumePrecision: 0 }); c.setPeriod({ type: "day", span: 1 });
      setTimeout(afterData, 0);
    }).catch(function (err) { h.err = String(err && err.message || err); h._rej(err); });
    return h;
  }

  /* ---------- keyboard ---------- */
  function onKey(e) {
    if (!chart || !stage || !root.offsetParent && !root.classList.contains("kl-full")) return;
    var t = e.target, tag = t && t.tagName;
    if (e.key === "Escape") {
      if (!indPop.hidden || !helpPop.hidden) { closePops(); e.preventDefault(); return; }
      if (pendingId || activeTool) { pickTool(""); e.preventDefault(); return; }
      if (root.classList.contains("kl-full")) { e.preventDefault(); exitFull(); }
      return;
    }
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (!root.contains(t) && !root.classList.contains("kl-full") && t !== document.body) return;
    if (e.altKey && !e.ctrlKey && !e.metaKey) {
      var hit = TOOLS.filter(function (x) { return x.code === e.code; })[0];
      if (hit) { e.preventDefault(); pickTool(hit.n); }
      return;
    }
    if ((e.key === "Delete" || e.key === "Backspace") && selId) { e.preventDefault(); deleteSelected(); return; }
    if ((e.ctrlKey || e.metaKey) && !e.altKey) {
      var k = e.key.toLowerCase();
      if (k === "z" && !e.shiftKey) { e.preventDefault(); undo(); } else if (k === "y" || (k === "z" && e.shiftKey)) { e.preventDefault(); redo(); }
    }
  }

  /* ---------- theme ---------- */
  var themeWatch = false;
  function applyTheme() {
    if (!chart) return;
    readTokens(); state.theme = pageTheme();
    chart.setStyles(themeStyles());
    chart.getOverlays().forEach(function (o) { if (metas[o.id]) chart.overrideOverlay({ id: o.id, styles: ovStyles(metas[o.id]) }); });
    saveSoon();
  }
  function watchTheme() {
    if (themeWatch) return; themeWatch = true;
    new MutationObserver(applyTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    if (window.matchMedia) { var mq = window.matchMedia("(prefers-color-scheme: light)"); if (mq.addEventListener) mq.addEventListener("change", applyTheme); else if (mq.addListener) mq.addListener(applyTheme); }
  }

  /* ---------- snapshot, export, import ---------- */
  function snapshotCur() {
    if (!chart || !curSym || !firstReady || needRestore) return;
    var st = sc();
    try { adoptForeign(st); } catch (e) { /* never block a save */ }
    st.overlays = snapOv();
    state.last.symbol = curSym; state.last.magnet = magnet; state.theme = pageTheme();
  }
  /* drawings and indicators created straight through the chart API (VelourChart.chart().createOverlay / createIndicator)
     have no toolbar record yet: give them the default look and register them so they persist like toolbar ones */
  function adoptForeign(st) {
    chart.getOverlays().forEach(function (o) {
      if (metas[o.id] || !isComplete(o)) return;
      var m = defaultMeta(o.name); metas[o.id] = m;
      try { chart.overrideOverlay({ id: o.id, styles: ovStyles(m) }); } catch (e) {}
    });
    var known = {}; st.indicators.forEach(function (r) { if (r.kid) known[r.kid] = 1; });
    var added = false;
    chart.getIndicators().forEach(function (i) {
      if (known[i.id] || !INDS[i.name] || i.name === "VL_CMP" || st.indicators.length >= 12) return;
      var d = INDS[i.name], p = Array.isArray(i.calcParams) ? i.calcParams.slice(0, d.params.length) : d.params.slice();
      if (p.length !== d.params.length || !p.every(function (x) { return fin(x) && x >= 1 && x <= 400; })) p = d.params.slice();
      st.indicators.push({ n: i.name, p: p, vis: i.visible !== false, kid: i.id }); added = true;
    });
    if (added) renderChips();
  }
  function exportObj() {   // only symbols in today's feed: stored-but-hidden entries stay in localStorage, and strict import accepts the result
    var o = JSON.parse(JSON.stringify(state, dropKid)), left = 0, k, ind, i;
    for (k in o.symbols) {
      if (!byId[k]) { delete o.symbols[k]; left++; continue; }
      ind = o.symbols[k].indicators || [];
      for (i = ind.length - 1; i >= 0; i--) if (ind[i].n === "VL_CMP" && !byId[ind[i].sym]) { ind.splice(i, 1); left++; }
    }
    if (o.last && !byId[o.last.symbol]) o.last.symbol = null;
    if (!Object.keys(o.symbols).length) o.symbols[curSym && byId[curSym] ? curSym : feed[0].id] = freshSym();
    o.app = "velour-chart-layout"; o.saved = new Date().toISOString(); exportLeft = left; return o;
  }
  var exportLeft = 0;
  function doExport() {
    saveNow();
    try {
      var blob = new Blob([JSON.stringify(exportObj(), null, 1)], { type: "application/json" }), a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = "velour-chart-layout.json"; document.body.appendChild(a); a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 500);
      say("Layout exported as velour-chart-layout.json." + (exportLeft ? " " + exportLeft + " saved item(s) for series missing from today\u2019s data were left out (still kept in this browser)." : ""));
    } catch (e) { say("Export failed in this browser.", true); }
  }
  function validNum(x) { return fin(x) && Math.abs(x) < 1e15; }
  function validate(text, lenient) {   // lenient (saved layout only): entries for symbols missing from today's feed are checked and kept, never drawn
    var o; if (typeof text !== "string" || text.length > 2e6) return { error: "not a layout file" };
    try { o = JSON.parse(text); } catch (e) { return { error: "not valid JSON" }; }
    if (!isObj(o)) return { error: "not a layout object" };
    if (o.v !== VER) return { error: "unsupported layout version " + String(o.v) };
    if (!isObj(o.symbols)) return { error: "no symbols section" };
    var out = freshState(), ids = Object.keys(o.symbols), i, j;
    if (!ids.length) return { error: "no symbols in the file" };
    for (i = 0; i < ids.length; i++) {
      var id = ids[i], s = o.symbols[id];
      if (!byId[id] && !(lenient && /^[\w.\-^=]{1,40}$/.test(id) && id !== "__proto__" && ids.length <= 200)) return { error: "unknown symbol \"" + id.slice(0, 20) + "\"" };
      if (!isObj(s)) return { error: "bad data for " + id };
      var n = freshSym();
      if (s.interval === "D" || s.interval === "W") n.interval = s.interval;
      if (RANGES.indexOf(s.range) >= 0) n.range = s.range;
      if (TYPES[s.type]) n.type = s.type;
      if (SCALES[s.scale]) n.scale = s.scale;
      if (s.overlays != null) {
        if (!Array.isArray(s.overlays) || s.overlays.length > 500) return { error: "bad drawings list for " + id };
        for (j = 0; j < s.overlays.length; j++) {
          var r = s.overlays[j];
          if (!isObj(r) || !TOOLNAME[r.n]) return { error: "unknown drawing type in " + id };
          if (!Array.isArray(r.p) || !r.p.length || r.p.length > 3000 || !r.p.every(function (q) { return isObj(q) && validNum(q.t) && validNum(q.v); })) return { error: "bad drawing points in " + id };
          var m = isObj(r.m) ? r.m : {}, rec = { n: r.n, p: r.p.map(function (q) { return { t: q.t, v: q.v }; }),
            m: { ck: COLKEYS.indexOf(m.ck) >= 0 ? m.ck : "primary", w: clamp(Math.round(+m.w) || 1, 1, 4), dash: DASHES.indexOf(m.dash) >= 0 ? m.dash : "solid", fill: !!m.fill } };
          if (r.lock) rec.lock = true;
          if (typeof r.x === "string") rec.x = r.x.slice(0, 60);
          n.overlays.push(rec);
        }
      }
      if (s.indicators != null) {
        if (!Array.isArray(s.indicators) || s.indicators.length > 12) return { error: "bad indicator list for " + id };
        for (j = 0; j < s.indicators.length; j++) {
          var q = s.indicators[j];
          if (!isObj(q) || !INDS[q.n]) return { error: "unknown indicator in " + id };
          var d = INDS[q.n], p = Array.isArray(q.p) ? q.p.slice(0, d.params.length) : d.params.slice();
          if (p.length !== d.params.length || !p.every(function (x) { return fin(x) && x >= 1 && x <= 400; })) p = d.params.slice();
          var ir = { n: q.n, p: p, vis: q.vis !== false };
          if (q.n === "VL_CMP") { if (!byId[q.sym] && !(lenient && typeof q.sym === "string" && /^[\w.\-^=]{1,40}$/.test(q.sym))) return { error: "bad compare symbol in " + id }; ir.sym = q.sym; }
          n.indicators.push(ir);
        }
      }
      out.symbols[id] = n;
    }
    if (isObj(o.last) && byId[o.last.symbol]) out.last.symbol = o.last.symbol;
    out.theme = ["auto", "light", "dark"].indexOf(o.theme) >= 0 ? o.theme : "auto";
    return { ok: out };
  }
  function doImport(text) {
    if (!feed.length) { parseFeed(); }
    var v = validate(text);
    if (!v.ok) return { ok: false, error: v.error };
    var old = state.symbols, k;   // keep stored entries for series missing from today's feed (hidden, never drawn)
    state = v.ok; for (k in old) if (!byId[k] && Object.prototype.hasOwnProperty.call(old, k) && k !== "__proto__" && !Object.prototype.hasOwnProperty.call(state.symbols, k)) state.symbols[k] = old[k];
    if (!state.last.symbol) state.last.symbol = curSym || (feed[0] && feed[0].id);
    try { localStorage.setItem(KEY, JSON.stringify(state, dropKid)); } catch (e) { mem = JSON.stringify(state, dropKid); }
    if (chart) {
      var want = state.last.symbol || curSym;
      curSym = null; showSymbol(want, true);   // re-draws this symbol from the imported layout
    }
    return { ok: true };
  }

  /* ---------- start: the card may already be open on the kline tab when this script runs ---------- */
  setTimeout(function () {
    var card = document.getElementById("tvcard"), tab = document.querySelector('#chart-tabs [data-v="kline"]');
    if (card && card.open && tab && tab.getAttribute("aria-pressed") === "true" && phase === 0) onOpen();
  }, 0);
})();
