(() => {
  const API = window.location.origin;
  let ws = null;
  let chart = null;
  let candleSeries = null;
  let slLine = null;
  let tpLine = null;
  let currentSpeed = 2;
  let loadedCandles = [];
  let chartTimeframe = "1s";
  let botInstances = [];
  let scenarioCatalog = [];
  let selectedBotId = null;
  let selectedTradeId = null;
  let selectedTradeHighlight = null;
  let botsRuntime = { bots: [], active_trade: null, highlight: null };
  let lastCurrentMs = 0;
  let playerProfile = { wallet_usdt: 100, target_monthly_pct: 30 };
  let allPoolPairs = [];
  let preferredPairs = [];
  let selectedSimPairs = new Set();
  let mlPnl = { ready: false, model: "", byId: new Map() };
  let mlGate = { enabled: true, ready: false, model: "" };
  let uiMode = "sim"; // sim | chart | live | grid
  let liveEnabledSnapshot = null;
  let livePairsSnapshot = null;
  let selectedLiveTradeId = null;
  let gridScan = { best: null, top5: [], reason: "", metrics: {}, scanning: false };
  let gridSelectedSymbol = null;

  const PAIRS_STORAGE_KEY = "simPlayerSelectedPairs";
  const MODE_HINTS = {
    sim: "Мультистратегия · Play / Прогон",
    chart: "Просмотр свечей пары за период",
    live: "Пара + стратегия · все сделки на графике",
    grid: "Bybit Grid · ranging-скан · график 5m",
  };
  const $ = (id) => document.getElementById(id);

  function tradeArchiveId(tr) {
    if (tr.id && String(tr.id).includes("|")) return tr.id;
    const sid = tr.scenario_id || tr.bot_id || "";
    const close = tr.close_ms || tr.open_ms || 0;
    return `${sid}|${tr.pair}|${tr.open_ms}|${close}`;
  }

  function mlForTrade(tr) {
    return mlPnl.byId.get(tradeArchiveId(tr)) || tr.ml || null;
  }

  function fmtMlBadge(ml, closed) {
    if (!ml) return "";
    const pred = ml.predicted === "profit" ? "Profit" : "Loss";
    const cls = ml.predicted === "profit" ? "ml-profit" : "ml-loss";
    const pct = Math.round((ml.confidence || 0) * 100);
    const pProfit = Math.round((ml.confidence_profit || 0) * 100);
    const pLoss = Math.round((ml.confidence_loss || 0) * 100);
    const title = closed
      ? `ML: ${pred} ${pct}% · P(profit)=${pProfit}% P(loss)=${pLoss}%`
      : `ML на входе: ${pred} ${pct}% · P(profit)=${pProfit}% P(loss)=${pLoss}%`;
    return `<span class="ml-badge ${cls}" title="${title}">ML ${pred} ${pct}%</span>`;
  }

  async function loadMlGate() {
    try {
      const st = await api("/sim/ml/entry-gate");
      mlGate = { enabled: !!st.enabled, ready: !!st.ready, model: st.model || "" };
      syncMlGateCheckboxes();
    } catch (_) {
      mlGate = { enabled: true, ready: false, model: "" };
    }
  }

  function syncMlGateCheckboxes() {
    for (const id of ["mlGateToggle", "mlGateToggleLive"]) {
      const el = $(id);
      if (el) el.checked = !!mlGate.enabled;
    }
  }

  async function setMlGateEnabled(enabled) {
    const st = await api("/sim/ml/entry-gate", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    mlGate = { enabled: !!st.enabled, ready: !!st.ready, model: st.model || "" };
    syncMlGateCheckboxes();
    log(`ML gate: ${mlGate.enabled ? "вкл" : "выкл"}${mlGate.model ? ` · ${mlGate.model}` : ""}`);
    return mlGate;
  }

  async function loadMlPnl() {
    try {
      const status = await api("/sim/ml/pnl/status");
      if (!status.ready) {
        mlPnl = { ready: false, model: "", byId: new Map() };
        return;
      }
      mlPnl.model = status.model || "";
      const preds = await api("/sim/ml/pnl/predictions");
      const byId = new Map();
      for (const p of preds.predictions || []) {
        if (p.id) byId.set(p.id, p);
      }
      mlPnl = { ready: true, model: mlPnl.model, byId };
    } catch (_) {
      mlPnl = { ready: false, model: "", byId: new Map() };
    }
  }

  async function scoreOpenTrade(tr, scenarioId) {
    if (!mlPnl.ready || tr._closed !== false) return null;
    const key = tradeArchiveId(tr);
    if (mlPnl.byId.has(key)) return mlPnl.byId.get(key);
    const sc = scenarioCatalog.find((s) => s.id === scenarioId);
    try {
      const body = {
        scenario_id: scenarioId,
        pair: tr.pair,
        open_ms: tr.open_ms,
        open_rate: tr.open_rate,
        is_short: tr.is_short,
        basis: {
          scenario_id: scenarioId,
          pair: tr.pair,
          scan_type: sc?.scan_type,
          group: sc?.group,
          strategy: sc?.strategy,
          stake_usdt: tr.stake_usdt || sc?.stake_usdt,
          stoploss: sc?.stoploss,
          minimal_roi: sc?.minimal_roi,
          armed_at_ms: sc?.armed_at_ms,
        },
        trade: {
          open_ms: tr.open_ms,
          open_rate: tr.open_rate,
          is_short: tr.is_short,
        },
      };
      const ml = await api("/sim/ml/pnl/predict", { method: "POST", body: JSON.stringify(body) });
      ml.id = key;
      mlPnl.byId.set(key, ml);
      tr.ml = ml;
      return ml;
    } catch (_) {
      return null;
    }
  }


  function log(msg) {
    const ts = new Date().toISOString().slice(11, 19);
    const line = `[${ts}] ${msg}\n`;
    for (const id of ["logBox", "liveLogBox", "gridLogBox"]) {
      const box = $(id);
      if (!box) continue;
      box.textContent = line + box.textContent.slice(0, 3000);
    }
  }

  function fillLiveStrategySelect() {
    const sel = $("liveStrategySelect");
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = "";
    for (const sc of scenarioCatalog) {
      const o = document.createElement("option");
      o.value = sc.id;
      o.textContent = sc.label || sc.id;
      sel.appendChild(o);
    }
    if (prev && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
    else if (sel.options.length) sel.selectedIndex = 0;
  }

  function setMode(mode) {
    if (!["sim", "chart", "live", "grid"].includes(mode)) return;
    const prev = uiMode;
    if (prev === "live" && mode !== "live") {
      restoreLiveScenarios().catch(() => {});
      if (livePairsSnapshot) {
        selectedSimPairs = new Set(livePairsSnapshot);
        livePairsSnapshot = null;
        updatePairsButtonLabel();
      }
      selectedLiveTradeId = null;
      const tip = $("chartHover");
      if (tip) {
        tip.classList.add("hidden");
        tip.innerHTML = "";
      }
    }
    uiMode = mode;
    document.body.classList.remove("mode-sim", "mode-chart", "mode-live", "mode-grid");
    document.body.classList.add("mode-" + mode);
    document.querySelectorAll(".mode-tab").forEach((btn) => {
      const on = btn.dataset.mode === mode;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    const hint = $("modeHint");
    if (hint) hint.textContent = MODE_HINTS[mode] || "";
    if (mode === "live") {
      fillLiveStrategySelect();
      syncLivePeriodLabel();
      renderLiveTradesPanel();
    }
    if (mode === "chart") {
      selectedTradeHighlight = null;
      if (candleSeries) candleSeries.setMarkers([]);
      const th = $("tradeHighlights");
      if (th) th.innerHTML = "";
      hidePriceLines();
    }
    if (mode === "grid") {
      selectedTradeHighlight = null;
      selectedLiveTradeId = null;
      if (candleSeries) candleSeries.setMarkers([]);
      const th = $("tradeHighlights");
      if (th) th.innerHTML = "";
      hidePriceLines();
      renderGridPanel();
      loadGridStatus().catch((e) => log(e.message));
    }
    drawHighlights();
    resizeChartForMode();
    log("Режим: " + (MODE_HINTS[mode] || mode));
  }

  function syncLivePeriodLabel() {
    const el = $("livePeriodLabel");
    if (!el) return;
    const a = $("dateFrom")?.value;
    const b = $("dateTo")?.value;
    el.textContent = a && b ? `${a} → ${b}` : "укажите От / До слева";
  }

  function gridSymbolOf(row) {
    if (!row) return null;
    if (row.symbol) return String(row.symbol).replace("/", "").replace(":USDT", "").toUpperCase();
    const p = row.pair || "";
    return p.replace("/", "").replace(":USDT", "").toUpperCase() || null;
  }

  function fmtPct(x, digits) {
    if (x == null || Number.isNaN(Number(x))) return "—";
    return (Number(x) * 100).toFixed(digits ?? 1) + "%";
  }

  function fmtNum(x, digits) {
    if (x == null || Number.isNaN(Number(x))) return "—";
    return Number(x).toFixed(digits ?? 2);
  }

  function renderGridPanel() {
    const reasonEl = $("gridReason");
    const metricsEl = $("gridMetrics");
    const topEl = $("gridTop5");
    const badge = $("gridPairBadge");
    const meta = $("gridMeta");
    const st = $("gridScanStatus");

    if (reasonEl) {
      reasonEl.textContent = gridScan.reason || "Запустите скан ranging-пар (1–2 мин). Бот на бирже не создаётся.";
      reasonEl.classList.toggle("muted", !gridScan.best);
    }

    if (metricsEl) {
      const m = gridScan.metrics || {};
      const cards = [
        ["score", fmtNum(m.score, 4)],
        ["ADX", fmtNum(m.adx, 1)],
        ["BB width", fmtPct(m.bb_width, 2)],
        ["ranging", fmtPct(m.ranging_ratio, 0)],
        ["inside BB", fmtPct(m.inside_bb_ratio, 0)],
        ["ATR ratio", fmtNum(m.atr_ratio, 2)],
        ["HTF", m.htf_ok == null ? "—" : m.htf_ok ? "ok" : "weak"],
      ];
      metricsEl.innerHTML = gridScan.best
        ? cards
            .map(
              ([k, v]) =>
                `<div class="grid-metric"><span class="k">${k}</span><span class="v">${v}</span></div>`
            )
            .join("")
        : "";
    }

    const top5 = gridScan.top5 || [];
    if (topEl) {
      if (!top5.length) {
        topEl.className = "grid-top5 muted";
        topEl.textContent = "—";
      } else {
        topEl.className = "grid-top5";
        topEl.innerHTML = top5
          .map((row, i) => {
            const sym = gridSymbolOf(row);
            const active = sym && sym === gridSelectedSymbol ? " active" : "";
            const pair = row.pair || sym || "—";
            const near = row.qualified === false ? " near" : "";
            return (
              `<div class="grid-top-row${active}${near}" data-symbol="${sym || ""}" data-idx="${i}">` +
              `<span class="rank">#${i + 1}</span>` +
              `<span class="pair">${pair}${row.qualified === false ? " · near" : ""}</span>` +
              `<span class="score">${fmtNum(row.score, 4)}</span>` +
              `<span class="adx">ADX ${fmtNum(row.adx, 1)}</span>` +
              `</div>`
            );
          })
          .join("");
        topEl.querySelectorAll(".grid-top-row").forEach((el) => {
          el.addEventListener("click", () => {
            const idx = Number(el.dataset.idx);
            const row = top5[idx];
            if (!row) return;
            selectGridPair(row).catch((e) => log(e.message));
          });
        });
      }
    }

    const sym = gridSelectedSymbol || gridSymbolOf(gridScan.best);
    if (badge) badge.textContent = sym ? `пара: ${sym} · 5m` : "пара: —";
    if (meta) {
      const parts = [];
      if (gridScan.scanned_at) parts.push(`скан: ${gridScan.scanned_at}`);
      if (gridScan.candidates_checked != null) parts.push(`проверено: ${gridScan.candidates_checked}`);
      if (sym) parts.push(`график: ${sym} 5m`);
      meta.textContent = parts.length ? parts.join(" · ") : "—";
    }
    if (st && !gridScan.scanning) {
      st.textContent = gridScan.best
        ? `Готово · ${gridScan.best.pair || gridSymbolOf(gridScan.best)}`
        : "Ranging-пары · без создания бота на Bybit";
    }
  }

  async function loadGridChart(symbol) {
    const sym = String(symbol || "").toUpperCase();
    if (!sym) throw new Error("symbol required");
    const data = await api(`/sim/bybit-grid/chart?symbol=${encodeURIComponent(sym)}&limit=200`);
    const candles = (data.candles || []).map((c) => ({
      time: Number(c.time),
      open: Number(c.open),
      high: Number(c.high),
      low: Number(c.low),
      close: Number(c.close),
    }));
    if (!candles.length) throw new Error("пустой график 5m");
    loadedCandles = candles;
    chartTimeframe = "5m";
    gridSelectedSymbol = data.symbol || sym;
    if (candleSeries) {
      candleSeries.setData(candles);
      candleSeries.setMarkers([]);
    }
    const th = $("tradeHighlights");
    if (th) th.innerHTML = "";
    if (chart && candles.length) {
      chart.timeScale().fitContent();
    }
    const last = candles[candles.length - 1];
    if ($("priceLabel") && last) $("priceLabel").textContent = Number(last.close).toFixed(6);
    if ($("statusLabel")) $("statusLabel").textContent = `grid · ${gridSelectedSymbol} 5m`;
    renderGridPanel();
    return data;
  }

  async function selectGridPair(row) {
    const sym = gridSymbolOf(row);
    if (!sym) return;
    gridSelectedSymbol = sym;
    const fromBest = gridScan.best && gridSymbolOf(gridScan.best) === sym;
    if ($("gridReason") && !fromBest) {
      const bits = [
        `Просмотр ${row.pair || sym} (score ${fmtNum(row.score, 4)}).`,
        row.adx != null ? `ADX ${fmtNum(row.adx, 1)}.` : "",
        row.bb_width != null ? `BB width ${fmtPct(row.bb_width, 2)}.` : "",
        row.ranging_ratio != null ? `ranging ${fmtPct(row.ranging_ratio, 0)}.` : "",
      ].filter(Boolean);
      $("gridReason").textContent = bits.join(" ");
      $("gridReason").classList.remove("muted");
      const m = {
        score: row.score,
        adx: row.adx,
        bb_width: row.bb_width,
        ranging_ratio: row.ranging_ratio,
        inside_bb_ratio: row.inside_bb_ratio,
        atr_ratio: row.atr_ratio,
        htf_ok: row.htf_ok,
      };
      gridScan.metrics = m;
    } else if (fromBest) {
      gridScan.metrics = gridScan.metricsBest || gridScan.metrics;
      if (gridScan.reasonBest) gridScan.reason = gridScan.reasonBest;
      if ($("gridReason")) {
        $("gridReason").textContent = gridScan.reason;
        $("gridReason").classList.toggle("muted", !gridScan.best);
      }
    }
    renderGridPanel();
    await loadGridChart(sym);
    log(`Grid chart: ${sym} 5m`);
  }

  function applyGridScanPayload(data) {
    gridScan.best = data.best || null;
    gridScan.top5 = data.top5 || [];
    gridScan.reason = data.reason || data.message || "";
    gridScan.reasonBest = gridScan.reason;
    gridScan.metrics = data.metrics || {};
    gridScan.metricsBest = data.metrics || {};
    gridScan.scanned_at = data.scanned_at;
    gridScan.candidates_checked = data.candidates_checked;
    gridScan.qualified = data.qualified !== false;
    gridScan.scanning = false;
  }

  async function loadGridStatus() {
    try {
      const data = await api("/sim/bybit-grid/status");
      if (data && (data.best || data.scanned_at || (data.top5 && data.top5.length))) {
        applyGridScanPayload(data);
        renderGridPanel();
        const sym = gridSelectedSymbol || gridSymbolOf(gridScan.best) || gridSymbolOf(gridScan.top5[0]);
        if (sym) await loadGridChart(sym);
      } else {
        renderGridPanel();
      }
    } catch (e) {
      renderGridPanel();
      throw e;
    }
  }

  async function runGridScan() {
    if (gridScan.scanning) return;
    gridScan.scanning = true;
    const btn = $("btnGridScan");
    const st = $("gridScanStatus");
    if (btn) btn.disabled = true;
    if (st) st.textContent = "Скан… 1–2 мин, ждите";
    if ($("statusLabel")) $("statusLabel").textContent = "scanning…";
    log("Bybit Grid: старт скана (без create_grid)");
    try {
      const data = await api("/sim/bybit-grid/scan", { method: "POST", body: "{}" });
      applyGridScanPayload(data);
      renderGridPanel();
      const sym = gridSymbolOf(gridScan.best) || gridSymbolOf(gridScan.top5[0]);
      if (sym) await loadGridChart(sym);
      const tag = data.qualified === false ? "near-miss" : "ok";
      log(
        `Bybit Grid: ${tag} · ${gridScan.best?.pair || "—"} · checked=${data.candidates_checked ?? "—"} · ranging=${data.ranging_found ?? 0}`
      );
      if (st) {
        st.textContent = data.qualified === false
          ? "Нет qualifying — показаны ближайшие"
          : `Готово · ${gridScan.best?.pair || "—"}`;
      }
    } catch (e) {
      gridScan.scanning = false;
      if (st) st.textContent = "Ошибка скана";
      if ($("statusLabel")) $("statusLabel").textContent = "scan error";
      throw e;
    } finally {
      gridScan.scanning = false;
      if (btn) btn.disabled = false;
      renderGridPanel();
    }
  }

  function resizeChartForMode() {
    const el = $("chart");
    if (!chart || !el) return;
    const h = uiMode === "sim" ? Math.max(420, el.clientHeight) : Math.max(560, el.clientHeight);
    chart.applyOptions({ width: el.clientWidth, height: h });
    drawHighlights();
  }

  async function snapshotAndEnableLiveScenario(sid) {
    liveEnabledSnapshot = scenarioCatalog.map((sc) => ({ id: sc.id, enabled: sc.enabled !== false }));
    for (const sc of scenarioCatalog) {
      const want = sc.id === sid;
      if ((sc.enabled !== false) !== want) {
        await setScenarioEnabled(sc.id, want);
      }
    }
  }

  async function restoreLiveScenarios() {
    if (!liveEnabledSnapshot) return;
    for (const row of liveEnabledSnapshot) {
      const sc = scenarioCatalog.find((s) => s.id === row.id);
      if (!sc) continue;
      if ((sc.enabled !== false) !== row.enabled) {
        await setScenarioEnabled(row.id, row.enabled);
      }
    }
    liveEnabledSnapshot = null;
  }

  function renderLiveTradesPanel() {
    const el = $("liveTradesPanel");
    if (!el) return;
    const sid = ($("liveStrategySelect") && $("liveStrategySelect").value) || activeReplayScenario;
    if (!sid) {
      el.innerHTML = "<span class='muted'>Выберите стратегию</span>";
      return;
    }
    const pair = $("pairSelect").value;
    const trades = liveTradesList(sid).filter((t) => !pair || t.pair === pair);
    if (!trades.length) {
      el.innerHTML =
        "<span class='muted'>Сделок пока нет — загрузите график и нажмите «Найти сделки»</span>";
      return;
    }
    el.innerHTML = trades
      .slice()
      .reverse()
      .map((t) => {
        const open = t._closed === false;
        const pnl =
          t.profit_abs == null
            ? "—"
            : (t.profit_abs >= 0 ? "+" : "") + Number(t.profit_abs).toFixed(4);
        const tag = open ? '<span class="tag">OPEN</span>' : "";
        const span = open
          ? fmtTime(t.open_ms)
          : fmtTime(t.open_ms) + " → " + fmtTime(t.close_ms);
        const reason = t.exit_reason || (open ? "в рынке" : "");
        const selected = t.id && t.id === selectedLiveTradeId ? " selected" : "";
        return (
          '<div class="live-trade-row ' +
          (open ? "open" : "") +
          selected +
          '" data-trade-id="' +
          (t.id || "") +
          '" title="Показать на графике">' +
          tag +
          "<span>" +
          span +
          "</span><span>" +
          pnl +
          ' USDT</span><span class="muted">' +
          reason +
          "</span></div>"
        );
      })
      .join("");
    el.querySelectorAll(".live-trade-row[data-trade-id]").forEach((row) => {
      row.addEventListener("click", () => {
        const tr = trades.find((t) => t.id === row.dataset.tradeId);
        if (tr) focusLiveTrade(tr);
      });
    });
  }

  function fmtPrice(v) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    if (n >= 1000) return n.toFixed(2);
    if (n >= 1) return n.toFixed(4);
    return n.toFixed(6);
  }

  function updateChartHover(param) {
    const tip = $("chartHover");
    if (!tip || !candleSeries) return;
    if (!param || param.time === undefined || param.point === undefined) {
      tip.classList.add("hidden");
      tip.innerHTML = "";
      return;
    }
    const raw = param.seriesData?.get?.(candleSeries);
    if (!raw || raw.close == null) {
      tip.classList.add("hidden");
      tip.innerHTML = "";
      return;
    }
    let timeLabel = "—";
    if (typeof param.time === "number") {
      timeLabel = fmtTime(param.time * 1000);
    } else if (param.time && param.time.year) {
      const t = param.time;
      timeLabel = `${t.year}-${String(t.month).padStart(2, "0")}-${String(t.day).padStart(2, "0")}`;
    }
    tip.innerHTML =
      '<div class="ch-time">' +
      timeLabel +
      '</div><div class="ch-ohlc">' +
      "<span>O <b>" +
      fmtPrice(raw.open) +
      "</b></span>" +
      "<span>H <b>" +
      fmtPrice(raw.high) +
      "</b></span>" +
      "<span>L <b>" +
      fmtPrice(raw.low) +
      "</b></span>" +
      "<span>C <b>" +
      fmtPrice(raw.close) +
      "</b></span>" +
      "</div>";
    tip.classList.remove("hidden");
    if ($("priceLabel")) $("priceLabel").textContent = fmtPrice(raw.close);
  }

  async function focusLiveTrade(tr) {
    if (!tr || !tr.open_ms) return;
    selectedLiveTradeId = tr.id || `${tr.bot_id || tr.inst_id}:${tr.open_ms}`;
    showPriceLines(tr);
    const openSec = Math.floor(tr.open_ms / 1000);
    const closeSec = Math.floor((tr.close_ms || tr.open_ms) / 1000);
    const pad = Math.max(180, Math.floor((closeSec - openSec) * 0.35) || 180);
    try {
      chart.timeScale().setVisibleRange({
        from: openSec - pad,
        to: closeSec + pad,
      });
    } catch (_) {}
    renderLiveTradesPanel();
    drawHighlights();
    const sym = (tr.pair || "").split("/")[0] || "";
    log(`→ сделка ${sym} ${fmtTime(tr.open_ms)}`);
  }

  function collectLiveHighlight() {
    if (uiMode !== "live") return null;
    const pair = $("pairSelect").value;
    const sid = ($("liveStrategySelect") && $("liveStrategySelect").value) || activeReplayScenario;
    if (!sid || !pair) return null;
    const trades = liveTradesList(sid).filter((t) => t.pair === pair);
    if (!trades.length) return null;
    return {
      bot_id: sid,
      label: sid,
      pair: pair,
      trades: trades.map((t) => Object.assign({}, t, { _liveOpen: false, _closed: true })),
      summary: { total_trades: trades.length },
      _liveMode: true,
    };
  }

  async function liveLoad() {
    await loadChart();
    const meta = $("chartMeta");
    if (meta) {
      meta.textContent =
        $("pairSelect").value +
        "\n" +
        $("dateFrom").value +
        " → " +
        $("dateTo").value +
        "\n" +
        loadedCandles.length +
        " свечей";
    }
    renderLiveTradesPanel();
  }

  function ingestFindTrades(sid, rows) {
    const map = new Map();
    for (const row of rows || []) {
      const id = row.id || `${row.inst_id || row.bot_id || sid}:${row.open_ms}`;
      map.set(id, {
        id,
        scenario_id: sid,
        bot_id: row.inst_id || row.bot_id || sid,
        pair: row.pair,
        label: row.label,
        open_ms: row.open_ms,
        close_ms: row.close_ms,
        open_rate: row.open_rate,
        close_rate: row.close_rate,
        profit_abs: row.profit_abs,
        profit_ratio: row.profit_ratio,
        exit_reason: row.exit_reason,
        is_short: row.is_short,
        stop_loss: row.stop_loss,
        take_profit: row.take_profit,
        _live: true,
        _closed: true,
      });
    }
    liveTradesByScenario.set(sid, map);
  }

  async function findTrades() {
    const pair = $("pairSelect").value;
    const sid = $("liveStrategySelect") && $("liveStrategySelect").value;
    const start_ms = localInputToMs($("dateFrom").value);
    const end_ms = localInputToMs($("dateTo").value);
    if (!pair || !sid || !start_ms || !end_ms) {
      log("Выберите пару, стратегию и период (От / До)");
      return;
    }
    if (!loadedCandles.length) await loadChart();
    await snapshotAndEnableLiveScenario(sid);
    if (!livePairsSnapshot) {
      livePairsSnapshot = [...getSelectedSimPairs()];
    }
    selectedSimPairs = new Set([pair]);
    updatePairsButtonLabel();
    resetBotsUi(false);
    liveTradesByScenario.delete(sid);
    selectedLiveTradeId = null;
    if ($("btnLivePlay")) $("btnLivePlay").disabled = true;
    $("statusLabel").textContent = "поиск…";
    log("Поиск сделок ▶ " + sid + " · " + pair);
    try {
      const res = await api("/sim/player/find-trades", {
        method: "POST",
        body: JSON.stringify({
          pair: pair,
          pairs: [pair],
          timeframe: chartTimeframe || "1s",
          range_start_ms: start_ms,
          range_end_ms: end_ms,
        }),
      });
      const rows = (res.trades || []).filter((t) => !t.scenario_id || t.scenario_id === sid);
      // group by scenario if backend returned mixed
      const bySid = new Map();
      for (const t of res.trades || []) {
        const s = t.scenario_id || sid;
        if (!bySid.has(s)) bySid.set(s, []);
        bySid.get(s).push(t);
      }
      if (!bySid.size) bySid.set(sid, rows);
      for (const [s, list] of bySid) ingestFindTrades(s, list);
      activeReplayScenario = sid;
      replayComplete = true;
      sequentialReplay = false;
      $("statusLabel").textContent = "готово";
      renderLiveTradesPanel();
      drawHighlights();
      const n = liveTradesList(sid).length;
      const cache = res.cache || {};
      const cacheTxt =
        cache.computed_ranges > 0
          ? ` · кэш +${cache.computed_ranges} новых диапазонов`
          : cache.hits
            ? " · из кэша"
            : "";
      log(
        `Найдено сделок: ${n}` +
          (res.ml_gate ? " · ML gate вкл" : " · ML gate выкл") +
          cacheTxt +
          (n ? " · клик по строке — на график" : "")
      );
      if (n && chart) {
        const all = liveTradesList(sid);
        const lo = Math.min(...all.map((t) => t.open_ms));
        const hi = Math.max(...all.map((t) => t.close_ms || t.open_ms));
        try {
          chart.timeScale().setVisibleRange({
            from: Math.floor(lo / 1000) - 300,
            to: Math.floor(hi / 1000) + 300,
          });
        } catch (_) {}
      }
      updateUI(res);
    } catch (e) {
      log("Ошибка поиска: " + e.message);
      $("statusLabel").textContent = "error";
    } finally {
      if ($("btnLivePlay")) $("btnLivePlay").disabled = false;
    }
  }

  function msToLocalInput(ms) {
    if (!ms) return "";
    const d = new Date(ms);
    d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
    return d.toISOString().slice(0, 19);
  }

  function localInputToMs(val) {
    if (!val) return 0;
    return new Date(val).getTime();
  }

  function fmtTime(ms) {
    if (!ms) return "—";
    return new Date(ms).toISOString().replace("T", " ").slice(0, 19);
  }

  async function api(path, opts = {}) {
    const r = await fetch(`${API}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  }

  function initChart() {
    const el = $("chart");
    chart = LightweightCharts.createChart(el, {
      layout: { background: { color: "#0d1117" }, textColor: "#8b949e" },
      grid: { vertLines: { color: "#21262d" }, horzLines: { color: "#21262d" } },
      timeScale: { timeVisible: true, secondsVisible: true },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    });
    candleSeries = chart.addCandlestickSeries({
      upColor: "#3fb950",
      downColor: "#f85149",
      borderVisible: false,
      wickUpColor: "#3fb950",
      wickDownColor: "#f85149",
    });
    slLine = candleSeries.createPriceLine({
      color: "#f85149",
      lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true,
      title: "SL",
    });
    tpLine = candleSeries.createPriceLine({
      color: "#3fb950",
      lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true,
      title: "TP",
    });
    hidePriceLines();
    new ResizeObserver(() => {
      chart.applyOptions({ width: el.clientWidth, height: Math.max(480, el.clientHeight) });
      drawHighlights();
    }).observe(el);
    chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
      drawHighlights();
    });
    chart.subscribeCrosshairMove((param) => updateChartHover(param));
  }

  function hidePriceLines() {
    slLine.applyOptions({ price: 0, axisLabelVisible: false, lineVisible: false });
    tpLine.applyOptions({ price: 0, axisLabelVisible: false, lineVisible: false });
    $("slTpLabel").classList.add("hidden");
  }

  function showPriceLines(trade) {
    if (!trade) {
      hidePriceLines();
      return;
    }
    slLine.applyOptions({ price: trade.stop_loss, axisLabelVisible: true, lineVisible: true, title: "SL" });
    tpLine.applyOptions({ price: trade.take_profit, axisLabelVisible: true, lineVisible: true, title: "TP" });
    $("slTpLabel").classList.remove("hidden");
    $("slTpLabel").querySelector("span").textContent =
      `SL ${trade.stop_loss?.toFixed(6)} · TP ${trade.take_profit?.toFixed(6)}`;
  }

  function loadSelectedPairsFromStorage(pool, preferred) {
    const fallback = (preferred || []).filter((p) => pool.includes(p));
    const defaultSet = fallback.length ? fallback : pool.slice(0, Math.min(20, pool.length));
    try {
      const raw = localStorage.getItem(PAIRS_STORAGE_KEY);
      if (!raw) {
        selectedSimPairs = new Set(defaultSet);
        return;
      }
      const saved = JSON.parse(raw);
      if (Array.isArray(saved) && saved.length) {
        selectedSimPairs = new Set(saved.filter((p) => pool.includes(p)));
      } else {
        selectedSimPairs = new Set(defaultSet);
      }
    } catch (_) {
      selectedSimPairs = new Set(defaultSet);
    }
    if (!selectedSimPairs.size && pool.length) {
      selectedSimPairs = new Set(defaultSet.length ? defaultSet : pool.slice(0, 20));
    }
  }

  function saveSelectedPairsToStorage() {
    localStorage.setItem(PAIRS_STORAGE_KEY, JSON.stringify([...selectedSimPairs]));
  }

  function getSelectedSimPairs() {
    const list = [...selectedSimPairs];
    return list.length ? list : [...allPoolPairs];
  }

  function updatePairsButtonLabel() {
    const total = allPoolPairs.length;
    const n = getSelectedSimPairs().length;
    $("btnPairs").textContent = total ? `Пары (${n}/${total})` : "Пары (…)";
  }

  function renderPairsChecklist() {
    const box = $("pairsChecklist");
    box.innerHTML = "";
    for (const pair of allPoolPairs) {
      const sym = pair.split("/")[0];
      const label = document.createElement("label");
      label.className = "pair-check";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = pair;
      cb.checked = selectedSimPairs.has(pair);
      cb.addEventListener("change", () => {
        if (cb.checked) selectedSimPairs.add(pair);
        else selectedSimPairs.delete(pair);
        updatePairsModalCount();
      });
      label.appendChild(cb);
      label.appendChild(document.createTextNode(sym));
      box.appendChild(label);
    }
    updatePairsModalCount();
  }

  function updatePairsModalCount() {
    $("pairsSelectedCount").textContent = `Выбрано: ${selectedSimPairs.size} из ${allPoolPairs.length}`;
  }

  function openPairsModal() {
    renderPairsChecklist();
    $("pairsModal").classList.remove("hidden");
  }

  function closePairsModal() {
    $("pairsModal").classList.add("hidden");
  }

  async function applyPairsSelection() {
    if (!selectedSimPairs.size) {
      log("Выберите хотя бы одну пару");
      return;
    }
    saveSelectedPairsToStorage();
    updatePairsButtonLabel();
    closePairsModal();
    log(`Симуляция: ${selectedSimPairs.size} пар`);
    await resetProgress("bots", false);
  }

  async function resetProgress(scope, logMsg = true) {
    if (scope === "reprepare") {
      try {
        await api("/sim/bots/invalidate", { method: "POST" });
        if (logMsg) log("Кэш симуляции сброшен — следующий Play пересчитает заново");
      } catch (e) {
        log(`Сброс кэша: ${e.message}`);
      }
      return;
    }
    const body = { scope: scope === "probe" ? "bots" : scope };
    if (scope === "probe") body.clear_probe_cache = true;
    try {
      const res = await api("/sim/player/reset", { method: "POST", body: JSON.stringify(body) });
      if (scope === "replay" || scope === "all") {
        if (res.player) updateUI(res.player);
      }
      if (scope === "bots" || scope === "all" || scope === "probe") {
        resetBotsUi(false);
      }
      if (scope === "replay" || scope === "all") {
        selectedTradeId = null;
        selectedTradeHighlight = null;
        selectedBotId = null;
        $("botDetail").classList.add("hidden");
        if (candleSeries) candleSeries.setMarkers([]);
        $("tradeHighlights").innerHTML = "";
        hidePriceLines();
        drawHighlights();
      }
      const labels = {
        replay: "Replay сброшен к началу периода",
        bots: "Боты сброшены — нужен новый Play",
        all: "Полный сброс",
        reprepare: "Кэш симуляции сброшен",
      };
      if (logMsg) log(labels[scope] || "Сброс выполнен");
    } catch (e) {
      log(`Сброс: ${e.message}`);
    }
  }

  function toggleResetMenu(show) {
    const menu = $("resetMenu");
    if (show === undefined) menu.classList.toggle("hidden");
    else menu.classList.toggle("hidden", !show);
  }

  async function loadPairs() {
    const data = await api("/sim/pairs");
    allPoolPairs = data.pairs || [];
    preferredPairs = data.preferred || [];
    loadSelectedPairsFromStorage(allPoolPairs, preferredPairs);
    updatePairsButtonLabel();
    const sel = $("pairSelect");
    sel.innerHTML = "";
    for (const p of allPoolPairs) {
      const o = document.createElement("option");
      o.value = p;
      o.textContent = p;
      sel.appendChild(o);
    }
    log(`Пары: ${allPoolPairs.length} доступно` + (preferredPairs.length ? ` · дефолт симуляции ${preferredPairs.length}` : ""));
    const initial =
      (preferredPairs.find((p) => allPoolPairs.includes(p)) || allPoolPairs[0]) || "";
    if (initial) {
      sel.value = initial;
      await loadRange(initial);
    }
  }

  async function loadRange(pair) {
    try {
      const rng = await api(`/sim/range?pair=${encodeURIComponent(pair)}&timeframe=1s`);
      if (rng.timeframe) chartTimeframe = rng.timeframe;
      if (rng.start_ms) $("dateFrom").value = msToLocalInput(rng.start_ms);
      if (rng.end_ms) $("dateTo").value = msToLocalInput(rng.end_ms);
      syncLivePeriodLabel();
      if (!rng.start_ms || !rng.end_ms) {
        log(`Нет диапазона дат для ${pair}`);
      } else if (rng.timeframe && rng.timeframe !== "1s") {
        log(`${pair}: свечи ${rng.timeframe} (${rng.count || "?"} баров)`);
      }
    } catch (e) {
      log(`Нет данных для ${pair}`);
    }
  }

  async function loadChart(keepTime, rangeOverride) {
    const pair = $("pairSelect").value;
    const start_ms = rangeOverride?.start_ms ?? localInputToMs($("dateFrom").value);
    const end_ms = rangeOverride?.end_ms ?? localInputToMs($("dateTo").value);
    if (!pair || !start_ms || !end_ms) {
      log("Выберите пару и период");
      return;
    }
    if (!rangeOverride) {
      $("dateFrom").value = msToLocalInput(start_ms);
      $("dateTo").value = msToLocalInput(end_ms);
    }
    const data = await api(
      `/sim/chart?pair=${encodeURIComponent(pair)}&timeframe=1s&start_ms=${start_ms}&end_ms=${end_ms}&limit=20000`
    );
    chartTimeframe = data.timeframe || "1s";
    loadedCandles = data.candles;
    candleSeries.setData(loadedCandles);
    chart.timeScale().fitContent();
    const cfg = {
      pair,
      timeframe: chartTimeframe,
      range_start_ms: start_ms,
      range_end_ms: end_ms,
    };
    if (keepTime) cfg.start_ms = localInputToMs($("clockLabel").dataset?.ms) || start_ms;
    if (rangeOverride?.seek_ms) cfg.start_ms = rangeOverride.seek_ms;
    await api("/sim/player/configure", { method: "POST", body: JSON.stringify(cfg) });
    log(`График ${pair}: ${loadedCandles.length} свечей ${chartTimeframe}`);
    const meta = $("chartMeta");
    if (meta) {
      meta.textContent =
        pair +
        "\n" +
        msToLocalInput(start_ms) +
        " → " +
        msToLocalInput(end_ms) +
        "\n" +
        loadedCandles.length +
        " свечей " +
        chartTimeframe;
    }
    drawHighlights();
  }

  async function loadChartForTrade(tr) {
    const pad = 7200_000;
    $("pairSelect").value = tr.pair;
    await loadChart(false, {
      start_ms: tr.open_ms - pad,
      end_ms: tr.close_ms + pad,
      seek_ms: tr.open_ms,
    });
  }

  let botsRuntimeMap = new Map();
  let batchRun = false;
  const PRGON_WORKERS = 3;
  let pairReplayLogged = new Set();
  let liveTradesByScenario = new Map();
  let activeReplayScenario = null;
  let sequentialReplay = false;
  let replayComplete = false;

  function setSimRunning(on) {
    $("btnPlay").disabled = on;
    $("btnPrgon").disabled = on;
  }

  function onBatchTrades(msg) {
    const sid = msg.scenario_id;
    if (!sid || !msg.trades) return;
    ensureStrategyCard(sid);
    const map = new Map();
    for (const row of msg.trades) {
      const id = `${row.inst_id}:${row.open_ms}`;
      map.set(id, {
        id,
        scenario_id: sid,
        bot_id: row.inst_id,
        pair: row.pair,
        label: row.label,
        open_ms: row.open_ms,
        close_ms: row.close_ms,
        open_rate: row.open_rate,
        close_rate: row.close_rate,
        profit_abs: row.profit_abs,
        profit_ratio: row.profit_ratio,
        exit_reason: row.exit_reason,
        is_short: row.is_short,
        stop_loss: row.stop_loss,
        take_profit: row.take_profit,
        _live: true,
        _closed: row.closed !== false,
      });
    }
    liveTradesByScenario.set(sid, map);
    sequentialReplay = true;
    batchRun = true;
    activeReplayScenario = sid;
    refreshStrategyCard(sid);
    markReplayStrategyCards();
    if (msg.revealed === msg.total) {
      log(`  ${msg.label || sid}: ${msg.total} сд. в карточке`);
    }
  }

  function mergeRuntimeBots(botsAll) {
    if (!botsAll?.length) return;
    for (const rt of botsAll) {
      botsRuntimeMap.set(rt.id, rt);
    }
  }

  function runtimeForInst(inst) {
    return botsRuntimeMap.get(inst.id);
  }

  function phaseLabel(phase, inst) {
    if (!inst.trading_active) {
      return "⏳ Сканер не выбрал";
    }
    if (phase === "waiting") {
      if (!inst.armed_at_ms) return "⏳ Ждёт сканер (30m)";
      return "⏳ До whitelist";
    }
    if (phase === "in_trade") return "● В сделке";
    if (phase === "done") return `■ Завершён · ${inst.summary?.profit_abs ?? 0} USDT`;
    if (inst.summary?.total_trades) {
      return `✓ Whitelist · ${inst.summary.total_trades} сд.`;
    }
    return "✓ В whitelist";
  }

  function instPhase(inst, currentMs) {
    if (inst.trading_active === false) return "skipped";
    const rt = runtimeForInst(inst);
    if (rt?.phase && inst.pair === $("pairSelect").value) return rt.phase;
    if (rt?.current_trade) return "in_trade";
    if (!inst.armed_at_ms || currentMs < inst.armed_at_ms) return "waiting";
    const open = (inst.trades || []).find((t) => t.open_ms <= currentMs && t.close_ms >= currentMs);
    if (open) return "in_trade";
    const allDone = inst.trades?.length && inst.trades.every((t) => t.close_ms <= currentMs);
    if (allDone) return "done";
    return "active";
  }

  function instVisible() {
    return botInstances.length > 0;
  }

  function stakeLabelFor(inst) {
    const s = inst.config?.stake;
    return s ? `${s} USDT` : "—";
  }

  function effectiveStake(sc) {
    if (playerProfile.stake_mode === "uniform") {
      return Number(playerProfile.stake_usdt) || 50;
    }
    return Number(sc?.stake_usdt ?? playerProfile.stake_usdt ?? 10);
  }

  function formatStakeSummary() {
    const mot = playerProfile.max_open_trades || 2;
    if (playerProfile.stake_mode === "uniform") {
      return `${playerProfile.stake_usdt || 50} USDT · все боты · max ${mot} поз.`;
    }
    const stakes = playerProfile.scenario_stakes_usdt;
    if (stakes && stakes.length) {
      const min = Math.min(...stakes);
      const max = Math.max(...stakes);
      const range = min === max ? `${min}` : `${min}–${max}`;
      return `по стратегии · ${range} USDT · max ${mot} поз.`;
    }
    return `по стратегии · max ${mot} поз.`;
  }

  function updateStakeControls() {
    const mode = playerProfile.stake_mode || "scenario";
    const modeEl = $("stakeModeSelect");
    const amountEl = $("stakeAmountInput");
    const wrap = $("stakeAmountWrap");
    if (modeEl) modeEl.value = mode;
    if (amountEl) amountEl.value = playerProfile.stake_usdt || 50;
    if (wrap) wrap.classList.toggle("hidden", mode === "scenario");
    if ($("stakeLabel")) $("stakeLabel").textContent = formatStakeSummary();
  }

  async function saveProfileStake() {
    const mode = $("stakeModeSelect").value;
    const stake = Number($("stakeAmountInput").value);
    if (mode === "uniform" && (!stake || stake <= 0)) {
      throw new Error("Укажите размер стейка");
    }
    playerProfile = await api("/sim/profile", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stake_mode: mode, stake_usdt: stake }),
    });
    updateStakeControls();
    renderStrategyPanels(true);
    log("Стейк сохранён — перезапустите Play или Прогон");
  }

  async function setScenarioStake(scenarioId, stakeUsdt) {
    const stake = Number(stakeUsdt);
    if (!stake || stake <= 0) return;
    await api(`/sim/scenarios/${scenarioId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stake_usdt: stake }),
    });
    const sc = scenarioCatalog.find((s) => s.id === scenarioId);
    if (sc) sc.stake_usdt = stake;
    playerProfile = await api("/sim/profile");
    updateStakeControls();
    renderStrategyPanels(true);
    log(`${sc?.label || scenarioId}: стейк ${stake} USDT — перезапустите прогон`);
  }

  function renderBotList(botsAll) {
    const panel = $("botsPanel");
    const scrollTop = panel.scrollTop;
    const pair = $("pairSelect").value;
    const list = (botsAll || botInstances).filter((i) => !pair || i.pair === pair);
    if (!list.length) {
      panel.innerHTML = pair
        ? `<span class="muted">Нет ботов для ${pair.split("/")[0]} — ▶ Play</span>`
        : '<span class="muted">Нажмите ▶ Play — боты загрузятся автоматически</span>';
      $("botsCount").textContent = "";
      return;
    }
    $("botsCount").textContent = `(${list.length} · ${pair ? pair.split("/")[0] : "все"})`;
    panel.innerHTML = "";

    const live = list.filter((i) => (i.scenario_id || "").startsWith("live_"));
    const lite = list.filter((i) => !(i.scenario_id || "").startsWith("live_"));
    for (const [title, group] of [
      ["Live боты", live],
      ["LiteFinance", lite],
    ]) {
      if (!group.length) continue;
      const head = document.createElement("div");
      head.className = "bot-group-title";
      head.textContent = title;
      panel.appendChild(head);
      for (const inst of group) {
        appendBotCard(panel, inst);
      }
    }
    panel.scrollTop = scrollTop;
  }

  function appendBotCard(panel, inst) {
    const rt = runtimeForInst(inst);
    const phase = rt?.phase || inst.phase || instPhase(inst, lastCurrentMs);
    const card = document.createElement("div");
    const inactive = inst.trading_active === false;
    const armed = !inactive && inst.armed_at_ms && lastCurrentMs >= inst.armed_at_ms;
    card.className = `bot-card ${phase}${selectedBotId === inst.id ? " selected" : ""}${inactive ? " inactive" : armed ? "" : " waiting"}`;
    card.dataset.id = inst.id;
    let pnlTxt;
    let pnlClass;
    if (inactive) {
      pnlTxt = "0 сд.";
      pnlClass = "muted";
    } else {
      const pnl = inst.summary?.profit_abs ?? 0;
      pnlTxt = inst.summary?.total_trades
        ? `${inst.summary.total_trades} сд. · ${pnl >= 0 ? "+" : ""}${pnl.toFixed(4)} USDT`
        : "0 сд.";
      pnlClass = pnl >= 0 ? "pnl-win" : "pnl-loss";
    }
    card.innerHTML = `
      <div class="title">${inst.label}</div>
      <div class="meta">${inst.settings} · стейк ${stakeLabelFor(inst)}</div>
      <div class="meta ${pnlClass}">${pnlTxt}</div>
      <div class="phase ${phase}">${phaseLabel(phase, inst)}</div>
    `;
    card.addEventListener("click", () => selectBot(inst.id));
    panel.appendChild(card);
  }

  function resetLiveReplay() {
    liveTradesByScenario = new Map();
    activeReplayScenario = null;
    sequentialReplay = false;
    replayComplete = false;
  }

  function shouldPreserveLiveCards() {
    return sequentialReplay || replayComplete || liveTradesByScenario.size > 0;
  }

  function instanceTradesForDisplay(scenarioId) {
    const rows = [];
    for (const inst of botInstances) {
      if (inst.scenario_id !== scenarioId) continue;
      for (const t of inst.trades || []) {
        rows.push({
          id: `${inst.id}:${t.open_ms}`,
          bot_id: inst.id,
          scenario_id: scenarioId,
          label: inst.label,
          pair: inst.pair,
          stake_usdt: inst.config?.stake,
          _closed: true,
          ...t,
        });
      }
    }
    return rows.sort((a, b) => a.open_ms - b.open_ms);
  }

  function hydrateLiveTradesFromInstances(scenarioId) {
    const trades = instanceTradesForDisplay(scenarioId);
    if (!trades.length) return;
    const map = new Map();
    for (const t of trades) {
      map.set(t.id, { ...t, _live: true, _closed: true });
    }
    liveTradesByScenario.set(scenarioId, map);
  }

  function tradesForStrategyDisplay(sc) {
    const live = liveTradesList(sc.id);
    if (batchRun && !replayComplete) {
      return live;
    }
    const sim = instanceTradesForDisplay(sc.id);
    const isActive = sc.id === activeReplayScenario && sequentialReplay && !replayComplete;
    if (isActive && live.length) {
      const map = new Map();
      for (const t of sim) map.set(t.id, t);
      for (const t of live) map.set(t.id, t);
      return [...map.values()].sort((a, b) => a.open_ms - b.open_ms);
    }
    if (sim.length) return sim;
    return live;
  }

  function finalizeSequentialReplay() {
    activeReplayScenario = null;
    sequentialReplay = false;
    batchRun = false;
    replayComplete = true;
    setSimRunning(false);
    for (const sc of scenarioCatalog.filter(isScenarioEnabled)) {
      hydrateLiveTradesFromInstances(sc.id);
      refreshStrategyCard(sc.id);
    }
    document.querySelectorAll(".strategy-block").forEach((block) => {
      block.classList.remove("replaying", "replay-done");
    });
    updatePortfolioSummary();
    if (uiMode === "live") {
      renderLiveTradesPanel();
      drawHighlights();
    }
  }

  function liveTradesList(scenarioId) {
    const map = liveTradesByScenario.get(scenarioId);
    if (!map) return [];
    return [...map.values()].sort((a, b) => a.open_ms - b.open_ms);
  }

  function syncLiveTradesFromTick(msg) {
    const sid = msg.active_scenario_id;
    const rows = msg.live_trades;
    if (!sid || !rows) return;
    const map = new Map(liveTradesByScenario.get(sid) || []);
    for (const row of rows) {
      const id = `${row.inst_id}:${row.open_ms}`;
      map.set(id, {
        id,
        scenario_id: sid,
        bot_id: row.inst_id,
        pair: row.pair,
        label: row.label,
        open_ms: row.open_ms,
        close_ms: row.close_ms,
        open_rate: row.open_rate,
        close_rate: row.close_rate,
        profit_abs: row.profit_abs,
        profit_ratio: row.profit_ratio,
        exit_reason: row.exit_reason,
        is_short: row.is_short,
        stop_loss: row.stop_loss,
        take_profit: row.take_profit,
        _live: true,
        _closed: !!row.closed,
      });
    }
    liveTradesByScenario.set(sid, map);
    sequentialReplay = true;
    activeReplayScenario = sid;
    ensureStrategyCard(sid);
    refreshStrategyCard(sid);
    markReplayStrategyCards();
    if (uiMode === "live") {
      renderLiveTradesPanel();
      drawHighlights();
    }
  }

  function ensureStrategyCard(scenarioId) {
    if (document.querySelector(`.strategy-block[data-scenario="${scenarioId}"]`)) return;
    renderStrategyPanels(true);
  }

  function onTradeLive(msg) {
    const sid = msg.scenario_id;
    if (!sid || !msg.trade) return;
    ensureStrategyCard(sid);
    if (!liveTradesByScenario.has(sid)) liveTradesByScenario.set(sid, new Map());
    const map = liveTradesByScenario.get(sid);
    const id = `${msg.inst_id}:${msg.trade.open_ms}`;
    const prev = map.get(id) || { id, scenario_id: sid, bot_id: msg.inst_id, pair: msg.pair, label: msg.label };
    if (msg.event === "open") {
      map.set(id, {
        ...prev,
        ...msg.trade,
        pair: msg.pair || prev.pair,
        _live: true,
        _closed: false,
      });
    } else if (msg.event === "close") {
      map.set(id, {
        ...prev,
        ...msg.trade,
        pair: msg.pair || prev.pair,
        _live: true,
        _closed: true,
      });
    }
    refreshStrategyCard(sid);
    if (msg.event === "open") {
      const tr = map.get(id);
      scoreOpenTrade(tr, sid).then(() => refreshStrategyCard(sid)).catch(() => {});
      log(`▸ ${msg.label} ${msg.pair.split("/")[0]} вход ${fmtTime(msg.trade.open_ms)}`);
    }
    if (uiMode === "live") {
      renderLiveTradesPanel();
      drawHighlights();
    }
  }

  function refreshStrategyCard(scenarioId) {
    const block = document.querySelector(`.strategy-block[data-scenario="${scenarioId}"]`);
    if (!block) return;
    const sc = scenarioCatalog.find((s) => s.id === scenarioId) || { id: scenarioId, label: scenarioId };
    const trades = tradesForStrategyDisplay(sc);
    const closed = trades.filter((t) => t._closed !== false);
    const livePnl = closed.reduce((s, t) => s + (t.profit_abs || 0), 0);
    const pnlClass = livePnl >= 0 ? "pnl-win" : "pnl-loss";
    const headPnl = block.querySelector(".strat-pnl");
    if (headPnl) {
      headPnl.className = `strat-pnl ${pnlClass}`;
      const openN = trades.filter((t) => t._closed === false).length;
      const openTxt = openN ? ` · ${openN} откр.` : "";
      headPnl.textContent = `${trades.length} сд. · ${livePnl >= 0 ? "+" : ""}${livePnl.toFixed(4)}${openTxt}`;
    }
    const stats = block.querySelector(".trades-stats");
    const panel = block.querySelector(".trades-panel");
    if (stats) stats.innerHTML = buildStatsHtml(trades);
    if (panel) {
      panel.innerHTML = buildTradesListHtml(trades);
      panel.querySelectorAll(".trade-row").forEach((row) => {
        row.addEventListener("click", () => {
          const tr = trades.find((t) => t.id === row.dataset.tradeId);
          if (tr) selectTrade(tr).catch((e) => log(e.message));
        });
      });
    }
    const overviewWrap = block.querySelector(".pair-overview");
    if (overviewWrap && botInstances.some((i) => i.scenario_id === scenarioId)) {
      overviewWrap.outerHTML = buildPairOverviewHtml(scenarioId);
    }
    updatePortfolioSummary();
  }

  function markReplayStrategyCards() {
    document.querySelectorAll(".strategy-block").forEach((block) => {
      const sid = block.dataset.scenario;
      block.classList.toggle("replaying", sid === activeReplayScenario);
      block.classList.toggle("replay-done", sequentialReplay && liveTradesByScenario.has(sid) && sid !== activeReplayScenario);
    });
  }

  function resetBotsUi(clearLog) {
    resetLiveReplay();
    botInstances = [];
    botsRuntimeMap = new Map();
    selectedBotId = null;
    selectedTradeId = null;
    selectedTradeHighlight = null;
    botsRuntime = { bots: [], active_trade: null, highlight: null };
    lastCurrentMs = 0;
    renderBotList();
    renderStrategyPanels();
    renderBotDetail(null);
    $("tradeHighlights").innerHTML = "";
    if (candleSeries) candleSeries.setMarkers([]);
    hidePriceLines();
    if (clearLog) $("logBox").textContent = "";
  }

  function collectRawTradesForScenario(scenarioId) {
    const rows = [];
    for (const inst of botInstances) {
      if (inst.scenario_id !== scenarioId) continue;
      for (const t of inst.raw_trades || inst.trades || []) {
        rows.push({
          id: `${inst.id}:${t.open_ms}`,
          bot_id: inst.id,
          scenario_id: inst.scenario_id,
          label: inst.label,
          pair: inst.pair,
          stake_usdt: inst.config?.stake,
          ...t,
        });
      }
    }
    return rows.sort((a, b) => a.open_ms - b.open_ms);
  }

  function collectTradesForScenario(scenarioId) {
    const rows = [];
    for (const inst of botInstances) {
      if (inst.scenario_id !== scenarioId) continue;
      for (const t of inst.trades || []) {
        rows.push({
          id: `${inst.id}:${t.open_ms}`,
          bot_id: inst.id,
          scenario_id: inst.scenario_id,
          label: inst.label,
          pair: inst.pair,
          stake_usdt: inst.config?.stake,
          ...t,
        });
      }
    }
    return rows.sort((a, b) => a.open_ms - b.open_ms);
  }

  function collectAllTrades() {
    const rows = [];
    for (const inst of botInstances) {
      for (const t of inst.trades || []) {
        rows.push({
          id: `${inst.id}:${t.open_ms}`,
          bot_id: inst.id,
          scenario_id: inst.scenario_id,
          label: inst.label,
          pair: inst.pair,
          stake_usdt: inst.config?.stake,
          ...t,
        });
      }
    }
    return rows.sort((a, b) => a.open_ms - b.open_ms);
  }

  function computeTradeStats(trades) {
    let grossProfit = 0;
    let grossLoss = 0;
    let wins = 0;
    const byPair = new Map();
    for (const tr of trades) {
      if (tr.profit_abs >= 0) {
        wins += 1;
        grossProfit += tr.profit_abs;
      } else {
        grossLoss += tr.profit_abs;
      }
      const sym = tr.pair.split("/")[0];
      const row = byPair.get(sym) || { sym, pair: tr.pair, count: 0, wins: 0, losses: 0, profit: 0, loss: 0, pnl: 0 };
      row.count += 1;
      row.pnl += tr.profit_abs;
      if (tr.profit_abs >= 0) {
        row.wins += 1;
        row.profit += tr.profit_abs;
      } else {
        row.losses += 1;
        row.loss += tr.profit_abs;
      }
      byPair.set(sym, row);
    }
    return {
      total: trades.length,
      wins,
      losses: trades.length - wins,
      profit: grossProfit,
      loss: grossLoss,
      pnl: grossProfit + grossLoss,
      winRate: trades.length ? (wins / trades.length) * 100 : 0,
      pairs: [...byPair.values()].sort((a, b) => b.count - a.count || a.sym.localeCompare(b.sym)),
    };
  }

  function updatePortfolioSummary() {
    const wallet = playerProfile.wallet_usdt || 100;
    const target = playerProfile.target_monthly_pct || 30;
    let total = 0;
    if (sequentialReplay || replayComplete || liveTradesByScenario.size) {
      for (const [, map] of liveTradesByScenario) {
        for (const tr of map.values()) {
          if (tr._closed !== false) total += tr.profit_abs || 0;
        }
      }
    } else {
      total = botInstances.reduce((s, i) => s + (i.summary?.profit_abs ?? 0), 0);
    }
    const pct = wallet ? (total / wallet) * 100 : 0;
    const el = $("portfolioLabel");
    if (!botInstances.length) {
      el.textContent = `100 USDT · цель ${target}%`;
      el.className = "muted";
      return;
    }
    const ok = pct >= target;
    el.textContent = `${(wallet + total).toFixed(2)} USDT · ${total >= 0 ? "+" : ""}${total.toFixed(2)} (${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%)`;
    el.className = ok ? "pnl-win" : pct >= 0 ? "muted" : "pnl-loss";
  }

  function fmtPnl(val, signed) {
    if (!val) return "—";
    const s = signed && val > 0 ? "+" : "";
    return `${s}${val.toFixed(4)}`;
  }

  function buildStatsHtml(trades) {
    if (!trades.length) {
      return '<span class="muted">Нет сделок после Play</span>';
    }
    const st = computeTradeStats(trades);
    const pnlClass = st.pnl >= 0 ? "pnl-win" : "pnl-loss";
    const pairRows = st.pairs
      .slice(0, 8)
      .map((p) => {
        const netClass = p.pnl >= 0 ? "pnl-win" : "pnl-loss";
        return `<div class="pair-stat">
          <span class="sym">${p.sym}</span>
          <span class="cnt">${p.count}</span>
          <span class="wl">${p.wins}/${p.losses}</span>
          <span class="pnl-win">${fmtPnl(p.profit, true)}</span>
          <span class="pnl-loss">${fmtPnl(p.loss, false)}</span>
          <span class="${netClass}">${fmtPnl(p.pnl, true)}</span>
        </div>`;
      })
      .join("");
    const morePairs = st.pairs.length > 8 ? `<div class="muted" style="font-size:0.68rem;margin-top:4px">ещё ${st.pairs.length - 8} пар</div>` : "";
    return `
      <div class="stats-head">
        <span>Пара</span><span>N</span><span>W/L</span><span>Профит</span><span>Потери</span><span>Итого</span>
      </div>
      <div class="stats-total pair-stat">
        <span class="sym">Всего</span>
        <span class="cnt">${st.total}</span>
        <span class="wl">${st.wins}/${st.losses}</span>
        <span class="pnl-win">${fmtPnl(st.profit, true)}</span>
        <span class="pnl-loss">${fmtPnl(st.loss, false)}</span>
        <span class="${pnlClass}">${fmtPnl(st.pnl, true)}</span>
      </div>
      <div class="stats-pairs">${pairRows}${morePairs}</div>
    `;
  }

  function buildTradesListHtml(trades) {
    if (!trades.length) {
      return '<span class="muted">—</span>';
    }
    return trades
      .map((tr) => {
        const win = (tr.profit_abs ?? 0) >= 0;
        const sel = selectedTradeId === tr.id ? " selected" : "";
        const stake = tr.stake_usdt ? `${tr.stake_usdt} USDT · ` : "";
        const open = tr._closed === false;
        const pnlTxt = open ? "…" : `${win ? "+" : ""}${(tr.profit_abs ?? 0).toFixed(4)}`;
        const pnlClass = open ? "muted" : win ? "pnl-win" : "pnl-loss";
        const ml = mlForTrade(tr);
        const mlHtml = ml ? fmtMlBadge(ml, !open) : "";
        return `<div class="trade-row ${win ? "win" : "loss"}${sel}" data-trade-id="${tr.id}">
          <div class="head">${tr.pair.split("/")[0]} ${mlHtml}</div>
          <div class="meta">${fmtTime(tr.open_ms)} → ${fmtTime(tr.close_ms)}</div>
          <div class="meta ${pnlClass}">${stake}${pnlTxt} · ${tr.exit_reason || (open ? "открыта" : "—")}</div>
        </div>`;
      })
      .join("");
  }

  async function setScenarioEnabled(id, enabled) {
    await api(`/sim/scenarios/${id}`, { method: "PATCH", body: JSON.stringify({ enabled }) });
    const sc = scenarioCatalog.find((s) => s.id === id);
    if (sc) sc.enabled = enabled;
    renderStrategyPanels();
    log(`${enabled ? "Вкл" : "Выкл"}: ${sc?.label || id} — нажмите Play для пересчёта`);
  }

  async function setAllScenariosEnabled(enabled) {
    const res = await api("/sim/scenarios/enabled-bulk", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    for (const sc of scenarioCatalog) {
      sc.enabled = enabled;
    }
    renderStrategyPanels();
    log(`${enabled ? "Включены" : "Выключены"} все стратегии (${res.updated})`);
  }

  function isScenarioEnabled(sc) {
    return sc.enabled !== false;
  }

  function renderStrategyPanels(force = false) {
    if (!force && shouldPreserveLiveCards() && document.querySelector("#strategyPanels .strategy-block")) {
      const countEl = document.querySelector(".strategy-toolbar .muted");
      if (countEl && scenarioCatalog.length) {
        countEl.textContent = `${scenarioCatalog.filter(isScenarioEnabled).length}/${scenarioCatalog.length} включено`;
      }
      return;
    }
    const root = $("strategyPanels");
    const scrollByScenario = new Map();
    root.querySelectorAll(".strategy-block").forEach((block) => {
      const panel = block.querySelector(".trades-panel");
      if (panel) scrollByScenario.set(block.dataset.scenario, panel.scrollTop);
    });
    const catalog = scenarioCatalog.length
      ? scenarioCatalog
      : [...new Map(botInstances.map((i) => [i.scenario_id, { id: i.scenario_id, label: i.label, article: i.article || "", group: i.group, stake_usdt: i.config?.stake, settings: i.settings, enabled: true }])).values()];
    if (!catalog.length) {
      root.innerHTML = '<p class="muted">▶ Play — боты по очереди, пара за парой</p>';
      return;
    }
    const toolbar = document.createElement("div");
    toolbar.className = "strategy-toolbar";
    toolbar.innerHTML = `
      <button type="button" class="btn sm" id="btnScenariosAll">Все вкл.</button>
      <button type="button" class="btn sm" id="btnScenariosNone">Все выкл.</button>
      <span class="muted">${catalog.filter(isScenarioEnabled).length}/${catalog.length} включено</span>
    `;
    root.innerHTML = "";
    root.appendChild(toolbar);
    toolbar.querySelector("#btnScenariosAll").addEventListener("click", () =>
      setAllScenariosEnabled(true).catch((e) => log(e.message)),
    );
    toolbar.querySelector("#btnScenariosNone").addEventListener("click", () =>
      setAllScenariosEnabled(false).catch((e) => log(e.message)),
    );
    const groups = [
      { key: "live", title: "Live боты" },
      { key: "trend", title: "Тренд-стратегии" },
      { key: "lite", title: "LiteFinance" },
      { key: "scalp", title: "Scalp" },
      { key: "scalp_liq", title: "Liquidity / Alt breakout" },
      { key: "newset", title: "New set" },
      { key: "chart", title: "Chart TA" },
      { key: "chart2", title: "Chart TA · wave 2" },
      { key: "chart3", title: "Chart TA · wave 3" },
      { key: "chart4", title: "Chart TA · wave 4" },
      { key: "combo", title: "Combo" },
    ];
    const known = new Set(groups.map((g) => g.key));
    for (const g of groups) {
      const items = catalog.filter((sc) => (sc.group || "lite") === g.key);
      if (!items.length) continue;
      const groupWrap = document.createElement("div");
      groupWrap.className = "strategy-group";
      const h = document.createElement("h3");
      h.className = "strategy-group-title";
      h.textContent = `${g.title} (${items.length})`;
      groupWrap.appendChild(h);
      const cards = document.createElement("div");
      cards.className = "strategy-group-cards";
      for (const sc of items) {
        cards.appendChild(buildStrategyBlock(sc));
      }
      groupWrap.appendChild(cards);
      root.appendChild(groupWrap);
    }
    const other = catalog.filter((sc) => sc.group && !known.has(sc.group));
    if (other.length) {
      const groupWrap = document.createElement("div");
      groupWrap.className = "strategy-group";
      const h = document.createElement("h3");
      h.className = "strategy-group-title";
      h.textContent = `Прочие (${other.length})`;
      groupWrap.appendChild(h);
      const cards = document.createElement("div");
      cards.className = "strategy-group-cards";
      for (const sc of other) cards.appendChild(buildStrategyBlock(sc));
      groupWrap.appendChild(cards);
      root.appendChild(groupWrap);
    }
    root.querySelectorAll(".strategy-block").forEach((block) => {
      const top = scrollByScenario.get(block.dataset.scenario);
      const panel = block.querySelector(".trades-panel");
      if (panel && top != null) panel.scrollTop = top;
    });
  }

  function buildPairOverviewHtml(scenarioId) {
    const rows = botInstances.filter((i) => i.scenario_id === scenarioId);
    if (!rows.length) return "";
    const sorted = [...rows].sort((a, b) => {
      const pa = a.summary?.profit_abs ?? -999;
      const pb = b.summary?.profit_abs ?? -999;
      return pb - pa;
    });
    const cells = sorted
      .map((inst) => {
        const sym = inst.pair.split("/")[0];
        const n = (inst.trades || []).length;
        const pnl = inst.summary?.profit_abs ?? 0;
        const inScan = inst.trading_active !== false;
        const badge = n > 0 ? "✓" : inScan ? "✓" : "○";
        const pnlClass = n > 0 ? (pnl >= 0 ? "pnl-win" : "pnl-loss") : "muted";
        const pnlTxt = n > 0 ? `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)} · ${n} сд.` : "0 сд.";
        const chipClass = n > 0 ? "active" : inScan ? "active" : "skipped";
        return `<div class="pair-chip ${chipClass}">
          <span class="sym">${badge} ${sym}</span>
          <span class="${pnlClass}">${pnlTxt}</span>
        </div>`;
      })
      .join("");
    const activeN = sorted.filter((i) => (i.trades || []).length > 0).length;
    return `<div class="pair-overview">
      <div class="pair-overview-head">Пары ${activeN}/${sorted.length} · PnL симуляции (○ — не в whitelist)</div>
      <div class="pair-chips">${cells}</div>
    </div>`;
  }

  function buildStrategyBlock(sc) {
    const enabled = isScenarioEnabled(sc);
    const useLive = shouldPreserveLiveCards();
    const displayTrades = tradesForStrategyDisplay(sc);
    const simPnl = botInstances
      .filter((i) => i.scenario_id === sc.id)
      .reduce((s, i) => s + (i.summary?.profit_abs ?? 0), 0);
    const livePnl = displayTrades.filter((t) => t._closed !== false).reduce((s, t) => s + (t.profit_abs || 0), 0);
    const block = document.createElement("div");
    const isReplaying = sc.id === activeReplayScenario;
    block.className = `strategy-block${displayTrades.length ? "" : " empty"}${enabled ? "" : " disabled"}${isReplaying ? " replaying" : ""}${useLive && !isReplaying && displayTrades.length ? " replay-done" : ""}`;
    block.dataset.scenario = sc.id;
    const pnl = useLive ? livePnl : simPnl;
    const pnlClass = pnl >= 0 ? "pnl-win" : "pnl-loss";
    const stakeVal = effectiveStake(sc);
    const stakeTxt =
      playerProfile.stake_mode === "uniform"
        ? `стейк ${stakeVal} USDT (единый) · max ${sc.max_open_trades || 1} поз.`
        : `стейк <input type="number" class="stake-inline" data-scenario-id="${sc.id}" min="1" max="500" step="1" value="${stakeVal}" title="USDT на сделку" /> · max ${sc.max_open_trades || 1} поз.`;
    const liveHint = useLive && !replayComplete ? " · live" : replayComplete ? " · готово" : "";
    const mlBlocked = botInstances
      .filter((i) => i.scenario_id === sc.id)
      .reduce((s, i) => s + (i.ml_gate?.skipped || 0), 0);
    const mlBlockedTxt = mlBlocked ? ` · ML −${mlBlocked}` : "";
    const openN = useLive ? displayTrades.filter((t) => t._closed === false).length : 0;
    const openTxt = openN ? ` · ${openN} откр.` : "";
    block.innerHTML = `
      <div class="strat-head">
        <label class="strat-toggle" title="${enabled ? "Выключить из симуляции" : "Включить в симуляцию"}">
          <input type="checkbox" data-scenario-id="${sc.id}" ${enabled ? "checked" : ""} />
          <span class="toggle-ui" aria-hidden="true"></span>
        </label>
        <h3>${sc.label}${isReplaying ? " ▶" : ""}</h3>
        <span class="strat-pnl ${pnlClass}">${displayTrades.length ? `${displayTrades.length} сд. · ${pnl >= 0 ? "+" : ""}${pnl.toFixed(4)}${openTxt}${mlBlockedTxt}` : useLive ? "ожидание…" : `${pnl >= 0 ? "+" : ""}${pnl.toFixed(4)} USDT${mlBlockedTxt}`}</span>
      </div>
      <div class="strat-desc">${stakeTxt ? `${stakeTxt} · ` : ""}${sc.settings || sc.article || ""}${liveHint}</div>
      ${enabled ? buildPairOverviewHtml(sc.id) : '<p class="muted strat-off">Выключено — не участвует в Play</p>'}
      <div class="trades-stats">${enabled ? buildStatsHtml(displayTrades) : ""}</div>
      <div class="trades-panel">${enabled ? buildTradesListHtml(displayTrades) : ""}</div>
    `;
    const toggle = block.querySelector(".strat-toggle input");
    toggle.addEventListener("click", (e) => e.stopPropagation());
    toggle.addEventListener("change", (e) => {
      setScenarioEnabled(sc.id, e.target.checked).catch((err) => {
        e.target.checked = !e.target.checked;
        log(err.message);
      });
    });
    block.querySelectorAll(".stake-inline").forEach((inp) => {
      inp.addEventListener("click", (e) => e.stopPropagation());
      inp.addEventListener("change", (e) => {
        setScenarioStake(inp.dataset.scenarioId, inp.value).catch((err) => log(err.message));
      });
    });
    block.querySelectorAll(".trade-row").forEach((row) => {
      row.addEventListener("click", () => {
        const tr = displayTrades.find((t) => t.id === row.dataset.tradeId);
        if (tr) selectTrade(tr).catch((e) => log(e.message));
      });
    });
    return block;
  }

  function renderTradesList() {
    if (shouldPreserveLiveCards()) {
      updatePortfolioSummary();
      return;
    }
    renderStrategyPanels(true);
    updatePortfolioSummary();
  }

  async function selectTrade(tr) {
    selectedTradeId = tr.id;
    selectedBotId = tr.bot_id;
    selectedTradeHighlight = {
      bot_id: tr.bot_id,
      label: tr.label,
      pair: tr.pair,
      trades: [tr],
      summary: { total_trades: 1, profit_abs: tr.profit_abs, wins: tr.profit_abs >= 0 ? 1 : 0, losses: tr.profit_abs >= 0 ? 0 : 1 },
    };

    await loadChartForTrade(tr);
    showPriceLines(tr);
    renderTradesList();
    renderBotList();
    renderBotDetail(selectedTradeHighlight);

    chart.timeScale().setVisibleRange({
      from: Math.floor(tr.open_ms / 1000) - 120,
      to: Math.floor(tr.close_ms / 1000) + 120,
    });
    log(`Сделка: ${tr.label} ${tr.pair.split("/")[0]} · ${tr.exit_reason || "—"}`);
  }

  function renderBotDetail(highlight) {
    const el = $("botDetail");
    if (!highlight) {
      el.classList.add("hidden");
      return;
    }
    el.classList.remove("hidden");
    const rows = highlight.trades
      .map(
        (t) =>
          `<div>${fmtTime(t.open_ms)} → ${fmtTime(t.close_ms)} · ${t.profit_abs >= 0 ? "+" : ""}${t.profit_abs.toFixed(4)} USDT · ${t.exit_reason}</div>`
      )
      .join("");
    el.innerHTML = `
      <h4>${highlight.label} · ${highlight.pair}</h4>
      <div>Итого: ${highlight.summary.total_trades} сд., ${highlight.summary.profit_abs >= 0 ? "+" : ""}${highlight.summary.profit_abs} USDT</div>
      ${rows || "<div class='muted'>Сделок нет — бот ждёт сигнал</div>"}
    `;
  }

  function drawHighlights() {
    const container = $("tradeHighlights");
    if (!container) return;
    container.innerHTML = "";
    const liveHl = collectLiveHighlight();
    const hl = liveHl || selectedTradeHighlight || botsRuntime.highlight;
    if (!hl || !chart || !loadedCandles.length) {
      if (!hl && candleSeries) candleSeries.setMarkers([]);
      return;
    }
    const ts = chart.timeScale();
    const trades = hl.trades || [];
    const isLive = !!hl._liveMode || uiMode === "live";
    for (const tr of trades) {
      const endMs = tr._liveOpen
        ? Math.max(lastCurrentMs || tr.open_ms, tr.open_ms)
        : tr.close_ms;
      const x1 = ts.timeToCoordinate(Math.floor(tr.open_ms / 1000));
      const x2 = ts.timeToCoordinate(Math.floor(endMs / 1000));
      if (x1 == null || x2 == null) continue;
      const div = document.createElement("div");
      const tid = tr.id || `${tr.bot_id || ""}:${tr.open_ms}`;
      const isSelected = isLive && selectedLiveTradeId && tid === selectedLiveTradeId;
      if (isLive) {
        div.className =
          "trade-zone " +
          (tr._liveOpen ? "live-open" : "live-closed") +
          (isSelected ? " live-selected" : "");
      } else {
        div.className = "trade-zone " + (tr.profit_abs >= 0 ? "win" : "loss") + " selected";
      }
      div.style.left = Math.min(x1, x2) + "px";
      div.style.width = Math.max(Math.abs(x2 - x1), 4) + "px";
      container.appendChild(div);
    }
    if (isLive) {
      const markers = trades.flatMap((t) => {
        const m = [
          {
            time: Math.floor(t.open_ms / 1000),
            position: "belowBar",
            color: "#d29922",
            shape: "arrowUp",
            text: "In",
          },
        ];
        if (!t._liveOpen && t.close_ms) {
          m.push({
            time: Math.floor(t.close_ms / 1000),
            position: "aboveBar",
            color: t.profit_abs >= 0 ? "#3fb950" : "#f85149",
            shape: "circle",
            text: (t.profit_abs >= 0 ? "+" : "") + Number(t.profit_abs || 0).toFixed(3),
          });
        }
        return m;
      });
      candleSeries.setMarkers(markers);
      return;
    }
    const markers = trades.flatMap((t) => [
      {
        time: Math.floor(t.open_ms / 1000),
        position: "belowBar",
        color: "#58a6ff",
        shape: "arrowUp",
        text: "In",
      },
      {
        time: Math.floor(t.close_ms / 1000),
        position: "aboveBar",
        color: t.profit_abs >= 0 ? "#3fb950" : "#f85149",
        shape: "circle",
        text: (t.profit_abs >= 0 ? "+" : "") + Number(t.profit_abs || 0).toFixed(3),
      },
    ]);
    candleSeries.setMarkers(markers);
  }

  async function selectBot(id) {
    selectedBotId = id;
    selectedTradeId = null;
    selectedTradeHighlight = null;
    const res = await api("/sim/bots/select", { method: "POST", body: JSON.stringify({ id }) });
    if (res.pair && res.pair !== $("pairSelect").value) {
      $("pairSelect").value = res.pair;
      await loadChart(true);
    }
    botsRuntime = res.bots_runtime || botsRuntime;
    renderBotList();
    renderBotDetail(botsRuntime.highlight);
    drawHighlights();
    renderTradesList();
    log(`Выбран бот ${id}`);
  }

  function applyRuntime(rt) {
    if (!rt) return;
    botsRuntime = rt;
    if (rt.highlight && !selectedTradeHighlight) renderBotDetail(rt.highlight);
    if (!selectedTradeHighlight) showPriceLines(rt.active_trade);
    drawHighlights();
  }

  function updateUI(snap) {
    if (snap.type === "finished" && (snap.stopped || snap.status === "stopped")) {
      setSimRunning(false);
    }
    if (snap.type === "scan_tick") {
      $("statusLabel").textContent = "scanning";
      if (snap.current_iso) $("clockLabel").textContent = snap.current_iso + " (сканер)";
    } else if (snap.status) {
      $("statusLabel").textContent = snap.status || "—";
      $("clockLabel").textContent = snap.current_iso || "—";
    }
    if (snap.current_ms) {
      $("clockLabel").dataset.ms = snap.current_ms;
      lastCurrentMs = snap.current_ms;
      if (uiMode === "live") drawHighlights();
    }
    if (snap.candle) $("priceLabel").textContent = snap.candle.close?.toFixed(6) ?? "—";
    if (snap.engine) $("equityLabel").textContent = `${snap.engine.equity} USDT`;
    if (snap.active_scenario_id) {
      activeReplayScenario = snap.active_scenario_id;
      sequentialReplay = !!snap.sequential_replay;
      markReplayStrategyCards();
    }
    if (snap.active_scenario_id && snap.live_trades) {
      syncLiveTradesFromTick(snap);
    }
    if (snap.bots_all) {
      mergeRuntimeBots(snap.bots_all);
      renderBotList();
    } else if (snap.type === "tick" || snap.type === "scan_tick") {
      renderBotList();
    }
    if (snap.bots_runtime) applyRuntime(snap.bots_runtime);
    if (snap.scan_events_visible?.length) {
      const last = snap.scan_events_visible[snap.scan_events_visible.length - 1];
      log(`Скан: ${last.scenario_id} → ${last.pair}`);
    }
  }

  function onBotsEvent(msg) {
    if (msg.event === "session_ready" || msg.event === "scanning") {
      pairReplayLogged = new Set();
      resetBotsUi(false);
      sequentialReplay = true;
      renderStrategyPanels(true);
      if (msg.event === "scanning") log("Сканер истории (30m)…");
      return;
    }
    if (msg.instances && msg.event !== "loaded") {
      botInstances = msg.instances;
    }
    if (msg.selected_id !== undefined) selectedBotId = msg.selected_id;
    if (!sequentialReplay && !replayComplete) {
      renderBotList();
    }
    if (msg.event === "strategy_loading") {
      const sid = Object.keys(msg.scenarios || {})[0];
      const sc = msg.scenarios?.[sid];
      if (sc) log(`⏳ ${sc.label}: расчёт ${sc.pairs ?? "?"} пар…`);
      return;
    }
    if (msg.event === "strategy_loaded" || msg.event === "pair_loaded") {
      api("/sim/bots/instances")
        .then((res) => {
          botInstances = res.instances || botInstances;
          const sid = Object.keys(msg.scenarios || {})[0];
          if (sid) {
            if (!batchRun) hydrateLiveTradesFromInstances(sid);
            refreshStrategyCard(sid);
          }
          renderStrategyPanels(false);
          updatePortfolioSummary();
          if (msg.event === "strategy_loaded" && msg.scenarios?.[sid]) {
            const sc = msg.scenarios[sid];
            const n = botInstances
              .filter((i) => i.scenario_id === sid)
              .reduce((s, i) => s + (i.trades || []).length, 0);
            if (!batchRun) log(`✓ ${sc.label}: ${n} сд. · ${sc.pairs ?? "?"} пар`);
          }
        })
        .catch(() => {});
      return;
    }
    if (msg.event === "loaded") {
      api("/sim/bots/instances")
        .then((res) => {
          botInstances = res.instances || botInstances;
          renderBotList();
          sequentialReplay = true;
          renderStrategyPanels(true);
          updatePortfolioSummary();
          log(`Симуляция завершена: ${botInstances.length} инстансов · сделки ${botInstances.filter((i) => (i.trades || []).length).length}`);
          if (!botInstances.length) {
            log("⚠ Нет сделок — проверьте период, пары и включённые боты");
          }
          const pf = msg.portfolio;
          if (pf) {
            log(`Портфель: ${pf.pnl_usdt >= 0 ? "+" : ""}${pf.pnl_usdt} USDT (${pf.pnl_pct}%)`);
          }
        })
        .catch((e) => log(`Ошибка загрузки сделок: ${e.message}`));
      return;
    }
  }

  function connectWs() {
    ws = new WebSocket(`${API.replace("http", "ws")}/sim/ws`);
    ws.onopen = () => {
      $("connStatus").textContent = "online";
      $("connStatus").className = "conn on";
    };
    ws.onclose = () => {
      $("connStatus").textContent = "offline";
      $("connStatus").className = "conn off";
      setTimeout(connectWs, 2000);
    };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "batch_trades") onBatchTrades(msg);
      if (msg.type === "trade_live") onTradeLive(msg);
      if (msg.type === "tick" || msg.type === "hello" || msg.type === "finished" || msg.type === "scan_tick" || msg.type === "scan_done") {
        updateUI(msg);
      }
      if (msg.type === "bots") onBotsEvent(msg);
      if (msg.type === "scan_arm") {
        log(`Скан ${fmtTime(msg.sim_ms)}: ${msg.scenario_id} → ${msg.pair?.split("/")[0]}`);
      }
      if (msg.type === "phase") {
        $("statusLabel").textContent = msg.phase || "—";
        if (msg.phase === "prgon_start") {
          sequentialReplay = true;
          batchRun = true;
          setSimRunning(true);
          renderStrategyPanels(true);
          log(`Прогон: ${msg.strategies ?? "?"} ботов × ${msg.pairs ?? "?"} пар · параллельно ${msg.workers ?? PRGON_WORKERS}`);
        }
        if (msg.phase === "sim_start") {
          sequentialReplay = true;
          renderStrategyPanels(true);
          log(`Симуляция: ${msg.strategies ?? "?"} ботов × ${msg.pairs ?? "?"} пар`);
        }
        if (msg.phase === "strategy_loading") {
          activeReplayScenario = msg.scenario_id;
          sequentialReplay = true;
          ensureStrategyCard(msg.scenario_id);
          markReplayStrategyCards();
          log(`⏳ [${msg.index}/${msg.total}] ${msg.label} — расчёт ${msg.pairs_total} пар…`);
        }
        if (msg.phase === "strategy_replay") {
          activeReplayScenario = msg.scenario_id;
          sequentialReplay = true;
          ensureStrategyCard(msg.scenario_id);
          liveTradesByScenario.set(msg.scenario_id, new Map());
          markReplayStrategyCards();
          refreshStrategyCard(msg.scenario_id);
          log(`▶ [${msg.index}/${msg.total}] ${msg.label} — ${msg.pairs_total ?? "?"} пар`);
        }
        if (msg.phase === "pair_replay") {
          const sym = msg.pair?.split("/")[0] || msg.pair;
          const key = `${msg.scenario_id}:${msg.pair}`;
          if (!pairReplayLogged.has(key)) {
            pairReplayLogged.add(key);
            log(`  ▶ ${msg.label} · ${sym} (${msg.pair_index}/${msg.pairs_total})`);
          }
          activeReplayScenario = msg.scenario_id;
          markReplayStrategyCards();
          refreshStrategyCard(msg.scenario_id);
        }
        if (msg.phase === "pair_done") {
          refreshStrategyCard(msg.scenario_id);
        }
        if (msg.phase === "strategy_done") {
          hydrateLiveTradesFromInstances(msg.scenario_id);
          refreshStrategyCard(msg.scenario_id);
          log(`■ [${msg.index}/${msg.total}] ${msg.label} завершён`);
        }
        if (msg.phase === "strategy_error") {
          log(`✗ ${msg.label}: ${msg.message || "ошибка"}`);
        }
        if (msg.phase === "archive_done") {
          log(
            `База сделок: +${msg.trades_saved} новых, пропуск дублей ${msg.trades_skipped_duplicate}, net ${msg.net_usdt} USDT`,
          );
        }
        if (msg.phase === "sequential_done") {
          finalizeSequentialReplay();
          log(batchRun ? "Прогон завершён" : "Все боты проиграны");
        }
        if (msg.phase === "error") {
          log(`Ошибка: ${msg.message || "симуляция"}`);
          setSimRunning(false);
        }
        if (msg.phase === "reset") {
          if (msg.scope === "bots" || msg.scope === "all" || msg.scope === "probe") {
            resetBotsUi(false);
          }
        }
      }
    };
  }

  async function play() {
    const pair = $("pairSelect").value;
    const start_ms = localInputToMs($("dateFrom").value);
    const end_ms = localInputToMs($("dateTo").value);
    if (!pair || !start_ms || !end_ms) {
      log("Сначала загрузите график (пара + период)");
      return;
    }
    resetBotsUi(false);
    const simPairs = getSelectedSimPairs();
    setSimRunning(true);
    $("statusLabel").textContent = "loading bots…";
    log(`Play: ${simPairs.length} пар → симуляция ботов…`);
    try {
      const res = await api("/sim/player/play", {
        method: "POST",
        body: JSON.stringify({
          pair,
          pairs: simPairs,
          range_start_ms: start_ms,
          range_end_ms: end_ms,
          speed: currentSpeed,
        }),
      });
      updateUI(res);
      botInstances = (await api("/sim/bots/instances")).instances || botInstances;
      renderBotList();
      if (res.sequential) sequentialReplay = true;
      renderStrategyPanels(true);
      if (res.sequential) {
        log(`Последовательный replay ×${currentSpeed} — сделки в карточках`);
      }
    } catch (e) {
      log(`Ошибка: ${e.message}`);
      setSimRunning(false);
    }
  }

  async function runPrgon() {
    const pair = $("pairSelect").value;
    const start_ms = localInputToMs($("dateFrom").value);
    const end_ms = localInputToMs($("dateTo").value);
    if (!pair || !start_ms || !end_ms) {
      log("Сначала загрузите график (пара + период)");
      return;
    }
    resetBotsUi(false);
    const simPairs = getSelectedSimPairs();
    setSimRunning(true);
    $("statusLabel").textContent = "прогон…";
    log(`Прогон: ${simPairs.length} пар · ${PRGON_WORKERS} бота параллельно`);
    try {
      const res = await api("/sim/player/prgon", {
        method: "POST",
        body: JSON.stringify({
          pair,
          pairs: simPairs,
          range_start_ms: start_ms,
          range_end_ms: end_ms,
          workers: PRGON_WORKERS,
        }),
      });
      updateUI(res);
      botInstances = (await api("/sim/bots/instances")).instances || botInstances;
      renderStrategyPanels(true);
      log("Прогон запущен — сделки появятся в карточках без часов");
    } catch (e) {
      log(`Ошибка: ${e.message}`);
      setSimRunning(false);
    }
  }

  async function setSpeed(speed) {
    currentSpeed = speed;
    const label = `${speed} sim-с/с`;
    if ($("speedLabel")) $("speedLabel").textContent = label;
    document.querySelectorAll(".btn.speed").forEach((b) => {
      b.classList.toggle("active", Number(b.dataset.speed) === speed);
    });
    await api("/sim/player/speed", { method: "POST", body: JSON.stringify({ speed }) });
  }

  async function runAllBots() {
    const pairs = getSelectedSimPairs();
    const start_ms = localInputToMs($("dateFrom").value);
    const end_ms = localInputToMs($("dateTo").value);
    resetBotsUi(false);
    log("Плеер остановлен. Запуск ботов…");
    const res = await api("/sim/bots/run-all", {
      method: "POST",
      body: JSON.stringify({ pairs, range_start_ms: start_ms, range_end_ms: end_ms }),
    });
    if (!res.ok) {
      log(res.error || "уже запущено");
      return;
    }
    updateUI(res.player || {});
    $("statusLabel").textContent = "paused";
  }

  $("pairSelect").addEventListener("change", async (e) => {
    await loadRange(e.target.value);
    selectedTradeId = null;
    selectedTradeHighlight = null;
    if (selectedBotId) {
      const inst = botInstances.find((b) => b.id === selectedBotId);
      if (inst && inst.pair !== e.target.value) {
        selectedBotId = null;
        $("botDetail").classList.add("hidden");
        candleSeries.setMarkers([]);
        $("tradeHighlights").innerHTML = "";
        hidePriceLines();
      }
    }
    renderBotList();
    renderTradesList();
    drawHighlights();
  });
  $("btnLoad")?.addEventListener("click", () => loadChart().catch((e) => log(e.message)));
  $("btnChartOpen")?.addEventListener("click", () => loadChart().catch((e) => log(e.message)));
  $("btnLiveLoad")?.addEventListener("click", () => liveLoad().catch((e) => log(e.message)));
  $("btnLivePlay")?.addEventListener("click", () => findTrades().catch((e) => log(e.message)));
  $("btnGridScan")?.addEventListener("click", () => runGridScan().catch((e) => log(e.message)));
  $("liveStrategySelect")?.addEventListener("change", () => {
    renderLiveTradesPanel();
    drawHighlights();
  });
  document.querySelectorAll(".mode-tab").forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });
  $("dateFrom")?.addEventListener("change", syncLivePeriodLabel);
  $("dateTo")?.addEventListener("change", syncLivePeriodLabel);
  $("btnStakeApply")?.addEventListener("click", () => saveProfileStake().catch((e) => log(e.message)));
  $("stakeModeSelect")?.addEventListener("change", () => {
    $("stakeAmountWrap")?.classList.toggle("hidden", $("stakeModeSelect").value === "scenario");
  });
  $("btnPlay").addEventListener("click", () => play().catch((e) => log(e.message)));
  $("btnPrgon").addEventListener("click", () => runPrgon().catch((e) => log(e.message)));
  const mlGateEl = $("mlGateToggle");
  if (mlGateEl) {
    mlGateEl.addEventListener("change", () => {
      setMlGateEnabled(mlGateEl.checked)
        .then(() => log("Запустите Play / Прогон / «Найти сделки» заново"))
        .catch((e) => {
          mlGateEl.checked = !mlGateEl.checked;
          log(e.message);
        });
    });
  }
  const mlGateLiveEl = $("mlGateToggleLive");
  if (mlGateLiveEl) {
    mlGateLiveEl.addEventListener("change", () => {
      setMlGateEnabled(mlGateLiveEl.checked)
        .then(() => {
          if (uiMode === "live" && liveTradesByScenario.size) {
            log("ML gate изменён — нажмите «Найти сделки» ещё раз (кэш переиспользуется)");
          }
        })
        .catch((e) => {
          mlGateLiveEl.checked = !mlGateLiveEl.checked;
          log(e.message);
        });
    });
  }
  $("btnPause").addEventListener("click", () => api("/sim/player/pause", { method: "POST" }).then(updateUI));
  $("btnStop").addEventListener("click", () => api("/sim/player/stop", { method: "POST" }).then(updateUI));
  $("btnPairs").addEventListener("click", () => openPairsModal());
  $("pairsSave").addEventListener("click", () => applyPairsSelection().catch((e) => log(e.message)));
  $("pairsSelectAll").addEventListener("click", () => {
    selectedSimPairs = new Set(allPoolPairs);
    renderPairsChecklist();
  });
  $("pairsSelectNone").addEventListener("click", () => {
    selectedSimPairs = new Set();
    renderPairsChecklist();
  });
  document.querySelectorAll("[data-close=pairs]").forEach((el) => {
    el.addEventListener("click", () => closePairsModal());
  });
  $("btnMenu").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleResetMenu();
  });
  $("resetMenu").querySelectorAll("button[data-reset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      toggleResetMenu(false);
      resetProgress(btn.dataset.reset).catch((e) => log(e.message));
    });
  });
  document.addEventListener("click", () => toggleResetMenu(false));
  $("resetMenu").addEventListener("click", (e) => e.stopPropagation());
  document.querySelectorAll(".btn.speed").forEach((b) => {
    b.addEventListener("click", () => setSpeed(Number(b.dataset.speed)));
  });

  async function loadScenarios() {
    try {
      const data = await api("/sim/scenarios");
      scenarioCatalog = data.scenarios || [];
      renderStrategyPanels();
      fillLiveStrategySelect();
    } catch (_) {
      scenarioCatalog = [];
    }
  }

  async function loadProfile() {
    try {
      playerProfile = await api("/sim/profile");
      updateStakeControls();
      updatePortfolioSummary();
    } catch (_) {
      /* defaults */
    }
  }

  initChart();
  setMode("sim");
  loadProfile().catch(() => {});
  loadMlGate().catch(() => {});
  loadMlPnl()
    .then(() => renderTradesList())
    .catch(() => {});
  loadScenarios().catch(() => {});
  loadPairs().catch((e) => log(e.message));
  api("/sim/bots/instances")
    .then((d) => {
      botInstances = d.instances || [];
      selectedBotId = d.selected_id;
      renderBotList();
      renderTradesList();
    })
    .catch(() => {});
  connectWs();
  setSpeed(2);
})();
