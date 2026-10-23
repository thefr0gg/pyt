"""Minimal protobuf varint encoder/decoder for SABR protocol.

Only wire types 0 (varint), 2 (len-delimited), and 5 (32-bit) are needed.
No external dependencies.
"""

import struct


def encode_varint(value: int) -> bytes:
    parts = []
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            parts.append(bits | 0x80)
        else:
            parts.append(bits)
            break
    return bytes(parts)


def decode_varint(data: bytes, pos: int) -> tuple:
    """Return (value, new_pos). Raises ValueError on truncated data."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    raise ValueError("Truncated varint")


def field_varint(field_num: int, value: int) -> bytes:
    return encode_varint((field_num << 3) | 0) + encode_varint(value)


def field_bytes(field_num: int, data: bytes) -> bytes:
    return encode_varint((field_num << 3) | 2) + encode_varint(len(data)) + data


def field_string(field_num: int, s: str) -> bytes:
    return field_bytes(field_num, s.encode("utf-8"))


def field_message(field_num: int, msg: bytes) -> bytes:
    return field_bytes(field_num, msg)


def read_field(data: bytes, pos: int) -> tuple:
    """Read one field. Returns (field_num, wire_type, value, new_pos)."""
    tag, pos = decode_varint(data, pos)
    field_num = tag >> 3
    wire_type = tag & 0x7
    if wire_type == 0:
        value, pos = decode_varint(data, pos)
    elif wire_type == 1:
        value = struct.unpack_from("<Q", data, pos)[0]
        pos += 8
    elif wire_type == 2:
        length, pos = decode_varint(data, pos)
        value = data[pos : pos + length]
        pos += length
    elif wire_type == 5:
        value = struct.unpack_from("<I", data, pos)[0]
        pos += 4
    else:
        raise ValueError(f"Unknown wire type {wire_type} in field {field_num}")
    return field_num, wire_type, value, pos


def parse_fields(data: bytes) -> dict:
    """Parse all protobuf fields into {field_num: [values]}."""
    result = {}
    pos = 0
    while pos < len(data):
        field_num, _wt, value, pos = read_field(data, pos)
        result.setdefault(field_num, []).append(value)
    return result
