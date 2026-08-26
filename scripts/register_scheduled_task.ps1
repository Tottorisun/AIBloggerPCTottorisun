# Registers (or re-registers) the daily pc-price-tracker scrape as a
# Windows Scheduled Task: runs scripts/run_daily_scrape.py at 04:00 every
# day under the current user account. Safe to re-run — it replaces any
# existing task with the same name.
#
# -StartWhenAvailable: if the PC was off/asleep at 04:00, the task runs as
#   soon as it's next available instead of silently skipping the day.
# -WakeToRun: has the OS wake the machine from sleep for the scheduled
#   time itself (needs S3/S4 wake support enabled in BIOS/UEFI+Windows
#   power settings — this only sets the Task Scheduler side).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\register_scheduled_task.ps1
#
# To remove it later:
#   Unregister-ScheduledTask -TaskName "PCPriceTracker_DailyScrapeAll" -Confirm:$false

$ErrorActionPreference = "Stop"

$TaskName = "PCPriceTracker_DailyScrapeAll"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $PSScriptRoot "run_daily_scrape.py"
$PythonExe = (Get-Command python).Source

if (-not (Test-Path $ScriptPath)) {
    throw "Не найден $ScriptPath"
}

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$ScriptPath`"" -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At 04:00
# scrape-all takes ~20-30 min end to end; with up to 3 retries an hour
# apart, a failing run could take ~2h15m worst case. 6h ceiling leaves
# generous headroom without letting a stuck run linger indefinitely.
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -StartWhenAvailable `
    -WakeToRun `
    -DontStopOnIdleEnd

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Ежедневный scrape-all + backup для pc-price-tracker. Логи: $ProjectRoot\logs\scrape_all.log" `
    -Force | Out-Null

Write-Host "Задача '$TaskName' зарегистрирована: ежедневно в 04:00."
Write-Host "Команда: $PythonExe $ScriptPath"
Write-Host "Рабочая папка: $ProjectRoot"
Write-Host "Логи: $ProjectRoot\logs\scrape_all.log (ротация раз в сутки, храним 30 файлов)"

