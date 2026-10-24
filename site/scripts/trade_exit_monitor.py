#!/usr/bin/env python3
"""Monitor and close open strategy trades for shared-bot users."""
from __future__ import annotations

import importlib
import json
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

log = logging.getLogger("trade_exit_monitor")

# Bybit tick rounding can leave the exchange price slightly off the target.
# 0.1% of price is enough to notice a 0.5% stake step at 3x, and the cooldown
# stops a retry every monitor tick when the exchange cannot match exactly.
_STOP_SYNC_AT: dict[tuple, float] = {}
_STOP_SYNC_INTERVAL = 30.0
_STOP_MOVE_EPS = 0.001


def _price_move_off(exchange_price: str, open_rate: float, expect_move: float) -> bool:
    raw = str(exchange_price or "").strip()
    if not raw or raw in ("0", "0.0"):
        return expect_move > 0
    if open_rate <= 0 or expect_move <= 0:
        return False
    try:
        actual = abs(float(raw) - open_rate) / open_rate
    except (TypeError, ValueError):
        return True
    return abs(actual - expect_move) > _STOP_MOVE_EPS

HEDGE_SUFFIX = ":hedge"
INV_SUFFIX = ":inv"


def base_enter_tag(tag: str | None) -> str:
    if not tag:
        return ""
    t = str(tag)
    for suffix in (HEDGE_SUFFIX, INV_SUFFIX):
        while t.endswith(suffix):
            t = t[: -len(suffix)]
    return t


def is_test_tag(tag: str) -> bool:
    return tag.endswith("TestStrategy")


def _repo_roots() -> list[Path]:
    here = Path(__file__).resolve()
    site = here.parent.parent  # .../site
    return [site.parent, site, here.parents[2]]


