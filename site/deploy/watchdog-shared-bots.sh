#!/bin/bash
# Watchdog for shared-bot stack — restart wedged services.
set -euo pipefail

ping_api() {
  curl -sf --max-time "${2:-8}" "$1" >/dev/null
}

if ! ping_api "http://127.0.0.1:8081/api/v1/ping" 10; then
  echo "signal-engine ping failed — restart"
  systemctl restart cryptotools-signal-engine.service
fi

if ! systemctl is-active --quiet trade-executor.service; then
  echo "trade-executor down — restart"
  systemctl restart trade-executor.service
fi

if ! ping_api "http://127.0.0.1:8090/health" 5; then
  echo "pair-config health failed — restart"
  systemctl restart pair-config.service
fi

free -m | awk 'NR==2{if ($3/$2 > 0.92) print "WARN: RAM >92% used"}'
