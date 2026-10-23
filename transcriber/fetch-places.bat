@echo off
rem Builds places.csv (local streets, routes and landmarks) from OpenStreetMap. Takes a minute or two.
rem Hand-made rows in places.csv are kept. Restarting the transcriber is not needed.
cd /d "%~dp0"
.venv\Scripts\python.exe -m sdrtrunk_transcriber.fetch_places %*
pause
