#!/bin/bash
# Sets up Recall's OCR: PaddleOCR's PP-OCR models on ONNX Runtime, through RapidOCR, in a Python environment at
# ~/.local/share/recall/ocr (or $1), with `bin/recall-ocr` to run it. Downloads the models
# once, so indexing never goes online. Uses ONNX Runtime's CUDA build when there's an NVIDIA GPU. Needs uv.
set -euo pipefail
cd "$(dirname "$0")/.."
DEST=${1:-${XDG_DATA_HOME:-$HOME/.local/share}/recall/ocr}
command -v uv > /dev/null || { echo "needs uv: https://docs.astral.sh/uv/" >&2; exit 1; }
RUNTIME=onnxruntime
if command -v nvidia-smi > /dev/null && nvidia-smi -L > /dev/null 2>&1; then RUNTIME=onnxruntime-gpu; fi

uv venv --quiet --allow-existing --python 3.12 "$DEST"
uv pip install --quiet --python "$DEST/bin/python" "rapidocr>=3.4" "$RUNTIME"
cp ocr/recall_ocr.py "$DEST/recall_ocr.py"
cat > "$DEST/bin/recall-ocr" <<WRAPPER
#!/bin/sh
exec "$DEST/bin/python" -I "$DEST/recall_ocr.py" "\$@"
WRAPPER
chmod +x "$DEST/bin/recall-ocr"

# A first run downloads the models; a blank page keeps it quick.
"$DEST/bin/python" -I -c "
import numpy, sys
sys.path.insert(0, '$DEST')
import recall_ocr
ocr, model = recall_ocr.engine()
ocr(numpy.full((64, 256, 3), 255, numpy.uint8))
print('OCR ready:', model)
" 2> >(grep -v INFO >&2)
echo "Installed $DEST/bin/recall-ocr"
