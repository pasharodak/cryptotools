"""Bot session: backtest all scenarios, build per-pair instances with trades + SL/TP."""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .player_sync import (
    filter_trades_after_arm,
    filter_trades_by_pair_pnl_history,
    filter_trades_rolling_whitelist,
    pair_ever_whitelisted,
    resolve_armed_at,
    summarize_trades,
    whitelist_at_ms,
)
from .scan_replay import DEFAULT_SCAN_INTERVAL_MS, build_arm_schedules

SCENARIOS_PATH = "simulation/config/player_scenarios.json"


def load_bot_scenarios(root: Path) -> list[dict]:
    path = root / SCENARIOS_PATH
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return []


BOT_SCENARIOS: list[dict] = []


def ms_to_timerange(start_ms: int, end_ms: int) -> str:
    s = datetime.fromtimestamp(start_ms / 1000, tz=UTC).strftime("%Y%m%d")
    e = datetime.fromtimestamp(end_ms / 1000, tz=UTC).strftime("%Y%m%d")
    return f"{s}-{e}"


def load_player_profile(root: Path) -> dict[str, Any]:
    path = root / "simulation" / "config" / "player_profile.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def effective_stake_usdt(scenario: dict | None, profile: dict, fallback: float = 10) -> float:
    """Resolve stake: uniform profile override or per-scenario default."""
    if profile.get("stake_mode") == "uniform":
        return float(profile.get("stake_usdt", fallback))
    sc_stake = (scenario or {}).get("stake_usdt")
    if sc_stake is not None:
        return float(sc_stake)
    return float(profile.get("stake_usdt", fallback))


def patch_whitelist(root: Path, config_rel: str, pairs: list[str], out: Path, scenario: dict | None = None) -> dict:
    cfg = json.loads((root / config_rel).read_text(encoding="utf-8"))
    cfg["exchange"]["pair_whitelist"] = pairs
    frag = root / "simulation" / "config" / "sim_common_fragment.json"
    if frag.is_file():
        for key, val in json.loads(frag.read_text(encoding="utf-8")).items():
            if key == "exchange":
                cfg.setdefault("exchange", {}).update(val)
            else:
                cfg[key] = val
    profile_path = root / "simulation" / "config" / "player_profile.json"
    if profile_path.is_file():
        profile = load_player_profile(root)
        cfg["stake_amount"] = effective_stake_usdt(scenario, profile, float(cfg.get("stake_amount", 10)))
        wallet = float(profile.get("wallet_usdt", cfg.get("dry_run_wallet", 100)))
        stake = float(cfg["stake_amount"])
        mot = int((scenario or {}).get("max_open_trades") or profile.get("max_open_trades", cfg.get("max_open_trades", 1)))
        cfg["dry_run_wallet"] = max(wallet, stake * mot * 1.15)
        cfg["max_open_trades"] = mot
    out.parent.mkdir(parents=True, exist_ok=True)
    # Isolate ctengine sqlite per runtime config (parallel bots must not share one DB).
    cfg["db_url"] = f"sqlite:///{out.with_suffix('.sqlite').as_posix()}"
    out.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    return cfg


def parse_backtest_pnl(stdout: str) -> dict[str, Any]:
    m = re.search(r"Total profit %.*?(-?\d+\.?\d*)", stdout)
    trades = re.search(r"Total/Daily Avg Trades\s*\|\s*(\d+)", stdout)
    balance = re.search(r"Final balance\s*\|\s*(-?\d+\.?\d*)", stdout)
    return {
        "profit_pct": float(m.group(1)) if m else None,
        "trades": int(trades.group(1)) if trades else 0,
        "final_balance": float(balance.group(1)) if balance else None,
    }


def _parse_ts(val: Any) -> int:
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        return int(datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp() * 1000)
    return 0


def roi_target_price(open_rate: float, minimal_roi: dict, duration_min: float, is_short: bool) -> float | None:
    if not minimal_roi:
        return None
    roi = 0.0
    for mins_str in sorted(minimal_roi.keys(), key=lambda x: int(x), reverse=True):
        if duration_min >= int(mins_str):
            roi = float(minimal_roi[mins_str])
            break
    if roi <= 0 and "0" in minimal_roi:
        roi = float(minimal_roi["0"])
    if roi <= 0:
        return None
    if is_short:
        return open_rate * (1 - roi)
    return open_rate * (1 + roi)


def normalize_trade(raw: dict, minimal_roi: dict, grid_partial_tp: float = 0.004) -> dict[str, Any]:
    open_ms = int(raw.get("open_timestamp") or _parse_ts(raw.get("open_date")))
    close_ms = int(raw.get("close_timestamp") or _parse_ts(raw.get("close_date")))
    open_rate = float(raw["open_rate"])
    is_short = bool(raw.get("is_short"))
    duration_min = float(raw.get("trade_duration") or max(0, (close_ms - open_ms) / 60000))
    sl = float(raw.get("stop_loss_abs") or raw.get("initial_stop_loss_abs") or 0)
    tp = roi_target_price(open_rate, minimal_roi, duration_min, is_short)
    if tp is None and not is_short:
        tp = open_rate * (1 + grid_partial_tp)
    elif tp is None and is_short:
        tp = open_rate * (1 - grid_partial_tp)
    return {
        "open_ms": open_ms,
        "close_ms": close_ms,
        "open_rate": open_rate,
        "close_rate": float(raw.get("close_rate") or open_rate),
        "profit_abs": float(raw.get("profit_abs") or 0),
        "profit_ratio": float(raw.get("profit_ratio") or 0),
        "exit_reason": raw.get("exit_reason") or "",
        "is_short": is_short,
        "stop_loss": sl,
        "take_profit": tp,
        "duration_min": duration_min,
    }


def load_trades_from_zip(zip_path: Path, strategy: str) -> list[dict]:
    if not zip_path.is_file():
        return []
    with zipfile.ZipFile(zip_path) as zf:
        json_name = next((n for n in zf.namelist() if n.endswith(".json") and "_config" not in n), None)
        if not json_name:
            return []
        data = json.loads(zf.read(json_name))
    strat = data.get("strategy", {}).get(strategy, {})
    return strat.get("trades") or []


