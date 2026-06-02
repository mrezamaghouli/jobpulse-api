from scripts.providers.base_provider import JobProvider


class LinkedInProviderPlaceholder(JobProvider):
    def fetch_jobs(self):
        raise NotImplementedError(
            "LinkedIn provider is not implemented. "
            "Use an authorized LinkedIn data source, official integration, "
            "or compliant third-party job data provider."
        )