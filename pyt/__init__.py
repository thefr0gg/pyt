"""pyt — YouTube downloader for Python 3.

v3: the pytube-era API (``YouTube``, ``Stream``, ``Playlist``, ``Channel``,
``Search``) is gone. ``pyt.Client`` is the only entry point.
"""

__title__ = "pyt"
__author__ = "ramsy0dev"
__license__ = "MIT"

from pyt.version import __version__

# Public API.
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
