#!/usr/bin/env python3
"""PPT Master - API Presentation Pipeline

Plan sources with a compatible model, author SVG pages, ground speaker notes in
the checked pages, and export through the existing native PPTX pipeline.
The user-requested outline JSON is a planning deliverable, not a spec lock.

Usage:
    python3 scripts/web_gui/pipeline.py --project /absolute/project

Examples:
    python3 scripts/web_gui/pipeline.py --project /absolute/projects/web_demo

Dependencies:
    Project requirements; configured model endpoint
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
SKILL_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from console_encoding import configure_utf8_stdio  # noqa: E402

configure_utf8_stdio()

from config import CANVAS_FORMATS  # noqa: E402
from web_gui.provider import ModelClient, ProviderError, load_config  # noqa: E402


def safe_path(project: Path, relative: str) -> Path:
    """Confine a generated artifact to the current project."""
    result = (project / relative).resolve()
    if result == project or not result.is_relative_to(project):
        raise ValueError("生成文件的路径超出当前项目。")
    return result


def write_json(path: Path, value: dict) -> None:
    """Atomically replace a JSON artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.is_symlink():
        raise ValueError("拒绝向符号链接写入生成结果。")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def progress(stage: str, message: str, current: int = 0, total: int = 0) -> None:
    """Emit machine-readable progress without credentials or raw API responses."""
    print(json.dumps({"type": "progress", "stage": stage, "message": message,
                      "current": current, "total": total}, ensure_ascii=False), flush=True)


def extract_svg(raw: str, viewbox: str, *, source_text: str | None = None) -> str:
    """Extract one native SVG and reject active content and unprepared resources."""
    match = re.search(r"<svg\b[\s\S]*?</svg\s*>", raw, re.I)
    if not match:
        raise ValueError("模型没有返回完整 SVG。")
    content = match.group(0)
    root = ElementTree.fromstring(content)
    if root.tag != "{http://www.w3.org/2000/svg}svg" or root.get("viewBox") != viewbox:
        raise ValueError("SVG namespace 或画布 viewBox 与项目不一致。")
    native_text = False
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in {"script", "foreignObject", "iframe", "style", "animate", "set"}:
            raise ValueError(f"SVG 包含不支持的活动或 HTML 内容：{tag}")
        if tag == "text" and "".join(element.itertext()).strip():
            native_text = True
        for key, value in element.attrib.items():
            name = key.rsplit("}", 1)[-1]
            if name.lower().startswith("on"):
                raise ValueError("SVG 不能包含事件处理器。")
            if name == "href":
                decoded = unquote(value)
                if decoded.startswith("#"):
                    continue
                tail = decoded.removeprefix("../images/")
                if (not decoded.startswith("../images/") or tail in {"", ".", ".."}
                        or any(c in tail for c in "/\\:")):
                    raise ValueError("SVG 只能引用本项目预备的 images 文件或页内元素。")
    if not native_text:
        raise ValueError("页面必须包含原生可编辑文字。")
    if source_text is not None:
        source = re.sub(r"\s+", "", source_text).casefold()
        for text_node in root.iter():
            if text_node.tag.rsplit("}", 1)[-1] != "text":
                continue
            label = "".join(text_node.itertext()).strip()
            if (re.fullmatch(r"(?:19|20)\d{2}(?:[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?)?年?", label)
                    and label.casefold() not in source):
                raise ValueError(f"页面包含未提供的日期：{label}。请删除自动添加的日期或年份。")
        for element in root.iter():
            identity = element.get("id", "") + " " + element.get("data-pptx-role", "")
            if not re.search(r"footer|logo|watermark|copyright|branding", identity, re.I):
                continue
            for text_node in element.iter():
                if text_node.tag.rsplit("}", 1)[-1] != "text":
                    continue
                label = "".join(text_node.itertext()).strip()
                normalized = re.sub(r"\s+", "", label).casefold()
                if not normalized or normalized in source:
                    continue
                credit = re.search(r"logo|watermark|copyright|branding", identity, re.I) or re.search(
                    r"\b(?:AI|company|inc|llc|copyright|author)\b|(?:powered|generated|made|presented)\s+by"
                    r"|©|公司|作者|部门|制作[:：]", label, re.I,
                )
                if not credit:
                    continue
                if re.fullmatch(r"(?:第|page)?\s*\d+\s*页?\s*(?:[/／]|of)?\s*共?\s*\d*\s*页?",
                                label, re.I):
                    continue
                labels = {"来源", "参考", "来源参考", "资料来源", "source", "reference", "提示", "警示", "注意"}
                parts = [re.sub(r"\s+", "", part).casefold().strip("⚠ℹ")
                         for part in re.split(r"[·|：:;；]", label)]
                if all(not part or part in labels or part in source for part in parts):
                    continue
                raise ValueError(f"页脚或署名包含未提供的文字：{label}。仅保留明确提供的文字或页码。")
    changed = False
    for index, group in enumerate(list(root)):
        if (group.tag.rsplit("}", 1)[-1] != "g" or len(group) != 1
                or group.get("id") not in {"page-number", "page_number", "pageNumber"}
                or group.get("data-pptx-role") not in {None, "page-number"}
                or not set(group.attrib) <= {"id", "data-pptx-bounds", "data-pptx-role"}):
            continue
        atom = group[0]
        if atom.tag.rsplit("}", 1)[-1] != "text" or atom.get("id") or atom.get("data-pptx-role"):
            continue
        # A lone page-number text is already an atomic page-frame object. This
        # removes only its redundant bounds wrapper, preserving all visual attrs.
        atom.set("id", group.get("id"))
        atom.set("data-pptx-role", "page-number")
        root.remove(group)
        root.insert(index, atom)
        changed = True
    if changed:
        ElementTree.register_namespace("", "http://www.w3.org/2000/svg")
        content = ElementTree.tostring(root, encoding="unicode")
    return content + "\n"


