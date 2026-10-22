# SDRTrunk call transcriber

A small local service that turns SDRTrunk calls into text. SDRTrunk uploads each finished call to it
through its built-in **Rdio Scanner** streaming option, so SDRTrunk itself needs no changes. The service
saves the audio, transcribes it with Whisper, and logs every call with its time, talkgroup, radio ID and
transcript in a searchable page at <http://localhost:3000>.

```
SDRTrunk --(Rdio Scanner upload: MP3 + talkgroup, radio ID, time)--> transcriber --> Whisper
                                                                         |
                                                     call log (SQLite) <-+--> web page
```

Two Whisper integrations are included:

| Backend | What it is | When to use it |
| --- | --- | --- |
| `faster-whisper` (default) | Runs Whisper inside the service on an NVIDIA GPU (or CPU) | Best accuracy and speed on an NVIDIA card |
| `whisper-cpp` | Sends audio to a running [whisper.cpp](https://github.com/ggml-org/whisper.cpp) `whisper-server` | No Python GPU libraries, AMD/Intel GPUs via Vulkan, or Whisper on another PC |

## Install on Windows

You need Python 3.11 or newer (3.12 recommended, from [python.org](https://www.python.org/downloads/), tick
"Add python.exe to PATH") and a current NVIDIA driver.

1. Copy this `transcriber` folder anywhere, for example `C:\sdrtrunk-transcriber`.
2. Double-click **install.bat**. It creates a private Python environment in `.venv` and installs the
   service plus the NVIDIA cuBLAS and cuDNN libraries (about 1.5 GB download).
3. Open a Command Prompt and run `nvidia-smi -L`. Note the number of the GTX 1070 Ti, for example
   `GPU 1: NVIDIA GeForce GTX 1070 Ti`.
4. Open **config.toml** in Notepad and set `device_index` under `[faster_whisper]` to that number.
5. Optional: put local unit names, streets and 10-codes in **vocabulary.txt**, one per line.
6. Double-click **start-transcriber.bat**. The first start downloads the `large-v3-turbo` model
   (about 1.6 GB) and then logs `faster-whisper model loaded`.

Check Whisper on its own with any recording before wiring up SDRTrunk:

```
.venv\Scripts\python.exe -m sdrtrunk_transcriber --test-file "C:\path\to\a\recording.mp3"
```

### GTX 10-series notes

* Keep `compute_type = "int8"` (or `"float32"`). GTX 10-series (Pascal) cards run float16 very slowly, and
  CTranslate2 only supports int8 on Pascal through its int8/float32 mode, which is what `int8` selects.
* The service uses CUDA 12 libraries. CUDA 13 no longer supports GTX 10-series cards, so don't upgrade
  those packages past what `requirements-gpu.txt` pins.
* Whisper (about 2 GB with int8) fits easily on the 8 GB 1070 Ti, leaving the 1070 free for Ollama later.
* SDRTrunk does not use the GPU, so decoding is not affected.

## Point SDRTrunk at it

1. In SDRTrunk open **Playlist Editor > Streaming**, add a **Rdio Scanner** stream and set:
   * **Name**: `Transcriber`
   * **API Key**: any text, for example `local`. If you also put it in `api_key` in config.toml, the
     service rejects uploads without it.
   * **System ID**: any number above 0, for example `1`
   * **RdioScanner URL**: `http://localhost:3000` (SDRTrunk adds `/api/call-upload` itself)
   * **Enabled**: on
2. Save. Within a few seconds the stream status turns **Connected**.
3. In **Playlist Editor > Aliases**, open each talkgroup you want transcribed, and under **Streaming**
   tick **Transcriber**. SDRTrunk only uploads calls whose alias is assigned to a stream.
4. Recommended: in **User Preferences > Audio > Call Management**, turn on duplicate call detection and
   **Streaming** suppression, so the same call heard on two sites is transcribed once.

Calls appear on <http://localhost:3000> a moment after they end. The page refreshes every five seconds,
searches transcripts, and filters by talkgroup or radio ID.

## Using whisper.cpp instead

1. Download a Windows release of whisper.cpp (the CUDA or Vulkan build) and a model such as
   `ggml-large-v3-turbo.bin` or `ggml-medium.en.bin`.
2. Start it: `whisper-server.exe -m ggml-large-v3-turbo.bin --port 8080`
3. In config.toml set `backend = "whisper-cpp"`. The default `url` matches the command above.

## Where things are stored

* `data\calls.db`: the call log (SQLite, with full-text search on transcripts)
* `data\calls\YYYY\MM\DD\`: one MP3 per call

Deleting the `data` folder starts a fresh log. Calls that were still waiting when the service stopped
are transcribed when it starts again.

## Accuracy tips

* Whisper invents words on silence. The service drops calls shorter than `min_duration_s`, runs voice
  activity filtering, discards segments Whisper flags as non-speech, and treats each call on its own.
* A good vocabulary list helps more than any other setting with unit and street names.
* Clean decoding matters most: transcripts are only as good as the audio SDRTrunk produces.

## Development

```
pip install -r requirements.txt pytest httpx
python -m pytest tests
```

The tests post uploads laid out byte for byte the way SDRTrunk's `RdioScannerBuilder` builds them, and
use stand-ins for both Whisper backends, so they run without a GPU or a model download.
