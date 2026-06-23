const BOTS = {
  freqai: {
    prefix: "/api/freqai",
    label: "FreqAI",
    maxTrades: 3,
    stateEl: "freqai-state",
    statsEl: "freqai-stats",
    actionsEl: "freqai-actions",
    tradesEl: "freqai-trades",
    pairsEl: "freqai-pairs",
    maxTradesEl: "freqai-max-trades",
  },
  strategy: {
    prefix: "/api/strategy",
    label: "Стратегии",
    maxTrades: 2,
    stateEl: "strategy-state",
    statsEl: "strategy-stats",
    actionsEl: "strategy-actions",
    tradesEl: "strategy-trades",
    pairsEl: "strategy-pairs",
    maxTradesEl: "strategy-max-trades",
  },
  grid: {
    prefix: "/api/grid",
    label: "Grid (BB+ADX)",
    maxTrades: 2,
    stateEl: "grid-state",
    statsEl: "grid-stats",
    actionsEl: "grid-actions",
    tradesEl: "grid-trades",
    pairsEl: "grid-pairs",
    maxTradesEl: "grid-max-trades",
  },
};

const MAX_TRADES_LIMIT = 10;
const FREQAI_STATS_LABEL = "FreqAI (LightGBM)";
const GRID_STATS_LABEL = "Grid (BB+ADX)";
const SESSION_DAYS = 7;
const SESSION_USER_KEY = "ct_user";
const SESSION_PASS_KEY = "ct_pass";
const SESSION_UNTIL_KEY = "ct_session_until";

let strategyCatalog = [];
let enabledStrategies = {};

let tokens = { freqai: null, strategy: null, grid: null };
let creds = { user: "", pass: "" };

const $ = (id) => document.getElementById(id);

function formatApiError(message) {
  if (!message) return "Не удалось выполнить запрос";
  try {
    const data = JSON.parse(message);
    const err = data.error || data.detail || message;
    if (typeof err === "string") return humanizeApiError(err);
  } catch {
    /* plain text */
  }
  return humanizeApiError(message);
}

function humanizeApiError(err) {
  if (!err) return "Не удалось выполнить запрос";
  if (err.includes("Connection refused") || err.includes("[Errno 111]")) {
    return "Бот перезапускается — подождите несколько секунд и обновите страницу";
  }
  if (
    err.includes("position is zero") ||
    err.includes("110017") ||
    err.includes("Failed to exit trade")
  ) {
    return "На бирже позиции уже нет — сделка «зависла» в базе бота";
  }
  if (err.includes("trader is not running")) {
    return "Бот остановлен — нажмите «Старт» и повторите";
  }
  if (err === "Internal Server Error" || err.includes("InvalidOrderException")) {
    return "Биржа отклонила закрытие — попробуйте ещё раз (синхронизация с биржей)";
  }
  if (err.includes("Error querying /api/v1/forceexit:")) {
    return humanizeApiError(err.split(": ").slice(1).join(": "));
  }
  return err;
}

async function readApiErrorResponse(res) {
  const text = await res.text();
  if (!text) return res.statusText || "Ошибка запроса";
  try {
    const data = JSON.parse(text);
    return data.error || data.detail || data.message || text;
  } catch {
    return text;
  }
}

function isStaleTradeError(message) {
  const msg = String(message || "");
  return (
    msg.includes("position is zero") ||
    msg.includes("110017") ||
    msg.includes("Failed to exit") ||
    msg.includes("InvalidOrderException") ||
    msg === "Internal Server Error" ||
    /Error querying \/api\/v1\/forceexit:/i.test(msg)
  );
}

async function closeTrade(bot, tradeId) {
  const id = String(tradeId);
  try {
    return await api(bot, "/forceexit", "POST", { tradeid: id, ordertype: "market" });
  } catch (firstErr) {
    if (!isStaleTradeError(firstErr.message)) throw firstErr;
    try {
      await api(bot, `/trades/${id}/reload`, "POST");
    } catch {
      /* reload may fail if trade is already gone on exchange */
    }
    try {
      return await api(bot, "/forceexit", "POST", { tradeid: id, ordertype: "market" });
    } catch (secondErr) {
      if (!isStaleTradeError(secondErr.message)) throw secondErr;
      const ok = confirm(
        `Сделка #${id} не закрывается на бирже (позиция уже нулевая).\n\nУдалить её из базы бота? Это уберёт «зависшую» запись.`
      );
      if (!ok) {
        throw new Error("Сделка не закрыта — позиция на бирже отсутствует");
      }
      await api(bot, `/trades/${id}`, "DELETE");
      return { result: "deleted", trade_id: id };
    }
  }
}

function showReloadWarning(data) {
  if (!data?.reload_warning) return;
  const msg = formatApiError(JSON.stringify({ error: data.reload_warning }));
  const el = $("pair-msg");
  if (el) {
    el.textContent = msg;
    el.classList.remove("hidden");
  }
}

function authHeader(bot) {
  return tokens[bot] ? { Authorization: `Bearer ${tokens[bot]}` } : {};
}

function basicHeader() {
  return { Authorization: `Basic ${btoa(`${creds.user}:${creds.pass}`)}` };
}

function saveSession() {
  const until = Date.now() + SESSION_DAYS * 24 * 60 * 60 * 1000;
  localStorage.setItem(SESSION_USER_KEY, creds.user);
  localStorage.setItem(SESSION_PASS_KEY, creds.pass);
  localStorage.setItem(SESSION_UNTIL_KEY, String(until));
  sessionStorage.removeItem("ct_user");
  sessionStorage.removeItem("ct_pass");
}

function clearSession() {
  localStorage.removeItem(SESSION_USER_KEY);
  localStorage.removeItem(SESSION_PASS_KEY);
  localStorage.removeItem(SESSION_UNTIL_KEY);
  sessionStorage.removeItem("ct_user");
  sessionStorage.removeItem("ct_pass");
}

function loadStoredSession() {
  const until = Number(localStorage.getItem(SESSION_UNTIL_KEY) || 0);
  if (!until || Date.now() > until) {
    clearSession();
    return null;
  }
  const user = localStorage.getItem(SESSION_USER_KEY);
  const pass = localStorage.getItem(SESSION_PASS_KEY);
  if (!user || !pass) return null;
  return { user, pass, until };
}

function applyStoredCreds(session) {
  creds.user = session.user;
  creds.pass = session.pass;
  $("login-user").value = session.user;
}

async function reloginFromStorage() {
  const session = loadStoredSession();
  if (!session) throw new Error("auth");
  applyStoredCreds(session);
  tokens = { freqai: null, strategy: null, grid: null };
  await loginAll();
}

