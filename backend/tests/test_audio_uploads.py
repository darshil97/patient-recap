"""
Recording uploads, and live recordings sent while they are recorded (§8.24).

Offline: the repository is a mock, storage is a temporary directory, and nothing
calls Deepgram or a model. Route status codes and tenancy are in test_api.py with
the other endpoints; this file covers the rules themselves.

Run from backend/:
    python -m unittest discover -s tests -t .
"""

import sys
import tempfile
import unittest
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.core.errors import (  # noqa: E402
    ConflictError,
    TranscriptError,
    TranscriptTooLargeError,
    ValidationError,
)
from app.db.models import AudioUploadRow, VisitRow  # noqa: E402
from app.domain.audio import (  # noqa: E402
    check_live_chunk,
    chunk_count,
    expected_chunk_size,
    live_plan,
)
from app.schemas.transcripts import AudioUploadOut  # noqa: E402
from app.services import audio_upload_service, visit_service  # noqa: E402
from app.storage.audio import LocalAudioStore, chunk_locator  # noqa: E402

DOCTOR_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
VISIT_ID = "22222222-2222-2222-2222-222222222222"
UPLOAD_ID = "55555555-5555-5555-5555-555555555555"
OTHER_UPLOAD_ID = "66666666-6666-6666-6666-666666666666"

LIVE = 256 * 1024          # speech.live_chunk_bytes
CEILING = 100 * 1024 * 1024  # speech.max_upload_bytes


def a_visit(**over: Any) -> VisitRow:
    fields: Dict[str, Any] = dict(
        visit_id=VISIT_ID, patient_id="11111111-1111-1111-1111-111111111111",
        visit_number=1, visit_date=date(2026, 10, 10), visit_time=time(10, 0),
        visit_type="initial", visit_status="in_progress", transcript_path=None,
        transcript_provider=None, insights_generated=False, doctor_id=DOCTOR_ID,
        first_name="Riya", last_name="Sharma")
    fields.update(over)
    return VisitRow(**fields)


def a_live_upload(**over: Any) -> AudioUploadRow:
    fields: Dict[str, Any] = dict(
        upload_id=UPLOAD_ID, visit_id=VISIT_ID, status="receiving",
        filename="consultation.webm", extension=".webm", total_bytes=None,
        chunk_bytes=LIVE, chunk_count=None, replace_transcript=False,
        storage_provider="local", audio_path=None, duration_seconds=None,
        speakers=None, warnings=None, error_code=None, error_message=None,
        mode="live")
    fields.update(over)
    return AudioUploadRow(**fields)


def a_file_upload(**over: Any) -> AudioUploadRow:
    fields: Dict[str, Any] = dict(
        upload_id=UPLOAD_ID, visit_id=VISIT_ID, status="receiving",
        filename="visit.mp3", extension=".mp3", total_bytes=10_000_000,
        chunk_bytes=4_000_000, chunk_count=3, replace_transcript=False,
        storage_provider="local", audio_path=None, duration_seconds=None,
        speakers=None, warnings=None, error_code=None, error_message=None,
        mode="file")
    fields.update(over)
    return AudioUploadRow(**fields)


# --------------------------------------------------------------------------- #
# Domain: the plan of a recording whose size was not known when it started
# --------------------------------------------------------------------------- #

