"""Placement lane for a strategy id.

Callers pass the placement document they already loaded. This module does not
read SQLite or JSON, so signal-engine, trade-executor, and pair-config share
one rule without sharing a store.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import Any


def panel_for(
    sid: str,
    placement: Mapping[str, Any] | None,
    *,
    htf_ids: Collection[str] = (),
) -> str:
    """Return ``main``, ``test``, or ``hidden``.

    Retired 1h ids passed in ``htf_ids`` are hidden even when listed in main.
    Anything else outside main and hidden is the test lane.
    """
    raw = placement if isinstance(placement, Mapping) else {}
    strategy_id = str(sid or "")
    main = {str(x) for x in (raw.get("main") or [])}
    hidden = {str(x) for x in (raw.get("hidden") or [])}
    retired = {str(x) for x in htf_ids}
    if strategy_id in retired or strategy_id in hidden:
        return "hidden"
    if strategy_id in main:
        return "main"
    return "test"


def test_lane_ids(
    catalog: Iterable[str],
    placement: Mapping[str, Any] | None,
    *,
    htf_ids: Collection[str] = (),
) -> frozenset[str]:
    """Ids from ``catalog`` whose lane is test."""
    return frozenset(
        str(sid)
        for sid in catalog
        if panel_for(str(sid), placement, htf_ids=htf_ids) == "test"
    )
