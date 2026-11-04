# Logging & Diagnostics

pyt is silent by default — like a well-behaved library should be. Turn
on logging when you want to see what it's doing, debug a failure, or
collect output for a bug report.

## Enabling logs from Python

```python
import pyt

pyt.enable_logging()                     # INFO (lifecycle events, no per-chunk noise)
pyt.enable_logging("DEBUG")              # DEBUG (filter chains, picks, timings, retries)
pyt.enable_logging("TRACE")              # everything, including per-chunk SABR (very loud)
pyt.enable_logging(file="/tmp/pyt.log")  # also write to a file (handy for bug reports)
pyt.set_log_level("DEBUG")               # adjust mid-run without recreating handlers
pyt.disable_logging()                    # silence again
```

`enable_logging` is idempotent — calling it again replaces the previous
pyt-managed handler instead of stacking. Handlers you've added yourself
to the `pyt` logger (or to the root logger) are left alone.

## Levels

| Level | Use when |
|---|---|
| `WARNING` (default when off) | Library behaves silently except for genuine warnings via the `warnings` module |
| `INFO` | Lifecycle events: `client.video` fetches, stream catalog hydration, download start/end with bytes + duration, post-processing step start, doctor install steps |
| `DEBUG` | Decisions: which stream `.best()` picked and why, filter chain results, byte-range fallback rationale, ffmpeg argv shape, chunk schedules |
| `TRACE` (`-vv` on CLI) | Per-chunk SABR, every HTTP request/response, every PNG frame extracted — only useful when chasing a specific protocol-level bug |

`TRACE` is level 5 (below `DEBUG=10`). The constant is exposed as
`pyt.TRACE` for use with stdlib logging:

```python
import logging
logging.getLogger("pyt").setLevel(pyt.TRACE)
```

## CLI

```bash
pyt <url> -v       # DEBUG
pyt <url> -vv      # TRACE
pyt <url> -v --logfile /tmp/pyt.log
```

`-v` and `--logfile` route through the same `pyt.api` logging path that
`enable_logging()` controls, so CLI and library messages come out together.

## Environment variable

Setting `PYT_LOG_LEVEL` before importing pyt enables logging without
touching the code that uses pyt — useful for investigating an issue in
a third-party tool that imports the library:

```bash
PYT_LOG_LEVEL=DEBUG python my_script.py
PYT_LOG_LEVEL=TRACE pyt <url>
```

Bad values (typos, unknown levels) are silently ignored at import time
rather than raising — pyt should never fail to import because of a
misconfigured environment variable.

## Sample output

```
$ PYT_LOG_LEVEL=DEBUG python -c "import pyt; pyt.Client().video('https://youtu.be/dQw4w9WgXcQ').download_best('./').run()"
14:32:01 DEBUG   pyt.api.client :: Client init: proxy=False cookies=False cookies_from_browser=None po_token=unset oauth=False on_progress=unset on_complete=unset
14:32:01 INFO    pyt.api.client :: client.video: fetching https://youtu.be/dQw4w9WgXcQ
14:32:02 DEBUG   pyt.api.video  :: Video._from_url: player response OK video_id=dQw4w9WgXcQ client=ANDROID_VR
14:32:02 INFO    pyt.api.client :: client.video: dQw4w9WgXcQ hydrated in 1.45s (title='Rick Astley - Never Gonna Give You Up' length=0:03:32)
14:32:02 DEBUG   pyt.api.video  :: video.streams: hydrating stream catalog for dQw4w9WgXcQ
14:32:02 INFO    pyt.api.video  :: video.streams: dQw4w9WgXcQ catalog has 24 streams
14:32:02 INFO    pyt.api.streams :: StreamSet.best_pair: video itag=137 res=1080p codec=avc1.640028 + audio itag=140 abr=128kbps codec=mp4a.40.2 (prefer_resolution=None, video_id=dQw4w9WgXcQ)
14:32:02 INFO    pyt.api.combined :: CombinedDownload.run: video itag=137 (avc1.640028) + audio itag=140 (mp4a.40.2) container=mp4 output=. steps=0 (video_id=dQw4w9WgXcQ)
14:32:02 DEBUG   pyt.api.combined :: CombinedDownload: opening SABR session url=https://...
14:32:18 DEBUG   pyt.api.combined :: CombinedDownload: itag=137 delivered fully via SABR (76443521 bytes)
14:32:18 DEBUG   pyt.api.combined :: CombinedDownload: itag=140 delivered fully via SABR (3398551 bytes)
```

## Diagnostic report for bug reports

`pyt.diagnostic_report()` returns a self-contained text block capturing
the environment and tool state. Paste it into a GitHub issue — no
URLs, video IDs, or other user content is included.

```python
import pyt
print(pyt.diagnostic_report())
```

```
pyt version    : 1.1.0
python version : 3.13.1
platform       : Linux 6.6.10 (x86_64)
managed bin    : /home/you/.pyt/bin

Tools:
  ffmpeg        OK   ffmpeg version 7.1
                     path: /usr/bin/ffmpeg
  ffprobe       OK   ffprobe version 7.1
                     path: /usr/bin/ffprobe
  realesrgan    MISSING

PYT_LOG_LEVEL  : <unset>
effective level: WARNING
```

## Integrating with your own logging setup

The `pyt` logger inherits from the root logger by default. If you've
already configured handlers on the root logger (most apps do), pyt's
records will flow into them automatically — `enable_logging` is only
needed if you want pyt's own handlers in addition.

To keep pyt records from appearing in your root handlers (e.g. in a
production app where you want pyt to log only to its own file):

```python
pyt.enable_logging("DEBUG", file="/var/log/pyt.log", propagate=False)
```

To log structured / JSON output, install your own handler with a JSON
formatter directly on the `pyt` logger:

```python
import logging
import json_log_formatter   # third-party

handler = logging.StreamHandler()
handler.setFormatter(json_log_formatter.JSONFormatter())
logging.getLogger("pyt").addHandler(handler)
logging.getLogger("pyt").setLevel(logging.DEBUG)
```

`enable_logging` won't touch user-installed handlers — only the
pyt-managed stream/file handler it owns. `disable_logging` removes
only the managed handler, leaving yours alone.

## Performance

Building log payloads has a real cost when arguments include large
strings (titles, paths). When you want to skip that cost in hot paths,
guard with `pyt.logging_enabled()`:

```python
if pyt.logging_enabled():
    logger.debug("expensive context: %r", build_big_payload())
```

But for normal usage, default Python logging's lazy `%` formatting is
fast enough — `logger.debug("foo %s", x)` doesn't format the string
unless DEBUG is enabled.
