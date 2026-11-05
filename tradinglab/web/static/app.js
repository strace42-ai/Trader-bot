/* Три торговых терминала (1H/4H/1D). Все данные — из /api/terminal/{tf}: свечи, индикаторы, операции и решения ядра. */
"use strict";

(() => {
  const { fmt, esc, api, css, store } = Lab;
  const LW = window.LightweightCharts;
  const TFS = ["1h", "4h", "1d"];
  const TITLES = { "1h": "TRADER 1H", "4h": "TRADER 4H", "1d": "TRADER 1D" };
  const TABS = [["overview", "Обзор"], ["ind", "Индикаторы"], ["struct", "Структура"], ["onchain", "Ончейн"], ["dec", "Решения"], ["diag", "Диагностика"]];
  const PANES = { MACD_HIST: 1, MFI: 2, CMF: 3, OBV: 4 };
  let mode = store.get("lab.mode", "BACKTEST");
  let overview = null;
  const T = {}; // состояние терминалов

  // ------------------------------------------------------------------ каркас
  function skeleton(tf) {
    const sec = document.createElement("section");
    sec.className = "terminal";
    sec.id = `t-${tf}`;
    sec.innerHTML = `
      <div class="term-head"><h2>${TITLES[tf]}</h2><span class="strat" data-f="strat"></span><span data-f="state"></span>
        <span data-f="sample"></span><span class="spacer"></span><span class="muted" data-f="asof"></span></div>
      <div class="metrics" data-f="metrics"></div>
      <div class="term-body">
        <div class="chart-col">
          <div class="chart-tools" data-f="tools">
            ${[["ema", "EMA", "--s1"], ["bb", "Bollinger", "--text-muted"], ["vol", "Объём", "--text-muted"], ["levels", "Уровни S/R", "--text-secondary"],
               ["fib", "Fibonacci", "--s4"], ["struct", "Структура", "--s-violet"], ["div", "Дивергенции", "--up"], ["trades", "Сделки", "--text-primary"]]
              .map(([k, n, c]) => `<label><input type="checkbox" data-ov="${k}" ${k === "fib" ? "" : "checked"}><span class="legend-dot" style="background:var(${c})"></span>${n}</label>`).join("")}
            <span class="spacer"></span><span class="muted">UTC · панели: MACD · MFI · CMF · OBV</span>
          </div>
          <div class="chart" data-f="chart"><div class="legend" data-f="legend"></div><div class="tip" data-f="tip"></div></div>
        </div>
        <div class="side">
          <div class="panel-h"><span>Позиция</span><span data-f="posbadge"></span></div>
          <div class="pos-card" data-f="pos"></div>
          <div class="panel-h"><span>Журнал операций</span><span class="muted" data-f="opcount"></span></div>
          <div class="filters">
            <select data-flt="side"><option value="">Все</option><option value="BUY">BUY</option><option value="SELL">SELL</option></select>
            <select data-flt="res"><option value="">Любой результат</option><option value="win">Прибыль</option><option value="loss">Убыток</option></select>
            <select data-flt="per"><option value="">Весь период</option><option value="86400">24 ч</option><option value="604800">7 дней</option></select>
          </div>
          <div class="list" data-f="ops"></div>
          <div class="panel-h"><span>Завершённые сделки</span><span class="muted" data-f="trcount"></span></div>
          <div class="list" data-f="trades" style="max-height:200px"></div>
        </div>
      </div>
      <div class="analysis">
        <div class="tabs">${TABS.map(([k, n], i) => `<button data-tab="${k}" class="${i === 0 ? "on" : ""}">${n}</button>`).join("")}</div>
        ${TABS.map(([k], i) => `<div class="tab-body ${i === 0 ? "on" : ""}" data-body="${k}"></div>`).join("")}
      </div>`;
    sec.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => {
      sec.querySelectorAll("[data-tab]").forEach((x) => x.classList.toggle("on", x === b));
      sec.querySelectorAll("[data-body]").forEach((x) => x.classList.toggle("on", x.dataset.body === b.dataset.tab));
    }));
    sec.querySelectorAll("[data-flt]").forEach((s) => s.addEventListener("change", () => renderOps(tf)));
    sec.querySelectorAll("[data-ov]").forEach((c) => c.addEventListener("change", () => applyOverlays(tf)));
    return sec;
  }
  const $ = (tf, f) => document.querySelector(`#t-${tf} [data-f="${f}"]`);

  // ------------------------------------------------------------------ график
  function buildChart(tf) {
    const el = $(tf, "chart");
    const chart = LW.createChart(el, {
      autoSize: true,
      layout: { background: { type: LW.ColorType.Solid, color: css("--surface-1") }, textColor: css("--text-muted"), fontSize: 11,
                fontFamily: css("--mono"), panes: { separatorColor: css("--border"), separatorHoverColor: css("--border-strong"), enableResize: true } },
      grid: { vertLines: { color: "#151a21" }, horzLines: { color: "#151a21" } },
      rightPriceScale: { borderColor: css("--border"), minimumWidth: 72 },
      timeScale: { borderColor: css("--border"), timeVisible: true, secondsVisible: false, rightOffset: 4 },
      crosshair: { mode: LW.CrosshairMode.Normal },
      localization: { locale: "ru-RU" },
    });
    const pf = { type: "price", precision: 5, minMove: 0.00001 };
    const line = (color, pane, extra = {}) => chart.addSeries(LW.LineSeries, { color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
                                                                               crosshairMarkerVisible: false, ...extra }, pane);
    const s = {};
    s.candle = chart.addSeries(LW.CandlestickSeries, { upColor: css("--up"), downColor: css("--down"), borderVisible: false,
      wickUpColor: css("--up"), wickDownColor: css("--down"), priceFormat: pf }, 0);
    s.vol = chart.addSeries(LW.HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false }, 0);
    s.vol.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    s.emaF = line(css("--s1"), 0, { priceFormat: pf, title: "" });
    s.emaS = line(css("--s2"), 0, { priceFormat: pf });
    const bbOpt = { lineWidth: 1, lineStyle: LW.LineStyle.Dashed, priceFormat: pf };
    s.bbU = line(css("--text-muted"), 0, bbOpt);
    s.bbM = line("#3a434e", 0, { ...bbOpt, lineStyle: LW.LineStyle.Dotted });
    s.bbL = line(css("--text-muted"), 0, bbOpt);
    s.hist = chart.addSeries(LW.HistogramSeries, { priceLineVisible: false, lastValueVisible: false, priceFormat: { type: "price", precision: 6, minMove: 0.000001 } }, 1);
    s.macd = line(css("--s1"), 1, { lineWidth: 1, lastValueVisible: true, title: "MACD", priceFormat: { type: "price", precision: 6, minMove: 0.000001 } });
    s.sig = line(css("--s2"), 1, { lineWidth: 1, priceFormat: { type: "price", precision: 6, minMove: 0.000001 } });
    s.mfi = line(css("--s-violet"), 2, { lineWidth: 1, lastValueVisible: true, title: "MFI", priceFormat: { type: "price", precision: 1, minMove: 0.1 } });
    s.cmf = chart.addSeries(LW.HistogramSeries, { priceLineVisible: false, lastValueVisible: true, title: "CMF", priceFormat: { type: "price", precision: 3, minMove: 0.001 } }, 3);
    s.obv = line(css("--s1"), 4, { lineWidth: 1, lastValueVisible: true, title: "OBV", priceFormat: { type: "volume" } });
    const panes = chart.panes();
    [340, 80, 70, 70, 70].forEach((h, i) => panes[i] && panes[i].setHeight(h));
    T[tf].chart = chart; T[tf].s = s; T[tf].lines = []; T[tf].extra = [];
    T[tf].markers = LW.createSeriesMarkers(s.candle, []);
    chart.subscribeCrosshairMove((p) => onCrosshair(tf, p));
  }

  function setData(tf, d) {
    const { s } = T[tf];
    const S = d.series;
    s.candle.setData(d.candles.map((k) => ({ time: k.time, open: k.open, high: k.high, low: k.low, close: k.close })));
    s.vol.setData(d.candles.map((k) => ({ time: k.time, value: k.volume, color: k.close >= k.open ? "rgba(25,158,112,.35)" : "rgba(230,103,103,.35)" })));
    s.emaF.setData(S.ema_fast); s.emaS.setData(S.ema_slow);
    s.bbU.setData(S.bb_upper); s.bbM.setData(S.bb_mid); s.bbL.setData(S.bb_lower);
    s.hist.setData(S.macd_hist.map((p) => ({ ...p, color: p.value >= 0 ? "rgba(25,158,112,.75)" : "rgba(230,103,103,.75)" })));
    s.macd.setData(S.macd); s.sig.setData(S.macd_signal); s.mfi.setData(S.mfi);
    s.cmf.setData(S.cmf.map((p) => ({ ...p, color: p.value >= 0 ? "rgba(25,158,112,.75)" : "rgba(230,103,103,.75)" })));
    s.obv.setData(S.obv);
    if (!T[tf].guides) {
      s.mfi.createPriceLine({ price: 80, color: "#3a434e", lineWidth: 1, lineStyle: LW.LineStyle.Dashed, axisLabelVisible: false });
      s.mfi.createPriceLine({ price: 20, color: "#3a434e", lineWidth: 1, lineStyle: LW.LineStyle.Dashed, axisLabelVisible: false });
      s.cmf.createPriceLine({ price: 0, color: "#3a434e", lineWidth: 1, axisLabelVisible: false });
      T[tf].guides = true;
    }
    T[tf].byTime = new Map(d.candles.map((k) => [k.time, k]));
    T[tf].ind = Object.fromEntries(Object.entries(S).map(([n, arr]) => [n, new Map(arr.map((p) => [p.time, p.value]))]));
    // по умолчанию — период с начала торговли (день запуска с пиком первого часа только история; прокрутка доступна)
    const start = overview?.backtest?.start_ts;
    const from = start ? d.candles.findIndex((k) => k.time >= start) : 0;
    if (from > 0) T[tf].chart.timeScale().setVisibleLogicalRange({ from, to: d.candles.length + 3 });
    else T[tf].chart.timeScale().fitContent();
  }

  function applyOverlays(tf) {
    const t = T[tf], d = t.data, s = t.s;
    if (!d) return;
    const on = (k) => document.querySelector(`#t-${tf} [data-ov="${k}"]`).checked;
    s.emaF.applyOptions({ visible: on("ema") }); s.emaS.applyOptions({ visible: on("ema") });
    [s.bbU, s.bbM, s.bbL].forEach((x) => x.applyOptions({ visible: on("bb") }));
    s.vol.applyOptions({ visible: on("vol") });
    t.lines.forEach((l) => s.candle.removePriceLine(l)); t.lines = [];
    t.extra.forEach((x) => t.chart.removeSeries(x)); t.extra = [];
    const tr = d.trader, st = tr?.analysis?.structure || {};
    const pl = (price, color, title, style = LW.LineStyle.Dashed, w = 1) =>
      t.lines.push(s.candle.createPriceLine({ price, color, lineWidth: w, lineStyle: style, axisLabelVisible: true, title }));
    const lastC = d.candles.at(-1)?.close;
    if (on("levels") && lastC) (st.levels || []).filter((l) => l.touches >= 3 && Math.abs(Math.log(l.price / lastC)) < 0.25)
      .sort((a, b) => Math.abs(a.price - lastC) - Math.abs(b.price - lastC)).slice(0, 6)
      .forEach((l) => pl(l.price, "#56606c", `S/R×${l.touches}`, LW.LineStyle.Dotted));
    if (on("fib") && st.fib) {
      ["0.382", "0.5", "0.618"].forEach((r) => pl(st.fib.retracement[r], css("--s4"), `Fib ${r}`, LW.LineStyle.Dotted));
      pl(st.fib.extension["1.618"], css("--s4"), "Ext 1.618", LW.LineStyle.Dotted);
    }
    const pos = tr?.position;
    if (pos) {
      pl(pos.entry_mid, css("--text-primary"), "Вход", LW.LineStyle.Solid);
      pl(pos.stop, css("--down"), "Стоп", LW.LineStyle.Solid, 2);
      if (pos.t1 && !pos.t1_done) pl(pos.t1, css("--up"), "Цель 1", LW.LineStyle.Solid);
    }
    if (on("div")) (st.divergences || []).slice(-8).forEach((dv) => {
      const bull = dv.type.includes("bullish");
      const color = bull ? css("--up") : css("--down");
      const mk = (pane, a, b) => {
        const x = t.chart.addSeries(LW.LineSeries, { color, lineWidth: 1, lineStyle: dv.type.startsWith("hidden") ? LW.LineStyle.Dotted : LW.LineStyle.Solid,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, priceFormat: { type: "price", precision: 6, minMove: 0.000001 } }, pane);
        x.setData([{ time: dv.p1.ts, value: a }, { time: dv.p2.ts, value: b }]);
        t.extra.push(x);
      };
      mk(0, dv.p1.price, dv.p2.price);
      if (PANES[dv.indicator] != null) mk(PANES[dv.indicator], dv.p1.ind, dv.p2.ind);
    });
    const marks = [];
    if (on("trades")) (tr?.ops || []).forEach((o) => marks.push(o.side === "BUY"
      ? { time: o.ts, position: "belowBar", color: css("--up"), shape: "arrowUp", text: "BUY" }
      : { time: o.ts, position: "aboveBar", color: css("--down"), shape: "arrowDown", text: o.reason.startsWith("цель") ? "T1" : o.reason.startsWith("стоп") ? "STOP" : "SELL" }));
    if (on("struct")) (st.events || []).slice(-12).forEach((e) => marks.push({ time: e.ts, position: e.type.endsWith("UP") ? "belowBar" : "aboveBar",
      color: css("--s-violet"), shape: "circle", size: 0.6, text: e.type.replace("_UP", "↑").replace("_DOWN", "↓") }));
    marks.sort((a, b) => a.time - b.time);
    t.markers.setMarkers(marks);
  }

  function onCrosshair(tf, p) {
    const t = T[tf], leg = $(tf, "legend"), tip = $(tf, "tip");
    const tm = p?.time;
    const k = tm != null ? t.byTime?.get(tm) : null;
    const last = t.data?.candles?.[t.data.candles.length - 1];
    const c = k || last;
    if (c) {
      const g = (n) => t.ind?.[n]?.get(c.time);
      leg.innerHTML = `${fmt.time(c.time)}  O ${fmt.price(c.open)}  H ${fmt.price(c.high)}  L ${fmt.price(c.low)}  C <span class="${c.close >= c.open ? "pos" : "neg"}">${fmt.price(c.close)}</span>
        V ${fmt.big(c.volume)}  ·  <span style="color:var(--s1)">EMA ${fmt.price(g("ema_fast"))}</span> <span style="color:var(--s2)">${fmt.price(g("ema_slow"))}</span>
        · RSI ${fmt.num(g("rsi"), 1)} · MFI ${fmt.num(g("mfi"), 1)} · CMF ${fmt.num(g("cmf"), 3)}`;
    }
    const ops = tm != null ? (t.data?.trader?.ops || []).filter((o) => o.ts === tm) : [];
    if (!ops.length || !p.point) { tip.style.display = "none"; return; }
    tip.innerHTML = ops.map((o) => `<div class="t"><span class="side-b ${o.side === "BUY" ? "buy" : "sell"}">${o.side}</span> · ${fmt.dt(o.ts)}</div>
      <table>
        <tr><td>Цена исп.</td><td>${fmt.price(Number(o.price))} (mid ${fmt.price(Number(o.mid))})</td></tr>
        <tr><td>Количество</td><td>${fmt.qty(Number(o.qty))} FABLES</td></tr>
        <tr><td>Сумма</td><td>${fmt.usd(Number(o.usd))}</td></tr>
        <tr><td>Издержки</td><td>fee ${fmt.usd(Number(o.fee_usd), 3)} · impact ${fmt.usd(Number(o.impact_usd), 3)} · slip ${fmt.usd(Number(o.slippage_usd), 3)} · gas ${fmt.usd(Number(o.gas_usd), 3)}</td></tr>
        ${o.realized_pnl != null ? `<tr><td>PnL</td><td class="${fmt.cls(Number(o.realized_pnl))}">${fmt.sUsd(Number(o.realized_pnl))}</td></tr>` : ""}
        <tr><td>Причина</td><td>${esc(o.reason)}</td></tr>
        <tr><td>Позиция</td><td>${esc(o.position_id)}</td></tr>
      </table>`).join("<hr style='border-color:var(--border)'>");
    const box = $(tf, "chart").getBoundingClientRect();
    tip.style.display = "block";
    tip.style.left = Math.min(p.point.x + 16, box.width - 350) + "px";
    tip.style.top = Math.max(p.point.y - 20, 30) + "px";
  }

  // ------------------------------------------------------------------ метрики, позиция, журнал
  function metric(k, v, s = "", cls = "") {
    return `<div class="metric"><div class="k" title="${esc(k)}">${esc(k)}</div><div class="v ${cls}">${v}</div><div class="s">${s || "&nbsp;"}</div></div>`;
  }

  function renderHead(tf) {
    const d = T[tf].data, tr = d.trader;
    if (!tr) {
      $(tf, "strat").textContent = "";
      $(tf, "state").innerHTML = `<span class="badge">FORWARD: ${Lab.STATUS_RU[d.forward.status]}</span>`;
      $(tf, "sample").innerHTML = "";
      $(tf, "asof").textContent = `рыночные данные до ${fmt.dt(d.candles.at(-1)?.time + ({ "1h": 3600, "4h": 14400, "1d": 86400 })[tf])}`;
      $(tf, "metrics").innerHTML = metric("Режим", "FORWARD", "") + metric("Статус", Lab.STATUS_RU[d.forward.status]) +
        `<div class="metric" style="grid-column: span 11"><div class="k">Пояснение</div><div class="s" style="white-space:normal">${
          d.forward.status === "NOT_STARTED" ? "Forward paper trading не запущен. Нажмите START, чтобы создать эксперимент; сделки появятся только после подключения живых данных." :
          esc(d.forward.feed.reason)}</div></div>`;
      return;
    }
    const sm = tr.summary;
    $(tf, "strat").textContent = `${tr.strategy.id} v${tr.strategy.version} · ${tr.mode}`;
    const stc = { LONG: "good", ENTRY_PENDING: "info", EXIT_PENDING: "warn", RISK_OFF: "crit", DATA_ERROR: "crit", PAUSED: "warn", WARMUP: "warn" }[tr.state] || "";
    $(tf, "state").innerHTML = `<span class="badge ${stc}">${esc(tr.state)}</span>`;
    $(tf, "sample").innerHTML = `<span class="badge warn" title="Нужно ≥ 30 завершённых сделок для статистических выводов">${esc(sm.sample)}</span>
      <span class="badge">${esc(sm.validation)}</span>`;
    $(tf, "asof").textContent = sm.mark ? `оценка по ${fmt.price(sm.mark.mid)} @ ${fmt.time(sm.mark.ts)} · ${sm.cost_model}`
      : `${sm.mark_note || "котировки для оценки нет"} · ${sm.cost_model}`;
    const pf = sm.profit_factor == null ? "—" : fmt.num(sm.profit_factor, 2);
    $(tf, "metrics").innerHTML = [
      metric("Equity", fmt.usd(sm.equity), `старт ${fmt.usd(sm.initial, 0)}`),
      metric("Cash", fmt.usd(sm.cash)),
      metric("FABLES", fmt.qty(sm.fables_qty), fmt.usd(sm.fables_value)),
      metric("Net PnL", fmt.sUsd(sm.net_pnl), fmt.sPct(sm.net_return_pct), fmt.cls(sm.net_pnl)),
      metric("Realized", fmt.sUsd(sm.realized_pnl), "", fmt.cls(sm.realized_pnl)),
      metric("Unrealized", fmt.sUsd(sm.unrealized_pnl), "", fmt.cls(sm.unrealized_pnl)),
      metric("Fees / издержки", fmt.usd(sm.fees), `всего ${fmt.usd(sm.costs_total)}`),
      metric("Сделок", String(sm.closed_trades), `операций ${sm.operations}`),
      metric("Открыто", String(sm.open_positions)),
      metric("Win Rate", sm.win_rate == null ? "—" : fmt.pct(sm.win_rate, 1)),
      metric("Profit Factor", pf),
      metric("Max DD", fmt.pct(sm.max_drawdown_pct, 2)),
      metric("vs Buy & Hold", fmt.sUsd(sm.vs_bh_usd), sm.bh_equity_same_time != null ? `B&H ${fmt.usd(sm.bh_equity_same_time)}` : "", fmt.cls(sm.vs_bh_usd)),
    ].join("");
  }

  function renderPosition(tf) {
    const tr = T[tf].data.trader, el = $(tf, "pos");
    if (!tr) { el.innerHTML = `<div class="empty">FORWARD: ${Lab.STATUS_RU[T[tf].data.forward.status]} — позиций нет.</div>`; $(tf, "posbadge").innerHTML = ""; return; }
    const p = tr.position, pend = tr.pending;
    $(tf, "posbadge").innerHTML = p ? `<span class="badge good">LONG</span>` : `<span class="badge">FLAT</span>`;
    let h = "";
    if (p) {
      h += `<div class="kv">
        <span class="k">Вход</span><span class="v">${fmt.price(p.entry_mid)} · ${fmt.time(p.entry_ts)}</span>
        <span class="k">Количество</span><span class="v">${fmt.qty(p.qty)} / ${fmt.qty(p.qty_initial)}</span>
        <span class="k">Стоп</span><span class="v neg">${fmt.price(p.stop)} (исх. ${fmt.price(p.initial_stop)})</span>
        <span class="k">Цель 1</span><span class="v">${p.t1 ? fmt.price(p.t1) + (p.t1_done ? " ✓" : "") : "—"}</span>
        <span class="k">Trailing</span><span class="v">${fmt.num(p.trail_atr, 1)} ATR</span>
        <span class="k">Time stop</span><span class="v">${p.time_stop_bars} баров</span>
        <span class="k">Риск</span><span class="v">${fmt.usd(p.risk_usd)}</span>
        <span class="k">К входу</span><span class="v ${fmt.cls(p.unrealized_vs_entry_pct)}">${fmt.sPct(p.unrealized_vs_entry_pct)}</span></div>`;
    } else {
      h += `<div class="sec">Нет открытой позиции.</div>`;
    }
    if (pend) h += `<div class="warnline" style="margin-top:6px">Отложенный ордер: ${esc(pend.kind)} — исполнится по следующей доступной цене. ${esc(pend.reason || "")}</div>`;
    el.innerHTML = h;
  }

  function renderOps(tf) {
    const d = T[tf].data, tr = d.trader;
    const el = $(tf, "ops"), tel = $(tf, "trades");
    if (!tr) { el.innerHTML = `<div class="empty">FORWARD не запущен — операций нет.</div>`; tel.innerHTML = ""; $(tf, "opcount").textContent = ""; $(tf, "trcount").textContent = ""; return; }
    const sec = document.querySelector(`#t-${tf}`);
    const side = sec.querySelector('[data-flt="side"]').value, res = sec.querySelector('[data-flt="res"]').value,
      per = Number(sec.querySelector('[data-flt="per"]').value || 0);
    const markTs = tr.summary.mark?.ts || Date.now() / 1000;
    const tradePnl = Object.fromEntries(tr.trades.map((x) => [x.trade_id, Number(x.net_pnl)]));
    const ops = tr.ops.filter((o) => (!side || o.side === side) && (!per || o.ts >= markTs - per) &&
      (!res || (tradePnl[o.position_id] != null && (res === "win" ? tradePnl[o.position_id] > 0 : tradePnl[o.position_id] <= 0)))).slice().reverse();
    $(tf, "opcount").textContent = `${ops.length} из ${tr.ops.length}`;
    el.innerHTML = ops.length ? ops.map((o) => `<div class="op">
        <div class="l1"><span class="side-b ${o.side === "BUY" ? "buy" : "sell"}">${o.side}</span> · ${fmt.price(Number(o.price))} · ${fmt.qty(Number(o.qty))} FABLES · ${fmt.usd(Number(o.usd))}</div>
        <div class="l2">Причина: ${esc(o.reason)}</div>
        <div class="l3">${fmt.dt(o.ts)} · издержки ${fmt.usd(Number(o.fee_usd) + Number(o.impact_usd) + Number(o.slippage_usd) + Number(o.gas_usd), 3)}
          ${o.realized_pnl != null ? ` · PnL <span class="${fmt.cls(Number(o.realized_pnl))}">${fmt.sUsd(Number(o.realized_pnl))}</span>` : ""} · ${esc(o.position_id)}</div></div>`).join("")
      : `<div class="empty">${tr.ops.length ? "Нет операций под выбранные фильтры." : !tr.decisions.length ?
          "Решений ещё нет: после START не закрылось ни одной новой свечи (живые данные не подключены)." :
          "Операций нет: стратегия не нашла входа, удовлетворяющего всем условиям и издержкам. Причины — во вкладке «Решения»."}</div>`;
    $(tf, "trcount").textContent = `${tr.trades.length}`;
    tel.innerHTML = tr.trades.length ? tr.trades.slice().reverse().map((x) => `<div class="op">
        <div class="l1">${fmt.time(x.entry_ts)} → ${fmt.time(x.exit_ts)} · <span class="${fmt.cls(Number(x.net_pnl))}">${fmt.sUsd(Number(x.net_pnl))}</span>
          · ${x.r_multiple == null ? "" : fmt.num(x.r_multiple, 2) + "R"}</div>
        <div class="l3">${fmt.price(Number(x.entry_price))} → ${fmt.price(Number(x.exit_price))} · MFE ${fmt.sPct(x.mfe_pct, 1)} · MAE ${fmt.sPct(x.mae_pct, 1)} · ${x.bars_held} баров</div>
        <div class="l2">${esc(x.exit_reason)}</div></div>`).join("") : `<div class="empty">Завершённых сделок нет.</div>`;
  }

  // ------------------------------------------------------------------ анализ
  function conds(list) {
    return `<ul class="conds">${(list || []).map((c) => {
      const k = c.ok === true ? "ok" : c.ok === false ? "bad" : "na";
      return `<li class="${k}"><span class="ic">${k === "ok" ? "✓" : k === "bad" ? "✗" : "—"}</span><span>${esc(c.name)}<br><span class="d">${esc(c.detail)}</span></span></li>`;
    }).join("")}</ul>`;
  }

  function renderAnalysis(tf) {
    const d = T[tf].data, tr = d.trader, sec = document.querySelector(`#t-${tf}`);
    const body = (k) => sec.querySelector(`[data-body="${k}"]`);
    if (!tr) {
      const msg = `<div class="empty">FORWARD: ${Lab.STATUS_RU[d.forward.status]}. Анализ и решения появятся после запуска и поступления новых закрытых свечей. ${esc(d.forward.feed.reason)}.</div>`;
      TABS.forEach(([k]) => (body(k).innerHTML = msg));
      body("onchain").innerHTML = onchain();
      return;
    }
    const dec = tr.decision, an = tr.analysis, st = an.structure || {};
    if (!dec) {
      const msg = `<div class="empty">${esc(tr.mode)}: решений ещё нет — после начала эксперимента не закрылось ни одной новой свечи.
        ${tr.mode === "FORWARD" ? "Живые данные не подключены, поэтому новые свечи не поступают." : ""}
        Рыночные индикаторы ниже — на последней закрытой свече записанного набора.</div>`;
      TABS.forEach(([k]) => (body(k).innerHTML = msg));
      body("ind").innerHTML = msg + `<table class="t"><tr><th>Индикатор</th><th>Значение</th><th>Статус</th><th>Интерпретация</th></tr>
        ${an.indicators.map((r) => `<tr><td>${esc(r.name)}</td><td class="n">${esc(r.display)}</td><td>${esc(r.status)}</td><td class="sec">${esc(r.text)}</td></tr>`).join("")}</table>`;
      body("onchain").innerHTML = onchain();
      return;
    }
    const reg = st.regime || {};
    const act = dec?.action_final || "—";
    const actCls = { ENTER: "pos", EXIT: "neg", HOLD: "", WAIT: "sec", WARMUP: "warnline", NO_TRADE: "neg" }[act] || "";
    const failed = (dec?.conditions || []).filter((c) => c.ok === false);
    const plan = tr.position ? { "Вход": fmt.price(tr.position.entry_mid), "Стоп": fmt.price(tr.position.stop), "Цель 1": fmt.price(tr.position.t1) } :
      dec?.plan ? { "Опорная цена": fmt.price(dec.plan.entry_ref), "Стоп": fmt.price(dec.plan.stop), "Цель 1": fmt.price(dec.plan.t1),
                    ...(dec.plan.t2 ? { "Цель 2": fmt.price(dec.plan.t2) } : {}), "Чистый R:R": fmt.num(dec.plan.rr_net, 2),
                    "Trailing": `${dec.plan.trail_atr} ATR`, "Time stop": `${dec.plan.time_stop} баров` } : null;
    body("overview").innerHTML = `<div class="grid2">
      <div class="box">
        <div class="decision-head"><span class="muted">Решение на закрытии ${fmt.dt(dec?.decided_at)}:</span><span class="act ${actCls}">${esc(act)}</span>
          <span class="badge">${esc(tr.state)}</span></div>
        <h4>Состояние рынка</h4><div><b>${esc(reg.regime || "—")}</b> <span class="sec">— ${esc(reg.why || "")}</span></div>
        <h4 style="margin-top:10px">Аргументы за вход</h4>${conds((dec?.conditions || []).filter((c) => c.ok === true))}
        <h4 style="margin-top:10px">Аргументы против</h4>${failed.length ? conds(failed) : `<div class="sec">нет</div>`}
        ${(dec?.conditions || []).some((c) => c.ok == null) ? `<h4 style="margin-top:10px">Не проверено (нет данных)</h4>${conds(dec.conditions.filter((c) => c.ok == null))}` : ""}
      </div>
      <div class="box">
        <h4>Что изменилось с предыдущей закрытой свечи</h4><ul class="plain">${an.changes.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
        <h4 style="margin-top:10px">${act === "ENTER" || act === "HOLD" ? "Статус" : "Причина ожидания"}</h4>
        <div class="sec">${act === "WARMUP" ? "недостаточно истории для индикаторов стратегии" : failed[0] ? esc(failed[0].name) + ": " + esc(failed[0].detail) : act === "HOLD" ? "позиция сопровождается" : "—"}</div>
        <h4 style="margin-top:10px">Ближайшие условия изменения решения</h4>
        <div class="sec">${esc(dec?.next || "—")}</div>
        ${failed.length > 1 ? `<ul class="plain">${failed.slice(1).map((c) => `<li>должно выполниться: ${esc(c.name)}</li>`).join("")}</ul>` : ""}
        <h4 style="margin-top:10px">${tr.position ? "Текущий план позиции" : "Гипотетический план (вход не разрешён)"}</h4>
        ${plan ? `<div class="kv">${Object.entries(plan).map(([k, v]) => `<span class="k">${k}</span><span class="v">${v}</span>`).join("")}</div>` : `<div class="sec">плана нет: базовый сетап не обнаружен</div>`}
        <h4 style="margin-top:10px">Риск-фильтры</h4>${conds(dec?.risk || tr.risk.checks)}
        <h4 style="margin-top:10px">Полное объяснение решения</h4><pre class="expl">${esc(dec?.explanation || "")}</pre>
      </div></div>`;

    body("ind").innerHTML = `<table class="t"><tr><th>Индикатор</th><th>Значение</th><th>Мин. баров</th><th>Статус</th><th>Интерпретация</th></tr>
      ${an.indicators.map((r) => `<tr><td>${esc(r.name)}</td><td class="n">${esc(r.display)}</td><td class="n">${r.min_bars ?? "—"}</td>
        <td><span class="badge ${r.status === "OK" ? "good" : "warn"}">${esc(r.status)}</span></td><td class="sec">${esc(r.text)}</td></tr>`).join("")}</table>
      <div class="muted" style="margin-top:6px">Значения на закрытии ${fmt.dt(an.as_of_ts)}. Индикатор без достаточной истории не рассчитывается и не используется стратегией.</div>`;

    const divs = (st.divergences || []).slice().reverse();
    body("struct").innerHTML = `<div class="grid2">
      <div class="box"><h4>Swing-структура</h4><table class="t"><tr><th>Метка</th><th>Время</th><th>Цена</th><th>Подтверждён</th></tr>
        ${(st.swings || []).slice().reverse().map((x) => `<tr><td>${x.label}</td><td>${fmt.time(x.ts)}</td><td class="n">${fmt.price(x.price)}</td><td class="n">бар ${x.confirmed_idx}</td></tr>`).join("") || "<tr><td colspan=4 class='muted'>нет</td></tr>"}</table>
        <h4 style="margin-top:10px">События структуры (BOS / CHoCH)</h4><table class="t"><tr><th>Тип</th><th>Время</th><th>Уровень</th></tr>
        ${(st.events || []).slice(-10).reverse().map((e) => `<tr><td>${e.type}</td><td>${fmt.time(e.ts)}</td><td class="n">${fmt.price(e.level)}</td></tr>`).join("") || "<tr><td colspan=3 class='muted'>нет</td></tr>"}</table></div>
      <div class="box"><h4>Подтверждённые дивергенции</h4><table class="t"><tr><th>Индикатор</th><th>Тип</th><th>p1 → p2 (цена)</th><th>Δ цены</th><th>Δ инд.</th><th>Подтв.</th></tr>
        ${divs.map((x) => `<tr><td>${x.indicator}${x.multiplicity > 1 ? " ×2" : ""}</td><td>${x.type}</td><td class="n">${fmt.time(x.p1.ts)} ${fmt.price(x.p1.price)} → ${fmt.time(x.p2.ts)} ${fmt.price(x.p2.price)}</td>
          <td class="n">${fmt.sPct(x.price_change_pct)}</td><td class="n">${fmt.num(x.ind_change, 3)}</td><td>${fmt.time(x.confirmed_time)}</td></tr>`).join("") || "<tr><td colspan=6 class='muted'>нет</td></tr>"}</table>
        <div class="muted">Дивергенция — информация, а не сигнал: известна только после подтверждения второго pivot.</div>
        <h4 style="margin-top:10px">Уровни S/R (кластеры pivots)</h4><div class="sec mono">${(st.levels || []).map((l) => `${fmt.price(l.price)} ×${l.touches}`).join(" · ") || "нет"}</div>
        <h4 style="margin-top:10px">Логарифмический Fibonacci последнего импульса</h4>
        ${st.fib ? `<div class="sec mono">${fmt.price(st.fib.low.price)} (${fmt.time(st.fib.low.time)}) → ${fmt.price(st.fib.high.price)} (${fmt.time(st.fib.high.time)})<br>
          ${Object.entries(st.fib.retracement).map(([r, v]) => `${r}: ${fmt.price(v)}`).join(" · ")}<br>ext ${Object.entries(st.fib.extension).map(([r, v]) => `${r}: ${fmt.price(v)}`).join(" · ")}</div>` : `<div class="sec">нет подтверждённого импульса</div>`}
        <h4 style="margin-top:10px">Fair value gaps (последние)</h4><div class="sec mono">${(st.fvg || []).map((f) => `${f.type} ${fmt.time(f.ts)} ${fmt.price(f.bottom)}–${fmt.price(f.top)}`).join("<br>") || "нет"}</div></div></div>`;

    body("onchain").innerHTML = onchain();

    const counts = {};
    tr.decisions.forEach((x) => (counts[x.action_final] = (counts[x.action_final] || 0) + 1));
    const blk = {};
    tr.decisions.forEach((x) => x.blocker && (blk[x.blocker] = (blk[x.blocker] || 0) + 1));
    body("dec").innerHTML = `<div class="sec" style="margin-bottom:8px">Всего решений: ${tr.decisions.length} · ${Object.entries(counts).map(([k, v]) => `${k}: ${v}`).join(" · ")}<br>
      Главные блокеры: ${Object.entries(blk).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([k, v]) => `${esc(k)} — ${v}`).join("; ") || "нет"}</div>
      <table class="t"><tr><th>Закрытие свечи</th><th>Решение</th><th>Состояние</th><th>Первый блокер</th><th>R:R</th></tr>
      ${tr.decisions.slice(0, 150).map((x, i) => `<tr class="click" data-i="${i}"><td>${fmt.dt(x.decided_at)}</td><td>${esc(x.action_final)}</td><td>${esc(x.state)}</td>
        <td class="sec">${esc(x.blocker || "")}</td><td class="n">${x.rr_net == null ? "—" : fmt.num(x.rr_net, 2)}</td></tr>
        <tr data-exp="${i}" style="display:none"><td colspan="5"><pre class="expl">${esc(x.explanation)}</pre></td></tr>`).join("")}</table>`;
    body("dec").querySelectorAll("tr.click").forEach((r) => r.addEventListener("click", () => {
      const e = body("dec").querySelector(`[data-exp="${r.dataset.i}"]`);
      e.style.display = e.style.display === "none" ? "" : "none";
    }));

    const dg = tr.diagnostics, q = dg.quality;
    body("diag").innerHTML = `<div class="grid2">
      <div class="box"><h4>Данные</h4><div class="kv">
        <span class="k">Набор</span><span class="v">${esc(dg.dataset)}</span>
        <span class="k">Закрытых свечей</span><span class="v">${q.bars}</span>
        <span class="k">Пропусков (всего / в окне)</span><span class="v">${q.gaps_total} / ${q.gaps_recent}</span>
        <span class="k">Конфликтов / невалидных</span><span class="v">${q.conflicts} / ${q.invalid}</span>
        <span class="k">Отброшено незакрытых</span><span class="v">${q.dropped_unclosed}</span>
        <span class="k">Качество</span><span class="v ${q.ok ? "pos" : "neg"}">${q.ok ? "OK" : "DATA_ERROR"}</span></div>
        <h4 style="margin-top:10px">Эксперимент</h4><div class="kv">
        <span class="k">ID</span><span class="v">${esc(dg.experiment)}</span>
        ${dg.start_ts ? `<span class="k">Старт торговли</span><span class="v">${fmt.dt(dg.start_ts)}</span>` : ""}
        ${dg.persist ? `<span class="k">Журнал в БД</span><span class="v">${esc(dg.persist.status)}</span>` : ""}
        <span class="k">Модель издержек</span><span class="v">${esc(dg.cost_model.name)}</span>
        <span class="k">Комиссия buy / sell</span><span class="v">${fmt.pct(dg.cost_model.buy_fee * 100, 2)} / ${fmt.pct(dg.cost_model.sell_fee * 100, 2)}</span>
        <span class="k">Slippage / стоп</span><span class="v">${fmt.pct(dg.cost_model.slippage * 100, 2)} / +${fmt.pct(dg.cost_model.stop_slippage * 100, 2)}</span>
        <span class="k">Глубина модели</span><span class="v">${fmt.usd(Number(dg.cost_model.depth_usd), 0)}</span></div>
        <div class="muted" style="margin-top:4px">${esc(dg.cost_model.label)}</div></div>
      <div class="box"><h4>Риск (${esc(tr.risk.version)})</h4><div class="kv">
        ${Object.entries(tr.risk.config).map(([k, v]) => `<span class="k">${esc(k)}</span><span class="v">${esc(v)}</span>`).join("")}
        <span class="k">Kill switch</span><span class="v ${tr.risk.killed ? "neg" : ""}">${tr.risk.killed ? esc(tr.risk.kill_reason) : "не сработал"}</span>
        <span class="k">Серия убытков</span><span class="v">${tr.risk.loss_streak}</span>
        <span class="k">Пик equity</span><span class="v">${fmt.usd(tr.risk.peak_equity)}</span></div>
        <h4 style="margin-top:10px">Параметры стратегии</h4><pre class="expl">${esc(JSON.stringify(tr.strategy.params, null, 1))}</pre>
        <h4>События исполнения</h4><div class="sec mono">${tr.events.map((e) => `${fmt.time(e.ts)} ${esc(e.type)}: ${esc(e.why)}`).join("<br>") || "нет"}</div></div></div>`;
  }

  function onchain() {
    const m = overview?.market, p = overview?.price;
    if (!m) return `<div class="empty">Снимка рынка нет.</div>`;
    const tx = m.txns || {};
    const row = (k) => tx[k] ? `<tr><td>${k}</td><td class="n">${tx[k].buys}</td><td class="n">${tx[k].sells}</td><td class="n">${tx[k].buys ? fmt.num(tx[k].sells / tx[k].buys, 2) : "—"}</td></tr>` : "";
    return `<div class="grid2">
      <div class="box"><h4>Срез пула (DexScreener, ${fmt.dt(p.ts)}) — РЕАЛЬНОЕ ИЗМЕРЕНИЕ, не история</h4><div class="kv">
        <span class="k">Цена USD / USDG</span><span class="v">${fmt.price(p.usd)} / ${fmt.price(p.usdg)}</span>
        <span class="k">Ликвидность</span><span class="v">${fmt.big(m.liquidity_usd)}</span>
        <span class="k">FDV / Market cap</span><span class="v">${fmt.big(m.fdv)} / ${fmt.big(m.market_cap)}</span>
        <span class="k">Объём 1ч / 6ч / 24ч</span><span class="v">${fmt.big(m.volume?.h1)} / ${fmt.big(m.volume?.h6)} / ${fmt.big(m.volume?.h24)}</span>
        <span class="k">Объём / ликвидность 24ч</span><span class="v">${m.liquidity_usd ? fmt.num(m.volume.h24 / m.liquidity_usd, 2) + "×" : "—"}</span>
        <span class="k">Δ цены 1ч / 6ч / 24ч</span><span class="v">${fmt.sPct(m.price_change?.h1)} / ${fmt.sPct(m.price_change?.h6)} / ${fmt.sPct(m.price_change?.h24)}</span></div>
        <h4 style="margin-top:10px">Сделки: покупки / продажи</h4><table class="t"><tr><th>Окно</th><th>Buys</th><th>Sells</th><th>Sells/Buys</th></tr>${row("h1")}${row("h6")}${row("h24")}</table></div>
      <div class="box"><h4>Измерено ончейн (2026-11-05)</h4><ul class="plain">
        <li>Комиссия hook: покупка 0,50%, продажа 1,00% — по 88 событиям Swap (evidence/fables_swaps)</li>
        <li>Основной пул = launchPoolId официального конфига; PoolKey сверен с poolId</li></ul>
        <h4 style="margin-top:10px">Недоступно (не рассчитывается и не подменяется оценками)</h4><ul class="plain">
        <li>CVD / order flow / whale activity — нет потока Swap-событий (нужен разрешённый RPC-сборщик)</li>
        <li>Держатели и концентрация — Blockscout закрыт Cloudflare из облака, Transfer-индексация не запускалась</li>
        <li>Свежая эмиссия — данные Release A только до блока 106 387 177; индексатор не возобновлялся</li>
        <li>История ликвидности — только снимок; фильтры S3 по ликвидности помечаются «не проверено»</li></ul></div></div>`;
  }

  // ------------------------------------------------------------------ загрузка
  async function loadTerminal(tf) {
    const d = await api(`/api/terminal/${tf}?mode=${mode}`);
    T[tf].data = d;
    setData(tf, d);
    applyOverlays(tf);
    renderHead(tf); renderPosition(tf); renderOps(tf); renderAnalysis(tf);
    onCrosshair(tf, null);
  }

  function banner() {
    const b = document.getElementById("modeBanner");
    if (mode === "BACKTEST") {
      b.innerHTML = `<b>BACKTEST · NOT VALIDATED.</b> Офлайн-воспроизведение записанных закрытых свечей (${esc(overview.dataset.id)}) тем же кодом стратегий; исполнение по следующей цене,
        консервативная модель издержек. Это не forward-результат и не прогноз. Старт: ${fmt.dt(overview.backtest.start_ts)}.`;
    } else {
      const f = overview.forward;
      b.innerHTML = `<b>FORWARD · ${Lab.STATUS_RU[f.status]}.</b> ${f.status === "NOT_STARTED" ? "Эксперимент не запущен — данные ниже только рыночные, сделок и PnL нет." :
        `Эксперимент ${esc(f.experiment)} начат ${fmt.dt(f.started_at)}.`} Живые данные: <b>${esc(f.feed.status)}</b> — ${esc(f.feed.reason)}.`;
    }
  }

  async function refresh(full) {
    overview = await api("/api/overview");
    Lab.renderTopbar(overview, mode, "terminal");
    document.querySelectorAll("#modeSeg [data-mode]").forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.mode === mode) return;
      mode = b.dataset.mode; store.set("lab.mode", mode); refresh(true);
    }));
    banner();
    if (full) await Promise.all(TFS.map(loadTerminal));
  }

  async function init() {
    const main = document.getElementById("terminals");
    TFS.forEach((tf) => { T[tf] = {}; main.appendChild(skeleton(tf)); buildChart(tf); });
    try { await refresh(true); } catch (e) {
      document.getElementById("modeBanner").innerHTML = `<b>Ошибка загрузки:</b> ${esc(e.message)}`;
      console.error(e);
    }
    document.addEventListener("lab:refresh", () => refresh(mode === "FORWARD"));
    setInterval(() => refresh(false).catch(() => {}), 30000);
  }
  init();
})();
