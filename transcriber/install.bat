@echo off
rem One-time setup: creates a Python environment and installs the service plus NVIDIA GPU libraries.
cd /d "%~dp0"
where py >nul 2>nul || (echo Python is not installed. Get Python 3.12 from https://www.python.org/downloads/ and tick "Add to PATH". & pause & exit /b 1)
py -3.12 -m venv .venv || py -3 -m venv .venv || (echo Could not create the Python environment. & pause & exit /b 1)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-gpu.txt || (echo Install failed, see the messages above. & pause & exit /b 1)
if not exist config.toml copy config.example.toml config.toml >nul
if not exist vocabulary.txt copy vocabulary.example.txt vocabulary.txt >nul
if not exist codes.csv copy codes.example.csv codes.csv >nul
if not exist places.csv copy places.example.csv places.csv >nul
echo.
echo Installed. Next: run start-transcriber.bat (it picks the GTX 1070 Ti by itself)
pause
