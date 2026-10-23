"""UMP (Unified Media Protocol) response stream parser.

Each UMP part has the form:
  [ump-varint: part_type][ump-varint: payload_length][bytes: payload]

UMP uses an RFC8794-based varint encoding (NOT standard protobuf LEB128).
The top N bits of the first byte determine byte count:
  0xxxxxxx  -> 1 byte,  value = first & 0x7F
  10xxxxxx  -> 2 bytes, value = (next << 6) | (first & 0x3F)
  110xxxxx  -> 3 bytes, value = LE-uint16(next2) | (first & 0x1F)
  1110xxxx  -> 4 bytes, value = LE-uint24(next3) | (first & 0x0F)
  11110xxx  -> 5 bytes, value = LE-uint32(next4)  (ignores low 3 bits of first)

Parts MAY span multiple HTTP responses (chunked delivery). This parser is
therefore stateful: create one instance and call feed() once per HTTP response.
The caller (SabrSession) is responsible for sending follow-up requests.
"""

import struct
import logging
from typing import Iterator, Optional, Tuple

from pyt.sabr.proto import parse_fields

logger = logging.getLogger(__name__)

# UMP part types (yt-dlp PR #13515 + reverse-engineered).
UMP_ONESIE_HEADER = 10
UMP_ONESIE_DATA = 11
UMP_MEDIA_HEADER = 20  # protobuf: header_id, itag, lmt, segment info
UMP_MEDIA = 21  # raw media bytes, prefixed by header_id (UMP varint)
UMP_MEDIA_END = 22  # empty terminator (1-byte payload = header_id)
UMP_LIVE_METADATA = 31  # field layout unconfirmed - see parse_live_metadata
UMP_HOSTNAME_CHANGE_HINT = 32
UMP_LIVE_METADATA_PROMISE = 33  # field layout unconfirmed
UMP_LIVE_METADATA_PROMISE_CANCEL = 34  # field layout unconfirmed
UMP_NEXT_REQUEST_POLICY = 35
UMP_USTREAMER_VIDEO_AND_FORMAT_DATA = 36
UMP_FORMAT_SELECTION_CONFIG = 37
UMP_USTREAMER_SELECTED_MEDIA_STREAM = 38
UMP_FORMAT_INITIALIZATION_METADATA = 42
UMP_SABR_REDIRECT = 43
UMP_SABR_ERROR = 44
UMP_SABR_SEEK = 45
UMP_RELOAD_PLAYER_RESPONSE = 46
UMP_PLAYBACK_START_POLICY = 47
UMP_ALLOWED_CACHED_FORMATS = 48
UMP_START_BW_SAMPLING_HINT = 49
UMP_PAUSE_BW_SAMPLING_HINT = 50
UMP_SELECTABLE_FORMATS = 51
UMP_REQUEST_IDENTIFIER = 52
UMP_REQUEST_CANCELLATION_POLICY = 53
UMP_ONESIE_PREFETCH_REJECTION = 54
UMP_TIMELINE_CONTEXT = 55
UMP_REQUEST_PIPELINING = 56
UMP_SABR_CONTEXT_UPDATE = 57
UMP_STREAM_PROTECTION_STATUS = 58
UMP_SABR_CONTEXT_SENDING_POLICY = 59
UMP_LAWNMOWER_MESSAGE = 60
UMP_SABR_ACK = 61
UMP_END_OF_TRACK = 62
UMP_CACHE_LOAD_POLICY = 63
UMP_LAWNMOWER_MESSAGING_POLICY = 64
UMP_PREWARM_CONNECTION = 65

_READ_SIZE = 65536


