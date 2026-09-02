"""Stage 1: LinkedIn job URL -> company name and website. The only paid code.

This is the manual path, transcribed. Opening a job posting tells you the company
and links to its profile; the website only appears on the profile's About
section. ScrapingDog scrapes one page per call, so those two pages are two calls:

    /linkedinjobs?job_id=...        the posting  -> company name + profile link
    /linkedin?type=company&linkId=  the profile  -> website

Run it directly to see what a single URL resolves to:

    uv run python -m job_source_agent.linkedin <linkedin job url> [--raw]

Both calls cost credits. To test the second one alone against a slug you already
know, skipping the first:

    uv run python -m job_source_agent.linkedin --slug harvey-ai
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from job_source_agent.config import (
    SCRAPINGDOG_ATTEMPTS,
    SCRAPINGDOG_BACKOFF,
    SCRAPINGDOG_JOB_URL,
    SCRAPINGDOG_PROFILE_URL,
    SCRAPINGDOG_RETRY_STATUS,
    SCRAPINGDOG_TIMEOUT,
    require_scrapingdog_key,
)
from job_source_agent.models import CompanyIdentity


class LinkedInError(RuntimeError):
    """Stage 1 could not resolve a company. Carries what went wrong."""

###
def extract_job_id(linkedin_url: str) -> str:
    """
    Pull the numeric job id out of a LinkedIn job URL.
    """
    match = re.search(r"/jobs/view/(\d+)", linkedin_url)
    if match:
        return match.group(1)
    match = re.search(r"[?&]currentJobId=(\d+)", linkedin_url)
    if match:
        return match.group(1)
    if linkedin_url.strip().isdigit():
        return linkedin_url.strip()
    raise LinkedInError(f"no job id found in {linkedin_url!r}")

###
def extract_slug(company_link: str) -> str | None:
    """
    Pull the company slug out of the profile link on a job posting.
    """
    if not company_link:
        return None
    path = urlparse(company_link).path if "//" in company_link else company_link
    match = re.search(r"/(?:company|showcase)/([^/?#]+)", path)
    if match:
        return match.group(1)
    # Some responses give the bare slug already.
    bare = company_link.strip().strip("/")
    return bare if bare and "/" not in bare else None

###
def _unwrap(payload: Any) -> dict:
    """
    Checks if it is a list, if it is and empty -> error. Ensures it is a dict, if not error. Finally returns the dict.
    """
    if isinstance(payload, list):
        if not payload:
            raise LinkedInError("ScrapingDog returned an empty list")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise LinkedInError(f"unexpected response type: {type(payload).__name__}")
    return payload

###
def _wait_for(attempt: int, retry_after: str | None) -> float:
    """Work out how long to wait before trying a rejected call again. Returns
    the server's own `Retry-After` value when it sent one, and otherwise a delay
    that doubles each attempt, so a burst backs off instead of hammering."""
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return SCRAPINGDOG_BACKOFF * (2**attempt)


def _get(client: httpx.Client, url: str, params: dict) -> dict:
    """
    GET request the the profile URL. Unwrap scraping dogs list response, return the dict.

    Retries on 429 and on server errors, waiting longer each time. ScrapingDog
    rejects calls made too close together even when only two URLs are in flight,
    and a rejected call is a temporary condition rather than a failed lookup.
    """
    last: str = "no attempt was made"

    for attempt in range(SCRAPINGDOG_ATTEMPTS):
        try:
            response = client.get(url, params=params)
        except httpx.HTTPError as exc:
            last = f"request to {url} failed: {exc}"
            if attempt == SCRAPINGDOG_ATTEMPTS - 1:
                raise LinkedInError(last) from exc
            time.sleep(_wait_for(attempt, None))
            continue

        if response.status_code == 200:
            try:
                return _unwrap(response.json())
            except ValueError as exc:
                raise LinkedInError(
                    f"{url} did not return JSON: {response.text[:200]}"
                ) from exc

        last = f"{url} returned {response.status_code}: {response.text[:200]}"
        if (
            response.status_code in SCRAPINGDOG_RETRY_STATUS
            and attempt < SCRAPINGDOG_ATTEMPTS - 1
        ):
            time.sleep(_wait_for(attempt, response.headers.get("retry-after")))
            continue
        raise LinkedInError(last)

    raise LinkedInError(last)

###
def fetch_job(job_id: str, *, client: httpx.Client | None = None) -> dict:
    """
    Returns ScrapingDog's raw response to the job posting call.
    """
    owns = client is None
    client = client or httpx.Client(timeout=SCRAPINGDOG_TIMEOUT)
    try:
        return _get(
            client,
            SCRAPINGDOG_JOB_URL,
            {"api_key": require_scrapingdog_key(), "job_id": job_id},
        )
    finally:
        if owns:
            client.close()

###
def fetch_company(slug: str, *, client: httpx.Client | None = None) -> dict:
    """
    Use the provided slug to fetch the dict response from the company profile. If a client was passed in, dont close the connection.
    """
    owns = client is None
    client = client or httpx.Client(timeout=SCRAPINGDOG_TIMEOUT)
    try:
        return _get(
            client,
            SCRAPINGDOG_PROFILE_URL,
            # camelCase is what ScrapingDog expects; an unrecognised parameter
            # is ignored, and the lookup then fails as "Not a valid Linkedin Id".
            {
                "api_key": require_scrapingdog_key(),
                "type": "company",
                "linkId": slug,
            },
        )
    finally:
        if owns:
            client.close()


def resolve(linkedin_url: str) -> CompanyIdentity:
    """LinkedIn job URL -> the company behind it, website included.

    Two paid calls. Raises LinkedInError if the posting cannot be read or names
    no company; a company with no website on its profile is not an error, it
    just means Stage 2 has nowhere to start.
    """
    job_id = extract_job_id(linkedin_url)

    with httpx.Client(timeout=SCRAPINGDOG_TIMEOUT) as client:
        job = fetch_job(job_id, client=client)

        company_name = (job.get("company_name") or "").strip()
        slug = extract_slug(job.get("company_linkedin_id") or "")
        if not company_name and not slug:
            raise LinkedInError(f"job {job_id} named no company")
        if not slug:
            raise LinkedInError(f"job {job_id} gave no company profile link")

        profile = fetch_company(slug, client=client)

    website = (profile.get("website") or "").strip() or None
    if not company_name:
        company_name = (profile.get("company_name") or "").strip()

    return CompanyIdentity(
        company_name=company_name,
        slug=slug,
        website=website,
        job_id=job_id,
    )


if __name__ == "__main__":
    """
    Separate the command arguments. If the slug is provided we only make the one api call to the company profile to extract the website. We add the ability to show raw output for debugging. You can run this as a stand alone file are call the resolve function. The resolve function will be imported into other modules. 
    """
    import json
    import sys

    argv = sys.argv[1:]
    args = [a for a in argv if not a.startswith("--")]
    show_raw = "--raw" in argv

    # --slug, if you want to skip the job listing go directly to the company profile, exit when complete.
    if "--slug" in argv:
        known_slug = argv[argv.index("--slug") + 1]
        profile = fetch_company(known_slug)
        print(f"slug    : {known_slug}")
        print(f"company : {(profile.get('company_name') or '').strip() or '(none)'}")
        print(f"website : {(profile.get('website') or '').strip() or '(none)'}")
        # in case you wan to see the raw output for debugging
        if show_raw:
            print("--- company response ---")
            print(json.dumps(profile, indent=2)[:4000])
        raise SystemExit(0)

    # if there is not url -> systemexit(1)
    if not args:
        print("usage: linkedin.py <linkedin job url> [--raw]")
        print("       linkedin.py --slug <company-slug> [--raw]")
        raise SystemExit(1)

    url = args[0]
    job_id = extract_job_id(url)
    print(f"job_id  : {job_id}")

    with httpx.Client(timeout=SCRAPINGDOG_TIMEOUT) as c:
        job = fetch_job(job_id, client=c)
        slug = extract_slug(job.get("company_linkedin_id") or "")
        print(f"company : {(job.get('company_name') or '').strip() or '(none)'}")
        print(f"slug    : {slug or '(none)'}")
        if show_raw:
            print("\n--- job response ---")
            print(json.dumps(job, indent=2)[:4000])

        if slug:
            profile = fetch_company(slug, client=c)
            print(f"website : {(profile.get('website') or '').strip() or '(none)'}")
            if show_raw:
                print("\n--- company response ---")
                print(json.dumps(profile, indent=2)[:4000])
