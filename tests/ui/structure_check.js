// UI-проверка структуры v3: слои и их переключатели, отрезки событий = API, protected, 5 таблиц, подсказки, legacy выключен.
const { chromium } = require("playwright");
(async () => {
  const url = process.argv[2];
  const b = await chromium.launch(); const errs = [];
  const p = await b.newPage({ viewport: { width: 2560, height: 1440 } });
  p.on("pageerror", (e) => errs.push(e.message)); p.on("console", (m) => m.type() === "error" && errs.push(m.text()));
  await p.goto(url);
  await p.evaluate(() => { localStorage.setItem("lab.mode", "BACKTEST"); });
  await p.reload();
  await p.waitForFunction(() => window.__lab && ["1h", "4h", "1d"].every((tf) => window.__lab[tf].data), null, { timeout: 30000 });
  const state = (tf) => p.evaluate((tf) => {
    const t = window.__lab[tf], v = t.data.structure_v3;
    return { lines: t.v3lines, segs: (t.structSegs || []).map((x) => x.data().map((q) => [q.time, q.value])),
      markers: t.markers.markers().map((m) => [m.time, m.text]),
      plines: t.s.candle.priceLines().map((l) => [l.options().title, l.options().price]),
      events: v.major.events.map((e) => ({ type: e.type, ts: e.ts, level: e.level, src: e.src && e.src.ts })),
      swings: v.major.swings.map((w) => [w.ts, w.price]), state: v.major.state,
      toggles: [...document.querySelectorAll(`#t-${tf} [data-ov]`)].map((x) => [x.dataset.ov, x.checked]) };
  }, tf);
  const res = {};
  for (const tf of ["1h", "4h", "1d"]) res[tf] = await state(tf);
  // выключить все слои v3 → отрезков и v3-меток нет; включить internal → появляются
  for (const k of ["mswing", "mevents", "prot", "phase"]) await p.click(`#t-1h [data-ov="${k}"]`);
  await p.waitForTimeout(300);
  res.off = await state("1h");
  for (const k of ["mswing", "mevents", "prot", "phase", "ievents", "iswing"]) await p.click(`#t-1h [data-ov="${k}"]`);
  await p.waitForTimeout(300);
  res.allon = await state("1h");
  // подсказка на свече major BOS
  const ev = res["1h"].events.find((e) => e.type.startsWith("BOS"));
  await p.locator("#t-1h").scrollIntoViewIfNeeded(); await p.waitForTimeout(300);
  const c = await p.evaluate((time) => { const t = window.__lab["1h"], r = document.querySelector("#t-1h [data-f=chart]").getBoundingClientRect();
    t.chart.timeScale().setVisibleRange({ from: time - 40 * 3600, to: time + 20 * 3600 });
    return { r: [r.left, r.top], h: t.chart.panes()[0].getHeight() }; }, ev.ts);
  await p.waitForTimeout(300);
  const x = await p.evaluate((time) => window.__lab["1h"].chart.timeScale().timeToCoordinate(time), ev.ts);
  await p.mouse.move(c.r[0] + x, c.r[1] + c.h / 2); await p.waitForTimeout(300);
  const tip = await p.evaluate(() => { const t = document.querySelector("#t-1h [data-f=tip]"); return { shown: t.style.display, text: t.innerText }; });
  await p.click('#t-1h [data-tab="struct"]'); await p.waitForTimeout(300);
  const tab = await p.evaluate(() => { const el = document.querySelector('#t-1h [data-body="struct"]');
    return { tables: ["t-major-swings", "t-internal-swings", "t-major-events", "t-internal-events", "t-protected"].map((k) => !!el.querySelector(`[data-v3="${k}"]`)),
      panel: el.querySelector('[data-v3="panel"]')?.textContent || "" }; });
  if (process.argv[3]) await p.screenshot({ path: process.argv[3] });
  console.log(JSON.stringify({ res, ev, tip, tab, errs }));
  await b.close();
})().catch((e) => { console.log(JSON.stringify({ fatal: String(e) })); process.exit(1); });
