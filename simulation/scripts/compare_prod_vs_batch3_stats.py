"""Compare prod top-30 pack winners vs current overall winners (after batch3)."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "config" / "prod_top30_pack.json"
PER = ROOT / "results" / "ml_param_experiments" / "per_scenario"
OUT = ROOT / "results" / "ml_param_experiments" / "prod_vs_batch3_stats.json"


def is_b3(eid: str) -> bool:
    try:
        n = int(str(eid).split("_", 1)[0].replace("exp", ""))
        return 31 <= n <= 50
    except Exception:
        return False


def metrics(e: dict) -> dict:
    c = e.get("classifier") or {}
    m = e.get("test_ml") or {}
    g = e.get("gate") or {}
    return {
        "id": e["id"],
        "pnl": m.get("pnl"),
        "n": m.get("n") or 0,
        "wins": m.get("wins") or 0,
        "losses": m.get("losses") or 0,
        "winrate": m.get("winrate"),
        "avg_pnl": m.get("avg_pnl"),
        "keep_rate": g.get("keep_rate"),
        "acc": c.get("accuracy"),
        "auc": c.get("roc_auc"),
        "pp": c.get("profit_precision"),
        "pr": c.get("profit_recall"),
        "lp": c.get("loss_precision"),
        "cm": c.get("confusion_matrix"),
    }


def agg(packs: list[dict]) -> dict:
    n = sum(p["n"] for p in packs)
    w = sum(p["wins"] for p in packs)
    losses = sum(p["losses"] for p in packs)
    pnl = sum(float(p["pnl"] or 0) for p in packs)

    def mean(field: str):
        vals = [p[field] for p in packs if p.get(field) is not None]
        return sum(vals) / len(vals) if vals else None

    return {
        "scenarios": len(packs),
        "trades": n,
        "wins": w,
        "losses": losses,
        "winrate": w / n if n else None,
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
    }


def fmt_pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def main() -> None:
    pack = json.loads(PACK.read_text(encoding="utf-8"))
    strategies = pack["strategies"]
    rows = []
    missing = []

    for s in strategies:
        sid = s["scenario_id"]
        prod_exp = s["winner_exp"]
        path = PER / f"{sid}.json"
        if not path.exists():
            missing.append(sid)
            continue
        d = json.loads(path.read_text(encoding="utf-8"))
        by_id = {e["id"]: e for e in d.get("experiments") or []}
        prod_e = by_id.get(prod_exp)
        if not prod_e:
            missing.append(f"{sid}:{prod_exp}")
            continue
        # overall winner after merge (includes batch3)
        overall_id = d.get("winner")
        overall_e = by_id.get(overall_id) or prod_e
        # also best batch3 alone
        b3 = [e for e in d.get("experiments") or [] if is_b3(e["id"])]
        b3_e = max(b3, key=lambda e: float(e.get("score") or -1e18)) if b3 else None

        prod_m = metrics(prod_e)
        new_m = metrics(overall_e)
        b3_m = metrics(b3_e) if b3_e else None
        changed = prod_m["id"] != new_m["id"]
        rows.append(
            {
                "scenario": sid,
                "rank": s.get("rank"),
                "prod_gate": s.get("min_profit_proba"),
                "prod": prod_m,
                "new": new_m,
                "b3_best": b3_m,
                "changed": changed,
                "delta_pnl": float(new_m["pnl"] or 0) - float(prod_m["pnl"] or 0),
                "delta_wr": (new_m["winrate"] or 0) - (prod_m["winrate"] or 0),
                "delta_acc": (new_m["acc"] or 0) - (prod_m["acc"] or 0),
                "delta_n": (new_m["n"] or 0) - (prod_m["n"] or 0),
            }
        )

    prod_packs = [r["prod"] for r in rows]
    new_packs = [r["new"] for r in rows]
    ap = agg(prod_packs)
    an = agg(new_packs)
    cp = cm_sum(prod_packs)
    cn = cm_sum(new_packs)

    print(f"Compared {len(rows)}/30 prod strategies  missing={missing}")
    print(f"Changed winners: {sum(1 for r in rows if r['changed'])}/30")
    print()
    print("=== PROD (current pack winners) vs NEW overall (after batch3) ===")
    for label, a, c in (("PROD", ap, cp), ("NEW ", an, cn)):
        print(
            f"{label}: kept={a['trades']}  W={a['wins']} L={a['losses']}  "
            f"WR={fmt_pct(a['winrate'])}  pnl={a['pnl']:.1f}  avg/trade={a['avg_pnl_trade']}"
        )
        print(
            f"      mean clf: acc={fmt_pct(a['mean_acc'])}  auc={a['mean_auc']:.3f}  "
            f"profit_prec={fmt_pct(a['mean_profit_prec'])}  profit_rec={fmt_pct(a['mean_profit_rec'])}"
        )
        print(
            f"      mean keep={fmt_pct(a['mean_keep_rate'])}  "
            f"CM acc={fmt_pct(c['acc'])} P-prec={fmt_pct(c['profit_precision'])} "
            f"P-rec={fmt_pct(c['profit_recall'])}"
        )

    print()
    print(
        f"DELTA: trades {an['trades'] - ap['trades']:+d}  "
        f"W {an['wins'] - ap['wins']:+d}  L {an['losses'] - ap['losses']:+d}  "
        f"WR {(an['winrate'] - ap['winrate']) * 100:+.1f}pp  "
        f"pnl {an['pnl'] - ap['pnl']:+.1f}  "
        f"acc {(an['mean_acc'] - ap['mean_acc']) * 100:+.2f}pp"
    )

    print()
    print("=== Per scenario (prod -> new) ===")
    print(
        f"{'rk':>2} {'scenario':22} {'prod_exp':24} {'new_exp':24} "
        f"{'WR':>11} {'W/L':>15} {'acc':>11} {'pnl':>13} {'dn':>5}"
    )
    for r in sorted(rows, key=lambda x: x["delta_pnl"], reverse=True):
        p, n = r["prod"], r["new"]
        mark = "*" if r["changed"] else " "
        print(
            f"{r['rank']:2}{mark}{r['scenario']:22} {p['id'][:24]:24} {n['id'][:24]:24} "
            f"{p['winrate']*100:4.1f}->{n['winrate']*100:4.1f} "
            f"{p['wins']:4}/{p['losses']:<3}->{n['wins']:4}/{n['losses']:<3} "
            f"{p['acc']*100:4.1f}->{n['acc']*100:4.1f} "
            f"{p['pnl']:5.1f}->{n['pnl']:5.1f} "
            f"{r['delta_n']:+5d}"
        )

    # raw baseline (same cache)
    raw_n = raw_w = raw_l = 0
    raw_pnl = 0.0
    for s in strategies:
        path = PER / f"{s['scenario_id']}.json"
        if not path.exists():
            continue
        d = json.loads(path.read_text(encoding="utf-8"))
        for e in d.get("experiments") or []:
            if e.get("test_raw"):
                tr = e["test_raw"]
                raw_n += tr.get("n") or 0
                raw_w += tr.get("wins") or 0
                raw_l += tr.get("losses") or 0
                raw_pnl += float(tr.get("pnl") or 0)
                break
    if raw_n:
        print()
        print(
            f"RAW no-ML (same test, 30 scen): trades={raw_n} W={raw_w} L={raw_l} "
            f"WR={raw_w/raw_n*100:.1f}% pnl={raw_pnl:.1f}"
        )

    out = {
        "prod_pack_updated": pack.get("updated"),
        "train_range": pack.get("train_range"),
        "test_range": pack.get("test_range"),
        "aggregate": {"prod": ap, "new": an, "delta": {
            "trades": an["trades"] - ap["trades"],
            "wins": an["wins"] - ap["wins"],
            "losses": an["losses"] - ap["losses"],
            "winrate_pp": round((an["winrate"] - ap["winrate"]) * 100, 2),
            "pnl": round(an["pnl"] - ap["pnl"], 2),
            "mean_acc_pp": round((an["mean_acc"] - ap["mean_acc"]) * 100, 2),
        }},
        "confusion": {"prod": cp, "new": cn},
        "changed_winners": sum(1 for r in rows if r["changed"]),
        "per_scenario": [
            {
                "scenario": r["scenario"],
                "rank": r["rank"],
                "changed": r["changed"],
                "prod": {k: v for k, v in r["prod"].items() if k != "cm"},
                "new": {k: v for k, v in r["new"].items() if k != "cm"},
                "delta_pnl": r["delta_pnl"],
                "delta_wr_pp": round(r["delta_wr"] * 100, 2),
                "delta_acc_pp": round(r["delta_acc"] * 100, 2),
                "delta_n": r["delta_n"],
            }
            for r in rows
        ],
    }
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("Saved", OUT)


if __name__ == "__main__":
    main()
