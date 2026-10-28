@echo off
rem Starts Ollama (when [summaries] is on), the transcriber and SDRTrunk, then keeps watching them:
rem any that crashes or freezes is restarted, with a phone alert. Leave this window open.
rem Closing it stops the watching; the programs keep running.
cd /d "%~dp0"
title SDRTrunk watchdog
if not exist config.toml copy config.example.toml config.toml >nul
.venv\Scripts\python.exe -m sdrtrunk_transcriber.watchdog --config config.toml
pause
