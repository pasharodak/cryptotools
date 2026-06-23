# Setup CriptoTools UI in browser via HTTPS on VPS port 8443
param(
    [string]$ServerIp = "77.222.35.209",
    [string]$SshUser = "root",
    [string]$KeyPath = "D:\criptotools\id_rsa\id_rsa"
)

$ErrorActionPreference = "Stop"
$ssh = if (Test-Path "C:\Program Files\Git\usr\bin\ssh.exe") { "C:\Program Files\Git\usr\bin\ssh.exe" } else { "ssh" }
$scp = $ssh -replace "ssh.exe", "scp.exe"

Write-Host "Testing SSH to ${SshUser}@${ServerIp}..."
& $ssh -i $KeyPath -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new "${SshUser}@${ServerIp}" "echo SSH_OK"
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "SSH unavailable. Reboot VPS from hosting panel, then run this script again."
    Write-Host "Or open VNC console and run: wg-quick down wgcf"
    exit 1
}

Write-Host "Uploading nginx config, UI, and setup script..."
& $scp -i $KeyPath `
    "D:\freqtrade\deploy\nginx-freqtrade.conf" `
    "D:\freqtrade\deploy\setup-web-access.sh" `
    "D:\freqtrade\scripts\load_env.sh" `
    "${SshUser}@${ServerIp}:/tmp/"

& $ssh -i $KeyPath "${SshUser}@${ServerIp}" "mkdir -p /tmp/custom-ui"
& $scp -i $KeyPath `
    "D:\freqtrade\custom-ui\index.html" `
    "D:\freqtrade\custom-ui\app.js" `
    "D:\freqtrade\custom-ui\styles.css" `
    "${SshUser}@${ServerIp}:/tmp/custom-ui/"

& $ssh -i $KeyPath "${SshUser}@${ServerIp}" @"
sed -i 's/\r$//' /tmp/setup-web-access.sh /tmp/load_env.sh
cp /tmp/load_env.sh /home/freqtrade/freqtrade/scripts/load_env.sh 2>/dev/null || true
chmod +x /tmp/setup-web-access.sh
bash /tmp/setup-web-access.sh
"@

Write-Host ""
Write-Host "Open in browser: https://${ServerIp}:8443"
Write-Host "Login: see FREQUI_USERNAME in D:\criptotools\.env"
Write-Host "Password: see FREQUI_PASSWORD in D:\criptotools\.env"
Write-Host "(Browser will warn about self-signed certificate - accept for this IP)"
