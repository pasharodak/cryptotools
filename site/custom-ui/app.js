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
    maxPerStrategy: 0,
    stakeAmount: 5,
    stakeEl: "strategy-stake",
    stateEl: "strategy-state",
    statsEl: "strategy-stats",
    actionsEl: "strategy-actions",
    tradesEl: "strategy-trades",
    pairsEl: "strategy-pairs",
    maxTradesEl: "strategy-max-trades",
    maxPerStrategyEl: "strategy-max-per-strategy",
  },
  grid: {
    prefix: "/api/grid",
    label: "Grid · Demo",
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

const STAKE_MIN = 1;
const STAKE_MAX = 100;
const STRATEGY_SL_MIN = 1;
const STRATEGY_SL_MAX = 20;
const STRATEGY_TP_MIN = 2;
const STRATEGY_TP_MAX = 50;
const TEST_SL_MIN = 1;
const TEST_SL_MAX = 20;
const TEST_TP_MIN = 0.5;
const TEST_TP_MAX = 50;
const TEST_TIME_CLOSE_MIN_MIN = 1;
const TEST_TIME_CLOSE_MIN_MAX = 10080; // 7 суток
const STAKE_EDITABLE_BOTS = new Set(["grid", "strategy"]);
const FINDER_STATS_LABEL = "ML Finder (Transformer)";
const GRID_STATS_LABEL = "Grid (BB+ADX) + ML gate";
const STRATEGY_STATS_LABEL = "Стратегии + ML gate";
const BYBIT_GRID_STATS_LABEL = "Bybit Grid";
const LLM_STATS_LABEL = "DeepSeek";
const LLM50_STATS_LABEL = "DeepSeek 50";
const CURSOR_STATS_LABEL = "Cursor";
const CURSOR50_STATS_LABEL = "Cursor 50";
const ML_GATE_STRATEGY = "XGBoost · profit ≥60%";
const ML_GATE_BLOCK_LOSS = "XGBoost · block loss";
const BYBIT_GRID_MAX_LIMIT = 5;
let bybitGridMaxBots = 1;
let bybitGridInvest = "10";
const SESSION_DAYS = 0.5; // 12h — match server SESSION_TTL_SECONDS
const SESSION_USER_KEY = "ct_user";
const SESSION_TOKEN_KEY = "ct_token";
const SESSION_UNTIL_KEY = "ct_session_until";
const SESSION_ROLE_KEY = "ct_role";
const SESSION_PASS_KEY = "ct_pass"; // legacy — cleared on login
const SESSION_IMPERSONATING_KEY = "ct_impersonating";

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Compact tip behind «i» — keeps panels clean. `text` may include trusted HTML. */
function uiHintHtml(text, { summary = "i", block = false } = {}) {
  const body = String(text ?? "").trim();
  if (!body) return "";
  const cls = block ? "ui-hint ui-hint-block" : "ui-hint";
  return `<details class="${cls}"><summary class="ui-hint-btn" aria-label="Подсказка">${escapeHtml(summary)}</summary><div class="ui-hint-body">${body}</div></details>`;
}

/** Display & calendar day boundaries for the whole panel (Moscow / no DST). */
const APP_TIMEZONE = "Europe/Moscow";
const APP_TZ_OFFSET_MS = 3 * 60 * 60 * 1000;
const APP_TZ_LABEL = "UTC+3";

let strategyCatalog = [];
let enabledStrategies = {};
let invertedStrategies = {};
let trainedRiskStrategies = {};
let trainedRiskAvailable = {};
let trainedRiskSpecs = {};
let mlConfidenceStrategies = {};
let entrySideStrategies = {};
let mlConfidenceDefaults = {};
let mlConfidenceChoices = [0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95];
let htfSltp = {};
let htfSltpDefaults = {};
let htfSltpChoices = [2, 3, 5, 8, 10, 13, 15, 20];
let sltpSlChoices = [2, 3, 5, 8, 10, 13, 15, 20];
let sltpTpChoices = [2, 3, 5, 8, 10, 13, 15, 20, 30, 50];
let htfStakeAmount = 5;
let strategyPairsMap = {};
let strategyPairsCascade = false;
let strategyRisk = {
  stoploss_pct: 5,
  take_profit_pct: 10,
  time_close_enabled: false,
  time_close_minutes: 60,
  time_close_tp_pct: 1.3,
  time_close_loss_enabled: false,
  time_close_loss_minutes: 60,
  time_close_loss_tp_pct: 1.3,
};
let testSettings = {
  max_open_trades: 8,
  max_open_trades_per_strategy: 2,
  stake_amount: 5,
  stoploss_pct: 2,
  take_profit_pct: 2,
  time_close_enabled: false,
  time_close_minutes: 60,
  time_close_tp_pct: 1.3,
  time_close_loss_enabled: false,
  time_close_loss_minutes: 60,
  time_close_loss_tp_pct: 1.3,
};
let dualHedgeEnabled = false;
let allowedBlocks = null; // null = all
let allowedStrategies = null; // null = all
let permissionCatalog = { blocks: [], strategies: [] };
let isImpersonating = false;
let impersonatedBy = null;
let accountKind = "live"; // live | admin (demo login removed)
let demoMirrorAdmin = false;
let settingsReadonly = false;
let canSwitchDemo = false;
let canSwitchLive = false;
let linkedAccountUsername = null;
let hasLiveSecrets = false;
let hasDemoSecrets = false;
let tradeOnPref = "bybit"; // legacy mirror (= main)
let tradeOnMainPref = "bybit";
let tradeOnTestPref = "bybit";
let tradingFlagsLive = {};
let tradingFlagsDemo = {};
let pnlDashMode = "all";
let ratingAccountMode = "all";
let statsAccountMode = "all";

let lastBybitGridRefresh = 0;
const BYBIT_GRID_REFRESH_MS = 30000;
const WHITELIST_REFRESH_MS = 60000;
const whitelistCache = {};
/** Last successful bot payload — keep UI filled while refresh is in flight / fails. */
const botLiveCache = {};
let serverStatusCache = null;
let gridScanInfoText = null;
let strategyScanInfoText = null;
let tokens = { finder: null, strategy: null, grid: null };
let creds = { user: "", pass: "" };
let sessionToken = "";
let currentUser = null; // { id, username, role, ... }
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
  if (/invalid argument/i.test(err)) {
    return "Не удалось закрыть через signal-engine — обновите страницу (Ctrl+F5) и попробуйте снова";
  }
  if (/нет API-ключей|no api.?key|has_secrets/i.test(err)) {
    return "Нет API-ключей Bybit — сохраните ключи в Настройках";
  }
  if (/trade.?block|trading.?disabled|торговля выключена/i.test(err)) {
    return "Торговля выключена — включите бота Стратегии (Старт) и повторите";
  }
  if (err === "Internal Server Error" || err.includes("InvalidOrderException")) {
    return "Биржа отклонила закрытие — попробуйте ещё раз (синхронизация с биржей)";
  }
  if (err.includes("Error querying /api/v1/forceexit:")) {
    return humanizeApiError(err.split(": ").slice(1).join(": "));
  }
  return err;
}

/** Parse bot/API timestamps: naive datetime → UTC instant. */
function parseAppInstant(raw) {
  if (raw == null || raw === "") return null;
  if (raw instanceof Date) return Number.isNaN(raw.getTime()) ? null : raw;
  if (typeof raw === "number") {
    const ms = raw < 1e12 ? raw * 1000 : raw;
    const d = new Date(ms);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  let s = String(raw).trim();
  if (!s) return null;
  if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(s) && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(s)) {
    s = s.replace(" ", "T");
    if (!/[zZ]$/.test(s)) s += "Z";
  }
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

function fmtAppDateTime(raw, options = {}) {
  const d = parseAppInstant(raw);
  if (!d) return raw == null || raw === "" ? "—" : String(raw);
  return d.toLocaleString("ru-RU", { timeZone: APP_TIMEZONE, ...options });
}

function fmtAppTime(raw, options = {}) {
  return fmtAppDateTime(raw, {
    hour: "2-digit",
    minute: "2-digit",
    ...options,
  });
}

function fmtAppDateShort(raw) {
  return fmtAppDateTime(raw, {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtAppDayLabel(raw) {
  return fmtAppDateTime(raw, {
    day: "2-digit",
    month: "long",
    year: "numeric",
  });
}

/** Midnight of the calendar day in APP_TIMEZONE that contains `ms` (default: now). */
function appStartOfDayMs(ms = Date.now()) {
  const wall = new Date(ms + APP_TZ_OFFSET_MS);
  return Date.UTC(wall.getUTCFullYear(), wall.getUTCMonth(), wall.getUTCDate()) - APP_TZ_OFFSET_MS;
}

function appEndOfDayMs(ms = Date.now()) {
  return appStartOfDayMs(ms) + 86400000 - 1;
}

/** Interpret YYYY-MM-DD from date inputs as a calendar day in UTC+3. */
function appDateInputBoundsMs(dateFrom, dateTo) {
  let fromMs = 0;
  let toMs = Infinity;
  if (dateFrom) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateFrom).trim());
    if (m) {
      fromMs =
        Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) - APP_TZ_OFFSET_MS;
    }
  }
  if (dateTo) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateTo).trim());
    if (m) {
      toMs =
        Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) -
        APP_TZ_OFFSET_MS +
        86400000 -
        1;
    }
  }
  return { fromMs, toMs };
}

function isTodayMs(tsMs) {
  if (!tsMs) return false;
  const start = appStartOfDayMs();
  return tsMs >= start && tsMs <= appEndOfDayMs();
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
    /Error querying \/api\/v1\/forceexit:.*(position is zero|110017|Failed to exit)/i.test(msg)
  );
}

async function closeTrade(bot, tradeId) {
  if (settingsReadonly || demoMirrorAdmin) {
    throw new Error("Demo: только просмотр сделок администратора");
  }
  const id = String(tradeId);
  // Shared signal-engine has no local fills — close via trade-executor / Bybit.
  if (bot === "strategy" && sharedStackMode) {
    return await pairConfigApi("/force-close", "POST", {
      bot: "strategy",
      trade_id: Number(id),
    });
  }
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
  } else {
    console.warn("reload_warning", msg);
  }
}

/** Keep UI/limit values from being overwritten by a stale live show_config after save. */
const limitPins = {
  maxTrades: {}, // bot -> { value, until }
  maxPerStrategy: null, // { value, until } | null
  testMaxOpen: null,
  testMaxPer: null,
  stake: {}, // bot -> { value, until }
};
const LIMIT_PIN_MS = 300000;
/** Keep trade cards expanded across refreshAll / paintBotLive re-renders. */
const expandedTradeIds = new Set(); // `${bot}:${tradeId}`

function tradeExpandKey(bot, tradeId) {
  return `${bot}:${tradeId}`;
}

function isTradeExpanded(bot, tradeId) {
  return expandedTradeIds.has(tradeExpandKey(bot, tradeId));
}

function setTradeExpanded(bot, tradeId, expanded) {
  const key = tradeExpandKey(bot, tradeId);
  if (expanded) expandedTradeIds.add(key);
  else expandedTradeIds.delete(key);
}

function pinMaxTrades(bot, value) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n) || n < 0) return;
  limitPins.maxTrades[bot] = { value: n, until: Date.now() + LIMIT_PIN_MS };
  BOTS[bot].maxTrades = n;
}

function applyPinnedMaxTrades(bot, fromDb) {
  const pin = limitPins.maxTrades[bot];
  if (pin && Date.now() < pin.until) {
    BOTS[bot].maxTrades = pin.value;
    return pin.value;
  }
  const n = Number(fromDb);
  if (Number.isFinite(n) && n >= 0) {
    BOTS[bot].maxTrades = Math.round(n);
  }
  return BOTS[bot].maxTrades;
}

/** Apply slot/stake limits from pair-config DB payload only (never show_config). */
function syncLimitsFromPairPayload(data) {
  if (!data || typeof data !== "object") return;
  const lim = data.limits || {};
  const maxMap = lim.max_open_trades || {};
  const stakeMap = lim.stake_amount || {};
  for (const bot of Object.keys(BOTS)) {
    const fromLimits = maxMap[bot];
    const fromBot = data[bot]?.max_open_trades;
    const v = fromLimits != null ? fromLimits : fromBot;
    if (v != null) applyPinnedMaxTrades(bot, v);
  }
  if (lim.max_open_trades_per_strategy != null) {
    applyPinnedMaxPerStrategy(lim.max_open_trades_per_strategy);
  } else if (data.strategy?.max_open_trades_per_strategy != null) {
    applyPinnedMaxPerStrategy(data.strategy.max_open_trades_per_strategy);
  }
  if (stakeMap.strategy != null && Number.isFinite(Number(stakeMap.strategy))) {
    BOTS.strategy.stakeAmount = Number(stakeMap.strategy);
  } else if (data.strategy?.stake_amount != null) {
    BOTS.strategy.stakeAmount = Number(data.strategy.stake_amount);
  }
  if (stakeMap.grid != null && Number.isFinite(Number(stakeMap.grid))) {
    BOTS.grid.stakeAmount = Number(stakeMap.grid);
  } else if (data.grid?.stake_amount != null) {
    BOTS.grid.stakeAmount = Number(data.grid.stake_amount);
  }
  if (lim.htf_stake_amount != null && Number.isFinite(Number(lim.htf_stake_amount))) {
    htfStakeAmount = Number(lim.htf_stake_amount);
  }
  updateMaxTradesHint();
  updateBotSettingsSummaries();
}

function pinMaxPerStrategy(value) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n) || n < 0) return;
  limitPins.maxPerStrategy = { value: n, until: Date.now() + LIMIT_PIN_MS };
  BOTS.strategy.maxPerStrategy = n;
}

function applyPinnedMaxPerStrategy(fromLive) {
  const pin = limitPins.maxPerStrategy;
  if (pin && Date.now() < pin.until) {
    BOTS.strategy.maxPerStrategy = pin.value;
    return pin.value;
  }
  if (fromLive != null && Number.isFinite(Number(fromLive))) {
    BOTS.strategy.maxPerStrategy = Math.round(Number(fromLive));
  }
  return BOTS.strategy.maxPerStrategy;
}

function pinTestMaxOpen(value) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n) || n < 0) return;
  limitPins.testMaxOpen = { value: n, until: Date.now() + LIMIT_PIN_MS };
  testSettings.max_open_trades = n;
}

function pinTestMaxPer(value) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n) || n < 0) return;
  limitPins.testMaxPer = { value: n, until: Date.now() + LIMIT_PIN_MS };
  testSettings.max_open_trades_per_strategy = n;
}

function applyPinnedTestSettings() {
  const o = limitPins.testMaxOpen;
  if (o && Date.now() < o.until) testSettings.max_open_trades = o.value;
  const p = limitPins.testMaxPer;
  if (p && Date.now() < p.until) testSettings.max_open_trades_per_strategy = p.value;
}

function authHeader(_bot) {
  if (sessionToken) return { Authorization: `Bearer ${sessionToken}` };
  return {};
}

function sessionHeader() {
  if (sessionToken) return { Authorization: `Bearer ${sessionToken}` };
  if (creds.user && creds.pass) {
    return { Authorization: `Basic ${btoa(`${creds.user}:${creds.pass}`)}` };
  }
  return {};
}

function isAdminUser() {
  if (isImpersonating) return false;
  return (currentUser?.role || localStorage.getItem(SESSION_ROLE_KEY) || "") === "admin";
}

function saveSession() {
  const until = Date.now() + SESSION_DAYS * 24 * 60 * 60 * 1000;
  localStorage.setItem(SESSION_USER_KEY, creds.user || currentUser?.username || "");
  localStorage.setItem(SESSION_TOKEN_KEY, sessionToken || "");
  localStorage.setItem(SESSION_UNTIL_KEY, String(until));
  if (currentUser?.role) localStorage.setItem(SESSION_ROLE_KEY, currentUser.role);
  localStorage.setItem(SESSION_IMPERSONATING_KEY, isImpersonating ? "1" : "0");
  localStorage.removeItem(SESSION_PASS_KEY);
  sessionStorage.removeItem("ct_user");
  sessionStorage.removeItem("ct_pass");
}

function clearSession() {
  localStorage.removeItem(SESSION_USER_KEY);
  localStorage.removeItem(SESSION_TOKEN_KEY);
  localStorage.removeItem(SESSION_UNTIL_KEY);
  localStorage.removeItem(SESSION_ROLE_KEY);
  localStorage.removeItem(SESSION_PASS_KEY);
  localStorage.removeItem(SESSION_IMPERSONATING_KEY);
  sessionStorage.removeItem("ct_user");
  sessionStorage.removeItem("ct_pass");
  sessionToken = "";
  currentUser = null;
  isImpersonating = false;
  impersonatedBy = null;
  allowedBlocks = null;
  allowedStrategies = null;
}

function loadStoredSession() {
  const until = Number(localStorage.getItem(SESSION_UNTIL_KEY) || 0);
  if (!until || Date.now() > until) {
    clearSession();
    return null;
  }
  const user = localStorage.getItem(SESSION_USER_KEY);
  const token = localStorage.getItem(SESSION_TOKEN_KEY);
  if (user && token) {
    return { user, token, until, role: localStorage.getItem(SESSION_ROLE_KEY) || "" };
  }
  // Legacy password session → force re-login
  clearSession();
  return null;
}

function applyStoredCreds(session) {
  creds.user = session.user;
  creds.pass = "";
  sessionToken = session.token || "";
  if ($("login-user")) $("login-user").value = session.user;
}

async function reloginFromStorage() {
  const session = loadStoredSession();
  if (!session?.token) throw new Error("auth");
  applyStoredCreds(session);
  tokens = { finder: null, strategy: null, grid: null };
  await refreshAuthMe();
}

async function api(bot, path, method = "GET", body = null, retried = false) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json", ...sessionHeader() },
  };
  if (body) opts.body = JSON.stringify(body);
  const proxyPath = path.startsWith("/") ? path.slice(1) : path;
  const res = await fetch(`/api/pair-config/bot-proxy/${bot}/${proxyPath}`, opts);
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
  const opts = { method, headers: { ...sessionHeader() } };
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

async function refreshAuthMe() {
  const me = await pairConfigApi("/auth/me");
  currentUser = me.user || me;
  if (me.token) sessionToken = me.token;
  applyAuthMeState(me);
  updateMultiUserUi(me);
  return me;
}

function applyAuthMeState(me) {
  isImpersonating = !!me?.impersonating;
  impersonatedBy = me?.impersonated_by || null;
  // Prefer explicit kind from /auth/me or user; never fall back to stale "live"
  // when the payload omitted the field (that made Demo→Live no-op via disabled btn).
  const nextKind =
    me?.account_kind ||
    me?.user?.account_kind ||
    currentUser?.account_kind ||
    null;
  if (nextKind) accountKind = String(nextKind);
  demoMirrorAdmin = false;
  settingsReadonly = !!(me?.settings_readonly);
  canSwitchDemo = false;
  canSwitchLive = false;
  linkedAccountUsername = null;
  hasLiveSecrets = !!(me?.has_live_secrets ?? me?.secrets_dual?.has_live_secrets ?? me?.secrets?.has_secrets);
  hasDemoSecrets = !!(me?.has_demo_secrets ?? me?.secrets_dual?.has_demo_secrets);
  if (me?.trade_on_main) tradeOnMainPref = String(me.trade_on_main);
  if (me?.trade_on_test) tradeOnTestPref = String(me.trade_on_test);
  if (me?.trade_on) tradeOnPref = String(me.trade_on);
  else tradeOnPref = tradeOnMainPref;
  tradingFlagsLive = me?.trading_flags_live || me?.trading_flags || tradingFlagsLive;
  tradingFlagsDemo = me?.trading_flags_demo || tradingFlagsDemo;
  // null = unrestricted; [] = none. Only update when the field is present.
  if (me && "allowed_blocks" in me) {
    allowedBlocks = me.allowed_blocks;
  } else if (me?.user && "allowed_blocks" in me.user) {
    allowedBlocks = me.user.allowed_blocks;
  }
  if (me && "allowed_strategies" in me) {
    allowedStrategies = me.allowed_strategies;
  } else if (me?.user && "allowed_strategies" in me.user) {
    allowedStrategies = me.user.allowed_strategies;
  }
  if (me?.permission_catalog) permissionCatalog = me.permission_catalog;
  sharedStackMode = !!(me?.shared_stack?.mode === "shared" || me?.shared_stack?.signal_engine);
  sharedStackStatus = me?.shared_stack || sharedStackStatus;
  tradingFlags = me?.trading_flags || tradingFlags;
  if (me?.bots_control) applyBotControlSnapshot(me.bots_control);
  if (sharedStackMode) paintSharedStrategyState();
  if (currentUser?.role) localStorage.setItem(SESSION_ROLE_KEY, currentUser.role);
  localStorage.setItem(SESSION_IMPERSONATING_KEY, isImpersonating ? "1" : "0");
  if (sessionToken && !botsEventsAbort) startBotsEventsStream();
}

const RETIRED_BLOCKS = new Set(["finder", "grid", "bybitgrid", "llm", "cursor50", "htf1h"]);
// cursor50 / 1h / plain cursor / llm panels stay retired via RETIRED_BLOCKS.

function userMayUseBlock(blockId) {
  if (blockId === "grid" && isAdminUser()) return true;
  if (RETIRED_BLOCKS.has(blockId)) return false;
  if (allowedBlocks == null) return true;
  return allowedBlocks.includes(blockId);
}

function applyBlockVisibility() {
  document.querySelectorAll("[data-block]").forEach((el) => {
    const block = el.getAttribute("data-block");
    if (!block) return;
    el.classList.toggle("hidden", !userMayUseBlock(block));
  });
  const adminHome = isAdminUser();
  // Test + Main panels stay visible whenever the block is allowed
  // (Demo keys still required to actually trade test strategies).
  const testPanel = $("panel-test-strategies");
  if (testPanel) {
    testPanel.classList.toggle("hidden", !userMayUseBlock("test_strategies"));
  }
  const strategyPanel = $("panel-strategy");
  if (strategyPanel) {
    strategyPanel.classList.toggle("hidden", !userMayUseBlock("strategy"));
  }
  const demoWrap = $("strategy-demo-wrap");
  if (demoWrap) demoWrap.classList.toggle("hidden", !(hasDemoSecrets || adminHome));
  const liveWrap = $("strategy-live-wrap");
  if (liveWrap) {
    // Always show Live list when main panel is allowed; empty-keys banner covers missing keys.
    liveWrap.classList.toggle("hidden", false);
  }
  const gridVisible = userMayUseBlock("grid");
  document
    .querySelectorAll('[data-history-scope="grid"], [data-pnl-dash-scope="grid"], [data-charts-bot="grid"]')
    .forEach((el) => {
      el.classList.toggle("hidden", !gridVisible);
    });
}

function updateMultiUserUi(me) {
  const role = me?.user?.role || me?.role || currentUser?.role || "";
  const secrets = me?.secrets;
  const banner = $("secrets-banner");
  if (banner) {
    // Show for tenants (incl. while admin impersonates) when secrets missing.
    // Treat profiles / key flags as present so a transient has_secrets=false
    // (decrypt race, Bybit probe) does not flash the banner.
    const uid = String(me?.user?.id || currentUser?.id || "");
    const profiles = Array.isArray(secrets?.profiles) ? secrets.profiles : [];
    const keysPresent =
      hasLiveSecrets ||
      hasDemoSecrets ||
      (!!secrets &&
        (secrets.has_secrets === true ||
          profiles.length > 0 ||
          !!secrets.has_bybit_api_key ||
          !!secrets.has_bybit_api_secret));
    if (uid && keysPresent) {
      window.__ctSecretsOk = window.__ctSecretsOk || new Set();
      window.__ctSecretsOk.add(uid);
    }
    const stickyOk = !!(uid && window.__ctSecretsOk && window.__ctSecretsOk.has(uid));
    const showSecretsBanner =
      (role !== "admin" || isImpersonating) &&
      !!secrets &&
      secrets.has_secrets === false &&
      !keysPresent &&
      !stickyOk;
    banner.classList.toggle("hidden", !showSecretsBanner);
  }
  const impBanner = $("impersonate-banner");
  const impText = $("impersonate-banner-text");
  if (impBanner) {
    impBanner.classList.toggle("hidden", !isImpersonating);
    if (impText && isImpersonating) {
      const uname = currentUser?.username || currentUser?.id || "пользователь";
      const by = impersonatedBy?.username || "админ";
      impText.textContent = `Сессия: ${uname} (вход от ${by})`;
    }
  }
  // Live↔Demo account switch removed — dual API keys on one login.

  document.querySelectorAll("[data-admin-only]").forEach((el) => {
    el.classList.toggle("hidden", !isAdminUser());
  });
  paintTestStrategyScanActions();
  // Placement lives under main strategies settings.
  document.querySelectorAll("[data-placement-manage]").forEach((el) => {
    const show = isAdminUser() || userMayUseBlock("strategy");
    el.classList.toggle("hidden", !show);
  });
  // Demo mirror: hide secrets / telegram / trading toggles (read-only view).
  document.querySelectorAll("[data-user-secrets]").forEach((el) => {
    el.classList.toggle("hidden", settingsReadonly);
  });
  const tgSection = $("telegram-settings-section");
  if (tgSection) tgSection.classList.toggle("hidden", settingsReadonly);
  const tradingSection = $("trading-toggle-section");
  if (tradingSection) tradingSection.classList.toggle("hidden", settingsReadonly);
  document.body.classList.toggle("demo-readonly", settingsReadonly);
  const dual = me?.secrets_dual;
  const statusEl = $("secrets-status");
  if (statusEl) {
    const fmt = (v) => (v === "all" ? "все" : v === "binance" ? "Binance" : "Bybit");
    const bybitOk = !!(dual?.exchanges?.bybit?.has_live_secrets || dual?.has_live_secrets);
    const binanceOk = !!dual?.exchanges?.binance?.has_live_secrets;
    const parts = [];
    if (bybitOk) parts.push("Bybit");
    if (binanceOk) parts.push("Binance");
    const keysLabel = parts.length ? `ключи: ${parts.join(", ")}` : "ключи не заданы";
    statusEl.textContent = `Тест: ${fmt(tradeOnTestPref)} · Основные: ${fmt(tradeOnMainPref)} · ${keysLabel}`;
    statusEl.classList.remove("secrets-status-warn");
  }
  const mainSel = $("trade-on-main-select");
  if (mainSel) mainSel.value = tradeOnMainPref || "bybit";
  const testSel = $("trade-on-test-select");
  if (testSel) testSel.value = tradeOnTestPref || "bybit";
  const paintSlot = (exchange, mode, st) => {
    const el = $(`secrets-${exchange}-${mode}-status`);
    const chip = $(`secrets-${exchange}-${mode}-chip`);
    const has = !!st?.has_secrets;
    const warn = has && st.can_trade === false && st.trade_block_reason;
    const stateClass = warn ? "is-warn" : has ? "is-ok" : "is-missing";
    if (el) {
      el.textContent = warn
        ? `⚠ ${st.trade_block_reason}`
        : has
          ? "Заданы"
          : "Не заданы";
      el.classList.remove("is-ok", "is-warn", "is-missing");
      el.classList.add(stateClass);
    }
    if (chip) {
      chip.classList.remove("is-ok", "is-warn", "is-missing");
      chip.classList.add(stateClass);
      chip.title = warn
        ? String(st.trade_block_reason || "")
        : has
          ? `${mode === "demo" ? "Demo" : "Live"}: ключи заданы`
          : `${mode === "demo" ? "Demo" : "Live"}: ключи не заданы`;
    }
  };
  for (const ex of ["bybit", "binance"]) {
    const block = dual?.exchanges?.[ex] || (ex === "bybit" ? dual : null);
    paintSlot(ex, "live", block?.live || (ex === "bybit" ? dual?.live : null));
    paintSlot(ex, "demo", block?.demo || (ex === "bybit" ? dual?.demo : null));
  }
  // Auto-open exchange card that already has any keys (keeps empty Binance collapsed).
  document.querySelectorAll(".secrets-ex-card[data-exchange]").forEach((card) => {
    const ex = card.getAttribute("data-exchange");
    const block = dual?.exchanges?.[ex] || (ex === "bybit" ? dual : null);
    const hasAny = !!(block?.has_live_secrets || block?.has_demo_secrets
      || (ex === "bybit" && (dual?.has_live_secrets || dual?.has_demo_secrets)));
    if (hasAny && !card.dataset.userToggled) card.open = true;
  });
  // Legacy single-slot status elements (hidden)
  const liveSt = dual?.live || (secrets?.demo_trading ? null : secrets);
  const demoSt = dual?.demo;
  const liveStatusEl = $("secrets-live-status");
  if (liveStatusEl) {
    liveStatusEl.textContent = liveSt?.has_secrets ? "Ключи заданы" : "Ключи не заданы";
  }
  const demoStatusEl = $("secrets-demo-status");
  if (demoStatusEl) {
    demoStatusEl.textContent = demoSt?.has_secrets ? "Ключи заданы" : "Ключи не заданы";
  }
  renderSecretsProfiles(secrets);
  applyBlockVisibility();
  syncRatingUserFilterVisibility();
}

function renderSecretsProfiles(secrets) {
  const box = $("secrets-profiles");
  const tradeOn = $("secrets-trade-on");
  const tradeOpts = $("secrets-trade-on-options");
  if (!box) return;
  const profiles = Array.isArray(secrets?.profiles) ? secrets.profiles : [];
  if (!profiles.length) {
    box.innerHTML = `<p class="muted secrets-profiles-empty">Сохранённых ключей пока нет</p>`;
    if (tradeOn) tradeOn.hidden = true;
    if (tradeOpts) tradeOpts.innerHTML = "";
    return;
  }
  if (tradeOn) tradeOn.hidden = false;
  if (tradeOpts) {
    tradeOpts.innerHTML = profiles
      .map((p) => {
        const mode = p.mode_label || (p.demo_trading ? "Demo" : "Live");
        const label = `${p.name || mode} · ${mode}`;
        return `<label class="secrets-trade-option${p.active ? " is-active" : ""}">
          <input type="radio" name="secrets-trade-profile" value="${escapeHtml(p.id)}" ${p.active ? "checked" : ""} />
          <span class="secrets-trade-option-text">
            <strong>${escapeHtml(p.name || mode)}</strong>
            <span class="muted">${escapeHtml(mode)} · ${escapeHtml(p.api_key || "—")}</span>
          </span>
        </label>`;
      })
      .join("");
    tradeOpts.querySelectorAll('input[name="secrets-trade-profile"]').forEach((input) => {
      input.addEventListener("change", () => {
        if (!input.checked) return;
        activateSecretsProfile(input.value).catch(() => {});
      });
    });
  }
  box.innerHTML = profiles
    .map((p) => {
      const mode = p.mode_label || (p.demo_trading ? "Demo" : "Live");
      const activeBadge = p.active
        ? `<span class="secrets-profile-badge active">торгуем здесь</span>`
        : `<span class="secrets-profile-badge">${escapeHtml(mode)}</span>`;
      return `<div class="secrets-profile-card${p.active ? " is-active" : ""}" data-profile-id="${escapeHtml(p.id)}">
        <div class="secrets-profile-head">
          <strong class="secrets-profile-name">${escapeHtml(p.name || mode)}</strong>
          ${activeBadge}
        </div>
        <div class="secrets-profile-body">
          <div><span class="muted">Тип</span> ${escapeHtml(mode)}</div>
          <div><span class="muted">API Key</span> <code>${escapeHtml(p.api_key || "—")}</code></div>
          <div><span class="muted">API Secret</span> <code>${escapeHtml(p.api_secret || "—")}</code></div>
        </div>
        <div class="secrets-profile-actions">
          ${
            p.active
              ? ""
              : `<button type="button" class="btn btn-sm primary" data-secrets-activate="${escapeHtml(p.id)}">Торговать на этих</button>`
          }
          <button type="button" class="btn btn-sm danger" data-secrets-delete="${escapeHtml(p.id)}">Удалить</button>
        </div>
      </div>`;
    })
    .join("");
  box.querySelectorAll("[data-secrets-delete]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-secrets-delete");
      if (!id) return;
      if (!confirm("Удалить эти ключи?")) return;
      deleteSecretsProfile(id).catch(() => {});
    });
  });
  box.querySelectorAll("[data-secrets-activate]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-secrets-activate");
      if (!id) return;
      activateSecretsProfile(id).catch(() => {});
    });
  });
}

async function deleteSecretsProfile(profileId) {
  try {
    const payload = { profile_id: profileId };
    if (isAdminUser() && !isImpersonating) payload.user_id = "admin";
    await pairConfigApi(`/secrets?profile_id=${encodeURIComponent(profileId)}`, "DELETE", payload);
    showSecretsMsg("Ключи удалены");
    await refreshAuthMe();
  } catch (e) {
    showSecretsMsg(formatApiError(e.message), true);
  }
}

async function activateSecretsProfile(profileId) {
  try {
    const payload = { profile_id: profileId };
    if (isAdminUser() && !isImpersonating) payload.user_id = "admin";
    await pairConfigApi("/secrets/activate", "POST", payload);
    showSecretsMsg("Торговля переключена на выбранный аккаунт");
    await refreshAuthMe();
  } catch (e) {
    showSecretsMsg(formatApiError(e.message), true);
  }
}

async function loginBot(_bot) {
  /* Bot APIs go through pair-config proxy with session token — no per-bot JWT. */
}

async function loginAll() {
  // Resume from stored session token
  if (sessionToken && !creds.pass) {
    tokens = { finder: "proxy", strategy: "proxy", grid: "proxy" };
    await refreshAuthMe();
    saveSession();
    return;
  }

  const username = creds.user || $("login-user")?.value.trim() || "";
  const password = creds.pass || $("login-pass")?.value || "";

  // Explicit login/password wins; otherwise try Telegram Mini App silent auth
  if (!username || !password) {
    if (await tryTelegramWebAppLogin()) {
      return;
    }
    throw new Error("auth");
  }

  const res = await fetch("/api/pair-config/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    if (res.status === 401) throw new Error("auth");
    const t = await res.text();
    throw new Error(t || "login_failed");
  }
  const data = await res.json();
  sessionToken = data.token || "";
  currentUser = data.user || { username, role: data.role };
  creds.user = currentUser.username || username;
  creds.pass = "";
  tokens = { finder: "proxy", strategy: "proxy", grid: "proxy" };
  saveSession();
  try {
    await refreshAuthMe();
  } catch {
    updateMultiUserUi({ user: currentUser });
  }
}

async function tryTelegramWebAppLogin() {
  const tg = window.Telegram?.WebApp;
  const initData = tg?.initData;
  if (!initData) return false;
  try {
    const res = await fetch("/api/pair-config/auth/telegram-webapp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ init_data: initData }),
    });
    if (!res.ok) {
      let msg = "Не удалось войти через Telegram";
      try {
        const err = await res.json();
        if (err?.message) msg = String(err.message);
        else if (err?.error === "telegram_not_linked") {
          msg = "Сначала войдите логином и привяжите Telegram в Настройках";
        }
      } catch {
        /* keep fallback */
      }
      const errEl = $("login-error");
      if (errEl) {
        errEl.textContent = msg;
        errEl.classList.remove("hidden");
      }
      return false;
    }
    const data = await res.json();
    if (!data.token) return false;
    sessionToken = data.token || "";
    currentUser = data.user || {};
    creds.user = currentUser.username || "";
    creds.pass = "";
    tokens = { finder: "proxy", strategy: "proxy", grid: "proxy" };
    saveSession();
    try {
      await refreshAuthMe();
    } catch {
      updateMultiUserUi({ user: currentUser });
    }
    return true;
  } catch {
    return false;
  }
}

function showTelegramLoginHint() {
  const hint = $("login-tg-hint");
  if (!hint) return;
  if (window.Telegram?.WebApp?.initData) hint.classList.remove("hidden");
  else hint.classList.add("hidden");
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

function isBookTrade(trade) {
  const tag = String(trade?.enter_tag || "");
  const strat = String(trade?.strategy || "");
  return tag.startsWith("Deepseek50") || strat === "Deepseek50";
}

function isLlmStatsRow(row) {
  return String(row?.key || "").startsWith("DeepseekLlmStrategy");
}

function isBookStatsRow(row) {
  return String(row?.key || "").startsWith("Deepseek50");
}

function isCursorStatsRow(row) {
  return String(row?.key || "").startsWith("CursorLlmStrategy");
}

function isCursorBookStatsRow(row) {
  return String(row?.key || "").startsWith("Cursor50");
}

function isDeepseekStatsRow(row) {
  return isLlmStatsRow(row) || isBookStatsRow(row) || isCursorStatsRow(row) || isCursorBookStatsRow(row);
}

function isPriceActionTrade(trade) {
  if (!trade) return false;
  const tag = String(trade.enter_tag || "");
  const strat = String(trade.strategy || "");
  return (
    tag.startsWith("PaTrendPullback") ||
    tag.startsWith("PaLiquiditySweep") ||
    strat === "PaTrendPullback" ||
    strat === "PaLiquiditySweep"
  );
}

function isPriceActionStatsRow(row) {
  if (!row) return false;
  return isPriceActionTrade({ enter_tag: row.enter_tag || row.source_id || row.id, strategy: row.strategy });
}

function isRetiredTrade(trade) {
  return (
    isLlmTrade(trade) ||
    isBookTrade(trade) ||
    isCursorTrade(trade) ||
    isCursorBookTrade(trade) ||
    isPriceActionTrade(trade)
  );
}

function isRetiredStatsRow(row) {
  return isDeepseekStatsRow(row) || isPriceActionStatsRow(row);
}

function tradeSourceLabel(trade, bot) {
  if (bot === "finder") return FINDER_STATS_LABEL;
  if (bot === "grid") return GRID_STATS_LABEL;
  if (isLlmTrade(trade)) return LLM_STATS_LABEL;
  if (isBookTrade(trade)) return LLM50_STATS_LABEL;
  if (isCursorTrade(trade)) return CURSOR_STATS_LABEL;
  if (isCursorBookTrade(trade)) return CURSOR50_STATS_LABEL;
  if (isPriceActionTrade(trade)) return "Price Action";
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
    time_exit: "По времени (прибыль)",
    time_exit_loss: "По времени (убыток)",
  };
  return map[reason] || reason || "—";
}

function fmtTradeDate(trade) {
  const raw = trade.close_date || trade.close_fill_date;
  if (!raw) return "—";
  const d = parseAppInstant(raw);
  if (!d) return String(raw).slice(0, 16);
  return fmtAppDateShort(d);
}

/** Bybit USDT-perp taker fee — одинакова в % на вход и выход (~0.1%). */
const BYBIT_TAKER_FEE_RATE = 0.001;

function closedTradePnl(trade) {
  const abs = Number(trade.close_profit_abs ?? trade.profit_abs ?? trade.realized_profit ?? 0);
  const pct = Number(
    trade.close_profit_pct ?? trade.profit_pct ?? trade.realized_profit_ratio ?? 0
  );
  const pctDisplay = Math.abs(pct) <= 1 && Math.abs(pct) > 0 ? pct * 100 : pct;
  return { abs, pct: pctDisplay };
}

function tradeRoundTripFees(trade) {
  const fee = BYBIT_TAKER_FEE_RATE;
  const amount = Number(trade?.amount || 0);
  const open = Number(trade?.open_rate || 0);
  const close = Number(trade?.close_rate || 0);
  if (amount > 0 && open > 0) {
    const closePx = close > 0 ? close : open;
    return amount * open * fee + amount * closePx * fee;
  }
  const stake = Number(trade?.stake_amount || 0);
  const lev = Number(trade?.leverage || 1) || 1;
  if (stake > 0) return 2 * stake * lev * fee;
  return 0;
}

/** PnL после round-trip комиссии (0.1% × 2). */
function closedTradePnlNet(trade) {
  const { abs, pct } = closedTradePnl(trade);
  const fees = tradeRoundTripFees(trade);
  const net = abs - fees;
  const stake = Number(trade?.stake_amount || 0);
  const netPct = stake > 0 ? (net / stake) * 100 : pct;
  return { abs: net, pct: netPct, fees, gross: abs, grossPct: pct };
}

function historyEntryGrossPnl(entry) {
  if (entry.kind === "bybit") return Number(entry.item.realised_pnl ?? entry.item.pnl ?? 0);
  return closedTradePnl(entry.trade).abs;
}

function historyEntryNetPnl(entry) {
  // Bybit Grid already reports realised PnL net of fees.
  if (entry.kind === "bybit") return Number(entry.item.realised_pnl ?? entry.item.pnl ?? 0);
  return closedTradePnlNet(entry.trade).abs;
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

function tradeMlMinConfidence(trade) {
  const m = tradeMlMeta(trade);
  if (!m || m.ml_min_confidence == null) return null;
  const n = Number(m.ml_min_confidence);
  return Number.isFinite(n) ? n : null;
}

function fmtMlConfidence(trade) {
  const conf = tradeMlConfidence(trade);
  if (conf == null) return "—";
  return `${(conf * 100).toFixed(0)}%`;
}

function fmtMlConfidenceDetail(trade) {
  const m = tradeMlMeta(trade);
  if (!m) return "нет данных";
  const main = fmtMlConfidence(trade);
  if (main === "—") return "нет данных";
  const pred = m.ml_predicted ? String(m.ml_predicted) : "";
  const minC = tradeMlMinConfidence(trade);
  const parts = [`факт. ${main}`];
  if (pred) parts.push(pred);
  if (minC != null) parts.push(`порог ${(minC * 100).toFixed(0)}%`);
  return parts.join(" · ");
}

function tradeMlSummaryLabel(trade) {
  const conf = tradeMlConfidence(trade);
  if (conf == null) return "";
  const minC = tradeMlMinConfidence(trade);
  const minNote = minC != null ? `≥${(minC * 100).toFixed(0)}%` : "";
  return minNote ? ` · ML ${(conf * 100).toFixed(0)}% (${minNote})` : ` · ML ${(conf * 100).toFixed(0)}%`;
}

function invalidateWhitelistCache(bot = null) {
  if (bot) delete whitelistCache[bot];
  else Object.keys(whitelistCache).forEach((k) => delete whitelistCache[k]);
}

function tradeRowId(trade) {
  const raw = trade?.trade_id ?? trade?.id;
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : null;
}

function mergeTradeMlMeta(trades, metaById) {
  if (!trades?.length || !metaById || !Object.keys(metaById).length) return trades || [];
  return trades.map((t) => {
    const tid = tradeRowId(t);
    const m = tid != null ? metaById[String(tid)] : null;
    if (!m) return t;
    const row = tid != null && t.trade_id == null ? { ...t, trade_id: tid } : { ...t };
    return { ...row, ml_meta: { ...(row.ml_meta || {}), ...m } };
  });
}

/** Keep previously loaded ML badges when a refresh returns trades without ml_meta. */
function preserveTradeMlMeta(prevTrades, nextTrades) {
  if (!nextTrades?.length) return nextTrades || [];
  if (!prevTrades?.length) return nextTrades;
  const prevById = new Map();
  for (const t of prevTrades) {
    const id = tradeRowId(t);
    if (id != null && t.ml_meta && typeof t.ml_meta === "object") {
      prevById.set(id, t.ml_meta);
    }
  }
  if (!prevById.size) return nextTrades;
  return nextTrades.map((t) => {
    const id = tradeRowId(t);
    if (id == null) return t;
    const prevMeta = prevById.get(id);
    if (!prevMeta) return t;
    const existing = t.ml_meta && typeof t.ml_meta === "object" ? t.ml_meta : {};
    const hasConf =
      existing.ml_confidence != null || existing.ml_gate_confidence != null;
    if (hasConf) return t;
    const row = t.trade_id == null ? { ...t, trade_id: id } : { ...t };
    return { ...row, ml_meta: { ...prevMeta, ...existing } };
  });
}

function withPreservedMlMeta(bot, trades) {
  const prev = botLiveCache[bot]?.trades;
  return preserveTradeMlMeta(prev, trades);
}

function tradesNeedMlEnrich(trades) {
  return (trades || []).some((t) => {
    const m = t?.ml_meta;
    if (!m || typeof m !== "object") return true;
    return m.ml_confidence == null && m.ml_gate_confidence == null;
  });
}

async function enrichTradesWithMl(bot, trades) {
  if (!trades?.length) return trades || [];
  const ids = trades.map((t) => tradeRowId(t)).filter((id) => id != null);
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
  const m = tradeMlMeta(trade);
  const pred = m?.ml_predicted ? ` · ${m.ml_predicted}` : "";
  const minC = tradeMlMinConfidence(trade);
  const minNote = minC != null ? ` · порог ${(minC * 100).toFixed(0)}%` : "";
  if (conf == null) {
    return `<span class="trade-ml-badge is-missing" title="Фактическая уверенность ML при входе недоступна">ML —</span>`;
  }
  return `<span class="trade-ml-badge" title="Фактическая уверенность ML при входе${pred}${minNote}">ML ${(conf * 100).toFixed(0)}%</span>`;
}

const STATS_SCOPE_META = {
  all: {
    title: "Статистика — все боты",
    note: "Все закрытые сделки по всем ботам (включая выключенные стратегии и остановленные боты) и Bybit Grid.",
  },
  finder: { title: "Статистика — ML Finder", note: "Сделки TradeFinderStrategy (XGBoost scanner + pnl gate), даже если Finder сейчас выключен." },
  strategy: {
    title: "Статистика — стратегии + ML gate",
    note: "Закрытые сделки основных стратегий по enter_tag (без тестового блока и 1-часовых). Входы с ML gate.",
  },
  htf1h: {
    title: "Статистика — 1-часовые",
    note: "Только закрытые сделки 1-часовых стратегий. В основные и тестовые они не входят.",
  },
  test_strategy: {
    title: "Статистика — тестовые стратегии",
    note: "Сделки стратегий из панели «Тестовые» (перенос в Настройках). Тот же strategy-бот; инв/хедж считаются отдельно.",
  },
  grid: {
    title: "Статистика — Grid · Demo",
    note: "Закрытые сделки Grid на Demo-счёте администратора, даже если бот остановлен.",
  },
  bybitgrid: { title: "Статистика — Bybit Grid", note: "Закрытые нативные grid-боты на бирже Bybit." },
};

let pairlistMode = "scanner";
/** @deprecated control-plane Start/Stop is the source of truth; always treat as allowed. */
let finderBotEnabled = true;
let sharedStackMode = false;
let sharedStackStatus = {};
let tradingFlags = { strategy: true, grid: false, finder: false };
let lastAuthMeRefresh = 0;
/** @type {Record<string, {desired?: boolean, status?: string, observed?: object}>} */
let botsControl = {};
let botsEventsAbort = null;
let botsEventsLastId = 0;

const CONTROL_STATUS_LABEL = {
  OFF: "STOPPED",
  STARTING: "STARTING…",
  RUNNING: "RUNNING",
  STOPPING: "STOPPING…",
  ERROR: "ERROR",
};

function controlStatusOf(bot) {
  const row = botsControl[bot];
  return String(row?.status || "").toUpperCase() || "";
}

function controlRunning(bot) {
  const st = controlStatusOf(bot);
  return st === "RUNNING" || st === "STARTING" || st === "STOPPING";
}

function applyBotControlSnapshot(bots) {
  if (!bots || typeof bots !== "object") return;
  botsControl = { ...botsControl, ...bots };
  for (const bot of Object.keys(BOTS)) {
    if (!bots[bot]) continue;
    paintControlState(bot);
  }
}

function paintControlState(bot) {
  const st = controlStatusOf(bot);
  if (!st) return;
  const running = st === "RUNNING" || st === "STARTING";
  let hint = "";
  if (st === "STARTING" || st === "STOPPING") hint = "sync";
  else if (st === "ERROR") hint = "offline";
  // Shared strategy: only mark offline if BOTH signal-engine and executor are down.
  // A brief executor restart must not flip the badge to STOPPED every few seconds.
  if (bot === "strategy" && sharedStackMode && st === "RUNNING") {
    const sig = sharedStackStatus.signal_engine || {};
    const exe = sharedStackStatus.trade_executor || {};
    if (sig.active === false && exe.active === false) hint = "offline";
  }
  setState(bot, running && hint !== "offline", hint === "sync" ? "sync" : hint === "offline" ? "offline" : st);
  renderActions(bot, st === "RUNNING" || st === "STARTING");
  rememberBotLive(bot, { running: st === "RUNNING", controlStatus: st });
}

function applyControlEvent(ev) {
  if (!ev || typeof ev !== "object") return;
  if (ev.id) botsEventsLastId = Math.max(botsEventsLastId, Number(ev.id) || 0);
  if (ev.type === "bot.snapshot" && ev.payload?.bots) {
    applyBotControlSnapshot(ev.payload.bots);
    return;
  }
  if (ev.type === "bot.status" && ev.payload?.bot) {
    const bot = ev.payload.bot;
    botsControl[bot] = ev.payload;
    if (typeof ev.payload.desired === "boolean") {
      tradingFlags = { ...tradingFlags, [bot]: !!ev.payload.desired };
    }
    paintControlState(bot);
    if (bot === "strategy") paintSharedStrategyState();
  }
}

async function startBotsEventsStream() {
  if (botsEventsAbort) {
    try {
      botsEventsAbort.abort();
    } catch {
      /* ignore */
    }
  }
  const ac = new AbortController();
  botsEventsAbort = ac;
  const headers = { ...sessionHeader(), Accept: "text/event-stream" };
  try {
    const res = await fetch(`/api/pair-config/bots/events?after=${botsEventsLastId}`, {
      headers,
      signal: ac.signal,
    });
    if (!res.ok || !res.body) return;
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let eventName = "message";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n");
      buf = parts.pop() || "";
      for (const line of parts) {
        if (line.startsWith("event:")) {
          eventName = line.slice(6).trim();
          continue;
        }
        if (line.startsWith("data:")) {
          const raw = line.slice(5).trim();
          try {
            const ev = JSON.parse(raw);
            if (!ev.type) ev.type = eventName;
            applyControlEvent(ev);
          } catch {
            /* ignore */
          }
          eventName = "message";
        }
      }
    }
  } catch (e) {
    if (e?.name === "AbortError") return;
  } finally {
    if (botsEventsAbort === ac) botsEventsAbort = null;
  }
  // reconnect
  setTimeout(() => {
    if (sessionToken) startBotsEventsStream();
  }, 2500);
}

async function setBotEnabled(bot, enabled, accountMode = "live") {
  // Optimistic
  botsControl[bot] = {
    ...(botsControl[bot] || {}),
    bot,
    desired: !!enabled,
    enabled: !!enabled,
    status: enabled ? "STARTING" : "STOPPING",
  };
  tradingFlags = { ...tradingFlags, [bot]: !!enabled };
  if (accountMode === "demo") {
    tradingFlagsDemo = { ...tradingFlagsDemo, [bot]: !!enabled };
  } else {
    tradingFlagsLive = { ...tradingFlagsLive, [bot]: !!enabled };
  }
  paintControlState(bot);
  const snap = await pairConfigApi(`/bots/${bot}`, "PATCH", {
    enabled: !!enabled,
    account_mode: accountMode === "demo" ? "demo" : "live",
  });
  if (snap?.bot || snap?.desired != null) {
    botsControl[bot] = { ...(botsControl[bot] || {}), ...snap, bot };
    paintControlState(bot);
  }
  return snap;
}

function strategyRunningInSharedMode() {
  const st = controlStatusOf("strategy");
  if (st === "RUNNING" || st === "STARTING") return true;
  if (st === "OFF" || st === "STOPPING" || st === "ERROR") return false;
  const sig = sharedStackStatus.signal_engine || {};
  const exe = sharedStackStatus.trade_executor || {};
  return !!(sig.active && exe.active && tradingFlags.strategy);
}

function sharedStrategyStateHint() {
  const sig = sharedStackStatus.signal_engine || {};
  const exe = sharedStackStatus.trade_executor || {};
  // Only offline when both are known-down (avoid flap on one unit restart).
  if (sig.active === false && exe.active === false) return "offline";
  return "";
}

function paintSharedStrategyState() {
  if (!sharedStackMode) return;
  const running = strategyRunningInSharedMode();
  const hint = sharedStrategyStateHint();
  setState("strategy", running, hint);
  const snap = botLiveCache.strategy || {};
  paintBotLive("strategy", {
    config: snap.config,
    profit: snap.profit,
    balance: snap.balance,
    trades: snap.trades || [],
    running,
    stateHint: hint,
  });
}

function isAllVolumePairlist() {
  return pairlistMode === "all_volume";
}

async function refreshPairlistMode() {
  try {
    const d = await pairConfigApi("/pairs");
    pairlistMode = d._pairlist_mode || "scanner";
    finderBotEnabled = true;
  } catch {
    /* keep previous */
  }
}

let statsScope = "all";
let statsPeriod = "all";
let statsDateFrom = "";
let statsDateTo = "";
let statsDataCache = null;

let historyScope = "all";
let historyPeriod = "all";
let historyDateFrom = "";
let historyDateTo = "";
let historyStrategyFilter = "all";
let historyPairFilter = "";
let historyDataCache = null;
let historyPage = 1;
const HISTORY_PAGE_SIZE = 50;
/** 0 = all closed trades (server hard-cap). */
const CLOSED_TRADES_FETCH_LIMIT = 0;

let pnlDashScope = "all";
let pnlDashTf = "5m";
let pnlDashPeriod = "all";
let pnlDashDateFrom = "";
let pnlDashDateTo = "";
let pnlDashSide = "all"; // all | long | short
let pnlDashDataCache = null;

const PNL_DASH_COLORS = [
  "#22c55e",
  "#3b82f6",
  "#f59e0b",
  "#8b5cf6",
  "#ec4899",
  "#14b8a6",
  "#f97316",
  "#6366f1",
  "#84cc16",
  "#d946ef",
  "#0ea5e9",
  "#eab308",
  "#a855f7",
  "#ef4444",
  "#06b6d4",
  "#65a30d",
  "#ea580c",
  "#2563eb",
  "#c026d3",
  "#db2777",
];
const PNL_DASH_OTHER_COLOR = "#64748b";
const PNL_DASH_MAX_SLICES = 25;
const PNL_DASH_MIN_PCT = 0.5;

const HISTORY_EXPORT_COLUMNS = [
  { id: "close_date", label: "Дата закрытия", default: true },
  { id: "open_date", label: "Дата открытия", default: false },
  { id: "bot", label: "Бот", default: true },
  { id: "pair", label: "Пара", default: true },
  { id: "side", label: "Сторона", default: true },
  { id: "strategy", label: "Стратегия", default: true },
  { id: "sl", label: "SL %", default: true },
  { id: "tp", label: "TP %", default: true },
  { id: "ml", label: "ML %", default: true },
  { id: "pnl_usdt", label: "PnL USDT", default: true },
  { id: "pnl_pct", label: "PnL %", default: true },
  { id: "pnl_net_usdt", label: "С комиссией USDT", default: true },
  { id: "pnl_net_pct", label: "С комиссией %", default: false },
  { id: "max_profit_usdt", label: "Макс. прибыль USDT", default: true },
  { id: "max_profit_pct", label: "Макс. прибыль %", default: false },
  { id: "max_loss_usdt", label: "Макс. убыток USDT", default: true },
  { id: "max_loss_pct", label: "Макс. убыток %", default: false },
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
      // Bot API timestamps: milliseconds; older payloads may use seconds
      return ts < 1e12 ? ts * 1000 : ts;
    }
  }
  const raw = trade.close_date || trade.close_fill_date;
  if (!raw) return 0;
  const d = parseAppInstant(raw);
  return d ? d.getTime() : 0;
}

function bybitCloseMs(item) {
  if (!item?.closed_at) return 0;
  const d = parseAppInstant(item.closed_at);
  return d ? d.getTime() : 0;
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

const STATS_PERIODS = new Set(["today", "yesterday", "7d", "30d", "all"]);

const STATS_PERIOD_LABELS = {
  today: "за сегодня",
  yesterday: "за вчера",
  "7d": "за неделю",
  "30d": "за месяц",
  all: "за всё время",
};

function normalizeStatsPeriod(period) {
  return STATS_PERIODS.has(period) ? period : "all";
}

function formatCustomPeriodLabel(dateFrom, dateTo) {
  const fmt = (iso) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ""));
    return m ? `${m[3]}.${m[2]}.${m[1]}` : "";
  };
  const from = fmt(dateFrom);
  const to = fmt(dateTo);
  if (from && to) return `${from} — ${to}`;
  if (from) return `с ${from}`;
  if (to) return `по ${to}`;
  return "";
}

function statsPeriodLabel(period, dateFrom = "", dateTo = "") {
  return formatCustomPeriodLabel(dateFrom, dateTo) || STATS_PERIOD_LABELS[period] || STATS_PERIOD_LABELS.all;
}

function syncDateInputs(fromId, toId, fromValue, toValue) {
  const from = $(fromId);
  const to = $(toId);
  if (from && from.value !== fromValue) from.value = fromValue;
  if (to && to.value !== toValue) to.value = toValue;
}

function periodBounds(period, dateFrom, dateTo) {
  if (dateFrom || dateTo) {
    return appDateInputBoundsMs(dateFrom, dateTo);
  }
  const nowMs = Date.now();
  const startOfToday = appStartOfDayMs(nowMs);
  const endOfToday = appEndOfDayMs(nowMs);
  switch (period) {
    case "today":
      return { fromMs: startOfToday, toMs: endOfToday };
    case "yesterday":
      return {
        fromMs: startOfToday - 86400000,
        toMs: startOfToday - 1,
      };
    case "7d":
      return { fromMs: nowMs - 7 * 86400000, toMs: Infinity };
    case "30d":
      return { fromMs: nowMs - 30 * 86400000, toMs: Infinity };
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

function normalizeTradeSide(side) {
  const raw = String(side || "all").toLowerCase();
  if (raw === "long" || raw === "short") return raw;
  return "all";
}

function tradeSideLabel(side) {
  const want = normalizeTradeSide(side);
  if (want === "long") return "Лонг";
  if (want === "short") return "Шорт";
  return "Все";
}

function filterTradesBySide(trades, side) {
  const want = normalizeTradeSide(side);
  if (want === "all") return trades || [];
  const short = want === "short";
  return (trades || []).filter((t) => Boolean(t.is_short) === short);
}

function addBybitGridToRow(row, item) {
  const p = Number(item.realised_pnl ?? item.pnl ?? 0);
  row.closed = (row.closed || 0) + 1;
  if (p > 0) {
    row.wins += 1;
    row.profit += p;
  } else if (p < 0) {
    row.losses += 1;
    row.loss += Math.abs(p);
  } else {
    row.flat = (row.flat || 0) + 1;
  }
}

function isBotStatsLabel(name) {
  return (
    name === FINDER_STATS_LABEL ||
    name === GRID_STATS_LABEL ||
    name === BYBIT_GRID_STATS_LABEL
  );
}

function isBirthTestStrategy(s) {
  if (!s) return false;
  if (s.catalog_test_group != null) return !!s.catalog_test_group;
  // Birth test pack: *TestStrategy / explicit test_group / nums 101+
  if (s.test_group || s.testGroup) return true;
  const id = String(s.id || "");
  if (/TestStrategy$/i.test(id)) return true;
  if (id === "GruBarrierStrategy" || id === "OkalaRange8020Strategy") return true;
  const num = Number(s.num);
  return Number.isFinite(num) && num >= 101;
}

function isTestCatalogEntry(s) {
  // Listed on Тестовые toggles (full catalog may show here).
  if (!s) return false;
  if (s.show_on_test === true) return true;
  if (s.ui_panel && s.ui_panel === "hidden") return false;
  return isBirthTestStrategy(s) || strategyUiPanel(s) === "test" || !s.in_main;
}

/** Demo/test lane for trades & stats: not copied into placement.main. */
function isTestLaneStrategy(s) {
  if (!s || isHtfStrategy(s)) return false;
  if (s.ui_panel === "test" || s.ui_panel === "main") return s.ui_panel === "test";
  if (s.in_main != null) return !s.in_main;
  if (s.show_on_main === true) return false;
  return strategyUiPanel(s) === "test" || isBirthTestStrategy(s);
}

function isHtfStrategyId(id) {
  return /1hStrategy(?::inv|:hedge)?$/i.test(String(id || ""));
}

/** Strip :inv / :hedge so panel filters match catalog ids. */
function statsRowBaseId(key) {
  let raw = String(key || "");
  while (true) {
    const low = raw.toLowerCase();
    if (low.endsWith(":hedge")) raw = raw.slice(0, -":hedge".length);
    else if (low.endsWith(":inv")) raw = raw.slice(0, -":inv".length);
    else return raw;
  }
}

function isTestStatsRow(row, catalog = strategyCatalog) {
  const list = catalog || strategyCatalog || [];
  const base = statsRowBaseId(row?.key);
  const fromCatalog = list.find(
    (s) => s.id === base || s.id === row?.key || strategyCatalogLabel(s) === row?.name
  );
  if (fromCatalog) return isTestLaneStrategy(fromCatalog);
  return /TestStrategy$/i.test(String(base || row?.key || ""));
}

function isHtfStatsRow(row, catalog = strategyCatalog) {
  if (isHtfStrategyId(row?.key) || isHtfStrategyId(statsRowBaseId(row?.key))) return true;
  const list = catalog || strategyCatalog || [];
  const base = statsRowBaseId(row?.key);
  if (list.some((s) => (s.id === row?.key || s.id === base) && (isHtfStrategy(s) || isHtfStrategyId(s.id)))) {
    return true;
  }
  const fromCatalog = list.find(
    (s) => s.id === base || strategyCatalogLabel(s) === row?.name
  );
  return !!(fromCatalog && (isHtfStrategy(fromCatalog) || isHtfStrategyId(fromCatalog.id)));
}

function filterStatsRows(rows, scope, catalog = strategyCatalog) {
  const visible = rows.filter((r) => !isRetiredStatsRow(r));
  if (scope === "all") return visible;
  if (scope === "finder") return visible.filter((r) => r.name === FINDER_STATS_LABEL);
  if (scope === "grid") return visible.filter((r) => r.name === GRID_STATS_LABEL);
  if (scope === "bybitgrid") return visible.filter((r) => r.name === BYBIT_GRID_STATS_LABEL);
  if (scope === "strategy") {
    return visible.filter(
      (r) =>
        !isBotStatsLabel(r.name) &&
        !isTestStatsRow(r, catalog) &&
        !isHtfStatsRow(r, catalog) &&
        !isPriceActionStatsRow(r)
    );
  }
  if (scope === "htf1h") {
    return visible.filter((r) => isHtfStatsRow(r, catalog));
  }
  if (scope === "test_strategy") {
    return visible.filter((r) => isTestStatsRow(r, catalog) && !isHtfStatsRow(r, catalog));
  }
  if (scope === "llm") {
    return visible.filter((r) => isLlmStatsRow(r));
  }
  if (scope === "llm50") {
    return visible.filter((r) => isBookStatsRow(r));
  }
  if (scope === "cursor") {
    return visible.filter((r) => isCursorStatsRow(r));
  }
  if (scope === "cursor50") {
    return visible.filter((r) => isCursorBookStatsRow(r));
  }
  return visible;
}

function buildStatsRows(finderTrades, stratTrades, gridTrades, bybitHistory, catalog) {
  const rows = new Map();

  const ensure = (key, displayName) => {
    if (!rows.has(key)) {
      rows.set(key, {
        key,
        name: displayName,
        wins: 0,
        losses: 0,
        flat: 0,
        closed: 0,
        profit: 0,
        loss: 0,
      });
    } else if (displayName) {
      rows.get(key).name = displayName;
    }
    return rows.get(key);
  };

  ensure(FINDER_STATS_LABEL, FINDER_STATS_LABEL);
  ensure(GRID_STATS_LABEL, GRID_STATS_LABEL);
  ensure(BYBIT_GRID_STATS_LABEL, BYBIT_GRID_STATS_LABEL);
  // Keep every catalog strategy row (incl. disabled) so add/remove toggles never drop history labels.
  for (const s of catalog) ensure(s.id, strategyCatalogLabel(s));

  for (const t of finderTrades) addClosedTradeToRow(ensure(FINDER_STATS_LABEL, FINDER_STATS_LABEL), t);
  for (const t of gridTrades) addClosedTradeToRow(ensure(GRID_STATS_LABEL, GRID_STATS_LABEL), t);
  for (const t of stratTrades) {
    const key = tradeStatsRowKey(t, "strategy");
    addClosedTradeToRow(ensure(key, tradeSourceLabel(t, "strategy")), t);
  }
  for (const item of bybitHistory) {
    addBybitGridToRow(ensure(BYBIT_GRID_STATS_LABEL, BYBIT_GRID_STATS_LABEL), item);
  }

  const ordered = [
    FINDER_STATS_LABEL,
    GRID_STATS_LABEL,
    BYBIT_GRID_STATS_LABEL,
    ...catalog.map((s) => s.id),
  ];
  const result = [];
  for (const key of ordered) {
    if (rows.has(key)) result.push(rows.get(key));
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
  const modelScopes = new Set(["llm", "llm50", "cursor", "cursor50"]);
  const ftBots =
    scope === "all"
      ? ["finder", "strategy", "grid"]
      : scope === "bybitgrid" || scope === "test_strategy" || scope === "htf1h" || modelScopes.has(scope)
        ? []
        : [scope];

  if (ftBots.includes("finder")) {
    for (const t of filterClosedTrades(finderTrades, period, dateFrom, dateTo)) {
      entries.push({ kind: "trade", bot: "finder", trade: t });
    }
  }
  if (ftBots.includes("strategy") || scope === "test_strategy" || scope === "htf1h" || modelScopes.has(scope)) {
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
  if (scope === "test_strategy" || scope === "strategy" || scope === "htf1h") {
    const testIds = new Set(
      (strategyCatalog || []).filter(isTestLaneStrategy).map((s) => s.id)
    );
    filtered = filtered.filter((entry) => {
      if (entry.kind !== "trade" || entry.bot !== "strategy") return false;
      const sid = tradeSourceId(entry.trade, entry.bot);
      const htf = isHtfTrade(entry.trade) || isHtfStrategyId(sid);
      if (scope === "htf1h") return htf;
      if (htf) return false;
      if (scope === "strategy" && isRetiredTrade(entry.trade)) return false;
      const isTest = testIds.has(sid);
      return scope === "test_strategy" ? isTest : !isTest;
    });
  }
  if (scope === "all") {
    filtered = filtered.filter((entry) => entry.kind !== "trade" || !isRetiredTrade(entry.trade));
  }
  if (modelScopes.has(scope)) {
    filtered = filtered.filter((entry) => {
      if (entry.kind !== "trade") return false;
      if (scope === "llm") return isLlmTrade(entry.trade);
      if (scope === "llm50") return isBookTrade(entry.trade);
      if (scope === "cursor") return isCursorTrade(entry.trade);
      if (scope === "cursor50") return isCursorBookTrade(entry.trade);
      return false;
    });
  }
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
  const d = parseAppInstant(item.closed_at);
  if (!d) return String(item.closed_at).slice(0, 16);
  return fmtAppDateShort(d);
}

function bybitExitLabel(item) {
  return item.status_label || item.close_reason || "—";
}

function historyBotLabel(entry) {
  if (entry.kind === "bybit") return BYBIT_GRID_STATS_LABEL;
  if (entry.bot === "finder") return FINDER_STATS_LABEL;
  if (entry.bot === "grid") return GRID_STATS_LABEL;
  if (entry.bot === "strategy" && entry.trade && isPriceActionTrade(entry.trade)) return "Price Action";
  if (entry.bot === "strategy" && entry.trade && isHtfTrade(entry.trade)) return "1-часовые";
  return BOTS[entry.bot]?.label || entry.bot;
}

function historyBotClass(entry) {
  if (entry.kind === "bybit") return "bot-bybitgrid";
  if (entry.bot === "finder") return "bot-finder";
  if (entry.bot === "grid") return "bot-grid";
  if (entry.bot === "strategy" && entry.trade && isPriceActionTrade(entry.trade)) return "bot-priceaction";
  if (entry.bot === "strategy" && entry.trade && isHtfTrade(entry.trade)) return "bot-htf";
  if (entry.bot === "strategy" && isTestTrade(entry.trade, "strategy")) return "bot-test";
  return "bot-strategy";
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
  const d = parseAppInstant(raw);
  if (!d) return String(raw);
  return fmtAppDateTime(d);
}

function historyRiskPctNumber(ratio) {
  if (ratio == null || ratio === "") return null;
  const n = Number(ratio);
  if (!Number.isFinite(n) || n === 0) return null;
  return Math.abs(n) * 100;
}

function fmtHistoryRiskPct(ratio) {
  const pct = historyRiskPctNumber(ratio);
  if (pct == null) return "—";
  const rounded = Math.round(pct * 10) / 10;
  const text = Math.abs(rounded - Math.round(rounded)) < 0.05 ? String(Math.round(rounded)) : rounded.toFixed(1);
  return `${text}%`;
}

function tradeStopRatio(trade) {
  if (!trade) return null;
  if (trade.initial_stop_loss_pct != null && trade.initial_stop_loss_pct !== "") return trade.initial_stop_loss_pct;
  if (trade.stop_loss_pct != null && trade.stop_loss_pct !== "") return trade.stop_loss_pct;
  const meta = trade.ml_meta;
  if (meta && meta.stop_loss_ratio != null) return meta.stop_loss_ratio;
  return null;
}

function tradeTakeRatio(trade) {
  if (!trade) return null;
  if (trade.take_profit_ratio != null && trade.take_profit_ratio !== "") return trade.take_profit_ratio;
  const meta = trade.ml_meta;
  if (meta && meta.take_profit_ratio != null) return meta.take_profit_ratio;
  return null;
}

function historySltpCell(trade) {
  const sl = fmtHistoryRiskPct(tradeStopRatio(trade));
  const tp = fmtHistoryRiskPct(tradeTakeRatio(trade));
  if (sl === "—" && tp === "—") return "—";
  return `−${sl} / +${tp}`;
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
      sl: "",
      tp: "",
      ml: "",
      pnl_usdt: pnl.toFixed(4),
      pnl_pct: "",
      pnl_net_usdt: pnl.toFixed(4),
      pnl_net_pct: "",
      max_profit_usdt: "",
      max_profit_pct: "",
      max_loss_usdt: "",
      max_loss_pct: "",
      stake: item.total_investment != null ? String(item.total_investment) : "",
      leverage: item.leverage != null ? String(item.leverage) : "",
      trade_id: item.bot_id || "",
      exit: bybitExitLabel(item),
    };
  }
  const t = entry.trade;
  const { abs, pct } = closedTradePnl(t);
  const net = closedTradePnlNet(t);
  const conf = tradeMlConfidence(t);
  return {
    close_date: fmtHistoryDateRaw(t.close_date || t.close_fill_date),
    open_date: fmtHistoryDateRaw(t.open_date),
    bot: historyBotLabel(entry),
    pair: t.pair || "",
    side: t.is_short ? "SHORT" : "LONG",
    strategy: tradeSourceLabel(t, entry.bot),
    sl: historyRiskPctNumber(tradeStopRatio(t))?.toFixed(2) ?? "",
    tp: historyRiskPctNumber(tradeTakeRatio(t))?.toFixed(2) ?? "",
    ml: conf != null ? (conf * 100).toFixed(1) : "",
    pnl_usdt: abs.toFixed(4),
    pnl_pct: pct.toFixed(2),
    pnl_net_usdt: net.abs.toFixed(4),
    pnl_net_pct: net.pct.toFixed(2),
    ...historyExtremeExportFields(t),
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
    showToast("Выберите хотя бы одну колонку для экспорта", "warn");
    return;
  }
  if (!entries.length) {
    showToast("Нет данных для экспорта с текущими фильтрами", "warn");
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

function historyCardHtml(entry) {
  const botClass = historyBotClass(entry);
  if (entry.kind === "bybit") {
    const item = entry.item;
    const pnl = Number(item.realised_pnl ?? item.pnl ?? 0);
    const pair = escapeHtml(item.pair || item.symbol || "—");
    return `<article class="history-card ${pnlClass(pnl)} ${botClass}">
      <div class="history-card-top">
        <div class="history-card-main">
          <strong class="history-card-pair">${pair}</strong>
          <span class="history-card-meta">${fmtBybitHistoryDate(item)} · ${BYBIT_GRID_STATS_LABEL}</span>
        </div>
        <div class="history-card-pnl ${pnlClass(pnl)}">${fmtUsdSigned(pnl, 2)}</div>
      </div>
      <div class="history-card-tags">
        <span class="history-chip">${escapeHtml(item.grid_mode_label || "grid")}</span>
        <span class="history-chip">Bybit</span>
        <span class="history-chip muted-chip">${escapeHtml(bybitExitLabel(item))}</span>
      </div>
    </article>`;
  }
  const t = entry.trade;
  const { abs } = closedTradePnl(t);
  const net = closedTradePnlNet(t);
  const side = t.is_short ? "SHORT" : "LONG";
  const source = tradeSourceLabel(t, entry.bot);
  const mlConf = fmtMlConfidence(t);
  const mlChip =
    mlConf && mlConf !== "—"
      ? `<span class="history-chip history-chip-ml" title="${escapeHtml(fmtMlConfidenceDetail(t))}">ML ${escapeHtml(mlConf)}</span>`
      : `<span class="history-chip history-chip-ml is-missing" title="Фактическая уверенность ML недоступна">ML —</span>`;
  const { maxProfit, maxLoss } = tradeMaxProfitLoss(t, { requireTracked: true });
  const extremeChips = [
    maxProfit
      ? `<span class="history-chip ${pnlClass(maxProfit.abs)}" title="Максимальная прибыль за время сделки">Макс+ ${fmtUsdSigned(maxProfit.abs, 2)}</span>`
      : "",
    maxLoss
      ? `<span class="history-chip ${pnlClass(maxLoss.abs)}" title="Максимальный убыток за время сделки">Макс− ${fmtUsdSigned(maxLoss.abs, 2)}</span>`
      : "",
  ].join("");
  return `<article class="history-card ${pnlClass(net.abs)} ${botClass}">
    <div class="history-card-top">
      <div class="history-card-main">
        <strong class="history-card-pair">${escapeHtml(t.pair || "—")}</strong>
        <span class="history-card-meta">${fmtTradeDate(t)} · ${historyBotLabel(entry)}</span>
      </div>
      <div class="history-card-pnl ${pnlClass(net.abs)}">
        <span>${fmtUsdSigned(net.abs, 2)}</span>
        <span class="history-card-pct muted">${fmtPct(net.pct)}</span>
        <span class="history-card-pct muted" title="Без комиссии">без ком. ${fmtUsdSigned(abs, 2)}</span>
      </div>
    </div>
    <div class="history-card-tags">
      <span class="history-chip side-${t.is_short ? "short" : "long"}">${side}</span>
      <span class="history-chip">${escapeHtml(source)}</span>
      ${mlChip}
      <span class="history-chip" title="Стоп и тейк, записанные на сделке">${escapeHtml(historySltpCell(t))}</span>
      ${extremeChips}
      <span class="history-chip muted-chip">${escapeHtml(exitReasonLabel(t.exit_reason))}</span>
    </div>
  </article>`;
}

function renderHistoryPagination(total) {
  const el = $("history-pagination");
  const info = $("history-page-info");
  const prev = $("history-page-prev");
  const next = $("history-page-next");
  if (!el || !info) return;

  const pages = Math.max(1, Math.ceil(total / HISTORY_PAGE_SIZE));
  if (historyPage > pages) historyPage = pages;
  if (historyPage < 1) historyPage = 1;

  const show = total > HISTORY_PAGE_SIZE;
  el.classList.toggle("hidden", !show);
  el.hidden = !show;
  if (!show) return;

  const from = total ? (historyPage - 1) * HISTORY_PAGE_SIZE + 1 : 0;
  const to = Math.min(historyPage * HISTORY_PAGE_SIZE, total);
  info.textContent = `${from}–${to} из ${total} · стр. ${historyPage}/${pages}`;
  if (prev) prev.disabled = historyPage <= 1;
  if (next) next.disabled = historyPage >= pages;
}

function historyPageSlice(entries) {
  const start = (historyPage - 1) * HISTORY_PAGE_SIZE;
  return entries.slice(start, start + HISTORY_PAGE_SIZE);
}

function setHistoryPage(page) {
  historyPage = Math.max(1, Number(page) || 1);
  renderHistoryView();
  $("history-table-wrap")?.scrollTo?.({ top: 0 });
  $("history-view")?.querySelector(".history-table-wrap")?.scrollTo?.({ top: 0 });
}

function renderHistoryTable(entries) {
  const bodyEl = $("history-table-body");
  const cardsEl = $("history-cards");
  const pageEntries = historyPageSlice(entries);

  if (bodyEl) {
    if (!entries.length) {
      bodyEl.innerHTML =
        '<tr class="stats-history-empty"><td colspan="12">Нет закрытых сделок по выбранным фильтрам</td></tr>';
    } else {
      bodyEl.innerHTML = pageEntries
        .map((entry) => {
          if (entry.kind === "bybit") {
            const item = entry.item;
            const pnl = Number(item.realised_pnl ?? item.pnl ?? 0);
            const pnlClassName = pnlClass(pnl);
            return `<tr class="history-row bot-bybitgrid">
          <td>${fmtBybitHistoryDate(item)}</td>
          <td><span class="history-bot-badge">${BYBIT_GRID_STATS_LABEL}</span></td>
          <td class="history-pair-cell">${escapeHtml(item.pair || item.symbol || "—")}</td>
          <td>${escapeHtml(item.grid_mode_label || "grid")}</td>
          <td>Bybit</td>
          <td>—</td>
          <td>—</td>
          <td class="history-pnl-cell ${pnlClassName}">${fmtUsdSigned(pnl, 2)}</td>
          <td class="history-pnl-cell ${pnlClassName}">${fmtUsdSigned(pnl, 2)}</td>
          <td class="history-pnl-cell">—</td>
          <td class="history-pnl-cell">—</td>
          <td>${escapeHtml(bybitExitLabel(item))}</td>
        </tr>`;
          }
          const t = entry.trade;
          const { abs, pct } = closedTradePnl(t);
          const net = closedTradePnlNet(t);
          const botLabel = historyBotLabel(entry);
          const side = t.is_short ? "SHORT" : "LONG";
          const source = tradeSourceLabel(t, entry.bot);
          const pnlClassName = pnlClass(abs);
          const netClassName = pnlClass(net.abs);
          const mlConf = fmtMlConfidence(t);
          const mlTitle = escapeHtml(fmtMlConfidenceDetail(t));
          const feeTitle = escapeHtml(
            `Комиссия ${(BYBIT_TAKER_FEE_RATE * 100).toFixed(2)}% × 2 ≈ ${fmtUsd(net.fees, 4)}`
          );
          const botClass = historyBotClass(entry);
          const { maxProfit, maxLoss } = tradeMaxProfitLoss(t, { requireTracked: true });
          return `<tr class="history-row ${botClass}">
        <td>${fmtTradeDate(t)}</td>
        <td><span class="history-bot-badge">${botLabel}</span></td>
        <td class="history-pair-cell">${escapeHtml(t.pair || "—")}</td>
        <td><span class="history-side-badge side-${t.is_short ? "short" : "long"}">${side}</span></td>
        <td>${escapeHtml(source)}</td>
        <td class="history-sltp-cell" title="Стоп и тейк на момент входа">${escapeHtml(historySltpCell(t))}</td>
        <td title="${mlTitle}">${escapeHtml(mlConf)}</td>
        <td class="history-pnl-cell ${pnlClassName}">${fmtUsdSigned(abs, 2)}<br><span class="muted history-pnl-pct">${fmtPct(pct)}</span></td>
        <td class="history-pnl-cell ${netClassName}" title="${feeTitle}">${fmtUsdSigned(net.abs, 2)}<br><span class="muted history-pnl-pct">${fmtPct(net.pct)}</span></td>
        <td class="history-pnl-cell ${maxProfit ? pnlClass(maxProfit.abs) : ""}" title="Максимальная прибыль за время сделки">${fmtHistoryExtremeCell(maxProfit)}</td>
        <td class="history-pnl-cell ${maxLoss ? pnlClass(maxLoss.abs) : ""}" title="Максимальный убыток за время сделки">${fmtHistoryExtremeCell(maxLoss)}</td>
        <td>${escapeHtml(exitReasonLabel(t.exit_reason))}</td>
      </tr>`;
        })
        .join("");
    }
  }

  if (cardsEl) {
    if (!entries.length) {
      cardsEl.innerHTML =
        '<p class="history-cards-empty muted">Нет закрытых сделок по выбранным фильтрам</p>';
    } else {
      cardsEl.innerHTML = pageEntries.map(historyCardHtml).join("");
    }
  }

  renderHistoryPagination(entries.length);
  renderHistoryDistribution(entries);
}

/* history-dist-start */
/** Distribution, tertiles and Pearson correlation for the history panel. */

function fmtHistoryDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const min = ms / 60000;
  if (min < 1) return "< 1 мин";
  if (min < 90) return `${Math.round(min)} мин`;
  const hours = min / 60;
  if (hours < 48) {
    const rounded = hours < 10 ? Math.round(hours * 10) / 10 : Math.round(hours);
    return `${rounded} ч`;
  }
  const days = hours / 24;
  const rounded = days < 10 ? Math.round(days * 10) / 10 : Math.round(days);
  return `${rounded} д`;
}

function distNum(n) {
  if (!Number.isFinite(n)) return "0";
  return String(Math.round(n * 100) / 100);
}

function distClamp01(t) {
  if (!Number.isFinite(t)) return 0;
  return Math.min(1, Math.max(0, t));
}

function distScale(v, min, max) {
  if (!Number.isFinite(v) || !Number.isFinite(min) || !Number.isFinite(max) || max === min) return 0.5;
  return (v - min) / (max - min);
}

function distQuantile(sorted, p) {
  const n = sorted.length;
  if (!n) return null;
  if (n === 1) return sorted[0];
  const idx = (n - 1) * Math.min(1, Math.max(0, p));
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  if (lo === hi) return sorted[lo];
  const w = idx - lo;
  return sorted[lo] * (1 - w) + sorted[hi] * w;
}

function distRows(points, key) {
  return (points || []).filter((row) => Number.isFinite(row?.[key]));
}

function distMeanOf(rows, key) {
  if (!rows || !rows.length) return null;
  let sum = 0;
  for (const row of rows) sum += row[key];
  return sum / rows.length;
}

function distMedianOf(rows, key) {
  if (!rows || !rows.length) return null;
  const values = rows.map((row) => row[key]).sort((a, b) => a - b);
  return distQuantile(values, 0.5);
}

function distDescribe(rows, key) {
  const sorted = rows.slice().sort((a, b) => a[key] - b[key]);
  const values = sorted.map((row) => row[key]);
  const n = values.length;
  if (!n) return null;
  const q1 = distQuantile(values, 0.25);
  const q3 = distQuantile(values, 0.75);
  const median = distQuantile(values, 0.5);
  let sum = 0;
  for (const value of values) sum += value;
  const iqr = q3 - q1;
  const fenceLo = q1 - 1.5 * iqr;
  const fenceHi = q3 + 1.5 * iqr;
  const classify = n >= 8;
  const outliers = [];
  const inliers = [];
  for (const row of sorted) {
    const value = row[key];
    if (classify && (value < fenceLo || value > fenceHi)) outliers.push(row);
    else inliers.push(row);
  }
  let lowN = 0;
  let highN = 0;
  for (const row of outliers) {
    if (row[key] < fenceLo) lowN += 1;
    else highN += 1;
  }
  return {
    sorted,
    values,
    n,
    q1,
    q3,
    median,
    mean: sum / n,
    iqr,
    fenceLo,
    fenceHi,
    outliers,
    inliers,
    lowN,
    highN,
    min: values[0],
    max: values[n - 1],
    whiskerLo: inliers.length ? inliers[0][key] : values[0],
    whiskerHi: inliers.length ? inliers[inliers.length - 1][key] : values[n - 1],
    classify,
  };
}

function historyDistBinCount(n) {
  if (n <= 1) return 1;
  if (n < 8) return Math.min(5, n);
  const sturges = Math.round(Math.log2(n) + 1);
  return Math.max(6, Math.min(14, sturges));
}

function distHistogram(values, binCount) {
  const n = values.length;
  if (!n) return [];
  let min = values[0];
  let max = values[0];
  for (const value of values) {
    if (value < min) min = value;
    if (value > max) max = value;
  }
  const binsN = Math.max(1, binCount);
  if (min === max) return [{ from: min, to: max, mid: min, count: n }];
  const width = (max - min) / binsN;
  const bins = [];
  for (let i = 0; i < binsN; i++) {
    const from = min + i * width;
    const to = min + (i + 1) * width;
    bins.push({ from, to, mid: (from + to) / 2, count: 0 });
  }
  for (const value of values) {
    let i = Math.floor((value - min) / width);
    if (i < 0) i = 0;
    if (i >= binsN) i = binsN - 1;
    bins[i].count += 1;
  }
  return bins;
}

function distPearson(xs, ys) {
  const n = xs.length;
  if (n < 3 || ys.length !== n) return null;
  let sx = 0;
  let sy = 0;
  for (let i = 0; i < n; i++) {
    sx += xs[i];
    sy += ys[i];
  }
  const mx = sx / n;
  const my = sy / n;
  let num = 0;
  let dx = 0;
  let dy = 0;
  for (let i = 0; i < n; i++) {
    const a = xs[i] - mx;
    const b = ys[i] - my;
    num += a * b;
    dx += a * a;
    dy += b * b;
  }
  if (dx <= 0 || dy <= 0) return null;
  const r = num / Math.sqrt(dx * dy);
  if (!Number.isFinite(r)) return null;
  return Math.max(-1, Math.min(1, r));
}

function distLinreg(xs, ys) {
  const n = xs.length;
  if (n < 2 || ys.length !== n) return null;
  let sx = 0;
  let sy = 0;
  for (let i = 0; i < n; i++) {
    sx += xs[i];
    sy += ys[i];
  }
  const mx = sx / n;
  const my = sy / n;
  let num = 0;
  let den = 0;
  for (let i = 0; i < n; i++) {
    const a = xs[i] - mx;
    num += a * (ys[i] - my);
    den += a * a;
  }
  if (den <= 0) return null;
  const slope = num / den;
  return { slope, intercept: my - slope * mx };
}

function distTertileBands(rows, key) {
  const sorted = rows.slice().sort((a, b) => a[key] - b[key]);
  const n = sorted.length;
  if (n < 3) return null;
  const i1 = Math.floor(n / 3);
  const i2 = Math.floor((2 * n) / 3);
  const low = sorted.slice(0, i1);
  const mid = sorted.slice(i1, i2);
  const high = sorted.slice(i2);
  if (!low.length || !mid.length || !high.length) return null;
  return { low, mid, high };
}

function distTertiles(rows, key, higherIsBetter) {
  const bands = distTertileBands(rows, key);
  if (!bands) return null;
  if (higherIsBetter) return { best: bands.high, mid: bands.mid, worst: bands.low };
  return { best: bands.low, mid: bands.mid, worst: bands.high };
}

function distSamplePairs(pairs, max) {
  if (pairs.length <= max) return pairs.slice();
  const want = new Set();
  const last = pairs.length - 1;
  const steps = Math.max(1, max - 1);
  for (let i = 0; i < max; i++) want.add(Math.round((i * last) / steps));
  let minI = 0;
  let maxI = 0;
  for (let i = 1; i < pairs.length; i++) {
    if (pairs[i].y < pairs[minI].y) minI = i;
    if (pairs[i].y > pairs[maxI].y) maxI = i;
  }
  want.add(minI);
  want.add(maxI);
  return [...want].sort((a, b) => a - b).map((i) => pairs[i]);
}

const HISTORY_DIST_METRICS = [
  {
    key: "net",
    pctKey: "netPct",
    title: "С комиссией",
    hint: "Колонка «С комиссией»: результат после round-trip комиссии",
    kind: "usd",
    higherIsBetter: true,
    color: "#60a5fa",
    segment: true,
    bestHint: "больший результат после комиссии",
  },
  {
    key: "maxProfit",
    pctKey: "maxProfitPct",
    title: "Макс. прибыль",
    hint: "Самый высокий PnL за время сделки",
    kind: "usd",
    higherIsBetter: true,
    color: "#34d399",
    segment: true,
    bestHint: "больший пик",
  },
  {
    key: "maxLoss",
    pctKey: "maxLossPct",
    title: "Макс. убыток",
    hint: "Самый низкий PnL за время сделки. Лучшие — менее глубокая просадка",
    kind: "usd",
    higherIsBetter: true,
    color: "#f87171",
    segment: true,
    bestHint: "менее глубокая просадка",
  },
  {
    key: "durationMs",
    pctKey: null,
    title: "Длительность",
    hint: "От открытия до закрытия",
    kind: "duration",
    higherIsBetter: false,
    color: "#f59e0b",
    segment: false,
    bestHint: "",
  },
];

function historyDistFormat(kind, value) {
  if (!Number.isFinite(value)) return "—";
  if (kind === "duration") return fmtHistoryDuration(value);
  const digits = Math.abs(value) >= 100 ? 1 : 2;
  return fmtUsdSigned(value, digits);
}

function historyDistTick(kind, value) {
  if (!Number.isFinite(value)) return "—";
  if (kind === "duration") return fmtHistoryDuration(value);
  if (Math.abs(value) >= 1000) {
    const text = `${(value / 1000).toFixed(1)}k`;
    return value > 0 ? `+${text}` : text;
  }
  const digits = Math.abs(value) >= 100 ? 0 : Math.abs(value) >= 10 ? 1 : 2;
  const text = value.toFixed(digits);
  return value > 0 ? `+${text}` : text;
}

function historyDistPoints(entries) {
  const out = [];
  for (const entry of entries || []) {
    if (entry?.kind === "bybit") {
      const item = entry.item || {};
      const net = Number(item.realised_pnl ?? item.pnl);
      const openMs = parseAppInstant(item.created_at || item.started_at)?.getTime() || 0;
      const closeMs = bybitCloseMs(item);
      out.push({
        pair: item.pair || item.symbol || "—",
        when: fmtBybitHistoryDate(item),
        net: Number.isFinite(net) ? net : null,
        netPct: null,
        maxProfit: null,
        maxProfitPct: null,
        maxLoss: null,
        maxLossPct: null,
        durationMs: openMs > 0 && closeMs >= openMs ? closeMs - openMs : null,
      });
      continue;
    }
    const trade = entry?.trade;
    if (!trade) continue;
    const net = closedTradePnlNet(trade);
    const extremes = tradeMaxProfitLoss(trade, { requireTracked: true });
    const openMs = parseAppInstant(trade.open_date)?.getTime() || 0;
    const closeMs = tradeCloseMs(trade);
    out.push({
      pair: trade.pair || "—",
      when: fmtTradeDate(trade),
      net: Number.isFinite(net.abs) ? net.abs : null,
      netPct: Number.isFinite(net.pct) ? net.pct : null,
      maxProfit: extremes.maxProfit && Number.isFinite(extremes.maxProfit.abs) ? extremes.maxProfit.abs : null,
      maxProfitPct: extremes.maxProfit && Number.isFinite(extremes.maxProfit.pct) ? extremes.maxProfit.pct : null,
      maxLoss: extremes.maxLoss && Number.isFinite(extremes.maxLoss.abs) ? extremes.maxLoss.abs : null,
      maxLossPct: extremes.maxLoss && Number.isFinite(extremes.maxLoss.pct) ? extremes.maxLoss.pct : null,
      durationMs: openMs > 0 && closeMs >= openMs ? closeMs - openMs : null,
    });
  }
  return out;
}

function historyDistEdgeLabel(rows, key, kind, sign) {
  if (!rows.length) return "";
  let extreme = rows[0][key];
  for (const row of rows) {
    if (sign < 0 ? row[key] < extreme : row[key] > extreme) extreme = row[key];
  }
  const text = historyDistTick(kind, extreme);
  return rows.length > 1 ? `${text} · ${rows.length}` : text;
}

function historyDistBoxSvg(desc, metric) {
  const lowRows = desc.outliers.filter((row) => row[metric.key] < desc.fenceLo);
  const highRows = desc.outliers.filter((row) => row[metric.key] > desc.fenceHi);
  const lowText = historyDistEdgeLabel(lowRows, metric.key, metric.kind, -1);
  const highText = historyDistEdgeLabel(highRows, metric.key, metric.kind, 1);
  const padL = lowText ? Math.min(112, Math.max(48, lowText.length * 6.4 + 12)) : 12;
  const padR = highText ? Math.min(112, Math.max(48, highText.length * 6.4 + 12)) : 12;
  const W = 360;
  const H = 64;
  const y = 24;
  const span = desc.whiskerHi - desc.whiskerLo;
  const lo = span === 0 ? desc.whiskerLo - 1 : desc.whiskerLo - span * 0.08;
  const hi = span === 0 ? desc.whiskerHi + 1 : desc.whiskerHi + span * 0.08;
  const plotW = W - padL - padR;
  const xOf = (value) => padL + distClamp01(distScale(value, lo, hi)) * plotW;
  const x1 = xOf(desc.whiskerLo);
  const x2 = xOf(desc.whiskerHi);
  const q1 = xOf(desc.q1);
  const boxW = Math.max(2, xOf(desc.q3) - q1);
  const med = xOf(desc.median);
  const aria = escapeHtml(`Шкала ${metric.title}, медиана ${historyDistFormat(metric.kind, desc.median)}`);
  const lowMark = lowText
    ? `<text x="4" y="${y + 4}" fill="#f59e0b" font-size="10" text-anchor="start">${escapeHtml(lowText)}</text>`
    : "";
  const highMark = highText
    ? `<text x="${W - 4}" y="${y + 4}" fill="#f59e0b" font-size="10" text-anchor="end">${escapeHtml(highText)}</text>`
    : "";
  return `<svg viewBox="0 0 ${W} ${H}" class="history-dist-svg" role="img" aria-label="${aria}">
    ${lowMark}
    ${highMark}
    <line x1="${distNum(x1)}" y1="${y}" x2="${distNum(x2)}" y2="${y}" stroke="#8b9cb3" stroke-width="1.5"/>
    <line x1="${distNum(x1)}" y1="${y - 7}" x2="${distNum(x1)}" y2="${y + 7}" stroke="#8b9cb3" stroke-width="1.5"/>
    <line x1="${distNum(x2)}" y1="${y - 7}" x2="${distNum(x2)}" y2="${y + 7}" stroke="#8b9cb3" stroke-width="1.5"/>
    <rect x="${distNum(q1)}" y="${y - 9}" width="${distNum(boxW)}" height="18" rx="3" fill="${metric.color}" fill-opacity="0.28" stroke="${metric.color}"/>
    <line x1="${distNum(med)}" y1="${y - 9}" x2="${distNum(med)}" y2="${y + 9}" stroke="#e8eef7" stroke-width="2"/>
    <text x="${distNum(x1)}" y="56" fill="#8b9cb3" font-size="10" text-anchor="start">${escapeHtml(historyDistTick(metric.kind, desc.whiskerLo))}</text>
    <text x="${distNum(x2)}" y="56" fill="#8b9cb3" font-size="10" text-anchor="end">${escapeHtml(historyDistTick(metric.kind, desc.whiskerHi))}</text>
  </svg>`;
}

function historyDistHistSvg(values, metric) {
  const bins = distHistogram(values, historyDistBinCount(values.length));
  if (!bins.length) return "";
  const W = 360;
  const H = 132;
  const padL = 8;
  const padR = 8;
  const padT = 10;
  const padB = 22;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  let maxCount = 1;
  for (const bin of bins) if (bin.count > maxCount) maxCount = bin.count;
  const slot = plotW / bins.length;
  const barW = Math.max(1, slot - (bins.length > 1 ? 3 : 0));
  const parts = [];
  if (metric.kind === "usd" && bins[0].from < 0 && bins[bins.length - 1].to > 0) {
    const zx = padL + distClamp01(distScale(0, bins[0].from, bins[bins.length - 1].to)) * plotW;
    parts.push(`<line x1="${distNum(zx)}" y1="${padT}" x2="${distNum(zx)}" y2="${padT + plotH}" stroke="#8b9cb3" stroke-dasharray="3 3" stroke-width="1"/>`);
  }
  bins.forEach((bin, i) => {
    const h = (bin.count / maxCount) * plotH;
    const x = padL + i * slot + (slot - barW) / 2;
    const y = padT + plotH - h;
    const fill = metric.kind === "duration" ? metric.color : bin.mid < 0 ? "#f87171" : "#34d399";
    const title = `${historyDistTick(metric.kind, bin.from)} … ${historyDistTick(metric.kind, bin.to)}: ${bin.count}`;
    parts.push(
      `<rect x="${distNum(x)}" y="${distNum(y)}" width="${distNum(barW)}" height="${distNum(bin.count ? Math.max(h, 1.5) : 0)}" rx="2" fill="${fill}"><title>${escapeHtml(title)}</title></rect>`
    );
  });
  const yText = H - 6;
  const min = bins[0].from;
  const max = bins[bins.length - 1].to;
  parts.push(`<text x="${padL}" y="${yText}" fill="#8b9cb3" font-size="10" text-anchor="start">${escapeHtml(historyDistTick(metric.kind, min))}</text>`);
  if (bins.length > 1 && max !== min) {
    parts.push(`<text x="${W / 2}" y="${yText}" fill="#8b9cb3" font-size="10" text-anchor="middle">${escapeHtml(historyDistTick(metric.kind, (min + max) / 2))}</text>`);
    parts.push(`<text x="${W - padR}" y="${yText}" fill="#8b9cb3" font-size="10" text-anchor="end">${escapeHtml(historyDistTick(metric.kind, max))}</text>`);
  }
  return `<svg viewBox="0 0 ${W} ${H}" class="history-dist-svg" role="img" aria-label="${escapeHtml(`Гистограмма «${metric.title}»`)}">${parts.join("")}</svg>`;
}

function historyDistStatsTable(desc, metric) {
  const fmt = (value) => escapeHtml(historyDistFormat(metric.kind, value));
  const lines = [
    ["Сделок", String(desc.n)],
    ["Минимум", fmt(desc.min)],
    ["25%", fmt(desc.q1)],
    ["Медиана", fmt(desc.median)],
    ["75%", fmt(desc.q3)],
    ["Максимум", fmt(desc.max)],
    ["Среднее", fmt(desc.mean)],
  ];
  if (metric.pctKey) {
    const pctRows = distRows(desc.sorted, metric.pctKey);
    if (pctRows.length === desc.n) {
      const pctDesc = distDescribe(pctRows, metric.pctKey);
      if (pctDesc) {
        lines.push(["Медиана %", escapeHtml(fmtPct(pctDesc.median))]);
        lines.push(["Среднее %", escapeHtml(fmtPct(pctDesc.mean))]);
      }
    }
  }
  lines.push(["Выбросы", desc.classify ? String(desc.outliers.length) : "от 8 сделок"]);
  const body = lines
    .map(([label, value]) => `<tr><th scope="row">${escapeHtml(label)}</th><td>${value}</td></tr>`)
    .join("");
  return `<table class="history-dist-stats">${body}</table>`;
}

function historyDistOutlierHtml(desc, metric) {
  if (!desc.classify) {
    return `<p class="muted history-dist-note">Выбросы считаются от 8 сделок.</p>`;
  }
  if (!desc.outliers.length) {
    return `<p class="muted history-dist-note">Выбросов нет: все значения внутри типичного разброса.</p>`;
  }
  const ranked = desc.outliers
    .slice()
    .sort((a, b) => Math.abs(b[metric.key] - desc.median) - Math.abs(a[metric.key] - desc.median));
  const shown = ranked.slice(0, 6);
  const extra = ranked.length - shown.length;
  const body = shown
    .map((row) => {
      const value = row[metric.key];
      const cls = metric.kind === "usd" ? pnlClass(value) : "";
      const pct =
        metric.pctKey && Number.isFinite(row[metric.pctKey])
          ? `<br><span class="muted">${escapeHtml(fmtPct(row[metric.pctKey]))}</span>`
          : "";
      return `<tr>
        <td>${escapeHtml(row.pair)}</td>
        <td class="${cls}">${escapeHtml(historyDistFormat(metric.kind, value))}${pct}</td>
        <td>${escapeHtml(row.when)}</td>
      </tr>`;
    })
    .join("");
  const more = extra > 0 ? `<p class="muted history-dist-note">Ещё ${extra}. В таблице самые далёкие от медианы.</p>` : "";
  return `<p class="history-dist-tails"><span>Слева: ${desc.lowN}</span><span>Справа: ${desc.highN}</span></p>
    <table class="history-dist-outliers">
      <thead><tr><th>Пара</th><th>Значение</th><th>Дата</th></tr></thead>
      <tbody>${body}</tbody>
    </table>
    ${more}`;
}

function historyDistCardHtml(metric, points) {
  const rows = distRows(points, metric.key);
  const cover =
    rows.length === points.length ? `${rows.length} сделок` : `${rows.length} из ${points.length}`;
  let coverTitle = "";
  if (rows.length !== points.length && (metric.key === "maxProfit" || metric.key === "maxLoss")) {
    coverTitle = "Нет записанных min/max цены";
  } else if (rows.length !== points.length && metric.key === "durationMs") {
    coverTitle = "Нет пары дат открытия и закрытия";
  }
  if (!rows.length) {
    return `<article class="history-dist-card" data-metric="${metric.key}">
      <header class="history-dist-card-head">
        <div>
          <h4>${escapeHtml(metric.title)}</h4>
          <p class="muted">${escapeHtml(metric.hint)}</p>
        </div>
        <p class="history-dist-cover">${escapeHtml(cover)}</p>
      </header>
      <p class="history-dist-empty">Нет данных в выбранных сделках.</p>
    </article>`;
  }
  const desc = distDescribe(rows, metric.key);
  const histRows = desc.inliers.length ? desc.inliers : desc.sorted;
  const histNote =
    histRows.length === desc.n
      ? "Высота столбца — число сделок. На шкале блок — середина 50% сделок."
      : "Столбцы и шкала — без выбросов, чтобы был виден основной разброс. Жёлтые числа по краям шкалы и таблица ниже — выбросы.";
  return `<article class="history-dist-card" data-metric="${metric.key}">
    <header class="history-dist-card-head">
      <div>
        <h4>${escapeHtml(metric.title)}</h4>
        <p class="muted">${escapeHtml(metric.hint)}</p>
      </div>
      <p class="history-dist-cover" title="${escapeHtml(coverTitle)}">${escapeHtml(cover)}</p>
    </header>
    <div class="history-dist-card-body">
      ${historyDistStatsTable(desc, metric)}
      <div class="history-dist-visual">
        ${historyDistBoxSvg(desc, metric)}
        ${historyDistHistSvg(histRows.map((row) => row[metric.key]), metric)}
        <p class="muted history-dist-note">${escapeHtml(histNote)}</p>
      </div>
    </div>
    ${historyDistOutlierHtml(desc, metric)}
  </article>`;
}

function historyDistSegmentHtml(metric, points) {
  const rows = distRows(points, metric.key);
  const groups = distTertiles(rows, metric.key, metric.higherIsBetter);
  if (!groups) {
    return `<article class="history-seg-card">
      <h4>${escapeHtml(metric.title)}</h4>
      <p class="history-dist-empty">Нужно минимум 3 сделки с данными.</p>
    </article>`;
  }
  const order = [
    ["best", "Лучшие", groups.best],
    ["mid", "Средние", groups.mid],
    ["worst", "Худшие", groups.worst],
  ];
  const bar = order
    .map(([id, , group]) => `<span class="history-seg-chunk is-${id}" style="flex:${group.length} 1 0"></span>`)
    .join("");
  const cards = order
    .map(([id, label, group]) => {
      const med = distMedianOf(group, metric.key);
      const min = group[0][metric.key];
      const max = group[group.length - 1][metric.key];
      const pctRows = metric.pctKey ? group.filter((row) => Number.isFinite(row[metric.pctKey])) : [];
      const pctMed = pctRows.length === group.length ? distMedianOf(pctRows, metric.pctKey) : null;
      const range =
        min === max
          ? escapeHtml(historyDistFormat(metric.kind, min))
          : `${escapeHtml(historyDistFormat(metric.kind, min))} — ${escapeHtml(historyDistFormat(metric.kind, max))}`;
      const share = Math.round((group.length / rows.length) * 100);
      const pct = pctMed != null ? `<div class="muted">${escapeHtml(fmtPct(pctMed))}</div>` : "";
      return `<article class="history-seg-group is-${id}">
        <div class="history-seg-label">${label}</div>
        <div class="history-seg-count">${group.length} · ${share}%</div>
        <div class="history-seg-median ${metric.kind === "usd" ? pnlClass(med) : ""}">${escapeHtml(historyDistFormat(metric.kind, med))}</div>
        ${pct}
        <div class="muted history-seg-range">${range}</div>
      </article>`;
    })
    .join("");
  return `<article class="history-seg-card">
    <header class="history-seg-head">
      <h4>${escapeHtml(metric.title)}</h4>
      <p class="muted">Лучшие — ${escapeHtml(metric.bestHint)}. Крупная цифра — медиана группы.</p>
    </header>
    <div class="history-seg-bar" aria-hidden="true">${bar}</div>
    <div class="history-seg-groups">${cards}</div>
  </article>`;
}

function historyDistCorrPhrase(r, n) {
  if (n < 3) return "Нужно минимум 3 сделки с длительностью и этой метрикой.";
  if (r == null) return "Длительность или метрика почти не меняется, связь не считается.";
  const abs = Math.abs(r);
  const rText = `${r > 0 ? "+" : ""}${r.toFixed(2)}`;
  let strength = "Сильная";
  if (abs < 0.2) strength = "Слабая";
  else if (abs < 0.4) strength = "Умеренная";
  else if (abs < 0.7) strength = "Заметная";
  let direction = "без выраженного наклона";
  if (r >= 0.1) direction = "значение растёт вместе с длительностью";
  else if (r <= -0.1) direction = "значение снижается у более долгих сделок";
  const caution = n < 12 ? " Сделок мало, оценка грубая." : "";
  return `${strength} связь, r = ${rText}: ${direction}.${caution}`;
}

function historyDistScatterSvg(pairs, metric) {
  if (pairs.length < 3) return "";
  const W = 360;
  const H = 168;
  const padL = 46;
  const padR = 10;
  const padT = 12;
  const padB = 24;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  let xMin = pairs[0].x;
  let xMax = pairs[0].x;
  for (const point of pairs) {
    if (point.x < xMin) xMin = point.x;
    if (point.x > xMax) xMax = point.x;
  }
  const yDesc = distDescribe(
    pairs.map((point) => ({ y: point.y })),
    "y"
  );
  let yMin = yDesc.whiskerLo;
  let yMax = yDesc.whiskerHi;
  if (yMax === yMin) {
    yMin -= 1;
    yMax += 1;
  } else {
    const padY = (yMax - yMin) * 0.1;
    yMin -= padY;
    yMax += padY;
  }
  const xOf = (x) => padL + distScale(x, xMin, xMax) * plotW;
  const yOf = (y) => padT + (1 - distScale(y, yMin, yMax)) * plotH;
  const clipId = `hd-clip-${metric.key}`;
  const parts = [
    `<defs><clipPath id="${clipId}"><rect x="${padL}" y="${padT}" width="${distNum(plotW)}" height="${distNum(plotH)}"/></clipPath></defs>`,
    `<line x1="${padL}" y1="${padT + plotH}" x2="${padL + plotW}" y2="${padT + plotH}" stroke="#2a3648"/>`,
    `<line x1="${padL}" y1="${padT}" x2="${padL}" y2="${padT + plotH}" stroke="#2a3648"/>`,
  ];
  if (yMin < 0 && yMax > 0) {
    const zy = yOf(0);
    parts.push(`<line x1="${padL}" y1="${distNum(zy)}" x2="${padL + plotW}" y2="${distNum(zy)}" stroke="#8b9cb3" stroke-dasharray="3 3"/>`);
  }
  const fitPoints = pairs.filter((point) => point.y >= yDesc.whiskerLo && point.y <= yDesc.whiskerHi);
  const regSource = fitPoints.length >= 3 ? fitPoints : pairs;
  const reg = distLinreg(
    regSource.map((point) => point.x),
    regSource.map((point) => point.y)
  );
  const sample = distSamplePairs(pairs, 220);
  parts.push(`<g clip-path="url(#${clipId})">`);
  if (reg) {
    const y1 = reg.intercept + reg.slope * xMin;
    const y2 = reg.intercept + reg.slope * xMax;
    parts.push(
      `<line x1="${distNum(xOf(xMin))}" y1="${distNum(yOf(y1))}" x2="${distNum(xOf(xMax))}" y2="${distNum(yOf(y2))}" stroke="${metric.color}" stroke-width="1.6"/>`
    );
  }
  for (const point of sample) {
    const outside = point.y < yMin || point.y > yMax;
    const y = Math.min(yMax, Math.max(yMin, point.y));
    const title = `${point.pair} · ${historyDistFormat(metric.kind, point.y)} · ${fmtHistoryDuration(point.x)}`;
    parts.push(
      `<circle cx="${distNum(xOf(point.x))}" cy="${distNum(yOf(y))}" r="${outside ? 3 : 2.4}" fill="${outside ? "#f59e0b" : metric.color}" fill-opacity="${outside ? "0.95" : "0.55"}"><title>${escapeHtml(title)}</title></circle>`
    );
  }
  parts.push(`</g>`);
  parts.push(`<text x="${padL - 4}" y="${padT + 8}" fill="#8b9cb3" font-size="10" text-anchor="end">${escapeHtml(historyDistTick(metric.kind, yMax))}</text>`);
  parts.push(`<text x="${padL - 4}" y="${padT + plotH}" fill="#8b9cb3" font-size="10" text-anchor="end">${escapeHtml(historyDistTick(metric.kind, yMin))}</text>`);
  parts.push(`<text x="${padL}" y="${H - 6}" fill="#8b9cb3" font-size="10" text-anchor="start">${escapeHtml(fmtHistoryDuration(xMin))}</text>`);
  parts.push(`<text x="${W - padR}" y="${H - 6}" fill="#8b9cb3" font-size="10" text-anchor="end">${escapeHtml(fmtHistoryDuration(xMax))}</text>`);
  return `<svg viewBox="0 0 ${W} ${H}" class="history-dist-svg" role="img" aria-label="${escapeHtml(`Корреляция «${metric.title}» и длительности`)}">${parts.join("")}</svg>`;
}

function historyDistBucketHtml(buckets, metric) {
  let maxAbs = 0;
  for (const bucket of buckets) {
    if (Number.isFinite(bucket.value)) maxAbs = Math.max(maxAbs, Math.abs(bucket.value));
  }
  return buckets
    .map((bucket) => {
      const mag = maxAbs > 0 && Number.isFinite(bucket.value) ? (Math.abs(bucket.value) / maxAbs) * 50 : 0;
      const positive = !(bucket.value < 0);
      const left = positive ? 50 : 50 - mag;
      return `<div class="history-corr-bucket">
        <div class="history-corr-bucket-name">${escapeHtml(bucket.label)} <span class="muted">${escapeHtml(bucket.span)}</span></div>
        <div class="history-corr-track">
          <span class="history-corr-zero"></span>
          <span class="history-corr-bar ${positive ? "is-pos" : "is-neg"}" style="left:${distNum(left)}%;width:${distNum(mag)}%"></span>
        </div>
        <div class="history-corr-bucket-val ${metric.kind === "usd" ? pnlClass(bucket.value) : ""}">${escapeHtml(historyDistFormat(metric.kind, bucket.value))}</div>
      </div>`;
    })
    .join("");
}

function historyDistCorrHtml(metric, points) {
  const pairs = [];
  for (const row of points) {
    if (Number.isFinite(row[metric.key]) && Number.isFinite(row.durationMs)) {
      pairs.push({ x: row.durationMs, y: row[metric.key], pair: row.pair, when: row.when });
    }
  }
  pairs.sort((a, b) => a.x - b.x || a.y - b.y);
  const r = pairs.length >= 3 ? distPearson(pairs.map((point) => point.x), pairs.map((point) => point.y)) : null;
  let body = `<p class="history-corr-phrase">${escapeHtml(historyDistCorrPhrase(r, pairs.length))}</p>`;
  if (pairs.length >= 3) {
    body += historyDistScatterSvg(pairs, metric);
    const bands = distTertileBands(
      pairs.map((point) => ({ durationMs: point.x, y: point.y })),
      "durationMs"
    );
    if (bands) {
      const buckets = [
        ["Короткие", bands.low],
        ["Средние", bands.mid],
        ["Долгие", bands.high],
      ].map(([label, group]) => ({
        label: `${label} · ${group.length}`,
        span: fmtHistoryDuration(distMedianOf(group, "durationMs")),
        value: distMedianOf(group, "y"),
      }));
      body += `<div class="history-corr-buckets">${historyDistBucketHtml(buckets, metric)}</div>`;
    }
    const shown = Math.min(pairs.length, 220);
    if (shown < pairs.length) {
      body += `<p class="muted history-dist-note">На диаграмме ${shown} точек из ${pairs.length}. Коэффициент r по всем парам.</p>`;
    }
  }
  return `<article class="history-corr-card"><h4>${escapeHtml(metric.title)}</h4>${body}</article>`;
}

function historyDistHtml(points) {
  const head = `<header class="history-dist-head">
      <h3 class="history-dist-title">Распределение данных</h3>
      <p class="muted history-dist-lead">Все сделки текущего фильтра, не только эта страница. Выброс — значение за пределами типичного разброса между 25-м и 75-м процентилями.</p>
    </header>`;
  if (!points.length) {
    return `${head}<p class="history-dist-empty">Нет закрытых сделок по выбранным фильтрам.</p>`;
  }
  const cards = HISTORY_DIST_METRICS.map((metric) => historyDistCardHtml(metric, points)).join("");
  const segments = HISTORY_DIST_METRICS.filter((metric) => metric.segment)
    .map((metric) => historyDistSegmentHtml(metric, points))
    .join("");
  const corrs = HISTORY_DIST_METRICS.filter((metric) => metric.segment)
    .map((metric) => historyDistCorrHtml(metric, points))
    .join("");
  return `${head}
    <div class="history-dist-grid">${cards}</div>
    <h3 class="history-dist-subtitle">Сегменты</h3>
    <p class="muted history-dist-lead">Три группы с равным числом сделок: лучшие, средние и худшие.</p>
    <div class="history-seg-list">${segments}</div>
    <h3 class="history-dist-subtitle">Корреляция с длительностью</h3>
    <p class="muted history-dist-lead">Связь трёх метрик с продолжительностью сделки. r — коэффициент Пирсона от −1 до +1. Полосы — медиана коротких, средних и долгих сделок. Жёлтые точки — выбросы за пределами типичного разброса.</p>
    <div class="history-corr-grid">${corrs}</div>`;
}

function renderHistoryDistribution(entries) {
  const root = $("history-dist");
  if (!root) return;
  root.innerHTML = historyDistHtml(historyDistPoints(entries || []));
}
/* history-dist-end */

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

function catalogForHistoryScope(catalog, scope) {
  const list = catalog || strategyCatalog || [];
  if (scope === "htf1h") return list.filter((s) => isHtfStrategy(s) || isHtfStrategyId(s.id));
  if (scope === "test_strategy") {
    return list.filter((s) => isTestLaneStrategy(s) && !isHtfStrategyId(s.id));
  }
  if (scope === "strategy") {
    return list.filter(
      (s) => !isTestLaneStrategy(s) && !isHtfStrategy(s) && !isHtfStrategyId(s.id)
    );
  }
  return list;
}

function updateHistoryStrategyFilterOptions(catalog) {
  const sel = $("history-strategy-filter");
  if (!sel) return;
  const prev = historyStrategyFilter;
  const list = catalogForHistoryScope(catalog || strategyCatalog, historyScope);
  sel.innerHTML =
    '<option value="all">Все стратегии</option>' +
    list
      .map(
        (s) =>
          `<option value="${escapeHtml(s.id)}">${escapeHtml(strategyCatalogLabel(s))}</option>`
      )
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
      historyScope !== "all" &&
        historyScope !== "strategy" &&
        historyScope !== "test_strategy" &&
        historyScope !== "htf1h"
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
  const maxPage = Math.max(1, Math.ceil(entries.length / HISTORY_PAGE_SIZE) || 1);
  if (historyPage > maxPage) historyPage = maxPage;
  renderHistoryTable(entries);
  const statusEl = $("history-status");
  if (statusEl) {
    const warn =
      historyDataCache.loadErrors?.length > 0
        ? ` · ${historyDataCache.loadErrors.join(" · ")}`
        : "";
    const pages = Math.max(1, Math.ceil(entries.length / HISTORY_PAGE_SIZE) || 1);
    const pageNote =
      entries.length > HISTORY_PAGE_SIZE ? ` · стр. ${historyPage}/${pages}` : "";
    let grossSum = 0;
    let netSum = 0;
    for (const e of entries) {
      grossSum += historyEntryGrossPnl(e);
      netSum += historyEntryNetPnl(e);
    }
    const pnlNote =
      entries.length > 0
        ? ` · PnL ${fmtUsdSigned(grossSum, 2)} · с комиссией ${fmtUsdSigned(netSum, 2)}`
        : "";
    statusEl.textContent = `Всего ${entries.length} сделок${pnlNote}${pageNote}${warn}`;
  }
}

async function loadHistory() {
  const statusEl = $("history-status");
  const bodyEl = $("history-table-body");
  const cardsEl = $("history-cards");

  if (historyDataCache || statsDataCache) {
    historyDataCache = historyDataCache || statsDataCache;
    updateHistoryStrategyFilterOptions(historyDataCache.catalog);
    updateHistoryPairSuggestions(collectUniquePairs(historyDataCache));
    renderHistoryView();
  } else if (statusEl) {
    statusEl.textContent = "Загрузка…";
    if (bodyEl) bodyEl.innerHTML = "";
    if (cardsEl) cardsEl.innerHTML = "";
  }

  try {
    historyPage = historyDataCache ? historyPage : 1;
    historyDataCache = await prefetchStatsBundle();
    updateHistoryStrategyFilterOptions(historyDataCache.catalog);
    updateHistoryPairSuggestions(collectUniquePairs(historyDataCache));
    renderHistoryView();
  } catch (e) {
    if (e.message === "auth") logout();
    else if (statusEl && !historyDataCache) statusEl.textContent = formatApiError(e.message);
  }
}

function openHistory() {
  if (!userMayUseBlock("history")) return;
  closeMobileMenu();
  hideMainViewsForOverlay();
  const view = $("history-view");
  view?.classList.remove("hidden");
  view?.setAttribute("aria-hidden", "false");
  renderHistoryExportColumns();
  if (historyDataCache || statsDataCache) {
    historyDataCache = historyDataCache || statsDataCache;
    updateHistoryStrategyFilterOptions(historyDataCache.catalog);
    updateHistoryPairSuggestions(collectUniquePairs(historyDataCache));
    renderHistoryView();
  }
  loadHistory();
}

function closeHistory() {
  $("history-view")?.classList.add("hidden");
  $("history-view")?.setAttribute("aria-hidden", "true");
  $("dashboard-view")?.classList.remove("hidden");
}

function setHistoryScope(scope) {
  historyScope = STATS_SCOPE_META[scope] ? scope : "all";
  if (
    historyScope !== "all" &&
    historyScope !== "strategy" &&
    historyScope !== "test_strategy" &&
    historyScope !== "htf1h"
  ) {
    historyStrategyFilter = "all";
  }
  if (historyDataCache) updateHistoryStrategyFilterOptions(historyDataCache.catalog);
  historyPage = 1;
  renderHistoryView();
}

function setHistoryPeriod(period) {
  historyPeriod = period || "all";
  historyDateFrom = "";
  historyDateTo = "";
  historyPage = 1;
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
  historyPage = 1;
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
    historyPage = 1;
    renderHistoryView();
  });
  $("history-pair-filter")?.addEventListener("input", (e) => {
    historyPairFilter = e.target.value.trim();
    historyPage = 1;
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
  $("history-page-prev")?.addEventListener("click", () => setHistoryPage(historyPage - 1));
  $("history-page-next")?.addEventListener("click", () => setHistoryPage(historyPage + 1));
}

/* --- Analytics: Графики сделок --- */
const CHART_SETUP_BARS = 60;
const CHART_TAIL_CONTEXT = 8;
const CHARTS_PAGE_SIZE = 40;
const CHARTS_ZIP_MAX = 2000;

let chartsPeriod = "all";
let chartsDateFrom = "";
let chartsDateTo = "";
let chartsSide = "all";
let chartsBot = "all";
let chartsStrategy = "all";
let chartsPage = 1;
let chartsClosed = [];
let chartsOpen = [];
let chartsZipBusy = false;
let chartsLoadToken = 0;
const chartsExpanded = new Set();
const chartsModelCache = new Map();
const chartsPngCache = new Map();
const chartsPhoneBlobs = new Map();
const chartsJsonCache = new Map();
const chartObjectUrls = new Map();
const chartPaintGen = new Map();

const ZIP_CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function closeCharts() {
  $("charts-view")?.classList.add("hidden");
  $("charts-view")?.setAttribute("aria-hidden", "true");
  $("dashboard-view")?.classList.remove("hidden");
}

function openCharts() {
  if (!userMayUseBlock("history")) return;
  closeMobileMenu();
  hideMainViewsForOverlay();
  const view = $("charts-view");
  view?.classList.remove("hidden");
  view?.setAttribute("aria-hidden", "false");
  seedChartsFromKnownTrades();
  loadCharts();
}

function chartErrorText(err) {
  const raw = String(err?.message || err || "ошибка");
  if (raw === "auth") return "нужен вход";
  try {
    const parsed = JSON.parse(raw);
    if (parsed?.error) return String(parsed.error);
  } catch {
    /* plain text */
  }
  return raw.slice(0, 200);
}

function chartEntryKey(entry) {
  const id = entry.trade.trade_id ?? entry.trade.id ?? entry.trade.open_date ?? "";
  return `${entry.bot}:${id}:${entry.open ? "open" : "closed"}`;
}

function chartTradePnl(entry) {
  const t = entry.trade;
  if (entry.open) {
    const abs = Number(t.profit_abs ?? t.total_profit_abs ?? 0);
    let pct = Number(t.profit_pct ?? 0);
    if (Math.abs(pct) <= 1 && pct !== 0) pct *= 100;
    return { abs, pct };
  }
  return closedTradePnl(t);
}

function chartTimeframe(trade) {
  if (isHtfTrade(trade)) return { interval: "60", tfMs: 60 * 60 * 1000, label: "1ч" };
  return { interval: "5", tfMs: 5 * 60 * 1000, label: "5м" };
}

function ingestClosedChartTrades(data) {
  const out = [];
  const groups = [
    ["finder", data?.finderTrades],
    ["strategy", data?.stratTrades],
    ["grid", data?.gridTrades],
  ];
  for (const [bot, list] of groups) {
    for (const trade of list || []) {
      if (!trade?.pair || trade.is_open || isRetiredTrade(trade)) continue;
      out.push({ bot, trade, open: false });
    }
  }
  return out;
}

function ingestOpenChartTrades(bundle) {
  const out = [];
  const bots = bundle?.bots || {};
  for (const bot of Object.keys(bots)) {
    for (const trade of bots[bot]?.trades || []) {
      if (!trade?.pair || !trade.open_date || isRetiredTrade(trade)) continue;
      out.push({ bot, trade, open: true });
    }
  }
  return out;
}

function chartUniverse() {
  const map = new Map();
  for (const entry of chartsClosed) {
    const id = entry.trade.trade_id ?? entry.trade.id ?? entry.trade.open_date;
    map.set(`${entry.bot}:${id}`, entry);
  }
  for (const entry of chartsOpen) {
    const id = entry.trade.trade_id ?? entry.trade.id ?? entry.trade.open_date;
    const key = `${entry.bot}:${id}`;
    if (!map.has(key)) map.set(key, entry);
  }
  return [...map.values()];
}

function chartEntryInPeriod(entry) {
  const custom = Boolean(chartsDateFrom || chartsDateTo);
  if (entry.open && !custom && chartsPeriod !== "yesterday") return true;
  const ms = entry.open
    ? parseAppInstant(entry.trade.open_date)?.getTime() || 0
    : tradeCloseMs(entry.trade);
  return isInPeriodMs(ms, chartsPeriod, chartsDateFrom, chartsDateTo);
}

function chartEntryTime(entry) {
  if (entry.open) return parseAppInstant(entry.trade.open_date)?.getTime() || 0;
  return tradeCloseMs(entry.trade);
}

function normalizeChartsBot(bot) {
  const raw = String(bot || "all").toLowerCase();
  if (raw === "strategy" || raw === "grid") return raw;
  return "all";
}

function filterChartEntries(strategy) {
  let list = chartUniverse().filter(chartEntryInPeriod);
  const bot = chartsBot === "grid" && !userMayUseBlock("grid") ? "all" : chartsBot;
  if (bot === "strategy" || bot === "grid") list = list.filter((e) => e.bot === bot);
  if (chartsSide === "long") list = list.filter((e) => !e.trade.is_short);
  else if (chartsSide === "short") list = list.filter((e) => Boolean(e.trade.is_short));
  if (strategy && strategy !== "all") {
    list = list.filter((e) => tradeSourceId(e.trade, e.bot) === strategy);
  }
  list.sort((a, b) => {
    if (a.open !== b.open) return a.open ? -1 : 1;
    return chartEntryTime(b) - chartEntryTime(a);
  });
  return list;
}

function getFilteredChartEntries() {
  return filterChartEntries(chartsStrategy);
}

function findChartEntry(key) {
  return chartUniverse().find((entry) => chartEntryKey(entry) === key) || null;
}

function renderChartsStrategyPills(entries) {
  const el = $("charts-strategy-pills");
  if (!el) return;
  const map = new Map();
  for (const entry of entries) {
    const id = tradeSourceId(entry.trade, entry.bot);
    if (!map.has(id)) map.set(id, tradeSourceLabel(entry.trade, entry.bot));
  }
  if (chartsStrategy !== "all" && !map.has(chartsStrategy)) chartsStrategy = "all";
  const sig = `${chartsStrategy}|${[...map.keys()].sort().join("\n")}`;
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  const items = [...map.entries()].sort((a, b) => a[1].localeCompare(b[1], "ru"));
  const allActive = chartsStrategy === "all" ? " is-active" : "";
  el.innerHTML =
    `<button type="button" class="btn btn-sm charts-pill${allActive}" data-charts-strategy="all">Все</button>` +
    items
      .map(([id, label]) => {
        const active = chartsStrategy === id ? " is-active" : "";
        return `<button type="button" class="btn btn-sm charts-pill${active}" data-charts-strategy="${escapeHtml(id)}">${escapeHtml(label)}</button>`;
      })
      .join("");
}

function chartsCells(entry) {
  const t = entry.trade;
  const pnl = chartTradePnl(entry);
  const botLabel = historyBotLabel(entry);
  const side = t.is_short ? "SHORT" : "LONG";
  const source = tradeSourceLabel(t, entry.bot);
  const pnlClassName = pnlClass(pnl.abs);
  const when = entry.open ? fmtAppDateShort(t.open_date) : fmtTradeDate(t);
  const exit = entry.open ? "Открыта" : exitReasonLabel(t.exit_reason);
  return { when, botLabel, side, source, pnl, pnlClassName, exit, t };
}

function chartPanelHtml(key) {
  return `<div class="trade-chart-panel">
    <img class="trade-chart-canvas trade-chart-img" data-chart-img="${escapeHtml(key)}" alt="График сделки" />
    <div class="trade-chart-actions">
      <button type="button" class="btn btn-sm primary" data-chart-png="${escapeHtml(key)}">Скачать PNG</button>
      <span class="muted trade-chart-status" data-chart-status="${escapeHtml(key)}"></span>
    </div>
  </div>`;
}

function renderChartsTable(entries) {
  const body = $("charts-table-body");
  const cards = $("charts-cards");
  const start = (chartsPage - 1) * CHARTS_PAGE_SIZE;
  const pageEntries = entries.slice(start, start + CHARTS_PAGE_SIZE);
  if (body) {
    if (!entries.length) {
      body.innerHTML = '<tr class="stats-history-empty"><td colspan="9">Нет сделок по выбранным фильтрам</td></tr>';
    } else {
      body.innerHTML = pageEntries
        .map((entry) => {
          const key = chartEntryKey(entry);
          const open = chartsExpanded.has(key);
          const c = chartsCells(entry);
          const botClass = historyBotClass(entry);
          const detail = open
            ? `<tr class="charts-detail"><td colspan="9">${chartPanelHtml(key)}</td></tr>`
            : "";
          return `<tr class="history-row charts-row ${botClass}${entry.open ? " charts-row-open" : ""}">
        <td>${c.when}</td>
        <td><span class="history-bot-badge">${c.botLabel}</span></td>
        <td class="history-pair-cell">${escapeHtml(c.t.pair || "—")}</td>
        <td><span class="history-side-badge side-${c.t.is_short ? "short" : "long"}">${c.side}</span></td>
        <td>${escapeHtml(c.source)}</td>
        <td class="history-sltp-cell">${escapeHtml(historySltpCell(c.t))}</td>
        <td class="history-pnl-cell ${c.pnlClassName}">${fmtUsdSigned(c.pnl.abs, 2)}<br><span class="muted history-pnl-pct">${fmtPct(c.pnl.pct)}</span></td>
        <td>${escapeHtml(c.exit)}</td>
        <td class="charts-action-cell"><button type="button" class="btn btn-sm ${open ? "ghost" : "primary"}" data-chart-toggle="${escapeHtml(key)}">${open ? "Скрыть" : "График"}</button></td>
      </tr>${detail}`;
        })
        .join("");
    }
  }
  if (cards) {
    if (!entries.length) {
      cards.innerHTML = '<p class="history-cards-empty muted">Нет сделок по выбранным фильтрам</p>';
    } else {
      cards.innerHTML = pageEntries
        .map((entry) => {
          const key = chartEntryKey(entry);
          const open = chartsExpanded.has(key);
          const c = chartsCells(entry);
          const botClass = historyBotClass(entry);
          const panel = open ? chartPanelHtml(key) : "";
          return `<article class="history-card charts-card ${c.pnlClassName} ${botClass}${entry.open ? " is-open" : ""}">
        <div class="history-card-top">
          <div class="history-card-main">
            <strong class="history-card-pair">${escapeHtml(c.t.pair || "—")}</strong>
            <span class="history-card-meta">${c.when} · ${c.botLabel} · ${escapeHtml(c.source)}</span>
          </div>
          <div class="history-card-pnl ${c.pnlClassName}">
            <span>${fmtUsdSigned(c.pnl.abs, 2)}</span>
            <span class="history-card-pct muted">${fmtPct(c.pnl.pct)}</span>
          </div>
        </div>
        <div class="history-card-tags">
          <span class="history-chip side-${c.t.is_short ? "short" : "long"}">${c.side}</span>
          <span class="history-chip">${escapeHtml(c.exit)}</span>
          <span class="history-chip">${escapeHtml(historySltpCell(c.t))}</span>
        </div>
        <div class="trade-chart-actions">
          <button type="button" class="btn btn-sm ${open ? "ghost" : "primary"}" data-chart-toggle="${escapeHtml(key)}">${open ? "Скрыть" : "График"}</button>
        </div>
        ${panel}
      </article>`;
        })
        .join("");
    }
  }
}

function renderChartsPagination(total) {
  const wrap = $("charts-pagination");
  const info = $("charts-page-info");
  const pages = Math.max(1, Math.ceil(total / CHARTS_PAGE_SIZE) || 1);
  if (chartsPage > pages) chartsPage = pages;
  const show = total > CHARTS_PAGE_SIZE;
  if (wrap) {
    wrap.classList.toggle("hidden", !show);
    wrap.hidden = !show;
  }
  if (info) {
    const from = total ? (chartsPage - 1) * CHARTS_PAGE_SIZE + 1 : 0;
    const to = Math.min(chartsPage * CHARTS_PAGE_SIZE, total);
    info.textContent = show ? `${from}–${to} из ${total}` : "";
  }
  const prev = $("charts-page-prev");
  const next = $("charts-page-next");
  if (prev) prev.disabled = chartsPage <= 1;
  if (next) next.disabled = chartsPage >= pages;
}

function syncChartsDateInputs() {
  const from = $("charts-date-from");
  const to = $("charts-date-to");
  if (from && from.value !== chartsDateFrom) from.value = chartsDateFrom;
  if (to && to.value !== chartsDateTo) to.value = chartsDateTo;
}

function applyChartsDateRange() {
  chartsDateFrom = $("charts-date-from")?.value || "";
  chartsDateTo = $("charts-date-to")?.value || "";
  chartsPage = 1;
  renderChartsView();
}

function renderChartsView() {
  const customRange = Boolean(chartsDateFrom || chartsDateTo);
  document.querySelectorAll("[data-charts-period]").forEach((btn) => {
    btn.classList.toggle(
      "is-active",
      !customRange && (btn.dataset.chartsPeriod || "all") === chartsPeriod,
    );
  });
  syncChartsDateInputs();
  document.querySelectorAll("[data-charts-side]").forEach((btn) => {
    btn.classList.toggle("is-active", (btn.dataset.chartsSide || "all") === chartsSide);
  });
  if (chartsBot === "grid" && !userMayUseBlock("grid")) chartsBot = "all";
  document.querySelectorAll("[data-charts-bot]").forEach((btn) => {
    btn.classList.toggle("is-active", (btn.dataset.chartsBot || "all") === chartsBot);
  });
  const periodEntries = filterChartEntries("all");
  renderChartsStrategyPills(periodEntries);
  const entries = chartsStrategy === "all" ? periodEntries : filterChartEntries(chartsStrategy);
  const pages = Math.max(1, Math.ceil(entries.length / CHARTS_PAGE_SIZE) || 1);
  if (chartsPage > pages) chartsPage = pages;
  renderChartsTable(entries);
  renderChartsPagination(entries.length);
  const status = $("charts-status");
  if (status && !chartsZipBusy) {
    const openN = entries.filter((e) => e.open).length;
    let pnl = 0;
    for (const entry of entries) {
      if (!entry.open) pnl += chartTradePnl(entry).abs;
    }
    const pnlNote = entries.length ? ` · PnL закрытых ${fmtUsdSigned(pnl, 2)}` : "";
    const openNote = openN ? ` · открытых ${openN}` : "";
    const pageNote = entries.length > CHARTS_PAGE_SIZE ? ` · стр. ${chartsPage}/${pages}` : "";
    status.textContent = `Всего ${entries.length} сделок${openNote}${pnlNote}${pageNote}`;
  }
  const start = (chartsPage - 1) * CHARTS_PAGE_SIZE;
  const phone = isChartsPhone();
  for (const entry of entries.slice(start, start + CHARTS_PAGE_SIZE)) {
    const key = chartEntryKey(entry);
    if (!chartsExpanded.has(key)) continue;
    if (phone) {
      const blob = chartsPhoneBlobs.get(phoneChartBlobKey(entry));
      if (blob) showChartImage(key, blob);
    } else {
      const mem = chartsPngCache.get(chartPngMemKey(entry));
      if (mem?.blob) showChartImage(key, mem.blob);
    }
  }
}

function seedChartsFromKnownTrades() {
  const stats = historyDataCache || statsDataCache;
  if (stats) chartsClosed = ingestClosedChartTrades(stats);
  if (chartsClosed.length || chartsOpen.length) renderChartsView();
}

async function loadCharts() {
  const token = ++chartsLoadToken;
  const status = $("charts-status");
  seedChartsFromKnownTrades();
  if (!chartsClosed.length && !chartsOpen.length && status) status.textContent = "Загрузка сделок…";
  const openPromise = pairConfigApi("/open-trades").catch(() => null);
  try {
    const stats = await prefetchStatsBundle();
    if (token !== chartsLoadToken) return;
    for (const key of [...chartsModelCache.keys()]) {
      if (String(key).endsWith(":open")) chartsModelCache.delete(key);
    }
    chartsClosed = ingestClosedChartTrades(stats);
    renderChartsView();
  } catch (err) {
    if (err?.message === "auth") {
      logout();
      return;
    }
    if (status && !chartsClosed.length && !chartsOpen.length) status.textContent = chartErrorText(err);
  }
  try {
    const openBundle = await openPromise;
    if (token !== chartsLoadToken || !openBundle) return;
    chartsOpen = ingestOpenChartTrades(openBundle);
    renderChartsView();
  } catch {
    /* открытые сделки не блокируют список */
  }
}

function chartNodes(attr, key) {
  return [...document.querySelectorAll(`[${attr}]`)].filter((node) => node.getAttribute(attr) === key);
}

function pickVisibleChartCanvas(key) {
  return chartNodes("data-chart-canvas", key).find((el) => el.getClientRects().length > 0) || null;
}

function candleIndexAt(candles, ms) {
  if (!candles.length) return -1;
  if (!ms || ms < candles[0].t) return 0;
  let best = 0;
  for (let i = 0; i < candles.length; i += 1) {
    if (candles[i].t <= ms) best = i;
    else break;
  }
  return best;
}

function buildTradeChartModel(entry, payload, ctx) {
  const head = Array.isArray(payload?.candles) ? payload.candles : [];
  const tailRaw = Array.isArray(payload?.tail) ? payload.tail : [];
  const headLast = head.length ? head[head.length - 1].t : 0;
  const tail = tailRaw.filter((candle) => candle.t > headLast);
  const gapAt = tail.length ? head.length : null;
  const candles = tail.length ? head.concat(tail) : head.slice();
  if (!candles.length) throw new Error("нет свечей по этой паре");
  const entryIdx = candleIndexAt(candles, ctx.openMs);
  let exitIdx = ctx.closeMs ? candleIndexAt(candles, ctx.closeMs) : candles.length - 1;
  if (exitIdx < entryIdx) exitIdx = Math.max(entryIdx, candles.length - 1);
  const t = entry.trade;
  const pnl = chartTradePnl(entry);
  return {
    candles,
    gapAt,
    entryIdx,
    exitIdx,
    shadeFrom: Math.max(0, entryIdx - CHART_SETUP_BARS),
    shadeTo: exitIdx,
    strategy: tradeSourceLabel(t, entry.bot),
    side: t.is_short ? "SHORT" : "LONG",
    pair: t.pair || "—",
    tfLabel: ctx.tfLabel,
    pnlAbs: pnl.abs,
    pnlPct: pnl.pct,
    openRate: Number(t.open_rate) || null,
    closeRate: entry.open ? Number(t.current_rate) || null : Number(t.close_rate) || null,
    openTrade: !!entry.open,
    truncated: !!payload?.truncated || gapAt != null,
  };
}

async function loadChartModel(entry) {
  const key = chartEntryKey(entry);
  if (chartsModelCache.has(key)) return chartsModelCache.get(key);
  const t = entry.trade;
  const tf = chartTimeframe(t);
  const openMs = parseAppInstant(t.open_date)?.getTime();
  if (!openMs) throw new Error("нет времени входа");
  const closeMs = entry.open ? null : tradeCloseMs(t);
  if (!entry.open && !closeMs) throw new Error("нет времени выхода");
  const endMs = (closeMs || Date.now()) + CHART_TAIL_CONTEXT * tf.tfMs;
  const params = new URLSearchParams({
    pair: t.pair || "",
    interval: tf.interval,
    start: String(Math.floor(openMs - (CHART_SETUP_BARS + 3) * tf.tfMs)),
    end: String(Math.floor(endMs)),
  });
  const data = await pairConfigApi(`/market-klines?${params}`);
  const model = buildTradeChartModel(entry, data, {
    tfLabel: tf.label,
    openMs,
    closeMs,
  });
  chartsModelCache.set(key, model);
  return model;
}

function chartFitText(ctx, text, maxW) {
  const raw = String(text || "");
  if (ctx.measureText(raw).width <= maxW) return raw;
  let s = raw;
  while (s.length > 1 && ctx.measureText(`${s}…`).width > maxW) s = s.slice(0, -1);
  return `${s}…`;
}

function chartRoundRect(ctx, x, y, w, h, r) {
  const radius = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}

function fmtChartTime(ms) {
  return new Date(ms).toLocaleString("ru-RU", {
    timeZone: APP_TIMEZONE,
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function drawTradeChart(ctx, model, width, height) {
  const candles = model.candles || [];
  const compact = width < 640;
  const padX = compact ? 10 : 16;
  const titlePx = compact ? 13 : 16;
  const badgePx = compact ? 11 : 13;
  const subPx = compact ? 11 : 12;
  const axisPx = compact ? 10 : 11;
  const axisW = compact ? 64 : 74;
  const plotX = compact ? 8 : 12;
  const plotY = compact ? 48 : 64;
  const plotBottom = compact ? 20 : 28;
  const titleY = compact ? 16 : 22;
  const subY = compact ? 34 : 46;
  const badgeH = compact ? 22 : 26;
  const badgeY = compact ? 6 : 9;
  const badgeTextY = compact ? 17 : 23;
  const tickGap = compact ? 54 : 72;
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#121a24";
  ctx.fillRect(0, 0, width, height);
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.font = `600 ${titlePx}px Segoe UI, system-ui, sans-serif`;
  ctx.fillStyle = "#e8eef7";
  const sideText = model.side === "SHORT" ? "ШОРТ" : "ЛОНГ";
  ctx.font = `700 ${badgePx}px Segoe UI, system-ui, sans-serif`;
  const badgeW = ctx.measureText(sideText).width + (compact ? 16 : 22);
  ctx.font = `600 ${titlePx}px Segoe UI, system-ui, sans-serif`;
  ctx.fillText(chartFitText(ctx, model.strategy || "Стратегия", Math.max(80, width - badgeW - padX * 2 - 8)), padX, titleY);

  const short = model.side === "SHORT";
  const loss = Number(model.pnlAbs) < 0;
  const profit = Number(model.pnlAbs) > 0;
  const ink = loss ? "#fca5a5" : profit ? "#6ee7b7" : "#cbd5e1";
  const bx = width - padX - badgeW;
  ctx.font = `700 ${badgePx}px Segoe UI, system-ui, sans-serif`;
  ctx.fillStyle = short ? "rgba(239,68,68,0.2)" : "rgba(16,185,129,0.2)";
  chartRoundRect(ctx, bx, badgeY, badgeW, badgeH, 13);
  ctx.fill();
  ctx.strokeStyle = short ? "#f87171" : "#34d399";
  ctx.lineWidth = 1;
  ctx.stroke();
  ctx.fillStyle = short ? "#fca5a5" : "#6ee7b7";
  ctx.textAlign = "center";
  ctx.fillText(sideText, bx + badgeW / 2, badgeTextY);
  ctx.textAlign = "left";

  const pnlBit = `${fmtUsdSigned(model.pnlAbs, 2)} (${fmtPct(model.pnlPct)})`;
  let sub = `${model.pair} · ${model.tfLabel} · ${pnlBit}`;
  if (model.openTrade) sub += " · открыта";
  if (model.truncated) sub += " · середина скрыта";
  ctx.font = `${subPx}px Segoe UI, system-ui, sans-serif`;
  ctx.fillStyle = ink;
  ctx.fillText(chartFitText(ctx, sub, width - padX * 2), padX, subY);

  if (!candles.length) return;
  const plotW = Math.max(40, width - plotX - axisW - (compact ? 6 : 8));
  const plotH = Math.max(80, height - plotY - plotBottom);
  let lo = Infinity;
  let hi = -Infinity;
  for (const candle of candles) {
    lo = Math.min(lo, Number(candle.l));
    hi = Math.max(hi, Number(candle.h));
  }
  for (const price of [model.openRate, model.closeRate]) {
    if (price > 0) {
      lo = Math.min(lo, price);
      hi = Math.max(hi, price);
    }
  }
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return;
  const pad = (hi - lo) * 0.1 || Math.abs(hi) * 0.002 || 1;
  const min = lo - pad;
  const max = hi + pad;
  const yPrice = (price) => plotY + ((max - price) / (max - min)) * plotH;
  const n = candles.length;
  const gapAt = model.gapAt;
  const gapW = gapAt == null ? 0 : 28;
  const slot = (plotW - gapW) / n;
  const xMid = (i) => plotX + (gapAt != null && i >= gapAt ? gapW : 0) + i * slot + slot / 2;
  const xLeft = (i) => xMid(i) - slot / 2;

  const s0 = Math.max(0, Math.min(model.shadeFrom ?? 0, n - 1));
  const s1 = Math.max(s0, Math.min(model.shadeTo ?? n - 1, n - 1));
  const zone = loss ? "rgba(239,68,68,0.22)" : profit ? "rgba(16,185,129,0.20)" : "rgba(148,163,184,0.16)";
  const zoneStrong = loss ? "rgba(239,68,68,0.42)" : profit ? "rgba(16,185,129,0.40)" : "rgba(148,163,184,0.30)";
  ctx.fillStyle = zone;
  ctx.fillRect(xLeft(s0), plotY, xMid(s1) + slot / 2 - xLeft(s0), plotH);
  ctx.fillStyle = zoneStrong;
  const strong = new Set([model.entryIdx, model.openTrade ? n - 1 : model.exitIdx]);
  for (const idx of strong) {
    if (idx >= 0 && idx < n) ctx.fillRect(xLeft(idx), plotY, Math.max(slot, 1), plotH);
  }

  ctx.strokeStyle = "rgba(255,255,255,0.06)";
  ctx.lineWidth = 1;
  ctx.font = `${axisPx}px Segoe UI, system-ui, sans-serif`;
  ctx.textAlign = "left";
  for (let i = 0; i <= 3; i += 1) {
    const price = max - ((max - min) * i) / 3;
    const y = yPrice(price);
    ctx.beginPath();
    ctx.moveTo(plotX, y);
    ctx.lineTo(plotX + plotW, y);
    ctx.stroke();
    ctx.fillStyle = "#8b9cb3";
    ctx.textBaseline = "middle";
    ctx.fillText(fmtRate(price), plotX + plotW + 6, y);
  }

  const priceTags = [];
  const hLine = (price, color, label) => {
    if (!(price > 0)) return;
    const y = yPrice(price);
    ctx.save();
    ctx.setLineDash([4, 3]);
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.moveTo(plotX, y);
    ctx.lineTo(plotX + plotW, y);
    ctx.stroke();
    ctx.restore();
    priceTags.push({ y, color, label });
  };
  hLine(model.openRate, "#e8eef7", "вход");
  hLine(model.closeRate, ink, model.openTrade ? "сейчас" : "выход");

  for (let i = 0; i < n; i += 1) {
    const candle = candles[i];
    const x = xMid(i);
    const up = Number(candle.c) >= Number(candle.o);
    ctx.strokeStyle = up ? "#34d399" : "#f87171";
    ctx.fillStyle = ctx.strokeStyle;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, yPrice(candle.h));
    ctx.lineTo(x, yPrice(candle.l));
    ctx.stroke();
    const top = yPrice(Math.max(candle.o, candle.c));
    const bot = yPrice(Math.min(candle.o, candle.c));
    const bw = Math.max(1, Math.min(slot * 0.64, Math.max(slot - 0.3, 1)));
    ctx.fillRect(x - bw / 2, top, bw, Math.max(1, bot - top));
  }

  const vMark = (idx, text) => {
    if (idx < 0 || idx >= n) return;
    const x = xMid(idx);
    ctx.save();
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = "rgba(232,238,247,0.7)";
    ctx.beginPath();
    ctx.moveTo(x, plotY);
    ctx.lineTo(x, plotY + plotH);
    ctx.stroke();
    ctx.restore();
    ctx.font = `600 ${axisPx}px Segoe UI, system-ui, sans-serif`;
    ctx.fillStyle = "#e8eef7";
    ctx.textBaseline = "top";
    const tw = ctx.measureText(text).width;
    let tx = x + 4;
    if (tx + tw > plotX + plotW - 2) tx = Math.max(plotX + 2, x - tw - 4);
    ctx.fillText(text, tx, plotY + 4);
  };
  vMark(model.entryIdx, "вход");
  const endLabel = model.openTrade ? `сейчас ${fmtUsdSigned(model.pnlAbs, 2)}` : `выход ${fmtUsdSigned(model.pnlAbs, 2)}`;
  vMark(model.openTrade ? n - 1 : model.exitIdx, endLabel);

  ctx.font = `${axisPx}px Segoe UI, system-ui, sans-serif`;
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  for (const tag of priceTags) {
    const tw = ctx.measureText(tag.label).width;
    const x = plotX + 6;
    const y = tag.y < plotY + 16 ? tag.y + 14 : tag.y - 5;
    ctx.fillStyle = "rgba(18,26,36,0.88)";
    ctx.fillRect(x - 3, y - 11, tw + 8, 15);
    ctx.fillStyle = tag.color;
    ctx.fillText(tag.label, x, y);
  }

  ctx.fillStyle = ink;
  ctx.font = `${axisPx}px Segoe UI, system-ui, sans-serif`;
  ctx.textBaseline = "bottom";
  ctx.fillText("окно входа", xLeft(s0) + 4, plotY + plotH - 4);

  if (gapAt != null && gapAt > 0 && gapAt < n) {
    ctx.fillStyle = "#8b9cb3";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("···", xMid(gapAt - 1) + slot / 2 + gapW / 2, plotY + plotH / 2);
    ctx.textAlign = "left";
  }

  ctx.strokeStyle = "rgba(255,255,255,0.12)";
  ctx.strokeRect(plotX + 0.5, plotY + 0.5, plotW, plotH);
  ctx.font = `${axisPx}px Segoe UI, system-ui, sans-serif`;
  ctx.fillStyle = "#8b9cb3";
  ctx.textBaseline = "top";
  ctx.textAlign = "left";
  const ticks = [0, model.entryIdx, model.openTrade ? n - 1 : model.exitIdx, n - 1];
  let lastX = -999;
  for (const idx of ticks) {
    if (idx < 0 || idx >= n) continue;
    const x = xMid(idx);
    if (x - lastX < tickGap) continue;
    lastX = x;
    const timeRight = compact ? 62 : 78;
    ctx.fillText(fmtChartTime(candles[idx].t), Math.min(Math.max(plotX, x - 24), width - timeRight), plotY + plotH + 6);
  }
}

function paintTradeChart(canvas, model, cssWidth, cssHeight) {
  const width = cssWidth || Math.max(320, canvas.parentElement?.clientWidth || 960);
  const height = cssHeight || (width < 640 ? 320 : 430);
  const dpr = cssWidth ? 2 : Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  drawTradeChart(ctx, model, width, height);
}

function chartTradeNumericId(entry) {
  const id = Number(entry?.trade?.trade_id ?? entry?.trade?.id);
  return Number.isFinite(id) && id > 0 ? Math.trunc(id) : 0;
}

function chartCacheStamp(entry) {
  const t = entry.trade;
  if (entry.open) {
    const bucket = Math.floor(Date.now() / (15 * 60 * 1000));
    return `open|${t.open_date || ""}|${bucket}`;
  }
  const close = t.close_date || t.close_fill_date || "";
  const rate = Number(t.close_rate || 0).toFixed(6);
  const pnl = Number(t.close_profit_abs ?? t.profit_abs ?? 0).toFixed(4);
  return `closed|${close}|${rate}|${pnl}`;
}

function chartCacheQuery(entry) {
  return new URLSearchParams({
    bot: entry.bot,
    trade_id: String(chartTradeNumericId(entry)),
    stamp: chartCacheStamp(entry),
  }).toString();
}

function chartPngMemKey(entry) {
  return `${chartEntryKey(entry)}|${chartCacheStamp(entry)}`;
}

async function fetchTradeChartPng(entry) {
  if (!chartTradeNumericId(entry)) return null;
  const res = await fetch(`/api/pair-config/trade-chart?${chartCacheQuery(entry)}`, {
    headers: sessionHeader(),
  });
  if (res.status === 404) return null;
  if (res.status === 401) throw new Error("auth");
  if (!res.ok) return null;
  const blob = await res.blob();
  return blob && blob.size > 64 ? blob : null;
}

async function saveTradeChartPng(entry, blob) {
  if (!chartTradeNumericId(entry)) return false;
  const res = await fetch(`/api/pair-config/trade-chart?${chartCacheQuery(entry)}`, {
    method: "PUT",
    headers: { ...sessionHeader(), "Content-Type": "image/png" },
    body: blob,
  });
  if (res.status === 401) throw new Error("auth");
  return res.ok;
}

async function renderTradeChartPng(entry) {
  const model = await loadChartModel(entry);
  const canvas = document.createElement("canvas");
  paintTradeChart(canvas, model, 1280, 560);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  if (!blob) throw new Error("не удалось собрать PNG");
  return { blob, truncated: !!model.truncated };
}

function chartJsonDocument(entry, model) {
  const t = entry.trade;
  const candles = (model.candles || []).map((candle) => ({
    t: candle.t,
    o: candle.o,
    h: candle.h,
    l: candle.l,
    c: candle.c,
    v: candle.v,
  }));
  return {
    bot: entry.bot,
    trade_id: chartTradeNumericId(entry) || null,
    open: !!entry.open,
    pair: model.pair,
    strategy: model.strategy,
    side: model.side,
    timeframe: model.tfLabel,
    pnl_abs: model.pnlAbs,
    pnl_pct: model.pnlPct,
    open_rate: model.openRate,
    close_rate: model.closeRate,
    open_date: t.open_date || null,
    close_date: entry.open ? null : t.close_date || t.close_fill_date || null,
    entry_index: model.entryIdx,
    exit_index: model.exitIdx,
    shade_from: model.shadeFrom,
    shade_to: model.shadeTo,
    setup_bars: CHART_SETUP_BARS,
    truncated: !!model.truncated,
    gap_at: model.gapAt,
    candles,
  };
}

async function fetchTradeChartJson(entry) {
  if (!chartTradeNumericId(entry)) return null;
  const res = await fetch(`/api/pair-config/trade-chart-json?${chartCacheQuery(entry)}`, {
    headers: sessionHeader(),
  });
  if (res.status === 404) return null;
  if (res.status === 401) throw new Error("auth");
  if (!res.ok) return null;
  const text = await res.text();
  if (!text.startsWith("{")) return null;
  return text;
}

async function saveTradeChartJson(entry, text) {
  if (!chartTradeNumericId(entry)) return false;
  const res = await fetch(`/api/pair-config/trade-chart-json?${chartCacheQuery(entry)}`, {
    method: "PUT",
    headers: { ...sessionHeader(), "Content-Type": "application/json" },
    body: text,
  });
  if (res.status === 401) throw new Error("auth");
  return res.ok;
}

async function ensureTradeChartJson(entry) {
  const memKey = chartPngMemKey(entry);
  const cachedMem = chartsJsonCache.get(memKey);
  if (cachedMem) return { text: cachedMem, fromCache: true };
  const fromDisk = await fetchTradeChartJson(entry);
  if (fromDisk) {
    chartsJsonCache.set(memKey, fromDisk);
    return { text: fromDisk, fromCache: true };
  }
  const model = await loadChartModel(entry);
  const text = JSON.stringify(chartJsonDocument(entry, model));
  chartsJsonCache.set(memKey, text);
  try {
    await saveTradeChartJson(entry, text);
  } catch (err) {
    if (String(err?.message) === "auth") throw err;
  }
  return { text, fromCache: false };
}

function chartJsonName(entry) {
  return chartPngName(entry).replace(/\.png$/, ".json");
}

function chartStrategyFolder(entry) {
  const label = tradeSourceLabel(entry.trade, entry.bot) || "Стратегия";
  const folder = String(label)
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[. ]+$/g, "")
    .slice(0, 80);
  return folder || "Стратегия";
}

function chartArchiveName(entry, fileName) {
  return `${chartStrategyFolder(entry)}/${fileName}`;
}

async function ensureTradeChartPng(entry) {
  const memKey = chartPngMemKey(entry);
  const cachedMem = chartsPngCache.get(memKey);
  if (cachedMem) return { blob: cachedMem.blob, fromCache: true, truncated: !!cachedMem.truncated };
  const fromDisk = await fetchTradeChartPng(entry);
  if (fromDisk) {
    chartsPngCache.set(memKey, { blob: fromDisk, truncated: false });
    return { blob: fromDisk, fromCache: true, truncated: false };
  }
  const rendered = await renderTradeChartPng(entry);
  chartsPngCache.set(memKey, { blob: rendered.blob, truncated: rendered.truncated });
  let saved = false;
  try {
    saved = await saveTradeChartPng(entry, rendered.blob);
  } catch (err) {
    if (String(err?.message) === "auth") throw err;
    saved = false;
  }
  return { blob: rendered.blob, fromCache: false, saved, truncated: rendered.truncated };
}

function isChartsPhone() {
  return window.matchMedia("(max-width: 720px)").matches;
}

function chartsPhoneBucket() {
  const width = $("charts-cards")?.clientWidth || window.innerWidth || 360;
  return Math.max(280, Math.round(width / 20) * 20);
}

function phoneChartBlobKey(entry) {
  return `${chartEntryKey(entry)}|${chartsPhoneBucket()}`;
}

function chartPhonePaintSize(key) {
  const img = chartNodes("data-chart-img", key).find((el) => el.clientWidth > 40);
  const width = Math.max(280, Math.round(img?.clientWidth || chartsPhoneBucket()));
  const height = Math.round(Math.max(260, Math.min(420, width * 0.82)));
  return { width, height };
}

async function paintPhoneChart(entry) {
  const key = chartEntryKey(entry);
  const model = await loadChartModel(entry);
  const { width, height } = chartPhonePaintSize(key);
  const canvas = document.createElement("canvas");
  paintTradeChart(canvas, model, width, height);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  if (!blob) throw new Error("не удалось собрать график");
  chartsPhoneBlobs.set(phoneChartBlobKey(entry), blob);
  return { blob, model };
}

function showChartImage(key, blob) {
  const prev = chartObjectUrls.get(key);
  if (prev) URL.revokeObjectURL(prev);
  const url = URL.createObjectURL(blob);
  chartObjectUrls.set(key, url);
  for (const img of chartNodes("data-chart-img", key)) img.src = url;
}

async function showChart(entry) {
  const key = chartEntryKey(entry);
  const gen = (chartPaintGen.get(key) || 0) + 1;
  chartPaintGen.set(key, gen);
  const setStatus = (text) => {
    for (const el of chartNodes("data-chart-status", key)) el.textContent = text;
  };
  try {
    setStatus("Загрузка графика…");
    if (isChartsPhone()) {
      const painted = await paintPhoneChart(entry);
      if (chartPaintGen.get(key) !== gen || !chartsExpanded.has(key)) return;
      showChartImage(key, painted.blob);
      setStatus(
        painted.model.truncated ? "Длинная сделка: на графике вход и выход, середина скрыта." : "",
      );
      return;
    }
    const chart = await ensureTradeChartPng(entry);
    if (chartPaintGen.get(key) !== gen || !chartsExpanded.has(key)) return;
    showChartImage(key, chart.blob);
    if (chart.fromCache) setStatus("Из сохранённых графиков");
    else if (chart.saved === false) setStatus("График построен, на сервер не записался");
    else if (chart.truncated) setStatus("Длинная сделка: на графике вход и выход, середина скрыта.");
    else setStatus("");
  } catch (err) {
    if (String(err?.message) === "auth") {
      logout();
      return;
    }
    if (chartPaintGen.get(key) !== gen) return;
    setStatus(chartErrorText(err));
  }
}

function chartPngName(entry) {
  const t = entry.trade;
  const pair = String(t.pair || "pair").replace(/[^\w]+/g, "");
  const side = t.is_short ? "SHORT" : "LONG";
  const strat = tradeSourceLabel(t, entry.bot)
    .replace(/[^\wа-яА-ЯёЁ.-]+/gi, "_")
    .replace(/_+/g, "_")
    .slice(0, 48);
  const raw = entry.open ? t.open_date : t.close_date || t.close_fill_date || t.open_date || "";
  const when = String(raw).replace(/\D/g, "").slice(0, 14);
  const id = t.trade_id ?? t.id ?? "x";
  return `${pair}_${side}_${strat}_${when}_${id}.png`;
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

async function downloadOneChart(entry) {
  const chart = await ensureTradeChartPng(entry);
  triggerDownload(chart.blob, chartPngName(entry));
}

function crc32Bytes(bytes) {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) {
    c = ZIP_CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  }
  return (c ^ 0xffffffff) >>> 0;
}

function zipU16(n) {
  const b = new Uint8Array(2);
  new DataView(b.buffer).setUint16(0, n, true);
  return b;
}

function zipU32(n) {
  const b = new Uint8Array(4);
  new DataView(b.buffer).setUint32(0, n >>> 0, true);
  return b;
}

function concatBytes(parts) {
  let len = 0;
  for (const part of parts) len += part.length;
  const out = new Uint8Array(len);
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
}

function buildStoredZip(files) {
  const now = new Date();
  const dosTime = ((now.getHours() & 31) << 11) | ((now.getMinutes() & 63) << 5) | ((now.getSeconds() >> 1) & 31);
  const dosDate = (((now.getFullYear() - 1980) & 127) << 9) | (((now.getMonth() + 1) & 15) << 5) | (now.getDate() & 31);
  const enc = new TextEncoder();
  const locals = [];
  const centrals = [];
  let offset = 0;
  for (const file of files) {
    const name = enc.encode(file.name);
    const data = file.data;
    const crc = crc32Bytes(data);
    const local = concatBytes([
      zipU32(0x04034b50),
      zipU16(20),
      zipU16(0x0800),
      zipU16(0),
      zipU16(dosTime),
      zipU16(dosDate),
      zipU32(crc),
      zipU32(data.length),
      zipU32(data.length),
      zipU16(name.length),
      zipU16(0),
      name,
      data,
    ]);
    const central = concatBytes([
      zipU32(0x02014b50),
      zipU16(20),
      zipU16(20),
      zipU16(0x0800),
      zipU16(0),
      zipU16(dosTime),
      zipU16(dosDate),
      zipU32(crc),
      zipU32(data.length),
      zipU32(data.length),
      zipU16(name.length),
      zipU16(0),
      zipU16(0),
      zipU16(0),
      zipU16(0),
      zipU32(0),
      zipU32(offset),
      name,
    ]);
    locals.push(local);
    centrals.push(central);
    offset += local.length;
  }
  const centralBlob = concatBytes(centrals);
  const eocd = concatBytes([
    zipU32(0x06054b50),
    zipU16(0),
    zipU16(0),
    zipU16(files.length),
    zipU16(files.length),
    zipU32(centralBlob.length),
    zipU32(offset),
    zipU16(0),
  ]);
  return concatBytes([...locals, centralBlob, eocd]);
}

async function mapPool(items, limit, fn) {
  let cursor = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (cursor < items.length) {
      const index = cursor;
      cursor += 1;
      await fn(items[index], index);
    }
  });
  await Promise.all(workers);
}

function setChartsArchiveProgress(text) {
  const el = $("charts-archive-progress");
  if (!el) return;
  el.textContent = text || "";
  el.hidden = !text;
}

function setChartsArchiveBusy(busy) {
  chartsZipBusy = busy;
  const png = $("charts-download-all-btn");
  const json = $("charts-download-json-btn");
  if (png) png.disabled = busy;
  if (json) json.disabled = busy;
  if (!busy) setChartsArchiveProgress("");
}

async function downloadChartArchive(kind) {
  if (chartsZipBusy) return;
  const entries = getFilteredChartEntries();
  const json = kind === "json";
  if (!entries.length) {
    showToast("Нет сделок по выбранным фильтрам", "warn");
    return;
  }
  if (entries.length > CHARTS_ZIP_MAX) {
    showToast(`Слишком много сделок (${entries.length}). Сузьте период или стратегию — в один архив до ${CHARTS_ZIP_MAX}.`, "warn");
    return;
  }
  const noun = json ? "JSON" : "графиков";
  if (entries.length > 80 && !window.confirm(`Собрать ${entries.length} ${noun} в один ZIP?`)) return;
  setChartsArchiveBusy(true);
  setChartsArchiveProgress(`${json ? "JSON" : "PNG"}: 0 / ${entries.length}`);
  let done = 0;
  const files = [];
  const errors = [];
  try {
    await mapPool(entries, 3, async (entry) => {
      const fileName = json ? chartJsonName(entry) : chartPngName(entry);
      const name = chartArchiveName(entry, fileName);
      try {
        if (json) {
          const chart = await ensureTradeChartJson(entry);
          files.push({ name, data: new TextEncoder().encode(chart.text) });
        } else {
          const chart = await ensureTradeChartPng(entry);
          files.push({ name, data: new Uint8Array(await chart.blob.arrayBuffer()) });
        }
      } catch (err) {
        errors.push(`${name}: ${chartErrorText(err)}`);
      } finally {
        done += 1;
        setChartsArchiveProgress(`${json ? "JSON" : "PNG"}: ${done} / ${entries.length}`);
      }
    });
    if (!files.length) {
      showToast(errors[0] || (json ? "Не удалось собрать JSON" : "Не удалось построить графики"), "warn");
      return;
    }
    if (errors.length) {
      files.push({ name: "errors.txt", data: new TextEncoder().encode(errors.join("\n")) });
    }
    const stamp = new Date().toISOString().slice(0, 10);
    const count = files.length - (errors.length ? 1 : 0);
    triggerDownload(
      new Blob([buildStoredZip(files)], { type: "application/zip" }),
      json ? `sdelki-${stamp}.zip` : `grafiki-${stamp}.zip`,
    );
    showToast(
      `Архив сохранён: ${count} ${json ? "JSON" : "PNG"}${errors.length ? `, ошибок ${errors.length}` : ""}`,
      "ok",
    );
  } finally {
    setChartsArchiveBusy(false);
    renderChartsView();
  }
}

async function downloadAllChartPngs() {
  return downloadChartArchive("png");
}

function redrawOpenCharts() {
  if ($("charts-view")?.classList.contains("hidden")) return;
  if (isChartsPhone()) {
    const bucket = chartsPhoneBucket();
    if (redrawOpenCharts.bucket === bucket) return;
    redrawOpenCharts.bucket = bucket;
    for (const key of chartsExpanded) {
      const entry = findChartEntry(key);
      if (entry && !chartsPhoneBlobs.get(phoneChartBlobKey(entry))) showChart(entry);
    }
    return;
  }
  for (const key of chartsExpanded) {
    const model = chartsModelCache.get(key);
    const canvas = pickVisibleChartCanvas(key);
    if (model && canvas) paintTradeChart(canvas, model);
  }
}

function bindChartsControls() {
  $("charts-back-btn")?.addEventListener("click", closeCharts);
  $("charts-refresh-btn")?.addEventListener("click", () => {
    chartsModelCache.clear();
    chartsPngCache.clear();
    chartsJsonCache.clear();
    loadCharts();
  });
  $("charts-download-all-btn")?.addEventListener("click", () => downloadAllChartPngs());
  $("charts-download-json-btn")?.addEventListener("click", () => downloadChartArchive("json"));
  $("charts-date-from")?.addEventListener("change", applyChartsDateRange);
  $("charts-date-to")?.addEventListener("change", applyChartsDateRange);
  $("charts-page-prev")?.addEventListener("click", () => {
    if (chartsPage > 1) {
      chartsPage -= 1;
      renderChartsView();
    }
  });
  $("charts-page-next")?.addEventListener("click", () => {
    chartsPage += 1;
    renderChartsView();
  });
  $("charts-view")?.addEventListener("click", (e) => {
    const periodBtn = e.target.closest("[data-charts-period]");
    if (periodBtn) {
      chartsPeriod = normalizeStatsPeriod(periodBtn.dataset.chartsPeriod || "all");
      chartsDateFrom = "";
      chartsDateTo = "";
      chartsPage = 1;
      renderChartsView();
      return;
    }
    const sideBtn = e.target.closest("[data-charts-side]");
    if (sideBtn) {
      chartsSide = normalizeTradeSide(sideBtn.dataset.chartsSide || "all");
      chartsPage = 1;
      renderChartsView();
      return;
    }
    const botBtn = e.target.closest("[data-charts-bot]");
    if (botBtn) {
      chartsBot = normalizeChartsBot(botBtn.dataset.chartsBot || "all");
      chartsPage = 1;
      renderChartsView();
      return;
    }
    const stratBtn = e.target.closest("[data-charts-strategy]");
    if (stratBtn) {
      chartsStrategy = stratBtn.getAttribute("data-charts-strategy") || "all";
      chartsPage = 1;
      renderChartsView();
      return;
    }
    const toggle = e.target.closest("[data-chart-toggle]");
    if (toggle) {
      const key = toggle.getAttribute("data-chart-toggle");
      const opening = !chartsExpanded.has(key);
      if (opening) chartsExpanded.add(key);
      else chartsExpanded.delete(key);
      renderChartsView();
      if (!opening) return;
      const entry = findChartEntry(key);
      if (!entry) return;
      if (isChartsPhone()) {
        if (!chartsPhoneBlobs.get(phoneChartBlobKey(entry))) showChart(entry);
        return;
      }
      if (!chartsPngCache.get(chartPngMemKey(entry))) showChart(entry);
      return;
    }
    const png = e.target.closest("[data-chart-png]");
    if (!png) return;
    const key = png.getAttribute("data-chart-png");
    const entry = findChartEntry(key);
    if (!entry) return;
    const nodes = chartNodes("data-chart-status", key);
    nodes.forEach((el) => {
      el.textContent = "Сохраняю PNG…";
    });
    downloadOneChart(entry)
      .then(() => {
        nodes.forEach((el) => {
          el.textContent = "";
        });
      })
      .catch((err) => {
        if (String(err?.message) === "auth") logout();
        else {
          nodes.forEach((el) => {
            el.textContent = chartErrorText(err);
          });
        }
      });
  });
  window.addEventListener("resize", redrawOpenCharts);
}

/* --- Analytics: Отчёт (Cursor agent) --- */
let reportPeriod = "today";
let reportDateFrom = "";
let reportDateTo = "";
let reportLastPayload = null;
let reportBusy = false;

function userMayUseReport() {
  return userMayUseBlock("report") || userMayUseBlock("history");
}

function reportPeriodPayload() {
  if (reportPeriod === "custom") {
    return {
      period: "custom",
      from: reportDateFrom || undefined,
      to: reportDateTo || undefined,
    };
  }
  return { period: reportPeriod };
}

function fmtReportPnl(value) {
  const n = Number(value || 0);
  return `${n > 0 ? "+" : ""}${n.toFixed(2)} $`;
}

function renderReportSummary(payload) {
  const el = $("report-summary");
  if (!el) return;
  const s = payload?.summary || {};
  const period = payload?.period || {};
  const wr = s.winrate_pct != null ? `${s.winrate_pct}%` : "—";
  const cells = [
    ["Период", period.label || "—"],
    ["Сделок", String(s.trades ?? 0)],
    ["Winrate", wr],
    ["PnL", fmtReportPnl(s.pnl_abs)],
    ["Wins / Losses", `${s.wins ?? 0} / ${s.losses ?? 0}`],
  ];
  el.innerHTML = cells
    .map(
      ([label, value]) =>
        `<div class="stats-kpi"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
    )
    .join("");
}

/** Light markdown → HTML for agent analysis (headers, lists, bold, paragraphs). */
function renderReportMarkdown(text) {
  const raw = String(text || "").trim();
  if (!raw) return "<p class=\"muted\">Пустой ответ агента.</p>";
  const lines = raw.split(/\r?\n/);
  const parts = [];
  let listOpen = false;
  const flushList = () => {
    if (listOpen) {
      parts.push("</ul>");
      listOpen = false;
    }
  };
  const inline = (s) =>
    escapeHtml(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");
  for (const line of lines) {
    const h = /^(#{1,3})\s+(.+)$/.exec(line);
    if (h) {
      flushList();
      const lvl = h[1].length === 1 ? "h2" : "h3";
      parts.push(`<${lvl}>${inline(h[2])}</${lvl}>`);
      continue;
    }
    const li = /^[-*•]\s+(.+)$/.exec(line);
    if (li) {
      if (!listOpen) {
        parts.push("<ul>");
        listOpen = true;
      }
      parts.push(`<li>${inline(li[1])}</li>`);
      continue;
    }
    flushList();
    if (!line.trim()) {
      parts.push("");
      continue;
    }
    parts.push(`<p>${inline(line)}</p>`);
  }
  flushList();
  return parts.filter((p) => p !== "").join("\n");
}

function updateReportPeriodUi() {
  document.querySelectorAll(".report-period-btn").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.reportPeriod === reportPeriod);
  });
  const range = $("report-date-range");
  if (range) range.hidden = reportPeriod !== "custom";
  if ($("report-date-from")) $("report-date-from").value = reportDateFrom;
  if ($("report-date-to")) $("report-date-to").value = reportDateTo;
}

function setReportPeriod(period) {
  reportPeriod = period || "today";
  if (reportPeriod !== "custom") {
    reportDateFrom = "";
    reportDateTo = "";
  }
  updateReportPeriodUi();
}

function applyReportDateRange() {
  reportDateFrom = $("report-date-from")?.value || "";
  reportDateTo = $("report-date-to")?.value || "";
  reportPeriod = "custom";
  updateReportPeriodUi();
}

function downloadReportJson(payload) {
  if (!payload) return;
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
  const a = document.createElement("a");
  const stamp = (payload.period?.date_from || "report") + "_" + (payload.period?.date_to || "");
  a.href = URL.createObjectURL(blob);
  a.download = `cryptotools-report-${stamp}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

async function runTradeReport({ analyze = true } = {}) {
  if (!userMayUseReport() || reportBusy) return;
  const statusEl = $("report-status");
  const analysisEl = $("report-analysis");
  const runBtn = $("report-run-btn");
  if (reportPeriod === "custom" && !reportDateFrom && !reportDateTo) {
    if (statusEl) statusEl.textContent = "Укажите даты интервала";
    return;
  }
  reportBusy = true;
  if (runBtn) {
    runBtn.disabled = true;
    runBtn.classList.add("is-busy");
  }
  if (analysisEl && analyze) {
    analysisEl.classList.add("is-loading");
    analysisEl.textContent = "Агент анализирует сделки… обычно 1–3 мин.";
  }
  if (statusEl) statusEl.textContent = analyze ? "Формирование отчёта…" : "Загрузка сделок…";
  try {
    const customPrompt = String($("report-prompt")?.value || "").trim();
    const body = { ...reportPeriodPayload(), analyze: !!analyze };
    if (analyze && customPrompt) body.prompt = customPrompt;
    const path = analyze ? "/report/analyze" : "/report";
    const payload = analyze
      ? await pairConfigApi(path, "POST", body)
      : await pairConfigApi(
          `/report?period=${encodeURIComponent(body.period || "today")}` +
            (body.from ? `&from=${encodeURIComponent(body.from)}` : "") +
            (body.to ? `&to=${encodeURIComponent(body.to)}` : "")
        );
    reportLastPayload = payload;
    renderReportSummary(payload);
    if (analysisEl) {
      analysisEl.classList.remove("is-loading");
      if (payload.analysis) {
        analysisEl.innerHTML = renderReportMarkdown(payload.analysis);
      } else if (analyze) {
        analysisEl.textContent = "Агент не вернул текст.";
      }
    }
    const n = payload.trades_total ?? payload.trades?.length ?? 0;
    const model = payload.model ? ` · ${payload.model}` : "";
    const ms = payload.elapsed_ms != null ? ` · ${(payload.elapsed_ms / 1000).toFixed(0)}с` : "";
    if (statusEl) {
      statusEl.textContent = `${n} сделок · PnL ${fmtReportPnl(payload.summary?.pnl_abs)}${model}${ms}`;
    }
  } catch (e) {
    if (e.message === "auth") logout();
    else {
      if (analysisEl) {
        analysisEl.classList.remove("is-loading");
        analysisEl.textContent = formatApiError(e.message);
      }
      if (statusEl) statusEl.textContent = formatApiError(e.message);
    }
  } finally {
    reportBusy = false;
    if (runBtn) {
      runBtn.disabled = false;
      runBtn.classList.remove("is-busy");
    }
  }
}

function openReport() {
  if (!userMayUseReport()) return;
  closeMobileMenu();
  hideMainViewsForOverlay();
  const view = $("report-view");
  view?.classList.remove("hidden");
  view?.setAttribute("aria-hidden", "false");
  updateReportPeriodUi();
  if (!reportLastPayload) {
    runTradeReport({ analyze: false }).catch(() => {});
  }
}

function closeReport() {
  $("report-view")?.classList.add("hidden");
  $("report-view")?.setAttribute("aria-hidden", "true");
  $("dashboard-view")?.classList.remove("hidden");
}

function bindReportControls() {
  document.querySelectorAll(".report-period-btn").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setReportPeriod(btn.dataset.reportPeriod || "today"));
  });
  $("report-date-from")?.addEventListener("change", applyReportDateRange);
  $("report-date-to")?.addEventListener("change", applyReportDateRange);
  $("report-run-btn")?.addEventListener("click", () => runTradeReport({ analyze: true }));
  $("report-json-btn")?.addEventListener("click", async () => {
    if (!reportLastPayload) await runTradeReport({ analyze: false });
    if (reportLastPayload) downloadReportJson(reportLastPayload);
  });
  $("report-back-btn")?.addEventListener("click", closeReport);
}

function colorForStrategyName(name, colorMap) {
  if (colorMap.has(name)) return colorMap.get(name);
  const color = PNL_DASH_COLORS[colorMap.size % PNL_DASH_COLORS.length];
  colorMap.set(name, color);
  return color;
}

function fmtUsdCompact(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 10000) return `${(n / 1000).toFixed(1)}k`;
  if (abs >= 1000) return `${(n / 1000).toFixed(2)}k`;
  if (abs >= 100) return n.toFixed(0);
  if (abs >= 10) return n.toFixed(1);
  return n.toFixed(2);
}

function pnlPolar(cx, cy, r, angleDeg) {
  const rad = ((angleDeg - 90) * Math.PI) / 180;
  return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
}

function pnlDonutSlicePath(cx, cy, rOuter, rInner, startAngle, endAngle) {
  const sweep = Math.min(Math.max(endAngle - startAngle, 0), 359.999);
  if (sweep <= 0.001) return "";
  const end = startAngle + sweep;
  const large = sweep > 180 ? 1 : 0;
  const o1 = pnlPolar(cx, cy, rOuter, startAngle);
  const o2 = pnlPolar(cx, cy, rOuter, end);
  const i2 = pnlPolar(cx, cy, rInner, end);
  const i1 = pnlPolar(cx, cy, rInner, startAngle);
  return [
    `M ${o1.x.toFixed(2)} ${o1.y.toFixed(2)}`,
    `A ${rOuter} ${rOuter} 0 ${large} 1 ${o2.x.toFixed(2)} ${o2.y.toFixed(2)}`,
    `L ${i2.x.toFixed(2)} ${i2.y.toFixed(2)}`,
    `A ${rInner} ${rInner} 0 ${large} 0 ${i1.x.toFixed(2)} ${i1.y.toFixed(2)}`,
    "Z",
  ].join(" ");
}

function buildPnlSlices(rows, field, colorMap) {
  const raw = rows
    .filter((r) => Number(r[field]) > 0)
    .map((r) => ({ name: r.name, value: Number(r[field]) }))
    .sort((a, b) => b.value - a.value);
  const total = raw.reduce((s, x) => s + x.value, 0);
  if (!total) return { total: 0, slices: [] };

  const kept = [];
  let otherValue = 0;
  raw.forEach((item, idx) => {
    const pct = (item.value / total) * 100;
    if (idx < PNL_DASH_MAX_SLICES && pct >= PNL_DASH_MIN_PCT) {
      kept.push(item);
    } else {
      otherValue += item.value;
    }
  });
  if (otherValue > 0) {
    kept.push({ name: "Прочее", value: otherValue, isOther: true });
  }

  const slices = kept.map((item) => ({
    name: item.name,
    value: item.value,
    pct: (item.value / total) * 100,
    color: item.isOther ? PNL_DASH_OTHER_COLOR : colorForStrategyName(item.name, colorMap),
  }));
  return { total, slices };
}

function bindPnlChartHover(root) {
  if (!root || root.dataset.hoverBound) return;
  root.dataset.hoverBound = "1";
  const setActive = (idx) => {
    root.querySelectorAll("[data-slice-idx]").forEach((el) => {
      const on = idx != null && el.dataset.sliceIdx === String(idx);
      el.classList.toggle("is-active", on);
      el.classList.toggle("is-dim", idx != null && !on);
    });
  };
  root.addEventListener("mouseover", (e) => {
    const t = e.target.closest?.("[data-slice-idx]");
    if (!t || !root.contains(t)) return;
    setActive(t.dataset.sliceIdx);
  });
  root.addEventListener("mouseleave", () => setActive(null));
  root.addEventListener("focusin", (e) => {
    const t = e.target.closest?.("[data-slice-idx]");
    if (t) setActive(t.dataset.sliceIdx);
  });
  root.addEventListener("focusout", () => setActive(null));
}

function renderDonutChart(container, slices, centerLabel, totalValue, toneClass) {
  if (!container) return;
  if (!slices.length) {
    container.innerHTML = `<div class="pnl-chart-empty-wrap"><p class="pnl-chart-empty">Нет данных</p></div>`;
    return;
  }

  const size = 220;
  const cx = size / 2;
  const cy = size / 2;
  const rOuter = 78;
  const rInner = 48;
  const rLabel = 96;
  const gapDeg = slices.length > 1 ? 1.4 : 0;
  let angle = 0;

  const paths = [];
  const labels = [];
  slices.forEach((s, idx) => {
    const sweep = (s.pct / 100) * 360;
    const start = angle + gapDeg / 2;
    const end = angle + sweep - gapDeg / 2;
    const mid = angle + sweep / 2;
    angle += sweep;
    if (end <= start) return;

    const d = pnlDonutSlicePath(cx, cy, rOuter, rInner, start, end);
    paths.push(
      `<path class="pnl-donut-slice" data-slice-idx="${idx}" d="${d}" fill="${escapeHtml(s.color)}"
        tabindex="0" role="listitem"
        aria-label="${escapeHtml(s.name)} ${s.pct.toFixed(1)}%">
        <title>${escapeHtml(s.name)}: ${fmtUsd(s.value)} (${s.pct.toFixed(1)}%)</title>
      </path>`
    );

    if (s.pct >= 7) {
      const tip = pnlPolar(cx, cy, rLabel, mid);
      const anchor = tip.x >= cx ? "start" : "end";
      const dx = tip.x >= cx ? 2 : -2;
      labels.push(
        `<text class="pnl-donut-pct-label" data-slice-idx="${idx}"
          x="${(tip.x + dx).toFixed(1)}" y="${tip.y.toFixed(1)}"
          text-anchor="${anchor}" dominant-baseline="middle">${s.pct.toFixed(0)}%</text>`
      );
    }
  });

  const tone = toneClass === "neg" ? "pnl-tone-neg" : "pnl-tone-pos";
  const filterId = `pnl-soft-${toneClass}-${Math.abs(Math.round(totalValue * 100) % 9973)}`;
  container.innerHTML = `<svg class="pnl-donut-svg" viewBox="0 0 ${size} ${size}" role="img" aria-label="${escapeHtml(centerLabel)}">
    <defs>
      <filter id="${filterId}" x="-20%" y="-20%" width="140%" height="140%">
        <feDropShadow dx="0" dy="2" stdDeviation="2.5" flood-color="#000" flood-opacity="0.35"/>
      </filter>
    </defs>
    <circle class="pnl-donut-track" cx="${cx}" cy="${cy}" r="${((rOuter + rInner) / 2).toFixed(1)}"
      fill="none" stroke="rgba(45,58,79,0.85)" stroke-width="${(rOuter - rInner).toFixed(1)}" />
    <g class="pnl-donut-slices" filter="url(#${filterId})" role="list">${paths.join("")}</g>
    <circle class="pnl-donut-hole" cx="${cx}" cy="${cy}" r="${rInner - 1}" fill="#182131" />
    <text x="${cx}" y="${cy - 10}" class="pnl-donut-center-label">${escapeHtml(centerLabel)}</text>
    <text x="${cx}" y="${cy + 10}" class="pnl-donut-center-value ${tone}">${escapeHtml(fmtUsdCompact(totalValue))}</text>
    <text x="${cx}" y="${cy + 26}" class="pnl-donut-center-unit">USDT</text>
    <g class="pnl-donut-labels">${labels.join("")}</g>
  </svg>`;
}

function renderPnlLegend(listEl, slices, toneClass) {
  if (!listEl) return;
  if (!slices.length) {
    listEl.innerHTML = "";
    return;
  }
  listEl.innerHTML = slices
    .map(
      (s, idx) => `<li class="pnl-legend-item" data-slice-idx="${idx}" tabindex="0" title="${escapeHtml(s.name)}">
        <span class="pnl-legend-swatch" style="background:${escapeHtml(s.color)}"></span>
        <div class="pnl-legend-main">
          <div class="pnl-legend-row">
            <span class="pnl-legend-name">${escapeHtml(s.name)}</span>
            <span class="pnl-legend-meta ${toneClass}">${fmtUsd(s.value)}</span>
            <span class="pnl-legend-pct">${s.pct.toFixed(0)}%</span>
          </div>
          <div class="pnl-legend-bar" aria-hidden="true">
            <span class="pnl-legend-bar-fill ${toneClass === "neg" ? "is-neg" : "is-pos"}"
              style="width:${Math.max(s.pct, 1.5).toFixed(1)}%; background:${escapeHtml(s.color)}"></span>
          </div>
        </div>
      </li>`
    )
    .join("");
}

function updatePnlDashFilterUi() {
  document.querySelectorAll(".pnl-dash-scope").forEach((btn) => {
    btn.classList.toggle("is-active", (btn.dataset.pnlDashScope || "all") === pnlDashScope);
  });
  document.querySelectorAll(".pnl-dash-tf").forEach((btn) => {
    btn.classList.toggle("is-active", (btn.dataset.pnlDashTf || "5m") === pnlDashTf);
  });
  document.querySelector(".pnl-dash-bot-group")?.classList.toggle("hidden", pnlDashTf === "1h");
  document.querySelectorAll(".pnl-dash-period").forEach((btn) => {
    const custom = Boolean(pnlDashDateFrom || pnlDashDateTo);
    btn.classList.toggle(
      "is-active",
      !custom && (btn.dataset.pnlDashPeriod || "all") === pnlDashPeriod,
    );
  });
  syncDateInputs("pnl-date-from", "pnl-date-to", pnlDashDateFrom, pnlDashDateTo);
  document.querySelectorAll(".pnl-dash-side").forEach((btn) => {
    btn.classList.toggle("is-active", (btn.dataset.pnlDashSide || "all") === pnlDashSide);
  });
}

function renderPnlDashboard(data) {
  if (!data) return;
  updatePnlDashFilterUi();

  const catalog = data.catalog || strategyCatalog;
  const finderFiltered = filterTradesBySide(
    filterClosedTrades(data.finderTrades, pnlDashPeriod, pnlDashDateFrom, pnlDashDateTo),
    pnlDashSide,
  );
  const stratFiltered = filterTradesBySide(
    filterClosedTrades(data.stratTrades, pnlDashPeriod, pnlDashDateFrom, pnlDashDateTo),
    pnlDashSide,
  );
  const gridFiltered = filterTradesBySide(
    filterClosedTrades(data.gridTrades, pnlDashPeriod, pnlDashDateFrom, pnlDashDateTo),
    pnlDashSide,
  );
  const bybitFiltered =
    pnlDashSide === "all"
      ? filterBybitHistory(data.bybitHistory, pnlDashPeriod, pnlDashDateFrom, pnlDashDateTo)
      : [];
  const built = buildStatsRows(finderFiltered, stratFiltered, gridFiltered, bybitFiltered, catalog);
  let rows;
  if (pnlDashTf === "1h") {
    rows = filterStatsRows(built, "htf1h", catalog);
  } else {
    rows = filterStatsRows(built, pnlDashScope, catalog).filter((r) => !isHtfStatsRow(r, catalog));
  }

  let totalProfit = 0;
  let totalLoss = 0;
  let totalWins = 0;
  let totalLosses = 0;
  let totalClosed = 0;
  for (const r of rows) {
    totalProfit += r.profit;
    totalLoss += r.loss;
    totalWins += r.wins;
    totalLosses += r.losses;
    totalClosed += r.closed || r.wins + r.losses + (r.flat || 0);
  }
  const net = totalProfit - totalLoss;

  const summaryEl = $("pnl-dashboard-summary");
  if (summaryEl) {
    const warnHtml =
      data.loadErrors?.length > 0
        ? `<p class="stats-load-warn">${data.loadErrors.map((e) => escapeHtml(e)).join(" · ")}</p>`
        : "";
    summaryEl.innerHTML = `
      ${warnHtml}
      <div class="stats-kpi stats-kpi-income">
        <span>Доход</span>
        <strong class="pos">${fmtUsd(totalProfit)}</strong>
      </div>
      <div class="stats-kpi stats-kpi-expense">
        <span>Расход</span>
        <strong class="neg">${fmtUsd(totalLoss)}</strong>
      </div>
      <div class="stats-kpi stats-kpi-net">
        <span>Итого</span>
        <strong class="${pnlClass(net)}">${fmtUsdSigned(net)}</strong>
      </div>
      <div class="stats-kpi stats-kpi-ratio">
        <span>Успешные / неуспешные</span>
        <strong>${totalWins} / ${totalLosses}</strong>
      </div>
    `;
  }

  const colorMap = new Map();
  const income = buildPnlSlices(rows, "profit", colorMap);
  const expense = buildPnlSlices(rows, "loss", colorMap);

  const incomeTotalEl = $("pnl-income-total");
  if (incomeTotalEl) {
    incomeTotalEl.textContent = income.total
      ? `Сумма ${fmtUsd(income.total)} · доля стратегий`
      : "Нет прибыльных сделок за период";
  }
  const expenseTotalEl = $("pnl-expense-total");
  if (expenseTotalEl) {
    expenseTotalEl.textContent = expense.total
      ? `Сумма ${fmtUsd(expense.total)} · доля стратегий`
      : "Нет убыточных сделок за период";
  }

  renderDonutChart($("pnl-income-chart"), income.slices, "Доход", income.total, "pos");
  renderDonutChart($("pnl-expense-chart"), expense.slices, "Расход", expense.total, "neg");
  renderPnlLegend($("pnl-income-legend"), income.slices, "pos");
  renderPnlLegend($("pnl-expense-legend"), expense.slices, "neg");

  document.querySelectorAll(".pnl-chart-card").forEach((card) => bindPnlChartHover(card));

  const statusEl = $("pnl-dashboard-status");
  if (statusEl) {
    const periodLabel = statsPeriodLabel(pnlDashPeriod, pnlDashDateFrom, pnlDashDateTo);
    const sideLabel = tradeSideLabel(pnlDashSide);
    statusEl.textContent =
      totalClosed > 0
        ? `Закрытых операций: ${totalClosed} · ${sideLabel} · период: ${periodLabel}`
        : `Нет закрытых операций · ${sideLabel} · период: ${periodLabel}`;
  }
}

async function loadPnlDashboard(force = false) {
  const statusEl = $("pnl-dashboard-status");
  if (statusEl) statusEl.textContent = "Загрузка…";
  try {
    pnlDashDataCache = await fetchStatsData(CLOSED_TRADES_FETCH_LIMIT, {
      force,
      accountMode: pnlDashMode,
    });
    statsDataCache = pnlDashDataCache;
    renderPnlDashboard(pnlDashDataCache);
  } catch (e) {
    if (e.message === "auth") logout();
    else if (statusEl) statusEl.textContent = formatApiError(e.message);
  }
}

function openPnlDashboard() {
  if (!userMayUseBlock("dashboard")) return;
  closeMobileMenu();
  hideMainViewsForOverlay();
  const view = $("pnl-dashboard-view");
  view?.classList.remove("hidden");
  view?.setAttribute("aria-hidden", "false");
  loadPnlDashboard(false);
}

function closePnlDashboard() {
  $("pnl-dashboard-view")?.classList.add("hidden");
  $("pnl-dashboard-view")?.setAttribute("aria-hidden", "true");
  $("dashboard-view")?.classList.remove("hidden");
}

function setPnlDashScope(scope) {
  pnlDashScope = STATS_SCOPE_META[scope] ? scope : "all";
  if (pnlDashDataCache) renderPnlDashboard(pnlDashDataCache);
  else loadPnlDashboard(false);
}

function setPnlDashTf(tf) {
  pnlDashTf = tf === "1h" ? "1h" : "5m";
  updatePnlDashFilterUi();
  if (pnlDashDataCache) renderPnlDashboard(pnlDashDataCache);
  else loadPnlDashboard(false);
}

function setPnlDashPeriod(period) {
  pnlDashPeriod = normalizeStatsPeriod(period);
  pnlDashDateFrom = "";
  pnlDashDateTo = "";
  if (pnlDashDataCache) renderPnlDashboard(pnlDashDataCache);
  else loadPnlDashboard(false);
}

function applyPnlDashDateRange() {
  pnlDashDateFrom = $("pnl-date-from")?.value || "";
  pnlDashDateTo = $("pnl-date-to")?.value || "";
  if (pnlDashDataCache) renderPnlDashboard(pnlDashDataCache);
  else loadPnlDashboard(false);
}

function setPnlDashSide(side) {
  pnlDashSide = normalizeTradeSide(side);
  updatePnlDashFilterUi();
  if (pnlDashDataCache) renderPnlDashboard(pnlDashDataCache);
  else loadPnlDashboard(false);
}


function setRatingAccountMode(mode) {
  ratingAccountMode = ["live", "demo", "all"].includes(mode) ? mode : "all";
  document.querySelectorAll(".rating-mode").forEach((btn) => {
    btn.classList.toggle("is-active", (btn.dataset.ratingMode || "all") === ratingAccountMode);
  });
  loadRating(true).catch(() => {});
}
function setPnlDashMode(mode) {
  pnlDashMode = ["live", "demo", "all"].includes(mode) ? mode : "all";
  document.querySelectorAll(".pnl-dash-mode").forEach((btn) => {
    btn.classList.toggle("is-active", (btn.dataset.pnlDashMode || "all") === pnlDashMode);
  });
  loadPnlDashboard(true).catch(() => {});
}
function setStatsAccountMode(mode) {
  statsAccountMode = ["live", "demo", "all"].includes(mode) ? mode : "all";
  document.querySelectorAll(".stats-mode-btn").forEach((btn) => {
    btn.classList.toggle("is-active", (btn.dataset.statsMode || "all") === statsAccountMode);
  });
}

function bindPnlDashboardControls() {
  document.querySelectorAll(".pnl-dash-scope").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setPnlDashScope(btn.dataset.pnlDashScope || "all"));
  });
  document.querySelectorAll(".pnl-dash-tf").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setPnlDashTf(btn.dataset.pnlDashTf || "5m"));
  });
  document.querySelectorAll(".pnl-dash-period").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setPnlDashPeriod(btn.dataset.pnlDashPeriod || "all"));
  });
  $("pnl-date-from")?.addEventListener("change", applyPnlDashDateRange);
  $("pnl-date-to")?.addEventListener("change", applyPnlDashDateRange);
  document.querySelectorAll(".pnl-dash-side").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setPnlDashSide(btn.dataset.pnlDashSide || "all"));
  });
  document.querySelectorAll(".pnl-dash-mode").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setPnlDashMode(btn.dataset.pnlDashMode || "all"));
  });
  $("pnl-dashboard-back-btn")?.addEventListener("click", closePnlDashboard);
  $("pnl-dashboard-refresh-btn")?.addEventListener("click", () => loadPnlDashboard(true));
}

let ratingPeriod = "all";
let ratingDateFrom = "";
let ratingDateTo = "";
let ratingTf = "5m";
let ratingScope = "strategies"; // strategies | pairs | days | matrix
let ratingBot = "all";
let ratingSide = "all"; // all | long | short
let ratingUserFilter = ""; // "" = all accounts (admin only)
let ratingDataCache = {}; // key: `${scope}:${user||"all"}:${tf}:${bot}:${side}`
let ratingSortKey = "pnl";
let ratingSortDesc = true;
const ratingExpandedPairs = new Set(); // also used for expanded weekdays in «По дням недели»
const RATING_WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const RATING_WEEKDAY_SHORT = {
  mon: "Пн",
  tue: "Вт",
  wed: "Ср",
  thu: "Чт",
  fri: "Пт",
  sat: "Сб",
  sun: "Вс",
};
const RATING_DEFAULT_THEAD = `<tr>
  <th class="rating-sort" data-rating-sort="rank" data-label-base="#" title="Сортировать">#</th>
  <th class="rating-sort" data-rating-sort="label" id="rating-col-label" data-label-base="Стратегия" title="Сортировать">Стратегия</th>
  <th class="rating-sort num" data-rating-sort="trades" data-label-base="Сделок" title="Сортировать">Сделок</th>
  <th class="rating-sort num" data-rating-sort="winrate" data-label-base="WR%" title="Сортировать">WR%</th>
  <th class="rating-sort num" data-rating-sort="conf_avg" data-label-base="Увер. мин/ср/макс" title="Мин / сред / макс уверенность модели">Увер. мин/ср/макс</th>
  <th class="rating-sort num" data-rating-sort="income" data-label-base="PnL" title="Сортировать">PnL</th>
  <th class="rating-sort num" data-rating-sort="loss_abs" data-label-base="Убытки" title="Сортировать">Убытки</th>
  <th class="rating-sort num is-active" data-rating-sort="pnl" data-label-base="Чистый PnL" title="Сортировать">Чистый PnL</th>
</tr>`;

function ratingCacheKey(scope = ratingScope, user = ratingUserFilter) {
  return `${scope}:${user || "all"}:${ratingTf}:${ratingBot}:${ratingSide}:${ratingAccountMode}:${ratingDateFrom}:${ratingDateTo}`;
}

function ratingBotLabel() {
  return "";
}

function ratingAccountLabel() {
  if (!ratingUserFilter) return "Все аккаунты";
  const sel = $("rating-user-filter");
  const opt = sel?.selectedOptions?.[0];
  if (opt?.textContent) return opt.textContent.trim();
  return ratingUserFilter;
}

async function ensureRatingUserOptions() {
  const group = $("rating-user-group");
  const sel = $("rating-user-filter");
  if (!group || !sel) return;
  group.classList.remove("hidden");
  const prev = ratingUserFilter;
  const options = [
    `<option value="">Все аккаунты</option>`,
    `<option value="self">Свой</option>`,
  ];
  if (isAdminUser()) {
    try {
      const data = await pairConfigApi("/users");
      const users = Array.isArray(data.users) ? data.users : [];
      for (const u of users) {
        const id = String(u.id || "");
        if (!id) continue;
        const name = String(u.username || id);
        const isAdm = u.role === "admin" || id === "admin";
        const label = isAdm ? `Главный (${name})` : name;
        options.push(`<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`);
      }
      const ids = new Set(["", "self", ...users.map((u) => String(u.id || ""))]);
      ratingUserFilter = prev && ids.has(prev) ? prev : "";
    } catch {
      ratingUserFilter = prev === "self" || prev === "" ? prev : "";
    }
  } else {
    ratingUserFilter = prev === "self" || prev === "" ? prev : "";
  }
  sel.innerHTML = options.join("");
  sel.value = ratingUserFilter;
}

function syncRatingUserFilterVisibility() {
  const group = $("rating-user-group");
  if (!group) return;
  group.classList.remove("hidden");
}

function hideMainViewsForOverlay() {
  $("changelog-view")?.classList.add("hidden");
  $("logs-view")?.classList.add("hidden");
  $("settings-view")?.classList.add("hidden");
  $("history-view")?.classList.add("hidden");
  $("charts-view")?.classList.add("hidden");
  $("charts-view")?.setAttribute("aria-hidden", "true");
  $("report-view")?.classList.add("hidden");
  $("report-view")?.setAttribute("aria-hidden", "true");
  $("pnl-dashboard-view")?.classList.add("hidden");
  $("pnl-dashboard-view")?.setAttribute("aria-hidden", "true");
  $("rating-view")?.classList.add("hidden");
  $("rating-view")?.setAttribute("aria-hidden", "true");
  $("stats-view")?.classList.add("hidden");
  $("stats-view")?.setAttribute("aria-hidden", "true");
  $("dashboard-view")?.classList.add("hidden");
}

function openRating() {
  if (!userMayUseBlock("rating")) return;
  closeMobileMenu();
  hideMainViewsForOverlay();
  const view = $("rating-view");
  view?.classList.remove("hidden");
  view?.setAttribute("aria-hidden", "false");
  ensureRatingUserOptions()
    .catch(() => {})
    .finally(() => loadRating(false));
}

function closeRating() {
  $("rating-view")?.classList.add("hidden");
  $("rating-view")?.setAttribute("aria-hidden", "true");
  $("dashboard-view")?.classList.remove("hidden");
}

function setRatingPeriod(period) {
  ratingPeriod = normalizeStatsPeriod(period);
  ratingDateFrom = "";
  ratingDateTo = "";
  updateRatingFilterUi();
  loadRating(true);
}

function applyRatingDateRange() {
  ratingDateFrom = $("rating-date-from")?.value || "";
  ratingDateTo = $("rating-date-to")?.value || "";
  updateRatingFilterUi();
  loadRating(true);
}

function setRatingTf(tf) {
  const next = tf === "1h" ? "1h" : "5m";
  if (ratingTf === next) return;
  ratingTf = next;
  ratingExpandedPairs.clear();
  updateRatingFilterUi();
  loadRating(false);
}

function setRatingScope(scope) {
  const next =
    scope === "pairs"
      ? "pairs"
      : scope === "stratpairs"
        ? "stratpairs"
        : scope === "days"
          ? "days"
          : scope === "matrix"
            ? "matrix"
            : "strategies";
  if (ratingScope === next) return;
  ratingScope = next;
  ratingExpandedPairs.clear();
  if (next === "days") {
    ratingSortKey = "weekday_order";
    ratingSortDesc = false; // Mon → Sun
  } else if (next === "matrix") {
    ratingSortKey = "pnl";
    ratingSortDesc = true;
  } else if (
    ratingSortKey === "day" ||
    ratingSortKey === "weekday_order" ||
    String(ratingSortKey).startsWith("pnl_")
  ) {
    ratingSortKey = "pnl";
    ratingSortDesc = true;
  }
  updateRatingFilterUi();
  loadRating(false);
}

function setRatingBot(_bot) {
  if (ratingBot === "all") return;
  ratingBot = "all";
  ratingExpandedPairs.clear();
  updateRatingFilterUi();
  loadRating(false);
}

function setRatingSide(side) {
  const next = normalizeTradeSide(side);
  if (ratingSide === next) return;
  ratingSide = next;
  ratingExpandedPairs.clear();
  updateRatingFilterUi();
  loadRating(false);
}

function setRatingUserFilter(userId) {
  const next = String(userId || "");
  if (ratingUserFilter === next) return;
  ratingUserFilter = next;
  ratingExpandedPairs.clear();
  updateRatingFilterUi();
  loadRating(true);
}

function toggleRatingPairExpand(pairId) {
  if (!pairId) return;
  if (ratingExpandedPairs.has(pairId)) ratingExpandedPairs.delete(pairId);
  else ratingExpandedPairs.add(pairId);
  const cached = ratingDataCache[ratingCacheKey()];
  if (cached) renderRating(cached);
}

function ensureRatingTableHead() {
  const table = $("rating-table");
  const thead = table?.querySelector("thead");
  if (!thead) return;
  const isMatrix = ratingScope === "matrix";
  if (isMatrix) {
    const dayHeads = RATING_WEEKDAY_KEYS.map(
      (k) =>
        `<th class="rating-sort num rating-matrix-day" data-rating-sort="pnl_${k}" data-label-base="${RATING_WEEKDAY_SHORT[k]}" title="Сортировать по ${RATING_WEEKDAY_SHORT[k]}">${RATING_WEEKDAY_SHORT[k]}</th>`
    ).join("");
    thead.innerHTML = `<tr>
      <th class="rating-sort" data-rating-sort="rank" data-label-base="#" title="Сортировать">#</th>
      <th class="rating-sort rating-matrix-strategy" data-rating-sort="label" id="rating-col-label" data-label-base="Стратегия" title="Сортировать">Стратегия</th>
      ${dayHeads}
      <th class="rating-sort num is-active" data-rating-sort="pnl" data-label-base="Итого" title="Сортировать">Итого</th>
    </tr>`;
  } else if (thead.dataset.mode === "matrix" || !thead.querySelector("#rating-col-label")) {
    thead.innerHTML = RATING_DEFAULT_THEAD;
  }
  thead.dataset.mode = isMatrix ? "matrix" : "default";
  // Re-bind sort headers after thead rebuild
  thead.querySelectorAll(".rating-sort").forEach((th) => {
    if (th.dataset.bound) return;
    th.dataset.bound = "1";
    th.addEventListener("click", () => setRatingSort(th.dataset.ratingSort || "pnl"));
  });
}

function updateRatingFilterUi() {
  const isPairs = ratingScope === "pairs";
  const isStratPairs = ratingScope === "stratpairs";
  const isDays = ratingScope === "days";
  const isMatrix = ratingScope === "matrix";
  syncRatingUserFilterVisibility();
  ensureRatingTableHead();
  document.querySelectorAll(".rating-scope").forEach((btn) => {
    const active = btn.dataset.ratingScope === ratingScope;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll(".rating-period").forEach((btn) => {
    const custom = Boolean(ratingDateFrom || ratingDateTo);
    btn.classList.toggle("is-active", !custom && btn.dataset.ratingPeriod === ratingPeriod);
  });
  syncDateInputs("rating-date-from", "rating-date-to", ratingDateFrom, ratingDateTo);
  document.querySelectorAll(".rating-tf").forEach((btn) => {
    btn.classList.toggle("is-active", (btn.dataset.ratingTf || "5m") === ratingTf);
  });
  document.querySelectorAll(".rating-bot").forEach((btn) => {
    btn.classList.toggle("is-active", (btn.dataset.ratingBot || "all") === ratingBot);
  });
  document.querySelectorAll(".rating-side").forEach((btn) => {
    btn.classList.toggle("is-active", (btn.dataset.ratingSide || "all") === ratingSide);
  });
  const sel = $("rating-user-filter");
  if (sel && sel.value !== ratingUserFilter) sel.value = ratingUserFilter;

  const title = $("rating-title");
  const subtitle = $("rating-subtitle");
  const account = ratingAccountLabel();
  if (title) {
    title.textContent = isMatrix
      ? "Стратегии × дни недели"
      : isDays
        ? "Рейтинг по дням недели"
        : isStratPairs
          ? "Стратегии / пары"
          : isPairs
            ? "Рейтинг пар"
            : "Рейтинг стратегий";
  }
  if (subtitle) {
    subtitle.textContent = isMatrix
      ? `${account} · строки — стратегии, колонки — Пн–Вс · PnL с комиссией`
      : isDays
        ? `${account} · Пн–Вс · клик по дню — стратегии · PnL с комиссией`
        : isStratPairs
          ? `${account} · клик по стратегии — топ пар · PnL с комиссией`
          : isPairs
            ? `${account} · strategy / finder / grid · клик по паре — детализация`
            : `${account} · закрытые сделки strategy-бота`;
    subtitle.textContent += ratingTf === "1h" ? " · 1 ч" : " · 5 мин";
    const botLabel = ratingBotLabel();
    if (botLabel) subtitle.textContent += ` · ${botLabel}`;
    if (ratingSide !== "all") subtitle.textContent += ` · ${tradeSideLabel(ratingSide)}`;
  }

  const labelTh = $("rating-col-label");
  if (labelTh && !isMatrix) {
    labelTh.dataset.labelBase = isDays ? "День недели" : isPairs ? "Пара" : "Стратегия";
    labelTh.dataset.ratingSort = isDays ? "weekday_order" : "label";
  }

  const table = $("rating-table");
  table?.classList.toggle("rating-matrix-table", isMatrix);

  document.querySelectorAll("#rating-table .rating-sort").forEach((th) => {
    const key = th.dataset.ratingSort;
    const active = key === ratingSortKey;
    th.classList.toggle("is-active", active);
    th.classList.toggle("is-asc", active && !ratingSortDesc);
    th.classList.toggle("is-desc", active && ratingSortDesc);
    const base = th.dataset.labelBase || th.textContent.replace(/\s*[▲▼]\s*$/, "").trim();
    th.dataset.labelBase = base;
    th.textContent = active ? `${base} ${ratingSortDesc ? "▼" : "▲"}` : base;
  });
}

function ratingSortValue(row, key) {
  if (key === "weekday_order" || key === "day") {
    const order = Number(row.weekday_order);
    if (Number.isFinite(order)) return order;
    const map = { mon: 0, tue: 1, wed: 2, thu: 3, fri: 4, sat: 5, sun: 6 };
    return map[String(row.day || row.weekday || "")] ?? 99;
  }
  if (typeof key === "string" && key.startsWith("pnl_")) {
    const day = key.slice(4);
    const cell = row.by_weekday?.[day];
    if (cell && cell.pnl != null) return Number(cell.pnl);
    return Number(row[key] ?? 0);
  }
  if (key === "label") {
    if (ratingScope !== "matrix" && (row.weekday_order != null || row.day)) {
      return ratingSortValue(row, "weekday_order");
    }
    return String(row.label || row.pair || row.strategy_id || "").toLowerCase();
  }
  if (key === "rank") return Number(row.rank || 0);
  if (key === "conf_avg" || key === "conf_min" || key === "conf_max") {
    const v = row[key];
    return v == null || Number.isNaN(Number(v)) ? (ratingSortDesc ? -1 : 1e9) : Number(v);
  }
  return Number(row[key] ?? 0);
}

function sortedRatingRows(data) {
  const rows = Array.isArray(data?.rows)
    ? [...data.rows]
    : Array.isArray(data?.pnl)
      ? [...data.pnl]
      : [];
  const desc = ratingSortDesc;
  const key = ratingSortKey;
  rows.sort((a, b) => {
    const av = ratingSortValue(a, key);
    const bv = ratingSortValue(b, key);
    if (typeof av === "string" || typeof bv === "string") {
      const cmp = String(av).localeCompare(String(bv), "ru");
      return desc ? -cmp : cmp;
    }
    if (av === bv) return 0;
    return desc ? (bv > av ? 1 : -1) : av > bv ? 1 : -1;
  });
  return rows;
}

function setRatingSort(key) {
  if (!key) return;
  if (ratingSortKey === key) ratingSortDesc = !ratingSortDesc;
  else {
    ratingSortKey = key;
    // weekday Mon→Sun asc; label asc; numbers (pnl/trades/...) desc first
    if (key === "day" || key === "weekday_order") ratingSortDesc = false;
    else ratingSortDesc = key !== "label";
  }
  const cached = ratingDataCache[ratingCacheKey()];
  if (cached) renderRating(cached);
  else loadRating(false);
}

function fmtConfPct(v) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  return `${Number(v).toFixed(1)}%`;
}

function ratingMetricCells(r) {
  const income = Number(r.income ?? 0);
  const lossAbs = Number(r.loss_abs ?? Math.abs(r.loss_sum ?? 0));
  const pnl = Number(r.pnl ?? 0);
  const wr = r.winrate != null ? Number(r.winrate) : null;
  const incomeCls = income > 0 ? "pos" : "";
  const lossCls = lossAbs > 0 ? "neg" : "";
  const pnlCls = pnl > 0 ? "pos" : pnl < 0 ? "neg" : "";
  const wrBar =
    wr == null
      ? "—"
      : `<span class="rating-wr">
          <span class="rating-wr-val">${wr.toFixed(1)}</span>
          <span class="rating-wr-track" aria-hidden="true"><span class="rating-wr-fill" style="width:${Math.max(0, Math.min(100, wr))}%"></span></span>
        </span>`;
  const confCell = `<span class="rating-conf" title="Мин / сред / макс уверенность модели">
      <span class="rating-conf-min">${fmtConfPct(r.conf_min)}</span>
      <span class="rating-conf-sep">/</span>
      <span class="rating-conf-avg">${fmtConfPct(r.conf_avg)}</span>
      <span class="rating-conf-sep">/</span>
      <span class="rating-conf-max">${fmtConfPct(r.conf_max)}</span>
    </span>`;
  return {
    income,
    lossAbs,
    pnl,
    incomeCls,
    lossCls,
    pnlCls,
    wrBar,
    cells: `
      <td class="num">${r.trades ?? 0}</td>
      <td class="num rating-wr-cell">${wrBar}</td>
      <td class="num rating-conf-cell">${confCell}</td>
      <td class="num ${incomeCls}">${fmtUsd(income)}</td>
      <td class="num ${lossCls}">${lossAbs > 0 ? fmtUsd(-lossAbs) : fmtUsd(0)}</td>
      <td class="num rating-net ${pnlCls}"><strong>${fmtUsd(pnl)}</strong></td>`,
  };
}

/** Plain display name for rating rows (no «инв» suffix). */
function ratingCopyName(row, { preferBase = false } = {}) {
  const inverted = !!row?.inverted;
  let text = preferBase
    ? String(row?.pair_base || row?.strategy_base || row?.label || row?.strategy_id || row?.pair || "")
    : String(row?.label || row?.strategy_label || row?.strategy_id || row?.pair || "");
  if (inverted) {
    text = text.replace(/\s*·\s*инв\s*$/i, "").trim() || text;
  }
  return text.trim();
}

/** Display name + optional «инв» badge for rating rows. */
function ratingInvLabelHtml(row, { preferBase = false } = {}) {
  const inverted = !!row?.inverted;
  const text = ratingCopyName(row, { preferBase }) || "—";
  const badge = inverted ? ' <span class="strategy-inv-badge">инв</span>' : "";
  return `${escapeHtml(text)}${badge}`;
}

const RATING_COPY_ICON = `<svg class="rating-copy-icon" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false"><path fill="currentColor" d="M5.5 2A1.5 1.5 0 0 0 4 3.5V11h1.5V3.5h7V2h-7Zm2 2.5A1.5 1.5 0 0 0 6 6v7.5A1.5 1.5 0 0 0 7.5 15h6A1.5 1.5 0 0 0 15 13.5V6A1.5 1.5 0 0 0 13.5 4.5h-6ZM7.5 6h6v7.5h-6V6Z"/></svg>`;

function ratingCopyBtnHtml(name) {
  const text = String(name || "").trim();
  if (!text || text === "—") return "";
  return `<button type="button" class="rating-copy-btn" data-copy="${escapeHtml(text)}" title="Копировать название" aria-label="Копировать ${escapeHtml(text)}">${RATING_COPY_ICON}</button>`;
}

function ratingNameCellHtml(row, { preferBase = false } = {}) {
  const name = ratingCopyName(row, { preferBase });
  return `<span class="rating-name-wrap"><span class="rating-name-text">${ratingInvLabelHtml(row, { preferBase })}</span>${ratingCopyBtnHtml(name)}</span>`;
}

async function copyRatingName(text, btn) {
  const value = String(text || "").trim();
  if (!value) return;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
    } else {
      const ta = document.createElement("textarea");
      ta.value = value;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    if (btn) {
      btn.classList.add("is-copied");
      const prev = btn.getAttribute("title") || "Копировать название";
      btn.setAttribute("title", "Скопировано");
      clearTimeout(btn._copyTimer);
      btn._copyTimer = setTimeout(() => {
        btn.classList.remove("is-copied");
        btn.setAttribute("title", prev);
      }, 1200);
    }
  } catch {
    /* ignore */
  }
}

function bindRatingCopyButtons(root) {
  root?.querySelectorAll(".rating-copy-btn").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      copyRatingName(btn.dataset.copy || "", btn);
    });
  });
}

function renderRatingDetailRows(groupId, details) {
  if (!Array.isArray(details) || !details.length) {
    return `<tr class="rating-detail-row" data-pair="${escapeHtml(groupId)}">
      <td></td>
      <td colspan="7" class="muted rating-detail-empty">Нет детализации</td>
    </tr>`;
  }
  return details
    .map((d) => {
      const m = ratingMetricCells(d);
      const bot = escapeHtml(d.bot_label || d.bot || "");
      return `<tr class="rating-detail-row ${d.inverted ? "is-inverted" : ""}" data-pair="${escapeHtml(groupId)}">
        <td class="rating-detail-pad"></td>
        <td class="rating-detail-label">
          <span class="rating-detail-strategy">${ratingNameCellHtml(d)}</span>
          ${bot ? `<span class="rating-detail-bot">${bot}</span>` : ""}
        </td>
        ${m.cells}
      </tr>`;
    })
    .join("");
}

function ratingMatrixDayCell(cell) {
  const pnl = Number(cell?.pnl ?? 0);
  const trades = Number(cell?.trades ?? 0);
  if (!trades && !pnl) {
    return `<td class="num rating-matrix-cell muted">—</td>`;
  }
  const cls = pnl > 0 ? "pos" : pnl < 0 ? "neg" : "";
  const title = escapeHtml(`${trades} сд. · WR ${cell?.winrate != null ? Number(cell.winrate).toFixed(0) + "%" : "—"}`);
  return `<td class="num rating-matrix-cell ${cls}" title="${title}">
    <span class="rating-matrix-pnl">${fmtUsdSigned(pnl, 2)}</span>
    <span class="rating-matrix-trades muted">${trades}</span>
  </td>`;
}

function renderRatingMatrix(data, rows) {
  const body = $("rating-table-body");
  if (!body) return;
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="10" class="stats-history-empty">Нет закрытых сделок за период</td></tr>`;
    return;
  }
  body.innerHTML = rows
    .map((r, idx) => {
      const place = idx + 1;
      const placeCls =
        place === 1 ? "is-gold" : place === 2 ? "is-silver" : place === 3 ? "is-bronze" : "";
      const total = Number(r.pnl ?? 0);
      const totalCls = total > 0 ? "pos" : total < 0 ? "neg" : "";
      const dayCells = RATING_WEEKDAY_KEYS.map((k) =>
        ratingMatrixDayCell(r.by_weekday?.[k] || { pnl: r[`pnl_${k}`], trades: r[`trades_${k}`] })
      ).join("");
      return `<tr class="rating-row rating-matrix-row ${placeCls} ${r.inverted ? "is-inverted" : ""}">
        <td class="rating-place"><span class="rating-place-badge">${place}</span></td>
        <td class="rating-strategy rating-matrix-strategy">${ratingNameCellHtml(r, { preferBase: true })}</td>
        ${dayCells}
        <td class="num rating-net ${totalCls}"><strong>${fmtUsdSigned(total, 2)}</strong></td>
      </tr>`;
    })
    .join("");
  bindRatingCopyButtons(body);
}

function renderRating(data) {
  updateRatingFilterUi();
  const rows = sortedRatingRows(data);
  const body = $("rating-table-body");
  const status = $("rating-status");
  const summary = $("rating-summary");
  if (!body) return;
  const isPairs = ratingScope === "pairs";
  const isStratPairs = ratingScope === "stratpairs";
  const isDays = ratingScope === "days";
  const isMatrix = ratingScope === "matrix";
  const expandable = isPairs || isDays || isStratPairs;

  if (isMatrix) {
    renderRatingMatrix(data, rows);
  } else if (!rows.length) {
    body.innerHTML = `<tr><td colspan="8" class="stats-history-empty">Нет закрытых сделок за период</td></tr>`;
  } else if (!expandable) {
    body.innerHTML = rows
      .map((r, idx) => {
        const m = ratingMetricCells(r);
        const place = idx + 1;
        const placeCls =
          place === 1 ? "is-gold" : place === 2 ? "is-silver" : place === 3 ? "is-bronze" : "";
        return `<tr class="rating-row ${placeCls} ${r.inverted ? "is-inverted" : ""}">
          <td class="rating-place"><span class="rating-place-badge">${place}</span></td>
          <td class="rating-strategy">${ratingNameCellHtml(r, { preferBase: true })}</td>
          ${m.cells}
        </tr>`;
      })
      .join("");
    bindRatingCopyButtons(body);
  } else {
    body.innerHTML = rows
      .map((r, idx) => {
        const m = ratingMetricCells(r);
        const place = idx + 1;
        const placeCls =
          place === 1 ? "is-gold" : place === 2 ? "is-silver" : place === 3 ? "is-bronze" : "";
        const groupId = isDays
          ? String(r.day || r.label || "")
          : isStratPairs
            ? String(r.strategy_id || r.label || "")
            : String(r.pair || r.label || "");
        const copyName = isDays
          ? ""
          : ratingCopyName(r, { preferBase: true });
        const groupLabelHtml = isDays
          ? escapeHtml(String(r.label || r.day || "—"))
          : ratingInvLabelHtml(r, { preferBase: true });
        const expanded = ratingExpandedPairs.has(groupId);
        const details = Array.isArray(r.details) ? r.details : [];
        const chevron = expanded ? "▾" : "▸";
        const pairsTotal = Number(r.pairs_total || details.length);
        const meta = isDays
          ? `${details.length} стр.`
          : isStratPairs
            ? pairsTotal > details.length
              ? `топ ${details.length} из ${pairsTotal}`
              : `${details.length} пар`
            : `${details.length} ист.`;
        const main = `<tr class="rating-row rating-pair-row ${placeCls} ${expanded ? "is-expanded" : ""} ${!isDays && r.inverted ? "is-inverted" : ""}" data-pair="${escapeHtml(groupId)}" tabindex="0" role="button" aria-expanded="${expanded ? "true" : "false"}">
          <td class="rating-place"><span class="rating-place-badge">${place}</span></td>
          <td class="rating-strategy rating-pair-label">
            <span class="rating-expand-icon" aria-hidden="true">${chevron}</span>
            <span class="rating-name-wrap"><span class="rating-name-text">${groupLabelHtml}</span>${ratingCopyBtnHtml(copyName)}</span>
            <span class="rating-pair-meta muted">${meta}</span>
          </td>
          ${m.cells}
        </tr>`;
        return expanded ? main + renderRatingDetailRows(groupId, details) : main;
      })
      .join("");

    body.querySelectorAll(".rating-pair-row").forEach((tr) => {
      const pairId = tr.dataset.pair || "";
      const toggle = () => toggleRatingPairExpand(pairId);
      tr.addEventListener("click", toggle);
      tr.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          toggle();
        }
      });
    });
    bindRatingCopyButtons(body);
  }

  const periodLabel = statsPeriodLabel(ratingPeriod, ratingDateFrom, ratingDateTo);
  const users = data?.users ?? 0;
  const trades = data?.trades_total ?? 0;
  const entityLabel = isDays ? "Дней недели" : isPairs ? "Пар" : "Стратегий";
  const entityCount = isDays
    ? data?.days_total ?? rows.length
    : isPairs
      ? data?.pairs_total ?? rows.length
      : data?.strategies_total ?? rows.length;
  if (summary) {
    const accountChip = data?.user_label
      ? `<div class="rating-chip"><span>Аккаунт</span><strong>${escapeHtml(data.user_label)}</strong></div>`
      : `<div class="rating-chip"><span>Аккаунтов</span><strong>${users}</strong></div>`;
    summary.innerHTML = `
      <div class="rating-chip"><span>Период</span><strong>${escapeHtml(periodLabel)}</strong></div>
      <div class="rating-chip"><span>${entityLabel}</span><strong>${entityCount}</strong></div>
      <div class="rating-chip"><span>Сделок</span><strong>${trades}</strong></div>
      ${accountChip}
      <div class="rating-chip"><span>Сторона</span><strong>${escapeHtml(tradeSideLabel(ratingSide))}</strong></div>
    `;
  }
  if (status) {
    status.textContent = isMatrix
      ? "Стратегии слева · дни недели сверху · клик по Пн–Вс или «Итого» — сортировка · комиссия 0.1%×2"
      : isDays
        ? "Клик по дню недели — стратегии · сортировка Пн→Вс или по PnL · комиссия 0.1%×2"
        : isStratPairs
          ? "Клик по стратегии — топ пар по чистому PnL · комиссия 0.1%×2"
          : isPairs
            ? "Клик по паре — стратегии и боты · PnL с комиссией 0.1%×2 · Убытки = сумма минусов · Чистый PnL = итог"
            : "Клик по заголовку — сортировка · PnL с комиссией 0.1%×2 · Убытки = сумма минусов · Чистый PnL = итог";
  }
}

async function loadRating(force) {
  const status = $("rating-status");
  if (status) status.textContent = "Загрузка…";
  updateRatingFilterUi();
  const scope = ratingScope;
  const user = ratingUserFilter || "";
  const tf = ratingTf;
  const cacheKey = ratingCacheKey(scope, user);
  try {
    const cached = ratingDataCache[cacheKey];
    if (
      !force &&
      cached &&
      cached.period === ratingPeriod &&
      (cached.date_from || "") === ratingDateFrom &&
      (cached.date_to || "") === ratingDateTo &&
      (cached.tf || "5m") === tf &&
      (cached.user_filter || "") === (user || "") &&
      normalizeTradeSide(cached.side || "all") === ratingSide &&
      (cached.account_mode || "all") === ratingAccountMode
    ) {
      renderRating(cached);
      return;
    }
    const params = new URLSearchParams({
      period: ratingPeriod,
      tf,
      limit: scope === "days" ? "7" : "100",
      account_mode: ratingAccountMode || "all",
    });
    if (ratingDateFrom) params.set("from", ratingDateFrom);
    if (ratingDateTo) params.set("to", ratingDateTo);
    if (user) params.set("user", user);
    if (ratingBot && ratingBot !== "all") params.set("bot", ratingBot);
    if (ratingSide && ratingSide !== "all") params.set("side", ratingSide);
    const endpoint =
      scope === "pairs"
        ? `/pair-rating?${params}`
        : scope === "stratpairs"
          ? `/strategy-pair-rating?${params}`
          : scope === "days"
            ? `/strategy-rating-daily?${params}`
            : scope === "matrix"
              ? `/strategy-rating-weekday-matrix?${params}`
              : `/strategy-rating?${params}`;
    const data = await pairConfigApi(endpoint);
    data.date_from = ratingDateFrom;
    data.date_to = ratingDateTo;
    ratingDataCache[cacheKey] = data;
    if (ratingScope === scope && ratingTf === tf && ratingUserFilter === user) {
      renderRating(data);
    }
  } catch (e) {
    if (e.message === "auth") logout();
    else if (status) status.textContent = formatApiError(e.message);
  }
}

function bindRatingControls() {
    document.querySelectorAll(".rating-mode").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setRatingAccountMode(btn.dataset.ratingMode || "all"));
  });
  document.querySelectorAll(".rating-period").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setRatingPeriod(btn.dataset.ratingPeriod || "all"));
  });
  $("rating-date-from")?.addEventListener("change", applyRatingDateRange);
  $("rating-date-to")?.addEventListener("change", applyRatingDateRange);
  document.querySelectorAll(".rating-tf").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setRatingTf(btn.dataset.ratingTf || "5m"));
  });
  document.querySelectorAll(".rating-bot").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setRatingBot(btn.dataset.ratingBot || "all"));
  });
  document.querySelectorAll(".rating-side").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setRatingSide(btn.dataset.ratingSide || "all"));
  });
  document.querySelectorAll(".rating-scope").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setRatingScope(btn.dataset.ratingScope || "strategies"));
  });
  document.querySelectorAll("#rating-table .rating-sort").forEach((th) => {
    if (th.dataset.bound) return;
    th.dataset.bound = "1";
    th.addEventListener("click", () => setRatingSort(th.dataset.ratingSort || "pnl"));
  });
  const userSel = $("rating-user-filter");
  if (userSel && !userSel.dataset.bound) {
    userSel.dataset.bound = "1";
    userSel.addEventListener("change", () => setRatingUserFilter(userSel.value || ""));
  }
  $("rating-back-btn")?.addEventListener("click", closeRating);
  $("rating-refresh-btn")?.addEventListener("click", () => loadRating(true));
}

function addClosedTradeToRow(row, trade) {
  // Stats / PnL-dashboard: net of fixed Bybit fee (0.1% × 2). Rating uses same on API.
  const p = closedTradePnlNet(trade).abs;
  row.closed = (row.closed || 0) + 1;
  if (p > 0) {
    row.wins += 1;
    row.profit += p;
  } else if (p < 0) {
    row.losses += 1;
    row.loss += Math.abs(p);
  } else {
    row.flat = (row.flat || 0) + 1;
  }
}

function updateStatsPeriodUi() {
  const custom = Boolean(statsDateFrom || statsDateTo);
  document.querySelectorAll(".stats-period-btn").forEach((btn) => {
    const active = !custom && (btn.dataset.period || "all") === statsPeriod;
    btn.classList.toggle("is-active", active);
  });
  syncDateInputs("stats-date-from", "stats-date-to", statsDateFrom, statsDateTo);
}

function updateStatsModalChrome() {
  const meta = STATS_SCOPE_META[statsScope] || STATS_SCOPE_META.all;
  const titleEl = $("stats-view-title");
  const noteEl = $("stats-view-note");
  if (titleEl) titleEl.textContent = meta.title;
  if (noteEl) {
    const periodLabel = statsPeriodLabel(statsPeriod, statsDateFrom, statsDateTo);
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
  const finderFiltered = filterClosedTrades(data.finderTrades, statsPeriod, statsDateFrom, statsDateTo);
  const stratFiltered = filterClosedTrades(data.stratTrades, statsPeriod, statsDateFrom, statsDateTo);
  const gridFiltered = filterClosedTrades(data.gridTrades, statsPeriod, statsDateFrom, statsDateTo);
  const bybitFiltered = filterBybitHistory(data.bybitHistory, statsPeriod, statsDateFrom, statsDateTo);

  const rows = filterStatsRows(
    buildStatsRows(finderFiltered, stratFiltered, gridFiltered, bybitFiltered, catalog),
    statsScope,
    catalog
  );
  // Active strategies first; idle (no trades in period) sink to the bottom.
  rows.sort((a, b) => {
    const aActive = a.closed || a.wins || a.losses || a.flat ? 1 : 0;
    const bActive = b.closed || b.wins || b.losses || b.flat ? 1 : 0;
    return bActive - aActive;
  });

  let totalProfit = 0;
  let totalLoss = 0;
  let totalWins = 0;
  let totalLosses = 0;
  let totalFlat = 0;
  let totalClosed = 0;
  for (const r of rows) {
    totalProfit += r.profit;
    totalLoss += r.loss;
    totalWins += r.wins;
    totalLosses += r.losses;
    totalFlat += r.flat || 0;
    totalClosed += r.closed || r.wins + r.losses + (r.flat || 0);
  }
  const net = totalProfit - totalLoss;
  const closedCount = totalClosed || totalWins + totalLosses;
  const warnHtml =
    data.loadErrors?.length > 0
      ? `<p class="stats-load-warn">${data.loadErrors.map((e) => escapeHtml(e)).join(" · ")}</p>`
      : "";

  summaryEl.innerHTML = `
    ${warnHtml}
    <div class="stats-kpi stats-kpi-income">
      <span>Прибыль</span>
      <strong class="pos">${fmtUsd(totalProfit)}</strong>
    </div>
    <div class="stats-kpi stats-kpi-expense">
      <span>Потери</span>
      <strong class="neg">${fmtUsd(totalLoss)}</strong>
    </div>
    <div class="stats-kpi stats-kpi-net">
      <span>Итого</span>
      <strong class="${pnlClass(net)}">${fmtUsdSigned(net)}</strong>
    </div>
    <div class="stats-kpi stats-kpi-ratio">
      <span>Успешные / неуспешные</span>
      <strong>${totalWins} / ${totalLosses}${totalFlat ? ` · 0: ${totalFlat}` : ""}</strong>
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
          const hasActivity = r.closed || r.wins || r.losses || r.flat;
          return `<tr class="${hasActivity ? "" : "stats-row-idle"}">
            <td>${escapeHtml(r.name)}</td>
            <td>${r.wins || "—"}</td>
            <td>${r.losses || "—"}</td>
            <td>${profitCell}</td>
            <td>${lossCell}</td>
          </tr>`;
        })
        .join("")
    : '<tr class="stats-history-empty"><td colspan="5">Нет данных за выбранный период</td></tr>';

  if (footEl) {
    const flatNote = totalFlat ? ` (в т.ч. ${totalFlat} с нулевым PnL)` : "";
    footEl.textContent =
      closedCount > 0
        ? `Закрытых операций за период: ${closedCount}${flatNote}. Учитываются все сделки из БД, включая выключенные стратегии.`
        : "Нет закрытых операций за выбранный период.";
  }

  updateStatsModalChrome();
}

async function fetchClosedTradesFromDb(limit = CLOSED_TRADES_FETCH_LIMIT) {
  const q = limit <= 0 ? "all" : String(limit);
  return pairConfigApi(`/closed-trades?limit=${encodeURIComponent(q)}`);
}

function mergeClosedTradeLists(a, b) {
  const map = new Map();
  for (const list of [a || [], b || []]) {
    for (const t of list) {
      const id = t.trade_id ?? t.id;
      const key = id != null ? String(id) : `anon-${map.size}-${t.pair || ""}-${t.close_date || ""}`;
      const prev = map.get(key);
      map.set(key, prev ? { ...prev, ...t } : t);
    }
  }
  return Array.from(map.values());
}

async function fetchBotClosedTrades(bot, limit = 5000) {
  try {
    const pageSize = 500;
    const all = [];
    let offset = 0;
    let total = Infinity;
    while (offset < limit && offset < total) {
      const take = Math.min(pageSize, limit - offset);
      const data = await api(bot, `/trades?limit=${take}&offset=${offset}&order_by_id=false`);
      const trades = Array.isArray(data?.trades) ? data.trades : [];
      if (!trades.length) break;
      all.push(...trades);
      total = Number(data?.total_trades ?? trades.length);
      offset += trades.length;
      if (trades.length < take) break;
    }
    return all.filter((t) => !t.is_open);
  } catch {
    return [];
  }
}

/** Server keeps a warm /stats-bundle cache; client only renders it. */
async function fetchStatsData(_tradeLimit = CLOSED_TRADES_FETCH_LIMIT, { force = false, accountMode = null } = {}) {
  const mode = accountMode || statsAccountMode || pnlDashMode || "all";
  const params = new URLSearchParams();
  if (force) params.set("force", "1");
  params.set("account_mode", mode);
  const qs = `?${params.toString()}`;
  const data = await pairConfigApi(`/stats-bundle${qs}`);
  return {
    finderTrades: data.finder || [],
    stratTrades: data.strategy || [],
    gridTrades: data.grid || [],
    bybitHistory: data.bybit_history || [],
    catalog: data.strategies || strategyCatalog,
    openSummary: data.open_summary || null,
    loadErrors: [],
    builtAt: data.built_at || null,
    cached: !!data.cached,
    ageSec: data.age_sec,
  };
}

let statsPrefetchInFlight = null;
let lastStatsPrefetchAt = 0;
const STATS_PREFETCH_MS = 25000;
let openSummaryCache = null;
/** Last non-empty summary — avoids UI flashing 0 when live/status briefly empty. */
let openSummarySticky = null;
const OPEN_SUMMARY_STICKY_MS = 45000;

function adoptOpenSummary(summary) {
  if (!summary || typeof summary !== "object") return openSummaryCache;
  const next = { ...summary };
  const count = Number(next.count) || 0;
  const maxTotal = Number(next.max_total) || 0;
  const now = Date.now();

  if (count > 0) {
    openSummarySticky = { ...next, _ts: now };
    openSummaryCache = next;
    return openSummaryCache;
  }

  // count == 0: keep sticky if recent and previously had opens
  const sticky = openSummarySticky;
  if (
    sticky
    && Number(sticky.count) > 0
    && now - Number(sticky._ts || 0) < OPEN_SUMMARY_STICKY_MS
  ) {
    openSummaryCache = {
      ...sticky,
      max_total: maxTotal > 0 ? maxTotal : Number(sticky.max_total) || 0,
      // keep margin/pnl from sticky until confirmed empty after sticky window
    };
    return openSummaryCache;
  }

  openSummarySticky = null;
  openSummaryCache = next;
  return openSummaryCache;
}

async function prefetchStatsBundle({ force = false, accountMode = null } = {}) {
  const mode = accountMode || statsAccountMode || "all";
  const now = Date.now();
  if (!force && statsDataCache && now - lastStatsPrefetchAt < STATS_PREFETCH_MS) {
    if (statsDataCache.openSummary) {
      const summary = statsDataCache.openSummary;
      adoptOpenSummary(summary?.live || summary?.demo || summary);
      renderTradesSummaryFromOpen(openSummaryCache);
    }
    return statsDataCache;
  }
  if (statsPrefetchInFlight) return statsPrefetchInFlight;
  statsPrefetchInFlight = (async () => {
    try {
      const data = await fetchStatsData(CLOSED_TRADES_FETCH_LIMIT, {
        force,
        accountMode: mode,
      });
      statsDataCache = data;
      historyDataCache = data;
      if (!pnlDashDataCache) pnlDashDataCache = data;
      lastStatsPrefetchAt = Date.now();
      if (data.openSummary) {
        const summary = data.openSummary;
        adoptOpenSummary(summary?.live || summary?.demo || summary);
        renderTradesSummaryFromOpen(openSummaryCache);
      }
      return data;
    } finally {
      statsPrefetchInFlight = null;
    }
  })();
  return statsPrefetchInFlight;
}

async function loadStatistics(force = false) {
  const summaryEl = $("stats-summary");
  const bodyEl = $("stats-table-body");
  if (!bodyEl) return;

  if (statsDataCache && !force) {
    renderStatsModal(statsDataCache);
  } else {
    summaryEl.innerHTML = '<p class="muted">Загрузка…</p>';
    bodyEl.innerHTML = "";
  }

  try {
    statsDataCache = await prefetchStatsBundle({
      force: !!force,
      accountMode: statsAccountMode,
    });
    renderStatsModal(statsDataCache);
  } catch (e) {
    if (e.message === "auth") logout();
    else if (!statsDataCache) {
      summaryEl.innerHTML = `<p class="error">${escapeHtml(formatApiError(e.message))}</p>`;
      const footEl = $("stats-footnote");
      if (footEl) footEl.textContent = "";
      bodyEl.innerHTML = "";
    }
  }
}

function setStatsPeriod(period) {
  statsPeriod = normalizeStatsPeriod(period);
  statsDateFrom = "";
  statsDateTo = "";
  if (statsDataCache) renderStatsModal(statsDataCache);
  else loadStatistics();
}

function applyStatsDateRange() {
  statsDateFrom = $("stats-date-from")?.value || "";
  statsDateTo = $("stats-date-to")?.value || "";
  if (statsDataCache) renderStatsModal(statsDataCache);
  else loadStatistics();
}

function openStats(scope = "all", period = null) {
  statsScope = STATS_SCOPE_META[scope] ? scope : "all";
  if (period) {
    statsPeriod = normalizeStatsPeriod(period);
    statsDateFrom = "";
    statsDateTo = "";
  }
  closeMobileMenu();
  hideMainViewsForOverlay();
  const view = $("stats-view");
  view?.classList.remove("hidden");
  view?.setAttribute("aria-hidden", "false");
  if (statsDataCache) renderStatsModal(statsDataCache);
  loadStatistics();
}

function closeStats() {
  $("stats-view")?.classList.add("hidden");
  $("stats-view")?.setAttribute("aria-hidden", "true");
  $("dashboard-view")?.classList.remove("hidden");
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
  $("stats-date-from")?.addEventListener("change", applyStatsDateRange);
  $("stats-date-to")?.addEventListener("change", applyStatsDateRange);
  $("stats-back-btn")?.addEventListener("click", closeStats);
  $("stats-refresh-btn")?.addEventListener("click", () => loadStatistics(true));
  document.querySelectorAll(".stats-mode-btn").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => {
      setStatsAccountMode(btn.dataset.statsMode || "all");
      loadStatistics(true).catch(() => {});
    });
  });
}

/** Stable display: `#num name` — num never changes when strategies are added/removed. */
function strategyCatalogLabel(s) {
  if (!s) return "—";
  const name = s.name || s.id || "—";
  if (s.num != null && s.num !== "" && Number.isFinite(Number(s.num))) {
    return `#${Number(s.num)} ${name}`;
  }
  return name;
}

function strategyLabel(id) {
  const fromCatalog = strategyCatalog.find((s) => s.id === id);
  return fromCatalog ? strategyCatalogLabel(fromCatalog) : id || "—";
}

/** Stats/history row key by enter_tag base (+inv/hedge) — not by display name. */
function tradeStatsRowKey(trade, bot) {
  if (bot === "finder") return FINDER_STATS_LABEL;
  if (bot === "grid") return GRID_STATS_LABEL;
  if (isPriceActionTrade(trade)) return "__price_action__";
  const base = tradeSourceId(trade, bot);
  if (!trade?.enter_tag) return base;
  const raw = String(trade.enter_tag);
  let key = base;
  if (/:inv/i.test(raw)) key += ":inv";
  if (/:hedge/i.test(raw)) key += ":hedge";
  return key;
}

function syncEnabledFromPayload(data) {
  strategyCatalog = data.strategies || strategyCatalog;
  strategyCatalog = [...strategyCatalog].sort((a, b) => {
    const aHas = a.num != null && a.num !== "" && Number.isFinite(Number(a.num));
    const bHas = b.num != null && b.num !== "" && Number.isFinite(Number(b.num));
    if (aHas !== bHas) return aHas ? -1 : 1;
    if (aHas && bHas) {
      const an = Number(a.num);
      const bn = Number(b.num);
      if (an !== bn) return an - bn;
    }
    return String(a.id || "").localeCompare(String(b.id || ""));
  });
  enabledStrategies = {};
  invertedStrategies = {};
  trainedRiskStrategies = {};
  mlConfidenceStrategies = {};
  entrySideStrategies = {};
  strategyPairsMap = {};
  trainedRiskAvailable = data.trained_risk_available || {};
  trainedRiskSpecs = data.trained_risk_specs || {};
  mlConfidenceDefaults = data.ml_confidence_defaults || {};
  if (Array.isArray(data.ml_confidence_choices) && data.ml_confidence_choices.length) {
    mlConfidenceChoices = data.ml_confidence_choices.map(Number);
  }
  htfSltp = data.sltp && typeof data.sltp === "object" ? data.sltp : {};
  htfSltpDefaults = data.sltp_defaults && typeof data.sltp_defaults === "object" ? data.sltp_defaults : {};
  if (Array.isArray(data.sl_choices) && data.sl_choices.length) {
    sltpSlChoices = data.sl_choices.map(Number);
  } else if (Array.isArray(data.sltp_choices) && data.sltp_choices.length) {
    sltpSlChoices = data.sltp_choices.map(Number);
  }
  if (Array.isArray(data.tp_choices) && data.tp_choices.length) {
    sltpTpChoices = data.tp_choices.map(Number);
  } else if (Array.isArray(data.sltp_choices) && data.sltp_choices.length) {
    sltpTpChoices = data.sltp_choices.map(Number);
  }
  htfSltpChoices = sltpSlChoices;
  if (data.htf_stake_amount != null && Number.isFinite(Number(data.htf_stake_amount))) {
    htfStakeAmount = Number(data.htf_stake_amount);
  }
  const enabled = data.enabled || {};
  const inverted = data.inverted || {};
  const trained = data.trained_risk || {};
  const mlConf = data.ml_confidence || {};
  const pairs = data.pairs || {};
  strategyPairsCascade = !!data._pairs_cascade || Object.keys(pairs).length > 0;
  for (const s of strategyCatalog) {
    enabledStrategies[s.id] = !!enabled[s.id];
    invertedStrategies[s.id] = !!inverted[s.id];
    trainedRiskStrategies[s.id] = !!trained[s.id];
    if (trainedRiskAvailable[s.id] == null) {
      trainedRiskAvailable[s.id] = !!trainedRiskSpecs[s.id];
    }
    const def =
      mlConfidenceDefaults[s.id] != null
        ? Number(mlConfidenceDefaults[s.id])
        : mlConf[s.id] != null
          ? Number(mlConf[s.id])
          : 0.55;
    mlConfidenceStrategies[s.id] = mlConf[s.id] != null ? Number(mlConf[s.id]) : def;
    strategyPairsMap[s.id] = Array.isArray(pairs[s.id]) ? pairs[s.id].slice() : [];
    const sideRaw = data.entry_sides && data.entry_sides[s.id];
    entrySideStrategies[s.id] =
      sideRaw === "long" || sideRaw === "short" || sideRaw === "both" ? sideRaw : "both";
  }
  syncStrategyRiskFromPayload(data);
  syncTestSettingsFromPayload(data);
  if (data?.dual_hedge != null) dualHedgeEnabled = !!data.dual_hedge;
  if (data?.max_open_trades_per_strategy != null) {
    applyPinnedMaxPerStrategy(data.max_open_trades_per_strategy);
    updateMaxTradesHint();
  }
  // Placement lives on the strategy panel (not only Settings) — always refresh count/lists.
  renderPlacementAdmin(data);
  repaintStrategyTradesFromCache();
}

function showPlacementMsg(text, isError = false) {
  const el = $("placement-msg");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("hidden", !text);
  el.classList.toggle("error", !!isError);
}

function bindPlacementTagsOnce() {
  if (document.documentElement.dataset.placementTagsBound) return;
  document.documentElement.dataset.placementTagsBound = "1";
  const toggle = (open) => {
    const wrap = $("placement-tags");
    const panel = $("placement-tags-panel");
    const btn = $("placement-tags-btn");
    if (!wrap || !panel || !btn) return;
    panel.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    wrap.classList.toggle("is-open", open);
    if (open) positionStrategyTickersPanel(wrap, panel);
    else resetStrategyTickersPanelPosition(panel);
  };
  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (!(t instanceof Element)) return;
    const btn = t.closest("[data-placement-tags-btn]");
    if (btn) {
      ev.preventDefault();
      ev.stopPropagation();
      const panel = $("placement-tags-panel");
      toggle(!!panel?.hidden);
      return;
    }
    if (t.closest("[data-placement-tags-wrap]")) return;
    toggle(false);
  });
}

function renderPlacementAdmin(data) {
  const testList = $("placement-test-list");
  const htfList = $("placement-htf-list");
  const mainList = $("placement-main-list");
  if (!testList || !mainList) return;
  const catalog = data?.strategies || strategyCatalog;
  const promoteCandidates = Array.isArray(data?.promote_candidates)
    ? data.promote_candidates
    : [];
  const managementCatalog = Array.isArray(data?.management_catalog)
    ? data.management_catalog
    : [];
  const byId = new Map();
  (managementCatalog.length ? managementCatalog : catalog || []).forEach((s) => {
    if (s?.id) byId.set(String(s.id), s);
  });
  (catalog || []).forEach((s) => {
    if (s?.id && !byId.has(String(s.id))) byId.set(String(s.id), s);
  });
  promoteCandidates.forEach((s) => {
    if (s?.id && !byId.has(String(s.id))) byId.set(String(s.id), s);
  });

  // Promote candidates = not yet in main (birth test stays listed forever on Test panel).
  const sourceRows = promoteCandidates.length
    ? promoteCandidates
    : (catalog || []).filter((s) => isTestCatalogEntry(s) || isHtfStrategy(s));
  const testRows = sourceRows.filter((s) => isTestCatalogEntry(s) && !isHtfStrategy(s));
  const htfRows = sourceRows.filter((s) => isHtfStrategy(s));
  const mainFromPlacement = (data?.ui_placement?.main || []).map(String);
  const mainRows = (catalog || []).filter(
    (s) => s.in_main || s.show_on_main || strategyUiPanel(s) === "main"
  );
  const displayMainRows = (data?.ui_placement
    ? mainFromPlacement
    : mainRows.map((s) => s.id)
  ).map((id) => {
    const known = byId.get(id);
    if (known) return { ...known, in_main: true, show_on_main: true };
    const htf = String(id).endsWith("1hStrategy");
    return { id, name: id, ui_panel: htf ? "htf1h" : "main", htf_group: htf, in_main: true, show_on_main: true };
  });

  const tagHtml = (s, action) => {
    const backHtf = !!(s.htf_group || s.htfGroup) || String(s.id || "").endsWith("1hStrategy");
    const title =
      action === "promote"
        ? "Скопировать в основные"
        : backHtf
          ? "Убрать из основных"
          : "Убрать из основных";
    return `<button type="button" class="placement-tag${action === "demote" ? " is-main" : ""}" data-placement-action="${action}" data-strategy="${escapeHtml(s.id)}" data-htf-back="${backHtf ? "1" : "0"}" title="${title}">${escapeHtml(strategyCatalogLabel(s))}${action === "demote" ? " ×" : ""}</button>`;
  };
  const empty = (text) => `<span class="placement-tag-empty muted">${text}</span>`;
  testList.innerHTML = testRows.length ? testRows.map((s) => tagHtml(s, "promote")).join("") : empty("пусто");
  if (htfList) {
    htfList.innerHTML = htfRows.length ? htfRows.map((s) => tagHtml(s, "promote")).join("") : empty("пусто");
  }
  mainList.innerHTML = displayMainRows.length
    ? displayMainRows.map((s) => tagHtml(s, "demote")).join("")
    : empty("пусто");
  const countEl = $("placement-tags-count");
  if (countEl) countEl.textContent = String(testRows.length + htfRows.length + displayMainRows.length);
  const leftHead = $("placement-test-head");
  if (leftHead) leftHead.textContent = "Скопировать в основные";

  const root = $("placement-tags") || testList;
  root.querySelectorAll("[data-placement-action]").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const id = btn.getAttribute("data-strategy");
      const action = btn.getAttribute("data-placement-action");
      if (!id || !action) return;
      btn.disabled = true;
      try {
        const apiAction =
          action === "promote" ? "promote_strategy_to_main" : "demote_strategy_to_test";
        const res = await pairConfigApi("/pairs", "POST", {
          action: apiAction,
          strategy: id,
        });
        syncEnabledFromPayload(res);
        renderStrategyToggles();
        updateStrategyDisplay();
        updateTestStrategyDisplay();
        renderPlacementAdmin(res);
        const name = strategyLabel(id);
        showPlacementMsg(
          action === "promote"
            ? `${name} скопирована в Основные (выкл). В Тестовых остаётся.`
            : `${name} убрана из Основных (выкл)`
        );
      } catch (e) {
        showPlacementMsg(formatApiError(e.message) || String(e), true);
        btn.disabled = false;
      }
    });
  });
  bindPlacementTagsOnce();
}

function syncStrategyRiskFromPayload(data) {
  const risk = data?.risk || data?.state?.risk;
  if (!risk) return;
  if (risk.stoploss_pct != null) strategyRisk.stoploss_pct = Number(risk.stoploss_pct);
  if (risk.take_profit_pct != null) strategyRisk.take_profit_pct = Number(risk.take_profit_pct);
  if (risk.time_close_enabled != null) strategyRisk.time_close_enabled = !!risk.time_close_enabled;
  if (risk.time_close_minutes != null) strategyRisk.time_close_minutes = Number(risk.time_close_minutes);
  if (risk.time_close_tp_pct != null) strategyRisk.time_close_tp_pct = Number(risk.time_close_tp_pct);
  if (risk.time_close_loss_enabled != null) strategyRisk.time_close_loss_enabled = !!risk.time_close_loss_enabled;
  if (risk.time_close_loss_minutes != null) strategyRisk.time_close_loss_minutes = Number(risk.time_close_loss_minutes);
  if (risk.time_close_loss_tp_pct != null) strategyRisk.time_close_loss_tp_pct = Number(risk.time_close_loss_tp_pct);
}

function syncTestSettingsFromPayload(data) {
  const ts = data?.test_settings;
  if (!ts) return;
  if (ts.max_open_trades != null) {
    const pin = limitPins.testMaxOpen;
    if (!(pin && Date.now() < pin.until)) {
      testSettings.max_open_trades = Number(ts.max_open_trades);
    } else {
      testSettings.max_open_trades = pin.value;
    }
  }
  if (ts.max_open_trades_per_strategy != null) {
    const pin = limitPins.testMaxPer;
    if (!(pin && Date.now() < pin.until)) {
      testSettings.max_open_trades_per_strategy = Number(ts.max_open_trades_per_strategy);
    } else {
      testSettings.max_open_trades_per_strategy = pin.value;
    }
  }
  if (ts.stake_amount != null) testSettings.stake_amount = Number(ts.stake_amount);
  if (ts.stoploss_pct != null) testSettings.stoploss_pct = Number(ts.stoploss_pct);
  if (ts.take_profit_pct != null) testSettings.take_profit_pct = Number(ts.take_profit_pct);
  if (ts.time_close_enabled != null) testSettings.time_close_enabled = !!ts.time_close_enabled;
  if (ts.time_close_minutes != null) testSettings.time_close_minutes = Number(ts.time_close_minutes);
  if (ts.time_close_tp_pct != null) testSettings.time_close_tp_pct = Number(ts.time_close_tp_pct);
  if (ts.time_close_loss_enabled != null) testSettings.time_close_loss_enabled = !!ts.time_close_loss_enabled;
  if (ts.time_close_loss_minutes != null) testSettings.time_close_loss_minutes = Number(ts.time_close_loss_minutes);
  if (ts.time_close_loss_tp_pct != null) testSettings.time_close_loss_tp_pct = Number(ts.time_close_loss_tp_pct);
  applyPinnedTestSettings();
  // Main and test pools are separate — never overwrite «Открыто (основные)» with test max.
  updateMaxTradesHint();
}

function strategyRiskSummaryText() {
  const sl = Number(strategyRisk.stoploss_pct);
  const tp = Number(strategyRisk.take_profit_pct);
  if (!Number.isFinite(sl) || !Number.isFinite(tp)) return "";
  const hedge = dualHedgeEnabled ? " · dual L+S" : "";
  const timeClose = strategyRisk.time_close_enabled
    ? ` · прибыль ${strategyRisk.time_close_minutes} мин (>${strategyRisk.time_close_tp_pct}%)`
    : "";
  const timeCloseLoss = strategyRisk.time_close_loss_enabled
    ? ` · убыток ${strategyRisk.time_close_loss_minutes} мин (>${strategyRisk.time_close_loss_tp_pct}%)`
    : "";
  return `SL −${sl}% · TP +${tp}%${hedge}${timeClose}${timeCloseLoss}`;
}

function setSettingsSummary(id, text) {
  const el = $(id);
  if (el) el.textContent = text || "развернуть";
}

function closedCoin(profit, panel) {
  if (!profit) return null;
  if (panel === "test" && profit.profit_closed_test_coin != null) return profit.profit_closed_test_coin;
  if (panel === "main" && profit.profit_closed_main_coin != null) return profit.profit_closed_main_coin;
  if (panel === "htf1h" && profit.profit_closed_htf_coin != null) return profit.profit_closed_htf_coin;
  return profit.profit_closed_coin ?? null;
}

function fmtClosedProfit(profit, panel) {
  const coin = closedCoin(profit, panel);
  if (coin != null && Number.isFinite(Number(coin))) return fmtUsd(coin);
  const fallback = profit?.profit_closed_percent;
  if (fallback == null || Number.isNaN(Number(fallback))) return "—";
  return fmtPct(fallback);
}

function updateBotSettingsSummaries() {
  const strat = BOTS.strategy;
  const risk = strategyRiskSummaryText();
  setSettingsSummary(
    "strategy-settings-summary",
    `макс ${strat.maxTrades} · stake ${strat.stakeAmount} USDT${risk ? ` · ${risk}` : ""} · развернуть`
  );
  const tSl = Number(testSettings.stoploss_pct);
  const tTp = Number(testSettings.take_profit_pct);
  const tRisk =
    Number.isFinite(tSl) && Number.isFinite(tTp) ? `SL −${tSl}% · TP +${tTp}%` : "";
  const tTimeClose = testSettings.time_close_enabled
    ? ` · прибыль ${testSettings.time_close_minutes} мин (>${testSettings.time_close_tp_pct}%)`
    : "";
  const tTimeCloseLoss = testSettings.time_close_loss_enabled
    ? ` · убыток ${testSettings.time_close_loss_minutes} мин (>${testSettings.time_close_loss_tp_pct}%)`
    : "";
  setSettingsSummary(
    "test-strategy-settings-summary",
    `макс ${testSettings.max_open_trades} · stake ${testSettings.stake_amount} USDT${tRisk ? ` · ${tRisk}` : ""}${tTimeClose}${tTimeCloseLoss} · развернуть`
  );
  const grid = BOTS.grid;
  setSettingsSummary(
    "grid-settings-summary",
    `боковиков ${grid.maxTrades} · stake ${grid.stakeAmount} USDT · SL −15% · развернуть`
  );
  setSettingsSummary(
    "bybitgrid-settings-summary",
    `лимит ${bybitGridMaxBots} · invest ${bybitGridInvest} USDT · развернуть`
  );
  const finder = BOTS.finder;
  setSettingsSummary(
    "finder-settings-summary",
    `макс ${finder.maxTrades} · развернуть`
  );
}

async function setDualHedge(enabled) {
  const data = await pairConfigApi("/pairs", "POST", {
    action: "set_dual_hedge",
    enabled: !!enabled,
  });
  syncEnabledFromPayload(data);
  updateStrategyDisplay();
  showReloadWarning(data);
  updateBotSettingsSummaries();
  return data;
}

function renderDualHedgeControl() {
  const hint = uiHintHtml(
    "Bybit hedge mode · 1 пара = 2 слота max_open_trades · ML gate на обе ноги · общий флаг для strategy-бота. На каждый сигнал сразу long + short (2 позиции, 2× stake · max_open_trades ≥ 4)."
  );
  const html = `
    <div class="strategy-dual-hedge-row">
      <label class="strategy-dual-hedge-label">
        <input type="checkbox" class="strategy-dual-hedge-cb" ${dualHedgeEnabled ? "checked" : ""} />
        <span class="strategy-dual-hedge-title-text"><strong>Dual hedge</strong></span>
      </label>
      ${hint}
    </div>
  `;
  for (const id of ["strategy-dual-hedge"]) {
    const el = $(id);
    if (!el) continue;
    el.innerHTML = html;
    const cb = el.querySelector(".strategy-dual-hedge-cb");
    if (!cb || cb.dataset.bound) continue;
    cb.dataset.bound = "1";
    cb.addEventListener("change", async () => {
      const next = cb.checked;
      document.querySelectorAll(".strategy-dual-hedge-cb").forEach((x) => {
        x.disabled = true;
      });
      try {
        if (
          next &&
          !confirm(
            "Включить dual hedge? На каждый сигнал сразу long + short. max_open_trades поднимется до 4, Bybit — hedge mode."
          )
        ) {
          document.querySelectorAll(".strategy-dual-hedge-cb").forEach((x) => {
            x.checked = false;
          });
          return;
        }
        const data = await setDualHedge(next);
        document.querySelectorAll(".strategy-dual-hedge-cb").forEach((x) => {
          x.checked = next;
        });
        if (data?.limits_note) showPairMsg(data.limits_note);
        await refreshAll();
      } catch (e) {
        document.querySelectorAll(".strategy-dual-hedge-cb").forEach((x) => {
          x.checked = !next;
        });
        if (e.message === "auth") logout();
        else showToast(formatApiError(e.message), "error");
      } finally {
        document.querySelectorAll(".strategy-dual-hedge-cb").forEach((x) => {
          x.disabled = false;
        });
      }
    });
  }
}

function isTestStrategy(s) {
  if (!s) return false;
  return isTestCatalogEntry(s);
}

function isHtfStrategy(s) {
  if (!s) return false;
  if (s.ui_panel) return s.ui_panel === "htf1h";
  return !!(s.htf_group || s.htfGroup);
}

function strategyUiPanel(s) {
  if (!s) return "hidden";
  if (s.ui_panel) return s.ui_panel;
  if (s.htf_group || s.htfGroup) return "htf1h";
  return isTestStrategy(s) ? "test" : "main";
}

function catalogByGroup(group = false) {
  const want =
    group === true || group === "test" ? "test" : group === "htf1h" ? "htf1h" : "main";
  return strategyCatalog.filter((s) => {
    if (!s) return false;
    if (want === "test") return isTestCatalogEntry(s) && !isHtfStrategy(s);
    if (want === "htf1h") return isHtfStrategy(s);
    // Main: only strategies explicitly copied into placement.main
    if (s.in_main != null) return !!s.in_main;
    if (s.show_on_main != null) return !!s.show_on_main;
    return strategyUiPanel(s) === "main" && !isBirthTestStrategy(s);
  });
}

function updateStrategyDisplay() {
  const el = $("strategy-active-name");
  const hint = $("strategy-enabled-hint");
  const panelSummary = $("strategy-panel-summary");
  const mainCatalog = catalogByGroup(false);
  const active = mainCatalog.filter((s) => enabledStrategies[s.id]);

  if (el) {
    el.classList.remove("warn");
    const riskNote = strategyRiskSummaryText();
    if (!active.length) {
      el.textContent = riskNote ? `Нет активных · ${riskNote}` : "Нет активных стратегий";
      el.classList.add("warn");
    } else if (active.length === 1) {
      el.textContent = riskNote
        ? `${strategyCatalogLabel(active[0])} · ${riskNote}`
        : strategyCatalogLabel(active[0]);
    } else {
      el.textContent = riskNote
        ? `${active.length} из ${mainCatalog.length} активны · ${riskNote}`
        : `${active.length} из ${mainCatalog.length} активны`;
    }
  }

  if (hint) {
    const riskNote = strategyRiskSummaryText();
    if (!active.length) {
      hint.textContent = "Включите хотя бы одну стратегию";
    } else {
      hint.textContent = `${active.length} из ${mainCatalog.length} включено · сигнал от любой из них${riskNote ? ` · ${riskNote}` : ""}`;
    }
  }

  if (panelSummary) {
    const riskNote = strategyRiskSummaryText();
    if (!active.length) {
      panelSummary.textContent = riskNote ? `${riskNote} · развернуть` : "нет активных · развернуть";
    } else if (active.length === 1) {
      panelSummary.textContent = `${strategyCatalogLabel(active[0])}${riskNote ? ` · ${riskNote}` : ""} · изменить`;
    } else {
      panelSummary.textContent = `${active.length} из ${mainCatalog.length} включено${riskNote ? ` · ${riskNote}` : ""} · изменить`;
    }
  }

  updateTestStrategyDisplay();
  updateHtfStrategyDisplay();
  renderStrategyRiskControls();
  renderTestStrategyRiskControls();
  renderDualHedgeControl();
}

function updateTestStrategyDisplay() {
  const el = $("test-strategy-active-name");
  const hint = $("test-strategy-enabled-hint");
  const panelSummary = $("test-strategy-panel-summary");
  const catalog = catalogByGroup(true);
  const active = catalog.filter((s) => enabledStrategies[s.id]);

  if (el) {
    if (!catalog.length) {
      el.textContent = "Нет тестовых в каталоге";
    } else if (!active.length) {
      el.textContent = "Все тестовые выключены";
    } else if (active.length === 1) {
      el.textContent = strategyCatalogLabel(active[0]);
    } else {
      el.textContent = `${active.length} из ${catalog.length} активны`;
    }
  }

  if (hint) {
    if (!active.length) {
      hint.textContent = "Тестовые стратегии выключены — на торговлю не влияют";
    } else {
      hint.textContent = `${active.length} из ${catalog.length} включено · тот же MultiStrategyRouter`;
    }
  }

  if (panelSummary) {
    if (!active.length) {
      panelSummary.textContent = "все выкл · развернуть";
    } else if (active.length === 1) {
      panelSummary.textContent = `${strategyCatalogLabel(active[0])} · изменить`;
    } else {
      panelSummary.textContent = `${active.length} из ${catalog.length} включено · изменить`;
    }
  }
}

function updateHtfStrategyDisplay() {
  const el = $("htf1h-active-name");
  const panelSummary = $("htf1h-panel-summary");
  const catalog = catalogByGroup("htf1h");
  const active = catalog.filter((s) => enabledStrategies[s.id]);
  if (el) {
    if (!catalog.length) {
      el.textContent = isAdminUser()
        ? "Нет тестовых 1-часовых"
        : "Нет в основных — добавьте в Настройках, меню «Перенос»";
    } else if (!active.length) el.textContent = "Все 1-часовые выключены";
    else if (active.length === 1) el.textContent = strategyCatalogLabel(active[0]);
    else el.textContent = `${active.length} из ${catalog.length} активны`;
  }
  if (panelSummary) {
    if (!active.length) panelSummary.textContent = "все выкл · развернуть";
    else if (active.length === 1) panelSummary.textContent = `${strategyCatalogLabel(active[0])} · изменить`;
    else panelSummary.textContent = `${active.length} из ${catalog.length} включено · изменить`;
  }
  renderHtfStakeControls();
}

const COLLAPSIBLE_STORAGE_KEY = "ct_ui_collapsible_v1";

function readCollapsiblePrefs() {
  try {
    const raw = localStorage.getItem(COLLAPSIBLE_STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function writeCollapsiblePref(sectionId, expanded) {
  if (!sectionId) return;
  const prefs = readCollapsiblePrefs();
  prefs[sectionId] = !!expanded;
  try {
    localStorage.setItem(COLLAPSIBLE_STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    /* quota / private mode */
  }
}

function setCollapsibleExpanded(section, expanded, { persist = true } = {}) {
  if (!section) return;
  section.classList.toggle("is-expanded", expanded);
  section.classList.toggle("is-collapsed", !expanded);
  const btn = section.querySelector("[data-collapsible-toggle]");
  if (btn) btn.setAttribute("aria-expanded", expanded ? "true" : "false");
  if (persist && section.id) writeCollapsiblePref(section.id, expanded);
}

function restoreCollapsibleSections() {
  const prefs = readCollapsiblePrefs();
  document.querySelectorAll(".collapsible-section[id]").forEach((section) => {
    if (!Object.prototype.hasOwnProperty.call(prefs, section.id)) return;
    setCollapsibleExpanded(section, !!prefs[section.id], { persist: false });
  });
}

function bindCollapsibleSections() {
  if (document.documentElement.dataset.collapsibleBound) {
    restoreCollapsibleSections();
    return;
  }
  document.documentElement.dataset.collapsibleBound = "1";

  const toggleCollapsible = (btn) => {
    const section = btn.closest(".collapsible-section");
    if (!section) return;
    const expanded = !section.classList.contains("is-expanded");
    setCollapsibleExpanded(section, expanded);
  };

  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-collapsible-toggle]");
    if (!btn) return;
    toggleCollapsible(btn);
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const btn = ev.target.closest("[data-collapsible-toggle]");
    if (!btn) return;
    ev.preventDefault();
    toggleCollapsible(btn);
  });

  restoreCollapsibleSections();
}

/** Fixed overlay for tips; on mobile section-header tips must be fixed too
 *  (absolute under the toggle gets buried by the next collapsible blocks). */
function positionUiHintBody(details) {
  if (!details || details.classList.contains("ui-hint-block") || !details.open) return;
  const body = details.querySelector(":scope > .ui-hint-body");
  const btn = details.querySelector(":scope > .ui-hint-btn");
  if (!body || !btn) return;

  const viewW = window.innerWidth || document.documentElement.clientWidth;
  const isCollapsibleTip = details.classList.contains("collapsible-title-hint");
  const isPanelTitleTip = !!details.closest(".panel-title-row, .panel-head");
  // Row tips (strategy name, ML bulk, etc.) stay CSS-absolute under the row.
  if (viewW <= 960 && !isCollapsibleTip && !isPanelTitleTip) {
    resetUiHintBody(details);
    return;
  }

  const pad = 10;
  const gap = 8;
  const viewH = window.innerHeight || document.documentElement.clientHeight;
  const btnRect = btn.getBoundingClientRect();
  const maxW =
    viewW <= 960
      ? Math.min(viewW - pad * 2, viewW - 24)
      : Math.min(18.5 * 16, viewW - pad * 2);

  body.style.position = "fixed";
  body.style.setProperty("position", "fixed", "important");
  body.style.right = "auto";
  body.style.bottom = "auto";
  body.style.transform = "none";
  /* Keep below sticky topbar (z-index 200) so tips never cover the menu */
  body.style.zIndex = "150";
  body.style.setProperty("z-index", "150", "important");
  body.style.width = `${Math.round(maxW)}px`;
  body.style.maxWidth = `${Math.round(viewW - pad * 2)}px`;
  body.style.maxHeight = "";
  body.style.overflowY = "";
  body.style.left = "0px";
  body.style.top = "0px";

  const h = body.offsetHeight || body.getBoundingClientRect().height;
  const w = body.offsetWidth || maxW;

  let left = btnRect.left;
  if (viewW <= 960) {
    // Prefer full usable width under the header on phones.
    left = pad;
  } else {
    if (left + w > viewW - pad) left = viewW - pad - w;
    if (left < pad) left = pad;
  }

  const topbarEl = document.getElementById("topbar");
  const topFloor = topbarEl
    ? Math.max(pad, Math.ceil(topbarEl.getBoundingClientRect().bottom) + 6)
    : pad;

  let top = btnRect.bottom + gap;
  const sideLeft = btnRect.right + gap;
  const canPlaceSide = viewW > 960 && isCollapsibleTip && sideLeft + w <= viewW - pad;

  if (canPlaceSide) {
    left = sideLeft;
    top = Math.max(topFloor, btnRect.top);
  } else {
    const spaceBelow = viewH - pad - (btnRect.bottom + gap);
    const spaceAbove = btnRect.top - gap - topFloor;
    if (h > spaceBelow && spaceAbove > spaceBelow) {
      top = btnRect.top - gap - h;
    }
  }
  if (top < topFloor) top = topFloor;
  if (top + h > viewH - pad) {
    const avail = Math.max(120, viewH - pad - top);
    body.style.maxHeight = `${Math.round(avail)}px`;
    body.style.overflowY = "auto";
  }

  body.style.setProperty("left", `${Math.round(left)}px`, "important");
  body.style.setProperty("top", `${Math.round(top)}px`, "important");
}

function resetUiHintBody(details) {
  const body = details?.querySelector?.(":scope > .ui-hint-body");
  if (!body) return;
  body.style.removeProperty("position");
  body.style.removeProperty("left");
  body.style.removeProperty("top");
  body.style.removeProperty("right");
  body.style.removeProperty("bottom");
  body.style.removeProperty("transform");
  body.style.removeProperty("width");
  body.style.removeProperty("max-width");
  body.style.removeProperty("max-height");
  body.style.removeProperty("overflow-y");
  body.style.removeProperty("z-index");
  body.style.position = "";
  body.style.left = "";
  body.style.top = "";
  body.style.right = "";
  body.style.bottom = "";
  body.style.transform = "";
  body.style.width = "";
  body.style.maxWidth = "";
  body.style.maxHeight = "";
  body.style.overflowY = "";
  body.style.zIndex = "";
}

function repositionOpenUiHints() {
  document.querySelectorAll("details.ui-hint[open]").forEach((d) => positionUiHintBody(d));
}

function bindUiHints() {
  if (document.body.dataset.uiHintsBound) return;
  document.body.dataset.uiHintsBound = "1";
  document.addEventListener(
    "toggle",
    (e) => {
      const details = e.target;
      if (!(details instanceof HTMLDetailsElement) || !details.classList.contains("ui-hint")) return;
      if (details.open) {
        document.querySelectorAll("details.ui-hint[open]").forEach((other) => {
          if (other !== details && !other.classList.contains("ui-hint-block")) {
            other.open = false;
            resetUiHintBody(other);
          }
        });
        requestAnimationFrame(() => positionUiHintBody(details));
      } else {
        resetUiHintBody(details);
      }
    },
    true
  );
  document.addEventListener(
    "pointerdown",
    (e) => {
      const t = e.target;
      if (!(t instanceof Element)) return;
      if (t.closest("details.ui-hint")) return;
      document.querySelectorAll("details.ui-hint[open]:not(.ui-hint-block)").forEach((d) => {
        d.open = false;
        resetUiHintBody(d);
      });
    },
    true
  );
  window.addEventListener("resize", repositionOpenUiHints, { passive: true });
  let hintScrollTimer = 0;
  window.addEventListener(
    "scroll",
    () => {
      if (hintScrollTimer) return;
      hintScrollTimer = window.setTimeout(() => {
        hintScrollTimer = 0;
        repositionOpenUiHints();
      }, 80);
    },
    { passive: true, capture: true }
  );
}

function whitelistSummaryText(pairs, bot) {
  if (bot !== "grid" && isAllVolumePairlist()) return "top-200 по объёму · развернуть";
  if (!pairs.length) return "пусто · развернуть";
  if (pairs.length <= 2) return `${pairs.join(", ")} · развернуть`;
  return `${pairs.length} пар · развернуть`;
}

function renderWhitelist(bot, whitelist) {
  // null/undefined = skip (keep last rendered pairs during refresh)
  if (whitelist == null) return;
  const pairs = whitelist?.whitelist || [];
  const el = $(BOTS[bot].pairsEl);
  if (el) el.textContent = pairs.join(", ") || "—";
  const summary = $(`${bot}-pairs-summary`);
  if (summary) summary.textContent = whitelistSummaryText(pairs, bot);
  if (bot === "strategy") {
    const testEl = $("test-strategy-pairs");
    if (testEl) testEl.textContent = pairs.join(", ") || "—";
    const testSummary = $("test-strategy-pairs-summary");
    if (testSummary) testSummary.textContent = whitelistSummaryText(pairs);
    const htfEl = $("htf1h-pairs");
    if (htfEl) htfEl.textContent = pairs.join(", ") || "—";
    const htfSummary = $("htf1h-pairs-summary");
    if (htfSummary) htfSummary.textContent = whitelistSummaryText(pairs);
  }
}

function trainedRiskHint(id) {
  const spec = trainedRiskSpecs[id];
  if (!spec) return "";
  const sl = Math.abs(Number(spec.stoploss) * 100);
  const tp = Number(spec.tp) * 100;
  if (!Number.isFinite(sl) || !Number.isFinite(tp)) return "";
  return `SL −${sl.toFixed(sl < 2 ? 1 : 0)}% · TP +${tp.toFixed(tp < 2 ? 1 : 0)}%`;
}

function mlConfidencePct(id) {
  const v = Number(mlConfidenceStrategies[id]);
  if (!Number.isFinite(v)) return 55;
  return Math.round(v * 100);
}

function mlConfidenceChoicesFor(id) {
  const cur = Number(mlConfidenceStrategies[id]);
  const def = Number(mlConfidenceDefaults[id]);
  const set = new Set(mlConfidenceChoices.map(Number));
  if (Number.isFinite(cur)) set.add(cur);
  if (Number.isFinite(def)) set.add(def);
  return [...set].sort((a, b) => a - b);
}

function bulkMlConfidenceChoices() {
  const set = new Set(mlConfidenceChoices.map(Number));
  for (const v of Object.values(mlConfidenceDefaults)) {
    if (Number.isFinite(Number(v))) set.add(Number(v));
  }
  return [...set].sort((a, b) => a - b);
}

async function setAllStrategiesMlConfidence(value, { reset = false, group = "main" } = {}) {
  const payload = reset
    ? { action: "set_all_strategies_ml_confidence", reset: true, group }
    : { action: "set_all_strategies_ml_confidence", ml_confidence: value, group };
  const data = await pairConfigApi("/pairs", "POST", payload);
  syncEnabledFromPayload(data);
  updateStrategyDisplay();
  renderStrategyToggles();
  renderBulkMlConfidenceControls();
  return data;
}

async function setAllStrategiesEnabled(enabled, { group = "main" } = {}) {
  const data = await pairConfigApi("/pairs", "POST", {
    action: "set_all_strategies_enabled",
    enabled: !!enabled,
    group,
  });
  syncEnabledFromPayload(data);
  updateStrategyDisplay();
  renderStrategyToggles();
  return data;
}

function renderBulkEnableControlsFor(ids, { group = "main", label = "Стратегии", hint = "" } = {}) {
  const catalog = catalogByGroup(group);
  const activeCount = catalog.filter((s) => enabledStrategies[s.id]).length;
  const total = catalog.length;
  const html = `
    <div class="strategy-enable-bulk-inner">
      <span class="strategy-enable-bulk-head">
        <span class="strategy-enable-bulk-label">${label}</span>
        ${hint ? uiHintHtml(hint) : ""}
      </span>
      <span class="muted strategy-enable-bulk-count">${activeCount} / ${total} вкл</span>
      <div class="strategy-enable-bulk-actions">
        <button type="button" class="btn btn-sm primary" data-strategy-enable-all>Включить все</button>
        <button type="button" class="btn btn-sm ghost" data-strategy-disable-all>Выключить все</button>
      </div>
    </div>
  `;
  for (const id of ids) {
    const el = $(id);
    if (!el) continue;
    el.innerHTML = html;
    const onBtn = el.querySelector("[data-strategy-enable-all]");
    const offBtn = el.querySelector("[data-strategy-disable-all]");
    if (onBtn && !onBtn.dataset.bound) {
      onBtn.dataset.bound = "1";
      onBtn.addEventListener("click", async () => {
        const confirmMsg =
          group === "test"
            ? "Включить все тестовые стратегии?"
            : group === "htf1h"
              ? "Включить все 1-часовые стратегии?"
              : "Включить все стратегии?";
        if (!confirm(confirmMsg)) return;
        onBtn.disabled = true;
        if (offBtn) offBtn.disabled = true;
        try {
          await setAllStrategiesEnabled(true, { group });
          const msg =
            group === "test"
              ? "Все тестовые стратегии включены"
              : group === "htf1h"
                ? "Все 1-часовые стратегии включены"
                : "Все стратегии включены";
          if (isSettingsOpen()) showPairMsg(msg);
        } catch (e) {
          if (e.message === "auth") logout();
          else showToast(formatApiError(e.message), "error");
        } finally {
          onBtn.disabled = false;
          if (offBtn) offBtn.disabled = false;
        }
      });
    }
    if (offBtn && !offBtn.dataset.bound) {
      offBtn.dataset.bound = "1";
      offBtn.addEventListener("click", async () => {
        const confirmMsg =
          group === "test"
            ? "Выключить все тестовые стратегии?"
            : group === "htf1h"
              ? "Выключить все 1-часовые стратегии?"
              : "Выключить все стратегии? Новые входы по основным остановятся.";
        if (!confirm(confirmMsg)) return;
        onBtn.disabled = true;
        offBtn.disabled = true;
        try {
          await setAllStrategiesEnabled(false, { group });
          const msg =
            group === "test"
              ? "Все тестовые стратегии выключены"
              : group === "htf1h"
                ? "Все 1-часовые стратегии выключены"
                : "Все стратегии выключены";
          if (isSettingsOpen()) showPairMsg(msg);
        } catch (e) {
          if (e.message === "auth") logout();
          else showToast(formatApiError(e.message), "error");
        } finally {
          onBtn.disabled = false;
          offBtn.disabled = false;
        }
      });
    }
  }
}

function renderBulkEnableControls() {
  renderBulkEnableControlsFor(["strategy-enable-bulk"], {
    group: "main",
    label: "Стратегии",
    hint:
      "Выключить все = пауза входов по основным · Включить все = pack без тестовых · без рестарта · деплой не затирает включения",
  });
  renderBulkEnableControlsFor(["test-strategy-enable-bulk"], {
    group: "test",
    label: "Тестовые",
    hint: "Только тестовый блок · общий strategy-бот · без рестарта",
  });
  renderBulkEnableControlsFor(["htf1h-enable-bulk"], {
    group: "htf1h",
    label: "1-часовые",
    hint: "Только часовой блок · общий strategy-бот · без рестарта",
  });
}

function renderBulkMlConfidenceControlsFor(ids, { group = "main" } = {}) {
  bindMlConfidenceMenusOnce();
  const catalog = catalogByGroup(group);
  const choices = bulkMlConfidenceChoices();
  const common = (() => {
    const vals = catalog.map((s) => Number(mlConfidenceStrategies[s.id])).filter(Number.isFinite);
    if (!vals.length) return null;
    const first = vals[0];
    return vals.every((v) => Math.abs(v - first) < 1e-9) ? first : null;
  })();
  const scopeLabel = group === "test" ? "тестовых" : "основных";
  const valueLabel = common != null ? `${Math.round(common * 100)}%` : "разные";
  const wrapKey = group === "test" ? "test" : "main";
  const options = choices
    .map((v) => {
      const p = Math.round(Number(v) * 100);
      const isCur = common != null && Math.abs(common - Number(v)) < 1e-9;
      return `<button type="button" class="strategy-ml-conf-option${isCur ? " is-selected" : ""}" data-ml-conf-bulk-option data-group="${wrapKey}" data-value="${v}" role="option" aria-selected="${isCur ? "true" : "false"}">${p}%</button>`;
    })
    .join("");
  const html = `
    <div class="strategy-ml-conf-bulk-inner">
      <div class="strategy-ml-conf-bulk-head">
        <span class="strategy-ml-conf-bulk-title">Уверенность ML для ${scopeLabel}</span>
        ${uiHintHtml(`Только ${scopeLabel} · без рестарта · «Стандарт» = дефолт из pack`)}
      </div>
      <div class="strategy-ml-conf strategy-ml-conf-bulk-picker" data-ml-conf-bulk-wrap="${wrapKey}">
        <button type="button" class="strategy-ml-conf-btn" data-ml-conf-bulk-btn="${wrapKey}" aria-haspopup="listbox" aria-expanded="false" title="Минимальная уверенность ML для всех ${scopeLabel}">
          <span class="strategy-ml-conf-label">Порог</span>
          <span class="strategy-ml-conf-value" data-ml-conf-bulk-value>${valueLabel}</span>
          <span class="strategy-ml-conf-caret" aria-hidden="true">▾</span>
        </button>
        <div class="strategy-ml-conf-menu" data-ml-conf-bulk-menu="${wrapKey}" hidden role="listbox" aria-label="Уверенность ML для ${scopeLabel}">
          ${options}
        </div>
      </div>
      <div class="strategy-ml-conf-bulk-actions">
        <button type="button" class="btn btn-sm ghost" data-ml-conf-bulk-reset title="Вернуть порог из обучения (pack)">Стандарт</button>
      </div>
    </div>
  `;
  for (const id of ids) {
    const el = $(id);
    if (!el) continue;
    el.innerHTML = html;
    const wrap = el.querySelector("[data-ml-conf-bulk-wrap]");
    const btn = el.querySelector("[data-ml-conf-bulk-btn]");
    const menu = el.querySelector("[data-ml-conf-bulk-menu]");
    const resetBtn = el.querySelector("[data-ml-conf-bulk-reset]");

    if (btn && menu && !btn.dataset.bound) {
      btn.dataset.bound = "1";
      btn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const willOpen = menu.hidden;
        closeAllMlConfidenceMenus(willOpen ? `__bulk_${wrapKey}` : null);
        menu.hidden = !willOpen;
        btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
        wrap?.classList.toggle("is-open", willOpen);
        if (willOpen) {
          requestAnimationFrame(() => positionMlConfidenceMenu(wrap, menu));
        } else {
          resetMlConfidenceMenuPosition(menu);
        }
      });
    }

    el.querySelectorAll("[data-ml-conf-bulk-option]").forEach((opt) => {
      if (opt.dataset.bound) return;
      opt.dataset.bound = "1";
      opt.addEventListener("click", async (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const value = Number(opt.getAttribute("data-value"));
        if (!Number.isFinite(value)) return;
        const pct = Math.round(value * 100);
        if (!confirm(`Поставить уверенность ML ${pct}% для ${scopeLabel}?`)) return;
        closeAllMlConfidenceMenus();
        if (btn) btn.disabled = true;
        if (resetBtn) resetBtn.disabled = true;
        try {
          await setAllStrategiesMlConfidence(value, { group });
          const msg = `Уверенность ML ${pct}% для ${scopeLabel}`;
          if (isSettingsOpen()) showPairMsg(msg);
        } catch (e) {
          if (e.message === "auth") logout();
          else showToast(formatApiError(e.message), "error");
        } finally {
          if (btn) btn.disabled = false;
          if (resetBtn) resetBtn.disabled = false;
        }
      });
    });

    if (resetBtn && !resetBtn.dataset.bound) {
      resetBtn.dataset.bound = "1";
      resetBtn.addEventListener("click", async () => {
        if (!confirm(`Сбросить уверенность ML к стандарту для ${scopeLabel}?`)) return;
        if (btn) btn.disabled = true;
        resetBtn.disabled = true;
        try {
          await setAllStrategiesMlConfidence(null, { reset: true, group });
          const msg = `Уверенность ML сброшена к стандарту (${scopeLabel})`;
          if (isSettingsOpen()) showPairMsg(msg);
        } catch (e) {
          if (e.message === "auth") logout();
          else showToast(formatApiError(e.message), "error");
        } finally {
          if (btn) btn.disabled = false;
          resetBtn.disabled = false;
        }
      });
    }
  }
}

function renderBulkMlConfidenceControls() {
  renderBulkMlConfidenceControlsFor(["strategy-ml-conf-bulk"], {
    group: "main",
  });
  renderBulkMlConfidenceControlsFor(["test-strategy-ml-conf-bulk"], {
    group: "test",
  });
}

function entrySideLabel(side) {
  if (side === "long") return "Лонг";
  if (side === "short") return "Шорт";
  return "Оба";
}

function renderEntrySideControl(id) {
  const current = entrySideStrategies[id] === "long" || entrySideStrategies[id] === "short"
    ? entrySideStrategies[id]
    : "both";
  const options = [
    ["both", "Оба"],
    ["long", "Лонг"],
    ["short", "Шорт"],
  ]
    .map(([value, label]) => {
      const isCur = value === current;
      return `<button type="button" class="strategy-ml-conf-option${isCur ? " is-selected" : ""}" data-strategy-entry-side-option="${id}" data-value="${value}" role="option" aria-selected="${isCur ? "true" : "false"}">${label}</button>`;
    })
    .join("");
  return `
    <div class="strategy-ml-conf strategy-entry-side" data-strategy-entry-side-wrap="${id}">
      <button type="button" class="strategy-ml-conf-btn" data-strategy-entry-side-btn="${id}" aria-haspopup="listbox" aria-expanded="false" title="Какие сделки открывать: лонг, шорт или оба направления. Считается уже после инверсии.">
        <span class="strategy-ml-conf-label">Направление</span>
        <span class="strategy-ml-conf-value" data-side="${current}">${entrySideLabel(current)}</span>
        <span class="strategy-ml-conf-caret" aria-hidden="true">▾</span>
      </button>
      <div class="strategy-ml-conf-menu" data-strategy-entry-side-menu="${id}" hidden role="listbox" aria-label="Направление">
        ${options}
      </div>
    </div>`;
}

function renderMlConfidenceControl(id) {
  const pct = mlConfidencePct(id);
  const defPct = Math.round((Number(mlConfidenceDefaults[id]) || 0.55) * 100);
  const options = mlConfidenceChoicesFor(id)
    .map((v) => {
      const p = Math.round(Number(v) * 100);
      const isCur = p === pct;
      const isDef = p === defPct;
      const label = isDef ? `${p}% (стандарт)` : `${p}%`;
      return `<button type="button" class="strategy-ml-conf-option${isCur ? " is-selected" : ""}" data-strategy-ml-confidence-option="${id}" data-value="${v}" role="option" aria-selected="${isCur ? "true" : "false"}">${label}</button>`;
    })
    .join("");
  return `
    <div class="strategy-ml-conf" data-strategy-ml-confidence-wrap="${id}">
      <button type="button" class="strategy-ml-conf-btn" data-strategy-ml-confidence-btn="${id}" aria-haspopup="listbox" aria-expanded="false" title="Минимальная уверенность ML (profit) для входа">
        <span class="strategy-ml-conf-label">Уверенность ML</span>
        <span class="strategy-ml-conf-value">${pct}%</span>
        <span class="strategy-ml-conf-caret" aria-hidden="true">▾</span>
      </button>
      <div class="strategy-ml-conf-menu" data-strategy-ml-confidence-menu="${id}" hidden role="listbox" aria-label="Уверенность ML">
        ${options}
      </div>
    </div>`;
}

function strategyPairsFor(id) {
  const list = strategyPairsMap[id];
  return Array.isArray(list) ? list : [];
}

function pairBaseLabel(pair) {
  const raw = String(pair || "");
  const base = raw.split("/")[0] || raw;
  return base.replace(/:USDT$/i, "");
}

function renderTestStrategyAllPairs() {
  return `
    <div class="strategy-tickers strategy-tickers-all">
      <div class="strategy-tickers-btn is-static" title="Тестовый блок смотрит все пары общего списка. Выбранные тикеры действуют только в «Основных».">
        <span class="strategy-tickers-label">Тикеры</span>
        <span class="strategy-tickers-value">все пары</span>
      </div>
    </div>`;
}

function renderStrategyTickersControl(id, { emptyMeansAll = false } = {}) {
  const pairs = strategyPairsFor(id);
  const n = pairs.length;
  const showAll = emptyMeansAll && n === 0;
  const chips = pairs.length
    ? pairs
        .map(
          (p) => `
      <li class="strategy-tickers-item">
        <span class="strategy-tickers-pair" title="${escapeHtml(p)}">${escapeHtml(pairBaseLabel(p))}</span>
        <button type="button" class="strategy-tickers-remove" data-strategy-ticker-remove="${id}" data-pair="${escapeHtml(p)}" title="Убрать ${escapeHtml(pairBaseLabel(p))}" ${settingsReadonly || demoMirrorAdmin ? "disabled" : ""}>×</button>
      </li>`
        )
        .join("")
    : showAll
      ? `<li class="strategy-tickers-empty muted">Пока пусто — торгуются все пары общего списка. Добавьте тикер, чтобы оставить только выбранные.</li>`
      : `<li class="strategy-tickers-empty muted">Нет тикеров — стратегия не торгует</li>`;
  const valueLabel = showAll ? "все" : String(n);
  const title = showAll
    ? "Тикеры этой стратегии. Пустой список — все пары общего белого списка."
    : "Тикеры этой стратегии. Пустой список — стратегия не открывает сделки.";
  return `
    <div class="strategy-tickers" data-strategy-tickers-wrap="${id}">
      <button type="button" class="strategy-tickers-btn" data-strategy-tickers-btn="${id}" aria-haspopup="dialog" aria-expanded="false" title="${title}">
        <span class="strategy-tickers-label">Тикеры</span>
        <span class="strategy-tickers-value">${valueLabel}</span>
        <span class="strategy-tickers-caret" aria-hidden="true">▾</span>
      </button>
      <div class="strategy-tickers-panel" data-strategy-tickers-panel="${id}" hidden>
        <div class="strategy-tickers-head">Тикеры · ${escapeHtml(strategyLabel(id))}</div>
        <ul class="strategy-tickers-list">${chips}</ul>
        <form class="strategy-tickers-add" data-strategy-ticker-add-form="${id}">
          <input type="text" class="strategy-tickers-input" data-strategy-ticker-input="${id}" placeholder="BTC или BTC/USDT:USDT" ${settingsReadonly || demoMirrorAdmin ? "disabled" : ""} autocomplete="off" />
          <button type="submit" class="btn btn-sm" ${settingsReadonly || demoMirrorAdmin ? "disabled" : ""}>Добавить</button>
        </form>
      </div>
    </div>`;
}

function resetStrategyTickersPanelPosition(panel) {
  if (!panel) return;
  panel.classList.remove("opens-up");
  panel.style.position = "";
  panel.style.left = "";
  panel.style.right = "";
  panel.style.top = "";
  panel.style.bottom = "";
  panel.style.width = "";
  panel.style.maxWidth = "";
  panel.style.maxHeight = "";
  panel.style.zIndex = "";
}

function positionStrategyTickersPanel(wrap, panel) {
  if (!wrap || !panel || panel.hidden) return;
  const btn = wrap.querySelector("[data-strategy-tickers-btn], .strategy-tickers-btn");
  const anchor = btn || wrap;
  // Leave the document flow before measuring. Otherwise the open list
  // inflates the wrap and the popup is pinned over the next panel.
  panel.style.position = "fixed";
  const rect = anchor.getBoundingClientRect();
  const pad = 8;
  const viewW = window.innerWidth || document.documentElement.clientWidth;
  const viewH = window.innerHeight || document.documentElement.clientHeight;
  const width = Math.min(Math.max(rect.width, 240), viewW - pad * 2);
  const maxH = Math.min(18 * 16, viewH * 0.5, 320);

  panel.style.right = "auto";
  panel.style.bottom = "auto";
  panel.style.zIndex = "230";
  panel.style.width = `${Math.round(width)}px`;
  panel.style.maxWidth = `${Math.round(viewW - pad * 2)}px`;
  panel.style.maxHeight = `${Math.round(maxH)}px`;
  panel.style.left = `${Math.round(Math.max(pad, Math.min(rect.left, viewW - pad - width)))}px`;
  panel.style.top = "0px";

  const panelH = Math.min(panel.scrollHeight || maxH, maxH);
  const spaceBelow = viewH - pad - rect.bottom;
  const spaceAbove = rect.top - pad;
  let top = rect.bottom + 4;
  panel.classList.remove("opens-up");
  if (panelH > spaceBelow && spaceAbove > spaceBelow) {
    top = rect.top - 4 - panelH;
    panel.classList.add("opens-up");
  }
  if (top < pad) top = pad;
  if (top + panelH > viewH - pad) {
    panel.style.maxHeight = `${Math.round(Math.max(140, viewH - pad - top))}px`;
  }
  panel.style.top = `${Math.round(top)}px`;
}

function closeAllStrategyTickersPanels(exceptId = null) {
  document.querySelectorAll("[data-strategy-tickers-wrap]").forEach((wrap) => {
    const id = wrap.getAttribute("data-strategy-tickers-wrap");
    if (exceptId && id === exceptId) return;
    const panel = wrap.querySelector("[data-strategy-tickers-panel]");
    const btn = wrap.querySelector("[data-strategy-tickers-btn]");
    if (panel) {
      panel.hidden = true;
      resetStrategyTickersPanelPosition(panel);
    }
    if (btn) btn.setAttribute("aria-expanded", "false");
    wrap.classList.remove("is-open");
    wrap.closest(".strategy-toggle-item")?.classList.remove("is-tickers-open");
  });
}

function parkPickerMenu(menu) {
  if (!menu || menu.dataset.pickerParked) return;
  menu._pickerHome = menu.parentElement;
  menu.dataset.pickerParked = "1";
  document.body.appendChild(menu);
}

function restorePickerMenu(menu) {
  if (!menu?.dataset.pickerParked) return;
  if (menu._pickerHome) menu._pickerHome.appendChild(menu);
  delete menu.dataset.pickerParked;
  menu._pickerHome = null;
}

function resetMlConfidenceMenuPosition(menu) {
  if (!menu) return;
  menu.classList.remove("opens-up");
  menu.style.position = "";
  menu.style.left = "";
  menu.style.right = "";
  menu.style.top = "";
  menu.style.bottom = "";
  menu.style.width = "";
  menu.style.maxWidth = "";
  menu.style.maxHeight = "";
  menu.style.zIndex = "";
  restorePickerMenu(menu);
  menu.closest(".strategy-toggle-item")?.classList.remove("is-ml-conf-open");
}

function positionMlConfidenceMenu(wrap, menu) {
  if (!wrap || !menu || menu.hidden) return;
  const btn = wrap.querySelector(
    "[data-strategy-ml-confidence-btn], [data-ml-conf-bulk-btn], [data-strategy-sltp-btn], [data-strategy-entry-side-btn]"
  );
  const anchor = btn || wrap;
  const rect = anchor.getBoundingClientRect();
  const pad = 8;
  const viewW = window.innerWidth || document.documentElement.clientWidth;
  const viewH = window.innerHeight || document.documentElement.clientHeight;
  const width = Math.max(112, Math.min(Math.max(rect.width, 112), viewW - pad * 2));

  menu.style.position = "fixed";
  menu.style.right = "auto";
  menu.style.bottom = "auto";
  menu.style.zIndex = "400";
  menu.style.width = `${Math.round(width)}px`;
  menu.style.maxWidth = `${Math.round(viewW - pad * 2)}px`;
  menu.style.maxHeight = "none";
  menu.style.left = `${Math.round(Math.max(pad, Math.min(rect.left, viewW - pad - width)))}px`;
  menu.style.top = "0px";

  parkPickerMenu(menu);
  const contentH = menu.scrollHeight || 0;
  const spaceBelow = Math.max(0, viewH - rect.bottom - pad - 4);
  const spaceAbove = Math.max(0, rect.top - pad - 4);
  const want = Math.min(contentH || 320, 360);
  const openUp = spaceBelow < want && spaceAbove >= spaceBelow;
  const room = openUp ? spaceAbove : spaceBelow;
  const maxH = Math.min(want, Math.max(room, 96));
  menu.classList.toggle("opens-up", openUp);
  menu.style.maxHeight = `${Math.round(maxH)}px`;
  const shown = Math.min(contentH || maxH, maxH);
  let top = openUp ? rect.top - 4 - shown : rect.bottom + 4;
  if (top < pad) top = pad;
  if (top + shown > viewH - pad) top = Math.max(pad, viewH - pad - shown);
  menu.style.top = `${Math.round(top)}px`;
  const selected = menu.querySelector(".is-selected");
  if (selected) {
    const topEdge = selected.offsetTop;
    const bottomEdge = topEdge + selected.offsetHeight;
    if (topEdge < menu.scrollTop) menu.scrollTop = topEdge;
    else if (bottomEdge > menu.scrollTop + menu.clientHeight) {
      menu.scrollTop = bottomEdge - menu.clientHeight;
    }
  }
}

function pickerMenu(wrap, wrapAttr, menuAttr) {
  const id = wrap.getAttribute(wrapAttr);
  const local = wrap.querySelector(`[${menuAttr}]`);
  if (local) return local;
  if (!id || typeof CSS === "undefined" || !CSS.escape) return null;
  return document.querySelector(`[${menuAttr}="${CSS.escape(id)}"]`);
}

function closeAllMlConfidenceMenus(exceptId) {
  document.querySelectorAll("[data-strategy-ml-confidence-wrap]").forEach((wrap) => {
    const id = wrap.getAttribute("data-strategy-ml-confidence-wrap");
    if (exceptId && id === exceptId) return;
    const menu = pickerMenu(wrap, "data-strategy-ml-confidence-wrap", "data-strategy-ml-confidence-menu");
    const btn = wrap.querySelector("[data-strategy-ml-confidence-btn]");
    if (menu) {
      menu.hidden = true;
      resetMlConfidenceMenuPosition(menu);
    }
    if (btn) btn.setAttribute("aria-expanded", "false");
    wrap.classList.remove("is-open");
  });
  document.querySelectorAll("[data-ml-conf-bulk-wrap]").forEach((wrap) => {
    const key = wrap.getAttribute("data-ml-conf-bulk-wrap");
    const exceptKey = exceptId && String(exceptId).startsWith("__bulk_")
      ? String(exceptId).slice("__bulk_".length)
      : null;
    if (exceptKey && key === exceptKey) return;
    const menu = pickerMenu(wrap, "data-ml-conf-bulk-wrap", "data-ml-conf-bulk-menu");
    const btn = wrap.querySelector("[data-ml-conf-bulk-btn]");
    if (menu) {
      menu.hidden = true;
      resetMlConfidenceMenuPosition(menu);
    }
    if (btn) btn.setAttribute("aria-expanded", "false");
    wrap.classList.remove("is-open");
  });
  document.querySelectorAll("[data-strategy-entry-side-wrap]").forEach((wrap) => {
    const id = wrap.getAttribute("data-strategy-entry-side-wrap");
    if (exceptId && exceptId === `entry:${id}`) return;
    const menu = pickerMenu(wrap, "data-strategy-entry-side-wrap", "data-strategy-entry-side-menu");
    const btn = wrap.querySelector("[data-strategy-entry-side-btn]");
    if (menu) {
      menu.hidden = true;
      resetMlConfidenceMenuPosition(menu);
    }
    if (btn) btn.setAttribute("aria-expanded", "false");
    wrap.classList.remove("is-open");
  });
  document.querySelectorAll("[data-strategy-sltp-wrap]").forEach((wrap) => {
    const id = wrap.getAttribute("data-strategy-sltp-wrap");
    if (exceptId && id === exceptId) return;
    const menu = pickerMenu(wrap, "data-strategy-sltp-wrap", "data-strategy-sltp-menu");
    const btn = wrap.querySelector("[data-strategy-sltp-btn]");
    if (menu) {
      menu.hidden = true;
      resetMlConfidenceMenuPosition(menu);
    }
    if (btn) btn.setAttribute("aria-expanded", "false");
    wrap.classList.remove("is-open");
  });
}

function bindMlConfidenceMenusOnce() {
  if (document.documentElement.dataset.mlConfBound) return;
  document.documentElement.dataset.mlConfBound = "1";
  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (!(t instanceof Element)) return;
    if (t.closest("[data-strategy-ml-confidence-wrap], [data-ml-conf-bulk-wrap], [data-strategy-sltp-wrap], [data-strategy-entry-side-wrap], .strategy-ml-conf-menu")) return;
    closeAllMlConfidenceMenus();
    if (t.closest("[data-strategy-tickers-wrap], [data-llm-tickers-wrap]")) return;
    closeAllStrategyTickersPanels();
    closeLlmTickersPanel();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      closeAllMlConfidenceMenus();
      closeAllStrategyTickersPanels();
      closeLlmTickersPanel();
    }
  });
}

function fmtSltpPct(n) {
  const v = Math.round(Number(n) * 10) / 10;
  if (!Number.isFinite(v)) return "—";
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}

function strategySltpEffective(id, key) {
  const stored = htfSltp[id];
  if (stored && stored[key] != null && Number.isFinite(Number(stored[key]))) {
    return { value: Number(stored[key]), source: "custom" };
  }
  const def = htfSltpDefaults[id] && htfSltpDefaults[id][key];
  const n = Number(def);
  return {
    value: Number.isFinite(n) ? n : key === "sl" ? 5 : 10,
    source: isHtfStrategyId(id) ? "htf" : "block",
  };
}

function htfSltpValue(id, key) {
  return strategySltpEffective(id, key).value;
}

function renderStrategySltpControl(id) {
  const picker = (key, label) => {
    const shown = strategySltpEffective(id, key);
    const cur = shown.value;
    const def = Number(htfSltpDefaults[id] && htfSltpDefaults[id][key]);
    const base = key === "tp" ? sltpTpChoices : sltpSlChoices;
    const values = [...new Set(base.map(Number).concat(Number.isFinite(cur) ? [cur] : []))].sort(
      (a, b) => a - b
    );
    const options = values
      .map((v) => {
        const n = Number(v);
        const isCur = Math.abs(n - cur) < 0.05;
        const isDef = Number.isFinite(def) && Math.abs(n - def) < 0.05;
        const mark = isCur && shown.source === "trained" ? " (обучение)" : isDef ? " (стандарт)" : "";
        return `<button type="button" class="strategy-ml-conf-option${isCur ? " is-selected" : ""}" data-strategy-sltp-option="${id}" data-sltp-key="${key}" data-value="${n}" role="option" aria-selected="${isCur ? "true" : "false"}">${fmtSltpPct(n)}%${mark}</button>`;
      })
      .join("");
    return `
      <div class="strategy-ml-conf" data-strategy-sltp-wrap="${id}-${key}">
        <button type="button" class="strategy-ml-conf-btn" data-strategy-sltp-btn="${id}-${key}" aria-haspopup="listbox" aria-expanded="false" title="${label} этой стратегии. Свой выбор важнее общих SL/TP и обучения.">
          <span class="strategy-ml-conf-label">${label}</span>
          <span class="strategy-ml-conf-value">${fmtSltpPct(cur)}%</span>
          <span class="strategy-ml-conf-caret" aria-hidden="true">▾</span>
        </button>
        <div class="strategy-ml-conf-menu" data-strategy-sltp-menu="${id}-${key}" hidden role="listbox" aria-label="${label}">
          ${options}
        </div>
      </div>`;
  };
  return `<div class="strategy-htf-sltp">${picker("sl", "Стоп")}${picker("tp", "Тейк")}</div>`;
}

function renderStrategyTogglesInto(container, { testGroup = false, group = "" } = {}) {
  if (!container) return;
  bindMlConfidenceMenusOnce();
  const panelGroup = group || (testGroup ? "test" : "main");
  const catalog = catalogByGroup(panelGroup);
  if (!catalog.length) {
    const empty =
      panelGroup === "htf1h"
        ? isAdminUser()
          ? "Нет тестовых 1-часовых"
          : "Нет 1-часовых в основных — добавьте в Настройках, меню «Перенос»"
        : panelGroup === "test"
          ? "Нет тестовых стратегий в каталоге"
          : "Нет стратегий в Основных — добавьте через «Перенос» выше (копия, в Тестовых останутся)";
    container.innerHTML = `<li class="strategy-toggle-item strategy-panel-empty"><span class="muted">${empty}</span></li>`;
    return;
  }
  container.innerHTML = catalog
    .map((s) => {
      const trainOn = !!trainedRiskStrategies[s.id];
      const trainRow = `
      <label class="strategy-invert-switch strategy-trained-risk-switch${trainOn ? " is-on" : ""}" title="Включено — стоп и тейк, выбранные на этой строке. Выключено — общий SL/TP.">
        <span class="strategy-invert-text">Свой SL/TP</span>
        <input type="checkbox" data-strategy-trained-risk="${s.id}" ${trainOn ? "checked" : ""} />
        <span class="strategy-invert-slider" aria-hidden="true"></span>
      </label>`;
      const descHint = s.desc ? uiHintHtml(escapeHtml(s.desc)) : "";
      return `
    <li class="strategy-toggle-item${invertedStrategies[s.id] ? " is-inverted" : ""}${trainOn ? " is-trained-risk" : ""}">
      <div class="strategy-toggle-main">
        <label class="strategy-toggle-label">
          <input type="checkbox" data-strategy="${s.id}" ${enabledStrategies[s.id] ? "checked" : ""} ${settingsReadonly || demoMirrorAdmin ? "disabled" : ""} />
          <span class="strategy-toggle-text">
            <span class="strategy-toggle-title-row">
              <strong>${strategyCatalogLabel(s)}${invertedStrategies[s.id] ? ' <span class="strategy-inv-badge">инв</span>' : ""}${entrySideStrategies[s.id] === "long" ? ' <span class="strategy-side-badge is-long">лонг</span>' : entrySideStrategies[s.id] === "short" ? ' <span class="strategy-side-badge is-short">шорт</span>' : ""} <span class="strategy-ml-badge">ML ${mlConfidencePct(s.id)}%</span></strong>
              ${descHint}
            </span>
          </span>
        </label>
      </div>
      <label class="strategy-invert-switch" title="Инвертировать: long↔short">
        <span class="strategy-invert-text">Инвертировать</span>
        <input type="checkbox" data-strategy-invert="${s.id}" ${invertedStrategies[s.id] ? "checked" : ""} />
        <span class="strategy-invert-slider" aria-hidden="true"></span>
      </label>
      ${trainRow}
      ${group === "htf1h"
        ? renderStrategyTickersControl(s.id, { emptyMeansAll: !isAdminUser() || !s.in_main })
        : testGroup
          ? renderTestStrategyAllPairs()
          : renderStrategyTickersControl(s.id, { emptyMeansAll: !isAdminUser() })}
      ${renderStrategySltpControl(s.id)}
      ${renderEntrySideControl(s.id)}
      ${renderMlConfidenceControl(s.id)}
    </li>`;
    })
    .join("");

  // Hint is inside the enable <label>; stop label activation without blocking <details>.
  container.querySelectorAll(".strategy-toggle-title-row .ui-hint").forEach((hint) => {
    hint.addEventListener("click", (ev) => ev.stopPropagation());
    hint.addEventListener("mousedown", (ev) => ev.preventDefault());
  });

  container.querySelectorAll("[data-strategy-tickers-btn]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const id = btn.getAttribute("data-strategy-tickers-btn");
      const wrap = btn.closest("[data-strategy-tickers-wrap]");
      const panel = wrap?.querySelector("[data-strategy-tickers-panel]");
      const card = wrap?.closest(".strategy-toggle-item");
      if (!panel) return;
      const willOpen = panel.hidden;
      closeAllStrategyTickersPanels(willOpen ? id : null);
      closeLlmTickersPanel();
      closeAllMlConfidenceMenus();
      panel.hidden = !willOpen;
      btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
      wrap?.classList.toggle("is-open", willOpen);
      card?.classList.toggle("is-tickers-open", willOpen);
      if (willOpen) {
        requestAnimationFrame(() => {
          positionStrategyTickersPanel(wrap, panel);
          const input = wrap.querySelector("[data-strategy-ticker-input]");
          if (input && !input.disabled) input.focus();
        });
      } else {
        resetStrategyTickersPanelPosition(panel);
      }
    });
  });

  container.querySelectorAll("[data-strategy-ticker-remove]").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (settingsReadonly || demoMirrorAdmin) return;
      const id = btn.getAttribute("data-strategy-ticker-remove");
      const pair = btn.getAttribute("data-pair");
      if (!id || !pair) return;
      btn.disabled = true;
      try {
        const data = await pairConfigApi("/pairs", "POST", {
          action: "remove_strategy_pair",
          strategy: id,
          pair,
        });
        syncEnabledFromPayload(data);
        updateStrategyDisplay();
        renderStrategyToggles();
        const msg = `${strategyLabel(id)}: убран ${pairBaseLabel(pair)}`;
        if (isSettingsOpen()) showPairMsg(msg);
      } catch (e) {
        if (e.message === "auth") logout();
        else showToast(formatApiError(e.message), "error");
      } finally {
        btn.disabled = false;
      }
    });
  });

  container.querySelectorAll("[data-strategy-ticker-add-form]").forEach((form) => {
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (settingsReadonly || demoMirrorAdmin) return;
      const id = form.getAttribute("data-strategy-ticker-add-form");
      const input = form.querySelector("[data-strategy-ticker-input]");
      const raw = String(input?.value || "").trim();
      if (!id || !raw) return;
      const submitBtn = form.querySelector("button[type=submit]");
      if (submitBtn) submitBtn.disabled = true;
      if (input) input.disabled = true;
      try {
        const data = await pairConfigApi("/pairs", "POST", {
          action: "add_strategy_pair",
          strategy: id,
          pair: raw,
        });
        syncEnabledFromPayload(data);
        updateStrategyDisplay();
        renderStrategyToggles();
        const msg = `${strategyLabel(id)}: добавлен ${pairBaseLabel(raw)}`;
        if (isSettingsOpen()) showPairMsg(msg);
      } catch (e) {
        if (e.message === "auth") logout();
        else showToast(formatApiError(e.message), "error");
      } finally {
        if (submitBtn) submitBtn.disabled = false;
        if (input) input.disabled = false;
      }
    });
  });

  container.querySelectorAll('input[data-strategy]').forEach((cb) => {
    cb.addEventListener("change", async () => {
      const id = cb.dataset.strategy;
      const next = cb.checked;
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
        if (isSettingsOpen()) showPairMsg(msg);
        await refreshAll();
      } catch (e) {
        document.querySelectorAll(`input[data-strategy="${id}"]`).forEach((el) => {
          el.checked = !next;
        });
        if (e.message === "auth") logout();
        else showToast(formatApiError(e.message), "error");
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
        if (isSettingsOpen()) showPairMsg(msg);
      } catch (e) {
        document.querySelectorAll(`input[data-strategy-invert="${id}"]`).forEach((el) => {
          el.checked = !next;
        });
        if (e.message === "auth") logout();
        else showToast(formatApiError(e.message), "error");
      } finally {
        document.querySelectorAll(`input[data-strategy-invert="${id}"]`).forEach((el) => {
          el.disabled = false;
        });
      }
    });
  });

  container.querySelectorAll("input[data-strategy-trained-risk]").forEach((cb) => {
    cb.addEventListener("change", async () => {
      const id = cb.dataset.strategyTrainedRisk;
      const next = cb.checked;
      document.querySelectorAll(`input[data-strategy-trained-risk="${id}"]`).forEach((el) => {
        el.disabled = true;
      });
      try {
        const data = await pairConfigApi("/pairs", "POST", {
          action: "toggle_strategy_trained_risk",
          strategy: id,
          trained_risk: next,
        });
        syncEnabledFromPayload(data);
        updateStrategyDisplay();
        renderStrategyToggles();
        const msg = `${strategyLabel(id)}: свой SL/TP ${next ? "вкл" : "выкл, берётся общий"}`;
        if (isSettingsOpen()) showPairMsg(msg);
      } catch (e) {
        document.querySelectorAll(`input[data-strategy-trained-risk="${id}"]`).forEach((el) => {
          el.checked = !next;
        });
        if (e.message === "auth") logout();
        else showToast(formatApiError(e.message), "error");
      } finally {
        document.querySelectorAll(`input[data-strategy-trained-risk="${id}"]`).forEach((el) => {
          el.disabled = false;
        });
      }
    });
  });

  container.querySelectorAll("[data-strategy-entry-side-btn]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const id = btn.getAttribute("data-strategy-entry-side-btn");
      const wrap = btn.closest("[data-strategy-entry-side-wrap]");
      const menu = wrap?.querySelector("[data-strategy-entry-side-menu]");
      if (!menu) return;
      const willOpen = menu.hidden;
      closeAllMlConfidenceMenus(willOpen ? `entry:${id}` : null);
      closeAllStrategyTickersPanels();
      menu.hidden = !willOpen;
      btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
      wrap?.classList.toggle("is-open", willOpen);
      if (willOpen) {
        wrap?.closest(".strategy-toggle-item")?.classList.add("is-ml-conf-open");
        requestAnimationFrame(() => positionMlConfidenceMenu(wrap, menu));
      } else {
        resetMlConfidenceMenuPosition(menu);
      }
    });
  });

  container.querySelectorAll("[data-strategy-entry-side-option]").forEach((opt) => {
    opt.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const id = opt.getAttribute("data-strategy-entry-side-option");
      const value = opt.getAttribute("data-value");
      if (!id || (value !== "both" && value !== "long" && value !== "short")) return;
      const prev = entrySideStrategies[id] || "both";
      entrySideStrategies[id] = value;
      closeAllMlConfidenceMenus();
      document.querySelectorAll(`[data-strategy-entry-side-btn="${id}"]`).forEach((el) => {
        el.disabled = true;
      });
      try {
        const data = await pairConfigApi("/pairs", "POST", {
          action: "set_strategy_entry_side",
          strategy: id,
          entry_side: value,
        });
        syncEnabledFromPayload(data);
        updateStrategyDisplay();
        renderStrategyToggles();
        const msg = `${strategyLabel(id)}: направление ${entrySideLabel(value).toLowerCase()}`;
        if (isSettingsOpen()) showPairMsg(msg);
      } catch (e) {
        entrySideStrategies[id] = prev;
        if (e.message === "auth") logout();
        else showToast(formatApiError(e.message), "error");
      } finally {
        document.querySelectorAll(`[data-strategy-entry-side-btn="${id}"]`).forEach((el) => {
          el.disabled = false;
        });
      }
    });
  });

  container.querySelectorAll("[data-strategy-ml-confidence-btn]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const id = btn.getAttribute("data-strategy-ml-confidence-btn");
      const wrap = btn.closest("[data-strategy-ml-confidence-wrap]");
      const menu = wrap?.querySelector("[data-strategy-ml-confidence-menu]");
      if (!menu) return;
      const willOpen = menu.hidden;
      closeAllMlConfidenceMenus(willOpen ? id : null);
      closeAllStrategyTickersPanels();
      menu.hidden = !willOpen;
      btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
      wrap?.classList.toggle("is-open", willOpen);
      if (willOpen) {
        wrap?.closest(".strategy-toggle-item")?.classList.add("is-ml-conf-open");
        // Next frame so layout is applied before measuring.
        requestAnimationFrame(() => positionMlConfidenceMenu(wrap, menu));
      } else {
        resetMlConfidenceMenuPosition(menu);
      }
    });
  });

  container.querySelectorAll("[data-strategy-ml-confidence-option]").forEach((opt) => {
    opt.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const id = opt.getAttribute("data-strategy-ml-confidence-option");
      const value = Number(opt.getAttribute("data-value"));
      if (!id || !Number.isFinite(value)) return;
      const prev = mlConfidenceStrategies[id];
      closeAllMlConfidenceMenus();
      document.querySelectorAll(`[data-strategy-ml-confidence-btn="${id}"]`).forEach((el) => {
        el.disabled = true;
      });
      try {
        const data = await pairConfigApi("/pairs", "POST", {
          action: "set_strategy_ml_confidence",
          strategy: id,
          ml_confidence: value,
        });
        syncEnabledFromPayload(data);
        updateStrategyDisplay();
        renderStrategyToggles();
        const msg = `${strategyLabel(id)}: уверенность ML ${Math.round(value * 100)}%`;
        if (isSettingsOpen()) showPairMsg(msg);
      } catch (e) {
        mlConfidenceStrategies[id] = prev;
        if (e.message === "auth") logout();
        else showToast(formatApiError(e.message), "error");
      }       finally {
        document.querySelectorAll(`[data-strategy-ml-confidence-btn="${id}"]`).forEach((el) => {
          el.disabled = false;
        });
      }
    });
  });

  container.querySelectorAll("[data-strategy-sltp-btn]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const key = btn.getAttribute("data-strategy-sltp-btn");
      const wrap = btn.closest("[data-strategy-sltp-wrap]");
      const menu = wrap?.querySelector("[data-strategy-sltp-menu]");
      if (!menu) return;
      const willOpen = menu.hidden;
      closeAllMlConfidenceMenus(willOpen ? key : null);
      closeAllStrategyTickersPanels();
      menu.hidden = !willOpen;
      btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
      wrap?.classList.toggle("is-open", willOpen);
      if (willOpen) requestAnimationFrame(() => positionMlConfidenceMenu(wrap, menu));
      else resetMlConfidenceMenuPosition(menu);
    });
  });

  container.querySelectorAll("[data-strategy-sltp-option]").forEach((opt) => {
    opt.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const id = opt.getAttribute("data-strategy-sltp-option");
      const key = opt.getAttribute("data-sltp-key");
      const value = Number(opt.getAttribute("data-value"));
      if (!id || (key !== "sl" && key !== "tp") || !Number.isFinite(value)) return;
      const prev = htfSltp[id] ? { ...htfSltp[id] } : null;
      const next = {
        sl: key === "sl" ? value : htfSltpValue(id, "sl"),
        tp: key === "tp" ? value : htfSltpValue(id, "tp"),
      };
      htfSltp[id] = next;
      closeAllMlConfidenceMenus();
      try {
        const data = await pairConfigApi("/pairs", "POST", {
          action: "set_strategy_sltp",
          strategy: id,
          sl: next.sl,
          tp: next.tp,
        });
        syncEnabledFromPayload(data);
        updateStrategyDisplay();
        renderStrategyToggles();
        if (isSettingsOpen()) showPairMsg(`${strategyLabel(id)}: стоп −${next.sl}% · тейк +${next.tp}%`);
      } catch (e) {
        if (prev) htfSltp[id] = prev;
        else delete htfSltp[id];
        if (e.message === "auth") logout();
        else showToast(formatApiError(e.message), "error");
      }
    });
  });
}

function renderStrategyToggles() {
  renderBulkEnableControls();
  renderBulkMlConfidenceControls();
  renderStrategyTogglesInto($("strategy-panel-toggles"), { testGroup: false });
  renderStrategyTogglesInto($("test-strategy-panel-toggles"), { testGroup: true });
  renderStrategyTogglesInto($("htf1h-panel-toggles"), { group: "htf1h" });
}

async function refreshStrategyEnabled() {
  try {
    const data = await pairConfigApi("/strategies");
    syncEnabledFromPayload(data);
    updateStrategyDisplay();
    renderStrategyToggles();
  } catch (e) {
    if (e.message === "auth") logout();
    else console.warn("refreshStrategyEnabled:", e.message);
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

async function setStrategyTimeClose(patch) {
  const data = await pairConfigApi("/pairs", "POST", {
    action: "set_strategy_time_close",
    ...patch,
  });
  syncStrategyRiskFromPayload(data);
  updateStrategyDisplay();
  return data;
}

function renderStrategyRiskInputHtml() {
  const sl = Number(strategyRisk.stoploss_pct);
  const tp = Number(strategyRisk.take_profit_pct);
  const hint = uiHintHtml(
    `Общий SL/TP. Используется, когда у стратегии выключен «Свой SL/TP». Диапазон: SL ${STRATEGY_SL_MIN}–${STRATEGY_SL_MAX}% · TP ${STRATEGY_TP_MIN}–${STRATEGY_TP_MAX}%.`
  );
  return `
    <div class="strategy-risk-bar-inner">
      <span class="strategy-risk-title">Стоп / тейк ${hint}</span>
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
    </div>
  `;
}

function renderStrategyRiskControls() {
  const targets = ["strategy-risk-panel"];
  for (const id of targets) {
    const el = $(id);
    if (!el) continue;
    el.innerHTML = renderStrategyRiskInputHtml();
    bindStrategyRiskInput(el.querySelector("[data-strategy-risk-input]"));
  }
  const tcEl = $("strategy-time-close-panel");
  const locked = settingsReadonly || demoMirrorAdmin;
  const sig = [
    strategyRisk.time_close_enabled,
    strategyRisk.time_close_minutes,
    strategyRisk.time_close_tp_pct,
    strategyRisk.time_close_loss_enabled,
    strategyRisk.time_close_loss_minutes,
    strategyRisk.time_close_loss_tp_pct,
    locked,
  ].join("|");
  if (tcEl && tcEl.dataset.settingsSig !== sig && !tcEl.contains(document.activeElement)) {
    tcEl.innerHTML = renderStrategyTimeCloseHtml();
    tcEl.dataset.settingsSig = sig;
    bindStrategyTimeClose(tcEl);
    tcEl.querySelectorAll("input, button").forEach((el) => { el.disabled = locked; });
  }
  updateBotSettingsSummaries();
}

function renderTimeCloseLegHtml({
  kind,
  title,
  enabled,
  minutes,
  threshold,
  thresholdLabel,
  ariaThreshold,
  prefix,
}) {
  return `
    <div class="numeric-setting strategy-risk-setting strategy-time-close-leg" data-${prefix}-time-close-${kind}>
      <span class="strategy-time-close-leg-title">${title}</span>
      <label class="strategy-dual-hedge-label" title="${title}: включить">
        <input type="checkbox" class="${prefix}-time-close-${kind}-cb" ${enabled ? "checked" : ""} />
        <span class="strategy-dual-hedge-title-text"><strong>Вкл</strong></span>
      </label>
      <label class="strategy-risk-field">
        <span class="muted">Через</span>
        <input type="number" class="numeric-setting-input ${prefix}-time-close-${kind}-minutes" min="${TEST_TIME_CLOSE_MIN_MIN}" max="${TEST_TIME_CLOSE_MIN_MAX}" step="1" value="${minutes}" inputmode="numeric" aria-label="${title}: минуты" />
        <span class="numeric-setting-suffix">мин</span>
      </label>
      <label class="strategy-risk-field">
        <span class="muted">${thresholdLabel} &gt;</span>
        <input type="number" class="numeric-setting-input ${prefix}-time-close-${kind}-tp" min="${TEST_TP_MIN}" max="${TEST_TP_MAX}" step="0.1" value="${threshold}" inputmode="decimal" aria-label="${ariaThreshold}" />
        <span class="numeric-setting-suffix">%</span>
      </label>
      <button type="button" class="btn btn-sm primary numeric-setting-save" data-time-close-kind="${kind}">Сохранить</button>
    </div>
  `;
}

function renderStrategyTimeCloseHtml() {
  const hint = uiHintHtml(
    "Таймер считается от открытия каждой сделки отдельно. После указанного времени сделка закрывается, пока прибыль строго выше порога — в том числе если порог пройден позже дедлайна. Сделки с разным временем входа не закрываются вместе. «Закрытие убытков» — то же для убытка (по умолчанию выкл). PnL до комиссии. Обычные SL/TP работают независимо."
  );
  return `
    <div class="strategy-risk-bar-inner strategy-time-close-stack">
      <span class="strategy-risk-title">Закрытие по истечению времени ${hint}</span>
      ${renderTimeCloseLegHtml({
        kind: "profit",
        title: "Закрытие прибыли",
        enabled: !!strategyRisk.time_close_enabled,
        minutes: Number(strategyRisk.time_close_minutes),
        threshold: Number(strategyRisk.time_close_tp_pct),
        thresholdLabel: "Прибыль",
        ariaThreshold: "Текущая прибыль до комиссии больше, %",
        prefix: "strategy",
      })}
      ${renderTimeCloseLegHtml({
        kind: "loss",
        title: "Закрытие убытков",
        enabled: !!strategyRisk.time_close_loss_enabled,
        minutes: Number(strategyRisk.time_close_loss_minutes),
        threshold: Number(strategyRisk.time_close_loss_tp_pct),
        thresholdLabel: "Убыток",
        ariaThreshold: "Текущий убыток до комиссии больше, %",
        prefix: "strategy",
      })}
    </div>
  `;
}

function bindTimeCloseLeg(container, { prefix, kind, saveFn, enabledKey, minutesKey, tpKey }) {
  if (!container) return;
  const cb = container.querySelector(`.${prefix}-time-close-${kind}-cb`);
  const minInput = container.querySelector(`.${prefix}-time-close-${kind}-minutes`);
  const tpInput = container.querySelector(`.${prefix}-time-close-${kind}-tp`);
  const btn = container.querySelector(".numeric-setting-save");
  if (!cb || !minInput || !tpInput || !btn) return;

  const save = async () => {
    const minutes = Number(minInput.value);
    const tp = Number(tpInput.value);
    if (!Number.isInteger(minutes) || minutes < TEST_TIME_CLOSE_MIN_MIN || minutes > TEST_TIME_CLOSE_MIN_MAX) {
      showToast(`Время: от ${TEST_TIME_CLOSE_MIN_MIN} до ${TEST_TIME_CLOSE_MIN_MAX} мин`, "warn");
      return;
    }
    if (!Number.isFinite(tp) || tp < TEST_TP_MIN || tp > TEST_TP_MAX) {
      showToast(`Порог: от ${TEST_TP_MIN}% до ${TEST_TP_MAX}%`, "warn");
      return;
    }
    btn.disabled = true;
    try {
      await saveFn({
        [enabledKey]: cb.checked,
        [minutesKey]: Math.round(minutes),
        [tpKey]: tp,
      });
    } catch (e) {
      if (e.message === "auth") logout();
      else showToast(formatApiError(e.message), "error");
    } finally {
      btn.disabled = false;
    }
  };

  btn.onclick = save;
  cb.onchange = save;
  minInput.onkeydown = (e) => {
    if (e.key === "Enter") save();
  };
  tpInput.onkeydown = (e) => {
    if (e.key === "Enter") save();
  };
}

function bindStrategyTimeClose(panel) {
  if (!panel) return;
  bindTimeCloseLeg(panel.querySelector("[data-strategy-time-close-profit]"), {
    prefix: "strategy",
    kind: "profit",
    saveFn: setStrategyTimeClose,
    enabledKey: "time_close_enabled",
    minutesKey: "time_close_minutes",
    tpKey: "time_close_tp_pct",
  });
  bindTimeCloseLeg(panel.querySelector("[data-strategy-time-close-loss]"), {
    prefix: "strategy",
    kind: "loss",
    saveFn: setStrategyTimeClose,
    enabledKey: "time_close_loss_enabled",
    minutesKey: "time_close_loss_minutes",
    tpKey: "time_close_loss_tp_pct",
  });
}

function renderTestStrategyRiskInputHtml() {
  const sl = Number(testSettings.stoploss_pct);
  const tp = Number(testSettings.take_profit_pct);
  const hint = uiHintHtml(
    "Общий SL/TP тестового блока. Используется, когда у стратегии выключен «Свой SL/TP»."
  );
  return `
    <div class="strategy-risk-bar-inner">
      <span class="strategy-risk-title">Стоп / тейк (тест) ${hint}</span>
      <div class="numeric-setting strategy-risk-setting" data-test-strategy-risk-input>
        <label class="strategy-risk-field">
          <span class="muted">SL</span>
          <input type="number" class="numeric-setting-input strategy-risk-sl" min="${TEST_SL_MIN}" max="${TEST_SL_MAX}" step="0.5" value="${sl}" inputmode="decimal" aria-label="Стоп-лосс тестовых %" />
          <span class="numeric-setting-suffix">%</span>
        </label>
        <label class="strategy-risk-field">
          <span class="muted">TP</span>
          <input type="number" class="numeric-setting-input strategy-risk-tp" min="${TEST_TP_MIN}" max="${TEST_TP_MAX}" step="0.1" value="${tp}" inputmode="decimal" aria-label="Тейк-профит тестовых %" />
          <span class="numeric-setting-suffix">%</span>
        </label>
        <button type="button" class="btn btn-sm primary numeric-setting-save">Сохранить</button>
      </div>
    </div>
  `;
}

function renderTestStrategyRiskControls() {
  const el = $("test-strategy-risk-panel");
  if (el) {
    el.innerHTML = renderTestStrategyRiskInputHtml();
    bindTestStrategyRiskInput(el.querySelector("[data-test-strategy-risk-input]"));
  }
  const tcEl = $("test-strategy-time-close-panel");
  const locked = settingsReadonly || demoMirrorAdmin;
  const sig = [
    testSettings.time_close_enabled,
    testSettings.time_close_minutes,
    testSettings.time_close_tp_pct,
    testSettings.time_close_loss_enabled,
    testSettings.time_close_loss_minutes,
    testSettings.time_close_loss_tp_pct,
    locked,
  ].join("|");
  if (tcEl && tcEl.dataset.settingsSig !== sig && !tcEl.contains(document.activeElement)) {
    tcEl.innerHTML = renderTestStrategyTimeCloseHtml();
    tcEl.dataset.settingsSig = sig;
    bindTestStrategyTimeClose(tcEl);
    tcEl.querySelectorAll("input, button").forEach((el) => { el.disabled = locked; });
  }
  updateBotSettingsSummaries();
}

function renderTestStrategyTimeCloseHtml() {
  const hint = uiHintHtml(
    "Таймер тестового блока считается от открытия каждой сделки отдельно. После указанного времени сделка закрывается, пока прибыль строго выше порога — в том числе если порог пройден позже дедлайна. «Закрытие убытков» — то же для убытка (по умолчанию выкл). PnL до комиссии. Обычные SL/TP работают независимо."
  );
  return `
    <div class="strategy-risk-bar-inner strategy-time-close-stack">
      <span class="strategy-risk-title">Закрытие по истечению времени ${hint}</span>
      ${renderTimeCloseLegHtml({
        kind: "profit",
        title: "Закрытие прибыли",
        enabled: !!testSettings.time_close_enabled,
        minutes: Number(testSettings.time_close_minutes),
        threshold: Number(testSettings.time_close_tp_pct),
        thresholdLabel: "Прибыль",
        ariaThreshold: "Текущая прибыль до комиссии больше, %",
        prefix: "test",
      })}
      ${renderTimeCloseLegHtml({
        kind: "loss",
        title: "Закрытие убытков",
        enabled: !!testSettings.time_close_loss_enabled,
        minutes: Number(testSettings.time_close_loss_minutes),
        threshold: Number(testSettings.time_close_loss_tp_pct),
        thresholdLabel: "Убыток",
        ariaThreshold: "Текущий убыток до комиссии больше, %",
        prefix: "test",
      })}
    </div>
  `;
}

function bindTestStrategyTimeClose(panel) {
  if (!panel) return;
  bindTimeCloseLeg(panel.querySelector("[data-test-time-close-profit]"), {
    prefix: "test",
    kind: "profit",
    saveFn: setTestStrategySettings,
    enabledKey: "time_close_enabled",
    minutesKey: "time_close_minutes",
    tpKey: "time_close_tp_pct",
  });
  bindTimeCloseLeg(panel.querySelector("[data-test-time-close-loss]"), {
    prefix: "test",
    kind: "loss",
    saveFn: setTestStrategySettings,
    enabledKey: "time_close_loss_enabled",
    minutesKey: "time_close_loss_minutes",
    tpKey: "time_close_loss_tp_pct",
  });
}

async function setTestStrategySettings(patch) {
  const data = await pairConfigApi("/pairs", "POST", {
    action: "set_test_strategy_settings",
    ...patch,
  });
  syncEnabledFromPayload(data);
  syncTestSettingsFromPayload(data);
  updateStrategyDisplay();
  renderStrategyToggles();
  renderTestStrategyRiskControls();
  return data;
}

function bindTestStrategyRiskInput(container) {
  if (!container) return;
  const slInput = container.querySelector(".strategy-risk-sl");
  const tpInput = container.querySelector(".strategy-risk-tp");
  const btn = container.querySelector(".numeric-setting-save");
  if (!slInput || !tpInput || !btn) return;

  const save = async () => {
    const sl = Number(slInput.value);
    const tp = Number(tpInput.value);
    if (!Number.isFinite(sl) || sl < TEST_SL_MIN || sl > TEST_SL_MAX) {
      showToast(`Стоп-лосс: от ${TEST_SL_MIN}% до ${TEST_SL_MAX}%`, "warn");
      return;
    }
    if (!Number.isFinite(tp) || tp < TEST_TP_MIN || tp > TEST_TP_MAX) {
      showToast(`Тейк-профит: от ${TEST_TP_MIN}% до ${TEST_TP_MAX}%`, "warn");
      return;
    }
    btn.disabled = true;
    try {
      await setTestStrategySettings({ stoploss_pct: sl, take_profit_pct: tp });
      await refreshBotSafe("strategy");
    } catch (e) {
      if (e.message === "auth") logout();
      else showToast(formatApiError(e.message), "error");
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
      showToast(`Стоп-лосс: от ${STRATEGY_SL_MIN}% до ${STRATEGY_SL_MAX}%`, "warn");
      return;
    }
    if (!Number.isFinite(tp) || tp < STRATEGY_TP_MIN || tp > STRATEGY_TP_MAX) {
      showToast(`Тейк-профит: от ${STRATEGY_TP_MIN}% до ${STRATEGY_TP_MAX}%`, "warn");
      return;
    }
    btn.disabled = true;
    try {
      await setStrategyRisk(sl, tp);
      await refreshAll();
      if (isSettingsOpen()) {
        await loadPairSettings();
      }
    } catch (e) {
      if (e.message === "auth") logout();
      else showToast(formatApiError(e.message), "error");
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
  const testMax = Number(testSettings.max_open_trades) || 0;
  const mainSlots = Number(BOTS.strategy.maxTrades) || 0;
  const stratSlots = mainSlots + testMax;
  const per =
    Number(BOTS.strategy.maxPerStrategy) > 0
      ? ` · ≤${BOTS.strategy.maxPerStrategy}/стратегия`
      : "";
  const testBit = testMax > 0 ? ` · тест ≤${testMax}` : "";
  el.textContent = `Стратегии — ${mainSlots}+${testMax}${per}${testBit}`;
}

async function setMaxTrades(bot, value) {
  const next = Number(value);
  if (!Number.isFinite(next) || next < 0 || !Number.isInteger(next)) return;
  const data = await pairConfigApi("/pairs", "POST", {
    action: "set_max_trades",
    bot,
    max_open_trades: next,
  });
  pinMaxTrades(bot, data?.max_open_trades ?? next);
  if (data?.limits) syncLimitsFromPairPayload(data);
  updateMaxTradesHint();
  updateBotSettingsSummaries();
  // Refresh card immediately so the input shows the saved value even if
  // live bot reload lagged or the process was down.
  try {
    const snap = botLiveCache[bot] || {};
    const openCount =
      bot === "strategy" && Array.isArray(snap.trades)
        ? splitStrategyTrades(snap.trades).main.length
        : Array.isArray(snap.trades)
          ? snap.trades.length
          : 0;
    renderStats(bot, snap.profit, snap.balance, openCount, snap.config?.stake_amount);
  } catch {
    /* ignore */
  }
  showReloadWarning(data);
  if (data?.trade_warning) {
    showReloadWarning({ reload_warning: data.trade_warning });
  }
  const applied = data?.live_applied !== false;
  const diskMsg =
    `${BOTS[bot].label}: лимит ${BOTS[bot].maxTrades} сохранён` +
    (applied ? "" : " на диск") +
    (data?.reload_warning && !applied
      ? ` (${formatApiError(JSON.stringify({ error: data.reload_warning }))})`
      : "");
  if (isSettingsOpen()) {
    showPairMsg(diskMsg, !applied);
  } else if ($("pair-msg")) {
    showPairMsg(diskMsg, !applied);
  } else {
    // Main panel — brief toast via alert only on failure
    if (!applied && data?.reload_warning) {
      /* soft: value is pinned; background ensure continues */
    }
  }
  return data;
}

async function setMaxOpenTradesPerStrategy(value) {
  const next = Number(value);
  if (!Number.isFinite(next) || next < 0 || !Number.isInteger(next)) return;
  const data = await pairConfigApi("/pairs", "POST", {
    action: "set_max_open_trades_per_strategy",
    max_open_trades_per_strategy: next,
  });
  if (data?.max_open_trades_per_strategy != null) {
    pinMaxPerStrategy(data.max_open_trades_per_strategy);
  } else {
    pinMaxPerStrategy(next);
  }
  updateMaxTradesHint();
  renderMaxPerStrategySettings();
  // Refresh dashboard card so the new value appears under «Открыто сделок»
  refreshBotSafe("strategy").catch(() => {});
  if (isSettingsOpen()) {
    showPairMsg(
      next > 0
        ? `Лимит на стратегию: ${next}`
        : "Лимит на стратегию снят (без потолка)"
    );
  }
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
      showToast("Введите число", "warn");
      return;
    }
    const validation = validate ? validate(next) : true;
    if (validation !== true) {
      showToast(typeof validation === "string" ? validation : "Некорректное значение", "warn");
      return;
    }
    btn.disabled = true;
    const prevLabel = btn.textContent;
    btn.textContent = "…";
    try {
      await onSave(next);
      input.value = String(Number.isInteger(next) ? next : next);
      // Soft refresh for trades/profit — limits already pinned; avoid immediate
      // loadPairSettings rebuild that races and resets the input.
      refreshAll().catch(() => {});
    } catch (e) {
      if (e.message === "auth") logout();
      else showToast(formatApiError(e.message), "error");
    } finally {
      btn.disabled = false;
      btn.textContent = prevLabel || "Сохранить";
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
      value >= 0 && Number.isInteger(value)
        ? true
        : "Целое число ≥ 0 (0 = бот остановлен)",
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
  const label = bot === "grid" ? "Боковиков" : `Макс. сделок ${BOTS[bot].label}`;
  const hint = bot === "grid" ? `<span class="muted stat-hint">сколько каналов держим · 0 = стоп</span>` : "";
  return `
    <div class="numeric-setting" data-max-trades-input data-bot="${bot}">
      <input type="number" class="numeric-setting-input" min="0" step="1" value="${maxTrades}" inputmode="numeric" aria-label="${label}" />
      <button type="button" class="btn btn-sm primary numeric-setting-save">Сохранить</button>
    </div>
    ${hint}
  `;
}

function renderMaxPerStrategyInput() {
  const value = Number(BOTS.strategy.maxPerStrategy) || 0;
  return `
    <div class="numeric-setting" data-max-per-strategy-input>
      <input type="number" class="numeric-setting-input" min="0" step="1" value="${value}" inputmode="numeric" aria-label="Макс. сделок на одну стратегию" />
      <button type="button" class="btn btn-sm primary numeric-setting-save">Сохранить</button>
    </div>
  `;
}

function bindMaxPerStrategyInput(container) {
  bindNumericSetting(container, {
    validate: (value) =>
      value >= 0 && Number.isInteger(value)
        ? true
        : "Целое число ≥ 0 (0 = без лимита на стратегию)",
    onSave: (value) => setMaxOpenTradesPerStrategy(value),
  });
}

function renderMaxPerStrategySettings() {
  const el = $("strategy-max-per-strategy");
  if (!el) return;
  el.innerHTML = `
    <span class="max-trades-label">На одну стратегию</span>
    ${renderMaxPerStrategyInput()}
    <span class="muted max-trades-hint">0 = без лимита · иначе потолок открытых по enter_tag</span>
  `;
  bindMaxPerStrategyInput(el.querySelector("[data-max-per-strategy-input]"));
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

function renderStrategySharedLimitsInto(el, { compact = false } = {}) {
  if (!el) return;
  const bot = "strategy";
  const cfg = BOTS[bot];
  if (compact) {
    el.innerHTML = `
      <div class="stat stat-trades-limit">
        <span class="stat-inline-label">Макс. сделок</span>
        <strong>${cfg.maxTrades}</strong>
        ${renderMaxTradesInput(bot)}
      </div>
      <div class="stat-per-strategy">
        <span class="stat-inline-label">На одну стратегию</span>
        ${renderMaxPerStrategyInput()}
        <span class="muted stat-hint">0 = без лимита</span>
      </div>
      <div class="stat stat-stake-limit">
        <span class="stat-inline-label">Stake</span>
        ${renderStakeInput(bot)}
      </div>
    `;
  } else {
    el.innerHTML = `
      <div class="max-trades-row">
        <span class="max-trades-label">Стратегии · макс. сделок</span>
        ${renderMaxTradesInput(bot)}
        <span class="muted max-trades-hint">общий лимит strategy-бота · 0 = стоп</span>
      </div>
      <div class="max-trades-row">
        <span class="max-trades-label">На одну стратегию</span>
        ${renderMaxPerStrategyInput()}
        <span class="muted max-trades-hint">0 = без лимита · иначе потолок по enter_tag</span>
      </div>
      <div class="max-trades-row">
        <span class="max-trades-label">Стратегии · stake</span>
        ${renderStakeInput(bot)}
        <span class="muted max-trades-hint">от ${STAKE_MIN} до ${STAKE_MAX} USDT · общий для основных и тестовых</span>
      </div>
    `;
  }
  el.querySelectorAll("[data-max-trades-input]").forEach((node) => bindMaxTradesInput(node, bot));
  el.querySelectorAll("[data-max-per-strategy-input]").forEach((node) => bindMaxPerStrategyInput(node));
  el.querySelectorAll("[data-stake-input]").forEach((node) => bindStakeInput(node, bot));
}

function renderTestStrategyStats(el, profit, balance, openCount) {
  renderBotBalanceSection("test-strategy", profit, "test", balance);
  if (!el) return;
  const active = document.activeElement;
  if (active && el.contains(active) && active.matches("input, button")) return;
  const maxOpen = Number(testSettings.max_open_trades) || 0;
  const per = Number(testSettings.max_open_trades_per_strategy) || 0;
  const stake = Number(testSettings.stake_amount) || 5;
  const sig = [
    openCount,
    maxOpen,
    per,
    stake,
  ].join("|");
  const dirtyInput = [...el.querySelectorAll("input")].some(
    (input) => input !== document.activeElement && input.value !== input.defaultValue
  );
  if (!dirtyInput && el.dataset.statSig === sig) return;
  el.dataset.statSig = sig;
  el.innerHTML = `
    <div class="stat stat-trades-limit">
      <span class="stat-inline-label">Открыто (тестовые)</span>
      <strong>${openCount} / ${maxOpen}</strong>
      <div class="numeric-setting" data-test-max-trades-input>
        <input type="number" class="numeric-setting-input" min="0" step="1" value="${maxOpen}" inputmode="numeric" aria-label="Макс. сделок тестового блока" />
        <button type="button" class="btn btn-sm primary numeric-setting-save">Сохранить</button>
      </div>
      <span class="muted stat-hint">0 = стоп тестовых · отдельно от основных</span>
    </div>
    <div class="stat stat-per-strategy">
      <span class="stat-inline-label">На одну тестовую</span>
      <div class="numeric-setting" data-test-max-per-input>
        <input type="number" class="numeric-setting-input" min="0" step="1" value="${per}" inputmode="numeric" aria-label="Макс. сделок на одну тестовую стратегию" />
        <button type="button" class="btn btn-sm primary numeric-setting-save">Сохранить</button>
      </div>
      <span class="muted stat-hint">0 = без лимита по enter_tag</span>
    </div>
    <div class="stat stat-stake-limit">
      <span class="stat-inline-label">Stake (тест)</span>
      <div class="numeric-setting numeric-setting-stake" data-test-stake-input>
        <input type="number" class="numeric-setting-input" min="${STAKE_MIN}" max="${STAKE_MAX}" step="1" value="${stake}" inputmode="decimal" aria-label="Stake тестовых USDT" />
        <span class="numeric-setting-suffix">USDT</span>
        <button type="button" class="btn btn-sm primary numeric-setting-save">Сохранить</button>
      </div>
    </div>
  `;
  bindNumericSetting(el.querySelector("[data-test-max-trades-input]"), {
    validate: (value) =>
      value >= 0 && Number.isInteger(value) ? true : "Целое число ≥ 0",
    onSave: async (value) => {
      pinTestMaxOpen(value);
      await setTestStrategySettings({ max_open_trades: value });
    },
  });
  bindNumericSetting(el.querySelector("[data-test-max-per-input]"), {
    validate: (value) =>
      value >= 0 && Number.isInteger(value) ? true : "Целое число ≥ 0 (0 = без лимита)",
    onSave: async (value) => {
      pinTestMaxPer(value);
      await setTestStrategySettings({ max_open_trades_per_strategy: value });
    },
  });
  bindNumericSetting(el.querySelector("[data-test-stake-input]"), {
    validate: (value) =>
      value >= STAKE_MIN && value <= STAKE_MAX
        ? true
        : `От ${STAKE_MIN} до ${STAKE_MAX} USDT`,
    onSave: async (value) => {
      await setTestStrategySettings({ stake_amount: value });
    },
  });
  updateBotSettingsSummaries();
}

function renderHtfStakeControls() {
  const el = $("htf1h-stake");
  if (!el) return;
  const active = document.activeElement;
  if (active && el.contains(active) && active.matches("input")) return;
  const stake = Number(htfStakeAmount);
  const shown = Number.isFinite(stake) ? stake : 5;
  if (el.dataset.stake === String(shown) && el.querySelector("[data-htf-stake-input]")) return;
  el.dataset.stake = String(shown);
  const locked = settingsReadonly || demoMirrorAdmin;
  el.innerHTML = `
    <span class="max-trades-label">Стейк 1ч</span>
    <div class="numeric-setting numeric-setting-stake" data-htf-stake-input>
      <input type="number" class="numeric-setting-input" min="${STAKE_MIN}" max="${STAKE_MAX}" step="1" value="${shown}" inputmode="decimal" aria-label="Стейк 1-часовых USDT" ${locked ? "disabled" : ""} />
      <span class="numeric-setting-suffix">USDT</span>
      <button type="button" class="btn btn-sm primary numeric-setting-save" ${locked ? "disabled" : ""}>Сохранить</button>
    </div>
    <span class="muted max-trades-hint">только этот аккаунт · основные стратегии не меняет</span>
  `;
  bindNumericSetting(el.querySelector("[data-htf-stake-input]"), {
    validate: (value) =>
      value >= STAKE_MIN && value <= STAKE_MAX ? true : `От ${STAKE_MIN} до ${STAKE_MAX} USDT`,
    onSave: async (value) => {
      const data = await pairConfigApi("/pairs", "POST", {
        action: "set_htf_stake",
        stake_amount: value,
      });
      htfStakeAmount = Number(data?.htf_stake_amount ?? value);
      if (data?.limits) syncLimitsFromPairPayload(data);
      syncEnabledFromPayload(data);
      el.dataset.stake = "";
      renderHtfStakeControls();
    },
  });
}

function renderHtfStrategyStats(el, profit, openCount, balance) {
  if (!el) return;
  const n = profit?.trade_count_htf;
  const closedNote = n != null ? `${n} сделок` : "";
  renderBotBalanceSection("htf1h", profit, "htf1h", balance, closedNote);
  const sig = String(openCount);
  if (el.dataset.statSig === sig) return;
  el.dataset.statSig = sig;
  el.innerHTML = `
    <div class="stat">Открыто (1-часовые)<strong>${openCount}</strong></div>
  `;
}

function renderTestStrategyLimits() {
  /* limits rendered in #test-strategy-stats */
}

function isTestStrategyId(sid) {
  const id = String(sid || "");
  if (!id) return false;
  const base = statsRowBaseId(id);
  const fromCatalog = (strategyCatalog || []).find((s) => s.id === id || s.id === base);
  if (fromCatalog) return isTestLaneStrategy(fromCatalog);
  // Catalog may still be loading, or the tenant allowlist omitted this id.
  return /TestStrategy$/i.test(id);
}

function isTestTrade(trade, bot = "strategy") {
  if (bot !== "strategy") return false;
  if (isTestStrategyId(tradeSourceId(trade, bot))) return true;
  const strat = String(trade.strategy || "");
  return !!(strat && strat !== "MultiStrategyRouter" && /TestStrategy$/i.test(strat));
}

function repaintStrategyTradesFromCache() {
  const snap = botLiveCache.strategy;
  if (!snap || !Array.isArray(snap.trades)) return;
  paintBotLive("strategy", {
    config: snap.config,
    profit: snap.profit,
    balance: snap.balance,
    trades: snap.trades,
    running: snap.running,
  });
}

function isLlmTrade(trade) {
  const tag = String(trade?.enter_tag || "");
  const strat = String(trade?.strategy || "");
  return tag.startsWith("DeepseekLlmStrategy") || strat === "DeepseekLlmStrategy";
}

function isCursorTrade(trade) {
  const tag = String(trade?.enter_tag || "");
  const strat = String(trade?.strategy || "");
  return tag.startsWith("CursorLlmStrategy") || strat === "CursorLlmStrategy";
}

function isCursorBookTrade(trade) {
  const tag = String(trade?.enter_tag || "");
  const strat = String(trade?.strategy || "");
  return tag.startsWith("Cursor50") || strat === "Cursor50";
}

function isHtfTrade(trade) {
  const id = tradeSourceId(trade, "strategy");
  const fromCatalog = (strategyCatalog || []).find((s) => s.id === id);
  if (fromCatalog) return isHtfStrategy(fromCatalog);
  return /1hStrategy$/i.test(String(id || ""));
}

function splitStrategyTrades(trades) {
  const list = trades || [];
  const main = [];
  const test = [];
  const htf = [];
  for (const t of list) {
    if (isRetiredTrade(t)) continue;
    if (isHtfTrade(t)) htf.push(t);
    else if (isTestTrade(t, "strategy")) test.push(t);
    else main.push(t);
  }
  return { main, test, htf };
}

function renderBotBalanceSection(prefix, profit, scope, balance, note = "") {
  const body = $(`${prefix}-balance-body`);
  if (!body) return;
  const usdt = getUsdtWallet(balance);
  const botAvail = usdt?.bot_owned;
  const walletFree = usdt?.free;
  const inMargin = usdt?.used;
  const closed = fmtClosedProfit(profit, scope);
  const walletHint = balance?.note || `общий кошелёк ${walletFree != null ? fmtUsd(walletFree) : "—"} · в марже ${inMargin != null ? fmtUsd(inMargin) : "—"}`;
  const sig = [closed, botAvail ?? "", walletHint, note].join("|");
  if (body.dataset.statSig === sig) return;
  body.dataset.statSig = sig;
  body.innerHTML = `
    <div class="stats">
      <div class="stat">Прибыль (закрытые)<strong>${closed}</strong>${note ? `<span class="stat-hint">${note}</span>` : ""}</div>
      <div class="stat" title="Сколько USDT этот бот может использовать для новой сделки (учитываются только его сделки в БД)">
        Доступно боту<strong>${botAvail != null ? fmtUsd(botAvail) : "—"}</strong>
        <span class="stat-hint">${escapeHtml(walletHint)}</span>
      </div>
    </div>
  `;
  const summary = $(`${prefix}-balance-summary`);
  if (summary) {
    const coin = closedCoin(profit, scope);
    const parts = [];
    if (coin != null && Number.isFinite(Number(coin))) parts.push(`PnL ${fmtUsdSigned(coin)}`);
    if (botAvail != null) parts.push(`доступно ${fmtUsd(botAvail)}`);
    summary.textContent = parts.length ? parts.join(" · ") : "нет данных";
  }
}

function renderStatsInto(el, bot, profit, balance, openCount, stakeAmount, { openLabel = null } = {}) {
  renderBotBalanceSection(bot, profit, bot === "strategy" ? "main" : null, balance);
  if (!el) return;
  // Poll rebuild must not wipe an in-progress edit of max trades / stake.
  const active = document.activeElement;
  if (
    active &&
    el.contains(active) &&
    active.matches("input.numeric-setting-input, button.numeric-setting-save")
  ) {
    const strong = el.querySelector(".stat-trades-limit > strong");
    if (strong) strong.textContent = `${openCount} / ${BOTS[bot].maxTrades}`;
    return;
  }
  const cfg = BOTS[bot];
  if (STAKE_EDITABLE_BOTS.has(bot) && stakeAmount != null) {
    BOTS[bot].stakeAmount = Number(stakeAmount);
  }
  const stakeCell = STAKE_EDITABLE_BOTS.has(bot)
    ? `<div class="stat stat-stake-limit"><span class="stat-inline-label">Stake</span>${renderStakeInput(bot)}</div>`
    : `<div class="stat">Stake<strong>${fmtUsd(stakeAmount ?? 5)}</strong></div>`;
  const perStrategyBlock =
    bot === "strategy"
      ? `<div class="stat stat-per-strategy">
          <span class="stat-inline-label">На одну стратегию</span>
          ${renderMaxPerStrategyInput()}
          <span class="muted stat-hint">0 = без лимита</span>
        </div>`
      : "";
  const tradesTitle = openLabel || "Открыто сделок";
  const statSig = [
    bot,
    openCount,
    cfg.maxTrades,
    BOTS[bot]?.stakeAmount ?? stakeAmount ?? "",
    bot === "strategy" ? BOTS.strategy.maxPerStrategy : "",
    settingsReadonly ? 1 : 0,
    tradesTitle,
  ].join("|");
  const dirtyInput = [...el.querySelectorAll("input")].some(
    (input) => input !== document.activeElement && input.value !== input.defaultValue
  );
  if (!dirtyInput && el.dataset.statSig === statSig) return;
  el.dataset.statSig = statSig;
  el.innerHTML = `
    <div class="stat stat-trades-limit">
      ${tradesTitle}<strong>${openCount} / ${cfg.maxTrades}</strong>
      ${renderMaxTradesInput(bot)}
    </div>
    ${perStrategyBlock}
    ${stakeCell}
  `;
  bindMaxTradesInput(el.querySelector("[data-max-trades-input]"), bot);
  if (bot === "strategy") {
    bindMaxPerStrategyInput(el.querySelector("[data-max-per-strategy-input]"));
  }
  if (STAKE_EDITABLE_BOTS.has(bot)) {
    bindStakeInput(el.querySelector("[data-stake-input]"), bot);
  }
  updateBotSettingsSummaries();
}

function renderStats(bot, profit, balance, openCount, stakeAmount) {
  const openLabel = bot === "grid" ? "Боковиков" : null;
  renderStatsInto($(BOTS[bot].statsEl), bot, profit, balance, openCount, stakeAmount, { openLabel });
}

/** Stake PnL at a given mark vs open_rate (same formula as backend mark-to-market). */
function tradePnlAtRate(t, rate) {
  const open = Number(t.open_rate);
  const r = Number(rate);
  const lev = Number(t.leverage) || 1;
  const stake = Number(t.stake_amount) || 0;
  if (!(open > 0) || !(r > 0) || !Number.isFinite(r)) return null;
  const ratio = t.is_short ? ((open - r) / open) * lev : ((r - open) / open) * lev;
  return { ratio, abs: ratio * stake, pct: ratio * 100 };
}

function fmtTradePnlSpan(pnl) {
  if (!pnl) return "—";
  return `<span class="${pnlClass(pnl.abs)}">${fmtPct(pnl.pct)} (${fmtUsd(pnl.abs, 4)})</span>`;
}

function tradeHasTrackedExtremes(t) {
  const minR = Number(t?.min_rate);
  const maxR = Number(t?.max_rate);
  return minR > 0 && maxR > 0 && Number.isFinite(minR) && Number.isFinite(maxR);
}

/**
 * Best / worst stake-PnL extremes from min_rate/max_rate.
 * requireTracked=true (history): empty when min/max never recorded (old closes).
 * Open cards also fold in current_rate; closed — close_rate when tracked.
 */
function tradeMaxProfitLoss(t, { requireTracked = false } = {}) {
  const open = Number(t.open_rate);
  if (!(open > 0)) return { maxProfit: null, maxLoss: null };
  const hasTracked = tradeHasTrackedExtremes(t);
  if (requireTracked && !hasTracked) return { maxProfit: null, maxLoss: null };

  const cur = Number(t.current_rate);
  const close = Number(t.close_rate);
  let minR = hasTracked ? Number(t.min_rate) : NaN;
  let maxR = hasTracked ? Number(t.max_rate) : NaN;
  const extras = [];
  if (Number.isFinite(cur) && cur > 0) extras.push(cur);
  if (Number.isFinite(close) && close > 0) extras.push(close);

  if (!(minR > 0) || !(maxR > 0)) {
    if (requireTracked) return { maxProfit: null, maxLoss: null };
    minR = extras.length ? Math.min(open, ...extras) : open;
    maxR = extras.length ? Math.max(open, ...extras) : open;
  } else {
    for (const e of extras) {
      minR = Math.min(minR, e);
      maxR = Math.max(maxR, e);
    }
  }

  const atMin = tradePnlAtRate(t, minR);
  const atMax = tradePnlAtRate(t, maxR);
  const candidates = [atMin, atMax].filter(Boolean);
  if (!candidates.length) return { maxProfit: null, maxLoss: null };
  let maxProfit = candidates[0];
  let maxLoss = candidates[0];
  for (const c of candidates) {
    if (c.ratio > maxProfit.ratio) maxProfit = c;
    if (c.ratio < maxLoss.ratio) maxLoss = c;
  }
  return { maxProfit, maxLoss };
}

function fmtHistoryExtremeCell(pnl) {
  if (!pnl) return "—";
  return `${fmtUsdSigned(pnl.abs, 2)}<br><span class="muted history-pnl-pct">${fmtPct(pnl.pct)}</span>`;
}

function historyExtremeExportFields(t) {
  const { maxProfit, maxLoss } = tradeMaxProfitLoss(t, { requireTracked: true });
  return {
    max_profit_usdt: maxProfit ? maxProfit.abs.toFixed(4) : "",
    max_profit_pct: maxProfit ? maxProfit.pct.toFixed(2) : "",
    max_loss_usdt: maxLoss ? maxLoss.abs.toFixed(4) : "",
    max_loss_pct: maxLoss ? maxLoss.pct.toFixed(2) : "",
  };
}

function tradeDetailRows(t) {
  const direction = t.is_short ? "SHORT" : "LONG";
  const lev = t.leverage || 1;
  const notional = t.amount != null && t.current_rate != null ? t.amount * t.current_rate : null;
  const openOrders = (t.orders || []).filter((o) => o.is_open);
  const openOrderText = openOrders.length
    ? openOrders.map((o) => `${o.ft_order_side} ${o.order_type}`).join(", ")
    : null;
  const extremes = tradeMaxProfitLoss(t);

  return [
    ["Пара", t.pair],
    ["№ сделки", `#${t.trade_id}`],
    ["Направление", `${direction} · ${lev}x`],
    ["Стратегия", t.strategy || "—"],
    ["Тег входа", t.enter_tag || "—"],
    ["ML уверенность", fmtMlConfidenceDetail(t)],
    ["Открыта", t.open_date ? fmtAppDateTime(t.open_date) : "—"],
    ["Цена входа", fmtRate(t.open_rate)],
    ["Текущая цена", fmtRate(t.current_rate)],
    ["Объём", `${t.amount ?? "—"} (${fmtUsd(t.stake_amount)})`],
    ["Номинал (сейчас)", notional != null ? fmtUsd(notional) : "—"],
    ["Вложено (маржа)", fmtUsd(t.max_stake_amount ?? t.stake_amount)],
    ["Нереализ. PnL", `<span class="${pnlClass(t.profit_abs)}">${fmtPct(t.profit_pct)} (${fmtUsd(t.profit_abs, 4)})</span>`],
    ["Макс. прибыль", fmtTradePnlSpan(extremes.maxProfit)],
    ["Макс. убыток", fmtTradePnlSpan(extremes.maxLoss)],
    ["Общий PnL", `<span class="${pnlClass(t.total_profit_abs)}">${fmtPctRatio(t.total_profit_ratio)} (${fmtUsd(t.total_profit_abs, 4)})</span>`],
    ["Комиссия funding", t.funding_fees != null ? fmtUsd(t.funding_fees, 4) : "—"],
    ["Стоп-лосс", t.stop_loss_abs != null ? `${fmtRate(t.stop_loss_abs)} (${fmtPctRatio(t.stop_loss_ratio)})` : "—"],
    ["До стопа", t.stoploss_current_dist != null ? `${fmtRate(t.stoploss_current_dist)} (${fmtPctRatio(t.stoploss_current_dist_ratio)})` : "—"],
    ["Ликвидация", t.liquidation_price != null ? fmtRate(t.liquidation_price) : "—"],
    ["Min / Max", `${fmtRate(t.min_rate)} / ${fmtRate(t.max_rate)}`],
    ["Открытый ордер", openOrderText || "—"],
  ];
}

function tradeListSignature(bot, trades) {
  const readonly = settingsReadonly || demoMirrorAdmin ? "1" : "0";
  if (!trades.length) return `empty|${readonly}`;
  return (
    readonly +
    "\n" +
    trades
      .map((t) => {
        const orders = (t.orders || [])
          .filter((o) => o.is_open)
          .map((o) => `${o.ft_order_side}:${o.order_type}`)
          .join(",");
        return [
          t.trade_id,
          t.pair,
          t.is_short ? 1 : 0,
          t.leverage || 1,
          t.strategy || "",
          t.enter_tag || "",
          t.open_date || "",
          fmtMlConfidenceDetail(t),
          t.open_rate,
          t.current_rate,
          t.amount,
          t.stake_amount,
          t.max_stake_amount,
          t.profit_pct,
          t.profit_abs,
          t.total_profit_ratio,
          t.total_profit_abs,
          t.funding_fees,
          t.stop_loss_abs,
          t.stop_loss_ratio,
          t.stoploss_current_dist,
          t.stoploss_current_dist_ratio,
          t.liquidation_price,
          t.min_rate,
          t.max_rate,
          orders,
          isTradeExpanded(bot, t.trade_id) ? 1 : 0,
        ].join("|");
      })
      .join("\n")
  );
}

function tradesSummaryText(trades, emptyLabel = "нет сделок") {
  const open = trades || [];
  if (!open.length) return `${emptyLabel} · развернуть`;
  if (open.length === 1) {
    const t = open[0];
    const pair = t.pair || t.symbol || "#1";
    return `${pair} · развернуть`;
  }
  return `${open.length} сделок · развернуть`;
}

function updateTradesSectionSummary(summaryId, trades, emptyLabel) {
  const summary = $(summaryId);
  if (summary) summary.textContent = tradesSummaryText(trades, emptyLabel);
}


const BOARD_CHART_MS = 10 * 60 * 1000;
let boardMode = "summary";
let openSummarySeq = 0;
let boardChartsAt = 0;

function boardChartsStale() {
  return Date.now() - boardChartsAt >= BOARD_CHART_MS;
}

function tradeBoardVisible() {
  if (boardMode !== "trades") return false;
  const dash = $("dashboard-view");
  if (!dash || dash.classList.contains("hidden")) return false;
  return panelIsActive();
}

function boardTradeKey(entry) {
  const id = entry.trade.trade_id ?? entry.trade.id;
  return `${entry.bot}:${id}`;
}

function boardTradePnl(entry) {
  const t = entry.trade;
  const abs = Number(t.profit_abs ?? t.total_profit_abs ?? 0);
  const pct = Number(t.profit_pct ?? 0);
  return {
    abs: Number.isFinite(abs) ? abs : 0,
    pct: Number.isFinite(pct) ? pct : 0,
  };
}

function boardChartModel(entry, model) {
  const pnl = boardTradePnl(entry);
  const mark = Number(entry.trade.current_rate);
  return {
    ...model,
    pnlAbs: pnl.abs,
    pnlPct: pnl.pct,
    closeRate: mark > 0 ? mark : model.closeRate,
  };
}

function collectBoardTrades() {
  const bots = ["strategy"];
  if (userMayUseBlock("grid")) bots.push("grid");
  if (userMayUseBlock("finder")) bots.push("finder");
  const entries = [];
  for (const bot of bots) {
    for (const trade of botLiveCache[bot]?.trades || []) {
      if (!trade?.pair || isRetiredTrade(trade)) continue;
      const id = trade.trade_id ?? trade.id;
      if (id == null || id === "") continue;
      entries.push({ bot, trade, open: true });
    }
  }
  entries.sort((a, b) => {
    const ta = parseAppInstant(a.trade.open_date)?.getTime() || 0;
    const tb = parseAppInstant(b.trade.open_date)?.getTime() || 0;
    return tb - ta;
  });
  return entries;
}

function boardCardHtml(entry) {
  const t = entry.trade;
  const key = boardTradeKey(entry);
  const pnl = boardTradePnl(entry);
  const cls = pnlClass(pnl.abs);
  const tradeId = t.trade_id ?? t.id;
  const closeBtn =
    settingsReadonly || demoMirrorAdmin
      ? ""
      : `<button type="button" class="btn danger btn-sm trade-chart-close" data-act="forceexit" data-bot="${escapeHtml(entry.bot)}" data-id="${escapeHtml(tradeId)}">Закрыть</button>`;
  return `<article class="trade-chart-card ${cls}" data-board-key="${escapeHtml(key)}">
    <div class="trade-chart-card-top">
      <strong class="trade-chart-pair" title="${escapeHtml(t.pair || "")}">${escapeHtml(t.pair || "—")}</strong>
      <div class="trade-chart-pnl ${cls}">
        <span class="trade-chart-pnl-abs">${fmtUsdSigned(pnl.abs, 2)}</span>
        <span class="trade-chart-pnl-pct">${fmtPct(pnl.pct)}</span>
      </div>
      ${closeBtn}
    </div>
    <div class="trade-chart-card-plot">
      <canvas class="trade-chart-mini" data-board-chart="${escapeHtml(key)}" aria-label="График ${escapeHtml(t.pair || "сделки")}"></canvas>
      <p class="trade-chart-mini-status muted">График…</p>
    </div>
  </article>`;
}

function updateBoardCard(card, entry) {
  const pnl = boardTradePnl(entry);
  const cls = pnlClass(pnl.abs);
  card.classList.toggle("pos", cls === "pos");
  card.classList.toggle("neg", cls === "neg");
  const wrap = card.querySelector(".trade-chart-pnl");
  if (wrap) {
    wrap.classList.toggle("pos", cls === "pos");
    wrap.classList.toggle("neg", cls === "neg");
  }
  const abs = card.querySelector(".trade-chart-pnl-abs");
  const pct = card.querySelector(".trade-chart-pnl-pct");
  if (abs) abs.textContent = fmtUsdSigned(pnl.abs, 2);
  if (pct) pct.textContent = fmtPct(pnl.pct);
}

function boardChartCssSize(canvas) {
  const width = Math.round(canvas.parentElement?.clientWidth || 0);
  if (width < 80) return null;
  const height = Math.round(Math.max(168, Math.min(280, width * 0.68)));
  return { width, height };
}

function setBoardChartStatus(canvas, text) {
  const status = canvas.parentElement?.querySelector(".trade-chart-mini-status");
  if (!status) return;
  status.textContent = text || "";
  status.classList.toggle("is-hidden", !text);
}

async function paintBoardChart(entry, { fresh = false } = {}) {
  const key = boardTradeKey(entry);
  const canvas = document.querySelector(`canvas[data-board-chart="${key}"]`);
  if (!canvas || !tradeBoardVisible()) return false;
  const size = boardChartCssSize(canvas);
  if (!size) return false;
  const sameSize = canvas.dataset.w === String(size.width);
  if (canvas.dataset.painting === "1") return false;
  if (!fresh && sameSize && (canvas.dataset.ready === "1" || canvas.dataset.ready === "err")) return false;
  canvas.dataset.painting = "1";
  const gen = String((Number(canvas.dataset.gen) || 0) + 1);
  canvas.dataset.gen = gen;
  if (canvas.dataset.ready !== "1") setBoardChartStatus(canvas, "График…");
  try {
    if (fresh) chartsModelCache.delete(chartEntryKey(entry));
    const model = boardChartModel(entry, await loadChartModel(entry));
    if (canvas.dataset.gen !== gen || !canvas.isConnected || !tradeBoardVisible()) return false;
    const next = boardChartCssSize(canvas) || size;
    paintTradeChart(canvas, model, next.width, next.height);
    canvas.dataset.w = String(next.width);
    canvas.dataset.ready = "1";
    setBoardChartStatus(canvas, "");
    return true;
  } catch (err) {
    if (canvas.dataset.gen !== gen || !canvas.isConnected) return false;
    canvas.dataset.w = String(size.width);
    canvas.dataset.ready = "err";
    setBoardChartStatus(canvas, chartErrorText(err));
    return false;
  } finally {
    if (canvas.isConnected && canvas.dataset.gen === gen) canvas.dataset.painting = "0";
  }
}

async function paintBoardCharts(entries, { fresh = false } = {}) {
  const queue = entries.slice();
  if (!queue.length) return;
  const workers = Array.from({ length: 1 }, async () => {
    while (queue.length && tradeBoardVisible()) {
      const entry = queue.shift();
      await paintBoardChart(entry, { fresh });
    }
  });
  await Promise.all(workers);
}

function renderTradeChartsBoard({ repaint = false } = {}) {
  const root = $("trade-charts-board");
  if (!root || boardMode !== "trades") return;
  const entries = collectBoardTrades();
  const sig = entries.map(boardTradeKey).join("|");
  if (root.dataset.sig !== sig) {
    root.dataset.sig = sig;
    root.innerHTML = entries.length
      ? entries.map(boardCardHtml).join("")
      : '<p class="empty trade-charts-empty">Нет открытых сделок</p>';
  } else {
    for (const entry of entries) {
      const card = root.querySelector(`[data-board-key="${boardTradeKey(entry)}"]`);
      if (card) updateBoardCard(card, entry);
    }
  }
  if (!entries.length) return;
  paintBoardCharts(entries, { fresh: repaint }).catch(() => {});
}

function syncTradeChartsBoard() {
  if (!tradeBoardVisible()) return;
  const repaint = boardChartsStale();
  if (repaint) boardChartsAt = Date.now();
  renderTradeChartsBoard({ repaint });
}

function setBoardPane(id, show) {
  const node = $(id);
  if (!node) return;
  node.classList.toggle("hidden", !show);
  node.hidden = !show;
  node.setAttribute("aria-hidden", show ? "false" : "true");
}

function openTradeNetAbs(trade) {
  const gross = Number(trade?.profit_abs);
  if (!Number.isFinite(gross)) return null;
  const mark = Number(trade.current_rate || trade.close_rate || trade.open_rate);
  const fees = tradeRoundTripFees({
    ...trade,
    close_rate: mark > 0 ? mark : trade.open_rate,
  });
  return gross - fees;
}

function summarizeOpenTrades(trades) {
  const list = (trades || []).filter((trade) => trade?.pair && !isRetiredTrade(trade));
  const exchanges = new Set(list.map((trade) => trade.exchange || "bybit"));
  const showEx = exchanges.size > 1;
  const groups = new Map();
  for (const trade of list) {
    const side = trade.is_short ? "short" : "long";
    const ex = String(trade.exchange || "bybit");
    const exLabel = ex === "binance" ? "Binance" : "Bybit";
    const name = tradeSourceLabel(trade, trade.bot || "strategy") + (showEx ? ` · ${exLabel}` : "");
    const key = `${name}|${side}`;
    let row = groups.get(key);
    if (!row) {
      row = { name, side, count: 0, pnl: 0, known: false, wins: 0, losses: 0, winPnl: 0, lossPnl: 0 };
      groups.set(key, row);
    }
    row.count += 1;
    const net = openTradeNetAbs(trade);
    if (net != null) {
      row.pnl += net;
      row.known = true;
      if (net > 0) {
        row.wins += 1;
        row.winPnl += net;
      } else if (net < 0) {
        row.losses += 1;
        row.lossPnl += net;
      }
    }
  }
  return [...groups.values()].sort((a, b) => {
    const byName = a.name.localeCompare(b.name, "ru");
    if (byName) return byName;
    return a.side === b.side ? 0 : a.side === "long" ? -1 : 1;
  });
}

const openSummaryExpanded = new Set();

function openSummaryRowKey(mode, row) {
  return `${mode}|${row.name}`;
}

function foldSummaryStrategies(sideRows) {
  const groups = new Map();
  for (const row of sideRows) {
    let group = groups.get(row.name);
    if (!group) {
      group = { name: row.name, count: 0, pnl: 0, known: false, long: null, short: null };
      groups.set(row.name, group);
    }
    group.count += row.count;
    if (row.known) {
      group.pnl += row.pnl;
      group.known = true;
    }
    group[row.side === "short" ? "short" : "long"] = row;
  }
  return [...groups.values()].sort((a, b) => a.name.localeCompare(b.name, "ru"));
}

function openSummarySideLine(label, side, row) {
  const count = row?.count || 0;
  const known = !!row?.known;
  const pnl = known ? fmtUsdSigned(row.pnl, 2) : count ? "—" : fmtUsdSigned(0, 2);
  const cls = known ? pnlClass(row.pnl) : "";
  return `<div class="open-summary-side-line">
    <span class="open-summary-side is-${side}">${label}</span>
    <span class="open-summary-count">${count}</span>
    <span class="open-summary-pnl ${cls}">${pnl}</span>
  </div>`;
}

function openSummaryRowHtml(mode, row) {
  const cls = row.known ? pnlClass(row.pnl) : "";
  const pnl = row.known ? fmtUsdSigned(row.pnl, 2) : "—";
  const key = openSummaryRowKey(mode, row);
  const open = openSummaryExpanded.has(key);
  return `<button type="button" class="open-summary-row open-summary-toggle" data-summary-key="${escapeHtml(key)}" aria-expanded="${open ? "true" : "false"}">
    <span class="open-summary-count">${row.count}</span>
    <span class="open-summary-name" title="${escapeHtml(row.name)}">${escapeHtml(row.name)}</span>
    <span class="open-summary-pnl ${cls}">${pnl}</span>
  </button>
  <div class="open-summary-detail"${open ? "" : " hidden"}>
    ${openSummarySideLine("Лонг", "long", row.long)}
    ${openSummarySideLine("Шорт", "short", row.short)}
  </div>`;
}

function openSummaryTotalHtml(label, count, pnl, known) {
  const cls = known ? pnlClass(pnl) : "";
  const value = known ? fmtUsdSigned(pnl, 2) : "—";
  return `<div class="open-summary-total">
    <span>${escapeHtml(label)} · ${count}</span>
    <span class="open-summary-total-pnl ${cls}">${value}</span>
  </div>`;
}

function renderOpenSummaryBoard(data) {
  const root = $("trade-summary-board");
  if (!root) return;
  const accounts = (data?.accounts || []).filter(
    (acc) => acc && (acc.active || (acc.rows || []).length || (acc.trades || []).length)
  );
  if (!accounts.length) {
    root.innerHTML = `<p class="empty">Нет открытых сделок</p>`;
    root.dataset.ready = "1";
    return;
  }
  let grandCount = 0;
  let grandPnl = 0;
  let grandKnown = false;
  const blocks = accounts.map((acc) => {
    const mode = acc.mode === "demo" ? "demo" : "live";
    const title = mode === "demo" ? "Demo" : "Live";
    const sideRows = Array.isArray(acc.rows)
      ? acc.rows
          .map((row) => ({
            name: row?.name || "—",
            side: row?.side === "short" ? "short" : "long",
            count: Number(row?.count) || 0,
            pnl: Number(row?.pnl),
            known: row?.pnl != null && Number.isFinite(Number(row.pnl)),
          }))
          .filter((row) => row.count > 0)
      : summarizeOpenTrades(acc.trades);
    const rows = foldSummaryStrategies(sideRows);
    const count = rows.reduce((sum, row) => sum + row.count, 0);
    const knownRows = rows.filter((row) => row.known);
    const pnl = knownRows.reduce((sum, row) => sum + row.pnl, 0);
    const known = knownRows.length > 0;
    grandCount += count;
    if (known) {
      grandPnl += pnl;
      grandKnown = true;
    }
    const body = rows.length
      ? `<div class="open-summary-row open-summary-cols"><span>Кол-во</span><span>Стратегия</span><span>PNL</span></div>${rows.map((row) => openSummaryRowHtml(mode, row)).join("")}`
      : `<p class="empty open-summary-empty">Нет открытых сделок</p>`;
    return `<section class="open-summary-account">
      <div class="open-summary-account-head">
        <span class="mode-badge mode-badge-${mode}">${title}</span>
        <span class="muted open-summary-fee">PNL с комиссией</span>
      </div>
      ${body}
      ${openSummaryTotalHtml("Итого", count, pnl, known)}
    </section>`;
  });
  const grand = accounts.length > 1
    ? `<section class="open-summary-grand">${openSummaryTotalHtml("Итого по счетам", grandCount, grandPnl, grandKnown)}</section>`
    : "";
  root.innerHTML = blocks.join("") + grand + openSummaryWeekHtml(accounts);
  root.dataset.ready = "1";
}

function openSummaryWeekRows(rows) {
  return (rows || [])
    .map((row) => ({
      name: row?.name || "—",
      count: Number(row?.count) || 0,
      pnl: Number(row?.pnl),
      known: row?.pnl != null && Number.isFinite(Number(row.pnl)),
    }))
    .filter((row) => row.count > 0);
}

function openSummaryStaticRowsHtml(rows) {
  if (!rows.length) return `<p class="empty open-summary-empty">Нет закрытых сделок</p>`;
  return `<div class="open-summary-row open-summary-cols"><span>Кол-во</span><span>Стратегия</span><span>PNL</span></div>${rows
    .map((row) => {
      const cls = row.known ? pnlClass(row.pnl) : "";
      const pnl = row.known ? fmtUsdSigned(row.pnl, 2) : "—";
      return `<div class="open-summary-row">
        <span class="open-summary-count">${row.count}</span>
        <span class="open-summary-name" title="${escapeHtml(row.name)}">${escapeHtml(row.name)}</span>
        <span class="open-summary-pnl ${cls}">${pnl}</span>
      </div>`;
    })
    .join("")}`;
}

function openSummaryWeekHtml(accounts) {
  const shown = (accounts || []).filter((acc) => acc && (acc.active || (acc.week || []).length));
  if (!shown.length) return "";
  let grandCount = 0;
  let grandPnl = 0;
  let grandKnown = false;
  const parts = shown.map((acc) => {
    const mode = acc.mode === "demo" ? "demo" : "live";
    const title = mode === "demo" ? "Demo" : "Live";
    const rows = openSummaryWeekRows(acc.week);
    const count = rows.reduce((sum, row) => sum + row.count, 0);
    const knownRows = rows.filter((row) => row.known);
    const pnl = knownRows.reduce((sum, row) => sum + row.pnl, 0);
    const known = knownRows.length > 0;
    grandCount += count;
    if (known) {
      grandPnl += pnl;
      grandKnown = true;
    }
    const badge = shown.length > 1 ? `<div class="open-summary-week-mode"><span class="mode-badge mode-badge-${mode}">${title}</span></div>` : "";
    return `${badge}${openSummaryStaticRowsHtml(rows)}${openSummaryTotalHtml("Итого", count, pnl, known)}`;
  });
  const grand = shown.length > 1
    ? openSummaryTotalHtml("Итого по счетам", grandCount, grandPnl, grandKnown)
    : "";
  return `<section class="open-summary-account open-summary-week">
    <div class="open-summary-account-head">
      <strong>За последнюю неделю</strong>
      <span class="muted open-summary-fee">PNL с комиссией</span>
    </div>
    ${parts.join("")}
    ${grand}
  </section>`;
}

async function syncOpenSummaryBoard() {
  const root = $("trade-summary-board");
  if (!root || boardMode !== "summary" || !panelIsActive()) return;
  const seq = ++openSummarySeq;
  if (!root.dataset.ready) root.innerHTML = `<p class="empty">Сводка…</p>`;
  try {
    const data = await pairConfigApi("/open-trades?account_mode=all");
    if (seq !== openSummarySeq || boardMode !== "summary") return;
    renderOpenSummaryBoard(data);
  } catch (err) {
    if (seq !== openSummarySeq || boardMode !== "summary") return;
    if (err?.message === "auth") throw err;
    root.innerHTML = `<p class="empty">${escapeHtml(err?.message || "Не удалось загрузить сводку")}</p>`;
  }
}

function setBoardMode(mode) {
  boardMode = mode === "trades" || mode === "summary" ? mode : "bots";
  $("main-content")?.classList.toggle("is-trade-board", boardMode !== "bots");
  setBoardPane("trade-charts-board", boardMode === "trades");
  setBoardPane("trade-summary-board", boardMode === "summary");
  document.querySelectorAll(".board-switch-btn[data-board]").forEach((btn) => {
    const on = btn.dataset.board === boardMode;
    btn.classList.toggle("is-active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  });
  if (boardMode === "trades") {
    const hasTrades = ["strategy", "grid", "finder"].some((bot) => (botLiveCache[bot]?.trades || []).length);
    if (hasTrades) {
      const repaint = panelIsActive() && boardChartsStale();
      if (repaint) boardChartsAt = Date.now();
      renderTradeChartsBoard({ repaint });
    } else {
      const root = $("trade-charts-board");
      if (root && !root.dataset.sig) root.innerHTML = `<p class="empty">Графики…</p>`;
    }
  } else if (boardMode === "bots") {
    lastHeavyRefreshAt = 0;
  }
  refreshAll({ heavy: boardMode === "bots" }).catch((err) => {
    if (err?.message === "auth") logout();
  });
}

function bindBoardSwitch() {
  document.querySelector(".board-switch")?.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-board]");
    if (!btn || btn.dataset.board === boardMode) return;
    setBoardMode(btn.dataset.board);
  });
  $("trade-summary-board")?.addEventListener("click", (ev) => {
    const row = ev.target.closest("[data-summary-key]");
    if (!row) return;
    const key = row.dataset.summaryKey;
    const detail = row.nextElementSibling;
    const open = row.getAttribute("aria-expanded") === "true";
    row.setAttribute("aria-expanded", open ? "false" : "true");
    if (detail?.classList.contains("open-summary-detail")) detail.hidden = open;
    if (open) openSummaryExpanded.delete(key);
    else openSummaryExpanded.add(key);
  });
  $("trade-charts-board")?.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-act=forceexit]");
    if (!btn || btn.disabled) return;
    ev.preventDefault();
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
        showToast(`Сделка #${tradeId} удалена из базы бота (на бирже уже была закрыта).`, "ok");
      }
      await refreshAll();
    } catch (e) {
      if (e.message === "auth") logout();
      else showToast(formatApiError(e.message), "error");
    } finally {
      if (btn.isConnected) {
        btn.disabled = false;
        btn.textContent = label;
      }
    }
  });
  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    if (!tradeBoardVisible()) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => renderTradeChartsBoard({ repaint: false }), 200);
  });
}

function renderTradesInto(el, bot, trades) {
  if (!el) return;
  const open = trades || [];
  const summaryId =
    el.id === "test-strategy-trades"
      ? "test-strategy-trades-summary"
      : el.id === "htf1h-trades"
        ? "htf1h-trades-summary"
      : el.id === "strategy-trades"
        ? "strategy-trades-summary"
        : el.id === "grid-trades"
          ? "grid-trades-summary"
          : el.id === "finder-trades"
            ? "finder-trades-summary"
            : el.id === "llm-trades"
              ? "llm-trades-summary"
              : el.id === "pa-trades"
                ? "pa-trades-summary"
                : null;
  if (summaryId) updateTradesSectionSummary(summaryId, open);

  const tradeSig = tradeListSignature(bot, open);
  if (el.dataset.tradeSig === tradeSig) return;
  el.dataset.tradeSig = tradeSig;

  if (!open.length) {
    el.innerHTML = '<p class="empty">Нет открытых сделок</p>';
    return;
  }

  // Drop expand pins for trades that are no longer open in this list.
  const openIds = new Set(open.map((t) => String(t.trade_id)));
  for (const key of [...expandedTradeIds]) {
    if (!key.startsWith(`${bot}:`)) continue;
    const id = key.slice(bot.length + 1);
    if (!openIds.has(id)) expandedTradeIds.delete(key);
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
      const openAt = t.open_date ? fmtAppDateShort(t.open_date) : "—";
      const expanded = isTradeExpanded(bot, t.trade_id);
      const expandCls = expanded ? "is-expanded" : "is-collapsed";
      return `<article class="trade-card ${expandCls}" data-trade-id="${t.trade_id}">
        <div class="trade-summary">
          <button type="button" class="trade-toggle" aria-expanded="${expanded ? "true" : "false"}" aria-label="${expanded ? "Свернуть сделку" : "Развернуть сделку"}">
            <div class="trade-summary-main">
              <strong class="trade-pair">${t.pair}</strong>
              <span class="trade-meta">${openAt} · ${t.leverage || 1}x · ${tag} · маржа ${fmtUsd(t.stake_amount)}</span>
            </div>
            <div class="trade-head-right">
              <span class="trade-pnl ${pnlClass(t.profit_abs)}">${fmtPct(pnl)}</span>
              <span class="trade-rate">${fmtUsd(t.profit_abs, 4)}</span>
            </div>
            <span class="trade-chevron" aria-hidden="true">▸</span>
          </button>
          ${
            settingsReadonly || demoMirrorAdmin
              ? ""
              : `<button type="button" class="btn danger btn-sm" data-bot="${bot}" data-id="${t.trade_id}" data-act="forceexit">Закрыть</button>`
          }
        </div>
        <div class="trade-details">
          <div class="trade-grid">${rows}</div>
        </div>
      </article>`;
    })
    .join("");
}

function renderTrades(bot, trades) {
  renderTradesInto($(BOTS[bot].tradesEl), bot, trades);
}

function bindTradeListEl(el) {
  if (!el || el.dataset.actionsBound) return;
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
      const tradeId = card.dataset.tradeId;
      const exitBtn = card.querySelector("[data-act=forceexit]");
      const tradeBot = exitBtn?.dataset.bot;
      if (tradeBot && tradeId) setTradeExpanded(tradeBot, tradeId, expanded);
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
        showToast(`Сделка #${tradeId} удалена из базы бота (на бирже уже была закрыта).`, "ok");
      }
      await refreshAll();
    } catch (e) {
      if (e.message === "auth") logout();
      else showToast(formatApiError(e.message), "error");
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  });
}

function bindTradeLists() {
  for (const bot of Object.keys(BOTS)) {
    bindTradeListEl($(BOTS[bot].tradesEl));
  }
  bindTradeListEl($("test-strategy-trades"));
  bindTradeListEl($("htf1h-trades"));
  bindTradeListEl($("llm-trades"));
  bindTradeListEl($("llm50-trades"));
  bindTradeListEl($("cursor-trades"));
  bindTradeListEl($("cursor50-trades"));
  bindTradeListEl($("pa-trades"));
}

function strategyActionTargets() {
  return [$("strategy-actions")].filter(Boolean);
}

function renderActions(bot, running) {
  const cfg = BOTS[bot];
  const targets =
    bot === "strategy" ? strategyActionTargets() : [$(cfg.actionsEl)].filter(Boolean);
  if (!targets.length) return;

  const fillAll = (html) => {
    for (const el of targets) el.innerHTML = html;
  };
  const bindAll = (handler) => {
    for (const el of targets) {
      el.querySelectorAll("button").forEach((btn) => {
        btn.addEventListener("click", handler);
      });
    }
  };

  if (bot === "strategy" && sharedStackMode) {
    const note = running
      ? "Торговля через общий Signal Engine + Executor (не отдельный бот пользователя)."
      : tradingFlags.strategy
        ? "Signal Engine или Executor недоступен — проверьте статус в Настройках."
        : "Автоторговля выключена. Включите в Настройках → «Strategy trading». Открытые сделки всё ещё мониторятся.";
    const adminScan = isAdminUser() && !isImpersonating
      ? `<button class="btn primary" data-bot="strategy" data-act="scan-strategy" title="Скан пар (отчёт) — whitelist в DB не затирается">Запустить скан</button>`
      : "";
    fillAll(`<p class="muted finder-disabled-note">${note}</p>${adminScan}`);
    if (adminScan) {
      bindAll(async (ev) => {
        const btn = ev.currentTarget;
        if (btn.dataset.act !== "scan-strategy") return;
        await runStrategyScanAction(btn);
      });
    }
    return;
  }
  const slotsDisabled = Number(cfg.maxTrades) <= 0;
  if (slotsDisabled) {
    const slotsNote =
      bot === "grid"
        ? "Боковиков = 0 — бот остановлен. Укажите, сколько каналов держать."
        : "Макс. сделок = 0 — бот остановлен и не торгует. Установите 1 или больше на карточке бота.";
    fillAll(`
      <p class="muted finder-disabled-note">${slotsNote}</p>
      <button class="btn" data-bot="${bot}" data-act="stop" ${!running ? "disabled" : ""}>Стоп</button>
      <button class="btn" data-bot="${bot}" data-act="reload">Reload config</button>
    `);
    bindAll(async (ev) => {
      const btn = ev.currentTarget;
      const act = btn.dataset.act;
      try {
        if (act === "stop") {
          if (!confirm(`Остановить бота «${BOTS[bot].label}»?`)) return;
          await setBotEnabled(bot, false);
        }
        if (act === "reload") await api(bot, "/reload_config", "POST");
        await refreshAll();
      } catch (e) {
        if (e.message === "auth") logout();
        else showToast(formatApiError(e.message), "error");
      }
    });
    return;
  }
  const scanBtn =
    bot === "grid"
      ? `<button class="btn" data-bot="grid" data-act="scan-ranging" title="Поиск боковика среди 150 ликвидных пар">Скан боковика</button>`
      : bot === "strategy"
        ? `<button class="btn" data-bot="strategy" data-act="scan-strategy" title="Подбор пар под каждую включённую стратегию → Signal Engine">Запустить скан</button>`
        : "";
  if (settingsReadonly || demoMirrorAdmin) {
    fillAll(`<span class="muted">Demo: просмотр сделок администратора</span>`);
    return;
  }
  const startLabel = bot === "grid" ? "Старт · Demo" : "Старт";
  fillAll(`
    <button class="btn primary" data-bot="${bot}" data-act="start" ${running ? "disabled" : ""}>${startLabel}</button>
    <button class="btn" data-bot="${bot}" data-act="stop" ${!running ? "disabled" : ""}>Стоп</button>
    <button class="btn" data-bot="${bot}" data-act="reload">Reload config</button>
    ${scanBtn}
  `);
  bindAll(async (ev) => {
    const btn = ev.currentTarget;
    const act = btn.dataset.act;
    const label = btn.textContent;
    try {
      if (act === "start") {
        if (Number(BOTS[bot].maxTrades) <= 0) {
          showToast(
            bot === "grid"
              ? "Боковиков = 0. Укажите, сколько каналов держать."
              : "Макс. сделок = 0. Установите 1 или больше в Настройках.",
            "warn"
          );
          return;
        }
        btn.disabled = true;
        await setBotEnabled(bot, true);
        if (bot === "grid") showToast("Grid торгует на Demo-счёте администратора", "info");
      }
      if (act === "stop") {
        if (!confirm(`Остановить бота «${BOTS[bot].label}»? Новые сделки не будут открываться.`)) return;
        btn.disabled = true;
        await setBotEnabled(bot, false);
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
        await runStrategyScanAction(btn);
        return;
      } else {
        refreshAll();
        return;
      }
      await refreshAll();
      if (bot === "grid") await loadGridScanInfo();
      if (bot === "strategy") await loadStrategyScanInfo();
    } catch (e) {
      if (e.message === "auth") logout();
      else showToast(formatApiError(e.message), "error");
      try {
        const all = await pairConfigApi("/bots");
        if (all?.bots) applyBotControlSnapshot(all.bots);
      } catch {
        /* ignore */
      }
      if (bot === "grid") await loadGridScanInfo();
      if (bot === "strategy") await loadStrategyScanInfo();
    }
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
    let text;
    if (d.running) {
      text = "Скан выполняется…";
    } else if (!d.scanned_at) {
      text = "Скан боковика ещё не запускался";
    } else {
      const t = fmtAppDateShort(d.scanned_at);
      const n = d.selected_count ?? d.ranging_found ?? 0;
      const checked = d.candidates_checked ?? "?";
      text = `Боковик: ${n} пар (из ${checked} проверенных) · ${t}`;
    }
    gridScanInfoText = text;
    el.textContent = text;
  } catch {
    if (gridScanInfoText) el.textContent = gridScanInfoText;
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

async function runStrategyScanAction(btn) {
  if (!isAdminUser() || isImpersonating) {
    showToast("Скан стратегий доступен только админу.", "warn");
    return;
  }
  if (
    !confirm(
      "Запустить скан пар по включённым стратегиям?\n" +
        "Только отчёт — текущий большой whitelist НЕ затирается.\n" +
        "~2–3 мин."
    )
  ) {
    return;
  }
  const label = "Запустить скан";
  const allBtns = Array.from(document.querySelectorAll(".js-strategy-run-scan"));
  for (const b of allBtns) {
    b.disabled = true;
    b.textContent = "Скан…";
  }
  for (const id of ["strategy-scan-info"]) {
    const info = $(id);
    if (info) info.textContent = "Скан выполняется…";
  }
  try {
    if (!tradingFlags.strategy) {
      try {
        await pairConfigApi("/trading-enabled", "POST", { strategy: true });
        tradingFlags.strategy = true;
      } catch {
        /* non-fatal */
      }
    }
    // apply:false — whitelist живёт в DB; скан не должен его схлопывать.
    const res = await pairConfigApi("/strategy-scan", "POST", { apply: false });
    const n = res?.selected_count ?? res?.summary?.found ?? "?";
    const apply = res?.apply || {};
    const applyBit = apply.applied
      ? `whitelist→SE ${apply.pairs || n}`
      : apply.reason || "whitelist сохранён (DB)";
    for (const id of ["strategy-scan-info"]) {
      const info = $(id);
      if (info) {
        info.textContent = `Скан готов: ${n} пар · ${applyBit}`;
      }
    }
    await refreshBot("strategy");
    await loadStrategyScanInfo();
    await refreshAll();
  } catch (e) {
    if (e.message === "auth") logout();
    else showToast(formatApiError(e.message), "error");
    await loadStrategyScanInfo();
  } finally {
    for (const b of allBtns) {
      b.disabled = false;
      b.textContent = label;
    }
  }
}

function bindStrategyRunScanButton() {
  document.querySelectorAll(".js-strategy-run-scan").forEach((btn) => {
    if (btn.dataset.bound === "1") return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => runStrategyScanAction(btn));
  });
}

/** Admin-only scan CTA inside Тестовые (settings body + panel head). */
function paintTestStrategyScanActions() {
  const el = $("test-strategy-scan-actions");
  const headBtn = $("test-strategy-run-scan");
  const show = isAdminUser() && !isImpersonating;
  if (headBtn) headBtn.classList.toggle("hidden", !show);
  if (!el) {
    bindStrategyRunScanButton();
    return;
  }
  if (!show) {
    el.innerHTML = "";
    bindStrategyRunScanButton();
    return;
  }
  el.innerHTML = `
    <button
      type="button"
      class="btn primary js-strategy-run-scan"
      title="Скан пар по включённым стратегиям → whitelist Signal Engine → reload"
    >Запустить скан</button>
    <span class="muted" style="align-self:center">Подбор пар → Signal Engine → открытие сделок через executor/ML gate</span>
  `;
  // Re-bind: innerHTML wiped previous listeners
  el.querySelectorAll(".js-strategy-run-scan").forEach((btn) => {
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => runStrategyScanAction(btn));
  });
  bindStrategyRunScanButton();
}

function strategyScanCount(raw) {
  if (raw == null || raw === "") return null;
  if (typeof raw === "number" && Number.isFinite(raw)) return raw;
  if (typeof raw === "string" && raw.trim() !== "" && Number.isFinite(Number(raw))) return Number(raw);
  if (Array.isArray(raw)) return raw.length;
  if (typeof raw === "object") {
    if (Array.isArray(raw.selected)) return raw.selected.length;
    if (typeof raw.selected === "number") return raw.selected;
    if (typeof raw.found === "number") return raw.found;
    if (typeof raw.count === "number") return raw.count;
  }
  return null;
}

async function loadStrategyScanInfo() {
  const els = ["strategy-scan-info"]
    .map((id) => $(id))
    .filter(Boolean);
  if (!els.length) return;
  const setText = (text) => {
    strategyScanInfoText = text;
    for (const el of els) el.textContent = text;
  };
  if (isAllVolumePairlist()) {
    setText("Пары: top-200 USDT futures по объёму · whitelist отключён · сканер на паузе");
    return;
  }
  try {
    const d = await pairConfigApi("/strategy-scan");
    if (d.running) {
      setText("Скан выполняется…");
      return;
    }
    if (!d.scanned_at) {
      setText("Скан пар ещё не запускался");
      return;
    }
    const t = fmtAppDateShort(d.scanned_at);
    const n = d.selected_count ?? 0;
    const checked = d.candidates_checked ?? "?";
    const by = d.by_strategy || {};
    const parts = Object.entries(by)
      .slice(0, 4)
      .map(([sid, raw]) => {
        const name = STRATEGY_SCAN_SHORT[sid] || String(sid).replace(/Strategy$/, "");
        const count = strategyScanCount(raw);
        return count == null ? name : `${name}: ${count}`;
      })
      .join(" · ");
    setText(
      parts
        ? `Скан пар: ${n} (из ${checked}) · ${parts} · ${t}`
        : `Скан пар: ${n} (из ${checked}) · ${t}`
    );
  } catch {
    if (strategyScanInfoText) {
      for (const el of els) el.textContent = strategyScanInfoText;
    }
  }
}

function setState(bot, running, hint = "") {
  const els =
    bot === "strategy"
      ? [$(BOTS[bot].stateEl), $("test-strategy-state")].filter(Boolean)
      : [$(BOTS[bot].stateEl)].filter(Boolean);
  const apply = (text, className, title) => {
    for (const el of els) {
      el.textContent = text;
      el.className = className;
      el.title = title;
    }
  };
  if (hint === "offline") {
    apply("OFFLINE", "badge stopped", "Нет связи с API бота — перезапуск или перегрузка");
    return;
  }
  if (hint === "sync") {
    apply("SYNC…", "badge running", "Бот перезагружается или обновляет конфиг — подождите");
    return;
  }
  // Control-plane derived statuses (RU titles, EN badge codes)
  const ctrl = typeof hint === "string" ? hint.toUpperCase() : "";
  // Live ctbot state during whitelist/scan reload — not a cold start.
  if (ctrl === "RELOAD_CONFIG" || ctrl === "RELOADING") {
    apply("SYNC…", "badge running", "Обновление конфига (скан/whitelist) — подождите несколько секунд");
    return;
  }
  if (ctrl === "STARTING") {
    apply("STARTING…", "badge running", "Запуск торговли…");
    return;
  }
  if (ctrl === "STOPPING") {
    apply("STOPPING…", "badge stopped", "Остановка новых входов…");
    return;
  }
  if (ctrl === "ERROR") {
    apply("ERROR", "badge stopped", botsControl[bot]?.observed?.last_error || "Ошибка запуска");
    return;
  }
  if (ctrl === "OFF") {
    apply("STOPPED", "badge stopped", "Бот остановлен — нажмите «Старт»");
    return;
  }
  if (ctrl === "RUNNING") {
    running = true;
  }
  const title =
    bot === "strategy" && sharedStackMode
      ? running
        ? "Signal Engine + Executor · автоторговля включена"
        : hint === "offline"
          ? "Signal Engine или Executor не работает — см. Настройки"
          : "Автоторговля выключена — Настройки → переключатель Strategy"
      : running
        ? bot === "finder"
          ? `ML Finder · scanner + pnl gate ${ML_GATE_STRATEGY}`
          : bot === "strategy"
            ? `Бот торгует · ML gate ${ML_GATE_STRATEGY}`
            : bot === "grid"
              ? `Бот торгует · ML gate ${ML_GATE_STRATEGY}`
              : "Бот торгует"
        : "Бот остановлен — нажмите «Старт»";
  apply(
    running ? "RUNNING" : "STOPPED",
    `badge ${running ? "running" : "stopped"}`,
    title
  );
}

function rememberBotLive(bot, patch) {
  botLiveCache[bot] = { ...(botLiveCache[bot] || {}), ...patch, ts: Date.now() };
}

/** Sqlite net closed PnL wins. Signal-engine /profit has no fills on the shared stack. */
function mergeClosedProfit(liveProfit, dbProfit) {
  const dbCount = Number(dbProfit?.trade_count ?? 0);
  const dbCoin = dbProfit?.profit_closed_coin;
  const hasDb =
    dbProfit != null &&
    (dbCount > 0 || (dbCoin != null && Number.isFinite(Number(dbCoin)) && Number(dbCoin) !== 0));
  if (hasDb) {
    return {
      ...(liveProfit || {}),
      profit_closed_coin: dbProfit.profit_closed_coin,
      profit_closed_fiat: dbProfit.profit_closed_fiat ?? dbProfit.profit_closed_coin,
      profit_closed_main_coin: dbProfit.profit_closed_main_coin,
      profit_closed_test_coin: dbProfit.profit_closed_test_coin,
      profit_closed_htf_coin: dbProfit.profit_closed_htf_coin,
      trade_count: dbProfit.trade_count,
      trade_count_main: dbProfit.trade_count_main,
      trade_count_test: dbProfit.trade_count_test,
      trade_count_htf: dbProfit.trade_count_htf,
      closed_profit_source: "db",
    };
  }
  return liveProfit;
}

function paintBotLive(bot, { config, profit, balance, trades, running, stateHint = "" } = {}) {
  const openTrades = Array.isArray(trades) ? trades : [];
  const ctrl = controlStatusOf(bot);
  const isRunning =
    ctrl
      ? ctrl === "RUNNING" || ctrl === "STARTING"
      : running != null
        ? !!running
        : String(config?.state || "").toLowerCase() === "running";
  const hint = stateHint || ctrl || "";
  if (hint) setState(bot, isRunning, hint);
  else setState(bot, isRunning);

  if (bot === "strategy") {
    const { main, test, htf } = splitStrategyTrades(openTrades);
    renderStatsInto($(BOTS.strategy.statsEl), bot, profit, balance, main.length, config?.stake_amount, {
      openLabel: "Открыто (основные)",
    });
    renderTestStrategyStats($("test-strategy-stats"), profit, balance, test.length);
    renderHtfStrategyStats($("htf1h-stats"), profit, (htf || []).length, balance);
    renderActions(bot, isRunning);
    renderTradesInto($(BOTS.strategy.tradesEl), bot, main);
    renderTradesInto($("test-strategy-trades"), bot, test);
    renderTradesInto($("htf1h-trades"), bot, htf || []);
    const htfBadge = $("htf1h-state");
    const stratBadge = $("strategy-state");
    if (htfBadge && stratBadge) {
      htfBadge.className = stratBadge.className;
      htfBadge.textContent = stratBadge.textContent;
      htfBadge.title = stratBadge.title || "";
    }
    const htfSummary = $("htf1h-trades-summary");
    if (htfSummary) htfSummary.textContent = (htf || []).length ? `${htf.length} открыто` : "нет";
  } else {
    renderStats(bot, profit, balance, openTrades.length, config?.stake_amount);
    renderActions(bot, isRunning);
    renderTrades(bot, openTrades);
  }
  return openTrades;
}

function paintBotFromCache(bot, stateHint = "") {
  const snap = botLiveCache[bot];
  if (!snap?.config && !snap?.trades && snap?.profit == null && snap?.balance == null) {
    return false;
  }
  paintBotLive(bot, {
    config: snap.config,
    profit: snap.profit,
    balance: snap.balance,
    trades: snap.trades || [],
    running: snap.running,
    stateHint,
  });
  if (whitelistCache[bot]?.data) renderWhitelist(bot, whitelistCache[bot].data);
  return true;
}

async function refreshBot(bot) {
  // Paint last-known immediately, then refresh from pair-config cache/db (fast),
  // then optionally live ctbot APIs in the background (may be slow/wedged).
  const prev = botLiveCache[bot] || {};
  if (Array.isArray(prev.trades)) {
    paintBotLive(bot, {
      config: prev.config,
      profit: prev.profit,
      balance: prev.balance,
      trades: prev.trades,
      running: prev.running,
    });
  }

  let openTrades = Array.isArray(prev.trades) ? prev.trades : [];
  try {
    const bundle = await fetchOpenTradesCache();
    const entry = bundle?.bots?.[bot];
    if (entry && Array.isArray(entry.trades)) {
      openTrades = withPreservedMlMeta(bot, entry.trades);
      if (entry.closed_profit) {
        rememberBotLive(bot, { closedProfitDb: entry.closed_profit });
      }
      const profit = mergeClosedProfit(
        prev.profit,
        entry.closed_profit || botLiveCache[bot]?.closedProfitDb
      );
      paintBotLive(bot, {
        config: prev.config,
        profit,
        balance: prev.balance,
        trades: openTrades,
        running: prev.running,
      });
      rememberBotLive(bot, { trades: openTrades, profit });
    }
  } catch {
    /* keep prev */
  }

  if (bot === "strategy" && sharedStackMode) {
    const running = strategyRunningInSharedMode();
    const hint = sharedStrategyStateHint();
    paintBotLive(bot, {
      config: prev.config,
      profit: botLiveCache[bot]?.profit ?? prev.profit,
      balance: prev.balance,
      trades: openTrades,
      running,
      stateHint: hint,
    });
    rememberBotLive(bot, { trades: openTrades, running });

    // Shared mode must not replace open trades with signal-engine /status (empty),
    // but still pull balance/profit/config/whitelist from the live API.
    void (async () => {
      let config = prev.config || null;
      let profit = prev.profit ?? null;
      let balance = prev.balance ?? null;
      try {
        const cfg = await api(bot, "/show_config").catch(() => null);
        if (cfg) {
          config = cfg;
          // Shared signal-engine config keeps max_open_trades=1 (no local fills).
          // Real slot limit lives in bot_limits / config_strategy — never overwrite UI from SE.
          rememberBotLive(bot, { config });
        }
      } catch {
        /* keep cache */
      }
      const paintShared = () => {
        paintBotLive(bot, {
          config: botLiveCache[bot]?.config || config,
          profit: botLiveCache[bot]?.profit ?? profit,
          balance: botLiveCache[bot]?.balance ?? balance,
          trades: botLiveCache[bot]?.trades || openTrades,
          running: strategyRunningInSharedMode(),
          stateHint: sharedStrategyStateHint(),
        });
      };
      const now = Date.now();
      const needWhitelist =
        !whitelistCache[bot] || now - whitelistCache[bot].ts >= WHITELIST_REFRESH_MS;
      await Promise.all([
        api(bot, "/profit")
          .then((v) => {
            profit = mergeClosedProfit(v, botLiveCache[bot]?.closedProfitDb);
            rememberBotLive(bot, { profit });
            paintShared();
          })
          .catch(() => {
            profit = mergeClosedProfit(profit, botLiveCache[bot]?.closedProfitDb);
            if (profit) {
              rememberBotLive(bot, { profit });
              paintShared();
            }
          }),
        api(bot, "/balance")
          .then((v) => {
            balance = v;
            rememberBotLive(bot, { balance });
            paintShared();
          })
          .catch(() => {}),
        needWhitelist
          ? api(bot, "/whitelist")
              .then((w) => {
                whitelistCache[bot] = { data: w, ts: Date.now() };
                renderWhitelist(bot, w);
              })
              .catch(() => {})
          : Promise.resolve(),
      ]);
    })();
    return openTrades;
  }

  // Live enrichment — never blocks the caller.
  void (async () => {
    let config = prev.config || null;
    let profit = prev.profit ?? null;
    let balance = prev.balance ?? null;
    let trades = openTrades;
    try {
      const [cfg, status] = await Promise.all([
        api(bot, "/show_config").catch(() => null),
        api(bot, "/status").catch(() => null),
      ]);
      if (cfg) {
        config = cfg;
        // Status badge comes from control-plane; do not overwrite from show_config.state
        if (!controlStatusOf(bot) && !(bot === "strategy" && sharedStackMode)) {
          setState(bot, String(cfg?.state || "").toLowerCase() === "running");
        }
        // Never take max_open_trades from live show_config — SE/Finder often lag or
        // stay at 1. Source of truth is pair-config bot_limits (/pairs).
      }
      if (Array.isArray(status)) trades = withPreservedMlMeta(bot, status);
    } catch {
      /* keep cache */
    }

    const ctrl = controlStatusOf(bot);
    const running =
      ctrl
        ? ctrl === "RUNNING" || ctrl === "STARTING"
        : bot === "strategy" && sharedStackMode
          ? strategyRunningInSharedMode()
          : config != null
            ? String(config?.state || "").toLowerCase() === "running"
            : prev.running;
    const stateHint = ctrl || (bot === "strategy" && sharedStackMode ? sharedStrategyStateHint() : "");
    paintBotLive(bot, { config, profit, balance, trades, running, stateHint });
    rememberBotLive(bot, { config, profit, balance, trades, running });

    if (trades.length && tradesNeedMlEnrich(trades)) {
      try {
        const enriched = await enrichTradesWithMl(bot, trades);
        if (enriched?.length) {
          trades = enriched;
          paintBotLive(bot, { config, profit, balance, trades, running });
          rememberBotLive(bot, { trades });
        }
      } catch {
        /* ignore */
      }
    }

    const now = Date.now();
    const needWhitelist = !whitelistCache[bot] || now - whitelistCache[bot].ts >= WHITELIST_REFRESH_MS;
    Promise.all([
      api(bot, "/profit")
        .then((v) => {
          profit = mergeClosedProfit(v, botLiveCache[bot]?.closedProfitDb);
          rememberBotLive(bot, { profit });
          // Use latest cached trades (may already include ML) — never wipe badges.
          const latest = botLiveCache[bot]?.trades || trades;
          paintBotLive(bot, { config, profit, balance, trades: latest, running });
        })
        .catch(() => {
          profit = mergeClosedProfit(profit, botLiveCache[bot]?.closedProfitDb);
          if (profit) {
            rememberBotLive(bot, { profit });
            const latest = botLiveCache[bot]?.trades || trades;
            paintBotLive(bot, { config, profit, balance, trades: latest, running });
          }
        }),
      api(bot, "/balance")
        .then((v) => {
          balance = v;
          rememberBotLive(bot, { balance });
          const latest = botLiveCache[bot]?.trades || trades;
          paintBotLive(bot, { config, profit, balance, trades: latest, running });
        })
        .catch(() => {}),
      (needWhitelist
        ? api(bot, "/whitelist")
            .then((w) => {
              whitelistCache[bot] = { data: w, ts: Date.now() };
              return w;
            })
            .catch(() => whitelistCache[bot]?.data)
        : Promise.resolve(whitelistCache[bot]?.data)
      ).then((w) => {
        if (w != null) renderWhitelist(bot, w);
        updateMaxTradesHint();
      }),
    ]).catch(() => {});
  })();

  return openTrades;
}

function totalMaxOpenSlots() {
  const testMax = Number(testSettings.max_open_trades) || 0;
  const fromBots = Object.values(BOTS).reduce((s, b) => {
    const n = Number(b.maxTrades || 0);
    if (b === BOTS.strategy) return s + Math.max(n, testMax);
    return s + n;
  }, 0);
  const fromServer = Number(openSummaryCache?.max_total);
  // Prefer pair-config sum (includes test-block ceiling) when available.
  if (Number.isFinite(fromServer) && fromServer > 0) return Math.max(fromServer, fromBots);
  return fromBots;
}

function renderTradesSummaryFromOpen(summary) {
  const el = $("trades-summary");
  if (!el || !summary) return;
  const adopted = adoptOpenSummary(summary);
  const count = Number(adopted.count) || 0;
  const maxTotal = Number(adopted.max_total);
  const maxLabel = Number.isFinite(maxTotal) && maxTotal > 0 ? maxTotal : totalMaxOpenSlots();
  if (!count) {
    el.classList.add("is-empty");
    el.innerHTML = `<span class="ts-label">Открытые сделки</span><strong>нет · 0 / ${maxLabel}</strong>`;
    return;
  }
  el.classList.remove("is-empty");
  const margin = Number(adopted.margin) || 0;
  const pnl = Number(adopted.pnl) || 0;
  const pnlClassName = pnlClass(pnl);
  el.innerHTML = `
    <span class="ts-label">Всего по сделкам</span>
    <strong>${count} / ${maxLabel}</strong>
    <span class="ts-row">Маржа <b>${fmtUsd(margin)}</b></span>
    <span class="ts-row">PnL <b class="${pnlClassName}">${pnl >= 0 ? "+" : ""}${Number(pnl).toFixed(2)} USDT</b></span>
  `;
}

function renderTradesSummary(allTrades) {
  const el = $("trades-summary");
  if (!el) return;

  // Prefer server warm summary when live bot poll is still empty (e.g. finder retries).
  if ((!allTrades || !allTrades.length) && openSummaryCache && Number(openSummaryCache.count) > 0) {
    renderTradesSummaryFromOpen(openSummaryCache);
    return;
  }

  const maxTotal = totalMaxOpenSlots();
  const count = allTrades.length;

  if (!count) {
    // Do not flash empty over a recent sticky summary.
    if (openSummarySticky && Number(openSummarySticky.count) > 0) {
      const age = Date.now() - Number(openSummarySticky._ts || 0);
      if (age < OPEN_SUMMARY_STICKY_MS) {
        renderTradesSummaryFromOpen(openSummarySticky);
        return;
      }
    }
    el.classList.add("is-empty");
    el.innerHTML = `<span class="ts-label">Открытые сделки</span><strong>нет · 0 / ${maxTotal}</strong>`;
    adoptOpenSummary({ count: 0, max_total: maxTotal, margin: 0, pnl: 0 });
    return;
  }

  el.classList.remove("is-empty");
  const margin = allTrades.reduce((s, t) => s + Number(t.stake_amount || 0), 0);
  const pnl = allTrades.reduce(
    (s, t) => s + Number(t.total_profit_abs ?? t.profit_abs ?? 0),
    0
  );
  const pnlClassName = pnlClass(pnl);

  adoptOpenSummary({
    count,
    max_total: Number(openSummaryCache?.max_total) > 0 ? Number(openSummaryCache.max_total) : maxTotal,
    margin,
    pnl,
  });

  el.innerHTML = `
    <span class="ts-label">Всего по сделкам</span>
    <strong>${count} / ${openSummaryCache.max_total}</strong>
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
    const mode = adaptive.mode || adaptive.settings?.mode || "adaptive";
    const stepCount = (adaptive.intervals_min || []).length || 4;
    const bots = s.bots || {};
    const slotBit = (key, label) => {
      const b = bots[key];
      if (!b) return null;
      return `${label} ${Number(b.open) || 0}/${Number(b.max) || 0}`;
    };
    // signal-engine max_open_trades is forced to 1 and holds no user trades.
    const slotParts = [];
    if (slotParts.length) adaptiveLines.push(`Слоты: ${slotParts.join(" · ")}`);
    if (s.full) {
      adaptiveLines.push("Скан пар: слоты заполнены (Finder ~30 мин)");
    } else if (mode === "fixed") {
      adaptiveLines.push(`Скан пар: каждые ${adaptive.current_interval_min} мин (фикс.)`);
    } else {
      adaptiveLines.push(
        `Скан пар: каждые ${adaptive.current_interval_min} мин (шаг ${(adaptive.step || 0) + 1}/${stepCount})`,
      );
    }
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
    serverStatusCache = { stats, adaptive };
    renderServerStatus(stats, adaptive);
  } catch (e) {
    if (e.message === "auth") throw e;
    // Keep last CPU/RAM line instead of flashing "Сервер —".
    if (serverStatusCache) {
      renderServerStatus(serverStatusCache.stats, serverStatusCache.adaptive);
      return;
    }
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
        const fixed = result.fixed || [];
        const archived = fixed.filter((x) => x.action === "archived").length;
        const exited = fixed.filter((x) => x.action === "forceexit").length;
        const deduped = fixed.filter((x) => x.action === "archive_duplicate").length;
        const after = result.after || {};
        showToast(
          `Синхронизация: закрыто через биржу ${exited}, призраков в историю ${archived}, ` +
            `дублей убрано ${deduped}. ` +
            `Сейчас: боты ${after.ft_open_count ?? "—"} / биржа ${after.bybit_position_count ?? "—"}. ` +
            (result.ok ? "Расхождений больше нет." : "Часть расхождений осталась — обновите страницу."),
          result.ok ? "ok" : "warn"
        );
        await refreshAll();
      } catch (e) {
        showToast(formatApiError(e.message), "error");
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
  // No retries — open trades come from pair-config cache/db; live ctbot may be wedged.
  try {
    return await refreshBot(bot);
  } catch (e) {
    if (e.message === "auth") throw e;
    if (bot === "strategy" && sharedStackMode) {
      paintSharedStrategyState();
      return botLiveCache[bot]?.trades || [];
    }
    if (paintBotFromCache(bot)) {
      console.warn(`refresh ${bot} (kept last):`, e.message);
      return botLiveCache[bot]?.trades || [];
    }
    const cfg = BOTS[bot];
    setState(bot, false, "offline");
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
  const d = parseAppInstant(iso);
  if (!d) return String(iso);
  return fmtAppDateShort(d);
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
    <input id="bg-cfg-max-bots" type="number" class="numeric-setting-input" min="0" max="${BYBIT_GRID_MAX_LIMIT}" step="1" value="${maxBots}" inputmode="numeric" aria-label="Макс. активных Bybit Grid ботов" />
  `;
}

function renderBybitGridSettings(data) {
  const el = $("bybitgrid-settings");
  if (!el) return;
  const cfg = data?.config || {};
  const defs = cfg.defaults || {};
  const maxBots = cfg.max_active_bots != null ? Number(cfg.max_active_bots) : 1;
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
    if (maxBotsVal < 0 || maxBotsVal > BYBIT_GRID_MAX_LIMIT) {
      showToast(`Макс. активных ботов — от 0 до ${BYBIT_GRID_MAX_LIMIT} (0 = не создавать новые)`, "warn");
      return;
    }
    if (investVal < 5) {
      showToast("Минимальная инвестиция — 5 USDT", "warn");
      return;
    }
    if (tpVal <= 0 || slVal <= 0) {
      showToast("TP и SL должны быть больше 0", "warn");
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
      showToast(formatApiError(e.message), "error");
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
    const t = fmtAppDateShort(d.scanned_at);
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
      <div class="stat"><span class="muted">Лимит</span><strong>${maxBots}</strong></div>
    `;
    $("bybitgrid-balance-body").innerHTML = `
      <div class="stats">
        <div class="stat">PnL активных сеток<strong class="${pnlCls}">${fmtUsdSigned(pnl)}</strong><span class="stat-hint">Реализованный + нереализованный</span></div>
        <div class="stat">Баланс Funding<strong class="${funding.ok === false ? "loss" : ""}">${funding.fund_usdt != null ? fmtUsd(funding.fund_usdt) : "—"}</strong><span class="stat-hint">Unified ${funding.unified_usdt != null ? fmtUsd(funding.unified_usdt) : "—"}</span></div>
      </div>
    `;
    $("bybitgrid-balance-summary").textContent = `PnL активных ${fmtUsdSigned(pnl)}`;
    updateBotSettingsSummaries();

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
        showToast(formatApiError(e.message), "error");
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
          showToast(res.message || "Достигнут лимит активных grid", "warn");
        } else if (res.deployed === false) {
          const pair = res.params?.pair || res.scan?.best?.pair || "?";
          showToast(
            `Скан: ${pair} · Не удалось создать grid: ${formatApiError(res.error || "ошибка")}${res.hint ? ` · ${res.hint}` : ""}`,
            "error"
          );
        } else {
          const pair = res.params?.pair || res.scan?.best?.pair || "?";
          showToast(`Grid создан: ${pair} · ${bybitGridInvest} USDT`, "ok");
        }
        await refreshBybitGrid();
        await loadBybitGridScanInfo();
      } catch (e) {
        showToast(formatApiError(e.message), "error");
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
          showToast(msg.replace(/\n+/g, " · "), "info");
        }
        await loadBybitGridScanInfo();
      } catch (e) {
        showToast(formatApiError(e.message), "error");
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
    updateTradesSectionSummary(
      "bybitgrid-bots-summary",
      activeBots.map((b) => ({ pair: b.pair || b.symbol })),
      "нет ботов"
    );
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
            showToast(formatApiError(e.message), "error");
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
        ? "Биржевой grid: ордера на Bybit, не через Grid-бота"
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
  $("bybitgrid-modal")?.setAttribute("aria-hidden", "false");
  $("bybitgrid-form-msg")?.classList.add("hidden");
  if (!$("bg-pair").value) $("bg-pair").value = "SOL/USDT:USDT";
}

function closeBybitGridModal() {
  $("bybitgrid-modal")?.classList.add("hidden");
  $("bybitgrid-modal")?.setAttribute("aria-hidden", "true");
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

async function fetchOpenTradesCache({ refresh = false } = {}) {
  const qs = refresh ? "?refresh=1" : "";
  return pairConfigApi(`/open-trades${qs}`);
}

function paintOpenTradesFromServerBundle(bundle) {
  if (!bundle?.bots) return [];
  const all = [];
  for (const bot of Object.keys(BOTS)) {
    const entry = bundle.bots[bot];
    if (!entry || !Array.isArray(entry.trades)) continue;
    const trades = withPreservedMlMeta(bot, entry.trades);
    const prev = botLiveCache[bot] || {};
    if (entry.closed_profit) {
      rememberBotLive(bot, { closedProfitDb: entry.closed_profit });
    }
    const profit = mergeClosedProfit(prev.profit, entry.closed_profit || prev.closedProfitDb);
    paintBotLive(bot, {
      config: prev.config,
      profit,
      balance: prev.balance,
      trades,
      running: prev.running,
    });
    rememberBotLive(bot, { trades, profit });
    all.push(...trades);
    if (tradesNeedMlEnrich(trades)) {
      enrichTradesWithMl(bot, trades)
        .then((enriched) => {
          if (!enriched?.length) return;
          const cur = botLiveCache[bot] || {};
          paintBotLive(bot, {
            config: cur.config,
            profit: cur.profit,
            balance: cur.balance,
            trades: enriched,
            running: cur.running,
          });
          rememberBotLive(bot, { trades: enriched });
        })
        .catch(() => {});
    }
  }
  if (all.length || openSummaryCache) {
    renderTradesSummary(all);
  }
  return all;
}

function llmBase(pair) {
  const text = String(pair || "");
  return text.includes("/") ? text.split("/")[0] : text;
}

function llmCycleError(reason) {
  const text = String(reason || "");
  if (/authorization failed|invalid token/i.test(text)) return "Ошибка цикла: сессия DeepSeek истекла";
  if (/insufficient_user_quota|额度已用尽/i.test(text)) return "Ошибка цикла: бесплатная квота модели закончилась";
  const http = text.match(/LLM HTTP (\d+)/);
  if (http) return `Ошибка цикла: шлюз ответил ${http[1]}`;
  return text ? `Ошибка цикла: ${text.slice(0, 140)}` : "Ошибка цикла";
}

function llmDecisionText(view) {
  if (!view) return "—";
  if (!view.openseek_up) return "DeepSeek не отвечает";
  const st = view.status || {};
  const rows = Array.isArray(st.decisions) ? st.decisions : [];
  const hits = rows.filter((row) => row.side === "long" || row.side === "short");
  if (st.state === "idle") return "Остановлен — модель не вызывается";
  if (st.state === "wait") return "Ждёт ответ DeepSeek";
  if (st.state === "error") return llmCycleError(st.reason);
  if (hits.length) {
    return hits
      .map((row) => {
        const conf = Number(row.confidence);
        const pct = Number.isFinite(conf) ? ` ${(conf * 100).toFixed(0)}%` : "";
        const side = row.side === "short" ? "SHORT" : "LONG";
        return `${llmBase(row.pair)} ${side}${pct}`;
      })
      .join(" · ");
  }
  if (st.state === "flat" || st.state === "signal") {
    if (st.reason) return String(st.reason);
    const n = rows.length || (Array.isArray(view.pairs) ? view.pairs.length : 0);
    return n ? `HOLD по ${n} парам` : "HOLD";
  }
  if (!view.enabled) return "Нажмите Старт, чтобы бот начал запрашивать DeepSeek";
  return "Ожидание первого цикла";
}

let llmPairs = [];

function closeLlmTickersPanel() {
  const wrap = $("llm-tickers");
  const panel = $("llm-tickers-panel");
  const btn = $("llm-tickers-btn");
  if (panel) {
    panel.hidden = true;
    resetStrategyTickersPanelPosition(panel);
  }
  if (btn) btn.setAttribute("aria-expanded", "false");
  wrap?.classList.remove("is-open");
  const cursorPanel = $("cursor-tickers-panel");
  const cursorBtn = $("cursor-tickers-btn");
  const cursorWrap = $("cursor-tickers");
  if (cursorPanel) {
    cursorPanel.hidden = true;
    resetStrategyTickersPanelPosition(cursorPanel);
  }
  if (cursorBtn) cursorBtn.setAttribute("aria-expanded", "false");
  cursorWrap?.classList.remove("is-open");
  closePaTickersPanel();
}

function renderLlmPairs(pairs) {
  const el = $("llm-pairs");
  if (!el) return;
  llmPairs = Array.isArray(pairs) ? pairs.slice() : [];
  const count = $("llm-tickers-count");
  if (count) count.textContent = String(llmPairs.length);
  const key = llmPairs.join("|");
  if (el.dataset.pairsKey === key) return;
  el.dataset.pairsKey = key;
  el.innerHTML = llmPairs.length
    ? llmPairs
        .map(
          (pair) => `
      <li class="strategy-tickers-item">
        <span class="strategy-tickers-pair" title="${escapeHtml(pair)}">${escapeHtml(pairBaseLabel(pair))}</span>
        <button type="button" class="strategy-tickers-remove" data-pair="${escapeHtml(pair)}" title="Убрать ${escapeHtml(pairBaseLabel(pair))}" ${settingsReadonly ? "disabled" : ""}>×</button>
      </li>`
        )
        .join("")
    : `<li class="strategy-tickers-empty muted">Нет тикеров — бот не торгует</li>`;
  const panel = $("llm-tickers-panel");
  const wrap = $("llm-tickers");
  if (panel && wrap && !panel.hidden) positionStrategyTickersPanel(wrap, panel);
}

function paintLlmBot(view) {
  const badge = $("llm-state");
  if (badge) {
    const on = !!view?.enabled;
    badge.textContent = on ? "RUNNING" : "STOPPED";
    badge.className = on ? "badge running" : "badge stopped";
  }
  const tp = $("llm-tp");
  const stake = $("llm-stake");
  const max = $("llm-max");
  const conf = $("llm-conf");
  const active = document.activeElement;
  const settings = view?.settings || {};
  if (tp && active !== tp) tp.value = String(settings.take_profit_pct ?? 3);
  if (stake && active !== stake) stake.value = String(settings.stake_amount ?? 5);
  if (max && active !== max) max.value = String(settings.max_open_trades ?? 1);
  if (conf && active !== conf) conf.value = String(settings.min_confidence_pct ?? 65);
  const locked = !!settingsReadonly;
  for (const el of [tp, stake, max, conf, $("llm-save"), $("llm-pair-add"), $("llm-pair-input")]) {
    if (el) el.disabled = locked;
  }
  const toggle = $("llm-toggle");
  if (toggle) {
    toggle.textContent = view?.enabled ? "Стоп" : "Старт";
    toggle.classList.toggle("danger", !!view?.enabled);
    toggle.classList.toggle("primary", !view?.enabled);
    toggle.disabled = locked;
  }
  const pairs = Array.isArray(settings.pairs) ? settings.pairs : view?.pairs || [];
  renderLlmPairs(pairs);
  const subtitle = $("llm-subtitle");
  if (subtitle) {
    subtitle.textContent = `вход от ${settings.min_confidence_pct ?? 65}% · стейк ${settings.stake_amount ?? 5} USDT · цикл 30 мин · стоп −2%`;
  }
  const decision = $("llm-decision");
  if (decision) decision.textContent = llmDecisionText(view);
  renderTradesInto($("llm-trades"), "strategy", view?.trades || []);
}

async function refreshLlmBot() {
  if (!userMayUseBlock("llm")) return;
  const view = await pairConfigApi("/llm");
  paintLlmBot(view);
}

async function saveLlmSettings(extra = {}) {
  const tp = Number($("llm-tp")?.value);
  const stake = Number($("llm-stake")?.value);
  const maxOpen = Number($("llm-max")?.value);
  const minConf = Number($("llm-conf")?.value);
  const body = {
    take_profit_pct: tp,
    stake_amount: stake,
    max_open_trades: maxOpen,
    min_confidence_pct: minConf,
    ...extra,
  };
  const view = await pairConfigApi("/llm", "POST", body);
  paintLlmBot(view);
  return view;
}

function showLlmMsg(text, isError = false) {
  const el = $("llm-msg");
  if (!el) return;
  el.textContent = text;
  el.className = `pair-msg ${isError ? "error" : "ok"}`;
  el.classList.remove("hidden");
}

function llm50DecisionText(view) {
  const actions = Array.isArray(view?.status?.actions) ? view.status.actions : [];
  const hits = actions.filter((row) => row.result === "signal" || row.result === "close");
  if (hits.length) {
    return hits
      .map((row) => {
        const base = llmBase(row.pair);
        if (row.result === "close") return `${base} CLOSE`;
        const side = row.action === "SELL" ? "SHORT" : "LONG";
        const stake = Number(row.stake_usdt);
        const usd = Number.isFinite(stake) ? ` ${stake}$` : "";
        return `${base} ${side}${usd}`;
      })
      .join(" · ");
  }
  if (!view?.enabled) return "Нажмите Старт — модель получит бюджет 50 USDT";
  if (view?.status?.state === "idle") return "Остановлен";
  if (view?.status?.state === "wait") return "Ждёт ответ DeepSeek";
  if (view?.status?.state === "error") return llmCycleError(view?.status?.reason);
  if (view?.status?.reason && !hits.length) return String(view.status.reason);
  return actions.length ? "Без новых входов" : "Ожидание первого цикла";
}

function paintLlm50(view) {
  const badge = $("llm50-state");
  if (badge) {
    const on = !!view?.enabled;
    badge.textContent = on ? "RUNNING" : "STOPPED";
    badge.className = on ? "badge running" : "badge stopped";
  }
  const stats = view?.stats || {};
  const free = $("llm50-free");
  const used = $("llm50-used");
  const pnl = $("llm50-pnl");
  const trades = $("llm50-count");
  if (free) free.textContent = `${stats.free_usdt ?? 50} $`;
  if (used) used.textContent = `${stats.used_usdt ?? 0} $`;
  if (pnl) {
    const value = Number(stats.realized_usdt ?? 0);
    pnl.textContent = `${value > 0 ? "+" : ""}${value.toFixed(2)} $`;
    pnl.className = value > 0 ? "pos" : value < 0 ? "neg" : "";
  }
  if (trades) {
    trades.textContent = `${stats.closed_count ?? 0} · ${stats.wins ?? 0}/${stats.losses ?? 0}`;
  }
  const subtitle = $("llm50-subtitle");
  if (subtitle) {
    const pairCount = Array.isArray(view?.pairs) ? view.pairs.length : 25;
    subtitle.textContent = `бюджет ${view?.budget_usdt ?? 50} USDT · в работе ${stats.used_usdt ?? 0} · ${pairCount} пар · плечо 3x · цикл 30 мин`;
  }
  const toggle = $("llm50-toggle");
  if (toggle) {
    toggle.textContent = view?.enabled ? "Стоп" : "Старт";
    toggle.classList.toggle("danger", !!view?.enabled);
    toggle.classList.toggle("primary", !view?.enabled);
    toggle.disabled = !!settingsReadonly;
  }
  const decision = $("llm50-decision");
  if (decision) decision.textContent = llm50DecisionText(view);
  renderTradesInto($("llm50-trades"), "strategy", view?.trades || []);
}

async function refreshLlm50() {
  if (!userMayUseBlock("llm")) return;
  const view = await pairConfigApi("/llm50");
  paintLlm50(view);
}

function showLlm50Msg(text, isError = false) {
  const el = $("llm50-msg");
  if (!el) return;
  el.textContent = text;
  el.className = `pair-msg ${isError ? "error" : "ok"}`;
  el.classList.remove("hidden");
}

function bindLlm50Controls() {
  $("llm50-toggle")?.addEventListener("click", async () => {
    const btn = $("llm50-toggle");
    if (!btn || btn.disabled) return;
    const enable = btn.textContent.trim() !== "Стоп";
    btn.disabled = true;
    try {
      const view = await pairConfigApi("/llm50", "POST", { enabled: enable });
      paintLlm50(view);
      showLlm50Msg(enable ? "Бот запущен, бюджет 50 USDT" : "Новые решения остановлены");
    } catch (e) {
      if (e.message === "auth") logout();
      else showLlm50Msg(formatApiError(e.message), true);
    } finally {
      btn.disabled = !!settingsReadonly;
    }
  });
}

function bindLlmControls() {
  $("llm-save")?.addEventListener("click", async () => {
    const btn = $("llm-save");
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    try {
      await saveLlmSettings();
      showLlmMsg("Сохранено");
    } catch (e) {
      if (e.message === "auth") logout();
      else showLlmMsg(formatApiError(e.message), true);
    } finally {
      btn.disabled = !!settingsReadonly;
    }
  });
  $("llm-toggle")?.addEventListener("click", async () => {
    const btn = $("llm-toggle");
    if (!btn || btn.disabled) return;
    const enable = btn.textContent.trim() !== "Стоп";
    btn.disabled = true;
    try {
      await saveLlmSettings({ enabled: enable });
      showLlmMsg(enable ? "Бот запущен" : "Новые входы остановлены");
    } catch (e) {
      if (e.message === "auth") logout();
      else showLlmMsg(formatApiError(e.message), true);
    } finally {
      btn.disabled = !!settingsReadonly;
    }
  });
  $("llm-tickers-btn")?.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    const wrap = $("llm-tickers");
    const panel = $("llm-tickers-panel");
    const btn = $("llm-tickers-btn");
    if (!wrap || !panel || !btn) return;
    const willOpen = panel.hidden;
    closeAllStrategyTickersPanels();
    closeAllMlConfidenceMenus();
    if (!willOpen) {
      closeLlmTickersPanel();
      return;
    }
    panel.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    wrap.classList.add("is-open");
    requestAnimationFrame(() => {
      positionStrategyTickersPanel(wrap, panel);
      const input = $("llm-pair-input");
      if (input && !input.disabled) input.focus();
    });
  });
  $("llm-pair-form")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = $("llm-pair-input");
    const raw = String(input?.value || "").trim();
    if (!raw || settingsReadonly) return;
    const btn = $("llm-pair-add");
    if (btn) btn.disabled = true;
    try {
      await saveLlmSettings({ pairs: [...llmPairs, raw] });
      if (input) input.value = "";
      showLlmMsg("Пара добавлена");
    } catch (e) {
      if (e.message === "auth") logout();
      else showLlmMsg(formatApiError(e.message), true);
    } finally {
      if (btn) btn.disabled = !!settingsReadonly;
    }
  });
  $("llm-pairs")?.addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".strategy-tickers-remove");
    if (!btn || btn.disabled) return;
    ev.preventDefault();
    ev.stopPropagation();
    const pair = btn.getAttribute("data-pair");
    if (!pair) return;
    btn.disabled = true;
    try {
      await saveLlmSettings({ pairs: llmPairs.filter((item) => item !== pair) });
      showLlmMsg(`${pairBaseLabel(pair)} убрана`);
    } catch (e) {
      if (e.message === "auth") logout();
      else showLlmMsg(formatApiError(e.message), true);
    }
  });
}


let cursorPairs = [];

function cursorDownText(view) {
  const reason = String(view?.health_reason || view?.status?.reason || "");
  if (/not available in your region/i.test(reason)) return "Cursor API недоступен из региона сервера";
  if (/token/i.test(reason) || !reason) return "Токен Cursor не задан";
  return `Cursor не отвечает: ${reason.slice(0, 140)}`;
}

function cursorDecisionText(view) {
  if (!view) return "—";
  if (!view.openseek_up) return cursorDownText(view);
  const st = view.status || {};
  const rows = Array.isArray(st.decisions) ? st.decisions : [];
  const hits = rows.filter((row) => row.side === "long" || row.side === "short");
  if (st.state === "idle") return "Остановлен — модель не вызывается";
  if (st.state === "wait") return "Ждёт ответ Cursor";
  if (st.state === "error") return llmCycleError(st.reason);
  if (hits.length) {
    return hits
      .map((row) => {
        const conf = Number(row.confidence);
        const pct = Number.isFinite(conf) ? ` ${(conf * 100).toFixed(0)}%` : "";
        const side = row.side === "short" ? "SHORT" : "LONG";
        return `${llmBase(row.pair)} ${side}${pct}`;
      })
      .join(" · ");
  }
  if (st.state === "flat" || st.state === "signal") {
    if (st.reason) return String(st.reason);
    const n = rows.length || (Array.isArray(view.pairs) ? view.pairs.length : 0);
    return n ? `HOLD по ${n} парам` : "HOLD";
  }
  return "Ожидание первого цикла";
}

function renderCursorPairs(pairs) {
  const el = $("cursor-pairs");
  if (!el) return;
  cursorPairs = Array.isArray(pairs) ? pairs.slice() : [];
  const count = $("cursor-tickers-count");
  if (count) count.textContent = String(cursorPairs.length);
  const key = cursorPairs.join("|");
  if (el.dataset.pairsKey === key) return;
  el.dataset.pairsKey = key;
  el.innerHTML = cursorPairs.length
    ? cursorPairs
        .map(
          (pair) => `
      <li class="strategy-tickers-item">
        <span class="strategy-tickers-pair" title="${escapeHtml(pair)}">${escapeHtml(pairBaseLabel(pair))}</span>
        <button type="button" class="strategy-tickers-remove" data-pair="${escapeHtml(pair)}" title="Убрать ${escapeHtml(pairBaseLabel(pair))}" ${settingsReadonly ? "disabled" : ""}>×</button>
      </li>`
        )
        .join("")
    : `<li class="strategy-tickers-empty muted">Нет тикеров — бот не торгует</li>`;
}

function paintCursorBot(view) {
  const badge = $("cursor-state");
  if (badge) {
    const on = !!view?.enabled;
    badge.textContent = on ? "RUNNING" : "STOPPED";
    badge.className = on ? "badge running" : "badge stopped";
  }
  const tp = $("cursor-tp");
  const stake = $("cursor-stake");
  const max = $("cursor-max");
  const conf = $("cursor-conf");
  const active = document.activeElement;
  const settings = view?.settings || {};
  if (tp && active !== tp) tp.value = String(settings.take_profit_pct ?? 3);
  if (stake && active !== stake) stake.value = String(settings.stake_amount ?? 5);
  if (max && active !== max) max.value = String(settings.max_open_trades ?? 1);
  if (conf && active !== conf) conf.value = String(settings.min_confidence_pct ?? 65);
  const locked = !!settingsReadonly;
  for (const el of [tp, stake, max, conf, $("cursor-save"), $("cursor-pair-add"), $("cursor-pair-input")]) {
    if (el) el.disabled = locked;
  }
  const toggle = $("cursor-toggle");
  if (toggle) {
    toggle.textContent = view?.enabled ? "Стоп" : "Старт";
    toggle.classList.toggle("danger", !!view?.enabled);
    toggle.classList.toggle("primary", !view?.enabled);
    toggle.disabled = locked;
  }
  renderCursorPairs(Array.isArray(settings.pairs) ? settings.pairs : view?.pairs || []);
  const subtitle = $("cursor-subtitle");
  if (subtitle) {
    subtitle.textContent = `вход от ${settings.min_confidence_pct ?? 65}% · стейк ${settings.stake_amount ?? 5} USDT · цикл 30 мин · стоп −2%`;
  }
  const decision = $("cursor-decision");
  if (decision) decision.textContent = cursorDecisionText(view);
  renderTradesInto($("cursor-trades"), "strategy", view?.trades || []);
}

async function refreshCursorBot() {
  if (!userMayUseBlock("llm")) return;
  const view = await pairConfigApi("/cursor");
  paintCursorBot(view);
}

async function saveCursorSettings(extra = {}) {
  const body = {
    take_profit_pct: Number($("cursor-tp")?.value),
    stake_amount: Number($("cursor-stake")?.value),
    max_open_trades: Number($("cursor-max")?.value),
    min_confidence_pct: Number($("cursor-conf")?.value),
    ...extra,
  };
  const view = await pairConfigApi("/cursor", "POST", body);
  paintCursorBot(view);
  return view;
}

function showCursorMsg(text, isError = false) {
  const el = $("cursor-msg");
  if (!el) return;
  el.textContent = text;
  el.className = `pair-msg ${isError ? "error" : "ok"}`;
  el.classList.remove("hidden");
}

function cursor50DecisionText(view) {
  const actions = Array.isArray(view?.status?.actions) ? view.status.actions : [];
  const hits = actions.filter((row) => row.result === "signal" || row.result === "close");
  if (hits.length) {
    return hits
      .map((row) => {
        const base = llmBase(row.pair);
        if (row.result === "close") return `${base} CLOSE`;
        const side = row.action === "SELL" ? "SHORT" : "LONG";
        const stake = Number(row.stake_usdt);
        const usd = Number.isFinite(stake) ? ` ${stake}$` : "";
        return `${base} ${side}${usd}`;
      })
      .join(" · ");
  }
  if (!view?.openseek_up) return cursorDownText(view);
  if (!view?.enabled) return "Нажмите Старт — модель получит бюджет 50 USDT";
  if (view?.status?.state === "idle") return "Остановлен";
  if (view?.status?.state === "wait") return "Ждёт ответ Cursor";
  if (view?.status?.state === "error") return llmCycleError(view?.status?.reason);
  if (view?.status?.reason && !hits.length) return String(view.status.reason);
  return actions.length ? "Без новых входов" : "Ожидание первого цикла";
}

function paintCursor50(view) {
  const badge = $("cursor50-state");
  if (badge) {
    const on = !!view?.enabled;
    badge.textContent = on ? "RUNNING" : "STOPPED";
    badge.className = on ? "badge running" : "badge stopped";
  }
  const stats = view?.stats || {};
  const free = $("cursor50-free");
  const used = $("cursor50-used");
  const pnl = $("cursor50-pnl");
  const trades = $("cursor50-count");
  if (free) free.textContent = `${stats.free_usdt ?? 50} $`;
  if (used) used.textContent = `${stats.used_usdt ?? 0} $`;
  if (pnl) {
    const value = Number(stats.realized_usdt ?? 0);
    pnl.textContent = `${value > 0 ? "+" : ""}${value.toFixed(2)} $`;
    pnl.className = value > 0 ? "pos" : value < 0 ? "neg" : "";
  }
  if (trades) trades.textContent = `${stats.closed_count ?? 0} · ${stats.wins ?? 0}/${stats.losses ?? 0}`;
  const subtitle = $("cursor50-subtitle");
  if (subtitle) {
    const pairCount = Array.isArray(view?.pairs) ? view.pairs.length : 25;
    subtitle.textContent = `бюджет ${view?.budget_usdt ?? 50} USDT · в работе ${stats.used_usdt ?? 0} · ${pairCount} пар · плечо 3x · цикл 30 мин`;
  }
  const toggle = $("cursor50-toggle");
  if (toggle) {
    toggle.textContent = view?.enabled ? "Стоп" : "Старт";
    toggle.classList.toggle("danger", !!view?.enabled);
    toggle.classList.toggle("primary", !view?.enabled);
    toggle.disabled = !!settingsReadonly;
  }
  const decision = $("cursor50-decision");
  if (decision) decision.textContent = cursor50DecisionText(view);
  renderTradesInto($("cursor50-trades"), "strategy", view?.trades || []);
}

async function refreshCursor50() {
  if (!userMayUseBlock("cursor50")) return;
  const view = await pairConfigApi("/cursor50");
  paintCursor50(view);
}

function showCursor50Msg(text, isError = false) {
  const el = $("cursor50-msg");
  if (!el) return;
  el.textContent = text;
  el.className = `pair-msg ${isError ? "error" : "ok"}`;
  el.classList.remove("hidden");
}

function bindCursorControls() {
  $("cursor-save")?.addEventListener("click", async () => {
    const btn = $("cursor-save");
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    try {
      await saveCursorSettings();
      showCursorMsg("Сохранено");
    } catch (e) {
      if (e.message === "auth") logout();
      else showCursorMsg(formatApiError(e.message), true);
    } finally {
      btn.disabled = !!settingsReadonly;
    }
  });
  $("cursor-toggle")?.addEventListener("click", async () => {
    const btn = $("cursor-toggle");
    if (!btn || btn.disabled) return;
    const enable = btn.textContent.trim() !== "Стоп";
    btn.disabled = true;
    try {
      await saveCursorSettings({ enabled: enable });
      showCursorMsg(enable ? "Бот запущен" : "Новые входы остановлены");
    } catch (e) {
      if (e.message === "auth") logout();
      else showCursorMsg(formatApiError(e.message), true);
    } finally {
      btn.disabled = !!settingsReadonly;
    }
  });
  $("cursor-tickers-btn")?.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    const wrap = $("cursor-tickers");
    const panel = $("cursor-tickers-panel");
    const btn = $("cursor-tickers-btn");
    if (!wrap || !panel || !btn) return;
    const willOpen = panel.hidden;
    closeLlmTickersPanel();
    if (!willOpen) return;
    panel.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    wrap.classList.add("is-open");
    requestAnimationFrame(() => {
      positionStrategyTickersPanel(wrap, panel);
      const input = $("cursor-pair-input");
      if (input && !input.disabled) input.focus();
    });
  });
  $("cursor-pair-form")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = $("cursor-pair-input");
    const raw = String(input?.value || "").trim();
    if (!raw || settingsReadonly) return;
    const btn = $("cursor-pair-add");
    if (btn) btn.disabled = true;
    try {
      await saveCursorSettings({ pairs: [...cursorPairs, raw] });
      if (input) input.value = "";
      showCursorMsg("Пара добавлена");
    } catch (e) {
      if (e.message === "auth") logout();
      else showCursorMsg(formatApiError(e.message), true);
    } finally {
      if (btn) btn.disabled = !!settingsReadonly;
    }
  });
  $("cursor-pairs")?.addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".strategy-tickers-remove");
    if (!btn || btn.disabled) return;
    ev.preventDefault();
    ev.stopPropagation();
    const pair = btn.getAttribute("data-pair");
    if (!pair) return;
    btn.disabled = true;
    try {
      await saveCursorSettings({ pairs: cursorPairs.filter((item) => item !== pair) });
      showCursorMsg(`${pairBaseLabel(pair)} убрана`);
    } catch (e) {
      if (e.message === "auth") logout();
      else showCursorMsg(formatApiError(e.message), true);
    }
  });
  $("cursor50-toggle")?.addEventListener("click", async () => {
    const btn = $("cursor50-toggle");
    if (!btn || btn.disabled) return;
    const enable = btn.textContent.trim() !== "Стоп";
    btn.disabled = true;
    try {
      const view = await pairConfigApi("/cursor50", "POST", { enabled: enable });
      paintCursor50(view);
      showCursor50Msg(enable ? "Бот запущен, бюджет 50 USDT" : "Новые решения остановлены");
    } catch (e) {
      if (e.message === "auth") logout();
      else showCursor50Msg(formatApiError(e.message), true);
    } finally {
      btn.disabled = !!settingsReadonly;
    }
  });
}

let fastRefreshLock = false;
let heavyRefreshLock = false;
let fastRefreshAgain = false;
let lastHeavyRefreshAt = 0;
let telegramPaused = false;
const HEAVY_REFRESH_MS = 40000;

function panelIsActive() {
  const app = $("app-screen");
  return !!(
    sessionToken &&
    app &&
    !app.classList.contains("hidden") &&
    document.visibilityState !== "hidden" &&
    !telegramPaused
  );
}

function stampLastUpdate() {
  const stamp = $("last-update");
  if (stamp) stamp.textContent = `Обновлено: ${fmtAppTime(new Date())} ${APP_TZ_LABEL}`;
}

async function refreshOpenTradesFast() {
  try {
    if (boardMode === "summary") {
      await syncOpenSummaryBoard();
      stampLastUpdate();
      return;
    }
    if (openSummaryCache) renderTradesSummaryFromOpen(openSummaryCache);
    let cachedTrades = [];
    try {
      const bundle = await fetchOpenTradesCache();
      cachedTrades = paintOpenTradesFromServerBundle(bundle) || [];
    } catch (e) {
      if (e.message === "auth") throw e;
      console.warn("open-trades cache:", e.message);
    }
    renderTradesSummary(cachedTrades);
    stampLastUpdate();
    if (boardMode === "trades") syncTradeChartsBoard();
  } catch (e) {
    if (e.message === "auth") logout();
    else console.error(e);
  }
}

async function refreshPanelHeavy() {
  try {
    const now = Date.now();
    if (sessionToken) {
      try {
        const me = await refreshAuthMe();
        lastAuthMeRefresh = now;
        if (sharedStackMode) paintSharedStrategyState();
        updateMultiUserUi(me);
      } catch (e) {
        if (e.message === "auth") throw e;
      }
    }

    try {
      const data = await pairConfigApi("/pairs");
      syncTestSettingsFromPayload(data);
      syncLimitsFromPairPayload(data);
    } catch {
      /* pairs are optional for the first bot paint */
    }

    try {
      await refreshStrategyEnabled();
    } catch (e) {
      if (e.message === "auth") throw e;
      console.warn("refreshStrategyEnabled:", e.message);
    }
    const liveBots = ["strategy"];
    if (userMayUseBlock("grid")) liveBots.push("grid");
    const results = [];
    for (const bot of liveBots) {
      if (boardMode !== "bots") break;
      results.push(await refreshBotSafe(bot));
    }
    if (boardMode === "bots") {
      const stratTrades = results[0] || [];
      const gridTrades = liveBots.includes("grid") ? results[liveBots.indexOf("grid")] || [] : [];
      const finderTrades = liveBots.includes("finder") ? results[liveBots.indexOf("finder")] || [] : [];
      renderTradesSummary([
        ...(stratTrades || []),
        ...(gridTrades || []),
        ...(finderTrades || []),
      ]);
      stampLastUpdate();
    }

    if (boardMode === "bots") {
      const sideTasks = [
        refreshPairlistMode().catch(() => {}),
        refreshServerStats().catch(() => {}),
        refreshReconcileBanner().catch(() => {}),
        loadStrategyScanInfo().catch(() => {}),
        refreshCursor50().catch((e) => {
          if (e?.message === "auth") logout();
          else console.warn("refreshCursor50:", e?.message || e);
        }),
      ];
      Promise.all(sideTasks).catch(() => {});
      prefetchStatsBundle().catch(() => {});
    }
  } catch (e) {
    if (e.message === "auth") logout();
    else console.error(e);
  }
}

async function refreshAll({ heavy = null } = {}) {
  if (!panelIsActive()) return;
  const wantHeavy =
    boardMode === "bots" &&
    (heavy === true || (heavy !== false && Date.now() - lastHeavyRefreshAt >= HEAVY_REFRESH_MS));
  const jobs = [];
  if (fastRefreshLock) {
    fastRefreshAgain = true;
  } else {
    fastRefreshLock = true;
    jobs.push(
      refreshOpenTradesFast().finally(() => {
        fastRefreshLock = false;
        if (fastRefreshAgain) {
          fastRefreshAgain = false;
          refreshAll({ heavy: false }).catch(() => {});
        }
      })
    );
  }
  if (wantHeavy && !heavyRefreshLock) {
    lastHeavyRefreshAt = Date.now();
    heavyRefreshLock = true;
    jobs.push(
      refreshPanelHeavy().finally(() => {
        heavyRefreshLock = false;
      })
    );
  }
  if (jobs.length) await Promise.all(jobs);
}

function showPairMsg(text, isError = false) {
  const el = $("pair-msg");
  el.textContent = text;
  el.className = `pair-msg ${isError ? "error" : "ok"}`;
  el.classList.remove("hidden");
}

function showToast(message, kind = "info", timeoutMs = 4500) {
  let wrap = $("toast-wrap");
  if (!wrap) {
    wrap = document.createElement("div");
    wrap.id = "toast-wrap";
    wrap.className = "toast-wrap";
    wrap.setAttribute("role", "status");
    wrap.setAttribute("aria-live", "polite");
    document.body.appendChild(wrap);
  }
  const t = document.createElement("div");
  t.className = `toast toast-${kind}`;
  t.textContent = String(message || "");
  wrap.appendChild(t);
  requestAnimationFrame(() => t.classList.add("is-shown"));
  let gone = false;
  const dismiss = () => {
    if (gone) return;
    gone = true;
    t.classList.remove("is-shown");
    setTimeout(() => t.remove(), 220);
  };
  t.addEventListener("click", dismiss);
  setTimeout(dismiss, timeoutMs);
}

async function loadPairSettings() {
  await loadStrategySettings();
  const data = await pairConfigApi("/pairs");
  finderBotEnabled = true;
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

  syncTestSettingsFromPayload(data);
  syncLimitsFromPairPayload(data);
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
      <span class="muted max-trades-hint">0 = стоп · любое целое ≥ 1</span>
    `;
    bindMaxTradesInput(el.querySelector("[data-max-trades-input]"), bot);
  });

  renderMaxPerStrategySettings();

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

function isSettingsOpen() {
  const v = $("settings-view");
  return !!(v && !v.classList.contains("hidden"));
}

function renderTradingToggle(me) {
  const statusEl = $("shared-stack-status");
  const chk = $("trading-strategy-enabled");
  if (!statusEl || !chk) return;
  const stack = me?.shared_stack || {};
  const sig = stack.signal_engine || {};
  const exe = stack.trade_executor || {};
  const sigOk = sig.active ? "работает" : sig.state || "—";
  const exeOk = exe.active ? "работает" : exe.state || "—";
  statusEl.textContent = `Signal Engine: ${sigOk} · Executor: ${exeOk}`;
  const liveFlags = me?.trading_flags_live || tradingFlagsLive || tradingFlags;
  const demoFlags = me?.trading_flags_demo || tradingFlagsDemo;
  chk.checked = !!(liveFlags.strategy ?? tradingFlags.strategy);
  const demoChk = $("trading-strategy-demo-enabled");
  const demoRow = $("trading-strategy-demo-row");
  if (demoRow) demoRow.classList.toggle("hidden", !(hasDemoSecrets || isAdminUser()));
  if (demoChk) demoChk.checked = !!demoFlags.strategy;
  const liveRow = $("trading-strategy-live-row");
  if (liveRow) liveRow.classList.toggle("hidden", !hasLiveSecrets && hasDemoSecrets && !isAdminUser());
}

function showTelegramMsg(text, isError = false) {
  const el = $("telegram-msg");
  if (!el) return;
  el.textContent = text || "";
  el.className = isError ? "pair-msg error" : "pair-msg ok";
  el.classList.toggle("hidden", !text);
}

function collectTelegramNotifyPrefs() {
  const prefs = {};
  document.querySelectorAll("[data-tg-notify]").forEach((el) => {
    const key = el.getAttribute("data-tg-notify");
    if (key) prefs[key] = !!el.checked;
  });
  if (prefs.trade_close_all) {
    prefs.trade_close_profit = true;
    prefs.trade_close_loss = true;
  }
  return prefs;
}

function applyTelegramNotifyPrefs(notify) {
  const prefs = notify && typeof notify === "object" ? notify : {};
  document.querySelectorAll("[data-tg-notify]").forEach((el) => {
    const key = el.getAttribute("data-tg-notify");
    if (!key) return;
    el.checked = prefs[key] !== undefined ? !!prefs[key] : !!el.checked;
  });
  syncTelegramCloseAllUi();
}

function syncTelegramCloseAllUi() {
  const all = document.querySelector('[data-tg-notify="trade_close_all"]');
  const profit = document.querySelector('[data-tg-notify="trade_close_profit"]');
  const loss = document.querySelector('[data-tg-notify="trade_close_loss"]');
  if (!all) return;
  if (all.checked) {
    if (profit) {
      profit.checked = true;
      profit.disabled = true;
    }
    if (loss) {
      loss.checked = true;
      loss.disabled = true;
    }
  } else {
    if (profit) profit.disabled = false;
    if (loss) loss.disabled = false;
  }
  const master = $("telegram-notify-enabled");
  const box = $("telegram-notify-kinds");
  if (box) box.classList.toggle("is-disabled", master && !master.checked);
}

async function loadTelegramSettings() {
  const statusEl = $("telegram-status");
  const input = $("telegram-chat-id");
  const chk = $("telegram-notify-enabled");
  try {
    const data = await pairConfigApi("/telegram");
    if (input && !input.dataset.dirty) {
      input.value = data.chat_id || "";
    }
    if (chk) chk.checked = data.enabled !== false && !!data.chat_id;
    applyTelegramNotifyPrefs(data.notify || {});
    if (statusEl) {
      const bot = data.bot_configured ? "бот настроен" : "TELEGRAM_BOT_TOKEN не задан";
      statusEl.textContent = data.chat_id
        ? `Привязан chat id ${data.chat_id} · ${bot}`
        : `Не привязан · ${bot}. В боте: /start → скопируйте id`;
    }
    // Mini App: prefill Telegram user id if empty
    const tgId = window.Telegram?.WebApp?.initDataUnsafe?.user?.id;
    if (input && !input.value && tgId) {
      input.value = String(tgId);
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = e.message || "Ошибка загрузки Telegram";
  }
}

async function saveTelegramSettings({ clear = false } = {}) {
  const input = $("telegram-chat-id");
  const chk = $("telegram-notify-enabled");
  const chatId = clear ? "" : (input?.value.trim() || "");
  const payload = {
    chat_id: chatId,
    enabled: clear ? false : !!(chk?.checked ?? true),
    notify: clear ? undefined : collectTelegramNotifyPrefs(),
  };
  const initData = window.Telegram?.WebApp?.initData;
  if (!chatId && initData && !clear) {
    payload.init_data = initData;
  }
  const data = await pairConfigApi("/telegram", "PATCH", payload);
  if (input) {
    input.value = data.chat_id || "";
    delete input.dataset.dirty;
  }
  if (chk) chk.checked = !!data.enabled;
  applyTelegramNotifyPrefs(data.notify || {});
  showTelegramMsg(
    data.chat_id
      ? `Сохранено: chat id ${data.chat_id}. Проверьте сообщение в Telegram.`
      : "Telegram отвязан"
  );
  await loadTelegramSettings();
  return data;
}

async function saveTradingToggle(enabled, accountMode = "live") {
  const msg = $("trading-toggle-msg");
  try {
    await setBotEnabled("strategy", !!enabled, accountMode);
    const data = await pairConfigApi(`/trading-enabled?account_mode=${accountMode === "demo" ? "demo" : "live"}`);
    if (accountMode === "demo") tradingFlagsDemo = data.flags || tradingFlagsDemo;
    else {
      tradingFlags = data.flags || tradingFlags;
      tradingFlagsLive = data.flags || tradingFlagsLive;
    }
    paintSharedStrategyState();
    if (msg) {
      msg.textContent = enabled ? "Автоторговля включена" : "Автоторговля выключена (открытые сделки всё ещё мониторятся)";
      msg.className = "pair-msg ok";
    }
  } catch (e) {
    if (e.message === "auth") throw e;
    if (msg) {
      msg.textContent = e.message || "Ошибка сохранения";
      msg.className = "pair-msg error";
    }
  }
}

function showScanFreqMsg(text, isError = false) {
  const el = $("scan-freq-msg");
  if (!el) return;
  el.textContent = text || "";
  el.className = isError ? "pair-msg error" : "pair-msg ok";
  el.classList.toggle("hidden", !text);
}

function syncScanFreqFixedVisibility() {
  const mode = $("scan-freq-mode")?.value || "adaptive";
  const wrap = $("scan-freq-fixed-wrap");
  if (wrap) wrap.classList.toggle("hidden", mode !== "fixed");
}

async function loadScanFreqSettings() {
  if (!isAdminUser()) return;
  const statusEl = $("scan-freq-status");
  const modeEl = $("scan-freq-mode");
  const fixedEl = $("scan-freq-fixed");
  try {
    const data = await pairConfigApi("/adaptive-scan");
    const settings = data.settings || {};
    const mode = settings.mode || data.mode || "adaptive";
    const fixed = Number(settings.fixed_interval_min || 15);
    if (modeEl) modeEl.value = mode === "fixed" ? "fixed" : "adaptive";
    if (fixedEl) {
      const allowed = data.allowed_fixed_intervals_min || [5, 10, 15, 30, 60];
      if (allowed.length && fixedEl.options.length !== allowed.length) {
        fixedEl.innerHTML = allowed
          .map((m) => `<option value="${m}">Каждые ${m} мин</option>`)
          .join("");
      }
      fixedEl.value = String(allowed.includes(fixed) ? fixed : allowed[0] || 15);
    }
    syncScanFreqFixedVisibility();
    if (statusEl) {
      if (mode === "fixed") {
        statusEl.textContent = `Сейчас: каждые ${fixed} мин (фикс.) · текущий шаг ${data.current_interval_min ?? fixed} мин`;
      } else {
        const ladder = (data.adaptive_intervals_min || data.intervals_min || [5, 10, 15, 30]).join("→");
        statusEl.textContent = `Сейчас: адаптивно ${ladder} мин · текущий интервал ${data.current_interval_min ?? "—"} мин`;
      }
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = e.message || "Ошибка загрузки частоты скана";
  }
}

async function saveScanFreqSettings() {
  const mode = $("scan-freq-mode")?.value || "adaptive";
  const fixed = Number($("scan-freq-fixed")?.value || 15);
  const payload = {
    mode,
    fixed_interval_min: fixed,
  };
  const data = await pairConfigApi("/adaptive-scan/settings", "PATCH", payload);
  showScanFreqMsg(
    mode === "fixed"
      ? `Сохранено: каждые ${data.settings?.fixed_interval_min || fixed} мин`
      : "Сохранено: адаптивный режим (5→10→15→30)",
  );
  await loadScanFreqSettings();
  refreshServerStats().catch(() => {});
  return data;
}

function openSettings() {
  closeMobileMenu();
  hideMainViewsForOverlay();
  const view = $("settings-view");
  view?.classList.remove("hidden");
  view?.setAttribute("aria-hidden", "false");
  loadPairSettings().catch((e) => {
    if (e.message === "auth") logout();
    else showPairMsg(e.message, true);
  });
  refreshAuthMe()
    .then((me) => renderTradingToggle(me))
    .catch(() => {});
  loadTelegramSettings().catch(() => {});
  if (isAdminUser()) {
    loadUsersAdmin().catch(() => {});
    loadScanFreqSettings().catch(() => {});
  }
}

function closeSettings() {
  $("settings-view")?.classList.add("hidden");
  $("settings-view")?.setAttribute("aria-hidden", "true");
  $("dashboard-view")?.classList.remove("hidden");
  $("pair-msg")?.classList.add("hidden");
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
        <strong class="info-strategy-name">${strategyCatalogLabel(s)}</strong>
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

function openGuide() {
  $("guide-modal")?.classList.remove("hidden");
  $("guide-modal")?.setAttribute("aria-hidden", "false");
}

function closeGuide() {
  $("guide-modal")?.classList.add("hidden");
  $("guide-modal")?.setAttribute("aria-hidden", "true");
}

$("login-btn").addEventListener("click", async () => {
  const loginBtn = $("login-btn");
  if (loginBtn.classList.contains("is-busy")) return;
  creds.user = $("login-user").value.trim();
  creds.pass = $("login-pass").value;
  sessionToken = "";
  $("login-error").classList.add("hidden");
  if (!creds.user || !creds.pass) {
    $("login-error").textContent = "Введите логин и пароль";
    $("login-error").classList.remove("hidden");
    return;
  }
  loginBtn.classList.add("is-busy");
  loginBtn.disabled = true;
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
  } finally {
    loginBtn.classList.remove("is-busy");
    loginBtn.disabled = false;
  }
});

function showUsersMsg(text, isError = false) {
  const el = $("users-msg");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("error", !!isError);
  el.classList.remove("hidden");
}

function showSecretsMsg(text, isError = false) {
  const el = $("secrets-msg");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("error", !!isError);
  el.classList.remove("hidden");
}

async function loadUsersAdmin() {
  const list = $("users-list");
  if (!list || !isAdminUser()) return;
  if (!permissionCatalog?.blocks?.length) {
    try {
      permissionCatalog = await pairConfigApi("/permission-catalog");
    } catch {
      /* ignore */
    }
  }
  const data = await pairConfigApi("/users");
  const users = data.users || [];
  list.innerHTML = users
    .map((u) => {
      const role = escapeHtml(u.role || "user");
      const name = escapeHtml(u.username || u.id);
      const en = u.enabled !== false;
      const isAdm = u.role === "admin" || u.id === "admin";
      const stratN = u.allowed_strategies == null ? "все" : String(u.allowed_strategies.length);
      const blockN = u.allowed_blocks == null ? "все" : String(u.allowed_blocks.length);
      return `<li class="users-list-item">
        <div>
          <strong>${name}</strong>
          <span class="muted"> · ${role}${en ? "" : " · выкл"}</span>
          ${
            isAdm
              ? ""
              : `<div class="muted users-perms-hint">блоки: ${blockN} · стратегии: ${stratN}${
                  u.demo_username ? ` · demo: ${escapeHtml(u.demo_username)}` : ""
                }</div>`
          }
        </div>
        <div class="users-list-actions">
          ${
            isAdm
              ? ""
              : `<button type="button" class="btn btn-sm ghost" data-user-impersonate="${escapeHtml(u.id)}">Войти как</button>
                 <button type="button" class="btn btn-sm ghost" data-user-perms="${escapeHtml(u.id)}">Права</button>
                 <button type="button" class="btn btn-sm ghost" data-user-toggle="${escapeHtml(u.id)}" data-enabled="${en ? "0" : "1"}">${en ? "Выкл" : "Вкл"}</button>
                 <button type="button" class="btn btn-sm ghost" data-user-pass="${escapeHtml(u.id)}">Пароль</button>`
          }
        </div>
      </li>`;
    })
    .join("");
  list.querySelectorAll("[data-user-toggle]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await pairConfigApi(`/users/${btn.dataset.userToggle}`, "PATCH", {
          enabled: btn.dataset.enabled === "1",
        });
        showUsersMsg("Статус обновлён");
        await loadUsersAdmin();
      } catch (e) {
        showUsersMsg(formatApiError(e.message), true);
      }
    });
  });
  list.querySelectorAll("[data-user-pass]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const password = prompt("Новый пароль пользователя:");
      if (!password) return;
      try {
        await pairConfigApi(`/users/${btn.dataset.userPass}/password`, "POST", { password });
        showUsersMsg("Пароль обновлён");
      } catch (e) {
        showUsersMsg(formatApiError(e.message), true);
      }
    });
  });
  list.querySelectorAll("[data-user-perms]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const u = users.find((x) => String(x.id) === String(btn.dataset.userPerms));
      if (u) openUserPermsModal(u);
    });
  });
  list.querySelectorAll("[data-user-impersonate]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Войти в панель этого пользователя? Можно вернуться кнопкой «Вернуться в админ».")) return;
      try {
        await startImpersonation(btn.dataset.userImpersonate);
      } catch (e) {
        showUsersMsg(formatApiError(e.message), true);
      }
    });
  });
}

async function startImpersonation(userId) {
  const data = await pairConfigApi("/auth/impersonate", "POST", { user_id: userId });
  sessionToken = data.token || "";
  currentUser = data.user || null;
  creds.user = currentUser?.username || "";
  isImpersonating = true;
  impersonatedBy = data.impersonated_by || null;
  tokens = { finder: null, strategy: null, grid: null };
  saveSession();
  closeSettings();
  await refreshAuthMe();
  tokens = { finder: "proxy", strategy: "proxy", grid: "proxy" };
  await refreshAll().catch(() => {});
}

async function stopImpersonation() {
  const data = await pairConfigApi("/auth/stop-impersonate", "POST", {});
  sessionToken = data.token || "";
  currentUser = data.user || null;
  creds.user = currentUser?.username || "";
  isImpersonating = false;
  impersonatedBy = null;
  tokens = { finder: null, strategy: null, grid: null };
  saveSession();
  await refreshAuthMe();
  tokens = { finder: "proxy", strategy: "proxy", grid: "proxy" };
  await refreshAll().catch(() => {});
  if (isAdminUser()) loadUsersAdmin().catch(() => {});
}

async function switchAccount(target) {
  const data = await pairConfigApi("/auth/switch-account", "POST", { target });
  sessionToken = data.token || "";
  currentUser = data.user || null;
  creds.user = currentUser?.username || "";
  tokens = { finder: null, strategy: null, grid: null };
  saveSession();
  // Apply full switch payload immediately (includes account_kind, allowed_blocks,
  // secrets) so Live/Demo buttons and panels update before /auth/me round-trip.
  applyAuthMeState(data);
  updateMultiUserUi(data);
  // Allow reverse switch while panels refresh (do not hold UI lock across refreshAll).
  _accountSwitchInFlight = false;
  try {
    await refreshAuthMe();
  } catch (e) {
    if (e.message === "auth") throw e;
  }
  tokens = { finder: "proxy", strategy: "proxy", grid: "proxy" };
  await refreshAll().catch(() => {});
}

function showUserPermsMsg(text, isError = false) {
  const el = $("user-perms-msg");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("error", !!isError);
  el.classList.remove("hidden");
}

function closeUserPermsModal() {
  const modal = $("user-perms-modal");
  if (!modal) return;
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
}

function openUserPermsModal(user) {
  const modal = $("user-perms-modal");
  if (!modal) return;
  $("user-perms-id").value = user.id || "";
  $("user-perms-title").textContent = `Права: ${user.username || user.id}`;
  $("user-perms-msg")?.classList.add("hidden");

  const blocks = permissionCatalog.blocks || [];
  const strategies = permissionCatalog.strategies || [];
  const allowBlocks = user.allowed_blocks; // null = all
  const allowStrats = user.allowed_strategies;

  const blocksEl = $("user-perms-blocks");
  if (blocksEl) {
    blocksEl.innerHTML = blocks
      .map((b) => {
        const checked =
          allowBlocks == null ? true : allowBlocks.includes(b.id);
        return `<label class="user-perms-check">
          <input type="checkbox" data-perm-block="${escapeHtml(b.id)}" ${checked ? "checked" : ""} />
          <span>${escapeHtml(b.name || b.id)}</span>
        </label>`;
      })
      .join("");
  }

  const stratsEl = $("user-perms-strategies");
  if (stratsEl) {
    stratsEl.innerHTML = strategies
      .map((s) => {
        const checked =
          allowStrats == null ? true : allowStrats.includes(s.id);
        const label = s.num != null ? `#${s.num} ${s.name || s.id}` : s.name || s.id;
        const grp = s.test_group ? " · тест" : "";
        return `<label class="user-perms-check">
          <input type="checkbox" data-perm-strategy="${escapeHtml(s.id)}" ${checked ? "checked" : ""} />
          <span>${escapeHtml(label)}${grp ? `<span class="muted">${grp}</span>` : ""}</span>
        </label>`;
      })
      .join("");
  }

  modal.dataset.blocksUnrestricted = allowBlocks == null ? "1" : "0";
  modal.dataset.stratsUnrestricted = allowStrats == null ? "1" : "0";
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
}

async function saveUserPerms() {
  const userId = $("user-perms-id")?.value;
  if (!userId) return;
  const blockBoxes = [...document.querySelectorAll("#user-perms-blocks input[data-perm-block]")];
  const stratBoxes = [...document.querySelectorAll("#user-perms-strategies input[data-perm-strategy]")];
  const allBlocksChecked = blockBoxes.length > 0 && blockBoxes.every((b) => b.checked);
  const allStratsChecked = stratBoxes.length > 0 && stratBoxes.every((b) => b.checked);
  // If everything checked → unrestricted (null). Else explicit list (may be empty).
  const allowed_blocks = allBlocksChecked
    ? null
    : blockBoxes.filter((b) => b.checked).map((b) => b.dataset.permBlock);
  const allowed_strategies = allStratsChecked
    ? null
    : stratBoxes.filter((b) => b.checked).map((b) => b.dataset.permStrategy);
  try {
    await pairConfigApi(`/users/${userId}`, "PATCH", {
      allowed_blocks,
      allowed_strategies,
    });
    showUserPermsMsg("Права сохранены");
    closeUserPermsModal();
    await loadUsersAdmin();
  } catch (e) {
    showUserPermsMsg(formatApiError(e.message), true);
  }
}

$("users-create-btn")?.addEventListener("click", async () => {
  const username = $("users-new-name")?.value.trim();
  const password = $("users-new-pass")?.value || "";
  if (!username || !password) return showUsersMsg("Укажите логин и пароль", true);
  try {
    await pairConfigApi("/users", "POST", { username, password });
    $("users-new-name").value = "";
    $("users-new-pass").value = "";
    showUsersMsg(`Создан ${username}. Боты выкл. Live/Demo ключи — в настройках.`);
    await loadUsersAdmin();
  } catch (e) {
    showUsersMsg(formatApiError(e.message), true);
  }
});

$("users-migrate-demo-btn")?.addEventListener("click", async () => {
  if (!confirm("Удалить legacy *_demo логины и перенести данные в dual-key контексты?")) return;
  try {
    const r = await pairConfigApi("/users/migrate-demo-accounts", "POST", {});
    showUsersMsg(
      `Миграция Demo: перенесено ${(r.migrated || []).length}, пустых удалено ${(r.deleted_empty || []).length}, ошибок ${(r.errors || []).length}`
    );
    await loadUsersAdmin();
  } catch (e) {
    showUsersMsg(formatApiError(e.message), true);
  }
});

$("impersonate-return-btn")?.addEventListener("click", async () => {
  try {
    await stopImpersonation();
  } catch (e) {
    showToast(formatApiError(e.message), "error");
  }
});

let _accountSwitchInFlight = false;
$("account-switch-live")?.addEventListener("click", async () => {
  if (_accountSwitchInFlight) return;
  if (accountKind === "live") return;
  _accountSwitchInFlight = true;
  try {
    await switchAccount("live");
  } catch (e) {
    showToast(formatApiError(e.message) || "Не удалось переключить аккаунт", "error");
  } finally {
    _accountSwitchInFlight = false;
  }
});
$("account-switch-demo")?.addEventListener("click", async () => {
  if (_accountSwitchInFlight) return;
  if (accountKind === "demo") return;
  _accountSwitchInFlight = true;
  try {
    await switchAccount("demo");
  } catch (e) {
    showToast(formatApiError(e.message) || "Не удалось переключить аккаунт", "error");
  } finally {
    _accountSwitchInFlight = false;
  }
});

$("user-perms-close")?.addEventListener("click", closeUserPermsModal);
$("user-perms-cancel")?.addEventListener("click", closeUserPermsModal);
$("user-perms-save")?.addEventListener("click", () => {
  saveUserPerms().catch(() => {});
});
document.querySelector('#user-perms-modal [data-close="user-perms"]')?.addEventListener("click", closeUserPermsModal);
$("user-perms-blocks-all")?.addEventListener("click", () => {
  document.querySelectorAll("#user-perms-blocks input[data-perm-block]").forEach((el) => {
    el.checked = true;
  });
});
$("user-perms-blocks-none")?.addEventListener("click", () => {
  document.querySelectorAll("#user-perms-blocks input[data-perm-block]").forEach((el) => {
    el.checked = false;
  });
});
$("user-perms-strats-all")?.addEventListener("click", () => {
  document.querySelectorAll("#user-perms-strategies input[data-perm-strategy]").forEach((el) => {
    el.checked = true;
  });
});
$("user-perms-strats-none")?.addEventListener("click", () => {
  document.querySelectorAll("#user-perms-strategies input[data-perm-strategy]").forEach((el) => {
    el.checked = false;
  });
});

async function saveModeSecrets(mode, exchange = "bybit") {
  const ex = exchange === "binance" ? "binance" : "bybit";
  const prefix = `secrets-${ex}-${mode === "demo" ? "demo" : "live"}`;
  const key = $(`${prefix}-api-key`)?.value.trim() || "";
  const secret = $(`${prefix}-api-secret`)?.value || "";
  if (!key || !secret) return showSecretsMsg("Введите API Key и Secret", true);
  const exLabel = ex === "binance" ? "Binance" : "Bybit";
  try {
    const payload = {
      account_mode: mode,
      exchange: ex,
      bybit_api_key: key,
      bybit_api_secret: secret,
      name: `${exLabel} ${mode === "demo" ? "Demo" : "Live"}`,
    };
    if (isAdminUser() && !isImpersonating && ex === "bybit") payload.user_id = "admin";
    await pairConfigApi("/secrets", "PUT", payload);
    if ($(`${prefix}-api-key`)) $(`${prefix}-api-key`).value = "";
    if ($(`${prefix}-api-secret`)) $(`${prefix}-api-secret`).value = "";
    showSecretsMsg(
      mode === "demo"
        ? `${exLabel} Demo ключи сохранены`
        : `${exLabel} Live ключи сохранены`
    );
    await refreshAuthMe();
    applyBlockVisibility();
  } catch (e) {
    showSecretsMsg(formatApiError(e.message), true);
  }
}
async function savePanelTradeOn(panel, value) {
  const next = String(value || "bybit").toLowerCase();
  const key = panel === "test" ? "trade_on_test" : "trade_on_main";
  try {
    const payload = { [key]: next };
    if (isAdminUser() && !isImpersonating) payload.user_id = currentUser?.id || "admin";
    const res = await pairConfigApi("/trade-on", "PUT", payload);
    tradeOnMainPref = res?.trade_on_main || tradeOnMainPref;
    tradeOnTestPref = res?.trade_on_test || tradeOnTestPref;
    tradeOnPref = res?.trade_on || tradeOnMainPref;
    const mainSel = $("trade-on-main-select");
    if (mainSel) mainSel.value = tradeOnMainPref;
    const testSel = $("trade-on-test-select");
    if (testSel) testSel.value = tradeOnTestPref;
    const label = next === "all" ? "все" : next === "binance" ? "Binance" : "Bybit";
    showSecretsMsg(
      panel === "test" ? `Тестовые → биржа: ${label}` : `Основные → биржа: ${label}`
    );
    await refreshAuthMe();
  } catch (e) {
    showSecretsMsg(formatApiError(e.message), true);
    const mainSel = $("trade-on-main-select");
    if (mainSel) mainSel.value = tradeOnMainPref;
    const testSel = $("trade-on-test-select");
    if (testSel) testSel.value = tradeOnTestPref;
  }
}
document.querySelectorAll("[data-secrets-save]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const mode = btn.getAttribute("data-secrets-save") || "live";
    const exchange = btn.getAttribute("data-secrets-exchange") || "bybit";
    saveModeSecrets(mode, exchange).catch(() => {});
  });
});
$("trade-on-main-select")?.addEventListener("change", (e) => {
  savePanelTradeOn("main", e.target.value).catch(() => {});
});
$("trade-on-test-select")?.addEventListener("change", (e) => {
  savePanelTradeOn("test", e.target.value).catch(() => {});
});
document.querySelectorAll(".secrets-ex-card").forEach((card) => {
  card.addEventListener("toggle", () => {
    card.dataset.userToggled = "1";
  });
});

$("trading-strategy-enabled")?.addEventListener("change", async (e) => {
  const enabled = !!e.target.checked;
  try {
    await saveTradingToggle(enabled, "live");
  } catch (err) {
    if (err.message === "auth") logout();
    e.target.checked = !enabled;
  }
});
$("trading-strategy-demo-enabled")?.addEventListener("change", async (e) => {
  const enabled = !!e.target.checked;
  try {
    await saveTradingToggle(enabled, "demo");
  } catch (err) {
    if (err.message === "auth") logout();
    e.target.checked = !enabled;
  }
});

$("telegram-chat-id")?.addEventListener("input", () => {
  if ($("telegram-chat-id")) $("telegram-chat-id").dataset.dirty = "1";
});
$("telegram-notify-enabled")?.addEventListener("change", () => {
  syncTelegramCloseAllUi();
});
$("telegram-notify-kinds")?.addEventListener("change", (e) => {
  const t = e.target;
  if (!(t instanceof HTMLInputElement) || !t.hasAttribute("data-tg-notify")) return;
  if (t.getAttribute("data-tg-notify") === "trade_close_all") {
    syncTelegramCloseAllUi();
  }
});
$("telegram-save-btn")?.addEventListener("click", async () => {
  try {
    await saveTelegramSettings();
  } catch (e) {
    if (e.message === "auth") logout();
    else showTelegramMsg(formatApiError(e.message), true);
  }
});
$("telegram-clear-btn")?.addEventListener("click", async () => {
  try {
    await saveTelegramSettings({ clear: true });
  } catch (e) {
    if (e.message === "auth") logout();
    else showTelegramMsg(formatApiError(e.message), true);
  }
});

$("scan-freq-mode")?.addEventListener("change", () => {
  syncScanFreqFixedVisibility();
});
$("scan-freq-save-btn")?.addEventListener("click", async () => {
  try {
    await saveScanFreqSettings();
  } catch (e) {
    if (e.message === "auth") logout();
    else showScanFreqMsg(formatApiError(e.message), true);
  }
});

function showStrategyBotRestartMsg(text, isError = false) {
  const el = $("strategy-bot-restart-msg");
  if (!el) return;
  el.textContent = text;
  el.className = isError ? "pair-msg error" : "pair-msg ok";
  el.classList.remove("hidden");
}

$("strategy-bot-restart-btn")?.addEventListener("click", async () => {
  const btn = $("strategy-bot-restart-btn");
  if (!confirm("Перезапустить бот стратегий? На время старта новые сигналы не пойдут.")) return;
  if (btn) btn.disabled = true;
  showStrategyBotRestartMsg("Перезапуск…");
  try {
    const data = await pairConfigApi("/strategy-bot/restart", "POST", {});
    const state = data.state || "active";
    showStrategyBotRestartMsg(`Бот стратегий перезапущен (${state}).`);
  } catch (e) {
    if (e.message === "auth") logout();
    else showStrategyBotRestartMsg(formatApiError(e.message), true);
  } finally {
    if (btn) btn.disabled = false;
  }
});

function closeNavDropdowns(except = null) {
  document.querySelectorAll("[data-nav-dropdown].is-open").forEach((dd) => {
    if (except && dd === except) return;
    dd.classList.remove("is-open");
    const toggle = dd.querySelector(".nav-dropdown-toggle");
    const menu = dd.querySelector(".nav-dropdown-menu");
    if (toggle) toggle.setAttribute("aria-expanded", "false");
    if (menu) menu.setAttribute("hidden", "");
  });
}

function openNavDropdown(dd) {
  if (!dd) return;
  const toggle = dd.querySelector(".nav-dropdown-toggle");
  const menu = dd.querySelector(".nav-dropdown-menu");
  if (!toggle || !menu) return;
  closeNavDropdowns(dd);
  dd.classList.add("is-open");
  toggle.setAttribute("aria-expanded", "true");
  menu.removeAttribute("hidden");
}

function bindNavDropdowns() {
  const root = $("topbar");
  if (!root || root.dataset.navDropdownBound) return;
  root.dataset.navDropdownBound = "1";
  root.addEventListener("click", (e) => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    const toggle = t.closest(".nav-dropdown-toggle");
    if (toggle) {
      e.preventDefault();
      e.stopPropagation();
      const dd = toggle.closest("[data-nav-dropdown]");
      if (!dd) return;
      if (dd.classList.contains("is-open")) closeNavDropdowns();
      else openNavDropdown(dd);
      return;
    }
    if (t.closest(".nav-dropdown-item")) closeNavDropdowns();
  });
  document.addEventListener(
    "click",
    (e) => {
      const t = e.target;
      if (!(t instanceof Element)) return;
      if (t.closest("[data-nav-dropdown]")) return;
      closeNavDropdowns();
    },
    true
  );
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeNavDropdowns();
  });
  window.addEventListener("resize", () => closeNavDropdowns());
}

function closeMobileMenu() {
  const bar = $("topbar");
  const toggle = $("menu-toggle");
  closeNavDropdowns();
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
  return window.matchMedia("(max-width: 900px)").matches;
}

const LOG_POLL_MS = 1500;
let logsPollTimer = null;
let logsPositions = {};
let logsFilter = "all";
let logsStickBottom = true;

function logLineClass(line) {
  if (/\b(ERROR|CRITICAL|Exception|Traceback|ORDER FAIL|ТОРГОВЛЯ ЗАБЛОКИРОВАНА)\b/i.test(line)) {
    return "log-error";
  }
  if (/\b(WARNING|WARN|SKIP|ML gate (BLOCK|отклонил)|сделки не открываются)\b/i.test(line)) {
    return "log-warn";
  }
  if (/\b(ENTRY OK|ML gate ALLOW|SIGNAL → executor)\b/i.test(line)) {
    return "log-ok";
  }
  return "";
}

function splitLogLine(line) {
  const m = line.match(
    /^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?(?: UTC(?:\+3)?)?)\s+(.*)$/s
  );
  if (m) return { ts: m[1], text: m[2] };
  return { ts: null, text: line };
}

/** Bot log files are written in UTC on VPS — show wall clock in APP_TIMEZONE. */
function fmtLogTs(tsRaw) {
  if (!tsRaw) return null;
  const s = String(tsRaw).trim();
  if (/UTC\+3\b/i.test(s)) {
    return s.replace(/\s*UTC\+3\b/i, "").trim() + ` ${APP_TZ_LABEL}`;
  }
  const d = parseAppInstant(s.replace(/\s*UTC\b/i, "").trim());
  if (!d) return s;
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: APP_TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(d);
  const get = (t) => parts.find((p) => p.type === t)?.value || "";
  const ms = s.match(/[,.](\d+)/);
  const frac = ms ? `,${ms[1].padEnd(3, "0").slice(0, 3)}` : "";
  return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}:${get("second")}${frac} ${APP_TZ_LABEL}`;
}

function appendLogEntries(entries) {
  const out = $("logs-output");
  if (!out || !entries.length) return;
  const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 48;
  const frag = document.createDocumentFragment();
  for (const e of entries) {
    const row = document.createElement("div");
    const parsed = e.ts ? { ts: e.ts, text: e.line } : splitLogLine(e.line);
    let body = e.line;
    if (parsed.ts) {
      const split = splitLogLine(e.line);
      body = split.ts ? split.text : e.line;
    }
    row.className = `log-line log-${e.bot} ${logLineClass(body)}`;
    const shownTs = fmtLogTs(parsed.ts);
    const timeHtml = shownTs
      ? `<span class="log-time">${escapeHtml(shownTs)}</span>`
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
      status.textContent = `Обновлено: ${fmtAppTime(new Date())} ${APP_TZ_LABEL} · ${(data.entries || []).length} новых строк`;
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
  hideMainViewsForOverlay();
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
  grid_ft: "Grid",
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
  const from = fmtAppDateShort(row.from);
  if (!row.until) {
    return `Действует с ${from} — по сейчас`;
  }
  const until = fmtAppDateShort(row.until);
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
    const d = e.at ? parseAppInstant(e.at) || new Date() : new Date();
    const key = fmtAppDayLabel(d);
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
      const time = e.at ? fmtAppTime(e.at) : "—";
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
  if (status) status.textContent = `${entries.length} записей · обновлено ${fmtAppTime(new Date())} ${APP_TZ_LABEL}`;
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
  hideMainViewsForOverlay();
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

function goHome() {
  closeMobileMenu();
  stopLogsPoll();
  closeStats();
  closeInfo();
  closeGuide();
  closeBybitGridModal();
  $("changelog-view")?.classList.add("hidden");
  $("changelog-view")?.setAttribute("aria-hidden", "true");
  $("logs-view")?.classList.add("hidden");
  $("logs-view")?.setAttribute("aria-hidden", "true");
  $("history-view")?.classList.add("hidden");
  $("history-view")?.setAttribute("aria-hidden", "true");
  $("charts-view")?.classList.add("hidden");
  $("charts-view")?.setAttribute("aria-hidden", "true");
  $("report-view")?.classList.add("hidden");
  $("report-view")?.setAttribute("aria-hidden", "true");
  $("settings-view")?.classList.add("hidden");
  $("settings-view")?.setAttribute("aria-hidden", "true");
  $("pnl-dashboard-view")?.classList.add("hidden");
  $("pnl-dashboard-view")?.setAttribute("aria-hidden", "true");
  $("rating-view")?.classList.add("hidden");
  $("rating-view")?.setAttribute("aria-hidden", "true");
  $("dashboard-view")?.classList.remove("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function isNavSurfaceOpen(id) {
  const el = $(id);
  return !!(el && !el.classList.contains("hidden"));
}

/** Second click on the same menu item returns to the main dashboard. */
function toggleNavItem(surfaceId, openFn) {
  closeMobileMenu();
  if (isNavSurfaceOpen(surfaceId)) {
    goHome();
    return;
  }
  openFn();
}

$("logout-btn").addEventListener("click", () => {
  closeMobileMenu();
  logout();
});
$("brand-home-btn")?.addEventListener("click", goHome);
bindNavDropdowns();
$("stats-btn").addEventListener("click", () => {
  toggleNavItem("stats-view", () => openStats("all", "all"));
});
$("pnl-dashboard-btn")?.addEventListener("click", () => {
  toggleNavItem("pnl-dashboard-view", openPnlDashboard);
});
$("rating-btn")?.addEventListener("click", () => {
  toggleNavItem("rating-view", openRating);
});
$("history-btn")?.addEventListener("click", () => {
  toggleNavItem("history-view", openHistory);
});
$("charts-btn")?.addEventListener("click", () => {
  toggleNavItem("charts-view", openCharts);
});
$("report-btn")?.addEventListener("click", () => {
  toggleNavItem("report-view", openReport);
});
$("info-btn").addEventListener("click", () => {
  toggleNavItem("info-modal", openInfo);
});
$("guide-btn")?.addEventListener("click", () => {
  toggleNavItem("guide-modal", openGuide);
});
$("settings-btn").addEventListener("click", () => {
  toggleNavItem("settings-view", openSettings);
});
$("logs-btn").addEventListener("click", () => {
  toggleNavItem("logs-view", openLogs);
});
$("changelog-btn")?.addEventListener("click", () => {
  toggleNavItem("changelog-view", openChangelog);
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
    if ($("pnl-dashboard-view") && !$("pnl-dashboard-view").classList.contains("hidden")) {
      closePnlDashboard();
      return;
    }
    if ($("rating-view") && !$("rating-view").classList.contains("hidden")) {
      closeRating();
      return;
    }
    if ($("stats-view") && !$("stats-view").classList.contains("hidden")) {
      closeStats();
      return;
    }
    if ($("charts-view") && !$("charts-view").classList.contains("hidden")) {
      closeCharts();
      return;
    }
    if ($("history-view") && !$("history-view").classList.contains("hidden")) {
      closeHistory();
      return;
    }
    if ($("settings-view") && !$("settings-view").classList.contains("hidden")) {
      closeSettings();
      return;
    }
    closeMobileMenu();
    closeInfo();
    closeGuide();
    closeBybitGridModal();
  }
});

window.addEventListener("resize", () => {
  if (!isMobileMenuMode()) closeMobileMenu();
});
$("bybitgrid-history-toggle")?.addEventListener("click", toggleBybitGridHistory);
$("info-close").addEventListener("click", closeInfo);
$("info-modal").querySelector(".modal-backdrop").addEventListener("click", closeInfo);
$("guide-close")?.addEventListener("click", closeGuide);
$("guide-modal")?.querySelector(".modal-backdrop")?.addEventListener("click", closeGuide);
$("settings-back-btn")?.addEventListener("click", closeSettings);

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
showTelegramLoginHint();

function enterAppAfterLogin() {
  $("login-screen").classList.add("hidden");
  $("app-screen").classList.remove("hidden");
  loadStrategySettings()
    .catch(() => {})
    .finally(() => refreshAll());
}

if (savedSession) {
  applyStoredCreds(savedSession);
  loginAll()
    .then(() => enterAppAfterLogin())
    .catch(() => {
      clearSession();
      // Session expired: try Telegram Mini App silent login
      tryTelegramWebAppLogin()
        .then((ok) => {
          if (ok) enterAppAfterLogin();
        })
        .catch(() => {});
    });
} else if (window.Telegram?.WebApp?.initData) {
  tryTelegramWebAppLogin()
    .then((ok) => {
      if (ok) enterAppAfterLogin();
    })
    .catch(() => {});
}

function closeTelegramOverlay() {
  if (isNavSurfaceOpen("changelog-view")) {
    closeChangelog();
    return true;
  }
  if (isNavSurfaceOpen("logs-view")) {
    closeLogs();
    return true;
  }
  if (isNavSurfaceOpen("pnl-dashboard-view")) {
    closePnlDashboard();
    return true;
  }
  if (isNavSurfaceOpen("rating-view")) {
    closeRating();
    return true;
  }
  if (isNavSurfaceOpen("history-view")) {
    closeHistory();
    return true;
  }
  if (isNavSurfaceOpen("report-view")) {
    closeReport();
    return true;
  }
  if (isNavSurfaceOpen("settings-view")) {
    closeSettings();
    return true;
  }
  if (isNavSurfaceOpen("stats-view")) {
    closeStats();
    return true;
  }
  if (isNavSurfaceOpen("info-modal")) {
    closeInfo();
    return true;
  }
  if (isNavSurfaceOpen("guide-modal")) {
    closeGuide();
    return true;
  }
  if (isNavSurfaceOpen("bybitgrid-modal")) {
    closeBybitGridModal();
    return true;
  }
  if ($("topbar")?.classList.contains("is-menu-open")) {
    closeMobileMenu();
    return true;
  }
  return false;
}

function telegramOverlayOpen() {
  const ids = [
    "changelog-view",
    "logs-view",
    "pnl-dashboard-view",
    "rating-view",
    "history-view",
    "report-view",
    "settings-view",
    "stats-view",
    "info-modal",
    "guide-modal",
    "bybitgrid-modal",
  ];
  if (ids.some((id) => isNavSurfaceOpen(id))) return true;
  return !!$("topbar")?.classList.contains("is-menu-open");
}

function syncTelegramBackButton() {
  const back = window.Telegram?.WebApp?.BackButton;
  if (!back) return;
  if (telegramOverlayOpen()) back.show();
  else back.hide();
}

function bindTelegramShell() {
  const tg = window.Telegram?.WebApp;
  if (!tg?.initData || bindTelegramShell.done) return;
  bindTelegramShell.done = true;
  if (tg.BackButton) {
    tg.BackButton.onClick(() => {
      closeTelegramOverlay();
      syncTelegramBackButton();
    });
  }
  const watch = [
    "changelog-view",
    "logs-view",
    "pnl-dashboard-view",
    "rating-view",
    "history-view",
    "report-view",
    "settings-view",
    "stats-view",
    "info-modal",
    "guide-modal",
    "bybitgrid-modal",
    "topbar",
  ];
  const obs = new MutationObserver(() => syncTelegramBackButton());
  watch.forEach((id) => {
    const el = $(id);
    if (el) obs.observe(el, { attributes: true, attributeFilter: ["class"] });
  });
  if (tg.onEvent) {
    tg.onEvent("deactivated", () => {
      telegramPaused = true;
    });
    tg.onEvent("activated", () => {
      telegramPaused = false;
      lastHeavyRefreshAt = 0;
      refreshAll({ heavy: true }).catch(() => {});
    });
  }
  syncTelegramBackButton();
}

bindTelegramShell();

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") return;
  lastHeavyRefreshAt = 0;
  refreshAll({ heavy: true }).catch(() => {});
});

setInterval(() => {
  refreshAll().catch(() => {});
}, 10000);
setInterval(() => {
  if (loadStoredSession() && $("app-screen") && !$("app-screen").classList.contains("hidden")) {
    reloginFromStorage().catch(() => {});
  }
}, 10 * 60 * 1000);

bindCollapsibleSections();
bindUiHints();
updateBotSettingsSummaries();
bindTradeLists();
bindBoardSwitch();
bindLlmControls();
bindLlm50Controls();
bindCursorControls();
bindStrategyRunScanButton();
bindPanelStatsButtons();
bindStatsPeriodButtons();
bindHistoryControls();
bindChartsControls();
bindReportControls();
bindPnlDashboardControls();
bindRatingControls();
renderStrategyRiskControls();
renderTestStrategyRiskControls();
renderDualHedgeControl();
