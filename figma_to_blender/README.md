# Figma to Blender

A Blender add-on that imports a **Figma page as editable 3D UI**: text becomes Blender text
objects, rectangles become planes with a *Corner Radius* Bevel modifier, ellipses become
filled curves, icons become SVG curves (or textured planes), image fills become textured
planes, and Figma frames/groups become parented Empties, so you can grab a whole card or menu
and move it in 3D. Everything is built [non-destructively](#non-destructive-by-design).

It talks to the Figma REST API directly from Blender (standard library only, no `requests`),
and the same pure-Python core also runs as a CLI so a page can be exported once to an offline
**scene bundle** (`scene.json` + `assets/`) and imported reproducibly later.

![Fixture page imported and rendered headlessly](docs/render.png)

## Install

Grab `figma_to_blender.zip`: every push and pull request to [this repo](../README.md) runs the
[Build add-ons](../.github/workflows/build-addon.yml) workflow, which runs the tests and uploads
the zip as the `figma_to_blender` artifact (GitHub ▸ *Actions* ▸ pick the run ▸ *Artifacts*).
To build it locally (standard library only, from the repo root):

```sh
python tools/build_zip.py --addon figma_to_blender   # -> ./dist/figma_to_blender.zip
```

**Requires Blender 5.0 or newer.** The add-on is a Blender extension: drag the zip into the
Blender window, or use *Edit ▸ Preferences ▸ Get Extensions ▸ ⌄ ▸ Install from Disk*. The zip
contains the `figma_to_blender/` folder with `blender_manifest.toml` inside it. Older Blender
versions are not supported.

*SVG curves* icon mode uses Blender's bundled SVG importer (`bpy.ops.import_curve.svg`, part of
Blender 5.0 and of the `bpy` 5.0 wheel); if a custom build lacks it, icons fall back to planes
with a warning.

## Get a Figma token

Figma ▸ account menu ▸ *Settings* ▸ *Security* ▸ *Personal access tokens* ▸ *Generate new token*
with the **File content: read** scope. Paste it into *Edit ▸ Preferences ▸ Add-ons ▸ Figma to
Blender ▸ Personal access token* (or set the `FIGMA_TOKEN` environment variable). You need at
least view access to the file you import.

## Use in Blender

Open the 3D Viewport sidebar (`N`) ▸ **Figma** tab.

1. Paste the file URL (`https://www.figma.com/design/<key>/...`) or bare file key.
2. **Fetch pages**, then pick a page from the dropdown.
3. Choose options:
   - **Icons**: *SVG curves* or *Image planes* (see below).
   - **Orientation**: *Upright (XZ)*, the UI faces -Y like the front view, or *Flat (XY)*.
   - **Scale**: metres per Figma px (default `0.001`, a 360 px card is 36 cm wide).
   - **Depth step**: offset per element in draw order so overlapping shapes never z-fight
     (later-drawn = in front).
   - **Corner segments**: segments per rounded corner on the *Corner Radius* Bevel modifier
     (default 8; change it later per object in the modifier itself).
   - **Icon max size**, **Raster scale**, **Center at origin**.
4. **Import page**. A new collection named after the page appears, with one Empty per Figma
   frame/group and children parented to it. Every object carries `figma_id`, `figma_type`,
   `figma_name` custom properties; text whose font was not found gets `figma_font`.

**Offline bundle** box: *Export bundle to folder* fetches without building; *Import bundle*
builds from a folder written earlier by the add-on or the CLI.

The operator prints a summary (counts per kind, fonts not found, warnings) to the status bar
and full warnings to the system console.

## CLI

Runs with plain CPython 3.8+ (no Blender needed):

```sh
export FIGMA_TOKEN=figd_...
python -m figma_to_blender.cli --file https://www.figma.com/design/<key>/Name --list-pages
python -m figma_to_blender.cli --file <key> --page "Page 1" --out ./bundle \
    --icon-format svg --raster-scale 2 --icon-max-size 128
```

Then in Blender: *Offline bundle ▸ Import bundle* pointing at `./bundle`, or from a script:

```python
import sys; sys.path.append("/path/to/repo")
from figma_to_blender import builder
report = builder.build_bundle("./bundle", builder.BuildOptions(icon_mode="SVG"))
print(report.summary())
```

## Non-destructive by design

Whatever Blender can express as an object property, modifier or curve parameter is **not**
baked into geometry, so you can keep tweaking after import:

| Figma | Blender | Where to edit |
|---|---|---|
| Rectangle / frame fill | 4-vertex plane at the node size, object scale 1 | mesh stays a sharp quad |
| Corner radius (`cornerRadius`, `rectangleCornerRadii`) | **Bevel modifier** *Corner Radius*: vertices only, `width` = largest radius, per-corner ratio stored as vertex bevel weight (tl, tr, br, bl), segments from *Corner segments* | modifier panel (width / segments), vertex bevel weights for per-corner radii; every rect carries the modifier even at radius 0 so you can dial one in |
| Ellipse | filled **2D Bezier curve** (4 aligned points, resolution 24), sized through its control points | curve edit mode, `Resolution Preview U` |
| Image fill | plane with an image texture material (+ the same Bevel modifier when the node has radii) | material nodes / modifier |
| Icon (SVG mode) | the importer's curve objects, untouched, under an Empty whose **object transform** scales the SVG to the node size | move / scale the Empty; the curves are the raw SVG paths |
| Position, rotation, flip | object `matrix_world` (rotation on the object, never in the mesh) | N panel |
| Fill colour, opacity | emission material shared per colour | material |
| Text | Blender text object (`size`, `align_x/y`, `space_line`, `space_character`, text box) | data properties |

