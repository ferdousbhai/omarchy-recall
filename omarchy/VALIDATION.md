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

- Published v0.2.0: CPU fallback and real-model software-Vulkan smoke tests passed on both x86_64 and aarch64 hosted runners: https://github.com/ferdousbhai/recall-for-omarchy/actions/runs/38543459225. The published ARM binary also passed on the physical M1 Max GPU. Native v0.2.0 Arch packaging/checks passed with `makepkg --cleanbuild --nodeps`.


## Performance and cleanup review — 2026-10-14

Implemented lazy fallback GTK panel construction; deferred vector loading after index updates;
partial candidate selection with deterministic final ordering; reusable float32 conversion scratch;
and bulk float16 conversion through the existing `half` dependency. Bundles preserve shared-library
symlinks and use portable checksum filenames. `RECALL_SKIP_OCR=1` skips OCR in the bash installer;
`recall ocr` installs it later. OCR remains enabled by default to preserve screenshot text search.

Synthetic vector-only benchmark on the M1 Max, five warmed scoring runs per size:

| Vectors | Before bulk conversion (ms) | After (ms) | Float16 storage |
| --- | --- | --- | --- |
| 10,000 | 13.06 | 5.55 | 15.36 MB |
| 100,000 | 89.60 | 47.62 | 153.6 MB |
| 1,000,000 | 927.24 | 484.29 | 1.536 GB |

Reproduce with `cargo run --release --example vector_bench`. These are synthetic scoring timings,
not end-to-end retrieval or a representative quality evaluation. Exact search remains linear;
an approximate index would add complexity and a recall tradeoff, so this review does not add one.
The v0.3.0 ARM bundle contained 13.61 MiB of duplicate shared-library alias bytes; preserving
symlinks removes those duplicates (compressed savings vary). Runtime dependencies remain unchanged.
GTK lazy loading removes unnecessary construction but does not eliminate GTK settings dependencies.

Validation: 77 Rust tests including model reference vectors; 16 Python integration tests in the
checkout and extracted bundle; clean Clippy with warnings denied; clean Ruff F checks; shell syntax,
byte compilation, relocatable Vulkan device detection, checksum verification, and real-model CPU
smoke. Ranking tests compare limited results to the full ordering and exercise lazy invalidation.
Final review found no further concrete low-risk cleanup in the reviewed paths. This is not a claim
that no future performance improvement is possible; large-index ANN and alternative OCR stacks
remain workload/quality tradeoffs requiring separate evidence.

## Native runtime consolidation — 2026-10-14 (0.4.0)

The Python/GTK app and Python CLI/bridge/installer/skill generator are removed. The public
`recall` binary and coordinator are Rust; the private worker is `recall-engine`. Search and
settings use QML. Python remains only for optional OCR and developer benchmark/smoke scripts.
The public executable links to libc/libgcc only; inference plugins are loaded by the worker.
No additional Rust dependency was introduced. The coordinator measured approximately 3 MiB RSS
on the M1 Max versus approximately 80 MiB for the earlier Python/GTK coordinator, excluding
workers and the existing Quickshell desktop process. Timing varies with system load; no CLI
latency claim is made from the busy development machine.

Regression checks cover JSON arguments/errors, embedded skill and custom-directory preservation,
private socket mode, duplicate-daemon protection, settings validation/reload, inotify debounce/new
subdirectories, scoped opening, code lines, media timestamps, shell-special characters, persistent
worker ownership and executable replacement. Existing extraction, retrieval, reference-vector and
ranking checks remain. CPU and Vulkan real-model smoke pass. The bash installer was checked for
native setup, optional OCR dependencies and checksum rejection before changes. QML settings was
opened and visually inspected on the compatible Omarchy shell. Its save button is pinned outside
the scrolling content.

The coordinator bounds clients and its search queue, coalesces vector invalidation, and retains
periodic watch recovery and battery/power-saver policy. Model filenames, revision, sizes and hashes
now have one Rust source; release validation reads `recall models`. Existing settings, selected
folders, downloaded models and indexes are preserved.

The final scan of runtime dependencies, stale Python/GTK references, process lifecycle, packaging
and changed paths found no additional concrete simplification that preserves tested behavior.
This does not claim that every possible workload-specific optimization has been exhausted.

The pending 0.4.0 build was cancelled during the final scan. Version 0.4.1 also checks current
folder scope and exclusions for retained search hits, indexed passages and opening actions;
empty selections return no hits. Regression tests cover this inherited edge case. Downloads
are deferred when no folders are selected. Existing cached data remains intact.

## System OCR (0.4.2)

Replaced the optional RapidOCR Python environment with system Tesseract, supplied by Omarchy.
The Rust adapter bounds each image to 30 seconds and both pipes to 1 MiB, reaps the child,
and starts a fresh process per image so failures do not poison later OCR.
The fixture `tests/reference/ocr.png` contains synthetic English text; integration tests check
recognition, recovery after invalid input, paths containing spaces, and service-independent CLI
OCR diagnostics. Unit tests check timeout recovery, failing exits and excessive stdout/stderr.
A manual comparison on the fixture produced identical text from Tesseract and the previous
PP-OCRv5 helper. This does not establish general accuracy parity on photos or complex layouts.
Existing indexed content is preserved; the engine does not automatically re-OCR unchanged files.
