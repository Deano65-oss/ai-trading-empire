# ObsidianCipher

A permissionless commit–reveal registry with a refundable stake, in a single
Solidity file with no external dependencies.

You publish the hash of a secret and optionally lock ETH behind it. Later you
reveal the preimage; the contract verifies it and credits your stake back. If
the reveal window lapses, anyone can sweep the commitment and take the stake as
a keeper bounty, so abandoned commitments clean themselves up.

There is no owner, no admin key and no upgrade path. Every function is callable
by anyone, and the only ETH the contract holds is either backing a live
commitment or already earmarked for a specific address.

## Interface

| Function | Description |
| --- | --- |
| `seal(bytes32 commitment, uint64 revealWindow) payable` | File a commitment under `msg.sender`, optionally staking ETH. Window must be 10 minutes – 30 days. |
| `reveal(bytes secret, bytes32 salt)` | Prove the preimage before the deadline; credits the stake back. |
| `sweep(address committer, bytes32 commitment)` | After the deadline, settle the commitment and take its stake as a bounty. |
| `withdraw()` | Pull the caller's credited ETH. |
| `hashSecret(address committer, bytes secret, bytes32 salt) view` | Build the value to pass to `seal`. |
| `commitmentOf` / `isSweepable` / `balanceOf` / `totalStaked` / `unlockedBalance` | Views. |

Failure modes are custom errors (`CommitmentExists`, `RevealWindowClosed`,
`NothingToWithdraw`, …) rather than strings.

## Design notes

**The commitment hash binds more than the secret.** `hashSecret` mixes in
`block.chainid`, `address(this)` and the committer's address. A preimage seen
in the mempool therefore cannot be replayed by an observer, against another
deployment, or on another chain.

**Commitments are stored per committer.** The contract cannot check that a
submitted hash really is a hash of the caller's secret — that is the whole
point of a commitment — so an observer could otherwise copy a pending
commitment hash and seal it first, permanently locking the rightful committer
out of their own slot. Filing entries under `keccak256(committer, commitment)`
removes that: a copied hash only occupies the copier's own slot, where it is
unrevealable and eventually sweepable by anyone.

**Payouts are pull, not push.** `reveal` and `sweep` credit `balanceOf` and
never transfer, so a committer with a reverting `receive` hook cannot brick
their own reveal or block a keeper. `withdraw` zeroes the balance before the
external call, so a re-entrant withdrawal finds nothing left to take.

**The contract never reads its own balance to make a decision.** ETH can be
forced in (a `selfdestruct` beneficiary, a coinbase payout) without disturbing
any accounting; it just shows up in `unlockedBalance`.

**A `Commitment` is one storage slot** — `uint96` stake, `uint64` deadline,
`Status` enum — so sealing writes one slot rather than two.

## Build

```
scripts/fetch_solc.sh          # pinned solc 0.8.36, sha256-verified
SOLC=.toolchain/solc scripts/build_contracts.sh
```

Artifacts land in `build/contracts/`. The build uses solc's Standard JSON
interface, so the compiler version, optimizer settings and source hashes are
all captured in the emitted metadata. It fails if the deployed code exceeds the
EIP-170 limit.

Current output — solc 0.8.36, optimizer on (200 runs), EVM version Cancun:

```
ObsidianCipher   runtime 3,773 bytes   creation 3,801 bytes
deployment       755,384 gas
```

## Test

```
SOLC=.toolchain/solc EVM=<path-to-geth-evm> scripts/run_tests.py
```

`test/ObsidianCipherHarness.sol` is an executable test suite: a contract whose
assertions are `require`s, exercised by three counterparty contracts — a keeper
that sweeps expired commitments, a griefer that tries to copy commitments and
steal preimages, and a hostile receiver that rejects ETH and re-enters
`withdraw`.

`scripts/run_tests.py` drives it on a real EVM using go-ethereum's state
transition tool (`evm t8n`): each block is a JSON pre-state plus transactions,
and the post-state feeds the next block, which is what lets the suite mine
phase 2 an hour later and test expiry. Coverage includes the full seal → reveal
→ withdraw path, every input validation branch, reveal-after-deadline,
double-reveal, double-sweep, sweeping a zero-stake commitment, bounty
attribution, seal front-running, preimage theft, and both hostile withdrawal
paths.

```
74 assertions passed across 2 phases, 0 failed
cipher holds 0.0 ETH (every stake settled)
contracts hold 10.0 ETH in total (10.0 deployed, none lost)
```

The suite was mutation-checked: dropping the reveal deadline check, crediting a
sweep bounty to the committer instead of the keeper, and moving the balance
zeroing after the transfer (the classic reentrancy hole) each make it fail.

### Toolchain notes

`scripts/fetch_solc.sh` pulls solc out of the official `ethereum/solc-bin` git
repository rather than `binaries.soliditylang.org`, because package registries
and that host are unreachable from some sandboxes; the binary is checked
against the sha256 in the repository's own published manifest.

The test runner needs go-ethereum's `evm` binary, which is normally one
`go install github.com/ethereum/go-ethereum/cmd/evm@v1.14.11` away. It is not
vendored here.
