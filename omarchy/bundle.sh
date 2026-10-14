#!/bin/bash
# Build an architecture-specific, relocatable user-install bundle (models excluded).
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -x target/release/recall ]] || { echo 'Build recall first' >&2; exit 1; }
out=${1:-"$PWD/dist"}
mkdir -p "$out"
out=$(realpath "$out")
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
root="$stage/omarchy-recall"
mkdir -p "$root/target/release"
cp target/release/recall Vendor/llama/lib/*.so* "$root/target/release/"
cp -r app omarchy "$root/"
mkdir -p "$root/tests"
cp -r tests/reference "$root/tests/"
cp LICENSE THIRD_PARTY_NOTICES.md README.md "$root/"
find "$root" -type d -name __pycache__ -prune -exec rm -rf {} +
tar -C "$stage" -czf "$out/omarchy-recall-$(uname -m).tar.gz" omarchy-recall
sha256sum "$out/omarchy-recall-$(uname -m).tar.gz" > "$out/omarchy-recall-$(uname -m).tar.gz.sha256"
echo "$out/omarchy-recall-$(uname -m).tar.gz"
