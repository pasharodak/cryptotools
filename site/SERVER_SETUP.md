# Настройка VPS для Freqtrade + FreqAI

Тариф: **Cloud Extra** (4 CPU / 6 GB RAM / 60 GB) или аналог.  
ОС: **Ubuntu 26.04 LTS**.

---

## 1. Первый вход и базовая безопасность

С локального ПК (PowerShell):

```powershell
ssh root@ВАШ_IP
```

На сервере:

```bash
apt update && apt upgrade -y

# Отдельный пользователь (не root)
adduser freqtrade
usermod -aG sudo freqtrade

# SSH-ключ (скопируйте с ПК: ssh-copy-id freqtrade@ВАШ_IP)
# Затем отключите вход по паролю для root — по желанию

# Фаервол: только SSH
ufw allow OpenSSH
ufw enable
```

---

## 2. Время (обязательно для Bybit)

```bash
timedatectl set-timezone UTC
timedatectl set-ntp true
timedatectl status
```

Если Bybit снова отдаёт `10002` — проверьте `System clock synchronized: yes`.

---

## 3. Swap (рекомендуется на 6 GB)

```bash
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

---

## 4. Зависимости

```bash
sudo apt install -y git python3 python3-venv python3-dev build-essential \
  libffi-dev libssl-dev curl
```

---

## 5. Перенос проекта с Windows

**Вариант A — архив с ПК** (PowerShell на `D:\cryptotools\site`):

```powershell
# Не копируйте .venv с Windows — на Linux создадим заново
tar -czf freqtrade-deploy.tgz --exclude=.venv --exclude=user_data/data `
  user_data scripts freqtrade deploy README.md SERVER_SETUP.md pyproject.toml requirements.txt setup.sh
scp freqtrade-deploy.tgz freqtrade@ВАШ_IP:~/
```

На сервере:

```bash
sudo -u freqtrade -i
cd ~
mkdir -p freqtrade && cd freqtrade
tar -xzf ../freqtrade-deploy.tgz -C .
```

**Вариант B — клонировать Freqtrade и скопировать только `user_data`:**

```bash
sudo -u freqtrade -i
git clone --branch stable https://github.com/freqtrade/freqtrade.git
cd freqtrade
# Скопируйте user_data/, scripts/, правки telegram.py с ПК
```

---

## 6. Секреты (не в git!)

```bash
nano ~/.freqtrade.env
chmod 600 ~/.freqtrade.env
```

Содержимое:

```env
BYBIT_API_KEY=ваш_ключ
BYBIT_API_SECRET=ваш_секрет
BYBIT_TESTNET=false
TELEGRAM_BOT_TOKEN=ваш_токен
TRADE_OWNER_USER_ID=ваш_telegram_id
```

Права API Bybit: **Contract Orders**, **Contract Positions**. **Без вывода** средств.

---

## 7. Установка Freqtrade + FreqAI

```bash
cd ~/freqtrade
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt
pip install -e ".[freqai]"
freqtrade install-ui
```

Проверка:

```bash
source scripts/load_env.sh ~/.freqtrade.env
freqtrade --version
freqtrade list-freqaimodels
```

---

## 8. Конфиг под сервер

В `user_data/config.json`:

- `"dry_run": true` — **сначала тест** 1–2 дня
- `"dry_run": false` — live
- Web UI только локально:

```json
"api_server": {
    "enabled": true,
    "listen_ip_address": "127.0.0.1",
    "listen_port": 8080
}
```

Не открывайте порт 8080 в ufw наружу.

---

## 9. Исторические данные

```bash
source scripts/load_env.sh ~/.freqtrade.env
freqtrade download-data \
  --config user_data/config.json \
  --timeframe 5m 15m 1h 4h \
  --days 45
```

---

## 10. Пробный запуск (вручную)

```bash
source .venv/bin/activate
source scripts/load_env.sh ~/.freqtrade.env

# Dry-run
# поменяйте dry_run: true в config
freqtrade trade \
  --config user_data/config.json \
  --strategy FreqaiExampleStrategy \
  --freqaimodel LightGBMRegressor \
  --logfile user_data/logs/freqtrade-live.log
```

Ctrl+C для остановки. В Telegram: `/status`, `/balance`.

**Важно:** на сервере должен работать **только один** бот с этим Telegram-токеном. Остановите бота на Windows.

---

## 11. Автозапуск (systemd)

```bash
sudo cp deploy/freqtrade.service /etc/systemd/system/freqtrade.service
# Поправьте пути User/WorkingDirectory при необходимости
sudo systemctl daemon-reload
sudo systemctl enable freqtrade
sudo systemctl start freqtrade
sudo systemctl status freqtrade
```

Логи:

```bash
journalctl -u freqtrade -f
tail -f ~/freqtrade/user_data/logs/freqtrade-live.log
```

Перезапуск:

```bash
sudo systemctl restart freqtrade
```

---

## 12. Web UI с вашего ПК (без открытия порта)

На **локальном** ПК:

```powershell
ssh -L 8080:127.0.0.1:8080 freqtrade@ВАШ_IP
```

Браузер: http://127.0.0.1:8080 (логин `freqtrader` / `freqtrader`).

---

## 13. Чеклист перед live

- [ ] `dry_run: true` протестирован минимум несколько часов
- [ ] Бот на Windows **остановлен** (конфликт Telegram)
- [ ] Время синхронизировано (NTP)
- [ ] Баланс Bybit ≥ 10 USDT (ставка 5 USDT + запас)
- [ ] `systemctl status freqtrade` — active (running)
- [ ] В логе: `Done training` для всех пар, `heartbeat RUNNING`

---

## 14. Обслуживание

| Задача | Команда |
|--------|---------|
| Обновить Freqtrade | `git pull && pip install -e ".[freqai]" && sudo systemctl restart freqtrade` |
| Место на диске | `du -sh user_data/data user_data/models user_data/logs` |
| Очистить старые модели | настроено `purge_old_models: 3` в config |
| Сменить стратегию | правка config + `sudo systemctl restart freqtrade` |

---

## Типичные проблемы

| Симптом | Решение |
|---------|---------|
| Bybit `10002` | `timedatectl set-ntp true`, в config уже есть `recvWindow: 60000` |
| Telegram Conflict | один процесс с токеном; `systemctl stop` на старом сервере/ПК |
| OOM / убит процесс | swap 2G, уменьшить пары или `train_period_days` |
| Нет сделок | нормально — ждёт сигнал FreqAI; `/status` |
