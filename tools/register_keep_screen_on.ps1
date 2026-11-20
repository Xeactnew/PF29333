# Registers the "PipsFarmer Keep Screen On" task (run as administrator; setup and update do this for you).
$script = "C:\PipsFarmer\tools\keep_screen_on.ps1"
$class = Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskSessionStateChangeTrigger
$trigger = New-CimInstance -CimClass $class -ClientOnly
$trigger.StateChange = 4          # TASK_REMOTE_DISCONNECT
$trigger.Enabled = $true
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName "PipsFarmer Keep Screen On" -Action $action -Trigger $trigger -User "SYSTEM" `
    -RunLevel Highest -Settings $settings -Force | Out-Null
# don't blank or lock the VPS's own screen
powercfg /change monitor-timeout-ac 0 2>$null
Write-Host "    the desktop stays live after you disconnect"
