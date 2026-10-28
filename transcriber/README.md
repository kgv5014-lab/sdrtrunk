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
3. Nothing to set for the graphics card: start-transcriber.bat finds the GTX 1070 Ti by name (through
   `nvidia-smi -L`) and gives Whisper only that card, leaving the 1070 for Ollama. The window shows
   `Whisper uses the GTX 1070 Ti: GPU-...` when it works. For another card, set `WHISPER_GPU_NAME` at the
   top of use-whisper-gpu.bat. Don't rely on card numbers: Windows tools and CUDA can number the two
   cards differently, which once put Whisper on the 1070 next to Ollama.
4. Optional: open **config.toml** in Notepad to change other settings.
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

## Updating SDRTrunk and the transcriber

Double-click **update-sdrtrunk-plus.bat** in the sdrtrunk code folder (on this PC
`E:\SDR-Stuff\FreshSDRTrunk\BUILD\sdrtrunk`). It waits until SDRTrunk and the transcriber are closed (type C
to have it close them), gets the latest code from GitHub, builds SDRTrunk and copies the new transcriber
files into `E:\SDR-Stuff\sdrtrunk-transcriber`. It never touches config.toml, vocabulary.txt, codes.csv,
corrections.csv, places.csv, places-geo.json, paging_tones.csv or the data, ab-samples and blank-samples folders.
If the build fails, the old SDRTrunk is put back and the build log is in `update-log.txt` next to the code
folder. The previous SDRTrunk stays in `sdrtrunk-previous-image` until the next update. For a different
transcriber folder, run `update-sdrtrunk-plus.bat -TranscriberDir "D:\my-transcriber"`.

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
| `type` | `ten-code`, `signal`, `station` (two-digit station number), `unit-suffix` (last two digits, e.g. `81-89`), `police-esn` (two-digit police department number), `call-type` or `acronym` |
| `code` | `10-23`, `Signal 33`, `20`, `81-89`, `ALS` (call types and acronyms match as written, capitals included) |
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
   ("1346 Main St") are left alone. Whisper writes the time dispatch reads at the end of a call many
   ways ("14-16", "14, 11", "13.43 hours", "Ops 113-30"); these become `14:16` and so on too.
4. The call log page shows labels such as `10-23 Arrived on scene`, `2081 Easton Fire/EMS, Ambulance`,
   `EMS 15 Hanover Twp Fire/EMS`, `ALS Advanced life support call` and `13:46 1:46 PM`. Call signs
   written with a dash (`15-82`) count too. On police talkgroups, three- and four-digit numbers are read
   as police cars instead (`2741 Palmer Twp PD, car 41`). Whisper's original text is kept in the log as `transcript_raw`.

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
* **Dashboard layout**: incidents on the left, the map in the middle, and **Live calls** on the right. Live
  calls shows the newest transcripts and tone-outs as they come in, with Play, **Edit** (the same as on the
  call log: replaced words are fixed in later calls too) and **Show on map** for calls in an incident. Calls
  in map popups have Edit too. On a phone or narrow window the three parts are tabs.
