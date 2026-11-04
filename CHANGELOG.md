# Changelog

## 3.0.0 — 2026-11-04

The pytube-era API is gone. `pyt.Client` is the only Python entry point, and
the `pyt` command line now runs on it too.

### Removed

- **`pyt.legacy`** (the v2 compatibility shim) and the classes behind it:
  `YouTube`, `Stream`, `StreamQuery`, `CaptionQuery`, `Monostate`,
  `Playlist`, `Channel` and `Search` (`pyt.contrib.*`). See the migration
  table in the README.
- **`.legacy` escape hatches** on `Playlist`, `ChannelFeed` and
  `SearchResults`.
- **`pyt.helpers.create_mock_html_json`** / `generate_all_html_json_mocks`,
  which regenerated fixtures through the removed `YouTube` class.
- Exception types nothing raised any more: `AgeRestrictedError`,
  `VideoPrivate`, `RecordingUnavailable`, `MembersOnly`, `VideoRegionBlocked`
  (use `pyt.AgeRestricted` / `pyt.VideoUnavailable`).
- `extract.playability_status`, `video_info_url`,
  `video_info_url_age_restricted`, `recording_available`, `is_private`.

### Changed

- **The CLI runs on `pyt.Client`.** Single videos, playlists and channels all
  go through `Client`; merged downloads (default and `-f`) use
  `CombinedDownload`, so both tracks share one SABR session. If the merged
  download fails, the CLI still falls back to the best progressive stream.
  `--build-playback-report` now dumps the player response (`video_info`);
  it no longer contains `js` / `watch_html`.
- **Post-processors and `OutputTemplate` take `Video` / `StreamRef`.**
  `PostProcessor.run(path, stream, video)` and
  `OutputTemplate.render(video, stream, ...)` read `video.author.name`,
  `video.published_at` and `video.author.id`.
- `python -m pyt` now runs the CLI.

### Fixed

- **`pyt.api.pipeline` steps were broken.** They passed `stream.legacy` /
  `video.legacy`, which the modern `Video` / `StreamRef` never defined.
- **`Client(proxy=...)` had no effect.** The proxy was computed but never
  installed; it is now installed when the client first fetches.
- **`Client.search()`** no longer goes through the HTML-era scraper; it reads
  `videoRenderer` entries from InnerTube's `search` response.

### Added

- `Video.captions`, `Video.thumbnail_url` and `Video.player_response`.
- `SearchResults.video_ids`.

## 2.1.0 — 2026-05-09

### Fixed

#### Doctor — realesrgan binary download

- **Wrong repo and filename.** The previous code fetched from
  `xinntao/Real-ESRGAN-ncnn-vulkan` (binary-only repo) using a
  version-derived filename that never existed. The download now targets
  `xinntao/Real-ESRGAN` v0.2.5.0, whose release bundle ships the binary
  **and** model files together in
  `realesrgan-ncnn-vulkan-20220424-{platform}.zip`.
- **Models were missing.** After install the upscaler would fail at
  runtime because `realesrgan-ncnn-vulkan` couldn't find its `.bin`/
  `.param` model files. The install now copies the bundled `models/`
  directory to `~/.pyt/bin/models/` via `copy_archive_dir=True`.
- **`-m` flag not passed.** `upscale.py` invoked the binary without
  `-m`, so it looked for `models/` relative to the current working
  directory — wrong for any invocation not from `~/.pyt/bin/`. pyt now
  detects a `models/` directory next to the binary and passes
  `-m <path>` automatically.

#### Live stream handling

Previously all three live-stream states were conflated into a single
`LiveStreamNotSupported` error thrown at stream-fetch time with no
recovery path.

- **Active live streams** (`isLive=true`) — `VideoMeta` now carries
  `is_live: bool`, `hls_manifest_url: Optional[str]`. The new
  `Video.record_live(output_path, *, filename, timeout)` method captures
  the broadcast via `ffmpeg -i <hls_url> -c copy`, blocking until the
  stream ends or `timeout` seconds elapse.
