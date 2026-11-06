/* Trading Office — front-end controller. Plain JS, no build step. */
(function () {
  const state = {
    market: "idx",
    tab: "screen",
    screen: null,
    analysis: null,
    bandar: null,
    backtest: null,
    engine: null,
    metrics: null,
    // (live metrics live in WS.last, see the metrics socket below)
    health: null,
    engineTimer: null,
  };

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  /* ------------------------------------------------------------------ utils */
  async function api(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`${res.status} ${text.slice(0, 200)}`);
    }
    return res.json();
  }

  function toast(message, isError, tone) {
    const stack = $("#toast-container") || document.body;
    const el = document.createElement("div");
    el.className = "toast" + (isError ? " err" : tone ? " " + tone : "");
    const text = document.createElement("span");
    text.textContent = message;
    const close = document.createElement("button");
    close.className = "toast-close";
    close.type = "button";
    close.setAttribute("aria-label", "dismiss");
    close.textContent = "×";
    close.addEventListener("click", () => dismissToast(el));
    el.append(text, close);
    stack.appendChild(el);
    requestAnimationFrame(() => el.classList.add("visible"));
    setTimeout(() => dismissToast(el), isError ? 8000 : 4500);
    return el;
  }

  function dismissToast(el) {
    if (!el || !el.isConnected) return;
    el.classList.remove("visible");
    setTimeout(() => el.remove(), 200);
  }

  // window.confirm blocks the whole page (and the websocket repaints behind it): use a
  // real dialog so the operator sees the book while deciding.
  function confirmAction(title, body, okLabel) {
    const dialog = $("#confirm-dialog");
    if (!dialog || typeof dialog.showModal !== "function") {
      return Promise.resolve(window.confirm(`${title}\n\n${body}`));
    }
    $("#confirm-title").textContent = title;
    $("#confirm-body").textContent = body;
    $("#confirm-ok").textContent = okLabel || "Confirm";
    return new Promise((resolve) => {
      const done = () => {
        dialog.removeEventListener("close", done);
        resolve(dialog.returnValue === "ok");
      };
      dialog.addEventListener("close", done);
      dialog.returnValue = "cancel";
      dialog.showModal();
    });
  }

  const fmtNum = (v, digits) => {
    if (v == null || Number.isNaN(v)) return "—";
    const d = digits == null ? (Math.abs(v) >= 1000 ? 0 : 2) : digits;
    return Number(v).toLocaleString("id-ID", { minimumFractionDigits: d, maximumFractionDigits: d });
  };
  const fmtPct = (v, digits) => (v == null ? "—" : `${v > 0 ? "+" : ""}${Number(v).toFixed(digits == null ? 2 : digits)}%`);
  const fmtCompact = (v) => {
    if (v == null) return "—";
    const a = Math.abs(v);
    if (a >= 1e12) return (v / 1e12).toFixed(2) + " T";
    if (a >= 1e9) return (v / 1e9).toFixed(2) + " B";
    if (a >= 1e6) return (v / 1e6).toFixed(2) + " M";
    if (a >= 1e3) return (v / 1e3).toFixed(1) + " K";
    return String(Math.round(v));
  };
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function chip(label) {
    return `<span class="chip ${esc(label)}">${esc(String(label).replace(/_/g, " "))}</span>`;
  }

  function convictionBar(v) {
    const pct = Math.min(50, (Math.abs(v) / 10) * 50);
    const cls = v >= 0 ? "pos" : "neg";
    return `<div class="conv"><div class="conv-track"><div class="conv-fill ${cls}" style="width:${pct}%"></div></div>
      <span class="conv-val">${v > 0 ? "+" : ""}${Number(v).toFixed(1)}</span></div>`;
  }

  function stat(k, v, s, tone) {
    return `<div class="stat ${tone || ""}"><div class="k">${esc(k)}</div><div class="v">${v}</div>
      ${s ? `<div class="s">${esc(s)}</div>` : ""}</div>`;
  }

  /* ------------------------------------------------------------------ clock */
  function tickClock() {
    const now = new Date();
    const wib = new Date(now.getTime() + (now.getTimezoneOffset() + 420) * 60000);
    const p = (n) => String(n).padStart(2, "0");
    $("#clock").textContent = `${p(wib.getHours())}:${p(wib.getMinutes())}:${p(wib.getSeconds())} WIB`;
  }

  /* ------------------------------------------------------------------ health */
  async function loadHealth() {
    try {
      const health = await api("/api/health");
      state.health = health;
      const idxLive = health.providers?.idx?.available;
      const cryptoLive = health.providers?.crypto?.available;
      const live = state.market === "idx" ? idxLive : cryptoLive;
      const pill = $("#source-pill");
      pill.className = "pill " + (live ? "live" : "demo");
      $("#source-text").textContent = live
        ? `${state.market === "idx" ? "idx CLI live" : health.providers.crypto.exchange + " live"}`
        : "synthetic demo data";
      pill.title = live
        ? "Live provider reachable"
        : "Live providers unreachable in this environment — candles and quotes are deterministic synthetic data, labelled on every row.";
      renderProviders(health);
    } catch (err) {
      const pill = $("#source-pill");
      pill.className = "pill down";
      $("#source-text").textContent = "API offline";
    }
  }

  function renderProviders(health) {
    const box = $("#provider-status");
    if (!box) return;
    const idx = health.providers.idx;
    const crypto = health.providers.crypto;
    box.innerHTML = `
      <div class="prov">
        <div class="head"><span>idx CLI (IDX / IHSG)</span>${idx.available ? chip("LIVE") : chip("HOLD")}</div>
        <div class="line">command: ${esc(idx.command)}</div>
        <div class="line">cached responses: ${idx.cache_ttl_s}s TTL</div>
        <div class="line">${idx.available ? "prices, bandarmology, broker summary, foreign flow, KSEI, corporate actions" : "install: " + esc(idx.install)}</div>
      </div>
      <div class="prov">
        <div class="head"><span>ccxt (${esc(crypto.exchange)})</span>${crypto.available ? chip("LIVE") : chip("HOLD")}</div>
        <div class="line">symbols: ${esc((crypto.symbols || []).join(", "))}</div>
        <div class="line">${crypto.available ? "candles, tickers, order book, trades" : "install: " + esc(crypto.install)}</div>
      </div>
      <div class="prov">
        <div class="head"><span>Execution</span>${health.execution.paper ? chip("WATCH") : chip("SELL")}</div>
        <div class="line">mode: ${health.execution.paper ? "paper broker" : "live venue"}</div>
        <div class="line">exchange: ${esc(health.execution.exchange)} · testnet: ${health.execution.testnet}</div>
        <div class="line">dry run: ${health.execution.dry_run} (no capital at risk)</div>
      </div>
      <div class="prov">
        <div class="head"><span>Data mode</span>${health.data_mode === "live" ? chip("LIVE") : chip("HOLD")}</div>
        <div class="line">${esc(health.data_mode)}</div>
        <div class="line">config: ${esc(health.config_path)}</div>
      </div>`;
  }

  /* ------------------------------------------------------------------ screening */
  async function loadScreen() {
    const tbody = $("#screen-table tbody");
    tbody.innerHTML = `<tr><td colspan="8" class="muted"><span class="spinner"></span> scanning…</td></tr>`;
    const symbols = ($("#screen-symbols").value || "").trim();
    const isIdx = state.market === "idx";
    $("#screen-title").textContent = isIdx ? "IDX screening" : "Crypto screening";
    $("#screen-subtitle").textContent = isIdx
      ? "Market filter → signal → risk over the most liquid names. Bandarmology, foreign flow and valuation feed the conviction score."
      : "Same pipeline on perpetual futures: trend + volatility + session + spread, then the confluence score.";
    try {
      const q = new URLSearchParams({ market: state.market, limit: isIdx ? "18" : "8" });
      if (symbols) q.set("symbols", symbols);
      const data = await api("/api/screen?" + q.toString());
      state.screen = data;
      renderScreen(data);
      if (isIdx) loadMarketIdx(data); else loadMarketCrypto(data);
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="8" class="chg down">${esc(err.message)}</td></tr>`;
      toast("Screening failed: " + err.message, true);
    }
  }

  function renderScreen(data) {
    const rows = data.rows || [];
    const tbody = $("#screen-table tbody");
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="muted">No rows — ${esc((data.failures || []).join("; ") || "empty universe")}</td></tr>`;
    } else {
      tbody.innerHTML = rows
        .map(
          (r) => `<tr data-symbol="${esc(r.symbol)}">
            <td class="sym">${esc(r.symbol)}</td>
            <td>${esc(r.name).slice(0, 26)}</td>
            <td class="num">${fmtNum(r.price)}</td>
            <td class="num chg ${(r.change_pct || 0) >= 0 ? "up" : "down"}">${fmtPct(r.change_pct)}</td>
            <td>${chip(r.label)}</td>
            <td class="num">${r.score}/9</td>
            <td>${convictionBar(r.conviction)}</td>
            <td class="why">${esc((r.reasons || []).slice(0, 2).join(" · "))}</td>
          </tr>`
        )
        .join("");
      tbody.querySelectorAll("tr[data-symbol]").forEach((tr) =>
        tr.addEventListener("click", () => {
          $("#analysis-symbol").value = tr.dataset.symbol;
          $("#bandar-symbol").value = tr.dataset.symbol;
          switchTab("analysis");
          loadAnalysis(tr.dataset.symbol);
        })
      );
    }

    const counts = data.counts || {};
    const buy = (counts.STRONG_BUY || 0) + (counts.BUY || 0);
    const watch = counts.WATCH || 0;
    const sell = (counts.SELL || 0) + (counts.REDUCE || 0);
    const top = rows[0];
    $("#screen-stats").innerHTML = [
      stat("Scanned", data.scanned, `${data.universe.length} in universe`),
      stat("Buy signals", buy, "pipeline-ready + context agrees", buy ? "good" : ""),
      stat("Watch", watch, "setup forming"),
      stat("Reduce / sell", sell, "distribution or breakdown", sell ? "bad" : ""),
      stat("Top conviction", top ? esc(top.symbol) : "—", top ? `conviction ${top.conviction.toFixed(1)}` : "", "good"),
    ].join("");
    $("#screen-meta").textContent = `${data.scanned} scanned · generated ${new Date(data.generated_at).toLocaleTimeString("id-ID")}`;
  }

  async function loadMarketIdx(screenData) {
    const box = $("#market-card");
    box.innerHTML = `<div class="muted">loading market…</div>`;
    try {
      const data = await api("/api/market/idx");
      const ihsg = data.ihsg;
      const b = data.breadth || {};
      const total = (b.advancing || 0) + (b.declining || 0) + (b.unchanged || 0) || 1;
      const movers = (data.top_movers || []).slice(0, 6);
      box.innerHTML = `
        ${ihsg ? `<div class="idx-row"><strong>IHSG</strong>
          <span class="mono chg ${(ihsg.change_pct || 0) >= 0 ? "up" : "down"}">${fmtNum(ihsg.close)} · ${fmtPct(ihsg.change_pct)}</span></div>` : ""}
        <div class="idx-row"><span class="muted">Advancing</span><span class="mono chg up">${b.advancing || 0}</span></div>
        <div class="idx-row"><span class="muted">Declining</span><span class="mono chg down">${b.declining || 0}</span></div>
        <div class="idx-row"><span class="muted">Unchanged</span><span class="mono">${b.unchanged || 0}</span></div>
        <div class="breadth-bar">
          <div class="adv" style="width:${((b.advancing || 0) / total) * 100}%"></div>
          <div class="flat" style="width:${((b.unchanged || 0) / total) * 100}%"></div>
          <div class="dec" style="width:${((b.declining || 0) / total) * 100}%"></div>
        </div>
        <div class="muted small" style="margin:10px 0 6px;text-transform:uppercase;letter-spacing:.6px;font-size:10px">Top movers</div>
        ${movers.map((m) => `<div class="mover"><span class="sym">${esc(m.symbol)}</span>
          <span class="mono chg ${(m.change_pct || 0) >= 0 ? "up" : "down"}">${fmtNum(m.price)} · ${fmtPct(m.change_pct)}</span></div>`).join("")}
        <div class="muted small" style="margin-top:12px">source: ${esc(data.source)}</div>`;
    } catch (err) {
      box.innerHTML = `<div class="chg down">${esc(err.message)}</div>`;
    }
  }

  async function loadMarketCrypto() {
    const box = $("#market-card");
    box.innerHTML = `<div class="muted">loading tickers…</div>`;
    try {
      const data = await api("/api/market/crypto");
      box.innerHTML = data.tickers
        .map(
          (t) => `<div class="idx-row"><span class="sym">${esc(t.symbol.split("/")[0])}</span>
            <span class="mono chg ${(t.change_pct || 0) >= 0 ? "up" : "down"}">${fmtNum(t.price)} · ${fmtPct(t.change_pct)}</span></div>
            <div class="muted small" style="margin:-4px 0 6px">spread ${t.spread_bps} bps · ${esc(t.source || "")}</div>`
        )
        .join("");
    } catch (err) {
      box.innerHTML = `<div class="chg down">${esc(err.message)}</div>`;
    }
  }

  /* ------------------------------------------------------------------ analysis */
  async function loadAnalysis(symbol) {
    const sym = symbol || $("#analysis-symbol").value.trim() || (state.market === "idx" ? "BBCA" : "BTC/USDT:USDT");
    $("#verdict-body").innerHTML = `<div class="muted"><span class="spinner"></span> running the gates…</div>`;
    try {
      const data = await api(`/api/symbol/${state.market}/${encodeURIComponent(sym)}`);
      state.analysis = data;
      renderAnalysis(data);
    } catch (err) {
      toast("Analysis failed: " + err.message, true);
      $("#verdict-body").innerHTML = `<div class="chg down">${esc(err.message)}</div>`;
    }
  }

  function renderAnalysis(data) {
    const scan = data.scan || {};
    const row = data.row || {};
    const plan = scan.risk?.plan;
    $("#chart-title").textContent = `${row.symbol} · ${data.candles?.[0]?.time?.slice(0, 10) || ""} → ${data.candles?.[data.candles.length - 1]?.time?.slice(0, 10) || ""}`;
    $("#chart-legend").innerHTML = `
      <span><i style="background:#4f9dfd"></i>EMA50</span>
      <span><i style="background:#a78bfa"></i>EMA200</span>
      <span><i style="background:rgba(79,157,253,.5)"></i>Bollinger</span>
      <span><i style="background:#f5b544"></i>RSI</span>
      <span class="muted">source: ${esc(data.source || "?")}</span>`;

    const bars = data.candles || [];
    const localSeries = localIndicators(bars);
    if (bars.length) {
      Charts.candles($("#price-chart"), bars, localSeries);
      Charts.rsi($("#rsi-chart"), localSeries.rsi14);
    }

    const action = scan.action || "WAIT";
    const executable = scan.executable;
    const score = scan.score || 0;
    $("#verdict-chip").className = "chip " + (row.label || "HOLD");
    $("#verdict-chip").textContent = (row.label || "—").replace(/_/g, " ");
    $("#verdict-body").innerHTML = `
      <h4>${esc(row.label || "—").replace(/_/g, " ")}</h4>
      <div class="sub">${esc(row.symbol)} · ${fmtNum(scan.price)} · pipeline action ${chip(action)}
        ${executable === false && action === "SHORT" ? '<span class="muted small">(short not executable on IDX cash)</span>' : ""}</div>
      <div class="sub">conviction <b>${(row.conviction ?? 0).toFixed(1)}</b> / 10 · confluence ${esc(String(score))}/9 · stage ${esc(scan.stage || "")}</div>
      ${(row.reasons || []).slice(0, 6).map((r) => `<div class="reason">${esc(r)}</div>`).join("")}
      ${(scan.warnings || []).length ? `<div class="muted small">⚠ ${esc(scan.warnings[0])}</div>` : ""}`;

    renderGates(scan);
    renderPlan(plan, scan, data);
    renderScore(scan);
  }

  function localIndicators(bars) {
    // Recompute the overlays in the browser from the same bars the server sent, so the
    // chart always matches the candles shown (no stale series offsets).
    const closes = bars.map((b) => b.close);
    const ema = (period) => {
      const out = new Array(closes.length).fill(null);
      if (closes.length < period) return out;
      let prev = closes.slice(0, period).reduce((a, b) => a + b, 0) / period;
      out[period - 1] = prev;
      const a = 2 / (period + 1);
      for (let i = period; i < closes.length; i++) {
        prev = closes[i] * a + prev * (1 - a);
        out[i] = prev;
      }
      return out;
    };
    const sma = (period) => {
      const out = new Array(closes.length).fill(null);
      let sum = 0;
      for (let i = 0; i < closes.length; i++) {
        sum += closes[i];
        if (i >= period) sum -= closes[i - period];
        if (i >= period - 1) out[i] = sum / period;
      }
      return out;
    };
    const mid = sma(20);
    const upper = new Array(closes.length).fill(null);
    const lower = new Array(closes.length).fill(null);
    for (let i = 19; i < closes.length; i++) {
      const win = closes.slice(i - 19, i + 1);
      const m = mid[i];
      const sd = Math.sqrt(win.reduce((a, v) => a + (v - m) ** 2, 0) / 20);
      upper[i] = m + 2 * sd;
      lower[i] = m - 2 * sd;
    }
    const rsi = new Array(closes.length).fill(null);
    if (closes.length > 14) {
      let gain = 0, loss = 0;
      for (let i = 1; i <= 14; i++) {
        const d = closes[i] - closes[i - 1];
        if (d > 0) gain += d; else loss -= d;
      }
      gain /= 14; loss /= 14;
      rsi[14] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
      for (let i = 15; i < closes.length; i++) {
        const d = closes[i] - closes[i - 1];
        gain = (gain * 13 + Math.max(d, 0)) / 14;
        loss = (loss * 13 + Math.max(-d, 0)) / 14;
        rsi[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
      }
    }
    return { ema50: ema(50), ema200: ema(200), bb_upper: upper, bb_lower: lower, rsi14: rsi };
  }

  function renderGates(scan) {
    const checks = scan.filter?.checks || [];
    const meta = {
      trend: { title: "1 · Trend filter", hint: "higher timeframe EMA" },
      volatility: { title: "2 · Volatility (ATR)", hint: "dead market / news spike band" },
      session: { title: "3 · Session & data", hint: "trading window, news, freshness" },
      spread: { title: "4 · Spread & liquidity", hint: "cost and depth" },
    };
    $("#gate-grid").innerHTML = checks
      .map((c) => {
        const m = meta[c.name] || { title: c.name, hint: "" };
        return `<div class="gate ${c.passed ? "pass" : "fail"}">
          <div class="gate-head"><span class="gate-name">${esc(m.title)}</span>
          <span class="gate-state">${c.passed ? "PASS" : "BLOCK"}</span></div>
          <div class="gate-detail">${esc(c.detail || m.hint)}</div>
        </div>`;
      })
      .join("");
  }

  function renderPlan(plan, scan, data) {
    const box = $("#plan-body");
    if (!plan) {
      box.innerHTML = `<div class="empty">No plan: ${esc((scan.reasons || [])[0] || "the gates did not produce an entry")}</div>`;
      return;
    }
    const dir = plan.side === "long" ? "long" : "short";
    const unit = plan.market === "idx" ? "IDR" : "USDT";
    box.innerHTML = `
      <div class="plan-row ${dir}"><span>Direction</span><b>${esc(plan.side.toUpperCase())}</b></div>
      <div class="plan-row"><span>Entry (last closed price)</span><b>${fmtNum(plan.entry)}</b></div>
      <div class="plan-row"><span>Stop loss <span class="muted small">(${esc(plan.stop_source)})</span></span><b class="chg down">${fmtNum(plan.stop)}</b></div>
      <div class="plan-row"><span>Take profit <span class="muted small">(${esc(plan.target_source)})</span></span><b class="chg up">${fmtNum(plan.target)}</b></div>
      <div class="plan-row"><span>Reward : risk</span><b>1 : ${Number(plan.rr).toFixed(2)}</b></div>
      <div class="plan-row"><span>Size</span><b>${plan.market === "idx" ? `${fmtNum(plan.lots)} lots` : fmtNum(plan.size, 4) + " units"}</b></div>
      <div class="plan-row"><span>Notional</span><b>${fmtNum(plan.notional)} ${unit}</b></div>
      <div class="plan-row"><span>Risk at stop</span><b>${fmtNum(plan.risk_cash)} ${unit} (${Number(plan.risk_pct).toFixed(2)}%)</b></div>
      <div class="plan-row"><span>Round-trip costs</span><b>${Number(plan.costs?.round_trip_cost_pct || 0).toFixed(3)}%</b></div>
      <div class="plan-row"><span>Expected profit</span><b>${fmtNum(plan.costs?.expected_profit_cash || 0)} ${unit}</b></div>`;
  }

  function renderScore(scan) {
    const comps = scan.signal?.components || [];
    $("#score-meta").textContent = `long ${scan.signal?.long_score ?? 0} · short ${scan.signal?.short_score ?? 0} · min 5`;
    if (!comps.length) {
      $("#score-body").innerHTML = `<div class="empty">${esc((scan.reasons || [])[0] || "signal not evaluated (filter blocked)")}</div>`;
      return;
    }
    $("#score-body").innerHTML = comps
      .map((c) => {
        const on = c.long || c.short;
        const side = c.long && c.short ? "±" : c.long ? "L" : c.short ? "S" : "–";
        return `<div class="score-row">
          <span class="name">${esc(c.name.replace(/_/g, " "))}<br><span class="muted small">${esc(c.detail || "")}</span></span>
          <span class="kind">${esc(c.kind)}</span>
          <span class="reg ${on ? "on" : "off"}">${on ? side + "+" + c.weight : "—"}</span>
        </div>`;
      })
      .join("");
  }

  /* ------------------------------------------------------------------ bandarmology */
  async function loadBandar() {
    const sym = ($("#bandar-symbol").value || "BBCA").trim().toUpperCase();
    const period = $("#bandar-period").value;
    $("#bandar-stats").innerHTML = `<div class="stat"><div class="k">loading</div><div class="v"><span class="spinner"></span></div></div>`;
    if (state.market === "crypto") {
      toast("Bandarmology is an IDX feature — switching to IDX.", false);
      setMarket("idx");
    }
    try {
      const [detail] = await Promise.all([api(`/api/symbol/idx/${encodeURIComponent(sym)}`)]);
      const ctx = detail.context || {};
      const bandar = ctx.bandar || {};
      const foreign = ctx.foreign || {};
      const ownership = ctx.ownership || {};
      const actions = ctx.corporate_actions || {};
      state.bandar = { sym, bandar, foreign, ownership, actions, broker: ctx.broker };

      $("#bandar-stats").innerHTML = [
        stat("Signal", esc(String(bandar.signal || "—").replace(/_/g, " ")), bandar.phase ? esc(String(bandar.phase).replace(/_/g, " ")) : "", /accum/.test(bandar.signal || "") ? "good" : /distrib/.test(bandar.signal || "") ? "bad" : ""),
        stat("Top-5 net", bandar.net_pct == null ? "—" : `${bandar.net_pct > 0 ? "+" : ""}${Number(bandar.net_pct).toFixed(1)}%`, "of value traded"),
        stat("Bandar P/L", bandar.bandar_gain_pct == null ? "—" : fmtPct(bandar.bandar_gain_pct), "top buyers vs last price", (bandar.bandar_gain_pct || 0) >= 0 ? "good" : "bad"),
        stat("Foreign net", fmtCompact(foreign.net_value_at_close), `streak ${foreign.streak ?? 0}`, (foreign.net_value_at_close || 0) >= 0 ? "good" : "bad"),
        stat("KSEI listed", ownership.listed_pct == null ? "—" : `${ownership.listed_pct}%`, ownership.as_of || ""),
      ].join("");

      const broker = ctx.broker || {};
      $("#broker-date").textContent = broker.from ? `${broker.from} → ${broker.to}` : "";
      const buyers = broker.buyers || [];
      const sellers = broker.sellers || [];
      const maxBuy = Math.max(1, ...buyers.map((b) => b.value || 0));
      const maxSell = Math.max(1, ...sellers.map((b) => b.value || 0));
      $("#buyers").innerHTML = buyers.length
        ? buyers.map((b) => `<div class="broker"><span class="rank">${b.rank}</span><span class="code">${esc(b.code)}</span>
            <span class="bar"><i style="width:${((b.value || 0) / maxBuy) * 100}%"></i></span>
            <span class="val">${fmtCompact(b.value)}</span></div>`).join("")
        : `<div class="empty">no broker summary</div>`;
      $("#sellers").innerHTML = sellers.length
        ? sellers.map((s) => `<div class="broker sell"><span class="rank">${s.rank}</span><span class="code">${esc(s.code)}</span>
            <span class="bar"><i style="width:${((s.value || 0) / maxSell) * 100}%"></i></span>
            <span class="val">${fmtCompact(s.value)}</span></div>`).join("")
        : `<div class="empty">no broker summary</div>`;

      const days = foreign.days || [];
      if (days.length) {
        Charts.signedBars($("#foreign-chart"), days.map((d) => d.foreign_net_value_at_close || d.foreign_net || 0),
          days.map((d) => d.date));
        $("#foreign-meta").textContent = `${days[0].date} → ${days[days.length - 1].date}`;
      } else {
        $("#foreign-chart").getContext("2d").clearRect(0, 0, $("#foreign-chart").width, $("#foreign-chart").height);
        $("#foreign-meta").textContent = "no foreign flow data";
      }

      const holders = ownership.top || [];
      $("#ksei-meta").textContent = ownership.as_of ? `as of ${ownership.as_of}` : "";
      $("#ksei").innerHTML = holders.length
        ? holders.map((h) => `<div class="holder"><span class="name">${esc(h.holder)}</span>
            <span><span class="pct">${Number(h.pct).toFixed(2)}%</span>
            <span class="chg ${(h.change_pct || 0) >= 0 ? "up" : "down"}">${h.change_pct == null ? "" : fmtPct(h.change_pct, 2)}</span></span></div>`).join("")
        : `<div class="empty">KSEI register not available (run \`idx ownership sync\`)</div>`;

      const events = actions.actions || [];
      const meetings = actions.meetings || [];
      const items = [
        ...events.map((e) => ({ kind: e.kind, label: e.label || e.kind, when: e.ex_date || e.cum_date, meta: e.amount ? `${fmtNum(e.amount)} / share` : e.status || "" })),
        ...meetings.map((m) => ({ kind: "rups", label: "Shareholders meeting", when: m.date, meta: `${m.time || ""} ${m.venue || ""}` })),
      ];
      $("#actions").innerHTML = items.length
        ? items.map((i) => `<div class="action-row"><span class="kind">${esc(String(i.kind).replace(/_/g, " "))}</span>
            <span>${esc(i.label)}</span><span class="mono muted small">${esc(i.when || "")} ${esc(i.meta || "")}</span></div>`).join("")
        : `<div class="empty">No upcoming corporate actions.</div>`;
    } catch (err) {
      toast("Bandarmology failed: " + err.message, true);
    }
  }

  /* ------------------------------------------------------------------ backtest */
  async function runBacktest() {
    const symbol = ($("#bt-symbol").value || "").trim();
    const market = $("#bt-market").value;
    $("#bt-stats").innerHTML = `<div class="stat"><div class="k">running</div><div class="v"><span class="spinner"></span></div><div class="s">walk-forward on closed candles</div></div>`;
    try {
      const data = await api("/api/backtest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ market, symbol, oos: 0.3, bars: 700 }),
      });
      state.backtest = data;
      renderBacktest(data);
    } catch (err) {
      toast("Backtest failed: " + err.message, true);
      $("#bt-stats").innerHTML = `<div class="stat bad"><div class="k">error</div><div class="v">${esc(err.message).slice(0, 60)}</div></div>`;
    }
  }

  function renderBacktest(data) {
    const oos = data.out_of_sample || data;
    const m = oos.metrics || {};
    const ins = (data.in_sample || {}).metrics || null;
    $("#bt-stats").innerHTML = [
      stat("OOS trades", m.trades, "unseen data only"),
      stat("Win rate", `${m.win_rate_pct}%`, `${m.wins}W / ${m.losses}L`, m.win_rate_pct >= 50 ? "good" : ""),
      stat("Expectancy", `${m.expectancy_r} R`, "per trade", m.expectancy_r > 0 ? "good" : "bad"),
      stat("Profit factor", m.profit_factor == null ? "∞" : m.profit_factor, "gross win / loss", (m.profit_factor || 0) > 1 ? "good" : "bad"),
      stat("Return", fmtPct(m.return_pct), "net of costs", m.return_pct >= 0 ? "good" : "bad"),
      stat("Max drawdown", `${m.max_drawdown_pct ?? 0}%`,
        `mark-to-market ${m.max_drawdown_pct_mtm ?? "—"}%`, (m.max_drawdown_pct || 0) > 10 ? "bad" : ""),
      stat("Fills at the cap", m.costs?.slippage_capped ?? 0, "slippage truncated", ""),
      stat("In-sample", ins ? `${ins.trades} trades · ${ins.expectancy_r}R` : "—", "used for selection", ""),
    ].join("");

    // equity_curve points are {time, equity, peak, drawdown_pct}; older payloads were pairs
    const curve = (oos.equity_curve || []).map((p) => (Array.isArray(p) ? p[1] : p.equity));
    if (curve.length > 1) {
      Charts.area($("#equity-chart"), curve, { baseline: curve[0], color: "#2ee6a8" });
      const dds = (data.drawdown_curve || oos.equity_curve || [])
        .map((p) => (Array.isArray(p) ? 0 : p.drawdown_pct || 0));
      if (dds.length > 1) Charts.drawdown($("#drawdown-chart"), dds);
    }
    $("#bt-meta").textContent = `${data.symbol || ""} · ${oos.bars || 0} OOS bars · params ${JSON.stringify(data.chosen_params || {})}`;

    const funnel = m.rejections || {};
    const fblock = m.filters_blocked || {};
    const all = [
      ...Object.entries(fblock).map(([k, v]) => [`filter: ${k}`, v]),
      ...Object.entries(funnel).map(([k, v]) => [k.replace(/_/g, " "), v]),
    ].sort((a, b) => b[1] - a[1]).slice(0, 8);
    const max = Math.max(1, ...all.map((a) => a[1]));
    $("#bt-funnel").innerHTML = all.length
      ? all.map(([k, v]) => `<div class="funnel-row"><span class="muted">${esc(k)}</span>
          <span class="bar"><i style="width:${(v / max) * 100}%"></i></span><span class="n">${v}</span></div>`).join("")
      : `<div class="empty">no rejections recorded</div>`;

    const tbody = $("#bt-table tbody");
    const trades = (oos.trades || []).slice().reverse();
    tbody.innerHTML = trades.length
      ? trades.map((t, i) => `<tr>
          <td class="mono">${trades.length - i}</td>
          <td>${chip(t.side === "long" ? "LONG" : "SHORT")}</td>
          <td class="num">${fmtNum(t.entry)}</td>
          <td class="num">${fmtNum(t.stop)}</td>
          <td class="num">${fmtNum(t.target)}</td>
          <td class="num">${fmtNum(t.exit)}</td>
          <td class="num chg ${t.r_multiple >= 0 ? "up" : "down"}">${t.r_multiple}</td>
          <td class="num chg ${t.pnl >= 0 ? "up" : "down"}">${fmtNum(t.pnl)}</td>
          <td class="why">${esc(t.reason)}</td>
          <td class="why">${esc((t.entry_reason || "").slice(0, 90))}</td>
        </tr>`).join("")
      : `<tr><td colspan="10" class="muted">No trades in the out-of-sample window — the filters were strict. Check the skip funnel on the right.</td></tr>`;
  }

  /* ------------------------------------------------------------------ live metrics (WS) */
  const WS = {
    sock: null,
    attempts: 0,
    base: 1000,          // 1 s
    cap: 30000,          // 30 s
    timer: null,
    last: null,          // last frame, so a tab switch can repaint without waiting
    healthy: false,
  };

  function wsUrl() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.host}/api/ws/metrics?market=${encodeURIComponent(state.market)}`;
  }

  function setWsStatus(text, tone) {
    const label = $("#ws-status-indicator");
    const dot = $("#ws-dot");
    if (label) label.textContent = text;
    if (dot) dot.className = "dot" + (tone ? " " + tone : "");
  }

  function connectMetricsSocket() {
    if (WS.sock && (WS.sock.readyState === WebSocket.OPEN || WS.sock.readyState === WebSocket.CONNECTING)) return;
    clearTimeout(WS.timer);
    let sock;
    try {
      sock = new WebSocket(wsUrl());
    } catch (err) {
      scheduleReconnect("constructor failed");
      return;
    }
    WS.sock = sock;
    setWsStatus(WS.attempts ? `reconnecting (attempt ${WS.attempts + 1})…` : "connecting…", "warn");

    sock.onopen = () => {
      WS.attempts = 0;
      WS.healthy = true;
      setWsStatus("live · websocket", "live");
    };
    sock.onmessage = (event) => {
      let frame;
      try { frame = JSON.parse(event.data); } catch (err) { return; }
      if (frame.type === "stale") { setWsStatus("stale connection, reconnecting…", "warn"); return; }
      if (frame.type !== "metrics") return;
      WS.last = frame;
      renderMetrics(frame);
      // answer the server ping so it knows this tab is alive
      if (sock.readyState === WebSocket.OPEN) sock.send(JSON.stringify({ type: "pong", seq: frame.seq }));
      setWsStatus("live · websocket", "live");
    };
    sock.onclose = (event) => {
      WS.healthy = false;
      // a clean close after a market switch is not a failure: just reconnect
      scheduleReconnect(`closed (${event.code || "?"})`);
    };
    sock.onerror = () => {
      WS.healthy = false;
      setWsStatus("socket error", "off");
      try { sock.close(); } catch (err) { /* onclose handles the retry */ }
    };
  }

  function scheduleReconnect(reason) {
    WS.attempts += 1;
    const backoff = Math.min(WS.cap, WS.base * Math.pow(2, WS.attempts - 1));
    const jitter = Math.random() * 400;                 // desync many tabs
    const delay = Math.round(backoff + jitter);
    setWsStatus(`offline · retrying in ${(delay / 1000).toFixed(1)}s`, "warn");
    if (reason) console.warn("[metrics ws]", reason);
    clearTimeout(WS.timer);
    WS.timer = setTimeout(connectMetricsSocket, delay);
    // while the socket is down, keep the cards honest with a single HTTP snapshot
    api(`/api/metrics?market=${state.market}`).then((frame) => {
      WS.last = { ...frame, type: "metrics", transport: "http" };
      renderMetrics(WS.last);
    }).catch(() => {});
  }

  function closeMetricsSocket() {
    clearTimeout(WS.timer);
    if (WS.sock) { try { WS.sock.close(); } catch (err) { /* ignore */ } }
    WS.sock = null;
    WS.healthy = false;
  }

  function fmtFactor(value) {
    if (value === null || value === undefined) return "—";
    return Number.isFinite(value) ? value.toFixed(2) : "∞";
  }

  function bookRows(frame) {
    const trades = frame.active_trades || [];
    const orders = frame.open_orders || [];
    if (!trades.length && !orders.length) return "";
    const gate = frame.execution_gate || state.engine?.execution_gate || {};
    const simTag = gate.simulated ? `<span class="tag sim">${esc(gate.broker === "paper" ? "paper" : "dry-run")}</span>` : "";
    const rows = [];
    for (const t of trades) {
      const pnl = Number(t.pnl || 0);
      const tags = [
        t.partial_done ? '<span class="tag partial">½ banked</span>' : "",
        simTag,
      ].join("");
      rows.push(`<tr>
        <td class="sym">${esc(t.symbol)}</td>
        <td>${chip(t.side)}${tags}</td>
        <td>${fmtNum(t.size, 6)}</td>
        <td>${fmtNum(t.entry)}</td>
        <td>${fmtNum(t.stop)}</td>
        <td>${fmtNum(t.target)}</td>
        <td>${fmtNum(t.mark)}</td>
        <td class="pnl ${pnl >= 0 ? "up" : "down"}">${pnl >= 0 ? "+" : ""}${fmtNum(pnl)}</td>
        <td class="${(t.r_multiple || 0) >= 0 ? "pnl up" : "pnl down"}">${Number(t.r_multiple || 0).toFixed(2)}R</td>
        <td class="ta-right"><button class="btn-mini" data-action="close" data-symbol="${esc(t.symbol)}">CLOSE</button></td>
      </tr>`);
    }
    for (const o of orders) {
      const side = String(o.side || "").toUpperCase();
      rows.push(`<tr>
        <td class="sym">${esc(o.symbol || "")}</td>
        <td>${esc(String(o.type || "order").toUpperCase())} ${side ? chip(side) : ""}${
          o.ours === false ? '<span class="tag ghost">untracked</span>' : ""}</td>
        <td>${fmtNum(o.amount, 6)}</td>
        <td>${o.price ? fmtNum(o.price) : "—"}</td>
        <td class="muted-cell">—</td>
        <td class="muted-cell">${o.reduce_only ? "reduce-only" : "—"}</td>
        <td class="muted-cell">${esc(o.status || "open")}</td>
        <td class="muted-cell">pending</td>
        <td class="muted-cell">—</td>
        <td class="ta-right"><button class="btn-mini ghost" data-action="cancel"
          data-order="${esc(o.id || "")}" data-symbol="${esc(o.symbol || "")}">CANCEL</button></td>
      </tr>`);
    }
    return rows.join("");
  }

  function renderMetrics(frame) {
    const kpi = frame.kpi || (frame.performance
      ? {
          win_rate: frame.performance.win_rate_pct,
          profit_factor: frame.performance.profit_factor,
          total_trades: frame.performance.trades,
          max_drawdown_pct: frame.performance.max_drawdown_pct,
          net_profit_pct: frame.performance.net_profit_pct,
          equity: frame.performance.ending_equity,
        }
      : {});
    const wr = Number(kpi.win_rate || 0);
    const pf = kpi.profit_factor;
    const set = (id, value) => { const el = $("#" + id); if (el) el.textContent = value; };
    set("kpi-win-rate", kpi.total_trades ? `${wr.toFixed(1)}%` : "—");
    set("kpi-profit-factor", fmtFactor(pf));
    set("kpi-total-trades", kpi.total_trades ?? 0);
    set("kpi-win-rate-s", kpi.total_trades ? `${Math.round((wr / 100) * kpi.total_trades)} winners` : "no closed trades yet");
    set("kpi-profit-factor-s", pf == null ? "no losing trade in the sample" : pf >= 1.5 ? "healthy (≥ 1.5)" : "below 1.5");
    set("kpi-max-dd", `${Number(kpi.max_drawdown_pct || 0).toFixed(2)}%`);
    set("kpi-net-profit", `${Number(kpi.net_profit_pct || 0).toFixed(2)}%`);
    set("kpi-equity", fmtNum(kpi.equity));
    const wrCard = $("#kpi-win-rate")?.closest(".kpi-card");
    if (wrCard) wrCard.dataset.tone = !kpi.total_trades ? "" : wr >= 50 ? "good" : "bad";
    const pfCard = $("#kpi-profit-factor")?.closest(".kpi-card");
    if (pfCard) pfCard.dataset.tone = pf == null ? "blue" : pf >= 1.5 ? "blue" : "bad";

    const series = frame.series || [];
    const equity = series.map((p) => p.equity);
    const dd = series.map((p) => p.drawdown_pct || 0);
    if (equity.length > 1) {
      Charts.area($("#live-equity"), equity, { baseline: equity[0] });
      Charts.drawdown($("#live-drawdown"), dd);
    }
    const worst = dd.length ? Math.max(...dd) : 0;
    set("eng-dd-meta", `max ${worst.toFixed(2)}% · now ${(dd[dd.length - 1] || 0).toFixed(2)}%`);
    set("eng-equity-meta",
      `${equity.length} points${frame.transport === "http" ? " · http fallback" : " · live"}`);

    // active trades & open orders (rendered on every frame: the P&L mark moves)
    const body = $("#book-rows");
    if (body) {
      const rows = bookRows(frame);
      body.innerHTML = rows || `<tr class="empty-row"><td colspan="10">No open positions or resting orders.</td></tr>`;
    }
    const gate = frame.execution_gate || {};
    set("book-meta", `${(frame.active_trades || []).length} position(s) · ${(frame.open_orders || []).length} resting order(s)`
      + (gate.note ? ` · ${gate.note}` : ""));
  }

  async function closePosition(symbol) {
    const ok = await confirmAction(
      `Close ${symbol}?`,
      "A reduce-only market order will flatten this position at the current price. The resting stop/target are cancelled and the trade is booked in the ledger.",
      "Close position",
    );
    if (!ok) return;
    try {
      const result = await api("/api/trade/close", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ market: state.market, symbol }),
      });
      if (result.ok) {
        const pnl = result.closed?.pnl ?? 0;
        toast(`${symbol} closed · P&L ${pnl >= 0 ? "+" : ""}${fmtNum(pnl)}`, false);
        await loadEngine();
      } else {
        toast(`Close refused: ${result.reason || "unknown error"}`, true);
      }
    } catch (err) {
      toast("Close failed: " + err.message, true);
    }
  }

  async function cancelOrder(orderId, symbol) {
    const ok = await confirmAction(
      "Cancel order?",
      `Order ${orderId} on ${symbol || "the venue"} will be cancelled. If it is the stop of an open position, that position is left unprotected.`,
      "Cancel order",
    );
    if (!ok) return;
    try {
      const result = await api("/api/order/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ market: state.market, order_id: orderId, symbol: symbol || null }),
      });
      if (result.ok) {
        toast(`Order ${orderId} cancelled`, false);
        await loadEngine();
      } else {
        toast(`Cancel refused: ${result.reason || "unknown error"}`, true);
      }
    } catch (err) {
      toast("Cancel failed: " + err.message, true);
    }
  }

  /* ------------------------------------------------------------------ engine */
  async function engineCall(path, body) {
    try {
      const data = await api(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || { market: state.market }),
      });
      toast(path.split("/").pop().replace("-", " ") + " ok");
      await loadEngine();
      return data;
    } catch (err) {
      toast("Engine: " + err.message, true);
    }
  }

  async function loadEngine() {
    try {
      const data = await api(`/api/engine/status?market=${state.market}`);
      state.engine = data;
      renderEngine(data, WS.last);
      if (!WS.healthy) connectMetricsSocket();
    } catch (err) {
      toast("Engine status failed: " + err.message, true);
    }
  }

  function renderEngine(data, metrics) {
    const st = data.status || {};
    const broker = data.broker || {};
    $("#eng-stats").innerHTML = [
      stat("Engine", st.running ? "LIVE LOOP" : "STOPPED", st.mode, st.running ? "good" : ""),
      stat("Equity", fmtNum(broker.equity ?? st.equity), broker.realised_pnl != null ? `realised ${fmtNum(broker.realised_pnl)}` : "", ""),
      stat("Scans", st.scans, `${st.cycle_count} cycles`),
      stat("Orders", st.orders, "bar-locked", st.orders ? "good" : ""),
      stat("Blocked", st.blocked, "filter / signal / risk", st.blocked ? "warn" : ""),
      stat("Positions", st.open_positions, "one per symbol"),
      stat("Partials", st.partials, "50% at 2R, reduce-only", st.partials ? "good" : ""),
      stat("Gap blocks", Object.keys(data.gap_cooldowns || {}).length, "black-swan cooldown",
        Object.keys(data.gap_cooldowns || {}).length ? "warn" : ""),
      stat("Kill switch", st.guard?.blocked ? "OPEN" : "armed",
        st.guard?.blocked ? (st.guard.reasons || []).join("; ") : "daily loss / loss streak within limits",
        st.guard?.blocked ? "bad" : "good"),
    ].join("");

    renderReconcile(data.status?.reconciled || {}, data.gap_cooldowns || {});

    // the open-position cards now live in the Active trades table (with actions), so the
    // engine tab has one book view instead of two that can disagree
    if (!metrics) {
      renderMetrics({ series: data.equity_curve || [], performance: data.performance,
                      active_trades: data.active_trades, open_orders: data.open_orders,
                      execution_gate: data.execution_gate });
    }

    // charts + KPI cards are driven by the websocket frame (or its HTTP fallback)
    const perf = (metrics && metrics.performance) || data.performance || {};
    $("#eng-perf").innerHTML = [
      stat("Profit factor", fmtFactor(perf.profit_factor), "gross win ÷ loss",
        (perf.profit_factor || 0) >= 1.5 ? "good" : perf.profit_factor == null ? "" : "bad"),
      stat("Net profit", `${(perf.net_profit_pct ?? 0).toFixed(2)}%`, `equity ${fmtNum(perf.ending_equity)}`,
        (perf.net_profit_pct || 0) >= 0 ? "good" : "bad"),
      stat("Scans / orders", `${st.scans ?? 0} / ${st.orders ?? 0}`, "bar-locked", ""),
      stat("Last cycle", st.last_cycle_at ? new Date(st.last_cycle_at).toLocaleTimeString("id-ID") : "not started",
        st.running ? "loop running" : "loop stopped", st.running ? "good" : ""),
    ].join("");

    const ledger = (data.ledger || []).slice().reverse().slice(0, 60);
    $("#eng-ledger").innerHTML = ledger.length
      ? ledger.map((e) => `<div class="ledger-row">
          <span class="ts">${esc(new Date(e.ts).toLocaleString("id-ID"))}</span>
          <span class="ev ${esc(e.event)}">${esc(e.event)}</span>
          <span class="body">${esc(ledgerBody(e))}</span></div>`).join("")
      : `<div class="empty">The ledger is empty — start the engine or run one cycle.</div>`;
  }

  function renderReconcile(rec, cooldowns) {
    const rows = [
      ["Venue", rec.venue_available === true ? "reachable" : rec.venue_available === false ? "no answer" : "not run yet",
        rec.venue_available === false ? "warn" : "good"],
      ["Restored", rec.restored ?? 0, "state matches the exchange"],
      ["Adopted", rec.adopted ?? 0, "exchange position re-attached"],
      ["Closed", rec.closed ?? 0, "exchange was flat: ghost dropped"],
      ["Untracked", rec.untracked ?? 0, "exchange position we never authorised"],
      ["Blocked", rec.blocked ?? 0, "cannot verify: no new entries"],
      ["Missing stops", rec.stops_missing ?? 0, "resting stop not found"],
    ];
    const gaps = Object.entries(cooldowns);
    $("#eng-reconcile").innerHTML =
      `<div class="recon-grid">${rows.map(([k, v, note, tone]) => `
        <div class="recon-cell ${tone && v ? tone : ""}">
          <span class="k">${esc(k)}</span>
          <b>${esc(String(v))}</b>
          <span class="muted small">${esc(note)}</span>
        </div>`).join("")}</div>` +
      (gaps.length
        ? `<div class="muted small">Entry cooldowns: ${gaps.map(([s, n]) => `${esc(s)} (${n})`).join(", ")}</div>`
        : "") +
      (rec.at ? `<div class="muted small">last sync ${new Date(rec.at).toLocaleString("id-ID")}</div>` : "");
  }

  function ledgerBody(e) {
    if (e.event === "scan") return `${e.symbol} ${e.action} score ${e.score}/9 · ${(e.reasons || []).join("; ")}`;
    if (e.event === "skip") return `${e.symbol}: ${e.reason}`;
    if (e.event === "order") return `${e.symbol} ${e.order?.side} ${e.order?.quantity} @ ${e.fill?.price} · ${e.fill?.message}`;
    if (e.event === "trade_closed") return `${e.symbol} ${e.side} pnl ${fmtNum(e.pnl)} (${e.r_multiple}R) · ${e.reason}`;
    if (e.event === "manage") return `${e.symbol} ${e.action?.kind} · ${e.action?.reason}`;
    if (e.event === "partial") return `${e.symbol} closed ${e.size} @ ${fmtNum(e.price)} · pnl ${fmtNum(e.pnl)} · ${e.reason}`;
    if (e.event === "reconciliation") {
      return `venue ${e.venue_available ? "reachable" : "unavailable"} · restored ${e.restored?.length ?? 0} · `
        + `adopted ${e.adopted?.length ?? 0} · closed ${e.closed?.length ?? 0} · untracked ${e.untracked?.length ?? 0}`;
    }
    if (e.event === "cost_deferred") return `${e.symbol}: ${e.reason}`;
    return JSON.stringify(e).slice(0, 140);
  }

  /* ------------------------------------------------------------------ api tab */
  async function loadApiTab() {
    $("#cli-snippets").textContent = [
      "# screening (the same pipeline the UI uses)",
      "python -m trading_office.cli screen --market idx --limit 20",
      "python -m trading_office.cli screen --market crypto --symbols BTC/USDT:USDT,ETH/USDT:USDT",
      "",
      "# one symbol, every gate explained",
      "python -m trading_office.cli analyse --market idx --symbol BBCA",
      "",
      "# bandarmology / broker summary / foreign flow / KSEI / corporate actions",
      "python -m trading_office.cli bandar --symbol BBCA --period 1w",
      "python -m trading_office.cli broker --symbol BBCA",
      "python -m trading_office.cli foreign --symbol BBCA --days 20",
      "python -m trading_office.cli ownership --symbol BBCA",
      "python -m trading_office.cli actions --symbol BBCA",
      "",
      "# backtest on unseen data (in-sample selection, out-of-sample report)",
      "python -m trading_office.backtest --market crypto --data data/crypto_BTCUSDT_15m.json --oos 0.3",
      "",
      "# what a fill really costs (latency, ATR slippage, gap guards)",
      "python -m trading_office.cli costs --market crypto",
      "",
      "# reconcile after a crash/restart (adopt what the exchange really has)",
      "python -m trading_office.cli reconcile --market crypto --adopt",
      "",
      "# live engine: paper by default, one decision per closed bar",
      "python -m trading_office.engine --market crypto --symbols BTC/USDT:USDT,ETH/USDT:USDT",
      "",
      "# REST",
      "curl -s localhost:8000/api/screen?market=idx | jq '.rows[:3] | .[] | {symbol,label,conviction}'",
      "curl -s -X POST localhost:8000/api/backtest -H 'content-type: application/json' \\",
      "  -d '{\"market\":\"crypto\",\"symbol\":\"BTC/USDT:USDT\",\"oos\":0.3}' | jq '.out_of_sample.metrics'",
    ].join("\n");

    try {
      const data = await api("/api/mcp/tools");
      $("#mcp-snippets").textContent = [
        "# stdio (Claude Code, Cursor, Codex, Zed, …)",
        "python -m trading_office.api.mcp_server",
        "",
        "# streamable HTTP for URL-only clients",
        "python -m trading_office.api.mcp_server --http --port 8765",
        "# then add  http://<host>:8765/mcp  as an MCP connector",
        "",
        "# Claude Code",
        data.registration.claude_code,
        "",
        "# Codex",
        data.registration.codex,
        "",
        "# Claude Desktop / Cursor (settings.json)",
        data.registration.claude_desktop,
      ].join("\n");
      $("#tool-list").innerHTML = data.tools
        .map((t) => `<div class="tool"><div class="name">${esc(t.name)}</div>
          <div class="desc">${esc(t.description)}</div>
          <div class="params">${esc(Object.entries(t.parameters).map(([k, v]) => `${k}: ${v}`).join("  "))}</div></div>`)
        .join("");
    } catch (err) {
      toast("Could not load the tool catalog: " + err.message, true);
    }
  }

  /* ------------------------------------------------------------------ navigation */
  function switchTab(name) {
    state.tab = name;
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
    if (name === "engine") {
      loadEngine();
      connectMetricsSocket();
      if (WS.last) renderMetrics(WS.last);
    } else {
      closeMetricsSocket();
    }
    if (name === "api") loadApiTab();
    if (name === "bandar" && !state.bandar) loadBandar();
  }

  function setMarket(market) {
    state.market = market;
    $$(".switch-btn").forEach((b) => b.classList.toggle("active", b.dataset.market === market));
    $("#analysis-symbol").value = market === "idx" ? "BBCA" : "BTC/USDT:USDT";
    $("#bt-symbol").value = market === "idx" ? "BBCA" : "BTC/USDT:USDT";
    $("#bt-market").value = market;
    loadHealth();
    loadScreen();
    if (state.tab === "engine") {
      // the socket URL embeds the market, so a switch needs a fresh connection
      closeMetricsSocket();
      WS.attempts = 0;
      loadEngine();
      connectMetricsSocket();
    }
  }

  /* ------------------------------------------------------------------ boot */
  function bind() {
    $$(".switch-btn").forEach((b) => b.addEventListener("click", () => setMarket(b.dataset.market)));
    $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
    $("#screen-run").addEventListener("click", loadScreen);
    $("#analysis-run").addEventListener("click", () => loadAnalysis());
    $("#analysis-symbol").addEventListener("keydown", (e) => { if (e.key === "Enter") loadAnalysis(); });
    $("#bandar-run").addEventListener("click", loadBandar);
    $("#bandar-period").addEventListener("change", loadBandar);
    $("#bt-run").addEventListener("click", runBacktest);
    $("#eng-start").addEventListener("click", () => engineCall("/api/engine/start", { market: state.market }));
    $("#eng-stop").addEventListener("click", () => engineCall("/api/engine/stop", { market: state.market }));
    $("#eng-cycle").addEventListener("click", () => engineCall("/api/engine/cycle", { market: state.market }));
    $("#eng-flat").addEventListener("click", () => engineCall("/api/engine/close-all", { market: state.market }));
    $("#eng-sync").addEventListener("click", () => engineCall("/api/engine/reconcile", { market: state.market }));
    $("#eng-resume").addEventListener("click", () => engineCall("/api/engine/resume", { market: state.market }));
    $("#book-rows")?.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const action = button.dataset.action;
      button.disabled = true;
      const done = () => { button.disabled = false; };
      if (action === "close") closePosition(button.dataset.symbol).finally(done);
      else if (action === "cancel") cancelOrder(button.dataset.order, button.dataset.symbol).finally(done);
    });
    window.addEventListener("resize", () => {
      if (state.analysis && state.tab === "analysis") renderAnalysis(state.analysis);
      if (state.backtest && state.tab === "backtest") renderBacktest(state.backtest);
      if (state.engine && state.tab === "engine") {
        renderEngine(state.engine, WS.last);
        if (WS.last) renderMetrics(WS.last);
      }
    });
  }

  setInterval(tickClock, 1000);
  // metrics arrive over the websocket; this only refreshes the engine *status* card
  setInterval(() => {
    if (state.tab === "engine") loadEngine();
  }, 5000);
  tickClock();
  bind();
  loadHealth().then(() => {
    loadScreen();
    loadAnalysis(state.market === "idx" ? "BBCA" : "BTC/USDT:USDT");
  });
})();
