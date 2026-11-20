@echo off
rem PipsFarmer setup: double-click this on the VPS.
net session >nul 2>&1
if %errorlevel%==0 (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_vps.ps1"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','\"%~dp0setup_vps.ps1\"'"
)
