@echo off
rem Undoes autostart-on.bat: everything no longer starts by itself at sign-in.
powershell -NoProfile -Command "Remove-Item -ErrorAction SilentlyContinue ([Environment]::GetFolderPath('Startup') + '\SDRTrunk Plus.lnk')"
echo Done. Start things yourself with start-everything.bat from now on.
pause
