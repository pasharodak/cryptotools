"""Multi-user tenant isolation for CryptoTools.

Manages users.json, encrypted Bybit secrets, per-tenant configs/ports/env,
session JWTs, bot API proxying, and systemd lifecycle helpers.

Dependencies: stdlib + cryptography.fernet only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

BASE = Path(os.environ.get("CT_BASE", "/home/cryptotools/app"))
USERS_FILE = BASE / "user_data" / "users.json"
SECRETS_DIR = BASE / "user_data" / "secrets"
TENANTS_DIR = BASE / "user_data" / "tenants"
MASTER_KEY_FILE = BASE / "user_data" / ".secrets_master"

PORT_BASE = 18100  # finder=base+3n, strategy=base+3n+1, grid=base+3n+2

ADMIN_BOT_PORTS: dict[str, int] = {
    "finder": 8080,
    "strategy": 8081,
    "grid": 8082,
}

BOT_NAMES = ("finder", "strategy", "grid")
CONFIG_FILES: dict[str, str] = {
    "finder": "config.json",
    "strategy": "config_strategy.json",
    "grid": "config_grid.json",
}

DEFAULT_TENANT_LIMITS = {"finder": 1, "strategy": 1, "grid": 1}
DEFAULT_TENANT_STAKES = {"finder": 5, "strategy": 5, "grid": 10}

PBKDF2_ITERATIONS = 390_000
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 days
_USERS_LOCK = threading.RLock()

_UNIT_TEMPLATES = {
    "finder": "cryptotools-finder@{id}.service",
    "strategy": "cryptotools-strategy@{id}.service",
    "grid": "cryptotools-grid@{id}.service",
}


# ---------------------------------------------------------------------------
# Password hashing (pbkdf2_hmac SHA-256)
# ---------------------------------------------------------------------------

def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Return ``pbkdf2$iterations$salt_hex$hash_hex``."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return f"pbkdf2${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time verify against ``hash_password`` output."""
    try:
        algo, iter_s, salt_hex, hash_hex = password_hash.split("$", 3)
    except (ValueError, AttributeError):
        return False
    if algo != "pbkdf2":
        return False
    try:
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(digest, expected)


# ---------------------------------------------------------------------------
# Master key / Fernet
# ---------------------------------------------------------------------------

def _ensure_user_data_dir() -> Path:
    d = BASE / "user_data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_master_key() -> str:
    """Resolve secrets master key.

    Order: ``SECRETS_MASTER_KEY`` → ``FREQUI_JWT_SECRET`` → generate and
    persist to ``user_data/.secrets_master`` (mode 0o600; owner-only).
    """
    for env_name in ("SECRETS_MASTER_KEY", "FREQUI_JWT_SECRET"):
        val = (os.environ.get(env_name) or "").strip()
        if val:
            return val

    _ensure_user_data_dir()
    if MASTER_KEY_FILE.is_file():
        raw = MASTER_KEY_FILE.read_text(encoding="utf-8").strip()
        if raw:
            return raw

    # Persist a new random key; chmod 600 so only the service user can read it.
    key = secrets.token_urlsafe(32)
    MASTER_KEY_FILE.write_text(key + "\n", encoding="utf-8")
    try:
        os.chmod(MASTER_KEY_FILE, 0o600)
    except OSError:
        pass
    return key


def _fernet() -> Fernet:
    """Fernet instance derived from the master key (SHA-256 → urlsafe b64)."""
    master = get_master_key().encode("utf-8")
    # Accept raw Fernet keys (32 url-safe-b64 bytes) if provided as-is.
    try:
        if len(master) == 44:
            return Fernet(master)
    except (ValueError, TypeError):
        pass
    digest = hashlib.sha256(master).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secrets(payload: dict[str, str]) -> bytes:
    """Encrypt a secrets dict (JSON) with Fernet."""
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _fernet().encrypt(data)


