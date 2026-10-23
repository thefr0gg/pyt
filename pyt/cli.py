#!/usr/bin/env python3
"""pyt — YouTube downloader CLI."""

import os
import sys
import json
import gzip
import math
import shutil
import logging
import argparse
import subprocess  # nosec
import time
import random
import warnings
import datetime as dt
from pathlib import Path
import pyt.exceptions as exceptions
from urllib.error import HTTPError

from typing import List, Optional

from pyt import __version__
from pyt.__main__ import YouTube
from pyt.query import CaptionQuery
from pyt.streams import Stream
from pyt.contrib.playlist import Playlist
from pyt.contrib.channel import Channel
from pyt.archive import DownloadArchive
from pyt.config import apply_config, load_config
from pyt.helpers import setup_logger, safe_filename
from pyt.postprocessors import (
    AudioExtractor,
    EmbedSubtitlePostProcessor,
    EmbedThumbnailPostProcessor,
    FFmpegMetadataEmbedder,
    PostProcessorError,
    SponsorBlockPP,
)
from pyt.postprocessors.sponsorblock import ALL_CATEGORIES
from pyt.template import DEFAULT_TEMPLATE, OutputTemplate

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
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} TB"


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


def _clear_progress_line() -> None:
    cols = shutil.get_terminal_size().columns
    sys.stdout.write(f"\r{' ' * cols}\r")
    sys.stdout.flush()


def _draw_progress(bytes_recv: int, filesize: int, speed: float, eta: float) -> None:
    pct = min(bytes_recv / filesize, 1.0) if filesize else 0
    filled = int(_BAR_WIDTH * pct)
    empty = _BAR_WIDTH - filled

    bar = f"{BGN}{_BAR_FULL * filled}{R}{DM}{_BAR_EMPTY * empty}{R}"
    size_str = f"{_fmt_bytes(bytes_recv)}/{_fmt_bytes(filesize)}"
    spd_str = f"{_fmt_bytes(int(speed))}/s" if speed > 0 else ""
    eta_str = f"eta {_fmt_seconds(eta)}" if eta > 0 and pct < 1 else ""

    parts = [s for s in (size_str, spd_str, eta_str) if s]
    meta = "  ".join(parts)

    sys.stdout.write(f"\r  {bar}  {DM}{meta}{R}  ")
    sys.stdout.flush()


def on_progress(stream: Stream, chunk: bytes, bytes_remaining: int) -> None:
    filesize = stream.filesize
    bytes_recv = filesize - bytes_remaining
    now = time.monotonic()

    if not _DL_STATE:
        _DL_STATE["start"] = now

    elapsed = now - _DL_STATE["start"]
    speed = bytes_recv / elapsed if elapsed > 0 else 0
    eta = bytes_remaining / speed if speed > 0 else 0

    _draw_progress(bytes_recv, filesize, speed, eta)


# ── Stream table ──────────────────────────────────────────────────────────────


def display_streams(youtube: YouTube) -> None:
    streams = list(youtube.streams)
    if not streams:
        _print_warn("No streams found.")
        return

    _print_section(f"Available streams — {youtube.title}")

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
        if s.includes_audio_track and s.includes_video_track:
            kind = "progressive"
            kind_col = f"{GR}{kind:<{col_type}}{R}"
        elif s.includes_video_track:
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


def _show_video_info(youtube: YouTube) -> None:
    try:
        title = youtube.title
        author = youtube.author
        length = _fmt_duration(youtube.length)
        views = f"{youtube.views:,}"
        url = youtube.watch_url
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
    pps = []

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


def _run_pp_chain(pps: list, file_path: str, stream: Stream, youtube: YouTube) -> str:
    """Run *pps* in sequence, threading the file path through each."""
    current = file_path
    for pp in pps:
        try:
            _print_section(f"Post-processing  {BWH}{pp.__class__.__name__}{R}")
            current = pp.run(current, stream, youtube)
        except PostProcessorError as exc:
            _print_err(f"{pp.__class__.__name__} failed: {exc}")
    return current


# ── Output template resolution ────────────────────────────────────────────────