def prepare_output_folders(project: Path) -> None:
    """Archive the previous SVG/notes roster before writing a new complete one."""
    suffix = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    backup = safe_path(project, "backup/web_gui_" + suffix)
    for name in ("svg_output", "notes"):
        source = safe_path(project, name)
        if source.exists() and any(source.iterdir()):
            destination = safe_path(project, str(backup.relative_to(project) / name))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
        source.mkdir(parents=True, exist_ok=True)


def verify_delivery(path: Path, expected_pages: int) -> None:
    """Require an actual PPTX with one nonempty notes part for every planned page."""
    from pptx import Presentation
    deck = Presentation(path)
    if len(deck.slides) != expected_pages:
        raise ValueError(f"导出页数 {len(deck.slides)} 与大纲 {expected_pages} 不一致。")
    for index, slide in enumerate(deck.slides, 1):
        if not slide.has_notes_slide or not slide.notes_slide.notes_text_frame.text.strip():
            raise ValueError(f"第 {index} 页缺少讲稿 notes。")


def _script(project: Path, filename: str, *arguments: str, allow_failure=False) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / filename), str(project), *arguments],
        cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=240, env=env,
    )
    if result.returncode and not allow_failure:
        detail = (result.stderr or result.stdout)[-6000:]
        raise RuntimeError(f"{filename} 执行失败：{detail}")
    return result


