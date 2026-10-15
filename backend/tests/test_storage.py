"""
Transcript storage: the locator guard, both backends, and provider routing.

Offline. The object-storage backend takes its S3 client by injection, so a fake
in-memory client covers the key handling and the error mapping without a network,
a credential, or boto3 being installed.

Run from backend/:
    python -m unittest discover -s tests -t .
"""

import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings, load_settings  # noqa: E402
from app.core.errors import NotFoundError  # noqa: E402
from app.db.models import VisitRow  # noqa: E402
from app.storage import (  # noqa: E402
    LOCAL,
    NEON_OBJECT_STORAGE,
    PROVIDERS,
    LocalTranscriptStore,
    ObjectTranscriptStore,
    StorageError,
    transcript_locator,
    validate_locator,
)
from app.storage import factory  # noqa: E402

VISIT_ID = "22222222-2222-2222-2222-222222222222"
OTHER_VISIT_ID = "33333333-3333-3333-3333-333333333333"
LOCATOR = f"transcripts/{VISIT_ID}.txt"
SAMPLE = "DOCTOR: How have you been?\nPATIENT: Better, thank you."


# --------------------------------------------------------------------------- #
# A fake S3, good enough to be honest about
# --------------------------------------------------------------------------- #

class FakeClientError(Exception):
    """Shaped like botocore's ClientError: carries a `response` dict.

    The backend reads the error code off `response` rather than importing
    ClientError, precisely so this fake is a faithful stand-in.
    """

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code},
                         "ResponseMetadata": {"HTTPStatusCode": status}}


class FakeS3:
    """In-memory S3 covering the four calls ObjectTranscriptStore makes."""

    def __init__(self, bucket: str = "test-bucket") -> None:
        self.bucket = bucket
        self.objects: Dict[str, bytes] = {}
        self.calls: list = []
        #: Set to an error code to make the next matching call fail.
        self.fail_put: Optional[str] = None
        self.fail_get: Optional[str] = None

    def _check_bucket(self, name: str) -> None:
        if name != self.bucket:
            raise FakeClientError("NoSuchBucket", 404)

    def put_object(self, Bucket: str, Key: str, Body: bytes, **kw: Any) -> Dict:
        self.calls.append(("put", Key))
        self._check_bucket(Bucket)
        if self.fail_put:
            raise FakeClientError(self.fail_put, 403)
        self.objects[Key] = Body
        return {"ETag": '"fake"'}

    def get_object(self, Bucket: str, Key: str) -> Dict:
        self.calls.append(("get", Key))
        self._check_bucket(Bucket)
        if self.fail_get:
            raise FakeClientError(self.fail_get, 403)
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey", 404)
        payload = self.objects[Key]
        return {"Body": mock.MagicMock(read=lambda: payload),
                "LastModified": datetime(2026, 9, 10, tzinfo=timezone.utc),
                "ContentLength": len(payload)}

    def head_object(self, Bucket: str, Key: str) -> Dict:
        self.calls.append(("head", Key))
        self._check_bucket(Bucket)
        if Key not in self.objects:
            raise FakeClientError("NotFound", 404)
        return {"ContentLength": len(self.objects[Key])}

    def delete_object(self, Bucket: str, Key: str) -> Dict:
        self.calls.append(("delete", Key))
        self._check_bucket(Bucket)
        self.objects.pop(Key, None)
        return {}


def a_visit(**over: Any) -> VisitRow:
    fields: Dict[str, Any] = dict(
        visit_id=VISIT_ID, patient_id="11111111-1111-1111-1111-111111111111",
        visit_number=1, visit_date=date(2026, 2, 16), visit_time=None,
        visit_type="initial", visit_status="completed",
        transcript_path=LOCATOR, transcript_provider=LOCAL,
        insights_generated=False, first_name="Riya")
    fields.update(over)
    return VisitRow(**fields)


# --------------------------------------------------------------------------- #

