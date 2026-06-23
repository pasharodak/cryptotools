param(
    [string]$EnvFile = "D:\criptotools\.env"
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
    $env:FREQTRADE__EXCHANGE__KEY = $env:BYBIT_API_KEY
}
if ($env:BYBIT_API_SECRET) {
    $env:FREQTRADE__EXCHANGE__SECRET = $env:BYBIT_API_SECRET
}
if ($env:TELEGRAM_BOT_TOKEN) {
    $env:FREQTRADE__TELEGRAM__TOKEN = $env:TELEGRAM_BOT_TOKEN
}
if ($env:TRADE_OWNER_USER_ID) {
    $env:FREQTRADE__TELEGRAM__CHAT_ID = $env:TRADE_OWNER_USER_ID
}

Write-Host "Loaded Freqtrade secrets from $EnvFile"
