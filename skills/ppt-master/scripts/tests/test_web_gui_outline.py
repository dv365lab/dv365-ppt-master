#!/usr/bin/env python3
"""PPT Master - Web GUI Outline Planner Tests

Verify source coverage, page constraints, grounded notes, and bounded model repair.

Usage:
    python3 -m unittest tests.test_web_gui_outline

Examples:
    python3 -m unittest tests.test_web_gui_outline

Dependencies:
    None (only uses standard library)
"""

import json
import sys
import threading
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from web_gui.outline import OutlineGenerationError, plan_outline  # noqa: E402


def make_outline(count=3, refs=(), title="Verification workflow"):
    slides = []
    for index in range(count):
        slide_type = "title" if index == 0 else "conclusion" if index == count - 1 else "content"
        slides.append({
            "page_number": index + 1, "title": f"Verification step {index + 1}",
            "slide_type": slide_type,
            "content_points": ["Stimulus exercises the design and coverage records what was exercised."],
            "description": "Explain how this verification activity supports the workflow.",
            "notes": "Explain how stimulus exercises the design. Coverage records what was exercised, "
                     "which prepares the next discussion of verification progress.",
            "source_refs": list(refs),
        })
    return {"title": title, "total_pages": count, "page_count_reason": "Each page explains one workflow step.",
            "slides": slides, "metadata": {"chunks_processed": 999}}


STRUCTURE = {
    "title": "Verification workflow", "type": "technical report", "sections": ["Verification"],
    "key_concepts": ["stimulus", "coverage"], "language": "en", "complexity": "medium",
    "key_data": [], "main_conclusions": ["Coverage records exercised behavior."],
}


class FakeClient:
    def __init__(self, replies, callback=None):
        self.replies = list(replies)
        self.calls = []
        self.callback = callback

    def complete(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user, **kwargs})
        if self.callback:
            self.callback()
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return json.dumps(reply) if isinstance(reply, dict) else reply


class FakeProviderError(RuntimeError):
    def __init__(self, message, *, recoverable):
        super().__init__(message)
        self.recoverable = recoverable


