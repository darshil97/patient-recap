"""
Follow-up tracking rules: what gets queued, what preserves a human decision,
and how scheduling and cancellation move rows between states.

Offline: the repository is mocked, so no database is touched.

Run from backend/:
    python -m unittest discover -s tests -t .
"""

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.errors import NotFoundError, ValidationError  # noqa: E402
from app.db.models import VisitRow  # noqa: E402
from app.domain.follow_up.models import (  # noqa: E402
    CONDITIONAL_ONLY,
    NOT_DISCUSSED,
    SCHEDULED,
    ResolvedFollowUp,
)
from app.services import follow_up_service  # noqa: E402

VISIT_ID = "22222222-2222-2222-2222-222222222222"
PATIENT_ID = "11111111-1111-1111-1111-111111111111"
FOLLOW_UP_ID = "44444444-4444-4444-4444-444444444444"
# The tenant every fixture here belongs to. Passed explicitly rather than
# defaulted, so these tests exercise the same scoped signatures the API uses.
DOCTOR_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def a_visit() -> VisitRow:
    return VisitRow(
        visit_id=VISIT_ID, patient_id=PATIENT_ID, visit_number=2,
        visit_date=date(2026, 8, 7), visit_time=None, visit_type="follow_up",
        visit_status="completed", transcript_path=f"transcripts/{VISIT_ID}.txt",
        transcript_provider="local", insights_generated=True,
        doctor_id=DOCTOR_ID, first_name="Vikas")


def scheduled_follow_up(**over) -> ResolvedFollowUp:
    fields = dict(type=SCHEDULED, due_earliest="2026-08-28",
                  due_latest="2026-08-28", needs_human_confirmation=False,
                  stated="Come back for a review in three weeks.",
                  purpose="monitor reflux response")
    fields.update(over)
    return ResolvedFollowUp(**fields)


class TestSyncFromInsights(unittest.TestCase):
    """Only dated follow-ups belong in a due-date queue."""

    def setUp(self) -> None:
        patcher = mock.patch.object(follow_up_service, "follow_ups_repo")
        self.repo = patcher.start()
        self.addCleanup(patcher.stop)
        self.repo.upsert.return_value = FOLLOW_UP_ID
        self.repo.delete_for_visit.return_value = 0
        self.conn = mock.MagicMock()

    def test_scheduled_is_upserted(self):
        result = follow_up_service.sync_from_insights(
            self.conn, DOCTOR_ID, a_visit(), scheduled_follow_up())
        self.assertEqual(result, FOLLOW_UP_ID)
        kwargs = self.repo.upsert.call_args.kwargs
        self.assertEqual(kwargs["source_visit_id"], VISIT_ID)
        self.assertEqual(kwargs["patient_id"], PATIENT_ID)
        self.assertEqual(kwargs["due_earliest"], "2026-08-28")
        self.assertEqual(kwargs["purpose"], "monitor reflux response")
        self.repo.delete_for_visit.assert_not_called()
        # The scope reaches the repository positionally, which is what makes the
        # write land in the right doctor's queue.
        self.assertEqual(self.repo.upsert.call_args.args[1], DOCTOR_ID)

    def test_needs_confirmation_is_mirrored(self):
        follow_up_service.sync_from_insights(
            self.conn, DOCTOR_ID, a_visit(),
            scheduled_follow_up(needs_human_confirmation=True))
        self.assertTrue(self.repo.upsert.call_args.kwargs["needs_confirmation"])

    def test_conditional_only_is_not_queued(self):
        """It has no date, so it cannot be sorted into a due-date list. Its
        return triggers render on the visit brief instead."""
        follow_up_service.sync_from_insights(
            self.conn, DOCTOR_ID, a_visit(),
            ResolvedFollowUp(type=CONDITIONAL_ONLY, due_earliest=None))
        self.repo.upsert.assert_not_called()
        self.repo.delete_for_visit.assert_called_once_with(
            self.conn, DOCTOR_ID, VISIT_ID)

    def test_not_discussed_is_not_queued(self):
        follow_up_service.sync_from_insights(
            self.conn, DOCTOR_ID, a_visit(), ResolvedFollowUp(type=NOT_DISCUSSED))
        self.repo.upsert.assert_not_called()
        self.repo.delete_for_visit.assert_called_once()

    def test_none_is_not_queued(self):
        follow_up_service.sync_from_insights(self.conn, DOCTOR_ID, a_visit(), None)
        self.repo.upsert.assert_not_called()
        self.repo.delete_for_visit.assert_called_once()

    def test_scheduled_without_a_date_is_not_queued(self):
        """Defensive: the type says scheduled but there is nothing to sort by."""
        follow_up_service.sync_from_insights(
            self.conn, DOCTOR_ID, a_visit(), scheduled_follow_up(due_earliest=None))
        self.repo.upsert.assert_not_called()
        self.repo.delete_for_visit.assert_called_once()

    def test_regeneration_that_drops_the_follow_up_clears_the_stale_row(self):
        self.repo.delete_for_visit.return_value = 1
        follow_up_service.sync_from_insights(
            self.conn, DOCTOR_ID, a_visit(), ResolvedFollowUp(type=NOT_DISCUSSED))
        self.repo.delete_for_visit.assert_called_once_with(
            self.conn, DOCTOR_ID, VISIT_ID)


