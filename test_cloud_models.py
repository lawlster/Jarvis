"""Step 40 tests — cloud AI model integration.

Stubbed HTTP transports only. No network, no production
writes, no real API keys: the only key value that appears
is a fake test value in an environment variable, and the
tests assert it never reaches a config file or a turn log.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import requests

from agent import cloud_models as cm
from agent import orchestrator as orch
from agent.orchestrator import ModelUnavailable

TEST_KEY_ENV = "JARVIS_TEST_CLOUD_KEY"
TEST_KEY = "sk-test-fake-key-do-not-use-12345"
MESSAGES = [{"role": "user", "content": "Hello"}]


def completion_payload(content="Cloud reply.", usage=None):
    payload = {
        "choices": [{"message": {"role": "assistant",
                                 "content": content}}],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


class FakeResponse:
    """A stub HTTP response for the pluggable transport."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class StubTransport:
    """A stub http_post: returns queued responses or raises."""

    def __init__(self, behaviours):
        self.behaviours = list(behaviours)
        self.calls = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({
            "url": url, "payload": json, "headers": headers,
            "timeout": timeout,
        })
        behaviour = self.behaviours.pop(0)
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour


def cloud_client(transport, **overrides):
    kwargs = {
        "name": "test-cloud",
        "model": "test-model",
        "endpoint": "https://example.invalid/v1/chat/completions",
        "api_key_env": TEST_KEY_ENV,
        "timeout": 5,
        "max_retries": 2,
        "retry_backoff": 0,
        "http_post": transport,
    }
    kwargs.update(overrides)
    return cm.CloudModelClient(**kwargs)


