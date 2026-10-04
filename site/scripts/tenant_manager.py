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


# UI / bot blocks that admin can grant to a user. Missing allowlist = all.
UI_BLOCKS: list[dict[str, str]] = [
    {"id": "test_strategies", "name": "Тестовые стратегии"},
    {"id": "strategy", "name": "Стратегии"},
    {"id": "grid", "name": "Grid"},
    {"id": "bybitgrid", "name": "Bybit Grid"},
    {"id": "finder", "name": "ML Finder"},
    {"id": "history", "name": "История сделок"},
    {"id": "dashboard", "name": "Дашборд PnL"},
    {"id": "rating", "name": "Рейтинг"},
]
UI_BLOCK_IDS: frozenset[str] = frozenset(b["id"] for b in UI_BLOCKS)


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
    allow = user_allowed_blocks(user)
    if allow is None:
        return True
    return str(block_id) in allow


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
    return out


def list_users_public() -> list[dict[str, Any]]:
    """All users without password_hash."""
    ensure_users_migrated()
    return [_public_user(u) for u in load_users()]


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
    return {"blocks": list(UI_BLOCKS), "strategies": strategies}


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
            # null / omitted = все стратегии и блоки; admin сужает через PATCH
            "allowed_strategies": None,
            "allowed_blocks": None,
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
    """Write ``tenants/{id}/.env`` with Bybit keys (if any) and FREQUI_* API creds.

    CTENGINE__* mappings are optional here — ``load_env.sh`` maps FREQUI_/BYBIT_
    into CTENGINE__ when sourcing the env file.
    """
    td = tenant_user_data(user_id)
    td.mkdir(parents=True, exist_ok=True)
    api_user, api_pass, jwt_secret = derive_api_creds(user_id)

    bybit_key = ""
    bybit_secret = ""
    demo_trading = False
    sec = load_user_secrets(user_id)
    if sec:
        bybit_key = str(sec.get("bybit_api_key") or "")
        bybit_secret = str(sec.get("bybit_api_secret") or "")
        demo_trading = _as_bool(sec.get("bybit_demo_trading"))

    demo_val = "true" if demo_trading else "false"
    lines = [
        f"BYBIT_API_KEY={bybit_key}",
        f"BYBIT_API_SECRET={bybit_secret}",
        f"BYBIT_DEMO_TRADING={demo_val}",
        f"FREQUI_USERNAME={api_user}",
        f"FREQUI_PASSWORD={api_pass}",
        f"FREQUI_JWT_SECRET={jwt_secret}",
        # Optional direct mappings (load_env.sh also sets these from FREQUI_/BYBIT_):
        f"CTENGINE__EXCHANGE__KEY={bybit_key}",
        f"CTENGINE__EXCHANGE__SECRET={bybit_secret}",
        f"CTENGINE__EXCHANGE__DEMO_TRADING={demo_val}",
        f"CTENGINE__API_SERVER__USERNAME={api_user}",
        f"CTENGINE__API_SERVER__PASSWORD={api_pass}",
        f"CTENGINE__API_SERVER__JWT_SECRET_KEY={jwt_secret}",
        f"CT_ENABLED_STRATEGIES={td / 'enabled_strategies.json'}",
        f"CT_TEST_STRATEGY_SETTINGS={td / 'test_strategy_settings.json'}",
        f"CT_MAX_OPEN_TRADES_PER_STRATEGY={td / 'max_open_trades_per_strategy.json'}",
        f"CT_DUAL_HEDGE={td / 'dual_hedge.json'}",
        f"CT_STRATEGY_UI_PLACEMENT={td / 'strategy_ui_placement.json'}",
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
    if path.is_file():
        try:
            loaded = decrypt_secrets(path.read_bytes())
            if isinstance(loaded, dict):
                blob.update(loaded)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    profiles = blob.get("profiles")
    if not isinstance(profiles, list):
        profiles = []
    blob["profiles"] = [p for p in profiles if isinstance(p, dict)]

    # Migrate legacy single-key blob / admin env into a named profile.
    key = str(blob.get("bybit_api_key") or "").strip()
    secret = str(blob.get("bybit_api_secret") or "").strip()
    if owner_id == "admin" and (not key or not secret):
        key = (os.environ.get("BYBIT_API_KEY") or "").strip()
        secret = (os.environ.get("BYBIT_API_SECRET") or "").strip()
        if key and secret:
            blob["bybit_api_key"] = key
            blob["bybit_api_secret"] = secret
            blob["bybit_demo_trading"] = _as_bool(
                os.environ.get("BYBIT_DEMO_TRADING")
                or os.environ.get("CTENGINE__EXCHANGE__DEMO_TRADING")
            )
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
        _write_secrets_blob(owner_id, blob)
    return blob


def _write_secrets_blob(owner_id: str, blob: dict[str, Any]) -> None:
    owner_id = str(owner_id)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    path = _admin_secrets_path() if owner_id == "admin" else _secrets_path(owner_id)
    path.write_bytes(encrypt_secrets(blob))
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
) -> dict[str, Any]:
    """Add a named key profile (or replace same name) and make it active."""
    key = (key or "").strip()
    secret = (secret or "").strip()
    if not key or not secret:
        raise ValueError("bybit api key and secret are required")
    owner_id = str(owner_id)
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
        "demo_trading": demo,
        "active_profile_id": pid,
        "profiles": _public_profiles(blob),
        "bots_restarted": bots,
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
    found = False
    for p in blob.get("profiles") or []:
        if isinstance(p, dict) and str(p.get("id") or "") == profile_id:
            found = True
            break
    if not found:
        raise KeyError(f"profile not found: {profile_id}")
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
        from user_exchange import UserBybitExchange

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
    """Status of stored secrets for a tenant (no raw secret values)."""
    blob = _load_secrets_blob(str(user_id))
    key = str(blob.get("bybit_api_key") or "").strip()
    secret = str(blob.get("bybit_api_secret") or "").strip()
    has = bool(key and secret)
    demo = bool(_as_bool(blob.get("bybit_demo_trading")))
    status = {
        "user_id": str(user_id),
        "has_secrets": has,
        "has_bybit_api_key": bool(key),
        "has_bybit_api_secret": bool(secret),
        "demo_trading": demo,
        "active_profile_id": blob.get("active_profile_id"),
        "profiles": _public_profiles(blob),
    }
    return _enrich_trade_capability(status, key, secret, demo)


