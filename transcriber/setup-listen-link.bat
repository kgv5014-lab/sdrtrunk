@echo off
rem Puts ONLY the listen-only page on the internet with Tailscale Funnel, so your phone can listen with
rem NordVPN (or no VPN) on. The transcriber serves that page on 127.0.0.1:3001, and every request needs
rem the secret key from data\listen-key.txt. The map, call log and settings stay private on Tailscale.
rem
rem Run this once. Tailscale remembers it across restarts.
rem To turn the public link off again:  tailscale funnel --https=8443 off

where tailscale >nul 2>nul
if errorlevel 1 (
  set "PATH=%PATH%;C:\Program Files\Tailscale"
)

tailscale funnel --bg --https=8443 http://127.0.0.1:3001
if errorlevel 1 (
  echo.
  echo Tailscale couldn't turn on Funnel. If it printed a link above, open it, allow Funnel for this PC,
  echo then run this file again.
  pause
  exit /b 1
)

echo.
tailscale funnel status
echo.
echo Done. On the map page press "Listen" (on this PC or over Tailscale) to see and copy your secret link.
pause
