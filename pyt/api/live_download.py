"""Native SABR live-stream download: drive one :class:`SabrSession` in
live mode for a video + audio stream pair, then merge with ffmpeg once
the caller stops the recording.

Unlike :class:`pyt.api.combined.CombinedDownload`, a live download has no
natural end-of-stream — the SABR server never announces a total segment
count or byte size for an open broadcast. Termination is driven by the
caller (Ctrl-C / :meth:`SabrSession.stop`) or a recognized SABR_ERROR
broadcast-end signal, not by reaching an expected size. Byte-range
fallback (used by CombinedDownload to finish whatever SABR didn't
deliver) doesn't apply here either, since there's no fixed target to
fall back to.

:meth:`pyt.api.video.Video.record_live` remains the simpler ffmpeg/HLS
capture path and is not replaced by this — it covers live configurations
(audio-only unusual itags, missing serverAbrStreamingUrl) this SABR path
may not yet handle.
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from pyt.api._merge import (
    FFmpegMergeError,
    FFmpegNotFound,
    pick_merge_container_for_streams,
    run_ffmpeg_merge,
)
from pyt.api.combined import _decode_po_token, _silently_unlink
from pyt.api.errors import AttestationRequired, DownloadError, PostProcessError
from pyt.helpers import target_directory
from pyt.sabr.session import SabrAttestationRequired, SabrError, SabrSession

if TYPE_CHECKING:
    from pyt.api.streams import StreamRef
    from pyt.api.video import Video


logger = logging.getLogger(__name__)
PathLike = Union[str, Path]


@dataclass
class _LivePartFile:
    """One open .part file for a live format — no expected size, since
    an open broadcast has no fixed total."""

    itag: int
    path: Path
    fh: Any
    written: int = 0


class LiveDownload:
    """Lazy plan to capture a live broadcast via a native SABR session
    until stopped, then merge with ffmpeg.

    Construct via :meth:`Video.download_live`; direct construction is
    internal.
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
    ):
        if video_stream.kind != "video":
            raise ValueError(
                f"video_stream must have kind='video' (got {video_stream.kind!r})"
            )
        if audio_stream.kind != "audio":
            raise ValueError(
                f"audio_stream must have kind='audio' (got {audio_stream.kind!r})"
            )

        self._video = video
        self._video_stream = video_stream
        self._audio_stream = audio_stream
        self._output_path = str(output_path) if output_path is not None else None
        self._filename = filename
        self._container = container
        self._timeout = timeout
        self._session: Optional[SabrSession] = None

    # ── execution ───────────────────────────────────────────────────────────

    def stop(self) -> None:
        """Request a clean stop. Safe to call from a signal handler — the
        in-flight SABR round finishes, then :meth:`run` merges and returns."""
        if self._session is not None:
            self._session.stop()

    def run(self) -> Path:
        """Drive the live SABR session until stopped, merge with ffmpeg,
        and return the final :class:`Path`.

        A ``KeyboardInterrupt`` (Ctrl-C) during capture is treated as a
        stop request: the in-flight round finishes and the recording is
        merged normally instead of being discarded."""
        target_dir = Path(target_directory(self._output_path))
        container = self._container or pick_merge_container_for_streams(
            self._video_stream, self._audio_stream
        )
        logger.info(
            "LiveDownload.run: video itag=%d (%s) + audio itag=%d (%s) "
            "container=%s output=%s (video_id=%s)",
            self._video_stream.itag,
            self._video_stream.video_codec,
            self._audio_stream.itag,
            self._audio_stream.audio_codec,
            container,
            target_dir,
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
            except KeyboardInterrupt:
                logger.info("LiveDownload: interrupted; finalizing capture so far")

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
                raise DownloadError(
                    str(exc),
                    video_id=self._video.video_id,
                    url=self._video.url,
                ) from exc

            for p in parts:
                _silently_unlink(p.path)
        except BaseException:
            for p in parts:
                try:
                    p.fh.close()
                except Exception:
                    pass
            raise
        return final_path

    # ── internals ──────────────────────────────────────────────────────────

    def _open_parts(self, target_dir: Path, base_stem: str) -> List[_LivePartFile]:
        target_dir.mkdir(parents=True, exist_ok=True)
        out: List[_LivePartFile] = []
        for stream, role in (
            (self._video_stream, "video"),
            (self._audio_stream, "audio"),
        ):
            path = target_dir / f"{base_stem}.{role}.{stream.subtype}.part"
            fh = open(path, "wb")
            out.append(_LivePartFile(itag=stream.itag, path=path, fh=fh))
        return out

    def _drive_sabr_session(self, parts: List[_LivePartFile]) -> None:
        sabr_cfg = self._video._sabr_config
        sabr_url = sabr_cfg.sabr_url if sabr_cfg else None
        if not sabr_url:
            raise DownloadError(
                "no SABR URL available for this live stream; try "
                "Video.record_live() instead",
                video_id=self._video.video_id,
                url=self._video.url,
            )

        logger.debug(
            "LiveDownload: opening live SABR session url=%s formats=[(%d,video),(%d,audio)]",
            sabr_url,
            self._video_stream.itag,
            self._audio_stream.itag,
        )

        self._session = SabrSession(
            sabr_url=sabr_url,
            ustreamer_config=sabr_cfg.ustreamer_config if sabr_cfg else None,
            formats=[
                (self._video_stream.itag, True),
                (self._audio_stream.itag, False),
            ],
            po_token=_decode_po_token(sabr_cfg.po_token if sabr_cfg else None),
            client_info=sabr_cfg.client_info if sabr_cfg else {},
            timeout=(
                self._timeout
                if self._timeout is not None
                else socket._GLOBAL_DEFAULT_TIMEOUT
            ),
            refresh_callback=sabr_cfg.refresh_sabr_config if sabr_cfg else None,
            live=True,
        )

        by_itag: Dict[int, _LivePartFile] = {p.itag: p for p in parts}
        try:
            for itag, chunk in self._session.iter_chunks():
                p = by_itag.get(itag)
                if p is None:
                    continue
                p.fh.write(chunk)
                p.written += len(chunk)
        except SabrAttestationRequired as exc:
            logger.info(
                "LiveDownload: ATTESTATION_REQUIRED on itag=%d/%d for video_id=%s",
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
            logger.warning("LiveDownload: SABR session ended with error (%s)", exc)

    def __repr__(self) -> str:
        return (
            f"<LiveDownload video={self._video_stream!r} "
            f"audio={self._audio_stream!r}>"
        )
