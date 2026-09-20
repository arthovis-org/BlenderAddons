"""Convert a Figma node tree (a page or a single frame) into a *scene bundle*.

The bundle is a folder containing ``scene.json`` (a flat list of elements in
draw order with world transforms) and an ``assets/`` directory with the SVG /
PNG files Figma rendered for icons and image fills.

This module is pure Python (no ``bpy``) so that the same code runs inside
Blender and from the standalone CLI, and so the conversion can be unit tested
with plain CPython.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from .figma_api import FigmaClient, sanitize_id

log = logging.getLogger(__name__)

SCENE_VERSION = 1

CONTAINER_TYPES = {"FRAME", "GROUP", "INSTANCE", "COMPONENT", "COMPONENT_SET", "SECTION"}
FILLABLE_CONTAINER_TYPES = {"FRAME", "INSTANCE", "COMPONENT", "COMPONENT_SET"}
TRUE_VECTOR_TYPES = {"VECTOR", "BOOLEAN_OPERATION", "STAR", "LINE", "REGULAR_POLYGON"}
SHAPE_TYPES = {"RECTANGLE", "ELLIPSE"}
VECTOR_LIKE_TYPES = TRUE_VECTOR_TYPES | SHAPE_TYPES
GRADIENT_TYPES = {"GRADIENT_LINEAR", "GRADIENT_RADIAL", "GRADIENT_ANGULAR", "GRADIENT_DIAMOND"}
STROKE_ALIGNS = {"INSIDE", "CENTER", "OUTSIDE"}
# Figma's default handles for a gradient without gradientHandlePositions: top -> bottom
DEFAULT_GRADIENT_HANDLES = [[0.5, 0.0], [0.5, 1.0], [0.0, 0.0]]

Matrix = Tuple[float, float, float, float, float, float]  # a, b, tx, c, d, ty
IDENTITY: Matrix = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


# ---------------------------------------------------------------------------
# Options / data classes
# ---------------------------------------------------------------------------


@dataclass
class ExportOptions:
    icon_max_size: float = 128.0
    icon_format: str = "svg"  # "svg" | "png"
    raster_scale: float = 2.0
    image_format: str = "png"


@dataclass
class Element:
    id: str
    name: str
    kind: str  # text | rect | ellipse | icon | image | group
    figma_type: str
    parent: Optional[str]
    x: float
    y: float
    w: float
    h: float
    rotation: float  # degrees, counter-clockwise (Figma convention)
    matrix: List[float]  # world 2x3 matrix [a, b, tx, c, d, ty]
    opacity: float = 1.0
    fill: Optional[List[float]] = None  # rgba 0..1 (for a gradient: the stops' average, used as fallback / preview)
    fill_approx: bool = False  # ``fill`` only approximates the paint (gradient averaged to one colour)
    fill_gradient: Optional[Dict[str, Any]] = None  # {"type", "stops": [{"color": rgba, "position"}], "handles": [[x, y], ...]}
    stroke_rgba: Optional[List[float]] = None  # first visible stroke, rgba 0..1
    stroke_weight: Optional[float] = None  # px
    stroke_align: Optional[str] = None  # INSIDE | CENTER | OUTSIDE
    corner_radii: Optional[List[float]] = None  # tl, tr, br, bl
    asset: Optional[str] = None  # relative path inside the bundle
    asset_format: Optional[str] = None
    text: Optional[Dict[str, Any]] = None
    flipped: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None and v is not False}


@dataclass
class Scene:
    """A flat, draw-ordered list of elements plus metadata about their root.

    ``page_id`` / ``page_name`` hold the *root node* the scene was built from:
    a CANVAS when a whole page was imported, or the frame / group / node that
    was imported on its own (``root_type`` tells which).  The field names are
    kept for ``scene.json`` compatibility with bundles written by v0.1.
    """

    page_id: str
    page_name: str
    file_key: str = ""
    root_type: str = "CANVAS"
    elements: List[Element] = field(default_factory=list)
    bounds: Optional[Dict[str, float]] = None
    options: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": SCENE_VERSION,
            "file_key": self.file_key,
            "page_id": self.page_id,
            "page_name": self.page_name,
            "root_type": self.root_type,
            "bounds": self.bounds,
            "options": self.options,
            "warnings": self.warnings,
            "elements": [e.to_dict() for e in self.elements],
        }

    def by_kind(self, kind: str) -> List[Element]:
        return [e for e in self.elements if e.kind == kind]


# ---------------------------------------------------------------------------
# Matrix helpers
# ---------------------------------------------------------------------------


def mat_from_figma(rt: Any) -> Matrix:
    """``[[a, b, tx], [c, d, ty]]`` -> flat tuple."""
    (a, b, tx), (c, d, ty) = rt
    return (float(a), float(b), float(tx), float(c), float(d), float(ty))


def mat_mul(p: Matrix, q: Matrix) -> Matrix:
    """Return ``p @ q`` (apply q first, then p)."""
    pa, pb, ptx, pc, pd, pty = p
    qa, qb, qtx, qc, qd, qty = q
    return (
        pa * qa + pb * qc,
        pa * qb + pb * qd,
        pa * qtx + pb * qty + ptx,
        pc * qa + pd * qc,
        pc * qb + pd * qd,
        pc * qtx + pd * qty + pty,
    )


def mat_apply(m: Matrix, x: float, y: float) -> Tuple[float, float]:
    a, b, tx, c, d, ty = m
    return (a * x + b * y + tx, c * x + d * y + ty)


def mat_rotation_deg(m: Matrix) -> float:
    """Counter-clockwise rotation in degrees using Figma's convention.

    Figma builds ``[[cos, sin, x], [-sin, cos, y]]`` for a CCW rotation in its
    y-down coordinate system, so the angle is ``atan2(b, a)``.
    """
    a, b, _, _, _, _ = m
    return math.degrees(math.atan2(b, a))


def mat_determinant(m: Matrix) -> float:
    a, b, _, c, d, _ = m
    return a * d - b * c


# ---------------------------------------------------------------------------
# Node inspection helpers
# ---------------------------------------------------------------------------


def is_visible(node: dict) -> bool:
    return node.get("visible", True) is not False


def visible_fills(node: dict) -> List[dict]:
    return [f for f in node.get("fills") or [] if f.get("visible", True) is not False]


def first_visible_fill(node: dict) -> Optional[dict]:
    fills = visible_fills(node)
    return fills[0] if fills else None


def has_image_fill(node: dict) -> bool:
    f = first_visible_fill(node)
    return bool(f and f.get("type") == "IMAGE")


def paint_to_rgba(paint: dict) -> Tuple[Optional[List[float]], bool]:
    """Return ``(rgba, approximated)`` for a paint, or ``(None, False)``."""
    ptype = paint.get("type", "SOLID")
    popacity = float(paint.get("opacity", 1.0))
    if ptype == "SOLID":
        c = paint.get("color") or {}
        return (
            [
                float(c.get("r", 0.0)),
                float(c.get("g", 0.0)),
                float(c.get("b", 0.0)),
                float(c.get("a", 1.0)) * popacity,
            ],
            False,
        )
    if ptype.startswith("GRADIENT"):
        stops = paint.get("gradientStops") or []
        if not stops:
            return None, False
        n = float(len(stops))
        r = sum(float(s["color"].get("r", 0)) for s in stops) / n
        g = sum(float(s["color"].get("g", 0)) for s in stops) / n
        b = sum(float(s["color"].get("b", 0)) for s in stops) / n
        a = sum(float(s["color"].get("a", 1)) for s in stops) / n
        return [r, g, b, a * popacity], True
    return None, False


def paint_to_gradient(paint: dict) -> Optional[Dict[str, Any]]:
    """Full description of a gradient paint, or ``None`` for any other paint.

    Stops are sorted by position with the paint's ``opacity`` folded into their
    alpha; handle positions are Figma's, normalised to the node box (y down).
    """
    if paint.get("type") not in GRADIENT_TYPES:
        return None
    stops = paint.get("gradientStops") or []
    if not stops:
        return None
    popacity = float(paint.get("opacity", 1.0))
    out_stops = []
    for st in stops:
        c = st.get("color") or {}
        out_stops.append(
            {
                "color": [float(c.get("r", 0.0)), float(c.get("g", 0.0)), float(c.get("b", 0.0)), float(c.get("a", 1.0)) * popacity],
                "position": max(0.0, min(1.0, float(st.get("position", 0.0)))),
            }
        )
    out_stops.sort(key=lambda st: st["position"])
    handles = [[float(h.get("x", 0.0)), float(h.get("y", 0.0))] for h in paint.get("gradientHandlePositions") or []]
    if len(handles) < 2:
        handles = [list(h) for h in DEFAULT_GRADIENT_HANDLES]
    return {"type": paint["type"], "stops": out_stops, "handles": handles}


def _first_colour_paint(node: dict) -> Optional[dict]:
    for paint in visible_fills(node):
        if paint.get("type") == "IMAGE":
            continue
        if paint_to_rgba(paint)[0] is not None:
            return paint
    return None


def node_fill(node: dict) -> Tuple[Optional[List[float]], bool]:
    """First visible non-image fill as rgba plus an ``approx`` flag."""
    paint = _first_colour_paint(node)
    if paint is None:
        return None, False
    return paint_to_rgba(paint)


def node_gradient(node: dict) -> Optional[Dict[str, Any]]:
    """The gradient behind :func:`node_fill`'s colour, when that fill is a gradient."""
    paint = _first_colour_paint(node)
    return paint_to_gradient(paint) if paint is not None else None


