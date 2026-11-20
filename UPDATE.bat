@echo off
rem PipsFarmer update: double-click this in the NEW extracted zip.
rem Easiest of all: send the zip to your admin bot on Telegram, or upload it in Admin > System.
net session >nul 2>&1
if %errorlevel%==0 (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_vps.ps1"
  goto :eof
)
schtasks /query /tn "PipsFarmer Update Helper" >nul 2>&1
if %errorlevel%==0 (
  if not exist "C:\Users\Public\PipsFarmer" mkdir "C:\Users\Public\PipsFarmer"
  echo %~dp0> "C:\Users\Public\PipsFarmer\update-from.txt"
  schtasks /run /tn "PipsFarmer Update Helper" >nul
  echo Updating PipsFarmer in the background. It takes about a minute.
  echo The log is in C:\PipsFarmer\updates\last-update.log and in Admin ^> System.
  timeout /t 15 >nul
  goto :eof
)
rem first time only: Windows asks for permission once
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','\"%~dp0update_vps.ps1\"'"
