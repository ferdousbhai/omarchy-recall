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
omarchy pkg add omarchy-recall
omarchy-recall setup
```

## Desktop and agents

**Super+Ctrl+Shift+F** opens search. Arrow keys select, Enter opens at the matching page/line/media interval, and Ctrl+Enter reveals the file. Settings chooses folders; Pause stops background indexing.

```bash
omarchy-recall search "payment error" --kind screenshot --limit 5
omarchy-recall search "retry with exponential backoff" --code
omarchy-recall text 123 --code
omarchy-recall status
omarchy-recall doctor
omarchy-recall skill
omarchy-recall skill install
```

The CLI outputs JSON with meaningful exit codes. The embedded skill installs to the shared agents skill directory, with Claude Code linking when available. Searching never opens a window. The background service must be running. Only selected folders are searched; code repositories require explicit selection. Audio matches point to intervals, not word-level transcripts.

[Usage and development](omarchy/README.md) · [Website](https://ferdousbhai.com/recall)

## Build

```bash
export PATH="$HOME/.cargo/bin:$PATH"
scripts/build-llama.sh
cargo build --release --locked
cargo test --release --locked
python3 -m unittest discover -s omarchy/tests
python3 omarchy/embed_skill.py
omarchy/bundle.sh
```

## Credits and license

Based on [Recall by pdstn12](https://github.com/pdstn12/Recall), inspired by [DigUp by Abdur Rahim](https://github.com/ARahim3/DigUp). The Rust indexing/retrieval engine and GTK settings interface come from Recall. This fork adds the native Omarchy panel, bar integration, agent CLI/skill, packaging, and ARM fixes. MIT; see LICENSE and THIRD_PARTY_NOTICES.md. [Original project documentation](UPSTREAM_README.md).
