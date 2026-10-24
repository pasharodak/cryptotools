"""Thin wrapper: cross-bot first-wins claim for strategy confirm_trade_entry."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _ensure_scripts_path() -> None:
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    s = str(scripts)
    if s in sys.path:
        sys.path.remove(s)
    # Keep behind user_data so site/user_data/ml stays preferred over other ml pkgs.
    ud = str(Path(__file__).resolve().parents[1])
    if ud in sys.path:
        idx = sys.path.index(ud)
        sys.path.insert(idx + 1, s)
    else:
        sys.path.append(s)


def claim_entry(bot: str, pair: str, side: str) -> bool:
    """Claim pair for bot. Fail-closed: import/errors deny entry."""
    try:
        _ensure_scripts_path()
        from pair_entry_guard import allow_entry  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        logger.warning("pair_entry_guard unavailable (%s) — DENY entry", exc)
        return False
    try:
        return bool(allow_entry(bot, pair, side, tenant_id="admin", check_bybit=True))
    except Exception as exc:  # noqa: BLE001
        logger.warning("pair_entry_guard allow_entry failed (%s) — DENY entry", exc)
        return False


def pair_open_elsewhere(bot: str, pair: str) -> bool:
    """True when another bot already has this pair open. Search should skip it until that trade closes."""
    if not pair:
        return False
    try:
        _ensure_scripts_path()
        from pair_entry_guard import pairs_blocked_for  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        logger.warning("pair open-elsewhere check unavailable: %s", exc)
        return False
    try:
        return pair in pairs_blocked_for(bot)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pair open-elsewhere failed: %s", exc)
        return False


def pair_busy(pair: str, side: str, *, bot: str | None = None) -> str | None:
    """Return busy reason, or a deny string if guard is unavailable (fail-closed)."""
    try:
        _ensure_scripts_path()
        from pair_entry_guard import busy_reason  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        logger.warning("pair_entry_guard busy check unavailable: %s — DENY", exc)
        return f"pair_entry_guard недоступен: {exc}"
    try:
        return busy_reason(pair, side, bot=bot, tenant_id="admin", check_bybit=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pair_entry_guard busy_reason failed: %s — DENY", exc)
        return f"ошибка pair_entry_guard: {exc}"
