"""Multi-format SABR download: drive one :class:`SabrSession` for an
adaptive video stream + an audio stream, then merge with ffmpeg.

Today the legacy :meth:`pyt.Stream.download` opens a fresh ``SabrSession``
per stream, so a single 1080p+audio download fans out to two parallel
SABR exchanges. Each session gets its own throttle decisions, its own
ad-enforcement context, and its own playback cookie — and YouTube's SABR
server treats them as independent users. Multiplexing both formats over
one session is what real players do; the underlying :class:`SabrSession`
already supports it (``formats=[(video_itag, True), (audio_itag, False)]``)
but no caller wired it up.

This module is that wiring.

If SABR fails to deliver the full byte-count for either format, the
remaining tail is finished via byte-range request — same fallback the
single-format :meth:`Stream.download` path uses. A 99%-complete download
should never die because of one bad SABR exchange.
"""

from __future__ import annotations

import logging
import os
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union
from urllib.error import HTTPError

from pyt import request
from pyt.api._merge import (
    FFmpegMergeError,
    FFmpegNotFound,
    pick_merge_container_for_streams,
    run_ffmpeg_merge,
)
from pyt.api.errors import AttestationRequired, DownloadError, PostProcessError
from pyt.helpers import target_directory
from pyt.sabr.session import SabrAttestationRequired, SabrError, SabrSession

if TYPE_CHECKING:
    from pyt.api.pipeline import PipelineStep
    from pyt.api.streams import StreamRef
    from pyt.api.video import Video


logger = logging.getLogger(__name__)
PathLike = Union[str, Path]


@dataclass
class _PartFile:
    """One open .part file plus the bookkeeping the fallback path needs."""

    itag: int
    path: Path
    fh: Any  # actually io.BufferedWriter, but typing keeps imports light
    expected: int  # 0 if unknown
    written: int = 0
    direct_url: Optional[str] = None  # for byte-range fallback

    def remaining(self) -> int:
        if not self.expected:
            return 0
        return max(0, self.expected - self.written)


