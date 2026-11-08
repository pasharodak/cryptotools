"""Reconcile must call the venue named by the trading context."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import import_exchange_positions as iep
from user_exchange import UserBinanceExchange, UserBybitExchange


class LoadExchangeTests(unittest.TestCase):
    def test_binance_demo_context_uses_demo_fapi(self) -> None:
        blob = {
            "exchange": "binance",
            "bybit_api_key": "k" * 16,
            "bybit_api_secret": "s" * 16,
            "bybit_demo_trading": True,
        }
        with patch.object(iep.tm, "load_user_secrets", return_value=blob):
            ex = iep._load_exchange("pasha__binance__demo")
        self.assertIsInstance(ex, UserBinanceExchange)
        self.assertEqual(ex.api_base, "https://demo-fapi.binance.com")
        self.assertTrue(ex.demo_trading)

    def test_bybit_context_stays_on_bybit(self) -> None:
        blob = {
            "exchange": "bybit",
            "bybit_api_key": "k" * 16,
            "bybit_api_secret": "s" * 16,
            "bybit_demo_trading": False,
        }
        with patch.object(iep.tm, "load_user_secrets", return_value=blob):
            ex = iep._load_exchange("pasha")
        self.assertIsInstance(ex, UserBybitExchange)
        self.assertEqual(ex.api_base, "https://api.bybit.com")


class BinancePositionAliasTests(unittest.TestCase):
    def test_position_risk_exposes_import_fields(self) -> None:
        ex = UserBinanceExchange("k" * 8, "s" * 8, demo_trading=True)
        row = {
            "symbol": "BTCUSDT",
            "positionAmt": "-1.5",
            "entryPrice": "100",
            "leverage": "3",
            "isolatedWallet": "50",
            "updateTime": 1700000000000,
        }
        ex._request = lambda *args, **kwargs: [row]  # type: ignore[method-assign]
        pos = ex.positions_map()[("BTCUSDT", "sell")]
        self.assertEqual(pos["size"], 1.5)
        self.assertEqual(float(pos["avgPrice"]), 100.0)
        self.assertEqual(pos["positionIM"], "50")
        self.assertEqual(pos["createdTime"], 1700000000000)


if __name__ == "__main__":
    unittest.main()
