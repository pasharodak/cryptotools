# Deploy Freqtrade to VPS
# Usage: .\scripts\deploy_to_server.ps1 -SshUser root
param(
    [string]$ServerIp = "77.222.35.209",
    [string]$SshUser = "root",
    [string]$KeyPath = "D:\cryptotools\site\deploy\id_rsa\id_rsa"
)

$ErrorActionPreference = "Stop"
$ssh = if (Test-Path "C:\Program Files\Git\usr\bin\ssh.exe") { "C:\Program Files\Git\usr\bin\ssh.exe" } else { "C:\Windows\System32\OpenSSH\ssh.exe" }
$scp = $ssh -replace "ssh.exe", "scp.exe"
$archive = "C:\Temp\freqtrade-userdata.tgz"
$envFile = "D:\cryptotools\site\.env"

Write-Host "Stopping local bots..."
Get-CimInstance Win32_Process -Filter "Name='freqtrade.exe'" -EA SilentlyContinue | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -EA SilentlyContinue | Where-Object { $_.CommandLine -match 'freqtrade|criptotools\\main\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }

Write-Host "Packing user_data + scripts..."
Push-Location D:\cryptotools
tar -czf $archive user_data scripts deploy freqtrade/rpc/telegram.py README.md SERVER_SETUP.md
Pop-Location

Write-Host "SSH test: ${SshUser}@${ServerIp}"
& $ssh -i $KeyPath -o StrictHostKeyChecking=accept-new "${SshUser}@${ServerIp}" "whoami"

& $scp -i $KeyPath $archive "${SshUser}@${ServerIp}:/tmp/freqtrade-userdata.tgz"
& $scp -i $KeyPath $envFile "${SshUser}@${ServerIp}:/tmp/freqtrade.env"

$remoteScript = Join-Path $env:TEMP "freqtrade-remote-setup.sh"
@'
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git python3 python3-venv python3-dev build-essential libffi-dev libssl-dev curl
timedatectl set-timezone UTC || true
timedatectl set-ntp true || true
if ! swapon --show | grep -q swapfile; then
  fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
id -u freqtrade &>/dev/null || useradd -m -s /bin/bash freqtrade
if [ ! -d /home/freqtrade/freqtrade/.git ]; then
  sudo -u freqtrade git clone --branch stable --depth 1 https://github.com/freqtrade/freqtrade.git /home/freqtrade/freqtrade
fi
tar -xzf /tmp/freqtrade-userdata.tgz -C /home/freqtrade/freqtrade
install -m 600 -o freqtrade -g freqtrade /tmp/freqtrade.env /home/freqtrade/.freqtrade.env
sed -i 's/\r$//' /home/freqtrade/.freqtrade.env
chown -R freqtrade:freqtrade /home/freqtrade/freqtrade/user_data /home/freqtrade/freqtrade/scripts /home/freqtrade/freqtrade/deploy
sudo -u freqtrade bash -lc 'cd ~/freqtrade && python3 -m venv .venv && source .venv/bin/activate && pip install -U pip wheel && pip install -r requirements.txt && pip install -e "." && freqtrade install-ui'
cp /home/freqtrade/freqtrade/deploy/freqtrade.service /etc/systemd/system/freqtrade.service
systemctl daemon-reload
systemctl enable freqtrade
systemctl restart freqtrade
sleep 10
systemctl is-active freqtrade
journalctl -u freqtrade -n 20 --no-pager
'@ | Set-Content -Path $remoteScript -Encoding UTF8

Get-Content $remoteScript | & $ssh -i $KeyPath "${SshUser}@${ServerIp}" "bash -s"
Write-Host "Deployed. Telegram: /status"

