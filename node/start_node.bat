@echo off
cd /d "%~dp0"
rem Started with a window (e.g. by the scheduled task): start again with no window and close this one,
rem so the MT5 manager can't be closed by accident.
if not "%PF_NODE_HIDDEN%"=="1" if exist "%~dp0start_node_hidden.vbs" (
  set PF_NODE_HIDDEN=1
  wscript.exe "%~dp0start_node_hidden.vbs"
  exit /b 0
)
:loop
python node_agent.py
rem exit code 3 = another copy is already running, so this one stops
if %errorlevel%==3 goto :eof
echo Node agent stopped, restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
