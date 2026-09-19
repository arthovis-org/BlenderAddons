"""Pure-Python tests for the Figma -> scene bundle conversion (no bpy needed)."""

import io
import json
import math
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from figma_to_blender import cli, figma_api, scene_model  # noqa: E402
from figma_to_blender.figma_api import FigmaClient, parse_file_key, sanitize_id  # noqa: E402
from figma_to_blender.scene_model import ExportOptions, build_scene, load_scene, write_scene  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "sample_page.json")


def load_page():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)["nodes"]["0:1"]["document"]


def by_id(scene, el_id):
    return next(e for e in scene.elements if e.id == el_id)


class ParseKeyTests(unittest.TestCase):
    def test_design_url(self):
        self.assertEqual(parse_file_key("https://www.figma.com/design/AbC123xyz/My-File?node-id=1-2&t=abc"), "AbC123xyz")

    def test_file_url(self):
        self.assertEqual(parse_file_key("https://www.figma.com/file/KEY987/Name"), "KEY987")

    def test_bare_key(self):
        self.assertEqual(parse_file_key("  KEY987 "), "KEY987")

    def test_bad_url(self):
        with self.assertRaises(ValueError):
            parse_file_key("https://www.figma.com/community/plugin/123")
        with self.assertRaises(ValueError):
            parse_file_key("")

    def test_sanitize(self):
        self.assertEqual(sanitize_id("1:23"), "1_23")
        self.assertEqual(sanitize_id("I12:3;45:6"), "I12_3_45_6")


class BuildSceneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scene = build_scene(load_page(), ExportOptions(), file_key="KEY")

    def test_counts_per_kind(self):
        counts = {}
        for e in self.scene.elements:
            counts[e.kind] = counts.get(e.kind, 0) + 1
        self.assertEqual(counts, {"group": 5, "rect": 4, "text": 5, "ellipse": 1, "image": 1, "icon": 5})
        self.assertEqual(self.scene.page_name, "Page 1")
        self.assertEqual(self.scene.file_key, "KEY")

    def test_draw_order_and_parenting(self):
        ids = [e.id for e in self.scene.elements]
        self.assertEqual(ids[0], "1:2")  # Card group first
        self.assertEqual(ids[1], "1:2:bg")  # its background right after
        self.assertLess(ids.index("1:3"), ids.index("1:4"))  # header before title
        self.assertEqual(by_id(self.scene, "1:2:bg").parent, "1:2")
        self.assertEqual(by_id(self.scene, "1:7").parent, "1:6")
        self.assertEqual(by_id(self.scene, "1:6").parent, "1:2")
        self.assertIsNone(by_id(self.scene, "1:2").parent)

    def test_hidden_nodes_skipped(self):
        self.assertFalse(any(e.id.startswith("1:16") for e in self.scene.elements))

    def test_frame_with_fill_becomes_background_and_recurses(self):
        bg = by_id(self.scene, "1:2:bg")
        self.assertEqual(bg.kind, "rect")
        self.assertEqual(bg.fill, [1.0, 1.0, 1.0, 1.0])
        self.assertEqual(bg.corner_radii, [16.0] * 4)
        self.assertEqual((bg.x, bg.y, bg.w, bg.h), (100.0, 200.0, 360.0, 480.0))
        # children exist
        self.assertEqual(by_id(self.scene, "1:4").kind, "text")

    def test_nested_translation(self):
        title = by_id(self.scene, "1:4")
        self.assertAlmostEqual(title.x, 124.0)
        self.assertAlmostEqual(title.y, 340.0)
        self.assertAlmostEqual(title.rotation, 0.0)
        label = by_id(self.scene, "1:12")  # Card(100,200) + Button(24,400) + (16,14)
        self.assertAlmostEqual(label.x, 140.0)
        self.assertAlmostEqual(label.y, 614.0)

    def test_rotated_nested_frame(self):
        badge = by_id(self.scene, "1:14")
        self.assertAlmostEqual(badge.x, 380.0)
        self.assertAlmostEqual(badge.y, 220.0)
        self.assertAlmostEqual(badge.rotation, 15.0, places=5)
        self.assertFalse(badge.flipped)
        # Text child at local (6, 4) inside the rotated badge
        txt = by_id(self.scene, "1:15")
        a = math.radians(15.0)
        ex = 380.0 + math.cos(a) * 6 + math.sin(a) * 4
        ey = 220.0 - math.sin(a) * 6 + math.cos(a) * 4
        self.assertAlmostEqual(txt.x, ex, places=5)
        self.assertAlmostEqual(txt.y, ey, places=5)
        self.assertAlmostEqual(txt.rotation, 15.0, places=5)

    def test_absolute_bounding_box_fallback(self):
        el = by_id(self.scene, "1:18")
        self.assertEqual((el.x, el.y, el.w, el.h), (520.0, 200.0, 200.0, 24.0))
        self.assertEqual(el.rotation, 0.0)

    def test_gradient_is_averaged_and_flagged(self):
        header = by_id(self.scene, "1:3")
        self.assertTrue(header.fill_approx)
        self.assertAlmostEqual(header.fill[0], 0.4)
        self.assertAlmostEqual(header.fill[1], 0.3)
        self.assertAlmostEqual(header.fill[2], 0.9)
        self.assertEqual(header.corner_radii, [16.0, 16.0, 0.0, 0.0])

    def test_ellipse_opacity(self):
        e = by_id(self.scene, "1:7")
        self.assertEqual(e.kind, "ellipse")
        self.assertAlmostEqual(e.opacity, 0.9)
        self.assertAlmostEqual(e.fill[2], 1.0)

    def test_image_fill(self):
        img = by_id(self.scene, "1:8")
        self.assertEqual(img.kind, "image")
        self.assertEqual(img.asset, "assets/image_1_8.png")
        self.assertEqual(img.asset_format, "png")
        self.assertEqual((img.x, img.y), (132.0, 468.0))

    def test_icon_detection(self):
        inst = by_id(self.scene, "1:9")
        self.assertEqual(inst.kind, "icon")
        self.assertEqual(inst.asset, "assets/icon_1_9.svg")
        # children of the icon are NOT recursed
        self.assertFalse(any(e.id in ("1:10", "1:22", "1:23") for e in self.scene.elements))
        self.assertEqual(by_id(self.scene, "1:13").kind, "icon")  # bare VECTOR
        self.assertEqual(by_id(self.scene, "1:17").kind, "icon")  # STAR
        # Big vector frame is > icon_max_size so it stays a group with icon children
        self.assertEqual(by_id(self.scene, "1:19").kind, "group")
        self.assertEqual(by_id(self.scene, "1:20").kind, "icon")
        self.assertEqual(by_id(self.scene, "1:21").parent, "1:19")

    def test_icon_max_size_option(self):
        scene = build_scene(load_page(), ExportOptions(icon_max_size=256, icon_format="png"))
        big = by_id(scene, "1:19")
        self.assertEqual(big.kind, "icon")
        self.assertEqual(big.asset, "assets/icon_1_19.png")
        self.assertFalse(any(e.id == "1:20" for e in scene.elements))

    def test_text_info(self):
        t = by_id(self.scene, "1:5").text
        self.assertEqual(t["characters"].splitlines()[0], "Imported straight from Figma.")
        self.assertEqual(t["fontSize"], 14.0)
        self.assertEqual(t["fontFamily"], "Inter")
        self.assertEqual(t["fontPostScriptName"], "Inter-Regular")
        self.assertEqual(t["fontWeight"], 400)
        self.assertEqual(t["textAlignHorizontal"], "CENTER")
        self.assertEqual(t["textAlignVertical"], "CENTER")
        self.assertEqual(t["lineHeightPx"], 20.0)
        self.assertAlmostEqual(t["letterSpacing"], 0.2)
        title = by_id(self.scene, "1:4").text
        self.assertEqual(title["textAutoResize"], "HEIGHT")
        self.assertEqual(by_id(self.scene, "1:4").fill[:3], [0x11 / 255] * 3)

    def test_bounds(self):
        b = self.scene.bounds
        self.assertAlmostEqual(b["x"], 100.0)
        self.assertAlmostEqual(b["y"], 200.0)
        self.assertGreaterEqual(b["x"] + b["w"], 720.0)  # big frame right edge
        self.assertGreaterEqual(b["y"] + b["h"], 680.0)  # card bottom edge

    def test_roundtrip_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_scene(self.scene, tmp)
            self.assertTrue(os.path.exists(path))
            loaded = load_scene(tmp)
        self.assertEqual(len(loaded.elements), len(self.scene.elements))
        for a, b in zip(loaded.elements, self.scene.elements):
            self.assertEqual(a.to_dict(), b.to_dict())
        self.assertEqual(loaded.page_name, "Page 1")
        self.assertEqual(loaded.options["icon_max_size"], 128.0)

    def test_unsupported_node_warns(self):
        page = {"id": "0:1", "name": "P", "type": "CANVAS", "children": [{"id": "5:1", "name": "Slice", "type": "SLICE"}]}
        scene = build_scene(page)
        self.assertEqual(scene.elements, [])
        self.assertIn("SLICE", scene.warnings[0])


class FakeClient:
    """Stands in for FigmaClient in export tests."""

    def __init__(self, missing=()):
        self.missing = set(missing)
        self.calls = []

    def export_images(self, file_key, node_ids, fmt="svg", scale=1.0, batch_size=40):
        self.calls.append((fmt, scale, list(node_ids)))
        return {nid: (None if nid in self.missing else "https://cdn.example/%s.%s" % (nid, fmt)) for nid in node_ids}

    def download(self, url):
        if url.endswith(".svg"):
            return b"<svg xmlns='http://www.w3.org/2000/svg'/>"
        return b"\x89PNG fake"


