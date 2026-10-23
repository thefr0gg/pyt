"""Tests for the protobuf encoder/decoder primitives in pyt.sabr.proto.

These are tiny but load-bearing: every SABR message we send and every
UMP part we parse goes through them. A regression here would cascade
into completely silent wire-format corruption."""

from __future__ import annotations

import struct

import pytest

from pyt.sabr import proto

# ── varint round-trips ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        0,
        1,
        7,
        127,
        128,
        255,
        300,
        16_383,
        16_384,
        65_535,
        65_536,
        1_000_000,
        2_097_151,
        2_097_152,
        268_435_455,
        268_435_456,
        (1 << 31) - 1,
        (1 << 32) - 1,
        (1 << 53) - 1,
    ],
)
def test_varint_round_trip(value):
    encoded = proto.encode_varint(value)
    decoded, pos = proto.decode_varint(encoded, 0)
    assert decoded == value
    assert pos == len(encoded)


def test_decode_varint_truncated():
    """A varint where the continuation bit on the last byte is set
    means we ran off the end of the buffer."""
    # Single byte with continuation bit set, no more bytes available.
    with pytest.raises(ValueError, match="Truncated"):
        proto.decode_varint(bytes([0x80]), 0)


def test_decode_varint_leaves_position_correct_for_followups():
    """``decode_varint`` returns ``(value, new_pos)`` — verifying the
    new_pos on consecutive calls is the basis of every higher-level
    parser working at all."""
    data = proto.encode_varint(42) + proto.encode_varint(1234)
    v1, p1 = proto.decode_varint(data, 0)
    v2, p2 = proto.decode_varint(data, p1)
    assert (v1, v2) == (42, 1234)
    assert p2 == len(data)


# ── field encoders ────────────────────────────────────────────────────────


def test_field_varint_tag_format():
    """Wire tag = ``(field_num << 3) | wire_type``, wire_type=0 for varint."""
    encoded = proto.field_varint(7, 42)
    # Tag for field 7 wire 0 = 0b00111000 = 56
    assert encoded[0] == (7 << 3) | 0
    assert encoded[1:] == proto.encode_varint(42)


def test_field_bytes_tag_format():
    """Wire type 2 (length-delimited)."""
    encoded = proto.field_bytes(3, b"hello")
    assert encoded[0] == (3 << 3) | 2  # tag
    assert encoded[1:] == proto.encode_varint(5) + b"hello"


def test_field_string_encodes_utf8():
    encoded = proto.field_string(1, "héllo")
    body = encoded[1:]
    length, pos = proto.decode_varint(body, 0)
    assert body[pos : pos + length] == "héllo".encode("utf-8")


def test_field_message_is_field_bytes_alias():
    """field_message is just field_bytes by another name."""
    payload = b"\x08\x05"
    assert proto.field_message(1, payload) == proto.field_bytes(1, payload)


# ── read_field ────────────────────────────────────────────────────────────


def test_read_field_varint():
    data = proto.field_varint(7, 42)
    field_num, wire_type, value, pos = proto.read_field(data, 0)
    assert field_num == 7
    assert wire_type == 0
    assert value == 42
    assert pos == len(data)


def test_read_field_bytes():
    data = proto.field_bytes(3, b"hello")
    field_num, wire_type, value, pos = proto.read_field(data, 0)
    assert field_num == 3
    assert wire_type == 2
    assert value == b"hello"


def test_read_field_fixed64():
    """Wire type 1 = 64-bit fixed; not produced by our encoders but
    must parse cleanly when the server sends it."""
    tag = bytes([(1 << 3) | 1])  # field 1, wire 1
    data = tag + struct.pack("<Q", 0xDEADBEEFCAFEBABE)
    field_num, wire_type, value, pos = proto.read_field(data, 0)
    assert wire_type == 1
    assert value == 0xDEADBEEFCAFEBABE


def test_read_field_fixed32():
    """Wire type 5 = 32-bit fixed."""
    tag = bytes([(2 << 3) | 5])  # field 2, wire 5
    data = tag + struct.pack("<I", 0xCAFEBABE)
    field_num, wire_type, value, pos = proto.read_field(data, 0)
    assert wire_type == 5
    assert value == 0xCAFEBABE


def test_read_field_unknown_wire_type():
    """Wire types 3 and 4 are deprecated group markers; we don't
    handle them. The parser should raise rather than silently
    corrupt downstream parsing."""
    bad = bytes([(1 << 3) | 3])  # field 1, wire 3
    with pytest.raises(ValueError, match="wire type"):
        proto.read_field(bad, 0)


# ── parse_fields ──────────────────────────────────────────────────────────


def test_parse_fields_returns_dict_of_lists():
    data = proto.field_varint(1, 100) + proto.field_string(2, "hi")
    fields = proto.parse_fields(data)
    assert fields == {1: [100], 2: [b"hi"]}


def test_parse_fields_repeated_collects_all_values():
    """Repeated fields are stored as a list under their field number,
    in encounter order."""
    data = (
        proto.field_varint(1, 10)
        + proto.field_varint(1, 20)
        + proto.field_varint(1, 30)
    )
    fields = proto.parse_fields(data)
    assert fields[1] == [10, 20, 30]


def test_parse_fields_mixed_repeated_and_singular():
    data = (
        proto.field_varint(1, 1)
        + proto.field_string(2, "a")
        + proto.field_varint(1, 2)
        + proto.field_string(3, "b")
    )
    fields = proto.parse_fields(data)
    assert fields == {1: [1, 2], 2: [b"a"], 3: [b"b"]}


def test_parse_fields_empty():
    assert proto.parse_fields(b"") == {}


# ── round-trip via fixtures (cross-checks with sabr_fixtures.encode_ump_varint) ──


def test_encode_decode_consistency_against_uvarint():
    """The protobuf varint and UMP varint have different framings —
    here we just confirm our protobuf encoder isn't accidentally
    producing UMP-style bytes (which would corrupt every SABR message
    we send)."""
    # 128 in protobuf varint = 0x80 0x01 (low byte then high byte, top
    # bit indicates continuation); in UMP varint = 0x80 0x02 (10xxxxxx
    # marker plus 0x02 in the next byte).
    assert proto.encode_varint(128) == bytes([0x80, 0x01])
