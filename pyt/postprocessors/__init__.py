"""pyt post-processors — yt-dlp-compatible post-download processing pipeline."""

from pyt.postprocessors.base import BasePostProcessor, PostProcessorError
from pyt.postprocessors.ffmpeg import FFmpegPostProcessor
from pyt.postprocessors.audio import AudioExtractor, SUPPORTED_FORMATS
from pyt.postprocessors.metadata import FFmpegMetadataEmbedder
from pyt.postprocessors.thumbnail import EmbedThumbnailPostProcessor
from pyt.postprocessors.subtitle import EmbedSubtitlePostProcessor
from pyt.postprocessors.sponsorblock import SponsorBlockPP, ALL_CATEGORIES

__all__ = [
    "BasePostProcessor",
    "PostProcessorError",
    "FFmpegPostProcessor",
    "AudioExtractor",
    "SUPPORTED_FORMATS",
    "FFmpegMetadataEmbedder",
    "EmbedThumbnailPostProcessor",
    "EmbedSubtitlePostProcessor",
    "SponsorBlockPP",
    "ALL_CATEGORIES",
]
