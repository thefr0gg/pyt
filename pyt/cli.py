#!/usr/bin/env python3
"""pyt — YouTube downloader CLI."""

import os
import sys
import json
import gzip
import shutil
import logging
import argparse
import time
import random
import datetime as dt
from typing import Dict, List, Optional

from pyt import __version__
from pyt.api import Client, CombinedDownload, DownloadError, PytError, StreamRef, Video
from pyt.api.streams import _resolution_to_int
from pyt.archive import DownloadArchive
from pyt.captions import Caption
from pyt.config import apply_config, load_config
from pyt.helpers import safe_filename, setup_logger, target_directory
from pyt.postprocessors import (
    AudioExtractor,
    EmbedSubtitlePostProcessor,
    EmbedThumbnailPostProcessor,
    FFmpegMetadataEmbedder,
    PostProcessorError,
    SponsorBlockPP,
)
from pyt.postprocessors.sponsorblock import ALL_CATEGORIES
from pyt.template import OutputTemplate

logger = logging.getLogger(__name__)

# ── Terminal colour helpers ───────────────────────────────────────────────────

_COLOUR = sys.stdout.isatty()

if _COLOUR and sys.platform == "win32":
    os.system("")  # activate ENABLE_VIRTUAL_TERMINAL_PROCESSING


def _c(code: str) -> str:
    return f"\033[{code}m" if _COLOUR else ""


R = _c("0")  # reset
B = _c("1")  # bold
DM = _c("2")  # dim
CY = _c("36")  # cyan
GR = _c("32")  # green
YL = _c("33")  # yellow
RD = _c("31")  # red
MG = _c("35")  # magenta
BCY = _c("96")  # bright cyan
BGN = _c("92")  # bright green
BYL = _c("93")  # bright yellow
BRD = _c("91")  # bright red
BWH = _c("97")  # bright white

# ── Output helpers ────────────────────────────────────────────────────────────


def _print_info(label: str, value: str) -> None:
    pad = 10
    sys.stdout.write(f"  {B}{BWH}{label:<{pad}}{R}  {value}\n")


def _print_ok(msg: str) -> None:
    sys.stdout.write(f"\n  {BGN}+{R}  {msg}\n")


def _print_err(msg: str) -> None:
    sys.stdout.write(f"\n  {BRD}x{R}  {msg}\n")


def _print_warn(msg: str) -> None:
    sys.stdout.write(f"  {BYL}!{R}  {msg}\n")


def _print_section(title: str) -> None:
    sys.stdout.write(f"\n  {B}{BCY}{title}{R}\n")


def _fmt_bytes(n: int) -> str:
    """Human-readable byte count."""
    if n < 1024:
        return f"{n} B"
    size = float(n)
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} TB"


def _fmt_seconds(secs: float) -> str:
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _fmt_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ── Progress bar ──────────────────────────────────────────────────────────────

_DL_STATE: dict = {}

_BAR_WIDTH = 40
_BAR_FULL = "━"  # ━
_BAR_EMPTY = "━"  # ━ (dimmed)


def _bar_glyphs() -> tuple:
    """Bar characters the current stdout can encode.

    A redirected stdout on Windows defaults to the ANSI code page (cp1252),
    which has no box-drawing characters; writing ``━`` there raises
    ``UnicodeEncodeError`` and aborts the download.
    """
    try:
        _BAR_FULL.encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return "#", "-"
    return _BAR_FULL, _BAR_EMPTY


