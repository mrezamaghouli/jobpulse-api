import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
SEARCH_PLAN_FILE = BASE_DIR / "config" / "linkedin_search_plan.json"
OUTPUT_FILE = BASE_DIR / "config" / "linkedin_expanded_queries.json"


def load_search_plan():
    if not SEARCH_PLAN_FILE.exists():
        raise FileNotFoundError(f"Search plan file not found: {SEARCH_PLAN_FILE}")

    with SEARCH_PLAN_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def expand_linkedin_queries(search_plan: dict) -> list[dict]:
    enabled_categories = search_plan.get("enabled_categories", [])
    categories = search_plan.get("categories", {})
    locations = search_plan.get("locations", [""])
    work_modes = search_plan.get("work_modes", ["any"])

    lookback_days = int(search_plan.get("lookback_days", 60))
    max_pages_per_query = int(search_plan.get("max_pages_per_query", 3))
    max_jobs_per_query = int(search_plan.get("max_jobs_per_query", 30))

    queries = []

    for category_name in enabled_categories:
        keywords = categories.get(category_name, [])

        for keyword in keywords:
            for location in locations:
                for work_mode in work_modes:
                    queries.append(
                        {
                            "category": category_name,
                            "keywords": keyword,
                            "location": location,
                            "work_mode": work_mode,
                            "lookback_days": lookback_days,
                            "max_pages": max_pages_per_query,
                            "limit": max_jobs_per_query
                        }
                    )

    return queries


def save_expanded_queries(queries: list[dict]):
    OUTPUT_FILE.parent.mkdir(exist_ok=True)

    with OUTPUT_FILE.open("w", encoding="utf-8") as file:
        json.dump(queries, file, ensure_ascii=False, indent=2)


def main():
    search_plan = load_search_plan()
    queries = expand_linkedin_queries(search_plan)
    save_expanded_queries(queries)

    print(f"Search plan: {search_plan.get('name')}")
    print(f"Enabled categories: {', '.join(search_plan.get('enabled_categories', []))}")
    print(f"Generated queries: {len(queries)}")
    print(f"Output file: {OUTPUT_FILE}")

    print("\nPreview:")
    for index, query in enumerate(queries[:20], start=1):
        print(
            f"{index}. "
            f"{query['category']} | "
            f"{query['keywords']} | "
            f"{query['location'] or 'Worldwide'} | "
            f"{query['work_mode']} | "
            f"{query['lookback_days']} days"
        )

    if len(queries) > 20:
        print(f"... and {len(queries) - 20} more queries")


if __name__ == "__main__":
    main()