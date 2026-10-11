#!/usr/bin/env python3
"""PPT Master - Web GUI Supplied Image Tests

Exercise supplied bitmap preservation, converted Markdown references and confinement.

Usage:
    python3 -m unittest tests.test_web_gui_assets

Examples:
    python3 -m unittest tests.test_web_gui_assets

Dependencies:
    Pillow
"""

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from web_gui.assets import prepare_images


class SuppliedImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        for relative in ("sources", ".web_gui", "analysis/source_markdown"):
            (self.project / relative).mkdir(parents=True, exist_ok=True)
        self.client = Mock()
        self.progress = Mock()
        self.cancel = threading.Event()
        self.brief([])

    def brief(self, files: list[str]) -> None:
        (self.project / ".web_gui/brief.json").write_text(
            json.dumps({"source_files": files}), encoding="utf-8",
        )

    def image(self, relative: str, color: str = "red", size: tuple[int, int] = (40, 25)) -> Path:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, color).save(path)
        return path

    def prepare(self) -> list[dict]:
        return prepare_images(self.project, self.client, self.progress, self.cancel)

    def test_original_bitmap_is_preserved_with_dimensions_and_ocr_description(self) -> None:
        self.brief(["figure.png"])
        original = self.image("sources/figure.png")
        raw = original.read_bytes()
        markdown = self.project / "analysis/source_markdown/001_figure.md"
        markdown.write_text("# Figure\n\nDetected chart labels and source context", encoding="utf-8")
        (self.project / "analysis/source_manifest.json").write_text(json.dumps({"documents": [{
            "source": "figure.png", "markdown": markdown.relative_to(self.project).as_posix(),
        }]}), encoding="utf-8")
        images = self.prepare()
        self.assertEqual(len(images), 1)
        asset = images[0]
        self.assertEqual((asset["width"], asset["height"]), (40, 25))
        self.assertTrue(asset["file"].startswith("source_" + hashlib.sha256(raw).hexdigest()[:20]))
        self.assertEqual(asset["href"], "../images/" + asset["file"])
        self.assertEqual((self.project / "images" / asset["file"]).read_bytes(), raw)
        self.assertEqual(original.read_bytes(), raw)
        self.assertIn("Detected chart labels", asset["description"])
        self.client.complete.assert_not_called()
        self.assertEqual(json.loads((self.project / "analysis/prepared_images.json").read_text(
            encoding="utf-8"))["images"], images)

    def test_converted_markdown_and_html_use_only_exact_local_references(self) -> None:
        self.image("analysis/source_markdown/report_files/figure one.png")
        self.image("analysis/source_markdown/report_files/figure_two.jpg", color="blue")
        self.image("analysis/source_markdown/report_files/unreferenced.png", color="green")
        (self.project / "analysis/source_markdown/report.md").write_text(
            '![Revenue growth](report_files/figure%20one.png)\n'
            '<img src="report_files/figure_two.jpg" alt="Product architecture">', encoding="utf-8",
        )
        images = self.prepare()
        self.assertEqual(len(images), 2)
        self.assertEqual({item["description"] for item in images}, {"Revenue growth", "Product architecture"})

    def test_external_and_traversal_references_are_rejected_without_image_reads(self) -> None:
        (self.project / "analysis/source_markdown/report.md").write_text(
            '![External](https://example.com/figure.png)\n'
            '![Traversal](../../../private.png)\n'
            '![Encoded traversal](%2e%2e/%2e%2e/private.png)\n'
            '<img src="file:///C:/private.png" alt="Private">', encoding="utf-8",
        )
        with patch("web_gui.assets.Image.open") as image_open, patch.object(Path, "read_bytes") as read_bytes:
            self.assertEqual(self.prepare(), [])
            image_open.assert_not_called()
            read_bytes.assert_not_called()
        self.assertGreaterEqual(self.progress.call_count, 4)
        self.client.complete.assert_not_called()

    def test_duplicate_content_deduplicates_and_existing_copies_are_verified(self) -> None:
        self.brief(["one.png", "two.png"])
        original = self.image("sources/one.png", size=(1, 1))
        (self.project / "sources/two.png").write_bytes(original.read_bytes())
        first = self.prepare()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["width"], 1)
        self.assertEqual(self.prepare(), first)
        prepared = self.project / "images" / first[0]["file"]
        prepared.write_bytes(b"unrelated existing contents")
        with self.assertRaisesRegex(RuntimeError, "冲突|collision"):
            self.prepare()
        self.assertEqual(prepared.read_bytes(), b"unrelated existing contents")

    def test_reference_style_images_and_cancel(self) -> None:
        self.image("analysis/source_markdown/assets/image.png")
        (self.project / "analysis/source_markdown/report.md").write_text(
            '![Architecture][figure]\n\n[figure]: <assets/image.png> "Figure caption"', encoding="utf-8",
        )
        self.assertEqual(len(self.prepare()), 1)
        self.cancel.set()
        with self.assertRaises(RuntimeError):
            self.prepare()

    def test_invalid_original_is_reported_instead_of_silently_omitted(self) -> None:
        self.brief(["broken.png"])
        original = self.project / "sources/broken.png"
        original.write_bytes(b"not a bitmap")
        with self.assertRaisesRegex(RuntimeError, "broken.png"):
            self.prepare()
        self.assertEqual(original.read_bytes(), b"not a bitmap")

    def test_symlinks_and_unlisted_originals_are_not_read(self) -> None:
        original = self.image("sources/unlisted.png")
        self.assertEqual(self.prepare(), [])
        target = self.project / "analysis/source_markdown/linked.png"
        try:
            target.symlink_to(original)
        except OSError:
            self.skipTest("Host does not permit symlinks")
        (self.project / "analysis/source_markdown/report.md").write_text(
            "![Linked](linked.png)", encoding="utf-8",
        )
        with patch("web_gui.assets.Image.open") as image_open:
            self.assertEqual(self.prepare(), [])
            image_open.assert_not_called()