async function api(bot, path, method = "GET", body = null, retried = false) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json", ...authHeader(bot) },
  };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(`${BOTS[bot].prefix}${path}`, opts);
  if (res.status === 401 && !retried && loadStoredSession()) {
    await reloginFromStorage();
    return api(bot, path, method, body, true);
  }
  if (res.status === 401) throw new Error("auth");
  if (!res.ok) {
    const errText = await readApiErrorResponse(res);
    const err = new Error(errText || res.statusText);
    err.status = res.status;
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

async function pairConfigApi(path, method = "GET", body = null, retried = false) {
  const opts = { method, headers: { ...basicHeader() } };
  if (body) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`/api/pair-config${path}`, opts);
  if (res.status === 401 && !retried && loadStoredSession()) {
    await reloginFromStorage();
    return pairConfigApi(path, method, body, true);
  }
  if (res.status === 401) throw new Error("auth");
  if (!res.ok) {
    const t = await res.text();
    throw new Error(t || res.statusText);
  }
  return res.json();
}

async function loginBot(bot) {
  const res = await fetch(`${BOTS[bot].prefix}/token/login`, {
    method: "POST",
    headers: basicHeader(),
  });
  if (!res.ok) {
    const err = new Error(`${bot}_login_failed`);
    err.bot = bot;
    err.status = res.status;
    throw err;
  }
  const data = await res.json();
  tokens[bot] = data.access_token;
}

async function loginAll() {
  const errors = [];
  for (const bot of Object.keys(BOTS)) {
    try {
      await loginBot(bot);
    } catch (e) {
      errors.push(e);
    }
  }
  if (errors.length === Object.keys(BOTS).length) {
    const allAuth = errors.every((e) => e.status === 401);
    throw new Error(allAuth ? "auth" : errors[0].message || "Ошибка входа");
  }
  if (errors.length >= 1) {
    const e = errors[0];
    const label = BOTS[e.bot]?.label || e.bot;
    if (e.status === 401) throw new Error("auth");
    console.warn(`Бот «${label}» недоступен при входе`);
  }
  saveSession();
}

function logout() {
  closeLogs();
  tokens = { freqai: null, strategy: null, grid: null };
  clearSession();
  $("app-screen").classList.add("hidden");
  $("login-screen").classList.remove("hidden");
}

function fmtPctRatio(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return `${(Number(v) * 100).toFixed(2)}%`;
}

function fmtPct(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return `${Number(v).toFixed(2)}%`;
}

function fmtUsd(v, digits = 2) {
  if (v == null || Number.isNaN(v)) return "—";
  return `${Number(v).toFixed(digits)} USDT`;
}

function fmtRate(v) {
  if (v == null || Number.isNaN(v)) return "—";
  const n = Number(v);
  if (n >= 100) return n.toFixed(2);
  if (n >= 1) return n.toFixed(4);
  return n.toFixed(6);
}

function pnlClass(v) {
  if (v == null || Number.isNaN(v)) return "";
  return Number(v) >= 0 ? "pos" : "neg";
}

function getUsdtWallet(balance) {
  return balance?.currencies?.find((c) => c.currency === "USDT");
}

function fmtUsdSigned(v, digits = 2) {
  if (v == null || Number.isNaN(v)) return "—";
  const n = Number(v);
  const sign = n >= 0 ? "+" : "";
  return `${sign}${n.toFixed(digits)} USDT`;
}

function fmtShare(amount, total) {
  if (!total || !amount) return "";
  const pct = (Math.abs(amount) / Math.abs(total)) * 100;
  return ` (${pct.toFixed(0)}%)`;
}

function tradeSourceLabel(trade, bot) {
  if (bot === "freqai") return FREQAI_STATS_LABEL;
  if (bot === "grid") return GRID_STATS_LABEL;
  if (trade.enter_tag) return strategyLabel(trade.enter_tag);
  if (trade.strategy && trade.strategy !== "MultiStrategyRouter") {
    return strategyLabel(trade.strategy);
  }
  return "Без тега";
}

function exitReasonLabel(reason) {
  const map = {
    roi: "ROI",
    stop_loss: "Стоп",
    trailing_stop_loss: "Трейлинг",
    exit_signal: "Сигнал",
    force_exit: "Принудительно",
    emergency_exit: "Аварийный",
    sold_on_exchange: "На бирже",
    custom_exit: "Кастом",
  };
  return map[reason] || reason || "—";
}

