#!/usr/bin/env python3
"""One-shot: add test-block clones for main #4-10 + #31/#33/#34 (skip already-tested)."""
from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Shared test risk (like Supertrend/CMF tests)
TEST_SL = -0.03
TEST_TP = 0.012
TEST_ROI = {"0": 0.012, "60": 0.008, "180": 0.005, "480": 0.0}
TEST_COOLDOWN = 180

SPECS = [
    {
        "num": 106,
        "live_num": 4,
        "class_name": "ScalpEmaCrossTestStrategy",
        "live_class": "ScalpEmaCrossStrategy",
        "scenario_id": "scalp_ema_test",
        "source_scenario": "scalp_ema",
        "label": "Scalp EMA 8/21 (test)",
        "group": "scalp_test",
        "sim_module": "SimScalpingStrategies",
        "sim_file": ROOT / "simulation/strategies/SimScalpingStrategies.py",
        "parent": "ScalpEmaCrossStrategy",
        "gate": 0.55,
        "exit_reason": "ema_flip",
        "exit_code": """
        e8 = float(last.get("ema8") or 0)
        e21 = float(last.get("ema21") or 0)
        if e8 <= 0 or e21 <= 0:
            return None
        if trade.is_short and e8 > e21:
            return "ema_flip"
        if (not trade.is_short) and e8 < e21:
            return "ema_flip"
        return None
""",
        "score_as": "SupertrendStrategy",
        "wrapper_import": "simulation.strategies.SimScalpingStrategies",
    },
    {
        "num": 107,
        "live_num": 5,
        "class_name": "ChaikinOscTestStrategy",
        "live_class": "ChaikinOscStrategy",
        "scenario_id": "chart3_adosc_test",
        "source_scenario": "chart3_adosc",
        "label": "Chaikin Oscillator (test)",
        "group": "chart3_test",
        "sim_module": "SimChartTaWave3Strategies",
        "sim_file": ROOT / "simulation/strategies/SimChartTaWave3Strategies.py",
        "parent": "ChaikinOscStrategy",
        "gate": 0.45,
        "exit_reason": "adosc_flip",
        "exit_code": """
        v = last.get("adosc")
        if v is None:
            return None
        v = float(v)
        if trade.is_short and v > 0:
            return "adosc_flip"
        if (not trade.is_short) and v < 0:
            return "adosc_flip"
        return None
""",
        "score_as": "AdxMomentumStrategy",
        "wrapper_import": "simulation.strategies.SimChartTaWave3Strategies",
    },
    {
        "num": 108,
        "live_num": 6,
        "class_name": "DonchianBreakoutTestStrategy",
        "live_class": "DonchianBreakoutStrategy",
        "scenario_id": "new_donchian_test",
        "source_scenario": "new_donchian",
        "label": "Donchian / Turtle (test)",
        "group": "newset_test",
        "sim_module": "SimNewSetStrategies",
        "sim_file": ROOT / "simulation/strategies/SimNewSetStrategies.py",
        "parent": "DonchianBreakoutStrategy",
        "gate": 0.45,
        "exit_reason": "don_mid",
        "exit_code": """
        hi = last.get("don_high")
        lo = last.get("don_low")
        close = float(last.get("close") or current_rate or 0)
        if hi is None or lo is None or close <= 0:
            return None
        mid = (float(hi) + float(lo)) / 2.0
        if trade.is_short and close > mid:
            return "don_mid"
        if (not trade.is_short) and close < mid:
            return "don_mid"
        return None
""",
        "score_as": "AdxMomentumStrategy",
        "wrapper_import": "simulation.strategies.SimNewSetStrategies",
    },
    {
        "num": 109,
        "live_num": 7,
        "class_name": "PpoSignalTestStrategy",
        "live_class": "PpoSignalStrategy",
        "scenario_id": "chart3_ppo_test",
        "source_scenario": "chart3_ppo",
        "label": "PPO signal cross (test)",
        "group": "chart3_test",
        "sim_module": "SimChartTaWave3Strategies",
        "sim_file": ROOT / "simulation/strategies/SimChartTaWave3Strategies.py",
        "parent": "PpoSignalStrategy",
        "gate": 0.55,
        "exit_reason": "ppo_flip",
        "exit_code": """
        ppo = last.get("ppo")
        sig = last.get("ppo_sig")
        if ppo is None or sig is None:
            return None
        ppo, sig = float(ppo), float(sig)
        if trade.is_short and ppo > sig:
            return "ppo_flip"
        if (not trade.is_short) and ppo < sig:
            return "ppo_flip"
        return None
""",
        "score_as": "AdxMomentumStrategy",
        "wrapper_import": "simulation.strategies.SimChartTaWave3Strategies",
    },
    {
        "num": 110,
        "live_num": 8,
        "class_name": "DonchianAdxVolComboTestStrategy",
        "live_class": "DonchianAdxVolComboStrategy",
        "scenario_id": "combo_don_adx_vol_test",
        "source_scenario": "combo_don_adx_vol",
        "label": "Donchian+ADX+Vol (test)",
        "group": "combo_test",
        "sim_module": "SimComboStrategies",
        "sim_file": ROOT / "simulation/strategies/SimComboStrategies.py",
        "parent": "DonchianAdxVolComboStrategy",
        "gate": 0.65,
        "exit_reason": "don_mid",
        "exit_code": """
        hi = last.get("don_high")
        lo = last.get("don_low")
        close = float(last.get("close") or current_rate or 0)
        if hi is None or lo is None or close <= 0:
            return None
        mid = (float(hi) + float(lo)) / 2.0
        if trade.is_short and close > mid:
            return "don_mid"
        if (not trade.is_short) and close < mid:
            return "don_mid"
        return None
""",
        "score_as": "AdxMomentumStrategy",
        "wrapper_import": "simulation.strategies.SimComboStrategies",
    },
    {
        "num": 111,
        "live_num": 9,
        "class_name": "ObvEmaCrossTestStrategy",
        "live_class": "ObvEmaCrossStrategy",
        "scenario_id": "chart2_obv_test",
        "source_scenario": "chart2_obv",
        "label": "OBV EMA cross (test)",
        "group": "chart2_test",
        "sim_module": "SimChartTaWave2Strategies",
        "sim_file": ROOT / "simulation/strategies/SimChartTaWave2Strategies.py",
        "parent": "ObvEmaCrossStrategy",
        "gate": 0.45,
        "exit_reason": "obv_flip",
        "exit_code": """
        obv = last.get("obv")
        ema = last.get("obv_ema")
        if obv is None or ema is None:
            return None
        obv, ema = float(obv), float(ema)
        if trade.is_short and obv > ema:
            return "obv_flip"
        if (not trade.is_short) and obv < ema:
            return "obv_flip"
        return None
""",
        "score_as": "AdxMomentumStrategy",
        "wrapper_import": "simulation.strategies.SimChartTaWave2Strategies",
    },
    {
        "num": 112,
        "live_num": 10,
        "class_name": "ElderRayTestStrategy",
        "live_class": "ElderRayStrategy",
        "scenario_id": "chart3_elder_test",
        "source_scenario": "chart3_elder",
        "label": "Elder Ray Bull/Bear (test)",
        "group": "chart3_test",
        "sim_module": "SimChartTaWave3Strategies",
        "sim_file": ROOT / "simulation/strategies/SimChartTaWave3Strategies.py",
        "parent": "ElderRayStrategy",
        "gate": 0.45,
        "exit_reason": "elder_flip",
        "exit_code": """
        bear = last.get("bear_power")
        bull = last.get("bull_power")
        if bear is None or bull is None:
            return None
        bear, bull = float(bear), float(bull)
        if trade.is_short and bull > 0:
            return "elder_flip"
        if (not trade.is_short) and bear < 0:
            return "elder_flip"
        return None
""",
        "score_as": "AdxMomentumStrategy",
        "wrapper_import": "simulation.strategies.SimChartTaWave3Strategies",
    },
    {
        "num": 113,
        "live_num": 31,
        "class_name": "AltVolumeBreakoutTestStrategy",
        "live_class": "AltVolumeBreakoutStrategy",
        "scenario_id": "scalp_liq_breakout_test",
        "source_scenario": "scalp_liq_breakout",
        "label": "Alt volume breakout (test)",
        "group": "scalp_liq_test",
        "sim_module": "SimLiquidityScalpStrategies",
        "sim_file": ROOT / "simulation/strategies/SimLiquidityScalpStrategies.py",
        "parent": "AltVolumeBreakoutStrategy",
        "gate": 0.55,
        "exit_reason": "don_mid",
        "exit_code": """
        mid = last.get("don_mid")
        close = float(last.get("close") or current_rate or 0)
        if mid is None or close <= 0:
            return None
        mid = float(mid)
        if trade.is_short and close > mid:
            return "don_mid"
        if (not trade.is_short) and close < mid:
            return "don_mid"
        return None
""",
        "score_as": "AdxMomentumStrategy",
        "wrapper_import": "simulation.strategies.SimLiquidityScalpStrategies",
    },
    {
        "num": 114,
        "live_num": 33,
        "class_name": "BollingerRsiTestStrategy",
        "live_class": "BollingerRsiStrategy",
        "scenario_id": "lite_mean_rev_test",
        "source_scenario": "lite_mean_rev",
        "label": "Mean-reversion (BB) (test)",
        "group": "lite_test",
        "sim_module": "SimMeanReversionRange",
        "sim_file": ROOT / "simulation/strategies/SimMeanReversionRange.py",
        "parent": "SimMeanReversionRange",
        "gate": 0.70,
        "exit_reason": "bb_mid",
        "exit_code": """
        mid = last.get("bb_mid")
        close = float(last.get("close") or current_rate or 0)
        if mid is None or close <= 0:
            return None
        mid = float(mid)
        if trade.is_short and close < mid:
            return "bb_mid"
        if (not trade.is_short) and close > mid:
            return "bb_mid"
        return None
""",
        "score_as": "BollingerRsiStrategy",
        "wrapper_import": "simulation.strategies.SimMeanReversionRange",
        "wrapper_alias": "BollingerRsiTestStrategy",
    },
    {
        "num": 115,
        "live_num": 34,
        "class_name": "MacdEmaTestStrategy",
        "live_class": "MacdEmaStrategy",
        "scenario_id": "trend_macd_ema_test",
        "source_scenario": "trend_macd_ema",
        "label": "MACD + EMA200 (test)",
        "group": "trend_test",
        "sim_module": "SimClassicStrategies",
        "sim_file": ROOT / "simulation/strategies/SimClassicStrategies.py",
        "parent": "SimMacdEma",
        "gate": 0.45,
        "exit_reason": "macd_flip",
        "exit_code": """
        macd = last.get("macd")
        sig = last.get("macdsignal")
        if macd is None or sig is None:
            return None
        macd, sig = float(macd), float(sig)
        if trade.is_short and macd > sig:
            return "macd_flip"
        if (not trade.is_short) and macd < sig:
            return "macd_flip"
        return None
""",
        "score_as": "MacdEmaStrategy",
        "wrapper_import": "simulation.strategies.SimClassicStrategies",
        "wrapper_alias": "MacdEmaTestStrategy",
    },
]