class TestLocatorGuard(unittest.TestCase):
    """The single place that decides what a transcript address may look like.

    Matters more since object storage arrived: on a filesystem a malformed path is
    still caught by resolve_within, but S3 has no parent directory to be confined
    to, so a bad key would simply be written. This pattern is the only thing
    standing there.
    """

    def test_locator_is_built_from_the_visit_id(self):
        self.assertEqual(transcript_locator(VISIT_ID), LOCATOR)

    def test_a_well_formed_locator_is_accepted(self):
        self.assertEqual(validate_locator(LOCATOR), LOCATOR)

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(validate_locator(f"  {LOCATOR}  "), LOCATOR)

    def test_traversal_is_refused(self):
        for bad in ("transcripts/../../etc/passwd",
                    "../etc/passwd",
                    "transcripts/../secrets.txt",
                    f"../transcripts/{VISIT_ID}.txt",
                    f"/etc/{VISIT_ID}.txt"):
            with self.subTest(locator=bad):
                with self.assertRaises(StorageError):
                    validate_locator(bad)

    def test_a_non_uuid_name_is_refused(self):
        """The client's filename must never become the address (§8.19)."""
        for bad in ("transcripts/riya_1.txt",
                    "transcripts/passwd.txt",
                    "transcripts/.txt"):
            with self.subTest(locator=bad):
                with self.assertRaises(StorageError):
                    validate_locator(bad)

    def test_a_non_txt_suffix_is_refused(self):
        for bad in (f"transcripts/{VISIT_ID}.json",
                    f"transcripts/{VISIT_ID}.exe",
                    f"transcripts/{VISIT_ID}"):
            with self.subTest(locator=bad):
                with self.assertRaises(StorageError):
                    validate_locator(bad)

    def test_a_nested_prefix_is_refused(self):
        """One flat namespace, so there is no directory structure to reason about."""
        for bad in (f"transcripts/2026/{VISIT_ID}.txt",
                    f"other/{VISIT_ID}.txt",
                    f"{VISIT_ID}.txt"):
            with self.subTest(locator=bad):
                with self.assertRaises(StorageError):
                    validate_locator(bad)

    def test_empty_is_refused(self):
        for bad in ("", "   ", None):
            with self.subTest(locator=bad):
                with self.assertRaises(StorageError):
                    validate_locator(bad)  # type: ignore[arg-type]


class StoreContractTests:
    """Behaviour both backends must share.

    Mixed into a TestCase per backend, so a future third backend gets the same
    coverage by adding four lines rather than copying twenty assertions — and so
    the two backends cannot quietly diverge on what read/write/delete mean.
    """

    store: Any

    def test_write_then_read_round_trips(self):
        self.store.write(LOCATOR, SAMPLE)
        self.assertEqual(self.store.read(LOCATOR).text, SAMPLE)

    def test_read_reports_the_provider(self):
        self.store.write(LOCATOR, SAMPLE)
        self.assertEqual(self.store.read(LOCATOR).provider, self.store.provider)

    def test_reading_something_absent_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.store.read(LOCATOR)

    def test_exists_is_false_before_and_true_after(self):
        self.assertFalse(self.store.exists(LOCATOR))
        self.store.write(LOCATOR, SAMPLE)
        self.assertTrue(self.store.exists(LOCATOR))

    def test_write_overwrites(self):
        self.store.write(LOCATOR, SAMPLE)
        self.store.write(LOCATOR, "DOCTOR: replaced.")
        self.assertEqual(self.store.read(LOCATOR).text, "DOCTOR: replaced.")

    def test_delete_reports_whether_it_removed_anything(self):
        """The migration relies on this to tell 'removed' from 'already gone'."""
        self.assertFalse(self.store.delete(LOCATOR))
        self.store.write(LOCATOR, SAMPLE)
        self.assertTrue(self.store.delete(LOCATOR))
        self.assertFalse(self.store.exists(LOCATOR))

    def test_writes_are_isolated_per_locator(self):
        other = transcript_locator(OTHER_VISIT_ID)
        self.store.write(LOCATOR, SAMPLE)
        self.store.write(other, "DOCTOR: a different visit.")
        self.assertEqual(self.store.read(LOCATOR).text, SAMPLE)
        self.assertEqual(self.store.read(other).text, "DOCTOR: a different visit.")

    def test_a_bad_locator_is_refused_on_every_operation(self):
        for op in ("write", "read", "delete"):
            with self.subTest(operation=op):
                with self.assertRaises(StorageError):
                    if op == "write":
                        self.store.write("../escape.txt", SAMPLE)
                    else:
                        getattr(self.store, op)("../escape.txt")

    def test_exists_says_no_for_a_bad_locator_rather_than_raising(self):
        """An existence check should answer the question, not explode."""
        self.assertFalse(self.store.exists("../escape.txt"))

    def test_unicode_survives_the_round_trip(self):
        text = "DOCTOR: सिरदर्द कैसा है?\nPATIENT: ठीक है — बेहतर."
        self.store.write(LOCATOR, text)
        self.assertEqual(self.store.read(LOCATOR).text, text)

    def test_a_byte_order_mark_is_stripped_on_read(self):
        """§8.19: a BOM is valid UTF-8, so it would otherwise reach the model as
        transcript content. Both backends use the same decoder."""
        self._put_raw("\ufeff".encode("utf-8") + SAMPLE.encode("utf-8"))
        self.assertEqual(self.store.read(LOCATOR).text, SAMPLE)


