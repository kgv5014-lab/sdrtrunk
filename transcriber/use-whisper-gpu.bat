@echo off
rem Points Whisper at the GTX 1070 (the card the monitor is on), called by start-transcriber.bat, compare-transcripts.bat and compare-hints.bat.
rem Windows tools (nvidia-smi) and CUDA can number the two cards differently, so a card number such as
rem CUDA_VISIBLE_DEVICES=1 can land on the 1070 Ti that Ollama uses. A card's UUID never changes, so this
rem finds the 1070 by name and hands CUDA its UUID. Whisper then sees only that card, as device 0.
rem WHISPER_GPU_NAME must match a line of `nvidia-smi -L` up to " (UUID"; "GTX 1070" does not match the 1070 Ti.
rem Whisper needs only about 1.5 GB, so it shares the monitor's card; Ollama's model (about 6 GB) gets the
rem 1070 Ti to itself. With the monitor's windows on the same card, the model didn't fit and ran partly on the CPU.
if not defined WHISPER_GPU_NAME set WHISPER_GPU_NAME=GTX 1070
set CUDA_DEVICE_ORDER=PCI_BUS_ID

set WHISPER_GPU_UUID=
rem A line reads "GPU 0: NVIDIA GeForce GTX 1070 (UUID: GPU-...)"; keep the GPU-... part.
for /f "tokens=2 delims=()" %%a in ('nvidia-smi -L 2^>nul ^| findstr /c:"%WHISPER_GPU_NAME% (UUID"') do (
  for /f "tokens=2" %%b in ("%%a") do set WHISPER_GPU_UUID=%%b
)
if defined WHISPER_GPU_UUID (
  echo Whisper uses the %WHISPER_GPU_NAME%: %WHISPER_GPU_UUID%
  set CUDA_VISIBLE_DEVICES=%WHISPER_GPU_UUID%
  set WHISPER_DEVICE_INDEX=0
) else (
  echo Could not find a "%WHISPER_GPU_NAME%" in nvidia-smi -L. Using device_index from config.toml.
)