class TestLivePlan(unittest.TestCase):

    def test_full_chunks_and_a_short_last_one(self):
        self.assertEqual(live_plan({0: LIVE, 1: LIVE, 2: 10}, LIVE),
                         (2 * LIVE + 10, 3))

    def test_an_exact_multiple_ends_on_a_full_chunk(self):
        """The recorder flushes nothing extra when the last slice was exactly
        full, so the last chunk being full size must be accepted."""
        self.assertEqual(live_plan({0: LIVE, 1: LIVE}, LIVE), (2 * LIVE, 2))

    def test_a_recording_shorter_than_one_chunk(self):
        self.assertEqual(live_plan({0: 1234}, LIVE), (1234, 1))

    def test_nothing_recorded_is_refused(self):
        with self.assertRaises(TranscriptError):
            live_plan({}, LIVE)

    def test_a_gap_is_a_conflict_naming_what_is_missing(self):
        with self.assertRaises(ConflictError) as ctx:
            live_plan({0: LIVE, 2: 10}, LIVE)
        self.assertEqual(ctx.exception.details["missing"], [1])

    def test_declared_count_finds_chunks_still_in_flight(self):
        """The recorder says it sent 4; the server has 3. Resend, not process."""
        with self.assertRaises(ConflictError) as ctx:
            live_plan({0: LIVE, 1: LIVE, 2: LIVE}, LIVE, declared_count=4)
        self.assertEqual(ctx.exception.details["missing"], [3])

    def test_declared_count_lower_than_what_arrived_is_refused(self):
        """Processing 2 of 3 stored chunks would drop the end of the
        consultation without saying so."""
        with self.assertRaises(ConflictError):
            live_plan({0: LIVE, 1: LIVE, 2: 10}, LIVE, declared_count=2)

    def test_declared_count_matching_is_the_plan(self):
        self.assertEqual(live_plan({0: LIVE, 1: 99}, LIVE, declared_count=2),
                         (LIVE + 99, 2))

    def test_a_short_chunk_before_the_last_is_refused(self):
        """It would make the stitched file something other than the recording."""
        with self.assertRaises(ValidationError) as ctx:
            live_plan({0: LIVE, 1: 100, 2: 10}, LIVE)
        self.assertEqual(ctx.exception.details["short"], [1])

    def test_the_plan_satisfies_the_file_upload_rules(self):
        """Whatever live_plan returns must be a plan the table's cover
        constraint and the stitcher's expected_chunk_size both agree with,
        because from finish onwards it is treated as an ordinary upload."""
        for sizes in ({0: 1}, {0: LIVE}, {0: LIVE, 1: 1},
                      {i: LIVE for i in range(7)} | {7: 12345}):
            with self.subTest(chunks=len(sizes)):
                total, count = live_plan(sizes, LIVE)
                self.assertEqual(chunk_count(total, LIVE), count)
                for index, size in sizes.items():
                    self.assertEqual(expected_chunk_size(index, total, LIVE), size)


class TestCheckLiveChunk(unittest.TestCase):

    def test_any_size_up_to_a_chunk_is_accepted(self):
        for size in (1, 1000, LIVE):
            check_live_chunk(0, size, LIVE, CEILING)

    def test_empty_is_refused(self):
        with self.assertRaises(ValidationError):
            check_live_chunk(3, 0, LIVE, CEILING)

    def test_larger_than_a_chunk_is_refused(self):
        with self.assertRaises(ValidationError):
            check_live_chunk(0, LIVE + 1, LIVE, CEILING)

    def test_the_ceiling_is_enforced_as_the_recording_grows(self):
        last = CEILING // LIVE - 1          # the chunk that ends exactly at it
        check_live_chunk(last, LIVE, LIVE, CEILING)
        with self.assertRaises(TranscriptTooLargeError):
            check_live_chunk(last + 1, 1, LIVE, CEILING)


# --------------------------------------------------------------------------- #
# Rows and responses without a plan
# --------------------------------------------------------------------------- #

class TestUploadWithoutAPlan(unittest.TestCase):

    def test_a_receiving_live_upload_is_not_complete(self):
        upload = a_live_upload(chunks_received=5)
        self.assertFalse(upload.plan_known)
        self.assertFalse(upload.all_chunks_received)
        self.assertFalse(upload.retryable)

    def test_it_serialises_with_a_null_plan(self):
        when = datetime(2026, 10, 10, 10, 41, tzinfo=timezone.utc)
        out = AudioUploadOut.from_row(
            a_live_upload(chunks_received=2, bytes_received=LIVE + 10,
                          last_chunk_at=when))
        self.assertEqual(out.mode, "live")
        self.assertIsNone(out.size_bytes)
        self.assertIsNone(out.chunk_count)
        self.assertEqual(out.bytes_received, LIVE + 10)
        self.assertEqual(out.last_chunk_at, when)

    def test_a_file_upload_still_reports_its_plan(self):
        out = AudioUploadOut.from_row(a_file_upload())
        self.assertEqual(out.mode, "file")
        self.assertEqual((out.size_bytes, out.chunk_count), (10_000_000, 3))


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #

