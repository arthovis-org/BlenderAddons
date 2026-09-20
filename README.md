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

Download the latest zip from [`releases/`](releases/) (for example
[`figma_to_blender-v0.1.0.zip`](releases/figma_to_blender/figma_to_blender-v0.1.0.zip)), grab
`<name>.zip` from the latest workflow run, or build it locally (standard library only):

```sh
python tools/build_zip.py --addon figma_to_blender   # -> dist/figma_to_blender.zip
python tools/build_zip.py                            # every add-on in the repo
```

All add-ons target **Blender 5.0 or newer** (they are packaged as extensions and are not tested
on older versions). Drag the zip into the Blender window, or *Edit ▸ Preferences ▸ Get Extensions ▸
⌄ ▸ Install from Disk*. Each add-on's README has the details.

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

1. Create `<name>/` with a `blender_manifest.toml` (`blender_version_min = "5.0.0"`);
   `tools/build_zip.py` picks it up automatically.
2. Put its tests in `<name>/tests/` so `python -m unittest discover -s <name>/tests -t .` works
   from the repo root, and add `<name>` to the `addon` matrix in
   `.github/workflows/build-addon.yml`.
3. Add a row to the table above.

## License

MIT, see [LICENSE](LICENSE).
