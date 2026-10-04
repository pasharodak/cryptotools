#!/usr/bin/env python3
"""Bybit Futures Grid Bot API — create, monitor, close native grid bots."""
from __future__ import annotations

import contextlib
import contextvars
import copy
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

_tenant_user_data: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "bybit_tenant_user_data", default=None
)
_tenant_env_file: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "bybit_tenant_env_file", default=None
)


@contextlib.contextmanager
def tenant_bybit_context(user_data: Path | None, env_file: Path | None = None):
    t1 = _tenant_user_data.set(user_data)
    t2 = _tenant_env_file.set(env_file)
    try:
        yield
    finally:
        _tenant_user_data.reset(t1)
        _tenant_env_file.reset(t2)


BYBIT_API = "https://api.bybit.com"
BYBIT_DEMO_API = "https://api-demo.bybit.com"
GRID_MODE_LABELS = {1: "neutral", 2: "long", 3: "short"}
GRID_TYPE_LABELS = {1: "arithmetic", 2: "geometric"}
# Neutral grid with leverage often fills SL worse than configured % (empirical ~lev×0.8).
SL_LEVERAGE_OVERSHOOT = 0.8
CHECK_CODE_SUCCESS = "FGRID_CHECK_CODE_SUCCESS"

# Bybit v5 limits (per UID, per second) — keep ~20% headroom.
_RATE_MIN_INTERVAL: dict[str, float] = {
    "fgridbot": 0.11,   # 10/s
    "market": 0.022,    # ~45/s (public; IP also 600/5s)
    "trade": 0.022,     # 50/s execution, order history, closed-pnl
    "account": 0.042,   # 25/s transaction-log
    "asset": 0.035,     # 30/s fundinghistory
    "default": 0.05,
}
_rate_last_call: dict[str, float] = {}
_detail_cache: dict[str, tuple[float, dict[str, Any]]] = {}
DETAIL_CACHE_TTL = 25.0
_SYNC_TTL_SEC = 120.0
_FUNDING_IDS_TTL_SEC = 300.0
_last_light_sync = 0.0
_funding_ids_cache: tuple[float, set[str]] = (0.0, set())


def ft_base() -> Path:
    return Path(os.environ.get("CT_BASE", "/home/cryptotools/app"))


def config_path() -> Path:
    override = _tenant_user_data.get()
    if override is not None:
        return override / "bybit_grid_config.json"
    return ft_base() / "user_data" / "bybit_grid_config.json"


def state_path() -> Path:
    override = _tenant_user_data.get()
    if override is not None:
        return override / "bybit_grid_state.json"
    return ft_base() / "user_data" / "bybit_grid_state.json"


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip().rstrip("\r")
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _env_truthy(val: str | None) -> bool:
    return str(val or "").strip().lower() in ("1", "true", "t", "yes", "y", "on")


def is_demo_trading() -> bool:
    """Demo API keys must hit api-demo.bybit.com (not api.bybit.com)."""
    tenant_env = _tenant_env_file.get()
    if tenant_env is not None:
        env = load_env_file(tenant_env)
        if "BYBIT_DEMO_TRADING" in env or "CTENGINE__EXCHANGE__DEMO_TRADING" in env:
            return _env_truthy(
                env.get("BYBIT_DEMO_TRADING") or env.get("CTENGINE__EXCHANGE__DEMO_TRADING")
            )
    if os.environ.get("BYBIT_DEMO_TRADING") is not None:
        return _env_truthy(os.environ.get("BYBIT_DEMO_TRADING"))
    if os.environ.get("CTENGINE__EXCHANGE__DEMO_TRADING") is not None:
        return _env_truthy(os.environ.get("CTENGINE__EXCHANGE__DEMO_TRADING"))
    env_file = Path(os.environ.get("CT_ENV", ft_base().parent / ".cryptotools.env"))
    env = load_env_file(env_file)
    return _env_truthy(
        env.get("BYBIT_DEMO_TRADING") or env.get("CTENGINE__EXCHANGE__DEMO_TRADING")
    )


def bybit_api_base() -> str:
    return BYBIT_DEMO_API if is_demo_trading() else BYBIT_API


def get_credentials() -> tuple[str, str]:
    tenant_env = _tenant_env_file.get()
    if tenant_env is not None:
        env = load_env_file(tenant_env)
        key = env.get("BYBIT_API_KEY", "").strip()
        secret = env.get("BYBIT_API_SECRET", "").strip()
        if key and secret:
            return key, secret
    env_file = Path(os.environ.get("CT_ENV", ft_base().parent / ".cryptotools.env"))
    env = load_env_file(env_file)
    key = (
        os.environ.get("BYBIT_API_KEY")
        or os.environ.get("CTENGINE__EXCHANGE__KEY")
        or env.get("BYBIT_API_KEY", "")
    ).strip()
    secret = (
        os.environ.get("BYBIT_API_SECRET")
        or os.environ.get("CTENGINE__EXCHANGE__SECRET")
        or env.get("BYBIT_API_SECRET", "")
    ).strip()
    if key and secret:
        return key, secret
    for name in ("config_grid.json", "config.json"):
        cfg_path = ft_base() / "user_data" / name
        if cfg_path.is_file():
            ex = load_json(cfg_path, {}).get("exchange", {})
            key = (ex.get("key") or "").strip()
            secret = (ex.get("secret") or "").strip()
            if key and secret:
                return key, secret
    raise RuntimeError("Bybit API keys not configured (BYBIT_API_KEY / exchange.key)")


def ft_pair_to_symbol(pair: str) -> str:
    pair = pair.strip().upper()
    if ":" in pair:
        pair = pair.split(":")[0]
    if "/" in pair:
        base, quote = pair.split("/", 1)
        return f"{base}{quote}"
    if pair.endswith("USDT"):
        return pair
    return f"{pair}USDT"


def symbol_to_ft_pair(symbol: str) -> str:
    symbol = symbol.upper()
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}/USDT:USDT"
    return symbol


def _sign(secret: str, payload: str, timestamp: str, api_key: str, recv_window: str = "5000") -> str:
    raw = f"{timestamp}{api_key}{recv_window}{payload}"
    return hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()


def _endpoint_group(path: str) -> str:
    if "/fgridbot/" in path or "/fcombobot/" in path or "/fmartingalebot/" in path:
        return "fgridbot"
    if path.startswith("/v5/market/"):
        return "market"
    if path.startswith("/v5/asset/"):
        return "asset"
    if path.startswith("/v5/account/"):
        return "account"
    if path.startswith("/v5/position/") or path.startswith("/v5/order/") or path.startswith("/v5/execution/"):
        return "trade"
    return "default"


def _throttle(path: str) -> None:
    group = _endpoint_group(path)
    min_iv = _RATE_MIN_INTERVAL.get(group, _RATE_MIN_INTERVAL["default"])
    now = time.monotonic()
    last = _rate_last_call.get(group, 0.0)
    wait = min_iv - (now - last)
    if wait > 0:
        time.sleep(wait)
    _rate_last_call[group] = time.monotonic()


