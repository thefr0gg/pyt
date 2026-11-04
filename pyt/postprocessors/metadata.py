"""Metadata embedding post-processor."""

from __future__ import annotations

import os
import logging
from typing import TYPE_CHECKING

from mutagen.id3 import ID3, TIT2, TPE1, TDRC, COMM, ID3NoHeaderError

from pyt.postprocessors.ffmpeg import FFmpegPostProcessor

if TYPE_CHECKING:
    from pyt.api.streams import StreamRef
    from pyt.api.video import Video

logger = logging.getLogger(__name__)


class FFmpegMetadataEmbedder(FFmpegPostProcessor):
    """Embed video metadata (title, artist, date, description) into the file.

    Equivalent to yt-dlp's ``--embed-metadata``.
    Uses ffmpeg ``-metadata`` flags so no re-encoding is required.
    For MP3 files, uses ``mutagen`` for richer ID3 tag support.
    """

    def run(self, file_path: str, stream: "StreamRef", video: "Video") -> str:
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".mp3" and self._try_mutagen(file_path, video):
            return file_path
        return self._embed_ffmpeg(file_path, video)

    # ── ffmpeg path ──────────────────────────────────────────────────────────

    def _embed_ffmpeg(self, file_path: str, video: "Video") -> str:
        ext = os.path.splitext(file_path)[1]
        tmp = self._temp_path(file_path, ext)
        meta = self._collect_meta(video)
        args = ["-i", file_path]
        for k, v in meta.items():
            args += ["-metadata", f"{k}={v}"]
        args += ["-codec", "copy", tmp]
        self._run_ffmpeg(args)
        return self._replace_file(tmp, file_path)

    # ── mutagen path (MP3) ───────────────────────────────────────────────────

    def _try_mutagen(self, file_path: str, video: "Video") -> bool:
        try:
            tags = ID3(file_path)
        except ID3NoHeaderError:
            tags = ID3()

        meta = self._collect_meta(video)
        if "title" in meta:
            tags["TIT2"] = TIT2(encoding=3, text=meta["title"])
        if "artist" in meta:
            tags["TPE1"] = TPE1(encoding=3, text=meta["artist"])
        if "date" in meta:
            tags["TDRC"] = TDRC(encoding=3, text=meta["date"])
        if "comment" in meta:
            tags["COMM"] = COMM(encoding=3, lang="eng", desc="", text=meta["comment"])
        tags.save(file_path)
        return True

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _collect_meta(video: "Video") -> dict:
        meta: dict = {}

        def _try(key, fn):
            try:
                val = fn()
                if val is not None:
                    meta[key] = str(val)
            except Exception:
                pass

        _try("title", lambda: video.title)
        _try("artist", lambda: video.author.name)
        _try(
            "date",
            lambda: (
                video.published_at.strftime("%Y%m%d") if video.published_at else None
            ),
        )
        _try(
            "comment",
            lambda: (video.description or "")[:500].replace("\n", " ") or None,
        )
        return meta
