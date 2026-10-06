"""LinkedIn job description extraction and preservation.

Covers `LinkedInBrowserProvider.extract_detail_for_job()` (the only place
the description is read from the detail page) and both normalization
paths the description travels through before persistence:
`LinkedInBrowserProvider.normalize_job()` and
`scripts.collector_postgres.normalize_job()`.
"""

from scripts import collector_postgres
from scripts.providers.linkedin_browser_provider import (
    LINKEDIN_DESCRIPTION_WAIT_SELECTOR,
    LinkedInBrowserProvider,
)


DESCRIPTION = "We are hiring a Python engineer.\nBuild data pipelines."

JOB = {
    "job_url": "https://www.linkedin.com/jobs/view/4012345678/",
    "linkedin_job_id": "4012345678",
    "title": "Python Engineer",
    "company": "Acme",
    "location": "Berlin, Germany",
}


class _EmptyLocator:
    @property
    def first(self):
        return self

    def count(self):
        return 0


class FakePage:
    def __init__(self, detail_data, wait_raises=False):
        self._detail_data = detail_data
        self._wait_raises = wait_raises
        self.wait_calls = []
        self.scripts = []

    def locator(self, selector):
        return _EmptyLocator()

    def wait_for_timeout(self, ms):
        pass

    def wait_for_selector(self, selector, **kwargs):
        self.wait_calls.append((selector, kwargs))

        if self._wait_raises:
            raise TimeoutError("simulated description wait timeout")

    def evaluate(self, script):
        self.scripts.append(script)
        return self._detail_data


def _provider():
    provider = LinkedInBrowserProvider.__new__(LinkedInBrowserProvider)
    provider.extract_apply_info_from_detail_page = lambda page, job_url: {}
    provider.extract_poster_info_from_detail_page = lambda page: {}
    return provider


def _detail_data(description):
    return {
        "detail_title": "Python Engineer",
        "detail_company": "Acme",
        "detail_location": "Berlin, Germany",
        "details_text": "",
        "job_description": description,
        "job_about": description,
    }


def test_detail_page_with_description_produces_non_empty_description():
    page = FakePage(_detail_data(DESCRIPTION))

    detail = _provider().extract_detail_for_job(page, dict(JOB))

    assert detail["job_description"] == DESCRIPTION
    assert detail["job_about"] == DESCRIPTION


def test_waits_for_description_container_before_extracting():
    page = FakePage(_detail_data(DESCRIPTION))

    _provider().extract_detail_for_job(page, dict(JOB))

    assert len(page.wait_calls) == 1
    selector, kwargs = page.wait_calls[0]
    assert selector == LINKEDIN_DESCRIPTION_WAIT_SELECTOR
    assert kwargs.get("timeout")


def test_description_wait_timeout_does_not_fail_extraction():
    page = FakePage(_detail_data(DESCRIPTION), wait_raises=True)

    detail = _provider().extract_detail_for_job(page, dict(JOB))

    assert detail["job_description"] == DESCRIPTION


def test_extraction_script_keeps_legacy_selectors_and_adds_heading_fallback():
    page = FakePage(_detail_data(DESCRIPTION))

    _provider().extract_detail_for_job(page, dict(JOB))

    script = page.scripts[0]

    for selector in (
        ".jobs-description-content__text",
        ".jobs-box__html-content",
        "#job-details",
        ".jobs-description__content",
        ".jobs-description",
    ):
        assert selector in script
        assert selector in LINKEDIN_DESCRIPTION_WAIT_SELECTOR

    assert "about the job" in script
    assert "getAboutTheJobText()" in script


def test_missing_description_stays_none_and_job_is_still_returned():
    page = FakePage(_detail_data(None))

    detail = _provider().extract_detail_for_job(page, dict(JOB))

    assert detail["job_description"] is None
    assert detail["title"] == "Python Engineer"


def test_description_survives_provider_normalization():
    provider = _provider()
    page = FakePage(_detail_data(DESCRIPTION))

    raw_job = {**JOB, **provider.extract_detail_for_job(page, dict(JOB))}
    normalized = provider.normalize_job(raw_job)

    assert normalized["job_description"] == DESCRIPTION
    assert normalized["job_about"] == DESCRIPTION


def test_description_survives_collector_normalization():
    provider = _provider()
    page = FakePage(_detail_data(DESCRIPTION))

    raw_job = provider.normalize_job(
        {**JOB, **provider.extract_detail_for_job(page, dict(JOB))}
    )
    normalized = collector_postgres.normalize_job(raw_job)

    assert normalized["job_description"] == DESCRIPTION
    assert normalized["job_about"] == DESCRIPTION
