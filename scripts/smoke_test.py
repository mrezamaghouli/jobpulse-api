import json
import os
import urllib.request
import urllib.error


API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
API_KEY = os.getenv("API_KEY", "").strip()


def request_json(path):
    url = f"{API_BASE_URL}{path}"

    request = urllib.request.Request(url)

    if API_KEY:
        request.add_header("X-API-Key", API_KEY)

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status_code = response.status
            data = json.loads(response.read().decode("utf-8"))
            return status_code, data

    except urllib.error.HTTPError as error:
        try:
            data = json.loads(error.read().decode("utf-8"))
        except Exception:
            data = {"error": error.reason}

        return error.code, data

    except Exception as error:
        return None, {"error": str(error)}


def check_endpoint(name, path, expected_status=200):
    status_code, data = request_json(path)

    if status_code == expected_status:
        print(f"✅ {name} passed")
        return True, data

    print(f"❌ {name} failed")
    print(f"   Path: {path}")
    print(f"   Expected status: {expected_status}")
    print(f"   Actual status: {status_code}")
    print(f"   Response: {data}")
    return False, data


def run_smoke_tests():
    print("Running JobPulse smoke tests...")
    print(f"API base URL: {API_BASE_URL}")

    if API_KEY:
        print("API key: enabled")
    else:
        print("API key: disabled")

    print("-" * 50)

    health_ok, _ = check_endpoint("Health check", "/health")
    stats_ok, _ = check_endpoint("Stats endpoint", "/jobs/stats")

    search_ok, search_data = check_endpoint(
        "Search endpoint",
        "/jobs/search?source=linkedin&page=1&limit=10"
    )

    job_details_ok = False

    results = search_data.get("results", []) if isinstance(search_data, dict) else []

    if results:
        first_job_id = results[0].get("id")

        if first_job_id:
            job_details_ok, _ = check_endpoint(
                "Job details endpoint",
                f"/jobs/{first_job_id}"
            )
        else:
            print("❌ Job details endpoint failed")
            print("   Search result did not include a job id.")
    else:
        print("❌ Job details endpoint failed")
        print("   No jobs found in search results. Collector may not have inserted data.")

    print("-" * 50)

    checks = [
        health_ok,
        stats_ok,
        search_ok,
        job_details_ok
    ]

    passed = sum(checks)
    total = len(checks)

    print(f"Passed: {passed}/{total}")

    if passed == total:
        print("🎉 All smoke tests passed.")
        return

    print("⚠️ Some smoke tests failed.")
    raise SystemExit(1)


if __name__ == "__main__":
    run_smoke_tests()