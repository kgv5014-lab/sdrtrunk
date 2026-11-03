@echo off
rem Undoes boot-on.bat: nothing starts at boot any more. Asks for Windows' permission once.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%~dp0boot-setup.ps1\" -Mode off'"
