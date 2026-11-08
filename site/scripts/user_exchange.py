#!/usr/bin/env python3
"""Per-user Bybit REST client for trade executor."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN
from typing import Any

BYBIT_API = "https://api.bybit.com"
BYBIT_DEMO_API = "https://api-demo.bybit.com"
_UNCHECKED = object()


def ft_pair_to_symbol(pair: str) -> str:
    pair = pair.strip().upper()
    if ":" in pair:
        pair = pair.split(":")[0]
    if "/" in pair:
        base, quote = pair.split("/", 1)
        return f"{base}{quote}"
    if pair.endswith("USDT"):
        return pair
    return f"{pair}USDT"


def _sign(secret: str, payload: str, timestamp: str, api_key: str, recv_window: str = "5000") -> str:
    raw = f"{timestamp}{api_key}{recv_window}{payload}"
    return hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()


class UserBybitExchange:
    exchange_id = "bybit"

    def __init__(self, api_key: str, api_secret: str, *, demo_trading: bool = False) -> None:
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.demo_trading = bool(demo_trading)
        self.api_base = BYBIT_DEMO_API if self.demo_trading else BYBIT_API
        self._time_offset_ms = 0
        self._time_synced = False
        self._api_key_info: dict[str, Any] | None = None
        self._trade_block_reason: str | None | object = _UNCHECKED
        if not self.api_key or not self.api_secret:
            raise ValueError("Bybit API key/secret required")

    def sync_time(self, *, force: bool = False) -> int:
        """Align local timestamps with Bybit server (fixes recv_window errors)."""
        if self._time_synced and not force:
            return self._time_offset_ms
        url = f"{self.api_base}/v5/market/time"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        result = data.get("result") or {}
        server_ms = None
        if result.get("timeNano"):
            server_ms = int(result["timeNano"]) // 1_000_000
        elif result.get("timeSecond"):
            server_ms = int(result["timeSecond"]) * 1000
        if server_ms is None:
            raise RuntimeError("bybit time endpoint returned no timestamp")
        local_ms = int(time.time() * 1000)
        self._time_offset_ms = int(server_ms - local_ms)
        self._time_synced = True
        return self._time_offset_ms

    def _timestamp_ms(self) -> str:
        if not self._time_synced:
            try:
                self.sync_time()
            except Exception:
                pass
        return str(int(time.time() * 1000) + int(self._time_offset_ms))

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retries: int = 3,
    ) -> dict[str, Any]:
        last_err: Exception | None = None
        recv_window = "120000"
        for attempt in range(retries):
            try:
                if attempt > 0:
                    # Re-sync clock on retry (skew / sleep drift).
                    try:
                        self.sync_time(force=True)
                    except Exception:
                        pass
                if method == "POST":
                    payload = json.dumps(body or {}, separators=(",", ":"))
                    timestamp = self._timestamp_ms()
                    sign = _sign(self.api_secret, payload, timestamp, self.api_key, recv_window)
                    headers = {
                        "Content-Type": "application/json",
                        "X-BAPI-API-KEY": self.api_key,
                        "X-BAPI-TIMESTAMP": timestamp,
                        "X-BAPI-SIGN": sign,
                        "X-BAPI-RECV-WINDOW": recv_window,
                    }
                    req = urllib.request.Request(
                        f"{self.api_base}{path}",
                        data=payload.encode(),
                        headers=headers,
                        method="POST",
                    )
                else:
                    qs = urllib.parse.urlencode(params or {}, doseq=True)
                    url = f"{self.api_base}{path}?{qs}" if qs else f"{self.api_base}{path}"
                    timestamp = self._timestamp_ms()
                    sign = _sign(self.api_secret, qs, timestamp, self.api_key, recv_window)
                    headers = {
                        "X-BAPI-API-KEY": self.api_key,
                        "X-BAPI-TIMESTAMP": timestamp,
                        "X-BAPI-SIGN": sign,
                        "X-BAPI-RECV-WINDOW": recv_window,
                    }
                    req = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
                if int(data.get("retCode", -1)) != 0:
                    msg = str(data.get("retMsg") or data)
                    if "timestamp" in msg.lower() or "recv_window" in msg.lower():
                        try:
                            self.sync_time(force=True)
                        except Exception:
                            pass
                    raise RuntimeError(msg)
                return data.get("result") or {}
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as exc:
                last_err = exc
                time.sleep(0.25 * (attempt + 1))
        raise RuntimeError(str(last_err or "bybit request failed"))

    def instrument(self, symbol: str) -> dict[str, Any]:
        res = self._request(
            "GET",
            "/v5/market/instruments-info",
            params={"category": "linear", "symbol": symbol},
        )
        items = res.get("list") or []
        return items[0] if items else {}

    def last_price(self, symbol: str) -> float:
        res = self._request(
            "GET",
            "/v5/market/tickers",
            params={"category": "linear", "symbol": symbol},
        )
        items = res.get("list") or []
        if not items:
            raise RuntimeError(f"no ticker for {symbol}")
        return float(items[0].get("lastPrice") or 0)

    def order_avg_price(
        self,
        symbol: str,
        order_id: str,
        *,
        attempts: int = 10,
        delay_s: float = 0.25,
    ) -> float | None:
        """Actual fill avgPrice for an order (not ticker lastPrice)."""
        oid = str(order_id or "").strip()
        if not oid:
            return None
        for attempt in range(max(1, attempts)):
            for path in ("/v5/order/realtime", "/v5/order/history"):
                try:
                    res = self._request(
                        "GET",
                        path,
                        params={
                            "category": "linear",
                            "symbol": symbol,
                            "orderId": oid,
                        },
                    )
                    items = res.get("list") or []
                    if items:
                        avg = float(items[0].get("avgPrice") or 0)
                        if avg > 0:
                            return avg
                except Exception:
                    pass
            try:
                res = self._request(
                    "GET",
                    "/v5/execution/list",
                    params={
                        "category": "linear",
                        "symbol": symbol,
                        "orderId": oid,
                        "limit": "50",
                    },
                )
                rows = res.get("list") or []
                notional = 0.0
                qty = 0.0
                for row in rows:
                    px = float(row.get("execPrice") or 0)
                    q = float(row.get("execQty") or 0)
                    if px > 0 and q > 0:
                        notional += px * q
                        qty += q
                if qty > 0:
                    return notional / qty
            except Exception:
                pass
            if attempt + 1 < attempts:
                time.sleep(delay_s * (1.0 + 0.15 * attempt))
        return None

    def latest_closed_pnl(
        self,
        symbol: str,
        *,
        attempts: int = 8,
        delay_s: float = 0.3,
    ) -> dict[str, Any] | None:
        """Latest Bybit closed-pnl row for symbol (avgExitPrice / closedPnl)."""
        for attempt in range(max(1, attempts)):
            try:
                res = self._request(
                    "GET",
                    "/v5/position/closed-pnl",
                    params={"category": "linear", "symbol": symbol, "limit": "5"},
                )
                rows = res.get("list") or []
                if rows:
                    return rows[0]
            except Exception:
                pass
            if attempt + 1 < attempts:
                time.sleep(delay_s * (1.0 + 0.15 * attempt))
        return None

    def fill_or_last_price(self, symbol: str, order_id: str | None) -> float:
        """Prefer order fill avgPrice; fall back to ticker lastPrice."""
        if order_id:
            fill = self.order_avg_price(symbol, str(order_id))
            if fill and fill > 0:
                return float(fill)
        return float(self.last_price(symbol))

    def resolve_close_result(
        self,
        symbol: str,
        *,
        order_id: str | None = None,
        fallback_price: float | None = None,
    ) -> dict[str, Any]:
        """Fill price + Bybit realized PnL (fees included) after a close."""
        price: float | None = None
        if order_id:
            fill = self.order_avg_price(symbol, str(order_id))
            if fill and fill > 0:
                price = float(fill)
        profit_abs: float | None = None
        closed = self.latest_closed_pnl(symbol)
        if closed:
            if price is None:
                exit_px = float(closed.get("avgExitPrice") or 0)
                if exit_px > 0:
                    price = exit_px
            if closed.get("closedPnl") is not None:
                try:
                    profit_abs = float(closed["closedPnl"])
                except (TypeError, ValueError):
                    profit_abs = None
        if price is None or price <= 0:
            try:
                price = float(fallback_price) if fallback_price and fallback_price > 0 else self.last_price(symbol)
            except Exception:
                price = float(fallback_price or 0)
        return {"price": float(price or 0), "profit_abs": profit_abs, "closed_row": closed}

    def query_api_key(self, *, force: bool = False) -> dict[str, Any]:
        """Bybit /v5/user/query-api — permissions, readOnly flag, note."""
        if self._api_key_info is not None and not force:
            return self._api_key_info
        info = self._request("GET", "/v5/user/query-api")
        self._api_key_info = info if isinstance(info, dict) else {}
        return self._api_key_info

    def trade_block_reason(self, *, force: bool = False) -> str | None:
        """
        Human-readable reason why market orders cannot be placed.
        None means trading looks allowed (or check failed open — caller may still try).
        """
        if self._trade_block_reason is not _UNCHECKED and not force:
            return self._trade_block_reason  # type: ignore[return-value]
        reason: str | None = None
        try:
            info = self.query_api_key(force=force)
            read_only = int(info.get("readOnly") or 0) == 1
            perms = info.get("permissions") if isinstance(info.get("permissions"), dict) else {}
            contract = perms.get("ContractTrade") if isinstance(perms, dict) else None
            has_order = isinstance(contract, list) and ("Order" in contract or "Position" in contract)
            mode = "Demo" if self.demo_trading else "Live"
            if read_only:
                reason = (
                    f"API-ключ {mode} создан как Read-Only (readOnly=1). "
                    f"Ордера и плечо запрещены. В Bybit → API → создайте новый ключ "
                    f"БЕЗ Read-Only, с правами Contract Trade (Orders + Positions)"
                    + (" на Demo Trading" if self.demo_trading else "")
                    + ", затем замените ключи в Настройки → Секреты."
                )
            elif contract is not None and not has_order:
                reason = (
                    f"У API-ключа {mode} нет ContractTrade Order/Position "
                    f"(сейчас: {contract}). Включите права торговли деривативами."
                )
        except Exception as exc:
            # Don't hard-block on probe failure; surface soft hint.
            reason = None
            self._api_key_info = None
            _ = exc
        self._trade_block_reason = reason
        return reason

    def set_leverage(self, symbol: str, leverage: float) -> None:
        lev = max(1, min(int(leverage), 100))
        try:
            self._request(
                "POST",
                "/v5/position/set-leverage",
                body={
                    "category": "linear",
                    "symbol": symbol,
                    "buyLeverage": str(lev),
                    "sellLeverage": str(lev),
                },
            )
        except RuntimeError as exc:
            msg = str(exc).lower()
            # Bybit 110043 — leverage already at requested value.
            if "leverage not modified" in msg:
                return
            # Demo / restricted keys often deny set-leverage; keep current leverage.
            if "permission" in msg or "denied" in msg:
                return
            raise

    def _round_qty(self, symbol: str, qty: float) -> str:
        info = self.instrument(symbol)
        step = Decimal(str((info.get("lotSizeFilter") or {}).get("qtyStep") or "0.001"))
        min_qty = Decimal(str((info.get("lotSizeFilter") or {}).get("minOrderQty") or "0.001"))
        q = Decimal(str(max(qty, 0)))
        if step > 0:
            q = (q / step).to_integral_value(rounding=ROUND_DOWN) * step
        if q < min_qty:
            q = min_qty
        return format(q.normalize() if q == q.to_integral() else q, "f")

    @staticmethod
    def stake_risk_to_price_move(ratio: float, leverage: float) -> float:
        """Convert stake-PnL ratio (as used by exit monitor / TAG_RISK) to price move.

        Soft exits compare leveraged PnL to TAG_RISK. Exchange stopLoss/takeProfit
        are absolute prices, so divide by leverage or the exchange stop is ~Nx wider
        than the software SL and never fires first.
        """
        lev = max(float(leverage or 1.0), 1.0)
        return abs(float(ratio)) / lev

    def set_trading_stop(
        self,
        *,
        pair: str,
        is_short: bool,
        open_rate: float,
        leverage: float = 3.0,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        """Attach / refresh position TP-SL on Bybit from stake-PnL ratios."""
        symbol = ft_pair_to_symbol(pair)
        if open_rate <= 0:
            raise RuntimeError(f"invalid open_rate for {symbol}")
        body: dict[str, Any] = {
            "category": "linear",
            "symbol": symbol,
            "positionIdx": 0,
            "tpslMode": "Full",
            "slTriggerBy": "LastPrice",
            "tpTriggerBy": "LastPrice",
        }
        if stop_loss is not None and stop_loss < 0:
            move = self.stake_risk_to_price_move(stop_loss, leverage)
            sl_price = open_rate * (1 + move) if is_short else open_rate * (1 - move)
            body["stopLoss"] = str(round(sl_price, 8))
        if take_profit is not None and take_profit > 0:
            move = self.stake_risk_to_price_move(take_profit, leverage)
            tp_price = open_rate * (1 - move) if is_short else open_rate * (1 + move)
            body["takeProfit"] = str(round(tp_price, 8))
        if "stopLoss" not in body and "takeProfit" not in body:
            return {"ok": False, "reason": "no_sl_tp"}
        result = self._request("POST", "/v5/position/trading-stop", body=body)
        return {
            "ok": True,
            "symbol": symbol,
            "stopLoss": body.get("stopLoss"),
            "takeProfit": body.get("takeProfit"),
            "result": result,
        }

    def market_entry(
        self,
        *,
        pair: str,
        side: str,
        stake_usdt: float,
        leverage: float = 3.0,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        block = self.trade_block_reason()
        if block:
            raise RuntimeError(block)
        symbol = ft_pair_to_symbol(pair)
        side_l = side.lower()
        is_short = side_l in ("short", "sell")
        bybit_side = "Sell" if is_short else "Buy"
        price = self.last_price(symbol)
        if price <= 0:
            raise RuntimeError(f"invalid price for {symbol}")
        self.set_leverage(symbol, leverage)
        qty = (float(stake_usdt) * float(leverage)) / price
        qty_str = self._round_qty(symbol, qty)
        body: dict[str, Any] = {
            "category": "linear",
            "symbol": symbol,
            "side": bybit_side,
            "orderType": "Market",
            "qty": qty_str,
            "positionIdx": 0,
        }
        # TAG_RISK / soft monitor use stake-PnL ratios — convert to price for Bybit.
        if stop_loss is not None and stop_loss < 0:
            move = self.stake_risk_to_price_move(stop_loss, leverage)
            sl_price = price * (1 + move) if is_short else price * (1 - move)
            body["stopLoss"] = str(round(sl_price, 8))
        if take_profit is not None and take_profit > 0:
            move = self.stake_risk_to_price_move(take_profit, leverage)
            tp_price = price * (1 - move) if is_short else price * (1 + move)
            body["takeProfit"] = str(round(tp_price, 8))
        try:
            result = self._request("POST", "/v5/order/create", body=body)
        except RuntimeError as exc:
            msg = str(exc)
            if "permission" in msg.lower() or "denied" in msg.lower():
                hint = self.trade_block_reason(force=True) or (
                    "Проверьте права API-ключа: не Read-Only, Contract Trade Orders+Positions"
                    + (" (Demo Trading)" if self.demo_trading else "")
                )
                raise RuntimeError(f"{msg} — {hint}") from exc
            raise
        order_id = result.get("orderId")
        # Prefer actual fill — ticker lastPrice often differs from Bybit avgEntryPrice.
        fill_price = self.fill_or_last_price(symbol, order_id)
        fill = float(fill_price)
        # Re-anchor TP/SL to fill price (order-time ticker can differ from avg fill).
        sl_set = None
        tp_set = None
        try:
            if (stop_loss is not None and stop_loss < 0) or (
                take_profit is not None and take_profit > 0
            ):
                anchored = self.set_trading_stop(
                    pair=pair,
                    is_short=is_short,
                    open_rate=fill if fill > 0 else price,
                    leverage=leverage,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                )
                sl_set = anchored.get("stopLoss")
                tp_set = anchored.get("takeProfit")
        except Exception:
            # Entry already filled — soft exit monitor remains the backup.
            sl_set = body.get("stopLoss")
            tp_set = body.get("takeProfit")
        return {
            "symbol": symbol,
            "pair": pair,
            "side": side_l,
            "order_id": order_id,
            "price": fill,
            "qty": qty_str,
            "stake_usdt": float(stake_usdt),
            "leverage": float(leverage),
            "stop_loss": sl_set,
            "take_profit": tp_set,
        }

    def positions_map(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Map (symbol, buy|sell) -> position row with size > 0."""
        out: dict[tuple[str, str], dict[str, Any]] = {}
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"category": "linear", "settleCoin": "USDT", "limit": "200"}
            if cursor:
                params["cursor"] = cursor
            res = self._request("GET", "/v5/position/list", params=params)
            for p in res.get("list") or []:
                size = float(p.get("size") or 0)
                if size <= 0:
                    continue
                sym = str(p.get("symbol") or "")
                side = str(p.get("side") or "").lower()
                key_side = "sell" if side in ("sell", "short") else "buy"
                out[(sym, key_side)] = p
            cursor = res.get("nextPageCursor") or None
            if not cursor:
                break
        return out

    def market_close(self, *, pair: str, is_short: bool, qty: float | None = None) -> dict[str, Any]:
        symbol = ft_pair_to_symbol(pair)
        pos_side = "sell" if is_short else "buy"
        positions = self.positions_map()
        pos = positions.get((symbol, pos_side))
        if not pos:
            return {"closed": False, "reason": "no_position"}
        size = float(pos.get("size") or 0)
        if size <= 0:
            return {"closed": False, "reason": "zero_size"}
        close_qty = float(qty) if qty and qty > 0 else size
        close_qty = min(close_qty, size)
        qty_str = self._round_qty(symbol, close_qty)
        bybit_side = "Buy" if is_short else "Sell"
        result = self._request(
            "POST",
            "/v5/order/create",
            body={
                "category": "linear",
                "symbol": symbol,
                "side": bybit_side,
                "orderType": "Market",
                "qty": qty_str,
                "reduceOnly": True,
                "positionIdx": 0,
            },
        )
        order_id = result.get("orderId")
        # Fill avgPrice + Bybit closedPnl (net of fees) — matches exchange history.
        resolved = self.resolve_close_result(symbol, order_id=order_id)
        out: dict[str, Any] = {
            "closed": True,
            "symbol": symbol,
            "order_id": order_id,
            "price": float(resolved["price"]),
            "qty": qty_str,
        }
        if resolved.get("profit_abs") is not None:
            out["profit_abs"] = float(resolved["profit_abs"])
        return out


