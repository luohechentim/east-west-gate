"""Pluggable model-client registry for cross-vendor review panels.

A review panel is only trustworthy when its members come from independent
model vendors. This module is a thin, vendor-agnostic abstraction over LLM
chat clients so a panel can be assembled from many vendors behind one factory.

The module itself only depends on the standard library. ``requests`` is an
*optional* dependency: an OpenAI-compatible client falls back to stdlib
``urllib`` when ``requests`` is not installed, and a deterministic offline
``StubClient`` never touches the network at all, so the whole project runs and
is testable with zero API keys.
"""

from __future__ import annotations

import json
import os
import re
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

__all__ = [
    "ModelResponse",
    "BaseClient",
    "OpenAICompatibleClient",
    "StubClient",
    "get_client",
    "call_panel",
    "role_aware_chat",
    "ROLE_PREFIXES",
    "registered_vendors",
]

#: canonical review verdicts shared with the prompt templates and adjudicator
REVIEW_VERDICTS = ("approved", "minor-issues", "serious-issues", "critical")

_MODEL_REGISTRY: Dict[str, type] = {}
_REGISTRY_LOCK = Lock()


@dataclass
class ModelResponse:
    """A structured, failure-safe result of a single chat call.

    ``success=False`` means the call failed for any reason (auth, timeout,
    malformed reply, network). Clients are expected to *never* raise; errors
    are captured in ``error`` instead.
    """

    content: Optional[str] = None
    model: Optional[str] = None
    vendor: str = ""
    success: bool = False
    duration_ms: Optional[float] = None
    error: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None


