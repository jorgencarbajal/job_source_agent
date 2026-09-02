"""Decide whether a rendered page is a list of open jobs.

This is the thing that ends the hop loop. `navigator.py` follows links until it
runs out of budget, and this module is what tells it which of the pages it saw
was the answer.

Nothing here is a gate. Eight independent signals each look at the page and vote,
and the votes are added into one score. That matters because no single test
survives contact with real career sites: page titles lie ("IP Global Career
Site", "Carrers"), job boards live on hosts whose names mean nothing, and half of
them build their listings with JavaScript.

The signals fall into two tiers, and the split is the whole design:

    Tier 1 asks "are there job rows on this page?" and carries the score.
    Tier 2 asks "are we even in the right neighbourhood?" and is worth little.

AMETEK is why. Its LinkedIn profile points at `ametek.com/careers`, a careers
page with no jobs on it -- exactly the page the hop loop must not stop on. It
scores full marks on Tier 2 and zero on every Tier 1 signal. A design that
weighted "this page says Careers" heavily would stop there and be wrong.

Each signal is a yes-or-no test rather than a quantity. Gopuff's board shows
1,523 locations and MLK's shows 6, but both are listings pages, and letting
magnitude into the score would drown the small one. A signal pays its points
once it clears the cutoff, and how far past the cutoff it landed is discarded.

The cutoffs come from `benchmark/calibrate.py`, measured against 20 hand-walked
companies. This module owns the patterns; the benchmark imports them, so that
calibration always measures what the agent actually does.

Running it:

    uv run python -m job_source_agent.arrival <url> [options]

    1. Render the URL in a browser, or load a page already cached on disk.
    2. Measure all eight signals against it.
    3. Print each reading, its cutoff, and the points it earned.
    4. Print the total and whether that clears the arrival score.

Options:

    --cached PATH  Score a page saved by benchmark/dump_pages.py instead of
                   rendering a live one. Give the .json sidecar; the .html
                   beside it is read too. Free and instant.
    --quiet        Print only the score and reason, without the signal table.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Sequence

COUNT_NOUNS = (
    r"(?:jobs?|positions?|openings?|opportunit(?:y|ies)|vacanc(?:y|ies)"
    r"|roles?|results?)"
)

RESULT_COUNT_RE = re.compile(
    r"(?:"
    r"\b\d[\d,]*\s*(?:\+\s*)?"
    r"(?:open\s+|available\s+|current\s+|matching\s+|total\s+)?"
    + COUNT_NOUNS + r"\b"
    r"|"
    r"\b" + COUNT_NOUNS + r"\s*\(\s*\d[\d,]*\s*\)"
    r")",
    re.IGNORECASE,
)

JOB_HREF_RE = re.compile(
    r"(?:/job/|/jobs/|/job-\d|jobdetail|job_detail|job-detail|/requisition"
    r"|/posting|viewjob|jobid=|job_id=|/opportunity/|/vacancy/|/apply/)",
    re.IGNORECASE,
)

TITLE_NOUN_RE = re.compile(
    r"\b(?:"
    r"engineer|manager|analyst|specialist|director|technician|nurse"
    r"|associate|representative|intern|internship|coordinator|supervisor"
    r"|assistant|developer|designer|accountant|attorney|consultant|architect"
    r"|scientist|operator|driver|clerk|teacher|officer|administrator"
    r"|president|lead|agent|advisor|adviser|counselor|therapist|mechanic"
    r"|welder|planner|buyer|recruiter|controller|auditor|paralegal|cashier"
    r"|server|technologist"
    r"|worker|handler|laborer|labourer|assembler|machinist|fabricator"
    r"|custodian|janitor|packer|picker|loader|forklift|warehouse|stocker"
    r"|cook|chef|baker|housekeeper|attendant|aide|orderly|caregiver"
    r"|dispatcher|installer|inspector|maintenance|millwright|electrician"
    r"|plumber|carpenter|painter|roofer|foreman|crew|apprentice|trainee"
    r"|machine|press|shift"
    r"|physician|surgeon|pharmacist|hygienist|dentist"
    r"|paramedic|phlebotomist|radiologist|sonographer|dietitian|counsellor"
    r"|teller|underwriter|actuary|broker|adjuster|appraiser|bookkeeper"
    r"|strategist|marketer|copywriter|editor|writer|producer"
    r"|photographer|videographer|animator|illustrator"
    r"|scheduler|expeditor|estimator|surveyor|drafter|draftsman"
    r"|programmer|tester|sre|devops|dba|statistician"
    r"|principal|superintendent|librarian|instructor|professor"
    r"|tutor|paraeducator|substitute|coach|trainer|facilitator"
    r"|guard|deputy|firefighter|ranger"
    r"|bartender|barista|host|hostess|waiter|waitress|concierge|valet"
    r"|stylist|barber|esthetician|groomer"
    r"|pilot|conductor|courier|deliverer|chauffeur|trucker"
    r"|vp|chief|head|partner|fellow|volunteer"
    r")\b",
    re.IGNORECASE,
)

LOCATION_RE = re.compile(
    r"\b[A-Z][a-zA-Z.'-]+(?:\s[A-Z][a-zA-Z.'-]+){0,2},\s?(?:[A-Z]{2}\b|[A-Z][a-z]+)"
)

REMOTE_RE = re.compile(r"\b(?:remote|hybrid|on-?site)\b", re.IGNORECASE)

ATS_HOST_RE = re.compile(
    r"(?:myworkdayjobs|workday|ashbyhq|greenhouse\.io|lever\.co|icims|taleo"
    r"|successfactors|oraclecloud|cadienttalent|governmentjobs|edjoin|appone"
    r"|jobvite|smartrecruiters|ultipro|paylocity|workforcenow|brassring"
    r"|silkroad|jazzhr|breezy|recruitee|teamtailor|phenom|avature|eightfold"
    r"|dayforcehcm|paycom|bamboohr|workable)",
    re.IGNORECASE,
)

URL_KEYWORD_RE = re.compile(
    r"(?:/careers?|/jobs?|search-?jobs|job-?search|/openings|/opportunit"
    r"|/vacanc|/join-?us|/work-?with-?us|search-?results)",
    re.IGNORECASE,
)

TITLE_KEYWORD_RE = re.compile(
    r"\b(?:jobs?|careers?|opening|opportunit|vacanc|hiring|work with us"
    r"|join us|employment|carrers)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Signal:
    """One test a page either passes or fails, and what passing is worth.

    `cutoff` is the reading at which the signal starts paying, chosen by
    `benchmark/calibrate.py` as the value that best separated the 20 listings
    pages from the 20 pages that were not.
    """

    name: str
    tier: int
    cutoff: int
    points: int
    describes: str


SIGNALS: tuple[Signal, ...] = (
    Signal("count_phrase", 1, 1, 12, "page states its own job count"),
    Signal("job_hrefs", 1, 1, 10, "links to individual postings"),
    Signal("title_nouns", 1, 8, 10, "job titles as link text"),
    Signal("title_text", 1, 11, 10, "job titles anywhere on the page"),
    Signal("locations", 1, 8, 10, "locations repeated down the page"),
    Signal("title_keyword", 2, 1, 2, "jobs wording in the page title"),
    Signal("url_keyword", 2, 1, 2, "jobs wording in the address"),
    Signal("ats_host", 2, 1, 2, "known applicant tracking system"),
)

MAX_SCORE = sum(signal.points for signal in SIGNALS)

ARRIVED_SCORE = 20

EVIDENCE_LIMIT = 6


@dataclass
class ArrivalScore:
    """The verdict on one page, with the numbers that produced it.

    `readings` is what each signal measured, `points` is what each one paid, and
    `evidence` keeps a few of the strings behind each reading so a score can be
    explained rather than just asserted.
    """

    url: str
    total: int
    readings: dict[str, int] = field(default_factory=dict)
    points: dict[str, int] = field(default_factory=dict)
    evidence: dict[str, list[str]] = field(default_factory=dict)

    @property
    def arrived(self) -> bool:
        """Report whether this page scored high enough to be called a listings
        page on its own. The hop loop does not rely on this, since it keeps the
        best page it saw regardless, but the demo and the benchmark need a plain
        yes or no."""
        return self.total >= ARRIVED_SCORE

    @property
    def fired(self) -> list[str]:
        """Return the names of the signals that earned points, strongest tier
        first. Useful for explaining a score in one line."""
        return [name for name, paid in self.points.items() if paid > 0]

    @property
    def reason(self) -> str:
        """Summarise in one sentence why this page scored what it did, naming
        the signals that fired and a piece of their evidence. Returns a plain
        string suitable for `Hop.reason` and for the demo output."""
        if not self.fired:
            return "no sign of job listings"
        parts = []
        for signal in SIGNALS:
            if self.points.get(signal.name, 0) <= 0:
                continue
            reading = self.readings[signal.name]
            if signal.name in ("title_keyword", "url_keyword", "ats_host"):
                parts.append(signal.describes)
            else:
                parts.append(f"{reading} {signal.describes}")
        return ", ".join(parts)


def visible_text(html: str) -> str:
    """Reduce rendered HTML to roughly the words a person would see, by dropping
    script and style blocks and then every remaining tag. Returns one long
    whitespace-collapsed string; `unescape` turns entities such as `&amp;` back
    into the characters they stand for."""
    without_code = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1\s*>", " ", html)
    without_tags = re.sub(r"(?s)<[^>]+>", " ", without_code)
    return re.sub(r"\s+", " ", unescape(without_tags)).strip()


def measure(
    url: str, title: str, html: str, links: Sequence[tuple[str, str]]
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Run all eight signals over one page and return what each measured along
    with a few of the strings behind it. Takes links as plain `(text, href)`
    pairs rather than any particular class, so the benchmark can pass the same
    data it loaded from JSON."""
    text = visible_text(html)
    readings: dict[str, int] = {}
    evidence: dict[str, list[str]] = {}

    phrases = [match.group(0) for match in RESULT_COUNT_RE.finditer(text)]
    readings["count_phrase"] = len(phrases)
    evidence["count_phrase"] = phrases[:EVIDENCE_LIMIT]

    job_links = [href for _, href in links if JOB_HREF_RE.search(href)]
    readings["job_hrefs"] = len(job_links)
    evidence["job_hrefs"] = job_links[:EVIDENCE_LIMIT]

    titled = [word for word, _ in links if word and TITLE_NOUN_RE.search(word)]
    readings["title_nouns"] = len(titled)
    evidence["title_nouns"] = titled[:EVIDENCE_LIMIT]

    in_text = [match.group(0) for match in TITLE_NOUN_RE.finditer(text)]
    readings["title_text"] = len(in_text)
    evidence["title_text"] = in_text[:EVIDENCE_LIMIT]

    places = [match.group(0) for match in LOCATION_RE.finditer(text)]
    readings["locations"] = len(places) + len(REMOTE_RE.findall(text))
    evidence["locations"] = places[:EVIDENCE_LIMIT]

    title_hit = TITLE_KEYWORD_RE.search(title or "")
    readings["title_keyword"] = 1 if title_hit else 0
    evidence["title_keyword"] = [title_hit.group(0)] if title_hit else []

    url_hit = URL_KEYWORD_RE.search(url or "")
    readings["url_keyword"] = 1 if url_hit else 0
    evidence["url_keyword"] = [url_hit.group(0)] if url_hit else []

    host_hit = ATS_HOST_RE.search(url or "")
    readings["ats_host"] = 1 if host_hit else 0
    evidence["ats_host"] = [host_hit.group(0)] if host_hit else []

    return readings, evidence