def _test_class_src(spec: dict) -> str:
    exit_body = textwrap.dedent(spec["exit_code"]).strip()
    exit_body = textwrap.indent(exit_body, "        ")
    return f'''

class {spec["class_name"]}({spec["parent"]}):
    """UI test clone of #{spec["live_num"]}: 1x, SL -3%, no RSI/range chase, custom exit."""

    stoploss = {TEST_SL}
    minimal_roi = {TEST_ROI!r}
    pair_cooldown_minutes = {TEST_COOLDOWN}
    sim_leverage = 1.0
    rsi_long_max = 65
    rsi_short_min = 35
    range_lookback_1h = 12
    max_long_range_pos = 0.75
    min_short_range_pos = 0.25

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        dataframe["rsi_14"] = ta.RSI(dataframe, timeperiod=14)
        hi = dataframe["high"].rolling(self.range_lookback_1h).max()
        lo = dataframe["low"].rolling(self.range_lookback_1h).min()
        span = (hi - lo).where((hi - lo) > 0)
        dataframe["range_pos_1h"] = (dataframe["close"] - lo) / span
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        rsi = dataframe["rsi_14"]
        pos = dataframe["range_pos_1h"]
        late_long = (rsi >= self.rsi_long_max) | (pos >= self.max_long_range_pos)
        late_short = (rsi <= self.rsi_short_min) | (pos <= self.min_short_range_pos)
        dataframe.loc[late_long, "enter_long"] = 0
        dataframe.loc[late_short, "enter_short"] = 0
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return min(self.sim_leverage, max_leverage)

    def exit_reason_from_ohlcv(self, dataframe: DataFrame, trade, current_rate: float) -> str | None:
        if dataframe is None or len(dataframe) < 30:
            return None
        df = self.populate_indicators(dataframe.copy(), {{"pair": getattr(trade, "pair", "")}})
        last = df.iloc[-1]
{exit_body}
'''


