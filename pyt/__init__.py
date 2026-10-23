"""pyt — YouTube downloader for Python 3.

v3: the legacy pytube API has been removed from the top-level namespace.
Legacy symbols (YouTube, Stream, Caption, Playlist, etc.) are still
importable from ``pyt.legacy`` through this release cycle:

    from pyt.legacy import YouTube, Playlist, Channel, Search

They emit a ``DeprecationWarning`` and will be removed in v4.
"""

__title__ = "pyt"
__author__ = "ramsy0dev"
__license__ = "MIT"

# Module-level JS cache — used internally by pyt._legacy_archive.YouTube.
__js__ = None
__js_url__ = None

from pyt.version import __version__

# Modern API — the only public surface in v3.
from pyt.api import (
    Client,
    Video,
    StreamSet,
    StreamRef,
    Download,
    CombinedDownload,
    LiveDownload,
    ChannelFeed,
    SearchResults,
    pipeline,
    VideoMeta,
    Thumbnail,
    PytError,
    VideoUnavailable,
    AgeRestricted,
    LiveStreamNotSupported,
    AttestationRequired,
    NoMatchingStream,
    DownloadError,
    PostProcessError,
    ConfigError,
    enable_logging,
    disable_logging,
    set_log_level,
    get_log_level,
    logging_enabled,
    diagnostic_report,
    TRACE,
)

# v2 post-processors and archive helpers remain importable directly.
from pyt.archive import DownloadArchive
from pyt.template import OutputTemplate
from pyt.postprocessors import (
    AudioExtractor,
    FFmpegMetadataEmbedder,
    EmbedThumbnailPostProcessor,
    EmbedSubtitlePostProcessor,
    SponsorBlockPP,
    PostProcessorError,
)
