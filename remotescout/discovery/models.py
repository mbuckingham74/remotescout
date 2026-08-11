from dataclasses import dataclass


@dataclass
class DiscoveredJob:
    source: str
    source_url: str
    title: str
    employer: str
    description: str
    source_job_id: str | None = None
    location: str | None = None
    compensation: str | None = None
    posted_at: str | None = None
