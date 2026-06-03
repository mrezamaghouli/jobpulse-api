import pytest

from scripts.providers.provider_factory import get_job_provider
from scripts.providers.json_provider import JsonJobProvider
from scripts.providers.linkedin_provider_placeholder import LinkedInProviderPlaceholder


def test_provider_factory_returns_json_provider(monkeypatch):
    monkeypatch.setenv("JOB_PROVIDER", "json")

    provider = get_job_provider()

    assert isinstance(provider, JsonJobProvider)


def test_provider_factory_returns_linkedin_placeholder(monkeypatch):
    monkeypatch.setenv("JOB_PROVIDER", "linkedin")

    provider = get_job_provider()

    assert isinstance(provider, LinkedInProviderPlaceholder)


def test_provider_factory_rejects_unknown_provider(monkeypatch):
    monkeypatch.setenv("JOB_PROVIDER", "unknown")

    with pytest.raises(ValueError):
        get_job_provider()