def _page_system() -> str:
    standards = (SKILL_DIR / "references/shared-standards-core.md").read_text(encoding="utf-8")
    return """You author complete editable PowerPoint slides as canonical SVG. Return ONE SVG only.
Use the supplied page title, content_points and grounded supporting details; never invent facts or numbers.
The current_slide defines this page's factual scope. Do not expand it with unsupported product
capabilities, supported formats, or workflow steps. Preserve every constraint in user_request,
including restrictions to supplied facts, throughout initial authoring and any repair.
Use original layout decisions appropriate to this page's job and consistent colors/type across the deck.
Never add model/provider branding, invented company names, author credits, dates, copyright or slogans.
Footers may contain only the given deck title and page number, unless a credit is explicitly supplied.
Content must remain concise and readable: use explicit line breaks with text/tspan, not wrapping guesses.
Typical body 26-32px, title 40-52px; title 64-80px on covers; margin at least 48px.
Estimate CJK width as roughly one font-size per character, Latin roughly 0.6; split lines to fit.
Root font-family="Microsoft YaHei, Arial", positive font-size, exact requested viewBox.
All x/y/cx/cy coordinates are ABSOLUTE page coordinates. data-pptx-bounds is validation metadata,
NOT a layout position or translation. A group with bounds x=640 does NOT move children to x=640.
Write the full page offset into every child coordinate; do not use card-local coordinates.
Root data-pptx-page-role uses cover/toc/section/content/ending. No root paint.
Page numbers are direct root <text id="page-number" data-pptx-role="page-number"> atoms; do not wrap them in groups.
Group semantic units with unique ids and accurate data-pptx-bounds="x y width height".
Use ordinary native text/rect/circle/line/path/polygon geometry, common group inheritance and #RRGGBB colors.
Use no scripts, HTML, foreignObject, class, stylesheet, external network resource, animation or embedded data URI.
You may place relevant supplied images ONLY from the prepared_images list using their exact href and aspect ratio.
Use supplied image descriptions and source identity to choose an appropriate figure; do not replace native page
text with a screenshot. If a prepared image represents a whole reference slide, extract meaning rather than skinning
the page with it. Do not use icon placeholders or Master/Layout metadata. No other resource is available.
Keep every important content point visible. Use notes for expansion, never bury an independent claim there.
Complete any graph/table needed with editable SVG geometry and exact supplied data; never infer values.
The following repository standard is authoritative for technical grammar:
""" + standards


