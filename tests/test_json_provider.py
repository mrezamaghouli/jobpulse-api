import json

import pytest

from scripts.providers.json_provider import JsonJobProvider


def test_json_provider_reads_jobs(tmp_path):
    jobs_file = tmp_path / "jobs.json"

    jobs = [
        {
            "linkedin_job_id": "li-test-1",
            "title": "UX Designer",
            "company": "Test Company",
            "location": "Berlin, Germany",
            "remote": True,
            "source": "LinkedIn",
            "job_url": "https://www.linkedin.com/jobs/view/li-test-1"
        }
    ]

    jobs_file.write_text(
        json.dumps(jobs),
        encoding="utf-8"
    )

    provider = JsonJobProvider(jobs_file)
    result = provider.fetch_jobs()

    assert len(result) == 1
    assert result[0]["title"] == "UX Designer"
    assert result[0]["source"] == "LinkedIn"


def test_json_provider_returns_empty_list_for_missing_file(tmp_path):
    missing_file = tmp_path / "missing.json"

    provider = JsonJobProvider(missing_file)
    result = provider.fetch_jobs()

    assert result == []


def test_json_provider_rejects_non_list_json(tmp_path):
    jobs_file = tmp_path / "jobs.json"

    jobs_file.write_text(
        json.dumps({"title": "Invalid"}),
        encoding="utf-8"
    )

    provider = JsonJobProvider(jobs_file)

    with pytest.raises(ValueError):
        provider.fetch_jobs()