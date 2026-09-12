"""Phase 4B: capture LinkedIn job poster/recruiter profile data.

Covers, without any network or real LinkedIn request:

  1. `normalize_linkedin_profile_url()` -- the standalone URL validation/
     normalization helper (scripts/providers/linkedin_browser_provider.py).
  2. `LinkedInBrowserProvider.extract_poster_info_from_detail_page()`'s
     PYTHON-side contract (post-validation, exception-swallowing, and
     the found/not-found -> all-None mapping), exercised via a fake
     `page.evaluate()` returning canned in-page-evaluation results. This
     does NOT exercise the actual extraction JavaScript (DOM traversal,
     heading matching, scope selection, profile-link selection, name/
     title extraction) -- see section 5 below for that.
  3. `LinkedInBrowserProvider.normalize_job()` and
     `scripts.collector_postgres.insert_job()` -- proving poster fields
     flow from a raw/enriched job through normalization and into the
     UPSERT's SQL parameters unchanged.
  4. Structural regression checks on the extraction JavaScript's source
     text -- these run in any environment (no browser required) and
     guard against reintroducing the specific false-positive patterns
     this phase removed (arbitrary ancestor-depth climbing, substring
     heading matching, page-global `/in/` discovery).
  5. ACTUAL behavioral DOM tests: `LinkedInBrowserProvider.
     extract_poster_info_from_detail_page()` invoked against a real
     Playwright Chromium page loaded via `page.set_content(...)` with
     synthetic, fully offline HTML (no external URL is ever requested).
     This DOES execute the real extraction JavaScript end to end. These
     tests require a Playwright Chromium executable to already be
     present locally (this suite never downloads one); if none is
     found, they are skipped rather than failed -- see
     `_dom_browser_or_skip()`.

Before this change, `normalize_job()` hardcoded `poster_name`/
`poster_title`/`poster_profile_url` to `None` -- the database column,
UPSERT statement, and public API model already existed and are
unaffected by this PR.
"""
import glob
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.collector_postgres as cp
from scripts.providers.linkedin_browser_provider import (
    LinkedInBrowserProvider,
    normalize_linkedin_profile_url,
)

PROVIDER_SOURCE_PATH = REPO_ROOT / "scripts" / "providers" / "linkedin_browser_provider.py"


# =====================================================================
# 1. normalize_linkedin_profile_url() -- URL validation/normalization
# =====================================================================

def test_canonical_absolute_profile_url_accepted():
    assert (
        normalize_linkedin_profile_url("https://www.linkedin.com/in/anna-example/")
        == "https://www.linkedin.com/in/anna-example/"
    )


def test_absolute_profile_url_without_trailing_slash_accepted():
    assert (
        normalize_linkedin_profile_url("https://www.linkedin.com/in/anna-example")
        == "https://www.linkedin.com/in/anna-example/"
    )


def test_relative_profile_url_normalized_to_absolute():
    assert (
        normalize_linkedin_profile_url("/in/anna-example/")
        == "https://www.linkedin.com/in/anna-example/"
    )


def test_query_and_fragment_are_stripped_without_altering_slug():
    assert (
        normalize_linkedin_profile_url(
            "https://www.linkedin.com/in/anna-example/?originalSubdomain=de#about"
        )
        == "https://www.linkedin.com/in/anna-example/"
    )


def test_relative_url_with_query_string_normalized():
    assert (
        normalize_linkedin_profile_url("/in/anna-example?trk=public_profile")
        == "https://www.linkedin.com/in/anna-example/"
    )


def test_company_url_rejected():
    assert normalize_linkedin_profile_url("https://www.linkedin.com/company/acme/") is None


def test_job_listing_url_rejected():
    assert (
        normalize_linkedin_profile_url("https://www.linkedin.com/jobs/view/123456/")
        is None
    )


def test_feed_url_rejected():
    assert normalize_linkedin_profile_url("https://www.linkedin.com/feed/") is None


def test_learning_url_rejected():
    assert normalize_linkedin_profile_url("https://www.linkedin.com/learning/some-course") is None