def node_stroke(node: dict) -> Optional[Tuple[List[float], float, str]]:
    """``(rgba, weight_px, align)`` of the first visible stroke with a weight > 0, else ``None``.

    Gradient strokes are averaged to one colour; image strokes are ignored.
    ``individualStrokeWeights`` (per side) is reduced to its largest side.
    """
    strokes = [st for st in node.get("strokes") or [] if st.get("visible", True) is not False]
    if not strokes:
        return None
    rgba = None
    for paint in strokes:
        rgba, _ = paint_to_rgba(paint)
        if rgba is not None:
            break
    if rgba is None or rgba[3] <= 0.0:
        return None
    weight = node.get("strokeWeight")
    if weight is None and node.get("individualStrokeWeights"):
        weight = max(float(v) for v in node["individualStrokeWeights"].values())
    weight = float(weight or 0.0)
    if weight <= 0.0:
        return None
    align = node.get("strokeAlign") or "INSIDE"
    return rgba, weight, align if align in STROKE_ALIGNS else "INSIDE"


def corner_radii(node: dict) -> Optional[List[float]]:
    radii = node.get("rectangleCornerRadii")
    if radii and len(radii) == 4:
        vals = [float(r) for r in radii]
    else:
        r = node.get("cornerRadius")
        if not r:
            return None
        vals = [float(r)] * 4
    if all(v <= 0 for v in vals):
        return None
    return vals


