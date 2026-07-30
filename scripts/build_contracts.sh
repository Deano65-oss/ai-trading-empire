#!/usr/bin/env bash
# Compile the Solidity sources in contracts/ into build/contracts/.
#
# Uses solc's Standard JSON interface so the build is reproducible: the exact
# compiler version, optimizer settings and source hashes all end up in the
# emitted metadata.
#
# Set SOLC to point at a solc binary (default: whatever is on PATH).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOLC="${SOLC:-solc}"
OUT="$ROOT/build/contracts"

if ! command -v "$SOLC" >/dev/null 2>&1 && [ ! -x "$SOLC" ]; then
  echo "error: solc not found (set SOLC=/path/to/solc)" >&2
  exit 1
fi

echo "compiler: $("$SOLC" --version | tail -1)"
rm -rf "$OUT"
mkdir -p "$OUT"

python3 - "$ROOT" "$OUT" "$SOLC" <<'PY'
import json, pathlib, subprocess, sys

root, out, solc = (pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3])
sources = sorted((root / "contracts").glob("*.sol"))
if not sources:
    sys.exit("error: no .sol files under contracts/")

std_input = {
    "language": "Solidity",
    "sources": {
        str(p.relative_to(root)): {"content": p.read_text()} for p in sources
    },
    "settings": {
        "optimizer": {"enabled": True, "runs": 200},
        "evmVersion": "cancun",
        "outputSelection": {
            "*": {
                "*": [
                    "abi",
                    "evm.bytecode.object",
                    "evm.deployedBytecode.object",
                    "evm.gasEstimates",
                    "evm.methodIdentifiers",
                    "metadata",
                    "storageLayout",
                    "userdoc",
                    "devdoc",
                ]
            }
        },
    },
}

proc = subprocess.run(
    [solc, "--standard-json", "--base-path", str(root)],
    input=json.dumps(std_input),
    capture_output=True,
    text=True,
)
if proc.returncode != 0:
    sys.exit(f"solc failed: {proc.stderr.strip()}")

result = json.loads(proc.stdout)
errors = [d for d in result.get("errors", []) if d["severity"] == "error"]
warnings = [d for d in result.get("errors", []) if d["severity"] != "error"]

for d in warnings:
    print(d["formattedMessage"].rstrip())
if errors:
    for d in errors:
        print(d["formattedMessage"].rstrip(), file=sys.stderr)
    sys.exit(f"error: {len(errors)} compilation error(s)")

for path, units in result["contracts"].items():
    for name, c in units.items():
        artifact = out / f"{name}.json"
        artifact.write_text(json.dumps({"contractName": name, "sourceName": path, **c}, indent=2))
        runtime = c["evm"]["deployedBytecode"]["object"]
        creation = c["evm"]["bytecode"]["object"]
        print(
            f"  {name:<20} runtime {len(runtime)//2:>6} bytes"
            f"   creation {len(creation)//2:>6} bytes   -> {artifact.relative_to(root)}"
        )
        limit = 24576  # EIP-170 deployed-code limit
        if len(runtime) // 2 > limit:
            sys.exit(f"error: {name} exceeds the EIP-170 limit of {limit} bytes")

print(f"\n{len(warnings)} warning(s), 0 error(s)")
PY

echo "build complete: $(realpath --relative-to="$ROOT" "$OUT")"
