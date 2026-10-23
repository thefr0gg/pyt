"""FFmpeg-based post-processor base class."""

from __future__ import annotations

import os
import shutil
import logging
import subprocess
import tempfile
from typing import List, TYPE_CHECKING

from pyt.postprocessors.base import BasePostProcessor, PostProcessorError

if TYPE_CHECKING:
    from pyt.streams import Stream
    from pyt.__main__ import YouTube

logger = logging.getLogger(__name__)


class FFmpegPostProcessor(BasePostProcessor):
    """Base class for post-processors that require ffmpeg."""

    def __init__(self) -> None:
        self._ffmpeg = shutil.which("ffmpeg")
        if not self._ffmpeg:
            raise PostProcessorError(
                "ffmpeg not found. Install ffmpeg and ensure it is on PATH."
            )

    def _run_ffmpeg(self, args: List[str]) -> None:
        cmd = [self._ffmpeg, "-y", "-loglevel", "error"] + args
        logger.debug("Running: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, check=False)  # nosec
        if result.returncode != 0:
            raise PostProcessorError(
                f"ffmpeg exited with code {result.returncode}:\n"
                + result.stderr.decode("utf-8", errors="replace")
            )

    def _replace_file(self, src: str, dst: str) -> str:
        """Move *src* over *dst*, returning *dst*."""
        if os.path.exists(dst):
            os.unlink(dst)
        os.replace(src, dst)
        return dst

    @staticmethod
    def _temp_path(original: str, suffix: str = "") -> str:
        """Return a temp file path in the same directory as *original*."""
        dir_ = os.path.dirname(os.path.abspath(original))
        fd, path = tempfile.mkstemp(suffix=suffix, dir=dir_)
        os.close(fd)
        return path
