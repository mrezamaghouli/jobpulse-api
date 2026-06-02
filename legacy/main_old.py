from fastapi import FastAPI, Query

app = FastAPI(title="JobPulse API")

jobs = [
    {
        "id": 1,
        "title": "Data Analyst",
        "company": "Example GmbH",
        "location": "Berlin, Germany",
        "remote": True,
        "source": "Sample Data",
        "url": "https://example.com/jobs/1"
    },
    {
        "id": 2,
        "title": "UI UX Designer",
        "company": "Design Studio",
        "location": "Hamburg, Germany",
        "remote": False,
        "source": "Sample Data",
        "url": "https://example.com/jobs/2"
    },
    {
        "id": 3,
        "title": "Python Backend Developer",
        "company": "Tech Company",
        "location": "Munich, Germany",
        "remote": True,
        "source": "Sample Data",
        "url": "https://example.com/jobs/3"
    }
]


@app.get("/")
def home():
    return {
        "message": "JobPulse API is running",
        "docs": "/docs"
    }


@app.get("/jobs/search")
def search_jobs(
    title: str | None = Query(default=None),
    location: str | None = Query(default=None),
    remote: bool | None = Query(default=None)
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

    return {
        "count": len(results),
        "results": results
    }