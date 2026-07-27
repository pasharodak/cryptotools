@echo off
REM Start SOCKS proxy + SSH tunnel for Telegram (server bot -> PC internet)
start "pproxy" /min python -m pproxy -l socks5://127.0.0.1:7890
timeout /t 2 /nobreak >nul
start "tg-tunnel" /min "C:\Program Files\Git\usr\bin\ssh.exe" -i "D:\cryptotools\site\deploy\id_rsa\id_rsa" -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -N -R 127.0.0.1:1081:127.0.0.1:7890 root@77.222.35.209
echo Telegram tunnel started (PC must stay on).
