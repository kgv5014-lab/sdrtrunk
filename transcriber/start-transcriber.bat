@echo off
rem Starts the SDRTrunk transcription service. Run install.bat once first.
cd /d "%~dp0"
if not exist config.toml copy config.example.toml config.toml >nul
if not exist vocabulary.txt copy vocabulary.example.txt vocabulary.txt >nul
.venv\Scripts\python.exe -m sdrtrunk_transcriber --config config.toml
pause
