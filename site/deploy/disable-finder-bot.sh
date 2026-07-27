#!/bin/bash
# Disable ML Finder systemd unit so it never starts on boot or deploy.
set -euo pipefail
UNIT="${1:-freqtrade.service}"
APP="${FT_BASE:-/home/freqtrade/freqtrade}"
UNIT_PATH="/etc/systemd/system/${UNIT}"
BACKUP="${APP}/deploy/${UNIT}.disabled"

systemctl stop "$UNIT" 2>/dev/null || true
systemctl disable "$UNIT" 2>/dev/null || true

if [ -f "$UNIT_PATH" ] && [ ! -L "$UNIT_PATH" ]; then
  cp -a "$UNIT_PATH" "$BACKUP"
  rm -f "$UNIT_PATH"
  systemctl daemon-reload
fi

systemctl mask "$UNIT" 2>/dev/null || true
echo "Finder unit: $(systemctl is-enabled "$UNIT" 2>&1 || true) · active: $(systemctl is-active "$UNIT" 2>&1 || true)"