class TestLocalTranscriptStore(StoreContractTests, unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = LocalTranscriptStore(root=self.root)

    def _put_raw(self, raw: bytes) -> None:
        (self.root / f"{VISIT_ID}.txt").write_bytes(raw)

    def test_provider_name(self):
        self.assertEqual(self.store.provider, LOCAL)

    def test_the_file_lands_under_the_root_named_by_visit_id(self):
        self.store.write(LOCATOR, SAMPLE)
        self.assertTrue((self.root / f"{VISIT_ID}.txt").is_file())

    def test_the_directory_name_is_not_doubled(self):
        """The locator already starts with 'transcripts/' and the root already *is*
        the transcripts directory, so a naive join would produce
        transcripts/transcripts/<id>.txt."""
        self.store.write(LOCATOR, SAMPLE)
        self.assertFalse((self.root / "transcripts").exists())

    def test_crlf_is_not_written(self):
        self.store.write(LOCATOR, "DOCTOR: one\nPATIENT: two")
        raw = (self.root / f"{VISIT_ID}.txt").read_bytes()
        self.assertNotIn(b"\r\n", raw)


class TestObjectTranscriptStore(StoreContractTests, unittest.TestCase):

    def setUp(self) -> None:
        self.client = FakeS3(bucket="test-bucket")
        self.store = ObjectTranscriptStore(bucket="test-bucket",
                                           client=self.client)

    def _put_raw(self, raw: bytes) -> None:
        self.client.objects[LOCATOR] = raw

    def test_provider_name(self):
        self.assertEqual(self.store.provider, NEON_OBJECT_STORAGE)

    def test_the_object_key_is_the_locator_verbatim(self):
        """Identical to the local path, which is what lets the migration flip one
        column and rewrite no paths."""
        self.store.write(LOCATOR, SAMPLE)
        self.assertIn(LOCATOR, self.client.objects)

    def test_the_key_carries_no_doctor_id(self):
        """Tenancy is derived through the database (§6), never encoded in the key.

        Putting doctor_id in the key would be a second place ownership is recorded,
        and it would go stale the moment a patient were reassigned. Access is
        already impossible without passing a doctor-scoped visit lookup first.
        """
        self.store.write(LOCATOR, SAMPLE)
        key = next(iter(self.client.objects))
        self.assertEqual(key, LOCATOR)
        self.assertNotIn("doctor", key)

    def test_text_is_stored_as_utf8_bytes(self):
        self.store.write(LOCATOR, SAMPLE)
        self.assertEqual(self.client.objects[LOCATOR], SAMPLE.encode("utf-8"))

    def test_content_type_is_set(self):
        with mock.patch.object(self.client, "put_object",
                               wraps=self.client.put_object) as put:
            self.store.write(LOCATOR, SAMPLE)
        self.assertIn("charset=utf-8", put.call_args.kwargs["ContentType"])

    def test_delete_checks_existence_first(self):
        """S3 DeleteObject reports success for a key that never existed, so the
        backend has to HEAD first to answer 'did I remove anything'."""
        self.assertFalse(self.store.delete(LOCATOR))
        self.assertNotIn(("delete", LOCATOR), self.client.calls)

    def test_a_permission_error_is_a_storage_error_not_not_found(self):
        """403 must not be reported as a missing transcript. Reading 'no such
        transcript' when the real problem is a revoked credential would send
        someone hunting for lost data instead of checking their key."""
        self.client.fail_get = "AccessDenied"
        with self.assertRaises(StorageError) as ctx:
            self.store.read(LOCATOR)
        self.assertEqual(ctx.exception.details.get("s3_code"), "AccessDenied")

    def test_a_write_failure_is_a_storage_error(self):
        self.client.fail_put = "AccessDenied"
        with self.assertRaises(StorageError):
            self.store.write(LOCATOR, SAMPLE)

    def test_the_wrong_bucket_is_a_storage_error(self):
        store = ObjectTranscriptStore(bucket="does-not-exist", client=self.client)
        with self.assertRaises(StorageError):
            store.write(LOCATOR, SAMPLE)

    def test_storage_error_maps_to_a_client_error_status(self):
        """StorageError subclasses TranscriptError, so the existing HTTP mapping
        and the CLI's `except AppError` both already handle it."""
        from app.api.errors import status_for
        from app.core.errors import AppError, TranscriptError

        err = StorageError("nope")
        self.assertIsInstance(err, TranscriptError)
        self.assertIsInstance(err, AppError)
        self.assertEqual(status_for(err), 422)


class TestMigrationDoesNotClaimAnUpload(unittest.TestCase):
    """Moving a transcript between backends must not touch transcript_uploaded_at.

    This is a regression test for a real bug. `cli/migrate_transcripts.py`
    originally reused `visits_repo.set_transcript()` to flip the provider, and that
    function stamps `transcript_uploaded_at = now()`. The UI compares that column
    against `visit_insights.generated_at` and, when it is newer, warns "This brief
    may be out of date. The transcript was replaced after this brief was compiled."

    So migrating every transcript to object storage put a clinical-safety warning on
    every brief in the application, and advised regenerating them: 3 model calls
    per visit against a 20/day quota, to rebuild briefs from byte-identical input.

    The failure was invisible from the backend — no error, no failing test, correct
    data everywhere except one timestamp — and only showed up as scary red text on a
    patient's record. Hence a test that reads the SQL.
    """

    @staticmethod
    def source(name: str) -> str:
        """A function's source with its docstring removed.

        Necessary, not fussy: these docstrings *discuss* `transcript_uploaded_at`
        at length, so matching against raw source would find the prose and pass —
        or fail — for the wrong reason. Only the executable body is evidence.
        """
        import ast
        import inspect
        import textwrap

        from app.db.repositories import visits as repo

        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(repo, name))))
        fn = tree.body[0]
        if (fn.body and isinstance(fn.body[0], ast.Expr)
                and isinstance(fn.body[0].value, ast.Constant)
                and isinstance(fn.body[0].value.value, str)):
            fn.body = fn.body[1:]
        return ast.unparse(tree)

    def test_set_transcript_provider_exists(self):
        from app.db.repositories import visits as repo

        self.assertTrue(hasattr(repo, "set_transcript_provider"))

    def test_it_does_not_write_transcript_uploaded_at(self):
        sql = self.source("set_transcript_provider")
        self.assertNotIn("transcript_uploaded_at", sql)

    def test_it_does_not_write_the_path_or_filename_either(self):
        """Only the provider changes; the address is identical on both backends."""
        sql = self.source("set_transcript_provider")
        self.assertNotIn("transcript_path", sql)
        self.assertNotIn("transcript_filename", sql)
        self.assertIn("transcript_provider", sql)

    def test_it_is_doctor_scoped_like_every_other_write(self):
        self.assertIn("_write_conditions(scope", self.source("set_transcript_provider"))

    def test_set_transcript_still_stamps_a_real_upload(self):
        """The other direction must keep working: a genuine upload IS a change."""
        self.assertIn("transcript_uploaded_at = now()", self.source("set_transcript"))

    def test_the_migration_cli_uses_the_provider_only_writer(self):
        import inspect

        import cli.migrate_transcripts as mig

        sql = inspect.getsource(mig.migrate)
        self.assertIn("set_transcript_provider", sql)
        self.assertNotIn("visits_repo.set_transcript(", sql)


