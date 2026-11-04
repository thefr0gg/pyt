# Combined Adaptive Download

`video.download_best(...)` runs one adaptive video stream and one
audio stream through a **single multiplexed SABR session**, then
merges them with ffmpeg. This is what real YouTube players do — opening
a separate session per stream would make YouTube see two independent
users, each with its own playback cookie, throttle decision, and
ad-enforcement context. Here both formats share one logical user.

The `pyt` command line uses this path for its default and `-f` downloads.

## Quick start

```python
from pyt import Client

video = Client().video("https://youtu.be/dQw4w9WgXcQ")

# Auto-pick best video + best audio, merge to mp4 / webm / mkv
# depending on the codec pair.
path = video.download_best("downloads/").run()

# Cap quality (useful for slow connections / smaller files)
video.download_best("downloads/", prefer_resolution="1080p").run()
```

## Pipeline composition

`CombinedDownload` accepts the same `.then(...)` / `|` post-processing
syntax as the single-stream `Download` builder:

```python
from pyt import Client, pipeline as pp

path = (
    Client().video(url).download_best("downloads/")
        | pp.sponsorblock(mark=["sponsor", "intro"])
        | pp.embed_metadata()
        | pp.embed_thumbnail()
).run()
```

## Stream picking

`download_best` calls `StreamSet.best_pair(prefer_resolution=)` under
the hood. Override the picks manually if you need finer control:

```python
from pyt import Client, CombinedDownload

video = Client().video(url)
v_stream = video.streams.video.adaptive.codec("av01").best()
a_stream = video.streams.audio.codec("opus").best()

CombinedDownload(
    video=video,
    video_stream=v_stream,
    audio_stream=a_stream,
    output_path="downloads/",
).run()
```

## Container selection

The merge container is auto-picked from the codec pair via
`pyt.api._merge.pick_merge_container`:

| Video codec | Audio codec | Container |
|---|---|---|
| avc1 (H.264) / av01 (AV1) | aac | mp4 |
| vp9 | opus | webm |
| anything else / mixed | anything else | mkv (safe fallback) |

Override with `container=`:

```python
video.download_best("downloads/", container="mkv").run()
```

## Per-format byte-range fallback

If SABR can't deliver every byte for either format (throttle, 403,
session expiry), the missing tail is finished via byte-range request
from the direct URL. A 99%-complete download won't die from one bad
SABR exchange.

## Output validation

After ffmpeg merges, the output's duration is probed via ffprobe and
compared against the inputs. If the output is less than 90% of the
longer input, the merge raises `DownloadError` rather than walking
away with a silently-truncated file.

This catches the case where `-c copy` + libdav1d/libaom hits corrupt
AV1 OBUs partway through, halts at that point, writes what it has,
and exits 0. Without the duration check, a 4-second output from a
2-minute input would pass as "success" by exit code alone.

The merge command also passes `-fflags +discardcorrupt -err_detect
ignore_err` so the muxer drops corrupt packets and keeps going
instead of stopping at the first bad one.

## Error handling

```python
from pyt.api.errors import DownloadError, PostProcessError

try:
    video.download_best("./").run()
except DownloadError as exc:
    print(f"Download failed: {exc}")
    print(f"Video ID: {exc.video_id}")
    print(f"Client used: {exc.client_used}")
except PostProcessError as exc:
    # Pipeline step failed (e.g. embed_metadata)
    print(f"Step '{exc.step}' failed: {exc}")
    print(f"Partial output kept at: {exc.partial_output_path}")
```

On ffmpeg merge failure, both `.part` files are kept on disk so you
can retry without re-downloading the bytes.
