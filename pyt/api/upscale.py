"""Experimental video upscaling post-processor.

Two algorithms, picked by ``algorithm=`` on :func:`pyt.api.pipeline.upscale`:

``"lanczos"`` (default)
    Pure ffmpeg: ``scale`` filter with the Lanczos resampler followed
    by a conservative ``unsharp`` pass to compensate for Lanczos's
    natural edge softening. Single ffmpeg invocation — no frame
    extraction, no temp-dir explosion. Real-time or faster on any
    modern CPU. **No extra install.** Best at 2× (360→720, 720→1440);
    quality drops above that. Doesn't add detail the source doesn't
    have, but produces a noticeably cleaner result than naive bilinear
    or YouTube's player upscaling.

``"realesrgan"``
    `Real-ESRGAN <https://github.com/xinntao/Real-ESRGAN>`_ via the
    standalone ``realesrgan-ncnn-vulkan`` binary. Neural-net detail
    recovery; native 4× ratio. The pipeline processes the video in
    N-second chunks (default 30s) so peak disk usage is bounded by
    chunk size rather than the whole video — a 5-minute 720p clip
    peaks at ~6 GB instead of ~45 GB. Each chunk: extract frames →
    upscale → re-encode to a small mp4 → drop the PNGs. Final step:
    ffmpeg concat demuxer (no re-encode) + remux original audio.

The whole feature is experimental and emits a one-shot
:class:`FutureWarning` on first use. API and defaults may change.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess  # nosec — argv is fully constructed by us
import tempfile
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from pyt.api.errors import PostProcessError
from pyt.api.pipeline import PipelineStep

if TYPE_CHECKING:
    from pyt.api.streams import StreamRef
    from pyt.api.video import Video


logger = logging.getLogger(__name__)

_EXPERIMENTAL_NOTICE_FIRED = False
_VALID_ALGORITHMS = ("lanczos", "realesrgan")


def _emit_experimental_warning_once() -> None:
    global _EXPERIMENTAL_NOTICE_FIRED
    if _EXPERIMENTAL_NOTICE_FIRED:
        return
    _EXPERIMENTAL_NOTICE_FIRED = True
    warnings.warn(
        "pyt.pipeline.upscale is experimental: API and defaults may change, "
        "and output quality varies by algorithm and source content. See README.",
        FutureWarning,
        stacklevel=4,
    )


# Algorithm-specific defaults / metadata.
_REALESRGAN_BINARY = "realesrgan-ncnn-vulkan"
_REALESRGAN_KNOWN_MODELS = {
    "realesrgan-x4plus",
    "realesrgan-x4plus-anime",
    "realesr-animevideov3",
    "realesrnet-x4plus",
}


@dataclass(frozen=True)
class _Upscale(PipelineStep):
    """Internal pipeline step. Constructed by
    :func:`pyt.api.pipeline.upscale`; not meant to be instantiated directly."""

    algorithm: str = "lanczos"
    scale: int = 2

    # ── lanczos kwargs ─────────────────────────────────────────────────────
    crf: int = 18  # x264 CRF for the re-encode
    preset: str = "medium"  # x264 speed/efficiency preset
    sharpen: float = 0.4  # unsharp amount; 0 disables the sharpen pass

    # ── realesrgan kwargs ──────────────────────────────────────────────────
    model: str = "realesrgan-x4plus"
    binary: Optional[str] = None
    tile_size: int = 0  # 0 = auto. Lower if GPU OOMs.
    keep_intermediate: bool = False
    chunk_seconds: int = 30  # 0 disables chunking (process whole video in one shot)
    threads: Optional[str] = None  # ncnn -j load:proc:save; None = binary default

    def apply(self, path: str, *, stream: "StreamRef", video: "Video") -> str:
        _emit_experimental_warning_once()
        if self.algorithm == "lanczos":
            return _run_lanczos(
                input_path=Path(path),
                scale=self.scale,
                crf=self.crf,
                preset=self.preset,
                sharpen=self.sharpen,
            )
        if self.algorithm == "realesrgan":
            return _run_realesrgan(
                input_path=Path(path),
                scale=self.scale,
                model=self.model,
                binary_override=self.binary,
                tile_size=self.tile_size,
                keep_intermediate=self.keep_intermediate,
                chunk_seconds=self.chunk_seconds,
                threads=self.threads,
            )
        raise PostProcessError(
            f"upscale: unknown algorithm {self.algorithm!r} "
            f"(choose from {sorted(_VALID_ALGORITHMS)})",
            step="upscale",
            partial_output_path=path,
        )


# ── shared helpers ────────────────────────────────────────────────────────


def _check_input(input_path: Path, scale: int, allowed_scales=(2, 3, 4)) -> None:
    if scale not in allowed_scales:
        raise PostProcessError(
            f"upscale: scale must be one of {allowed_scales} (got {scale!r})",
            step="upscale",
            partial_output_path=str(input_path),
        )
    if not input_path.is_file():
        raise PostProcessError(
            f"upscale: input file not found: {input_path}",
            step="upscale",
        )


def _atomic_swap(input_path: Path, new_file: Path) -> None:
    """Replace *input_path* with *new_file* via a .pre-upscale backup.

    On any os.replace failure we roll the backup back so the user's
    original bytes survive.
    """
    backup = input_path.with_suffix(input_path.suffix + ".pre-upscale")
    os.replace(input_path, backup)
    try:
        os.replace(new_file, input_path)
    except OSError:
        os.replace(backup, input_path)
        raise
    backup.unlink(missing_ok=True)


def _translate_subprocess_failure(
    exc: subprocess.CalledProcessError, partial: Path
) -> None:
    stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
    raise PostProcessError(
        f"upscale: external command failed ({exc.cmd[0]}): "
        f"exit {exc.returncode}\n{stderr.strip()[:600]}",
        step="upscale",
        partial_output_path=str(partial),
        cause=exc,
    ) from exc


# ── lanczos algorithm ─────────────────────────────────────────────────────


def _run_lanczos(
    *,
    input_path: Path,
    scale: int,
    crf: int,
    preset: str,
    sharpen: float,
) -> str:
    _check_input(input_path, scale, allowed_scales=(2, 3, 4))
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise PostProcessError(
            "ffmpeg not found on PATH (required for lanczos upscaling)",
            step="upscale",
            partial_output_path=str(input_path),
        )

    # Build the ffmpeg filter chain. Lanczos for the resize, optional
    # unsharp pass to recover the edge sharpness lanczos blurs.
    filters = [f"scale=iw*{scale}:ih*{scale}:flags=lanczos"]
    if sharpen > 0:
        # unsharp luma_msize_x:luma_msize_y:luma_amount:chroma_msize_x:chroma_msize_y:chroma_amount
        # Conservative defaults — the goal is "less soft", not "looks fake".
        filters.append(f"unsharp=5:5:{sharpen:.2f}:3:3:{(sharpen * 0.5):.2f}")
    vf = ",".join(filters)

    workdir = Path(tempfile.mkdtemp(prefix="pyt-upscale-", dir=str(input_path.parent)))
    output_tmp = workdir / f"out{input_path.suffix}"
    try:
        cmd = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            str(output_tmp),
        ]
        logger.debug("upscale[lanczos]: %s", " ".join(cmd))
        try:
            subprocess.run(cmd, capture_output=True, check=True)  # nosec
        except subprocess.CalledProcessError as exc:
            _translate_subprocess_failure(exc, input_path)

        _atomic_swap(input_path, output_tmp)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return str(input_path)


# ── realesrgan algorithm ──────────────────────────────────────────────────


def _run_realesrgan(
    *,
    input_path: Path,
    scale: int,
    model: str,
    binary_override: Optional[str],
    tile_size: int,
    keep_intermediate: bool,
    chunk_seconds: int = 30,
    threads: Optional[str] = None,
) -> str:
    _check_input(input_path, scale, allowed_scales=(2, 3, 4))

    binary = binary_override or shutil.which(_REALESRGAN_BINARY)
    if not binary:
        raise PostProcessError(
            "realesrgan-ncnn-vulkan not found on PATH. "
            "Install from https://github.com/xinntao/Real-ESRGAN/releases "
            "and ensure the binary is on PATH (or pass binary=...).",
            step="upscale",
            partial_output_path=str(input_path),
        )
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise PostProcessError(
            "ffmpeg not found on PATH (required to extract / recombine frames)",
            step="upscale",
            partial_output_path=str(input_path),
        )

    if model not in _REALESRGAN_KNOWN_MODELS:
        # Don't hard-error — Real-ESRGAN ships with a model registry the user
        # can extend. Just warn so the typo'd-model case is loud.
        logger.warning(
            "upscale[realesrgan]: model %r not in known set %s; passing through verbatim",
            model,
            sorted(_REALESRGAN_KNOWN_MODELS),
        )

    fps = _probe_fps(input_path) or 30.0
    has_audio = _probe_has_audio(input_path)

    workdir = Path(tempfile.mkdtemp(prefix="pyt-upscale-", dir=str(input_path.parent)))
    output_tmp = workdir / f"out{input_path.suffix}"

    try:
        try:
            if chunk_seconds and chunk_seconds > 0:
                _realesrgan_chunked(
                    ffmpeg=ffmpeg,
                    binary=binary,
                    workdir=workdir,
                    input_path=input_path,
                    output_tmp=output_tmp,
                    scale=scale,
                    model=model,
                    tile_size=tile_size,
                    threads=threads,
                    has_audio=has_audio,
                    fps=fps,
                    chunk_seconds=chunk_seconds,
                )
            else:
                _realesrgan_single_shot(
                    ffmpeg=ffmpeg,
                    binary=binary,
                    workdir=workdir,
                    input_path=input_path,
                    output_tmp=output_tmp,
                    scale=scale,
                    model=model,
                    tile_size=tile_size,
                    threads=threads,
                    has_audio=has_audio,
                    fps=fps,
                )
        except subprocess.CalledProcessError as exc:
            _translate_subprocess_failure(exc, input_path)

        _atomic_swap(input_path, output_tmp)
    finally:
        if not keep_intermediate:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            logger.info("upscale[realesrgan]: kept intermediates at %s", workdir)

    return str(input_path)


# ── chunked vs single-shot orchestration ──────────────────────────────────


def _realesrgan_single_shot(
    *,
    ffmpeg: str,
    binary: str,
    workdir: Path,
    input_path: Path,
    output_tmp: Path,
    scale: int,
    model: str,
    tile_size: int,
    threads: Optional[str],
    has_audio: bool,
    fps: float,
) -> None:
    """Original whole-video path. Kept as ``chunk_seconds=0`` for users who
    prefer the simpler flow and have plenty of disk."""
    frames_dir = workdir / "frames"
    upscaled_dir = workdir / "upscaled"
    frames_dir.mkdir()
    upscaled_dir.mkdir()

    _extract_frames(ffmpeg, input_path, frames_dir)
    _upscale_frames_realesrgan(
        binary,
        frames_dir,
        upscaled_dir,
        model=model,
        scale=scale,
        tile_size=tile_size,
        threads=threads,
    )
    _recombine(
        ffmpeg,
        frames_dir=upscaled_dir,
        audio_source=input_path if has_audio else None,
        output=output_tmp,
        fps=fps,
    )


def _realesrgan_chunked(
    *,
    ffmpeg: str,
    binary: str,
    workdir: Path,
    input_path: Path,
    output_tmp: Path,
    scale: int,
    model: str,
    tile_size: int,
    threads: Optional[str],
    has_audio: bool,
    fps: float,
    chunk_seconds: int,
) -> None:
    """Process the video in N-second segments to bound peak disk usage.

    Per chunk:
      1. Extract just that chunk's frames (PNG)
      2. Upscale them
      3. Re-encode to a video-only mp4 in workdir/chunks/
      4. Drop both PNG dirs

    After all chunks land:
      5. ffmpeg concat demuxer (`-c copy`, no re-encode) merges chunks
      6. Remux the original audio track in (also copy)
    """
    duration = _probe_duration(input_path)
    if not duration or duration <= 0:
        # Couldn't probe — fall back to single-shot rather than make N=1 a
        # fake chunk (the extra concat invocation is wasted work on a tiny clip).
        logger.info("upscale: could not probe duration; falling back to single-shot")
        return _realesrgan_single_shot(
            ffmpeg=ffmpeg,
            binary=binary,
            workdir=workdir,
            input_path=input_path,
            output_tmp=output_tmp,
            scale=scale,
            model=model,
            tile_size=tile_size,
            threads=threads,
            has_audio=has_audio,
            fps=fps,
        )

    chunk_dir = workdir / "chunks"
    chunk_dir.mkdir()

    n_chunks = max(1, int((duration + chunk_seconds - 1) // chunk_seconds))
    chunk_videos: List[Path] = []
    logger.info(
        "upscale[realesrgan]: %d chunks of ~%ds (duration=%.1fs)",
        n_chunks,
        chunk_seconds,
        duration,
    )

    # Reusable scratch directories so we never hold more than one chunk's
    # frames on disk at a time.
    frames_dir = workdir / "frames"
    upscaled_dir = workdir / "upscaled"

    for i in range(n_chunks):
        start = i * chunk_seconds
        # Last chunk picks up whatever's left rather than over-reading.
        chunk_dur = max(0.0, min(float(chunk_seconds), duration - start))
        if chunk_dur <= 0:
            break

        for d in (frames_dir, upscaled_dir):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir()

        _extract_frames_segment(
            ffmpeg,
            input_path,
            frames_dir,
            start=start,
            duration=chunk_dur,
        )
        _upscale_frames_realesrgan(
            binary,
            frames_dir,
            upscaled_dir,
            model=model,
            scale=scale,
            tile_size=tile_size,
            threads=threads,
        )
        chunk_video = chunk_dir / f"chunk_{i:04d}.mp4"
        _encode_chunk_video(ffmpeg, upscaled_dir, chunk_video, fps=fps)
        chunk_videos.append(chunk_video)

    # Free the last chunk's frame buffers before concatenation.
    shutil.rmtree(frames_dir, ignore_errors=True)
    shutil.rmtree(upscaled_dir, ignore_errors=True)

    if not chunk_videos:
        raise PostProcessError(
            "upscale: chunked pipeline produced no chunks (zero-duration video?)",
            step="upscale",
            partial_output_path=str(input_path),
        )

    if has_audio:
        concat_video = workdir / f"concat{input_path.suffix}"
        _concat_videos(ffmpeg, chunk_videos, concat_video)
        _mux_audio(
            ffmpeg, video=concat_video, audio_source=input_path, output=output_tmp
        )
    else:
        _concat_videos(ffmpeg, chunk_videos, output_tmp)


# ── primitives ────────────────────────────────────────────────────────────


def _extract_frames(ffmpeg: str, source: Path, target_dir: Path) -> None:
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        # PNG to avoid recompression artifacts going into the upscaler.
        str(target_dir / "frame_%08d.png"),
    ]
    logger.debug("upscale: extracting frames -> %s", target_dir)
    subprocess.run(cmd, capture_output=True, check=True)  # nosec


def _extract_frames_segment(
    ffmpeg: str,
    source: Path,
    target_dir: Path,
    *,
    start: float,
    duration: float,
) -> None:
    """Extract just the [start, start+duration) window as a PNG sequence.

    ``-ss`` placed BEFORE ``-i`` uses fast-seek (keyframe-aligned with
    decode-from-keyframe), which is the right call here: we trade
    sub-second start precision for big speedups on long videos. The
    boundary error is rounded away by the chunked accounting since
    ffmpeg's `-t` is honored against the actual decode start.
    """
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration:.3f}",
        "-an",  # video-only — audio is muxed back at the end
        str(target_dir / "frame_%08d.png"),
    ]
    logger.debug(
        "upscale: extracting [%.2fs +%.2fs] -> %s",
        start,
        duration,
        target_dir,
    )
    subprocess.run(cmd, capture_output=True, check=True)  # nosec


def _upscale_frames_realesrgan(
    binary: str,
    source_dir: Path,
    target_dir: Path,
    *,
    model: str,
    scale: int,
    tile_size: int,
    threads: Optional[str] = None,
) -> None:
    cmd: List[str] = [
        binary,
        "-i",
        str(source_dir),
        "-o",
        str(target_dir),
        "-n",
        model,
        "-s",
        str(scale),
        "-f",
        "png",
    ]
    # The binary's default model path is "models/" relative to CWD, which
    # breaks when pyt is invoked from an arbitrary directory. Pass -m
    # pointing to the models/ dir that ships alongside the binary so it
    # always resolves correctly.
    models_dir = Path(binary).parent / "models"
    if models_dir.is_dir():
        cmd += ["-m", str(models_dir)]
    if tile_size:
        cmd += ["-t", str(tile_size)]
    if threads:
        cmd += ["-j", threads]
    logger.debug("upscale[realesrgan]: %s", " ".join(cmd))
    subprocess.run(cmd, capture_output=True, check=True)  # nosec


def _encode_chunk_video(
    ffmpeg: str,
    frames_dir: Path,
    output: Path,
    *,
    fps: float,
) -> None:
    """Re-encode one chunk's upscaled PNG sequence to a video-only mp4.

    Using identical encode params (``libx264``, ``yuv420p``, CRF 18) for
    every chunk lets us concat them later with ``-c copy`` (no re-encode).
    """
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-framerate",
        f"{fps:.6f}",
        "-i",
        str(frames_dir / "frame_%08d.png"),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "18",
        "-an",
        str(output),
    ]
    logger.debug("upscale: encoding chunk -> %s", output)
    subprocess.run(cmd, capture_output=True, check=True)  # nosec


def _concat_videos(ffmpeg: str, chunks: List[Path], output: Path) -> None:
    """Use ffmpeg's concat demuxer to stitch chunks without re-encoding."""
    list_file = output.parent / "concat_list.txt"
    # ffmpeg's concat list requires forward slashes and quoted POSIX-style
    # paths. Path.as_posix() handles the slash conversion on Windows.
    list_file.write_text(
        "\n".join(f"file '{c.as_posix()}'" for c in chunks) + "\n",
        encoding="utf-8",
    )
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(output),
    ]
    logger.debug("upscale: concat %d chunks -> %s", len(chunks), output)
    subprocess.run(cmd, capture_output=True, check=True)  # nosec


