"""
Settings parsing, particularly CORS_ORIGINS.

Regression guard: pydantic-settings treats List[str] as a complex field and runs
json.loads() on the raw environment value inside the settings source, *before*
field validators run. Without `NoDecode` on the annotation,
`CORS_ORIGINS=https://app.example.com` raises SettingsError and the process will
not start -- a deploy-time failure that never shows up locally, because locally
the variable is unset and the default applies.

Run from backend/:
    python -m unittest discover -s tests -t .
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402


def resolve(value=None, **extra):
    """cors_origins as parsed from an environment holding `value`.

    The surrounding environment is cleared and _env_file disabled so the result
    cannot depend on the developer's machine or a real .env.
    """
    env = dict(extra)
    if value is not None:
        env["CORS_ORIGINS"] = value
    with mock.patch.dict(os.environ, env, clear=True):
        return Settings(_env_file=None).cors_origins


class TestCorsOrigins(unittest.TestCase):

    def test_unset_defaults_to_star(self):
        """Local development must need no configuration at all."""
        self.assertEqual(resolve(), ["*"])

    def test_bare_star(self):
        self.assertEqual(resolve("*"), ["*"])

    def test_single_origin_does_not_crash(self):
        """The deploy case. A bare URL is not valid JSON, so this is exactly
        what NoDecode exists to make work."""
        self.assertEqual(resolve("https://myapp.vercel.app"),
                         ["https://myapp.vercel.app"])

    def test_comma_separated(self):
        self.assertEqual(
            resolve("https://a.vercel.app,http://localhost:5173"),
            ["https://a.vercel.app", "http://localhost:5173"])

    def test_whitespace_around_commas_is_tolerated(self):
        self.assertEqual(
            resolve("https://a.vercel.app , http://localhost:5173 "),
            ["https://a.vercel.app", "http://localhost:5173"])

    def test_trailing_slash_is_stripped(self):
        """An Origin header is scheme+host+port with no path, so a trailing
        slash would never match and the mismatch is invisible in a log."""
        self.assertEqual(resolve("https://myapp.vercel.app/"),
                         ["https://myapp.vercel.app"])

    def test_empty_value_falls_back_to_the_default(self):
        """An empty CORS_ORIGINS= must not silently block every origin."""
        self.assertEqual(resolve(""), ["*"])

    def test_whitespace_only_falls_back(self):
        self.assertEqual(resolve("   "), ["*"])

    def test_empty_elements_are_dropped(self):
        self.assertEqual(resolve("https://a.app,,https://b.app"),
                         ["https://a.app", "https://b.app"])

    def test_json_list_still_works(self):
        self.assertEqual(resolve('["https://a.app","https://b.app"]'),
                         ["https://a.app", "https://b.app"])

    def test_malformed_json_is_a_clear_error(self):
        with self.assertRaises(Exception) as ctx:
            resolve('["unclosed')
        self.assertIn("CORS_ORIGINS", str(ctx.exception))

    def test_no_decode_is_still_on_the_annotation(self):
        """Removing it reintroduces the startup crash, so assert it directly."""
        from pydantic_settings import NoDecode

        metadata = Settings.model_fields["cors_origins"].metadata
        self.assertTrue(
            any(m is NoDecode or isinstance(m, NoDecode) for m in metadata),
            "cors_origins must keep NoDecode or a non-JSON CORS_ORIGINS "
            "will crash the app at startup")


class TestCorsWiring(unittest.TestCase):

    def test_middleware_receives_the_parsed_list(self):
        """The value reaches CORSMiddleware, not just Settings."""
        import inspect

        from app import main

        source = inspect.getsource(main.create_app)
        self.assertIn("allow_origins=settings.cors_origins", source)

    def test_x_total_count_is_exposed(self):
        """Only a short safelist of response headers is readable cross-origin,
        so paginated lists would otherwise arrive with no total."""
        import inspect

        from app import main

        self.assertIn("X-Total-Count", inspect.getsource(main.create_app))


class TestOtherSettings(unittest.TestCase):

    def test_api_key_unset_means_open(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(Settings(_env_file=None).api_key)

    def test_max_transcript_bytes_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Settings(_env_file=None).max_transcript_bytes,
                             256 * 1024)

    def test_max_transcript_bytes_from_env(self):
        with mock.patch.dict(os.environ, {"MAX_TRANSCRIPT_BYTES": "1024"},
                             clear=True):
            self.assertEqual(Settings(_env_file=None).max_transcript_bytes, 1024)

    def test_no_other_list_field_lacks_no_decode(self):
        """Any future List[str] setting needs NoDecode for the same reason."""
        from pydantic_settings import NoDecode

        for name, field in Settings.model_fields.items():
            annotation = str(field.annotation)
            if "List[str]" not in annotation and "list[str]" not in annotation:
                continue
            has = any(m is NoDecode or isinstance(m, NoDecode)
                      for m in field.metadata)
            self.assertTrue(
                has,
                f"{name} is a list setting without NoDecode: a plain "
                f"comma-separated env value would crash at startup")


if __name__ == "__main__":
    unittest.main(verbosity=2)
