# Validation on this installation

Tested on Arch Linux ARM (aarch64), CPU inference, 2026-10-14.

- Rust suite: 55 unit tests, 21 integration tests, 1 model-reference test passed.
- Real model reference vectors: all nine text/image/frame/audio comparisons exceeded cosine 0.999, including Bengali and Arabic queries.
- Python integration suite: socket permissions/transport, folder scope enforcement, media timestamp and Neovim line arguments passed (3 tests).
- Disposable real-model smoke: image/audio indexing and semantic document/code retrieval passed. The four-file corpus uses a relaxed semantic threshold.
- Live inotify check: a newly created synthetic document became searchable by its content; deletion removed its database entry.
- Native Quickshell panel: opened and displayed live semantic code matches with line locations; no plugin QML errors after installation.
- User service: enabled and active; Hyprland configuration validation clean.
- Extracted aarch64 bundle: packaged backend loaded the downloaded model successfully.

Initial Documents/Pictures pass indexed 137 files and skipped one. The new checkout's code is also enabled and indexes in the background. Large additional folders will need their own first pass.

Not validated here: CUDA/Vulkan acceleration, other architectures, word-level audio alignment, and an exhaustive set of external PDF/editor/player applications. Audio matches identify indexed intervals; they do not provide transcripts.

Agent CLI update:

- Eight Python tests passed, covering argument forwarding, code/passage routing, invalid argument rejection, unavailable-service exit status, semantic fallback warnings, and the original bridge/action checks.
- Installed CLI exercised against the live index: semantic code search with a two-result limit, full passage retrieval, screenshot filtering, and keyword-only search passed.
- Bundled agent skill passed the skill-creator validator and was installed in both Codex and shared agents skill directories.
