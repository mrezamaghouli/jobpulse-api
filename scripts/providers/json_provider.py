import json
from pathlib import Path

from scripts.providers.base_provider import JobProvider


class JsonJobProvider(JobProvider):
    def __init__(self, file_path):
        self.file_path = Path(file_path)

    def fetch_jobs(self):
        if not self.file_path.exists():
            return []

        with open(self.file_path, "r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, list):
            raise ValueError("Job data file must contain a JSON list.")

        return data