function fmtTradeDate(trade) {
  const raw = trade.close_date || trade.close_fill_date;
  if (!raw) return "—";
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return raw.slice(0, 16);
  return d.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function closedTradePnl(trade) {
  const abs = Number(trade.close_profit_abs ?? trade.profit_abs ?? trade.realized_profit ?? 0);
  const pct = Number(
    trade.close_profit_pct ?? trade.profit_pct ?? trade.realized_profit_ratio ?? 0
  );
  const pctDisplay = Math.abs(pct) <= 1 && Math.abs(pct) > 0 ? pct * 100 : pct;
  return { abs, pct: pctDisplay };
}

function buildHistoryEntries(freqaiTrades, stratTrades, gridTrades) {
  const entries = [];
  for (const t of freqaiTrades || []) {
    if (t.is_open) continue;
    entries.push({ bot: "freqai", trade: t });
  }
  for (const t of stratTrades || []) {
    if (t.is_open) continue;
    entries.push({ bot: "strategy", trade: t });
  }
  for (const t of gridTrades || []) {
    if (t.is_open) continue;
    entries.push({ bot: "grid", trade: t });
  }
  entries.sort(
    (a, b) =>
      (b.trade.close_timestamp || b.trade.open_timestamp || 0) -
      (a.trade.close_timestamp || a.trade.open_timestamp || 0)
  );
  return entries;
}

function renderStatsHistory(entries) {
  const bodyEl = $("stats-history-body");
  if (!bodyEl) return;

  if (!entries.length) {
    bodyEl.innerHTML =
      '<tr class="stats-history-empty"><td colspan="7">Нет закрытых сделок</td></tr>';
    return;
  }

  bodyEl.innerHTML = entries
    .map(({ bot, trade: t }) => {
      const { abs, pct } = closedTradePnl(t);
      const botLabel = BOTS[bot]?.label || bot;
      const side = t.is_short ? "SHORT" : "LONG";
      const source = tradeSourceLabel(t, bot);
      const pnlClassName = pnlClass(abs);
      return `<tr>
        <td>${fmtTradeDate(t)}</td>
        <td>${botLabel}</td>
        <td>${t.pair}</td>
        <td>${side}</td>
        <td>${source}</td>
        <td class="${pnlClassName}">${fmtUsdSigned(abs, 2)}<br><span class="muted" style="font-size:0.75rem">${fmtPct(pct)}</span></td>
        <td>${exitReasonLabel(t.exit_reason)}</td>
      </tr>`;
    })
    .join("");
}

function setStatsHistoryOpen(open) {
  const panel = $("stats-history");
  const btn = $("stats-history-toggle");
  if (!panel || !btn) return;
  panel.classList.toggle("hidden", !open);
  panel.setAttribute("aria-hidden", open ? "false" : "true");
  btn.setAttribute("aria-expanded", open ? "true" : "false");
}

function toggleStatsHistory() {
  const panel = $("stats-history");
  if (!panel) return;
  setStatsHistoryOpen(panel.classList.contains("hidden"));
}

function addClosedTradeToRow(row, trade) {
  const p = Number(trade.close_profit_abs ?? trade.profit_abs ?? 0);
  if (p > 0) {
    row.wins += 1;
    row.profit += p;
  } else if (p < 0) {
    row.losses += 1;
    row.loss += Math.abs(p);
  }
}

function buildStatsRows(freqaiTrades, stratTrades, gridTrades, catalog) {
  const rows = new Map();

  const ensure = (name) => {
    if (!rows.has(name)) {
      rows.set(name, { name, wins: 0, losses: 0, profit: 0, loss: 0 });
    }
    return rows.get(name);
  };

  ensure(FREQAI_STATS_LABEL);
  ensure(GRID_STATS_LABEL);
  for (const s of catalog) ensure(s.name);

  for (const t of freqaiTrades) addClosedTradeToRow(ensure(FREQAI_STATS_LABEL), t);
  for (const t of gridTrades) addClosedTradeToRow(ensure(GRID_STATS_LABEL), t);
  for (const t of stratTrades) {
    const name = tradeSourceLabel(t, "strategy");
    addClosedTradeToRow(ensure(name), t);
  }

  const ordered = [FREQAI_STATS_LABEL, GRID_STATS_LABEL, ...catalog.map((s) => s.name)];
  const result = [];
  for (const name of ordered) {
    if (rows.has(name)) result.push(rows.get(name));
  }
  for (const row of rows.values()) {
    if (!result.includes(row)) result.push(row);
  }
  return result;
}

async function loadStatistics() {
  const summaryEl = $("stats-summary");
  const bodyEl = $("stats-table-body");
  const footEl = $("stats-footnote");
  if (!bodyEl) return;

  summaryEl.innerHTML = '<p class="muted">Загрузка…</p>';
  bodyEl.innerHTML = "";

  try {
    const [freqaiTrades, stratTrades, gridTrades, freqaiProfit, stratProfit, gridProfit, catalogData] =
      await Promise.all([
      api("freqai", "/trades?limit=500"),
      api("strategy", "/trades?limit=500"),
      api("grid", "/trades?limit=500").catch(() => ({ trades: [] })),
      api("freqai", "/profit"),
      api("strategy", "/profit"),
      api("grid", "/profit").catch(() => ({})),
      pairConfigApi("/strategies").catch(() => ({ strategies: strategyCatalog })),
    ]);

    const catalog = catalogData.strategies || strategyCatalog;
    const rows = buildStatsRows(
      freqaiTrades?.trades || [],
      stratTrades?.trades || [],
      gridTrades?.trades || [],
      catalog
    );

    let totalProfit = 0;
    let totalLoss = 0;
    let totalWins = 0;
    let totalLosses = 0;
    for (const r of rows) {
      totalProfit += r.profit;
      totalLoss += r.loss;
      totalWins += r.wins;
      totalLosses += r.losses;
    }

    const net =
      (freqaiProfit?.profit_closed_coin ?? 0) +
      (stratProfit?.profit_closed_coin ?? 0) +
      (gridProfit?.profit_closed_coin ?? 0);
    const closedCount =
      (freqaiProfit?.closed_trade_count ?? 0) +
      (stratProfit?.closed_trade_count ?? 0) +
      (gridProfit?.closed_trade_count ?? 0);

    summaryEl.innerHTML = `
      <div class="stats-kpi">
        <span>Общая прибыль</span>
        <strong class="pos">${fmtUsd(totalProfit)}</strong>
      </div>
      <div class="stats-kpi">
        <span>Общие потери</span>
        <strong class="neg">${fmtUsd(totalLoss)}</strong>
      </div>
      <div class="stats-kpi">
        <span>Итого (закрытые)</span>
        <strong class="${pnlClass(net)}">${fmtUsdSigned(net)}</strong>
      </div>
      <div class="stats-kpi">
        <span>Успешные / неуспешные</span>
        <strong>${totalWins} / ${totalLosses}</strong>
      </div>
    `;

    bodyEl.innerHTML = rows
      .map((r) => {
        const profitCell =
          r.profit > 0
            ? `<span class="pos">${fmtUsd(r.profit)}${fmtShare(r.profit, totalProfit)}</span>`
            : `<span class="muted">—</span>`;
        const lossCell =
          r.loss > 0
            ? `<span class="neg">${fmtUsd(r.loss)}${fmtShare(r.loss, totalLoss)}</span>`
            : `<span class="muted">—</span>`;
        const hasActivity = r.wins || r.losses;
        return `<tr class="${hasActivity ? "" : "stats-row-idle"}">
          <td>${r.name}</td>
          <td>${r.wins || "—"}</td>
          <td>${r.losses || "—"}</td>
          <td>${profitCell}</td>
          <td>${lossCell}</td>
        </tr>`;
      })
      .join("");

    footEl.textContent = `Всего закрытых сделок в базах ботов: ${closedCount}.`;
    renderStatsHistory(
      buildHistoryEntries(
        freqaiTrades?.trades || [],
        stratTrades?.trades || [],
        gridTrades?.trades || []
      )
    );
    setStatsHistoryOpen(false);
  } catch (e) {
    if (e.message === "auth") logout();
    else {
      summaryEl.innerHTML = `<p class="error">${e.message}</p>`;
      footEl.textContent = "";
    }
  }
}

function openStats() {
  $("stats-modal").classList.remove("hidden");
  $("stats-modal").setAttribute("aria-hidden", "false");
  loadStatistics();
}

function closeStats() {
  $("stats-modal").classList.add("hidden");
  $("stats-modal").setAttribute("aria-hidden", "true");
  setStatsHistoryOpen(false);
}

function strategyLabel(id) {
  const fromCatalog = strategyCatalog.find((s) => s.id === id);
  return fromCatalog?.name || id || "—";
}

function syncEnabledFromPayload(data) {
  strategyCatalog = data.strategies || strategyCatalog;
  enabledStrategies = {};
  const enabled = data.enabled || {};
  for (const s of strategyCatalog) {
    enabledStrategies[s.id] = !!enabled[s.id];
  }
}

function updateStrategyDisplay() {
  const el = $("strategy-active-name");
  const hint = $("strategy-enabled-hint");
  const panelSummary = $("strategy-panel-summary");
  const active = strategyCatalog.filter((s) => enabledStrategies[s.id]);

  if (el) {
    el.classList.remove("warn");
    if (!active.length) {
      el.textContent = "Нет активных стратегий";
      el.classList.add("warn");
    } else {
      el.textContent =
        active.length === 1
          ? active[0].name
          : `${active.length} активны: ${active.map((s) => s.name).join(", ")}`;
    }
  }

  if (hint) {
    if (!active.length) {
      hint.textContent = "Включите хотя бы одну стратегию";
    } else {
      hint.textContent = `${active.length} из ${strategyCatalog.length} включено · сигнал от любой из них`;
    }
  }

  if (panelSummary) {
    if (!active.length) {
      panelSummary.textContent = "нет активных · развернуть";
    } else if (active.length === 1) {
      panelSummary.textContent = `${active[0].name} · изменить`;
    } else {
      panelSummary.textContent = `${active.length} из ${strategyCatalog.length} включено · изменить`;
    }
  }
}

function bindStrategyPanelCollapse() {
  const section = $("strategy-panel-section");
  const btn = $("strategy-panel-toggle");
  if (!section || !btn || btn.dataset.bound) return;
  btn.dataset.bound = "1";
  btn.addEventListener("click", () => {
    const expanded = section.classList.toggle("is-expanded");
    section.classList.toggle("is-collapsed", !expanded);
    btn.setAttribute("aria-expanded", expanded ? "true" : "false");
  });
}

function renderStrategyTogglesInto(container) {
  if (!container) return;
  container.innerHTML = strategyCatalog
    .map(
      (s) => `
    <li class="strategy-toggle-item">
      <label class="strategy-toggle-label">
        <input type="checkbox" data-strategy="${s.id}" ${enabledStrategies[s.id] ? "checked" : ""} />
        <span class="strategy-toggle-text">
          <strong>${s.name}</strong>
          <span class="muted strategy-toggle-desc">${s.desc}</span>
        </span>
      </label>
    </li>`
    )
    .join("");

  container.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener("change", async () => {
      const id = cb.dataset.strategy;
      const next = cb.checked;
      const activeCount = Object.values(enabledStrategies).filter(Boolean).length;
      if (!next && activeCount <= 1 && enabledStrategies[id]) {
        cb.checked = true;
        alert("Должна остаться включённой хотя бы одна стратегия");
        return;
      }
      document.querySelectorAll(`input[data-strategy="${id}"]`).forEach((el) => {
        el.disabled = true;
      });
      try {
        const data = await pairConfigApi("/pairs", "POST", {
          action: "toggle_strategy",
          strategy: id,
          enabled: next,
        });
        syncEnabledFromPayload(data);
        updateStrategyDisplay();
        renderStrategyToggles();
        const msg = `${strategyLabel(id)} ${next ? "включена" : "выключена"}`;
        if (!$("settings-modal").classList.contains("hidden")) showPairMsg(msg);
        await refreshAll();
      } catch (e) {
        document.querySelectorAll(`input[data-strategy="${id}"]`).forEach((el) => {
          el.checked = !next;
        });
        if (e.message === "auth") logout();
        else alert(formatApiError(e.message));
      } finally {
        document.querySelectorAll(`input[data-strategy="${id}"]`).forEach((el) => {
          el.disabled = false;
        });
      }
    });
  });
}

