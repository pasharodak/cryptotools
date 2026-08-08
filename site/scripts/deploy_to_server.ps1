# Deploy CryptoTools site to VPS
# Usage: .\scripts\deploy_to_server.ps1 -SshUser root
param(
    [string]$ServerIp = "77.222.35.209",
    [string]$SshUser = "root",
    [string]$KeyPath = "D:\cryptotools\site\deploy\id_rsa\id_rsa"
)

$ErrorActionPreference = "Stop"
$ssh = if (Test-Path "C:\Program Files\Git\usr\bin\ssh.exe") { "C:\Program Files\Git\usr\bin\ssh.exe" } else { "C:\Windows\System32\OpenSSH\ssh.exe" }
$scp = $ssh -replace "ssh.exe", "scp.exe"
$archive = "C:\Temp\cryptotools-userdata.tgz"
$envFile = "D:\cryptotools\site\.env"

Write-Host "Stopping local bots..."
Get-CimInstance Win32_Process -Filter "Name='ctbot.exe'" -EA SilentlyContinue | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -EA SilentlyContinue | Where-Object { $_.CommandLine -match 'ctengine|criptotools\\main\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }

Write-Host "Packing user_data + scripts..."
Push-Location D:\cryptotools
tar -czf $archive user_data scripts deploy ctengine/rpc/telegram.py README.md SERVER_SETUP.md
Pop-Location

Write-Host "SSH test: ${SshUser}@${ServerIp}"
& $ssh -i $KeyPath -o StrictHostKeyChecking=accept-new "${SshUser}@${ServerIp}" "whoami"

& $scp -i $KeyPath $archive "${SshUser}@${ServerIp}:/tmp/cryptotools-userdata.tgz"
& $scp -i $KeyPath $envFile "${SshUser}@${ServerIp}:/tmp/cryptotools.env"

$remoteScript = Join-Path $env:TEMP "ctengine-remote-setup.sh"
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
id -u cryptotools &>/dev/null || useradd -m -s /bin/bash ctengine
if [ ! -d /home/cryptotools/app/.git ]; then
  sudo -u cryptotools git clone --branch stable --depth 1 https://github.com/pasharodak/cryptotools.git /home/cryptotools/app
fi
tar -xzf /tmp/cryptotools-userdata.tgz -C /home/cryptotools/app
install -m 600 -o ctengine -g ctengine /tmp/cryptotools.env /home/cryptotools/.cryptotools.env
sed -i 's/\r$//' /home/cryptotools/.cryptotools.env
chown -R cryptotools:cryptotools /home/cryptotools/app/user_data /home/cryptotools/app/scripts /home/cryptotools/app/deploy
sudo -u cryptotools bash -lc 'cd ~/ctengine && python3 -m venv .venv && source .venv/bin/activate && pip install -U pip wheel && pip install -r requirements.txt && pip install -e "." && ctengine install-ui'
cp /home/cryptotools/app/deploy/cryptotools-finder.service /etc/systemd/system/cryptotools-finder.service
systemctl daemon-reload
systemctl enable cryptotools-finder
systemctl restart cryptotools-finder
sleep 10
systemctl is-active cryptotools-finder
journalctl -u cryptotools-finder -n 20 --no-pager
'@ | Set-Content -Path $remoteScript -Encoding UTF8

Get-Content $remoteScript | & $ssh -i $KeyPath "${SshUser}@${ServerIp}" "bash -s"
Write-Host "Deployed. Telegram: /status"

