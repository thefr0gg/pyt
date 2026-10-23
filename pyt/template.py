"""Output filename template engine."""

from __future__ import annotations

import os
import re
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pyt.streams import Stream
    from pyt.__main__ import YouTube

# Characters illegal in filenames on Windows/macOS/Linux
_UNSAFE = re.compile(r'[\\/:*?"<>|]')

# Matches {field} or {field:strftime_format}
_FIELD_RE = re.compile(r"\{(\w+)(?::([^}]+))?\}")

DEFAULT_TEMPLATE = "{title}.{ext}"


class OutputTemplate:
    """Output filename template using a simple {field} syntax.

    Supported fields
    ----------------
    title, id, author, channel_id, upload_date, ext, resolution, fps, abr,
    filesize, filesize_approx, playlist_index, playlist_title.

    Date formatting
    ---------------
    ``{upload_date}``           → ``20240101``
    ``{upload_date:%Y-%m-%d}``  → ``2024-01-01``
    ``{upload_date:%B %Y}``     → ``January 2024``

    Directory separators
    --------------------
    A ``/`` in the template creates sub-directories automatically:
    ``{author}/{title}.{ext}`` saves into a per-author folder.
    Each path component is sanitised independently.

    Example
    -------
    >>> t = OutputTemplate("{author} - {title}.{ext}")
    >>> t.render(youtube, stream)
    'Rick Astley - Never Gonna Give You Up.mp4'
    """

    def __init__(self, template: str = DEFAULT_TEMPLATE) -> None:
        self._template = template

    def render(
        self,
        youtube: "YouTube",
        stream: "Stream",
        playlist_index: Optional[int] = None,
        playlist_title: Optional[str] = None,
    ) -> str:
        fields = self._collect(youtube, stream, playlist_index, playlist_title)

        def _replace(m: re.Match) -> str:
            key = m.group(1)
            fmt = m.group(2)
            val = fields.get(key)
            if val is None:
                return f"{{{key}}}"  # leave unknown fields as-is
            if fmt and hasattr(val, "strftime"):
                return val.strftime(fmt)
            if hasattr(val, "strftime"):
                return val.strftime("%Y%m%d")  # default date format
            return str(val)

        result = _FIELD_RE.sub(_replace, self._template)
        # Sanitise each path component separately
        parts = result.replace("\\", "/").split("/")
        clean = [_UNSAFE.sub("_", p).strip(" .") or "_" for p in parts]
        return os.path.join(*clean) if len(clean) > 1 else clean[0]

    @staticmethod
    def _collect(
        youtube: "YouTube",
        stream: "Stream",
        playlist_index: Optional[int],
        playlist_title: Optional[str],
    ) -> dict:
        fields: dict = {}

        def _try(key, fn):
            try:
                v = fn()
                if v is not None and v != "":
                    fields[key] = v
            except Exception:
                pass

        _try("title", lambda: youtube.title)
        _try("id", lambda: youtube.video_id)
        _try("author", lambda: youtube.author)
        _try("channel_id", lambda: youtube.channel_id)
        _try(
            "upload_date", lambda: youtube.publish_date
        )  # datetime object; formatted in _replace
        _try("ext", lambda: stream.subtype)
        _try("resolution", lambda: stream.resolution)
        _try("fps", lambda: getattr(stream, "fps", None))
        _try("abr", lambda: stream.abr)
        _try("filesize", lambda: stream.filesize_approx)
        _try("filesize_approx", lambda: stream.filesize_approx)

        if playlist_index is not None:
            fields["playlist_index"] = f"{playlist_index:02d}"
        if playlist_title is not None:
            fields["playlist_title"] = playlist_title

        return fields
