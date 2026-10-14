#!/bin/bash
# Recall for Omarchy: release installer. No source build and no changes under /usr/share/omarchy.
# It does add a marked Super+Ctrl+Shift+F block to ~/.config/hypr/bindings.lua (backed up first);
# set RECALL_NO_KEYBINDING=1 to skip that.
# Usage: install.sh [--yes]   (--yes or RECALL_YES=1 installs packages without asking)
set -euo pipefail
ASSUME_YES=${RECALL_YES:-0}
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done
REPOSITORY=ferdousbhai/recall-for-omarchy
VERSION=${RECALL_VERSION:-latest}
[[ $(uname -s) == Linux ]] || { echo 'Recall requires Linux/Omarchy.' >&2; exit 1; }
ARCH=$(uname -m)
case "$ARCH" in x86_64|aarch64) ;; *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;; esac
command -v omarchy >/dev/null || { echo 'Install Omarchy first: https://omarchy.org' >&2; exit 1; }
command -v jq >/dev/null || { echo 'jq is required.' >&2; exit 1; }
command -v curl >/dev/null || { echo 'curl is required.' >&2; exit 1; }
if [[ $VERSION == latest ]]; then
  AUTH=()
  [[ -n ${GITHUB_TOKEN:-} ]] && AUTH=(-H "Authorization: Bearer $GITHUB_TOKEN")
  if ! RELEASE=$(curl -fsSL "${AUTH[@]}" "https://api.github.com/repos/$REPOSITORY/releases/latest"); then
    echo 'Could not look up the latest release (GitHub API rate limit or network error).' >&2
    echo 'Retry later, set GITHUB_TOKEN, or pin a version: RECALL_VERSION=v0.4.2' >&2
    exit 1
  fi
  VERSION=$(jq -er '.tag_name' <<<"$RELEASE") || { echo 'Unexpected GitHub API response.' >&2; exit 1; }
fi
[[ $VERSION =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo 'Invalid release version.' >&2; exit 1; }
BASE="https://github.com/$REPOSITORY/releases/download/$VERSION"
ASSET="recall-for-omarchy-$ARCH.tar.gz"
TEMP=$(mktemp -d)
trap 'rm -rf "$TEMP"' EXIT
curl -fSL --retry 3 "$BASE/$ASSET" -o "$TEMP/$ASSET"
curl -fsSL --retry 3 "$BASE/$ASSET.sha256" -o "$TEMP/checksum"
DIGEST=$(awk 'NR==1 {print $1}' "$TEMP/checksum")
[[ $DIGEST =~ ^[0-9a-f]{64}$ ]] || { echo 'Invalid checksum file.' >&2; exit 1; }
(cd "$TEMP"; printf '%s  %s\n' "$DIGEST" "$ASSET" | sha256sum --check --status)
# Preserve authentication input for piped installers; also support noninteractive environments.
install_packages() {
  echo "Recall needs these packages: $*"
  if [[ $ASSUME_YES != 1 ]]; then
    if (: < /dev/tty) 2>/dev/null; then
      read -r -p 'Install them with omarchy pkg add? [Y/n] ' reply < /dev/tty
      [[ -z $reply || $reply =~ ^[Yy] ]] || { echo 'Cancelled.' >&2; exit 1; }
    else
      echo 'No terminal to confirm; rerun with --yes (or RECALL_YES=1) to install packages.' >&2
      exit 1
    fi
  fi
  if (: < /dev/tty) 2>/dev/null; then
    omarchy pkg add "$@" < /dev/tty
  else
    omarchy pkg add "$@" < /dev/null
  fi
}
DEPS=(curl jq ffmpeg poppler libvips xdg-utils xdg-terminal-exec vulkan-icd-loader glib2 tesseract tesseract-data-eng)
install_packages "${DEPS[@]}"
# Apple GPUs on Linux use the Asahi Vulkan userspace driver.
if [[ -r /proc/device-tree/model ]] && grep -aq Apple /proc/device-tree/model; then
  install_packages vulkan-asahi
fi
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/recall-for-omarchy/releases/$VERSION"
mkdir -p "$DEST"
tar -xzf "$TEMP/$ASSET" -C "$DEST"
ROOT="$DEST/recall-for-omarchy"
"$ROOT/target/release/recall-engine" --help >/dev/null
# Also works when RECALL_VERSION selects an older release with optional Python OCR.
"$ROOT/target/release/recall" setup --skip-ocr
printf '\nRecall installed. Open search with Super+Ctrl+Shift+F.\nAgents: recall skill\n'