def test_school_url_rejected():
    assert normalize_linkedin_profile_url("https://www.linkedin.com/school/some-school/") is None


def test_external_domain_rejected():
    assert normalize_linkedin_profile_url("https://not-linkedin.example.com/in/anna/") is None


def test_plain_http_absolute_url_rejected():
    """Defense-in-depth hardening: an absolute profile URL must be HTTPS.
    A bare http:// candidate is rejected outright rather than silently
    upgraded -- this function only ever validates a value that was
    actually captured, never rewrites one it doesn't trust."""
    assert normalize_linkedin_profile_url("http://www.linkedin.com/in/anna-example/") is None


def test_lookalike_subdomain_rejected():
    assert normalize_linkedin_profile_url("https://www.linkedin.com.evil.example/in/anna/") is None


def test_profile_subpage_rejected_not_just_bare_profile():
    """Only the bare /in/<slug>/ profile link is accepted -- a deeper
    sub-page (e.g. recent-activity) is not the raw profile link this
    extractor is scoped to preserve."""
    assert (
        normalize_linkedin_profile_url(
            "https://www.linkedin.com/in/anna-example/recent-activity/"
        )
        is None
    )


def test_empty_value_returns_none():
    assert normalize_linkedin_profile_url("") is None
    assert normalize_linkedin_profile_url(None) is None
    assert normalize_linkedin_profile_url("   ") is None


def test_malformed_url_returns_none():
    assert normalize_linkedin_profile_url("not a url at all") is None
    assert normalize_linkedin_profile_url("ftp://www.linkedin.com/in/anna/") is None
    assert normalize_linkedin_profile_url(12345) is None


def test_never_infers_a_profile_from_a_name():
    """This helper only ever validates a URL that was actually present in
    extracted page data -- it has no code path that could construct a
    profile URL from a person's name."""
    assert normalize_linkedin_profile_url("Anna Müller") is None


# =====================================================================
# 2. extract_poster_info_from_detail_page() -- already-loaded-DOM only
# =====================================================================

class FakePage:
    def __init__(self, evaluate_result=None, raise_on_evaluate=False):
        self._evaluate_result = evaluate_result
        self._raise_on_evaluate = raise_on_evaluate
        self.evaluate_calls = 0

    def evaluate(self, script):
        self.evaluate_calls += 1

        if self._raise_on_evaluate:
            raise RuntimeError("simulated page.evaluate failure")

        return self._evaluate_result


def _provider():
    # __new__ (never __init__) avoids touching Playwright/Postgres --
    # matches the existing FakePage-based transport tests' construction
    # style (tests/test_linkedin_browser_provider_transport.py).
    return LinkedInBrowserProvider.__new__(LinkedInBrowserProvider)


def test_extraction_a_hiring_block_with_name_title_and_profile_url():
    provider = _provider()
    page = FakePage({
        "found": True,
        "poster_name": "Anna Müller",
        "poster_title": "Technical Recruiter",
        "poster_profile_url": "https://www.linkedin.com/in/anna-example/",
    })

    result = provider.extract_poster_info_from_detail_page(page=page)

    assert result == {
        "poster_name": "Anna Müller",
        "poster_title": "Technical Recruiter",
        "poster_profile_url": "https://www.linkedin.com/in/anna-example/",
    }
    assert page.evaluate_calls == 1


def test_extraction_b_profile_url_with_missing_title():
    provider = _provider()
    page = FakePage({
        "found": True,
        "poster_name": "Anna Müller",
        "poster_title": None,
        "poster_profile_url": "https://www.linkedin.com/in/anna-example/",
    })

    result = provider.extract_poster_info_from_detail_page(page=page)

    assert result["poster_name"] == "Anna Müller"
    assert result["poster_title"] is None
    assert result["poster_profile_url"] == "https://www.linkedin.com/in/anna-example/"


def test_extraction_c_poster_absent_entirely_returns_all_none():
    provider = _provider()
    page = FakePage({
        "found": False,
        "poster_name": None,
        "poster_title": None,
        "poster_profile_url": None,
    })

    result = provider.extract_poster_info_from_detail_page(page=page)

    assert result == {"poster_name": None, "poster_title": None, "poster_profile_url": None}


