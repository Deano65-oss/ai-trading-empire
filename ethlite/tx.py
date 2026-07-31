"""Build and sign legacy (EIP-155) Ethereum transactions."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import rlp
from .keccak import keccak256
from .secp256k1 import sign


def to_bytes(address: str | None) -> bytes:
    if address is None:
        return b""
    return bytes.fromhex(address.lower().removeprefix("0x"))


@dataclass
class Transaction:
    """A legacy transaction. `to` is None for a contract deployment."""

    nonce: int
    gas_price: int
    gas: int
    to: str | None
    value: int
    data: bytes = field(default=b"")
    chain_id: int = 1

    def _payload(self) -> list:
        return [
            self.nonce,
            self.gas_price,
            self.gas,
            to_bytes(self.to),
            self.value,
            self.data,
        ]

    def signing_hash(self) -> bytes:
        """The digest signed under EIP-155, which binds the chain id."""
        return keccak256(rlp.encode(self._payload() + [self.chain_id, 0, 0]))

    def sign(self, private_key: int) -> "SignedTransaction":
        recovery_id, r, s = sign(self.signing_hash(), private_key)
        v = recovery_id + self.chain_id * 2 + 35
        raw = rlp.encode(self._payload() + [v, r, s])
        return SignedTransaction(raw=raw, v=v, r=r, s=s)


@dataclass
class SignedTransaction:
    raw: bytes
    v: int
    r: int
    s: int

    @property
    def hex(self) -> str:
        return "0x" + self.raw.hex()

    @property
    def hash(self) -> str:
        return "0x" + keccak256(self.raw).hex()


def contract_address(deployer: str, nonce: int) -> str:
    """Address a CREATE from `deployer` at `nonce` will land on."""
    digest = keccak256(rlp.encode([to_bytes(deployer), nonce]))
    return "0x" + digest[-20:].hex()
