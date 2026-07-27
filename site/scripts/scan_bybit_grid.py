#!/usr/bin/env python3
"""Scan best ranging pair and deploy one Bybit Futures Grid bot."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from bybit_grid_manager import (  # noqa: E402
    check_deploy_allowed,
    create_grid,
    ft_base,
    get_active_grid_pair_keys,
    get_status_payload,
    load_config,
    normalize_grid_pair_key,
    save_json,
    suggest_params,
)
from scan_ranging_pairs import (  # noqa: E402
    INTERVAL_MAP,
    analyze_ranging,
    bybit_symbol_to_ft,
    fetch_htf_klines,
    fetch_klines,
    fetch_tickers,
    load_scan_config,
)

SCAN_RESULT_FILE = "user_data/bybit_grid_scan.json"


def _merged_exclude_bases(ranging_cfg: dict[str, Any], bybit_cfg: dict[str, Any]) -> set[str]:
    return set(ranging_cfg.get("exclude_bases", [])) | set(bybit_cfg.get("exclude_bases", []))


def scan_best_pair(*, verbose: bool = False, exclude_active_pairs: bool = False) -> dict[str, Any]:
    """Return highest-scoring ranging pair suitable for neutral Bybit grid."""
    base = ft_base()
    ranging_cfg = load_scan_config(base)
    bybit_cfg = load_config()
    exclude_bases = _merged_exclude_bases(ranging_cfg, bybit_cfg)

    interval = INTERVAL_MAP.get(ranging_cfg["timeframe"], "5")
    lookback = int(ranging_cfg["lookback_candles"])
    min_ratio = float(ranging_cfg["min_ranging_ratio"])
    scan_top = int(ranging_cfg.get("scan_top_volume", 150))
    min_vol = float(ranging_cfg["min_turnover24h_usd"])
    exclude_sym = set(ranging_cfg.get("exclude_symbols", []))
    priority_bases = set(ranging_cfg.get("priority_bases", []))
    priority_boost = float(ranging_cfg.get("priority_score_boost", 1.25))

    candidates: list[tuple[float, str]] = []
    for t in fetch_tickers():
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym in exclude_sym:
            continue
        base_coin = sym[: -len("USDT")]
        if base_coin in exclude_bases:
            continue
        vol = float(t.get("turnover24h") or 0)
        if vol >= min_vol:
            candidates.append((vol, sym))
    candidates.sort(reverse=True)
    candidates = candidates[:scan_top]

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, (vol, sym) in enumerate(candidates):
        try:
            df = fetch_klines(sym, interval, lookback + 15)
            df_htf = fetch_htf_klines(sym, ranging_cfg)
            metrics = analyze_ranging(df, ranging_cfg, df_htf=df_htf)
            if metrics is None or metrics["ranging_ratio"] < min_ratio:
                continue
            base_coin = sym[: -len("USDT")]
            score = metrics["score"]
            if base_coin in priority_bases:
                score *= priority_boost
                metrics["priority"] = True
            else:
                metrics["priority"] = False
            metrics["score"] = round(score, 6)
            pair = bybit_symbol_to_ft(sym)
            results.append({"pair": pair, "symbol": sym, "turnover24h": vol, **metrics})
            if verbose:
                print(f"  OK {pair} score={metrics['score']}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{sym}: {exc}")
        if i % 10 == 9:
            time.sleep(0.35)

    if not results:
        raise RuntimeError("No ranging pair found for Bybit grid")

    results.sort(key=lambda x: x["score"], reverse=True)
    active_keys = get_active_grid_pair_keys(refresh=True)
    best_overall = results[0]
    available = [
        r
        for r in results
        if normalize_grid_pair_key(pair=r.get("pair"), symbol=r.get("symbol")) not in active_keys
    ]

    if exclude_active_pairs:
        if not available:
            active_labels = ", ".join(sorted(active_keys)) or "—"
            raise RuntimeError(
                "Нет доступных пар для нового grid — все кандидаты уже в активных ботах: "
                f"{active_labels}"
            )
        best = available[0]
    else:
        best = best_overall

    if verbose and active_keys:
        print(f"Active pairs skipped: {', '.join(sorted(active_keys))}")
        if best_overall is not best:
            print(
                f"Best overall {best_overall['pair']} is active — selected {best['pair']} "
                f"(score {best['score']})"
            )

    report = {
        "scanned_at": datetime.now(UTC).isoformat(),
        "candidates_checked": len(candidates),
        "ranging_found": len(results),
        "available_count": len(available),
        "active_pairs": sorted(active_keys),
        "best": best,
        "best_overall": best_overall,
        "top5": results[:5],
        "top5_available": available[:5],
        "errors": errors[:15],
    }
    save_json(base / SCAN_RESULT_FILE, report)
    return report


def _deploy_error_hint(message: str) -> str:
    lower = message.lower()
    if "precision" in lower:
        return "Некорректная точность цен сетки для этой пары"
    if "balance" in lower or "insufficient" in lower or "enough money" in lower or "400001" in message:
        return "Недостаточно USDT на Funding Account Bybit (Grid боты берут маржу оттуда, не с Unified)"
    if "fbu" in lower or "funding account" in lower:
        return "Нет USDT на Funding Account — переведите с Unified Trading Account"
    if "investment" in lower:
        return "Увеличьте сумму инвестиции в настройках Grid — минимум зависит от пары"
    return "Проверьте параметры сетки и баланс Funding Account на Bybit"


def deploy_best(*, dry_run: bool = False, force: bool = False, verbose: bool = False) -> dict[str, Any]:
    cfg = load_config()
    status = get_status_payload()
    active = [b for b in status.get("bots", []) if b.get("is_active")]
    max_bots = int(cfg.get("max_active_bots", 1))

    if len(active) >= max_bots and not force:
        return {
            "skipped": True,
            "reason": "active_bot_exists",
            "active_bots": active,
            "max_active_bots": max_bots,
            "message": f"Достигнут лимит Bybit Grid ({len(active)}/{max_bots}) — закройте бота или увеличьте лимит",
        }

    scan = scan_best_pair(verbose=verbose, exclude_active_pairs=True)
    defaults = cfg.get("defaults", {})
    grid_mode = int(cfg.get("auto_grid_mode", 1))
    overrides = {
        "grid_mode": grid_mode,
        "total_investment": str(defaults.get("total_investment", "10")),
    }

    candidates = list(scan.get("top5_available") or [])
    if not candidates:
        candidates = [scan["best"]]
    seen_pairs: set[str] = set()
    try_list: list[dict[str, Any]] = []
    for cand in candidates:
        key = normalize_grid_pair_key(pair=cand.get("pair"), symbol=cand.get("symbol"))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        try_list.append(cand)

    last_error: str | None = None
    last_params: dict[str, Any] | None = None
    skipped: list[dict[str, str]] = []

    for cand in try_list:
        pair = cand["pair"]
        try:
            check_deploy_allowed(pair, grid_mode, cfg)
            params = suggest_params(pair, overrides)
            last_params = params
        except ValueError as exc:
            last_error = str(exc)
            skipped.append({"pair": pair, "reason": str(exc)})
            if verbose:
                print(f"  skip {pair}: {exc}")
            continue

        if dry_run:
            return {
                "dry_run": True,
                "scan": scan,
                "params": params,
                "skipped_candidates": skipped,
            }

        try:
            created = create_grid(params)
        except (RuntimeError, ValueError) as exc:
            last_error = str(exc)
            skipped.append({"pair": pair, "reason": str(exc)})
            if verbose:
                print(f"  fail {pair}: {exc}")
            continue

        result = {
            "deployed": True,
            "scan": scan,
            "params": params,
            "create": created,
            "skipped_candidates": skipped,
        }
        save_json(ft_base() / SCAN_RESULT_FILE, {**scan, "last_deploy": result})
        return result

    return {
        "deployed": False,
        "error": last_error or "Нет подходящих пар после фильтров A1/A3",
        "scan": scan,
        "params": last_params,
        "skipped_candidates": skipped,
        "hint": _deploy_error_hint(last_error or ""),
    }


def run_scan_only(*, verbose: bool = False) -> dict[str, Any]:
    """Scan ranging pairs and save report without deploying a grid bot."""
    scan = scan_best_pair(verbose=verbose, exclude_active_pairs=True)
    return {"ok": True, "scan": scan}


def get_scan_status() -> dict[str, Any]:
    path = ft_base() / SCAN_RESULT_FILE
    if not path.is_file():
        return {"scanned_at": None, "best": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"scanned_at": None, "best": None}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Scan + deploy one Bybit Futures Grid")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Deploy even if bot exists")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    try:
        out = deploy_best(dry_run=args.dry_run, force=args.force, verbose=args.verbose)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