class TestUpsertPreservesHumanDecisions(unittest.TestCase):
    """The doctor's decision outranks a model rerun."""

    @staticmethod
    def upsert_sql() -> str:
        """The upsert statement, read from the function that runs it.

        Previously read a module-level `_UPSERT` constant. That constant is gone:
        the statement now has to resolve its patient_id from a doctor-scoped
        SELECT on `patients` rather than from a VALUES list, so the SQL and its
        tenancy filter are one inseparable thing and live inside upsert() itself.
        Reading the source keeps this assertion pointed at the SQL that actually
        executes instead of a copy that could drift from it.
        """
        import inspect

        from app.db.repositories import follow_ups as repo

        return inspect.getsource(repo.upsert)

    @classmethod
    def assignments(cls) -> str:
        """The DO UPDATE clause with whitespace collapsed.

        The SQL aligns its `=` signs for readability, so matching on
        "column =" needs normalising or the assertions pass vacuously.
        """
        import re

        clause = cls.upsert_sql().split("DO UPDATE SET")[1].split("RETURNING")[0]
        return re.sub(r"\s+", " ", clause)

    def test_do_update_list_excludes_human_fields(self):
        updated = self.assignments()
        for protected in ("status", "resolved_at", "resolved_reason",
                          "resolved_by_visit_id", "dismissed_at",
                          "dismissed_reason", "last_contacted_at", "note"):
            self.assertNotIn(f"{protected} =", updated,
                             f"{protected} must survive regeneration")

    def test_do_update_list_refreshes_model_fields(self):
        updated = self.assignments()
        for refreshed in ("due_earliest", "due_latest", "needs_confirmation",
                          "stated", "purpose"):
            self.assertIn(f"{refreshed} =", updated,
                          f"{refreshed} should be refreshed by a rerun")

    def test_conflict_target_is_the_source_visit(self):
        self.assertIn("ON CONFLICT (source_visit_id)", self.upsert_sql())

    def test_upsert_resolves_its_patient_under_the_doctor_filter(self):
        """The insert must not take patient_id on trust.

        Reading it from `patients` under the doctor filter is what stops a
        mismatched (visit, patient) pair planting a queue entry — complete with the
        patient's phone number — in another doctor's follow-up list.
        """
        sql = self.upsert_sql()
        self.assertIn("FROM patients p", sql)
        self.assertIn("p.doctor_id = %(scope_doctor_id)s", sql)
        # patient_id comes from the scoped row, not from the parameter.
        self.assertIn("SELECT %(source_visit_id)s, p.patient_id", sql)


