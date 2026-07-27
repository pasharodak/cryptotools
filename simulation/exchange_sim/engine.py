"""Virtual wallet, positions, and market-order fills at candle close."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from .datastore import HistoricalDatastore


@dataclass
class Position:
    symbol: str
    side: str  # Buy | Sell
    size: float
    entry_price: float
    leverage: float = 1.0
    unrealized_pnl: float = 0.0


@dataclass
class SimOrder:
    order_id: str
    symbol: str
    side: str
    qty: float
    price: float
    status: str
    order_type: str


@dataclass
class SimulationEngine:
    datastore: HistoricalDatastore
    wallet_usdt: float = 100.0
    clock_ms: int = 0
    positions: dict[str, Position] = field(default_factory=dict)
    orders: list[SimOrder] = field(default_factory=list)
    closed_pnl: float = 0.0
    timeframe: str = "5m"

    def set_clock(self, ts_ms: int) -> None:
        self.clock_ms = ts_ms
        self._mark_positions()

    def _pair_from_symbol(self, symbol: str) -> str:
        if symbol.endswith("USDT") and "/" not in symbol:
            base = symbol[:-4]
            return f"{base}/USDT:USDT"
        return symbol

    def _price(self, symbol: str) -> float:
        pair = self._pair_from_symbol(symbol)
        k = self.datastore.kline_at(pair, self.timeframe, self.clock_ms)
        if not k:
            raise ValueError(f"No price for {symbol} at {self.clock_ms}")
        return float(k["close"])

    def _mark_positions(self) -> None:
        for sym, pos in self.positions.items():
            try:
                px = self._price(sym)
            except ValueError:
                continue
            if pos.side == "Buy":
                pos.unrealized_pnl = (px - pos.entry_price) * pos.size
            else:
                pos.unrealized_pnl = (pos.entry_price - px) * pos.size

    def equity(self) -> float:
        upnl = sum(p.unrealized_pnl for p in self.positions.values())
        return self.wallet_usdt + self.closed_pnl + upnl

    def place_market(self, symbol: str, side: str, qty: float) -> SimOrder:
        px = self._price(symbol)
        fee = qty * px * 0.00055
        oid = str(uuid.uuid4())[:16]
        order = SimOrder(oid, symbol, side, qty, px, "Filled", "Market")
        self.orders.append(order)

        if side == "Buy":
            if symbol in self.positions and self.positions[symbol].side == "Sell":
                self._close_position(symbol, px, qty)
            else:
                self._open_position(symbol, "Buy", qty, px, fee)
        else:
            if symbol in self.positions and self.positions[symbol].side == "Buy":
                self._close_position(symbol, px, qty)
            else:
                self._open_position(symbol, "Sell", qty, px, fee)
        return order

    def _open_position(self, symbol: str, side: str, qty: float, px: float, fee: float) -> None:
        self.wallet_usdt -= fee
        self.positions[symbol] = Position(symbol, side, qty, px)

    def _close_position(self, symbol: str, px: float, qty: float) -> None:
        pos = self.positions.get(symbol)
        if not pos:
            return
        close_qty = min(qty, pos.size)
        if pos.side == "Buy":
            pnl = (px - pos.entry_price) * close_qty
        else:
            pnl = (pos.entry_price - px) * close_qty
        fee = close_qty * px * 0.00055
        self.closed_pnl += pnl - fee
        self.wallet_usdt -= fee
        pos.size -= close_qty
        if pos.size <= 1e-12:
            del self.positions[symbol]

    def snapshot(self) -> dict[str, Any]:
        return {
            "clock_ms": self.clock_ms,
            "wallet_usdt": round(self.wallet_usdt, 4),
            "closed_pnl": round(self.closed_pnl, 4),
            "equity": round(self.equity(), 4),
            "positions": [
                {
                    "symbol": p.symbol,
                    "side": p.side,
                    "size": p.size,
                    "entry_price": p.entry_price,
                    "unrealized_pnl": round(p.unrealized_pnl, 4),
                }
                for p in self.positions.values()
            ],
            "orders_count": len(self.orders),
        }
