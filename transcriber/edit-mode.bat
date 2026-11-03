@echo off
rem With boot-on.bat, SDRTrunk runs with no window. This stops that copy and opens the normal SDRTrunk
rem window so you can change channels or settings. Close the SDRTrunk window when you're done and the
rem no-window copy starts again by itself (your changes are kept).
cd /d "%~dp0"
title SDRTrunk edit mode
.venv\Scripts\python.exe -m sdrtrunk_transcriber.editmode --config config.toml
pause
