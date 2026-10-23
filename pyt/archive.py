"""Download archive — track already-downloaded video IDs."""

from __future__ import annotations

import os
import threading


class DownloadArchive:
    """Persist a set of downloaded video IDs to avoid re-downloading.

    File format is compatible with yt-dlp: one ``youtube <video_id>`` per line.

    Example
    -------
    >>> archive = DownloadArchive("archive.txt")
    >>> archive.is_downloaded("dQw4w9WgXcQ")
    False
    >>> archive.mark_downloaded("dQw4w9WgXcQ")
    >>> archive.is_downloaded("dQw4w9WgXcQ")
    True
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("youtube "):
                    self._ids.add(line[len("youtube ") :].strip())

    def is_downloaded(self, video_id: str) -> bool:
        """Return True if *video_id* is in the archive."""
        return video_id in self._ids

    def mark_downloaded(self, video_id: str) -> None:
        """Append *video_id* to the archive file (thread-safe)."""
        with self._lock:
            if video_id in self._ids:
                return
            self._ids.add(video_id)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(f"youtube {video_id}\n")
