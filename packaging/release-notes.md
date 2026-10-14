Recall for Omarchy 0.4.2 uses Omarchy’s bundled Tesseract for OCR.

- No Python runtime, uv environment, RapidOCR, ONNX Runtime, or separate OCR model downloads are needed.
- Screenshot and scanned-PDF text extraction use installed Tesseract language packs, English first.
- OCR has a 30-second per-image deadline, bounded output, and child cleanup. Failed images do not disable subsequent OCR.
- `recall ocr` reports system OCR availability and recognition languages as JSON without installing anything.
- The installer installs Tesseract and English data through Omarchy. Existing indexes, models and folders are preserved; already-indexed files are not automatically re-OCRed.
- The native `recall` CLI, bundled agent skill, QML panel, CPU fallback and Vulkan acceleration remain available on x86_64 and aarch64. OCR itself runs on CPU.

Old optional OCR environments are no longer used; they are left on disk for users to remove when ready. Recognition quality can differ from PaddleOCR, particularly on photos and complex layouts; the synthetic text fixture is a regression check, not an accuracy benchmark.

Install: `curl -fsSL https://ferdousbhai.com/recall/install.sh | bash`

The Omarchy package submission remains subject to maintainer approval and publication. Based on pdstn12/Recall (MIT), inspired by ARahim3/DigUp.