def append_sim_classes() -> None:
    by_file: dict[Path, list[dict]] = {}
    for s in SPECS:
        by_file.setdefault(s["sim_file"], []).append(s)
    for path, specs in by_file.items():
        text = path.read_text(encoding="utf-8")
        for s in specs:
            if f"class {s['class_name']}(" in text:
                print(f"skip sim class {s['class_name']} (exists)")
                continue
            if "import talib.abstract as ta" not in text and "talib.abstract as ta" not in text:
                # SimMeanReversionRange already imports ta
                pass
            chunk = _test_class_src(s)
            path.write_text(text + chunk, encoding="utf-8")
            text = path.read_text(encoding="utf-8")
            print(f"appended {s['class_name']} -> {path.name}")


def write_wrappers() -> None:
    strat_dir = ROOT / "site/user_data/strategies"
    for s in SPECS:
        path = strat_dir / f"{s['class_name']}.py"
        if path.exists():
            print(f"skip wrapper {path.name}")
            continue
        alias = s.get("wrapper_alias", s["class_name"])
        # For SimMacdEma / SimMeanReversionRange the class name in module differs
        if s["parent"].startswith("Sim"):
            import_cls = s["parent"]
            body = (
                f"from {s['wrapper_import']} import {import_cls} as _Base\n\n"
                f"class {s['class_name']}(SimLiveCooldownMixin, _Base):\n"
                f"    # test overrides live on sim subclass {s['class_name']} if defined there\n"
                f"    pass\n"
            )
            # Actually for Bollinger/Macd the Test class IS in the sim file as BollingerRsiTestStrategy
            # subclassing SimMeanReversionRange / SimMacdEma — wrappers should import the Test class
            body = (
                f'from _sim_live import SimLiveCooldownMixin\n'
                f'from {s["wrapper_import"]} import {s["class_name"]} as _Sim\n\n\n'
                f'class {s["class_name"]}(SimLiveCooldownMixin, _Sim):\n'
                f'    pass\n'
            )
        else:
            body = (
                f'from _sim_live import SimLiveCooldownMixin\n'
                f'from {s["wrapper_import"]} import {s["class_name"]} as _Sim\n\n\n'
                f'class {s["class_name"]}(SimLiveCooldownMixin, _Sim):\n'
                f'    pass\n'
            )
        path.write_text(
            "# pragma pylint: disable=missing-docstring, invalid-name\n"
            f'"""Test-block clone of live #{s["live_num"]} {s["live_class"]}."""\n\n'
            + body,
            encoding="utf-8",
        )
        print(f"wrote wrapper {path.name}")


