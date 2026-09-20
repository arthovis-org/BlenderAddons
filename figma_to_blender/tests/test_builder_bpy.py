"""Builder tests. They run only when ``bpy`` is importable (Blender or the ``bpy`` wheel).

    pip install bpy   # Python 3.11 for the Blender 5.x bpy wheel
    python -m pytest figma_to_blender/tests/test_builder_bpy.py -q

Set ``FIGMA_RENDER_OUT=/path/render.png`` to also write a headless render of the
fixture page (proof-of-work screenshot).
"""

import json
import math
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

try:
    import bpy  # noqa: F401
    from mathutils import Matrix, Vector
except ImportError:  # pragma: no cover
    bpy = None

from figma_to_blender.scene_model import ExportOptions, build_scene, write_scene, load_scene  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "sample_page.json")
ASSETS = os.path.join(HERE, "fixtures", "assets")


def find_node(node, node_id):
    if node.get("id") == node_id:
        return node
    for child in node.get("children") or []:
        found = find_node(child, node_id)
        if found is not None:
            return found
    return None


def make_bundle(out_dir, icon_format="svg", font_override=None, root_id=None):
    """Write a bundle from the fixture using the tiny SVG/PNG as every asset.

    ``root_id`` builds the bundle from that single node (frame import) instead
    of the whole fixture page.
    """
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        page = json.load(fh)["nodes"]["0:1"]["document"]
    root = find_node(page, root_id) if root_id else page
    scene = build_scene(root, ExportOptions(icon_format=icon_format), file_key="FIXTURE")
    os.makedirs(os.path.join(out_dir, "assets"), exist_ok=True)
    for el in scene.elements:
        if not el.asset:
            continue
        src = os.path.join(ASSETS, "icon.svg" if el.asset.endswith(".svg") else "image.png")
        shutil.copy(src, os.path.join(out_dir, el.asset))
        if el.kind == "text" and font_override and el.id in font_override:
            el.text["fontFamily"] = font_override[el.id]
    if font_override:
        for el in scene.elements:
            if el.kind == "text" and el.id in font_override:
                el.text["fontFamily"], el.text["fontPostScriptName"] = font_override[el.id], None
    write_scene(scene, out_dir)
    return scene


def obj_bbox(objs):
    pts = [ob.matrix_world @ Vector(c) for ob in objs for c in ob.bound_box]
    return Vector(map(min, *pts)), Vector(map(max, *pts))


