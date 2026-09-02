"""Run the agent against the ground truth and report how often it was right.

This is the number the take-home asks for. Two modes, because the two halves of
the system cost very different amounts to test.

Stage 2 only starts at each company's `website` column and walks to the
listings. It never touches ScrapingDog, so it is free apart from a few cents of
model calls, and it exercises everything that is actually uncertain -- the link
ranker, the model's choices, the arrival score, the hop budget. Run it as often
as you like.

The full pipeline starts at each `linkedin_url` and pays for stage 1 first. That
is the number to report, but stage 1 is deterministic and already verified, so
paying to re-run it on every experiment tests nothing new. Run it once at the
end.

Answers are compared strictly: scheme, `www.`, trailing slash, query and
fragment are normalised away, and then the host and path must match. Nothing
looser, because a permissive comparison fails silently -- it credits a wrong page
and inflates the result -- while a strict one fails loudly and prints the pair so
it can be checked by hand. Two known consequences: a company reachable through a
vanity domain counts as a miss even when the page is right, so the reported rate
is a floor rather than the true rate.

Every row is written to disk the moment it finishes, so a crash halfway through
a paid run never discards what was already bought.

Running it:

    uv run python benchmark/run.py [options]

    1. Read benchmark/ground_truth.csv.
    2. Walk each company, several at a time, sharing one browser.
    3. Append each result to the output CSV as it lands.
    4. Print a table of hits and misses, then the success rate.

Options:

    --full           Start from the LinkedIn URLs and pay for stage 1. Without
                     this the run starts from the website column and is free.
    --dry-run        List what would be run and spend nothing.
    --only TEXT      Only companies whose name contains TEXT, ignoring case.
    --limit N        Stop after the first N companies. Useful for a quick check.
    --concurrency N  How many companies to process at once. Default comes
                     from config.BROWSER_CONCURRENCY, which is 2. Raising it
                     made the run both slower and less accurate: at 4 the
                     pages snapshot half-built and the walks wander.
    --max-hops N     Hop budget per website. Default is config.MAX_HOPS.
    --out PATH       Where to write the results CSV. Defaults to a name based
                     on the mode, beside this script.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from job_source_agent import pipeline
from job_source_agent.browser import BrowserSession
from job_source_agent.config import BROWSER_CONCURRENCY, MAX_HOPS
from job_source_agent.navigator import walk

BENCHMARK_DIR = Path(__file__).resolve().parent
GROUND_TRUTH = BENCHMARK_DIR / "ground_truth.csv"

DEFAULT_CONCURRENCY = BROWSER_CONCURRENCY
CREDITS_PER_URL = 55


@dataclass
class Outcome:
    """What the agent produced for one company, and whether it was right."""

    company: str
    started_from: str
    expected: str
    got: str | None
    hit: bool
    score: int
    hops: int
    outcome: str
    seconds: float


def canonical(url: str) -> str:
    """Reduce a URL to the form two answers must share to count as the same
    page: lowercased host without `www.`, plus the path without a trailing
    slash. Query and fragment are dropped, because redirects bolt tracking
    parameters onto an otherwise correct answer."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/").lower()
    return f"{host}{path}"


def is_hit(expected: str, got: str | None) -> bool:
    """Report whether the agent's answer matches the answer key. Returns False
    when the agent found nothing at all, so a missing answer is never counted as
    a match against an empty expectation."""
    if not got or not expected:
        return False
    return canonical(expected) == canonical(got)


def load_rows(only: str | None, limit: int | None) -> list[dict[str, str]]:
    """Read the ground truth, keeping only the companies asked for. Raises
    `SystemExit` when the filter matches nothing, since an empty run looks like
    a pass otherwise."""
    with GROUND_TRUTH.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if only:
        rows = [row for row in rows if only.lower() in row["company"].lower()]
    if limit:
        rows = rows[:limit]
    if not rows:
        raise SystemExit("No companies matched. Check --only.")
    return rows


async def one_company(
    row: dict[str, str], session: BrowserSession, full: bool, max_hops: int
) -> Outcome:
    """Run a single company end to end and judge the answer. Uses the pipeline
    when `full` is set, which pays for stage 1, and otherwise starts at the
    website column and walks for free."""
    expected = row["listings_url"].strip()
    started = time.monotonic()

    if full:
        started_from = row["linkedin_url"].strip()
        result = await pipeline.run(started_from, session=session, max_hops=max_hops)
        got, outcome, hops = result.listings_url, result.outcome, len(result.hops)
        score = result.hops[-1].score if result.hops else 0
    else:
        started_from = row["website"].strip()
        walked = await walk(started_from, session=session, max_hops=max_hops)
        got = walked.listings_url if walked.ok else None
        outcome, hops, score = walked.outcome, len(walked.hops), walked.score

    return Outcome(
        company=row["company"],
        started_from=started_from,
        expected=expected,
        got=got,
        hit=is_hit(expected, got),
        score=score,
        hops=hops,
        outcome=outcome,
        seconds=time.monotonic() - started,
    )


