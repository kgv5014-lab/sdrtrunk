@echo off
rem Sends a test alert to the phone. The transcriber must be running with [alerts] enabled = true.
curl -s -X POST http://localhost:3000/api/alerts/test
echo.
pause