def fetch_public_klines(symbol: str, *, interval: str = "5", limit: int = 250) -> list[list[Any]]:
    qs = urllib.parse.urlencode(
        {"category": "linear", "symbol": symbol, "interval": interval, "limit": str(limit)}
    )
    url = f"{BYBIT_API}/v5/market/kline?{qs}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if int(data.get("retCode", -1)) != 0:
        raise RuntimeError(data.get("retMsg") or data)
    rows = (data.get("result") or {}).get("list") or []
    # Bybit returns newest first — reverse to oldest-first OHLCV
    rows.reverse()
    return rows


_KLINE_TF_MS = {"1": 60_000, "5": 300_000, "15": 900_000, "60": 3_600_000}
_BINANCE_KLINE_IV = {"1": "1m", "5": "5m", "15": "15m", "60": "1h"}


def _read_json_url(url: str, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _kline_point(ts: int, o: Any, h: Any, l: Any, c: Any, v: Any) -> dict[str, Any]:
    return {
        "t": int(ts),
        "o": float(o),
        "h": float(h),
        "l": float(l),
        "c": float(c),
        "v": float(v or 0),
    }


def _bybit_klines_range(
    symbol: str, interval: str, start_ms: int, end_ms: int, tf_ms: int
) -> list[dict[str, Any]]:
    """Public linear klines, oldest first. Windows stay under the 1000-bar page."""
    out: dict[int, dict[str, Any]] = {}
    cursor = int(start_ms)
    end_ms = int(end_ms)
    page_span = tf_ms * 900
    for _ in range(12):
        if cursor > end_ms:
            break
        window_end = min(end_ms, cursor + page_span - tf_ms)
        qs = urllib.parse.urlencode(
            {
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "start": str(cursor),
                "end": str(window_end),
                "limit": "1000",
            }
        )
        data = _read_json_url(f"{BYBIT_API}/v5/market/kline?{qs}")
        if not isinstance(data, dict) or int(data.get("retCode", -1)) != 0:
            msg = data.get("retMsg") if isinstance(data, dict) else "bybit kline"
            raise RuntimeError(str(msg or "bybit kline"))
        batch = ((data.get("result") or {}).get("list") or [])
        newest: int | None = None
        for row in batch:
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            ts = int(row[0])
            if newest is None or ts > newest:
                newest = ts
            if start_ms <= ts <= end_ms:
                vol = row[5] if len(row) > 5 else 0
                out[ts] = _kline_point(ts, row[1], row[2], row[3], row[4], vol)
        if newest is None:
            cursor = window_end + tf_ms
            continue
        nxt = newest + tf_ms
        cursor = nxt if nxt > cursor else window_end + tf_ms
    return [out[k] for k in sorted(out)]


def _binance_klines_range(
    symbol: str, interval: str, start_ms: int, end_ms: int, tf_ms: int
) -> list[dict[str, Any]]:
    iv = _BINANCE_KLINE_IV[interval]
    out: dict[int, dict[str, Any]] = {}
    cursor = int(start_ms)
    end_ms = int(end_ms)
    for _ in range(8):
        if cursor > end_ms:
            break
        qs = urllib.parse.urlencode(
            {
                "symbol": symbol,
                "interval": iv,
                "startTime": str(cursor),
                "endTime": str(end_ms),
                "limit": "1500",
            }
        )
        data = _read_json_url(f"{BINANCE_FAPI}/fapi/v1/klines?{qs}")
        if isinstance(data, dict):
            raise RuntimeError(str(data.get("msg") or "binance kline"))
        if not isinstance(data, list) or not data:
            break
        newest: int | None = None
        for row in data:
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            ts = int(row[0])
            if newest is None or ts > newest:
                newest = ts
            if start_ms <= ts <= end_ms:
                vol = row[5] if len(row) > 5 else 0
                out[ts] = _kline_point(ts, row[1], row[2], row[3], row[4], vol)
        if newest is None:
            break
        nxt = newest + tf_ms
        if nxt <= cursor:
            break
        cursor = nxt
        if len(data) < 1500:
            break
    return [out[k] for k in sorted(out)]


def fetch_klines_range(
    pair: str, *, interval: str = "5", start_ms: int, end_ms: int
) -> list[dict[str, Any]]:
    """OHLCV for a time window. Bybit public klines, Binance USD-M if Bybit is empty."""
    interval = str(interval or "5")
    tf_ms = _KLINE_TF_MS.get(interval)
    if tf_ms is None:
        raise ValueError("bad interval")
    symbol = ft_pair_to_symbol(pair)
    start_ms = int(start_ms)
    end_ms = int(end_ms)
    if end_ms < start_ms:
        raise ValueError("bad range")
    errors: list[str] = []
    for name, fn in (("bybit", _bybit_klines_range), ("binance", _binance_klines_range)):
        try:
            rows = fn(symbol, interval, start_ms, end_ms, tf_ms)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            continue
        if rows:
            return rows
    if errors:
        raise RuntimeError("; ".join(errors)[:300])
    return []


def _public_get(path: str, params: dict[str, Any], *, timeout: float = 20.0) -> dict[str, Any]:
    qs = urllib.parse.urlencode(params)
    url = f"{BYBIT_API}{path}?{qs}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    if int(data.get("retCode", -1)) != 0:
        raise RuntimeError(data.get("retMsg") or data)
    result = data.get("result")
    return result if isinstance(result, dict) else {}


def fetch_public_ticker(symbol: str) -> dict[str, Any]:
    result = _public_get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
    rows = result.get("list") or []
    return rows[0] if rows and isinstance(rows[0], dict) else {}


def fetch_public_orderbook(symbol: str, *, limit: int = 50) -> dict[str, Any]:
    return _public_get(
        "/v5/market/orderbook",
        {"category": "linear", "symbol": symbol, "limit": int(limit)},
    )


# ---------------------------------------------------------------------------
# Binance USDT-M Futures (live + Demo Trading)
# Live:  https://fapi.binance.com
# Demo:  https://demo-fapi.binance.com  (keys from demo.binance.com API Management)
# Spot Demo (demo-api.binance.com) is a different product — we trade USD-M futures.
# Legacy testnet.binancefuture.com is obsolete for new Demo Trading keys.
# ---------------------------------------------------------------------------

BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_FAPI_DEMO = "https://demo-fapi.binance.com"


class UserBinanceExchange:
    """Binance USD-M futures client with the same surface used by trade_executor."""

    exchange_id = "binance"

    def __init__(self, api_key: str, api_secret: str, *, demo_trading: bool = False) -> None:
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.demo_trading = bool(demo_trading)
        self.api_base = BINANCE_FAPI_DEMO if self.demo_trading else BINANCE_FAPI
        self._time_offset_ms = 0
        self._time_synced = False
        self._api_key_info: dict[str, Any] | None = None
        self._trade_block_reason: str | None | object = _UNCHECKED
        self._filters: dict[str, dict[str, Any]] = {}
        if not self.api_key or not self.api_secret:
            raise ValueError("Binance API key/secret required")

    def sync_time(self, *, force: bool = False) -> int:
        if self._time_synced and not force:
            return self._time_offset_ms
        url = f"{self.api_base}/fapi/v1/time"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        server_ms = int(data.get("serverTime") or 0)
        if not server_ms:
            raise RuntimeError("binance time endpoint returned no timestamp")
        local_ms = int(time.time() * 1000)
        self._time_offset_ms = int(server_ms - local_ms)
        self._time_synced = True
        return self._time_offset_ms

    def _timestamp_ms(self) -> int:
        if not self._time_synced:
            try:
                self.sync_time()
            except Exception:
                pass
        return int(time.time() * 1000) + int(self._time_offset_ms)

    def _sign(self, query: str) -> str:
        return hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = True,
        retries: int = 3,
    ) -> Any:
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                if attempt > 0:
                    try:
                        self.sync_time(force=True)
                    except Exception:
                        pass
                q = dict(params or {})
                if signed:
                    q["timestamp"] = self._timestamp_ms()
                    q["recvWindow"] = 60000
                qs = urllib.parse.urlencode(q, doseq=True)
                headers = {"X-MBX-APIKEY": self.api_key}
                if signed:
                    qs = f"{qs}&signature={self._sign(qs)}" if qs else f"signature={self._sign('')}"
                url = f"{self.api_base}{path}"
                if method == "GET":
                    req = urllib.request.Request(
                        f"{url}?{qs}" if qs else url, headers=headers, method="GET"
                    )
                    body = None
                else:
                    req = urllib.request.Request(
                        url,
                        data=qs.encode(),
                        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                        method=method,
                    )
                    body = None
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        raw = resp.read().decode()
                except urllib.error.HTTPError as http_exc:
                    raw = http_exc.read().decode() if http_exc.fp else str(http_exc)
                    try:
                        err = json.loads(raw)
                    except Exception:
                        raise RuntimeError(raw or str(http_exc)) from http_exc
                    msg = str(err.get("msg") or err)
                    if "timestamp" in msg.lower():
                        try:
                            self.sync_time(force=True)
                        except Exception:
                            pass
                    raise RuntimeError(msg) from http_exc
                data = json.loads(raw) if raw else {}
                if isinstance(data, dict) and data.get("code") not in (None, 0, 200) and "msg" in data:
                    # Binance error payload
                    raise RuntimeError(str(data.get("msg") or data))
                return data
            except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
                last_err = exc
                time.sleep(0.25 * (attempt + 1))
        raise RuntimeError(str(last_err or "binance request failed"))

    def instrument(self, symbol: str) -> dict[str, Any]:
        if symbol in self._filters:
            return self._filters[symbol]
        data = self._request("GET", "/fapi/v1/exchangeInfo", params={}, signed=False)
        for item in data.get("symbols") or []:
            if str(item.get("symbol") or "") == symbol:
                filt: dict[str, Any] = {"symbol": symbol}
                for f in item.get("filters") or []:
                    if f.get("filterType") == "LOT_SIZE":
                        filt["stepSize"] = f.get("stepSize")
                        filt["minQty"] = f.get("minQty")
                    if f.get("filterType") == "MIN_NOTIONAL":
                        filt["minNotional"] = f.get("notional") or f.get("minNotional")
                self._filters[symbol] = filt
                return filt
        return {}

    def last_price(self, symbol: str) -> float:
        data = self._request(
            "GET", "/fapi/v1/ticker/price", params={"symbol": symbol}, signed=False
        )
        return float(data.get("price") or 0)

    def order_avg_price(
        self,
        symbol: str,
        order_id: str,
        *,
        attempts: int = 10,
        delay_s: float = 0.25,
    ) -> float | None:
        oid = str(order_id or "").strip()
        if not oid:
            return None
        for attempt in range(max(1, attempts)):
            try:
                data = self._request(
                    "GET",
                    "/fapi/v1/order",
                    params={"symbol": symbol, "orderId": oid},
                )
                avg = float(data.get("avgPrice") or 0)
                if avg > 0:
                    return avg
            except Exception:
                pass
            if attempt + 1 < attempts:
                time.sleep(delay_s * (1.0 + 0.15 * attempt))
        return None

    def latest_closed_pnl(
        self,
        symbol: str,
        *,
        attempts: int = 8,
        delay_s: float = 0.3,
    ) -> dict[str, Any] | None:
        for attempt in range(max(1, attempts)):
            try:
                rows = self._request(
                    "GET",
                    "/fapi/v1/income",
                    params={"symbol": symbol, "incomeType": "REALIZED_PNL", "limit": 5},
                )
                if isinstance(rows, list) and rows:
                    row = rows[0]
                    return {
                        "avgExitPrice": 0,
                        "closedPnl": row.get("income"),
                        "raw": row,
                    }
            except Exception:
                pass
            if attempt + 1 < attempts:
                time.sleep(delay_s * (1.0 + 0.15 * attempt))
        return None

    def fill_or_last_price(self, symbol: str, order_id: str | None) -> float:
        if order_id:
            fill = self.order_avg_price(symbol, str(order_id))
            if fill and fill > 0:
                return float(fill)
        return float(self.last_price(symbol))

    def resolve_close_result(
        self,
        symbol: str,
        *,
        order_id: str | None = None,
        fallback_price: float | None = None,
    ) -> dict[str, Any]:
        price: float | None = None
        if order_id:
            fill = self.order_avg_price(symbol, str(order_id))
            if fill and fill > 0:
                price = float(fill)
        profit_abs: float | None = None
        closed = self.latest_closed_pnl(symbol)
        if closed and closed.get("closedPnl") is not None:
            try:
                profit_abs = float(closed["closedPnl"])
            except (TypeError, ValueError):
                profit_abs = None
        if price is None or price <= 0:
            try:
                price = (
                    float(fallback_price)
                    if fallback_price and fallback_price > 0
                    else self.last_price(symbol)
                )
            except Exception:
                price = float(fallback_price or 0)
        return {"price": float(price or 0), "profit_abs": profit_abs, "closed_row": closed}

    def query_api_key(self, *, force: bool = False) -> dict[str, Any]:
        if self._api_key_info is not None and not force:
            return self._api_key_info
        try:
            # demo-fapi / current USD-M API: account is v2 (v1 → "Path ... is invalid").
            info = self._request("GET", "/fapi/v2/account", params={})
            self._api_key_info = {
                "readOnly": 0,
                "note": "binance-futures",
                "permissions": {"ContractTrade": ["Order", "Position"]},
                "raw": info if isinstance(info, dict) else {},
            }
        except Exception as exc:
            self._api_key_info = {
                "readOnly": 0,
                "note": f"probe_failed:{exc}",
                "permissions": {"ContractTrade": ["Order", "Position"]},
            }
        return self._api_key_info

    def trade_block_reason(self, *, force: bool = False) -> str | None:
        if self._trade_block_reason is not _UNCHECKED and not force:
            return self._trade_block_reason  # type: ignore[return-value]
        reason: str | None = None
        try:
            self.query_api_key(force=force)
        except Exception as exc:
            reason = f"Binance API недоступен: {exc}"
        self._trade_block_reason = reason
        return reason

    def set_leverage(self, symbol: str, leverage: float) -> None:
        lev = max(1, min(int(leverage), 125))
        try:
            self._request(
                "POST",
                "/fapi/v1/leverage",
                params={"symbol": symbol, "leverage": lev},
            )
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "no need to change" in msg or "not modified" in msg:
                return
            if "permission" in msg:
                return
            raise

    def _round_qty(self, symbol: str, qty: float) -> str:
        info = self.instrument(symbol)
        step = Decimal(str(info.get("stepSize") or "0.001"))
        min_qty = Decimal(str(info.get("minQty") or "0.001"))
        q = Decimal(str(max(qty, 0)))
        if step > 0:
            q = (q / step).to_integral_value(rounding=ROUND_DOWN) * step
        if q < min_qty:
            q = min_qty
        return format(q.normalize() if q == q.to_integral() else q, "f")

    @staticmethod
    def stake_risk_to_price_move(ratio: float, leverage: float) -> float:
        lev = max(float(leverage or 1.0), 1.0)
        return abs(float(ratio)) / lev

    def set_trading_stop(
        self,
        *,
        pair: str,
        is_short: bool,
        open_rate: float,
        leverage: float = 3.0,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        """Attach TP/SL via Binance algo orders (best-effort)."""
        symbol = ft_pair_to_symbol(pair)
        if open_rate <= 0:
            raise RuntimeError(f"invalid open_rate for {symbol}")
        placed: dict[str, Any] = {"ok": True, "symbol": symbol}
        side = "BUY" if is_short else "SELL"  # close side
        if stop_loss is not None and stop_loss < 0:
            move = self.stake_risk_to_price_move(stop_loss, leverage)
            sl_price = open_rate * (1 + move) if is_short else open_rate * (1 - move)
            try:
                self._request(
                    "POST",
                    "/fapi/v1/order",
                    params={
                        "symbol": symbol,
                        "side": side,
                        "type": "STOP_MARKET",
                        "stopPrice": str(round(sl_price, 8)),
                        "closePosition": "true",
                        "workingType": "CONTRACT_PRICE",
                    },
                )
                placed["stopLoss"] = str(round(sl_price, 8))
            except Exception as exc:
                placed["stopLoss_error"] = str(exc)
        if take_profit is not None and take_profit > 0:
            move = self.stake_risk_to_price_move(take_profit, leverage)
            tp_price = open_rate * (1 - move) if is_short else open_rate * (1 + move)
            try:
                self._request(
                    "POST",
                    "/fapi/v1/order",
                    params={
                        "symbol": symbol,
                        "side": side,
                        "type": "TAKE_PROFIT_MARKET",
                        "stopPrice": str(round(tp_price, 8)),
                        "closePosition": "true",
                        "workingType": "CONTRACT_PRICE",
                    },
                )
                placed["takeProfit"] = str(round(tp_price, 8))
            except Exception as exc:
                placed["takeProfit_error"] = str(exc)
        if "stopLoss" not in placed and "takeProfit" not in placed:
            return {"ok": False, "reason": "no_sl_tp", **placed}
        return placed

    def market_entry(
        self,
        *,
        pair: str,
        side: str,
        stake_usdt: float,
        leverage: float = 3.0,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        block = self.trade_block_reason()
        if block:
            raise RuntimeError(block)
        symbol = ft_pair_to_symbol(pair)
        side_l = side.lower()
        is_short = side_l in ("short", "sell")
        bn_side = "SELL" if is_short else "BUY"
        price = self.last_price(symbol)
        if price <= 0:
            raise RuntimeError(f"invalid price for {symbol}")
        self.set_leverage(symbol, leverage)
        qty = (float(stake_usdt) * float(leverage)) / price
        qty_str = self._round_qty(symbol, qty)
        result = self._request(
            "POST",
            "/fapi/v1/order",
            params={
                "symbol": symbol,
                "side": bn_side,
                "type": "MARKET",
                "quantity": qty_str,
            },
        )
        order_id = str(result.get("orderId") or "")
        fill = float(self.fill_or_last_price(symbol, order_id))
        sl_set = None
        tp_set = None
        try:
            if (stop_loss is not None and stop_loss < 0) or (
                take_profit is not None and take_profit > 0
            ):
                anchored = self.set_trading_stop(
                    pair=pair,
                    is_short=is_short,
                    open_rate=fill if fill > 0 else price,
                    leverage=leverage,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                )
                sl_set = anchored.get("stopLoss")
                tp_set = anchored.get("takeProfit")
        except Exception:
            pass
        return {
            "symbol": symbol,
            "pair": pair,
            "side": side_l,
            "order_id": order_id,
            "price": fill,
            "qty": qty_str,
            "stake_usdt": float(stake_usdt),
            "leverage": float(leverage),
            "stop_loss": sl_set,
            "take_profit": tp_set,
        }

    def positions_map(self) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        rows = self._request("GET", "/fapi/v2/positionRisk", params={})
        if not isinstance(rows, list):
            return out
        for p in rows:
            amt = float(p.get("positionAmt") or 0)
            if abs(amt) <= 0:
                continue
            sym = str(p.get("symbol") or "")
            key_side = "sell" if amt < 0 else "buy"
            # import_positions reads the Bybit field names.
            out[(sym, key_side)] = {
                **p,
                "size": abs(amt),
                "side": "Sell" if amt < 0 else "Buy",
                "avgPrice": p.get("entryPrice") or p.get("avgPrice") or 0,
                "positionIM": (
                    p.get("isolatedWallet")
                    or p.get("isolatedMargin")
                    or p.get("positionInitialMargin")
                    or 0
                ),
                "createdTime": p.get("updateTime") or p.get("createdTime"),
            }
        return out

    def market_close(self, *, pair: str, is_short: bool, qty: float | None = None) -> dict[str, Any]:
        symbol = ft_pair_to_symbol(pair)
        pos_side = "sell" if is_short else "buy"
        positions = self.positions_map()
        pos = positions.get((symbol, pos_side))
        if not pos:
            return {"closed": False, "reason": "no_position"}
        size = float(pos.get("size") or 0)
        if size <= 0:
            return {"closed": False, "reason": "zero_size"}
        close_qty = float(qty) if qty and qty > 0 else size
        close_qty = min(close_qty, size)
        qty_str = self._round_qty(symbol, close_qty)
        bn_side = "BUY" if is_short else "SELL"
        result = self._request(
            "POST",
            "/fapi/v1/order",
            params={
                "symbol": symbol,
                "side": bn_side,
                "type": "MARKET",
                "quantity": qty_str,
                "reduceOnly": "true",
            },
        )
        order_id = str(result.get("orderId") or "")
        resolved = self.resolve_close_result(symbol, order_id=order_id)
        out: dict[str, Any] = {
            "closed": True,
            "symbol": symbol,
            "order_id": order_id,
            "price": float(resolved["price"]),
            "qty": qty_str,
        }
        if resolved.get("profit_abs") is not None:
            out["profit_abs"] = float(resolved["profit_abs"])
        return out


def make_user_exchange(
    exchange: str,
    api_key: str,
    api_secret: str,
    *,
    demo_trading: bool = False,
) -> UserBybitExchange | UserBinanceExchange:
    ex = (exchange or "bybit").strip().lower()
    if ex == "binance":
        return UserBinanceExchange(api_key, api_secret, demo_trading=demo_trading)
    return UserBybitExchange(api_key, api_secret, demo_trading=demo_trading)
