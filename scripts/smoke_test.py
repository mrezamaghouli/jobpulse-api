import json
import urllib.request
import urllib.error


API_BASE_URL = "http://127.0.0.1:8000"


def request_json(path):
    url = f"{API_BASE_URL}{path}"

    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            status_code = response.status
            data = json.loads(response.read().decode("utf-8"))

            return status_code, data

    except urllib.error.HTTPError as error:
        return error.code, {
            "error": error.reason
        }

    except Exception as error:
        return None, {
            "error": str(error)
        }


def check_endpoint(name, path, expected_status=200):
    status_code, data = request_json(path)

    if status_code == expected_status:
        print(f"✅ {name} passed")
        return True

    print(f"❌ {name} failed")
    print(f"   Path: {path}")
    print(f"   Expected status: {expected_status}")
    print(f"   Actual status: {status_code}")
    print(f"   Response: {data}")
    return False


def run_smoke_tests():
    print("Running JobPulse smoke tests...")
    print("-" * 50)

    checks = [
        check_endpoint("Health check", "/health"),
        check_endpoint("Stats endpoint", "/jobs/stats"),
        check_endpoint("Search endpoint", "/jobs/search?source=linkedin"),
        check_endpoint("Job details endpoint", "/jobs/1"),
    ]

    print("-" * 50)

    passed = sum(checks)
    total = len(checks)

    print(f"Passed: {passed}/{total}")

    if passed == total:
        print("🎉 All smoke tests passed.")
    else:
        print("⚠️ Some smoke tests failed.")


if __name__ == "__main__":
    run_smoke_tests()