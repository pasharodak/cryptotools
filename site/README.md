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
| nginx | конфиг на сервере (в git не входит) | 8443 |

Start/Stop: control-plane + `bot_reconcile` через серверные `deploy/admin-systemctl.sh` / `tenant-systemctl.sh` (в git их нет).

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

> Деплой-инструменты в репозиторий не входят (`site/deploy/*` и `deploy_*.ps1` удалены в `f74b321`).
> На сервере остаются установленные units/nginx/sudo-wrappers; деплой — приватным набором скриптов владельца.

SSH (VPN off): `ssh -i site/deploy/id_rsa/id_rsa root@194.163.149.122`.
