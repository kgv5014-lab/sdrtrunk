@echo off
rem Learned hints check: runs the 50 newest calls (and every call you fixed by hand) through Whisper
rem without and with the streets and units learned from the call log, then opens a side-by-side report.
rem The transcriber can keep running. For more or fewer calls: compare-hints.bat 100
cd /d "%~dp0"
set CALLS=%1
if "%CALLS%"=="" set CALLS=50
call use-whisper-gpu.bat
.venv\Scripts\python.exe -m sdrtrunk_transcriber.compare --config config.toml --calls %CALLS% --hints
pause
