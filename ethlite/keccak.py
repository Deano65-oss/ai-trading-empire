"""Keccak-256 (the pre-standardisation variant Ethereum uses).

Python's hashlib ships SHA3-256, which differs from Ethereum's keccak256 only
in the padding byte, so it cannot be used here. This is a self-contained
implementation of Keccak-f[1600] with the original 0x01 padding.

Correctness is pinned by the vectors in tests/test_ethlite.py, and indirectly
by every address this package derives.
"""

from __future__ import annotations

_MASK64 = (1 << 64) - 1

_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

# Rotation offsets r[x][y], indexed as _ROTATIONS[x][y].
_ROTATIONS = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)

_RATE = 136  # 1088 bits, the rate for Keccak-256


def _rotl64(value: int, shift: int) -> int:
    if shift == 0:
        return value
    return ((value << shift) | (value >> (64 - shift))) & _MASK64


def _keccak_f1600(a: list[int]) -> None:
    """Permute the 25-lane state in place. Lane (x, y) lives at a[x + 5*y]."""
    for rc in _ROUND_CONSTANTS:
        # theta
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl64(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x + 5 * y] ^= d[x]

        # rho and pi
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl64(a[x + 5 * y], _ROTATIONS[x][y])

        # chi
        for x in range(5):
            for y in range(5):
                a[x + 5 * y] = b[x + 5 * y] ^ (
                    (~b[(x + 1) % 5 + 5 * y] & _MASK64) & b[(x + 2) % 5 + 5 * y]
                )

        # iota
        a[0] ^= rc


def keccak256(data: bytes) -> bytes:
    """Return the 32-byte Keccak-256 digest of `data`."""
    state = [0] * 25

    # Absorb, with Keccak's original padding: 0x01 ... 0x80.
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % _RATE != 0:
        padded.append(0x00)
    padded[-1] |= 0x80

    for offset in range(0, len(padded), _RATE):
        block = padded[offset:offset + _RATE]
        for i in range(_RATE // 8):
            state[i] ^= int.from_bytes(block[i * 8:(i + 1) * 8], "little")
        _keccak_f1600(state)

    # Squeeze; 32 bytes fit inside the first block of output.
    out = bytearray()
    for i in range(4):
        out += state[i].to_bytes(8, "little")
    return bytes(out)
