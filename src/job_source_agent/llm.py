"""Choose which link on a page leads toward the job listings.

`arrival.py` decides whether we have arrived. This module decides where to go
next, and it is the only place in stage 2 that spends model tokens.

The problem is volume. A company homepage carries between 100 and 800 links --
Gopuff's has 811 -- and handing all of them to a model every hop would be slow,
expensive, and mostly noise. So the work happens in two passes. A cheap ranker
scores every link on wording and position and keeps the best handful, then the
model picks from that shortlist and says why.

The ranker is deliberately generous rather than clever. Its only job is to make
sure the right link survives into the shortlist; deciding between "Join our
team", "Life at Gopuff" and "Employee login" is what the model is for, and a
ranker that tried to make that call itself would quietly throw away answers.

Two things earn a link most of its score: careers wording in its text or its
address, and sitting in the navigation or the footer, which is where companies
almost always put the careers link.

Running it:

    uv run python -m job_source_agent.llm <url> [options]

    1. Render the URL in a browser, or load a page cached on disk.
    2. Rank every link on it.
    3. Print the shortlist in order, with the score and why each one scored.
    4. With --choose, ask the model to pick one and print its answer.

Options:

    --cached PATH  Rank a page saved by benchmark/dump_pages.py instead of
                   rendering a live one. Give the .json sidecar.
    --top N        How many links to show. Defaults to the shortlist size.
    --all          Show every link with a score above zero, not just the
                   shortlist, which is how you check the ranker is not
                   discarding something it should have kept.
    --choose       Also call the model and print which link it picked and why.
                   This is the only option here that costs anything; without
                   it the whole file runs for free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from pydantic import BaseModel

from job_source_agent.arrival import ATS_HOST_RE
from job_source_agent.config import (
    ANTHROPIC_WORKSPACE_ID,
    LLM_MAX_TOKENS,
    LLM_MODEL,
    LLM_TIMEOUT,
    require_anthropic_key,
)

SHORTLIST_SIZE = 30

CAREERS_TEXT_RE = re.compile(
    r"\b(?:careers?|jobs?|openings?|opportunit(?:y|ies)|vacanc(?:y|ies)"
    r"|hiring|employment|recruit(?:ing|ment)?|talent|apply|positions?"
    r"|join\s+(?:us|our\s+team|the\s+team)|work\s+(?:with|for|at|here)"
    r"|life\s+at|working\s+at|come\s+work|we're\s+hiring|search\s+jobs"
    r"|view\s+(?:all\s+)?(?:jobs|openings)|see\s+(?:all\s+)?(?:jobs|openings)"
    r"|all\s+jobs|current\s+openings|explore\s+(?:jobs|careers))\b",
    re.IGNORECASE,
)

CAREERS_HREF_RE = re.compile(
    r"(?:/careers?|/jobs?|search-?jobs|job-?search|/openings|/opportunit"
    r"|/vacanc|/join-?us|/work-?with-?us|/work-?for-?us|/employment"
    r"|/hiring|/recruit|/talent|search-?results|/life-?at)",
    re.IGNORECASE,
)

DEAD_END_RE = re.compile(
    r"\b(?:privacy|cookie|terms|legal|accessibility|sitemap|disclaimer"
    r"|copyright|trademark|investor|shareholder|press\s+release|newsroom"
    r"|sign\s?in|log\s?in|logout|register|subscribe|newsletter|donate"
    r"|cart|checkout|store\s+locator|contact\s+us|faq)\b",
    re.IGNORECASE,
)

INTERNAL_JOB_RE = re.compile(
    r"(?:employee|internal|current\s+employee|alumni|returning\s+applicant"
    r"|my\s?profile|candidate\s+login|job\s+description)",
    re.IGNORECASE,
)

TEXT_POINTS = 40
HREF_POINTS = 30
ATS_POINTS = 35
NAV_POINTS = 12
FOOTER_POINTS = 10
DEAD_END_PENALTY = -30
INTERNAL_PENALTY = -25
NO_TEXT_PENALTY = -5


@dataclass(frozen=True)
class Candidate:
    """One link the ranker considered, with its score and what earned it.

    `why` lists the reasons in the order they were applied, so a surprising
    score can be read back rather than guessed at.
    """

    text: str
    href: str
    score: int
    why: tuple[str, ...]

    def __str__(self) -> str:
        label = self.text or "(no text)"
        return f"{self.score:>5}  {label[:48]:<50} {self.href[:70]}  [{', '.join(self.why)}]"


def normalise_for_compare(url: str) -> str:
    """Reduce a URL to the part that decides whether two links go to the same
    place, which is the host and path without a trailing slash. Returns a
    lowercased string, so `Example.com/Careers/` and `example.com/careers` come
    out identical."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    return f"{parts.netloc.lower()}{path.lower()}"


