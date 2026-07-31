#!/usr/bin/env python3
"""A single-file Ethereum devnet backed by go-ethereum's state transition tool.

Speaks enough JSON-RPC for this project's tooling, and executes transactions on
a real EVM rather than simulating them: every `eth_sendRawTransaction` is handed
to `evm t8n`, which recovers the sender from the signature, applies the state
transition and returns real receipts and logs.

One transaction per block, mined immediately. Time can be moved forward with
the `devnet_increaseTime` method, which is what makes reveal-window expiry
testable in seconds instead of hours.

    EVM=/path/to/evm python3 devnet/server.py --port 8545 --fund 0xaddr

This exists so the Cassandra flow can be exercised end to end offline. It is a
test fixture, not a node: no mempool, no reorgs, no peers.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ethlite import rlp  # noqa: E402
from ethlite.keccak import keccak256  # noqa: E402

EVM = os.environ.get("EVM", "evm")
FORK = "Cancun"
CHAIN_ID = 1337
GAS_LIMIT = 0x1C9C380
BASE_FEE = 7
GAS_PRICE = 10**9
ZERO32 = "0x" + "00" * 32
COINBASE = "0x" + "11" * 20


class Chain:
    def __init__(self, funded: list[str], balance_eth: int = 1000):
        self.alloc = {
            address.lower(): {
                "balance": hex(balance_eth * 10**18),
                "nonce": "0x0",
                "code": "0x",
                "storage": {},
            }
            for address in funded
        }
        self.number = 1
        # Start at wall-clock time so reveal deadlines line up with the clock
        # the CLI reads.
        self.timestamp = int(time.time())
        self.receipts: dict[str, dict] = {}
        self.logs: list[dict] = []

    # -- state helpers ----------------------------------------------------

    def account(self, address: str) -> dict:
        return self.alloc.get(address.lower(), {})

    def balance(self, address: str) -> int:
        return int(self.account(address).get("balance", "0x0"), 16)

    def nonce(self, address: str) -> int:
        return int(self.account(address).get("nonce", "0x0"), 16)

    # -- block production -------------------------------------------------

    def _env(self) -> dict:
        return {
            "currentCoinbase": COINBASE,
            "currentGasLimit": hex(GAS_LIMIT),
            "currentNumber": hex(self.number),
            "currentTimestamp": hex(self.timestamp),
            "currentRandom": ZERO32,
            "currentBaseFee": hex(BASE_FEE),
            "parentBeaconBlockRoot": ZERO32,
            "currentExcessBlobGas": "0x0",
            "withdrawals": [],
        }

    def apply(self, raw: bytes) -> str:
        """Execute one signed transaction in its own block; return its hash."""
        tx_hash = "0x" + keccak256(raw).hex()

        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            (tmp / "alloc.json").write_text(json.dumps(self.alloc))
            (tmp / "env.json").write_text(json.dumps(self._env()))
            # t8n reads a JSON string of the RLP-encoded transaction list.
            (tmp / "txs.rlp").write_text(json.dumps("0x" + rlp.encode_raw_list([raw]).hex()))

            proc = subprocess.run(
                [
                    EVM, "t8n",
                    f"--input.alloc={tmp / 'alloc.json'}",
                    f"--input.env={tmp / 'env.json'}",
                    f"--input.txs={tmp / 'txs.rlp'}",
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
                raise RuntimeError(f"evm t8n failed: {proc.stderr.strip()[:400]}")

            result = json.loads((tmp / "out" / "result.json").read_text())
            post = json.loads((tmp / "out" / "alloc.json").read_text())

        rejected = result.get("rejected") or []
        if rejected:
            raise RuntimeError(f"transaction rejected: {rejected[0].get('error')}")

        receipt = (result.get("receipts") or [None])[0]
        if receipt is None:
            raise RuntimeError("no receipt produced")

        self.alloc = post
        receipt = dict(receipt)
        receipt["blockNumber"] = hex(self.number)
        receipt["transactionHash"] = tx_hash

        stored_logs = []
        for index, log in enumerate(receipt.get("logs") or []):
            entry = dict(log)
            entry.update(
                {
                    "blockNumber": hex(self.number),
                    "transactionHash": tx_hash,
                    "logIndex": hex(index),
                    "removed": False,
                }
            )
            stored_logs.append(entry)
        receipt["logs"] = stored_logs
        self.logs.extend(stored_logs)

        self.receipts[tx_hash] = receipt
        self.number += 1
        self.timestamp += 12
        return tx_hash


class Handler(BaseHTTPRequestHandler):
    chain: Chain = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # keep the console readable
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        try:
            result = self.dispatch(request["method"], request.get("params") or [])
            body = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
        except Exception as exc:  # surfaced to the client as a JSON-RPC error
            body = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32000, "message": str(exc)},
            }

        encoded = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def dispatch(self, method: str, params: list):
        chain = self.chain

        if method == "eth_chainId":
            return hex(CHAIN_ID)
        if method == "net_version":
            return str(CHAIN_ID)
        if method == "eth_blockNumber":
            return hex(chain.number - 1)
        if method == "eth_gasPrice":
            return hex(GAS_PRICE)
        if method == "eth_getBalance":
            return hex(chain.balance(params[0]))
        if method == "eth_getTransactionCount":
            return hex(chain.nonce(params[0]))
        if method == "eth_estimateGas":
            # No trial execution: callers treat this as a cap, and unused gas
            # is not charged.
            return hex(6_000_000)
        if method == "eth_sendRawTransaction":
            return chain.apply(bytes.fromhex(params[0].removeprefix("0x")))
        if method == "eth_getTransactionReceipt":
            return chain.receipts.get(params[0])
        if method == "eth_getLogs":
            return self.filter_logs(params[0] if params else {})
        if method == "eth_getCode":
            return chain.account(params[0]).get("code", "0x")
        if method == "eth_getStorageAt":
            slot = params[1]
            storage = chain.account(params[0]).get("storage", {})
            for key, value in storage.items():
                if int(key, 16) == int(slot, 16):
                    return value
            return ZERO32

        # Devnet-only helpers.
        if method == "devnet_increaseTime":
            chain.timestamp += int(params[0], 16) if isinstance(params[0], str) else int(params[0])
            return hex(chain.timestamp)
        if method == "devnet_timestamp":
            return hex(chain.timestamp)

        raise ValueError(f"method not supported by devnet: {method}")

    def filter_logs(self, criteria: dict) -> list:
        address = (criteria.get("address") or "").lower()
        topics = criteria.get("topics") or []
        from_block = criteria.get("fromBlock", "0x0")
        from_block = 0 if from_block in ("earliest", None) else int(from_block, 16)

        out = []
        for log in self.chain.logs:
            if address and log["address"].lower() != address:
                continue
            if int(log["blockNumber"], 16) < from_block:
                continue
            if topics:
                log_topics = log.get("topics") or []
                if any(
                    wanted is not None
                    and (index >= len(log_topics) or log_topics[index].lower() != wanted.lower())
                    for index, wanted in enumerate(topics)
                ):
                    continue
            out.append(log)
        return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8545)
    parser.add_argument("--fund", action="append", default=[], help="address to prefund")
    args = parser.parse_args()

    Handler.chain = Chain(funded=args.fund)
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"devnet listening on http://127.0.0.1:{args.port} (chain id {CHAIN_ID})")
    for address in args.fund:
        print(f"  funded {address} with 1000 ETH")
    sys.stdout.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
