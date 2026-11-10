"""Behavior locks for the deepened seams. Expected values are literals."""

from __future__ import annotations
import sys
from pathlib import Path as _BootstrapPath

_scripts_root = _BootstrapPath(__file__).resolve().parents[1]
if str(_scripts_root) not in sys.path:
    sys.path.insert(0, str(_scripts_root))

import unittest

import control.bot_orchestrator as bot_orchestrator
import trading.entry_constraints as entry_constraints
import trading.entry_policy as entry_policy
import control.pair_config_routes as pair_config_routes
import trading.placement_lanes as placement_lanes
import tenants.trading_context as trading_context


class PlacementLaneTests(unittest.TestCase):
    def test_main_test_and_hidden(self) -> None:
        placement = {"main": ["Alpha"], "hidden": ["Beta"]}
        self.assertEqual(placement_lanes.panel_for("Alpha", placement), "main")
        self.assertEqual(placement_lanes.panel_for("Beta", placement), "hidden")
        self.assertEqual(placement_lanes.panel_for("Gamma", placement), "test")

    def test_retired_hour_id_stays_hidden_even_on_main(self) -> None:
        placement = {"main": ["HourStrategy"], "hidden": []}
        self.assertEqual(
            placement_lanes.panel_for("HourStrategy", placement, htf_ids={"HourStrategy"}),
            "hidden",
        )

    def test_test_lane_ids_stay_inside_the_catalog(self) -> None:
        placement = {"main": ["Alpha"], "hidden": ["Beta"]}
        self.assertEqual(
            entry_constraints.test_ids(["Alpha", "Beta", "Gamma"], placement),
            frozenset({"Gamma"}),
        )
        self.assertEqual(
            entry_constraints.test_ids(["Alpha", "Gamma"], placement, htf_ids={"Gamma"}),
            frozenset(),
        )

    def test_full_pair_universe_is_the_test_lane_without_htf(self) -> None:
        placement = {"main": ["Alpha"], "hidden": []}
        self.assertFalse(entry_constraints.uses_full_pair_universe("Alpha", placement))
        self.assertTrue(entry_constraints.uses_full_pair_universe("Gamma", placement))

    def test_cap_reached_when_off_or_full(self) -> None:
        self.assertTrue(entry_constraints.cap_reached(0, 0))
        self.assertTrue(entry_constraints.cap_reached(2, 2))
        self.assertFalse(entry_constraints.cap_reached(1, 2))


class TradingContextTests(unittest.TestCase):
    def test_binance_demo_id_round_trip(self) -> None:
        cid = trading_context.exchange_context_id("alice", exchange="binance", mode="demo")
        self.assertEqual(cid, "alice__binance__demo")
        ctx = trading_context.TradingContext.parse(cid)
        self.assertEqual(ctx.parent_login_id, "alice")
        self.assertEqual(ctx.exchange, "binance")
        self.assertEqual(ctx.mode, "demo")
        self.assertEqual(ctx.secrets_scope, "alice__binance__demo")
        self.assertEqual(trading_context.flag_context_id(cid), "alice__demo")

    def test_bybit_live_id_is_the_login(self) -> None:
        self.assertEqual(trading_context.live_context_id("alice"), "alice")
        self.assertEqual(trading_context.demo_context_id("alice"), "alice__demo")
        self.assertEqual(
            trading_context.iter_user_contexts("alice"),
            ["alice", "alice__demo", "alice__binance", "alice__binance__demo"],
        )


class EntryPolicyTests(unittest.TestCase):
    def test_quiet_skip_is_only_a_disabled_exchange(self) -> None:
        self.assertTrue(
            entry_policy.is_quiet_skip("тестовые: биржа bybit выключена (trade_on_test=binance)")
        )
        self.assertFalse(entry_policy.is_quiet_skip("лимит открытых сделок 1/1"))

    def test_allowed_and_skipped_are_distinct(self) -> None:
        self.assertIsInstance(entry_policy.Allowed(), entry_policy.Allowed)
        skipped = entry_policy.Skipped(reason="лимит", quiet=False)
        self.assertEqual(skipped.reason, "лимит")
        self.assertFalse(skipped.quiet)


class PairConfigRouteTests(unittest.TestCase):
    def test_public_routes_skip_auth(self) -> None:
        self.assertEqual(pair_config_routes.classify("GET", "/health"), ("public", "health"))
        self.assertEqual(pair_config_routes.classify("POST", "/auth/login"), ("public", "login"))
        self.assertEqual(
            pair_config_routes.classify("POST", "/auth/telegram-webapp"),
            ("public", "telegram"),
        )

    def test_user_routes_keep_their_method(self) -> None:
        self.assertEqual(pair_config_routes.classify("GET", "/bots"), ("user", "get"))
        self.assertEqual(
            pair_config_routes.classify("GET", "/bot-proxy/strategy/status"),
            ("user", "bot_proxy"),
        )
        self.assertEqual(pair_config_routes.classify("PUT", "/trade-chart"), ("user", "put"))
        self.assertEqual(pair_config_routes.classify("OPTIONS", "/bots"), ("user", "method_not_allowed"))


class OrchestratorTests(unittest.TestCase):
    def test_windows_adapter_is_selected_on_windows(self) -> None:
        seen: list[str] = []

        def linux(user_id: str, bot: str) -> tuple[bool, str]:
            seen.append("linux")
            return True, ""

        def win(user_id: str, bot: str) -> tuple[bool, str]:
            seen.append("win")
            return True, user_id + bot

        ok, err = bot_orchestrator.ensure_with_adapter(
            "u", "grid", windows=True, linux=linux, win=win
        )
        self.assertTrue(ok)
        self.assertEqual(err, "ugrid")
        self.assertEqual(seen, ["win"])

    def test_apply_desired_calls_disable_when_off(self) -> None:
        seen: list[str] = []

        def enable(user_id: str, bot: str) -> tuple[bool, str]:
            seen.append("on")
            return True, ""

        def disable(user_id: str, bot: str) -> tuple[bool, str]:
            seen.append("off")
            return False, "stopped"

        ok, err = bot_orchestrator.apply_desired(
            "u", "finder", False, enable=enable, disable=disable
        )
        self.assertFalse(ok)
        self.assertEqual(err, "stopped")
        self.assertEqual(seen, ["off"])


if __name__ == "__main__":
    unittest.main()
