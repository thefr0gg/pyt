"""Subtitle embedding post-processor."""

from __future__ import annotations

import os
import logging
import tempfile
from typing import TYPE_CHECKING

from pyt.postprocessors.ffmpeg import FFmpegPostProcessor

if TYPE_CHECKING:
    from pyt.streams import Stream
    from pyt.__main__ import YouTube

logger = logging.getLogger(__name__)

# Maps container extension → ffmpeg subtitle codec
_SUB_CODEC = {
    ".mp4": "mov_text",
    ".m4a": "mov_text",
    ".mkv": "srt",
    ".webm": "webvtt",
}


class EmbedSubtitlePostProcessor(FFmpegPostProcessor):
    """Embed a subtitle track into the video file.

    Equivalent to yt-dlp's ``--embed-subs``.
    Downloads the requested language caption as SRT and muxes it in without
    re-encoding the audio/video streams.

    Supported containers: MP4 (mov_text), MKV (SRT), WebM (WebVTT).
    """

    def __init__(self, lang_code: str = "en") -> None:
        super().__init__()
        self._lang_code = lang_code

    def run(self, file_path: str, stream: "Stream", youtube: "YouTube") -> str:
        ext = os.path.splitext(file_path)[1].lower()
        sub_codec = _SUB_CODEC.get(ext)
        if not sub_codec:
            logger.warning("Subtitle embedding not supported for %s", ext)
            return file_path

        caption = youtube.captions.get(self._lang_code)
        if caption is None:
            # Try auto-generated variant (prefix "a.")
            caption = youtube.captions.get(f"a.{self._lang_code}")
        if caption is None:
            logger.warning(
                "No caption found for language '%s'; skipping subtitle embedding",
                self._lang_code,
            )
            return file_path

        dir_ = os.path.dirname(os.path.abspath(file_path))
        fd, srt_path = tempfile.mkstemp(suffix=".srt", dir=dir_)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(caption.generate_srt_captions())

            tmp = self._temp_path(file_path, ext)
            self._run_ffmpeg(
                [
                    "-i",
                    file_path,
                    "-i",
                    srt_path,
                    "-c",
                    "copy",
                    "-c:s",
                    sub_codec,
                    "-metadata:s:s:0",
                    f"language={self._lang_code}",
                    tmp,
                ]
            )
            return self._replace_file(tmp, file_path)
        finally:
            if os.path.exists(srt_path):
                os.unlink(srt_path)
