"""Tests for the UMP wire-format parser in pyt.sabr.ump.

UMP frames look like ``[varint part_type][varint payload_length][payload]``.
The parser is **stateful** — parts can span multiple HTTP responses
(chunked delivery), so the test suite has to exercise both the
single-feed-many-parts case and the part-spanning-multiple-feeds case.

The varint scheme used here is RFC8794-style (5 size classes), NOT
LEB128 / standard protobuf. Every size class gets its own round-trip
test to catch off-by-one bugs at the boundaries.
"""

from __future__ import annotations

import io

import pytest

from pyt.sabr.ump import (
    UmpParser,
    decode_ump_varint,
)
from tests.sabr_fixtures import (
    PT_MEDIA_HEADER,
    PT_MEDIA,
    PT_SABR_REDIRECT,
    MockResponse,
    encode_ump_varint,
    make_ump_part,
)

# ── varint round-trips: every size class ─────────────────────────────────


@pytest.mark.parametrize(
    "value,expected_size",
    [
        (0, 1),
        (1, 1),
        (127, 1),  # last 1-byte value
        (128, 2),  # first 2-byte
        (16_383, 2),  # last 2-byte
        (16_384, 3),  # first 3-byte
        (2_097_151, 3),  # last 3-byte
        (2_097_152, 4),  # first 4-byte
        (268_435_455, 4),  # last 4-byte (2^28 - 1)
        (268_435_456, 5),  # first 5-byte (2^28)
        ((1 << 32) - 1, 5),  # max 5-byte
    ],
)
def test_varint_size_classes(value, expected_size):
    encoded = encode_ump_varint(value)
    assert (
        len(encoded) == expected_size
    ), f"value {value}: expected {expected_size}-byte encoding, got {len(encoded)}"
    decoded, pos = decode_ump_varint(encoded, 0)
    assert decoded == value
    assert pos == len(encoded)


def test_varint_round_trip_at_each_boundary():
    """Specifically verify the byte boundaries: encoding the value just
    before and just after each size jump."""
    boundaries = [
        (127, 128),
        (16_383, 16_384),
        (2_097_151, 2_097_152),
        (268_435_455, 268_435_456),
    ]
    for low, high in boundaries:
        for v in (low, high):
            assert decode_ump_varint(encode_ump_varint(v), 0)[0] == v


def test_varint_rejects_oversize_value():
    """Values that don't fit in 32 bits aren't representable."""
    with pytest.raises(ValueError):
        encode_ump_varint(1 << 32)


def test_varint_rejects_negative():
    with pytest.raises(ValueError):
        encode_ump_varint(-1)


# ── UmpParser: complete-part case ────────────────────────────────────────


def _bytes_response(data: bytes) -> MockResponse:
    return MockResponse(data, headers={})


def test_parser_yields_single_part_in_one_feed():
    payload = b"hello sabr"
    frame = make_ump_part(PT_SABR_REDIRECT, payload)
    parser = UmpParser()
    parts = list(parser.feed(_bytes_response(frame)))
    assert parts == [(PT_SABR_REDIRECT, payload)]
    assert not parser.has_pending


def test_parser_yields_multiple_parts_in_one_feed():
    """A single HTTP response that carries N complete parts must
    yield all N of them in encounter order."""
    a = make_ump_part(PT_MEDIA_HEADER, b"\x08\x01")  # field 1 varint 1
    b = make_ump_part(PT_MEDIA, b"\x05data!")
    c = make_ump_part(PT_SABR_REDIRECT, b"https://x")
    parser = UmpParser()
    parts = list(parser.feed(_bytes_response(a + b + c)))
    types = [pt for pt, _ in parts]
    payloads = [pl for _, pl in parts]
    assert types == [PT_MEDIA_HEADER, PT_MEDIA, PT_SABR_REDIRECT]
    assert payloads == [b"\x08\x01", b"\x05data!", b"https://x"]


# ── UmpParser: chunked / split-across-feeds ──────────────────────────────


def test_parser_buffers_incomplete_header_across_feeds():
    """A part split mid-header (between part_type and length varints)
    must complete on the next feed, with no data loss."""
    payload = b"this-is-the-payload"
    frame = make_ump_part(PT_SABR_REDIRECT, payload)

    # Split somewhere inside the leading varints.
    cut = 1
    parser = UmpParser()
    first = list(parser.feed(_bytes_response(frame[:cut])))
    assert first == []
    assert parser.has_pending is False  # haven't even read part_type yet
    second = list(parser.feed(_bytes_response(frame[cut:])))
    assert second == [(PT_SABR_REDIRECT, payload)]


