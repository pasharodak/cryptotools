# Build release tarball and run VPS cutover to CryptoTools paths/units
param(
    [string]$ServerIp = "77.222.35.209",
    [string]$SshUser = "root",
    [string]$KeyPath = "D:\cryptotools\site\deploy\id_rsa\id_rsa"
)
$ErrorActionPreference = "Stop"
$ssh = if (Test-Path "C:\Program Files\Git\usr\bin\ssh.exe") { "C:\Program Files\Git\usr\bin\ssh.exe" } else { "ssh" }
$scp = $ssh -replace "ssh.exe", "scp.exe"
$Site = "D:\cryptotools\site"
$archive = "C:\Temp\cryptotools-app.tgz"

Write-Host "=== Pack site release ==="
if (Test-Path $archive) { Remove-Item $archive -Force }
Push-Location $Site
# exclude heavy/local junk
& tar --exclude=.venv --exclude=__pycache__ --exclude=user_data/data --exclude=user_data/logs --exclude=user_data/plot --exclude=user_data/notebooks --exclude="*.pyc" --exclude=.git -czf $archive `
  ctengine custom-ui deploy scripts user_data requirements.txt pyproject.toml setup.py setup.cfg README.md SERVER_SETUP.md 2>$null
if (-not (Test-Path $archive)) {
  # fallback without long excludes if tar differs
  & tar -czf $archive ctengine custom-ui deploy scripts user_data/config.json user_data/config_strategy.json user_data/config_grid.json user_data/strategies user_data/ml user_data/models user_data/*.json requirements.txt pyproject.toml
}
Pop-Location
Write-Host "archive bytes=" (Get-Item $archive).Length

Write-Host "=== Upload ==="
& $ssh -i $KeyPath "${SshUser}@${ServerIp}" "mkdir -p /tmp/cryptotools_release"
& $scp -i $KeyPath $archive "${SshUser}@${ServerIp}:/tmp/cryptotools_release/app.tgz"
& $scp -i $KeyPath "$Site\scripts\cutover_vps_rebrand.sh" "${SshUser}@${ServerIp}:/tmp/cryptotools_release/cutover.sh"
# also ensure env can be copied if needed
if (Test-Path "$Site\.env") {
  & $scp -i $KeyPath "$Site\.env" "${SshUser}@${ServerIp}:/tmp/cryptotools_release/cryptotools.env"
}

Write-Host "=== Run cutover ==="
& $ssh -i $KeyPath "${SshUser}@${ServerIp}" "sed -i 's/\r$//' /tmp/cryptotools_release/cutover.sh; chmod +x /tmp/cryptotools_release/cutover.sh; bash /tmp/cryptotools_release/cutover.sh"
Write-Host "=== Done ==="
