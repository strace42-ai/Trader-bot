// UI-проверка графиков в Chromium: значения панелей = ряды ядра (API), RSI 0–100 с уровнями, переключатель RSI, мобильный экран.
const { chromium } = require("playwright");
(async () => {
  const url = process.argv[2];
  const b = await chromium.launch();
  const errs = [];
  const p = await b.newPage({ viewport: { width: 2560, height: 1440 } });
  p.on("pageerror", (e) => errs.push(e.message));
  p.on("console", (m) => m.type() === "error" && errs.push(m.text()));
  await p.goto(url);
  await p.evaluate(() => { localStorage.setItem("lab.mode", "BACKTEST"); localStorage.setItem("lab.rsi", "1"); });
  await p.reload();
  await p.waitForFunction(() => window.__lab && ["1h", "4h", "1d"].every((tf) => window.__lab[tf].data), null, { timeout: 30000 });
  const check = () => p.evaluate(async () => {
    const out = {};
    for (const tf of ["1h", "4h", "1d"]) {
      const t = window.__lab[tf], s = t.s;
      const api = await (await fetch(`/api/terminal/${tf}?mode=BACKTEST`)).json();
      const cmp = (ser, key) => {
        if (!ser) return null;
        const m = new Map(api.series[key].map((x) => [x.time, x.value]));
        const d = ser.data();
        return { n_api: api.series[key].length, n_chart: d.length, maxdiff: Math.max(0, ...d.map((x) => Math.abs(x.value - m.get(x.time)))) };
      };
      const macd = new Map(s.macd.data().map((x) => [x.time, x.value])), sig = new Map(s.sig.data().map((x) => [x.time, x.value]));
      const hist = s.hist.data();
      out[tf] = {
        panes: t.chart.panes().length, paneMap: t.panes,
        rsiPane: s.rsi ? s.rsi.getPane().paneIndex() : null,
        rsi: cmp(s.rsi, "rsi"), zones: cmp(s.rsiOb, "rsi"), macd: cmp(s.macd, "macd"), signal: cmp(s.sig, "macd_signal"),
        hist: cmp(s.hist, "macd_hist"), mfi: cmp(s.mfi, "mfi"), cmf: cmp(s.cmf, "cmf"), obv: cmp(s.obv, "obv"),
        histVsDiff: Math.max(0, ...hist.map((x) => Math.abs(x.value - (macd.get(x.time) - sig.get(x.time))))),
        rsiLevels: s.rsi ? s.rsi.priceLines().map((l) => l.options().price).sort((a, c) => a - c) : null,
        rsiRange: s.rsi ? s.rsi.options().autoscaleInfoProvider().priceRange : null,
        zoneBases: s.rsi ? [s.rsiOs.options().baseValue.price, s.rsiOb.options().baseValue.price] : null,
        rsiLastLabel: s.rsi ? s.rsi.options().lastValueVisible : null,
        paneHeights: t.chart.panes().map((q) => q.getHeight()),
        rsiSpan3070: s.rsi ? Math.abs(s.rsi.priceToCoordinate(30) - s.rsi.priceToCoordinate(70)) : null,
        rsiY100: s.rsi ? s.rsi.priceToCoordinate(100) : null, rsiY0: s.rsi ? s.rsi.priceToCoordinate(0) : null,
        labels: [...document.querySelectorAll(`#t-${tf} .pane-label`)].map((x) => x.textContent),
        range: t.chart.timeScale().getVisibleLogicalRange(),
      };
    }
    return out;
  });
  const on = await check();
  await p.click('#t-1h [data-osc="rsi"]');                     // выключить RSI
  await p.waitForTimeout(500);
  const off = await check();
  await p.click('#t-1h [data-osc="rsi"]');                     // включить снова
  await p.waitForTimeout(500);
  const back = await check();
  const m = await b.newPage({ viewport: { width: 390, height: 844 } });
  m.on("pageerror", (e) => errs.push("mobile: " + e.message));
  await m.goto(url);
  await m.waitForFunction(() => window.__lab && window.__lab["1h"].data, null, { timeout: 30000 });
  const mobile = await m.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,
    rsiPaneH: window.__lab["1h"].s.rsi ? window.__lab["1h"].chart.panes()[window.__lab["1h"].panes.rsi].getHeight() : 0 }));
  if (process.argv[3]) await p.screenshot({ path: process.argv[3] });
  console.log(JSON.stringify({ on, off, back, mobile, errs }));
  await b.close();
})().catch((e) => { console.log(JSON.stringify({ fatal: String(e) })); process.exit(1); });