def node_size(node: dict) -> Tuple[float, float]:
    size = node.get("size")
    if size and "x" in size and "y" in size:
        return float(size["x"]), float(size["y"])
    bb = node.get("absoluteBoundingBox") or {}
    return float(bb.get("width", 0.0)), float(bb.get("height", 0.0))


def node_local_matrix(node: dict, parent_world: Matrix, page_to_scene: Matrix = IDENTITY) -> Matrix:
    """World (scene) matrix for ``node``.

    Uses ``relativeTransform`` (composed with the parent's world matrix) when
    available, otherwise falls back to ``absoluteBoundingBox`` which is already
    in *page* coordinates and carries no rotation; ``page_to_scene`` maps page
    coordinates into the scene (identity for a whole-page import, a translation
    when a single frame is imported with its corner at the origin).
    """
    rt = node.get("relativeTransform")
    if rt and node.get("size"):
        try:
            return mat_mul(parent_world, mat_from_figma(rt))
        except (TypeError, ValueError):
            pass
    bb = node.get("absoluteBoundingBox") or {}
    return mat_mul(page_to_scene, (1.0, 0.0, float(bb.get("x", 0.0)), 0.0, 1.0, float(bb.get("y", 0.0))))


def _subtree_is_vector_like(node: dict) -> bool:
    """True when every descendant is vector-like (no TEXT, no IMAGE fills)."""
    t = node.get("type")
    if t == "TEXT":
        return False
    if has_image_fill(node):
        return False
    if t in VECTOR_LIKE_TYPES:
        return True
    if t in CONTAINER_TYPES:
        children = [c for c in node.get("children") or [] if is_visible(c)]
        if not children:
            return False
        return all(_subtree_is_vector_like(c) for c in children)
    return False


