"""Builders for SABR / UMP test fixtures.

Two layers:

1. **Low-level** (``encode_ump_varint``, ``make_ump_part``): produce raw
   UMP wire bytes from primitive values. Useful for testing the parser
   in isolation.

2. **High-level** (``make_media_header``, ``make_sabr_redirect``, etc.):
   produce complete UMP parts for each protobuf message we care about.
   These are what session-level tests use to compose realistic SABR
   responses.

The functions here intentionally mirror the structure of the wire
format (one helper per part type, named after the UMP constant they
emit). When YouTube changes a field number, the test suite breaks at
the helper, not in the test bodies — making it obvious where to fix.

A :class:`MockResponse` rounds out the package: a simple file-like
that mimics ``urllib`` responses so tests can hand pre-built UMP
bytes to ``UmpParser.feed()`` and ``SabrSession``'s HTTP loop without
hitting the network.
"""

from __future__ import annotations

import gzip
import io
import struct
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from pyt.sabr.proto import (
    encode_varint,
    field_bytes,
    field_message,
    field_string,
    field_varint,
)

# ── UMP varint encoder (mirror of pyt.sabr.ump._ump_varint decoding) ──────


def encode_ump_varint(value: int) -> bytes:
    """RFC8794-style varint, the exact format SABR's UMP frames use.

    The decoder lives in ``pyt.sabr.ump._ump_varint``; this is the
    inverse, written for tests so we don't need to round-trip via the
    decoder to build fixtures.

    Size classes:
      0..127           → 1 byte  (0xxxxxxx)
      128..16,383      → 2 bytes (10xxxxxx + extra)
      16,384..2,097,151 → 3 bytes (110xxxxx + 2 extra)
      2,097,152..2^28-1 → 4 bytes (1110xxxx + 3 extra)
      2^28..2^32-1     → 5 bytes (11110xxx + LE uint32)
    """
    if value < 0 or value >= (1 << 32):
        raise ValueError(f"value {value!r} doesn't fit in a UMP varint")

    if value < 0x80:
        return bytes([value])

    if value < (1 << 14):
        # 10xxxxxx + 1 byte; first holds low 6 bits, second holds bits 6..13
        return bytes([0x80 | (value & 0x3F), (value >> 6) & 0xFF])

    if value < (1 << 21):
        # 110xxxxx + 2 bytes; first holds low 5 bits, then 8 bits, then 8 bits
        return bytes(
            [
                0xC0 | (value & 0x1F),
                (value >> 5) & 0xFF,
                (value >> 13) & 0xFF,
            ]
        )

    if value < (1 << 28):
        # 1110xxxx + 3 bytes
        return bytes(
            [
                0xE0 | (value & 0x0F),
                (value >> 4) & 0xFF,
                (value >> 12) & 0xFF,
                (value >> 20) & 0xFF,
            ]
        )

    # 11110xxx + 4 bytes LE uint32 (low 3 bits of the leading byte are
    # ignored by the decoder).
    return bytes([0xF0]) + struct.pack("<I", value)


def make_ump_part(part_type: int, payload: bytes) -> bytes:
    """Wrap a payload in a UMP part header.

    Frame: ``[varint part_type][varint payload_length][payload]``.
    """
    return encode_ump_varint(part_type) + encode_ump_varint(len(payload)) + payload


def concat_parts(*parts: bytes) -> bytes:
    """Concatenate already-framed UMP parts into a complete response body."""
    return b"".join(parts)