function renderStrategyToggles() {
  renderStrategyTogglesInto($("strategy-panel-toggles"));
  renderStrategyTogglesInto($("strategy-toggle-list"));
}

async function refreshStrategyEnabled() {
  try {
    const data = await pairConfigApi("/strategies");
    syncEnabledFromPayload(data);
    updateStrategyDisplay();
    renderStrategyToggles();
  } catch (e) {
    if (e.message === "auth") logout();
  }
}

async function loadStrategySettings() {
  const data = await pairConfigApi("/strategies");
  syncEnabledFromPayload(data);
  updateStrategyDisplay();
  renderStrategyToggles();
}

function updateMaxTradesHint() {
  const el = $("max-trades-hint");
  if (!el) return;
  const total = Object.values(BOTS).reduce((s, b) => s + b.maxTrades, 0);
  el.textContent = `До ${total} сделок: FreqAI — ${BOTS.freqai.maxTrades}, стратегии — ${BOTS.strategy.maxTrades}, Grid — ${BOTS.grid.maxTrades}`;
}

async function setMaxTrades(bot, value) {
  const next = Number(value);
  if (!Number.isFinite(next) || next < 1 || next > MAX_TRADES_LIMIT) return;
  const data = await pairConfigApi("/pairs", "POST", {
    action: "set_max_trades",
    bot,
    max_open_trades: next,
  });
  BOTS[bot].maxTrades = next;
  updateMaxTradesHint();
  showReloadWarning(data);
  return data;
}

function bindMaxTradesStepper(container, bot) {
  if (!container) return;
  container.querySelectorAll("[data-delta]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const delta = Number(btn.dataset.delta);
      const next = BOTS[bot].maxTrades + delta;
      if (next < 1 || next > MAX_TRADES_LIMIT) return;
      btn.disabled = true;
      try {
        await setMaxTrades(bot, next);
        await refreshAll();
        if (!$("settings-modal").classList.contains("hidden")) {
          await loadPairSettings();
        }
      } catch (e) {
        if (e.message === "auth") logout();
        else alert(formatApiError(e.message));
      } finally {
        btn.disabled = false;
      }
    });
  });
}

function renderMaxTradesStepper(bot) {
  const maxTrades = BOTS[bot].maxTrades;
  return `
    <div class="stepper" data-bot="${bot}">
      <button type="button" class="btn btn-sm stepper-btn" data-delta="-1" aria-label="Меньше сделок" ${maxTrades <= 1 ? "disabled" : ""}>−</button>
      <span class="stepper-val" title="Максимум одновременных сделок">${maxTrades}</span>
      <button type="button" class="btn btn-sm stepper-btn" data-delta="1" aria-label="Больше сделок" ${maxTrades >= MAX_TRADES_LIMIT ? "disabled" : ""}>+</button>
    </div>
  `;
}

function renderStats(bot, profit, balance, openCount, stakeAmount) {
  const cfg = BOTS[bot];
  const profitClosed = profit?.profit_closed_coin ?? profit?.profit_closed_percent;
  const usdt = getUsdtWallet(balance);
  const botAvail = usdt?.bot_owned;
  const walletFree = usdt?.free;
  const walletTotal = usdt?.balance;
  const inMargin = usdt?.used;
  const el = $(cfg.statsEl);
  el.innerHTML = `
    <div class="stat stat-trades-limit">
      Открыто сделок<strong>${openCount} / ${cfg.maxTrades}</strong>
      ${renderMaxTradesStepper(bot)}
    </div>
    <div class="stat">Прибыль (закрытые)<strong>${profit?.profit_closed_coin != null ? fmtUsd(profit.profit_closed_coin) : fmtPctRatio(profitClosed)}</strong></div>
    <div class="stat">Stake<strong>${fmtUsd(stakeAmount ?? 5)}</strong></div>
    <div class="stat" title="Сколько USDT этот бот может использовать для новой сделки (учитываются только его сделки в БД)">
      Доступно боту<strong>${botAvail != null ? fmtUsd(botAvail) : "—"}</strong>
      <span class="stat-hint">общий кошелёк ${walletFree != null ? fmtUsd(walletFree) : "—"} · в марже ${inMargin != null ? fmtUsd(inMargin) : "—"}</span>
    </div>
  `;
  bindMaxTradesStepper(el.querySelector(".stepper"), bot);
}

