#!/usr/bin/env python3
"""Deploy ObsidianCipher to any EVM chain over JSON-RPC.

    export CASSANDRA_KEY=0x<your testnet private key>
    python3 scripts/deploy.py --rpc https://ethereum-sepolia-rpc.publicnode.com

Writes the deployed address to deployments/<chain-id>.json so the Cassandra
CLI picks it up automatically.

Use a throwaway key. This signs with a pure-Python ECDSA implementation that
makes no side-channel guarantees, and the key is read from your environment.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ethlite.rpc import Rpc  # noqa: E402
from ethlite.secp256k1 import private_to_address  # noqa: E402
from ethlite.tx import Transaction, contract_address  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
ARTIFACT = ROOT / "build" / "contracts" / "ObsidianCipher.json"

EXPLORERS = {
    1: "https://etherscan.io",
    11155111: "https://sepolia.etherscan.io",
    17000: "https://holesky.etherscan.io",
    84532: "https://sepolia.basescan.org",
    421614: "https://sepolia.arbiscan.io",
    11155420: "https://sepolia-optimism.etherscan.io",
}


def load_key() -> int:
    raw = os.environ.get("CASSANDRA_KEY", "").strip()
    if not raw:
        sys.exit(
            "error: set CASSANDRA_KEY to a testnet private key\n"
            "  export CASSANDRA_KEY=0x...\n"
            "Use a throwaway account, not one holding real funds."
        )
    try:
        return int(raw, 16)
    except ValueError:
        sys.exit("error: CASSANDRA_KEY must be a hex private key")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", required=True, help="JSON-RPC endpoint")
    parser.add_argument("--gas-price", type=int, help="override gas price in wei")
    parser.add_argument("--dry-run", action="store_true", help="build and sign, do not send")
    args = parser.parse_args()

    if not ARTIFACT.exists():
        sys.exit(f"error: {ARTIFACT.relative_to(ROOT)} missing; run scripts/build_contracts.sh")

    artifact = json.loads(ARTIFACT.read_text())
    bytecode = bytes.fromhex(artifact["evm"]["bytecode"]["object"])

    key = load_key()
    sender = private_to_address(key)
    rpc = Rpc(args.rpc)

    chain_id = rpc.chain_id()
    nonce = rpc.nonce(sender)
    balance = rpc.balance(sender)
    gas_price = args.gas_price or rpc.gas_price()

    print(f"rpc      {args.rpc}")
    print(f"chain    {chain_id}")
    print(f"sender   {sender}")
    print(f"balance  {balance / 10**18:.6f} ETH")
    print(f"nonce    {nonce}")
    print(f"gas      {gas_price / 10**9:.3f} gwei")

    if balance == 0:
        sys.exit(
            "\nerror: sender has no ETH.\n"
            "Fund it from a faucet before deploying (search for a faucet for "
            f"chain {chain_id})."
        )

    try:
        gas = rpc.estimate_gas({"from": sender, "data": "0x" + bytecode.hex()})
        gas = int(gas * 1.2)
    except Exception as exc:  # nodes differ on estimating deployments
        print(f"note     gas estimation failed ({exc}); using 1,200,000")
        gas = 1_200_000

    cost = gas * gas_price
    print(f"gas cap  {gas:,} (max cost {cost / 10**18:.6f} ETH)")

    if cost > balance:
        sys.exit("\nerror: balance will not cover the deployment at this gas price")

    tx = Transaction(
        nonce=nonce, gas_price=gas_price, gas=gas, to=None, value=0,
        data=bytecode, chain_id=chain_id,
    )
    signed = tx.sign(key)
    predicted = contract_address(sender, nonce)

    print(f"\ntx hash  {signed.hash}")
    print(f"contract {predicted} (predicted)")

    if args.dry_run:
        print("\ndry run: nothing sent")
        return

    print("\nsending...")
    tx_hash = rpc.send_raw(signed.hex)
    print(f"sent     {tx_hash}")
    print("waiting for the receipt...")

    receipt = rpc.wait_for_receipt(tx_hash)
    if receipt.get("status") != "0x1":
        sys.exit(f"error: deployment reverted\n{json.dumps(receipt, indent=2)}")

    address = receipt.get("contractAddress") or predicted
    used = int(receipt.get("gasUsed", "0x0"), 16)
    print(f"\ndeployed {address}")
    print(f"gas used {used:,} ({used * gas_price / 10**18:.6f} ETH)")

    explorer = EXPLORERS.get(chain_id)
    if explorer:
        print(f"explorer {explorer}/address/{address}")

    out = ROOT / "deployments"
    out.mkdir(exist_ok=True)
    record = {
        "chainId": chain_id,
        "address": address,
        "deployer": sender,
        "transactionHash": tx_hash,
        "blockNumber": int(receipt.get("blockNumber", "0x0"), 16),
        "compiler": json.loads(artifact["metadata"])["compiler"]["version"],
    }
    path = out / f"{chain_id}.json"
    path.write_text(json.dumps(record, indent=2) + "\n")
    print(f"recorded {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