def test_extraction_d_ambiguous_or_unscoped_in_page_result_never_becomes_a_guess():
    """The real in-page evaluation is narrowly scoped to a single
    unambiguous hiring-team profile link and reports found=False whenever
    it cannot identify exactly one (e.g. only unrelated `/in/` links exist
    elsewhere on the page -- navbar, suggested people, other employees).
    That DOM-tree-walking logic itself requires a live browser to execute
    and is not re-implemented in this Python test suite; what IS tested
    here is the contract this wrapper must honor: an ambiguous/not-found
    result must never be upgraded into a fabricated poster value."""
    provider = _provider()
    page = FakePage({
        "found": False,
        "poster_name": None,
        "poster_title": None,
        "poster_profile_url": None,
    })

    result = provider.extract_poster_info_from_detail_page(page=page)

    assert result["poster_profile_url"] is None
    assert result["poster_name"] is None


def test_extraction_e_company_link_can_never_surface_as_poster_profile_url():
    """Defense in depth: even if the in-page evaluation ever mistakenly
    reported a non-profile link (e.g. a company page) as the poster URL,
    the Python-side normalize_linkedin_profile_url() validation
    independently rejects it before it reaches the caller. Trust-anchor
    invariant: since the URL is rejected, poster_name/poster_title must
    NEVER survive on their own -- all three fields come back None
    together."""
    provider = _provider()
    page = FakePage({
        "found": True,
        "poster_name": "Acme Corp",
        "poster_title": None,
        "poster_profile_url": "https://www.linkedin.com/company/acme/",
    })

    result = provider.extract_poster_info_from_detail_page(page=page)

    assert result == {"poster_name": None, "poster_title": None, "poster_profile_url": None}


def test_extraction_f_malformed_poster_url_clears_name_and_title_too():
    """Trust-anchor invariant: a malformed poster URL must clear
    poster_name/poster_title along with it, not just poster_profile_url.
    A name/title without a validated URL behind it is exactly the kind
    of orphaned, unverifiable claim this feature must never surface."""
    provider = _provider()
    page = FakePage({
        "found": True,
        "poster_name": "Someone",
        "poster_title": "Recruiter",
        "poster_profile_url": "not a url at all",
    })

    result = provider.extract_poster_info_from_detail_page(page=page)

    assert result == {"poster_name": None, "poster_title": None, "poster_profile_url": None}


def test_extraction_external_in_path_url_clears_all_poster_fields():
    """A non-LinkedIn domain reusing the /in/ path shape must not leave
    behind a name/title even though it superficially resembles a
    profile URL."""
    provider = _provider()
    page = FakePage({
        "found": True,
        "poster_name": "Fake Person",
        "poster_title": "Recruiter",
        "poster_profile_url": "https://evil.example/in/fake-person/",
    })

    result = provider.extract_poster_info_from_detail_page(page=page)

    assert result == {"poster_name": None, "poster_title": None, "poster_profile_url": None}


def test_extraction_plain_http_linkedin_url_clears_all_poster_fields():
    """A plain http:// (non-HTTPS) LinkedIn profile URL must be
    rejected, clearing name/title along with it."""
    provider = _provider()
    page = FakePage({
        "found": True,
        "poster_name": "Fake Person",
        "poster_title": "Recruiter",
        "poster_profile_url": "http://www.linkedin.com/in/fake-person/",
    })

    result = provider.extract_poster_info_from_detail_page(page=page)

    assert result == {"poster_name": None, "poster_title": None, "poster_profile_url": None}


def test_extraction_swallows_page_evaluate_exception():
    """No poster extraction error may ever fail collection of an
    otherwise-valid job."""
    provider = _provider()
    page = FakePage(raise_on_evaluate=True)

    result = provider.extract_poster_info_from_detail_page(page=page)

    assert result == {"poster_name": None, "poster_title": None, "poster_profile_url": None}


