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
