#!/usr/bin/env python3
"""Execute the ObsidianCipher test harness on a real EVM.

There is no Hardhat/Foundry here, so this drives go-ethereum's state
transition tool (`evm t8n`) directly: each "block" is a JSON pre-state plus a
list of transactions, and t8n hands back receipts and the post-state, which is
fed into the next block. That is enough to run a multi-transaction scenario
with a moving block timestamp, which is what the expiry tests need.

Environment:
    SOLC  path to a solc binary  (default: solc)
    EVM   path to geth's evm     (default: evm)

Exit status is non-zero if any transaction reverts or any assertion in
test/ObsidianCipherHarness.sol fails.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOLC = os.environ.get("SOLC", "solc")
EVM = os.environ.get("EVM", "evm")

FORK = "Cancun"
CHAIN_ID = 1
GAS_LIMIT = 0x1C9C380  # 30M
TX_GAS = 0x1C9C380
GAS_PRICE = 0x3B9ACA00  # 1 gwei
BASE_FEE = 0x7

# Canonical test key from the execution-spec fixtures; the matching address is
# verified below by checking that t8n accepted a transaction signed with it.
SENDER_KEY = "0x45a915e4d060149eb4365960e6a7a45f334393093061116b197e3240065ff2d8"
SENDER = "0xa94f5374fce5edbc8e2a8697c15331677e6ebf0b"
COINBASE = "0x2adc25665018aa1fe0e6bc666dac8fc2697ff9ba"

ZERO32 = "0x" + "00" * 32
ETHER = 10**18


def compile_sources() -> dict:
    sources = sorted((ROOT / "contracts").glob("*.sol")) + sorted((ROOT / "test").glob("*.sol"))
    std_input = {
        "language": "Solidity",
        "sources": {
            str(p.relative_to(ROOT)): {"content": p.read_text()} for p in sources
        },
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "evmVersion": FORK.lower(),
            "outputSelection": {
                "*": {
                    "*": [
                        "abi",
                        "evm.bytecode.object",
                        "evm.deployedBytecode.object",
                        "evm.methodIdentifiers",
                        "storageLayout",
                    ]
                }
            },
        },
    }
    proc = subprocess.run(
        [SOLC, "--standard-json", "--base-path", str(ROOT)],
        input=json.dumps(std_input),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"solc failed: {proc.stderr.strip()}")

    result = json.loads(proc.stdout)
    fatal = [d for d in result.get("errors", []) if d["severity"] == "error"]
    if fatal:
        for d in fatal:
            print(d["formattedMessage"].rstrip(), file=sys.stderr)
        sys.exit("compilation failed")

    out = {}
    for units in result["contracts"].values():
        out.update(units)
    return out


def selector(artifact: dict, signature: str) -> str:
    ids = artifact["evm"]["methodIdentifiers"]
    if signature not in ids:
        sys.exit(f"no such method {signature}; have {sorted(ids)}")
    return "0x" + ids[signature]


def slot_of(artifact: dict, label: str) -> int:
    for item in artifact["storageLayout"]["storage"]:
        if item["label"] == label:
            return int(item["slot"])
    sys.exit(f"no storage variable named {label}")


def transact(alloc: dict, txs: list, number: int, timestamp: int) -> tuple[dict, dict]:
    """Apply `txs` to `alloc` in a block at `timestamp`; return (post, result)."""
    env = {
        "currentCoinbase": COINBASE,
        "currentGasLimit": hex(GAS_LIMIT),
        "currentNumber": hex(number),
        "currentTimestamp": hex(timestamp),
        "currentRandom": ZERO32,
        "currentBaseFee": hex(BASE_FEE),
        "parentBeaconBlockRoot": ZERO32,
        "currentExcessBlobGas": "0x0",
        "withdrawals": [],
    }

    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        (tmp / "alloc.json").write_text(json.dumps(alloc))
        (tmp / "env.json").write_text(json.dumps(env))
        (tmp / "txs.json").write_text(json.dumps(txs))

        proc = subprocess.run(
            [
                EVM,
                "t8n",
                f"--input.alloc={tmp / 'alloc.json'}",
                f"--input.env={tmp / 'env.json'}",
                f"--input.txs={tmp / 'txs.json'}",
                "--output.result=result.json",
                "--output.alloc=alloc.json",
                f"--output.basedir={tmp / 'out'}",
                f"--state.fork={FORK}",
                f"--state.chainid={CHAIN_ID}",
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            sys.exit(f"evm t8n failed:\n{proc.stderr.strip()}")

        post = json.loads((tmp / "out" / "alloc.json").read_text())
        result = json.loads((tmp / "out" / "result.json").read_text())
        return post, result


def check_block(result: dict, label: str, expected_txs: int) -> list:
    rejected = result.get("rejected") or []
    if rejected:
        sys.exit(f"{label}: t8n rejected transaction(s): {json.dumps(rejected)}")

    receipts = result.get("receipts") or []
    if len(receipts) != expected_txs:
        sys.exit(f"{label}: expected {expected_txs} receipt(s), got {len(receipts)}")

    for i, receipt in enumerate(receipts):
        if receipt["status"] != "0x1":
            sys.exit(
                f"{label}: transaction {i} reverted "
                f"(gasUsed {int(receipt['gasUsed'], 16)}). "
                "A revert here means a require() in the harness failed."
            )
    return receipts


def tx(nonce: int, to: str | None, data: str, value: int = 0) -> dict:
    return {
        "type": "0x0",
        "chainId": hex(CHAIN_ID),
        "nonce": hex(nonce),
        "gas": hex(TX_GAS),
        "gasPrice": hex(GAS_PRICE),
        "to": to,
        "value": hex(value),
        "input": data,
        "v": "0x0",
        "r": "0x0",
        "s": "0x0",
        "secretKey": SENDER_KEY,
    }


def main() -> None:
    artifacts = compile_sources()
    harness = artifacts["ObsidianCipherHarness"]
    cipher = artifacts["ObsidianCipher"]

    print(f"solc: {subprocess.run([SOLC, '--version'], capture_output=True, text=True).stdout.strip().splitlines()[-1]}")
    print(f"evm:  {subprocess.run([EVM, '--version'], capture_output=True, text=True).stdout.strip()}")
    print(f"fork: {FORK}\n")

    alloc = {SENDER: {"balance": hex(1000 * ETHER), "nonce": "0x0", "code": "0x", "storage": {}}}
    timestamp = 1_700_000_000

    # Block 1: deploy the harness, funded with ETH for the stakes.
    creation = "0x" + harness["evm"]["bytecode"]["object"]
    alloc, result = transact(
        alloc, [tx(0, None, creation, value=10 * ETHER)], number=1, timestamp=timestamp
    )
    check_block(result, "deploy", 1)

    deployed = [
        addr
        for addr, acct in alloc.items()
        if acct.get("code", "0x") != "0x" and addr.lower() != SENDER.lower()
    ]
    # The harness constructor deploys the cipher plus three counterparties.
    if len(deployed) != 5:
        sys.exit(f"deploy: expected 5 contracts, got {len(deployed)}")

    def find_by_code(name: str) -> str:
        # Neither of these two declares an immutable, so their runtime code is
        # byte-identical to what solc emitted.
        wanted = "0x" + artifacts[name]["evm"]["deployedBytecode"]["object"]
        matches = [a for a in deployed if alloc[a]["code"] == wanted]
        if len(matches) != 1:
            sys.exit(f"deploy: found {len(matches)} candidates for {name}")
        return matches[0]

    harness_addr = find_by_code("ObsidianCipherHarness")
    cipher_addr = find_by_code("ObsidianCipher")
    print(f"deployed harness at {harness_addr}")
    print(f"deployed cipher  at {cipher_addr}")
    print(f"  gas used: {int(result['receipts'][0]['gasUsed'], 16):,}\n")

    # Block 2: phase 1 — seal, validation, reveal, withdraw, front-running.
    alloc, result = transact(
        alloc,
        [tx(1, harness_addr, selector(harness, "phase1()"))],
        number=2,
        timestamp=timestamp + 12,
    )
    receipts = check_block(result, "phase1", 1)
    logs1 = receipts[0].get("logs") or []
    print(f"phase1: passed  ({int(receipts[0]['gasUsed'], 16):,} gas, {len(logs1)} events)")

    # Block 3: phase 2 — mined an hour later so the short window has lapsed.
    alloc, result = transact(
        alloc,
        [tx(2, harness_addr, selector(harness, "phase2()"))],
        number=3,
        timestamp=timestamp + 12 + 3600,
    )
    receipts = check_block(result, "phase2", 1)
    logs2 = receipts[0].get("logs") or []
    print(f"phase2: passed  ({int(receipts[0]['gasUsed'], 16):,} gas, {len(logs2)} events)")

    # Cross-check the harness's own counters against the post-state.
    storage = alloc[harness_addr]["storage"]

    def read(label: str) -> int:
        slot = "0x" + format(slot_of(harness, label), "064x")
        for key, value in storage.items():
            if int(key, 16) == int(slot, 16):
                return int(value, 16)
        return 0

    checks = read("checksPassed")
    phase = read("phase")
    if phase != 2:
        sys.exit(f"harness stopped at phase {phase}")
    if checks == 0:
        sys.exit("harness recorded no checks")

    print(f"\n{checks} assertions passed across 2 phases, 0 failed")

    # Final sanity check on the money: every stake has been settled, so the
    # cipher itself is empty, and none of the 10 ETH the harness started with
    # has gone missing.
    cipher_held = int(alloc[cipher_addr]["balance"], 16)
    print(f"cipher holds {cipher_held / ETHER} ETH (every stake settled)")
    if cipher_held != 0:
        sys.exit(f"expected the cipher to be empty, found {cipher_held / ETHER} ETH")

    total = sum(int(alloc[a]["balance"], 16) for a in deployed)
    print(f"contracts hold {total / ETHER} ETH in total (10.0 deployed, none lost)")
    if total != 10 * ETHER:
        sys.exit(f"ETH conservation failed: {total / ETHER} != 10.0")

    print("\nOK")


if __name__ == "__main__":
    main()
