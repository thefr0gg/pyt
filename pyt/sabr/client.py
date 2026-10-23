"""Builders for the SABR protobuf request body and a thin compatibility wrapper.

The actual protocol loop lives in pyt.sabr.session.SabrSession. This module
only encodes the VideoPlaybackAbrRequest message and provides a single-format
adapter (`SabrClient.stream`) used by request.sabr_stream for backwards
compatibility.

Reference: yt-dlp PR #13515 (coletdjnz:feat/youtube/sabr)
Field numbers verified against:
  yt_dlp/extractor/youtube/_proto/videostreaming/video_playback_abr_request.py
  yt_dlp/extractor/youtube/_proto/videostreaming/streamer_context.py
"""

from __future__ import annotations

import base64
import logging
import socket
from typing import Iterator, List, Optional

from pyt.sabr.proto import field_bytes, field_message, field_string, field_varint

logger = logging.getLogger(__name__)


# -- Protobuf message builders --------------------------------------------------


def _format_id(itag: int) -> bytes:
    """FormatId { 1: itag }"""
    return field_varint(1, itag)


def _client_abr_state(track_types: int, player_time_ms: int = 0) -> bytes:
    """ClientAbrState { 28: player_time_ms, 40: enabled_track_types_bitfield }"""
    msg = b""
    if player_time_ms:
        msg += field_varint(28, player_time_ms)
    msg += field_varint(40, track_types)
    return msg


def _buffered_range(
    itag: int,
    start_time_ms: int,
    duration_ms: int,
    start_segment_index: Optional[int] = None,
    end_segment_index: Optional[int] = None,
) -> bytes:
    """BufferedRange {
        1: format_id,
        2: start_time_ms,
        3: duration_ms,
        4: start_segment_index,
        5: end_segment_index,
    }"""
    msg = field_message(1, _format_id(itag))
    msg += field_varint(2, start_time_ms)
    msg += field_varint(3, duration_ms)
    if start_segment_index is not None:
        msg += field_varint(4, start_segment_index)
    if end_segment_index is not None:
        msg += field_varint(5, end_segment_index)
    return msg


def _client_info(info: dict) -> bytes:
    """ClientInfo (innertube/client_info.py).

    1  hl              (String)
    2  gl              (String)
    4  remote_host     (String)
    12 device_make     (String)
    13 device_model    (String)
    14 visitor_data    (String)
    15 user_agent      (String)
    16 client_name     (ClientName enum / Int32)
    17 client_version  (String)
    18 os_name         (String)
    19 os_version      (String)
    """
    out = b""
    if info.get("hl"):
        out += field_string(1, info["hl"])
    if info.get("gl"):
        out += field_string(2, info["gl"])
    if info.get("device_make"):
        out += field_string(12, info["device_make"])
    if info.get("device_model"):
        out += field_string(13, info["device_model"])
    if info.get("visitor_data"):
        out += field_string(14, info["visitor_data"])
    if info.get("user_agent"):
        out += field_string(15, info["user_agent"])
    if info.get("client_name") is not None:
        out += field_varint(16, int(info["client_name"]))
    if info.get("client_version"):
        out += field_string(17, info["client_version"])
    if info.get("os_name"):
        out += field_string(18, info["os_name"])
    if info.get("os_version"):
        out += field_string(19, info["os_version"])
    return out


def _sabr_context(ctx_type: int, value: bytes) -> bytes:
    """SabrContext { 1: type (Int32), 2: value (Bytes) }."""
    return field_varint(1, int(ctx_type)) + field_bytes(2, value)


def _streamer_context(
    po_token_bytes: Optional[bytes],
    client_info: Optional[dict],
    playback_cookie: Optional[bytes] = None,
    sabr_contexts: Optional[List[tuple]] = None,
    unsent_sabr_contexts: Optional[List[int]] = None,
) -> bytes:
    """StreamerContext {
        1: client_info             (ClientInfo message)
        2: po_token                (Bytes)
        3: playback_cookie         (Bytes)
        5: sabr_contexts           (repeated SabrContext)
        6: unsent_sabr_contexts    (repeated Int32)
    }"""
    out = b""
    if client_info:
        ci = _client_info(client_info)
        if ci:
            out += field_message(1, ci)
    if po_token_bytes:
        out += field_bytes(2, po_token_bytes)
    if playback_cookie:
        out += field_bytes(3, playback_cookie)
    for ctx_type, value in sabr_contexts or []:
        out += field_message(5, _sabr_context(ctx_type, value))
    for t in unsent_sabr_contexts or []:
        out += field_varint(6, int(t))
    return out


