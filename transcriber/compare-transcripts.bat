@echo off
rem Before/after check: runs the 50 newest calls (and every call you fixed by hand) through Whisper the
rem old way and the new way, then opens a side-by-side report. The transcriber can keep running.
rem For more or fewer calls: compare-transcripts.bat 100
cd /d "%~dp0"
set CALLS=%1
if "%CALLS%"=="" set CALLS=50
.venv\Scripts\python.exe -m sdrtrunk_transcriber.compare --config config.toml --calls %CALLS%
pause
