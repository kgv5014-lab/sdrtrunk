@echo off
rem Turns off the map link for friends: the link and password stop working and everyone is signed out.
rem The listen-only link keeps working. Run setup-share-link.bat to share again (with a new link).
cd /d "%~dp0"
.venv\Scripts\python.exe -m sdrtrunk_transcriber --config config.toml --share stop
pause
