"""Lazy download builder."""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Union
from urllib.error import HTTPError

from pyt.api.errors import DownloadError, PostProcessError
from pyt import request as _req
from pyt.helpers import safe_filename, target_directory

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyt.api.pipeline import PipelineStep
    from pyt.api.streams import StreamRef


PathLike = Union[str, Path]


@dataclass(frozen=True)
class ProgressEvent:
    """Returned to user-supplied callbacks.

    For now only the legacy chunk-based progress is exposed. Once the
    transport moves to httpx we'll add ``Started``, ``Completed``, and
    ``Error`` variants as separate types so callers can pattern-match.
    """

    bytes_done: int
    bytes_total: int
    chunk: bytes


class Download:
    """A planned-but-not-yet-executed download.

    Construct via :meth:`StreamRef.download_to`. Run with ``.run()``, or
    pipe through post-processors with ``.then(...)`` / ``|``.
    """

    def __init__(
        self,
        *,
        stream: "StreamRef",
        output_path: Optional[PathLike] = None,
        filename: Optional[str] = None,
        filename_prefix: Optional[str] = None,
        skip_existing: bool = True,
        timeout: Optional[int] = None,
        max_retries: int = 0,
        steps: Optional[List["PipelineStep"]] = None,
    ):
        self._stream = stream
        self._output_path = str(output_path) if output_path is not None else None
        self._filename = filename
        self._filename_prefix = filename_prefix
        self._skip_existing = skip_existing
        self._timeout = timeout
        self._max_retries = max_retries
        self._steps: List["PipelineStep"] = list(steps or [])

    # ── composition ─────────────────────────────────────────────────────────

    def then(self, *steps: "PipelineStep") -> "Download":
        """Append one or more post-processing steps. Returns a new
        :class:`Download` (the original is not mutated)."""
        return self._replace(steps=self._steps + list(steps))

    def __or__(self, step: "PipelineStep") -> "Download":
        return self.then(step)

    # ── execution ───────────────────────────────────────────────────────────

    def run(self) -> Path:
        """Transfer the bytes to disk, run the pipeline, return the final path."""
        video_id = self._stream.video.video_id
        logger.info(
            "Download.run: starting itag=%d kind=%s subtype=%s output=%s "
            "filename=%s steps=%d (video_id=%s)",
            self._stream.itag,
            self._stream.kind,
            self._stream.subtype,
            self._output_path,
            self._filename,
            len(self._steps),
            video_id,
        )
        t_dl_start = time.monotonic()
        try:
            raw_path = self._transfer_bytes()
        except DownloadError:
            raise
        except Exception as exc:
            logger.warning(
                "Download.run: byte transfer failed for itag=%d after %.2fs: %s",
                self._stream.itag,
                time.monotonic() - t_dl_start,
                exc,
            )
            raise DownloadError(
                f"download failed: {exc}",
                video_id=video_id,
                url=self._stream.video.url,
            ) from exc
        logger.info(
            "Download.run: bytes complete itag=%d in %.2fs path=%s",
            self._stream.itag,
            time.monotonic() - t_dl_start,
            raw_path,
        )

        path = raw_path
        for step in self._steps:
            t_step = time.monotonic()
            logger.debug("Download.run: step '%s' starting on %s", step.name, path)
            try:
                path = step.apply(path, stream=self._stream, video=self._stream.video)
            except PostProcessError:
                logger.warning(
                    "Download.run: step '%s' raised PostProcessError", step.name
                )
                raise
            except Exception as exc:
                logger.warning(
                    "Download.run: step '%s' failed after %.2fs: %s",
                    step.name,
                    time.monotonic() - t_step,
                    exc,
                )
                raise PostProcessError(
                    f"step '{step.name}' failed: {exc}",
                    step=step.name,
                    partial_output_path=path,
                    cause=exc,
                ) from exc
            logger.debug(
                "Download.run: step '%s' done in %.2fs -> %s",
                step.name,
                time.monotonic() - t_step,
                path,
            )

        return Path(path)

    # ── byte transfer ────────────────────────────────────────────────────────

    def _transfer_bytes(self) -> str:
        """Write stream bytes to disk. Returns the file path string."""
        stream = self._stream
        sabr_cfg = getattr(stream.video, "_sabr_config", None)

        # ── file path ──────────────────────────────────────────────────────
        filename = self._filename or stream.default_filename
        if self._filename_prefix:
            filename = f"{self._filename_prefix}{filename}"
        file_path = os.path.join(target_directory(self._output_path), filename)

        # ── skip-existing ──────────────────────────────────────────────────
        if self._skip_existing and os.path.isfile(file_path):
            known = stream.filesize
            if known and os.path.getsize(file_path) == known:
                logger.debug("file %s already exists, skipping", file_path)
                if sabr_cfg and sabr_cfg.on_complete:
                    sabr_cfg.on_complete(stream, file_path)
                return file_path

        # ── transfer ───────────────────────────────────────────────────────
        timeout = (
            self._timeout
            if self._timeout is not None
            else socket._GLOBAL_DEFAULT_TIMEOUT
        )
        max_retries = self._max_retries
        url = stream.url
        extra_headers = (sabr_cfg.stream_headers or {}) if sabr_cfg else {}

        known_filesize = stream.filesize or stream.filesize_approx or 0
        bytes_remaining = known_filesize

        sabr_url = sabr_cfg.sabr_url if sabr_cfg else None
        if url is None and not sabr_url:
            raise DownloadError(
                f"itag {stream.itag}: no direct URL and no SABR URL available "
                "for this stream"
            )

        with open(file_path, "wb") as fh:
            # ── SABR path ──────────────────────────────────────────────────
            sabr_url = sabr_cfg.sabr_url if sabr_cfg else None
            if sabr_url:
                try:
                    for chunk in _req.sabr_stream(
                        sabr_url,
                        stream.itag,
                        ustreamer_config=(
                            sabr_cfg.ustreamer_config if sabr_cfg else None
                        ),
                        po_token=sabr_cfg.po_token if sabr_cfg else None,
                        is_video=(stream.kind == "video"),
                        filesize=known_filesize,
                        duration_ms=(
                            int((sabr_cfg.duration or 0) * 1000) if sabr_cfg else 0
                        ),
                        client_info=sabr_cfg.client_info if sabr_cfg else {},
                        refresh_callback=(
                            sabr_cfg.refresh_sabr_config if sabr_cfg else None
                        ),
                        timeout=timeout,
                    ):
                        bytes_remaining = max(0, bytes_remaining - len(chunk))
                        self._fire_progress(
                            sabr_cfg, stream, chunk, fh, bytes_remaining
                        )
                except Exception as exc:
                    if url is None:
                        raise DownloadError(
                            f"itag {stream.itag}: SABR failed ({exc}) and this "
                            "stream has no direct URL to fall back to"
                        ) from exc
                    logger.warning(
                        "SABR failed (%s), falling back to range download", exc
                    )
                    fh.seek(0)
                    fh.truncate()
                    bytes_remaining = known_filesize
                else:
                    if bytes_remaining == 0:
                        self._fire_complete(sabr_cfg, stream, file_path)
                        return file_path
                    if url is None:
                        raise DownloadError(
                            f"itag {stream.itag}: SABR delivered "
                            f"{known_filesize - bytes_remaining}/{known_filesize} "
                            "bytes and this stream has no direct URL for the "
                            "byte-range fallback"
                        )
                    sabr_offset = known_filesize - bytes_remaining
                    logger.warning(
                        "SABR incomplete (%d bytes remaining); finishing with range",
                        bytes_remaining,
                    )
                    try:
                        for chunk in _req.stream(
                            url,
                            timeout=timeout,
                            max_retries=max_retries,
                            start_byte=sabr_offset,
                            extra_headers=extra_headers or None,
                        ):
                            bytes_remaining -= len(chunk)
                            self._fire_progress(
                                sabr_cfg, stream, chunk, fh, bytes_remaining
                            )
                        self._fire_complete(sabr_cfg, stream, file_path)
                        return file_path
                    except HTTPError as e:
                        if e.code not in (403, 404):
                            raise
                        fh.seek(0)
                        fh.truncate()
                        bytes_remaining = known_filesize

            # ── byte-range / seq path ──────────────────────────────────────
            used_seq = False
            try:
                for chunk in _req.stream(
                    url,
                    timeout=timeout,
                    max_retries=max_retries,
                    extra_headers=extra_headers or None,
                ):
                    bytes_remaining -= len(chunk)
                    self._fire_progress(sabr_cfg, stream, chunk, fh, bytes_remaining)
            except HTTPError as e:
                if e.code not in (403, 404):
                    raise
                fh.seek(0)
                fh.truncate()
                bytes_remaining = known_filesize
                used_seq = True
                for chunk in _req.seq_stream(
                    url, timeout=timeout, max_retries=max_retries
                ):
                    bytes_remaining -= len(chunk)
                    self._fire_progress(sabr_cfg, stream, chunk, fh, bytes_remaining)

        # ── size check ─────────────────────────────────────────────────────
        if stream.filesize and stream.filesize > 0 and not used_seq:
            try:
                actual = os.path.getsize(file_path)
            except OSError:
                actual = None
            if actual is not None:
                shortfall = stream.filesize - actual
                if shortfall > stream.filesize * 0.01:
                    raise DownloadError(
                        f"download incomplete: got {actual} bytes, "
                        f"expected {stream.filesize} ({shortfall} short). "
                        "Retry the download.",
                        video_id=stream.video.video_id,
                        url=stream.video.url,
                    )

        self._fire_complete(sabr_cfg, stream, file_path)
        return file_path

    @staticmethod
    def _fire_progress(
        sabr_cfg, stream, chunk: bytes, fh, bytes_remaining: int
    ) -> None:
        fh.write(chunk)
        if sabr_cfg and sabr_cfg.on_progress:
            sabr_cfg.on_progress(stream, chunk, bytes_remaining)

    @staticmethod
    def _fire_complete(sabr_cfg, stream, file_path: str) -> None:
        if sabr_cfg and sabr_cfg.on_complete:
            sabr_cfg.on_complete(stream, file_path)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _replace(self, **changes: Any) -> "Download":
        kwargs = dict(
            stream=self._stream,
            output_path=self._output_path,
            filename=self._filename,
            filename_prefix=self._filename_prefix,
            skip_existing=self._skip_existing,
            timeout=self._timeout,
            max_retries=self._max_retries,
            steps=self._steps,
        )
        kwargs.update(changes)
        return Download(**kwargs)

    def __repr__(self) -> str:
        n = len(self._steps)
        return f"<Download stream={self._stream!r} steps={n}>"
