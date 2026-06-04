# Local Development Guide

This guide explains how to run JobPulse locally when Docker image builds are not available or when developing faster without rebuilding containers.

In this mode:

```text
PostgreSQL → Docker
Adminer    → Docker
FastAPI    → Local Python venv
Frontend   → Local Python HTTP server
```

This mode is useful when Docker cannot access PyPI or when you want to develop the backend faster without rebuilding Docker images.

---

## 1. Start PostgreSQL and Adminer

From the project root:

```powershell
cd H:\linkedin-api
docker compose up -d db adminer
```

Check services:

```powershell
docker compose ps
```

Expected services:

```text
jobpulse-postgres
jobpulse-adminer
```

Adminer URL:

```text
http://127.0.0.1:8081
```

---

## 2. Activate Python Virtual Environment

From the project root:

```powershell
cd H:\linkedin-api
venv\Scripts\activate
```

If dependencies are missing, install them:

```powershell
pip install -r requirements.txt
```

---

## 3. Set Local Environment Variables

When the API runs locally, it should connect to PostgreSQL through `localhost`, not `db`.

Run these commands in the same PowerShell terminal where you will start the API:

```powershell
$env:POSTGRES_HOST="localhost"
$env:POSTGRES_PORT="5432"
$env:POSTGRES_DB="jobpulse"
$env:POSTGRES_USER="jobpulse_user"
$env:POSTGRES_PASSWORD="jobpulse_password"
$env:JOB_PROVIDER="json"
$env:API_KEY=""
$env:RATE_LIMIT_ENABLED="false"
$env:CORS_ALLOWED_ORIGINS="http://127.0.0.1:5500,http://localhost:5500"
```

Optional rate limit values:

```powershell
$env:RATE_LIMIT_MAX_REQUESTS="60"
$env:RATE_LIMIT_WINDOW_SECONDS="60"
```

---

## 4. Start the API Locally

Run this from the project root:

```powershell
python -m uvicorn app.main:app --reload
```

API URL:

```text
http://127.0.0.1:8000
```

API docs:

```text
http://127.0.0.1:8000/docs
```

Health check:

```text
http://127.0.0.1:8000/health
```

Expected health response:

```json
{
  "status": "ok",
  "api": "running",
  "database": "connected",
  "database_type": "PostgreSQL"
}
```

---

## 5. Run the Collector Locally

Open another PowerShell terminal.

From the project root:

```powershell
cd H:\linkedin-api
venv\Scripts\activate
```

Set the same database-related environment variables:

```powershell
$env:POSTGRES_HOST="localhost"
$env:POSTGRES_PORT="5432"
$env:POSTGRES_DB="jobpulse"
$env:POSTGRES_USER="jobpulse_user"
$env:POSTGRES_PASSWORD="jobpulse_password"
$env:JOB_PROVIDER="json"
```

Run the collector:

```powershell
python -m scripts.collector_postgres
```

Expected output:

```text
LinkedIn PostgreSQL collector finished successfully.
Provider: JsonJobProvider
```

If the job already exists, duplicate jobs will be skipped.

---

## 6. Start the Frontend Locally

Open another PowerShell terminal.

Run:

```powershell
cd H:\linkedin-api\frontend
python -m http.server 5500
```

Frontend URL:

```text
http://127.0.0.1:5500
```

The frontend reads the API URL from:

```text
frontend/config.js
```

Local config should point to:

```js
window.JOBPULSE_CONFIG = {
  API_BASE_URL: "http://127.0.0.1:8000"
};
```

---

## 7. Run Smoke Tests

Make sure the API is already running.

Then open another PowerShell terminal:

```powershell
cd H:\linkedin-api
venv\Scripts\activate
python scripts/smoke_test.py
```

Expected result:

```text
Running JobPulse smoke tests...
API base URL: http://127.0.0.1:8000
API key: disabled
--------------------------------------------------
✅ Health check passed
✅ Stats endpoint passed
✅ Search endpoint passed
✅ Job details endpoint passed
--------------------------------------------------
Passed: 4/4
🎉 All smoke tests passed.
```

If the job details test fails, run the collector first:

```powershell
python -m scripts.collector_postgres
```

Then run the smoke test again:

```powershell
python scripts/smoke_test.py
```

---

## 8. Run Unit Tests

Unit tests do not require the full Docker stack.

From the project root:

```powershell
pytest
```

Expected result:

```text
passed
```

---

## 9. Local API Key Testing

By default, local development keeps API key authentication disabled:

```powershell
$env:API_KEY=""
```

To test API key authentication locally:

```powershell
$env:API_KEY="test123"
python -m uvicorn app.main:app --reload
```

Then call a protected endpoint without a key:

```powershell
curl.exe -i "http://127.0.0.1:8000/jobs/search?source=linkedin"
```

Expected response:

```text
401 Unauthorized
```

Call it with the key:

```powershell
curl.exe -i -H "X-API-Key: test123" "http://127.0.0.1:8000/jobs/search?source=linkedin"
```

Expected response:

```text
200 OK
```

To clear the API key from the current PowerShell session:

```powershell
Remove-Item Env:\API_KEY
```

---

## 10. Local Rate Limit Testing

By default, rate limiting should be disabled:

```powershell
$env:RATE_LIMIT_ENABLED="false"
```

To test rate limiting:

```powershell
$env:RATE_LIMIT_ENABLED="true"
$env:RATE_LIMIT_MAX_REQUESTS="3"
$env:RATE_LIMIT_WINDOW_SECONDS="60"
```

Start the API and call this several times:

```powershell
curl.exe -i "http://127.0.0.1:8000/jobs/search?source=linkedin"
```

After the limit is exceeded, the API should return:

```text
429 Too Many Requests
```

Response:

```json
{
  "detail": "Rate limit exceeded"
}
```

To disable rate limiting again:

```powershell
$env:RATE_LIMIT_ENABLED="false"
```

---

## 11. Stop Local Services

To stop the local API, press:

```text
Ctrl + C
```

To stop the frontend server, press:

```text
Ctrl + C
```

To stop PostgreSQL and Adminer:

```powershell
docker compose down
```

---

## When to Use Local Development Mode

Use this mode when:

* Docker cannot access PyPI
* Docker image build is slow
* You are changing Python code frequently
* You want faster debugging
* You want to run only PostgreSQL in Docker
* You want to avoid rebuilding the API container repeatedly

For final validation, GitHub Actions still builds and tests the Docker-based setup.
