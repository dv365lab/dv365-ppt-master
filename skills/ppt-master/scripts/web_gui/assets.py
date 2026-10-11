#!/usr/bin/env python3
"""PPT Master - Web GUI Supplied Image Preparation

Preserve uploaded and converter-referenced bitmaps as verified local image assets.
Use analysis/prepared_images.json for the handoff inventory; the license registry
in references/image-base.md remains owned by explicitly supplied credit metadata.

Usage:
    Import prepare_images from the Web GUI generation pipeline.

Examples:
    images = prepare_images(project, client, progress, cancel_event)

Dependencies:
    Pillow
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

from PIL import Image

from web_gui.ingestion import _check_cancel, _safe

_RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_FORMAT_SUFFIXES = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}
_MARKDOWN_IMAGE = re.compile(
    r'''!\[(?P<alt>(?:\\.|[^\]])*)\]\(\s*'''
    r'''(?P<url><[^>\n]+>|(?:\\.|[^()\s]|\([^()\n]*\))+?)'''
    r'''(?:\s+["'][^\n]*?["'])?\s*\)''',
)
_REFERENCE_IMAGE = re.compile(r"!\[(?P<alt>(?:\\.|[^\]])*)\]\[(?P<key>[^\]]*)\]")
_REFERENCE_DEFINITION = re.compile(r"^\s{0,3}\[([^\]]+)\]:\s*(<[^>\n]+>|\S+)", re.M)


class _HTMLImages(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            values = dict(attrs)
            if values.get("src"):
                self.images.append((values["src"], values.get("alt") or values.get("title") or ""))


def _references(text: str) -> list[tuple[str, str]]:
    # Literal examples in fenced code are not supplied image placements.
    text = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$", "", text)
    references = [(match["url"].strip("<>"), match["alt"]) for match in _MARKDOWN_IMAGE.finditer(text)]
    definitions = {match[1].strip().casefold(): match[2].strip("<>")
                   for match in _REFERENCE_DEFINITION.finditer(text)}
    for match in _REFERENCE_IMAGE.finditer(text):
        key = (match["key"] or match["alt"]).strip().casefold()
        if key in definitions:
            references.append((definitions[key], match["alt"]))
    parser = _HTMLImages()
    parser.feed(text)
    references.extend(parser.images)
    return references


def _json_file(project: Path, relative: str) -> dict:
    path = _safe(project, project / relative)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise RuntimeError(f"无法读取图片准备所需的项目清单：{relative}。") from error
    if not isinstance(value, dict):
        raise ValueError(f"项目清单必须是 JSON 对象：{relative}。")
    return value


def _markdown_documents(project: Path, manifest: dict, progress: Callable) -> list[tuple[Path, str, str]]:
    folder = _safe(project, project / "analysis/source_markdown")
    if not folder.is_dir():
        return []
    entries = manifest.get("documents")
    if isinstance(entries, list):
        candidates = [(project / item["markdown"], str(item.get("source", ""))) for item in entries
                      if isinstance(item, dict) and isinstance(item.get("markdown"), str)]
    else:
        candidates = [(path, path.name) for path in sorted(folder.glob("*.md"))]
    result = []
    seen = set()
    for candidate, source in candidates:
        try:
            path = _safe(folder, candidate)
        except ValueError:
            progress("preparing_images", "跳过不在已转换正文目录内的图片来源记录。")
            continue
        if path in seen or path.suffix.lower() not in {".md", ".markdown"} or not path.is_file():
            continue
        seen.add(path)
        result.append((path, source or path.name, path.read_text(encoding="utf-8", errors="replace")))
    return result


def _local_reference(project: Path, markdown: Path, reference: str) -> Path:
    reference = unquote(unescape(reference.strip()))
    if not reference or any(char in reference for char in "\\\x00"):
        raise ValueError("无效的图片引用")
    url = urlsplit(reference)
    if url.scheme or url.netloc or url.query or url.fragment or ":" in url.path:
        raise ValueError("只接受项目本地图片")
    relative = Path(url.path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("图片引用不能使用绝对路径或越界路径")
    if relative.suffix.lower() not in _RASTER_SUFFIXES:
        raise ValueError("不支持的位图格式")
    path = _safe(project, markdown.parent / relative)
    if not path.is_file():
        raise ValueError("引用的图片文件不存在")
    return path


def _bitmap(path: Path) -> tuple[bytes, int, int, str]:
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as image:
        width, height = image.size
        suffix = _FORMAT_SUFFIXES.get(image.format)
        if suffix is None:
            raise ValueError("只接受 PNG、JPEG、WEBP 或 GIF 位图")
        image.verify()
    return raw, width, height, suffix


def prepare_images(project: Path, client, progress: Callable, cancel_event: threading.Event) -> list[dict]:
    """Copy verified source figures without extra model calls or source modifications."""
    project = project.resolve()
    _check_cancel(cancel_event)
    brief = _json_file(project, ".web_gui/brief.json")
    manifest = _json_file(project, "analysis/source_manifest.json")
    documents = _markdown_documents(project, manifest, progress)
    summaries = {source: " ".join(text.split())[:500] for _, source, text in documents}
    sources = _safe(project, project / "sources")
    names = brief.get("source_files", [])
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise ValueError("source_files 必须是上传文件名列表。")
    candidates = []
    for name in names:
        if not name or any(char in name for char in "/\\:") or name in {".", ".."}:
            raise ValueError("上传图片文件名不能包含路径。")
        if Path(name).suffix.lower() not in _RASTER_SUFFIXES:
            continue
        source = _safe(sources, sources / name)
        if not source.is_file():
            raise RuntimeError(f"上传图片不存在：{name}。请重新上传。")
        candidates.append((source, name, summaries.get(name, f"用户提供的图片：{name}"), True))
    for markdown, source_name, text in documents:
        _check_cancel(cancel_event)
        for reference, alt in _references(text):
            try:
                source = _local_reference(project, markdown, reference)
            except ValueError as error:
                progress("preparing_images", f"跳过 {source_name} 中的图片引用：{error}。")
                continue
            description = " ".join(unescape(alt).split()) or f"{source_name} 中的插图"
            candidates.append((source, source_name, description, False))
    folder = _safe(project, project / "images")
    folder.mkdir(parents=True, exist_ok=True)
    images = []
    by_digest = {}
    for index, (source, origin, description, required) in enumerate(candidates, 1):
        _check_cancel(cancel_event)
        try:
            raw, width, height, suffix = _bitmap(source)
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            if required:
                raise RuntimeError(f"上传图片无法验证：{origin}。请检查文件或重新导出图片。") from error
            progress("preparing_images", f"跳过 {origin} 中无法验证的位图，请检查转换后的图片。")
            continue
        digest = hashlib.sha256(raw).hexdigest()
        if digest in by_digest:
            existing = by_digest[digest]
            if origin not in existing["source"].split(" / "):
                existing["source"] += " / " + origin
            if description and description not in existing["description"]:
                existing["description"] += "\n" + description
            continue
        filename = f"source_{digest[:20]}{suffix}"
        destination = _safe(folder, folder / filename)
        if destination.exists():
            if not destination.is_file() or destination.read_bytes() != raw:
                raise RuntimeError(f"预备图片文件发生内容冲突，原文件已保留：{filename}。")
        else:
            with destination.open("xb") as handle:
                handle.write(raw)
        row = {"file": filename, "href": "../images/" + filename, "source": origin,
               "width": width, "height": height, "description": description}
        images.append(row)
        by_digest[digest] = row
        progress("preparing_images", f"已保留资料图片：{origin}（{width} × {height}）。", index, len(candidates))
    _check_cancel(cancel_event)
    analysis = _safe(project, project / "analysis")
    analysis.mkdir(parents=True, exist_ok=True)
    output = _safe(analysis, analysis / "prepared_images.json")
    temporary = _safe(analysis, analysis / "prepared_images.json.tmp")
    temporary.write_text(json.dumps({"version": 1, "images": images}, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(output)
    progress("preparing_images", f"已准备 {len(images)} 张本地资料图片。", len(images), len(images))
    return images
