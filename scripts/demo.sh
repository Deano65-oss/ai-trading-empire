#!/usr/bin/env bash
# End-to-end Cassandra demo on a local devnet: deploy, seal, reveal, forfeit.
#
# Everything runs on a real EVM (go-ethereum's state transition tool) against
# transactions signed by ethlite, so this exercises the same code path a testnet
# deployment takes — only the network is local.
#
# Requirements:
#   SOLC  a solc binary          (scripts/fetch_solc.sh)
#   EVM   go-ethereum's evm      (go install github.com/ethereum/go-ethereum/cmd/evm@v1.14.11)
#
# Usage: SOLC=.toolchain/solc EVM=~/go/bin/evm scripts/demo.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SOLC="${SOLC:-solc}"
EVM="${EVM:-evm}"
PORT="${PORT:-8546}"
RPC="http://127.0.0.1:$PORT"

# Throwaway keys, published in every Ethereum test fixture. Never fund these.
AUTHOR_KEY=0x45a915e4d060149eb4365960e6a7a45f334393093061116b197e3240065ff2d8
AUTHOR=0xa94f5374Fce5edBC8E2a8697C15331677e6EbF0B
KEEPER_KEY=0x9c0257114eb9399a2985f8e75dad7600c5d89fe3824ffa99ec1c3eb8bf3b0501
KEEPER=0x328809Bc894f92807417D2dAD6b7C998c1aFdac6

command -v "$EVM" >/dev/null 2>&1 || [ -x "$EVM" ] || {
  echo "error: evm not found (set EVM=/path/to/evm)" >&2; exit 1; }

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "Building the contract"
SOLC="$SOLC" ./scripts/build_contracts.sh

step "Starting the devnet on port $PORT"
rm -rf "$ROOT/cassandra/vault" "$ROOT/deployments/1337.json"
EVM="$EVM" python3 devnet/server.py --port "$PORT" --fund "$AUTHOR" --fund "$KEEPER" &
DEVNET_PID=$!
trap 'kill $DEVNET_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  curl -s -o /dev/null -X POST -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"eth_chainId","params":[],"id":1}' "$RPC" && break
  sleep 0.25
done

step "Deploying ObsidianCipher"
CASSANDRA_KEY=$AUTHOR_KEY python3 scripts/deploy.py --rpc "$RPC"

RESOLVE_SOON=$(python3 -c "import datetime as dt; print((dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=12)).strftime('%Y-%m-%dT%H:%M:%SZ'))")
RESOLVE_LATE=$(python3 -c "import datetime as dt; print((dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=6)).strftime('%Y-%m-%dT%H:%M:%SZ'))")

step "Sealing a call the author will stand behind"
CASSANDRA_KEY=$AUTHOR_KEY python3 cassandra/predict.py --rpc "$RPC" seal \
  --asset BTC-USD --claim "close above 72000" --resolve "$RESOLVE_SOON" --stake 0.05

step "Revealing it"
CASSANDRA_KEY=$AUTHOR_KEY python3 cassandra/predict.py --rpc "$RPC" reveal --all --early

step "Sealing a call the author will go quiet on"
ABANDONED=$(CASSANDRA_KEY=$AUTHOR_KEY python3 cassandra/predict.py --rpc "$RPC" seal \
  --asset ETH-USD --claim "close above 4000" --resolve "$RESOLVE_SOON" --grace 0 \
  --stake 0.2 | awk '/^commitment/ {print $2}')
echo "abandoned commitment: 0x$ABANDONED"

step "Sealing a third call that stays pending"
CASSANDRA_KEY=$AUTHOR_KEY python3 cassandra/predict.py --rpc "$RPC" seal \
  --asset SOL-USD --claim "close above 180" --resolve "$RESOLVE_LATE" --stake 0.1 \
  | tail -3

step "Moving past the deadline"
curl -s -o /dev/null -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"devnet_increaseTime","params":[3600],"id":1}' "$RPC"
echo "advanced one hour"

step "A keeper sweeps the abandoned call and takes the stake"
CASSANDRA_KEY=$KEEPER_KEY python3 cassandra/predict.py --rpc "$RPC" sweep \
  --committer "$AUTHOR" --commitment "0x$ABANDONED"
CASSANDRA_KEY=$KEEPER_KEY python3 cassandra/predict.py --rpc "$RPC" withdraw

step "The public record, rebuilt from chain events alone"
python3 cassandra/predict.py --rpc "$RPC" ledger

printf '\n\033[1mdemo complete\033[0m — the forfeited call cannot be removed from that record.\n'
