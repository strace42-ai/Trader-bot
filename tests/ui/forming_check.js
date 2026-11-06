// Тест 9: над формирующейся свечей 1H/4H/1D нет надписи FORMING и серого квадратного маркера, а сама свеча продолжает
// обновляться (OHLC и объём) по мере поступления котировок. Статус — в подсказке.
const { chromium } = require("playwright");
(async () => {
  const url = process.argv[2];
  const b = await chromium.launch(); const errs = [];
  const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
  p.on("pageerror", (e) => errs.push(e.message)); p.on("console", (m) => m.type() === "error" && errs.push(m.text()));
  await p.goto(url);
  await p.evaluate(() => localStorage.setItem("lab.mode", "FORWARD"));
  await p.reload();
  await p.waitForFunction(() => window.__lab && ["1h", "4h", "1d"].every((tf) => window.__lab[tf].data && window.__lab[tf].data.live), null, { timeout: 30000 });
  const snap = () => p.evaluate(() => Object.fromEntries(["1h", "4h", "1d"].map((tf) => {
    const t = window.__lab[tf], d = t.data, fb = d.live.bars.at(-1), last = t.s.candle.data().at(-1), vol = t.s.vol.data().at(-1);
    const mk = t.markers.markers().filter((m) => m.time === fb.time);
    return [tf, { time: last.time, fbTime: fb.time, status: fb.status, o: last.open, h: last.high, l: last.low, c: last.close, v: vol && vol.time === last.time ? vol.value : null,
      markers: mk.map((m) => [m.text, m.shape, m.color]), allSquares: t.markers.markers().filter((m) => m.shape === "square" && /FORMING|ждёт GT/.test(m.text || "")).length }];
  })));
  const first = await snap();
  await p.waitForTimeout(16000);                       // updateLive — каждые 5 с; бот в тесте двигает время и котировки
  const second = await snap();
  // подсказка на формирующейся свече 1H содержит статус
  await p.locator("#t-1h").scrollIntoViewIfNeeded(); await p.waitForTimeout(600);
  const c = await p.evaluate(() => { const t = window.__lab["1h"], el = document.querySelector("#t-1h [data-f=chart]"), r = el.getBoundingClientRect();
    return { x: r.left + t.chart.timeScale().timeToCoordinate(t.data.live.bars.at(-1).time), y: r.top + t.chart.panes()[0].getHeight() / 2 }; });
  await p.mouse.move(c.x - 30, c.y); await p.mouse.move(c.x, c.y); await p.waitForTimeout(300);
  const tipText = await p.evaluate(() => document.querySelector("#t-1h [data-f=tip]").innerText);
  await p.screenshot({ path: process.argv[3] || "/dev/null", clip: await p.evaluate(() => { const r = document.querySelector("#t-1h [data-f=chart]").getBoundingClientRect();
    return { x: r.left, y: r.top + window.scrollY, width: r.width, height: r.height }; }) }).catch(() => {});
  console.log(JSON.stringify({ first, second, tipText, errs }));
  await b.close();
})().catch((e) => { console.log(JSON.stringify({ fatal: String(e) })); process.exit(1); });
