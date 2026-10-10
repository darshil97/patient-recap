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
from app.db.models import (  # noqa: E402
    AudioUploadRow,
    DoctorRow,
    JobRow,
    PatientRow,
    VisitRow,
)

PATIENT_ID = "11111111-1111-1111-1111-111111111111"
VISIT_ID = "22222222-2222-2222-2222-222222222222"
JOB_ID = "33333333-3333-3333-3333-333333333333"
FOLLOW_UP_ID = "44444444-4444-4444-4444-444444444444"
# Well-formed but not in the database, to separate 404 from 422.
ABSENT_ID = "99999999-9999-9999-9999-999999999999"
MALFORMED_ID = "not-a-uuid"

# Two tenants. Doctor A owns everything the fixtures build; doctor B owns nothing
# and is used to prove they cannot reach it.
DOCTOR_A_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
DOCTOR_A_EMAIL = "doctor.a@example.com"
DOCTOR_A_AUTH_ID = "neon_auth_user_aaa"
DOCTOR_B_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DOCTOR_B_EMAIL = "doctor.b@example.com"
DOCTOR_B_AUTH_ID = "neon_auth_user_bbb"

# What the Next.js BFF forwards for a signed-in doctor (§8.12).
DOCTOR_A_HEADERS = {"X-Doctor-Auth-Id": DOCTOR_A_AUTH_ID,
                    "X-Doctor-Email": DOCTOR_A_EMAIL,
                    "X-Doctor-Name": "Doctor A"}
DOCTOR_B_HEADERS = {"X-Doctor-Auth-Id": DOCTOR_B_AUTH_ID,
                    "X-Doctor-Email": DOCTOR_B_EMAIL,
                    "X-Doctor-Name": "Doctor B"}


