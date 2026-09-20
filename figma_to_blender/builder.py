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

Non-destructive by design
-------------------------
Nothing that Blender can express as an object property, modifier or curve
parameter is baked into geometry: rectangles are plain 4-vertex planes with a
"Corner Radius" Bevel modifier (per-corner radii as vertex bevel weights),
ellipses are 2D Bezier curves, SVG icons keep the importer's curves and are
sized through the parent Empty's transform, rotation lives on the object and
colour lives in the material.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import bpy
from mathutils import Matrix, Vector

from . import fonts
from .scene_model import Element, Scene, load_scene

log = logging.getLogger(__name__)

CORNER_SEGMENTS = 8  # default Bevel modifier segments per rounded corner
ELLIPSE_RESOLUTION = 24  # curve resolution_u (points per Bezier segment) for ellipses
CORNER_MODIFIER_NAME = "Corner Radius"
BEVEL_WEIGHT_ATTR = "bevel_weight_vert"  # mesh attribute holding per-vertex bevel weights
BEZIER_CIRCLE_K = 0.5522847498  # handle length / radius for a 4-point Bezier circle
TEXT_ASCENT_RATIO = 0.8  # approximate ascender / font size used to place Figma baselines
MANAGED_PROP = "figma_managed"  # custom property marking materials the importer created (re-sync may swap them)
ELEM_ID_PROP = "figma_elem_id"  # exact scene element id (``figma_id`` plus ``:bg`` / ``#n`` suffixes) used by re-sync
REMOVED_SUFFIX = " (removed)"  # sub-collection receiving objects whose Figma element disappeared


@dataclass
class BuildOptions:
    scale: float = 0.001  # metres per Figma px
    depth_step: float = 0.0005  # metres per draw-order step
    icon_mode: str = "SVG"  # "SVG" (curves) | "PLANE" (textured plane)
    plane_orientation: str = "XZ"  # "XZ" | "XY"
    center: bool = True  # put the page bounds' centre at the world origin
    corner_segments: int = CORNER_SEGMENTS  # Bevel modifier segments for rounded corners
    collection_name: Optional[str] = None
    update_existing: bool = True  # re-sync: update objects of an earlier import of the same page/frame in place
    remove_missing: bool = False  # re-sync: delete objects whose element vanished (default: move to "<root> (removed)")