def patch_router() -> None:
    path = ROOT / "site/user_data/strategies/MultiStrategyRouter.py"
    text = path.read_text(encoding="utf-8")

    # Imports — after CmfZeroCrossTestStrategy import line
    for s in SPECS:
        line = f"from {s['class_name']} import {s['class_name']}\n"
        if line not in text:
            # insert alphabetically-ish near related imports: after live class import if present
            live_imp = f"from {s['live_class']} import {s['live_class']}\n"
            if live_imp in text:
                text = text.replace(live_imp, live_imp + line, 1)
            else:
                text = text.replace(
                    "from CmfZeroCrossTestStrategy import CmfZeroCrossTestStrategy\n",
                    "from CmfZeroCrossTestStrategy import CmfZeroCrossTestStrategy\n" + line,
                    1,
                )

    # TEST_STRATEGY_TAGS
    if "ScalpEmaCrossTestStrategy" not in text.split("TEST_STRATEGY_TAGS")[1].split(")")[0]:
        text = text.replace(
            '"CmfZeroCrossTestStrategy",\n    }\n)',
            '"CmfZeroCrossTestStrategy",\n'
            + "".join(f'        "{s["class_name"]}",\n' for s in SPECS)
            + "    }\n)",
            1,
        )

    # STRATEGY_REGISTRY — after live entries
    for s in SPECS:
        entry = f'    "{s["class_name"]}": {s["class_name"]},\n'
        if entry not in text:
            live_e = f'    "{s["live_class"]}": {s["live_class"]},\n'
            if live_e in text:
                text = text.replace(live_e, live_e + entry, 1)
            else:
                text = text.replace(
                    '    "CmfZeroCrossTestStrategy": CmfZeroCrossTestStrategy,\n',
                    '    "CmfZeroCrossTestStrategy": CmfZeroCrossTestStrategy,\n' + entry,
                    1,
                )

    # TAG_RISK
    for s in SPECS:
        if f'"{s["class_name"]}":' in text.split("TAG_RISK")[1].split("MEAN_REV")[0]:
            continue
        block = (
            f'    "{s["class_name"]}": {{\n'
            f'        "stoploss": {TEST_SL},\n'
            f'        "tp": {TEST_TP},\n'
            f'        "minimal_roi": {json.dumps(TEST_ROI)},\n'
            f'    }},\n'
        )
        live_key = f'    "{s["live_class"]}":'
        idx = text.find("TAG_RISK")
        live_pos = text.find(live_key, idx)
        if live_pos > 0:
            # find end of live dict entry (next line starting with 4 spaces + quote after closing brace)
            brace = text.find("},", live_pos)
            insert_at = brace + 2
            if text[insert_at : insert_at + 1] == "\n":
                insert_at += 1
            text = text[:insert_at] + block + text[insert_at:]
        else:
            text = text.replace(
                '    "CmfZeroCrossTestStrategy": {\n'
                '        "stoploss": -0.03,\n'
                '        "tp": 0.012,\n'
                '        "minimal_roi": {"0": 0.012, "60": 0.008, "180": 0.005, "480": 0.0},\n'
                "    },\n",
                '    "CmfZeroCrossTestStrategy": {\n'
                '        "stoploss": -0.03,\n'
                '        "tp": 0.012,\n'
                '        "minimal_roi": {"0": 0.012, "60": 0.008, "180": 0.005, "480": 0.0},\n'
                "    },\n" + block,
                1,
            )

    # SCENARIO_BY_TAG
    for s in SPECS:
        if f'"{s["class_name"]}":' in text.split("SCENARIO_BY_TAG")[1]:
            continue
        block = (
            f'    "{s["class_name"]}": {{\n'
            f'        "scenario_id": "{s["scenario_id"]}",\n'
            f'        "scan_type": "strategy",\n'
            f'        "group": "{s["group"]}",\n'
            f'        "strategy": "{s["class_name"]}",\n'
            f'        "label": "{s["label"]} wide",\n'
            f'    }},\n'
        )
        live_key = f'    "{s["live_class"]}":'
        scen_idx = text.find("SCENARIO_BY_TAG")
        live_pos = text.find(live_key, scen_idx)
        if live_pos > 0:
            brace = text.find("},", live_pos)
            insert_at = brace + 2
            if text[insert_at : insert_at + 1] == "\n":
                insert_at += 1
            text = text[:insert_at] + block + text[insert_at:]
        else:
            text = text.replace(
                '    "CmfZeroCrossTestStrategy": {\n'
                '        "scenario_id": "chart2_cmf_test",\n'
                '        "scan_type": "strategy",\n'
                '        "group": "chart2_test",\n'
                '        "strategy": "CmfZeroCrossTestStrategy",\n'
                '        "label": "CMF zero cross (test wide)",\n'
                "    },\n",
                '    "CmfZeroCrossTestStrategy": {\n'
                '        "scenario_id": "chart2_cmf_test",\n'
                '        "scan_type": "strategy",\n'
                '        "group": "chart2_test",\n'
                '        "strategy": "CmfZeroCrossTestStrategy",\n'
                '        "label": "CMF zero cross (test wide)",\n'
                "    },\n" + block,
                1,
            )

    # MEAN_REV may need BollingerRsiTest
    if "BollingerRsiTestStrategy" not in text.split("MEAN_REV_ADX_TAGS")[1].split(")")[0]:
        text = text.replace(
            'MEAN_REV_ADX_TAGS = frozenset({"BollingerRsiStrategy", "LiteRangeStrategy", "CriptoPairsStrategy"})',
            'MEAN_REV_ADX_TAGS = frozenset({"BollingerRsiStrategy", "BollingerRsiTestStrategy", "LiteRangeStrategy", "CriptoPairsStrategy"})',
            1,
        )

    path.write_text(text, encoding="utf-8")
    print("patched MultiStrategyRouter.py")