def is_icon(node: dict, icon_max_size: float) -> bool:
    t = node.get("type")
    if t in TRUE_VECTOR_TYPES:
        return True
    if t in {"GROUP", "FRAME", "INSTANCE", "COMPONENT"}:
        w, h = node_size(node)
        if max(w, h) <= icon_max_size and _subtree_is_vector_like(node):
            return True
    return False


def text_info(node: dict) -> Dict[str, Any]:
    st = node.get("style") or {}
    info: Dict[str, Any] = {
        "characters": node.get("characters", ""),
        "fontSize": float(st.get("fontSize", 12.0)),
        "fontFamily": st.get("fontFamily"),
        "fontPostScriptName": st.get("fontPostScriptName"),
        "fontWeight": st.get("fontWeight"),
        "italic": bool(st.get("italic", False)),
        "textAlignHorizontal": st.get("textAlignHorizontal", "LEFT"),
        "textAlignVertical": st.get("textAlignVertical", "TOP"),
        "textAutoResize": st.get("textAutoResize", "NONE"),
        "textCase": st.get("textCase"),
    }
    if st.get("lineHeightPx") is not None:
        info["lineHeightPx"] = float(st["lineHeightPx"])
    if st.get("letterSpacing") is not None:
        info["letterSpacing"] = float(st["letterSpacing"])
    if node.get("characterStyleOverrides"):
        info["hasStyleOverrides"] = True
    return info


# ---------------------------------------------------------------------------
# Tree walk
# ---------------------------------------------------------------------------


class SceneBuilder:
    def __init__(self, options: Optional[ExportOptions] = None):
        self.options = options or ExportOptions()
        self.elements: List[Element] = []
        self.warnings: List[str] = []
        self._ids_seen: Dict[str, int] = {}
        # Maps Figma page coordinates to scene coordinates (see build_scene / root_origin_matrix).
        self.page_to_scene: Matrix = IDENTITY

    def _unique_id(self, nid: str) -> str:
        n = self._ids_seen.get(nid, 0)
        self._ids_seen[nid] = n + 1
        return nid if n == 0 else "%s#%d" % (nid, n)

    def _make(self, node: dict, kind: str, parent: Optional[str], world: Matrix) -> Element:
        w, h = node_size(node)
        a, b, tx, c, d, ty = world
        det = mat_determinant(world)
        rot = mat_rotation_deg(world)
        el = Element(
            id=self._unique_id(node["id"]),
            name=node.get("name", node["id"]),
            kind=kind,
            figma_type=node.get("type", ""),
            parent=parent,
            x=tx,
            y=ty,
            w=w,
            h=h,
            rotation=rot,
            matrix=[a, b, tx, c, d, ty],
            opacity=float(node.get("opacity", 1.0)),
            flipped=det < 0,
        )
        return el

    @staticmethod
    def _paint(el: Element, node: dict, shape: bool) -> None:
        """Fill colour, gradient and stroke of ``node`` onto ``el``.

        ``shape`` elements (rectangles, ellipses, frame backgrounds) that have a
        stroke but no fill get a fully transparent fill so the outline alone is
        imported.
        """
        el.fill, el.fill_approx = node_fill(node)
        el.fill_gradient = node_gradient(node)
        stroke = node_stroke(node)
        if stroke is not None:
            el.stroke_rgba, el.stroke_weight, el.stroke_align = list(stroke[0]), stroke[1], stroke[2]
            if shape and el.fill is None:
                el.fill = [0.0, 0.0, 0.0, 0.0]

    def walk(self, node: dict, parent_id: Optional[str], parent_world: Matrix) -> None:
        if not is_visible(node):
            return
        t = node.get("type")
        world = node_local_matrix(node, parent_world, self.page_to_scene)
        opts = self.options

        if t == "TEXT":
            el = self._make(node, "text", parent_id, world)
            self._paint(el, node, shape=False)
            el.text = text_info(node)
            if el.text.get("hasStyleOverrides"):
                self.warnings.append("Text %r has per-character style overrides (not supported)" % el.name)
            self.elements.append(el)
            return

        if has_image_fill(node):
            el = self._make(node, "image", parent_id, world)
            stroke = node_stroke(node)
            if stroke is not None:
                el.stroke_rgba, el.stroke_weight, el.stroke_align = list(stroke[0]), stroke[1], stroke[2]
            el.corner_radii = corner_radii(node)
            el.asset_format = opts.image_format
            el.asset = "assets/image_%s.%s" % (sanitize_id(node["id"]), opts.image_format)
            self.elements.append(el)
            return

        if is_icon(node, opts.icon_max_size):
            el = self._make(node, "icon", parent_id, world)
            self._paint(el, node, shape=False)
            el.asset_format = opts.icon_format
            el.asset = "assets/icon_%s.%s" % (sanitize_id(node["id"]), opts.icon_format)
            self.elements.append(el)
            return

        if t == "RECTANGLE":
            el = self._make(node, "rect", parent_id, world)
            self._paint(el, node, shape=True)
            el.corner_radii = corner_radii(node)
            if el.fill is not None:
                self.elements.append(el)
            return

        if t == "ELLIPSE":
            el = self._make(node, "ellipse", parent_id, world)
            self._paint(el, node, shape=True)
            if el.fill is not None:
                self.elements.append(el)
            return

        if t in CONTAINER_TYPES:
            group = self._make(node, "group", parent_id, world)
            self.elements.append(group)
            if t in FILLABLE_CONTAINER_TYPES:
                bg = self._make(node, "rect", group.id, world)
                self._paint(bg, node, shape=True)
                if bg.fill is not None:  # a fill and/or a stroke -> background plane
                    bg.id = group.id + ":bg"
                    bg.name = group.name + " (background)"
                    bg.corner_radii = corner_radii(node)
                    self.elements.append(bg)
                else:
                    self._ids_seen[node["id"]] -= 1  # no background element: give its id back
            for child in node.get("children") or []:
                self.walk(child, group.id, world)
            return

        # SLICE, STICKY, CONNECTOR, unknown...
        self.warnings.append("Skipped unsupported node type %s (%r)" % (t, node.get("name")))


