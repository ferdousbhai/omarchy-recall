"""Search results as the windows show them: a row per hit, with its place in the file
("p. 4", "0:06", "L12–40"), why it matched, and the groups the panel shows them in."""

import os
import re
import time
from dataclasses import dataclass

from . import paths

KIND_TITLES = {"screenshot": "Screenshot", "image": "Image", "pdf": "PDF", "doc": "Document", "audio": "Audio",
               "video": "Video", "code": "Code"}

# The panel's groups, in order.
GROUPS = [("screenshots", "Screenshots", {"screenshot"}, "camera-photo-symbolic"),
          ("images", "Images", {"image"}, "image-x-generic-symbolic"),
          ("documents", "Documents", {"pdf", "doc"}, "x-office-document-symbolic"),
          ("video", "Video", {"video"}, "video-x-generic-symbolic"),
          ("audio", "Audio", {"audio"}, "audio-x-generic-symbolic"),
          ("code", "Code", {"code"}, "text-x-script-symbolic")]

LANGUAGES = {"py": "Python", "swift": "Swift", "js": "JavaScript", "ts": "TypeScript", "tsx": "TypeScript",
             "go": "Go", "rs": "Rust", "c": "C", "h": "C header", "cpp": "C++", "cc": "C++", "java": "Java",
             "kt": "Kotlin", "rb": "Ruby", "php": "PHP", "cs": "C#", "sh": "Shell", "ipynb": "Notebook", "md": "Markdown"}


def clock(seconds):
    total = int(seconds)
    if total >= 3600:
        return f"{total // 3600}:{total // 60 % 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


def rough_bytes(size):
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}".replace(".0 ", " ")
        size /= 1000
    return ""


@dataclass
class ResultRow:
    path: str
    kind: str
    segment: str
    segment_id: int
    score: float
    z: float
    keyword_rank: int
    loc: float
    loc_end: float
    focus: float
    excerpt: str
    modified: float
    size: int
    info: dict
    code: bool = False

    @classmethod
    def from_hit(cls, hit, code=False):
        return cls(path=hit["path"], kind=hit["kind"], segment=hit["segment"], segment_id=hit.get("segment_id", 0),
                   score=hit.get("score", 0), z=hit.get("z"), keyword_rank=hit.get("keyword_rank"),
                   loc=hit.get("loc"), loc_end=hit.get("loc_end"), focus=hit.get("focus"),
                   excerpt=hit.get("excerpt") or "", modified=hit.get("modified", 0), size=hit.get("size", 0),
                   info=hit.get("info") or {}, code=code)

    @property
    def id(self):
        return self.path

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def folder(self):
        if self.code and self.info.get("repo"):
            repo = self.info["repo"]
            parent = os.path.dirname(repo.rstrip("/"))
            return os.path.relpath(os.path.dirname(self.path), parent)
        return paths.tilde(os.path.dirname(self.path))

    @property
    def matched_meaning(self):
        return self.z is not None and self.z >= 2

    @property
    def matched_words(self):
        return self.keyword_rank is not None

    @property
    def page(self):
        return int(self.loc) if self.segment in ("page", "page_image") and self.loc else None

    @property
    def moment(self):
        return self.loc if self.segment in ("frame", "audio") and self.loc is not None else None

    @property
    def code_line(self):
        return int(self.focus or self.loc or 1)

    @property
    def place(self):
        """The badge: where in the file the match is."""
        if self.segment in ("page", "page_image") and self.loc:
            return f"p. {int(self.loc)}"
        if self.segment == "frame" and self.loc is not None:
            return clock(self.loc)
        if self.segment == "audio" and self.loc is not None:
            return ("♪ " if self.kind == "video" else "") + clock(self.loc)
        if self.segment == "lines" and self.loc:
            end = int(self.loc_end or self.loc)
            return f"L{int(self.loc)}–{end}" if end != int(self.loc) else f"L{int(self.loc)}"
        if self.segment == "cells" and self.loc:
            end = int(self.loc_end or self.loc)
            return f"cell {int(self.loc)}" if end == int(self.loc) else f"cells {int(self.loc)}–{end}"
        return ""

    @property
    def reason(self):
        """Why it matched, in words."""
        if not self.matched_meaning:
            return "Matches its name" if self.segment == "name" else "Has your words"
        segment, kind = self.segment, self.kind
        if segment == "image":
            return "Matches what's on screen" if kind == "screenshot" else "Matches the picture"
        if segment == "ocr":
            return "Matches the text in it"
        if segment == "frame":
            return f"Matches the frame at {clock(self.loc or 0)}"
        if segment == "audio":
            return f"Matches the sound at {clock(self.loc or 0)}" if kind == "video" \
                else f"Matches what's heard at {clock(self.loc or 0)}"
        if segment == "page":
            return f"Matches the text on page {int(self.loc or 1)}"
        if segment == "page_image":
            return f"Matches page {int(self.loc or 1)}"
        if segment == "chunk":
            return "Matches its text"
        if segment in ("lines", "cells"):
            return f"Matches {self.place.replace('L', 'lines ', 1) if segment == 'lines' else self.place}"
        return "Matches its name"

    @property
    def details(self):
        """"Kind · date · W × H · N pages · duration · size"."""
        info = self.info
        parts = []
        if self.code:
            extension = os.path.splitext(self.path)[1].lstrip(".").lower()
            parts.append(LANGUAGES.get(extension, extension.upper() or "Code"))
            if info.get("lines"):
                parts.append(f"{info['lines']} lines")
        else:
            parts.append(KIND_TITLES.get(self.kind, self.kind))
        if self.modified:
            parts.append(time.strftime("%-d %b %Y", time.localtime(self.modified)))
        if info.get("width") and info.get("height"):
            parts.append(f"{info['width']} × {info['height']}")
        if info.get("pages"):
            pages = f"{info['pages']} page" + ("" if str(info["pages"]) == "1" else "s")
            if info.get("read_to") and info.get("read_end") and info["read_to"] != info["read_end"]:
                pages += f", {info['read_to']} read so far"
            parts.append(pages)
        if info.get("duration"):
            parts.append(clock(float(info["duration"])))
        if self.size:
            parts.append(rough_bytes(self.size))
        return " · ".join(parts)

    @property
    def found_words(self):
        """The words the keyword search marked in the excerpt («…»)."""
        return re.findall(r"«([^»]+)»", self.excerpt or "")


class ResultSet:
    """Hits as the panel shows them: the strong ones in groups, the rest folded under "weaker matches"."""

    def __init__(self, reply, rows_per_group=6):
        code = reply.get("code", False)
        rows = [ResultRow.from_hit(hit, code) for hit in reply.get("hits", [])]
        strong = reply.get("strong", len(rows))
        self.code = code
        self.meaning = reply.get("meaning", False)
        self.ranked = rows[:strong]
        self.weaker = rows[strong:]
        self.groups = []   # (key, title, icon, rows, total)
        order = []
        for row in self.ranked:
            for key, title, kinds, icon in GROUPS:
                if (row.kind in kinds or (code and key == "code")) and key not in order:
                    order.append(key)
        for key in order:
            _, title, kinds, icon = next(group for group in GROUPS if group[0] == key)
            members = [row for row in self.ranked if row.kind in kinds or (code and key == "code")]
            limit = rows_per_group * (3 if code else 1)
            self.groups.append((key, title, icon, members[:limit], len(members)))

    @property
    def empty(self):
        return not self.ranked and not self.weaker
