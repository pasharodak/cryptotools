# CryptoTools — Freqtrade dual-bot stack

Custom deployment for three Freqtrade bots (FreqAI, Strategy, Grid) with a unified web UI, pair scanners, and VPS automation.

## Components

| Path | Description |
|------|-------------|
| `custom-ui/` | Web UI (3 panels, scan controls, logs) |
| `deploy/` | VPS setup (`setup-dual-ui.sh`, systemd units) |
| `scripts/` | Pair config API, ranging/strategy scanners, deploy helpers |
| `user_data/strategies/` | Grid and multi-strategy implementations |
| `user_data/config*.json` | Bot configs (API keys empty — fill locally) |
| `user_data/*_scan_config.json` | Scanner settings |

## Quick start

1. Install [Freqtrade](https://www.freqtrade.io/) (stable) on the server.
2. Copy `user_data/` into your Freqtrade install.
3. Run `deploy/setup-dual-ui.sh` on the VPS (see `SERVER_SETUP.md`).
4. Fill exchange and API credentials in `config.json`, `config_strategy.json`, `config_grid.json`.

## Bots

- **FreqAI** — port 8080, `config.json`
- **Strategy** — port 8081, `config_strategy.json`, `MultiStrategyRouter`
- **Grid** — port 8082, `config_grid.json`, `VolatilityGridStrategy` (3× leverage, DCA limit, pair cooldown)

## Scanners

- `scripts/scan_strategy_pairs.py` — top 150 volume pairs per enabled strategy
- `scripts/scan_ranging_pairs.py` — sideways-market filter for Grid whitelist