def rank_link(text: str, href: str, in_nav: bool, in_footer: bool) -> Candidate:
    """Score one link on how likely it is to lead toward job listings. Returns a
    `Candidate` carrying the score and the list of reasons behind it; a score of
    zero or less means nothing about the link suggested careers at all."""
    score = 0
    why: list[str] = []

    if CAREERS_TEXT_RE.search(text or ""):
        score += TEXT_POINTS
        why.append("careers wording")
    if CAREERS_HREF_RE.search(href or ""):
        score += HREF_POINTS
        why.append("careers address")
    if ATS_HOST_RE.search(href or ""):
        score += ATS_POINTS
        why.append("job board host")
    if in_nav:
        score += NAV_POINTS
        why.append("in nav")
    if in_footer:
        score += FOOTER_POINTS
        why.append("in footer")
    if DEAD_END_RE.search(text or ""):
        score += DEAD_END_PENALTY
        why.append("dead end wording")
    if INTERNAL_JOB_RE.search(text or "") or INTERNAL_JOB_RE.search(href or ""):
        score += INTERNAL_PENALTY
        why.append("internal or employee only")
    if not (text or "").strip():
        score += NO_TEXT_PENALTY
        why.append("no link text")

    return Candidate(text=text or "", href=href, score=score, why=tuple(why))


def rank(snapshot, visited: set[str] | None = None) -> list[Candidate]:
    """Rank every link on a page, best first, dropping anything already visited
    and anything that scored nothing. Returns a list that may be empty, which is
    the loop's signal that this page leads nowhere new.

    The page's own address is always excluded, whether or not the caller passed
    it in `visited`, because AMETEK's careers page links to itself as "Join Us"
    and ranks it first -- following that would burn a hop to stand still.
    """
    seen = set(visited or ()) | {normalise_for_compare(snapshot.url)}
    best: dict[str, Candidate] = {}

    for link in snapshot.links:
        key = normalise_for_compare(link.href)
        if not key or key in seen:
            continue
        candidate = rank_link(link.text, link.href, link.in_nav, link.in_footer)
        if candidate.score <= 0:
            continue
        current = best.get(key)
        if current is None or candidate.score > current.score:
            best[key] = candidate

    return sorted(best.values(), key=lambda item: (-item.score, len(item.href)))


def shortlist(snapshot, visited: set[str] | None = None) -> list[Candidate]:
    """Return just the links worth showing the model, best first. Caps the list
    at `SHORTLIST_SIZE` so the prompt stays small, which is the whole reason the
    ranker exists."""
    return rank(snapshot, visited)[:SHORTLIST_SIZE]


SYSTEM_PROMPT = """You are navigating a company's website to find the page that \
lists their open job postings -- the page a job seeker would land on to browse or \
search every current opening.

You are shown the page you are on and a numbered shortlist of links from it. Pick \
the single link most likely to lead to that listings page, or 0 if none of them \
would.

What you are looking for, in order of preference:
1. A job board or search results page -- "Search jobs", "View all openings",
   "All jobs (448)", or a link to an applicant tracking system such as Workday,
   Ashby, Greenhouse, iCIMS, Oracle, GovernmentJobs or edjoin.
2. A careers landing page, if no board is linked directly. It is a step toward
   the answer, not the answer.

What to avoid:
- Pages about working somewhere rather than pages of jobs: "Life at", "Our
  culture", "Meet our people", "Benefits", "Locations", "Diversity".
- Anything for existing employees: "Employee login", "Internal candidates",
  "Returning applicant", "My profile".
- Job descriptions or classification documents, which are HR policy rather than
  open positions.
- A single specific job posting, when a link to the whole list is available.

Answer with the number of your choice and one short sentence saying why."""


class LinkChoice(BaseModel):
    """The model's answer: which shortlist entry to follow, and why.

    `choice` is the number shown beside a link, or 0 when none of them is worth
    following. It is an index rather than a URL so that a wrong answer is caught
    by a range check instead of quietly sending the browser somewhere invented.
    """

    choice: int
    reason: str


