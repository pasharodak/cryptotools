"""Multi-user tenant isolation for CryptoTools.

Manages users.json, encrypted Bybit secrets, per-tenant configs/ports/env,
session JWTs, bot API proxying, and systemd lifecycle helpers.

Dependencies: stdlib + cryptography.fernet only.
"""

from __future__ import annotations
import sys
from pathlib import Path as _BootstrapPath

_scripts_root = _BootstrapPath(__file__).resolve().parents[1]
if str(_scripts_root) not in sys.path:
    sys.path.insert(0, str(_scripts_root))

import base64
import hashlib
import hmac
import json
import logging
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

from tenants.trading_context import (
    BINANCE_CONTEXT_SUFFIX,
    DEFAULT_EXCHANGE,
    DEFAULT_TRADE_ON,
    DEMO_CONTEXT_SUFFIX,
    SUPPORTED_EXCHANGES,
    TradingContext,
    context_exchange,
    context_mode,
    demo_context_id,
    exchange_context_id,
    flag_context_id,
    is_demo_context_id,
    iter_user_contexts,
    live_context_id,
    normalize_account_mode,
    normalize_exchange,
    normalize_trade_on,
    parent_user_id_of_context,
    trading_context_id,
)

log = logging.getLogger("tenant_manager")

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
SESSION_TTL_SECONDS = 12 * 3600  # 12 hours (was 7 days)
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
    """Resolve secrets master key (Fernet).

    Order: ``SECRETS_MASTER_KEY`` → ``FREQUI_JWT_SECRET`` (legacy) → generate and
    persist to ``user_data/.secrets_master`` (mode 0o600; owner-only).
    Prefer a dedicated ``SECRETS_MASTER_KEY`` so JWT rotation does not break .enc.
    """
    for env_name in ("SECRETS_MASTER_KEY", "FREQUI_JWT_SECRET"):
        val = (os.environ.get(env_name) or "").strip().replace("\r", "")
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


def encrypt_secrets(payload: dict[str, Any]) -> bytes:
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


def is_impersonating(user: Optional[dict[str, Any]]) -> bool:
    return bool(user and user.get("_imp_by"))


def real_admin_id(user: Optional[dict[str, Any]]) -> Optional[str]:
    """Admin id when impersonating; else None."""
    if not user:
        return None
    imp = user.get("_imp_by")
    return str(imp) if imp else None


def account_kind(user: Optional[dict[str, Any]]) -> str:
    """``live`` | ``demo`` | ``admin``. Legacy ``demo`` login rows may still exist pre-migration."""
    if not user:
        return "live"
    if is_admin(user) and not is_impersonating(user):
        return "admin"
    kind = str(user.get("account_kind") or "").strip().lower()
    if kind in ("live", "demo"):
        return kind
    if user.get("parent_user_id"):
        return "demo"
    return "live"


def is_demo_account(user: Optional[dict[str, Any]]) -> bool:
    return account_kind(user) == "demo"


def is_live_account(user: Optional[dict[str, Any]]) -> bool:
    return account_kind(user) == "live"


