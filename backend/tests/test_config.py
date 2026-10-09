"""
Settings: config.yaml loading, ${...} substitution, and CORS_ORIGINS parsing.

Every test controls its inputs: load_settings(env_file=None, environ={...}) reads
the real config.yaml against an explicit environment, so results cannot depend on
the developer's shell or a real .env.

Run from backend/:
    python -m unittest discover -s tests -t .
"""

import logging
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from app.config import CONFIG_FILE, Settings, load_settings  # noqa: E402


def from_env(**environ):
    """Settings from the real config.yaml, with only `environ` as the environment."""
    return load_settings(env_file=None, environ=environ)


def resolve(value=None):
    """api.cors_origins as parsed when CORS_ORIGINS holds `value`."""
    return from_env(**({} if value is None else {"CORS_ORIGINS": value})).api.cors_origins


class TempFiles(unittest.TestCase):
    """Writes throwaway config.yaml / .env files for loader tests."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def write(self, name, text):
        path = self.dir / name
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path


class TestCorsOrigins(unittest.TestCase):

    def test_unset_defaults_to_star(self):
        """Local development must need no configuration at all."""
        self.assertEqual(resolve(), ["*"])

    def test_bare_star(self):
        self.assertEqual(resolve("*"), ["*"])

    def test_single_origin(self):
        """The deploy case: a bare URL, not JSON."""
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
        with self.assertRaises(ValueError) as ctx:
            resolve('["unclosed')
        self.assertIn("CORS_ORIGINS", str(ctx.exception))

    def test_a_yaml_list_works_too(self):
        settings = Settings(api={"cors_origins": ["https://a.app/", " https://b.app"]})
        self.assertEqual(settings.api.cors_origins, ["https://a.app", "https://b.app"])


class TestCorsWiring(unittest.TestCase):

    def test_middleware_receives_the_parsed_list(self):
        """The value reaches CORSMiddleware, not just Settings."""
        import inspect

        from app import main

        source = inspect.getsource(main.create_app)
        self.assertIn("allow_origins=settings.api.cors_origins", source)

    def test_x_total_count_is_exposed(self):
        """Only a short safelist of response headers is readable cross-origin,
        so paginated lists would arrive with no total."""
        import inspect

        from app import main

        self.assertIn("X-Total-Count", inspect.getsource(main.create_app))


class TestConfigYaml(unittest.TestCase):
    """The committed config.yaml itself."""

    def test_it_loads_with_an_empty_environment(self):
        settings = from_env()
        self.assertIn(settings.llm.provider, ("gemini", "bedrock"))
        self.assertIsNone(settings.api.key, "API_KEY unset must mean an open API")
        self.assertIsNone(settings.llm.gemini.api_key)
        self.assertIsNone(settings.database.url)

    def test_secrets_reach_their_fields(self):
        settings = from_env(GOOGLE_API_KEY="g-key", AWS_BEARER_TOKEN_BEDROCK="b-key",
                            CONNSTR="postgresql://x", API_KEY="shared")
        self.assertEqual(settings.llm.gemini.api_key, "g-key")
        self.assertEqual(settings.llm.bedrock.api_key, "b-key")
        self.assertEqual(settings.database.url, "postgresql://x")
        self.assertEqual(settings.api.key, "shared")

    def test_no_literal_secret_is_committed(self):
        """config.yaml is in git, so every secret-shaped value must be a ${...}
        reference rather than the value itself."""
        raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
        secret_names = {"api_key", "key", "url", "access_key_id", "secret_access_key",
                        "endpoint_url"}

        def walk(node, path=""):
            for name, value in node.items():
                here = f"{path}.{name}" if path else name
                if isinstance(value, dict):
                    walk(value, here)
                elif name in secret_names:
                    self.assertTrue(
                        isinstance(value, str) and value.startswith("${"),
                        f"{here} in config.yaml must reference an environment "
                        f"variable, not hold a value")

        walk(raw)

    def test_paths_resolve_under_backend(self):
        settings = from_env()
        self.assertTrue(settings.llm.prompts_dir.is_absolute())
        self.assertTrue(settings.llm.prompts_dir.is_dir())
        self.assertEqual(settings.transcripts.local_dir.name, "transcripts")


class TestLoader(TempFiles):

    def test_reference_with_fallback(self):
        config = self.write("config.yaml", """
            llm:
              gemini:
                model: ${MODEL:-gemini-fallback}
        """)
        self.assertEqual(load_settings(config, None, {}).llm.gemini.model,
                         "gemini-fallback")
        self.assertEqual(load_settings(config, None, {"MODEL": "m2"}).llm.gemini.model,
                         "m2")

    def test_blank_reference_means_the_field_default(self):
        config = self.write("config.yaml", """
            transcripts:
              max_upload_bytes: ${LIMIT}
        """)
        self.assertEqual(load_settings(config, None, {}).transcripts.max_upload_bytes,
                         256 * 1024)
        self.assertEqual(load_settings(config, None, {"LIMIT": "1024"})
                         .transcripts.max_upload_bytes, 1024)

    def test_reference_inside_text(self):
        config = self.write("config.yaml", """
            api:
              prefix: /api/${VERSION}
        """)
        self.assertEqual(load_settings(config, None, {"VERSION": "v2"}).api.prefix,
                         "/api/v2")

    def test_dotenv_wins_over_the_process_environment(self):
        """A stale key exported in a shell once shadowed the real one."""
        config = self.write("config.yaml", "api:\n  key: ${API_KEY}\n")
        env_file = self.write(".env", "API_KEY=from-dotenv\n")
        settings = load_settings(config, env_file, {"API_KEY": "stale-shell-export"})
        self.assertEqual(settings.api.key, "from-dotenv")

    def test_an_empty_dotenv_value_does_not_blank_a_real_one(self):
        config = self.write("config.yaml", "api:\n  key: ${API_KEY}\n")
        env_file = self.write(".env", "API_KEY=\n")
        self.assertEqual(load_settings(config, env_file, {"API_KEY": "real"}).api.key,
                         "real")

    def test_unknown_key_is_rejected(self):
        """A typo must fail at startup, not fall back to a default in silence."""
        config = self.write("config.yaml", """
            llm:
              gemini:
                modle: gemini-typo
        """)
        with self.assertRaises(ValueError) as ctx:
            load_settings(config, None, {})
        self.assertIn("modle", str(ctx.exception))

    def test_provider_is_normalised_and_validated(self):
        self.assertEqual(Settings(llm={"provider": " Bedrock "}).llm.provider, "bedrock")
        with self.assertRaises(Exception):
            Settings(llm={"provider": "openai"})

    def test_model_follows_the_provider(self):
        settings = Settings(llm={"provider": "bedrock"})
        self.assertEqual(settings.llm.model, settings.llm.bedrock.model)
        self.assertEqual(Settings().llm.model, Settings().llm.gemini.model)

    def test_missing_config_file_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            load_settings(self.dir / "absent.yaml", None, {})

    def test_retired_variables_are_reported(self):
        config = self.write("config.yaml", "{}\n")
        with self.assertLogs("app.config", level=logging.WARNING) as logs:
            load_settings(config, None, {"GOOGLE_API_KEY_1": "x",
                                         "TRANSCRIPT_STORAGE": "local"})
        output = "\n".join(logs.output)
        self.assertIn("GOOGLE_API_KEY_1", output)
        self.assertIn("transcripts.storage", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