def score_parts(
    url: str, title: str, html: str, links: Sequence[tuple[str, str]]
) -> ArrivalScore:
    """Measure a page and turn its readings into points and a total. Each signal
    pays its full value once its reading reaches the cutoff and nothing at all
    below it, which is what stops a page with thousands of rows from outweighing
    a small one that is equally a listings page."""
    readings, evidence = measure(url, title, html, links)
    points = {
        signal.name: signal.points if readings[signal.name] >= signal.cutoff else 0
        for signal in SIGNALS
    }
    return ArrivalScore(
        url=url,
        total=sum(points.values()),
        readings=readings,
        points=points,
        evidence=evidence,
    )


def score(snapshot) -> ArrivalScore:
    """Score a `PageSnapshot` from browser.py, which is how the hop loop calls
    this module. Pulls the `(text, href)` pairs off the snapshot's links and
    hands everything to `score_parts`."""
    links = [(link.text, link.href) for link in snapshot.links]
    return score_parts(snapshot.url, snapshot.title, snapshot.html, links)


def format_report(result: ArrivalScore, quiet: bool = False) -> str:
    """Lay out one score as text, with a row per signal showing what it read,
    what it needed, and what it paid. Returns the whole report as a single
    string so callers can print it or log it."""
    lines = [f"url    : {result.url}"]
    if not quiet:
        lines.append("")
        lines.append(f"  {'signal':<16}{'read':>8}{'cutoff':>8}{'points':>8}   tier")
        lines.append("  " + "-" * 56)
        for signal in SIGNALS:
            paid = result.points[signal.name]
            mark = " " if paid else "."
            lines.append(
                f"{mark} {signal.name:<16}{result.readings[signal.name]:>8}"
                f"{signal.cutoff:>8}{paid:>8}      {signal.tier}"
            )
        lines.append("")
    lines.append(f"score  : {result.total}/{MAX_SCORE}   (arrival at {ARRIVED_SCORE})")
    lines.append(f"verdict: {'LISTINGS PAGE' if result.arrived else 'not a listings page'}")
    lines.append(f"reason : {result.reason}")
    return "\n".join(lines)