def _mux_audio(
    ffmpeg: str,
    *,
    video: Path,
    audio_source: Path,
    output: Path,
) -> None:
    """Combine an upscaled video-only file with the original audio track."""
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-i",
        str(audio_source),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        # If durations don't match exactly (chunk-boundary rounding), trim
        # to the shorter of the two so the mux doesn't fail on EOF mismatch.
        "-shortest",
        str(output),
    ]
    logger.debug("upscale: muxing audio -> %s", output)
    subprocess.run(cmd, capture_output=True, check=True)  # nosec


def _recombine(
    ffmpeg: str,
    *,
    frames_dir: Path,
    audio_source: Optional[Path],
    output: Path,
    fps: float,
) -> None:
    """Single-shot path: encode all upscaled frames straight to the final
    container and (optionally) mux the source's audio in one pass."""
    cmd: List[str] = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-framerate",
        f"{fps:.6f}",
        "-i",
        str(frames_dir / "frame_%08d.png"),
    ]
    if audio_source is not None:
        cmd += [
            "-i",
            str(audio_source),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:a",
            "copy",
        ]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", str(output)]
    logger.debug("upscale: recombining -> %s", output)
    subprocess.run(cmd, capture_output=True, check=True)  # nosec


# ── ffprobe helpers ───────────────────────────────────────────────────────