@unittest.skipIf(bpy is None, "bpy not importable")
class BuilderTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        self.tmp = tempfile.mkdtemp(prefix="figma_bundle_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build(self, icon_mode, orientation="XZ", center=False, icon_format=None, font_override=None, **extra):
        from figma_to_blender import builder

        fmt = icon_format or ("svg" if icon_mode == "SVG" else "png")
        make_bundle(self.tmp, icon_format=fmt, font_override=font_override)
        scene = load_scene(self.tmp)
        opts = builder.BuildOptions(
            scale=0.001, depth_step=0.0005, icon_mode=icon_mode, plane_orientation=orientation, center=center, **extra
        )
        report = builder.build_scene(scene, self.tmp, opts)
        return scene, report, opts

    def _by_name(self, report, name):
        return next(o for o in report.objects if o.name == name)

    def test_svg_mode_objects(self):
        scene, report, _ = self._build("SVG")
        self.assertIn("Page 1", bpy.data.collections)
        coll = bpy.data.collections["Page 1"]
        self.assertEqual(report.counts, {"group": 5, "rect": 7, "text": 5, "ellipse": 2, "image": 1, "icon": 5})
        fonts = [o for o in report.objects if o.type == "FONT"]
        meshes = [o for o in report.objects if o.type == "MESH"]
        curves = [o for o in report.objects if o.type == "CURVE"]
        empties = [o for o in report.objects if o.type == "EMPTY"]
        self.assertEqual(len(fonts), 5)
        self.assertEqual(len(meshes), 8)  # 7 rects + image plane (ellipses are curves)
        self.assertEqual(len(curves), 5 * 2 + 2)  # fixture SVG has 2 shapes, 5 icons, + 2 ellipse curves
        self.assertEqual(len(empties), 5 + 5)  # groups + icon roots
        for ob in report.objects:
            self.assertIn(coll, ob.users_collection)
            self.assertEqual(len(ob.users_collection), 1)
        # importer's temporary collections are gone
        self.assertFalse([c.name for c in bpy.data.collections if c.name.endswith(".svg")])
        # svg icon bbox width matches node width (Arrow is 24 px -> 0.024 m)
        arrow = self._by_name(report, "Arrow")
        children = [o for o in report.objects if o.parent == arrow]
        self.assertEqual(len(children), 2)
        lo, hi = obj_bbox(children)
        self.assertAlmostEqual(hi.x - lo.x, 0.024, places=4)
        self.assertAlmostEqual(hi.z - lo.z, 0.024, places=4)
        # positioned at Card(100,200)+Button(24,400)+(270,12) = (394, 612) top-left, 24x24
        self.assertAlmostEqual(lo.x, 0.394, places=4)
        self.assertAlmostEqual(hi.z, -0.612, places=4)
        # the importer's SVG fill colours are preserved
        mats = {m.name for o in children for m in o.data.materials}
        self.assertTrue(mats)
        # non-destructive: the icon is sized through the parent Empty's transform, the
        # curve data / child transforms are left exactly as the importer made them
        self.assertNotAlmostEqual(arrow.matrix_world.to_scale().x, 1.0, places=3)
        for child in children:
            self.assertEqual(tuple(child.scale), (1.0, 1.0, 1.0))
            self.assertEqual(child.matrix_parent_inverse, Matrix.Identity(4))

    def test_plane_mode_objects(self):
        scene, report, _ = self._build("PLANE")
        self.assertEqual(report.counts["icon"], 5)
        # no icon curves in PLANE mode (the ellipse curve is unrelated to icon mode)
        self.assertFalse([o for o in report.objects if o.type == "CURVE" and o.get("figma_kind") != "ellipse"])
        self.assertEqual(len([o for o in report.objects if o.type == "CURVE"]), 2)
        arrow = self._by_name(report, "Arrow")
        self.assertEqual(arrow.type, "MESH")
        lo, hi = obj_bbox([arrow])
        self.assertAlmostEqual(hi.x - lo.x, 0.024, places=6)
        self.assertAlmostEqual(lo.x, 0.394, places=6)
        self.assertAlmostEqual(hi.z, -0.612, places=6)
        mat = arrow.data.materials[0]
        tex = [n for n in mat.node_tree.nodes if n.type == "TEX_IMAGE"]
        self.assertEqual(len(tex), 1)
        self.assertIsNotNone(tex[0].image)
        self.assertEqual(tuple(tex[0].image.size), (4, 4))

    def test_positions_depth_and_parenting_xz(self):
        scene, report, opts = self._build("SVG")
        bg = self._by_name(report, "Card (background)")
        # centre of the 360x480 card at (100,200) -> (280, 440); depth index 1
        self.assertAlmostEqual(bg.location.x, 0.280, places=6)
        self.assertAlmostEqual(bg.location.z, -0.440, places=6)
        self.assertAlmostEqual(bg.location.y, -1 * 0.0005, places=6)
        self.assertEqual(bg.parent.name, "Card")
        self.assertEqual(bg["figma_id"], "1:2")
        self.assertEqual(bg["figma_type"], "FRAME")
        # later-drawn elements are closer to the viewer (more negative Y)
        header = self._by_name(report, "Header")
        self.assertLess(header.location.y, bg.location.y)
        photo = self._by_name(report, "Photo")
        self.assertEqual(photo.parent.name, "Avatar group")
        self.assertEqual(photo.parent.parent.name, "Card")
        self.assertAlmostEqual(photo.location.x, 0.156, places=6)  # 132 + 24
        self.assertAlmostEqual(photo.location.z, -0.492, places=6)  # 468 + 24
        # rotated badge: local X axis is rotated 15 degrees CCW in the XZ view
        badge = self._by_name(report, "Badge (background)")
        ax = badge.matrix_world.col[0].xyz.normalized()
        self.assertAlmostEqual(ax.x, math.cos(math.radians(15)), places=5)
        self.assertAlmostEqual(ax.z, math.sin(math.radians(15)), places=5)
        self.assertAlmostEqual(ax.y, 0.0, places=6)
        new_label = self._by_name(report, "New label")
        self.assertEqual(new_label.parent.name, "Badge")
        self.assertAlmostEqual(new_label.matrix_world.col[0].xyz.normalized().z, math.sin(math.radians(15)), places=5)
        # group empties sit at the Figma top-left
        card = self._by_name(report, "Card")
        self.assertEqual(card.type, "EMPTY")
        self.assertAlmostEqual(card.location.x, 0.100, places=6)
        self.assertAlmostEqual(card.location.z, -0.200, places=6)
        # collection metadata
        self.assertEqual(report.collection["figma_page_id"], "0:1")

    def test_positions_xy_and_centering(self):
        scene, report, opts = self._build("SVG", orientation="XY", center=True)
        b = scene.bounds
        cx, cy = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
        bg = self._by_name(report, "Card (background)")
        self.assertAlmostEqual(bg.location.x, (280 - cx) * 0.001, places=6)
        self.assertAlmostEqual(bg.location.y, -(440 - cy) * 0.001, places=6)
        self.assertAlmostEqual(bg.location.z, 0.0005, places=6)
        n = bg.matrix_world.col[2].xyz
        self.assertAlmostEqual(n.z, 1.0, places=6)
        badge = self._by_name(report, "Badge (background)")
        ax = badge.matrix_world.col[0].xyz
        self.assertAlmostEqual(ax.y, math.sin(math.radians(15)), places=5)

    def test_text_properties_and_fonts(self):
        scene, report, opts = self._build("SVG", font_override={"1:5": "DejaVu Sans"})
        title = self._by_name(report, "Title")
        cu = title.data
        self.assertEqual(cu.body, "Hello Blender")
        self.assertAlmostEqual(cu.size, 0.024, places=6)
        self.assertEqual(cu.align_x, "LEFT")
        self.assertEqual(cu.align_y, "TOP")
        self.assertAlmostEqual(cu.text_boxes[0].width, 0.312, places=6)
        self.assertAlmostEqual(cu.space_line, 32 / 24, places=6)
        self.assertAlmostEqual(title.location.x, 0.124, places=6)
        self.assertLess(title.location.z, -0.340)  # origin shifted down from the box top for the baseline
        self.assertGreater(title.location.z, -0.340 - 0.05)
        # the glyphs actually sit inside the Figma box (top 340px, bottom 372px)
        ev = title.evaluated_get(bpy.context.evaluated_depsgraph_get())
        pts = [title.matrix_world @ Vector(c) for c in ev.bound_box]
        self.assertLess(max(p.z for p in pts), -0.340 + 0.002)
        self.assertGreater(min(p.z for p in pts), -0.372 - 0.004)
        self.assertGreater(min(p.x for p in pts), 0.124 - 0.001)
        # Inter is not installed here -> recorded, custom property set
        self.assertIn("Inter", report.missing_fonts)
        self.assertEqual(title["figma_font"], "Inter")
        body = self._by_name(report, "Body")
        self.assertEqual(body.data.align_x, "CENTER")
        self.assertEqual(body.data.align_y, "CENTER")
        if any("DejaVu" in f.family for f in __import__("figma_to_blender.fonts", fromlist=["x"]).font_index()):
            self.assertIsNotNone(body.data.font)
            self.assertIn("DejaVu", body.data.font.filepath)
        label = self._by_name(report, "Label")
        mat = label.data.materials[0]
        self.assertAlmostEqual(mat.diffuse_color[0], 1.0, places=3)

    def test_materials(self):
        scene, report, _ = self._build("SVG")
        bg = self._by_name(report, "Card (background)")
        mat = bg.data.materials[0]
        self.assertEqual(tuple(round(c, 3) for c in mat.diffuse_color), (1.0, 1.0, 1.0, 1.0))
        em = [n for n in mat.node_tree.nodes if n.type == "EMISSION"]
        self.assertEqual(len(em), 1)
        avatar = self._by_name(report, "Avatar bg")
        amat = avatar.data.materials[0]
        self.assertAlmostEqual(amat.diffuse_color[3], 0.9, places=3)
        self.assertEqual(amat.surface_render_method, "BLENDED")
        self.assertFalse(amat.show_transparent_back)
        # materials are shared by colour
        label = self._by_name(report, "Label")  # white text shares the white card material
        self.assertIs(label.data.materials[0], bg.data.materials[0])
        button = self._by_name(report, "Button (background)")
        self.assertIsNot(button.data.materials[0], amat)  # same colour, different alpha
        for m in bpy.data.materials:
            if m.name.startswith("Figma"):
                self.assertTrue(m["figma_managed"])
        # the ellipse is a filled curve carrying the same flat material
        ellipse = self._by_name(report, "Avatar bg")
        self.assertEqual(ellipse.type, "CURVE")
        self.assertIs(ellipse.data.materials[0], amat)

    # -- non-destructive shapes ----------------------------------------------

    def _corner_modifier(self, ob):
        mods = [m for m in ob.modifiers if m.type == "BEVEL"]
        self.assertEqual(len(mods), 1, "expected exactly one Bevel modifier on %s" % ob.name)
        mod = mods[0]
        self.assertEqual(mod.name, "Corner Radius")
        self.assertEqual(mod.affect, "VERTICES")
        self.assertEqual(mod.limit_method, "WEIGHT")
        self.assertEqual(mod.offset_type, "OFFSET")
        return mod

    def _evaluated(self, ob):
        bpy.context.view_layer.update()
        return ob.evaluated_get(bpy.context.evaluated_depsgraph_get())

    def test_rect_is_plane_with_corner_radius_modifier(self):
        from figma_to_blender import builder

        scene, report, _ = self._build("SVG")
        header = self._by_name(report, "Header")
        # the mesh itself stays a sharp 4-vertex quad with UVs, object scale untouched
        self.assertEqual(header.type, "MESH")
        self.assertEqual(len(header.data.vertices), 4)
        self.assertEqual(len(header.data.polygons), 1)
        self.assertTrue(header.data.uv_layers)
        self.assertEqual(tuple(header.scale), (1.0, 1.0, 1.0))
        self.assertAlmostEqual(header.dimensions.x, 0.360, places=6)
        self.assertAlmostEqual(header.dimensions.y, 0.120, places=6)
        # vertex order is Figma's tl, tr, br, bl (local frame: x right, y up)
        co = [tuple(round(c, 6) for c in v.co) for v in header.data.vertices]
        self.assertEqual(co, [(-0.18, 0.06, 0.0), (0.18, 0.06, 0.0), (0.18, -0.06, 0.0), (-0.18, -0.06, 0.0)])
        # rectangleCornerRadii [16, 16, 0, 0] -> width 16 px, weights per corner
        mod = self._corner_modifier(header)
        self.assertAlmostEqual(mod.width, 0.016, places=6)
        self.assertEqual(mod.segments, 8)
        self.assertEqual(builder.vertex_bevel_weights(header.data), [1.0, 1.0, 0.0, 0.0])
        # the evaluated mesh has 2 rounded (segments + 1 verts) + 2 sharp corners and keeps its UVs and size
        ev = self._evaluated(header)
        self.assertEqual(len(ev.data.vertices), 2 * 9 + 2)
        self.assertEqual(len(ev.data.polygons), 1)
        self.assertTrue(ev.data.uv_layers)
        self.assertAlmostEqual(ev.dimensions.x, 0.360, places=6)
        # uniform cornerRadius -> every corner weight 1
        card = self._by_name(report, "Card (background)")
        mod = self._corner_modifier(card)
        self.assertAlmostEqual(mod.width, 0.016, places=6)
        self.assertEqual(builder.vertex_bevel_weights(card.data), [1.0] * 4)
        self.assertEqual(len(self._evaluated(card).data.vertices), 4 * 9)
        button = self._by_name(report, "Button (background)")
        self.assertAlmostEqual(self._corner_modifier(button).width, 0.008, places=6)
        # image plane: plain textured quad, radius 24 on a 48 px photo is clamped to min(w, h) / 2
        photo = self._by_name(report, "Photo")
        self.assertEqual(len(photo.data.vertices), 4)
        self.assertAlmostEqual(self._corner_modifier(photo).width, 0.024, places=6)
        self.assertAlmostEqual(self._evaluated(photo).dimensions.x, 0.048, places=6)
        self.assertEqual(photo.data.materials[0].node_tree.nodes["Image Texture"].type, "TEX_IMAGE")

    def test_corner_segments_option(self):
        scene, report, _ = self._build("SVG", corner_segments=4)
        header = self._by_name(report, "Header")
        self.assertEqual(self._corner_modifier(header).segments, 4)
        self.assertEqual(len(self._evaluated(header).data.vertices), 2 * 5 + 2)

    def test_per_corner_weights_and_zero_radius(self):
        from figma_to_blender import builder

        mesh = builder.plane_mesh("sq", 0.2, 0.1)
        ob = bpy.data.objects.new("sq", mesh)
        bpy.context.scene.collection.objects.link(ob)
        # tl 0.01, tr 0.05, br 0.02, bl 0.5 (clamped to min(w, h) / 2 = 0.05)
        mod = builder.add_corner_radius_modifier(ob, 0.2, 0.1, [0.01, 0.05, 0.02, 0.5], segments=6)
        self.assertAlmostEqual(mod.width, 0.05, places=6)
        self.assertEqual(mod.segments, 6)
        weights = builder.vertex_bevel_weights(mesh)
        self.assertAlmostEqual(weights[0], 0.2, places=6)  # tl
        self.assertAlmostEqual(weights[1], 1.0, places=6)  # tr
        self.assertAlmostEqual(weights[2], 0.4, places=6)  # br
        self.assertAlmostEqual(weights[3], 1.0, places=6)  # bl
        ev = self._evaluated(ob)
        self.assertEqual(len(ev.data.vertices), 4 * 7)
        # the rounded top-right corner no longer reaches the sharp corner point
        xs = [v.co for v in ev.data.vertices]
        self.assertFalse(any(abs(c.x - 0.1) < 1e-9 and abs(c.y - 0.05) < 1e-9 for c in xs))
        # no radius at all: modifier present with width 0 and weights 1 so it can be dialled in later
        mesh2 = builder.plane_mesh("sharp", 0.2, 0.1)
        ob2 = bpy.data.objects.new("sharp", mesh2)
        bpy.context.scene.collection.objects.link(ob2)
        mod2 = builder.add_corner_radius_modifier(ob2, 0.2, 0.1, None)
        self.assertEqual(mod2.width, 0.0)
        self.assertEqual(builder.vertex_bevel_weights(mesh2), [1.0] * 4)
        self.assertEqual(len(self._evaluated(ob2).data.vertices), 4)
        mod2.width = 0.03
        self.assertEqual(len(self._evaluated(ob2).data.vertices), 4 * 9)

    def test_ellipse_is_filled_curve(self):
        scene, report, _ = self._build("SVG")
        ellipse = self._by_name(report, "Avatar bg")
        self.assertEqual(ellipse.type, "CURVE")
        cu = ellipse.data
        self.assertEqual(cu.dimensions, "2D")
        self.assertEqual(cu.fill_mode, "BOTH")
        self.assertEqual(cu.resolution_u, 24)
        self.assertEqual(len(cu.splines), 1)
        self.assertEqual(cu.splines[0].type, "BEZIER")
        self.assertTrue(cu.splines[0].use_cyclic_u)
        self.assertEqual(len(cu.splines[0].bezier_points), 4)
        self.assertEqual(tuple(ellipse.scale), (1.0, 1.0, 1.0))
        # 64 x 64 px -> 0.064 m, sized through the control points
        self.assertAlmostEqual(ellipse.dimensions.x, 0.064, places=6)
        self.assertAlmostEqual(ellipse.dimensions.y, 0.064, places=6)
        ev = self._evaluated(ellipse)
        me = ev.to_mesh()
        self.assertGreater(len(me.polygons), 0)
        ev.to_mesh_clear()
        # centred on the node: Avatar group at Card(100,200)+(24,260) = (124,460), 64x64 -> centre (156, 492)
        self.assertAlmostEqual(ellipse.location.x, 0.156, places=6)
        self.assertAlmostEqual(ellipse.location.z, -0.492, places=6)

    # -- gradients (shader nodes) and strokes (Geometry Nodes) -------------------

    def _eval_extent(self, ob):
        """(dx, dy) of the evaluated geometry in object space (modifiers applied)."""
        ev = self._evaluated(ob)
        me = ev.to_mesh()
        xs = [v.co.x for v in me.vertices]
        ys = [v.co.y for v in me.vertices]
        mats = [m.name for m in me.materials if m is not None]
        n_faces = len(me.polygons)
        ev.to_mesh_clear()
        return round(max(xs) - min(xs), 6), round(max(ys) - min(ys), 6), mats, n_faces

    def test_gradient_fill_is_shader_nodes(self):
        from figma_to_blender import builder

        scene, report, _ = self._build("SVG")
        header = self._by_name(report, "Header")
        self.assertNotIn("figma_fill_approx", header)  # a real gradient, not an approximation any more
        mat = header.data.materials[0]
        self.assertTrue(mat["figma_managed"])
        nodes = mat.node_tree.nodes
        grads = [n for n in nodes if n.type == "TEX_GRADIENT"]
        self.assertEqual(len(grads), 1)
        self.assertEqual(grads[0].gradient_type, "LINEAR")
        ramps = [n for n in nodes if n.type == "VALTORGB"]
        self.assertEqual(len(ramps), 1)
        stops = ramps[0].color_ramp.elements
        self.assertEqual(len(stops), 2)
        self.assertAlmostEqual(stops[0].position, 0.0)
        self.assertAlmostEqual(stops[1].position, 1.0)
        self.assertEqual(tuple(round(c, 3) for c in stops[0].color), (0.2, 0.4, 1.0, 1.0))
        self.assertEqual(tuple(round(c, 3) for c in stops[1].color), (0.6, 0.2, 0.8, 1.0))
        self.assertEqual(len([n for n in nodes if n.type == "EMISSION"]), 1)
        mapping = [n for n in nodes if n.type == "MAPPING"][0]
        self.assertEqual(mapping.vector_type, "TEXTURE")
        # handles (0, .5) -> (1, .5): origin at UV (0, .5), no rotation, unit length
        self.assertEqual(tuple(round(v, 4) for v in mapping.inputs["Location"].default_value), (0.0, 0.5, 0.0))
        self.assertEqual(tuple(round(v, 4) for v in mapping.inputs["Rotation"].default_value), (0.0, 0.0, 0.0))
        self.assertEqual(tuple(round(v, 4) for v in mapping.inputs["Scale"].default_value), (1.0, 1.0, 1.0))
        link = [l for l in mat.node_tree.links if l.to_node == mapping][0]
        self.assertEqual(link.from_node.type, "TEX_COORD")
        self.assertEqual(link.from_socket.name, "UV")  # planes carry 0..1 UVs across the node box
        # radial gradient: SPHERICAL (1 at the centre) inverted so Figma's position 0 is the centre
        glow = self._by_name(report, "Glow")
        gm = glow.data.materials[0]
        self.assertIsNot(gm, mat)
        self.assertEqual([n for n in gm.node_tree.nodes if n.type == "TEX_GRADIENT"][0].gradient_type, "SPHERICAL")
        self.assertTrue([n for n in gm.node_tree.nodes if n.type == "MATH" and n.operation == "SUBTRACT"])
        gmap = [n for n in gm.node_tree.nodes if n.type == "MAPPING"][0]
        self.assertEqual(tuple(round(v, 4) for v in gmap.inputs["Location"].default_value), (0.5, 0.5, 0.0))
        self.assertEqual(tuple(round(v, 4) for v in gmap.inputs["Scale"].default_value), (0.5, 0.5, 1.0))
        gstops = [n for n in gm.node_tree.nodes if n.type == "VALTORGB"][0].color_ramp.elements
        self.assertEqual(tuple(round(c, 3) for c in gstops[0].color), (1.0, 0.85, 0.2, 1.0))
        self.assertEqual(tuple(round(c, 3) for c in gstops[-1].color), (0.9, 0.2, 0.3, 1.0))
        # mapping maths: a top -> bottom Figma gradient starts at UV (.5, 1) and points down
        loc, rot, scl = builder.gradient_mapping({"type": "GRADIENT_LINEAR", "handles": [[0.5, 0.0], [0.5, 1.0]]})
        self.assertEqual(tuple(round(v, 6) for v in loc), (0.5, 1.0, 0.0))
        self.assertAlmostEqual(rot, -math.pi / 2, places=6)
        self.assertEqual(tuple(round(v, 6) for v in scl), (1.0, 1.0, 1.0))
        # angular / diamond build too (angular through RADIAL + fract, diamond as |x| + |y|); alpha stops blend
        for gtype, expect in (("GRADIENT_ANGULAR", "RADIAL"), ("GRADIENT_DIAMOND", None)):
            g = {
                "type": gtype,
                "stops": [{"color": [1, 0, 0, 1], "position": 0.0}, {"color": [0, 0, 1, 0.5], "position": 1.0}],
                "handles": [[0.5, 0.5], [1.0, 0.5], [0.5, 1.0]],
            }
            m = builder.make_gradient_material("t " + gtype, g, 1.0, "Generated")
            kinds = [n.type for n in m.node_tree.nodes]
            self.assertIn("VALTORGB", kinds)
            self.assertIn("MIX_SHADER", kinds)  # a translucent stop -> transparent mix
            self.assertEqual(m.surface_render_method, "BLENDED")
            if expect:
                self.assertEqual([n for n in m.node_tree.nodes if n.type == "TEX_GRADIENT"][0].gradient_type, expect)
            else:
                self.assertNotIn("TEX_GRADIENT", kinds)
                self.assertIn("SEPXYZ", kinds)
            self.assertEqual([l.from_socket.name for l in m.node_tree.links if l.from_node.type == "TEX_COORD"], ["Generated"])
        # more stops than a Color Ramp holds are clamped to 32
        many = {"type": "GRADIENT_LINEAR", "stops": [{"color": [i / 40, 0, 0, 1], "position": i / 39} for i in range(40)], "handles": [[0, 0], [1, 0]]}
        m = builder.make_gradient_material("many", many)
        self.assertEqual(len([n for n in m.node_tree.nodes if n.type == "VALTORGB"][0].color_ramp.elements), 32)
        # the same gradient is one material (cached by content), text with a gradient keeps a flat colour
        cache = builder.MaterialCache()
        self.assertIs(cache.gradient(many, 1.0), cache.gradient(dict(many), 1.0))

    def test_stroke_is_geometry_nodes_modifier(self):
        from figma_to_blender import builder

        scene, report, _ = self._build("SVG")
        card = self._by_name(report, "Outlined card")
        self.assertEqual([m.name for m in card.modifiers], ["Corner Radius", "Stroke"])
        mod = card.modifiers["Stroke"]
        self.assertEqual(mod.type, "NODES")
        self.assertEqual(mod.node_group.name, "Figma Stroke")
        st = builder.stroke_settings(mod)
        self.assertAlmostEqual(st["width"], 4 * 0.001, places=9)  # strokeWeight x scale
        self.assertEqual(st["align"], "INSIDE")
        self.assertEqual(tuple(round(c, 3) for c in st["material"].diffuse_color), (0.2, 0.4, 1.0, 1.0))
        self.assertAlmostEqual(st["lift"], 0.0001, places=9)
        # one shared node group for every stroked object in the file
        self.assertEqual(len([g for g in bpy.data.node_groups if g.name.startswith("Figma Stroke")]), 1)
        # the mesh itself is still the 4-vertex plane; the evaluated result carries the ribbon
        self.assertEqual(len(card.data.vertices), 4)
        dx, dy, mats, n_faces = self._eval_extent(card)
        self.assertEqual((dx, dy), (0.2, 0.06))  # INSIDE: nothing sticks out of the 200 x 60 px box
        self.assertGreater(n_faces, 1)
        self.assertIn(card.data.materials[0].name, mats)
        self.assertIn(st["material"].name, mats)
        # ellipse (curve object): CENTER, 80 px + 6 px -> 86 px; the fill face is still there
        ring = self._by_name(report, "Ring")
        self.assertEqual(ring.type, "CURVE")
        rmod = builder.stroke_modifier(ring)
        self.assertIsNotNone(rmod)
        self.assertEqual(builder.stroke_settings(rmod)["align"], "CENTER")
        self.assertAlmostEqual(builder.stroke_settings(rmod)["width"], 0.006, places=9)
        dx, dy, mats, n_faces = self._eval_extent(ring)
        self.assertAlmostEqual(dx, 0.086, places=4)
        self.assertAlmostEqual(dy, 0.086, places=4)
        self.assertIn(ring.data.materials[0].name, mats)
        self.assertGreater(n_faces, 4 * 24)  # more than the ribbon quads alone: the filled disc is still there
        # OUTSIDE: 100 px + 2 x 2 px
        glow = self._by_name(report, "Glow")
        self.assertEqual(builder.stroke_settings(builder.stroke_modifier(glow))["align"], "OUTSIDE")
        dx, dy, _, _ = self._eval_extent(glow)
        self.assertEqual((dx, dy), (0.104, 0.104))
        # stroke-only rectangle: a transparent fill plus the outline
        outline = self._by_name(report, "Outline only")
        self.assertAlmostEqual(outline.data.materials[0].diffuse_color[3], 0.0, places=6)
        self.assertEqual(outline.data.materials[0].surface_render_method, "BLENDED")
        self.assertIsNotNone(builder.stroke_modifier(outline))
        # elements without a stroke get no modifier; the stroke material is a shared flat material
        self.assertIsNone(builder.stroke_modifier(self._by_name(report, "Header")))
        self.assertTrue(st["material"].name.startswith("Figma 3366FF"))
        # re-import: changed weight / align update the modifier in place, a dropped stroke removes it
        el = next(e for e in scene.elements if e.id == "2:1")
        el.stroke_weight, el.stroke_align = 8.0, "OUTSIDE"
        next(e for e in scene.elements if e.id == "2:2").stroke_rgba = None
        report2 = self._rebuild(scene)
        self.assertIs(self._by_name(report2, "Outlined card"), card)
        self.assertEqual(card.modifiers["Stroke"], mod)  # same modifier (RNA wrappers compare by pointer)
        self.assertAlmostEqual(builder.stroke_settings(mod)["width"], 0.008, places=9)
        self.assertEqual(builder.stroke_settings(mod)["align"], "OUTSIDE")
        self.assertEqual(self._eval_extent(card)[:2], (0.216, 0.076))
        self.assertIsNone(builder.stroke_modifier(ring))
        self.assertEqual(len([g for g in bpy.data.node_groups if g.name.startswith("Figma Stroke")]), 1)

    def test_frame_root_collection_named_after_frame_at_origin(self):
        from figma_to_blender import builder

        make_bundle(self.tmp, root_id="1:2")
        scene = load_scene(self.tmp)
        self.assertEqual(scene.root_type, "FRAME")
        report = builder.build_bundle(self.tmp, builder.BuildOptions(icon_mode="SVG", center=False))
        # the collection takes the frame's name, not the page's
        self.assertIn("Card", bpy.data.collections)
        self.assertNotIn("Page 1", bpy.data.collections)
        self.assertIs(report.collection, bpy.data.collections["Card"])
        self.assertEqual(report.collection["figma_page_id"], "1:2")
        # only the Card subtree was built: 4 groups, 4 rects, 4 texts, 1 ellipse, 1 image, 3 icons
        self.assertEqual(report.counts, {"group": 4, "rect": 4, "text": 4, "ellipse": 1, "image": 1, "icon": 3})
        self.assertFalse([o for o in report.objects if o.name in ("Standalone label", "Big vector frame")])
        # the frame's Empty sits at the world origin and its background plane has its
        # top-left corner there (360x480 px plane, centre at (180, -240) mm, depth index 1)
        card = self._by_name(report, "Card")
        self.assertEqual(card.type, "EMPTY")
        self.assertAlmostEqual(card.location.x, 0.0, places=6)
        self.assertAlmostEqual(card.location.z, 0.0, places=6)
        bg = self._by_name(report, "Card (background)")
        self.assertEqual(bg.parent, card)
        self.assertAlmostEqual(bg.location.x, 0.180, places=6)
        self.assertAlmostEqual(bg.location.z, -0.240, places=6)
        self.assertAlmostEqual(bg.location.y, -1 * 0.0005, places=6)
        lo, hi = obj_bbox([bg])
        self.assertAlmostEqual(lo.x, 0.0, places=6)
        self.assertAlmostEqual(hi.z, 0.0, places=6)
        self.assertAlmostEqual(hi.x, 0.360, places=6)
        self.assertAlmostEqual(lo.z, -0.480, places=6)
        self._corner_modifier(bg)  # still a plane + Corner Radius modifier
        # children moved with the frame: Title was at page (124, 340) -> (24, 140)
        title = self._by_name(report, "Title")
        self.assertAlmostEqual(title.location.x, 0.024, places=6)
        self.assertLess(title.location.z, -0.140)
        self.assertGreater(title.location.z, -0.140 - 0.05)

    def test_missing_asset_is_reported_not_fatal(self):
        from figma_to_blender import builder

        make_bundle(self.tmp)
        os.remove(os.path.join(self.tmp, "assets", "image_1_8.png"))
        os.remove(os.path.join(self.tmp, "assets", "icon_1_17.svg"))
        report = builder.build_bundle(self.tmp, builder.BuildOptions())
        self.assertEqual(report.counts.get("image", 0), 0)
        self.assertEqual(report.counts["icon"], 4 + 1)  # Rating falls back to its solid fill
        self.assertTrue(any("image_1_8.png" in w for w in report.warnings))

    # -- re-import / sync by Figma id ------------------------------------------

    def _rebuild(self, scene, **extra):
        """Write ``scene`` over the bundle and build it again into the same Blender scene."""
        from figma_to_blender import builder

        write_scene(scene, self.tmp)
        opts = builder.BuildOptions(scale=0.001, depth_step=0.0005, icon_mode="SVG", center=False, **extra)
        return builder.build_scene(load_scene(self.tmp), self.tmp, opts)

    def test_reimport_updates_in_place_without_duplicates(self):
        scene, report, _ = self._build("SVG")
        coll = report.collection
        n_objects, n_collections = len(coll.objects), len(bpy.data.collections)
        pointers = {o.name: o.as_pointer() for o in coll.objects}
        self.assertEqual(report.sync["created"], len(scene.elements))  # icon curves are not elements
        self.assertEqual(report.sync["updated"], 0)
        report2 = self._rebuild(scene)
        self.assertIs(report2.collection, coll)
        self.assertEqual(len(coll.objects), n_objects)
        self.assertEqual(len(bpy.data.collections), n_collections)  # no "Page 1.001", no removed collection
        self.assertEqual(report2.sync, {"created": 0, "updated": len(scene.elements), "moved": 0, "removed": 0})
        self.assertEqual(report2.counts, report.counts)
        self.assertEqual({o.name: o.as_pointer() for o in coll.objects}, pointers)  # same objects, same names
        self.assertFalse([o for o in bpy.data.objects if o.name.endswith(".001")])
        # materials were reused by name, not duplicated
        self.assertFalse([m for m in bpy.data.materials if m.name.startswith("Figma") and m.name.endswith(".001")])
        self.assertIn("sync: updated %d" % len(scene.elements), report2.summary())
        # the icon curves were kept (same SVG), still parented under their Empty
        arrow = self._by_name(report2, "Arrow")
        self.assertEqual(len([o for o in coll.objects if o.parent == arrow]), 2)
        lo, hi = obj_bbox([o for o in coll.objects if o.parent == arrow])
        self.assertAlmostEqual(hi.x - lo.x, 0.024, places=4)
        self.assertAlmostEqual(lo.x, 0.394, places=4)

    def test_reimport_applies_figma_changes_and_keeps_user_edits(self):
        from figma_to_blender import builder

        scene, report, _ = self._build("SVG")
        coll = report.collection
        header = self._by_name(report, "Header")
        title = self._by_name(report, "Title")
        button_bg = self._by_name(report, "Button (background)")
        card_bg = self._by_name(report, "Card (background)")
        rating = self._by_name(report, "Rating")
        photo = self._by_name(report, "Photo")
        old_button_mat = button_bg.data.materials[0]
        old_header_mat = header.data.materials[0]
        # user edits: an extra modifier, a swapped material, custom props, a font change
        header.modifiers.new("Solidify", "SOLIDIFY")
        user_mat = bpy.data.materials.new("My card material")
        card_bg.data.materials[0] = user_mat
        header["my_note"] = "keep me"
        header.data.materials[0] = header.data.materials[0]  # importer material stays -> managed
        # Figma edits: move + resize the header, retext the title, recolour the button, drop the
        # star, add a rectangle, change the ellipse size, change the photo's asset
        el_header = next(e for e in scene.elements if e.id == "1:3")
        el_header.matrix[2] += 10.0
        el_header.x += 10.0
        el_header.w = 300.0
        el_header.corner_radii = [8.0, 8.0, 8.0, 8.0]
        next(e for e in scene.elements if e.id == "1:4").text["characters"] = "Hello again"
        next(e for e in scene.elements if e.id == "1:11:bg").fill = [0.0, 0.5, 0.0, 1.0]
        next(e for e in scene.elements if e.id == "1:7").w = 80.0
        scene.elements = [e for e in scene.elements if e.id != "1:17"]
        from figma_to_blender.scene_model import Element

        new_rect = Element(
            id="9:9", name="Added rect", kind="rect", figma_type="RECTANGLE", parent="1:2", x=110, y=210, w=50, h=20,
            rotation=0.0, matrix=[1, 0, 110, 0, 1, 210], fill=[1.0, 0.0, 0.0, 1.0],
        )
        scene.elements.append(new_rect)
        with open(os.path.join(self.tmp, "assets", "image_1_8.png"), "ab") as fh:
            fh.write(b"\x00")  # different bytes -> different hash (Blender ignores trailing junk)
        n_before = len(coll.objects)

        report2 = self._rebuild(scene)
        self.assertEqual(report2.sync["created"], 1)
        self.assertEqual(report2.sync["moved"], 1)
        self.assertEqual(report2.sync["removed"], 0)
        self.assertEqual(report2.sync["updated"], len(scene.elements) - 1)
        self.assertEqual(len(coll.objects), n_before - 3 + 1)  # star Empty + its 2 curves moved out, 1 added
        # header: same object, moved, resized, radius updated, Solidify + custom prop kept, modifier order kept
        self.assertIs(self._by_name(report2, "Header"), header)
        self.assertAlmostEqual(header.location.x, (100 + 10 + 150) * 0.001, places=6)
        self.assertAlmostEqual(header.dimensions.x, 0.300, places=6)
        self.assertEqual(len(header.data.vertices), 4)
        self.assertEqual([m.name for m in header.modifiers], ["Corner Radius", "Solidify"])
        self.assertAlmostEqual(header.modifiers["Corner Radius"].width, 0.008, places=6)
        self.assertEqual(builder.vertex_bevel_weights(header.data), [1.0] * 4)
        self.assertEqual(header["my_note"], "keep me")
        self.assertIs(header.data.materials[0], old_header_mat)
        # text updated in place
        self.assertIs(self._by_name(report2, "Title"), title)
        self.assertEqual(title.data.body, "Hello again")
        # importer material swapped for the new colour, user material left alone
        self.assertIsNot(button_bg.data.materials[0], old_button_mat)
        self.assertEqual(tuple(round(c, 2) for c in button_bg.data.materials[0].diffuse_color[:3]), (0.0, 0.5, 0.0))
        self.assertIs(card_bg.data.materials[0], user_mat)
        # ellipse resized through its control points, same curve datablock
        avatar = self._by_name(report2, "Avatar bg")
        self._evaluated(avatar)  # refresh the bound box
        self.assertAlmostEqual(avatar.dimensions.x, 0.080, places=6)
        self.assertEqual(len(avatar.data.splines[0].bezier_points), 4)
        # image asset changed -> new image datablock on the same plane
        self.assertIs(self._by_name(report2, "Photo"), photo)
        # removed element parked in the removed sub-collection, still in the file
        removed = bpy.data.collections.get("Page 1 (removed)")
        self.assertIsNotNone(removed)
        self.assertIn(removed, list(coll.children))
        self.assertIn(rating, list(removed.objects))
        self.assertNotIn(rating, list(coll.objects))
        self.assertTrue(all(o.parent == rating for o in removed.objects if o != rating))  # its curves went with it
        # the new element was created and parented like a fresh import
        added = self._by_name(report2, "Added rect")
        self.assertEqual(added["figma_elem_id"], "9:9")
        self.assertEqual(added.parent.name, "Card")
        self.assertIn("Corner Radius", added.modifiers)
        # a third import with remove_missing deletes the element that vanished this time
        scene.elements = [e for e in scene.elements if e.id != "9:9"]
        report3 = self._rebuild(scene, remove_missing=True)
        self.assertEqual(report3.sync["removed"], 1)
        self.assertNotIn("Added rect", bpy.data.objects)

    def test_reimport_can_be_disabled(self):
        scene, report, _ = self._build("SVG")
        n = len(bpy.data.objects)
        report2 = self._rebuild(scene, update_existing=False)
        self.assertIsNot(report2.collection, report.collection)
        self.assertEqual(report2.sync["updated"], 0)
        self.assertEqual(len(bpy.data.objects), 2 * n)

    def test_reimport_matches_legacy_objects_without_elem_id(self):
        scene, report, _ = self._build("SVG")
        for ob in report.collection.objects:
            if "figma_elem_id" in ob:
                del ob["figma_elem_id"]
        n = len(report.collection.objects)
        report2 = self._rebuild(scene)
        self.assertEqual(report2.sync["created"], 0)
        self.assertEqual(len(report.collection.objects), n)
        self.assertEqual(self._by_name(report2, "Card (background)")["figma_elem_id"], "1:2:bg")

    @staticmethod
    def _cancelled(op):
        try:
            return op() == {"CANCELLED"}
        except RuntimeError as e:
            return "Error" in str(e)

    def test_addon_registration_and_bundle_operator(self):
        import figma_to_blender

        figma_to_blender.register()
        try:
            self.assertTrue(hasattr(bpy.types.Scene, "figma_to_blender"))
            self.assertTrue(hasattr(bpy.ops.figma, "import_bundle"))
            make_bundle(self.tmp)
            s = bpy.context.scene.figma_to_blender
            s.bundle_dir = self.tmp
            s.icon_mode = "SVG"
            s.center = False
            result = bpy.ops.figma.import_bundle()
            self.assertEqual(result, {"FINISHED"})
            self.assertIn("Page 1", bpy.data.collections)
            self.assertEqual(len([o for o in bpy.data.collections["Page 1"].objects if o.type == "FONT"]), 5)
            # error reports cancel the operator (bpy raises RuntimeError for ERROR reports in background mode)
            s.bundle_dir = os.path.join(self.tmp, "nope")
            self.assertTrue(self._cancelled(bpy.ops.figma.import_bundle))
            os.environ.pop("FIGMA_TOKEN", None)
            s.file_url = "https://www.figma.com/design/KEY/x"
            self.assertTrue(self._cancelled(bpy.ops.figma.fetch_pages))
            self.assertTrue(hasattr(bpy.ops.figma, "fetch_frames"))
            # frame dropdown always offers the whole page; an unparsable node reference is refused
            # before any network access (no token set, no pages fetched)
            self.assertEqual(s.frame, "PAGE")
            s.node_ref = "https://www.figma.com/design/KEY/x"  # no node-id
            self.assertTrue(self._cancelled(bpy.ops.figma.import_page))
            self.assertTrue(self._cancelled(bpy.ops.figma.fetch_frames))  # needs a page first
            self.assertEqual(figma_to_blender.resolve_target.__name__, "resolve_target")
            s.node_ref = "https://www.figma.com/design/KEY/x?node-id=1-2"
            self.assertEqual(figma_to_blender.resolve_target(s), ("1:2", "node 1:2"))
        finally:
            figma_to_blender.unregister()
        self.assertFalse(hasattr(bpy.types.Scene, "figma_to_blender"))

    def test_render(self):
        from figma_to_blender import builder

        out = os.environ.get("FIGMA_RENDER_OUT") or os.path.join(self.tmp, "render.png")
        scene, report, opts = self._build("SVG", center=True)
        builder.add_preview_camera(report, opts, scene)
        sc = bpy.context.scene
        sc.render.resolution_x, sc.render.resolution_y = 800, 600
        sc.render.resolution_percentage = 100
        sc.render.filepath = out
        sc.render.image_settings.file_format = "PNG"
        try:  # Cycles is an add-on; the bpy module does not enable it by default
            import addon_utils

            addon_utils.enable("cycles", default_set=False)
        except Exception as e:  # noqa: BLE001
            print("could not enable cycles:", e)
        engines = [i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
        # EEVEE / Workbench need a GPU context and abort the process without one; in
        # background mode (bpy module, blender -b on a headless box) use Cycles on the CPU.
        if bpy.app.background:
            candidates = ("CYCLES",)
        else:
            candidates = ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "BLENDER_WORKBENCH", "CYCLES")
        try:
            sc.view_settings.view_transform = "Standard"  # keep the flat UI colours as authored
        except TypeError:
            pass
        world = bpy.data.worlds.new("Figma World")
        sc.world = world
        world.color = (0.85, 0.85, 0.88)
        rendered = False
        for engine in candidates:
            if engine != "CYCLES" and engine not in engines:
                continue
            try:
                sc.render.engine = engine
                if engine == "CYCLES":
                    sc.cycles.samples = 16
                    sc.cycles.device = "CPU"
                    sc.cycles.use_denoising = False
                elif engine.startswith("BLENDER_EEVEE") and hasattr(sc.eevee, "taa_render_samples"):
                    sc.eevee.taa_render_samples = 4
                bpy.ops.render.render(write_still=True)
                rendered = os.path.exists(out) and os.path.getsize(out) > 0
                if rendered:
                    print("rendered with", engine, "->", out)
                    break
            except Exception as e:  # noqa: BLE001
                print("render with", engine, "failed:", e)
        self.assertTrue(rendered, "no render engine produced an image")


if __name__ == "__main__":
    unittest.main()
