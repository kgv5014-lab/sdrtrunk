@echo off
rem Run once: from now on Windows starts SDRTrunk, the transcriber and Ollama when the PC boots, before
rem anyone signs in, with no windows. Asks for Windows' permission and your Windows password once.
rem boot-off.bat goes back to the normal way.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%~dp0boot-setup.ps1\" -Mode on'"
