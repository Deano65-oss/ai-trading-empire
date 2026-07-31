"""Minimal secp256k1 ECDSA, enough to sign Ethereum transactions.

Pure Python and stdlib only. This is not constant-time and makes no attempt to
resist side-channel attacks: it is intended for signing testnet transactions
from a key you are willing to keep in a file. Do not point it at a key holding
funds you care about.
"""

from __future__ import annotations

import hashlib
import hmac

from .keccak import keccak256

# Curve parameters.
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)

Point = tuple[int, int] | None


class InvalidKey(ValueError):
    pass


def _inv(value: int, modulus: int) -> int:
    return pow(value, modulus - 2, modulus)


def _add(p1: Point, p2: Point) -> Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1

    x1, y1 = p1
    x2, y2 = p2

    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        lam = (3 * x1 * x1) % P * _inv(2 * y1 % P, P) % P
    else:
        lam = (y2 - y1) % P * _inv((x2 - x1) % P, P) % P

    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return (x3, y3)


def _mul(point: Point, scalar: int) -> Point:
    result: Point = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


def private_to_public(private_key: int) -> tuple[int, int]:
    if not 1 <= private_key < N:
        raise InvalidKey("private key out of range")
    public = _mul(G, private_key)
    assert public is not None
    return public


def private_to_address(private_key: int) -> str:
    x, y = private_to_public(private_key)
    digest = keccak256(x.to_bytes(32, "big") + y.to_bytes(32, "big"))
    return to_checksum_address("0x" + digest[-20:].hex())


def to_checksum_address(address: str) -> str:
    """EIP-55 mixed-case checksum encoding."""
    body = address.lower().removeprefix("0x")
    digest = keccak256(body.encode()).hex()
    return "0x" + "".join(
        char.upper() if char.isalpha() and int(digest[i], 16) >= 8 else char
        for i, char in enumerate(body)
    )


def _bits2octets(data: bytes) -> bytes:
    return (int.from_bytes(data, "big") % N).to_bytes(32, "big")


def _deterministic_k(private_key: int, message_hash: bytes) -> int:
    """RFC 6979 nonce derivation, so signing never needs an RNG."""
    x = private_key.to_bytes(32, "big")
    h1 = _bits2octets(message_hash)

    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()

    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, "big")
        if 1 <= candidate < N:
            return candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign(message_hash: bytes, private_key: int) -> tuple[int, int, int]:
    """Sign a 32-byte hash; returns (recovery_id, r, s) with s normalised low."""
    if len(message_hash) != 32:
        raise ValueError("message hash must be 32 bytes")
    if not 1 <= private_key < N:
        raise InvalidKey("private key out of range")

    z = int.from_bytes(message_hash, "big")

    while True:
        k = _deterministic_k(private_key, message_hash)
        point = _mul(G, k)
        assert point is not None
        x, y = point

        r = x % N
        if r == 0:
            continue

        s = _inv(k, N) * (z + r * private_key) % N
        if s == 0:
            continue

        recovery_id = (y & 1) | (2 if x >= N else 0)

        # EIP-2 requires the low-s form; flipping s flips the parity too.
        if s > N // 2:
            s = N - s
            recovery_id ^= 1

        return recovery_id, r, s
