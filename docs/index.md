# pyt Documentation

**pyt** is an actively maintained Python 3 library and CLI for downloading YouTube videos. It is a feature-enhanced fork of pytube with minimal runtime dependencies that rivals yt-dlp for YouTube-specific use cases.

## Feature Overview

| Category | Features |
|---|---|
| **Download** | 144p – 8K, progressive & DASH, audio-only, ffmpeg merge |
| **Combined adaptive download** | One multiplexed SABR session for video+audio, ffmpeg merge with output validation |
| **Audio extraction** | Convert to mp3, m4a, flac, opus, vorbis, wav, aac, alac |
| **Metadata** | Embed title, artist, date, description (ffmpeg + mutagen) |
| **Thumbnails** | Embed cover art into mp4, m4a, mp3, ogg, flac |
| **Subtitles** | Embed SRT into mp4, mkv, webm; 100+ language codes |
| **SponsorBlock** | Mark segments as chapters or remove them entirely |
| **Upscaling** *(experimental)* | Lanczos (real-time, no GPU) and Real-ESRGAN (neural, GPU recommended) |
| **Output templates** | `%(title)s`, `%(author)s`, `%(upload_date)s`, and more |
| **Archive tracking** | Skip already-downloaded videos across runs |
| **Batch processing** | Download from a file of URLs |
| **Authentication** | Netscape cookie files, browser cookie extraction |
| **Proxy / geo-bypass** | HTTP, HTTPS, SOCKS5 proxy; X-Forwarded-For spoofing |
| **Config files** | `~/.pyt.conf` for persistent defaults |
| **Playlists & channels** | Full playlist and channel support |
| **Search** | Search YouTube from the Python API |
| **Doctor command** | Detect installed tools, auto-install ffmpeg / realesrgan-ncnn-vulkan |

## Installation

```bash
pip install pyt
```

`mutagen` (richer MP3/ID3 tags) and `browser-cookie3` (browser cookie
extraction) are part of the base install.

ffmpeg is required for post-processing features (audio extraction,
metadata embedding, SponsorBlock, thumbnail/subtitle embedding,
combined merge, upscaling). Get it from
https://ffmpeg.org/download.html — or run `pyt --doctor --install
ffmpeg` to drop it into `~/.pyt/bin/` automatically.

Run `pyt --doctor` at any time to see which tools are installed and
which features that unlocks.

## Quick Start

```bash
# Download best quality (auto-merges with ffmpeg if available)
pyt "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Download and convert to MP3
pyt <url> -x --audio-format mp3

# Download with metadata, thumbnail, and SponsorBlock chapters
pyt <url> --embed-metadata --embed-thumbnail --sponsorblock-mark sponsor,intro,outro

# Custom output template
pyt <url> -o "{author}/{upload_date:%Y-%m-%d} - {title}.{ext}"

# Batch download with archive (skip already downloaded)
pyt --batch-file urls.txt --download-archive archive.txt --sleep-interval 2

# What tools are installed?
pyt --doctor
```

```python
# Modern Python API
from pyt import Client, pipeline as pp

client = Client()
video = client.video("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

path = (
    video.download_best("downloads/", prefer_resolution="1080p")
        | pp.embed_metadata()
        | pp.embed_thumbnail()
).run()
```

## Stability tiers

| Tier | Features | Notes |
|---|---|---|
| **Stable** | All download / merge / metadata / thumbnail / subtitle / SponsorBlock / playlist / search / cookie / proxy / archive features; doctor command | Public API, semver applies |
| **Stable (modern)** | `pyt.Client`, `Video`, `StreamSet`, `Download`, `CombinedDownload`, `pipeline.*` | Replaces the pytube-era `YouTube`/`Playlist`/`Channel`/`Search` classes, removed in v3 |
| **Experimental** | `pp.upscale(...)` (both `algorithm="lanczos"` and `algorithm="realesrgan"`) | API and defaults may change. Emits `FutureWarning` on first use |

## Feature Documentation

**Modern surface (recommended for new code):**

- [Modern Python API (Client / Video / Streams / Pipeline)](features/python-api.md)
- [Combined Adaptive Download](features/combined-download.md)
- [Doctor Command](features/doctor.md) — tool detection + auto-install
- [PO Tokens](features/po-token.md) — handle `ATTESTATION_REQUIRED`
- [Logging & Diagnostics](features/logging.md) — `pyt.enable_logging()`, `-v`/`-vv`, `pyt.diagnostic_report()`
- [Upscaling](features/upscale.md) *(experimental)*

**Per-feature guides:**

- [Audio Extraction](features/audio-extraction.md)
- [Output Templates](features/output-templates.md)
- [SponsorBlock](features/sponsorblock.md)
- [Metadata Embedding](features/metadata-embedding.md)
- [Thumbnail Embedding](features/thumbnail-embedding.md)
- [Subtitle Embedding](features/subtitle-embedding.md)
- [Cookie Authentication](features/cookies-auth.md)
- [Proxy Support](features/proxy-support.md)
- [Archive Tracking](features/archive-tracking.md)
- [Batch Processing](features/batch-processing.md)
- [Configuration Files](features/config-files.md)

## yt-dlp Comparison

See [comparison.md](comparison.md) for a full feature table comparing pyt to yt-dlp.
