"""
Endpoint tests using FastAPI's TestClient.

The database and the LLM are both stubbed, so these run offline and make no API
calls: the point is to check routing, validation, status codes and error mapping,
not the pipeline (which tests/test_follow_up.py covers).

Run from the project root:
    python -m unittest discover -s tests -t .
"""

import sys
import unittest
from datetime import date, time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main  # noqa: E402,F401  imported so mock.patch("app.main...") resolves
from app.core.errors import (  # noqa: E402
    ConflictError,
    DuplicatePatientError,
    NotFoundError,
    QuotaExhaustedError,
)
from app.db.models import JobRow, PatientRow, VisitRow  # noqa: E402

PATIENT_ID = "11111111-1111-1111-1111-111111111111"
VISIT_ID = "22222222-2222-2222-2222-222222222222"
JOB_ID = "33333333-3333-3333-3333-333333333333"
# Well-formed but not in the database, to separate 404 from 422.
ABSENT_ID = "99999999-9999-9999-9999-999999999999"
MALFORMED_ID = "not-a-uuid"


def a_patient(**over: Any) -> PatientRow:
    fields: Dict[str, Any] = dict(
        patient_id=PATIENT_ID, external_id="P-2026-00009", first_name="Riya",
        last_name="Sharma", date_of_birth=date(2001, 6, 11), gender="female",
        phone="+919876543210", email=None, age=25, visit_count=2,
        last_visit_date=date(2026, 7, 6))
    fields.update(over)
    return PatientRow(**fields)


def a_visit(**over: Any) -> VisitRow:
    fields: Dict[str, Any] = dict(
        visit_id=VISIT_ID, patient_id=PATIENT_ID, visit_number=1,
        visit_date=date(2026, 2, 16), visit_time=time(10, 30), visit_type="initial",
        visit_status="scheduled", transcript_path=None, transcript_provider=None,
        insights_generated=False, first_name="Riya", last_name="Sharma",
        external_id="P-2026-00009", gender="female", age_at_visit=24)
    fields.update(over)
    return VisitRow(**fields)


def a_job(**over: Any) -> JobRow:
    fields: Dict[str, Any] = dict(
        job_id=JOB_ID, visit_id=VISIT_ID, status="queued", layers_completed=0,
        stop_after=3, regenerate=False, model_name=None, insight_id=None,
        error_code=None, error_message=None, total_tokens=None)
    fields.update(over)
    return JobRow(**fields)


class ApiTestCase(unittest.TestCase):
    """Builds an app whose DB dependency and lifespan are neutralised."""

    def setUp(self) -> None:
        # The real lifespan opens a connection pool; there is no database here.
        patcher = mock.patch("app.main.get_pool")
        patcher.start()
        self.addCleanup(patcher.stop)
        reaper = mock.patch("app.services.job_service.reap_orphans", return_value=0)
        reaper.start()
        self.addCleanup(reaper.stop)
        closer = mock.patch("app.main.close_pool")
        closer.start()
        self.addCleanup(closer.stop)

        from app.api.deps import get_db
        from app.main import create_app

        self.app = create_app()
        self.app.dependency_overrides[get_db] = lambda: mock.MagicMock()
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)