class ServiceTestCase(unittest.TestCase):
    """audio_upload_service with its repository, visit lookup and settings
    replaced. `self.repo` is the mocked audio_uploads repository."""

    visit = a_visit()

    def setUp(self) -> None:
        patches = {
            "uploads_repo": mock.MagicMock(),
            "get_visit": mock.MagicMock(side_effect=lambda *a, **k: self.visit),
            "get_settings": mock.MagicMock(return_value=Settings()),
            "_require_processing_configured": mock.MagicMock(),
            "default_provider": mock.MagicMock(return_value="local"),
            "get_audio_store": mock.MagicMock(),
        }
        for name, value in patches.items():
            patcher = mock.patch.object(audio_upload_service, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.repo = audio_upload_service.uploads_repo
        self.store = audio_upload_service.get_audio_store.return_value
        self.repo.unfinished_for_visit.return_value = []
        self.repo.chunk_sizes.return_value = {}
        self.conn = mock.MagicMock()


class TestStartLiveUpload(ServiceTestCase):

    def start(self, **kwargs):
        return audio_upload_service.start_live_upload(
            self.conn, DOCTOR_ID, VISIT_ID,
            filename=kwargs.pop("filename", "consultation.webm"), **kwargs)

    def test_opens_a_live_upload_with_no_plan(self):
        self.repo.create.return_value = a_live_upload()
        self.start()
        call = self.repo.create.call_args
        self.assertEqual(call.args[1], DOCTOR_ID, "created outside the tenant")
        self.assertEqual(call.kwargs["mode"], "live")
        self.assertIsNone(call.kwargs["total_bytes"])
        self.assertIsNone(call.kwargs["chunk_count"])
        self.assertEqual(call.kwargs["chunk_bytes"], LIVE)
        self.assertEqual(call.kwargs["extension"], ".webm")

    def test_needs_a_started_session(self):
        for status in ("scheduled", "completed", "cancelled"):
            with self.subTest(status=status):
                self.visit = a_visit(visit_status=status)
                with self.assertRaises(ConflictError):
                    self.start()
        self.repo.create.assert_not_called()

    def test_refuses_over_an_existing_transcript_unless_replacing(self):
        self.visit = a_visit(transcript_path=f"transcripts/{VISIT_ID}.txt")
        with self.assertRaises(ConflictError):
            self.start()
        self.repo.create.return_value = a_live_upload()
        self.start(replace=True)
        self.assertTrue(self.repo.create.call_args.kwargs["replace_transcript"])

    def test_safari_and_firefox_formats_are_accepted(self):
        self.repo.create.return_value = a_live_upload()
        for name in ("consultation.mp4", "consultation.ogg", "consultation.m4a"):
            with self.subTest(name=name):
                self.start(filename=name)

    def test_an_unknown_format_is_refused_before_recording(self):
        with self.assertRaises(TranscriptError):
            self.start(filename="consultation.aiff")
        self.repo.create.assert_not_called()

    def test_a_second_live_recording_is_a_conflict_not_a_replacement(self):
        """The first may still be recording in another tab."""
        self.repo.unfinished_for_visit.return_value = [
            a_live_upload(upload_id=OTHER_UPLOAD_ID)]
        with self.assertRaises(ConflictError) as ctx:
            self.start()
        self.assertEqual(ctx.exception.details["upload_id"], OTHER_UPLOAD_ID)
        self.repo.mark_abandoned.assert_not_called()
        self.repo.create.assert_not_called()

    def test_refused_while_another_recording_is_processing(self):
        self.repo.unfinished_for_visit.return_value = [
            a_file_upload(status="processing")]
        with self.assertRaises(ConflictError):
            self.start()
        self.repo.create.assert_not_called()


class TestFileUploadLeavesALiveRecordingAlone(ServiceTestCase):
    """Starting a file upload discards whatever is unfinished for the visit.
    That must never include a live recording: it may be a consultation still
    being recorded."""

    visit = a_visit(visit_status="completed")

    def start(self):
        return audio_upload_service.start_upload(
            self.conn, DOCTOR_ID, VISIT_ID, filename="visit.mp3",
            size_bytes=10_000_000)

    def test_a_live_recording_still_receiving_is_a_conflict(self):
        self.repo.unfinished_for_visit.return_value = [a_live_upload()]
        with self.assertRaises(ConflictError):
            self.start()
        self.repo.mark_abandoned.assert_not_called()
        self.repo.create.assert_not_called()

    def test_an_unfinished_file_upload_is_still_discarded(self):
        """Unchanged behaviour: a closed tab's half-sent file is replaced."""
        stale = a_file_upload(upload_id=OTHER_UPLOAD_ID)
        self.repo.unfinished_for_visit.return_value = [stale]
        self.repo.chunk_sizes.return_value = {0: 4_000_000}
        self.repo.create.return_value = a_file_upload()
        self.start()
        self.repo.mark_abandoned.assert_called_once_with(self.conn, [OTHER_UPLOAD_ID])
        self.assertEqual(self.repo.create.call_args.kwargs["chunk_count"], 3)


class TestLiveChunks(ServiceTestCase):

    def put(self, index, data):
        return audio_upload_service.put_chunk(self.conn, DOCTOR_ID, UPLOAD_ID,
                                              index, data)

    def test_a_partial_chunk_is_stored_while_recording(self):
        self.repo.get.return_value = a_live_upload()
        self.repo.record_chunk.return_value = True
        self.put(4, b"x" * 1000)
        self.store.write_bytes.assert_called_once()
        self.assertEqual(self.store.write_bytes.call_args.args[0],
                         chunk_locator(UPLOAD_ID, 4))
        self.repo.record_chunk.assert_called_once_with(
            self.conn, DOCTOR_ID, UPLOAD_ID, 4, 1000)

    def test_an_oversized_chunk_is_refused_before_it_is_stored(self):
        self.repo.get.return_value = a_live_upload()
        with self.assertRaises(ValidationError):
            self.put(0, b"x" * (LIVE + 1))
        self.store.write_bytes.assert_not_called()

    def test_a_recording_past_the_ceiling_is_refused(self):
        self.repo.get.return_value = a_live_upload()
        with self.assertRaises(TranscriptTooLargeError):
            self.put(CEILING // LIVE, b"x")
        self.store.write_bytes.assert_not_called()

    def test_a_finished_or_discarded_recording_takes_no_more_chunks(self):
        """How a recorder learns its upload was ended elsewhere."""
        for status in ("processing", "abandoned", "completed"):
            with self.subTest(status=status):
                self.repo.get.return_value = a_live_upload(status=status)
                with self.assertRaises(ConflictError):
                    self.put(0, b"x")

    def test_file_uploads_keep_their_exact_sizes(self):
        self.repo.get.return_value = a_file_upload()
        with self.assertRaises(ValidationError):
            self.put(0, b"x" * 1000)
        with self.assertRaises(ValidationError):
            self.put(3, b"x" * 1000)        # out of the 3-chunk plan


class TestFinishLiveRecording(ServiceTestCase):

    visit = a_visit(visit_status="completed")

    def finish(self, chunk_count=None):
        return audio_upload_service.finish_upload(self.conn, DOCTOR_ID, UPLOAD_ID,
                                                  chunk_count=chunk_count)

    def test_the_plan_comes_from_what_arrived(self):
        self.repo.get.return_value = a_live_upload()
        self.repo.chunk_sizes.return_value = {0: LIVE, 1: LIVE, 2: 777}
        self.repo.claim_live.return_value = a_live_upload(status="processing")
        self.finish(chunk_count=3)
        call = self.repo.claim_live.call_args
        self.assertEqual(call.args[1], DOCTOR_ID)
        self.assertEqual(call.kwargs, {"total_bytes": 2 * LIVE + 777,
                                       "chunk_count": 3})
        self.repo.claim.assert_not_called()

    def test_an_interrupted_session_processes_whatever_arrived(self):
        self.repo.get.return_value = a_live_upload()
        self.repo.chunk_sizes.return_value = {0: LIVE, 1: 5}
        self.repo.claim_live.return_value = a_live_upload(status="processing")
        self.finish()
        self.assertEqual(self.repo.claim_live.call_args.kwargs["chunk_count"], 2)

    def test_the_visit_must_be_completed_first(self):
        """Refused while the recording is intact, rather than after paying for
        speech-to-text on a transcript the visit cannot accept."""
        self.visit = a_visit(visit_status="in_progress")
        self.repo.get.return_value = a_live_upload()
        self.repo.chunk_sizes.return_value = {0: 5}
        with self.assertRaises(ConflictError):
            self.finish(chunk_count=1)
        self.repo.claim_live.assert_not_called()

    def test_missing_chunks_name_the_upload_so_they_can_be_resent(self):
        self.repo.get.return_value = a_live_upload()
        self.repo.chunk_sizes.return_value = {0: LIVE}
        with self.assertRaises(ConflictError) as ctx:
            self.finish(chunk_count=2)
        self.assertEqual(ctx.exception.details["missing"], [1])
        self.assertEqual(ctx.exception.details["upload_id"], UPLOAD_ID)
        self.repo.claim_live.assert_not_called()

    def test_a_chunk_landing_mid_finish_is_a_conflict_to_retry(self):
        self.repo.get.return_value = a_live_upload()
        self.repo.chunk_sizes.return_value = {0: 5}
        self.repo.claim_live.return_value = None
        with self.assertRaises(ConflictError):
            self.finish(chunk_count=1)

    def test_a_failed_live_recording_retries_like_any_other(self):
        """Once claimed it has a plan, so a retry takes the ordinary path."""
        self.repo.get.return_value = a_live_upload(
            status="failed", total_bytes=LIVE + 5, chunk_count=2,
            audio_path=f"recordings/{VISIT_ID}/{UPLOAD_ID}.webm")
        self.repo.claim.return_value = a_live_upload(status="processing")
        self.finish()
        self.repo.claim.assert_called_once()
        self.repo.claim_live.assert_not_called()

    def test_a_discarded_recording_cannot_be_finished(self):
        self.repo.get.return_value = a_live_upload(status="abandoned")
        with self.assertRaises(ConflictError):
            self.finish(chunk_count=1)

    def test_a_file_upload_must_match_its_declared_plan(self):
        self.repo.get.return_value = a_file_upload()
        with self.assertRaises(ValidationError):
            self.finish(chunk_count=2)
        self.repo.claim.assert_not_called()


class TestDiscard(ServiceTestCase):

    def discard(self):
        return audio_upload_service.discard_upload(self.conn, DOCTOR_ID, UPLOAD_ID)

    def test_removes_the_chunks_and_marks_it_abandoned(self):
        self.repo.get.side_effect = [a_live_upload(),
                                     a_live_upload(status="abandoned")]
        self.repo.chunk_sizes.return_value = {0: LIVE, 1: 10}
        result = self.discard()
        self.assertEqual(result.status, "abandoned")
        self.repo.mark_abandoned.assert_called_once_with(self.conn, [UPLOAD_ID])
        deleted = [c.args[0] for c in self.store.delete.call_args_list]
        self.assertEqual(deleted, [chunk_locator(UPLOAD_ID, 0),
                                   chunk_locator(UPLOAD_ID, 1)])

    def test_is_idempotent(self):
        self.repo.get.return_value = a_live_upload(status="abandoned")
        self.discard()
        self.repo.mark_abandoned.assert_not_called()

    def test_refused_once_processing_or_transcribed(self):
        for status in ("processing", "completed", "superseded"):
            with self.subTest(status=status):
                self.repo.get.side_effect = None
                self.repo.get.return_value = a_live_upload(status=status)
                with self.assertRaises(ConflictError):
                    self.discard()
        self.repo.mark_abandoned.assert_not_called()

    def test_for_a_cancelled_visit_skips_one_processing(self):
        self.repo.unfinished_for_visit.return_value = [
            a_live_upload(), a_file_upload(upload_id=OTHER_UPLOAD_ID,
                                           status="processing")]
        count = audio_upload_service.discard_unfinished_for_visit(
            self.conn, DOCTOR_ID, VISIT_ID)
        self.assertEqual(count, 1)
        self.repo.mark_abandoned.assert_called_once_with(self.conn, [UPLOAD_ID])


class TestLiveRecordingStitchesByteForByte(unittest.TestCase):
    """A plan fixed at finish feeds the unchanged stitcher, against real files."""

    def test_stitched_recording_is_the_bytes_that_were_sent(self):
        recording = bytes(range(256)) * 2100           # 537,600 bytes
        pieces = [recording[i:i + LIVE] for i in range(0, len(recording), LIVE)]

        with tempfile.TemporaryDirectory() as root:
            store = LocalAudioStore(Path(root))
            for index, piece in enumerate(pieces):
                store.write_bytes(chunk_locator(UPLOAD_ID, index), piece,
                                  "application/octet-stream")
            total, count = live_plan({i: len(p) for i, p in enumerate(pieces)},
                                     LIVE, declared_count=len(pieces))
            upload = a_live_upload(status="processing", total_bytes=total,
                                   chunk_count=count)

            with mock.patch.object(audio_upload_service, "get_audio_store",
                                   return_value=store), \
                 mock.patch.object(audio_upload_service, "uploads_repo") as repo:
                target = audio_upload_service._stitch(mock.MagicMock(), upload)

            with store.open(target) as stitched:
                self.assertEqual(stitched.read(), recording)
            repo.set_audio_path.assert_called_once()
            self.assertIsNone(store.size(chunk_locator(UPLOAD_ID, 0)),
                              "chunks should be removed once stitched")


# --------------------------------------------------------------------------- #
# Visit lifecycle: starting a session, and cancelling one
# --------------------------------------------------------------------------- #

class TestStartVisit(unittest.TestCase):

    def setUp(self) -> None:
        patcher = mock.patch.object(visit_service, "visits_repo")
        self.repo = patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = mock.MagicMock()

    def start(self):
        return visit_service.start_visit(self.conn, DOCTOR_ID, VISIT_ID)

    def test_a_scheduled_visit_moves_to_in_progress(self):
        self.repo.get.return_value = a_visit(visit_status="scheduled")
        self.repo.mark_in_progress.return_value = a_visit(visit_status="in_progress")
        self.assertEqual(self.start().visit_status, "in_progress")
        self.repo.mark_in_progress.assert_called_once_with(self.conn, DOCTOR_ID,
                                                           VISIT_ID)

    def test_starting_twice_changes_nothing(self):
        self.repo.get.return_value = a_visit(visit_status="in_progress")
        self.assertEqual(self.start().visit_status, "in_progress")
        self.repo.mark_in_progress.assert_not_called()

    def test_a_finished_consultation_cannot_be_restarted(self):
        for status in ("completed", "cancelled"):
            with self.subTest(status=status):
                self.repo.get.return_value = a_visit(visit_status=status)
                with self.assertRaises(ConflictError):
                    self.start()
        self.repo.mark_in_progress.assert_not_called()

    def test_a_visit_completed_in_between_is_a_conflict(self):
        """The UPDATE's own status condition left it alone; say so."""
        self.repo.get.return_value = a_visit(visit_status="scheduled")
        self.repo.mark_in_progress.return_value = a_visit(visit_status="completed")
        with self.assertRaises(ConflictError):
            self.start()

    def test_the_write_carries_its_own_status_condition(self):
        import inspect
        import re

        from app.db.repositories import visits as repo

        source = re.sub(r"\s+", " ", inspect.getsource(repo.mark_in_progress))
        self.assertIn("_write_conditions(scope, visit_id)", source)
        self.assertIn("visit_status IN ('scheduled', 'in_progress')", source)


class TestCancelDiscardsRecordings(unittest.TestCase):

    def setUp(self) -> None:
        for target, value in (
                ("visits_repo", mock.MagicMock()),
                ("insights_repo", mock.MagicMock()),
                ("follow_up_service", mock.MagicMock())):
            patcher = mock.patch.object(visit_service, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        visit_service.visits_repo.get.return_value = a_visit()
        visit_service.insights_repo.get_for_visit.return_value = None
        visit_service.follow_up_service.reopen_for_cancelled_visit.return_value = []
        self.conn = mock.MagicMock()

    def test_unfinished_recordings_are_discarded(self):
        with mock.patch.object(audio_upload_service,
                               "discard_unfinished_for_visit") as discard:
            visit_service.cancel_visit(self.conn, DOCTOR_ID, VISIT_ID)
        discard.assert_called_once_with(self.conn, DOCTOR_ID, VISIT_ID)

    def test_a_failed_discard_does_not_fail_the_cancellation(self):
        with mock.patch.object(audio_upload_service, "discard_unfinished_for_visit",
                               side_effect=RuntimeError("storage down")), \
             self.assertLogs("app.services.visit_service", level="WARNING"):
            visit_service.cancel_visit(self.conn, DOCTOR_ID, VISIT_ID)
        visit_service.visits_repo.mark_cancelled.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
