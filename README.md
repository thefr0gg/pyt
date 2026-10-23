# pyt
### formerly pytube

[![CI](https://github.com/ramsy0dev/pyt/actions/workflows/ci.yml/badge.svg)](https://github.com/ramsy0dev/pyt/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> **Heads-up:** SABR support is still under active development. Some videos
> won't download yet, others will be slow or 403 partway through. If a download
> fails, that's why. See [Why downloads break sometimes (SABR)](#why-downloads-break-sometimes-sabr)
> for the full story.

A maintained pytube fork. The original went unmaintained while YouTube's API
kept changing. Everything that was broken has been fixed, and a full
post-processing pipeline has been added so pyt can hold its own against yt-dlp
for YouTube-specific use cases.

---

## Features

| Category | What pyt does |
|---|---|
| **Download** | 144p – 8K, progressive & DASH, audio-only, auto-merge with ffmpeg |
| **Combined adaptive download** | One multiplexed SABR session for video+audio, post-merge duration validation |
| **Audio extraction** | Convert to mp3, m4a, flac, opus, vorbis, wav, aac, alac |
| **Metadata** | Embed title, artist, date, description (ffmpeg + mutagen) |
| **Thumbnails** | Embed cover art into mp4, m4a, mp3, ogg, flac |
| **Subtitles** | Embed SRT/VTT into mp4, mkv, webm; 100+ language codes |
| **SponsorBlock** | Mark segments as chapters or cut them out entirely |
| **Upscaling** *(experimental)* | Lanczos (real-time, no GPU) and Real-ESRGAN (neural, GPU recommended) |
| **Output templates** | `{author}/{upload_date:%Y-%m-%d} - {title}.{ext}` and more |
| **Archive tracking** | Skip already-downloaded videos across runs |
| **Batch processing** | Download from a file of URLs |
| **Authentication** | Netscape cookie files, browser cookie extraction |
| **Proxy / geo-bypass** | HTTP, HTTPS, SOCKS5 proxy; `X-Forwarded-For` spoofing |
| **Config files** | `~/.pyt.conf` for persistent defaults |
| **Playlists & channels** | Full playlist and channel support |
| **Doctor command** | Detect installed tools, auto-install ffmpeg / realesrgan-ncnn-vulkan |

### Stability tiers

| Tier | What's in it |
|---|---|
| **Stable** | All download, post-processing, playlist, search, cookie, proxy, archive features. CLI flags. The `pyt.Client` / `Video` / `Stream` / pipeline modern Python API. The doctor command. |
| **Deprecated** | `pyt.YouTube`, `pyt.Playlist`, `pyt.Channel`, `pyt.Search`, `register_on_*_callback`. Still works in 2.x; emits `DeprecationWarning`. Will be removed from the top-level namespace in v3 — `from pyt.legacy import ...` is the explicit-import path that survives. |
| **Experimental** | `pp.upscale(...)` — both `algorithm="lanczos"` and `algorithm="realesrgan"`. Emits `FutureWarning` on first use; API and defaults may change. |

---

## Install

```bash
pip install git+https://github.com/ramsy0dev/pyt
```

`mutagen` (richer MP3/ID3 tagging) and `browser-cookie3` (browser cookie
extraction) are installed automatically — they used to be optional extras,
but cookies in particular are now load-bearing for the SABR workaround so
they're part of the base install.

ffmpeg is required for post-processing (audio conversion, metadata/thumbnail/subtitle
embedding, SponsorBlock). Get it at https://ffmpeg.org/download.html — or run
`pyt --doctor --install ffmpeg` to drop it into `~/.pyt/bin/` automatically.

### `pyt --doctor` — what's installed and what works

```bash
$ pyt --doctor
pyt doctor - linux/x86_64, python 3.13
Managed bin dir: /home/you/.pyt/bin

Tools
------------------------------------------------------------
  [OK]   ffmpeg         ffmpeg version 7.1
         path:    /usr/bin/ffmpeg
         used by: muxing, audio extraction, post-processing, upscale re-encode
  [OK]   ffprobe        ffprobe version 7.1
         path:    /usr/bin/ffprobe
         used by: duration / fps / audio detection (ships with ffmpeg)
  [--]   realesrgan     not installed  (run: pyt --doctor --install realesrgan)
         used by: Real-ESRGAN neural upscaler (optional, for pp.upscale algorithm='realesrgan')

Features
------------------------------------------------------------
  [OK]   Stream download (SABR + byte-range)
  [OK]   Audio extraction / format conversion
  [OK]   Combined video+audio merge (CombinedDownload)
  [OK]   Metadata / thumbnail / subtitle embedding
  [OK]   SponsorBlock chapter marking
  [OK]   Upscale (algorithm='lanczos')
  [--]   Upscale (algorithm='realesrgan')  (needs: realesrgan)
```

Install missing binaries on the user's behalf — they go in
`~/.pyt/bin/`, which pyt prepends to its own `PATH` at startup so it
finds them without polluting your shell:

```bash
pyt --doctor --install ffmpeg                # Windows / Linux only (use Homebrew on macOS)
pyt --doctor --install realesrgan            # all platforms
pyt --doctor --install po-token-generator    # needs Node 18+ on PATH
pyt --doctor --install all
```

Sources:

| Tool | Windows | Linux | macOS |
|---|---|---|---|
| ffmpeg | [gyan.dev essentials build](https://www.gyan.dev/ffmpeg/builds/) | [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases) | use `brew install ffmpeg` |
| realesrgan-ncnn-vulkan | [xinntao/Real-ESRGAN releases](https://github.com/xinntao/Real-ESRGAN/releases) | same | same |
| po-token-generator | `npm install bgutils-js youtubei.js` into `~/.pyt/js/` + wrapper at `~/.pyt/bin/pyt-po-token` | same | same |

---

## Quick start — CLI

```bash
# Best quality (auto-merges video + audio with ffmpeg)
pyt "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Extract audio as MP3 with metadata and cover art
pyt <url> -x --audio-format mp3 --embed-metadata --embed-thumbnail

# Custom output path
pyt <url> -o "{author}/{upload_date:%Y-%m-%d} - {title}.{ext}" -t ~/Downloads

# SponsorBlock chapters (no re-encode)
pyt <url> --sponsorblock-mark sponsor,intro,outro

# Batch download with archive (skips already downloaded)
pyt --batch-file urls.txt --download-archive archive.txt --sleep-interval 2

# Playlist
pyt "https://www.youtube.com/playlist?list=PLxxxxxxxx"

# Behind a proxy
pyt <url> --proxy socks5://127.0.0.1:9050

# Cookies from browser
pyt <url> --cookies-from-browser firefox

# Check what's installed and what works
pyt --doctor
```

---

## Quick start — Python API

`pyt.Client` is the modern entry point. Construct it once, then use it
to fetch videos and queue downloads.

```python
from pyt import Client

client = Client()
video = client.video("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

print(video.title)            # str
print(video.length)           # datetime.timedelta
print(video.author.name)      # str

# Highest progressive quality (video + audio in one file)
video.streams.progressive.best().download_to("downloads/").run()

# Audio only — .best() raises NoMatchingStream if nothing matches
video.streams.audio.best().download_to("downloads/").run()

# Specific resolution
video.streams.video.filter(resolution="1080p").best().download_to().run()

# Best adaptive video + audio merged in one go (one SABR session, ffmpeg merge)
video.download_best("downloads/").run()
video.download_best("downloads/", prefer_resolution="1080p").run()
```

`Client` accepts `proxy=`, `cookies=`, `cookies_from_browser=`, `po_token=`,
`po_token_provider=` / `po_token_cmd=` / `po_token_script=` for dynamic
generators (see [docs/features/po-token.md](docs/features/po-token.md)),
`use_oauth=`, plus `on_progress` / `on_complete` callbacks. The session
state lives on the client — there are no module-level globals you need
to reason about across instances.

### Combined adaptive download (SABR multi-format)

`video.download_best(...)` runs both the video-only and audio streams
through **one** multiplexed SABR session and merges the result with
ffmpeg. The single-stream path opens an independent `SabrSession` per
stream, which YouTube's server treats as two separate users — each with
its own throttle decisions, ad-enforcement context, and playback cookie.
Multiplexing is what real players do; this matches them.

```python
from pyt import Client, pipeline as pp

video = Client().video(url)

# Auto-pick best video + audio, merge, then attach metadata
path = (
    video.download_best("downloads/")
        | pp.embed_metadata()
        | pp.embed_thumbnail()
).run()
```

If SABR can't deliver every byte for either format (throttle, 403,
session expiry), the missing tail is finished via byte-range request —
a 99%-complete download won't die from one bad SABR exchange. Use
`prefer_resolution=` to cap quality (e.g. `"1080p"` on a 4K source).

### Upscaling (experimental)

`pp.upscale(...)` runs the downloaded video through one of two
upscalers — pick `algorithm=` based on what hardware you have.

| | `algorithm="lanczos"` (default) | `algorithm="realesrgan"` |
|---|---|---|
| Method | ffmpeg's Lanczos resize + light unsharp pass | Real-ESRGAN neural network |
| Adds detail? | No (cleaner interpolation) | Yes (model-hallucinated) |
| Speed | Real-time on any CPU | GPU-bound; ~1 hour per minute of 720p without a GPU |
| Peak disk (5-min 720p) | A few hundred MB (single ffmpeg pass) | ~6 GB chunked (default), ~45 GB unchunked |
| Extra install | None (ffmpeg already required) | `realesrgan-ncnn-vulkan` binary on `PATH` |
| Best at | 2× (360→720, 720→1440) | 4× when you have the hardware |

**Lanczos — the default, works on any machine.** Single ffmpeg
invocation: `scale=iw*N:ih*N:flags=lanczos` followed by a conservative
`unsharp` pass to recover the edge sharpness Lanczos blurs. It doesn't
add detail the source doesn't have, but it produces a noticeably
cleaner result than naive bilinear or browser-side player upscaling,
and it runs in real-time. **Use this for the typical 360→720 / 720→1440
case.**

```python
from pyt import Client, pipeline as pp

video = Client().video(url)
path = (
    video.download_best("downloads/", prefer_resolution="720p")
        | pp.upscale(scale=2)
).run()
```

**Real-ESRGAN — opt-in for users with GPUs.** Actual neural-net
super-resolution that recovers detail (within reason). Install the
`realesrgan-ncnn-vulkan` binary from
[xinntao/Real-ESRGAN releases](https://github.com/xinntao/Real-ESRGAN/releases)
and put it on `PATH` (~50 MB binary, replaces a 2 GB torch+basicsr
install).

The pipeline processes the video in **N-second chunks** (default 30s)
so peak disk usage is bounded by chunk size, not video length:

1. Extract one chunk's frames as PNG
2. Upscale them with Real-ESRGAN
3. Re-encode that chunk to a small mp4
4. Drop both PNG dirs
5. (After all chunks) ffmpeg concat (no re-encode) + remux original audio

Concrete numbers for a 5-minute 720p × 4× upscale:

| | Default chunked (30s) | Unchunked (`chunk_seconds=0`) |
|---|---|---|
| Peak intermediate disk | **~6 GB** | ~45 GB |
| Wall-clock | GPU-bound — ~same as unchunked | GPU-bound |
| Final output size | identical | identical |

```python
path = (
    video.download_best("downloads/", prefer_resolution="720p")
        | pp.upscale(scale=4, algorithm="realesrgan")     # default chunk_seconds=30
        | pp.embed_metadata()
).run()

# Tighter disk budget? Smaller chunks.
| pp.upscale(scale=4, algorithm="realesrgan", chunk_seconds=10)

# Beefy GPU sitting idle? Push more parallel inference batches.
| pp.upscale(scale=4, algorithm="realesrgan", threads="1:4:1")
```

Wall-clock is dominated by per-frame inference, which is GPU-bound;
chunking doesn't speed that up, it just keeps you from running out of
disk. Without a GPU, Real-ESRGAN is effectively unusable on anything
longer than a few minutes regardless of chunk size — use lanczos
instead.

**Common arguments**

| Argument | Default | Notes |
|---|---|---|
| `scale` | `2` | 2, 3, or 4 |
| `algorithm` | `"lanczos"` | or `"realesrgan"` |

**Lanczos-specific**

| Argument | Default | Notes |
|---|---|---|
| `crf` | `18` | x264 CRF for the re-encode (lower = larger / higher quality; 18 is "visually lossless") |
| `preset` | `"medium"` | x264 speed/efficiency preset; `"slow"` trades CPU for ~10% smaller output |
| `sharpen` | `0.4` | unsharp amount, 0.0–1.5; `0` disables the sharpen pass |

**Real-ESRGAN-specific**

| Argument | Default | Notes |
|---|---|---|
| `model` | `"realesrgan-x4plus"` | also `realesrgan-x4plus-anime`, `realesr-animevideov3` |
| `binary` | auto-detected on `PATH` | explicit path override |
| `tile_size` | `0` (auto) | lower (e.g. 64, 128) if you hit GPU memory errors |
| `chunk_seconds` | `30` | size of each processing window in seconds; `0` disables chunking |
| `threads` | `None` (binary picks) | passes through to `realesrgan-ncnn-vulkan -j load:proc:save` |
| `keep_intermediate` | `False` | keep the extracted PNG frames for debugging |

A `FutureWarning` is emitted on first use to make the experimental
status loud. Filter it with
`warnings.filterwarnings("ignore", category=FutureWarning, module=r"pyt\..*")`
once you've read this section.

### Post-processing — declarative pipeline

```python
from pyt import Client, pipeline as pp

client = Client()
video = client.video(url)
stream = video.streams.audio.order_by("abr").desc().best()

path = (
    stream.download_to("downloads/")
        | pp.sponsorblock(mark=["sponsor", "intro"])
        | pp.embed_metadata()
        | pp.embed_thumbnail()
        | pp.extract_audio("mp3")
).run()
```

Steps run in order. Failures raise `PostProcessError` with `step=` and
`partial_output_path=` set, so you can recover or report cleanly.

### Logging & diagnostics

pyt is silent by default. Turn it on when you need it:

```python
import pyt

pyt.enable_logging("DEBUG")              # filter chains, picks, timings, retries
pyt.enable_logging("TRACE")              # everything, incl. per-chunk SABR
pyt.enable_logging(file="/tmp/pyt.log")  # also write to file (good for bug reports)
pyt.set_log_level("INFO")                # adjust mid-run
pyt.disable_logging()
```

Or set `PYT_LOG_LEVEL=DEBUG` in your environment to enable logging
without touching the code that uses pyt.

For bug reports, `pyt.diagnostic_report()` returns a self-contained
text block with version / platform / installed-tool state — paste it
into a GitHub issue. It does **not** include URLs, video IDs, or any
user content.

See [docs/features/logging.md](docs/features/logging.md) for the full
reference (levels, formats, integration with your own logging setup).

### Errors

Every modern API failure inherits from `pyt.PytError`:

| Exception | When |
|---|---|
| `VideoUnavailable` | private, removed, region-blocked, members-only |
| `AgeRestricted` | tier-3 age gate (needs OAuth) |
| `LiveStreamNotSupported` | URL is a live stream |
| `AttestationRequired` | YouTube wants a PO token (see [docs/features/po-token.md](docs/features/po-token.md)) |
| `NoMatchingStream` | `.best()` / `.one()` saw an empty filter chain |
| `DownloadError` | byte transfer failed (network, 403, SABR exhaustion) |
| `PostProcessError` | a pipeline step failed |
| `ConfigError` | invalid `Client(...)` argument |

### Migrating from the legacy API

The original `YouTube` / `Playlist` / `Channel` / `Search` /
`StreamQuery` / `register_on_*_callback` classes now emit a
`DeprecationWarning`. They still work — the new API is a thin facade
over them — but new code should use `pyt.Client`.

| Old | New |
|---|---|
| `YouTube(url)` | `Client().video(url)` |
| `YouTube.from_id(vid)` | `Client().video(f"https://youtu.be/{vid}")` |
| `yt.streams.get_highest_resolution()` | `video.streams.progressive.best()` |
| `yt.streams.filter(only_audio=True).first()` | `video.streams.audio.best()` |
| `yt.streams.filter(res="1080p").first()` | `video.streams.video.filter(resolution="1080p").best()` |
| `yt.register_on_progress_callback(cb)` | `Client(on_progress=cb)` |
| `Playlist(url)` | `Client().playlist(url)` |
| `Channel(url)` | `Client().channel(url)` |
| `Search(query)` | `Client().search(query)` |
| Manual PP chain (`SponsorBlockPP(...).run(p, s, yt)` …) | `download \| pp.sponsorblock(...) \| pp.embed_metadata()` |
| `from pyt.exceptions import …` | `from pyt import PytError, VideoUnavailable, NoMatchingStream, …` |

Need something the new surface doesn't expose? Every wrapper has a
`.legacy` escape hatch (`video.legacy`, `stream.legacy`,
`playlist.legacy`) that returns the underlying old object.

To silence the deprecation warnings while you migrate, either filter
them in your code:

```python
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"pyt\..*")
```

…or import explicitly from the `pyt.legacy` namespace
(`from pyt.legacy import YouTube`) so it's grep-able which call sites
are still on the old surface.

The legacy classes are removed from the top-level `pyt` namespace
in v3; `pyt.legacy.*` will outlive that release by one more cycle to
keep migration windows reasonable. Throughout 2.x they continue to
work and only emit `DeprecationWarning`.

---

## CLI reference

```
pyt [url] [options]

Stream selection:
  -f, --ffmpeg [RES]       Merge best video + audio with ffmpeg (default: best)
  -r, --resolution RES     Download specific resolution (e.g. 1080p)
  -a, --audio [FMT]        Download audio-only stream
  --itag N                 Download by itag number
  -l, --list               List available streams

Post-processing:
  -x, --extract-audio      Extract audio track
  --audio-format FMT       Target format: mp3 m4a aac flac opus vorbis wav alac
  --embed-metadata         Embed title, artist, date, description
  --embed-thumbnail        Embed cover art
  --embed-subs             Embed subtitle track
  --sponsorblock-mark CATS Mark SponsorBlock segments as chapters
  --sponsorblock-remove CATS  Cut SponsorBlock segments out

Output:
  -o, --output TEMPLATE    Filename template, e.g. "{author}/{title}.{ext}"
  -t, --target DIR         Download directory (default: current dir)
  -j, --dump-json          Print video info as JSON, don't download

Captions:
  -c, --caption-code LANG  Download / embed caption language (e.g. en)
  --list-captions          List available caption tracks

Network:
  --proxy URL              HTTP/HTTPS/SOCKS5 proxy URL
  --cookies FILE           Netscape cookie file
  --cookies-from-browser B Extract cookies from browser (chrome firefox brave edge safari)
  --geo-bypass             Spoof X-Forwarded-For with a random IP
  --geo-bypass-country CC  Use a specific country code (e.g. US)

PO token (for ATTESTATION_REQUIRED — see docs/features/po-token.md):
  --po-token TOKEN         Static base64url token (extract from browser DevTools)
  --po-token-cmd CMD       Command that prints a token to stdout
  --po-token-script FILE   JS file run with node / bun / deno

Batch:
  --batch-file FILE        File of URLs to download (one per line)
  --download-archive FILE  Record downloaded IDs; skip if already present
  --sleep-interval N       Sleep N seconds between downloads
  --max-sleep-interval N   Random sleep between N and max-N seconds

Debug:
  -v, --verbose            Increase log verbosity (-v=DEBUG, -vv=TRACE)
  --logfile FILE           Also write logs to a file (great for bug reports)
  --build-playback-report  Dump playback report to file

Setup:
  --doctor                 Report installed tools and available features
  --doctor --install TOOL  Install a tool to ~/.pyt/bin (ffmpeg, realesrgan, all)
```

---

## Config file

Store defaults in `~/.pyt.conf` (also checked: `./pyt.conf`,
`~/.config/pyt/pyt.conf`):

```ini
[default]
target = ~/Downloads
output = {author}/{title}.{ext}
embed_metadata = true
embed_thumbnail = true
sponsorblock_mark = sponsor,intro,outro
sleep_interval = 2
```

CLI flags always override config values.

---

## Playlists, channels and search

```python
from pyt import Client

client = Client()

# Playlist — lazy iteration, one HTTP fetch per video
playlist = client.playlist("https://www.youtube.com/playlist?list=PLxxxxxxxx")
print(playlist.title, len(playlist))
for video in playlist:
    video.streams.progressive.best().download_to("downloads/").run()

# Channel feed
channel = client.channel("https://www.youtube.com/@somechannel")
print(channel.name)

# Search
results = client.search("python tutorial")
for video in results.videos:
    print(video.title, video.url)
```

---

## Why downloads break sometimes (SABR)

YouTube has been rolling out SABR ("Server Adaptive Bitrate Request") for a
while now. Instead of handing your player a signed CDN URL and letting it pull
bytes with a normal `Range:` request, the server hands you a `serverAbrStreamingUrl`
and expects you to POST a protobuf describing what you've already buffered, on
which it streams back a binary UMP response with the next chunk it feels like
giving you. The protocol is undocumented, the server holds back delivery
based on player-time/readahead heuristics, and it's the official direction —
fewer and fewer client variants still get plain signed URLs.

What this means in practice:

- **Some videos download fine, others crawl or 403.** That's the SABR rollout
  hitting different itag/client combinations.
- **The `ANDROID_VR` client at version ≤1.65** is the current "give me real
  URLs" loophole, and it's the priority client here. When Google closes that
  (they will), there's no escape from SABR.
- **Slow downloads ≠ your network.** SABR's readahead throttle is real —
  server says "wait 8 seconds" between chunks, you wait, the file lands at
  approximately playback speed. We work around this by lying about the
  player's position so the server stops throttling.

### What I'm doing about it

There's a real SABR implementation under [pyt/sabr/](pyt/sabr/). It's modeled
after [yt-dlp's PR #13515](https://github.com/yt-dlp/yt-dlp/pull/13515) (massive
thanks to coletdjnz for reverse-engineering the wire format). It currently
handles:

- The full UMP part taxonomy — `MEDIA_HEADER`, `MEDIA`, `MEDIA_END`,
  `NEXT_REQUEST_POLICY`, `FORMAT_INITIALIZATION_METADATA`, `SABR_REDIRECT`,
  `SABR_ERROR`, `STREAM_PROTECTION_STATUS`, `SABR_CONTEXT_UPDATE` /
  `SABR_CONTEXT_SENDING_POLICY`, `SABR_SEEK`.
- Audio + video multiplex on one session via `header_id → itag` demux.
- Segment-aligned `BufferedRange` (start/end segment indices), not byte-fraction
  guesswork.
- Proper `StreamerContext` with full `ClientInfo`, PO token, playback cookie,
  and the SABR contexts the server tells you to echo back (this is the
  unskippable-ad enforcement channel).
- `player_time_ms` set forward to the buffered edge so the server stops
  throttling.
- Backoff cap outside ad windows. Ad-scoped backoffs are honored in full.
- Token refresh, redirect follow, retry/backoff on transport errors.

What's missing / works but not great:

- **PO token.** Required by some accounts for some videos. With Node
  18+ on PATH, `pyt --doctor --install po-token-generator` gets you
  end-to-end support: the doctor `npm install`s the BotGuard JS
  packages into `~/.pyt/js/` and drops a `pyt-po-token` wrapper into
  `~/.pyt/bin/`. Use it via `--po-token-cmd "pyt-po-token"`. The modern
  `Client` also accepts arbitrary external generators via
  `po_token=` / `po_token_provider=` / `po_token_cmd=` / `po_token_script=`,
  and auto-retries downloads once with a fresh token on
  `ATTESTATION_REQUIRED`. See
  [docs/features/po-token.md](docs/features/po-token.md) for the full
  workflow.
- **Live streams.** `video.download_live(...)` now chases the live edge
  over a native SABR session — `SABR_SEEK` resets buffered state to the
  server-directed position, and the session stops cleanly via `.stop()`
  (Ctrl-C) or a recognized broadcast-end signal. `LIVE_METADATA` is parsed
  but not yet acted on: its wire layout isn't documented anywhere we could
  find, so it's inert for now (logged, not interpreted) until it's
  reverse-engineered against real traffic. `video.record_live(...)`
  (ffmpeg over the HLS manifest) remains the simpler, more broadly
  compatible fallback for live configurations SABR live doesn't cover yet.

The two items below are complete; kept here because the SABR layer
grew enough that calling it out seemed worth it:

- **Multi-format download orchestrator.** `video.download_best(...)` (or
  construct `pyt.CombinedDownload` directly) runs video+audio over a
  single multiplexed SABR session, with a byte-range fallback for whatever
  bytes SABR doesn't deliver. The legacy `Stream.download()` path still
  opens one session per stream; no plans to back-port — migrate to
  `Client.video(...).download_best(...)`.
- **Tests on recorded UMP fixtures.** The SABR + UMP layers have 90+
  fixture-driven tests covering single-format completion, multi-format
  multiplex, attestation, redirects, error-then-refresh, ad-scope backoff
  (and the cap-outside-ads behavior that keeps VOD downloads from
  crawling), playback-cookie propagation, stall detection, gzip-encoded
  responses, context-update sending policies, live-edge chasing, and
  player-time advancement to the buffered edge. Builders live in
  `tests/sabr_fixtures.py` so adding scenarios is one-liner work.

If something downloads slow or 403s, file an issue with the URL and your
client (anonymized — no account info needed). I do this in spare time so
turnaround is days-to-weeks, not hours.

---

## Known limitations

- **Age-restricted videos** — the bypass works for most, but tier-3
  age-gated content (country-level) cannot be unlocked without OAuth.
- **po_token** — YouTube's browser-attestation token is not generated
  here. If a video returns 403s on web-client streams it falls back to
  the mobile client, which usually works without it.
- **Live streams** — `download_live()` works for the common case, but
  `LIVE_METADATA` isn't decoded yet (unconfirmed wire layout) and broadcast-end
  detection relies on a heuristic match against `SABR_ERROR`'s type string
  that hasn't been confirmed against real traffic. See
  [docs/features/live-streaming.md](docs/features/live-streaming.md).

---

## Dev setup

```bash
git clone https://github.com/ramsy0dev/pyt
cd pyt
pip install poetry
poetry install --with dev
poetry run pytest tests/ -v
```

---

## Status

I maintain this in my spare time. It works for everything I've tested.
If something breaks, open an issue with a video URL and I'll look at it.
PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