class BaseClient(ABC):
    """Abstract chat client.

    Subclasses set a ``vendor`` class attribute and implement :meth:`chat`.
    Call ``Subclass.register()`` to publish the client to the global registry
    so it can be reached through :func:`get_client` / :func:`call_panel`.
    """

    vendor: str = "base"

    @abstractmethod
    def chat(
        self,
        system: str,
        messages: List[Any],
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Send a chat request and return a structured :class:`ModelResponse`.

        ``system`` is the system prompt. ``messages`` is a list of strings or
        ``{"role": ..., "content": ...}`` dicts. Implementations must not
        raise; failures are reported through ``success`` / ``error``.
        """

    @classmethod
    def register(cls) -> type:
        """Register this client class in the global registry under its vendor."""
        with _REGISTRY_LOCK:
            _MODEL_REGISTRY[getattr(cls, "vendor")] = cls
        return cls

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not getattr(cls, "vendor", None):
            cls.vendor = cls.__name__.lower()


def get_client(vendor: str, **kwargs: Any) -> BaseClient:
    """Factory: instantiate the registered client class for ``vendor``.

    Raises ``ValueError`` for unknown vendors.
    """
    normalized_vendor = str(vendor or "").strip()
    with _REGISTRY_LOCK:
        cls = _MODEL_REGISTRY.get(normalized_vendor)
    if cls is None:
        raise ValueError(f"unknown model vendor: {normalized_vendor!r}")
    return cls(**kwargs)


def registered_vendors() -> List[str]:
    """Return registered client identifiers in deterministic order."""
    with _REGISTRY_LOCK:
        return sorted(_MODEL_REGISTRY)


def _safe_error(exc: Exception, api_key: Optional[str] = None) -> str:
    """Return an error safe to persist in an audit report.

    Some HTTP client exceptions include request headers or URLs.  Review reports
    are frequently saved or shared, so never allow an API key or bearer token to
    escape through the error path.
    """
    message = str(exc)
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    message = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", message)
    return message[:500] or exc.__class__.__name__


class OpenAICompatibleClient(BaseClient):
    """Chat client for any OpenAI-compatible ``/chat/completions`` endpoint.

    Configuration is read from environment variables at construction time so
    no secrets live in source code:

      ``OPENAI_BASE_URL``  default ``https://api.openai.com/v1``
      ``OPENAI_API_KEY``   optional bearer token (omitted when unset)
      ``OPENAI_MODEL``     default model id
    """

    vendor = "openai_compatible"

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.3,
        timeout: float = 120.0,
        **kwargs: Any,
    ) -> None:
        resolved_base_url = (
            base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        ).strip().rstrip("/")
        parsed_url = urlparse(resolved_base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        self.base_url = resolved_base_url
        self.api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        self.model = model if model is not None else os.environ.get("OPENAI_MODEL") or "gpt-4o"
        self.temperature = float(temperature)
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout must be greater than zero")

    def chat(
        self,
        system: str,
        messages: List[Any],
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> ModelResponse:
        start = time.monotonic()
        target_model = model or self.model
        temperature = float(kwargs.get("temperature", self.temperature))
        timeout = float(kwargs.get("timeout", self.timeout))
        if timeout <= 0:
            return ModelResponse(
                vendor=self.vendor,
                model=target_model,
                success=False,
                duration_ms=(time.monotonic() - start) * 1000,
                error="timeout must be greater than zero",
            )

        payload: Dict[str, Any] = {
            "model": target_model,
            "messages": [{"role": "system", "content": system}],
            "temperature": temperature,
        }
        for message in messages or []:
            if isinstance(message, str):
                payload["messages"].append({"role": "user", "content": message})
            elif isinstance(message, dict):
                payload["messages"].append(
                    {"role": message.get("role", "user"), "content": message.get("content", "")}
                )
        for key in ("max_tokens", "top_p", "frequency_penalty", "presence_penalty", "stop"):
            if key in kwargs:
                payload[key] = kwargs[key]

        url = f"{self.base_url}/chat/completions"
        try:
            data = self._post_json(url, payload, timeout)
            choices = data.get("choices") or []
            content = None
            if choices and isinstance(choices[0], dict):
                message = choices[0].get("message") or {}
                content = message.get("content")
            usage = data.get("usage")
            return ModelResponse(
                content=content,
                model=target_model,
                vendor=self.vendor,
                success=content is not None,
                duration_ms=(time.monotonic() - start) * 1000,
                error=None if content is not None else "empty response content",
                usage=usage if isinstance(usage, dict) else None,
            )
        except Exception as exc:  # never propagate: mark the call as failed
            return ModelResponse(
                vendor=self.vendor,
                model=target_model,
                success=False,
                duration_ms=(time.monotonic() - start) * 1000,
                error=_safe_error(exc, self.api_key),
            )

    def _post_json(self, url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        """POST JSON, preferring ``requests`` when available."""
        try:
            import requests
        except ImportError:
            return self._post_json_urllib(url, payload, timeout)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()

    def _post_json_urllib(
        self, url: str, payload: Dict[str, Any], timeout: float
    ) -> Dict[str, Any]:
        """stdlib fallback for ``_post_json`` when ``requests`` is missing."""
        import urllib.request

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
        return json.loads(body)


class StubClient(BaseClient):
    """Deterministic offline client used for tests and zero-API-key demos.

    ``chat`` never touches the network: it returns a fixed, schema-shaped JSON
    review. The verdict defaults to ``minor-issues`` and can be overridden per
    instance or via the ``STUB_VERDICT`` environment variable.
    """

    vendor = "stub"

    _SCORES = {"approved": 90, "minor-issues": 73, "serious-issues": 55, "critical": 35}

    _FINDINGS: Dict[str, List[Dict[str, Any]]] = {
        "approved": [
            {
                "severity": "minor",
                "category": "clarity",
                "summary": "The submission is clear and well structured overall.",
                "detail": (
                    "A few sentences could be tightened for precision, "
                    "but nothing blocks progress."
                ),
                "suggestion": "Minor copy polish only; no structural changes required.",
            }
        ],
        "minor-issues": [
            {
                "severity": "minor",
                "category": "clarity",
                "summary": "Some sections could be stated more precisely.",
                "detail": (
                    "A couple of key terms are used loosely, which may lead "
                    "to ambiguous interpretation."
                ),
                "suggestion": "Define the key terms explicitly in the opening section.",
            },
            {
                "severity": "info",
                "category": "completeness",
                "summary": "Consider adding a worked example.",
                "detail": (
                    "A concrete example would help readers verify their "
                    "understanding of the approach."
                ),
                "suggestion": "Add one small illustrative example near the end.",
            },
        ],
        "serious-issues": [
            {
                "severity": "major",
                "category": "logic",
                "summary": "One of the core arguments is not fully supported.",
                "detail": (
                    "The reasoning jumps from premises to a conclusion without "
                    "addressing a key countercase."
                ),
                "suggestion": (
                    "Add explicit handling of the main countercase before "
                    "drawing the conclusion."
                ),
            },
            {
                "severity": "minor",
                "category": "clarity",
                "summary": "Some sections could be stated more precisely.",
                "detail": "A couple of key terms are used loosely.",
                "suggestion": "Define the key terms explicitly.",
            },
        ],
        "critical": [
            {
                "severity": "critical",
                "category": "correctness",
                "summary": "A fundamental flaw invalidates the main conclusion.",
                "detail": (
                    "The central claim depends on an assumption that does not "
                    "hold for the stated scope."
                ),
                "suggestion": "Re-derive the conclusion from corrected assumptions.",
            },
            {
                "severity": "major",
                "category": "completeness",
                "summary": "The submission is missing key evidence.",
                "detail": "Evidence required to support the main claim is absent.",
                "suggestion": "Provide the missing evidence or narrow the claim accordingly.",
            },
        ],
    }

    _STRENGTHS: Dict[str, List[str]] = {
        "approved": ["Clear structure and consistent terminology."],
        "minor-issues": ["The overall direction is sound and the scope is appropriate."],
        "serious-issues": ["The author addresses a genuinely important problem."],
        "critical": ["The topic is relevant even though the execution has flaws."],
    }

    def __init__(self, verdict: Optional[str] = None, **kwargs: Any) -> None:
        chosen = (verdict or os.environ.get("STUB_VERDICT") or "minor-issues").strip().lower()
        self.verdict = chosen if chosen in REVIEW_VERDICTS else "minor-issues"

    def chat(
        self,
        system: str,
        messages: List[Any],
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> ModelResponse:
        start = time.monotonic()
        content = json.dumps(
            {
                "verdict": self.verdict,
                "score": self._SCORES[self.verdict],
                "findings": self._FINDINGS[self.verdict],
                "strengths": self._STRENGTHS[self.verdict],
                "confidence": 0.8,
            },
            ensure_ascii=False,
        )
        return ModelResponse(
            content=content,
            model=model or "stub-v1",
            vendor=self.vendor,
            success=True,
            duration_ms=(time.monotonic() - start) * 1000,
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )


#: role prefix -> english persona injected ahead of the system prompt
ROLE_PREFIXES: Dict[str, str] = {
    "guard": (
        "You are a cautious guard reviewer. Your job is to be conservative and "
        "flag every possible risk or flaw, even unlikely ones."
    ),
    "critic": (
        "You are a critical skeptic reviewer. Your job is to actively challenge "
        "assumptions and hunt for weaknesses rather than confirming them."
    ),
    "primary": (
        "You are the primary reviewer. Provide a balanced, thorough and rigorous "
        "assessment of the submission."
    ),
}


def role_aware_chat(
    client: BaseClient,
    role: Optional[str],
    system: str,
    messages: Optional[Any] = None,
    **kwargs: Any,
) -> ModelResponse:
    """Call ``client.chat`` with a role-specific persona injected.

    ``role`` is one of ``"guard"`` / ``"critic"`` / ``"primary"`` (or any
    string / ``None``). When a known role is given, its persona prefix is
    prepended to the system prompt, giving one model several distinct
    viewpoints without changing any vendor code.
    """
    effective = system
    prefix = ROLE_PREFIXES.get((role or "").strip().lower())
    if prefix:
        effective = f"{prefix}\n\n{system}"
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    return client.chat(effective, messages or [], **kwargs)


def _run_member(
    client: BaseClient,
    role: Optional[str],
    system: str,
    user_message: Any,
    model: Optional[str],
) -> ModelResponse:
    try:
        response = role_aware_chat(client, role, system, user_message, model=model)
        if not response.vendor:
            response.vendor = client.vendor
        if not response.model:
            response.model = model
        return response
    except Exception as exc:  # defensive: a misbehaving client must not kill the panel
        return ModelResponse(
            vendor=client.vendor,
            model=model,
            success=False,
            error=_safe_error(exc),
        )


def _member_client_kwargs(member: Dict[str, Any]) -> Dict[str, Any]:
    """Extract safe per-seat client settings from a panel member definition.

    A panel often needs distinct OpenAI-compatible gateways and API-key
    environment variables.  Keeping those settings on the member (rather than
    only in process-wide environment variables) lets a panel genuinely combine
    providers without putting secrets in version-controlled JSON.
    """
    configured = member.get("client_kwargs", {})
    if configured is None:
        configured = {}
    if not isinstance(configured, dict):
        raise ValueError("panel member client_kwargs must be a mapping")
    kwargs = dict(configured)
    for key in ("base_url", "api_key", "temperature", "timeout"):
        if key in member:
            kwargs[key] = member[key]
    api_key_env = member.get("api_key_env")
    if api_key_env:
        if not isinstance(api_key_env, str):
            raise ValueError("panel member api_key_env must be a string")
        api_key = os.environ.get(api_key_env)
        if api_key is None:
            raise ValueError(f"panel member API-key environment variable is not set: {api_key_env}")
        kwargs["api_key"] = api_key
    return kwargs


def call_panel(
    system: str,
    user_message: Any,
    panel_spec: List[Dict[str, Any]],
    max_workers: int = 4,
) -> List[ModelResponse]:
    """Run a panel of reviewers concurrently and preserve every requested seat.

    ``panel_spec`` is a list of member dicts ``{"vendor": str, "model": str,
    "role": str}``. ``role`` is optional.  Configuration errors are represented
    as failed :class:`ModelResponse` objects rather than being silently dropped;
    otherwise a typo could make a three-reviewer panel look healthy after only one
    reviewer answered.  Results are returned in the same order as ``panel_spec``.
    """
    members = list(panel_spec or [])
    if not members:
        return []
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")

    results: List[Optional[ModelResponse]] = [None] * len(members)
    with ThreadPoolExecutor(max_workers=min(max_workers, len(members))) as executor:
        futures = {}
        for index, member in enumerate(members):
            if not isinstance(member, dict):
                results[index] = ModelResponse(
                    success=False,
                    error="panel member must be a mapping",
                )
                continue
            vendor = str(member.get("vendor") or "").strip()
            model = member.get("model")
            if not vendor:
                results[index] = ModelResponse(
                    model=model,
                    success=False,
                    error="panel member is missing a vendor",
                )
                continue
            try:
                client = get_client(vendor, **_member_client_kwargs(member))
            except ValueError as exc:
                results[index] = ModelResponse(
                    vendor=vendor,
                    model=model,
                    success=False,
                    error=_safe_error(exc),
                )
                continue
            futures[index] = executor.submit(
                _run_member,
                client,
                member.get("role"),
                system,
                user_message,
                model,
            )
        for index, future in futures.items():
            results[index] = future.result()

    return [result for result in results if result is not None]


OpenAICompatibleClient.register()
StubClient.register()
