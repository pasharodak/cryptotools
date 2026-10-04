#!/bin/bash
# Stop per-tenant ctbot units; shared signal engine + executor replace them.
set -euo pipefail
APP="${CT_BASE:-/home/cryptotools/app}"

echo "=== stop tenant bot instances ==="
systemctl list-units 'cryptotools-*@*' --all --no-pager --plain 2>/dev/null | awk '{print $1}' | grep '@' | while read -r u; do
  systemctl stop "$u" 2>/dev/null || true
  systemctl disable "$u" 2>/dev/null || true
done

echo "=== stop legacy admin grid/finder/strategy ==="
for u in cryptotools-grid cryptotools-finder cryptotools-strategy; do
  systemctl stop "$u" 2>/dev/null || true
  systemctl disable "$u" 2>/dev/null || true
done

echo "=== enable shared stack ==="
cp -a "$APP/deploy/cryptotools-signal-engine.service" /etc/systemd/system/
cp -a "$APP/deploy/trade-executor.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cryptotools-signal-engine.service
systemctl enable --now trade-executor.service

echo "=== status ==="
systemctl is-active cryptotools-signal-engine trade-executor pair-config
