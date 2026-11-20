# PipsFarmer: runs an update that UPDATE.bat asked for, with admin rights but no permission prompt.
# Started by the "PipsFarmer Update Helper" task (UPDATE.bat runs it with: schtasks /run).
$ErrorActionPreference = "Continue"
$req = "C:\Users\Public\PipsFarmer\update-from.txt"
if (-not (Test-Path $req)) { exit 0 }
$src = (Get-Content $req -Raw).Trim().TrimEnd("\")
Remove-Item $req -Force
if (-not (Test-Path "$src\update_vps.ps1") -or -not (Test-Path "$src\server\app.py")) { exit 1 }
# copy the update somewhere only admins can write before running it
$stage = "C:\PipsFarmer\updates\manual-" + (Get-Date -Format "yyyyMMdd-HHmmss")
New-Item -ItemType Directory -Force -Path $stage | Out-Null
robocopy $src $stage /E /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$stage\update_vps.ps1" -Unattended
