"""Recall's OCR helper: PaddleOCR's models (PP-OCR) on ONNX Runtime, through RapidOCR.

Protocol: one JSON object per line on stdin, one JSON reply per line on stdout. Logs go to stderr.
The first line the helper prints is {"ready": true, "model": "...", "languages": [...]} once the models are loaded.

  {"path": "/tmp/shot.png"}   ->   {"text": "line one\nline two"}   (or {"text": "", "error": "..."})
  {"path": "/tmp/shot.png", "boxes": true}   ->   also "lines": [{"text": "line one", "box": [x0, y0, x1, y1]}, ...]
                                                   (in pixels: where the app marks the words that matched)

Lines come top to bottom, as the detector finds them. The helper exits when stdin closes.

Models: PP-OCRv6 when ONNX Runtime has CUDA, else PP-OCRv5 mobile (on a laptop CPU, 2.3 s for a 1440x900 screenshot
against 3.3 s for v6, reading the same lines; 2026-10-10). $RECALL_OCR_MODEL=v5|v6 picks one by hand.
"""

import json
import os
import sys
import time


def log(*parts):
    print("[ocr]", *parts, file=sys.stderr, flush=True)


def engine():
    import onnxruntime
    from rapidocr import ModelType, OCRVersion, RapidOCR

    cuda = "CUDAExecutionProvider" in onnxruntime.get_available_providers()
    model = os.environ.get("RECALL_OCR_MODEL") or ("v6" if cuda else "v5")
    params = {
        "Global.log_level": "error",   # not every model it loads, nor a warning for each picture without text
        "EngineConfig.onnxruntime.intra_op_num_threads": max(2, (os.cpu_count() or 4) // 2),
    }
    if cuda:
        params["EngineConfig.onnxruntime.use_cuda"] = True
    if model == "v6":
        params.update({"Det.ocr_version": OCRVersion.PPOCRV6, "Rec.ocr_version": OCRVersion.PPOCRV6})
    else:
        params.update({"Det.ocr_version": OCRVersion.PPOCRV5, "Rec.ocr_version": OCRVersion.PPOCRV5,
                       "Det.model_type": ModelType.MOBILE, "Rec.model_type": ModelType.MOBILE})
    return RapidOCR(params=params), f"PP-OCR{model} ({'CUDA' if cuda else 'CPU'})"


def main():
    started = time.time()
    try:
        ocr, model = engine()
    except Exception as error:  # noqa: BLE001 - anything that stops the models from loading
        print(json.dumps({"ready": False, "error": str(error)[:500]}), flush=True)
        return 1
    # PP-OCR's recognizer reads Latin script and Chinese; Arabic would need its own model.
    print(json.dumps({"ready": True, "model": model, "languages": ["en", "zh"]}), flush=True)
    log(f"{model} ready in {time.time() - started:.1f} s")
    for line in sys.stdin:
        try:
            request = json.loads(line)
            result = ocr(request["path"])
            reply = {"text": "\n".join(result.txts or [])}
            if request.get("boxes"):
                reply["lines"] = [{"text": text, "box": [float(min(p[0] for p in box)), float(min(p[1] for p in box)),
                                                         float(max(p[0] for p in box)), float(max(p[1] for p in box))]}
                                  for text, box in zip(result.txts or [], result.boxes if result.boxes is not None
                                                       else [])]
        except Exception as error:  # noqa: BLE001 - one bad picture must not end the helper
            reply = {"text": "", "error": str(error)[:300]}
        print(json.dumps(reply, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
