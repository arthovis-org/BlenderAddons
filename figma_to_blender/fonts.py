"""Best-effort lookup of installed system fonts by family / PostScript name.

Strategy:

1. If ``fc-list`` (fontconfig) is available (Linux, many macOS installs) ask it
   for ``file|family|postscriptname|weight|style`` of every font.
2. Otherwise scan the standard font directories of the current platform and
   read the ``name`` and ``OS/2`` tables of every ``.ttf`` / ``.otf`` file with
   a tiny built-in parser.

Both paths feed the same in-memory index; lookups are cached per process.
No ``bpy`` import so the CLI can use it for diagnostics too.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

FONT_EXTS = (".ttf", ".otf", ".TTF", ".OTF")

_WEIGHT_WORDS = {
    "thin": 100,
    "hairline": 100,
    "extralight": 200,
    "ultralight": 200,
    "light": 300,
    "regular": 400,
    "normal": 400,
    "book": 400,
    "medium": 500,
    "semibold": 600,
    "demibold": 600,
    "bold": 700,
    "extrabold": 800,
    "ultrabold": 800,
    "black": 900,
    "heavy": 900,
}


@dataclass
class FontEntry:
    path: str
    family: str
    postscript: str
    weight: int
    italic: bool


def _norm(s: Optional[str]) -> str:
    return re.sub(r"[\s_-]+", "", (s or "")).lower()


def style_to_weight(style: str) -> int:
    s = _norm(style)
    best = 400
    for word, w in sorted(_WEIGHT_WORDS.items(), key=lambda kv: -len(kv[0])):
        if word in s:
            best = w
            break
    return best


def font_dirs() -> List[str]:
    dirs: List[str] = []
    if sys.platform.startswith("win"):
        windir = os.environ.get("WINDIR", r"C:\Windows")
        dirs.append(os.path.join(windir, "Fonts"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    elif sys.platform == "darwin":
        dirs += ["/System/Library/Fonts", "/Library/Fonts", os.path.expanduser("~/Library/Fonts")]
    else:
        dirs += [
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.expanduser("~/.fonts"),
            os.path.expanduser("~/.local/share/fonts"),
        ]
    return [d for d in dirs if os.path.isdir(d)]


# ---------------------------------------------------------------------------
# Minimal TrueType/OpenType name table reader
# ---------------------------------------------------------------------------


def _read_name_table(path: str) -> Optional[FontEntry]:
    try:
        with open(path, "rb") as fh:
            data = fh.read(1 << 20)  # name/OS2 tables are near the start
    except OSError:
        return None
    if len(data) < 12:
        return None
    tag = data[:4]
    offset = 0
    if tag == b"ttcf":  # collection: use the first font
        if len(data) < 16:
            return None
        offset = struct.unpack(">I", data[12:16])[0]
        if offset + 12 > len(data):
            return None
    try:
        num_tables = struct.unpack(">H", data[offset + 4 : offset + 6])[0]
    except struct.error:
        return None
    tables: Dict[bytes, tuple] = {}
    pos = offset + 12
    for _ in range(num_tables):
        if pos + 16 > len(data):
            break
        ttag, _cs, toff, tlen = struct.unpack(">4sIII", data[pos : pos + 16])
        tables[ttag] = (toff, tlen)
        pos += 16
    if b"name" not in tables:
        return None
    toff, tlen = tables[b"name"]
    if toff + 6 > len(data):
        return None
    _fmt, count, str_off = struct.unpack(">HHH", data[toff : toff + 6])
    names: Dict[int, str] = {}
    for i in range(count):
        rec = toff + 6 + i * 12
        if rec + 12 > len(data):
            break
        plat, enc, _lang, name_id, length, off = struct.unpack(">HHHHHH", data[rec : rec + 12])
        if name_id not in (1, 2, 4, 6, 16, 17):
            continue
        start = toff + str_off + off
        raw = data[start : start + length]
        try:
            if plat == 3 or (plat == 0):
                txt = raw.decode("utf-16-be", "ignore")
            else:
                txt = raw.decode("mac-roman", "ignore")
        except Exception:
            continue
        if name_id not in names or plat == 3:
            names[name_id] = txt
    family = names.get(16) or names.get(1) or ""
    subfamily = names.get(17) or names.get(2) or ""
    postscript = names.get(6) or ""
    weight = style_to_weight(subfamily)
    if b"OS/2" in tables:
        o, _l = tables[b"OS/2"]
        if o + 6 <= len(data):
            wc = struct.unpack(">H", data[o + 4 : o + 6])[0]
            if 1 <= wc <= 1000:
                weight = int(wc)
    italic = "italic" in subfamily.lower() or "oblique" in subfamily.lower()
    if not family and not postscript:
        return None
    return FontEntry(path=path, family=family, postscript=postscript, weight=weight, italic=italic)


def _scan_dirs() -> List[FontEntry]:
    entries: List[FontEntry] = []
    for d in font_dirs():
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if fn.endswith(FONT_EXTS):
                    e = _read_name_table(os.path.join(root, fn))
                    if e:
                        entries.append(e)
    return entries


def _fc_list() -> Optional[List[FontEntry]]:
    exe = shutil.which("fc-list")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--format", "%{file}|%{family}|%{postscriptname}|%{weight}|%{style}\n"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    entries: List[FontEntry] = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 5 or not parts[0].endswith(FONT_EXTS):
            continue
        path, family, ps, weight, style = parts[:5]
        family = family.split(",")[0]
        try:
            fcw = float(weight.split(",")[0]) if weight else 80.0
        except ValueError:
            fcw = 80.0
        # fontconfig weights: 0..210 where 80 = regular, 200 = bold
        w = _fc_weight_to_css(fcw)
        style_l = style.lower()
        entries.append(
            FontEntry(path=path, family=family, postscript=ps, weight=w, italic="italic" in style_l or "oblique" in style_l)
        )
    return entries


def _fc_weight_to_css(w: float) -> int:
    table = [(0, 100), (40, 200), (50, 300), (80, 400), (100, 500), (180, 600), (200, 700), (205, 800), (210, 900)]
    best = min(table, key=lambda t: abs(t[0] - w))
    return best[1]


_INDEX: Optional[List[FontEntry]] = None


def font_index(refresh: bool = False) -> List[FontEntry]:
    global _INDEX
    if _INDEX is None or refresh:
        entries = _fc_list()
        if not entries:
            entries = _scan_dirs()
        _INDEX = entries
        log.debug("Indexed %d system fonts", len(entries))
    return _INDEX


def find_font(
    family: Optional[str] = None,
    postscript_name: Optional[str] = None,
    weight: Optional[int] = None,
    italic: bool = False,
    index: Optional[List[FontEntry]] = None,
) -> Optional[str]:
    """Return a font file path or ``None``.

    Order of preference: exact PostScript name, then family + closest weight
    (+ italic match), then family only.
    """
    entries = index if index is not None else font_index()
    if not entries:
        return None
    if postscript_name:
        ps = _norm(postscript_name)
        for e in entries:
            if _norm(e.postscript) == ps:
                return e.path
        # Some fonts ship as "Family-Style" filenames
        for e in entries:
            if _norm(os.path.splitext(os.path.basename(e.path))[0]) == ps:
                return e.path
    if family:
        fam = _norm(family)
        cands = [e for e in entries if _norm(e.family) == fam]
        if not cands:
            cands = [e for e in entries if fam and fam in _norm(e.family)]
        if cands:
            target = int(weight or 400)

            def score(e: FontEntry) -> tuple:
                return (0 if e.italic == italic else 1, abs(e.weight - target))

            return min(cands, key=score).path
    return None