function tradeDetailRows(t) {
  const direction = t.is_short ? "SHORT" : "LONG";
  const lev = t.leverage || 1;
  const notional = t.amount != null && t.current_rate != null ? t.amount * t.current_rate : null;
  const openOrders = (t.orders || []).filter((o) => o.is_open);
  const openOrderText = openOrders.length
    ? openOrders.map((o) => `${o.ft_order_side} ${o.order_type}`).join(", ")
    : null;

  return [
    ["Пара", t.pair],
    ["№ сделки", `#${t.trade_id}`],
    ["Направление", `${direction} · ${lev}x`],
    ["Стратегия", t.strategy || "—"],
    ["Тег входа", t.enter_tag || "—"],
    ["Открыта", t.open_date || "—"],
    ["Цена входа", fmtRate(t.open_rate)],
    ["Текущая цена", fmtRate(t.current_rate)],
    ["Объём", `${t.amount ?? "—"} (${fmtUsd(t.stake_amount)})`],
    ["Номинал (сейчас)", notional != null ? fmtUsd(notional) : "—"],
    ["Вложено (маржа)", fmtUsd(t.max_stake_amount ?? t.stake_amount)],
    ["Нереализ. PnL", `<span class="${pnlClass(t.profit_abs)}">${fmtPct(t.profit_pct)} (${fmtUsd(t.profit_abs, 4)})</span>`],
    ["Общий PnL", `<span class="${pnlClass(t.total_profit_abs)}">${fmtPctRatio(t.total_profit_ratio)} (${fmtUsd(t.total_profit_abs, 4)})</span>`],
    ["Комиссия funding", t.funding_fees != null ? fmtUsd(t.funding_fees, 4) : "—"],
    ["Стоп-лосс", t.stop_loss_abs != null ? `${fmtRate(t.stop_loss_abs)} (${fmtPctRatio(t.stop_loss_ratio)})` : "—"],
    ["До стопа", t.stoploss_current_dist != null ? `${fmtRate(t.stoploss_current_dist)} (${fmtPctRatio(t.stoploss_current_dist_ratio)})` : "—"],
    ["Ликвидация", t.liquidation_price != null ? fmtRate(t.liquidation_price) : "—"],
    ["Min / Max", `${fmtRate(t.min_rate)} / ${fmtRate(t.max_rate)}`],
    ["Открытый ордер", openOrderText || "—"],
  ];
}

function renderTrades(bot, trades) {
  const cfg = BOTS[bot];
  const open = trades || [];
  const el = $(cfg.tradesEl);
  if (!open.length) {
    el.innerHTML = '<p class="empty">Нет открытых сделок</p>';
    return;
  }

  el.innerHTML = open
    .map((t) => {
      const rows = tradeDetailRows(t)
        .map(
          ([label, value]) =>
            `<div class="trade-field"><span>${label}</span><strong>${value}</strong></div>`
        )
        .join("");
      const pnl = Number(t.profit_pct || 0);
      const tag = tradeSourceLabel(t, bot);
      return `<article class="trade-card is-collapsed" data-trade-id="${t.trade_id}">
        <div class="trade-summary">
          <button type="button" class="trade-toggle" aria-expanded="false" aria-label="Развернуть сделку">
            <div class="trade-summary-main">
              <strong class="trade-pair">${t.pair}</strong>
              <span class="trade-meta">${t.is_short ? "SHORT" : "LONG"} · ${t.leverage || 1}x · #${t.trade_id}</span>
              <span class="trade-meta">${tag} · маржа ${fmtUsd(t.stake_amount)}</span>
            </div>
            <div class="trade-head-right">
              <span class="trade-pnl ${pnlClass(t.profit_abs)}">${fmtPct(pnl)}</span>
              <span class="trade-rate">${fmtUsd(t.profit_abs, 4)}</span>
            </div>
            <span class="trade-chevron" aria-hidden="true">▸</span>
          </button>
          <button type="button" class="btn danger btn-sm" data-bot="${bot}" data-id="${t.trade_id}" data-act="forceexit">Закрыть</button>
        </div>
        <div class="trade-details">
          <div class="trade-grid">${rows}</div>
        </div>
      </article>`;
    })
    .join("");

}

function bindTradeLists() {
  for (const bot of Object.keys(BOTS)) {
    const el = $(BOTS[bot].tradesEl);
    if (!el || el.dataset.actionsBound) continue;
    el.dataset.actionsBound = "1";

    el.addEventListener("click", async (ev) => {
      const toggle = ev.target.closest(".trade-toggle");
      if (toggle) {
        const card = toggle.closest(".trade-card");
        if (!card) return;
        const expanded = card.classList.toggle("is-expanded");
        card.classList.toggle("is-collapsed", !expanded);
        toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
        toggle.setAttribute("aria-label", expanded ? "Свернуть сделку" : "Развернуть сделку");
        return;
      }

      const btn = ev.target.closest("[data-act=forceexit]");
      if (!btn || btn.disabled) return;
      ev.preventDefault();
      ev.stopPropagation();

      const tradeBot = btn.dataset.bot;
      const tradeId = btn.dataset.id;
      if (!tradeBot || !tradeId) return;
      if (!confirm(`Закрыть сделку #${tradeId}?`)) return;

      const label = btn.textContent;
      btn.disabled = true;
      btn.textContent = "…";
      try {
        const result = await closeTrade(tradeBot, tradeId);
        if (result?.result === "deleted") {
          alert(`Сделка #${tradeId} удалена из базы бота (на бирже уже была закрыта).`);
        }
        await refreshAll();
      } catch (e) {
        if (e.message === "auth") logout();
        else alert(formatApiError(e.message));
      } finally {
        btn.disabled = false;
        btn.textContent = label;
      }
    });
  }
}

function renderActions(bot, running) {
  const cfg = BOTS[bot];
  const scanBtn =
    bot === "grid"
      ? `<button class="btn" data-bot="grid" data-act="scan-ranging" title="Поиск боковика среди 150 ликвидных пар">Скан боковика</button>`
      : bot === "strategy"
        ? `<button class="btn" data-bot="strategy" data-act="scan-strategy" title="Подбор пар под каждую включённую стратегию (150 ликвидных)">Скан пар</button>`
        : "";
  $(cfg.actionsEl).innerHTML = `
    <button class="btn primary" data-bot="${bot}" data-act="start" ${running ? "disabled" : ""}>Старт</button>
    <button class="btn" data-bot="${bot}" data-act="stop" ${!running ? "disabled" : ""}>Стоп</button>
    <button class="btn" data-bot="${bot}" data-act="reload">Reload config</button>
    ${scanBtn}
  `;
  $(cfg.actionsEl).querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const act = btn.dataset.act;
      const label = btn.textContent;
      try {
        if (act === "start") await api(bot, "/start", "POST");
        if (act === "stop") {
          if (!confirm(`Остановить бота «${BOTS[bot].label}»? Новые сделки не будут открываться.`)) return;
          await api(bot, "/stop", "POST");
        }
        if (act === "reload") await api(bot, "/reload_config", "POST");
        if (act === "scan-ranging") {
          if (!confirm("Запустить скан боковика по 150 ликвидным парам? Займёт ~2–3 мин.")) return;
          btn.disabled = true;
          btn.textContent = "Скан…";
          const info = $("grid-scan-info");
          if (info) info.textContent = "Скан выполняется…";
          try {
            await pairConfigApi("/ranging-scan", "POST", {});
            await refreshBot("grid");
          } finally {
            btn.disabled = false;
            btn.textContent = label;
          }
        } else if (act === "scan-strategy") {
          if (!confirm("Запустить скан пар по каждой включённой стратегии (150 ликвидных)? Займёт ~2–3 мин.")) return;
          btn.disabled = true;
          btn.textContent = "Скан…";
          const info = $("strategy-scan-info");
          if (info) info.textContent = "Скан выполняется…";
          try {
            await pairConfigApi("/strategy-scan", "POST", {});
            await refreshBot("strategy");
          } finally {
            btn.disabled = false;
            btn.textContent = label;
          }
        } else {
          refreshAll();
          return;
        }
        await refreshAll();
        if (bot === "grid") await loadGridScanInfo();
        if (bot === "strategy") await loadStrategyScanInfo();
      } catch (e) {
        if (e.message === "auth") logout();
        else alert(formatApiError(e.message));
        if (bot === "grid") await loadGridScanInfo();
        if (bot === "strategy") await loadStrategyScanInfo();
      }
    });
  });
}

