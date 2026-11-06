/* SHADOW LAB: независимый paper-трейдер SHADOW-P2-4H. Только данные /api/shadow; команды — в очередь процесса shadow.bat. */
"use strict";

(() => {
  const { fmt, esc, api, css, age } = Lab;
  const LW = window.LightweightCharts;
  const $ = (id) => document.getElementById(id);
  const DAY = 86400;
  let D = null, chart = null, S = {}, mk = null, extra = [], plines = [], jFilter = "ALL";

  const on = (k) => !!document.querySelector(`#shTools [data-ov="${k}"]`)?.checked;
  const kpi = (k, v, s, cls = "") => `<div class="kpi"><div class="k">${esc(k)}</div><div class="v ${cls}">${v}</div><div class="s">${s ?? "&nbsp;"}</div></div>`;
  const act = (a) => `<span class="sh-act ${esc(a)}">${esc(a)}</span>`;

  // ------------------------------------------------------------------ график
  function build() {
    chart = LW.createChart($("shChart"), {
      autoSize: true,
      layout: { background: { type: LW.ColorType.Solid, color: css("--surface-1") }, textColor: css("--text-muted"), fontSize: 11, fontFamily: css("--mono") },
      grid: { vertLines: { color: "#151a21" }, horzLines: { color: "#151a21" } },
      rightPriceScale: { borderColor: css("--border") }, timeScale: { borderColor: css("--border"), timeVisible: true, rightOffset: 4 },
      crosshair: { mode: LW.CrosshairMode.Normal },
      localization: { locale: "ru-RU", priceFormatter: (p) => fmt.price(p) },
    });
    S.candle = chart.addSeries(LW.CandlestickSeries, { upColor: css("--up"), downColor: css("--down"), borderVisible: false,
      wickUpColor: css("--up"), wickDownColor: css("--down"), priceFormat: { type: "price", precision: 6, minMove: 0.000001 } });
    S.vol = chart.addSeries(LW.HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    mk = LW.createSeriesMarkers(S.candle, []);
    chart.subscribeCrosshairMove(tip);
    window.__shadow = { chart, S, get data() { return D; }, markers: () => mk.markers() };   // для UI-проверок (только чтение)
  }

  const liveBars = () => (D?.live?.bars || []).filter((b) => !D.chart.candles.length || b.time > D.chart.candles.at(-1).time);

  function bar(b, live) {
    const up = b.close >= b.open;
    const c = up ? "25,158,112" : "230,103,103";
    return live ? { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close, color: `rgba(${c},.35)`, wickColor: `rgba(${c},.6)` }
      : { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close };
  }

  function line(pts, color, width, style) {
    const s = chart.addSeries(LW.LineSeries, { color, lineWidth: width, lineStyle: style, lastValueVisible: false, priceLineVisible: false,
      crosshairMarkerVisible: false, pointMarkersVisible: false });
    const m = new Map(); pts.forEach((p) => m.set(p.time, p.value));
    s.setData([...m].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value })));
    extra.push(s);
  }

  function priceLine(price, color, title, style = LW.LineStyle.Dashed) {
    if (price == null) return;
    plines.push(S.candle.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title }));
  }

  function setChart(fit) {
    const c = D.chart?.candles || [];
    const lb = liveBars();
    S.candle.setData([...c.map((b) => bar(b, false)), ...lb.map((b) => bar(b, true))]);
    S.vol.setData([...c, ...lb].filter((b) => b.volume != null).map((b) => ({ time: b.time, value: b.volume, color: "rgba(166,174,185,.25)" })));
    overlays();
    if (fit) chart.timeScale().fitContent();
  }

  function overlays() {
    extra.forEach((s) => chart.removeSeries(s)); extra = [];
    plines.forEach((l) => S.candle.removePriceLine(l)); plines = [];
    const v3 = D.structure_v3, marks = [];
    const lab = (t) => t.replace("_UP", "↑").replace("_DOWN", "↓").replace("_HIGH", "↑").replace("_LOW", "↓");
    if (v3 && !v3.empty) {
      const maj = v3.major.swings.filter((w) => w.tier === "major");
      if (on("mswing")) {
        line(maj.map((w) => ({ time: w.ts, value: w.price })), css("--s-violet"), 2, LW.LineStyle.Solid);
        maj.forEach((w) => marks.push({ time: w.ts, position: w.kind === "H" ? "aboveBar" : "belowBar", color: css("--s-violet"), shape: "circle", size: 0.6,
          text: `${w.major_label || w.kind} · major` }));
      }
      if (on("iswing")) line(v3.internal.swings.map((w) => ({ time: w.ts, value: w.price })), "#56606c", 1, LW.LineStyle.Dotted);
      if (on("mevents")) v3.major.events.filter((e) => !e.type.startsWith("SWEEP") || true).forEach((e) => marks.push({ time: e.ts,
        position: /UP|HIGH/.test(e.type) ? "aboveBar" : "belowBar", color: e.type.startsWith("CHoCH") ? css("--s2") : e.type.startsWith("BOS") ? css("--s-violet") : css("--text-muted"),
        shape: e.type.startsWith("SWEEP") ? "circle" : "arrowUp", size: e.type.startsWith("SWEEP") ? 0.4 : 1, text: lab(e.type) }));
      if (on("ievents")) v3.internal.events.filter((e) => !e.type.startsWith("iSWEEP")).forEach((e) => marks.push({ time: e.ts,
        position: e.type.endsWith("UP") ? "belowBar" : "aboveBar", color: "#7d8590", shape: "circle", size: 0.4, text: lab(e.type) }));
      if (on("prot")) {
        const st = v3.major.state || {}, r = v3.range || {};
        priceLine(st.ph, css("--down"), `protected high ${st.ph_status || ""}`.trim(), LW.LineStyle.Solid);
        priceLine(st.pl, css("--up"), `protected low ${st.pl_status || ""}`.trim(), LW.LineStyle.Solid);
        if (r.structural_high) priceLine(r.structural_high.price, "#7d8590", "structural high");
        if (r.structural_low) priceLine(r.structural_low.price, "#7d8590", "structural low");
      }
    }
    if (on("trades")) (D.markers || []).forEach((m) => marks.push({ time: m.time,
      position: m.kind === "BUY" || m.kind === "ENTER" ? "belowBar" : "aboveBar",
      color: m.kind === "BUY" || m.kind === "ENTER" ? css("--up") : m.kind === "SKIPPED" ? css("--warning") : css("--down"),
      shape: m.kind === "BUY" || m.kind === "ENTER" ? "arrowUp" : m.kind === "SKIPPED" ? "circle" : "arrowDown", size: m.kind === "SKIPPED" ? 0.5 : 1.4, text: m.text }));
    if (on("plan")) {
      const pos = D.trader?.position, pl = (D.decision || D.preview)?.plan;
      if (pos) {
        priceLine(pos.entry_mid, css("--accent"), "вход P2", LW.LineStyle.Solid);
        priceLine(pos.stop, css("--critical"), "стоп P2");
        if (!pos.t1_done) priceLine(pos.t1, css("--good"), "T1 P2");
      } else if (pl && (D.decision || D.preview)?.action === "ENTER") {
        priceLine(pl.entry_ref, css("--accent"), "план: вход");
        priceLine(pl.stop, css("--critical"), "план: стоп");
        priceLine(pl.t1, css("--good"), "план: T1");
      }
    }
    marks.sort((a, b) => a.time - b.time);
    mk.setMarkers(marks);
  }

  function tip(p) {
    const el = $("shTip");
    if (!p.time || !p.point || !D) { el.style.display = "none"; return; }
    const c = D.chart.candles.find((b) => b.time === p.time), lb = liveBars().find((b) => b.time === p.time), b = c || lb;
    if (!b) { el.style.display = "none"; return; }
    const st = c ? "закрытая свеча" : lb.status === "FORMING" ? "FORMING — формирующаяся, только отображение (стратегия её не использует)" : "закрыта, ждёт свечу GeckoTerminal";
    const dec = (D.trader?.decisions || []).find((d) => d.bar_ts === p.time);
    const v3 = D.structure_v3, ev = v3 && !v3.empty ? [...v3.major.events, ...v3.internal.events].filter((e) => e.ts === p.time) : [];
    const tl = v3 && !v3.empty ? v3.timeline.filter((x) => x.ts <= p.time).at(-1) : null;
    let h = `<div class="t">${fmt.dt(p.time)} · <span class="${c ? "muted" : "warnline"}">${esc(st)}</span></div><table>
      <tr><td>O / H</td><td>${fmt.price(b.open)} / ${fmt.price(b.high)}</td></tr><tr><td>L / C</td><td>${fmt.price(b.low)} / <b>${fmt.price(b.close)}</b></td></tr>
      <tr><td>Объём</td><td>${b.volume == null ? "—" : fmt.big(b.volume)}</td></tr>`;
    if (tl) h += `<tr><td>Major</td><td>${esc(tl.major)}${tl.bias ? "/" + esc(tl.bias) : ""} · ${esc(tl.phase || "")}</td></tr><tr><td>Internal</td><td>${esc(tl.internal || "—")}</td></tr>`;
    ev.forEach((e) => (h += `<tr><td>${esc(e.type)}</td><td>${fmt.price(e.level)}</td></tr>`));
    if (dec) h += `<tr><td>P2</td><td>${esc(dec.action_final)}${dec.blocker ? " · " + esc(dec.blocker) : ""}</td></tr>`;
    el.innerHTML = h + "</table>";
    el.style.display = "block";
    const w = $("shChart").clientWidth;
    el.style.left = (p.point.x + 16 + 300 > w ? p.point.x - 316 : p.point.x + 16) + "px";
    el.style.top = "40px";
  }

  // ------------------------------------------------------------------ панели
  function head() {
    const w = D.worker, r = D.readiness, al = new Set(D.allowed), q = D.risk;
    const mainOk = r ? r.checks.find((c) => c.name.startsWith("Основной бот"))?.ok : null;
    $("shChips").innerHTML = [
      `<span class="chip ${w.alive ? "good" : "crit"}" title="${esc(w.alive ? "heartbeat " + age(w.age) : "процесс SHADOW не отвечает")}"><i></i>SHADOW-процесс: ${w.alive ? "РАБОТАЕТ" : w.installed ? "ОСТАНОВЛЕН" : "НЕ ЗАПУСКАЛСЯ"}</span>`,
      mainOk == null ? "" : `<span class="chip ${mainOk ? "good" : "crit"}"><i></i>Основной бот: ${mainOk ? "данные идут" : "нет heartbeat"}</span>`,
      `<span class="chip ${D.integrity.ok ? "good" : "crit"}" title="${esc(D.integrity.issues.join("; "))}"><i></i>Целостность: ${D.integrity.ok ? "OK" : "ОШИБКА"}</span>`,
      q ? `<span class="chip ${q.protection === "FULL" || !D.trader?.position ? "good" : q.protection === "DEGRADED" ? "warn" : "crit"}"><i></i>Котировка: ${q.quote_valid ? "проверена" : esc(q.quote_level)}</span>` : "",
    ].join("");
    const can = (c) => al.has(c) && w.alive;
    const pause = D.status === "PAUSED" ? `<button class="btn" data-cmd="resume" ${can("RESUME") ? "" : "disabled"}>RESUME</button>`
      : `<button class="btn" data-cmd="pause" ${can("PAUSE") ? "" : "disabled"}>PAUSE</button>`;
    $("shCtl").innerHTML = `<span class="badge ${Lab.STATUS_BADGE[D.status] || ""}">SHADOW: ${esc(Lab.STATUS_RU[D.status] || D.status)}</span>
      <button class="btn primary" data-cmd="start" ${can("START") ? "" : "disabled"} title="${w.alive ? "" : "запустите shadow.bat"}">START</button>${pause}
      <button class="btn danger" data-cmd="stop" ${can("STOP") ? "" : "disabled"}>STOP</button>
      <button class="btn danger" data-cmd="close" ${can("CLOSE_POSITION") ? "" : "disabled"}>CLOSE POSITION</button>
      <button class="btn danger" data-cmd="emergency" ${can("EMERGENCY_EXIT") ? "" : "disabled"}>EMERGENCY EXIT</button>`;
    $("shCtl").querySelectorAll("[data-cmd]").forEach((b) => b.addEventListener("click", () => command(b.dataset.cmd)));
    const t = D.trader, s = t?.summary, idle = D.portfolio_idle, c = t?.costs;
    const st = s || { initial: 1000, cash: idle.cash, fables_qty: 0, fables_value: 0, equity: idle.equity, net_pnl: 0, net_return_pct: 0, realized_pnl: 0, unrealized_pnl: 0 };
    $("shKpis").innerHTML = [
      kpi("Капитал (старт)", fmt.usd(1000, 0), "отдельно от $3 000"),
      kpi("Cash", fmt.usd(st.cash), "USDG"),
      kpi("FABLES", fmt.qty(st.fables_qty), st.fables_value ? fmt.usd(st.fables_value) + " по ликвидации" : "позиции нет"),
      kpi("Equity", fmt.usd(st.equity), D.quote ? `оценка @ ${fmt.time(D.quote.ts)}` : "нет котировки"),
      kpi("Net PnL", fmt.sUsd(st.net_pnl), fmt.sPct(st.net_return_pct), fmt.cls(st.net_pnl)),
      kpi("Realized / Unrealized", `${fmt.sUsd(st.realized_pnl)} / ${fmt.sUsd(st.unrealized_pnl)}`, "после издержек"),
      kpi("Издержки", fmt.usd(c?.total_usd ?? 0), c ? `fee ${fmt.usd(c.fee_usd)} · gas ${fmt.usd(c.gas_usd)}` : "операций нет"),
      kpi("Сделок", String(s?.closed_trades ?? 0), `операций ${s?.operations ?? 0}`),
      kpi("Win Rate / PF", `${s?.win_rate == null ? "—" : fmt.pct(s.win_rate, 0)} / ${s?.profit_factor == null ? "—" : fmt.num(s.profit_factor, 2)}`, "закрытые сделки"),
      kpi("Expectancy R", s?.expectancy_r == null ? "—" : fmt.num(s.expectancy_r, 2) + "R", s?.expectancy_usd == null ? "" : fmt.sUsd(s.expectancy_usd)),
      kpi("Max Drawdown", fmt.pct(s?.max_drawdown_marks_pct ?? 0), "по оценкам"),
      kpi("vs Buy & Hold", s?.vs_bh_usd == null ? "—" : fmt.sUsd(s.vs_bh_usd), s?.bh_equity_same_time ? "B&H " + fmt.usd(s.bh_equity_same_time) : "B&H — после START", fmt.cls(s?.vs_bh_usd)),
    ].join("");
    const days = D.started_at ? (D.now - D.started_at) / DAY : 0;
    $("shBanner").innerHTML = `<b>SHADOW LAB — виртуальный (paper) эксперимент.</b> ${esc(D.paper)} Портфель $1 000 SHADOW <b>не входит</b> в $3 000 основного FORWARD;
      рыночные данные читаются из базы основного бота <b>только на чтение</b>. ` +
      (D.status === "NOT_STARTED" ? `<b>Эксперимент не запущен</b> — ниже предпросмотр правил на последней закрытой свече, сделок нет.`
        : `Эксперимент ${esc(D.experiment)} · начат ${fmt.dt(D.started_at)} · идёт ${fmt.num(days, 1)} дн.`) +
      (D.version_ok ? "" : ` <b class="neg">${esc(D.version_issue)}</b>`);
    $("shStrat").textContent = `${D.strategy.id} ${D.strategy.version} · хэш правил ${D.strategy.rules_hash.slice(0, 12)}`;
    $("shState").innerHTML = t ? `<span class="badge">${esc(t.state)}</span>` : "";
    $("shAsof").textContent = D.chart?.candles?.length ? `последняя закрытая 4H: ${fmt.dt(D.chart.candles.at(-1).time)}` : "свечей нет";
  }

  function conds(list) {
    return `<ul class="conds">${(list || []).map((c) => `<li class="${c.ok === true ? "ok" : c.ok === false ? "bad" : "na"}"><span class="ic">${c.ok === true ? "✓" : c.ok === false ? "✗" : "—"}</span>
      <span>${esc(c.name)}<div class="d">${esc(c.detail)}${c.req ? ` <span class="muted">(требование: ${esc(c.req)})</span>` : ""}</div></span></li>`).join("")}</ul>`;
  }

  function decision() {
    const d = D.decision || D.preview;
    if (!d) { $("shDecision").innerHTML = `<div class="empty">Решений ещё нет${D.status === "RUNNING" ? " — первое на закрытии следующей 4H-свечи" : ""}.</div>`; return; }
    if (d.error) { $("shDecision").innerHTML = `<div class="empty">Предпросмотр недоступен: ${esc(d.error)}</div>`; return; }
    const a = d.action_final || d.action, p = d.plan;
    $("shDecTs").textContent = `свеча ${fmt.time(d.bar_ts)}${d.decided_ts ? " · решено " + fmt.time(d.decided_ts) : ""}`;
    $("shDecision").innerHTML = `<div class="decision-head">${act(a)}${d.preview ? `<span class="badge warn">ПРЕДПРОСМОТР</span>` : ""}</div>
      ${d.preview ? `<div class="sh-note">${esc(d.note)}</div>` : ""}
      ${conds(d.conditions)}
      ${p ? `<div class="kv" style="margin-top:8px">
        <span class="k">Плановый вход (close)</span><span class="v">${fmt.price(p.entry_ref)}</span>
        <span class="k">Стоп (HL − 0,25 ATR)</span><span class="v">${fmt.price(p.stop)}</span>
        <span class="k">T1 (${fmt.num((p.t1_frac || 0) * 100, 0)}%)</span><span class="v">${fmt.price(p.t1)}</span>
        <span class="k">Trailing / time stop</span><span class="v">${p.trail_atr} ATR / ${p.time_stop} баров</span>
        <span class="k">Чистый R:R до T1</span><span class="v ${p.rr_net != null && p.rr_net >= 1.5 ? "pos" : "neg"}">${p.rr_net == null ? "—" : fmt.num(p.rr_net, 2)}</span>
        <span class="k">Плановый размер</span><span class="v">${fmt.usd(p.plan_usd)}</span>
        <span class="k">Круг издержек</span><span class="v">${p.rr_net == null ? "—" : fmt.pct(p.roundtrip_cost_pct)}</span></div>` : ""}
      <div class="sh-note">${esc(d.next || "")}</div>`;
  }

  function position() {
    const p = D.trader?.position, pend = D.trader?.pending;
    if (!p) { $("shPosition").innerHTML = `<div class="empty">Позиции нет${pend ? ` · отложено: ${esc(pend.kind)} (${esc(pend.reason || "")})` : ""}.</div>`; return; }
    const prot = D.risk?.traders?.["4h"] || {};
    $("shPosition").innerHTML = `<div class="kv">
      <span class="k">Вход</span><span class="v">${fmt.price(p.entry_mid)} · ${fmt.time(p.entry_ts)}</span>
      <span class="k">FABLES</span><span class="v">${fmt.qty(p.qty)}</span>
      <span class="k">Стоп / начальный</span><span class="v">${fmt.price(p.stop)} / ${fmt.price(p.initial_stop)}</span>
      <span class="k">T1</span><span class="v">${fmt.price(p.t1)}${p.t1_done ? " ✓" : ""}</span>
      <span class="k">Риск</span><span class="v">${fmt.usd(p.risk_usd)}</span>
      <span class="k">К входу</span><span class="v ${fmt.cls(p.unrealized_vs_entry_pct)}">${fmt.sPct(p.unrealized_vs_entry_pct)}</span>
      <span class="k">Защита</span><span class="v">${esc(prot.protection || "—")}${prot.last_stop_check ? " · " + fmt.time(prot.last_stop_check.ts) : ""}</span></div>`;
  }

  function costs() {
    const c = D.trader?.costs;
    $("shCosts").innerHTML = c && c.operations ? `<div class="kv">
      <span class="k">Комиссия hook</span><span class="v">${fmt.usd(c.fee_usd, 4)}</span>
      <span class="k">Price impact (CLMM)</span><span class="v">${fmt.usd(c.impact_usd, 4)}</span>
      <span class="k">Проскальзывание</span><span class="v">${fmt.usd(c.slippage_usd, 4)}</span>
      <span class="k">Газ</span><span class="v">${fmt.usd(c.gas_usd, 4)}</span>
      <span class="k">Итого (${c.operations} опер.)</span><span class="v">${fmt.usd(c.total_usd, 4)}</span></div>`
      : `<div class="empty">Операций нет. Модель: комиссия hook из свопов пула, импакт по тикам CLMM, проскальзывание, газ — данные основного бота.</div>`;
  }

  function journalTab() {
    const rows = D.trader?.journal || [];
    const kinds = { ALL: "Все", DECISION: "Решения", FILL: "Операции", EVENT: "Отказы и события" };
    const list = rows.filter((r) => jFilter === "ALL" || r.kind === jFilter);
    $("shJournal").innerHTML = `<div class="sh-filter">${Object.entries(kinds).map(([k, n]) => `<button data-jf="${k}" class="${jFilter === k ? "on" : ""}">${n}</button>`).join("")}</div>
      ${list.length ? `<div class="sh-scroll"><table class="t"><tr><th>Время</th><th>Тип</th><th>Действие</th><th>Причина / детали</th><th>Цена / $</th></tr>
      ${list.map((r) => `<tr><td class="n">${fmt.dt(r.ts)}</td><td>${esc(r.kind)}</td><td>${act(r.action)}</td>
        <td>${esc(r.kind === "DECISION" ? (r.blockers.length ? "блокирует: " + r.blockers.join("; ") : r.text.split("\n")[0]) : r.text)}
        ${r.kind === "FILL" ? `<div class="muted">fee ${fmt.usd(+r.costs.fee_usd, 4)} · impact ${fmt.usd(+r.costs.impact_usd, 4)} · slip ${fmt.usd(+r.costs.slippage_usd, 4)} · gas ${fmt.usd(+r.costs.gas_usd, 4)}<br>${esc(r.note || "")}</div>` : ""}
        ${r.kind === "DECISION" && r.bar_ts ? `<div class="muted">свеча ${fmt.dt(r.bar_ts)}${r.rr_net != null ? " · R:R " + fmt.num(r.rr_net, 2) : ""}</div>` : ""}</td>
        <td class="n">${r.kind === "FILL" ? `${fmt.price(+r.price)}<br>${fmt.usd(+r.usd)}` : ""}</td></tr>`).join("")}</table></div>`
      : `<div class="empty">${D.status === "NOT_STARTED" ? "Журнал пуст: эксперимент не запущен." : "Записей нет."}</div>`}
      <h4 class="muted" style="margin:12px 0 4px">Команды управления SHADOW</h4>
      ${D.controls.length ? `<table class="t"><tr><th>Время</th><th>Команда</th><th>Переход</th><th>Примечание</th></tr>${D.controls.map((c) => `<tr><td class="n">${fmt.dt(c.ts)}</td><td>${esc(c.command)}</td><td>${esc(c.from_status || "")} → ${esc(c.to_status)}</td><td>${esc(c.note || "")}</td></tr>`).join("")}</table>` : `<div class="empty">Команд не было.</div>`}`;
    $("shJournal").querySelectorAll("[data-jf]").forEach((b) => b.addEventListener("click", () => { jFilter = b.dataset.jf; journalTab(); }));
  }

  function structureTab() {
    const v3 = D.structure_v3;
    if (!v3 || v3.empty) { $("shStructure").innerHTML = `<div class="empty">Нет закрытых 4H-свечей.</div>`; return; }
    const m = v3.major.state || {}, i = v3.internal.state || {};
    const ev = [...v3.major.events].slice(-12).reverse();
    $("shStructure").innerHTML = `<div class="grid2"><div class="box"><h4>Major (4H)</h4><div class="kv">
      <span class="k">Тренд / bias / фаза</span><span class="v">${esc(m.trend)}${m.bias ? "/" + esc(m.bias) : ""} · ${esc(m.phase || "—")}</span>
      <span class="k">Protected high</span><span class="v">${fmt.price(m.ph)} ${esc(m.ph_status || "")}</span>
      <span class="k">Protected low</span><span class="v">${fmt.price(m.pl)} ${esc(m.pl_status || "")}</span>
      <span class="k">История</span><span class="v">${esc(v3.history_status)}</span>
      <span class="k">SCALE_CONFLICT</span><span class="v">${v3.scale_conflict_now ? "да" : "нет"}</span>
      <span class="k">HTF 1D</span><span class="v">${esc(v3.htf?.text || "—")}</span></div></div>
      <div class="box"><h4>Internal</h4><div class="kv"><span class="k">Состояние</span><span class="v">${esc(i.trend || JSON.stringify(i).slice(0, 60))}</span>
      <span class="k">Последний iCHoCH</span><span class="v">${v3.last_internal_choch ? esc(v3.last_internal_choch.type) + " " + fmt.time(v3.last_internal_choch.ts) : "—"}</span>
      <span class="k">Последний iBOS</span><span class="v">${v3.last_internal_bos ? esc(v3.last_internal_bos.type) + " " + fmt.time(v3.last_internal_bos.ts) : "—"}</span></div></div></div>
      <h4 class="muted" style="margin:12px 0 4px">Major-события (последние)</h4>
      <table class="t"><tr><th>Свеча</th><th>Известно</th><th>Событие</th><th>Уровень</th><th>Почему</th></tr>
      ${ev.map((e) => `<tr><td class="n">${fmt.dt(e.ts)}</td><td class="n">${fmt.dt(e.known_ts)}</td><td>${esc(e.type)}</td><td class="n">${fmt.price(e.level)}</td><td>${esc(e.why || "")}</td></tr>`).join("")}</table>
      <div class="sh-note">Структура считается только по закрытым свечам; P2 использует Major v3 для режима и Internal для триггера. ${esc(v3.note)}</div>`;
  }

  function readyTab() {
    const r = D.readiness, w = D.worker, days = D.started_at ? (D.now - D.started_at) / DAY : 0, n = D.trader?.summary?.closed_trades || 0;
    $("shReady").innerHTML = `<div class="grid2"><div class="box"><h4>Проверка готовности к активации${r ? ` · ${fmt.dt(r.ts)}` : ""}</h4>
      ${r ? `<ul class="conds">${r.checks.map((c) => `<li class="${c.ok ? "ok" : "bad"}"><span class="ic">${c.ok ? "✓" : "✗"}</span><span>${esc(c.name)}<div class="d">${esc(c.detail)}</div></span></li>`).join("")}</ul>
      <div class="sh-note">${r.ok ? "Готов: START можно дать вручную (кнопка вверху)." : "Не готов: " + esc(r.failed.join(", "))}</div>` : `<div class="empty">Проверка выполняется при START.</div>`}</div>
      <div class="box"><h4>Процесс SHADOW</h4><div class="kv">
      <span class="k">Статус</span><span class="v">${esc(w.status || (w.installed ? "остановлен" : "не запускался"))}</span>
      <span class="k">Heartbeat</span><span class="v">${w.age == null ? "—" : age(w.age)}</span>
      <span class="k">PID / циклов</span><span class="v">${esc(w.pid ?? "—")} / ${esc(w.ticks ?? "—")}</span>
      <span class="k">База</span><span class="v">data/shadow_lab.db</span>
      <span class="k">Журнал</span><span class="v">data/logs/shadow.log</span>
      <span class="k">Целостность</span><span class="v ${D.integrity.ok ? "pos" : "neg"}">${D.integrity.ok ? "состояние = журнал" : esc(D.integrity.issues.join("; "))}</span></div>
      ${(w.errors || []).length ? `<h4 style="margin-top:8px">Последние ошибки</h4><ul class="plain">${w.errors.map((e) => `<li>${fmt.time(e.ts)} ${esc(e.where)}: ${esc(e.error)}</li>`).join("")}</ul>` : ""}
      <h4 style="margin-top:10px">Контрольные точки оценки</h4>
      <div class="sh-ck">${[30, 60, 120].map((x) => `<div><b>${x} дн.</b><div class="muted">${D.started_at ? (days >= x ? "достигнута" : `через ${fmt.num(x - days, 1)} дн.`) : "после START"}</div></div>`).join("")}</div>
      <div class="sh-note">Сделок: ${n} из 20 — минимум для ПРЕДВАРИТЕЛЬНОГО анализа (не доказательство). Параметры под текущее поведение FABLES не подстраиваются.</div></div></div>
      ${D.events.length ? `<h4 class="muted" style="margin:12px 0 4px">События SHADOW</h4><table class="t">${D.events.slice(0, 20).map((e) => `<tr><td class="n">${fmt.dt(e.ts)}</td><td>${esc(e.kind)}</td><td>${esc(JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k]) => !["ts", "kind"].includes(k)))).slice(0, 220))}</td></tr>`).join("")}</table>` : ""}`;
  }

  function rulesTab() {
    const s = D.strategy, f = D.fixed;
    $("shRules").innerHTML = `<div class="grid2"><div class="box"><h4>Стратегия</h4><div class="kv">
      <span class="k">ID / версия</span><span class="v">${esc(s.id)} ${esc(s.version)}</span>
      <span class="k">Хэш правил (код)</span><span class="v">${esc(s.rules_hash.slice(0, 16))}…</span>
      <span class="k">Зафиксировано при START</span><span class="v">${f ? esc(f.version + " · " + f.rules_hash.slice(0, 16) + "…") : "—"}</span>
      <span class="k">Актив</span><span class="v">${esc(D.asset.pair)} · ${esc(D.asset.chain)} · ${esc(D.asset.kind)}</span>
      <span class="k">ТФ / HTF</span><span class="v">4H / 1D (только вето)</span>
      <span class="k">Капитал / риск</span><span class="v">$1 000 / 1% на сделку</span></div>
      <pre class="expl">${esc(JSON.stringify(s.params, null, 2))}</pre></div>
      <div class="box"><h4>Правила P2 (без изменений с research-0.1)</h4><ul class="plain">
      <li>Major v3 в TRANSITION вверх после CHoCH_UP (protected high нарушен), история OK, нет SCALE_CONFLICT.</li>
      <li>Триггер: internal iCHoCH_UP или iBOS_UP на закрытой 4H-свече.</li>
      <li>Internal higher low после CHoCH выше кандидата protected low.</li>
      <li>1D — только вето (если определён и DOWN).</li>
      <li>Стоп = HL − 0,25 ATR, дистанция 0,5–3 ATR; T1 = структурный high (иначе max ноги CHoCH).</li>
      <li>Чистый R:R до T1 ≥ 1,5 при плановом размере (комиссии, импакт, проскальзывание стопа, газ).</li>
      <li>T1 — 50%, trailing 2,5 ATR по close, time stop 40 баров; выход при возврате Major к DOWN.</li>
      <li>Решения — только по закрытым 4H-свечам; формирующаяся свеча не используется; сделок задним числом нет.</li>
      <li>Сопровождение позиции — на каждой новой проверенной котировке (Risk Monitor основного типа).</li></ul></div></div>`;
  }

  function render(fit) {
    head();
    setChart(fit);
    decision(); position(); costs();
    journalTab(); structureTab(); readyTab(); rulesTab();
  }

  // ------------------------------------------------------------------ команды
  const CONFIRM = {
    start: "Запустить эксперимент SHADOW-P2-4H?\n\nВиртуальный портфель $1 000 начнёт работать на живых данных FABLES с этого момента. Реальных сделок нет. Портфель не входит в $3 000 основного FORWARD.",
    stop: "Остановить SHADOW?\n\nАвтоматической продажи нет: позиция остаётся и оценивается по рынку.",
    close: "Закрыть позицию SHADOW по первой проверенной котировке?",
    emergency: "EMERGENCY EXIT SHADOW: закрыть позицию и остановить новые входы?",
  };
  async function command(cmd) {
    if (CONFIRM[cmd] && !window.confirm(CONFIRM[cmd])) return;
    try { await api(`/api/shadow/${cmd}`, { method: "POST" }); } catch (e) { window.alert(`SHADOW ${cmd.toUpperCase()} не выполнена: ${e.message}`); }
    refresh(false).catch(() => {});
  }

  async function refresh(fit) {
    D = await api("/api/shadow");
    render(fit);
  }

  build();
  document.querySelectorAll("#shTools input").forEach((x) => x.addEventListener("change", overlays));
  document.querySelectorAll("#shTabs button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("#shTabs button, .tab-body").forEach((x) => x.classList.toggle("on", x.dataset.tab === b.dataset.tab));
  }));
  refresh(true).catch((e) => { $("shBanner").textContent = "Ошибка загрузки SHADOW LAB: " + e.message; });
  setInterval(() => refresh(false).catch(() => {}), 10000);
})();
