# CryptoTools — site (прод Contabo)

Live Bybit USDT Perp на **Contabo `194.163.149.122`**: shared Strategy + Grid + ML Finder, **custom-ui**, **pair-config**, ML gate, multi-user Live/Demo.

- UI: https://194.163.149.122:8443/
- App на VPS: `/home/cryptotools/app`
- Static UI: `/var/www/criptotools-ui/`
- Обзор и правила: [`../README.md`](../README.md) · агент: [`../AGENTS.md`](../AGENTS.md) · сервер: [`SERVER_SETUP.md`](SERVER_SETUP.md)

Симуляции: `../simulation/`. Старый VPS `77.222.35.209` не использовать.

## Сервисы

| Unit | Config / код | Порт |
|------|--------------|------|
| `cryptotools-signal-engine` | `config_signal_engine.json` | 8081 |
| `trade-executor` | `scripts/trade_executor.py` | — |
| `cryptotools-grid` | `config_grid.json` | 8082 |
| `cryptotools-finder` | `config.json` | 8080 |
| `pair-config` | `scripts/pair_config_server.py` | 8090 |
| nginx | `deploy/nginx-cryptotools.conf` | 8443 |

Start/Stop: control-plane + `bot_reconcile` через `deploy/admin-systemctl.sh` / `tenant-systemctl.sh`.

## Локально

```powershell
cd D:\cryptotools\site
.\scripts\load_env.ps1
.\scripts\start_local_site.ps1
```

## Важные скрипты

| Скрипт | Назначение |
|--------|------------|
| `bot_reconcile.py` | desired → process (systemd wrappers) |
| `control_plane.py` | desired/observed + user_settings |
| `pair_entry_guard.py` | first-wins по паре |
| `reconcile_positions.py` | призраки / дубли vs Bybit |
| `signal_bus.py` | очередь сигналов → executor |

## Деплой Contabo

```powershell
cd D:\cryptotools\site\scripts
.\deploy_prod_ml.ps1

# VPN OFF — точечные фиксы:
cd D:\cryptotools\site\deploy
.\fix_finder_start_auth.bat
```
