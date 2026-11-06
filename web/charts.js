/* Minimal canvas charting: candles + overlays, line/area, bars, sparkline.
   Deliberately dependency-free so the terminal works offline and behind any proxy. */
(function () {
  const C = {
    bg: "transparent",
    grid: "rgba(29, 41, 57, 0.9)",
    text: "#5a6b80",
    up: "#2ee6a8",
    down: "#ff5d73",
    ema50: "#4f9dfd",
    ema200: "#a78bfa",
    band: "rgba(79, 157, 253, 0.28)",
    line: "#2ee6a8",
    muted: "#8296ae",
    amber: "#f5b544",
  };

  function setup(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = rect.width || canvas.clientWidth || 720;
    const h = Number(canvas.getAttribute("height")) || 220;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.height = h + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  function niceExtent(min, max, pad) {
    if (!isFinite(min) || !isFinite(max)) return [0, 1];
    if (min === max) { min -= 1; max += 1; }
    const p = (max - min) * (pad == null ? 0.08 : pad);
    return [min - p, max + p];
  }

  function grid(ctx, w, h, pad, minV, maxV, rows) {
    ctx.strokeStyle = C.grid;
    ctx.fillStyle = C.text;
    ctx.lineWidth = 1;
    ctx.font = "10px ui-monospace, monospace";
    const n = rows || 4;
    for (let i = 0; i <= n; i++) {
      const y = pad.t + ((h - pad.t - pad.b) * i) / n;
      ctx.beginPath();
      ctx.moveTo(pad.l, y + 0.5);
      ctx.lineTo(w - pad.r, y + 0.5);
      ctx.stroke();
      const v = maxV - ((maxV - minV) * i) / n;
      ctx.fillText(fmt(v), w - pad.r + 6, y + 3);
    }
  }

  function fmt(v) {
    const a = Math.abs(v);
    if (a >= 1e12) return (v / 1e12).toFixed(2) + "T";
    if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (a >= 1e3) return v.toFixed(0);
    if (a >= 1) return v.toFixed(2);
    return v.toFixed(4);
  }

  function path(ctx, pts, color, width) {
    const valid = pts.filter((p) => p && p[1] != null && isFinite(p[1]));
    if (valid.length < 2) return;
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = width || 1.4;
    ctx.lineJoin = "round";
    valid.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    ctx.stroke();
  }

  /* ---------------------------------------------------------------- candles */
  function candles(canvas, bars, series) {
    const { ctx, w, h } = setup(canvas);
    const pad = { l: 8, t: 12, r: 58, b: 22 };
    const plotW = w - pad.l - pad.r;
    const plotH = h - pad.t - pad.b;
    const all = [];
    bars.forEach((b) => all.push(b.high, b.low));
    (series.bb_upper || []).forEach((v) => v && all.push(v));
    (series.bb_lower || []).forEach((v) => v && all.push(v));
    (series.ema200 || []).forEach((v) => v && all.push(v));
    const [minV, maxV] = niceExtent(Math.min(...all), Math.max(...all));
    const n = bars.length;
    const step = plotW / n;
    const bw = Math.max(1, Math.min(9, step * 0.62));
    const y = (v) => pad.t + ((maxV - v) / (maxV - minV)) * plotH;
    const x = (i) => pad.l + step * (i + 0.5);

    // bollinger band fill
    if (series.bb_upper && series.bb_lower) {
      ctx.beginPath();
      let started = false;
      series.bb_upper.forEach((v, i) => {
        if (v == null) return;
        const px = x(i), py = y(v);
        if (!started) { ctx.moveTo(px, py); started = true; } else ctx.lineTo(px, py);
      });
      for (let i = series.bb_lower.length - 1; i >= 0; i--) {
        const v = series.bb_lower[i];
        if (v == null) continue;
        ctx.lineTo(x(i), y(v));
      }
      ctx.closePath();
      ctx.fillStyle = "rgba(79, 157, 253, 0.06)";
      ctx.fill();
    }

    grid(ctx, w, h, pad, minV, maxV, 5);

    bars.forEach((b, i) => {
      const up = b.close >= b.open;
      ctx.strokeStyle = up ? C.up : C.down;
      ctx.fillStyle = up ? C.up : C.down;
      ctx.lineWidth = Math.max(1, bw * 0.18);
      const cx = x(i);
      ctx.beginPath();
      ctx.moveTo(cx, y(b.high));
      ctx.lineTo(cx, y(b.low));
      ctx.stroke();
      const top = y(Math.max(b.open, b.close));
      const bh = Math.max(1, Math.abs(y(b.open) - y(b.close)));
      ctx.fillRect(cx - bw / 2, top, bw, bh);
    });

    path(ctx, (series.bb_upper || []).map((v, i) => [x(i), v == null ? null : y(v)]), C.band, 0.9);
    path(ctx, (series.bb_lower || []).map((v, i) => [x(i), v == null ? null : y(v)]), C.band, 0.9);
    path(ctx, (series.ema50 || []).map((v, i) => [x(i), v == null ? null : y(v)]), C.ema50, 1.5);
    path(ctx, (series.ema200 || []).map((v, i) => [x(i), v == null ? null : y(v)]), C.ema200, 1.5);

    // last price tag
    const last = bars[bars.length - 1];
    if (last) {
      const py = y(last.close);
      ctx.strokeStyle = "rgba(46, 230, 168, 0.5)";
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(pad.l, py);
      ctx.lineTo(w - pad.r, py);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = C.up;
      ctx.fillRect(w - pad.r + 2, py - 8, pad.r - 4, 16);
      ctx.fillStyle = "#04120d";
      ctx.font = "bold 10px ui-monospace, monospace";
      ctx.fillText(fmt(last.close), w - pad.r + 6, py + 3.5);
    }

    // x labels
    ctx.fillStyle = C.text;
    ctx.font = "10px ui-monospace, monospace";
    const every = Math.max(1, Math.floor(n / 6));
    bars.forEach((b, i) => {
      if (i % every !== 0) return;
      const label = String(b.time || "").replace("T", " ").slice(5, 16);
      ctx.fillText(label, x(i) - 26, h - 6);
    });
  }

  /* ---------------------------------------------------------------- RSI pane */
  function rsi(canvas, values) {
    const { ctx, w, h } = setup(canvas);
    const pad = { l: 8, t: 10, r: 58, b: 16 };
    const plotH = h - pad.t - pad.b;
    const y = (v) => pad.t + ((100 - v) / 100) * plotH;
    [30, 50, 70].forEach((level) => {
      ctx.strokeStyle = level === 50 ? C.grid : "rgba(130,150,174,0.22)";
      ctx.setLineDash(level === 50 ? [] : [3, 3]);
      ctx.beginPath();
      ctx.moveTo(pad.l, y(level));
      ctx.lineTo(w - pad.r, y(level));
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = C.text;
      ctx.font = "9px ui-monospace, monospace";
      ctx.fillText(String(level), w - pad.r + 6, y(level) + 3);
    });
    const step = (w - pad.l - pad.r) / values.length;
    path(ctx, values.map((v, i) => [pad.l + step * (i + 0.5), v == null ? null : y(v)]), C.amber, 1.4);
  }

  /* ---------------------------------------------------------------- line/area */
  function area(canvas, values, opts) {
    const o = opts || {};
    const { ctx, w, h } = setup(canvas);
    const pad = { l: 8, t: 12, r: 58, b: 16 };
    const plotH = h - pad.t - pad.b;
    const nums = values.map((p) => (typeof p === "number" ? p : p.value)).filter((v) => v != null);
    if (!nums.length) return;
    const [minV, maxV] = niceExtent(Math.min(...nums), Math.max(...nums), 0.12);
    const y = (v) => pad.t + ((maxV - v) / (maxV - minV)) * plotH;
    grid(ctx, w, h, pad, minV, maxV, 3);
    const step = (w - pad.l - pad.r) / Math.max(1, values.length - 1);
    const pts = nums.map((v, i) => [pad.l + step * i, y(v)]);
    const grad = ctx.createLinearGradient(0, pad.t, 0, h - pad.b);
    const color = o.color || C.line;
    grad.addColorStop(0, o.fill || "rgba(46,230,168,0.22)");
    grad.addColorStop(1, "rgba(46,230,168,0)");
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    ctx.lineTo(pts[pts.length - 1][0], h - pad.b);
    ctx.lineTo(pts[0][0], h - pad.b);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();
    path(ctx, pts, color, 1.6);
    // baseline (starting equity)
    if (o.baseline != null) {
      ctx.strokeStyle = "rgba(130,150,174,0.35)";
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(pad.l, y(o.baseline));
      ctx.lineTo(w - pad.r, y(o.baseline));
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  /* ---------------------------------------------------------------- bars (+/-) */
  function signedBars(canvas, values, labels) {
    const { ctx, w, h } = setup(canvas);
    const pad = { l: 8, t: 12, r: 58, b: 18 };
    const plotH = h - pad.t - pad.b;
    const m = Math.max(1, ...values.map((v) => Math.abs(v || 0)));
    const mid = pad.t + plotH / 2;
    const step = (w - pad.l - pad.r) / Math.max(1, values.length);
    const bw = Math.min(22, step * 0.62);
    ctx.strokeStyle = C.grid;
    ctx.beginPath();
    ctx.moveTo(pad.l, mid);
    ctx.lineTo(w - pad.r, mid);
    ctx.stroke();
    values.forEach((v, i) => {
      const val = v || 0;
      const bh = (Math.abs(val) / m) * (plotH / 2 - 2);
      const x = pad.l + step * i + step / 2 - bw / 2;
      ctx.fillStyle = val >= 0 ? "rgba(46,230,168,0.75)" : "rgba(255,93,115,0.75)";
      ctx.fillRect(x, val >= 0 ? mid - bh : mid, bw, Math.max(1, bh));
      if (labels && labels[i] && values.length <= 14) {
        ctx.fillStyle = C.text;
        ctx.font = "9px ui-monospace, monospace";
        ctx.fillText(String(labels[i]).slice(5), x - 2, h - 5);
      }
    });
  }

  window.Charts = { candles, rsi, area, signedBars, fmt };
})();