class ExportAssetsTests(unittest.TestCase):
    def test_assets_written_and_missing_skipped(self):
        scene = build_scene(load_page(), ExportOptions(raster_scale=3.0))
        client = FakeClient(missing={"1:17"})
        with tempfile.TemporaryDirectory() as tmp:
            warnings = scene_model.export_assets(client, "KEY", scene, tmp)
            files = sorted(os.listdir(os.path.join(tmp, "assets")))
            self.assertEqual(files, ["icon_1_13.svg", "icon_1_20.svg", "icon_1_21.svg", "icon_1_9.svg", "image_1_8.png"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("Rating", warnings[0])
        self.assertIsNone(by_id(scene, "1:17").asset)
        fmts = {c[0]: c for c in client.calls}
        self.assertEqual(fmts["png"][1], 3.0)
        self.assertEqual(fmts["svg"][1], 1.0)
        self.assertEqual(sorted(fmts["svg"][2]), ["1:13", "1:17", "1:20", "1:21", "1:9"])


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ClientTests(unittest.TestCase):
    def test_requires_token(self):
        with self.assertRaises(figma_api.FigmaError):
            FigmaClient("")

    def test_retry_on_429_then_success(self):
        client = FigmaClient("tok", max_retries=2)
        err = urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "0"}, io.BytesIO(b""))
        responses = [err, _Resp(json.dumps({"document": {"children": [{"id": "0:1", "type": "CANVAS", "name": "A"}]}}).encode())]

        def fake_urlopen(req, timeout=None, context=None):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            self.assertEqual(req.get_header("X-figma-token"), "tok")
            return r

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), mock.patch("time.sleep") as slp:
            pages = client.list_pages("KEY")
        self.assertEqual(pages, [{"id": "0:1", "name": "A"}])
        slp.assert_called_once()

    def test_http_error_raises_figma_error(self):
        client = FigmaClient("tok", max_retries=0)
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b'{"err":"bad token"}'))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(figma_api.FigmaError) as cm:
                client.get_file_meta("KEY")
        self.assertEqual(cm.exception.status, 403)

    def test_export_images_batches_and_survives_failure(self):
        client = FigmaClient("tok", max_retries=0)
        ids = ["n%d" % i for i in range(45)]
        calls = []

        def fake_get_json(path, params=None):
            calls.append(params["ids"].split(","))
            if len(calls) == 2:
                raise figma_api.FigmaError("boom", 500)
            return {"images": {nid: "https://x/%s" % nid for nid in calls[-1][:-1]}}  # last id in batch has no url

        with mock.patch.object(client, "_get_json", side_effect=fake_get_json):
            out = client.export_images("KEY", ids, fmt="png", scale=2.0)
        self.assertEqual([len(c) for c in calls], [40, 5])
        self.assertEqual(len(out), 45)
        self.assertIsNone(out["n39"])  # dropped by server
        self.assertTrue(all(out[i] is None for i in ids[40:]))  # failed batch
        self.assertEqual(out["n0"], "https://x/n0")


class CliTests(unittest.TestCase):
    def test_list_pages(self):
        with mock.patch.object(cli, "FigmaClient") as FC:
            FC.return_value.list_pages.return_value = [{"id": "0:1", "name": "Page 1"}]
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = cli.main(["--token", "t", "--file", "https://www.figma.com/design/KEY1/x", "--list-pages"])
        self.assertEqual(rc, 0)
        self.assertIn("0:1\tPage 1", buf.getvalue())
        FC.return_value.list_pages.assert_called_with("KEY1")

    def test_no_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cli.main(["--file", "KEY"]), 2)

    def test_export_writes_bundle(self):
        page = load_page()
        with mock.patch.object(cli, "FigmaClient") as FC, tempfile.TemporaryDirectory() as tmp:
            inst = FC.return_value
            inst.list_pages.return_value = [{"id": "0:1", "name": "Page 1"}]
            inst.get_page.return_value = page
            fake = FakeClient()
            inst.export_images.side_effect = fake.export_images
            inst.download.side_effect = fake.download
            rc = cli.main(["--token", "t", "--file", "KEY", "--page", "Page 1", "--out", tmp, "--icon-format", "png"])
            self.assertEqual(rc, 0)
            scene = load_scene(tmp)
            self.assertTrue(os.path.exists(os.path.join(tmp, "assets", "icon_1_9.png")))
        self.assertEqual(len(scene.by_kind("icon")), 5)


if __name__ == "__main__":
    unittest.main()
