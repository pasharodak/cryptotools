const BOTS = {
  finder: {
    prefix: "/api/finder",
    label: "ML Finder",
    maxTrades: 3,
    stateEl: "finder-state",
    statsEl: "finder-stats",
    actionsEl: "finder-actions",
    tradesEl: "finder-trades",
    pairsEl: "finder-pairs",
    maxTradesEl: "finder-max-trades",
  },
  strategy: {
    prefix: "/api/strategy",
    label: "Стратегии + ML",
    maxTrades: 2,
    stakeAmount: 5,
    stakeEl: "strategy-stake",
    stateEl: "strategy-state",
    statsEl: "strategy-stats",
    actionsEl: "strategy-actions",
    tradesEl: "strategy-trades",
    pairsEl: "strategy-pairs",
    maxTradesEl: "strategy-max-trades",
  },
  grid: {
    prefix: "/api/grid",
    label: "Grid + ML",
    maxTrades: 2,
    stakeAmount: 10,
    stakeEl: "grid-stake",
    stateEl: "grid-state",
    statsEl: "grid-stats",
    actionsEl: "grid-actions",
    tradesEl: "grid-trades",
    pairsEl: "grid-pairs",
    maxTradesEl: "grid-max-trades",
  },
};

const MAX_TRADES_LIMIT = 20;
const STAKE_MIN = 1;
const STAKE_MAX = 100;
const STRATEGY_SL_MIN = 1;
const STRATEGY_SL_MAX = 20;
const STRATEGY_TP_MIN = 2;
const STRATEGY_TP_MAX = 50;
const STAKE_EDITABLE_BOTS = new Set(["grid", "strategy"]);
const FINDER_STATS_LABEL = "ML Finder (XGBoost scanner)";
const GRID_STATS_LABEL = "Grid (BB+ADX) + ML gate";
const STRATEGY_STATS_LABEL = "Стратегии + ML gate";
const BYBIT_GRID_STATS_LABEL = "Bybit Grid";
const ML_GATE_STRATEGY = "XGBoost · profit ≥60%";
const ML_GATE_BLOCK_LOSS = "XGBoost · block loss";
const BYBIT_GRID_MAX_LIMIT = 5;
let bybitGridMaxBots = 1;
let bybitGridInvest = "10";
const SESSION_DAYS = 7;
const SESSION_USER_KEY = "ct_user";
const SESSION_PASS_KEY = "ct_pass";
const SESSION_UNTIL_KEY = "ct_session_until";

let strategyCatalog = [];
let enabledStrategies = {};
let invertedStrategies = {};
let strategyRisk = { stoploss_pct: 5, take_profit_pct: 10 };
let dualHedgeEnabled = false;

let lastBybitGridRefresh = 0;
const BYBIT_GRID_REFRESH_MS = 30000;
const WHITELIST_REFRESH_MS = 60000;
const whitelistCache = {};
let tokens = { finder: null, strategy: null, grid: null };
let creds = { user: "", pass: "" };
let bybitGridHistoryOpen = false;

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
    err.includes("502 Bad Gateway") ||
    err.includes("503 Service Unavailable") ||
    err.includes("<html") ||
    err.includes("<!DOCTYPE")
  ) {
    return "Бот перезапускается — подождите несколько секунд и откройте статистику снова";
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
        `Сделка #${id} уже закрыта на бирже (позиция нулевая).\n\nЗаписать её в историю как закрытую? Сделка не будет удалена.`
      );
      if (!ok) {
        throw new Error("Сделка не закрыта — позиция на бирже отсутствует");
      }
      const archived = await pairConfigApi("/position-reconcile/archive", "POST", {
        bot,
        trade_id: Number(id),
      });
      if (!archived?.ok) {
        throw new Error(archived?.error || "Не удалось сохранить сделку в истории");
      }
      return { result: "archived", trade_id: id, ...archived };
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
  tokens = { finder: null, strategy: null, grid: null };
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
    if (allAuth) throw new Error("auth");
    const first = errors[0];
    const label = BOTS[first.bot]?.label || first.bot;
    if (first.status === 502 || first.status === 503) {
      throw new Error(`${label}: бот перезапускается — подождите 10–20 сек и обновите страницу`);
    }
    throw new Error(`${label}: не удалось подключиться (API недоступен)`);
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
  tokens = { finder: null, strategy: null, grid: null };
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
  if (bot === "finder") return FINDER_STATS_LABEL;
  if (bot === "grid") return GRID_STATS_LABEL;
  if (trade.enter_tag) {
    const raw = String(trade.enter_tag);
    let tag = raw.replace(/:hedge$/i, "").replace(/:inv$/i, "");
    tag = tag.replace(/:hedge$/i, "").replace(/:inv$/i, "");
    const bits = [];
    if (/:inv/i.test(raw)) bits.push("инв");
    if (/:hedge/i.test(raw)) bits.push("хедж");
    return strategyLabel(tag) + (bits.length ? ` · ${bits.join(" · ")}` : "");
  }
  if (trade.strategy && trade.strategy !== "MultiStrategyRouter") {
    return strategyLabel(trade.strategy);
  }
  return "Без тега";
}

