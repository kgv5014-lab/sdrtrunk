@echo off
rem Run by Windows at boot, before anyone signs in, once boot-on.bat has been run. Starts Ollama, the
rem transcriber and SDRTrunk with no windows, and keeps them running (the watchdog). Its log is
rem watchdog.log in this folder. Don't run this yourself; use start-everything.bat for the normal way.
cd /d "%~dp0"
if not exist config.toml copy config.example.toml config.toml >nul
.venv\Scripts\python.exe -m sdrtrunk_transcriber.watchdog --config config.toml --background
rem The updater closes everything on purpose; don't let Windows count that as the task failing.
if exist updating.flag exit /b 0
exit /b 1
