#!/usr/bin/env python3
"""
PPT Master - Web GUI Runner Regression Tests

Exercise CLI isolation, artifact validation, and cancellation without live AI calls.

Usage:
    python -m unittest tests.test_web_gui_runner

Examples:
    python -m unittest tests.test_web_gui_runner

Dependencies:
    None (only uses standard library)
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from web_gui.runner import GenerationRunner
from web_gui.provider import ProviderConfig, ProviderError


def make_pptx(project: Path) -> None:
    exports = project / "exports"
    exports.mkdir(exist_ok=True)
    with zipfile.ZipFile(exports / "deck.pptx", "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="ppt/presentation.xml"/></Relationships>',
        )
        archive.writestr(
            "ppt/presentation.xml",
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>',
        )
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
            'Target="slides/slide1.xml"/></Relationships>',
        )
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>',
        )


class FakeProcess:
    def __init__(self, events: str, project: Path, *, artifact=False, code=0):
        self.stdout = io.BytesIO(events.encode("utf-8"))
        self.pid = 123456
        self.returncode = None
        self.project = project
        self.artifact = artifact
        self.code = code

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.artifact:
            make_pptx(self.project)
        self.returncode = self.code
        return self.code


class GenerationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.skill = Path(self.temp.name) / "skill"
        self.runner = GenerationRunner(self.skill)
        self.brief = {"topic": "中文 topic & $(literal)", "slides": 3, "format": "ppt169", "style": "清爽"}
        self.settings = patch("web_gui.runner.load_config", return_value=ProviderConfig("private-test-key"))
        self.settings.start()

    def tearDown(self) -> None:
        self.runner.close()
        self.settings.stop()
        self.temp.cleanup()

    def completed(self) -> dict:
        deadline = time.monotonic() + 3
        while self.runner.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.runner.busy)
        return self.runner.snapshot(self.project)

    def test_success_uses_fixed_pipeline_command_without_credentials(self) -> None:
        events = '{"type":"completed","pages":1,"file":"deck.pptx","notes":1}\n'
        process = FakeProcess(events, self.project, artifact=True)
        with patch("web_gui.runner.subprocess.Popen", return_value=process) as popen:
            self.runner.start(self.project, self.brief)
            result = self.completed()
        args = popen.call_args.args[0]
        self.assertEqual(args, [sys.executable, "-u", str(self.skill / "scripts/web_gui/pipeline.py"),
                                "--project", str(self.project.resolve())])
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs["cwd"], str(self.project.resolve()))
        self.assertNotIn("private-test-key", str(args))
        self.assertFalse(popen.call_args.kwargs.get("shell", False))
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue((self.project / ".web_gui" / "stdout.jsonl").exists())
        self.assertTrue((self.project / ".web_gui" / "stderr.log").exists())

    def test_exit_zero_without_pptx_fails(self) -> None:
        process = FakeProcess('{"type":"completed","file":"deck.pptx"}\n', self.project)
        with patch("web_gui.runner.subprocess.Popen", return_value=process):
            self.runner.start(self.project, self.brief)
            result = self.completed()
        self.assertEqual(result["state"], "failed")
        self.assertIn("PPTX", result["error"])

    def test_existing_pptx_is_not_a_successful_new_generation(self) -> None:
        make_pptx(self.project)
        process = FakeProcess('{"type":"completed","file":"deck.pptx"}\n', self.project)
        with patch("web_gui.runner.subprocess.Popen", return_value=process):
            self.runner.start(self.project, self.brief)
            result = self.completed()
        self.assertEqual(result["state"], "failed")

    def test_pipeline_error_event_cannot_be_masked_by_artifact(self) -> None:
        events = '{"type":"error","message":"quota exceeded"}\n'
        with patch("web_gui.runner.subprocess.Popen", return_value=FakeProcess(events, self.project, artifact=True)):
            self.runner.start(self.project, self.brief)
            result = self.completed()
        self.assertEqual(result["state"], "failed")
        self.assertIn("quota exceeded", result["error"])

    def test_nonzero_exit_cannot_be_masked_by_artifact(self) -> None:
        process = FakeProcess('{"type":"completed","file":"deck.pptx"}\n', self.project, artifact=True, code=7)
        with patch("web_gui.runner.subprocess.Popen", return_value=process):
            self.runner.start(self.project, self.brief)
            result = self.completed()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["exit_code"], 7)

    def test_new_file_named_pptx_must_have_valid_presentation_structure(self) -> None:
        process = FakeProcess('{"type":"completed","file":"deck.pptx"}\n', self.project)

        def finish(timeout=None):
            (self.project / "exports").mkdir()
            with zipfile.ZipFile(self.project / "exports" / "deck.pptx", "w") as archive:
                archive.writestr("ppt/presentation.xml", "<bad/>")
            return 0

        with patch("web_gui.runner.subprocess.Popen", return_value=process), patch.object(
            process, "wait", side_effect=finish
        ):
            self.runner.start(self.project, self.brief)
            result = self.completed()
        self.assertEqual(result["state"], "failed")
        self.assertIn("PPTX", result["error"])

    def test_invalid_json_is_reported(self) -> None:
        process = FakeProcess("not JSON\n", self.project, artifact=True)
        with patch("web_gui.runner.subprocess.Popen", return_value=process):
            self.runner.start(self.project, self.brief)
            result = self.completed()
        self.assertEqual(result["state"], "failed")
        self.assertIn("JSON", result["error"])

    def test_restart_marks_persisted_running_job_interrupted(self) -> None:
        folder = self.project / ".web_gui"
        folder.mkdir()
        job = {"state": "running", "started_at": "old", "updated_at": "old", "error": "", "exit_code": None}
        (folder / "job.json").write_text(json.dumps(job), encoding="utf-8")
        result = self.runner.snapshot(self.project)
        self.assertEqual(result["state"], "interrupted")
        self.assertEqual(json.loads((folder / "job.json").read_text(encoding="utf-8"))["state"], "interrupted")

    def test_cancel_during_spawn_stops_new_process_and_keeps_single_slot(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        process = FakeProcess('{"type":"completed","file":"deck.pptx"}\n', self.project)

        def spawn(*args, **kwargs):
            entered.set()
            release.wait(3)
            return process

        with patch("web_gui.runner.subprocess.Popen", side_effect=spawn), patch(
            "web_gui.runner._terminate_tree"
        ) as terminate:
            self.runner.start(self.project, self.brief)
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.runner.cancel(self.project)["state"], "cancelled")
            with self.assertRaises(RuntimeError):
                self.runner.start(self.project, self.brief)
            release.set()
            result = self.completed()
            terminate.assert_called_once_with(process)
        self.assertEqual(result["state"], "cancelled")

    def test_browser_log_is_bounded(self) -> None:
        folder = self.project / ".web_gui"
        folder.mkdir()
        (folder / "job.json").write_text(json.dumps({"state": "failed", "error": "", "exit_code": 1}), encoding="utf-8")
        event = {"type": "progress", "stage": "rendering_slides", "message": "a" * 2000}
        (folder / "stdout.jsonl").write_text((json.dumps(event) + "\n") * 100, encoding="utf-8")
        (folder / "stderr.log").write_text("x" * 100000, encoding="utf-8")
        self.assertLessEqual(len(self.runner.snapshot(self.project)["log"]), 24000)

    def test_spawn_error_releases_slot_and_records_failure(self) -> None:
        with patch("web_gui.runner.subprocess.Popen", side_effect=OSError("executable unavailable")):
            self.runner.start(self.project, self.brief)
            result = self.completed()
        self.assertEqual(result["state"], "failed")
        self.assertIn("生成进程", result["error"])

    def test_status_reads_only_configuration_without_network_or_subprocess(self) -> None:
        with patch("web_gui.runner.subprocess.run") as run:
            result = self.runner.status()
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["provider"], "agnes")
        self.assertEqual(result["model"], "agnes-2.5-flash")
        self.assertNotIn("private-test-key", json.dumps(result))
        run.assert_not_called()

    def test_missing_configuration_is_actionable(self) -> None:
        with patch("web_gui.runner.load_config", side_effect=ProviderError("请配置 api_key。")):
            result = self.runner.status()
            with self.assertRaisesRegex(RuntimeError, "配置"):
                self.runner.start(self.project, self.brief)
        self.assertFalse(result["authenticated"])
        self.assertFalse(self.runner.busy)

    def test_fresh_artifact_without_completed_event_fails(self) -> None:
        process = FakeProcess('{"type":"progress","stage":"exporting","message":"导出中"}\n',
                              self.project, artifact=True)
        with patch("web_gui.runner.subprocess.Popen", return_value=process):
            self.runner.start(self.project, self.brief)
            result = self.completed()
        self.assertEqual(result["state"], "failed")
        self.assertIn("完成", result["error"])

    def test_progress_keeps_stage_and_planned_page_count(self) -> None:
        events = '\n'.join([
            json.dumps({"type": "progress", "stage": "outline_ready", "message": "已规划", "current": 8, "total": 8}),
            json.dumps({"type": "progress", "stage": "writing_notes", "message": "讲稿", "current": 3, "total": 8}),
            json.dumps({"type": "error", "message": "模型暂不可用"}),
        ])
        with patch("web_gui.runner.subprocess.Popen", return_value=FakeProcess(events, self.project, code=1)):
            self.runner.start(self.project, self.brief)
            result = self.completed()
        self.assertEqual(result["planned_pages"], 8)
        self.assertEqual(result["stage"], "writing_notes")
        self.assertEqual(result["current"], 3)
        self.assertEqual(result["total"], 8)
        self.assertEqual(result["message"], "讲稿")

    def test_external_job_symlink_cannot_be_read_or_written(self) -> None:
        folder = self.project / ".web_gui"
        folder.mkdir()
        outside = Path(self.temp.name) / "private.json"
        original = '{"state":"running","secret":"keep-private"}'
        outside.write_text(original, encoding="utf-8")
        try:
            (folder / "job.json").symlink_to(outside)
        except OSError as error:
            self.skipTest(f"Symlink creation is unavailable: {error}")
        with self.assertRaises(ValueError):
            self.runner.snapshot(self.project)
        with self.assertRaises(ValueError):
            self.runner.start(self.project, self.brief)
        self.assertEqual(outside.read_text(encoding="utf-8"), original)
        self.assertFalse(self.runner.busy)

    def test_external_stdout_symlink_cannot_be_read_or_truncated(self) -> None:
        folder = self.project / ".web_gui"
        folder.mkdir()
        outside = Path(self.temp.name) / "private.log"
        outside.write_text("keep-private", encoding="utf-8")
        (folder / "job.json").write_text('{"state":"failed"}', encoding="utf-8")
        try:
            (folder / "stdout.jsonl").symlink_to(outside)
        except OSError as error:
            self.skipTest(f"Symlink creation is unavailable: {error}")
        with self.assertRaises(ValueError):
            self.runner.snapshot(self.project)
        with self.assertRaises(ValueError):
            self.runner.start(self.project, self.brief)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep-private")

    def test_external_temporary_job_symlink_cannot_be_overwritten(self) -> None:
        folder = self.project / ".web_gui"
        folder.mkdir()
        outside = Path(self.temp.name) / "private.tmp"
        outside.write_text("keep-private", encoding="utf-8")
        try:
            (folder / "job.tmp").symlink_to(outside)
        except OSError as error:
            self.skipTest(f"Symlink creation is unavailable: {error}")
        with self.assertRaises(ValueError):
            self.runner.start(self.project, self.brief)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep-private")
        self.assertFalse(self.runner.busy)

    def test_external_export_directory_is_rejected_before_launch(self) -> None:
        outside = Path(self.temp.name) / "private-exports"
        outside.mkdir()
        try:
            (self.project / "exports").symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Symlink creation is unavailable: {error}")
        with self.assertRaises(ValueError):
            self.runner.start(self.project, self.brief)
        self.assertFalse(self.runner.busy)

    @unittest.skipUnless(os.name == "nt", "Windows process-tree cancellation regression")
    def test_cancel_stops_real_python_process_and_descendant(self) -> None:
        import ctypes

        pid_file = self.project / "descendant.pid"
        script = (
            "import json,pathlib,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']); "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); "
            "print(json.dumps({'type':'progress','stage':'sources','message':'ready'}),flush=True); "
            "time.sleep(120)"
        )
        pipeline = self.skill / "scripts/web_gui/pipeline.py"
        pipeline.parent.mkdir(parents=True)
        pipeline.write_text(script, encoding="utf-8")
        self.runner.start(self.project, self.brief)
        deadline = time.monotonic() + 6
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(pid_file.exists(), self.runner.snapshot(self.project))
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        self.runner.cancel(self.project)
        result = self.completed()
        self.assertEqual(result["state"], "cancelled")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        handle = kernel.OpenProcess(0x1000, False, child_pid)
        if handle:
            try:
                code = ctypes.c_ulong()
                self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                self.assertNotEqual(code.value, 259, "Descendant process is still running")
            finally:
                kernel.CloseHandle(handle)
