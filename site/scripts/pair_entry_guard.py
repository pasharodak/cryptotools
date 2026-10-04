#!/usr/bin/env python3
"""Cross-bot pair entry lock (first-wins) for Strategy / Grid / Finder / executor."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# site/scripts/pair_entry_guard.py -> parents[1] == site/
_SITE_ROOT = Path(__file__).resolve().parent.parent
CLAIM_TTL_SEC = float(os.environ.get("CT_PAIR_CLAIM_TTL_SEC", "60"))
BYBIT_CACHE_TTL_SEC = float(os.environ.get("CT_PAIR_BYBIT_CACHE_SEC", "8"))

# bot -> config relative to BASE (admin / shared stack)
_BOT_CONFIGS: dict[str, str] = {
    "finder": "user_data/config.json",
    "strategy": "user_data/config_strategy.json",
    "grid": "user_data/config_grid.json",
}

_bybit_cache: tuple[float, set[tuple[str, str]]] | None = None


def get_base() -> Path:
    """Resolve app root even when CT_BASE is missing in bot processes."""
    env = (os.environ.get("CT_BASE") or "").strip()
    if env:
        p = Path(env)
        if (p / "user_data").is_dir():
            return p
    if (_SITE_ROOT / "user_data").is_dir():
        return _SITE_ROOT
    return Path(env) if env else _SITE_ROOT


# Back-compat alias (some callers read BASE); keep it fresh via property-like usage.
BASE = get_base()
DEFAULT_ENV = Path(os.environ.get("CT_ENV", str(get_base() / ".env")))


def _normalize_side(side: str | int | bool) -> str:
    if isinstance(side, bool):
        return "sell" if side else "buy"
    if isinstance(side, int):
        return "sell" if side else "buy"
    s = str(side or "long").strip().lower()
    return "sell" if s in ("sell", "short", "1", "true") else "buy"


def lock_key(pair: str, side: str | int | bool) -> str:
    return f"{pair}:{_normalize_side(side)}"


def user_data_dir(tenant_id: str | None = None) -> Path:
    tid = (tenant_id or "admin").strip() or "admin"
    base = get_base()
    if tid == "admin":
        return base / "user_data"
    return base / "user_data" / "tenants" / tid


def _claims_db(tenant_id: str | None = None) -> Path:
    ud = user_data_dir(tenant_id)
    ud.mkdir(parents=True, exist_ok=True)
    return ud / "pair_entry_claims.sqlite"


def _connect(tenant_id: str | None = None) -> sqlite3.Connection:
    path = _claims_db(tenant_id)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS claims (
            key TEXT PRIMARY KEY,
            bot TEXT NOT NULL,
            pair TEXT NOT NULL,
            side TEXT NOT NULL,
            ts REAL NOT NULL
        )
        """
    )
    return conn


def _purge_expired(conn: sqlite3.Connection, now: float | None = None) -> None:
    now = time.time() if now is None else now
    conn.execute("DELETE FROM claims WHERE ts < ?", (now - CLAIM_TTL_SEC,))


def _db_path_for_config(base: Path, config_rel: str) -> Path | None:
    cfg_path = base / config_rel
    if not cfg_path.is_file():
        return None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = str(cfg.get("db_url") or "sqlite:///tradesv3.sqlite")
    name = Path(raw.split("///")[-1]).name
    candidates = [
        base / "user_data" / name,
        base / name,
    ]
    # Prefer the non-empty DB that actually has a trades table.
    best: Path | None = None
    best_size = -1
    for cand in candidates:
        if not cand.is_file():
            continue
        try:
            size = cand.stat().st_size
        except OSError:
            continue
        if size <= 0:
            continue
        if size > best_size:
            best = cand
            best_size = size
    return best


