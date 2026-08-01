#!/bin/bash
# Run ranging-market scanner and apply results to Grid bot whitelist.
set -euo pipefail
CT_BASE="${CT_BASE:-/home/cryptotools/app}"
CT_ENV="${CT_ENV:-/home/cryptotools/.cryptotools.env}"
cd "$CT_BASE"
source "$CT_BASE/scripts/load_env.sh" "$CT_ENV"
LOG_DIR="$CT_BASE/user_data/logs"
mkdir -p "$LOG_DIR"
echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') === ranging scan start ===" >> "$LOG_DIR/ranging-scanner.log"
"$CT_BASE/.venv/bin/python3" "$CT_BASE/scripts/scan_ranging_pairs.py" -v "$@" 2>&1 \
  | "$CT_BASE/scripts/timestamp_pipe.sh" >> "$LOG_DIR/ranging-scanner.log"
