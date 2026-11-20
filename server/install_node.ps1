# PipsFarmer: add this Windows VPS as an MT5 server.
# Made for you by your PipsFarmer website. Run it once in PowerShell (as Administrator).
# After this, the VPS runs by itself: it restarts customers' MT5 after reboots and updates
# itself whenever you update the website.

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ServerUrl = "__SERVER_URL__"
$Token     = "__NODE_TOKEN__"
$NodeName  = "__NODE_NAME__"
$Dest      = "C:\PipsFarmer"
$PyVersion = "3.12.8"
$PyExe     = "C:\Program Files\Python312\python.exe"
$Mt5Dir    = "C:\Program Files\MetaTrader 5"
$Tmp       = Join-Path $env:TEMP "pipsfarmer-node"
New-Item -ItemType Directory -Force -Path $Tmp, "$Dest\node", "$Dest\accounts" | Out-Null

function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Yellow }
function Ok($t)   { Write-Host "    $t" -ForegroundColor Green }
function Warn($t) { Write-Host "    $t" -ForegroundColor Magenta }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host "Open PowerShell with 'Run as administrator' and paste the command again." -ForegroundColor Red; return }

Write-Host "Adding this VPS to PipsFarmer as '$NodeName'" -ForegroundColor Cyan

Step "Python $PyVersion"
if (Test-Path $PyExe) { Ok "already installed" } else {
    Invoke-WebRequest "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-amd64.exe" -OutFile "$Tmp\python.exe" -UseBasicParsing
    Start-Process "$Tmp\python.exe" -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
    if (-not (Test-Path $PyExe)) { Write-Host "Python didn't install. Run the command again." -ForegroundColor Red; return }
    Ok "installed"
}

Step "MetaTrader 5"
if (Test-Path "$Mt5Dir\terminal64.exe") { Ok "already installed" } else {
    Invoke-WebRequest "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" -OutFile "$Tmp\mt5setup.exe" -UseBasicParsing
    Start-Process "$Tmp\mt5setup.exe" -ArgumentList "/auto" -Wait
    $w = 0; while (-not (Test-Path "$Mt5Dir\terminal64.exe") -and $w -lt 300) { Start-Sleep 5; $w += 5 }
    if (Test-Path "$Mt5Dir\terminal64.exe") { Ok "installed" } else { Warn "MT5 didn't finish installing. Run the command again." ; return }
    Get-Process terminal64 -ErrorAction SilentlyContinue | Stop-Process -Force
}

Step "PipsFarmer node"
Invoke-WebRequest "$ServerUrl/api/node/package" -Headers @{ Authorization = "Bearer $Token" } -OutFile "$Tmp\node.zip" -UseBasicParsing
Expand-Archive "$Tmp\node.zip" -DestinationPath "$Dest\node" -Force
& $PyExe -m pip install --upgrade pip --quiet --disable-pip-version-check
& $PyExe -m pip install -r "$Dest\node\requirements-node.txt" --quiet --disable-pip-version-check
[ordered]@{ server_url = $ServerUrl; node_token = $Token; mt5_template = $Mt5Dir; accounts_dir = "$Dest\accounts" } |
    ConvertTo-Json | Set-Content "$Dest\node\node.json" -Encoding ASCII
Set-Content "$Dest\node\start_node.bat" -Encoding ASCII -Value @(
    "@echo off", "cd /d $Dest\node",
    'if not "%PF_NODE_HIDDEN%"=="1" if exist "%~dp0start_node_hidden.vbs" (',
    "  set PF_NODE_HIDDEN=1", '  wscript.exe "%~dp0start_node_hidden.vbs"', "  exit /b 0", ")",
    ":loop", "`"$PyExe`" node_agent.py", "if %errorlevel%==3 goto :eof",
    "timeout /t 10 /nobreak >nul", "goto loop")
# runs the loop above with no window, so it can't be closed by accident
Set-Content "$Dest\node\start_node_hidden.vbs" -Encoding ASCII -Value @(
    'Set sh = CreateObject("WScript.Shell")', "sh.CurrentDirectory = `"$Dest\node`"",
    'sh.Run "cmd /c start_node.bat", 0, False')
Ok "installed in $Dest\node"

Step "Start by itself"
$me = "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$Dest\node\start_node_hidden.vbs`"" -WorkingDirectory "$Dest\node"
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName "PipsFarmer MT5 Node" -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $me) `
    -Principal $principal -Settings $settings -Force | Out-Null
# Windows updates: install, but restart only at the weekend (PipsFarmer restarts it on Saturday)
New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" -Force | Out-Null
Set-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" NoAutoRebootWithLoggedOnUsers 1 -Type DWord
Ok "MT5 node starts when $me logs in"

Step "Automatic login (needed so MT5 comes back after a restart)"
$done = $false
while (-not $done) {
    $sec = Read-Host "Windows password for $env:USERNAME (the one you log in to this VPS with)" -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    Add-Type -AssemblyName System.DirectoryServices.AccountManagement
    $ctx = New-Object System.DirectoryServices.AccountManagement.PrincipalContext('Machine')
    if (-not $ctx.ValidateCredentials($env:USERNAME, $plain)) { Warn "That password is wrong, try again."; continue }
    try {
        Invoke-WebRequest "https://download.sysinternals.com/files/AutoLogon.zip" -OutFile "$Tmp\AutoLogon.zip" -UseBasicParsing
        Expand-Archive "$Tmp\AutoLogon.zip" -DestinationPath "$Tmp\autologon" -Force
        & "$Tmp\autologon\Autologon64.exe" $env:USERNAME $env:USERDOMAIN $plain /accepteula | Out-Null
    } catch {
        $wl = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
        Set-ItemProperty $wl AutoAdminLogon "1"; Set-ItemProperty $wl DefaultUserName $env:USERNAME
        Set-ItemProperty $wl DefaultDomainName $env:USERDOMAIN; Set-ItemProperty $wl DefaultPassword $plain
    }
    $plain = $null
    $done = $true
    Ok "on"
}

Step "Keeping the desktop live after you disconnect"
New-Item -ItemType Directory -Force -Path "$Dest\tools" | Out-Null
Invoke-WebRequest "$ServerUrl/api/node/tool/keep_screen_on.ps1" -Headers @{ Authorization = "Bearer $Token" } -OutFile "$Dest\tools\keep_screen_on.ps1" -UseBasicParsing
Invoke-WebRequest "$ServerUrl/api/node/tool/register_keep_screen_on.ps1" -Headers @{ Authorization = "Bearer $Token" } -OutFile "$Dest\tools\register_keep_screen_on.ps1" -UseBasicParsing
foreach ($n in @("watchdog.ps1", "register_watchdog.ps1")) {
    Invoke-WebRequest "$ServerUrl/api/node/tool/$n" -Headers @{ Authorization = "Bearer $Token" } -OutFile "$Dest\tools\$n" -UseBasicParsing
}
& "$Dest\tools\register_keep_screen_on.ps1"
& "$Dest\tools\register_watchdog.ps1"

Step "Starting"
Start-ScheduledTask -TaskName "PipsFarmer MT5 Node"
Write-Host ""
Write-Host "Done. Within a minute this VPS shows as Online in Admin -> Servers." -ForegroundColor Cyan
Write-Host "Close the Windows App / Remote Desktop window when you leave. Don't 'Sign out' of Windows." -ForegroundColor Cyan
