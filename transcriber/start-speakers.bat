@echo off
rem Plays the live calls on this PC's speakers while SDRTrunk runs with no window (boot-on.bat starts
rem this when you sign in). Close this window to stop the sound; run it again to start it.
cd /d "%~dp0"
title PC speakers
if not exist config.toml copy config.example.toml config.toml >nul
.venv\Scripts\python.exe -m sdrtrunk_transcriber.pc_speakers --config config.toml
pause