def build_prompt(snapshot, candidates: list[Candidate], arrival_note: str = "") -> str:
    """Lay out the current page and its shortlist as the numbered list the model
    answers against. Returns the whole prompt as one string, with links numbered
    from 1 so that 0 can mean "none of these"."""
    lines = [
        f"Current page: {snapshot.url}",
        f"Page title: {snapshot.title or '(none)'}",
    ]
    if arrival_note:
        lines.append(f"Job listing signals on this page: {arrival_note}")
    lines.append("")
    lines.append("Links to choose from:")

    for number, candidate in enumerate(candidates, 1):
        where = " [nav]" if "in nav" in candidate.why else ""
        where += " [footer]" if "in footer" in candidate.why else ""
        text = candidate.text or "(no link text)"
        lines.append(f"{number}. {text}{where}\n   {candidate.href}")

    lines.append("")
    lines.append("Which number leads toward the full list of open jobs?")
    return "\n".join(lines)


async def choose(
    snapshot, visited: set[str] | None = None, arrival_note: str = ""
) -> Candidate | None:
    """Ask the model which link to follow next and return it, or None when the
    page leads nowhere worth going. Falls back to the highest-ranked link if the
    API call fails, so a network hiccup costs a worse hop rather than the whole
    run."""
    candidates = shortlist(snapshot, visited)
    if not candidates:
        return None

    from anthropic import AsyncAnthropic

    headers = (
        {"anthropic-workspace-id": ANTHROPIC_WORKSPACE_ID}
        if ANTHROPIC_WORKSPACE_ID
        else None
    )
    client = AsyncAnthropic(
        api_key=require_anthropic_key(),
        timeout=LLM_TIMEOUT,
        default_headers=headers,
    )
    try:
        response = await client.messages.parse(
            model=LLM_MODEL,
            max_tokens=LLM_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": build_prompt(snapshot, candidates, arrival_note),
                }
            ],
            output_format=LinkChoice,
        )
        answer = response.parsed_output
    except Exception as exc:
        return replace(
            candidates[0], why=candidates[0].why + (f"model unavailable: {type(exc).__name__}",)
        )
    finally:
        await client.close()

    if answer is None or not 1 <= answer.choice <= len(candidates):
        return None

    picked = candidates[answer.choice - 1]
    return replace(picked, why=(answer.reason,))


def main() -> int:
    """Rank one page from the command line, live or cached, and print the
    result. Returns 1 when a live page cannot be loaded, so a failure shows up
    without reading the output."""
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Rank a page's links by careers likelihood.")
    parser.add_argument("url", nargs="?", default=None)
    parser.add_argument("--cached", default=None, help="rank a dump_pages .json sidecar")
    parser.add_argument("--top", type=int, default=SHORTLIST_SIZE)
    parser.add_argument("--all", action="store_true", help="show every scoring link")
    parser.add_argument("--choose", action="store_true", help="also ask the model (costs)")
    args = parser.parse_args()

    if args.cached:
        snapshot = _snapshot_from_cache(args.cached)
    elif args.url:
        from job_source_agent.browser import snapshot as render

        snapshot = asyncio.run(render(args.url))
        if snapshot is None:
            print(f"could not load {args.url}")
            return 1
    else:
        parser.error("give a url, or --cached PATH")

    ranked = rank(snapshot)
    shown = ranked if args.all else ranked[: args.top]

    print(f"url   : {snapshot.url}")
    print(f"links : {len(snapshot.links)} on the page, {len(ranked)} scored above zero\n")
    for candidate in shown:
        print(candidate)
    if not args.all and len(ranked) > len(shown):
        print(f"\n... {len(ranked) - len(shown)} more (--all to see them)")

    if args.choose:
        picked = asyncio.run(choose(snapshot))
        print("\nmodel picked:")
        if picked is None:
            print("  nothing worth following on this page")
        else:
            print(f"  {picked.text or '(no text)'}  ->  {picked.href}")
            print(f"  because: {', '.join(picked.why)}")
    return 0


def _snapshot_from_cache(sidecar_path: str):
    """Rebuild a `PageSnapshot` from a page saved by benchmark/dump_pages.py, so
    the ranker can be exercised without a browser. Returns the snapshot, and
    raises `FileNotFoundError` if either cached file is missing."""
    import json
    from pathlib import Path

    from job_source_agent.browser import Link, PageSnapshot

    path = Path(sidecar_path)
    meta = json.loads(path.read_text(encoding="utf-8"))
    links = [
        Link(
            text=item.get("text", ""),
            href=item.get("href", ""),
            in_footer=bool(item.get("in_footer")),
            in_nav=bool(item.get("in_nav")),
        )
        for item in meta.get("links", [])
    ]
    return PageSnapshot(
        url=meta.get("final_url", ""),
        title=meta.get("title", ""),
        html=path.with_suffix(".html").read_text(encoding="utf-8", errors="replace"),
        links=links,
    )


if __name__ == "__main__":
    raise SystemExit(main())
