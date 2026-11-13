@echo off
rem Starts Ollama on the GTX 1070 Ti for call summaries (see "Call summaries" in the README).
rem The monitor is on the 1070, and Windows' own windows filled so much of it that the model (about 6 GB)
rem no longer fit and ran partly on the CPU. So Ollama gets the 1070 Ti to itself, and start-transcriber.bat
rem puts Whisper (about 1.5 GB) on the 1070 by UUID.
rem GPU_NAME must match a line of `nvidia-smi -L` up to " (UUID".
set GPU_NAME=GTX 1070 Ti
rem The model comes from [summaries] in config.toml; this one is used if that can't be read.
set MODEL=qwen3.5:9b-q4_K_M
if exist .venv\Scripts\python.exe (
  for /f "delims=" %%m in ('.venv\Scripts\python.exe -m sdrtrunk_transcriber.ollama_model config.toml 2^>nul') do set MODEL=%%m
)

where ollama >nul 2>&1
if errorlevel 1 (
  echo Ollama is not installed. Get it from https://ollama.com/download and run this again.
  if not defined SDRTRUNK_HIDDEN pause
  exit /b 1
)

set GPU_UUID=
rem A line reads "GPU 1: NVIDIA GeForce GTX 1070 Ti (UUID: GPU-...)"; keep the GPU-... part.
for /f "tokens=2 delims=()" %%a in ('nvidia-smi -L ^| findstr /c:"%GPU_NAME% (UUID"') do (
  for /f "tokens=2" %%b in ("%%a") do set GPU_UUID=%%b
)
if defined GPU_UUID (
  echo Using %GPU_NAME%: %GPU_UUID%
  set CUDA_VISIBLE_DEVICES=%GPU_UUID%
  rem Ollama also finds cards through Vulkan, which ignores CUDA_VISIBLE_DEVICES and would put part of
  rem the model on the 1070 next to Whisper. CUDA alone is enough for the 1070 Ti.
  set OLLAMA_VULKAN=0
) else (
  echo Could not find a "%GPU_NAME%" in nvidia-smi -L. Ollama will pick a GPU itself.
  set CUDA_VISIBLE_DEVICES=
)

rem The Ollama tray app starts its own copy at sign-in, which would use the wrong card. Stop it.
taskkill /im "ollama app.exe" /f >nul 2>&1
taskkill /im ollama.exe /f >nul 2>&1
timeout /t 2 /nobreak >nul

rem Started with no windows (boot or the tray icon): keep Ollama in this hidden window instead of a new one.
if defined SDRTRUNK_HIDDEN (
  start "" /b ollama serve
) else (
  start "Ollama (%GPU_NAME%)" /min ollama serve
)
timeout /t 5 /nobreak >nul
rem The Ollama tray app can start again with Windows or on its own; it then keeps trying to start a second
rem copy of Ollama (hundreds of "port already in use" errors in its log). This window's copy is all that's needed.
taskkill /im "ollama app.exe" /f >nul 2>&1
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Ollama.lnk" (
  del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Ollama.lnk" >nul 2>&1
  echo Turned off the Ollama tray app starting with Windows; this window starts Ollama instead.
)

ollama list | findstr /c:"%MODEL%" >nul
if errorlevel 1 (
  echo Downloading %MODEL% once, about 6.6 GB. Needs a recent Ollama; if it fails, update Ollama from https://ollama.com/download...
  ollama pull %MODEL%
  if errorlevel 1 echo Could not download %MODEL%. If it is a model you made yourself, create it with "ollama create" first.
)
rem Summaries never look at pictures, so remake the model once without its image add-on (about 1 GB).
if exist .venv\Scripts\python.exe .venv\Scripts\python.exe -m sdrtrunk_transcriber.ollama_text_only config.toml
echo Ollama is running in its own window (minimized). Leave it open while the transcriber runs.
timeout /t 10