def _load_trained_risk_catalog() -> dict[str, dict[str, Any]]:
    """SL/TP keyed by strategy class — same sources as pair_config / TAG_RISK."""
    out: dict[str, dict[str, Any]] = {}
    for root in _repo_roots():
        pack_path = root / "simulation" / "config" / "prod_top30_pack.json"
        if not pack_path.is_file():
            continue
        try:
            pack = json.loads(pack_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for s in pack.get("strategies") or []:
            cls = s.get("class_name")
            if not cls:
                continue
            out[str(cls)] = {
                "stoploss": float(s.get("stoploss") or 0),
                "tp": float(s.get("tp") or 0),
                "minimal_roi": s.get("minimal_roi") or {"0": float(s.get("tp") or 0)},
            }
        break
    for root in _repo_roots():
        sc_path = root / "simulation" / "config" / "player_scenarios.json"
        if not sc_path.is_file():
            continue
        try:
            scenarios = json.loads(sc_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(scenarios, list):
            break
        for sc in scenarios:
            if not isinstance(sc, dict):
                continue
            cls = sc.get("strategy")
            if not cls or cls in out:
                continue
            sl = sc.get("stoploss")
            roi = sc.get("minimal_roi")
            if sl is None and not isinstance(roi, dict):
                continue
            tp = 0.0
            if isinstance(roi, dict) and roi:
                try:
                    tp = float(roi.get("0") or next(iter(roi.values())))
                except (TypeError, ValueError, StopIteration):
                    tp = 0.0
            out[str(cls)] = {
                "stoploss": float(sl or 0),
                "tp": float(tp),
                "minimal_roi": roi if isinstance(roi, dict) else {"0": float(tp)},
            }
        break
    return out


def risk_from_trained(tag: str) -> tuple[float, float] | None:
    catalog = _load_trained_risk_catalog()
    risk = catalog.get(tag)
    if not risk:
        return None
    sl = float(risk.get("stoploss") or 0)
    if sl > 0:
        sl = -abs(sl)
    elif sl == 0:
        return None
    tp = float(risk.get("tp") or 0)
    if tp <= 0:
        mroi = risk.get("minimal_roi") or {}
        if isinstance(mroi, dict) and mroi:
            try:
                tp = abs(float(next(iter(mroi.values()))))
            except (TypeError, ValueError, StopIteration):
                tp = 0.0
    if tp <= 0:
        return None
    return sl, abs(tp)


def _trained_risk_from_payload(tag: str, data: dict[str, Any]) -> bool | None:
    """True when «Свой SL/TP» is on for this strategy. Missing key means off."""
    stored = data.get("trained_risk") or {}
    if not isinstance(stored, dict) or tag not in stored:
        return False
    return bool(stored[tag])


def _trained_risk_enabled(user_id: str, tag: str, *, tenant_dir, load_json) -> bool:
    try:
        import user_prefs as up

        data = up.load_enabled_payload(user_id)
        if isinstance(data, dict):
            return bool(_trained_risk_from_payload(tag, data))
    except Exception:
        pass
    candidates = [
        tenant_dir(user_id) / "enabled_strategies.json",
        Path(__file__).resolve().parent.parent / "user_data" / "enabled_strategies.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = load_json(path, {})
        except Exception:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
        if not isinstance(data, dict):
            continue
        stored = data.get("trained_risk") or {}
        if isinstance(stored, dict) and tag in stored:
            return bool(stored[tag])
        return False
    return False


@dataclass
class TradeRow:
    id: int
    pair: str
    is_short: bool
    amount: float
    open_rate: float
    stake_amount: float
    enter_tag: str
    strategy: str
    leverage: float


def load_open_trades(db_path) -> list[TradeRow]:
    if not db_path.is_file():
        return []
    out: list[TradeRow] = []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            for r in conn.execute(
                """
                SELECT id, pair, is_short, amount, open_rate, stake_amount,
                       enter_tag, strategy, leverage
                FROM trades WHERE is_open = 1 ORDER BY id
                """
            ):
                out.append(
                    TradeRow(
                        id=int(r["id"]),
                        pair=str(r["pair"]),
                        is_short=bool(r["is_short"]),
                        amount=float(r["amount"] or 0),
                        open_rate=float(r["open_rate"] or 0),
                        stake_amount=float(r["stake_amount"] or 0),
                        enter_tag=str(r["enter_tag"] or ""),
                        strategy=str(r["strategy"] or ""),
                        leverage=float(r["leverage"] or 1),
                    )
                )
    except sqlite3.Error as exc:
        log.warning("load open trades %s: %s", db_path, exc)
    return out


def profit_ratio(trade: TradeRow, rate: float) -> float:
    if not trade.open_rate or not rate:
        return 0.0
    lev = max(trade.leverage or 1.0, 1.0)
    if trade.is_short:
        return ((trade.open_rate - rate) / trade.open_rate) * lev
    return ((rate - trade.open_rate) / trade.open_rate) * lev


def update_mark_pnl(db_path, trade_id: int, ratio: float, abs_pnl: float) -> None:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE trades SET close_profit = ?, close_profit_abs = ?
                WHERE id = ? AND is_open = 1
                """,
                (ratio, abs_pnl, trade_id),
            )
            conn.commit()
    except sqlite3.Error:
        pass


def _is_test_panel_tag(user_id: str, tag: str, *, tenant_dir, load_json) -> bool:
    try:
        from htf1h_catalog import HTF1H_IDS
    except Exception:
        HTF1H_IDS = frozenset()
    if tag in HTF1H_IDS:
        return False
    if tag.endswith("TestStrategy"):
        return True
    data = None
    try:
        import user_prefs as up

        data = up.load_placement(user_id)
    except Exception:
        data = None
    if not isinstance(data, dict):
        data = load_json(tenant_dir(user_id) / "strategy_ui_placement.json", {})
    if not isinstance(data, dict):
        return False
    main = {str(x) for x in (data.get("main") or [])}
    hidden = {str(x) for x in (data.get("hidden") or [])}
    return tag not in main and tag not in hidden


def _global_sl_tp(user_id: str, cfg: dict[str, Any]) -> tuple[float, float]:
    """Global SL/TP from SQLite bot_limits, then config_strategy.json."""
    sl = float(cfg.get("stoploss") or -0.15)
    roi = cfg.get("minimal_roi") or {"0": 0.05}
    tp = float(roi.get("0") or roi.get(0) or 0.05)
    try:
        import user_prefs as up

        data = up.load_bot_limits_payload(user_id)
    except Exception:
        data = {}
    stored = data.get("strategy_risk") if isinstance(data, dict) else None
    if isinstance(stored, dict):
        if stored.get("stoploss") is not None:
            try:
                sl = float(stored["stoploss"])
            except (TypeError, ValueError):
                pass
        if stored.get("take_profit") is not None:
            try:
                tp = abs(float(stored["take_profit"]))
            except (TypeError, ValueError):
                pass
    return sl, tp


def _enabled_payload(user_id: str, *, tenant_dir, load_json) -> dict:
    try:
        import user_prefs as up

        data = up.load_enabled_payload(user_id)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    data = load_json(tenant_dir(user_id) / "enabled_strategies.json", {})
    return data if isinstance(data, dict) else {}


def _explicit_sltp(user_id: str, tag: str, *, tenant_dir, load_json) -> tuple[float, float] | None:
    """User-picked stop/take for this strategy. Beats trained and the block default."""
    try:
        from htf1h_catalog import sltp_ratios
    except Exception:
        return None
    data = _enabled_payload(user_id, tenant_dir=tenant_dir, load_json=load_json)
    raw_map = data.get("sltp")
    if not isinstance(raw_map, dict):
        return None
    raw = raw_map.get(tag)
    if not isinstance(raw, dict):
        return None
    if raw.get("sl") is None and raw.get("tp") is None:
        return None
    return sltp_ratios(tag, raw)


def _htf_default_sltp(tag: str) -> tuple[float, float] | None:
    try:
        from htf1h_catalog import HTF1H_IDS, sltp_ratios
    except Exception:
        return None
    if tag not in HTF1H_IDS:
        return None
    return sltp_ratios(tag, None)


def risk_thresholds(user_id: str, tag: str, *, tenant_dir, load_json, load_user_config) -> tuple[float, float]:
    """Return (stoploss_ratio < 0, take_profit_ratio > 0) for entry + exit monitor.

    Priority:
      1) DeepSeek / Cursor own settings
      2) «Свой SL/TP» on: the stop and take chosen on that strategy row
         (1h catalog default if that row has no saved choice)
      3) «Свой SL/TP» off: general block SL/TP (test block for test-panel tags)
    """
    tag = base_enter_tag(tag)
    if tag == "DeepseekLlmStrategy":
        try:
            import llm_settings

            cfg = llm_settings.load(user_id)
            return float(cfg["stoploss"]), float(cfg["take_profit"])
        except Exception:
            return -0.02, 0.03
    if tag == "CursorLlmStrategy":
        try:
            import cursor_settings

            cfg = cursor_settings.load(user_id)
            return float(cfg["stoploss"]), float(cfg["take_profit"])
        except Exception:
            return -0.02, 0.03
    own = _trained_risk_enabled(user_id, tag, tenant_dir=tenant_dir, load_json=load_json)
    if own:
        explicit = _explicit_sltp(user_id, tag, tenant_dir=tenant_dir, load_json=load_json)
        if explicit is not None:
            return explicit
        htf = _htf_default_sltp(tag)
        if htf is not None:
            return htf
    cfg = load_user_config(user_id)
    if not isinstance(cfg, dict):
        cfg = {}
    sl, tp = _global_sl_tp(user_id, cfg)

    if _is_test_panel_tag(user_id, tag, tenant_dir=tenant_dir, load_json=load_json):
        ts = None
        try:
            import user_prefs as up

            ts = up.load_test_settings(user_id)
        except Exception:
            ts = None
        if not isinstance(ts, dict):
            ts = load_json(tenant_dir(user_id) / "test_strategy_settings.json", {})
        if isinstance(ts, dict):
            if ts.get("stoploss") is not None:
                sl = -abs(float(ts["stoploss"]))
            if ts.get("take_profit") is not None:
                tp = abs(float(ts["take_profit"]))
    return sl, tp


def klines_to_df(rows: list[list[Any]]) -> pd.DataFrame | None:
    if not rows:
        return None
    df = pd.DataFrame(
        rows,
        columns=["date_ms", "open", "high", "low", "close", "volume", "turnover"],
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date_ms"].astype(int), unit="ms", utc=True)
    return df


def try_strategy_exit(tag: str, df: pd.DataFrame, trade: TradeRow, rate: float) -> str | None:
    strategies_dir = Path(__file__).resolve().parent.parent / "user_data" / "strategies"
    if str(strategies_dir) not in sys.path:
        sys.path.insert(0, str(strategies_dir))
    try:
        mod = importlib.import_module(tag)
        cls = getattr(mod, tag, None)
        if cls is None:
            return None
        inst = cls({})
        fn = getattr(inst, "exit_reason_from_ohlcv", None)
        if not callable(fn):
            return None
        stub = SimpleNamespace(
            pair=trade.pair,
            is_short=trade.is_short,
            open_rate=trade.open_rate,
            enter_tag=trade.enter_tag,
        )
        ind = getattr(inst, "populate_indicators", None)
        work = df
        if callable(ind):
            work = ind(df.copy(), {"pair": trade.pair})
        reason = fn(work, stub, rate)
        return str(reason) if reason else None
    except Exception as exc:
        log.debug("custom exit %s: %s", tag, exc)
        return None


def monitor_user_exits(user_id: str, ctx: dict[str, Any]) -> None:
    get_exchange = ctx["get_exchange"]
    db_path_fn = ctx["db_path"]
    tenant_dir = ctx["tenant_dir"]
    load_json = ctx["load_json"]
    load_user_config = ctx["load_user_config"]
    archive_trade_in_db = ctx["archive_trade_in_db"]
    ft_pair_to_symbol = ctx["ft_pair_to_symbol"]
    fetch_public_klines = ctx["fetch_public_klines"]

    path = db_path_fn(user_id)
    trades = load_open_trades(path)
    if not trades:
        return
    ex = get_exchange(user_id)
    if ex is None:
        return
    try:
        pos_map = ex.positions_map()
    except Exception as exc:
        log.warning("positions user=%s: %s", user_id, exc)
        return

    for trade in trades:
        sym = ft_pair_to_symbol(trade.pair)
        side_key = "sell" if trade.is_short else "buy"
        pos = pos_map.get((sym, side_key))
        try:
            mark = ex.last_price(sym)
        except Exception:
            mark = trade.open_rate
        ratio = profit_ratio(trade, mark)
        abs_pnl = ratio * (trade.stake_amount or 0)
        update_mark_pnl(path, trade.id, ratio, abs_pnl)

        if not pos or float(pos.get("size") or 0) <= 0:
            close_rate = mark
            try:
                closed = ex.latest_closed_pnl(sym)
                if closed:
                    exit_px = float(closed.get("avgExitPrice") or 0)
                    if exit_px > 0:
                        close_rate = exit_px
            except Exception as exc:
                log.debug("closed-pnl user=%s %s: %s", user_id, trade.pair, exc)
            final_ratio = profit_ratio(trade, close_rate)
            profit_abs = final_ratio * (trade.stake_amount or 0)
            archive_trade_in_db(
                path,
                trade.id,
                close_rate=close_rate,
                profit_abs=profit_abs,
                exit_reason="exchange_flat",
            )
            log.info("ARCHIVE user=%s #%s %s (flat on exchange)", user_id, trade.id, trade.pair)
            try:
                import telegram_links as tg_links

                stake_f = float(trade.stake_amount or 0) or None
                fee_info = tg_links.pnl_with_fees(
                    profit_abs,
                    amount=float(trade.amount or 0) or None,
                    open_rate=float(trade.open_rate or 0) or None,
                    close_rate=close_rate,
                    stake_amount=stake_f,
                    leverage=float(trade.leverage or 1) or None,
                    profit_pct=final_ratio * 100,
                )
                tg_links.notify_user(
                    user_id,
                    tg_links.format_exit_ru(
                        pair=trade.pair,
                        strategy=base_enter_tag(trade.enter_tag or trade.strategy),
                        reason="exchange_flat",
                        pnl_pct=final_ratio * 100,
                        pnl_usdt=profit_abs,
                        stake=stake_f,
                        is_short=bool(trade.is_short),
                        close_rate=close_rate,
                        amount=float(trade.amount or 0) or None,
                        open_rate=float(trade.open_rate or 0) or None,
                        leverage=float(trade.leverage or 1) or None,
                    ),
                    kind="trade_close",
                    pnl_pct=fee_info["net_pct"],
                )
            except Exception:
                pass
            continue

        tag = base_enter_tag(trade.enter_tag or trade.strategy)
        sl, tp = risk_thresholds(
            user_id, tag, tenant_dir=tenant_dir, load_json=load_json, load_user_config=load_user_config
        )
        # Ensure Bybit position has exchange SL/TP matching stake-PnL risk (backup to soft exits).
        try:
            from user_exchange import UserBybitExchange

            ex_sl = str(pos.get("stopLoss") or "").strip()
            ex_tp = str(pos.get("takeProfit") or "").strip()
            open_rate = float(trade.open_rate or 0)
            lev = float(trade.leverage or 1) or 1.0
            expect_sl = UserBybitExchange.stake_risk_to_price_move(sl, lev) if sl < 0 else 0.0
            expect_tp = UserBybitExchange.stake_risk_to_price_move(tp, lev) if tp > 0 else 0.0
            need_sl = _price_move_off(ex_sl, open_rate, expect_sl)
            need_tp = _price_move_off(ex_tp, open_rate, expect_tp)
            if need_sl or need_tp:
                sync_key = (user_id, int(trade.id), round(float(sl), 6), round(float(tp), 6))
                now = time.monotonic()
                last = _STOP_SYNC_AT.get(sync_key)
                if last is not None and now - last < _STOP_SYNC_INTERVAL:
                    need_sl = False
                    need_tp = False
                else:
                    _STOP_SYNC_AT[sync_key] = now
            if need_sl or need_tp:
                anchored = ex.set_trading_stop(
                    pair=trade.pair,
                    is_short=bool(trade.is_short),
                    open_rate=open_rate,
                    leverage=lev,
                    stop_loss=sl if sl < 0 else None,
                    take_profit=tp if tp > 0 else None,
                )
                log.info(
                    "EXCHANGE TP/SL user=%s #%s %s sl=%s tp=%s",
                    user_id,
                    trade.id,
                    trade.pair,
                    anchored.get("stopLoss"),
                    anchored.get("takeProfit"),
                )
        except Exception as exc:
            log.debug("set_trading_stop user=%s %s: %s", user_id, trade.pair, exc)

        exit_reason: str | None = None
        if tp > 0 and ratio >= tp:
            exit_reason = "roi_take_profit"
        elif sl < 0 and ratio <= sl:
            exit_reason = "stop_loss"

        if not exit_reason:
            try:
                rows = fetch_public_klines(sym, interval="5", limit=250)
                df = klines_to_df(rows)
                if df is not None and not df.empty:
                    custom = try_strategy_exit(tag, df, trade, mark)
                    if custom:
                        exit_reason = str(custom)
            except Exception as exc:
                log.debug("klines exit user=%s %s: %s", user_id, trade.pair, exc)

        if not exit_reason:
            continue

        try:
            res = ex.market_close(pair=trade.pair, is_short=trade.is_short)
            close_rate = float(res.get("price") or mark)
            if res.get("closed"):
                # Gross price PnL; UI applies fixed Bybit fee % for «С комиссией».
                final_ratio = profit_ratio(trade, close_rate)
                pnl_abs = final_ratio * (trade.stake_amount or 0)
                archive_trade_in_db(
                    path,
                    trade.id,
                    close_rate=close_rate,
                    profit_abs=pnl_abs,
                    exit_reason=exit_reason,
                )
                log.info(
                    "EXIT user=%s #%s %s reason=%s pnl=%.2f%% abs=%.4f",
                    user_id,
                    trade.id,
                    trade.pair,
                    exit_reason,
                    final_ratio * 100,
                    pnl_abs,
                )
                try:
                    import telegram_links as tg_links

                    stake_f = float(trade.stake_amount or 0) or None
                    fee_info = tg_links.pnl_with_fees(
                        pnl_abs,
                        amount=float(trade.amount or 0) or None,
                        open_rate=float(trade.open_rate or 0) or None,
                        close_rate=close_rate,
                        stake_amount=stake_f,
                        leverage=float(trade.leverage or 1) or None,
                        profit_pct=final_ratio * 100,
                    )
                    tg_links.notify_user(
                        user_id,
                        tg_links.format_exit_ru(
                            pair=trade.pair,
                            strategy=tag,
                            reason=exit_reason,
                            pnl_pct=final_ratio * 100,
                            pnl_usdt=pnl_abs,
                            stake=stake_f,
                            is_short=bool(trade.is_short),
                            close_rate=close_rate,
                            amount=float(trade.amount or 0) or None,
                            open_rate=float(trade.open_rate or 0) or None,
                            leverage=float(trade.leverage or 1) or None,
                        ),
                        kind="trade_close",
                        pnl_pct=fee_info["net_pct"],
                    )
                except Exception:
                    pass
        except Exception as exc:
            log.error("close failed user=%s #%s: %s", user_id, trade.id, exc)


def monitor_all_users(ctx: dict[str, Any]) -> None:
    import tenant_manager as tm

    users: set[str] = set()
    for user in tm.load_users():
        uid = str(user.get("id") or "")
        if not uid or not user.get("enabled", True):
            continue
        if ctx["count_open_trades"](uid) > 0:
            users.add(uid)
    for uid in sorted(users):
        try:
            monitor_user_exits(uid, ctx)
        except Exception as exc:
            log.exception("exit monitor user=%s: %s", uid, exc)
