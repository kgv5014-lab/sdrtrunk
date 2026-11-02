@echo off
rem Run by Windows at sign-in once autostart-on.bat has been run. Gives the PC a minute to finish
rem starting (USB dongles, network, NordVPN, Tailscale), then runs start-everything.bat in this window.
cd /d "%~dp0"
title SDRTrunk watchdog
rem Don't start a second watchdog if one is already running.
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*sdrtrunk_transcriber.watchdog*' }) { exit 1 }"
if errorlevel 1 (
  echo The watchdog is already running, so nothing to do.
  timeout /t 10
  exit /b 0
)
echo Starting SDRTrunk, the transcriber and Ollama in 60 seconds. Press a key to start now.
timeout /t 60
call start-everything.bat
