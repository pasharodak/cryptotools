#!/usr/bin/env python3
"""Append-only signal feed between signal engine and trade executor."""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

_lock = threading.Lock()
_seq = 0
# One entry signal per pair+strategy+side per 5m candle (stops throttle spam).
_recent_publish: dict[str, float] = {}
SIGNAL_DEDUPE_SEC = 300.0


def _base() -> Path:
    return Path(os.environ.get("CT_BASE", "/home/cryptotools/app"))


def feed_path() -> Path:
    override = os.environ.get("CT_SIGNAL_FEED")
    if override:
        return Path(override)
    return _base() / "user_data" / "signals" / "feed.jsonl"


def offset_path() -> Path:
    return feed_path().with_suffix(".offset")


def _recover_seq() -> int:
    """Continue seq counter after signal-engine restarts."""
    path = feed_path()
    if not path.is_file():
        return 0
    max_seq = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                max_seq = max(max_seq, int(ev.get("seq") or 0))
    except OSError:
        return 0
    return max_seq


def _next_seq() -> int:
    global _seq
    if _seq <= 0:
        _seq = _recover_seq()
    _seq += 1
    return _seq


def publish(event: dict[str, Any]) -> dict[str, Any]:
    """Append one signal event; returns enriched payload with id/seq."""
    path = feed_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    with _lock:
        payload = {
            "id": str(uuid.uuid4()),
            "seq": _next_seq(),
            "ts": now.isoformat(),
            **event,
        }
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return payload


def _entry_dedupe_key(pair: str, strategy_id: str, side: str, ts: datetime) -> str:
    bucket = int(ts.timestamp()) // 300
    return f"{pair}|{strategy_id}|{side}|{bucket}"


def _prune_dedupe(now: float) -> None:
    cutoff = now - SIGNAL_DEDUPE_SEC
    stale = [k for k, t in _recent_publish.items() if t < cutoff]
    for k in stale:
        del _recent_publish[k]


def publish_entry_signal(
    *,
    pair: str,
    side: str,
    strategy_id: str,
    rate: float,
    entry_tag: str | None = None,
    scenario: dict[str, Any] | None = None,
    indicators: dict[str, str] | None = None,
    ml: dict[str, Any] | None = None,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    ts = timestamp or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    key = _entry_dedupe_key(pair, strategy_id, side, ts)
    with _lock:
        _prune_dedupe(ts.timestamp())
        if key in _recent_publish:
            return {"event": "entry_signal_deduped", "pair": pair, "strategy_id": strategy_id, "side": side}
        _recent_publish[key] = ts.timestamp()
    return publish(
        {
            "event": "entry_signal",
            "pair": pair,
            "side": side,
            "strategy_id": strategy_id,
            "entry_tag": entry_tag or strategy_id,
            "rate": float(rate),
            "scenario": scenario or {},
            "indicators": indicators or {},
            "ml": ml or {},
            "signal_time": ts.isoformat(),
        }
    )


def read_offset() -> int:
    """Return last processed feed line number (not event seq)."""
    path = offset_path()
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "line" in data:
            return max(0, int(data["line"]))
        # Legacy seq-based offsets are unsafe after signal-engine restarts.
        return 0
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def write_offset(line: int) -> None:
    path = offset_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".offset.tmp")
    tmp.write_text(json.dumps({"line": int(line)}), encoding="utf-8")
    tmp.replace(path)


def iter_events(*, after_line: int = 0) -> Iterator[dict[str, Any]]:
    path = feed_path()
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if line_no <= after_line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def tail_new_events(last_line: int) -> tuple[list[dict[str, Any]], int]:
    out: list[dict[str, Any]] = []
    max_line = last_line
    path = feed_path()
    if not path.is_file():
        return out, max_line
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if line_no <= last_line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.append(ev)
            max_line = line_no
    return out, max_line


def feed_line_count() -> int:
    path = feed_path()
    if not path.is_file():
        return 0
    count = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                count += 1
    return count
