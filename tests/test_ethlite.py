#!/usr/bin/env python3
"""Tests for the ethlite primitives.

Plain asserts, no test framework, so this runs on a bare Python install:

    python3 tests/test_ethlite.py

The signing path is additionally covered end to end by devnet/server.py, where
go-ethereum recovers the sender from signatures produced here.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ethlite import abi, rlp  # noqa: E402
from ethlite.keccak import keccak256  # noqa: E402
from ethlite.secp256k1 import N, private_to_address, sign, to_checksum_address  # noqa: E402
from ethlite.tx import Transaction, contract_address  # noqa: E402

passed = 0


def check(condition: bool, what: str) -> None:
    global passed
    if not condition:
        raise AssertionError(what)
    passed += 1


def test_keccak() -> None:
    check(
        keccak256(b"").hex()
        == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
        "keccak256 of the empty string",
    )
    check(
        keccak256(b"abc").hex()
        == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45",
        "keccak256('abc')",
    )
    check(
        keccak256(b"The quick brown fox jumps over the lazy dog").hex()
        == "4d741b6f1eb29cb2a9b9911c82f56fa8d73b04959d3d9d222895df6c0b28aa15",
        "keccak256 of the pangram",
    )
    # Longer than the 136-byte rate, so this exercises multi-block absorption.
    check(len(keccak256(b"a" * 500)) == 32, "keccak256 spanning several blocks")
    # Distinct from SHA3-256, which differs only in the padding byte.
    import hashlib

    check(
        keccak256(b"") != hashlib.sha3_256(b"").digest(),
        "keccak256 is not SHA3-256",
    )


def test_rlp() -> None:
    check(rlp.encode(b"dog").hex() == "83646f67", "rlp of a short string")
    check(rlp.encode([b"cat", b"dog"]).hex() == "c88363617483646f67", "rlp of a list")
    check(rlp.encode(b"").hex() == "80", "rlp of the empty string")
    check(rlp.encode(0).hex() == "80", "rlp of zero")
    check(rlp.encode(15).hex() == "0f", "rlp of a single byte int")
    check(rlp.encode(1024).hex() == "820400", "rlp of a multi-byte int")
    check(rlp.encode([]).hex() == "c0", "rlp of the empty list")
    check(
        rlp.encode(b"a" * 56).hex() == "b838" + "61" * 56,
        "rlp of a string needing a long header",
    )
    # Pre-encoded items must not be encoded a second time.
    check(
        rlp.encode_raw_list([rlp.encode(b"cat"), rlp.encode(b"dog")])
        == rlp.encode([b"cat", b"dog"]),
        "encode_raw_list matches encode for the same items",
    )


def test_addresses() -> None:
    key = 0x45A915E4D060149EB4365960E6A7A45F334393093061116B197E3240065FF2D8
    check(
        private_to_address(key).lower() == "0xa94f5374fce5edbc8e2a8697c15331677e6ebf0b",
        "address derived from a known private key",
    )
    # EIP-55 vectors.
    for address in (
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
        "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
        "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
    ):
        check(to_checksum_address(address.lower()) == address, f"EIP-55 for {address}")

    # CREATE address derivation.
    check(
        contract_address("0x6ac7ea33f8831ea9dcc53393aaa88b25a785dbf0", 0)
        == "0xcd234a471b72ba2f1ccf0a70fcaba648a5eecd8d",
        "contract address at nonce 0",
    )


def test_signing() -> None:
    key = 0x45A915E4D060149EB4365960E6A7A45F334393093061116B197E3240065FF2D8
    digest = keccak256(b"cassandra")

    recovery_id, r, s = sign(digest, key)
    check(0 <= recovery_id <= 3, "recovery id in range")
    check(0 < r < N and 0 < s < N, "r and s in range")
    check(s <= N // 2, "s is normalised low, as EIP-2 requires")

    # RFC 6979: the same input must always produce the same signature.
    check(sign(digest, key) == (recovery_id, r, s), "signing is deterministic")
    check(sign(keccak256(b"other"), key) != (recovery_id, r, s), "different message, different signature")

    # EIP-155 binds the chain id, so the same transaction signs differently per chain.
    fields = dict(nonce=0, gas_price=10**9, gas=21000, to="0x" + "11" * 20, value=1)
    mainnet = Transaction(**fields, chain_id=1).sign(key)
    sepolia = Transaction(**fields, chain_id=11155111).sign(key)
    check(mainnet.raw != sepolia.raw, "chain id changes the signature")
    check(mainnet.v in (37, 38), "mainnet v encodes chain id 1")
    check(sepolia.v in (11155111 * 2 + 35, 11155111 * 2 + 36), "sepolia v encodes its chain id")


def test_abi() -> None:
    # Static types pack into single words.
    encoded = abi.encode(["uint256", "address"], [1, "0x" + "11" * 20])
    check(len(encoded) == 64, "two static words")
    check(encoded[:32] == (1).to_bytes(32, "big"), "uint256 encoding")
    check(encoded[44:64] == b"\x11" * 20, "address is right-aligned")

    # Dynamic bytes are placed after the head, referenced by offset. This is the
    # layout ObsidianCipher.hashSecret depends on.
    encoded = abi.encode(
        ["uint256", "address", "address", "bytes", "bytes32"],
        [1337, "0x" + "22" * 20, "0x" + "33" * 20, b"hello", b"\x44" * 32],
    )
    offset = int.from_bytes(encoded[96:128], "big")
    check(offset == 160, "dynamic offset points past the five head words")
    check(int.from_bytes(encoded[160:192], "big") == 5, "length prefix for the bytes")
    check(encoded[192:197] == b"hello", "payload follows the length")
    check(len(encoded) == 224, "payload is padded to a whole word")

    # Selectors and topics.
    check(abi.selector("withdraw()").hex() == keccak256(b"withdraw()")[:4].hex(), "selector")
    check(
        abi.encode_call("seal(bytes32,uint64)", [b"\x01" * 32, 600])[:4]
        == abi.selector("seal(bytes32,uint64)"),
        "calldata starts with the selector",
    )
    check(
        abi.topic("Withdrawn(address,uint256)")
        == "0x" + keccak256(b"Withdrawn(address,uint256)").hex(),
        "event topic",
    )

    # Round trip through the decoder, including a dynamic field.
    values = abi.decode(["bytes", "uint256"], abi.encode(["bytes", "uint256"], [b"abc", 7]))
    check(values == [b"abc", 7], "decode round trip")


def main() -> None:
    for test in (test_keccak, test_rlp, test_addresses, test_signing, test_abi):
        test()
        print(f"  {test.__name__} ok")
    print(f"\n{passed} assertions passed")


if __name__ == "__main__":
    main()