class TestAddPatient(ApiTestCase):

    PAYLOAD = {"first_name": "Riya", "last_name": "Sharma",
               "date_of_birth": "2001-06-11", "gender": "female",
               "phone": "+91 98765 43210"}

    def test_creates_patient(self):
        with mock.patch("app.services.patient_service.add_patient",
                        return_value=a_patient()) as add:
            response = self.client.post("/api/v1/patients", json=self.PAYLOAD)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["external_id"], "P-2026-00009")
        self.assertEqual(body["patient_id"], PATIENT_ID)
        self.assertFalse(add.call_args.kwargs["allow_duplicate"])

    def test_probable_duplicate_returns_409_with_the_existing_patient(self):
        error = DuplicatePatientError(
            "A patient with the same name and date of birth already exists.",
            details={"existing_patient_id": PATIENT_ID,
                     "match_reason": "name_and_date_of_birth"})
        with mock.patch("app.services.patient_service.add_patient", side_effect=error):
            response = self.client.post("/api/v1/patients", json=self.PAYLOAD)
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "duplicate_patient")
        self.assertEqual(body["details"]["existing_patient_id"], PATIENT_ID)

    def test_date_of_birth_is_required(self):
        payload = {k: v for k, v in self.PAYLOAD.items() if k != "date_of_birth"}
        response = self.client.post("/api/v1/patients", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_future_date_of_birth_rejected(self):
        payload = {**self.PAYLOAD, "date_of_birth": "2099-01-01"}
        response = self.client.post("/api/v1/patients", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_blank_first_name_rejected(self):
        response = self.client.post("/api/v1/patients",
                                    json={**self.PAYLOAD, "first_name": ""})
        self.assertEqual(response.status_code, 422)

    def test_bad_email_rejected(self):
        response = self.client.post("/api/v1/patients",
                                    json={**self.PAYLOAD, "email": "not-an-email"})
        self.assertEqual(response.status_code, 422)

    def test_unknown_patient_is_404(self):
        with mock.patch("app.services.patient_service.get_patient",
                        side_effect=NotFoundError("No patient with that id.")):
            response = self.client.get(f"/api/v1/patients/{ABSENT_ID}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")

    def test_malformed_patient_id_is_422_not_500(self):
        """Ids are validated at the edge; otherwise Postgres fails to parse the
        uuid and the caller gets a 500 instead of a clear client error."""
        response = self.client.get(f"/api/v1/patients/{MALFORMED_ID}")
        self.assertEqual(response.status_code, 422)


class TestScheduleVisit(ApiTestCase):

    PAYLOAD = {"patient_id": PATIENT_ID, "visit_date": "2026-02-16",
               "visit_time": "10:30:00"}

    def test_creates_visit(self):
        with mock.patch("app.services.visit_service.schedule_visit",
                        return_value=a_visit()) as create:
            response = self.client.post("/api/v1/visits", json=self.PAYLOAD)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["visit_number"], 1)
        # Omitted, so the service assigns it.
        self.assertIsNone(create.call_args.kwargs["visit_number"])

    def test_visit_date_is_required(self):
        """It anchors follow-up resolution, so it must never be defaulted."""
        response = self.client.post("/api/v1/visits", json={"patient_id": PATIENT_ID})
        self.assertEqual(response.status_code, 422)

    def test_unknown_patient_is_404(self):
        with mock.patch("app.services.visit_service.schedule_visit",
                        side_effect=NotFoundError("No patient with that id.")):
            response = self.client.post("/api/v1/visits", json=self.PAYLOAD)
        self.assertEqual(response.status_code, 404)

    def test_malformed_patient_id_in_body_is_422(self):
        response = self.client.post(
            "/api/v1/visits", json={**self.PAYLOAD, "patient_id": MALFORMED_ID})
        self.assertEqual(response.status_code, 422)

    def test_duplicate_visit_number_is_409(self):
        with mock.patch("app.services.visit_service.schedule_visit",
                        side_effect=ConflictError("Visit number 1 already exists.")):
            response = self.client.post("/api/v1/visits",
                                        json={**self.PAYLOAD, "visit_number": 1})
        self.assertEqual(response.status_code, 409)

    def test_bad_visit_status_rejected(self):
        response = self.client.post(
            "/api/v1/visits", json={**self.PAYLOAD, "visit_status": "nonsense"})
        self.assertEqual(response.status_code, 422)

    def test_bad_visit_type_rejected(self):
        response = self.client.post(
            "/api/v1/visits", json={**self.PAYLOAD, "visit_type": "checkup"})
        self.assertEqual(response.status_code, 422)


class TestAttachTranscript(ApiTestCase):

    def test_patch_attaches_transcript_without_completing_the_visit(self):
        """Completion is an explicit transition now, so PATCH must not imply it.

        An implicit status change would contradict the Complete button and make
        the real state depend on the order of two unrelated actions.
        """
        updated = a_visit(transcript_path=f"transcripts/{VISIT_ID}.txt",
                          transcript_provider="local",
                          transcript_filename="riya_1.txt",
                          visit_status="scheduled")
        with mock.patch("app.services.visit_service.update_visit",
                        return_value=updated) as patch_call:
            response = self.client.patch(
                f"/api/v1/visits/{VISIT_ID}",
                json={"transcript_path": f"transcripts/{VISIT_ID}.txt"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["transcript_path"], f"transcripts/{VISIT_ID}.txt")
        self.assertEqual(body["visit_status"], "scheduled")
        self.assertEqual(patch_call.call_args.kwargs["transcript_path"],
                         f"transcripts/{VISIT_ID}.txt")

    def test_empty_patch_rejected(self):
        response = self.client.patch(f"/api/v1/visits/{VISIT_ID}", json={})
        self.assertEqual(response.status_code, 422)

    def test_path_outside_transcripts_dir_is_422(self):
        """No auth in front of this API, so traversal must be refused."""
        from app.core.errors import TranscriptError

        with mock.patch("app.services.visit_service.update_visit",
                        side_effect=TranscriptError(
                            "Transcript path resolves outside the allowed "
                            "transcripts directory.")):
            response = self.client.patch(
                f"/api/v1/visits/{VISIT_ID}",
                json={"transcript_path": "../../etc/passwd"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "transcript_error")


class TestGenerateInsights(ApiTestCase):

    def test_returns_202_with_a_job_to_poll(self):
        with mock.patch("app.services.job_service.enqueue", return_value=a_job()), \
             mock.patch("app.services.job_service.run_job") as run:
            response = self.client.post(f"/api/v1/visits/{VISIT_ID}/insights", json={})
        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        self.assertEqual(body["job_id"], JOB_ID)
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["poll_url"], f"/api/v1/jobs/{JOB_ID}")
        self.assertEqual(body["insights_url"], f"/api/v1/visits/{VISIT_ID}/insights")
        # The background task runs after the response is sent.
        run.assert_called_once_with(JOB_ID)

    def test_body_is_optional(self):
        with mock.patch("app.services.job_service.enqueue", return_value=a_job()), \
             mock.patch("app.services.job_service.run_job"):
            response = self.client.post(f"/api/v1/visits/{VISIT_ID}/insights")
        self.assertEqual(response.status_code, 202)

    def test_options_are_passed_through(self):
        with mock.patch("app.services.job_service.enqueue",
                        return_value=a_job()) as enqueue, \
             mock.patch("app.services.job_service.run_job"):
            self.client.post(f"/api/v1/visits/{VISIT_ID}/insights",
                             json={"stop_after": 1, "regenerate": True,
                                   "model": "gemini-3.6-flash"})
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["stop_after"], 1)
        self.assertTrue(kwargs["regenerate"])
        self.assertEqual(kwargs["model"], "gemini-3.6-flash")

    def test_stop_after_out_of_range_rejected(self):
        response = self.client.post(f"/api/v1/visits/{VISIT_ID}/insights",
                                    json={"stop_after": 9})
        self.assertEqual(response.status_code, 422)

    def test_missing_transcript_is_409(self):
        with mock.patch("app.services.job_service.enqueue",
                        side_effect=ConflictError("Visit has no transcript attached.")):
            response = self.client.post(f"/api/v1/visits/{VISIT_ID}/insights", json={})
        self.assertEqual(response.status_code, 409)

    def test_already_generated_is_409(self):
        with mock.patch("app.services.job_service.enqueue",
                        side_effect=ConflictError("already has insights")):
            response = self.client.post(f"/api/v1/visits/{VISIT_ID}/insights", json={})
        self.assertEqual(response.status_code, 409)

    def test_job_already_running_is_409(self):
        """The partial unique index means one visit cannot be processed twice at
        once, which would pay for two sets of API calls."""
        with mock.patch("app.services.job_service.enqueue",
                        side_effect=ConflictError("A job is already running.")):
            response = self.client.post(f"/api/v1/visits/{VISIT_ID}/insights", json={})
        self.assertEqual(response.status_code, 409)

    def test_unknown_visit_is_404(self):
        with mock.patch("app.services.job_service.enqueue",
                        side_effect=NotFoundError("No visit with that id.")):
            response = self.client.post(f"/api/v1/visits/{ABSENT_ID}/insights", json={})
        self.assertEqual(response.status_code, 404)

    def test_malformed_visit_id_is_422(self):
        response = self.client.post(f"/api/v1/visits/{MALFORMED_ID}/insights", json={})
        self.assertEqual(response.status_code, 422)


class TestJobPolling(ApiTestCase):

    def test_running_job(self):
        with mock.patch("app.services.job_service.get_job",
                        return_value=a_job(status="running", layers_completed=2)):
            response = self.client.get(f"/api/v1/jobs/{JOB_ID}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "running")
        self.assertEqual(body["layers_completed"], 2)

    def test_succeeded_job_reports_the_insight(self):
        with mock.patch("app.services.job_service.get_job",
                        return_value=a_job(status="succeeded", layers_completed=3,
                                           insight_id="ins-1", total_tokens=19539)):
            response = self.client.get(f"/api/v1/jobs/{JOB_ID}")
        body = response.json()
        self.assertEqual(body["insight_id"], "ins-1")
        self.assertEqual(body["total_tokens"], 19539)

    def test_quota_failure_has_its_own_status(self):
        """Distinct from 'failed' because it is worth retrying unchanged."""
        with mock.patch("app.services.job_service.get_job",
                        return_value=a_job(status="failed_quota",
                                           error_code="quota_exhausted",
                                           error_message="all keys exhausted")):
            response = self.client.get(f"/api/v1/jobs/{JOB_ID}")
        self.assertEqual(response.json()["status"], "failed_quota")

    def test_unknown_job_is_404(self):
        with mock.patch("app.services.job_service.get_job",
                        side_effect=NotFoundError("No job with that id.")):
            response = self.client.get(f"/api/v1/jobs/{ABSENT_ID}")
        self.assertEqual(response.status_code, 404)

    def test_malformed_job_id_is_422(self):
        response = self.client.get(f"/api/v1/jobs/{MALFORMED_ID}")
        self.assertEqual(response.status_code, 422)


class TestReadInsights(ApiTestCase):

    def test_not_generated_yet_is_404(self):
        with mock.patch("app.services.insight_service.get_insights",
                        side_effect=NotFoundError("No insights generated yet.")):
            response = self.client.get(f"/api/v1/visits/{VISIT_ID}/insights")
        self.assertEqual(response.status_code, 404)


class TestErrorMapping(unittest.TestCase):
    """Each error type maps to exactly one status, in one place."""

    def test_status_codes(self):
        from app.api.errors import status_for

        cases = [
            (NotFoundError("x"), 404),
            (ConflictError("x"), 409),
            (DuplicatePatientError("x"), 409),
            (QuotaExhaustedError("x"), 429),
        ]
        for error, expected in cases:
            self.assertEqual(status_for(error), expected, type(error).__name__)


class TestOpenApi(ApiTestCase):

    def test_schema_lists_every_endpoint(self):
        schema = self.client.get("/openapi.json").json()
        paths = schema["paths"]
        for path, method in [
            ("/api/v1/patients", "post"),
            ("/api/v1/patients/{patient_id}", "get"),
            ("/api/v1/visits", "post"),
            ("/api/v1/visits/{visit_id}", "patch"),
            ("/api/v1/visits/{visit_id}/insights", "post"),
            ("/api/v1/visits/{visit_id}/insights", "get"),
            ("/api/v1/jobs/{job_id}", "get"),
            ("/api/v1/health", "get"),
            ("/api/v1/patients/{patient_id}", "patch"),
            ("/api/v1/patients/{patient_id}/timeline", "get"),
            ("/api/v1/visits/{visit_id}/complete", "post"),
            ("/api/v1/visits/{visit_id}/cancel", "post"),
            ("/api/v1/visits/{visit_id}/transcript", "post"),
            ("/api/v1/visits/{visit_id}/transcript", "get"),
            ("/api/v1/visits/{visit_id}/transcript", "delete"),
            ("/api/v1/visits/{visit_id}/job", "get"),
            ("/api/v1/follow-ups", "get"),
            ("/api/v1/follow-ups/{follow_up_id}", "get"),
            ("/api/v1/follow-ups/{follow_up_id}", "patch"),
            ("/api/v1/stats", "get"),
        ]:
            self.assertIn(path, paths, path)
            self.assertIn(method, paths[path], f"{method.upper()} {path}")

    def test_docs_available(self):
        self.assertEqual(self.client.get("/docs").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
