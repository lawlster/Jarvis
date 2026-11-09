"""Step 40 — Cloud AI model integration.

The integration layer for model providers beyond the local
Ollama instance. It does NOT make any live cloud call and it
does NOT hold any credential:

- The owner has never supplied a cloud API key and has never
  asked for a cloud model. No key is requested, invented or
  stored here. A real cloud call happens only if the owner
  later supplies a key through the Secure Vault flow and
  explicitly approves it.
- Credentials are read from environment variables only, at
  call time, and are never written to a file, a log or a
  config. ``validate_config`` refuses any config that tries
  to carry a secret (an ``api_key`` / ``secret`` / ``token`` /
  ``password`` key).
- ``LocalModelClient`` is the Step 39 ``OllamaModelClient``
  re-exported, not duplicated. Any client exposing
  ``chat(messages) -> str`` can be swapped into the Step 39
  orchestrator.
- ``CloudModelClient`` speaks the OpenAI-compatible chat
  completions shape (``{"model", "messages"}`` in,
  ``choices[0].message.content`` plus optional ``usage`` out)
  through a pluggable HTTP transport, so tests stub it and
  no test ever touches a network.
- ``ModelRouter`` tries local first and falls back to a
  configured cloud client only when local is unavailable
  AND that cloud client has credentials. The fallback
  decision is recorded on ``last_routing`` (and, when the
  router is used as the Step 39 orchestrator's model
  client, in the orchestrator's turn record). If neither
  is available it raises :class:`ModelUnavailable` — the
  same structured "model unavailable" result Step 39 uses
  — and never invents an answer.
- Cost awareness: a cloud call records the model and the
  token counts when the provider reports them, plus an
  estimated cost only when a per-token price is supplied in
  the config. A price is never guessed.

Scope is deliberately tight: provider clients, routing and
fallback, config, cost recording, graceful failure. It does
not wire anything into ``main.py`` (Step 50), does not send
or schedule anything (Step 46), and does not place trades.
"""

import json
import os
import time
from pathlib import Path

import requests

from agent.orchestrator import ModelUnavailable, OllamaModelClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "data" / "cloud_models.json"

# Config keys that would carry a secret. Any provider entry
# containing one of these (case-insensitive, substring match)
# is refused by validate_config.
_SECRET_KEY_MARKERS = (
    "api_key", "apikey", "secret", "token", "password",
    "credential", "auth",
)

TRANSIENT_REASONS = ("unreachable", "timeout", "http_error")


class CloudModelError(ModelUnavailable):
    """A cloud call failed, with a machine-readable reason.

    ``reason`` is one of ``missing_credentials``,
    ``unreachable``, ``timeout``, ``http_error``,
    ``malformed_response`` or ``empty_reply``. It subclasses
    :class:`agent.orchestrator.ModelUnavailable` so the Step 39
    orchestrator's existing failure handling applies unchanged.
    """

    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


# The local client is the Step 39 Ollama client, re-exported
# under the provider-agnostic name. Do not duplicate it.
LocalModelClient = OllamaModelClient


class _Response:
    """The part of an HTTP response the client reads.

    A tiny adapter so the default ``requests`` transport and
    stub transports in tests share one shape.
    """

    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


def _default_http_post(url, json=None, headers=None, timeout=None):
    """The real transport: one ``requests.post`` call."""
    response = requests.post(
        url, json=json, headers=headers, timeout=timeout
    )
    return _Response(
        status_code=response.status_code,
        payload=_safe_json(response),
        text=getattr(response, "text", "") or "",
    )


def _safe_json(response):
    try:
        return response.json()
    except (ValueError, AttributeError):
        return None