def test_extraction_handles_none_evaluate_result():
    provider = _provider()
    page = FakePage(evaluate_result=None)

    result = provider.extract_poster_info_from_detail_page(page=page)

    assert result == {"poster_name": None, "poster_title": None, "poster_profile_url": None}


# =====================================================================
# 3. normalize_job() -- provider-level regression
# =====================================================================

def test_normalize_job_preserves_valid_poster_fields():
    provider = _provider()
    raw_job = {
        "job_url": "https://www.linkedin.com/jobs/view/123456/",
        "linkedin_job_id": "123456",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Berlin, Germany",
        "poster_name": "Example Recruiter",
        "poster_title": "Talent Acquisition",
        "poster_profile_url": "https://www.linkedin.com/in/example-recruiter/",
    }

    normalized = provider.normalize_job(raw_job)

    assert normalized["poster_name"] == "Example Recruiter"
    assert normalized["poster_title"] == "Talent Acquisition"
    assert normalized["poster_profile_url"] == "https://www.linkedin.com/in/example-recruiter/"


def test_normalize_job_missing_poster_fields_remain_none():
    provider = _provider()
    raw_job = {
        "job_url": "https://www.linkedin.com/jobs/view/123456/",
        "linkedin_job_id": "123456",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Berlin, Germany",
    }

    normalized = provider.normalize_job(raw_job)

    assert normalized["poster_name"] is None
    assert normalized["poster_title"] is None
    assert normalized["poster_profile_url"] is None


def test_normalize_job_rejects_invalid_poster_url_and_clears_name_and_title():
    """Trust-anchor invariant, enforced independently inside
    normalize_job() as defense in depth: an invalid poster_profile_url
    must clear poster_name/poster_title along with it -- even though
    the job itself (title, job_url) must still be retained. Invalid
    poster metadata is dropped; the valid job is not."""
    provider = _provider()
    raw_job = {
        "job_url": "https://www.linkedin.com/jobs/view/123456/",
        "linkedin_job_id": "123456",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Berlin, Germany",
        "poster_name": "Someone",
        "poster_title": "Recruiter",
        "poster_profile_url": "https://www.linkedin.com/company/acme/",
    }

    normalized = provider.normalize_job(raw_job)

    assert normalized["poster_profile_url"] is None
    assert normalized["poster_name"] is None
    assert normalized["poster_title"] is None
    assert normalized["title"] == "Backend Engineer"
    assert normalized["job_url"] == "https://www.linkedin.com/jobs/view/123456/"


def test_normalize_job_plain_http_poster_url_clears_name_and_title_too():
    provider = _provider()
    raw_job = {
        "job_url": "https://www.linkedin.com/jobs/view/123456/",
        "linkedin_job_id": "123456",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Berlin, Germany",
        "poster_name": "Someone",
        "poster_title": "Recruiter",
        "poster_profile_url": "http://www.linkedin.com/in/someone/",
    }

    normalized = provider.normalize_job(raw_job)

    assert normalized["poster_profile_url"] is None
    assert normalized["poster_name"] is None
    assert normalized["poster_title"] is None
    assert normalized["title"] == "Backend Engineer"


# =====================================================================
# 4. Collector flow-through -- provider -> collector_postgres.insert_job()
# =====================================================================

class FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return (True,)

    def close(self):
        pass


def test_collector_insert_job_passes_through_provider_poster_fields():
    """Protects the new provider -> collector vertical slice only -- the
    INSERT/UPSERT SQL and its poster_* columns already existed and are
    covered elsewhere (tests/test_upsert_returning_integration.py,
    real Postgres); this test does not duplicate that."""
    cursor = FakeCursor()
    raw_job = {
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Berlin, Germany",
        "job_url": "https://www.linkedin.com/jobs/view/123456/",
        "linkedin_job_id": "123456",
        "source": "LinkedIn",
        "poster_name": "Example Recruiter",
        "poster_title": "Talent Acquisition",
        "poster_profile_url": "https://www.linkedin.com/in/example-recruiter/",
    }

    normalized = cp.normalize_job(raw_job)
    outcome = cp.insert_job(cursor, normalized)

    assert outcome == cp.ROW_OUTCOME_INSERTED
    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    assert "poster_name" in sql
    assert "poster_title" in sql
    assert "poster_profile_url" in sql
    assert params["poster_name"] == "Example Recruiter"
    assert params["poster_title"] == "Talent Acquisition"
    assert params["poster_profile_url"] == "https://www.linkedin.com/in/example-recruiter/"