* Popups never get taller than the map; long incidents scroll inside, so the close button stays in view.
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
5. **Tac channels.** A tone-out usually ends with the channel ("Lower Saucon Communications, on TAC 2,
   16:32"), and the popup shows it ("Working on Tac 2"). Calls on that Tac talkgroup then join the
   incident for up to an hour after its latest call, even with no place in them. Whisper's "Tag 2",
   "Pac-1" and "TAC 3-634" count. The latest incident sent to a channel gets its calls. Ops channels are
   shown but not followed, since they carry many incidents at once (`follow_channels` under `[map]`).
   When every unit is done ("all the units are available") the channel is free again.
6. **Other calls with no place** join an incident when they name its street within 15 minutes ("staging
   on Northampton Street"). A unit that went available, or was sent to another call ("1786, you are
   responding..."), stops following its earlier incident. A new dispatch never joins by these rules; one
   more than 100 m away, or to the same place more than 20 minutes after the last call, starts a new
   incident. The call type comes from the dispatch first, so a later call mentioning a crash doesn't turn
   a fire alarm into an MVA.
7. **Police, fire and EMS sent to one scene.** Each dispatcher words the place a little differently, so a
   dispatch that brings a service the incident doesn't have yet (police to a fire, an ambulance to a
   fight) joins it within 15 minutes of its latest call: within 500 m, anywhere along the same street
   (up to 2 km), or, if its place couldn't be found, when it names the incident's street. The call types
   must go together: a theft and a cardiac arrest a block apart stay two incidents. The service comes
   from the talkgroup, or on a shared "Fire / EMS" talkgroup from the call (an ambulance or ALS/BLS is
   EMS, an engine is fire). The map shows Police / Fire / EMS tags on each incident, and "Several
   services" shows only incidents more than one service was sent to.
8. **Made-up transcripts are skipped.** On unclear audio Whisper often reads its hint words back
   ("Dispatch, Engine 12, Main Street") or repeats a phrase over and over. Those calls stay off the map.

Radio speech is messy, so some calls are missed and some land in the wrong place. Each call in a popup
shows its transcript and how it was placed (house address, intersection, landmark, or street only), and
**Remove from map** hides a wrong one ("Show removed" brings it back).

**Fixing it by hand.** Under each call in a popup: **Move to…** puts it in another incident nearby in
time, **Own incident** takes it out into an incident of its own, and **Not an incident** takes it off the
map. **Merge into…** at the bottom joins two incidents. Fixes are kept when the map is rebuilt.

After editing `places.csv` by hand, map earlier calls again with (from the same PC):
`curl -X POST http://localhost:3000/api/map/rebuild`

Settings are under `[map]` in `config.toml`. The map page loads Leaflet and the map tiles from the
internet; map data is © OpenStreetMap contributors.

### Helicopters from ADS-B

If an ADS-B receiver (readsb, tar1090 or ultrafeeder) runs on your network, the map can show helicopters
with a short trail, and mark one coming down low near a recent incident as landing there ("🚁 LIFE12
landing" on the incident). Air ambulance operators (Geisinger Life Flight, Air Methods, PHI and others)
are outlined in red. In `config.toml`:

```toml
[aircraft]
enabled = true
url = "http://192.168.50.102:8080/data/aircraft.json"
# Name aircraft you know, or show only these with all_helicopters = false
watch = [{match = "N123AB", name = "St. Luke's 1"}]
# Also send a phone alert when a helicopter lands near an incident
alert_landing = true
```

The service reads the receiver, so the map works the same from your phone over Tailscale.

### PennDOT traffic and cameras

The map can show PennDOT's crashes, road closures and roadwork from the public 511PA website (no sign-up),
and the traffic cameras in the map area. Click a camera for its picture, refreshed every 5 seconds;
"Live video on 511PA" opens 511PA's map on that camera in a new tab (press Show Video there), since
PennDOT only plays video on its own site. A radio incident near a PennDOT crash or closure shows it ("🚦 PennDOT:
Multi vehicle crash · US 22"), and incident popups list the closest cameras. The three layers are turned
on and off in the box at the top right of the map. In `config.toml`:

```toml
[traffic]
enabled = true
```

Cameras are only shown live: PennDOT doesn't allow recording them, so nothing is saved. Data is © PennDOT.

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

### Watch zones

A watch zone alerts for **any** call near a spot, whatever the call is about. Add one under `[alerts]`:

```toml
zones = [{name = "Home", lat = 40.6915, lon = -75.2100, radius_miles = 0.5, prefix = "DRONE LAUNCH REQUESTED"}]
```

A call alerts when the map places it, or the incident it joins, inside the circle. The alert reads like
"DRONE LAUNCH REQUESTED - MVA" with the place and what was said, once per incident. A call that only
names a road ("Concord Circle", with no house number or cross street) alerts when that whole road is
inside the circle, since a long road passing through could mean anywhere along it. A store, school or
other landmark from the street list counts the same way when every one of that name is inside the circle
(a chain with branches elsewhere only counts when the call names the town). Instead of `lat` and `lon`
you can give `address = "123 Main Street, Easton"`, looked up online once. When the transcriber starts,
its log lists the roads it found within each zone.

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

## Signal quality per dongle

At the top of every hour SDRTrunk adds up how each dongle did and writes it in three places:

- The SDRTrunk log, one line per dongle and one per channel, for example
  `R820T 00000011: drift +0.42 ppm, channels off by 12 Hz, USB errors 0` and
  `Control South 852.5500 MHz P25-1: 97.1% of 41,203 messages decoded, lost sync 3 s, signal -42.0 dB`.
  Plain FM channels show their signal on calls and the background noise instead, since they have no error check.
- `signal_quality.csv` in SDRTrunk's logs folder, one row per channel per hour, to open in Excel.
- The **Last hour** column of SDRTrunk's Tuners tab, like `97% decoded, +0.42 ppm`.

SDRTrunk also sends the numbers to this service (the same address two-tone pages go to, ending in
`/api/signal`). Once a dongle has about six hours of history, a phone alert goes out when it gets worse than its
own normal: fewer messages decoding, the control channel dropping out, a weaker signal, more noise, frequency
drift, USB errors, or the dongle disappearing. Each problem alerts once, and again only if it clears and comes
back. The limits are in `[signal]` in `config.toml`; `alert = false` keeps the numbers without alerts.

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

### Hints learned from your calls

Whisper also gets street and unit names learned from the call log, so nothing needs to be kept up by hand:

- **Going on now:** the streets, landmarks and units of incidents with a call on the map in the last hour.
  A follow-up call like "2011 on location, Northampton Street" then comes out spelled the way dispatch
  said it.
- **Most heard:** the places found most often in each talkgroup's calls over the last 30 days, and the
  unit call signs said most often (three times or more). These are better hints than the biggest roads,
  many of which hardly ever come up on the radio.

They take about 160 characters of the hint list. Whisper only reads about 600, so when the list is full
the biggest-roads list and the 10-codes are dropped first (spoken codes are still written as 10-4 after
Whisper). Your vocabulary.txt is never dropped for them. Settings are under `[hints]` in config.toml;
`enabled = false` turns them off. To see whether they help, run **compare-hints.bat**: it runs the same
calls without and with the learned hints and scores both against your fixes.

## Call summaries

Ollama, a free program that runs AI language models on your own PC, can write a short summary for each
incident on the map, for example "Two-car MVA at Route 22 and 25th Street, one patient, Medic 2081
transporting BLS", plus tags such as *injuries* or *road blocked* and the latest status (active, under
control, cleared, canceled). The summary is written again a little while after each new call joins the
incident. Long calls (25 words or more) also get a one-line summary on the call log. Summaries show on the
map, on the call log, and in SDRTrunk's Incidents tab, marked **AI**. Nothing leaves your PC.

The summaries come from the transcripts, so they are only as good as the transcripts. They never change
how calls are grouped into incidents.

Setting it up, once:

1. Install Ollama for Windows from <https://ollama.com/download>.
2. Double-click **start-ollama.bat** in the transcriber folder. It finds the GTX 1070 by its UUID
   (start-transcriber.bat does the same for the 1070 Ti, so the two never share a card), starts Ollama on it in a minimized window, and the first time downloads the model
   (`qwen2.5:7b`, about 5 GB). It also turns off Ollama's Vulkan support, which would otherwise see the 1070 Ti too, and closes the Ollama tray app, which would otherwise use the wrong card.
3. In `config.toml`, add the `[summaries]` section from `config.example.toml` and set `enabled = true`.
   Restart the transcriber. The log says "Summaries: using Ollama" after the first one.

Run start-ollama.bat before start-transcriber.bat each time, for example from the batch file that opens
everything. If Ollama isn't running, everything else works as before; the log says so once, and the
summaries catch up within a minute of Ollama starting. On start the transcriber also summarizes the last
24 hours of incidents that have none yet.

To check that Ollama is on the 1070, run `nvidia-smi` while a summary is being written: `ollama.exe` should
be listed on the GTX 1070 with about 5 GB of memory.

### Tone-outs on P25 talkgroups

SDRTrunk's Two-Tone Paging decoder only hears analog channels. When the tones go out on a P25 (digital)
talkgroup, the transcriber finds them in the call recordings instead: tone A for 0.3 to 5 seconds, then
a different tone B, or one tone of 6 seconds or more. They are logged with the other tone-outs and can
send phone alerts (`stations` under `[alerts]`).

Name them in `paging_tones.csv` next to `config.toml` (same format as SDRTrunk's list:
`tone_a,tone_b,name`). Unnamed tone-outs show their tones, e.g. "349.0 Hz + 832.5 Hz"; copy those numbers
into the file with the name the dispatcher calls right after the tones. Changes apply without a
restart. Settings are under `[tones]`; `talkgroups` limits the search to dispatch talkgroups.

Unnamed tones also get named by themselves (`learn = true`). Every few minutes the transcriber looks at
the last `learn_days` of unnamed tone-outs and at what dispatch said on that talkgroup from the tones
until 90 seconds later ("EMS 27", "Fire 54", "Station 20", or a call sign like "Medic 2781"). When the
same station was named after more than `learn_share` (half) of a tone pair's tone-outs that name a
station, more often than any other, and at least `learn_min_seen` times, the pair is added to `paging_tones.csv` with a "# Learned" note above it, and its
tone-outs already logged get the name. When the service varies ("Fire 20", then "EMS 20") the name is
"Station 20". Tones that start or end every fire call name a different station each time, so they stay
unnamed. Fix or delete a learned line like any other; it is not learned again while it's in the file.

## Accuracy tips

* Whisper invents words on noise. `junk_filter` drops a whole call of "Thank you." heard with low
  confidence, cuts loops ("10-4, 10-4, 10-4...") to one copy, and removes counting runs and echoes of
  the hint list.
* Whisper sometimes writes Korean, Chinese, Cyrillic or Arabic on noise, even with `language = "en"`.
  `english_only` blocks those characters while Whisper writes (faster-whisper) and removes any left
  afterwards; a call that is mostly another script is dropped.
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
