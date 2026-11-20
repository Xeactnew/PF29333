# PipsFarmer one-click VPS setup (Windows Server 2019/2022/2025).
# Run it with SETUP.bat (it asks for administrator rights).
#
# Installs Python, MetaTrader 5 and the Claude desktop app, sets up the PipsFarmer
# website and this VPS as your first MT5 server, and makes both start by themselves.

# Continue so warnings from python/pip on stderr don't stop the script; important steps check results
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"   # makes downloads much faster
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Dest      = "C:\PipsFarmer"
$PyVersion = "3.12.8"
$PyExe     = "C:\Program Files\Python312\python.exe"
$Mt5Dir    = "C:\Program Files\MetaTrader 5"
$Tmp       = Join-Path $env:TEMP "pipsfarmer-setup"
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Yellow }
function Ok($text)   { Write-Host "    $text" -ForegroundColor Green }
function Warn($text) { Write-Host "    $text" -ForegroundColor Magenta }
function Download($url, $file) {
    $out = Join-Path $Tmp $file
    Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing -ErrorAction Stop
    return $out
}

# ------------------------------------------------------------------ checks
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host "Please run SETUP.bat (it asks for administrator rights)." -ForegroundColor Red; exit 1 }

Write-Host "PipsFarmer VPS setup" -ForegroundColor Cyan
Write-Host "This takes about 10 minutes. Leave this window open until it says it's finished."

# ------------------------------------------------------------------ 1. files
Step "Copying PipsFarmer to $Dest"
$Src = $PSScriptRoot
if ((Resolve-Path $Src).Path.TrimEnd("\") -ne $Dest) {
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    # keep an existing .env, database and node.json if this is a re-run
    robocopy $Src $Dest /E /XF .env node.json /XD data accounts __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
}
Ok "done"

# ------------------------------------------------------------------ 2. python
Step "Installing Python $PyVersion"
if (Test-Path $PyExe) {
    Ok "already installed"
} else {
    try { $pyInst = Download "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-amd64.exe" "python.exe" }
    catch { Write-Host "Couldn't download Python: $($_.Exception.Message)" -ForegroundColor Red; Read-Host "Press Enter"; exit 1 }
    Start-Process $pyInst -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0 Include_launcher=1" -Wait
    if (-not (Test-Path $PyExe)) { Write-Host "Python didn't install. Run SETUP.bat again." -ForegroundColor Red; Read-Host "Press Enter"; exit 1 }
    Ok "installed"
}

Step "Installing PipsFarmer's Python packages"
& $PyExe -m pip install --upgrade pip --quiet --disable-pip-version-check
& $PyExe -m pip install -r "$Dest\requirements-server.txt" -r "$Dest\node\requirements-node.txt" --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Write-Host "Installing packages failed. Check the internet connection and run SETUP.bat again." -ForegroundColor Red; Read-Host "Press Enter"; exit 1 }
Ok "done"

# ------------------------------------------------------------------ 3. MT5
Step "Installing MetaTrader 5"
if (Test-Path "$Mt5Dir\terminal64.exe") {
    Ok "already installed"
} else {
    try {
        $mt5 = Download "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" "mt5setup.exe"
        Start-Process $mt5 -ArgumentList "/auto" -Wait
    } catch { Warn "Couldn't download MT5: $($_.Exception.Message)" }
    $waited = 0
    while (-not (Test-Path "$Mt5Dir\terminal64.exe") -and $waited -lt 300) { Start-Sleep 5; $waited += 5 }
    if (Test-Path "$Mt5Dir\terminal64.exe") { Ok "installed" } else { Warn "MT5 didn't finish installing. Install it from metatrader5.com, then run SETUP.bat again." }
}

# ------------------------------------------------------------------ 4. Claude desktop
Step "Installing the Claude desktop app"
if (Get-AppxPackage -Name "Claude" -ErrorAction SilentlyContinue) {
    Ok "already installed"
} else {
    try {
        $msix = Download "https://claude.ai/api/desktop/win32/x64/msix/latest/redirect" "Claude.msix"
        Add-AppxPackage -Path $msix -ErrorAction Stop
        Ok "installed - find Claude in the Start menu and sign in"
    } catch {
        Warn "Couldn't install Claude automatically ($($_.Exception.Message))."
        Warn "Opening the download page instead: run the Windows installer from there."
        Start-Process "https://claude.com/download"
    }
}

