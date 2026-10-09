"""Tests for follow-up capture and date resolution.

Run from the project root:
    python -m unittest discover -s tests -t .
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.domain.follow_up.models import (  # noqa: E402
    CONDITIONAL_ONLY as FOLLOW_UP_CONDITIONAL,
)
from app.domain.follow_up.models import NOT_DISCUSSED as FOLLOW_UP_NONE  # noqa: E402
from app.domain.follow_up.models import SCHEDULED as FOLLOW_UP_SCHEDULED  # noqa: E402
from app.domain.follow_up.resolver import resolve_follow_up  # noqa: E402
from app.domain.transcripts import Transcript  # noqa: E402
from app.pipeline.layers import LAYERS  # noqa: E402
from app.pipeline.prompts import render_prompt  # noqa: E402
from app.pipeline.results import PatientContext  # noqa: E402
from app.pipeline.runner import build_contexts  # noqa: E402

PROMPTS_DIR = get_settings().llm.prompts_dir

CALL_DATE = "2026-08-22"


def scheduled(min_value=None, max_value=None, unit=None, ttype="relative",
              absolute_date=None, approximate=False, commitment="definite",
              stated="come back for a review", stated_by="doctor",
              purpose="review progress", triggers=None):
    """Build a Layer 1 output dict with a follow_up block."""
    return {
        "follow_up": {
            "scheduled_visit": {
                "stated": stated,
                "commitment": commitment,
                "timing": {
                    "type": ttype,
                    "min_value": min_value,
                    "max_value": max_value,
                    "unit": unit,
                    "absolute_date": absolute_date,
                    "approximate": approximate,
                },
                "purpose": purpose,
                "stated_by": stated_by,
            },
            "return_earlier_if": triggers if triggers is not None else [],
        }
    }


class TestCorpusTimings(unittest.TestCase):
    """The five timing patterns that actually appear in the 16 transcripts."""

    def test_two_weeks(self):
        r = resolve_follow_up(scheduled(2, 2, "week"), CALL_DATE)
        self.assertEqual(r.type, FOLLOW_UP_SCHEDULED)
        self.assertEqual(r.due_earliest, "2026-09-05")
        self.assertEqual(r.due_latest, "2026-09-05")
        self.assertEqual(r.days_from_call, 14)
        self.assertEqual(r.window_label, "2 weeks")
        self.assertEqual(r.basis, "relative")
        self.assertEqual(r.confidence, "high")
        self.assertFalse(r.needs_human_confirmation)

    def test_three_weeks(self):
        r = resolve_follow_up(scheduled(3, 3, "week"), CALL_DATE)
        self.assertEqual(r.due_earliest, "2026-09-12")
        self.assertEqual(r.days_from_call, 21)

    def test_four_weeks(self):
        r = resolve_follow_up(scheduled(4, 4, "week"), CALL_DATE)
        self.assertEqual(r.due_earliest, "2026-09-19")
        self.assertEqual(r.days_from_call, 28)

    def test_six_weeks(self):
        r = resolve_follow_up(scheduled(6, 6, "week"), CALL_DATE)
        self.assertEqual(r.due_earliest, "2026-10-03")
        self.assertEqual(r.days_from_call, 42)

    def test_riya_6_range_hedged_and_mislabelled_speaker(self):
        """'about three to four months, or sooner if your cycles fluctuate',
        spoken under a PATIENT: label because riya_6's speakers are swapped."""
        data = scheduled(
            3, 4, "month", ttype="range", approximate=True,
            commitment="patient_discretion",
            stated="Please drop in for a routine check-in in about three to four months",
            stated_by="patient",
            triggers=[{
                "stated": "or sooner if you ever feel your cycles are fluctuating",
                "condition": "cycles feel like they are fluctuating",
                "action": "contact_clinic",
                "urgency": "routine",
            }],
        )
        r = resolve_follow_up(data, CALL_DATE)

        self.assertEqual(r.type, FOLLOW_UP_SCHEDULED)
        self.assertEqual(r.basis, "range")
        self.assertEqual(r.due_earliest, "2026-11-22")
        self.assertEqual(r.due_latest, "2026-12-22")
        self.assertEqual(r.days_from_call, 92)          # measured off the earliest
        self.assertEqual(r.window_label, "about 3 to 4 months")
        self.assertTrue(r.approximate)
        self.assertTrue(r.needs_human_confirmation)
        self.assertEqual(len(r.come_sooner_if), 1)
        self.assertTrue(any("speaker labels may be swapped" in w for w in r.warnings))


