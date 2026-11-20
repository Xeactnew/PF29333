# Registers "PipsFarmer Update Helper": lets UPDATE.bat run updates without a Windows permission prompt.
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"C:\PipsFarmer\tools\run_update_helper.ps1`""
$principal = New-ScheduledTaskPrincipal -GroupId "BUILTIN\Administrators" -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "PipsFarmer Update Helper" -Action $action -Principal $principal -Settings $settings -Force | Out-Null
New-Item -ItemType Directory -Force -Path "C:\Users\Public\PipsFarmer" | Out-Null
Write-Host "    future updates run without a permission prompt"
