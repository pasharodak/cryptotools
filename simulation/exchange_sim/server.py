#!/usr/bin/env python3
"""Exchange sim API + replay player UI."""
from __future__ import annotations

import asyncio
import functools
import json
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.exchange_sim.bot_session import BotSessionManager, load_bot_scenarios, load_player_profile
from simulation.exchange_sim.datastore import HistoricalDatastore
from simulation.exchange_sim.engine import SimulationEngine
from simulation.exchange_sim.player import ReplayPlayer
from simulation.ml.pnl_classifier import META_FILE, MODEL_DIR, load_model, make_market_store, predict_entry

MANIFEST = ROOT / "simulation" / "config" / "manifest.json"
PLAYER_DIR = ROOT / "simulation" / "player"
EMA_STUDY_EXPORT = ROOT / "simulation/results/ema_only_study/export"


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8-sig"))


def create_app() -> FastAPI:
    cfg = load_manifest()
    datadir = ROOT / cfg["ctengine_datadir"]
    ds = HistoricalDatastore(datadir, exchange=cfg.get("exchange", "bybit"))
    engine = SimulationEngine(datastore=ds, wallet_usdt=float(cfg.get("starting_balance_usdt", 100)))
    player = ReplayPlayer(engine, ds)
    ws_clients: list[WebSocket] = []

    async def _safe_send(ws: WebSocket, msg: str) -> None:
        try:
            await ws.send_text(msg)
        except Exception:
            if ws in ws_clients:
                ws_clients.remove(ws)

    loop_holder: dict[str, asyncio.AbstractEventLoop | None] = {"loop": None}

    async def broadcast(msg: str) -> None:
        for ws in list(ws_clients):
            await _safe_send(ws, msg)

    def schedule_broadcast(payload: dict) -> None:
        loop = loop_holder.get("loop")
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(
                broadcast(json.dumps(payload, default=str)),
                loop,
            )

    def on_bot_update(status: dict) -> None:
        player.set_bots_status(status.get("running", False), status)
        slim = {k: v for k, v in status.items() if k != "scan_events"}
        schedule_broadcast({"type": "bots", **slim})

    bot_session = BotSessionManager(ROOT, on_update=on_bot_update)

    def enrich_tick(snap: dict) -> dict:
        sim_ms = snap.get("current_ms") or snap.get("sim_ms") or 0
        active_sid = snap.get("active_scenario_id") or player.state.active_scenario_id
        if bot_session.instances and sim_ms:
            if active_sid:
                snap["bots_all"] = bot_session.runtime_for_scenario(active_sid, sim_ms)
            else:
                snap["bots_all"] = bot_session.runtime_all(sim_ms)
            snap["scan_events_visible"] = bot_session.events_until(sim_ms)
            if snap.get("pair"):
                snap["bots_runtime"] = bot_session.runtime_for_pair(snap["pair"], sim_ms)
        if active_sid:
            snap["active_scenario_id"] = active_sid
        snap["sequential_replay"] = player.state.sequential_replay
        if active_sid and sim_ms:
            snap["live_trades"] = bot_session.visible_trades_for_scenario(active_sid, sim_ms)
        return snap

    def on_sim_step(prev_ms: int, cur_ms: int) -> None:
        active_sid = player.state.active_scenario_id
        if active_sid:
            for tev in bot_session.trade_events_between(active_sid, prev_ms, cur_ms):
                schedule_broadcast({"type": "trade_live", "sim_ms": cur_ms, **tev})
        for ev in bot_session.events_between(prev_ms, cur_ms):
            if not active_sid or ev.get("scenario_id") == active_sid:
                schedule_broadcast({"type": "scan_arm", "sim_ms": cur_ms, **ev})

    player.on_sim_step(on_sim_step)

    def on_tick_event(ev: dict) -> None:
        sim_ms = ev.get("current_ms") or ev.get("sim_ms") or 0
        active_sid = player.state.active_scenario_id
        if sim_ms and bot_session.instances:
            if active_sid:
                ev["bots_all"] = bot_session.runtime_for_scenario(active_sid, sim_ms)
            else:
                ev["bots_all"] = bot_session.runtime_all(sim_ms)
            ev["scan_events_visible"] = bot_session.events_until(sim_ms)
            if ev.get("pair"):
                ev["bots_runtime"] = bot_session.runtime_for_pair(ev["pair"], sim_ms)
        if active_sid:
            ev["active_scenario_id"] = active_sid
        ev["sequential_replay"] = player.state.sequential_replay
        if active_sid and sim_ms:
            ev["live_trades"] = bot_session.visible_trades_for_scenario(active_sid, sim_ms)
        schedule_broadcast(ev)

    player.on_tick(on_tick_event)

    async def stream_scenario_trades(
        scenario_id: str,
        *,
        label: str = "",
        batch_size: int = 12,
        pause_sec: float = 0.02,
    ) -> None:
        """Push gated trades into UI in chunks (no sim-clock replay)."""
        all_trades = bot_session.trades_payload_for_scenario(scenario_id)
        bot_session.begin_strategy_walkthrough(scenario_id)
        if not all_trades:
            bot_session.mark_strategy_walkthrough_done(scenario_id)
            return
        revealed: list[dict[str, Any]] = []
        for i in range(0, len(all_trades), batch_size):
            revealed = all_trades[: i + batch_size]
            schedule_broadcast(
                {
                    "type": "batch_trades",
                    "scenario_id": scenario_id,
                    "label": label,
                    "trades": revealed,
                    "total": len(all_trades),
                    "revealed": len(revealed),
                }
            )
            if pause_sec > 0:
                await asyncio.sleep(pause_sec)
        bot_session.mark_strategy_walkthrough_done(scenario_id)

    async def run_batch_prgon(
        start_ms: int,
        end_ms: int,
        pairs: list[str],
        *,
        workers: int = 4,
    ) -> None:
        """Fast batch: parallel bot calc, trades stream into cards without realtime clock."""
        order = bot_session.enabled_scenario_ids()
        if not order:
            schedule_broadcast({"type": "phase", "phase": "sequential_done"})
            return
        player.state.sequential_replay = True
        player.state.batch_run = True
        total_bots = len(order)
        workers = max(1, min(int(workers), 8, total_bots))
        try:
            await asyncio.to_thread(bot_session.init_live_session, pairs, start_ms, end_ms, datadir)
            pool = bot_session.status.get("sim_pool") or pairs
            schedule_broadcast(
                {
                    "type": "phase",
                    "phase": "prgon_start",
                    "strategies": total_bots,
                    "pairs": len(pool),
                    "workers": workers,
                    "sim_ms": start_ms,
                }
            )
            sem = asyncio.Semaphore(workers)
            progress = {"done": 0}

            async def run_one(idx: int, sid: str) -> None:
                sc = next((s for s in bot_session.scenarios if s["id"] == sid), {})
                label = sc.get("label", sid)
                eligible = await asyncio.to_thread(bot_session.pairs_for_scenario, sid, pool)
                if not eligible:
                    schedule_broadcast(
                        {
                            "type": "phase",
                            "phase": "strategy_skip",
                            "scenario_id": sid,
                            "label": label,
                            "reason": "no_eligible_pairs",
                        }
                    )
                    return
                async with sem:
                    player.state.active_scenario_id = sid
                    schedule_broadcast(
                        {
                            "type": "phase",
                            "phase": "strategy_loading",
                            "scenario_id": sid,
                            "label": label,
                            "index": idx + 1,
                            "total": total_bots,
                            "pairs_total": len(eligible),
                        }
                    )
                    try:
                        await asyncio.to_thread(
                            bot_session.load_scenario_instances,
                            sid,
                            pool,
                            start_ms,
                            end_ms,
                            datadir,
                        )
                    except Exception as exc:
                        schedule_broadcast(
                            {
                                "type": "phase",
                                "phase": "strategy_error",
                                "scenario_id": sid,
                                "label": label,
                                "message": str(exc),
                            }
                        )
                        return
                    await stream_scenario_trades(sid, label=label)
                    progress["done"] += 1
                    schedule_broadcast(
                        {
                            "type": "phase",
                            "phase": "strategy_done",
                            "scenario_id": sid,
                            "label": label,
                            "index": progress["done"],
                            "total": total_bots,
                        }
                    )

            await asyncio.gather(*(run_one(idx, sid) for idx, sid in enumerate(order)))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            bot_session.status["phase"] = "error"
            bot_session.status["error"] = str(exc)
            schedule_broadcast({"type": "phase", "phase": "error", "message": str(exc)})
        finally:
            archive_result = None
            if bot_session.is_running():
                archive_result = await asyncio.to_thread(
                    functools.partial(
                        bot_session.finish_live_session, persist_trades=True, source="prgon"
                    )
                )
            player.state.active_scenario_id = None
            player.state.sequential_replay = False
            player.state.batch_run = False
            player._sequential_task = None
            if archive_result:
                schedule_broadcast(
                    {
                        "type": "phase",
                        "phase": "archive_done",
                        "run_id": archive_result.get("run_id"),
                        "trades_saved": archive_result.get("trades_saved"),
                        "trades_skipped_duplicate": archive_result.get("trades_skipped_duplicate"),
                        "net_usdt": archive_result.get("net_usdt"),
                        "report_path": archive_result.get("report_path"),
                    }
                )
            schedule_broadcast({"type": "phase", "phase": "sequential_done"})

    async def run_live_walkthrough(
        start_ms: int, end_ms: int, tf: str, chart_pair: str, pairs: list[str]
    ) -> None:
        """Each enabled bot walks every eligible pair; trades appear in card as sim time advances."""
        order = bot_session.enabled_scenario_ids()
        if not order:
            schedule_broadcast({"type": "phase", "phase": "sequential_done"})
            return
        player.state.sequential_replay = True
        total_bots = len(order)
        try:
            await asyncio.to_thread(bot_session.init_live_session, pairs, start_ms, end_ms, datadir)
            pool = bot_session.status.get("sim_pool") or pairs
            schedule_broadcast(
                {
                    "type": "phase",
                    "phase": "sim_start",
                    "strategies": total_bots,
                    "pairs": len(pool),
                    "sim_ms": start_ms,
                }
            )
            await asyncio.sleep(0.5)
            for idx, sid in enumerate(order):
                sc = next((s for s in bot_session.scenarios if s["id"] == sid), {})
                player.state.active_scenario_id = sid
                eligible = await asyncio.to_thread(bot_session.pairs_for_scenario, sid, pool)
                if not eligible:
                    schedule_broadcast(
                        {
                            "type": "phase",
                            "phase": "strategy_skip",
                            "scenario_id": sid,
                            "label": sc.get("label", sid),
                            "reason": "no_eligible_pairs",
                        }
                    )
                    continue
                schedule_broadcast(
                    {
                        "type": "phase",
                        "phase": "strategy_replay",
                        "scenario_id": sid,
                        "label": sc.get("label", sid),
                        "index": idx + 1,
                        "total": total_bots,
                        "pairs_total": len(eligible),
                        "sim_ms": start_ms,
                    }
                )
                bot_session.begin_strategy_walkthrough(sid)
                schedule_broadcast(
                    {
                        "type": "phase",
                        "phase": "strategy_loading",
                        "scenario_id": sid,
                        "label": sc.get("label", sid),
                        "index": idx + 1,
                        "total": total_bots,
                        "pairs_total": len(eligible),
                    }
                )
                await asyncio.to_thread(
                    bot_session.load_scenario_instances, sid, pool, start_ms, end_ms, datadir
                )
                replay_pairs = [
                    i["pair"]
                    for i in bot_session.instances
                    if i["scenario_id"] == sid
                ]
                await asyncio.sleep(0.2)
                for pidx, pair in enumerate(replay_pairs):
                    bot_session.begin_pair_walkthrough(sid, pair)
                    player.configure(pair, start_ms, end_ms, tf, start_ms)
                    schedule_broadcast(
                        {
                            "type": "phase",
                            "phase": "pair_replay",
                            "scenario_id": sid,
                            "label": sc.get("label", sid),
                            "pair": pair,
                            "bot_index": idx + 1,
                            "bots_total": total_bots,
                            "pair_index": pidx + 1,
                            "pairs_total": len(replay_pairs),
                            "sim_ms": start_ms,
                        }
                    )
                    await asyncio.sleep(0.05)
                    await player.play_through()
                    bot_session.finish_pair_walkthrough(sid, pair)
                    schedule_broadcast(
                        {
                            "type": "phase",
                            "phase": "pair_done",
                            "scenario_id": sid,
                            "pair": pair,
                            "pair_index": pidx + 1,
                            "pairs_total": len(replay_pairs),
                        }
                    )
                schedule_broadcast(
                    {
                        "type": "phase",
                        "phase": "strategy_done",
                        "scenario_id": sid,
                        "label": sc.get("label", sid),
                        "index": idx + 1,
                        "total": total_bots,
                    }
                )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            bot_session.status["phase"] = "error"
            bot_session.status["error"] = str(exc)
            schedule_broadcast({"type": "phase", "phase": "error", "message": str(exc)})
        finally:
            archive_result = None
            if bot_session.is_running():
                archive_result = await asyncio.to_thread(
                    functools.partial(
                        bot_session.finish_live_session, persist_trades=True, source="play"
                    )
                )
            player.state.active_scenario_id = None
            player.state.sequential_replay = False
            player._sequential_task = None
            if archive_result:
                schedule_broadcast(
                    {
                        "type": "phase",
                        "phase": "archive_done",
                        "run_id": archive_result.get("run_id"),
                        "trades_saved": archive_result.get("trades_saved"),
                        "trades_skipped_duplicate": archive_result.get("trades_skipped_duplicate"),
                        "net_usdt": archive_result.get("net_usdt"),
                        "report_path": archive_result.get("report_path"),
                    }
                )
            schedule_broadcast({"type": "phase", "phase": "sequential_done"})

    app = FastAPI(title="CriptoTools Sim Player", version="2.0")

    @app.on_event("startup")
    async def _capture_loop() -> None:
        loop_holder["loop"] = asyncio.get_running_loop()

    @app.get("/")
    def index():
        return FileResponse(PLAYER_DIR / "index.html")

    @app.get("/health")
    def health():
        pairs = cfg.get("player_pairs") or ds.list_pairs("1s") or ds.list_pairs("5m")
        return {"status": "ok", "mode": "simulation", "pairs": len(pairs)}

    # --- Player data ---
    @app.get("/sim/profile")
    def sim_profile():
        profile = load_player_profile(ROOT)
        scenarios = json.loads((ROOT / "simulation/config/player_scenarios.json").read_text(encoding="utf-8")) if (
            ROOT / "simulation/config/player_scenarios.json"
        ).is_file() else []
        stakes = sorted({float(sc.get("stake_usdt") or 0) for sc in scenarios if sc.get("stake_usdt")})
        return {
            "wallet_usdt": profile.get("wallet_usdt", cfg.get("starting_balance_usdt", 100)),
            "stake_usdt": profile.get("stake_usdt", 50),
            "stake_mode": profile.get("stake_mode", "scenario"),
            "scenario_stakes_usdt": stakes,
            "max_open_trades": profile.get("max_open_trades", 2),
            "target_monthly_pct": profile.get("target_monthly_pct", 30),
        }

    PROFILE_PATH = ROOT / "simulation" / "config" / "player_profile.json"

    @app.patch("/sim/profile")
    def patch_profile(body: dict):
        profile = load_player_profile(ROOT)
        if "stake_usdt" in body:
            stake = float(body["stake_usdt"])
            if stake <= 0 or stake > 10_000:
                raise HTTPException(400, "stake_usdt must be between 0 and 10000")
            profile["stake_usdt"] = stake
        if "stake_mode" in body:
            mode = str(body["stake_mode"])
            if mode not in ("scenario", "uniform"):
                raise HTTPException(400, "stake_mode must be scenario or uniform")
            profile["stake_mode"] = mode
        if "wallet_usdt" in body:
            profile["wallet_usdt"] = float(body["wallet_usdt"])
        if "max_open_trades" in body:
            profile["max_open_trades"] = int(body["max_open_trades"])
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_PATH.write_text(json.dumps(profile, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        bot_session.mark_stale()
        return sim_profile()

    SCENARIOS_PATH = ROOT / "simulation" / "config" / "player_scenarios.json"

    def _read_scenarios() -> list[dict]:
        if not SCENARIOS_PATH.is_file():
            return []
        return json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))

    def _write_scenarios(scenarios: list[dict]) -> None:
        SCENARIOS_PATH.write_text(
            json.dumps(scenarios, indent=4, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        bot_session.scenarios = load_bot_scenarios(ROOT)
        bot_session.mark_stale()

    @app.get("/sim/scenarios")
    def sim_scenarios():
        return {"scenarios": _read_scenarios()}

    @app.patch("/sim/scenarios/{scenario_id}")
    def patch_scenario(scenario_id: str, body: dict):
        enabled = body.get("enabled")
        stake_usdt = body.get("stake_usdt")
        if enabled is None and stake_usdt is None:
            raise HTTPException(400, "enabled or stake_usdt required")
        scenarios = _read_scenarios()
        found = False
        for sc in scenarios:
            if sc["id"] == scenario_id:
                if enabled is not None:
                    sc["enabled"] = bool(enabled)
                if stake_usdt is not None:
                    stake = float(stake_usdt)
                    if stake <= 0 or stake > 10_000:
                        raise HTTPException(400, "stake_usdt must be between 0 and 10000")
                    sc["stake_usdt"] = stake
                found = True
                break
        if not found:
            raise HTTPException(404, "scenario not found")
        _write_scenarios(scenarios)
        return {"ok": True, "id": scenario_id, "enabled": bool(enabled) if enabled is not None else None, "stake_usdt": stake_usdt}

    @app.post("/sim/scenarios/enabled-bulk")
    def scenarios_enabled_bulk(body: dict):
        enabled = body.get("enabled")
        if enabled is None:
            raise HTTPException(400, "enabled required")
        ids = body.get("ids")
        scenarios = _read_scenarios()
        updated = 0
        for sc in scenarios:
            if ids is not None and sc["id"] not in ids:
                continue
            sc["enabled"] = bool(enabled)
            updated += 1
        _write_scenarios(scenarios)
        return {"ok": True, "updated": updated, "enabled": bool(enabled)}

    @app.get("/sim/pairs")
    def sim_pairs():
        pool_path = ROOT / "simulation" / "config" / "player_pair_pool.json"
        if pool_path.is_file():
            pool = json.loads(pool_path.read_text(encoding="utf-8")).get("pairs") or []
            if pool:
                return {"pairs": pool}
        configured = cfg.get("player_pairs") or []
        discovered = ds.list_pairs("1s") or ds.list_pairs("1m") or ds.list_pairs("5m")
        merged = list(dict.fromkeys(configured + discovered))
        return {"pairs": merged}

    @app.get("/sim/range")
    def sim_range(pair: str = Query(...), timeframe: str = Query("1s")):
        return {"pair": pair, "timeframe": timeframe, **ds.pair_range(pair, timeframe)}

    @app.get("/sim/chart")
    def sim_chart(
        pair: str = Query(...),
        timeframe: str = Query("1s"),
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = Query(5000, le=20000),
    ):
        try:
            candles = ds.chart_slice(pair, timeframe, start_ms, end_ms, limit)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"pair": pair, "timeframe": timeframe, "candles": candles}

    def _load_ema_study_trades() -> list[dict]:
        rows: list[dict] = []
        if not EMA_STUDY_EXPORT.is_dir():
            return rows
        for name in ("all_wins.jsonl", "all_losses.jsonl"):
            path = EMA_STUDY_EXPORT / name
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        rows.sort(key=lambda r: (r.get("trade") or {}).get("open_ms") or 0)
        return rows

    @app.get("/sim/ema-study/trades")
    def ema_study_trades(pair: str | None = None):
        rows = _load_ema_study_trades()
        if pair:
            rows = [r for r in rows if r.get("pair") == pair]
        out = []
        for rec in rows:
            tr = rec.get("trade") or {}
            out.append(
                {
                    "pair": rec.get("pair"),
                    "label": rec.get("label"),
                    "profit_abs": float(rec.get("profit_abs") or 0),
                    "open_ms": tr.get("open_ms"),
                    "close_ms": tr.get("close_ms"),
                    "open_rate": tr.get("open_rate"),
                    "close_rate": tr.get("close_rate"),
                    "is_short": bool(tr.get("is_short")),
                    "exit_reason": tr.get("exit_reason"),
                    "duration_min": tr.get("duration_min"),
                }
            )
        pairs = sorted({r["pair"] for r in out if r.get("pair")})
        pair_counts = {}
        for r in out:
            p = r.get("pair")
            if p:
                pair_counts[p] = pair_counts.get(p, 0) + 1
        pairs = sorted(pair_counts.keys(), key=lambda p: (-pair_counts[p], p))
        wins = sum(1 for r in out if r["profit_abs"] >= 0)
        net = sum(r["profit_abs"] for r in out)
        return {
            "scenario": "trend_ema",
            "strategy": "SimEmaGoldenCross",
            "pairs": pairs,
            "pair_counts": pair_counts,
            "summary": {
                "trades": len(out),
                "wins": wins,
                "losses": len(out) - wins,
                "net_usdt": round(net, 4),
            },
            "trades": out,
        }

    @app.get("/ema")
    def ema_chart_page():
        path = PLAYER_DIR / "ema_chart.html"
        if not path.is_file():
            raise HTTPException(404, "ema_chart.html missing")
        return FileResponse(path)

    @app.get("/sim/player/state")
    def player_state():
        return player.snapshot()

    @app.post("/sim/player/configure")
    def player_configure(body: dict):
        pair = body.get("pair", "")
        if not pair:
            raise HTTPException(400, "pair required")
        tf = body.get("timeframe", "1s")
        start_ms = int(body.get("range_start_ms", 0))
        end_ms = int(body.get("range_end_ms", 0))
        if not start_ms or not end_ms:
            rng = ds.pair_range(pair, tf)
            start_ms = rng["start_ms"] or start_ms
            end_ms = rng["end_ms"] or end_ms
        return player.configure(pair, start_ms, end_ms, tf, body.get("start_ms"))

    @app.post("/sim/player/seek")
    def player_seek(body: dict):
        snap = player.seek(int(body.get("timestamp_ms", 0)))
        return enrich_tick(snap)

    @app.post("/sim/player/play")
    async def player_play(body: dict | None = None):
        body = body or {}
        try:
            pairs = body.get("pairs") or cfg.get("player_pairs") or []
            tf = body.get("timeframe", "1s")
            start_ms = int(body.get("range_start_ms", 0))
            end_ms = int(body.get("range_end_ms", 0))

            if body.get("pair"):
                pair = body["pair"]
                if not start_ms or not end_ms:
                    rng = ds.pair_range(pair, tf)
                    start_ms = start_ms or rng.get("start_ms") or 0
                    end_ms = end_ms or rng.get("end_ms") or 0
                if start_ms and end_ms:
                    player.configure(pair, start_ms, end_ms, tf, start_ms)
            elif start_ms and end_ms and player.state.pair:
                player.configure(player.state.pair, start_ms, end_ms, tf, start_ms)

            if not start_ms or not end_ms or not pairs:
                raise HTTPException(400, "pair, period and pairs required — load chart first")

            if body.get("speed"):
                player.set_speed(float(body["speed"]))

            player.stop_replay_only()
            player.cancel_sequential()
            bot_session.scenarios = load_bot_scenarios(ROOT)
            bot_session.reset()
            bot_session.select(None)

            chart_pair = body.get("pair") or player.state.pair
            player.configure(chart_pair, start_ms, end_ms, tf, start_ms)
            schedule_broadcast(
                {
                    "type": "phase",
                    "phase": "sim_start",
                    "sim_ms": start_ms,
                    "sequential": True,
                    "strategies": len(bot_session.enabled_scenario_ids()),
                    "pairs": len(pairs),
                }
            )

            player._sequential_task = asyncio.create_task(
                run_live_walkthrough(start_ms, end_ms, tf, chart_pair, pairs)
            )
            snap = enrich_tick(player.snapshot())
            snap["sequential"] = True
            return snap
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, f"play failed: {exc}") from exc

    @app.post("/sim/player/prgon")
    async def player_prgon(body: dict | None = None):
        body = body or {}
        try:
            pairs = body.get("pairs") or cfg.get("player_pairs") or []
            start_ms = int(body.get("range_start_ms", 0))
            end_ms = int(body.get("range_end_ms", 0))
            workers = int(body.get("workers", 4))

            if body.get("pair"):
                pair = body["pair"]
                if not start_ms or not end_ms:
                    rng = ds.pair_range(pair, "1s")
                    start_ms = start_ms or rng.get("start_ms") or 0
                    end_ms = end_ms or rng.get("end_ms") or 0
            elif start_ms and end_ms and player.state.pair:
                pair = player.state.pair
            else:
                pair = player.state.pair

            if not start_ms or not end_ms or not pairs:
                raise HTTPException(400, "pair, period and pairs required — load chart first")

            player.stop_replay_only()
            player.cancel_sequential()
            bot_session.scenarios = load_bot_scenarios(ROOT)
            bot_session.reset()
            bot_session.select(None)

            if pair and start_ms and end_ms:
                player.configure(pair, start_ms, end_ms, "1s", start_ms)

            schedule_broadcast(
                {
                    "type": "phase",
                    "phase": "prgon_start",
                    "strategies": len(bot_session.enabled_scenario_ids()),
                    "pairs": len(pairs),
                    "workers": max(1, min(workers, 8)),
                }
            )

            player._sequential_task = asyncio.create_task(
                run_batch_prgon(start_ms, end_ms, pairs, workers=workers)
            )
            snap = enrich_tick(player.snapshot())
            snap["batch"] = True
            snap["workers"] = max(1, min(workers, 8))
            return snap
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, f"prgon failed: {exc}") from exc

    @app.post("/sim/player/pause")
    def player_pause():
        return player.pause()

    @app.post("/sim/player/stop")
    def player_stop():
        return player.stop()

    @app.post("/sim/player/reset")
    def player_reset(body: dict | None = None):
        body = body or {}
        scope = body.get("scope", "all")
        out: dict = {"ok": True, "scope": scope}

        if scope in ("replay", "all"):
            player.pause()
            out["player"] = enrich_tick(player.reset_replay())

        if scope in ("bots", "all"):
            bot_session.reset()
            bot_session.select(None)
            player.set_bots_status(False, {"state": "idle"})
            out["bots"] = bot_session.snapshot()

        if body.get("clear_probe_cache"):
            cache = ROOT / "simulation" / "results" / "selected_player_pairs.json"
            if cache.is_file():
                cache.unlink()
            out["probe_cache_cleared"] = True

        schedule_broadcast({"type": "phase", "phase": "reset", "scope": scope})
        return out

    @app.post("/sim/player/speed")
    def player_speed(body: dict):
        return player.set_speed(float(body.get("speed", 1)))

    @app.post("/sim/bots/run-all")
    def bots_run_all(body: dict):
        if bot_session.is_running():
            return {"ok": False, "error": "already running", "status": bot_session.status}
        pairs = body.get("pairs") or cfg.get("player_pairs") or []
        start_ms = int(body.get("range_start_ms", player.state.range_start_ms))
        end_ms = int(body.get("range_end_ms", player.state.range_end_ms))
        if not pairs or not start_ms or not end_ms:
            raise HTTPException(400, "pairs and date range required")
        player.pause()
        bot_session.select(None)
        player.set_bots_status(True, {"state": "starting", "phase": "backtesting"})
        bot_session.run_all(pairs, start_ms, end_ms, datadir)
        snap = enrich_tick(player.snapshot())
        return {"ok": True, "player": snap, "timerange_ms": [start_ms, end_ms], "pairs": pairs}

    @app.get("/sim/bots/status")
    def bots_status():
        return bot_session.snapshot()

    @app.post("/sim/bots/invalidate")
    def bots_invalidate():
        bot_session.mark_stale()
        return {"ok": True, "phase": bot_session.status.get("phase")}

    @app.get("/sim/bots/instances")
    def bots_instances(pair: str | None = None):
        return {"instances": bot_session.get_instances(pair), "selected_id": bot_session.status.get("selected_id")}

    @app.post("/sim/bots/select")
    def bots_select(body: dict):
        iid = body.get("id")
        inst = next((i for i in bot_session.instances if i["id"] == iid), None)
        if iid and not inst:
            raise HTTPException(404, "bot not found")
        result = bot_session.select(iid)
        if inst and inst["pair"] != player.state.pair:
            player.configure(
                inst["pair"],
                player.state.range_start_ms,
                player.state.range_end_ms,
                player.state.timeframe,
                player.state.current_ms,
            )
        rt = bot_session.runtime_for_pair(player.state.pair, player.state.current_ms)
        return {"ok": True, "session": result, "bots_runtime": rt, "pair": player.state.pair}

    # --- Legacy exchange API ---
    @app.post("/v5/market/time/set")
    def set_time(body: dict):
        ts = int(body.get("timestamp_ms", 0))
        engine.set_clock(ts)
        return {"retCode": 0, "result": engine.snapshot()}

    @app.get("/v5/market/kline")
    def kline(
        symbol: str = Query(...),
        interval: str = Query("5"),
        start: int | None = None,
        limit: int = Query(200, le=1000),
    ):
        tf = f"{interval}m" if interval.isdigit() else interval
        pair = symbol if "/" in symbol else f"{symbol.replace('USDT', '')}/USDT:USDT"
        try:
            candles = ds.chart_slice(pair, tf, start, None, limit)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        rows = []
        for c in candles[-limit:]:
            rows.append(
                [
                    str(c["time"] * 1000),
                    str(c["open"]),
                    str(c["high"]),
                    str(c["low"]),
                    str(c["close"]),
                    str(c["volume"]),
                    "0",
                ]
            )
        return {"retCode": 0, "result": {"list": rows}}

    @app.get("/v5/account/wallet-balance")
    def wallet():
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "accountType": "UNIFIED",
                        "coin": [
                            {
                                "coin": "USDT",
                                "walletBalance": str(engine.equity()),
                                "availableToWithdraw": str(engine.wallet_usdt),
                            }
                        ],
                    }
                ]
            },
        }

    @app.post("/v5/order/create")
    def order_create(body: dict):
        symbol = body.get("symbol", "")
        side = body.get("side", "Buy")
        qty = float(body.get("qty", 0))
        if qty <= 0:
            return {"retCode": 10001, "retMsg": "invalid qty"}
        if engine.clock_ms <= 0:
            return {"retCode": 10002, "retMsg": "set clock first"}
        o = engine.place_market(symbol, side, qty)
        return {
            "retCode": 0,
            "result": {
                "orderId": o.order_id,
                "symbol": symbol,
                "side": side,
                "orderStatus": o.status,
                "avgPrice": str(o.price),
            },
        }

    @app.get("/v5/position/list")
    def positions():
        items = [
            {
                "symbol": p.symbol,
                "side": p.side,
                "size": str(p.size),
                "avgPrice": str(p.entry_price),
                "unrealisedPnl": str(p.unrealized_pnl),
            }
            for p in engine.positions.values()
        ]
        return {"retCode": 0, "result": {"list": items}}

    @app.get("/sim/snapshot")
    def snapshot():
        return engine.snapshot()

    def _ml_meta_path() -> Path:
        return ROOT / MODEL_DIR / META_FILE

    def _ml_model():
        try:
            return load_model(ROOT)
        except FileNotFoundError:
            return None

    _ml_market_store: dict[str, Any] = {"store": None}

    def _ml_market():
        if _ml_market_store["store"] is None:
            _ml_market_store["store"] = make_market_store(ROOT)
        return _ml_market_store["store"]

    @app.get("/sim/ml/pnl/status")
    def ml_pnl_status():
        meta_path = _ml_meta_path()
        if not meta_path.is_file():
            return {"ready": False, "message": "Модель не обучена. Запустите train_pnl_classifier.py"}
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return {"ready": True, **meta}

    @app.get("/sim/ml/pnl/predictions")
    def ml_pnl_predictions():
        path = ROOT / MODEL_DIR / "predictions.json"
        if not path.is_file():
            raise HTTPException(404, "predictions.json not found — train model first")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.post("/sim/ml/pnl/predict")
    def ml_pnl_predict(body: dict):
        pipe = _ml_model()
        if pipe is None:
            raise HTTPException(503, "model not trained")
        return predict_entry(pipe, body, _ml_market())

    @app.get("/sim/ml/entry-gate")
    def ml_entry_gate_get():
        return bot_session.ml_gate_status()

    @app.post("/sim/ml/entry-gate")
    def ml_entry_gate_set(body: dict):
        enabled = bool(body.get("enabled", True))
        return bot_session.set_ml_gate_enabled(enabled)

    @app.websocket("/sim/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        ws_clients.append(websocket)
        try:
            await websocket.send_text(
                json.dumps({"type": "hello", **enrich_tick(player.snapshot())}, default=str)
            )
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            if websocket in ws_clients:
                ws_clients.remove(websocket)

    if PLAYER_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(PLAYER_DIR)), name="static")

    return app


def main():
    cfg = load_manifest()
    port = int(cfg.get("sim_player_port", cfg.get("sim_exchange_port", 18999)))
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
