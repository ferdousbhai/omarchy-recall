# Recall for Omarchy

A native Quickshell panel and bar button, an agent CLI, and a bundled usage skill backed by Recall's local multimodal search engine. Based on upstream Recall (MIT); its original README, license, and third-party notices are preserved.

## Install from source

Needs Python GObject/GTK4/libadwaita, FFmpeg, Poppler, libvips, Rust and a C/C++ compiler. `uv` supplies CMake/Ninja and the isolated OCR environment if needed. On ARM, the bundled llama.cpp build includes runtime-selected ARM CPU variants. GPU acceleration requires the corresponding supported build toolchain; Apple GPU acceleration on Linux is not assumed.

```sh
export PATH="$HOME/.cargo/bin:$PATH"
scripts/build-llama.sh
cargo build --release
cargo test --release
app/install.sh
python3 omarchy/install.py
```

Run `omarchy/install.py` from a permanent checkout: the launchers and service refer to this path. The first start downloads 865 MB of pinned, SHA-256-verified model files. Initial indexing covers existing Documents and Pictures folders. Choose code repositories explicitly in Settings. Existing Recall settings are preserved.

The installer reserves **Super+Ctrl+Shift+F**. Check `omarchy menu keybindings --print` before installation on another machine and change the installer binding if occupied. Existing files are backed up with timestamps. No root access or edits under `/usr/share/omarchy` are needed.

## Use

- Super+Ctrl+Shift+F or the magnifying-glass bar button opens search.
- Describe a photo or document, or type an exact identifier.
- Prefix source-code queries with `code:`.
- Up/Down selects; Enter opens at the page, line or media interval; Ctrl+Enter reveals the file.
- Settings selects folders, exclusions and editor. Pause/Resume controls background indexing.
- `omarchy-recall status`, `omarchy-recall search "query"`, `omarchy-recall pause`, `omarchy-recall resume` work in a terminal.
- `journalctl --user -u omarchy-recall` and `~/.local/share/recall/index/*.log` hold diagnostics.

The GTK application owns the indexing worker, inotify watchers, power policy and query process. Its optional Omarchy bridge exposes a mode-0600 Unix socket in the user's runtime directory. Quickshell uses an argument-safe JSON-lines client; no file names or queries are interpolated into shell commands. There is no network listener. Indexes, OCR text and previews remain sensitive local data; normal disk encryption is recommended.

Audio is embedded directly, without transcription: matches point to a window, not a word-level timestamp. The UI reports warming/fallback keyword searches. Model compatibility and search quality should be checked on representative files, not inferred from a synthetic benchmark.

## Remove integration

```sh
systemctl --user disable --now omarchy-recall.service
omarchy plugin disable ferdous.recall
```

Remove the marked `omarchy-recall` block from `~/.config/hypr/bindings.lua` and run `hyprctl reload` then `hyprctl configerrors`. Remove `~/.config/omarchy/plugins/ferdous.recall`, the user service file, and the four installed launchers if no longer needed. `app/install.sh --uninstall` removes the GTK desktop entry. Models, settings and indexes are retained unless you explicitly delete them.

## Validation and distribution

`python3 -m unittest discover -s omarchy/tests -v` checks socket permissions, dispatch and result-opening scope. `python3 omarchy/smoke.py` checks actual model inference, image/audio extraction and document/code retrieval in a disposable index (with a relaxed semantic gate because the corpus contains only four files). `RECALL_MODELS=~/.local/share/recall/Models cargo test --release --test reference_vectors -- --nocapture` verifies text, image, frame and speech vectors against pinned references.

`omarchy/bundle.sh` creates `dist/omarchy-recall-<architecture>.tar.gz` and its SHA-256 checksum. Extract to a permanent directory and run `app/install.sh` then `python3 omarchy/install.py`. The binary and shared libraries travel together; models and personal indexes are excluded. This is a user-install bundle, not a pacman/AUR package. It still requires the runtime dependencies listed above. CUDA/Vulkan builds must be validated separately; this installation was tested on aarch64 CPU.

The installer detects the running Omarchy shell path and handles the `ShellColor` compatibility rename used by newer Qt installations. If QML edits are cached, restart the shell using that same `OMARCHY_PATH` to preserve any local compatibility copy.

## Agent CLI and bundled skill

Agents can use the installed `omarchy-recall` command without opening the panel. JSON is the default output. Successful empty searches exit 0, service/search errors exit 1 with an `error` field, and invalid arguments exit 2. The shared background service must be running; `systemctl --user start omarchy-recall` starts it without a panel.

```sh
omarchy-recall --help
omarchy-recall status
omarchy-recall search "retry with exponential backoff" --code --limit 5
omarchy-recall search "payment error" --kind screenshot --limit 5
omarchy-recall search "release deadline" --kind audio --kind video
omarchy-recall search "ERR_CONNECTION_RESET" --keyword-only
omarchy-recall text 123 --code
```

`text` takes a fresh hit's `segment_id` and returns its full indexed passage. Use `--code` when the hit belongs to the code index. Results include source paths and page/line/time locations; inspect source content before making changes. Audio locations are intervals, not transcripts. Semantic encoder failures are reported explicitly in `encoder_error` and `warning` rather than being presented as successful semantic retrieval.

Like HEY CLI, `omarchy-recall skill` prints the skill embedded in the CLI package, and `omarchy-recall skill install` installs it without contacting the search service. The canonical installed location is `~/.agents/skills/omarchy-recall/SKILL.md`, which shared-skill-aware agents can discover directly. When Claude Code is available, installation links `~/.claude/skills/omarchy-recall` to that directory. Earlier identical Codex copies are migrated to links; custom agent skill directories are preserved.

The desktop installer calls the same `skill install` command. [The Markdown source](skills/omarchy-recall/SKILL.md) generates `embedded_skill.py` through `python3 omarchy/embed_skill.py`, also run by the bundle builder. The source and generated content are checked by tests. To remove the skill, remove its shared directory and agent links.
