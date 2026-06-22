import math
import os
import re
from datetime import datetime, timezone
from typing import Any


STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "job", "jobs", "of", "on", "or", "role", "the", "to", "with",
    "work", "hiring", "remote"
}

SYNONYMS = {
    "backend": ["backend", "back-end", "server-side", "api", "microservice", "microservices"],
    "frontend": ["frontend", "front-end", "react", "web developer", "ui engineer", "javascript", "typescript"],
    "fullstack": ["fullstack", "full-stack", "full stack"],
    "python": ["python", "django", "fastapi", "flask"],
    "node": ["node", "node.js", "nestjs", "express"],
    "devops": ["devops", "sre", "site reliability", "kubernetes", "docker", "terraform", "ci/cd"],
    "data": ["data engineer", "data engineering", "analytics engineer", "etl", "warehouse"],
    "ai": ["ai", "machine learning", "ml", "llm", "artificial intelligence"],
    "ux": ["ux", "user experience", "product designer", "ui/ux", "ux designer"],
    "ui": ["ui", "user interface", "ui engineer", "frontend"],
    "designer": ["designer", "design", "product designer", "ux designer", "ui designer"],
    "qa": ["qa", "quality assurance", "test automation", "automation engineer"],
    "product": ["product manager", "product owner", "technical product manager"],
    "remote": ["remote", "work from anywhere", "work remotely"],
}


NEGATIVE_ROLE_GROUPS = {
    "ux": ["backend engineer", "data engineer", "devops", "qa automation", "security engineer"],
    "designer": ["backend engineer", "data engineer", "devops", "qa automation", "security engineer"],
    "backend": ["ux designer", "product designer", "frontend designer", "ui designer"],
    "devops": ["ux designer", "product designer", "frontend developer"],
    "qa": ["ux designer", "product designer"],
}


