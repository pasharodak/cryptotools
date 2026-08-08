#!/bin/bash
set -euo pipefail
CT_BASE="${CT_BASE:-/home/cryptotools/app}"
CT_ENV="${CT_ENV:-/home/cryptotools/.cryptotools.env}"
cd "$CT_BASE"
source "$CT_BASE/scripts/load_env.sh" "$CT_ENV"
LOG_DIR="$CT_BASE/user_data/logs"
mkdir -p "$LOG_DIR"
echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') === strategy scan start ===" >> "$LOG_DIR/strategy-scanner.log"
"$CT_BASE/.venv/bin/python3" "$CT_BASE/scripts/scan_strategy_pairs.py" -v "$@" 2>&1 \
  | "$CT_BASE/scripts/timestamp_pipe.sh" >> "$LOG_DIR/strategy-scanner.log"
