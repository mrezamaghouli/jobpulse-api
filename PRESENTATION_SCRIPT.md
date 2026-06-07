# JobPulse Presentation Script

## Short Version — 1 Minute

JobPulse is a job search and monitoring dashboard that collects authorized LinkedIn job listings, stores them in PostgreSQL, and displays them through a searchable frontend dashboard.

The main goal of this project is to turn LinkedIn job search results into structured and trackable data. Instead of manually checking job listings, the system can collect jobs based on configured search queries, save them in a database, update existing records, and track when each job was first seen and last seen.

The backend is built with FastAPI, the database is PostgreSQL, and everything runs with Docker Compose. I also added an Adminer database panel, smoke tests, collector run logs, and a local scheduler for automatic updates.

For LinkedIn collection, the system uses an authorized browser session. The user logs in manually, and the crawler uses that saved local session. It does not store LinkedIn credentials in code and does not bypass CAPTCHA.

In the dashboard, users can search jobs, filter results, view job details, open the original LinkedIn job link, and see system stats like total jobs, active jobs, last update time, and latest collector run.

Overall, JobPulse is a practical prototype for job market research, job monitoring, and structured job data collection.

---

## Longer Version — 2 Minutes

JobPulse is a full-stack job data collection and monitoring project. It is built with FastAPI, PostgreSQL, Docker Compose, and a frontend dashboard.

The main problem this project solves is that job listings on LinkedIn change frequently. New jobs appear, old jobs disappear, and it is difficult to manually track what has changed over time. JobPulse solves this by collecting authorized LinkedIn job listings, saving them in a structured database, and keeping them updated.

The system has a LinkedIn browser-based collector. The user logs in manually with an authorized LinkedIn account, and the session is saved locally. After that, the collector can open LinkedIn job search pages, extract job title, company, location, job URL, and other available fields, normalize the data, and store it in PostgreSQL.

The collector supports multiple search queries from a config file. For example, it can collect jobs for UX Designer, UI Designer, Product Designer, and Frontend Developer in Germany. It also avoids duplicate records by using the job URL as a unique identifier. If a job already exists, the system updates its `last_seen_at` timestamp instead of inserting a duplicate.

I also added job tracking fields such as `first_seen_at`, `last_seen_at`, and `is_active`. This makes the system more useful because it can distinguish between jobs that are still active and jobs that have not been seen recently.

Another important part is collector run logging. Every collector execution is saved in a `collector_runs` table, including the keyword, location, status, start time, finish time, and duration. The frontend displays the latest collector run so the user can see when the data was last updated.

The project also includes a local scheduler. With one PowerShell command, the collector can run once or continuously every few hours.

On the frontend, users can see total jobs, LinkedIn jobs, active jobs, remote jobs, companies, locations, last job update, system health, and latest collector status. They can search and filter jobs, view job details, and open the original LinkedIn job page.

Finally, I added smoke tests to validate the main API endpoints, including health, stats, job search, job details, and collector run endpoints.

Overall, JobPulse demonstrates backend development, database design, Docker infrastructure, browser-based authorized data collection, scheduled updates, frontend dashboard development, and practical data pipeline design.
