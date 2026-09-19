"""Turn a scene bundle (``scene.json`` + ``assets/``) into Blender objects.

This is the only module (besides the add-on UI in ``__init__``) that imports
``bpy``.  Everything is placed in a new Collection named after the Figma page,
with one Empty per Figma container so groups can be moved together.

Coordinate mapping
------------------
Elements are laid out in a *2D frame*: x to the right, y up (Figma y negated),
z towards the viewer (draw order, ``depth_step`` per element).  A single base
rotation then maps that frame into the chosen ``plane_orientation``:

* ``XZ`` (default): the UI stands upright and faces -Y (Blender front view).
* ``XY``: the UI lies flat on the ground, depth stacks along +Z.
"""

from __future__ import annotations

import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import bpy
import bmesh
from mathutils import Matrix, Vector

from . import fonts
from .scene_model import Element, Scene, load_scene

log = logging.getLogger(__name__)

CORNER_SEGMENTS = 8
ELLIPSE_SEGMENTS = 64
TEXT_ASCENT_RATIO = 0.8  # approximate ascender / font size used to place Figma baselines


@dataclass
class BuildOptions:
    scale: float = 0.001  # metres per Figma px
    depth_step: float = 0.0005  # metres per draw-order step
    icon_mode: str = "SVG"  # "SVG" (curves) | "PLANE" (textured plane)
    plane_orientation: str = "XZ"  # "XZ" | "XY"
    center: bool = True  # put the page bounds' centre at the world origin
    collection_name: Optional[str] = None