def gzip_wrap(body: bytes) -> bytes:
    """Wrap a body in gzip framing for testing the
    ``Content-Encoding: gzip`` path of :class:`SabrSession`."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(body)
    return buf.getvalue()


# ── Per-part-type builders ────────────────────────────────────────────────


# Mirror of UMP part-type constants from pyt/sabr/ump.py. Re-defined
# here so tests don't depend on import order (and so when YouTube adds
# a new part type, the wire-format constants stay together).
PT_MEDIA_HEADER = 20
PT_MEDIA = 21
PT_MEDIA_END = 22
PT_NEXT_REQUEST_POLICY = 35
PT_FORMAT_INITIALIZATION_METADATA = 42
PT_SABR_REDIRECT = 43
PT_SABR_ERROR = 44
PT_SABR_SEEK = 45
PT_LIVE_METADATA = 31
PT_SABR_CONTEXT_UPDATE = 57
PT_STREAM_PROTECTION_STATUS = 58
PT_SABR_CONTEXT_SENDING_POLICY = 59


def _format_id(itag: int) -> bytes:
    """FormatId message: { 1: itag } — used in MEDIA_HEADER field 13."""
    return field_varint(1, itag)


def make_media_header(
    *,
    header_id: int,
    itag: int,
    sequence_number: Optional[int] = None,
    is_init_seg: bool = False,
    content_length: Optional[int] = None,
    start_ms: Optional[int] = None,
    duration_ms: Optional[int] = None,
    compression: Optional[int] = None,
) -> bytes:
    """Build a MEDIA_HEADER (part 20) UMP part.

    The MEDIA / MEDIA_END parts that follow refer back to this
    *header_id*; passing the same value across the three keeps the
    server-style demux working.
    """
    msg = field_varint(1, header_id)
    msg += field_message(13, _format_id(itag))
    if is_init_seg:
        msg += field_varint(8, 1)
    if sequence_number is not None:
        msg += field_varint(9, sequence_number)
    if start_ms is not None:
        msg += field_varint(11, start_ms)
    if duration_ms is not None:
        msg += field_varint(12, duration_ms)
    if content_length is not None:
        msg += field_varint(14, content_length)
    if compression is not None:
        msg += field_varint(7, compression)
    return make_ump_part(PT_MEDIA_HEADER, msg)


def make_media(*, header_id: int, data: bytes) -> bytes:
    """MEDIA (part 21): leading UMP-varint header_id + payload bytes."""
    return make_ump_part(PT_MEDIA, encode_ump_varint(header_id) + data)


def make_media_end(*, header_id: int) -> bytes:
    """MEDIA_END (part 22): single UMP-varint header_id, no body."""
    return make_ump_part(PT_MEDIA_END, encode_ump_varint(header_id))


def make_format_init(
    *,
    itag: int,
    total_segments: Optional[int] = None,
    end_time_ms: Optional[int] = None,
    mime_type: Optional[str] = None,
) -> bytes:
    """FORMAT_INITIALIZATION_METADATA (part 42)."""
    msg = field_message(2, _format_id(itag))
    if end_time_ms is not None:
        msg += field_varint(3, end_time_ms)
    if total_segments is not None:
        msg += field_varint(4, total_segments)
    if mime_type is not None:
        msg += field_string(5, mime_type)
    return make_ump_part(PT_FORMAT_INITIALIZATION_METADATA, msg)


def make_next_request_policy(
    *,
    backoff_time_ms: Optional[int] = None,
    playback_cookie: Optional[bytes] = None,
    target_audio_readahead_ms: Optional[int] = None,
    target_video_readahead_ms: Optional[int] = None,
) -> bytes:
    """NEXT_REQUEST_POLICY (part 35)."""
    msg = b""
    if target_audio_readahead_ms is not None:
        msg += field_varint(1, target_audio_readahead_ms)
    if target_video_readahead_ms is not None:
        msg += field_varint(2, target_video_readahead_ms)
    if backoff_time_ms is not None:
        msg += field_varint(4, backoff_time_ms)
    if playback_cookie is not None:
        msg += field_bytes(7, playback_cookie)
    return make_ump_part(PT_NEXT_REQUEST_POLICY, msg)


def make_sabr_redirect(url: str) -> bytes:
    """SABR_REDIRECT (part 43): single string field."""
    return make_ump_part(PT_SABR_REDIRECT, field_string(1, url))


def make_sabr_error(
    *,
    type: Optional[str] = None,
    action: Optional[int] = None,
    status_code: Optional[int] = None,
    error_type: Optional[int] = None,
) -> bytes:
    """SABR_ERROR (part 44).

    ``status_code`` and ``error_type`` go in the nested ``Error`` message
    (field 3 of the SABR_ERROR), matching yt-dlp's wire format.
    """
    msg = b""
    if type is not None:
        msg += field_string(1, type)
    if action is not None:
        msg += field_varint(2, action)
    if status_code is not None or error_type is not None:
        nested = b""
        if status_code is not None:
            nested += field_varint(1, status_code)
        if error_type is not None:
            nested += field_varint(4, error_type)
        msg += field_message(3, nested)
    return make_ump_part(PT_SABR_ERROR, msg)


def make_stream_protection_status(
    status: int,
    max_retries: Optional[int] = None,
) -> bytes:
    """STREAM_PROTECTION_STATUS (part 58). status: 1=OK, 2=PENDING, 3=REQUIRED."""
    msg = field_varint(1, status)
    if max_retries is not None:
        msg += field_varint(2, max_retries)
    return make_ump_part(PT_STREAM_PROTECTION_STATUS, msg)


def make_sabr_context_update(
    *,
    type: int,
    scope: Optional[int] = None,
    value: bytes = b"",
    send_by_default: bool = False,
    write_policy: Optional[int] = None,
) -> bytes:
    """SABR_CONTEXT_UPDATE (part 57). scope=4 is CONTENT_ADS."""
    msg = field_varint(1, type)
    if scope is not None:
        msg += field_varint(2, scope)
    if value:
        msg += field_bytes(3, value)
    if send_by_default:
        msg += field_varint(4, 1)
    if write_policy is not None:
        msg += field_varint(5, write_policy)
    return make_ump_part(PT_SABR_CONTEXT_UPDATE, msg)


def make_sabr_seek(
    *,
    seek_time_ticks: int,
    timescale: int,
    seek_source: Optional[int] = None,
) -> bytes:
    """SABR_SEEK (part 45)."""
    msg = field_varint(1, seek_time_ticks)
    msg += field_varint(2, timescale)
    if seek_source is not None:
        msg += field_varint(3, seek_source)
    return make_ump_part(PT_SABR_SEEK, msg)


def make_live_metadata(raw_fields: Optional[Mapping[int, int]] = None) -> bytes:
    """LIVE_METADATA (part 31). Real field layout is unconfirmed (see
    pyt.sabr.ump.parse_live_metadata) — this emits generic varint fields
    keyed by field number so tests can assert on the inert-logging
    contract without encoding semantics we haven't verified."""
    msg = b""
    for field_num, value in (raw_fields or {}).items():
        msg += field_varint(field_num, value)
    return make_ump_part(PT_LIVE_METADATA, msg)


