"""Standalone exporter: Figma file + page -> offline scene bundle.

Runs with plain CPython (no ``bpy``)::

    python -m figma_to_blender.cli --token XXX --file <url|key> --list-pages
    python -m figma_to_blender.cli --file <url|key> --page "Page 1" --out ./bundle

The token may also come from the ``FIGMA_TOKEN`` environment variable.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .figma_api import FigmaClient, FigmaError, parse_file_key
from .scene_model import ExportOptions, export_bundle


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m figma_to_blender.cli",
        description="Export a Figma page to a scene bundle (scene.json + assets/) for the Blender add-on.",
    )
    p.add_argument("--token", default=os.environ.get("FIGMA_TOKEN"), help="Figma personal access token (or FIGMA_TOKEN env var)")
    p.add_argument("--file", required=True, help="Figma file URL or bare file key")
    p.add_argument("--page", help="Page name (or page node id such as 0:1)")
    p.add_argument("--out", default="./bundle", help="Output folder (default ./bundle)")
    p.add_argument("--icon-format", choices=["svg", "png"], default="svg")
    p.add_argument("--raster-scale", type=float, default=2.0, help="PNG export scale for images / raster icons")
    p.add_argument("--icon-max-size", type=float, default=128.0, help="Max px size for a vector group to count as an icon")
    p.add_argument("--list-pages", action="store_true", help="List the pages of the file and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    if not args.token:
        print("error: no token given (use --token or FIGMA_TOKEN)", file=sys.stderr)
        return 2
    try:
        key = parse_file_key(args.file)
    except ValueError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2

    client = FigmaClient(args.token)
    try:
        pages = client.list_pages(key)
    except FigmaError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1

    if args.list_pages or not args.page:
        for pg in pages:
            print("%s\t%s" % (pg["id"], pg["name"]))
        if not args.list_pages:
            print("\nPick one with --page <name>", file=sys.stderr)
            return 2
        return 0

    page = next((pg for pg in pages if pg["name"] == args.page or pg["id"] == args.page), None)
    if page is None:
        print("error: page %r not found. Available: %s" % (args.page, ", ".join(p["name"] for p in pages)), file=sys.stderr)
        return 1

    options = ExportOptions(
        icon_max_size=args.icon_max_size,
        icon_format=args.icon_format,
        raster_scale=args.raster_scale,
    )
    try:
        scene = export_bundle(client, key, page["id"], args.out, options)
    except FigmaError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1

    counts = {}
    for el in scene.elements:
        counts[el.kind] = counts.get(el.kind, 0) + 1
    print("Wrote %s" % os.path.join(args.out, "scene.json"))
    print("Elements: " + ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
    for w in scene.warnings:
        print("warning: %s" % w, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
