#!/usr/bin/env python3
"""Cassandra — a trading record that cannot be edited after the fact.

The problem this solves: anyone can claim a great track record, because the
losing calls get quietly deleted. Screenshots are forgeable, threads get
edited, and "I called this" is unfalsifiable after the move has happened.

The naive fix — publishing a hash of your prediction — does not actually work.
You can seal ten calls, then only reveal the two that came good and stay silent
about the rest. Silence is free, so the record is still curated.

Cassandra closes that hole with the stake. Every sealed call locks ETH that is
only released by revealing on time. Go quiet on a loser and the commitment sits
there publicly unrevealed until anyone can sweep it and take the money. Staying
silent stops being free, and it stops being invisible: an abandoned call is a
permanent on-chain record that you had something to say and would not say it.

So every call ends in exactly one of three states, all of them public:

    REVEALED    the full text, provably written before the market moved
    FORFEITED   you went quiet, and it cost you
    SEALED      still pending, deadline ticking

There is no fourth state, and no way to add one. That is the whole idea.

Usage:
    python3 cassandra/predict.py seal  --asset BTC-USD --claim "close above 72000" \\
                                       --resolve 2026-08-01T00:00:00Z --stake 0.01
    python3 cassandra/predict.py due
    python3 cassandra/predict.py reveal --all
    python3 cassandra/predict.py ledger
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import secrets
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ethlite import abi  # noqa: E402
from ethlite.keccak import keccak256  # noqa: E402
from ethlite.rpc import Rpc  # noqa: E402
from ethlite.secp256k1 import private_to_address  # noqa: E402
from ethlite.tx import Transaction  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
VAULT = ROOT / "cassandra" / "vault"

SEALED_TOPIC = abi.topic("Sealed(address,bytes32,uint256,uint64)")
REVEALED_TOPIC = abi.topic("Revealed(address,bytes32,bytes,uint256)")
SWEPT_TOPIC = abi.topic("Swept(address,bytes32,address,uint256)")

GRACE_SECONDS = 30 * 60
MIN_WINDOW = 10 * 60
MAX_WINDOW = 30 * 24 * 60 * 60
FORMAT = "CASSANDRA/v1"


# ----------------------------------------------------------------------------
# Prediction format
# ----------------------------------------------------------------------------

def render_prediction(asset: str, claim: str, resolve: dt.datetime, source: str,
                      issued: dt.datetime) -> bytes:
    """The exact bytes that get hashed, and later published verbatim on-chain.

    Kept as plain lines so that anyone reading the reveal transaction can
    understand the call without tooling.
    """
    text = (
        f"{FORMAT}\n"
        f"asset   {asset}\n"
        f"claim   {claim}\n"
        f"source  {source}\n"
        f"resolve {resolve.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"issued  {issued.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
    )
    return text.encode()


def parse_prediction(raw: bytes) -> dict:
    fields = {"format": "", "asset": "", "claim": "", "source": "", "resolve": "", "issued": ""}
    try:
        lines = raw.decode().strip().splitlines()
    except UnicodeDecodeError:
        return {**fields, "format": "unreadable"}

    if lines:
        fields["format"] = lines[0].strip()
    for line in lines[1:]:
        key, _, value = line.strip().partition(" ")
        if key in fields:
            fields[key] = value.strip()
    return fields


def commitment_hash(chain_id: int, contract: str, committer: str,
                    secret: bytes, salt: bytes) -> bytes:
    """Mirror of ObsidianCipher.hashSecret, computed locally.

    Must match the contract byte for byte: if it does not, the reveal reverts
    with CommitmentUnknown, which the devnet test would catch immediately.
    """
    return keccak256(
        abi.encode(
            ["uint256", "address", "address", "bytes", "bytes32"],
            [chain_id, contract, committer, secret, salt],
        )
    )


# ----------------------------------------------------------------------------
# Chain plumbing
# ----------------------------------------------------------------------------

def load_key() -> int:
    raw = os.environ.get("CASSANDRA_KEY", "").strip()
    if not raw:
        sys.exit("error: set CASSANDRA_KEY to a testnet private key (use a throwaway account)")
    try:
        return int(raw, 16)
    except ValueError:
        sys.exit("error: CASSANDRA_KEY must be a hex private key")


def resolve_contract(rpc: Rpc, override: str | None) -> tuple[str, int]:
    chain_id = rpc.chain_id()
    if override:
        return override.lower(), chain_id

    path = ROOT / "deployments" / f"{chain_id}.json"
    if not path.exists():
        sys.exit(
            f"error: no deployment recorded for chain {chain_id}\n"
            f"Deploy first (scripts/deploy.py) or pass --address."
        )
    return json.loads(path.read_text())["address"].lower(), chain_id


def send(rpc: Rpc, key: int, to: str | None, data: bytes, value: int = 0,
         gas: int = 400_000) -> dict:
    sender = private_to_address(key)
    tx = Transaction(
        nonce=rpc.nonce(sender),
        gas_price=rpc.gas_price(),
        gas=gas,
        to=to,
        value=value,
        data=data,
        chain_id=rpc.chain_id(),
    )
    signed = tx.sign(key)
    tx_hash = rpc.send_raw(signed.hex)
    receipt = rpc.wait_for_receipt(tx_hash)
    if receipt.get("status") != "0x1":
        raise RuntimeError(f"transaction reverted: {tx_hash}")
    return receipt


# ----------------------------------------------------------------------------
# Local vault (salts live here; without them a call cannot be revealed)
# ----------------------------------------------------------------------------

def vault_path(commitment: bytes) -> pathlib.Path:
    VAULT.mkdir(parents=True, exist_ok=True)
    return VAULT / f"{commitment.hex()}.json"


def save_locally(commitment: bytes, record: dict) -> pathlib.Path:
    path = vault_path(commitment)
    path.write_text(json.dumps(record, indent=2) + "\n")
    return path


def load_vault() -> list[dict]:
    if not VAULT.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(VAULT.glob("*.json"))]


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------

def cmd_seal(args) -> None:
    rpc = Rpc(args.rpc)
    contract, chain_id = resolve_contract(rpc, args.address)
    key = load_key()
    sender = private_to_address(key)

    resolve_at = dt.datetime.strptime(args.resolve, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc
    )
    now = dt.datetime.now(dt.timezone.utc)
    if resolve_at <= now:
        sys.exit("error: --resolve must be in the future; a prediction about the past is not one")

    # The window has to outlast the event itself, plus time to publish.
    window = int((resolve_at - now).total_seconds()) + args.grace
    if window < MIN_WINDOW:
        window = MIN_WINDOW
    if window > MAX_WINDOW:
        sys.exit(
            f"error: reveal window would be {window // 86400} days; "
            f"the contract caps it at {MAX_WINDOW // 86400}"
        )

    secret = render_prediction(args.asset, args.claim, resolve_at, args.source, now)
    salt = secrets.token_bytes(32)
    commitment = commitment_hash(chain_id, contract, sender, secret, salt)
    stake_wei = int(args.stake * 10**18)

    print("sealing this call:\n")
    print("    " + secret.decode().replace("\n", "\n    ").rstrip())
    print(f"\ncommitment {commitment.hex()}")
    print(f"stake      {args.stake} ETH")
    print(f"reveal by  {(now + dt.timedelta(seconds=window)).strftime('%Y-%m-%dT%H:%M:%SZ')}")

    # Saved before sending: losing the salt means losing the stake.
    record = {
        "commitment": "0x" + commitment.hex(),
        "salt": "0x" + salt.hex(),
        "secret": secret.decode(),
        "chainId": chain_id,
        "contract": contract,
        "committer": sender,
        "stakeWei": str(stake_wei),
        "resolveAt": resolve_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sealedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = save_locally(commitment, record)
    print(f"saved      {path.relative_to(ROOT)}")

    if args.dry_run:
        print("\ndry run: nothing sent")
        return

    data = abi.encode_call("seal(bytes32,uint64)", [commitment, window])
    receipt = send(rpc, key, contract, data, value=stake_wei)
    print(f"\nsealed on-chain: {receipt['transactionHash']}")
    print(f"gas used {int(receipt['gasUsed'], 16):,}")


def cmd_reveal(args) -> None:
    rpc = Rpc(args.rpc)
    contract, chain_id = resolve_contract(rpc, args.address)
    key = load_key()

    now = dt.datetime.now(dt.timezone.utc)
    candidates = []
    for record in load_vault():
        if record["contract"].lower() != contract or record["chainId"] != chain_id:
            continue
        if args.commitment and record["commitment"].lower() != args.commitment.lower():
            continue
        resolve_at = dt.datetime.strptime(record["resolveAt"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
        if not args.early and resolve_at > now:
            continue
        candidates.append(record)

    if not candidates:
        print("nothing to reveal (use --early to reveal before the resolve time)")
        return

    for record in candidates:
        secret = record["secret"].encode()
        salt = bytes.fromhex(record["salt"].removeprefix("0x"))
        print(f"revealing {record['commitment']}")
        data = abi.encode_call("reveal(bytes,bytes32)", [secret, salt])
        try:
            receipt = send(rpc, key, contract, data)
        except RuntimeError as exc:
            print(f"  failed: {exc}")
            print("  (past the deadline, or already revealed)")
            continue
        print(f"  published: {receipt['transactionHash']}")


def cmd_sweep(args) -> None:
    """Take the stake from a call whose author went quiet."""
    rpc = Rpc(args.rpc)
    contract, _ = resolve_contract(rpc, args.address)
    key = load_key()

    data = abi.encode_call(
        "sweep(address,bytes32)",
        [args.committer, bytes.fromhex(args.commitment.removeprefix("0x"))],
    )
    receipt = send(rpc, key, contract, data)
    print(f"swept: {receipt['transactionHash']}")
    print("the forfeited stake is now credited to you; run `withdraw` to collect")


def cmd_withdraw(args) -> None:
    rpc = Rpc(args.rpc)
    contract, _ = resolve_contract(rpc, args.address)
    key = load_key()
    receipt = send(rpc, key, contract, abi.encode_call("withdraw()"))
    print(f"withdrawn: {receipt['transactionHash']}")


def build_ledger(rpc: Rpc, contract: str, from_block: int = 0) -> list[dict]:
    """Rebuild the whole record from chain events alone.

    Nothing here reads the local vault, so anyone can run this against the same
    contract and get the same answer. That is the point: the record does not
    depend on trusting whoever published it.
    """
    calls: dict[str, dict] = {}

    for log in rpc.logs(contract, from_block):
        topics = log["topics"]
        data = bytes.fromhex(log["data"].removeprefix("0x"))

        # The contract also emits Withdrawn, which carries neither a committer
        # nor a commitment; skip anything that is not part of the record.
        if not topics or topics[0].lower() not in (
            SEALED_TOPIC, REVEALED_TOPIC, SWEPT_TOPIC
        ):
            continue

        committer = "0x" + topics[1][-40:]
        commitment = topics[2]

        if topics[0].lower() == SEALED_TOPIC:
            stake, deadline = abi.decode(["uint256", "uint64"], data)
            calls[commitment] = {
                "commitment": commitment,
                "committer": committer,
                "stakeWei": stake,
                "deadline": deadline,
                "status": "SEALED",
                "prediction": None,
                "sealedInBlock": int(log["blockNumber"], 16),
            }
        elif topics[0].lower() == REVEALED_TOPIC:
            secret, _stake = abi.decode(["bytes", "uint256"], data)
            entry = calls.setdefault(commitment, {"commitment": commitment,
                                                  "committer": committer})
            entry["status"] = "REVEALED"
            entry["prediction"] = parse_prediction(secret)
            entry["raw"] = secret.decode(errors="replace")
            entry["revealedInBlock"] = int(log["blockNumber"], 16)
        elif topics[0].lower() == SWEPT_TOPIC:
            entry = calls.setdefault(commitment, {"commitment": commitment,
                                                  "committer": committer})
            entry["status"] = "FORFEITED"
            entry["keeper"] = "0x" + topics[3][-40:]
            entry["bountyWei"] = abi.decode(["uint256"], data)[0]
            entry["sweptInBlock"] = int(log["blockNumber"], 16)

    return sorted(calls.values(), key=lambda c: c.get("sealedInBlock", 0))


def cmd_ledger(args) -> None:
    rpc = Rpc(args.rpc)
    contract, chain_id = resolve_contract(rpc, args.address)
    calls = build_ledger(rpc, contract, args.from_block)

    if args.json:
        print(json.dumps({"chainId": chain_id, "contract": contract, "calls": calls}, indent=2))
        return

    if not calls:
        print("no calls on this contract yet")
        return

    revealed = [c for c in calls if c["status"] == "REVEALED"]
    forfeited = [c for c in calls if c["status"] == "FORFEITED"]
    pending = [c for c in calls if c["status"] == "SEALED"]

    print(f"contract {contract}  (chain {chain_id})")
    print(f"{len(calls)} calls: {len(revealed)} revealed, "
          f"{len(forfeited)} forfeited, {len(pending)} pending\n")

    for call in calls:
        status = call["status"]
        marker = {"REVEALED": "[revealed]", "FORFEITED": "[forfeited]", "SEALED": "[sealed]"}[status]
        stake = call.get("stakeWei", 0) / 10**18
        print(f"{marker:<12} {call['commitment'][:18]}...  {stake:g} ETH  by {call['committer'][:10]}...")
        if status == "REVEALED" and call.get("prediction"):
            p = call["prediction"]
            print(f"             {p['asset']}: {p['claim']}")
            print(f"             resolves {p['resolve']}  per {p['source']}")
        elif status == "FORFEITED":
            print(f"             went quiet; stake taken by {call.get('keeper', '?')[:10]}...")
        elif status == "SEALED":
            deadline = dt.datetime.fromtimestamp(call["deadline"], dt.timezone.utc)
            print(f"             contents hidden until revealed; deadline {deadline:%Y-%m-%dT%H:%M:%SZ}")
        print()

    if forfeited:
        print(f"note: {len(forfeited)} call(s) were abandoned rather than revealed. "
              "Those count against the record.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rpc", default=os.environ.get("CASSANDRA_RPC", "http://127.0.0.1:8545"))
    parser.add_argument("--address", help="ObsidianCipher address (default: deployments/)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("seal", help="seal a new call")
    p.add_argument("--asset", required=True, help="e.g. BTC-USD")
    p.add_argument("--claim", required=True, help='e.g. "close above 72000"')
    p.add_argument("--resolve", required=True, help="UTC resolve time, 2026-08-01T00:00:00Z")
    p.add_argument("--source", default="kraken spot close",
                   help="the public source that settles this claim")
    p.add_argument("--stake", type=float, default=0.01, help="ETH to stake")
    p.add_argument("--grace", type=int, default=GRACE_SECONDS,
                   help="seconds after resolve time to publish in")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_seal)

    p = sub.add_parser("reveal", help="publish calls whose resolve time has passed")
    p.add_argument("--commitment", help="reveal one specific call")
    p.add_argument("--all", action="store_true", help="reveal every due call")
    p.add_argument("--early", action="store_true", help="reveal before the resolve time")
    p.set_defaults(func=cmd_reveal)

    p = sub.add_parser("sweep", help="claim the stake from an abandoned call")
    p.add_argument("--committer", required=True)
    p.add_argument("--commitment", required=True)
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("withdraw", help="collect credited ETH")
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser("ledger", help="rebuild the public record from chain events")
    p.add_argument("--json", action="store_true")
    p.add_argument("--from-block", type=int, default=0)
    p.set_defaults(func=cmd_ledger)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