@dataclass
class BuildReport:
    counts: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    missing_fonts: List[str] = field(default_factory=list)
    objects: List["bpy.types.Object"] = field(default_factory=list)
    collection: Optional["bpy.types.Collection"] = None

    def bump(self, kind: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def summary(self) -> str:
        parts = ["%s=%d" % kv for kv in sorted(self.counts.items())]
        s = "Imported " + (", ".join(parts) if parts else "nothing")
        if self.missing_fonts:
            s += "; fonts not found: " + ", ".join(sorted(set(self.missing_fonts)))
        if self.warnings:
            s += "; %d warning(s)" % len(self.warnings)
        return s


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------


def set_material_blend(mat: "bpy.types.Material", alpha: float) -> None:
    """Enable alpha blending, guarding attributes that differ across versions."""
    if alpha >= 0.999:
        return
    if hasattr(mat, "blend_method"):
        try:
            mat.blend_method = "BLEND"
        except TypeError:
            pass
    if hasattr(mat, "surface_render_method"):  # 4.2+
        mat.surface_render_method = "BLENDED"
    if hasattr(mat, "shadow_method"):  # <= 4.1
        try:
            mat.shadow_method = "HASHED"
        except TypeError:
            pass
    if hasattr(mat, "show_transparent_back"):
        mat.show_transparent_back = False


def _emission_output(mat: "bpy.types.Material"):
    """Reset the node tree to Emission -> Output and return (nodes, links, emission, output)."""
    if hasattr(mat, "use_nodes") and not mat.use_nodes:  # always on from 5.0, deprecated there
        mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (400, 0)
    em = nt.nodes.new("ShaderNodeEmission")
    em.location = (0, 0)
    em.inputs["Strength"].default_value = 1.0
    return nt.nodes, nt.links, em, out


def _link_with_alpha(nodes, links, shader, out, alpha_socket=None, alpha_value: float = 1.0):
    """Connect ``shader`` to ``out``, mixing with Transparent when alpha < 1 or a socket is given."""
    if alpha_socket is None and alpha_value >= 0.999:
        links.new(shader.outputs[0], out.inputs["Surface"])
        return
    transp = nodes.new("ShaderNodeBsdfTransparent")
    transp.location = (0, -200)
    mix = nodes.new("ShaderNodeMixShader")
    mix.location = (200, 0)
    links.new(transp.outputs[0], mix.inputs[1])
    links.new(shader.outputs[0], mix.inputs[2])
    if alpha_socket is not None:
        if alpha_value < 0.999:
            mul = nodes.new("ShaderNodeMath")
            mul.operation = "MULTIPLY"
            mul.location = (0, 200)
            mul.inputs[1].default_value = alpha_value
            links.new(alpha_socket, mul.inputs[0])
            links.new(mul.outputs[0], mix.inputs["Fac"])
        else:
            links.new(alpha_socket, mix.inputs["Fac"])
    else:
        mix.inputs["Fac"].default_value = alpha_value
    links.new(mix.outputs[0], out.inputs["Surface"])


class MaterialCache:
    def __init__(self):
        self._flat: Dict[Tuple[int, int, int, int], "bpy.types.Material"] = {}
        self._image: Dict[Tuple[str, int], "bpy.types.Material"] = {}

    def flat(self, color, alpha: float) -> "bpy.types.Material":
        r, g, b = (float(c) for c in color[:3])
        key = (round(r * 255), round(g * 255), round(b * 255), round(alpha * 255))
        mat = self._flat.get(key)
        if mat is None:
            mat = make_flat_material("Figma %02X%02X%02X a%d" % key, (r, g, b), alpha)
            self._flat[key] = mat
        return mat

    def image(self, image: "bpy.types.Image", alpha: float) -> "bpy.types.Material":
        key = (image.name, round(alpha * 255))
        mat = self._image.get(key)
        if mat is None:
            mat = make_image_material("Figma img " + image.name, image, alpha)
            self._image[key] = mat
        return mat


def make_flat_material(name: str, color, alpha: float = 1.0) -> "bpy.types.Material":
    """Unlit (emission) material of a single colour, optionally transparent."""
    mat = bpy.data.materials.new(name)
    r, g, b = (float(c) for c in color[:3])
    nodes, links, em, out = _emission_output(mat)
    em.inputs["Color"].default_value = (r, g, b, 1.0)
    _link_with_alpha(nodes, links, em, out, alpha_value=alpha)
    mat.diffuse_color = (r, g, b, alpha)
    set_material_blend(mat, alpha)
    return mat


def make_image_material(name: str, image: "bpy.types.Image", alpha: float = 1.0) -> "bpy.types.Material":
    """Unlit material showing ``image`` with its own alpha (times ``alpha``)."""
    mat = bpy.data.materials.new(name)
    nodes, links, em, out = _emission_output(mat)
    tex = nodes.new("ShaderNodeTexImage")
    tex.location = (-300, 0)
    tex.image = image
    tex.interpolation = "Linear"
    links.new(tex.outputs["Color"], em.inputs["Color"])
    _link_with_alpha(nodes, links, em, out, alpha_socket=tex.outputs["Alpha"], alpha_value=alpha)
    set_material_blend(mat, 0.5)  # always blended: the texture carries alpha
    return mat


# ---------------------------------------------------------------------------
# Geometry helpers (built in the local 2D frame: x right, y up, centred)
# ---------------------------------------------------------------------------


def rounded_rect_outline(w: float, h: float, radii: Optional[List[float]]) -> List[Tuple[float, float]]:
    """Outline points of a w x h rectangle centred on the origin (y up).

    ``radii`` is Figma's ``[tl, tr, br, bl]`` in the same units as w/h.
    """
    radii = list(radii or [0.0, 0.0, 0.0, 0.0])
    cap = min(w, h) / 2.0
    tl, tr, br, bl = (max(0.0, min(float(r), cap)) for r in radii)
    hw, hh = w / 2.0, h / 2.0
    pts: List[Tuple[float, float]] = []

    # corners in counter-clockwise order starting bottom-left: (centre, radius, start angle)
    corners = (
        ((-hw + bl, -hh + bl), bl, math.pi),  # bottom-left
        ((hw - br, -hh + br), br, 1.5 * math.pi),  # bottom-right
        ((hw - tr, hh - tr), tr, 0.0),  # top-right
        ((-hw + tl, hh - tl), tl, 0.5 * math.pi),  # top-left
    )
    for (cx, cy), r, start in corners:
        if r <= 1e-9:
            # sharp corner
            sx = -hw if cx < 0 else hw
            sy = -hh if cy < 0 else hh
            pts.append((sx, sy))
            continue
        for i in range(CORNER_SEGMENTS + 1):
            a = start + (math.pi / 2.0) * (i / CORNER_SEGMENTS)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    # remove consecutive duplicates
    out: List[Tuple[float, float]] = []
    for p in pts:
        if not out or (abs(out[-1][0] - p[0]) > 1e-9 or abs(out[-1][1] - p[1]) > 1e-9):
            out.append(p)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) < 1e-9 and abs(out[0][1] - out[-1][1]) < 1e-9:
        out.pop()
    return out