def _parse_bybit_response(raw: str, headers: Any) -> dict[str, Any]:
    data = json.loads(raw)
    status = headers.get("X-Bapi-Limit-Status") if headers else None
    if status is not None:
        try:
            if int(status) <= 1:
                time.sleep(_RATE_MIN_INTERVAL.get("fgridbot", 0.11))
        except (TypeError, ValueError):
            pass
    return data


def _bybit_request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    signed: bool = False,
    retries: int = 4,
) -> dict[str, Any]:
    last_err: Exception | None = None
    for attempt in range(retries):
        _throttle(path)
        try:
            if method == "POST":
                key, secret = get_credentials() if signed else ("", "")
                payload = json.dumps(body or {}, separators=(",", ":"))
                timestamp = str(int(time.time() * 1000))
                recv_window = "5000"
                headers = {"Content-Type": "application/json"}
                if signed:
                    sign = _sign(secret, payload, timestamp, key, recv_window)
                    headers.update(
                        {
                            "X-BAPI-API-KEY": key,
                            "X-BAPI-TIMESTAMP": timestamp,
                            "X-BAPI-SIGN": sign,
                            "X-BAPI-RECV-WINDOW": recv_window,
                        }
                    )
                api_base = bybit_api_base()
                req = urllib.request.Request(
                    f"{api_base}{path}",
                    data=payload.encode(),
                    headers=headers,
                    method="POST",
                )
            else:
                api_base = bybit_api_base()
                qs = urllib.parse.urlencode(params or {}, doseq=True)
                url = f"{api_base}{path}?{qs}" if qs else f"{api_base}{path}"
                headers: dict[str, str] = {}
                if signed:
                    key, secret = get_credentials()
                    timestamp = str(int(time.time() * 1000))
                    recv_window = "5000"
                    sign = _sign(secret, qs, timestamp, key, recv_window)
                    headers = {
                        "X-BAPI-API-KEY": key,
                        "X-BAPI-TIMESTAMP": timestamp,
                        "X-BAPI-SIGN": sign,
                        "X-BAPI-RECV-WINDOW": recv_window,
                    }
                req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = _parse_bybit_response(resp.read().decode(), resp.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                raise RuntimeError(raw or str(exc)) from exc
        ret_code = data.get("retCode")
        if ret_code == 10006:
            time.sleep(min(2.0, 0.2 * (2**attempt)))
            last_err = RuntimeError(data.get("retMsg") or "Too many visits")
            continue
        if ret_code != 0:
            raise RuntimeError(data.get("retMsg") or f"Bybit error {ret_code}")
        return data.get("result") or {}
    if last_err:
        raise last_err
    raise RuntimeError("Bybit request failed")


def bybit_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    signed = (
        path.startswith("/v5/fgridbot/")
        or path.startswith("/v5/fcombobot/")
        or path.startswith("/v5/asset/")
    )
    return _bybit_request("POST", path, body=body, signed=signed)


def bybit_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return _bybit_request("GET", path, params=params, signed=False)


def bybit_get_signed(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return _bybit_request("GET", path, params=params, signed=True)


def _is_asset_permission_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "permission denied" in msg or "check your api key permissions" in msg


_MANUAL_FUNDING_HINT = (
    "Bybit Grid списывает маржу с Funding Account (не Unified). "
    "Перевод: Bybit → Активы → Перевод → Unified Trading → Funding Account. "
    "Для автопроверки включите права Asset/Transfer на API-ключе."
)


def fetch_usdt_balance(account_type: str) -> float:
    """USDT balance on FUND / UNIFIED (Bybit grid uses Funding Account)."""
    account_type = account_type.upper()
    if account_type == "UNIFIED":
        result = bybit_get_signed(
            "/v5/account/wallet-balance",
            {"accountType": "UNIFIED", "coin": "USDT"},
        )
        for row in result.get("list") or []:
            for coin in row.get("coin") or []:
                if str(coin.get("coin") or "").upper() != "USDT":
                    continue
                for key in ("walletBalance", "availableToWithdraw", "equity"):
                    raw = coin.get(key)
                    if raw in (None, ""):
                        continue
                    try:
                        return float(raw)
                    except (TypeError, ValueError):
                        continue
        return 0.0
    result = bybit_get_signed(
        "/v5/asset/transfer/query-account-coin-balance",
        {"accountType": account_type, "coin": "USDT"},
    )
    balance = result.get("balance") or {}
    if isinstance(balance, dict):
        for key in ("walletBalance", "transferBalance"):
            raw = balance.get(key)
            if raw not in (None, ""):
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
    return 0.0


def try_fetch_fund_balance() -> tuple[float | None, str | None]:
    try:
        return fetch_usdt_balance("FUND"), None
    except RuntimeError as exc:
        if _is_asset_permission_error(exc):
            return None, str(exc)
        raise


def inter_transfer_usdt(amount: float, from_account: str, to_account: str) -> dict[str, Any]:
    if amount <= 0:
        raise ValueError("transfer amount must be positive")
    body = {
        "transferId": str(uuid.uuid4()),
        "coin": "USDT",
        "amount": f"{amount:.4f}".rstrip("0").rstrip("."),
        "fromAccountType": from_account,
        "toAccountType": to_account,
    }
    return bybit_post("/v5/asset/transfer/inter-transfer", body)


def ensure_funding_balance(required_usdt: float, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Grid bots debit Funding Account — auto-transfer from Unified if configured."""
    cfg = cfg or load_config()
    required = max(0.0, float(required_usdt))
    buffer = float(cfg.get("funding_buffer_usdt", 2))
    target = required + buffer
    unified = fetch_usdt_balance("UNIFIED")
    fund, fund_err = try_fetch_fund_balance()
    out: dict[str, Any] = {
        "fund_usdt": round(fund, 4) if fund is not None else None,
        "fund_unknown": fund is None,
        "unified_usdt": round(unified, 4),
        "required_usdt": round(required, 4),
        "target_usdt": round(target, 4),
        "transferred_usdt": 0.0,
    }
    if fund_err:
        out["fund_api_error"] = fund_err

    if cfg.get("skip_funding_precheck"):
        if unified < required:
            raise ValueError(
                f"Недостаточно USDT на Unified ({unified:.2f}, нужно ≥{required:.2f}). "
                f"{_MANUAL_FUNDING_HINT}"
            )
        out["ok"] = True
        out["skipped_precheck"] = True
        return out

    if fund is not None and fund >= target:
        out["ok"] = True
        return out

    if not cfg.get("auto_fund_transfer", True):
        fund_label = f"{fund:.2f}" if fund is not None else "?"
        raise ValueError(
            f"На Funding Account {fund_label} USDT (нужно ≥{target:.2f}). "
            f"Unified: {unified:.2f} USDT. {_MANUAL_FUNDING_HINT}"
        )

    deficit = target - (fund if fund is not None else 0.0)
    if unified < deficit:
        fund_label = f"{fund:.2f}" if fund is not None else "?"
        raise ValueError(
            f"Недостаточно USDT для Grid: Funding {fund_label}, Unified {unified:.2f}, "
            f"нужно ≥{target:.2f} USDT на Funding Account. {_MANUAL_FUNDING_HINT}"
        )

    try:
        inter_transfer_usdt(deficit, "UNIFIED", "FUND")
    except RuntimeError as exc:
        if _is_asset_permission_error(exc):
            raise ValueError(
                f"Нужно ≥{target:.2f} USDT на Funding Account (на Unified {unified:.2f}). "
                f"{_MANUAL_FUNDING_HINT} "
                "После ручного перевода можно включить skip_funding_precheck в конфиге Grid."
            ) from exc
        raise

    time.sleep(0.8)
    fund_after, _ = try_fetch_fund_balance()
    out["fund_usdt"] = round(fund_after, 4) if fund_after is not None else None
    out["transferred_usdt"] = round(deficit, 4)
    if fund_after is not None and fund_after < required:
        raise ValueError(
            f"Перевод {deficit:.2f} USDT на Funding не завершился: баланс {fund_after:.2f} USDT. "
            f"{_MANUAL_FUNDING_HINT}"
        )
    out["ok"] = True
    return out


def funding_status(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    invest = float(cfg.get("defaults", {}).get("total_investment", 10))
    max_bots = int(cfg.get("max_active_bots", 1))
    required = invest * max_bots
    unified = fetch_usdt_balance("UNIFIED")
    fund, fund_err = try_fetch_fund_balance()
    buffer = float(cfg.get("funding_buffer_usdt", 2))
    target = required + buffer
    skip = bool(cfg.get("skip_funding_precheck"))
    if fund is not None:
        ok = fund >= target
    elif skip:
        ok = unified >= target
    else:
        ok = False
    payload: dict[str, Any] = {
        "fund_usdt": round(fund, 4) if fund is not None else None,
        "fund_unknown": fund is None,
        "unified_usdt": round(unified, 4),
        "required_usdt": round(required, 4),
        "target_usdt": round(target, 4),
        "ok": ok,
        "auto_fund_transfer": bool(cfg.get("auto_fund_transfer", True)),
        "skip_funding_precheck": skip,
    }
    if fund_err:
        payload["fund_api_error"] = fund_err
        payload["message"] = (
            f"API не видит Funding Account — переведите ≥{target:.2f} USDT вручную. "
            f"{_MANUAL_FUNDING_HINT}"
        )
    elif fund is not None and not ok:
        payload["message"] = (
            f"На Funding {fund:.2f} USDT, нужно ≥{target:.2f}. Unified: {unified:.2f} USDT."
        )
    return payload


def load_config() -> dict[str, Any]:
    default = {
        "defaults": {
            "grid_mode": 1,
            "grid_type": 1,
            "cell_number": 15,
            "leverage": "3",
            "total_investment": "10",
            "price_range_pct": 0.09,
            "take_profit_usdt": 0.3,
            "stop_loss_usdt": 0.55,
        },
        "max_active_bots": 1,
        "auto_fund_transfer": True,
        "skip_funding_precheck": False,
        "funding_buffer_usdt": 2,
        "exclude_bases": ["SKHYNIX"],
        "deploy_guards": {
            "neutral_max_1h_move_pct": 1.5,
            "sl_cooldown_hours": 3,
            "sl_adaptive_window_hours": 24,
            "sl_adaptive_min_count": 2,
            "take_profit_usdt_frequent_sl": 0.3,
        },
    }
    cfg = load_json(config_path(), default)
    cfg.setdefault("defaults", default["defaults"])
    cfg.setdefault("max_active_bots", 1)
    cfg.setdefault("deploy_guards", default["deploy_guards"])
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    save_json(config_path(), cfg)


def format_bybit_percent(pct: float, *, max_decimals: int = 4) -> str:
    """Bybit fgridbot percent field: human-readable percent (4 = 4%, not 0.04)."""
    pct = round(float(pct), max_decimals)
    if pct <= 0:
        raise ValueError("percent must be positive")
    if abs(pct - round(pct)) < 10 ** -(max_decimals + 1):
        return str(int(round(pct)))
    text = f"{pct:.{max_decimals}f}".rstrip("0").rstrip(".")
    return text or "0"


def pnl_usdt_to_bybit_percent(
    pnl_usdt: float,
    investment_usdt: float,
    *,
    leverage: float = 1.0,
    for_stop_loss: bool = False,
) -> str:
    """Convert target USDT PnL into Bybit grid TP/SL percent string."""
    if investment_usdt <= 0:
        raise ValueError("total_investment must be positive")
    pct = (float(pnl_usdt) / float(investment_usdt)) * 100.0
    if for_stop_loss:
        lev = max(1.0, float(leverage))
        if lev > 1.0:
            pct = pct / (lev * SL_LEVERAGE_OVERSHOOT)
        return format_bybit_percent(pct, max_decimals=2)
    return format_bybit_percent(pct)


def apply_tp_sl_from_usdt(params: dict[str, Any], defaults: dict[str, Any] | None = None) -> None:
    src = {**(defaults or {}), **params}
    try:
        inv = float(str(params.get("total_investment") or src.get("total_investment") or "10"))
    except (TypeError, ValueError):
        inv = 10.0
    try:
        leverage = float(str(params.get("leverage") or src.get("leverage") or "1"))
    except (TypeError, ValueError):
        leverage = 1.0
    tp_usdt = float(src.get("take_profit_usdt") or 0)
    sl_usdt = float(src.get("stop_loss_usdt") or 0)
    if tp_usdt > 0:
        params["take_profit_per"] = pnl_usdt_to_bybit_percent(tp_usdt, inv)
        params["tp_sl_type"] = 1
    if sl_usdt > 0:
        params["stop_loss_per"] = pnl_usdt_to_bybit_percent(
            sl_usdt, inv, leverage=leverage, for_stop_loss=True
        )
        params["tp_sl_type"] = 1
    params.pop("take_profit_usdt", None)
    params.pop("stop_loss_usdt", None)


def tp_sl_preview(defaults: dict[str, Any]) -> dict[str, Any]:
    try:
        inv = float(str(defaults.get("total_investment") or "10"))
    except (TypeError, ValueError):
        inv = 10.0
    try:
        leverage = float(str(defaults.get("leverage") or "1"))
    except (TypeError, ValueError):
        leverage = 1.0
    tp_usdt = float(defaults.get("take_profit_usdt") or 0)
    sl_usdt = float(defaults.get("stop_loss_usdt") or 0)
    out: dict[str, Any] = {
        "take_profit_usdt": tp_usdt,
        "stop_loss_usdt": sl_usdt,
        "total_investment": inv,
        "leverage": leverage,
    }
    if tp_usdt > 0 and inv > 0:
        out["take_profit_per"] = pnl_usdt_to_bybit_percent(tp_usdt, inv)
    if sl_usdt > 0 and inv > 0:
        out["stop_loss_per"] = pnl_usdt_to_bybit_percent(
            sl_usdt, inv, leverage=leverage, for_stop_loss=True
        )
        if leverage > 1:
            out["stop_loss_note"] = (
                f"Bybit получит ~{out['stop_loss_per']}% (цель {sl_usdt} USDT с плечом {leverage:g}x)"
            )
    return out


def _validation_error_message(result: dict[str, Any]) -> str | None:
    code = str(result.get("check_code") or "")
    if not code or code in (CHECK_CODE_SUCCESS, "FGRID_CHECK_CODE_UNSPECIFIED"):
        return None
    labels = {
        "FGRID_CHECK_CODE_SL_TOO_LOW": "стоп-лосс слишком мал",
        "FGRID_CHECK_CODE_SL_TOO_HIGH": "стоп-лосс слишком велик",
        "FGRID_CHECK_CODE_TP_TOO_LOW": "тейк-профит слишком мал",
        "FGRID_CHECK_CODE_TP_TOO_HIGH": "тейк-профит слишком велик",
        "FGRID_CHECK_CODE_INVESTMENT_TOO_LOW": "инвестиция слишком мала",
        "FGRID_CHECK_CODE_SL_CAUSE_LIQUIDATION": "стоп-лосс приведёт к ликвидации",
    }
    return labels.get(code, code)


def update_config(updates: dict[str, Any]) -> dict[str, Any]:
    from grid_changelog import record_config_diff

    cfg = load_config()
    old_cfg = copy.deepcopy(cfg)
    if "max_active_bots" in updates:
        cfg["max_active_bots"] = max(0, min(5, int(updates["max_active_bots"])))
    defaults = cfg.setdefault("defaults", {})
    if "total_investment" in updates:
        inv = float(updates["total_investment"])
        if inv < 5:
            raise ValueError("Минимальная инвестиция — 5 USDT")
        defaults["total_investment"] = str(inv)
    for key in ("take_profit_usdt", "stop_loss_usdt"):
        if key in updates:
            val = float(updates[key])
            if val <= 0:
                raise ValueError(f"{key} must be positive")
            defaults[key] = val
    save_config(cfg)
    record_config_diff(old_cfg, cfg, source="ui")
    return cfg


MAX_HISTORY = 500


def load_state() -> dict[str, Any]:
    state = load_json(state_path(), {"bots": [], "history": []})
    state.setdefault("bots", [])
    state.setdefault("history", [])
    return _normalize_state(state)


def _merge_history(new_items: list[dict[str, Any]], existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for item in new_items + existing:
        bot_id = str(item.get("bot_id", ""))
        if not bot_id or bot_id in seen:
            continue
        seen.add(bot_id)
        merged.append(item)
    return merged[:MAX_HISTORY]


def _normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Move closed bots from the active list into history."""
    active: list[dict[str, Any]] = []
    history = list(state.get("history", []))
    seen_history = {str(h.get("bot_id")) for h in history if h.get("bot_id")}

    for entry in state.get("bots", []):
        bot_id = str(entry.get("bot_id", ""))
        if entry.get("status") == "closed":
            if bot_id and bot_id not in seen_history:
                history.insert(0, _archive_bot(entry))
                seen_history.add(bot_id)
            continue
        active.append(entry)

    state["bots"] = _dedupe_active_bots(active)
    state["history"] = history[:MAX_HISTORY]
    return state


def _dedupe_active_bots(bots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for entry in bots:
        bot_id = str(entry.get("bot_id", ""))
        if bot_id:
            by_id[bot_id] = entry
    return list(by_id.values())


def collect_tracked_bot_ids(state: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("bots", "history"):
        for entry in state.get(key, []):
            bot_id = str(entry.get("bot_id", "")).strip()
            if bot_id:
                ids.add(bot_id)
    for bot_id in state.get("tracked_bot_ids", []):
        s = str(bot_id).strip()
        if s:
            ids.add(s)
    scan = load_json(ft_base() / "user_data" / "bybit_grid_scan.json", {})
    last = scan.get("last_deploy") or {}
    create = last.get("create") or {}
    bot_id = str(create.get("bot_id") or "").strip()
    if bot_id:
        ids.add(bot_id)
    return ids


def track_bot_id(state: dict[str, Any], bot_id: str) -> None:
    bot_id = str(bot_id).strip()
    if not bot_id:
        return
    tracked = state.setdefault("tracked_bot_ids", [])
    if bot_id not in tracked:
        tracked.append(bot_id)


def import_active_bot(bot_id: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    bot_id = str(bot_id).strip()
    if not bot_id:
        return None
    state = load_state()
    if any(str(b.get("bot_id")) == bot_id for b in state.get("bots", [])):
        track_bot_id(state, bot_id)
        save_state(state)
        return None
    try:
        norm = _normalize_detail(get_bot_detail(bot_id, use_cache=False))
    except RuntimeError:
        track_bot_id(state, bot_id)
        save_state(state)
        return None
    if not norm.get("is_active"):
        track_bot_id(state, bot_id)
        save_state(state)
        return None
    entry = {
        "bot_id": bot_id,
        "symbol": norm.get("symbol") or "",
        "pair": norm.get("pair") or "",
        "params": params
        or {
            "symbol": norm.get("symbol"),
            "min_price": norm.get("min_price"),
            "max_price": norm.get("max_price"),
            "cell_number": norm.get("cell_number"),
            "leverage": norm.get("leverage"),
            "grid_type": 1,
            "total_investment": norm.get("total_investment"),
        },
        "created_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "imported": True,
    }
    state.setdefault("bots", []).append(entry)
    track_bot_id(state, bot_id)
    save_state(state)
    return entry


def sync_missing_bots(*, force: bool = False) -> None:
    """Light reconcile: active bots only, throttled (not on every UI poll)."""
    global _last_light_sync
    now = time.time()
    if not force and now - _last_light_sync < _SYNC_TTL_SEC:
        return
    sync_all_bots_from_bybit(full_reconcile=False)
    _last_light_sync = now


def collect_all_known_bot_ids(state: dict[str, Any] | None = None) -> set[str]:
    state = state or load_state()
    ids = collect_tracked_bot_ids(state)
    for key in ("bots", "history"):
        for entry in state.get(key, []):
            bot_id = str(entry.get("bot_id", "")).strip()
            if bot_id:
                ids.add(bot_id)
    ids.update(discover_bot_ids_from_funding())
    return ids


def discover_bot_ids_from_funding() -> set[str]:
    """Best-effort: extract grid bot IDs from Funding account history."""
    import re

    global _funding_ids_cache
    now = time.time()
    if now - _funding_ids_cache[0] < _FUNDING_IDS_TTL_SEC:
        return set(_funding_ids_cache[1])

    found: set[str] = set()
    try:
        result = bybit_get_signed(
            "/v5/asset/fundinghistory",
            {"coin": "USDT", "limit": 50},
        )
        blob = json.dumps(result, ensure_ascii=False)
        for match in re.findall(r"\d{15,22}", blob):
            if match.startswith("625"):
                found.add(match)
    except RuntimeError:
        pass
    _funding_ids_cache = (now, found)
    return set(found)


def collect_sync_candidate_ids(state: dict[str, Any] | None = None) -> set[str]:
    """IDs worth checking on routine sync — active bots + explicit tracking only."""
    state = state or load_state()
    ids = collect_tracked_bot_ids(state)
    for entry in state.get("bots", []):
        bot_id = str(entry.get("bot_id", "")).strip()
        if bot_id:
            ids.add(bot_id)
    return ids


def discover_active_bots_scanning(
    *,
    below_anchor: int | None = None,
    span: int = 400_000_000,
    step: int = 2_000_000,
    max_checks: int = 60,
) -> list[str]:
    """Scan bot_id space near a known ID to find untracked active grid bots."""
    state = load_state()
    known = collect_all_known_bot_ids(state)
    if below_anchor is None:
        anchors = sorted(int(x) for x in known if str(x).isdigit())
        if not anchors:
            return []
        below_anchor = anchors[-1]
    start = max(below_anchor - span, 625300000000000000)
    imported: list[str] = []
    checks = 0
    for n in range(start, below_anchor, step):
        if checks >= max_checks:
            break
        checks += 1
        bid = str(n)
        if bid in known:
            continue
        try:
            norm = _normalize_detail(get_bot_detail(bid, use_cache=False))
            if norm.get("is_active"):
                import_active_bot(bid)
                known.add(bid)
                imported.append(bid)
        except RuntimeError:
            pass
    return imported


def sync_all_bots_from_bybit(
    extra_bot_ids: list[str] | None = None,
    *,
    scan_missing: bool = False,
    full_reconcile: bool = False,
) -> dict[str, Any]:
    """Reconcile local state with Bybit: import all active bots, archive closed ones."""
    state = load_state()
    if full_reconcile or scan_missing:
        candidate_ids = collect_all_known_bot_ids(state)
    else:
        candidate_ids = collect_sync_candidate_ids(state)
    for bot_id in extra_bot_ids or []:
        s = str(bot_id).strip()
        if s:
            candidate_ids.add(s)
    scanned: list[str] = []
    if scan_missing:
        scanned = discover_active_bots_scanning()
        candidate_ids.update(scanned)
    active_on_bybit: dict[str, dict[str, Any]] = {}
    import_errors: list[str] = []

    for bot_id in sorted(candidate_ids):
        try:
            detail = get_bot_detail(bot_id, use_cache=not (full_reconcile or scan_missing))
            norm = _normalize_detail(detail)
            track_bot_id(state, bot_id)
            if norm.get("is_active"):
                active_on_bybit[bot_id] = norm
        except RuntimeError as exc:
            import_errors.append(f"{bot_id}: {exc}")

    known_active = {str(b.get("bot_id")) for b in state.get("bots", []) if b.get("bot_id")}
    imported = 0
    for bot_id, norm in active_on_bybit.items():
        if bot_id in known_active:
            continue
        entry = {
            "bot_id": bot_id,
            "symbol": norm.get("symbol") or "",
            "pair": norm.get("pair") or "",
            "params": {
                "symbol": norm.get("symbol"),
                "min_price": norm.get("min_price"),
                "max_price": norm.get("max_price"),
                "cell_number": norm.get("cell_number"),
                "leverage": norm.get("leverage"),
                "grid_type": 1,
                "total_investment": norm.get("total_investment"),
            },
            "created_at": datetime.now(UTC).isoformat(),
            "status": "running",
            "imported": True,
        }
        state.setdefault("bots", []).append(entry)
        known_active.add(bot_id)
        imported += 1

    save_state(state)
    return {
        "candidates": len(candidate_ids),
        "active_on_bybit": len(active_on_bybit),
        "imported": imported,
        "scanned_imported": scanned,
        "errors": import_errors[:10],
        "active_pairs": sorted({v.get("pair") or v.get("symbol") for v in active_on_bybit.values()}),
    }


def is_sl_close_reason(reason: str) -> bool:
    r = str(reason).upper()
    return "STOP_TYPE_SL" in r or ("_SL" in r and "TP" not in r)


def count_recent_sl(state: dict[str, Any] | None = None, window_hours: float = 24) -> int:
    state = state or load_state()
    since = datetime.now(UTC) - timedelta(hours=window_hours)
    count = 0
    for entry in state.get("history", []):
        closed = entry.get("closed_at") or ""
        if not closed:
            continue
        try:
            ts = datetime.fromisoformat(str(closed).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < since:
            continue
        if is_sl_close_reason(str(entry.get("close_reason") or entry.get("status_label") or "")):
            count += 1
    return count


def resolve_tp_sl_defaults(cfg: dict[str, Any] | None = None) -> tuple[float, float]:
    cfg = cfg or load_config()
    defaults = cfg.get("defaults", {})
    sl = float(defaults.get("stop_loss_usdt", 0.55))
    tp = float(defaults.get("take_profit_usdt", 0.3))
    guards = cfg.get("deploy_guards", {})
    window_h = float(guards.get("sl_adaptive_window_hours", 24))
    min_sl = int(guards.get("sl_adaptive_min_count", 2))
    if count_recent_sl(window_hours=window_h) >= min_sl:
        tp = float(guards.get("take_profit_usdt_frequent_sl", tp))
    return tp, sl


def fetch_1h_move_pct(symbol: str) -> float:
    klines = bybit_get(
        "/v5/market/kline",
        {"category": "linear", "symbol": symbol, "interval": "60", "limit": 3},
    ).get("list") or []
    if len(klines) < 2:
        return 0.0
    latest = float(klines[0][4])
    prev = float(klines[1][4])
    if prev == 0:
        return 0.0
    return (latest - prev) / prev * 100.0


def pair_sl_cooldown_message(symbol: str, cfg: dict[str, Any] | None = None) -> str | None:
    cfg = cfg or load_config()
    guards = cfg.get("deploy_guards", {})
    hours = float(guards.get("sl_cooldown_hours", 3))
    state = load_state()
    key = normalize_grid_pair_key(symbol=symbol)
    started = (state.get("sl_cooldowns") or {}).get(key)
    if not started:
        return None
    try:
        ts = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
    except ValueError:
        return None
    until = ts + timedelta(hours=hours)
    if datetime.now(UTC) >= until:
        return None
    return (
        f"{symbol_to_ft_pair(symbol)}: cooldown после SL до "
        f"{until.strftime('%H:%M UTC')} ({hours:g} ч)"
    )


def check_deploy_allowed(pair: str, grid_mode: int, cfg: dict[str, Any] | None = None) -> None:
    cfg = cfg or load_config()
    symbol = ft_pair_to_symbol(pair)
    cooldown = pair_sl_cooldown_message(symbol, cfg)
    if cooldown:
        raise ValueError(cooldown)
    guards = cfg.get("deploy_guards", {})
    max_move = float(guards.get("neutral_max_1h_move_pct", 1.5))
    if int(grid_mode) == 1:
        move = fetch_1h_move_pct(symbol)
        if abs(move) > max_move:
            raise ValueError(
                f"Neutral запрещён для {symbol_to_ft_pair(symbol)}: "
                f"движение за 1ч {move:+.2f}% (лимит ±{max_move:g}%)"
            )


def record_sl_cooldown(state: dict[str, Any], archived: dict[str, Any]) -> None:
    if not is_sl_close_reason(str(archived.get("close_reason") or "")):
        return
    symbol = archived.get("symbol") or ft_pair_to_symbol(archived.get("pair") or "")
    if not symbol:
        return
    key = normalize_grid_pair_key(symbol=symbol)
    cooldowns = state.setdefault("sl_cooldowns", {})
    cooldowns[key] = datetime.now(UTC).isoformat()


def _archive_bot(entry: dict[str, Any], norm: dict[str, Any] | None = None) -> dict[str, Any]:
    norm = norm or {}
    return {
        "bot_id": str(entry.get("bot_id") or norm.get("bot_id") or ""),
        "symbol": entry.get("symbol") or norm.get("symbol"),
        "pair": entry.get("pair") or norm.get("pair"),
        "params": entry.get("params"),
        "created_at": entry.get("created_at"),
        "closed_at": entry.get("closed_at") or datetime.now(UTC).isoformat(),
        "close_reason": norm.get("close_reason") or entry.get("close_reason"),
        "status_label": norm.get("status_label"),
        "grid_mode_label": norm.get("grid_mode_label"),
        "realised_pnl": norm.get("realised_pnl") or norm.get("pnl"),
        "pnl_per": norm.get("pnl_per"),
        "min_price": norm.get("min_price"),
        "max_price": norm.get("max_price"),
        "cell_number": norm.get("cell_number"),
        "leverage": norm.get("leverage"),
        "total_investment": norm.get("total_investment"),
        "settlement": norm.get("settlement"),
    }


def save_state(state: dict[str, Any]) -> None:
    save_json(state_path(), state)


def fetch_last_price(symbol: str) -> float:
    result = bybit_get(
        "/v5/market/tickers",
        {"category": "linear", "symbol": symbol},
    )
    items = result.get("list") or []
    if not items:
        raise RuntimeError(f"No ticker for {symbol}")
    return float(items[0]["lastPrice"])


def fetch_symbol_filters(symbol: str) -> dict[str, str]:
    result = bybit_get(
        "/v5/market/instruments-info",
        {"category": "linear", "symbol": symbol},
    )
    items = result.get("list") or []
    if not items:
        raise RuntimeError(f"No instrument info for {symbol}")
    price_filter = items[0].get("priceFilter") or {}
    return {
        "tick_size": str(price_filter.get("tickSize") or "0.01"),
        "price_scale": str(items[0].get("priceScale") or ""),
        "min_price": str(price_filter.get("minPrice") or "0"),
        "max_price": str(price_filter.get("maxPrice") or "999999"),
    }


def _as_decimal(value: float | str | Decimal) -> Decimal:
    return Decimal(str(value))


def _tick_decimals(tick_size: str) -> int:
    tick = _as_decimal(tick_size)
    # Keep literal precision from API (normalize() can turn 0.00001 into 1E-5).
    raw = str(tick_size).strip().lower()
    if "e" not in raw and "." in raw:
        frac = raw.split(".", 1)[1].rstrip("0")
        return len(frac)
    text = format(tick, "f")
    if "." not in text:
        return 0
    frac = text.rstrip("0").split(".", 1)[1]
    return len(frac)


def _price_decimals(tick_size: str, price_scale: str = "") -> int:
    if price_scale and str(price_scale).isdigit():
        return int(price_scale)
    return _tick_decimals(tick_size)


def format_price_str(
    value: float | str | Decimal, tick_size: str, price_scale: str = ""
) -> str:
    decimals = _price_decimals(tick_size, price_scale)
    quantum = Decimal(1) if decimals == 0 else Decimal(1).scaleb(-decimals)
    rounded = _as_decimal(value).quantize(quantum)
    if decimals == 0:
        return str(int(rounded))
    return f"{rounded:.{decimals}f}"


def align_arithmetic_grid_prices(
    min_price: float | str,
    max_price: float | str,
    cell_number: int,
    tick_size: str,
    price_scale: str = "",
) -> tuple[str, str, int]:
    tick = _as_decimal(tick_size)
    if tick <= 0:
        raise ValueError(f"Invalid tick size: {tick_size}")

    cells = max(2, int(cell_number))
    min_aligned = (_as_decimal(min_price) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    max_aligned = (_as_decimal(max_price) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    if max_aligned <= min_aligned:
        max_aligned = min_aligned + tick * cells

    step_ticks = int(
        ((max_aligned - min_aligned) / tick / cells).to_integral_value(rounding=ROUND_FLOOR)
    )
    if step_ticks < 1:
        step_ticks = 1
    max_aligned = min_aligned + tick * step_ticks * cells

    return (
        format_price_str(min_aligned, tick_size, price_scale),
        format_price_str(max_aligned, tick_size, price_scale),
        cells,
    )


def normalize_grid_prices(params: dict[str, Any]) -> dict[str, Any]:
    symbol = params.get("symbol") or ft_pair_to_symbol(params.get("pair", ""))
    filters = fetch_symbol_filters(symbol)
    tick_size = filters["tick_size"]
    price_scale = filters.get("price_scale", "")
    grid_type = int(params.get("grid_type", 1))
    cells = int(params.get("cell_number", 10))

    if grid_type == 1:
        min_p, max_p, cells = align_arithmetic_grid_prices(
            params["min_price"], params["max_price"], cells, tick_size, price_scale
        )
    else:
        min_p = format_price_str(params["min_price"], tick_size, price_scale)
        max_p = format_price_str(params["max_price"], tick_size, price_scale)

    params["min_price"] = min_p
    params["max_price"] = max_p
    params["cell_number"] = cells
    return params


def suggest_params(pair: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = load_config()
    tp_usdt, sl_usdt = resolve_tp_sl_defaults(cfg)
    defaults = {
        **cfg["defaults"],
        **(overrides or {}),
        "take_profit_usdt": tp_usdt,
        "stop_loss_usdt": sl_usdt,
    }
    symbol = ft_pair_to_symbol(pair)
    price = fetch_last_price(symbol)
    pct = float(defaults.get("price_range_pct", 0.09))
    filters = fetch_symbol_filters(symbol)
    tick_size = filters["tick_size"]
    price_scale = filters.get("price_scale", "")
    min_price = price * (1 - pct)
    max_price = price * (1 + pct)

    klines = bybit_get(
        "/v5/market/kline",
        {"category": "linear", "symbol": symbol, "interval": "60", "limit": 48},
    ).get("list") or []
    closes = [float(k[4]) for k in reversed(klines)] if klines else [price]
    ema_fast = sum(closes[-12:]) / min(12, len(closes))
    ema_slow = sum(closes) / len(closes)
    if ema_fast > ema_slow * 1.002:
        grid_mode = 2
    elif ema_fast < ema_slow * 0.998:
        grid_mode = 3
    else:
        grid_mode = int(defaults.get("grid_mode", 1))
    if overrides and "grid_mode" in overrides:
        grid_mode = int(overrides["grid_mode"])

    params = {
        "symbol": symbol,
        "pair": symbol_to_ft_pair(symbol),
        "grid_mode": grid_mode,
        "grid_mode_label": GRID_MODE_LABELS.get(grid_mode, "neutral"),
        "grid_type": int(defaults.get("grid_type", 1)),
        "grid_type_label": GRID_TYPE_LABELS.get(int(defaults.get("grid_type", 1)), "arithmetic"),
        "min_price": format_price_str(min_price, tick_size, price_scale),
        "max_price": format_price_str(max_price, tick_size, price_scale),
        "cell_number": int(defaults.get("cell_number", 15)),
        "leverage": str(defaults.get("leverage", "3")),
        "total_investment": str(defaults.get("total_investment", "50")),
        "mark_price": str(price),
    }
    for key in ("take_profit_per", "stop_loss_per", "tp_sl_type"):
        if key in defaults and defaults[key] not in (None, ""):
            params[key] = defaults[key] if key == "tp_sl_type" else str(defaults[key])
    apply_tp_sl_from_usdt(params, defaults)
    normalize_grid_prices(params)
    return params


def build_create_body(params: dict[str, Any]) -> dict[str, Any]:
    symbol = params.get("symbol") or ft_pair_to_symbol(params.get("pair", ""))
    body: dict[str, Any] = {
        "symbol": symbol,
        "grid_mode": int(params["grid_mode"]),
        "min_price": str(params["min_price"]),
        "max_price": str(params["max_price"]),
        "cell_number": int(params["cell_number"]),
        "leverage": str(params["leverage"]),
        "grid_type": int(params.get("grid_type", 1)),
        "total_investment": str(params["total_investment"]),
    }
    for key in (
        "take_profit_per",
        "stop_loss_per",
        "take_profit_price",
        "stop_loss_price",
        "tp_sl_type",
        "entry_price",
        "trailing_stop_per",
        "move_up_price",
        "move_down_price",
    ):
        if params.get(key) not in (None, ""):
            body[key] = str(params[key]) if key.endswith("_per") or "price" in key else params[key]
    if params.get("tp_sl_type") is not None:
        body["tp_sl_type"] = int(params["tp_sl_type"])
    return body


def normalize_grid_pair_key(pair: str | None = None, symbol: str | None = None) -> str:
    if symbol:
        return ft_pair_to_symbol(symbol).upper()
    return ft_pair_to_symbol(pair or "").upper()


def get_active_grid_pair_keys(refresh: bool = True) -> set[str]:
    payload = list_bots(refresh=refresh)
    keys: set[str] = set()
    for bot in payload.get("bots", []):
        if not bot.get("is_active"):
            continue
        keys.add(normalize_grid_pair_key(pair=bot.get("pair"), symbol=bot.get("symbol")))
    return keys


def pair_has_active_grid(pair: str | None = None, symbol: str | None = None, refresh: bool = True) -> bool:
    key = normalize_grid_pair_key(pair=pair, symbol=symbol)
    return key in get_active_grid_pair_keys(refresh=refresh)


def prepare_grid_params(params: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    defaults = dict(cfg.get("defaults", {}))
    tp_usdt, sl_usdt = resolve_tp_sl_defaults(cfg)
    defaults["take_profit_usdt"] = tp_usdt
    defaults["stop_loss_usdt"] = sl_usdt
    merged: dict[str, Any] = {**defaults, **params}
    merged["total_investment"] = str(merged.get("total_investment", defaults.get("total_investment", "10")))
    apply_tp_sl_from_usdt(merged, defaults)
    if merged.get("min_price") and merged.get("max_price"):
        normalize_grid_prices(merged)
    return merged


def validate_grid(params: dict[str, Any]) -> dict[str, Any]:
    params = prepare_grid_params(params)
    body = build_create_body(params)
    result = bybit_post("/v5/fgridbot/validate", body)
    err = _validation_error_message(result)
    if err:
        raise ValueError(f"Bybit grid validate: {err}")
    return {"ok": True, "params": body, "validation": result}


def create_grid(params: dict[str, Any]) -> dict[str, Any]:
    cfg = load_config()
    state = load_state()
    status = get_status_payload()
    active = [b for b in status.get("bots", []) if b.get("is_active")]
    if len(active) >= int(cfg.get("max_active_bots", 1)):
        raise ValueError(
            f"Max active Bybit grids: {cfg['max_active_bots']}"
            + (" (0 = создание отключено)" if int(cfg.get("max_active_bots", 1)) <= 0 else "")
        )

    symbol = params.get("symbol") or ft_pair_to_symbol(params.get("pair", ""))
    base = symbol.replace("USDT", "")
    if base in cfg.get("exclude_bases", []):
        raise ValueError(f"Pair {base} is excluded")

    active_keys = {
        normalize_grid_pair_key(pair=b.get("pair"), symbol=b.get("symbol")) for b in active
    }
    if normalize_grid_pair_key(symbol=symbol) in active_keys:
        raise ValueError(
            f"Уже есть активный Bybit Grid на {symbol_to_ft_pair(symbol)} — дождитесь завершения или выберите другую пару"
        )

    grid_mode = int(params.get("grid_mode") or cfg.get("auto_grid_mode") or cfg["defaults"].get("grid_mode", 1))
    check_deploy_allowed(params.get("pair") or symbol_to_ft_pair(symbol), grid_mode, cfg)

    params = prepare_grid_params(params, cfg)
    invest = float(str(params.get("total_investment") or "10"))
    ensure_funding_balance(invest, cfg)
    body = build_create_body(params)
    result = bybit_post("/v5/fgridbot/validate", body)
    err = _validation_error_message(result)
    if err:
        raise ValueError(f"Bybit grid validate: {err}")
    result = bybit_post("/v5/fgridbot/create", body)
    bot_id = str(
        result.get("bot_id")
        or (result.get("result") or {}).get("bot_id")
        or (result.get("result") or {}).get("id")
        or result.get("id")
        or ""
    )
    if not bot_id:
        raise RuntimeError(f"Bybit create returned no bot_id: {result!r}")
    entry = {
        "bot_id": bot_id,
        "symbol": symbol,
        "pair": symbol_to_ft_pair(symbol),
        "params": body,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "running",
    }
    state.setdefault("bots", []).append(entry)
    track_bot_id(state, bot_id)
    save_state(state)
    invalidate_bot_detail_cache(bot_id)
    return {"bot_id": bot_id, "bot": entry, "result": result}


def get_bot_detail(bot_id: str, *, use_cache: bool = True) -> dict[str, Any]:
    bid = str(bot_id)
    if use_cache:
        hit = _detail_cache.get(bid)
        if hit and time.time() - hit[0] < DETAIL_CACHE_TTL:
            return hit[1]
    result = bybit_post("/v5/fgridbot/detail", {"bot_id": bid})
    if use_cache:
        _detail_cache[bid] = (time.time(), result)
    return result


def invalidate_bot_detail_cache(bot_id: str | None = None) -> None:
    if bot_id is None:
        _detail_cache.clear()
    else:
        _detail_cache.pop(str(bot_id), None)


def close_grid(bot_id: str) -> dict[str, Any]:
    result = bybit_post("/v5/fgridbot/close", {"bot_id": str(bot_id)})
    state = load_state()
    kept: list[dict[str, Any]] = []
    archived: dict[str, Any] | None = None
    for bot in state.get("bots", []):
        if str(bot.get("bot_id")) == str(bot_id):
            bot["status"] = "closed"
            bot["closed_at"] = datetime.now(UTC).isoformat()
            try:
                norm = _normalize_detail(get_bot_detail(bot_id, use_cache=False))
            except RuntimeError:
                norm = None
            archived = _archive_bot(bot, norm)
        else:
            kept.append(bot)
    if archived:
        state["history"] = _merge_history([archived], state.get("history", []))
        record_sl_cooldown(state, archived)
    state["bots"] = kept
    save_state(state)
    invalidate_bot_detail_cache(bot_id)
    return result


def _normalize_detail(detail: dict[str, Any]) -> dict[str, Any]:
    bot = detail.get("detail") or detail.get("bot") or detail
    symbol = bot.get("symbol") or ""
    status = bot.get("status") or bot.get("bot_status") or "unknown"
    close_reason = bot.get("close_reason") or bot.get("bot_close_code") or ""
    pnl = bot.get("pnl") or bot.get("realised_pnl") or bot.get("realized_pnl") or "0"
    grid_mode_raw = bot.get("grid_mode") or bot.get("grid_mode_label") or ""
    grid_mode_label = str(grid_mode_raw)
    if "LONG" in grid_mode_label.upper():
        grid_mode_label = "long"
    elif "SHORT" in grid_mode_label.upper():
        grid_mode_label = "short"
    elif "NEUTRAL" in grid_mode_label.upper():
        grid_mode_label = "neutral"
    return {
        "bot_id": str(bot.get("bot_id") or bot.get("id") or ""),
        "symbol": symbol,
        "pair": symbol_to_ft_pair(symbol) if symbol else "",
        "grid_mode": grid_mode_raw,
        "grid_mode_label": grid_mode_label,
        "status": status,
        "status_label": _status_label(status, close_reason),
        "close_reason": close_reason,
        "min_price": bot.get("min_price") or bot.get("curr_min_price"),
        "max_price": bot.get("max_price") or bot.get("curr_max_price"),
        "cell_number": bot.get("cell_number"),
        "leverage": bot.get("leverage") or bot.get("real_leverage"),
        "total_investment": bot.get("total_investment") or bot.get("init_margin"),
        "realised_pnl": pnl,
        "unrealised_pnl": bot.get("unrealised_pnl") or bot.get("unrealized_pnl") or "0",
        "pnl": pnl,
        "pnl_per": bot.get("pnl_per"),
        "settlement": bot.get("settlement_assets"),
        "total_apr": bot.get("total_apr") or bot.get("grid_apr"),
        "mark_price": bot.get("mark_price") or bot.get("last_price") or bot.get("entry_price"),
        "liquidation_price": bot.get("liquidation_price"),
        "is_active": _is_active_status(status),
        "raw": bot,
    }


def _is_active_status(status: str) -> bool:
    s = str(status).upper()
    if "COMPLETED" in s:
        return False
    if "CANCELLING" in s or "INITIALIZING" in s or "AWAIT" in s:
        return True
    return "RUNNING" in s or "ACTIVE" in s or "INIT" in s


def _status_label(status: str, close_reason: str = "") -> str:
    s = str(status).upper()
    reason = str(close_reason).upper()
    if "FBU" in reason or "FBU_FAIL" in reason:
        return "ошибка Funding Account"
    if "COMPLETED" in s or "CANCELED" in s or "STOP" in s:
        if "SL" in reason:
            return "остановлен (стоп-лосс)"
        if "TP" in reason:
            return "остановлен (тейк-профит)"
        return "завершён"
    if _is_active_status(s):
        return "работает"
    return status


def list_bots(refresh: bool = True, *, sync: bool = False) -> dict[str, Any]:
    cfg = load_config()
    if sync:
        sync_missing_bots(force=True)
    state = load_state()
    bots_out: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    newly_archived: list[dict[str, Any]] = []

    for entry in state.get("bots", []):
        bot_id = str(entry.get("bot_id", ""))
        if not bot_id:
            continue
        try:
            detail = get_bot_detail(bot_id, use_cache=refresh)
            norm = _normalize_detail(detail)
            norm["created_at"] = entry.get("created_at")
            if norm.get("is_active"):
                entry["status"] = "running"
                bots_out.append(norm)
                kept.append(entry)
            else:
                entry["status"] = "closed"
                entry["close_reason"] = norm.get("close_reason")
                entry["closed_at"] = entry.get("closed_at") or datetime.now(UTC).isoformat()
                newly_archived.append(_archive_bot(entry, norm))
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "not exist" in msg or "not found" in msg:
                entry["status"] = "closed"
                entry["closed_at"] = entry.get("closed_at") or datetime.now(UTC).isoformat()
                newly_archived.append(_archive_bot(entry))
            else:
                bots_out.append({**entry, "error": str(exc), "is_active": True})
                kept.append(entry)

    if newly_archived:
        for item in newly_archived:
            record_sl_cooldown(state, item)
        state["history"] = _merge_history(newly_archived, state.get("history", []))
    state["bots"] = kept
    save_state(state)

    active = [b for b in bots_out if b.get("is_active")]
    total_pnl = sum(float(b.get("realised_pnl") or 0) + float(b.get("unrealised_pnl") or 0) for b in active)
    return {
        "config": cfg,
        "max_active_bots": cfg.get("max_active_bots", 2),
        "active_count": len(active),
        "bots": bots_out,
        "history_count": len(state.get("history", [])),
        "total_pnl": round(total_pnl, 4),
    }


def get_history_payload(limit: int = MAX_HISTORY) -> dict[str, Any]:
    state = load_state()
    history = state.get("history", [])[:limit]
    items: list[dict[str, Any]] = []
    for entry in history:
        pnl = float(entry.get("realised_pnl") or 0)
        items.append(
            {
                "bot_id": entry.get("bot_id"),
                "symbol": entry.get("symbol"),
                "pair": entry.get("pair"),
                "grid_mode_label": entry.get("grid_mode_label"),
                "status_label": entry.get("status_label") or "завершён",
                "close_reason": entry.get("close_reason"),
                "created_at": entry.get("created_at"),
                "closed_at": entry.get("closed_at"),
                "min_price": entry.get("min_price"),
                "max_price": entry.get("max_price"),
                "cell_number": entry.get("cell_number"),
                "leverage": entry.get("leverage"),
                "total_investment": entry.get("total_investment"),
                "realised_pnl": entry.get("realised_pnl"),
                "pnl_per": entry.get("pnl_per"),
                "settlement": entry.get("settlement"),
                "pnl": pnl,
            }
        )
    return {"history": items, "count": len(state.get("history", []))}


def get_status_payload() -> dict[str, Any]:
    try:
        get_credentials()
        creds_ok = True
        creds_error = None
    except RuntimeError as exc:
        creds_ok = False
        creds_error = str(exc)
    payload = list_bots(refresh=True)
    cfg = load_config()
    payload["tp_sl_preview"] = tp_sl_preview(cfg.get("defaults", {}))
    payload["credentials_ok"] = creds_ok
    payload["credentials_error"] = creds_error
    payload["demo_trading"] = is_demo_trading()
    if creds_ok:
        try:
            payload["funding"] = funding_status(cfg)
        except RuntimeError as exc:
            payload["funding"] = {"ok": False, "error": str(exc)}
    return payload
