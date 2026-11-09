<#
.SYNOPSIS
    Creates (or replaces) the "Jarvis Daily Scan" scheduled task (Step 59).

.DESCRIPTION
    Jarvis is not always-on, so the daily scan runs as a Windows
    Task Scheduler task: once a day at 05:30 SGT — after the US
    close (16:00 Eastern is 05:00 SGT in winter, 04:00 SGT in
    summer) — the task starts Python, runs one live scan of the
    38-ticker watchlist, stores the run under data\scans\, diffs
    it against the previous run, raises Step 38 alerts for any
    new strongest-alert bands, and exits. No Jarvis window stays
    open, no trade is ever placed, and a new band is a name worth
    a closer look — never a recommendation.

    Idempotent: re-running this script replaces the task, never
    duplicates it. Two scans can never run at once — the scan
    itself holds a lock file (data\scans\.scan.lock) and a second
    run refuses to start.

    HOW TO RUN IT (on the owner's Windows PC, in D:\AiAssistant):
        1. Right-click PowerShell and choose "Run as administrator".
        2. cd D:\AiAssistant
           (if PowerShell refuses to run scripts, first run:
            Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass)
        3. .\scripts\setup_daily_scan.ps1
        4. Run it once by hand to check it works:
           Start-ScheduledTask -TaskName "Jarvis Daily Scan"
           then open data\scans\daily-scan.log and the dashboard.

    THREE THINGS TO KNOW BEFORE THE FIRST RUN:
        * The PC must be ON or ASLEEP (not fully shut down) at
          05:30, with your Windows account logged in. The task
          is set to wake the PC from sleep, and to start as soon
          as possible afterwards if the time was missed — but it
          cannot wake a PC that is fully shut down or logged out.
          A missed run is recorded as missed, never backfilled.
        * The FIRST run needs you present: the scan fetches live
          data from Yahoo and the first fetch raises approval
          cards (the Step 54 run stalled ~15 minutes on them).
          Tap "always allow" on the Yahoo cards once, and later
          scheduled runs proceed unattended. A scheduled run that
          hits an unapproved card fails loudly in its log instead
          of pretending the scan happened.
        * Results live on your PC: the run file
          data\scans\YYYY-MM-DD.json, the log
          data\scans\daily-scan.log, and the /dashboard panel
          ("Scheduled daily scan"). There is no channel that
          pushes the results to Muse's chat — new candidates
          surface on your dashboard and in Muse's own daily
          check, not as a push message from Jarvis.

.PARAMETER Repo
    The Jarvis project folder. Defaults to the parent of this
    script's folder (D:\AiAssistant when run from there).

.PARAMETER TaskName
    The scheduled-task name. Defaults to "Jarvis Daily Scan".

.PARAMETER Time
    The daily run time, local (Singapore) time. Defaults to
    "05:30" — after the US close year-round.
#>
[CmdletBinding()]
param(
    [string]$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$TaskName = "Jarvis Daily Scan",
    [string]$Time = "05:30"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Repo)) {
    Write-Error "Project folder not found: $Repo"
    exit 1
}
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error @"
Python not found at $Python.
Create the virtual environment first (in $Repo):
    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt
Then re-run this script.
"@
    exit 1
}
if (-not (Test-Path (Join-Path $Repo "agents\scheduled_scan.py"))) {
    Write-Error "agents\scheduled_scan.py not found in $Repo — pull the latest code (git pull) and re-run this script."
    exit 1
}

$ScansDir = Join-Path $Repo "data\scans"
if (-not (Test-Path $ScansDir)) {
    New-Item -ItemType Directory -Path $ScansDir | Out-Null
}
$LogFile = Join-Path $ScansDir "daily-scan.log"

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "-m agents.scheduled_scan run --log-file `"$LogFile`"" `
    -WorkingDirectory $Repo

$Trigger = New-ScheduledTaskTrigger -Daily -At $Time

$Settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Description "Jarvis Step 59: daily live scan of the 38-ticker watchlist after the US close. Runs one scan, stores it under data\scans\, diffs it against the previous run, raises alerts for new strongest-alert bands, then exits. No trade is ever placed." `
        -Force | Out-Null
}
catch {
    Write-Error @"
Could not create the scheduled task: $($_.Exception.Message)
Run PowerShell as administrator (right-click PowerShell > Run as administrator) and try again.
"@
    exit 1
}

Write-Host "Task '$TaskName' is set: daily at $Time (Singapore time),"
Write-Host "  running: $Python -m agents.scheduled_scan run"
Write-Host "  in:      $Repo"
Write-Host "  log:     $LogFile"
Write-Host ""
Write-Host "To check it works, run it once by hand:"
Write-Host "  Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host "then open $LogFile (the scan takes about 20 minutes)."
Write-Host ""
Write-Host "Remember: the PC must be on or asleep at $Time, and the"
Write-Host "first run needs you present for the Yahoo approval cards."
Get-ScheduledTask -TaskName $TaskName |
    Format-List TaskName, State, NextRunTime, LastRunTime, LastTaskResult
