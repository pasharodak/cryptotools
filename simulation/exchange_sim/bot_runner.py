"""Run all trading bot backtests for the player-selected period."""
from __future__ import annotations

import json
import re
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


BOT_SCENARIOS = [
    {
        "id": "grid_improved",
        "label": "Grid FT",
        "config": "simulation/config/backtest_grid_improved.json",
        "strategy": "VolatilityGridStrategy",
    },
    {
        "id": "strategy_improved",
        "label": "Стратегии",
        "config": "simulation/config/backtest_strategy_improved.json",
        "strategy": "MultiStrategyRouter",
    },
]


def ms_to_timerange(start_ms: int, end_ms: int) -> str:
    s = datetime.fromtimestamp(start_ms / 1000, tz=UTC).strftime("%Y%m%d")
    e = datetime.fromtimestamp(end_ms / 1000, tz=UTC).strftime("%Y%m%d")
    return f"{s}-{e}"


def patch_whitelist(root: Path, config_rel: str, pairs: list[str], out: Path) -> None:
    cfg = json.loads((root / config_rel).read_text(encoding="utf-8"))
    cfg["exchange"]["pair_whitelist"] = pairs
    frag = root / "simulation" / "config" / "sim_common_fragment.json"
    if frag.is_file():
        for key, val in json.loads(frag.read_text(encoding="utf-8")).items():
            if key == "exchange":
                cfg.setdefault("exchange", {}).update(val)
            else:
                cfg[key] = val
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, indent=4), encoding="utf-8")


def parse_backtest_pnl(stdout: str) -> dict[str, Any]:
    m = re.search(r"Total profit %.*?(-?\d+\.?\d*)", stdout)
    trades = re.search(r"Total/Daily Avg Trades\s*\|\s*(\d+)", stdout)
    balance = re.search(r"Final balance\s*\|\s*(-?\d+\.?\d*)", stdout)
    return {
        "profit_pct": float(m.group(1)) if m else None,
        "trades": int(trades.group(1)) if trades else 0,
        "final_balance": float(balance.group(1)) if balance else None,
    }


class BotRunner:
    def __init__(self, root: Path, on_update: Callable[[dict], None] | None = None):
        self.root = root
        self.on_update = on_update
        self._thread: threading.Thread | None = None
        self.status: dict[str, Any] = {"running": False, "scenarios": {}}

    def is_running(self) -> bool:
        return bool(self.status.get("running"))

    def run_all(self, pairs: list[str], start_ms: int, end_ms: int, datadir: Path) -> None:
        if self.is_running():
            return
        self.status = {"running": True, "scenarios": {}, "pairs": pairs}
        self._notify()
        self._thread = threading.Thread(
            target=self._run, args=(pairs, start_ms, end_ms, datadir), daemon=True
        )
        self._thread.start()

    def _notify(self) -> None:
        if self.on_update:
            self.on_update(dict(self.status))

    def _run(self, pairs: list[str], start_ms: int, end_ms: int, datadir: Path) -> None:
        timerange = ms_to_timerange(start_ms, end_ms)
        ft = self.root / ".venv" / "Scripts" / "ctbot.exe"
        if not ft.is_file():
            ft = Path("ctengine")

        for sc in BOT_SCENARIOS:
            sid = sc["id"]
            self.status["scenarios"][sid] = {"label": sc["label"], "state": "running"}
            self._notify()
            runtime_cfg = self.root / "simulation" / "data" / "runtime" / f"player_{sid}.json"
            patch_whitelist(self.root, sc["config"], pairs, runtime_cfg)
            out_dir = self.root / "simulation" / "results" / f"player_{sid}"
            out_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                str(ft),
                "backtesting",
                "--config",
                str(runtime_cfg),
                "--strategy",
                sc["strategy"],
                "--datadir",
                str(datadir),
                "--timerange",
                timerange,
                "--export",
                "trades",
                "--export-filename",
                str(out_dir / "trades.json"),
            ]
            try:
                proc = subprocess.run(cmd, cwd=str(self.root), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
                parsed = parse_backtest_pnl(proc.stdout + proc.stderr)
                self.status["scenarios"][sid] = {
                    "label": sc["label"],
                    "state": "done" if proc.returncode == 0 else "error",
                    "returncode": proc.returncode,
                    **parsed,
                }
            except Exception as exc:
                self.status["scenarios"][sid] = {
                    "label": sc["label"],
                    "state": "error",
                    "error": str(exc),
                }
            self._notify()

        self.status["running"] = False
        self._notify()
