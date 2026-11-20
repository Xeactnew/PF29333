# PipsFarmer: keep the VPS desktop running after you close Windows App / Remote Desktop.
# Runs as SYSTEM whenever a remote session disconnects (scheduled task "PipsFarmer Keep Screen On").
# It hands the disconnected session back to the VPS's own screen (the console), unlocked, so MT5 keeps
# a real desktop and screenshots (Claude, monitoring) still work. Reconnecting with Windows App works as normal.
$ErrorActionPreference = "SilentlyContinue"
$log = "C:\PipsFarmer\logs\keep_screen_on.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
Start-Sleep -Seconds 3
$lines = & quser 2>$null
foreach ($line in $lines) {
    if ($line -match '^\s*>?(?<user>\S+)\s+(?:(?<sess>[^\s\d]\S*)\s+)?(?<id>\d+)\s+(?<state>Disc)') {
        $id = $Matches.id
        & "$env:windir\System32\tscon.exe" $id /dest:console 2>&1 | Out-Null
        Add-Content $log "$(Get-Date -Format s) moved session $id ($($Matches.user)) to the console, exit $LASTEXITCODE"
        break
    }
}
