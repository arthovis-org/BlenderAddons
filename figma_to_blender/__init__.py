"""Figma to Blender: import a Figma page or a single frame as editable 3D UI.

The package doubles as a plain Python library / CLI (``python -m
figma_to_blender.cli``), so ``bpy`` is imported defensively and the Blender UI
classes are only defined when it is available.
"""

bl_info = {
    "name": "Figma to Blender",
    "author": "arthovis-org",
    "version": (0, 3, 0),
    "blender": (5, 0, 0),
    "location": "3D Viewport > Sidebar (N) > Figma",
    "description": "Import a Figma page or a single frame as editable 3D UI: text, rounded shapes, gradients, strokes, icons (SVG curves or planes) and images; re-import updates existing objects",
    "doc_url": "https://github.com/arthovis-org/BlenderAddons",
    "tracker_url": "https://github.com/arthovis-org/BlenderAddons/issues",
    "category": "Import-Export",
}

try:
    import bpy
except ImportError:  # running outside Blender (CLI / tests)
    bpy = None


if bpy is not None:
    import os
    import tempfile
    import traceback

    from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
    from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup

    from . import builder, scene_model
    from .figma_api import FigmaClient, FigmaError, parse_file_key, parse_node_id

    ADDON_ID = __package__

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    class FIGMA_preferences(AddonPreferences):
        bl_idname = ADDON_ID

        token: StringProperty(
            name="Personal access token",
            description="Figma personal access token (Figma > Settings > Security > Personal access tokens). "
            "Leave empty to use the FIGMA_TOKEN environment variable",
            subtype="PASSWORD",
        )

        fonts_dir: StringProperty(
            name="Fonts folder",
            description="Default folder scanned (recursively) for .ttf / .otf fonts before the system fonts; "
            "the panel's Fonts folder overrides it per scene. Nothing is downloaded",
            subtype="DIR_PATH",
        )

        def draw(self, context):
            layout = self.layout
            layout.prop(self, "token")
            layout.label(text="Needs the 'File content' read scope. The token is stored in your Blender preferences.", icon="INFO")
            layout.prop(self, "fonts_dir")

    def get_pref(context, name: str, default=""):
        addon = context.preferences.addons.get(ADDON_ID)
        prefs = addon.preferences if addon and hasattr(addon, "preferences") else None
        return getattr(prefs, name, default) if prefs is not None else default

    def get_token(context) -> str:
        addon = context.preferences.addons.get(ADDON_ID)
        token = addon.preferences.token if addon and hasattr(addon, "preferences") and addon.preferences else ""
        return (token or os.environ.get("FIGMA_TOKEN", "")).strip()

    # ------------------------------------------------------------------
    # Scene settings
    # ------------------------------------------------------------------

    _page_items = []  # kept alive: EnumProperty item strings must not be garbage collected

    def _page_enum_items(self, context):
        if _page_items:
            return _page_items
        return [("NONE", "(fetch pages first)", "Click 'Fetch pages' to list the file's pages")]

    WHOLE_PAGE = "PAGE"
    # First entry is fixed; 'Fetch frames' appends the selected page's top-level layers after it.
    _frame_items = [(WHOLE_PAGE, "(whole page)", "Import every top-level layer of the selected page")]

    def _frame_enum_items(self, context):
        return _frame_items

    def _on_page_changed(self, context):
        # The frame list belongs to the page it was fetched for.
        del _frame_items[1:]
        if self.frame != WHOLE_PAGE:
            self.frame = WHOLE_PAGE

    def resolve_target(s: "FIGMA_settings"):
        """Return ``(node_id, label)`` for what the panel says to import.

        A non-empty *Node URL / ID* wins over the dropdowns; otherwise the
        selected frame, or the whole page when *(whole page)* is chosen.
        Raises ``ValueError`` with a user-facing message.
        """
        ref = (s.node_ref or "").strip()
        if ref:
            node_id = parse_node_id(ref)
            if node_id is None:
                raise ValueError(
                    "Could not read a node id from %r. Paste a Figma 'Copy link to selection' URL "
                    "(...?node-id=12-345) or an id like 12:345, or clear the field to use the dropdowns" % ref
                )
            return node_id, "node %s" % node_id
        if not _page_items or s.page == "NONE":
            raise ValueError("Fetch pages and pick one first, or paste a node URL / ID")
        if s.frame != WHOLE_PAGE and len(_frame_items) > 1:
            return s.frame, "frame %s" % s.frame
        return s.page, "page %s" % s.page

    class FIGMA_settings(PropertyGroup):
        file_url: StringProperty(name="File URL / key", description="Figma file URL (…/design/<key>/…) or bare file key")
        page: EnumProperty(name="Page", items=_page_enum_items, update=_on_page_changed)
        frame: EnumProperty(
            name="Frame",
            description="Top-level frame of the selected page to import on its own; '(whole page)' imports everything. Click 'Fetch frames' to fill the list",
            items=_frame_enum_items,
        )
        node_ref: StringProperty(
            name="Node URL / ID (optional)",
            description="Import exactly this node instead of the page/frame above: paste Figma's 'Copy link to selection' URL (…?node-id=12-345) or a node id like 12:345",
        )
        icon_mode: EnumProperty(
            name="Icons",
            items=[
                ("SVG", "SVG curves", "Editable, resolution independent curve objects (gradients/effects are lost)"),
                ("PLANE", "Image planes", "Pixel-perfect PNG textured planes (not editable)"),
            ],
            default="SVG",
        )
        scale: FloatProperty(name="Scale", description="Metres per Figma pixel", default=0.001, min=1e-6, precision=4, step=0.01)
        depth_step: FloatProperty(
            name="Depth step", description="Offset between consecutive elements in draw order (avoids z-fighting)", default=0.0005, min=0.0, precision=5, step=0.01
        )
        corner_segments: IntProperty(
            name="Corner segments",
            description="Segments per rounded corner on the 'Corner Radius' Bevel modifier of rectangles and image planes (editable later)",
            default=builder.CORNER_SEGMENTS,
            min=1,
            max=64,
        )
        icon_max_size: FloatProperty(name="Icon max size", description="Vector groups up to this size (px) are imported as one icon", default=128.0, min=1.0)
        raster_scale: FloatProperty(name="Raster scale", description="PNG export scale for image fills and plane icons", default=2.0, min=0.1, max=4.0)
        orientation: EnumProperty(
            name="Orientation",
            items=[
                ("XZ", "Upright (XZ)", "UI stands upright facing -Y (front view)"),
                ("XY", "Flat (XY)", "UI lies flat on the ground"),
            ],
            default="XZ",
        )
        center: BoolProperty(name="Center at origin", description="Move the page's centre to the world origin", default=True)
        update_existing: BoolProperty(
            name="Update existing objects",
            description="Re-import into the collection of an earlier import of the same page/frame: objects are matched by "
            "Figma id and updated in place (transform, size, text, importer materials) while your extra modifiers, "
            "swapped materials and custom properties are kept. Off: always build a fresh collection",
            default=True,
        )
        remove_missing: BoolProperty(
            name="Delete removed elements",
            description="When updating, delete objects whose Figma element no longer exists instead of moving them "
            "into the '<collection> (removed)' sub-collection",
            default=False,
        )
        link_instances: BoolProperty(
            name="Link component instances",
            description="Elements of a component instance share the component's mesh / curve / text data (and material), "
            "so editing one updates every instance. Overridden instance elements (own text, fill, size...) get their own data",
            default=True,
        )
        instance_mode: EnumProperty(
            name="Instance mode",
            items=[
                ("LINKED_DATA", "Linked data", "One object per instance element, sharing the component's datablocks"),
                (
                    "COLLECTION_INSTANCE",
                    "Collection instances",
                    "The component's objects go into their own collection and each instance is one Empty instancing it "
                    "(components that are not part of the import fall back to linked data)",
                ),
            ],
            default="LINKED_DATA",
        )
        depth_preset: EnumProperty(
            name="3D preset",
            description="Give the flat UI some depth, all through modifiers and curve properties (nothing baked): "
            "planes get a 'Depth' Solidify modifier growing toward the back, text / ellipses / icon curves get curve extrude",
            items=[
                ("FLAT", "Flat", "Plain 2D import (no depth)"),
                ("SUBTLE", "Subtle", "Frames 2 px, buttons 3 px, shapes 1 px, text and icons 0.5 px, images 1 px"),
                ("CARD", "Card", "Frames 8 px, buttons 6 px, shapes 3 px, text and icons 1.5 px, images 3 px, text bevel 0.25 px"),
                ("CUSTOM", "Custom", "Use the per-kind depths below (Figma px, converted with the scale)"),
            ],
            default="FLAT",
        )
        depth_frame: FloatProperty(name="Frame / background", description="Depth (px) of frame background planes", default=8.0, min=0.0)
        depth_button: FloatProperty(
            name="Button",
            description="Depth (px) of button-like backgrounds: a container smaller than 400 px with a text child",
            default=6.0,
            min=0.0,
        )
        depth_shape: FloatProperty(name="Rectangle / ellipse", description="Depth (px) of plain rectangles and ellipses", default=3.0, min=0.0)
        depth_text: FloatProperty(name="Text", description="Text extrude (px, total thickness)", default=1.5, min=0.0)
        depth_icon: FloatProperty(name="Icon", description="Extrude (px) of SVG icon curves / depth of icon planes", default=1.5, min=0.0)
        depth_image: FloatProperty(name="Image", description="Depth (px) of image planes", default=3.0, min=0.0)
        text_bevel: FloatProperty(name="Text bevel", description="Bevel depth (px) on text curves", default=0.25, min=0.0)
        curve_screen: BoolProperty(
            name="Curve screen",
            description="Bend every object around a shared '<collection> Curve Origin' Empty at the frame centre "
            "('Screen Curve' Simple Deform modifiers) so the UI wraps onto a cylinder facing the viewer",
            default=False,
        )
        curve_radius: FloatProperty(name="Radius", description="Cylinder radius in metres", default=1.0, min=0.01, unit="LENGTH")
        fonts_dir: StringProperty(
            name="Fonts folder",
            description="Folder scanned (recursively) for .ttf / .otf fonts before the system fonts; empty = the add-on preference",
            subtype="DIR_PATH",
        )
        missing_fonts: StringProperty(name="Missing fonts", description="Fonts the last import could not find (one per line)", options={"HIDDEN"})
        show_missing_fonts: BoolProperty(name="Show missing fonts", default=True)
        bundle_dir: StringProperty(name="Bundle folder", description="Folder containing scene.json and assets/", subtype="DIR_PATH")
        export_dir: StringProperty(name="Export to", description="Folder to write the bundle into", subtype="DIR_PATH")

    def build_options(s: "FIGMA_settings", context=None) -> builder.BuildOptions:
        fonts_dir = s.fonts_dir or (get_pref(context, "fonts_dir") if context is not None else "")
        return builder.BuildOptions(
            scale=s.scale,
            depth_step=s.depth_step,
            icon_mode=s.icon_mode,
            plane_orientation=s.orientation,
            center=s.center,
            corner_segments=s.corner_segments,
            update_existing=s.update_existing,
            remove_missing=s.remove_missing,
            link_instances=s.link_instances,
            instance_mode=s.instance_mode,
            depth_preset=s.depth_preset,
            depths=(
                {
                    "frame": s.depth_frame,
                    "button": s.depth_button,
                    "shape": s.depth_shape,
                    "text": s.depth_text,
                    "icon": s.depth_icon,
                    "image": s.depth_image,
                    "text_bevel": s.text_bevel,
                }
                if s.depth_preset == "CUSTOM"
                else None
            ),
            curve_screen=s.curve_screen,
            curve_radius=s.curve_radius,
            fonts_dir=bpy.path.abspath(fonts_dir) if fonts_dir else None,
        )

    def export_options(s: "FIGMA_settings") -> scene_model.ExportOptions:
        return scene_model.ExportOptions(
            icon_max_size=s.icon_max_size, icon_format="svg" if s.icon_mode == "SVG" else "png", raster_scale=s.raster_scale
        )

    def _client(op: Operator, context):
        token = get_token(context)
        if not token:
            op.report({"ERROR"}, "Set your Figma token in Edit > Preferences > Add-ons > Figma to Blender")
            return None
        return FigmaClient(token)

    def _report_build(op: Operator, report: builder.BuildReport, context=None):
        for w in report.warnings:
            print("[figma_to_blender] warning:", w)
        for label in report.missing_font_labels():
            print("[figma_to_blender] font not found:", label)
        if context is not None:
            context.scene.figma_to_blender.missing_fonts = "\n".join(report.missing_font_labels())
        level = {"WARNING"} if (report.warnings or report.missing_fonts) else {"INFO"}
        op.report(level, report.summary() + (" (see console for details)" if report.warnings else ""))

    # ------------------------------------------------------------------
    # Operators
    # ------------------------------------------------------------------

    class FIGMA_OT_fetch_pages(Operator):
        bl_idname = "figma.fetch_pages"
        bl_label = "Fetch pages"
        bl_description = "List the pages of the Figma file"

        def execute(self, context):
            s = context.scene.figma_to_blender
            client = _client(self, context)
            if client is None:
                return {"CANCELLED"}
            try:
                key = parse_file_key(s.file_url)
                pages = client.list_pages(key)
            except (ValueError, FigmaError) as e:
                self.report({"ERROR"}, str(e))
                return {"CANCELLED"}
            _page_items.clear()
            for pg in pages:
                _page_items.append((pg["id"], pg["name"], "Page %s" % pg["id"]))
            if not pages:
                self.report({"WARNING"}, "File has no pages")
                return {"CANCELLED"}
            s.page = pages[0]["id"]
            self.report({"INFO"}, "Found %d page(s)" % len(pages))
            return {"FINISHED"}

    class FIGMA_OT_fetch_frames(Operator):
        bl_idname = "figma.fetch_frames"
        bl_label = "Fetch frames"
        bl_description = "List the top-level frames of the selected page so one of them can be imported on its own"

        def execute(self, context):
            s = context.scene.figma_to_blender
            if not _page_items or s.page == "NONE":
                self.report({"ERROR"}, "Fetch pages and pick one first")
                return {"CANCELLED"}
            client = _client(self, context)
            if client is None:
                return {"CANCELLED"}
            try:
                key = parse_file_key(s.file_url)
                frames = client.list_top_level_frames(key, s.page)
            except (ValueError, FigmaError) as e:
                self.report({"ERROR"}, str(e))
                return {"CANCELLED"}
            del _frame_items[1:]
            for fr in frames:
                kind = (fr.get("type") or "node").replace("_", " ").title()
                _frame_items.append((fr["id"], fr["name"], "%s %s" % (kind, fr["id"])))
            s.frame = WHOLE_PAGE
            if not frames:
                self.report({"WARNING"}, "Page has no top-level layers")
                return {"CANCELLED"}
            self.report({"INFO"}, "Found %d top-level layer(s)" % len(frames))
            return {"FINISHED"}

    class _ExportMixin:
        def _export(self, context, out_dir):
            s = context.scene.figma_to_blender
            try:
                node_id, label = resolve_target(s)
                key = parse_file_key(s.file_url)
            except ValueError as e:
                self.report({"ERROR"}, str(e))
                return None
            client = _client(self, context)
            if client is None:
                return None
            wm = context.window_manager
            wm.progress_begin(0, 100)
            try:
                wm.progress_update(10)
                print("[figma_to_blender] fetching %s of file %s" % (label, key))
                scene = scene_model.export_bundle(client, key, node_id, out_dir, export_options(s))
                wm.progress_update(90)
            except (ValueError, FigmaError, OSError) as e:
                self.report({"ERROR"}, str(e))
                return None
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self.report({"ERROR"}, "Export failed, see console")
                return None
            finally:
                wm.progress_end()
            return scene

    class FIGMA_OT_import_page(_ExportMixin, Operator):
        bl_idname = "figma.import_page"
        bl_label = "Import"
        bl_description = (
            "Fetch the selected page, frame or node from Figma and build it as 3D objects in a new collection "
            "named after it (a frame is placed with its top-left corner at the origin)"
        )

        def execute(self, context):
            s = context.scene.figma_to_blender
            out_dir = tempfile.mkdtemp(prefix="figma_", dir=bpy.app.tempdir or None)
            scene = self._export(context, out_dir)
            if scene is None:
                return {"CANCELLED"}
            try:
                report = builder.build_scene(scene, out_dir, build_options(s, context))
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self.report({"ERROR"}, "Build failed, see console")
                return {"CANCELLED"}
            _report_build(self, report, context)
            return {"FINISHED"}

    class FIGMA_OT_export_bundle(_ExportMixin, Operator):
        bl_idname = "figma.export_bundle"
        bl_label = "Export bundle to folder"
        bl_description = "Fetch the selected page, frame or node and write scene.json + assets without building objects"

        def execute(self, context):
            s = context.scene.figma_to_blender
            out_dir = bpy.path.abspath(s.export_dir) if s.export_dir else ""
            if not out_dir:
                self.report({"ERROR"}, "Choose an export folder")
                return {"CANCELLED"}
            scene = self._export(context, out_dir)
            if scene is None:
                return {"CANCELLED"}
            self.report({"INFO"}, "Wrote %d element(s) to %s" % (len(scene.elements), out_dir))
            return {"FINISHED"}

    class FIGMA_OT_import_bundle(Operator):
        bl_idname = "figma.import_bundle"
        bl_label = "Import bundle"
        bl_description = "Build objects from a previously exported bundle folder (offline)"

        def execute(self, context):
            s = context.scene.figma_to_blender
            bundle_dir = bpy.path.abspath(s.bundle_dir) if s.bundle_dir else ""
            if not bundle_dir or not os.path.exists(os.path.join(bundle_dir, "scene.json")):
                self.report({"ERROR"}, "Bundle folder must contain scene.json")
                return {"CANCELLED"}
            try:
                report = builder.build_bundle(bundle_dir, build_options(s, context))
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self.report({"ERROR"}, "Build failed, see console")
                return {"CANCELLED"}
            _report_build(self, report, context)
            return {"FINISHED"}

    # ------------------------------------------------------------------
    # Panel
    # ------------------------------------------------------------------

    class FIGMA_PT_panel(Panel):
        bl_label = "Figma to Blender"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "Figma"

        def draw(self, context):
            s = context.scene.figma_to_blender
            layout = self.layout
            if not get_token(context):
                layout.label(text="No token set (see add-on preferences)", icon="ERROR")

            box = layout.box()
            box.label(text="Figma file", icon="URL")
            box.prop(s, "file_url", text="")
            row = box.row(align=True)
            row.operator(FIGMA_OT_fetch_pages.bl_idname, icon="FILE_REFRESH")
            box.prop(s, "page")
            row = box.row(align=True)
            row.operator(FIGMA_OT_fetch_frames.bl_idname, icon="FILE_REFRESH")
            box.prop(s, "frame")
            box.prop(s, "node_ref")

            box = layout.box()
            box.label(text="Import options", icon="PREFERENCES")
            box.prop(s, "icon_mode")
            box.prop(s, "orientation")
            col = box.column(align=True)
            col.prop(s, "scale")
            col.prop(s, "depth_step")
            col.prop(s, "corner_segments")
            col.prop(s, "icon_max_size")
            col.prop(s, "raster_scale")
            box.prop(s, "center")
            box.prop(s, "update_existing")
            sub = box.row()
            sub.active = s.update_existing
            sub.prop(s, "remove_missing")

            box = layout.box()
            box.label(text="Component instances", icon="LINKED")
            box.prop(s, "link_instances")
            box.prop(s, "instance_mode", text="Mode")

            box = layout.box()
            box.label(text="3D", icon="MOD_SOLIDIFY")
            box.prop(s, "depth_preset")
            if s.depth_preset == "CUSTOM":
                col = box.column(align=True)
                for name in ("depth_frame", "depth_button", "depth_shape", "depth_text", "depth_icon", "depth_image", "text_bevel"):
                    col.prop(s, name)
            row = box.row(align=True)
            row.prop(s, "curve_screen")
            sub = row.row()
            sub.active = s.curve_screen
            sub.prop(s, "curve_radius")

            box = layout.box()
            box.label(text="Fonts", icon="FONT_DATA")
            box.prop(s, "fonts_dir", text="")
            if not s.fonts_dir and get_pref(context, "fonts_dir"):
                box.label(text="Using the preference folder", icon="INFO")
            missing = [line for line in s.missing_fonts.split("\n") if line]
            if missing:
                row = box.row()
                row.prop(
                    s,
                    "show_missing_fonts",
                    text="Missing fonts (%d)" % len(missing),
                    icon="TRIA_DOWN" if s.show_missing_fonts else "TRIA_RIGHT",
                    emboss=False,
                )
                if s.show_missing_fonts:
                    col = box.column(align=True)
                    for line in missing:
                        col.label(text=line, icon="ERROR")
                    col.label(text="Put the .ttf / .otf files in the fonts folder and import again", icon="INFO")
            layout.operator(FIGMA_OT_import_page.bl_idname, icon="IMPORT")

            box = layout.box()
            box.label(text="Offline bundle", icon="FILE_FOLDER")
            box.prop(s, "bundle_dir", text="")
            box.operator(FIGMA_OT_import_bundle.bl_idname, icon="IMPORT")
            box.separator()
            box.prop(s, "export_dir", text="")
            box.operator(FIGMA_OT_export_bundle.bl_idname, icon="EXPORT")

    classes = (
        FIGMA_preferences,
        FIGMA_settings,
        FIGMA_OT_fetch_pages,
        FIGMA_OT_fetch_frames,
        FIGMA_OT_import_page,
        FIGMA_OT_export_bundle,
        FIGMA_OT_import_bundle,
        FIGMA_PT_panel,
    )

    def register():
        for cls in classes:
            bpy.utils.register_class(cls)
        bpy.types.Scene.figma_to_blender = PointerProperty(type=FIGMA_settings)

    def unregister():
        if hasattr(bpy.types.Scene, "figma_to_blender"):
            del bpy.types.Scene.figma_to_blender
        for cls in reversed(classes):
            bpy.utils.unregister_class(cls)

else:

    def register():  # pragma: no cover - only meaningful inside Blender
        raise RuntimeError("figma_to_blender.register() requires bpy")

    def unregister():  # pragma: no cover
        pass
