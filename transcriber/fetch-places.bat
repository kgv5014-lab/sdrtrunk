@echo off
rem Builds places.csv (local streets, routes and landmarks) from OpenStreetMap, then address-ranges.json
rem (house numbers on every street, from the US Census) for the address autocorrect. Takes a few minutes.
rem Hand-made rows in places.csv are kept. Restarting the transcriber is not needed.
cd /d "%~dp0"
.venv\Scripts\python.exe -m sdrtrunk_transcriber.fetch_places %*
pause
