#!/usr/bin/env python3
"""PPT Master - Generation Pipeline Tests

Check model output boundaries and preservation of earlier artifacts on retries.

Usage:
    python3 -m unittest tests.test_web_gui_pipeline

Dependencies:
    Project requirements
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from web_gui.pipeline import (  # noqa: E402
    _author_page,
    _check_pages,
    extract_svg,
    prepare_output_folders,
    recovery_layout,
    run_pipeline,
    verify_delivery,
)


class PipelineTests(unittest.TestCase):
    def test_authoring_and_format_repair_preserve_user_fact_constraints(self):
        request = "仅限给定事实：只支持文字输入；不要添加产品能力、支持格式或新步骤。"
        slide = {"page_number": 1, "title": "范围", "content_points": ["仅支持文字输入。"]}
        outline = {"title": "功能说明", "slides": [slide]}
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">' \
              '<text x="100" y="100">仅支持文字输入。</text></svg>'
        client = Mock()
        client.complete.side_effect = ["incomplete SVG", svg]
        with patch("web_gui.pipeline.progress"):
            result = _author_page(client, outline, slide, {"topic": request, "style": "简洁"},
                                  "0 0 1280 720")
        self.assertIn("仅支持文字输入。", result)
        self.assertEqual(client.complete.call_count, 2)
        for call in client.complete.call_args_list:
            context, _ = json.JSONDecoder().raw_decode(call.args[1])
            self.assertEqual(context["user_request"], request)
            self.assertEqual(context["current_slide"], slide)

    def test_recovery_layout_preserves_all_text_and_readable_size(self):
        points = ["输入可以是正文或附件。", "形成大纲后再制作页面。", "每页都附一份讲稿。"]
        layout = recovery_layout({"title": "完整链路", "content_points": points}, "0 0 1280 720")
        self.assertEqual(["".join(line["text"] for line in row["lines"]) for row in layout["body"]], points)
        self.assertTrue(all(row["font_size"] >= 22 for row in layout["body"]))
        self.assertLess(layout["body"][-1]["bounds"][1] + layout["body"][-1]["bounds"][3], 656)

    def test_dense_eleven_point_slide_fits_without_losing_content(self):
        points = [
            "第二步：确认统计的是哪些字节",
            "发送数据至少有四种口径：本地逻辑提交、链路传输、TLP有效载荷、接收端应用获得。",
            "对于ready/valid握手接口，应在握手成功时依据有效字节指示计数，只看到valid拉高不代表已接收。",
            "第三步：确认分母包含哪些时间",
            "示例：Gen3 x4，100 ms窗口内成功交付320,000,000 Byte，吞吐为3.20 GB/s。",
            "相对于单向编码后上限3.938 GB/s，效率为81.25%。",
            "关键洞察：",
            "81.25%不能直接说明物理线路只忙了81.25%，线路可能一直在传输，只是其中包含协议开销。",
            "如果把空闲和反压周期从分母中删掉，得到的是活跃传输期间的速率，不是整个窗口平均吞吐。",
            "极短窗口内缓存集中释放，也可能让本地接口显示高于链路长期平均能力的瞬时吞吐，需要检查缓存。",
            "方向、观察位置、有效字节定义和完整时间窗口缺少任何一项，性能数字都难以可靠比较。",
        ]
        layout = recovery_layout({"title": "核验吞吐数字的三步法（下）", "content_points": points},
                                 "0 0 1280 720")
        self.assertEqual(["".join(line["text"] for line in row["lines"]) for row in layout["body"]], points)
        self.assertTrue(all(row["font_size"] >= 22 for row in layout["body"]))
        self.assertTrue(all(row["bounds"][1] + row["bounds"][3] <= 656 for row in layout["body"]))

    def test_many_points_use_two_columns_with_original_reading_order(self):
        points = [f"第{i}项：保留原文和限定条件。" for i in range(1, 15)]
        layout = recovery_layout({"title": "核验清单", "content_points": points}, "0 0 1280 720")
        self.assertEqual(["".join(line["text"] for line in row["lines"]) for row in layout["body"]], points)
        self.assertEqual(len({row["bounds"][0] for row in layout["body"]}), 2)
        self.assertTrue(all(row["font_size"] >= 22 for row in layout["body"]))
        self.assertTrue(all(row["bounds"][1] + row["bounds"][3] <= 656 for row in layout["body"]))

    def test_unfit_content_still_fails_instead_of_truncating(self):
        with self.assertRaisesRegex(ValueError, "无法以可读字号"):
            recovery_layout({"title": "过长内容", "content_points": ["必须保留的完整事实。" * 300]},
                            "0 0 1280 720")

    def test_constrained_rebuild_retries_if_model_omits_a_required_point(self):
        slide = {"page_number": 1, "title": "核验清单", "content_points": ["完整时间窗口", "成功握手计数"]}
        prefix = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
        title = '<text x="72" y="76">核验清单</text>'
        first = '<text x="80" y="196">完整时间窗口</text>'
        second = '<text x="80" y="260">成功握手计数</text>'
        client = Mock()
        client.complete.side_effect = [prefix + title + first + '</svg>',
                                       prefix + title + first + second + '</svg>']
        with patch("web_gui.pipeline.progress"):
            result = _author_page(client, {"title": "Demo", "slides": [slide]}, slide, {},
                                  "0 0 1280 720", safe_layout=True)
        self.assertEqual(client.complete.call_count, 2)
        self.assertIn("成功握手计数", result)

    def test_recovery_compares_rendered_markdown_text_without_format_markers(self):
        slide = {"page_number": 1, "title": "清单", "content_points": ["### 统计口径", "**成功**握手与`valid`"]}
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">' \
              '<text x="72" y="76">清单</text><text x="80" y="196">统计口径</text>' \
              '<text x="80" y="260">成功握手与valid</text></svg>'
        client = Mock()
        client.complete.return_value = svg
        result = _author_page(client, {"title": "Demo", "slides": [slide]}, slide, {},
                              "0 0 1280 720", safe_layout=True)
        self.assertIn("成功握手与valid", result)
        self.assertEqual(client.complete.call_count, 1)

    def test_constrained_rebuild_is_checked_again_before_export(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            (project / "svg_output").mkdir()
            (project / "validation").mkdir()
            (project / "svg_output/01.svg").write_text("<svg/>")
            calls = []

            def checker(*args, **kwargs):
                calls.append(args)
                failing = len(calls) < 4
                report = {"summary": {"errors": int(failing)},
                          "files": [{"file": "01.svg", "errors": ["layout error"] if failing else []}]}
                (project / "validation/svg_quality_report.json").write_text(json.dumps(report))
                return Mock(returncode=int(failing), stderr="", stdout="")

            with patch("web_gui.pipeline._script", side_effect=checker), \
                    patch("web_gui.svg_repair.repair_module_bounds", return_value=("<svg/>", ["layout error"])), \
                    patch("web_gui.pipeline._author_page", return_value="<svg/>") as author:
                _check_pages(project, Mock(), {"slides": [{}]}, {}, "0 0 1280 720", ["01.svg"])
            self.assertEqual(len(calls), 4)
            self.assertTrue(author.call_args.kwargs["safe_layout"])

    def test_completed_notes_survive_a_later_model_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            (project / ".web_gui").mkdir()
            (project / ".web_gui/brief.json").write_text(json.dumps({"topic": "Demo", "format": "ppt169"}))
            outline = {"title": "Demo", "page_count_reason": "Two parts", "slides": [
                {"page_number": 1, "title": "First", "notes": "draft"},
                {"page_number": 2, "title": "Last", "notes": "draft"},
            ]}
            with patch("attribution_guard.require_skill_integrity"), \
                    patch("web_gui.ingestion.collect_chunks", return_value=[]), \
                    patch("web_gui.outline.plan_outline", return_value=outline), \
                    patch("web_gui.assets.prepare_images", return_value=[]), \
                    patch("web_gui.pipeline._author_page", return_value='<svg xmlns="http://www.w3.org/2000/svg"/>'), \
                    patch("web_gui.pipeline._check_pages"), \
                    patch("web_gui.pipeline._final_notes", side_effect=["A complete spoken first note.",
                                                                       RuntimeError("temporary note failure")]), \
                    patch("web_gui.pipeline._script") as script:
                with self.assertRaisesRegex(RuntimeError, "temporary note failure"):
                    run_pipeline(project, client=Mock())
                script.assert_not_called()
            self.assertEqual((project / "notes/01_slide.md").read_text().strip(), "A complete spoken first note.")
            self.assertFalse((project / "notes/02_slide.md").exists())

    def test_page_number_wrapper_normalization_preserves_visual_attributes(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">' \
              '<g id="page-number" data-pptx-bounds="1190 24 90 32">' \
              '<text x="1220" y="52" font-size="22" fill="#64748B" text-anchor="end">07 / 11</text>' \
              '</g></svg>'
        root = ElementTree.fromstring(extract_svg(svg, "0 0 1280 720"))
        self.assertEqual(root[0].tag.rsplit("}", 1)[-1], "text")
        self.assertEqual(root[0].get("data-pptx-role"), "page-number")
        self.assertEqual(root[0].get("text-anchor"), "end")
        self.assertEqual(root[0].get("x"), "1220")
        self.assertEqual(root[0].text, "07 / 11")

    def test_early_gate_reads_its_own_report(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            (project / "validation").mkdir()
            (project / "validation/svg_quality_early_report.json").write_text('{"summary":{"errors":0}}')
            with patch("web_gui.pipeline._script", return_value=Mock(returncode=0, stderr="", stdout="")):
                _check_pages(project, Mock(), {"slides": []}, {}, "0 0 1280 720", [], stage="early")

    def test_svg_code_fences_and_native_text(self):
        raw = '```svg\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">' \
              '<text x="100" y="100">Title</text></svg>\n```'
        self.assertIn("<text", extract_svg(raw, "0 0 1280 720"))

    def test_unsupplied_footer_branding_is_rejected(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">' \
              '<g id="footer"><text x="10" y="690">Invented AI Company</text>' \
              '<text x="1100" y="690">1 / 5</text></g></svg>'
        with self.assertRaisesRegex(ValueError, "署名"):
            extract_svg(svg, "0 0 1280 720", source_text="User supplied title")
        extract_svg(svg, "0 0 1280 720", source_text="Credit: Invented AI Company")

    def test_unsupplied_standalone_cover_date_is_rejected(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">' \
              '<text x="80" y="640">2026</text></svg>'
        with self.assertRaisesRegex(ValueError, "日期"):
            extract_svg(svg, "0 0 1280 720", source_text="A supplied title without a date")
        extract_svg(svg, "0 0 1280 720", source_text="2026 annual review")

    def test_composed_footer_source_labels_and_chinese_page_numbers(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">' \
              '<g id="footer"><text x="10" y="690">来源：D001-C0001 | Deck title · Closing</text>' \
              '<text x="1100" y="690">第 4 页 / 共 11 页</text></g></svg>'
        extract_svg(svg, "0 0 1280 720", source_text="Deck title. Closing. D001-C0001")

    def test_footer_caution_is_not_mistaken_for_author_credit(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">' \
              '<g id="footer-warning"><text x="10" y="690">注意保留关键限定条件。</text></g></svg>'
        extract_svg(svg, "0 0 1280 720", source_text="不得遗漏材料中的限制和风险。")

    def test_active_external_or_wrong_canvas_output_rejected(self):
        for fragment in ('<script/>', '<image href="https://example.com/a.png"/>',
                         '<foreignObject/>', '<rect onclick="alert(1)"/>',
                         '<image href="../../outside.png"/>'):
            with self.subTest(fragment=fragment), self.assertRaises(ValueError):
                extract_svg(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                            f'{fragment}<text x="1" y="1">t</text></svg>', "0 0 1280 720")
        with self.assertRaises(ValueError):
            extract_svg('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>', "0 0 1280 720")

    def test_retry_archives_old_roster_instead_of_mixing(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            (project / "svg_output").mkdir()
            (project / "notes").mkdir()
            (project / "svg_output" / "01_old.svg").write_text("old")
            (project / "notes" / "01_old.md").write_text("old note")
            prepare_output_folders(project)
            self.assertEqual(list((project / "svg_output").iterdir()), [])
            self.assertEqual(list((project / "notes").iterdir()), [])
            self.assertEqual(len(list((project / "backup").rglob("01_old.svg"))), 1)
            self.assertEqual(len(list((project / "backup").rglob("01_old.md"))), 1)

    def test_valid_pptx_without_notes_is_not_delivery(self):
        from pptx import Presentation
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.pptx"
            deck = Presentation()
            deck.slides.add_slide(deck.slide_layouts[6])
            deck.save(path)
            with self.assertRaises(ValueError):
                verify_delivery(path, 1)
            deck.slides[0].notes_slide.notes_text_frame.text = "Notes grounded in the slide."
            deck.save(path)
            verify_delivery(path, 1)
            with self.assertRaises(ValueError):
                verify_delivery(path, 2)
