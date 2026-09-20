"""Standalone exporter: Figma file + page (or a single frame) -> offline scene bundle.

Runs with plain CPython (no ``bpy``)::

    python -m figma_to_blender.cli --token XXX --file <url|key> --list-pages
    python -m figma_to_blender.cli --file <url|key> --page "Page 1" --list-frames
    python -m figma_to_blender.cli --file <url|key> --page "Page 1" --out ./bundle
    python -m figma_to_blender.cli --file <url|key> --node 12:345 --out ./bundle
    python -m figma_to_blender.cli --file <url|key> --node "https://www.figma.com/design/<key>/x?node-id=12-345"

The token may also come from the ``FIGMA_TOKEN`` environment variable.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .figma_api import FigmaClient, FigmaError, parse_file_key, parse_node_id
from .scene_model import ExportOptions, export_bundle


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m figma_to_blender.cli",
        description="Export a Figma page, or a single frame / node, to a scene bundle (scene.json + assets/) for the Blender add-on.",
    )
    p.add_argument("--token", default=os.environ.get("FIGMA_TOKEN"), help="Figma personal access token (or FIGMA_TOKEN env var)")
    p.add_argument("--file", required=True, help="Figma file URL or bare file key")
    target = p.add_mutually_exclusive_group()
    target.add_argument("--page", help="Page name (or page node id such as 0:1)")
    target.add_argument(
        "--node",
        help="Import a single frame / node instead of a whole page: its id (12:345) or a Figma "
        "'Copy link to selection' URL (...?node-id=12-345). Its top-left corner lands at the origin",
    )
    p.add_argument("--out", default="./bundle", help="Output folder (default ./bundle)")
    p.add_argument("--icon-format", choices=["svg", "png"], default="svg")
    p.add_argument("--raster-scale", type=float, default=2.0, help="PNG export scale for images / raster icons")
    p.add_argument("--icon-max-size", type=float, default=128.0, help="Max px size for a vector group to count as an icon")
    p.add_argument("--list-pages", action="store_true", help="List the pages of the file and exit")
    p.add_argument("--list-frames", action="store_true", help="List the top-level frames of --page (id, type, name) and exit")
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

    if args.node:
        node_id = parse_node_id(args.node)
        if node_id is None:
            print("error: --node %r is not a node id (12:345) or a URL with node-id=12-345" % args.node, file=sys.stderr)
            return 2
    else:
        try:
            pages = client.list_pages(key)
        except FigmaError as e:
            print("error: %s" % e, file=sys.stderr)
            return 1

        if args.list_pages or not args.page:
            for pg in pages:
                print("%s\t%s" % (pg["id"], pg["name"]))
            if not args.list_pages:
                print("\nPick one with --page <name> (add --list-frames to see its frames), or use --node <id|url>", file=sys.stderr)
                return 2
            return 0

        page = next((pg for pg in pages if pg["name"] == args.page or pg["id"] == args.page), None)
        if page is None:
            print("error: page %r not found. Available: %s" % (args.page, ", ".join(p["name"] for p in pages)), file=sys.stderr)
            return 1

        if args.list_frames:
            try:
                frames = client.list_top_level_frames(key, page["id"])
            except FigmaError as e:
                print("error: %s" % e, file=sys.stderr)
                return 1
            for fr in frames:
                print("%s\t%s\t%s" % (fr["id"], fr["type"], fr["name"]))
            if not frames:
                print("(page has no top-level layers)", file=sys.stderr)
            return 0
        node_id = page["id"]

    options = ExportOptions(
        icon_max_size=args.icon_max_size,
        icon_format=args.icon_format,
        raster_scale=args.raster_scale,
    )
    try:
        scene = export_bundle(client, key, node_id, args.out, options)
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