function exitReasonLabel(reason) {
  const map = {
    roi: "ROI",
    reconcile: "Синхронизация",
    exchange_closed: "Закрыто на бирже",
    stop_loss: "Стоп",
    trailing_stop_loss: "Трейлинг",
    exit_signal: "Сигнал",
    force_exit: "Принудительно",
    emergency_exit: "Аварийный",
    sold_on_exchange: "На бирже",
    stoploss_on_exchange: "Стоп (биржа)",
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

function tradeMlMeta(trade) {
  return trade?.ml_meta || null;
}

function tradeMlConfidence(trade) {
  const m = tradeMlMeta(trade);
  if (!m) return null;
  const conf = m.ml_confidence ?? m.ml_gate_confidence;
  const n = Number(conf);
  return Number.isFinite(n) ? n : null;
}

function fmtMlConfidence(trade) {
  const conf = tradeMlConfidence(trade);
  if (conf == null) return "—";
  return `${(conf * 100).toFixed(0)}%`;
}

function fmtMlConfidenceDetail(trade) {
  const m = tradeMlMeta(trade);
  if (!m) return "—";
  const main = fmtMlConfidence(trade);
  if (main === "—") return "—";
  const pred = m.ml_predicted ? ` · ${m.ml_predicted}` : "";
  const gate = m.ml_gate_confidence != null && m.ml_confidence != null
    ? ` · gate ${(Number(m.ml_gate_confidence) * 100).toFixed(0)}%`
    : "";
  return `${main}${pred}${gate}`;
}

function tradeMlSummaryLabel(trade) {
  const m = tradeMlMeta(trade);
  if (!m) return "";
  const parts = [];
  const main = tradeMlConfidence(trade);
  if (main != null) parts.push(`ML ${fmtMlConfidence(trade)}`);
  if (m.ml_gate_confidence != null && m.ml_confidence != null) {
    parts.push(`gate ${(Number(m.ml_gate_confidence) * 100).toFixed(0)}%`);
  }
  return parts.length ? ` · ${parts.join(" · ")}` : "";
}

function invalidateWhitelistCache(bot = null) {
  if (bot) delete whitelistCache[bot];
  else Object.keys(whitelistCache).forEach((k) => delete whitelistCache[k]);
}

function mergeTradeMlMeta(trades, metaById) {
  if (!trades?.length || !metaById || !Object.keys(metaById).length) return trades || [];
  return trades.map((t) => {
    const m = metaById[String(t.trade_id)];
    return m ? { ...t, ml_meta: { ...(t.ml_meta || {}), ...m } } : t;
  });
}

async function enrichTradesWithMl(bot, trades) {
  if (!trades?.length) return trades || [];
  const ids = trades.map((t) => t.trade_id).filter((id) => id != null);
  if (!ids.length) return trades;
  try {
    const data = await pairConfigApi(
      `/trade-ml-meta?bot=${encodeURIComponent(bot)}&ids=${ids.join(",")}`
    );
    return mergeTradeMlMeta(trades, data?.meta || {});
  } catch {
    return trades;
  }
}

function renderTradeMlBadge(trade) {
  const conf = tradeMlConfidence(trade);
  if (conf == null) return "";
  const m = tradeMlMeta(trade);
  const pred = m?.ml_predicted ? ` ${m.ml_predicted}` : "";
  const gate =
    m?.ml_gate_confidence != null && m?.ml_confidence != null
      ? ` · gate ${(Number(m.ml_gate_confidence) * 100).toFixed(0)}%`
      : "";
  return `<span class="trade-ml-badge" title="ML уверенность при входе${pred}${gate}">ML ${fmtMlConfidence(trade)}${gate}</span>`;
}

const STATS_SCOPE_META = {
  all: {
    title: "Статистика — все боты",
    note: "Закрытые сделки по всем ботам, включая Bybit Grid.",
  },
  finder: { title: "Статистика — ML Finder", note: "Сделки TradeFinderStrategy (XGBoost scanner + pnl gate)." },
  strategy: {
    title: "Статистика — стратегии + ML gate",
    note: "Закрытые сделки по включённым стратегиям (TripleEMA, BB+RSI, ADX). Входы с ML gate.",
  },
  grid: {
    title: "Статистика — Grid + ML gate",
    note: "Закрытые сделки Freqtrade Grid (VolatilityGridStrategy, ML gate live_grid).",
  },
  bybitgrid: { title: "Статистика — Bybit Grid", note: "Закрытые нативные grid-боты на бирже Bybit." },
};

let pairlistMode = "scanner";
let finderBotEnabled = false;

function isAllVolumePairlist() {
  return pairlistMode === "all_volume";
}

async function refreshPairlistMode() {
  try {
    const d = await pairConfigApi("/pairs");
    pairlistMode = d._pairlist_mode || "scanner";
    finderBotEnabled = d._finder_bot_enabled !== false;
  } catch {
    /* keep previous */
  }
}

let statsScope = "all";
let statsPeriod = "all";
let statsDataCache = null;

let historyScope = "all";
let historyPeriod = "all";
let historyDateFrom = "";
let historyDateTo = "";
let historyStrategyFilter = "all";
let historyPairFilter = "";
let historyDataCache = null;

const HISTORY_EXPORT_COLUMNS = [
  { id: "close_date", label: "Дата закрытия", default: true },
  { id: "open_date", label: "Дата открытия", default: false },
  { id: "bot", label: "Бот", default: true },
  { id: "pair", label: "Пара", default: true },
  { id: "side", label: "Сторона", default: true },
  { id: "strategy", label: "Стратегия", default: true },
  { id: "ml", label: "ML %", default: true },
  { id: "pnl_usdt", label: "PnL USDT", default: true },
  { id: "pnl_pct", label: "PnL %", default: true },
  { id: "stake", label: "Стейк USDT", default: false },
  { id: "leverage", label: "Плечо", default: false },
  { id: "trade_id", label: "№ сделки", default: false },
  { id: "exit", label: "Выход", default: true },
];

let historyExportColumns = Object.fromEntries(
  HISTORY_EXPORT_COLUMNS.map((c) => [c.id, c.default])
);

function tradeCloseMs(trade) {
  if (trade.close_timestamp != null && trade.close_timestamp !== "") {
    const ts = Number(trade.close_timestamp);
    if (!Number.isNaN(ts) && ts > 0) {
      // Freqtrade API: milliseconds; older payloads may use seconds
      return ts < 1e12 ? ts * 1000 : ts;
    }
  }
  const raw = trade.close_date || trade.close_fill_date;
  if (!raw) return 0;
  const ms = new Date(raw).getTime();
  return Number.isNaN(ms) ? 0 : ms;
}

function bybitCloseMs(item) {
  if (!item?.closed_at) return 0;
  const ms = new Date(item.closed_at).getTime();
  return Number.isNaN(ms) ? 0 : ms;
}

function isTodayMs(tsMs) {
  if (!tsMs) return false;
  const d = new Date(tsMs);
  const now = new Date();
  return (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  );
}

function tradeSourceId(trade, bot) {
  if (bot === "finder") return "__finder__";
  if (bot === "grid") return "__grid__";
  if (trade.enter_tag) {
    return String(trade.enter_tag)
      .replace(/:hedge$/i, "")
      .replace(/:inv$/i, "")
      .replace(/:hedge$/i, "")
      .replace(/:inv$/i, "");
  }
  if (trade.strategy && trade.strategy !== "MultiStrategyRouter") return trade.strategy;
  return "__unknown__";
}

function periodBounds(period, dateFrom, dateTo) {
  if (dateFrom || dateTo) {
    const fromMs = dateFrom ? new Date(`${dateFrom}T00:00:00`).getTime() : 0;
    const toMs = dateTo ? new Date(`${dateTo}T23:59:59.999`).getTime() : Infinity;
    return { fromMs, toMs };
  }
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const endOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 59, 59, 999).getTime();
  switch (period) {
    case "today":
      return { fromMs: startOfToday, toMs: endOfToday };
    case "7d":
      return { fromMs: now.getTime() - 7 * 86400000, toMs: Infinity };
    case "30d":
      return { fromMs: now.getTime() - 30 * 86400000, toMs: Infinity };
    default:
      return { fromMs: 0, toMs: Infinity };
  }
}

function isInPeriodMs(tsMs, period, dateFrom, dateTo) {
  if (!tsMs) return false;
  const { fromMs, toMs } = periodBounds(period, dateFrom, dateTo);
  return tsMs >= fromMs && tsMs <= toMs;
}

function parsePairFilter(text) {
  const raw = String(text || "")
    .split(/[,;\s]+/)
    .map((p) => p.trim())
    .filter(Boolean);
  if (!raw.length) return null;
  return new Set(raw.map((p) => p.toUpperCase()));
}

function entryPair(entry) {
  if (entry.kind === "bybit") return entry.item?.pair || entry.item?.symbol || "";
  return entry.trade?.pair || "";
}

function filterClosedTrades(trades, period, dateFrom = "", dateTo = "") {
  const closed = (trades || []).filter((t) => !t.is_open);
  if (period === "all" && !dateFrom && !dateTo) return closed;
  return closed.filter((t) => isInPeriodMs(tradeCloseMs(t), period, dateFrom, dateTo));
}

function filterBybitHistory(items, period, dateFrom = "", dateTo = "") {
  const list = items || [];
  if (period === "all" && !dateFrom && !dateTo) return list;
  return list.filter((item) => isInPeriodMs(bybitCloseMs(item), period, dateFrom, dateTo));
}

function addBybitGridToRow(row, item) {
  const p = Number(item.realised_pnl ?? item.pnl ?? 0);
  if (p > 0) {
    row.wins += 1;
    row.profit += p;
  } else if (p < 0) {
    row.losses += 1;
    row.loss += Math.abs(p);
  }
}

function filterStatsRows(rows, scope) {
  if (scope === "all") return rows;
  if (scope === "finder") return rows.filter((r) => r.name === FINDER_STATS_LABEL);
  if (scope === "grid") return rows.filter((r) => r.name === GRID_STATS_LABEL);
  if (scope === "bybitgrid") return rows.filter((r) => r.name === BYBIT_GRID_STATS_LABEL);
  if (scope === "strategy") {
    return rows.filter(
      (r) =>
        r.name !== FINDER_STATS_LABEL &&
        r.name !== GRID_STATS_LABEL &&
        r.name !== BYBIT_GRID_STATS_LABEL
    );
  }
  return rows;
}

function buildStatsRows(finderTrades, stratTrades, gridTrades, bybitHistory, catalog) {
  const rows = new Map();

  const ensure = (name) => {
    if (!rows.has(name)) {
      rows.set(name, { name, wins: 0, losses: 0, profit: 0, loss: 0 });
    }
    return rows.get(name);
  };

  ensure(FINDER_STATS_LABEL);
  ensure(GRID_STATS_LABEL);
  ensure(BYBIT_GRID_STATS_LABEL);
  for (const s of catalog) ensure(s.name);

  for (const t of finderTrades) addClosedTradeToRow(ensure(FINDER_STATS_LABEL), t);
  for (const t of gridTrades) addClosedTradeToRow(ensure(GRID_STATS_LABEL), t);
  for (const t of stratTrades) {
    const name = tradeSourceLabel(t, "strategy");
    addClosedTradeToRow(ensure(name), t);
  }
  for (const item of bybitHistory) addBybitGridToRow(ensure(BYBIT_GRID_STATS_LABEL), item);

  const ordered = [
    FINDER_STATS_LABEL,
    GRID_STATS_LABEL,
    BYBIT_GRID_STATS_LABEL,
    ...catalog.map((s) => s.name),
  ];
  const result = [];
  for (const name of ordered) {
    if (rows.has(name)) result.push(rows.get(name));
  }
  for (const row of rows.values()) {
    if (!result.includes(row)) result.push(row);
  }
  return result;
}

function buildHistoryEntries(
  finderTrades,
  stratTrades,
  gridTrades,
  bybitHistory,
  scope,
  period,
  dateFrom = "",
  dateTo = "",
  strategyFilter = "all",
  pairFilter = ""
) {
  const entries = [];
  const ftBots =
    scope === "all"
      ? ["finder", "strategy", "grid"]
      : scope === "bybitgrid"
        ? []
        : [scope];

  if (ftBots.includes("finder")) {
    for (const t of filterClosedTrades(finderTrades, period, dateFrom, dateTo)) {
      entries.push({ kind: "trade", bot: "finder", trade: t });
    }
  }
  if (ftBots.includes("strategy")) {
    for (const t of filterClosedTrades(stratTrades, period, dateFrom, dateTo)) {
      entries.push({ kind: "trade", bot: "strategy", trade: t });
    }
  }
  if (ftBots.includes("grid")) {
    for (const t of filterClosedTrades(gridTrades, period, dateFrom, dateTo)) {
      entries.push({ kind: "trade", bot: "grid", trade: t });
    }
  }
  if (scope === "all" || scope === "bybitgrid") {
    for (const item of filterBybitHistory(bybitHistory, period, dateFrom, dateTo)) {
      entries.push({ kind: "bybit", bot: "bybitgrid", item });
    }
  }

  let filtered = entries;
  if (strategyFilter && strategyFilter !== "all") {
    filtered = filtered.filter((entry) => {
      if (entry.kind === "bybit") return false;
      if (entry.bot !== "strategy") return false;
      return tradeSourceId(entry.trade, entry.bot) === strategyFilter;
    });
  }

  const pairs = parsePairFilter(pairFilter);
  if (pairs) {
    filtered = filtered.filter((entry) => pairs.has(String(entryPair(entry)).toUpperCase()));
  }

  filtered.sort((a, b) => {
    const ta =
      a.kind === "bybit"
        ? bybitCloseMs(a.item)
        : tradeCloseMs(a.trade);
    const tb =
      b.kind === "bybit"
        ? bybitCloseMs(b.item)
        : tradeCloseMs(b.trade);
    return tb - ta;
  });
  return filtered;
}

function fmtBybitHistoryDate(item) {
  if (!item?.closed_at) return "—";
  const d = new Date(item.closed_at);
  if (Number.isNaN(d.getTime())) return item.closed_at.slice(0, 16);
  return d.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function bybitExitLabel(item) {
  return item.status_label || item.close_reason || "—";
}

function historyBotLabel(entry) {
  if (entry.kind === "bybit") return BYBIT_GRID_STATS_LABEL;
  if (entry.bot === "finder") return FINDER_STATS_LABEL;
  if (entry.bot === "grid") return GRID_STATS_LABEL;
  return BOTS[entry.bot]?.label || entry.bot;
}

function historyEntryCloseDateRaw(entry) {
  if (entry.kind === "bybit") return entry.item?.closed_at || "";
  const t = entry.trade;
  return t.close_date || t.close_fill_date || "";
}

function historyEntryOpenDateRaw(entry) {
  if (entry.kind === "bybit") return entry.item?.created_at || entry.item?.started_at || "";
  return entry.trade?.open_date || "";
}

function fmtHistoryDateRaw(raw) {
  if (!raw) return "";
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return String(raw);
  return d.toLocaleString("ru-RU");
}

function historyEntryExportRow(entry) {
  if (entry.kind === "bybit") {
    const item = entry.item;
    const pnl = Number(item.realised_pnl ?? item.pnl ?? 0);
    return {
      close_date: fmtHistoryDateRaw(item.closed_at),
      open_date: fmtHistoryDateRaw(item.created_at || item.started_at),
      bot: BYBIT_GRID_STATS_LABEL,
      pair: item.pair || item.symbol || "",
      side: item.grid_mode_label || "grid",
      strategy: "Bybit Grid",
      ml: "",
      pnl_usdt: pnl.toFixed(4),
      pnl_pct: "",
      stake: item.total_investment != null ? String(item.total_investment) : "",
      leverage: item.leverage != null ? String(item.leverage) : "",
      trade_id: item.bot_id || "",
      exit: bybitExitLabel(item),
    };
  }
  const t = entry.trade;
  const { abs, pct } = closedTradePnl(t);
  const conf = tradeMlConfidence(t);
  return {
    close_date: fmtHistoryDateRaw(t.close_date || t.close_fill_date),
    open_date: fmtHistoryDateRaw(t.open_date),
    bot: historyBotLabel(entry),
    pair: t.pair || "",
    side: t.is_short ? "SHORT" : "LONG",
    strategy: tradeSourceLabel(t, entry.bot),
    ml: conf != null ? (conf * 100).toFixed(1) : "",
    pnl_usdt: abs.toFixed(4),
    pnl_pct: pct.toFixed(2),
    stake: t.stake_amount != null ? String(t.stake_amount) : "",
    leverage: t.leverage != null ? String(t.leverage) : "",
    trade_id: t.trade_id != null ? String(t.trade_id) : "",
    exit: exitReasonLabel(t.exit_reason),
  };
}

function csvEscapeCell(value) {
  const s = String(value ?? "");
  if (/[;"\r\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

function downloadHistoryCsv(entries) {
  const cols = HISTORY_EXPORT_COLUMNS.filter((c) => historyExportColumns[c.id]);
  if (!cols.length) {
    alert("Выберите хотя бы одну колонку для экспорта");
    return;
  }
  if (!entries.length) {
    alert("Нет данных для экспорта с текущими фильтрами");
    return;
  }
  const header = cols.map((c) => csvEscapeCell(c.label));
  const rows = entries.map((entry) => {
    const data = historyEntryExportRow(entry);
    return cols.map((c) => csvEscapeCell(data[c.id] ?? ""));
  });
  const content = `\uFEFF${[header, ...rows].map((r) => r.join(";")).join("\r\n")}`;
  const blob = new Blob([content], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const stamp = new Date().toISOString().slice(0, 10);
  a.href = url;
  a.download = `criptotools-history-${stamp}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function renderHistoryTable(entries) {
  const bodyEl = $("history-table-body");
  if (!bodyEl) return;

  if (!entries.length) {
    bodyEl.innerHTML =
      '<tr class="stats-history-empty"><td colspan="8">Нет закрытых сделок по выбранным фильтрам</td></tr>';
    return;
  }

  bodyEl.innerHTML = entries
    .map((entry) => {
      if (entry.kind === "bybit") {
        const item = entry.item;
        const pnl = Number(item.realised_pnl ?? item.pnl ?? 0);
        const pnlClassName = pnlClass(pnl);
        return `<tr>
          <td>${fmtBybitHistoryDate(item)}</td>
          <td>${BYBIT_GRID_STATS_LABEL}</td>
          <td>${escapeHtml(item.pair || item.symbol || "—")}</td>
          <td>${escapeHtml(item.grid_mode_label || "grid")}</td>
          <td>Bybit</td>
          <td>—</td>
          <td class="${pnlClassName}">${fmtUsdSigned(pnl, 2)}</td>
          <td>${escapeHtml(bybitExitLabel(item))}</td>
        </tr>`;
      }
      const t = entry.trade;
      const { abs, pct } = closedTradePnl(t);
      const botLabel = historyBotLabel(entry);
      const side = t.is_short ? "SHORT" : "LONG";
      const source = tradeSourceLabel(t, entry.bot);
      const pnlClassName = pnlClass(abs);
      const mlConf = fmtMlConfidence(t);
      return `<tr>
        <td>${fmtTradeDate(t)}</td>
        <td>${botLabel}</td>
        <td>${t.pair}</td>
        <td>${side}</td>
        <td>${source}</td>
        <td>${mlConf}</td>
        <td class="${pnlClassName}">${fmtUsdSigned(abs, 2)}<br><span class="muted" style="font-size:0.75rem">${fmtPct(pct)}</span></td>
        <td>${exitReasonLabel(t.exit_reason)}</td>
      </tr>`;
    })
    .join("");
}

function collectUniquePairs(data) {
  const pairs = new Set();
  for (const t of [...(data.finderTrades || []), ...(data.stratTrades || []), ...(data.gridTrades || [])]) {
    if (t.pair) pairs.add(t.pair);
  }
  for (const item of data.bybitHistory || []) {
    const p = item.pair || item.symbol;
    if (p) pairs.add(p);
  }
  return [...pairs].sort();
}

function updateHistoryStrategyFilterOptions(catalog) {
  const sel = $("history-strategy-filter");
  if (!sel) return;
  const prev = historyStrategyFilter;
  sel.innerHTML =
    '<option value="all">Все стратегии</option>' +
    (catalog || strategyCatalog)
      .map((s) => `<option value="${escapeHtml(s.id)}">${escapeHtml(s.name)}</option>`)
      .join("");
  sel.value = [...sel.options].some((o) => o.value === prev) ? prev : "all";
  historyStrategyFilter = sel.value;
}

function updateHistoryPairSuggestions(pairs) {
  const dl = $("history-pair-suggestions");
  if (!dl) return;
  dl.innerHTML = pairs.map((p) => `<option value="${escapeHtml(p)}"></option>`).join("");
}

function renderHistoryExportColumns() {
  const el = $("history-export-columns");
  if (!el) return;
  if (el.dataset.bound) {
    el.querySelectorAll("[data-export-col]").forEach((input) => {
      input.checked = !!historyExportColumns[input.dataset.exportCol];
    });
    updateHistoryExportSummary();
    return;
  }
  el.dataset.bound = "1";
  el.innerHTML = HISTORY_EXPORT_COLUMNS.map(
    (c) => `<label class="history-export-col">
      <input type="checkbox" data-export-col="${c.id}" ${historyExportColumns[c.id] ? "checked" : ""} />
      ${escapeHtml(c.label)}
    </label>`
  ).join("");
  el.querySelectorAll("[data-export-col]").forEach((input) => {
    input.addEventListener("change", () => {
      historyExportColumns[input.dataset.exportCol] = input.checked;
      updateHistoryExportSummary();
    });
  });
  updateHistoryExportSummary();
}

function updateHistoryExportSummary() {
  const el = $("history-export-summary");
  if (!el) return;
  const n = HISTORY_EXPORT_COLUMNS.filter((c) => historyExportColumns[c.id]).length;
  el.textContent = `разделитель ; · ${n} колонок`;
}

function updateHistoryFilterUi() {
  document.querySelectorAll(".history-filter").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.historyScope === historyScope);
  });
  document.querySelectorAll(".history-period-btn").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.historyPeriod === historyPeriod);
  });
  const stratWrap = $("history-strategy-filter-wrap");
  if (stratWrap) {
    stratWrap.classList.toggle(
      "hidden",
      historyScope !== "all" && historyScope !== "strategy"
    );
  }
  if ($("history-date-from")) $("history-date-from").value = historyDateFrom;
  if ($("history-date-to")) $("history-date-to").value = historyDateTo;
  if ($("history-pair-filter")) $("history-pair-filter").value = historyPairFilter;
  if ($("history-strategy-filter")) $("history-strategy-filter").value = historyStrategyFilter;
}

function getFilteredHistoryEntries(data) {
  if (!data) return [];
  return buildHistoryEntries(
    data.finderTrades,
    data.stratTrades,
    data.gridTrades,
    data.bybitHistory,
    historyScope,
    historyPeriod,
    historyDateFrom,
    historyDateTo,
    historyStrategyFilter,
    historyPairFilter
  );
}

function renderHistoryView() {
  if (!historyDataCache) return;
  updateHistoryFilterUi();
  const entries = getFilteredHistoryEntries(historyDataCache);
  renderHistoryTable(entries);
  const statusEl = $("history-status");
  if (statusEl) {
    const warn =
      historyDataCache.loadErrors?.length > 0
        ? ` · ${historyDataCache.loadErrors.join(" · ")}`
        : "";
    statusEl.textContent = `Показано ${entries.length} сделок${warn}`;
  }
}

async function loadHistory() {
  const statusEl = $("history-status");
  if (statusEl) statusEl.textContent = "Загрузка…";
  const bodyEl = $("history-table-body");
  if (bodyEl) bodyEl.innerHTML = "";
  try {
    historyDataCache = await fetchStatsData(1000);
    updateHistoryStrategyFilterOptions(historyDataCache.catalog);
    updateHistoryPairSuggestions(collectUniquePairs(historyDataCache));
    renderHistoryView();
  } catch (e) {
    if (e.message === "auth") logout();
    else if (statusEl) statusEl.textContent = formatApiError(e.message);
  }
}

function openHistory() {
  closeMobileMenu();
  $("changelog-view")?.classList.add("hidden");
  $("logs-view")?.classList.add("hidden");
  $("dashboard-view")?.classList.add("hidden");
  const view = $("history-view");
  view?.classList.remove("hidden");
  view?.setAttribute("aria-hidden", "false");
  renderHistoryExportColumns();
  loadHistory();
}

function closeHistory() {
  $("history-view")?.classList.add("hidden");
  $("history-view")?.setAttribute("aria-hidden", "true");
  $("dashboard-view")?.classList.remove("hidden");
}

function setHistoryScope(scope) {
  historyScope = STATS_SCOPE_META[scope] ? scope : "all";
  if (historyScope !== "all" && historyScope !== "strategy") {
    historyStrategyFilter = "all";
  }
  renderHistoryView();
}

function setHistoryPeriod(period) {
  historyPeriod = period || "all";
  historyDateFrom = "";
  historyDateTo = "";
  renderHistoryView();
}

function applyHistoryDateRange() {
  historyDateFrom = $("history-date-from")?.value || "";
  historyDateTo = $("history-date-to")?.value || "";
  if (historyDateFrom || historyDateTo) {
    document.querySelectorAll(".history-period-btn").forEach((btn) => {
      btn.classList.remove("is-active");
    });
  }
  renderHistoryView();
}

function bindHistoryControls() {
  document.querySelectorAll(".history-filter").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setHistoryScope(btn.dataset.historyScope || "all"));
  });
  document.querySelectorAll(".history-period-btn").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setHistoryPeriod(btn.dataset.historyPeriod || "all"));
  });
  $("history-strategy-filter")?.addEventListener("change", (e) => {
    historyStrategyFilter = e.target.value || "all";
    renderHistoryView();
  });
  $("history-pair-filter")?.addEventListener("input", (e) => {
    historyPairFilter = e.target.value.trim();
    renderHistoryView();
  });
  $("history-date-from")?.addEventListener("change", applyHistoryDateRange);
  $("history-date-to")?.addEventListener("change", applyHistoryDateRange);
  $("history-export-btn")?.addEventListener("click", () => {
    if (!historyDataCache) return;
    downloadHistoryCsv(getFilteredHistoryEntries(historyDataCache));
  });
  $("history-refresh-btn")?.addEventListener("click", loadHistory);
  $("history-back-btn")?.addEventListener("click", closeHistory);
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

function updateStatsPeriodUi() {
  document.querySelectorAll(".stats-period-btn").forEach((btn) => {
    const active = (btn.dataset.period || "all") === statsPeriod;
    btn.classList.toggle("is-active", active);
  });
}

function updateStatsModalChrome() {
  const meta = STATS_SCOPE_META[statsScope] || STATS_SCOPE_META.all;
  const titleEl = $("stats-modal-title");
  const noteEl = $("stats-modal-note");
  if (titleEl) titleEl.textContent = meta.title;
  if (noteEl) {
    const periodLabel = statsPeriod === "today" ? "за сегодня" : "за всё время";
    noteEl.textContent = `${meta.note} Период: ${periodLabel}.`;
  }
  updateStatsPeriodUi();
}

function renderStatsModal(data) {
  const summaryEl = $("stats-summary");
  const bodyEl = $("stats-table-body");
  const footEl = $("stats-footnote");
  if (!bodyEl || !summaryEl) return;

  const catalog = data.catalog || strategyCatalog;
  const finderFiltered = filterClosedTrades(data.finderTrades, statsPeriod);
  const stratFiltered = filterClosedTrades(data.stratTrades, statsPeriod);
  const gridFiltered = filterClosedTrades(data.gridTrades, statsPeriod);
  const bybitFiltered = filterBybitHistory(data.bybitHistory, statsPeriod);

  const rows = filterStatsRows(
    buildStatsRows(finderFiltered, stratFiltered, gridFiltered, bybitFiltered, catalog),
    statsScope
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
  const net = totalProfit - totalLoss;
  const closedCount = totalWins + totalLosses;
  const warnHtml =
    data.loadErrors?.length > 0
      ? `<p class="stats-load-warn">${data.loadErrors.map((e) => escapeHtml(e)).join(" · ")}</p>`
      : "";

  summaryEl.innerHTML = `
    ${warnHtml}
    <div class="stats-kpi">
      <span>Прибыль</span>
      <strong class="pos">${fmtUsd(totalProfit)}</strong>
    </div>
    <div class="stats-kpi">
      <span>Потери</span>
      <strong class="neg">${fmtUsd(totalLoss)}</strong>
    </div>
    <div class="stats-kpi">
      <span>Итого</span>
      <strong class="${pnlClass(net)}">${fmtUsdSigned(net)}</strong>
    </div>
    <div class="stats-kpi">
      <span>Успешные / неуспешные</span>
      <strong>${totalWins} / ${totalLosses}</strong>
    </div>
  `;

  bodyEl.innerHTML = rows.length
    ? rows
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
        .join("")
    : '<tr class="stats-history-empty"><td colspan="5">Нет данных за выбранный период</td></tr>';

  if (footEl) {
    footEl.textContent =
      closedCount > 0
        ? `Закрытых операций за период: ${closedCount}.`
        : "Нет закрытых операций за выбранный период.";
  }

  updateStatsModalChrome();
}

async function fetchClosedTradesFromDb(limit = 500) {
  return pairConfigApi(`/closed-trades?limit=${limit}`);
}

async function fetchStatsData(tradeLimit = 500) {
  const loadErrors = [];
  let closedData = {};
  try {
    closedData = await fetchClosedTradesFromDb(tradeLimit);
  } catch (e) {
    loadErrors.push(`История сделок: ${formatApiError(e.message)}`);
  }
  const [catalogData, bybitData] = await Promise.all([
    pairConfigApi("/strategies").catch(() => ({ strategies: strategyCatalog })),
    pairConfigApi("/bybit-grid/history").catch(() => ({ history: [] })),
  ]);
  return {
    finderTrades: closedData.finder || [],
    stratTrades: closedData.strategy || [],
    gridTrades: closedData.grid || [],
    bybitHistory: bybitData?.history || [],
    catalog: catalogData.strategies || strategyCatalog,
    loadErrors,
  };
}

async function loadStatistics() {
  const summaryEl = $("stats-summary");
  const bodyEl = $("stats-table-body");
  if (!bodyEl) return;

  summaryEl.innerHTML = '<p class="muted">Загрузка…</p>';
  bodyEl.innerHTML = "";

  try {
    statsDataCache = await fetchStatsData();
    renderStatsModal(statsDataCache);
  } catch (e) {
    if (e.message === "auth") logout();
    else {
      summaryEl.innerHTML = `<p class="error">${escapeHtml(formatApiError(e.message))}</p>`;
      const footEl = $("stats-footnote");
      if (footEl) footEl.textContent = "";
      bodyEl.innerHTML = "";
    }
  }
}

function setStatsPeriod(period) {
  statsPeriod = period === "today" ? "today" : "all";
  if (statsDataCache) renderStatsModal(statsDataCache);
  else loadStatistics();
}

function openStats(scope = "all", period = null) {
  statsScope = STATS_SCOPE_META[scope] ? scope : "all";
  if (period) statsPeriod = period === "today" ? "today" : "all";
  $("stats-modal").classList.remove("hidden");
  $("stats-modal").setAttribute("aria-hidden", "false");
  loadStatistics();
}

function closeStats() {
  $("stats-modal").classList.add("hidden");
  $("stats-modal").setAttribute("aria-hidden", "true");
}

function bindPanelStatsButtons() {
  document.querySelectorAll(".panel-stats-btn").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => {
      openStats(btn.dataset.statsScope || "all", "today");
    });
  });
}

function bindStatsPeriodButtons() {
  document.querySelectorAll(".stats-period-btn").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => {
      setStatsPeriod(btn.dataset.period || "all");
    });
  });
}

function strategyLabel(id) {
  const fromCatalog = strategyCatalog.find((s) => s.id === id);
  return fromCatalog?.name || id || "—";
}

function syncEnabledFromPayload(data) {
  strategyCatalog = data.strategies || strategyCatalog;
  enabledStrategies = {};
  invertedStrategies = {};
  const enabled = data.enabled || {};
  const inverted = data.inverted || {};
  for (const s of strategyCatalog) {
    enabledStrategies[s.id] = !!enabled[s.id];
    invertedStrategies[s.id] = !!inverted[s.id];
  }
  syncStrategyRiskFromPayload(data);
  if (data?.dual_hedge != null) dualHedgeEnabled = !!data.dual_hedge;
}

function syncStrategyRiskFromPayload(data) {
  const risk = data?.risk || data?.state?.risk;
  if (!risk) return;
  if (risk.stoploss_pct != null) strategyRisk.stoploss_pct = Number(risk.stoploss_pct);
  if (risk.take_profit_pct != null) strategyRisk.take_profit_pct = Number(risk.take_profit_pct);
}

function strategyRiskSummaryText() {
  const sl = Number(strategyRisk.stoploss_pct);
  const tp = Number(strategyRisk.take_profit_pct);
  if (!Number.isFinite(sl) || !Number.isFinite(tp)) return "";
  const hedge = dualHedgeEnabled ? " · dual L+S" : "";
  return `SL −${sl}% · TP +${tp}%${hedge}`;
}

async function setDualHedge(enabled) {
  const data = await pairConfigApi("/pairs", "POST", {
    action: "set_dual_hedge",
    enabled: !!enabled,
  });
  syncEnabledFromPayload(data);
  updateStrategyDisplay();
  showReloadWarning(data);
  return data;
}

function renderDualHedgeControl() {
  const el = $("strategy-dual-hedge");
  if (!el) return;
  el.innerHTML = `
    <label class="strategy-dual-hedge-label">
      <input type="checkbox" id="strategy-dual-hedge-cb" ${dualHedgeEnabled ? "checked" : ""} />
      <span>
        <strong>Dual hedge</strong>
        <span class="muted"> — на каждый сигнал сразу long + short (2 позиции, 2× stake · max_open_trades ≥ 4)</span>
      </span>
    </label>
    <p class="muted strategy-dual-hedge-hint">Bybit hedge mode · 1 пара = 2 слота max_open_trades · ML gate на обе ноги</p>
  `;
  const cb = $("strategy-dual-hedge-cb");
  if (!cb || cb.dataset.bound) return;
  cb.dataset.bound = "1";
  cb.addEventListener("change", async () => {
    const next = cb.checked;
    cb.disabled = true;
    try {
      if (next && !confirm("Включить dual hedge? На каждый сигнал сразу long + short. max_open_trades поднимется до 4, Bybit — hedge mode.")) {
        cb.checked = false;
        return;
      }
      const data = await setDualHedge(next);
      if (data?.limits_note) showPairMsg(data.limits_note);
      await refreshAll();
    } catch (e) {
      cb.checked = !next;
      if (e.message === "auth") logout();
      else alert(formatApiError(e.message));
    } finally {
      cb.disabled = false;
    }
  });
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
    const riskNote = strategyRiskSummaryText();
    if (!active.length) {
      hint.textContent = "Включите хотя бы одну стратегию";
    } else {
      hint.textContent = `${active.length} из ${strategyCatalog.length} включено · сигнал от любой из них${riskNote ? ` · ${riskNote}` : ""}`;
    }
  }

  if (panelSummary) {
    const riskNote = strategyRiskSummaryText();
    if (!active.length) {
      panelSummary.textContent = riskNote ? `${riskNote} · развернуть` : "нет активных · развернуть";
    } else if (active.length === 1) {
      panelSummary.textContent = `${active[0].name}${riskNote ? ` · ${riskNote}` : ""} · изменить`;
    } else {
      panelSummary.textContent = `${active.length} из ${strategyCatalog.length} включено${riskNote ? ` · ${riskNote}` : ""} · изменить`;
    }
  }

  renderStrategyRiskControls();
  renderDualHedgeControl();
}

function bindCollapsibleSections() {
  document.querySelectorAll("[data-collapsible-toggle]").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    const section = btn.closest(".collapsible-section");
    if (!section) return;
    btn.addEventListener("click", () => {
      const expanded = section.classList.toggle("is-expanded");
      section.classList.toggle("is-collapsed", !expanded);
      btn.setAttribute("aria-expanded", expanded ? "true" : "false");
    });
  });
}

function whitelistSummaryText(pairs) {
  if (isAllVolumePairlist()) return "top-200 по объёму · развернуть";
  if (!pairs.length) return "пусто · развернуть";
  if (pairs.length <= 2) return `${pairs.join(", ")} · развернуть`;
  return `${pairs.length} пар · развернуть`;
}

function renderWhitelist(bot, whitelist) {
  const pairs = whitelist?.whitelist || [];
  const el = $(BOTS[bot].pairsEl);
  if (el) el.textContent = pairs.join(", ") || "—";
  const summary = $(`${bot}-pairs-summary`);
  if (summary) summary.textContent = whitelistSummaryText(pairs);
}

function renderStrategyTogglesInto(container) {
  if (!container) return;
  container.innerHTML = strategyCatalog
    .map(
      (s) => `
    <li class="strategy-toggle-item${invertedStrategies[s.id] ? " is-inverted" : ""}">
      <label class="strategy-toggle-label">
        <input type="checkbox" data-strategy="${s.id}" ${enabledStrategies[s.id] ? "checked" : ""} />
        <span class="strategy-toggle-text">
          <strong>${s.name}${invertedStrategies[s.id] ? ' <span class="strategy-inv-badge">инв</span>' : ""}</strong>
          <span class="muted strategy-toggle-desc">${s.desc}</span>
        </span>
      </label>
      <label class="strategy-invert-switch" title="Инвертировать: long↔short">
        <span class="strategy-invert-text">Инвертировать</span>
        <input type="checkbox" data-strategy-invert="${s.id}" ${invertedStrategies[s.id] ? "checked" : ""} />
        <span class="strategy-invert-slider" aria-hidden="true"></span>
      </label>
    </li>`
    )
    .join("");

  container.querySelectorAll('input[data-strategy]').forEach((cb) => {
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

  container.querySelectorAll("input[data-strategy-invert]").forEach((cb) => {
    cb.addEventListener("change", async () => {
      const id = cb.dataset.strategyInvert;
      const next = cb.checked;
      document.querySelectorAll(`input[data-strategy-invert="${id}"]`).forEach((el) => {
        el.disabled = true;
      });
      try {
        const data = await pairConfigApi("/pairs", "POST", {
          action: "toggle_strategy_invert",
          strategy: id,
          inverted: next,
        });
        syncEnabledFromPayload(data);
        updateStrategyDisplay();
        renderStrategyToggles();
        const msg = `${strategyLabel(id)}: инверсия ${next ? "вкл" : "выкл"}`;
        if (!$("settings-modal").classList.contains("hidden")) showPairMsg(msg);
      } catch (e) {
        document.querySelectorAll(`input[data-strategy-invert="${id}"]`).forEach((el) => {
          el.checked = !next;
        });
        if (e.message === "auth") logout();
        else alert(formatApiError(e.message));
      } finally {
        document.querySelectorAll(`input[data-strategy-invert="${id}"]`).forEach((el) => {
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

async function setStrategyRisk(stoplossPct, takeProfitPct) {
  const data = await pairConfigApi("/pairs", "POST", {
    action: "set_strategy_risk",
    stoploss_pct: stoplossPct,
    take_profit_pct: takeProfitPct,
  });
  syncStrategyRiskFromPayload(data);
  updateStrategyDisplay();
  showReloadWarning(data);
  return data;
}

function renderStrategyRiskInputHtml() {
  const sl = Number(strategyRisk.stoploss_pct);
  const tp = Number(strategyRisk.take_profit_pct);
  return `
    <div class="strategy-risk-bar-inner">
      <span class="strategy-risk-title">Стоп / тейк</span>
      <div class="numeric-setting strategy-risk-setting" data-strategy-risk-input>
        <label class="strategy-risk-field">
          <span class="muted">SL</span>
          <input type="number" class="numeric-setting-input strategy-risk-sl" min="${STRATEGY_SL_MIN}" max="${STRATEGY_SL_MAX}" step="0.5" value="${sl}" inputmode="decimal" aria-label="Стоп-лосс стратегий %" />
          <span class="numeric-setting-suffix">%</span>
        </label>
        <label class="strategy-risk-field">
          <span class="muted">TP</span>
          <input type="number" class="numeric-setting-input strategy-risk-tp" min="${STRATEGY_TP_MIN}" max="${STRATEGY_TP_MAX}" step="0.5" value="${tp}" inputmode="decimal" aria-label="Тейк-профит стратегий %" />
          <span class="numeric-setting-suffix">%</span>
        </label>
        <button type="button" class="btn btn-sm primary numeric-setting-save">Сохранить</button>
      </div>
      <span class="muted strategy-risk-hint">для всех стратегий · SL ${STRATEGY_SL_MIN}–${STRATEGY_SL_MAX}% · TP ${STRATEGY_TP_MIN}–${STRATEGY_TP_MAX}%</span>
    </div>
  `;
}

function renderStrategyRiskControls() {
  const panel = $("strategy-risk-panel");
  if (panel) {
    panel.innerHTML = renderStrategyRiskInputHtml();
    bindStrategyRiskInput(panel.querySelector("[data-strategy-risk-input]"));
  }
  const settings = $("strategy-risk-settings");
  if (settings) {
    settings.innerHTML = renderStrategyRiskInputHtml();
    bindStrategyRiskInput(settings.querySelector("[data-strategy-risk-input]"));
  }
}

function bindStrategyRiskInput(container) {
  if (!container) return;
  const slInput = container.querySelector(".strategy-risk-sl");
  const tpInput = container.querySelector(".strategy-risk-tp");
  const btn = container.querySelector(".numeric-setting-save");
  if (!slInput || !tpInput || !btn) return;

  const save = async () => {
    const sl = Number(slInput.value);
    const tp = Number(tpInput.value);
    if (!Number.isFinite(sl) || sl < STRATEGY_SL_MIN || sl > STRATEGY_SL_MAX) {
      alert(`Стоп-лосс: от ${STRATEGY_SL_MIN}% до ${STRATEGY_SL_MAX}%`);
      return;
    }
    if (!Number.isFinite(tp) || tp < STRATEGY_TP_MIN || tp > STRATEGY_TP_MAX) {
      alert(`Тейк-профит: от ${STRATEGY_TP_MIN}% до ${STRATEGY_TP_MAX}%`);
      return;
    }
    btn.disabled = true;
    try {
      await setStrategyRisk(sl, tp);
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
  };

  btn.onclick = save;
  slInput.onkeydown = (e) => {
    if (e.key === "Enter") save();
  };
  tpInput.onkeydown = (e) => {
    if (e.key === "Enter") save();
  };
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
  const activeMax =
    BOTS.strategy.maxTrades + BOTS.grid.maxTrades + bybitGridMaxBots;
  el.textContent = `До ${activeMax} позиций: Finder — ${BOTS.finder.maxTrades}, стратегии — ${BOTS.strategy.maxTrades}, Grid — ${BOTS.grid.maxTrades}, Bybit Grid — ${bybitGridMaxBots} · ML gate`;
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

async function setBotStake(bot, value) {
  const next = Number(value);
  if (!Number.isFinite(next) || next < STAKE_MIN || next > STAKE_MAX) return;
  const data = await pairConfigApi("/pairs", "POST", {
    action: "set_stake",
    bot,
    stake_amount: next,
  });
  BOTS[bot].stakeAmount = next;
  showReloadWarning(data);
  return data;
}

function bindNumericSetting(container, { validate, onSave }) {
  if (!container || container.dataset.bound) return;
  const input = container.querySelector(".numeric-setting-input");
  const btn = container.querySelector(".numeric-setting-save");
  if (!input || !btn) return;
  container.dataset.bound = "1";

  const save = async () => {
    const next = Number(input.value);
    if (!Number.isFinite(next)) {
      alert("Введите число");
      return;
    }
    const validation = validate ? validate(next) : true;
    if (validation !== true) {
      alert(typeof validation === "string" ? validation : "Некорректное значение");
      return;
    }
    btn.disabled = true;
    try {
      await onSave(next);
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
  };

  btn.addEventListener("click", save);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") save();
  });
}

function bindMaxTradesInput(container, bot) {
  bindNumericSetting(container, {
    validate: (value) =>
      value >= 1 && value <= MAX_TRADES_LIMIT ? true : `От 1 до ${MAX_TRADES_LIMIT}`,
    onSave: (value) => setMaxTrades(bot, value),
  });
}

function bindStakeInput(container, bot) {
  bindNumericSetting(container, {
    validate: (value) =>
      value >= STAKE_MIN && value <= STAKE_MAX
        ? true
        : `От ${STAKE_MIN} до ${STAKE_MAX} USDT`,
    onSave: (value) => setBotStake(bot, value),
  });
}

function renderMaxTradesInput(bot) {
  const maxTrades = BOTS[bot].maxTrades;
  return `
    <div class="numeric-setting" data-max-trades-input data-bot="${bot}">
      <input type="number" class="numeric-setting-input" min="1" max="${MAX_TRADES_LIMIT}" step="1" value="${maxTrades}" inputmode="numeric" aria-label="Макс. сделок ${BOTS[bot].label}" />
      <button type="button" class="btn btn-sm primary numeric-setting-save">Сохранить</button>
    </div>
  `;
}

function renderStakeInput(bot) {
  const stake = BOTS[bot].stakeAmount;
  return `
    <div class="numeric-setting numeric-setting-stake" data-stake-input data-bot="${bot}">
      <input type="number" class="numeric-setting-input" min="${STAKE_MIN}" max="${STAKE_MAX}" step="1" value="${stake}" inputmode="decimal" aria-label="Stake ${BOTS[bot].label} USDT" />
      <span class="numeric-setting-suffix">USDT</span>
      <button type="button" class="btn btn-sm primary numeric-setting-save">Сохранить</button>
    </div>
  `;
}

function renderStats(bot, profit, balance, openCount, stakeAmount) {
  const cfg = BOTS[bot];
  if (STAKE_EDITABLE_BOTS.has(bot) && stakeAmount != null) {
    BOTS[bot].stakeAmount = Number(stakeAmount);
  }
  const profitClosed = profit?.profit_closed_coin ?? profit?.profit_closed_percent;
  const usdt = getUsdtWallet(balance);
  const botAvail = usdt?.bot_owned;
  const walletFree = usdt?.free;
  const walletTotal = usdt?.balance;
  const inMargin = usdt?.used;
  const stakeCell = STAKE_EDITABLE_BOTS.has(bot)
    ? `<div class="stat stat-stake-limit"><span class="stat-inline-label">Stake</span>${renderStakeInput(bot)}</div>`
    : `<div class="stat">Stake<strong>${fmtUsd(stakeAmount ?? 5)}</strong></div>`;
  const el = $(cfg.statsEl);
  el.innerHTML = `
    <div class="stat stat-trades-limit">
      Открыто сделок<strong>${openCount} / ${cfg.maxTrades}</strong>
      ${renderMaxTradesInput(bot)}
    </div>
    <div class="stat">Прибыль (закрытые)<strong>${profit?.profit_closed_coin != null ? fmtUsd(profit.profit_closed_coin) : fmtPctRatio(profitClosed)}</strong></div>
    ${stakeCell}
    <div class="stat" title="Сколько USDT этот бот может использовать для новой сделки (учитываются только его сделки в БД)">
      Доступно боту<strong>${botAvail != null ? fmtUsd(botAvail) : "—"}</strong>
      <span class="stat-hint">общий кошелёк ${walletFree != null ? fmtUsd(walletFree) : "—"} · в марже ${inMargin != null ? fmtUsd(inMargin) : "—"}</span>
    </div>
  `;
  bindMaxTradesInput(el.querySelector("[data-max-trades-input]"), bot);
  if (STAKE_EDITABLE_BOTS.has(bot)) {
    bindStakeInput(el.querySelector("[data-stake-input]"), bot);
  }
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
    ["ML уверенность", fmtMlConfidenceDetail(t)],
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
      const mlBadge = renderTradeMlBadge(t);
      return `<article class="trade-card is-collapsed" data-trade-id="${t.trade_id}">
        <div class="trade-summary">
          <button type="button" class="trade-toggle" aria-expanded="false" aria-label="Развернуть сделку">
            <div class="trade-summary-main">
              <strong class="trade-pair">${t.pair}</strong>
              <span class="trade-meta">${t.is_short ? "SHORT" : "LONG"} · ${t.leverage || 1}x · #${t.trade_id}</span>
              <span class="trade-meta">${tag}${tradeMlSummaryLabel(t)} · маржа ${fmtUsd(t.stake_amount)}</span>
            </div>
            <div class="trade-head-right">
              ${mlBadge}
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
  if (bot === "finder" && !finderBotEnabled) {
    $(cfg.actionsEl).innerHTML =
      '<p class="muted finder-disabled-note">ML Finder отключён на сервере. Кнопка «Старт» недоступна.</p>';
    return;
  }
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
        if (act === "start") {
          if (bot === "finder" && !finderBotEnabled) {
            alert("ML Finder отключён на сервере.");
            return;
          }
          await api(bot, "/start", "POST");
        }
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
  if (isAllVolumePairlist()) {
    el.textContent = "Пары: top-200 USDT futures по объёму · whitelist отключён · сканер на паузе";
    return;
  }
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
  SupertrendStrategy: "Supertrend ML",
  MacdEmaStrategy: "MACD ML",
  FibPullbackStrategy: "Fib ML",
  TripleEmaStrategy: "EMA trend",
  BollingerRsiStrategy: "BB mean-rev",
  AdxMomentumStrategy: "Breakout",
  LiteIntradayStrategy: "Intraday",
  LiteRangeStrategy: "Range",
};

async function loadStrategyScanInfo() {
  const el = $("strategy-scan-info");
  if (!el) return;
  if (isAllVolumePairlist()) {
    el.textContent = "Пары: top-200 USDT futures по объёму · whitelist отключён · сканер на паузе";
    return;
  }
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

function setState(bot, running, hint = "") {
  const el = $(BOTS[bot].stateEl);
  if (bot === "finder" && !finderBotEnabled) {
    el.textContent = "DISABLED";
    el.className = "badge stopped";
    el.title = "ML Finder отключён на сервере — не запускается при перезагрузке";
    return;
  }
  if (hint === "offline") {
    el.textContent = "OFFLINE";
    el.className = "badge stopped";
    el.title = "Нет связи с API бота — перезапуск или перегрузка";
    return;
  }
  if (hint === "sync") {
    el.textContent = "SYNC…";
    el.className = "badge running";
    el.title = "Бот перезагружается или обновляет конфиг — подождите";
    return;
  }
  el.textContent = running ? "RUNNING" : "STOPPED";
  el.className = `badge ${running ? "running" : "stopped"}`;
  el.title = running
    ? bot === "finder"
      ? `ML Finder · scanner + pnl gate ${ML_GATE_STRATEGY}`
      : bot === "strategy"
        ? `Бот торгует · ML gate ${ML_GATE_STRATEGY}`
      : bot === "grid"
        ? `Бот торгует · ML gate ${ML_GATE_STRATEGY}`
        : "Бот торгует"
    : "Бот остановлен — нажмите «Старт»";
}

async function refreshBot(bot) {
  const settled = await Promise.allSettled([
    api(bot, "/show_config"),
    api(bot, "/profit"),
    api(bot, "/balance"),
    api(bot, "/status"),
  ]);
  const val = (i) => (settled[i].status === "fulfilled" ? settled[i].value : null);
  const config = val(0);
  const profit = val(1);
  const balance = val(2);
  const status = val(3);

  if (!config && settled[0].status === "rejected") {
    try {
      const count = await api(bot, "/count");
      if (count != null) {
        setState(bot, true, "sync");
        $(BOTS[bot].pairsEl).textContent = "—";
        const summary = $(`${bot}-pairs-summary`);
        if (summary) summary.textContent = "—";
        return [];
      }
    } catch {
      /* fall through */
    }
    throw settled[0].reason;
  }

  const running = String(config?.state || "").toLowerCase() === "running";
  const openTrades = Array.isArray(status) ? status : [];
  const maxFromConfig = Number(config?.max_open_trades);
  if (Number.isFinite(maxFromConfig) && maxFromConfig > 0) {
    BOTS[bot].maxTrades = Math.round(maxFromConfig);
  }
  const openCount = openTrades.length;

  const now = Date.now();
  let whitelist = whitelistCache[bot]?.data;
  if (!whitelistCache[bot] || now - whitelistCache[bot].ts >= WHITELIST_REFRESH_MS) {
    try {
      whitelist = await api(bot, "/whitelist");
      whitelistCache[bot] = { data: whitelist, ts: now };
    } catch {
      /* keep stale cache if any */
    }
  }

  setState(bot, running);
  renderStats(bot, profit, balance, openCount, config?.stake_amount);
  renderActions(bot, running);
  const enrichedTrades = await enrichTradesWithMl(bot, openTrades);
  renderTrades(bot, enrichedTrades);
  renderWhitelist(bot, whitelist);
  updateMaxTradesHint();
  return enrichedTrades;
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

function renderServerStatus(stats, adaptive = null) {
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
  const adaptiveLines = [];
  if (adaptive?.slots) {
    const s = adaptive.slots;
    adaptiveLines.push(
      `Слоты: ${s.open_total}/${s.max_total} открыто`,
      s.full
        ? "Адаптивный скан: слоты заполнены (Finder ~30 мин)"
        : `Адаптивный скан: каждые ${adaptive.current_interval_min} мин (шаг ${adaptive.step + 1}/4)`,
    );
    if (adaptive.seconds_until_next != null) {
      adaptiveLines.push(`До следующего скана: ~${Math.ceil(adaptive.seconds_until_next / 60)} мин`);
    }
  }
  el.title = [
    `Load: ${stats.load_1m} / ${stats.load_5m} / ${stats.load_15m} (${stats.cpus} CPU)`,
    `RAM: ${mem.used_mb ?? "?"} / ${mem.total_mb ?? "?"} MB (свободно ${mem.available_mb ?? "?"} MB)`,
    mem.swap_used_mb ? `Swap: ${mem.swap_used_mb} MB` : null,
    `Диск: ${dsk.used_gb ?? "?"} / ${dsk.total_gb ?? "?"} GB`,
    stats.uptime_seconds ? `Uptime: ${fmtUptime(stats.uptime_seconds)}` : null,
    adaptiveLines.length ? "" : null,
    ...adaptiveLines,
  ]
    .filter(Boolean)
    .join("\n");
}

async function refreshServerStats() {
  try {
    const [stats, adaptive] = await Promise.all([
      pairConfigApi("/system"),
      pairConfigApi("/adaptive-scan").catch(() => null),
    ]);
    renderServerStatus(stats, adaptive);
  } catch (e) {
    if (e.message === "auth") throw e;
    const el = $("server-status");
    if (el) {
      el.textContent = "Сервер —";
      el.className = "server-status muted";
    }
  }
}

async function refreshReconcileBanner() {
  const el = $("reconcile-banner");
  if (!el) return;
  try {
    const data = await pairConfigApi("/position-reconcile");
    const ghosts = Number(data.ghost_count || 0);
    const ft = Number(data.ft_open_count || 0);
    const bybit = Number(data.bybit_position_count || 0);
    const dupes = data.duplicate_pairs || [];
    if (ghosts === 0 && dupes.length === 0) {
      el.classList.add("hidden");
      el.innerHTML = "";
      return;
    }
    const ghostLines = (data.ghosts || [])
      .map((g) => `${BOTS[g.bot]?.label || g.bot} #${g.trade_id} ${g.pair}`)
      .join(", ");
    const dupeLine =
      dupes.length > 0
        ? ` Одна пара в нескольких ботах: ${dupes.join(", ")}.`
        : "";
    el.classList.remove("hidden");
    el.innerHTML = `
      <span class="reconcile-text">
        <strong>Расхождение с Bybit:</strong> в ботах ${ft} открытых, на бирже ${bybit}.
        Зависшие: ${ghostLines || "—"}.${dupeLine}
      </span>
      <button type="button" class="btn danger btn-sm" id="reconcile-fix-btn">Синхронизировать</button>
    `;
    $("reconcile-fix-btn")?.addEventListener("click", async () => {
      const btn = $("reconcile-fix-btn");
      if (!btn || btn.disabled) return;
      btn.disabled = true;
      btn.textContent = "…";
      try {
        const result = await pairConfigApi("/position-reconcile/fix", "POST", {});
        const archived = (result.fixed || []).filter((x) => x.action === "archived").length;
        const exited = (result.fixed || []).filter((x) => x.action === "forceexit").length;
        if (archived || exited) {
          alert(
            `Синхронизация: закрыто через биржу ${exited}, записано в историю ${archived}. ` +
              (result.ok ? "Расхождений больше нет." : "Часть расхождений осталась — обновите страницу.")
          );
        }
        await refreshAll();
      } catch (e) {
        alert(formatApiError(e.message));
      } finally {
        btn.disabled = false;
        btn.textContent = "Синхронизировать";
      }
    });
  } catch {
    el.classList.add("hidden");
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

function setBybitGridHistoryOpen(open) {
  bybitGridHistoryOpen = open;
  const panel = $("bybitgrid-history");
  const btn = $("bybitgrid-history-toggle");
  if (!panel || !btn) return;
  panel.classList.toggle("hidden", !open);
  panel.setAttribute("aria-hidden", open ? "false" : "true");
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) loadBybitGridHistory();
}

function toggleBybitGridHistory() {
  const panel = $("bybitgrid-history");
  if (!panel) return;
  setBybitGridHistoryOpen(panel.classList.contains("hidden"));
}

function formatBybitGridDate(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function renderBybitGridHistoryCard(b) {
  const pnlBot = Number(b.realised_pnl ?? b.pnl ?? 0);
  const mode = b.grid_mode_label || "—";
  const st = b.status_label || "завершён";
  const closedAt = formatBybitGridDate(b.closed_at);
  return `
    <div class="trade-card trade-card-closed" data-bot-id="${escapeHtml(b.bot_id || "")}">
      <div class="trade-head">
        <strong>${escapeHtml(b.pair || b.symbol || "—")}</strong>
        <span class="muted">${escapeHtml(String(mode))} · ${escapeHtml(String(st))}</span>
      </div>
      <p class="muted">Сеток: ${b.cell_number ?? "—"} · ${b.min_price ?? "—"} – ${b.max_price ?? "—"} · ${b.leverage ?? "—"}x</p>
      <p>PnL <span class="${pnlClass(pnlBot)}">${pnlBot >= 0 ? "+" : ""}${pnlBot.toFixed(2)} USDT</span>${b.settlement ? ` · ${escapeHtml(b.settlement)}` : ""}</p>
      <p class="muted">Закрыт ${closedAt}</p>
    </div>`;
}

async function loadBybitGridHistory() {
  const listEl = $("bybitgrid-history-list");
  if (!listEl) return;
  listEl.innerHTML = '<p class="muted">Загрузка…</p>';
  try {
    const data = await pairConfigApi("/bybit-grid/history");
    const items = data.history || [];
    if (!items.length) {
      listEl.innerHTML = '<p class="muted">История пуста</p>';
      return;
    }
    listEl.innerHTML = items.map(renderBybitGridHistoryCard).join("");
  } catch (e) {
    if (e.message === "auth") logout();
    else listEl.innerHTML = `<p class="error">${escapeHtml(formatApiError(e.message))}</p>`;
  }
}

async function saveBybitGridSettings(payload) {
  return pairConfigApi("/bybit-grid/config", "POST", payload);
}

function renderBybitGridMaxBotsInput(maxBots) {
  return `
    <input id="bg-cfg-max-bots" type="number" class="numeric-setting-input" min="1" max="${BYBIT_GRID_MAX_LIMIT}" step="1" value="${maxBots}" inputmode="numeric" aria-label="Макс. активных Bybit Grid ботов" />
  `;
}

function renderBybitGridSettings(data) {
  const el = $("bybitgrid-settings");
  if (!el) return;
  const cfg = data?.config || {};
  const defs = cfg.defaults || {};
  const maxBots = Number(cfg.max_active_bots || 1);
  const invest = String(defs.total_investment || "10");
  const tpUsdt = Number(defs.take_profit_usdt ?? 0.4);
  const slUsdt = Number(defs.stop_loss_usdt ?? 0.4);
  const preview = data?.tp_sl_preview || {};
  const tpPer = preview.take_profit_per ? ` (${preview.take_profit_per}%)` : "";
  const slPer = preview.stop_loss_per ? ` (${preview.stop_loss_per}%)` : "";

  bybitGridMaxBots = maxBots;
  bybitGridInvest = invest;
  updateMaxTradesHint();

  const slNote = preview.stop_loss_note
    ? `<br>${escapeHtml(preview.stop_loss_note)}`
    : "";

  el.innerHTML = `
    <h3>Настройки Bybit Grid</h3>
    <div class="bybitgrid-settings-grid">
      <label class="bybitgrid-max-bots-label">Макс. активных ботов
        ${renderBybitGridMaxBotsInput(maxBots)}
      </label>
      <label>Инвестиция на бота (USDT)
        <input id="bg-cfg-invest" type="number" min="5" step="1" value="${escapeHtml(invest)}" />
      </label>
      <label>Тейк-профит (USDT PnL)${tpPer}
        <input id="bg-cfg-tp" type="number" min="0.01" step="0.01" value="${tpUsdt}" />
      </label>
      <label>Стоп-лосс (USDT PnL)${slPer}
        <input id="bg-cfg-sl" type="number" min="0.01" step="0.01" value="${slUsdt}" />
      </label>
      <button type="button" class="btn btn-sm bybitgrid-settings-save" id="bybitgrid-save-settings">Сохранить</button>
      <p class="muted bybitgrid-settings-note">TP/SL — целевой PnL бота. Для SL процент на Bybit уменьшается с учётом плеча (иначе фактический убыток больше).${slNote}</p>
    </div>
  `;

  $("bybitgrid-save-settings")?.addEventListener("click", async () => {
    const btn = $("bybitgrid-save-settings");
    const maxBotsVal = Number($("bg-cfg-max-bots")?.value || 0);
    const investVal = Number($("bg-cfg-invest")?.value || 0);
    const tpVal = Number($("bg-cfg-tp")?.value || 0);
    const slVal = Number($("bg-cfg-sl")?.value || 0);
    if (maxBotsVal < 1 || maxBotsVal > BYBIT_GRID_MAX_LIMIT) {
      alert(`Макс. активных ботов — от 1 до ${BYBIT_GRID_MAX_LIMIT}`);
      return;
    }
    if (investVal < 5) {
      alert("Минимальная инвестиция — 5 USDT");
      return;
    }
    if (tpVal <= 0 || slVal <= 0) {
      alert("TP и SL должны быть больше 0");
      return;
    }
    btn.disabled = true;
    try {
      await saveBybitGridSettings({
        max_active_bots: maxBotsVal,
        total_investment: investVal,
        take_profit_usdt: tpVal,
        stop_loss_usdt: slVal,
      });
      await refreshBybitGrid();
    } catch (e) {
      alert(formatApiError(e.message));
    } finally {
      btn.disabled = false;
    }
  });
}

async function loadBybitGridScanInfo() {
  const el = $("bybitgrid-hint");
  if (!el) return;
  try {
    const d = await pairConfigApi("/bybit-grid/scan");
    if (!d.scanned_at || !d.best) {
      el.textContent = `Авто: скан боковика → grid ${bybitGridInvest} USDT · выключение в один клик`;
      return;
    }
    const t = new Date(d.scanned_at).toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
    const b = d.best;
    const overall = d.best_overall;
    let line = `Для запуска: ${b.pair} (ADX ${b.adx}, ATR×${b.atr_ratio ?? "—"}, score ${b.score})`;
    if (overall?.pair && overall.pair !== b.pair) {
      line = `Топ ${overall.pair} уже активна · ${line}`;
    }
    el.textContent = `${line} · скан ${t}`;
  } catch {
    el.textContent = `Авто: скан → grid ${bybitGridInvest} USDT`;
  }
}

async function refreshBybitGrid({ scan = false } = {}) {
  const stateEl = $("bybitgrid-state");
  const statsEl = $("bybitgrid-stats");
  const actionsEl = $("bybitgrid-actions");
  const botsEl = $("bybitgrid-bots");
  const defaultsEl = $("bybitgrid-defaults");
  const hintEl = $("bybitgrid-hint");
  if (!stateEl) return;

  try {
    if (scan) {
      try {
        await pairConfigApi("/bybit-grid/sync", "POST", { scan: true, full: true });
      } catch (_) {
        /* sync optional */
      }
    }
    const data = await pairConfigApi("/bybit-grid");
    const active = (data.bots || []).filter((b) => b.is_active);
    const maxBots = Number(data.max_active_bots || bybitGridMaxBots);
    bybitGridMaxBots = maxBots;
    renderBybitGridSettings(data);
    const credsOk = data.credentials_ok !== false;

    stateEl.textContent = credsOk ? (active.length ? `${active.length} / ${maxBots}` : "0 / " + maxBots) : "NO KEYS";
    stateEl.className = `badge ${active.length > 0 && credsOk ? "running" : credsOk ? "stopped" : "stopped"}`;
    stateEl.title = credsOk
      ? `Активных Bybit Grid: ${active.length} из ${maxBots}`
      : data.credentials_error || "API-ключи Bybit не настроены";

    const pnl = Number(data.total_pnl || 0);
    const pnlCls = pnlClass(pnl);
    const funding = data.funding || {};
    const fundLabel = funding.fund_usdt != null
      ? Number(funding.fund_usdt).toFixed(2)
      : funding.fund_unknown ? "?" : "—";
    const fundingLine = `Funding ${fundLabel} / Unified ${Number(funding.unified_usdt || 0).toFixed(2)} USDT`;
    if (hintEl) {
      let hint = funding.message;
      if (!hint) {
        hint = funding.ok
          ? `${fundingLine} · OK для grid`
          : `${fundingLine} · нужно ≥${Number(funding.target_usdt || 0).toFixed(2)} USDT на Funding (Grid не стартует с Unified)`;
      }
      if (funding.skip_funding_precheck && funding.fund_unknown) {
        hint += " · precheck отключён";
      }
      hintEl.textContent = hint;
      hintEl.className = funding.ok ? "muted bybitgrid-hint" : "bybitgrid-hint funding-warn";
    }
    statsEl.innerHTML = `
      <div class="stat"><span class="muted">Активных</span><strong>${active.length}</strong></div>
      <div class="stat"><span class="muted">Суммарный PnL</span><strong class="${pnlCls}">${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)} USDT</strong></div>
      <div class="stat"><span class="muted">Funding</span><strong class="${funding.ok === false ? "loss" : ""}">${funding.fund_usdt != null ? Number(funding.fund_usdt).toFixed(1) : "—"}</strong></div>
      <div class="stat"><span class="muted">Лимит</span><strong>${maxBots}</strong></div>
    `;

    const canDeployMore = active.length < maxBots;

    actionsEl.innerHTML = `
      ${canDeployMore ? `<button type="button" class="btn primary" id="bybitgrid-auto-deploy">Скан + запуск (${bybitGridInvest} USDT)</button>` : ""}
      <button type="button" class="btn" id="bybitgrid-scan">Скан</button>
      ${active.length > 0 ? `<button type="button" class="btn danger" id="bybitgrid-stop">Выключить grid</button>` : ""}
      <button type="button" class="btn ghost" id="bybitgrid-open-create">Вручную</button>
      <button type="button" class="btn" id="bybitgrid-refresh">Обновить</button>
    `;
    $("bybitgrid-stop")?.addEventListener("click", async () => {
      const running = (data.bots || []).filter((b) => b.is_active && b.bot_id);
      if (!running.length) return;
      const b = running[0];
      if (!confirm(`Выключить Bybit Grid на ${b.pair || b.symbol}? Позиция закроется на бирже.`)) return;
      const btn = $("bybitgrid-stop");
      btn.disabled = true;
      try {
        await pairConfigApi("/bybit-grid/close", "POST", { bot_id: b.bot_id });
        await refreshBybitGrid();
      } catch (e) {
        alert(formatApiError(e.message));
      } finally {
        btn.disabled = false;
      }
    });
    $("bybitgrid-auto-deploy")?.addEventListener("click", async () => {
      if (!confirm(`Запустить скан боковика и создать Bybit Grid на ${bybitGridInvest} USDT?`)) return;
      const btn = $("bybitgrid-auto-deploy");
      btn.disabled = true;
      btn.textContent = "Скан…";
      try {
        const res = await pairConfigApi("/bybit-grid/auto", "POST", {});
        if (res.skipped) {
          alert(res.message || "Достигнут лимит активных grid");
        } else if (res.deployed === false) {
          const pair = res.params?.pair || res.scan?.best?.pair || "?";
          alert(
            `Скан: ${pair}\nНе удалось создать grid:\n${formatApiError(res.error || "ошибка")}\n\n${res.hint || ""}`
          );
        } else {
          const pair = res.params?.pair || res.scan?.best?.pair || "?";
          alert(`Grid создан: ${pair} · ${bybitGridInvest} USDT`);
        }
        await refreshBybitGrid();
        await loadBybitGridScanInfo();
      } catch (e) {
        alert(formatApiError(e.message));
      } finally {
        btn.disabled = false;
        btn.textContent = `Скан + запуск (${bybitGridInvest} USDT)`;
      }
    });
    $("bybitgrid-scan")?.addEventListener("click", async () => {
      if (!confirm("Запустить скан боковика для Bybit Grid? Сетка не создаётся, только обновится лучшая пара.")) return;
      const btn = $("bybitgrid-scan");
      const hint = $("bybitgrid-hint");
      btn.disabled = true;
      const prev = btn.textContent;
      btn.textContent = "Скан…";
      if (hint) hint.textContent = "Скан выполняется…";
      try {
        const res = await pairConfigApi("/bybit-grid/scan", "POST", {});
        const scan = res.scan || {};
        const best = scan.best;
        const overall = scan.best_overall;
        if (best) {
          let msg = `Для запуска: ${best.pair}\nADX ${best.adx} · score ${best.score}`;
          if (overall?.pair && overall.pair !== best.pair) {
            msg = `Топ ${overall.pair} уже в активных ботах.\n\n${msg}`;
          }
          alert(msg);
        }
        await loadBybitGridScanInfo();
      } catch (e) {
        alert(formatApiError(e.message));
      } finally {
        btn.disabled = false;
        btn.textContent = prev;
      }
    });
    $("bybitgrid-open-create")?.addEventListener("click", openBybitGridModal);
    $("bybitgrid-refresh")?.addEventListener("click", () => refreshBybitGrid({ scan: true }));

    const historyCountEl = $("bybitgrid-history-count");
    const historyCount = Number(data.history_count || 0);
    if (historyCountEl) {
      historyCountEl.textContent = historyCount ? `(${historyCount})` : "";
    }
    if (bybitGridHistoryOpen) loadBybitGridHistory();

    const activeBots = (data.bots || []).filter((b) => b.is_active);
    if (!activeBots.length) {
      botsEl.innerHTML = '<p class="muted">Нет активных grid-ботов</p>';
    } else {
      botsEl.innerHTML = activeBots
        .map((b) => {
          const pnlBot =
            Number(b.realised_pnl || 0) + Number(b.unrealised_pnl || 0);
          const mode = b.grid_mode_label || b.params?.grid_mode || "—";
          const st = b.status_label || b.status || "—";
          const err = b.error ? `<p class="error">${escapeHtml(b.error)}</p>` : "";
          return `
            <div class="trade-card" data-bot-id="${escapeHtml(b.bot_id || "")}">
              <div class="trade-head">
                <strong>${escapeHtml(b.pair || b.symbol || "—")}</strong>
                <span class="muted">${escapeHtml(String(mode))} · ${escapeHtml(String(st))}</span>
              </div>
              <p class="muted">Сеток: ${b.cell_number ?? "—"} · ${b.min_price ?? "—"} – ${b.max_price ?? "—"} · ${b.leverage ?? "—"}x</p>
              <p>PnL <span class="${pnlClass(pnlBot)}">${pnlBot >= 0 ? "+" : ""}${pnlBot.toFixed(2)} USDT</span></p>
              ${err}
              <button type="button" class="btn btn-sm" data-close-grid="${escapeHtml(b.bot_id || "")}">Закрыть</button>
            </div>`;
        })
        .join("");
      botsEl.querySelectorAll("[data-close-grid]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const botId = btn.dataset.closeGrid;
          if (!botId || !confirm(`Закрыть Bybit Grid ${botId}?`)) return;
          btn.disabled = true;
          try {
            await pairConfigApi("/bybit-grid/close", "POST", { bot_id: botId });
            await refreshBybitGrid();
          } catch (e) {
            alert(formatApiError(e.message));
          } finally {
            btn.disabled = false;
          }
        });
      });
    }

    const defs = data.config?.defaults || {};
    if (defaultsEl) {
      defaultsEl.textContent = `Инвест. ${defs.total_investment || bybitGridInvest} USDT · TP/SL ${defs.take_profit_usdt ?? 0.4}/${defs.stop_loss_usdt ?? 0.4} USDT · сеток ${defs.cell_number} · плечо ${defs.leverage}x · ±${(Number(defs.price_range_pct || 0) * 100).toFixed(0)}%`;
    }
    await loadBybitGridScanInfo();
    if (hintEl) {
      hintEl.textContent = credsOk
        ? "Биржевой grid: ордера на Bybit, не Freqtrade"
        : (data.credentials_error || "Настройте BYBIT_API_KEY в .env");
    }
  } catch (e) {
    if (e.message === "auth") logout();
    else {
      stateEl.textContent = "ERR";
      if (hintEl) hintEl.textContent = formatApiError(e.message);
    }
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function showBybitGridFormMsg(text, isError = false) {
  const el = $("bybitgrid-form-msg");
  if (!el) return;
  el.textContent = text;
  el.className = `pair-msg ${isError ? "error" : "ok"}`;
  el.classList.remove("hidden");
}

function fillBybitGridForm(data) {
  if (!data) return;
  if (data.pair) $("bg-pair").value = data.pair;
  if (data.grid_mode != null) $("bg-mode").value = String(data.grid_mode);
  if (data.min_price) $("bg-min").value = data.min_price;
  if (data.max_price) $("bg-max").value = data.max_price;
  if (data.cell_number) $("bg-cells").value = data.cell_number;
  if (data.leverage) $("bg-leverage").value = data.leverage;
  if (data.total_investment) $("bg-invest").value = data.total_investment;
  const tpUsdt = $("bg-cfg-tp")?.value || data.take_profit_usdt;
  const slUsdt = $("bg-cfg-sl")?.value || data.stop_loss_usdt;
  if (tpUsdt != null && $("bg-tp")) $("bg-tp").value = tpUsdt;
  if (slUsdt != null && $("bg-sl")) $("bg-sl").value = slUsdt;
  if (data.grid_type != null) $("bg-type").value = String(data.grid_type);
}

function openBybitGridModal() {
  const invest = $("bg-cfg-invest")?.value || bybitGridInvest;
  const tp = $("bg-cfg-tp")?.value || "0.4";
  const sl = $("bg-cfg-sl")?.value || "0.4";
  if ($("bg-invest") && invest) $("bg-invest").value = invest;
  if ($("bg-tp")) $("bg-tp").value = tp;
  if ($("bg-sl")) $("bg-sl").value = sl;
  $("bybitgrid-modal")?.classList.remove("hidden");
  $("bybitgrid-form-msg")?.classList.add("hidden");
  if (!$("bg-pair").value) $("bg-pair").value = "SOL/USDT:USDT";
}

function closeBybitGridModal() {
  $("bybitgrid-modal")?.classList.add("hidden");
}

function collectBybitGridForm() {
  return {
    pair: $("bg-pair").value.trim(),
    grid_mode: Number($("bg-mode").value),
    min_price: $("bg-min").value.trim(),
    max_price: $("bg-max").value.trim(),
    cell_number: Number($("bg-cells").value),
    leverage: $("bg-leverage").value.trim(),
    total_investment: $("bg-invest").value.trim(),
    take_profit_usdt: $("bg-tp").value.trim(),
    stop_loss_usdt: $("bg-sl").value.trim(),
    tp_sl_type: 1,
    grid_type: Number($("bg-type").value),
  };
}

async function refreshAll() {
  try {
    const now = Date.now();
    const tasks = [
      refreshPairlistMode(),
      refreshStrategyEnabled(),
      refreshBotSafe("finder"),
      refreshBotSafe("strategy"),
      refreshBotSafe("grid"),
      refreshServerStats(),
      refreshReconcileBanner(),
      loadGridScanInfo(),
      loadStrategyScanInfo(),
    ];
    if (now - lastBybitGridRefresh >= BYBIT_GRID_REFRESH_MS) {
      tasks.push(refreshBybitGrid());
      lastBybitGridRefresh = now;
    }
    const results = await Promise.all(tasks);
    const allTrades = results.slice(2, 5).flat();
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
  const finder = data.finder || {};
  const strategy = data.strategy || {};
  const grid = data.grid || {};
  const active = finder.active_whitelist || [];
  const black = [
    ...new Set([
      ...(finder.blacklist || []),
      ...(strategy.blacklist || []),
      ...(grid.blacklist || []),
    ]),
  ];

  if (finder.max_open_trades != null) BOTS.finder.maxTrades = finder.max_open_trades;
  if (strategy.max_open_trades != null) BOTS.strategy.maxTrades = strategy.max_open_trades;
  if (grid.max_open_trades != null) BOTS.grid.maxTrades = grid.max_open_trades;
  if (grid.stake_amount != null) BOTS.grid.stakeAmount = Number(grid.stake_amount);
  if (strategy.stake_amount != null) BOTS.strategy.stakeAmount = Number(strategy.stake_amount);
  if (strategy.enabled_strategies) {
    enabledStrategies = { ...enabledStrategies, ...strategy.enabled_strategies };
    updateStrategyDisplay();
  }
  if (strategy.risk) {
    syncStrategyRiskFromPayload({ risk: strategy.risk });
    updateStrategyDisplay();
  }
  updateMaxTradesHint();

  Object.keys(BOTS).forEach((bot) => {
    const el = $(BOTS[bot].maxTradesEl);
    if (!el) return;
    el.innerHTML = `
      <span class="max-trades-label">${BOTS[bot].label}</span>
      ${renderMaxTradesInput(bot)}
      <span class="muted max-trades-hint">от 1 до ${MAX_TRADES_LIMIT}</span>
    `;
    bindMaxTradesInput(el.querySelector("[data-max-trades-input]"), bot);
  });

  STAKE_EDITABLE_BOTS.forEach((bot) => {
    const stakeEl = $(BOTS[bot].stakeEl);
    if (!stakeEl) return;
    stakeEl.innerHTML = `
      <span class="max-trades-label">${BOTS[bot].label} stake</span>
      ${renderStakeInput(bot)}
      <span class="muted max-trades-hint">от ${STAKE_MIN} до ${STAKE_MAX} USDT</span>
    `;
    bindStakeInput(stakeEl.querySelector("[data-stake-input]"), bot);
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

  const listsSummary = $("pair-lists-summary");
  if (listsSummary) {
    const parts = [];
    if (active.length) parts.push(`${active.length} активных`);
    if (black.length) parts.push(`${black.length} в blacklist`);
    listsSummary.textContent = parts.length ? `${parts.join(" · ")} · развернуть` : "пусто · развернуть";
  }

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

function splitLogLine(line) {
  const m = line.match(
    /^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?(?: UTC)?)\s+(.*)$/s
  );
  if (m) return { ts: m[1], text: m[2] };
  return { ts: null, text: line };
}

function appendLogEntries(entries) {
  const out = $("logs-output");
  if (!out || !entries.length) return;
  const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 48;
  const frag = document.createDocumentFragment();
  for (const e of entries) {
    const row = document.createElement("div");
    const parsed = e.ts ? { ts: e.ts, text: e.line } : splitLogLine(e.line);
    const body = parsed.ts && parsed.text !== e.line ? parsed.text : e.line;
    row.className = `log-line log-${e.bot} ${logLineClass(body)}`;
    const timeHtml = parsed.ts
      ? `<span class="log-time">${escapeHtml(parsed.ts)}</span>`
      : `<span class="log-time log-time-missing">—</span>`;
    row.innerHTML = `${timeHtml}<span class="log-tag">[${e.label}]</span>${escapeHtml(body)}`;
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
  $("changelog-view")?.classList.add("hidden");
  $("history-view")?.classList.add("hidden");
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

const CHANGELOG_CAT_LABELS = {
  telegram_bot: "Telegram-бот",
  finder: "ML Finder",
  strategy: "Стратегии",
  grid_ft: "Grid Freqtrade",
  ranging_scanner: "Сканер боковика",
  strategy_scanner: "Сканер стратегий",
  bybit_grid: "Bybit Grid",
  deploy_guards: "Фильтры деплоя",
  ui_infra: "UI и сервер",
  system: "Система",
};

let changelogFilter = "all";
let changelogCache = null;

function setChangelogFilter(cat) {
  changelogFilter = cat;
  document.querySelectorAll(".changelog-filter").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.changelogFilter === cat);
  });
  if (changelogCache) renderChangelog(changelogCache);
}

function formatChangelogPeriod(field, periods, entryAt) {
  if (!field || field === "_note" || !periods?.[field]) return "";
  const row = periods[field].find((p) => p.from === entryAt);
  if (!row) return "";
  const from = new Date(row.from).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
  if (!row.until) {
    return `Действует с ${from} — по сейчас`;
  }
  const until = new Date(row.until).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
  return `Период: ${from} — ${until} · значение: ${row.value_fmt}`;
}

function renderChangelog(data) {
  changelogCache = data;
  const out = $("changelog-output");
  const status = $("changelog-status");
  if (!out) return;
  const labels = { ...CHANGELOG_CAT_LABELS, ...(data?.categories || {}) };
  let entries = data?.entries || [];
  if (changelogFilter !== "all") {
    entries = entries.filter((e) => (e.category || "system") === changelogFilter);
  }
  const periods = data?.periods || {};
  if (!entries.length) {
    out.innerHTML = '<p class="muted">Записей пока нет. Изменения настроек будут появляться автоматически.</p>';
    if (status) status.textContent = "0 записей";
    return;
  }
  const byDay = new Map();
  for (const e of entries) {
    const d = e.at ? new Date(e.at) : new Date();
    const key = d.toLocaleDateString("ru-RU", { day: "2-digit", month: "long", year: "numeric" });
    if (!byDay.has(key)) byDay.set(key, []);
    byDay.get(key).push(e);
  }
  let html = "";
  for (const [day, items] of byDay) {
    html += `<section class="changelog-day"><h3 class="changelog-day-title">${escapeHtml(day)}</h3>`;
    for (const e of items) {
      const cat = e.category || "bybit_grid";
      const cls =
        cat === "deploy_guards"
          ? "is-guards"
          : cat === "system"
            ? "is-system"
            : cat === "telegram_bot"
              ? "is-telegram"
              : cat === "finder"
                ? "is-finder"
                : cat === "strategy"
                  ? "is-strategy"
                  : cat === "grid_ft"
                    ? "is-grid"
                    : cat === "ranging_scanner" || cat === "strategy_scanner"
                      ? "is-scanner"
                      : "";
      const time = e.at
        ? new Date(e.at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })
        : "—";
      const period = formatChangelogPeriod(e.field, periods, e.at);
      const note = e.note ? `<p class="changelog-period">${escapeHtml(e.note)}</p>` : "";
      const periodLine = period ? `<p class="changelog-period">${escapeHtml(period)}</p>` : "";
      html += `
        <article class="changelog-entry ${cls}">
          <div class="changelog-entry-head">
            <span class="changelog-entry-time">${escapeHtml(time)}</span>
            <span class="changelog-entry-cat">${escapeHtml(labels[cat] || cat)}</span>
          </div>
          <p class="changelog-entry-text">${escapeHtml(e.text || "")}</p>
          ${note}
          ${periodLine}
        </article>`;
    }
    html += "</section>";
  }
  out.innerHTML = html;
  if (status) status.textContent = `${entries.length} записей · обновлено ${new Date().toLocaleTimeString("ru-RU")}`;
}

async function loadChangelog() {
  const status = $("changelog-status");
  if (status) status.textContent = "Загрузка…";
  try {
    const data = await pairConfigApi("/changelog");
    renderChangelog(data);
  } catch (e) {
    if ($("changelog-output")) {
      $("changelog-output").innerHTML = `<p class="error">${escapeHtml(formatApiError(e.message))}</p>`;
    }
    if (status) status.textContent = "Ошибка загрузки";
  }
}

function openChangelog() {
  closeMobileMenu();
  stopLogsPoll();
  $("dashboard-view")?.classList.add("hidden");
  $("logs-view")?.classList.add("hidden");
  $("history-view")?.classList.add("hidden");
  const view = $("changelog-view");
  view?.classList.remove("hidden");
  view?.setAttribute("aria-hidden", "false");
  loadChangelog();
}

function closeChangelog() {
  $("changelog-view")?.classList.add("hidden");
  $("changelog-view")?.setAttribute("aria-hidden", "true");
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
  openStats("all", "all");
});
$("history-btn")?.addEventListener("click", () => {
  closeMobileMenu();
  openHistory();
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
$("changelog-btn")?.addEventListener("click", () => {
  closeMobileMenu();
  openChangelog();
});
$("changelog-back-btn")?.addEventListener("click", closeChangelog);
document.querySelectorAll(".changelog-filter").forEach((btn) => {
  btn.addEventListener("click", () => setChangelogFilter(btn.dataset.changelogFilter || "all"));
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
    if ($("changelog-view") && !$("changelog-view").classList.contains("hidden")) {
      closeChangelog();
      return;
    }
    if ($("logs-view") && !$("logs-view").classList.contains("hidden")) {
      closeLogs();
      return;
    }
    if ($("history-view") && !$("history-view").classList.contains("hidden")) {
      closeHistory();
      return;
    }
    closeMobileMenu();
    closeStats();
    closeInfo();
    closeSettings();
    closeBybitGridModal();
  }
});

window.addEventListener("resize", () => {
  if (!isMobileMenuMode()) closeMobileMenu();
});
$("stats-close").addEventListener("click", closeStats);
$("stats-modal").querySelector(".modal-backdrop").addEventListener("click", closeStats);
$("bybitgrid-history-toggle")?.addEventListener("click", toggleBybitGridHistory);
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
    invalidateWhitelistCache();
    await loadPairSettings();
    await refreshAll();
  } catch (e) {
    showPairMsg(e.message, true);
  }
});

$("pair-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("pair-add-btn").click();
});

$("bybitgrid-close")?.addEventListener("click", closeBybitGridModal);
$("bybitgrid-modal")?.querySelector(".modal-backdrop")?.addEventListener("click", closeBybitGridModal);
$("bybitgrid-suggest")?.addEventListener("click", async () => {
  const pair = $("bg-pair").value.trim();
  if (!pair) return showBybitGridFormMsg("Укажите пару", true);
  $("bybitgrid-suggest").disabled = true;
  try {
    const data = await pairConfigApi(`/bybit-grid/suggest?pair=${encodeURIComponent(pair)}`);
    fillBybitGridForm(data);
    showBybitGridFormMsg(`Подобрано для ${data.pair} · цена ${data.mark_price}`);
  } catch (e) {
    showBybitGridFormMsg(formatApiError(e.message), true);
  } finally {
    $("bybitgrid-suggest").disabled = false;
  }
});
$("bybitgrid-validate")?.addEventListener("click", async () => {
  $("bybitgrid-validate").disabled = true;
  try {
    await pairConfigApi("/bybit-grid/validate", "POST", collectBybitGridForm());
    showBybitGridFormMsg("Параметры прошли проверку Bybit");
  } catch (e) {
    showBybitGridFormMsg(formatApiError(e.message), true);
  } finally {
    $("bybitgrid-validate").disabled = false;
  }
});
$("bybitgrid-create")?.addEventListener("click", async () => {
  $("bybitgrid-create").disabled = true;
  try {
    const res = await pairConfigApi("/bybit-grid/create", "POST", collectBybitGridForm());
    showBybitGridFormMsg(`Grid создан: ${res.bot_id}`);
    closeBybitGridModal();
    await refreshBybitGrid();
  } catch (e) {
    showBybitGridFormMsg(formatApiError(e.message), true);
  } finally {
    $("bybitgrid-create").disabled = false;
  }
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

setInterval(refreshAll, 10000);
setInterval(() => {
  if (loadStoredSession() && $("app-screen") && !$("app-screen").classList.contains("hidden")) {
    reloginFromStorage().catch(() => {});
  }
}, 10 * 60 * 1000);

bindCollapsibleSections();
bindTradeLists();
bindPanelStatsButtons();
bindStatsPeriodButtons();
bindHistoryControls();
renderStrategyRiskControls();
renderDualHedgeControl();
