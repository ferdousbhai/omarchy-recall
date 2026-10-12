#!/bin/bash
# Installs Recall for Linux for this user, from this checkout: the app in the applications menu (with its icon) and
# OCR if it isn't set up yet. The engine must be built first (cargo build --release, see the README).
# ./install.sh --uninstall takes it out again (your index, settings and the model stay in ~/.local/share/recall).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
app_id=io.github.pdstn12.Recall
data=${XDG_DATA_HOME:-$HOME/.local/share}
desktop=$data/applications/$app_id.desktop
icon=$data/icons/hicolor/scalable/apps/$app_id.svg
autostart=${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$app_id.desktop

refresh() {
  command -v update-desktop-database > /dev/null && update-desktop-database -q "$data/applications" || true
  command -v gtk-update-icon-cache > /dev/null && gtk-update-icon-cache -q -t "$data/icons/hicolor" 2> /dev/null || true
  command -v kbuildsycoca5 > /dev/null && kbuildsycoca5 > /dev/null 2>&1 || true
  command -v kbuildsycoca6 > /dev/null && kbuildsycoca6 > /dev/null 2>&1 || true
}

if [[ "${1:-}" == "--uninstall" ]]; then
  "$here/recall-app" --quit 2> /dev/null || true
  rm -f "$desktop" "$icon" "$autostart"
  refresh
  echo "Recall is uninstalled. Your index, settings and the model are still in $data/recall and ~/.config/recall."
  exit 0
fi

if [[ ! -x "$here/../target/release/recall" ]]; then
  echo "Build the engine first: cargo build --release (see the README's \"Build and install\")" >&2
  exit 1
fi
python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' 2> /dev/null || {
  echo "Recall needs GTK 4 and libadwaita for Python: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1" >&2
  exit 1
}

mkdir -p "$(dirname "$desktop")" "$(dirname "$icon")"
sed "s|@EXEC@|$here/recall-app|g" "$here/data/$app_id.desktop" > "$desktop"
cp "$here/data/$app_id.svg" "$icon"
# A start at login that points at this checkout (Settings → General turns it on and off).
[[ -f "$autostart" ]] && sed -i "s|^Exec=.*|Exec=$here/recall-app --background|" "$autostart"
refresh
echo "Installed: Recall is in the applications menu."

if [[ ! -x "$data/recall/ocr/bin/recall-ocr" ]]; then
  echo "Setting up OCR (the text in screenshots)…"
  "$here/../scripts/setup-ocr.sh" || echo "OCR isn't set up: run scripts/setup-ocr.sh later (needs uv)." >&2
fi