- **Past live recordings / VODs** (`isLiveContent=true, isLive=false`)
  — previously mislabelled as live. `is_live` is now set only from
  `isLive`, not `isLiveContent`. VODs are fully downloadable via the
  normal stream path.
- **Scheduled / upcoming streams** (`isUpcoming=true`) — previously
  surfaced as a confusing generic error. `client.video()` now raises
  `LiveStreamUpcoming` immediately. The exception carries
  `scheduled_start: Optional[datetime]` (UTC) when YouTube includes a
  start time in the response.
- `VideoMeta` gains `is_live_content: bool`, `hls_manifest_url:
  Optional[str]`, and `scheduled_start: Optional[datetime]`.
- `Video` gains `is_live_content`, `hls_manifest_url`, and
  `record_live()`.

#### Age-restricted videos

- **Wrong error hierarchy.** `AgeRestricted` was a subclass of
  `VideoUnavailable`, but an age-gated video is accessible — it just
  requires authentication. `AgeRestricted` is now a direct `PytError`
  subclass.
- **Useless error message.** The exception now explains the fix inline:
  `Client(cookies_from_browser="chrome")` or
  `Client(cookies="path/to/cookies.txt")`.
- **No early detection.** `VideoMeta` gains `is_age_restricted: bool`
  (populated at hydration time from the legacy layer's cached
  `watch_html` check — no extra network call). `Video.is_age_restricted`
  exposes it.
- **Wasted network round-trips.** Accessing `Video.streams` on an
  age-restricted video without auth configured now raises `AgeRestricted`
  immediately, before the legacy `bypass_age_gate()` loop fires several
  extra HTTP requests that would all fail anyway.

### Changed

- `AgeRestricted` no longer inherits `VideoUnavailable`. Code catching
  `VideoUnavailable` to handle "completely inaccessible" videos is not
  affected; code catching `VideoUnavailable` specifically to handle
  age-restricted content should be updated to catch `AgeRestricted`.

### Test counts

2.0.0 shipped **633** tests; 2.1.0 ships **640**, all passing.

---

## 2.0.0 — 2026-05-08

The "modern API" release. Adds a redesigned Python interface
(`pyt.Client`), a doctor command that reports + auto-installs every
external tool the library can use, structured logging, end-to-end
PO-token support, an experimental upscaler, multi-format SABR
multiplexing, and a fixture-driven test suite for the SABR/UMP
layers.

**Backward compatibility:** every legacy entry point still works.
`pyt.YouTube`, `pyt.Playlist`, `pyt.Channel`, `pyt.Search`,
`register_on_progress_callback`, and `register_on_complete_callback`
emit `DeprecationWarning`. `from pyt.legacy import YouTube` is the
explicit-import home. Top-level removal targets v3 — there is no
forced migration in 2.x.

**Test counts:** 1.1.0 shipped ~250 tests; 2.0.0 ships **624**, all
passing.

### Added

#### Modern Python API (`pyt.Client`)

A redesigned developer interface lives at `pyt.api` and is re-exported
from `pyt`. The legacy `YouTube` / `Playlist` / `Stream` classes still
exist; the new surface is opt-in but recommended for new code.

- **`Client(...)`** — single entry point, owns proxy / cookies /
  PO-token / OAuth state. Replaces the module-level `pyt.__js__` /
  `pyt.__js_url__` / `Monostate` globals (legacy still touches them
  internally; full decoupling is a v3 task).
- **`Video`** — typed, frozen-dataclass-backed snapshot. Attribute
  access is **pure**: no hidden network I/O. `client.video(url)` is
  the I/O moment.
- **`StreamSet`** — chainable, immutable filter view. `.audio` /
  `.video` / `.progressive` / `.adaptive` shortcuts, `.filter`,
  `.codec`, `.at_least`, `.order_by`, `.desc`, `.first`, `.last`,
  `.best` (raises `NoMatchingStream` when empty), `.one` (raises if
  not exactly one match), `.best_pair(prefer_resolution=)`.