class OutlinePlannerTests(unittest.TestCase):
    def setUp(self):
        self.brief = {"topic": "Verification workflow", "page_count_mode": "fixed", "slides": 3,
                      "style": "Concise", "format": "ppt169", "analysis_depth": "normal", "notes": True}
        self.cancel = threading.Event()
        self.events = []

    def progress(self, stage, message, current=0, total=0):
        self.events.append((stage, message, current, total))

    def run_plan(self, replies, chunks=(), **brief):
        client = FakeClient(replies)
        result = plan_outline(list(chunks), {**self.brief, **brief}, client, self.progress, self.cancel)
        return result, client

    def test_every_chunk_updates_full_outline_including_last(self):
        chunks = [{"id": f"c{i}", "source": f"source{i}.md", "text": f"Unique source fact {i}"}
                  for i in range(1, 4)]
        replies = [STRUCTURE] + [make_outline(refs=[f"c{j}" for j in range(1, i + 1)],
                                             title=f"Revision {i}") for i in range(1, 4)]
        result, client = self.run_plan(replies, chunks)
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(result["title"], "Revision 3")
        self.assertEqual(result["metadata"]["chunks_processed"], 3)
        self.assertEqual(result["metadata"]["source_names"], ["source1.md", "source2.md", "source3.md"])
        self.assertIn("Unique source fact 3", client.calls[-1]["user"])
        self.assertIn("Revision 2", client.calls[-1]["user"])
        self.assertEqual({ref for slide in result["slides"] for ref in slide["source_refs"]}, {"c1", "c2", "c3"})

    def test_topic_only_uses_topic_prompt_and_real_zero_coverage(self):
        result, client = self.run_plan([make_outline()])
        self.assertEqual(len(client.calls), 1)
        self.assertIn("[phase:topic_outline]", client.calls[0]["user"])
        self.assertEqual(result["metadata"]["chunks_processed"], 0)
        self.assertEqual(result["metadata"]["source_names"], [])

    def test_fixed_range_and_auto_page_constraints_are_checked(self):
        for settings, invalid_count, valid_count in [
            ({"page_count_mode": "fixed", "slides": 2}, 3, 2),
            ({"page_count_mode": "range", "min_pages": 3, "max_pages": 4}, 5, 4),
            ({"page_count_mode": "auto", "slides": None}, 2, 5),
        ]:
            with self.subTest(settings=settings):
                result, client = self.run_plan([make_outline(invalid_count), make_outline(valid_count)], **settings)
                self.assertEqual(result["total_pages"], valid_count)
                self.assertIn("[phase:error_recovery]", client.calls[1]["user"])
                self.assertIn("page", client.calls[1]["user"].lower())

    def test_single_page_is_allowed_without_forcing_extra_structural_pages(self):
        result, _ = self.run_plan([make_outline(1)], slides=1)
        self.assertEqual(result["total_pages"], 1)

    def test_malformed_output_uses_recovery_then_structured_repair(self):
        result, client = self.run_plan(["not JSON", "```json\n{broken}\n```", make_outline()])
        self.assertEqual(result["total_pages"], 3)
        self.assertEqual(len(client.calls), 3)
        self.assertIn("[phase:error_recovery]", client.calls[1]["user"])
        self.assertIn("[phase:json_repair]", client.calls[2]["user"])
        self.assertIn("JSON", client.calls[2]["user"])

    def test_refinement_failure_retains_valid_debug_snapshot_and_never_skips_chunk(self):
        chunks = [{"id": f"c{i}", "source": "report.md", "text": f"Source fact {i}"}
                  for i in range(1, 4)]
        initial = make_outline(refs=["c1"], title="Last valid")
        client = FakeClient([STRUCTURE, initial, "broken", "broken", "broken"])
        with self.assertRaises(OutlineGenerationError) as caught:
            plan_outline(chunks, self.brief, client, self.progress, self.cancel)
        self.assertEqual(len(client.calls), 5)
        self.assertEqual(caught.exception.last_valid_outline["title"], "Last valid")
        self.assertEqual(caught.exception.last_valid_outline["metadata"]["chunks_processed"], 1)
        self.assertIn("c2", str(caught.exception))

    def test_substantive_notes_are_required_and_repaired(self):
        invalid = make_outline()
        invalid["slides"][1]["notes"] = ""
        result, client = self.run_plan([invalid, make_outline()])
        self.assertTrue(all(slide["notes"] for slide in result["slides"]))
        self.assertIn("notes", client.calls[1]["user"])

    def test_pragmatic_aliases_string_numbers_and_point_objects(self):
        outline = make_outline(2)
        outline["slides"][0].update(page_number="1", slide_type="cover",
                                     content_points=[{"text": "Exercise the design with controlled stimulus."}])
        outline["slides"][1].update(page_number="2", slide_type="summary")
        outline["total_pages"] = "2"
        outline["pages"] = outline.pop("slides")
        result, client = self.run_plan([outline], slides=2)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["slides"][0]["slide_type"], "title")
        self.assertEqual(result["slides"][1]["slide_type"], "conclusion")
        self.assertIsInstance(result["slides"][0]["content_points"][0], str)

    def test_source_references_must_cover_current_chunk_and_known_ids(self):
        chunks = [{"id": "c1", "source": "report.md", "text": "Coverage measures exercised behavior."}]
        result, client = self.run_plan([STRUCTURE, make_outline(refs=["invented"]),
                                        make_outline(refs=["c1"])], chunks)
        self.assertIn("invented", client.calls[-1]["user"])
        self.assertEqual(result["metadata"]["chunks_processed"], 1)

    def test_cancellation_stops_before_or_after_an_api_call(self):
        self.cancel.set()
        client = FakeClient([make_outline()])
        with self.assertRaises(InterruptedError):
            plan_outline([], self.brief, client, self.progress, self.cancel)
        self.assertFalse(client.calls)
        self.cancel.clear()
        client = FakeClient([make_outline()], callback=self.cancel.set)
        with self.assertRaises(InterruptedError):
            plan_outline([], self.brief, client, self.progress, self.cancel)
        self.assertEqual(len(client.calls), 1)

    def test_duplicate_chunk_ids_and_empty_chunks_fail_before_api(self):
        for chunks in ([{"id": "c1", "source": "a.md", "text": ""}],
                       [{"id": "c1", "source": "a.md", "text": "one"},
                        {"id": "c1", "source": "a.md", "text": "two"}]):
            client = FakeClient([])
            with self.assertRaises(ValueError):
                plan_outline(chunks, self.brief, client, self.progress, self.cancel)
            self.assertFalse(client.calls)

    def test_float_page_numbers_and_totals_need_repair(self):
        outline = make_outline()
        outline["total_pages"] = 3.0
        outline["slides"][0]["page_number"] = 1.0
        _, client = self.run_plan([outline, make_outline()])
        self.assertEqual(len(client.calls), 2)
        self.assertIn("total_pages", client.calls[1]["user"])
        self.assertIn("page_number", client.calls[1]["user"])

    def test_structure_recovery_never_accepts_a_fake_default_outline(self):
        chunks = [{"id": "c1", "source": "report.md", "text": "Coverage records exercised behavior."}]
        result, client = self.run_plan(["not JSON", STRUCTURE, make_outline(refs=["c1"])], chunks)
        self.assertEqual(result["metadata"]["chunks_processed"], 1)
        self.assertIn("[phase:error_recovery]", client.calls[1]["user"])
        self.assertIn("main_conclusions", client.calls[1]["user"])

    def test_recoverable_provider_exhaustion_enters_active_recovery(self):
        result, client = self.run_plan([
            FakeProviderError("upstream timeout after retries", recoverable=True), make_outline(),
        ])
        self.assertEqual(result["total_pages"], 3)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("[phase:error_recovery]", client.calls[1]["user"])
        self.assertIn("upstream timeout after retries", client.calls[1]["user"])

    def test_nonrecoverable_auth_error_stops_without_repeating_request(self):
        client = FakeClient([FakeProviderError("authentication failed", recoverable=False)])
        with self.assertRaises(OutlineGenerationError) as caught:
            plan_outline([], self.brief, client, self.progress, self.cancel)
        self.assertEqual(len(client.calls), 1)
        self.assertIn("authentication failed", str(caught.exception))

    def test_raised_cancellation_never_retries(self):
        client = FakeClient([InterruptedError("cancelled while waiting")])
        with self.assertRaises(InterruptedError):
            plan_outline([], self.brief, client, self.progress, self.cancel)
        self.assertEqual(len(client.calls), 1)

    def test_large_deck_has_adaptive_output_token_budget(self):
        _, client = self.run_plan([make_outline(40)], slides=40)
        self.assertGreaterEqual(client.calls[0]["max_tokens"], 16000)
        self.assertLessEqual(client.calls[0]["max_tokens"], 24000)

    def test_public_progress_distinguishes_structure_initial_and_refinement(self):
        chunks = [{"id": f"c{i}", "source": "report.md", "text": f"Source fact {i}"} for i in (1, 2)]
        self.run_plan([STRUCTURE, make_outline(refs=["c1"]), make_outline(refs=["c1", "c2"])], chunks)
        stages = {event[0] for event in self.events}
        self.assertTrue({"analyzing_structure", "initial_outline", "refining_outline"} <= stages)

    def test_failed_repair_exposes_last_invalid_response_and_validation_errors(self):
        invalid = make_outline()
        invalid["slides"][1]["notes"] = ""
        client = FakeClient([invalid, invalid, invalid])
        with self.assertRaises(OutlineGenerationError) as caught:
            plan_outline([], self.brief, client, self.progress, self.cancel)
        self.assertEqual(json.loads(caught.exception.invalid_output), invalid)
        self.assertTrue(any("notes" in error for error in caught.exception.validation_errors))
        repair_events = [event for event in self.events if event[0] == "outline_repair"]
        self.assertEqual(len(repair_events), 2)
        self.assertTrue(all("notes" in event[1] for event in repair_events))

    def test_repair_progress_bounds_error_details_without_losing_diagnostics(self):
        invalid = make_outline(40)
        for slide in invalid["slides"]:
            slide["notes"] = ""
            slide["description"] = ""
        client = FakeClient([invalid, invalid, invalid])
        with self.assertRaises(OutlineGenerationError) as caught:
            plan_outline([], {**self.brief, "slides": 40}, client, self.progress, self.cancel)
        self.assertGreater(len(caught.exception.validation_errors), 40)
        self.assertTrue(all(len(event[1]) <= 1000 for event in self.events if event[0] == "outline_repair"))

    def test_provider_error_details_are_not_added_to_repair_progress(self):
        self.run_plan([FakeProviderError("raw transport detail", recoverable=True), make_outline()])
        repair_events = [event for event in self.events if event[0] == "outline_repair"]
        self.assertTrue(repair_events)
        self.assertTrue(all("raw transport detail" not in event[1] for event in repair_events))

    def test_planning_progress_is_localized_for_the_existing_gui(self):
        self.run_plan([make_outline()])
        self.assertTrue(all(any("\u4e00" <= char <= "\u9fff" for char in event[1]) for event in self.events))

    def test_structure_outline_and_repair_request_native_json(self):
        chunks = [{"id": "c1", "source": "report.md", "text": "Coverage records exercised behavior."}]
        _, client = self.run_plan([STRUCTURE, "invalid output", make_outline(refs=["c1"])], chunks)
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(all(call.get("json_mode") is True for call in client.calls))

    def test_unprovided_cover_year_triggers_recovery_even_when_created_at_contains_it(self):
        invalid = make_outline()
        invalid["slides"][0]["content_points"].append("2026")
        _, client = self.run_plan([invalid, make_outline()], created_at="2026-10-11T12:00:00+08:00")
        self.assertEqual(len(client.calls), 2)
        self.assertIn("[phase:error_recovery]", client.calls[1]["user"])
        self.assertIn("cover date", client.calls[1]["user"])

    def test_explicit_cover_date_survives(self):
        outline = make_outline()
        outline["slides"][0]["content_points"].append("2026-10-11")
        result, client = self.run_plan([outline], topic="Verification workshop, 2026年10月11日")
        self.assertEqual(len(client.calls), 1)
        self.assertIn("2026-10-11", result["slides"][0]["content_points"])

    def test_cover_date_can_be_grounded_in_previously_processed_source_text(self):
        chunks = [{"id": "c1", "source": "report.md", "text": "Workshop date: 2026-10-11."},
                  {"id": "c2", "source": "appendix.md", "text": "Coverage records exercised behavior."}]
        refined = make_outline(refs=["c1", "c2"])
        refined["slides"][0]["content_points"].append("2026")
        result, client = self.run_plan([STRUCTURE, make_outline(refs=["c1"]), refined], chunks)
        self.assertEqual(len(client.calls), 3)
        self.assertIn("2026", result["slides"][0]["content_points"])

    def test_arrow_only_points_trigger_repair_but_equations_remain_meaningful(self):
        invalid = make_outline()
        invalid["slides"][1]["content_points"].append("↑ ↓")
        valid = make_outline()
        valid["slides"][1]["content_points"].append("A → B")
        result, client = self.run_plan([invalid, valid])
        self.assertEqual(len(client.calls), 2)
        self.assertIn("decorative arrows", client.calls[1]["user"])
        self.assertIn("A → B", result["slides"][1]["content_points"])