def test_collector_insert_job_with_absent_poster_fields_still_succeeds():
    """Poster enrichment is optional metadata, not a collection
    requirement -- a job with no hiring-contact information must still
    insert successfully."""
    cursor = FakeCursor()
    raw_job = {
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Berlin, Germany",
        "job_url": "https://www.linkedin.com/jobs/view/654321/",
        "linkedin_job_id": "654321",
        "source": "LinkedIn",
    }

    normalized = cp.normalize_job(raw_job)
    outcome = cp.insert_job(cursor, normalized)

    assert outcome == cp.ROW_OUTCOME_INSERTED
    _, params = cursor.executed[0]
    assert params["poster_name"] is None
    assert params["poster_title"] is None
    assert params["poster_profile_url"] is None


# =====================================================================
# 5. Structural regression checks on the extraction JavaScript source
#
# These run in every environment, no browser required. They protect
# the specific false-positive patterns this hardening phase removed,
# so a future edit can't silently reintroduce them even where the
# behavioral Playwright tests below happen to be skipped.
# =====================================================================

def _provider_source() -> str:
    return PROVIDER_SOURCE_PATH.read_text(encoding="utf-8")


def _poster_extraction_js(source: str) -> str:
    match = re.search(
        r'def extract_poster_info_from_detail_page\(self, page\).*?'
        r'page\.evaluate\(\s*"""(.*?)"""\s*\)',
        source,
        re.DOTALL,
    )
    assert match, (
        "expected to find extract_poster_info_from_detail_page()'s "
        "page.evaluate(...) JavaScript body"
    )
    return match.group(1)


def test_structural_no_arbitrary_ancestor_depth_loop():
    """The original review blocker: a `for (depth < 6)` loop walking
    `node = node.parentElement` upward from the heading, stopping at the
    first ancestor containing ANY `/in/` link. Must never come back."""
    js = _poster_extraction_js(_provider_source())
    assert "depth < 6" not in js
    assert "node = node.parentElement" not in js


def test_structural_heading_match_is_exact_not_substring():
    js = _poster_extraction_js(_provider_source())
    assert ".includes(label)" not in js
    assert "text.includes(" not in js
    assert "HIRING_TEAM_LABELS.has(" in js


def test_structural_scope_is_a_fixed_bounded_candidate_set():
    js = _poster_extraction_js(_provider_source())
    assert "getLocalScopeCandidates" in js
    # No unbounded/looping ancestor climb of any kind.
    assert "while (" not in js
    assert "for (let depth" not in js


def test_structural_no_page_global_profile_link_discovery():
    js = _poster_extraction_js(_provider_source())
    assert 'document.querySelectorAll(\'a[href*="/in/"]\')' not in js
    assert "document.body" not in js
    assert "document.querySelector('main'" not in js


def test_structural_ambiguous_results_fail_closed():
    js = _poster_extraction_js(_provider_source())
    assert "distinctKeys.size !== 1" in js


def test_structural_name_not_derived_by_climbing_arbitrary_card_lines():
    """The removed pattern climbed a fixed number of parents from the
    profile link and split innerText into arbitrary "lines[0]"/
    "lines[1]" guesses. The replacement must read text off the anchor
    itself instead."""
    js = _poster_extraction_js(_provider_source())
    assert "lines[0]" not in js
    assert "lines[1]" not in js
    assert "posterAnchor.innerText" in js


