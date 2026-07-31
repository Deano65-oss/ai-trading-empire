"""A JSON-RPC client for Ethereum nodes, built on urllib.

No third-party HTTP library, so this runs on a bare Python install.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class RpcError(RuntimeError):
    def __init__(self, method: str, payload: dict):
        self.method = method
        self.payload = payload
        message = payload.get("message", str(payload))
        super().__init__(f"{method}: {message}")


class Rpc:
    def __init__(self, url: str, timeout: int = 30):
        self.url = url
        self.timeout = timeout
        self._id = 0

    def call(self, method: str, params: list | None = None):
        self._id += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []}
        ).encode()
        request = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise RpcError(method, {"message": f"HTTP {exc.code}: {exc.read()[:200]!r}"}) from exc
        except urllib.error.URLError as exc:
            raise RpcError(method, {"message": f"cannot reach {self.url}: {exc.reason}"}) from exc

        if "error" in payload:
            raise RpcError(method, payload["error"])
        return payload["result"]

    # -- convenience wrappers -------------------------------------------------

    def chain_id(self) -> int:
        return int(self.call("eth_chainId"), 16)

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber"), 16)

    def balance(self, address: str) -> int:
        return int(self.call("eth_getBalance", [address, "latest"]), 16)

    def nonce(self, address: str) -> int:
        return int(self.call("eth_getTransactionCount", [address, "pending"]), 16)

    def gas_price(self) -> int:
        return int(self.call("eth_gasPrice"), 16)

    def estimate_gas(self, tx: dict) -> int:
        return int(self.call("eth_estimateGas", [tx]), 16)

    def send_raw(self, raw_hex: str) -> str:
        return self.call("eth_sendRawTransaction", [raw_hex])

    def receipt(self, tx_hash: str):
        return self.call("eth_getTransactionReceipt", [tx_hash])

    def logs(self, address: str, from_block: int = 0, topics: list | None = None) -> list:
        return self.call(
            "eth_getLogs",
            [
                {
                    "address": address,
                    "fromBlock": hex(from_block),
                    "toBlock": "latest",
                    **({"topics": topics} if topics else {}),
                }
            ],
        )

    def wait_for_receipt(self, tx_hash: str, timeout: int = 300, poll: float = 2.0) -> dict:
        """Block until the transaction is mined, or raise on timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self.receipt(tx_hash)
            if result:
                return result
            time.sleep(poll)
        raise TimeoutError(f"no receipt for {tx_hash} after {timeout}s")
