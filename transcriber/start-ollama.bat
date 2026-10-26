@echo off
rem Starts Ollama on the GTX 1070 for call summaries (see "Call summaries" in the README).
rem start-transcriber.bat gives Whisper the 1070 Ti by UUID; this window picks the 1070 the same way.
rem GPU_NAME must match a line of `nvidia-smi -L` up to " (UUID". "GTX 1070" does not match the 1070 Ti.
set GPU_NAME=GTX 1070
set MODEL=qwen2.5:7b

where ollama >nul 2>&1
if errorlevel 1 (
  echo Ollama is not installed. Get it from https://ollama.com/download and run this again.
  pause
  exit /b 1
)

set GPU_UUID=
rem A line reads "GPU 0: NVIDIA GeForce GTX 1070 (UUID: GPU-...)"; keep the GPU-... part.
for /f "tokens=2 delims=()" %%a in ('nvidia-smi -L ^| findstr /c:"%GPU_NAME% (UUID"') do (
  for /f "tokens=2" %%b in ("%%a") do set GPU_UUID=%%b
)
if defined GPU_UUID (
  echo Using %GPU_NAME%: %GPU_UUID%
  set CUDA_VISIBLE_DEVICES=%GPU_UUID%
  rem Ollama also finds cards through Vulkan, which ignores CUDA_VISIBLE_DEVICES and would put part of
  rem the model on the 1070 Ti next to Whisper. CUDA alone is enough for the 1070.
  set OLLAMA_VULKAN=0
) else (
  echo Could not find a "%GPU_NAME%" in nvidia-smi -L. Ollama will pick a GPU itself.
  set CUDA_VISIBLE_DEVICES=
)

rem The Ollama tray app starts its own copy at sign-in, which would use the wrong card. Stop it.
taskkill /im "ollama app.exe" /f >nul 2>&1
taskkill /im ollama.exe /f >nul 2>&1
timeout /t 2 /nobreak >nul

start "Ollama (%GPU_NAME%)" /min ollama serve
timeout /t 5 /nobreak >nul

ollama list | findstr /c:"%MODEL%" >nul
if errorlevel 1 (
  echo Downloading %MODEL% once, about 5 GB...
  ollama pull %MODEL%
)
echo Ollama is running in its own window (minimized). Leave it open while the transcriber runs.
timeout /t 10