def normalize_text(value: Any) -> str:
    value = "" if value is None else str(value)
    value = value.lower()
    value = value.replace("–", "-").replace("—", "-")
    value = re.sub(r"[^a-z0-9+#./ -]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def query_terms(query: str) -> list[str]:
    text = normalize_text(query)
    terms = []
    for part in text.split():
        if len(part) < 2:
            continue
        if part in STOPWORDS:
            continue
        terms.append(part)
    return terms


def expanded_terms(query: str) -> list[str]:
    base_terms = query_terms(query)
    expanded = set(base_terms)

    for term in base_terms:
        for synonym in SYNONYMS.get(term, []):
            expanded.add(normalize_text(synonym))

    return sorted(expanded, key=len, reverse=True)


def contains_any(text: str, values: list[str]) -> bool:
    return any(value and value in text for value in values)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def parse_datetime(value: Any):
    if not value:
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except Exception:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def recency_boost(job: dict) -> float:
    dt = parse_datetime(job.get("date_posted_at")) or parse_datetime(job.get("last_seen_at")) or parse_datetime(job.get("first_seen_at"))

    if not dt:
        return 0.0

    age_days = max((datetime.now(timezone.utc) - dt).total_seconds() / 86400, 0)

    if age_days <= 1:
        return 0.10
    if age_days <= 3:
        return 0.07
    if age_days <= 7:
        return 0.05
    if age_days <= 14:
        return 0.03
    return 0.0


def remote_boost(query: str, job: dict) -> float:
    q = normalize_text(query)
    if "remote" not in q:
        return 0.0

    work_mode = normalize_text(job.get("work_mode"))
    location = normalize_text(job.get("location"))
    description = normalize_text(job.get("job_description") or job.get("job_about"))

    if job.get("remote") is True:
        return 0.10

    if work_mode == "remote":
        return 0.10

    if "remote" in location:
        return 0.08

    if "work from anywhere" in description or "location remote" in description:
        return 0.06

    return -0.03


def negative_role_penalty(query: str, title_text: str) -> float:
    q_terms = query_terms(query)
    penalty = 0.0

    for q_term in q_terms:
        for bad_phrase in NEGATIVE_ROLE_GROUPS.get(q_term, []):
            if bad_phrase in title_text:
                penalty -= 0.18

    return penalty


def lexical_score(query: str, job: dict) -> tuple[float, list[str]]:
    q = normalize_text(query)
    terms = query_terms(query)
    ex_terms = expanded_terms(query)

    title = normalize_text(job.get("title"))
    company = normalize_text(job.get("company"))
    location = normalize_text(job.get("location"))
    work_mode = normalize_text(job.get("work_mode"))
    description = normalize_text(job.get("job_description") or job.get("job_about"))

    haystack_short = f"{title} {company} {location} {work_mode}"
    haystack_long = f"{haystack_short} {description[:4000]}"

    score = 0.0
    reasons = []

    if q and q in title:
        score += 0.45
        reasons.append("exact_query_in_title")
    elif q and q in haystack_short:
        score += 0.30
        reasons.append("exact_query_in_short_fields")
    elif q and q in haystack_long:
        score += 0.18
        reasons.append("exact_query_in_description")

    matched_title_terms = 0
    matched_any_terms = 0

    for term in terms:
        term_variants = [term] + SYNONYMS.get(term, [])

        if contains_any(title, term_variants):
            matched_title_terms += 1
            score += 0.16
        elif contains_any(haystack_short, term_variants):
            score += 0.09
        elif contains_any(description, term_variants):
            score += 0.045

        if contains_any(haystack_long, term_variants):
            matched_any_terms += 1

    if terms:
        title_ratio = matched_title_terms / len(terms)
        any_ratio = matched_any_terms / len(terms)

        score += min(title_ratio * 0.25, 0.25)
        score += min(any_ratio * 0.18, 0.18)

        if any_ratio >= 0.75:
            reasons.append("most_query_terms_matched")
        elif any_ratio < 0.35:
            score -= 0.18
            reasons.append("weak_query_term_match")

    # Extra synonym phrase matching, capped
    synonym_hits = 0
    for term in ex_terms:
        if term in title:
            synonym_hits += 1
        elif term in haystack_short:
            synonym_hits += 0.5

    score += min(synonym_hits * 0.04, 0.16)

    rboost = remote_boost(query, job)
    score += rboost
    if rboost > 0:
        reasons.append("remote_match")

    recent = recency_boost(job)
    score += recent
    if recent > 0:
        reasons.append("recent_job")

    penalty = negative_role_penalty(query, title)
    score += penalty
    if penalty < 0:
        reasons.append("negative_role_penalty")

    return score, reasons


def quality_score(query: str, job: dict) -> tuple[float, list[str]]:
    base_search_score = safe_float(job.get("search_score"), 0.0)
    lexical, reasons = lexical_score(query, job)

    # Keep the existing semantic score, but make lexical/title matching more important.
    combined = (base_search_score * 0.55) + (lexical * 0.45)

    # Smooth into 0..1-ish range.
    combined = max(0.0, min(round(combined, 6), 1.0))

    return combined, reasons


def rerank_jobs(result, query: str | None):
    if not query:
        return result

    min_quality_score = safe_float(os.getenv("JOB_SEARCH_MIN_QUALITY_SCORE"), 0.18)
    should_filter = os.getenv("JOB_SEARCH_FILTER_LOW_QUALITY", "true").lower() in {"1", "true", "yes", "on"}

    if isinstance(result, dict) and isinstance(result.get("results"), list):
        jobs = result["results"]
        result = dict(result)
    elif isinstance(result, list):
        jobs = result
    else:
        return result

    reranked = []

    for job in jobs:
        if not isinstance(job, dict):
            reranked.append(job)
            continue

        score, reasons = quality_score(query, job)
        job = dict(job)
        job["quality_score"] = score
        job["quality_reasons"] = reasons

        if not should_filter or score >= min_quality_score:
            reranked.append(job)

    reranked.sort(
        key=lambda item: (
            safe_float(item.get("quality_score"), 0.0),
            safe_float(item.get("search_score"), 0.0),
            str(item.get("last_seen_at") or ""),
        ),
        reverse=True,
    )

    if isinstance(result, dict):
        result["results"] = reranked
        result["count"] = len(reranked)
        return result

    return reranked
