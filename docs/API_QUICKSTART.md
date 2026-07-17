# JobPulse API Quickstart

## Base URL

```text
http://35.192.251.190/api
```

---

## Authentication

Public endpoints require an API key.

Send the key using this header:

```http
X-API-Key:گانه رو هم کامل بساز:

````bash
cd /opt/jobpulse

mkdir -p docs

cat > docs/API_QUICKSTART.md <<'EOF'
# JobPulse API Quickstart

## Base URL

```text
http://35.192.251.190/api
```

---

## Authentication

Public endpoints require an API key.

Send the key using this header:

```http
X-API-Key: YOUR_API_KEY
```

---

## Health

```bash
curl -fsS http://35.192.251.190/api/health
```


---

## Metadata Endpoints

These endpoints are public and do not require an API key.

### Version

```bash
curl -fsS http://35.192.251.190/api/version | python3 -m json.tool
```

Example response fields:

```text
name
status
environment
version
image
server_time_utc
docs
```

### Docs Info

```bash
curl -fsS http://35.192.251.190/api/docs-info | python3 -m json.tool
```

Example response fields:

```text
name
base_url
authentication
endpoints
sort_fields
docs_files
```

---

## Search Jobs

```bash
curl -sS \
  -H "X-API-Key: YOUR_API_KEY" \
  "http://35.192.251.190/api/jobs/search?query=data%20analyst&location=Germany&limit=10"
```

---

## Search Parameters

| Parameter | Example | Notes |
|---|---|---|
| `query` | `data analyst` | Keyword search |
| `location` | `Germany` | Location filter |
| `company` | `ALTEN Germany` | Company filter |
| `work_mode` | `remote` | Remote, hybrid, onsite when available |
| `apply_type` | `external` | Apply type when available |
| `posted_within_days` | `7` | Freshness filter |
| `has_apply_url` | `true` | Only jobs with apply URL |
| `has_logo` | `true` | Only jobs with company logo |
| `sort_by` | `relevance` | Sort field |
| `sort_order` | `desc` | `asc` or `desc` |
| `page` | `1` | Pagination |
| `limit` | `10` | Page size |

---

## Sort Fields

```text
relevance
last_seen_at
first_seen_at
date_posted_at
```

---

## Example: Relevance Search

```bash
curl -sS \
  -H "X-API-Key: YOUR_API_KEY" \
  "http://35.192.251.190/api/jobs/search?query=mechanical%20engineer&location=Canada&sort_by=relevance&sort_order=desc&limit=10"
```

---

## Example: Fresh Remote Jobs

```bash
curl -sS \
  -H "X-API-Key: YOUR_API_KEY" \
  "http://35.192.251.190/api/jobs/search?query=data%20analyst&work_mode=remote&posted_within_days=7&limit=10"
```

---

## Response Shape

```json
{
  "results": [],
  "count": 0,
  "page": 1,
  "limit": 10,
  "total_pages": 0,
  "search_mode": "hybrid",
  "sort_mode": "relevance",
  "sort_order": "desc",
  "filters_applied": {},
  "explain_enabled": true,
  "metadata": {}
}
```

---

## Common Result Fields

```text
id
title
company
location
job_url
apply_url
apply_type
work_mode
remote
date_posted_at
first_seen_at
last_seen_at
company_logo_url
search_score
quality_score
source
```

---

## Rate Limit

The production API is rate limited.

Responses may include rate limit headers such as:

```text
x-ratelimit-limit
x-ratelimit-remaining
```

---

## Notes

Do not expose production API keys publicly.

Use server-side environment variables or private client storage for API keys.
