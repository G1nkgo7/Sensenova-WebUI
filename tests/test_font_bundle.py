import importlib.util
import sys
import unittest
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont


ROOT = Path(__file__).resolve().parents[1]


def _weight_axis(font):
    return next(axis for axis in font["fvar"].axes if axis.axisTag == "wght")


class ReleaseFontBundleTests(unittest.TestCase):
    def test_bundled_noto_sans_is_a_real_100_to_900_variable_font(self):
        path = ROOT / "bundled/fonts/NotoSansSC.ttf"
        font = TTFont(path, lazy=False)
        try:
            axis = _weight_axis(font)
            self.assertEqual((100.0, 900.0), (axis.minValue, axis.maxValue))
            self.assertIn("gvar", font)

            regular = instantiateVariableFont(font, {"wght": 400}, inplace=False)
            black = instantiateVariableFont(font, {"wght": 900}, inplace=False)
            glyph_name = font.getBestCmap()[ord("中")]
            self.assertNotEqual(
                regular["glyf"][glyph_name].compile(regular["glyf"]),
                black["glyf"][glyph_name].compile(black["glyf"]),
            )
        finally:
            font.close()

    def test_all_public_skill_entries_require_the_variable_weight_range(self):
        for skill in ("sn-ppt-web", "sn-ppt-web-zh", "sn-ppt-web-en"):
            path = ROOT / f"bundled/static-ppt-skill-suite/skills/{skill}/scripts/font_bundle.py"
            name = f"font_bundle_{skill.replace('-', '_')}"
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
                faces = module.FAMILY_FACES["Noto Sans SC"]
                self.assertEqual(1, len(faces), skill)
                self.assertEqual("100 900", faces[0].weight, skill)
                self.assertTrue(
                    module._font_supports_face(ROOT / "bundled/fonts/NotoSansSC.ttf", faces[0]),
                    skill,
                )
            finally:
                sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
