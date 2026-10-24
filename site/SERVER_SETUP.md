# Настройка VPS — CryptoTools (Contabo)

**Текущий прод:** Contabo **`194.163.149.122`**  
UI: [https://194.163.149.122:8443/](https://194.163.149.122:8443/)  
App: `/home/cryptotools/app` (= `site/`) · UI files: `/var/www/criptotools-ui/`  
User: `cryptotools` · secrets: `/home/cryptotools/.cryptotools.env`  
SSH: `site/deploy/id_rsa/id_rsa` as `root` (VPN off, если timeout).

Старый хост `77.222.35.209` не используется.

ОС: Ubuntu 24.04 LTS (ориентир). С Finder (PyTorch) лучше ≥8 GB RAM.

---

## Сервисы

| systemd | Роль | Порт |
|---------|------|------|
| `cryptotools-signal-engine` | MultiStrategyRouter (`CT_SIGNAL_ONLY`) | 8081 |
| `trade-executor` | Исполнение сигналов | — |
| `cryptotools-grid` | VolatilityGridStrategy | 8082 |
| `cryptotools-finder` | TradeFinderStrategy | 8080 |
| `pair-config` | UI API, control-plane, reconcile | 8090 |
| `cryptotools-telegram-bot` | Telegram | — |
| nginx | UI + proxy | 8443 |

Путь Strategy: **signal-engine → trade-executor**.

### Sudo wrappers (обязательно)

`cryptotools` не может вызывать plain `systemctl` (polkit → *Interactive authentication required*).

| Wrapper | Для чего |
|---------|----------|
| `deploy/admin-systemctl.sh` | admin units: finder, grid, signal-engine, trade-executor |
| `deploy/tenant-systemctl.sh` | `cryptotools-{grid,finder}@user` |
| `/etc/sudoers.d/cryptotools-tenants` | NOPASSWD на оба wrapper |

Файлы wrappers живут **на сервере** в `/home/cryptotools/app/deploy/` и в git не публикуются (удалены в `f74b321`).
Проверка: `sudo -n /home/cryptotools/app/deploy/admin-systemctl.sh status cryptotools-finder`; sudoers — `visudo -cf /etc/sudoers.d/cryptotools-tenants`.

---

## Деплой с ПК

> В репозитории нет деплой-скриптов: `site/deploy/*` и `deploy_*.ps1` удалены в `f74b321`.
> Используйте приватный деплой-набор владельца; на сервере остаются установленные units/nginx/sudo-wrappers.

SSH (VPN off): `ssh -i site/deploy/id_rsa/id_rsa root@194.163.149.122`.

Секреты: локально `site/.env` → на сервере `~/.cryptotools.env`.

```bash
systemctl restart pair-config cryptotools-signal-engine trade-executor cryptotools-grid
# Finder по необходимости:
systemctl restart cryptotools-finder
```

UI после правки `custom-ui`:

```bash
cp /tmp/custom-ui/* /var/www/criptotools-ui/
chown -R www-data:www-data /var/www/criptotools-ui
```

---

## Проверка

```bash
systemctl is-active cryptotools-signal-engine trade-executor cryptotools-grid cryptotools-finder pair-config
curl -sk https://127.0.0.1:8443/api/pair-config/state
curl -sk https://127.0.0.1:8443/api/strategy/ping
```

См. также корневой [`README.md`](../README.md) и [`AGENTS.md`](../AGENTS.md).
