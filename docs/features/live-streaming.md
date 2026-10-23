# Live streaming

## Two ways to capture a live broadcast

pyt has two paths for an active live stream, with different trade-offs.
Neither replaces the other.

### `video.record_live(...)` — ffmpeg over HLS

```python
video = client.video("https://youtube.com/watch?v=...")
video.record_live(output_path="stream.ts")
```

Shells out to `ffmpeg -i <hls_manifest_url> -c copy`. Blocks until the
broadcast ends or the process is interrupted (Ctrl-C propagates cleanly).
This is the simpler, more broadly compatible path — it works for any live
configuration that exposes an HLS manifest, independent of whether pyt's
SABR layer fully understands that stream's quirks. Requires `ffmpeg` on
PATH (`pyt --doctor --install ffmpeg`).

### `video.download_live(...)` — native SABR

```python
video = client.video("https://youtube.com/watch?v=...")
download = video.download_live(output_path="stream.mp4", prefer_resolution="1080p")
try:
    final_path = download.run()
except KeyboardInterrupt:
    pass
```

Drives pyt's own SABR/UMP implementation directly — no ffmpeg dependency
during capture (still needed for the final video+audio mux at the end).
Gives you the same resolution/itag selection as `download_best()` for VOD.

Call `download.stop()` (e.g. from a signal handler) to end the capture
cleanly; the in-flight SABR round finishes and the recording is merged
normally instead of being discarded.

## How live-mode SABR differs from VOD

A VOD download knows its total size/segment count up front and stops when
it's reached. A live broadcast has no such bound while it's still live, so
`SabrSession(..., live=True)` changes behavior in a few places:

- **Buffered-range reporting** is a sliding window near the live edge
  (last few segments) rather than a from-zero range, since a from-zero
  range would grow unboundedly over a multi-hour broadcast.
- **`player_time_ms`** is computed only from the buffered edge the server
  has actually reported — VOD's byte-fraction estimate doesn't apply
  (there's no known total duration/size to estimate against).
- **`SABR_SEEK`** (sent by the server to redirect playback, e.g. on
  rejoining near the live edge) resets the format's buffered-range state
  to the seek target, instead of being logged and ignored like it is for
  VOD.
- **Termination** comes from `session.stop()` or a `SABR_ERROR` recognized
  as a broadcast-end signal — not from reaching a known end.

## Known gap: `LIVE_METADATA`

UMP part type 31 (`LIVE_METADATA`) is parsed into a raw field dict but not
otherwise acted on. Every other UMP part pyt decodes has a confirmed field
layout (cross-referenced against yt-dlp's reverse-engineering or derived
from observed traffic); `LIVE_METADATA`'s layout isn't documented anywhere
we could find. Until it's reverse-engineered against real traffic, pyt
tracks the live edge from `MEDIA_HEADER`'s `sequence_number`/`start_ms`/
`duration_ms` fields instead — sufficient to chase the live edge, but not
to authoritatively detect broadcast-end the way a decoded `LIVE_METADATA`
could.

Likewise, the `SABR_ERROR` type string pyt currently treats as
"broadcast ended" (any type containing `"ended"`) is a reasonable guess,
not something confirmed against a real ended broadcast. If
`download_live()` hangs past when a stream actually ends, that heuristic
is the first place to check — enable `pyt.enable_logging("DEBUG")` and
look for an unhandled `SABR_ERROR` or a `LIVE_METADATA` dump in the log.
