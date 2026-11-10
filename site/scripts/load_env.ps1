param(
    [string]$EnvFile = "D:\cryptotools\site\.env"
)

if (-not (Test-Path $EnvFile)) {
    Write-Warning "Env file not found: $EnvFile"
    return
}

Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }
    $name = $line.Substring(0, $idx).Trim()
    $value = $line.Substring($idx + 1).Trim()
    Set-Item -Path "Env:$name" -Value $value
}

if ($env:BYBIT_API_KEY) {
    $env:CTENGINE__EXCHANGE__KEY = $env:BYBIT_API_KEY
}
if ($env:BYBIT_API_SECRET) {
    $env:CTENGINE__EXCHANGE__SECRET = $env:BYBIT_API_SECRET
}
# Demo Trading → api-demo.bybit.com (ctengine exchange.demo_trading)
if (-not $env:BYBIT_DEMO_TRADING -and $env:CTENGINE__EXCHANGE__DEMO_TRADING) {
    $env:BYBIT_DEMO_TRADING = $env:CTENGINE__EXCHANGE__DEMO_TRADING
}
if ($env:BYBIT_DEMO_TRADING) {
    $env:CTENGINE__EXCHANGE__DEMO_TRADING = $env:BYBIT_DEMO_TRADING
}
if (-not $env:FREQUI_USERNAME) { $env:FREQUI_USERNAME = "cryptotools" }
$env:CTENGINE__API_SERVER__USERNAME = $env:FREQUI_USERNAME
if ($env:FREQUI_PASSWORD) {
    $env:CTENGINE__API_SERVER__PASSWORD = $env:FREQUI_PASSWORD
}
if ($env:FREQUI_JWT_SECRET) {
    $env:CTENGINE__API_SERVER__JWT_SECRET_KEY = $env:FREQUI_JWT_SECRET
}
if ($env:TELEGRAM_BOT_TOKEN) {
    $env:CTENGINE__TELEGRAM__TOKEN = $env:TELEGRAM_BOT_TOKEN
}
if ($env:TRADE_OWNER_USER_ID) {
    $env:CTENGINE__TELEGRAM__CHAT_ID = $env:TRADE_OWNER_USER_ID
}

Write-Host "Loaded CryptoTools secrets from $EnvFile"

