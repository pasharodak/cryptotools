"""Trading context ids: live/demo × bybit/binance.

Id strings stored in control_plane stay the same. Secrets provisioning stays
in tenant_manager; this module only parses and builds the ids.
"""

from __future__ import annotations
import sys
from pathlib import Path as _BootstrapPath

_scripts_root = _BootstrapPath(__file__).resolve().parents[1]
if str(_scripts_root) not in sys.path:
    sys.path.insert(0, str(_scripts_root))

from dataclasses import dataclass
from typing import Any, Optional

DEMO_CONTEXT_SUFFIX = "__demo"
BINANCE_CONTEXT_SUFFIX = "__binance"
SUPPORTED_EXCHANGES = ("bybit", "binance")
DEFAULT_EXCHANGE = "bybit"
DEFAULT_TRADE_ON = "bybit"


def is_demo_context_id(context_id: Optional[str]) -> bool:
    """True for internal demo trading context ids (``…__demo``)."""
    cid = str(context_id or "")
    return bool(cid.endswith(DEMO_CONTEXT_SUFFIX) and len(cid) > len(DEMO_CONTEXT_SUFFIX))


def normalize_exchange(exchange: Any, *, default: str = DEFAULT_EXCHANGE) -> str:
    raw = str(exchange or "").strip().lower()
    if raw in SUPPORTED_EXCHANGES:
        return raw
    return default


def normalize_trade_on(value: Any, *, default: str = DEFAULT_TRADE_ON) -> str:
    raw = str(value or "").strip().lower()
    if raw in ("all", "bybit", "binance"):
        return raw
    return default


def normalize_account_mode(mode: Any, *, default: str = "live") -> str:
    raw = str(mode or "").strip().lower()
    if raw in ("live", "demo", "all"):
        return raw
    return default


def parent_user_id_of_context(context_id: str) -> str:
    """Login user id for a trading context (strips ``__demo`` / ``__binance``)."""
    cid = str(context_id or "").strip()
    if is_demo_context_id(cid):
        cid = cid[: -len(DEMO_CONTEXT_SUFFIX)]
    if cid.endswith(BINANCE_CONTEXT_SUFFIX):
        cid = cid[: -len(BINANCE_CONTEXT_SUFFIX)]
    return cid


def context_exchange(context_id: str) -> str:
    """``bybit`` | ``binance`` for a trading context id."""
    cid = str(context_id or "")
    if is_demo_context_id(cid):
        cid = cid[: -len(DEMO_CONTEXT_SUFFIX)]
    if cid.endswith(BINANCE_CONTEXT_SUFFIX):
        return "binance"
    return "bybit"


def context_mode(context_id: str) -> str:
    """``live`` | ``demo`` for a trading context id."""
    return "demo" if is_demo_context_id(context_id) else "live"


def exchange_context_id(
    user_id: str,
    *,
    exchange: Any = DEFAULT_EXCHANGE,
    mode: Any = "live",
) -> str:
    """Trading context: bybit live=``{uid}``, demo=``{uid}__demo``;
    binance live=``{uid}__binance``, demo=``{uid}__binance__demo``.
    """
    uid = parent_user_id_of_context(str(user_id or "").strip())
    if not uid:
        raise ValueError("user_id required")
    ex = normalize_exchange(exchange)
    m = normalize_account_mode(mode, default="live")
    if ex == "binance":
        base = f"{uid}{BINANCE_CONTEXT_SUFFIX}"
    else:
        base = uid
    if m == "demo":
        return f"{base}{DEMO_CONTEXT_SUFFIX}"
    return base


def demo_context_id(user_id: str, *, exchange: Any = DEFAULT_EXCHANGE) -> str:
    """Internal demo trading context (not a loginable account)."""
    return exchange_context_id(user_id, exchange=exchange, mode="demo")


def live_context_id(user_id: str, *, exchange: Any = DEFAULT_EXCHANGE) -> str:
    return exchange_context_id(user_id, exchange=exchange, mode="live")


def trading_context_id(
    user_id: str,
    mode: Any = "live",
    *,
    exchange: Any = DEFAULT_EXCHANGE,
) -> str:
    """Resolve live/demo trading context id for a login user (+ exchange)."""
    return exchange_context_id(user_id, exchange=exchange, mode=mode)


def flag_context_id(context_id: str) -> str:
    """Bybit live/demo context that owns shared trading flags for a mode."""
    uid = parent_user_id_of_context(context_id)
    return exchange_context_id(uid, exchange="bybit", mode=context_mode(context_id))


def iter_user_contexts(
    user_id: str,
    *,
    modes: Optional[list[str]] = None,
    exchanges: Optional[list[str]] = None,
) -> list[str]:
    """Return trading context ids for a login user (exchanges × modes)."""
    uid = parent_user_id_of_context(str(user_id or "").strip())
    want_modes = modes or ["live", "demo"]
    want_ex = exchanges or list(SUPPORTED_EXCHANGES)
    out: list[str] = []
    for ex in want_ex:
        nex = normalize_exchange(ex, default="")
        if nex not in SUPPORTED_EXCHANGES:
            continue
        for m in want_modes:
            nm = normalize_account_mode(m, default="")
            if nm not in ("live", "demo"):
                continue
            out.append(exchange_context_id(uid, exchange=nex, mode=nm))
    seen: set[str] = set()
    uniq: list[str] = []
    for cid in out:
        if cid and cid not in seen:
            seen.add(cid)
            uniq.append(cid)
    return uniq


@dataclass(frozen=True)
class TradingContext:
    """Parsed trading context. ``context_id`` is the stored string."""

    context_id: str

    @classmethod
    def parse(cls, context_id: str) -> TradingContext:
        return cls(str(context_id or "").strip())

    @property
    def parent_login_id(self) -> str:
        return parent_user_id_of_context(self.context_id)

    @property
    def exchange(self) -> str:
        return context_exchange(self.context_id)

    @property
    def mode(self) -> str:
        return context_mode(self.context_id)

    @property
    def secrets_scope(self) -> str:
        return self.context_id
