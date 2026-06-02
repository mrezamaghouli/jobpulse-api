from pydantic import BaseModel


class Job(BaseModel):
    id: int

    linkedin_job_id: str | None = None

    title: str
    company: str
    company_linkedin_url: str | None = None

    location: str
    remote: bool

    job_type: str | None = None
    seniority: str | None = None

    salary_min: int | None = None
    salary_max: int | None = None
    currency: str | None = None

    source: str
    job_url: str

    poster_name: str | None = None
    poster_title: str | None = None
    poster_profile_url: str | None = None

    date_posted: str | None = None


class JobSearchResponse(BaseModel):
    count: int
    page: int
    limit: int
    total_pages: int
    results: list[Job]