#!/usr/bin/env python3
"""Run strategy iterations: tune on May, validate on June, append CSV stats per bot."""
from __future__ import annotations

import csv
import json
import copy
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT))

from simulation.scripts.bench_scanner_portfolio import eval_portfolio  # noqa: E402

SCAN_PATH = ROOT / "simulation/config/sim_player_scan.json"
SCEN_PATH = ROOT / "simulation/config/player_scenarios.json"
CSV_PATH = ROOT / "simulation/results/bot_iteration_stats.csv"
RESEARCH_PATH = ROOT / "simulation/results/iteration_research_log.json"

MAY = "20260501-20260531"
JUNE = "20260601-20260625"

CSV_FIELDS = [
    "iteration",
    "timestamp_utc",
    "phase",
    "change_summary",
    "sources",
    "portfolio_pnl_usdt",
    "portfolio_pnl_pct",
    "bot_id",
    "bot_label",
    "stake_usdt",
    "trades",
    "wins",
    "losses",
    "profit_usdt",
    "armed_pairs",
    "traded_pairs",
    "top_pairs",
    "enabled",
]


def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(p: Path, data) -> None:
    p.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")


def apply_scan_patch(patch: dict) -> None:
    scan = load_json(SCAN_PATH)
    scan.setdefault("ranging", {}).update(patch.get("ranging") or {})
    scan.setdefault("strategy", {}).update(patch.get("strategy") or {})
    save_json(SCAN_PATH, scan)


def apply_scenarios(scenarios: list[dict]) -> None:
    save_json(SCEN_PATH, scenarios)


def set_enabled(scenarios: list[dict], enabled_map: dict[str, bool]) -> list[dict]:
    out = copy.deepcopy(scenarios)
    for s in out:
        if s["id"] in enabled_map:
            s["enabled"] = enabled_map[s["id"]]
    return out


def append_csv(rows: list[dict]) -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not CSV_PATH.is_file()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(rows)


def log_iteration(
    iteration: str,
    phase: str,
    change: str,
    sources: str,
    report: dict,
    enabled_ids: set[str],
) -> list[dict]:
    ts = datetime.now(UTC).isoformat()
    rows = []
    for s in report.get("strategies") or []:
        top = s.get("top_pairs") or []
        top_str = "; ".join(f"{p.get('pair', '?')}:{p.get('profit_abs', 0):+.2f}" for p in top[:4])
        rows.append(
            {
                "iteration": iteration,
                "timestamp_utc": ts,
                "phase": phase,
                "change_summary": change,
                "sources": sources,
                "portfolio_pnl_usdt": report.get("pnl_usdt"),
                "portfolio_pnl_pct": report.get("pnl_pct"),
                "bot_id": s.get("id"),
                "bot_label": s.get("label"),
                "stake_usdt": s.get("stake_usdt"),
                "trades": s.get("total_trades"),
                "wins": s.get("wins"),
                "losses": s.get("losses"),
                "profit_usdt": s.get("profit_abs"),
                "armed_pairs": s.get("armed_pairs"),
                "traded_pairs": s.get("traded_pairs"),
                "top_pairs": top_str,
                "enabled": s.get("id") in enabled_ids,
            }
        )
    append_csv(rows)
    return rows


def run_experiment(
    iteration: str,
    change: str,
    sources: str,
    scan_patch: dict,
    scenario_patches: dict | None = None,
    enabled_map: dict[str, bool] | None = None,
) -> dict:
    orig_scan = load_json(SCAN_PATH)
    orig_scen = load_json(SCEN_PATH)
    try:
        if scan_patch.get("ranging") or scan_patch.get("strategy"):
            apply_scan_patch(scan_patch)
        scen = copy.deepcopy(orig_scen)
        if scenario_patches:
            by_id = {s["id"]: s for s in scen}
            for sid, patch in scenario_patches.items():
                if sid in by_id:
                    by_id[sid].update(patch)
                else:
                    scen.append(patch)
        if enabled_map:
            scen = set_enabled(scen, enabled_map)
        apply_scenarios(scen)
        enabled_ids = {s["id"] for s in scen if s.get("enabled", True)}

        may = eval_portfolio(MAY)
        log_iteration(iteration, "may_tune", change, sources, may, enabled_ids)
        june = eval_portfolio(JUNE)
        log_iteration(iteration, "june_validate", change, sources, june, enabled_ids)

        result = {
            "iteration": iteration,
            "change": change,
            "sources": sources,
            "may_pnl_pct": may["pnl_pct"],
            "june_pnl_pct": june["pnl_pct"],
            "may_pnl_usdt": may["pnl_usdt"],
            "june_pnl_usdt": june["pnl_usdt"],
            "both_positive": may["pnl_pct"] > 0 and june["pnl_pct"] > 0,
        }
        return result
    finally:
        save_json(SCAN_PATH, orig_scan)
        save_json(SCEN_PATH, orig_scen)


