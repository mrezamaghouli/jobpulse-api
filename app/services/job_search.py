import math


def filter_jobs(
    jobs,
    title=None,
    location=None,
    remote=None,
    job_type=None,
    seniority=None,
    min_salary=None,
    max_salary=None,
    source=None
):
    results = jobs

    if title:
        results = [
            job for job in results
            if title.lower() in job["title"].lower()
        ]

    if location:
        results = [
            job for job in results
            if location.lower() in job["location"].lower()
        ]

    if remote is not None:
        results = [
            job for job in results
            if job["remote"] == remote
        ]

    if job_type:
        results = [
            job for job in results
            if job.get("job_type") and job_type.lower() in job["job_type"].lower()
        ]

    if seniority:
        results = [
            job for job in results
            if job.get("seniority") and seniority.lower() in job["seniority"].lower()
        ]

    if source:
        results = [
            job for job in results
            if job.get("source") and source.lower() in job["source"].lower()
        ]

    if min_salary is not None:
        results = [
            job for job in results
            if job.get("salary_max") is not None and job["salary_max"] >= min_salary
        ]

    if max_salary is not None:
        results = [
            job for job in results
            if job.get("salary_min") is not None and job["salary_min"] <= max_salary
        ]

    return results


def sort_jobs(jobs, sort_by="date_posted", sort_order="desc"):
    allowed_sort_fields = {
        "title",
        "company",
        "location",
        "salary_min",
        "salary_max",
        "date_posted"
    }

    if sort_by not in allowed_sort_fields:
        sort_by = "date_posted"

    reverse = sort_order == "desc"

    return sorted(
        jobs,
        key=lambda job: job.get(sort_by) if job.get(sort_by) is not None else "",
        reverse=reverse
    )


def paginate_jobs(jobs, page=1, limit=10):
    total_count = len(jobs)

    if total_count == 0:
        return {
            "items": [],
            "total_pages": 0
        }

    total_pages = math.ceil(total_count / limit)

    start_index = (page - 1) * limit
    end_index = start_index + limit

    paginated_items = jobs[start_index:end_index]

    return {
        "items": paginated_items,
        "total_pages": total_pages
    }