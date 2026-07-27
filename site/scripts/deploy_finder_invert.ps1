# Deploy ML Finder invert_signal to VPS
param(
    [string]$ServerIp = "77.222.35.209",
    [string]$SshUser = "root",
    [string]$KeyPath = "D:\cryptotools\site\deploy\id_rsa\id_rsa"
)

$ErrorActionPreference = "Stop"
$ssh = if (Test-Path "C:\Program Files\Git\usr\bin\ssh.exe") { "C:\Program Files\Git\usr\bin\ssh.exe" } else { "ssh" }
$scp = $ssh -replace "ssh.exe", "scp.exe"
$root = "D:\cryptotools\site"
$remoteHost = $SshUser + '@' + $ServerIp
$remote = "/home/freqtrade/freqtrade/user_data"

Write-Host "=== Upload Finder invert_signal ==="
& $scp -i $KeyPath `
    "$root\user_data\trade_finder.json" `
    "$root\user_data\ml\finder_live.py" `
    "$root\user_data\strategies\TradeFinderStrategy.py" `
    ($remoteHost + ':' + $remote + '/')

& $scp -i $KeyPath "$root\user_data\ml\finder_live.py" ($remoteHost + ':' + $remote + '/ml/')
& $scp -i $KeyPath "$root\user_data\strategies\TradeFinderStrategy.py" ($remoteHost + ':' + $remote + '/strategies/')

Write-Host "=== Fix ownership (no restart, start bot manually) ==="
$cmd = 'chown -R freqtrade:freqtrade /home/freqtrade/freqtrade/user_data/trade_finder.json /home/freqtrade/freqtrade/user_data/ml/finder_live.py /home/freqtrade/freqtrade/user_data/strategies/TradeFinderStrategy.py; grep invert_signal /home/freqtrade/freqtrade/user_data/trade_finder.json'
& $ssh -i $KeyPath $remoteHost $cmd
Write-Host "Done. Start Finder via UI or: systemctl restart freqtrade"

