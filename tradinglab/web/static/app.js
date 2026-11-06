/* Три торговых терминала (1H/4H/1D). Все данные — из /api/terminal/{tf}: свечи, индикаторы, операции и решения ядра. */
"use strict";

(() => {
  const { fmt, esc, api, css, store } = Lab;
  const LW = window.LightweightCharts;
  const TFS = ["1h", "4h", "1d"];
  const TITLES = { "1h": "TRADER 1H", "4h": "TRADER 4H", "1d": "TRADER 1D" };
  const TF_DT = { "1h": 3600, "4h": 14400, "1d": 86400 };
  const OV_OFF = new Set(["fib", "ievents", "iswing", "iswing_mid", "legacy", "ltfmajor"]);
  const LTF_OF = { "1h": null, "4h": "1h", "1d": "4h" };   // слои, выключенные по умолчанию (читаемость)
  const TABS = [["overview", "Обзор"], ["ind", "Индикаторы"], ["struct", "Структура"], ["onchain", "Ончейн"], ["dec", "Решения"], ["audit", "Аудит индикаторов"], ["diag", "Диагностика"]];
  const MOBILE = () => window.matchMedia("(max-width: 640px), (hover: none)").matches;
  const OSC = ["rsi", "macd", "mfi", "cmf", "obv"];          // панели осцилляторов сверху вниз (значения — из ядра)
  const DIV_PANE = { RSI: "rsi", MACD_HIST: "macd", MFI: "mfi", CMF: "cmf", OBV: "obv" };
  const OSC_LABEL = { rsi: "RSI(14) · 30 / 50 / 70", macd: "MACD(12,26,9) · гистограмма = MACD − Signal", mfi: "MFI(14) · 20 / 80",
                      cmf: "CMF(20)", obv: "OBV" };
  const rsiOn = () => store.get("lab.rsi", "1") !== "0";
  let mode = store.get("lab.mode", "FORWARD");
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
               ["fib", "Fibonacci", "--s4"],
               ["mswing", "Major swings v3", "--s-violet"], ["iswing_mid", "Intermediate swings", "--text-secondary"], ["mevents", "Major BOS/CHoCH v3", "--s2"], ["prot", "Protected v3", "--down"],
               ["ievents", "iBOS/iCHoCH v3 (internal)", "--text-muted"], ["iswing", "Локальные swing", "--text-muted"], ["phase", "Фаза и диапазон", "--s4"],
               ["legacy", "Legacy v1/v2", "--text-muted"],
               ...(LTF_OF[tf] ? [["ltfmajor", `${LTF_OF[tf].toUpperCase()} Major (сравнение)`, "--warn"]] : []),
               ["div", "Дивергенции", "--up"], ["trades", "Сделки", "--text-primary"]]
              .map(([k, n, c]) => `<label><input type="checkbox" data-ov="${k}" ${OV_OFF.has(k) ? "" : "checked"}><span class="legend-dot" style="background:var(${c})"></span>${n}</label>`).join("")}
            <label title="панель RSI(14) из аналитического ядра"><input type="checkbox" data-osc="rsi" ${rsiOn() ? "checked" : ""}><span class="legend-dot" style="background:var(--accent)"></span>RSI</label>
            <span class="spacer"></span><span class="muted">UTC · панели: RSI · MACD · MFI · CMF · OBV</span>
          </div>
          <div class="chart" data-f="chart"><div class="legend" data-f="legend"></div><div class="tip" data-f="tip"></div></div>
          <div class="barinfo" data-f="barinfo"></div>
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
      if (b.dataset.tab === "audit") renderAudit(tf);
      sec.querySelectorAll("[data-tab]").forEach((x) => x.classList.toggle("on", x === b));
      sec.querySelectorAll("[data-body]").forEach((x) => x.classList.toggle("on", x.dataset.body === b.dataset.tab));
    }));
    sec.querySelectorAll("[data-flt]").forEach((s) => s.addEventListener("change", () => renderOps(tf)));
    sec.querySelectorAll("[data-ov]").forEach((c) => c.addEventListener("change", () => applyOverlays(tf)));
    sec.querySelector('[data-osc="rsi"]').addEventListener("change", (e) => { store.set("lab.rsi", e.target.checked ? "1" : "0"); rebuildChart(tf); });
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
    const p6 = { type: "price", precision: 6, minMove: 0.000001 };
    const line = (color, pane, extra = {}) => chart.addSeries(LW.LineSeries, { color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
                                                                               crosshairMarkerVisible: false, ...extra }, pane);
    // индексы панелей: цена = 0, затем включённые осцилляторы по порядку OSC
    const panes = {};
    OSC.filter((k) => k !== "rsi" || rsiOn()).forEach((k, i) => (panes[k] = i + 1));
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
    if (panes.rsi) {
      const fixed = () => ({ priceRange: { minValue: 0, maxValue: 100 } });      // шкала RSI всегда 0–100
      const rp = { type: "price", precision: 1, minMove: 0.1 };
      // зоны: заливка между линией RSI и уровнем 70 (выше — перекупленность) / 30 (ниже — перепроданность)
      s.rsiOb = chart.addSeries(LW.BaselineSeries, { baseValue: { type: "price", price: 70 }, topLineColor: "rgba(0,0,0,0)", bottomLineColor: "rgba(0,0,0,0)",
        topFillColor1: "rgba(230,103,103,.45)", topFillColor2: "rgba(230,103,103,.15)", bottomFillColor1: "rgba(0,0,0,0)", bottomFillColor2: "rgba(0,0,0,0)",
        lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, autoscaleInfoProvider: fixed, priceFormat: rp }, panes.rsi);
      s.rsiOs = chart.addSeries(LW.BaselineSeries, { baseValue: { type: "price", price: 30 }, topLineColor: "rgba(0,0,0,0)", bottomLineColor: "rgba(0,0,0,0)",
        topFillColor1: "rgba(0,0,0,0)", topFillColor2: "rgba(0,0,0,0)", bottomFillColor1: "rgba(25,158,112,.15)", bottomFillColor2: "rgba(25,158,112,.45)",
        lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, autoscaleInfoProvider: fixed, priceFormat: rp }, panes.rsi);
      s.rsi = line(css("--accent"), panes.rsi, { lineWidth: 2, lastValueVisible: true, title: "RSI", crosshairMarkerVisible: true,
        autoscaleInfoProvider: fixed, priceFormat: rp });
      s.rsi.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.06 } });   // 0–100 почти на всю высоту панели
      s.rsi.createPriceLine({ price: 70, color: "rgba(230,103,103,.8)", lineWidth: 1, lineStyle: LW.LineStyle.Dashed, axisLabelVisible: true, title: "70" });
      s.rsi.createPriceLine({ price: 50, color: "#4a5360", lineWidth: 1, lineStyle: LW.LineStyle.Dotted, axisLabelVisible: false, title: "" });
      s.rsi.createPriceLine({ price: 30, color: "rgba(25,158,112,.8)", lineWidth: 1, lineStyle: LW.LineStyle.Dashed, axisLabelVisible: true, title: "30" });
    }
    s.hist = chart.addSeries(LW.HistogramSeries, { priceLineVisible: false, lastValueVisible: true, title: "Hist", priceFormat: p6 }, panes.macd);
    s.macd = line(css("--s1"), panes.macd, { lineWidth: 1, lastValueVisible: true, title: "MACD", priceFormat: p6 });
    s.sig = line(css("--s2"), panes.macd, { lineWidth: 1, lastValueVisible: true, title: "Signal", priceFormat: p6 });
    s.mfi = line(css("--s-violet"), panes.mfi, { lineWidth: 1, lastValueVisible: true, title: "MFI", priceFormat: { type: "price", precision: 1, minMove: 0.1 } });
    s.cmf = chart.addSeries(LW.HistogramSeries, { priceLineVisible: false, lastValueVisible: true, title: "CMF", priceFormat: { type: "price", precision: 3, minMove: 0.001 } }, panes.cmf);
    s.obv = line(css("--s1"), panes.obv, { lineWidth: 1, lastValueVisible: true, title: "OBV", priceFormat: { type: "volume" } });
    s.mfi.createPriceLine({ price: 80, color: "#3a434e", lineWidth: 1, lineStyle: LW.LineStyle.Dashed, axisLabelVisible: false });
    s.mfi.createPriceLine({ price: 20, color: "#3a434e", lineWidth: 1, lineStyle: LW.LineStyle.Dashed, axisLabelVisible: false });
    s.cmf.createPriceLine({ price: 0, color: "#3a434e", lineWidth: 1, axisLabelVisible: false });
    T[tf].chart = chart; T[tf].s = s; T[tf].lines = []; T[tf].extra = []; T[tf].panes = panes;
    window.__lab = T;   // для автоматических UI-проверок (только чтение)
    T[tf].markers = LW.createSeriesMarkers(s.candle, []);
    chart.subscribeCrosshairMove((p) => onCrosshair(tf, p));
    sizePanes(tf);
    if (!T[tf].ro) {
      T[tf].ro = new ResizeObserver(() => sizePanes(tf));
      T[tf].ro.observe(el);
    }
  }

  function sizePanes(tf) {
    const t = T[tf], el = $(tf, "chart");
    if (!t.chart) return;
    const panes = t.chart.panes(), n = panes.length - 1;
    const H = Math.max(el.clientHeight - 30, 300);                     // минус ось времени
    const w = (i) => (i === t.panes.rsi ? 1.6 : 1);                    // RSI выше остальных: зоны 30/70 должны читаться
    const units = Array.from({ length: n }, (_, k) => w(k + 1)).reduce((x, y) => x + y, 0);
    const osc = Math.max(44, Math.min(90, Math.floor(H * 0.52 / Math.max(units, 1))));
    // пропорции (а не абсолютные высоты): setHeight перераспределяет высоту соседней панели, stretch factor — нет
    panes.forEach((p, i) => p.setStretchFactor(i === 0 ? Math.max(160, H - osc * units) : osc * w(i)));
    requestAnimationFrame(() => placeLabels(tf));
  }

  function placeLabels(tf) {
    const t = T[tf], el = $(tf, "chart");
    el.querySelectorAll(".pane-label").forEach((x) => x.remove());
    let y = 0;
    t.chart.panes().forEach((p, i) => {
      if (i > 0) {
        const key = Object.keys(t.panes).find((k) => t.panes[k] === i);
        const d = document.createElement("div");
        d.className = "pane-label";
        d.style.top = `${y + 2}px`;
        d.textContent = OSC_LABEL[key] || "";
        el.appendChild(d);
      }
      y += p.getHeight() + 1;
    });
  }

  function rebuildChart(tf) {
    const t = T[tf];
    const range = t.chart?.timeScale().getVisibleLogicalRange();
    t.chart?.remove();
    buildChart(tf);
    if (t.data) {
      t.ranged = t.data.mode;                                          // масштаб пользователя сохраняется
      setData(tf, t.data);
      applyOverlays(tf);
      onCrosshair(tf, null);
      if (range) t.chart.timeScale().setVisibleLogicalRange(range);
    }
  }

  function setData(tf, d) {
    const { s } = T[tf];
    // живых свечей может ещё не быть (бот загружает историю) — отсутствующие ряды отображаются пустыми
    const S = new Proxy(d.series || {}, { get: (o, k) => o[k] || [] });
    const cs = d.candles.map((k) => ({ time: k.time, open: k.open, high: k.high, low: k.low, close: k.close }));
    const vs = d.candles.map((k) => ({ time: k.time, value: k.volume, color: k.close >= k.open ? "rgba(25,158,112,.35)" : "rgba(230,103,103,.35)" }));
    // формирующаяся свеча (и предварительно закрытая, пока GT её не отдал): только отображение, в сигналах не участвует
    liveBars(d).forEach((b) => { cs.push(styledBar(b)); if (b.volume != null) vs.push({ time: b.time, value: b.volume, color: "rgba(166,174,185,.25)" }); });
    s.candle.setData(cs);
    s.vol.setData(vs);
    s.emaF.setData(S.ema_fast); s.emaS.setData(S.ema_slow);
    s.bbU.setData(S.bb_upper); s.bbM.setData(S.bb_mid); s.bbL.setData(S.bb_lower);
    s.hist.setData(S.macd_hist.map((p) => ({ ...p, color: p.value >= 0 ? "rgba(25,158,112,.75)" : "rgba(230,103,103,.75)" })));
    s.macd.setData(S.macd); s.sig.setData(S.macd_signal); s.mfi.setData(S.mfi);
    s.cmf.setData(S.cmf.map((p) => ({ ...p, color: p.value >= 0 ? "rgba(25,158,112,.75)" : "rgba(230,103,103,.75)" })));
    s.obv.setData(S.obv);
    if (s.rsi) {                                   // RSI(14) — ряд ядра без пересчёта
      s.rsi.setData(S.rsi); s.rsiOb.setData(S.rsi); s.rsiOs.setData(S.rsi);
    }
    T[tf].byTime = new Map(d.candles.map((k) => [k.time, k]));
    T[tf].ind = Object.fromEntries(Object.entries(S).map(([n, arr]) => [n, new Map(arr.map((p) => [p.time, p.value]))]));
    // по умолчанию — период с начала торговли (день запуска с пиком первого часа только история; прокрутка доступна)
    if (T[tf].ranged === d.mode) return;      // при автообновлении масштаб пользователя не сбрасывается
    T[tf].ranged = d.mode;
    const start = overview?.backtest?.start_ts;
    const from = start ? d.candles.findIndex((k) => k.time >= start) : 0;
    if (from > 0) T[tf].chart.timeScale().setVisibleLogicalRange({ from, to: d.candles.length + 3 });
    else T[tf].chart.timeScale().fitContent();
  }

  function liveBars(d) {
    const last = d.candles.at(-1)?.time ?? -1;
    return d.mode === "FORWARD" ? (d.live?.bars || []).filter((b) => b.time > last) : [];
  }

  function styledBar(b) {
    const up = b.close >= b.open;
    const a = b.status === "FORMING" ? 0.35 : 0.55;              // формирующаяся — полупрозрачная, отдельного цвета
    const col = up ? `rgba(25,158,112,${a})` : `rgba(230,103,103,${a})`;
    return { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close, color: col, wickColor: col, borderColor: up ? css("--up") : css("--down") };
  }

  function buildMarkers(tf) {
    const t = T[tf], d = t.data, tr = d.trader, st = tr?.analysis?.structure || d.preview?.structure || {};
    const on = (k) => !!document.querySelector(`#t-${tf} [data-ov="${k}"]`)?.checked;
    const marks = [];
    if (on("trades")) (tr?.ops || []).forEach((o) => marks.push(o.side === "BUY"
      ? { time: o.ts, position: "belowBar", color: css("--up"), shape: "arrowUp", text: "BUY" }
      : { time: o.ts, position: "aboveBar", color: css("--down"), shape: "arrowDown", text: o.reason.startsWith("цель") ? "T1" : o.reason.startsWith("стоп") ? "STOP" : "SELL" }));
    const lab = (x) => x.replace("_UP", "↑").replace("_DOWN", "↓");
    const v3 = S3(tf);
    if (v3 && !v3.empty) {
      // major: только точки со структурной ролью ядра (tier === "major"); intermediate — отдельный слой
      if (on("mswing")) v3.major.swings.filter((w) => w.tier === "major").forEach((w) => marks.push({ time: w.ts, position: w.kind === "H" ? "aboveBar" : "belowBar",
        color: css("--s-violet"), shape: "circle", size: 0.8, text: (w.major_label || w.kind) + " · major" }));
      // младший ТФ — только для сравнения: отдельный цвет и подпись, не сигналы этого ТФ
      if (on("ltfmajor") && v3.ltf_major) v3.ltf_major.swings.forEach((w) => marks.push({ time: w.ts - (w.ts % TF_DT[tf]), position: w.kind === "H" ? "aboveBar" : "belowBar",
        color: css("--warn"), shape: "circle", size: 0.4, text: `${v3.ltf_major.tf.toUpperCase()} ${w.major_label || w.kind} (сравн.)` }));
      if (on("iswing_mid")) v3.major.swings.filter((w) => w.tier === "intermediate").forEach((w) => marks.push({ time: w.ts, position: w.kind === "H" ? "aboveBar" : "belowBar",
        color: "#7d8590", shape: "circle", size: 0.25, text: "int·" + (w.label || w.kind) }));
      if (on("mevents")) v3.major.events.forEach((e) => marks.push({ time: e.ts, position: /UP|HIGH/.test(e.type) ? "aboveBar" : "belowBar",
        color: e.type.startsWith("CHoCH") ? css("--s2") : e.type.startsWith("BOS") ? css("--s-violet") : e.type === "AMBIGUOUS" ? css("--warn") : "#9aa4af",
        shape: e.type.startsWith("SWEEP") || e.type === "AMBIGUOUS" ? "square" : /UP/.test(e.type) ? "arrowUp" : "arrowDown", size: e.type.startsWith("SWEEP") ? 0.4 : 1,
        text: (e.type === "AMBIGUOUS" ? "AMBIG" : S3LAB(e.type)) + (e.sweep ? " (тень)" : "") + (e.scale_conflict ? " ⚠" : "") }));
      if (on("mevents")) v3.scale_conflicts.filter((c) => c.type === "SCALE_CONFLICT").forEach((c) => marks.push({ time: c.ts, position: "aboveBar", color: css("--warn"), shape: "square", size: 0.5, text: "SCALE_CONFLICT" }));
      if (on("ievents")) v3.internal.events.filter((e) => !e.type.startsWith("iSWEEP")).forEach((e) => marks.push({ time: e.ts, position: e.type.endsWith("UP") ? "belowBar" : "aboveBar",
        color: "#8b949e", shape: "circle", size: 0.35, text: S3LAB(e.type) }));
      if (on("phase")) v3.timeline.forEach((p, k) => (k === 0 || p.major !== v3.timeline[k - 1].major || p.phase !== v3.timeline[k - 1].phase) &&
        marks.push({ time: p.ts, position: "belowBar", color: css("--s4"), shape: "square", size: 0.3, text: p.major === "UP" || p.major === "DOWN" ? `${p.major} ${p.phase}` : p.major + (p.bias ? "/" + p.bias : "") }));
    }
    if (st.v2 && on("legacy")) {
      if (on("legacy")) st.v2.external.forEach((e) => marks.push({ time: e.ts, position: e.type.endsWith("UP") ? "belowBar" : "aboveBar",
        color: e.type.startsWith("CHoCH") ? css("--s2") : css("--s-violet"), shape: "circle", size: 0.8, text: lab(e.type) }));
      st.v2.internal.slice(-30).forEach((e) => marks.push({ time: e.ts, position: e.type.endsWith("UP") ? "belowBar" : "aboveBar",
        color: "#6e7783", shape: "circle", size: 0.3, text: "v1 " + lab(e.type) }));
    }
    liveBars(d).forEach((b) => marks.push({ time: b.time, position: "aboveBar", color: css("--text-secondary"), shape: "square", size: 0.5,
      text: b.status === "FORMING" ? "FORMING" : "закрыта · ждёт GT" }));
    marks.sort((a, b) => a.time - b.time);
    return marks;
  }

  async function updateLive(tf) {
    const t = T[tf];
    if (!t.data || t.data.mode !== "FORWARD" || !t.data.candles.length) return;
    const lv = await api(`/api/live/${tf}`);
    const prev = Math.max(-1, ...(t.data.live?.bars || []).map((b) => b.time));
    const now = Math.max(-1, ...lv.bars.map((b) => b.time));
    t.data.live = lv;
    if (now > prev && prev > 0) { await loadTerminal(tf); return; }  // период закрылся: полная перезагрузка (закрытая свеча из БД)
    liveBars(t.data).forEach((b) => { t.s.candle.update(styledBar(b)); if (b.volume != null) t.s.vol.update({ time: b.time, value: b.volume, color: "rgba(166,174,185,.25)" }); });
    t.markers.setMarkers(buildMarkers(tf));
    if (!t.hovering) onCrosshair(tf, null);
  }

  function applyOverlays(tf) {
    const t = T[tf], d = t.data, s = t.s;
    if (!d) return;
    const on = (k) => !!document.querySelector(`#t-${tf} [data-ov="${k}"]`)?.checked;
    s.emaF.applyOptions({ visible: on("ema") }); s.emaS.applyOptions({ visible: on("ema") });
    [s.bbU, s.bbM, s.bbL].forEach((x) => x.applyOptions({ visible: on("bb") }));
    s.vol.applyOptions({ visible: on("vol") });
    t.lines.forEach((l) => s.candle.removePriceLine(l)); t.lines = [];
    t.extra.forEach((x) => t.chart.removeSeries(x)); t.extra = []; t.structSegs = [];
    const tr = d.trader, st = tr?.analysis?.structure || d.preview?.structure || {};
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
    const line = (pts, color, width, style, keep = true) => {
      const xs = pts.filter((p, k) => k === 0 || p.time > pts[k - 1].time);
      if (xs.length < 2) return null;
      const x = t.chart.addSeries(LW.LineSeries, { color, lineWidth: width, lineStyle: style, priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false, pointMarkersVisible: false, priceFormat: { type: "price", precision: 6, minMove: 0.000001 } }, 0);
      x.setData(xs);
      t.extra.push(x);
      if (keep) t.structSegs.push(x);
      return x;
    };
    const v3 = S3(tf);
    t.v3lines = { mswing: 0, iswing_mid: 0, mevents: 0, prot: 0, ievents: 0, iswing: 0, phase: 0 };
    t.v3series = {};
    if (v3 && !v3.empty) {
      const lastT = d.candles.at(-1)?.time;
      // главная линия — напрямую от одного major-экстремума к следующему, без промежуточных точек
      if (on("mswing")) { const x = line(v3.major.swings.filter((w) => w.tier === "major").map((w) => ({ time: w.ts, value: w.price })), css("--s-violet"), 2, LW.LineStyle.Solid);
        if (x) { t.v3lines.mswing++; t.v3series.major = x; } }
      if (on("ltfmajor") && v3.ltf_major) {
        const pts = v3.ltf_major.swings.map((w) => ({ time: w.ts - (w.ts % TF_DT[tf]), value: w.price }));   // точка на свече старшего ТФ, содержащей экстремум
        const x = line(pts, css("--warn"), 1, LW.LineStyle.LargeDashed);
        if (x) { t.v3lines.ltfmajor = 1; t.v3series.ltf = x; }
      }
      // intermediate: тонкий пунктир по полному major-ZigZag (major + intermediate), отдельной серией
      if (on("iswing_mid")) { const x = line(v3.major.swings.map((w) => ({ time: w.ts, value: w.price })), "#7d8590", 1, LW.LineStyle.Dotted);
        if (x) { t.v3lines.iswing_mid++; t.v3series.inter = x; } }
      // internal — собственная модель (ZigZag internal), не смешивается с intermediate
      if (on("iswing")) { const x = line(v3.internal.swings.map((w) => ({ time: w.ts, value: w.price })), "#56606c", 1, LW.LineStyle.Dotted);
        if (x) { t.v3lines.iswing++; t.v3series.internal = x; } }
      if (on("mevents")) v3.major.events.forEach((e) => {
        if (e.level == null || !e.src || e.src.ts == null || e.src.ts >= e.ts) return;
        if (line([{ time: e.src.ts, value: e.level }, { time: e.ts, value: e.level }], e.type.startsWith("CHoCH") ? css("--s2") : e.type.startsWith("BOS") ? css("--s-violet") : "#9aa4af",
          e.type.startsWith("CHoCH") ? 2 : 1, e.type.startsWith("SWEEP") ? LW.LineStyle.Dotted : LW.LineStyle.Solid)) t.v3lines.mevents++;
      });
      if (on("ievents")) v3.internal.events.filter((e) => !e.type.startsWith("iSWEEP")).forEach((e) => {
        if (e.src && e.src.ts < e.ts && line([{ time: e.src.ts, value: e.level }, { time: e.ts, value: e.level }], "#6e7783", 1, LW.LineStyle.Dotted)) t.v3lines.ievents++;
      });
      if (on("prot")) {
        const hist = v3.major.protected_history;
        hist.forEach((p, k) => {
          if (p.status !== "PROTECTED" && p.status !== "CANDIDATE") return;
          const end = hist.slice(k + 1).find((q) => q.side === p.side && q.src_idx === p.src_idx && q.status !== p.status) || hist.slice(k + 1).find((q) => q.side === p.side);
          const t1 = end ? end.set_ts - TF_DT[tf] : lastT;
          if (line([{ time: p.src_ts, value: p.price }, { time: Math.max(p.src_ts + 1, Math.min(t1, lastT)), value: p.price }], p.side === "H" ? css("--down") : css("--up"), 1,
            p.status === "CANDIDATE" ? LW.LineStyle.SparseDotted : LW.LineStyle.Dashed)) t.v3lines.prot++;
        });
        const ms = v3.major.state || {};
        if (ms.ph != null) pl(ms.ph, css("--down"), `Protected H ${ms.ph_status === "CANDIDATE" ? "(кандидат)" : "(major)"}`, LW.LineStyle.Dashed, 2);
        if (ms.pl != null) pl(ms.pl, css("--up"), `Protected L ${ms.pl_status === "CANDIDATE" ? "(кандидат)" : "(major)"}`, LW.LineStyle.Dashed, 2);
        const sn = v3.senior.state || {};
        if (sn.ph != null && sn.ph !== ms.ph) pl(sn.ph, "#a371f7", "Старший protected H", LW.LineStyle.Dotted);
        if (sn.pl != null && sn.pl !== ms.pl) pl(sn.pl, "#a371f7", "Старший protected L", LW.LineStyle.Dotted);
      }
      if (on("phase")) {
        const r = v3.range || {};
        if (r.structural_high) { pl(r.structural_high.price, css("--s4"), "Структурный max", LW.LineStyle.Dotted); t.v3lines.phase++; }
        if (r.structural_low) { pl(r.structural_low.price, css("--s4"), "Структурный min", LW.LineStyle.Dotted); t.v3lines.phase++; }
      }
    }
    const v2 = on("legacy") ? st.v2 : null;
    if (v2) {
      const seg = (e, color, width, style) => {
        if (e.pivot.ts >= e.ts) return;
        const x = t.chart.addSeries(LW.LineSeries, { color, lineWidth: width, lineStyle: style, priceLineVisible: false, lastValueVisible: false,
          crosshairMarkerVisible: false, pointMarkersVisible: false, priceFormat: { type: "price", precision: 6, minMove: 0.000001 } }, 0);
        x.setData([{ time: e.pivot.ts, value: e.level }, { time: e.ts, value: e.level }]);   // от пробитого pivot до свечи пробоя
        t.extra.push(x); t.structSegs.push(x);
      };
      v2.external.forEach((e) => seg(e, "#7d8590", 1, LW.LineStyle.Dotted));      // legacy v2 — бледно, для сравнения
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
      const pk = t.panes[DIV_PANE[dv.indicator]];
      if (pk != null) mk(pk, dv.p1.ind, dv.p2.ind);
    });
    t.markers.setMarkers(buildMarkers(tf));
  }

  const num6 = (x) => (x == null ? "—" : (x >= 0 ? "+" : "−") + Math.abs(x).toFixed(6));
  function rsiTxt(r) {
    if (r == null) return "RSI —";
    const z = r >= 70 ? ' <span class="neg">перекупленность</span>' : r <= 30 ? ' <span class="pos">перепроданность</span>' : "";
    return `<span style="color:var(--accent)">RSI ${fmt.num(r, 1)}</span>${z}`;
  }

  // ------------------------------------------------------------------ подсказки при наведении
  const PANE_KEY = (t, i) => (i === 0 ? "price" : Object.keys(t.panes).find((k) => t.panes[k] === i));

  function barValues(tf, time) {
    const t = T[tf], d = t.data;
    const lb = liveBars(d).find((b) => b.time === time);
    if (lb) {
      const lv = d.live?.values?.live, cl = d.live?.values?.closed;
      const isLast = lv && lv.time === time;
      return { bar: lb, status: lb.status, v: (n) => (isLast ? lv[n] : null), closed: (n) => cl?.[n], closedTime: cl?.time };
    }
    const k = t.byTime?.get(time);
    return k ? { bar: k, status: "CLOSED", v: (n) => t.ind?.[n]?.get(time), closed: null } : null;
  }

  function tipHtml(tf, time, pane) {
    const x = barValues(tf, time);
    if (!x) return "";
    const { bar, status, v } = x;
    const live = status !== "CLOSED";
    const val = (n, f) => {
      const a = v(n), cv = live && x.closed ? x.closed(n) : null;
      return `${f(a)}${live && cv != null ? ` <span class="muted">(закрытая ${f(cv)})</span>` : ""}`;
    };
    const p5 = (z) => fmt.price(z), n1 = (z) => fmt.num(z, 1), n3 = (z) => fmt.num(z, 3), n0 = (z) => fmt.num(z, 0);
    const sec = (key, title, rows) => `<div class="tsec ${pane === key ? "on" : ""}"><div class="th">${title}</div><table>${rows.map(([a, b]) => `<tr><td>${a}</td><td>${b}</td></tr>`).join("")}</table></div>`;
    const tag = { CLOSED: "закрытая свеча", FORMING: "FORMING — формирующаяся, меняется с ценой", CLOSING: "закрыта, ждёт свечу GeckoTerminal (предварительно)" }[status];
    const r = v("rsi");
    let h = `<div class="t">${fmt.dt(time)} · <span class="${live ? "warnline" : "muted"}">${tag}</span></div>`;
    h += sec("price", "Цена", [["O / H", `${p5(bar.open)} / ${p5(bar.high)}`], ["L / C", `${p5(bar.low)} / <b>${p5(bar.close)}</b>`],
      ["Объём", bar.volume == null ? "—" : fmt.big(bar.volume) + (live ? " (частичный)" : "")],
      ["EMA20 / EMA50", `${val("ema_fast", p5)} / ${val("ema_slow", p5)}`],
      ["BB верх / ср / низ", `${val("bb_upper", p5)} / ${val("bb_mid", p5)} / ${val("bb_lower", p5)}`]]);
    if (T[tf].panes.rsi) h += sec("rsi", "RSI(14)", [["RSI", val("rsi", n1) + (r == null ? "" : r >= 70 ? ' <span class="neg">перекупленность</span>' : r <= 30 ? ' <span class="pos">перепроданность</span>' : "")]]);
    h += sec("macd", "MACD(12,26,9)", [["MACD", val("macd", num6)], ["Signal", val("macd_signal", num6)], ["Histogram", val("macd_hist", num6) + ' <span class="muted">= MACD − Signal</span>']]);
    h += sec("mfi", "MFI(14)", [["MFI", val("mfi", n1)]]) + sec("cmf", "CMF(20)", [["CMF", val("cmf", n3)]]) + sec("obv", "OBV", [["OBV", val("obv", n0)]]);
    if (live && r == null && v("macd_hist") == null) h += `<div class="muted">Значения индикаторов этой свечи появятся после получения закрытой свечи GeckoTerminal.</div>`;
    else if (live) h += `<div class="muted">${status === "CLOSING" ? "Предварительные значения по котировкам — окончательные после свечи GeckoTerminal" :
      "LIVE-значения — по формирующейся свече"}; стратегиями не используются. В скобках — по последней закрытой свече.</div>`;
    const v3 = S3(tf);
    if (v3 && !v3.empty) {
      const tl = v3.timeline.filter((p) => p.ts <= time).at(-1);
      if (tl) h += `<div class="tsec"><div class="th">Структура v3 на этой свече</div><table>
        <tr><td>Major</td><td>${esc(tl.major)}${tl.bias ? "/" + esc(tl.bias) : ""} · ${esc(tl.phase)}${tl.conflict ? ' <span class="badge crit">SCALE_CONFLICT</span>' : ""}</td></tr>
        <tr><td>Internal</td><td>${esc(tl.internal)} · старший масштаб ${esc(tl.senior)}</td></tr></table></div>`;
      [...v3.major.events, ...v3.internal.events].filter((e) => e.ts === time).forEach((e) => {
        h += `<div class="tsec ${pane === "price" ? "on" : ""}"><div class="th">${e.scope === "internal" ? "Internal (minor)" : "MAJOR"}: ${esc(e.type)}${e.sweep ? " — тень, close вернулся (sweep)" : ""}${e.initial ? " (начальный)" : ""}${e.confirmation ? " (подтверждение)" : ""}</div><table>
          <tr><td>Уровень</td><td>${e.level != null ? fmt.price(e.level) : "—"}${e.src && e.src.ts ? ` · экстремум ${fmt.dt(e.src.ts)}` : ""}</td></tr>
          <tr><td>Триггер</td><td>${e.trigger ? esc(e.trigger) + " " + fmt.price(e.trigger_price) : "—"} · close ${fmt.price(e.close)}</td></tr>
          <tr><td>Свеча / известно</td><td>${fmt.dt(e.ts)} / ${fmt.dt(e.known_ts)}${e.break_fact_ts && e.break_fact_ts !== e.ts ? ` · первое касание ${fmt.dt(e.break_fact_ts)}` : ""}</td></tr>
          <tr><td>Было</td><td>${esc(e.prev || "")}</td></tr>
          <tr><td>Причина</td><td>${esc(e.why || "")}</td></tr>
          ${e.scale_conflict ? `<tr><td>SCALE_CONFLICT</td><td>${esc(e.scale_conflict.text)}</td></tr>` : ""}
          ${e.gap ? `<tr><td>Данные</td><td>свеча после пропуска</td></tr>` : ""}</table></div>`;
      });
      const onv = (k) => document.querySelector(`#t-${tf} [data-ov="${k}"]`)?.checked;
      [...v3.major.swings.filter((w) => (w.tier === "major" && onv("mswing")) || (w.tier === "intermediate" && onv("iswing_mid"))),
       ...v3.internal.swings.filter(() => onv("iswing"))].filter((w) => w.ts === time).forEach((w) => {
        const major = w.scope === "major" && w.tier === "major";
        h += `<div class="tsec ${pane === "price" ? "on" : ""}"><div class="th">Swing ${major ? "MAJOR" : w.scope === "major" ? "intermediate (major-ZigZag, внутри major-диапазона)" : "internal"}: ${w.kind} ${esc(major ? (w.major_label || "") : (w.label || ""))}</div><table>
          <tr><td>Цена</td><td>${fmt.price(w.price)}</td></tr><tr><td>Экстремум</td><td>${fmt.dt(w.ts)} <span class="muted">(отображается ретроспективно)</span></td></tr>
          <tr><td>Подтверждён ZigZag</td><td>${fmt.dt(w.confirmed_ts)}</td></tr>
          ${major ? `<tr><td>Доступен как major</td><td>${fmt.dt(w.available_ts)}</td></tr>` : ""}
          <tr><td>Роль</td><td>${(w.roles || []).map((r) => `${esc(r.role)} · с ${fmt.dt(r.assigned_ts)}`).join("<br>") || (w.tier === "intermediate" ? "внутри major-диапазона — без major-роли" : "—")}</td></tr>
          ${major && w.role_basis ? `<tr><td>Обоснование</td><td>${w.role_basis.map(esc).join("<br>")}</td></tr>` : ""}</table></div>`;
      });
    }
    const v2 = (T[tf].data?.trader?.analysis?.structure || T[tf].data?.preview?.structure || {}).v2;
    const evs = v2 && document.querySelector(`#t-${tf} [data-ov="legacy"]`)?.checked ? [...v2.external, ...v2.internal].filter((e) => e.ts === time) : [];
    h += evs.map((e) => `<div class="tsec ${pane === "price" ? "on" : ""}"><div class="th">${e.scope === "external" ? "Внешняя swing-структура v2 (исследование)" : "Внутренняя структура v1 (iBOS/iCHoCH, используется S2)"}: ${esc(e.type)}${e.initial ? " (начальный)" : ""}</div><table>
        <tr><td>Пробой</td><td>свеча ${fmt.dt(e.ts)} · close ${fmt.price(e.close)}</td></tr>
        <tr><td>Сигнал известен</td><td>${fmt.dt(e.known_ts)} (закрытие свечи пробоя)</td></tr>
        <tr><td>Пробитый уровень</td><td>${fmt.price(e.level)} — ${e.pivot.kind === "H" ? "swing high" : "swing low"}${e.pivot.label ? " " + esc(e.pivot.label) : ""} от ${fmt.dt(e.pivot.ts)}</td></tr>
        <tr><td>Подтверждение pivot</td><td>${e.pivot.label === "protected" ? "protected установлен" : "свеча"} ${fmt.dt(e.pivot.confirmed_ts)} · известно ${fmt.dt(e.pivot.known_ts)}</td></tr>
        <tr><td>Предыдущая структура</td><td>${{ up: "вверх", down: "вниз", none: "не определена" }[e.prev_trend]}</td></tr>
        ${e.protected_after != null ? `<tr><td>Protected после</td><td>${fmt.price(e.protected_after)}</td></tr>` : ""}</table></div>`).join("");
    const ops = (T[tf].data?.trader?.ops || []).filter((o) => o.ts === time);
    h += ops.map((o) => `<div class="tsec"><div class="th"><span class="side-b ${o.side === "BUY" ? "buy" : "sell"}">${o.side}</span> · ${fmt.dt(o.ts)}</div><table>
        <tr><td>Цена исп.</td><td>${fmt.price(Number(o.price))} (mid ${fmt.price(Number(o.mid))})</td></tr>
        <tr><td>Количество</td><td>${fmt.qty(Number(o.qty))} FABLES · ${fmt.usd(Number(o.usd))}</td></tr>
        <tr><td>Издержки</td><td>fee ${fmt.usd(Number(o.fee_usd), 3)} · impact ${fmt.usd(Number(o.impact_usd), 3)} · slip ${fmt.usd(Number(o.slippage_usd), 3)} · gas ${fmt.usd(Number(o.gas_usd), 3)}</td></tr>
        ${o.realized_pnl != null ? `<tr><td>PnL</td><td class="${fmt.cls(Number(o.realized_pnl))}">${fmt.sUsd(Number(o.realized_pnl))}</td></tr>` : ""}
        <tr><td>Причина</td><td>${esc(o.reason)}</td></tr></table></div>`).join("");
    return h;
  }

  function onCrosshair(tf, p) {
    const t = T[tf], leg = $(tf, "legend"), tip = $(tf, "tip"), info = $(tf, "barinfo");
    const d = t.data;
    if (!d) return;
    const tm = p?.time;
    t.hovering = tm != null;
    const lb = liveBars(d);
    const lastTime = lb.length ? lb.at(-1).time : d.candles.at(-1)?.time;
    const time = tm != null ? tm : lastTime;
    const x = time != null ? barValues(tf, time) : null;
    if (x) {
      const c = x.bar;
      leg.innerHTML = `${fmt.time(time)} ${x.status !== "CLOSED" ? '<span class="warnline">' + x.status + "</span> " : ""}O ${fmt.price(c.open)}  H ${fmt.price(c.high)}  L ${fmt.price(c.low)}  C <span class="${c.close >= c.open ? "pos" : "neg"}">${fmt.price(c.close)}</span>
        V ${c.volume == null ? "—" : fmt.big(c.volume)} · ${rsiTxt(x.v("rsi"))} · Hist ${num6(x.v("macd_hist"))}`;
      placeLabels(tf);
    }
    const html = time != null ? tipHtml(tf, time, p && p.paneIndex != null ? PANE_KEY(t, p.paneIndex) : null) : "";
    if (MOBILE()) {                                   // мобильный: фиксированный блок значений выбранного бара
      tip.style.display = "none";
      info.style.display = html ? "block" : "none";
      info.innerHTML = html ? `<div class="muted" style="margin-bottom:4px">${tm != null ? "Выбранный бар" : "Последний бар — коснитесь графика, чтобы выбрать другой"}</div>${html}` : "";
      return;
    }
    info.style.display = "none";
    if (tm == null || !p.point || !html) { tip.style.display = "none"; return; }
    tip.innerHTML = html;
    tip.style.display = "block";
    const box = $(tf, "chart").getBoundingClientRect();
    const w = tip.offsetWidth, hgt = tip.offsetHeight;
    tip.style.left = (p.point.x + 18 + w > box.width ? Math.max(4, p.point.x - w - 18) : p.point.x + 18) + "px";
    tip.style.top = Math.max(4, Math.min(p.point.y - 20, box.height - hgt - 4)) + "px";
  }

  // ------------------------------------------------------------------ метрики, позиция, журнал
  function metric(k, v, s = "", cls = "") {
    return `<div class="metric"><div class="k" title="${esc(k)}">${esc(k)}</div><div class="v ${cls}">${v}</div><div class="s">${s || "&nbsp;"}</div></div>`;
  }

  function monitorInfo(tf) {
    const m = T[tf].data?.trader?.monitor;
    if (!m) return "";
    const pc = { FULL: "pos", DEGRADED: "warnline", NONE: "neg" }[m.protection] || "";
    const sc = m.last_stop_check;
    return `последнее решение ${m.last_decision_ts ? fmt.time(m.last_decision_ts) : "— (свеча ещё не закрылась)"} · стоп проверен ${sc ? fmt.time(sc.ts) + " (" + Lab.age(Math.round(Date.now() / 1000 - sc.ts)) + ")" : "—"}` +
      (m.position ? ` · защита <b class="${pc}">${esc(m.protection || "—")}</b>` : "");
  }

  function closeInfo(tf) {
    const d = T[tf].data;
    if (d.mode !== "FORWARD" || !d.next_close) return "";
    const left = Math.max(0, d.next_close - Date.now() / 1000);
    const h = Math.floor(left / 3600), m = Math.floor((left % 3600) / 60), sx = Math.floor(left % 60);
    return `<span title="свеча обрабатывается после получения из GeckoTerminal (≈ +3 мин)">следующее закрытие ${fmt.time(d.next_close)} UTC · через <b class="mono">${h ? h + " ч " : ""}${m} мин ${sx} с</b></span>`;
  }

  function renderHead(tf) {
    const d = T[tf].data, tr = d.trader;
    if (!tr) {
      $(tf, "strat").textContent = "";
      $(tf, "state").innerHTML = `<span class="badge">FORWARD: ${Lab.STATUS_RU[d.forward.status]}</span>`;
      $(tf, "sample").innerHTML = "";
      $(tf, "asof").innerHTML = closeInfo(tf);
      $(tf, "metrics").innerHTML = metric("Режим", "FORWARD", "") + metric("Статус", Lab.STATUS_RU[d.forward.status]) +
        metric("Закрытых свечей", String(d.candles.length), d.candles.length ? `последняя ${fmt.time(d.candles.at(-1).time)}` : "бот ещё не загрузил") +
        `<div class="metric" style="grid-column: span 10"><div class="k">Пояснение</div><div class="s" style="white-space:normal">${
          !d.worker?.alive ? "Фоновый бот не запущен: живые данные не собираются. Запустите start.bat." :
          d.forward.status === "NOT_STARTED" ? "Бот собирает живые данные. Forward paper trading не запущен — нажмите START, чтобы три трейдера начали торговать с этого момента." :
          "Нет данных трейдера."}</div></div>`;
      return;
    }
    const sm = tr.summary;
    $(tf, "strat").textContent = `${tr.strategy.id} v${tr.strategy.version} · ${tr.mode}`;
    const stc = { LONG: "good", ENTRY_PENDING: "info", EXIT_PENDING: "warn", RISK_OFF: "crit", DATA_ERROR: "crit", PAUSED: "warn", WARMUP: "warn" }[tr.state] || "";
    $(tf, "state").innerHTML = `<span class="badge ${stc}">${esc(tr.state)}</span>`;
    $(tf, "sample").innerHTML = `<span class="badge warn" title="Нужно ≥ 30 завершённых сделок для статистических выводов">${esc(sm.sample)}</span>
      <span class="badge">${esc(sm.validation)}</span>`;
    $(tf, "asof").innerHTML = (d.mode === "FORWARD" ? monitorInfo(tf) + " · " + closeInfo(tf) + " · " : "") + esc(sm.mark ? `оценка по ${fmt.price(sm.mark.mid)} @ ${fmt.time(sm.mark.ts)} · ${d.mode === "FORWARD" ? (tr.diagnostics.exec_model || "") : sm.cost_model}`
      : `${sm.mark_note || "проверенной котировки для оценки нет"}`);
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
    if (pend) h += `<div class="warnline" style="margin-top:6px">Отложенный ордер: ${esc(pend.kind)} — исполнится по первой проверенной котировке после решения. ${esc(pend.reason || "")}</div>`;
    const mon = tr.monitor;
    if (tr.mode === "FORWARD" && p && mon) {
      const txt = { FULL: "ПОЛНАЯ — стоп по проверенной ончейн-котировке (StateView)",
        DEGRADED: "ОСЛАБЛЕНА — StateView недоступен: стоп только после 2 котировок DexScreener ниже уровня, цели не исполняются",
        NONE: "НЕТ — котировки недостоверны: стоп не может быть исполнен, позиция не защищена" }[mon.protection] || "—";
      h += `<div class="${{ FULL: "sec", DEGRADED: "warnline", NONE: "neg" }[mon.protection] || "sec"}" style="margin-top:6px">Защита: ${esc(txt)}${
        mon.last_stop_check ? `<br>последняя проверка стопа ${fmt.dt(mon.last_stop_check.ts)} по ${fmt.price(mon.last_stop_check.price)} (${esc(mon.last_stop_check.source || "")})` : ""}</div>`;
    }
    if (tr.mode === "FORWARD" && p && !(pend && pend.kind === "EXIT"))
      h += `<div style="margin-top:8px"><button class="btn danger" data-close="${tf}">CLOSE POSITION</button></div>`;
    el.innerHTML = h;
    el.querySelector("[data-close]")?.addEventListener("click", () => Lab.command("close", tf));
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
          (fee ${fmt.usd(Number(o.fee_usd), 3)}${o.fee_ppm ? " = " + fmt.num(o.fee_ppm / 1e4, 2) + "%" : ""} · impact ${fmt.usd(Number(o.impact_usd), 4)} · slip ${fmt.usd(Number(o.slippage_usd), 3)} · gas ${fmt.usd(Number(o.gas_usd), 4)})
          ${o.realized_pnl != null ? ` · PnL <span class="${fmt.cls(Number(o.realized_pnl))}">${fmt.sUsd(Number(o.realized_pnl))}</span>` : ""} · ${esc(o.position_id)}</div>
        ${o.signal_ts ? `<div class="l3">сигнал ${fmt.time(o.signal_ts)} · получен ${fmt.time(o.received_ts)} · решение ${fmt.time(o.decided_ts)} · исполнение ${fmt.time(o.exec_ts)}
          · ожидалось ${fmt.price(o.expected_price)} → mid ${fmt.price(Number(o.mid))} · ${esc(o.quote_source || "")}</div>` : ""}
        <div class="l3">модель: ${esc(o.exec_model || o.cost_model)} — оценка, не подтверждённый своп${o.exec_note ? " · " + esc(o.exec_note) : ""}</div></div>`).join("")
      : `<div class="empty">${tr.ops.length ? "Нет операций под выбранные фильтры." : !tr.decisions.length ?
          "Решений ещё нет: после START ещё не закрылась ни одна новая свеча этого таймфрейма." :
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

  function indTable(an) {
    const live = an.indicators.some((r) => r.live !== undefined);
    return `<table class="t"><tr><th>Индикатор</th><th>Закрытая свеча</th>${live ? "<th>LIVE (формирующаяся)</th>" : ""}<th>Мин. баров</th><th>Статус</th><th>Интерпретация</th></tr>
      ${an.indicators.map((r) => `<tr><td>${esc(r.name)}</td><td class="n">${esc(r.display)}</td>${live ? `<td class="n warnline">${r.live == null ? "—" : esc(r.live_display)}</td>` : ""}
        <td class="n">${r.min_bars ?? "—"}</td>
        <td><span class="badge ${r.status === "OK" ? "good" : "warn"}">${esc(r.status)}</span></td><td class="sec">${esc(r.text)}</td></tr>`).join("")}</table>
      <div class="muted" style="margin-top:6px">Закрытая свеча — значения, по которым решают стратегии (те же ряды, что на графике и в API).
      ${live ? "LIVE — по формирующейся свече (open периода, high/low с начала периода, close — текущая котировка); меняется с ценой, стратегиями не используется." : ""}</div>`;
  }

  async function renderAudit(tf) {
    const sec = document.querySelector(`#t-${tf}`), body = sec.querySelector('[data-body="audit"]');
    body.innerHTML = `<div class="muted">Загрузка аудита…</div>`;
    let r;
    try { r = await api(`/api/audit/${tf}?mode=${mode}`); } catch (e) { body.innerHTML = `<div class="neg">${esc(e.message)}</div>`; return; }
    if (r.error) { body.innerHTML = `<div class="empty">${esc(r.error)}</div>`; return; }
    const N = { rsi: "RSI(14)", macd: "MACD", macd_signal: "MACD Signal", macd_hist: "MACD Histogram", mfi: "MFI(14)", cmf: "CMF(20)", obv: "OBV",
      ema_fast: "EMA быстрая", ema_slow: "EMA медленная", bb_upper: "BB верхняя", bb_mid: "BB средняя", bb_lower: "BB нижняя", atr: "ATR(14)",
      adx: "ADX", pdi: "+DI", ndi: "−DI" };
    const st = { OK: "good", METHOD: "warn", WARMUP: "", "BUG?": "crit" };
    const sel = T[tf].auditKey || "rsi";
    const sum = Object.entries(r.summary).map(([k, v]) => `<tr class="click" data-k="${k}"><td>${N[k]}</td><td><span class="badge ${st[v.status]}">${v.status}</span></td>
      <td class="n">${v.max_abs_diff.toExponential(2)}</td><td class="n">${(v.max_rel_diff * 100).toFixed(4)}%</td></tr>`).join("");
    const g = (x, k) => x == null ? "—" : ["obv"].includes(k) ? fmt.num(x, 0) : Math.abs(x) < 0.01 ? x.toPrecision(6) : fmt.num(x, 4);
    const rows = r.tables[sel].map((x) => `<tr><td>${x.ts ? fmt.dt(x.ts) : ""}</td><td class="n">${x.close == null ? "LIVE" : fmt.price(x.close)}</td>
      <td class="n">${g(x.ours, sel)}</td><td class="n">${g(x.reference, sel)}</td><td class="n">${x.diff == null ? "—" : x.diff.toExponential(2)}</td>
      ${sel === "rsi" ? `<td class="n">${g(x.rsi_sma, sel)}</td><td class="n">${g(x.rsi_ema, sel)}</td>` : ""}<td class="sec">${esc(x.note)}</td></tr>`).join("");
    body.innerHTML = `<div class="grid2"><div class="box">
        <h4>Сводка: ядро vs эталон (семантика TradingView / Pine — как на графике DexScreener)</h4>
        <table class="t"><tr><th>Индикатор</th><th>Статус</th><th>max |diff|</th><th>max отн.</th></tr>${sum}</table>
        <div class="muted" style="margin-top:6px">OK — совпадает; METHOD — различие методик (засев EMA/ATR), не ошибка; WARMUP — ядро ещё не показывает индикатор;
        BUG? — необъяснённое расхождение. Источник: ${esc(r.source)}. ${esc(r.timestamp_convention)}. ${esc(r.value_convention)}.</div></div>
      <div class="box"><h4>Почему RSI DexScreener может отличаться от нашего</h4><ul class="plain">
        <li>Формула та же: RSI Уайлдера (RMA) — как стандартный RSI TradingView. «SMA 14» в легенде DexScreener — параметры <i>скользящей средней от RSI</i>, а не сглаживания RSI.
          SMA-вариант RSI (Cutler) дал бы совсем другие значения — см. колонку «RSI SMA».</li>
        <li>Наш график, легенда, таблица и API показывают RSI <b>закрытой</b> свечи. DexScreener показывает RSI <b>формирующейся</b> свечи — он меняется с ценой; сравнивайте со строкой LIVE.</li>
        <li>Свечи DexScreener строятся из его собственного индекса сделок; у нас — свечи GeckoTerminal. Небольшая разница цен закрытия даёт разницу RSI.</li>
        <li>DexScreener подписывает свечи временем начала в часовом поясе браузера; у нас — UTC.</li></ul></div></div>
      <h4 style="margin-top:10px">${N[sel]}: последние 20 закрытых баров (+ LIVE)</h4>
      <table class="t"><tr><th>timestamp (UTC, начало)</th><th>close</th><th>ours (ядро)</th><th>reference (Pine)</th><th>diff</th>
        ${sel === "rsi" ? "<th>RSI SMA (Cutler)</th><th>RSI EMA</th>" : ""}<th>note</th></tr>${rows}</table>`;
    body.querySelectorAll("tr[data-k]").forEach((tr) => tr.addEventListener("click", () => { T[tf].auditKey = tr.dataset.k; renderAudit(tf); }));
  }

  function condTable(list, title) {
    if (!list || !list.length) return "";
    return `<h4 style="margin-top:10px">${esc(title)}</h4><table class="t"><tr><th>Условие</th><th>Значение</th><th>Требование</th><th>Результат</th></tr>
      ${list.map((c) => { const r = c.result || (c.ok === true ? "PASS" : c.ok === false ? "FAIL" : "N/A");
        return `<tr><td>${esc(c.name)}</td><td class="mono">${esc(c.value ?? c.detail ?? "")}</td><td class="mono sec">${esc(c.req ?? "")}</td>
          <td><span class="badge ${r === "PASS" ? "good" : r === "FAIL" ? "crit" : ""}">${r}</span></td></tr>`; }).join("")}</table>`;
  }

  function rejTable(rj) {
    if (!rj || !rj.rejected) return `<div class="sec">Отклонённых решений пока нет.</div>`;
    const rows = (arr) => arr.slice(0, 8).map((x) => `<tr><td>${esc(x.name)}</td><td class="n">${x.count}</td><td class="n">${fmt.pct(x.pct, 1)}</td></tr>`).join("");
    return `<div class="sec" style="margin-bottom:6px">Решений: ${rj.decisions}, без входа: ${rj.rejected} · ${Object.entries(rj.by_action).map(([k, v]) => `${esc(k)}: ${v}`).join(" · ")}</div>
      <div class="grid2"><div><h4>Чаще всего не выполняется (любое условие)</h4><table class="t"><tr><th>Условие</th><th>Раз</th><th>% решений</th></tr>${rows(rj.any_fail)}</table></div>
      <div><h4>Первый блокер</h4><table class="t"><tr><th>Условие</th><th>Раз</th><th>%</th></tr>${rows(rj.first_blocker)}</table>
      ${rj.risk_fail.length ? `<h4 style="margin-top:8px">Риск-фильтры</h4><table class="t"><tr><th>Фильтр</th><th>Раз</th><th>%</th></tr>${rows(rj.risk_fail)}</table>` : ""}</div></div>`;
  }

  const S3 = (tf) => T[tf].data?.structure_v3 || null;
  const S3LAB = (x) => x.replace("_UP", "↑").replace("_DOWN", "↓").replace("SWEEP_HIGH", "sweep↑").replace("SWEEP_LOW", "sweep↓");
  const TREND_RU = { UP: "UP", DOWN: "DOWN", TRANSITION: "TRANSITION", RANGE: "RANGE", UNDEFINED: "UNDEFINED" };
  function majorLabel(st, conflict) {
    if (!st) return "UNDEFINED";
    let x = TREND_RU[st.trend] + (st.bias ? ` (уклон ${st.bias === "up" ? "вверх" : "вниз"})` : "");
    if (conflict) x += " · SCALE_CONFLICT";
    return x;
  }
  function v3Html(tf) {
    const v = S3(tf);
    if (!v || v.empty) return `<div class="box" style="margin-bottom:12px"><h4>Структура v3</h4><div class="sec">нет закрытых свечей</div></div>`;
    const m = v.major, ms = m.state || {}, it = v.internal.state, sn = v.senior.state || {};
    const ev = (e) => e ? `${esc(e.type)} ${e.level != null ? fmt.price(e.level) : ""} · ${fmt.dt(e.ts)} (известно ${fmt.time(e.known_ts)})` : "—";
    const roles = (w) => (w.roles || []).map((r) => `${esc(r.role)} <span class="muted">${r.assigned_ts ? fmt.dt(r.assigned_ts) : ""}</span>`).join("<br>") || "—";
    const interp = ms.trend && ms.trend !== it && ["UP", "DOWN"].includes(ms.trend) && ["UP", "DOWN"].includes(it)
      ? (ms.trend === "DOWN" ? "Major DOWN + Internal UP — восстановление внутри нисходящей структуры" : "Major UP + Internal DOWN — коррекция внутри восходящей структуры") : "";
    const swRows = (xs) => xs.slice().reverse().map((w) => `<tr><td><b>${w.kind}</b> ${esc(w.label || "")}</td><td class="n">${fmt.price(w.price)}</td>
        <td>${fmt.dt(w.ts)}</td><td>${fmt.dt(w.confirmed_ts)}</td><td>${esc(w.tier)}</td><td>${roles(w)}</td></tr>`).join("") || "<tr><td colspan=6 class='muted'>нет</td></tr>";
    const evRows = (xs) => xs.slice().reverse().map((e) => `<tr><td><b class="${/UP|HIGH/.test(e.type) ? "pos" : /DOWN|LOW/.test(e.type) ? "neg" : ""}">${esc(e.type)}</b>
        ${e.sweep ? ' <span class="badge warn">sweep</span>' : ""}${e.initial ? ' <span class="badge">начальный</span>' : ""}${e.confirmation ? ' <span class="badge good">подтверждение</span>' : ""}
        ${e.scale_conflict ? ' <span class="badge crit">SCALE_CONFLICT</span>' : ""}${e.gap ? ' <span class="badge warn">после пропуска</span>' : ""}</td>
        <td>${fmt.dt(e.ts)}<br><span class="muted">известно ${fmt.dt(e.known_ts)}</span></td><td class="n">${e.level != null ? fmt.price(e.level) : "—"}</td>
        <td class="n">${e.trigger ? esc(e.trigger) + " " + fmt.price(e.trigger_price) : "—"}<br>close ${fmt.price(e.close)}</td>
        <td>${e.src && e.src.ts ? fmt.dt(e.src.ts) : "—"}</td><td>${esc(e.prev || "")}</td><td class="sec">${esc(e.why || "")}${e.levels ? "<br>" + esc(JSON.stringify(e.levels)) : ""}</td></tr>`).join("") || "<tr><td colspan=7 class='muted'>нет</td></tr>";
    const evHead = "<tr><th>Событие</th><th>Свеча / известно</th><th>Уровень</th><th>Триггер / close</th><th>Источник уровня</th><th>Было</th><th>Причина</th></tr>";
    const prRows = m.protected_history.slice().reverse().map((p) => `<tr><td>${p.side === "H" ? "high" : "low"}</td><td class="n">${fmt.price(p.price)}</td>
        <td>${fmt.dt(p.src_ts)}</td><td>${fmt.dt(p.set_ts)}</td><td><span class="badge ${p.status === "PROTECTED" ? "good" : p.status === "VIOLATED" ? "crit" : "warn"}">${esc(p.status)}</span></td>
        <td class="sec">${esc(p.reason)}${p.replaced != null ? ` (заменил ${fmt.price(p.replaced)})` : ""}</td></tr>`).join("") || "<tr><td colspan=6 class='muted'>нет</td></tr>";
    const cf = v.scale_conflict_now;
    return `<div class="box" data-v3="panel" style="margin-bottom:12px"><h4>Структура v3 (исследование; стратегиями не используется) · k major ${v.k.major} · internal ${v.k.internal} · старший масштаб ${v.k.senior}</h4>
      <div class="kv">
        <span class="k">Major Trend</span><span class="v"><b>${esc(v.history_status === "OK" ? majorLabel(ms, cf) : v.history_status + " — " + majorLabel(ms, cf) + " (не подтверждено)")}</b></span>
        <span class="k">История</span><span class="v">${esc(v.history === "launch" ? "с запуска торгов" : "усечённая")} · ${esc(v.history_status)} · живая свеча: не обрабатывается (L2)</span>
        <span class="k">Internal Trend</span><span class="v">${esc(it)}</span>
        <span class="k">Market Phase</span><span class="v">${esc(ms.phase || "—")}</span>
        <span class="k">Protected High</span><span class="v">${ms.ph != null ? fmt.price(ms.ph) + " · " + esc(ms.ph_status) : "—"}</span>
        <span class="k">Protected Low</span><span class="v">${ms.pl != null ? fmt.price(ms.pl) + " · " + esc(ms.pl_status) : "—"}</span>
        <span class="k">Старший масштаб</span><span class="v">${esc(sn.trend || "—")}${sn.ph != null ? " · protected high " + fmt.price(sn.ph) : ""}${sn.pl != null ? " · protected low " + fmt.price(sn.pl) : ""}</span>
        <span class="k">Старший ТФ</span><span class="v">${v.htf ? esc(v.htf.text) : "нет"}</span>
        <span class="k">Last Major BOS / CHoCH</span><span class="v">${ev(v.last_major_bos)}<br>${ev(v.last_major_choch)}</span>
        <span class="k">Last Internal iBOS / iCHoCH</span><span class="v">${ev(v.last_internal_bos)}<br>${ev(v.last_internal_choch)}</span>
      </div>
      ${interp ? `<div class="sec" style="margin-top:6px">${esc(interp)}</div>` : ""}
      ${cf ? `<div class="warnline" data-v3="conflict" style="margin-top:6px">SCALE_CONFLICT: ${esc(cf.text)}. Локальный CHoCH/BOS не считается подтверждённым разворотом внешней структуры; конфликт не разрешается автоматически.</div>` : ""}
      <h4 style="margin-top:8px">Условия изменения основного режима</h4><ul class="plain">${(v.conditions || []).map((x) => `<li>${esc(x)}</li>`).join("") || "<li>—</li>"}</ul>
      ${v.gaps.length ? `<div class="warnline">Пропуски свечей: ${v.gaps.length} (события на свечах после пропуска помечены)</div>` : ""}
      ${v.insufficient_history ? `<div class="warnline">Недостаточно истории для ATR(14): структура не определяется</div>` : ""}
      <div class="muted" style="margin-top:6px">${esc(v.note)}</div></div>
      <div class="box" style="margin-bottom:12px"><h4>1. Major swings (ZigZag ${v.k.major}·ATR; tier: major — получил структурную роль, intermediate — внутри major-диапазона)</h4>
        <table class="t" data-v3="t-major-swings"><tr><th>Swing</th><th>Цена</th><th>Экстремум</th><th>Подтверждён (известно)</th><th>Уровень</th><th>Роль · назначена</th></tr>${swRows(m.swings)}</table></div>
      <div class="box" style="margin-bottom:12px"><h4>2. Internal swings (ZigZag ${v.k.internal}·ATR)</h4>
        <table class="t" data-v3="t-internal-swings"><tr><th>Swing</th><th>Цена</th><th>Экстремум</th><th>Подтверждён (известно)</th><th>Уровень</th><th>Роль</th></tr>${swRows(v.internal.swings)}</table></div>
      <div class="box" style="margin-bottom:12px"><h4>3. Major BOS / CHoCH / SWEEP / AMBIGUOUS</h4><table class="t" data-v3="t-major-events">${evHead}${evRows(m.events)}</table>
        ${v.scale_conflicts.length ? `<div class="sec" style="margin-top:6px">История конфликтов масштаба: ${v.scale_conflicts.map((c) => `${esc(c.type)} ${fmt.dt(c.ts)}${c.text ? " — " + esc(c.text) : ""}`).join("; ")}</div>` : ""}</div>
      <div class="box" style="margin-bottom:12px"><h4>4. Internal iBOS / iCHoCH</h4><table class="t" data-v3="t-internal-events">${evHead}${evRows(v.internal.events)}</table></div>
      <div class="box" style="margin-bottom:12px"><h4>5. Protected Levels History (major)</h4><table class="t" data-v3="t-protected"><tr><th>Сторона</th><th>Уровень</th><th>Экстремум</th><th>Назначен / изменён</th><th>Статус</th><th>Причина</th></tr>${prRows}</table></div>`;
  }

  function renderAnalysis(tf) {
    const d = T[tf].data, tr = d.trader, sec = document.querySelector(`#t-${tf}`);
    const body = (k) => sec.querySelector(`[data-body="${k}"]`);
    if (!tr) {
      const msg = `<div class="empty">FORWARD: ${Lab.STATUS_RU[d.forward.status]}. Решения трейдера появятся после START на новых закрытых свечах.
        ${d.preview ? "Ниже — живой рыночный анализ последней закрытой свечи (не решение трейдера)." : "Живых свечей ещё нет — бот не запущен или не загрузил историю."}</div>`;
      TABS.forEach(([k]) => (body(k).innerHTML = msg));
      if (d.preview) body("ind").innerHTML = msg + indTable(d.preview);
      body("struct").innerHTML = v3Html(tf) + msg;
      body("onchain").innerHTML = onchain();
      return;
    }
    const dec = tr.decision, an = tr.analysis, st = an.structure || {};
    if (!dec) {
      const msg = `<div class="empty">${esc(tr.mode)}: решений ещё нет — после START ещё не закрылась ни одна новая свеча этого таймфрейма
        (${closeInfo(tf) || "ожидание закрытия"}). Рыночные индикаторы ниже — на последней закрытой живой свече; это анализ рынка, а не решение трейдера.</div>`;
      TABS.forEach(([k]) => (body(k).innerHTML = msg));
      body("ind").innerHTML = msg + indTable(an);
      body("struct").innerHTML = v3Html(tf) + msg;
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
        ${condTable(dec?.conditions, "Диагностика условий стратегии")}
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
        ${condTable(dec?.risk || tr.risk.checks, "Риск-фильтры")}
        ${tr.rejections ? `<h4 style="margin-top:10px">Что чаще всего мешает входу</h4><div class="sec">${(tr.rejections.any_fail || []).slice(0, 3)
          .map((x) => `${esc(x.name)} — ${fmt.pct(x.pct, 0)} решений`).join("<br>") || "—"}</div>` : ""}
        <h4 style="margin-top:10px">Полное объяснение решения</h4><pre class="expl">${esc(dec?.explanation || "")}</pre>
      </div></div>`;

    body("ind").innerHTML = `<table class="t"><tr><th>Индикатор</th><th>Значение</th><th>Мин. баров</th><th>Статус</th><th>Интерпретация</th></tr>
      ${an.indicators.map((r) => `<tr><td>${esc(r.name)}</td><td class="n">${esc(r.display)}</td><td class="n">${r.min_bars ?? "—"}</td>
        <td><span class="badge ${r.status === "OK" ? "good" : "warn"}">${esc(r.status)}</span></td><td class="sec">${esc(r.text)}</td></tr>`).join("")}</table>
      <div class="muted" style="margin-top:6px">Значения на закрытии ${fmt.dt(an.as_of_ts)}. Индикатор без достаточной истории не рассчитывается и не используется стратегией.</div>`;

    const divs = (st.divergences || []).slice().reverse();
    const v2 = st.v2;
    const evRows = v2 ? [...v2.external, ...v2.internal].sort((x, y) => y.ts - x.ts || (x.scope === "external" ? -1 : 1)).slice(0, 40)
      .map((e) => `<tr><td><b class="${e.type.endsWith("UP") ? "pos" : "neg"}">${esc(e.type)}</b>${e.initial ? " (нач.)" : ""}</td><td>${e.scope === "external" ? "внешняя v2" : "внутренняя v1"}</td>
        <td>${fmt.dt(e.ts)}<br><span class="muted">известно ${fmt.time(e.known_ts)}</span></td><td>${e.pivot.kind}${e.pivot.label ? " " + esc(e.pivot.label) : ""} @ ${fmt.time(e.pivot.ts)}</td><td class="n">${fmt.price(e.level)}</td>
        <td class="n">${fmt.price(e.close)}</td><td>${fmt.time(e.pivot.confirmed_ts)}</td><td>${{ up: "вверх", down: "вниз", none: "—" }[e.prev_trend]}</td></tr>`).join("") : "";
    const v2Html = v2 ? `<div class="box" style="margin-bottom:12px"><h4>События структуры: BOS/CHoCH — внешняя swing v2 (ATR-zigzag, k=${v2.k_atr} — исследовательский параметр); iBOS/iCHoCH — внутренняя v1 (используется S2)</h4>
      <div class="sec" style="margin-bottom:6px">Внешний тренд: <b>${{ up: "вверх", down: "вниз", none: "не определён" }[v2.trend]}</b>${v2.protected != null ? ` · protected ${v2.trend === "up" ? "low" : "high"} ${fmt.price(v2.protected)}${v2.protected_src ? " (" + fmt.dt(v2.protected_src.ts) + ")" : ""}` : ""}.
        ${esc(v2.note)}</div>
      <table class="t"><tr><th>Тип</th><th>Масштаб</th><th>Пробой (close)</th><th>Пробитый pivot</th><th>Уровень</th><th>Close</th><th>Pivot подтверждён</th><th>Пред. структура</th></tr>${evRows || "<tr><td colspan=8 class='muted'>нет</td></tr>"}</table></div>` : "";
    body("struct").innerHTML = v3Html(tf) + `<details class="box" style="margin-bottom:12px"><summary>Legacy: v2 (внешняя swing) и v1 (iBOS/iCHoCH pivots 3/3, используется S2) — для сравнения</summary>${v2Html}</details>` + `<div class="grid2">
      <div class="box"><h4>Legacy v1: pivots 3/3</h4><table class="t"><tr><th>Метка</th><th>Время</th><th>Цена</th><th>Подтверждён</th></tr>
        ${(st.swings || []).slice().reverse().map((x) => `<tr><td>${x.label}</td><td>${fmt.time(x.ts)}</td><td class="n">${fmt.price(x.price)}</td><td class="n">бар ${x.confirmed_idx}</td></tr>`).join("") || "<tr><td colspan=4 class='muted'>нет</td></tr>"}</table>
        <h4 style="margin-top:10px">События legacy v1 (pivots 3/3; используется S2)</h4><table class="t"><tr><th>Тип</th><th>Время</th><th>Уровень</th></tr>
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
    body("dec").innerHTML = `<h4>Статистика причин отклонённых входов</h4>${rejTable(tr.rejections || rejLocal(tr))}
      <h4 style="margin-top:12px">История решений</h4>
      <div class="sec" style="margin-bottom:8px">Всего решений: ${tr.decisions.length} · ${Object.entries(counts).map(([k, v]) => `${k}: ${v}`).join(" · ")}<br>
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
    const L = overview?.live;
    const liveDs = mode === "FORWARD" && L?.ds;
    const m = liveDs ? L.ds : overview?.market;
    const p = liveDs ? { ts: L.ds.ts, usd: L.ds.price_usd, usdg: L.ds.mid } : overview?.price;
    if (!m) return `<div class="empty">Снимка рынка нет.</div>`;
    const tx = m.txns || {};
    const row = (k) => tx[k] ? `<tr><td>${k}</td><td class="n">${tx[k].buys}</td><td class="n">${tx[k].sells}</td><td class="n">${tx[k].buys ? fmt.num(tx[k].sells / tx[k].buys, 2) : "—"}</td></tr>` : "";
    return `<div class="grid2">
      <div class="box"><h4>Срез пула (DexScreener, ${fmt.dt(p.ts)}) — ${liveDs ? "ЖИВОЙ, " + esc(Lab.age(L.ds_age)) : "РЕАЛЬНОЕ ИЗМЕРЕНИЕ, не история"}</h4><div class="kv">
        <span class="k">Цена USD / USDG</span><span class="v">${fmt.price(p.usd)} / ${fmt.price(p.usdg)}</span>
        <span class="k">Ликвидность</span><span class="v">${fmt.big(m.liquidity_usd)}</span>
        <span class="k">FDV / Market cap</span><span class="v">${fmt.big(m.fdv)} / ${fmt.big(m.market_cap)}</span>
        <span class="k">Объём 1ч / 6ч / 24ч</span><span class="v">${fmt.big(m.volume?.h1)} / ${fmt.big(m.volume?.h6)} / ${fmt.big(m.volume?.h24)}</span>
        <span class="k">Объём / ликвидность 24ч</span><span class="v">${m.liquidity_usd ? fmt.num(m.volume.h24 / m.liquidity_usd, 2) + "×" : "—"}</span>
        <span class="k">Δ цены 1ч / 6ч / 24ч</span><span class="v">${fmt.sPct(m.price_change?.h1)} / ${fmt.sPct(m.price_change?.h6)} / ${fmt.sPct(m.price_change?.h24)}</span></div>
        <h4 style="margin-top:10px">Сделки: покупки / продажи</h4><table class="t"><tr><th>Окно</th><th>Buys</th><th>Sells</th><th>Sells/Buys</th></tr>${row("h1")}${row("h6")}${row("h24")}</table></div>
      <div class="box"><h4>Измерено ончейн (2026-11-05)</h4><ul class="plain">
        <li>Комиссия hook: ${liveDs && L.fees?.BUY ? `сейчас BUY ${fmt.num(L.fees.BUY / 1e4, 2)}% / SELL ${fmt.num(L.fees.SELL / 1e4, 2)}% (медиана ${L.fees.BUY_n}/${L.fees.SELL_n} последних Swap)` : "покупка 0,50%, продажа 1,00% — по 88 событиям Swap (evidence/fables_swaps)"}</li>
        ${liveDs && L.pool ? `<li>StateView: mid ${fmt.price(L.pool.mid)} USDG, блок ${L.pool.block}, tick ${L.pool.tick}, L = ${esc(L.pool.liquidity)} (${Lab.age(L.pool_age)})</li>` : ""}
        ${liveDs && L.gas?.usd_per_swap != null ? `<li>Газ: ${fmt.usd(L.gas.usd_per_swap, 4)} за своп (${L.gas.wei} wei × 250k, ETH ${fmt.usd(L.gas.eth_usd, 0)})</li>` : ""}
        ${liveDs && L.context ? `<li>Контекст: BTC ${fmt.usd(L.context.BTC?.usd, 0)} (${fmt.sPct(L.context.BTC?.chg24, 1)} 24ч) · ETH ${fmt.usd(L.context.ETH?.usd, 0)} (${fmt.sPct(L.context.ETH?.chg24, 1)}) · USDG ${fmt.num(L.context.USDG?.usd, 4)}$</li>` : ""}
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

  function rejLocal(tr) {   // BACKTEST: та же статистика из списка решений терминала
    const rej = tr.decisions.filter((x) => ["WAIT", "NO_TRADE", "WARMUP", "SKIPPED"].includes(x.action_final));
    const first = {};
    rej.forEach((x) => x.blocker && (first[x.blocker] = (first[x.blocker] || 0) + 1));
    const by = {};
    tr.decisions.forEach((x) => (by[x.action_final] = (by[x.action_final] || 0) + 1));
    const arr = Object.entries(first).sort((a, b) => b[1] - a[1]).map(([name, count]) => ({ name, count, pct: count / rej.length * 100 }));
    return { decisions: tr.decisions.length, rejected: rej.length, by_action: by, any_fail: arr, first_blocker: arr, risk_fail: [] };
  }

  function banner() {
    const b = document.getElementById("modeBanner");
    if (mode === "BACKTEST") {
      b.innerHTML = `<b>BACKTEST · NOT VALIDATED.</b> Офлайн-воспроизведение записанных закрытых свечей (${esc(overview.dataset.id)}) тем же кодом стратегий; исполнение по следующей цене,
        консервативная модель издержек. Это не forward-результат и не прогноз. Старт: ${fmt.dt(overview.backtest.start_ts)}.`;
    } else {
      const f = overview.forward, w = overview.live.worker, rk = overview.live.risk || {};
      const st = { NOT_STARTED: "Эксперимент не запущен — ниже живые рыночные данные; сделок и PnL нет. Нажмите START.",
        RUNNING: `Эксперимент ${esc(f.experiment)} идёт с ${fmt.dt(f.started_at)}: трейдеры решают на каждой закрытой свече, Risk Monitor проверяет стопы на каждой котировке.`,
        PAUSED: `Эксперимент ${esc(f.experiment)} на паузе: новые входы запрещены, стопы и выходы защищают позиции.`,
        STOPPED: `Эксперимент ${esc(f.experiment)} остановлен: решения не принимаются, позиции (если есть) сохранены и оцениваются по рынку.` }[f.status];
      const ig = overview.live.integrity, ld = overview.live.last_decisions || {};
      const lastDec = ["1h", "4h", "1d"].map((x) => `${x.toUpperCase()} ${ld[x]?.decided_ts ? fmt.time(ld[x].decided_ts) + " " + esc(ld[x].action) : "—"}`).join(" · ");
      b.innerHTML = `${ig && ig.ok === false ? `<div class="neg"><b>Целостность состояния нарушена:</b> ${esc((ig.issues || []).join("; "))}. Не продолжайте, запустите diagnose.bat.</div>` : ""}
        <b>FORWARD · LIVE · ${Lab.STATUS_RU[f.status]}.</b> ${st} Бот: <b class="${w.alive ? "pos" : "neg"}">${w.alive ? "работает" : "не запущен"}</b>${w.age != null ? " (heartbeat " + esc(Lab.age(w.age)) + ")" : ""}.
        Последние решения: ${lastDec}.
        ${rk.entry_blockers?.length ? `<span class="warnline">Новые входы заблокированы: ${esc(rk.entry_blockers.join("; "))}.</span>` : ""}
        ${rk.unprotected?.length ? `<span class="neg">Позиции ${esc(rk.unprotected.join(", ").toUpperCase())} без проверки: котировка недостоверна.</span>` : ""}
        Исполнение — оценка (локальный расчёт свопа v4 по состоянию StateView), не точный quote и не подтверждённые свопы.`;
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
    document.addEventListener("lab:refresh", () => refresh(true));
    // FORWARD: живое обновление (локальный сервер; внешние источники опрашивает только бот по своему расписанию)
    setInterval(() => refresh(mode === "FORWARD").catch(() => {}), 15000);
    setInterval(() => { if (mode === "FORWARD") TFS.forEach((tf) => updateLive(tf).catch(() => {})); }, 5000);   // формирующаяся свеча
    setInterval(() => TFS.forEach((tf) => { if (T[tf].data?.mode === "FORWARD") $(tf, "asof").innerHTML = T[tf].data.trader
      ? monitorInfo(tf) + " · " + closeInfo(tf) + " · " + esc(T[tf].data.trader.summary.mark ? `оценка по ${fmt.price(T[tf].data.trader.summary.mark.mid)} @ ${fmt.time(T[tf].data.trader.summary.mark.ts)}` : "")
      : closeInfo(tf); }), 1000);
  }
  init();
})();
