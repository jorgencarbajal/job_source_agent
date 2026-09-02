"""Walk from a company website to the page listing its open jobs.

This is stage 2's loop, and it does nothing clever on its own. It renders a page
with `browser.py`, asks `arrival.py` what the page is, asks `llm.py` where to go
next, and repeats. All the judgment lives in those three; what belongs here is
knowing when to stop and what to keep.

Two rules shape it.

**Keep the best page seen, rather than stopping at the first good one.** Scores
are only comparable within one company: Riverside's homepage scores 30 while
MLK's real job board scores 24, so no fixed line separates them everywhere. What
does hold is that a company's own listings page beats its own homepage every
time, on all twenty measured. So the walk records every page and returns the
highest scorer.

**Stop early only when certain.** A page at `CONFIDENT_SCORE` or above is
unmistakably a job board -- 16 of the 20 real listings pages clear it and no
page that was not one has ever come close, the highest being a homepage at 30.
Below that the walk spends its whole budget and lets best-so-far decide, which
costs time and never costs accuracy.

Dead ends are expected rather than fatal. A page can fail to load, or turn out to
be a "Life at" page with nothing onward. When that happens the walk backs up and
takes the next-best link from the page before it. Real paths run 1 to 3 hops and
the budget is 5, so that slack is there to spend.

One page load is repeated on purpose: a page reading zero on every job-row signal
is rendered a second time before being believed. Esri once cached with the right
address, the right title, and no jobs at all, because the site's own background
request failed that once -- nothing about it looked wrong.

Running it:

    uv run python -m job_source_agent.navigator <url> [options]

    1. Render the starting page.
    2. Score it, and keep it if it is the best so far.
    3. Stop if the score is decisive, otherwise ask the model for a link.
    4. Follow that link, or back up to a leftover one at a dead end.
    5. Print every hop taken, then the page it settled on.

    Each hop costs one model call, a fraction of a cent.

Options:

    --max-hops N   How many pages to load before giving up. Default 5.
    --quiet        Print only the final answer, without the hop-by-hop trace.
    --headful      Ignored here; set BROWSER_HEADLESS=0 in .env to watch it work.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from job_source_agent import arrival, llm
from job_source_agent.arrival import CONFIDENT_SCORE, ArrivalScore
from job_source_agent.browser import BrowserSession
from job_source_agent.config import MAX_HOPS
from job_source_agent.models import Hop

ARRIVED = "arrived"
LIKELY = "likely, not certain"
NO_LISTINGS = "no listings found"
UNREACHABLE = "nothing loaded"


@dataclass
class Walk:
    """Everything one walk from a website produced.

    `listings_url` is the best page found rather than the last page visited, and
    `outcome` says why the walk ended so a failure can be told apart from a
    confident success.
    """

    start_url: str
    listings_url: str | None = None
    score: int = 0
    outcome: str = UNREACHABLE
    hops: list[Hop] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Report whether the walk found something that actually looks like a
        listings page. Returns False when no page loaded, and also when pages
        loaded but none of them showed enough job evidence to count, since
        best-so-far always has *a* page to hand back."""
        return self.listings_url is not None and self.score >= arrival.ARRIVED_SCORE


def _key(url: str) -> str:
    """Reduce a URL to the form used for remembering where we have been, which
    is the host and path without a trailing slash. Delegates to `llm.py` so the
    loop and the ranker never disagree about what counts as the same page."""
    return llm.normalise_for_compare(url)


def _looks_empty(score: ArrivalScore) -> bool:
    """Report whether a page showed no evidence of job rows whatsoever. Returns
    True only when every Tier 1 signal paid nothing, which is the shape of a
    page whose listings failed to load rather than a page that has none."""
    return all(
        score.points.get(signal.name, 0) == 0
        for signal in arrival.SIGNALS
        if signal.tier == 1
    )


