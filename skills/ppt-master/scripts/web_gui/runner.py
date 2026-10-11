#!/usr/bin/env python3
"""
PPT Master - Web GUI Generation Runner

Run the local model pipeline in an owned subprocess and retain bounded browser
progress with durable job metadata and separate process logs.

Usage:
    Import GenerationRunner from the web GUI server.

Examples:
    runner = GenerationRunner(skill_dir)
    runner.start(project, brief)

Dependencies:
    Project requirements; a private model provider configuration
"""

from __future__ import annotations

import json
import os
import posixpath
import signal
import subprocess
import sys
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from web_gui.provider import ProviderError, load_config

_LOG_LIMIT = 24000
_TAIL_BYTES = 65536
_EVENT_BYTES = 1024 * 1024
_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_RELATIONSHIP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_STAGE_LABELS = {
    "starting": "准备生成", "converting": "读取资料", "sources": "读取资料",
    "analyzing": "分析资料", "analyzing_sources": "分析资料", "planning": "规划大纲",
    "refining": "完善大纲", "validating_outline": "校验大纲", "outline_ready": "大纲已完成",
    "analyzing_structure": "分析结构", "initial_outline": "生成大纲", "refining_outline": "细化大纲",
    "outline_repair": "校正大纲",
    "preparing_images": "准备资料图片",
    "rendering_slides": "制作页面", "checking_slides": "检查页面", "repairing_slides": "修复页面",
    "writing_notes": "撰写讲稿", "exporting": "导出 PPTX", "retrying": "重试模型请求",
    "completed": "生成完成",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project_path(project: Path, relative: str) -> Path:
    path = (project / relative).resolve()
    if path == project or not path.is_relative_to(project):
        raise ValueError("Web GUI 文件路径必须位于当前项目内，请移除指向项目外的符号链接。")
    return path


def _creation_options() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _terminate_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
            **_creation_options(),
        )
        if result.returncode and process.poll() is None:
            raise RuntimeError("无法停止生成进程，请在任务管理器中关闭该生成任务。")
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def _write_job(project: Path, job: dict) -> None:
    destination = _project_path(project, ".web_gui/job.json")
    temporary = _project_path(project, ".web_gui/job.tmp")
    temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def _tail(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_BYTES))
            contents = handle.read(_TAIL_BYTES).decode("utf-8", errors="replace")
        return contents.split("\n", 1)[-1] if size > _TAIL_BYTES else contents
    except FileNotFoundError:
        return ""


def _event_error(event: dict) -> str:
    if event.get("type") != "error":
        return ""
    return str(event.get("message") or "生成管线报告错误，请查看进度日志。")[:3000]


def _browser_log(project: Path) -> str:
    lines = []
    for line in _tail(_project_path(project, ".web_gui/stdout.jsonl")).splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        error = _event_error(event)
        if error:
            lines.append(error)
        if event.get("type") == "progress":
            lines.append(str(event.get("message", ""))[:6000])
        elif event.get("type") == "completed":
            lines.append(f"已导出 {event.get('pages', '')} 页 PPTX 与对应讲稿。")
    stderr = _tail(_project_path(project, ".web_gui/stderr.log")).strip()
    if stderr:
        lines.append(stderr[-6000:])
    return "\n\n".join(lines)[-_LOG_LIMIT:]


def _exports(project: Path) -> dict[str, tuple[int, int]]:
    result = {}
    for candidate in _project_path(project, "exports").glob("*.pptx"):
        path = _project_path(project, "exports/" + candidate.name)
        if not path.is_file():
            continue
        stat = path.stat()
        result[path.name] = (stat.st_size, stat.st_mtime_ns)
    return result


