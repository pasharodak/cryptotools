#!/bin/bash
# Run ranging-market scanner and apply results to Grid bot whitelist.
set -euo pipefail
FT_BASE="${FT_BASE:-/home/freqtrade/freqtrade}"
FT_ENV="${FT_ENV:-/home/freqtrade/.freqtrade.env}"
cd "$FT_BASE"
source "$FT_BASE/scripts/load_env.sh" "$FT_ENV"
LOG_DIR="$FT_BASE/user_data/logs"
mkdir -p "$LOG_DIR"
exec "$FT_BASE/.venv/bin/python3" "$FT_BASE/scripts/scan_ranging_pairs.py" -v "$@" >> "$LOG_DIR/ranging-scanner.log" 2>&1
