@echo off
rem Run once: from now on Windows starts everything (start-everything.bat) a minute after you sign in.
rem It puts a shortcut called "SDRTrunk Plus" in your Startup folder. autostart-off.bat removes it.
cd /d "%~dp0"
powershell -NoProfile -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + '\SDRTrunk Plus.lnk'); $s.TargetPath = '%~dp0autostart.bat'; $s.WorkingDirectory = '%~dp0'; $s.WindowStyle = 7; $s.Description = 'Starts SDRTrunk, the transcriber and Ollama at sign-in'; $s.Save()"
if errorlevel 1 (
  echo Could not add the shortcut.
  pause
  exit /b 1
)
echo Done. Everything now starts by itself a minute after you sign in to Windows.
echo To stop that, run autostart-off.bat.
echo.
echo For it to work after a power cut or a Windows Update restart with nobody at the PC, Windows has
echo to sign you in by itself. See "Start everything when the PC boots" in README.md.
pause
