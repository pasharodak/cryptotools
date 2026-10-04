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
const STAKE_EDITABLE_BOTS = new Set(["grid", "strategy"]);
const FINDER_STATS_LABEL = "ML Finder (Transformer)";
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
let mlConfidenceDefaults = {};
let mlConfidenceChoices = [0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95];
let strategyRisk = { stoploss_pct: 5, take_profit_pct: 10 };
let testSettings = {
  max_open_trades: 8,
  max_open_trades_per_strategy: 2,
  stake_amount: 5,
  stoploss_pct: 2,
  take_profit_pct: 2,
};
let dualHedgeEnabled = false;
let allowedBlocks = null; // null = all
let allowedStrategies = null; // null = all
let permissionCatalog = { blocks: [], strategies: [] };
let isImpersonating = false;
let impersonatedBy = null;

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
const LIMIT_PIN_MS = 120000;
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

function applyPinnedMaxTrades(bot, fromLive) {
  const pin = limitPins.maxTrades[bot];
  if (pin && Date.now() < pin.until) {
    BOTS[bot].maxTrades = pin.value;
    return pin.value;
  }
  const n = Number(fromLive);
  if (Number.isFinite(n) && n >= 0) {
    BOTS[bot].maxTrades = Math.round(n);
  }
  return BOTS[bot].maxTrades;
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
  // null = unrestricted; [] = none
  allowedBlocks = me?.allowed_blocks === undefined ? null : me.allowed_blocks;
  allowedStrategies = me?.allowed_strategies === undefined ? null : me.allowed_strategies;
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

function userMayUseBlock(blockId) {
  if (allowedBlocks == null) return true;
  return allowedBlocks.includes(blockId);
}

function applyBlockVisibility() {
  document.querySelectorAll("[data-block]").forEach((el) => {
    const block = el.getAttribute("data-block");
    if (!block) return;
    el.classList.toggle("hidden", !userMayUseBlock(block));
  });
}

function updateMultiUserUi(me) {
  const role = me?.user?.role || me?.role || currentUser?.role || "";
  const secrets = me?.secrets;
  const banner = $("secrets-banner");
  if (banner) {
    // Show for tenants (incl. while admin impersonates) when secrets missing
    const showSecretsBanner =
      (role !== "admin" || isImpersonating) && secrets && secrets.has_secrets === false;
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
  document.querySelectorAll("[data-admin-only]").forEach((el) => {
    el.classList.toggle("hidden", !isAdminUser());
  });
  // Secrets form is available for admin and tenants (Live / Demo keys).
  document.querySelectorAll("[data-user-secrets]").forEach((el) => {
    el.classList.remove("hidden");
  });
  const statusEl = $("secrets-status");
  if (statusEl && secrets) {
    const profiles = Array.isArray(secrets.profiles) ? secrets.profiles : [];
    const active = profiles.find((p) => p.active);
    let base = "Ключи Bybit не заданы — добавьте API Key / Secret ниже";
    if (profiles.length) {
      base = active
        ? `Торгуем на: ${active.name} (${active.mode_label || (active.demo_trading ? "Demo" : "Live")})`
        : `Сохранено профилей: ${profiles.length} — выберите аккаунт ниже`;
    }
    if (secrets.can_trade === false && secrets.trade_block_reason) {
      statusEl.textContent = `${base}. ⚠ ${secrets.trade_block_reason}`;
      statusEl.classList.add("secrets-status-warn");
    } else if (secrets.can_trade === true) {
      statusEl.textContent = `${base}. Торговля разрешена ключом.`;
      statusEl.classList.remove("secrets-status-warn");
    } else {
      statusEl.textContent = base;
      statusEl.classList.remove("secrets-status-warn");
    }
  }
  const liveRadio = $("secrets-mode-live");
  const demoRadio = $("secrets-mode-demo");
  if (liveRadio && demoRadio && secrets && typeof secrets.demo_trading === "boolean") {
    liveRadio.checked = !secrets.demo_trading;
    demoRadio.checked = !!secrets.demo_trading;
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
  if (!username || !password) throw new Error("auth");

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
  const d = parseAppInstant(raw);
  if (!d) return String(raw).slice(0, 16);
  return fmtAppDateShort(d);
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
    note: "Закрытые сделки основных стратегий по enter_tag (без тестового блока). Входы с ML gate.",
  },
  test_strategy: {
    title: "Статистика — тестовые стратегии",
    note: "Только тестовый блок (#101–115: top-10 + #31/#32–35 клоны). Тот же strategy-бот.",
  },
  grid: {
    title: "Статистика — Grid + ML gate",
    note: "Закрытые сделки Grid (VolatilityGridStrategy, ML gate live_grid), даже если бот остановлен.",
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
  // Shared strategy still respects signal/executor health
  if (bot === "strategy" && sharedStackMode && st === "RUNNING") {
    const sig = sharedStackStatus.signal_engine || {};
    const exe = sharedStackStatus.trade_executor || {};
    if (!sig.active || !exe.active) hint = "offline";
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

async function setBotEnabled(bot, enabled) {
  // Optimistic
  botsControl[bot] = {
    ...(botsControl[bot] || {}),
    bot,
    desired: !!enabled,
    enabled: !!enabled,
    status: enabled ? "STARTING" : "STOPPING",
  };
  tradingFlags = { ...tradingFlags, [bot]: !!enabled };
  paintControlState(bot);
  const snap = await pairConfigApi(`/bots/${bot}`, "PATCH", { enabled: !!enabled });
  if (snap?.bot) {
    botsControl[bot] = snap;
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
  if (!sig.active || !exe.active) return "offline";
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
let pnlDashPeriod = "all";
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

function isTestCatalogEntry(s) {
  if (!s) return false;
  if (s.ui_panel) return s.ui_panel === "test";
  return !!(s.test_group || s.testGroup);
}

function isTestStatsRow(row, catalog = strategyCatalog) {
  const list = catalog || strategyCatalog || [];
  const ids = new Set(list.filter(isTestCatalogEntry).map((s) => s.id));
  if (ids.has(row.key)) return true;
  const fromCatalog = list.find((s) => strategyCatalogLabel(s) === row.name);
  return !!(fromCatalog && isTestCatalogEntry(fromCatalog));
}

function filterStatsRows(rows, scope, catalog = strategyCatalog) {
  if (scope === "all") return rows;
  if (scope === "finder") return rows.filter((r) => r.name === FINDER_STATS_LABEL);
  if (scope === "grid") return rows.filter((r) => r.name === GRID_STATS_LABEL);
  if (scope === "bybitgrid") return rows.filter((r) => r.name === BYBIT_GRID_STATS_LABEL);
  if (scope === "strategy") {
    return rows.filter((r) => !isBotStatsLabel(r.name) && !isTestStatsRow(r, catalog));
  }
  if (scope === "test_strategy") {
    return rows.filter((r) => isTestStatsRow(r, catalog));
  }
  return rows;
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
  const ftBots =
    scope === "all"
      ? ["finder", "strategy", "grid"]
      : scope === "bybitgrid" || scope === "test_strategy"
        ? []
        : [scope];

  if (ftBots.includes("finder")) {
    for (const t of filterClosedTrades(finderTrades, period, dateFrom, dateTo)) {
      entries.push({ kind: "trade", bot: "finder", trade: t });
    }
  }
  if (ftBots.includes("strategy") || scope === "test_strategy") {
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
  if (scope === "test_strategy" || scope === "strategy") {
    const testIds = new Set(
      (strategyCatalog || []).filter(isTestCatalogEntry).map((s) => s.id)
    );
    filtered = filtered.filter((entry) => {
      if (entry.kind !== "trade" || entry.bot !== "strategy") return false;
      const sid = tradeSourceId(entry.trade, entry.bot);
      const isTest = testIds.has(sid);
      return scope === "test_strategy" ? isTest : !isTest;
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
  const d = parseAppInstant(raw);
  if (!d) return String(raw);
  return fmtAppDateTime(d);
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

function historyCardHtml(entry) {
  const botClass =
    entry.kind === "bybit"
      ? "bot-bybitgrid"
      : entry.bot === "finder"
        ? "bot-finder"
        : entry.bot === "grid"
          ? "bot-grid"
          : entry.bot === "strategy" && isTestTrade(entry.trade, "strategy")
            ? "bot-test"
            : "bot-strategy";
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
  const { abs, pct } = closedTradePnl(t);
  const side = t.is_short ? "SHORT" : "LONG";
  const source = tradeSourceLabel(t, entry.bot);
  const mlConf = fmtMlConfidence(t);
  const mlChip =
    mlConf && mlConf !== "—"
      ? `<span class="history-chip history-chip-ml" title="${escapeHtml(fmtMlConfidenceDetail(t))}">ML ${escapeHtml(mlConf)}</span>`
      : `<span class="history-chip history-chip-ml is-missing" title="Фактическая уверенность ML недоступна">ML —</span>`;
  return `<article class="history-card ${pnlClass(abs)} ${botClass}">
    <div class="history-card-top">
      <div class="history-card-main">
        <strong class="history-card-pair">${escapeHtml(t.pair || "—")}</strong>
        <span class="history-card-meta">${fmtTradeDate(t)} · ${historyBotLabel(entry)}</span>
      </div>
      <div class="history-card-pnl ${pnlClass(abs)}">
        <span>${fmtUsdSigned(abs, 2)}</span>
        <span class="history-card-pct muted">${fmtPct(pct)}</span>
      </div>
    </div>
    <div class="history-card-tags">
      <span class="history-chip side-${t.is_short ? "short" : "long"}">${side}</span>
      <span class="history-chip">${escapeHtml(source)}</span>
      ${mlChip}
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
        '<tr class="stats-history-empty"><td colspan="8">Нет закрытых сделок по выбранным фильтрам</td></tr>';
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
          <td class="history-pnl-cell ${pnlClassName}">${fmtUsdSigned(pnl, 2)}</td>
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
          const mlTitle = escapeHtml(fmtMlConfidenceDetail(t));
          const botClass =
            entry.bot === "finder"
              ? "bot-finder"
              : entry.bot === "grid"
                ? "bot-grid"
                : entry.bot === "strategy" && isTestTrade(t, "strategy")
                  ? "bot-test"
                  : "bot-strategy";
          return `<tr class="history-row ${botClass}">
        <td>${fmtTradeDate(t)}</td>
        <td><span class="history-bot-badge">${botLabel}</span></td>
        <td class="history-pair-cell">${escapeHtml(t.pair || "—")}</td>
        <td><span class="history-side-badge side-${t.is_short ? "short" : "long"}">${side}</span></td>
        <td>${escapeHtml(source)}</td>
        <td title="${mlTitle}">${escapeHtml(mlConf)}</td>
        <td class="history-pnl-cell ${pnlClassName}">${fmtUsdSigned(abs, 2)}<br><span class="muted history-pnl-pct">${fmtPct(pct)}</span></td>
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
      historyScope !== "all" && historyScope !== "strategy" && historyScope !== "test_strategy"
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
    statusEl.textContent = `Всего ${entries.length} сделок${pageNote}${warn}`;
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
  if (historyScope !== "all" && historyScope !== "strategy" && historyScope !== "test_strategy") {
    historyStrategyFilter = "all";
  }
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
  document.querySelectorAll(".pnl-dash-period").forEach((btn) => {
    btn.classList.toggle("is-active", (btn.dataset.pnlDashPeriod || "all") === pnlDashPeriod);
  });
}

function renderPnlDashboard(data) {
  if (!data) return;
  updatePnlDashFilterUi();

  const catalog = data.catalog || strategyCatalog;
  const finderFiltered = filterClosedTrades(data.finderTrades, pnlDashPeriod);
  const stratFiltered = filterClosedTrades(data.stratTrades, pnlDashPeriod);
  const gridFiltered = filterClosedTrades(data.gridTrades, pnlDashPeriod);
  const bybitFiltered = filterBybitHistory(data.bybitHistory, pnlDashPeriod);
  const rows = filterStatsRows(
    buildStatsRows(finderFiltered, stratFiltered, gridFiltered, bybitFiltered, catalog),
    pnlDashScope,
    catalog
  );

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
    const periodLabel = STATS_PERIOD_LABELS[pnlDashPeriod] || STATS_PERIOD_LABELS.all;
    statusEl.textContent =
      totalClosed > 0
        ? `Закрытых операций: ${totalClosed} · период: ${periodLabel}`
        : `Нет закрытых операций · период: ${periodLabel}`;
  }
}

async function loadPnlDashboard(force = false) {
  const statusEl = $("pnl-dashboard-status");
  if (statusEl) statusEl.textContent = "Загрузка…";
  try {
    if (!force && (pnlDashDataCache || statsDataCache || historyDataCache)) {
      pnlDashDataCache = pnlDashDataCache || statsDataCache || historyDataCache;
    } else {
      pnlDashDataCache = await fetchStatsData(CLOSED_TRADES_FETCH_LIMIT);
      statsDataCache = pnlDashDataCache;
    }
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

function setPnlDashPeriod(period) {
  pnlDashPeriod = normalizeStatsPeriod(period);
  if (pnlDashDataCache) renderPnlDashboard(pnlDashDataCache);
  else loadPnlDashboard(false);
}

function bindPnlDashboardControls() {
  document.querySelectorAll(".pnl-dash-scope").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setPnlDashScope(btn.dataset.pnlDashScope || "all"));
  });
  document.querySelectorAll(".pnl-dash-period").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setPnlDashPeriod(btn.dataset.pnlDashPeriod || "all"));
  });
  $("pnl-dashboard-back-btn")?.addEventListener("click", closePnlDashboard);
  $("pnl-dashboard-refresh-btn")?.addEventListener("click", () => loadPnlDashboard(true));
}

let ratingPeriod = "all";
let ratingScope = "strategies"; // strategies | pairs
let ratingUserFilter = ""; // "" = all accounts (admin only)
let ratingDataCache = {}; // key: `${scope}:${user||"all"}`
let ratingSortKey = "pnl";
let ratingSortDesc = true;
const ratingExpandedPairs = new Set();

function ratingCacheKey(scope = ratingScope, user = ratingUserFilter) {
  return `${scope}:${user || "all"}`;
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
  if (!isAdminUser()) {
    group.classList.add("hidden");
    ratingUserFilter = "";
    sel.value = "";
    return;
  }
  group.classList.remove("hidden");
  try {
    const data = await pairConfigApi("/users");
    const users = Array.isArray(data.users) ? data.users : [];
    const prev = ratingUserFilter;
    const options = [`<option value="">Все аккаунты</option>`];
    for (const u of users) {
      const id = String(u.id || "");
      if (!id) continue;
      const name = String(u.username || id);
      const isAdm = u.role === "admin" || id === "admin";
      const label = isAdm ? `Главный (${name})` : name;
      options.push(`<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`);
    }
    sel.innerHTML = options.join("");
    const ids = new Set(users.map((u) => String(u.id || "")));
    ratingUserFilter = prev && ids.has(prev) ? prev : "";
    sel.value = ratingUserFilter;
  } catch {
    // Keep current options if /users fails
  }
}

function syncRatingUserFilterVisibility() {
  const group = $("rating-user-group");
  if (!group) return;
  if (!isAdminUser()) {
    group.classList.add("hidden");
    if (ratingUserFilter) {
      ratingUserFilter = "";
      const sel = $("rating-user-filter");
      if (sel) sel.value = "";
    }
  } else {
    group.classList.remove("hidden");
  }
}

function hideMainViewsForOverlay() {
  $("changelog-view")?.classList.add("hidden");
  $("logs-view")?.classList.add("hidden");
  $("settings-view")?.classList.add("hidden");
  $("history-view")?.classList.add("hidden");
  $("pnl-dashboard-view")?.classList.add("hidden");
  $("pnl-dashboard-view")?.setAttribute("aria-hidden", "true");
  $("rating-view")?.classList.add("hidden");
  $("rating-view")?.setAttribute("aria-hidden", "true");
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
  updateRatingFilterUi();
  loadRating(true);
}

function setRatingScope(scope) {
  const next = scope === "pairs" ? "pairs" : "strategies";
  if (ratingScope === next) return;
  ratingScope = next;
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

function updateRatingFilterUi() {
  const isPairs = ratingScope === "pairs";
  syncRatingUserFilterVisibility();
  document.querySelectorAll(".rating-scope").forEach((btn) => {
    const active = btn.dataset.ratingScope === ratingScope;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll(".rating-period").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.ratingPeriod === ratingPeriod);
  });
  const sel = $("rating-user-filter");
  if (sel && sel.value !== ratingUserFilter) sel.value = ratingUserFilter;

  const title = $("rating-title");
  const subtitle = $("rating-subtitle");
  const account = ratingAccountLabel();
  if (title) title.textContent = isPairs ? "Рейтинг пар" : "Рейтинг стратегий";
  if (subtitle) {
    subtitle.textContent = isPairs
      ? `${account} · strategy / finder / grid · клик по паре — детализация`
      : `${account} · закрытые сделки strategy-бота`;
  }

  const labelTh = $("rating-col-label");
  if (labelTh) {
    labelTh.dataset.labelBase = isPairs ? "Пара" : "Стратегия";
  }

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
  if (key === "label") return String(row.label || row.pair || row.strategy_id || "").toLowerCase();
  if (key === "rank") return Number(row.rank || 0);
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
    // strings asc first; numbers (pnl/trades/...) desc first
    ratingSortDesc = key !== "label";
  }
  const cached = ratingDataCache[ratingCacheKey()];
  if (cached) renderRating(cached);
  else loadRating(false);
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
      <td class="num ${incomeCls}">${fmtUsd(income)}</td>
      <td class="num ${lossCls}">${lossAbs > 0 ? fmtUsd(-lossAbs) : fmtUsd(0)}</td>
      <td class="num rating-net ${pnlCls}"><strong>${fmtUsd(pnl)}</strong></td>`,
  };
}

function renderRatingDetailRows(pairId, details) {
  if (!Array.isArray(details) || !details.length) {
    return `<tr class="rating-detail-row" data-pair="${escapeHtml(pairId)}">
      <td></td>
      <td colspan="6" class="muted rating-detail-empty">Нет детализации</td>
    </tr>`;
  }
  return details
    .map((d) => {
      const m = ratingMetricCells(d);
      const strat = escapeHtml(d.strategy_label || d.label || d.strategy_id || "—");
      const bot = escapeHtml(d.bot_label || d.bot || "");
      return `<tr class="rating-detail-row" data-pair="${escapeHtml(pairId)}">
        <td class="rating-detail-pad"></td>
        <td class="rating-detail-label">
          <span class="rating-detail-strategy">${strat}</span>
          ${bot ? `<span class="rating-detail-bot">${bot}</span>` : ""}
        </td>
        ${m.cells}
      </tr>`;
    })
    .join("");
}

function renderRating(data) {
  updateRatingFilterUi();
  const rows = sortedRatingRows(data);
  const body = $("rating-table-body");
  const status = $("rating-status");
  const summary = $("rating-summary");
  if (!body) return;
  const isPairs = ratingScope === "pairs";

  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="7" class="stats-history-empty">Нет закрытых сделок за период</td></tr>`;
  } else if (!isPairs) {
    body.innerHTML = rows
      .map((r, idx) => {
        const m = ratingMetricCells(r);
        const place = idx + 1;
        const placeCls =
          place === 1 ? "is-gold" : place === 2 ? "is-silver" : place === 3 ? "is-bronze" : "";
        return `<tr class="rating-row ${placeCls}">
          <td class="rating-place"><span class="rating-place-badge">${place}</span></td>
          <td class="rating-strategy">${escapeHtml(r.label || r.strategy_id || "—")}</td>
          ${m.cells}
        </tr>`;
      })
      .join("");
  } else {
    body.innerHTML = rows
      .map((r, idx) => {
        const m = ratingMetricCells(r);
        const place = idx + 1;
        const placeCls =
          place === 1 ? "is-gold" : place === 2 ? "is-silver" : place === 3 ? "is-bronze" : "";
        const pairId = String(r.pair || r.label || "");
        const expanded = ratingExpandedPairs.has(pairId);
        const details = Array.isArray(r.details) ? r.details : [];
        const chevron = expanded ? "▾" : "▸";
        const main = `<tr class="rating-row rating-pair-row ${placeCls} ${expanded ? "is-expanded" : ""}" data-pair="${escapeHtml(pairId)}" tabindex="0" role="button" aria-expanded="${expanded ? "true" : "false"}">
          <td class="rating-place"><span class="rating-place-badge">${place}</span></td>
          <td class="rating-strategy rating-pair-label">
            <span class="rating-expand-icon" aria-hidden="true">${chevron}</span>
            <span>${escapeHtml(pairId || "—")}</span>
            <span class="rating-pair-meta muted">${details.length} ист.</span>
          </td>
          ${m.cells}
        </tr>`;
        return expanded ? main + renderRatingDetailRows(pairId, details) : main;
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
  }

  const periodLabel = STATS_PERIOD_LABELS[ratingPeriod] || STATS_PERIOD_LABELS.all;
  const users = data?.users ?? 0;
  const trades = data?.trades_total ?? 0;
  const entityLabel = isPairs ? "Пар" : "Стратегий";
  const entityCount = isPairs
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
    `;
  }
  if (status) {
    status.textContent = isPairs
      ? "Клик по паре — стратегии и боты · PnL = сумма плюсов · Убытки = сумма минусов · Чистый PnL = итог"
      : "Клик по заголовку — сортировка · PnL = сумма плюсов · Убытки = сумма минусов · Чистый PnL = итог";
  }
}

async function loadRating(force) {
  const status = $("rating-status");
  if (status) status.textContent = "Загрузка…";
  updateRatingFilterUi();
  const scope = ratingScope;
  const user = isAdminUser() ? ratingUserFilter : "";
  const cacheKey = ratingCacheKey(scope, user);
  try {
    const cached = ratingDataCache[cacheKey];
    if (!force && cached && cached.period === ratingPeriod && (cached.user_filter || "") === (user || "")) {
      renderRating(cached);
      return;
    }
    const params = new URLSearchParams({
      period: ratingPeriod,
      limit: "100",
    });
    if (user) params.set("user", user);
    const endpoint =
      scope === "pairs" ? `/pair-rating?${params}` : `/strategy-rating?${params}`;
    const data = await pairConfigApi(endpoint);
    ratingDataCache[cacheKey] = data;
    if (ratingScope === scope && (isAdminUser() ? ratingUserFilter : "") === user) {
      renderRating(data);
    }
  } catch (e) {
    if (e.message === "auth") logout();
    else if (status) status.textContent = formatApiError(e.message);
  }
}

function bindRatingControls() {
  document.querySelectorAll(".rating-period").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => setRatingPeriod(btn.dataset.ratingPeriod || "all"));
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
  const p = Number(trade.close_profit_abs ?? trade.profit_abs ?? 0);
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
    const periodLabel = STATS_PERIOD_LABELS[statsPeriod] || STATS_PERIOD_LABELS.all;
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
async function fetchStatsData(_tradeLimit = CLOSED_TRADES_FETCH_LIMIT, { force = false } = {}) {
  const qs = force ? "?force=1" : "";
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

async function prefetchStatsBundle({ force = false } = {}) {
  const now = Date.now();
  if (!force && statsDataCache && now - lastStatsPrefetchAt < STATS_PREFETCH_MS) {
    if (statsDataCache.openSummary) {
      openSummaryCache = statsDataCache.openSummary;
      renderTradesSummaryFromOpen(openSummaryCache);
    }
    return statsDataCache;
  }
  if (statsPrefetchInFlight) return statsPrefetchInFlight;
  statsPrefetchInFlight = (async () => {
    try {
      const data = await fetchStatsData(CLOSED_TRADES_FETCH_LIMIT, { force });
      statsDataCache = data;
      historyDataCache = data;
      if (!pnlDashDataCache) pnlDashDataCache = data;
      lastStatsPrefetchAt = Date.now();
      if (data.openSummary) {
        openSummaryCache = data.openSummary;
        renderTradesSummaryFromOpen(openSummaryCache);
      }
      return data;
    } finally {
      statsPrefetchInFlight = null;
    }
  })();
  return statsPrefetchInFlight;
}

async function loadStatistics() {
  const summaryEl = $("stats-summary");
  const bodyEl = $("stats-table-body");
  if (!bodyEl) return;

  if (statsDataCache) {
    renderStatsModal(statsDataCache);
  } else {
    summaryEl.innerHTML = '<p class="muted">Загрузка…</p>';
    bodyEl.innerHTML = "";
  }

  try {
    statsDataCache = await prefetchStatsBundle();
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
  if (statsDataCache) renderStatsModal(statsDataCache);
  else loadStatistics();
}

function openStats(scope = "all", period = null) {
  statsScope = STATS_SCOPE_META[scope] ? scope : "all";
  if (period) statsPeriod = normalizeStatsPeriod(period);
  $("stats-modal").classList.remove("hidden");
  $("stats-modal").setAttribute("aria-hidden", "false");
  if (statsDataCache) renderStatsModal(statsDataCache);
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
  trainedRiskAvailable = data.trained_risk_available || {};
  trainedRiskSpecs = data.trained_risk_specs || {};
  mlConfidenceDefaults = data.ml_confidence_defaults || {};
  if (Array.isArray(data.ml_confidence_choices) && data.ml_confidence_choices.length) {
    mlConfidenceChoices = data.ml_confidence_choices.map(Number);
  }
  const enabled = data.enabled || {};
  const inverted = data.inverted || {};
  const trained = data.trained_risk || {};
  const mlConf = data.ml_confidence || {};
  for (const s of strategyCatalog) {
    enabledStrategies[s.id] = !!enabled[s.id];
    invertedStrategies[s.id] = !!inverted[s.id];
    trainedRiskStrategies[s.id] = trained[s.id] != null ? !!trained[s.id] : !!trainedRiskAvailable[s.id];
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
  }
  syncStrategyRiskFromPayload(data);
  syncTestSettingsFromPayload(data);
  if (data?.dual_hedge != null) dualHedgeEnabled = !!data.dual_hedge;
  if (data?.max_open_trades_per_strategy != null) {
    applyPinnedMaxPerStrategy(data.max_open_trades_per_strategy);
    updateMaxTradesHint();
  }
  if (isSettingsOpen() && isAdminUser()) {
    renderPlacementAdmin(data);
  }
  repaintStrategyTradesFromCache();
}

function showPlacementMsg(text, isError = false) {
  const el = $("placement-msg");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("hidden", !text);
  el.classList.toggle("error", !!isError);
}

function renderPlacementAdmin(data) {
  if (!isAdminUser()) return;
  const testList = $("placement-test-list");
  const mainList = $("placement-main-list");
  if (!testList || !mainList) return;
  const catalog = data?.strategies || strategyCatalog;
  const testRows = (catalog || []).filter((s) => strategyUiPanel(s) === "test");
  const mainRows = (catalog || []).filter((s) => strategyUiPanel(s) === "main");

  const rowHtml = (s, action, label) => `
    <li class="placement-list-item">
      <span class="placement-list-name">${escapeHtml(strategyCatalogLabel(s))}</span>
      <button type="button" class="btn btn-sm primary" data-placement-action="${action}" data-strategy="${escapeHtml(s.id)}">${label}</button>
    </li>`;

  testList.innerHTML = testRows.length
    ? testRows.map((s) => rowHtml(s, "promote", "В основной")).join("")
    : `<li class="placement-list-empty muted">Пусто</li>`;
  mainList.innerHTML = mainRows.length
    ? mainRows.map((s) => rowHtml(s, "demote", "В тестовые")).join("")
    : `<li class="placement-list-empty muted">Пусто — перенесите из тестовых</li>`;

  const bind = (root) => {
    root.querySelectorAll("[data-placement-action]").forEach((btn) => {
      btn.addEventListener("click", async () => {
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
          renderPlacementAdmin(res);
          const name = strategyLabel(id);
          showPlacementMsg(
            action === "promote"
              ? `${name} → основной блок (выкл)`
              : `${name} → тестовые (выкл)`
          );
        } catch (e) {
          showPlacementMsg(e.message || String(e), true);
        } finally {
          btn.disabled = false;
        }
      });
    });
  };
  bind(testList);
  bind(mainList);
}

function syncStrategyRiskFromPayload(data) {
  const risk = data?.risk || data?.state?.risk;
  if (!risk) return;
  if (risk.stoploss_pct != null) strategyRisk.stoploss_pct = Number(risk.stoploss_pct);
  if (risk.take_profit_pct != null) strategyRisk.take_profit_pct = Number(risk.take_profit_pct);
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
  applyPinnedTestSettings();
  // Test-block max is part of strategy-bot capacity — reflect it on the Strategy card/chip.
  const tMax = Number(testSettings.max_open_trades) || 0;
  if (tMax > Number(BOTS.strategy.maxTrades || 0)) {
    BOTS.strategy.maxTrades = tMax;
  }
  updateMaxTradesHint();
}

function strategyRiskSummaryText() {
  const sl = Number(strategyRisk.stoploss_pct);
  const tp = Number(strategyRisk.take_profit_pct);
  if (!Number.isFinite(sl) || !Number.isFinite(tp)) return "";
  const hedge = dualHedgeEnabled ? " · dual L+S" : "";
  return `SL −${sl}% · TP +${tp}%${hedge}`;
}

function setSettingsSummary(id, text) {
  const el = $(id);
  if (el) el.textContent = text || "развернуть";
}

function closedPnlSummaryText(bot) {
  const profit = botLiveCache[bot]?.profit || botLiveCache[bot]?.closedProfitDb;
  const coin = profit?.profit_closed_coin;
  if (coin == null || !Number.isFinite(Number(coin))) return "";
  return ` · PnL ${fmtUsdSigned(coin)}`;
}

function updateBotSettingsSummaries() {
  const strat = BOTS.strategy;
  const risk = strategyRiskSummaryText();
  setSettingsSummary(
    "strategy-settings-summary",
    `макс ${strat.maxTrades} · stake ${strat.stakeAmount} USDT${risk ? ` · ${risk}` : ""}${closedPnlSummaryText("strategy")} · развернуть`
  );
  const tSl = Number(testSettings.stoploss_pct);
  const tTp = Number(testSettings.take_profit_pct);
  const tRisk =
    Number.isFinite(tSl) && Number.isFinite(tTp) ? `SL −${tSl}% · TP +${tTp}%` : "";
  setSettingsSummary(
    "test-strategy-settings-summary",
    `макс ${testSettings.max_open_trades} · stake ${testSettings.stake_amount} USDT${tRisk ? ` · ${tRisk}` : ""} · развернуть`
  );
  const grid = BOTS.grid;
  setSettingsSummary(
    "grid-settings-summary",
    `макс ${grid.maxTrades} · stake ${grid.stakeAmount} USDT${closedPnlSummaryText("grid")} · развернуть`
  );
  setSettingsSummary(
    "bybitgrid-settings-summary",
    `лимит ${bybitGridMaxBots} · invest ${bybitGridInvest} USDT · развернуть`
  );
  const finder = BOTS.finder;
  setSettingsSummary(
    "finder-settings-summary",
    `макс ${finder.maxTrades}${closedPnlSummaryText("finder")} · развернуть`
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
        else alert(formatApiError(e.message));
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
  if (s.ui_panel) return s.ui_panel === "test";
  return !!(s.test_group || s.testGroup);
}

function strategyUiPanel(s) {
  if (!s) return "hidden";
  if (s.ui_panel) return s.ui_panel;
  return isTestStrategy(s) ? "test" : "main";
}

function catalogByGroup(testGroup = false) {
  return strategyCatalog.filter((s) => {
    const panel = strategyUiPanel(s);
    if (panel === "hidden") return false;
    return testGroup ? panel === "test" : panel === "main";
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

function whitelistSummaryText(pairs) {
  if (isAllVolumePairlist()) return "top-200 по объёму · развернуть";
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
  if (summary) summary.textContent = whitelistSummaryText(pairs);
  if (bot === "strategy") {
    const testEl = $("test-strategy-pairs");
    if (testEl) testEl.textContent = pairs.join(", ") || "—";
    const testSummary = $("test-strategy-pairs-summary");
    if (testSummary) testSummary.textContent = whitelistSummaryText(pairs);
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
  const catalog = catalogByGroup(group === "test");
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
          group === "test" ? "Включить все тестовые стратегии?" : "Включить все стратегии?";
        if (!confirm(confirmMsg)) return;
        onBtn.disabled = true;
        if (offBtn) offBtn.disabled = true;
        try {
          await setAllStrategiesEnabled(true, { group });
          const msg =
            group === "test" ? "Все тестовые стратегии включены" : "Все стратегии включены";
          if (isSettingsOpen()) showPairMsg(msg);
        } catch (e) {
          if (e.message === "auth") logout();
          else alert(formatApiError(e.message));
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
            : "Выключить все стратегии? Новые входы по основным остановятся.";
        if (!confirm(confirmMsg)) return;
        onBtn.disabled = true;
        offBtn.disabled = true;
        try {
          await setAllStrategiesEnabled(false, { group });
          const msg =
            group === "test" ? "Все тестовые стратегии выключены" : "Все стратегии выключены";
          if (isSettingsOpen()) showPairMsg(msg);
        } catch (e) {
          if (e.message === "auth") logout();
          else alert(formatApiError(e.message));
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
}

function renderBulkMlConfidenceControlsFor(ids, { group = "main" } = {}) {
  bindMlConfidenceMenusOnce();
  const catalog = catalogByGroup(group === "test");
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
          else alert(formatApiError(e.message));
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
          else alert(formatApiError(e.message));
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
}

function positionMlConfidenceMenu(wrap, menu) {
  if (!wrap || !menu || menu.hidden) return;
  const btn = wrap.querySelector(
    "[data-strategy-ml-confidence-btn], [data-ml-conf-bulk-btn]"
  );
  const anchor = btn || wrap;
  const rect = anchor.getBoundingClientRect();
  const pad = 8;
  const viewW = window.innerWidth || document.documentElement.clientWidth;
  const viewH = window.innerHeight || document.documentElement.clientHeight;
  const width = Math.min(rect.width, viewW - pad * 2);
  const maxH = Math.min(14 * 16, viewH * 0.45, 224);

  menu.style.position = "fixed";
  menu.style.right = "auto";
  menu.style.bottom = "auto";
  menu.style.zIndex = "220";
  menu.style.width = `${Math.round(width)}px`;
  menu.style.maxWidth = `${Math.round(viewW - pad * 2)}px`;
  menu.style.maxHeight = `${Math.round(maxH)}px`;
  menu.style.left = `${Math.round(Math.max(pad, Math.min(rect.left, viewW - pad - width)))}px`;
  menu.style.top = "0px";

  const menuH = Math.min(menu.scrollHeight || maxH, maxH);
  const spaceBelow = viewH - pad - rect.bottom;
  const spaceAbove = rect.top - pad;
  let top = rect.bottom + 4;
  menu.classList.remove("opens-up");
  if (menuH > spaceBelow && spaceAbove > spaceBelow) {
    top = rect.top - 4 - menuH;
    menu.classList.add("opens-up");
  }
  if (top < pad) top = pad;
  if (top + menuH > viewH - pad) {
    menu.style.maxHeight = `${Math.round(Math.max(120, viewH - pad - top))}px`;
  }
  menu.style.top = `${Math.round(top)}px`;
}

function closeAllMlConfidenceMenus(exceptId) {
  document.querySelectorAll("[data-strategy-ml-confidence-wrap]").forEach((wrap) => {
    const id = wrap.getAttribute("data-strategy-ml-confidence-wrap");
    if (exceptId && id === exceptId) return;
    const menu = wrap.querySelector("[data-strategy-ml-confidence-menu]");
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
    const menu = wrap.querySelector("[data-ml-conf-bulk-menu]");
    const btn = wrap.querySelector("[data-ml-conf-bulk-btn]");
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
    if (t.closest("[data-strategy-ml-confidence-wrap], [data-ml-conf-bulk-wrap]")) return;
    closeAllMlConfidenceMenus();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeAllMlConfidenceMenus();
  });
}

function renderStrategyTogglesInto(container, { testGroup = false } = {}) {
  if (!container) return;
  bindMlConfidenceMenusOnce();
  const catalog = catalogByGroup(testGroup);
  if (!catalog.length) {
    container.innerHTML = testGroup
      ? `<li class="strategy-toggle-item strategy-panel-empty"><span class="muted">Нет стратегий в тестовом блоке</span></li>`
      : `<li class="strategy-toggle-item strategy-panel-empty"><span class="muted">Нет стратегий в основном блоке — админ может перенести из «Тестовые» в Настройках</span></li>`;
    return;
  }
  container.innerHTML = catalog
    .map((s) => {
      const hasTrain = !!trainedRiskAvailable[s.id] || !!trainedRiskSpecs[s.id];
      const trainOn = !!trainedRiskStrategies[s.id];
      const trainHint = trainedRiskHint(s.id);
      const trainRow = hasTrain
        ? `
      <label class="strategy-invert-switch strategy-trained-risk-switch${trainOn ? " is-on" : ""}" title="SL/TP из обучения (player_scenarios). Выкл = глобальные SL/TP.">
        <span class="strategy-invert-text">SL/TP из обучения${trainHint ? ` <span class="muted">(${trainHint})</span>` : ""}</span>
        <input type="checkbox" data-strategy-trained-risk="${s.id}" ${trainOn ? "checked" : ""} />
        <span class="strategy-invert-slider" aria-hidden="true"></span>
      </label>`
        : "";
      const descHint = s.desc ? uiHintHtml(escapeHtml(s.desc)) : "";
      return `
    <li class="strategy-toggle-item${invertedStrategies[s.id] ? " is-inverted" : ""}${trainOn ? " is-trained-risk" : ""}">
      <div class="strategy-toggle-main">
        <label class="strategy-toggle-label">
          <input type="checkbox" data-strategy="${s.id}" ${enabledStrategies[s.id] ? "checked" : ""} />
          <span class="strategy-toggle-text">
            <span class="strategy-toggle-title-row">
              <strong>${strategyCatalogLabel(s)}${invertedStrategies[s.id] ? ' <span class="strategy-inv-badge">инв</span>' : ""}${trainOn ? ' <span class="strategy-train-badge">train SL/TP</span>' : ""} <span class="strategy-ml-badge">ML ${mlConfidencePct(s.id)}%</span></strong>
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
      ${renderMlConfidenceControl(s.id)}
    </li>`;
    })
    .join("");

  // Hint is inside the enable <label>; stop label activation without blocking <details>.
  container.querySelectorAll(".strategy-toggle-title-row .ui-hint").forEach((hint) => {
    hint.addEventListener("click", (ev) => ev.stopPropagation());
    hint.addEventListener("mousedown", (ev) => ev.preventDefault());
  });

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
        if (isSettingsOpen()) showPairMsg(msg);
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
        if (isSettingsOpen()) showPairMsg(msg);
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
        const msg = `${strategyLabel(id)}: SL/TP из обучения ${next ? "вкл" : "выкл (глобальные)"}`;
        if (isSettingsOpen()) showPairMsg(msg);
      } catch (e) {
        document.querySelectorAll(`input[data-strategy-trained-risk="${id}"]`).forEach((el) => {
          el.checked = !next;
        });
        if (e.message === "auth") logout();
        else alert(formatApiError(e.message));
      } finally {
        document.querySelectorAll(`input[data-strategy-trained-risk="${id}"]`).forEach((el) => {
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
      menu.hidden = !willOpen;
      btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
      wrap?.classList.toggle("is-open", willOpen);
      if (willOpen) {
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
        else alert(formatApiError(e.message));
      } finally {
        document.querySelectorAll(`[data-strategy-ml-confidence-btn="${id}"]`).forEach((el) => {
          el.disabled = false;
        });
      }
    });
  });
}

function renderStrategyToggles() {
  renderBulkEnableControls();
  renderBulkMlConfidenceControls();
  renderStrategyTogglesInto($("strategy-panel-toggles"), { testGroup: false });
  renderStrategyTogglesInto($("test-strategy-panel-toggles"), { testGroup: true });
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

function renderStrategyRiskInputHtml() {
  const sl = Number(strategyRisk.stoploss_pct);
  const tp = Number(strategyRisk.take_profit_pct);
  const hint = uiHintHtml(
    `Глобальные SL/TP (если у стратегии выкл. «SL/TP из обучения»). Диапазон: SL ${STRATEGY_SL_MIN}–${STRATEGY_SL_MAX}% · TP ${STRATEGY_TP_MIN}–${STRATEGY_TP_MAX}%.`
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
  updateBotSettingsSummaries();
}

function renderTestStrategyRiskInputHtml() {
  const sl = Number(testSettings.stoploss_pct);
  const tp = Number(testSettings.take_profit_pct);
  const hint = uiHintHtml(
    "Только тестовый блок. Если у стратегии выкл. «SL/TP из обучения» — эти значения; иначе TAG_RISK из обучения."
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
  if (!el) return;
  el.innerHTML = renderTestStrategyRiskInputHtml();
  bindTestStrategyRiskInput(el.querySelector("[data-test-strategy-risk-input]"));
  updateBotSettingsSummaries();
}

async function setTestStrategySettings(patch) {
  const data = await pairConfigApi("/pairs", "POST", {
    action: "set_test_strategy_settings",
    ...patch,
  });
  syncEnabledFromPayload(data);
  syncTestSettingsFromPayload(data);
  updateStrategyDisplay();
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
      alert(`Стоп-лосс: от ${TEST_SL_MIN}% до ${TEST_SL_MAX}%`);
      return;
    }
    if (!Number.isFinite(tp) || tp < TEST_TP_MIN || tp > TEST_TP_MAX) {
      alert(`Тейк-профит: от ${TEST_TP_MIN}% до ${TEST_TP_MAX}%`);
      return;
    }
    btn.disabled = true;
    try {
      await setTestStrategySettings({ stoploss_pct: sl, take_profit_pct: tp });
      await refreshBotSafe("strategy");
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
      if (isSettingsOpen()) {
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
  const testMax = Number(testSettings.max_open_trades) || 0;
  const stratSlots = Math.max(Number(BOTS.strategy.maxTrades) || 0, testMax);
  const activeMax = stratSlots + BOTS.grid.maxTrades + bybitGridMaxBots;
  const per =
    Number(BOTS.strategy.maxPerStrategy) > 0
      ? ` · ≤${BOTS.strategy.maxPerStrategy}/стратегия`
      : "";
  const testBit = testMax > 0 ? ` · тест ≤${testMax}` : "";
  el.textContent = `До ${activeMax} позиций: Finder — ${BOTS.finder.maxTrades}, стратегии — ${stratSlots}${per}${testBit}, Grid — ${BOTS.grid.maxTrades}, Bybit Grid — ${bybitGridMaxBots} · ML gate`;
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
      alert("Введите число");
      return;
    }
    const validation = validate ? validate(next) : true;
    if (validation !== true) {
      alert(typeof validation === "string" ? validation : "Некорректное значение");
      return;
    }
    btn.disabled = true;
    const prevLabel = btn.textContent;
    btn.textContent = "…";
    try {
      await onSave(next);
      input.value = String(Number.isInteger(next) ? next : next);
      // Soft refresh — don't block UI on full stack poll
      refreshAll().catch(() => {});
      if (isSettingsOpen()) {
        loadPairSettings().catch(() => {});
      }
    } catch (e) {
      if (e.message === "auth") logout();
      else alert(formatApiError(e.message));
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
  return `
    <div class="numeric-setting" data-max-trades-input data-bot="${bot}">
      <input type="number" class="numeric-setting-input" min="0" step="1" value="${maxTrades}" inputmode="numeric" aria-label="Макс. сделок ${BOTS[bot].label}" />
      <button type="button" class="btn btn-sm primary numeric-setting-save">Сохранить</button>
    </div>
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
  if (!el) return;
  const maxOpen = Number(testSettings.max_open_trades) || 0;
  const per = Number(testSettings.max_open_trades_per_strategy) || 0;
  const stake = Number(testSettings.stake_amount) || 5;
  const profitClosed = profit?.profit_closed_coin ?? profit?.profit_closed_percent;
  const usdt = getUsdtWallet(balance);
  const botAvail = usdt?.bot_owned;
  const walletFree = usdt?.free;
  const inMargin = usdt?.used;
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
    <div class="stat-per-strategy">
      <span class="stat-inline-label">На одну тестовую</span>
      <div class="numeric-setting" data-test-max-per-input>
        <input type="number" class="numeric-setting-input" min="0" step="1" value="${per}" inputmode="numeric" aria-label="Макс. сделок на одну тестовую стратегию" />
        <button type="button" class="btn btn-sm primary numeric-setting-save">Сохранить</button>
      </div>
      <span class="muted stat-hint">0 = без лимита по enter_tag</span>
    </div>
    <div class="stat">Прибыль (закрытые)<strong>${profit?.profit_closed_coin != null ? fmtUsd(profit.profit_closed_coin) : fmtPctRatio(profitClosed)}</strong></div>
    <div class="stat stat-stake-limit">
      <span class="stat-inline-label">Stake (тест)</span>
      <div class="numeric-setting numeric-setting-stake" data-test-stake-input>
        <input type="number" class="numeric-setting-input" min="${STAKE_MIN}" max="${STAKE_MAX}" step="1" value="${stake}" inputmode="decimal" aria-label="Stake тестовых USDT" />
        <span class="numeric-setting-suffix">USDT</span>
        <button type="button" class="btn btn-sm primary numeric-setting-save">Сохранить</button>
      </div>
    </div>
    <div class="stat" title="Сколько USDT этот бот может использовать для новой сделки (учитываются только его сделки в БД)">
      Доступно боту<strong>${botAvail != null ? fmtUsd(botAvail) : "—"}</strong>
      <span class="stat-hint">общий кошелёк ${walletFree != null ? fmtUsd(walletFree) : "—"} · в марже ${inMargin != null ? fmtUsd(inMargin) : "—"}</span>
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

function renderTestStrategyLimits() {
  /* limits rendered in #test-strategy-stats */
}

function isTestStrategyId(sid) {
  const id = String(sid || "");
  if (!id) return false;
  const fromCatalog = (strategyCatalog || []).find((s) => s.id === id);
  if (fromCatalog) return isTestCatalogEntry(fromCatalog);
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

function splitStrategyTrades(trades) {
  const list = trades || [];
  const main = [];
  const test = [];
  for (const t of list) {
    if (isTestTrade(t, "strategy")) test.push(t);
    else main.push(t);
  }
  return { main, test };
}

function renderStatsInto(el, bot, profit, balance, openCount, stakeAmount, { openLabel = null } = {}) {
  if (!el) return;
  const cfg = BOTS[bot];
  if (STAKE_EDITABLE_BOTS.has(bot) && stakeAmount != null) {
    BOTS[bot].stakeAmount = Number(stakeAmount);
  }
  const profitClosed = profit?.profit_closed_coin ?? profit?.profit_closed_percent;
  const usdt = getUsdtWallet(balance);
  const botAvail = usdt?.bot_owned;
  const walletFree = usdt?.free;
  const inMargin = usdt?.used;
  const stakeCell = STAKE_EDITABLE_BOTS.has(bot)
    ? `<div class="stat stat-stake-limit"><span class="stat-inline-label">Stake</span>${renderStakeInput(bot)}</div>`
    : `<div class="stat">Stake<strong>${fmtUsd(stakeAmount ?? 5)}</strong></div>`;
  const perStrategyBlock =
    bot === "strategy"
      ? `<div class="stat-per-strategy">
          <span class="stat-inline-label">На одну стратегию</span>
          ${renderMaxPerStrategyInput()}
          <span class="muted stat-hint">0 = без лимита</span>
        </div>`
      : "";
  const tradesTitle = openLabel || "Открыто сделок";
  el.innerHTML = `
    <div class="stat stat-trades-limit">
      ${tradesTitle}<strong>${openCount} / ${cfg.maxTrades}</strong>
      ${renderMaxTradesInput(bot)}
      ${perStrategyBlock}
    </div>
    <div class="stat">Прибыль (закрытые)<strong>${profit?.profit_closed_coin != null ? fmtUsd(profit.profit_closed_coin) : fmtPctRatio(profitClosed)}</strong></div>
    ${stakeCell}
    <div class="stat" title="Сколько USDT этот бот может использовать для новой сделки (учитываются только его сделки в БД)">
      Доступно боту<strong>${botAvail != null ? fmtUsd(botAvail) : "—"}</strong>
      <span class="stat-hint">общий кошелёк ${walletFree != null ? fmtUsd(walletFree) : "—"} · в марже ${inMargin != null ? fmtUsd(inMargin) : "—"}</span>
    </div>
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
  renderStatsInto($(BOTS[bot].statsEl), bot, profit, balance, openCount, stakeAmount);
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
    ["Открыта", t.open_date ? fmtAppDateTime(t.open_date) : "—"],
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


function renderTradesInto(el, bot, trades) {
  if (!el) return;
  const open = trades || [];
  const summaryId =
    el.id === "test-strategy-trades"
      ? "test-strategy-trades-summary"
      : el.id === "strategy-trades"
        ? "strategy-trades-summary"
        : el.id === "grid-trades"
          ? "grid-trades-summary"
          : el.id === "finder-trades"
            ? "finder-trades-summary"
            : null;
  if (summaryId) updateTradesSectionSummary(summaryId, open);

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
      const mlBadge = renderTradeMlBadge(t);
      const expanded = isTradeExpanded(bot, t.trade_id);
      const expandCls = expanded ? "is-expanded" : "is-collapsed";
      return `<article class="trade-card ${expandCls}" data-trade-id="${t.trade_id}">
        <div class="trade-summary">
          <button type="button" class="trade-toggle" aria-expanded="${expanded ? "true" : "false"}" aria-label="${expanded ? "Свернуть сделку" : "Развернуть сделку"}">
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

function bindTradeLists() {
  for (const bot of Object.keys(BOTS)) {
    bindTradeListEl($(BOTS[bot].tradesEl));
  }
  bindTradeListEl($("test-strategy-trades"));
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
    fillAll(`<p class="muted finder-disabled-note">${note}</p>`);
    return;
  }
  const slotsDisabled = Number(cfg.maxTrades) <= 0;
  if (slotsDisabled) {
    fillAll(`
      <p class="muted finder-disabled-note">Макс. сделок = 0 — бот остановлен и не торгует. Установите 1 или больше на карточке бота.</p>
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
        else alert(formatApiError(e.message));
      }
    });
    return;
  }
  const scanBtn =
    bot === "grid"
      ? `<button class="btn" data-bot="grid" data-act="scan-ranging" title="Поиск боковика среди 150 ликвидных пар">Скан боковика</button>`
      : bot === "strategy"
        ? `<button class="btn" data-bot="strategy" data-act="scan-strategy" title="Подбор пар под каждую включённую стратегию (150 ликвидных)">Скан пар</button>`
        : "";
  fillAll(`
    <button class="btn primary" data-bot="${bot}" data-act="start" ${running ? "disabled" : ""}>Старт</button>
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
          alert("Макс. сделок = 0. Установите 1 или больше в Настройках.");
          return;
        }
        btn.disabled = true;
        await setBotEnabled(bot, true);
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
        if (!confirm("Запустить скан пар по каждой включённой стратегии (150 ликвидных)? Займёт ~2–3 мин.")) return;
        btn.disabled = true;
        btn.textContent = "Скан…";
        for (const id of ["strategy-scan-info"]) {
          const info = $(id);
          if (info) info.textContent = "Скан выполняется…";
        }
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
      .map(([sid, cnt]) => `${STRATEGY_SCAN_SHORT[sid] || sid.replace(/Strategy$/, "")}:${cnt}`)
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

/** Prefer live /profit; fall back to sqlite closed_profit from open-trades bundle. */
function mergeClosedProfit(liveProfit, dbProfit) {
  const liveCount = Number(liveProfit?.trade_count ?? liveProfit?.closed_trade_count ?? 0);
  const liveCoin = liveProfit?.profit_closed_coin;
  const hasLive =
    liveProfit != null &&
    ((liveCoin != null && Number.isFinite(Number(liveCoin)) && (liveCount > 0 || Number(liveCoin) !== 0)) ||
      liveCount > 0);
  if (hasLive) return liveProfit;
  if (dbProfit && (dbProfit.trade_count > 0 || dbProfit.profit_closed_coin != null)) {
    return {
      ...(liveProfit || {}),
      profit_closed_coin: dbProfit.profit_closed_coin,
      profit_closed_fiat: dbProfit.profit_closed_fiat ?? dbProfit.profit_closed_coin,
      trade_count: dbProfit.trade_count,
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
    const { main, test } = splitStrategyTrades(openTrades);
    renderStatsInto($(BOTS.strategy.statsEl), bot, profit, balance, main.length, config?.stake_amount, {
      openLabel: "Открыто (основные)",
    });
    renderTestStrategyStats($("test-strategy-stats"), profit, balance, test.length);
    renderActions(bot, isRunning);
    renderTradesInto($(BOTS.strategy.tradesEl), bot, main);
    renderTradesInto($("test-strategy-trades"), bot, test);
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
      paintBotLive(bot, {
        config: prev.config,
        profit: prev.profit,
        balance: prev.balance,
        trades: openTrades,
        running: prev.running,
      });
      rememberBotLive(bot, { trades: openTrades });
    }
  } catch {
    /* keep prev */
  }

  if (bot === "strategy" && sharedStackMode) {
    const running = strategyRunningInSharedMode();
    const hint = sharedStrategyStateHint();
    paintBotLive(bot, {
      config: prev.config,
      profit: prev.profit,
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
        // Shared signal-engine reports max_open_trades=1; keep pair-config / bot_limits value.
        if (!(bot === "strategy" && sharedStackMode)) {
          applyPinnedMaxTrades(bot, cfg?.max_open_trades);
        }
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
  const count = Number(summary.count) || 0;
  const maxTotal = Number(summary.max_total);
  const maxLabel = Number.isFinite(maxTotal) && maxTotal > 0 ? maxTotal : totalMaxOpenSlots();
  if (!count) {
    el.classList.add("is-empty");
    el.innerHTML = `<span class="ts-label">Открытые сделки</span><strong>нет · 0 / ${maxLabel}</strong>`;
    return;
  }
  el.classList.remove("is-empty");
  const margin = Number(summary.margin) || 0;
  const pnl = Number(summary.pnl) || 0;
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
    el.classList.add("is-empty");
    el.innerHTML = `<span class="ts-label">Открытые сделки</span><strong>нет · 0 / ${maxTotal}</strong>`;
    return;
  }

  el.classList.remove("is-empty");
  const margin = allTrades.reduce((s, t) => s + Number(t.stake_amount || 0), 0);
  const pnl = allTrades.reduce(
    (s, t) => s + Number(t.total_profit_abs ?? t.profit_abs ?? 0),
    0
  );
  const pnlClassName = pnlClass(pnl);

  openSummaryCache = {
    count,
    max_total: Number(openSummaryCache?.max_total) > 0 ? Number(openSummaryCache.max_total) : maxTotal,
    margin,
    pnl,
  };

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
        alert(
          `Синхронизация: закрыто через биржу ${exited}, призраков в историю ${archived}, ` +
            `дублей убрано ${deduped}. ` +
            `Сейчас: боты ${after.ft_open_count ?? "—"} / биржа ${after.bybit_position_count ?? "—"}. ` +
            (result.ok ? "Расхождений больше нет." : "Часть расхождений осталась — обновите страницу.")
        );
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
      alert(`Макс. активных ботов — от 0 до ${BYBIT_GRID_MAX_LIMIT} (0 = не создавать новые)`);
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
      <div class="stat"><span class="muted">Суммарный PnL</span><strong class="${pnlCls}">${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)} USDT</strong></div>
      <div class="stat"><span class="muted">Funding</span><strong class="${funding.ok === false ? "loss" : ""}">${funding.fund_usdt != null ? Number(funding.fund_usdt).toFixed(1) : "—"}</strong></div>
      <div class="stat"><span class="muted">Лимит</span><strong>${maxBots}</strong></div>
    `;
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

async function refreshAll() {
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
    if (openSummaryCache) renderTradesSummaryFromOpen(openSummaryCache);

    // Instant path: pair-config open-trades (sqlite/cache) — never waits on ctbot.
    let cachedTrades = [];
    try {
      const bundle = await fetchOpenTradesCache();
      cachedTrades = paintOpenTradesFromServerBundle(bundle) || [];
    } catch (e) {
      if (e.message === "auth") throw e;
      console.warn("open-trades cache:", e.message);
    }

    renderTradesSummary(cachedTrades);
    $("last-update").textContent = `Обновлено: ${fmtAppTime(new Date())} ${APP_TZ_LABEL}`;

    // Test-block limits live in pair-config files — sync even if /strategies is slow.
    pairConfigApi("/pairs")
      .then((data) => {
        syncTestSettingsFromPayload(data);
        const strat = data?.strategy || {};
        if (strat.max_open_trades != null) applyPinnedMaxTrades("strategy", strat.max_open_trades);
        if (strat.max_open_trades_per_strategy != null) {
          applyPinnedMaxPerStrategy(strat.max_open_trades_per_strategy);
        }
        updateBotSettingsSummaries();
      })
      .catch(() => {});

    // Background: strategy flags + live bot APIs (may be slow; UI already painted).
    refreshStrategyEnabled().catch((e) => {
      if (e.message === "auth") logout();
      else console.warn("refreshStrategyEnabled:", e.message);
    });
    // Always refresh grid — shared-stack only replaces strategy trading, not Volatility Grid.
    const liveBots = ["strategy", "grid", "finder"];
    Promise.all(liveBots.map((b) => refreshBotSafe(b)))
      .then((results) => {
        const stratTrades = results[0] || [];
        const gridTrades = liveBots.includes("grid") ? results[liveBots.indexOf("grid")] || [] : [];
        const finderTrades = liveBots.includes("finder") ? results[liveBots.indexOf("finder")] || [] : [];
        renderTradesSummary([
          ...(stratTrades || []),
          ...(gridTrades || []),
          ...(finderTrades || []),
        ]);
        $("last-update").textContent = `Обновлено: ${fmtAppTime(new Date())} ${APP_TZ_LABEL}`;
      })
      .catch(() => {});

    const sideTasks = [
      refreshPairlistMode().catch(() => {}),
      refreshServerStats().catch(() => {}),
      refreshReconcileBanner().catch(() => {}),
      loadGridScanInfo().catch(() => {}),
      loadStrategyScanInfo().catch(() => {}),
    ];
    if (now - lastBybitGridRefresh >= BYBIT_GRID_REFRESH_MS) {
      sideTasks.push(refreshBybitGrid().catch(() => {}));
      lastBybitGridRefresh = now;
    }
    Promise.all(sideTasks).catch(() => {});
    prefetchStatsBundle().catch(() => {});
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
  if (finder.max_open_trades != null) applyPinnedMaxTrades("finder", finder.max_open_trades);
  if (strategy.max_open_trades != null) applyPinnedMaxTrades("strategy", strategy.max_open_trades);
  if (strategy.max_open_trades_per_strategy != null) {
    applyPinnedMaxPerStrategy(strategy.max_open_trades_per_strategy);
  }
  if (grid.max_open_trades != null) applyPinnedMaxTrades("grid", grid.max_open_trades);
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
  chk.checked = !!tradingFlags.strategy;
}

function showTelegramMsg(text, isError = false) {
  const el = $("telegram-msg");
  if (!el) return;
  el.textContent = text || "";
  el.className = isError ? "pair-msg error" : "pair-msg ok";
  el.classList.toggle("hidden", !text);
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
  showTelegramMsg(
    data.chat_id
      ? `Сохранено: chat id ${data.chat_id}. Проверьте сообщение в Telegram.`
      : "Telegram отвязан"
  );
  await loadTelegramSettings();
  return data;
}

async function saveTradingToggle(enabled) {
  const msg = $("trading-toggle-msg");
  try {
    await setBotEnabled("strategy", !!enabled);
    const data = await pairConfigApi("/trading-enabled");
    tradingFlags = data.flags || tradingFlags;
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
    renderPlacementAdmin({ strategies: strategyCatalog });
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
  creds.user = $("login-user").value.trim();
  creds.pass = $("login-pass").value;
  sessionToken = "";
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
              : `<div class="muted users-perms-hint">блоки: ${blockN} · стратегии: ${stratN}</div>`
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
    showUsersMsg(`Пользователь ${username} создан (лимиты 1/1/1). Пусть задаст ключи Bybit.`);
    await loadUsersAdmin();
  } catch (e) {
    showUsersMsg(formatApiError(e.message), true);
  }
});

$("impersonate-return-btn")?.addEventListener("click", async () => {
  try {
    await stopImpersonation();
  } catch (e) {
    alert(formatApiError(e.message));
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

$("secrets-save-btn")?.addEventListener("click", async () => {
  const key = $("secrets-api-key")?.value.trim() || "";
  const secret = $("secrets-api-secret")?.value || "";
  const demoTrading = !!$("secrets-mode-demo")?.checked;
  const name =
    $("secrets-name")?.value.trim() ||
    (demoTrading ? "Demo Trading" : "Live");
  if (!key || !secret) return showSecretsMsg("Введите API Key и Secret", true);
  try {
    const payload = {
      name,
      bybit_api_key: key,
      bybit_api_secret: secret,
      bybit_demo_trading: demoTrading,
    };
    // Admin writes own profiles; tenants / impersonation write encrypted secrets.
    if (isAdminUser() && !isImpersonating) {
      payload.user_id = "admin";
    }
    await pairConfigApi("/secrets", "PUT", payload);
    $("secrets-api-key").value = "";
    $("secrets-api-secret").value = "";
    if ($("secrets-name")) $("secrets-name").value = "";
    showSecretsMsg(
      demoTrading
        ? `«${name}» сохранён (Demo → api-demo.bybit.com)`
        : `«${name}» сохранён (Live → api.bybit.com)`
    );
    await refreshAuthMe();
  } catch (e) {
    showSecretsMsg(formatApiError(e.message), true);
  }
});

$("trading-strategy-enabled")?.addEventListener("change", async (e) => {
  const enabled = !!e.target.checked;
  try {
    await saveTradingToggle(enabled);
  } catch (err) {
    if (err.message === "auth") logout();
    e.target.checked = !enabled;
  }
});

$("telegram-chat-id")?.addEventListener("input", () => {
  if ($("telegram-chat-id")) $("telegram-chat-id").dataset.dirty = "1";
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
$("refresh-btn").addEventListener("click", () => {
  closeMobileMenu();
  refreshAll();
});
$("stats-btn").addEventListener("click", () => {
  toggleNavItem("stats-modal", () => openStats("all", "all"));
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
    if ($("history-view") && !$("history-view").classList.contains("hidden")) {
      closeHistory();
      return;
    }
    if ($("settings-view") && !$("settings-view").classList.contains("hidden")) {
      closeSettings();
      return;
    }
    closeMobileMenu();
    closeStats();
    closeInfo();
    closeGuide();
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
if (savedSession) {
  applyStoredCreds(savedSession);
  loginAll()
    .then(() => {
      $("login-screen").classList.add("hidden");
      $("app-screen").classList.remove("hidden");
      loadStrategySettings()
        .catch(() => {})
        .finally(() => refreshAll());
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
bindUiHints();
updateBotSettingsSummaries();
bindTradeLists();
bindPanelStatsButtons();
bindStatsPeriodButtons();
bindHistoryControls();
bindPnlDashboardControls();
bindRatingControls();
renderStrategyRiskControls();
renderTestStrategyRiskControls();
renderDualHedgeControl();
