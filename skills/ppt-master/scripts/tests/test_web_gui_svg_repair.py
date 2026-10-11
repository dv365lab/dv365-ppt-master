#!/usr/bin/env python3
"""PPT Master - Web GUI SVG Bounds Repair Tests

Exercise metadata-only repair using real local checker diagnostic messages.

Usage:
    python3 -m unittest tests.test_web_gui_svg_repair

Examples:
    python3 -m unittest tests.test_web_gui_svg_repair

Dependencies:
    Existing SVG quality checker dependencies
"""

import unittest
from xml.etree import ElementTree as ET

from svg_quality.checker import SVGQualityChecker
from web_gui.svg_repair import repair_module_bounds

SVG = "http://www.w3.org/2000/svg"
VIEWBOX = "0 0 1280 720"


def diagnostic(inner, outer=(60, 100, 260, 150), group="content"):
    result = {"errors": [], "warnings": []}
    SVGQualityChecker._record_bounds_overflow(
        result, subject='<text id="body">', inner=inner, outer=outer,
        container=f'<g id="{group}"> data-pptx-bounds',
        repair="expand the root module bounds into available non-overlapping space; otherwise reflow the text",
    )
    return (result["errors"] + result["warnings"])[0]


def document(extra="", attributes="", root_attributes=""):
    return (f'<svg xmlns="{SVG}" viewBox="{VIEWBOX}" {root_attributes}>'
            f'<g id="content" data-pptx-bounds="60 100 200 50" {attributes}>'
            '<text id="body" x="60" y="140" font-size="24">Unchanged text</text>'
            '<rect x="70" y="110" width="30" height="20" fill="#123456"/>'
            '</g>' + extra + '</svg>')


class ModuleBoundsRepairTests(unittest.TestCase):
    def test_expands_only_metadata_with_two_pixel_measurement_padding(self):
        source = document()
        error = diagnostic((60, 127.6, 246.6, 180.4))
        fixed, unresolved = repair_module_bounds(source, [error], VIEWBOX)
        self.assertEqual(unresolved, [])
        before, after = ET.fromstring(source), ET.fromstring(fixed)
        self.assertEqual(before.attrib, after.attrib)
        self.assertIsNone(after.get("font-family"))
        expected = (58, 100, 260, 182.4)
        self.assertEqual(SVGQualityChecker._resolved_root_module_bounds(after[0])[1], expected)
        for old, new in zip(before[0], after[0]):
            self.assertEqual(old.attrib, new.attrib)
            self.assertEqual(old.text, new.text)
        self.assertEqual(after[0].get("id"), "content")

    def test_all_reported_children_are_unioned_before_repair(self):
        errors = [diagnostic((60, 127.6, 246.6, 180.4)), diagnostic((245, 112, 300, 145))]
        fixed, unresolved = repair_module_bounds(document(), errors, VIEWBOX)
        bounds = SVGQualityChecker._resolved_root_module_bounds(ET.fromstring(fixed)[0])[1]
        self.assertEqual(bounds, (58, 100, 302, 182.4))
        self.assertEqual(unresolved, [])

    def test_collision_or_off_canvas_is_left_for_model_repair(self):
        neighbor = '<g id="other" data-pptx-bounds="50 170 300 80"><text x="50" y="200">Other</text></g>'
        for source, error in [
            (document(neighbor), diagnostic((60, 127.6, 246.6, 180.4))),
            (document(), diagnostic((-5, 120, 260, 180))),
            (document(), diagnostic((60, 120, 1290, 180))),
        ]:
            with self.subTest(error=error):
                fixed, unresolved = repair_module_bounds(source, [error], VIEWBOX)
                self.assertEqual(fixed, source)
                self.assertEqual(unresolved, [error])

    def test_one_pixel_overlap_and_exempt_background_follow_checker_rules(self):
        error = diagnostic((60, 127.6, 246.6, 180.4))
        neighbor = '<g id="other" data-pptx-bounds="50 181.4 300 80"/>'
        background = '<g id="bg" data-pptx-role="background" data-pptx-bounds="0 0 1280 720"/>'
        fixed, unresolved = repair_module_bounds(document(neighbor + background), [error], VIEWBOX)
        self.assertNotEqual(fixed, document(neighbor + background))
        self.assertEqual(unresolved, [])

    def test_duplicate_ids_structured_groups_and_transforms_are_not_repaired(self):
        error = diagnostic((60, 127.6, 246.6, 180.4))
        sources = [
            document('<g id="content" data-pptx-bounds="500 100 100 100"/>'),
            document(attributes='data-pptx-placeholder="body"'),
            document(attributes='data-pptx-layer="master"'),
            document(attributes='data-pptx-layer="layout"'),
            document(attributes='transform="translate(10 0)"'),
            document(root_attributes='transform="scale(1)"'),
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(repair_module_bounds(source, [error], VIEWBOX), (source, [error]))

    def test_nested_group_diagnostic_and_malformed_or_dict_errors_are_preserved(self):
        source = document('<g id="outer" data-pptx-bounds="500 100 200 300">'
                          '<g id="inner" data-pptx-bounds="60 100 200 50"/></g>')
        nested = diagnostic((60, 127, 247, 180), group="inner")
        malformed = 'content exceeds <g id="content"> data-pptx-bounds content (NaN, 2)-(3, 4)'
        dictionary = {"message": diagnostic((60, 127, 247, 180))}
        errors = [nested, malformed, dictionary, 123]
        self.assertEqual(repair_module_bounds(source, errors, VIEWBOX), (source, errors))

    def test_success_resolves_only_its_exact_error(self):
        fixed_error = diagnostic((60, 127.6, 246.6, 180.4))
        unrelated = "Text runs are too fragmented"
        stale = diagnostic((60, 127.6, 246.6, 180.4), outer=(10, 20, 30, 40))
        fixed, unresolved = repair_module_bounds(document(), [fixed_error, unrelated, stale], VIEWBOX)
        self.assertNotEqual(fixed, document())
        self.assertEqual(unresolved, [unrelated, stale])

    def test_two_proposed_expansions_must_not_collide(self):
        source = document('<g id="other" data-pptx-bounds="60 210 200 50"/>')
        errors = [diagnostic((60, 127, 247, 190)),
                  diagnostic((60, 185, 247, 250), outer=(60, 210, 260, 260), group="other")]
        self.assertEqual(repair_module_bounds(source, errors, VIEWBOX), (source, errors))
