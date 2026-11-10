#!/usr/bin/env python3
import sys
from pathlib import Path as _BootstrapPath

_scripts_root = _BootstrapPath(__file__).resolve().parents[1]
if str(_scripts_root) not in sys.path:
    sys.path.insert(0, str(_scripts_root))

"""
Simple command line client into RPC commands
Can be used as an alternate to Telegram

Should not import anything from ctengine,
so it can be used as a standalone script.
"""

from ctengine_client.ft_client import main


if __name__ == "__main__":
    main()