# ------------------------------------------------------------------ 5. settings
Step "Creating your settings (.env)"
$envFile = "$Dest\.env"
if (Test-Path $envFile) {
    Ok ".env already exists, keeping it"
} else {
    $keys = & $PyExe "$Dest\tools\make_keys.py" | Select-String "^PF_"
    $lines = @(
        "PF_SITE_NAME=PipsFarmer",
        "PF_BASE_URL=http://localhost:8000",
        "PF_HOST=127.0.0.1",
        "PF_PORT=8000",
        "# change to 1 once the site is on your domain with https",
        "PF_HTTPS_ONLY=0"
    ) + ($keys | ForEach-Object { $_.Line }) + @(
        "",
        "# Telegram reader: run  python tools\telegram_login.py  and paste its lines here",
        "TG_API_ID=",
        "TG_API_HASH=",
        "TG_MAIN_SESSION=",
        "TG_VIP_CHATS=",
        "",
        "# Admin bot from @BotFather; chat ids of you and your partner, comma separated",
        "TG_BOT_TOKEN=",
        "TG_ADMIN_CHAT_IDS="
    )
    Set-Content -Path $envFile -Value $lines -Encoding ASCII
    Copy-Item $envFile "$Dest\BACKUP-your-keys.env.txt"
    Ok "created. A copy is in BACKUP-your-keys.env.txt: save it somewhere safe (it unlocks saved MT5 passwords)"
}

# ------------------------------------------------------------------ 6. admins
Push-Location $Dest
Step "Admin logins for you and your partner"
$existing = & $PyExe manage.py list-admins
if ($existing) {
    Ok "admins already exist: $($existing -join ', ')"
} else {
    foreach ($who in @("your", "your partner's")) {
        $name = Read-Host "Choose $who admin username (letters/numbers)"
        if ($name) {
            Write-Host "    Passwords need at least 10 characters. Typing doesn't show on screen."
            & $PyExe manage.py create-admin $name
            if ($LASTEXITCODE -ne 0) { & $PyExe manage.py create-admin $name }
        }
    }
}

# ------------------------------------------------------------------ 7. this VPS as server 1
Step "Registering this VPS as your first MT5 server"
if (Test-Path "$Dest\node\node.json") {
    Ok "node.json already exists, keeping it"
} else {
    $slots = Read-Host "How many customer accounts should this VPS run? (press Enter for 20)"
    if (-not $slots) { $slots = "20" }
    $out = & $PyExe manage.py create-node "VPS 1" $slots
    $token = ($out | Select-String "^TOKEN=").Line -replace "^TOKEN=", ""
    if (-not $token) { Write-Host "Couldn't register the VPS: $out" -ForegroundColor Red; Read-Host "Press Enter"; exit 1 }
    $nodeCfg = [ordered]@{
        server_url   = "http://127.0.0.1:8000"
        node_token   = $token
        mt5_template = $Mt5Dir
        accounts_dir = "C:\PipsFarmer\accounts"
    }
    $nodeCfg | ConvertTo-Json | Set-Content -Path "$Dest\node\node.json" -Encoding ASCII
    Ok "registered with $slots slots"
}
Pop-Location

# ------------------------------------------------------------------ 8. start on boot
Step "Making PipsFarmer start by itself"
Set-Content -Path "$Dest\start_server.bat" -Encoding ASCII -Value @(
    "@echo off", "cd /d C:\PipsFarmer", ":loop", "`"$PyExe`" run_server.py",
    "timeout /t 10 /nobreak >nul", "goto loop")
Set-Content -Path "$Dest\node\start_node.bat" -Encoding ASCII -Value @(
    "@echo off", "cd /d C:\PipsFarmer\node",
    'if not "%PF_NODE_HIDDEN%"=="1" if exist "%~dp0start_node_hidden.vbs" (',
    "  set PF_NODE_HIDDEN=1", '  wscript.exe "%~dp0start_node_hidden.vbs"', "  exit /b 0", ")",
    ":loop", "`"$PyExe`" node_agent.py", "if %errorlevel%==3 goto :eof",
    "timeout /t 10 /nobreak >nul", "goto loop")