@dataclass
class BuildReport:
    counts: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    missing_fonts: List[str] = field(default_factory=list)
    objects: List["bpy.types.Object"] = field(default_factory=list)
    collection: Optional["bpy.types.Collection"] = None
    # re-sync tallies: created / updated (in place) / moved (to the removed collection) / removed (deleted)
    sync: Dict[str, int] = field(default_factory=lambda: {"created": 0, "updated": 0, "moved": 0, "removed": 0})

    def bump(self, kind: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def summary(self) -> str:
        parts = ["%s=%d" % kv for kv in sorted(self.counts.items())]
        s = "Imported " + (", ".join(parts) if parts else "nothing")
        if any(self.sync.get(k) for k in ("updated", "moved", "removed")):
            s += "; sync: " + ", ".join("%s %d" % (k, self.sync[k]) for k in ("created", "updated", "moved", "removed") if self.sync.get(k))
        if self.missing_fonts:
            s += "; fonts not found: " + ", ".join(sorted(set(self.missing_fonts)))
        if self.warnings:
            s += "; %d warning(s)" % len(self.warnings)
        return s


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------


def set_material_blend(mat: "bpy.types.Material", alpha: float) -> None:
    """Enable EEVEE alpha blending for a translucent fill (Blender 5.0 ``surface_render_method``)."""
    if alpha >= 0.999:
        return
    mat.surface_render_method = "BLENDED"
    mat.show_transparent_back = False


def _emission_output(mat: "bpy.types.Material"):
    """Reset the node tree to Emission -> Output and return (nodes, links, emission, output)."""
    nt = mat.node_tree  # node trees are always on in Blender 5.0
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


def is_managed_material(mat: Optional["bpy.types.Material"]) -> bool:
    """True for materials the importer made (re-sync may swap them; user materials are left alone)."""
    return mat is not None and (bool(mat.get(MANAGED_PROP)) or mat.name.startswith("Figma "))


def _new_managed_material(name: str) -> "bpy.types.Material":
    mat = bpy.data.materials.new(name)
    mat[MANAGED_PROP] = True
    return mat


class MaterialCache:
    """Materials shared per colour / image, reused across imports by their deterministic name."""

    def __init__(self):
        self._flat: Dict[Tuple[int, int, int, int], "bpy.types.Material"] = {}
        self._image: Dict[Tuple[str, int], "bpy.types.Material"] = {}

    @staticmethod
    def _existing(name: str) -> Optional["bpy.types.Material"]:
        mat = bpy.data.materials.get(name)
        return mat if is_managed_material(mat) else None

    def flat(self, color, alpha: float) -> "bpy.types.Material":
        r, g, b = (float(c) for c in color[:3])
        key = (round(r * 255), round(g * 255), round(b * 255), round(alpha * 255))
        mat = self._flat.get(key)
        if mat is None:
            name = "Figma %02X%02X%02X a%d" % key
            mat = self._existing(name) or make_flat_material(name, (r, g, b), alpha)
            self._flat[key] = mat
        return mat

    def image(self, image: "bpy.types.Image", alpha: float) -> "bpy.types.Material":
        key = (image.name, round(alpha * 255))
        mat = self._image.get(key)
        if mat is None:
            name = "Figma img %s a%d" % (image.name, key[1])
            mat = self._existing(name) or make_image_material(name, image, alpha)
            self._image[key] = mat
        return mat


def make_flat_material(name: str, color, alpha: float = 1.0) -> "bpy.types.Material":
    """Unlit (emission) material of a single colour, optionally transparent."""
    mat = _new_managed_material(name)
    r, g, b = (float(c) for c in color[:3])
    nodes, links, em, out = _emission_output(mat)
    em.inputs["Color"].default_value = (r, g, b, 1.0)
    _link_with_alpha(nodes, links, em, out, alpha_value=alpha)
    mat.diffuse_color = (r, g, b, alpha)
    set_material_blend(mat, alpha)
    return mat


def make_image_material(name: str, image: "bpy.types.Image", alpha: float = 1.0) -> "bpy.types.Material":
    """Unlit material showing ``image`` with its own alpha (times ``alpha``)."""
    mat = _new_managed_material(name)
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
#
# Shapes are kept editable: a rectangle is a 4-vertex plane whose rounded
# corners come from a Bevel modifier, an ellipse is a Bezier curve.
# ---------------------------------------------------------------------------


def plane_mesh(name: str, w: float, h: float) -> "bpy.types.Mesh":
    """A w x h quad centred on the origin (y up) with a 0..1 UV layer.

    Vertex order follows Figma's corner order ``[tl, tr, br, bl]`` so that
    ``rectangleCornerRadii`` maps 1:1 onto vertex bevel weights.
    """
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0.0, 0.0, 0.0)] * 4, [], [(0, 3, 2, 1)])  # counter-clockwise -> normal towards +Z (the viewer)
    mesh.uv_layers.new(name="UVMap")
    set_plane_geometry(mesh, w, h)
    return mesh


def is_plane_mesh(mesh: "bpy.types.Mesh") -> bool:
    return len(mesh.vertices) == 4 and len(mesh.polygons) == 1


def set_plane_geometry(mesh: "bpy.types.Mesh", w: float, h: float) -> None:
    """Resize a :func:`plane_mesh` in place (vertices + UVs), keeping the datablock and its users."""
    hw, hh = w / 2.0, h / 2.0
    verts = [(-hw, hh, 0.0), (hw, hh, 0.0), (hw, -hh, 0.0), (-hw, -hh, 0.0)]  # tl, tr, br, bl
    for v, co in zip(mesh.vertices, verts):
        v.co = co
    uv = mesh.uv_layers.active or (mesh.uv_layers[0] if mesh.uv_layers else mesh.uv_layers.new(name="UVMap"))
    for poly in mesh.polygons:
        for li in poly.loop_indices:
            x, y, _ = mesh.vertices[mesh.loops[li].vertex_index].co
            uv.data[li].uv = ((x + hw) / w if w else 0.5, (y + hh) / h if h else 0.5)
    mesh.update()


