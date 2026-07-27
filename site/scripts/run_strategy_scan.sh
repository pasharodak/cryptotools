#!/bin/bash
set -euo pipefail
FT_BASE="${FT_BASE:-/home/freqtrade/freqtrade}"
FT_ENV="${FT_ENV:-/home/freqtrade/.freqtrade.env}"
cd "$FT_BASE"
source "$FT_BASE/scripts/load_env.sh" "$FT_ENV"
LOG_DIR="$FT_BASE/user_data/logs"
mkdir -p "$LOG_DIR"
echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') === strategy scan start ===" >> "$LOG_DIR/strategy-scanner.log"
"$FT_BASE/.venv/bin/python3" "$FT_BASE/scripts/scan_strategy_pairs.py" -v "$@" 2>&1 \
  | "$FT_BASE/scripts/timestamp_pipe.sh" >> "$LOG_DIR/strategy-scanner.log"