class TestTimingTypes(unittest.TestCase):

    def test_absolute_date(self):
        r = resolve_follow_up(
            scheduled(ttype="absolute", absolute_date="2026-03-15"), "2026-03-01")
        self.assertEqual(r.basis, "absolute")
        self.assertEqual(r.due_earliest, "2026-03-15")
        self.assertEqual(r.due_latest, "2026-03-15")
        self.assertEqual(r.days_from_call, 14)
        self.assertEqual(r.confidence, "high")

    def test_absolute_with_unusable_date_degrades(self):
        r = resolve_follow_up(
            scheduled(ttype="absolute", absolute_date="next March"), CALL_DATE)
        self.assertIsNone(r.due_earliest)
        self.assertEqual(r.basis, "unspecified")
        self.assertTrue(any("absolute_date was unusable" in w for w in r.warnings))

    def test_days_unit(self):
        r = resolve_follow_up(scheduled(10, 10, "days"), CALL_DATE)
        self.assertEqual(r.due_earliest, "2026-09-01")
        self.assertEqual(r.window_label, "10 days")

    def test_month_arithmetic_is_calendar_correct(self):
        """relativedelta clamps to the end of a short month; +30 days would not."""
        r = resolve_follow_up(scheduled(1, 1, "month"), "2026-01-31")
        self.assertEqual(r.due_earliest, "2026-02-28")

    def test_singular_unit_label(self):
        r = resolve_follow_up(scheduled(1, 1, "week"), CALL_DATE)
        self.assertEqual(r.window_label, "1 week")

    def test_unit_case_and_plural_tolerated(self):
        for unit in ("Weeks", "WEEK", "weeks", "week"):
            r = resolve_follow_up(scheduled(3, 3, unit), CALL_DATE)
            self.assertEqual(r.due_earliest, "2026-09-12", unit)

    def test_number_words_tolerated(self):
        r = resolve_follow_up(scheduled("three", "three", "week"), CALL_DATE)
        self.assertEqual(r.due_earliest, "2026-09-12")

    def test_numeric_strings_tolerated(self):
        r = resolve_follow_up(scheduled("3", "3", "week"), CALL_DATE)
        self.assertEqual(r.due_earliest, "2026-09-12")

    def test_missing_max_falls_back_to_min(self):
        r = resolve_follow_up(scheduled(3, None, "week"), CALL_DATE)
        self.assertEqual(r.due_earliest, "2026-09-12")
        self.assertEqual(r.due_latest, "2026-09-12")
        self.assertEqual(r.basis, "relative")

    def test_reversed_range_is_swapped_and_flagged(self):
        r = resolve_follow_up(scheduled(4, 2, "week", ttype="range"), CALL_DATE)
        self.assertEqual(r.due_earliest, "2026-09-05")
        self.assertEqual(r.due_latest, "2026-09-19")
        self.assertTrue(any("swapped" in w for w in r.warnings))

    def test_range_declared_relative_still_detected(self):
        """basis follows the values, not the model's label."""
        r = resolve_follow_up(scheduled(3, 4, "month", ttype="relative"), CALL_DATE)
        self.assertEqual(r.basis, "range")

    def test_unusable_unit_degrades(self):
        r = resolve_follow_up(scheduled(3, 3, "fortnights"), CALL_DATE)
        self.assertIsNone(r.due_earliest)
        self.assertEqual(r.type, FOLLOW_UP_NONE)
        self.assertTrue(any("unusable" in w for w in r.warnings))


