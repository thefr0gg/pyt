# Modern Python API

`pyt.Client` is the modern Python interface (debuted in 2.0). The pytube-era `YouTube`, `Playlist` and friends were removed in v3.

## Why the new API exists

The pytube-era interface had several footguns:

* **Hidden network I/O on attribute access.** `yt.title` could fire
  HTTP requests; surprises in async code, hard to mock, hard to
  time-out.
* **Module-level globals.** `pyt.__js__`, `pyt.__js_url__`, and the
  `Monostate` Borg pattern shared session state across instances —
  two `YouTube` objects in one process stepped on each other.
* **`.streams.filter(...).first()` returning `Optional[Stream]`** that
  then crashed with `AttributeError` on `.download()` if no stream
  matched.
* **Three ways to register progress callbacks** that drifted in
  behavior between versions.

The modern API fixes all of these.

## Quick start

```python
from pyt import Client

client = Client()
video = client.video("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

print(video.title)            # str
print(video.length)           # datetime.timedelta
print(video.author.name)      # str
print(video.published_at)     # tz-aware datetime
print(video.thumbnails)       # list[Thumbnail]

# Best progressive (video + audio in one file, no ffmpeg merge)
video.streams.progressive.best().download_to("downloads/").run()

# Best adaptive video + audio merged with ffmpeg in one SABR session
video.download_best("downloads/").run()
```

## Client

`Client` owns session state. Construct one per process / FastAPI app
/ notebook.

```python
client = Client(
    proxy="socks5://127.0.0.1:9050",
    cookies="/path/to/cookies.txt",       # XOR with cookies_from_browser
    cookies_from_browser="firefox",       # any of: chrome chromium firefox brave edge safari opera
    po_token=None,                        # PO token if needed for SABR
    use_oauth=False,
    on_progress=lambda stream, chunk, remaining: ...,
    on_complete=lambda stream, path: ...,
)
```

Validation runs eagerly: passing both `cookies=` and
`cookies_from_browser=` raises `ConfigError`, as does an unknown
browser name.

## Video

`client.video(url)` is the **network boundary** — it does the HTTP
work up-front and returns a fully-hydrated `Video` whose attributes
are pure getters on a typed dataclass.

```python
video.metadata           # VideoMeta dataclass (frozen)
video.video_id           # str
video.url                # str
video.title              # str
video.author             # Channel(id=, name=, url=)
video.length             # datetime.timedelta
video.description        # Optional[str]
video.published_at       # Optional[datetime]
video.views              # Optional[int]
video.thumbnails         # list[Thumbnail(url=, width=, height=)]

# Live stream state
video.is_live            # bool — True only while actively broadcasting
video.is_live_content    # bool — True for VODs that were once live streams
video.hls_manifest_url   # Optional[str] — HLS URL when is_live is True

# Age gate
video.is_age_restricted  # bool — True when age verification is required
```

## StreamSet

Chainable, immutable filter view over the stream catalog.

```python
video.streams                     # StreamSet of all
    .audio                        # property; equivalent to .filter(kind="audio")
    .codec("opus")                # codec substring match
    .at_least(bitrate="128k")     # threshold filter
    .order_by("bitrate").desc()
    .first()                      # Optional[StreamRef]
```

**Discoverable shortcuts:**

* `.audio` — audio-only streams
* `.video` — anything with a video track
* `.progressive` — single-file (video+audio combined)
* `.adaptive` — DASH (video-only or audio-only)

**Filtering:**

* `.filter(kind=, subtype=, resolution=, codec=, custom=)`
* `.codec(name)` — codec substring (matches video or audio)
* `.at_least(resolution="1080p")` — keep streams ≥ that resolution
* `.at_least(bitrate="128k")` — keep streams ≥ that bitrate
* `.order_by(attribute)` + `.desc()`

**Terminal selectors:**

| Method | Returns | On empty |
|---|---|---|
| `.first()` | `Optional[StreamRef]` | `None` |
| `.last()` | `Optional[StreamRef]` | `None` |
| `.best()` | `StreamRef` | raises `NoMatchingStream` |
| `.one()` | `StreamRef` | raises if 0 or >1 match |
| `.best_pair(prefer_resolution=)` | `(video, audio)` | raises `NoMatchingStream` |

## Download

`stream.download_to(...)` returns a lazy `Download` builder. Call
`.run()` to execute, or chain post-processing first:

```python
from pyt import pipeline as pp

stream = video.streams.audio.best()
path = (
    stream.download_to("downloads/")
        | pp.sponsorblock(mark=["sponsor", "intro"])
        | pp.embed_metadata()
        | pp.embed_thumbnail()
        | pp.extract_audio("mp3")
).run()
```

The `|` operator is sugar for `.then(...)`. Use whichever you prefer;
they produce identical pipelines.

## Pipeline steps

