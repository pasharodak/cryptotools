#!/bin/bash
# Safe wrapper: only start/stop/restart/is-active tenant bot units.
set -euo pipefail
ACTION="${1:-}"
UNIT="${2:-}"

case "$ACTION" in
  start|stop|restart|is-active) ;;
  daemon-reload)
    exec /bin/systemctl daemon-reload
    ;;
  *)
    echo "invalid action" >&2
    exit 2
    ;;
esac

if [[ ! "$UNIT" =~ ^cryptotools-(strategy|grid|finder)@[A-Za-z0-9_-]+\.service$ ]]; then
  echo "invalid unit" >&2
  exit 2
fi

exec /bin/systemctl "$ACTION" "$UNIT"
