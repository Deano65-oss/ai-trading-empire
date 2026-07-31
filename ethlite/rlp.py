"""RLP encoding, the serialisation Ethereum uses for transactions."""

from __future__ import annotations


def _to_minimal_bytes(value: int) -> bytes:
    if value < 0:
        raise ValueError("cannot encode a negative integer")
    if value == 0:
        return b""
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def _encode_length(length: int, offset: int) -> bytes:
    if length < 56:
        return bytes([offset + length])
    encoded = _to_minimal_bytes(length)
    return bytes([offset + 55 + len(encoded)]) + encoded


def _encode_bytes(data: bytes) -> bytes:
    if len(data) == 1 and data[0] < 0x80:
        return data
    return _encode_length(len(data), 0x80) + data


def encode_raw_list(items: list[bytes]) -> bytes:
    """Wrap already-encoded RLP items in a list header.

    Passing encoded items to `encode` would encode them a second time as byte
    strings; this concatenates them as-is, which is what a list of signed
    transactions needs.
    """
    payload = b"".join(items)
    return _encode_length(len(payload), 0xC0) + payload


def encode(item) -> bytes:
    """Encode bytes, ints (as minimal big-endian) or nested lists thereof."""
    if isinstance(item, (bytes, bytearray)):
        return _encode_bytes(bytes(item))
    if isinstance(item, bool):
        raise TypeError("refusing to encode a bool; pass an int")
    if isinstance(item, int):
        return _encode_bytes(_to_minimal_bytes(item))
    if isinstance(item, (list, tuple)):
        payload = b"".join(encode(element) for element in item)
        return _encode_length(len(payload), 0xC0) + payload
    raise TypeError(f"cannot rlp-encode {type(item).__name__}")