def root_origin_matrix(root: dict) -> Matrix:
    """Parent matrix that moves ``root``'s top-left corner to ``(0, 0)``.

    A node fetched on its own from ``/files/{key}/nodes`` still carries the
    ``relativeTransform`` it has inside its parent (or, without one, its
    ``absoluteBoundingBox``), so a lone frame would otherwise be imported at
    its page coordinates.  Only the translation is cancelled: the root keeps
    its own Figma rotation / flip so the import looks like the Figma canvas.
    The same matrix is used as ``page_to_scene`` for descendants that only
    have an ``absoluteBoundingBox``.
    """
    _, _, tx, _, _, ty = node_local_matrix(root, IDENTITY)
    return (1.0, 0.0, -tx, 0.0, 1.0, -ty)


def build_scene(root_node: dict, options: Optional[ExportOptions] = None, file_key: str = "") -> Scene:
    """Convert a node tree (from ``/files/{key}/nodes``) into a :class:`Scene`.

    ``root_node`` may be a whole page (CANVAS): its children become the
    top-level elements in page coordinates, exactly as before.  Any other node
    (a frame, section, group, component...) is imported on its own: the node
    itself becomes the first element, so a frame's fill and corner radius turn
    into its background plane and its children are parented under it, and the
    tree is translated so the root's top-left corner sits at ``(0, 0)`` (see
    :func:`root_origin_matrix`).  The root's own rotation is kept.
    """
    options = options or ExportOptions()
    sb = SceneBuilder(options)
    root_type = root_node.get("type", "") or "CANVAS"
    if root_type == "CANVAS":
        for child in root_node.get("children") or []:
            sb.walk(child, None, IDENTITY)
    else:
        if not is_visible(root_node):
            sb.warnings.append("Root node %r is hidden in Figma; importing it anyway" % root_node.get("name"))
            root_node = dict(root_node, visible=True)
        origin = root_origin_matrix(root_node)
        sb.page_to_scene = origin
        sb.walk(root_node, None, origin)

    scene = Scene(
        page_id=root_node.get("id", ""),
        page_name=root_node.get("name", "Figma Page"),
        file_key=file_key,
        root_type=root_type,
        elements=sb.elements,
        options=asdict(options),
        warnings=sb.warnings,
    )
    scene.bounds = compute_bounds(scene.elements)
    return scene