class CloudClientTests(unittest.TestCase):
    """The cloud client itself: credentials, errors, usage."""

    def setUp(self):
        self.env = mock.patch.dict(
            os.environ, {TEST_KEY_ENV: TEST_KEY})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_interface_matches_ollama_client(self):
        # Both clients expose chat(messages) -> str, so the
        # Step 39 orchestrator can swap one for the other.
        for client in (
            orch.OllamaModelClient(),
            cloud_client(StubTransport([
                FakeResponse(200, completion_payload())])),
        ):
            self.assertTrue(callable(getattr(client, "chat", None)))
        client = cloud_client(StubTransport([
            FakeResponse(200, completion_payload("hi"))]))
        self.assertEqual(client.chat(MESSAGES), "hi")

    def test_missing_credentials_structured_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            client = cloud_client(StubTransport([]))
            self.assertFalse(client.has_credentials)
            with self.assertRaises(cm.CloudModelError) as ctx:
                client.chat(MESSAGES)
            self.assertEqual(ctx.exception.reason,
                             "missing_credentials")
            self.assertEqual(client.calls, 0)

    def test_blank_credentials_count_as_missing(self):
        with mock.patch.dict(os.environ, {TEST_KEY_ENV: "   "}):
            client = cloud_client(StubTransport([]))
            self.assertFalse(client.has_credentials)

    def test_malformed_response_structured_error(self):
        client = cloud_client(StubTransport([
            FakeResponse(200, {"nope": True})]))
        with self.assertRaises(cm.CloudModelError) as ctx:
            client.chat(MESSAGES)
        self.assertEqual(ctx.exception.reason, "malformed_response")

    def test_malformed_json_structured_error(self):
        client = cloud_client(StubTransport([
            FakeResponse(200, None, text="{not json")]))
        with self.assertRaises(cm.CloudModelError) as ctx:
            client.chat(MESSAGES)
        self.assertEqual(ctx.exception.reason, "malformed_response")

    def test_empty_reply_structured_error(self):
        client = cloud_client(StubTransport([
            FakeResponse(200, completion_payload(""))]))
        with self.assertRaises(cm.CloudModelError) as ctx:
            client.chat(MESSAGES)
        self.assertEqual(ctx.exception.reason, "empty_reply")

    def test_timeout_retries_then_structured_error(self):
        transport = StubTransport([
            requests.exceptions.Timeout("slow"),
            requests.exceptions.Timeout("slow"),
            requests.exceptions.Timeout("slow"),
        ])
        client = cloud_client(transport)
        with self.assertRaises(cm.CloudModelError) as ctx:
            client.chat(MESSAGES)
        self.assertEqual(ctx.exception.reason, "timeout")
        # max_retries=2 means 1 initial call + 2 retries.
        self.assertEqual(len(transport.calls), 3)

    def test_unreachable_structured_error(self):
        transport = StubTransport([
            requests.exceptions.ConnectionError("refused"),
        ] * 3)
        client = cloud_client(transport, max_retries=0)
        with self.assertRaises(cm.CloudModelError) as ctx:
            client.chat(MESSAGES)
        self.assertEqual(ctx.exception.reason, "unreachable")

    def test_http_401_is_not_retried(self):
        transport = StubTransport([FakeResponse(401, {})])
        client = cloud_client(transport)
        with self.assertRaises(cm.CloudModelError) as ctx:
            client.chat(MESSAGES)
        self.assertEqual(ctx.exception.reason, "http_error")
        self.assertEqual(len(transport.calls), 1)

    def test_transient_500_then_success(self):
        transport = StubTransport([
            FakeResponse(500, {}),
            FakeResponse(200, completion_payload("recovered")),
        ])
        client = cloud_client(transport)
        self.assertEqual(client.chat(MESSAGES), "recovered")
        self.assertEqual(len(transport.calls), 2)

    def test_usage_and_cost_recorded_when_reported(self):
        transport = StubTransport([FakeResponse(200, completion_payload(
            "done", usage={"prompt_tokens": 100,
                           "completion_tokens": 50,
                           "total_tokens": 150}))])
        client = cloud_client(
            transport, input_price_per_1k=0.01,
            output_price_per_1k=0.03)
        self.assertEqual(client.chat(MESSAGES), "done")
        usage = client.last_usage
        self.assertEqual(usage["prompt_tokens"], 100)
        self.assertEqual(usage["completion_tokens"], 50)
        self.assertEqual(usage["total_tokens"], 150)
        # 100/1000*0.01 + 50/1000*0.03 = 0.0025
        self.assertAlmostEqual(usage["estimated_cost"], 0.0025)
        self.assertEqual(usage["currency"], "USD")
        self.assertEqual(usage["model"], "test-model")

    def test_cost_is_none_without_prices(self):
        transport = StubTransport([FakeResponse(200, completion_payload(
            "done", usage={"prompt_tokens": 100,
                           "completion_tokens": 50,
                           "total_tokens": 150}))])
        client = cloud_client(transport)
        client.chat(MESSAGES)
        self.assertIsNone(client.last_usage["estimated_cost"])
        self.assertIsNone(client.last_usage["currency"])

    def test_cost_is_none_when_usage_not_reported(self):
        transport = StubTransport([
            FakeResponse(200, completion_payload("done"))])
        client = cloud_client(
            transport, input_price_per_1k=0.01,
            output_price_per_1k=0.03)
        client.chat(MESSAGES)
        self.assertIsNone(client.last_usage["estimated_cost"])

    def test_repr_never_carries_a_credential(self):
        client = cloud_client(StubTransport([]))
        self.assertNotIn(TEST_KEY, repr(client))

    def test_authorization_header_uses_env_key(self):
        transport = StubTransport([
            FakeResponse(200, completion_payload())])
        client = cloud_client(transport)
        client.chat(MESSAGES)
        headers = transport.calls[0]["headers"]
        self.assertEqual(headers["Authorization"],
                         f"Bearer {TEST_KEY}")


