"""Historical replay player — advances sim clock at configurable speed."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from .datastore import HistoricalDatastore
from .engine import SimulationEngine
from .sim_clock import SimClock


@dataclass
class PlayerState:
    status: str = "stopped"  # stopped | playing | paused | scanning
    pair: str = ""
    timeframe: str = "1s"
    range_start_ms: int = 0
    range_end_ms: int = 0
    current_ms: int = 0
    speed: float = 1.0  # sim-seconds per real second
    bots_running: bool = False
    bots_status: dict[str, Any] = field(default_factory=dict)
    active_scenario_id: str | None = None
    sequential_replay: bool = False
    batch_run: bool = False


class ReplayPlayer:
    def __init__(self, engine: SimulationEngine, datastore: HistoricalDatastore):
        self.engine = engine
        self.datastore = datastore
        self.clock = SimClock()
        self.state = PlayerState()
        self._task: asyncio.Task | None = None
        self._sequential_task: asyncio.Task | None = None
        self._replay_done: asyncio.Event = asyncio.Event()
        self._listeners: list[Callable[[dict], Any]] = []
        self._sim_step_hook: Callable[[int, int], None] | None = None

    def on_tick(self, callback: Callable[[dict], Any]) -> None:
        self._listeners.append(callback)

    def on_sim_step(self, callback: Callable[[int, int], None]) -> None:
        self._sim_step_hook = callback

    def _broadcast(self, event: dict) -> None:
        for cb in self._listeners:
            try:
                cb(event)
            except Exception:
                pass

    def snapshot(self, bots_runtime: dict | None = None) -> dict[str, Any]:
        k = None
        if self.state.pair and self.state.current_ms:
            k = self.datastore.kline_at(self.state.pair, self.state.timeframe, self.state.current_ms)
        out = {
            "status": self.state.status,
            "pair": self.state.pair,
            "timeframe": self.state.timeframe,
            "range_start_ms": self.state.range_start_ms,
            "range_end_ms": self.state.range_end_ms,
            "current_ms": self.state.current_ms,
            "sim_ms": self.state.current_ms,
            "current_iso": datetime.fromtimestamp(self.state.current_ms / 1000, tz=UTC).isoformat()
            if self.state.current_ms
            else None,
            "speed": self.state.speed,
            "bots_running": self.state.bots_running,
            "bots_status": self.state.bots_status,
            "active_scenario_id": self.state.active_scenario_id,
            "sequential_replay": self.state.sequential_replay,
            "batch_run": self.state.batch_run,
            "candle": k,
            "engine": self.engine.snapshot() if self.state.current_ms else None,
            **self.clock.snapshot(),
        }
        if bots_runtime is not None:
            out["bots_runtime"] = bots_runtime
        return out

    def configure(
        self,
        pair: str,
        range_start_ms: int,
        range_end_ms: int,
        timeframe: str = "1s",
        start_ms: int | None = None,
    ) -> dict:
        self.state.pair = pair
        self.state.timeframe = timeframe
        self.state.range_start_ms = range_start_ms
        self.state.range_end_ms = range_end_ms
        self.state.current_ms = start_ms if start_ms is not None else range_start_ms
        self.clock.set_range(range_start_ms, range_end_ms)
        self.clock.seek(self.state.current_ms)
        self.engine.timeframe = timeframe
        self.engine.set_clock(self.state.current_ms)
        return self.snapshot()

    def seek(self, ts_ms: int) -> dict:
        ts_ms = max(self.state.range_start_ms, min(ts_ms, self.state.range_end_ms))
        self.state.current_ms = ts_ms
        self.clock.seek(ts_ms)
        self.engine.set_clock(ts_ms)
        snap = self.snapshot()
        self._broadcast({"type": "tick", **snap})
        return snap

    def set_speed(self, speed: float) -> dict:
        self.state.speed = max(0.1, float(speed))
        return self.snapshot()

    async def play(self) -> None:
        if self.state.status == "playing":
            return
        self.state.status = "playing"
        self._replay_done.clear()
        self._task = asyncio.create_task(self._loop())

    async def play_through(self) -> None:
        """Run replay until period end or pause/stop."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._replay_done.clear()
        self.state.status = "playing"
        self._task = asyncio.create_task(self._loop())
        await self._replay_done.wait()

    def stop_replay_only(self) -> None:
        """Stop replay loop without signalling play_through completion."""
        self.state.status = "paused"
        if self._task and not self._task.done():
            self._task.cancel()

    def cancel_sequential(self) -> None:
        if self._sequential_task and not self._sequential_task.done():
            self._sequential_task.cancel()
        self._sequential_task = None
        self.state.active_scenario_id = None
        self.state.sequential_replay = False
        self.state.batch_run = False

    def pause(self) -> dict:
        self.state.status = "paused"
        if self._task and not self._task.done():
            self._task.cancel()
        self._replay_done.set()
        return self.snapshot()

    def stop(self) -> dict:
        self.state.status = "stopped"
        self.cancel_sequential()
        if self._task and not self._task.done():
            self._task.cancel()
        self._replay_done.set()
        return self.snapshot()

    def reset_replay(self) -> dict:
        """Stop and rewind sim clock to period start."""
        self.stop()
        if self.state.range_start_ms:
            return self.seek(self.state.range_start_ms)
        return self.snapshot()

    async def _loop(self) -> None:
        step_ms = 1000  # 1 sim-second per tick
        try:
            while self.state.status == "playing" and self.state.current_ms < self.state.range_end_ms:
                t0 = asyncio.get_event_loop().time()
                prev_ms, cur_ms = self.clock.advance(step_ms)
                self.state.current_ms = cur_ms
                self.engine.set_clock(cur_ms)

                if self._sim_step_hook:
                    self._sim_step_hook(prev_ms, cur_ms)

                snap = self.snapshot()
                event_type = "scan_tick" if self.clock.crossed_scan_boundary(prev_ms, cur_ms) else "tick"
                self._broadcast({"type": event_type, **snap})

                if self.state.current_ms >= self.state.range_end_ms:
                    self.state.status = "paused"
                    self._broadcast({"type": "finished", **snap})
                    self._replay_done.set()
                    break

                # Wall-clock sleep ONLY for UI pacing — bot logic uses sim_ms above
                delay = (step_ms / 1000.0) / self.state.speed
                elapsed = asyncio.get_event_loop().time() - t0
                await asyncio.sleep(max(0.001, delay - elapsed))
        except asyncio.CancelledError:
            self._replay_done.set()
            pass

    def set_bots_status(self, running: bool, status: dict | None = None) -> None:
        self.state.bots_running = running
        if status is not None:
            self.state.bots_status = status

    async def scan_catchup(
        self,
        scan_times: list[int],
        from_ms: int,
        to_ms: int,
        on_step: Callable[[dict], Any] | None = None,
        pause_sec: float = 0.15,
    ) -> None:
        """Fast-forward sim clock through scan ticks (wall sleep is UI only)."""
        self.state.status = "scanning"
        targets = [t for t in scan_times if from_ms <= t <= to_ms]
        if not targets and to_ms > from_ms:
            targets = [to_ms]
        for ts in targets:
            self.state.current_ms = ts
            self.clock.seek(ts)
            self.engine.set_clock(ts)
            snap = self.snapshot()
            snap["type"] = "scan_tick"
            self._broadcast(snap)
            if on_step:
                on_step(snap)
            await asyncio.sleep(pause_sec)
        self.state.status = "paused"