def patch_pack() -> None:
    path = ROOT / "simulation/config/prod_top30_pack.json"
    pack = json.loads(path.read_text(encoding="utf-8"))
    existing = {s["class_name"] for s in pack["strategies"]}
    for s in SPECS:
        if s["class_name"] in existing:
            continue
        pack["strategies"].append(
            {
                "rank": s["num"],
                "num": s["num"],
                "ui_order": s["num"],
                "scenario_id": s["scenario_id"],
                "class_name": s["class_name"],
                "label": s["label"],
                "group": s["group"],
                "test_group": True,
                "stoploss": TEST_SL,
                "tp": TEST_TP,
                "winner_exp": "test_block_xgb_wide",
                "score_ml_test_pnl": 0.0,
                "min_profit_proba": s["gate"],
                "sim_module": s["sim_module"],
                "minimal_roi": TEST_ROI,
            }
        )
    path.write_text(json.dumps(pack, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("patched prod_top30_pack.json")


def patch_player_scenarios() -> None:
    path = ROOT / "simulation/config/player_scenarios.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    existing = {x["id"] for x in data}
    for s in SPECS:
        if s["scenario_id"] in existing:
            continue
        # insert after source scenario if present
        entry = {
            "id": s["scenario_id"],
            "label": s["label"],
            "enabled": False,
            "strategy": s["class_name"],
            "strategy_path": "simulation/strategies",
            "config": "simulation/config/backtest_scalp_base.json",
            "scan_type": "strategy",
            "group": s["group"],
            "stake_usdt": 15,
            "stoploss": TEST_SL,
            "minimal_roi": TEST_ROI,
            "settings": f"Тест · #{s['live_num']} · без chase RSI/1h · 1x · SL −3% · TP 1.2% · выход {s['exit_reason']}",
        }
        src_idx = next((i for i, x in enumerate(data) if x["id"] == s["source_scenario"]), None)
        if src_idx is not None:
            data.insert(src_idx + 1, entry)
        else:
            data.append(entry)
    path.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    print("patched player_scenarios.json")


def patch_pair_config() -> None:
    path = ROOT / "site/scripts/pair_config_server.py"
    text = path.read_text(encoding="utf-8")
    anchor = (
        '    {\n'
        '        "id": "CmfZeroCrossTestStrategy",\n'
        '        "num": 105,\n'
        '        "ui_order": 105,\n'
        '        "name": "CMF zero cross (test)",\n'
        '        "test_group": True,\n'
        '        "desc": "Тест · wide XGB · ML PnL ~78 · 1x · SL -3% · без chase (RSI/1h range) · выход cmf_flip · gate>=55%",\n'
        "    },\n"
    )
    if "ScalpEmaCrossTestStrategy" in text:
        print("pair_config already has new tests")
        return
    block = ""
    for s in SPECS:
        block += (
            "    {\n"
            f'        "id": "{s["class_name"]}",\n'
            f'        "num": {s["num"]},\n'
            f'        "ui_order": {s["num"]},\n'
            f'        "name": "{s["label"]}",\n'
            '        "test_group": True,\n'
            f'        "desc": "Тест · wide XGB · 1x · SL -3% · без chase · выход {s["exit_reason"]} · live #{s["live_num"]} · gate>={int(s["gate"]*100)}%",\n'
            "    },\n"
        )
    if anchor not in text:
        raise SystemExit("pair_config anchor missing")
    path.write_text(text.replace(anchor, anchor + block, 1), encoding="utf-8")
    print("patched pair_config_server.py")


def patch_scan() -> None:
    path = ROOT / "site/scripts/scan_strategy_pairs.py"
    text = path.read_text(encoding="utf-8")
    for s in SPECS:
        if f'"{s["class_name"]}"' not in text.split("STRATEGY_IDS")[1].split("]")[0]:
            text = text.replace(
                '    "CmfZeroCrossTestStrategy",\n]',
                f'    "CmfZeroCrossTestStrategy",\n    "{s["class_name"]}",\n]',
                1,
            )
        score_line = f'    "{s["class_name"]}": "{s["score_as"]}",\n'
        if score_line not in text:
            text = text.replace(
                '    "CmfZeroCrossTestStrategy": "AdxMomentumStrategy",\n}',
                '    "CmfZeroCrossTestStrategy": "AdxMomentumStrategy",\n' + score_line + "}",
                1,
            )
    path.write_text(text, encoding="utf-8")
    print("patched scan_strategy_pairs.py")


def patch_deploy() -> None:
    path = ROOT / "site/scripts/deploy_prod_ml.ps1"
    text = path.read_text(encoding="utf-8")
    for s in SPECS:
        if f'"{s["scenario_id"]}"' in text:
            continue
        text = text.replace(
            '"chart2_cmf_test"))',
            f'"chart2_cmf_test", "{s["scenario_id"]}"))',
            1,
        )
    path.write_text(text, encoding="utf-8")
    print("patched deploy_prod_ml.ps1")


def patch_train_specs() -> None:
    path = ROOT / "simulation/scripts/train_test_block_3scen_ml.py"
    text = path.read_text(encoding="utf-8")
    if "scalp_ema_test" in text:
        print("train specs already patched")
        return
    extra = ""
    for s in SPECS:
        src_kind = "trade_db" if s["source_scenario"].startswith(("trend_", "lite_")) else "cache"
        extra += f'''    {{
        "source_scenario": "{s["source_scenario"]}",
        "source": "{src_kind}",
        "scenario_id": "{s["scenario_id"]}",
        "class_name": "{s["class_name"]}",
        "label": "{s["label"]} wide",
        "default_gate": {s["gate"]},
    }},
'''
    text = text.replace(
        '        "default_gate": 0.55,\n    },\n]\n',
        '        "default_gate": 0.55,\n    },\n' + extra + "]\n",
        1,
    )
    path.write_text(text, encoding="utf-8")
    print("patched train_test_block_3scen_ml.py")


def patch_enabled() -> None:
    for rel in ("site/user_data/enabled_strategies.json", "site/user_data/bot_strategies.json"):
        path = ROOT / rel
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        sim_map = data.setdefault("_sim_map", {})
        for s in SPECS:
            sid = s["class_name"]
            if sid not in data:
                data[sid] = {
                    "enabled": False,
                    "ml_confidence": int(s["gate"] * 100),
                    "trained_risk": True,
                    "inverted": False,
                }
            sim_map[sid] = s["sim_module"] if s["sim_module"] != "SimMeanReversionRange" else "SimMeanReversionRange"
            if s["class_name"] == "MacdEmaTestStrategy":
                sim_map[sid] = "SimClassicStrategies"
            if s["class_name"] == "BollingerRsiTestStrategy":
                sim_map[sid] = "SimMeanReversionRange"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"patched {rel}")


def bootstrap_models() -> None:
    for s in SPECS:
        src = ROOT / f"site/user_data/models/pnl_classifier/by_scenario/{s['source_scenario']}"
        dst_site = ROOT / f"site/user_data/models/pnl_classifier/by_scenario/{s['scenario_id']}"
        dst_sim = ROOT / f"simulation/models/pnl_classifier/by_scenario/{s['scenario_id']}"
        if not src.is_dir():
            print(f"WARN: no source model {src}")
            continue
        for dst in (dst_site, dst_sim):
            dst.mkdir(parents=True, exist_ok=True)
            for name in ("pnl_classifier.joblib", "pnl_classifier_meta.json"):
                sp = src / name
                if sp.exists():
                    shutil.copy2(sp, dst / name)
            meta_p = dst / "pnl_classifier_meta.json"
            if meta_p.exists():
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
                meta["strategy"] = s["class_name"]
                meta["scenario_id"] = s["scenario_id"]
                meta["min_profit_proba"] = s["gate"]
                meta["note"] = "bootstrapped from " + s["source_scenario"] + "; retrain via train_test_block_3scen_ml.py"
                meta_p.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"bootstrapped model {s['scenario_id']}")


