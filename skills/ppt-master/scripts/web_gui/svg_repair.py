#!/usr/bin/env python3
"""PPT Master - Conservative SVG Module Bounds Repair

Expand ordinary direct-root module metadata from trusted local checker measurements.
No visual coordinates, typography or structured ownership are modified. The caller
must rerun the quality checker after applying this metadata-only correction.

Usage:
    Import repair_module_bounds from the Web GUI generation pipeline.

Examples:
    updated, unresolved = repair_module_bounds(svg, checker_errors, "0 0 1280 720")

Dependencies:
    Existing SVG quality checker and canvas contract modules
"""

from __future__ import annotations

import math
import re
from collections import Counter
from xml.etree import ElementTree as ET

from svg_quality.checker import (
    SVGQualityChecker,
    _PPTX_ROOT_STRUCTURE_ATTRS,
    _PPTX_STRUCTURE_ATTRS,
)
from svg_to_pptx.canvas_contract import CanvasContractError, parse_project_viewbox

_SVG_NS = "http://www.w3.org/2000/svg"
_NUMBER = r"-?(?:\d+(?:\.\d+)?|\.\d+)"
_PAIR = rf"\(({_NUMBER}), ({_NUMBER})\)-\(({_NUMBER}), ({_NUMBER})\)"
_DIAGNOSTIC = re.compile(
    r'^<text(?:\s[^<>]*)?>(?: .*?)? exceeds <g id="([^"<>]+)"> data-pptx-bounds '
    r'on the (?:horizontal|vertical|horizontal and vertical) axis: content '
    + _PAIR + r', container ' + _PAIR
    + rf', overflow horizontal {_NUMBER}%, vertical {_NUMBER}% \(error above {_NUMBER}%\); '
    r'expand the root module bounds into available non-overlapping space; otherwise reflow the text'
    r'(?:; .*)?$',
)


def _group(element: ET.Element) -> bool:
    return isinstance(element.tag, str) and element.tag.rsplit("}", 1)[-1] == "g"


def _transformed(element: ET.Element) -> bool:
    return "transform" in element.attrib or bool(re.search(r"(?:^|;)\s*transform\s*:",
                                                         element.get("style", ""), re.I))


def _ordinary(group: ET.Element, structured: bool, canvas: tuple) -> bool:
    if SVGQualityChecker._root_module_overlap_exempt(group, structured_page=structured, canvas=canvas):
        return False
    return not any(_transformed(element) or any(name in element.attrib for name in _PPTX_STRUCTURE_ATTRS)
                   for element in group.iter())


def _contains(outer: tuple, inner: tuple) -> bool:
    return outer[0] <= inner[0] <= inner[2] <= outer[2] and outer[1] <= inner[1] <= inner[3] <= outer[3]


def repair_module_bounds(content: str, errors: list, viewbox: str) -> tuple[str, list]:
    """Expand only safe metadata envelopes and preserve every unresolved diagnostic."""
    unresolved = list(errors)
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
        root = ET.fromstring(content, parser=parser)
        requested = parse_project_viewbox(viewbox)
        declared = parse_project_viewbox(root.get("viewBox"))
    except (ET.ParseError, CanvasContractError, TypeError, ValueError):
        return content, unresolved
    if root.tag not in {"svg", f"{{{_SVG_NS}}}svg"} or requested != declared or _transformed(root):
        return content, unresolved
    canvas = tuple(float(number) for number in requested.values)
    groups = [element for element in root if _group(element)]
    ids = Counter(element.get("id") for element in root.iter() if element.get("id"))
    root_by_id = {group.get("id"): group for group in groups if group.get("id") and ids[group.get("id")] == 1}
    structured = all((root.get(name) or "").strip() for name in _PPTX_ROOT_STRUCTURE_ATTRS)
    bounds = {id(group): SVGQualityChecker._resolved_root_module_bounds(group) for group in groups}
    proposals = {}
    for index, error in enumerate(errors):
        if not isinstance(error, str):
            continue
        match = _DIAGNOSTIC.fullmatch(error)
        if match is None:
            continue
        name = match[1]
        group = root_by_id.get(name)
        if group is None or not _ordinary(group, structured, canvas):
            continue
        resolved = bounds[id(group)]
        if resolved is None:
            continue
        actual = tuple(float(number) for number in match.groups()[1:5])
        reported = tuple(float(number) for number in match.groups()[5:9])
        if not all(math.isfinite(number) for number in actual + reported):
            continue
        old = resolved[1]
        # Diagnostics describe a particular prior envelope to one decimal place.
        if any(not math.isclose(a, b, rel_tol=0, abs_tol=0.051) for a, b in zip(old, reported)):
            continue
        if actual[2] <= actual[0] or actual[3] <= actual[1] or _contains(old, actual):
            continue
        previous = proposals.get(name, {"bounds": old, "errors": [], "group": group})
        left, top, right, bottom = previous["bounds"]
        previous["bounds"] = (min(left, actual[0] - 2), min(top, actual[1] - 2),
                              max(right, actual[2] + 2), max(bottom, actual[3] + 2))
        previous["errors"].append(index)
        proposals[name] = previous
    proposals = {name: proposal for name, proposal in proposals.items() if _contains(canvas, proposal["bounds"])}
    accepted = []
    for name, proposal in proposals.items():
        safe = True
        for other in groups:
            if other is proposal["group"]:
                continue
            if SVGQualityChecker._root_module_overlap_exempt(other, structured_page=structured, canvas=canvas):
                continue
            resolved = bounds[id(other)]
            if resolved is None:
                safe = False
                break
            other_proposal = proposals.get(other.get("id"))
            other_bounds = other_proposal["bounds"] if other_proposal is not None else resolved[1]
            width, height = SVGQualityChecker._bounds_overlap_dimensions(proposal["bounds"], other_bounds)
            if width > 1 and height > 1:
                safe = False
                break
        if safe:
            accepted.append(proposal)
    if not accepted:
        return content, unresolved
    repaired = set()
    for proposal in accepted:
        left, top, right, bottom = proposal["bounds"]
        values = (left, top, right - left, bottom - top)
        proposal["group"].set("data-pptx-bounds", " ".join(str(value) for value in values))
        repaired.update(proposal["errors"])
    ET.register_namespace("", _SVG_NS)
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    return ET.tostring(root, encoding="unicode"), [error for index, error in enumerate(errors) if index not in repaired]
