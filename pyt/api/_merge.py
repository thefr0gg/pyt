"""ffmpeg-merge helpers shared by the modern download path and the CLI.

Kept private (leading underscore) for now — the API may move once the new
``CombinedDownload`` shape settles. The CLI imports the same helper so
container-selection logic doesn't drift between code paths.
"""

from __future__ import annotations

import logging
import shutil
import subprocess  # nosec - we control all argv entries
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pyt.api.streams import StreamRef
    from pyt.streams import Stream as _LegacyStream


logger = logging.getLogger(__name__)


def probe_duration(path: Path) -> Optional[float]:
    """Return the file's duration in seconds via ffprobe, or None.

    Used both before merge (sanity-check inputs) and after (verify the
    output didn't get truncated by libdav1d / libaom giving up on a
    corrupt input bitstream).
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
    except (subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        # Broad catch on purpose: some test environments globally mock
        # subprocess.run and return a Mock whose .stdout doesn't decode to
        # a parseable string. We treat probe_duration as best-effort —
        # falling back to None is always safe (the caller skips validation).
        return None


def pick_merge_container(video_codec: Optional[str], audio_codec: Optional[str]) -> str:
    """Pick the right container for muxing two codecs without re-encoding.

    YouTube serves video as one of: avc1 (H.264) in mp4, vp9 in webm, av01 in mp4.
    Audio: mp4a/aac in m4a, opus in webm.
    ``-codec copy`` requires the container to accept both codecs. mp4 takes
    avc1+aac and av01+aac; webm takes vp9+opus. mkv accepts everything and is
    the safe fallback for cross-family combinations.
    """
    v = (video_codec or "").lower()
    a = (audio_codec or "").lower()
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


def pick_merge_container_for_streams(video, audio) -> str:
    """Convenience wrapper accepting either modern :class:`StreamRef` or
    legacy :class:`pyt.Stream` instances.
    """
    return pick_merge_container(
        getattr(video, "video_codec", None),
        getattr(audio, "audio_codec", None),
    )


class FFmpegNotFound(RuntimeError):
    pass


class FFmpegMergeError(RuntimeError):
    def __init__(self, returncode: int, stderr: str):
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"ffmpeg merge failed (exit {returncode}): {stderr.strip()[:400]}"
        )


def run_ffmpeg_merge(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    *,
    container: Optional[str] = None,
) -> Path:
    """Mux *video_path* + *audio_path* into *output_path* with ``-codec copy``.

    After the mux completes, the output's duration is probed and compared
    against the inputs. ffmpeg's libdav1d / libaom bitstream filters will
    silently stop muxing partway through a corrupt AV1 stream and exit 0
    — meaning a 4-second output from a 2-minute input passes as "success"
    by exit code alone. The post-merge duration check turns that into a
    visible :class:`FFmpegMergeError` so the caller can re-download
    rather than walk away with a broken file.

    ``-fflags +discardcorrupt`` is added so the muxer drops corrupt
    packets and keeps going instead of stopping at the first bad one.

    Raises :class:`FFmpegNotFound` when ffmpeg isn't on PATH and
    :class:`FFmpegMergeError` when ffmpeg fails or the output is short.
    """
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise FFmpegNotFound("ffmpeg not found on PATH; cannot merge")

    ext = (container or output_path.suffix.lstrip(".")).lower()
    cmd = [
        ffmpeg_bin,
        "-y",
        "-loglevel",
        "warning",
        "-nostats",
        # Drop corrupt packets and keep muxing rather than halting on the
        # first bitstream issue. With -c copy the muxer doesn't decode, but
        # mp4 needs to validate AV1 OBU structure for track config — when
        # that validation trips, the default behavior is to stop early.
        "-fflags",
        "+discardcorrupt",
        "-err_detect",
        "ignore_err",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
    ]
    if ext == "mp4":
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(output_path))

    result = subprocess.run(cmd, capture_output=True, check=False)  # nosec
    if result.returncode != 0:
        raise FFmpegMergeError(
            result.returncode,
            result.stderr.decode("utf-8", errors="replace"),
        )

    _validate_merge_output(video_path, audio_path, output_path)
    return output_path


def _validate_merge_output(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    *,
    tolerance: float = 0.1,
) -> None:
    """Raise :class:`FFmpegMergeError` if the output is much shorter than
    the inputs. Tolerance is fractional (0.1 = output must be ≥ 90% of the
    longer input).

    Uses ffprobe for everything; falls back to a no-op when ffprobe isn't
    available — we don't want to fail merges just because ffprobe isn't
    installed (most ffmpeg builds bundle it but a few don't).
    """
    out_dur = probe_duration(output_path)
    if out_dur is None:
        return
    in_durs = [probe_duration(p) for p in (video_path, audio_path)]
    in_durs = [d for d in in_durs if d is not None]
    if not in_durs:
        return
    expected = max(in_durs)
    if expected <= 0:
        return
    if out_dur < expected * (1.0 - tolerance):
        try:
            output_path.unlink()
        except OSError:
            pass
        raise FFmpegMergeError(
            0,
            (
                f"output duration is {out_dur:.1f}s but the input video/audio "
                f"are ~{expected:.1f}s — ffmpeg muxed only "
                f"{out_dur / expected * 100:.0f}% of the expected length, "
                f"likely because the source bitstream is corrupt or truncated. "
                f"Re-download the source and try again."
            ),
        )
