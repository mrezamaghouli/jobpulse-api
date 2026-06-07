import json
import os
import sys
import urllib.error
import urllib.request


API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
API_KEY = os.getenv("API_KEY", "")


def make_request(path):
    url = f"{API_BASE_URL}{path}"

    headers = {
        "Accept": "application/json"
    }

    if API_KEY:
        headers["X-API-Key"] = API_KEY

    request = urllib.request.Request(
        url,
        headers=headers,
        method="GET"
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")

            if body:
                data = json.loads(body)
            else:
                data = {}

            return {
                "status_code": response.status,
                "data": data,
                "error": None
            }

    except urllib.error.HTTPError as error:
        try:
            body = error.read().decode("utf-8")
            data = json.loads(body) if body else {}
        except Exception:
            data = {}

        return {
            "status_code": error.code,
            "data": data,
            "error": str(error)
        }

    except Exception as error:
        return {
            "status_code": None,
            "data": {
                "error": str(error)
            },
            "error": str(error)
        }


def check_endpoint(name, path, expected_status=200):
    result = make_request(path)

    if result["status_code"] == expected_status:
        print(f"✅ {name} passed")
        return True, result["data"]

    print(f"❌ {name} failed")
    print(f"   Path: {path}")
    print(f"   Expected status: {expected_status}")
    print(f"   Actual status: {result['status_code']}")
    print(f"   Response: {result['data']}")
    return False, result["data"]


def check_health():
    ok, data = check_endpoint("Health check", "/health")

    if not ok:
        return False

    if data.get("status") != "ok":
        print("❌ Health response status is not ok")
        print(f"   Response: {data}")
        return False

    if data.get("database") != "connected":
        print("❌ Database is not connected")
        print(f"   Response: {data}")
        return False

    return True


def check_meta():
    ok, data = check_endpoint("Metadata endpoint", "/meta")

    if not ok:
        return False

    if not data.get("app_name"):
        print("❌ Metadata response missing app_name")
        print(f"   Response: {data}")
        return False

    if not data.get("version"):
        print("❌ Metadata response missing version")
        print(f"   Response: {data}")
        return False

    return True


def check_stats():
    ok, data = check_endpoint("Stats endpoint", "/jobs/stats")

    if not ok:
        return False

    required_keys = [
        "total_jobs",
        "linkedin_jobs",
        "active_linkedin_jobs",
        "remote_jobs",
        "total_companies",
        "total_locations"
    ]

    missing_keys = [
        key
        for key in required_keys
        if key not in data
    ]

    if missing_keys:
        print("❌ Stats response missing keys")
        print(f"   Missing: {missing_keys}")
        print(f"   Response: {data}")
        return False

    return True


def check_collector_latest():
    ok, data = check_endpoint("Latest collector run endpoint", "/collector-runs/latest")

    if not ok:
        return False

    if data.get("status") == "empty":
        print("⚠️ Latest collector run endpoint is empty")
        print("   Run: python -m scripts.linkedin_multi_collect")
        return True

    required_keys = [
        "id",
        "provider",
        "keywords",
        "location",
        "status",
        "started_at",
        "finished_at"
    ]

    missing_keys = [
        key
        for key in required_keys
        if key not in data
    ]

    if missing_keys:
        print("❌ Latest collector run response missing keys")
        print(f"   Missing: {missing_keys}")
        print(f"   Response: {data}")
        return False

    return True


def check_collector_recent():
    ok, data = check_endpoint(
        "Recent collector runs endpoint",
        "/collector-runs/recent?limit=5"
    )

    if not ok:
        return False

    if "results" not in data:
        print("❌ Recent collector runs response missing results")
        print(f"   Response: {data}")
        return False

    return True


def check_search():
    ok, data = check_endpoint(
        "Search endpoint",
        "/jobs/search?source=linkedin&page=1&limit=10"
    )

    if not ok:
        return False

    if "results" not in data:
        print("❌ Search response missing results")
        print(f"   Response: {data}")
        return False

    if "count" not in data:
        print("❌ Search response missing count")
        print(f"   Response: {data}")
        return False

    return True, data


def check_job_details(search_data):
    jobs = search_data.get("results", [])

    if not jobs:
        print("⚠️ Job details endpoint skipped")
        print("   No jobs found in search results. Run collector first.")
        return True

    first_job = jobs[0]
    job_id = first_job.get("id")

    if not job_id:
        print("❌ First job does not have an id")
        print(f"   Job: {first_job}")
        return False

    ok, data = check_endpoint("Job details endpoint", f"/jobs/{job_id}")

    if not ok:
        return False

    if data.get("id") != job_id:
        print("❌ Job details returned different id")
        print(f"   Expected: {job_id}")
        print(f"   Response: {data}")
        return False

    return True


def run_smoke_tests():
    print("Running JobPulse smoke tests...")
    print(f"API base URL: {API_BASE_URL}")
    print(f"API key: {'enabled' if API_KEY else 'disabled'}")
    print("-" * 50)

    health_ok = check_health()
    meta_ok = check_meta()
    stats_ok = check_stats()
    collector_latest_ok = check_collector_latest()
    collector_recent_ok = check_collector_recent()
    search_ok, search_data = check_search()

    job_details_ok = False

    if search_ok:
        job_details_ok = check_job_details(search_data)

    checks = [
        health_ok,
        meta_ok,
        stats_ok,
        collector_latest_ok,
        collector_recent_ok,
        search_ok,
        job_details_ok
    ]

    passed = sum(1 for check in checks if check)
    total = len(checks)

    print("-" * 50)
    print(f"Passed: {passed}/{total}")

    if passed == total:
        print("🎉 All smoke tests passed.")
        return 0

    print("⚠️ Some smoke tests failed.")
    return 1


if __name__ == "__main__":
    sys.exit(run_smoke_tests())