@echo off
rem One-click update: gets the latest SDRTrunk Plus from GitHub, builds SDRTrunk and installs the new
rem transcriber files, keeping your settings. The steps are in update-sdrtrunk-plus.ps1 next to this file.
rem Another transcriber folder: update-sdrtrunk-plus.bat -TranscriberDir "D:\my-transcriber"
cd /d "%~dp0"
rem Both commands sit on one line so a pull that changes this file cannot break the running copy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-sdrtrunk-plus.ps1" %* & pause