async function loadGridScanInfo() {
  const el = $("grid-scan-info");
  if (!el) return;
  try {
    const d = await pairConfigApi("/ranging-scan");
    if (d.running) {
      el.textContent = "Скан выполняется…";
      return;
    }
    if (!d.scanned_at) {
      el.textContent = "Скан боковика ещё не запускался";
      return;
    }
    const t = new Date(d.scanned_at).toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
    const n = d.selected_count ?? d.ranging_found ?? 0;
    const checked = d.candidates_checked ?? "?";
    el.textContent = `Боковик: ${n} пар (из ${checked} проверенных) · ${t}`;
  } catch {
    el.textContent = "Скан боковика: нет данных";
  }
}

const STRATEGY_SCAN_SHORT = {
  CriptoPairsStrategy: "RSI+EMA",
  SupertrendStrategy: "Supertrend",
  MacdEmaStrategy: "MACD",
  TripleEmaStrategy: "3×EMA",
  BollingerRsiStrategy: "BB+RSI",
  AdxMomentumStrategy: "ADX",
};

async function loadStrategyScanInfo() {
  const el = $("strategy-scan-info");
  if (!el) return;
  try {
    const d = await pairConfigApi("/strategy-scan");
    if (d.running) {
      el.textContent = "Скан выполняется…";
      return;
    }
    if (!d.scanned_at) {
      el.textContent = "Скан пар ещё не запускался";
      return;
    }
    const t = new Date(d.scanned_at).toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
    const n = d.selected_count ?? d.suitable_found ?? 0;
    const checked = d.candidates_checked ?? "?";
    const by = d.by_strategy || {};
    const parts = (d.enabled_strategies || [])
      .map((sid) => {
        const short = STRATEGY_SCAN_SHORT[sid] || sid;
        const cnt = by[sid]?.selected?.length ?? 0;
        return `${short}: ${cnt}`;
      })
      .filter(Boolean);
    const breakdown = parts.length ? ` · ${parts.join(", ")}` : "";
    el.textContent = `Подходящих: ${n} пар (из ${checked})${breakdown} · ${t}`;
  } catch {
    el.textContent = "Скан пар: нет данных";
  }
}

function setState(bot, running) {
  const el = $(BOTS[bot].stateEl);
  el.textContent = running ? "RUNNING" : "STOPPED";
  el.className = `badge ${running ? "running" : "stopped"}`;
  el.title = running ? "Бот торгует" : "Бот остановлен — нажмите «Старт»";
}

async function refreshBot(bot) {
  const [config, profit, balance, status, whitelist] = await Promise.all([
    api(bot, "/show_config"),
    api(bot, "/profit"),
    api(bot, "/balance"),
    api(bot, "/status"),
    api(bot, "/whitelist"),
  ]);
  const running = String(config?.state || "").toLowerCase() === "running";
  const openTrades = Array.isArray(status) ? status : [];
  const maxFromConfig = Number(config?.max_open_trades);
  if (Number.isFinite(maxFromConfig) && maxFromConfig > 0) {
    BOTS[bot].maxTrades = Math.round(maxFromConfig);
  }
  let openCount = openTrades.length;
  if (running) {
    try {
      const count = await api(bot, "/count");
      openCount = count?.current ?? openTrades.length;
    } catch {
      openCount = openTrades.length;
    }
  }
  setState(bot, running);
  renderStats(bot, profit, balance, openCount, config?.stake_amount);
  renderActions(bot, running);
  renderTrades(bot, openTrades);
  $(BOTS[bot].pairsEl).textContent = (whitelist?.whitelist || []).join(", ") || "—";
  updateMaxTradesHint();
  return openTrades;
}

function renderTradesSummary(allTrades) {
  const el = $("trades-summary");
  if (!el) return;

  const maxTotal = Object.values(BOTS).reduce((s, b) => s + b.maxTrades, 0);
  const count = allTrades.length;

  if (!count) {
    el.innerHTML = `<span class="ts-label">Открытые сделки</span><strong>нет · 0 / ${maxTotal}</strong>`;
    return;
  }

  const margin = allTrades.reduce((s, t) => s + Number(t.stake_amount || 0), 0);
  const pnl = allTrades.reduce(
    (s, t) => s + Number(t.total_profit_abs ?? t.profit_abs ?? 0),
    0
  );
  const pnlClassName = pnlClass(pnl);

  el.innerHTML = `
    <span class="ts-label">Всего по сделкам</span>
    <strong>${count} / ${maxTotal}</strong>
    <span class="ts-row">Маржа <b>${fmtUsd(margin)}</b></span>
    <span class="ts-row">PnL <b class="${pnlClassName}">${pnl >= 0 ? "+" : ""}${Number(pnl).toFixed(2)} USDT</b></span>
  `;
}

