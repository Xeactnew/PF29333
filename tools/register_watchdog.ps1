# Registers "PipsFarmer Watchdog": every 5 minutes (and 2 minutes after a restart) it brings back anything that stopped.
$script = "C:\PipsFarmer\tools\watchdog.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$boot = New-ScheduledTaskTrigger -AtStartup
$boot.Delay = "PT2M"
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "PipsFarmer Watchdog" -Action $action -Trigger @($boot, $every) -User "SYSTEM" `
    -RunLevel Highest -Settings $settings -Force | Out-Null
# the tunnel service: start with Windows and restart by itself if it ever crashes
if (Get-Service cloudflared -ErrorAction SilentlyContinue) {
    sc.exe config cloudflared start= auto | Out-Null
    sc.exe failure cloudflared reset= 86400 actions= restart/5000/restart/5000/restart/30000 | Out-Null
}
Write-Host "    watchdog checks everything every 5 minutes and after a restart"
