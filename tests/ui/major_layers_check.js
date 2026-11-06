// Major vs intermediate vs internal на FABLES 1H: точки и линии графика = API, слои независимы, масштабирование не ломает разметку.
const { chromium } = require("playwright");
(async () => {
  const url = process.argv[2];
  const b = await chromium.launch(); const errs = [];
  const p = await b.newPage({ viewport: { width: 2200, height: 1300 } });
  p.on("pageerror", (e) => errs.push(e.message)); p.on("console", (m) => m.type() === "error" && errs.push(m.text()));
  await p.goto(url);
  await p.evaluate(() => localStorage.setItem("lab.mode", "BACKTEST"));
  await p.reload();
  await p.waitForFunction(() => window.__lab && window.__lab["1h"].data, null, { timeout: 30000 });
  const snap = () => p.evaluate(() => {
    const t = window.__lab["1h"], v = t.data.structure_v3;
    const ser = (x) => (x ? x.data().map((q) => [q.time, q.value]) : null);
    return { api: v.major.swings.map((w) => ({ ts: w.ts, price: w.price, tier: w.tier, label: w.major_label, avail: w.available_ts, conf: w.confirmed_ts, roles: w.roles })),
      apiInt: v.internal.swings.map((w) => [w.ts, w.price]),
      major: ser(t.v3series.major), inter: ser(t.v3series.inter), internal: ser(t.v3series.internal),
      markers: t.markers.markers().map((m) => [m.time, m.text]), lines: t.v3lines,
      toggles: Object.fromEntries([...document.querySelectorAll("#t-1h [data-ov]")].map((x) => [x.dataset.ov, x.checked])) };
  });
  const click = async (k) => { await p.click(`#t-1h [data-ov="${k}"]`); await p.waitForTimeout(250); };
  const out = { def: await snap() };
  await click("iswing_mid"); out.mid = await snap();
  await click("iswing"); await click("ievents"); out.allint = await snap();
  await click("iswing_mid"); out.intOnly = await snap();
  await click("iswing"); await click("ievents"); out.back = await snap();
  // масштабирование: узкий диапазон и обратно
  await p.evaluate(() => window.__lab["1h"].chart.timeScale().setVisibleRange({ from: 1792540000, to: 1792900000 }));
  await p.waitForTimeout(300); out.zoom = await snap();
  await p.evaluate(() => window.__lab["1h"].chart.timeScale().fitContent());
  // подсказка на major-точке 0.07975 (LH)
  const lh = out.def.api.find((w) => w.tier === "major" && w.label === "LH");
  await p.locator("#t-1h").scrollIntoViewIfNeeded(); await p.waitForTimeout(200);
  const c = await p.evaluate((time) => { const t = window.__lab["1h"], r = document.querySelector("#t-1h [data-f=chart]").getBoundingClientRect();
    t.chart.timeScale().setVisibleRange({ from: time - 30 * 3600, to: time + 30 * 3600 });
    return { r: [r.left, r.top], h: t.chart.panes()[0].getHeight() }; }, lh.ts);
  await p.waitForTimeout(300);
  const x = await p.evaluate((time) => window.__lab["1h"].chart.timeScale().timeToCoordinate(time), lh.ts);
  await p.mouse.move(c.r[0] + x, c.r[1] + c.h / 3); await p.waitForTimeout(300);
  out.tip = await p.evaluate(() => document.querySelector("#t-1h [data-f=tip]").innerText);
  await p.evaluate(() => window.__lab["1h"].chart.timeScale().fitContent()); await p.waitForTimeout(300);
  if (process.argv[3]) {
    const r = await p.evaluate(() => { const q = document.querySelector("#t-1h [data-f=chart]").getBoundingClientRect(); return { x: q.x, y: q.y + window.scrollY, width: q.width, height: Math.min(q.height, 520) }; });
    await p.screenshot({ path: process.argv[3], clip: r, fullPage: true });
  }
  out.errs = errs;
  console.log(JSON.stringify(out));
  await b.close();
})().catch((e) => { console.log(JSON.stringify({ fatal: String(e) })); process.exit(1); });