def _recovery_label(text: str) -> str:
    """Render common Markdown formatting without changing its text or numbers."""
    text = re.sub(r"(?m)^\s*#{1,6}\s+", "", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    return re.sub(r"`([^`]+)`", r"\1", text)


def recovery_layout(slide: dict, viewbox: str) -> dict:
    """Calculate readable absolute text positions for model-authored recovery SVG."""
    from text_measure import wrap_text

    _, _, width, height = map(float, viewbox.split())
    family = "Microsoft YaHei, Arial"
    title_lines, _, oversized = wrap_text(slide["title"], size=40, max_width=width - 144,
                                          family=family, weight="bold")
    if oversized:
        raise ValueError("标题包含无法安全换行的超长文本，请缩短标题。")
    title_bottom = 44 + len(title_lines) * 52
    body_top = max(164.0, title_bottom + 36)
    # Try the ordinary rhythm first, then tighter spacing and finally two
    # balanced columns. Preserve complete points and their reading order.
    for columns, line_ratio, padding, gap in ((1, 1.4, 20, 12), (1, 1.25, 8, 4), (2, 1.25, 8, 8)):
        column_width = (width - 128 - (columns - 1) * 32) / columns
        for size in (28, 24, 22):
            spacing = size * line_ratio
            wrapped = [wrap_text(_recovery_label(point), size=size, max_width=column_width - 48, family=family)
                       for point in slide["content_points"]]
            if any(oversized for _, _, oversized in wrapped):
                continue
            row_heights = [len(lines) * spacing + padding for lines, _, _ in wrapped]
            costs = [row_height + gap for row_height in row_heights]
            if columns == 1:
                partitions = [(0, len(wrapped))]
            elif len(wrapped) >= 2:
                split = min(range(1, len(wrapped)), key=lambda i: max(sum(costs[:i]), sum(costs[i:])))
                partitions = [(0, split), (split, len(wrapped))]
            else:
                continue
            if any(body_top + sum(costs[start:end]) > height - 64 for start, end in partitions):
                continue
            rows = []
            for column, (start, end) in enumerate(partitions):
                left, top = 64 + column * (column_width + 32), body_top
                for index in range(start, end):
                    lines = wrapped[index][0]
                    rows.append({"id": f"point-{index + 1}",
                                 "bounds": [round(left, 2), round(top, 2), round(column_width, 2),
                                            round(row_heights[index], 2)], "font_size": size,
                                 "lines": [{"text": line, "x": round(left + 16, 2),
                                            "y": round(top + size + i * spacing + 4, 2)}
                                           for i, line in enumerate(lines)]})
                    top += costs[index]
            return {"title": {"bounds": [64, 32, width - 128, title_bottom - 24], "font_size": 40,
                              "lines": [{"text": line, "x": 72, "y": 76 + i * 52}
                                        for i, line in enumerate(title_lines)]},
                    "body": rows, "page_number": {"x": width - 64, "y": height - 24, "font_size": 18}}
    raise ValueError("该页内容无法以可读字号完整放入页面；请增加页数或精简该页内容。")


def _author_page(client, outline, slide, brief, viewbox, *, previous="", errors="", safe_layout=False) -> str:
    context = {
        "canvas": viewbox, "style": brief.get("style", ""),
        "user_request": brief.get("topic", ""),
        "deck_title": outline["title"],
        "page_roster": [{"page": s["page_number"], "title": s["title"]} for s in outline["slides"]],
        "current_slide": slide,
        "prepared_images": brief.get("prepared_images", []),
        "design": "White or very light background, dark navy text, indigo accent unless user asks otherwise.",
    }
    prompt = json.dumps(context, ensure_ascii=False)
    system = _page_system()
    if safe_layout:
        context["required_absolute_layout"] = recovery_layout(slide, viewbox)
        prompt = json.dumps(context, ensure_ascii=False)
        system = ("CONSTRAINED LAYOUT RECOVERY: author a fresh complete SVG using EXACTLY the text lines, "
                  "absolute positions, font sizes and bounds in required_absolute_layout. "
                  "Do not change these coordinates or add text, cards, translations, badges, emoji or decoration. "
                  "Use a simple solid background and the user's color preference. Each body row is one native "
                  "text group; the title is one group; the page number is one root text atom. "
                  "Preserve every given text line and native editability. This guide only supplies measured "
                  "text positions; YOU must author the SVG. The following standard remains applicable.\n" + system)
    if previous:
        system = ("SURGICAL REPAIR MODE: preserve the existing SVG design, wording, coordinates and IDs. "
                  "Change ONLY attributes or elements directly responsible for the listed failures. "
                  "Do not redesign, rephrase, add decorations, badges, new modules, footer credits or images. "
                  "Bounds-only faults normally need only corrected data-pptx-bounds values. "
                  "If a group is explicitly a page-number badge, mark its static framing role correctly. "
                  "Return the COMPLETE corrected SVG. The following authoring guide is technical context, "
                  "not permission to redesign the page.\n" + system)
        prompt += "\nApply only the required corrections to this existing SVG:\n" + previous
    if errors:
        prompt += "\nActual validation errors to fix:\n" + errors
    allowed_text = "\n".join([str(brief.get("topic", "")), str(brief.get("style", "")),
                               json.dumps(outline, ensure_ascii=False)])
    for attempt in range(3):
        raw = client.complete(system, prompt, temperature=0.15 if previous else 0.35, max_tokens=16000)
        try:
            content = extract_svg(raw, viewbox, source_text=allowed_text)
            if safe_layout:
                root = ElementTree.fromstring(content)
                visible = re.sub(r"\s+", "", "".join(
                    "".join(element.itertext()) for element in root.iter()
                    if element.tag.rsplit("}", 1)[-1] == "text"
                ))
                required = [slide["title"], *slide["content_points"]]
                if any(re.sub(r"\s+", "", _recovery_label(text)) not in visible for text in required):
                    raise ValueError("简洁版式遗漏或改写了必需的标题或要点；请保留全部原文，不能截断或删减。")
            return content
        except (ValueError, ElementTree.ParseError) as exc:
            if attempt == 2:
                raise RuntimeError(f'第 {slide["page_number"]} 页 SVG 修复失败：{exc}') from exc
            progress("repairing_slides", f'修复第 {slide["page_number"]} 页 SVG 格式。')
            prompt = json.dumps(context, ensure_ascii=False) + "\nInvalid SVG:\n" + raw \
                + "\nRepair this exact error and return a complete SVG:\n" + str(exc)
    raise RuntimeError("SVG generation did not complete")


def _check_pages(project, client, outline, brief, viewbox, filenames, stage="final"):
    from web_gui.svg_repair import repair_module_bounds

    for attempt in range(4):
        progress("checking_slides", "检查页面的可编辑结构、文字边界与导出兼容性。")
        result = _script(project, "svg_quality_checker.py", "--quick-generate", "--canonical-authoring",
                         "--stage", stage, "--json", allow_failure=True)
        report_name = "svg_quality_early_report.json" if stage == "early" else "svg_quality_report.json"
        report_path = safe_path(project, "validation/" + report_name)
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
        if result.returncode == 0 and report.get("summary", {}).get("errors") == 0:
            return
        if attempt == 3:
            raise RuntimeError("页面质量检查在修复和简洁版式重建后仍未通过。详见 validation/svg_quality_report.json。")
        failed = {item["file"]: item.get("errors", []) for item in report.get("files", []) if item.get("errors")}
        if not failed:
            raise RuntimeError("项目级质量检查失败：" + (result.stderr or result.stdout)[-4000:])
        for index, filename in enumerate(filenames):
            if filename not in failed:
                continue
            progress("repairing_slides", f"按检查结果修复第 {index + 1} 页。", index + 1, len(filenames))
            path = safe_path(project, "svg_output/" + filename)
            if attempt == 2:
                progress("repairing_slides", f"为第 {index + 1} 页重建简洁可编辑版式。", index + 1, len(filenames))
                content = _author_page(client, outline, outline["slides"][index], brief, viewbox, safe_layout=True)
                path.write_text(content, encoding="utf-8")
                continue
            previous = path.read_text(encoding="utf-8")
            content, remaining = repair_module_bounds(previous, failed[filename], viewbox)
            if remaining:
                content = _author_page(client, outline, outline["slides"][index], brief, viewbox,
                                       previous=content, errors=json.dumps(remaining, ensure_ascii=False))
            path.write_text(content, encoding="utf-8")


def _final_notes(client, slide, svg: str) -> str:
    reference = (SKILL_DIR / "references/executor-notes.md").read_text(encoding="utf-8")
    system = """Write the speaker notes for ONE finalized slide, grounded in its actual SVG and the supplied draft.
Return only natural spoken prose in the slide's language, normally 2-5 sentences, without headings,
bullets, page labels, Markdown fences or stage directions. Preserve every material claim, number and qualifier.
Explain connections and implications without inventing facts. A short cover needs fewer words.
Never describe geometry, IDs, typography or colors. Smoothly bridge to the next idea when appropriate.
""" + reference
    prompt = json.dumps(slide, ensure_ascii=False) + "\nFINAL SVG:\n" + svg
    for attempt in range(2):
        notes = client.complete(system, prompt, temperature=0.2, max_tokens=2500).strip()
        if len(notes) >= 8 and not re.search(r"(?m)^\s*(?:#{1,6}\s|[-*]\s|```|---\s*$)", notes):
            return notes
        prompt += "\nReturn coherent plain prose only; no Markdown, headings or bullets."
    raise RuntimeError(f'第 {slide["page_number"]} 页 notes 不是完整的口播正文。')


def run_pipeline(project: Path, *, client: ModelClient | None = None) -> dict:
    """Execute intake, incremental planning, native slides, notes and checked export."""
    from attribution_guard import require_skill_integrity
    from web_gui.ingestion import collect_chunks
    from web_gui.outline import plan_outline
    from web_gui.assets import prepare_images

    require_skill_integrity()
    project = project.resolve()
    brief = json.loads(safe_path(project, ".web_gui/brief.json").read_text(encoding="utf-8"))
    if "page_count_mode" not in brief and type(brief.get("slides")) is int:
        brief = {**brief, "page_count_mode": "fixed", "min_pages": brief["slides"],
                 "max_pages": brief["slides"], "notes": True}
    cancel = threading.Event()
    client = client or ModelClient(load_config(), cancel_event=cancel, progress=progress)
    progress("converting", "读取输入内容和附件，准备完整正文。")
    chunks = collect_chunks(project, brief, client, progress, cancel)
    try:
        outline = plan_outline(chunks, brief, client, progress, cancel)
    except Exception as exc:
        last_valid = getattr(exc, "last_valid_outline", None)
        if last_valid:
            write_json(safe_path(project, "analysis/outline.partial.json"), last_valid)
        invalid_output = getattr(exc, "invalid_output", "")
        if invalid_output:
            invalid_path = safe_path(project, "analysis/outline.invalid.txt")
            invalid_path.parent.mkdir(parents=True, exist_ok=True)
            invalid_path.write_text(invalid_output, encoding="utf-8")
            write_json(safe_path(project, "analysis/outline.errors.json"),
                       {"errors": getattr(exc, "validation_errors", [])})
        raise
    outline_path = safe_path(project, "analysis/outline.json")
    write_json(outline_path, outline)
    count = len(outline["slides"])
    progress("outline_ready", f"已根据内容规划 {count} 页：{outline['page_count_reason']}", count, count)
    brief = {**brief, "prepared_images": prepare_images(project, client, progress, cancel)}
    prepare_output_folders(project)
    viewbox = CANVAS_FORMATS[brief.get("format", "ppt169")]["viewbox"]
    filenames = []
    for index, slide in enumerate(outline["slides"], 1):
        progress("rendering_slides", f"制作第 {index}/{count} 页：{slide['title']}", index, count)
        filename = f"{index:02d}_slide.svg"
        content = _author_page(client, outline, slide, brief, viewbox)
        safe_path(project, "svg_output/" + filename).write_text(content, encoding="utf-8")
        filenames.append(filename)
        if count >= 7 and index == 5:
            _check_pages(project, client, outline, brief, viewbox, filenames, stage="early")
    _check_pages(project, client, outline, brief, viewbox, filenames)
    notes_sections = []
    for index, (slide, filename) in enumerate(zip(outline["slides"], filenames), 1):
        progress("writing_notes", f"根据最终页面生成第 {index}/{count} 页讲稿。", index, count)
        svg = safe_path(project, "svg_output/" + filename).read_text(encoding="utf-8")
        slide["notes"] = _final_notes(client, slide, svg)
        safe_path(project, "notes/" + Path(filename).stem + ".md").write_text(
            slide["notes"] + "\n", encoding="utf-8",
        )
        write_json(outline_path, outline)
        notes_sections.append(f"# {Path(filename).stem}\n\n{slide['notes']}")
    safe_path(project, "notes/total.md").write_text("\n\n---\n\n".join(notes_sections) + "\n", encoding="utf-8")
    write_json(outline_path, outline)
    progress("exporting", "将讲稿逐页写入可编辑 PPTX。")
    _script(project, "total_md_split.py")
    exports = safe_path(project, "exports")
    exports.mkdir(exist_ok=True)
    output = safe_path(project, "exports/presentation_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".pptx")
    _script(project, "svg_to_pptx.py", "--quick-generate", "--with-notes", "-o", str(output))
    verify_delivery(output, count)
    result = {"type": "completed", "pages": count, "file": output.name, "notes": count}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate PPTX and notes using the configured API model.")
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        run_pipeline(args.project)
    except (ProviderError, RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
