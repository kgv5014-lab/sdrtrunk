@echo off
rem Starts the SDRTrunk transcription service. Run install.bat once first.
cd /d "%~dp0"
if not exist config.toml copy config.example.toml config.toml >nul
if not exist vocabulary.txt copy vocabulary.example.txt vocabulary.txt >nul
if not exist codes.csv copy codes.example.csv codes.csv >nul
if not exist places.csv copy places.example.csv places.csv >nul
if not exist paging_tones.csv copy paging_tones.example.csv paging_tones.csv >nul
call use-whisper-gpu.bat
.venv\Scripts\python.exe -m sdrtrunk_transcriber --config config.toml
pause
