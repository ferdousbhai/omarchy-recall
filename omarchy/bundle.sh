#!/bin/bash
# Build an architecture-specific, relocatable user-install bundle (models excluded).
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -x target/release/recall ]] || { echo 'Build recall first' >&2; exit 1; }
python3 omarchy/embed_skill.py
out=${1:-"$PWD/dist"}
mkdir -p "$out"
out=$(realpath "$out")
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
root="$stage/recall-for-omarchy"
mkdir -p "$root/target/release"
cp target/release/recall Vendor/llama/lib/*.so* "$root/target/release/"
cp -r app omarchy "$root/"
mkdir -p "$root/tests"
cp -r tests/reference "$root/tests/"
mkdir -p "$root/scripts"
cp scripts/setup-ocr.sh "$root/scripts/"
cp -r ocr "$root/"
cp LICENSE THIRD_PARTY_NOTICES.md README.md UPSTREAM_README.md "$root/"
find "$root" -type d -name __pycache__ -prune -exec rm -rf {} +
tar -C "$stage" -czf "$out/recall-for-omarchy-$(uname -m).tar.gz" recall-for-omarchy
sha256sum "$out/recall-for-omarchy-$(uname -m).tar.gz" > "$out/recall-for-omarchy-$(uname -m).tar.gz.sha256"
echo "$out/recall-for-omarchy-$(uname -m).tar.gz"