def _valid_pptx(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            required = {"[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml",
                        "ppt/_rels/presentation.xml.rels"}
            if not required <= names or archive.testzip() is not None:
                return False
            for name in required:
                ElementTree.fromstring(archive.read(name))
            root = ElementTree.fromstring(archive.read("ppt/presentation.xml"))
            if root.tag != f"{{{_PRESENTATION_NS}}}presentation":
                return False
            slides = root.findall(f"{{{_PRESENTATION_NS}}}sldIdLst/{{{_PRESENTATION_NS}}}sldId")
            relationships = ElementTree.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
            targets = {item.get("Id"): item.get("Target", "") for item in relationships}
            if not slides:
                return False
            for slide in slides:
                target = targets.get(slide.get(f"{{{_RELATIONSHIP_NS}}}id"), "")
                name = posixpath.normpath(posixpath.join("ppt", target))
                if name not in names or not name.startswith("ppt/slides/"):
                    return False
                if ElementTree.fromstring(archive.read(name)).tag != f"{{{_PRESENTATION_NS}}}sld":
                    return False
        return True
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return False


class GenerationRunner:
    """Own one asynchronous API generation and persist its observable state."""

    def __init__(self, skill_dir: Path) -> None:
        self.skill_dir = skill_dir.resolve()
        self._lock = threading.RLock()
        self._project = None
        self._process = None
        self._thread = None
        self._job = None
        self._cancelled = threading.Event()
        self._closed = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._project is not None

    def status(self) -> dict:
        """Check local configuration without exposing credentials or calling the API."""
        try:
            config = load_config()
        except ProviderError as error:
            return {"available": True, "authenticated": False,
                    "detail": str(error), "provider": "agnes"}
        return {"available": True, "authenticated": True, "provider": "agnes", "model": config.model,
                "detail": "已配置 Agnes，生成时连接模型服务。"}

    def start(self, project: Path, brief: dict) -> dict:
        """Reserve the generation slot and launch the configured model pipeline."""
        project = project.resolve()
        if not project.is_dir():
            raise ValueError("Initialize the project before starting generation.")
        with self._lock:
            if self._closed:
                raise RuntimeError("Generation runner is closed. Restart the web GUI.")
            if self._project is not None:
                raise RuntimeError("已有生成任务正在运行，请等待完成或取消后重试。")
            status = self.status()
            if not status["authenticated"]:
                raise RuntimeError(status["detail"])
            _project_path(project, ".web_gui").mkdir(exist_ok=True)
            for name in ("brief.json", "job.json", "job.tmp", "stdout.jsonl", "stderr.log"):
                _project_path(project, ".web_gui/" + name)
            baseline = _exports(project)
            job = {"state": "running", "started_at": _now(), "updated_at": _now(),
                   "error": "", "exit_code": None, "stage": "starting", "stage_label": "准备生成",
                   "message": "正在启动 Agnes 生成管线。", "current": 0, "total": 0, "planned_pages": 0}
            _write_job(project, job)
            self._project = project
            self._job = job
            self._cancelled.clear()
            self._thread = threading.Thread(
                target=self._run, args=(project, baseline),
                name="ppt-master-generation", daemon=True,
            )
            self._thread.start()
            return {**job, "log": ""}

    def snapshot(self, project: Path) -> dict | None:
        """Return bounded progress and mark jobs abandoned by a prior server as interrupted."""
        project = project.resolve()
        with self._lock:
            if self._project == project:
                job = self._job.copy()
            else:
                path = _project_path(project, ".web_gui/job.json")
                if not path.is_file():
                    return None
                try:
                    job = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    return {"state": "failed", "started_at": "", "updated_at": "",
                            "error": f"Cannot read job metadata: {error}", "exit_code": None, "log": ""}
                if not isinstance(job, dict):
                    return None
                if job.get("state") == "running":
                    job.update(state="interrupted", updated_at=_now(),
                               error="上次服务退出时任务尚未完成，请创建新任务重新生成。")
                    _write_job(project, job)
            return {**job, "log": _browser_log(project)}

    def cancel(self, project: Path) -> dict:
        """Cancel only the active project's process tree, including a concurrent spawn."""
        project = project.resolve()
        with self._lock:
            if self._project != project:
                result = self.snapshot(project)
                if result is None:
                    raise RuntimeError("This project has no generation job to cancel.")
                return result
            self._cancelled.set()
            self._job.update(state="cancelled", updated_at=_now(), error="生成已取消。")
            process = self._process
            try:
                _write_job(project, self._job)
            finally:
                if process is not None:
                    _terminate_tree(process)
        return self.snapshot(project)

    def close(self) -> None:
        """Stop an owned process before the local web server exits."""
        with self._lock:
            self._closed = True
            project, thread = self._project, self._thread
        if project is not None:
            self.cancel(project)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=20)

    def _apply_progress(self, project: Path, event: dict) -> None:
        stage = str(event.get("stage", "starting"))[:80]
        current, total = event.get("current", 0), event.get("total", 0)
        current = max(0, current) if isinstance(current, int) else 0
        total = max(0, total) if isinstance(total, int) else 0
        self._job.update(stage=stage, stage_label=_STAGE_LABELS.get(stage, "处理中"),
                         message=str(event.get("message", ""))[:6000], current=current, total=total)
        if stage == "outline_ready":
            self._job["planned_pages"] = current
        _write_job(project, self._job)

    def _run(self, project: Path, baseline: dict) -> None:
        error, exit_code, process = "", None, None
        completed = False
        completion_event = None
        try:
            if self._cancelled.is_set():
                return
            arguments = [sys.executable, "-u", str(self.skill_dir / "scripts/web_gui/pipeline.py"),
                         "--project", str(project)]
            stderr_path = _project_path(project, ".web_gui/stderr.log")
            stdout_path = _project_path(project, ".web_gui/stdout.jsonl")
            with stderr_path.open("wb") as stderr, stdout_path.open("wb") as stdout:
                process = subprocess.Popen(
                    arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr,
                    cwd=str(project), **_creation_options(),
                )
                with self._lock:
                    self._process = process
                    cancelled_during_spawn = self._cancelled.is_set()
                if cancelled_during_spawn:
                    _terminate_tree(process)
                while True:
                    line = process.stdout.readline(_EVENT_BYTES)
                    if not line:
                        break
                    stdout.write(line)
                    stdout.flush()
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("expected an event object")
                        error = error or _event_error(event)
                        if event.get("type") == "completed":
                            completion_event = event
                        with self._lock:
                            self._job["updated_at"] = _now()
                            if event.get("type") == "progress":
                                self._apply_progress(project, event)
                    except (json.JSONDecodeError, ValueError):
                        error = error or "生成管线的 JSON 进度事件无法解析，请查看日志后重试。"
                process.stdout.close()
                exit_code = process.wait()
            if exit_code and not error:
                error = f"生成进程退出，状态码 {exit_code}。请查看进度日志后重试。"
            if not error and completion_event is None:
                error = "生成进程未报告完成，不能确认 PPTX 已完整交付。请查看日志后重试。"
            if not error and not self._cancelled.is_set():
                fresh = [_project_path(project, "exports/" + name) for name, stamp in _exports(project).items()
                         if baseline.get(name) != stamp]
                if not any(_valid_pptx(path) for path in fresh):
                    error = "生成进程未交付新的有效 PPTX。请查看生成日志，修复原因后创建新任务。"
                else:
                    completed = True
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
            error = "生成进程无法完成，请检查 Python、项目依赖与模型配置后重试。"
            if process is not None and process.poll() is None:
                try:
                    _terminate_tree(process)
                    exit_code = process.wait(timeout=10)
                except (OSError, subprocess.SubprocessError, RuntimeError):
                    error += " 自动停止进程失败，请在任务管理器中关闭该生成任务。"
        finally:
            with self._lock:
                state = "cancelled" if self._cancelled.is_set() else "completed" if completed else "failed"
                if state == "failed" and not error:
                    error = "Generation stopped unexpectedly. Check the server log and create a new task."
                self._job.update(state=state, updated_at=_now(), exit_code=exit_code,
                                 error="生成已取消。" if state == "cancelled" else error)
                if state == "completed":
                    pages = completion_event.get("pages", self._job.get("planned_pages", 0))
                    pages = pages if isinstance(pages, int) and pages > 0 else self._job.get("planned_pages", 0)
                    self._job.update(stage="completed", stage_label="生成完成", current=pages, total=pages,
                                     planned_pages=pages, message=f"已完成 {pages} 页 PPTX 与对应讲稿。")
                try:
                    _write_job(project, self._job)
                finally:
                    self._project = None
                    self._process = None