def _ump_varint(data: bytes, pos: int) -> Tuple[int, int]:
    """Decode one UMP RFC8794 varint from *data* starting at *pos*.

    Returns (value, new_pos).  Raises ValueError when more bytes are needed.
    """
    if pos >= len(data):
        raise ValueError("need more bytes")

    first = data[pos]

    if not (first & 0x80):  # 0xxxxxxx - 1 byte
        return first, pos + 1

    if not (first & 0x40):  # 10xxxxxx - 2 bytes
        if pos + 1 >= len(data):
            raise ValueError("need more bytes")
        return (data[pos + 1] << 6) | (first & 0x3F), pos + 2

    if not (first & 0x20):  # 110xxxxx - 3 bytes
        if pos + 2 >= len(data):
            raise ValueError("need more bytes")
        return (first & 0x1F) | (data[pos + 1] << 5) | (data[pos + 2] << 13), pos + 3

    if not (first & 0x10):  # 1110xxxx - 4 bytes
        if pos + 3 >= len(data):
            raise ValueError("need more bytes")
        return (first & 0x0F) | (data[pos + 1] << 4) | (data[pos + 2] << 12) | (
            data[pos + 3] << 20
        ), pos + 4

    # 11110xxx - 5 bytes; reads 4-byte LE uint32 from the next 4 bytes
    if pos + 4 >= len(data):
        raise ValueError("need more bytes")
    return struct.unpack_from("<I", data, pos + 1)[0], pos + 5


def decode_ump_varint(data: bytes, pos: int = 0) -> Tuple[int, int]:
    """Public wrapper around the UMP varint decoder."""
    return _ump_varint(data, pos)


class UmpParser:
    """Stateful UMP parser.  Feed one HTTP response at a time via feed().

    Yields (part_type: int, payload: bytes) for complete UMP parts.
    Incomplete parts are buffered and completed by subsequent feed() calls.
    """

    def __init__(self):
        self._buf = b""
        self._pending_type: Optional[int] = None
        self._pending_remaining: int = 0
        self._pending_chunks: list = []

    def _drain(self) -> Iterator[tuple]:
        """Parse as many complete parts from self._buf as possible."""
        if self._pending_type is not None:
            avail = len(self._buf)
            if avail == 0:
                return
            if avail >= self._pending_remaining:
                self._pending_chunks.append(self._buf[: self._pending_remaining])
                payload = b"".join(self._pending_chunks)
                self._buf = self._buf[self._pending_remaining :]
                pt = self._pending_type
                self._pending_type = None
                self._pending_remaining = 0
                self._pending_chunks = []
                yield pt, payload
            else:
                self._pending_chunks.append(self._buf)
                self._pending_remaining -= avail
                self._buf = b""
                return

        while True:
            if not self._buf:
                break
            pos = 0

            try:
                part_type, pos = _ump_varint(self._buf, pos)
            except ValueError:
                break

            try:
                length, pos = _ump_varint(self._buf, pos)
            except ValueError:
                break

            payload_end = pos + length
            if len(self._buf) >= payload_end:
                payload = self._buf[pos:payload_end]
                self._buf = self._buf[payload_end:]
                yield part_type, payload
            else:
                available = self._buf[pos:]
                self._pending_type = part_type
                self._pending_remaining = length - len(available)
                self._pending_chunks = [available]
                self._buf = b""
                break

    @property
    def has_pending(self) -> bool:
        """True if a part has started but not yet finished."""
        return self._pending_type is not None

    def feed(self, response) -> Iterator[tuple]:
        """Read one HTTP response and yield complete (part_type, payload) pairs."""
        while True:
            chunk = response.read(_READ_SIZE)
            if not chunk:
                break
            self._buf += chunk
            yield from self._drain()


