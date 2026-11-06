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

  function toast(message, isError) {
    const el = document.createElement("div");
    el.className = "toast" + (isError ? " err" : "");
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 5200);
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
      stat("Max drawdown", `${m.max_drawdown_pct}%`, "peak to trough", m.max_drawdown_pct > 10 ? "bad" : ""),
      stat("In-sample", ins ? `${ins.trades} trades · ${ins.expectancy_r}R` : "—", "used for selection", ""),
    ].join("");

    const curve = (oos.equity_curve || []).map((p) => p[1]);
    if (curve.length > 1) {
      Charts.area($("#equity-chart"), curve, { baseline: curve[0], color: "#2ee6a8" });
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
      renderEngine(data);
    } catch (err) {
      toast("Engine status failed: " + err.message, true);
    }
  }

  function renderEngine(data) {
    const st = data.status || {};
    const broker = data.broker || {};
    $("#eng-stats").innerHTML = [
      stat("Engine", st.running ? "LIVE LOOP" : "STOPPED", st.mode, st.running ? "good" : ""),
      stat("Equity", fmtNum(broker.equity ?? st.equity), broker.realised_pnl != null ? `realised ${fmtNum(broker.realised_pnl)}` : "", ""),
      stat("Scans", st.scans, `${st.cycle_count} cycles`),
      stat("Orders", st.orders, "bar-locked", st.orders ? "good" : ""),
      stat("Blocked", st.blocked, "filter / signal / risk", st.blocked ? "warn" : ""),
      stat("Positions", st.open_positions, "one per symbol"),
    ].join("");

    const positions = data.positions || [];
    $("#eng-positions").innerHTML = positions.length
      ? positions.map((p) => `<div class="pos-card">
          <div class="top"><strong class="sym">${esc(p.symbol)}</strong>${chip(p.side === "long" ? "LONG" : "SHORT")}</div>
          <div class="pos-grid">
            <span>Entry<b>${fmtNum(p.entry)}</b></span>
            <span>Stop<b>${fmtNum(p.stop)}</b></span>
            <span>Target<b>${fmtNum(p.target)}</b></span>
            <span>Unrealised<b class="chg ${(p.unrealised || 0) >= 0 ? "up" : "down"}">${fmtNum(p.unrealised || 0)}</b></span>
          </div></div>`).join("")
      : `<div class="empty">No open positions.</div>`;

    const curve = (data.equity_curve || []).map((p) => p.equity);
    if (curve.length > 1) Charts.area($("#live-equity"), curve, { baseline: curve[0] });
    $("#eng-equity-meta").textContent = `${curve.length} points · ${st.last_cycle_at ? new Date(st.last_cycle_at).toLocaleTimeString("id-ID") : "not started"}`;

    const ledger = (data.ledger || []).slice().reverse().slice(0, 60);
    $("#eng-ledger").innerHTML = ledger.length
      ? ledger.map((e) => `<div class="ledger-row">
          <span class="ts">${esc(new Date(e.ts).toLocaleString("id-ID"))}</span>
          <span class="ev ${esc(e.event)}">${esc(e.event)}</span>
          <span class="body">${esc(ledgerBody(e))}</span></div>`).join("")
      : `<div class="empty">The ledger is empty — start the engine or run one cycle.</div>`;
  }

  function ledgerBody(e) {
    if (e.event === "scan") return `${e.symbol} ${e.action} score ${e.score}/9 · ${(e.reasons || []).join("; ")}`;
    if (e.event === "skip") return `${e.symbol}: ${e.reason}`;
    if (e.event === "order") return `${e.symbol} ${e.order?.side} ${e.order?.quantity} @ ${e.fill?.price} · ${e.fill?.message}`;
    if (e.event === "trade_closed") return `${e.symbol} ${e.side} pnl ${fmtNum(e.pnl)} (${e.r_multiple}R) · ${e.reason}`;
    if (e.event === "manage") return `${e.symbol} ${e.action?.kind} · ${e.action?.reason}`;
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
    if (name === "engine") loadEngine();
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
    if (state.tab === "engine") loadEngine();
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
    window.addEventListener("resize", () => {
      if (state.analysis && state.tab === "analysis") renderAnalysis(state.analysis);
      if (state.backtest && state.tab === "backtest") renderBacktest(state.backtest);
      if (state.engine && state.tab === "engine") renderEngine(state.engine);
    });
  }

  setInterval(tickClock, 1000);
  setInterval(() => { if (state.tab === "engine" && state.engine?.status?.running) loadEngine(); }, 8000);
  tickClock();
  bind();
  loadHealth().then(() => {
    loadScreen();
    loadAnalysis(state.market === "idx" ? "BBCA" : "BTC/USDT:USDT");
  });
})();
