"""Audio extraction post-processor."""

from __future__ import annotations

import os
import logging
from typing import Optional, TYPE_CHECKING

from pyt.postprocessors.ffmpeg import FFmpegPostProcessor, PostProcessorError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyt.streams import Stream
    from pyt.__main__ import YouTube

# Maps user-facing format name → (ffmpeg codec, file extension)
_CODEC_MAP = {
    "mp3": ("libmp3lame", ".mp3"),
    "m4a": ("aac", ".m4a"),
    "aac": ("aac", ".aac"),
    "flac": ("flac", ".flac"),
    "opus": ("libopus", ".opus"),
    "vorbis": ("libvorbis", ".ogg"),
    "wav": ("pcm_s16le", ".wav"),
    "alac": ("alac", ".m4a"),
}

SUPPORTED_FORMATS = list(_CODEC_MAP)


class AudioExtractor(FFmpegPostProcessor):
    """Extract and convert audio from a downloaded stream.

    Equivalent to yt-dlp's ``-x / --extract-audio`` + ``--audio-format``.
    Removes the video track and converts to the target audio format using ffmpeg.
    The original file is deleted after a successful conversion.
    """

    def __init__(self, format: str = "mp3", quality: Optional[str] = None) -> None:
        """
        :param format: Target audio format. One of: mp3, m4a, aac, flac, opus,
            vorbis, wav, alac. Default: mp3.
        :param quality: Optional ffmpeg quality value passed as ``-q:a``.
            ``"0"`` means best VBR quality.
        """
        super().__init__()
        fmt = format.lower()
        if fmt not in _CODEC_MAP:
            raise PostProcessorError(
                f"Unsupported audio format '{format}'. "
                f"Choose from: {', '.join(_CODEC_MAP)}"
            )
        self._format = fmt
        self._codec, self._ext = _CODEC_MAP[fmt]
        self._quality = quality

    def run(self, file_path: str, stream: "Stream", youtube: "YouTube") -> str:
        base, _original_ext = os.path.splitext(file_path)
        out_path = base + self._ext

        tmp = self._temp_path(file_path, self._ext)
        args = ["-i", file_path, "-vn", "-acodec", self._codec]
        if self._quality:
            args += ["-q:a", self._quality]
        args.append(tmp)
        self._run_ffmpeg(args)

        # Move the converted file into place atomically BEFORE deleting the
        # source. os.replace is atomic on POSIX and best-effort atomic on
        # Windows; either way it's safer than unlink-then-replace.
        os.replace(tmp, out_path)
        if os.path.abspath(file_path) != os.path.abspath(out_path):
            try:
                os.unlink(file_path)
            except OSError as e:
                logger.warning("could not remove original %s: %s", file_path, e)
        return out_path