def _probe_fps(path: Path) -> Optional[float]:
    """Return the video stream's FPS via ffprobe, or None if unavailable.
    ffprobe ships with ffmpeg, so if ffmpeg is on PATH this usually is too.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(  # nosec
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        data = json.loads(result.stdout.decode("utf-8") or "{}")
        rate = (data.get("streams") or [{}])[0].get("avg_frame_rate", "")
        if "/" in rate:
            num, den = rate.split("/", 1)
            num_f, den_f = float(num), float(den)
            if den_f > 0:
                return num_f / den_f
        elif rate:
            return float(rate)
    except (subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        logger.debug("upscale: ffprobe fps lookup failed (%s)", exc)
    return None


def _probe_duration(path: Path) -> Optional[float]:
    """Return the video's duration in seconds via ffprobe, or None."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(  # nosec
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        text = result.stdout.decode("utf-8", errors="replace").strip()
        return float(text) if text else None
    except (subprocess.SubprocessError, ValueError) as exc:
        logger.debug("upscale: ffprobe duration lookup failed (%s)", exc)
        return None


def _probe_has_audio(path: Path) -> bool:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return True  # assume yes — recombine fails loudly if it isn't
    try:
        result = subprocess.run(  # nosec
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        return bool(result.stdout.strip())
    except subprocess.SubprocessError as exc:
        logger.debug("upscale: ffprobe audio detection failed (%s)", exc)
        return True
