#!/usr/bin/env python3
"""Bot control-plane: desired state + outbox + events (SQLite)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import tenant_manager as tm

BOTS = ("strategy", "grid", "finder")
ERROR_AFTER_SEC = 180

_DB_LOCK = threading.RLock()
_LISTENERS: list[Callable[[dict[str, Any]], None]] = []
_LISTENERS_LOCK = threading.Lock()
_INITED = False


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def db_path() -> Path:
    base = Path(tm.BASE) / "user_data"
    base.mkdir(parents=True, exist_ok=True)
    return base / "control_plane.sqlite"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    global _INITED
    with _DB_LOCK:
        with _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bot_desired (
                    user_id TEXT NOT NULL,
                    bot TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT,
                    PRIMARY KEY (user_id, bot)
                );
                CREATE TABLE IF NOT EXISTS bot_observed (
                    user_id TEXT NOT NULL,
                    bot TEXT NOT NULL,
                    process_up INTEGER NOT NULL DEFAULT 0,
                    trading_state TEXT NOT NULL DEFAULT 'unknown',
                    port INTEGER,
                    last_error TEXT,
                    last_seen_at TEXT,
                    starting_since TEXT,
                    PRIMARY KEY (user_id, bot)
                );
                CREATE TABLE IF NOT EXISTS bot_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    bot TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_bot_commands_pending
                    ON bot_commands(status, id);
                CREATE TABLE IF NOT EXISTS bot_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    bot TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_bot_events_id ON bot_events(id);
                """
            )
            conn.commit()
        _INITED = True


def ensure_init() -> None:
    if not _INITED:
        init_db()


def add_listener(cb: Callable[[dict[str, Any]], None]) -> None:
    with _LISTENERS_LOCK:
        _LISTENERS.append(cb)


def remove_listener(cb: Callable[[dict[str, Any]], None]) -> None:
    with _LISTENERS_LOCK:
        try:
            _LISTENERS.remove(cb)
        except ValueError:
            pass


def _emit(event: dict[str, Any]) -> None:
    with _LISTENERS_LOCK:
        listeners = list(_LISTENERS)
    for cb in listeners:
        try:
            cb(event)
        except Exception:
            pass


def append_event(
    user_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    bot: str | None = None,
) -> dict[str, Any]:
    ensure_init()
    created = _now_iso()
    body = json.dumps(payload, ensure_ascii=False)
    with _DB_LOCK:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO bot_events(user_id, bot, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, bot, event_type, body, created),
            )
            eid = int(cur.lastrowid)
            conn.commit()
    event = {
        "id": eid,
        "user_id": user_id,
        "bot": bot,
        "type": event_type,
        "payload": payload,
        "created_at": created,
    }
    _emit(event)
    return event


def get_desired(user_id: str, bot: str) -> bool | None:
    ensure_init()
    with _DB_LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT enabled FROM bot_desired WHERE user_id=? AND bot=?",
                (user_id, bot),
            ).fetchone()
    if row is None:
        return None
    return bool(row["enabled"])


def set_desired(
    user_id: str,
    bot: str,
    enabled: bool,
    *,
    updated_by: str | None = None,
    enqueue: bool = True,
) -> dict[str, Any]:
    ensure_init()
    if bot not in BOTS:
        raise ValueError(f"invalid bot: {bot}")
    now = _now_iso()
    with _DB_LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_desired(user_id, bot, enabled, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, bot) DO UPDATE SET
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at,
                    updated_by=excluded.updated_by
                """,
                (user_id, bot, 1 if enabled else 0, now, updated_by),
            )
            cmd_id = None
            if enqueue:
                action = "enable" if enabled else "disable"
                cur = conn.execute(
                    """
                    INSERT INTO bot_commands(user_id, bot, action, status, attempts, created_at)
                    VALUES (?, ?, ?, 'pending', 0, ?)
                    """,
                    (user_id, bot, action, now),
                )
                cmd_id = int(cur.lastrowid)
            if enabled:
                conn.execute(
                    """
                    INSERT INTO bot_observed(user_id, bot, process_up, trading_state, starting_since, last_seen_at)
                    VALUES (?, ?, 0, 'unknown', ?, ?)
                    ON CONFLICT(user_id, bot) DO UPDATE SET
                        starting_since=COALESCE(bot_observed.starting_since, excluded.starting_since),
                        last_error=NULL
                    """,
                    (user_id, bot, now, now),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO bot_observed(user_id, bot, process_up, trading_state, starting_since, last_seen_at)
                    VALUES (?, ?, 0, 'unknown', NULL, ?)
                    ON CONFLICT(user_id, bot) DO UPDATE SET starting_since=NULL
                    """,
                    (user_id, bot, now),
                )
            conn.commit()
    snap = snapshot_bot(user_id, bot)
    append_event(
        user_id,
        "bot.status",
        snap,
        bot=bot,
    )
    if enqueue and cmd_id is not None:
        append_event(
            user_id,
            "bot.command",
            {"id": cmd_id, "bot": bot, "action": "enable" if enabled else "disable", "status": "pending"},
            bot=bot,
        )
    return snap


