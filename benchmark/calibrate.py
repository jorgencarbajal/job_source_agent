"""Measure every candidate arrival signal against the cached pages.

arrival.py has to answer one question: is this page a list of open jobs? This
script is how that question stops being a guess. It runs each candidate signal
over the pages cached by dump_pages.py -- the twenty `listings_url` pages as
positives, the twenty `website` pages as negatives -- and prints what each
signal actually measured on both sets.

The point is separation. A signal earns its weight only if positives score high
on it and negatives score low. Anything that scores alike on both is noise, and
seeing that here is cheaper than discovering it later inside the hop loop.

For each numeric signal the summary also proposes a cutoff, chosen as the value
that splits the two sets most cleanly, along with how many pages on each side
land above it. Those numbers are what the weights in arrival.py should be read
off; nothing here writes code or decides anything on its own.

Reads only from disk, so it costs nothing and reruns in about a second.

Running it:

    uv run python benchmark/calibrate.py [options]

    1. Load every cached page from benchmark/pages/positive and .../negative.
    2. Score all signals on each page.
    3. Print a per-company table of the raw numbers, positives above negatives.
    4. Print a per-signal summary with the suggested cutoff and its split.
    5. Name any positive that shows no job evidence at all, which usually means
       the page failed to load its jobs rather than that it has none.

Options:

    --detail SIGNAL  Print what one signal actually matched on every page --
                     the phrases, hrefs or link texts behind the number. This
                     is how you check a signal is measuring what you think.
    --csv PATH       Also write the raw per-page numbers to a CSV, for sorting
                     and eyeballing outside the terminal.
    --pages DIR      Read the cache from somewhere other than benchmark/pages.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

BENCHMARK_DIR = Path(__file__).resolve().parent
DEFAULT_PAGES_DIR = BENCHMARK_DIR / "pages"

KINDS = ("positive", "negative")

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
    r"(?:/job/|/jobs/|/job-|jobdetail|job_detail|job-detail|/requisition"
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
    r"|technician|machine|press|forklift|shift"
    r"|physician|surgeon|pharmacist|therapist|technician|hygienist|dentist"
    r"|paramedic|phlebotomist|radiologist|sonographer|dietitian|counsellor"
    r"|teller|underwriter|actuary|broker|adjuster|appraiser|bookkeeper"
    r"|analyst|strategist|marketer|copywriter|editor|writer|producer"
    r"|photographer|videographer|animator|illustrator"
    r"|scheduler|dispatcher|expeditor|estimator|surveyor|drafter|draftsman"
    r"|programmer|tester|sre|devops|administrator|dba|statistician"
    r"|principal|superintendent|counselor|librarian|instructor|professor"
    r"|tutor|paraeducator|substitute|coach|trainer|facilitator"
    r"|guard|deputy|dispatcher|firefighter|ranger|inspector"
    r"|bartender|barista|host|hostess|waiter|waitress|concierge|valet"
    r"|stylist|barber|esthetician|groomer"
    r"|pilot|conductor|courier|deliverer|chauffeur|trucker"
    r"|vp|chief|head|partner|fellow|apprentice|volunteer"
    r")\b",
    re.IGNORECASE,
)

LOCATION_RE = re.compile(
    r"\b[A-Z][a-zA-Z.'-]+(?:\s[A-Z][a-zA-Z.'-]+){0,2},\s?(?:[A-Z]{2}\b|[A-Z][a-z]+)"
)

REMOTE_RE = re.compile(r"\b(?:remote|hybrid|on-?site)\b", re.IGNORECASE)

PAGINATION_HREF_RE = re.compile(r"(?:[?&](?:page|pg|p|from|startrow)=|/page/)", re.IGNORECASE)
PAGINATION_TEXT_RE = re.compile(r"^(?:next|next page|prev|previous|\d{1,3}|»|«|>|<)$", re.IGNORECASE)

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

SIGNAL_NAMES = (
    "count_phrase",
    "job_hrefs",
    "href_family",
    "title_nouns",
    "title_text",
    "locations",
    "pagination",
    "ats_host",
    "url_keyword",
    "title_keyword",
)

TIER_ONE = (
    "count_phrase",
    "job_hrefs",
    "title_nouns",
    "title_text",
    "locations",
)

SUSPECT_CEILING = 2


@dataclass
class Page:
    """One cached page, holding both files dump_pages.py wrote for it."""

    kind: str
    slug: str
    final_url: str
    title: str
    html: str
    links: list[dict]


@dataclass
class Scores:
    """What every signal measured on one page, plus the evidence behind it.

    `values` is the number each signal produced, and `evidence` keeps the actual
    matched strings so `--detail` can show what a number was built from.
    """

    page: Page
    values: dict[str, int] = field(default_factory=dict)
    evidence: dict[str, list[str]] = field(default_factory=dict)


def load_pages(pages_dir: Path) -> list[Page]:
    """Load every cached page from both kind folders into `Page` objects.
    Returns them sorted by slug then kind so each company's two pages sit
    together, and raises `SystemExit` if the cache is missing entirely, since
    that means dump_pages.py has not been run."""
    pages: list[Page] = []
    for kind in KINDS:
        for sidecar in sorted((pages_dir / kind).glob("*.json")):
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            html_file = sidecar.with_suffix(".html")
            pages.append(
                Page(
                    kind=kind,
                    slug=meta["slug"],
                    final_url=meta.get("final_url", ""),
                    title=meta.get("title", ""),
                    html=html_file.read_text(encoding="utf-8", errors="replace"),
                    links=meta.get("links", []),
                )
            )
    if not pages:
        raise SystemExit(f"No cached pages in {pages_dir}. Run dump_pages.py first.")
    return sorted(pages, key=lambda page: (page.slug, page.kind))


def visible_text(html: str) -> str:
    """Reduce rendered HTML to roughly the text a person would see, by dropping
    script and style blocks and then every remaining tag. Returns one long
    whitespace-collapsed string; `unescape` turns entities such as `&amp;` back
    into their real characters."""
    without_code = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1\s*>", " ", html)
    without_tags = re.sub(r"(?s)<[^>]+>", " ", without_code)
    return re.sub(r"\s+", " ", unescape(without_tags)).strip()


def href_shape(href: str) -> str:
    """Collapse a URL into the pattern it belongs to, so that `/job/12345` and
    `/job/67890` become the same string. Returns the host plus a path whose
    identifier-looking segments are replaced by `#`, where a segment counts as
    an identifier if it holds a digit or two or more hyphens."""
    parts = urlsplit(href)
    segments = []
    for segment in parts.path.split("/"):
        if any(char.isdigit() for char in segment) or segment.count("-") >= 2:
            segments.append("#")
        else:
            segments.append(segment.lower())
    return parts.netloc.lower() + "/".join(segments)


def score_page(page: Page) -> Scores:
    """Run every signal over one page and return the numbers with their
    evidence. Each signal is deliberately independent of the others so the
    summary can judge them one at a time."""
    text = visible_text(page.html)
    result = Scores(page=page)

    counts = RESULT_COUNT_RE.findall(text)
    phrases = [match.group(0) for match in RESULT_COUNT_RE.finditer(text)]
    result.values["count_phrase"] = len(counts)
    result.evidence["count_phrase"] = phrases[:8]

    job_links = [link for link in page.links if JOB_HREF_RE.search(link.get("href", ""))]
    result.values["job_hrefs"] = len(job_links)
    result.evidence["job_hrefs"] = [link["href"] for link in job_links[:8]]

    families: dict[str, int] = {}
    for link in page.links:
        shape = href_shape(link.get("href", ""))
        families[shape] = families.get(shape, 0) + 1
    biggest = max(families.items(), key=lambda item: item[1], default=("", 0))
    result.values["href_family"] = biggest[1]
    result.evidence["href_family"] = [f"{biggest[1]}x  {biggest[0]}"]

    titled = [
        link
        for link in page.links
        if link.get("text") and TITLE_NOUN_RE.search(link["text"])
    ]
    result.values["title_nouns"] = len(titled)
    result.evidence["title_nouns"] = [link["text"] for link in titled[:8]]

    in_text = TITLE_NOUN_RE.findall(text)
    result.values["title_text"] = len(in_text)
    result.evidence["title_text"] = [
        match.group(0) for match in TITLE_NOUN_RE.finditer(text)
    ][:8]

    places = LOCATION_RE.findall(text)
    result.values["locations"] = len(places) + len(REMOTE_RE.findall(text))
    result.evidence["locations"] = [match.group(0) for match in LOCATION_RE.finditer(text)][:8]

    paged = [
        link
        for link in page.links
        if PAGINATION_HREF_RE.search(link.get("href", ""))
        or PAGINATION_TEXT_RE.match((link.get("text") or "").strip())
    ]
    result.values["pagination"] = len(paged)
    result.evidence["pagination"] = [
        f"{link.get('text', '')!r} -> {link.get('href', '')}" for link in paged[:8]
    ]

    host_hit = ATS_HOST_RE.search(page.final_url)
    result.values["ats_host"] = 1 if host_hit else 0
    result.evidence["ats_host"] = [host_hit.group(0)] if host_hit else []

    url_hit = URL_KEYWORD_RE.search(page.final_url)
    result.values["url_keyword"] = 1 if url_hit else 0
    result.evidence["url_keyword"] = [url_hit.group(0)] if url_hit else []

    title_hit = TITLE_KEYWORD_RE.search(page.title)
    result.values["title_keyword"] = 1 if title_hit else 0
    result.evidence["title_keyword"] = [title_hit.group(0)] if title_hit else []

    return result


def suggest_cutoff(positives: list[int], negatives: list[int]) -> tuple[int, int, int]:
    """Find the threshold that separates the two sets best, returning it with
    how many positives and negatives sit at or above it. The best threshold is
    the one maximising positives kept minus negatives let through, so a signal
    that cannot separate at all reports a cutoff that keeps almost nothing."""
    candidates = sorted(set(positives + negatives + [1]))
    best = (1, 0, len(negatives))
    best_gap = -10**9
    for cutoff in candidates:
        kept = sum(1 for value in positives if value >= cutoff)
        leaked = sum(1 for value in negatives if value >= cutoff)
        gap = kept - leaked
        if gap > best_gap:
            best_gap = gap
            best = (cutoff, kept, leaked)
    return best


def print_suspects(scored: list[Scores]) -> None:
    """Name any positive page that shows no evidence of job rows at all, since a
    listings page scoring zero on every Tier 1 signal is far more likely to be a
    bad capture than a real result. Prints nothing when every positive looks
    plausible, and prints the command that re-renders each suspect."""
    suspects = [
        score
        for score in scored
        if score.page.kind == "positive"
        and all(score.values[name] <= SUSPECT_CEILING for name in TIER_ONE)
    ]
    if not suspects:
        return

    print("\nSUSPECT CAPTURES -- positives with no job evidence at all")
    print("-" * 88)
    for score in suspects:
        readings = "  ".join(f"{name}={score.values[name]}" for name in TIER_ONE)
        print(f"  {score.page.slug}\n      {readings}")
        print(
            f"      re-render: uv run python benchmark/dump_pages.py "
            f'--only "{score.page.slug}" --kind positive --force'
        )
    print(
        "\n  A page can read zero because the site failed to load its jobs that "
        "one time,\n  or because the ground-truth URL points at a careers page "
        "rather than a listing.\n  Re-render first; if the numbers do not move, "
        "check the URL by hand."
    )


def print_table(scored: list[Scores]) -> None:
    """Print the raw numbers for every page, each company's positive row above
    its negative row. The columns are the signal names abbreviated to keep the
    table inside a normal terminal width."""
    headers = ["company", "kind"] + [name[:9] for name in SIGNAL_NAMES]
    print(f"{headers[0]:<30}{headers[1]:<10}" + "".join(f"{h:>11}" for h in headers[2:]))
    print("-" * (40 + 11 * len(SIGNAL_NAMES)))
    for score in scored:
        row = "".join(f"{score.values[name]:>11}" for name in SIGNAL_NAMES)
        print(f"{score.page.slug[:29]:<30}{score.page.kind:<10}{row}")


def print_summary(scored: list[Scores]) -> None:
    """Print, for each signal, how positives and negatives compare and where the
    cleanest cutoff falls. `statistics.median` is used rather than the mean
    because one enormous page should not drag a whole signal's picture with
    it."""
    positives = [s for s in scored if s.page.kind == "positive"]
    negatives = [s for s in scored if s.page.kind == "negative"]

    print(f"\n{'signal':<16}{'pos median':>12}{'neg median':>12}{'cutoff':>9}"
          f"{'pos>=':>8}{'neg>=':>8}   verdict")
    print("-" * 88)

    for name in SIGNAL_NAMES:
        pos = [s.values[name] for s in positives]
        neg = [s.values[name] for s in negatives]
        cutoff, kept, leaked = suggest_cutoff(pos, neg)
        gap = kept - leaked
        if gap >= 14:
            verdict = "strong"
        elif gap >= 8:
            verdict = "useful"
        elif gap >= 4:
            verdict = "weak"
        else:
            verdict = "noise -- drop it"
        print(
            f"{name:<16}{statistics.median(pos):>12}{statistics.median(neg):>12}"
            f"{cutoff:>9}{kept:>8}/{len(pos):<3}{leaked:>4}/{len(neg):<3} {verdict}"
        )


def print_detail(scored: list[Scores], signal: str) -> None:
    """Show what one signal actually matched on every page, so a suspiciously
    high or low number can be traced to real strings. Raises `SystemExit` if the
    signal name is not one that was measured."""
    if signal not in SIGNAL_NAMES:
        raise SystemExit(f"Unknown signal {signal!r}. Choose from: {', '.join(SIGNAL_NAMES)}")
    print(f"\nWhat `{signal}` matched\n" + "=" * 60)
    for score in scored:
        print(f"\n{score.page.slug}  [{score.page.kind}]  = {score.values[signal]}")
        for item in score.evidence.get(signal, []) or ["(nothing)"]:
            print(f"    {item}")


def write_csv(scored: list[Scores], path: Path) -> None:
    """Write the same numbers as the table into a CSV for sorting elsewhere.
    Returns nothing; the file is overwritten if it already exists."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["company", "kind", "final_url", *SIGNAL_NAMES])
        for score in scored:
            writer.writerow(
                [
                    score.page.slug,
                    score.page.kind,
                    score.page.final_url,
                    *(score.values[name] for name in SIGNAL_NAMES),
                ]
            )
    print(f"\nWrote {path}")


def parse_args() -> argparse.Namespace:
    """Define and read the command line options described at the top of this
    file. Returns a namespace whose attributes are the option names."""
    parser = argparse.ArgumentParser(description="Measure arrival signals on cached pages.")
    parser.add_argument("--detail", default=None, help="show what one signal matched")
    parser.add_argument("--csv", default=None, help="also write raw numbers here")
    parser.add_argument("--pages", default=None, help="cache directory to read")
    return parser.parse_args()


def main() -> int:
    """Load the cache, score every page, and print the tables. Returns 0 always,
    because this script measures rather than judges -- deciding a signal is bad
    is your call, not its exit code."""
    args = parse_args()
    pages_dir = Path(args.pages) if args.pages else DEFAULT_PAGES_DIR
    pages = load_pages(pages_dir)
    scored = [score_page(page) for page in pages]

    print(f"Scored {len(scored)} pages from {pages_dir}\n")
    print_table(scored)
    print_summary(scored)
    print_suspects(scored)

    if args.detail:
        print_detail(scored, args.detail)
    if args.csv:
        write_csv(scored, Path(args.csv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
