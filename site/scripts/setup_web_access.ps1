# Setup CriptoTools UI in browser via HTTPS on VPS port 8443
param(
    [string]$ServerIp = "77.222.35.209",
    [string]$SshUser = "root",
    [string]$KeyPath = "D:\cryptotools\site\deploy\id_rsa\id_rsa"
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
    "D:\cryptotools\site\deploy\nginx-cryptotools.conf" `
    "D:\cryptotools\site\deploy\setup-web-access.sh" `
    "D:\cryptotools\site\scripts\load_env.sh" `
    "${SshUser}@${ServerIp}:/tmp/"

& $ssh -i $KeyPath "${SshUser}@${ServerIp}" "mkdir -p /tmp/custom-ui"
& $scp -i $KeyPath `
    "D:\cryptotools\site\custom-ui\index.html" `
    "D:\cryptotools\site\custom-ui\app.js" `
    "D:\cryptotools\site\custom-ui\styles.css" `
    "${SshUser}@${ServerIp}:/tmp/custom-ui/"

& $ssh -i $KeyPath "${SshUser}@${ServerIp}" @"
sed -i 's/\r$//' /tmp/setup-web-access.sh /tmp/load_env.sh
cp /tmp/load_env.sh /home/cryptotools/app/scripts/load_env.sh 2>/dev/null || true
chmod +x /tmp/setup-web-access.sh
bash /tmp/setup-web-access.sh
"@

Write-Host ""
Write-Host "Open in browser: https://${ServerIp}:8443"
Write-Host "Login: see FREQUI_USERNAME in D:\cryptotools\site\.env"
Write-Host "Password: see FREQUI_PASSWORD in D:\cryptotools\site\.env"
Write-Host "(Browser will warn about self-signed certificate - accept for this IP)"
