#!/usr/bin/env python3
"""Reconcile desired bot state: process ensure + ctengine start/stop + trading flags."""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import control_plane as cp
import tenant_manager as tm
import user_trading

logger = logging.getLogger("bot_reconcile")

BASE = Path(os.environ.get("CT_BASE", tm.BASE))
SCRIPTS = BASE / "scripts"
IS_WINDOWS = sys.platform.startswith("win")

ADMIN_PORTS = {"finder": 8080, "strategy": 8081, "grid": 8082}
ADMIN_UNITS = {
    "strategy": "cryptotools-signal-engine",
    "grid": "cryptotools-grid",
    "finder": "cryptotools-finder",
}

_WORKER_STOP = threading.Event()
_WORKER_STARTED = False


def _stable_observed(obs: Any) -> Any:
    """Observed snapshot without last_seen_at — it changes every poll and
    otherwise makes the before/after comparison always differ, publishing a
    bot.status event every tick (~600k/day → multi-GB bot_events)."""
    if isinstance(obs, dict) and "last_seen_at" in obs:
        return {k: v for k, v in obs.items() if k != "last_seen_at"}
    return obs


def _ctbot() -> Path:
    if IS_WINDOWS:
        return BASE / ".venv" / "Scripts" / "ctbot.exe"
    return BASE / ".venv" / "bin" / "ctbot"


def _python() -> Path:
    if IS_WINDOWS:
        return BASE / ".venv" / "Scripts" / "python.exe"
    return BASE / ".venv" / "bin" / "python3"


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def bot_port(user_id: str, bot: str) -> int:
    if user_id == "admin":
        return ADMIN_PORTS[bot]
    ports = tm.user_ports(user_id) if hasattr(tm, "user_ports") else None
    if isinstance(ports, dict) and bot in ports:
        return int(ports[bot])
    user = tm.get_user_by_id(user_id) or {}
    p = (user.get("ports") or {}).get(bot)
    if p:
        return int(p)
    return ADMIN_PORTS[bot]


def http_json(url: str, method: str = "GET", timeout: float = 5.0) -> tuple[int, Any]:
    req = urllib.request.Request(url, method=method)
    # Basic auth from env (admin bots)
    user = os.environ.get("FREQUI_USERNAME") or os.environ.get("CTENGINE__API_SERVER__USERNAME") or ""
    password = os.environ.get("FREQUI_PASSWORD") or os.environ.get("CTENGINE__API_SERVER__PASSWORD") or ""
    if user:
        import base64

        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw) if raw else {"error": str(exc)}
        except json.JSONDecodeError:
            return exc.code, {"error": raw or str(exc)}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def ping_bot(port: int) -> bool:
    code, _ = http_json(f"http://127.0.0.1:{port}/api/v1/ping", timeout=2.0)
    return code == 200


def show_config_state(port: int) -> str:
    code, data = http_json(f"http://127.0.0.1:{port}/api/v1/show_config", timeout=3.0)
    if code != 200 or not isinstance(data, dict):
        return "unknown"
    return str(data.get("state") or "unknown").lower()


# ctbot flaps through these while applying whitelist / max_open / reload_config.
_RUNNING_LIKE_STATES = frozenset({"running", "reload_config", "reloading"})