class TestProviderRouting(unittest.TestCase):
    """Reads follow the row; writes follow the setting.

    This is the property that makes a batched migration over live data safe, so it
    is worth asserting directly rather than inferring from the migration passing.
    """

    def setUp(self) -> None:
        factory.reset_stores()
        self.addCleanup(factory.reset_stores)

    def test_known_providers(self):
        self.assertEqual(set(PROVIDERS), {LOCAL, NEON_OBJECT_STORAGE})

    def test_store_for_visit_follows_the_row_not_the_setting(self):
        """A visit on local disk stays readable after the default flips."""
        settings = Settings(transcripts={"storage": NEON_OBJECT_STORAGE,
                                         "local_dir": "/tmp/transcripts"})
        with mock.patch.object(factory, "get_settings", return_value=settings):
            store = factory.store_for_visit(a_visit(transcript_provider=LOCAL))
        self.assertEqual(store.provider, LOCAL)

    def test_a_null_provider_reads_as_local(self):
        """transcript_provider is NULL when nothing is attached, and older rows
        predate the column being written, so NULL must not be an error."""
        settings = Settings(transcripts={"storage": LOCAL,
                                         "local_dir": "/tmp/transcripts"})
        with mock.patch.object(factory, "get_settings", return_value=settings):
            store = factory.store_for_visit(a_visit(transcript_provider=None))
        self.assertEqual(store.provider, LOCAL)

    def test_an_unknown_provider_raises_rather_than_falling_back(self):
        """A row naming a backend this build does not have is a real problem.
        Silently reading local disk could serve a stale file as a clinical record.
        """
        with self.assertRaises(StorageError) as ctx:
            factory.get_store("s3_us_west_experimental")
        self.assertIn("Unknown transcript provider", ctx.exception.message)

    def test_object_storage_without_credentials_names_the_missing_variables(self):
        """Better than a NoCredentialsError from three frames inside botocore."""
        settings = Settings(transcripts={"storage": NEON_OBJECT_STORAGE})

        with mock.patch.object(factory, "get_settings", return_value=settings):
            with self.assertRaises(StorageError) as ctx:
                factory.get_store(NEON_OBJECT_STORAGE)

        message = ctx.exception.message
        self.assertIn("AWS_ENDPOINT_URL_S3", message)
        self.assertIn("AWS_ACCESS_KEY_ID", message)
        self.assertIn("AWS_SECRET_ACCESS_KEY", message)
        # And it says how to carry on without it.
        self.assertIn("transcripts.storage to local", message)