def filter_pairs_for_scenario(pairs: list[str], cfg: dict) -> list[str]:
    bl = set(cfg.get("exchange", {}).get("pair_blacklist") or [])
    return [p for p in pairs if p not in bl]


def _public_instance(inst: dict, *, trades: list[dict] | None = None, extra: dict | None = None) -> dict[str, Any]:
    """API-safe instance view (omit full trade history unless requested)."""
    out: dict[str, Any] = {
        "id": inst["id"],
        "scenario_id": inst["scenario_id"],
        "article": inst.get("article", ""),
        "label": inst["label"],
        "strategy": inst["strategy"],
        "settings": inst.get("settings"),
        "group": inst.get("group", "lite"),
        "pair": inst["pair"],
        "trading_active": inst.get("trading_active"),
        "armed_at_ms": inst.get("armed_at_ms"),
        "scan_armed_at_ms": inst.get("scan_armed_at_ms"),
        "activated_at_ms": inst.get("activated_at_ms"),
        "scan_interval_ms": inst.get("scan_interval_ms"),
        "config": inst.get("config"),
        "summary": inst.get("summary"),
        "raw_summary": inst.get("raw_summary"),
        "ml_gate": inst.get("ml_gate"),
    }
    if trades is not None:
        out["trades"] = trades
    if extra:
        out.update(extra)
    return out


def _trades_visible_at(inst: dict, current_ms: int, *, cap: int = 50) -> list[dict]:
    visible = [t for t in inst.get("trades") or [] if t["open_ms"] <= current_ms]
    return visible[-cap:] if len(visible) > cap else visible


def resolve_scanner_trades(
    raw_trades: list[dict],
    scan_arm: int | None,
    start_ms: int,
    *,
    pair: str | None = None,
    timeline: list[tuple[int, frozenset[str] | set[str]]] | None = None,
    rolling: bool = True,
    grace_scans: int = 2,
    loss_streak_gate: int = 0,
    loss_window_ms: int = 7 * 24 * 3600 * 1000,
    loss_min_cum_usdt: float = 0.0,
) -> tuple[bool, list[dict], int | None, int | None]:
    """Replay trades gated by scanner — rolling whitelist (prod-like) or sticky arm."""
    pair = pair or (raw_trades[0].get("pair") if raw_trades else None)
    if rolling and timeline:
        if pair and not pair_ever_whitelisted(pair, timeline):
            return False, [], None, None
        ptrades = filter_trades_rolling_whitelist(raw_trades, timeline, grace_scans=grace_scans)
        if loss_streak_gate > 0:
            ptrades = filter_trades_by_pair_pnl_history(
                ptrades,
                window_ms=loss_window_ms,
                min_closed=loss_streak_gate,
                min_cum_loss_usdt=loss_min_cum_usdt,
            )
        armed = next((ts for ts, ps in timeline if pair in ps), None) if pair else None
        return bool(ptrades or (pair and pair_ever_whitelisted(pair, timeline))), ptrades, armed, armed

    if scan_arm is None:
        return False, [], None, None
    armed = resolve_armed_at(scan_arm, raw_trades, start_ms, allow_trade_fallback=False)
    ptrades = filter_trades_after_arm(raw_trades, armed)
    if loss_streak_gate > 0:
        ptrades = filter_trades_by_pair_pnl_history(
            ptrades,
            window_ms=loss_window_ms,
            min_closed=loss_streak_gate,
            min_cum_loss_usdt=loss_min_cum_usdt,
        )
    return True, ptrades, armed, scan_arm


