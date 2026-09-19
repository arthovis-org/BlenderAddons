"""Figma to Blender: import a Figma page as editable 3D UI.

The package doubles as a plain Python library / CLI (``python -m
figma_to_blender.cli``), so ``bpy`` is imported defensively and the Blender UI
classes are only defined when it is available.
"""

bl_info = {
    "name": "Figma to Blender",
    "author": "arthovis-org",
    "version": (0, 1, 0),
    "blender": (3, 6, 0),
    "location": "3D Viewport > Sidebar (N) > Figma",
    "description": "Import a Figma page as editable 3D UI: text, rounded shapes, icons (SVG curves or planes) and images",
    "doc_url": "https://github.com/arthovis-org/empty2",
    "tracker_url": "https://github.com/arthovis-org/empty2/issues",
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

    from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty
    from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup

    from . import builder, scene_model
    from .figma_api import FigmaClient, FigmaError, parse_file_key

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

        def draw(self, context):
            layout = self.layout
            layout.prop(self, "token")
            layout.label(text="Needs the 'File content' read scope. The token is stored in your Blender preferences.", icon="INFO")

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

    class FIGMA_settings(PropertyGroup):
        file_url: StringProperty(name="File URL / key", description="Figma file URL (…/design/<key>/…) or bare file key")
        page: EnumProperty(name="Page", items=_page_enum_items)
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
        bundle_dir: StringProperty(name="Bundle folder", description="Folder containing scene.json and assets/", subtype="DIR_PATH")
        export_dir: StringProperty(name="Export to", description="Folder to write the bundle into", subtype="DIR_PATH")

    def build_options(s: "FIGMA_settings") -> builder.BuildOptions:
        return builder.BuildOptions(
            scale=s.scale, depth_step=s.depth_step, icon_mode=s.icon_mode, plane_orientation=s.orientation, center=s.center
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

    def _report_build(op: Operator, report: builder.BuildReport):
        for w in report.warnings:
            print("[figma_to_blender] warning:", w)
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

    class _ExportMixin:
        def _export(self, context, out_dir):
            s = context.scene.figma_to_blender
            client = _client(self, context)
            if client is None:
                return None
            if not _page_items or s.page == "NONE":
                self.report({"ERROR"}, "Fetch pages and pick one first")
                return None
            wm = context.window_manager
            wm.progress_begin(0, 100)
            try:
                key = parse_file_key(s.file_url)
                wm.progress_update(10)
                scene = scene_model.export_bundle(client, key, s.page, out_dir, export_options(s))
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
        bl_label = "Import page"
        bl_description = "Fetch the selected page from Figma and build it as 3D objects"

        def execute(self, context):
            s = context.scene.figma_to_blender
            out_dir = tempfile.mkdtemp(prefix="figma_", dir=bpy.app.tempdir or None)
            scene = self._export(context, out_dir)
            if scene is None:
                return {"CANCELLED"}
            try:
                report = builder.build_scene(scene, out_dir, build_options(s))
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self.report({"ERROR"}, "Build failed, see console")
                return {"CANCELLED"}
            _report_build(self, report)
            return {"FINISHED"}

    class FIGMA_OT_export_bundle(_ExportMixin, Operator):
        bl_idname = "figma.export_bundle"
        bl_label = "Export bundle to folder"
        bl_description = "Fetch the selected page and write scene.json + assets without building objects"

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
                report = builder.build_bundle(bundle_dir, build_options(s))
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self.report({"ERROR"}, "Build failed, see console")
                return {"CANCELLED"}
            _report_build(self, report)
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

            box = layout.box()
            box.label(text="Import options", icon="PREFERENCES")
            box.prop(s, "icon_mode")
            box.prop(s, "orientation")
            col = box.column(align=True)
            col.prop(s, "scale")
            col.prop(s, "depth_step")
            col.prop(s, "icon_max_size")
            col.prop(s, "raster_scale")
            box.prop(s, "center")
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
