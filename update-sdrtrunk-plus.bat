@echo off
rem One-click update: gets the latest SDRTrunk Plus from GitHub, builds SDRTrunk and installs the new
rem transcriber files, keeping your settings. The steps are in update-sdrtrunk-plus.ps1 next to this file.
rem Another transcriber folder: update-sdrtrunk-plus.bat -TranscriberDir "D:\my-transcriber"
cd /d "%~dp0"
rem The script asks Windows for admin rights itself and carries on in a new admin window. Exit code 10
rem means it did that, so this window closes; otherwise it waits for a key so the messages can be read.
rem Both commands sit on one line so a pull that changes this file cannot break the running copy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-sdrtrunk-plus.ps1" %* & (if not errorlevel 10 pause) & exit /b
