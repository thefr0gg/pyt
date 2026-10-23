"""Typed, frozen dataclasses returned by the modern API.

These replace the ``vid_info.get('videoDetails', {}).get(...)`` pattern that
leaks through the legacy :class:`pyt.YouTube` properties. Attribute access
is pure: no network I/O, no surprise exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional


@dataclass(frozen=True)
class Thumbnail:
    """One thumbnail variant from YouTube's thumbnail array."""

    url: str
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass(frozen=True)
class Channel:
    """Reference to the channel that uploaded a video."""

    id: Optional[str]
    name: str
    url: Optional[str] = None


@dataclass(frozen=True)
class VideoMeta:
    """Frozen snapshot of a video's metadata, taken at the moment the
    enclosing :class:`pyt.api.Video` was constructed.

    Subsequent calls to ``client.video(url)`` will produce a fresh snapshot
    rather than mutating an existing one — by design.
    """

    video_id: str
    url: str
    title: str
    author: Channel
    length: timedelta
    description: Optional[str] = None
    published_at: Optional[datetime] = None
    views: Optional[int] = None
    rating: Optional[float] = None
    keywords: List[str] = field(default_factory=list)
    thumbnails: List[Thumbnail] = field(default_factory=list)
    is_live: bool = False
    is_live_content: bool = False
    hls_manifest_url: Optional[str] = None
    scheduled_start: Optional[datetime] = None
    is_age_restricted: bool = False
