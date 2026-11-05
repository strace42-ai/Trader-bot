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

  async function api(path, opts) {
    const r = await fetch(path, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const det = body.detail;
      throw new Error(typeof det === "string" ? det : det?.error || `HTTP ${r.status}`);
    }
    return body;
  }

  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* недоступно — не критично */ } },
  };

  const STATUS_BADGE = { NOT_STARTED: "", RUNNING: "good", PAUSED: "warn", STOPPING: "warn", STOPPED: "crit" };
  const STATUS_RU = { NOT_STARTED: "NOT STARTED", RUNNING: "RUNNING", PAUSED: "PAUSED", STOPPING: "STOPPING", STOPPED: "STOPPED" };

  function kpi(k, v, s, cls = "") {
    return `<div class="kpi"><div class="k">${esc(k)}</div><div class="v ${cls}">${v}</div><div class="s">${s ?? "&nbsp;"}</div></div>`;
  }

  function renderTopbar(o, mode, page) {
    const bt = o.backtest, fw = o.forward;
    const tr = bt.traders;
    const bestTxt = bt.best.by_return ? `${bt.best.by_return.toUpperCase()} / риск: ${bt.best.by_risk.toUpperCase()}` : "н/д";
    const srcChips = o.sources.map((s) => {
      const c = s.status === "RECORDED" ? "good" : s.status === "NOT CONNECTED" ? "idle" : "warn";
      return `<span class="chip ${c}" title="${esc(s.note)}"><i></i>${esc(s.name)}: ${esc(s.status)}</span>`;
    }).join("");
    const fwBadge = `<span class="badge ${STATUS_BADGE[fw.status] || ""}" title="${esc(fw.feed.reason)}">FORWARD: ${STATUS_RU[fw.status]}</span>`;
    const al = new Set(fw.allowed);
    const pauseBtn = fw.status === "PAUSED"
      ? `<button class="btn" data-cmd="resume" ${al.has("RESUME") ? "" : "disabled"}>RESUME</button>`
      : `<button class="btn" data-cmd="pause" ${al.has("PAUSE") ? "" : "disabled"}>PAUSE</button>`;
    const el = document.getElementById("topbar");
    el.innerHTML = `
      <div class="topbar-row">
        <div class="brand">FABLES Trading Lab<small>PAPER · v${esc(o.version)}</small></div>
        <nav class="nav">
          <a href="/" class="${page === "terminal" ? "active" : ""}">Терминал</a>
          <a href="/performance" class="${page === "performance" ? "active" : ""}">Performance</a>
        </nav>
        ${page === "terminal" ? `<div class="seg" id="modeSeg">
          <button data-mode="BACKTEST" class="${mode === "BACKTEST" ? "on" : ""}">BACKTEST</button>
          <button data-mode="FORWARD" class="${mode === "FORWARD" ? "on" : ""}">FORWARD</button></div>` : ""}
        <span class="badge info" title="${esc(bt.experiment)}">BACKTEST · ${esc(bt.validation)}</span>
        <div class="spacer"></div>
        <div class="chips">${srcChips}</div>
        <div class="ctl">${fwBadge}
          <button class="btn primary" data-cmd="start" ${al.has("START") ? "" : "disabled"}>START</button>
          ${pauseBtn}
          <button class="btn danger" data-cmd="stop" ${al.has("STOP") ? "" : "disabled"}>STOP</button>
        </div>
      </div>
      <div class="kpis">
        ${kpi("FABLES / USD", fmt.price(o.price.usd), `${esc(fmt.time(o.price.ts))} · снимок DexScreener`)}
        ${kpi("FABLES / USDG", fmt.price(o.price.usdg), `свеча 1H: ${fmt.price(o.price.last_candle_close)}`)}
        ${kpi("Ликвидность", fmt.big(o.market?.liquidity_usd), `объём 24ч ${fmt.big(o.market?.volume?.h24)}`)}
        ${kpi("Капитал (старт)", fmt.usd(bt.initial_total, 0), "3 × $1 000")}
        ${kpi("Стоимость портфелей", fmt.usd(bt.total_equity), `оценка ${fmt.price(o.mark.mid)} @ ${fmt.time(o.mark.ts)}`)}
        ${kpi("Net PnL", fmt.sUsd(bt.net_pnl), fmt.sPct(bt.net_return_pct), fmt.cls(bt.net_pnl))}
        ${kpi("Buy & Hold (3×)", fmt.usd(bt.bh_total), fmt.sPct(bt.bh_return_pct))}
        ${kpi("vs Buy & Hold", fmt.sUsd(bt.vs_bh_usd), "та же модель издержек", fmt.cls(bt.vs_bh_usd))}
        ${kpi("Лучшая стратегия", esc(bestTxt), esc(bt.best.note))}
        ${kpi("1H / 4H / 1D", `${fmt.num(tr["1h"].equity, 0)} / ${fmt.num(tr["4h"].equity, 0)} / ${fmt.num(tr["1d"].equity, 0)}`, "equity, $")}
        ${kpi("Данные", esc(fmt.dt(o.dataset.recorded_at)), esc(o.dataset.id))}
        ${kpi("Движок", `BT: ${esc(o.engine.backtest)}`, `FW: ${esc(STATUS_RU[o.engine.forward])}`)}
      </div>`;
    el.querySelectorAll("[data-cmd]").forEach((b) => b.addEventListener("click", () => command(b.dataset.cmd)));
  }

  const CONFIRM = {
    start: "Запустить FORWARD paper trading?\n\nБудет создан новый эксперимент с неизменяемым моментом начала и тремя виртуальными портфелями по $1 000.\nРеальных сделок нет. Источник живых данных пока НЕ подключён: новые свечи не поступают, сделок не будет, пока не будет разрешён и подключён сбор данных.",
    stop: "Остановить FORWARD?\n\nНовые входы будут запрещены. Открытые виртуальные позиции закрываются по первой доступной цене. Эксперимент завершается; следующий START создаст новый.",
  };

  async function command(cmd) {
    if (CONFIRM[cmd] && !window.confirm(CONFIRM[cmd])) return;
    try {
      await api(`/api/forward/${cmd}`, { method: "POST" });
    } catch (e) {
      window.alert(`Команда ${cmd.toUpperCase()} не выполнена: ${e.message}`);
    }
    document.dispatchEvent(new CustomEvent("lab:refresh"));
  }

  return { css, fmt, esc, api, store, renderTopbar, STATUS_RU, STATUS_BADGE };
})();