async def run_all(
    rows: list[dict[str, str]],
    full: bool,
    concurrency: int,
    max_hops: int,
    out_path: Path,
) -> list[Outcome]:
    """Run every company, writing each result as it lands and returning them
    all. The output file is opened for the whole run and flushed per row, so a
    crash keeps everything already finished -- which matters most in `--full`,
    where those rows cost credits."""
    session = BrowserSession()
    limit = asyncio.Semaphore(concurrency)
    results: list[Outcome] = []

    async def guarded(row: dict[str, str]) -> Outcome:
        """Run one company, turning any unexpected failure into a recorded miss.
        A crashed browser driver takes down whatever it was serving, and one
        company's exception must not end a run that has already paid for the
        rows before it."""
        async with limit:
            try:
                return await one_company(row, session, full, max_hops)
            except Exception as exc:
                return Outcome(
                    company=row["company"],
                    started_from=row["linkedin_url" if full else "website"].strip(),
                    expected=row["listings_url"].strip(),
                    got=None,
                    hit=False,
                    score=0,
                    hops=0,
                    outcome=f"error: {type(exc).__name__}: {exc}"[:160],
                    seconds=0.0,
                )

    tasks = [asyncio.create_task(guarded(row)) for row in rows]

    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["company", "hit", "started_from", "expected", "got", "score", "hops", "outcome", "seconds"]
        )
        try:
            for finished in asyncio.as_completed(tasks):
                result = await finished
                results.append(result)
                writer.writerow(
                    [
                        result.company,
                        "hit" if result.hit else "miss",
                        result.started_from,
                        result.expected,
                        result.got or "",
                        result.score,
                        result.hops,
                        result.outcome,
                        f"{result.seconds:.1f}",
                    ]
                )
                handle.flush()
                mark = "hit " if result.hit else "MISS"
                print(f"  {mark}  {result.company[:34]:36}{result.seconds:>6.1f}s  {result.outcome}")
        finally:
            for task in tasks:
                task.cancel()
            await session.close()

    return results


def print_report(results: list[Outcome], out_path: Path) -> None:
    """Print the misses in full and then the success rate. Hits are summarised
    rather than listed, because the only rows worth reading line by line are the
    ones that went wrong."""
    if not results:
        print("\nNo companies completed. Nothing to report.")
        return

    hits = [r for r in results if r.hit]
    misses = [r for r in results if not r.hit]

    if misses:
        print(f"\nMisses ({len(misses)})\n" + "-" * 78)
        for result in sorted(misses, key=lambda r: r.company):
            print(f"\n{result.company}   [{result.outcome}, {result.hops} hops, score {result.score}]")
            print(f"  expected  {result.expected}")
            print(f"  got       {result.got or '(nothing)'}")

    total = len(results)
    rate = 100.0 * len(hits) / total if total else 0.0
    seconds = sum(r.seconds for r in results)

    print(f"\n{'=' * 78}")
    print(f"Success rate: {len(hits)}/{total}  ({rate:.0f}%)")
    print(f"Hops: median {sorted(r.hops for r in results)[total // 2]}, max {max(r.hops for r in results)}")
    print(f"Wall time {seconds:.0f}s of work across {total} companies.")
    print(f"Full results in {out_path}")


def parse_args() -> argparse.Namespace:
    """Define and read the command line options described at the top of this
    file. Returns a namespace whose attributes are the option names."""
    parser = argparse.ArgumentParser(description="Score the agent against the ground truth.")
    parser.add_argument("--full", action="store_true", help="start from LinkedIn URLs (costs credits)")
    parser.add_argument("--dry-run", action="store_true", help="list the plan, spend nothing")
    parser.add_argument("--only", default=None, help="substring of a company name")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--max-hops", type=int, default=MAX_HOPS)
    parser.add_argument("--out", default=None)
    return parser.parse_args()


def main() -> int:
    """Run the benchmark and print the report. Returns 0 when every company was
    a hit and 1 otherwise, so a regression is visible from the exit code."""
    args = parse_args()
    rows = load_rows(args.only, args.limit)
    mode = "full pipeline" if args.full else "stage 2 only"
    out_path = Path(args.out) if args.out else BENCHMARK_DIR / (
        "results_full.csv" if args.full else "results_stage2.csv"
    )

    cost = (
        f"{len(rows) * CREDITS_PER_URL} ScrapingDog credits (~{len(rows) * 2.2:.0f}c)"
        if args.full
        else "no credits"
    )
    print(f"{len(rows)} companies, {mode}, {cost} plus ~{len(rows) * 0.4:.0f}c of model calls.\n")

    if args.dry_run:
        column = "linkedin_url" if args.full else "website"
        for row in rows:
            print(f"  {row['company'][:34]:36}{row[column]}")
        print("\n--dry-run, nothing spent.")
        return 0

    results = asyncio.run(
        run_all(rows, args.full, args.concurrency, args.max_hops, out_path)
    )
    print_report(results, out_path)
    return 0 if all(r.hit for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
