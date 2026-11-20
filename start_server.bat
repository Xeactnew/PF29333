@echo off
cd /d "%~dp0"
:loop
python run_server.py
echo Server stopped, restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