def a_patient(**over: Any) -> PatientRow:
    fields: Dict[str, Any] = dict(
        patient_id=PATIENT_ID, doctor_id=DOCTOR_A_ID,
        external_id="P-2026-00009", first_name="Riya",
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
        insights_generated=False, doctor_id=DOCTOR_A_ID,
        first_name="Riya", last_name="Sharma",
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


UPLOAD_ID = "55555555-5555-5555-5555-555555555555"


def a_live_upload(**over: Any) -> AudioUploadRow:
    """A live recording still being recorded: no plan yet (§8.24)."""
    fields: Dict[str, Any] = dict(
        upload_id=UPLOAD_ID, visit_id=VISIT_ID, status="receiving",
        filename="consultation.webm", extension=".webm", total_bytes=None,
        chunk_bytes=262144, chunk_count=None, replace_transcript=False,
        storage_provider="neon_object_storage", audio_path=None,
        duration_seconds=None, speakers=None, warnings=None, error_code=None,
        error_message=None, mode="live")
    fields.update(over)
    return AudioUploadRow(**fields)


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

        from app.api.deps import CurrentDoctor, get_db, require_auth, require_doctor
        from app.main import create_app

        self.app = create_app()
        self.app.dependency_overrides[get_db] = lambda: mock.MagicMock()
        # Auth is bypassed here on purpose: these tests are about routing,
        # validation and error mapping. Enforcement is covered by
        # TestApiKeyAuth, so a real API_KEY in .env cannot make this whole
        # suite fail with 401s for reasons unrelated to what it tests.
        self.app.dependency_overrides[require_auth] = lambda: None
        # Likewise the tenant: require_doctor needs a live database to resolve or
        # create a doctors row, and there is none here. Overridden with a fixed
        # doctor so these tests stay about routing. Tenancy enforcement has its own
        # two suites, TestDoctorIdentityAuth and TestCrossTenantIsolation.
        self.doctor = CurrentDoctor(doctor_id=DOCTOR_A_ID, email=DOCTOR_A_EMAIL,
                                    name="Doctor A")
        self.app.dependency_overrides[require_doctor] = lambda: self.doctor
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


class TestTranscriptLocationIsServerDecided(ApiTestCase):
    """A client cannot choose, or lie about, *where* a transcript lives.

    Two fields, same category as a client-supplied doctor_id: naming a
    server-side decision the caller has no business naming.

    `transcript_provider` — claiming a backend that does not hold the bytes makes
    the visit ungeneratable, because the read routes on that value (§8.22).

    `transcript_path` — worse. The only check it could be given is that it
    resolves inside `transcripts/`, which proves a file exists but not that it is
    *yours*; the locator is `transcripts/<visit-uuid>.txt` with no doctor_id in
    it, by design, so any valid visit uuid passes. One doctor could point their
    own visit at another doctor's transcript and read a verbatim consultation
    back through GET /visits/{id}/transcript. The address is now derived from
    visit_id at upload time and nowhere else (§8.19).
    """

    def test_visit_create_has_no_transcript_path_field(self):
        from app.schemas.visits import VisitCreate

        self.assertNotIn("transcript_path", VisitCreate.model_fields)

    def test_visit_update_has_no_transcript_path_field(self):
        from app.schemas.visits import VisitUpdate

        self.assertNotIn("transcript_path", VisitUpdate.model_fields)

    def test_a_client_supplied_path_is_ignored_on_create(self):
        with mock.patch("app.services.visit_service.schedule_visit",
                        return_value=a_visit()) as create:
            response = self.client.post(
                "/api/v1/visits",
                json={"patient_id": PATIENT_ID, "visit_date": "2026-02-16",
                      "transcript_path": "transcripts/deadbeef.txt"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("transcript_path", create.call_args.kwargs)

    def test_a_client_supplied_path_is_refused_on_patch(self):
        """422, not a silent 200: PATCH used to honour this, so ignoring it
        would answer success to a request that attached nothing."""
        with mock.patch("app.services.visit_service.update_visit",
                        return_value=a_visit()) as patch_call:
            response = self.client.patch(
                f"/api/v1/visits/{VISIT_ID}",
                json={"visit_type": "follow_up",
                      "transcript_path": "../../etc/passwd"})
        self.assertEqual(response.status_code, 422, response.text)
        patch_call.assert_not_called()

    def test_visit_create_has_no_transcript_provider_field(self):
        from app.schemas.visits import VisitCreate

        self.assertNotIn("transcript_provider", VisitCreate.model_fields)

    def test_visit_update_has_no_transcript_provider_field(self):
        from app.schemas.visits import VisitUpdate

        self.assertNotIn("transcript_provider", VisitUpdate.model_fields)

    def test_visit_out_still_reports_it(self):
        """Removed from requests, kept on responses: useful, and read-only."""
        from app.schemas.visits import VisitOut

        self.assertIn("transcript_provider", VisitOut.model_fields)

    def test_a_client_supplied_provider_is_ignored_on_create(self):
        """Pydantic drops unknown fields, so an older client sending it is
        harmlessly ignored rather than 422'd."""
        with mock.patch("app.services.visit_service.schedule_visit",
                        return_value=a_visit()) as create:
            response = self.client.post(
                "/api/v1/visits",
                json={"patient_id": PATIENT_ID, "visit_date": "2026-02-16",
                      "transcript_provider": "neon_object_storage"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("transcript_provider", create.call_args.kwargs)

    def test_a_client_supplied_provider_is_refused_on_patch(self):
        """VisitUpdate forbids extras; VisitCreate still ignores them."""
        with mock.patch("app.services.visit_service.update_visit",
                        return_value=a_visit()) as patch_call:
            response = self.client.patch(
                f"/api/v1/visits/{VISIT_ID}",
                json={"visit_type": "follow_up",
                      "transcript_provider": "neon_object_storage"})
        self.assertEqual(response.status_code, 422, response.text)
        patch_call.assert_not_called()

    def test_openapi_exposes_no_transcript_provider_request_field(self):
        """Structural, so re-adding it to a request model trips this."""
        schema = self.client.get("/openapi.json").json()
        for name in ("VisitCreate", "VisitUpdate"):
            props = schema["components"]["schemas"][name].get("properties", {})
            self.assertNotIn("transcript_provider", props, name)
            self.assertNotIn("transcript_path", props, name)


class TestVisitLifecycleIsTransitionsOnly(ApiTestCase):
    """PATCH corrects details. It cannot move a visit through its lifecycle.

    `visit_status` used to be accepted here, and the damage was invisible:
    `{"visit_status": "cancelled"}` skipped /cancel's refusal once insights
    exist, skipped reopening the follow-ups that visit had resolved, and left
    `cancelled_at` NULL. A patient who still needed calling simply dropped off
    the queue, with no error anywhere. `{"visit_status": "completed"}` left
    `completed_at` NULL and could revive a cancelled visit that /complete would
    have refused.

    The old test here mocked visit_service.update_visit wholesale, so it only
    ever proved the *route* did not invent a status — which is why the service
    forwarding one went unnoticed. These assert on the service and the schema.
    """

    def test_patch_rejects_visit_status(self):
        response = self.client.patch(f"/api/v1/visits/{VISIT_ID}",
                                     json={"visit_status": "completed"})
        self.assertEqual(response.status_code, 422, response.text)

    def test_patch_rejects_a_status_smuggled_alongside_a_real_field(self):
        """The 422 must come from the unknown field, not from an empty body."""
        with mock.patch("app.services.visit_service.update_visit",
                        return_value=a_visit()) as patch_call:
            response = self.client.patch(
                f"/api/v1/visits/{VISIT_ID}",
                json={"visit_type": "follow_up", "visit_status": "cancelled"})
        self.assertEqual(response.status_code, 422, response.text)
        patch_call.assert_not_called()

    def test_visit_update_schema_has_no_visit_status(self):
        from app.schemas.visits import VisitUpdate

        self.assertNotIn("visit_status", VisitUpdate.model_fields)

    def test_openapi_exposes_no_visit_status_on_patch(self):
        """Structural, so re-adding it to VisitUpdate trips this."""
        schema = self.client.get("/openapi.json").json()
        props = schema["components"]["schemas"]["VisitUpdate"].get("properties", {})
        self.assertNotIn("visit_status", props)

    def test_visit_create_still_accepts_a_status(self):
        """Creation is not a transition: historical loads land as 'completed'."""
        from app.schemas.visits import VisitCreate

        self.assertIn("visit_status", VisitCreate.model_fields)

    def test_the_service_never_writes_visit_status(self):
        """The repository is the last line: nothing reaches it but details."""
        from app.services import visit_service

        with mock.patch("app.services.visit_service.visits_repo") as repo:
            repo.get.return_value = a_visit()
            repo.update.return_value = a_visit()
            visit_service.update_visit(mock.MagicMock(), DOCTOR_A_ID,
                                       VISIT_ID, visit_type="follow_up")
        changes = repo.update.call_args.args[-1]
        self.assertEqual(changes, {"visit_type": "follow_up"})

    def test_update_visit_takes_no_status_argument(self):
        """A caller that still passes one gets a TypeError, not a silent write."""
        import inspect

        from app.services import visit_service

        params = inspect.signature(visit_service.update_visit).parameters
        self.assertNotIn("visit_status", params)
        self.assertNotIn("transcript_path", params)

    def test_status_is_not_in_the_repository_whitelist(self):
        """Read the source, so a refactor that re-widens `allowed` fails here."""
        import inspect

        from app.db.repositories import visits as visits_repo

        source = " ".join(inspect.getsource(visits_repo.update).split())
        self.assertIn('allowed = {"visit_date", "visit_time", "visit_type"}',
                      source)

    def test_empty_patch_rejected(self):
        response = self.client.patch(f"/api/v1/visits/{VISIT_ID}", json={})
        self.assertEqual(response.status_code, 422)

    def test_starting_a_session_is_its_own_transition(self):
        """`in_progress` is reached through /start, never through PATCH."""
        response = self.client.patch(f"/api/v1/visits/{VISIT_ID}",
                                     json={"visit_status": "in_progress"})
        self.assertEqual(response.status_code, 422, response.text)


class TestStartSession(ApiTestCase):
    """POST /visits/{id}/start: scheduled -> in_progress (§8.24)."""

    def test_returns_the_visit_in_progress(self):
        with mock.patch("app.services.visit_service.start_visit",
                        return_value=a_visit(visit_status="in_progress")) as start:
            response = self.client.post(f"/api/v1/visits/{VISIT_ID}/start")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["visit_status"], "in_progress")
        self.assertEqual(start.call_args.args[1:], (DOCTOR_A_ID, VISIT_ID))

    def test_a_finished_visit_is_409(self):
        with mock.patch("app.services.visit_service.start_visit",
                        side_effect=ConflictError("This visit is completed.")):
            response = self.client.post(f"/api/v1/visits/{VISIT_ID}/start")
        self.assertEqual(response.status_code, 409)

    def test_unknown_visit_is_404(self):
        with mock.patch("app.services.visit_service.start_visit",
                        side_effect=NotFoundError("No visit with that id.")):
            response = self.client.post(f"/api/v1/visits/{ABSENT_ID}/start")
        self.assertEqual(response.status_code, 404)

    def test_malformed_id_is_422(self):
        response = self.client.post(f"/api/v1/visits/{MALFORMED_ID}/start")
        self.assertEqual(response.status_code, 422)

    def test_complete_still_accepts_a_visit_in_progress(self):
        """End session completes the visit through the ordinary transition."""
        from app.services import visit_service

        with mock.patch("app.services.visit_service.visits_repo") as repo:
            repo.get.return_value = a_visit(visit_status="in_progress")
            repo.mark_completed.return_value = a_visit(visit_status="completed")
            visit = visit_service.complete_visit(mock.MagicMock(), DOCTOR_A_ID,
                                                 VISIT_ID)
        self.assertEqual(visit.visit_status, "completed")


class TestLiveRecordingRoutes(ApiTestCase):
    """The HTTP contract of a live recording. The rules are in
    tests/test_audio_uploads.py."""

    def test_start_returns_a_live_upload_without_a_plan(self):
        with mock.patch("app.services.audio_upload_service.start_live_upload",
                        return_value=a_live_upload()) as start:
            response = self.client.post(
                f"/api/v1/visits/{VISIT_ID}/audio-uploads/live",
                json={"filename": "consultation.webm"})
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["mode"], "live")
        self.assertIsNone(body["size_bytes"])
        self.assertIsNone(body["chunk_count"])
        self.assertEqual(body["chunk_bytes"], 262144)
        self.assertEqual(body["bytes_received"], 0)
        self.assertEqual(start.call_args.kwargs,
                         {"filename": "consultation.webm", "replace": False})

    def test_start_needs_a_filename(self):
        response = self.client.post(f"/api/v1/visits/{VISIT_ID}/audio-uploads/live",
                                    json={})
        self.assertEqual(response.status_code, 422)

    def test_start_without_a_session_is_409(self):
        with mock.patch("app.services.audio_upload_service.start_live_upload",
                        side_effect=ConflictError("Start the session first.")):
            response = self.client.post(
                f"/api/v1/visits/{VISIT_ID}/audio-uploads/live",
                json={"filename": "consultation.webm"})
        self.assertEqual(response.status_code, 409)

    def test_finish_passes_the_chunk_count_through(self):
        with mock.patch("app.services.audio_upload_service.finish_upload",
                        return_value=a_live_upload(status="processing",
                                                   total_bytes=300000,
                                                   chunk_count=2)) as finish, \
             mock.patch("app.services.audio_upload_service.process_upload"):
            response = self.client.post(f"/api/v1/audio-uploads/{UPLOAD_ID}/finish",
                                        json={"chunk_count": 2})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(finish.call_args.kwargs, {"chunk_count": 2})
        self.assertEqual(response.json()["size_bytes"], 300000)

    def test_finish_body_stays_optional(self):
        """File uploads and interrupted sessions finish with no body."""
        with mock.patch("app.services.audio_upload_service.finish_upload",
                        return_value=a_live_upload(status="processing",
                                                   total_bytes=10,
                                                   chunk_count=1)) as finish, \
             mock.patch("app.services.audio_upload_service.process_upload"):
            response = self.client.post(f"/api/v1/audio-uploads/{UPLOAD_ID}/finish")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(finish.call_args.kwargs, {"chunk_count": None})

    def test_finish_rejects_a_nonsense_count(self):
        response = self.client.post(f"/api/v1/audio-uploads/{UPLOAD_ID}/finish",
                                    json={"chunk_count": 0})
        self.assertEqual(response.status_code, 422)

    def test_discard(self):
        with mock.patch("app.services.audio_upload_service.discard_upload",
                        return_value=a_live_upload(status="abandoned")) as discard:
            response = self.client.post(f"/api/v1/audio-uploads/{UPLOAD_ID}/discard")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "abandoned")
        self.assertEqual(discard.call_args.args[1:], (DOCTOR_A_ID, UPLOAD_ID))

    def test_discarding_a_transcribed_recording_is_409(self):
        with mock.patch("app.services.audio_upload_service.discard_upload",
                        side_effect=ConflictError("Already transcribed.")):
            response = self.client.post(f"/api/v1/audio-uploads/{UPLOAD_ID}/discard")
        self.assertEqual(response.status_code, 409)

    def test_a_chunk_past_the_ceiling_is_413(self):
        from app.core.errors import TranscriptTooLargeError

        with mock.patch("app.services.audio_upload_service.put_chunk",
                        side_effect=TranscriptTooLargeError("Limit reached.")):
            response = self.client.put(
                f"/api/v1/audio-uploads/{UPLOAD_ID}/chunks/400",
                files={"file": ("chunk-400", b"x" * 10, "application/octet-stream")})
        self.assertEqual(response.status_code, 413)


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
                             json={"regenerate": True,
                                   "model": "gemini-3.6-flash"})
        kwargs = enqueue.call_args.kwargs
        self.assertTrue(kwargs["regenerate"])
        self.assertEqual(kwargs["model"], "gemini-3.6-flash")
        # The API never asks for a partial run, so it never names stop_after and
        # job_service's default of 3 stands.
        self.assertNotIn("stop_after", kwargs)
        # The tenant is positional and comes from the request context, never the
        # body: GenerateInsightsRequest has no doctor field to override it with.
        self.assertEqual(enqueue.call_args.args[0], DOCTOR_A_ID)

    def test_stop_after_is_not_a_request_field(self):
        """A partial run writes a brief with no care plan, which the UI renders
        directly and therefore cannot display at all. The knob stays on the CLI,
        and insight_service refuses to persist the result either way."""
        from app.schemas.insights import GenerateInsightsRequest

        self.assertNotIn("stop_after", GenerateInsightsRequest.model_fields)

        schema = self.client.get("/openapi.json").json()
        props = (schema["components"]["schemas"]["GenerateInsightsRequest"]
                 .get("properties", {}))
        self.assertNotIn("stop_after", props)

    def test_a_client_supplied_stop_after_is_ignored(self):
        """Ignored rather than 422'd: it was only ever an optimisation, so an
        older client asking for one still gets a complete, correct brief."""
        with mock.patch("app.services.job_service.enqueue",
                        return_value=a_job()) as enqueue, \
             mock.patch("app.services.job_service.run_job"):
            response = self.client.post(f"/api/v1/visits/{VISIT_ID}/insights",
                                        json={"stop_after": 1})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertNotIn("stop_after", enqueue.call_args.kwargs)

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


class TestPartialBriefIsNotPersisted(unittest.TestCase):
    """A run that did not reach Layer 3 must not be saved.

    insights_repo.save sets visits.insights_generated TRUE in the same
    transaction (§8.7), so a partial run produced a visit that *claims* to have a
    brief while its insights_json had no `layer_3`. The frontend renders Layer 3
    directly — chief_issue, the remedy, the watch-outs — so opening that visit
    threw and blanked the page.

    The HTTP API no longer offers stop_after at all, but the CLI still does,
    correctly, for iterating on an early prompt. So the guarantee lives at the
    write rather than in the absence of a request field.
    """

    def _result(self, layers):
        from app.pipeline.layers import LAYERS
        from app.pipeline.results import LayerResult, PatientContext, PipelineResult

        result = PipelineResult(
            patient=PatientContext(name="Riya", age=24, gender="Female",
                                   call_date="2026-02-16"),
            model="gemini-3.7-flash",
            transcript_path=f"transcripts/{VISIT_ID}.txt",
        )
        for number, data in layers.items():
            result.layers[number] = LayerResult(
                spec=LAYERS[number], prompt="", raw_response="{}", data=data)
        return result

    def test_a_three_layer_run_is_accepted(self):
        from app.services.insight_service import require_complete_brief

        require_complete_brief(
            self._result({1: {"a": 1}, 2: {"b": 2}, 3: {"chief_issue": "x"}}))

    def test_a_run_that_stopped_at_layer_1_is_refused(self):
        from app.core.errors import LayerOutputError
        from app.services.insight_service import require_complete_brief

        with self.assertRaises(LayerOutputError) as caught:
            require_complete_brief(self._result({1: {"a": 1}}))
        self.assertIn("Layer 3", str(caught.exception))

    def test_a_layer_3_that_is_not_an_object_is_refused(self):
        """extract_json returns whatever parsed: a list, a string, a number."""
        from app.core.errors import LayerOutputError
        from app.services.insight_service import require_complete_brief

        for bad in ([], "some prose", 7, None):
            with self.subTest(bad=bad), self.assertRaises(LayerOutputError):
                require_complete_brief(
                    self._result({1: {"a": 1}, 2: {"b": 2}, 3: bad}))

    def test_it_runs_before_anything_is_written(self):
        """Ordering matters: the guard is useless after the upsert."""
        import inspect

        from app.services import insight_service

        source = inspect.getsource(insight_service.generate_for_visit)
        self.assertLess(source.index("require_complete_brief(result)"),
                        source.index("insights_repo.save("))


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


class TestApiKeyAuth(unittest.TestCase):
    """The shared secret, with auth actually switched on.

    Builds its own client without the require_auth override, and patches the
    setting rather than relying on whatever is in .env, so the outcome does not
    depend on the developer's environment.
    """

    KEY = "test-key-8f3a9c2e"

    def setUp(self) -> None:
        for target in ("app.main.get_pool", "app.main.close_pool",
                       "app.services.job_service.reap_orphans"):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

        from app.api.deps import get_db
        from app.main import create_app

        self.app = create_app()
        self.app.dependency_overrides[get_db] = lambda: mock.MagicMock()

        # require_doctor is deliberately NOT overridden here. It depends on
        # require_auth, so overriding it would replace that whole sub-tree and the
        # key check would never run — every assertion below would then pass for
        # the wrong reason. Instead the doctor lookup is stubbed at the repository,
        # so the real dependency chain executes without a database.
        resolver = mock.patch(
            "app.api.deps.doctors_repo.resolve",
            return_value=DoctorRow(doctor_id=DOCTOR_A_ID, email=DOCTOR_A_EMAIL,
                                   auth_user_id=DOCTOR_A_AUTH_ID, name="Doctor A"))
        self.resolve = resolver.start()
        self.addCleanup(resolver.stop)

    def client(self, api_key: Optional[str]):
        """A client against an app whose configured key is `api_key`.

        Requests made through it carry valid doctor-identity headers unless a test
        strips them, so a 401 is attributable to the API key and not to a missing
        tenant.
        """
        settings = mock.MagicMock()
        settings.api.key = api_key
        patcher = mock.patch("app.api.deps.get_settings", return_value=settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        client = TestClient(self.app, headers=DOCTOR_A_HEADERS)
        self.addCleanup(client.close)
        return client

    # -- enforcement on ---------------------------------------------------- #

    def test_missing_key_is_401(self):
        response = self.client(self.KEY).get("/api/v1/stats")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "unauthorized")

    def test_wrong_key_is_401(self):
        response = self.client(self.KEY).get(
            "/api/v1/stats", headers={"X-API-Key": "wrong"})
        self.assertEqual(response.status_code, 401)

    def test_key_of_the_right_length_but_wrong_value_is_401(self):
        response = self.client(self.KEY).get(
            "/api/v1/stats", headers={"X-API-Key": "x" * len(self.KEY)})
        self.assertEqual(response.status_code, 401)

    def test_correct_key_passes_auth(self):
        """Reaches the handler, so the failure is no longer 401."""
        with mock.patch("app.services.stats_service.build_stats",
                        side_effect=RuntimeError("reached the handler")):
            with self.assertRaises(RuntimeError):
                self.client(self.KEY).get(
                    "/api/v1/stats", headers={"X-API-Key": self.KEY})

    def test_header_name_is_case_insensitive(self):
        """HTTP header names are case-insensitive; clients vary."""
        with mock.patch("app.services.stats_service.build_stats",
                        side_effect=RuntimeError("reached the handler")):
            with self.assertRaises(RuntimeError):
                self.client(self.KEY).get(
                    "/api/v1/stats", headers={"x-api-key": self.KEY})

    def test_every_data_route_is_protected(self):
        client = self.client(self.KEY)
        for method, path in [
            ("get", "/api/v1/stats"),
            ("get", "/api/v1/patients"),
            ("post", "/api/v1/patients"),
            ("get", f"/api/v1/patients/{PATIENT_ID}"),
            ("patch", f"/api/v1/patients/{PATIENT_ID}"),
            ("get", f"/api/v1/patients/{PATIENT_ID}/timeline"),
            ("get", "/api/v1/visits"),
            ("post", "/api/v1/visits"),
            ("get", f"/api/v1/visits/{VISIT_ID}"),
            ("patch", f"/api/v1/visits/{VISIT_ID}"),
            ("post", f"/api/v1/visits/{VISIT_ID}/complete"),
            ("post", f"/api/v1/visits/{VISIT_ID}/cancel"),
            ("post", f"/api/v1/visits/{VISIT_ID}/transcript"),
            ("get", f"/api/v1/visits/{VISIT_ID}/transcript"),
            ("delete", f"/api/v1/visits/{VISIT_ID}/transcript"),
            ("post", f"/api/v1/visits/{VISIT_ID}/insights"),
            ("get", f"/api/v1/visits/{VISIT_ID}/insights"),
            ("get", f"/api/v1/visits/{VISIT_ID}/job"),
            ("get", "/api/v1/follow-ups"),
            ("get", f"/api/v1/follow-ups/{JOB_ID}"),
            ("patch", f"/api/v1/follow-ups/{JOB_ID}"),
            ("get", "/api/v1/jobs"),
            ("get", f"/api/v1/jobs/{JOB_ID}"),
            ("get", "/api/v1/health/api-keys"),
        ]:
            response = getattr(client, method)(path)
            self.assertEqual(response.status_code, 401,
                             f"{method.upper()} {path} was not protected")

    def test_health_stays_open(self):
        """A liveness monitor should not need the secret."""
        with mock.patch("app.api.v1.health.connection"):
            response = self.client(self.KEY).get("/api/v1/health")
        self.assertNotEqual(response.status_code, 401)

    # -- enforcement off --------------------------------------------------- #

    def test_unset_key_leaves_the_api_open(self):
        """Local development is unchanged when API_KEY is not configured."""
        with mock.patch("app.services.stats_service.build_stats",
                        side_effect=RuntimeError("reached the handler")):
            with self.assertRaises(RuntimeError):
                self.client(None).get("/api/v1/stats")

    def test_empty_string_key_counts_as_unset(self):
        with mock.patch("app.services.stats_service.build_stats",
                        side_effect=RuntimeError("reached the handler")):
            with self.assertRaises(RuntimeError):
                self.client("").get("/api/v1/stats")

    def test_stray_header_is_ignored_when_auth_is_off(self):
        with mock.patch("app.services.stats_service.build_stats",
                        side_effect=RuntimeError("reached the handler")):
            with self.assertRaises(RuntimeError):
                self.client(None).get("/api/v1/stats",
                                      headers={"X-API-Key": "anything"})

    # -- comparison is constant time --------------------------------------- #

    def test_comparison_uses_compare_digest(self):
        """`==` on secrets leaks length and content through timing."""
        import inspect

        from app.api import deps

        source = inspect.getsource(deps.require_auth)
        self.assertIn("compare_digest", source)


class TestOpenApiSecurityScheme(ApiTestCase):

    def test_api_key_scheme_is_declared(self):
        schema = self.client.get("/openapi.json").json()
        schemes = schema["components"]["securitySchemes"]
        self.assertIn("ApiKeyAuth", schemes)
        self.assertEqual(schemes["ApiKeyAuth"]["type"], "apiKey")
        self.assertEqual(schemes["ApiKeyAuth"]["in"], "header")
        self.assertEqual(schemes["ApiKeyAuth"]["name"], "X-API-Key")

    def test_protected_routes_declare_the_requirement(self):
        """So /docs offers Authorize and a generated client sends the header."""
        paths = self.client.get("/openapi.json").json()["paths"]
        for path, method in [("/api/v1/stats", "get"),
                            ("/api/v1/patients", "post"),
                            ("/api/v1/visits", "get"),
                            ("/api/v1/follow-ups", "get")]:
            security = paths[path][method].get("security")
            self.assertTrue(security, f"{method.upper()} {path} declares no security")
            self.assertIn("ApiKeyAuth", security[0])

    def test_health_declares_no_requirement(self):
        health = self.client.get("/openapi.json").json()["paths"]["/api/v1/health"]
        self.assertFalse(health["get"].get("security"))

    def test_key_is_not_a_visible_query_or_body_field(self):
        """It belongs in a header, not somewhere it lands in access logs."""
        params = (self.client.get("/openapi.json").json()
                  ["paths"]["/api/v1/stats"]["get"].get("parameters", []))
        self.assertFalse([p for p in params if "api" in p["name"].lower()])


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
            ("/api/v1/visits/{visit_id}/start", "post"),
            ("/api/v1/visits/{visit_id}/audio-uploads", "post"),
            ("/api/v1/visits/{visit_id}/audio-uploads/live", "post"),
            ("/api/v1/visits/{visit_id}/audio-upload", "get"),
            ("/api/v1/audio-uploads/{upload_id}/chunks/{index}", "put"),
            ("/api/v1/audio-uploads/{upload_id}/finish", "post"),
            ("/api/v1/audio-uploads/{upload_id}/discard", "post"),
            ("/api/v1/audio-uploads/{upload_id}", "get"),
        ]:
            self.assertIn(path, paths, path)
            self.assertIn(method, paths[path], f"{method.upper()} {path}")

    def test_docs_available(self):
        self.assertEqual(self.client.get("/docs").status_code, 200)