def ctbot_start_stop(port: int, action: str) -> tuple[bool, str]:
    code, data = http_json(f"http://127.0.0.1:{port}/api/v1/{action}", method="POST", timeout=8.0)
    if code in (200, 201):
        return True, ""
    err = ""
    if isinstance(data, dict):
        err = str(data.get("error") or data.get("detail") or data)
    else:
        err = str(data)
    # Already running / stopped is OK
    low = err.lower()
    if action == "start" and ("already" in low or "running" in low):
        return True, ""
    if action == "stop" and ("already" in low or "stopped" in low):
        return True, ""
    return False, err or f"HTTP {code}"


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run systemctl; prefer NOPASSWD admin wrapper for shared units."""
    wrapper = BASE / "deploy" / "admin-systemctl.sh"
    if wrapper.is_file() and args:
        action = args[0]
        unit = args[1] if len(args) > 1 else ""
        # Accept both short and .service names
        if unit and not unit.endswith(".service") and action != "daemon-reload":
            unit = f"{unit}.service"
        if action == "daemon-reload":
            try:
                return subprocess.run(
                    ["sudo", "-n", str(wrapper), "daemon-reload"],
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return subprocess.CompletedProcess(
                    args=["sudo", str(wrapper), "daemon-reload"],
                    returncode=1,
                    stdout="",
                    stderr=str(exc),
                )
        if action in ("start", "stop", "restart", "enable", "disable", "is-active") and unit:
            try:
                return subprocess.run(
                    ["sudo", "-n", str(wrapper), action, unit],
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return subprocess.CompletedProcess(
                    args=["sudo", str(wrapper), action, unit],
                    returncode=1,
                    stdout="",
                    stderr=str(exc),
                )
    try:
        return subprocess.run(
            ["systemctl", *args],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(args=["systemctl", *args], returncode=1, stdout="", stderr=str(exc))


def _shared_strategy(bot: str) -> bool:
    """Strategy uses shared signal-engine + trade-executor for every user."""
    return bot == "strategy"


def _tenant_systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Prefer NOPASSWD wrapper; fall back to plain systemctl."""
    wrapper = BASE / "deploy" / "tenant-systemctl.sh"
    if wrapper.is_file() and args:
        # Map: start|stop|restart|enable|disable|is-active UNIT → wrapper ACTION UNIT
        action = args[0]
        unit = args[1] if len(args) > 1 else ""
        if action in ("start", "stop", "restart", "enable", "disable", "is-active") and unit:
            try:
                return subprocess.run(
                    ["sudo", "-n", str(wrapper), action, unit],
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return subprocess.CompletedProcess(
                    args=["sudo", str(wrapper), action, unit],
                    returncode=1,
                    stdout="",
                    stderr=str(exc),
                )
    return _systemctl(*args)


def ensure_process_linux(user_id: str, bot: str) -> tuple[bool, str]:
    if _shared_strategy(bot):
        # Shared stack for admin AND tenants (no cryptotools-strategy@user).
        for unit in ("cryptotools-signal-engine", "trade-executor"):
            st = _systemctl("is-active", unit)
            if (st.stdout or "").strip() != "active":
                cp_res = _systemctl("start", unit)
                if cp_res.returncode != 0:
                    return False, (cp_res.stderr or cp_res.stdout or f"start {unit} failed").strip()
        return True, ""
    if user_id == "admin":
        unit = ADMIN_UNITS[bot]
        st = _systemctl("is-active", unit)
        if (st.stdout or "").strip() == "active":
            return True, ""
        cp_res = _systemctl("start", unit)
        if cp_res.returncode != 0:
            return False, (cp_res.stderr or cp_res.stdout or f"start {unit} failed").strip()
        return True, ""
    # Tenant grid/finder units (need sudoers → tenant-systemctl.sh)
    unit = f"cryptotools-{bot}@{user_id}.service"
    st = _tenant_systemctl("is-active", unit)
    if (st.stdout or "").strip() == "active":
        return True, ""
    _tenant_systemctl("enable", unit)
    cp_res = _tenant_systemctl("start", unit)
    if cp_res.returncode != 0:
        return False, (cp_res.stderr or cp_res.stdout or f"start {unit} failed").strip()
    return True, ""


def _spawn_windows(args: list[str], log_tag: str) -> tuple[bool, str]:
    ctbot = _ctbot()
    if not ctbot.is_file() and args and "ctbot" in args[0]:
        return False, f"ctbot missing: {ctbot}"
    log_dir = BASE / "user_data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out = log_dir / f"{log_tag}.out.log"
    err = log_dir / f"{log_tag}.err.log"
    env = os.environ.copy()
    env.setdefault("CT_BASE", str(BASE))
    env.setdefault("CT_ENV", str(BASE / ".env"))
    try:
        # Detached process
        creationflags = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creationflags |= subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        with open(out, "a", encoding="utf-8") as fo, open(err, "a", encoding="utf-8") as fe:
            subprocess.Popen(
                args,
                cwd=str(BASE),
                stdout=fo,
                stderr=fe,
                env=env,
                creationflags=creationflags,
                close_fds=True,
            )
        return True, ""
    except OSError as exc:
        return False, str(exc)


def ensure_process_windows(user_id: str, bot: str) -> tuple[bool, str]:
    if _shared_strategy(bot):
        # Shared signal-engine + executor for every user (tenant = flags only).
        if not port_open(8081):
            ok, err = _spawn_windows(
                [
                    str(_ctbot()),
                    "trade",
                    "--config",
                    "user_data\\config_signal_engine.json",
                    "--strategy",
                    "MultiStrategyRouter",
                    "--strategy-path",
                    "user_data\\strategies",
                    "--logfile",
                    "user_data\\logs\\cryptotools-strategy.log",
                ],
                "signal-engine",
            )
            if not ok:
                return False, err
        hb = BASE / "user_data" / "logs" / "trade-executor.heartbeat"
        fresh = False
        if hb.is_file():
            try:
                fresh = (time.time() - hb.stat().st_mtime) < 120
            except OSError:
                fresh = False
        if not fresh:
            ok, err = _spawn_windows(
                [str(_python()), str(SCRIPTS / "trade_executor.py")],
                "trade-executor",
            )
            if not ok:
                return False, err
        for _ in range(20):
            if port_open(8081):
                return True, ""
            time.sleep(0.5)
        return False, "signal-engine port 8081 not up"

    if user_id != "admin":
        # Local multi-tenant grid/finder spawn not implemented
        port = bot_port(user_id, bot)
        if port_open(port):
            return True, ""
        return False, f"tenant bot process not running on :{port}"

    port = ADMIN_PORTS[bot]
    if port_open(port):
        return True, ""

    if bot == "grid":
        args = [
            str(_ctbot()),
            "trade",
            "--config",
            "user_data\\config_grid.json",
            "--strategy",
            "VolatilityGridStrategy",
            "--strategy-path",
            "user_data\\strategies",
            "--logfile",
            "user_data\\logs\\cryptotools-grid.log",
        ]
        tag = "cryptotools-grid"
    else:
        args = [
            str(_ctbot()),
            "trade",
            "--config",
            "user_data\\config.json",
            "--strategy",
            "TradeFinderStrategy",
            "--strategy-path",
            "user_data\\strategies",
            "--logfile",
            "user_data\\logs\\cryptotools-finder.log",
        ]
        tag = "cryptotools-finder"
    ok, err = _spawn_windows(args, tag)
    if not ok:
        return False, err
    for _ in range(30):
        if port_open(port):
            return True, ""
        time.sleep(0.5)
    return False, f"{bot} port {port} not up after spawn"


def ensure_process(user_id: str, bot: str) -> tuple[bool, str]:
    if IS_WINDOWS:
        return ensure_process_windows(user_id, bot)
    return ensure_process_linux(user_id, bot)


def refresh_observed(user_id: str, bot: str) -> dict[str, Any]:
    port = bot_port(user_id, bot)
    if _shared_strategy(bot):
        process_up = port_open(8081)
        trading = "unknown"
        if process_up:
            trading = show_config_state(8081)
        # Trading enabled flag gates "effective" running for strategy
        flagged = user_trading.is_trading_enabled(user_id, "strategy")
        if process_up and flagged and trading in _RUNNING_LIKE_STATES:
            # reload_config is a brief healthy flap — not a fresh STARTING.
            eff = "running"
        elif process_up and flagged and trading == "unknown":
            # show_config timeout must NOT flip UI to STOPPED/STARTING every poll.
            eff = "running"
        elif process_up and flagged and trading in ("stopped", "paused"):
            # Shared signal-engine can briefly report stopped while still serving API;
            # keep running if process is up and user wants trading on.
            eff = "running"
        elif process_up and flagged:
            eff = trading if trading else "running"
        else:
            eff = "stopped"
        return cp.update_observed(
            user_id,
            bot,
            process_up=process_up,
            trading_state=eff,
            port=8081,
            clear_error=process_up and eff == "running",
            clear_starting=eff == "running",
        )

    # Grid / Finder: port open is enough for process_up. Requiring ping AND port
    # made brief API timeouts flip UI STARTING↔RUNNING forever.
    process_up = port_open(port)
    # Accept-queue full / DDoS backoff: connect fails but systemd unit is still up.
    if not process_up and user_id == "admin" and bot in ADMIN_UNITS:
        st_unit = _systemctl("is-active", ADMIN_UNITS[bot])
        if (st_unit.stdout or "").strip() == "active":
            process_up = True
    trading = "unknown"
    if process_up and port_open(port):
        trading = show_config_state(port)
    flagged = user_trading.is_trading_enabled(user_id, bot)
    if process_up and flagged and trading in _RUNNING_LIKE_STATES:
        eff = "running"
    elif process_up and flagged and trading in ("unknown", "stopped", "paused"):
        # Same sticky rule as shared strategy — avoid badge flap during reload.
        eff = "running"
    elif process_up and flagged:
        eff = trading if trading else "running"
    elif process_up and not flagged:
        eff = "stopped"
    else:
        eff = "unknown"
    return cp.update_observed(
        user_id,
        bot,
        process_up=process_up,
        trading_state=eff,
        port=port,
        clear_error=process_up and eff == "running",
        clear_starting=eff == "running",
    )


def apply_enable(user_id: str, bot: str) -> tuple[bool, str]:
    user_trading.save_trading_flags(user_id, {bot: True})
    ok, err = ensure_process(user_id, bot)
    if not ok:
        cp.update_observed(user_id, bot, process_up=False, last_error=err)
        return False, err

    if _shared_strategy(bot):
        # Shared strategy: trading flag is enough; keep signal-engine running.
        for _ in range(15):
            if ping_bot(8081):
                break
            time.sleep(0.4)
        # Skip /start if already running — avoids reload flap.
        if show_config_state(8081) not in _RUNNING_LIKE_STATES:
            ctbot_start_stop(8081, "start")
    else:
        port = bot_port(user_id, bot)
        for _ in range(15):
            if port_open(port):
                break
            time.sleep(0.4)
        st = show_config_state(port) if port_open(port) else "unknown"
        # unknown = API timeout while process is up (Finder exchange backoff /
        # accept queue full). Calling /start here caused endless reload flaps.
        if st in _RUNNING_LIKE_STATES or st == "unknown":
            pass
        elif st in ("stopped", "paused"):
            started, start_err = ctbot_start_stop(port, "start")
            if not started:
                cp.update_observed(
                    user_id, bot, process_up=True, trading_state="stopped", last_error=start_err
                )
                return False, start_err
        elif not port_open(port):
            # Unit may already be active; do not hammer a wedged API.
            pass
        else:
            started, start_err = ctbot_start_stop(port, "start")
            if not started:
                cp.update_observed(
                    user_id, bot, process_up=True, trading_state="stopped", last_error=start_err
                )
                return False, start_err

    snap = refresh_observed(user_id, bot)
    cp.append_event(user_id, "bot.status", snap, bot=bot)
    try:
        import telegram_links as tg_links

        tg_links.notify_user(
            user_id,
            tg_links.format_bot_event_ru(action="start", bot=bot, user_id=user_id),
            kind="bot_start",
        )
    except Exception:
        pass
    return True, ""


def apply_disable(user_id: str, bot: str) -> tuple[bool, str]:
    user_trading.save_trading_flags(user_id, {bot: False})

    if _shared_strategy(bot):
        # Shared: only flag off — leave signal process running for other users.
        snap = refresh_observed(user_id, bot)
        cp.append_event(user_id, "bot.status", snap, bot=bot)
        try:
            import telegram_links as tg_links

            tg_links.notify_user(
                user_id,
                tg_links.format_bot_event_ru(
                    action="stop",
                    bot=bot,
                    user_id=user_id,
                    detail="Автоторговля стратегиями выключена",
                ),
                kind="bot_stop",
            )
        except Exception:
            pass
        return True, ""

    port = bot_port(user_id, bot)
    if port_open(port):
        ok, err = ctbot_start_stop(port, "stop")
        if not ok:
            cp.update_observed(user_id, bot, process_up=True, last_error=err)
            snap = refresh_observed(user_id, bot)
            cp.append_event(user_id, "bot.status", snap, bot=bot)
            return False, err

    snap = refresh_observed(user_id, bot)
    cp.append_event(user_id, "bot.status", snap, bot=bot)
    try:
        import telegram_links as tg_links

        tg_links.notify_user(
            user_id,
            tg_links.format_bot_event_ru(action="stop", bot=bot, user_id=user_id),
            kind="bot_stop",
        )
    except Exception:
        pass
    return True, ""


def process_command(cmd: dict[str, Any]) -> None:
    cmd_id = int(cmd["id"])
    user_id = str(cmd["user_id"])
    bot = str(cmd["bot"])
    action = str(cmd["action"])
    try:
        if action == "enable":
            ok, err = apply_enable(user_id, bot)
        elif action == "disable":
            ok, err = apply_disable(user_id, bot)
        elif action == "reconcile":
            desired = cp.get_desired(user_id, bot)
            if desired is None:
                ok, err = True, ""
            elif desired:
                ok, err = apply_enable(user_id, bot)
            else:
                ok, err = apply_disable(user_id, bot)
        else:
            ok, err = False, f"unknown action {action}"
        cp.finish_command(cmd_id, ok=ok, error=err or None)
        snap = cp.snapshot_bot(user_id, bot)
        cp.append_event(user_id, "bot.status", snap, bot=bot)
    except Exception as exc:  # noqa: BLE001
        logger.exception("command %s failed", cmd_id)
        cp.finish_command(cmd_id, ok=False, error=str(exc))
        cp.update_observed(user_id, bot, last_error=str(exc))


def reconcile_tick() -> None:
    cmds = cp.claim_pending_commands(limit=8)
    for cmd in cmds:
        process_command(cmd)
    # Refresh observed for users with desired rows
    try:
        with cp._connect() as conn:  # noqa: SLF001
            rows = conn.execute("SELECT DISTINCT user_id, bot FROM bot_desired").fetchall()
    except Exception:
        rows = []
    for row in rows:
        try:
            before = cp.snapshot_bot(row["user_id"], row["bot"])
            after = refresh_observed(row["user_id"], row["bot"])
            if before.get("status") != after.get("status") or _stable_observed(
                before.get("observed")
            ) != _stable_observed(after.get("observed")):
                cp.append_event(row["user_id"], "bot.status", after, bot=row["bot"])
        except Exception:
            logger.debug("refresh observed failed", exc_info=True)


def _loop(poll_sec: float = 2.5) -> None:
    cp.ensure_init()
    cp.seed_from_trading_flags()
    while not _WORKER_STOP.is_set():
        try:
            reconcile_tick()
        except Exception:
            logger.exception("reconcile tick failed")
        _WORKER_STOP.wait(poll_sec)


def start_reconcile_worker(poll_sec: float = 2.5) -> None:
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    _WORKER_STARTED = True
    t = threading.Thread(target=_loop, kwargs={"poll_sec": poll_sec}, name="bot-reconcile", daemon=True)
    t.start()
    logger.info("bot reconcile worker started (poll=%.1fs)", poll_sec)
