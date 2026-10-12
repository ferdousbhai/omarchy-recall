<p align="center"><img src="docs/logo.png" width="96" alt=""></p>

<h1 align="center">Recall</h1>

<p align="center"><b>Describe it. Recall it.</b><br>
Search the files on your Linux computer by what's in them: documents, scans, screenshots, photos, recordings and
videos. Everything runs on your computer.</p>

<p align="center"><img src="docs/demo.gif" width="800" alt="Recall finding a contract clause on its page, the moment in a meeting recording, a scanned receipt, an invoice number and a sentence in a handbook"></p>

You remember what was in a file, not what it was called or where you put it. Recall reads the folders you pick and
lets you find things by describing them, in your own words:

- "the clause about ending the Globex contract early" opens the contract **at page 2**, with the sentence marked
- "when did they decide to freeze hiring" plays the meeting recording **from 0:30**
- "dinner with the Globex team receipt" finds a **scanned** receipt by the text printed on it
- "payment gateway timeout" finds the **screenshot** of the error
- "INV-2026-0187" finds the invoice number, as written, in PDFs and emails
- "code: retry a failed call with backoff" finds the function in your projects, if you turn on code search

The preview shows **where** it matched: the words and the best-matching sentence marked on the page or screenshot,
the stretch of a recording marked on its timeline, ready to play.

## No LLM, no cloud

Recall doesn't generate anything, so it has nothing to make up. It uses an **embedding model**, Google DeepMind's
[EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2): every picture, page, passage, video frame and
30 seconds of sound becomes a vector in one shared space, and so do your words. A search finds the nearest vectors and
shows you the real file, at the real page or second.

Nothing you index leaves your computer. Recall goes online only to download the model once (865 MB). No account, no
telemetry.

## Using it

| Key | What it does |
|---|---|
| Shift+Super+Space | Opens the search panel from any app (Super is the Windows key) |
| ↓ ↑ | Moves through the results |
| ↩ | Opens the result where it matched: PDFs at the page (Evince, Okular, Zathura), videos and recordings at the moment (VLC, mpv), code in your editor at the line |
| Space | Plays the selected video or recording in the preview, from its moment (once you've moved to a result) |
| Ctrl+↩ | Shows it in the file manager |
| Ctrl+C | Copies the file, to paste anywhere |
| Esc | Closes the panel |

Recall lives in the tray, where you can see how indexing is going, pause it, or open Settings.

## What it reads

Only the folders you choose:

- pictures and screenshots (JPEG, PNG, HEIC, WebP, RAW and more), by what they show, and by their text (OCR)
- PDFs, page by page; scanned pages by their picture and their text (OCR). A long PDF gets its first 30 pages right
  away and the rest once everything else is in (up to 5,000 pages a file)
- documents: txt, md, rtf, doc, docx, odt, html
- slide decks (pptx), slide by slide with their speaker notes, and spreadsheets (xlsx), row by row under their
  column headers
- audio of any length, in overlapping 30-second windows, so a match points to its moment
- video of any length: a frame every few seconds, one per shot, plus the soundtrack the same way as audio

It never reads keys, certificates, password files, hidden folders like `~/.ssh`, or caches. Inside a code project (a
folder with `.git`, `package.json` and the like) it reads only screenshots, unless you turn on code search for it. It
skips folders that look like datasets. Recall only reads your files; it never changes, moves or uploads them.

After the first pass, a new file is searchable a second or two after it lands (inotify). The first pass waits for a
charger if you ask it to, and while the system saves power.

## Code search

Turn it on in Settings → Code for your projects, then start a search with `code:` and say what the code does, or type
a name that's in it. A name goes to the line that defines it. Recall reads source files, notebooks (without their
outputs) and a repo's own docs, and skips what git ignores, vendored and generated code, and anything that looks like a
secret. Results open in VS Code, VSCodium, Cursor, Zed, Sublime Text, JetBrains IDEs, Kate or gedit, at the line.

## How it works

```
your folders ─► readers: poppler (PDFs), ffmpeg (sound, video), vips (pictures), PaddleOCR (text in images)
             ─► EmbeddingGemma 2 on llama.cpp (CUDA, Vulkan or CPU), in a helper process that quits when it's done
             ─► one SQLite file: a vector for every picture, page, passage, video frame and 30 s of sound,
                plus a keyword index (FTS5); code search keeps a second one
your words   ─► the same model's text part ─► nearest vectors + exact words ─► the panel, with where it matched
```

- **Hybrid search.** Meaning comes from the vectors, scored on one scale per kind of input so a video frame can be
  ranked against a PDF page. Exact words come from SQLite's full-text index, because no embedding holds an invoice
  number exactly. Both count in the ranking.
- **Where it matched.** For the preview, the engine ranks the sentences of the page (or the lines of a screenshot)
  against your words, and marks the best one, even when none of your words is in it.
