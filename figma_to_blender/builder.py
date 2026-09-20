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
colour lives in the material.  Gradients are shader nodes (Mapping -> Gradient
Texture -> Color Ramp) and strokes are a "Figma Stroke" Geometry Nodes
modifier that outlines the evaluated shape, so nothing is baked.
"""

from __future__ import annotations

import hashlib
import json
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
from .scene_model import Element, Scene, load_scene, shared_signature

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
STROKE_MODIFIER_NAME = "Stroke"
STROKE_NODE_GROUP = "Figma Stroke"  # Geometry Nodes group shared by every stroked object in the .blend
STROKE_LIFT = 0.0001  # metres the stroke ribbon floats above its face so it never z-fights the fill
GRADIENT_MAX_STOPS = 32  # Blender's Color Ramp limit
COMPONENT_PROP = "figma_component"  # custom property on a component's own collection (COLLECTION_INSTANCE mode)
COMPONENT_SUFFIX = " (component)"
INSTANCE_MODES = ("LINKED_DATA", "COLLECTION_INSTANCE")
DEPTH_MODIFIER_NAME = "Depth"  # Solidify on planes (thickness toward the back, front face stays put)
SCREEN_MODIFIER_NAME = "Screen Curve"  # Simple Deform BEND around the shared "<root> Curve Origin" Empty
CURVE_ORIGIN_ID = "__curve_origin__"  # ``figma_elem_id`` of that Empty
DEPTH_PROP = "figma_depth"  # thickness (m) the importer applied; sync only overwrites while the value is unchanged
TEXT_BEVEL_PROP = "figma_text_bevel"
SCREEN_ANGLE_PROP = "figma_curve_angle"
DEPTH_KINDS = ("frame", "button", "shape", "text", "icon", "image")
BUTTON_MAX_SIZE = 400.0  # px: a container background with a TEXT child up to this size counts as a button
# 3D presets, in Figma px (converted with ``BuildOptions.scale``): total thickness per element kind and the
# text bevel.  FLAT is the plain 2D import.
DEPTH_PRESETS: Dict[str, Dict[str, float]] = {
    "FLAT": {"frame": 0.0, "button": 0.0, "shape": 0.0, "text": 0.0, "icon": 0.0, "image": 0.0, "text_bevel": 0.0},
    "SUBTLE": {"frame": 2.0, "button": 3.0, "shape": 1.0, "text": 0.5, "icon": 0.5, "image": 1.0, "text_bevel": 0.0},
    "CARD": {"frame": 8.0, "button": 6.0, "shape": 3.0, "text": 1.5, "icon": 1.5, "image": 3.0, "text_bevel": 0.25},
}


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
    # Component instances: share one mesh / curve / text datablock (and so one material) between a
    # component's element and the matching, non-overridden element of every instance.
    link_instances: bool = True
    # LINKED_DATA: one object per instance element with shared data.  COLLECTION_INSTANCE: the
    # component's objects go into their own collection and each instance is a single Empty
    # instancing it (only for components that are part of the export; others fall back).
    instance_mode: str = "LINKED_DATA"
    # 3D: a DEPTH_PRESETS key (FLAT / SUBTLE / CARD; anything else starts from FLAT) plus optional
    # per-kind overrides in Figma px ({"frame", "button", "shape", "text", "icon", "image", "text_bevel"}).
    depth_preset: str = "FLAT"
    depths: Optional[Dict[str, float]] = None
    # Curved screen: bend every mesh / curve / text object around a shared origin Empty at the
    # frame centre so the UI approximates a cylinder of ``curve_radius`` metres facing the viewer.
    curve_screen: bool = False
    curve_radius: float = 1.0


def resolve_depths(preset: str, overrides: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """Depth per kind (px) for a preset with per-kind overrides applied."""
    depths = dict(DEPTH_PRESETS.get(preset, DEPTH_PRESETS["FLAT"]))
    for k, v in (overrides or {}).items():
        if k in depths and v is not None:
            depths[k] = max(0.0, float(v))
    return depths


@dataclass
class BuildReport:
    counts: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    missing_fonts: List[str] = field(default_factory=list)
    objects: List["bpy.types.Object"] = field(default_factory=list)
    collection: Optional["bpy.types.Collection"] = None
    # re-sync tallies: created / updated (in place) / moved (to the removed collection) / removed (deleted)
    sync: Dict[str, int] = field(default_factory=lambda: {"created": 0, "updated": 0, "moved": 0, "removed": 0})
    linked: int = 0  # objects sharing a component's datablock (LINKED_DATA) or instancing its collection
    overrides: List[str] = field(default_factory=list)  # instance elements that got their own datablock

    def bump(self, kind: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def summary(self) -> str:
        parts = ["%s=%d" % kv for kv in sorted(self.counts.items())]
        s = "Imported " + (", ".join(parts) if parts else "nothing")
        if any(self.sync.get(k) for k in ("updated", "moved", "removed")):
            s += "; sync: " + ", ".join("%s %d" % (k, self.sync[k]) for k in ("created", "updated", "moved", "removed") if self.sync.get(k))
        if self.linked or self.overrides:
            s += "; instances: %d linked" % self.linked
            if self.overrides:
                s += ", %d override(s)" % len(self.overrides)
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

    def gradient(self, gradient: dict, alpha: float, coords: str = "UV") -> "bpy.types.Material":
        """One material per distinct gradient (type, stops, handles, opacity, coordinate source)."""
        digest = hashlib.md5(json.dumps([gradient, round(alpha, 4), coords], sort_keys=True).encode("utf-8")).hexdigest()[:10]
        name = "Figma gradient %s" % digest
        mat = self._existing(name)
        if mat is None:
            mat = make_gradient_material(name, gradient, alpha, coords)
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


def gradient_mapping(gradient: dict) -> Tuple[Tuple[float, float, float], float, Tuple[float, float, float]]:
    """``(location, z_rotation, scale)`` for a *Texture*-type Mapping node.

    Figma handles are normalised to the node box with y down; the plane's UVs
    (and a curve's Generated coordinates) span the same box with y up, so a
    handle ``(x, y)`` sits at UV ``(x, 1 - y)``.  A Texture mapping applies the
    inverse transform, so with location = handle 0, rotation = the angle of
    handle 0 -> handle 1 and scale = the handle distances, the output X runs
    0..1 from handle 0 to handle 1 (the gradient axis) and Y across it.
    """
    handles = gradient.get("handles") or []
    uv = [(float(h[0]), 1.0 - float(h[1])) for h in handles]
    while len(uv) < 2:
        uv.append((0.5, 0.0) if len(uv) == 1 else (0.5, 1.0))
    (x0, y0), (x1, y1) = uv[0], uv[1]
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    rot = math.atan2(dy, dx) if length > 1e-9 else 0.0
    length = length if length > 1e-9 else 1.0
    across = length
    if len(uv) > 2 and gradient.get("type") in ("GRADIENT_RADIAL", "GRADIENT_DIAMOND"):
        across = math.hypot(uv[2][0] - x0, uv[2][1] - y0) or length
    return (x0, y0, 0.0), rot, (length, across, 1.0)


def gradient_average(gradient: dict) -> List[float]:
    stops = gradient.get("stops") or []
    if not stops:
        return [0.5, 0.5, 0.5, 1.0]
    n = float(len(stops))
    return [sum(float(st["color"][i]) for st in stops) / n for i in range(4)]


def make_gradient_material(name: str, gradient: dict, alpha: float = 1.0, coords: str = "UV") -> "bpy.types.Material":
    """Unlit material reproducing a Figma gradient with shader nodes.

    Texture Coordinate (UV, or Generated for curve objects) -> Mapping (from the
    handle positions, see :func:`gradient_mapping`) -> Gradient Texture
    (LINEAR / SPHERICAL / RADIAL; a diamond is |x| + |y| from math nodes) ->
    Color Ramp with the stops (colour + alpha) -> Emission.  Everything stays
    editable: move the handles in the Mapping node, recolour stops in the ramp.
    """
    mat = _new_managed_material(name)
    nodes, links, em, out = _emission_output(mat)
    gtype = gradient.get("type", "GRADIENT_LINEAR")

    tc = nodes.new("ShaderNodeTexCoord")
    tc.location = (-1100, 0)
    mp = nodes.new("ShaderNodeMapping")
    mp.vector_type = "TEXTURE"
    mp.location = (-900, 0)
    loc, rot, scl = gradient_mapping(gradient)
    mp.inputs["Location"].default_value = loc
    mp.inputs["Rotation"].default_value = (0.0, 0.0, rot)
    mp.inputs["Scale"].default_value = scl
    links.new(tc.outputs["Generated" if coords == "Generated" else "UV"], mp.inputs["Vector"])

    if gtype == "GRADIENT_DIAMOND":
        # |x| + |y| == 1 on the diamond through the handles (no Gradient Texture type for it)
        absn = nodes.new("ShaderNodeVectorMath")
        absn.operation = "ABSOLUTE"
        absn.location = (-700, 0)
        links.new(mp.outputs[0], absn.inputs[0])
        sep = nodes.new("ShaderNodeSeparateXYZ")
        sep.location = (-550, 0)
        links.new(absn.outputs[0], sep.inputs[0])
        add = nodes.new("ShaderNodeMath")
        add.operation = "ADD"
        add.location = (-400, 0)
        links.new(sep.outputs["X"], add.inputs[0])
        links.new(sep.outputs["Y"], add.inputs[1])
        fac = add.outputs[0]
    else:
        gt = nodes.new("ShaderNodeTexGradient")
        gt.location = (-700, 0)
        gt.gradient_type = {"GRADIENT_RADIAL": "SPHERICAL", "GRADIENT_ANGULAR": "RADIAL"}.get(gtype, "LINEAR")
        links.new(mp.outputs[0], gt.inputs["Vector"])
        fac = gt.outputs["Fac"]
        if gtype == "GRADIENT_RADIAL":
            inv = nodes.new("ShaderNodeMath")  # SPHERICAL is 1 at the centre; Figma's position 0 is the centre
            inv.operation = "SUBTRACT"
            inv.location = (-450, 0)
            inv.inputs[0].default_value = 1.0
            links.new(fac, inv.inputs[1])
            fac = inv.outputs[0]
        elif gtype == "GRADIENT_ANGULAR":
            # RADIAL is atan2 / 2pi + 0.5, counter-clockwise from -X; Figma sweeps clockwise from handle 1
            sub = nodes.new("ShaderNodeMath")
            sub.operation = "SUBTRACT"
            sub.location = (-550, 0)
            sub.inputs[0].default_value = 0.5
            links.new(fac, sub.inputs[1])
            frac = nodes.new("ShaderNodeMath")
            frac.operation = "FRACT"
            frac.location = (-400, 0)
            links.new(sub.outputs[0], frac.inputs[0])
            fac = frac.outputs[0]

    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.location = (-250, 0)
    stops = list(gradient.get("stops") or [])[:GRADIENT_MAX_STOPS]
    if len(gradient.get("stops") or []) > GRADIENT_MAX_STOPS:
        log.warning("Gradient %s has %d stops; Blender ramps hold %d", name, len(gradient["stops"]), GRADIENT_MAX_STOPS)
    elements = ramp.color_ramp.elements
    while len(elements) > 1:
        elements.remove(elements[-1])
    for i, st in enumerate(stops or [{"color": [0.5, 0.5, 0.5, 1.0], "position": 0.0}]):
        el = elements[0] if i == 0 else elements.new(float(st["position"]))
        el.position = float(st["position"])
        el.color = tuple(float(c) for c in st["color"][:4])
    links.new(fac, ramp.inputs["Fac"])
    links.new(ramp.outputs["Color"], em.inputs["Color"])

    min_alpha = min([float(st["color"][3]) for st in stops] or [1.0])
    translucent = alpha < 0.999 or min_alpha < 0.999
    _link_with_alpha(nodes, links, em, out, alpha_socket=ramp.outputs["Alpha"] if translucent else None, alpha_value=alpha)
    avg = gradient_average(gradient)
    mat.diffuse_color = (avg[0], avg[1], avg[2], avg[3] * alpha)
    set_material_blend(mat, min(alpha, min_alpha))
    return mat


# ---------------------------------------------------------------------------
# Strokes: a Geometry Nodes outline (non-destructive, follows the Bevel)
# ---------------------------------------------------------------------------


def stroke_node_group() -> "bpy.types.NodeTree":
    """The shared "Figma Stroke" node group, created on first use.

    Inputs: Geometry, Width (m), Align (INSIDE / CENTER / OUTSIDE), Material,
    Lift (m).  The tree takes the evaluated geometry (a plane after its Corner
    Radius bevel, or an ellipse curve), turns the mesh boundary into a curve
    (Edge Neighbors == 1 -> Mesh to Curve; curve components pass through),
    resamples it to its evaluated points, gives it Z-up normals so the profile
    lies in the shape's plane, shifts it inward / outward by half the width
    for the alignment, sweeps a straight Width-long profile along it with
    Curve to Mesh (mitre-scaled at corners so sharp rectangles get square
    outer corners), assigns the stroke material and joins the ribbon with the
    original geometry so the fill stays.
    """
    ng = bpy.data.node_groups.get(STROKE_NODE_GROUP)
    if ng is not None and ng.bl_idname == "GeometryNodeTree":
        names = {s.name for s in ng.interface.items_tree if s.in_out == "INPUT"}
        if {"Width", "Align", "Material", "Lift"} <= names:
            return ng
        ng.name += ".old"
    ng = bpy.data.node_groups.new(STROKE_NODE_GROUP, "GeometryNodeTree")
    ng.is_modifier = True
    ng[MANAGED_PROP] = True
    iface = ng.interface
    iface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    width = iface.new_socket("Width", in_out="INPUT", socket_type="NodeSocketFloat")
    width.subtype = "DISTANCE"
    width.min_value = 0.0
    width.default_value = 0.001
    width.description = "Stroke width (Figma stroke weight x import scale)"
    align = iface.new_socket("Align", in_out="INPUT", socket_type="NodeSocketMenu")
    align.description = "Where the stroke sits relative to the shape edge (Figma stroke align)"
    material = iface.new_socket("Material", in_out="INPUT", socket_type="NodeSocketMaterial")
    lift = iface.new_socket("Lift", in_out="INPUT", socket_type="NodeSocketFloat")
    lift.subtype = "DISTANCE"
    lift.default_value = STROKE_LIFT
    lift.description = "Offset of the stroke above the fill so the two never z-fight"
    iface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")

    n, L = ng.nodes, ng.links

    def node(idname, x, y, **props):
        nd = n.new(idname)
        nd.location = (x, y)
        for k, v in props.items():
            setattr(nd, k, v)
        return nd

    inp = node("NodeGroupInput", -1500, 0)
    out = node("NodeGroupOutput", 1300, 0)

    # boundary edges of the mesh -> curve; any curve component passes straight through
    neighbors = node("GeometryNodeInputMeshEdgeNeighbors", -1300, -200)
    is_boundary = node("FunctionNodeCompare", -1150, -200, data_type="INT", operation="EQUAL")
    is_boundary.inputs[3].default_value = 1
    L.new(neighbors.outputs["Face Count"], is_boundary.inputs[2])
    to_curve = node("GeometryNodeMeshToCurve", -1000, 0)
    L.new(inp.outputs["Geometry"], to_curve.inputs["Mesh"])
    L.new(is_boundary.outputs["Result"], to_curve.inputs["Selection"])
    parts = node("GeometryNodeSeparateComponents", -1000, -250)
    L.new(inp.outputs["Geometry"], parts.inputs["Geometry"])
    outline = node("GeometryNodeJoinGeometry", -820, 0)
    L.new(to_curve.outputs["Curve"], outline.inputs["Geometry"])
    L.new(parts.outputs["Curve"], outline.inputs["Geometry"])
    poly = node("GeometryNodeResampleCurve", -660, 0)
    poly.inputs["Mode"].default_value = "Evaluated"
    L.new(outline.outputs["Geometry"], poly.inputs["Curve"])
    z_up = node("GeometryNodeSetCurveNormal", -500, 0)
    z_up.inputs["Mode"].default_value = "Z Up"
    L.new(poly.outputs["Curve"], z_up.inputs["Curve"])

    # mitre factor at each point: 1 / dot(tangent, direction of the incoming segment)
    index = node("GeometryNodeInputIndex", -660, -400)
    prev_index = node("GeometryNodeOffsetPointInCurve", -500, -400)
    prev_index.inputs["Offset"].default_value = -1
    L.new(index.outputs["Index"], prev_index.inputs["Point Index"])
    position = node("GeometryNodeInputPosition", -500, -550)
    prev_pos = node("GeometryNodeSampleIndex", -330, -450, data_type="FLOAT_VECTOR", domain="POINT")
    L.new(z_up.outputs["Curve"], prev_pos.inputs["Geometry"])
    L.new(position.outputs["Position"], prev_pos.inputs["Value"])
    L.new(prev_index.outputs["Point Index"], prev_pos.inputs["Index"])
    incoming = node("ShaderNodeVectorMath", -160, -450, operation="SUBTRACT")
    L.new(position.outputs["Position"], incoming.inputs[0])
    L.new(prev_pos.outputs["Value"], incoming.inputs[1])
    incoming_dir = node("ShaderNodeVectorMath", 0, -450, operation="NORMALIZE")
    L.new(incoming.outputs["Vector"], incoming_dir.inputs[0])
    tangent = node("GeometryNodeInputTangent", 0, -600)
    cos_half = node("ShaderNodeVectorMath", 160, -450, operation="DOT_PRODUCT")
    L.new(tangent.outputs["Tangent"], cos_half.inputs[0])
    L.new(incoming_dir.outputs["Vector"], cos_half.inputs[1])
    cos_clamped = node("ShaderNodeMath", 320, -450, operation="MAXIMUM")  # cap the mitre at ~5x for spikes
    cos_clamped.inputs[1].default_value = 0.2
    L.new(cos_half.outputs["Value"], cos_clamped.inputs[0])
    mitre = node("ShaderNodeMath", 480, -450, operation="DIVIDE")
    mitre.inputs[0].default_value = 1.0
    L.new(cos_clamped.outputs["Value"], mitre.inputs[1])

    # alignment: INSIDE -1 / CENTER 0 / OUTSIDE +1 times half the width, along the outward normal
    align_switch = node("GeometryNodeMenuSwitch", -1300, 300, data_type="FLOAT")
    items = align_switch.enum_definition.enum_items
    while len(items):  # by index: removing reallocates, so earlier item references would dangle
        items.remove(items[0])
    for label in ("INSIDE", "CENTER", "OUTSIDE"):
        items.new(label)
    L.new(inp.outputs["Align"], align_switch.inputs["Menu"])
    align_switch.inputs["INSIDE"].default_value = -1.0
    align_switch.inputs["CENTER"].default_value = 0.0
    align_switch.inputs["OUTSIDE"].default_value = 1.0
    half = node("ShaderNodeMath", -1300, 150, operation="MULTIPLY")
    half.inputs[1].default_value = 0.5
    L.new(inp.outputs["Width"], half.inputs[0])
    shift = node("ShaderNodeMath", -1100, 300, operation="MULTIPLY")
    L.new(align_switch.outputs[0], shift.inputs[0])
    L.new(half.outputs["Value"], shift.inputs[1])
    shift_mitred = node("ShaderNodeMath", 640, 300, operation="MULTIPLY")
    L.new(shift.outputs["Value"], shift_mitred.inputs[0])
    L.new(mitre.outputs["Value"], shift_mitred.inputs[1])
    normal = node("GeometryNodeInputNormal", 480, 150)
    outwardness = node("ShaderNodeVectorMath", 640, 150, operation="DOT_PRODUCT")  # shapes are centred on the origin
    L.new(normal.outputs["Normal"], outwardness.inputs[0])
    L.new(position.outputs["Position"], outwardness.inputs[1])
    outward_sign = node("ShaderNodeMath", 800, 150, operation="SIGN")
    L.new(outwardness.outputs["Value"], outward_sign.inputs[0])
    amount = node("ShaderNodeMath", 800, 300, operation="MULTIPLY")
    L.new(shift_mitred.outputs["Value"], amount.inputs[0])
    L.new(outward_sign.outputs["Value"], amount.inputs[1])
    offset = node("ShaderNodeVectorMath", 960, 300, operation="SCALE")
    L.new(normal.outputs["Normal"], offset.inputs[0])
    L.new(amount.outputs["Value"], offset.inputs["Scale"])
    lift_vec = node("ShaderNodeCombineXYZ", 960, 450)
    L.new(inp.outputs["Lift"], lift_vec.inputs["Z"])
    offset_lifted = node("ShaderNodeVectorMath", 1120, 300, operation="ADD")
    L.new(offset.outputs["Vector"], offset_lifted.inputs[0])
    L.new(lift_vec.outputs["Vector"], offset_lifted.inputs[1])

    capture = node("GeometryNodeCaptureAttribute", 660, 0, domain="POINT")
    capture.capture_items.new("FLOAT", "Mitre")
    L.new(z_up.outputs["Curve"], capture.inputs["Geometry"])
    L.new(mitre.outputs["Value"], capture.inputs["Mitre"])
    moved = node("GeometryNodeSetPosition", 860, 0)
    L.new(capture.outputs["Geometry"], moved.inputs["Geometry"])
    L.new(offset_lifted.outputs["Vector"], moved.inputs["Offset"])

    # ribbon: a straight profile of length Width centred on the curve, mitre-scaled at corners
    neg_half = node("ShaderNodeMath", -1100, 150, operation="MULTIPLY")
    neg_half.inputs[1].default_value = -1.0
    L.new(half.outputs["Value"], neg_half.inputs[0])
    start = node("ShaderNodeCombineXYZ", -900, 200)
    end = node("ShaderNodeCombineXYZ", -900, 100)
    L.new(neg_half.outputs["Value"], start.inputs["X"])
    L.new(half.outputs["Value"], end.inputs["X"])
    profile = node("GeometryNodeCurvePrimitiveLine", -700, 150, mode="POINTS")
    L.new(start.outputs["Vector"], profile.inputs["Start"])
    L.new(end.outputs["Vector"], profile.inputs["End"])
    ribbon = node("GeometryNodeCurveToMesh", 1000, 0)
    L.new(moved.outputs["Geometry"], ribbon.inputs["Curve"])
    L.new(profile.outputs["Curve"], ribbon.inputs["Profile Curve"])
    L.new(capture.outputs["Mitre"], ribbon.inputs["Scale"])
    coloured = node("GeometryNodeSetMaterial", 1140, 0)
    L.new(ribbon.outputs["Mesh"], coloured.inputs["Geometry"])
    L.new(inp.outputs["Material"], coloured.inputs["Material"])
    result = node("GeometryNodeJoinGeometry", 1240, 100)
    L.new(inp.outputs["Geometry"], result.inputs["Geometry"])
    L.new(coloured.outputs["Geometry"], result.inputs["Geometry"])
    L.new(result.outputs["Geometry"], out.inputs["Geometry"])
    return ng


def stroke_socket_ids(ng: "bpy.types.NodeTree") -> Dict[str, str]:
    return {s.name: s.identifier for s in ng.interface.items_tree if s.in_out == "INPUT"}


def stroke_modifier(ob: "bpy.types.Object") -> Optional["bpy.types.Modifier"]:
    mod = ob.modifiers.get(STROKE_MODIFIER_NAME)
    if mod is not None and mod.type == "NODES" and mod.node_group is not None and mod.node_group.name.startswith(STROKE_NODE_GROUP):
        return mod
    return None


def stroke_settings(mod: "bpy.types.Modifier") -> Dict[str, object]:
    """``{"width", "align", "material", "lift"}`` as set on a Stroke modifier."""
    ids = stroke_socket_ids(mod.node_group)
    items = {value: label for label, _, _, _, value in mod.id_properties_ui(ids["Align"]).as_dict()["items"]}
    return {
        "width": float(mod[ids["Width"]]),
        "align": items.get(mod[ids["Align"]], "INSIDE"),
        "material": mod[ids["Material"]],
        "lift": float(mod[ids["Lift"]]),
    }


def apply_stroke_modifier(
    ob: "bpy.types.Object", width: float, align: str, material: "bpy.types.Material", lift: float = STROKE_LIFT
) -> "bpy.types.Modifier":
    """Add (right after Corner Radius) or update the object's Stroke modifier."""
    mod = stroke_modifier(ob)
    ng = stroke_node_group()
    if mod is None:
        mod = ob.modifiers.new(STROKE_MODIFIER_NAME, "NODES")
        mod.node_group = ng
        mod.show_expanded = False
        names = [m.name for m in ob.modifiers]
        if CORNER_MODIFIER_NAME in names:
            ob.modifiers.move(len(names) - 1, names.index(CORNER_MODIFIER_NAME) + 1)
    elif mod.node_group is not ng:
        mod.node_group = ng
    ids = stroke_socket_ids(ng)
    mod[ids["Width"]] = float(width)
    mod[ids["Material"]] = material
    mod[ids["Lift"]] = float(lift)
    items = {label: value for label, _, _, _, value in mod.id_properties_ui(ids["Align"]).as_dict()["items"]}
    mod[ids["Align"]] = items.get(align, items["INSIDE"])
    ob.update_tag()
    return mod


def remove_stroke_modifier(ob: "bpy.types.Object") -> None:
    mod = stroke_modifier(ob)
    if mod is not None:
        ob.modifiers.remove(mod)


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
        # component instances: (component_id, path) -> (signature, datablock) shared by matching elements
        self._shared: Dict[Tuple[str, str], Tuple[tuple, "bpy.types.ID"]] = {}
        self._data_done: set = set()  # datablock pointers already written this build (shared data is updated once)
        self._components: Dict[str, Tuple[Element, int]] = {}  # component id -> (root group element, depth index)
        self._comp_colls: Dict[str, "bpy.types.Collection"] = {}  # COLLECTION_INSTANCE mode: component id -> collection
        self._collapsed: set = set()  # ids of instance descendants replaced by a collection instance
        self._target: Optional["bpy.types.Collection"] = None  # collection receiving the element being built
        # 3D presets / curved screen
        self.depths = resolve_depths(self.opt.depth_preset, self.opt.depths)
        self._by_id: Dict[str, Element] = {e.id: e for e in scene.elements}
        self._text_parents = {e.parent for e in scene.elements if e.kind == "text" and e.parent}
        self._first_child: Dict[str, str] = {}
        for e in scene.elements:
            if e.parent and e.kind != "group" and not e.id.endswith(":bg"):
                self._first_child.setdefault(e.parent, e.id)
        self._curve_origin: Optional["bpy.types.Object"] = None
        self._stale_origin: Optional["bpy.types.Object"] = None
        self._svg_px_to_m: Optional[float] = None
        self._text_baseline_ratio: Optional[float] = None
        self._font_cache: Dict[Tuple, Optional["bpy.types.VectorFont"]] = {}
        self._warned: set = set()

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
        self._link_to_target(ob)
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
        self._link_to_target(ob)
        return ob

    def _own_collections(self) -> List["bpy.types.Collection"]:
        """The import collection and its component sub-collections (not the removed one)."""
        return [self.report.collection] + [c for c in self.report.collection.children if c.get(COMPONENT_PROP)]

    def _link_to_target(self, ob: "bpy.types.Object") -> None:
        """Put ``ob`` in the collection of the element being built (and only there among ours)."""
        target = self._target or self.report.collection
        for c in self._own_collections():
            if c is not target and ob.name in c.objects:
                c.objects.unlink(ob)
        if ob.name not in target.objects:
            target.objects.link(ob)

    # -- component instances ----------------------------------------------------

    def _share_key(self, el: Element) -> Optional[Tuple[str, str]]:
        """Slot of ``el`` inside its component when its datablock may be shared."""
        if not self.opt.link_instances or not el.component_id or el.component_path is None or el.override:
            return None
        if el.kind not in ("rect", "ellipse", "text", "image"):
            return None
        return (el.component_id, el.component_path)

    def _signature(self, el: Element, asset_path: Optional[str] = None) -> tuple:
        sig = shared_signature(el)
        return sig + (file_hash(asset_path),) if asset_path else sig

    def _link_data(self, el: Element, ob: Optional["bpy.types.Object"], sig: tuple):
        """Shared datablock for ``el`` (``None`` = make a fresh one).

        For a new object the registered datablock of the element's component slot
        is returned when its signature matches.  An existing object is switched to
        the shared datablock (its own one is freed), or, when it is an overridden /
        no longer matching instance element that still shares data, given its own
        copy so the override does not leak into the other instances.
        """
        key = self._share_key(el)
        entry = self._shared.get(key) if key else None
        if ob is None:
            if entry is not None and entry[0] == sig:
                self.report.linked += 1
                return entry[1]
            return None
        if entry is not None and entry[0] == sig:
            if ob.data.as_pointer() != entry[1].as_pointer():
                old = ob.data
                ob.data = entry[1]
                self._free_data(old)
            self.report.linked += 1
        elif (key is None or entry is not None) and el.component_id and ob.data is not None and ob.data.users > 1:
            ob.data = ob.data.copy()  # overridden / no longer matching: stop sharing (the first element of a slot keeps its data)
        return None

    def _register_shared(self, el: Element, ob: "bpy.types.Object", sig: tuple) -> None:
        key = self._share_key(el)
        if key is not None and key not in self._shared:
            self._shared[key] = (sig, ob.data)
        if el.override and el.name not in self.report.overrides:
            self.report.overrides.append(el.name)
        self._data_done.add(ob.data.as_pointer())

    def _written(self, data) -> bool:
        """True when ``data`` was already written by an earlier element of this build (shared datablock)."""
        return data is not None and data.as_pointer() in self._data_done

    @staticmethod
    def _free_data(data) -> None:
        if data is None or data.users > 0:
            return
        for store in (bpy.data.meshes, bpy.data.curves):
            if data.name in store and store[data.name] == data:
                store.remove(data)
                return

    def _component_collection(self, cid: str, el: Element) -> "bpy.types.Collection":
        coll = self._comp_colls.get(cid)
        if coll is None:
            coll = next((c for c in self.report.collection.children if c.get(COMPONENT_PROP) == cid), None)
            if coll is None:
                coll = bpy.data.collections.new(el.name + COMPONENT_SUFFIX)
                self.report.collection.children.link(coll)
            coll[COMPONENT_PROP] = cid
            coll["figma_page_id"] = self.scene.page_id
            self._comp_colls[cid] = coll
        return coll

    def _target_collection(self, el: Element) -> "bpy.types.Collection":
        if self.opt.instance_mode == "COLLECTION_INSTANCE" and el.is_component and el.component_id in self._comp_colls:
            return self._comp_colls[el.component_id]
        return self.report.collection

    def build_collection_instance(self, el: Element, depth: int, ob: Optional["bpy.types.Object"] = None) -> "bpy.types.Object":
        """An INSTANCE root as an Empty instancing its component's collection.

        The component's objects sit where the component is on the page, so the
        collection's ``instance_offset`` is the component root's position and the
        Empty's matrix is ``instance @ component⁻¹`` (times that offset), which
        puts the component's top-left corner at the instance's.
        """
        comp_el, comp_depth = self._components[el.component_id]
        coll = self._component_collection(el.component_id, comp_el)
        if ob is None:
            ob = self._new_object(el, el.name, None)
        ob.empty_display_type = "PLAIN_AXES"
        ob.empty_display_size = max(0.01, min(el.w, el.h) * self.opt.scale * 0.25)
        ob.instance_type = "COLLECTION"
        ob.instance_collection = coll
        comp_matrix = self.matrix_for(comp_el, comp_depth, (0.0, 0.0))
        offset = comp_matrix.to_translation()
        coll.instance_offset = offset
        self._place(ob, self.matrix_for(el, depth, (0.0, 0.0)) @ comp_matrix.inverted() @ Matrix.Translation(offset))
        self.report.linked += 1
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

    def _warn_once(self, msg: str) -> None:
        if msg not in self._warned:
            self._warned.add(msg)
            self.report.warnings.append(msg)

    def _fill_material(self, el: Element, coords: str) -> Tuple["bpy.types.Material", bool]:
        """``(material, is_gradient)`` for an element's first visible fill."""
        if el.fill_gradient and el.fill_gradient.get("stops"):
            return self.materials.gradient(el.fill_gradient, el.opacity, coords), True
        color = el.fill or [0.5, 0.5, 0.5, 1.0]
        alpha = color[3] * el.opacity if len(color) > 3 else el.opacity
        return self.materials.flat(color, alpha), False

    def _has_stroke(self, el: Element) -> bool:
        return bool(el.stroke_rgba) and bool(el.stroke_weight) and float(el.stroke_weight) > 0.0 and el.stroke_rgba[3] * el.opacity > 0.0

    def _apply_stroke(self, ob: "bpy.types.Object", el: Element) -> None:
        """Add / update / drop the Stroke Geometry Nodes modifier to match the element's stroke."""
        if not self._has_stroke(el):
            remove_stroke_modifier(ob)
            return
        mat = self.materials.flat(el.stroke_rgba, el.stroke_rgba[3] * el.opacity)
        lift = min(STROKE_LIFT, self.opt.depth_step / 2.0) if self.opt.depth_step > 0 else STROKE_LIFT
        apply_stroke_modifier(ob, float(el.stroke_weight) * self.opt.scale, el.stroke_align or "INSIDE", mat, lift)

    def _asset_path(self, el: Element) -> Optional[str]:
        if not el.asset:
            return None
        p = os.path.join(self.bundle_dir, el.asset)
        if not os.path.exists(p):
            self.report.warnings.append("Asset missing for %r: %s" % (el.name, el.asset))
            return None
        return p

    # -- 3D presets: depth via modifiers / curve properties, curved screen ------------

    def depth_kind(self, el: Element) -> str:
        """Which ``DEPTH_KINDS`` entry applies to an element.

        *button*: the background plane of a container that has a direct TEXT
        child and is smaller than ``BUTTON_MAX_SIZE`` px on its longest side, or
        a plain rectangle that is the first drawn child of such a container
        and fills it.  Other container backgrounds are *frame*; other rects and
        ellipses are *shape*.
        """
        if el.kind in ("text", "icon", "image"):
            return el.kind
        if el.kind == "ellipse":
            return "shape"
        if el.id.endswith(":bg"):
            if el.parent in self._text_parents and max(el.w, el.h) < BUTTON_MAX_SIZE:
                return "button"
            return "frame"
        parent = self._by_id.get(el.parent) if el.parent else None
        if (
            parent is not None
            and el.parent in self._text_parents
            and self._first_child.get(el.parent) == el.id
            and abs(parent.w - el.w) <= 1.0
            and abs(parent.h - el.h) <= 1.0
            and max(el.w, el.h) < BUTTON_MAX_SIZE
        ):
            return "button"
        return "shape"

    def depth_for(self, el: Element) -> float:
        """Total thickness in metres for ``el`` under the current preset."""
        return self.depths.get(self.depth_kind(el), 0.0) * self.opt.scale

    def _apply_solidify(self, ob: "bpy.types.Object", thickness: float) -> None:
        """Add / update / remove the *Depth* Solidify modifier unless the user changed its thickness.

        ``offset = -1`` grows the shell toward the back so the front face stays
        where Figma put it; even thickness keeps the rim uniform around the
        bevelled corners.  The modifier sits after *Stroke* (the outline needs
        the flat boundary) and before *Screen Curve*.
        """
        mod = ob.modifiers.get(DEPTH_MODIFIER_NAME)
        if mod is not None and mod.type != "SOLIDIFY":
            mod = None
        applied = ob.get(DEPTH_PROP)
        if mod is not None and (applied is None or not math.isclose(mod.thickness, float(applied), abs_tol=1e-9)):
            return  # the user's own modifier, or a thickness they changed: leave it alone
        if thickness <= 0.0:
            if mod is not None:
                ob.modifiers.remove(mod)
            if DEPTH_PROP in ob:
                del ob[DEPTH_PROP]
            return
        if mod is None:
            mod = ob.modifiers.new(DEPTH_MODIFIER_NAME, "SOLIDIFY")
            mod.offset = -1.0
            mod.use_even_offset = True
            mod.use_rim = True
            mod.show_expanded = False
            names = [m.name for m in ob.modifiers]
            if SCREEN_MODIFIER_NAME in names:
                ob.modifiers.move(len(names) - 1, names.index(SCREEN_MODIFIER_NAME))
        mod.thickness = thickness
        ob[DEPTH_PROP] = thickness

    def _apply_extrude(self, cu, thickness: float, bevel: Optional[float] = None) -> None:
        """Set ``curve.extrude`` (half the thickness per side) and, for text, ``bevel_depth`` on a curve datablock.

        Values live on the data (shared between linked instances) and are only
        overwritten while they still equal what the importer applied last time.
        """
        if self._written(cu):
            return
        applied = float(cu.get(DEPTH_PROP, 0.0))
        if math.isclose(cu.extrude * 2.0, applied, abs_tol=1e-9):
            cu.extrude = thickness / 2.0
            if thickness > 0.0:
                cu[DEPTH_PROP] = thickness
            elif DEPTH_PROP in cu:
                del cu[DEPTH_PROP]
        if bevel is not None:
            applied_b = float(cu.get(TEXT_BEVEL_PROP, 0.0))
            if math.isclose(cu.bevel_depth, applied_b, abs_tol=1e-9):
                cu.bevel_depth = bevel
                if bevel > 0.0:
                    cu.bevel_resolution = max(cu.bevel_resolution, 2)
                    cu[TEXT_BEVEL_PROP] = bevel
                elif TEXT_BEVEL_PROP in cu:
                    del cu[TEXT_BEVEL_PROP]

    def _apply_depth(self, ob: "bpy.types.Object", el: Element) -> None:
        thickness = self.depth_for(el)
        if ob.type == "MESH":
            self._apply_solidify(ob, thickness)
        elif ob.type == "FONT":
            self._apply_extrude(ob.data, thickness, self.depths.get("text_bevel", 0.0) * self.opt.scale)
        elif ob.type == "CURVE":
            self._apply_extrude(ob.data, thickness)

    def _back_shift(self, ob: "bpy.types.Object") -> Matrix:
        """Local translation moving an extruded curve / text back so its front face stays where Figma put it."""
        if ob.type in ("CURVE", "FONT") and ob.data.extrude > 0.0:
            return Matrix.Translation((0.0, 0.0, -ob.data.extrude))
        return Matrix.Identity(4)

    def _screen_origin(self) -> "bpy.types.Object":
        """The shared bend origin Empty at the frame centre, axes: X along the UI, Y into the screen, Z up."""
        if self._curve_origin is None:
            ob = self._stale_origin
            self._stale_origin = None
            if ob is None or ob.type != "EMPTY":
                ob = bpy.data.objects.new(self.report.collection.name + " Curve Origin", None)
                ob.empty_display_type = "SINGLE_ARROW"
                ob.empty_display_size = 0.1
                self.report.objects.append(ob)
            ob[ELEM_ID_PROP] = CURVE_ORIGIN_ID
            ob["figma_kind"] = "curve_origin"
            if ob.name not in self.report.collection.objects:
                self.report.collection.objects.link(ob)
            b = self.scene.bounds or {"x": 0, "y": 0, "w": 0, "h": 0}
            fx = (b["x"] + b["w"] / 2.0 - self.offset[0]) * self.opt.scale
            fy = -(b["y"] + b["h"] / 2.0 - self.offset[1]) * self.opt.scale
            # frame axes (x right, y up, z toward the viewer) -> origin axes (X right, Y into the screen, Z up)
            frame_to_origin = Matrix(((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, -1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)))
            self._place(ob, self.base @ Matrix.Translation((fx, fy, 0.0)) @ frame_to_origin)
            self._curve_origin = ob
        return self._curve_origin

    def _x_extent(self, el: Optional[Element], ob: "bpy.types.Object") -> float:
        """Width of the object along the screen's x axis (metres), analytic from the element's rotated box."""
        if el is not None:
            a, b, _, _, _, _ = el.matrix
            return (abs(a) * el.w + abs(b) * el.h) * self.opt.scale
        xs = [(ob.matrix_world @ p.co).x for sp in ob.data.splines for p in sp.bezier_points]
        xs += [(ob.matrix_world @ Vector(p.co[:3])).x for sp in ob.data.splines for p in sp.points]
        return (max(xs) - min(xs)) if xs else 0.0

    def _apply_screen(self, ob: "bpy.types.Object", el: Optional[Element]) -> None:
        """Add / update / remove the *Screen Curve* Simple Deform (BEND) unless the user changed its angle.

        Every object is bent by ``extent / radius`` over its own width around the
        shared origin, so all of them lie on the same cylinder (negative angle:
        edges come toward the viewer).  Deforming per object is an
        approximation: an object is bent about its own vertices only, so very
        wide objects with few vertices (a plain plane) stay faceted unless you
        subdivide them.
        """
        mod = ob.modifiers.get(SCREEN_MODIFIER_NAME)
        if mod is not None and mod.type != "SIMPLE_DEFORM":
            mod = None
        applied = ob.get(SCREEN_ANGLE_PROP)
        if mod is not None and (applied is None or not math.isclose(mod.angle, float(applied), abs_tol=1e-9)):
            return
        extent = self._x_extent(el, ob)
        if not self.opt.curve_screen or self.opt.curve_radius <= 0.0 or extent <= 0.0:
            if mod is not None:
                ob.modifiers.remove(mod)
            if SCREEN_ANGLE_PROP in ob:
                del ob[SCREEN_ANGLE_PROP]
            return
        if mod is None:
            mod = ob.modifiers.new(SCREEN_MODIFIER_NAME, "SIMPLE_DEFORM")
            mod.deform_method = "BEND"
            mod.deform_axis = "Z"
            mod.show_expanded = False
        mod.origin = self._screen_origin()
        mod.angle = -extent / self.opt.curve_radius
        ob[SCREEN_ANGLE_PROP] = mod.angle

    def _apply_3d(self, ob: "bpy.types.Object", el: Element) -> None:
        self._apply_depth(ob, el)
        self._apply_screen(ob, el)

    # -- element builders ---------------------------------------------------

    def build_group(self, el: Element, depth: int, ob: Optional["bpy.types.Object"] = None) -> "bpy.types.Object":
        if ob is None:
            ob = self._new_object(el, el.name, None)
            ob.empty_display_type = "PLAIN_AXES"
        elif ob.instance_type == "COLLECTION" and ob.instance_collection is not None and ob.instance_collection.get(COMPONENT_PROP):
            ob.instance_type = "NONE"  # was a collection instance in an earlier import
            ob.instance_collection = None
        ob.empty_display_size = max(0.01, min(el.w, el.h) * self.opt.scale * 0.25)
        self._place(ob, self.matrix_for(el, depth, (0.0, 0.0)))
        return ob

    def _rect_object(
        self,
        el: Element,
        radii: Optional[List[float]],
        always_modifier: bool,
        ob: Optional["bpy.types.Object"] = None,
        shared=None,
    ) -> "bpy.types.Object":
        """Plane object at the element's size (object scale 1) plus a Corner Radius Bevel modifier.

        On update the four vertices are moved; the mesh datablock, its material
        slots and every modifier stay.  A mesh the user edited into something
        else is replaced by a fresh plane (materials carried over).  ``shared``
        is a component's mesh to use for a new object instead of a fresh plane.
        """
        s = self.opt.scale
        w, h = max(el.w, 1e-6) * s, max(el.h, 1e-6) * s
        if ob is None:
            ob = self._new_object(el, el.name, shared if shared is not None else plane_mesh(el.name, w, h))
        elif is_plane_mesh(ob.data):
            if not self._written(ob.data):
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
        sig = self._signature(el)
        shared = self._link_data(el, ob, sig)
        if el.kind == "ellipse":
            if ob is None:
                ob = self._new_object(el, el.name, shared if shared is not None else ellipse_curve(el.name, w, h))
            elif is_ellipse_curve(ob.data):
                if not self._written(ob.data):
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
            ob = self._rect_object(el, el.corner_radii, True, ob, shared)
        # planes carry 0..1 UVs; a curve has none, its Generated coordinates span its bound box instead
        mat, is_gradient = self._fill_material(el, "Generated" if ob.type == "CURVE" else "UV")
        self._apply_material(ob.data, mat)
        if el.fill_approx and not is_gradient:
            ob["figma_fill_approx"] = True
        elif "figma_fill_approx" in ob:
            del ob["figma_fill_approx"]
        self._apply_stroke(ob, el)
        self._apply_3d(ob, el)
        self._place(ob, self.matrix_for(el, depth, (el.w / 2.0, el.h / 2.0)) @ self._back_shift(ob))
        self._register_shared(el, ob, sig)
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
        digest = file_hash(path)
        sig = self._signature(el) + (digest,)  # instances share the plane only for the same picture
        shared = self._link_data(el, ob, sig)
        ob = self._rect_object(el, radii, always_modifier=False, ob=ob, shared=shared)
        mesh = ob.data
        image = self._current_image(mesh) if ob.get("figma_asset_hash") == digest else None
        try:
            if image is None:
                image = bpy.data.images.load(path, check_existing=True)
            self._apply_material(mesh, self.materials.image(image, el.opacity))
            ob["figma_asset_hash"] = digest
        except RuntimeError as e:
            self.report.warnings.append("Could not load image %s for %r: %s" % (path, el.name, e))
            self._apply_material(mesh, self.materials.flat(el.fill or [0.5, 0.5, 0.5, 1.0], el.opacity))
        self._apply_stroke(ob, el)
        self._apply_3d(ob, el)
        self._place(ob, self.matrix_for(el, depth, (el.w / 2.0, el.h / 2.0)))
        self._register_shared(el, ob, sig)
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
        if self._has_stroke(el):
            self._warn_once("Strokes on icons are not imported (they survive inside the SVG's own paths only)")
        if empty is None:
            empty = self._new_object(el, el.name, None)
            empty.empty_display_type = "PLAIN_AXES"
        empty.empty_display_size = max(0.005, min(el.w, el.h) * self.opt.scale * 0.5)

        old_curves = self._icon_curves(empty)
        fit_prop = empty.get("figma_icon_fit")
        if old_curves and empty.get("figma_asset_hash") == digest and fit_prop is not None and len(fit_prop) == 16:
            fit = Matrix([tuple(fit_prop[i * 4 : i * 4 + 4]) for i in range(4)])
            self.report.objects.extend(old_curves)
            curves = old_curves
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
                self._link_to_target(ob)
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
            curves = objs

        # 3D: extrude the icon's curves (the SVG importer's paths are 2D curves) and bend them with the screen
        thickness = self.depth_for(el)
        extrude = 0.0
        for ob in curves:
            if ob.type == "CURVE":
                self._apply_extrude(ob.data, thickness)
                extrude = max(extrude, ob.data.extrude)
        # the curves' own matrices come from the importer; the Empty carries the fit, so shift it back before the fit
        self._place(empty, self.matrix_for(el, depth, (0.0, 0.0)) @ Matrix.Translation((0.0, 0.0, -extrude)) @ fit)
        for ob in curves:
            if ob.type == "CURVE":
                self._apply_screen(ob, None)
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
        sig = self._signature(el)
        shared = self._link_data(el, ob, sig)
        if ob is None:
            ob = self._new_object(el, el.name, shared if shared is not None else bpy.data.curves.new(el.name, "FONT"))
        cu = ob.data
        write = not self._written(cu)  # a shared text curve is written once per build
        body = info.get("characters", "")
        case = info.get("textCase")
        if case == "UPPER":
            body = body.upper()
        elif case == "LOWER":
            body = body.lower()
        elif case == "TITLE":
            body = body.title()
        lh = info.get("lineHeightPx")
        if not (lh and fs > 0):
            lh = fs * 1.2
        auto = info.get("textAutoResize", "NONE")
        anchor_x = 0.0
        if auto == "WIDTH_AND_HEIGHT":
            anchor_x = {"CENTER": el.w / 2.0, "RIGHT": el.w}.get(info.get("textAlignHorizontal", "LEFT"), 0.0)
        if write:
            cu.body = body
            cu.size = fs * s
            cu.align_x = {"LEFT": "LEFT", "CENTER": "CENTER", "RIGHT": "RIGHT", "JUSTIFIED": "JUSTIFY"}.get(
                info.get("textAlignHorizontal", "LEFT"), "LEFT"
            )
            cu.align_y = {"TOP": "TOP", "CENTER": "CENTER", "BOTTOM": "BOTTOM"}.get(info.get("textAlignVertical", "TOP"), "TOP")
            cu.space_line = float(lh) / fs if info.get("lineHeightPx") and fs > 0 else 1.0
            ls = info.get("letterSpacing")
            cu.space_character = max(0.1, 1.0 + float(ls) / (fs * 0.5)) if ls else 1.0
            tb = cu.text_boxes[0]
            if auto == "WIDTH_AND_HEIGHT":
                tb.width = 0.0  # never wrap auto-width text; align about the anchor instead
            else:
                tb.width = el.w * s
            tb.height = max(el.h * s, 1e-6)

        # Font: assign the auto-matched font unless the user picked another one since the last import
        # (``figma_font_file`` remembers what the importer assigned; "" is Blender's built-in font).
        font = self._load_font(info)
        previous = ob.get("figma_font_file")
        if previous is None or previous == font_key(cu.font):
            if font is not None and write:
                cu.font = font
            ob["figma_font_file"] = font_key(cu.font)
        if font is None:
            fam = info.get("fontFamily") or "?"
            if fam not in self.report.missing_fonts:
                self.report.missing_fonts.append(fam)
        ob["figma_font"] = info.get("fontFamily") or ""
        ob["figma_font_postscript"] = info.get("fontPostScriptName") or ""
        # text keeps a solid colour: for a gradient fill that is the stops' average
        color = el.fill or [0.0, 0.0, 0.0, 1.0]
        alpha = color[3] * el.opacity if len(color) > 3 else el.opacity
        self._apply_material(cu, self.materials.flat(color, alpha))
        if el.fill_approx:
            ob["figma_fill_approx"] = True
        elif "figma_fill_approx" in ob:
            del ob["figma_fill_approx"]
        if self._has_stroke(el):
            self._warn_once("Strokes on text are not imported")

        # Vertical placement: put Blender's first baseline where Figma's is.
        blender_baseline = self._text_probe() * cu.size  # relative to object origin, y up
        figma_baseline = -((float(lh) - fs) / 2.0 + TEXT_ASCENT_RATIO * fs) * s  # from box top, y up
        dy = figma_baseline - blender_baseline
        self._apply_3d(ob, el)
        self._place(ob, self.matrix_for(el, depth, (anchor_x, 0.0)) @ Matrix.Translation((0.0, dy, 0.0)) @ self._back_shift(ob))
        self._register_shared(el, ob, sig)
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
        for c in [coll] + [c for c in coll.children if c.get(COMPONENT_PROP)]:
            for ob in c.objects:
                eid = ob.get(ELEM_ID_PROP) or legacy_elem_id(ob)
                if eid and eid not in self.existing:
                    self.existing[eid] = ob

    def _prepare_components(self) -> None:
        """Index the components in the export and, in COLLECTION_INSTANCE mode, give each its collection."""
        for depth, el in enumerate(self.scene.elements):
            if el.is_component and el.kind == "group" and el.component_id and el.component_path == "":
                self._components.setdefault(el.component_id, (el, depth))
        if self.opt.instance_mode == "COLLECTION_INSTANCE":
            for cid, (el, _depth) in self._components.items():
                self._component_collection(cid, el)

    def _is_collection_instance(self, el: Element) -> bool:
        return (
            self.opt.instance_mode == "COLLECTION_INSTANCE"
            and el.kind == "group"
            and not el.is_component
            and el.component_path == ""
            and el.component_id in self._components
        )

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
        if self.opt.instance_mode not in INSTANCE_MODES:
            self.report.warnings.append("Unknown instance mode %r; using LINKED_DATA" % self.opt.instance_mode)
            self.opt.instance_mode = "LINKED_DATA"
        self._prepare_components()
        self._stale_origin = self.existing.pop(CURVE_ORIGIN_ID, None)
        if self.opt.curve_screen and self.opt.curve_radius > 0.0:
            self._screen_origin()

        for depth, el in enumerate(self.scene.elements):
            if el.parent in self._collapsed:  # inside a collection instance: drawn by the instanced collection
                self._collapsed.add(el.id)
                continue
            existing = self.existing.pop(el.id, None)
            self._target = self._target_collection(el)
            try:
                if self._is_collection_instance(el):
                    ob = self.build_collection_instance(el, depth, self._reuse(existing, el, "EMPTY"))
                    self._collapsed.add(el.id)
                else:
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
        self._target = None

        for ob in list(self.existing.values()):  # elements that vanished from Figma (or collapsed into an instance)
            self._retire(ob)
        self.existing.clear()
        if self._stale_origin is not None:  # curved screen turned off: drop the origin unless a modifier still uses it
            used = any(
                m.type == "SIMPLE_DEFORM" and m.origin == self._stale_origin for c in self._own_collections() for o in c.objects for m in o.modifiers
            )
            if not used:
                bpy.data.objects.remove(self._stale_origin)
            self._stale_origin = None
        for c in list(coll.children):  # component collections left empty after a mode change
            if c.get(COMPONENT_PROP) and not c.objects and not c.children and c not in self._comp_colls.values():
                bpy.data.collections.remove(c)
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
