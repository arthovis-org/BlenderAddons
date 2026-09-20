"""Pure-Python tests for the offline font lookup (user folder, name-table / file-name indexing, matching)."""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from figma_to_blender import fonts  # noqa: E402

# a real TrueType file, if the machine has one, to exercise the name-table parser
REAL_TTF = next(
    (
        p
        for p in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "C:\\Windows\\Fonts\\arialbd.ttf",
        )
        if os.path.isfile(p)
    ),
    None,
)


class FontsFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="figma_fonts_")
        sub = os.path.join(self.tmp, "Inter", "static")
        os.makedirs(sub)
        # fake font files named like the real ones (their tables are unreadable -> indexed by file name)
        for name in ("Inter-Regular.ttf", "Inter-Bold.ttf", "Inter-BoldItalic.ttf", "Inter-SemiBold.otf", "Inter-Light.ttf"):
            with open(os.path.join(sub, name), "wb") as fh:
                fh.write(b"not really a font")
        with open(os.path.join(self.tmp, "OpenSans_Italic.ttf"), "wb") as fh:
            fh.write(b"x")
        with open(os.path.join(self.tmp, "Inter-Black.woff2"), "wb") as fh:  # web font: skipped
            fh.write(b"wOF2")
        with open(os.path.join(self.tmp, "readme.txt"), "w") as fh:
            fh.write("not a font")
        self.index = fonts.scan_dir(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        fonts._USER_INDEX.clear()

    def test_scan_is_recursive_and_skips_web_fonts(self):
        names = sorted(os.path.basename(e.path) for e in self.index)
        self.assertEqual(
            names, ["Inter-Bold.ttf", "Inter-BoldItalic.ttf", "Inter-Light.ttf", "Inter-Regular.ttf", "Inter-SemiBold.otf", "OpenSans_Italic.ttf"]
        )
        self.assertEqual(fonts.scan_dir(os.path.join(self.tmp, "does-not-exist")), [])
        self.assertEqual(fonts.scan_dir(""), [])

    def test_entry_from_filename(self):
        e = fonts.entry_from_filename("/x/Inter-BoldItalic.ttf")
        self.assertEqual((e.family, e.postscript, e.weight, e.italic), ("Inter", "Inter-BoldItalic", 700, True))
        e = fonts.entry_from_filename("/x/OpenSans_Italic.ttf")
        self.assertEqual((e.family, e.weight, e.italic), ("Open Sans", 400, True))
        e = fonts.entry_from_filename("/x/Roboto.ttf")
        self.assertEqual((e.family, e.postscript, e.weight, e.italic), ("Roboto", "Roboto", 400, False))
        self.assertEqual(fonts.entry_from_filename("/x/Inter-SemiBold.otf").weight, 600)
        self.assertEqual(fonts.entry_from_filename("/x/Inter-ExtraLight.otf").weight, 200)

    def test_matching_postscript_then_family_and_style(self):
        find = lambda **kw: os.path.basename(fonts.find_font(index=self.index, **kw) or "")  # noqa: E731
        # PostScript name (Figma's fontPostScriptName) wins, even when family / weight disagree
        self.assertEqual(find(postscript_name="Inter-SemiBold", family="Inter", weight=400), "Inter-SemiBold.otf")
        self.assertEqual(find(postscript_name="inter-bolditalic"), "Inter-BoldItalic.ttf")
        # family + weight (+ italic): closest weight, italic preferred when asked for
        self.assertEqual(find(family="Inter", weight=700), "Inter-Bold.ttf")
        self.assertEqual(find(family="Inter", weight=700, italic=True), "Inter-BoldItalic.ttf")
        self.assertEqual(find(family="Inter", weight=500), "Inter-Regular.ttf")  # 400 / 600 tie -> first indexed (sorted names)
        self.assertEqual(find(family="Inter", weight=650), "Inter-Bold.ttf")
        self.assertEqual(find(family="Inter", weight=200), "Inter-Light.ttf")
        self.assertEqual(find(family="Inter"), "Inter-Regular.ttf")
        # unknown PostScript name falls back to the family; spaces / case in the family do not matter
        self.assertEqual(find(postscript_name="Inter-Nope", family="Inter", weight=700), "Inter-Bold.ttf")
        self.assertEqual(find(family="open sans", italic=True), "OpenSans_Italic.ttf")
        self.assertEqual(find(family="Open Sans", italic=False), "OpenSans_Italic.ttf")  # only style available
        self.assertEqual(find(family="Roboto"), "")
        self.assertIsNone(fonts.find_font(index=[], family="Inter"))

    def test_user_dirs_are_cached_and_win_over_system_fonts(self):
        entries = fonts.user_font_index([self.tmp])
        self.assertEqual(len(entries), 6)
        with open(os.path.join(self.tmp, "Inter-Medium.ttf"), "wb") as fh:
            fh.write(b"x")
        self.assertEqual(len(fonts.user_font_index([self.tmp])), 6)  # cached per session
        self.assertEqual(len(fonts.user_font_index([self.tmp], refresh=True)), 7)
        self.assertEqual(fonts.user_font_index(None), [])
        self.assertEqual(fonts.user_font_index([""]), [])
        combined = fonts.font_index(user_dirs=[self.tmp])
        self.assertEqual([e.path for e in combined[:7]], [e.path for e in fonts.user_font_index([self.tmp])])
        path = fonts.find_font(family="Inter", weight=700, user_dirs=[self.tmp])
        self.assertEqual(os.path.basename(path), "Inter-Bold.ttf")

    @unittest.skipIf(REAL_TTF is None, "no TrueType file found on this machine")
    def test_real_font_is_indexed_from_its_tables(self):
        dest = os.path.join(self.tmp, "renamed-file.ttf")
        shutil.copy(REAL_TTF, dest)
        e = fonts.read_font_file(dest)
        self.assertTrue(e.family)  # from the name table, not "renamed"
        self.assertNotEqual(e.family, "renamed")
        self.assertEqual(e.weight, 700)
        self.assertFalse(e.italic)
        index = fonts.scan_dir(self.tmp)
        self.assertEqual(fonts.find_font(index=index, family=e.family, weight=700), dest)


class StyleNameTests(unittest.TestCase):
    def test_weight_style_name(self):
        self.assertEqual(fonts.weight_style_name(400), "Regular")
        self.assertEqual(fonts.weight_style_name(400, True), "Italic")
        self.assertEqual(fonts.weight_style_name(700, True), "Bold Italic")
        self.assertEqual(fonts.weight_style_name(600), "Semi Bold")
        self.assertEqual(fonts.weight_style_name(650), "Bold")  # rounds to the nearest hundred
        self.assertEqual(fonts.weight_style_name(None), "Regular")
        self.assertEqual(fonts.weight_style_name("900"), "Black")
        self.assertEqual(fonts.weight_style_name(1000), "Black")

    def test_style_to_weight(self):
        self.assertEqual(fonts.style_to_weight("Semi Bold Italic"), 600)
        self.assertEqual(fonts.style_to_weight("ExtraBold"), 800)
        self.assertEqual(fonts.style_to_weight("Book"), 400)
        self.assertEqual(fonts.style_to_weight(""), 400)


if __name__ == "__main__":
    unittest.main()