def update_observed(
    user_id: str,
    bot: str,
    *,
    process_up: bool | None = None,
    trading_state: str | None = None,
    port: int | None = None,
    last_error: str | None = None,
    clear_error: bool = False,
    clear_starting: bool = False,
) -> dict[str, Any]:
    ensure_init()
    now = _now_iso()
    with _DB_LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM bot_observed WHERE user_id=? AND bot=?",
                (user_id, bot),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO bot_observed(
                        user_id, bot, process_up, trading_state, port, last_error, last_seen_at, starting_since
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        user_id,
                        bot,
                        1 if process_up else 0,
                        trading_state or "unknown",
                        port,
                        None if clear_error else last_error,
                        now,
                    ),
                )
            else:
                pu = int(row["process_up"]) if process_up is None else (1 if process_up else 0)
                ts = trading_state if trading_state is not None else row["trading_state"]
                pt = port if port is not None else row["port"]
                err = row["last_error"]
                if clear_error:
                    err = None
                elif last_error is not None:
                    err = last_error
                starting = None if clear_starting else row["starting_since"]
                if trading_state == "running":
                    starting = None
                conn.execute(
                    """
                    UPDATE bot_observed SET
                        process_up=?, trading_state=?, port=?, last_error=?,
                        last_seen_at=?, starting_since=?
                    WHERE user_id=? AND bot=?
                    """,
                    (pu, ts, pt, err, now, starting, user_id, bot),
                )
            conn.commit()
    return snapshot_bot(user_id, bot)


def derive_status(desired: bool, observed: dict[str, Any] | None) -> str:
    obs = observed or {}
    process_up = bool(obs.get("process_up"))
    trading = str(obs.get("trading_state") or "unknown")
    last_error = obs.get("last_error")
    starting_since = obs.get("starting_since")
    if not desired:
        if process_up and trading == "running":
            return "STOPPING"
        return "OFF"
    if process_up and trading == "running":
        return "RUNNING"
    if last_error and starting_since:
        try:
            started = datetime.fromisoformat(str(starting_since))
            if (datetime.now(tz=UTC) - started).total_seconds() >= ERROR_AFTER_SEC:
                return "ERROR"
        except ValueError:
            pass
    if last_error and not process_up and not starting_since:
        return "ERROR"
    return "STARTING"


def snapshot_bot(user_id: str, bot: str) -> dict[str, Any]:
    ensure_init()
    with _DB_LOCK:
        with _connect() as conn:
            drow = conn.execute(
                "SELECT * FROM bot_desired WHERE user_id=? AND bot=?",
                (user_id, bot),
            ).fetchone()
            orow = conn.execute(
                "SELECT * FROM bot_observed WHERE user_id=? AND bot=?",
                (user_id, bot),
            ).fetchone()
    desired = bool(drow["enabled"]) if drow else False
    observed = None
    if orow:
        observed = {
            "process_up": bool(orow["process_up"]),
            "trading_state": orow["trading_state"],
            "port": orow["port"],
            "last_error": orow["last_error"],
            "last_seen_at": orow["last_seen_at"],
            "starting_since": orow["starting_since"],
        }
    status = derive_status(desired, observed)
    return {
        "user_id": user_id,
        "bot": bot,
        "enabled": desired,
        "desired": desired,
        "status": status,
        "observed": observed
        or {
            "process_up": False,
            "trading_state": "unknown",
            "port": None,
            "last_error": None,
            "last_seen_at": None,
            "starting_since": None,
        },
        "updated_at": drow["updated_at"] if drow else None,
    }


def snapshot_all(user_id: str) -> dict[str, Any]:
    bots = {b: snapshot_bot(user_id, b) for b in BOTS}
    return {"user_id": user_id, "bots": bots}


def claim_pending_commands(limit: int = 8) -> list[dict[str, Any]]:
    ensure_init()
    out: list[dict[str, Any]] = []
    with _DB_LOCK:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM bot_commands
                WHERE status='pending'
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            for row in rows:
                cur = conn.execute(
                    """
                    UPDATE bot_commands
                    SET status='running', attempts=attempts+1
                    WHERE id=? AND status='pending'
                    """,
                    (row["id"],),
                )
                if cur.rowcount:
                    out.append(dict(row))
            conn.commit()
    return out


def finish_command(cmd_id: int, *, ok: bool, error: str | None = None) -> None:
    ensure_init()
    now = _now_iso()
    with _DB_LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT user_id, bot, action FROM bot_commands WHERE id=?",
                (cmd_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE bot_commands
                SET status=?, finished_at=?, error=?
                WHERE id=?
                """,
                ("done" if ok else "failed", now, error, cmd_id),
            )
            conn.commit()
    if row:
        append_event(
            str(row["user_id"]),
            "bot.command",
            {
                "id": cmd_id,
                "bot": row["bot"],
                "action": row["action"],
                "status": "done" if ok else "failed",
                "error": error,
            },
            bot=str(row["bot"]),
        )


def events_since(user_id: str, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
    ensure_init()
    with _DB_LOCK:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM bot_events
                WHERE user_id=? AND id>?
                ORDER BY id ASC
                LIMIT ?
                """,
                (user_id, after_id, limit),
            ).fetchall()
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            payload = {}
        out.append(
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "bot": row["bot"],
                "type": row["event_type"],
                "payload": payload,
                "created_at": row["created_at"],
            }
        )
    return out


def seed_from_trading_flags() -> None:
    """One-time-ish seed: copy trading_enabled.json into bot_desired when missing."""
    ensure_init()
    import user_trading

    users = ["admin"]
    for u in tm.load_users():
        uid = str(u.get("id") or "")
        if uid and uid not in users:
            users.append(uid)
    for uid in users:
        flags = user_trading.load_trading_flags(uid)
        for bot in BOTS:
            if get_desired(uid, bot) is not None:
                continue
            set_desired(uid, bot, bool(flags.get(bot, False)), updated_by="seed", enqueue=False)


def sync_flags_from_desired(user_id: str) -> dict[str, bool]:
    """Write trading_enabled.json from bot_desired (all bots)."""
    import user_trading

    flags = {}
    for bot in BOTS:
        d = get_desired(user_id, bot)
        if d is None:
            flags[bot] = bool(user_trading.DEFAULT_FLAGS.get(bot, False))
        else:
            flags[bot] = bool(d)
    return user_trading.save_trading_flags(user_id, flags)
