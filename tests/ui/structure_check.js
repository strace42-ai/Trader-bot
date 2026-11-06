// UI-проверка структуры v2: отрезки от пробитого pivot до свечи пробоя, метки BOS/CHoCH и iBOS/iCHoCH, подсказка на метке.
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
    const t = window.__lab[tf], st = t.data.trader.analysis.structure, v2 = st.v2;
    return { ext: v2.external.map((e) => ({ type: e.type, ts: e.ts, level: e.level, pts: e.pivot.ts })),
      int: v2.internal.slice(-30).map((e) => ({ type: e.type, ts: e.ts, level: e.level, pts: e.pivot.ts })),
      segs: (t.structSegs || []).map((x) => x.data().map((q) => [q.time, q.value])),
      markers: t.markers.markers().map((m) => [m.time, m.text]),
      prot: t.s.candle.priceLines().map((l) => l.options().title).filter((x) => x.startsWith("Protected")), protected: v2.protected };
  }, tf);
  const res = {};
  for (const tf of ["1h", "4h", "1d"]) res[tf] = { off: await state(tf) };
  await p.click('#t-1h [data-ov="istruct"]'); await p.waitForTimeout(400);
  res["1h"].on = await state("1h");
  // подсказка на свече пробоя последнего внешнего события 1H (или внутреннего, если внешних нет)
  const ev = res["1h"].off.ext.at(-1) || res["1h"].on.int.at(-1);
  await p.locator("#t-1h").scrollIntoViewIfNeeded(); await p.waitForTimeout(400);
  const c = await p.evaluate((time) => { const t = window.__lab["1h"], r = document.querySelector("#t-1h [data-f=chart]").getBoundingClientRect();
    t.chart.timeScale().setVisibleRange({ from: time - 40 * 3600, to: time + 20 * 3600 });
    return { r: [r.left, r.top], h: t.chart.panes()[0].getHeight() }; }, ev.ts);
  await p.waitForTimeout(300);
  const x = await p.evaluate((time) => window.__lab["1h"].chart.timeScale().timeToCoordinate(time), ev.ts);
  await p.mouse.move(c.r[0] + x, c.r[1] + c.h / 2); await p.waitForTimeout(300);
  const tip = await p.evaluate(() => { const t = document.querySelector("#t-1h [data-f=tip]"); return { shown: t.style.display, text: t.innerText }; });
  if (process.argv[3]) await p.screenshot({ path: process.argv[3], clip: await p.evaluate(() => { const r = document.querySelector("#t-1h [data-f=chart]").getBoundingClientRect();
    return { x: r.left, y: r.top + window.scrollY, width: r.width, height: r.height }; }) }).catch(() => {});
  await p.click('#t-1h .tab[data-tab="struct"], #t-1h [data-tab="struct"]').catch(() => {});
  await p.waitForTimeout(300);
  const table = await p.evaluate(() => (document.querySelector('#t-1h [data-body="struct"]') || document.querySelector("#t-1h")).textContent.includes("События структуры"));
  const toggles = await p.evaluate(() => [...document.querySelectorAll("#t-1h [data-ov]")].map((x) => x.parentElement.textContent));
  console.log(JSON.stringify({ res, ev, tip, table, toggles, errs }));
  await b.close();
})().catch((e) => { console.log(JSON.stringify({ fatal: String(e) })); process.exit(1); });