Still baked, because Blender has no parameter for it: the plane's *size* (a plane is its four
vertices; scaling the object instead would distort the bevel), the ellipse's *size* (curve
control points, kept editable), and the text baseline offset (a translation in the object
matrix that compensates Blender's TOP alignment). Gradients are averaged into one colour and
strokes/effects are not imported (see Limitations).

## How it works

```
Figma REST API ──► figma_api.py ──► scene_model.py ──► scene.json + assets/ ──► builder.py ──► Blender objects
                   (urllib only)     (pure Python)        (the "bundle")          (bpy)
```

- `GET /v1/files/{key}?depth=1` lists pages; `GET /v1/files/{key}/nodes?ids=<page>&geometry=paths`
  fetches the tree with `size` + `relativeTransform`, which are composed down from the page
  root so nested and rotated frames land where Figma shows them (falls back to
  `absoluteBoundingBox` when missing).
- Icons and image fills are rendered by `GET /v1/images/{key}` in batches of 40 with retry/backoff
  on 429; a failed render is logged and skipped, never fatal.
- `scene.json` is a flat draw-ordered list of `text | rect | ellipse | icon | image | group`
  elements with world transform, size, first visible fill, opacity, corner radii and text style.

### Icon detection and SVG vs planes

A node is an icon when it is a `VECTOR`, `BOOLEAN_OPERATION`, `STAR`, `LINE` or
`REGULAR_POLYGON`, **or** a `GROUP`/`FRAME`/`INSTANCE`/`COMPONENT` no larger than *Icon max
size* (default 128 px) whose descendants are all vector-like (no text, no image fills). Icons
are exported as one unit and their children are not imported separately. Everything larger
recurses into its children as a group. A node whose first visible fill is an image is imported
as an image plane.

| | SVG curves | Image planes |
|---|---|---|
| Editable / extrudable | yes, real curve objects | no |
| Resolution | independent | fixed (`raster scale` × node size) |
| Gradients, shadows, blend modes | lost (flat fills only) | pixel-perfect |
| Scene weight | heavy for complex icons | one quad + texture |

Curves come from Blender's bundled SVG importer. The add-on does not trust its 90 dpi scale:
it measures the importer's px→metre factor once, then scales each icon so the SVG viewBox
matches the node size exactly (falling back to bounding-box fitting for SVGs without a
viewBox). If the importer is unavailable, icons fall back to planes with a warning.

## Limitations (v0.1)

- Gradients are approximated by a single averaged colour (`fill_approx` flag / `figma_fill_approx`
  property); image fills on shapes other than the first fill are ignored.
- Strokes, effects (shadows, blurs), blend modes and masks are not imported (they do survive
  inside PNG icons/images).
- Fonts must be installed locally; the add-on matches by PostScript name, then family + weight.
  Otherwise Blender's default font is used and the family is recorded in `figma_font`.
- Text vertical metrics are approximated (ascender ≈ 0.8 em); mixed styles within one text
  node (`characterStyleOverrides`) are not supported, the whole node uses its base style.
- Component variants and instances are imported as they appear; auto-layout is baked to
  absolute positions (as the API reports them).
- Only the first visible fill of each node is used; `clipsContent` is ignored.

## Roadmap

- Strokes as outline curves / inset meshes, drop shadows as translucent planes.
- Real gradients via colour-ramp shader nodes.
- Per-run text styling from `characterStyleOverrides`.
- Extrude presets (depth per kind) and a "re-sync from Figma" operator that updates existing
  objects by `figma_id` (the Bevel modifier / curve data make this a property update).

## Development

All commands run from the repository root (this add-on lives in `figma_to_blender/`, next to the
other add-ons in [arthovis-org/BlenderAddons](../README.md)):

```sh
python -m unittest discover -s figma_to_blender/tests -t .   # pure-python tests (fixture in tests/fixtures), also run in CI
pip install bpy && python -m pytest figma_to_blender/tests/  # also runs the builder tests (bpy 5.0 wheel, Python 3.11)
FIGMA_RENDER_OUT=render.png python -m pytest figma_to_blender/tests/test_builder_bpy.py -k render
python tools/build_zip.py --addon figma_to_blender          # the same zip CI uploads
```

The bpy tests build the fixture page in both icon modes and assert object counts, types,
positions, rotation, parenting, materials, the SVG icon's bounding box, the *Corner Radius*
Bevel modifier (width, segments, per-corner vertex weights, evaluated vertex count) and the
ellipse curve (2D, fill BOTH, dimensions).

## License

MIT, see [LICENSE](LICENSE).
