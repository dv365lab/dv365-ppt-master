#!/usr/bin/env python3
"""PPT Master - Web GUI API Tests

Exercise local request guards, upload validation and project file confinement.

Usage:
    python3 -m unittest tests.test_web_gui_server

Dependencies:
    flask
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from web_gui.server import create_app  # noqa: E402


class FakeRunner:
    busy = False

    def __init__(self):
        self.jobs = {}

    def status(self):
        return {"available": True, "authenticated": True, "detail": "Ready"}

    def snapshot(self, project):
        return self.jobs.get(str(project))

    def start(self, project, brief):
        if self.busy:
            raise RuntimeError("Another generation is running")
        self.busy = True
        job = {"state": "running", "log": "", "error": "", "exit_code": None}
        self.jobs[str(project)] = job
        return job

    def cancel(self, project):
        self.busy = False
        self.jobs[str(project)]["state"] = "cancelled"
        return self.jobs[str(project)]


class WebGuiServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "projects"
        self.runner = FakeRunner()
        self.app = create_app(self.root, runner=self.runner)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.token = self.client.get("/api/status").json["token"]
        self.headers = {"X-Web-GUI-Token": self.token}

    def create_project(self, **overrides):
        data = {"topic": "数字 IC 验证入门", "slides": "3", "format": "ppt169", "style": "简洁"}
        data.update(overrides)
        return self.client.post("/api/projects", data=data, headers=self.headers)

    def test_create_upload_generate_and_cancel(self):
        response = self.create_project(files=(io.BytesIO("原始资料".encode()), "资料.txt"))
        self.assertEqual(response.status_code, 201)
        project = response.json
        path = self.root / project["id"]
        self.assertEqual((path / "sources" / "资料.txt").read_text(encoding="utf-8"), "原始资料")
        self.assertTrue((path / "svg_output").is_dir())
        self.assertFalse((path / "spec_lock.md").exists())
        self.assertEqual(len(self.client.get("/api/projects").json["projects"]), 1)
        result = self.client.post(f'/api/projects/{project["id"]}/generate', headers=self.headers)
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.json["state"], "running")
        duplicate = self.client.post(f'/api/projects/{project["id"]}/generate', headers=self.headers)
        self.assertEqual(duplicate.status_code, 409)
        result = self.client.post(f'/api/projects/{project["id"]}/cancel', headers=self.headers)
        self.assertEqual(result.json["state"], "cancelled")

    def test_invalid_inputs_do_not_create_projects(self):
        for changes in ({"topic": ""}, {"slides": "0"}, {"slides": "41"},
                        {"slides": "2.5"}, {"format": "unknown"},
                        {"files": (io.BytesIO(b"x"), "../escape.txt")},
                        {"files": (io.BytesIO(b"x"), "run.bat")},
                        {"files": (io.BytesIO(b"x"), "CON.txt")}):
            with self.subTest(changes=changes):
                self.assertEqual(self.create_project(**changes).status_code, 400)
        self.assertEqual(self.client.get("/api/projects").json["projects"], [])

    def test_page_planning_defaults_and_legacy_fixed_mode(self):
        response = self.client.post("/api/projects", data={"topic": "演示主题"}, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        brief = response.json["brief"]
        self.assertEqual(brief["page_count_mode"], "auto")
        self.assertIsNone(brief["slides"])
        self.assertEqual((brief["min_pages"], brief["max_pages"]), (5, 30))
        self.assertEqual(brief["analysis_depth"], "normal")
        self.assertTrue(brief["notes"])
        fixed = self.create_project(slides="7").json["brief"]
        self.assertEqual(fixed["page_count_mode"], "fixed")
        self.assertEqual((fixed["slides"], fixed["min_pages"], fixed["max_pages"]), (7, 7, 7))
        ranged = self.create_project(page_count_mode="range", min_pages="8", max_pages="16",
                                    analysis_depth="deep").json["brief"]
        self.assertIsNone(ranged["slides"])
        self.assertEqual((ranged["min_pages"], ranged["max_pages"]), (8, 16))
        self.assertEqual(ranged["analysis_depth"], "deep")

    def test_invalid_planning_settings_create_no_project(self):
        cases = [
            {"page_count_mode": "unknown"}, {"analysis_depth": "unknown"},
            {"page_count_mode": "fixed", "slides": ""},
            {"page_count_mode": "range", "min_pages": "0", "max_pages": "4"},
            {"page_count_mode": "range", "min_pages": "5", "max_pages": "41"},
            {"page_count_mode": "range", "min_pages": "9", "max_pages": "8"},
            {"page_count_mode": "range", "min_pages": "1.5", "max_pages": "8"},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(self.create_project(**changes).status_code, 400)
        self.assertEqual(self.client.get("/api/projects").json["projects"], [])

    def test_attachments_only_records_original_source_whitelist(self):
        response = self.create_project(topic="", files=(io.BytesIO(b"material"), "source.txt"))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["brief"]["topic"], "根据附件制作演示文稿")
        self.assertEqual(response.json["brief"]["source_files"], ["source.txt"])

    def test_outline_and_notes_are_exposed_and_downloadable(self):
        project = self.create_project().json
        path = self.root / project["id"]
        (path / "analysis").mkdir()
        outline = {"total_pages": 1, "page_count_reason": "一个核心结论", "slides": [
            {"title": "主题", "points": ["要点"], "notes": "演讲备注"},
        ]}
        (path / "analysis" / "outline.json").write_text(json.dumps(outline), encoding="utf-8")
        (path / "notes").mkdir()
        (path / "notes" / "01.md").write_text("# 主题\n演讲备注", encoding="utf-8")
        (path / "notes" / "private.txt").write_text("not exposed", encoding="utf-8")
        detail = self.client.get(f'/api/projects/{project["id"]}').json
        self.assertEqual(detail["outline"], outline)
        self.assertEqual(detail["planned_pages"], 1)
        self.assertEqual([item["name"] for item in detail["notes"]], ["01.md"])
        self.assertIn("演讲备注", detail["notes"][0]["content"])
        for url in (detail["outline_url"], detail["notes"][0]["url"]):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment", response.headers["Content-Disposition"])
            response.close()
        for name in ("private.txt", "../analysis/outline.json", "..%2f.web_gui%2fbrief.json"):
            response = self.client.get(f'/api/projects/{project["id"]}/notes/{name}')
            self.assertIn(response.status_code, (400, 404))
        (path / "analysis" / "outline.json").write_text("{unfinished", encoding="utf-8")
        self.assertIsNone(self.client.get(f'/api/projects/{project["id"]}').json["outline"])

    def test_note_symlinks_cannot_escape_notes_directory(self):
        project = self.create_project().json
        path = self.root / project["id"]
        outside = path / "sources" / "secret.md"
        outside.write_text("private", encoding="utf-8")
        (path / "notes").mkdir()
        link = path / "notes" / "leak.md"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("Host does not permit symlinks")
        detail = self.client.get(f'/api/projects/{project["id"]}').json
        self.assertEqual(detail["notes"], [])
        response = self.client.get(f'/api/projects/{project["id"]}/notes/leak.md')
        self.assertIn(response.status_code, (400, 404))

    def test_duplicate_upload_names_are_rejected_without_overwrite(self):
        response = self.create_project(files=[(io.BytesIO(b"a"), "notes.txt"),
                                              (io.BytesIO(b"b"), "NOTES.txt")])
        self.assertEqual(response.status_code, 400)

    def test_local_request_and_csrf_guards(self):
        self.assertEqual(self.client.get("/api/status", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/status", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/projects", data={"topic": "x"}).status_code, 403)
        headers = dict(self.headers, Origin="http://localhost:9999")
        self.assertEqual(self.client.post("/api/projects", headers=headers).status_code, 403)

    def test_preview_is_sandboxed_and_files_are_confined(self):
        project = self.create_project().json
        path = self.root / project["id"]
        (path / "svg_output" / "02.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        (path / "svg_output" / "10.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        (path / "exports").mkdir()
        (path / "exports" / "deck.pptx").write_bytes(b"fixture")
        (path / ".env").write_text("secret")
        detail = self.client.get(f'/api/projects/{project["id"]}').json
        self.assertEqual([s["name"] for s in detail["slides"]], ["02.svg", "10.svg"])
        preview = self.client.get(detail["slides"][0]["url"])
        self.assertEqual(preview.status_code, 200)
        self.assertIn("sandbox", preview.headers["Content-Security-Policy"])
        preview.close()
        (path / "svg_output" / "02.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"><rect width="20" height="20"/></svg>'
        )
        updated = self.client.get(f'/api/projects/{project["id"]}').json
        self.assertNotEqual(detail["slides"][0]["url"], updated["slides"][0]["url"])
        download = self.client.get(detail["exports"][0]["url"])
        self.assertIn("attachment", download.headers["Content-Disposition"])
        download.close()
        for name in (".env", ".web_gui/brief.json", "svg_output/../../README.md"):
            response = self.client.get(f'/api/projects/{project["id"]}/files/{name}')
            self.assertIn(response.status_code, (400, 404))

    def test_existing_projects_are_browse_only_without_saved_brief(self):
        existing = self.root / "existing"
        (existing / "svg_output").mkdir(parents=True)
        self.assertEqual(len(self.client.get("/api/projects").json["projects"]), 1)
        response = self.client.post("/api/projects/existing/generate", headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_preview_expands_icons_and_resizes_without_editing_source(self):
        project = self.create_project().json
        path = self.root / project["id"]
        icon_dir = path / "icons" / "custom"
        icon_dir.mkdir(parents=True)
        (icon_dir / "sample.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
            '<path d="M 2 2 L 22 2 L 12 22 Z" fill="currentColor"/></svg>', encoding="utf-8",
        )
        source = (
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            'width="1280" height="720" viewBox="0 0 1280 720">'
            '<use data-icon="custom/sample" x="20" y="20" width="48" height="48" fill="#123456"/>'
            '<rect id="box" style="x:100px;y:100px;width:30px;height:40px"/>'
            '<image xlink:href="../images/sample.png" width="20" height="20"/>'
            '</svg>'
        ).encode("utf-8")
        file = path / "svg_output" / "01.svg"
        file.write_bytes(source)
        response = self.client.get(f'/api/projects/{project["id"]}/files/svg_output/01.svg')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/svg+xml")
        root = ElementTree.fromstring(response.data)
        ns = {"svg": "http://www.w3.org/2000/svg"}
        self.assertIsNotNone(root.find(".//svg:path", ns))
        self.assertIsNone(root.find(".//svg:use", ns))
        self.assertEqual(root.get("width"), "100%")
        self.assertEqual(root.get("height"), "100%")
        self.assertEqual(root.find("svg:rect", ns).get("width"), "30")
        self.assertEqual(root.find("svg:image", ns).get("href"), "../images/sample.png")
        self.assertEqual(file.read_bytes(), source)
        response.close()

    def test_missing_preview_icon_has_visible_warning(self):
        project = self.create_project().json
        file = self.root / project["id"] / "svg_output" / "01.svg"
        file.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '<use data-icon="custom/missing" width="24" height="24"/></svg>', encoding="utf-8",
        )
        response = self.client.get(f'/api/projects/{project["id"]}/files/svg_output/01.svg')
        self.assertEqual(response.status_code, 200)
        self.assertIn("custom/missing", response.headers.get("X-Preview-Warnings", ""))
        root = ElementTree.fromstring(response.data)
        self.assertIn("预览提示", "".join(root.itertext()))
        response.close()

    def test_missing_projects_and_large_requests_return_json(self):
        self.assertEqual(self.client.get("/api/projects/missing").status_code, 404)
        self.app.config["MAX_CONTENT_LENGTH"] = 20
        response = self.create_project()
        self.assertEqual(response.status_code, 413)
        self.assertIn("error", response.json)
