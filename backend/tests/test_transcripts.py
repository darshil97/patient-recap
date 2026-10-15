"""
Transcript loading, upload validation, and the storage convention.

Offline: no database, no API calls. Files are written to a temporary directory
rather than the real transcripts/ folder.

Run from backend/:
    python -m unittest discover -s tests -t .
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.errors import (  # noqa: E402
    TranscriptError,
    TranscriptTooLargeError,
)
from app.domain.transcripts import (  # noqa: E402
    TRANSCRIPT_SUFFIX,
    Transcript,
    decode_transcript_bytes,
    load_transcript,
    resolve_within,
)
from app.services import transcript_service  # noqa: E402
from app.storage import transcript_locator  # noqa: E402

SAMPLE = "DOCTOR: How have you been?\nPATIENT: Much better, thank you."


class TranscriptDirTestCase(unittest.TestCase):
    """Gives each test an isolated transcripts directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "transcripts"
        self.dir.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, text: str = SAMPLE) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        return path


class TestDecoding(unittest.TestCase):

    def test_plain_utf8(self):
        self.assertEqual(decode_transcript_bytes(SAMPLE.encode(), "x.txt"), SAMPLE)

    def test_bom_is_stripped(self):
        """A BOM is itself valid UTF-8, so decoding as plain utf-8 succeeds and
        leaves a U+FEFF that would be sent to the model as transcript content."""
        raw = b"\xef\xbb\xbf" + SAMPLE.encode()
        decoded = decode_transcript_bytes(raw, "x.txt")
        self.assertEqual(decoded, SAMPLE)
        self.assertFalse(decoded.startswith("\ufeff"))

    def test_utf16_rejected(self):
        with self.assertRaises(TranscriptError) as ctx:
            decode_transcript_bytes(SAMPLE.encode("utf-16"), "x.txt")
        self.assertIn("UTF-8", str(ctx.exception))

    def test_latin1_high_bytes_rejected(self):
        with self.assertRaises(TranscriptError):
            decode_transcript_bytes(b"Doctor: caf\xe9", "x.txt")

    def test_accents_survive(self):
        text = "DOCTOR: café, naïve, 日本語"
        self.assertEqual(decode_transcript_bytes(text.encode(), "x.txt"), text)


class TestLoadTranscript(TranscriptDirTestCase):

    def test_loads_txt(self):
        self.write("abc.txt")
        loaded = load_transcript("transcripts/abc.txt", self.dir)
        self.assertEqual(loaded.text, SAMPLE)
        self.assertEqual(loaded.character_count, len(SAMPLE))

    def test_transcript_has_no_patient_name_or_disease(self):
        """Both were JSON-only fields. disease must not exist anywhere it could
        reach the doctor, and the name comes from the database."""
        self.write("abc.txt")
        loaded = load_transcript("transcripts/abc.txt", self.dir)
        self.assertFalse(hasattr(loaded, "patient_name"))
        self.assertFalse(hasattr(loaded, "disease"))
        self.assertEqual(set(Transcript.__dataclass_fields__),
                         {"path", "relative_path", "text"})

    def test_json_rejected_with_a_fix_in_the_message(self):
        (self.dir / "old.json").write_text('{"transcript": "hi"}', encoding="utf-8")
        with self.assertRaises(TranscriptError) as ctx:
            load_transcript("transcripts/old.json", self.dir)
        message = str(ctx.exception)
        self.assertIn(TRANSCRIPT_SUFFIX, message)
        self.assertIn("convert_transcripts", message)

    def test_other_extensions_rejected(self):
        for name in ("notes.docx", "notes.pdf", "notes"):
            (self.dir / name).write_text("x", encoding="utf-8")
            with self.assertRaises(TranscriptError):
                load_transcript(f"transcripts/{name}", self.dir)

    def test_empty_file_rejected(self):
        self.write("blank.txt", "   \n\t\n ")
        with self.assertRaises(TranscriptError) as ctx:
            load_transcript("transcripts/blank.txt", self.dir)
        self.assertIn("empty", str(ctx.exception))

    def test_missing_file_rejected(self):
        with self.assertRaises(TranscriptError):
            load_transcript("transcripts/nope.txt", self.dir)

    def test_text_is_stripped(self):
        self.write("pad.txt", f"\n\n  {SAMPLE}  \n\n")
        self.assertEqual(load_transcript("transcripts/pad.txt", self.dir).text, SAMPLE)


