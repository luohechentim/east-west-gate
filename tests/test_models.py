"""Tests for the pluggable model-client registry (double_gate.models)."""

import builtins
import json

import pytest

from double_gate import models


def test_stub_client_returns_structured_json():
    client = models.get_client("stub")
    response = client.chat("system", ["hello"])
    assert response.success is True
    assert response.vendor == "stub"
    assert response.error is None

    data = json.loads(response.content)
    assert data["verdict"] in models.REVIEW_VERDICTS
    assert isinstance(data["score"], (int, float))
    assert isinstance(data["findings"], list)
    assert isinstance(data["strengths"], list)
    assert isinstance(data["confidence"], (int, float))


def test_stub_default_verdict_is_minor_issues():
    client = models.get_client("stub")
    data = json.loads(client.chat("s", ["m"]).content)
    assert data["verdict"] == "minor-issues"


def test_stub_verdict_env_override(monkeypatch):
    monkeypatch.setenv("STUB_VERDICT", "serious-issues")
    client = models.get_client("stub")
    data = json.loads(client.chat("s", ["m"]).content)
    assert data["verdict"] == "serious-issues"


def test_stub_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv("STUB_VERDICT", "not-a-verdict")
    client = models.get_client("stub")
    data = json.loads(client.chat("s", ["m"]).content)
    assert data["verdict"] == "minor-issues"


def test_register_and_get_client_factory():
    assert "stub" in models._MODEL_REGISTRY
    assert "openai_compatible" in models._MODEL_REGISTRY
    client = models.get_client("stub")
    assert isinstance(client, models.StubClient)


def test_unknown_vendor_raises():
    with pytest.raises(ValueError):
        models.get_client("no-such-vendor")


def test_call_panel_parallel_and_vendor_filtering():
    panel = [
        {"vendor": "stub", "model": "stub-a", "role": "primary"},
        {"vendor": "stub", "model": "stub-b", "role": "critic"},
        {"vendor": "no-such-vendor", "model": "x", "role": "guard"},
    ]
    results = models.call_panel("system", "user", panel)
    assert len(results) == 3
    for response in results[:2]:
        assert response.success is True
        assert json.loads(response.content)["verdict"] == "minor-issues"
    assert results[2].success is False
    assert "unknown model vendor" in results[2].error


def test_call_panel_empty_spec():
    assert models.call_panel("s", "u", []) == []


def test_call_panel_survives_client_exception():
    class BoomClient(models.BaseClient):
        vendor = "boom"

        def chat(self, *args, **kwargs):
            raise RuntimeError("boom")

    BoomClient.register()
    results = models.call_panel("s", "u", [{"vendor": "boom", "model": "m"}])
    assert len(results) == 1
    assert results[0].success is False
    assert "boom" in results[0].error


def test_role_aware_chat_injects_role_prefix():
    class RecordingClient(models.BaseClient):
        vendor = "recording"

        def __init__(self, **kwargs):
            self.system_prompts = []

        def chat(self, system, messages, model=None, **kwargs):
            self.system_prompts.append(system)
            return models.ModelResponse(content="{}", vendor=self.vendor, success=True)

    RecordingClient.register()
    recorder = models.get_client("recording")
    models.role_aware_chat(recorder, "critic", "Base system", ["hello"])
    assert recorder.system_prompts[0].startswith("You are a critical skeptic")
    assert "Base system" in recorder.system_prompts[0]


def test_role_aware_chat_unknown_role_passthrough():
    client = models.get_client("stub")
    response = models.role_aware_chat(client, "mystery-role", "sys", ["m"])
    assert response.success is True


def test_role_aware_chat_accepts_string_message():
    class RecordingClient(models.BaseClient):
        vendor = "recording2"

        def __init__(self, **kwargs):
            self.last = None

        def chat(self, system, messages, model=None, **kwargs):
            self.last = (system, messages)
            return models.ModelResponse(content="{}", vendor=self.vendor, success=True)

    RecordingClient.register()
    recorder = models.get_client("recording2")
    models.role_aware_chat(recorder, None, "sys", "just a string")
    assert isinstance(recorder.last[1], list)
    assert recorder.last[1][0]["role"] == "user"
    assert recorder.last[1][0]["content"] == "just a string"


def test_model_response_defaults():
    response = models.ModelResponse(content="x", vendor="v")
    assert response.success is False
    assert response.error is None
    assert response.duration_ms is None
    assert response.usage is None


def test_requests_is_not_a_hard_dependency():
    # requests is only imported lazily inside chat(); the module must not
    # expose it and must import cleanly without it.
    assert not hasattr(models, "requests")


def test_openai_client_uses_urllib_fallback_when_requests_missing(monkeypatch):
    captured = {}

    def fake_post_urllib(self, url, payload, timeout):
        captured["url"] = url
        captured["payload"] = payload
        return {
            "choices": [{"message": {"content": '{"verdict":"approved"}'}}],
            "usage": {"total_tokens": 5},
        }

    monkeypatch.setattr(
        models.OpenAICompatibleClient, "_post_json_urllib", fake_post_urllib
    )

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "requests":
            raise ImportError("requests is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    client = models.get_client("openai_compatible")
    response = client.chat("s", [{"role": "user", "content": "m"}], model="test-model")
    assert response.success is True
    assert json.loads(response.content)["verdict"] == "approved"
    assert "chat/completions" in captured["url"]
    assert captured["payload"]["model"] == "test-model"
    assert response.usage == {"total_tokens": 5}


def test_openai_client_failure_is_silent(monkeypatch):
    def fail_post(self, url, payload, timeout):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(models.OpenAICompatibleClient, "_post_json", fail_post)
    client = models.get_client("openai_compatible")
    response = client.chat("s", ["m"])
    assert response.success is False
    assert "network unreachable" in response.error
    assert response.duration_ms is not None


def test_per_member_client_kwargs_are_applied():
    class ConfigClient(models.BaseClient):
        vendor = "config-test"

        def __init__(self, marker=None, **kwargs):
            self.marker = marker

        def chat(self, *args, **kwargs):
            return models.ModelResponse(
                content=json.dumps({"verdict": "approved", "marker": self.marker}),
                model="config-test",
                vendor=self.vendor,
                success=True,
            )

    ConfigClient.register()
    results = models.call_panel(
        "system",
        "user",
        [{"vendor": "config-test", "model": "test", "client_kwargs": {"marker": "seat-a"}}],
    )
    assert json.loads(results[0].content)["marker"] == "seat-a"