class TestDoctorIdentityAuth(unittest.TestCase):
    """The tenancy gate: every request must say whose data it is about.

    Builds its own client without the require_doctor override, and stubs the
    doctors repository rather than the dependency, so the real resolution logic
    runs. The API key is left unconfigured so a 401 here can only come from the
    doctor-identity check — the mirror image of TestApiKeyAuth.
    """

    def setUp(self) -> None:
        for target in ("app.main.get_pool", "app.main.close_pool",
                       "app.services.job_service.reap_orphans"):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

        from app.api.deps import get_db
        from app.main import create_app

        settings = mock.MagicMock()
        settings.api.key = None  # key gate off; this suite is about the tenant
        key_patch = mock.patch("app.api.deps.get_settings", return_value=settings)
        key_patch.start()
        self.addCleanup(key_patch.stop)

        self.app = create_app()
        self.app.dependency_overrides[get_db] = lambda: mock.MagicMock()

        resolver = mock.patch("app.api.deps.doctors_repo.resolve")
        self.resolve = resolver.start()
        self.addCleanup(resolver.stop)
        self.resolve.return_value = DoctorRow(
            doctor_id=DOCTOR_A_ID, email=DOCTOR_A_EMAIL,
            auth_user_id=DOCTOR_A_AUTH_ID, name="Doctor A")

        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def get_stats(self, headers):
        """GET /stats, which reaches the handler only if both gates pass."""
        with mock.patch("app.services.stats_service.build_stats",
                        return_value={"patients": 0,
                                      "visits": {"total": 0, "scheduled": 0,
                                                 "in_progress": 0, "completed": 0,
                                                 "cancelled": 0, "with_insights": 0,
                                                 "awaiting_transcript": 0,
                                                 "awaiting_insights": 0,
                                                 "pending": 0},
                                      "follow_ups": {"open": 0, "resolved": 0,
                                                     "dismissed": 0, "overdue": 0,
                                                     "due_next_7_days": 0,
                                                     "needs_confirmation": 0},
                                      "as_of": date(2026, 9, 10)}):
            return self.client.get("/api/v1/stats", headers=headers)

    # -- absent identity is refused ---------------------------------------- #

    def test_no_doctor_headers_is_401(self):
        """The whole point of this change: no code path serves data without
        knowing whose it is."""
        response = self.get_stats({})
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["code"], "unauthorized")
        self.resolve.assert_not_called()

    def test_401_names_the_headers_it_wanted(self):
        """A misconfigured BFF is the likely cause, so the error has to say so."""
        body = self.get_stats({}).json()
        required = body["details"]["required_headers"]
        self.assertIn("X-Doctor-Auth-Id", required)
        self.assertIn("X-Doctor-Email", required)

    def test_blank_headers_count_as_absent(self):
        response = self.get_stats({"X-Doctor-Auth-Id": "  ",
                                   "X-Doctor-Email": ""})
        self.assertEqual(response.status_code, 401)

    def test_malformed_email_is_401_not_ignored(self):
        """Falling back to the auth id would create a doctor row with a junk
        email, and that row is what every patient gets foreign-keyed to."""
        response = self.get_stats({"X-Doctor-Auth-Id": DOCTOR_A_AUTH_ID,
                                   "X-Doctor-Email": "not-an-email"})
        self.assertEqual(response.status_code, 401)
        self.resolve.assert_not_called()

    def test_unknown_auth_id_with_no_email_is_401(self):
        """Creating a nameless tenant would split one doctor's records in two."""
        self.resolve.return_value = None
        response = self.get_stats({"X-Doctor-Auth-Id": "never-seen-before"})
        self.assertEqual(response.status_code, 401)

    def test_every_data_route_requires_a_doctor(self):
        client = self.client
        for method, path in [
            ("get", "/api/v1/stats"),
            ("get", "/api/v1/patients"),
            ("post", "/api/v1/patients"),
            ("get", f"/api/v1/patients/{PATIENT_ID}"),
            ("patch", f"/api/v1/patients/{PATIENT_ID}"),
            ("get", f"/api/v1/patients/{PATIENT_ID}/timeline"),
            ("get", "/api/v1/visits"),
            ("post", "/api/v1/visits"),
            ("get", f"/api/v1/visits/{VISIT_ID}"),
            ("patch", f"/api/v1/visits/{VISIT_ID}"),
            ("post", f"/api/v1/visits/{VISIT_ID}/complete"),
            ("post", f"/api/v1/visits/{VISIT_ID}/cancel"),
            ("post", f"/api/v1/visits/{VISIT_ID}/transcript"),
            ("get", f"/api/v1/visits/{VISIT_ID}/transcript"),
            ("delete", f"/api/v1/visits/{VISIT_ID}/transcript"),
            ("post", f"/api/v1/visits/{VISIT_ID}/insights"),
            ("get", f"/api/v1/visits/{VISIT_ID}/insights"),
            ("get", f"/api/v1/visits/{VISIT_ID}/job"),
            ("get", "/api/v1/follow-ups"),
            ("get", f"/api/v1/follow-ups/{FOLLOW_UP_ID}"),
            ("patch", f"/api/v1/follow-ups/{FOLLOW_UP_ID}"),
            ("get", "/api/v1/jobs"),
            ("get", f"/api/v1/jobs/{JOB_ID}"),
        ]:
            response = getattr(client, method)(path)
            self.assertEqual(
                response.status_code, 401,
                f"{method.upper()} {path} served data with no doctor identity")

    def test_health_stays_exempt(self):
        """A liveness probe is not a doctor. Requiring it to be one would stop
        monitoring distinguishing 'service down' from 'headers wrong'."""
        with mock.patch("app.api.v1.health.connection"):
            response = self.client.get("/api/v1/health")
        self.assertNotEqual(response.status_code, 401)

    # -- present identity resolves ----------------------------------------- #

    def test_both_headers_resolve_the_doctor(self):
        response = self.get_stats(DOCTOR_A_HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        kwargs = self.resolve.call_args.kwargs
        self.assertEqual(kwargs["auth_user_id"], DOCTOR_A_AUTH_ID)
        self.assertEqual(kwargs["email"], DOCTOR_A_EMAIL)
        self.assertEqual(kwargs["name"], "Doctor A")

    def test_email_alone_resolves(self):
        """This is the path the seeded doctor row takes before its first
        sign-in has stamped an auth id onto it."""
        response = self.get_stats({"X-Doctor-Email": DOCTOR_A_EMAIL})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.resolve.call_args.kwargs["auth_user_id"])

    def test_auth_id_alone_resolves_a_known_doctor(self):
        response = self.get_stats({"X-Doctor-Auth-Id": DOCTOR_A_AUTH_ID})
        self.assertEqual(response.status_code, 200, response.text)

    def test_email_is_lowercased_before_lookup(self):
        """One mailbox must not become two tenants."""
        self.get_stats({"X-Doctor-Email": "Doctor.A@Example.COM"})
        self.assertEqual(self.resolve.call_args.kwargs["email"], DOCTOR_A_EMAIL)

    def test_header_names_are_case_insensitive(self):
        response = self.get_stats({"x-doctor-auth-id": DOCTOR_A_AUTH_ID,
                                   "x-doctor-email": DOCTOR_A_EMAIL})
        self.assertEqual(response.status_code, 200, response.text)

    def test_resolved_doctor_id_reaches_the_service(self):
        """The scope the handler passes down must be the resolved doctor_id, not
        anything the client sent."""
        with mock.patch("app.services.stats_service.build_stats",
                        side_effect=RuntimeError("reached")) as build:
            with self.assertRaises(RuntimeError):
                self.client.get("/api/v1/stats", headers=DOCTOR_A_HEADERS)
        self.assertEqual(build.call_args.args[1], DOCTOR_A_ID)

    # -- the key gate runs first ------------------------------------------- #

    def test_an_email_bound_to_another_account_is_401(self):
        """resolve() refuses when the email's row is claimed by a different auth
        id; the dependency must turn that into a refusal, not a fallback."""
        self.resolve.return_value = None
        response = self.get_stats({"X-Doctor-Auth-Id": DOCTOR_B_AUTH_ID,
                                   "X-Doctor-Email": DOCTOR_A_EMAIL})
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["code"], "unauthorized")

    def test_a_bad_api_key_is_refused_before_any_doctor_row_is_touched(self):
        """require_doctor depends on require_auth, and the order matters: an
        unauthenticated request must not be able to create rows in `doctors` by
        presenting a new email, which would be an unauthenticated write."""
        settings = mock.MagicMock()
        settings.api.key = "the-real-key"
        with mock.patch("app.api.deps.get_settings", return_value=settings):
            response = self.client.get(
                "/api/v1/stats",
                headers={**DOCTOR_A_HEADERS, "X-API-Key": "wrong"})
        self.assertEqual(response.status_code, 401)
        self.resolve.assert_not_called()


