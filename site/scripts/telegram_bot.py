"""Compatibility entry. Implementation: telegram/telegram_bot.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "telegram" / "telegram_bot.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    raise ImportError("telegram_bot moved to telegram.telegram_bot. Import telegram.telegram_bot.")