def patch_agents() -> None:
    path = ROOT / "AGENTS.md"
    text = path.read_text(encoding="utf-8")
    needle = "- #105 `CmfZeroCrossTestStrategy` (`chart2_cmf_test`) — 1x · SL −3% · без chase · выход `cmf_flip`"
    if "ScalpEmaCrossTestStrategy" in text:
        return
    extra = "\n".join(
        f"- #{s['num']} `{s['class_name']}` (`{s['scenario_id']}`) — clone live #{s['live_num']} · 1x · SL −3% · без chase · выход `{s['exit_reason']}`"
        for s in SPECS
    )
    if needle in text:
        text = text.replace(needle, needle + "\n" + extra, 1)
    else:
        # older wording without cmf details
        alt = "- #105 `CmfZeroCrossTestStrategy` (`chart2_cmf_test`)"
        if alt in text:
            text = text.replace(alt, alt + "\n" + extra, 1)
    text = text.replace(
        "nums 101+ = UI test_group",
        "nums 101–115 = UI test_group",
    )
    path.write_text(text, encoding="utf-8")
    print("patched AGENTS.md")


def main() -> None:
    append_sim_classes()
    write_wrappers()
    patch_router()
    patch_pack()
    patch_player_scenarios()
    patch_pair_config()
    patch_scan()
    patch_deploy()
    patch_train_specs()
    patch_enabled()
    bootstrap_models()
    patch_agents()
    print("DONE")


if __name__ == "__main__":
    main()