EXPERIMENTS = [
    {
        "id": "iter00_baseline",
        "change": "Baseline: grid+arb+swing, streak2 cum0.55, OP/APT/SUI off",
        "sources": "prior session",
        "scan": {},
        "scenario_patches": None,
        "enabled": None,
    },
    {
        "id": "iter01_regime_adx22",
        "change": "Scanner ADX max 22, bb_width 4-6.5% (Vantixs: no squeeze, ADX<25)",
        "sources": "vantixs.com/bollinger-mean-reversion-crypto-bot",
        "scan": {"ranging": {"adx_max": 22, "bb_width_min": 0.04, "bb_width_max": 0.065}},
    },
    {
        "id": "iter02_mean_rev_bot",
        "change": "Add SimMeanReversionRange (BB+RSI+ADX<25, exit mid, SL -2%)",
        "sources": "Superior-Trade/mean-reversion.md + DEV RSI template",
        "scan": {"ranging": {"adx_max": 22, "bb_width_min": 0.04}},
        "enabled": {
            "lite_swing": False,
            "lite_arbitrage": True,
            "live_grid": True,
            "lite_mean_rev": True,
        },
        "scenario_patches": {
            "lite_mean_rev": {
                "id": "lite_mean_rev",
                "label": "Mean-reversion (BB)",
                "enabled": True,
                "strategy": "SimMeanReversionRange",
                "strategy_path": "simulation/strategies",
                "config": "simulation/config/backtest_lite_base.json",
                "scan_type": "strategy",
                "group": "lite",
                "stake_usdt": 15,
                "settings": "ADX<25 · BB 4% · exit mid · 3x",
            }
        },
    },
    {
        "id": "iter03_lite_range_on",
        "change": "Enable LiteRangeStrategy (BB rejection), disable swing",
        "sources": "LiteFinance range + XCryptoBot grid ranging",
        "scan": {"ranging": {"adx_max": 22, "bb_width_min": 0.04, "min_ranging_ratio": 0.58}},
        "enabled": {"lite_swing": False, "lite_range": True, "lite_arbitrage": True, "live_grid": True},
        "scenario_patches": {"lite_range": {"enabled": True, "stake_usdt": 20, "settings": "BB reject · ADX<18 · 1x"}},
    },
    {
        "id": "iter04_grid_safe_combo",
        "change": "Grid aggressive + grid safe 30 USDT, tighter ADX 16",
        "sources": "TradeAlgo geometric grid + VoiceOfChain ATR range",
        "scan": {
            "ranging": {
                "adx_max": 20,
                "bb_width_min": 0.038,
                "min_ranging_ratio": 0.59,
                "trade_skip_loss_streak": 2,
                "trade_skip_min_cum_loss_usdt": 0.5,
            }
        },
        "enabled": {"lite_swing": False, "lite_arbitrage": True, "live_grid": True, "live_grid_safe": True},
        "scenario_patches": {
            "live_grid_safe": {"enabled": True, "stake_usdt": 30},
            "live_grid": {"stake_usdt": 45, "settings": "5x · ADX20 · streak2 · stake 45"},
        },
    },
    {
        "id": "iter05_best_combo",
        "change": "Mean-rev + grid45 + arb; ADX22 BB4%; streak2/0.5",
        "sources": "combined best practices",
        "scan": {
            "ranging": {
                "adx_max": 22,
                "bb_width_min": 0.04,
                "bb_width_max": 0.065,
                "min_ranging_ratio": 0.58,
                "trade_skip_loss_streak": 2,
                "trade_skip_min_cum_loss_usdt": 0.5,
                "pnl_circuit_min_loss_usdt": 0.5,
            }
        },
        "enabled": {
            "lite_swing": False,
            "lite_arbitrage": True,
            "live_grid": True,
            "lite_mean_rev": True,
        },
        "scenario_patches": {
            "lite_mean_rev": {
                "id": "lite_mean_rev",
                "label": "Mean-reversion (BB)",
                "enabled": True,
                "strategy": "SimMeanReversionRange",
                "strategy_path": "simulation/strategies",
                "config": "simulation/config/backtest_lite_base.json",
                "scan_type": "strategy",
                "group": "lite",
                "stake_usdt": 15,
                "settings": "ADX<25 · BB 4% · exit mid · 3x",
            },
            "live_grid": {"stake_usdt": 45},
        },
    },
]