- **On your GPU.** llama.cpp's backends are plugins chosen when Recall starts: CUDA, else Vulkan (an integrated Intel
  or AMD GPU works), else the CPU. `RECALL_DEVICE=cpu|cuda|vulkan` picks one. The query encoder runs on the CPU, where
  it starts fastest.
- **Pieces.** The engine is Rust, with a small C shim over llama.cpp: `recall` indexes, searches and serves the app
  over JSON lines. The app is Python with GTK 4 and libadwaita. The model files are pinned and checked by SHA-256.

## Benchmark

On an everyday laptop (Intel Core i5-1334U with Iris Xe graphics, Ubuntu 24.04), over a test set of company files
(contracts, legal documents, emails, invoices, a scanned receipt, screenshots, meeting videos and recordings;
`scripts/testbed/make_enterprise.py` builds it, `evals/enterprise.json` asks it 22 questions):

| | |
|---|---|
| Right answer first | 95% (R@1 0.95, MRR 0.98) |
| Right answer in the top 5 | 100% |
| Results by words, while you type | 4 ms |
| Search with meaning | 0.17 s |
| Indexing, integrated GPU (Vulkan) against CPU | 3.2× faster, 0.3 GB of memory against 1.4 GB |

`tests/reference_vectors.rs` checks that the vectors match the reference model's (cosine ≥ 0.999 for text in three
languages, images, video frames and speech), on every backend.

## Limits

- Sound is matched as sound, not transcribed: Recall finds the moment, but can't quote what was said.
- OCR reads Latin script and Chinese for now.
- Slide decks and spreadsheets are read as text: pictures and charts on slides aren't, and LibreOffice's own ods and
  odp files aren't read yet.
- The global shortcut works on X11 everywhere; on Wayland it depends on the desktop's portal (KDE Plasma 6, GNOME 48+).
- The first pass over big folders takes a while on a computer without a usable GPU.

## Build and install

You'll need Rust (1.80 or later), CMake, and the usual tools:

```sh
sudo apt install cmake ninja-build ffmpeg poppler-utils unzip catdoc libvips-tools \
    python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
    libvulkan-dev glslc spirv-headers          # Vulkan (optional); nvidia-cuda-toolkit for CUDA
curl https://sh.rustup.rs -sSf | sh       # Rust, if you don't have it

git clone https://github.com/pdstn12/Recall && cd Recall
scripts/build-llama.sh            # llama.cpp at a pinned tag, with its GPU backends as plugins
cargo build --release && cargo test --release
app/install.sh                    # Recall in the applications menu, and OCR (needs uv)
```

Open Recall from the menu: it downloads the model and asks which folders to search. Its files live in
`~/.local/share/recall` (the index, the model, OCR) and `~/.config/recall` (settings).

The engine works from the terminal too:

```sh
target/release/recall index ~/Documents          # sync and embed a folder
target/release/recall search "lease renewal terms"
target/release/recall status                      # what's in the index
target/release/recall --help
```

## Development

```
src/            the engine: crawler, readers, SQLite store, indexer, hybrid searcher, llama.cpp embedder
src/cli/        the commands and the JSON-lines services the app talks to (serve, index-service, worker)
csrc/           a small C API over llama.cpp and mtmd, so the engine never depends on their struct layouts
app/            the desktop app (Python, GTK 4, libadwaita)
ocr/            the OCR helper (PaddleOCR models through RapidOCR)
tests/          integration tests, and the reference vectors every llama.cpp update must reproduce
evals/          retrieval evals with known answers
scripts/        llama.cpp and OCR setup, and the generators of the test sets the evals run on
```

`cargo test --release` runs the unit and integration tests; the reference-vector test needs the model files and is
skipped without them. To measure search quality, build a test set (a made-up company's documents, scans and meeting recordings; the
script's header lists what it needs) and run its eval:

```sh
uv run --with piper-tts --with pillow scripts/testbed/make_enterprise.py ~/RecallTestbed/Acme --voices DIR
target/release/recall index ~/RecallTestbed/Acme --index /tmp/eval-index
target/release/recall eval evals/enterprise.json --index /tmp/eval-index
```

## Roadmap

- Shortcuts that work the same on every Wayland desktop
- Flatpak and AppImage packages
- More OCR languages, and OCR on the GPU
- Pictures and charts on slides; LibreOffice's ods and odp
- Optional local transcription, to quote what was said in recordings

## Thanks

Recall started from [DigUp](https://github.com/ARahim3/DigUp), Abdur Rahim's macOS app, which showed how well
EmbeddingGemma 2 searches personal files; Recall is its idea, rebuilt for Linux. It runs Google DeepMind's
EmbeddingGemma 2 (Apache 2.0) on [llama.cpp](https://github.com/ggml-org/llama.cpp) (MIT), and reads text in images with
[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)'s models through [RapidOCR](https://github.com/RapidAI/RapidOCR).
See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

MIT. See [LICENSE](LICENSE).
