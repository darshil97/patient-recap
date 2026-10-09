"""Model providers: building them from config, and classifying their errors.

No network: models are only constructed, and the one Bedrock round trip goes
through botocore's Stubber.

Run from backend/:
    python -m unittest discover -s tests -t .
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from botocore.stub import Stubber  # noqa: E402
from langchain_aws import ChatBedrockConverse  # noqa: E402
from langchain_google_genai import ChatGoogleGenerativeAI  # noqa: E402

from app.config import Settings  # noqa: E402
from app.core.errors import ModelNotConfiguredError  # noqa: E402
from app.llm import factory  # noqa: E402
from app.llm.base import model_name  # noqa: E402
from app.llm.errors import classify_error, is_quota_failure  # noqa: E402
from app.llm.providers import bedrock, gemini  # noqa: E402
from app.pipeline.parsing import extract_json, response_text  # noqa: E402
from app.pipeline.runner import invoke_model  # noqa: E402

GEMINI_QUOTA = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': "
                "'You exceeded your current quota'}}")
GEMINI_BAD_KEY = ("400 INVALID_ARGUMENT. {'error': {'message': 'API key not valid. "
                  "Please pass a valid API key.', 'reason': 'API_KEY_INVALID'}}")
GEMINI_REQUEST = "404 NOT_FOUND: model 'gemini-does-not-exist' was not found"


def client_error(code, message, status):
    return ClientError({"Error": {"Code": code, "Message": message},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, "Converse")


def llm_config(**llm):
    """An `llm` config section with both keys set, plus overrides."""
    base = {"gemini": {"api_key": "g-key"}, "bedrock": {"api_key": "b-key"}}
    for name in ("gemini", "bedrock"):
        base[name].update(llm.pop(name, {}))
    return Settings(llm={**base, **llm}).llm


class BedrockEnvironment(unittest.TestCase):
    """langchain-aws hands the key to botocore through os.environ; undo that."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestClassifyError(unittest.TestCase):

    def check(self, kind, *texts):
        for text in texts:
            self.assertEqual(classify_error(RuntimeError(text)), kind, text[:40])

    def test_gemini(self):
        self.check("quota", GEMINI_QUOTA, "503 Service Unavailable",
                   "504 deadline exceeded", "model is overloaded")
        self.check("bad_key", GEMINI_BAD_KEY, "PERMISSION_DENIED",
                   "401 Unauthenticated", "API key expired. Please renew the API key.")
        self.check("request", GEMINI_REQUEST, "safety filter triggered")

    def test_bedrock(self):
        self.assertEqual(classify_error(client_error(
            "ThrottlingException", "Too many requests, please wait.", 429)), "quota")
        self.assertEqual(classify_error(client_error(
            "ServiceUnavailableException", "Service unavailable", 503)), "quota")
        self.assertEqual(classify_error(client_error(
            "AccessDeniedException",
            "User: arn:aws:iam::1:user/x is not authorized to perform: "
            "bedrock:CallWithBearerToken with an explicit deny in a service control "
            "policy", 403)), "bad_key")
        self.assertEqual(classify_error(client_error(
            "UnrecognizedClientException", "The security token included in the "
            "request is invalid.", 403)), "bad_key")
        self.assertEqual(classify_error(client_error(
            "ValidationException", "The provided model identifier is invalid.", 400)),
            "request")

    def test_bad_key_wins_over_status_code(self):
        """A 400 naming an invalid key is a key problem, not a request problem."""
        self.assertEqual(classify_error(RuntimeError(GEMINI_BAD_KEY)), "bad_key")

    def test_quota_failure_flag(self):
        self.assertTrue(is_quota_failure(RuntimeError(GEMINI_QUOTA)))
        self.assertFalse(is_quota_failure(RuntimeError(GEMINI_REQUEST)))


class TestGeminiProvider(unittest.TestCase):

    def test_builds_from_config(self):
        config = llm_config(temperature=0.2, max_retries=3, timeout_seconds=45)
        llm = gemini.build(config)
        self.assertIsInstance(llm, ChatGoogleGenerativeAI)
        self.assertEqual(llm.model, config.gemini.model)
        self.assertEqual(llm.temperature, 0.2)
        self.assertEqual(llm.max_retries, 3)
        self.assertEqual(llm.timeout, 45)
        self.assertEqual(llm.response_mime_type, "application/json")

    def test_json_mode_off(self):
        llm = gemini.build(llm_config(gemini={"json_mode": False}))
        self.assertIsNone(llm.response_mime_type)

    def test_model_override(self):
        self.assertEqual(gemini.build(llm_config(), model="gemini-other").model,
                         "gemini-other")

    def test_missing_key_names_the_variable(self):
        with self.assertRaises(ModelNotConfiguredError) as ctx:
            gemini.build(Settings().llm)
        self.assertIn("GOOGLE_API_KEY", ctx.exception.message)


