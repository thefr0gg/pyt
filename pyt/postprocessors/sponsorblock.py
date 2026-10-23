"""SponsorBlock integration post-processor."""

from __future__ import annotations

import json
import os
import logging
import tempfile
import urllib.parse
import urllib.request
from typing import List, Optional, TYPE_CHECKING

from pyt.postprocessors.ffmpeg import FFmpegPostProcessor, PostProcessorError

if TYPE_CHECKING:
    from pyt.streams import Stream
    from pyt.__main__ import YouTube

logger = logging.getLogger(__name__)

_API = "https://sponsor.ajay.app/api/skipSegments"

ALL_CATEGORIES = [
    "sponsor",
    "intro",
    "outro",
    "selfpromo",
    "preview",
    "filler",
    "interaction",
    "music_offtopic",
    "hook",
    "poi_highlight",
]

_LABELS = {
    "sponsor": "Sponsor",
    "intro": "Intro",
    "outro": "Outro",
    "selfpromo": "Self-Promotion",
    "preview": "Preview",
    "filler": "Filler",
    "interaction": "Interaction",
    "music_offtopic": "Non-Music",
    "hook": "Hook",
    "poi_highlight": "Highlight",
}


def _fetch_segments(video_id: str, categories: List[str]) -> List[dict]:
    """Query the SponsorBlock API for segments of *video_id*."""
    params = urllib.parse.urlencode(
        {
            "videoID": video_id,
            "categories": json.dumps(categories),
        }
    )
    url = f"{_API}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # nosec
            if resp.status == 404:
                return []
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("SponsorBlock API error: %s", exc)
        return []


class SponsorBlockPP(FFmpegPostProcessor):
    """Add SponsorBlock chapter markers or remove segments.

    Equivalent to yt-dlp's ``--sponsorblock-mark`` and ``--sponsorblock-remove``.

    **mark** mode (default): embeds chapter labels for each segment without
    re-encoding the media. Uses an ffmetadata sidecar file.

    **remove** mode: cuts the segments out of the video using ffmpeg's ``select``
    filter, which requires re-encoding the audio and video streams.

    Available categories: sponsor, intro, outro, selfpromo, preview, filler,
    interaction, music_offtopic, hook, poi_highlight — or ``"all"``.
    """

    def __init__(
        self,
        mode: str = "mark",
        categories: Optional[List[str]] = None,
    ) -> None:
        super().__init__()
        if mode not in ("mark", "remove"):
            raise PostProcessorError("SponsorBlockPP mode must be 'mark' or 'remove'")
        self._mode = mode
        self._categories = categories if categories is not None else ["sponsor"]

    def run(self, file_path: str, stream: "Stream", youtube: "YouTube") -> str:
        segments = _fetch_segments(youtube.video_id, self._categories)
        if not segments:
            logger.info("No SponsorBlock segments found for %s", youtube.video_id)
            return file_path

        logger.info(
            "Found %d SponsorBlock segment(s) for %s", len(segments), youtube.video_id
        )
        if self._mode == "mark":
            return self._mark(file_path, segments)
        return self._remove(file_path, segments)

    # ── mark mode ────────────────────────────────────────────────────────────

    def _mark(self, file_path: str, segments: List[dict]) -> str:
        chapters = self._segments_to_chapters(segments)
        dir_ = os.path.dirname(os.path.abspath(file_path))
        fd, meta_path = tempfile.mkstemp(suffix=".ini", dir=dir_)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(";FFMETADATA1\n")
                for ch in chapters:
                    f.write("\n[CHAPTER]\n")
                    f.write("TIMEBASE=1/1000\n")
                    f.write(f"START={ch['start_ms']}\n")
                    f.write(f"END={ch['end_ms']}\n")
                    f.write(f"title={ch['title']}\n")

            ext = os.path.splitext(file_path)[1]
            tmp = self._temp_path(file_path, ext)
            self._run_ffmpeg(
                [
                    "-i",
                    file_path,
                    "-i",
                    meta_path,
                    "-map_metadata",
                    "1",
                    "-map_chapters",
                    "1",
                    "-codec",
                    "copy",
                    tmp,
                ]
            )
            return self._replace_file(tmp, file_path)
        finally:
            if os.path.exists(meta_path):
                os.unlink(meta_path)

    # ── remove mode ──────────────────────────────────────────────────────────

    def _remove(self, file_path: str, segments: List[dict]) -> str:
        conditions = []
        for seg in segments:
            s, e = seg["segment"]
            conditions.append(f"not(between(t,{s},{e}))")
        expr = "*".join(f"({c})" for c in conditions)

        ext = os.path.splitext(file_path)[1]
        tmp = self._temp_path(file_path, ext)
        self._run_ffmpeg(
            [
                "-i",
                file_path,
                "-vf",
                f"select='{expr}',setpts=N/FRAME_RATE/TB",
                "-af",
                f"aselect='{expr}',asetpts=N/SR/TB",
                tmp,
            ]
        )
        return self._replace_file(tmp, file_path)

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _segments_to_chapters(segments: List[dict]) -> List[dict]:
        chapters = []
        for seg in segments:
            s, e = seg["segment"]
            cat = seg.get("category", "sponsor")
            chapters.append(
                {
                    "start_ms": int(s * 1000),
                    "end_ms": int(e * 1000),
                    "title": f"[SponsorBlock] {_LABELS.get(cat, cat.title())}",
                }
            )
        return sorted(chapters, key=lambda c: c["start_ms"])
