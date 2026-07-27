"""Simulation clock — all bot logic uses sim_ms, never wall clock."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SimClock:
    """Single source of simulated time for the player and bots."""

    sim_ms: int = 0
    range_start_ms: int = 0
    range_end_ms: int = 0
    last_scan_ms: int = 0
    scan_interval_ms: int = 30 * 60 * 1000

    def set_range(self, start_ms: int, end_ms: int) -> None:
        self.range_start_ms = start_ms
        self.range_end_ms = end_ms
        self.sim_ms = start_ms
        self.last_scan_ms = start_ms

    def seek(self, sim_ms: int) -> int:
        self.sim_ms = max(self.range_start_ms, min(sim_ms, self.range_end_ms))
        return self.sim_ms

    def advance(self, step_ms: int) -> tuple[int, int]:
        prev = self.sim_ms
        self.sim_ms = min(self.sim_ms + step_ms, self.range_end_ms)
        return prev, self.sim_ms

    def crossed_scan_boundary(self, prev_ms: int, cur_ms: int) -> bool:
        if cur_ms <= prev_ms:
            return False
        prev_bucket = (prev_ms - self.range_start_ms) // self.scan_interval_ms
        cur_bucket = (cur_ms - self.range_start_ms) // self.scan_interval_ms
        return cur_bucket > prev_bucket

    def snapshot(self) -> dict:
        return {
            "sim_ms": self.sim_ms,
            "range_start_ms": self.range_start_ms,
            "range_end_ms": self.range_end_ms,
            "scan_interval_ms": self.scan_interval_ms,
        }
