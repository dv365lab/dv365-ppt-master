#!/usr/bin/env python3
"""PPT Master - Local Web GUI

Serve a local presentation workspace and run generation through the Agnes API.
See docs/zh/web-gui.md in the repository for the Windows launcher.

Usage:
    python3 scripts/web_gui/server.py [--port 8765] [--no-browser]

Examples:
    python3 scripts/web_gui/server.py --projects-dir /absolute/path/projects

Dependencies:
    flask; configured Agnes API for generation
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
SKILL_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from console_encoding import configure_utf8_stdio  # noqa: E402

configure_utf8_stdio()

from project_management.paths import PROJECTS_ROOT  # noqa: E402
from server_common import (  # noqa: E402
    claim_lock,
    find_free_port,
    lock_pid,
    open_preview_browser,
    process_alive,
    read_lock,
    release_lock,
    validate_port,
)
from slide_roster import discover_slide_svgs  # noqa: E402

UPLOAD_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".xlsm", ".csv", ".tsv",
    ".md", ".markdown", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
}
IMAGE_EXTENSIONS = {".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DEVICE_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def _safe_child(parent: Path, relative: str) -> Path:
    path = (parent / relative).resolve()
    if path == parent or not path.is_relative_to(parent):
        raise ValueError("文件路径必须位于当前项目内。")
    return path


def _browser_svg(file: Path, project: Path) -> tuple[str, list[dict]]:
    """Render prepared icons and responsive sizing in a source-preserving preview copy."""
    from svg_editor.server import _inline_icons, _normalize_preview_hrefs
    from svg_to_pptx.canvas_contract import CanvasContractError, parse_project_viewbox
    from svg_to_pptx.geometry_properties import materialize_inline_geometry_properties

    try:
        root = ElementTree.fromstring(file.read_bytes())
    except ElementTree.ParseError as exc:
        raise ValueError("页面 SVG 尚未写完或格式无效，请等待生成完成后重试。") from exc
    materialize_inline_geometry_properties(root)
    _normalize_preview_hrefs(root)
    try:
        canvas = parse_project_viewbox(root.get("viewBox"))
    except CanvasContractError:
        canvas = None
    if canvas is not None:
        root.set("width", "100%")
        root.set("height", "100%")
    content, warnings = _inline_icons(
        ElementTree.tostring(root, encoding="unicode"), _safe_child(project, "icons"), file.parent,
    )
    if warnings:
        root = ElementTree.fromstring(content)
        svg = "{http://www.w3.org/2000/svg}"
        width, height = canvas.pixel_dimensions if canvas else (320, 48)
        banner = ElementTree.SubElement(root, svg + "g", {"data-preview-warning": "true"})
        ElementTree.SubElement(banner, svg + "title").text = "\n".join(
            f'{item["icon"]}: {item["reason"]}' for item in warnings
        )
        ElementTree.SubElement(banner, svg + "rect", {
            "x": "0", "y": str(max(0, height - 32)), "width": str(width), "height": "32",
            "fill": "#FFF3CD", "fill-opacity": "0.96",
        })
        ElementTree.SubElement(banner, svg + "text", {
            "x": "12", "y": str(max(20, height - 11)), "font-family": "sans-serif",
            "font-size": "14", "fill": "#805B10",
        }).text = f"预览提示：{len(warnings)} 个图标未就绪，请检查项目图标文件。"
        content = ElementTree.tostring(root, encoding="unicode")
    return content, warnings


def create_app(projects_dir: Path | str, *, runner=None):
    """Create the local application; accept a runner for isolated API tests."""
    from flask import Flask, Response, abort, jsonify, request, send_file
    from werkzeug.exceptions import HTTPException

    from project_management.cli import ProjectManager
    from project_utils import find_all_projects

    if runner is None:
        from web_gui.runner import GenerationRunner
        runner = GenerationRunner(SKILL_DIR)

    root = Path(projects_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__, static_folder="static")
    app.config.update(MAX_CONTENT_LENGTH=100 * 1024 * 1024, RUNNER=runner, PROJECTS_ROOT=root)
    token = secrets.token_urlsafe(32)

    @app.before_request
    def guard():
        try:
            host = urllib.parse.urlsplit("http://" + request.host).hostname
        except ValueError:
            host = None
        if host not in {"127.0.0.1", "localhost", "::1"}:
            abort(403, description="只允许本机访问。")
        origin = request.headers.get("Origin")
        if origin is not None and origin.lower() != request.host_url.rstrip("/").lower():
            abort(403, description="拒绝跨站请求。")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.headers.get("X-Web-GUI-Token", "")
            if not secrets.compare_digest(supplied, token):
                abort(403, description="页面会话已失效，请刷新后重试。")

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; frame-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'"
            )
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc):
        message = "上传总大小不能超过 100 MB。" if exc.code == 413 else exc.description
        return jsonify(error=message), exc.code

    @app.errorhandler(ValueError)
    def invalid_input(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(OSError)
    def io_error(exc):
        app.logger.exception("Workspace operation failed")
        return jsonify(error=f"文件操作失败：{exc}。请检查目录权限和可用磁盘空间。"), 500

    def project_path(project_id):
        if not project_id or any(c in project_id for c in "/\\:") or project_id.startswith("."):
            abort(404, description="项目不存在。")
        path = _safe_child(root, project_id)
        if not path.is_dir() or path.parent != root:
            abort(404, description="项目不存在。")
        return path

    def saved_brief(path):
        filename = _safe_child(path, ".web_gui/brief.json")
        if not filename.is_file():
            return None
        try:
            data = json.loads(filename.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) and isinstance(data.get("topic"), str) else None

    def file_info(path, directory, extensions=None):
        folder = _safe_child(path, directory)
        if not folder.is_dir():
            return []
        files = []
        for item in sorted(folder.iterdir()):
            if not item.is_file() or not item.resolve().is_relative_to(path):
                continue
            if extensions and item.suffix.lower() not in extensions:
                continue
            files.append({"name": item.name, "size": item.stat().st_size})
        return files

    def read_outline(path):
        filename = _safe_child(path, "analysis/outline.json")
        if not filename.is_file() or filename.stat().st_size > 5 * 1024 * 1024:
            return None
        try:
            outline = json.loads(filename.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeError):
            return None
        if not isinstance(outline, dict) or not isinstance(outline.get("slides"), list):
            return None
        return outline if all(isinstance(slide, dict) for slide in outline["slides"]) else None

    def project_notes(path):
        folder = _safe_child(path, "notes")
        if not folder.is_dir() or (path / "notes").is_symlink():
            return []
        notes = []
        for file in sorted(folder.glob("*.md")):
            if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(folder):
                continue
            if file.stat().st_size > 2 * 1024 * 1024:
                continue
            notes.append({
                "name": file.name, "content": file.read_text(encoding="utf-8", errors="replace"),
                "size": file.stat().st_size,
                "url": "/api/projects/" + urllib.parse.quote(path.name, safe="") + "/notes/"
                + urllib.parse.quote(file.name, safe=""),
            })
        return notes

    def describe(path, *, detail=False):
        brief = saved_brief(path)
        # Check metadata confinement before allowing the runner to inspect it.
        _safe_child(path, ".web_gui")
        job = runner.snapshot(path)
        slides = []
        slide_dir = _safe_child(path, "svg_output")
        if not any(slide_dir.glob("*.svg")):
            slide_dir = _safe_child(path, "svg_final")
        for item in discover_slide_svgs(slide_dir):
            if item.is_file() and item.resolve().is_relative_to(path):
                slides.append({
                    "name": item.name,
                    "url": "/api/projects/" + urllib.parse.quote(path.name, safe="") + "/files/"
                    + urllib.parse.quote(item.relative_to(path).as_posix(), safe="/")
                    + f"?v={item.stat().st_mtime_ns}",
                })
        exports = file_info(path, "exports", {".pptx"})
        for item in exports:
            item["url"] = "/api/projects/" + urllib.parse.quote(path.name, safe="") + "/downloads/" \
                + urllib.parse.quote(item["name"], safe="")
        result = {
            "id": path.name,
            "title": re.split(r"[\r\n。！？!?]", brief["topic"], maxsplit=1)[0][:60] if brief else path.name,
            "created_at": brief.get("created_at", "") if brief else "",
            "status": job["state"] if job else ("completed" if exports else "created"),
            "slide_count": len(slides), "exports_count": len(exports),
        }
        if detail:
            outline = read_outline(path)
            planned_pages = len(outline["slides"]) if outline else None
            result.update(path=str(path), brief=brief, sources=file_info(path, "sources"),
                          slides=slides, exports=exports, job=job, outline=outline,
                          planned_pages=planned_pages, notes=project_notes(path),
                          outline_url=("/api/projects/" + urllib.parse.quote(path.name, safe="")
                                       + "/outline/download") if outline else None)
        return result

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/health")
    def health():
        return jsonify(service="ppt-master-web-gui", projects_root=str(root))

    @app.get("/api/status")
    def status():
        return jsonify(engine=runner.status(), projects_root=str(root), active=runner.busy, token=token)

    @app.get("/api/projects")
    def list_projects():
        projects = []
        for path in find_all_projects(str(root)):
            if path.resolve().parent != root or path.is_symlink():
                continue
            try:
                projects.append(describe(path.resolve()))
            except ValueError:
                continue
        projects.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
        return jsonify(projects=projects)

    @app.post("/api/projects")
    def new_project():
        topic = request.form.get("topic", "").strip()
        style = request.form.get("style", "").strip()
        uploads = [item for item in request.files.getlist("files") if item.filename]
        if len(topic) > 20000 or (not topic and not uploads):
            raise ValueError("请输入主题或上传参考资料；主题最多 20,000 个字符。")
        if not topic:
            topic = "根据附件制作演示文稿"
        if len(style) > 2000:
            raise ValueError("风格要求最多 2,000 个字符。")
        count_mode = request.form.get("page_count_mode", "fixed" if "slides" in request.form else "auto")
        if count_mode not in {"auto", "fixed", "range"}:
            raise ValueError("请选择自动规划、固定页数或页数范围。")
        count = None
        minimum, maximum = 5, 30
        if count_mode in {"fixed", "range"}:
            try:
                if count_mode == "fixed":
                    count = int(request.form.get("slides", ""))
                    minimum = maximum = count
                else:
                    minimum = int(request.form.get("min_pages", ""))
                    maximum = int(request.form.get("max_pages", ""))
            except ValueError:
                raise ValueError("页数必须是 1 至 40 的整数。") from None
            if not 1 <= minimum <= maximum <= 40:
                raise ValueError("页数须在 1 至 40 之间，且最少页数不能大于最多页数。")
        depth = request.form.get("analysis_depth", "normal")
        if depth not in {"fast", "normal", "deep"}:
            raise ValueError("请选择简明、标准或深入分析。")
        canvas = request.form.get("format", "ppt169")
        if canvas not in {"ppt169", "ppt43"}:
            raise ValueError("请选择 16:9 或 4:3 画布。")
        if len(uploads) > 20:
            raise ValueError("一次最多上传 20 个文件。")
        names = set()
        for item in uploads:
            name = item.filename
            if (len(name) > 180 or any(c in name for c in '/\\:<>"|?*')
                    or any(ord(c) < 32 for c in name) or name.startswith(".")
                    or name.rstrip(" .") != name or DEVICE_NAME.match(name)):
                raise ValueError(f"文件名无效：{name}")
            if Path(name).suffix.lower() not in UPLOAD_EXTENSIONS:
                raise ValueError(f"暂不支持此文件类型：{name}")
            if name.casefold() in names:
                raise ValueError(f"上传文件重名，请先重命名：{name}")
            names.add(name.casefold())
        path = Path(ProjectManager(root).init_project(
            "web_" + uuid.uuid4().hex[:10], canvas, quick_generate=True,
        )).resolve()
        (path / "sources").mkdir()
        (path / ".web_gui").mkdir()
        for item in uploads:
            with (path / "sources" / item.filename).open("xb") as stream:
                item.save(stream)
        brief = {"topic": topic, "slides": count, "format": canvas, "style": style,
                 "mode": "quick", "page_count_mode": count_mode, "min_pages": minimum,
                 "max_pages": maximum, "analysis_depth": depth, "notes": True,
                 "source_files": [item.filename for item in uploads],
                 "created_at": datetime.now(timezone.utc).isoformat()}
        (path / ".web_gui" / "brief.json").write_text(
            json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return jsonify(describe(path, detail=True)), 201

    @app.get("/api/projects/<project_id>")
    def get_project(project_id):
        return jsonify(describe(project_path(project_id), detail=True))

    @app.post("/api/projects/<project_id>/generate")
    def generate(project_id):
        path = project_path(project_id)
        brief = saved_brief(path)
        if not brief or brief.get("mode") != "quick":
            raise ValueError("此项目没有网页生成需求，请新建一个项目。")
        if runner.busy:
            return jsonify(error="已有制作任务正在运行，请等待完成或先取消。"), 409
        try:
            job = runner.start(path, brief)
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 409 if runner.busy else 503
        return jsonify(job), 202

    @app.post("/api/projects/<project_id>/cancel")
    def cancel(project_id):
        path = project_path(project_id)
        _safe_child(path, ".web_gui")
        try:
            return jsonify(runner.cancel(path))
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 409

    @app.get("/api/projects/<project_id>/files/<path:filename>")
    def preview(project_id, filename):
        path = project_path(project_id)
        if "\\" in filename or ":" in filename or ".." in Path(filename).parts:
            abort(404)
        parts = Path(filename).parts
        if not parts or parts[0] not in {"svg_output", "svg_final", "images", "icons"}:
            abort(404)
        file = _safe_child(path, filename)
        if not file.is_file() or file.suffix.lower() not in IMAGE_EXTENSIONS:
            abort(404)
        if file.suffix.lower() == ".svg" and parts[0] in {"svg_output", "svg_final"}:
            content, warnings = _browser_svg(file, path)
            response = Response(content, mimetype="image/svg+xml")
            if warnings:
                response.headers["X-Preview-Warnings"] = json.dumps(warnings, ensure_ascii=True)
        else:
            response = send_file(file)
        response.headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
            "font-src data:; frame-ancestors 'self'"
        )
        return response

    @app.get("/api/projects/<project_id>/downloads/<filename>")
    def download(project_id, filename):
        path = project_path(project_id)
        if any(c in filename for c in "/\\:") or Path(filename).suffix.lower() != ".pptx":
            abort(404)
        file = _safe_child(path, "exports/" + filename)
        if not file.is_file():
            abort(404)
        return send_file(file, as_attachment=True, download_name=filename)

    @app.get("/api/projects/<project_id>/notes/<filename>")
    def download_notes(project_id, filename):
        path = project_path(project_id)
        if any(c in filename for c in "/\\:") or Path(filename).suffix.lower() != ".md":
            abort(404)
        folder = _safe_child(path, "notes")
        if (path / "notes").is_symlink() or (folder / filename).is_symlink():
            abort(404)
        file = _safe_child(folder, filename)
        if not file.is_file():
            abort(404)
        return send_file(file, as_attachment=True, download_name=filename, mimetype="text/markdown")

    @app.get("/api/projects/<project_id>/outline/download")
    def download_outline(project_id):
        path = project_path(project_id)
        if read_outline(path) is None:
            abort(404)
        file = _safe_child(path, "analysis/outline.json")
        return send_file(file, as_attachment=True, download_name="outline.json", mimetype="application/json")

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open the PPT Master local web workspace.")
    parser.add_argument("--port", type=int, default=8765, help="Preferred local port (default: 8765)")
    parser.add_argument("--projects-dir", type=Path, default=PROJECTS_ROOT, help="Project workspace root")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_port(args.port)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    from attribution_guard import require_skill_integrity
    require_skill_integrity()
    root = args.projects_dir.resolve()
    runtime = root / ".web_gui"
    runtime.mkdir(parents=True, exist_ok=True)
    lock_file = runtime / "server.lock"
    existing = read_lock(lock_file)
    if existing and process_alive(lock_pid(existing)):
        url = f'http://127.0.0.1:{existing.get("port")}'
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=3) as response:
                health = json.load(response)
            if health.get("service") == "ppt-master-web-gui" and health.get("projects_root") == str(root):
                print(f"PPT Master is already running: {url}")
                if not args.no_browser:
                    open_preview_browser(url)
                return 0
        except (OSError, ValueError, urllib.error.URLError):
            pass
        print("A local GUI process still owns this workspace. Close its window and retry.", file=sys.stderr)
        return 1
    try:
        from werkzeug.serving import make_server
        app = create_app(root)
    except ImportError as exc:
        print(f'Missing dependency: {exc}. Run: "{sys.executable}" -m pip install -r '
              f'"{SKILL_DIR / "requirements.txt"}"', file=sys.stderr)
        return 1
    port = find_free_port(args.port)
    server = make_server("127.0.0.1", port, app, threaded=True)
    if claim_lock(lock_file, port):
        server.server_close()
        print("Another GUI launch is in progress. Retry shortly.", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{port}"
    print(f"PPT Master Web GUI: {url}", flush=True)
    print(f"Workspace: {root}\nKeep this window open. Press Ctrl+C to stop.")
    if not args.no_browser:
        open_preview_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping PPT Master Web GUI...")
    finally:
        app.config["RUNNER"].close()
        server.server_close()
        release_lock(lock_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
