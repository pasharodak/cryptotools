#!/usr/bin/env python3
"""Compare prod VPS models vs current local models on the same OOS test data."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, roc_auc_score

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.ml.pnl_classifier import (  # noqa: E402
    build_dataframe,
    feature_columns,
    load_all_training_trades,
    make_market_store,
    predict_entry,
    time_split,
)
from simulation.ml.trade_gate import GRID_SCENARIO_ID, _resolve_gate_rules  # noqa: E402

EXPORT_DIR = ROOT / "simulation/results/full_ml_study/export"
PROD_PACK = ROOT / "simulation/results/prod_snapshot/models/pnl_classifier"
CURRENT_PACK = ROOT / "simulation/results/trade_db/models"
OUT_PATH = ROOT / "simulation/results/full_ml_study/prod_vs_current_models.json"


def _load_export_records() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in ("all_wins.jsonl", "all_losses.jsonl"):
        path = EXPORT_DIR / name
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def _open_ms(rec: dict) -> int:
    return int((rec.get("trade") or {}).get("open_ms") or 0)


def _oos_records(records: list[dict], test_frac: float = 0.2) -> list[dict]:
    keyed = sorted(enumerate(records), key=lambda ir: (_open_ms(ir[1]), ir[0]))
    cut = int(len(keyed) * (1 - test_frac))
    return [r for _, r in keyed[cut:]]


def _scenario_dict(rec: dict) -> dict:
    return {
        "id": rec["scenario_id"],
        "scenario_id": rec["scenario_id"],
        "label": rec.get("label"),
        "scan_type": rec.get("scan_type") or "strategy",
        "group": rec.get("group") or "unknown",
        "strategy": rec.get("strategy") or "unknown",
    }


def _aggregate(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"trades": 0, "pnl_usdt": 0.0, "wins": 0, "losses": 0, "win_rate": 0.0}
    pnl = sum(float(r.get("profit_abs") or 0) for r in rows)
    wins = sum(1 for r in rows if float(r.get("profit_abs") or 0) >= 0)
    n = len(rows)
    return {
        "trades": n,
        "pnl_usdt": round(pnl, 4),
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(wins / n, 4) if n else 0.0,
    }


class ModelPack:
    def __init__(self, name: str, pack_dir: Path, meta_path: Path | None = None):
        self.name = name
        self.pack_dir = pack_dir
        self.meta_path = meta_path or (pack_dir / "pnl_classifier_meta.json")
        self._global = None
        self._by_sid: dict[str, Any] = {}
        self._market = None

    def meta(self) -> dict[str, Any]:
        if self.meta_path.is_file():
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        return {}

    def global_pipe(self):
        if self._global is None:
            path = self.pack_dir / "pnl_classifier.joblib"
            if not path.is_file():
                raise FileNotFoundError(path)
            self._global = joblib.load(path)
        return self._global

    def pipe_for(self, scenario_id: str):
        path = self.pack_dir / "by_scenario" / scenario_id / "pnl_classifier.joblib"
        if path.is_file():
            if scenario_id not in self._by_sid:
                self._by_sid[scenario_id] = joblib.load(path)
            return self._by_sid[scenario_id]
        return self.global_pipe()

    def market(self):
        if self._market is None:
            self._market = make_market_store(ROOT)
        return self._market

    def predict(self, rec: dict, *, use_per_strategy: bool = False) -> dict[str, Any]:
        sc = _scenario_dict(rec)
        sid = sc["scenario_id"]
        pipe = self.pipe_for(sid) if use_per_strategy else self.global_pipe()
        return predict_entry(
            pipe,
            {
                "scenario_id": sid,
                "pair": rec["pair"],
                "label": rec.get("label"),
                "profit_abs": rec.get("profit_abs"),
                "basis": {
                    "scenario_id": sid,
                    "pair": rec["pair"],
                    "scan_type": sc.get("scan_type"),
                    "group": sc.get("group"),
                    "strategy": sc.get("strategy"),
                    "stake_usdt": (rec.get("inst_config") or {}).get("stake"),
                    "stoploss": (rec.get("inst_config") or {}).get("stoploss"),
                    "minimal_roi": (rec.get("inst_config") or {}).get("minimal_roi"),
                    "timeframe": (rec.get("inst_config") or {}).get("timeframe", "5m"),
                    "armed_at_ms": rec.get("armed_at_ms"),
                },
                "trade": rec.get("trade") or {},
            },
            self.market(),
        )

    def should_block(self, ml: dict, scenario: dict, gate_cfg: dict) -> bool:
        mode, min_conf = _resolve_gate_rules(gate_cfg, scenario)
        predicted = ml.get("predicted")
        conf = float(ml.get("confidence") or 0.0)
        if mode == "profit_only":
            if predicted != "profit":
                return True
            profit_conf = float(ml.get("confidence_profit") or (conf if predicted == "profit" else 0.0))
            return profit_conf < min_conf
        block = gate_cfg.get("block_predicted", "loss")
        if predicted != block:
            return False
        return conf >= min_conf

    def gate_portfolio(
        self,
        records: list[dict],
        gate_cfg: dict[str, Any],
        *,
        use_per_strategy: bool = False,
    ) -> dict[str, Any]:
        kept: list[dict] = []
        blocked: list[dict] = []
        for rec in records:
            sc = _scenario_dict(rec)
            ml = self.predict(rec, use_per_strategy=use_per_strategy)
            row = {**rec, "ml": ml}
            if self.should_block(ml, sc, gate_cfg):
                blocked.append(row)
            else:
                kept.append(row)
        port = _aggregate(kept)
        port["blocked"] = len(blocked)
        port["blocked_pnl_usdt"] = round(sum(float(r.get("profit_abs") or 0) for r in blocked), 4)
        by_sid: dict[str, list[dict]] = defaultdict(list)
        for r in kept:
            by_sid[r["scenario_id"]].append(r)
        port["by_strategy"] = {sid: _aggregate(rows) for sid, rows in sorted(by_sid.items())}
        return port


def score_pretrained(pipe, test_df: pd.DataFrame) -> dict[str, Any]:
    features = feature_columns()
    proba = pipe.predict_proba(test_df[features])
    pred = (proba[:, 1] >= 0.5).astype(int)
    y = test_df["label"].to_numpy()
    rep = classification_report(y, pred, target_names=["loss", "profit"], output_dict=True)
    return {
        "n_test": int(len(test_df)),
        "accuracy": round(float(accuracy_score(y, pred)), 4),
        "roc_auc": round(float(roc_auc_score(y, proba[:, 1])), 4) if len(set(y)) > 1 else None,
        "loss_recall": round(float(rep["loss"]["recall"]), 4),
        "profit_recall": round(float(rep["profit"]["recall"]), 4),
        "macro_f1": round(float(rep["macro avg"]["f1-score"]), 4),
        "confusion_matrix": confusion_matrix(y, pred).tolist(),
    }


def main() -> int:
    gate_cfg = {
        "enabled": True,
        "gate_mode": "block_loss",
        "block_predicted": "loss",
        "min_confidence": 0.0,
        "grid_bots": {"gate_mode": "profit_only", "min_confidence": 0.6},
        "finder_bots": {"gate_mode": "profit_only", "min_confidence": 0.6},
        "strategy_bots": {"gate_mode": "profit_only", "min_confidence": 0.6},
    }

    records = _load_export_records()
    oos = _oos_records(records)
    oos_grid = [r for r in oos if r.get("scenario_id") == GRID_SCENARIO_ID]

    prod = ModelPack("prod_vps", PROD_PACK)
    current = ModelPack("current_local", CURRENT_PACK)

    # Unified classifier test split from newest full dataset (same rows for both models).
    all_trades = load_all_training_trades(ROOT, EXPORT_DIR)
    market = make_market_store(ROOT)
    full_df = build_dataframe(all_trades, market)
    _, test_df = time_split(full_df, test_frac=0.2)

    packs = [prod, current]
    result: dict[str, Any] = {
        "compared_at": datetime.now(tz=UTC).isoformat(),
        "gate": gate_cfg,
        "dataset": {
            "export_total": len(records),
            "export_oos": len(oos),
            "export_oos_grid": len(oos_grid),
            "classifier_test_rows": int(len(test_df)),
            "classifier_test_period": {
                "from": int(test_df["open_ms"].min()) if len(test_df) else None,
                "to": int(test_df["open_ms"].max()) if len(test_df) else None,
            },
        },
        "models": {},
        "comparison": {},
    }

    for pack in packs:
        meta = pack.meta()
        result["models"][pack.name] = {
            "path": str(pack.pack_dir),
            "trained_at": meta.get("trained_at"),
            "n_trades_train_total": meta.get("n_trades"),
            "model_key": meta.get("model_key") or meta.get("model"),
            "deployed_at": json.loads((pack.pack_dir / "deploy_meta.json").read_text(encoding="utf-8")).get("deployed_at")
            if (pack.pack_dir / "deploy_meta.json").is_file()
            else None,
            "by_scenario_on_disk": sorted(
                p.name
                for p in (pack.pack_dir / "by_scenario").iterdir()
                if p.is_dir() and (p / "pnl_classifier.joblib").is_file()
            )
            if (pack.pack_dir / "by_scenario").is_dir()
            else [],
        }
        result["comparison"][pack.name] = {
            "classifier_on_unified_test": score_pretrained(pack.global_pipe(), test_df),
            "oos_global_gate": pack.gate_portfolio(oos, gate_cfg, use_per_strategy=False),
            "oos_per_strategy_gate": pack.gate_portfolio(oos, gate_cfg, use_per_strategy=True),
            "oos_grid_only_global": pack.gate_portfolio(oos_grid, gate_cfg, use_per_strategy=False),
            "oos_grid_only_per_scenario": pack.gate_portfolio(oos_grid, gate_cfg, use_per_strategy=True),
        }

    no_ml = _aggregate(oos)
    no_ml["blocked"] = 0
    no_ml["blocked_pnl_usdt"] = 0.0
    result["comparison"]["no_ml_baseline"] = {"oos_global_gate": no_ml}

    # Pretty print
    print("=== PROD vs CURRENT models · same OOS test data ===\n")
    print(f"Export OOS: {len(oos)} signals · classifier test rows: {len(test_df)}")
    print(f"Gate: strategy/grid/finder profit_only >= 60%\n")

    print("--- Training meta ---")
    for name, info in result["models"].items():
        print(
            f"  {name}: {info.get('model_key')} · trained {str(info.get('trained_at', ''))[:10]} "
            f"· n={info.get('n_trades_train_total')} · deploy {str(info.get('deployed_at', '—'))[:10]}"
        )
        print(f"    per-scenario: {', '.join(info.get('by_scenario_on_disk') or []) or '—'}")

    print("\n--- Classifier metrics (same unified test split, 20% newest) ---")
    print(f"{'Pack':<16} {'AUC':>6} {'Acc':>6} {'LossR':>6} {'ProfR':>6} {'F1':>6} {'n':>6}")
    print("-" * 52)
    for name in ("prod_vps", "current_local"):
        m = result["comparison"][name]["classifier_on_unified_test"]
        print(
            f"{name:<16} {m.get('roc_auc') or 0:>6.3f} {m.get('accuracy') or 0:>5.1%} "
            f"{m.get('loss_recall') or 0:>5.1%} {m.get('profit_recall') or 0:>5.1%} "
            f"{m.get('macro_f1') or 0:>6.3f} {m.get('n_test') or 0:>6}"
        )

    print("\n--- OOS gate PnL (export signals, global model) ---")
    print(f"{'Pack':<16} {'Trades':>7} {'PnL':>10} {'WR':>7} {'Blocked':>8} {'BlkPnL':>10}")
    print("-" * 62)
    base = result["comparison"]["no_ml_baseline"]["oos_global_gate"]
    print(
        f"{'no_ml':<16} {base['trades']:>7} {base['pnl_usdt']:>+10.2f} "
        f"{base['win_rate']*100:>6.1f}% {base['blocked']:>8} {base['blocked_pnl_usdt']:>+10.2f}"
    )
    for name in ("prod_vps", "current_local"):
        p = result["comparison"][name]["oos_global_gate"]
        print(
            f"{name:<16} {p['trades']:>7} {p['pnl_usdt']:>+10.2f} "
            f"{p['win_rate']*100:>6.1f}% {p['blocked']:>8} {p['blocked_pnl_usdt']:>+10.2f}"
        )

    print("\n--- OOS by strategy (global model) ---")
    for sid in sorted({r["scenario_id"] for r in oos}):
        print(f"  {sid}:")
        for name in ("prod_vps", "current_local"):
            row = result["comparison"][name]["oos_global_gate"]["by_strategy"].get(sid) or _aggregate([])
            print(f"    {name:<14} {row['trades']:>4} tr  {row['pnl_usdt']:>+8.2f} USDT  WR {row['win_rate']*100:5.1f}%")

    print("\n--- Grid only OOS (per-scenario model if present) ---")
    for name in ("prod_vps", "current_local"):
        g = result["comparison"][name]["oos_grid_only_per_scenario"]
        print(f"  {name}: {g['trades']} tr · {g['pnl_usdt']:+.2f} USDT · WR {g['win_rate']*100:.1f}%")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
