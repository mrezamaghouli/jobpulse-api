"""Phase 4B: static structural checks that frontend/index.html -- the
file actually served in production (see docker-compose.prod.yml's
frontend service and frontend/nginx.conf) -- conditionally surfaces the
existing `poster_profile_url`/`poster_name`/`poster_title` API fields.

No Node.js/browser toolchain exists in this repository (confirmed: no
`node`/`nodejs` binary present, no existing JS test harness), so these
are plain source-text checks in the same spirit as this repo's existing
static YAML/source audits (see tests/test_production_runtime_diagnostic.py)
-- not a full JS parse, and no new toolchain is introduced for this PR.

frontend/index.backup.html contains a prior, unused implementation of a
poster link; these tests only ever read frontend/index.html, proving the
CURRENT served file is self-sufficient and does not depend on the
backup file in any way.
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

FRONTEND_PATH = REPO_ROOT / "frontend" / "index.html"
BACKUP_PATH = REPO_ROOT / "frontend" / "index.backup.html"


def _source() -> str:
    return FRONTEND_PATH.read_text(encoding="utf-8")


def _render_details_block(source: str) -> str:
    """Isolate the renderDetails() function body -- the poster
    information is deliberately added to the detail panel, not the
    result-card list, per this feature's design."""
    match = re.search(
        r"function renderDetails\(job\) \{(.*?)\n    function selectJob\(",
        source,
        re.DOTALL,
    )
    assert match, "expected to find renderDetails() ending before selectJob()"
    return match.group(1)


def test_frontend_file_exists_and_is_the_served_file():
    assert FRONTEND_PATH.is_file()
    # This test's assertions are scoped to frontend/index.html only -- it
    # never reads index.backup.html for anything other than this
    # existence check demonstrating the two are independent files.
    assert BACKUP_PATH.is_file()


def test_poster_profile_url_is_conditionally_checked():
    block = _render_details_block(_source())
    assert "job.poster_profile_url ?" in block


def test_poster_name_and_title_are_conditionally_checked():
    block = _render_details_block(_source())
    assert "job.poster_name ?" in block
    assert "job.poster_title ?" in block


def test_poster_link_label_present():
    block = _render_details_block(_source())
    assert "Open Poster Profile" in block


def test_poster_link_uses_target_blank_and_safe_rel():
    block = _render_details_block(_source())
    poster_link_match = re.search(
        r'<a\s+class="link-btn"\s+href="\$\{escapeHtml\(job\.poster_profile_url\)\}"(.*?)>',
        block,
        re.DOTALL,
    )
    assert poster_link_match, "expected an <a> tag whose href is escapeHtml(job.poster_profile_url)"
    attrs = poster_link_match.group(1)
    assert 'target="_blank"' in attrs
    assert 'rel="noopener noreferrer"' in attrs


def test_poster_fields_use_escape_html_not_raw_interpolation():
    """Security: poster_name/poster_title/poster_profile_url must never
    be inserted into the HTML string unescaped."""
    block = _render_details_block(_source())

    for field in ("poster_name", "poster_title", "poster_profile_url"):
        # Every occurrence of the raw field reference must be wrapped in
        # escapeHtml(...) -- never interpolated bare as `${job.<field>}`.
        bare_pattern = re.compile(r"\$\{job\." + field + r"(?!\s*\?)")
        for match in bare_pattern.finditer(block):
            # The only bare (non-escaped) use allowed is inside the
            # truthiness check itself, e.g. `${job.poster_name ? ... : ''}`
            # -- already excluded by the negative lookahead above.
            raise AssertionError(f"found unescaped ${{job.{field}}} usage: {match.group(0)}")

        assert f"escapeHtml(job.{field})" in block


def test_no_literal_null_undefined_placeholder_for_poster_fields():
    """Never show undefined/null/None as user-facing text for poster
    fields -- they must be conditionally omitted, not defaulted to a
    literal placeholder string."""
    block = _render_details_block(_source())

    for field in ("poster_name", "poster_title"):
        # Guard against a future regression like
        # `${job.poster_name || 'None'}` / `|| 'unknown'` for these two
        # fields specifically (unlike e.g. work_mode/apply_type, which
        # legitimately show an 'unknown' fallback elsewhere in this file).
        forbidden = re.compile(
            r"job\." + field + r"\s*\|\|\s*['\"](unknown|none|null|undefined)['\"]",
            re.IGNORECASE,
        )
        assert not forbidden.search(block), field


def test_poster_block_only_renders_when_poster_profile_url_present():
    """Phase 4B hardening: a validated profile URL is the trust anchor for
    the whole poster block. poster_name/poster_title alone (with no
    profile URL) must never be enough to show a "Job Poster" block --
    the extractor itself never returns a name/title without also
    returning the validated URL they came from (see
    extract_poster_info_from_detail_page()), and the frontend must not
    make that guarantee load-bearing by rendering on a looser OR check."""
    block = _render_details_block(_source())
    assert "${job.poster_profile_url ? `" in block
    assert (
        "(job.poster_name || job.poster_title || job.poster_profile_url) ?"
        not in block
    )