class TestBedrockProvider(BedrockEnvironment):

    def test_builds_from_config(self):
        config = llm_config(temperature=0.1, max_retries=4, timeout_seconds=90,
                            bedrock={"region": "us-east-1", "max_tokens": 4000})
        llm = bedrock.build(config)
        self.assertIsInstance(llm, ChatBedrockConverse)
        self.assertEqual(llm.model_id, "openai.gpt-oss-120b-1:0")
        self.assertEqual(llm.region_name, "us-east-1")
        self.assertEqual(llm.temperature, 0.1)
        self.assertEqual(llm.max_tokens, 4000)
        self.assertEqual(llm.bedrock_api_key.get_secret_value(), "b-key")
        # botocore normalises max_attempts (retries) to total_max_attempts
        # (retries + the first call), so 4 retries is 5 attempts in all.
        self.assertEqual(llm.config.retries, {"total_max_attempts": 5, "mode": "standard"})
        self.assertEqual(llm.config.read_timeout, 90)

    def test_max_tokens_unset_means_api_default(self):
        self.assertIsNone(bedrock.build(llm_config()).max_tokens)

    def test_missing_key_names_the_variable(self):
        """Never fall back to boto3's credential chain: on the server it would
        find the Neon storage keys and sign Bedrock calls with them."""
        with self.assertRaises(ModelNotConfiguredError) as ctx:
            bedrock.build(Settings().llm)
        self.assertIn("AWS_BEARER_TOKEN_BEDROCK", ctx.exception.message)

    def test_a_converse_round_trip_yields_only_the_answer(self):
        """gpt-oss reasoning arrives as its own block and must not reach the
        JSON parser; usage and the model id must survive for the stored row."""
        llm = bedrock.build(llm_config())
        client = boto3.client("bedrock-runtime", region_name="us-west-2",
                              aws_access_key_id="x", aws_secret_access_key="x",
                              config=Config(retries={"max_attempts": 0}))
        with Stubber(client) as stub:
            stub.add_response("converse", {
                "output": {"message": {"role": "assistant", "content": [
                    {"reasoningContent": {"reasoningText": {
                        "text": "The user wants {json}. Think {a: b}."}}},
                    {"text": '{"chief_issue": "Acid reflux"}'},
                ]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
                "metrics": {"latencyMs": 300},
            })
            llm.client = client
            message = invoke_model(llm, "Return JSON")

        self.assertEqual(extract_json(response_text(message)),
                         {"chief_issue": "Acid reflux"})
        self.assertEqual(message.usage_metadata["total_tokens"], 120)
        self.assertEqual(model_name(llm), "openai.gpt-oss-120b-1:0")

    def test_throttling_surfaces_as_a_quota_failure(self):
        llm = bedrock.build(llm_config())
        client = boto3.client("bedrock-runtime", region_name="us-west-2",
                              aws_access_key_id="x", aws_secret_access_key="x",
                              config=Config(retries={"max_attempts": 0}))
        with Stubber(client) as stub:
            stub.add_client_error("converse", service_error_code="ThrottlingException",
                                  service_message="Too many requests",
                                  http_status_code=429)
            llm.client = client
            with self.assertRaises(Exception) as ctx:
                invoke_model(llm, "Return JSON")
        self.assertTrue(is_quota_failure(ctx.exception))


class TestFactory(BedrockEnvironment):

    def patch_settings(self, **llm):
        settings = Settings(llm={"gemini": {"api_key": "g"},
                                 "bedrock": {"api_key": "b"}, **llm})
        patcher = mock.patch.object(factory, "get_settings", return_value=settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_builds_the_configured_provider(self):
        self.patch_settings(provider="gemini")
        self.assertIsInstance(factory.build_llm(), ChatGoogleGenerativeAI)
        self.patch_settings(provider="bedrock")
        self.assertIsInstance(factory.build_llm(), ChatBedrockConverse)

    def test_model_override_stays_within_the_provider(self):
        self.patch_settings(provider="bedrock")
        llm = factory.build_llm(model="openai.gpt-oss-20b-1:0")
        self.assertEqual(model_name(llm), "openai.gpt-oss-20b-1:0")

    def test_provider_override(self):
        self.patch_settings(provider="gemini")
        self.assertIsInstance(factory.build_llm(provider="bedrock"), ChatBedrockConverse)

    def test_unknown_provider(self):
        self.patch_settings()
        with self.assertRaises(ValueError):
            factory.build_llm(provider="openai")

    def test_check_without_a_key_makes_no_call(self):
        patcher = mock.patch.object(factory, "get_settings", return_value=Settings())
        patcher.start()
        self.addCleanup(patcher.stop)
        result = factory.check_llm()
        self.assertFalse(result["configured"])
        self.assertFalse(result["usable"])
        self.assertEqual(result["kind"], "not_configured")
        self.assertEqual(result["key_variable"], "GOOGLE_API_KEY")


class TestModelName(unittest.TestCase):

    def test_reads_whichever_attribute_the_provider_has(self):
        class Gemini:
            model = "models/gemini-3.7-flash"

        class Bedrock:
            model_id = "openai.gpt-oss-120b-1:0"

        self.assertEqual(model_name(Gemini()), "gemini-3.7-flash")
        self.assertEqual(model_name(Bedrock()), "openai.gpt-oss-120b-1:0")
        self.assertEqual(model_name(object()), "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