def set_vertex_bevel_weights(mesh: "bpy.types.Mesh", weights: List[float]) -> None:
    """Write per-vertex bevel weights into the ``bevel_weight_vert`` mesh attribute."""
    attr = mesh.attributes.get(BEVEL_WEIGHT_ATTR)
    if attr is None:
        attr = mesh.attributes.new(BEVEL_WEIGHT_ATTR, "FLOAT", "POINT")
    for i, wgt in enumerate(weights):
        attr.data[i].value = float(wgt)


def vertex_bevel_weights(mesh: "bpy.types.Mesh") -> List[float]:
    """Read back what :func:`set_vertex_bevel_weights` wrote (used by tests and re-sync)."""
    attr = mesh.attributes.get(BEVEL_WEIGHT_ATTR)
    if attr is None:
        return [0.0] * len(mesh.vertices)
    return [float(d.value) for d in attr.data]


def corner_radius_weights(w: float, h: float, radii: Optional[List[float]]) -> Tuple[float, List[float]]:
    """``(bevel width, [tl, tr, br, bl] weights)`` for a w x h rectangle.

    Radii are clamped to ``min(w, h) / 2``.  When every radius is 0 the width
    is 0 and all weights are 1 so a radius can be dialled in on the modifier.
    """
    cap = min(w, h) / 2.0
    vals = [max(0.0, min(float(r), cap)) for r in (radii or [0.0, 0.0, 0.0, 0.0])]
    while len(vals) < 4:
        vals.append(vals[-1] if vals else 0.0)
    rmax = max(vals[:4])
    if rmax <= 0.0:
        return 0.0, [1.0, 1.0, 1.0, 1.0]
    return rmax, [r / rmax for r in vals[:4]]


def add_corner_radius_modifier(
    ob: "bpy.types.Object", w: float, h: float, radii: Optional[List[float]], segments: int = CORNER_SEGMENTS
) -> "bpy.types.Modifier":
    """Round the corners of a :func:`plane_mesh` object with a Bevel modifier.

    The modifier only touches vertices, is limited by vertex bevel weight, and
    its ``width`` is the largest corner radius; smaller corners get a weight of
    ``radius / max_radius`` so Figma's per-corner radii survive.  Turning the
    modifier off (or deleting it) gives the sharp rectangle back.
    """
    width, weights = corner_radius_weights(w, h, radii)
    set_vertex_bevel_weights(ob.data, weights)
    mod = ob.modifiers.get(CORNER_MODIFIER_NAME)
    if mod is None or mod.type != "BEVEL":
        mod = ob.modifiers.new(CORNER_MODIFIER_NAME, "BEVEL")
        mod.affect = "VERTICES"
        mod.limit_method = "WEIGHT"
        mod.offset_type = "OFFSET"
        mod.show_expanded = False
    mod.width = width
    mod.segments = max(1, int(segments))
    return mod


def ellipse_curve(name: str, w: float, h: float, resolution: int = ELLIPSE_RESOLUTION) -> "bpy.types.Curve":
    """Filled 2D Bezier ellipse (4 aligned control points) spanning w x h, centred on the origin.

    The size lives in the control points rather than the object scale so the
    object's scale stays 1 like every other imported shape.
    """
    cu = bpy.data.curves.new(name, "CURVE")
    cu.dimensions = "2D"
    cu.fill_mode = "BOTH"
    cu.resolution_u = max(1, int(resolution))
    sp = cu.splines.new("BEZIER")
    sp.bezier_points.add(3)
    sp.use_cyclic_u = True
    set_ellipse_points(cu, w, h)
    return cu


def is_ellipse_curve(cu: "bpy.types.Curve") -> bool:
    return len(cu.splines) == 1 and cu.splines[0].type == "BEZIER" and len(cu.splines[0].bezier_points) == 4