def _make_stdout_safe() -> None:
    """Never let an unencodable character (— · … ━) crash the CLI."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def _clear_progress_line() -> None:
    cols = shutil.get_terminal_size().columns
    sys.stdout.write(f"\r{' ' * cols}\r")
    sys.stdout.flush()


def _draw_progress(bytes_recv: int, filesize: int, speed: float, eta: float) -> None:
    pct = min(bytes_recv / filesize, 1.0) if filesize else 0
    filled = int(_BAR_WIDTH * pct)
    empty = _BAR_WIDTH - filled

    full, blank = _bar_glyphs()
    bar = f"{BGN}{full * filled}{R}{DM}{blank * empty}{R}"
    size_str = f"{_fmt_bytes(bytes_recv)}/{_fmt_bytes(filesize)}"
    spd_str = f"{_fmt_bytes(int(speed))}/s" if speed > 0 else ""
    eta_str = f"eta {_fmt_seconds(eta)}" if eta > 0 and pct < 1 else ""

    parts = [s for s in (size_str, spd_str, eta_str) if s]
    meta = "  ".join(parts)

    sys.stdout.write(f"\r  {bar}  {DM}{meta}{R}  ")
    sys.stdout.flush()


def on_progress(stream: StreamRef, chunk: bytes, bytes_remaining: int) -> None:
    filesize = stream.filesize or stream.filesize_approx
    bytes_recv = max(0, filesize - bytes_remaining)
    now = time.monotonic()

    # A merged download streams two formats through the same callback; start
    # the speed/ETA clock over whenever the stream changes.
    if _DL_STATE.get("itag") != stream.itag:
        _DL_STATE.clear()
        _DL_STATE["itag"] = stream.itag
        _DL_STATE["start"] = now

    elapsed = now - _DL_STATE["start"]
    speed = bytes_recv / elapsed if elapsed > 0 else 0
    eta = bytes_remaining / speed if speed > 0 else 0

    _draw_progress(bytes_recv, filesize, speed, eta)


# ── Stream table ──────────────────────────────────────────────────────────────


def display_streams(video: Video) -> None:
    streams = list(video.streams)
    if not streams:
        _print_warn("No streams found.")
        return

    _print_section(f"Available streams — {video.title}")

    col_itag = 6
    col_type = 14
    col_fmt = 6
    col_qual = 10
    col_size = 9

    header = (
        f"  {DM}"
        f"{'itag':>{col_itag}}  "
        f"{'type':<{col_type}}  "
        f"{'fmt':<{col_fmt}}  "
        f"{'quality':<{col_qual}}  "
        f"{'~size':>{col_size}}  "
        f"codec{R}"
    )
    sep = f"  {DM}{'─' * (col_itag + col_type + col_fmt + col_qual + col_size + 18)}{R}"

    sys.stdout.write(f"\n{header}\n{sep}\n")

    for s in streams:
        if s.is_progressive:
            kind = "progressive"
            kind_col = f"{GR}{kind:<{col_type}}{R}"
        elif s.kind == "video":
            kind = "video only"
            kind_col = f"{CY}{kind:<{col_type}}{R}"
        else:
            kind = "audio only"
            kind_col = f"{MG}{kind:<{col_type}}{R}"

        quality = str(s.resolution or s.abr or "—")
        subtype = str(s.subtype or "?")
        try:
            size = _fmt_bytes(s.filesize_approx)
        except (TypeError, ValueError):
            size = "—"

        codecs = []
        if s.video_codec:
            codecs.append(str(s.video_codec))
        if s.audio_codec:
            codecs.append(str(s.audio_codec))
        codec_str = f"{DM}" + " + ".join(codecs) + f"{R}"

        sys.stdout.write(
            f"  {BCY}{s.itag:>{col_itag}}{R}  "
            f"{kind_col}  "
            f"{subtype:<{col_fmt}}  "
            f"{quality:<{col_qual}}  "
            f"{size:>{col_size}}  "
            f"{codec_str}\n"
        )
    sys.stdout.write("\n")


# ── Video info header ─────────────────────────────────────────────────────────


def _show_video_info(video: Video) -> None:
    try:
        title = video.title
        author = video.author.name
        length = _fmt_duration(int(video.length.total_seconds()))
        views = f"{video.views:,}" if video.views is not None else "n/a"
        url = video.url
    except Exception:
        return

    sys.stdout.write("\n")
    _print_info("Title", f"{B}{BWH}{title}{R}")
    _print_info("Author", author)
    _print_info("Length", f"{length}  ·  Views  {views}")
    _print_info("URL", f"{DM}{url}{R}")
    sys.stdout.write("\n")


# ── Post-processor chain ──────────────────────────────────────────────────────


def _parse_categories(raw: str) -> List[str]:
    """Parse comma-separated SponsorBlock categories, or ``"all"``."""
    if raw.strip().lower() == "all":
        return ALL_CATEGORIES
    return [c.strip() for c in raw.split(",") if c.strip()]


def _build_pp_chain(args) -> list:
    """Assemble the ordered list of post-processors from CLI args."""
    pps: list = []

    if not shutil.which("ffmpeg"):
        _needs = []
        if getattr(args, "sponsorblock_mark", None):
            _needs.append("--sponsorblock-mark")
        if getattr(args, "sponsorblock_remove", None):
            _needs.append("--sponsorblock-remove")
        if getattr(args, "extract_audio", False):
            _needs.append("-x / --extract-audio")
        if getattr(args, "embed_metadata", False):
            _needs.append("--embed-metadata")
        if getattr(args, "embed_thumbnail", False):
            _needs.append("--embed-thumbnail")
        if getattr(args, "embed_subs", False):
            _needs.append("--embed-subs")
        if _needs:
            _print_warn(f"ffmpeg not found — skipping: {', '.join(_needs)}")
            return pps

    # SponsorBlock mark (chapters, no re-encode — must run first)
    if getattr(args, "sponsorblock_mark", None):
        cats = _parse_categories(args.sponsorblock_mark)
        try:
            pps.append(SponsorBlockPP(mode="mark", categories=cats))
        except PostProcessorError as e:
            _print_warn(str(e))

    # SponsorBlock remove (re-encodes, so after mark)
    if getattr(args, "sponsorblock_remove", None):
        cats = _parse_categories(args.sponsorblock_remove)
        try:
            pps.append(SponsorBlockPP(mode="remove", categories=cats))
        except PostProcessorError as e:
            _print_warn(str(e))

    # Subtitle embedding
    if getattr(args, "embed_subs", False) and getattr(args, "caption_code", None):
        try:
            pps.append(EmbedSubtitlePostProcessor(lang_code=args.caption_code))
        except PostProcessorError as e:
            _print_warn(str(e))

    # Audio extraction (format conversion)
    if getattr(args, "extract_audio", False):
        fmt = getattr(args, "audio_format", None) or "mp3"
        try:
            pps.append(AudioExtractor(format=fmt))
        except PostProcessorError as e:
            _print_warn(str(e))

    # Thumbnail embedding
    if getattr(args, "embed_thumbnail", False):
        try:
            pps.append(EmbedThumbnailPostProcessor())
        except PostProcessorError as e:
            _print_warn(str(e))

    # Metadata embedding (last — captures any format changes from above)
    if getattr(args, "embed_metadata", False):
        try:
            pps.append(FFmpegMetadataEmbedder())
        except PostProcessorError as e:
            _print_warn(str(e))

    return pps


def _run_pp_chain(pps: list, file_path: str, stream: StreamRef, video: Video) -> str:
    """Run *pps* in sequence, threading the file path through each."""
    current = file_path
    for pp in pps:
        try:
            _print_section(f"Post-processing  {BWH}{pp.__class__.__name__}{R}")
            current = pp.run(current, stream, video)
        except PostProcessorError as exc:
            _print_err(f"{pp.__class__.__name__} failed: {exc}")
    return current


# ── Output template resolution ────────────────────────────────────────────────


def _resolve_output(
    template: Optional[OutputTemplate],
    video: Video,
    stream: StreamRef,
    base_dir: Optional[str],
    playlist_index: Optional[int] = None,
    playlist_title: Optional[str] = None,
):
    """Return (output_dir, filename) honouring the template (if any)."""
    if template is None:
        return base_dir, None

    rendered = template.render(video, stream, playlist_index, playlist_title)
    dir_part, file_part = os.path.split(rendered)

    out_dir: Optional[str]
    if dir_part:
        out_dir = os.path.join(base_dir or ".", dir_part)
        os.makedirs(out_dir, exist_ok=True)
    else:
        out_dir = base_dir

    return out_dir, file_part or None


# ── Stream selection ──────────────────────────────────────────────────────────


def _res_int(stream: StreamRef) -> int:
    return _resolution_to_int(stream.resolution) or 0


def _is_avc(stream: StreamRef) -> bool:
    return bool(stream.video_codec and stream.video_codec.startswith("avc"))


def _by_itag(video: Video, itag: int) -> Optional[StreamRef]:
    return next((s for s in video.streams if s.itag == itag), None)


def _by_resolution(video: Video, resolution: str) -> Optional[StreamRef]:
    candidates = [s for s in video.streams.progressive if s.resolution == resolution]
    return next((s for s in candidates if s.subtype == "mp4"), None) or next(
        iter(candidates), None
    )


def _best_progressive(video: Video) -> Optional[StreamRef]:
    candidates = list(video.streams.progressive)
    if not candidates:
        return None
    return max(candidates, key=lambda s: (_res_int(s), s.bitrate or 0))


def _best_audio(video: Video, subtype: Optional[str] = None) -> Optional[StreamRef]:
    audio = video.streams.audio
    if subtype:
        audio = audio.filter(subtype=subtype)
    if not len(audio):
        return None
    return audio.best()


def _best_video_only(video: Video, resolution: str = "best") -> Optional[StreamRef]:
    """Pick the adaptive video track to merge with audio.

    H.264 (avc1) is preferred at a given resolution: those streams are
    fully cached across YouTube's CDN, while AV1/VP9 may be served from cache
    only for low-view videos.
    """
    adaptive = [s for s in video.streams if s.kind == "video" and s.is_adaptive]
    if resolution == "best":
        pool = [s for s in adaptive if s.subtype == "mp4"] or adaptive
        if not pool:
            return None
        top = max(_res_int(s) for s in pool)
        at_top = [s for s in pool if _res_int(s) == top]
        return next((s for s in at_top if _is_avc(s)), None) or at_top[0]

    at_res = [s for s in adaptive if s.resolution == resolution]
    mp4 = [s for s in at_res if s.subtype == "mp4"]
    return (
        next((s for s in mp4 if _is_avc(s)), None)
        or next(iter(mp4), None)
        or next(iter(at_res), None)
    )


# ── Download helpers ──────────────────────────────────────────────────────────


def _download(
    stream: StreamRef,
    target: Optional[str] = None,
    filename: Optional[str] = None,
    pp_chain: Optional[list] = None,
    video: Optional[Video] = None,
) -> Optional[str]:
    """Download *stream*, run post-processors, return the final file path."""
    _DL_STATE.clear()
    name = filename or stream.default_filename
    try:
        size = _fmt_bytes(stream.filesize_approx)
    except (TypeError, ValueError):
        size = "?"

    _print_section(f"Downloading  {BWH}{name}{R}  {DM}[{size}]{R}")
    sys.stdout.write("\n")

    file_path = os.path.join(target_directory(target), name)
    if (
        stream.filesize
        and os.path.isfile(file_path)
        and os.path.getsize(file_path) == stream.filesize
    ):
        _print_warn(f"Already exists at  {DM}{file_path}{R}")
        if pp_chain and video:
            file_path = _run_pp_chain(pp_chain, file_path, stream, video)
        return file_path

    try:
        file_path = str(stream.download_to(target, filename=filename).run())
    except KeyboardInterrupt:
        _clear_progress_line()
        if os.path.exists(file_path):
            os.unlink(file_path)
        _print_warn("Interrupted — partial file removed.")
        sys.exit(1)
    sys.stdout.write("\n")
    _print_ok(f"Saved to  {DM}{file_path}{R}")

    if pp_chain and video:
        file_path = _run_pp_chain(pp_chain, file_path, stream, video)

    return file_path


# ── Download commands ─────────────────────────────────────────────────────────


def _download_or_interrupt(
    stream: StreamRef,
    target: Optional[str],
    filename: Optional[str],
    pp_chain: Optional[list],
    video: Video,
) -> Optional[str]:
    try:
        return _download(
            stream, target=target, filename=filename, pp_chain=pp_chain, video=video
        )
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        _print_warn("Interrupted.")
        sys.exit(1)


def download_by_itag(
    video: Video,
    itag: int,
    target: Optional[str] = None,
    pp_chain: Optional[list] = None,
    filename: Optional[str] = None,
) -> Optional[str]:
    stream = _by_itag(video, itag)
    if stream is None:
        _print_err(f"No stream with itag {itag}. Available streams:")
        display_streams(video)
        sys.exit(1)
    return _download_or_interrupt(stream, target, filename, pp_chain, video)


def download_by_resolution(
    video: Video,
    resolution: str,
    target: Optional[str] = None,
    pp_chain: Optional[list] = None,
    filename: Optional[str] = None,
) -> Optional[str]:
    stream = _by_resolution(video, resolution)
    if stream is None:
        _print_err(f"No stream at {resolution}. Available streams:")
        display_streams(video)
        sys.exit(1)
    return _download_or_interrupt(stream, target, filename, pp_chain, video)


def download_highest_resolution_progressive(
    video: Video,
    target: Optional[str] = None,
    pp_chain: Optional[list] = None,
    filename: Optional[str] = None,
) -> Optional[str]:
    stream = _best_progressive(video)
    if stream is None:
        _print_err("No progressive stream available. Available streams:")
        display_streams(video)
        sys.exit(1)
    return _download_or_interrupt(stream, target, filename, pp_chain, video)


def download_audio(
    video: Video,
    filetype: str,
    target: Optional[str] = None,
    pp_chain: Optional[list] = None,
    filename: Optional[str] = None,
) -> Optional[str]:
    audio = _best_audio(video, filetype)
    if audio is None:
        _print_err(f"No {filetype} audio stream found. Available streams:")
        display_streams(video)
        sys.exit(1)
    return _download_or_interrupt(audio, target, filename, pp_chain, video)


def download_caption(
    video: Video, lang_code: Optional[str], target: Optional[str] = None
) -> None:
    captions = video.captions
    caption = captions.get(lang_code) if lang_code else None
    if caption is None:
        _print_err(f"No caption with code '{lang_code}'. Available:")
        _print_available_captions(captions)
        return
    path = caption.download(title=video.title, output_path=target)
    _print_ok(f"Caption saved to  {DM}{path}{R}")


def _print_available_captions(captions: Dict[str, Caption]) -> None:
    codes = "  ".join(f"{BCY}{code}{R}" for code in captions)
    sys.stdout.write(f"  {codes}\n")


def ffmpeg_process(
    video: Video,
    resolution: str,
    target: Optional[str] = None,
    pp_chain: Optional[list] = None,
    final_filename: Optional[str] = None,
) -> Optional[str]:
    target = target or os.getcwd()

    video_stream = _best_video_only(video, resolution)
    if video_stream is None:
        _print_err(f"No stream at {resolution}. Available streams:")
        display_streams(video)
        sys.exit(1)

    audio_stream = _best_audio(video, video_stream.subtype) or _best_audio(video)
    if audio_stream is None:
        _print_err("No audio stream found.")
        sys.exit(1)

    return _merged_download(
        video,
        video_stream=video_stream,
        audio_stream=audio_stream,
        target=target,
        pp_chain=pp_chain,
        final_filename=final_filename,
    )


def _merged_download(
    video: Video,
    video_stream: StreamRef,
    audio_stream: StreamRef,
    target: str,
    pp_chain: Optional[list] = None,
    final_filename: Optional[str] = None,
) -> Optional[str]:
    """Fetch both tracks over one session, merge with ffmpeg, post-process."""
    if not shutil.which("ffmpeg"):
        _print_err("ffmpeg not found on PATH; cannot merge.")
        sys.exit(1)

    _DL_STATE.clear()
    _print_section(
        f"Downloading  {BWH}{video_stream.resolution or 'video'} + "
        f"{audio_stream.abr or 'audio'}{R}"
    )
    sys.stdout.write("\n")

    plan = CombinedDownload(
        video=video,
        video_stream=video_stream,
        audio_stream=audio_stream,
        output_path=target,
        filename=final_filename,
    )
    try:
        final_path = str(plan.run())
    except KeyboardInterrupt:
        _clear_progress_line()
        _print_warn("Interrupted.")
        sys.exit(1)
    except DownloadError as exc:
        # The adaptive tracks couldn't be fetched (e.g. the CDN hasn't
        # pre-cached them, common for low-view videos). Fall back to the best
        # progressive stream, which has audio built in.
        _clear_progress_line()
        prog = _best_progressive(video)
        if prog is None:
            _print_err(str(exc))
            sys.exit(1)
        _print_err(
            f"Merged download failed ({exc}) — falling back to progressive "
            f"{prog.resolution} ({prog.mime_type})"
        )
        _DL_STATE.clear()
        name = None
        if final_filename:
            name = f"{os.path.splitext(final_filename)[0]}.{prog.subtype}"
        return _download(
            prog, target=target, filename=name, pp_chain=pp_chain, video=video
        )

    sys.stdout.write("\n")
    _print_ok(f"Merged to  {DM}{final_path}{R}")

    if pp_chain:
        final_path = _run_pp_chain(pp_chain, final_path, video_stream, video)

    return final_path


def build_playback_report(video: Video) -> None:
    ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
    fp = os.path.join(os.getcwd(), f"yt-video-{video.video_id}-{ts}.json.gz")
    with gzip.open(fp, "wb") as fh:
        fh.write(
            json.dumps(
                {
                    "url": video.url,
                    "video_info": video.player_response,
                }
            ).encode("utf-8")
        )
    _print_ok(f"Playback report saved to  {DM}{fp}{R}")


def dump_json(video: Video) -> None:
    """Print video metadata as JSON without downloading."""
    try:
        info = {
            "id": video.video_id,
            "title": video.title,
            "author": video.author.name,
            "channel_id": video.author.id,
            "channel_url": video.author.url,
            "length": int(video.length.total_seconds()),
            "views": video.views,
            "publish_date": (
                video.published_at.isoformat() if video.published_at else None
            ),
            "description": video.description,
            "keywords": video.metadata.keywords,
            "thumbnail_url": video.thumbnail_url,
            "watch_url": video.url,
            "streams": [
                {
                    "itag": s.itag,
                    "mime_type": s.mime_type,
                    "resolution": s.resolution,
                    "fps": s.fps,
                    "abr": s.abr,
                    "filesize": s.filesize_approx,
                    "is_progressive": s.is_progressive,
                }
                for s in video.streams
            ],
        }
    except Exception as exc:
        _print_err(f"Failed to collect video info: {exc}")
        sys.exit(1)

    sys.stdout.write(json.dumps(info, indent=2, ensure_ascii=False) + "\n")


# ── Sleep between downloads ───────────────────────────────────────────────────


def _sleep_between(args) -> None:
    lo = float(getattr(args, "sleep_interval", None) or 0)
    hi = float(getattr(args, "max_sleep_interval", None) or lo)
    if lo <= 0:
        return
    delay = random.uniform(lo, max(lo, hi))
    _print_warn(f"Sleeping {delay:.1f}s before next download…")
    time.sleep(delay)


# ── Help ─────────────────────────────────────────────────────────────────────


def _print_help() -> None:
    W = shutil.get_terminal_size().columns
    rule = f"{DM}{'-' * min(W - 2, 72)}{R}"

    def sec(title: str) -> None:
        sys.stdout.write(f"\n  {B}{BCY}{title}{R}\n")

    def row(flags: str, desc: str, note: str = "") -> None:
        col = 32
        note_str = f"  {DM}{note}{R}" if note else ""
        sys.stdout.write(f"    {GR}{flags:<{col}}{R}{desc}{note_str}\n")

    def gap() -> None:
        sys.stdout.write("\n")

    sys.stdout.write(f"\n  {B}{BWH}pyt{R}  {DM}{__version__}{R}\n")
    sys.stdout.write(f"  {DM}A maintained pytube fork.{R}\n\n")
    sys.stdout.write(f"  {rule}\n")
    sys.stdout.write(f"  {B}Usage{R}  pyt <url> [options]\n")
    sys.stdout.write(f"         pyt --batch-file urls.txt [options]\n")
    sys.stdout.write(f"  {rule}\n")

    sec("What to download")
    row("(default)", "Best quality, merged with ffmpeg when available")
    row("-f, --ffmpeg [RES]", "Merge best video + audio via ffmpeg", "default: best")
    row("-r RES", "Specific resolution  e.g. 720p, 1080p")
    row("-a [FMT]", "Audio-only stream", "default format: mp4")
    row("--itag N", "Pick a stream by itag number")
    row("-l", "List all available streams and exit")

    sec("After download")
    row("-x", "Extract audio track", "needs ffmpeg")
    row(
        "--audio-format FMT",
        "mp3  m4a  aac  flac  opus  vorbis  wav  alac",
        "default: mp3",
    )
    row("--embed-metadata", "Write title, artist, date into the file", "needs ffmpeg")
    row("--embed-thumbnail", "Attach cover art", "needs ffmpeg")
    row("--embed-subs", "Mux subtitle track in  (pair with -c)", "needs ffmpeg")
    row("--sponsorblock-mark CATS", "Add SponsorBlock chapter markers")
    row("--sponsorblock-remove CATS", "Cut SponsorBlock segments out  (re-encodes)")
    sys.stdout.write(
        f"    {DM}categories: sponsor intro outro selfpromo preview filler interaction music_offtopic hook  / all{R}\n"
    )

    sec("Subtitles & captions")
    row("-c LANG", "Download or embed captions  e.g. en, fr, de")
    row("--list-captions", "Show all available caption languages")

    sec("Save as")
    row("-t DIR", "Output directory", "default: current dir")
    row("-o TEMPLATE", "Filename template")
    sys.stdout.write(
        f"    {DM}e.g.  {{author}}/{{upload_date:%Y-%m-%d}} - {{title}}.{{ext}}{R}\n"
    )
    sys.stdout.write(
        f"    {DM}fields: title  id  author  channel_id  upload_date  ext  resolution  fps  abr  filesize  playlist_index  playlist_title{R}\n"
    )
    row("-j", "Print video info as JSON, skip download")

    sec("Connection")
    row("--proxy URL", "socks5://host:port  or  http://user:pass@host:port")
    row("--cookies FILE", "Netscape cookie file")
    row(
        "--cookies-from-browser B",
        "Pull cookies from  chrome  firefox  brave  edge  safari",
    )
    row("--geo-bypass", "Spoof X-Forwarded-For header")
    row("--geo-bypass-country CC", "Specific country code  e.g. US, DE")

    sec("Multiple videos")
    row("--batch-file FILE", "Download every URL in FILE  (one per line, # = comment)")
    row("--download-archive FILE", "Skip IDs already in FILE; append new ones")
    row("--sleep-interval N", "Wait N seconds between downloads")
    row("--max-sleep-interval N", "Random wait between N and max-N seconds")

    sec("Misc")
    row("-v, --verbose", "Debug logging")
    row("--logfile FILE", "Write log to file instead of stderr")
    row("--build-playback-report", "Dump the raw player response for bug reports")
    row("--version", "Print version and exit")

    gap()
    sys.stdout.write(
        f"  {DM}Config file:  ~/.pyt.conf  or  ./pyt.conf  (INI, [default] section){R}\n"
    )
    gap()


# ── Argument parsing ──────────────────────────────────────────────────────────


def _parse_args(
    parser: argparse.ArgumentParser, args: Optional[List] = None
) -> argparse.Namespace:
    parser.add_argument("url", nargs="?")
    parser.add_argument("--version", action="version", version=f"pyt {__version__}")
    parser.add_argument("--itag", type=int, metavar="ITAG")
    parser.add_argument("-r", "--resolution", metavar="RES")
    parser.add_argument("-l", "--list", action="store_true")
    parser.add_argument("-a", "--audio", const="mp4", nargs="?", metavar="FMT")
    parser.add_argument("-f", "--ffmpeg", const="best", nargs="?", metavar="RES")
    parser.add_argument("-x", "--extract-audio", action="store_true")
    parser.add_argument("--audio-format", metavar="FMT", default="mp3")
    parser.add_argument("--embed-metadata", action="store_true")
    parser.add_argument("--embed-thumbnail", action="store_true")
    parser.add_argument("--embed-subs", action="store_true")
    parser.add_argument("--sponsorblock-mark", metavar="CATS")
    parser.add_argument("--sponsorblock-remove", metavar="CATS")
    parser.add_argument("-c", "--caption-code", metavar="LANG")
    parser.add_argument("-lc", "--list-captions", action="store_true")
    parser.add_argument("-t", "--target", metavar="DIR")
    parser.add_argument("-o", "--output", metavar="TEMPLATE")
    parser.add_argument("-j", "--dump-json", action="store_true")
    parser.add_argument("--proxy", metavar="URL")
    parser.add_argument("--cookies", metavar="FILE")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER")
    parser.add_argument(
        "--po-token",
        metavar="TOKEN",
        help="Proof-of-origin token (base64url). Obtain with: yt-dlp --get-po-token <url>",
    )
    parser.add_argument(
        "--po-token-cmd",
        metavar="CMD",
        help=(
            "Command that prints a PO token to stdout. Run on first use "
            "and on every ATTESTATION_REQUIRED retry. Example: "
            "--po-token-cmd 'bgutil-pot --visitor-data $VD'"
        ),
    )
    parser.add_argument(
        "--po-token-script",
        metavar="FILE",
        help=(
            "Path to a JS script that prints a PO token. Runs with the "
            "first available JS runtime on PATH (node / bun / deno). "
            "Run: pyt --doctor to see what's installed."
        ),
    )
    parser.add_argument("--geo-bypass", action="store_true")
    parser.add_argument("--geo-bypass-country", metavar="CC")
    parser.add_argument("--sleep-interval", metavar="N", type=float)
    parser.add_argument("--max-sleep-interval", metavar="N", type=float)
    parser.add_argument("--batch-file", metavar="FILE")
    parser.add_argument("--download-archive", metavar="FILE")
    # `-v` enables DEBUG-level logging; `-vv` adds TRACE (per-chunk /
    # per-HTTP-call detail, very loud).
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="increase log verbosity. -v=DEBUG, -vv=TRACE",
    )
    parser.add_argument("--logfile", metavar="FILE")
    parser.add_argument("--build-playback-report", action="store_true")

    # Doctor: report on installed tools / available features, optionally
    # download missing binaries to ~/.pyt/bin/.
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="report on which tools/features are available",
    )
    parser.add_argument(
        "--install",
        metavar="TOOL",
        help="install a tool to ~/.pyt/bin (use with --doctor). "
        "Choices: ffmpeg, realesrgan, all",
    )

    return parser.parse_args(args)


# ── Single-video processing ───────────────────────────────────────────────────


def _perform_args_on_video(
    video: Video,
    args: argparse.Namespace,
    archive: Optional[DownloadArchive] = None,
    template: Optional[OutputTemplate] = None,
    playlist_index: Optional[int] = None,
    playlist_title: Optional[str] = None,
) -> None:
    # Archive check
    if archive and archive.is_downloaded(video.video_id):
        _print_warn(f"Skipping  {DM}{video.video_id}{R}  (already in archive)")
        return

    no_action = not any(
        [
            args.list,
            args.list_captions,
            args.build_playback_report,
            args.dump_json,
            args.itag,
            args.caption_code,
            args.resolution,
            args.audio,
            args.ffmpeg,
            args.extract_audio,
        ]
    )

    _show_video_info(video)

    if args.dump_json:
        dump_json(video)
        return

    if args.list:
        display_streams(video)
    if args.list_captions:
        _print_section("Available captions")
        _print_available_captions(video.captions)
    if args.build_playback_report:
        build_playback_report(video)

    # Caption download (standalone, not post-processing)
    if args.caption_code and not args.embed_subs:
        download_caption(video=video, lang_code=args.caption_code, target=args.target)

    # Build post-processor chain once
    pp_chain = _build_pp_chain(args)

    def _resolve(stream):
        if template is None:
            return args.target, None
        return _resolve_output(
            template, video, stream, args.target, playlist_index, playlist_title
        )

    def _resolve_if_found(stream):
        if template and stream:
            return _resolve(stream)
        return args.target, None

    if args.itag:
        out_dir, fname = _resolve_if_found(_by_itag(video, args.itag))
        download_by_itag(
            video=video,
            itag=args.itag,
            target=out_dir,
            pp_chain=pp_chain,
            filename=fname,
        )

    if args.resolution:
        out_dir, fname = _resolve_if_found(_by_resolution(video, args.resolution))
        download_by_resolution(
            video=video,
            resolution=args.resolution,
            target=out_dir,
            pp_chain=pp_chain,
            filename=fname,
        )

    if args.audio:
        out_dir, fname = _resolve_if_found(_best_audio(video, args.audio))
        download_audio(
            video=video,
            filetype=args.audio,
            target=out_dir,
            pp_chain=pp_chain,
            filename=fname,
        )

    if args.ffmpeg:
        out_dir, final_name = _resolve_if_found(_best_video_only(video))
        ffmpeg_process(
            video=video,
            resolution=args.ffmpeg,
            target=out_dir,
            pp_chain=pp_chain,
            final_filename=final_name,
        )

    if (
        args.extract_audio
        and not args.audio
        and not args.ffmpeg
        and not args.resolution
        and not args.itag
    ):
        audio_stream = _best_audio(video)
        if audio_stream is None:
            _print_err("No audio stream found.")
            sys.exit(1)
        out_dir, fname = _resolve(audio_stream)
        _download_or_interrupt(audio_stream, out_dir, fname, pp_chain, video)

    if no_action:
        if shutil.which("ffmpeg"):
            out_dir, final_name = _resolve_if_found(_best_video_only(video))
            ffmpeg_process(
                video=video,
                resolution="best",
                target=out_dir,
                pp_chain=pp_chain,
                final_filename=final_name,
            )
        else:
            _print_warn(
                "ffmpeg not found — falling back to best progressive stream (max 720p)"
            )
            out_dir, fname = _resolve_if_found(_best_progressive(video))
            download_highest_resolution_progressive(
                video=video, target=out_dir, pp_chain=pp_chain, filename=fname
            )

    # Mark as downloaded after all actions complete
    if archive:
        archive.mark_downloaded(video.video_id)


# ── Entry point ───────────────────────────────────────────────────────────────


def _iter_batch_urls(path: str) -> List[str]:
    """Read URLs from a batch file (one per line, # comments stripped)."""
    urls = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                urls.append(line)
    return urls


def _resolve_po_token(args) -> Optional[str]:
    """Reduce ``--po-token`` / ``--po-token-cmd`` / ``--po-token-script``
    to a single token string. Runs the generator once at startup, so a
    long-running batch with a stale token would need to be re-run. Use
    :class:`pyt.Client` directly for auto-refresh on ATTESTATION_REQUIRED.
    """
    set_count = sum(
        1
        for v in (
            getattr(args, "po_token", None),
            getattr(args, "po_token_cmd", None),
            getattr(args, "po_token_script", None),
        )
        if v
    )
    if set_count > 1:
        _print_err("pass at most one of: --po-token, --po-token-cmd, --po-token-script")
        sys.exit(1)

    if getattr(args, "po_token", None):
        return args.po_token

    if getattr(args, "po_token_cmd", None):
        from pyt.api.po_token import CommandProvider

        try:
            sys.stdout.write(f"  {DM}Generating PO token via command …{R}\n")
            return CommandProvider(args.po_token_cmd).get()
        except Exception as exc:  # noqa: BLE001
            _print_err(f"po_token command failed: {exc}")
            sys.exit(1)

    if getattr(args, "po_token_script", None):
        from pyt.api.po_token import ScriptProvider

        try:
            sys.stdout.write(f"  {DM}Generating PO token via JS script …{R}\n")
            return ScriptProvider(args.po_token_script).get()
        except Exception as exc:  # noqa: BLE001
            _print_err(f"po_token script failed: {exc}")
            sys.exit(1)

    return None


def _setup_cookies(client: Client, args) -> None:
    """Install the client's cookies into pyt's request layer, if any."""
    source = getattr(args, "cookies", None) or getattr(
        args, "cookies_from_browser", None
    )
    if not source:
        return
    try:
        client._ensure_cookies_installed()
    except Exception as exc:
        what = "cookies" if getattr(args, "cookies", None) else "cookies from browser"
        _print_err(f"Failed to load {what}: {exc}")
        sys.exit(1)
    _print_ok(f"Loaded cookies from  {DM}{source}{R}")


def _setup_geo_bypass(args) -> None:
    """Add X-Forwarded-For header for geo-bypass."""
    if not getattr(args, "geo_bypass", False) and not getattr(
        args, "geo_bypass_country", None
    ):
        return

    import ipaddress
    import random as _random
    from pyt import request

    _COUNTRY_RANGES = {
        "US": "104.16.0.0/12",
        "GB": "185.86.0.0/16",
        "CA": "99.228.0.0/16",
        "AU": "1.120.0.0/13",
        "DE": "80.128.0.0/11",
    }
    cc = (getattr(args, "geo_bypass_country", None) or "US").upper()
    cidr = _COUNTRY_RANGES.get(cc, "104.16.0.0/12")
    net = ipaddress.IPv4Network(cidr)
    fake_ip = str(
        ipaddress.IPv4Address(
            _random.randint(int(net.network_address), int(net.broadcast_address))
        )
    )

    _orig = request._execute_request

    def _patched(url, method=None, headers=None, data=None, timeout=...):
        h = dict(headers or {})
        h["X-Forwarded-For"] = fake_ip
        import socket

        t = socket._GLOBAL_DEFAULT_TIMEOUT if ... is timeout else timeout
        return _orig(url, method=method, headers=h, data=data, timeout=t)

    request._execute_request = _patched
    logger.debug("Geo-bypass active — spoofing IP: %s", fake_ip)


def _is_channel_url(url: str) -> bool:
    """Return True if *url* looks like a YouTube channel (not a video or playlist)."""
    from urllib.parse import urlparse

    path = urlparse(url).path.rstrip("/")
    parts = path.split("/")
    # /@handle  /channel/UCxxx  /c/name  /user/name
    return (
        (
            any(p.startswith("@") for p in parts)
            or "channel" in parts
            or "c" in parts
            or "user" in parts
        )
        and "watch" not in parts
        and "playlist" not in parts
    )


def _run_doctor(args) -> int:
    """Handle ``pyt --doctor [--install TOOL]``. Returns the exit code."""
    from pyt.api import doctor as doc

    if not args.install:
        tools = doc.detect_all()
        features = doc.feature_status(tools)
        print(doc.render_status(tools, features))
        return 0

    targets = (
        ["ffmpeg", "realesrgan", "po-token-generator"]
        if args.install == "all"
        else [args.install]
    )
    for tool in targets:
        _print_section(f"Installing {tool}")

        # po-token-generator runs via npm — no archive download to
        # preview, no progress callback; let doc.install drive its
        # own subprocess output.
        if tool == "po-token-generator":
            try:
                installed = doc.install(tool)
            except doc.InstallError as exc:
                _print_err(f"install failed: {exc}")
                return 1
            _print_ok(f"installed at  {DM}{installed}{R}")
            continue

        try:
            plan = doc.plan_install(tool)
        except doc.InstallError as exc:
            _print_err(str(exc))
            return 1

        sys.stdout.write(f"  source: {plan.url}\n")
        sys.stdout.write(f"  target: {plan.target_dir / plan.binary_name}\n")
        sys.stdout.flush()

        last_pct = [-1]

        def on_progress(done: int, total) -> None:
            if total:
                pct = int(100 * done / total)
                if pct != last_pct[0]:
                    sys.stdout.write(
                        f"\r  download: {pct}%  ({done / 1e6:.1f}/{total / 1e6:.1f} MB)"
                    )
                    sys.stdout.flush()
                    last_pct[0] = pct
            else:
                sys.stdout.write(f"\r  download: {done / 1e6:.1f} MB")
                sys.stdout.flush()

        try:
            installed = doc.install(tool, on_progress=on_progress)
        except doc.InstallError as exc:
            sys.stdout.write("\n")
            _print_err(f"install failed: {exc}")
            return 1

        sys.stdout.write("\n")
        _print_ok(f"installed at  {DM}{installed}{R}")
    return 0


def main() -> None:
    _make_stdout_safe()
    parser = argparse.ArgumentParser(prog="pyt", add_help=False)
    parser.add_argument("-h", "--help", action="store_true")
    args = _parse_args(parser)

    if getattr(args, "help", False):
        _print_help()
        sys.exit(0)

    if getattr(args, "doctor", False):
        sys.exit(_run_doctor(args))

    if args.verbose:
        # `-v` -> DEBUG (most actions, tool decisions, timings)
        # `-vv` -> TRACE (per-chunk SABR, per-HTTP-call, very loud)
        from pyt.api._logging import TRACE

        level = TRACE if args.verbose >= 2 else logging.DEBUG
        setup_logger(level, log_filename=args.logfile)
        # Route the same CLI verbosity through the public log-control API
        # so consumers of pyt.api see the
        # same level the modern API sees.
        from pyt.api._logging import enable_logging

        enable_logging(level=level, file=args.logfile)
        logger.debug("pyt %s (verbosity=%d)", __version__, args.verbose)

    # Merge config file values (CLI flags take precedence)
    apply_config(args, load_config())

    # Resolve PO token once at startup.
    args.po_token = _resolve_po_token(args)

    try:
        client = Client(
            proxy=getattr(args, "proxy", None),
            cookies=getattr(args, "cookies", None),
            cookies_from_browser=getattr(args, "cookies_from_browser", None),
            po_token=args.po_token,
            on_progress=on_progress,
        )
    except PytError as exc:
        _print_err(str(exc))
        sys.exit(1)

    # Network setup. Cookies go in first so geo-bypass wraps the cookie-aware
    # request function rather than being replaced by it.
    _setup_cookies(client, args)
    _setup_geo_bypass(args)

    # Archive
    archive: Optional[DownloadArchive] = None
    if getattr(args, "download_archive", None):
        archive = DownloadArchive(args.download_archive)

    # Output template
    template: Optional[OutputTemplate] = None
    if getattr(args, "output", None):
        template = OutputTemplate(args.output)

    # Collect URLs from positional arg and/or --batch-file
    urls: List[str] = []
    if getattr(args, "batch_file", None):
        if not os.path.isfile(args.batch_file):
            _print_err(f"Batch file not found: {args.batch_file}")
            sys.exit(1)
        urls.extend(_iter_batch_urls(args.batch_file))

    if args.url:
        if "youtu" not in args.url:
            _print_help()
            sys.exit(1)
        urls.append(args.url)

    if not urls:
        _print_help()
        sys.exit(1)

    first = True
    for url in urls:
        if not first:
            _sleep_between(args)
        first = False

        if _is_channel_url(url):
            sys.stdout.write(f"\n  {DM}Loading channel …{R}\n")
            try:
                channel = client.channel(url)
                title = channel.name or url.rstrip("/").split("/")[-1]
                _download_feed(
                    client, channel, title, "channel", args, archive, template
                )
            except Exception as exc:
                _print_err(f"Could not load channel: {exc}")

        elif "/playlist" in url:
            sys.stdout.write(f"\n  {DM}Loading playlist …{R}\n")
            try:
                playlist = client.playlist(url)
                title = playlist.title or "playlist"
                _download_feed(
                    client, playlist, title, "playlist", args, archive, template
                )
            except Exception as exc:
                _print_err(f"Could not load playlist: {exc}")

        else:
            sys.stdout.write(f"\n  {DM}Fetching  {url} …{R}\n")
            try:
                video = client.video(url)
                _perform_args_on_video(
                    video,
                    args,
                    archive=archive,
                    template=template,
                )
            except PytError as exc:
                _print_err(str(exc))
            except Exception as exc:
                _print_err(f"Error: {exc}")


def _download_feed(
    client: Client,
    feed,
    title: str,
    noun: str,
    args: argparse.Namespace,
    archive: Optional[DownloadArchive],
    template: Optional[OutputTemplate],
) -> None:
    """Run the requested actions over every video of a playlist or channel."""
    if not args.target and not template:
        args.target = safe_filename(title)
    video_urls = list(feed.video_urls)
    if not video_urls:
        _print_warn(f"No videos found in this {noun}.")
    for idx, video_url in enumerate(video_urls, start=1):
        try:
            video = client.video(video_url)
            _perform_args_on_video(
                video,
                args,
                archive=archive,
                template=template,
                playlist_index=idx,
                playlist_title=title,
            )
        except PytError as exc:
            _print_err(f"{video_url}  —  {exc}")
        except Exception as exc:
            _print_err(f"{video_url}  —  Error: {exc}")
        if idx < len(video_urls):
            _sleep_between(args)


if __name__ == "__main__":
    main()
