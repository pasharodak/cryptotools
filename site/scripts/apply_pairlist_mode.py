#!/usr/bin/env python3
"""Switch live bots between scanner whitelists and volume-based all-pairs mode."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODE_FILE = ROOT / "user_data/pairlist_mode.json"
ML_TRAINING_PAIRS = ROOT / "simulation/config/ml_training_pairs_whitelist.json"
RANGING_SCAN_CFG = ROOT / "user_data/ranging_scan_config.json"
STRATEGY_SCAN_CFG = ROOT / "user_data/strategy_scan_config.json"

BOT_CONFIGS: dict[str, Path] = {
    "finder": ROOT / "user_data/config.json",
    "strategy": ROOT / "user_data/config_strategy.json",
    "grid": ROOT / "user_data/config_grid.json",
}

RELOAD_URLS = {
    "finder": "http://127.0.0.1:8080/api/v1/reload_config",
    "strategy": "http://127.0.0.1:8081/api/v1/reload_config",
    "grid": "http://127.0.0.1:8082/api/v1/reload_config",
}

VOLUME_PAIRLISTS: list[dict[str, Any]] = [
    {
        "method": "VolumePairList",
        "number_assets": 200,
        "sort_key": "quoteVolume",
        "min_value": 1000000,
        "refresh_period": 86400,
    },
    {"method": "AgeFilter", "min_days_listed": 7},
    {"method": "PrecisionFilter"},
    {"method": "PriceFilter", "low_price_ratio": 0.01, "min_price": 1e-7},
]

STATIC_PAIRLISTS: list[dict[str, Any]] = [{"method": "StaticPairList"}]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_mode() -> dict[str, Any]:
    if MODE_FILE.is_file():
        return json.loads(MODE_FILE.read_text(encoding="utf-8"))
    return {"mode": "scanner", "backup": {}, "updated_at": None}


def save_mode(data: dict[str, Any]) -> None:
    MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    save_json(MODE_FILE, data)


def shared_blacklist() -> list[str]:
    grid = load_json(BOT_CONFIGS["grid"])
    bl = list(grid.get("exchange", {}).get("pair_blacklist", []))
    return sorted(set(bl))


def backup_bot(mode: dict[str, Any], bot: str, cfg: dict[str, Any]) -> None:
    ex = cfg.get("exchange", {})
    mode.setdefault("backup", {})[bot] = {
        "pairlists": cfg.get("pairlists", STATIC_PAIRLISTS),
        "pair_whitelist": list(ex.get("pair_whitelist", [])),
        "pair_blacklist": list(ex.get("pair_blacklist", [])),
    }


def set_scanner_apply(enabled: bool) -> None:
    for path in (RANGING_SCAN_CFG, STRATEGY_SCAN_CFG):
        if not path.is_file():
            continue
        cfg = load_json(path)
        cfg["apply_whitelist"] = enabled
        save_json(path, cfg)


def reload_bot(url: str, user: str, password: str) -> bool:
    import base64

    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(
        url,
        method="POST",
        headers={"Authorization": f"Basic {auth}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except urllib.error.HTTPError:
        return False
    except OSError:
        return False


def apply_all_volume(*, reload: bool, api_user: str, api_pass: str) -> dict[str, Any]:
    mode = load_mode()
    if mode.get("mode") != "all_volume":
        for bot, path in BOT_CONFIGS.items():
            if path.is_file():
                backup_bot(mode, bot, load_json(path))

    blacklist = shared_blacklist()
    changed: list[str] = []

    for bot, path in BOT_CONFIGS.items():
        if not path.is_file():
            continue
        cfg = load_json(path)
        ex = cfg.setdefault("exchange", {})
        cfg["pairlists"] = [dict(x) for x in VOLUME_PAIRLISTS]
        ex["pair_whitelist"] = []
        ex["pair_blacklist"] = list(blacklist)
        cfg["_pairlist_mode"] = "all_volume — top 200 USDT futures by 24h volume (scanners paused)"
        save_json(path, cfg)
        changed.append(bot)
        if reload and api_pass:
            ok = reload_bot(RELOAD_URLS[bot], api_user, api_pass)
            print(f"  reload {bot}: {'OK' if ok else 'failed — restart bot manually'}")

    mode["mode"] = "all_volume"
    mode["updated_at"] = datetime.now(tz=UTC).isoformat()
    save_mode(mode)
    set_scanner_apply(False)
    return {"mode": "all_volume", "bots": changed, "blacklist_count": len(blacklist)}


def load_ml_training_pairs() -> list[str]:
    if not ML_TRAINING_PAIRS.is_file():
        raise SystemExit(f"ML training whitelist not found: {ML_TRAINING_PAIRS}")
    pairs = list(load_json(ML_TRAINING_PAIRS).get("pairs") or [])
    if not pairs:
        raise SystemExit(f"ML training whitelist is empty: {ML_TRAINING_PAIRS}")
    return pairs


def apply_ml_training(*, reload: bool, api_user: str, api_pass: str) -> dict[str, Any]:
    mode = load_mode()
    if mode.get("mode") != "ml_training":
        for bot, path in BOT_CONFIGS.items():
            if path.is_file():
                backup_bot(mode, bot, load_json(path))

    pairs = load_ml_training_pairs()
    blacklist = shared_blacklist()
    changed: list[str] = []

    for bot, path in BOT_CONFIGS.items():
        if not path.is_file():
            continue
        cfg = load_json(path)
        ex = cfg.setdefault("exchange", {})
        cfg["pairlists"] = [dict(x) for x in STATIC_PAIRLISTS]
        ex["pair_whitelist"] = list(pairs)
        ex["pair_blacklist"] = list(blacklist)
        cfg["_pairlist_mode"] = (
            f"ml_training — StaticPairList {len(pairs)} pairs from ML training dataset"
        )
        save_json(path, cfg)
        changed.append(bot)
        if reload and api_pass:
            ok = reload_bot(RELOAD_URLS[bot], api_user, api_pass)
            print(f"  reload {bot}: {'OK' if ok else 'failed — restart bot manually'}")

    mode["mode"] = "ml_training"
    mode["ml_training_pairs"] = len(pairs)
    mode["updated_at"] = datetime.now(tz=UTC).isoformat()
    save_mode(mode)
    set_scanner_apply(False)
    return {"mode": "ml_training", "bots": changed, "pairs": len(pairs), "blacklist_count": len(blacklist)}


def apply_scanner(*, reload: bool, api_user: str, api_pass: str) -> dict[str, Any]:
    mode = load_mode()
    backup = mode.get("backup") or {}
    changed: list[str] = []

    for bot, path in BOT_CONFIGS.items():
        if not path.is_file() or bot not in backup:
            continue
        cfg = load_json(path)
        snap = backup[bot]
        cfg["pairlists"] = snap.get("pairlists", STATIC_PAIRLISTS)
        ex = cfg.setdefault("exchange", {})
        ex["pair_whitelist"] = list(snap.get("pair_whitelist", []))
        ex["pair_blacklist"] = list(snap.get("pair_blacklist", ex.get("pair_blacklist", [])))
        cfg["_pairlist_mode"] = "scanner whitelist — restored from backup"
        save_json(path, cfg)
        changed.append(bot)
        if reload and api_pass:
            ok = reload_bot(RELOAD_URLS[bot], api_user, api_pass)
            print(f"  reload {bot}: {'OK' if ok else 'failed — restart bot manually'}")

    mode["mode"] = "scanner"
    mode["updated_at"] = datetime.now(tz=UTC).isoformat()
    save_mode(mode)
    set_scanner_apply(True)
    return {"mode": "scanner", "bots": changed}


def main() -> int:
    parser = argparse.ArgumentParser(description="Toggle pairlist mode for live bots")
    parser.add_argument(
        "--mode",
        choices=("all_volume", "scanner", "ml_training"),
        default="all_volume",
        help="all_volume | ml_training (ML dataset pairs) | scanner (restore backup)",
    )
    parser.add_argument("--no-reload", action="store_true")
    parser.add_argument("--api-user", default="")
    parser.add_argument("--api-pass", default="")
    args = parser.parse_args()

    api_user = args.api_user or __import__("os").environ.get("FREQUI_USERNAME", "cryptotools")
    api_pass = args.api_pass or __import__("os").environ.get("FREQUI_PASSWORD", "")

    if args.mode == "all_volume":
        result = apply_all_volume(reload=not args.no_reload, api_user=api_user, api_pass=api_pass)
        print("All-volume mode ON — scanners will not overwrite whitelists")
    elif args.mode == "ml_training":
        result = apply_ml_training(reload=not args.no_reload, api_user=api_user, api_pass=api_pass)
        print(f"ML training whitelist ON — {result['pairs']} pairs, scanners paused")
    else:
        result = apply_scanner(reload=not args.no_reload, api_user=api_user, api_pass=api_pass)
        print("Scanner whitelist mode restored")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
