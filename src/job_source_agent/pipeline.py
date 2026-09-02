"""Join stage 1 to stage 2: a LinkedIn job URL in, a job listings page out.

This is the whole product in one function. `linkedin.py` turns a job posting into
the company and its website, `navigator.py` walks that website to the listings.
Everything interesting already happened in those two; this file only wires them
together and records what came out.

The two stages fail differently, and the outcome says which. Stage 1 fails when
ScrapingDog cannot read the posting or the company profile has no website on it,
and there is nothing stage 2 can do about that. Stage 2 fails when the site is
unreachable or nothing on it looks like job listings. Collapsing both into
"failed" would hide which half needs fixing.

`resolve()` is synchronous because it uses a plain HTTP client, so it runs on a
worker thread here. Without that, one slow ScrapingDog call would stall every
other URL being processed at the same time.

Stage 1 costs credits and stage 2 costs a fraction of a cent per hop. Roughly
2.2 cents and 0.4 cents per URL respectively.

Running it:

    uv run python -m job_source_agent.pipeline <linkedin url> [more urls] [options]

    1. Print what it is about to spend, then resolve each posting to a company.
    2. Walk that company's website to its job listings.
    3. Print each result as it finishes, not at the end.
    4. Print a one-line summary.

Options:

    --dry-run        Show what would be looked up and spend nothing. Use this
                     to check a URL list before paying for it.
    --concurrency N  How many URLs to walk at once. Defaults to
                     config.BROWSER_CONCURRENCY, which is 2 -- more than that
                     and pages snapshot half-built. This is the browser side
                     only; stage 1 has its own tighter cap, because
                     ScrapingDog rejects calls made too close together.
    --max-hops N     Hop budget per website. Default 5.
    --quiet          One line per URL instead of the full hop trace.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Sequence

from job_source_agent import linkedin
from job_source_agent.browser import BrowserSession
from job_source_agent.config import (
    BROWSER_CONCURRENCY,
    MAX_HOPS,
    SCRAPINGDOG_CONCURRENCY,
)
from job_source_agent.models import CompanyIdentity, JobSourceResult
from job_source_agent.navigator import walk

NO_WEBSITE = "no website on the company profile"
DEFAULT_CONCURRENCY = BROWSER_CONCURRENCY


_stage_one_limit: asyncio.Semaphore | None = None


def _stage_one_gate() -> asyncio.Semaphore:
    """Hand back the semaphore that caps how many postings may be looked up at
    once, creating it on first use. It is built lazily rather than at import
    time because a semaphore binds to the running event loop, and there is no
    loop yet when the module is imported."""
    global _stage_one_limit
    if _stage_one_limit is None:
        _stage_one_limit = asyncio.Semaphore(SCRAPINGDOG_CONCURRENCY)
    return _stage_one_limit


async def identify(linkedin_url: str) -> tuple[CompanyIdentity | None, str | None]:
    """Run stage 1 without blocking everything else, returning the company or a
    reason it could not be found. `asyncio.to_thread` moves the synchronous
    ScrapingDog call onto a worker thread so other URLs keep moving while it
    waits, and the semaphore keeps the request rate below what ScrapingDog will
    accept -- two calls fired together came back 429."""
    try:
        async with _stage_one_gate():
            identity = await asyncio.to_thread(linkedin.resolve, linkedin_url)
    except linkedin.LinkedInError as exc:
        return None, f"stage 1 failed: {exc}"
    except Exception as exc:
        return None, f"stage 1 error: {type(exc).__name__}: {exc}"
    return identity, None


async def run(
    linkedin_url: str,
    session: BrowserSession | None = None,
    max_hops: int = MAX_HOPS,
) -> JobSourceResult:
    """Take one LinkedIn job URL all the way to a job listings page. Returns a
    `JobSourceResult` whose `outcome` says how far it got, and which never
    raises -- a failed URL is a result, not an exception, so one bad posting
    cannot take down a batch of ten."""
    result = JobSourceResult(linkedin_url=linkedin_url)

    identity, problem = await identify(linkedin_url)
    if identity is None:
        result.outcome = problem or "stage 1 failed"
        return result

    result.identity = identity
    if not identity.website:
        result.outcome = NO_WEBSITE
        return result

    walked = await walk(identity.website, session=session, max_hops=max_hops)
    result.hops = walked.hops
    result.outcome = walked.outcome
    if walked.ok:
        result.listings_url = walked.listings_url
    return result


async def run_many(
    linkedin_urls: Sequence[str],
    concurrency: int = DEFAULT_CONCURRENCY,
    max_hops: int = MAX_HOPS,
) -> AsyncIterator[JobSourceResult]:
    """Process several URLs at once, handing back each result the moment it is
    ready rather than waiting for the slowest. One browser is shared across all
    of them, and `yield` inside an async function is what makes this something
    the caller can loop over as results arrive."""
    session = BrowserSession()
    limit = asyncio.Semaphore(concurrency)

    async def one(url: str) -> JobSourceResult:
        async with limit:
            return await run(url, session=session, max_hops=max_hops)

    tasks = [asyncio.create_task(one(url)) for url in linkedin_urls]
    try:
        for finished in asyncio.as_completed(tasks):
            yield await finished
    finally:
        for task in tasks:
            task.cancel()
        await session.close()


def format_result(result: JobSourceResult, quiet: bool = False) -> str:
    """Lay out one finished URL as text, with its hops unless `quiet`. Returns
    the whole block as a single string so the caller can print it as soon as the
    result arrives."""
    company = result.identity.company_name if result.identity else "unknown company"
    lines = [f"{result.linkedin_url}", f"  company : {company}"]
    if result.identity and result.identity.website:
        lines.append(f"  website : {result.identity.website}")

    if not quiet:
        for number, hop in enumerate(result.hops, 1):
            lines.append(f"  hop {number}   [{hop.score:>2}] {hop.url}")
            lines.append(f"            {hop.reason}")

    lines.append(f"  answer  : {result.listings_url or '-'}")
    lines.append(f"  outcome : {result.outcome}")
    return "\n".join(lines)


async def _run_cli(urls: list[str], concurrency: int, max_hops: int, quiet: bool) -> int:
    """Drive the command line run and print results as they land. Returns the
    number of URLs that produced a listings page, which the caller turns into an
    exit code."""
    found = 0
    async for result in run_many(urls, concurrency=concurrency, max_hops=max_hops):
        print(format_result(result, quiet))
        print()
        if result.ok:
            found += 1
    return found


def main() -> int:
    """Run the pipeline from the command line over one or more LinkedIn URLs.
    Returns 0 when every URL found a listings page and 1 otherwise, so a partial
    run is visible without reading the output."""
    import argparse

    parser = argparse.ArgumentParser(description="LinkedIn job URL -> job listings page.")
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--dry-run", action="store_true", help="spend nothing, just plan")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--max-hops", type=int, default=MAX_HOPS)
    parser.add_argument("--quiet", action="store_true", help="skip the hop trace")
    args = parser.parse_args()

    credits = len(args.urls) * 55
    print(
        f"{len(args.urls)} URL(s): about {credits} ScrapingDog credits "
        f"(~{len(args.urls) * 2.2:.0f}c) plus ~{len(args.urls) * 0.4:.0f}c of model calls."
    )

    if args.dry_run:
        print("\n--dry-run, nothing spent. Would look up:")
        for url in args.urls:
            print(f"  {linkedin.extract_job_id(url)}  <-  {url}")
        return 0

    print()
    found = asyncio.run(
        _run_cli(args.urls, args.concurrency, args.max_hops, args.quiet)
    )
    print(f"Found listings for {found}/{len(args.urls)}.")
    return 0 if found == len(args.urls) else 1


if __name__ == "__main__":
    raise SystemExit(main())
