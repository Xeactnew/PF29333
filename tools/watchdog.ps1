# PipsFarmer watchdog: runs every 5 minutes as SYSTEM (and 2 minutes after the VPS starts).
# Brings back anything that stopped: the website, the MT5 manager and the Cloudflare tunnel.
$ErrorActionPreference = "SilentlyContinue"
$Dest = "C:\PipsFarmer"
$log = "$Dest\logs\watchdog.log"
New-Item -ItemType Directory -Force -Path "$Dest\logs" | Out-Null
if ((Get-Item $log).Length -gt 1MB) { Remove-Item $log -Force }
function Log($m) { Add-Content $log ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) }
$up = ((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalMinutes
$py = Get-CimInstance Win32_Process -Filter "Name='python.exe'"

# 1. website (answers on http://127.0.0.1:8000/health) - only on the VPS that runs it
$hasSite = [bool](Get-ScheduledTask -TaskName "PipsFarmer Website")
$ok = -not $hasSite
for ($i = 0; $i -lt 3 -and -not $ok; $i++) {
    try { Invoke-WebRequest "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 20 | Out-Null; $ok = $true }
    catch { Start-Sleep 10 }
}
if (-not $ok -and $up -gt 4) {
    Log "website not answering: restarting it"
    $py | Where-Object { $_.CommandLine -like "*run_server.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep 3
    if ((Get-ScheduledTask -TaskName "PipsFarmer Website").State -ne "Running") { Start-ScheduledTask -TaskName "PipsFarmer Website" }
}

# 2. MT5 manager (it needs the logged-in desktop, so only when someone is logged in)
$loggedIn = [bool](Get-Process explorer)
$node = $py | Where-Object { $_.CommandLine -like "*node_agent.py*" }
if (-not $node) {
    if ($loggedIn) { Log "MT5 manager not running: starting it"; Start-ScheduledTask -TaskName "PipsFarmer MT5 Node" }
    elseif ($up -gt 4) { Log "MT5 manager can't start: nobody is logged in (automatic login is off?)" }
}

# 3. Cloudflare tunnel (how customers reach the site)
$svc = Get-Service cloudflared
if ($svc -and $svc.Status -ne "Running") { Log "tunnel stopped: starting it"; Start-Service cloudflared }
