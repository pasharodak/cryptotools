#!/usr/bin/env bash
# Load secrets into Freqtrade environment variables.
# Usage: source scripts/load_env.sh [/path/to/.env]

ENV_FILE="${1:-$HOME/.freqtrade.env}"

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
TRADE_OWNER_USER_ID=$(strip_crlf "${TRADE_OWNER_USER_ID:-}")

export FREQTRADE__EXCHANGE__KEY="${BYBIT_API_KEY:-$FREQTRADE__EXCHANGE__KEY}"
export FREQTRADE__EXCHANGE__SECRET="${BYBIT_API_SECRET:-$FREQTRADE__EXCHANGE__SECRET}"
export FREQTRADE__TELEGRAM__TOKEN="${TELEGRAM_BOT_TOKEN:-$FREQTRADE__TELEGRAM__TOKEN}"
export FREQTRADE__TELEGRAM__CHAT_ID="${TRADE_OWNER_USER_ID:-$FREQTRADE__TELEGRAM__CHAT_ID}"

FREQUI_USERNAME=$(strip_crlf "${FREQUI_USERNAME:-freqtrader}")
FREQUI_PASSWORD=$(strip_crlf "${FREQUI_PASSWORD:-}")
FREQUI_JWT_SECRET=$(strip_crlf "${FREQUI_JWT_SECRET:-}")
export FREQTRADE__API_SERVER__USERNAME="${FREQUI_USERNAME}"
export FREQTRADE__API_SERVER__PASSWORD="${FREQUI_PASSWORD:-$FREQTRADE__API_SERVER__PASSWORD}"
export FREQTRADE__API_SERVER__JWT_SECRET_KEY="${FREQUI_JWT_SECRET:-$FREQTRADE__API_SERVER__JWT_SECRET_KEY}"

# Telegram proxy (SOCKS5/HTTP). Used only by Telegram RPC, not exchange API.
if [[ -n "${TELEGRAM_PROXY:-}" ]]; then
  export TELEGRAM_PROXY
fi

echo "Loaded Freqtrade secrets from $ENV_FILE"