class TestUpdateFollowUp(unittest.TestCase):

    def setUp(self) -> None:
        patcher = mock.patch.object(follow_up_service, "follow_ups_repo")
        self.repo = patcher.start()
        self.addCleanup(patcher.stop)
        self.repo.get.return_value = mock.MagicMock(status="open")
        self.repo.apply_update.return_value = mock.MagicMock(status="dismissed")
        self.conn = mock.MagicMock()

    def test_resolved_is_rejected(self):
        """Resolution is caused by scheduling a visit. Setting it directly would
        create resolved rows with no visit behind them."""
        with self.assertRaises(ValidationError) as ctx:
            follow_up_service.update_follow_up(self.conn, DOCTOR_ID,
                                               FOLLOW_UP_ID, status="resolved")
        self.assertIn("schedule a visit", str(ctx.exception).lower())
        self.repo.apply_update.assert_not_called()

    def test_unknown_status_rejected(self):
        with self.assertRaises(ValidationError):
            follow_up_service.update_follow_up(self.conn, DOCTOR_ID,
                                               FOLLOW_UP_ID, status="archived")

    def test_dismiss_requires_a_reason(self):
        with self.assertRaises(ValidationError) as ctx:
            follow_up_service.update_follow_up(self.conn, DOCTOR_ID,
                                               FOLLOW_UP_ID, status="dismissed")
        self.assertIn("dismissed_reason", str(ctx.exception))

    def test_dismiss_with_blank_reason_rejected(self):
        with self.assertRaises(ValidationError):
            follow_up_service.update_follow_up(self.conn, DOCTOR_ID,
                                               FOLLOW_UP_ID, status="dismissed",
                                               dismissed_reason="   ")

    def test_dismiss_with_reason_accepted(self):
        follow_up_service.update_follow_up(
            self.conn, DOCTOR_ID, FOLLOW_UP_ID, status="dismissed",
            dismissed_reason="patient moved city")
        kwargs = self.repo.apply_update.call_args.kwargs
        self.assertEqual(kwargs["status"], "dismissed")
        self.assertEqual(kwargs["dismissed_reason"], "patient moved city")

    def test_reopen_accepted(self):
        follow_up_service.update_follow_up(self.conn, DOCTOR_ID, FOLLOW_UP_ID,
                                           status="open")
        self.assertEqual(self.repo.apply_update.call_args.kwargs["status"], "open")

    def test_mark_contacted_without_status(self):
        """"I called, they're still deciding" must leave the row open."""
        follow_up_service.update_follow_up(self.conn, DOCTOR_ID, FOLLOW_UP_ID,
                                           mark_contacted=True)
        kwargs = self.repo.apply_update.call_args.kwargs
        self.assertTrue(kwargs["mark_contacted"])
        self.assertIsNone(kwargs["status"])

    def test_unknown_follow_up_is_not_found(self):
        self.repo.get.return_value = None
        with self.assertRaises(NotFoundError):
            follow_up_service.update_follow_up(self.conn, DOCTOR_ID, FOLLOW_UP_ID,
                                               note="x")


class TestResolutionAndReopening(unittest.TestCase):

    def setUp(self) -> None:
        patcher = mock.patch.object(follow_up_service, "follow_ups_repo")
        self.repo = patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = mock.MagicMock()

    def test_scheduling_resolves_open_follow_ups(self):
        self.repo.resolve_open_for_patient.return_value = [FOLLOW_UP_ID]
        resolved = follow_up_service.resolve_for_new_visit(
            self.conn, DOCTOR_ID, PATIENT_ID, "new-visit", date(2026, 9, 1))
        self.assertEqual(resolved, [FOLLOW_UP_ID])
        self.repo.resolve_open_for_patient.assert_called_once_with(
            self.conn, DOCTOR_ID, PATIENT_ID, "new-visit", date(2026, 9, 1))

    def test_cancelling_reopens_what_it_resolved(self):
        self.repo.reopen_resolved_by_visit.return_value = [FOLLOW_UP_ID]
        reopened = follow_up_service.reopen_for_cancelled_visit(
            self.conn, DOCTOR_ID, VISIT_ID)
        self.assertEqual(reopened, [FOLLOW_UP_ID])

    def test_resolution_keys_on_source_visit_date_not_due_date(self):
        """Any open follow-up from an earlier visit is closed by booking the next
        one, whether or not the new date matches what was suggested."""
        from app.db.repositories.follow_ups import resolve_open_for_patient

        source = resolve_open_for_patient.__doc__ or ""
        self.assertIn("source visit", source.lower())

    def test_resolution_sql_compares_source_visit_date(self):
        import inspect

        from app.db.repositories import follow_ups as repo

        sql = inspect.getsource(repo.resolve_open_for_patient)
        self.assertIn("sv.visit_date    <= %(new_visit_date)s", sql)
        self.assertIn("resolved_reason      = 'visit_scheduled'", sql)
        # Must not resolve itself.
        self.assertIn("sv.visit_id      <> %(new_visit_id)s", sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