async def _render_and_score(
    session: BrowserSession, url: str, allow_retry: bool
) -> tuple[object | None, ArrivalScore | None]:
    """Load one page and score it, rendering a second time if the first result
    shows no job rows at all. Returns the snapshot and its score, or a pair of
    Nones when the page could not be loaded; `allow_retry` is spent once per
    walk so a site that genuinely has no jobs is not loaded twice every hop."""
    snapshot = await session.snapshot(url)
    if snapshot is None:
        return None, None

    score = arrival.score(snapshot)
    if allow_retry and _looks_empty(score):
        again = await session.snapshot(url)
        if again is not None:
            second = arrival.score(again)
            if second.total > score.total:
                return again, second
    return snapshot, score


async def walk(
    start_url: str,
    session: BrowserSession | None = None,
    max_hops: int = MAX_HOPS,
) -> Walk:
    """Follow links from a company website until the job listings are found or
    the budget runs out, returning the best page seen along the way. Creates its
    own browser if one is not passed, and closes only what it created, so the
    benchmark can share a single Chrome across many walks."""
    own_session = session is None
    session = session or BrowserSession()

    result = Walk(start_url=start_url)
    visited: set[str] = set()
    leftovers: list[llm.Candidate] = []
    retry_available = True
    url: str | None = start_url

    try:
        for _ in range(max_hops):
            if url is None:
                break
            visited.add(_key(url))

            snapshot, score = await _render_and_score(session, url, retry_available)

            if snapshot is None:
                result.hops.append(Hop(url=url, reason="page would not load", score=0))
                url = leftovers.pop(0).href if leftovers else None
                continue

            if _looks_empty(score):
                retry_available = False
            visited.add(_key(snapshot.url))

            if score.total > result.score or result.listings_url is None:
                result.listings_url = snapshot.url
                result.score = score.total

            if score.total >= CONFIDENT_SCORE:
                result.hops.append(
                    Hop(url=snapshot.url, reason=score.reason, score=score.total)
                )
                break

            picked = await llm.choose(snapshot, visited, arrival_note=score.reason)
            if picked is None:
                result.hops.append(
                    Hop(url=snapshot.url, reason="nothing worth following", score=score.total)
                )
                url = leftovers.pop(0).href if leftovers else None
                continue

            result.hops.append(
                Hop(url=snapshot.url, reason=", ".join(picked.why), score=score.total)
            )
            leftovers = [
                candidate
                for candidate in llm.shortlist(snapshot, visited)
                if _key(candidate.href) != _key(picked.href)
            ]
            url = picked.href
    finally:
        if own_session:
            await session.close()

    result.outcome = _describe(result)
    return result


def _describe(result: Walk) -> str:
    """Say how the walk ended, judged on the best page found rather than on how
    the loop happened to exit. Returns one of the four outcome strings, because
    running out of links after finding the listings page is a success and
    running out after finding nothing is not."""
    if result.listings_url is None:
        return UNREACHABLE
    if result.score >= CONFIDENT_SCORE:
        return ARRIVED
    if result.score >= arrival.ARRIVED_SCORE:
        return LIKELY
    return NO_LISTINGS


def format_walk(result: Walk, quiet: bool = False) -> str:
    """Lay out one walk as text, hop by hop, ending with the page it settled on.
    Returns the whole report as a single string so callers can print it or log
    it."""
    lines = [f"start  : {result.start_url}"]
    if not quiet:
        lines.append("")
        for number, hop in enumerate(result.hops, 1):
            lines.append(f"  hop {number}  [{hop.score:>2}]  {hop.url}")
            lines.append(f"           {hop.reason}")
        lines.append("")
    lines.append(f"answer : {result.listings_url or 'none found'}")
    lines.append(f"score  : {result.score}/{arrival.MAX_SCORE}")
    lines.append(f"outcome: {result.outcome}  ({len(result.hops)} hops)")
    return "\n".join(lines)


def main() -> int:
    """Walk one website from the command line and print the trace. Returns 1
    when nothing was found, so a failure is visible without reading the
    output."""
    import argparse

    parser = argparse.ArgumentParser(description="Walk a website to its job listings.")
    parser.add_argument("url")
    parser.add_argument("--max-hops", type=int, default=MAX_HOPS)
    parser.add_argument("--quiet", action="store_true", help="final answer only")
    args = parser.parse_args()

    result = asyncio.run(walk(args.url, max_hops=args.max_hops))
    print(format_walk(result, args.quiet))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