class BotSessionManager:
    """Runs backtests and exposes bot instances for replay overlay."""

    def __init__(self, root: Path, on_update: Callable[[dict], None] | None = None):
        self.root = root
        self.on_update = on_update
        self.scenarios = load_bot_scenarios(root)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.instances: list[dict[str, Any]] = []
        self.scan_events: list[dict[str, Any]] = []
        self.scan_schedule: dict[str, Any] = {}
        self._walkthrough: dict[str, Any] = {"done_pairs": {}, "active_pair": None}
        self._ml_gate = None
        self._scan_workers: int | None = None
        self.status: dict[str, Any] = {
            "running": False,
            "phase": "idle",
            "scenarios": {},
            "instances": [],
            "selected_id": None,
        }

    def is_running(self) -> bool:
        return bool(self.status.get("running"))

    def get_instances(self, pair: str | None = None, *, include_trades: bool = True) -> list[dict]:
        rows = self.instances
        if pair:
            rows = [i for i in rows if i["pair"] == pair]
        if include_trades:
            return list(rows)
        return [_public_instance(i) for i in rows]

    def select(self, instance_id: str | None) -> dict:
        self.status["selected_id"] = instance_id
        self._notify("selection")
        return self.snapshot()

    def snapshot(self, *, include_trades: bool = False) -> dict[str, Any]:
        wallet = 100.0
        profile_path = self.root / "simulation/config/player_profile.json"
        if profile_path.is_file():
            wallet = float(json.loads(profile_path.read_text(encoding="utf-8")).get("wallet_usdt", 100))
        total_raw = sum(
            float(i.get("raw_summary", {}).get("profit_abs") or 0)
            for i in self.instances
        )
        total_gated = sum(
            float(i.get("summary", {}).get("profit_abs") or 0)
            for i in self.instances
        )
        instances_out = (
            self.instances
            if include_trades
            else [_public_instance(i) for i in self.instances]
        )
        return {
            **self.status,
            "instances": instances_out,
            "scan_events": self.scan_events,
            "scan_interval_ms": self.scan_schedule.get("scan_interval_ms", DEFAULT_SCAN_INTERVAL_MS),
            "count": len(self.instances),
            "portfolio": {
                "wallet_usdt": wallet,
                "pnl_usdt": round(total_gated, 4),
                "pnl_gated_usdt": round(total_gated, 4),
                "pnl_pct": round(total_gated / wallet * 100, 2) if wallet else 0,
            },
        }

    def runtime_for_pair(self, pair: str, current_ms: int) -> dict[str, Any]:
        """Bot states at simulated time current_ms (never wall clock)."""
        bots = []
        active_trade = None
        selected = self.status.get("selected_id")
        for inst in self.instances:
            if inst["pair"] != pair:
                continue
            rt = self._instance_at_time(inst, current_ms)
            bots.append(rt)
            if rt.get("current_trade") and active_trade is None:
                active_trade = rt["current_trade"]
        highlight = None
        if selected:
            inst = next((i for i in self.instances if i["id"] == selected), None)
            if inst and inst["pair"] == pair:
                highlight = self._highlight_for_instance(inst)
        return {"bots": bots, "active_trade": active_trade, "highlight": highlight, "sim_ms": current_ms}

    def runtime_all(self, current_ms: int) -> list[dict[str, Any]]:
        return [self._instance_at_time(inst, current_ms) for inst in self.instances]

    def events_between(self, prev_ms: int, cur_ms: int) -> list[dict]:
        return [e for e in self.scan_events if prev_ms < e["ms"] <= cur_ms]

    def _instance_at_time(self, inst: dict, current_ms: int) -> dict[str, Any]:
        if not inst.get("trading_active", True):
            return _public_instance(
                inst,
                trades=[],
                extra={
                    "visible": True,
                    "phase": "skipped",
                    "completed_trades": 0,
                    "current_trade": None,
                    "sim_ms": current_ms,
                },
            )
        armed = inst.get("armed_at_ms")
        activated = inst.get("activated_at_ms") or armed
        current_trade = None
        completed = 0
        for tr in inst["trades"]:
            if tr["close_ms"] <= current_ms:
                completed += 1
            elif tr["open_ms"] <= current_ms <= tr["close_ms"]:
                dur = max(0, (current_ms - tr["open_ms"]) / 60000)
                tp = roi_target_price(
                    tr["open_rate"],
                    inst["config"].get("minimal_roi") or {},
                    dur,
                    tr["is_short"],
                )
                if tp is None:
                    tp = tr.get("take_profit")
                current_trade = {
                    "bot_id": inst["id"],
                    "open_ms": tr["open_ms"],
                    "close_ms": tr["close_ms"],
                    "open_rate": tr["open_rate"],
                    "stop_loss": tr["stop_loss"],
                    "take_profit": tp,
                    "is_short": tr["is_short"],
                }

        is_armed = armed is not None and current_ms >= armed
        in_trade = current_trade is not None

        if not is_armed and not in_trade:
            return _public_instance(
                inst,
                trades=_trades_visible_at(inst, current_ms),
                extra={
                    "visible": True,
                    "phase": "waiting",
                    "completed_trades": completed,
                    "current_trade": None,
                    "next_scan_ms": self._next_scan_ms(current_ms),
                    "sim_ms": current_ms,
                },
            )

        if in_trade:
            phase = "in_trade"
        elif inst["trades"] and all(t["close_ms"] <= current_ms for t in inst["trades"]):
            phase = "done"
        else:
            phase = "active"

        return _public_instance(
            inst,
            trades=_trades_visible_at(inst, current_ms),
            extra={
                "visible": True,
                "phase": phase,
                "completed_trades": completed,
                "current_trade": current_trade,
                "sim_ms": current_ms,
            },
        )

    def _next_scan_ms(self, current_ms: int) -> int:
        interval = int(self.scan_schedule.get("scan_interval_ms") or DEFAULT_SCAN_INTERVAL_MS)
        rem = current_ms % interval
        return current_ms + (interval - rem if rem else interval)

    def scans_due_until(self, from_ms: int, to_ms: int) -> list[int]:
        times = self.scan_schedule.get("scan_times") or []
        return [t for t in times if from_ms < t <= to_ms]

    def events_until(self, ms: int) -> list[dict]:
        return [e for e in self.scan_events if e["ms"] <= ms]

    def _highlight_for_instance(self, inst: dict) -> dict[str, Any]:
        return {
            "bot_id": inst["id"],
            "label": inst["label"],
            "pair": inst["pair"],
            "trades": inst["trades"],
            "summary": inst["summary"],
        }

    def enabled_scenario_ids(self) -> list[str]:
        return [s["id"] for s in self.scenarios if s.get("enabled") is not False]

    def trade_events_between(
        self, scenario_id: str, prev_ms: int, cur_ms: int
    ) -> list[dict[str, Any]]:
        """Trade open/close events for one strategy within a sim-time step."""
        done_pairs = self._walkthrough.get("done_pairs", {}).get(scenario_id) or set()
        active_pair = self._walkthrough.get("active_pair")
        out: list[dict[str, Any]] = []
        for inst in self.instances:
            if inst["scenario_id"] != scenario_id:
                continue
            pair = inst["pair"]
            if pair in done_pairs:
                continue
            if active_pair and pair != active_pair:
                continue
            for tr in inst.get("trades") or []:
                base = {
                    "inst_id": inst["id"],
                    "scenario_id": scenario_id,
                    "pair": inst["pair"],
                    "label": inst["label"],
                }
                if prev_ms < tr["open_ms"] <= cur_ms:
                    out.append({**base, "event": "open", "trade": tr})
                if prev_ms < tr["close_ms"] <= cur_ms:
                    out.append({**base, "event": "close", "trade": tr})
        return out

    def runtime_for_scenario(self, scenario_id: str, current_ms: int) -> list[dict[str, Any]]:
        return [
            self._instance_at_time(inst, current_ms)
            for inst in self.instances
            if inst["scenario_id"] == scenario_id
        ]

    def visible_trades_for_scenario(self, scenario_id: str, sim_ms: int) -> list[dict[str, Any]]:
        """Trades for live card sync — accumulates finished pairs during walkthrough."""
        done_pairs = self._walkthrough.get("done_pairs", {}).get(scenario_id) or set()
        active_pair = self._walkthrough.get("active_pair")
        rows: list[dict[str, Any]] = []
        for inst in self.instances:
            if inst["scenario_id"] != scenario_id:
                continue
            pair = inst["pair"]
            for tr in inst.get("trades") or []:
                if pair in done_pairs:
                    include = True
                    closed = True
                elif pair == active_pair:
                    if tr["open_ms"] > sim_ms:
                        continue
                    include = True
                    closed = tr["close_ms"] <= sim_ms
                else:
                    continue
                if not include:
                    continue
                rows.append(
                    {
                        "inst_id": inst["id"],
                        "pair": pair,
                        "label": inst["label"],
                        "closed": closed,
                        **tr,
                    }
                )
        rows.sort(key=lambda x: x["open_ms"])
        return rows

    def begin_strategy_walkthrough(self, scenario_id: str) -> None:
        self._walkthrough.setdefault("done_pairs", {})[scenario_id] = set()
        self._walkthrough["active_pair"] = None

    def begin_pair_walkthrough(self, scenario_id: str, pair: str) -> None:
        self._walkthrough.setdefault("done_pairs", {}).setdefault(scenario_id, set())
        self._walkthrough["active_pair"] = pair

    def finish_pair_walkthrough(self, scenario_id: str, pair: str) -> None:
        self._walkthrough.setdefault("done_pairs", {}).setdefault(scenario_id, set()).add(pair)
        if self._walkthrough.get("active_pair") == pair:
            self._walkthrough["active_pair"] = None

    def trades_payload_for_scenario(self, scenario_id: str) -> list[dict[str, Any]]:
        """All scanner-gated trades for one bot (sorted by open time)."""
        rows: list[dict[str, Any]] = []
        with self._lock:
            instances = [i for i in self.instances if i["scenario_id"] == scenario_id]
        for inst in instances:
            for tr in inst.get("trades") or []:
                rows.append(
                    {
                        "inst_id": inst["id"],
                        "pair": inst["pair"],
                        "label": inst["label"],
                        "closed": True,
                        **tr,
                    }
                )
        rows.sort(key=lambda x: x["open_ms"])
        return rows

    def mark_strategy_walkthrough_done(self, scenario_id: str) -> None:
        pairs = {
            inst["pair"]
            for inst in self.instances
            if inst.get("scenario_id") == scenario_id
        }
        self._walkthrough.setdefault("done_pairs", {})[scenario_id] = pairs
        if self._walkthrough.get("active_pair") and scenario_id:
            self._walkthrough["active_pair"] = None

    def enabled_signature(self) -> tuple[str, ...]:
        return tuple(sorted(s["id"] for s in self.scenarios if s.get("enabled") is not False))

    def mark_stale(self) -> None:
        """Invalidate cached backtest (e.g. after scenario toggle)."""
        if self.status.get("phase") == "ready":
            self.status["phase"] = "stale"

    def is_ready_for(
        self,
        start_ms: int,
        end_ms: int,
        *,
        sim_pool: list[str] | None = None,
    ) -> bool:
        if sim_pool is not None and set(self.status.get("sim_pool") or []) != set(sim_pool):
            return False
        if self.status.get("enabled_sig") != self.enabled_signature():
            return False
        return (
            self.status.get("phase") == "ready"
            and bool(self.instances)
            and self.status.get("range_ms") == [start_ms, end_ms]
        )

    def reset(self) -> None:
        """Clear backtest instances and return to idle."""
        self.instances = []
        self.scan_events = []
        self.scan_schedule = {}
        self._walkthrough = {"done_pairs": {}, "active_pair": None}
        self.status = {
            "running": False,
            "phase": "idle",
            "scenarios": {},
            "instances": [],
            "selected_id": None,
        }

    def _resolve_sim_pool(
        self, pairs: list[str], start_ms: int, datadir: Path
    ) -> tuple[list[str], dict[str, list[str]] | None, list[str]]:
        assignments: dict[str, list[str]] | None = None
        pool: list[str] = []
        profile_path = self.root / "simulation" / "config" / "player_profile.json"
        if profile_path.is_file():
            from simulation.scripts.select_player_pairs import assign_pairs, pool_pairs, save_assignment

            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            sel = profile.get("pair_selection", {})
            full_pool = pool_pairs(self.root)
            if pairs:
                pool = [p for p in full_pool if p in pairs]
                if not pool:
                    pool = list(pairs)
            else:
                pool = full_pool
            if sel.get("mode") == "probe_assign":
                result = assign_pairs(
                    self.root, datadir, start_ms, pool=pool, profile=profile, use_cache=True
                )
                pairs = result.pairs
                assignments = result.assignments
                ranking = result.ranking
                save_assignment(
                    result,
                    self.root / "simulation/results/selected_player_pairs.json",
                    start_ms,
                    pool,
                )
                self.status["pairs"] = pairs
                self.status["assignments"] = assignments
                self.status["ranking"] = ranking
            else:
                pairs = pool
                self.status["pair_selection_mode"] = sel.get("mode", "scanner")
        else:
            pool = list(pairs) if pairs else []
        display_pool = pool or list(pairs)
        return pairs, assignments, display_pool

    def _ctengine_backtest(
        self,
        sc: dict,
        bt_pairs: list[str],
        timerange: str,
        datadir: Path,
        *,
        prefix: str | None = None,
    ) -> tuple[list[dict], dict, float, dict, dict[str, Any]]:
        sid = sc["id"]
        try:
            from simulation.paths import VENV_CTBOT, VENV_PYTHON

            if VENV_PYTHON.is_file():
                ft_cmd = [str(VENV_PYTHON), "-m", "ctengine"]
            elif VENV_CTBOT.is_file():
                ft_cmd = [str(VENV_CTBOT)]
            else:
                ft_cmd = [str(self.root / ".venv" / "Scripts" / "ctbot.exe")]
        except ImportError:
            ft_cmd = [str(self.root / ".venv" / "Scripts" / "ctbot.exe")]
        if ft_cmd[0] == "ctengine" or (len(ft_cmd) == 1 and not Path(ft_cmd[0]).is_file()):
            ft_cmd = ["ctengine"]
        # Isolated dir per bot — parallel прогон не должен писать в один zip/.last_result
        bt_dir = self.root / "simulation" / "results" / "player_backtests" / sid
        bt_dir.mkdir(parents=True, exist_ok=True)
        safe_pair = bt_pairs[0].replace("/", "_").replace(":", "_") if len(bt_pairs) == 1 else ""
        runtime_cfg = self.root / "simulation" / "data" / "runtime" / (
            f"player_{sid}_{safe_pair}.json" if safe_pair else f"player_{sid}.json"
        )
        cfg = patch_whitelist(self.root, sc["config"], bt_pairs, runtime_cfg, scenario=sc)
        export_prefix = prefix or (
            f"player_{sid}_{safe_pair}_{timerange}" if safe_pair else f"player_{sid}_{timerange}"
        )
        cmd = [
            *ft_cmd,
            "backtesting",
            "--config",
            str(runtime_cfg),
            "--strategy",
            sc["strategy"],
        ]
        if sc.get("strategy_path"):
            cmd.extend(["--strategy-path", str(self.root / sc["strategy_path"])])
        cmd.extend(
            [
                "--datadir",
                str(datadir),
                "--timerange",
                timerange,
                "--export",
                "trades",
                "--export-filename",
                export_prefix,
                "--backtest-directory",
                str(bt_dir),
                "--cache",
                "none",
            ]
        )
        started_at = time.time()
        proc = subprocess.run(
            cmd,
            cwd=str(self.root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(600, 90 * len(bt_pairs)),
        )
        parsed = parse_backtest_pnl((proc.stdout or "") + (proc.stderr or ""))
        parsed["returncode"] = proc.returncode
        if proc.returncode != 0:
            raise RuntimeError(
                f"backtest failed for {sid} rc={proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '')[-800:]}"
            )
        # Prefer .last_result.json written by this successful run.
        zip_path = bt_dir / f"{export_prefix}.zip"
        last_path = bt_dir / ".last_result.json"
        if last_path.is_file():
            try:
                latest = json.loads(last_path.read_text(encoding="utf-8")).get("latest_backtest") or ""
                if latest and (bt_dir / latest).is_file():
                    zip_path = bt_dir / latest
            except json.JSONDecodeError:
                pass
        if not zip_path.is_file():
            zips = sorted(bt_dir.glob("backtest-result-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
            if zips:
                zip_path = zips[0]
        if not zip_path.is_file():
            raise FileNotFoundError(f"backtest zip not found for {sid} in {bt_dir}")
        # Guard against stale zip from a previous run if last_result was not refreshed.
        if zip_path.stat().st_mtime < (started_at - 1):
            raise FileNotFoundError(
                f"stale backtest zip for {sid}: {zip_path.name} mtime before run start"
            )
        raw_trades = load_trades_from_zip(zip_path, sc["strategy"])
        minimal_roi = cfg.get("minimal_roi") or {}
        stoploss = float(cfg.get("stoploss", -0.05))
        return raw_trades, minimal_roi, stoploss, cfg, parsed

    def _make_instance(
        self,
        sc: dict,
        pair: str,
        raw_trades: list[dict],
        minimal_roi: dict,
        stoploss: float,
        cfg: dict,
        start_ms: int,
    ) -> dict[str, Any]:
        sid = sc["id"]
        raw_trades = sorted(raw_trades, key=lambda t: t["open_ms"])
        arms = (
            self.scan_schedule.get("grid_arms", {})
            if sc.get("scan_type") == "grid"
            else self.scan_schedule.get("strategy_arms", {})
        )
        rolling = self.scan_schedule.get("rolling_whitelist", True)
        timeline_key = "grid_timeline" if sc.get("scan_type") == "grid" else "strategy_timeline"
        timeline = self.scan_schedule.get(timeline_key) or []
        grace_scans = int(self.scan_schedule.get("whitelist_grace_scans", 2))
        loss_streak = int(self.scan_schedule.get("trade_skip_loss_streak", 0))
        loss_window = int(self.scan_schedule.get("trade_loss_window_ms", 7 * 24 * 3600 * 1000))
        loss_min_cum = float(self.scan_schedule.get("trade_skip_min_cum_loss_usdt", 0.0))
        scan_arm = arms.get(pair)
        trading_active, ptrades, armed, scan_arm = resolve_scanner_trades(
            raw_trades,
            scan_arm,
            start_ms,
            pair=pair,
            timeline=timeline,
            rolling=rolling,
            grace_scans=grace_scans,
            loss_streak_gate=loss_streak if sc.get("scan_type") == "grid" else 0,
            loss_window_ms=loss_window,
            loss_min_cum_usdt=loss_min_cum if sc.get("scan_type") == "grid" else 0.0,
        )
        inst_config = {
            "stoploss": stoploss,
            "minimal_roi": minimal_roi,
            "stake": cfg.get("stake_amount"),
            "timeframe": cfg.get("timeframe", "5m"),
        }
        ml_skipped: list[dict] = []
        ml_gate_stats: dict[str, Any] = {"enabled": False, "kept": len(ptrades), "skipped": 0}
        scanner_trades = list(ptrades)
        gate = self._get_ml_gate()
        ml_kept = scanner_trades
        if gate and ptrades and gate.status().get("ready"):
            ml_kept, ml_skipped, eval_stats = gate.evaluate_trades(
                ptrades, sc, pair, inst_config, armed
            )
            ml_gate_stats = {**eval_stats, "enabled": gate.enabled}
            if gate.enabled:
                ptrades = ml_kept
            else:
                ptrades = sorted(ml_kept + ml_skipped, key=lambda t: t["open_ms"])
        scanner_summary = summarize_trades(scanner_trades)
        ml_summary = summarize_trades(ml_kept)
        raw_summary = summarize_trades(raw_trades)
        summary = summarize_trades(ptrades)
        return {
            "id": f"{sid}:{pair}",
            "scenario_id": sid,
            "article": sc.get("article", ""),
            "label": sc["label"],
            "strategy": sc["strategy"],
            "settings": sc["settings"],
            "group": sc.get("group", "lite"),
            "pair": pair,
            "trading_active": trading_active,
            "armed_at_ms": armed,
            "scan_armed_at_ms": scan_arm,
            "activated_at_ms": armed,
            "scan_interval_ms": self.scan_schedule.get("scan_interval_ms", DEFAULT_SCAN_INTERVAL_MS),
            "config": {
                "stoploss": stoploss,
                "minimal_roi": minimal_roi,
                "stake": cfg.get("stake_amount"),
                "stake_currency": cfg.get("stake_currency", "USDT"),
                "max_open_trades": sc.get("max_open_trades") or cfg.get("max_open_trades", 1),
                "wallet": sc.get("wallet_usdt") or cfg.get("dry_run_wallet", 100),
                "timeframe": cfg.get("timeframe", "5m"),
            },
            "raw_trades": raw_trades,
            "scanner_trades": scanner_trades,
            "trades": ptrades,
            "ml_skipped_trades": ml_skipped,
            "ml_gate": ml_gate_stats,
            "scanner_summary": scanner_summary,
            "ml_summary": ml_summary,
            "raw_summary": raw_summary,
            "summary": summary,
        }

    def _get_ml_gate(self):
        if self._ml_gate is None:
            from simulation.ml.trade_gate import MlEntryGate

            self._ml_gate = MlEntryGate(self.root)
        return self._ml_gate

    def ml_gate_status(self) -> dict[str, Any]:
        from simulation.ml.trade_gate import MlEntryGate

        gate = MlEntryGate(self.root)
        return gate.status()

    def set_ml_gate_enabled(self, enabled: bool) -> dict[str, Any]:
        from simulation.ml.trade_gate import MlEntryGate

        gate = MlEntryGate(self.root)
        gate.set_enabled(enabled)
        self._ml_gate = gate
        return gate.status()

    def init_live_session(
        self,
        pairs: list[str],
        start_ms: int,
        end_ms: int,
        datadir: Path,
        *,
        scan_interval_ms: int | None = None,
        pool_mode: str = "profile",
        scan_workers: int | None = None,
    ) -> None:
        """Scan-only init for sequential bot×pair live walkthrough (no bulk backtest).

        pool_mode:
          profile — intersect with player_pair_pool / probe_assign (default player UI)
          direct  — use the passed pairs list as-is (prod200 / full export)
        """
        self.instances = []
        self.scan_events = []
        self.scan_schedule = {}
        self._walkthrough = {"done_pairs": {}, "active_pair": None}
        self.status = {
            "running": True,
            "phase": "simulating",
            "scenarios": {},
            "instances": [],
            "selected_id": None,
            "pairs": pairs,
            "range_ms": [start_ms, end_ms],
            "pool_mode": pool_mode,
        }
        self._notify("session_ready")
        self._scan_workers = scan_workers
        if pool_mode == "direct":
            display_pool = list(pairs)
        else:
            pairs, _assignments, display_pool = self._resolve_sim_pool(pairs, start_ms, datadir)
        self.status["pool"] = display_pool
        self.status["sim_pool"] = display_pool
        from .datastore import HistoricalDatastore

        ds = HistoricalDatastore(datadir, exchange="bybit")
        grid_blacklist: set[str] = set()
        for sc in self.scenarios:
            if sc.get("enabled") is False or sc.get("scan_type") != "grid":
                continue
            runtime_cfg = self.root / "simulation" / "data" / "runtime" / f"player_{sc['id']}.json"
            cfg = patch_whitelist(self.root, sc["config"], display_pool, runtime_cfg, scenario=sc)
            grid_blacklist = set(cfg.get("exchange", {}).get("pair_blacklist") or [])
            break
        self.status["phase"] = "scanning"
        self._notify("scanning")
        self.scan_schedule = build_arm_schedules(
            ds,
            display_pool,
            start_ms,
            end_ms,
            self.root,
            grid_blacklist,
            grid_trades_by_pair={},
            scan_interval_ms=scan_interval_ms,
            workers=scan_workers,
        )
        self.scan_events = self.scan_schedule.get("events") or []
        self.status["phase"] = "simulating"
        self._notify("scan_ready")

    def finish_live_session(self, *, persist_trades: bool = True, source: str = "sim") -> dict[str, Any] | None:
        self.status["running"] = False
        self.status["phase"] = "ready"
        self.status["enabled_sig"] = self.enabled_signature()
        archive_result = None
        if persist_trades and self.instances:
            from .trade_archive import persist_session

            archive_result = persist_session(self, self.root, source=source)
            self.status["trade_archive"] = {
                "run_id": archive_result.get("run_id"),
                "trades_saved": archive_result.get("trades_saved"),
                "trades_skipped_duplicate": archive_result.get("trades_skipped_duplicate"),
                "net_usdt": archive_result.get("net_usdt"),
            }
        self._notify("loaded")
        return archive_result

    def pairs_for_scenario(self, sid: str, pool: list[str]) -> list[str]:
        sc = next((s for s in self.scenarios if s["id"] == sid), None)
        if not sc:
            return []
        runtime_cfg = self.root / "simulation" / "data" / "runtime" / f"player_{sid}.json"
        cfg = patch_whitelist(self.root, sc["config"], pool, runtime_cfg, scenario=sc)
        return filter_pairs_for_scenario(pool, cfg)

    def load_scenario_instances(
        self, sid: str, pool: list[str], start_ms: int, end_ms: int, datadir: Path
    ) -> list[dict[str, Any]]:
        """One backtest for all eligible pairs of a scenario (on bot turn)."""
        sc = next((s for s in self.scenarios if s["id"] == sid), None)
        if not sc:
            raise ValueError(f"unknown scenario {sid}")
        eligible = self.pairs_for_scenario(sid, pool)
        if not eligible:
            return []
        timerange = ms_to_timerange(start_ms, end_ms)
        self.status["scenarios"][sid] = {
            "label": sc["label"],
            "state": "running",
            "pairs": len(eligible),
        }
        self._notify("strategy_loading")
        last_err: Exception | None = None
        raw_trades: list[dict] = []
        minimal_roi: dict = {}
        stoploss = -0.05
        cfg: dict = {}
        parsed: dict[str, Any] = {}
        remaining = list(eligible)
        for attempt in range(4):
            if not remaining:
                break
            try:
                raw_trades, minimal_roi, stoploss, cfg, parsed = self._ctengine_backtest(
                    sc, remaining, timerange, datadir
                )
                last_err = None
                eligible = remaining
                break
            except Exception as exc:
                last_err = exc
                msg = str(exc)
                # Drop pairs ctengine rejects for missing leverage tiers, then retry.
                m = re.search(
                    r"Pairs\s+(.+?)\s+got no leverage tiers",
                    msg,
                    flags=re.S,
                )
                if m:
                    bad = {
                        p.strip()
                        for p in m.group(1).replace("\n", " ").split(",")
                        if p.strip()
                    }
                    remaining = [p for p in remaining if p not in bad]
                    print(
                        f"  [{sid}] drop {len(bad)} no-leverage pairs, retry n={len(remaining)}",
                        flush=True,
                    )
                    continue
                if attempt == 0:
                    import time

                    time.sleep(0.8)
                    continue
                break
        if last_err is not None:
            raise last_err
        trades_by_pair: dict[str, list[dict]] = {p: [] for p in eligible}
        for raw in raw_trades:
            pair = raw.get("pair")
            if pair in trades_by_pair:
                nt = normalize_trade(raw, minimal_roi)
                nt["pair"] = pair
                trades_by_pair[pair].append(nt)
        created: list[dict[str, Any]] = []
        with self._lock:
            self.instances = [i for i in self.instances if i["scenario_id"] != sid]
            for pair in eligible:
                inst = self._make_instance(
                    sc, pair, trades_by_pair[pair], minimal_roi, stoploss, cfg, start_ms
                )
                self.instances.append(inst)
                created.append(inst)
        self.status["scenarios"][sid] = {
            "label": sc["label"],
            "state": "done" if parsed.get("returncode") == 0 else "error",
            "pairs": len(eligible),
            "trades": sum(len(trades_by_pair[p]) for p in eligible),
            **{k: v for k, v in parsed.items() if k != "returncode"},
        }
        self._notify("strategy_loaded")
        return created

    def load_pair_instance(
        self, sid: str, pair: str, start_ms: int, end_ms: int, datadir: Path
    ) -> dict[str, Any]:
        """Backtest one scenario×pair and append instance (on-demand during live walkthrough)."""
        sc = next((s for s in self.scenarios if s["id"] == sid), None)
        if not sc:
            raise ValueError(f"unknown scenario {sid}")
        timerange = ms_to_timerange(start_ms, end_ms)
        self.status["scenarios"][sid] = {"label": sc["label"], "state": "running", "pair": pair}
        self._notify("pair_loading")
        raw_trades, minimal_roi, stoploss, cfg, parsed = self._ctengine_backtest(
            sc, [pair], timerange, datadir
        )
        trades_for_pair: list[dict] = []
        for raw in raw_trades:
            if raw.get("pair") == pair:
                nt = normalize_trade(raw, minimal_roi)
                nt["pair"] = pair
                trades_for_pair.append(nt)
        inst = self._make_instance(sc, pair, trades_for_pair, minimal_roi, stoploss, cfg, start_ms)
        self.instances = [i for i in self.instances if i["id"] != inst["id"]]
        self.instances.append(inst)
        self.status["scenarios"][sid] = {
            "label": sc["label"],
            "state": "done" if parsed.get("returncode") == 0 else "error",
            "pair": pair,
            "trades": len(trades_for_pair),
            **{k: v for k, v in parsed.items() if k != "returncode"},
        }
        self._notify("pair_loaded")
        return inst

    def prepare(self, pairs: list[str], start_ms: int, end_ms: int, datadir: Path) -> None:
        """Blocking: backtest + historical scan (call from worker thread)."""
        if self.is_running():
            raise RuntimeError("bot session already running")
        self.instances = []
        self.scan_events = []
        self.scan_schedule = {}
        self._walkthrough = {"done_pairs": {}, "active_pair": None}
        self.status = {
            "running": True,
            "phase": "backtesting",
            "scenarios": {},
            "instances": [],
            "selected_id": None,
            "pairs": pairs,
            "range_ms": [start_ms, end_ms],
        }
        self._notify("started")
        try:
            assignments: dict[str, list[str]] | None = None
            pool: list[str] = []
            profile_path = self.root / "simulation" / "config" / "player_profile.json"
            if profile_path.is_file():
                from simulation.scripts.select_player_pairs import assign_pairs, pool_pairs, save_assignment

                profile = json.loads(profile_path.read_text(encoding="utf-8"))
                sel = profile.get("pair_selection", {})
                full_pool = pool_pairs(self.root)
                if pairs:
                    pool = [p for p in full_pool if p in pairs]
                    if not pool:
                        pool = list(pairs)
                else:
                    pool = full_pool
                self.status["pool"] = pool
                self.status["sim_pool"] = pool
                if sel.get("mode") == "probe_assign":
                    result = assign_pairs(
                        self.root, datadir, start_ms, pool=pool, profile=profile, use_cache=True
                    )
                    pairs = result.pairs
                    assignments = result.assignments
                    ranking = result.ranking
                    save_assignment(
                        result,
                        self.root / "simulation/results/selected_player_pairs.json",
                        start_ms,
                        pool,
                    )
                    self.status["pairs"] = pairs
                    self.status["assignments"] = assignments
                    self.status["ranking"] = ranking
                else:
                    pairs = pool
                    self.status["pair_selection_mode"] = sel.get("mode", "scanner")
            else:
                pool = list(pairs) if pairs else []
                self.status["sim_pool"] = pool
            self._run(pairs, start_ms, end_ms, datadir, assignments=assignments, pool=pool)
        except Exception as exc:
            self.status["running"] = False
            self.status["phase"] = "error"
            self.status["error"] = str(exc)
            self._notify("error")
            raise

    def run_all(self, pairs: list[str], start_ms: int, end_ms: int, datadir: Path) -> None:
        if self.is_running():
            return
        self._thread = threading.Thread(
            target=self.prepare, args=(pairs, start_ms, end_ms, datadir), daemon=True
        )
        self._thread.start()

    def _notify(self, event: str = "update") -> None:
        payload = {**self.snapshot(include_trades=False), "event": event}
        if self.on_update:
            self.on_update(payload)

    def _run(
        self,
        pairs: list[str],
        start_ms: int,
        end_ms: int,
        datadir: Path,
        assignments: dict[str, list[str]] | None = None,
        pool: list[str] | None = None,
    ) -> None:
        timerange = ms_to_timerange(start_ms, end_ms)
        ft = self.root / ".venv" / "Scripts" / "ctbot.exe"
        if not ft.is_file():
            ft = Path("ctengine")
        bt_dir = self.root / "simulation" / "results" / "player_backtests"
        bt_dir.mkdir(parents=True, exist_ok=True)
        trades_data: dict[str, dict[str, Any]] = {}
        grid_blacklist: set[str] = set()
        display_pool = pool or pairs

        for sc in self.scenarios:
            if sc.get("enabled") is False:
                continue
            sid = sc["id"]
            bt_pairs = display_pool
            self.status["scenarios"][sid] = {"label": sc["label"], "state": "running"}
            self._notify()
            runtime_cfg = self.root / "simulation" / "data" / "runtime" / f"player_{sid}.json"
            cfg = patch_whitelist(self.root, sc["config"], bt_pairs, runtime_cfg, scenario=sc)
            eligible = filter_pairs_for_scenario(bt_pairs, cfg)
            if sc.get("scan_type") == "grid":
                grid_blacklist = set(cfg.get("exchange", {}).get("pair_blacklist") or [])
            prefix = f"player_{sid}"
            cmd = [
                str(ft),
                "backtesting",
                "--config",
                str(runtime_cfg),
                "--strategy",
                sc["strategy"],
            ]
            if sc.get("strategy_path"):
                cmd.extend(["--strategy-path", str(self.root / sc["strategy_path"])])
            cmd.extend(
                [
                "--datadir",
                str(datadir),
                "--timerange",
                timerange,
                "--export",
                "trades",
                "--export-filename",
                prefix,
                "--backtest-directory",
                str(bt_dir),
                "--cache",
                "none",
                ]
            )
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(self.root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=max(600, 90 * len(bt_pairs)),
                )
                parsed = parse_backtest_pnl((proc.stdout or "") + (proc.stderr or ""))
                zip_path = bt_dir / f"{prefix}.zip"
                last = bt_dir / ".last_result.json"
                if last.is_file():
                    latest = json.loads(last.read_text(encoding="utf-8")).get("latest_backtest", "")
                    if latest:
                        zip_path = bt_dir / latest
                raw_trades = load_trades_from_zip(zip_path, sc["strategy"])
                minimal_roi = cfg.get("minimal_roi") or {}
                stoploss = float(cfg.get("stoploss", -0.05))
                trades_by_pair: dict[str, list] = {p: [] for p in eligible}
                for raw in raw_trades:
                    pair = raw.get("pair")
                    if pair in trades_by_pair:
                        nt = normalize_trade(raw, minimal_roi)
                        nt["pair"] = pair
                        trades_by_pair[pair].append(nt)

                trades_data[sid] = {
                    "eligible": eligible,
                    "trades_by_pair": trades_by_pair,
                    "minimal_roi": minimal_roi,
                    "stoploss": stoploss,
                    "cfg": cfg,
                    "sc": sc,
                    "parsed": parsed,
                    "returncode": proc.returncode,
                }
                self.status["scenarios"][sid] = {
                    "label": sc["label"],
                    "state": "done" if proc.returncode == 0 else "error",
                    "returncode": proc.returncode,
                    "pairs": eligible,
                    **parsed,
                }
            except Exception as exc:
                self.status["scenarios"][sid] = {
                    "label": sc["label"],
                    "state": "error",
                    "error": str(exc),
                }
            self._notify()

        # Historical scanner (30 min, same as live timers)
        self.status["phase"] = "scanning"
        self._notify("scanning")
        from .datastore import HistoricalDatastore

        ds = HistoricalDatastore(datadir, exchange="bybit")
        grid_trades_by_pair: dict[str, list] = {}
        for sc in self.scenarios:
            if sc.get("enabled") is False or sc.get("scan_type") != "grid":
                continue
            td = trades_data.get(sc["id"])
            if td:
                grid_trades_by_pair = td["trades_by_pair"]
                break
        self.scan_schedule = build_arm_schedules(
            ds,
            display_pool,
            start_ms,
            end_ms,
            self.root,
            grid_blacklist,
            grid_trades_by_pair=grid_trades_by_pair,
            workers=self._scan_workers,
        )
        self.scan_events = self.scan_schedule.get("events") or []

        all_instances = []
        for sc in self.scenarios:
            if sc.get("enabled") is False:
                continue
            sid = sc["id"]
            td = trades_data.get(sid)
            if not td:
                continue
            for pair in td["eligible"]:
                raw_trades = sorted(td["trades_by_pair"].get(pair, []), key=lambda t: t["open_ms"])
                all_instances.append(
                    self._make_instance(
                        sc,
                        pair,
                        raw_trades,
                        td["minimal_roi"],
                        td["stoploss"],
                        td["cfg"],
                        start_ms,
                    )
                )

        self.instances = all_instances
        self.status["running"] = False
        self.status["phase"] = "ready"
        self.status["enabled_sig"] = self.enabled_signature()
        self._notify("loaded")

    def first_scan_both_armed_ms(self) -> int | None:
        scan_times = self.scan_schedule.get("scan_times") or []
        grid_arms = self.scan_schedule.get("grid_arms") or {}
        strat_arms = self.scan_schedule.get("strategy_arms") or {}
        has_grid = any(s.get("scan_type") == "grid" for s in self.scenarios)
        has_strat = any(s.get("scan_type") != "grid" for s in self.scenarios)
        for t in scan_times:
            g = any(v is not None and v <= t for v in grid_arms.values()) if has_grid else True
            s = any(v is not None and v <= t for v in strat_arms.values()) if has_strat else True
            if g and s:
                return t
        armed = [ms for ms in list(grid_arms.values()) + list(strat_arms.values()) if ms]
        return min(armed) if armed else (scan_times[0] if scan_times else None)