class CombinedDownload:
    """Lazy plan to download one video + one audio stream over a single
    SABR session and merge them with ffmpeg.

    Construct via :meth:`Video.download_best` or :meth:`Client.download_pair`;
    direct construction is internal.
    """

    def __init__(
        self,
        *,
        video: "Video",
        video_stream: "StreamRef",
        audio_stream: "StreamRef",
        output_path: Optional[PathLike] = None,
        filename: Optional[str] = None,
        container: Optional[str] = None,
        timeout: Optional[int] = None,
        steps: Optional[List["PipelineStep"]] = None,
    ):
        if video_stream.kind != "video":
            raise ValueError(
                f"video_stream must have kind='video' (got {video_stream.kind!r})"
            )
        if audio_stream.kind != "audio":
            raise ValueError(
                f"audio_stream must have kind='audio' (got {audio_stream.kind!r})"
            )
        if video_stream.is_progressive:
            raise ValueError(
                "video_stream is progressive (already includes audio); use "
                "stream.download_to(...) instead of CombinedDownload"
            )

        self._video = video
        self._video_stream = video_stream
        self._audio_stream = audio_stream
        self._output_path = str(output_path) if output_path is not None else None
        self._filename = filename
        self._container = container
        self._timeout = timeout
        self._steps: List["PipelineStep"] = list(steps or [])

    # ── composition (mirrors Download) ─────────────────────────────────────

    def then(self, *steps: "PipelineStep") -> "CombinedDownload":
        return CombinedDownload(
            video=self._video,
            video_stream=self._video_stream,
            audio_stream=self._audio_stream,
            output_path=self._output_path,
            filename=self._filename,
            container=self._container,
            timeout=self._timeout,
            steps=self._steps + list(steps),
        )

    def __or__(self, step: "PipelineStep") -> "CombinedDownload":
        return self.then(step)

    # ── execution ───────────────────────────────────────────────────────────

    def run(self) -> Path:
        """Drive one SABR session for both formats, merge with ffmpeg,
        run any pipeline steps, return the final :class:`Path`."""
        import time as _time

        t_start = _time.monotonic()
        target_dir = Path(target_directory(self._output_path))
        container = self._container or pick_merge_container_for_streams(
            self._video_stream, self._audio_stream
        )
        logger.info(
            "CombinedDownload.run: video itag=%d (%s) + audio itag=%d (%s) "
            "container=%s output=%s steps=%d (video_id=%s)",
            self._video_stream.itag,
            self._video_stream.video_codec,
            self._audio_stream.itag,
            self._audio_stream.audio_codec,
            container,
            target_dir,
            len(self._steps),
            self._video.video_id,
        )

        if self._filename:
            stem, ext = os.path.splitext(self._filename)
            if ext.lstrip(".").lower() != container:
                final_filename = f"{stem}.{container}"
            else:
                final_filename = self._filename
        else:
            from pyt.helpers import safe_filename as _sf

            stem = _sf(self._video.title or self._video.video_id)
            final_filename = f"{stem}.{container}"
        final_path = target_dir / final_filename

        parts = self._open_parts(target_dir, stem)
        try:
            try:
                self._drive_sabr_session(parts)
            except AttestationRequired as exc:
                # If the Client has a po_token provider, ask it for a
                # fresh token, update the legacy monostate, and retry
                # the SABR session once. We rebuild the part files
                # (truncate + reopen) to start fresh — yt-dlp does the
                # same on PO-token-driven retries.
                if not self._try_refresh_po_token():
                    raise
                logger.info("CombinedDownload: retrying SABR with refreshed PO token")
                # Reset part files for a clean second attempt.
                for p in parts:
                    p.fh.close()
                parts = self._open_parts(target_dir, stem)
                try:
                    self._drive_sabr_session(parts)
                except AttestationRequired:
                    logger.warning(
                        "CombinedDownload: ATTESTATION_REQUIRED still firing "
                        "after PO token refresh; propagating"
                    )
                    raise
            self._finish_with_range_fallback(parts)

            for p in parts:
                p.fh.flush()
                p.fh.close()

            video_part = next(p for p in parts if p.itag == self._video_stream.itag)
            audio_part = next(p for p in parts if p.itag == self._audio_stream.itag)

            try:
                run_ffmpeg_merge(
                    video_part.path,
                    audio_part.path,
                    final_path,
                    container=container,
                )
            except (FFmpegNotFound, FFmpegMergeError) as exc:
                # Leave the .part files behind so the user can retry/inspect.
                raise DownloadError(
                    str(exc),
                    video_id=self._video.video_id,
                    url=self._video.url,
                ) from exc

            for p in parts:
                _silently_unlink(p.path)
        except BaseException:
            # Close any still-open file handles before bubbling. Leave the
            # .part files on disk — they may be resumable on a retry.
            for p in parts:
                try:
                    p.fh.close()
                except Exception:
                    pass
            raise

        # Pipeline steps run on the merged file with the *video* stream as
        # context, matching the legacy CLI behavior in `_run_pp_chain`.
        path: Union[str, Path] = final_path
        for step in self._steps:
            try:
                path = step.apply(
                    str(path),
                    stream=self._video_stream,
                    video=self._video,
                )
            except PostProcessError:
                raise
            except Exception as exc:
                raise PostProcessError(
                    f"step '{step.name}' failed: {exc}",
                    step=step.name,
                    partial_output_path=str(path),
                    cause=exc,
                ) from exc
        return Path(path)

    # ── internals ──────────────────────────────────────────────────────────

    def _open_parts(self, target_dir: Path, base_stem: str) -> List[_PartFile]:
        target_dir.mkdir(parents=True, exist_ok=True)
        out: List[_PartFile] = []
        for stream, role in (
            (self._video_stream, "video"),
            (self._audio_stream, "audio"),
        ):
            path = target_dir / f"{base_stem}.{role}.{stream.subtype}.part"
            fh = open(path, "wb")
            expected = stream.filesize or stream.filesize_approx or 0
            out.append(
                _PartFile(
                    itag=stream.itag,
                    path=path,
                    fh=fh,
                    expected=int(expected) if expected else 0,
                    direct_url=stream.url,
                )
            )
        return out

    def _drive_sabr_session(self, parts: List[_PartFile]) -> None:
        sabr_cfg = self._video._sabr_config
        sabr_url = sabr_cfg.sabr_url if sabr_cfg else None
        if not sabr_url:
            # Pre-SABR account/video — nothing to multiplex. Fall through to
            # the byte-range path which the fallback method already handles.
            logger.info(
                "CombinedDownload: no SABR URL for video_id=%s; "
                "falling back to byte-range for both formats",
                self._video.video_id,
            )
            return

        logger.debug(
            "CombinedDownload: opening SABR session url=%s formats=[(%d,video),(%d,audio)]",
            sabr_url,
            self._video_stream.itag,
            self._audio_stream.itag,
        )

        session = SabrSession(
            sabr_url=sabr_url,
            ustreamer_config=sabr_cfg.ustreamer_config if sabr_cfg else None,
            formats=[
                (self._video_stream.itag, True),
                (self._audio_stream.itag, False),
            ],
            po_token=_decode_po_token(sabr_cfg.po_token if sabr_cfg else None),
            client_info=sabr_cfg.client_info if sabr_cfg else {},
            duration_ms=int((sabr_cfg.duration or 0) * 1000) if sabr_cfg else 0,
            expected_sizes={p.itag: p.expected for p in parts if p.expected},
            timeout=(
                self._timeout
                if self._timeout is not None
                else socket._GLOBAL_DEFAULT_TIMEOUT
            ),
            refresh_callback=sabr_cfg.refresh_sabr_config if sabr_cfg else None,
        )

        by_itag: Dict[int, _PartFile] = {p.itag: p for p in parts}
        try:
            for itag, chunk in session.iter_chunks():
                p = by_itag.get(itag)
                if p is None:
                    continue
                p.fh.write(chunk)
                p.written += len(chunk)
                self._fire_progress(itag, chunk, p)
        except SabrAttestationRequired as exc:
            # Treat ATTESTATION_REQUIRED specially — the byte-range
            # fallback won't help because YouTube's enforcement is at
            # the account/session level, not the protocol level.
            # Translate to the typed AttestationRequired error so the
            # caller can refresh a PO token and retry.
            logger.info(
                "CombinedDownload: ATTESTATION_REQUIRED on itag=%d/%d "
                "for video_id=%s",
                self._video_stream.itag,
                self._audio_stream.itag,
                self._video.video_id,
            )
            raise AttestationRequired(
                video_id=self._video.video_id,
                url=self._video.url,
                message=str(exc),
            ) from exc
        except SabrError as exc:
            # Other SABR failures — let the range-fallback path
            # finish whatever's missing.
            logger.warning(
                "SABR session ended with error (%s); falling back to range", exc
            )

    def _finish_with_range_fallback(self, parts: List[_PartFile]) -> None:
        for p in parts:
            remaining = p.remaining()
            if remaining <= 0:
                logger.debug(
                    "CombinedDownload: itag=%d delivered fully via SABR (%d bytes)",
                    p.itag,
                    p.written,
                )
                continue
            if not p.direct_url:
                raise DownloadError(
                    f"itag {p.itag}: SABR delivered {p.written}/{p.expected} bytes "
                    f"and no direct URL is available for the byte-range fallback",
                    video_id=self._video.video_id,
                    url=self._video.url,
                )
            logger.info(
                "itag %d: SABR left %d/%d bytes; finishing with byte-range",
                p.itag,
                remaining,
                p.expected,
            )
            try:
                self._range_finish(p)
            except HTTPError as exc:
                raise DownloadError(
                    f"itag {p.itag}: byte-range fallback failed: {exc}",
                    video_id=self._video.video_id,
                    url=self._video.url,
                ) from exc

    def _range_finish(self, p: _PartFile) -> None:
        sabr_cfg = self._video._sabr_config
        extra_headers = (sabr_cfg.stream_headers or {}) if sabr_cfg else {}
        for chunk in request.stream(
            p.direct_url,
            timeout=self._timeout,
            max_retries=0,
            start_byte=p.written,
            extra_headers=extra_headers or None,
        ):
            p.fh.write(chunk)
            p.written += len(chunk)
            self._fire_progress(p.itag, chunk, p)

    def _try_refresh_po_token(self) -> bool:
        """Ask the parent :class:`Client` for a fresh PO token and update
        the _SabrConfig so the next SABR session picks it up.
        Returns ``True`` on success, ``False`` if no provider is configured.
        """
        client = getattr(self._video, "_client", None)
        if client is None or not getattr(client, "has_po_token_provider", False):
            logger.info(
                "CombinedDownload: no po_token provider on the parent "
                "Client; cannot retry"
            )
            return False
        try:
            new_token = client.refresh_po_token()
        except Exception as exc:  # noqa: BLE001
            logger.warning("CombinedDownload: po_token refresh failed: %s", exc)
            return False
        if not new_token:
            return False
        sabr_cfg = self._video._sabr_config
        if sabr_cfg is not None:
            sabr_cfg.po_token = new_token
        logger.info(
            "CombinedDownload: refreshed po_token (%d chars) for video_id=%s",
            len(new_token),
            self._video.video_id,
        )
        return True

    def _fire_progress(self, itag: int, chunk: bytes, p: _PartFile) -> None:
        sabr_cfg = self._video._sabr_config
        callback = sabr_cfg.on_progress if sabr_cfg else None
        if callback is None:
            return
        stream = (
            self._video_stream
            if itag == self._video_stream.itag
            else self._audio_stream
        )
        callback(stream, chunk, p.remaining())

    def __repr__(self) -> str:
        return (
            f"<CombinedDownload video={self._video_stream!r} "
            f"audio={self._audio_stream!r} steps={len(self._steps)}>"
        )


def _decode_po_token(value):
    """Mirror SabrClient's PO-token handling so the multi-format path
    accepts the same string the user already passes to Client(po_token=...).
    """
    if value is None or isinstance(value, (bytes, bytearray)):
        return value
    import base64

    s = str(value)
    try:
        padded = s + "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(padded)
    except Exception:
        return s.encode("utf-8")


def _silently_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("could not remove %s: %s", path, exc)
