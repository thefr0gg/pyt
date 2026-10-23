"""Thumbnail embedding post-processor."""

from __future__ import annotations

import os
import logging
import tempfile
import urllib.request
from typing import TYPE_CHECKING

from mutagen.id3 import ID3, APIC, ID3NoHeaderError

from pyt.postprocessors.ffmpeg import FFmpegPostProcessor

if TYPE_CHECKING:
    from pyt.streams import Stream
    from pyt.__main__ import YouTube

logger = logging.getLogger(__name__)

_MP4_LIKE = {".mp4", ".m4a", ".mov"}
_OGG_LIKE = {".ogg", ".opus", ".flac"}


class EmbedThumbnailPostProcessor(FFmpegPostProcessor):
    """Download and embed the video thumbnail as cover art.

    Equivalent to yt-dlp's ``--embed-thumbnail``.
    MP4/M4A/MOV use ffmpeg's attached-picture method (no re-encode).
    OGG/Opus/FLAC embed via ffmpeg metadata stream.
    MP3 uses ``mutagen`` for the APIC frame.
    """

    def run(self, file_path: str, stream: "Stream", youtube: "YouTube") -> str:
        thumb_url = youtube.thumbnail_url
        if not thumb_url:
            logger.warning("No thumbnail URL — skipping thumbnail embedding")
            return file_path

        ext = os.path.splitext(file_path)[1].lower()
        dir_ = os.path.dirname(os.path.abspath(file_path))
        fd, thumb_path = tempfile.mkstemp(suffix=".jpg", dir=dir_)
        os.close(fd)
        try:
            urllib.request.urlretrieve(thumb_url, thumb_path)  # nosec

            if ext == ".mp3" and self._try_mutagen_mp3(file_path, thumb_path):
                return file_path

            if ext in _MP4_LIKE:
                return self._embed_mp4(file_path, thumb_path, ext)

            if ext in _OGG_LIKE:
                return self._embed_ogg(file_path, thumb_path, ext)

            logger.warning("Thumbnail embedding not supported for %s", ext)
            return file_path
        finally:
            if os.path.exists(thumb_path):
                os.unlink(thumb_path)

    # ── format-specific embedding ────────────────────────────────────────────

    def _embed_mp4(self, file_path: str, thumb: str, ext: str) -> str:
        tmp = self._temp_path(file_path, ext)
        self._run_ffmpeg(
            [
                "-i",
                file_path,
                "-i",
                thumb,
                "-map",
                "0",
                "-map",
                "1",
                "-disposition:v:1",
                "attached_pic",
                "-codec",
                "copy",
                tmp,
            ]
        )
        return self._replace_file(tmp, file_path)

    def _embed_ogg(self, file_path: str, thumb: str, ext: str) -> str:
        tmp = self._temp_path(file_path, ext)
        self._run_ffmpeg(
            [
                "-i",
                file_path,
                "-i",
                thumb,
                "-map",
                "0",
                "-map",
                "1",
                "-codec",
                "copy",
                tmp,
            ]
        )
        return self._replace_file(tmp, file_path)

    def _try_mutagen_mp3(self, file_path: str, thumb_path: str) -> bool:
        with open(thumb_path, "rb") as f:
            data = f.read()

        try:
            tags = ID3(file_path)
        except ID3NoHeaderError:
            tags = ID3()

        tags["APIC"] = APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,
            desc="Cover",
            data=data,
        )
        tags.save(file_path)
        return True