def score_cached(sidecar_path: str) -> ArrivalScore:
    """Score a page saved by benchmark/dump_pages.py, given its `.json` sidecar.
    Reads the `.html` sitting beside it, and raises `FileNotFoundError` if
    either file is missing."""
    import json
    from pathlib import Path

    path = Path(sidecar_path)
    meta = json.loads(path.read_text(encoding="utf-8"))
    html = path.with_suffix(".html").read_text(encoding="utf-8", errors="replace")
    links = [(link.get("text", ""), link.get("href", "")) for link in meta.get("links", [])]
    return score_parts(meta.get("final_url", ""), meta.get("title", ""), html, links)


async def score_url(url: str) -> ArrivalScore | None:
    """Render a live URL and score it, returning None if the page cannot be
    loaded at all. Imports browser.py here rather than at the top of the file so
    that scoring cached pages never has to start a browser."""
    from job_source_agent.browser import snapshot as render

    snap = await render(url)
    return score(snap) if snap is not None else None


def main() -> int:
    """Score one page from the command line, live or cached, and print the
    report. Returns 1 when a live page could not be loaded, so a failure is
    visible without reading the output."""
    import argparse

    parser = argparse.ArgumentParser(description="Score a page for job listings.")
    parser.add_argument("url", nargs="?", default=None)
    parser.add_argument("--cached", default=None, help="score a dump_pages .json sidecar")
    parser.add_argument("--quiet", action="store_true", help="omit the signal table")
    args = parser.parse_args()

    if args.cached:
        print(format_report(score_cached(args.cached), args.quiet))
        return 0

    if not args.url:
        parser.error("give a url, or --cached PATH")

    result = asyncio.run(score_url(args.url))
    if result is None:
        print(f"could not load {args.url}")
        return 1
    print(format_report(result, args.quiet))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
