"""Modern, ergonomic Python API for pyt.

This package is the v1 of the redesigned developer interface described in
the system-design proposal. It is **additive** — it sits on top of the
existing :class:`pyt.YouTube`, :class:`pyt.Stream`, and post-processor
internals without changing them. The legacy classes remain importable from
``pyt`` for backwards compatibility.

The design goals are:

* a single, obvious entry point (:class:`Client`)
* explicit network boundaries (``client.video(url)`` is the I/O moment;
  attribute access is pure)
* chainable, immutable :class:`StreamSet` with ``.best`` / ``.one`` that
  raise typed errors instead of returning ``Optional[Stream]``
* a declarative post-processing :class:`Pipeline` you can read top-to-bottom

Async support is intentionally not in this v1. Adding it requires moving
``pyt.request`` to ``httpx`` and threading awaitables through the SABR
session — that lands as a separate milestone.
"""

# Bootstrap ~/.pyt/bin onto PATH before anything else does shutil.which().
from pyt.api import _paths as _paths  # noqa: F401

# Public log-control API — silent until enable_logging() is called.
from pyt.api._logging import (  # noqa: F401
    enable_logging,
    disable_logging,
    set_log_level,
    get_log_level,
    is_enabled as logging_enabled,
    diagnostic_report,
    TRACE,
)

from pyt.api.errors import (
    PytError,
    VideoUnavailable,
    AgeRestricted,
    LiveStreamNotSupported,
    AttestationRequired,
    NoMatchingStream,
    DownloadError,
    PostProcessError,
    ConfigError,
)
from pyt.api.models import VideoMeta, Channel, Thumbnail
from pyt.api.client import Client
from pyt.api.video import Video
from pyt.api.streams import StreamSet, StreamRef
from pyt.api.download import Download, ProgressEvent
from pyt.api.combined import CombinedDownload
from pyt.api.live_download import LiveDownload
from pyt.api.playlists import Playlist, ChannelFeed, SearchResults
from pyt.api import pipeline

__all__ = [
    "Client",
    "Video",
    "StreamSet",
    "StreamRef",
    "Download",
    "CombinedDownload",
    "LiveDownload",
    "ProgressEvent",
    "Playlist",
    "ChannelFeed",
    "SearchResults",
    "pipeline",
    "VideoMeta",
    "Channel",
    "Thumbnail",
    "PytError",
    "VideoUnavailable",
    "AgeRestricted",
    "LiveStreamNotSupported",
    "AttestationRequired",
    "NoMatchingStream",
    "DownloadError",
    "PostProcessError",
    "ConfigError",
    # Logging
    "enable_logging",
    "disable_logging",
    "set_log_level",
    "get_log_level",
    "logging_enabled",
    "diagnostic_report",
    "TRACE",
]
