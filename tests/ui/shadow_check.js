// Тест 8: страница SHADOW LAB в Chromium — до активации (предпросмотр правил), START через очередь процесса SHADOW,
// позиция с планом на графике, CLOSE POSITION, журнал операций и отказов, мобильная ширина, без ошибок консоли.
const { chromium } = require("playwright");
(async () => {
  const url = process.argv[2], shots = process.argv[3] || "";
  const b = await chromium.launch(); const errs = [];
  const p = await b.newPage({ viewport: { width: 1600, height: 1100 }, deviceScaleFactor: shots ? 2 : 1 });
  p.on("pageerror", (e) => errs.push(e.message)); p.on("console", (m) => m.type() === "error" && errs.push(m.text()));
  p.on("dialog", (d) => d.accept());
  await p.goto(url + "shadow");
  await p.waitForFunction(() => window.__shadow && window.__shadow.data, null, { timeout: 30000 });
  await p.waitForTimeout(500);
  const snap = () => p.evaluate(() => {
    const d = window.__shadow.data, s = window.__shadow.S;
    return { status: d.status, candles: d.chart.candles.length, chartBars: s.candle.data().length, live: (d.live?.bars || []).length,
      markers: window.__shadow.markers().map((m) => m.text), priceLines: s.candle.priceLines().map((l) => l.options().title),
      kpis: document.getElementById("shKpis").innerText, decision: document.getElementById("shDecision").innerText,
      banner: document.getElementById("shBanner").innerText, ctl: [...document.querySelectorAll("#shCtl button")].map((x) => [x.innerText, x.disabled]),
      journal: document.getElementById("shJournal").innerText, position: document.getElementById("shPosition").innerText,
      costs: document.getElementById("shCosts").innerText, ready: document.getElementById("shReady").innerText,
      majorSwings: d.structure_v3.major.swings.filter((w) => w.tier === "major").length, trader: !!d.trader };
  });
  const pre = await snap();
  if (shots) await p.screenshot({ path: shots + "/shadow_lab_preview.png", fullPage: false });
  await p.click('#shCtl [data-cmd="start"]');
  await p.waitForFunction(() => window.__shadow.data.status === "RUNNING", null, { timeout: 30000 });
  await p.waitForFunction(() => window.__shadow.data.trader && window.__shadow.data.trader.position, null, { timeout: 90000 });
  await p.waitForTimeout(600);
  const run = await snap();
  if (shots) await p.screenshot({ path: shots + "/shadow_lab_position.png", fullPage: false });
  await p.click('#shCtl [data-cmd="close"]');
  await p.waitForFunction(() => window.__shadow.data.trader && !window.__shadow.data.trader.position && window.__shadow.data.trader.ops.length >= 2, null, { timeout: 60000 });
  await p.waitForTimeout(600);
  const closed = await snap();
  if (shots) {
    await p.locator("#shJournal").scrollIntoViewIfNeeded();
    await p.locator("section.card").first().screenshot({ path: shots + "/shadow_lab_journal.png" });
  }
  await p.goto(url + "performance");
  await p.waitForFunction(() => document.querySelector("#shBody table") || document.querySelector("#shBody .empty"), null, { timeout: 30000 });
  await p.waitForTimeout(800);
  const perf = await p.evaluate(() => ({ text: document.getElementById("shBody").innerText, legend: document.getElementById("shLegend").innerText,
    fw: document.getElementById("fwBody").innerText.slice(0, 200) }));
  if (shots) await p.locator("#shPerfChart").locator("xpath=ancestor::section").screenshot({ path: shots + "/performance_shadow.png" });
  const m = await b.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true });
  m.on("pageerror", (e) => errs.push("mobile: " + e.message));
  await m.goto(url + "shadow");
  await m.waitForFunction(() => window.__shadow && window.__shadow.data, null, { timeout: 30000 });
  await m.waitForTimeout(500);
  const mob = await m.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
  const nav = await p.evaluate(() => [...document.querySelectorAll(".nav a")].map((a) => a.innerText));
  console.log(JSON.stringify({ pre, run, closed, perf, mob, nav, errs }));
  await b.close();
})().catch((e) => { console.log(JSON.stringify({ fatal: String(e) })); process.exit(1); });
