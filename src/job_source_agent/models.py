"""The data that moves between stages."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CompanyIdentity:
    """What Stage 1 gets out of a LinkedIn job posting.

    `website` is the whole point -- it is the Stage 2 entry point, and the only
    field that cannot be read off the job posting itself.
    """

    company_name: str
    slug: str
    website: str | None
    job_id: str


@dataclass
class Hop:
    """One step of the walk from a company website toward its listings."""

    url: str
    reason: str  # why the navigator followed this link
    score: int = 0  # what arrival.py made of the page once it loaded


@dataclass
class JobSourceResult:
    """What the pipeline returns for one LinkedIn URL."""

    linkedin_url: str
    identity: CompanyIdentity | None = None
    listings_url: str | None = None
    hops: list[Hop] = field(default_factory=list)
    # Why we stopped: arrived, ran out of hops, no website, or an error.
    outcome: str = "unknown"

    @property
    def ok(self) -> bool:
        return self.listings_url is not None
