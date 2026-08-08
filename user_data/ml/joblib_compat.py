"""Register stub modules so joblib can unpickle sim-trained sklearn pipelines on live."""
from __future__ import annotations

import sys
import types

from ml.preprocess import _FramePreprocess

_REGISTERED = False


def register_simulation_stubs() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    pnl_mod = types.ModuleType("simulation.ml.pnl_classifier")
    pnl_mod._FramePreprocess = _FramePreprocess
    finder_mod = types.ModuleType("simulation.ml.trade_finder")
    finder_mod._FramePreprocess = _FramePreprocess

    ml_mod = types.ModuleType("simulation.ml")
    ml_mod.pnl_classifier = pnl_mod
    ml_mod.trade_finder = finder_mod

    sim_mod = types.ModuleType("simulation")
    sim_mod.ml = ml_mod

    sys.modules.setdefault("simulation", sim_mod)
    sys.modules.setdefault("simulation.ml", ml_mod)
    sys.modules.setdefault("simulation.ml.pnl_classifier", pnl_mod)
    sys.modules.setdefault("simulation.ml.trade_finder", finder_mod)
    _REGISTERED = True
