#!/usr/bin/env python3
"""PPT Master - Validated Outline Planner

Plan from topics or every supplied document chunk through an injected model client,
with strict source coverage, useful notes, and two bounded model repair attempts.

Usage:
    Import plan_outline from web_gui.outline.

Examples:
    outline = plan_outline(chunks, brief, client, progress, cancel_event)

Dependencies:
    None (only uses standard library)
"""

from __future__ import annotations

import copy
import json
import re
from threading import Event
from typing import Any, Callable

from web_gui.outline_prompts import SYSTEM_PROMPT, build_prompt

SLIDE_TYPES = {"title", "agenda", "content", "transition", "conclusion", "thankyou"}
TYPE_ALIASES = {
    "cover": "title", "intro": "title", "introduction": "title",
    "outline": "agenda", "catalog": "agenda", "directory": "agenda",
    "body": "content", "main": "content", "summary": "conclusion", "closing": "conclusion",
    "thanks": "thankyou", "thank_you": "thankyou", "ending": "thankyou", "q&a": "thankyou",
    "section": "transition", "section_divider": "transition", "chapter_divider": "transition",
}
_PLACEHOLDER_RE = re.compile(
    r"^(?:\.{3}|…|(?:point|bullet|slide|page|要点|补充要点|幻灯片|内容要点)\s*\d*|"
    r"\[.*\]|<.*>)$", re.IGNORECASE,
)
_DECORATIVE_ARROWS_RE = re.compile(r"^[\s↑↓←→↔↕↖↗↘↙⇐⇒⇑⇓⇔⇕➔➜➝➞➟➠➡⬅⬆⬇↪↩⟶⟵⟷]+$")
_DATE_RE = re.compile(
    r"(?<!\d)(?:19|20|21)\d{2}(?:\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?"
    r"|[-/.]\d{1,2}(?:[-/.]\d{1,2})?)?(?!\d)"
)


class OutlineGenerationError(RuntimeError):
    """Expose the last valid plan for debugging without reporting incomplete success."""

    def __init__(
        self, message: str, last_valid_outline: dict | None = None, *,
        invalid_output: str = "", validation_errors: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.last_valid_outline = copy.deepcopy(last_valid_outline)
        self.invalid_output = invalid_output
        self.validation_errors = list(validation_errors or [])


def _check_cancel(cancel_event) -> None:
    if cancel_event.is_set():
        raise InterruptedError("Outline generation cancelled.")


def _page_limits(brief: dict) -> tuple[int, int]:
    mode = brief.get("page_count_mode", "auto")
    if mode == "auto":
        return 5, 30
    if mode == "fixed":
        values = (brief.get("slides"), brief.get("slides"))
    elif mode == "range":
        values = (brief.get("min_pages"), brief.get("max_pages"))
    else:
        raise ValueError("page_count_mode must be auto, fixed, or range.")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise ValueError("Page limits must be integers.")
    lower, upper = values
    if not 1 <= lower <= upper <= 40:
        raise ValueError("Page limits must satisfy 1 <= minimum <= maximum <= 40.")
    return lower, upper


def _parse_json(raw: str, *, structure: bool = False) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("The model returned empty JSON output.")
    text = raw.strip().lstrip("\ufeff")
    candidates = [text]
    candidates.extend(re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE))
    decoder = json.JSONDecoder()
    for match in list(re.finditer(r"[\{\[]", text))[:100]:
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and (
            (structure and "sections" in value) or any(key in value for key in ("slides", "pages", "outline"))
        ):
            candidates.append(json.dumps(value, ensure_ascii=False))
    for candidate in candidates:
        try:
            result = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            return result
        if not structure and isinstance(result, list) and all(isinstance(item, dict) for item in result):
            return {"slides": result}
    raise ValueError("Response is not a complete JSON object matching the requested schema.")


