#!/usr/bin/env python3
"""Copy trained ML models into simulation/models/ and update registry.json."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODELS_ROOT = ROOT / "simulation/models"
REGISTRY_PATH = MODELS_ROOT / "registry.json"
TRAIN_DIR = ROOT / "simulation/results/trade_db/models"
USER_MODEL = ROOT / "user_data/models/pnl_classifier"

MODEL_SPECS: list[dict[str, Any]] = [
    {
        "id": "global",
        "label": "Global PnL classifier (strategy + finder)",
        "train_src": TRAIN_DIR / "pnl_classifier.joblib",
        "meta_src": TRAIN_DIR / "pnl_classifier_meta.json",
        "live_src": USER_MODEL / "pnl_classifier.joblib",
    },
    {
        "id": "live_grid",
        "label": "Grid bot (VolatilityGrid)",
        "train_src": TRAIN_DIR / "by_scenario/live_grid/pnl_classifier.joblib",
        "meta_src": TRAIN_DIR / "by_scenario/live_grid/pnl_classifier_meta.json",
        "live_src": USER_MODEL / "by_scenario/live_grid/pnl_classifier.joblib",
    },
    {
        "id": "trade_finder",
        "label": "Trade Finder scanner",
        "train_src": TRAIN_DIR / "trade_finder.joblib",
        "meta_src": TRAIN_DIR / "trade_finder_meta.json",
        "live_src": ROOT / "user_data/models/trade_finder/trade_finder.joblib",
    },
]

PER_SCENARIO_IDS = [
    "lite_mean_rev",
    "lite_range",
    "lite_intraday",
    "trend_breakout",
    "trend_ema",
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _read_meta(path: Path) -> dict[str, Any]:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _copy_model_pack(model_id: str, joblib_src: Path, meta_src: Path, *, note: str = "") -> dict[str, Any]:
    if not joblib_src.is_file():
        raise FileNotFoundError(f"missing model: {joblib_src}")
    meta = _read_meta(meta_src)
    trained_at = (meta.get("trained_at") or datetime.now(tz=UTC).isoformat())[:10]
    version = f"{trained_at}_n{meta.get('n_trades', 0)}"
    dest_dir = MODELS_ROOT / model_id / version
    dest_dir.mkdir(parents=True, exist_ok=True)
    current_dir = MODELS_ROOT / model_id / "current"
    if current_dir.exists():
        if current_dir.is_symlink() or current_dir.is_file():
            current_dir.unlink()
        elif current_dir.is_dir():
            shutil.rmtree(current_dir)

    shutil.copy2(joblib_src, dest_dir / "pnl_classifier.joblib")
    if meta_src.is_file():
        shutil.copy2(meta_src, dest_dir / "pnl_classifier_meta.json")
    else:
        (dest_dir / "pnl_classifier_meta.json").write_text("{}", encoding="utf-8")

    deploy_info = {
        "model_id": model_id,
        "version": version,
        "archived_at": datetime.now(tz=UTC).isoformat(),
        "note": note,
        "source_joblib": str(joblib_src),
        "sha256_16": _sha256(dest_dir / "pnl_classifier.joblib"),
        "characteristics": {
            "model_key": meta.get("model_key") or meta.get("model"),
            "trained_at": meta.get("trained_at"),
            "n_trades": meta.get("n_trades"),
            "n_train": meta.get("n_train"),
            "n_test": meta.get("n_test"),
            "accuracy": meta.get("accuracy"),
            "roc_auc": meta.get("roc_auc"),
            "loss_recall": meta.get("loss_recall"),
            "profit_recall": meta.get("profit_recall"),
            "macro_f1": meta.get("macro_f1"),
            "train_source": meta.get("train_source") or meta.get("note"),
        },
    }
    (dest_dir / "deploy_info.json").write_text(
        json.dumps(deploy_info, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Windows: junction/symlink may need admin — use copy for current/
    current_dir.mkdir(parents=True, exist_ok=True)
    for name in ("pnl_classifier.joblib", "pnl_classifier_meta.json", "deploy_info.json"):
        shutil.copy2(dest_dir / name, current_dir / name)

    return deploy_info


def _load_registry() -> dict[str, Any]:
    if REGISTRY_PATH.is_file():
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return {"updated_at": None, "models": {}}


def sync_registry(*, only: str | None = None, note: str = "") -> dict[str, Any]:
    MODELS_ROOT.mkdir(parents=True, exist_ok=True)
    registry = _load_registry()
    registry.setdefault("models", {})
    updated: list[str] = []

    specs = [s for s in MODEL_SPECS if not only or s["id"] == only]
    for spec in specs:
        mid = spec["id"]
        src = spec["train_src"]
        if not src.is_file() and spec.get("live_src") and Path(spec["live_src"]).is_file():
            src = Path(spec["live_src"])
            meta_src = src.with_name(src.name.replace(".joblib", "_meta.json").replace("trade_finder", "trade_finder_meta"))
            if mid == "trade_finder":
                meta_src = ROOT / "user_data/models/trade_finder/trade_finder_meta.json"
            elif mid == "global":
                meta_src = USER_MODEL / "pnl_classifier_meta.json"
            else:
                meta_src = src.parent / "pnl_classifier_meta.json"
        else:
            meta_src = spec["meta_src"]
        info = _copy_model_pack(mid, src, meta_src, note=note or spec.get("label", mid))
        registry["models"][mid] = {
            "label": spec["label"],
            "current_version": info["version"],
            "current_path": f"simulation/models/{mid}/current",
            "characteristics": info["characteristics"],
            "sha256_16": info["sha256_16"],
            "archived_at": info["archived_at"],
        }
        updated.append(mid)

    if not only or only == "per_scenario":
        per: dict[str, Any] = {}
        for sid in PER_SCENARIO_IDS:
            job = TRAIN_DIR / "by_scenario" / sid / "pnl_classifier.joblib"
            meta = TRAIN_DIR / "by_scenario" / sid / "pnl_classifier_meta.json"
            if not job.is_file():
                continue
            info = _copy_model_pack(sid, job, meta, note=f"per-scenario {sid}")
            per[sid] = {
                "current_version": info["version"],
                "current_path": f"simulation/models/{sid}/current",
                "characteristics": info["characteristics"],
                "sha256_16": info["sha256_16"],
            }
        if per:
            registry["models"]["per_scenario"] = per
            updated.append("per_scenario")

    registry["updated_at"] = datetime.now(tz=UTC).isoformat()
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Registry updated: {', '.join(updated)} -> {REGISTRY_PATH}")
    return registry


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Archive ML models to simulation/models/")
    ap.add_argument("--only", default=None, help="Model id: global, live_grid, trade_finder")
    ap.add_argument("--note", default="", help="Deploy/archive note")
    args = ap.parse_args()
    sync_registry(only=args.only, note=args.note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