def ellipse_outline(w: float, h: float, segments: int = ELLIPSE_SEGMENTS) -> List[Tuple[float, float]]:
    return [
        (w / 2.0 * math.cos(2 * math.pi * i / segments), h / 2.0 * math.sin(2 * math.pi * i / segments))
        for i in range(segments)
    ]


def mesh_from_outline(name: str, outline: List[Tuple[float, float]], w: float, h: float) -> "bpy.types.Mesh":
    """Single n-gon mesh with a 0..1 UV layer spanning the w x h box."""
    bm = bmesh.new()
    verts = [bm.verts.new((x, y, 0.0)) for x, y in outline]
    face = bm.faces.new(verts)
    uv_layer = bm.loops.layers.uv.new("UVMap")
    for loop in face.loops:
        x, y, _ = loop.vert.co
        loop[uv_layer].uv = ((x + w / 2.0) / w if w else 0.5, (y + h / 2.0) / h if h else 0.5)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    return mesh


# ---------------------------------------------------------------------------
# SVG helpers
# ---------------------------------------------------------------------------

_SVG_ROOT_RE = re.compile(rb"<svg\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(rb'([A-Za-z:_-]+)\s*=\s*"([^"]*)"')


def svg_document_size(path: str) -> Optional[Tuple[float, float]]:
    """(width, height) in SVG user units from ``viewBox`` or ``width``/``height``."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(65536)
    except OSError:
        return None
    m = _SVG_ROOT_RE.search(head)
    if not m:
        return None
    attrs = {k.decode().lower(): v.decode() for k, v in _ATTR_RE.findall(m.group(0))}
    vb = attrs.get("viewbox")
    if vb:
        parts = re.split(r"[\s,]+", vb.strip())
        if len(parts) == 4:
            try:
                w, h = float(parts[2]), float(parts[3])
                if w > 0 and h > 0:
                    return w, h
            except ValueError:
                pass
    try:
        w = float(re.sub(r"[a-z%]+$", "", attrs.get("width", "")))
        h = float(re.sub(r"[a-z%]+$", "", attrs.get("height", "")))
        if w > 0 and h > 0:
            return w, h
    except ValueError:
        pass
    return None


def svg_importer_available() -> bool:
    return hasattr(bpy.ops, "import_curve") and hasattr(bpy.ops.import_curve, "svg")


def _import_svg(path: str) -> Tuple[List["bpy.types.Object"], List["bpy.types.Collection"]]:
    """Run the SVG importer and return the objects / collections it created."""
    before_objs = set(bpy.data.objects)
    before_colls = set(bpy.data.collections)
    result = bpy.ops.import_curve.svg(filepath=path)
    if "FINISHED" not in result:
        raise RuntimeError("SVG importer returned %s" % result)
    new_objs = [o for o in bpy.data.objects if o not in before_objs]
    new_colls = [c for c in bpy.data.collections if c not in before_colls]
    return new_objs, new_colls


def world_bbox(objs: List["bpy.types.Object"]) -> Optional[Tuple[Vector, Vector]]:
    mins = Vector((math.inf,) * 3)
    maxs = Vector((-math.inf,) * 3)
    found = False
    for ob in objs:
        if ob.type == "CURVE" and ob.data.splines and all(len(s.points) == 0 and len(s.bezier_points) == 0 for s in ob.data.splines):
            continue
        for corner in ob.bound_box:
            p = ob.matrix_world @ Vector(corner)
            mins = Vector(map(min, mins, p))
            maxs = Vector(map(max, maxs, p))
            found = True
    return (mins, maxs) if found else None


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class SceneBuilder:
    def __init__(self, scene: Scene, bundle_dir: str, options: Optional[BuildOptions] = None):
        self.scene = scene
        self.bundle_dir = bundle_dir
        self.opt = options or BuildOptions()
        self.report = BuildReport()
        self.materials = MaterialCache()
        self.objects: Dict[str, "bpy.types.Object"] = {}
        self._svg_px_to_m: Optional[float] = None
        self._text_baseline_ratio: Optional[float] = None
        self._font_cache: Dict[Tuple, Optional["bpy.types.VectorFont"]] = {}

        b = scene.bounds or {"x": 0, "y": 0, "w": 0, "h": 0}
        if self.opt.center and scene.bounds:
            self.offset = (b["x"] + b["w"] / 2.0, b["y"] + b["h"] / 2.0)
        else:
            self.offset = (0.0, 0.0)
        if self.opt.plane_orientation == "XY":
            self.base = Matrix.Identity(4)
        else:
            self.base = Matrix.Rotation(math.pi / 2.0, 4, "X")

    # -- transforms ---------------------------------------------------------

    def _frame_point(self, el: Element, lx: float, ly: float, depth_index: int) -> Vector:
        """Figma local px point of ``el`` -> 2D-frame coordinates (metres)."""
        a, b, tx, c, d, ty = el.matrix
        fx = a * lx + b * ly + tx - self.offset[0]
        fy = c * lx + d * ly + ty - self.offset[1]
        return Vector((fx * self.opt.scale, -fy * self.opt.scale, depth_index * self.opt.depth_step))

    def _linear(self, el: Element) -> Matrix:
        """Rotation / flip part of the element's world matrix in the y-up frame."""
        a, b, _, c, d, _ = el.matrix
        return Matrix(((a, -b, 0.0, 0.0), (-c, d, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0)))

    def matrix_for(self, el: Element, depth_index: int, anchor: Tuple[float, float]) -> Matrix:
        """World matrix placing the object's origin at Figma-local ``anchor`` (px)."""
        return self.base @ Matrix.Translation(self._frame_point(el, anchor[0], anchor[1], depth_index)) @ self._linear(el)

    # -- object plumbing ----------------------------------------------------

    def _new_object(self, el: Element, name: str, data) -> "bpy.types.Object":
        ob = bpy.data.objects.new(name, data)
        self.report.collection.objects.link(ob)
        ob["figma_id"] = el.id.split("#", 1)[0].split(":bg", 1)[0]
        ob["figma_type"] = el.figma_type
        ob["figma_name"] = el.name
        ob["figma_kind"] = el.kind
        self.report.objects.append(ob)
        return ob

    def _parent(self, ob: "bpy.types.Object", el: Element) -> None:
        parent = self.objects.get(el.parent) if el.parent else None
        if parent is None or parent == ob:
            return
        ob.parent = parent
        ob.matrix_parent_inverse = parent.matrix_world.inverted()

    def _asset_path(self, el: Element) -> Optional[str]:
        if not el.asset:
            return None
        p = os.path.join(self.bundle_dir, el.asset)
        if not os.path.exists(p):
            self.report.warnings.append("Asset missing for %r: %s" % (el.name, el.asset))
            return None
        return p

    # -- element builders ---------------------------------------------------

    def build_group(self, el: Element, depth: int) -> "bpy.types.Object":
        ob = self._new_object(el, el.name, None)
        ob.empty_display_type = "PLAIN_AXES"
        ob.empty_display_size = max(0.01, min(el.w, el.h) * self.opt.scale * 0.25)
        ob.matrix_world = self.matrix_for(el, depth, (0.0, 0.0))
        return ob

    def build_shape(self, el: Element, depth: int) -> "bpy.types.Object":
        s = self.opt.scale
        w, h = max(el.w, 1e-6) * s, max(el.h, 1e-6) * s
        if el.kind == "ellipse":
            outline = ellipse_outline(w, h)
        else:
            radii = [r * s for r in el.corner_radii] if el.corner_radii else None
            outline = rounded_rect_outline(w, h, radii)
        mesh = mesh_from_outline(el.name, outline, w, h)
        ob = self._new_object(el, el.name, mesh)
        color = el.fill or [0.5, 0.5, 0.5, 1.0]
        alpha = color[3] * el.opacity if len(color) > 3 else el.opacity
        mesh.materials.append(self.materials.flat(color, alpha))
        if el.fill_approx:
            ob["figma_fill_approx"] = True
        ob.matrix_world = self.matrix_for(el, depth, (el.w / 2.0, el.h / 2.0))
        return ob

    def build_plane(self, el: Element, depth: int, path: str) -> "bpy.types.Object":
        s = self.opt.scale
        w, h = max(el.w, 1e-6) * s, max(el.h, 1e-6) * s
        radii = [r * s for r in el.corner_radii] if (el.kind == "image" and el.corner_radii) else None
        outline = rounded_rect_outline(w, h, radii)
        mesh = mesh_from_outline(el.name, outline, w, h)
        ob = self._new_object(el, el.name, mesh)
        try:
            image = bpy.data.images.load(path, check_existing=True)
            mesh.materials.append(self.materials.image(image, el.opacity))
        except RuntimeError as e:
            self.report.warnings.append("Could not load image %s for %r: %s" % (path, el.name, e))
            mesh.materials.append(self.materials.flat(el.fill or [0.5, 0.5, 0.5, 1.0], el.opacity))
        ob.matrix_world = self.matrix_for(el, depth, (el.w / 2.0, el.h / 2.0))
        return ob

    # SVG icons ------------------------------------------------------------

    def _svg_scale_probe(self) -> Optional[float]:
        """Measure how many metres the SVG importer makes of one SVG px."""
        if self._svg_px_to_m is not None:
            return self._svg_px_to_m
        tmpdir = tempfile.mkdtemp(prefix="figma_probe_")
        path = os.path.join(tmpdir, "probe.svg")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
                '<rect x="0" y="0" width="100" height="100" fill="#000000"/></svg>'
            )
        try:
            objs, colls = _import_svg(path)
            bb = world_bbox(objs)
            if bb is None:
                raise RuntimeError("probe produced no geometry")
            self._svg_px_to_m = (bb[1].x - bb[0].x) / 100.0
        except Exception as e:  # noqa: BLE001
            self.report.warnings.append("SVG scale probe failed (%s); falling back to bounding-box fitting" % e)
            self._svg_px_to_m = 0.0
            objs, colls = [], []
        for ob in objs:
            data = ob.data
            bpy.data.objects.remove(ob)
            if data and data.users == 0:
                bpy.data.curves.remove(data)
        for c in colls:
            if not c.objects and not c.children:
                bpy.data.collections.remove(c)
        try:
            os.remove(path)
            os.rmdir(tmpdir)
        except OSError:
            pass
        return self._svg_px_to_m

    def build_svg_icon(self, el: Element, depth: int, path: str) -> "bpy.types.Object":
        objs, colls = _import_svg(path)
        empty = self._new_object(el, el.name, None)
        empty.empty_display_type = "PLAIN_AXES"
        empty.empty_display_size = max(0.005, min(el.w, el.h) * self.opt.scale * 0.5)
        s = self.opt.scale

        k = self._svg_scale_probe() or 0.0
        doc = svg_document_size(path)
        bb = world_bbox(objs)
        target_w, target_h = el.w * s, el.h * s
        if k > 0 and doc:
            # exact: SVG (0,0) is the node's top-left, importer puts it at (0, doc_h * k)
            doc_w, doc_h = doc
            factor = target_w / (doc_w * k) if doc_w > 0 else target_h / (doc_h * k)
            fit = Matrix.Scale(factor, 4) @ Matrix.Translation((0.0, -doc_h * k, 0.0))
        elif bb is not None:
            bw, bh = bb[1].x - bb[0].x, bb[1].y - bb[0].y
            factor = target_w / bw if bw > 1e-12 else (target_h / bh if bh > 1e-12 else 1.0)
            fit = Matrix.Scale(factor, 4) @ Matrix.Translation((-bb[0].x, -bb[1].y, 0.0))
            self.report.warnings.append("Icon %r: SVG has no viewBox, fitted by bounding box" % el.name)
        else:
            fit = Matrix.Identity(4)
            self.report.warnings.append("Icon %r: SVG produced no geometry" % el.name)

        empty.matrix_world = self.matrix_for(el, depth, (0.0, 0.0)) @ fit

        for i, ob in enumerate(objs):
            ob.name = "%s.%d" % (el.name, i) if len(objs) > 1 else "%s.curve" % el.name
            for c in list(ob.users_collection):
                c.objects.unlink(ob)
            self.report.collection.objects.link(ob)
            ob.parent = empty
            ob.matrix_parent_inverse = Matrix.Identity(4)
            ob["figma_id"] = empty["figma_id"]
            ob["figma_type"] = el.figma_type
            ob["figma_kind"] = "icon_curve"
            self.report.objects.append(ob)
        for c in colls:
            if not c.objects and not c.children:
                bpy.data.collections.remove(c)
        return empty

    # Text -----------------------------------------------------------------

    def _text_probe(self) -> float:
        """Ratio (baseline y / size) Blender uses for TOP alignment inside a text box."""
        if self._text_baseline_ratio is not None:
            return self._text_baseline_ratio
        ratio = 0.2  # measured default for Blender 3.6 - 5.0
        try:
            cu = bpy.data.curves.new("_figma_probe", "FONT")
            cu.body = "H"
            cu.size = 1.0
            cu.align_x = "LEFT"
            cu.align_y = "TOP"
            cu.text_boxes[0].width = 10.0
            cu.text_boxes[0].height = 10.0
            ob = bpy.data.objects.new("_figma_probe", cu)
            bpy.context.scene.collection.objects.link(ob)
            bpy.context.view_layer.update()
            ev = ob.evaluated_get(bpy.context.evaluated_depsgraph_get())
            ratio = min(Vector(c).y for c in ev.bound_box)
            bpy.data.objects.remove(ob)
            bpy.data.curves.remove(cu)
        except Exception as e:  # noqa: BLE001
            log.debug("text probe failed: %s", e)
        self._text_baseline_ratio = ratio
        return ratio

    def _load_font(self, info: dict) -> Optional["bpy.types.VectorFont"]:
        key = (info.get("fontFamily"), info.get("fontPostScriptName"), info.get("fontWeight"), info.get("italic"))
        if key in self._font_cache:
            return self._font_cache[key]
        path = fonts.find_font(
            family=info.get("fontFamily"),
            postscript_name=info.get("fontPostScriptName"),
            weight=info.get("fontWeight"),
            italic=bool(info.get("italic")),
        )
        font = None
        if path:
            try:
                font = bpy.data.fonts.load(path, check_existing=True)
            except RuntimeError as e:
                self.report.warnings.append("Could not load font %s: %s" % (path, e))
        self._font_cache[key] = font
        return font

    def build_text(self, el: Element, depth: int) -> "bpy.types.Object":
        info = el.text or {}
        s = self.opt.scale
        fs = float(info.get("fontSize") or 12.0)
        cu = bpy.data.curves.new(el.name, "FONT")
        body = info.get("characters", "")
        case = info.get("textCase")
        if case == "UPPER":
            body = body.upper()
        elif case == "LOWER":
            body = body.lower()
        elif case == "TITLE":
            body = body.title()
        cu.body = body
        cu.size = fs * s
        cu.align_x = {"LEFT": "LEFT", "CENTER": "CENTER", "RIGHT": "RIGHT", "JUSTIFIED": "JUSTIFY"}.get(
            info.get("textAlignHorizontal", "LEFT"), "LEFT"
        )
        cu.align_y = {"TOP": "TOP", "CENTER": "CENTER", "BOTTOM": "BOTTOM"}.get(info.get("textAlignVertical", "TOP"), "TOP")

        lh = info.get("lineHeightPx")
        if lh and fs > 0:
            cu.space_line = float(lh) / fs
        else:
            lh = fs * 1.2
        ls = info.get("letterSpacing")
        if ls:
            cu.space_character = max(0.1, 1.0 + float(ls) / (fs * 0.5))

        auto = info.get("textAutoResize", "NONE")
        tb = cu.text_boxes[0]
        anchor_x = 0.0
        if auto == "WIDTH_AND_HEIGHT":
            tb.width = 0.0  # never wrap auto-width text; align about the anchor instead
            anchor_x = {"CENTER": el.w / 2.0, "RIGHT": el.w}.get(info.get("textAlignHorizontal", "LEFT"), 0.0)
        else:
            tb.width = el.w * s
        tb.height = max(el.h * s, 1e-6)

        font = self._load_font(info)
        if font is not None:
            cu.font = font
        else:
            fam = info.get("fontFamily") or "?"
            if fam not in self.report.missing_fonts:
                self.report.missing_fonts.append(fam)

        ob = self._new_object(el, el.name, cu)
        if font is None:
            ob["figma_font"] = info.get("fontFamily") or ""
            ob["figma_font_postscript"] = info.get("fontPostScriptName") or ""
        color = el.fill or [0.0, 0.0, 0.0, 1.0]
        alpha = color[3] * el.opacity if len(color) > 3 else el.opacity
        cu.materials.append(self.materials.flat(color, alpha))

        # Vertical placement: put Blender's first baseline where Figma's is.
        blender_baseline = self._text_probe() * cu.size  # relative to object origin, y up
        figma_baseline = -((float(lh) - fs) / 2.0 + TEXT_ASCENT_RATIO * fs) * s  # from box top, y up
        dy = figma_baseline - blender_baseline
        ob.matrix_world = self.matrix_for(el, depth, (anchor_x, 0.0)) @ Matrix.Translation((0.0, dy, 0.0))
        return ob

    # -- driver ---------------------------------------------------------------

    def build(self) -> BuildReport:
        name = self.opt.collection_name or self.scene.page_name or "Figma Page"
        coll = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
        coll["figma_page_id"] = self.scene.page_id
        coll["figma_file_key"] = self.scene.file_key
        self.report.collection = coll
        self.report.warnings.extend(self.scene.warnings)

        svg_ok = svg_importer_available()
        if self.opt.icon_mode == "SVG" and not svg_ok:
            self.report.warnings.append("SVG importer (import_curve.svg) not available; icons imported as planes")

        for depth, el in enumerate(self.scene.elements):
            try:
                ob = self._build_element(el, depth, svg_ok)
            except Exception as e:  # noqa: BLE001 - keep going, report the failure
                log.exception("Failed to build %s", el.id)
                self.report.warnings.append("Failed to build %r (%s): %s" % (el.name, el.kind, e))
                continue
            if ob is None:
                continue
            self.objects[el.id] = ob
            self._parent(ob, el)
            self.report.bump(el.kind)
        return self.report

    def _build_element(self, el: Element, depth: int, svg_ok: bool) -> Optional["bpy.types.Object"]:
        if el.kind == "group":
            return self.build_group(el, depth)
        if el.kind in ("rect", "ellipse"):
            return self.build_shape(el, depth)
        if el.kind == "text":
            return self.build_text(el, depth)
        if el.kind in ("image", "icon"):
            path = self._asset_path(el)
            if path is None:
                if el.fill:
                    self.report.warnings.append("No asset for %r; using its solid fill" % el.name)
                    return self.build_shape(el, depth)
                self.report.warnings.append("No asset for %r; skipped" % el.name)
                return None
            is_svg = path.lower().endswith(".svg")
            if el.kind == "icon" and is_svg and self.opt.icon_mode == "SVG" and svg_ok:
                try:
                    return self.build_svg_icon(el, depth, path)
                except Exception as e:  # noqa: BLE001
                    self.report.warnings.append("SVG import failed for %r (%s)" % (el.name, e))
                    if el.fill:
                        return self.build_shape(el, depth)
                    return None
            if is_svg:
                # SVG asset but PLANE mode (or no importer): planes need a raster
                self.report.warnings.append(
                    "Icon %r is an SVG but icon mode is PLANE; re-export the bundle with icon format PNG. Using solid fill." % el.name
                )
                return self.build_shape(el, depth) if el.fill else None
            return self.build_plane(el, depth, path)
        self.report.warnings.append("Unknown element kind %r for %r" % (el.kind, el.name))
        return None


def build_scene(scene: Scene, bundle_dir: str, options: Optional[BuildOptions] = None) -> BuildReport:
    """Build ``scene`` into the current Blender scene and return a report."""
    return SceneBuilder(scene, bundle_dir, options).build()


def build_bundle(bundle_dir: str, options: Optional[BuildOptions] = None) -> BuildReport:
    """Load ``bundle_dir/scene.json`` and build it."""
    return build_scene(load_scene(bundle_dir), bundle_dir, options)


def add_preview_camera(report: BuildReport, options: BuildOptions, scene: Scene, margin: float = 1.1) -> "bpy.types.Object":
    """Add an orthographic camera framing the imported page (handy for tests / turntables)."""
    b = scene.bounds or {"x": 0, "y": 0, "w": 1, "h": 1}
    w, h = b["w"] * options.scale, b["h"] * options.scale
    cam_data = bpy.data.cameras.new("Figma Camera")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = max(w, h) * margin
    cam = bpy.data.objects.new("Figma Camera", cam_data)
    report.collection.objects.link(cam)
    if options.center:
        cx, cy = 0.0, 0.0
    else:
        cx, cy = (b["x"] + b["w"] / 2.0) * options.scale, -(b["y"] + b["h"] / 2.0) * options.scale
    dist = 1.0 + len(scene.elements) * options.depth_step
    if options.plane_orientation == "XY":
        cam.location = (cx, cy, dist)
        cam.rotation_euler = (0.0, 0.0, 0.0)
    else:
        cam.location = (cx, -dist, cy)
        cam.rotation_euler = (math.pi / 2.0, 0.0, 0.0)
    bpy.context.scene.camera = cam
    return cam
