@echo off
rem Lists the addresses the autocorrect would fix in the last 30 days of calls, without changing anything.
rem Needs address-ranges.json from fetch-places.bat. For more days: check-addresses.bat 90
cd /d "%~dp0"
set DAYS=%1
if "%DAYS%"=="" set DAYS=30
.venv\Scripts\python.exe -m sdrtrunk_transcriber.address_fix --config config.toml --days %DAYS%
pause