def compute_bounds(elements: List[Element]) -> Optional[Dict[str, float]]:
    """Axis-aligned union of every element's rotated box, in Figma px."""
    xs: List[float] = []
    ys: List[float] = []
    for el in elements:
        m = tuple(el.matrix)  # type: ignore[assignment]
        for px, py in ((0, 0), (el.w, 0), (0, el.h), (el.w, el.h)):
            x, y = mat_apply(m, px, py)  # type: ignore[arg-type]
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return {"x": min(xs), "y": min(ys), "w": max(xs) - min(xs), "h": max(ys) - min(ys)}


# ---------------------------------------------------------------------------
# Asset export + bundle IO
# ---------------------------------------------------------------------------


def _original_id(element_id: str) -> str:
    return element_id.split("#", 1)[0].split(":bg", 1)[0]


def export_assets(client: FigmaClient, file_key: str, scene: Scene, out_dir: str) -> List[str]:
    """Render icons / images through the Figma images API into ``out_dir/assets``.

    Returns a list of warnings.  Missing renders are logged and the element's
    ``asset`` is set to ``None`` so the builder can skip it.
    """
    warnings: List[str] = []
    assets_dir = os.path.join(out_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    # group by (format, scale) so each batch is one request shape
    jobs: Dict[Tuple[str, float], List[Element]] = {}
    for el in scene.elements:
        if not el.asset:
            continue
        fmt = el.asset_format or "png"
        scale = 1.0 if fmt == "svg" else float(scene.options.get("raster_scale", 2.0))
        jobs.setdefault((fmt, scale), []).append(el)

    for (fmt, scale), els in jobs.items():
        ids = sorted({_original_id(e.id) for e in els})
        urls = client.export_images(file_key, ids, fmt=fmt, scale=scale)
        for el in els:
            nid = _original_id(el.id)
            url = urls.get(nid)
            if not url:
                msg = "Figma returned no %s render for %r (%s)" % (fmt, el.name, nid)
                log.warning(msg)
                warnings.append(msg)
                el.asset = None
                continue
            dest = os.path.join(out_dir, el.asset)
            try:
                data = client.download(url)
                with open(dest, "wb") as fh:
                    fh.write(data)
            except Exception as e:  # noqa: BLE001 - never crash the whole import
                msg = "Failed to download %s for %r: %s" % (fmt, el.name, e)
                log.warning(msg)
                warnings.append(msg)
                el.asset = None
    scene.warnings.extend(warnings)
    return warnings


def write_scene(scene: Scene, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "scene.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(scene.to_dict(), fh, indent=1, ensure_ascii=False)
    return path


def load_scene(bundle_dir: str) -> Scene:
    """Read ``scene.json`` from a bundle folder back into a :class:`Scene`."""
    path = bundle_dir if bundle_dir.endswith(".json") else os.path.join(bundle_dir, "scene.json")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return scene_from_dict(data)


def scene_from_dict(data: Dict[str, Any]) -> Scene:
    elements = []
    for d in data.get("elements", []):
        d = dict(d)
        d.setdefault("parent", None)
        d.setdefault("figma_type", "")
        d.setdefault("rotation", 0.0)
        d.setdefault("matrix", [1, 0, d.get("x", 0), 0, 1, d.get("y", 0)])
        allowed = {f for f in Element.__dataclass_fields__}  # type: ignore[attr-defined]
        elements.append(Element(**{k: v for k, v in d.items() if k in allowed}))
    return Scene(
        page_id=data.get("page_id", ""),
        page_name=data.get("page_name", "Figma Page"),
        file_key=data.get("file_key", ""),
        root_type=data.get("root_type", "CANVAS"),
        elements=elements,
        bounds=data.get("bounds"),
        options=data.get("options") or {},
        warnings=list(data.get("warnings") or []),
    )


def export_bundle(
    client: FigmaClient,
    file_key: str,
    node_id: str,
    out_dir: str,
    options: Optional[ExportOptions] = None,
) -> Scene:
    """Fetch a page or any single node, convert it and download its assets into ``out_dir``.

    ``node_id`` is a CANVAS id for a whole page or the id of a frame / group /
    node (``12:345``, see :func:`figma_api.parse_node_id`) to import just that
    subtree with its top-left corner at the origin.
    """
    root = client.get_node(file_key, node_id)
    scene = build_scene(root, options, file_key=file_key)
    export_assets(client, file_key, scene, out_dir)
    write_scene(scene, out_dir)
    return scene