- **`Download`** builder — lazy; call `.run()` to execute. Pipeline
  composition via `.then(step, ...)` or the `|` operator.
- **`pipeline.*`** — declarative post-processing factories:
  `sponsorblock(mark=...)`, `embed_metadata()`, `embed_thumbnail()`,
  `embed_subtitles(lang=)`, `extract_audio(format=)`, `upscale(...)`.
- **`Playlist` / `ChannelFeed` / `SearchResults`** — modern wrappers
  over the legacy `Playlist` / `Channel` / `Search` classes; lazy
  iteration yields `Video` objects.
- **Typed error hierarchy** — `PytError` root with `VideoUnavailable`,
  `AgeRestricted`, `LiveStreamNotSupported`, `AttestationRequired`,
  `NoMatchingStream`, `DownloadError`, `PostProcessError`,
  `ConfigError`. Every error carries `video_id` / `url` /
  (where relevant) `client_used`.

#### Combined adaptive download (multi-format SABR)

`video.download_best(...)` (or `pyt.CombinedDownload` directly) drives
**one** multiplexed SABR session for the chosen video + audio streams,
then merges with ffmpeg. YouTube treats both formats as a single
logical user (one playback cookie, one throttle decision, one
ad-enforcement context) — matching real-player behavior. Per-format
byte-range fallback finishes anything SABR doesn't deliver. Output
duration is validated against the inputs after merge; a 4-second
output from a 2-minute input now raises `DownloadError` instead of
silently passing as success.

The legacy `Stream.download()` path still opens one session per
stream; no plans to back-port — migrate to `Client.video(url).download_best(...)`.

#### Doctor command

`pyt --doctor` reports which external tools are installed and which
features that unlocks. `pyt --doctor --install <tool>` downloads the
right asset for the current platform and drops it in `~/.pyt/bin/`,
which pyt prepends to its own `PATH` at module import (process-local
mutation; never touches your shell profile).

| Tool | Auto-install support |
|---|---|
| ffmpeg / ffprobe | Windows x86_64 (gyan.dev), Linux x86_64 + arm64 (BtbN). macOS uses `brew install ffmpeg`. |
| realesrgan-ncnn-vulkan | All three platforms (xinntao GitHub releases). |
| pyt-po-token | All three platforms via npm into `~/.pyt/js/`. Requires Node 18+. |