$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

# website: runs at boot in the background, nobody needs to be logged in
$srvAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Dest\start_server.bat`"" -WorkingDirectory $Dest
Register-ScheduledTask -TaskName "PipsFarmer Website" -Action $srvAction -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -User "SYSTEM" -RunLevel Highest -Settings $settings -Force | Out-Null

# node: MT5 needs a logged-in desktop, so it starts when this user logs in
$me = "$env:USERDOMAIN\$env:USERNAME"
$nodeAction = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$Dest\node\start_node_hidden.vbs`"" -WorkingDirectory "$Dest\node"
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName "PipsFarmer MT5 Node" -Action $nodeAction -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $me) `
    -Principal $principal -Settings $settings -Force | Out-Null
Ok "website starts at boot; MT5 node starts when $me logs in"
New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" -Force | Out-Null
Set-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" NoAutoRebootWithLoggedOnUsers 1 -Type DWord
Ok "Windows updates restart the VPS only at the weekend (PipsFarmer does it on Saturday)"

Step "Prompt-free updates"
& "$Dest\tools\register_update_helper.ps1"
& "$Dest\tools\register_watchdog.ps1"

Step "Keeping the desktop live after you disconnect"
& "$Dest\tools\register_keep_screen_on.ps1"

Step "Automatic login after a restart"
Write-Host "    MT5 needs a logged-in desktop. With automatic login (recommended), the VPS logs itself in after Windows updates"
Write-Host "    or a reboot, so trading carries on without you connecting."
$auto = Read-Host "Turn on automatic login for $env:USERNAME? (y/n)"
if ($auto -match "^[yY]") {
    try {
        $zip = Download "https://download.sysinternals.com/files/AutoLogon.zip" "AutoLogon.zip"
        Expand-Archive $zip -DestinationPath "$Tmp\autologon" -Force -ErrorAction Stop
        $sec = Read-Host "Enter the Windows password for $env:USERNAME" -AsSecureString
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
        & "$Tmp\autologon\Autologon64.exe" $env:USERNAME $env:USERDOMAIN $plain /accepteula | Out-Null
        $plain = $null
        Ok "on (the password is stored encrypted by Windows)"
    } catch {
        Warn "Couldn't set it up: $($_.Exception.Message). You can run Sysinternals Autologon yourself later."
    }
} else {
    Warn "Skipped. After a reboot, connect once with Windows App so MT5 starts."
}

# ------------------------------------------------------------------ 9. start now
Step "Starting PipsFarmer"
Start-ScheduledTask -TaskName "PipsFarmer Website"
Start-Sleep 8
Start-ScheduledTask -TaskName "PipsFarmer MT5 Node"
try {
    Invoke-WebRequest "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 20 -ErrorAction Stop | Out-Null
    Ok "website is running"
} catch {
    Warn "The website hasn't answered yet. Give it a minute, then open http://localhost:8000"
}
Start-Process "http://localhost:8000/admin"

Write-Host ""
Write-Host "========================================================================" -ForegroundColor Cyan
Write-Host " Finished. PipsFarmer is running on this VPS." -ForegroundColor Cyan
Write-Host "========================================================================" -ForegroundColor Cyan
Write-Host @"

 Admin page:    http://localhost:8000/admin   (open in the browser on this VPS)
 Customer site: http://localhost:8000

 Still to do (the README has the details):
  1. Open MetaTrader 5, click Algo Trading so it's green, then File > Open an Account
     and search each broker you'll test with (pick it, then Cancel). Close MT5.
  2. Telegram reader: in PowerShell run
        cd C:\PipsFarmer
        python tools\telegram_login.py
     and paste the lines it prints into C:\PipsFarmer\.env
  3. Admin bot: create one with @BotFather, put the token and both your chat ids in .env
  4. Restart the VPS (or the 'PipsFarmer Website' task) so .env changes load.
  5. Sign in to the Claude app (Start menu) and open your PipsFarmer chat.

 When you leave, CLOSE the Windows App window. Don't 'Sign out' of Windows,
 or MT5 stops until you log in again.
"@
Read-Host "Press Enter to close"
