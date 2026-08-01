"""Project paths after cryptotools layout (simulation/ + site/)."""
from __future__ import annotations

from pathlib import Path

CRYPTO_ROOT = Path(__file__).resolve().parents[1]
SIM_ROOT = CRYPTO_ROOT / "simulation"
SITE_ROOT = CRYPTO_ROOT / "site"
SCRIPTS = SITE_ROOT / "scripts"
USER_DATA = SITE_ROOT / "user_data"
VENV_DIR = SITE_ROOT / ".venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
VENV_CTBOT = VENV_DIR / "Scripts" / "ctbot.exe"