function fmtUptime(seconds) {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}д ${h}ч`;
  if (h > 0) return `${h}ч ${m}м`;
  return `${m}м`;
}

function renderServerStatus(stats) {
  const el = $("server-status");
  if (!el || !stats) return;

  const cpu =
    stats.cpu_percent != null
      ? `${stats.cpu_percent}%`
      : `load ${Number(stats.load_1m).toFixed(1)}`;
  const ram = `${stats.memory?.used_pct ?? "—"}%`;
  const disk = `${stats.disk?.used_pct ?? "—"}%`;

  el.textContent = `CPU ${cpu} · RAM ${ram} · Диск ${disk}`;
  el.className = `server-status status-${stats.load_level || "ok"}`;

  const mem = stats.memory || {};
  const dsk = stats.disk || {};
  el.title = [
    `Load: ${stats.load_1m} / ${stats.load_5m} / ${stats.load_15m} (${stats.cpus} CPU)`,
    `RAM: ${mem.used_mb ?? "?"} / ${mem.total_mb ?? "?"} MB (свободно ${mem.available_mb ?? "?"} MB)`,
    mem.swap_used_mb ? `Swap: ${mem.swap_used_mb} MB` : null,
    `Диск: ${dsk.used_gb ?? "?"} / ${dsk.total_gb ?? "?"} GB`,
    stats.uptime_seconds ? `Uptime: ${fmtUptime(stats.uptime_seconds)}` : null,
  ]
    .filter(Boolean)
    .join("\n");
}

async function refreshServerStats() {
  try {
    const stats = await pairConfigApi("/system");
    renderServerStatus(stats);
  } catch (e) {
    if (e.message === "auth") throw e;
    const el = $("server-status");
    if (el) {
      el.textContent = "Сервер —";
      el.className = "server-status muted";
    }
  }
}

async function refreshBotSafe(bot, attempt = 0) {
  try {
    return await refreshBot(bot);
  } catch (e) {
    if (e.message === "auth") throw e;
    if (attempt < 2) {
      await new Promise((r) => setTimeout(r, 4000));
      return refreshBotSafe(bot, attempt + 1);
    }
    const cfg = BOTS[bot];
    const el = $(cfg.stateEl);
    if (el) {
      el.textContent = "OFFLINE";
      el.className = "badge stopped";
      el.title = "Бот недоступен — перезапуск или ошибка API";
    }
    $(cfg.statsEl).innerHTML = '<p class="muted">Нет связи с ботом</p>';
    $(cfg.actionsEl).innerHTML = "";
    $(cfg.tradesEl).innerHTML = '<p class="muted">—</p>';
    $(cfg.pairsEl).textContent = "—";
    console.warn(`refresh ${bot}:`, e.message);
    return [];
  }
}

async function refreshAll() {
  try {
    await refreshStrategyEnabled();
    const results = await Promise.all([
      refreshBotSafe("freqai"),
      refreshBotSafe("strategy"),
      refreshBotSafe("grid"),
      refreshServerStats(),
      loadGridScanInfo(),
      loadStrategyScanInfo(),
    ]);
    const allTrades = results.slice(0, 3).flat();
    renderTradesSummary(allTrades);
    $("last-update").textContent = `Обновлено: ${new Date().toLocaleTimeString("ru-RU")}`;
  } catch (e) {
    if (e.message === "auth") logout();
    else console.error(e);
  }
}

function showPairMsg(text, isError = false) {
  const el = $("pair-msg");
  el.textContent = text;
  el.className = `pair-msg ${isError ? "error" : "ok"}`;
  el.classList.remove("hidden");
}

async function loadPairSettings() {
  await loadStrategySettings();
  const data = await pairConfigApi("/pairs");
  const freqai = data.freqai || {};
  const strategy = data.strategy || {};
  const grid = data.grid || {};
  const active = freqai.active_whitelist || [];
  const black = [
    ...new Set([
      ...(freqai.blacklist || []),
      ...(strategy.blacklist || []),
      ...(grid.blacklist || []),
    ]),
  ];

  if (freqai.max_open_trades != null) BOTS.freqai.maxTrades = freqai.max_open_trades;
  if (strategy.max_open_trades != null) BOTS.strategy.maxTrades = strategy.max_open_trades;
  if (grid.max_open_trades != null) BOTS.grid.maxTrades = grid.max_open_trades;
  if (strategy.enabled_strategies) {
    enabledStrategies = { ...enabledStrategies, ...strategy.enabled_strategies };
    updateStrategyDisplay();
  }
  updateMaxTradesHint();

  Object.keys(BOTS).forEach((bot) => {
    const el = $(BOTS[bot].maxTradesEl);
    if (!el) return;
    el.innerHTML = `
      <span class="max-trades-label">${BOTS[bot].label}</span>
      ${renderMaxTradesStepper(bot)}
      <span class="muted max-trades-hint">от 1 до ${MAX_TRADES_LIMIT}</span>
    `;
    bindMaxTradesStepper(el.querySelector(".stepper"), bot);
  });

  $("pair-list-active").innerHTML = active.length
    ? active
        .map(
          (p) => `<li><span>${p}</span><button class="btn danger btn-sm" data-pair="${p}" data-act="remove-pair">Удалить</button></li>`
        )
        .join("")
    : '<li class="empty-li">Нет активных пар</li>';

  $("pair-list-black").innerHTML = black.length
    ? black.map((p) => `<li><span>${p}</span></li>`).join("")
    : '<li class="empty-li">Пусто</li>';

  $("pair-list-active").querySelectorAll("[data-act=remove-pair]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm(`Удалить ${btn.dataset.pair} из торговли?`)) return;
      try {
        await pairConfigApi("/pairs", "POST", { action: "remove", pair: btn.dataset.pair });
        showPairMsg(`${btn.dataset.pair} удалена`);
        await loadPairSettings();
        await refreshAll();
      } catch (e) {
        showPairMsg(e.message, true);
      }
    });
  });
}

function openSettings() {
  $("settings-modal").classList.remove("hidden");
  $("settings-modal").setAttribute("aria-hidden", "false");
  loadPairSettings().catch((e) => {
    if (e.message === "auth") logout();
    else showPairMsg(e.message, true);
  });
}

async function loadInfoContent() {
  const list = $("info-strategy-list");
  if (!list) return;
  list.innerHTML = '<li class="empty-li muted">Загрузка…</li>';
  try {
    const data = await pairConfigApi("/strategies");
    const items = data.strategies || [];
    list.innerHTML = items
      .map(
        (s) => `
      <li class="info-strategy-item">
        <strong class="info-strategy-name">${s.name}</strong>
        <p class="info-strategy-desc">${s.desc}</p>
      </li>`
      )
      .join("");
  } catch (e) {
    if (e.message === "auth") logout();
    else list.innerHTML = '<li class="empty-li error">Не удалось загрузить описания</li>';
  }
}

function openInfo() {
  $("info-modal").classList.remove("hidden");
  $("info-modal").setAttribute("aria-hidden", "false");
  loadInfoContent();
}

function closeInfo() {
  $("info-modal").classList.add("hidden");
  $("info-modal").setAttribute("aria-hidden", "true");
}

function closeSettings() {
  $("settings-modal").classList.add("hidden");
  $("settings-modal").setAttribute("aria-hidden", "true");
  $("pair-msg").classList.add("hidden");
}

$("login-btn").addEventListener("click", async () => {
  creds.user = $("login-user").value.trim();
  creds.pass = $("login-pass").value;
  $("login-error").classList.add("hidden");
  try {
    await loginAll();
    $("login-screen").classList.add("hidden");
    $("app-screen").classList.remove("hidden");
    await refreshAll();
  } catch (e) {
    $("login-error").textContent =
      e.message === "auth"
        ? "Неверный логин или пароль"
        : e.message || "Ошибка входа";
    $("login-error").classList.remove("hidden");
  }
});

function closeMobileMenu() {
  const bar = $("topbar");
  const toggle = $("menu-toggle");
  if (!bar || !toggle) return;
  bar.classList.remove("is-menu-open");
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-label", "Открыть меню");
}

function toggleMobileMenu() {
  const bar = $("topbar");
  const toggle = $("menu-toggle");
  if (!bar || !toggle) return;
  const open = !bar.classList.contains("is-menu-open");
  bar.classList.toggle("is-menu-open", open);
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
  toggle.setAttribute("aria-label", open ? "Закрыть меню" : "Открыть меню");
}

function isMobileMenuMode() {
  return window.matchMedia("(max-width: 640px)").matches;
}

const LOG_POLL_MS = 1500;
let logsPollTimer = null;
let logsPositions = {};
let logsFilter = "all";
let logsStickBottom = true;

function logLineClass(line) {
  if (/\b(ERROR|CRITICAL|Exception|Traceback)\b/i.test(line)) return "log-error";
  if (/\b(WARNING|WARN)\b/i.test(line)) return "log-warn";
  return "";
}

function appendLogEntries(entries) {
  const out = $("logs-output");
  if (!out || !entries.length) return;
  const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 48;
  const frag = document.createDocumentFragment();
  for (const e of entries) {
    const row = document.createElement("div");
    row.className = `log-line log-${e.bot} ${logLineClass(e.line)}`;
    row.innerHTML = `<span class="log-tag">[${e.label}]</span>${escapeHtml(e.line)}`;
    frag.appendChild(row);
  }
  out.appendChild(frag);
  while (out.childElementCount > 2500) {
    out.removeChild(out.firstElementChild);
  }
  if (logsStickBottom && (atBottom || entries.length)) {
    out.scrollTop = out.scrollHeight;
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

async function pollLogs() {
  const status = $("logs-status");
  if ($("logs-pause")?.checked) return;
  try {
    const qs = new URLSearchParams();
    if (logsFilter !== "all") qs.set("bots", logsFilter);
    const hasPos = Object.keys(logsPositions).length > 0;
    if (hasPos) qs.set("since", JSON.stringify(logsPositions));
    else qs.set("tail", "250");
    const data = await pairConfigApi(`/logs?${qs}`);
    if (data.positions) logsPositions = data.positions;
    appendLogEntries(data.entries || []);
    if (status) {
      status.textContent = `Обновлено: ${new Date().toLocaleTimeString("ru-RU")} · ${(data.entries || []).length} новых строк`;
    }
  } catch (e) {
    if (e.message === "auth") logout();
    else if (status) status.textContent = `Ошибка: ${e.message}`;
  }
}

function startLogsPoll() {
  stopLogsPoll();
  pollLogs();
  logsPollTimer = setInterval(pollLogs, LOG_POLL_MS);
}

function stopLogsPoll() {
  if (logsPollTimer) {
    clearInterval(logsPollTimer);
    logsPollTimer = null;
  }
}

function setLogsFilter(bot) {
  logsFilter = bot;
  logsPositions = {};
  $("logs-output").innerHTML = "";
  document.querySelectorAll(".logs-filter").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.logsFilter === bot);
  });
  pollLogs();
}

function openLogs() {
  closeMobileMenu();
  $("dashboard-view")?.classList.add("hidden");
  const view = $("logs-view");
  view?.classList.remove("hidden");
  view?.setAttribute("aria-hidden", "false");
  logsPositions = {};
  $("logs-output").innerHTML = "";
  $("logs-status").textContent = "Загрузка…";
  startLogsPoll();
}

function closeLogs() {
  stopLogsPoll();
  $("logs-view")?.classList.add("hidden");
  $("logs-view")?.setAttribute("aria-hidden", "true");
  $("dashboard-view")?.classList.remove("hidden");
}

$("logout-btn").addEventListener("click", () => {
  closeMobileMenu();
  logout();
});
$("refresh-btn").addEventListener("click", () => {
  closeMobileMenu();
  refreshAll();
});
$("stats-btn").addEventListener("click", () => {
  closeMobileMenu();
  openStats();
});
$("info-btn").addEventListener("click", () => {
  closeMobileMenu();
  openInfo();
});
$("settings-btn").addEventListener("click", () => {
  closeMobileMenu();
  openSettings();
});
$("logs-btn").addEventListener("click", () => {
  closeMobileMenu();
  openLogs();
});
$("logs-back-btn")?.addEventListener("click", closeLogs);
$("logs-clear-btn")?.addEventListener("click", () => {
  $("logs-output").innerHTML = "";
  logsPositions = {};
  pollLogs();
});
document.querySelectorAll(".logs-filter").forEach((btn) => {
  btn.addEventListener("click", () => setLogsFilter(btn.dataset.logsFilter || "all"));
});
$("logs-output")?.addEventListener("scroll", (e) => {
  const el = e.target;
  logsStickBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
});

$("menu-toggle").addEventListener("click", toggleMobileMenu);

document.addEventListener("click", (e) => {
  if (!isMobileMenuMode()) return;
  const bar = $("topbar");
  if (!bar?.classList.contains("is-menu-open")) return;
  if (bar.contains(e.target)) return;
  closeMobileMenu();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if ($("logs-view") && !$("logs-view").classList.contains("hidden")) {
      closeLogs();
      return;
    }
    closeMobileMenu();
    closeStats();
    closeInfo();
    closeSettings();
  }
});

window.addEventListener("resize", () => {
  if (!isMobileMenuMode()) closeMobileMenu();
});
$("stats-close").addEventListener("click", closeStats);
$("stats-modal").querySelector(".modal-backdrop").addEventListener("click", closeStats);
$("stats-history-toggle")?.addEventListener("click", toggleStatsHistory);
$("info-close").addEventListener("click", closeInfo);
$("info-modal").querySelector(".modal-backdrop").addEventListener("click", closeInfo);
$("settings-close").addEventListener("click", closeSettings);
$("settings-modal").querySelector(".modal-backdrop").addEventListener("click", closeSettings);

$("pair-add-btn").addEventListener("click", async () => {
  const pair = $("pair-input").value.trim();
  if (!pair) return;
  try {
    await pairConfigApi("/pairs", "POST", { action: "add", pair });
    $("pair-input").value = "";
    showPairMsg(`${pair} добавлена`);
    await loadPairSettings();
    await refreshAll();
  } catch (e) {
    showPairMsg(e.message, true);
  }
});

$("pair-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("pair-add-btn").click();
});

const savedSession = loadStoredSession();
if (savedSession) {
  applyStoredCreds(savedSession);
  loginAll()
    .then(() => {
      $("login-screen").classList.add("hidden");
      $("app-screen").classList.remove("hidden");
      refreshAll();
    })
    .catch(() => {
      clearSession();
    });
}

setInterval(refreshAll, 15000);
setInterval(() => {
  if (loadStoredSession() && $("app-screen") && !$("app-screen").classList.contains("hidden")) {
    reloginFromStorage().catch(() => {});
  }
}, 10 * 60 * 1000);

bindStrategyPanelCollapse();
bindTradeLists();