class CloudModelClient:
    """A client for one OpenAI-compatible cloud chat endpoint.

    ``api_key_env`` names the environment variable the key is
    read from at call time. The key is never stored on the
    client, never logged and never written anywhere — it only
    ever appears in the per-call ``Authorization`` header.
    ``http_post`` is the transport; pass a stub in tests. No
    live call is ever made unless the owner supplies a key
    and approves it.
    """

    def __init__(self, name, model, endpoint, api_key_env=None,
                 timeout=60, max_retries=2, retry_backoff=1.0,
                 http_post=None, input_price_per_1k=None,
                 output_price_per_1k=None, currency="USD"):
        self.name = name
        self.model = model
        self.endpoint = endpoint
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.http_post = http_post or _default_http_post
        self.input_price_per_1k = input_price_per_1k
        self.output_price_per_1k = output_price_per_1k
        self.currency = currency
        self.last_usage = None
        self.calls = 0

    def __repr__(self):
        # Never include a credential — the client does not
        # hold one, and the representation must not invite one.
        return (
            f"CloudModelClient(name={self.name!r}, "
            f"model={self.model!r}, endpoint={self.endpoint!r})"
        )

    @property
    def has_credentials(self):
        """True when the configured env var holds a key."""
        if not self.api_key_env:
            return False
        value = os.environ.get(self.api_key_env)
        return bool(value and value.strip())

    def _headers(self):
        key = os.environ.get(self.api_key_env or "")
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def _record_usage(self, payload):
        """Token counts and estimated cost, when reported.

        Cost is computed only when the provider reported usage
        AND per-token prices were supplied in the config. A
        price is never guessed, so the cost is None otherwise.
        """
        usage = (payload or {}).get("usage") or {}
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        total = usage.get("total_tokens")
        cost = None
        if (prompt is not None and completion is not None
                and self.input_price_per_1k is not None
                and self.output_price_per_1k is not None):
            try:
                cost = round(
                    prompt / 1000 * self.input_price_per_1k
                    + completion / 1000 * self.output_price_per_1k,
                    6,
                )
            except (TypeError, ValueError):
                cost = None
        self.last_usage = {
            "model": self.model,
            "provider": self.name,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "estimated_cost": cost,
            "currency": self.currency if cost is not None else None,
        }
        return self.last_usage

    def _parse_reply(self, response):
        """Extract the reply text from a response, or fail."""
        status = getattr(response, "status_code", None)
        if status is not None and status >= 400:
            if status in (401, 403):
                raise CloudModelError(
                    f"cloud provider {self.name} refused the "
                    f"request (HTTP {status}) — the configured "
                    "credentials were rejected",
                    "http_error",
                )
            raise CloudModelError(
                f"cloud provider {self.name} returned HTTP {status}",
                "http_error",
            )
        try:
            payload = response.json()
        except (ValueError, AttributeError) as exc:
            raise CloudModelError(
                f"cloud provider {self.name} returned a response "
                "that could not be read as JSON",
                "malformed_response",
            ) from exc
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise CloudModelError(
                f"cloud provider {self.name} returned a response "
                "without a chat completion",
                "malformed_response",
            ) from exc
        if not content:
            raise CloudModelError(
                f"cloud provider {self.name} returned an empty reply",
                "empty_reply",
            )
        self._record_usage(payload)
        return content

    def chat(self, messages):
        """Return the model's reply text for ``messages``."""
        if not self.has_credentials:
            raise CloudModelError(
                f"cloud provider {self.name} has no credentials — "
                f"environment variable {self.api_key_env!r} is not "
                "set. No key is stored anywhere; supply one "
                "through the Secure Vault flow to enable it.",
                "missing_credentials",
            )
        payload = {
            "model": self.model,
            "messages": list(messages),
            "stream": False,
        }
        attempts = 0
        while True:
            attempts += 1
            self.calls += 1
            try:
                response = self.http_post(
                    self.endpoint, json=payload,
                    headers=self._headers(), timeout=self.timeout,
                )
            except requests.exceptions.Timeout as exc:
                if attempts <= self.max_retries:
                    time.sleep(self.retry_backoff)
                    continue
                raise CloudModelError(
                    f"cloud provider {self.name} timed out after "
                    f"{attempts} attempt(s)",
                    "timeout",
                ) from exc
            except requests.exceptions.ConnectionError as exc:
                if attempts <= self.max_retries:
                    time.sleep(self.retry_backoff)
                    continue
                raise CloudModelError(
                    f"cloud provider {self.name} at {self.endpoint} "
                    "is unreachable",
                    "unreachable",
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise CloudModelError(
                    f"cloud provider {self.name} request failed: {exc}",
                    "unreachable",
                ) from exc
            # A stub transport may raise the plain builtins.
            except TimeoutError as exc:
                if attempts <= self.max_retries:
                    time.sleep(self.retry_backoff)
                    continue
                raise CloudModelError(
                    f"cloud provider {self.name} timed out after "
                    f"{attempts} attempt(s)",
                    "timeout",
                ) from exc
            except ConnectionError as exc:
                if attempts <= self.max_retries:
                    time.sleep(self.retry_backoff)
                    continue
                raise CloudModelError(
                    f"cloud provider {self.name} at {self.endpoint} "
                    "is unreachable",
                    "unreachable",
                ) from exc
            # A 5xx is transient: retry it like a transport
            # failure. A 4xx is not retried (see _parse_reply).
            status = getattr(response, "status_code", None)
            if status is not None and status >= 500 \
                    and attempts <= self.max_retries:
                time.sleep(self.retry_backoff)
                continue
            return self._parse_reply(response)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def default_config():
    """The config used when no config file exists: local only."""
    return {"default": "local", "providers": []}


def validate_config(config):
    """Reject a config that is malformed or carries a secret.

    Raises ``ValueError``. A provider entry must be a dict with
    ``name``, ``model`` and ``endpoint``; any key that looks
    like a credential (``api_key``, ``secret``, ``token``,
    ``password``, ``credential``, ``auth``) is refused — keys
    are read from environment variables only.
    """
    if not isinstance(config, dict):
        raise ValueError("cloud model config must be a JSON object")
    providers = config.get("providers", [])
    if not isinstance(providers, list):
        raise ValueError("cloud model config 'providers' must be a list")
    for provider in providers:
        if not isinstance(provider, dict):
            raise ValueError("each provider must be a JSON object")
        for key in provider:
            lowered = str(key).lower()
            # api_key_env names an environment variable — it is
            # not a secret itself, so it is exempt.
            if lowered == "api_key_env":
                continue
            if any(marker in lowered for marker in _SECRET_KEY_MARKERS):
                raise ValueError(
                    f"provider {provider.get('name')!r} carries a "
                    f"secret-looking key {key!r} — credentials are "
                    "read from environment variables only and must "
                    "never be stored in the config file"
                )
        for field in ("name", "model", "endpoint"):
            if not provider.get(field):
                raise ValueError(
                    f"provider {provider.get('name')!r} is missing "
                    f"required field {field!r}"
                )
    return config


def load_config(path=None):
    """Load the config file, or the local-only default.

    A missing file is not an error — it means no cloud
    provider is configured. A malformed file or one carrying
    a secret raises ``ValueError``.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return default_config()
    with open(config_path, encoding="utf-8") as handle:
        try:
            config = json.load(handle)
        except ValueError as exc:
            raise ValueError(
                f"cloud model config {config_path} is not valid JSON"
            ) from exc
    return validate_config(config)


def save_config(config, path=None):
    """Write ``config`` after validating it (no secrets)."""
    validate_config(config)
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return str(config_path)


def build_router(config, http_post=None, local_client=None):
    """Build a :class:`ModelRouter` from a config dict.

    Providers with ``kind`` ``"cloud"`` become
    :class:`CloudModelClient` instances (``api_key_env`` names
    the env var holding the key); ``kind`` ``"local"``
    becomes an :class:`OllamaModelClient`. A provider without
    a ``kind`` is treated as cloud.
    """
    validate_config(config)
    clouds = []
    local = local_client
    for provider in config.get("providers", []):
        kind = (provider.get("kind") or "cloud").lower()
        if kind == "local":
            if local is None:
                local = OllamaModelClient(
                    model=provider.get("model", "qwen3:8b"),
                    url=provider.get(
                        "endpoint", "http://localhost:11434/api/chat"),
                )
            continue
        clouds.append(CloudModelClient(
            name=provider["name"],
            model=provider["model"],
            endpoint=provider["endpoint"],
            api_key_env=provider.get("api_key_env"),
            timeout=provider.get("timeout", 60),
            max_retries=provider.get("max_retries", 2),
            http_post=http_post,
            input_price_per_1k=provider.get("input_price_per_1k"),
            output_price_per_1k=provider.get("output_price_per_1k"),
            currency=provider.get("currency", "USD"),
        ))
    return ModelRouter(
        local_client=local,
        cloud_clients=clouds,
        default=config.get("default", "local"),
    )


# ---------------------------------------------------------------------------
# Routing: local first, cloud only as a configured fallback
# ---------------------------------------------------------------------------

class ModelRouter:
    """Route a chat call: local first, cloud on failure.

    The cloud fallback runs only when a cloud client is
    configured AND has credentials. Every call records its
    routing decision on ``last_routing`` — attempted clients,
    which one answered, whether the fallback ran and why —
    so the Step 39 orchestrator can log it in the turn
    record. If nothing answers, :class:`ModelUnavailable`
    is raised; no answer is ever invented.
    """

    def __init__(self, local_client=None, cloud_clients=None,
                 default="local"):
        self.local_client = local_client or OllamaModelClient()
        self.cloud_clients = list(cloud_clients or [])
        self.default = default
        self.last_routing = None

    def _client_name(self, client):
        return getattr(client, "name", None) or getattr(
            client, "model", None) or type(client).__name__

    def _has_credentials(self, client):
        # A client that does not report credentials (e.g. a
        # plain stub) is treated as credentialed; a cloud
        # client reports False when its env var is unset.
        return getattr(client, "has_credentials", True)

    def chat(self, messages):
        reply, _routing = self.chat_with_routing(messages)
        return reply

    def chat_with_routing(self, messages):
        attempted = []
        errors = []
        # Local first (or first, whenever it is the default).
        try:
            reply = self.local_client.chat(messages)
            self.last_routing = {
                "attempted": [self._client_name(self.local_client)],
                "used": self._client_name(self.local_client),
                "fallback_used": False,
                "fallback_reason": None,
                "error": None,
            }
            return reply, self.last_routing
        except ModelUnavailable as exc:
            attempted.append(self._client_name(self.local_client))
            errors.append(str(exc))
            local_error = str(exc)
        # Fallback: configured cloud clients with credentials.
        for client in self.cloud_clients:
            name = self._client_name(client)
            if not self._has_credentials(client):
                attempted.append(name + " (no credentials)")
                continue
            attempted.append(name)
            try:
                reply = client.chat(messages)
                self.last_routing = {
                    "attempted": attempted,
                    "used": name,
                    "fallback_used": True,
                    "fallback_reason": (
                        f"local model unavailable: {local_error}"
                    ),
                    "error": None,
                }
                return reply, self.last_routing
            except ModelUnavailable as exc:
                errors.append(str(exc))
        self.last_routing = {
            "attempted": attempted,
            "used": None,
            "fallback_used": False,
            "fallback_reason": None,
            "error": "; ".join(errors) or "no model available",
        }
        raise ModelUnavailable(
            "no model available — local model unavailable and no "
            "configured cloud model with credentials answered: "
            + "; ".join(errors)
        )

    def available_clients(self):
        """Names of the clients the router could call."""
        names = [self._client_name(self.local_client)]
        names += [self._client_name(c) for c in self.cloud_clients]
        return names
