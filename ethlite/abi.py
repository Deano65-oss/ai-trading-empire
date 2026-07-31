"""A small ABI encoder/decoder covering the types this project uses.

Supports `address`, `bool`, `uintN`, `bytes32` and dynamic `bytes`/`string`,
which is everything ObsidianCipher's interface needs. Tuples are handled by
encoding their components in order, which matches how Solidity lays out
`abi.encode(...)`.
"""

from __future__ import annotations

from .keccak import keccak256

WORD = 32


def _is_dynamic(kind: str) -> bool:
    return kind in ("bytes", "string")


def _encode_word(value: int) -> bytes:
    return value.to_bytes(WORD, "big")


def _pad_right(data: bytes) -> bytes:
    remainder = len(data) % WORD
    return data if remainder == 0 else data + b"\x00" * (WORD - remainder)


def encode_single(kind: str, value) -> bytes:
    """Encode one static value into a single 32-byte word."""
    if kind == "address":
        if isinstance(value, str):
            value = int(value.removeprefix("0x"), 16)
        return _encode_word(value)
    if kind == "bool":
        return _encode_word(1 if value else 0)
    if kind.startswith("uint"):
        return _encode_word(int(value))
    if kind == "bytes32":
        if isinstance(value, str):
            value = bytes.fromhex(value.removeprefix("0x"))
        if len(value) != 32:
            raise ValueError("bytes32 must be exactly 32 bytes")
        return value
    raise TypeError(f"unsupported static type {kind}")


def encode(types: list[str], values: list) -> bytes:
    """ABI-encode `values`, matching Solidity's abi.encode()."""
    if len(types) != len(values):
        raise ValueError("types and values must be the same length")

    head = b""
    tail = b""
    head_size = WORD * len(types)

    for kind, value in zip(types, values):
        if _is_dynamic(kind):
            if isinstance(value, str):
                value = value.encode()
            head += _encode_word(head_size + len(tail))
            tail += _encode_word(len(value)) + _pad_right(value)
        else:
            head += encode_single(kind, value)

    return head + tail


def selector(signature: str) -> bytes:
    """First four bytes of the hash of a function signature."""
    return keccak256(signature.encode())[:4]


def topic(signature: str) -> str:
    """Event topic0 for a signature such as 'Sealed(address,bytes32,uint256,uint64)'."""
    return "0x" + keccak256(signature.encode()).hex()


def encode_call(signature: str, values: list | None = None) -> bytes:
    """Build calldata from a full signature like 'seal(bytes32,uint64)'."""
    inner = signature[signature.index("(") + 1:signature.rindex(")")]
    types = [t for t in inner.split(",") if t]
    return selector(signature) + encode(types, values or [])


def decode(types: list[str], data: bytes) -> list:
    """Decode a static-head return payload."""
    out = []
    for index, kind in enumerate(types):
        word = data[index * WORD:(index + 1) * WORD]
        if kind == "address":
            out.append("0x" + word[-20:].hex())
        elif kind == "bool":
            out.append(bool(int.from_bytes(word, "big")))
        elif kind.startswith("uint"):
            out.append(int.from_bytes(word, "big"))
        elif kind == "bytes32":
            out.append(word)
        elif _is_dynamic(kind):
            offset = int.from_bytes(word, "big")
            length = int.from_bytes(data[offset:offset + WORD], "big")
            out.append(data[offset + WORD:offset + WORD + length])
        else:
            raise TypeError(f"unsupported type {kind}")
    return out
