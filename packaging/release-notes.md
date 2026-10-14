Local semantic search for Omarchy, with a native desktop panel, a CLI, and a bundled skill so coding agents can search your files too.

- Search documents, photos, screenshots, audio/video intervals and source code by meaning or exact text.
- EmbeddingGemma 2, OCR, hybrid retrieval and SQLite run locally.
- Keyboard search, bar button, folder watching, pause/resume and page/line/timestamp-aware opening.
- `omarchy-recall search` returns JSON; `omarchy-recall skill` prints the agent instructions and `skill install` installs them.
- Linux x86_64 and aarch64 builds with Vulkan GPU acceleration and CPU fallback. Apple Silicon Linux uses the Asahi/Honeykrisp Vulkan driver.
- Validated real text/image/audio embeddings on an Apple M1 Max GPU against the existing reference vectors; existing indexes stay compatible.
- `omarchy-recall doctor` reports the selected device and available backend plugins without loading the model.
- Short interactive queries remain on CPU by default; GPU acceleration applies to background indexing. CUDA remains an optional source-build backend.

Install: `curl -fsSL https://ferdousbhai.com/recall/install.sh | bash`

Requires Omarchy. Downloads the model and OCR assets on first setup; file content remains local. Audio search identifies intervals rather than word-level transcripts. Omarchy package repository submission is pending.

Based on pdstn12/Recall (MIT), inspired by ARahim3/DigUp.
