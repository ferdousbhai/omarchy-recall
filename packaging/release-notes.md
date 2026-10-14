Recall for Omarchy 0.4.0 consolidates desktop coordination and the public CLI into native Rust.

- `recall` is the native JSON CLI; its bundled agent skill is compiled directly from Markdown.
- Search and settings share the Omarchy QML panel. The Python/GTK application and Python socket bridge are removed.
- Rust owns folder watching, power policy, verified model downloads, indexing controls and page/line/timestamp-aware opening.
- `recall-engine` keeps search and indexing workers isolated so model memory can be released independently.
- Existing models, indexes and selected folders are preserved. Vulkan GPU acceleration and CPU fallback remain available on x86_64 and aarch64, including Apple Silicon Linux.
- CLI/desktop use no Python or GTK runtime. Optional OCR retains its isolated Python environment; skip it with `RECALL_SKIP_OCR=1` in the bash installer and add it later with `recall ocr`.
- Vector scoring uses bulk float16 conversion and reusable scratch memory; candidate selection avoids sorting the entire result set. Shared-library aliases remain symlinks.

Install: `curl -fsSL https://ferdousbhai.com/recall/install.sh | bash`

The Omarchy package submission remains subject to maintainer approval and publication. Based on pdstn12/Recall (MIT), inspired by ARahim3/DigUp.