def test_parser_buffers_incomplete_payload_across_feeds():
    """The most common chunked-delivery case: part header arrives, then
    payload bytes trickle in across multiple HTTP reads."""
    payload = b"X" * 200
    frame = make_ump_part(PT_MEDIA, payload)

    parser = UmpParser()
    # Send the part header + half the payload first.
    half_point = len(frame) - 100
    first = list(parser.feed(_bytes_response(frame[:half_point])))
    assert first == []
    assert parser.has_pending  # mid-payload

    # Send the rest in a second feed.
    second = list(parser.feed(_bytes_response(frame[half_point:])))
    assert second == [(PT_MEDIA, payload)]


def test_parser_handles_payload_split_into_three_feeds():
    """A long payload split into many small reads exercises the
    pending_chunks accumulation path."""
    payload = bytes(range(256)) * 4  # 1024 bytes
    frame = make_ump_part(PT_MEDIA, payload)

    parser = UmpParser()
    splits = [len(frame) // 3, 2 * len(frame) // 3, len(frame)]
    out = []
    last = 0
    for s in splits:
        out.extend(parser.feed(_bytes_response(frame[last:s])))
        last = s
    assert out == [(PT_MEDIA, payload)]


def test_parser_handles_part_a_complete_then_part_b_split():
    """One complete part followed by a partial part. The complete part
    yields immediately; the partial holds for the next feed."""
    a_payload = b"payload-A"
    b_payload = b"X" * 100
    frame_a = make_ump_part(PT_SABR_REDIRECT, a_payload)
    frame_b = make_ump_part(PT_MEDIA, b_payload)

    cut_b = len(frame_b) - 50
    parser = UmpParser()
    first = list(parser.feed(_bytes_response(frame_a + frame_b[:cut_b])))
    assert first == [(PT_SABR_REDIRECT, a_payload)]
    assert parser.has_pending

    second = list(parser.feed(_bytes_response(frame_b[cut_b:])))
    assert second == [(PT_MEDIA, b_payload)]


# ── UmpParser: empty / no-op cases ───────────────────────────────────────


def test_parser_empty_feed_yields_nothing():
    parser = UmpParser()
    assert list(parser.feed(_bytes_response(b""))) == []
    assert not parser.has_pending


def test_parser_zero_length_payload():
    """MEDIA_END parts have a varint-only payload that's typically tiny;
    a part with length=0 is also valid wire format."""
    frame = make_ump_part(PT_MEDIA_HEADER, b"")
    parser = UmpParser()
    assert list(parser.feed(_bytes_response(frame))) == [(PT_MEDIA_HEADER, b"")]


def test_parser_state_persists_across_consecutive_feeds():
    """Verifies UmpParser is reusable for the lifetime of one
    SabrSession — one parser, many HTTP responses."""
    parser = UmpParser()

    a = make_ump_part(PT_MEDIA, b"part1")
    b = make_ump_part(PT_SABR_REDIRECT, b"part2")
    out_a = list(parser.feed(_bytes_response(a)))
    out_b = list(parser.feed(_bytes_response(b)))
    assert out_a == [(PT_MEDIA, b"part1")]
    assert out_b == [(PT_SABR_REDIRECT, b"part2")]


# ── decode_ump_varint: edge cases ─────────────────────────────────────────


def test_decode_ump_varint_advances_position():
    """The (value, new_pos) contract is what every higher-level parser
    builds on. Verify new_pos lands exactly past the encoded bytes."""
    blob = encode_ump_varint(42) + encode_ump_varint(99)
    v1, p1 = decode_ump_varint(blob, 0)
    v2, p2 = decode_ump_varint(blob, p1)
    assert (v1, v2) == (42, 99)
    assert p2 == len(blob)


def test_decode_ump_varint_truncated_one_byte_marker():
    """A leading byte with a 2-byte marker but no follow-up byte is
    truncated — must raise rather than silently produce a wrong
    value."""
    with pytest.raises(ValueError, match="more bytes"):
        decode_ump_varint(bytes([0x80]), 0)


def test_decode_ump_varint_truncated_4_byte_marker():
    """4-byte marker with only 2 follow-up bytes provided."""
    with pytest.raises(ValueError, match="more bytes"):
        decode_ump_varint(bytes([0xE0, 0x12, 0x34]), 0)
