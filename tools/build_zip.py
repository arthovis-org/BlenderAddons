#!/usr/bin/env python3
"""Zip the ``figma_to_blender/`` package into an installable Blender add-on / extension.

Standard library only::

    python tools/build_zip.py                 # -> ./figma_to_blender.zip
    python tools/build_zip.py --out dist/figma_to_blender.zip

The archive contains the ``figma_to_blender/`` folder itself (with
``blender_manifest.toml`` inside it), which is what both *Install from Disk*
(Blender 4.2+ extensions) and the legacy *Add-ons > Install...* expect.
``__pycache__`` folders and ``.pyc`` files are skipped.
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = "figma_to_blender"
EXCLUDE_DIRS = {"__pycache__"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo")


def collect_files(package_dir: str):
    for dirpath, dirnames, filenames in os.walk(package_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for fn in sorted(filenames):
            if fn.endswith(EXCLUDE_SUFFIXES):
                continue
            full = os.path.join(dirpath, fn)
            yield full, os.path.relpath(full, os.path.dirname(package_dir))


def build_zip(out_path: str, package_dir: str = os.path.join(ROOT, PACKAGE)) -> int:
    if not os.path.isfile(os.path.join(package_dir, "__init__.py")):
        raise SystemExit("package not found: %s" % package_dir)
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
    p.add_argument("--out", default=os.path.join(ROOT, PACKAGE + ".zip"), help="output zip path (default ./figma_to_blender.zip)")
    args = p.parse_args(argv)
    n = build_zip(args.out)
    print("wrote %s (%d files, %d bytes)" % (args.out, n, os.path.getsize(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
