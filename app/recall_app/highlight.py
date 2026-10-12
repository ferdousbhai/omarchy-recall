"""Where a result matched, marked on its picture: the words on a PDF page (from poppler's word boxes), and the text
on a screenshot or a scanned page (from OCR's line boxes), painted over in yellow, and the sentence or line that
matched best by meaning in an amber band. The picture is cut to the stretch around the first match when that helps
reading.

Everything here runs on a worker thread (previews.picture): it returns a PNG path and how many places it marked.
"""

import hashlib
import json
import os
import re
import subprocess
import unicodedata
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from PIL import Image, ImageDraw

from . import paths

FOLDER = paths.CACHE / "marked"
STOPWORDS = {"a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "and", "or", "my", "me", "that", "this",
             "these", "those", "from", "about", "where", "what", "which", "who", "when", "is", "was", "are", "were",
             "be", "it", "its", "some", "any", "find", "show", "file", "files", "photo", "picture", "image", "did",
             "screenshot", "video", "pdf", "document", "recording", "how", "why", "they", "we", "our", "their",
             "does", "do", "has", "have", "must", "should", "can", "code"}
YELLOW = (255, 214, 10, 120)
EDGE = (232, 160, 0, 255)


def fold(text):
    """Lowercase, without accents, as the keyword search sees words."""
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def terms(query, found=()):
    """The words worth marking: the ones the keyword search found, and the query's own (not the little ones)."""
    words = set()
    for word in list(found) + re.findall(r"[\w\-.]+", query.replace("code:", " ")):
        word = fold(word).strip(".-")
        if len(word) >= 3 and word not in STOPWORDS or any(ch.isdigit() for ch in word) and len(word) >= 2:
            words.add(word)
    return words


def matches(word, wanted):
    """A word on the page matches a query word when both start with the same five letters (contract/contracts,
    terminate/termination), or they're the same word, number or code."""
    word = fold(word).strip(".,;:!?()[]\"'«»“”")
    if not word:
        return False
    for target in wanted:
        if word == target:
            return True
        if len(target) >= 5 and len(word) >= 5 and word[:5] == target[:5]:
            return True
        if "-" in target and target in word:
            return True
    return False


def _key(*parts):
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0


# MARK: Sentences

def sentences(words):
    """A page's words, in reading order, as sentences: [(text, first word, last word + 1)]. A heading ("4. Term and
    termination") goes with the sentence after it, so it's never a match of its own."""
    units, start = [], 0
    for index, (text, *_box) in enumerate(words):
        last = index == len(words) - 1
        if last or text.endswith((".", "!", "?", ";", ":")) and index - start >= 5 or index - start >= 60:
            units.append((" ".join(w[0] for w in words[start:index + 1]), start, index + 1))
            start = index + 1
    return [unit for unit in units if len(unit[0]) >= 12]


def pdf_units(path, page):
    """The sentences of a PDF page, for ranking against the query; empty for a scan (its lines come from OCR)."""
    words, _, _ = _pdf_words(path, page)
    return [text for text, _, _ in sentences(words)]


def _scan(path, page, side=1100):
    """A scanned page rendered once and kept, so its OCR lines (and their order) are the same each time."""
    FOLDER.mkdir(parents=True, exist_ok=True)
    out = FOLDER / f"{_key(path, _mtime(path), page, side, 'scan')}"
    if not Path(str(out) + ".png").exists():
        subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-png", "-scale-to", str(side), "-singlefile",
                        path, str(out)], stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
    return str(out) + ".png"


def pdf_units_scanned(path, page):
    return [line["text"] for line in ocr_lines(_scan(path, page))]


def picture_units(path):
    return [line["text"] for line in ocr_lines(path)]


# MARK: Pages

def pdf_page(path, page, wanted, side=1100, unit=None):
    """The page with the wanted words marked, and the `unit`-th sentence (the one that matched best by meaning):
    (png path, marks) or None."""
    FOLDER.mkdir(parents=True, exist_ok=True)
    key = _key(path, _mtime(path), page, sorted(wanted), side, unit)
    out = FOLDER / f"{key}.png"
    meta = FOLDER / f"{key}.json"
    if out.exists() and meta.exists():
        return str(out), json.loads(meta.read_text())["marks"]
    base = FOLDER / f"{key}-page"
    subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-png", "-scale-to", str(side), "-singlefile",
                    path, str(base)], stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
    rendered = Path(str(base) + ".png")
    if not rendered.exists():
        return None
    image = Image.open(rendered).convert("RGBA")
    boxes, band = [], []
    words, width, height = _pdf_words(path, page)
    if words and width:
        scale = image.width / width
        boxes = [(x0 * scale, y0 * scale, x1 * scale, y1 * scale) for text, x0, y0, x1, y1 in words
                 if matches(text, wanted)]
        units = sentences(words)
        if unit is not None and unit < len(units):
            _, first, last = units[unit]
            band = [(x0 * scale, y0 * scale, x1 * scale, y1 * scale) for _, x0, y0, x1, y1 in words[first:last]]
    else:
        # A scan: its words come from OCR, by lines (of the same rendering every time, so lines keep their numbers).
        scan = _scan(path, page, side)
        lines = ocr_lines(scan)
        boxes = ocr_boxes(scan, wanted)
        if unit is not None and unit < len(lines):
            band = [tuple(lines[unit]["box"])]
    marked = _mark(image, boxes, band)
    marked = _crop(marked, band + boxes)
    marked.convert("RGB").save(out)
    rendered.unlink(missing_ok=True)
    meta.write_text(json.dumps({"marks": len(boxes)}))
    return str(out), len(boxes)


