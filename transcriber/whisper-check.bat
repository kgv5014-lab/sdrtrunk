@echo off
rem Whisper check: scores Whisper against the calls you typed from the audio on the Whisper check page
rem (call log > Whisper check), lists its most common mistakes and opens the report.
rem The transcriber can keep running. To also try the other models that fit the card: whisper-check.bat all
cd /d "%~dp0"
set MORE=
if /i "%1"=="all" set MORE=--all
call use-whisper-gpu.bat
.venv\Scripts\python.exe -m sdrtrunk_transcriber.whisper_check --config config.toml %MORE%
pause
