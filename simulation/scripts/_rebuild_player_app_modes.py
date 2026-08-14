#!/usr/bin/env python3
"""Rebuild app.js modes on top of clean original; validate brace/template balance."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(r"D:/cryptotools/simulation/player/app.js")
ORIG = Path(r"D:/cryptotools/simulation/results/_app_orig.js")

src = ORIG.read_text(encoding="utf-8")

# --- 1) state vars after mlGate ---
old = '''  let mlGate = { enabled: true, ready: false, model: "" };

  const PAIRS_STORAGE_KEY = "simPlayerSelectedPairs";
  const $ = (id) => document.getElementById(id);
'''
new = '''  let mlGate = { enabled: true, ready: false, model: "" };
  let uiMode = "sim"; // sim | chart | live
  let liveEnabledSnapshot = null;
  let liveFollowChart = true;

  const PAIRS_STORAGE_KEY = "simPlayerSelectedPairs";
  const MODE_HINTS = {
    sim: "Мультистратегия · Play / Прогон",
    chart: "Просмотр свечей пары за период",
    live: "Одна пара · одна стратегия · оранжевые сделки",
  };
  const $ = (id) => document.getElementById(id);
'''
assert old in src, "state insert anchor missing"
src = src.replace(old, new, 1)

# --- 2) replace log() with log + mode helpers ---
old = '''  function log(msg) {
    const box = $("logBox");
    const ts = new Date().toISOString().slice(11, 19);
    box.textContent = `[${ts}] ${msg}\\n` + box.textContent.slice(0, 3000);
  }
'''
# careful with the exact content from file
idx = src.find("  function log(msg) {")
assert idx > 0
end = src.find("  function msToLocalInput", idx)
assert end > idx
helpers = r'''  function log(msg) {
    const ts = new Date().toISOString().slice(11, 19);
    const line = `[${ts}] ${msg}\n`;
    for (const id of ["logBox", "liveLogBox"]) {
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
    if (!["sim", "chart", "live"].includes(mode)) return;
    const prev = uiMode;
    if (prev === "live" && mode !== "live") {
      restoreLiveScenarios().catch(() => {});
      liveFollowChart = false;
      const ph = $("livePlayhead");
      if (ph) ph.classList.add("hidden");
    }
    uiMode = mode;
    document.body.classList.remove("mode-sim", "mode-chart", "mode-live");
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
      liveFollowChart = true;
      renderLiveTradesPanel();
    }
    if (mode === "chart") {
      selectedTradeHighlight = null;
      if (candleSeries) candleSeries.setMarkers([]);
      const th = $("tradeHighlights");
      if (th) th.innerHTML = "";
      hidePriceLines();
    }
    drawHighlights();
    resizeChartForMode();
    log("Режим: " + (MODE_HINTS[mode] || mode));
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
      el.innerHTML = "<span class='muted'>Сделок пока нет — ждём сигнал стратегии</span>";
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
        return (
          '<div class="live-trade-row ' +
          (open ? "open" : "") +
          '">' +
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
  }

  function followLiveClock(currentMs) {
    if (uiMode !== "live" || !liveFollowChart || !chart || !currentMs || !loadedCandles.length) {
      return;
    }
    const t = Math.floor(currentMs / 1000);
    const windowSec = 240;
    try {
      chart.timeScale().setVisibleRange({ from: t - windowSec, to: t + 40 });
    } catch (_) {}
    const ph = $("livePlayhead");
    if (!ph) return;
    const x = chart.timeScale().timeToCoordinate(t);
    if (x == null) {
      ph.classList.add("hidden");
      return;
    }
    ph.classList.remove("hidden");
    ph.style.left = x + "px";
  }

  function collectLiveHighlight() {
    if (uiMode !== "live") return null;
    const pair = $("pairSelect").value;
    const sid = ($("liveStrategySelect") && $("liveStrategySelect").value) || activeReplayScenario;
    if (!sid || !pair) return null;
    const trades = liveTradesList(sid).filter((t) => t.pair === pair);
    if (!trades.length) return null;
    const now = lastCurrentMs || 0;
    return {
      bot_id: sid,
      label: sid,
      pair: pair,
      trades: trades.map((t) => {
        const open = t._closed === false;
        return Object.assign({}, t, {
          close_ms: open ? Math.max(now || t.open_ms, t.open_ms) : t.close_ms,
          _liveOpen: open,
        });
      }),
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

  async function livePlay() {
    const pair = $("pairSelect").value;
    const sid = $("liveStrategySelect") && $("liveStrategySelect").value;
    const start_ms = localInputToMs($("dateFrom").value);
    const end_ms = localInputToMs($("dateTo").value);
    if (!pair || !sid || !start_ms || !end_ms) {
      log("Live: выберите пару, стратегию и период (От / До)");
      return;
    }
    if (!loadedCandles.length) await loadChart();
    await snapshotAndEnableLiveScenario(sid);
    selectedSimPairs = new Set([pair]);
    updatePairsButtonLabel();
    resetBotsUi(false);
    liveTradesByScenario.delete(sid);
    setSimRunning(true);
    liveFollowChart = true;
    $("statusLabel").textContent = "live…";
    log("Live ▶ " + sid + " · " + pair + " · " + $("dateFrom").value + " → " + $("dateTo").value);
    try {
      const res = await api("/sim/player/play", {
        method: "POST",
        body: JSON.stringify({
          pair: pair,
          pairs: [pair],
          range_start_ms: start_ms,
          range_end_ms: end_ms,
          speed: currentSpeed,
        }),
      });
      updateUI(res);
      renderLiveTradesPanel();
    } catch (e) {
      log("Live ошибка: " + e.message);
      setSimRunning(false);
    }
  }

'''
src = src[:idx] + helpers + src[end:]

# --- 3) setSimRunning ---
old = '''  function setSimRunning(on) {
    $("btnPlay").disabled = on;
    $("btnPrgon").disabled = on;
  }
'''
new = '''  function setSimRunning(on) {
    $("btnPlay").disabled = on;
    $("btnPrgon").disabled = on;
    if ($("btnLivePlay")) $("btnLivePlay").disabled = on;
  }
'''
assert old in src
src = src.replace(old, new, 1)

# --- 4) syncLiveTradesFromTick tail ---
old = '''    liveTradesByScenario.set(sid, map);
    sequentialReplay = true;
    activeReplayScenario = sid;
    ensureStrategyCard(sid);
    refreshStrategyCard(sid);
    markReplayStrategyCards();
  }
'''
new = '''    liveTradesByScenario.set(sid, map);
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
'''
assert old in src
src = src.replace(old, new, 1)

# --- 5) onTradeLive tail ---
old = '''    if (msg.event === "open") {
      const tr = map.get(id);
      scoreOpenTrade(tr, sid).then(() => refreshStrategyCard(sid)).catch(() => {});
      log(`▸ ${msg.label} ${msg.pair.split("/")[0]} вход ${fmtTime(msg.trade.open_ms)}`);
    }
  }

  function refreshStrategyCard(scenarioId) {
'''
new = '''    if (msg.event === "open") {
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
'''
assert old in src
src = src.replace(old, new, 1)

# --- 6) finalizeSequentialReplay ---
old = '''    updatePortfolioSummary();
  }

  function liveTradesList(scenarioId) {
'''
# might match wrong — be more specific
old = '''    document.querySelectorAll(".strategy-block").forEach((block) => {
      block.classList.remove("replaying", "replay-done");
    });
    updatePortfolioSummary();
  }

  function liveTradesList(scenarioId) {
'''
new = '''    document.querySelectorAll(".strategy-block").forEach((block) => {
      block.classList.remove("replaying", "replay-done");
    });
    updatePortfolioSummary();
    if (uiMode === "live") {
      renderLiveTradesPanel();
      drawHighlights();
    }
  }

  function liveTradesList(scenarioId) {
'''
assert old in src
src = src.replace(old, new, 1)

# --- 7) updateUI current_ms ---
old = '''    if (snap.current_ms) {
      $("clockLabel").dataset.ms = snap.current_ms;
      lastCurrentMs = snap.current_ms;
    }
'''
new = '''    if (snap.current_ms) {
      $("clockLabel").dataset.ms = snap.current_ms;
      lastCurrentMs = snap.current_ms;
      followLiveClock(snap.current_ms);
      if (uiMode === "live") drawHighlights();
    }
'''
assert old in src
src = src.replace(old, new, 1)

# --- 8) drawHighlights ---
old_start = "  function drawHighlights() {"
idx = src.find(old_start)
assert idx > 0
end = src.find("\n  async function selectBot", idx)
assert end > idx
draw = r'''  function drawHighlights() {
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
      if (isLive) {
        div.className = "trade-zone " + (tr._liveOpen ? "live-open" : "live-closed");
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
'''
src = src[:idx] + draw + src[end:]

# --- 9) loadChart meta ---
old = '''    log(`График ${pair}: ${loadedCandles.length} свечей`);
    drawHighlights();
  }
'''
new = (
    "    log(`График ${pair}: ${loadedCandles.length} свечей`);\n"
    "    const meta = $(\"chartMeta\");\n"
    "    if (meta) {\n"
    "      meta.textContent =\n"
    "        pair +\n"
    '        "\\n" +\n'
    "        msToLocalInput(start_ms) +\n"
    '        " → " +\n'
    "        msToLocalInput(end_ms) +\n"
    '        "\\n" +\n'
    "        loadedCandles.length +\n"
    '        " свечей 1s";\n'
    "    }\n"
    "    drawHighlights();\n"
    "  }\n"
)
assert old in src
src = src.replace(old, new, 1)

# --- 10) setSpeed ---
old = '''  async function setSpeed(speed) {
    currentSpeed = speed;
    $("speedLabel").textContent = `${speed} sim-с/с`;
    document.querySelectorAll(".btn.speed").forEach((b) => {
      b.classList.toggle("active", Number(b.dataset.speed) === speed);
    });
    await api("/sim/player/speed", { method: "POST", body: JSON.stringify({ speed }) });
  }
'''
new = '''  async function setSpeed(speed) {
    currentSpeed = speed;
    const label = `${speed} sim-с/с`;
    if ($("speedLabel")) $("speedLabel").textContent = label;
    if ($("liveSpeedLabel")) $("liveSpeedLabel").textContent = label;
    document.querySelectorAll(".btn.speed").forEach((b) => {
      b.classList.toggle("active", Number(b.dataset.speed) === speed);
    });
    await api("/sim/player/speed", { method: "POST", body: JSON.stringify({ speed }) });
  }
'''
assert old in src
src = src.replace(old, new, 1)

# --- 11) loadScenarios ---
old = '''  async function loadScenarios() {
    try {
      const data = await api("/sim/scenarios");
      scenarioCatalog = data.scenarios || [];
      renderStrategyPanels();
    } catch (_) {
      scenarioCatalog = [];
    }
  }
'''
new = '''  async function loadScenarios() {
    try {
      const data = await api("/sim/scenarios");
      scenarioCatalog = data.scenarios || [];
      renderStrategyPanels();
      fillLiveStrategySelect();
    } catch (_) {
      scenarioCatalog = [];
    }
  }
'''
assert old in src
src = src.replace(old, new, 1)

# --- 12) event listeners + boot ---
old = '''  $("btnLoad")?.addEventListener("click", () => loadChart().catch((e) => log(e.message)));
'''
new = '''  $("btnLoad")?.addEventListener("click", () => loadChart().catch((e) => log(e.message)));
  $("btnChartOpen")?.addEventListener("click", () => loadChart().catch((e) => log(e.message)));
  $("btnLiveLoad")?.addEventListener("click", () => liveLoad().catch((e) => log(e.message)));
  $("btnLivePlay")?.addEventListener("click", () => livePlay().catch((e) => log(e.message)));
  $("btnLivePause")?.addEventListener("click", () =>
    api("/sim/player/pause", { method: "POST" }).then(updateUI)
  );
  $("btnLiveStop")?.addEventListener("click", () =>
    api("/sim/player/stop", { method: "POST" }).then((s) => {
      updateUI(s);
      setSimRunning(false);
      renderLiveTradesPanel();
      drawHighlights();
    })
  );
  $("liveStrategySelect")?.addEventListener("change", () => {
    renderLiveTradesPanel();
    drawHighlights();
  });
  document.querySelectorAll(".mode-tab").forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });
'''
assert old in src
src = src.replace(old, new, 1)

old = '''  initChart();
  loadProfile().catch(() => {});
'''
new = '''  initChart();
  setMode("sim");
  loadProfile().catch(() => {});
'''
assert old in src
src = src.replace(old, new, 1)

ROOT.write_text(src, encoding="utf-8", newline="\n")
print("Wrote", ROOT, "lines", len(src.splitlines()))
