/* Страница Performance: equity трёх портфелей, Buy & Hold и кеш на одном графике (одна ось — все стартуют с $1 000). */
"use strict";

(() => {
  const { fmt, esc, api, css } = Lab;
  const LW = window.LightweightCharts;
  const SER = [["1h", "Trader 1H", "--s1"], ["4h", "Trader 4H", "--s2"], ["1d", "Trader 1D", "--s3"], ["bh", "Buy & Hold", "--s4"]];
  let chart = null, fchart = null;
  const lines = {}, flines = {};

  function buildForward() {
    fchart = LW.createChart(document.getElementById("fwChart"), {
      autoSize: true,
      layout: { background: { type: LW.ColorType.Solid, color: css("--surface-1") }, textColor: css("--text-muted"), fontSize: 11, fontFamily: css("--mono") },
      grid: { vertLines: { color: "#151a21" }, horzLines: { color: "#151a21" } },
      rightPriceScale: { borderColor: css("--border") }, timeScale: { borderColor: css("--border"), timeVisible: true },
      localization: { locale: "ru-RU", priceFormatter: (p) => "$" + p.toFixed(2) },
    });
    SER.forEach(([k, name, c]) => (flines[k] = fchart.addSeries(LW.LineSeries, { color: css(c), lineWidth: 2, title: name, priceLineVisible: false })));
  }

  function renderForward(f) {
    document.getElementById("fwBadge").innerHTML = `<span class="badge ${Lab.STATUS_BADGE[f.status] || ""}">${Lab.STATUS_RU[f.status]}</span>`;
    document.getElementById("fwLegend").innerHTML = SER.map(([, n, c]) => `<span><i style="background:var(${c})"></i>${esc(n)}</span>`).join("");
    const body = document.getElementById("fwBody");
    if (f.status === "NOT_STARTED" || !f.table) {
      SER.forEach(([k]) => flines[k].setData([]));
      body.innerHTML = `<div class="empty">Forward paper trading не запущен. После START каждые 5 минут все три портфеля и Buy &amp; Hold оцениваются по одной проверенной котировке — здесь появятся кривые и метрики.</div>`;
      return;
    }
    SER.forEach(([k]) => flines[k].setData(dedupe(k === "bh" ? f.bh : f.curves[k] || [])));
    fchart.timeScale().fitContent();
    const T = f.table, bh = f.bh_summary;
    const rows = [
      ["Equity", (s) => fmt.usd(s.equity), bh ? fmt.usd(bh.equity) : "—"],
      ["Net PnL после издержек", (s) => `<span class="${fmt.cls(s.net_pnl)}">${fmt.sUsd(s.net_pnl)}</span>`, bh ? fmt.sUsd(bh.net_pnl) : "—"],
      ["Net Return", (s) => fmt.sPct(s.net_return_pct), bh ? fmt.sPct(bh.net_pnl / 10) : "—"],
      ["Издержки (fee + impact + slip + gas)", (s) => fmt.usd(s.costs_total), "вход + оценка по ликвидации"],
      ["Max Drawdown (по оценкам)", (s) => fmt.pct(s.max_drawdown_pct), bh ? fmt.pct(bh.max_drawdown_pct) : "—"],
      ["Win Rate", (s) => (s.win_rate == null ? "—" : fmt.pct(s.win_rate, 1)), "—"],
      ["Profit Factor", (s) => (s.profit_factor == null ? "—" : fmt.num(s.profit_factor, 2)), "—"],
      ["Закрытых сделок / операций", (s) => `${s.closed_trades} / ${s.operations}`, "0 / 1"],
      ["Время в FABLES", (s) => fmt.pct(s.time_in_fables_pct, 1), "100%"],
      ["Результат / Buy & Hold", (s) => (s.vs_bh_ratio == null ? "—" : fmt.num(s.vs_bh_ratio, 3) + "×"), "1×"],
      ["vs Buy & Hold, $", (s) => `<span class="${fmt.cls(s.vs_bh_usd)}">${fmt.sUsd(s.vs_bh_usd)}</span>`, "—"],
      ["Expectancy ($ / R)", (s) => `${fmt.sUsd(s.expectancy_usd)} / ${s.expectancy_r == null ? "—" : fmt.num(s.expectancy_r, 2) + "R"}`, "—"],
      ["Выборка", (s) => `<span class="badge warn">${esc(s.sample)}</span>`, "—"],
    ];
    body.innerHTML = `<div class="muted" style="margin-bottom:6px">Эксперимент ${esc(f.experiment)} · начат ${fmt.dt(f.started_at)} · оценок: ${f.marks}
      ${f.mark ? ` · последняя ${fmt.dt(f.mark.ts)} по ${fmt.price(f.mark.mid)} (${esc(f.mark.source)})` : ""}</div>
      <table class="t"><tr><th>Показатель</th><th>Trader 1H</th><th>Trader 4H</th><th>Trader 1D</th><th>Buy &amp; Hold</th></tr>
      ${rows.map(([n, fn, b]) => `<tr><td>${esc(n)}</td>${["1h", "4h", "1d"].map((k) => `<td class="n">${T[k] ? fn(T[k]) : "—"}</td>`).join("")}<td class="n">${b}</td></tr>`).join("")}</table>
      <div class="muted" style="margin-top:6px">Все значения — на одних и тех же моментах оценки с единой моделью исполнения; B&amp;H куплен по первой проверенной котировке после START.
      FORWARD и BACKTEST никогда не смешиваются.</div>`;
  }

  function build() {
    chart = LW.createChart(document.getElementById("perfChart"), {
      autoSize: true,
      layout: { background: { type: LW.ColorType.Solid, color: css("--surface-1") }, textColor: css("--text-muted"), fontSize: 11, fontFamily: css("--mono") },
      grid: { vertLines: { color: "#151a21" }, horzLines: { color: "#151a21" } },
      rightPriceScale: { borderColor: css("--border") }, timeScale: { borderColor: css("--border"), timeVisible: true },
      localization: { locale: "ru-RU", priceFormatter: (p) => "$" + p.toFixed(2) },
    });
    SER.forEach(([k, name, c]) => (lines[k] = chart.addSeries(LW.LineSeries, { color: css(c), lineWidth: 2, title: name, priceLineVisible: false })));
    lines.cash = chart.addSeries(LW.LineSeries, { color: css("--text-muted"), lineWidth: 1, lineStyle: LW.LineStyle.Dashed, title: "Кеш", priceLineVisible: false });
    chart.subscribeCrosshairMove((p) => {
      const tip = document.getElementById("perfTip");
      if (!p.time || !p.point) { tip.style.display = "none"; return; }
      const rows = [...SER.map(([k, n]) => [n, p.seriesData.get(lines[k])?.value]), ["Кеш", p.seriesData.get(lines.cash)?.value]]
        .filter(([, v]) => v != null).map(([n, v]) => `<tr><td>${esc(n)}</td><td>${fmt.usd(v)}</td></tr>`).join("");
      if (!rows) { tip.style.display = "none"; return; }
      tip.innerHTML = `<div class="t">${fmt.dt(p.time)}</div><table>${rows}</table>`;
      tip.style.display = "block";
      tip.style.left = Math.min(p.point.x + 16, document.getElementById("perfChart").clientWidth - 220) + "px";
      tip.style.top = "20px";
    });
  }

  const dedupe = (pts) => { const m = new Map(); pts.forEach((p) => m.set(p.time, p.value)); return [...m].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value })); };

  function render(p) {
    const b = p.backtest;
    SER.forEach(([k]) => lines[k].setData(dedupe(k === "bh" ? b.bh : b.curves[k])));
    lines.cash.setData(b.cash);
    chart.timeScale().fitContent();
    document.getElementById("perfLegend").innerHTML = [...SER.map(([, n, c]) => `<span><i style="background:var(${c})"></i>${esc(n)}</span>`),
      `<span><i style="background:var(--text-muted)"></i>Кеш ($1 000)</span>`].join("");
    document.getElementById("perfNote").textContent = `Эксперимент ${b.experiment}. Последняя точка каждой линии — синхронная оценка по ${fmt.price(b.mark.mid)} (${b.mark.source}, ${fmt.dt(b.mark.ts)}).`;
    document.getElementById("perfMark").textContent = `оценка @ ${fmt.dt(b.mark.ts)}`;
    const S = b.summary, bh = b.valuation.bh_per_trader;
    const rows = [
      ["Equity", (s) => fmt.usd(s.equity), fmt.usd(bh), fmt.usd(1000)],
      ["Net PnL", (s) => `<span class="${fmt.cls(s.net_pnl)}">${fmt.sUsd(s.net_pnl)}</span>`, `<span class="${fmt.cls(bh - 1000)}">${fmt.sUsd(bh - 1000)}</span>`, fmt.sUsd(0)],
      ["Net Return", (s) => fmt.sPct(s.net_return_pct), fmt.sPct((bh / 1000 - 1) * 100), fmt.sPct(0)],
      ["Gross Return", (s) => fmt.sPct(s.gross_return_pct), "—", "—"],
      ["vs Buy & Hold", (s) => `<span class="${fmt.cls(s.vs_bh_usd)}">${fmt.sUsd(s.vs_bh_usd)}</span>`, "—", `<span class="${fmt.cls(1000 - bh)}">${fmt.sUsd(1000 - bh)}</span>`],
      ["vs кеш", (s) => fmt.sUsd(s.vs_cash_usd), fmt.sUsd(bh - 1000), "—"],
      ["Realized / Unrealized", (s) => `${fmt.sUsd(s.realized_pnl)} / ${fmt.sUsd(s.unrealized_pnl)}`, "—", "—"],
      ["Издержки (fee + impact + slip + gas)", (s) => fmt.usd(s.costs_total), "вход + оценка по ликвидации", "—"],
      ["Завершённых сделок / операций", (s) => `${s.closed_trades} / ${s.operations}`, "0 / 1 (покупка)", "—"],
      ["Win Rate", (s) => (s.win_rate == null ? "—" : fmt.pct(s.win_rate, 1)), "—", "—"],
      ["Profit Factor", (s) => (s.profit_factor == null ? "—" : fmt.num(s.profit_factor, 2)), "—", "—"],
      ["Avg Win / Avg Loss", (s) => `${fmt.sUsd(s.avg_win)} / ${fmt.sUsd(s.avg_loss)}`, "—", "—"],
      ["Expectancy ($ / R)", (s) => `${fmt.sUsd(s.expectancy_usd)} / ${s.expectancy_r == null ? "—" : fmt.num(s.expectancy_r, 2) + "R"}`, "—", "—"],
      ["Max Drawdown", (s) => fmt.pct(s.max_drawdown_pct), "см. график", fmt.pct(0)],
      ["Макс. серия убытков", (s) => String(s.max_loss_streak), "—", "—"],
      ["Среднее удержание", (s) => (s.avg_holding_h == null ? "—" : fmt.num(s.avg_holding_h, 1) + " ч"), "весь период", "—"],
      ["Exposure / Turnover", (s) => `${fmt.pct(s.exposure_pct, 1)} / ${fmt.num(s.turnover_x, 2)}×`, "100%", "0%"],
      ["Slippage (сред. / мед., bps)", (s) => `${fmt.num(s.slippage_bps_mean, 1)} / ${fmt.num(s.slippage_bps_median, 1)}`, "—", "—"],
      ["Выборка", (s) => `<span class="badge warn">${s.sample}</span>`, "—", "—"],
    ];
    document.getElementById("perfTable").innerHTML = `<table class="t"><tr><th>Показатель</th><th>Trader 1H</th><th>Trader 4H</th><th>Trader 1D</th><th>Buy &amp; Hold</th><th>Кеш</th></tr>
      ${rows.map(([n, f, bhv, cash]) => `<tr><td>${esc(n)}</td>${["1h", "4h", "1d"].map((k) => `<td class="n">${f(S[k])}</td>`).join("")}<td class="n">${bhv}</td><td class="n">${cash}</td></tr>`).join("")}</table>
      <div class="muted" style="margin-top:6px">Buy &amp; Hold и кеш стартуют в тот же момент (${fmt.dt(b.start_ts)}) с тем же капиталом; B&amp;H платит ту же комиссию входа и оценивается по цене ликвидации (с комиссией продажи 1%).
      Результаты BACKTEST и FORWARD никогда не смешиваются.</div>`;
    renderForward(p.forward);
  }

  // ---------------------------------------------------------------- SHADOW: отдельный $1 000, в $3 000 не входит
  const SH = [["shadow", "SHADOW P2 4H", "--s-violet"], ["s2", "S2 (Trader 4H, норм. к $1 000)", "--s2"], ["bh", "Buy & Hold", "--s4"], ["cash", "Кеш", "--text-muted"]];
  let schart = null;
  const slines = {};
  function buildShadow() {
    schart = LW.createChart(document.getElementById("shPerfChart"), {
      autoSize: true,
      layout: { background: { type: LW.ColorType.Solid, color: css("--surface-1") }, textColor: css("--text-muted"), fontSize: 11, fontFamily: css("--mono") },
      grid: { vertLines: { color: "#151a21" }, horzLines: { color: "#151a21" } },
      rightPriceScale: { borderColor: css("--border") }, timeScale: { borderColor: css("--border"), timeVisible: true },
      localization: { locale: "ru-RU", priceFormatter: (p) => "$" + p.toFixed(2) },
    });
    SH.forEach(([k, name, c]) => (slines[k] = schart.addSeries(LW.LineSeries, { color: css(c), lineWidth: k === "cash" ? 1 : 2, title: name, priceLineVisible: false,
      lineStyle: k === "cash" ? LW.LineStyle.Dashed : LW.LineStyle.Solid })));
  }
  function renderShadow(s) {
    document.getElementById("shLegend").innerHTML = SH.map(([, n, c]) => `<span><i style="background:var(${c})"></i>${esc(n)}</span>`).join("");
    document.getElementById("shBadge").innerHTML = `<span class="badge ${Lab.STATUS_BADGE[s.status] || ""}">SHADOW: ${esc(Lab.STATUS_RU[s.status] || s.status)}</span>`;
    const body = document.getElementById("shBody");
    if (!s.experiment || !s.rows || !s.rows.length) {
      SH.forEach(([k]) => slines[k].setData([]));
      body.innerHTML = `<div class="empty">${esc(s.note || "SHADOW не активирован")}. Портфель SHADOW ($1 000) не входит в $3 000 основного FORWARD.</div>`;
      return;
    }
    SH.forEach(([k]) => slines[k].setData(dedupe(s.rows.filter((r) => r[k] != null).map((r) => ({ time: r.ts, value: r[k] })))));
    schart.timeScale().fitContent();
    const last = s.rows.at(-1), R = s.return_pct, t = s.table || {};
    body.innerHTML = `<table class="t"><tr><th>Показатель</th><th>SHADOW P2 4H</th><th>S2 (4H)</th><th>Buy &amp; Hold</th><th>Кеш</th></tr>
      <tr><td>Equity (на $1 000)</td>${["shadow", "s2", "bh", "cash"].map((k) => `<td class="n">${fmt.usd(last[k])}</td>`).join("")}</tr>
      <tr><td>Доходность с активации SHADOW</td>${["shadow", "s2", "bh", "cash"].map((k) => `<td class="n ${fmt.cls(R[k])}">${fmt.sPct(R[k])}</td>`).join("")}</tr>
      <tr><td>Сделок / Win Rate / PF</td><td class="n">${t.closed_trades ?? 0} / ${t.win_rate == null ? "—" : fmt.pct(t.win_rate, 0)} / ${t.profit_factor == null ? "—" : fmt.num(t.profit_factor, 2)}</td><td class="n">см. FORWARD</td><td class="n">—</td><td class="n">—</td></tr>
      <tr><td>Expectancy R / Max DD</td><td class="n">${t.expectancy_r == null ? "—" : fmt.num(t.expectancy_r, 2) + "R"} / ${fmt.pct(t.max_drawdown_pct)}</td><td class="n">—</td><td class="n">${s.bh_summary ? fmt.pct(s.bh_summary.max_drawdown_pct) : "—"}</td><td class="n">0%</td></tr></table>
      <div class="muted" style="margin-top:6px">${esc(s.strategy)} · эксперимент ${esc(s.experiment)} · с ${fmt.dt(s.since)} · оценок ${s.marks}. ${esc(s.note)}.
      Контрольные точки: 30 / 60 / 120 дней; 20 сделок — минимум для предварительного анализа, не доказательство.</div>`;
  }

  async function refresh() {
    const [o, p] = await Promise.all([api("/api/overview"), api("/api/performance")]);
    Lab.renderTopbar(o, o.forward.status === "NOT_STARTED" ? "BACKTEST" : "FORWARD", "performance");
    render(p);
    api("/api/shadow/performance").then(renderShadow).catch((e) => {      // сбой SHADOW не мешает основной странице
      document.getElementById("shBody").innerHTML = `<div class="empty">SHADOW недоступен: ${esc(e.message)}</div>`;
    });
  }
  build();
  buildForward();
  buildShadow();
  setInterval(() => refresh().catch(() => {}), 30000);
  refresh().catch((e) => { document.getElementById("perfNote").textContent = "Ошибка загрузки: " + e.message; });
  document.addEventListener("lab:refresh", () => refresh().catch(() => {}));
})();
