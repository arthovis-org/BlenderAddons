"""Best-effort, fully offline lookup of fonts by family / PostScript name.

Strategy:

1. A user *fonts folder* (``BuildOptions.fonts_dir`` / the add-on preference)
   is scanned recursively for ``.ttf`` / ``.otf`` files (``.woff`` / ``.woff2``
   are skipped: Blender cannot load them) and its fonts win over system fonts.
2. If ``fc-list`` (fontconfig) is available (Linux, many macOS installs) ask it
   for ``file|family|postscriptname|weight|style`` of every system font.
3. Otherwise scan the standard font directories of the current platform.

Font files are read with a tiny built-in ``name`` / ``OS/2`` table parser; a
file whose tables cannot be read is indexed from its file name
(``Family-Style.ttf``).  Every path feeds the same in-memory index, cached per
process (per folder for user folders).  Nothing is downloaded and no ``bpy``
is imported, so the CLI can use it for diagnostics too.
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
from typing import Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

FONT_EXTS = (".ttf", ".otf", ".TTF", ".OTF")
SKIPPED_EXTS = (".woff", ".woff2", ".WOFF", ".WOFF2")  # web fonts: Blender's font loader does not read them

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


_WEIGHT_NAMES = {
    100: "Thin",
    200: "Extra Light",
    300: "Light",
    400: "Regular",
    500: "Medium",
    600: "Semi Bold",
    700: "Bold",
    800: "Extra Bold",
    900: "Black",
}


def weight_style_name(weight: Optional[int], italic: bool = False) -> str:
    """``(700, True)`` -> ``"Bold Italic"``: the style name for a CSS weight (nearest hundred)."""
    try:
        w = int(float(weight or 400) / 100.0 + 0.5) * 100  # nearest hundred (650 -> 700)
    except (TypeError, ValueError):
        w = 400
    name = _WEIGHT_NAMES.get(max(100, min(900, w)), "Regular")
    if italic:
        name = "Italic" if name == "Regular" else name + " Italic"
    return name


def style_is_italic(style: str) -> bool:
    s = style.lower()
    return "italic" in s or "oblique" in s


def entry_from_filename(path: str) -> FontEntry:
    """Index a font from its file name alone (``Family-Style.ttf``), for files whose tables cannot be read."""
    stem = os.path.splitext(os.path.basename(path))[0]
    family, style = stem, ""
    for sep in ("-", "_"):
        if sep in stem:
            family, style = stem.rsplit(sep, 1)
            break
    # "OpenSans" -> "Open Sans" so it compares equal to Figma's family name after normalisation anyway
    family = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", family).strip()
    return FontEntry(path=path, family=family, postscript=stem, weight=style_to_weight(style), italic=style_is_italic(style))


def read_font_file(path: str) -> Optional[FontEntry]:
    """A :class:`FontEntry` for a ``.ttf`` / ``.otf`` file, from its tables or, failing that, its file name."""
    if not path.endswith(FONT_EXTS):
        return None
    return _read_name_table(path) or entry_from_filename(path)


def scan_dir(directory: str) -> List[FontEntry]:
    """Every font file under ``directory`` (recursive); web fonts are skipped with a debug log."""
    entries: List[FontEntry] = []
    if not directory or not os.path.isdir(directory):
        return entries
    for root, _dirs, files in os.walk(directory):
        for fn in sorted(files):
            full = os.path.join(root, fn)
            if fn.endswith(SKIPPED_EXTS):
                log.debug("Skipping web font %s (Blender cannot load woff/woff2)", full)
                continue
            e = read_font_file(full)
            if e:
                entries.append(e)
    return entries


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
        entries.extend(scan_dir(d))
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
_USER_INDEX: Dict[str, List[FontEntry]] = {}  # user fonts folder -> entries, cached per process


def system_font_index(refresh: bool = False) -> List[FontEntry]:
    global _INDEX
    if _INDEX is None or refresh:
        entries = _fc_list()
        if not entries:
            entries = _scan_dirs()
        _INDEX = entries
        log.debug("Indexed %d system fonts", len(entries))
    return _INDEX


def user_font_index(user_dirs: Optional[Sequence[str]], refresh: bool = False) -> List[FontEntry]:
    """Fonts of the user's folders (scanned once per folder per session; ``refresh`` rescans)."""
    entries: List[FontEntry] = []
    for d in user_dirs or ():
        if not d:
            continue
        d = os.path.abspath(os.path.expanduser(d))
        if refresh or d not in _USER_INDEX:
            _USER_INDEX[d] = scan_dir(d)
            log.debug("Indexed %d fonts in %s", len(_USER_INDEX[d]), d)
        entries.extend(_USER_INDEX[d])
    return entries


def font_index(refresh: bool = False, user_dirs: Optional[Sequence[str]] = None) -> List[FontEntry]:
    """User folder fonts first (they win ties), then the system fonts."""
    return user_font_index(user_dirs, refresh) + system_font_index(refresh)


def find_font(
    family: Optional[str] = None,
    postscript_name: Optional[str] = None,
    weight: Optional[int] = None,
    italic: bool = False,
    index: Optional[List[FontEntry]] = None,
    user_dirs: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Return a font file path or ``None``.

    Order of preference: exact PostScript name (also matched against the file
    name, ``Inter-SemiBold.otf``), then family + closest weight (+ italic
    match), then family only.  ``user_dirs`` are scanned in addition to the
    system fonts and take precedence.
    """
    entries = index if index is not None else font_index(user_dirs=user_dirs)
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
