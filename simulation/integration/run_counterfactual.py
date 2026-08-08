#!/usr/bin/env python3
"""Run baseline vs improved backtests and compare hypothetical balances."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SCENARIOS = [
    {
        "id": "grid_baseline",
        "bot": "grid",
        "label": "Grid FT (старые настройки)",
        "config": "simulation/config/backtest_grid_baseline.json",
        "strategy": "VolatilityGridStrategyBaseline",
    },
    {
        "id": "grid_improved",
        "bot": "grid",
        "label": "Grid FT (улучшения G1–G6)",
        "config": "simulation/config/backtest_grid_improved.json",
        "strategy": "VolatilityGridStrategy",
    },
    {
        "id": "strategy_baseline",
        "bot": "strategy",
        "label": "Стратегии (старые)",
        "config": "simulation/config/backtest_strategy_baseline.json",
        "strategy": "MultiStrategyRouterBaseline",
    },
    {
        "id": "strategy_improved",
        "bot": "strategy",
        "label": "Стратегии (улучшения S1–S5)",
        "config": "simulation/config/backtest_strategy_improved.json",
        "strategy": "MultiStrategyRouter",
    },
]


def root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_manifest() -> dict:
    return json.loads((root() / "simulation" / "config" / "manifest.json").read_text(encoding="utf-8"))


def load_export() -> dict:
    p = root() / "simulation" / "data" / "live_trades_export.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _load_blacklist(cfg: dict) -> list[str]:
    imp = root() / "simulation" / "config" / "backtest_grid_improved.json"
    if imp.is_file():
        bl = json.loads(imp.read_text(encoding="utf-8"))["exchange"].get("pair_blacklist", [])
        return list(bl)
    return []


def patch_whitelist(config_path: Path, pairs: list[str], out_path: Path) -> None:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    cfg["exchange"]["pair_whitelist"] = pairs
    frag_path = root() / "simulation" / "config" / "sim_common_fragment.json"
    if frag_path.is_file():
        frag = json.loads(frag_path.read_text(encoding="utf-8"))
        for key, val in frag.items():
            if key == "exchange":
                cfg.setdefault("exchange", {}).update(val)
            else:
                cfg[key] = val
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cfg, indent=4), encoding="utf-8")


def parse_backtest_stdout(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    m = re.search(r"Total profit %\s*\|\s*([-\d.]+)%", text)
    if m:
        out["profit_pct"] = float(m.group(1))
    m = re.search(r"Absolute profit\s*\|\s*([-\d.]+)", text)
    if m:
        out["profit_abs"] = float(m.group(1))
    m = re.search(r"Total/Daily Avg Trades\s*\|\s*(\d+)", text)
    if m:
        out["trades"] = int(m.group(1))
    m = re.search(r"Final balance\s*\|\s*([-\d.]+)", text)
    if m:
        out["final_balance"] = float(m.group(1))
    m = re.search(r"Win.*Draw.*Loss.*\|\s*(\d+).*(\d+).*(\d+)", text)
    if m:
        out["wins"] = int(m.group(1))
        out["losses"] = int(m.group(3))
    return out


def run_backtest(scenario: dict, pairs: list[str], cfg: dict) -> dict[str, Any]:
    r = root()
    config_src = r / scenario["config"]
    run_cfg = r / "simulation" / "data" / "runtime" / f"{scenario['id']}_config.json"
    patch_whitelist(config_src, pairs, run_cfg)

    datadir = r / cfg["ctengine_datadir"]
    results = r / cfg["results_dir"] / scenario["id"]
    results.mkdir(parents=True, exist_ok=True)

    ft = r / ".venv" / "Scripts" / "ctbot.exe"
    timerange = cfg.get("timerange", "20250618-20250626")
    balance = str(cfg.get("starting_balance_usdt", 100))

    cmd = [
        str(ft),
        "backtesting",
        "--config",
        str(run_cfg),
        "--strategy",
        scenario["strategy"],
        "--datadir",
        str(datadir),
        "--timerange",
        timerange,
        "--dry-run-wallet",
        balance,
        "--export",
        "trades",
        "--backtest-directory",
        str(results),
        "--cache",
        "none",
    ]
    print(f"\n>>> {scenario['label']}")
    proc = subprocess.run(cmd, cwd=str(r), capture_output=True, text=True)
    log_path = results / "console.log"
    log_path.write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        print(proc.stderr[-2000:] if proc.stderr else proc.stdout[-2000:])
        return {"id": scenario["id"], "error": proc.stderr or proc.stdout, "returncode": proc.returncode}

    metrics = parse_backtest_stdout(proc.stdout)
    metrics["id"] = scenario["id"]
    metrics["label"] = scenario["label"]
    metrics["bot"] = scenario["bot"]
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Counterfactual backtest: baseline vs improved")
    parser.add_argument("--only", choices=[s["id"] for s in SCENARIOS], action="append")
    parser.add_argument(
        "--all-pairs",
        action="store_true",
        help="Use union of all traded pairs (not per-bot subset)",
    )
    parser.add_argument("--output", type=str, default="", help="Report filename under results_dir")
    args = parser.parse_args()

    cfg = load_manifest()
    export = load_export()
    pairs = export.get("all_pairs") or []
    if not pairs:
        print("No pairs — fetch VPS DBs and run export_trades.py")
        return 1

    tradable = [p for p in pairs if p not in set(_load_blacklist(cfg))]
    print(f"Timerange: {cfg.get('timerange')} | pairs traded: {len(pairs)} | tradable (after BL): {len(tradable)}")

    scenarios = SCENARIOS
    if args.only:
        scenarios = [s for s in SCENARIOS if s["id"] in args.only]

    results: list[dict] = []
    for sc in scenarios:
        if args.all_pairs:
            if sc["bot"] == "grid":
                bot_pairs = tradable if sc["id"] == "grid_improved" else pairs
            else:
                bot_pairs = export["bots"].get(sc["bot"], {}).get("pairs") or pairs
        else:
            bot_pairs = export["bots"].get(sc["bot"], {}).get("pairs") or pairs
        print(f"  {sc['id']}: {len(bot_pairs)} pairs")
        results.append(run_backtest(sc, bot_pairs, cfg))

    actual = {name: export["bots"].get(name, {}).get("pnl_actual", 0) for name in ("grid", "strategy", "finder")}
    start = float(cfg.get("starting_balance_usdt", 100))

    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "timerange": cfg.get("timerange"),
        "starting_balance_per_bot": start,
        "pairs_traded": len(pairs),
        "pairs_tradable_after_blacklist": len(tradable),
        "actual_pnl": actual,
        "scenarios": results,
        "summary": [],
    }

    for bot in ("grid", "strategy"):
        base = next((r for r in results if r.get("id") == f"{bot}_baseline"), {})
        imp = next((r for r in results if r.get("id") == f"{bot}_improved"), {})
        if "final_balance" in imp and "final_balance" in base:
            report["summary"].append(
                {
                    "bot": bot,
                    "actual_pnl": actual.get(bot, 0),
                    "baseline_balance": base.get("final_balance"),
                    "improved_balance": imp.get("final_balance"),
                    "delta_vs_baseline": round(imp["final_balance"] - base["final_balance"], 4),
                    "delta_vs_actual": round(imp["final_balance"] - start - actual.get(bot, 0), 4),
                }
            )

    out_name = args.output or "counterfactual_report.json"
    out = root() / cfg["results_dir"] / out_name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + "=" * 60)
    print("COUNTERFACTUAL REPORT")
    print("=" * 60)
    for s in report["summary"]:
        print(f"\n{s['bot'].upper()}:")
        print(f"  Факт PnL (live):     {s['actual_pnl']:+.4f} USDT")
        print(f"  Баланс baseline:     {s['baseline_balance']:.2f} USDT")
        print(f"  Баланс improved:     {s['improved_balance']:.2f} USDT")
        print(f"  Выигрыш vs baseline: {s['delta_vs_baseline']:+.4f} USDT")
    print(f"\nFull report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
