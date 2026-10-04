#!/usr/bin/env python3
"""Local stack health check + auto-repair for 2h watch loop."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

SITE = Path(os.environ.get("CT_BASE", r"D:\cryptotools\site"))
SCRIPTS = SITE / "scripts"
LOG = SITE / "user_data" / "logs"
PY = SITE / ".venv" / "Scripts" / "python.exe"
CTBOT = SITE / ".venv" / "Scripts" / "ctbot.exe"
HB = LOG / "trade-executor.heartbeat"
FEED = SITE / "user_data" / "signals" / "feed.jsonl"
OFFSET = SITE / "user_data" / "signals" / "feed.offset"
STATE = LOG / "stack-watch-state.json"
EXE_LOG = LOG / "trade-executor.log"
ENV_FILE = SITE / ".env"


def _load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _port_up(port: int) -> bool:
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _procs() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        raw = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { "
                    "$_.Name -match '^(python|ctbot)\\.exe$' -and $_.CommandLine -match "
                    "'local_site_gateway|pair_config_server|trade_executor\\.py|"
                    "config_signal_engine|config_grid\\.json|user_data\\\\config\\.json|"
                    "TradeFinderStrategy|VolatilityGridStrategy' "
                    "} | "
                    "ForEach-Object { "
                    "$tag='other'; "
                    "if($_.CommandLine -match 'gateway'){$tag='gateway'} "
                    "elseif($_.CommandLine -match 'pair_config'){$tag='pair'} "
                    "elseif($_.CommandLine -match 'trade_executor'){$tag='executor'} "
                    "elseif($_.CommandLine -match 'config_grid|VolatilityGrid'){$tag='grid'} "
                    "elseif($_.CommandLine -match 'TradeFinder|user_data\\\\config\\.json'){$tag='finder'} "
                    "elseif($_.CommandLine -match 'signal_engine'){$tag='signal'}; "
                    "$exe=if($_.ExecutablePath -match '\\\\.venv\\\\'){'venv'}else{'sys'}; "
                    "Write-Output ([string]$_.ProcessId + '|' + $exe + '|' + $tag) "
                    "}"
                ),
            ],
            text=True,
            errors="replace",
        )
    except Exception as exc:
        return [{"error": str(exc)}]
    for line in raw.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        pid, exe, tag = line.split("|", 2)
        out.append({"pid": pid, "exe": exe, "tag": tag})
    return out


def _hb_age() -> float | None:
    if not HB.is_file():
        return None
    try:
        return time.time() - float(HB.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _feed_stats() -> dict:
    n = 0
    if FEED.is_file():
        n = sum(1 for _ in FEED.open(encoding="utf-8", errors="replace"))
    off = None
    if OFFSET.is_file():
        try:
            off = json.loads(OFFSET.read_text(encoding="utf-8")).get("line")
        except Exception:
            off = OFFSET.read_text(encoding="utf-8").strip()
    return {"feed_lines": n, "offset": off}


def _recent_skips(limit: int = 8) -> list[str]:
    if not EXE_LOG.is_file():
        return []
    lines = EXE_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    interesting = [
        ln
        for ln in lines[-80:]
        if any(x in ln for x in ("ENTRY OK", "ORDER FAIL", "SKIP", "ERROR", "STATUS", "ТОРГОВЛЯ"))
    ]
    return interesting[-limit:]


def _start(name: str, args: list[str], *, signal_only: bool = False) -> None:
    LOG.mkdir(parents=True, exist_ok=True)
    out = LOG / f"{name}.out.log"
    err = LOG / f"{name}.err.log"
    env = os.environ.copy()
    env.update(_load_dotenv(ENV_FILE))
    env["CT_BASE"] = str(SITE)
    env["CT_ENV"] = str(ENV_FILE)
    env["PYTHONPATH"] = str(SCRIPTS)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PAIR_CONFIG_HOST"] = "127.0.0.1"
    env["PAIR_CONFIG_PORT"] = "8090"
    if signal_only:
        env["CT_SIGNAL_ONLY"] = "1"
    else:
        env.pop("CT_SIGNAL_ONLY", None)
    # map FREQUI_/BYBIT_ like load_env.ps1
    mapping = {
        "FREQUI_USERNAME": "CTENGINE__API_SERVER__USERNAME",
        "FREQUI_PASSWORD": "CTENGINE__API_SERVER__PASSWORD",
        "FREQUI_JWT_SECRET": "CTENGINE__API_SERVER__JWT_SECRET_KEY",
        "BYBIT_API_KEY": "CTENGINE__EXCHANGE__KEY",
        "BYBIT_API_SECRET": "CTENGINE__EXCHANGE__SECRET",
    }
    for src, dst in mapping.items():
        if env.get(src) and not env.get(dst):
            env[dst] = env[src]
    if str(env.get("BYBIT_DEMO_TRADING") or "").lower() in ("1", "true", "yes", "on"):
        env["CTENGINE__EXCHANGE__DEMO_TRADING"] = "true"
    creationflags = 0x08000000  # CREATE_NO_WINDOW
    subprocess.Popen(
        args,
        cwd=str(SITE),
        stdout=out.open("a", encoding="utf-8"),
        stderr=err.open("a", encoding="utf-8"),
        env=env,
        creationflags=creationflags,
    )


def _kill_sys_duplicates(procs: list[dict[str, str]]) -> list[str]:
    """Do not kill 'sys' python — ctbot often spawns a system-python worker.

    Duplicate cleanup caused 8081 flaps. Only restart when ports/heartbeat are unhealthy.
    """
    return []


def _ensure_service(
    tag: str,
    procs: list[dict[str, str]],
    port: int | None,
    start_args: list[str],
    name: str,
    *,
    signal_only: bool = False,
) -> str:
    has_any = any(p.get("tag") == tag for p in procs)
    up = _port_up(port) if port is not None else True
    if tag == "executor":
        age = _hb_age()
        if has_any and age is not None and age < 90:
            return "ok"
        # stale/missing heartbeat — restart executor only
        for p in procs:
            if p.get("tag") == "executor":
                try:
                    subprocess.check_call(
                        ["taskkill", "/PID", p["pid"], "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    pass
        _start(name, start_args, signal_only=False)
        return "restarted"
    if has_any and up:
        return "ok"
    # Down: kill leftovers for this tag then start fresh
    for p in procs:
        if p.get("tag") == tag:
            try:
                subprocess.check_call(
                    ["taskkill", "/PID", p["pid"], "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
    _start(name, start_args, signal_only=signal_only)
    return "restarted"


def main() -> int:
    raw_tick = str(os.environ.get("STACK_WATCH_TICK") or "0").strip()
    try:
        tick = int(raw_tick)
    except ValueError:
        tick = 0
    procs = _procs()
    killed = _kill_sys_duplicates(procs)
    time.sleep(1)
    procs = _procs()

    actions = {
        "pair": _ensure_service(
            "pair",
            procs,
            8090,
            [str(PY), str(SCRIPTS / "pair_config_server.py")],
            "pair-config",
        ),
        "gateway": _ensure_service(
            "gateway",
            procs,
            8443,
            [str(PY), str(SCRIPTS / "local_site_gateway.py"), "--host", "127.0.0.1", "--port", "8443"],
            "gateway",
        ),
        "signal": _ensure_service(
            "signal",
            procs,
            8081,
            [
                str(CTBOT),
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
            signal_only=True,
        ),
        "grid": _ensure_service(
            "grid",
            procs,
            8082,
            [
                str(CTBOT),
                "trade",
                "--config",
                "user_data\\config_grid.json",
                "--strategy",
                "VolatilityGridStrategy",
                "--strategy-path",
                "user_data\\strategies",
                "--logfile",
                "user_data\\logs\\cryptotools-grid.log",
            ],
            "grid",
        ),
        "finder": _ensure_service(
            "finder",
            procs,
            8080,
            [
                str(CTBOT),
                "trade",
                "--config",
                "user_data\\config.json",
                "--strategy",
                "TradeFinderStrategy",
                "--strategy-path",
                "user_data\\strategies",
                "--logfile",
                "user_data\\logs\\cryptotools-finder.log",
            ],
            "finder",
        ),
        "executor": _ensure_service(
            "executor",
            procs,
            None,
            [str(PY), str(SCRIPTS / "trade_executor.py")],
            "trade-executor",
        ),
    }
    time.sleep(4)
    procs2 = _procs()
    report = {
        "ts": _now(),
        "tick": tick,
        "ports": {
            8090: _port_up(8090),
            8443: _port_up(8443),
            8081: _port_up(8081),
            8082: _port_up(8082),
            8080: _port_up(8080),
        },
        "hb_age_s": _hb_age(),
        "feed": _feed_stats(),
        "killed_sys": killed,
        "actions": actions,
        "procs": procs2,
        "recent": _recent_skips(),
    }
    ok = (
        report["ports"][8090]
        and report["ports"][8443]
        and report["ports"][8081]
        and report["ports"][8082]
        and report["ports"][8080]
        and report["hb_age_s"] is not None
        and report["hb_age_s"] < 120
    )
    report["ok"] = ok
    STATE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("STACK_WATCH_OK" if ok else "STACK_WATCH_BAD")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
