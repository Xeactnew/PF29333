# PipsFarmer quick update. Run it with UPDATE.bat from the NEW extracted zip.
# Keeps your settings (.env), database, node.json and customer MT5 folders.
# Open trades stay open at the broker with their SL/TP; workers pick them back up on restart.

param([switch]$Unattended)   # -Unattended: started from Admin -> System, nobody watching

$ErrorActionPreference = "Continue"
$Dest  = "C:\PipsFarmer"
if ($Unattended) {
    New-Item -ItemType Directory -Force -Path "$Dest\updates" | Out-Null
    Start-Transcript -Path "$Dest\updates\last-update.log" -Force | Out-Null
    Write-Host "Update started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') from $PSScriptRoot"
}
function Pause-IfWatching { if (-not $Unattended) { Read-Host "Press Enter to close" } else { Stop-Transcript | Out-Null } }
$PyExe = "C:\Program Files\Python312\python.exe"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host "Please run UPDATE.bat (it asks for administrator rights)." -ForegroundColor Red; exit 1 }
if (-not (Test-Path "$Dest\.env")) {
    Write-Host "PipsFarmer isn't installed yet. Run SETUP.bat instead." -ForegroundColor Red
    Pause-IfWatching; exit 1
}
if ((Resolve-Path $PSScriptRoot).Path.TrimEnd("\") -eq $Dest) {
    Write-Host "Run UPDATE.bat from the new extracted zip, not from C:\PipsFarmer." -ForegroundColor Red
    Pause-IfWatching; exit 1
}

Write-Host "Updating PipsFarmer..." -ForegroundColor Cyan

Write-Host "==> Stopping PipsFarmer" -ForegroundColor Yellow
Stop-ScheduledTask -TaskName "PipsFarmer Website" -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName "PipsFarmer MT5 Node" -ErrorAction SilentlyContinue
# stop the website, node agent and workers (python processes started from C:\PipsFarmer)
# the MT5 manager's start loop first, so it doesn't start the old copy again mid-update
Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" | Where-Object { $_.CommandLine -like "*start_node.bat*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*PipsFarmer*" -or $_.CommandLine -like "*run_server.py*" -or $_.CommandLine -like "*node_agent.py*" -or $_.CommandLine -like "*worker.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2

Write-Host "==> Backing up the database" -ForegroundColor Yellow
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
New-Item -ItemType Directory -Force -Path "$Dest\backups" | Out-Null
if (Test-Path "$Dest\data\pipsfarmer.db") { Copy-Item "$Dest\data\pipsfarmer.db" "$Dest\backups\pipsfarmer-$stamp.db" }
Get-ChildItem "$Dest\backups\*.db" | Sort-Object LastWriteTime -Descending | Select-Object -Skip 10 | Remove-Item -ErrorAction SilentlyContinue

Write-Host "==> Copying new files" -ForegroundColor Yellow
robocopy $PSScriptRoot $Dest /E /XF .env node.json start_server.bat start_node.bat BACKUP-your-keys.env.txt `
    /XD data accounts backups updates logs bin signals mt5-base mt5-base-building __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null

# Windows updates: install, but only restart at the weekend (PipsFarmer restarts the VPS on Saturday)
New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" -Force | Out-Null
Set-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" NoAutoRebootWithLoggedOnUsers 1 -Type DWord

Write-Host "==> Prompt-free updates" -ForegroundColor Yellow
& "C:\PipsFarmer\tools\register_update_helper.ps1"

Write-Host "==> Keeping the desktop live after you disconnect" -ForegroundColor Yellow
& "C:\PipsFarmer\tools\register_keep_screen_on.ps1"

Write-Host "==> Restart protection" -ForegroundColor Yellow
& "C:\PipsFarmer\tools\register_watchdog.ps1"
# start_node.bat isn't copied over (it holds this VPS's Python path), so bring it up to date here:
# it now starts the MT5 manager with no window and stops a second copy.
$nb = "$Dest\node\start_node.bat"
$pyLine = "python"
if (Test-Path $nb) {
    $found = Get-Content $nb | Where-Object { $_ -like "*node_agent.py*" } | Select-Object -First 1
    if ($found) { $pyLine = ($found -replace "node_agent\.py.*$", "").Trim() }
}
Set-Content $nb -Encoding ASCII -Value @(
    "@echo off", "cd /d `"%~dp0`"",
    'if not "%PF_NODE_HIDDEN%"=="1" if exist "%~dp0start_node_hidden.vbs" (',
    "  set PF_NODE_HIDDEN=1", '  wscript.exe "%~dp0start_node_hidden.vbs"', "  exit /b 0", ")",
    ":loop", "$pyLine node_agent.py", "if %errorlevel%==3 goto :eof",
    "timeout /t 10 /nobreak >nul", "goto loop")
Write-Host "    the MT5 manager runs without a window"

Write-Host "==> Updating Python packages" -ForegroundColor Yellow
& $PyExe -m pip install -r "$Dest\requirements-server.txt" -r "$Dest\node\requirements-node.txt" --quiet --disable-pip-version-check

Write-Host "==> Starting PipsFarmer" -ForegroundColor Yellow
Start-ScheduledTask -TaskName "PipsFarmer Website"
Start-Sleep 8
Start-ScheduledTask -TaskName "PipsFarmer MT5 Node"
try {
    Invoke-WebRequest "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 30 -ErrorAction Stop | Out-Null
    Write-Host "Done. PipsFarmer is updated and running." -ForegroundColor Green
} catch {
    Write-Host "Updated, but the website hasn't answered yet. Check http://localhost:8000 in a minute." -ForegroundColor Magenta
    Write-Host "If it still doesn't load, send Claude a screenshot. Your previous database is in C:\PipsFarmer\backups." -ForegroundColor Magenta
}
Pause-IfWatching