class TestTraversalGuard(TranscriptDirTestCase):
    """The guard's semantics must not weaken: there is no auth in front of this."""

    def test_escapes_refused(self):
        for bad in ("../../etc/passwd", "/etc/passwd",
                    "transcripts/../../../etc/passwd", "../outside.txt",
                    "a/../../b.txt"):
            with self.assertRaises(TranscriptError, msg=bad):
                resolve_within(bad, self.dir)

    def test_inside_allowed(self):
        resolved = resolve_within("transcripts/ok.txt", self.dir)
        self.assertEqual(resolved.parent, self.dir.resolve())

    def test_bare_name_allowed(self):
        self.assertEqual(resolve_within("ok.txt", self.dir).parent,
                         self.dir.resolve())


class TestUploadValidation(unittest.TestCase):
    """validate_upload rejects rather than coerces: a mis-parsed file would
    produce a confident, wrong clinical record."""

    def test_accepts_txt(self):
        self.assertEqual(
            transcript_service.validate_upload("visit.txt", SAMPLE.encode()), SAMPLE)

    def test_extension_is_case_insensitive(self):
        self.assertEqual(
            transcript_service.validate_upload("VISIT.TXT", SAMPLE.encode()), SAMPLE)

    def test_json_rejected(self):
        with self.assertRaises(TranscriptError):
            transcript_service.validate_upload("visit.json", b'{"a": 1}')

    def test_docx_and_pdf_rejected(self):
        for name in ("notes.docx", "notes.pdf", "notes"):
            with self.assertRaises(TranscriptError, msg=name):
                transcript_service.validate_upload(name, SAMPLE.encode())

    def test_size_ceiling(self):
        with mock.patch.object(transcript_service, "get_settings") as settings:
            settings.return_value.transcripts.max_upload_bytes = 1024
            with self.assertRaises(TranscriptTooLargeError) as ctx:
                transcript_service.validate_upload("big.txt", b"x" * 2048)
            self.assertEqual(ctx.exception.details["size_bytes"], 2048)
            self.assertEqual(ctx.exception.details["max_bytes"], 1024)

    def test_at_the_ceiling_is_allowed(self):
        with mock.patch.object(transcript_service, "get_settings") as settings:
            settings.return_value.transcripts.max_upload_bytes = 16
            self.assertEqual(
                transcript_service.validate_upload("ok.txt", b"a" * 16), "a" * 16)

    def test_utf16_rejected(self):
        with self.assertRaises(TranscriptError):
            transcript_service.validate_upload("visit.txt", SAMPLE.encode("utf-16"))

    def test_whitespace_only_rejected(self):
        with self.assertRaises(TranscriptError) as ctx:
            transcript_service.validate_upload("visit.txt", b"   \n\t  \n")
        self.assertIn("empty", str(ctx.exception).lower())

    def test_bom_upload_is_cleaned(self):
        raw = b"\xef\xbb\xbf" + SAMPLE.encode()
        self.assertEqual(transcript_service.validate_upload("visit.txt", raw), SAMPLE)


class TestFilenameHandling(unittest.TestCase):
    """The client filename is display-only and never builds a path."""

    def test_directory_components_stripped(self):
        for given, expected in [
            ("../../etc/passwd.txt", "passwd.txt"),
            ("/absolute/path/notes.txt", "notes.txt"),
            (r"C:\Users\doc\notes.txt", "notes.txt"),
            ("plain.txt", "plain.txt"),
        ]:
            self.assertEqual(
                transcript_service._display_filename(given), expected, given)

    def test_traversal_filename_still_validates_on_extension(self):
        """A traversal attempt with a .txt name is accepted as content but its
        name is reduced to a base name, and the stored path ignores it entirely."""
        self.assertEqual(
            transcript_service.validate_upload("../../etc/passwd.txt",
                                               SAMPLE.encode()), SAMPLE)

    def test_stored_path_is_derived_from_visit_id_only(self):
        """§8.19, unchanged in intent: the client's filename never reaches the
        address the transcript is stored at.

        This used to call transcript_service._stored_path. That helper is gone —
        building the address moved to app.storage.transcript_locator so that one
        function defines it for every backend, rather than the filesystem and the
        object store each having their own idea. Same assertion, new owner.
        """
        visit_id = "22222222-2222-2222-2222-222222222222"
        stored = transcript_locator(visit_id)
        self.assertEqual(stored, f"transcripts/{visit_id}.txt")
        self.assertNotIn("passwd", stored)

    def test_empty_filename_becomes_none(self):
        self.assertIsNone(transcript_service._display_filename(None))
        self.assertIsNone(transcript_service._display_filename("   "))


if __name__ == "__main__":
    unittest.main(verbosity=2)