class RouterTests(unittest.TestCase):
    """Local-first routing with a configured cloud fallback."""

    def setUp(self):
        self.env = mock.patch.dict(
            os.environ, {TEST_KEY_ENV: TEST_KEY})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_local_first_when_local_is_up(self):
        local = orch.StubModelClient(reply="local reply")
        transport = StubTransport([
            FakeResponse(200, completion_payload("cloud reply"))])
        cloud = cloud_client(transport)
        router = cm.ModelRouter(local_client=local,
                                cloud_clients=[cloud])
        reply, routing = router.chat_with_routing(MESSAGES)
        self.assertEqual(reply, "local reply")
        self.assertFalse(routing["fallback_used"])
        # The local client here is a stub, so routing names it
        # by its class; a real Ollama client is named by model.
        self.assertEqual(routing["used"], "StubModelClient")
        self.assertEqual(len(transport.calls), 0)

    def test_cloud_fallback_when_local_is_down(self):
        local = orch.StubModelClient(raise_unavailable=True)
        transport = StubTransport([
            FakeResponse(200, completion_payload("cloud reply"))])
        cloud = cloud_client(transport)
        router = cm.ModelRouter(local_client=local,
                                cloud_clients=[cloud])
        reply, routing = router.chat_with_routing(MESSAGES)
        self.assertEqual(reply, "cloud reply")
        self.assertTrue(routing["fallback_used"])
        self.assertEqual(routing["used"], "test-cloud")
        self.assertIn("local model unavailable",
                      routing["fallback_reason"])
        self.assertEqual(router.last_routing, routing)

    def test_no_fallback_without_credentials(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            local = orch.StubModelClient(raise_unavailable=True)
            transport = StubTransport([
                FakeResponse(200, completion_payload("cloud"))])
            cloud = cloud_client(transport)
            router = cm.ModelRouter(local_client=local,
                                    cloud_clients=[cloud])
            with self.assertRaises(ModelUnavailable):
                router.chat(MESSAGES)
            # The cloud transport was never called.
            self.assertEqual(len(transport.calls), 0)
            self.assertIsNone(router.last_routing["used"])
            self.assertFalse(router.last_routing["fallback_used"])

    def test_unavailable_when_both_down(self):
        local = orch.StubModelClient(raise_unavailable=True)
        transport = StubTransport([
            requests.exceptions.ConnectionError("refused")])
        cloud = cloud_client(transport, max_retries=0)
        router = cm.ModelRouter(local_client=local,
                                cloud_clients=[cloud])
        with self.assertRaises(ModelUnavailable) as ctx:
            router.chat(MESSAGES)
        self.assertIn("no model available", str(ctx.exception))
        self.assertIsNone(router.last_routing["used"])

    def test_cloud_error_falls_through_to_next_client(self):
        local = orch.StubModelClient(raise_unavailable=True)
        bad = cloud_client(StubTransport([
            FakeResponse(200, {"nope": True})]), name="bad-cloud")
        good = cloud_client(StubTransport([
            FakeResponse(200, completion_payload("second cloud"))]),
            name="good-cloud")
        router = cm.ModelRouter(local_client=local,
                                cloud_clients=[bad, good])
        reply, routing = router.chat_with_routing(MESSAGES)
        self.assertEqual(reply, "second cloud")
        self.assertEqual(routing["used"], "good-cloud")

    def test_local_model_client_is_the_ollama_client(self):
        # Step 40 reuses Step 39's Ollama client, not a copy.
        self.assertIs(cm.LocalModelClient,
                      orch.OllamaModelClient)


class ConfigTests(unittest.TestCase):
    """Config loading, validation and secret refusal."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _path(self, name="cloud_models.json"):
        return os.path.join(self.tmp, name)

    def test_missing_file_is_local_only_default(self):
        config = cm.load_config(self._path("absent.json"))
        self.assertEqual(config, {"default": "local", "providers": []})

    def test_save_and_load_round_trip(self):
        config = {
            "default": "local",
            "providers": [{
                "name": "test-cloud",
                "kind": "cloud",
                "model": "test-model",
                "endpoint": "https://example.invalid/v1/chat/completions",
                "api_key_env": TEST_KEY_ENV,
                "input_price_per_1k": 0.01,
                "output_price_per_1k": 0.03,
            }],
        }
        path = cm.save_config(config, self._path())
        loaded = cm.load_config(path)
        self.assertEqual(loaded, config)

    def test_config_refuses_a_secret_key(self):
        for bad_key in ("api_key", "API_KEY", "secret",
                        "auth_token", "password"):
            config = {"providers": [{
                "name": "bad", "model": "m",
                "endpoint": "https://example.invalid",
                bad_key: "shh",
            }]}
            with self.assertRaises(ValueError, msg=bad_key):
                cm.validate_config(config)

    def test_config_requires_name_model_endpoint(self):
        with self.assertRaises(ValueError):
            cm.validate_config({"providers": [{
                "name": "x", "model": "m"}]})

    def test_saved_config_never_contains_the_key(self):
        with mock.patch.dict(os.environ, {TEST_KEY_ENV: TEST_KEY}):
            config = {"default": "local", "providers": [{
                "name": "test-cloud", "model": "test-model",
                "endpoint": "https://example.invalid/v1/chat/completions",
                "api_key_env": TEST_KEY_ENV,
            }]}
            path = cm.save_config(config, self._path())
            with open(path, encoding="utf-8") as handle:
                self.assertNotIn(TEST_KEY, handle.read())

    def test_build_router_from_config(self):
        with mock.patch.dict(os.environ, {TEST_KEY_ENV: TEST_KEY}):
            transport = StubTransport([
                FakeResponse(200, completion_payload("via config"))])
            config = {"default": "local", "providers": [{
                "name": "cfg-cloud", "model": "cfg-model",
                "endpoint": "https://example.invalid/v1/chat/completions",
                "api_key_env": TEST_KEY_ENV,
            }]}
            router = cm.build_router(
                config, http_post=transport,
                local_client=orch.StubModelClient(
                    raise_unavailable=True))
            reply, routing = router.chat_with_routing(MESSAGES)
            self.assertEqual(reply, "via config")
            self.assertTrue(routing["fallback_used"])
            self.assertEqual(routing["used"], "cfg-cloud")


class OrchestratorIntegrationTests(unittest.TestCase):
    """The router as the Step 39 orchestrator's model client."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = mock.patch.dict(
            os.environ, {TEST_KEY_ENV: TEST_KEY})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _router(self):
        local = orch.StubModelClient(raise_unavailable=True)
        transport = StubTransport([
            FakeResponse(200, completion_payload(
                "Hello from the cloud stub."))])
        cloud = cloud_client(transport)
        return cm.ModelRouter(local_client=local,
                              cloud_clients=[cloud])

    def test_chat_turn_falls_back_and_logs_routing(self):
        router = self._router()
        result = orch.handle(
            "Hello, how are you?", model_client=router,
            log_dir=self.tmp)
        self.assertEqual(result["intent"], "chat")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reply"],
                         "Hello from the cloud stub.")
        routing = result.get("model_routing")
        self.assertIsNotNone(routing)
        self.assertTrue(routing["fallback_used"])
        self.assertEqual(routing["used"], "test-cloud")
        # The turn log records the fallback — and no secret.
        log_path = os.path.join(self.tmp, "turns.jsonl")
        with open(log_path, encoding="utf-8") as handle:
            logged = handle.read()
        self.assertNotIn(TEST_KEY, logged)
        record = json.loads(logged.strip().splitlines()[-1])
        self.assertTrue(record["model_routing"]["fallback_used"])

    def test_both_down_is_model_unavailable_not_invented(self):
        router = cm.ModelRouter(
            local_client=orch.StubModelClient(raise_unavailable=True),
            cloud_clients=[])
        result = orch.handle(
            "Hello, how are you?", model_client=router,
            log_dir=self.tmp)
        self.assertEqual(result["status"], "model_unavailable")
        self.assertIn("unreachable", result["reply"])
        self.assertNotIn("Hello from", result["reply"])

    def test_trade_guardrail_still_applies_with_router(self):
        router = self._router()
        result = orch.handle(
            "Buy 100 shares of MSFT at $500", model_client=router,
            log_dir=self.tmp)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["route"], "guardrail_trade")


if __name__ == "__main__":
    unittest.main()