def linked_demo_user(user: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Legacy helper — returns old ``*_demo`` login row if still present."""
    if not user:
        return None
    did = str(user.get("demo_user_id") or "").strip()
    return get_user_by_id(did) if did else None


def linked_parent_user(user: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not user:
        return None
    pid = str(user.get("parent_user_id") or "").strip()
    return get_user_by_id(pid) if pid else None


def telegram_owner_id(user_id: str) -> str:
    """Resolve user id that owns the Telegram link (parent for legacy demo accounts / contexts)."""
    uid = str(user_id or "").strip()
    if not uid or uid == "admin":
        return uid or "admin"
    if is_demo_context_id(uid):
        return parent_user_id_of_context(uid)
    user = get_user_by_id(uid)
    if not user:
        return parent_user_id_of_context(uid)
    parent = linked_parent_user(user)
    if parent and parent.get("id"):
        return str(parent["id"])
    return uid


def account_switch_info(user: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Deprecated Live↔Demo switcher fields (always off). Kept for API compat."""
    if not user or (is_admin(user) and not is_impersonating(user)):
        kind = "admin"
    else:
        kind = "live"
    return {
        "account_kind": kind,
        "can_switch_demo": False,
        "can_switch_live": False,
        "linked_username": None,
        "linked_user_id": None,
    }


# UI / bot blocks that admin can grant to a user. Missing allowlist = all.
UI_BLOCKS: list[dict[str, str]] = [
    {"id": "test_strategies", "name": "Тестовые стратегии"},
    {"id": "strategy", "name": "Стратегии"},
    {"id": "cursor50", "name": "Cursor 50 (снят)"},
    {"id": "llm", "name": "DeepSeek (снят)"},
    {"id": "history", "name": "История сделок"},
    {"id": "dashboard", "name": "Дашборд PnL"},
    {"id": "rating", "name": "Рейтинг"},
    {"id": "report", "name": "Отчёт"},
]
UI_BLOCK_IDS: frozenset[str] = frozenset(b["id"] for b in UI_BLOCKS)
# Hidden from permission editor/defaults; kept in UI_BLOCK_IDS so saved allowlists stay valid.
RETIRED_BLOCK_IDS: frozenset[str] = frozenset({"llm", "cursor50"})

# One account: main strategies + optional test panel (demo keys) + views.
LIVE_DEFAULT_ALLOWED_BLOCKS: list[str] = [
    b["id"] for b in UI_BLOCKS if b["id"] not in RETIRED_BLOCK_IDS
]
# Legacy demo-login defaults (migration may still reference).
DEMO_DEFAULT_ALLOWED_BLOCKS: list[str] = [
    b["id"]
    for b in UI_BLOCKS
    if b["id"]
    in {
        "strategy",
        "test_strategies",
        "history",
        "dashboard",
        "rating",
        "report",
    }
]


def _normalize_id_list(value: Any, valid_ids: set[str] | frozenset[str]) -> Optional[list[str]]:
    """None / omit → unrestricted. List (incl. empty) → only those ids."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("allowlist must be a list or null")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        sid = str(item or "").strip()
        if not sid or sid in seen:
            continue
        if sid not in valid_ids:
            raise ValueError(f"unknown id: {sid}")
        out.append(sid)
        seen.add(sid)
    return out


def normalize_allowed_strategies(
    value: Any, *, valid_ids: set[str] | frozenset[str]
) -> Optional[list[str]]:
    return _normalize_id_list(value, valid_ids)


def normalize_allowed_blocks(value: Any) -> Optional[list[str]]:
    return _normalize_id_list(value, UI_BLOCK_IDS)


def user_allowed_strategies(user: Optional[dict[str, Any]]) -> Optional[list[str]]:
    """None = unrestricted. Admin (not impersonating) always unrestricted."""
    if not user:
        return None
    if is_admin(user) and not is_impersonating(user):
        return None
    raw = user.get("allowed_strategies")
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    return [str(x) for x in raw if str(x or "").strip()]


def user_allowed_blocks(user: Optional[dict[str, Any]]) -> Optional[list[str]]:
    if not user:
        return None
    if is_admin(user) and not is_impersonating(user):
        return None
    raw = user.get("allowed_blocks")
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    return [str(x) for x in raw if str(x or "").strip()]


def user_may_use_strategy(user: Optional[dict[str, Any]], strategy_id: str) -> bool:
    allow = user_allowed_strategies(user)
    if allow is None:
        return True
    return str(strategy_id) in allow


def user_may_use_block(user: Optional[dict[str, Any]], block_id: str) -> bool:
    bid = str(block_id or "")
    if bid in RETIRED_BLOCK_IDS:
        return False
    allow = user_allowed_blocks(user)
    if allow is None:
        return True
    return bid in allow


def user_may_use_bot(user: Optional[dict[str, Any]], bot: str) -> bool:
    bot = str(bot or "")
    if bot == "strategy":
        return user_may_use_block(user, "strategy") or user_may_use_block(
            user, "test_strategies"
        )
    if bot in ("grid", "finder"):
        return user_may_use_block(user, bot)
    return True


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in user.items() if k not in ("password_hash", "_imp_by")}
    # Normalize allowlists for clients (omit means all).
    if "allowed_strategies" in user:
        out["allowed_strategies"] = user.get("allowed_strategies")
    if "allowed_blocks" in user:
        out["allowed_blocks"] = user.get("allowed_blocks")
    out["account_kind"] = account_kind(user)
    if user.get("demo_user_id"):
        out["demo_user_id"] = user.get("demo_user_id")
    if user.get("parent_user_id"):
        out["parent_user_id"] = user.get("parent_user_id")
    return out


def list_users_public(*, include_demo: bool = False) -> list[dict[str, Any]]:
    """All users without password_hash. Demo children hidden by default."""
    ensure_users_migrated()
    out: list[dict[str, Any]] = []
    for u in load_users():
        if not include_demo and is_demo_account(u):
            continue
        pub = _public_user(u)
        if not include_demo and pub.get("demo_user_id"):
            demo = linked_demo_user(u)
            if demo:
                pub["demo_username"] = demo.get("username")
        out.append(pub)
    return out


def permission_catalog(strategy_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Catalog for admin permission editor."""
    strategies = [
        {
            "id": s["id"],
            "name": s.get("name") or s["id"],
            "num": s.get("num"),
            "test_group": bool(s.get("test_group")),
        }
        for s in strategy_rows
        if isinstance(s, dict) and s.get("id")
    ]
    return {
        "blocks": [b for b in UI_BLOCKS if b["id"] not in RETIRED_BLOCK_IDS],
        "strategies": strategies,
    }


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
    """Create a single login user (live). Demo keys are optional later via secrets."""
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")
    if not password:
        raise ValueError("password is required")
    if len(username) > 64 or not re.match(r"^[A-Za-z0-9_.@-]+$", username):
        raise ValueError("invalid username")
    if username.lower().endswith("_demo"):
        raise ValueError("create the live username without _demo suffix")

    with _USERS_LOCK:
        users = ensure_users_migrated()
        lower_names = {str(u.get("username", "")).lower() for u in users}
        if username.lower() in lower_names:
            raise ValueError(f"username already exists: {username}")

        existing_ids = {str(u.get("id")) for u in users}
        live_id = _make_user_id(username, existing_ids)
        live_ports = _allocate_ports(users)
        pw_hash = hash_password(password)
        now = _utc_now_iso()

        live: dict[str, Any] = {
            "id": live_id,
            "username": username,
            "password_hash": pw_hash,
            "role": "user",
            "enabled": True,
            "ports": live_ports,
            "created_at": now,
            "account_kind": "live",
            "allowed_strategies": None,
            "allowed_blocks": list(LIVE_DEFAULT_ALLOWED_BLOCKS),
        }

        provision_tenant(live_id, live_ports)
        try:
            import control.user_trading as ut

            ut.save_trading_flags(
                live_id, {"strategy": False, "grid": False, "finder": False}
            )
        except Exception:
            pass

        users.append(live)
        save_users(users)
        return _public_user(live)


def ensure_demo_pair_for_user(user_id: str) -> dict[str, Any]:
    """Deprecated: demo logins removed. Ensures dual-key demo context exists."""
    live = get_user_by_id(str(user_id))
    if not live:
        raise KeyError(f"user not found: {user_id}")
    if is_admin(live):
        raise ValueError("admin has no demo pair")
    if is_demo_account(live):
        raise ValueError("legacy demo accounts are removed — migrate first")
    ensure_demo_context(str(live["id"]))
    return _public_user(live)


def backfill_demo_pairs() -> dict[str, Any]:
    """Deprecated no-op alias — runs dual-key migration instead."""
    return migrate_remove_demo_accounts()


def _tenant_has_trade_data(tenant_id: str) -> bool:
    """True if tenant has open trades or non-trivial closed trade journal."""
    td = tenant_user_data(str(tenant_id))
    if not td.is_dir():
        return False
    for name in (
        "tradesv3-strategy.sqlite",
        "tradesv3-finder.sqlite",
        "tradesv3-grid.sqlite",
    ):
        path = td / name
        if not path.is_file() or path.stat().st_size < 4096:
            continue
        try:
            import sqlite3

            with sqlite3.connect(path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE is_open=1"
                ).fetchone()
                if row and int(row[0] or 0) > 0:
                    return True
                row2 = conn.execute("SELECT COUNT(*) FROM trades").fetchone()
                if row2 and int(row2[0] or 0) > 0:
                    return True
        except Exception:
            # Non-empty sqlite we cannot read — treat as precious.
            return True
    return False


def _rewrite_control_plane_user_id(old_id: str, new_id: str) -> None:
    """Move control_plane rows from old_id → new_id when new_id has no conflicting rows."""
    old_id = str(old_id)
    new_id = str(new_id)
    if old_id == new_id:
        return
    try:
        import control.control_plane as cp

        cp.ensure_init()
        conn = cp._connect()
        try:
            tables = (
                ("bot_desired", "user_id"),
                ("bot_observed", "user_id"),
                ("bot_commands", "user_id"),
                ("user_settings", "user_id"),
            )
            for table, col in tables:
                try:
                    conn.execute(
                        f"UPDATE OR IGNORE {table} SET {col}=? WHERE {col}=?",
                        (new_id, old_id),
                    )
                    conn.execute(f"DELETE FROM {table} WHERE {col}=?", (old_id,))
                except Exception:
                    pass
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("control_plane rewrite %s→%s failed: %s", old_id, new_id, exc)


def _move_tenant_dir(src_id: str, dst_id: str) -> bool:
    src = tenant_user_data(src_id)
    dst = tenant_user_data(dst_id)
    if not src.is_dir():
        return False
    if dst.exists():
        # Prefer keeping dst if both exist; caller decides.
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return True


def _move_secrets_file(src_id: str, dst_id: str) -> bool:
    src = _secrets_path(src_id)
    dst = _secrets_path(dst_id)
    if not src.is_file():
        return False
    if dst.is_file():
        return False
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return True


def migrate_remove_demo_accounts() -> dict[str, Any]:
    """Collapse legacy ``*_demo`` logins into ``{live_id}__demo`` contexts and delete demo users."""
    ensure_users_migrated()
    migrated: list[dict[str, Any]] = []
    deleted_empty: list[str] = []
    errors: list[dict[str, str]] = []
    remaining = 0
    with _USERS_LOCK:
        users = list(load_users())
        by_id = {str(u.get("id")): u for u in users if u.get("id")}
        demo_rows = [u for u in users if is_demo_account(u)]
        keep: list[dict[str, Any]] = []
        removed_ids: set[str] = set()

        for demo in demo_rows:
            demo_id = str(demo.get("id") or "")
            parent_id = str(demo.get("parent_user_id") or "").strip()
            if not parent_id:
                # Try reverse link from live.demo_user_id
                for u in users:
                    if str(u.get("demo_user_id") or "") == demo_id:
                        parent_id = str(u.get("id") or "")
                        break
            if not parent_id or parent_id not in by_id:
                errors.append({"demo_id": demo_id, "error": "missing parent"})
                # Still drop login row to satisfy "remove demo accounts"
                removed_ids.add(demo_id)
                try:
                    if tenant_user_data(demo_id).is_dir():
                        shutil.rmtree(tenant_user_data(demo_id), ignore_errors=True)
                    sp = _secrets_path(demo_id)
                    if sp.is_file():
                        sp.unlink(missing_ok=True)  # type: ignore[arg-type]
                except Exception:
                    pass
                continue

            target = demo_context_id(parent_id)
            try:
                has_data = _tenant_has_trade_data(demo_id)
                if has_data:
                    moved = _move_tenant_dir(demo_id, target)
                    if not moved and tenant_user_data(demo_id).is_dir():
                        # Target already exists — keep demo data under target if empty merge needed
                        if not _tenant_has_trade_data(target):
                            shutil.rmtree(tenant_user_data(target), ignore_errors=True)
                            _move_tenant_dir(demo_id, target)
                        else:
                            # Both have data — leave demo dir renamed with suffix backup
                            backup = f"{target}__legacy_{demo_id}"
                            _move_tenant_dir(demo_id, backup)
                            errors.append(
                                {
                                    "demo_id": demo_id,
                                    "error": f"both have trades; demo kept as {backup}",
                                }
                            )
                    _move_secrets_file(demo_id, target)
                    _rewrite_control_plane_user_id(demo_id, target)
                    # Force demo venue on moved secrets
                    try:
                        blob = _load_secrets_blob(target)
                        if blob.get("bybit_api_key") and blob.get("bybit_api_secret"):
                            blob["bybit_demo_trading"] = True
                            for p in blob.get("profiles") or []:
                                if isinstance(p, dict):
                                    p["demo_trading"] = True
                            _write_secrets_blob(target, blob)
                            write_tenant_env(target)
                            sync_tenant_demo_trading(target, True)
                    except Exception:
                        pass
                    migrated.append(
                        {"demo_id": demo_id, "parent_id": parent_id, "context_id": target}
                    )
                else:
                    # Empty house-mirror tenant — delete
                    try:
                        if tenant_user_data(demo_id).is_dir():
                            shutil.rmtree(tenant_user_data(demo_id), ignore_errors=True)
                        sp = _secrets_path(demo_id)
                        if sp.is_file():
                            sp.unlink(missing_ok=True)  # type: ignore[arg-type]
                        _rewrite_control_plane_user_id(demo_id, target)  # drop old rows
                        try:
                            import control.control_plane as cp

                            cp.ensure_init()
                            conn = cp._connect()
                            try:
                                for table in (
                                    "bot_desired",
                                    "bot_observed",
                                    "bot_commands",
                                    "user_settings",
                                ):
                                    conn.execute(
                                        f"DELETE FROM {table} WHERE user_id=?",
                                        (demo_id,),
                                    )
                                conn.commit()
                            finally:
                                conn.close()
                        except Exception:
                            pass
                    except Exception as exc:
                        errors.append({"demo_id": demo_id, "error": str(exc)})
                    deleted_empty.append(demo_id)
                removed_ids.add(demo_id)
            except Exception as exc:
                errors.append({"demo_id": demo_id, "error": str(exc)})
                removed_ids.add(demo_id)

        for u in users:
            uid = str(u.get("id") or "")
            if uid in removed_ids or is_demo_account(u):
                continue
            u.pop("demo_user_id", None)
            u.pop("parent_user_id", None)
            if not is_admin(u):
                u["account_kind"] = "live"
                # Ensure test panel available under dual-key model
                blocks = u.get("allowed_blocks")
                if isinstance(blocks, list) and "test_strategies" not in blocks:
                    u["allowed_blocks"] = list(
                        dict.fromkeys([*blocks, "test_strategies"])
                    )
            keep.append(u)

        save_users(keep)
        remaining = len(keep)

    try:
        import trading.trade_executor as te

        te.clear_exchange_cache(None)
    except Exception:
        pass

    return {
        "ok": True,
        "migrated": migrated,
        "deleted_empty": deleted_empty,
        "errors": errors,
        "remaining_users": remaining,
    }


def set_user_enabled(user_id: str, enabled: bool) -> dict[str, Any]:
    with _USERS_LOCK:
        users = ensure_users_migrated()
        target = None
        for user in users:
            if str(user.get("id")) == str(user_id):
                target = user
                break
        if not target:
            raise KeyError(f"user not found: {user_id}")
        if is_admin(target) and not enabled:
            raise ValueError("cannot disable admin")
        if is_demo_account(target):
            raise ValueError("legacy demo accounts cannot be toggled — run migration")
        target["enabled"] = bool(enabled)
        save_users(users)
        return _public_user(target)


def set_user_permissions(
    user_id: str,
    *,
    allowed_strategies: Any = ...,
    allowed_blocks: Any = ...,
    valid_strategy_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Update allowlists. Pass ``...`` to leave a field unchanged. ``None`` = unrestricted."""
    if allowed_strategies is ... and allowed_blocks is ...:
        raise ValueError("nothing to update")
    with _USERS_LOCK:
        users = ensure_users_migrated()
        for user in users:
            if str(user.get("id")) != str(user_id):
                continue
            if is_admin(user):
                raise ValueError("cannot set permissions on admin")
            if allowed_strategies is not ...:
                if valid_strategy_ids is None:
                    raise ValueError("valid_strategy_ids required")
                user["allowed_strategies"] = normalize_allowed_strategies(
                    allowed_strategies, valid_ids=valid_strategy_ids
                )
            if allowed_blocks is not ...:
                user["allowed_blocks"] = normalize_allowed_blocks(allowed_blocks)
            save_users(users)
            return _public_user(user)
        raise KeyError(f"user not found: {user_id}")


def patch_user(
    user_id: str,
    data: dict[str, Any],
    *,
    valid_strategy_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Patch enabled and/or permission allowlists."""
    if not isinstance(data, dict) or not data:
        raise ValueError("empty patch")
    keys = set(data.keys())
    allowed_keys = {"enabled", "allowed_strategies", "allowed_blocks"}
    unknown = keys - allowed_keys
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")

    with _USERS_LOCK:
        users = ensure_users_migrated()
        for user in users:
            if str(user.get("id")) != str(user_id):
                continue
            if "enabled" in data:
                enabled = bool(data["enabled"])
                if is_admin(user) and not enabled:
                    raise ValueError("cannot disable admin")
                if is_demo_account(user):
                    raise ValueError("legacy demo accounts cannot be patched — run migration")
                user["enabled"] = enabled
            if "allowed_strategies" in data or "allowed_blocks" in data:
                if is_admin(user):
                    raise ValueError("cannot set permissions on admin")
                if "allowed_strategies" in data:
                    if valid_strategy_ids is None:
                        raise ValueError("valid_strategy_ids required")
                    user["allowed_strategies"] = normalize_allowed_strategies(
                        data["allowed_strategies"], valid_ids=valid_strategy_ids
                    )
                if "allowed_blocks" in data:
                    user["allowed_blocks"] = normalize_allowed_blocks(data["allowed_blocks"])
            save_users(users)
            return _public_user(user)
        raise KeyError(f"user not found: {user_id}")


def set_user_password(user_id: str, password: str) -> dict[str, Any]:
    if not password:
        raise ValueError("password is required")
    with _USERS_LOCK:
        users = ensure_users_migrated()
        target = None
        for user in users:
            if str(user.get("id")) == str(user_id):
                target = user
                break
        if not target:
            raise KeyError(f"user not found: {user_id}")
        pw_hash = hash_password(password)
        target["password_hash"] = pw_hash
        save_users(users)
        return _public_user(target)


# ---------------------------------------------------------------------------
# Session JWT (HMAC-SHA256, stdlib)
# ---------------------------------------------------------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _jwt_secret() -> bytes:
    """Session JWT signing key — must stay separate from SECRETS_MASTER_KEY.

    Prefer ``FREQUI_JWT_SECRET`` only. Do not fall through to the Fernet master
    key once ``SECRETS_MASTER_KEY`` is set (rotating secrets must not invalidate
    all sessions incorrectly, and vice versa).
    """
    val = (os.environ.get("FREQUI_JWT_SECRET") or "").strip().replace("\r", "")
    if val:
        return val.encode("utf-8")
    # Legacy: before SECRETS_MASTER_KEY existed, JWT shared the Fernet master.
    return get_master_key().encode("utf-8")


def issue_token(
    user: dict[str, Any],
    *,
    ttl_seconds: int = SESSION_TTL_SECONDS,
    impersonated_by: Optional[str] = None,
) -> str:
    """Issue session JWT with payload ``{sub, role, exp[, imp_by]}``."""
    if not user or not user.get("id"):
        raise ValueError("user required")
    header = _b64url_encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()
    )
    payload_obj: dict[str, Any] = {
        "sub": str(user["id"]),
        "role": str(user.get("role", "user")),
        "exp": int(time.time()) + int(ttl_seconds),
    }
    if impersonated_by:
        payload_obj["imp_by"] = str(impersonated_by)
    payload = _b64url_encode(
        json.dumps(payload_obj, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    sig = hmac.new(_jwt_secret(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url_encode(sig)}"


def issue_switch_account_token(
    session_user: dict[str, Any], target: str
) -> tuple[str, dict[str, Any]]:
    """Switch JWT between Live ↔ Demo. Preserves admin impersonation claim."""
    target = str(target or "").strip().lower()
    if target not in ("demo", "live"):
        raise ValueError("target must be 'demo' or 'live'")
    if not session_user or (is_admin(session_user) and not is_impersonating(session_user)):
        raise PermissionError("account switch not available for admin")

    imp_by = real_admin_id(session_user)
    if target == "demo":
        if not is_live_account(session_user):
            raise ValueError("already on demo account")
        dest = linked_demo_user(session_user)
        if not dest:
            raise KeyError("demo account not found")
    else:
        if not is_demo_account(session_user):
            raise ValueError("already on live account")
        dest = linked_parent_user(session_user)
        if not dest:
            raise KeyError("live account not found")
    if not dest.get("enabled", True):
        raise ValueError("linked account is disabled")
    token = issue_token(dest, impersonated_by=imp_by)
    return token, _public_user(dest)


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


def session_user_from_payload(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Resolve JWT payload to a user dict; attach ``_imp_by`` when impersonating."""
    user = get_user_by_id(str(payload.get("sub", "")))
    if not user or not user.get("enabled", True):
        return None
    out = dict(user)
    imp_by = payload.get("imp_by")
    if imp_by:
        admin = get_user_by_id(str(imp_by))
        if not admin or not is_admin(admin) or not admin.get("enabled", True):
            return None
        out["_imp_by"] = str(admin["id"])
    return out


def issue_impersonation_token(admin: dict[str, Any], target_user_id: str) -> tuple[str, dict[str, Any]]:
    """Admin-only: mint token as target user. Returns (token, public target)."""
    if not is_admin(admin) or is_impersonating(admin):
        raise PermissionError("admin required")
    target = get_user_by_id(str(target_user_id))
    if not target:
        raise KeyError(f"user not found: {target_user_id}")
    if is_admin(target):
        raise ValueError("cannot impersonate admin")
    if not target.get("enabled", True):
        raise ValueError("user is disabled")
    token = issue_token(target, impersonated_by=str(admin["id"]))
    return token, _public_user(target)


def stop_impersonation_token(session_user: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return to admin from an impersonation session."""
    admin_id = real_admin_id(session_user)
    if not admin_id:
        raise PermissionError("not impersonating")
    admin = get_user_by_id(admin_id)
    if not admin or not is_admin(admin) or not admin.get("enabled", True):
        raise PermissionError("admin session invalid")
    token = issue_token(admin)
    return token, _public_user(admin)


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


def tenant_api_creds(user_id: str) -> tuple[str, str, str]:
    """Creds the running tenant bot actually uses (from tenants/{id}/.env).

    Fall back to derive_api_creds when .env is missing. Do not rewrite .env here —
    a running ctbot keeps the password it was started with.
    """
    env_path = tenant_user_data(user_id) / ".env"
    user = pwd = jwt = ""
    if env_path.is_file():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key == "FREQUI_USERNAME":
                    user = val
                elif key == "FREQUI_PASSWORD":
                    pwd = val
                elif key == "FREQUI_JWT_SECRET":
                    jwt = val
        except OSError:
            pass
    if user and pwd:
        return user, pwd, jwt
    return derive_api_creds(user_id)


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
    """Write ``tenants/{id}/.env`` with exchange keys and FREQUI_* API creds.

    Storage still uses ``bybit_api_*`` blob fields for both Bybit and Binance;
    ``CT_EXCHANGE`` / ``BINANCE_*`` mirror the context exchange id.
    """
    td = tenant_user_data(user_id)
    td.mkdir(parents=True, exist_ok=True)
    api_user, api_pass, jwt_secret = derive_api_creds(user_id)

    api_key = ""
    api_secret = ""
    demo_trading = False
    exchange_id = context_exchange(user_id) if str(user_id) != "admin" else DEFAULT_EXCHANGE
    sec = load_user_secrets(user_id)
    if sec:
        api_key = str(sec.get("bybit_api_key") or sec.get("api_key") or "")
        api_secret = str(sec.get("bybit_api_secret") or sec.get("api_secret") or "")
        demo_trading = _as_bool(sec.get("bybit_demo_trading")) or is_demo_context_id(
            user_id
        )
        exchange_id = normalize_exchange(sec.get("exchange") or exchange_id)

    demo_val = "true" if demo_trading else "false"
    lines = [
        f"CT_EXCHANGE={exchange_id}",
        f"BYBIT_API_KEY={api_key if exchange_id == 'bybit' else ''}",
        f"BYBIT_API_SECRET={api_secret if exchange_id == 'bybit' else ''}",
        f"BYBIT_DEMO_TRADING={demo_val if exchange_id == 'bybit' else 'false'}",
        f"BINANCE_API_KEY={api_key if exchange_id == 'binance' else ''}",
        f"BINANCE_API_SECRET={api_secret if exchange_id == 'binance' else ''}",
        f"BINANCE_DEMO_TRADING={demo_val if exchange_id == 'binance' else 'false'}",
        f"FREQUI_USERNAME={api_user}",
        f"FREQUI_PASSWORD={api_pass}",
        f"FREQUI_JWT_SECRET={jwt_secret}",
        f"CTENGINE__EXCHANGE__KEY={api_key}",
        f"CTENGINE__EXCHANGE__SECRET={api_secret}",
        f"CTENGINE__EXCHANGE__DEMO_TRADING={demo_val}",
        f"CTENGINE__API_SERVER__USERNAME={api_user}",
        f"CTENGINE__API_SERVER__PASSWORD={api_pass}",
        f"CTENGINE__API_SERVER__JWT_SECRET_KEY={jwt_secret}",
        f"CT_ENABLED_STRATEGIES={td / 'enabled_strategies.json'}",
        f"CT_TEST_STRATEGY_SETTINGS={td / 'test_strategy_settings.json'}",
        f"CT_MAX_OPEN_TRADES_PER_STRATEGY={td / 'max_open_trades_per_strategy.json'}",
        f"CT_DUAL_HEDGE={td / 'dual_hedge.json'}",
    ]
    env_path = td / ".env"
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass
    return env_path


def _as_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    return str(val or "").strip().lower() in ("1", "true", "t", "yes", "y", "on")


def sync_tenant_demo_trading(user_id: str, demo_trading: bool) -> None:
    """Keep exchange.demo_trading in tenant bot configs in sync with secrets."""
    td = tenant_user_data(user_id)
    if not td.is_dir():
        return
    for name in ("config_signal_engine.json", "config_strategy.json", "config_grid.json", "config.json"):
        path = td / name
        if not path.is_file():
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        exchange = dict(cfg.get("exchange") or {})
        exchange["demo_trading"] = bool(demo_trading)
        cfg["exchange"] = exchange
        path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _mask_value(val: str, *, keep_start: int = 4, keep_end: int = 4) -> str:
    v = (val or "").strip()
    if not v:
        return "—"
    if len(v) <= keep_start + keep_end:
        return "•" * min(10, max(4, len(v)))
    start = v[:keep_start] if keep_start > 0 else ""
    end = v[-keep_end:] if keep_end > 0 else ""
    mid = "…" if start or end else "••••"
    return f"{start}{mid}{end}"


def _new_profile_id() -> str:
    return secrets.token_hex(8)



def _read_dotenv_file(path: Path) -> dict[str, str]:
    """Parse KEY=value pairs from an env file (no shell expansion)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in raw_text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'").replace("\r", "")
        if k:
            out[k] = v
    return out


def _admin_env_credentials() -> tuple[str, str, bool]:
    """BYBIT key/secret for admin: process env, then CT_ENV / .cryptotools.env file."""
    key = (os.environ.get("BYBIT_API_KEY") or os.environ.get("CTENGINE__EXCHANGE__KEY") or "").strip()
    secret = (
        os.environ.get("BYBIT_API_SECRET") or os.environ.get("CTENGINE__EXCHANGE__SECRET") or ""
    ).strip()
    demo = _as_bool(
        os.environ.get("BYBIT_DEMO_TRADING") or os.environ.get("CTENGINE__EXCHANGE__DEMO_TRADING")
    )
    if key and secret:
        return key, secret, bool(demo)
    file_vars = _read_dotenv_file(_admin_env_path())
    if not key:
        key = (file_vars.get("BYBIT_API_KEY") or file_vars.get("CTENGINE__EXCHANGE__KEY") or "").strip()
    if not secret:
        secret = (
            file_vars.get("BYBIT_API_SECRET") or file_vars.get("CTENGINE__EXCHANGE__SECRET") or ""
        ).strip()
    if not key or not secret:
        return "", "", False
    demo = _as_bool(
        file_vars.get("BYBIT_DEMO_TRADING") or file_vars.get("CTENGINE__EXCHANGE__DEMO_TRADING")
    )
    return key, secret, bool(demo)


def _empty_secrets_blob() -> dict[str, Any]:
    return {
        "bybit_api_key": "",
        "bybit_api_secret": "",
        "bybit_demo_trading": False,
        "active_profile_id": None,
        "profiles": [],
    }


def _admin_secrets_path() -> Path:
    return SECRETS_DIR / "admin.enc"


def _load_secrets_blob(owner_id: str) -> dict[str, Any]:
    owner_id = str(owner_id)
    path = _admin_secrets_path() if owner_id == "admin" else _secrets_path(owner_id)
    blob = _empty_secrets_blob()
    decrypt_failed = False
    if path.is_file():
        try:
            loaded = decrypt_secrets(path.read_bytes())
            if isinstance(loaded, dict):
                blob.update(loaded)
                # Never treat a previously persisted runtime flag as a live error.
                blob.pop("_decrypt_error", None)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # Do not silently treat encrypted files as empty — usually MASTER_KEY mismatch.
            log.error(
                "secrets decrypt failed owner=%s path=%s: %s",
                owner_id,
                path,
                exc,
            )
            blob["_decrypt_error"] = str(exc)
            decrypt_failed = True
    profiles = blob.get("profiles")
    if not isinstance(profiles, list):
        profiles = []
    blob["profiles"] = [p for p in profiles if isinstance(p, dict)]

    # Migrate legacy single-key blob / admin env into a named profile.
    key = str(blob.get("bybit_api_key") or "").strip()
    secret = str(blob.get("bybit_api_secret") or "").strip()
    if owner_id == "admin" and (not key or not secret):
        key, secret, demo = _admin_env_credentials()
        if key and secret:
            blob["bybit_api_key"] = key
            blob["bybit_api_secret"] = secret
            blob["bybit_demo_trading"] = demo
    if key and secret and not blob["profiles"]:
        pid = _new_profile_id()
        demo = _as_bool(blob.get("bybit_demo_trading"))
        blob["profiles"] = [
            {
                "id": pid,
                "name": "Demo Trading" if demo else "Live",
                "bybit_api_key": key,
                "bybit_api_secret": secret,
                "demo_trading": demo,
                "created_at": _utc_now_iso(),
            }
        ]
        blob["active_profile_id"] = pid
        blob["bybit_api_key"] = key
        blob["bybit_api_secret"] = secret
        blob["bybit_demo_trading"] = demo
        try:
            _write_secrets_blob(owner_id, blob)
        except Exception:
            # Env-file fallback must still work without SECRETS_MASTER_KEY in process.
            pass
    # Runtime-only marker: never leave it set after a successful decrypt path above.
    if decrypt_failed and "_decrypt_error" not in blob:
        pass
    return blob


def _write_secrets_blob(owner_id: str, blob: dict[str, Any]) -> None:
    owner_id = str(owner_id)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    path = _admin_secrets_path() if owner_id == "admin" else _secrets_path(owner_id)
    # `_decrypt_error` is process-local; persisting it poisons has_secrets forever.
    to_store = {k: v for k, v in blob.items() if k != "_decrypt_error"}
    path.write_bytes(encrypt_secrets(to_store))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _public_profiles(blob: dict[str, Any]) -> list[dict[str, Any]]:
    active_id = str(blob.get("active_profile_id") or "")
    out: list[dict[str, Any]] = []
    for p in blob.get("profiles") or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "")
        if not pid:
            continue
        key = str(p.get("bybit_api_key") or "")
        secret = str(p.get("bybit_api_secret") or "")
        demo = _as_bool(p.get("demo_trading"))
        out.append(
            {
                "id": pid,
                "name": str(p.get("name") or ("Demo" if demo else "Live")),
                "demo_trading": demo,
                "mode_label": "Demo" if demo else "Live",
                "api_key": _mask_value(key, keep_start=6, keep_end=4),
                "api_secret": _mask_value(secret, keep_start=0, keep_end=4),
                "active": pid == active_id,
                "created_at": str(p.get("created_at") or ""),
            }
        )
    return out


def _apply_active_credentials(owner_id: str, blob: dict[str, Any]) -> None:
    """Push active profile into env (admin) or tenant .env (user)."""
    owner_id = str(owner_id)
    active_id = str(blob.get("active_profile_id") or "")
    active = None
    for p in blob.get("profiles") or []:
        if isinstance(p, dict) and str(p.get("id") or "") == active_id:
            active = p
            break
    if not active:
        blob["bybit_api_key"] = ""
        blob["bybit_api_secret"] = ""
        blob["bybit_demo_trading"] = False
        blob["active_profile_id"] = None
        _write_secrets_blob(owner_id, blob)
        if owner_id == "admin":
            _apply_admin_env("", "", False)
        else:
            write_tenant_env(owner_id)
            sync_tenant_demo_trading(owner_id, False)
        return
    key = str(active.get("bybit_api_key") or "").strip()
    secret = str(active.get("bybit_api_secret") or "").strip()
    demo = _as_bool(active.get("demo_trading"))
    blob["bybit_api_key"] = key
    blob["bybit_api_secret"] = secret
    blob["bybit_demo_trading"] = demo
    _write_secrets_blob(owner_id, blob)
    if owner_id == "admin":
        _apply_admin_env(key, secret, demo)
    else:
        write_tenant_env(owner_id)
        sync_tenant_demo_trading(owner_id, demo)


def _apply_admin_env(key: str, secret: str, demo_trading: bool) -> None:
    demo_val = "true" if demo_trading else "false"
    updates = {
        "BYBIT_API_KEY": key,
        "BYBIT_API_SECRET": secret,
        "BYBIT_DEMO_TRADING": demo_val,
        "CTENGINE__EXCHANGE__KEY": key,
        "CTENGINE__EXCHANGE__SECRET": secret,
        "CTENGINE__EXCHANGE__DEMO_TRADING": demo_val,
    }
    env_path = _admin_env_path()
    _upsert_env_file(env_path, updates)
    for k, v in updates.items():
        os.environ[k] = v
    sync_admin_demo_trading(bool(demo_trading))


def save_user_secrets(
    user_id: str,
    key: str,
    secret: str,
    *,
    demo_trading: bool = False,
    name: str | None = None,
) -> dict[str, Any]:
    """Add/activate a named Bybit key profile for a tenant."""
    return upsert_key_profile(
        str(user_id),
        key,
        secret,
        demo_trading=demo_trading,
        name=name,
    )


def upsert_key_profile(
    owner_id: str,
    key: str,
    secret: str,
    *,
    demo_trading: bool = False,
    name: str | None = None,
    mirror_to_demo: bool = False,
) -> dict[str, Any]:
    """Add a named key profile (or replace same name) and make it active.

    ``owner_id`` may be a login user or a demo context ``{uid}__demo``.
    Mode is fixed by ``demo_trading`` (and by context id for non-admin).
    """
    key = (key or "").strip()
    secret = (secret or "").strip()
    if not key or not secret:
        raise ValueError("bybit api key and secret are required")
    owner_id = str(owner_id)
    # Context id forces venue; login users use explicit demo_trading flag.
    if is_demo_context_id(owner_id):
        demo_trading = True
    elif owner_id != "admin" and get_user_by_id(owner_id) and not is_admin(get_user_by_id(owner_id)):
        # Non-admin live context: never mark blob as demo unless saving into demo context.
        if not demo_trading:
            demo_trading = False
    blob = _load_secrets_blob(owner_id)
    demo = bool(demo_trading)
    label = (name or "").strip() or ("Demo Trading" if demo else "Live")
    profiles = list(blob.get("profiles") or [])
    existing = None
    for p in profiles:
        if isinstance(p, dict) and str(p.get("name") or "").strip().lower() == label.lower():
            existing = p
            break
    if existing is not None:
        existing["bybit_api_key"] = key
        existing["bybit_api_secret"] = secret
        existing["demo_trading"] = demo
        existing["name"] = label
        pid = str(existing.get("id") or _new_profile_id())
        existing["id"] = pid
    else:
        pid = _new_profile_id()
        profiles.append(
            {
                "id": pid,
                "name": label,
                "bybit_api_key": key,
                "bybit_api_secret": secret,
                "demo_trading": demo,
                "created_at": _utc_now_iso(),
            }
        )
    blob["profiles"] = profiles
    blob["active_profile_id"] = pid
    _apply_active_credentials(owner_id, blob)
    if owner_id != "admin":
        try:
            bots = restart_tenant_bots(owner_id)
        except Exception:
            bots = {}
    else:
        bots = {}
    return {
        "ok": True,
        "user_id": owner_id,
        "mode": "demo" if demo else "live",
        "demo_trading": demo,
        "active_profile_id": pid,
        "profiles": _public_profiles(blob),
        "bots_restarted": bots,
        "mirrored_to_demo": None,
    }


def delete_key_profile(owner_id: str, profile_id: str) -> dict[str, Any]:
    owner_id = str(owner_id)
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        raise ValueError("profile_id required")
    blob = _load_secrets_blob(owner_id)
    profiles = [
        p
        for p in (blob.get("profiles") or [])
        if isinstance(p, dict) and str(p.get("id") or "") != profile_id
    ]
    if len(profiles) == len(blob.get("profiles") or []):
        raise KeyError(f"profile not found: {profile_id}")
    blob["profiles"] = profiles
    if str(blob.get("active_profile_id") or "") == profile_id:
        blob["active_profile_id"] = str(profiles[0]["id"]) if profiles else None
    _apply_active_credentials(owner_id, blob)
    if owner_id != "admin":
        try:
            bots = restart_tenant_bots(owner_id)
        except Exception:
            bots = {}
    else:
        bots = {}
    return {
        "ok": True,
        "user_id": owner_id,
        "profiles": _public_profiles(blob),
        "active_profile_id": blob.get("active_profile_id"),
        "bots_restarted": bots,
    }


def activate_key_profile(owner_id: str, profile_id: str) -> dict[str, Any]:
    owner_id = str(owner_id)
    profile_id = str(profile_id or "").strip()
    blob = _load_secrets_blob(owner_id)
    found = None
    for p in blob.get("profiles") or []:
        if isinstance(p, dict) and str(p.get("id") or "") == profile_id:
            found = p
            break
    if not found:
        raise KeyError(f"profile not found: {profile_id}")
    # Venue fixed by trading context id.
    if is_demo_context_id(owner_id):
        found["demo_trading"] = True
    elif owner_id != "admin" and not is_demo_context_id(owner_id):
        found["demo_trading"] = False
    blob["active_profile_id"] = profile_id
    _apply_active_credentials(owner_id, blob)
    if owner_id != "admin":
        try:
            bots = restart_tenant_bots(owner_id)
        except Exception:
            bots = {}
    else:
        bots = {}
    return {
        "ok": True,
        "user_id": owner_id,
        "active_profile_id": profile_id,
        "profiles": _public_profiles(blob),
        "bots_restarted": bots,
        "demo_trading": bool(found.get("demo_trading")),
    }


def _enrich_trade_capability(status: dict[str, Any], key: str, secret: str, demo: bool) -> dict[str, Any]:
    """Probe Bybit key permissions; attach read_only / can_trade / trade_block_reason."""
    status.setdefault("read_only", None)
    status.setdefault("can_trade", None)
    status.setdefault("trade_block_reason", None)
    status.setdefault("api_key_note", None)
    if not key or not secret:
        status["can_trade"] = False
        status["trade_block_reason"] = "API-ключи не заданы"
        return status
    try:
        from exchange.user_exchange import UserBybitExchange

        ex = UserBybitExchange(key, secret, demo_trading=bool(demo))
        try:
            ex.sync_time()
        except Exception:
            pass
        info = ex.query_api_key()
        status["read_only"] = int(info.get("readOnly") or 0) == 1
        status["api_key_note"] = str(info.get("note") or "") or None
        block = ex.trade_block_reason()
        status["trade_block_reason"] = block
        status["can_trade"] = block is None
    except Exception as exc:
        status["can_trade"] = None
        status["trade_block_reason"] = f"не удалось проверить ключ: {exc}"
    return status


def secrets_status(user_id: str) -> dict[str, Any]:
    """Status of stored secrets for a tenant/context (no raw secret values)."""
    cid = str(user_id)
    blob = _load_secrets_blob(cid)
    key = str(blob.get("bybit_api_key") or "").strip()
    secret = str(blob.get("bybit_api_secret") or "").strip()
    has = bool(key and secret)
    demo = bool(_as_bool(blob.get("bybit_demo_trading"))) or is_demo_context_id(cid)
    enc_path = _secrets_path(cid)
    status = {
        "user_id": cid,
        "mode": context_mode(cid),
        "has_secrets": has,
        "has_bybit_api_key": bool(key),
        "has_bybit_api_secret": bool(secret),
        "demo_trading": demo,
        "active_profile_id": blob.get("active_profile_id"),
        "profiles": _public_profiles(blob),
        "secrets_file_present": enc_path.is_file(),
    }
    if blob.get("_decrypt_error"):
        status["decrypt_error"] = str(blob["_decrypt_error"])
        status["has_secrets"] = False
    return _enrich_trade_capability(status, key, secret, demo)


def has_mode_secrets(
    user_id: str,
    mode: str = "live",
    *,
    exchange: str = DEFAULT_EXCHANGE,
) -> bool:
    """True when the live/demo context for ``exchange`` has usable API keys."""
    uid = parent_user_id_of_context(str(user_id))
    m = normalize_account_mode(mode, default="live")
    ex = normalize_exchange(exchange)
    if uid == "admin" and m == "live" and ex == "bybit":
        return bool(admin_secrets_status().get("has_secrets"))
    return bool(
        secrets_status(trading_context_id(uid, m, exchange=ex)).get("has_secrets")
    )


def ensure_exchange_context(
    user_id: str,
    *,
    exchange: str = DEFAULT_EXCHANGE,
    mode: str = "live",
) -> str:
    """Provision tenant dir for exchange x mode if missing. Returns context id."""
    uid = parent_user_id_of_context(str(user_id))
    if not uid:
        raise ValueError("user_id required")
    ex = normalize_exchange(exchange)
    m = normalize_account_mode(mode, default="live")
    cid = exchange_context_id(uid, exchange=ex, mode=m)
    td = tenant_user_data(cid)
    created = False
    if not (td.is_dir() and (td / "config_strategy.json").is_file()):
        with _USERS_LOCK:
            users = load_users()
            ports = _allocate_ports(users)
        provision_tenant(cid, ports)
        created = True
    try:
        import control.user_prefs as up

        seed_from = live_context_id(uid, exchange="bybit")
        enabled = up.load_enabled_payload(cid)
        if created or not (enabled.get("enabled") or {}):
            up.put(
                cid,
                up.KEY_ENABLED,
                up.load_enabled_payload(seed_from),
                json_name="enabled_strategies.json",
            )
        limits = up.load_bot_limits_payload(cid)
        if created or not limits:
            up.save_bot_limits_payload(cid, up.load_bot_limits_payload(seed_from))
        up.save_placement(cid, up.load_placement(uid))
    except Exception as exc:
        log.debug("exchange context seed prefs %s: %s", cid, exc)
    return cid


def ensure_demo_context(user_id: str, *, exchange: str = DEFAULT_EXCHANGE) -> str:
    """Provision demo context for exchange (default Bybit)."""
    return ensure_exchange_context(user_id, exchange=exchange, mode="demo")


def save_mode_secrets(
    user_id: str,
    mode: str,
    key: str,
    secret: str,
    *,
    name: str | None = None,
    exchange: str = DEFAULT_EXCHANGE,
) -> dict[str, Any]:
    """Save Live/Demo API keys into the matching exchange trading context."""
    uid = parent_user_id_of_context(str(user_id))
    m = normalize_account_mode(mode, default="live")
    ex = normalize_exchange(exchange)
    if m not in ("live", "demo"):
        raise ValueError("mode must be live or demo")
    if uid == "admin" and m == "live" and ex == "bybit":
        result = save_admin_secrets(
            key, secret, demo_trading=False, name=name or "Live"
        )
        result["exchange"] = ex
        result["mode"] = m
        result["context_id"] = "admin"
        result["parent_user_id"] = "admin"
        return result
    cid = ensure_exchange_context(uid, exchange=ex, mode=m)
    label = name or (f"{ex.title()} Demo" if m == "demo" else f"{ex.title()} Live")
    result = upsert_key_profile(
        cid,
        key,
        secret,
        demo_trading=(m == "demo"),
        name=label,
        mirror_to_demo=False,
    )
    try:
        blob = _load_secrets_blob(cid)
        blob["exchange"] = ex
        blob["bybit_demo_trading"] = m == "demo"
        _write_secrets_blob(cid, blob)
        write_tenant_env(cid)
        sync_tenant_demo_trading(cid, m == "demo")
    except Exception:
        pass
    result["mode"] = m
    result["exchange"] = ex
    result["context_id"] = cid
    result["parent_user_id"] = uid
    return result


def dual_secrets_status(user_id: str) -> dict[str, Any]:
    """Live/Demo x Bybit/Binance secrets status for one login user."""
    uid = parent_user_id_of_context(str(user_id))
    exchanges: dict[str, Any] = {}
    for ex in SUPPORTED_EXCHANGES:
        if uid == "admin" and ex == "bybit":
            live = admin_secrets_status()
        else:
            live = secrets_status(live_context_id(uid, exchange=ex))
        demo = secrets_status(demo_context_id(uid, exchange=ex))
        exchanges[ex] = {
            "live": live,
            "demo": demo,
            "has_live_secrets": bool(live.get("has_secrets")),
            "has_demo_secrets": bool(demo.get("has_secrets")),
        }
    bybit = exchanges["bybit"]
    return {
        "user_id": uid,
        "live": bybit["live"],
        "demo": bybit["demo"],
        "has_live_secrets": bybit["has_live_secrets"],
        "has_demo_secrets": bybit["has_demo_secrets"],
        "exchanges": exchanges,
        "supported_exchanges": list(SUPPORTED_EXCHANGES),
        "default_exchange": DEFAULT_EXCHANGE,
    }


def admin_secrets_status() -> dict[str, Any]:
    """Admin Bybit secrets: encrypted profiles + env file / process env."""
    blob = _load_secrets_blob("admin")
    key = str(blob.get("bybit_api_key") or "").strip()
    secret = str(blob.get("bybit_api_secret") or "").strip()
    demo = _as_bool(blob.get("bybit_demo_trading"))
    if not key or not secret:
        key, secret, demo = _admin_env_credentials()
    status = {
        "user_id": "admin",
        "has_secrets": bool(key and secret),
        "has_bybit_api_key": bool(key),
        "has_bybit_api_secret": bool(secret),
        "demo_trading": demo,
        "active_profile_id": blob.get("active_profile_id"),
        "profiles": _public_profiles(blob),
        "source": "profiles",
    }
    return _enrich_trade_capability(status, key, secret, bool(demo))


def _admin_env_path() -> Path:
    raw = (os.environ.get("CT_ENV") or "").strip()
    if raw:
        return Path(raw)
    # Local default: site/.env ; prod often uses ~/.cryptotools.env via CT_ENV.
    candidate = BASE / ".env"
    if candidate.is_file():
        return candidate
    return Path.home() / ".cryptotools.env"


def _upsert_env_file(path: Path, updates: dict[str, str]) -> None:
    """Update or append KEY=value lines; preserve unrelated content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        raw = line.rstrip("\r")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(raw)
            continue
        key, _ = stripped.split("=", 1)
        key = key.strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(raw)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def pin_admin_grid_demo() -> dict[str, Any]:
    """Point the shared Grid bot at the admin Bybit demo keys.

    Writes ``user_data/grid.demo.env`` and sets ``demo_trading`` on
    ``config_grid.json`` only. Does not touch the shared live env or the
    strategy/finder configs.
    """
    cid = demo_context_id("admin")
    sec = load_user_secrets(cid) or {}
    key = str(sec.get("bybit_api_key") or sec.get("api_key") or "").strip()
    secret = str(sec.get("bybit_api_secret") or sec.get("api_secret") or "").strip()
    if not key or not secret:
        # Admin demo keys often live in the shared env when that account is Demo.
        env_key, env_secret, env_demo = _admin_env_credentials()
        if env_demo and env_key and env_secret:
            key, secret = env_key, env_secret
            cid = "admin"
    if not key or not secret:
        raise ValueError(
            "Нет Demo-ключей Bybit у администратора — добавьте их в Настройках"
        )
    if any(ch in key + secret for ch in "\r\n\x00"):
        raise ValueError("Demo-ключ содержит недопустимые символы")
    env_path = BASE / "user_data" / "grid.demo.env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(
        [
            f"CTENGINE__EXCHANGE__KEY={key}",
            f"CTENGINE__EXCHANGE__SECRET={secret}",
            "CTENGINE__EXCHANGE__DEMO_TRADING=true",
            f"BYBIT_API_KEY={key}",
            f"BYBIT_API_SECRET={secret}",
            "BYBIT_DEMO_TRADING=true",
        ]
    ) + "\n"
    previous = ""
    if env_path.is_file():
        try:
            previous = env_path.read_text(encoding="utf-8")
        except OSError:
            previous = ""
    if previous != text:
        env_path.write_text(text, encoding="utf-8")
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
    cfg_path = BASE / "user_data" / "config_grid.json"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = None
        if isinstance(cfg, dict):
            exchange = dict(cfg.get("exchange") or {})
            if exchange.get("demo_trading") is not True or str(exchange.get("name") or "") != "bybit":
                exchange["demo_trading"] = True
                exchange["name"] = "bybit"
                cfg["exchange"] = exchange
                cfg_path.write_text(
                    json.dumps(cfg, indent=4, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
    return {"context_id": cid, "demo_trading": True, "env_file": str(env_path)}


def sync_admin_demo_trading(demo_trading: bool) -> None:
    """Keep exchange.demo_trading in admin bot configs in sync with env flag."""
    ud = BASE / "user_data"
    for name in ("config_strategy.json", "config_grid.json", "config.json"):
        path = ud / name
        if not path.is_file():
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        exchange = dict(cfg.get("exchange") or {})
        exchange["demo_trading"] = bool(demo_trading)
        cfg["exchange"] = exchange
        path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def save_admin_secrets(
    key: str,
    secret: str,
    *,
    demo_trading: bool = False,
    name: str | None = None,
) -> dict[str, Any]:
    """Add/activate admin Bybit key profile and sync active creds to .env."""
    result = upsert_key_profile(
        "admin",
        key,
        secret,
        demo_trading=demo_trading,
        name=name,
    )
    result["env_file"] = str(_admin_env_path())
    result["source"] = "profiles"
    return result


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
            "demo_trading": False,
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
    sec = load_user_secrets(user_id)
    exchange["demo_trading"] = bool(sec and _as_bool(sec.get("bybit_demo_trading")))
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
        "max_open_trades_per_strategy": 0,
    }
    _atomic_write_json(td / "bot_limits.json", bot_limits, mode=0o644)
    _atomic_write_json(td / "max_open_trades_per_strategy.json", {"value": 0}, mode=0o644)
    _atomic_write_json(
        td / "test_strategy_settings.json",
        {
            "max_open_trades": 8,
            "max_open_trades_per_strategy": 2,
            "stake_amount": 5.0,
            "stoploss": -0.02,
            "take_profit": 0.02,
        },
        mode=0o644,
    )
    # Do NOT copy admin enabled/placement — seed isolated defaults (all OFF, all in test).
    _atomic_write_json(
        td / "enabled_strategies.json",
        {"enabled": {}, "inverted": {}, "trained_risk": {}, "ml_confidence": {}},
        mode=0o644,
    )
    _atomic_write_json(
        td / "strategy_ui_placement.json",
        {
            "main": [],
            "hidden": [],
            "_note": "main = Strategies; else test; hidden = neither",
            "_hidden_disabled": True,
        },
        mode=0o644,
    )
    try:
        import control.user_prefs as up

        up.seed_new_user_prefs(user_id)
        up.save_bot_limits_payload(user_id, bot_limits)
    except Exception:
        pass

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
    # Empty strategy trades DB so shared executor can reconcile immediately.
    try:
        import trading.trade_executor as te

        te.ensure_trades_db(user_id)
    except Exception:
        pass
    # New tenants start with all bots off (control-plane / trading_enabled).
    try:
        import control.user_trading as ut

        ut.save_trading_flags(
            user_id, {"strategy": False, "grid": False, "finder": False}
        )
    except Exception:
        _atomic_write_json(
            td / "trading_enabled.json",
            {"strategy": False, "grid": False, "finder": False},
            mode=0o600,
        )
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
    timeout: float = 60,
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
        api_user, api_pass, _jwt = tenant_api_creds(str(user["id"]))

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
        with urlopen(req, timeout=float(timeout)) as resp:
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
    except Exception as exc:  # noqa: BLE001 — socket.timeout on some Pythons
        name = type(exc).__name__.lower()
        if "timeout" in name or "timed out" in str(exc).lower():
            err = json.dumps({"error": "upstream_timeout"}).encode()
            return 504, {"content-type": "application/json"}, err
        err = json.dumps({"error": "upstream_error", "detail": str(exc)}).encode()
        return 502, {"content-type": "application/json"}, err


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
    "ensure_demo_pair_for_user",
    "backfill_demo_pairs",
    "account_kind",
    "is_demo_account",
    "is_live_account",
    "account_switch_info",
    "issue_switch_account_token",
    "telegram_owner_id",
    "LIVE_DEFAULT_ALLOWED_BLOCKS",
    "DEMO_DEFAULT_ALLOWED_BLOCKS",
    "set_user_enabled",
    "set_user_password",
    "list_users_public",
    "issue_token",
    "verify_token",
    "provision_tenant",
    "derive_api_creds",
    "write_tenant_env",
    "save_user_secrets",
    "upsert_key_profile",
    "delete_key_profile",
    "activate_key_profile",
    "secrets_status",
    "admin_secrets_status",
    "save_admin_secrets",
    "bot_ports_for_user",
    "start_tenant_bots",
    "stop_tenant_bots",
    "restart_tenant_bots",
    "tenant_bots_status",
    "proxy_bot_request",
    "tenant_user_data",
    "is_admin",
]