def _decode_str(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None
    return value


def _parse_format_id(value) -> Optional[int]:
    """FormatId message: { 1: itag, 2: lmt, 3: xtags } -> itag."""
    if not isinstance(value, (bytes, bytearray)):
        return None
    inner = parse_fields(bytes(value))
    return inner.get(1, [None])[0]


def _parse_time_range(value) -> dict:
    """TimeRange { 1: start_ticks, 2: duration_ticks, 3: timescale }."""
    if not isinstance(value, (bytes, bytearray)):
        return {}
    f = parse_fields(bytes(value))
    return {
        "start_ticks": f.get(1, [None])[0],
        "duration_ticks": f.get(2, [None])[0],
        "timescale": f.get(3, [None])[0],
    }


def _ticks_to_ms(ticks, timescale) -> Optional[int]:
    if not ticks or not timescale:
        return None
    return int(int(ticks) * 1000 / int(timescale))


def parse_media_header(payload: bytes) -> dict:
    """Decode part-20 (MEDIA_HEADER) protobuf payload.

    Field numbers (yt-dlp _proto/videostreaming/media_header.py):
      1  header_id            (UInt32) - id used by MEDIA / MEDIA_END to reference this header
      2  video_id             (String)
      3  itag                 (Int32)        - flat itag (legacy)
      4  last_modified        (UInt64)
      5  xtags                (String)
      6  start_data_range     (Int32)
      7  compression          (CompressionAlgorithm enum: 0=UNKNOWN, 1=NONE, 2=GZIP)
      8  is_init_segment      (Bool)
      9  sequence_number      (Int64)        - segment number
      10 bitrate_bps          (Int64)
      11 start_ms             (Int32)
      12 duration_ms          (Int32)
      13 format_id            (FormatId message)  - preferred over flat itag (field 3)
      14 content_length       (Int64) - missing for live streams
      15 time_range           (TimeRange message)
      16 sequence_lmt         (Int32)
    """
    fields = parse_fields(payload)

    itag = fields.get(3, [None])[0]
    nested_itag = _parse_format_id(fields.get(13, [None])[0])
    if nested_itag is not None:
        itag = nested_itag

    tr = _parse_time_range(fields.get(15, [None])[0])
    start_ms = fields.get(11, [None])[0]
    if start_ms is None:
        start_ms = _ticks_to_ms(tr.get("start_ticks"), tr.get("timescale"))

    duration_ms = fields.get(12, [None])[0]
    if duration_ms is None:
        duration_ms = _ticks_to_ms(tr.get("duration_ticks"), tr.get("timescale"))

    return {
        "header_id": fields.get(1, [None])[0],
        "itag": itag,
        "lmt": fields.get(4, [None])[0],
        "xtags": _decode_str(fields.get(5, [None])[0]),
        "start_data_range": fields.get(6, [None])[0],
        "compression": fields.get(7, [None])[0],  # 1=NONE, 2=GZIP
        "is_init_seg": bool(fields.get(8, [0])[0]),
        "sequence_number": fields.get(9, [None])[0],
        "bitrate_bps": fields.get(10, [None])[0],
        "start_ms": start_ms,
        "duration_ms": duration_ms,
        "content_length": fields.get(14, [None])[0],
        "time_range": tr or None,
        "sequence_lmt": fields.get(16, [None])[0],
    }


def parse_format_init_metadata(payload: bytes) -> dict:
    """Part 42 (FORMAT_INITIALIZATION_METADATA).

    1 video_id           (String)
    2 format_id          (FormatId message)
    3 end_time_ms        (Int32)
    4 total_segments     (Int32)
    5 mime_type          (String)
    6 init_range         (Range message)
    7 index_range        (Range message)
    9 duration_ticks     (Int32)
    10 duration_timescale (Int32)
    """
    fields = parse_fields(payload)
    return {
        "itag": _parse_format_id(fields.get(2, [None])[0]),
        "end_time_ms": fields.get(3, [None])[0],
        "total_segments": fields.get(4, [None])[0],
        "mime_type": _decode_str(fields.get(5, [None])[0]),
        "duration_ticks": fields.get(9, [None])[0],
        "duration_timescale": fields.get(10, [None])[0],
    }


def parse_next_request_policy(payload: bytes) -> dict:
    """Part 35 (NEXT_REQUEST_POLICY).

    1 target_audio_readahead_ms (Int32)
    2 target_video_readahead_ms (Int32)
    3 max_time_since_last_request_ms (Int32)
    4 backoff_time_ms           (Int32)
    5 min_audio_readahead_ms    (Int32)
    6 min_video_readahead_ms    (Int32)
    7 playback_cookie           (Bytes)
    8 video_id                  (String)
    """
    fields = parse_fields(payload)
    return {
        "target_audio_readahead_ms": fields.get(1, [None])[0],
        "target_video_readahead_ms": fields.get(2, [None])[0],
        "backoff_time_ms": fields.get(4, [None])[0],
        "playback_cookie": fields.get(7, [None])[0],
    }


def parse_sabr_redirect(payload: bytes) -> Optional[str]:
    """Part 43 (SABR_REDIRECT). Field 1 = redirect_url (String)."""
    fields = parse_fields(payload)
    return _decode_str(fields.get(1, [None])[0])


def parse_stream_protection_status(payload: bytes) -> dict:
    """Part 58 (STREAM_PROTECTION_STATUS).

    1 status      (enum)  1=OK, 2=ATTESTATION_PENDING, 3=ATTESTATION_REQUIRED
    2 max_retries (Int32)
    """
    fields = parse_fields(payload)
    return {
        "status": fields.get(1, [None])[0],
        "max_retries": fields.get(2, [None])[0],
    }


def parse_sabr_error(payload: bytes) -> dict:
    """Part 44 (SABR_ERROR).

    1 type   (String)
    2 action (Int32)
    3 error  (Error message: { 1: status_code, 4: type })
    """
    fields = parse_fields(payload)
    err_type = _decode_str(fields.get(1, [None])[0])
    nested = fields.get(3, [None])[0]
    status_code = inner_type = None
    if isinstance(nested, (bytes, bytearray)):
        inner = parse_fields(bytes(nested))
        status_code = inner.get(1, [None])[0]
        inner_type = inner.get(4, [None])[0]
    return {
        "type": err_type,
        "action": fields.get(2, [None])[0],
        "status_code": status_code,
        "error_type": inner_type,
    }


def parse_sabr_context_update(payload: bytes) -> dict:
    """Part 57 (SABR_CONTEXT_UPDATE).

    1 type             (Int32)
    2 scope            (enum: 1=PLAYBACK, 2=REQUEST, 3=WATCH_ENDPOINT, 4=CONTENT_ADS)
    3 value            (Bytes)
    4 send_by_default  (Bool)
    5 write_policy     (enum: 1=OVERWRITE, 2=KEEP_EXISTING)
    """
    fields = parse_fields(payload)
    return {
        "type": fields.get(1, [None])[0],
        "scope": fields.get(2, [None])[0],
        "value": fields.get(3, [None])[0],
        "send_by_default": bool(fields.get(4, [0])[0]),
        "write_policy": fields.get(5, [None])[0],
    }


def parse_sabr_context_sending_policy(payload: bytes) -> dict:
    """Part 59 (SABR_CONTEXT_SENDING_POLICY).

    1 start_policy   (repeated Int32)
    2 stop_policy    (repeated Int32)
    3 discard_policy (repeated Int32)
    """
    fields = parse_fields(payload)
    return {
        "start_policy": list(fields.get(1, [])),
        "stop_policy": list(fields.get(2, [])),
        "discard_policy": list(fields.get(3, [])),
    }


def parse_sabr_seek(payload: bytes) -> dict:
    """Part 45 (SABR_SEEK).

    1 seek_time_ticks (Int32)
    2 timescale       (Int32)
    3 seek_source     (enum)
    """
    fields = parse_fields(payload)
    return {
        "seek_time_ticks": fields.get(1, [None])[0],
        "timescale": fields.get(2, [None])[0],
        "seek_source": fields.get(3, [None])[0],
    }


def parse_reload_player_response(payload: bytes) -> dict:
    """Part 46 (RELOAD_PLAYER_RESPONSE). Sent when the server considers
    the current player response/URLs stale and wants the client to
    refetch rather than keep requesting against them. Field layout is
    not confirmed - returns the raw parsed field dict under 'raw' so
    SabrSession only needs to know that this part type fired, not what
    (if anything) it specifically asked for.
    """
    fields = parse_fields(payload)
    return {"raw": fields}


def parse_live_metadata(payload: bytes) -> dict:
    """Part 31 (LIVE_METADATA). Field layout is NOT confirmed - no reference
    proto or captured traffic has been decoded for this part type yet. This
    returns the raw parsed field dict under 'raw' so real traffic can be
    inspected (enable TRACE logging) instead of guessing at semantics.
    Treat any derived key here as speculative until verified.
    """
    fields = parse_fields(payload)
    return {"raw": fields}
