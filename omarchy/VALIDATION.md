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

Not validated here: CUDA acceleration, non-Apple physical GPU hardware, word-level audio alignment, and an exhaustive set of external PDF/editor/player applications. Audio matches identify indexed intervals; they do not provide transcripts.

Agent CLI update:

- Eight Python tests passed, covering argument forwarding, code/passage routing, invalid argument rejection, unavailable-service exit status, semantic fallback warnings, and the original bridge/action checks.
- Installed CLI exercised against the live index: semantic code search with a two-result limit, full passage retrieval, screenshot filtering, and keyword-only search passed.
- Bundled agent skill passed the skill-creator validator and was installed in both Codex and shared agents skill directories.

Vulkan GPU update (Apple M1 Max, Mesa Honeykrisp 26.2.4):

- Installed `vulkan-asahi`; Vulkan identifies the integrated Apple M1 Max GPU.
- All 77 Rust tests passed with `RECALL_DEVICE=vulkan`, including the nine reference-vector comparisons for text, images, sampled frames and speech.
- Twelve Python CLI/skill tests passed, including service-independent backend diagnostics.
- Actual service indexing logs confirm `Vulkan0 (Apple M1 Max (G13C C0))`; interactive queries remain CPU by default.
- Warmed, three-run-median fixture timings: photo embedding 2.0663 s CPU versus 0.4573 s GPU (4.52×); audio 0.4044 s versus 0.2544 s (1.59×). Short text query CPU 0.0293 s versus GPU 0.0948 s, supporting the CPU query default. These are fixture timings, not whole-library throughput guarantees.
- CPU/GPU embedding agreement exceeded cosine 0.999 for all benchmark modalities. Full measurements are in `m1-max-benchmark.json`; reproduce using `python3 omarchy/benchmark.py`.
- Explicit unavailable CUDA/invalid backend requests fail clearly. Automatic selection succeeds on CPU when no Vulkan driver is available.

- Published v0.2.0: CPU fallback and real-model software-Vulkan smoke tests passed on both x86_64 and aarch64 hosted runners: https://github.com/ferdousbhai/omarchy-recall/actions/runs/38543459225. The published ARM binary also passed on the physical M1 Max GPU. Native v0.2.0 Arch packaging/checks passed with `makepkg --cleanbuild --nodeps`.