def build_video_playback_abr_request(
    *,
    video_itag: Optional[int],
    audio_itag: Optional[int],
    ustreamer_config: Optional[bytes],
    po_token: Optional[bytes],
    client_info: Optional[dict],
    initialized_itags: Optional[List[int]] = None,
    buffered_ranges: Optional[List[dict]] = None,
    player_time_ms: int = 0,
    playback_cookie: Optional[bytes] = None,
    sabr_contexts: Optional[List[tuple]] = None,
    unsent_sabr_contexts: Optional[List[int]] = None,
) -> bytes:
    """Build a VideoPlaybackAbrRequest protobuf message.

    Field numbers from yt-dlp PR #13515:
      1  client_abr_state                    (message)
      2  initialized_format_ids              (repeated message)
      3  buffered_ranges                     (repeated message)
      4  player_time_ms                      (Int64)
      5  video_playback_ustreamer_config     (Bytes)
      16 preferred_audio_format_ids          (repeated message)
      17 preferred_video_format_ids          (repeated message)
      19 streamer_context                    (message)
    """
    track_types = 0
    if video_itag:
        track_types |= 2
    if audio_itag:
        track_types |= 1

    msg = b""

    msg += field_message(1, _client_abr_state(track_types, player_time_ms))

    for itag in initialized_itags or []:
        msg += field_message(2, _format_id(itag))

    for r in buffered_ranges or []:
        msg += field_message(
            3,
            _buffered_range(
                itag=r["itag"],
                start_time_ms=r.get("start_time_ms", 0),
                duration_ms=r.get("duration_ms", 0),
                start_segment_index=r.get("start_segment_index"),
                end_segment_index=r.get("end_segment_index"),
            ),
        )

    if player_time_ms:
        msg += field_varint(4, player_time_ms)

    if ustreamer_config:
        msg += field_bytes(5, ustreamer_config)

    if audio_itag:
        msg += field_message(16, _format_id(audio_itag))
    if video_itag:
        msg += field_message(17, _format_id(video_itag))

    sc = _streamer_context(
        po_token,
        client_info,
        playback_cookie,
        sabr_contexts=sabr_contexts,
        unsent_sabr_contexts=unsent_sabr_contexts,
    )
    if sc:
        msg += field_message(19, sc)

    return msg


def _decode_po_token(po_token: Optional[str]) -> Optional[bytes]:
    if not po_token:
        return None
    try:
        padded = po_token + "=" * (-len(po_token) % 4)
        return base64.urlsafe_b64decode(padded)
    except Exception:
        return po_token.encode("utf-8")


# -- Single-format adapter (backwards-compatible) ------------------------------


class SabrClient:
    """Single-format SABR adapter delegating to SabrSession.

    Kept so request.sabr_stream and any callers using the old API continue to
    work. New code should use pyt.sabr.session.SabrSession directly.
    """

    def __init__(
        self,
        sabr_url: str,
        itag: int,
        ustreamer_config: Optional[bytes] = None,
        po_token: Optional[str] = None,
        is_video: bool = True,
        filesize: int = 0,
        duration_ms: int = 0,
        already_downloaded: int = 0,
        client_info: Optional[dict] = None,
        refresh_callback=None,
    ):
        self._sabr_url = sabr_url
        self._itag = itag
        self._ustreamer_config = ustreamer_config
        self._po_token_bytes = _decode_po_token(po_token)
        self._is_video = is_video
        self._filesize = filesize
        self._duration_ms = duration_ms
        self._already_downloaded = already_downloaded
        self._client_info = client_info
        self._refresh_callback = refresh_callback

    def stream(self, timeout=socket._GLOBAL_DEFAULT_TIMEOUT) -> Iterator[bytes]:
        """Yield raw media bytes for the requested itag."""
        # Imported here to avoid a session<->client import cycle at module load.
        from pyt.sabr.session import SabrSession, _ResumeState

        resume = None
        if self._already_downloaded > 0:
            resume = {
                self._itag: _ResumeState(
                    downloaded_bytes=self._already_downloaded,
                    last_segment_index=-1,
                )
            }

        session = SabrSession(
            sabr_url=self._sabr_url,
            ustreamer_config=self._ustreamer_config,
            formats=[(self._itag, self._is_video)],
            po_token=self._po_token_bytes,
            client_info=self._client_info,
            duration_ms=self._duration_ms,
            expected_sizes={self._itag: self._filesize} if self._filesize else None,
            resume=resume,
            timeout=timeout,
            refresh_callback=self._refresh_callback,
        )
        for itag, chunk in session.iter_chunks():
            if itag == self._itag:
                yield chunk