class TestStorageSettings(unittest.TestCase):
    """Storage settings, from the real config.yaml with a controlled environment."""

    @staticmethod
    def _settings(**env: str):
        return load_settings(env_file=None, environ=env)

    @staticmethod
    def _storage(value: str):
        return Settings(transcripts={"storage": value}).transcripts.storage

    def test_code_default_is_local(self):
        """Settings() with no config file needs no credentials."""
        self.assertEqual(Settings().transcripts.storage, LOCAL)

    def test_config_yaml_selects_object_storage(self):
        """The deployment has no persistent disk, so the committed config must
        not quietly flip back to local."""
        self.assertEqual(self._settings().transcripts.storage, NEON_OBJECT_STORAGE)

    def test_case_and_whitespace_are_tolerated(self):
        self.assertEqual(self._storage("  Neon_Object_Storage  "), NEON_OBJECT_STORAGE)

    def test_a_typo_fails_at_startup_not_at_the_first_upload(self):
        with self.assertRaises(Exception) as ctx:
            self._storage("neon-object-storage")
        self.assertIn("storage", str(ctx.exception))

    def test_configured_flag_requires_all_four_values(self):
        self.assertFalse(self._settings().transcripts.object_storage.configured)
        partial = self._settings(AWS_ENDPOINT_URL_S3="https://x.neon.tech",
                                 AWS_ACCESS_KEY_ID="nak_live_x")
        self.assertFalse(partial.transcripts.object_storage.configured)
        full = self._settings(AWS_ENDPOINT_URL_S3="https://x.neon.tech",
                              AWS_ACCESS_KEY_ID="nak_live_x",
                              AWS_SECRET_ACCESS_KEY="nsk_live_x")
        self.assertTrue(full.transcripts.object_storage.configured)

    def test_aws_standard_names_are_used(self):
        """So `neon env pull` populates them directly."""
        s = self._settings(AWS_ENDPOINT_URL_S3="https://x.neon.tech",
                           AWS_REGION="eu-central-1")
        self.assertEqual(s.transcripts.object_storage.endpoint_url, "https://x.neon.tech")
        self.assertEqual(s.transcripts.object_storage.region, "eu-central-1")

    def test_region_defaults_when_unset(self):
        self.assertEqual(self._settings().transcripts.object_storage.region, "us-east-2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
