"""Summarize classifier accuracy and gated win/loss for batch3 vs old winners."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "results" / "ml_param_experiments"
PER = ROOT / "per_scenario"


def is_b3(eid: str) -> bool:
    try:
        n = int(str(eid).split("_", 1)[0].replace("exp", ""))
        return 31 <= n <= 50
    except Exception:
        return False


def pack(e: dict | None) -> dict | None:
    if not e:
        return None
    c = e.get("classifier") or {}
    m = e.get("test_ml") or {}
    g = e.get("gate") or {}
    return {
        "id": e["id"],
        "pnl": m.get("pnl"),
        "n": m.get("n"),
        "wins": m.get("wins"),
        "losses": m.get("losses"),
        "winrate": m.get("winrate"),
        "avg_pnl": m.get("avg_pnl"),
        "keep_rate": g.get("keep_rate"),
        "acc": c.get("accuracy"),
        "auc": c.get("roc_auc"),
        "pp": c.get("profit_precision"),
        "pr": c.get("profit_recall"),
        "lp": c.get("loss_precision"),
        "lr": c.get("loss_recall"),
        "macro_f1": c.get("macro_f1"),
        "cm": c.get("confusion_matrix"),
    }


def best(xs: list[dict]) -> dict | None:
    return max(xs, key=lambda e: float(e.get("score") or -1e18)) if xs else None


def agg(packs: list[dict]) -> dict:
    n = sum(p["n"] or 0 for p in packs)
    w = sum(p["wins"] or 0 for p in packs)
    losses = sum(p["losses"] or 0 for p in packs)
    pnl = sum(float(p["pnl"] or 0) for p in packs)

    def mean(field: str):
        vals = [p[field] for p in packs if p.get(field) is not None]
        return sum(vals) / len(vals) if vals else None

    return {
        "scenarios": len(packs),
        "trades": n,
        "wins": w,
        "losses": losses,
        "winrate": (w / n) if n else None,
        "pnl": round(pnl, 2),
        "avg_pnl_trade": round(pnl / n, 4) if n else None,
        "mean_acc": mean("acc"),
        "mean_auc": mean("auc"),
        "mean_profit_prec": mean("pp"),
        "mean_profit_rec": mean("pr"),
        "mean_loss_prec": mean("lp"),
        "mean_keep_rate": mean("keep_rate"),
        "mean_winrate": mean("winrate"),
    }


def cm_sum(packs: list[dict]) -> dict:
    tn = fp = fn = tp = 0
    for p in packs:
        cm = p.get("cm")
        if not cm:
            continue
        tn += cm[0][0]
        fp += cm[0][1]
        fn += cm[1][0]
        tp += cm[1][1]
    total = tn + fp + fn + tp
    return {
        "tn": tn,
        "fp": fp,
        "fn": fn,
        "tp": tp,
        "acc": (tn + tp) / total if total else None,
        "profit_precision": tp / (tp + fp) if (tp + fp) else None,
        "profit_recall": tp / (tp + fn) if (tp + fn) else None,
        "loss_precision": tn / (tn + fn) if (tn + fn) else None,
        "loss_recall": tn / (tn + fp) if (tn + fp) else None,
    }


def load_top30() -> set[str]:
    pack_path = Path(__file__).resolve().parents[1] / "config" / "prod_top30_pack.json"
    if not pack_path.exists():
        return set()
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for s in pack.get("strategies") or []:
        sid = s.get("scenario_id") or s.get("id")
        if sid:
            out.add(str(sid))
    return out


def main() -> None:
    top30 = load_top30()
    rows = []
    for path in sorted(PER.glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        if top30 and d.get("scenario_id") not in top30:
            continue
        exps = d.get("experiments") or []
        by_id = {e["id"]: e for e in exps}
        old = [e for e in exps if not is_b3(e["id"])]
        b3 = [e for e in exps if is_b3(e["id"])]
        old_w = best(old)
        b3_w = best(b3)
        overall = by_id.get(d.get("winner")) or best(exps)
        raw = None
        for e in exps:
            if e.get("test_raw"):
                raw = e["test_raw"]
                break
        rows.append(
            {
                "scenario": d["scenario_id"],
                "n_train": d.get("n_train_trades"),
                "n_test": d.get("n_test_trades"),
                "raw": raw,
                "old": pack(old_w),
                "b3": pack(b3_w),
                "winner": pack(overall),
                "improved": bool(
                    b3_w and old_w and float(b3_w["score"]) > float(old_w["score"])
                ),
            }
        )
    print(f"scenarios={len(rows)} (top30 filter={'on' if top30 else 'off'})")

    old_packs = [r["old"] for r in rows if r["old"]]
    b3_packs = [r["b3"] for r in rows if r["b3"]]
    win_packs = [r["winner"] for r in rows if r["winner"]]

    print("=== AGGREGATE (30 scenarios) ===")
    for label, packs in (("old_best", old_packs), ("b3_best", b3_packs), ("overall_winner", win_packs)):
        a = agg(packs)
        cm = cm_sum(packs)
        print(
            f"\n{label}: kept={a['trades']} W={a['wins']} L={a['losses']} "
            f"WR={a['winrate']*100:.1f}% pnl={a['pnl']:.1f}"
        )
        print(
            f"  mean clf: acc={a['mean_acc']*100:.1f}% auc={a['mean_auc']:.3f} "
            f"profit_prec={a['mean_profit_prec']*100:.1f}% profit_rec={a['mean_profit_rec']*100:.1f}% "
            f"loss_prec={a['mean_loss_prec']*100:.1f}%"
        )
        print(
            f"  mean keep={a['mean_keep_rate']*100:.1f}% mean gated WR={a['mean_winrate']*100:.1f}%"
        )
        print(
            f"  confusion sum: TN={cm['tn']} FP={cm['fp']} FN={cm['fn']} TP={cm['tp']} "
            f"| acc={cm['acc']*100:.1f}% profit_prec={cm['profit_precision']*100:.1f}% "
            f"profit_rec={cm['profit_recall']*100:.1f}%"
        )

    raws = [r["raw"] for r in rows if r.get("raw")]
    if raws:
        rn = sum(x.get("n") or 0 for x in raws)
        rw = sum(x.get("wins") or 0 for x in raws)
        rl = sum(x.get("losses") or 0 for x in raws)
        rp = sum(float(x.get("pnl") or 0) for x in raws)
        print(
            f"\nRAW no-ML ({len(raws)} scen): trades={rn} W={rw} L={rl} "
            f"WR={rw / rn * 100 if rn else 0:.1f}% pnl={rp:.1f}"
        )

    print("\n=== PER SCENARIO old vs b3 ===")
    hdr = (
        f"{'scenario':22} {'old':26} {'acc':>5} {'WR':>5} {'W/L':>9} {'pnl':>7} | "
        f"{'b3':26} {'acc':>5} {'WR':>5} {'W/L':>9} {'pnl':>7} {'dWR':>6} {'dAcc':>6}"
    )
    print(hdr)
    for r in sorted(
        rows,
        key=lambda x: float((x["b3"] or {}).get("pnl") or 0)
        - float((x["old"] or {}).get("pnl") or 0),
        reverse=True,
    ):
        o, b = r["old"], r["b3"]
        if not o or not b:
            continue
        dwr = (b["winrate"] - o["winrate"]) * 100
        dacc = (b["acc"] - o["acc"]) * 100
        print(
            f"{r['scenario']:22} {o['id'][:26]:26} {o['acc']*100:5.1f} {o['winrate']*100:5.1f} "
            f"{o['wins']:4}/{o['losses']:<4} {o['pnl']:7.1f} | "
            f"{b['id'][:26]:26} {b['acc']*100:5.1f} {b['winrate']*100:5.1f} "
            f"{b['wins']:4}/{b['losses']:<4} {b['pnl']:7.1f} {dwr:+5.1f} {dacc:+5.1f}"
        )

    imp_acc = sum(
        1 for r in rows if r["old"] and r["b3"] and r["b3"]["acc"] > r["old"]["acc"]
    )
    imp_wr = sum(
        1
        for r in rows
        if r["old"] and r["b3"] and r["b3"]["winrate"] > r["old"]["winrate"]
    )
    imp_pnl = sum(1 for r in rows if r["improved"])
    print(
        f"\nBatch3 best vs old best: better PnL {imp_pnl}/{len(rows)}, "
        f"better clf acc {imp_acc}/{len(rows)}, better gated WR {imp_wr}/{len(rows)}"
    )
    improved = [r for r in rows if r["improved"] and r["old"] and r["b3"]]
    if improved:
        dwr = [(r["b3"]["winrate"] - r["old"]["winrate"]) * 100 for r in improved]
        dacc = [(r["b3"]["acc"] - r["old"]["acc"]) * 100 for r in improved]
        print(
            f"Among PnL-improved: mean dWR={sum(dwr)/len(dwr):+.1f}pp, "
            f"mean dAcc={sum(dacc)/len(dacc):+.2f}pp; "
            f"WR up {sum(1 for x in dwr if x > 0)}/{len(dwr)}, "
            f"Acc up {sum(1 for x in dacc if x > 0)}/{len(dacc)}"
        )

    out = {
        "aggregate": {
            "old_best": agg(old_packs),
            "b3_best": agg(b3_packs),
            "overall_winner": agg(win_packs),
        },
        "confusion": {
            "old_best": cm_sum(old_packs),
            "b3_best": cm_sum(b3_packs),
            "overall_winner": cm_sum(win_packs),
        },
        "improved_counts": {"pnl": imp_pnl, "acc": imp_acc, "winrate": imp_wr},
        "per_scenario": [
            {
                "scenario": r["scenario"],
                "n_test": r["n_test"],
                "old": {k: v for k, v in (r["old"] or {}).items() if k != "cm"},
                "b3": {k: v for k, v in (r["b3"] or {}).items() if k != "cm"},
                "winner": {k: v for k, v in (r["winner"] or {}).items() if k != "cm"},
                "improved": r["improved"],
            }
            for r in rows
        ],
    }
    out_path = ROOT / "batch3_accuracy_trades.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("Saved", out_path)


if __name__ == "__main__":
    main()
