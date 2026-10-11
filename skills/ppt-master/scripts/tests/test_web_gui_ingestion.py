#!/usr/bin/env python3
"""PPT Master - Web GUI Ingestion Regression Tests

Exercise complete source coverage, converter failure handling, and path isolation.

Usage:
    python -m unittest tests.test_web_gui_ingestion

Examples:
    python -m unittest tests.test_web_gui_ingestion

Dependencies:
    None (only uses standard library)
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from web_gui.ingestion import collect_chunks


class IngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        (self.project / "sources").mkdir()
        self.client = Mock()
        self.progress = Mock()
        self.cancel = threading.Event()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def collect(self, brief: dict) -> list[dict]:
        return collect_chunks(self.project, brief, self.client, self.progress, self.cancel)

    def assert_full_coverage(self, chunks: list[dict], original: str) -> None:
        manifest = json.loads((self.project / "analysis/source_manifest.json").read_text(encoding="utf-8"))
        rows = manifest["chunks"]
        end = 0
        for chunk, row in zip(chunks, rows):
            self.assertLessEqual(row["start"], end)
            self.assertGreater(row["end"], end)
            self.assertEqual(chunk["text"], original[row["start"]:row["end"]])
            self.assertLessEqual(len(chunk["text"]), manifest["chunk_size_chars"])
            end = row["end"]
        self.assertEqual(end, len(original))

    def test_long_chinese_and_english_keep_every_character_with_bounded_overlap(self) -> None:
        text = "# 数据\n\n" + ("中文句子保留标点。English sentences!  " * 500) + "FINAL_BLOCK 最后一段"
        chunks = self.collect({"topic": text, "analysis_depth": "fast", "source_files": []})
        self.assertGreater(len(chunks), 5)
        self.assert_full_coverage(chunks, text)
        self.assertTrue(chunks[-1]["text"].endswith("FINAL_BLOCK 最后一段"))

    def test_unbroken_content_makes_monotonic_progress(self) -> None:
        text = "测" * 13001
        chunks = self.collect({"topic": text, "analysis_depth": "normal"})
        self.assert_full_coverage(chunks, text)
        self.assertLess(len(chunks), 10)

    def test_multiple_files_and_full_typed_input_are_kept(self) -> None:
        (self.project / "sources/first.txt").write_bytes("第一份\r\nEND_ONE".encode("utf-16"))
        csv = "编号,说明\n" + "1,完整数据\n" * 400 + "999,最后一行"
        (self.project / "sources/second.csv").write_bytes(csv.encode("gb18030"))
        topic = "用户完整要求 " * 400 + "USER_END"
        chunks = self.collect({"topic": topic, "source_files": ["first.txt", "second.csv"]})
        sources = {chunk["source"] for chunk in chunks}
        self.assertEqual(sources, {"网页输入", "first.txt", "second.csv"})
        self.assertEqual(chunks[0]["source"], "first.txt")
        self.assertIn("USER_END", "".join(chunk["text"] for chunk in chunks if chunk["source"] == "网页输入"))
        self.assertIn("999,最后一行", "".join(chunk["text"] for chunk in chunks if chunk["source"] == "second.csv"))
        self.client.complete.assert_not_called()

    def test_short_topic_only_uses_topic_route(self) -> None:
        self.assertEqual(self.collect({"topic": "机器学习入门"}), [])

    def test_converter_failure_never_silently_omits_attachment(self) -> None:
        (self.project / "sources/broken.docx").write_bytes(b"bad")
        process = Mock()
        process.poll.return_value = 1
        with patch("web_gui.ingestion.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "broken.docx"):
                self.collect({"topic": "主题", "source_files": ["broken.docx"]})

    def test_converter_keeps_stdout_stderr_separate_and_validates_output(self) -> None:
        source = self.project / "sources/report.pdf"
        source.write_bytes(b"mock PDF")

        def spawn(args, **kwargs):
            output = Path(args[args.index("-o") + 1])
            output.write_text("完整转换文字 END", encoding="utf-8")
            kwargs["stdout"].write(json.dumps({"input": str(source), "markdown": str(output)}).encode())
            self.assertIsNot(kwargs["stdout"], kwargs["stderr"])
            self.assertFalse(kwargs.get("shell", False))
            process = Mock()
            process.poll.return_value = 0
            process.returncode = 0
            return process

        with patch("web_gui.ingestion.subprocess.Popen", side_effect=spawn):
            chunks = self.collect({"topic": "主题", "source_files": ["report.pdf"]})
        self.assertTrue(next(chunk for chunk in chunks if chunk["source"] == "report.pdf")["text"].endswith("END"))
        self.assertEqual(source.read_bytes(), b"mock PDF")

    def test_path_traversal_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.collect({"topic": "主题", "source_files": ["../secret.txt"]})

    def test_symlink_outside_sources_is_rejected(self) -> None:
        outside = Path(self.temp.name) / "private.txt"
        outside.write_text("private", encoding="utf-8")
        try:
            (self.project / "sources/outside.txt").symlink_to(outside)
        except OSError as error:
            self.skipTest(str(error))
        with self.assertRaises(ValueError):
            self.collect({"topic": "主题", "source_files": ["outside.txt"]})

    def test_already_cancelled_job_does_no_conversion(self) -> None:
        self.cancel.set()
        with patch("web_gui.ingestion.subprocess.Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "取消"):
                self.collect({"topic": "内容" * 1000})
            popen.assert_not_called()

    def test_cancel_during_conversion_terminates_owned_process(self) -> None:
        (self.project / "sources/report.docx").write_bytes(b"mock document")
        process = Mock()
        process.pid = 123456
        process.poll.return_value = None

        def spawn(*args, **kwargs):
            self.cancel.set()
            return process

        with patch("web_gui.ingestion.subprocess.Popen", side_effect=spawn), patch(
            "web_gui.ingestion._stop_process"
        ) as stop:
            with self.assertRaisesRegex(RuntimeError, "取消"):
                self.collect({"topic": "主题", "source_files": ["report.docx"]})
            stop.assert_called_once_with(process)

    def test_converter_timeout_terminates_owned_process(self) -> None:
        (self.project / "sources/report.docx").write_bytes(b"mock document")
        process = Mock()
        process.poll.return_value = None
        with patch("web_gui.ingestion.subprocess.Popen", return_value=process), patch(
            "web_gui.ingestion.time.monotonic", side_effect=[0, 301]
        ), patch("web_gui.ingestion._stop_process") as stop:
            with self.assertRaisesRegex(RuntimeError, "300"):
                self.collect({"topic": "主题", "source_files": ["report.docx"]})
            stop.assert_called_once_with(process)

    def test_output_symlink_cannot_overwrite_an_original(self) -> None:
        source = self.project / "sources/original.txt"
        source.write_text("原始资料", encoding="utf-8")
        folder = self.project / "analysis/source_markdown"
        folder.mkdir(parents=True)
        try:
            (folder / "000_typed_input.md").symlink_to(source)
        except OSError as error:
            self.skipTest(str(error))
        with self.assertRaises(ValueError):
            self.collect({"topic": "新的输入", "source_files": ["original.txt"]})
        self.assertEqual(source.read_text(encoding="utf-8"), "原始资料")

    def test_empty_attachment_fails_instead_of_disappearing(self) -> None:
        (self.project / "sources/empty.txt").write_bytes(b"")
        with self.assertRaisesRegex(RuntimeError, "empty.txt"):
            self.collect({"topic": "主题", "source_files": ["empty.txt"]})

    def test_native_svg_text_extraction_preserves_tspan_text(self) -> None:
        svg = '<svg xmlns="http://www.w3.org/2000/svg"><text>标题<tspan>补充</tspan></text><text>末句</text></svg>'
        (self.project / "sources/text.svg").write_text(svg, encoding="utf-8")
        chunks = self.collect({"source_files": ["text.svg"]})
        self.assertEqual(chunks[0]["text"], "标题补充\n末句")
        self.client.complete.assert_not_called()

    def test_image_uses_model_vision_and_vector_only_svg_fails(self) -> None:
        (self.project / "sources/image.png").write_bytes(b"mock image")
        self.client.complete.return_value = "图中提取的原文"
        chunks = self.collect({"topic": "主题", "source_files": ["image.png"]})
        self.assertEqual(next(chunk for chunk in chunks if chunk["source"] == "image.png")["text"], "图中提取的原文")
        blocks = self.client.complete.call_args.args[1]
        self.assertTrue(blocks[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        (self.project / "sources/vector.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
        with self.assertRaisesRegex(RuntimeError, "SVG"):
            self.collect({"source_files": ["vector.svg"]})
