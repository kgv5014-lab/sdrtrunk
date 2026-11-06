@echo off
rem Starts the SDRTrunk Plus tray icon (a dot by the clock) and, with it, the calls on the PC speakers.
rem boot-on.bat makes this start when you sign in. Running it again while the icon is there does nothing.
cd /d "%~dp0"
if not exist config.toml copy config.example.toml config.toml >nul
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0tray.ps1"