def test_structural_absolute_candidates_require_https_and_linkedin_host():
    """Final trust-anchor fix: the JS pre-filter must reject an
    obviously invalid absolute profile URL (wrong scheme, non-LinkedIn
    host) before it can ever become a candidate -- not rely solely on
    the later Python-side re-validation. This is a pure source-text
    check so it runs even where no local browser is available to
    exercise the real behavior (see test_case_* below for that)."""
    js = _poster_extraction_js(_provider_source())
    assert "parsed.protocol !== 'https:'" in js
    assert "LINKEDIN_HOSTS.has(parsed.hostname.toLowerCase())" in js
    assert "LINKEDIN_HOSTS = new Set(['linkedin.com', 'www.linkedin.com'])" in js


# =====================================================================
# 6. Offline behavioral DOM tests (real Playwright Chromium, no network)
#
# These execute the ACTUAL extraction JavaScript inside a real browser
# page loaded via page.set_content(...) with synthetic, fully local
# HTML -- no external URL is ever requested. They require a Playwright
# Chromium executable to already exist locally; this suite never
# downloads one. If none is found, these tests are SKIPPED (not
# failed) and DOM_BEHAVIORAL_TEST_BROWSER_AVAILABLE=no should be
# reported by whatever ran this suite.
# =====================================================================

def _find_cached_chromium_executable() -> str | None:
    pattern = str(Path.home() / ".cache" / "ms-playwright" / "chromium-*" / "chrome-linux64" / "chrome")
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


@pytest.fixture(scope="module")
def dom_browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright package not installed")

    with sync_playwright() as playwright:
        browser = None

        try:
            browser = playwright.chromium.launch()
        except Exception:
            executable_path = _find_cached_chromium_executable()

            if executable_path:
                try:
                    browser = playwright.chromium.launch(executable_path=executable_path)
                except Exception:
                    browser = None

        if browser is None:
            pytest.skip(
                "No local Playwright Chromium executable available and this "
                "suite never downloads one -- "
                "DOM_BEHAVIORAL_TEST_BROWSER_AVAILABLE=no"
            )

        yield browser

        browser.close()


@pytest.fixture()
def dom_page(dom_browser):
    page = dom_browser.new_page()
    yield page
    page.close()


def _extract_via_real_dom(dom_page, html: str) -> dict:
    dom_page.set_content(html)
    provider = _provider()
    return provider.extract_poster_info_from_detail_page(page=dom_page)


def test_case_a_exact_heading_with_one_local_profile_link_is_extracted(dom_page):
    html = """
        <div>
          <h3>Meet the hiring team</h3>
          <div class="hiring-member">
            <a href="/in/jane-doe/">Jane Doe</a>
            <div>Technical Recruiter</div>
          </div>
        </div>
    """

    result = _extract_via_real_dom(dom_page, html)

    assert result["poster_profile_url"] == "https://www.linkedin.com/in/jane-doe/"
    assert result["poster_name"] == "Jane Doe"
    assert result["poster_title"] == "Technical Recruiter"


def test_case_b_broader_ancestor_unrelated_profile_never_inherited(dom_page):
    """The critical regression test for the original PR bug: the exact
    hiring-team heading's local structure has NO profile link, but a
    broader/further-away ancestor of the page happens to contain one
    unrelated `/in/` link (e.g. mentioned in the job description). The
    old 6-level ancestor climb would have found and misattributed it;
    the hardened extractor must never look that far and must return
    all-None instead."""
    html = """
        <div class="page">
          <div class="job-detail">
            <div class="hiring-section">
              <h3>Meet the hiring team</h3>
              <div class="hiring-body">No profile listed here.</div>
            </div>
          </div>
          <div class="description-mentions">
            <p>Also feel free to reach out to
              <a href="/in/unrelated-person/">Someone Else</a>
              about referrals.
            </p>
          </div>
        </div>
    """

    result = _extract_via_real_dom(dom_page, html)

    assert result == {"poster_name": None, "poster_title": None, "poster_profile_url": None}