The doctor also detects (but doesn't auto-install) Node, Bun, Deno,
and `bgutil-pot` — surfacing per-tool install URLs in the missing-hint
column.

#### PO-token support

When SABR returns `STREAM_PROTECTION_STATUS=ATTESTATION_REQUIRED`,
the modern API translates it to the typed `AttestationRequired`
error. `Client(...)` accepts four ways to provide a token:

- `po_token="abc..."` — static value (e.g. extracted from DevTools)
- `po_token_provider=fn` — Python callable returning a fresh token
- `po_token_cmd="bgutil-pot ..."` — shell out and read stdout
- `po_token_script="/path/x.js"` — run a JS file with the first
  available JS runtime on PATH (node / bun / deno; pyt picks node
  first if multiple are present)

Tokens are cached for 30 minutes (configurable); `CombinedDownload`
auto-refreshes on `AttestationRequired` and retries the SABR session
once. CLI flags: `--po-token`, `--po-token-cmd`, `--po-token-script`.

`pyt --doctor --install po-token-generator` installs a working
end-to-end generator (npm-installed `bgutils-js` + `youtubei.js`,
plus a vendored launcher and a `pyt-po-token` wrapper). After install,
`--po-token-cmd "pyt-po-token"` is the user-facing incantation.

#### Experimental upscaler (`pp.upscale(...)`)

Two algorithms, picked by `algorithm=`:

- `"lanczos"` (default) — single-pass ffmpeg `scale=...:flags=lanczos`
  + light unsharp filter. Real-time on any CPU. No extra installs.
  Best at 2× (360→720, 720→1440); doesn't add detail the source
  doesn't have, but produces a noticeably cleaner result than naive
  bilinear or browser-side player upscaling.
- `"realesrgan"` — Real-ESRGAN neural upscaler via the
  `realesrgan-ncnn-vulkan` binary. **Chunked by default** so peak
  intermediate disk drops from ~45 GB to ~6 GB on a 5-minute 720p × 4×
  example. `chunk_seconds=` tunes the trade-off; `threads=` passes
  through to the binary's `-j load:proc:save` for GPU throughput tuning.

Emits a one-shot `FutureWarning` on first use. Source bytes are
restored from a `.pre-upscale` backup if anything in the pipeline
fails.

#### Public logging API

```python
import pyt

pyt.enable_logging("DEBUG")              # filter chains, picks, timings
pyt.enable_logging("TRACE")              # everything, incl. per-chunk SABR
pyt.enable_logging(file="/tmp/pyt.log")  # also write to file
pyt.set_log_level("INFO")
pyt.disable_logging()
pyt.diagnostic_report()                  # bug-report dump (no user content)
```

Off by default — the library is silent until the consumer asks for
it. Heavy DEBUG/INFO instrumentation across `Client`, `Video`,
`StreamSet`, `Download`, `CombinedDownload`. CLI: `-v` for DEBUG,
`-vv` for TRACE. `PYT_LOG_LEVEL=DEBUG` enables logging at import
time without code changes.

#### Other features

- **`mutagen`** and **`browser-cookie3`** promoted from optional
  extras to base dependencies. The cookie path is load-bearing for
  the SABR mitigation; bundling them simplifies the install matrix.
- **Stream download size validation** — `Stream.download()` now
  raises `IOError` if the on-disk file is more than 1% short of the
  reported `Content-Length`. Catches the silent-truncation case where
  SABR delivered short, the byte-range fallback also came up short,
  and `on_complete` fired regardless.
- **ffmpeg merge hardening** — both the modern `_merge.py` path and
  the legacy CLI's inline merge add `-fflags +discardcorrupt -err_detect
  ignore_err` and validate the output's duration via ffprobe after
  the merge. Output that's <90% of the longer input now raises
  `DownloadError` (or exits 1 on the CLI) with the source files
  preserved for retry.

### Test coverage

- **624 tests pass** (up from ~250 in 1.1.0).
- **`tests/sabr_fixtures.py`** — fixture builders for UMP wire bytes.
  Per-part-type helpers (`make_media_header`, `make_format_init`,
  `make_sabr_redirect`, etc.) so adding scenarios is one-liner work
  and protocol-format changes break at the helper rather than across
  every test body.
- **81 new SABR / UMP / proto tests** covering varint round-trips at
  every size boundary, parser chunked delivery, single + multi-format
  SABR exchanges, attestation, redirect, error-then-refresh,
  ad-scope backoff cap, playback-cookie propagation, stall detection,
  gzip-encoded responses, context-update sending policies, player-time
  advancement, and resume seeding.

### Deprecated

The following emit `DeprecationWarning`. They still work and will be
removed in v3 (one major release window of warnings). Use
`from pyt.legacy import ...` for the explicit-import path.

- `pyt.YouTube` (use `pyt.Client().video(url)`)
- `pyt.YouTube.from_id` (same — pass a URL or an ID-formatted URL)
- `pyt.Playlist` (use `Client().playlist(url)`)
- `pyt.Channel` (use `Client().channel(url)`)
- `pyt.Search` (use `Client().search(query)`)
- `youtube.register_on_progress_callback` /
  `register_on_complete_callback` (pass `on_progress=` /
  `on_complete=` to `Client(...)`)

### Fixed

- ffmpeg merge silently producing a 4-second output from a 2-minute
  input when libdav1d hit corrupt OBUs. Both the modern and CLI
  merge paths now validate output duration via ffprobe.
- `Stream.download()` reporting "Saved" for files that were 40% short.
  Now raises `IOError` with retry guidance.
- Doctor's "(ships with ffmpeg)" hint appearing on tools that don't
  ship with ffmpeg (node, bun, deno, bgutil-pot, pyt-po-token).
  Per-tool install URL hints replace it.
- Test pollution from `test_download_with_existing` directly mutating
  `os.path.getsize`. New tests pin `os.path.getsize` for their scope
  rather than inheriting the leak.

### Migration guide

See [README.md](README.md#migrating-from-the-legacy-api) and
[docs/features/python-api.md](docs/features/python-api.md). The
12-row before/after table covers `YouTube` → `Client.video`,
`Playlist` → `Client.playlist`, the four ways to provide PO tokens,
the modern error names, and the silencing pattern for the
deprecation warnings.

---

## 1.1.0 — 2026-05-03

Major feature release closing the gap with yt-dlp for YouTube-specific use cases.
All changes are backward-compatible — existing scripts and CLI invocations continue
to work unchanged.

### Added

#### Post-processing pipeline

A chainable post-processor architecture (`pyt/postprocessors/`) lets you run
arbitrary transformations on the downloaded file without modifying any existing
`Stream` or `YouTube` code.

- **`AudioExtractor`** (`-x / --extract-audio`, `--audio-format FORMAT`) —
  strips the video track and converts to mp3, m4a, aac, flac, opus, vorbis,
  wav, or alac using ffmpeg. The original file is deleted after a successful
  conversion.
- **`FFmpegMetadataEmbedder`** (`--embed-metadata`) — embeds title, artist,
  upload date, and description. Uses `mutagen` for richer ID3 tags on MP3
  when the `[metadata]` extra is installed; falls back to ffmpeg otherwise.
- **`EmbedThumbnailPostProcessor`** (`--embed-thumbnail`) — downloads the
  video thumbnail and embeds it as cover art. MP4/M4A use ffmpeg's
  attached-picture method; MP3 uses a `mutagen` APIC frame.
- **`EmbedSubtitlePostProcessor`** (`--embed-subs`) — downloads a caption
  track and muxes it into the video without re-encoding. Supports MP4
  (`mov_text`), MKV (`srt`), and WebM (`webvtt`). Falls back to the
  auto-generated caption (`a.<lang>`) if a manual one is not available.
- **`SponsorBlockPP`** (`--sponsorblock-mark CATS`, `--sponsorblock-remove CATS`) —
  queries the SponsorBlock API (no new runtime dependency — uses stdlib
  urllib). Mark mode embeds chapter labels via an ffmetadata sidecar without
  re-encoding. Remove mode cuts segments with ffmpeg's `select` filter.
  Accepts comma-separated categories or `all`.

#### Output templates

`-o / --output TEMPLATE` controls the output filename and directory structure.
Syntax uses `{field}` placeholders:

```bash
pyt <url> -o "{author}/{upload_date:%Y-%m-%d} - {title}.{ext}"
```

Available fields: `title`, `id`, `author`, `channel_id`, `upload_date`
(supports strftime format via `{upload_date:%Y-%m-%d}`), `ext`, `resolution`,
`fps`, `abr`, `filesize`, `playlist_index`, `playlist_title`.
Each path component is sanitised for filesystem safety independently.

#### Download archive

`--download-archive FILE` tracks downloaded video IDs in a file compatible with
yt-dlp's archive format (`youtube <id>` per line). Already-downloaded videos
are skipped in batch and playlist mode. Writes are thread-safe.

#### Batch processing

`--batch-file FILE` reads one URL per line (blank lines and `#` comments
ignored) and downloads each with the full post-processing chain. Integrates
with `--download-archive` to skip already-downloaded videos.