def ensure_mean_rev_scenario(scenarios: list[dict]) -> list[dict]:
    if any(s.get("id") == "lite_mean_rev" for s in scenarios):
        return scenarios
    scenarios.append(
        {
            "id": "lite_mean_rev",
            "label": "Mean-reversion (BB)",
            "enabled": False,
            "strategy": "SimMeanReversionRange",
            "strategy_path": "simulation/strategies",
            "config": "simulation/config/backtest_lite_base.json",
            "scan_type": "strategy",
            "group": "lite",
            "stake_usdt": 15,
            "settings": "ADX<25 · BB 4% · exit mid · 3x",
        }
    )
    return scenarios


def main() -> int:
    orig_scen = ensure_mean_rev_scenario(load_json(SCEN_PATH))
    save_json(SCEN_PATH, orig_scen)

    results = []
    for exp in EXPERIMENTS:
        print(f"\n=== {exp['id']} ===", flush=True)
        r = run_experiment(
            exp["id"],
            exp["change"],
            exp.get("sources", ""),
            {
                "ranging": (exp.get("scan") or {}).get("ranging") or {},
                "strategy": (exp.get("scan") or {}).get("strategy") or {},
            },
            scenario_patches=exp.get("scenario_patches"),
            enabled_map=exp.get("enabled"),
        )
        print(
            f"May {r['may_pnl_pct']:+.2f}%  June {r['june_pnl_pct']:+.2f}%  both+={r['both_positive']}",
            flush=True,
        )
        results.append(r)

    best = sorted(
        [r for r in results if r["both_positive"]],
        key=lambda x: x["may_pnl_pct"] + x["june_pnl_pct"],
        reverse=True,
    )
    summary = {"experiments": results, "best_both_positive": best[:3]}
    save_json(RESEARCH_PATH, summary)
    print(f"\nCSV: {CSV_PATH}")
    print(f"Summary: {RESEARCH_PATH}")
    if best:
        print(f"Best both+: {best[0]['iteration']} May {best[0]['may_pnl_pct']}% June {best[0]['june_pnl_pct']}%")
        winner = next(e for e in EXPERIMENTS if e["id"] == best[0]["iteration"])
        apply_winning_config(winner)
        print(f"Applied winning config: {winner['id']}")
    return 0


def apply_winning_config(exp: dict) -> None:
    """Persist best iteration to live player config."""
    scan = load_json(SCAN_PATH)
    scan.setdefault("ranging", {}).update((exp.get("scan") or {}).get("ranging") or {})
    scan.setdefault("strategy", {}).update((exp.get("scan") or {}).get("strategy") or {})
    save_json(SCAN_PATH, scan)
    scen = ensure_mean_rev_scenario(load_json(SCEN_PATH))
    if exp.get("scenario_patches"):
        by_id = {s["id"]: s for s in scen}
        for sid, patch in exp["scenario_patches"].items():
            if sid in by_id:
                by_id[sid].update(patch)
            else:
                scen.append(patch)
    if exp.get("enabled"):
        scen = set_enabled(scen, exp["enabled"])
    save_json(SCEN_PATH, scen)
    # Final verification run logged to CSV
    enabled_ids = {s["id"] for s in scen if s.get("enabled", True)}
    may = eval_portfolio(MAY)
    log_iteration(exp["id"] + "_applied", "may_final", exp["change"], exp.get("sources", ""), may, enabled_ids)
    june = eval_portfolio(JUNE)
    log_iteration(exp["id"] + "_applied", "june_final", exp["change"], exp.get("sources", ""), june, enabled_ids)
    save_json(ROOT / "simulation/results/scanner_may_final.json", may)
    save_json(ROOT / "simulation/results/scanner_june_final.json", june)


if __name__ == "__main__":
    raise SystemExit(main())
