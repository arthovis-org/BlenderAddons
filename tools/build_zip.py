#!/usr/bin/env python3
"""Zip each add-on folder of this repo into an installable Blender add-on / extension.

Standard library only. Run from anywhere::

    python tools/build_zip.py                          # every add-on -> ./dist/<name>.zip
    python tools/build_zip.py --addon figma_to_blender # just one
    python tools/build_zip.py --out-dir build          # another output folder

An *add-on* is any top-level folder that contains a ``blender_manifest.toml``
or an ``__init__.py`` declaring ``bl_info``. Each archive contains the folder
itself (with ``blender_manifest.toml`` inside it), which is what both *Install
from Disk* (Blender 4.2+ extensions) and the legacy *Add-ons > Install...*
expect. ``__pycache__``, ``tests/`` and ``docs/`` folders, ``.pyc`` files and
``README.md`` at the add-on root are skipped: they are for the repo, not the
installed add-on.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT_DIR = os.path.join(ROOT, "dist")
EXCLUDE_DIRS = {"__pycache__", "tests", "docs"}
EXCLUDE_ROOT_FILES = {"README.md"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo")
BL_INFO_RE = re.compile(r"^\s*bl_info\s*=", re.MULTILINE)


def is_addon_dir(path: str) -> bool:
    """A folder is an add-on when it has a manifest or an ``__init__.py`` with ``bl_info``."""
    if not os.path.isdir(path) or os.path.basename(path).startswith("."):
        return False
    if os.path.isfile(os.path.join(path, "blender_manifest.toml")):
        return True
    init = os.path.join(path, "__init__.py")
    if not os.path.isfile(init):
        return False
    with open(init, "r", encoding="utf-8", errors="replace") as fh:
        return bool(BL_INFO_RE.search(fh.read()))


def discover_addons(root: str = ROOT):
    return sorted(
        name for name in os.listdir(root) if is_addon_dir(os.path.join(root, name))
    )


def collect_files(package_dir: str):
    for dirpath, dirnames, filenames in os.walk(package_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        at_root = os.path.abspath(dirpath) == os.path.abspath(package_dir)
        for fn in sorted(filenames):
            if fn.endswith(EXCLUDE_SUFFIXES) or (at_root and fn in EXCLUDE_ROOT_FILES):
                continue
            full = os.path.join(dirpath, fn)
            yield full, os.path.relpath(full, os.path.dirname(package_dir))


def build_zip(package_dir: str, out_path: str) -> int:
    """Zip ``package_dir`` into ``out_path``; returns the number of files written."""
    if not is_addon_dir(package_dir):
        raise SystemExit("not an add-on folder: %s" % package_dir)
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    count = 0
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for full, arcname in collect_files(package_dir):
            zf.write(full, arcname.replace(os.sep, "/"))
            count += 1
    return count


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--addon",
        action="append",
        metavar="NAME",
        help="add-on folder to zip (repeatable); default: every add-on folder in the repo",
    )
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="output folder (default ./dist)")
    p.add_argument("--list", action="store_true", help="only print the add-on folders found")
    args = p.parse_args(argv)

    names = args.addon or discover_addons()
    if args.list:
        print("\n".join(names))
        return 0
    if not names:
        raise SystemExit("no add-on folders found in %s" % ROOT)
    for name in names:
        out = os.path.join(args.out_dir, name + ".zip")
        n = build_zip(os.path.join(ROOT, name), out)
        print("wrote %s (%d files, %d bytes)" % (out, n, os.path.getsize(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
