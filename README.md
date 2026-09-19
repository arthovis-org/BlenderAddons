# BlenderAddons

Blender add-ons by arthovis-org. Each add-on lives in its own folder at the repo root and is
zipped on its own; `tools/build_zip.py` and the
[Build add-ons](.github/workflows/build-addon.yml) workflow produce one `dist/<name>.zip` per
add-on (GitHub ▸ *Actions* ▸ pick the run ▸ *Artifacts*).

## Add-ons

| Add-on | What it does | Docs |
|---|---|---|
| [`figma_to_blender/`](figma_to_blender/) | Imports a Figma page as editable 3D UI: text as text objects, rectangles as planes with a *Corner Radius* Bevel modifier, ellipses as filled curves, icons as SVG curves or textured planes, images as textured planes, frames/groups as parented Empties. Built non-destructively, so radii, sizes and transforms stay editable after import. Also runs as a CLI that exports an offline scene bundle. | [README](figma_to_blender/README.md) |

## Install an add-on

Download `<name>.zip` from the latest workflow run (or build it locally, standard library only):

```sh
python tools/build_zip.py --addon figma_to_blender   # -> dist/figma_to_blender.zip
python tools/build_zip.py                            # every add-on in the repo
```

**Blender 4.2+ (extension):** drag the zip into Blender, or *Edit ▸ Preferences ▸ Get Extensions ▸
⌄ ▸ Install from Disk*. **Blender 3.6 - 4.1:** *Edit ▸ Preferences ▸ Add-ons ▸ Install…* with the
same zip. Each add-on's README has the details.

## Layout

```
<addon>/                  the add-on package (this is what gets zipped)
<addon>/README.md         its documentation
<addon>/docs/             images and other docs (not zipped)
<addon>/tests/            its tests, run from the repo root (not zipped)
tools/build_zip.py        zips every add-on folder into dist/
.github/workflows/        runs each add-on's tests and uploads its zip
```

## Adding an add-on

1. Create `<name>/` with a `blender_manifest.toml` (Blender 4.2+) and/or an `__init__.py`
   declaring `bl_info`; `tools/build_zip.py` picks it up automatically.
2. Put its tests in `<name>/tests/` so `python -m unittest discover -s <name>/tests -t .` works
   from the repo root, and add `<name>` to the `addon` matrix in
   `.github/workflows/build-addon.yml`.
3. Add a row to the table above.

## License

MIT, see [LICENSE](LICENSE).
