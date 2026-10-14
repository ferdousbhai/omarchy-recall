#!/bin/bash
# Build an architecture-specific, relocatable user-install bundle (models excluded).
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -x target/release/recall-engine ]] || { echo 'Build recall first' >&2; exit 1; }
out=${1:-"$PWD/dist"}
mkdir -p "$out"
out=$(realpath "$out")
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
root="$stage/recall-for-omarchy"
mkdir -p "$root/target/release"
cp target/release/recall-engine target/release/recall "$root/target/release/"
cp -P Vendor/llama/lib/*.so* "$root/target/release/"
mkdir -p "$root/omarchy"
cp -r omarchy/plugin omarchy/assets omarchy/skills "$root/omarchy/"
cp omarchy/smoke.py "$root/omarchy/"
mkdir -p "$root/tests"
cp -r tests/reference "$root/tests/"
mkdir -p "$root/scripts"
cp scripts/setup-ocr.sh "$root/scripts/"
cp -r ocr "$root/"
cp LICENSE THIRD_PARTY_NOTICES.md README.md UPSTREAM_README.md "$root/"
find "$root" -type d -name __pycache__ -prune -exec rm -rf {} +
tar -C "$stage" -czf "$out/recall-for-omarchy-$(uname -m).tar.gz" recall-for-omarchy
(cd "$out"; sha256sum "recall-for-omarchy-$(uname -m).tar.gz" > "recall-for-omarchy-$(uname -m).tar.gz.sha256")
echo "$out/recall-for-omarchy-$(uname -m).tar.gz"
