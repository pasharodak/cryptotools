"""Request-scoped tenant path resolution for pair_config_server (contextvars)."""

from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Any, Optional

import tenant_manager as tm

_req_user: contextvars.ContextVar[Optional[dict[str, Any]]] = contextvars.ContextVar(
    "pair_config_req_user", default=None
)


def set_request_user(user: dict[str, Any] | None) -> contextvars.Token:
    return _req_user.set(user)


def reset_request_user(token: contextvars.Token) -> None:
    _req_user.reset(token)


def current_user() -> dict[str, Any] | None:
    return _req_user.get()


def is_request_admin() -> bool:
    u = _req_user.get()
    return bool(u and tm.is_admin(u))


def user_data_dir(base: Path) -> Path:
    u = _req_user.get()
    if u and not tm.is_admin(u):
        return tm.tenant_user_data(u["id"])
    return base / "user_data"


def resolve_configs(base: Path, admin_configs: dict[str, Path]) -> dict[str, Path]:
    u = _req_user.get()
    if u and not tm.is_admin(u):
        td = tm.tenant_user_data(u["id"])
        return {
            "finder": td / "config.json",
            "strategy": td / "config_strategy.json",
            "grid": td / "config_grid.json",
        }
    return admin_configs


def resolve_bot_ports(user: dict[str, Any] | None = None) -> dict[str, int]:
    u = user if user is not None else _req_user.get()
    if not u or tm.is_admin(u):
        return dict(tm.ADMIN_BOT_PORTS)
    return tm.bot_ports_for_user(u)


def resolve_api_urls(kind: str) -> dict[str, str]:
    """kind: reload_config | blacklist | whitelist | start | stop"""
    ports = resolve_bot_ports()
    path = {
        "reload_config": "reload_config",
        "blacklist": "blacklist",
        "whitelist": "whitelist",
        "start": "start",
        "stop": "stop",
    }[kind]
    return {
        bot: f"http://127.0.0.1:{port}/api/v1/{path}"
        for bot, port in ports.items()
    }


def bybit_context_paths(base: Path) -> tuple[Path | None, Path | None]:
    """Return (user_data, env_file) for tenant_bybit_context; (None, None) for admin."""
    u = _req_user.get()
    if not u or tm.is_admin(u):
        return None, None
    td = tm.tenant_user_data(u["id"])
    return td, td / ".env"