class TestCommitmentCases(unittest.TestCase):
    """The doctor's two phrasings: definite return vs. come-if-unwell."""

    def test_conditional_only_produces_no_date(self):
        data = scheduled(
            ttype="unspecified", commitment="conditional",
            stated="if you don't feel well, come and see me",
            triggers=[{
                "stated": "if you don't feel well, come and see me",
                "condition": "not feeling well",
                "action": "contact_clinic",
                "urgency": "routine",
            }],
        )
        r = resolve_follow_up(data, CALL_DATE)
        self.assertEqual(r.type, FOLLOW_UP_CONDITIONAL)
        self.assertIsNone(r.due_earliest)
        self.assertIsNone(r.days_from_call)
        self.assertEqual(len(r.come_sooner_if), 1)
        self.assertEqual(r.confidence, "high")
        self.assertFalse(r.needs_human_confirmation)

    def test_definite_without_timing_is_flagged(self):
        r = resolve_follow_up(
            scheduled(ttype="unspecified", commitment="definite"), CALL_DATE)
        self.assertNotEqual(r.type, FOLLOW_UP_SCHEDULED)
        self.assertTrue(r.needs_human_confirmation)
        self.assertEqual(r.confidence, "low")
        self.assertTrue(any("needs confirming" in w for w in r.warnings))

    def test_not_discussed(self):
        r = resolve_follow_up(
            scheduled(ttype="unspecified", commitment="not_discussed",
                      stated=None, purpose=None), CALL_DATE)
        self.assertEqual(r.type, FOLLOW_UP_NONE)
        self.assertIsNone(r.due_earliest)
        self.assertTrue(any("not discussed" in w for w in r.warnings))

    def test_scheduled_plus_trigger_coexist(self):
        """16/16 transcripts have both; neither may displace the other."""
        data = scheduled(3, 3, "week", triggers=[{
            "stated": "If you notice any sudden flare-up, you can contact the clinic.",
            "condition": "sudden flare-up",
            "action": "contact_clinic",
            "urgency": "urgent",
        }])
        r = resolve_follow_up(data, CALL_DATE)
        self.assertEqual(r.type, FOLLOW_UP_SCHEDULED)
        self.assertEqual(r.due_earliest, "2026-09-12")
        self.assertEqual(r.come_sooner_if[0]["urgency"], "urgent")


class TestTriggerNormalisation(unittest.TestCase):

    def test_record_and_report_preserved(self):
        """riya_1's conditional is patient homework, not an escalation."""
        data = scheduled(3, 3, "week", triggers=[{
            "stated": "If your period begins during this time, note down the flow",
            "condition": "period begins before the visit",
            "action": "record_and_report",
            "urgency": "routine",
        }])
        r = resolve_follow_up(data, CALL_DATE)
        self.assertEqual(r.come_sooner_if[0]["action"], "record_and_report")
        self.assertEqual(r.warnings, [])

    def test_unknown_action_kept_but_flagged(self):
        data = scheduled(3, 3, "week", triggers=[
            {"condition": "x", "action": "send_carrier_pigeon", "urgency": "routine"}])
        r = resolve_follow_up(data, CALL_DATE)
        self.assertEqual(r.come_sooner_if[0]["action"], "send_carrier_pigeon")
        self.assertTrue(any("unrecognised follow-up action" in w for w in r.warnings))

    def test_bare_string_trigger_accepted(self):
        data = scheduled(3, 3, "week", triggers=["if the rash spreads, call us"])
        r = resolve_follow_up(data, CALL_DATE)
        self.assertEqual(r.come_sooner_if[0]["condition"], "if the rash spreads, call us")
        self.assertEqual(r.come_sooner_if[0]["action"], "contact_clinic")

    def test_single_dict_trigger_wrapped(self):
        data = scheduled(3, 3, "week", triggers={"condition": "flare-up"})
        r = resolve_follow_up(data, CALL_DATE)
        self.assertEqual(len(r.come_sooner_if), 1)

    def test_non_list_trigger_flagged(self):
        data = scheduled(3, 3, "week", triggers=42)
        r = resolve_follow_up(data, CALL_DATE)
        self.assertEqual(r.come_sooner_if, [])
        self.assertTrue(any("not a list" in w for w in r.warnings))