#### Rate limiting

`--sleep-interval N` and `--max-sleep-interval N` add a fixed or random sleep
between downloads in batch and playlist mode.

#### Cookie authentication

`--cookies FILE` loads a Netscape-format cookie file (compatible with yt-dlp
and browser export extensions).
`--cookies-from-browser BROWSER` extracts cookies directly from Chrome,
Firefox, Brave, Edge, or Safari — requires `pip install pyt[cookies]`.
Cookies are injected into pyt's HTTP layer via a monkey-patched opener so all
subsequent requests carry them automatically.

#### Proxy support

`--proxy URL` passes HTTP, HTTPS, or SOCKS5 proxy configuration to the YouTube
HTTP layer. Examples: `http://user:pass@host:port`,
`socks5://127.0.0.1:9050`.

#### Geo-bypass

`--geo-bypass` injects a randomised `X-Forwarded-For` header.
`--geo-bypass-country CC` uses a specific country code (e.g. `US`, `DE`).

#### Configuration files

`pyt.conf` (INI format, `[default]` section) stores persistent defaults.
Searched in order: `./pyt.conf`, `~/.config/pyt/pyt.conf`, `~/.pyt.conf`.
CLI flags always override config values.

#### JSON dump mode

`-j / --dump-json` prints video metadata as JSON without downloading, useful
for scripting.

