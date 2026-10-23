"""Declarative post-processing pipeline.

Each step is a small, frozen dataclass that wraps the imperative
post-processor in :mod:`pyt.postprocessors`. Use the factory functions
(``sponsorblock(...)``, ``embed_metadata()``, …) instead of constructing
the step classes directly — the factories give you stable kwargs and
hide the underlying class names.

Example::

    from pyt.api import pipeline as pp

    path = (
        stream.download_to("downloads/")
            | pp.sponsorblock(mark=["sponsor", "intro"])
            | pp.embed_metadata()
            | pp.embed_thumbnail()
            | pp.extract_audio("mp3")
    ).run()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

from pyt.postprocessors import (
    AudioExtractor,
    EmbedSubtitlePostProcessor,
    EmbedThumbnailPostProcessor,
    FFmpegMetadataEmbedder,
    SponsorBlockPP,
)

if TYPE_CHECKING:
    from pyt.api.streams import StreamRef
    from pyt.api.video import Video


# ── base ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PipelineStep:
    """A single step. Subclasses override :meth:`apply`. The ``name``
    field is used in error messages and logs.
    """

    name: str

    def apply(self, path: str, *, stream: "StreamRef", video: "Video") -> str:
        raise NotImplementedError


# ── concrete steps ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _SponsorBlock(PipelineStep):
    mode: str = "mark"
    categories: List[str] = field(default_factory=lambda: ["sponsor"])

    def apply(self, path: str, *, stream: "StreamRef", video: "Video") -> str:
        pp = SponsorBlockPP(mode=self.mode, categories=list(self.categories))
        return pp.run(path, stream.legacy, video.legacy)


@dataclass(frozen=True)
class _EmbedMetadata(PipelineStep):
    def apply(self, path: str, *, stream: "StreamRef", video: "Video") -> str:
        return FFmpegMetadataEmbedder().run(path, stream.legacy, video.legacy)


@dataclass(frozen=True)
class _EmbedThumbnail(PipelineStep):
    def apply(self, path: str, *, stream: "StreamRef", video: "Video") -> str:
        return EmbedThumbnailPostProcessor().run(path, stream.legacy, video.legacy)


@dataclass(frozen=True)
class _EmbedSubtitles(PipelineStep):
    lang: str = "en"

    def apply(self, path: str, *, stream: "StreamRef", video: "Video") -> str:
        return EmbedSubtitlePostProcessor(lang_code=self.lang).run(
            path, stream.legacy, video.legacy
        )


@dataclass(frozen=True)
class _ExtractAudio(PipelineStep):
    format: str = "mp3"
    quality: Optional[str] = None

    def apply(self, path: str, *, stream: "StreamRef", video: "Video") -> str:
        return AudioExtractor(format=self.format, quality=self.quality).run(
            path, stream.legacy, video.legacy
        )


# ── public factories ───────────────────────────────────────────────────────


def sponsorblock(
    *,
    mark: Optional[List[str]] = None,
    remove: Optional[List[str]] = None,
) -> PipelineStep:
    """Mark or remove SponsorBlock segments. Pass exactly one of
    ``mark=`` or ``remove=``."""
    if mark and remove:
        raise ValueError("pass either mark= or remove=, not both")
    if not mark and not remove:
        raise ValueError("sponsorblock(): one of mark= or remove= is required")
    if mark:
        return _SponsorBlock(name="sponsorblock", mode="mark", categories=list(mark))
    return _SponsorBlock(
        name="sponsorblock", mode="remove", categories=list(remove or [])
    )


def embed_metadata() -> PipelineStep:
    """Embed title, artist, date, description (ffmpeg + mutagen for MP3)."""
    return _EmbedMetadata(name="embed_metadata")


def embed_thumbnail() -> PipelineStep:
    """Embed cover art into the file."""
    return _EmbedThumbnail(name="embed_thumbnail")


def embed_subtitles(lang: str = "en") -> PipelineStep:
    """Embed a subtitle track for *lang* into the file."""
    return _EmbedSubtitles(name="embed_subtitles", lang=lang)


def extract_audio(
    format: str = "mp3", *, quality: Optional[str] = None
) -> PipelineStep:
    """Transcode the file to an audio-only container."""
    return _ExtractAudio(name="extract_audio", format=format, quality=quality)


def upscale(
    *,
    scale: int = 2,
    algorithm: str = "lanczos",
    # lanczos kwargs
    crf: int = 18,
    preset: str = "medium",
    sharpen: float = 0.4,
    # realesrgan kwargs
    model: str = "realesrgan-x4plus",
    binary: Optional[str] = None,
    tile_size: int = 0,
    keep_intermediate: bool = False,
    chunk_seconds: int = 30,
    threads: Optional[str] = None,
) -> PipelineStep:
    """**Experimental.** Upscale the downloaded video.

    Two algorithms are available:

    ``algorithm="lanczos"`` (default)
        Pure ffmpeg: a Lanczos resize plus a conservative unsharp pass.
        Runs in real-time or faster on any CPU, no extra installs (ffmpeg
        is already required). Best at 2× (360→720, 720→1440); doesn't add
        detail the source doesn't have, but produces a noticeably cleaner
        result than naive bilinear or browser-side player upscaling.
        **This is the right choice for the typical user.**

    ``algorithm="realesrgan"``
        Neural-net super-resolution via the ``realesrgan-ncnn-vulkan``
        binary (install from https://github.com/xinntao/Real-ESRGAN/releases).
        The video is processed in N-second chunks (default 30s) so peak
        disk usage is bounded by chunk size, not video length — a
        5-minute 720p clip peaks at ~6 GB instead of ~45 GB. Each chunk:
        extract frames → upscale → re-encode to a small mp4 → drop the
        PNGs. Final step: concat (no re-encode) + remux original audio.
        Wall-clock is still GPU-bound; tune ``threads=`` if you have
        spare GPU memory.

    Common arguments:

    :param scale: 2, 3, or 4. Defaults to 2 (the lanczos sweet spot).
    :param algorithm: ``"lanczos"`` or ``"realesrgan"``.

    Lanczos-specific:

    :param crf: x264 constant rate factor for the re-encode (lower = larger
        & higher quality; 18 is "visually lossless" for most content).
    :param preset: x264 speed/efficiency preset. ``"medium"`` is the
        sensible default; ``"slow"`` trades CPU for ~10% smaller output.
    :param sharpen: unsharp filter amount, 0.0–1.5. ``0`` disables the
        sharpen pass. ``0.4`` is conservative; ``0.8`` is aggressive.

    Real-ESRGAN-specific:

    :param model: model name. Default ``realesrgan-x4plus``;
        ``realesrgan-x4plus-anime`` and ``realesr-animevideov3`` are
        better for cartoons / anime sources.
    :param binary: explicit path to the ``realesrgan-ncnn-vulkan`` binary.
        Defaults to ``shutil.which("realesrgan-ncnn-vulkan")``.
    :param tile_size: per-tile pixel size, 0 = auto. Lower (32, 64, 128)
        if you hit GPU memory errors.
    :param keep_intermediate: leave the extracted PNG frames on disk for
        debugging. Default deletes them.
    :param chunk_seconds: process the video in N-second segments to bound
        peak disk usage. ``30`` (default) is a good fit for most clips;
        smaller (``10``) if disk is really tight, larger (``60``) if you
        have plenty. ``0`` disables chunking and processes the whole
        video in one shot — only sensible on very short clips with
        plenty of disk.
    :param threads: passed verbatim to ``realesrgan-ncnn-vulkan -j`` as
        ``load:proc:save`` thread counts. Default ``None`` lets the
        binary pick (``1:2:2``). Try ``"1:4:1"`` or ``"1:8:2"`` if you
        have headroom on a beefy GPU.
    """
    from pyt.api.upscale import _Upscale, _VALID_ALGORITHMS

    if algorithm not in _VALID_ALGORITHMS:
        raise ValueError(
            f"upscale algorithm must be one of {list(_VALID_ALGORITHMS)} "
            f"(got {algorithm!r})"
        )

    return _Upscale(
        name="upscale",
        algorithm=algorithm,
        scale=scale,
        crf=crf,
        preset=preset,
        sharpen=sharpen,
        model=model,
        binary=binary,
        tile_size=tile_size,
        keep_intermediate=keep_intermediate,
        chunk_seconds=chunk_seconds,
        threads=threads,
    )


__all__ = [
    "PipelineStep",
    "sponsorblock",
    "embed_metadata",
    "embed_thumbnail",
    "embed_subtitles",
    "extract_audio",
    "upscale",
]