def _resolve_output(
    template: Optional[OutputTemplate],
    youtube: YouTube,
    stream: Stream,
    base_dir: Optional[str],
    playlist_index: Optional[int] = None,
    playlist_title: Optional[str] = None,
):
    """Return (output_dir, filename) honouring the template (if any)."""
    if template is None:
        return base_dir, None

    rendered = template.render(youtube, stream, playlist_index, playlist_title)
    dir_part, file_part = os.path.split(rendered)

    if dir_part:
        out_dir = os.path.join(base_dir or ".", dir_part)
        os.makedirs(out_dir, exist_ok=True)
    else:
        out_dir = base_dir

    return out_dir, file_part or None


# ── Download helpers ──────────────────────────────────────────────────────────


def _download(
    stream: Stream,
    target: Optional[str] = None,
    filename: Optional[str] = None,
    pp_chain: Optional[list] = None,
    youtube: Optional[YouTube] = None,
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

    file_path = stream.get_file_path(filename=filename, output_path=target)
    if stream.exists_at_path(file_path):
        _print_warn(f"Already exists at  {DM}{file_path}{R}")
        if pp_chain and youtube:
            file_path = _run_pp_chain(pp_chain, file_path, stream, youtube)
        return file_path

    try:
        file_path = stream.download(output_path=target, filename=filename)
    except KeyboardInterrupt:
        _clear_progress_line()
        if os.path.exists(file_path):
            os.unlink(file_path)
        _print_warn("Interrupted — partial file removed.")
        sys.exit(1)
    sys.stdout.write("\n")
    _print_ok(f"Saved to  {DM}{file_path}{R}")

    if pp_chain and youtube:
        file_path = _run_pp_chain(pp_chain, file_path, stream, youtube)

    return file_path


def _unique_name(base: str, subtype: str, media_type: str, target: str) -> str:
    counter = 0
    while True:
        name = f"{base}_{media_type}_{counter}"
        if not os.path.exists(os.path.join(target, f"{name}.{subtype}")):
            return name
        counter += 1


# ── Download commands ─────────────────────────────────────────────────────────


def download_by_itag(
    youtube: YouTube,
    itag: int,
    target: Optional[str] = None,
    pp_chain: Optional[list] = None,
    filename: Optional[str] = None,
) -> Optional[str]:
    stream = youtube.streams.get_by_itag(itag)
    if stream is None:
        _print_err(f"No stream with itag {itag}. Available streams:")
        display_streams(youtube)
        sys.exit(1)
    youtube.register_on_progress_callback(on_progress)
    try:
        return _download(
            stream, target=target, filename=filename, pp_chain=pp_chain, youtube=youtube
        )
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        _print_warn("Interrupted.")
        sys.exit(1)


def download_by_resolution(
    youtube: YouTube,
    resolution: str,
    target: Optional[str] = None,
    pp_chain: Optional[list] = None,
    filename: Optional[str] = None,
) -> Optional[str]:
    stream = youtube.streams.get_by_resolution(resolution)
    if stream is None:
        _print_err(f"No stream at {resolution}. Available streams:")
        display_streams(youtube)
        sys.exit(1)
    youtube.register_on_progress_callback(on_progress)
    try:
        return _download(
            stream, target=target, filename=filename, pp_chain=pp_chain, youtube=youtube
        )
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        _print_warn("Interrupted.")
        sys.exit(1)


def download_highest_resolution_progressive(
    youtube: YouTube,
    target: Optional[str] = None,
    pp_chain: Optional[list] = None,
    filename: Optional[str] = None,
) -> Optional[str]:
    youtube.register_on_progress_callback(on_progress)
    try:
        stream = youtube.streams.get_highest_resolution()
    except exceptions.VideoUnavailable as err:
        _print_err(f"Video unavailable: {err}")
        sys.exit(1)
    try:
        return _download(
            stream, target=target, filename=filename, pp_chain=pp_chain, youtube=youtube
        )
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        _print_warn("Interrupted.")
        sys.exit(1)


def download_audio(
    youtube: YouTube,
    filetype: str,
    target: Optional[str] = None,
    pp_chain: Optional[list] = None,
    filename: Optional[str] = None,
) -> Optional[str]:
    audio = (
        youtube.streams.filter(only_audio=True, subtype=filetype).order_by("abr").last()
    )
    if audio is None:
        _print_err(f"No {filetype} audio stream found. Available streams:")
        display_streams(youtube)
        sys.exit(1)
    youtube.register_on_progress_callback(on_progress)
    try:
        return _download(
            audio, target=target, filename=filename, pp_chain=pp_chain, youtube=youtube
        )
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        _print_warn("Interrupted.")
        sys.exit(1)


def download_caption(
    youtube: YouTube, lang_code: Optional[str], target: Optional[str] = None
) -> None:
    try:
        caption = youtube.captions[lang_code]
        path = caption.download(title=youtube.title, output_path=target)
        _print_ok(f"Caption saved to  {DM}{path}{R}")
    except KeyError:
        _print_err(f"No caption with code '{lang_code}'. Available:")
        _print_available_captions(youtube.captions)


def _print_available_captions(captions: CaptionQuery) -> None:
    codes = "  ".join(f"{BCY}{c.code}{R}" for c in captions)
    sys.stdout.write(f"  {codes}\n")


def ffmpeg_process(
    youtube: YouTube,
    resolution: str,
    target: Optional[str] = None,
    pp_chain: Optional[list] = None,
    final_filename: Optional[str] = None,
) -> Optional[str]:
    youtube.register_on_progress_callback(on_progress)
    target = target or os.getcwd()

    _is_avc = [lambda s: s.video_codec and s.video_codec.startswith("avc")]

    if resolution == "best":
        # Find the best resolution available, then prefer H.264 (avc1) at that
        # resolution — H.264 streams are fully cached across YouTube's CDN while
        # AV1/VP9 streams may be served from cache only for low-view videos.
        _best = (
            youtube.streams.filter(progressive=False, subtype="mp4")
            .order_by("resolution")
            .last()
        ) or (youtube.streams.filter(progressive=False).order_by("resolution").last())
        if _best:
            video_stream = (
                youtube.streams.filter(
                    progressive=False,
                    subtype="mp4",
                    resolution=_best.resolution,
                    custom_filter_functions=_is_avc,
                ).first()
            ) or _best
        else:
            video_stream = None
    else:
        video_stream = (
            youtube.streams.filter(
                progressive=False,
                resolution=resolution,
                subtype="mp4",
                custom_filter_functions=_is_avc,
            ).first()
            or youtube.streams.filter(
                progressive=False, resolution=resolution, subtype="mp4"
            ).first()
            or youtube.streams.filter(progressive=False, resolution=resolution).first()
        )

    if video_stream is None:
        _print_err(f"No stream at {resolution}. Available streams:")
        display_streams(youtube)
        sys.exit(1)

    audio_stream = (
        youtube.streams.get_audio_only(video_stream.subtype)
        or youtube.streams.filter(only_audio=True).order_by("abr").last()
    )
    if audio_stream is None:
        _print_err("No audio stream found.")
        sys.exit(1)

    return _ffmpeg_downloader(
        audio_stream=audio_stream,
        video_stream=video_stream,
        target=target,
        pp_chain=pp_chain,
        youtube=youtube,
        final_filename=final_filename,
    )


def _ffmpeg_downloader(
    audio_stream: Stream,
    video_stream: Stream,
    target: str,
    pp_chain: Optional[list] = None,
    youtube: Optional[YouTube] = None,
    final_filename: Optional[str] = None,
) -> Optional[str]:
    base = safe_filename(video_stream.title)
    video_name = _unique_name(base, video_stream.subtype, "video", target)
    audio_name = _unique_name(base, audio_stream.subtype, "audio", target)

    video_filename = f"{video_name}.{video_stream.subtype}"
    audio_filename = f"{audio_name}.{audio_stream.subtype}"
    video_path = os.path.join(target, video_filename)
    try:
        _download(stream=video_stream, target=target, filename=video_filename)
    except HTTPError as e:
        if e.code != 403 or youtube is None:
            raise
        # CDN hasn't pre-cached this stream (common for low-view videos).
        # Clean up the partial file and fall back to the best progressive stream.
        _DL_STATE.clear()
        if os.path.exists(video_path):
            os.unlink(video_path)
        prog = youtube.streams.get_highest_resolution()
        if prog is None:
            raise
        _print_err(
            f"DASH stream unavailable (403) — falling back to progressive "
            f"{prog.resolution} ({prog.mime_type})"
        )
        prog_name = _unique_name(base, prog.subtype, "progressive", target)
        prog_filename = f"{prog_name}.{prog.subtype}"
        _download(stream=prog, target=target, filename=prog_filename)
        prog_path = os.path.join(target, prog_filename)
        final_name = final_filename or f"{base}.{prog.subtype}"
        final_path = os.path.join(target, final_name)
        shutil.move(prog_path, final_path)
        _print_ok(f"Saved to   {DM}{final_path}{R}")
        if pp_chain and youtube:
            final_path = _run_pp_chain(pp_chain, final_path, prog, youtube)
        return final_path

    _DL_STATE.clear()
    _download(stream=audio_stream, target=target, filename=audio_filename)

    audio_path = os.path.join(target, audio_filename)

    # Pick a container that actually accepts both codecs. Without this, a
    # vp9+m4a or avc1+opus combo silently produces a broken file because
    # `-codec copy` can't transmux into an incompatible container.
    merge_ext = _pick_merge_container(video_stream, audio_stream)
    if final_filename:
        # Caller asked for a specific filename. If the requested extension is
        # incompatible with the codecs, rewrite it to the safe one — the
        # alternative is producing a "playable file" that isn't.
        stem, ext = os.path.splitext(final_filename)
        if ext.lstrip(".").lower() != merge_ext:
            final_filename = f"{stem}.{merge_ext}"
    else:
        final_filename = f"{base}.{merge_ext}"
    final_path = os.path.join(target, final_filename)

    _print_section("Merging with ffmpeg")
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        _print_err("ffmpeg not found on PATH; cannot merge.")
        sys.exit(1)
    cmd = [
        ffmpeg_bin,
        "-y",
        "-loglevel",
        "warning",
        "-nostats",
        # Drop corrupt packets rather than halting on the first bad one.
        # Without these, libdav1d giving up on a truncated AV1 stream
        # leaves the muxer with a 4-second output from a 2-minute source
        # (silently — exit code is still 0). See pyt.api._merge for the
        # same hardening in the modern path.
        "-fflags",
        "+discardcorrupt",
        "-err_detect",
        "ignore_err",
        "-i",
        video_path,
        "-i",
        audio_path,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
    ]
    if merge_ext == "mp4":
        cmd += ["-movflags", "+faststart"]
    cmd.append(final_path)

    result = subprocess.run(cmd, capture_output=True, check=False)  # nosec
    if result.returncode != 0:
        # Leave the source files behind so the user can retry / inspect.
        _print_err(
            f"ffmpeg merge failed (exit {result.returncode}):\n"
            + result.stderr.decode("utf-8", errors="replace")
        )
        if os.path.exists(final_path):
            try:
                os.unlink(final_path)
            except OSError:
                pass
        sys.exit(1)

    # ffmpeg returned 0 — but with -c copy + libdav1d/libaom, an exit code
    # of 0 doesn't actually mean "muxed everything." Compare durations to
    # catch the silently-truncated case where we'd otherwise tell the user
    # "Saved" for a 4-second clip from a 2-minute video.
    from pyt.api._merge import probe_duration

    out_dur = probe_duration(Path(final_path))
    if out_dur is not None:
        in_durs = [probe_duration(Path(p)) for p in (video_path, audio_path)]
        in_durs = [d for d in in_durs if d is not None]
        if in_durs:
            expected = max(in_durs)
            if expected > 0 and out_dur < expected * 0.9:
                _print_err(
                    f"merge produced {out_dur:.1f}s of output but the inputs "
                    f"are ~{expected:.1f}s — the video stream is likely "
                    f"corrupt or truncated. Source files preserved at:\n"
                    f"  {video_path}\n  {audio_path}\n"
                    f"Try re-downloading."
                )
                try:
                    os.unlink(final_path)
                except OSError:
                    pass
                sys.exit(1)

    # Merge succeeded — now safe to drop sources.
    for p in (video_path, audio_path):
        try:
            os.unlink(p)
        except OSError as e:
            logger.warning("could not delete %s: %s", p, e)
    _print_ok(f"Merged to  {DM}{final_path}{R}")

    if pp_chain and youtube:
        final_path = _run_pp_chain(pp_chain, final_path, video_stream, youtube)

    return final_path


def _pick_merge_container(video_stream: Stream, audio_stream: Stream) -> str:
    """Pick the right container for muxing *video_stream* and *audio_stream*.

    YouTube serves video as one of: avc1 (H.264) in mp4, vp9 in webm, av01 in mp4.
    Audio: mp4a/aac in m4a, opus in webm.
    `-codec copy` requires the container to accept both codecs. mp4 takes
    avc1+aac and av01+aac; webm takes vp9+opus. mkv accepts everything and is
    the safe fallback for cross-family combinations.
    """
    v = (video_stream.video_codec or "").lower()
    a = (audio_stream.audio_codec or "").lower()
    is_avc = v.startswith("avc")
    is_av1 = v.startswith("av01") or v.startswith("av1")
    is_vp9 = v.startswith("vp9") or v.startswith("vp09")
    is_aac = a.startswith("mp4a") or a.startswith("aac")
    is_opus = a.startswith("opus")

    if (is_avc or is_av1) and is_aac:
        return "mp4"
    if is_vp9 and is_opus:
        return "webm"
    return "mkv"


def build_playback_report(youtube: YouTube) -> None:
    ts = int(dt.datetime.now(dt.UTC).timestamp())
    fp = os.path.join(os.getcwd(), f"yt-video-{youtube.video_id}-{ts}.json.gz")
    with gzip.open(fp, "wb") as fh:
        fh.write(
            json.dumps(
                {
                    "url": youtube.watch_url,
                    "js": youtube.js,
                    "watch_html": youtube.watch_html,
                    "video_info": youtube.vid_info,
                }
            ).encode("utf-8")
        )
    _print_ok(f"Playback report saved to  {DM}{fp}{R}")


def dump_json(youtube: YouTube) -> None:
    """Print video metadata as JSON without downloading."""
    try:
        info = {
            "id": youtube.video_id,
            "title": youtube.title,
            "author": youtube.author,
            "channel_id": youtube.channel_id,
            "channel_url": youtube.channel_url,
            "length": youtube.length,
            "views": youtube.views,
            "publish_date": (
                youtube.publish_date.isoformat() if youtube.publish_date else None
            ),
            "description": youtube.description,
            "keywords": youtube.keywords,
            "thumbnail_url": youtube.thumbnail_url,
            "watch_url": youtube.watch_url,
            "streams": [
                {
                    "itag": s.itag,
                    "mime_type": s.mime_type,
                    "resolution": s.resolution,
                    "fps": getattr(s, "fps", None),
                    "abr": s.abr,
                    "filesize": s.filesize_approx,
                    "is_progressive": s.is_progressive,
                }
                for s in youtube.streams
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
    row("--build-playback-report", "Dump raw page data for bug reports")
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


def _perform_args_on_youtube(
    youtube: YouTube,
    args: argparse.Namespace,
    archive: Optional[DownloadArchive] = None,
    template: Optional[OutputTemplate] = None,
    playlist_index: Optional[int] = None,
    playlist_title: Optional[str] = None,
) -> None:
    # Availability check — raises a descriptive PytError subclass if the video
    # cannot be played (private, members-only, region-blocked, live, etc.).
    # Non-VideoUnavailable failures (network, parse errors) are ignored here
    # and will surface naturally when streams are accessed.
    try:
        youtube.check_availability()
    except exceptions.VideoUnavailable as exc:
        _print_err(str(exc))
        return
    except Exception:
        pass

    # Archive check
    if archive and archive.is_downloaded(youtube.video_id):
        _print_warn(f"Skipping  {DM}{youtube.video_id}{R}  (already in archive)")
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

    _show_video_info(youtube)

    if args.dump_json:
        dump_json(youtube)
        return

    if args.list:
        display_streams(youtube)
    if args.list_captions:
        _print_section("Available captions")
        _print_available_captions(youtube.captions)
    if args.build_playback_report:
        build_playback_report(youtube)

    # Caption download (standalone, not post-processing)
    if args.caption_code and not args.embed_subs:
        download_caption(
            youtube=youtube, lang_code=args.caption_code, target=args.target
        )

    # Build post-processor chain once
    pp_chain = _build_pp_chain(args)

    def _resolve(stream):
        if template is None:
            return args.target, None
        return _resolve_output(
            template, youtube, stream, args.target, playlist_index, playlist_title
        )

    if args.itag:
        if template:
            s = youtube.streams.get_by_itag(args.itag)
            out_dir, fname = _resolve(s) if s else (args.target, None)
        else:
            out_dir, fname = args.target, None
        download_by_itag(
            youtube=youtube,
            itag=args.itag,
            target=out_dir,
            pp_chain=pp_chain,
            filename=fname,
        )

    if args.resolution:
        if template:
            s = youtube.streams.get_by_resolution(args.resolution)
            out_dir, fname = _resolve(s) if s else (args.target, None)
        else:
            out_dir, fname = args.target, None
        download_by_resolution(
            youtube=youtube,
            resolution=args.resolution,
            target=out_dir,
            pp_chain=pp_chain,
            filename=fname,
        )

    if args.audio:
        if template:
            s = (
                youtube.streams.filter(only_audio=True, subtype=args.audio)
                .order_by("abr")
                .last()
            )
            out_dir, fname = _resolve(s) if s else (args.target, None)
        else:
            out_dir, fname = args.target, None
        download_audio(
            youtube=youtube,
            filetype=args.audio,
            target=out_dir,
            pp_chain=pp_chain,
            filename=fname,
        )

    if args.ffmpeg:
        if template:
            vs = (
                youtube.streams.filter(progressive=False, subtype="mp4")
                .order_by("resolution")
                .last()
                or youtube.streams.filter(progressive=False)
                .order_by("resolution")
                .last()
            )
            out_dir, final_name = _resolve(vs) if vs else (args.target, None)
        else:
            out_dir, final_name = args.target, None
        ffmpeg_process(
            youtube=youtube,
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
        audio_stream = youtube.streams.filter(only_audio=True).order_by("abr").last()
        if audio_stream is None:
            _print_err("No audio stream found.")
            sys.exit(1)
        out_dir, fname = _resolve(audio_stream)
        youtube.register_on_progress_callback(on_progress)
        try:
            _download(
                audio_stream,
                target=out_dir,
                filename=fname,
                pp_chain=pp_chain,
                youtube=youtube,
            )
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            _print_warn("Interrupted.")
            sys.exit(1)

    if no_action:
        if shutil.which("ffmpeg"):
            if template:
                vs = (
                    youtube.streams.filter(progressive=False, subtype="mp4")
                    .order_by("resolution")
                    .last()
                    or youtube.streams.filter(progressive=False)
                    .order_by("resolution")
                    .last()
                )
                out_dir, final_name = _resolve(vs) if vs else (args.target, None)
            else:
                out_dir, final_name = args.target, None
            ffmpeg_process(
                youtube=youtube,
                resolution="best",
                target=out_dir,
                pp_chain=pp_chain,
                final_filename=final_name,
            )
        else:
            _print_warn(
                "ffmpeg not found — falling back to best progressive stream (max 720p)"
            )
            if template:
                s = youtube.streams.get_highest_resolution()
                out_dir, fname = _resolve(s) if s else (args.target, None)
            else:
                out_dir, fname = args.target, None
            download_highest_resolution_progressive(
                youtube=youtube, target=out_dir, pp_chain=pp_chain, filename=fname
            )

    # Mark as downloaded after all actions complete
    if archive:
        archive.mark_downloaded(youtube.video_id)


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
    to a single token string. Runs the generator once at startup; the
    legacy CLI's :class:`YouTube` doesn't support auto-retry, so a
    long-running download with a stale token would need to be re-run.
    Use the modern :class:`pyt.Client` API for auto-retry.
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


def _setup_cookies(args) -> None:
    """Install cookies into pyt's request layer if requested."""
    if getattr(args, "cookies", None):
        from pyt.cookies import install_cookies, load_cookies_from_file

        try:
            install_cookies(load_cookies_from_file(args.cookies))
            _print_ok(f"Loaded cookies from  {DM}{args.cookies}{R}")
        except Exception as exc:
            _print_err(f"Failed to load cookies: {exc}")
            sys.exit(1)
    elif getattr(args, "cookies_from_browser", None):
        from pyt.cookies import install_cookies, load_cookies_from_browser

        try:
            install_cookies(load_cookies_from_browser(args.cookies_from_browser))
            _print_ok(f"Loaded cookies from  {DM}{args.cookies_from_browser}{R}")
        except Exception as exc:
            _print_err(f"Failed to load cookies from browser: {exc}")
            sys.exit(1)


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
        # Modern API uses the public log-control API; route the same
        # CLI verbosity through it so consumers of pyt.api see the
        # same level the legacy paths see.
        from pyt.api._logging import enable_logging

        enable_logging(level=level, file=args.logfile)
        logger.debug("pyt %s (verbosity=%d)", __version__, args.verbose)

    # Merge config file values (CLI flags take precedence)
    apply_config(args, load_config())

    # Network setup
    _setup_cookies(args)
    _setup_geo_bypass(args)

    # Resolve PO token once at startup. We mutate args.po_token in place
    # so the existing legacy code path below still uses `args.po_token`
    # without further changes.
    args.po_token = _resolve_po_token(args)

    # Proxy (proxies dict is passed per-YouTube instance below)
    proxies = None
    if getattr(args, "proxy", None):
        proxies = {"http": args.proxy, "https": args.proxy}

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
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    channel = Channel(url)
                playlist_title = (
                    getattr(channel, "channel_name", None)
                    or url.rstrip("/").split("/")[-1]
                )
                if not args.target and not template:
                    args.target = safe_filename(playlist_title)
                video_urls = list(channel.video_urls)
                if not video_urls:
                    _print_warn("No videos found on this channel.")
                for idx, video in enumerate(channel.videos, start=1):
                    try:
                        _perform_args_on_youtube(
                            video,
                            args,
                            archive=archive,
                            template=template,
                            playlist_index=idx,
                            playlist_title=playlist_title,
                        )
                    except exceptions.PytError as exc:
                        _print_err(f"{video.watch_url}  —  {exc}")
                    if idx < len(video_urls):
                        _sleep_between(args)
            except Exception as exc:
                _print_err(f"Could not load channel: {exc}")

        elif "/playlist" in url:
            sys.stdout.write(f"\n  {DM}Loading playlist …{R}\n")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    playlist = Playlist(url)
                playlist_title = getattr(playlist, "title", None)
                if not args.target and not template:
                    args.target = safe_filename(playlist_title or "playlist")
                video_urls = list(playlist.video_urls)
                if not video_urls:
                    _print_warn("No videos found in this playlist.")
                for idx, video in enumerate(playlist.videos, start=1):
                    try:
                        _perform_args_on_youtube(
                            video,
                            args,
                            archive=archive,
                            template=template,
                            playlist_index=idx,
                            playlist_title=playlist_title,
                        )
                    except exceptions.PytError as exc:
                        _print_err(f"{video.watch_url}  —  {exc}")
                    if idx < len(video_urls):
                        _sleep_between(args)
            except exceptions.PytError as exc:
                _print_err(f"Could not load playlist: {exc}")
            except Exception as exc:
                _print_err(f"Could not load playlist: {exc}")

        else:
            sys.stdout.write(f"\n  {DM}Fetching  {url} …{R}\n")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    youtube = YouTube(url, proxies=proxies, po_token=args.po_token)
                _perform_args_on_youtube(
                    youtube,
                    args,
                    archive=archive,
                    template=template,
                )
            except exceptions.VideoUnavailable as exc:
                _print_err(str(exc))
            except exceptions.PytError as exc:
                _print_err(str(exc))
            except Exception as exc:
                _print_err(f"Error: {exc}")


if __name__ == "__main__":
    main()
