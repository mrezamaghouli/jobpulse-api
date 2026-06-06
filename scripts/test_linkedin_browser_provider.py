from scripts.providers.linkedin_browser_provider import LinkedInBrowserProvider


def main():
    provider = LinkedInBrowserProvider()
    jobs = provider.fetch_jobs()

    print(f"\nProvider returned {len(jobs)} jobs.")

    for index, job in enumerate(jobs, start=1):
        print(f"{index}. {job['title']} | {job['company']} | {job['location']}")


if __name__ == "__main__":
    main()