def _string(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _integer(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        return int(value.strip())
    return value


def _text_points(value) -> list[str]:
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("text", item.get("content", item.get("point", "")))
        text = _string(item)
        if text:
            result.append(re.sub(r"^[\-\*•]\s+", "", text))
    return result


def _normalize(data: dict) -> dict:
    if isinstance(data.get("outline"), dict):
        data = {**data, **data["outline"]}
    slides = data.get("slides", data.get("pages"))
    normalized = []
    for index, slide in enumerate(slides if isinstance(slides, list) else [], 1):
        if not isinstance(slide, dict):
            normalized.append(slide)
            continue
        raw_type = _string(slide.get("slide_type", slide.get("type"))).lower().replace(" ", "_")
        refs = slide.get("source_refs", [])
        if isinstance(refs, str):
            refs = [refs]
        normalized.append({
            "page_number": _integer(slide.get("page_number", index)),
            "title": _string(slide.get("title", slide.get("heading"))),
            "slide_type": TYPE_ALIASES.get(raw_type, raw_type),
            "content_points": _text_points(
                slide.get("content_points", slide.get("bullet_points", slide.get("points"))),
            ),
            "description": _string(slide.get("description")),
            "notes": _string(slide.get("notes", slide.get("speaker_notes"))),
            "source_refs": refs,
        })
    return {
        "title": _string(data.get("title", data.get("topic"))),
        "total_pages": _integer(data.get("total_pages", len(normalized))),
        "page_count_reason": _string(data.get("page_count_reason")),
        "slides": normalized,
    }


def _useful(text: str) -> bool:
    return bool(text and not _PLACEHOLDER_RE.fullmatch(text))


def _validate(
    outline: dict, page_limits: tuple[int, int], required_refs: list[str], *, provided_text: str = "",
) -> list[str]:
    errors = []
    slides = outline["slides"]
    if not _useful(outline["title"]):
        errors.append("title must be a useful, nonempty presentation title.")
    if not outline["page_count_reason"]:
        errors.append("page_count_reason must explain the chosen page count.")
    lower, upper = page_limits
    if not lower <= len(slides) <= upper:
        errors.append(f"page count is {len(slides)}; required {lower} to {upper} pages.")
    if type(outline["total_pages"]) is not int or outline["total_pages"] != len(slides):
        errors.append("total_pages must equal the actual number of slides.")
    known = set(required_refs)
    covered = set()
    supplied_dates = [tuple(re.findall(r"\d+", match.group())) for match in _DATE_RE.finditer(provided_text)]
    for index, slide in enumerate(slides, 1):
        prefix = f"slides[{index - 1}]"
        if not isinstance(slide, dict):
            errors.append(f"{prefix} must be an object.")
            continue
        if type(slide["page_number"]) is not int or slide["page_number"] != index:
            errors.append(f"{prefix}.page_number must be the consecutive integer {index}.")
        if not _useful(slide["title"]):
            errors.append(f"{prefix}.title must state a meaningful page topic.")
        if slide["slide_type"] not in SLIDE_TYPES:
            errors.append(f"{prefix}.slide_type must be one of {', '.join(sorted(SLIDE_TYPES))}.")
        if not slide["content_points"] or not all(_useful(point) for point in slide["content_points"]):
            errors.append(f"{prefix}.content_points must contain meaningful nonempty strings, not placeholders.")
        for point_index, point in enumerate(slide["content_points"]):
            if _DECORATIVE_ARROWS_RE.fullmatch(point):
                errors.append(f"{prefix}.content_points[{point_index}] contains only decorative arrows; "
                              "express the intended relationship in meaningful words.")
            if slide["slide_type"] == "title" and _DATE_RE.fullmatch(point):
                date = tuple(str(int(number)) for number in re.findall(r"\d+", point))
                if not any(date == tuple(str(int(number)) for number in supplied[:len(date)])
                           for supplied in supplied_dates):
                    errors.append(f"{prefix}.content_points[{point_index}] adds an unsupported cover date "
                                  f"{point!r}; the runtime clock is not presentation metadata.")
        if not slide["description"]:
            errors.append(f"{prefix}.description must explain the purpose of the slide.")
        notes = slide["notes"]
        if len(notes) < 20 or notes == slide["title"] or notes == "\n".join(slide["content_points"]):
            errors.append(f"{prefix}.notes must be a substantive spoken explanation, not empty or copied bullets.")
        refs = slide["source_refs"]
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
            errors.append(f"{prefix}.source_refs must be an array of supplied chunk IDs.")
        else:
            unknown = set(refs) - known
            if unknown:
                errors.append(f"{prefix}.source_refs cites unknown chunks: {', '.join(sorted(unknown))}.")
            covered.update(set(refs) & known)
    if len(slides) >= 2:
        if isinstance(slides[0], dict) and slides[0]["slide_type"] != "title":
            errors.append("The first slide must be a title page for a multi-page deck.")
        if isinstance(slides[-1], dict) and slides[-1]["slide_type"] not in {"conclusion", "thankyou"}:
            errors.append("The final slide must be conclusion or thankyou for a multi-page deck.")
    missing = known - covered
    if missing:
        errors.append(f"Missing source coverage for processed chunks: {', '.join(sorted(missing))}.")
    return errors


def _validate_structure(value: dict) -> list[str]:
    errors = []
    if "title" not in value or (value["title"] is not None and not isinstance(value["title"], str)):
        errors.append("Structure title must be a string or null when absent from the source.")
    for name in ("type", "language", "complexity"):
        if not _string(value.get(name)):
            errors.append(f"Structure {name} must be a nonempty string.")
    for name in ("sections", "key_concepts", "key_data", "main_conclusions"):
        values = value.get(name)
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            errors.append(f"Structure {name} must be a list of strings, empty if unsupported.")
    return errors


def _generate_validated(
    phase: str, brief: dict, client, progress, cancel_event, *, page_limits: tuple[int, int],
    chunk: dict | None = None, structure: dict | None = None, previous: dict | None = None,
    context: str = "", required_refs: list[str] | None = None, structure_target: bool = False,
    provided_text: str = "",
) -> dict:
    errors, raw = [], ""
    invalid_output = ""
    content_errors = False
    phases = (phase, "error_recovery", "json_repair")
    max_tokens = 8192 if structure_target else min(24000, max(8192, page_limits[1] * 400))
    for attempt, current_phase in enumerate(phases):
        _check_cancel(cancel_event)
        if attempt:
            detail = "; ".join(errors) if content_errors else "模型服务未返回可校验结果。"
            message = f"正在校正大纲（第 {attempt}/2 次）：{detail}"[:1000]
            progress("outline_repair", message, attempt, 2)
        prompt = build_prompt(
            current_phase, brief, page_limits=page_limits, chunk=chunk, structure=structure,
            previous=previous, context=context, required_refs=required_refs,
            errors=errors, invalid_output=raw[:48000], structure_target=structure_target,
        )
        try:
            raw = client.complete(SYSTEM_PROMPT, prompt, temperature=0.3, max_tokens=max_tokens, json_mode=True)
        except InterruptedError:
            raise
        except RuntimeError as exc:
            if getattr(exc, "recoverable", True) is False:
                raise
            errors = [f"Model request failed after provider retries: {exc}"]
            raw = ""
            content_errors = False
            continue
        _check_cancel(cancel_event)
        try:
            value = _parse_json(raw, structure=structure_target)
            value = value if structure_target else _normalize(value)
            errors = _validate_structure(value) if structure_target else _validate(
                value, page_limits, required_refs or [], provided_text=provided_text,
            )
        except ValueError as exc:
            errors = [str(exc)]
        if not errors:
            return value
        invalid_output = raw
        content_errors = True
    identifier = f' for chunk {chunk["id"]}' if chunk else ""
    raise OutlineGenerationError(
        f"{phase}{identifier} failed after two repair attempts: " + "; ".join(errors), previous,
        invalid_output=invalid_output, validation_errors=errors,
    )


def plan_outline(
    chunks: list[dict], brief: dict, client: Any,
    progress: Callable[[str, str, int, int], None], cancel_event: Event,
) -> dict:
    """Produce a validated full outline, or raise with the last valid debugging snapshot."""
    page_limits = _page_limits(brief)
    if not isinstance(chunks, list):
        raise ValueError("Source chunks must be a list.")
    ids = []
    for chunk in chunks:
        if not isinstance(chunk, dict) or not all(_string(chunk.get(name)) for name in ("id", "source", "text")):
            raise ValueError("Every source chunk requires a nonempty id, source, and text.")
        if chunk["id"] in ids:
            raise ValueError("Source chunk IDs must be unique.")
        ids.append(chunk["id"])
    _check_cancel(cancel_event)
    last_valid = None
    source_names = []
    # Runtime timestamps and bookkeeping fields are not source evidence.
    provided_text = "\n".join(_string(brief.get(name)) for name in ("topic", "style"))
    try:
        if not chunks:
            progress("initial_outline", "正在根据主题规划演示大纲。", 0, 1)
            result = _generate_validated(
                "topic_outline", brief, client, progress, cancel_event, page_limits=page_limits,
                provided_text=provided_text,
            )
            result["metadata"] = {"chunks_processed": 0, "source_names": [], "source_chunk_ids": [],
                                  "page_count_mode": brief.get("page_count_mode", "auto")}
            progress("initial_outline", "主题大纲已通过结构校验。", 1, 1)
            return result
        progress("analyzing_structure", "正在分析首份资料的结构。", 0, len(chunks))
        structure = _generate_validated(
            "structure_analysis", brief, client, progress, cancel_event, page_limits=page_limits,
            chunk=chunks[0], structure_target=True,
        )
        context = ""
        for index, chunk in enumerate(chunks):
            provided_text += "\n" + chunk["text"]
            prior_text = "" if last_valid is None else json.dumps(
                {"title": last_valid["title"], "slides": last_valid["slides"]}, ensure_ascii=False,
            )
            phase = "initial_outline" if index == 0 else "refine_outline"
            stage = "initial_outline" if index == 0 else "refining_outline"
            progress(stage, f"正在整合资料片段 {index + 1}/{len(chunks)}：{chunk['source']}",
                     index, len(chunks))
            result = _generate_validated(
                phase, brief, client, progress, cancel_event, page_limits=page_limits, chunk=chunk,
                structure=structure, previous=last_valid, context=context, required_refs=ids[:index + 1],
                provided_text=provided_text + "\n" + prior_text,
            )
            if chunk["source"] not in source_names:
                source_names.append(chunk["source"])
            result["metadata"] = {
                "chunks_processed": index + 1, "source_names": list(source_names),
                "source_chunk_ids": ids[:index + 1], "page_count_mode": brief.get("page_count_mode", "auto"),
            }
            last_valid = copy.deepcopy(result)
            context = (context + f"\n[{chunk['id']}] {chunk['source']}\n" + chunk["text"][:500])[-2000:]
            progress(stage, f"已整合并校验资料片段 {index + 1}/{len(chunks)}。", index + 1, len(chunks))
        return last_valid
    except InterruptedError:
        raise
    except OutlineGenerationError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise OutlineGenerationError(f"Outline planning failed: {exc}", last_valid) from exc