def decrypt_secrets(token: bytes) -> dict[str, Any]:
    """Decrypt Fernet token to secrets dict. Raises ValueError on failure."""
    try:
        raw = _fernet().decrypt(token)
    except InvalidToken as exc:
        raise ValueError("invalid or corrupted secrets token") from exc
    obj = json.loads(raw.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("secrets payload must be an object")
    return obj


# ---------------------------------------------------------------------------
# Users store
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _atomic_write_json(path: Path, payload: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def load_users() -> list[dict[str, Any]]:
    """Load user records from users.json (empty list if missing)."""
    if not USERS_FILE.is_file():
        return []
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return [u for u in data if isinstance(u, dict)]
    if isinstance(data, dict):
        users = data.get("users", [])
        if isinstance(users, list):
            return [u for u in users if isinstance(u, dict)]
    return []


def save_users(users: list[dict[str, Any]]) -> None:
    """Persist users list to users.json."""
    with _USERS_LOCK:
        _atomic_write_json(USERS_FILE, {"users": users}, mode=0o600)


def ensure_users_migrated() -> list[dict[str, Any]]:
    """If users.json is missing, create admin from FREQUI_USERNAME/PASSWORD.

    Never overwrite an existing users.json that failed to parse or is empty
    due to transient IO errors — that previously wiped tenant users.
    """
    with _USERS_LOCK:
        if USERS_FILE.is_file():
            try:
                raw = USERS_FILE.read_text(encoding="utf-8")
                data = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                # Keep broken file; operator must repair. Do not recreate admin-only.
                import logging

                logging.getLogger(__name__).error(
                    "users.json unreadable (%s) — refusing to wipe; size=%s",
                    exc,
                    USERS_FILE.stat().st_size if USERS_FILE.is_file() else "?",
                )
                return load_users()

            users: list[dict[str, Any]] = []
            if isinstance(data, list):
                users = [u for u in data if isinstance(u, dict)]
            elif isinstance(data, dict):
                maybe = data.get("users", [])
                if isinstance(maybe, list):
                    users = [u for u in maybe if isinstance(u, dict)]
            if users:
                return users
            # File exists but empty list — still do not auto-wipe; bootstrap only if no tenants.
            tenants_root = TENANTS_DIR
            if tenants_root.is_dir() and any(tenants_root.iterdir()):
                import logging

                logging.getLogger(__name__).error(
                    "users.json empty but tenants/ present — refusing auto-recreate"
                )
                return []
            # Fall through to create admin only when truly empty install.

        username = (os.environ.get("FREQUI_USERNAME") or "cryptotools").strip()
        password = os.environ.get("FREQUI_PASSWORD") or ""
        if not password:
            # Still create admin so the store exists; login will fail until set.
            password = secrets.token_urlsafe(16)

        admin = {
            "id": "admin",
            "username": username,
            "password_hash": hash_password(password),
            "role": "admin",
            "enabled": True,
            "ports": dict(ADMIN_BOT_PORTS),
            "created_at": _utc_now_iso(),
        }
        save_users([admin])
        return [admin]


def get_user_by_username(username: str) -> Optional[dict[str, Any]]:
    ensure_users_migrated()
    uname = (username or "").strip().lower()
    for user in load_users():
        if str(user.get("username", "")).lower() == uname:
            return user
    return None


def get_user_by_id(user_id: str) -> Optional[dict[str, Any]]:
    ensure_users_migrated()
    for user in load_users():
        if str(user.get("id")) == str(user_id):
            return user
    return None


def is_admin(user: Optional[dict[str, Any]]) -> bool:
    return bool(user) and str(user.get("role", "")).lower() == "admin"


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in user.items() if k != "password_hash"}
    return out


def list_users_public() -> list[dict[str, Any]]:
    """All users without password_hash."""
    ensure_users_migrated()
    return [_public_user(u) for u in load_users()]


def _allocate_ports(users: list[dict[str, Any]]) -> dict[str, int]:
    used: set[int] = set()
    for u in users:
        ports = u.get("ports") or {}
        if not isinstance(ports, dict):
            continue
        for bot in BOT_NAMES:
            try:
                used.add(int(ports[bot]))
            except (KeyError, TypeError, ValueError):
                pass
    # Reserve admin ports always.
    used.update(ADMIN_BOT_PORTS.values())

    n = 0
    while True:
        ports = {
            "finder": PORT_BASE + 3 * n,
            "strategy": PORT_BASE + 3 * n + 1,
            "grid": PORT_BASE + 3 * n + 2,
        }
        if not any(p in used for p in ports.values()):
            return ports
        n += 1
        if n > 5000:
            raise RuntimeError("exhausted tenant port pool")


def _make_user_id(username: str, existing_ids: set[str]) -> str:
    base = re.sub(r"[^a-z0-9_-]", "", (username or "").lower())
    if not base or base == "admin":
        base = "user"
    candidate = base
    i = 2
    while candidate in existing_ids:
        candidate = f"{base}{i}"
        i += 1
    # Extra uniqueness if somehow still colliding with a path.
    if candidate in existing_ids:
        candidate = f"u{uuid.uuid4().hex[:10]}"
    return candidate


def create_user(username: str, password: str) -> dict[str, Any]:
    """Create a role=user tenant, allocate ports, provision dirs. Returns user dict."""
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")
    if not password:
        raise ValueError("password is required")
    if len(username) > 64 or not re.match(r"^[A-Za-z0-9_.@-]+$", username):
        raise ValueError("invalid username")

    with _USERS_LOCK:
        users = ensure_users_migrated()
        if any(str(u.get("username", "")).lower() == username.lower() for u in users):
            raise ValueError(f"username already exists: {username}")

        existing_ids = {str(u.get("id")) for u in users}
        user_id = _make_user_id(username, existing_ids)
        ports = _allocate_ports(users)

        user: dict[str, Any] = {
            "id": user_id,
            "username": username,
            "password_hash": hash_password(password),
            "role": "user",
            "enabled": True,
            "ports": ports,
            "created_at": _utc_now_iso(),
        }
        provision_tenant(user_id, ports)
        users.append(user)
        save_users(users)
        return _public_user(user)


def set_user_enabled(user_id: str, enabled: bool) -> dict[str, Any]:
    with _USERS_LOCK:
        users = ensure_users_migrated()
        for user in users:
            if str(user.get("id")) == str(user_id):
                if is_admin(user) and not enabled:
                    raise ValueError("cannot disable admin")
                user["enabled"] = bool(enabled)
                save_users(users)
                return _public_user(user)
        raise KeyError(f"user not found: {user_id}")


def set_user_password(user_id: str, password: str) -> dict[str, Any]:
    if not password:
        raise ValueError("password is required")
    with _USERS_LOCK:
        users = ensure_users_migrated()
        for user in users:
            if str(user.get("id")) == str(user_id):
                user["password_hash"] = hash_password(password)
                save_users(users)
                return _public_user(user)
        raise KeyError(f"user not found: {user_id}")


# ---------------------------------------------------------------------------
# Session JWT (HMAC-SHA256, stdlib)
# ---------------------------------------------------------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _jwt_secret() -> bytes:
    for env_name in ("SECRETS_MASTER_KEY", "FREQUI_JWT_SECRET"):
        val = (os.environ.get(env_name) or "").strip()
        if val:
            return val.encode("utf-8")
    return get_master_key().encode("utf-8")


def issue_token(user: dict[str, Any], *, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    """Issue session JWT with payload ``{sub, role, exp}``."""
    if not user or not user.get("id"):
        raise ValueError("user required")
    header = _b64url_encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()
    )
    payload_obj = {
        "sub": str(user["id"]),
        "role": str(user.get("role", "user")),
        "exp": int(time.time()) + int(ttl_seconds),
    }
    payload = _b64url_encode(
        json.dumps(payload_obj, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    sig = hmac.new(_jwt_secret(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url_encode(sig)}"


def verify_token(token: str) -> Optional[dict[str, Any]]:
    """Verify session JWT; return payload or None if invalid/expired."""
    if not token or not isinstance(token, str):
        return None
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    header_b64, payload_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        expected = hmac.new(_jwt_secret(), signing_input, hashlib.sha256).digest()
        got = _b64url_decode(sig_b64)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(expected, got):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        exp = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        return None
    if exp < int(time.time()):
        return None
    if "sub" not in payload or "role" not in payload:
        return None
    return payload


# ---------------------------------------------------------------------------
# API creds / tenant env
# ---------------------------------------------------------------------------

def derive_api_creds(user_id: str) -> tuple[str, str, str]:
    """Deterministic (username, password, jwt_secret) from master + user_id."""
    master = get_master_key().encode("utf-8")
    uid = str(user_id).encode("utf-8")

    def _derive(label: bytes, nbytes: int) -> str:
        digest = hmac.new(master, label + b"|" + uid, hashlib.sha256).digest()
        # Stretch if needed
        out = digest
        while len(out) < nbytes:
            out += hmac.new(master, out + label + uid, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(out[:nbytes]).decode("ascii").rstrip("=")

    username = f"ct_{re.sub(r'[^a-z0-9_-]', '', str(user_id).lower())[:24] or 'user'}"
    password = _derive(b"api-password", 24)
    jwt_secret = _derive(b"api-jwt", 32)
    return username, password, jwt_secret


def tenant_user_data(user_id: str) -> Path:
    """Path to ``user_data/tenants/{user_id}/``."""
    return TENANTS_DIR / str(user_id)


def _secrets_path(user_id: str) -> Path:
    return SECRETS_DIR / f"{user_id}.enc"


def load_user_secrets(user_id: str) -> Optional[dict[str, Any]]:
    path = _secrets_path(user_id)
    if not path.is_file():
        return None
    try:
        return decrypt_secrets(path.read_bytes())
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def write_tenant_env(user_id: str) -> Path:
    """Write ``tenants/{id}/.env`` with Bybit keys (if any) and FREQUI_* API creds.

    CTENGINE__* mappings are optional here — ``load_env.sh`` maps FREQUI_/BYBIT_
    into CTENGINE__ when sourcing the env file.
    """
    td = tenant_user_data(user_id)
    td.mkdir(parents=True, exist_ok=True)
    api_user, api_pass, jwt_secret = derive_api_creds(user_id)

    bybit_key = ""
    bybit_secret = ""
    sec = load_user_secrets(user_id)
    if sec:
        bybit_key = str(sec.get("bybit_api_key") or "")
        bybit_secret = str(sec.get("bybit_api_secret") or "")

    lines = [
        f"BYBIT_API_KEY={bybit_key}",
        f"BYBIT_API_SECRET={bybit_secret}",
        f"FREQUI_USERNAME={api_user}",
        f"FREQUI_PASSWORD={api_pass}",
        f"FREQUI_JWT_SECRET={jwt_secret}",
        # Optional direct mappings (load_env.sh also sets these from FREQUI_/BYBIT_):
        f"CTENGINE__EXCHANGE__KEY={bybit_key}",
        f"CTENGINE__EXCHANGE__SECRET={bybit_secret}",
        f"CTENGINE__API_SERVER__USERNAME={api_user}",
        f"CTENGINE__API_SERVER__PASSWORD={api_pass}",
        f"CTENGINE__API_SERVER__JWT_SECRET_KEY={jwt_secret}",
        f"CT_ENABLED_STRATEGIES={td / 'enabled_strategies.json'}",
    ]
    env_path = td / ".env"
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass
    return env_path


def save_user_secrets(user_id: str, key: str, secret: str) -> None:
    """Encrypt and store Bybit API credentials; refresh tenant .env."""
    key = (key or "").strip()
    secret = (secret or "").strip()
    if not key or not secret:
        raise ValueError("bybit api key and secret are required")
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    token = encrypt_secrets(
        {"bybit_api_key": key, "bybit_api_secret": secret}
    )
    path = _secrets_path(user_id)
    path.write_bytes(token)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    # Keep tenant env in sync when tenant dir exists (or create env early).
    write_tenant_env(user_id)


def secrets_status(user_id: str) -> dict[str, Any]:
    """Status of stored secrets for a tenant (no secret values)."""
    sec = load_user_secrets(user_id)
    has = bool(
        sec
        and str(sec.get("bybit_api_key") or "").strip()
        and str(sec.get("bybit_api_secret") or "").strip()
    )
    return {
        "user_id": str(user_id),
        "has_secrets": has,
        "has_bybit_api_key": bool(sec and str(sec.get("bybit_api_key") or "").strip()),
        "has_bybit_api_secret": bool(
            sec and str(sec.get("bybit_api_secret") or "").strip()
        ),
    }


def admin_secrets_status() -> dict[str, Any]:
    """Admin Bybit secrets come from process env (not duplicated in .enc)."""
    key = (os.environ.get("BYBIT_API_KEY") or "").strip()
    secret = (os.environ.get("BYBIT_API_SECRET") or "").strip()
    return {
        "user_id": "admin",
        "has_secrets": bool(key and secret),
        "has_bybit_api_key": bool(key),
        "has_bybit_api_secret": bool(secret),
        "source": "env",
    }


# ---------------------------------------------------------------------------
# Tenant provisioning
# ---------------------------------------------------------------------------

def _minimal_config(bot: str, user_id: str, port: int, stake: float) -> dict[str, Any]:
    strategy_by_bot = {
        "finder": "TradeFinderStrategy",
        "strategy": "MultiStrategyRouter",
        "grid": "VolatilityGridStrategy",
    }
    return {
        "max_open_trades": 1,
        "stake_currency": "USDT",
        "stake_amount": stake,
        "tradable_balance_ratio": 0.95,
        "dry_run": False,
        "db_url": f"sqlite:///user_data/tenants/{user_id}/tradesv3-{bot}.sqlite",
        "cancel_open_orders_on_exit": True,
        "timeframe": "5m",
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "minimal_roi": {"0": 0.05},
        "stoploss": -0.15,
        "trailing_stop": False,
        "entry_pricing": {
            "price_side": "other",
            "use_order_book": True,
            "order_book_top": 1,
        },
        "exit_pricing": {
            "price_side": "other",
            "use_order_book": True,
            "order_book_top": 1,
        },
        "order_types": {
            "entry": "market",
            "exit": "market",
            "stoploss": "market",
            "stoploss_on_exchange": True,
        },
        "pairlists": [{"method": "StaticPairList"}],
        "exchange": {
            "name": "bybit",
            "key": "",
            "secret": "",
            "pair_whitelist": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
            "pair_blacklist": [],
        },
        "telegram": {"enabled": False},
        "api_server": {
            "enabled": True,
            "listen_ip_address": "127.0.0.1",
            "listen_port": port,
            "verbosity": "error",
            "enable_openapi": True,
            "jwt_secret_key": "",
            "username": "",
            "password": "",
        },
        "bot_name": f"cryptotools-{bot}-{user_id}",
        "strategy": strategy_by_bot.get(bot, "MultiStrategyRouter"),
        "strategy_path": "user_data/strategies",
        "initial_state": "running",
        "internals": {"process_throttle_secs": 5},
    }


def _patch_tenant_config(
    cfg: dict[str, Any],
    *,
    bot: str,
    user_id: str,
    port: int,
    stake: float,
) -> dict[str, Any]:
    cfg = dict(cfg)
    cfg["max_open_trades"] = 1
    cfg["stake_amount"] = stake
    cfg["db_url"] = f"sqlite:///user_data/tenants/{user_id}/tradesv3-{bot}.sqlite"
    cfg["bot_name"] = f"cryptotools-{bot}-{user_id}"

    exchange = dict(cfg.get("exchange") or {})
    exchange["key"] = ""
    exchange["secret"] = ""
    cfg["exchange"] = exchange

    api = dict(cfg.get("api_server") or {})
    api["enabled"] = True
    api["listen_ip_address"] = "127.0.0.1"
    api["listen_port"] = int(port)
    # Creds come from env / load_env.sh — keep blank in config.
    api.setdefault("username", "")
    api.setdefault("password", "")
    api.setdefault("jwt_secret_key", "")
    cfg["api_server"] = api
    return cfg


def provision_tenant(user_id: str, ports: dict[str, int]) -> Path:
    """Create tenant directory tree, configs, limits, env."""
    user_id = str(user_id)
    td = tenant_user_data(user_id)
    (td / "logs").mkdir(parents=True, exist_ok=True)

    global_ud = BASE / "user_data"
    for bot in BOT_NAMES:
        fname = CONFIG_FILES[bot]
        src = global_ud / fname
        stake = float(DEFAULT_TENANT_STAKES[bot])
        port = int(ports[bot])
        if src.is_file():
            try:
                cfg = json.loads(src.read_text(encoding="utf-8"))
                if not isinstance(cfg, dict):
                    raise ValueError("config root must be object")
            except (OSError, json.JSONDecodeError, ValueError):
                cfg = _minimal_config(bot, user_id, port, stake)
        else:
            cfg = _minimal_config(bot, user_id, port, stake)
        cfg = _patch_tenant_config(
            cfg, bot=bot, user_id=user_id, port=port, stake=stake
        )
        _atomic_write_json(td / fname, cfg, mode=0o644)

    bot_limits = {
        "max_open_trades": dict(DEFAULT_TENANT_LIMITS),
        "stake_amount": dict(DEFAULT_TENANT_STAKES),
    }
    _atomic_write_json(td / "bot_limits.json", bot_limits, mode=0o644)

    enabled_src = global_ud / "enabled_strategies.json"
    if enabled_src.is_file():
        try:
            shutil.copy2(enabled_src, td / "enabled_strategies.json")
        except OSError:
            _atomic_write_json(td / "enabled_strategies.json", {}, mode=0o644)
    else:
        _atomic_write_json(td / "enabled_strategies.json", {}, mode=0o644)

    bybit_cfg = {
        "defaults": {
            "grid_mode": 1,
            "grid_type": 1,
            "cell_number": 15,
            "leverage": "3",
            "total_investment": "10",
            "price_range_pct": 0.09,
        },
        "max_active_bots": 1,
        "auto_fund_transfer": False,
    }
    _atomic_write_json(td / "bybit_grid_config.json", bybit_cfg, mode=0o644)
    _atomic_write_json(
        td / "bybit_grid_state.json",
        {"bots": [], "history": []},
        mode=0o644,
    )

    write_tenant_env(user_id)
    return td


def bot_ports_for_user(user: dict[str, Any]) -> dict[str, int]:
    """Return ``{finder, strategy, grid}`` listen ports for the user."""
    if is_admin(user):
        return dict(ADMIN_BOT_PORTS)
    ports = user.get("ports") or {}
    out: dict[str, int] = {}
    for bot in BOT_NAMES:
        try:
            out[bot] = int(ports[bot])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"user missing port for {bot}") from exc
    return out


# ---------------------------------------------------------------------------
# systemd helpers
# ---------------------------------------------------------------------------

def _unit_name(bot: str, user_id: str) -> str:
    tmpl = _UNIT_TEMPLATES.get(bot)
    if not tmpl:
        raise ValueError(f"unknown bot: {bot}")
    return tmpl.format(id=user_id)


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run tenant unit actions via sudo wrapper (no password)."""
    wrapper = Path(os.environ.get("CT_BASE", "/home/cryptotools/app")) / "deploy" / "tenant-systemctl.sh"
    if len(args) == 1 and args[0] == "daemon-reload":
        cmd = ["sudo", "-n", str(wrapper), "daemon-reload"]
    elif len(args) >= 2:
        cmd = ["sudo", "-n", str(wrapper), args[0], args[1]]
    else:
        cmd = ["sudo", "-n", "systemctl", *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr=str(exc)
        )


def start_tenant_bots(user_id: str) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for bot in BOT_NAMES:
        unit = _unit_name(bot, user_id)
        # Persist across reboot — start alone does not enable the instance.
        en = _systemctl("enable", unit)
        cp = _systemctl("start", unit)
        results[bot] = {
            "unit": unit,
            "ok": cp.returncode == 0,
            "returncode": cp.returncode,
            "stderr": (cp.stderr or "").strip(),
            "enabled": en.returncode == 0,
            "enable_stderr": (en.stderr or "").strip(),
        }
    return results


def stop_tenant_bots(user_id: str) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for bot in BOT_NAMES:
        unit = _unit_name(bot, user_id)
        cp = _systemctl("stop", unit)
        # Keep disabled so reboot does not revive stopped tenants.
        dis = _systemctl("disable", unit)
        results[bot] = {
            "unit": unit,
            "ok": cp.returncode == 0,
            "returncode": cp.returncode,
            "stderr": (cp.stderr or "").strip(),
            "disabled": dis.returncode == 0,
        }
    return results


def restart_tenant_bots(user_id: str) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for bot in BOT_NAMES:
        unit = _unit_name(bot, user_id)
        en = _systemctl("enable", unit)
        cp = _systemctl("restart", unit)
        results[bot] = {
            "unit": unit,
            "ok": cp.returncode == 0,
            "returncode": cp.returncode,
            "stderr": (cp.stderr or "").strip(),
            "enabled": en.returncode == 0,
            "enable_stderr": (en.stderr or "").strip(),
        }
    return results


def tenant_bots_status(user_id: str) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for bot in BOT_NAMES:
        unit = _unit_name(bot, user_id)
        cp = _systemctl("is-active", unit)
        state = (cp.stdout or "").strip() or "unknown"
        results[bot] = {
            "unit": unit,
            "active": state == "active",
            "state": state,
            "returncode": cp.returncode,
        }
    return results


# ---------------------------------------------------------------------------
# Bot API proxy
# ---------------------------------------------------------------------------

def _basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def proxy_bot_request(
    user: dict[str, Any],
    bot: str,
    method: str,
    path: str,
    query: Optional[dict[str, Any]] = None,
    body_bytes: Optional[bytes] = None,
    content_type: Optional[str] = None,
) -> tuple[int, dict[str, str], bytes]:
    """Proxy an HTTP call to the user's bot API.

    Hits ``http://127.0.0.1:{port}/api/v1/{path}`` with Basic auth.
    Admin uses ``FREQUI_USERNAME``/``FREQUI_PASSWORD`` and ports 8080–8082.
    Returns ``(status, headers_dict, body_bytes)``.
    """
    bot = str(bot).lower().strip()
    if bot not in BOT_NAMES:
        return 400, {"content-type": "application/json"}, json.dumps(
            {"error": f"unknown bot: {bot}"}
        ).encode()

    ports = bot_ports_for_user(user)
    port = ports[bot]

    if is_admin(user):
        api_user = (os.environ.get("FREQUI_USERNAME") or "cryptotools").strip()
        api_pass = os.environ.get("FREQUI_PASSWORD") or ""
    else:
        api_user, api_pass, _jwt = derive_api_creds(str(user["id"]))

    path = (path or "").lstrip("/")
    # Preserve path segments safely
    safe_path = "/".join(quote(seg, safe="") for seg in path.split("/") if seg != "")
    url = f"http://127.0.0.1:{port}/api/v1/{safe_path}"
    if query:
        # Drop None values; stringify the rest
        q = {k: v for k, v in query.items() if v is not None}
        if q:
            url = f"{url}?{urlencode(q, doseq=True)}"

    headers = {
        "Authorization": _basic_auth_header(api_user, api_pass),
        "Accept": "application/json",
    }
    data = body_bytes if body_bytes is not None else None
    if data is not None and content_type:
        headers["Content-Type"] = content_type
    elif data is not None:
        headers["Content-Type"] = "application/json"

    req = Request(url, data=data, headers=headers, method=(method or "GET").upper())
    try:
        with urlopen(req, timeout=60) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return int(resp.status), resp_headers, resp.read()
    except HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        body = exc.read() if hasattr(exc, "read") else b""
        return int(exc.code), resp_headers, body or b""
    except URLError as exc:
        err = json.dumps({"error": "upstream_unreachable", "detail": str(exc.reason)}).encode()
        return 502, {"content-type": "application/json"}, err
    except TimeoutError:
        err = json.dumps({"error": "upstream_timeout"}).encode()
        return 504, {"content-type": "application/json"}, err


__all__ = [
    "BASE",
    "USERS_FILE",
    "SECRETS_DIR",
    "TENANTS_DIR",
    "PORT_BASE",
    "ADMIN_BOT_PORTS",
    "hash_password",
    "verify_password",
    "get_master_key",
    "encrypt_secrets",
    "decrypt_secrets",
    "ensure_users_migrated",
    "load_users",
    "save_users",
    "get_user_by_username",
    "get_user_by_id",
    "create_user",
    "set_user_enabled",
    "set_user_password",
    "list_users_public",
    "issue_token",
    "verify_token",
    "provision_tenant",
    "derive_api_creds",
    "write_tenant_env",
    "save_user_secrets",
    "secrets_status",
    "admin_secrets_status",
    "bot_ports_for_user",
    "start_tenant_bots",
    "stop_tenant_bots",
    "restart_tenant_bots",
    "tenant_bots_status",
    "proxy_bot_request",
    "tenant_user_data",
    "is_admin",
]