def set_ellipse_points(cu: "bpy.types.Curve", w: float, h: float) -> None:
    """Resize an :func:`ellipse_curve` in place through its four control points."""
    sp = cu.splines[0]
    rx, ry = w / 2.0, h / 2.0
    kx, ky = rx * BEZIER_CIRCLE_K, ry * BEZIER_CIRCLE_K
    # counter-clockwise: right, top, left, bottom; (co, handle_left, handle_right)
    points = (
        ((rx, 0.0), (rx, -ky), (rx, ky)),
        ((0.0, ry), (kx, ry), (-kx, ry)),
        ((-rx, 0.0), (-rx, ky), (-rx, -ky)),
        ((0.0, -ry), (-kx, -ry), (kx, -ry)),
    )
    for bp, (co, hl, hr) in zip(sp.bezier_points, points):
        bp.handle_left_type = bp.handle_right_type = "FREE"
        bp.co = (co[0], co[1], 0.0)
        bp.handle_left = (hl[0], hl[1], 0.0)
        bp.handle_right = (hr[0], hr[1], 0.0)
        bp.handle_left_type = bp.handle_right_type = "ALIGNED"


def file_hash(path: str) -> str:
    """Short content hash of an asset file (detects changed icons / images on re-sync)."""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def font_key(font: Optional["bpy.types.VectorFont"]) -> str:
    """Identify the font on a text curve; the built-in default font is ``""``."""
    if font is None or font.filepath in ("", "<builtin>"):
        return ""
    return font.filepath


def legacy_elem_id(ob: "bpy.types.Object") -> Optional[str]:
    """Element id of an object imported before ``figma_elem_id`` existed (v0.2)."""
    fid = ob.get("figma_id")
    if not fid or ob.get("figma_kind") == "icon_curve":
        return None
    if ob.get("figma_kind") == "rect" and " (background)" in ob.name:
        return fid + ":bg"
    return fid


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
    """Feature check, not a version check: the bundled SVG importer can be disabled or missing in custom builds."""
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
#
# Every ``build_*`` method takes an optional existing object (found by element
# id in the target collection) and then works as an *update*: it moves the
# vertices / control points / text properties of the datablock it already has
# and leaves everything else (extra modifiers, user materials, custom
# properties) alone.  Without an existing object it creates one, so creation
# and re-sync share one code path.
# ---------------------------------------------------------------------------


