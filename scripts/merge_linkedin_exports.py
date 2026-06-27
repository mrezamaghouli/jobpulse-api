import csv
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path


def bad_title(title):
    title = (title or "").strip()
    if not title:
        return True
    if re.match(r"^\d+\s+.+\s+jobs\s+in\s+.+$", title, re.IGNORECASE):
        return True
    return False


def main():
    exports_dir = Path("exports")
    imports_dir = Path("imports")
    imports_dir.mkdir(exist_ok=True)

    files = sorted(exports_dir.glob("linkedin_export_*.json"))
    print("json files:", len(files))

    seen = set()
    rows = []

    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            print("skip bad json:", p, exc)
            continue

        for row in data:
            title = (row.get("title") or "").strip()
            job_id = (row.get("linkedin_job_id") or "").strip()
            job_url = (row.get("job_url") or row.get("raw_job_url") or "").strip()

            if bad_title(title):
                continue

            key = job_id or job_url
            if not key or key in seen:
                continue

            seen.add(key)

            rows.append({
                "linkedin_job_id": job_id,
                "title": title,
                "company": (row.get("company") or "").strip(),
                "location": (row.get("location") or "").strip(),
                "job_url": job_url,
                "raw_job_url": (row.get("raw_job_url") or "").strip(),
                "apply_url": (row.get("apply_url") or "").strip(),
                "job_description": (row.get("job_description") or "").strip(),
                "source": "linkedin",
            })

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    json_path = imports_dir / ("linkedin_jobs_merged_" + stamp + ".json")
    csv_path = imports_dir / ("linkedin_jobs_merged_" + stamp + ".csv")
    zip_path = imports_dir / ("linkedin_jobs_import_" + stamp + ".zip")
    latest_zip = imports_dir / "linkedin_jobs_import_latest.zip"

    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = [
        "linkedin_job_id",
        "title",
        "company",
        "location",
        "job_url",
        "raw_job_url",
        "apply_url",
        "job_description",
        "source",
    ]

    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(json_path, json_path.name)
        z.write(csv_path, csv_path.name)

    if latest_zip.exists():
        latest_zip.unlink()

    latest_zip.write_bytes(zip_path.read_bytes())

    print("merged unique jobs:", len(rows))
    print("json:", json_path)
    print("csv:", csv_path)
    print("zip:", zip_path)
    print("latest:", latest_zip)

    print("")
    print("first rows:")
    for row in rows[:20]:
        print("-", row["title"], "|", row["company"], "|", row["location"], "|", row["job_url"])


if __name__ == "__main__":
    main()
