#!/usr/bin/env bash
# Load secrets into CryptoTools environment variables.
# Usage: source scripts/load_env.sh [/path/to/.env]

ENV_FILE="${1:-$HOME/.cryptotools.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Env file not found: $ENV_FILE" >&2
  return 1 2>/dev/null || exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# Strip Windows CRLF from values (breaks HTTP headers on Linux)
strip_crlf() { printf '%s' "$1" | tr -d '\r'; }
BYBIT_API_KEY=$(strip_crlf "${BYBIT_API_KEY:-}")
BYBIT_API_SECRET=$(strip_crlf "${BYBIT_API_SECRET:-}")
TELEGRAM_BOT_TOKEN=$(strip_crlf "${TELEGRAM_BOT_TOKEN:-}")
TELEGRAM_WEBAPP_URL=$(strip_crlf "${TELEGRAM_WEBAPP_URL:-}")
TRADE_OWNER_USER_ID=$(strip_crlf "${TRADE_OWNER_USER_ID:-}")
export TELEGRAM_WEBAPP_URL

export CTENGINE__EXCHANGE__KEY="${BYBIT_API_KEY:-$CTENGINE__EXCHANGE__KEY}"
export CTENGINE__EXCHANGE__SECRET="${BYBIT_API_SECRET:-$CTENGINE__EXCHANGE__SECRET}"
# Demo Trading keys must use api-demo.bybit.com (ctengine enable_demo_trading).
BYBIT_DEMO_TRADING=$(strip_crlf "${BYBIT_DEMO_TRADING:-${CTENGINE__EXCHANGE__DEMO_TRADING:-false}}")
export BYBIT_DEMO_TRADING
export CTENGINE__EXCHANGE__DEMO_TRADING="${BYBIT_DEMO_TRADING}"
export CTENGINE__TELEGRAM__TOKEN="${TELEGRAM_BOT_TOKEN:-$CTENGINE__TELEGRAM__TOKEN}"
export CTENGINE__TELEGRAM__CHAT_ID="${TRADE_OWNER_USER_ID:-$CTENGINE__TELEGRAM__CHAT_ID}"

FREQUI_USERNAME=$(strip_crlf "${FREQUI_USERNAME:-cryptotools}")
FREQUI_PASSWORD=$(strip_crlf "${FREQUI_PASSWORD:-}")
FREQUI_JWT_SECRET=$(strip_crlf "${FREQUI_JWT_SECRET:-}")
export CTENGINE__API_SERVER__USERNAME="${FREQUI_USERNAME}"
export CTENGINE__API_SERVER__PASSWORD="${FREQUI_PASSWORD:-$CTENGINE__API_SERVER__PASSWORD}"
export CTENGINE__API_SERVER__JWT_SECRET_KEY="${FREQUI_JWT_SECRET:-$CTENGINE__API_SERVER__JWT_SECRET_KEY}"

# Telegram proxy (SOCKS5/HTTP). Used only by Telegram RPC, not exchange API.
if [[ -n "${TELEGRAM_PROXY:-}" ]]; then
  export TELEGRAM_PROXY
fi

echo "Loaded CryptoTools secrets from $ENV_FILE"