| Factory | Purpose |
|---|---|
| `pp.sponsorblock(mark=[...])` / `pp.sponsorblock(remove=[...])` | Mark or remove SponsorBlock segments |
| `pp.embed_metadata()` | Title / artist / date / description tags |
| `pp.embed_thumbnail()` | Cover art |
| `pp.embed_subtitles(lang="en")` | Subtitle track |
| `pp.extract_audio("mp3", quality=...)` | Transcode to audio-only |
| `pp.upscale(scale=2)` | **Experimental.** Lanczos / Real-ESRGAN — see [upscale.md](upscale.md) |

Steps run in order. Each is a small frozen dataclass — introspectable,
picklable, testable.

## Combined adaptive download

For "best video-only + best audio merged with ffmpeg", use
`video.download_best(...)`. It drives one SABR session for both
formats. See [combined-download.md](combined-download.md).

## Playlists, channels, search

```python
playlist = client.playlist("https://www.youtube.com/playlist?list=PL...")
print(playlist.title, len(playlist))
for video in playlist:                    # lazy: one HTTP per video at iter time
    video.streams.audio.best().download_to("music/").run()

channel = client.channel("https://www.youtube.com/@somechannel")
print(channel.name, channel.channel_id)

results = client.search("python tutorial")
for video in results.videos:
    print(video.title, video.url)
```

## Live streams

Three distinct live-stream states are handled differently:

| State | `is_live` | `is_live_content` | What to do |
|---|---|---|---|
| Actively broadcasting | `True` | `True` | use `video.record_live()` |
| Past live / VOD | `False` | `True` | download normally — fully supported |
| Scheduled / upcoming | n/a | n/a | `LiveStreamUpcoming` raised at `client.video()` time |

### Recording an active stream

```python
from pyt import Client

video = Client().video("https://www.youtube.com/watch?v=<live_id>")

if video.is_live:
    # Blocks until the broadcast ends (or timeout seconds elapse).
    # Requires ffmpeg on PATH — install with `pyt --doctor --install ffmpeg`.
    path = video.record_live("recordings/", timeout=3600)
```

`record_live(output_path, *, filename, timeout)` captures the HLS
manifest with `ffmpeg -i <hls_url> -c copy`. The call blocks in real
time. Ctrl-C is propagated cleanly. Output is a `.ts` file (the natural
container for HLS streams).

### Scheduled streams

`client.video()` raises `LiveStreamUpcoming` for streams that haven't
started yet. The exception carries `scheduled_start: Optional[datetime]`
when YouTube includes a start time:

```python
from pyt.api.errors import LiveStreamUpcoming

try:
    video = client.video(url)
except LiveStreamUpcoming as exc:
    if exc.scheduled_start:
        print(f"Stream starts at {exc.scheduled_start.isoformat()}")
```

## Age-restricted videos

`video.is_age_restricted` is `True` when YouTube requires age
verification. Accessing `.streams` without auth configured raises
`AgeRestricted` immediately (before any wasted network calls):

```python
video = Client().video(url)   # succeeds — metadata is fetched

if video.is_age_restricted:
    # Rebuild the client with cookies from a signed-in browser.
    video = Client(cookies_from_browser="chrome").video(url)

video.streams.audio.best().download_to("downloads/").run()
```

See [cookies-auth.md](cookies-auth.md) for how to export cookies.

## Errors

All modern errors inherit from `pyt.PytError`:

| Exception | When raised |
|---|---|
| `VideoUnavailable` | private, removed, region-blocked, members-only |
| `AgeRestricted` | age-gated; needs cookies from a signed-in browser (**not** a subclass of `VideoUnavailable` — the video exists) |
| `LiveStreamNotSupported` | `.streams` accessed on an active live stream; use `record_live()` instead |
| `LiveStreamUpcoming` | `client.video()` called on a not-yet-started scheduled stream; carries `scheduled_start` |
| `NoMatchingStream` | `.best()` / `.one()` saw an empty filter chain |
| `AttestationRequired` | SABR returned `ATTESTATION_REQUIRED`; configure a PO token |
| `DownloadError` | byte transfer failed (network, 403, SABR exhaustion) |
| `PostProcessError` | a pipeline step failed; carries `step=`, `partial_output_path=`, `cause=` |
| `ConfigError` | invalid `Client(...)` argument |

Every error carries enough context to file a useful bug report:
`video_id`, `url`, and (for `DownloadError`) `client_used`.

## Sync vs async

The modern API is sync-only at the moment. Async support is on the
roadmap but requires moving the request layer to `httpx` and
threading through SABR — substantial work that hasn't landed yet.

## Migrating from pytube-era `pyt`

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
| Manual PP chain | `download \| pp.sponsorblock(...) \| pp.embed_metadata()` |
| `from pyt.exceptions import …` | `from pyt import PytError, VideoUnavailable, NoMatchingStream, …` |
