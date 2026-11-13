"""Tests for ChatGPT client authentication and provider integration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from tradingagents import default_config
from tradingagents.llm_clients import chatgpt_auth, factory
from tradingagents.llm_clients.openai_client import (
    ChatGPTPlanChatOpenAI,
    SIGNED_IN_PROVIDERS,
)


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(chatgpt_auth, "credential", lambda: (lambda: "plan-token"))
    monkeypatch.setattr(chatgpt_auth, "access_token", lambda: "plan-token")
    monkeypatch.setattr(chatgpt_auth, "signed_in_as", lambda: "a@example.com")


def _llm(**kwargs):
    config = {
        **default_config.DEFAULT_CONFIG,
        "llm_provider": "chatgpt",
        "openai_reasoning_effort": "high",
        **kwargs,
    }
    return factory.create_llm_client(
        "chatgpt", "gpt-5.5", **factory.build_llm_kwargs(config)
    ).get_llm()


# --- the client ------------------------------------------------------------------

@pytest.mark.unit
def test_the_plan_is_reached_with_the_sign_in_never_an_api_key(signed_in, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-used")
    llm = _llm()
    assert isinstance(llm, ChatGPTPlanChatOpenAI)
    assert llm.openai_api_key() == "plan-token"
    assert llm.reasoning_effort == "high"


@pytest.mark.unit
def test_a_chatgpt_tier_beside_an_openai_tier_keeps_each_credential(signed_in, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    config = {
        **default_config.DEFAULT_CONFIG,
        "llm_provider": "openai",
        "backend_url": "https://api.openai.com/v1",
        "quick_think_provider": "chatgpt",
        "quick_think_llm": "gpt-5.5",
        "deep_think_llm": "gpt-6-sol",
        "openai_reasoning_effort": "low",
    }
    quick = factory.create_tier_client(config, "quick").get_llm()
    deep = factory.create_tier_client(config, "deep").get_llm()
    assert isinstance(quick, ChatGPTPlanChatOpenAI) and quick.reasoning_effort == "low"
    assert quick.openai_api_key() == "plan-token"
    assert not isinstance(deep, ChatGPTPlanChatOpenAI)
    assert deep.openai_api_key.get_secret_value() == "sk-openai"


@pytest.mark.unit
def test_the_plan_s_listed_models_are_offered_in_openai_s_order(signed_in, monkeypatch):
    monkeypatch.setattr(chatgpt_auth, "access_token", lambda: "plan-token")
    body = {
        "models": [
            {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list", "priority": 9},
            {"slug": "internal", "display_name": "Internal", "visibility": "hide", "priority": 0},
            {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list", "priority": 2},
        ]
    }
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)
    with mock.patch("requests.get", return_value=response) as get:
        assert chatgpt_auth.list_models() == [("GPT-6-Astra", "gpt-6-astra"), ("GPT-5.5", "gpt-5.5")]
    assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer plan-token"}


@pytest.mark.unit
def test_api_organization_and_project_do_not_travel_with_the_sign_in(signed_in, monkeypatch):
    monkeypatch.setenv("OPENAI_ORG_ID", "org-for-api-keys")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-for-api-keys")
    llm = _llm()
    for client in (llm.root_client, llm.root_async_client):
        sent = {k: v for k, v in client.default_headers.items() if isinstance(v, str)}
        assert "OpenAI-Organization" not in sent and "OpenAI-Project" not in sent


@pytest.mark.unit
def test_a_malformed_model_entry_does_not_hide_the_others(signed_in):
    body = {
        "models": [
            {"display_name": "No slug", "visibility": "list"},
            {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list"},
        ]
    }
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)
    with mock.patch("requests.get", return_value=response):
        assert chatgpt_auth.list_models() == [("GPT-5.5", "gpt-5.5")]


@pytest.mark.unit
def test_the_chatgpt_plan_is_the_only_provider_signed_in_to():
    assert SIGNED_IN_PROVIDERS == ("chatgpt",)
