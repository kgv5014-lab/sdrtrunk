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
* Tone-out pages from SDRTrunk's two-tone paging decoder are kept in the same `calls.db`

Deleting the `data` folder starts a fresh log. Calls that were still waiting when the service stopped
are transcribed when it starts again.

## Local codes, call signs and times

`codes.csv` (copied from `codes.example.csv` on first start) holds your area's radio codes. The starter list
is for Northampton County, PA: Pennsylvania State Police 10-codes and the county's call-sign numbering,
where the first two digits are the station and the last two the unit type (2081 = Easton, ambulance).
Codes vary by agency, so check the list against what you hear and edit it in Excel or Notepad. Changes
apply without a restart, and labels update on calls already in the log.

| Column | What it does |
| --- | --- |
| `type` | `ten-code`, `signal`, `station` (two-digit station number) or `unit-suffix` (last two digits, e.g. `81-89`) |
| `code` | `10-23`, `Signal 33`, `20`, `81-89` |
| `meaning` | Shown as a label under the transcript |
| `applies_to` | `all`, `police`, `fire`, `ems`, `fire-ems`. When a code means different things to different agencies, the talkgroup name picks the row |
| `prompt` | `yes` adds the code to Whisper's hint list. Keep this to the 20 or so codes you hear most |

What happens to each call:

1. Codes marked `prompt` are sent to Whisper as hints, before your `vocabulary.txt` terms.
2. Spoken codes in the list are rewritten: "ten four" or "10 4" becomes `10-4`, "signal thirty three"
   becomes `Signal 33`. Bare four-digit numbers like `1023` are left alone, because they are usually
   call signs.
3. A four-digit 24-hour time within `time_window_minutes` (default 3) of the call, like "1346" said at
   1:46 PM, becomes `13:46`. Numbers after unit words ("Engine 1346") or before street words
   ("1346 Main St") are left alone.
4. The call log page shows labels such as `10-23 Arrived on scene`, `2081 Easton Fire/EMS, Ambulance` and
   `13:46 1:46 PM`. Whisper's original text is kept in the log as `transcript_raw`.

## Local streets and landmarks

`places.csv` helps Whisper with street names. It starts with a short hand-made list (`places.example.csv`).
Run **fetch-places.bat** once to replace it with every named road (including private drives in
complexes and plazas), numbered route, highway exit, named bridge and landmark between Easton and
Allentown from OpenStreetMap. Landmarks include hospitals, schools, colleges, fire and police stations,
malls, shopping centers, supermarkets and big stores, gas stations and convenience stores, restaurants,
banks, pharmacies, apartment and townhouse complexes, neighborhoods, industrial and business parks,
churches, cemeteries, parks, golf courses, stadiums, hotels and train stations. It takes a minute or two, and running it again later refreshes the list. If a download
gets cut off it is retried, then other public OpenStreetMap servers are tried. Downloads that worked are
kept for a day in `data\osm-cache`, so running it again after a failure only fetches what's missing.

| Column | What it does |
| --- | --- |
| `name` | How the place is written, e.g. `Northampton Street`, `Route 22`, `I-78` |
| `kind` | `street`, `route` or `landmark` |
| `municipality` | Town or township. A street in two towns has a row for each |
| `spoken` | Other ways it is said, separated by `\|`, e.g. `the Circle` for Centre Square |
| `prompt` | `yes` makes it a candidate for Whisper's hint list |
| `rank` | Bigger is more important (road size and length). Picks hints when there are too many |
| `osm_id`, `source` | Where the row came from. Rows you add with any `source` other than `osm` survive a re-fetch |

What happens to each call:

