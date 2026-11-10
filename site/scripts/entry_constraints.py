"""Entry constraints shared by the signal router and the trade executor.

Lane membership is the shared rule. Each process still loads its own placement
document and its own open-trade counts.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import Any

from placement_lanes import panel_for, test_lane_ids


def test_ids(
    catalog: Iterable[str],
    placement: Mapping[str, Any] | None,
    *,
    htf_ids: Collection[str] = (),
) -> frozenset[str]:
    return test_lane_ids(catalog, placement, htf_ids=htf_ids)


def uses_full_pair_universe(sid: str, placement: Mapping[str, Any] | None) -> bool:
    """Test lane without the retired-1h exception: every whitelist pair."""
    return panel_for(sid, placement) == "test"


def cap_reached(open_count: int, cap: int) -> bool:
    """True when the pool is off (cap <= 0) or already full."""
    return int(cap) <= 0 or int(open_count) >= int(cap)
