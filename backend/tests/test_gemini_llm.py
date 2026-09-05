"""Tests for multi-key rotation. Stubs the client, so no API calls are made.

Run from the project root:
    python -m unittest discover -s tests -t .
"""

import os
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.llm.errors import AllKeysExhausted, classify_error  # noqa: E402
from app.llm.gemini import MultiKeyGemini, build_llm  # noqa: E402
from app.pipeline.runner import invoke_model  # noqa: E402

QUOTA = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': "
         "'You exceeded your current quota'}}")
BAD_KEY = ("400 INVALID_ARGUMENT. {'error': {'message': 'API key not valid. "
           "Please pass a valid API key.', 'reason': 'API_KEY_INVALID'}}")
REQUEST = "404 NOT_FOUND: model 'gemini-does-not-exist' was not found"


class StubClient:
    """Stands in for ChatGoogleGenerativeAI."""

    def __init__(self, index, error=None):
        self.index = index
        self.error = error
        self.calls = 0

    def invoke(self, input, **kwargs):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return f"reply from key {self.index}"


def wrapper_with(errors):
    """Build a wrapper over len(errors) keys, each backed by a stub."""
    llm = MultiKeyGemini(
        model="test-model",
        api_keys=[f"key-{i}" for i in range(1, len(errors) + 1)],
    )
    stubs = {i: StubClient(i, err) for i, err in enumerate(errors, 1)}
    llm._client_for = lambda state: stubs[state.index]  # noqa: SLF001
    return llm, stubs


class TestClassifyError(unittest.TestCase):

    def test_quota_variants(self):
        for text in (QUOTA, "503 Service Unavailable", "rate limit exceeded",
                     "504 deadline exceeded", "model is overloaded"):
            self.assertEqual(classify_error(RuntimeError(text)), "quota", text[:30])

    def test_bad_key_variants(self):
        for text in (BAD_KEY, "PERMISSION_DENIED", "401 Unauthenticated",
                     "API key expired. Please renew the API key."):
            self.assertEqual(classify_error(RuntimeError(text)), "bad_key", text[:30])

    def test_request_errors(self):
        for text in (REQUEST, "safety filter triggered", "invalid json schema"):
            self.assertEqual(classify_error(RuntimeError(text)), "request", text[:30])

    def test_bad_key_wins_over_status_code(self):
        """A 400 naming an invalid key is a key problem, not a request problem."""
        self.assertEqual(classify_error(RuntimeError(BAD_KEY)), "bad_key")


class TestApiKeyDiscovery(unittest.TestCase):

    """Key discovery now lives on Settings, not a module-level function."""

    @staticmethod
    def keys(**env):
        """Resolve api_keys from only the given values.

        _env_file=None keeps the project's .env out, and the surrounding
        os.environ is scrubbed of key variables too: a real GOOGLE_API_KEY
        exported in the developer's shell would otherwise leak in and the
        assertions would depend on the machine.
        """
        pollution = [k for k in os.environ
                     if k.startswith(("GOOGLE_API_KEY", "GEMINI_API_KEY"))]
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in pollution:
                os.environ.pop(name, None)
            return Settings(_env_file=None, **env).api_keys

    def test_reads_numbered_keys_in_order(self):
        self.assertEqual(
            self.keys(GOOGLE_API_KEY_1="a", GOOGLE_API_KEY_2="b", GOOGLE_API_KEY_3="c"),
            ["a", "b", "c"])

    def test_skips_gaps(self):
        self.assertEqual(self.keys(GOOGLE_API_KEY_1="a", GOOGLE_API_KEY_3="c"),
                         ["a", "c"])

    def test_deduplicates(self):
        self.assertEqual(self.keys(GOOGLE_API_KEY_1="a", GOOGLE_API_KEY_2="a"), ["a"])

    def test_falls_back_to_single_key(self):
        self.assertEqual(self.keys(GOOGLE_API_KEY="solo"), ["solo"])

    def test_numbered_keys_take_precedence(self):
        """A numbered key and the bare key are often the same credential; using
        both would double-count it against one quota."""
        self.assertEqual(self.keys(GOOGLE_API_KEY_1="numbered", GOOGLE_API_KEY="single"),
                         ["numbered"])

    def test_ignores_blank(self):
        self.assertEqual(self.keys(GOOGLE_API_KEY_1="   ", GOOGLE_API_KEY_2="b"), ["b"])

    def test_no_keys_at_all(self):
        self.assertEqual(self.keys(), [])

    def test_no_keys_raises(self):
        with self.assertRaises(ValueError):
            MultiKeyGemini(model="m", api_keys=[])