def test_case_c_navbar_profile_ignored_local_recruiter_selected(dom_page):
    html = """
        <nav>
          <a href="/in/me/">My Profile</a>
        </nav>
        <div class="job-detail">
          <div class="hiring-section">
            <h3>Hiring team</h3>
            <div class="hiring-member">
              <a href="/in/rick-recruiter/">Rick Recruiter</a>
            </div>
          </div>
        </div>
    """

    result = _extract_via_real_dom(dom_page, html)

    assert result["poster_profile_url"] == "https://www.linkedin.com/in/rick-recruiter/"
    assert result["poster_name"] == "Rick Recruiter"


def test_case_d_two_distinct_profiles_in_scope_returns_all_none(dom_page):
    html = """
        <div class="hiring-section">
          <h3>Meet the hiring team</h3>
          <div class="hiring-member"><a href="/in/jane-doe/">Jane Doe</a></div>
          <div class="hiring-member"><a href="/in/john-smith/">John Smith</a></div>
        </div>
    """

    result = _extract_via_real_dom(dom_page, html)

    assert result == {"poster_name": None, "poster_title": None, "poster_profile_url": None}


def test_case_e_unrelated_profile_with_no_hiring_heading_returns_all_none(dom_page):
    html = """
        <div class="description">
          <p>Great team led by <a href="/in/someone/">Someone</a>.</p>
        </div>
    """

    result = _extract_via_real_dom(dom_page, html)

    assert result == {"poster_name": None, "poster_title": None, "poster_profile_url": None}


def test_case_f_heading_like_text_does_not_qualify_as_exact_match(dom_page):
    """"How our hiring team works" contains the substring "hiring team"
    but must NOT match -- only the exact normalized labels do."""
    html = """
        <div class="hiring-section">
          <h3>How our hiring team works</h3>
          <div class="hiring-member"><a href="/in/jane-doe/">Jane Doe</a></div>
        </div>
    """

    result = _extract_via_real_dom(dom_page, html)

    assert result == {"poster_name": None, "poster_title": None, "poster_profile_url": None}


def test_case_g_external_lookalike_absolute_url_returns_all_none(dom_page):
    """Trust-anchor adversarial case: an exact hiring-team heading whose
    ONLY candidate is an absolute URL on a non-LinkedIn domain that
    merely reuses the /in/<slug> path shape. Must never be treated as a
    valid candidate -- the JS pre-filter's hostname allowlist rejects it
    before scope selection can even consider it a "found" candidate, so
    the local scope ends up with zero candidates. No network request is
    made; this is purely a string attribute on a static, offline page."""
    html = """
        <div class="hiring-section">
          <h3>Meet the hiring team</h3>
          <div class="hiring-member">
            <a href="https://evil.example/in/fake-person/">Fake Person</a>
          </div>
        </div>
    """

    result = _extract_via_real_dom(dom_page, html)

    assert result == {"poster_name": None, "poster_title": None, "poster_profile_url": None}


def test_case_h_plain_http_linkedin_url_returns_all_none(dom_page):
    """Trust-anchor adversarial case: a plain http:// (non-HTTPS)
    LinkedIn profile URL must never be accepted as a candidate, even
    though the host and path shape are otherwise correct."""
    html = """
        <div class="hiring-section">
          <h3>Meet the hiring team</h3>
          <div class="hiring-member">
            <a href="http://www.linkedin.com/in/fake-person/">Fake Person</a>
          </div>
        </div>
    """

    result = _extract_via_real_dom(dom_page, html)

    assert result == {"poster_name": None, "poster_title": None, "poster_profile_url": None}


def test_case_i_valid_https_linkedin_url_still_preserved(dom_page):
    """Control case proving the hardened pre-filter is not
    over-aggressive: a genuinely valid HTTPS www.linkedin.com/in/<slug>
    profile link in the same shape as cases G/H must still be
    extracted normally."""
    html = """
        <div class="hiring-section">
          <h3>Meet the hiring team</h3>
          <div class="hiring-member">
            <a href="https://www.linkedin.com/in/real-person/">Real Person</a>
          </div>
        </div>
    """

    result = _extract_via_real_dom(dom_page, html)

    assert result["poster_profile_url"] == "https://www.linkedin.com/in/real-person/"
    assert result["poster_name"] == "Real Person"