def _scan_db(db: Path, *, bot: str, pair: str, want_short: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            conn = sqlite3.connect(str(db), timeout=15)
            conn.row_factory = sqlite3.Row
            # Accept 0/1 and boolean-ish storage.
            for row in conn.execute(
                """
                SELECT id, pair, is_short, amount, open_date, strategy, enter_tag
                FROM trades
                WHERE is_open = 1 AND pair = ?
                  AND (
                    is_short = ?
                    OR is_short = ?
                    OR lower(cast(is_short as text)) IN (?, ?)
                  )
                ORDER BY id
                """,
                (
                    pair,
                    want_short,
                    bool(want_short),
                    "true" if want_short else "false",
                    "1" if want_short else "0",
                ),
            ):
                out.append(
                    {
                        "bot": bot,
                        "trade_id": int(row["id"]),
                        "pair": row["pair"],
                        "side": "sell" if row["is_short"] else "buy",
                        "amount": float(row["amount"] or 0),
                        "open_date": row["open_date"],
                        "strategy": row["strategy"],
                        "enter_tag": row["enter_tag"],
                    }
                )
            conn.close()
            return out
        except sqlite3.OperationalError as exc:
            last_exc = exc
            time.sleep(0.05 * (attempt + 1))
        except sqlite3.Error as exc:
            logger.warning("pair_entry_guard DB scan %s: %s", db, exc)
            raise
    assert last_exc is not None
    logger.warning("pair_entry_guard DB locked %s: %s", db, last_exc)
    raise last_exc


def open_trades_holding_pair(
    pair: str,
    side: str | int | bool,
    *,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Open trades in finder/strategy/grid DBs for this pair+side."""
    side_key = _normalize_side(side)
    want_short = 1 if side_key == "sell" else 0
    hits: list[dict[str, Any]] = []
    tid = (tenant_id or "admin").strip() or "admin"
    if tid != "admin":
        ud = user_data_dir(tid)
        for name in (
            "tradesv3-strategy.sqlite",
            "tradesv3.sqlite",
            "tradesv3-finder.sqlite",
            "tradesv3-grid.sqlite",
        ):
            db = ud / name
            if not db.is_file() or db.stat().st_size <= 0:
                continue
            hits.extend(
                _scan_db(
                    db,
                    bot=name.replace("tradesv3-", "").replace(".sqlite", "") or "strategy",
                    pair=pair,
                    want_short=want_short,
                )
            )
        return hits

    base = get_base()
    for bot, cfg_rel in _BOT_CONFIGS.items():
        db = _db_path_for_config(base, cfg_rel)
        if db is None:
            continue
        hits.extend(_scan_db(db, bot=bot, pair=pair, want_short=want_short))
    return hits


def bot_dbs_available(tenant_id: str | None = None) -> int:
    """How many bot trade DBs we can see (0 = guard is blind — deny entries)."""
    tid = (tenant_id or "admin").strip() or "admin"
    n = 0
    if tid != "admin":
        ud = user_data_dir(tid)
        for name in (
            "tradesv3-strategy.sqlite",
            "tradesv3.sqlite",
            "tradesv3-finder.sqlite",
            "tradesv3-grid.sqlite",
        ):
            db = ud / name
            if db.is_file() and db.stat().st_size > 0:
                n += 1
        return n
    base = get_base()
    for cfg_rel in _BOT_CONFIGS.values():
        if _db_path_for_config(base, cfg_rel) is not None:
            n += 1
    return n


def _load_env() -> dict[str, str]:
    env_path = Path(os.environ.get("CT_ENV") or (get_base() / ".env"))
    out: dict[str, str] = {}
    if not env_path.is_file():
        return out
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    except OSError:
        return out
    return out


def _ft_pair_to_bybit(pair: str) -> str:
    return pair.split(":")[0].replace("/", "")


def bybit_holds_pair(
    pair: str,
    side: str | int | bool,
    *,
    any_side: bool = False,
    use_cache: bool = True,
) -> bool | None:
    """
    Cached Bybit position check.
    Returns True/False, or None if the check could not be completed (do not treat as free).
    Empty snapshots are never cached — a flaky empty response caused Finder to steal Strategy positions.
    """
    global _bybit_cache
    side_key = _normalize_side(side)
    sym = _ft_pair_to_bybit(pair)
    now = time.time()
    if (
        use_cache
        and _bybit_cache is not None
        and (now - _bybit_cache[0]) < BYBIT_CACHE_TTL_SEC
        and _bybit_cache[1]  # never reuse an empty snapshot
    ):
        held_set = _bybit_cache[1]
        if any_side:
            return any(s == sym for s, _ in held_set)
        return (sym, side_key) in held_set
    try:
        from reconcile_positions import bybit_positions, bybit_side_key  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        logger.warning("pair_entry_guard bybit import: %s", exc)
        return None
    env = _load_env()
    # Detect demo from bot config when env flag missing.
    demo = str(
        env.get("BYBIT_DEMO_TRADING") or env.get("CTENGINE__EXCHANGE__DEMO_TRADING") or ""
    ).strip().lower() in ("1", "true", "t", "yes", "y", "on")
    if not demo:
        base = get_base()
        for cfg_rel in _BOT_CONFIGS.values():
            try:
                cfg = json.loads((base / cfg_rel).read_text(encoding="utf-8"))
                if bool((cfg.get("exchange") or {}).get("demo_trading")):
                    env = {**env, "BYBIT_DEMO_TRADING": "true"}
                    break
            except Exception:  # noqa: BLE001
                continue
    key = env.get("BYBIT_API_KEY", "")
    secret = env.get("BYBIT_API_SECRET", "")
    if not key or not secret:
        logger.warning("pair_entry_guard bybit: no API keys")
        return None
    try:
        positions = bybit_positions(key, secret, env=env)
        held = {
            (str(p.get("symbol") or ""), bybit_side_key(str(p.get("side") or "")))
            for p in positions
            if float(p.get("size") or 0) > 0
        }
        if held:
            _bybit_cache = (now, held)
        else:
            # Do not remember "flat account" — demo/API blips looked like free slots.
            _bybit_cache = None
        if any_side:
            return any(s == sym for s, _ in held)
        return (sym, side_key) in held
    except Exception as exc:  # noqa: BLE001
        logger.warning("pair_entry_guard bybit check failed: %s", exc)
        return None


def open_trades_holding_pair_any_side(
    pair: str,
    *,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Any open trade on this pair in any bot (Bybit one-way: one position per symbol)."""
    hits: list[dict[str, Any]] = []
    for side in ("buy", "sell"):
        hits.extend(open_trades_holding_pair(pair, side, tenant_id=tenant_id))
    # de-dupe by bot+trade_id
    seen: set[tuple[str, int]] = set()
    out: list[dict[str, Any]] = []
    for h in hits:
        key = (str(h.get("bot")), int(h.get("trade_id") or 0))
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def busy_reason(
    pair: str,
    side: str | int | bool,
    *,
    bot: str | None = None,
    tenant_id: str | None = None,
    check_bybit: bool = True,
) -> str | None:
    """
    Why this pair+side cannot be entered (None = free).
    Does not create a claim.
    """
    if not pair:
        return "нет pair"
    side_key = _normalize_side(side)
    key = lock_key(pair, side_key)
    now = time.time()

    try:
        conn = _connect(tenant_id)
        try:
            _purge_expired(conn, now)
            row = conn.execute(
                "SELECT bot, ts FROM claims WHERE key = ?", (key,)
            ).fetchone()
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("pair_entry_guard claim read: %s", exc)
        row = None

    if row:
        owner_bot, ts = str(row[0]), float(row[1])
        if bot and owner_bot == bot:
            pass  # our own claim — not busy for us
        elif (now - ts) <= CLAIM_TTL_SEC:
            age = int(now - ts)
            return f"пара занята claim бота {owner_bot} ({age}s назад)"

    try:
        # One-way Bybit account: any side on the pair blocks other bots.
        holders = open_trades_holding_pair_any_side(pair, tenant_id=tenant_id)
    except sqlite3.Error as exc:
        return f"не удалось прочитать БД ботов ({exc}) — вход заблокирован"
    for h in holders:
        if bot and h["bot"] == bot:
            # Same bot already has the trade — ctengine/executor handle self-dupes.
            continue
        return (
            f"уже открыто в {h['bot']} #{h['trade_id']} "
            f"({h.get('enter_tag') or h.get('strategy') or '?'})"
        )

    if check_bybit and (tenant_id or "admin") == "admin":
        # Fresh exchange check on every busy probe (no empty-cache reuse).
        held = bybit_holds_pair(pair, side_key, any_side=True, use_cache=False)
        if held is True:
            ours = [h for h in holders if h["bot"] == bot]
            if not ours:
                return f"на Bybit уже есть позиция по {pair}"
        elif held is None:
            # Never fail-open on exchange check — that created Finder↔Strategy dupes.
            return "не удалось проверить Bybit — вход заблокирован"

    return None


def claim_pair(
    bot: str,
    pair: str,
    side: str | int | bool,
    *,
    tenant_id: str | None = None,
    check_bybit: bool = True,
) -> tuple[bool, str | None]:
    """
    Atomically claim pair+side for ``bot`` (first-wins).
    Returns (True, None) on success, (False, reason) if busy.
    """
    if not pair:
        return False, "нет pair"
    if not bot:
        return False, "нет bot"
    side_key = _normalize_side(side)
    key = lock_key(pair, side_key)
    now = time.time()

    dbs = bot_dbs_available(tenant_id)
    if dbs <= 0:
        return False, f"guard blind: нет bot DB (base={get_base()})"

    # Fast pre-check (DB / Bybit) before taking write lock.
    pre = busy_reason(
        pair,
        side_key,
        bot=bot,
        tenant_id=tenant_id,
        check_bybit=check_bybit,
    )
    if pre:
        return False, pre

    try:
        conn = _connect(tenant_id)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _purge_expired(conn, now)
            row = conn.execute(
                "SELECT bot, ts FROM claims WHERE key = ?", (key,)
            ).fetchone()
            if row:
                owner_bot, ts = str(row[0]), float(row[1])
                if owner_bot != bot and (now - ts) <= CLAIM_TTL_SEC:
                    conn.rollback()
                    return False, f"пара занята claim бота {owner_bot}"
            # Re-check other bot DBs inside the write transaction.
            try:
                holders = open_trades_holding_pair_any_side(pair, tenant_id=tenant_id)
            except sqlite3.Error as exc:
                conn.rollback()
                return False, f"не удалось прочитать БД ботов ({exc})"
            for h in holders:
                if h["bot"] != bot:
                    conn.rollback()
                    return False, (
                        f"уже открыто в {h['bot']} #{h['trade_id']} "
                        f"({h.get('enter_tag') or h.get('strategy') or '?'})"
                    )
            conn.execute(
                """
                INSERT INTO claims(key, bot, pair, side, ts) VALUES(?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    bot=excluded.bot,
                    pair=excluded.pair,
                    side=excluded.side,
                    ts=excluded.ts
                """,
                (key, bot, pair, side_key, now),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("pair_entry_guard claim failed: %s", exc)
        return False, f"claim error: {exc}"

    logger.info(
        "pair claim ok bot=%s %s %s ttl=%ss base=%s dbs=%s",
        bot,
        side_key,
        pair,
        int(CLAIM_TTL_SEC),
        get_base(),
        dbs,
    )
    return True, None


def release_pair(
    bot: str | None,
    pair: str,
    side: str | int | bool,
    *,
    tenant_id: str | None = None,
    force: bool = False,
) -> bool:
    """Drop claim for pair+side. If bot set and not force, only owner can release."""
    if not pair:
        return False
    key = lock_key(pair, side)
    try:
        conn = _connect(tenant_id)
        try:
            if force or not bot:
                conn.execute("DELETE FROM claims WHERE key = ?", (key,))
            else:
                conn.execute(
                    "DELETE FROM claims WHERE key = ? AND bot = ?",
                    (key, bot),
                )
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("pair_entry_guard release: %s", exc)
        return False


def allow_entry(
    bot: str,
    pair: str,
    side: str | int | bool,
    *,
    tenant_id: str | None = None,
    check_bybit: bool = True,
) -> bool:
    """Convenience for confirm_trade_entry: claim or log+False."""
    ok, reason = claim_pair(
        bot,
        pair,
        side,
        tenant_id=tenant_id,
        check_bybit=check_bybit,
    )
    if not ok:
        logger.info(
            "SKIP pair busy bot=%s %s %s — %s",
            bot,
            _normalize_side(side),
            pair,
            reason,
        )
    return ok