def make_sabr_context_sending_policy(
    *,
    start: Sequence[int] = (),
    stop: Sequence[int] = (),
    discard: Sequence[int] = (),
) -> bytes:
    """SABR_CONTEXT_SENDING_POLICY (part 59): repeated int32 lists."""
    msg = b""
    for t in start:
        msg += field_varint(1, t)
    for t in stop:
        msg += field_varint(2, t)
    for t in discard:
        msg += field_varint(3, t)
    return make_ump_part(PT_SABR_CONTEXT_SENDING_POLICY, msg)


# ── MockResponse: stand-in for urllib's response object ───────────────────


class MockResponse:
    """File-like wrapper around bytes, plus a ``headers.get(name)`` shim.

    SabrSession reads via ``response.read(n)`` and checks
    ``response.headers.get('Content-Encoding')``. Both routes are
    covered. Multiple ``read()`` calls work — the buffer is consumed.

    Pass ``headers={}`` to skip the Content-Encoding check; pass
    ``{'Content-Encoding': 'gzip'}`` to opt into the gzip-wrap path
    (the buffer must then be gzipped, normally via :func:`gzip_wrap`).
    """

    def __init__(
        self,
        data: bytes,
        *,
        headers: Optional[Mapping[str, str]] = None,
        chunk_size: int = 65536,
    ):
        self._buf = io.BytesIO(data)
        self.headers = _Headers(headers or {})
        self._chunk_size = chunk_size

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)


class _Headers:
    """Tiny case-insensitive headers shim.

    urllib's response.headers is an ``email.message.Message`` whose
    ``get(name, default=None)`` is case-insensitive. SabrSession only
    uses ``.get('Content-Encoding')`` so a 5-line shim is plenty.
    """

    def __init__(self, mapping: Mapping[str, str]):
        self._items = {k.lower(): v for k, v in mapping.items()}

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self._items.get(name.lower(), default)


# ── Full-response builders ────────────────────────────────────────────────


def build_segment_response(
    *,
    header_id: int,
    itag: int,
    sequence_number: int,
    data: bytes,
    is_init_seg: bool = False,
    end_segment_index: Optional[int] = None,
    extra_parts: Iterable[bytes] = (),
) -> bytes:
    """Compose a typical "one media segment delivered" response:
    MEDIA_HEADER + MEDIA + MEDIA_END, optionally preceded by
    FORMAT_INITIALIZATION_METADATA if ``end_segment_index`` is set.

    The session uses ``end_segment_index`` (from FORMAT_INITIALIZATION_METADATA's
    ``total_segments``) to know when a format is done.
    """
    parts: List[bytes] = []
    if end_segment_index is not None:
        parts.append(make_format_init(itag=itag, total_segments=end_segment_index))
    parts.append(
        make_media_header(
            header_id=header_id,
            itag=itag,
            sequence_number=sequence_number,
            is_init_seg=is_init_seg,
            content_length=len(data) if not is_init_seg else None,
        )
    )
    parts.append(make_media(header_id=header_id, data=data))
    parts.append(make_media_end(header_id=header_id))
    parts.extend(extra_parts)
    return concat_parts(*parts)


def build_multi_format_response(
    *,
    formats: Sequence[Tuple[int, int, bytes]],  # (header_id, itag, data)
    sequence_number: int = 1,
    end_segment_index: Optional[int] = None,
    extra_parts: Iterable[bytes] = (),
) -> bytes:
    """Compose a response that delivers one segment for each format,
    interleaved (header, media, end, header, media, end ...).

    Multi-format SABR is the case where the test value is highest —
    the demux logic is what would silently corrupt output if it broke.
    """
    parts: List[bytes] = []
    for header_id, itag, data in formats:
        if end_segment_index is not None:
            parts.append(make_format_init(itag=itag, total_segments=end_segment_index))
        parts.append(
            make_media_header(
                header_id=header_id,
                itag=itag,
                sequence_number=sequence_number,
                content_length=len(data),
            )
        )
        parts.append(make_media(header_id=header_id, data=data))
        parts.append(make_media_end(header_id=header_id))
    parts.extend(extra_parts)
    return concat_parts(*parts)