class SceneBuilder:
    def __init__(self, scene: Scene, bundle_dir: str, options: Optional[BuildOptions] = None):
        self.scene = scene
        self.bundle_dir = bundle_dir
        self.opt = options or BuildOptions()
        self.report = BuildReport()
        self.materials = MaterialCache()
        self.objects: Dict[str, "bpy.types.Object"] = {}
        self.existing: Dict[str, "bpy.types.Object"] = {}  # element id -> object of an earlier import
        self._removed_coll: Optional["bpy.types.Collection"] = None
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
        self._tag(ob, el)
        self.report.sync["created"] += 1
        self.report.objects.append(ob)
        return ob

    @staticmethod
    def _tag(ob: "bpy.types.Object", el: Element) -> None:
        ob["figma_id"] = el.id.split("#", 1)[0].split(":bg", 1)[0]
        ob[ELEM_ID_PROP] = el.id
        ob["figma_type"] = el.figma_type
        ob["figma_name"] = el.name
        ob["figma_kind"] = el.kind

    def _reuse(self, ob: Optional["bpy.types.Object"], el: Element, ob_type: str) -> Optional["bpy.types.Object"]:
        """Return ``ob`` when it can be updated into ``el`` (same object type), else retire it."""
        if ob is None:
            return None
        if ob.type != ob_type:
            self._retire(ob)
            return None
        if ob.name == ob.get("figma_name") and el.name != ob.name:
            ob.name = el.name  # follow a Figma rename unless the user renamed the object
        self._tag(ob, el)
        self.report.sync["updated"] += 1
        self.report.objects.append(ob)
        if ob.name not in self.report.collection.objects:
            self.report.collection.objects.link(ob)
        return ob

    def _place(self, ob: "bpy.types.Object", matrix: Matrix) -> None:
        """Set the world matrix from Figma; parenting is (re)applied by :meth:`build`."""
        ob.parent = None
        ob.matrix_parent_inverse = Matrix.Identity(4)
        ob.matrix_world = matrix

    def _parent(self, ob: "bpy.types.Object", el: Element) -> None:
        parent = self.objects.get(el.parent) if el.parent else None
        if parent is None or parent == ob:
            return
        ob.parent = parent
        ob.matrix_parent_inverse = parent.matrix_world.inverted()

    @staticmethod
    def _apply_material(data, mat: "bpy.types.Material") -> None:
        """Assign ``mat`` to slot 0 unless the user replaced the importer's material there."""
        slots = data.materials
        if len(slots) == 0:
            slots.append(mat)
        elif slots[0] is None or is_managed_material(slots[0]):
            if slots[0] is not mat:
                slots[0] = mat

    def _icon_curves(self, empty: "bpy.types.Object") -> List["bpy.types.Object"]:
        return [o for o in bpy.data.objects if o.parent == empty and o.get("figma_kind") == "icon_curve"]

    def _removed_collection(self) -> "bpy.types.Collection":
        if self._removed_coll is None:
            name = self.report.collection.name + REMOVED_SUFFIX
            coll = next((c for c in self.report.collection.children if c.name.startswith(name)), None)
            if coll is None:
                coll = bpy.data.collections.new(name)
                self.report.collection.children.link(coll)
            self._removed_coll = coll
        return self._removed_coll

    def _retire(self, ob: "bpy.types.Object") -> None:
        """An element vanished from Figma: delete its object or park it in the removed collection."""
        for o in [ob] + self._icon_curves(ob):
            if self.opt.remove_missing:
                data = o.data
                bpy.data.objects.remove(o)
                if data is not None and data.users == 0:
                    for store in (bpy.data.meshes, bpy.data.curves):
                        if data.name in store and store[data.name] == data:
                            store.remove(data)
                            break
            else:
                removed = self._removed_collection()
                for c in list(o.users_collection):
                    c.objects.unlink(o)
                removed.objects.link(o)
        self.report.sync["removed" if self.opt.remove_missing else "moved"] += 1

    def _asset_path(self, el: Element) -> Optional[str]:
        if not el.asset:
            return None
        p = os.path.join(self.bundle_dir, el.asset)
        if not os.path.exists(p):
            self.report.warnings.append("Asset missing for %r: %s" % (el.name, el.asset))
            return None
        return p

    # -- element builders ---------------------------------------------------

    def build_group(self, el: Element, depth: int, ob: Optional["bpy.types.Object"] = None) -> "bpy.types.Object":
        if ob is None:
            ob = self._new_object(el, el.name, None)
            ob.empty_display_type = "PLAIN_AXES"
        ob.empty_display_size = max(0.01, min(el.w, el.h) * self.opt.scale * 0.25)
        self._place(ob, self.matrix_for(el, depth, (0.0, 0.0)))
        return ob

    def _rect_object(
        self, el: Element, radii: Optional[List[float]], always_modifier: bool, ob: Optional["bpy.types.Object"] = None
    ) -> "bpy.types.Object":
        """Plane object at the element's size (object scale 1) plus a Corner Radius Bevel modifier.

        On update the four vertices are moved; the mesh datablock, its material
        slots and every modifier stay.  A mesh the user edited into something
        else is replaced by a fresh plane (materials carried over).
        """
        s = self.opt.scale
        w, h = max(el.w, 1e-6) * s, max(el.h, 1e-6) * s
        if ob is None:
            ob = self._new_object(el, el.name, plane_mesh(el.name, w, h))
        elif is_plane_mesh(ob.data):
            set_plane_geometry(ob.data, w, h)
        else:
            self.report.warnings.append("Mesh of %r was edited; replaced by a fresh plane" % ob.name)
            old = ob.data
            ob.data = plane_mesh(el.name, w, h)
            for m in old.materials:
                ob.data.materials.append(m)
        scaled = [r * s for r in radii] if radii else None
        if scaled or always_modifier or ob.modifiers.get(CORNER_MODIFIER_NAME) is not None:
            add_corner_radius_modifier(ob, w, h, scaled, self.opt.corner_segments)
        return ob

    def build_shape(self, el: Element, depth: int, ob: Optional["bpy.types.Object"] = None) -> "bpy.types.Object":
        s = self.opt.scale
        w, h = max(el.w, 1e-6) * s, max(el.h, 1e-6) * s
        if el.kind == "ellipse":
            if ob is None:
                ob = self._new_object(el, el.name, ellipse_curve(el.name, w, h))
            elif is_ellipse_curve(ob.data):
                set_ellipse_points(ob.data, w, h)
            else:
                self.report.warnings.append("Curve of %r was edited; replaced by a fresh ellipse" % ob.name)
                old = ob.data
                ob.data = ellipse_curve(el.name, w, h)
                for m in old.materials:
                    ob.data.materials.append(m)
        else:
            # rect (or an icon / image falling back to its solid fill): always carry
            # the modifier so a radius can be added later without touching the mesh
            ob = self._rect_object(el, el.corner_radii, True, ob)
        color = el.fill or [0.5, 0.5, 0.5, 1.0]
        alpha = color[3] * el.opacity if len(color) > 3 else el.opacity
        self._apply_material(ob.data, self.materials.flat(color, alpha))
        if el.fill_approx:
            ob["figma_fill_approx"] = True
        elif "figma_fill_approx" in ob:
            del ob["figma_fill_approx"]
        self._place(ob, self.matrix_for(el, depth, (el.w / 2.0, el.h / 2.0)))
        return ob

    @staticmethod
    def _current_image(data) -> Optional["bpy.types.Image"]:
        """The image shown by the importer's material in slot 0, if any."""
        if not data.materials or not is_managed_material(data.materials[0]) or not data.materials[0].node_tree:
            return None
        for node in data.materials[0].node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None:
                return node.image
        return None

    def build_plane(self, el: Element, depth: int, path: str, ob: Optional["bpy.types.Object"] = None) -> "bpy.types.Object":
        # plain textured quad; image fills with corner radii get the same Bevel modifier
        radii = el.corner_radii if (el.kind == "image" and el.corner_radii) else None
        ob = self._rect_object(el, radii, always_modifier=False, ob=ob)
        mesh = ob.data
        digest = file_hash(path)
        image = self._current_image(mesh) if ob.get("figma_asset_hash") == digest else None
        try:
            if image is None:
                image = bpy.data.images.load(path, check_existing=True)
            self._apply_material(mesh, self.materials.image(image, el.opacity))
            ob["figma_asset_hash"] = digest
        except RuntimeError as e:
            self.report.warnings.append("Could not load image %s for %r: %s" % (path, el.name, e))
            self._apply_material(mesh, self.materials.flat(el.fill or [0.5, 0.5, 0.5, 1.0], el.opacity))
        self._place(ob, self.matrix_for(el, depth, (el.w / 2.0, el.h / 2.0)))
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

    def _svg_fit(self, el: Element, path: str, objs: List["bpy.types.Object"]) -> Matrix:
        """Matrix scaling the freshly imported SVG curves to the node size (SVG top-left at the origin)."""
        s = self.opt.scale
        k = self._svg_scale_probe() or 0.0
        doc = svg_document_size(path)
        bb = world_bbox(objs)
        target_w, target_h = el.w * s, el.h * s
        if k > 0 and doc:
            # exact: SVG (0,0) is the node's top-left, importer puts it at (0, doc_h * k)
            doc_w, doc_h = doc
            factor = target_w / (doc_w * k) if doc_w > 0 else target_h / (doc_h * k)
            return Matrix.Scale(factor, 4) @ Matrix.Translation((0.0, -doc_h * k, 0.0))
        if bb is not None:
            bw, bh = bb[1].x - bb[0].x, bb[1].y - bb[0].y
            factor = target_w / bw if bw > 1e-12 else (target_h / bh if bh > 1e-12 else 1.0)
            self.report.warnings.append("Icon %r: SVG has no viewBox, fitted by bounding box" % el.name)
            return Matrix.Scale(factor, 4) @ Matrix.Translation((-bb[0].x, -bb[1].y, 0.0))
        self.report.warnings.append("Icon %r: SVG produced no geometry" % el.name)
        return Matrix.Identity(4)

    def build_svg_icon(self, el: Element, depth: int, path: str, empty: Optional["bpy.types.Object"] = None) -> "bpy.types.Object":
        """Empty sized to the node with the SVG importer's curves as children.

        The curves are generated, so on update they are re-imported when the SVG
        changed (content hash) and kept as they are otherwise; the Empty itself
        (and anything the user hung on it) always survives.
        """
        digest = file_hash(path)
        if empty is None:
            empty = self._new_object(el, el.name, None)
            empty.empty_display_type = "PLAIN_AXES"
        empty.empty_display_size = max(0.005, min(el.w, el.h) * self.opt.scale * 0.5)

        old_curves = self._icon_curves(empty)
        fit_prop = empty.get("figma_icon_fit")
        if old_curves and empty.get("figma_asset_hash") == digest and fit_prop is not None and len(fit_prop) == 16:
            fit = Matrix([tuple(fit_prop[i * 4 : i * 4 + 4]) for i in range(4)])
            self.report.objects.extend(old_curves)
        else:
            for o in old_curves:
                data = o.data
                bpy.data.objects.remove(o)
                if data is not None and data.users == 0:
                    bpy.data.curves.remove(data)
            objs, colls = _import_svg(path)
            fit = self._svg_fit(el, path, objs)
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
            empty["figma_asset_hash"] = digest
            empty["figma_icon_fit"] = [v for row in fit for v in row]

        self._place(empty, self.matrix_for(el, depth, (0.0, 0.0)) @ fit)
        return empty

    # Text -----------------------------------------------------------------

    def _text_probe(self) -> float:
        """Ratio (baseline y / size) Blender uses for TOP alignment inside a text box."""
        if self._text_baseline_ratio is not None:
            return self._text_baseline_ratio
        ratio = 0.2  # measured default for Blender 5.0
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

    def build_text(self, el: Element, depth: int, ob: Optional["bpy.types.Object"] = None) -> "bpy.types.Object":
        info = el.text or {}
        s = self.opt.scale
        fs = float(info.get("fontSize") or 12.0)
        if ob is None:
            ob = self._new_object(el, el.name, bpy.data.curves.new(el.name, "FONT"))
        cu = ob.data
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
            cu.space_line = 1.0
        ls = info.get("letterSpacing")
        cu.space_character = max(0.1, 1.0 + float(ls) / (fs * 0.5)) if ls else 1.0

        auto = info.get("textAutoResize", "NONE")
        tb = cu.text_boxes[0]
        anchor_x = 0.0
        if auto == "WIDTH_AND_HEIGHT":
            tb.width = 0.0  # never wrap auto-width text; align about the anchor instead
            anchor_x = {"CENTER": el.w / 2.0, "RIGHT": el.w}.get(info.get("textAlignHorizontal", "LEFT"), 0.0)
        else:
            tb.width = el.w * s
        tb.height = max(el.h * s, 1e-6)

        # Font: assign the auto-matched font unless the user picked another one since the last import
        # (``figma_font_file`` remembers what the importer assigned; "" is Blender's built-in font).
        font = self._load_font(info)
        previous = ob.get("figma_font_file")
        if previous is None or previous == font_key(cu.font):
            if font is not None:
                cu.font = font
            ob["figma_font_file"] = font_key(cu.font)
        if font is None:
            fam = info.get("fontFamily") or "?"
            if fam not in self.report.missing_fonts:
                self.report.missing_fonts.append(fam)
        ob["figma_font"] = info.get("fontFamily") or ""
        ob["figma_font_postscript"] = info.get("fontPostScriptName") or ""
        color = el.fill or [0.0, 0.0, 0.0, 1.0]
        alpha = color[3] * el.opacity if len(color) > 3 else el.opacity
        self._apply_material(cu, self.materials.flat(color, alpha))

        # Vertical placement: put Blender's first baseline where Figma's is.
        blender_baseline = self._text_probe() * cu.size  # relative to object origin, y up
        figma_baseline = -((float(lh) - fs) / 2.0 + TEXT_ASCENT_RATIO * fs) * s  # from box top, y up
        dy = figma_baseline - blender_baseline
        self._place(ob, self.matrix_for(el, depth, (anchor_x, 0.0)) @ Matrix.Translation((0.0, dy, 0.0)))
        return ob

    # -- driver ---------------------------------------------------------------

    def _find_collection(self, name: str) -> Optional["bpy.types.Collection"]:
        """An earlier import of the same root (page / frame) in this scene, matched by name and ``figma_page_id``."""
        if not self.opt.update_existing:
            return None
        in_scene = set(bpy.context.scene.collection.children_recursive)
        candidates = [
            c
            for c in in_scene
            if c.get("figma_page_id") == self.scene.page_id
            and (c.name == name or c.name.rsplit(".", 1)[0] == name)
            and not c.name.endswith(REMOVED_SUFFIX)
            and (not self.scene.file_key or not c.get("figma_file_key") or c.get("figma_file_key") == self.scene.file_key)
        ]
        candidates.sort(key=lambda c: (c.name != name, c.name))
        return candidates[0] if candidates else None

    def _index_existing(self, coll: "bpy.types.Collection") -> None:
        for ob in coll.objects:
            eid = ob.get(ELEM_ID_PROP) or legacy_elem_id(ob)
            if eid and eid not in self.existing:
                self.existing[eid] = ob

    def build(self) -> BuildReport:
        name = self.opt.collection_name or self.scene.page_name or "Figma Page"
        coll = self._find_collection(name)
        if coll is None:
            coll = bpy.data.collections.new(name)
            bpy.context.scene.collection.children.link(coll)
        else:
            self._index_existing(coll)
        coll["figma_page_id"] = self.scene.page_id
        coll["figma_file_key"] = self.scene.file_key
        self.report.collection = coll
        self.report.warnings.extend(self.scene.warnings)

        svg_ok = svg_importer_available()
        if self.opt.icon_mode == "SVG" and not svg_ok:
            self.report.warnings.append("SVG importer (import_curve.svg) not available; icons imported as planes")

        for depth, el in enumerate(self.scene.elements):
            existing = self.existing.pop(el.id, None)
            try:
                ob = self._build_element(el, depth, svg_ok, existing)
            except Exception as e:  # noqa: BLE001 - keep going, report the failure
                log.exception("Failed to build %s", el.id)
                self.report.warnings.append("Failed to build %r (%s): %s" % (el.name, el.kind, e))
                continue
            if ob is None:
                continue
            self.objects[el.id] = ob
            self._parent(ob, el)
            self.report.bump(el.kind)

        for ob in list(self.existing.values()):  # elements that vanished from Figma
            self._retire(ob)
        self.existing.clear()
        return self.report

    def _build_element(
        self, el: Element, depth: int, svg_ok: bool, existing: Optional["bpy.types.Object"] = None
    ) -> Optional["bpy.types.Object"]:
        if el.kind == "group":
            return self.build_group(el, depth, self._reuse(existing, el, "EMPTY"))
        if el.kind == "rect":
            return self.build_shape(el, depth, self._reuse(existing, el, "MESH"))
        if el.kind == "ellipse":
            return self.build_shape(el, depth, self._reuse(existing, el, "CURVE"))
        if el.kind == "text":
            return self.build_text(el, depth, self._reuse(existing, el, "FONT"))
        if el.kind in ("image", "icon"):
            path = self._asset_path(el)
            if path is None:
                if el.fill:
                    self.report.warnings.append("No asset for %r; using its solid fill" % el.name)
                    return self.build_shape(el, depth, self._reuse(existing, el, "MESH"))
                self.report.warnings.append("No asset for %r; skipped" % el.name)
                if existing is not None:
                    self._retire(existing)
                return None
            is_svg = path.lower().endswith(".svg")
            if el.kind == "icon" and is_svg and self.opt.icon_mode == "SVG" and svg_ok:
                try:
                    return self.build_svg_icon(el, depth, path, self._reuse(existing, el, "EMPTY"))
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
                if el.fill:
                    return self.build_shape(el, depth, self._reuse(existing, el, "MESH"))
                if existing is not None:
                    self._retire(existing)
                return None
            return self.build_plane(el, depth, path, self._reuse(existing, el, "MESH"))
        self.report.warnings.append("Unknown element kind %r for %r" % (el.kind, el.name))
        return None


def build_scene(scene: Scene, bundle_dir: str, options: Optional[BuildOptions] = None) -> BuildReport:
    """Build ``scene`` into the current Blender scene and return a report.

    With ``options.update_existing`` (default) an earlier import of the same
    page / frame in this scene is updated in place instead of duplicated.
    """
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
