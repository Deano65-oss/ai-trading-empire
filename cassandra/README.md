# Cassandra

A trading record that can't be edited after the fact — built on
[ObsidianCipher](../contracts/README.md).

## The idea

A track record is only worth something if it's complete. But screenshots are
forgeable, threads get edited, and calls that aged badly quietly disappear.
"I called this" is unfalsifiable once the move has already happened.

The obvious fix — publish a hash of your prediction now, reveal it later — does
not actually work. You can seal ten calls, reveal the two that came good, and
say nothing about the other eight. Silence is free, so the record stays
curated. The hash proved the timing and nothing else.

Cassandra closes that hole with the stake. Every sealed call locks ETH that is
only released by revealing on time. Go quiet and the commitment sits there
unrevealed until anyone can sweep it and take the money. Silence stops being
free, and it stops being invisible: an abandoned call is a permanent on-chain
record that you had something to say and wouldn't say it.

Every call therefore ends in exactly one of three states, all public:

| State | Meaning |
| --- | --- |
| `REVEALED` | The full text, provably written before the market moved. |
| `FORFEITED` | The author went quiet; someone else took the stake. |
| `SEALED` | Still pending — contents hidden, deadline running. |

There is no fourth state and no way to add one. That is the entire design.

## What is actually being proved

Worth being precise, because commit-reveal is often oversold:

- **Proved.** The prediction existed, unmodified, before the reveal — the chain
  timestamps the commitment, and the contract checks the preimage. And the set
  of calls is complete: every seal is public, so an unrevealed call is visible
  as an abandonment rather than an absence.
- **Not proved.** Whether the call was *right*. No contract can know where BTC
  closed. Each prediction names the public source that settles it (`source
  kraken spot close`), so anyone can check — but that check happens off-chain,
  by a reader, against a source they choose to trust.

The ledger reports on-chain facts and does not score correctness for you. That
is deliberate: a scoreboard whose author decides what counts is the problem this
is trying to solve.

## Try it locally

Runs against a real EVM on a local devnet — no network, no faucet, no risk:

```
scripts/fetch_solc.sh
SOLC=.toolchain/solc EVM=<path-to-geth-evm> scripts/demo.sh
```

That deploys the contract, seals three calls, reveals one, abandons one, sweeps
it from a second account, and rebuilds the public record from chain logs.

## Use it on a testnet

```
export CASSANDRA_KEY=0x<throwaway testnet key>
python3 scripts/deploy.py --rpc https://<testnet-rpc>

python3 cassandra/predict.py --rpc https://<testnet-rpc> seal \
    --asset BTC-USD --claim "close above 72000" \
    --resolve 2026-08-01T00:00:00Z --stake 0.01

python3 cassandra/predict.py --rpc https://<testnet-rpc> reveal --all
python3 cassandra/predict.py --rpc https://<testnet-rpc> ledger
```

Commands: `seal`, `reveal`, `sweep`, `withdraw`, `ledger`.

`ledger` reads nothing but contract logs, so anyone can run it against your
contract and reproduce your record exactly — including the entries you would
rather they didn't see. That is the point.

## Things that will cost you money

- **The vault is not optional.** `cassandra/vault/` holds the salt for each
  sealed call. Lose it and you cannot reveal, so you forfeit the stake. It is
  gitignored, because committing it would publish your pending predictions
  early. Back it up somewhere private.
- **The deadline is real.** Miss it and anyone can take your stake. The CLI sets
  the window to the resolve time plus `--grace` (30 minutes by default); widen
  it if you might not be at a keyboard.
- **Don't reveal early** unless you mean to. `--early` publishes before the
  resolve time, which is harmless to honesty but tells the market what you think.

## Implementation

- `predict.py` — the CLI, and `build_ledger`, which reconstructs the record from
  `Sealed`/`Revealed`/`Swept` events.
- [`../ethlite/`](../ethlite/) — keccak256, secp256k1, RLP, ABI and JSON-RPC in
  pure Python, so this runs on a bare install with no pip packages.
- [`../devnet/server.py`](../devnet/server.py) — a local chain backed by
  go-ethereum's `evm t8n`, used to test the whole flow offline.

Tests: `python3 tests/test_ethlite.py`.