#### Docs

A full `docs/` folder was added covering every feature above, plus a
feature-comparison table against yt-dlp (`docs/comparison.md`).

### Optional dependencies

```bash
pip install pyt[metadata]   # mutagen — richer MP3/ID3 tags
pip install pyt[cookies]    # browser-cookie3 — browser cookie extraction
pip install pyt[all]        # both of the above
```

Core pyt remains dependency-free for users who don't need these extras.

### Tests

326 tests covering all new modules:
`test_template.py`, `test_archive.py`, `test_config.py`,
`test_cookies.py`, `test_postprocessors.py`.

---

## 1.0.0 — 2026-05-02

First release under the **pyt** name. This is a fork of the original
[pytube](https://github.com/pytube/pytube) project, which went unmaintained
while YouTube's API continued changing. Everything below was broken or missing
in the original and has been fixed here.

### Fixed

- **Multi-client extraction** — player data is now fetched by trying ANDROID →
  IOS → TV\_EMBED → page HTML in order. The first client that returns real
  stream URLs wins. This eliminates most 403s and "no streams" errors.
- **Pre-signed stream URLs** — ANDROID and IOS clients return URLs that don't
  require signature deciphering, so the vast majority of downloads work without
  touching the cipher at all.
- **visitorData** — extracted automatically from the watch page `ytcfg` blob
  and forwarded with every InnerTube player request. YouTube started requiring
  this for non-bot-looking traffic.
- **Self-updating web client version** — the `INNERTUBE_CLIENT_VERSION` is read
  from the live watch page instead of a hardcoded string that went stale within
  weeks of each release.
- **`video_id()` parsing** — the original used `url.split("=")[-1]`, which
  broke on playlist URLs and shorts. Replaced with a proper regex.
- **`apply_signature()`** — `parsed_url` was never assigned, causing a
  `NameError` at runtime. Fixed, along with a missing guard on the `n` param.
- **`datetime.utcfromtimestamp()` deprecation** — replaced with
  `datetime.fromtimestamp(..., timezone.utc)` throughout.
- **Lazy cipher initialisation** — the JS cipher is only fetched and parsed when
  actually needed (web-client fallback), not on every `YouTube()` construction.

### Changed

- Package renamed from `pytube` to `pyt`. CLI command is now `pyt`.
- `PytubeError` renamed to `PytError`.
- Logger redesigned: compact `HH:MM:SS  LVL  module  message` format,
  colourised on TTY, plain text when piped or in CI.
- CLI rewritten with a modern progress bar showing speed, ETA, and a `█`/`░`
  fill bar. Stream listing displays a formatted table.
- All stale InnerTube client version strings updated to current values.
- Dropped: `pipenv` / `.envrc`, `.deepsource.toml`, `.bumpversion.cfg`,
  `MANIFEST.in`, `docs/`, Sphinx dependencies.

### Dependencies

None. pyt has zero runtime dependencies — pure Python 3.10+.