1. Whisper only reads about 200 words of hints, so each call gets the biggest roads and landmarks of the
   town in its talkgroup name ("Easton PD" gets Easton's streets), then the biggest roads overall. For
   talkgroups without a town in the name, list the towns under `[places.areas]` in `config.toml`.
2. Spoken street numbers and routes are written the way dispatch writes them: "twenty second street"
   becomes `22nd Street`, "route two forty eight" becomes `Route 248`, "interstate seventy eight"
   becomes `I-78`, "exit fifty one b" becomes `Exit 51B` (exits in the list only). "En route twenty
   minutes" is left alone.
3. Near-miss spellings right before a street word are fixed against the list: "Northamton Street"
   becomes `Northampton Street`. Short names (under five letters) and names already in the list are
   left alone, and a street in the talkgroup's own town wins a tie.

The script also writes `places-geo.json` (road lines, landmark points, town outlines) for the incident
map.

## Incident map

<http://localhost:3000/map> shows possible incidents on a street map, built from the transcripts. It
updates itself every 10 seconds. Click a dot for the address, the time, the tone-outs just before it, the
10-codes heard, and the calls with play buttons; the words that set the type ("MVA", "structure fire") are
highlighted. Colors: red fire, green EMS, blue police, orange MVA (motor vehicle accident: "MVA",
"M.V.A.", "MVC", "rollover", ...), gray unknown. The type comes from the talkgroup name, then words in the
calls, then a tone-out just before.

* **Time and type filters**: last hour, 6 hours, today, 24 hours, this week (since Sunday) or 30 days, and
  tick boxes for Fire, EMS, Police, MVA and Other. Both are remembered by the browser.
* **New incidents** pulse and have a NEW badge; the browser tab shows how many, e.g. "(2) Incident map".
  Opening one, or **Mark all seen**, clears it.
* SDRTrunk's **Transcripts** tab has the same list under **Incidents**, with the same filters and an
  **Open Map** button.

It needs the street list: run **fetch-places.bat** once (see above). Calls logged before that are mapped
as soon as the list exists.

What happens to each transcribed call:

1. **Find a place.** The transcript is checked against the street list for a house address
   ("1234 Northampton Street", "the 1200 block of Northampton St"), an intersection ("North 3rd and
   Northampton", "Route 22 at Freemansburg Ave", "Main Street between 3rd and 4th") or a landmark
   ("Easton Hospital"). Unit numbers aren't house numbers ("Engine 4 Route 22", "Medic 2081 Main St").
   "In Nazareth", "Palmer Township" or a town in the talkgroup name picks the town when a street name
   exists in several.
2. **Put it on the map.** Intersections and landmarks are placed offline from `places-geo.json`. House
   addresses are looked up with OpenStreetMap's free search, at most one a second, and every answer is
   saved in `calls.db` so nothing is looked up twice. Answers that aren't on the named street are
   ignored. With `online_lookup = false`, an address on a short street (under 2 km) is placed at the
   middle of the street and marked approximate; others are skipped.
3. **Group into incidents.** Calls naming places within 500 m of each other within 45 minutes are one
   incident. A house address heard later moves the dot from the intersection to the house.
4. **Follow-up calls.** A call that names no place but names a unit ("Engine 2011 on scene", "2081 on
   location") joins the most recent incident in the last 45 minutes that the same unit was in. A radio's
   talker alias (e.g. `ENGINE 41`) counts too. Four-digit call signs match with or without the unit word.

Radio speech is messy, so some calls are missed and some land in the wrong place. Each call in a popup
shows its transcript and how it was placed (house address, intersection, landmark, or street only), and
**Remove from map** hides a wrong one ("Show removed" brings it back).

After editing `places.csv` by hand, map earlier calls again with (from the same PC):
`curl -X POST http://localhost:3000/api/map/rebuild`

Settings are under `[map]` in `config.toml`. The map page loads Leaflet and the map tiles from the
internet; map data is © OpenStreetMap contributors.

## Fire and EMS tone-outs

SDRTrunk's Two-Tone Paging decoder (turn it on under a channel's Additional Decoders) sends each tone-out
page here. The call log shows them between the calls, for example "Tone out: Station 4 Engine,
349.0 Hz + 832.5 Hz", usually just above the dispatcher's call. Pages whose tones aren't on SDRTrunk's
station list say "not on the station list"; add their tones to `paging_tones.csv` in the SDRTrunk
folder (User Preferences, Decoder, Two-Tone Paging, Open) with the name the dispatcher called.

SDRTrunk posts JSON to `/api/page`. Nothing needs setting up when both run on the same computer. If the
transcriber runs on a different computer, enter its address and your API key in SDRTrunk's Two-Tone
Paging preferences.

## Phone alerts

The transcriber can buzz your phone for a crash (MVA), a structure fire, or a tone-out for stations you
pick, for example "Structure fire / North 3rd Street & Northampton Street, Easton". Tapping the alert
opens the incident map. It uses the free **ntfy** phone app, but the transcriber answers the app itself,
so the alert text goes only from your PC to your phone. **Tailscale** (free) links the phone to the PC
over an encrypted connection, so this works away from home too.

iPhones can only be woken up through Apple. For each alert the transcriber sends a wake-up ping holding
nothing but a random id through ntfy.sh to Apple, and the iPhone then fetches the alert from your PC.
Set `wakeup_url = ""` to turn the ping off; an iPhone then shows alerts only when the app is opened.

Setting it up, once:

1. Install Tailscale on the PC (<https://tailscale.com/download>) and on the phone (App Store), and sign
   in to both with the same account. Leave Tailscale connected on the phone.
2. On the PC, open Command Prompt and run `tailscale serve --bg 3000`. The first time, it shows a link
   to turn on HTTPS for your Tailscale network: open it, approve, and run the command again. It prints
   this PC's address, like `https://desktop-j74k3qe.tail1234.ts.net`. Only your own Tailscale devices
   can open it.
3. In `config.toml`, add (or edit) the `[alerts]` section from `config.example.toml`: `enabled = true`,
   `base_url` set to that address, and the stations you want in `stations`. Restart the transcriber.
4. On the phone, install **ntfy** from the App Store, tap **+**, turn on **Use another server**, enter the
   same address, and enter the topic name `sdrtrunk-alerts`. Allow notifications when it asks.
5. Double-click **send-test-alert.bat** on the PC. The phone should buzz within a few seconds.

### Bark instead (iPhone)

If ntfy won't buzz the phone, the free **Bark** app can deliver the same alerts. Bark's server passes each
alert to Apple, so the transcriber encrypts it first with a key only your phone knows: Bark and Apple
see only scrambled text.

1. Install **Bark** from the App Store and allow notifications. Its home page shows a push address like
   `https://api.day.app/AbCdEf123/...`; the part after `api.day.app/` is your device key.
2. In Bark, open **Push Encryption** (encryption settings), choose **AES256** and **GCM**, and type or
   paste a 32-character key (letters and digits). Tap Done.
3. In `config.toml` under `[alerts]`, set `bark_key` to the device key and `bark_encryption_key` to the
   same 32-character key. `enabled = true` must be set too. Restart the transcriber.
4. Run **send-test-alert.bat**.

This needs the `cryptography` package; run **install.bat** again after updating if it is missing.

What alerts:

* An incident on the map whose type is in `incidents` (MVA and structure fire to start), once per
  incident, as soon as a call about it has an address. Calls with no address heard don't alert.
* A tone-out whose station name (from SDRTrunk's `paging_tones.csv`) contains one of `stations`, for
  example `stations = ["Station 4", "Easton"]`. The same station again within two minutes is skipped.
* Nothing older than `max_age_minutes`, so old calls mapped after a restart stay quiet.

## Fixing transcripts

Each call on the call log page has an **Edit** button. Type the right words and press Save. The words
you replaced are learned (for example "never mind" -> "Nevin"), fixed in every later call, and given to
Whisper as spelling hints. The page shows what it learned, with an Undo for each. Single everyday words
("the", "yes"), added or removed words, and changed numbers are treated as one-off fixes and not learned.

Everything learned is in `corrections.csv` (columns heard, correct, talkgroup, hint, count, source). You
can edit it in Excel or Notepad, and changes apply without a restart. Add a talkgroup to a row to apply it
only on talkgroups whose name contains that text. Set hint to "no" to keep a word out of Whisper's hints.

To check whether changes help, run **compare-transcripts.bat**. It runs the 50 newest calls, plus every
call you fixed by hand, through Whisper both the old way (no cleanup) and the new way. Then it opens a
side-by-side report that scores both against your fixes. The transcriber can keep running while it works.

## Accuracy tips

* Whisper invents words on noise. `junk_filter` drops a whole call of "Thank you." heard with low
  confidence, cuts loops ("10-4, 10-4, 10-4...") to one copy, and removes counting runs and echoes of
  the hint list.
* Keep vocabulary.txt to your own names. Whisper repeats hint words back on unclear audio, and the old
  sample terms ("Engine 12", "Main Street") are now ignored for that reason.
* Whisper invents words on silence. The service drops calls shorter than `min_duration_s`, runs voice
  activity filtering, discards segments Whisper flags as non-speech, and treats each call on its own.
* Each call is cleaned before Whisper hears it (`[audio]` in config.toml): only the voice band is kept,
  steady tones such as paging tones are cut out, and the speech is brought to a standard level so quiet
  channels transcribe as well as loud ones. The saved recording is not changed. The log line for a call
  says how many seconds of tones were cut.
* A good vocabulary list helps more than any other setting with unit and street names.
* Clean decoding matters most: transcripts are only as good as the audio SDRTrunk produces.

## Development

```
pip install -r requirements.txt pytest httpx
python -m pytest tests
```

The tests post uploads laid out byte for byte the way SDRTrunk's `RdioScannerBuilder` builds them, and
use stand-ins for both Whisper backends, so they run without a GPU or a model download.