class TestRotation(unittest.TestCase):

    def test_first_key_used_when_healthy(self):
        llm, stubs = wrapper_with([None, None, None])
        self.assertEqual(llm.invoke("hi"), "reply from key 1")
        self.assertEqual([s.calls for s in stubs.values()], [1, 0, 0])

    def test_falls_through_quota_to_next_key(self):
        llm, stubs = wrapper_with([QUOTA, None, None])
        self.assertEqual(llm.invoke("hi"), "reply from key 2")
        self.assertEqual(stubs[1].calls, 1)
        self.assertEqual(stubs[2].calls, 1)
        self.assertEqual(stubs[3].calls, 0)

    def test_falls_through_two_exhausted_keys(self):
        llm, stubs = wrapper_with([QUOTA, QUOTA, None])
        self.assertEqual(llm.invoke("hi"), "reply from key 3")
        self.assertEqual([s.calls for s in stubs.values()], [1, 1, 1])

    def test_rotation_is_sticky(self):
        """After moving to key 2, later calls start there instead of retrying key 1."""
        llm, stubs = wrapper_with([QUOTA, None, None])
        llm.invoke("one")
        llm.invoke("two")
        llm.invoke("three")
        self.assertEqual(stubs[1].calls, 1, "key 1 should not be retried every call")
        self.assertEqual(stubs[2].calls, 3)
        self.assertEqual(llm.active_key, 2)

    def test_bad_key_is_disabled_permanently(self):
        llm, stubs = wrapper_with([BAD_KEY, None])
        llm.invoke("one")
        llm.invoke("two")
        self.assertEqual(stubs[1].calls, 1, "a rejected key must not be tried again")
        self.assertEqual(stubs[2].calls, 2)
        self.assertIn("disabled", llm.status()["key 1"])

    def test_bad_key_does_not_abort_the_run(self):
        """The original failure mode: an invalid key killed everything."""
        llm, _ = wrapper_with([BAD_KEY, QUOTA, None])
        self.assertEqual(llm.invoke("hi"), "reply from key 3")

    def test_request_error_raises_without_trying_other_keys(self):
        llm, stubs = wrapper_with([REQUEST, None, None])
        with self.assertRaises(RuntimeError) as ctx:
            llm.invoke("hi")
        self.assertIn("NOT_FOUND", str(ctx.exception))
        self.assertEqual(stubs[2].calls, 0, "a bad request fails on every key alike")

    def test_all_keys_exhausted_raises_with_context(self):
        llm, stubs = wrapper_with([QUOTA, QUOTA, QUOTA])
        with self.assertRaises(AllKeysExhausted) as ctx:
            llm.invoke("hi")
        self.assertEqual([s.calls for s in stubs.values()], [1, 1, 1])
        self.assertIsNotNone(ctx.exception.last_error)
        self.assertIn("429", str(ctx.exception))
        self.assertEqual(len(ctx.exception.status), 3)

    def test_all_keys_disabled_raises_immediately(self):
        llm, stubs = wrapper_with([BAD_KEY, BAD_KEY])
        with self.assertRaises(AllKeysExhausted):
            llm.invoke("one")
        before = [s.calls for s in stubs.values()]
        with self.assertRaises(AllKeysExhausted):
            llm.invoke("two")
        self.assertEqual([s.calls for s in stubs.values()], before,
                         "no further calls once every key is disabled")

    def test_wraps_around_to_earlier_keys(self):
        """Sticky on key 3; when it fails, key 1 is tried again."""
        llm, stubs = wrapper_with([None, None, None])
        llm._cursor = 2  # noqa: SLF001 - pretend we settled on key 3
        stubs[3].error = QUOTA
        self.assertEqual(llm.invoke("hi"), "reply from key 1")
        self.assertEqual(stubs[3].calls, 1)
        self.assertEqual(stubs[1].calls, 1)

    def test_status_and_counts(self):
        llm, _ = wrapper_with([QUOTA, None, None])
        llm.invoke("hi")
        status = llm.status()
        self.assertEqual(llm.key_count, 3)
        self.assertIn("ok", status["key 2"])
        self.assertEqual(status["key 3"], "unused")

    def test_model_attribute_exposed_for_provenance(self):
        """db.py records result.model, which comes from this attribute."""
        llm, _ = wrapper_with([None])
        self.assertEqual(llm.model, "test-model")


class TestPipelineIntegration(unittest.TestCase):

    def test_wrapper_satisfies_the_pipeline_contract(self):
        llm, stubs = wrapper_with([QUOTA, None])
        self.assertEqual(invoke_model(llm, "rendered prompt"), "reply from key 2")
        self.assertEqual(stubs[2].calls, 1)

    def test_build_llm_uses_every_configured_key(self):
        llm = build_llm(model="test-model", api_keys=["k1", "k2", "k3"])
        self.assertEqual(llm.key_count, 3)
        self.assertEqual(llm.response_mime_type, "application/json")

    def test_build_llm_json_mode_off(self):
        llm = build_llm(model="test-model", json_mode=False, api_keys=["k1"])
        self.assertIsNone(llm.response_mime_type)

    def test_build_llm_without_keys_raises(self):
        with self.assertRaises(ValueError):
            build_llm(model="test-model", api_keys=[])


if __name__ == "__main__":
    unittest.main(verbosity=2)
