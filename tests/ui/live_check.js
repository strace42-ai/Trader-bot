// UI-проверка FORWARD/LIVE: формирующаяся свеча, отсутствие линии Live, подсказки по всем панелям, мобильный блок значений.
const { chromium } = require("playwright");
(async () => {
  const url = process.argv[2];
  const b = await chromium.launch(); const errs = [];
  const p = await b.newPage({ viewport: { width: 2560, height: 1440 } });
  p.on("pageerror", (e) => errs.push(e.message)); p.on("console", (m) => m.type() === "error" && errs.push(m.text()));
  await p.goto(url);
  await p.evaluate(() => { localStorage.setItem("lab.mode", "FORWARD"); localStorage.setItem("lab.rsi", "1"); });
  await p.reload();
  await p.waitForFunction(() => window.__lab && window.__lab["1h"].data && window.__lab["1h"].data.live, null, { timeout: 30000 });
  await p.locator("#t-1h").scrollIntoViewIfNeeded(); await p.waitForTimeout(600);
  const info = await p.evaluate(() => {
    const t = window.__lab["1h"], d = t.data, cd = t.s.candle.data(), last = cd.at(-1);
    const fb = d.live.bars.at(-1);
    return { lastTime: last.time, lastColor: last.color, formingTime: fb.time, formingStatus: fb.status, formingClose: fb.close, lastClose: last.close,
      markers: t.markers.markers().filter((m) => m.time === fb.time).map((m) => m.text),
      priceLines: t.s.candle.priceLines().map((l) => l.options().title),
      closedTime: d.candles.at(-1).time, rsiClosed: t.ind.rsi.get(d.candles.at(-1).time), liveRsi: d.live.values.live.rsi };
  });
  const coords = async (time, pane) => p.evaluate(([time, pane]) => {
    const t = window.__lab["1h"], el = document.querySelector("#t-1h [data-f=chart]"), r = el.getBoundingClientRect();
    let y = 0; const ps = t.chart.panes();
    for (let i = 0; i < pane; i++) y += ps[i].getHeight() + 1;
    return { x: r.left + t.chart.timeScale().timeToCoordinate(time), y: r.top + y + ps[pane].getHeight() / 2 };
  }, [time, pane]);
  let c = await coords(info.formingTime, 0); await p.mouse.move(c.x, c.y); await p.waitForTimeout(250);
  const tipForming = await p.evaluate(() => { const t = document.querySelector("#t-1h [data-f=tip]"); return { shown: t.style.display, text: t.innerText,
    on: [...t.querySelectorAll(".tsec.on .th")].map((x) => x.innerText) }; });
  const macdPane = await p.evaluate(() => window.__lab["1h"].panes.macd);
  c = await coords(info.closedTime, macdPane); await p.mouse.move(c.x, c.y); await p.waitForTimeout(250);
  const tipMacd = await p.evaluate(() => { const t = document.querySelector("#t-1h [data-f=tip]"); return { shown: t.style.display, text: t.innerText,
    on: [...t.querySelectorAll(".tsec.on .th")].map((x) => x.innerText) }; });
  await p.screenshot({ path: process.argv[3] || "/dev/null", clip: await p.evaluate(() => { const r = document.querySelector("#t-1h [data-f=chart]").getBoundingClientRect();
    return { x: r.left, y: r.top + window.scrollY, width: r.width, height: r.height }; }) }).catch(() => {});
  const m = await b.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true });
  m.on("pageerror", (e) => errs.push("mobile: " + e.message));
  await m.goto(url);
  await m.waitForFunction(() => window.__lab && window.__lab["1h"].data && window.__lab["1h"].data.live, null, { timeout: 30000 });
  await m.waitForTimeout(500);
  const mob = await m.evaluate(() => { const bi = document.querySelector("#t-1h [data-f=barinfo]"), t = document.querySelector("#t-1h [data-f=tip]");
    return { barinfo: getComputedStyle(bi).display, text: bi.innerText, tip: t.style.display, sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }; });
  console.log(JSON.stringify({ info, tipForming, tipMacd, mob, errs }));
  await b.close();
})().catch((e) => { console.log(JSON.stringify({ fatal: String(e) })); process.exit(1); });
