#!/bin/bash
# Recall for Omarchy: release installer. No source build, no modifications to packaged Omarchy files.
set -euo pipefail
REPOSITORY=ferdousbhai/omarchy-recall
VERSION=${RECALL_VERSION:-latest}
[[ $(uname -s) == Linux ]] || { echo 'Recall requires Linux/Omarchy.' >&2; exit 1; }
ARCH=$(uname -m)
case "$ARCH" in x86_64|aarch64) ;; *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;; esac
command -v omarchy >/dev/null || { echo 'Install Omarchy first: https://omarchy.org' >&2; exit 1; }
command -v curl >/dev/null || { echo 'curl is required.' >&2; exit 1; }
if [[ $VERSION == latest ]]; then
  VERSION=$(curl -fsSL "https://api.github.com/repos/$REPOSITORY/releases/latest" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')
fi
[[ $VERSION =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo 'Invalid release version.' >&2; exit 1; }
BASE="https://github.com/$REPOSITORY/releases/download/$VERSION"
ASSET="omarchy-recall-$ARCH.tar.gz"
TEMP=$(mktemp -d)
trap 'rm -rf "$TEMP"' EXIT
curl -fSL --retry 3 "$BASE/$ASSET" -o "$TEMP/$ASSET"
curl -fsSL --retry 3 "$BASE/$ASSET.sha256" -o "$TEMP/checksum"
DIGEST=$(awk 'NR==1 {print $1}' "$TEMP/checksum")
[[ $DIGEST =~ ^[0-9a-f]{64}$ ]] || { echo 'Invalid checksum file.' >&2; exit 1; }
(cd "$TEMP"; printf '%s  %s\n' "$DIGEST" "$ASSET" | sha256sum --check --status)
# Package installation may need a terminal for authentication when invoked through a pipe.
omarchy pkg add python python-gobject gtk4 libadwaita ffmpeg poppler libvips uv xdg-utils xdg-terminal-exec vulkan-icd-loader < /dev/tty
# Apple GPUs on Linux use the Asahi Vulkan userspace driver.
if [[ -r /proc/device-tree/model ]] && grep -aq Apple /proc/device-tree/model; then
  omarchy pkg add vulkan-asahi < /dev/tty
fi
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/omarchy-recall/releases/$VERSION"
mkdir -p "$DEST"
tar -xzf "$TEMP/$ASSET" -C "$DEST"
ROOT="$DEST/omarchy-recall"
"$ROOT/target/release/recall" --help >/dev/null
python3 "$ROOT/omarchy/cli.py" setup
printf '\nRecall installed. Open search with Super+Ctrl+Shift+F.\nAgents: omarchy-recall skill\n'