class TestCrossTenantIsolation(unittest.TestCase):
    """Doctor A must never reach doctor B's data, under any endpoint.

    THE MOST IMPORTANT SUITE IN THIS FILE. A regression here is a real patient
    data leak, not a cosmetic bug: it means one practitioner reading another's
    clinical records.

    The approach is deliberately not "assert the route returns 404". That would
    only prove the service raised, and would still pass if a future refactor
    dropped the tenancy filter and relied on the route to check ownership
    afterwards. Instead each test asserts on the value the API layer *hands the
    service*: the resolved doctor_id from the request context. Combined with
    TestScopedRepositorySql below — which asserts the SQL itself carries a
    doctor filter — that covers both halves of the boundary.

    Doctor B is signed in throughout. Every id used belongs to doctor A.
    """

    def setUp(self) -> None:
        for target in ("app.main.get_pool", "app.main.close_pool",
                       "app.services.job_service.reap_orphans"):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

        from app.api.deps import get_db
        from app.main import create_app

        settings = mock.MagicMock()
        settings.api.key = None
        key_patch = mock.patch("app.api.deps.get_settings", return_value=settings)
        key_patch.start()
        self.addCleanup(key_patch.stop)

        self.app = create_app()
        self.app.dependency_overrides[get_db] = lambda: mock.MagicMock()

        # Doctor B is who is signed in.
        resolver = mock.patch(
            "app.api.deps.doctors_repo.resolve",
            return_value=DoctorRow(doctor_id=DOCTOR_B_ID, email=DOCTOR_B_EMAIL,
                                   auth_user_id=DOCTOR_B_AUTH_ID, name="Doctor B"))
        resolver.start()
        self.addCleanup(resolver.stop)

        self.client = TestClient(self.app, headers=DOCTOR_B_HEADERS)
        self.addCleanup(self.client.close)

    def assert_scoped_to_b(self, target: str, call, positional: int = 1):
        """Run `call` and assert the patched service saw doctor B's id.

        `positional` is where the scope sits in the argument list: 1 for the
        conn-first services, 0 for job_service, which opens its own connection.
        """
        with mock.patch(target, side_effect=NotFoundError("not yours")) as patched:
            response = call()
        self.assertTrue(patched.called, f"{target} was never reached")
        self.assertEqual(
            patched.call_args.args[positional], DOCTOR_B_ID,
            f"{target} was called with the wrong tenant — doctor A's data would "
            f"be reachable by doctor B")
        return response

    # -- reads -------------------------------------------------------------- #

    def test_cannot_read_another_doctors_patient(self):
        response = self.assert_scoped_to_b(
            "app.services.patient_service.get_patient",
            lambda: self.client.get(f"/api/v1/patients/{PATIENT_ID}"))
        self.assertEqual(response.status_code, 404)

    def test_another_doctors_patient_is_404_not_403(self):
        """403 would confirm the id exists, which discloses that some other
        doctor holds a record under it. 404 says nothing."""
        with mock.patch("app.services.patient_service.get_patient",
                        side_effect=NotFoundError("No patient with that id.")):
            response = self.client.get(f"/api/v1/patients/{PATIENT_ID}")
        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "not_found")

    def test_another_doctors_patient_id_and_an_unknown_id_are_indistinguishable(self):
        """Same status, same code, and a message that does not reveal which
        case it was — otherwise the pair becomes an existence oracle."""
        with mock.patch("app.services.patient_service.get_patient",
                        side_effect=NotFoundError(
                            f"No patient with id {PATIENT_ID}.")):
            theirs = self.client.get(f"/api/v1/patients/{PATIENT_ID}")
        with mock.patch("app.services.patient_service.get_patient",
                        side_effect=NotFoundError(
                            f"No patient with id {ABSENT_ID}.")):
            nobodys = self.client.get(f"/api/v1/patients/{ABSENT_ID}")

        self.assertEqual(theirs.status_code, nobodys.status_code)
        self.assertEqual(theirs.json()["code"], nobodys.json()["code"])
        for body in (theirs.json(), nobodys.json()):
            self.assertNotIn("doctor", body["message"].lower())
            self.assertNotIn("belongs", body["message"].lower())

    def test_cannot_read_another_doctors_timeline(self):
        """One request would otherwise return a patient's entire history."""
        response = self.assert_scoped_to_b(
            "app.services.patient_service.build_timeline",
            lambda: self.client.get(f"/api/v1/patients/{PATIENT_ID}/timeline"))
        self.assertEqual(response.status_code, 404)

    def test_cannot_read_another_doctors_visit(self):
        response = self.assert_scoped_to_b(
            "app.services.visit_service.get_visit",
            lambda: self.client.get(f"/api/v1/visits/{VISIT_ID}"))
        self.assertEqual(response.status_code, 404)

    def test_cannot_read_another_doctors_insights(self):
        """The clinical content itself: extraction, remedy reasoning, care plan."""
        response = self.assert_scoped_to_b(
            "app.services.insight_service.get_insights",
            lambda: self.client.get(f"/api/v1/visits/{VISIT_ID}/insights"))
        self.assertEqual(response.status_code, 404)

    def test_cannot_read_another_doctors_transcript(self):
        """Verbatim consultation text is the rawest data the system holds."""
        response = self.assert_scoped_to_b(
            "app.services.transcript_service.read_transcript",
            lambda: self.client.get(f"/api/v1/visits/{VISIT_ID}/transcript"))
        self.assertEqual(response.status_code, 404)

    def test_cannot_read_another_doctors_follow_up(self):
        response = self.assert_scoped_to_b(
            "app.services.follow_up_service.get_follow_up",
            lambda: self.client.get(f"/api/v1/follow-ups/{FOLLOW_UP_ID}"))
        self.assertEqual(response.status_code, 404)

    def test_cannot_poll_another_doctors_job(self):
        """GET /jobs/{job_id} has no patient or visit in its path, so nothing
        about the route suggests it returns tenant data. It does, and the
        frontend polls it every 2.5s while a brief generates."""
        response = self.assert_scoped_to_b(
            "app.services.job_service.get_job",
            lambda: self.client.get(f"/api/v1/jobs/{JOB_ID}"),
            positional=0)  # job_service opens its own connection
        self.assertEqual(response.status_code, 404)

    def test_cannot_read_another_doctors_latest_job_for_a_visit(self):
        response = self.assert_scoped_to_b(
            "app.services.visit_service.get_visit",
            lambda: self.client.get(f"/api/v1/visits/{VISIT_ID}/job"))
        self.assertEqual(response.status_code, 404)

    # -- recordings and live sessions (§8.23, §8.24) ------------------------ #

    def test_cannot_start_another_doctors_session(self):
        response = self.assert_scoped_to_b(
            "app.services.visit_service.start_visit",
            lambda: self.client.post(f"/api/v1/visits/{VISIT_ID}/start"))
        self.assertEqual(response.status_code, 404)

    def test_cannot_record_into_another_doctors_visit(self):
        response = self.assert_scoped_to_b(
            "app.services.audio_upload_service.start_live_upload",
            lambda: self.client.post(
                f"/api/v1/visits/{VISIT_ID}/audio-uploads/live",
                json={"filename": "consultation.webm"}))
        self.assertEqual(response.status_code, 404)

    def test_cannot_send_chunks_to_another_doctors_recording(self):
        response = self.assert_scoped_to_b(
            "app.services.audio_upload_service.put_chunk",
            lambda: self.client.put(
                f"/api/v1/audio-uploads/{UPLOAD_ID}/chunks/0",
                files={"file": ("chunk-0", b"x", "application/octet-stream")}))
        self.assertEqual(response.status_code, 404)

    def test_cannot_finish_another_doctors_recording(self):
        response = self.assert_scoped_to_b(
            "app.services.audio_upload_service.finish_upload",
            lambda: self.client.post(f"/api/v1/audio-uploads/{UPLOAD_ID}/finish",
                                     json={"chunk_count": 1}))
        self.assertEqual(response.status_code, 404)

    def test_cannot_discard_another_doctors_recording(self):
        response = self.assert_scoped_to_b(
            "app.services.audio_upload_service.discard_upload",
            lambda: self.client.post(f"/api/v1/audio-uploads/{UPLOAD_ID}/discard"))
        self.assertEqual(response.status_code, 404)

    def test_cannot_poll_another_doctors_recording(self):
        response = self.assert_scoped_to_b(
            "app.services.audio_upload_service.get_upload",
            lambda: self.client.get(f"/api/v1/audio-uploads/{UPLOAD_ID}"))
        self.assertEqual(response.status_code, 404)

    # -- lists and counts --------------------------------------------------- #

    def test_patient_list_is_scoped(self):
        with mock.patch("app.db.repositories.patients.list_all",
                        return_value=[]) as listed, \
             mock.patch("app.db.repositories.patients.count_all", return_value=0):
            self.client.get("/api/v1/patients")
        self.assertEqual(listed.call_args.args[1], DOCTOR_B_ID)

    def test_patient_count_header_is_scoped(self):
        """An unscoped X-Total-Count tells every doctor how many patients the
        others have, and with `q` set answers 'does a patient named X exist
        anywhere' without returning a row."""
        with mock.patch("app.db.repositories.patients.list_all", return_value=[]), \
             mock.patch("app.db.repositories.patients.count_all",
                        return_value=0) as counted:
            self.client.get("/api/v1/patients?q=sharma")
        self.assertEqual(counted.call_args.args[1], DOCTOR_B_ID)

    def test_patient_search_cannot_probe_other_doctors(self):
        """The search runs inside the doctor filter, not beside it."""
        with mock.patch("app.db.repositories.patients.list_all",
                        return_value=[]) as listed, \
             mock.patch("app.db.repositories.patients.count_all", return_value=0):
            self.client.get("/api/v1/patients?q=P-2026-00009")
        self.assertEqual(listed.call_args.args[1], DOCTOR_B_ID)
        self.assertEqual(listed.call_args.kwargs["q"], "P-2026-00009")

    def test_visit_list_is_scoped(self):
        with mock.patch("app.db.repositories.visits.list_visits",
                        return_value=[]) as listed, \
             mock.patch("app.db.repositories.visits.count_visits", return_value=0):
            self.client.get("/api/v1/visits")
        self.assertEqual(listed.call_args.args[1], DOCTOR_B_ID)

    def test_filtering_visits_by_another_doctors_patient_stays_scoped(self):
        """Returns an empty list — the same answer as a patient with no visits —
        so a guessed patient_id reveals nothing."""
        with mock.patch("app.db.repositories.visits.list_visits",
                        return_value=[]) as listed, \
             mock.patch("app.db.repositories.visits.count_visits", return_value=0):
            response = self.client.get(f"/api/v1/visits?patient_id={PATIENT_ID}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        self.assertEqual(listed.call_args.args[1], DOCTOR_B_ID)

    def test_follow_up_queue_is_scoped(self):
        """Every row on this queue carries a patient's name and phone number."""
        with mock.patch("app.db.repositories.follow_ups.list_follow_ups",
                        return_value=[]) as listed, \
             mock.patch("app.db.repositories.follow_ups.count_follow_ups",
                        return_value=0):
            self.client.get("/api/v1/follow-ups")
        self.assertEqual(listed.call_args.args[1], DOCTOR_B_ID)

    def test_job_list_is_scoped(self):
        """A route with no id at all that used to return every doctor's
        generation history."""
        with mock.patch("app.db.repositories.jobs.list_recent",
                        return_value=[]) as listed:
            self.client.get("/api/v1/jobs")
        self.assertEqual(listed.call_args.args[1], DOCTOR_B_ID)

    def test_stats_counters_are_scoped(self):
        """A leaking list is obvious the moment a doctor reads a name they do not
        recognise. A leaking counter just looks like a number slightly too high."""
        with mock.patch("app.services.stats_service.build_stats",
                        side_effect=RuntimeError("reached")) as build:
            with self.assertRaises(RuntimeError):
                self.client.get("/api/v1/stats")
        self.assertEqual(build.call_args.args[1], DOCTOR_B_ID)

    # -- writes ------------------------------------------------------------- #

    def test_cannot_update_another_doctors_patient(self):
        response = self.assert_scoped_to_b(
            "app.services.patient_service.update_patient",
            lambda: self.client.patch(f"/api/v1/patients/{PATIENT_ID}",
                                      json={"phone": "+919999999999"}))
        self.assertEqual(response.status_code, 404)

    def test_cannot_update_another_doctors_visit(self):
        response = self.assert_scoped_to_b(
            "app.services.visit_service.update_visit",
            lambda: self.client.patch(f"/api/v1/visits/{VISIT_ID}",
                                      json={"visit_type": "follow_up"}))
        self.assertEqual(response.status_code, 404)

    def test_cannot_complete_another_doctors_visit(self):
        response = self.assert_scoped_to_b(
            "app.services.visit_service.complete_visit",
            lambda: self.client.post(f"/api/v1/visits/{VISIT_ID}/complete"))
        self.assertEqual(response.status_code, 404)

    def test_cannot_cancel_another_doctors_visit(self):
        response = self.assert_scoped_to_b(
            "app.services.visit_service.cancel_visit",
            lambda: self.client.post(f"/api/v1/visits/{VISIT_ID}/cancel"))
        self.assertEqual(response.status_code, 404)

    def test_cannot_schedule_a_visit_for_another_doctors_patient(self):
        response = self.assert_scoped_to_b(
            "app.services.visit_service.schedule_visit",
            lambda: self.client.post("/api/v1/visits",
                                     json={"patient_id": PATIENT_ID,
                                           "visit_date": "2026-10-01"}))
        self.assertEqual(response.status_code, 404)

    def test_cannot_upload_a_transcript_to_another_doctors_visit(self):
        """Uploading writes transcripts/{visit_id}.txt, so unscoped this would
        overwrite the source recording behind another doctor's brief."""
        response = self.assert_scoped_to_b(
            "app.services.transcript_service.store_transcript",
            lambda: self.client.post(
                f"/api/v1/visits/{VISIT_ID}/transcript",
                files={"file": ("t.txt", b"DOCTOR: hello", "text/plain")}))
        self.assertEqual(response.status_code, 404)

    def test_cannot_delete_another_doctors_transcript(self):
        response = self.assert_scoped_to_b(
            "app.services.transcript_service.delete_transcript",
            lambda: self.client.delete(f"/api/v1/visits/{VISIT_ID}/transcript"))
        self.assertEqual(response.status_code, 404)

    def test_cannot_dismiss_another_doctors_follow_up(self):
        """This one is quiet: dismissing another doctor's follow-up surfaces as
        no error anywhere. The patient simply drops off a queue nobody watches."""
        response = self.assert_scoped_to_b(
            "app.services.follow_up_service.update_follow_up",
            lambda: self.client.patch(
                f"/api/v1/follow-ups/{FOLLOW_UP_ID}",
                json={"status": "dismissed", "dismissed_reason": "x"}))
        self.assertEqual(response.status_code, 404)

    def test_cannot_generate_insights_for_another_doctors_visit(self):
        """Three model calls against a 20/day quota, writing a clinical brief."""
        response = self.assert_scoped_to_b(
            "app.services.job_service.enqueue",
            lambda: self.client.post(f"/api/v1/visits/{VISIT_ID}/insights",
                                     json={}),
            positional=0)  # job_service opens its own connection
        self.assertEqual(response.status_code, 404)

    # -- the soft-uniqueness check (§8.10) ---------------------------------- #

    def test_soft_conflict_check_is_scoped_to_the_registering_doctor(self):
        """THE §8.10 BUG. Two doctors having a patient with the same name and
        date of birth is ordinary, not a coincidence, and must not 409."""
        with mock.patch("app.db.repositories.patients.find_probable_duplicate",
                        return_value=None) as probe, \
             mock.patch("app.db.repositories.patients.create",
                        return_value=a_patient(doctor_id=DOCTOR_B_ID)):
            response = self.client.post(
                "/api/v1/patients",
                json={"first_name": "Riya", "last_name": "Sharma",
                      "date_of_birth": "2001-06-11"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(probe.call_args.args[1], DOCTOR_B_ID)

    def test_same_name_and_dob_as_another_doctors_patient_is_not_a_conflict(self):
        """End to end through the real service: doctor A already has Riya Sharma
        born 2001-06-11, and the scoped lookup returns nothing for doctor B, so
        registration succeeds instead of 409ing on someone else's record."""
        doctor_a_patients = {(DOCTOR_A_ID, "riya", "sharma",
                              date(2001, 6, 11)): a_patient()}

        def scoped_probe(conn, scope, first, last, dob):
            key = (str(scope), first.strip().lower(),
                   (last or "").strip().lower(), dob)
            return doctor_a_patients.get(key)

        with mock.patch("app.db.repositories.patients.find_probable_duplicate",
                        side_effect=scoped_probe), \
             mock.patch("app.db.repositories.patients.create",
                        return_value=a_patient(
                            doctor_id=DOCTOR_B_ID,
                            patient_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
                            external_id="P-2026-00042")) as created:
            response = self.client.post(
                "/api/v1/patients",
                json={"first_name": "Riya", "last_name": "Sharma",
                      "date_of_birth": "2001-06-11"})

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(created.call_args.args[1], DOCTOR_B_ID)
        # And the same submission *would* conflict for doctor A.
        self.assertIsNotNone(
            scoped_probe(None, DOCTOR_A_ID, "Riya", "Sharma", date(2001, 6, 11)))

    def test_a_duplicate_within_the_same_doctor_still_conflicts(self):
        """The soft check must still do its job inside one practice."""
        with mock.patch("app.db.repositories.patients.find_probable_duplicate",
                        return_value=a_patient(doctor_id=DOCTOR_B_ID)):
            response = self.client.post(
                "/api/v1/patients",
                json={"first_name": "Riya", "last_name": "Sharma",
                      "date_of_birth": "2001-06-11"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "duplicate_patient")

    def test_external_id_collision_across_doctors_discloses_nothing(self):
        """external_id stays globally unique because it is system-generated. The
        leak was never the constraint — it was the 409 body naming the holder's
        patient_id. A collision with another doctor's number now reports only
        that it is unavailable."""
        with mock.patch("app.db.repositories.patients.get_by_external_id",
                        return_value=None), \
             mock.patch("app.db.repositories.patients.external_id_taken",
                        return_value=True):
            response = self.client.post(
                "/api/v1/patients",
                json={"first_name": "Riya", "date_of_birth": "2001-06-11",
                      "external_id": "P-2026-00009"})
        self.assertEqual(response.status_code, 409, response.text)
        details = response.json()["details"]
        self.assertEqual(details["match_reason"], "external_id_unavailable")
        self.assertNotIn("existing_patient_id", details)
        self.assertNotIn("existing_external_id", details)
        self.assertNotIn(PATIENT_ID, response.text)

    # -- the client cannot name its own tenant ------------------------------ #

    def test_doctor_id_in_the_body_is_ignored(self):
        """A client claiming to be a different doctor must not work. doctor_id is
        not a field on any request schema, so Pydantic drops it and the scope
        still comes from the resolved session."""
        with mock.patch("app.db.repositories.patients.find_probable_duplicate",
                        return_value=None), \
             mock.patch("app.db.repositories.patients.create",
                        return_value=a_patient(doctor_id=DOCTOR_B_ID)) as created:
            response = self.client.post(
                "/api/v1/patients",
                json={"first_name": "Riya", "date_of_birth": "2001-06-11",
                      "doctor_id": DOCTOR_A_ID})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(created.call_args.args[1], DOCTOR_B_ID)

    def test_doctor_id_as_a_query_param_is_ignored(self):
        with mock.patch("app.db.repositories.patients.list_all",
                        return_value=[]) as listed, \
             mock.patch("app.db.repositories.patients.count_all", return_value=0):
            self.client.get(f"/api/v1/patients?doctor_id={DOCTOR_A_ID}")
        self.assertEqual(listed.call_args.args[1], DOCTOR_B_ID)

    def test_no_request_schema_accepts_a_doctor_id(self):
        """Structural, so a future schema addition trips this instead of silently
        creating a way to claim another tenant."""
        from app.schemas.patients import PatientCreate, PatientUpdate
        from app.schemas.visits import VisitCreate, VisitUpdate

        for model in (PatientCreate, PatientUpdate, VisitCreate, VisitUpdate):
            for field in model.model_fields:
                self.assertNotIn(
                    "doctor", field.lower(),
                    f"{model.__name__}.{field} would let a client name its tenant")

    def test_openapi_exposes_no_doctor_id_parameter(self):
        """Nor may it arrive as a documented query or path parameter."""
        schema = self.client.get("/openapi.json").json()
        for path, methods in schema["paths"].items():
            for method, spec in methods.items():
                for param in spec.get("parameters", []):
                    self.assertNotIn(
                        "doctor", param["name"].lower(),
                        f"{method.upper()} {path} accepts {param['name']}")


import pathlib  # noqa: E402  used by the migration assertions below


class TestDoctorResolutionPrefersTheStableId(unittest.TestCase):
    """The email fallback is one-way: it can claim a row, never steal one.

    `resolve()` prefers `auth_user_id` because email is mutable, then falls back
    to email because email is the only thing knowable before a first sign-in
    (§8.12). The fallback had no upper bound: a row already bound to auth id A
    was still returned to a request presenting auth id B with that email, so any
    caller naming the right address resolved the tenant regardless of which
    account they were signed in as. `claim()` guarded the *write* — it will not
    re-point a claimed row — but nothing guarded the *read*.
    """

    def setUp(self) -> None:
        from app.db.repositories import doctors as doctors_repo

        self.repo = doctors_repo
        self.conn = mock.MagicMock()

    def _row(self, **over):
        from app.db.models import DoctorRow

        fields = dict(doctor_id=DOCTOR_A_ID, email=DOCTOR_A_EMAIL,
                      auth_user_id=DOCTOR_A_AUTH_ID, name="Doctor A")
        fields.update(over)
        return DoctorRow(**fields)

    def test_a_known_auth_id_resolves_without_touching_email(self):
        with mock.patch.object(self.repo, "get_by_auth_user_id",
                               return_value=self._row()), \
             mock.patch.object(self.repo, "get_by_email") as by_email:
            found = self.repo.resolve(self.conn, auth_user_id=DOCTOR_A_AUTH_ID,
                                      email=DOCTOR_A_EMAIL)
        self.assertEqual(found.doctor_id, DOCTOR_A_ID)
        by_email.assert_not_called()

    def test_an_unclaimed_row_is_still_claimed_by_email(self):
        """The seeded first doctor has no auth_user_id until they sign in."""
        unclaimed = self._row(auth_user_id=None)
        claimed = self._row(auth_user_id=DOCTOR_A_AUTH_ID)
        with mock.patch.object(self.repo, "get_by_auth_user_id",
                               return_value=None), \
             mock.patch.object(self.repo, "get_by_email",
                               return_value=unclaimed), \
             mock.patch.object(self.repo, "claim",
                               return_value=claimed) as claim:
            found = self.repo.resolve(self.conn, auth_user_id=DOCTOR_A_AUTH_ID,
                                      email=DOCTOR_A_EMAIL)
        claim.assert_called_once_with(self.conn, DOCTOR_A_ID, DOCTOR_A_AUTH_ID)
        self.assertEqual(found.auth_user_id, DOCTOR_A_AUTH_ID)

    def test_a_row_claimed_by_another_account_is_refused(self):
        """The whole point. None here becomes a 401 at the dependency."""
        with mock.patch.object(self.repo, "get_by_auth_user_id",
                               return_value=None), \
             mock.patch.object(self.repo, "get_by_email",
                               return_value=self._row()), \
             mock.patch.object(self.repo, "claim") as claim, \
             mock.patch.object(self.repo, "create") as create:
            found = self.repo.resolve(self.conn,
                                      auth_user_id=DOCTOR_B_AUTH_ID,
                                      email=DOCTOR_A_EMAIL)
        self.assertIsNone(found)
        claim.assert_not_called()
        create.assert_not_called()

    def test_email_alone_still_resolves_a_claimed_row(self):
        """No auth id to conflict with — this is the CLI and the seeded path."""
        with mock.patch.object(self.repo, "get_by_email",
                               return_value=self._row()):
            found = self.repo.resolve(self.conn, auth_user_id=None,
                                      email=DOCTOR_A_EMAIL)
        self.assertEqual(found.doctor_id, DOCTOR_A_ID)


class TestCancellationReasonIsStored(ApiTestCase):
    """`reason` used to reach a log line and stop there.

    The endpoint accepted it, the schema documented it, and nothing kept it — so
    the only answer to "why was this consultation cancelled" was whichever log
    file still existed.
    """

    def test_the_reason_reaches_the_service(self):
        with mock.patch("app.services.visit_service.cancel_visit",
                        return_value=a_visit(visit_status="cancelled")) as cancel:
            response = self.client.post(f"/api/v1/visits/{VISIT_ID}/cancel",
                                        json={"reason": "Patient rescheduled"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(cancel.call_args.kwargs["reason"],
                         "Patient rescheduled")

    def test_the_service_hands_it_to_the_repository(self):
        from app.services import visit_service

        cancelled = a_visit(visit_status="cancelled",
                            cancelled_reason="Patient rescheduled")
        with mock.patch("app.services.visit_service.visits_repo") as repo, \
             mock.patch("app.services.visit_service.insights_repo") as insights, \
             mock.patch("app.services.visit_service.follow_up_service"):
            repo.get.return_value = a_visit()
            repo.mark_cancelled.return_value = cancelled
            insights.get_for_visit.return_value = None
            visit_service.cancel_visit(mock.MagicMock(), DOCTOR_A_ID, VISIT_ID,
                                       reason="  Patient rescheduled  ")
        self.assertEqual(repo.mark_cancelled.call_args.args[-1],
                         "Patient rescheduled")

    def test_a_blank_reason_is_stored_as_null_not_empty(self):
        """NULL means 'not recorded'; '' would claim 'cancelled, no reason'."""
        from app.services import visit_service

        with mock.patch("app.services.visit_service.visits_repo") as repo, \
             mock.patch("app.services.visit_service.insights_repo") as insights, \
             mock.patch("app.services.visit_service.follow_up_service"):
            repo.get.return_value = a_visit()
            repo.mark_cancelled.return_value = a_visit(visit_status="cancelled")
            insights.get_for_visit.return_value = None
            visit_service.cancel_visit(mock.MagicMock(), DOCTOR_A_ID, VISIT_ID,
                                       reason="   ")
        self.assertIsNone(repo.mark_cancelled.call_args.args[-1])

    def test_it_is_returned_on_the_visit(self):
        with mock.patch("app.services.visit_service.get_visit",
                        return_value=a_visit(visit_status="cancelled",
                                             cancelled_reason="Patient away")):
            response = self.client.get(f"/api/v1/visits/{VISIT_ID}")
        self.assertEqual(response.json()["cancelled_reason"], "Patient away")

    def test_the_cancel_write_records_it(self):
        """Read the SQL, so dropping the column from the UPDATE fails here."""
        import inspect

        from app.db.repositories import visits as visits_repo

        source = " ".join(inspect.getsource(visits_repo.mark_cancelled).split())
        self.assertIn("cancelled_reason = %(cancelled_reason)s", source)


class TestHealthDoesNotLeakOrLie(ApiTestCase):
    """The one route with no auth in front of it.

    It reported the raw psycopg exception, which carries host, user and database
    name. And its `degraded` branch was unreachable for the failure it exists to
    report: the connection came from `Depends(get_db)`, so an unreachable
    database raised out of the dependency and returned a bare 500 before the
    handler ran.
    """

    def test_a_healthy_database_reports_ok(self):
        with mock.patch("app.api.v1.health.connection"):
            response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["database"], "ok")

    def test_an_unreachable_database_is_degraded_not_a_500(self):
        with mock.patch("app.api.v1.health.connection",
                        side_effect=RuntimeError("connection to server at "
                                                 "\"ep-secret.neon.tech\" "
                                                 "user=admin failed")):
            response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "degraded")

    def test_the_exception_text_is_not_returned(self):
        with mock.patch("app.api.v1.health.connection",
                        side_effect=RuntimeError("connection to server at "
                                                 "\"ep-secret.neon.tech\" "
                                                 "user=admin failed")):
            response = self.client.get("/api/v1/health")
        body = response.text
        self.assertNotIn("ep-secret", body)
        self.assertNotIn("user=admin", body)
        self.assertEqual(response.json()["database"], "error")


class TestScopedRepositorySql(unittest.TestCase):
    """The other half of the boundary: the SQL itself must carry a doctor filter.

    TestCrossTenantIsolation proves the API hands the right doctor_id down. These
    prove the queries actually use it — so a refactor that kept the argument but
    stopped putting it in the WHERE clause fails here rather than silently
    reopening every leak.
    """

    @staticmethod
    def source(module, name: str) -> str:
        """A function's source with whitespace collapsed.

        Normalised because these helpers are called across line breaks —
        `_scoped(\n    scope, ...)` — and a literal substring match would pass or
        fail on formatting rather than on whether the filter is applied.
        """
        import inspect
        import re

        return re.sub(r"\s+", " ", inspect.getsource(getattr(module, name)))

    def assert_applies_tenancy(self, module, name: str, *helpers: str) -> None:
        """Assert a repository function routes its scope through one of `helpers`."""
        sql = self.source(module, name)
        self.assertTrue(
            any(f"{helper}( scope" in sql or f"{helper}(scope" in sql
                for helper in helpers),
            f"{module.__name__.rsplit('.', 1)[-1]}.{name} does not apply the "
            f"tenancy filter (looked for {', '.join(helpers)})")

    def test_every_patient_query_filters_on_doctor_id(self):
        from app.db.repositories import patients as repo

        for name in ("get", "get_by_external_id", "find_probable_duplicate",
                     "list_all", "count_all", "update", "count"):
            with self.subTest(function=name):
                self.assert_applies_tenancy(repo, name, "clause", "_where")

    def test_probable_duplicate_keys_on_doctor_plus_name_plus_dob(self):
        """The §8.10 fix, asserted on the query rather than on a call."""
        from app.db.repositories import patients as repo

        sql = self.source(repo, "find_probable_duplicate")
        self.assertIn("_where(", sql)                      # tenancy first
        self.assertIn("lower(btrim(p.first_name))", sql)
        self.assertIn("p.date_of_birth = %s", sql)
        # And the index behind it is doctor-first.
        migration = pathlib.Path("sql/7_multi_tenancy.sql").read_text()
        self.assertIn("idx_patients_doctor_identity", migration)
        self.assertIn("DROP INDEX IF EXISTS idx_patients_identity", migration)

    def test_patient_create_takes_doctor_id_from_the_scope(self):
        """Not from a keyword argument a caller could supply."""
        from app.db.repositories import patients as repo

        sql = self.source(repo, "create")
        self.assertIn("doctor_id_of(scope)", sql)
        self.assertIn('"doctor_id"', sql)

    def test_visit_writes_confine_themselves_to_the_scoped_doctor(self):
        from app.db.repositories import visits as repo

        self.assertIn("p.doctor_id = %(scope_doctor_id)s", repo._OWNED_BY_SCOPE)
        for name in ("update", "set_transcript", "clear_transcript",
                     "mark_in_progress", "mark_completed", "mark_cancelled"):
            with self.subTest(function=name):
                self.assert_applies_tenancy(repo, name, "_write_conditions")

    def test_audio_upload_queries_derive_tenancy_through_the_visit(self):
        """Every lookup that starts from a client-supplied upload or visit id,
        including the live recording's claim (§8.24)."""
        from app.db.repositories import audio_uploads as repo

        self.assertIn("JOIN patients p ON p.patient_id = v.patient_id",
                      repo._OWNED_BY_SCOPE)
        self.assertIn("p.doctor_id = %(scope_doctor_id)s", repo._OWNED_BY_SCOPE)
        for name in ("get", "latest_for_visit", "unfinished_for_visit",
                     "record_chunk", "claim", "claim_live"):
            with self.subTest(function=name):
                self.assert_applies_tenancy(repo, name, "_scoped")
        self.assertIn("p.doctor_id = %(scope_doctor_id)s",
                      self.source(repo, "create"))

    def test_the_live_claim_rechecks_the_chunks_it_planned_from(self):
        """A chunk landing between reading the chunks and claiming them must
        make the claim match nothing, not produce a plan that is wrong."""
        from app.db.repositories import audio_uploads as repo

        sql = self.source(repo, "claim_live")
        self.assertIn("u.chunk_count IS NULL", sql)
        self.assertIn("count(*) FROM audio_upload_chunks", sql)
        self.assertIn("sum(c.size_bytes)", sql)
        self.assertIn("c.chunk_index >= %(chunk_count)s", sql)

    def test_the_live_migration_keeps_the_cover_constraint(self):
        """A live upload gets its plan late, not a weaker one."""
        migration = pathlib.Path("sql/10_live_recordings.sql").read_text()
        self.assertIn("audio_uploads_plan_known", migration)
        self.assertIn("status IN ('receiving', 'abandoned')", migration)
        self.assertNotIn("DROP CONSTRAINT", migration.upper())

    def test_visit_create_resolves_its_patient_under_the_filter(self):
        """INSERT ... SELECT FROM patients, so another doctor's patient_id
        inserts nothing rather than planting a visit in their records."""
        from app.db.repositories import visits as repo

        sql = self.source(repo, "create")
        self.assertIn("FROM patients p", sql)
        self.assertIn("p.doctor_id", sql)
        self.assertIn("SELECT p.patient_id", sql)

    def test_insight_reads_and_writes_derive_tenancy_through_the_visit(self):
        from app.db.repositories import insights as repo

        self.assertIn("JOIN patients p ON p.patient_id = v.patient_id",
                      repo._OWNED_BY_SCOPE)
        self.assertIn("p.doctor_id = %s", repo._OWNED_BY_SCOPE)
        for name in ("get_for_visit", "list_for_visits"):
            with self.subTest(function=name):
                self.assertIn("_OWNED_BY_SCOPE", self.source(repo, name))
        # The write re-checks inside its own transaction.
        self.assertIn("p.doctor_id = %s", self.source(repo, "save"))

    def test_follow_up_reads_and_writes_filter_on_doctor(self):
        from app.db.repositories import follow_ups as repo

        self.assertIn("p.doctor_id = %(scope_doctor_id)s", repo._OWNED_BY_SCOPE)
        for name in ("get", "get_for_visit", "list_for_visits", "_filters"):
            with self.subTest(function=name):
                self.assert_applies_tenancy(repo, name, "clause")
        for name in ("delete_for_visit", "reopen_resolved_by_visit",
                     "apply_update"):
            with self.subTest(function=name):
                self.assertIn("_OWNED_BY_SCOPE", self.source(repo, name))
        self.assertIn("p.doctor_id", self.source(repo, "resolve_open_for_patient"))

    def test_follow_up_count_joins_patients_to_reach_doctor_id(self):
        """It used to read `FROM follow_ups f` alone, so its total counted every
        doctor's queue while the list beside it showed one."""
        from app.db.repositories import follow_ups as repo

        self.assertIn("JOIN patients p USING (patient_id)",
                      self.source(repo, "count_follow_ups"))

    def test_job_reads_derive_tenancy_through_the_visit(self):
        from app.db.repositories import jobs as repo

        self.assertIn("p.doctor_id = %(scope_doctor_id)s", repo._OWNED_BY_SCOPE)
        for name in ("get", "active_for_visit", "latest_for_visit", "list_recent"):
            with self.subTest(function=name):
                self.assert_applies_tenancy(repo, name, "_scoped")

    def test_counters_are_scoped_at_the_query(self):
        from app.db.repositories import follow_ups as fu_repo
        from app.db.repositories import patients as p_repo
        from app.db.repositories import visits as v_repo

        self.assert_applies_tenancy(p_repo, "count", "clause")
        self.assert_applies_tenancy(v_repo, "counts", "clause")
        self.assert_applies_tenancy(fu_repo, "counts", "clause")
        self.assertIn("JOIN patients p USING (patient_id)",
                      self.source(v_repo, "counts"))
        self.assertIn("JOIN patients p USING (patient_id)",
                      self.source(fu_repo, "counts"))

    def test_no_api_module_can_query_across_doctors(self):
        """ALL_DOCTORS is for the CLI and startup reaping. Nothing serving HTTP
        may use it — that would be a deliberate leak with a helper for it."""
        import pathlib

        offenders = []
        for path in pathlib.Path("app/api").rglob("*.py"):
            if "ALL_DOCTORS" in path.read_text():
                offenders.append(str(path))
        self.assertEqual(offenders, [],
                         "ALL_DOCTORS must not appear under app/api/")

    def test_routers_go_through_services_not_repositories(self):
        """`api -> services -> db`, for reads as well as writes.

        Every list endpoint used to call its repository directly and do its own
        count, which left the page size, the cap, and the pairing of list with
        count written out once per endpoint and held together by nothing. That is
        how three screens ended up each choosing their own limit and filtering the
        remainder in the browser.

        `deps.py` is the one exception and is not a route: require_doctor is what
        *establishes* the scope every service below it takes as an argument, so it
        has to reach `doctors` before any service exists to ask.
        """
        import pathlib

        api_dir = pathlib.Path(__file__).resolve().parent.parent / "app" / "api"
        offenders = [
            path.relative_to(api_dir).as_posix()
            for path in api_dir.rglob("*.py")
            if path.name != "deps.py"
            and "from app.db.repositories" in path.read_text()
        ]
        self.assertEqual(offenders, [], f"routers reaching past services: {offenders}")

    def test_all_doctors_sentinel_cannot_be_reached_by_accident(self):
        """None must raise rather than quietly disabling the filter, so an unset
        variable can never widen a query."""
        from app.db.tenancy import ALL_DOCTORS, clause, doctor_id_of, is_scoped

        self.assertFalse(is_scoped(ALL_DOCTORS))
        self.assertEqual(clause(ALL_DOCTORS), ([], []))
        for bad in (None, "", 0, False, []):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    is_scoped(bad)  # type: ignore[arg-type]
        # A write cannot be cross-doctor: there is no owner to record.
        with self.assertRaises(ValueError):
            doctor_id_of(ALL_DOCTORS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