class TestMalformedInput(unittest.TestCase):
    """A bad extraction must degrade, not raise."""

    def test_missing_follow_up_block(self):
        r = resolve_follow_up({"chief_complaints": []}, CALL_DATE)
        self.assertEqual(r.type, FOLLOW_UP_NONE)
        self.assertTrue(any("no usable 'follow_up' block" in w for w in r.warnings))

    def test_layer_1_not_a_dict(self):
        r = resolve_follow_up("the model returned prose", CALL_DATE)
        self.assertEqual(r.type, FOLLOW_UP_NONE)

    def test_layer_1_none(self):
        r = resolve_follow_up(None, CALL_DATE)
        self.assertEqual(r.type, FOLLOW_UP_NONE)

    def test_bad_call_date_is_flagged_not_fatal(self):
        r = resolve_follow_up(scheduled(3, 3, "week"), "22/08/2026")
        self.assertIsNone(r.due_earliest)
        self.assertEqual(r.window_label, "3 weeks")   # phrase still captured
        self.assertTrue(any("not an ISO date" in w for w in r.warnings))

    def test_missing_call_date(self):
        r = resolve_follow_up(scheduled(3, 3, "week"), None)
        self.assertIsNone(r.due_earliest)
        self.assertTrue(any("not an ISO date" in w for w in r.warnings))

    def test_unrecognised_timing_type_flagged(self):
        r = resolve_follow_up(scheduled(3, 3, "week", ttype="whenever"), CALL_DATE)
        self.assertTrue(any("unrecognised timing.type" in w for w in r.warnings))

    def test_nulls_throughout_do_not_raise(self):
        r = resolve_follow_up({"follow_up": {"scheduled_visit": None,
                                             "return_earlier_if": None}}, CALL_DATE)
        self.assertEqual(r.type, FOLLOW_UP_NONE)

    def test_to_dict_is_json_serialisable(self):
        r = resolve_follow_up(scheduled(3, 3, "week"), CALL_DATE)
        self.assertIsInstance(json.dumps(r.to_dict()), str)


class TestPromptWiring(unittest.TestCase):

    def setUp(self):
        self.transcript = Transcript(
            path=Path("transcripts/8a282c28.txt"),
            relative_path="transcripts/8a282c28.txt",
            text="DOCTOR: hello")
        self.patient = PatientContext(name="Riya", age=28, gender="Female",
                                      call_date=CALL_DATE)

    def test_days_until_next_visit_is_gone(self):
        for context in build_contexts(self.transcript, self.patient).values():
            self.assertNotIn("days_until_next_visit", context)

    def test_patient_context_rejects_removed_kwarg(self):
        with self.assertRaises(TypeError):
            PatientContext(name="Riya", age=28, gender="Female",
                           call_date=CALL_DATE, days_until_next_visit=21)

    def test_layer_3_renders_with_resolved_follow_up(self):
        resolved = resolve_follow_up(scheduled(3, 3, "week"), CALL_DATE)
        context = dict(build_contexts(self.transcript, self.patient)[3])
        context["layer_1_output"] = "{}"
        context["layer_2_output"] = "{}"
        context["follow_up_resolved"] = json.dumps(resolved.to_dict(), indent=2)

        prompt = render_prompt(LAYERS[3].template, context, PROMPTS_DIR)
        self.assertIn("2026-09-12", prompt)
        self.assertIn("- Call Date: 2026-08-22", prompt)
        self.assertNotIn("{{", prompt)

    def test_layer_3_missing_follow_up_var_raises(self):
        context = dict(build_contexts(self.transcript, self.patient)[3])
        context["layer_1_output"] = "{}"
        context["layer_2_output"] = "{}"
        with self.assertRaises(Exception):
            render_prompt(LAYERS[3].template, context, PROMPTS_DIR)

    def test_layer_1_prompt_documents_follow_up_schema(self):
        context = build_contexts(self.transcript, self.patient)[1]
        prompt = render_prompt(LAYERS[1].template, context, PROMPTS_DIR)
        for token in ('"follow_up"', '"scheduled_visit"', '"return_earlier_if"',
                      '"commitment"', '"stated_by"'):
            self.assertIn(token, prompt)


class TestSummary(unittest.TestCase):

    def test_scheduled_summary(self):
        r = resolve_follow_up(scheduled(3, 3, "week"), CALL_DATE)
        self.assertEqual(r.summary(), "due 2026-09-12 (3 weeks)")

    def test_range_summary_flags_confirmation(self):
        r = resolve_follow_up(
            scheduled(3, 4, "month", ttype="range", approximate=True), CALL_DATE)
        self.assertEqual(
            r.summary(),
            "due 2026-11-22 to 2026-12-22 (about 3 to 4 months) [confirm]")

    def test_conditional_summary(self):
        r = resolve_follow_up(
            scheduled(ttype="unspecified", commitment="conditional",
                      triggers=[{"condition": "unwell"}]), CALL_DATE)
        self.assertEqual(r.summary(), "no fixed date; 1 return trigger(s)")

    def test_not_discussed_summary(self):
        r = resolve_follow_up({}, CALL_DATE)
        self.assertEqual(r.summary(), "not discussed on this call")


if __name__ == "__main__":
    unittest.main(verbosity=2)
