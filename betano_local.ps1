# Betano's basketball points lines, collected from this PC.
#
# Betano refuses the posession server (HTTP 403 on every request since at
# least 6 Nov 2026), so the lines are fetched here, as the tennis and CS2
# Betano lines are: fresh DraftKings lines from ESPN, Betano's lines, price,
# write the tips, tell Telegram about new and changed ones. Results and
# settlement also run on the server (run_bpoints.sh in the posession repo).
#
#   powershell -ExecutionPolicy Bypass -File C:\Users\kenny\bpoints\betano_local.ps1
#
# Telegram credentials are read from the posession scraper's .env.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$env:PYTHONUTF8 = "1"
$env:PYTHONWARNINGS = "ignore"

foreach ($line in Get-Content "C:\Users\kenny\posession\scraper\.env") {
    if ($line -match '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)=(.*)$') {
        Set-Item -Path "env:$($Matches[1])" -Value $Matches[2].Trim('"')
    }
}

& $py collect.py
& $py odds.py
if ($LASTEXITCODE -ne 0) { Write-Host "!! odds.py exited $LASTEXITCODE - stopping"; exit 1 }
& $py sazka_odds.py
if ($LASTEXITCODE -ne 0) { Write-Host "!! sazka_odds.py exited $LASTEXITCODE - going on without SazkaBet" }
& $py price.py --store --top 0
& $py advice.py
& $py notify.py
