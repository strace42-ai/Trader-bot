/* Общие функции: API, форматирование, верхняя панель и управление FORWARD. Только данные с сервера ядра. */
"use strict";

const Lab = (() => {
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const nf = (d) => new Intl.NumberFormat("ru-RU", { minimumFractionDigits: d, maximumFractionDigits: d });
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const fmt = {
    usd(x, d = 2) { return x == null || isNaN(x) ? "—" : (x < 0 ? "−$" : "$") + nf(d).format(Math.abs(x)); },
    sUsd(x, d = 2) { return x == null || isNaN(x) ? "—" : (x > 0 ? "+" : x < 0 ? "−" : "") + "$" + nf(d).format(Math.abs(x)); },
    pct(x, d = 2) { return x == null || isNaN(x) ? "—" : nf(d).format(x) + "%"; },
    sPct(x, d = 2) { return x == null || isNaN(x) ? "—" : (x > 0 ? "+" : x < 0 ? "−" : "") + nf(d).format(Math.abs(x)) + "%"; },
    price(x) { return x == null || isNaN(x) ? "—" : Number(x).toPrecision(5).replace(/\.?0+$/, (m) => (m.includes(".") ? "" : m)); },
    qty(x) { return x == null || isNaN(x) ? "—" : nf(x >= 100 ? 0 : 2).format(x); },
    num(x, d = 2) { return x == null || isNaN(x) ? "—" : nf(d).format(x); },
    big(x) {
      if (x == null || isNaN(x)) return "—";
      const a = Math.abs(x);
      return a >= 1e6 ? "$" + nf(2).format(x / 1e6) + "M" : a >= 1e3 ? "$" + nf(1).format(x / 1e3) + "K" : "$" + nf(0).format(x);
    },
    time(ts) {
      if (!ts) return "—";
      const d = new Date(ts * 1000);
      const p = (n) => String(n).padStart(2, "0");
      return `${p(d.getUTCDate())}.${p(d.getUTCMonth() + 1)} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
    },
    dt(ts) {
      if (!ts) return "—";
      const d = new Date(ts * 1000);
      const p = (n) => String(n).padStart(2, "0");
      return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`;
    },
    cls(x) { return x == null || isNaN(x) || x === 0 ? "" : x > 0 ? "pos" : "neg"; },
  };

  const TOKEN = document.querySelector('meta[name="lab-token"]')?.content || "";

  async function api(path, opts = {}) {
    const o = { ...opts, headers: { ...(opts.headers || {}), ...(opts.method && opts.method !== "GET" ? { "X-Lab-Token": TOKEN } : {}) } };
    const r = await fetch(path, o);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const det = body.detail;
      throw new Error(typeof det === "string" ? det : det?.error || `HTTP ${r.status}`);
    }
    return body;
  }

  const age = (sec) => sec == null ? "—" : sec < 90 ? `${sec} с назад` : sec < 5400 ? `${Math.round(sec / 60)} мин назад` : `${Math.round(sec / 3600)} ч назад`;

  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* недоступно — не критично */ } },
  };

  const STATUS_BADGE = { NOT_STARTED: "", RUNNING: "good", PAUSED: "warn", STOPPED: "crit" };
  const STATUS_RU = { NOT_STARTED: "NOT STARTED", RUNNING: "RUNNING", PAUSED: "PAUSED", STOPPED: "STOPPED" };

  function kpi(k, v, s, cls = "") {
    return `<div class="kpi"><div class="k">${esc(k)}</div><div class="v ${cls}">${v}</div><div class="s">${s ?? "&nbsp;"}</div></div>`;
  }

  function renderTopbar(o, mode, page) {
    const bt = o.backtest, fw = o.forward, live = o.live, w = live.worker;
    const al = new Set(fw.allowed);
    const fwd = mode === "FORWARD";
    const srcChips = o.sources.map((s) => {
      const c = { OK: "good", DEGRADED: "warn", DOWN: "crit" }[s.status] || "idle";
      return `<span class="chip ${c}" title="${esc(s.note)}${s.ts ? " · последний успех " + esc(fmt.dt(s.ts)) : ""}"><i></i>${esc(s.name)}: ${esc(s.status)}</span>`;
    }).join("");
    const wChip = `<span class="chip ${w.alive ? "good" : "crit"}" title="${w.alive ? "heartbeat " + age(w.age) : "heartbeat нет или устарел"}"><i></i>Бот: ${w.alive ? "РАБОТАЕТ" : "НЕ ЗАПУЩЕН"}</span>`;
    const fwBadge = `<span class="badge ${STATUS_BADGE[fw.status] || ""}">FORWARD: ${STATUS_RU[fw.status]}</span>`;
    const pauseBtn = fw.status === "PAUSED"
      ? `<button class="btn" data-cmd="resume" ${al.has("RESUME") ? "" : "disabled"}>RESUME</button>`
      : `<button class="btn" data-cmd="pause" ${al.has("PAUSE") ? "" : "disabled"}>PAUSE</button>`;
    const q = live.quote, ds = live.ds, ft = fw.totals;
    const fresh = live.quote_fresh;
    let k1;
    if (fwd) {
      k1 = [
        kpi("FABLES live (USDG)", q ? fmt.price(q.mid) : "—", q ? `<span class="${fresh ? "" : "neg"}">${esc(age(live.quote_age))}</span> · ${esc((q.source || "").split(" ")[0])}` : "котировок нет", fresh ? "" : "neg"),
        kpi("FABLES / USD", ds ? fmt.price(ds.price_usd) : "—", ds ? `DexScreener ${age(live.ds_age)}` : ""),
        kpi("Ликвидность", fmt.big(ds?.liquidity_usd), `объём 24ч ${fmt.big(ds?.volume?.h24)}`),
        kpi("Котировка", live.risk?.quote_valid ? "ПРОВЕРЕНА" : q ? "НЕ ПРОВЕРЕНА" : "НЕТ", esc((live.risk?.issues || []).join("; ") || (live.risk?.entry_blockers || []).join("; ") || "источники согласованы"),
            live.risk?.quote_valid ? "pos" : "neg"),
        kpi("Капитал (старт)", fmt.usd(ft?.initial_total ?? 3000, 0), "3 × $1 000"),
        kpi("Стоимость портфелей", ft?.total_equity != null ? fmt.usd(ft.total_equity) : "—", ft?.mark_ts ? `оценка @ ${fmt.time(ft.mark_ts)}` : esc(ft?.note || "FORWARD не запущен")),
        kpi("Net PnL", ft?.net_pnl != null ? fmt.sUsd(ft.net_pnl) : "—", ft?.net_return_pct != null ? fmt.sPct(ft.net_return_pct) : "", fmt.cls(ft?.net_pnl)),
        kpi("Buy & Hold (3×)", ft?.bh_total != null ? fmt.usd(ft.bh_total) : "—", ft?.bh_return_pct != null ? fmt.sPct(ft.bh_return_pct) : "после первой котировки"),
        kpi("vs Buy & Hold", ft?.vs_bh_usd != null ? fmt.sUsd(ft.vs_bh_usd) : "—", "общая оценка", fmt.cls(ft?.vs_bh_usd)),
        kpi("Комиссия hook", live.fees?.BUY ? `${fmt.num(live.fees.BUY / 1e4, 2)}% / ${fmt.num(live.fees.SELL / 1e4, 2)}%` : "—", "BUY / SELL, события Swap"),
        kpi("Газ / своп", live.gas?.usd_per_swap != null ? fmt.usd(live.gas.usd_per_swap, 4) : "—", "eth_gasPrice × 250k"),
        kpi("Начат", fw.started_at ? esc(fmt.dt(fw.started_at)) : "—", esc(fw.experiment || "эксперимента нет")),
      ];
    } else {
      const tr = bt.traders;
      const bestTxt = bt.best.by_return ? `${bt.best.by_return.toUpperCase()} / риск: ${bt.best.by_risk.toUpperCase()}` : "н/д";
      k1 = [
        kpi("FABLES / USDG (набор)", fmt.price(o.price.usdg), `${esc(fmt.time(o.price.ts))} · записанный снимок`),
        kpi("Ликвидность (набор)", fmt.big(o.market?.liquidity_usd), `объём 24ч ${fmt.big(o.market?.volume?.h24)}`),
        kpi("Капитал (старт)", fmt.usd(bt.initial_total, 0), "3 × $1 000"),
        kpi("Стоимость портфелей", fmt.usd(bt.total_equity), `оценка ${fmt.price(o.mark.mid)} @ ${fmt.time(o.mark.ts)}`),
        kpi("Net PnL", fmt.sUsd(bt.net_pnl), fmt.sPct(bt.net_return_pct), fmt.cls(bt.net_pnl)),
        kpi("Buy & Hold (3×)", fmt.usd(bt.bh_total), fmt.sPct(bt.bh_return_pct)),
        kpi("vs Buy & Hold", fmt.sUsd(bt.vs_bh_usd), "та же модель издержек", fmt.cls(bt.vs_bh_usd)),
        kpi("Лучшая стратегия", esc(bestTxt), esc(bt.best.note)),
        kpi("1H / 4H / 1D", `${fmt.num(tr["1h"].equity, 0)} / ${fmt.num(tr["4h"].equity, 0)} / ${fmt.num(tr["1d"].equity, 0)}`, "equity, $"),
        kpi("Данные", esc(fmt.dt(o.dataset.recorded_at)), esc(o.dataset.id)),
      ];
    }
    const el = document.getElementById("topbar");
    el.innerHTML = `
      <div class="topbar-row">
        <div class="brand">FABLES Trading Lab<small>PAPER · v${esc(o.version)}</small></div>
        <nav class="nav">
          <a href="/" class="${page === "terminal" ? "active" : ""}">Терминал</a>
          <a href="/performance" class="${page === "performance" ? "active" : ""}">Performance</a>
        </nav>
        ${page === "terminal" ? `<div class="seg" id="modeSeg">
          <button data-mode="FORWARD" class="${fwd ? "on" : ""}">FORWARD · LIVE</button>
          <button data-mode="BACKTEST" class="${!fwd ? "on" : ""}">BACKTEST</button></div>` : ""}
        <div class="spacer"></div>
        <div class="chips">${wChip}${srcChips}</div>
        <div class="ctl">${fwBadge}
          <button class="btn primary" data-cmd="start" ${al.has("START") && w.alive ? "" : "disabled"} title="${w.alive ? "" : "бот не запущен"}">START</button>
          ${pauseBtn}
          <button class="btn danger" data-cmd="stop" ${al.has("STOP") ? "" : "disabled"}>STOP</button>
          <button class="btn danger" data-cmd="emergency" ${al.has("EMERGENCY_EXIT") ? "" : "disabled"} title="закрыть все виртуальные позиции">EMERGENCY EXIT</button>
        </div>
      </div>
      <div class="kpis">${k1.join("")}</div>`;
    el.querySelectorAll("[data-cmd]").forEach((b) => b.addEventListener("click", () => command(b.dataset.cmd)));
  }

  const CONFIRM = {
    start: "Запустить новый FORWARD-эксперимент?\n\nТри виртуальных портфеля по $1 000 начнут торговать на живых данных FABLES с этого момента. Реальных сделок нет. История до старта не торгуется.",
    stop: "Остановить эксперимент?\n\nАвтоматической продажи НЕТ: открытые виртуальные позиции останутся и будут оцениваться по рынку. Отложенные ордера отменяются, новые решения не принимаются. Продать позиции можно кнопками CLOSE POSITION / EMERGENCY EXIT.",
    emergency: "EMERGENCY EXIT: закрыть ВСЕ открытые виртуальные позиции?\n\nПродажа — по первой проверенной котировке с учётом комиссии, импакта и проскальзывания. Новые входы будут остановлены (PAUSED).",
    close: "Закрыть позицию этого трейдера?\n\nВиртуальная продажа по первой проверенной котировке.",
  };

  async function command(cmd, tf) {
    if (CONFIRM[cmd] && !window.confirm(CONFIRM[cmd])) return;
    try {
      await api(`/api/forward/${cmd}${tf ? "?tf=" + tf : ""}`, { method: "POST" });
    } catch (e) {
      window.alert(`Команда ${cmd.toUpperCase()} не выполнена: ${e.message}`);
    }
    document.dispatchEvent(new CustomEvent("lab:refresh"));
  }

  return { css, fmt, esc, api, store, renderTopbar, command, age, STATUS_RU, STATUS_BADGE };
})();
