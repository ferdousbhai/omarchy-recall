# Recall for Omarchy

Search your files by what’s in them. A native Omarchy desktop app **with a CLI and a bundled skill, so coding agents can search your files too**.

Describe a photo, find a document passage, jump to a recording interval, or search source code by intent. EmbeddingGemma 2 runs locally; file content, OCR text and embeddings stay on your computer.

## Install

```bash
curl -fsSL https://ferdousbhai.com/recall/install.sh | bash
```

Supports Omarchy on x86_64 and aarch64. The installer downloads a versioned, checksum-verified release, installs runtime dependencies through Omarchy, and configures the desktop integration and agent skill. Vulkan GPU acceleration is included on both architectures, with CPU fallback. Apple Silicon on Linux uses Asahi’s Honeykrisp driver; the installer adds `vulkan-asahi` on Apple machines. Model files (865 MB) and OCR models download once. Initial indexing takes time.

[Omarchy package PR #1066](https://github.com/omacom/omarchy-pkgs/pull/1066) awaits upstream build approval and publication. Once published in your configured channel:

```bash
omarchy pkg add recall-for-omarchy
recall setup --skip-ocr
# Optional OCR:
omarchy pkg add uv
recall ocr
```

For code/text-only use, skip the optional OCR environment:

```bash
curl -fsSL https://ferdousbhai.com/recall/install.sh | RECALL_SKIP_OCR=1 bash
# Add screenshot/scanned-document text recognition later:
recall ocr
```

Images still support visual semantic search without OCR; recognizing their text requires OCR.

## Desktop and agents

**Super+Ctrl+Shift+F** opens search. Arrow keys select, Enter opens at the matching page/line/media interval, and Ctrl+Enter reveals the file. Settings chooses folders; Pause stops background indexing.

```bash
recall search "payment error" --kind screenshot --limit 5
recall search "retry with exponential backoff" --code
recall text 123 --code
recall status
recall doctor
recall skill
recall skill install
```

The CLI and background coordinator are native Rust. The search panel and settings use QML. Python is needed only for optional OCR; GTK is not required. The CLI outputs JSON with meaningful exit codes. The embedded skill installs to the shared agents skill directory, with Claude Code linking when available. Searching never opens a window. The background service must be running. Only selected folders are searched; code repositories require explicit selection. Audio matches point to intervals, not word-level transcripts.

[Usage and development](omarchy/README.md) · [Website](https://ferdousbhai.com/recall)

## Build

```bash
export PATH="$HOME/.cargo/bin:$PATH"
scripts/build-llama.sh
cargo build --release --locked
cargo test --release --locked
cargo clippy --all-targets -- -D warnings
omarchy/bundle.sh
```

## Credits and license

Based on [Recall by pdstn12](https://github.com/pdstn12/Recall), inspired by [DigUp by Abdur Rahim](https://github.com/ARahim3/DigUp). The Rust indexing/retrieval engine comes from Recall. This fork adds a native Rust CLI/coordinator and QML settings, alongside the native Omarchy panel, bar integration, bundled agent skill, packaging, and ARM fixes. MIT; see LICENSE and THIRD_PARTY_NOTICES.md. [Original project documentation](UPSTREAM_README.md).