def _pdf_words(path, page):
    """Poppler's words on a page with their boxes, in points: ([(text, x0, y0, x1, y1)], width, height)."""
    try:
        result = subprocess.run(["pdftotext", "-bbox", "-f", str(page), "-l", str(page), path, "-"],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
        root = ElementTree.fromstring(re.sub(r"<!DOCTYPE[^>]*>", "", result.stdout.decode("utf-8", "replace")))
    except (subprocess.SubprocessError, ElementTree.ParseError, OSError):
        return [], 0, 0
    words, width, height = [], 0, 0
    for element in root.iter():
        tag = element.tag.split("}")[-1]
        if tag == "page":
            width, height = float(element.get("width", 0)), float(element.get("height", 0))
        elif tag == "word" and element.text:
            words.append((element.text, float(element.get("xMin")), float(element.get("yMin")),
                          float(element.get("xMax")), float(element.get("yMax"))))
    return words, width, height


# MARK: Screenshots and scans

def picture(path, wanted, side=1100, unit=None):
    """A screenshot with the wanted words marked (from OCR), and its `unit`-th line (the best match by meaning):
    (png path, marks) or None when nothing's marked."""
    FOLDER.mkdir(parents=True, exist_ok=True)
    key = _key(path, _mtime(path), sorted(wanted), side, "picture", unit)
    out = FOLDER / f"{key}.png"
    meta = FOLDER / f"{key}.json"
    if out.exists() and meta.exists():
        marks = json.loads(meta.read_text())["marks"]
        return (str(out), marks) if marks else None
    image = Image.open(path)
    boxes = ocr_boxes(path, wanted)
    lines = ocr_lines(path)
    band = [tuple(lines[unit]["box"])] if unit is not None and unit < len(lines) else []
    meta.write_text(json.dumps({"marks": len(boxes) + len(band)}))
    if not boxes and not band:
        return None
    image = image.convert("RGBA")
    marked = _mark(image, boxes, band)
    if max(marked.size) > side:
        ratio = side / max(marked.size)
        marked = marked.resize((int(marked.width * ratio), int(marked.height * ratio)), Image.LANCZOS)
    marked.convert("RGB").save(out)
    return str(out), len(boxes)


_ocr_lines = {}


def ocr_lines(path):
    """OCR's lines of a picture with their boxes, asked of recall-ocr (cached on disk: it takes a second or two)."""
    key = _key(path, _mtime(path), "ocr")
    cache = FOLDER / f"{key}.ocr.json"
    if cache.exists():
        return json.loads(cache.read_text())
    helper = os.environ.get("RECALL_OCR") or str(paths.DATA / "ocr/bin/recall-ocr")
    if not os.access(helper, os.X_OK):
        return []
    try:
        result = subprocess.run([helper], input=json.dumps({"path": path, "boxes": True}) + "\n",
                                capture_output=True, text=True, timeout=120, check=False)
    except (subprocess.SubprocessError, OSError):
        return []
    lines = []
    for line in result.stdout.splitlines():
        try:
            reply = json.loads(line)
        except ValueError:
            continue
        if "lines" in reply:
            lines = reply["lines"]
    FOLDER.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(lines))
    return lines


def ocr_boxes(path, wanted):
    """Boxes around the wanted words in OCR's lines: a word's place in its line is read from its share of the
    line's characters (close enough to mark it)."""
    boxes = []
    for line in ocr_lines(path):
        text, (x0, y0, x1, y1) = line["text"], line["box"]
        if not text:
            continue
        per_char = (x1 - x0) / max(len(text), 1)
        for found in re.finditer(r"\S+", text):
            if matches(found.group(), wanted):
                boxes.append((x0 + per_char * found.start(), y0, x0 + per_char * found.end(), y1))
    return boxes


# MARK: Drawing

BAND = (242, 162, 42, 70)


def _mark(image, boxes, band=()):
    """The sentence that matched as a soft amber band (one rectangle per line), the words as yellow marks."""
    if not boxes and not band:
        return image
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    pad = max(2, image.width // 400)
    for x0, y0, x1, y1 in _lines(band):
        draw.rectangle([x0 - pad * 2, y0 - pad, x1 + pad * 2, y1 + pad], fill=BAND)
        draw.rectangle([x0 - pad * 4, y0 - pad, x0 - pad * 3, y1 + pad], fill=EDGE)
    for x0, y0, x1, y1 in boxes:
        draw.rounded_rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], radius=pad * 2, fill=YELLOW,
                               outline=EDGE, width=max(1, pad // 2))
    return Image.alpha_composite(image, layer)


def _lines(boxes):
    """Word boxes merged into one box per line (words whose middles are at the same height)."""
    lines = []
    for x0, y0, x1, y1 in sorted(boxes, key=lambda box: (round((box[1] + box[3]) / 2 / 4), box[0])):
        middle = (y0 + y1) / 2
        if lines and abs((lines[-1][1] + lines[-1][3]) / 2 - middle) < (y1 - y0) * 0.6:
            a0, b0, a1, b1 = lines[-1]
            lines[-1] = (min(a0, x0), min(b0, y0), max(a1, x1), max(b1, y1))
        else:
            lines.append((x0, y0, x1, y1))
    return lines


def _crop(image, boxes, aspect=1.9):
    """The band of the page around the first match, as wide as the page (the whole page when it already fits)."""
    width, height = image.size
    band = int(width / aspect)
    if not boxes or height <= band:
        return image
    first = min(boxes, key=lambda box: box[1])
    top = int(max(0, min(height - band, first[1] - band * 0.3)))
    return image.crop((0, top, width, top + band))