def admin_secrets_status() -> dict[str, Any]:
    """Admin Bybit secrets: encrypted profiles + active sync to process env."""
    blob = _load_secrets_blob("admin")
    key = str(blob.get("bybit_api_key") or "").strip() or (
        os.environ.get("BYBIT_API_KEY") or ""
    ).strip()
    secret = str(blob.get("bybit_api_secret") or "").strip() or (
        os.environ.get("BYBIT_API_SECRET") or ""
    ).strip()
    demo = _as_bool(blob.get("bybit_demo_trading")) if key else _as_bool(
        os.environ.get("BYBIT_DEMO_TRADING")
        or os.environ.get("CTENGINE__EXCHANGE__DEMO_TRADING")
    )
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
    test_settings_src = global_ud / "test_strategy_settings.json"
    if test_settings_src.is_file():
        try:
            shutil.copy2(test_settings_src, td / "test_strategy_settings.json")
        except OSError:
            _atomic_write_json(
                td / "test_strategy_settings.json",
                {
                    "max_open_trades": 3,
                    "max_open_trades_per_strategy": 2,
                    "stake_amount": 5.0,
                    "stoploss": -0.02,
                    "take_profit": 0.02,
                },
                mode=0o644,
            )
    else:
        _atomic_write_json(
            td / "test_strategy_settings.json",
            {
                "max_open_trades": 3,
                "max_open_trades_per_strategy": 2,
                "stake_amount": 5.0,
                "stoploss": -0.02,
                "take_profit": 0.02,
            },
            mode=0o644,
        )

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
