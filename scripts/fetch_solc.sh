#!/usr/bin/env bash
# Fetch a pinned solc binary and verify it against the checksum the Solidity
# team publishes alongside it.
#
# The usual routes (npm, pypi, binaries.soliditylang.org) are unreachable from
# some sandboxes, so this pulls the binary out of the official ethereum/solc-bin
# git repository instead. A blobless partial clone keeps it to a single file
# rather than the several GB of history that repo holds.
#
# Usage: scripts/fetch_solc.sh [version] [destination]
set -euo pipefail

VERSION="${1:-0.8.36}"
DEST="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.toolchain}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$DEST"

echo "cloning ethereum/solc-bin metadata..."
git clone --quiet --filter=blob:none --no-checkout --depth 1 \
  https://github.com/ethereum/solc-bin "$WORK/solc-bin"

# Resolve the exact build for this version from the published manifest.
git -C "$WORK/solc-bin" show HEAD:linux-amd64/list.json > "$WORK/list.json"
read -r BUILD EXPECTED < <(python3 - "$WORK/list.json" "$VERSION" <<'PY'
import json, sys
manifest, version = sys.argv[1], sys.argv[2]
builds = [b for b in json.load(open(manifest))["builds"] if b["version"] == version]
if not builds:
    sys.exit(f"no linux-amd64 build published for solc {version}")
print(builds[-1]["path"], builds[-1]["sha256"].removeprefix("0x"))
PY
)

echo "fetching $BUILD"
git -C "$WORK/solc-bin" show "HEAD:linux-amd64/$BUILD" > "$WORK/solc"

ACTUAL="$(sha256sum "$WORK/solc" | cut -d' ' -f1)"
if [ "$ACTUAL" != "$EXPECTED" ]; then
  echo "error: checksum mismatch" >&2
  echo "  expected $EXPECTED" >&2
  echo "  actual   $ACTUAL" >&2
  exit 1
fi
echo "sha256 verified: $ACTUAL"

chmod +x "$WORK/solc"
mv "$WORK/solc" "$DEST/solc"
"$DEST/solc" --version | tail -1
echo "installed at $DEST/solc"
