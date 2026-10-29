@echo off
rem Sets the password for the map link you send to friends, and shows the link.
rem Friends see the map, calls and audio, view only. Run setup-listen-link.bat once first: it turns on
rem the public HTTPS address this uses. Run this again any time to change the password (everyone
rem signed in has to sign in again). stop-sharing.bat turns the link off.
cd /d "%~dp0"
if not exist config.toml copy config.example.toml config.toml >nul
where tailscale >nul 2>nul
if errorlevel 1 (
  set "PATH=%PATH%;C:\Program Files\Tailscale"
)
.venv\Scripts\python.exe -m sdrtrunk_transcriber --config config.toml --share setup
echo The transcriber picks this up right away; no restart needed.
pause
