"""Render every ground-truth page once and cache it to disk.

Calibration needs to compare pages that ARE job listings against pages that are
not, and re-rendering forty sites every time a weight changes would make that
loop too slow to use. So this script pays the browser cost once: each row of the
ground truth contributes its `listings_url` as a positive example and its
`website` (the company homepage) as a negative one.

Two files are written per page. The `.html` is the rendered DOM for pattern
hunting. The `.json` carries the final URL, the title, and every link with its
`in_nav` and `in_footer` flags -- those flags are computed inside the browser by
browser.py's extraction script, so they cannot be recovered by re-parsing the
saved HTML later.

Nothing here costs credits. It only drives Chrome.

Running it:

    uv run python benchmark/dump_pages.py [options]

    1. Read benchmark/ground_truth.csv.
    2. Build the list of pages to render, skipping any already cached.
    3. Render them through one shared browser, several at a time.
    4. Write <slug>.html and <slug>.json under benchmark/pages/<kind>/.
    5. Print a summary, naming every page that failed to load.

Options:

    --force          Re-render pages that are already cached. Without this,
                     anything already on disk is left alone, which is what
                     makes a half-finished run cheap to resume.
    --kind KIND      Which set to render: `positive` (the listings pages),
                     `negative` (the homepages), or `both`. Default `both`.
    --only TEXT      Only companies whose name contains TEXT, matched without
                     regard to case. Use it to retry a single flaky site.
    --concurrency N  How many pages to load at the same time. Defaults to
                     config.BROWSER_CONCURRENCY, which is 2. Each one is a
                     real browser context holding real memory, and starving
                     them makes pages snapshot before they finish building.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from dataclasses import asdict
from pathlib import Path

from job_source_agent.browser import BrowserSession, PageSnapshot
from job_source_agent.config import BROWSER_CONCURRENCY

BENCHMARK_DIR = Path(__file__).resolve().parent
GROUND_TRUTH = BENCHMARK_DIR / "ground_truth.csv"
PAGES_DIR = BENCHMARK_DIR / "pages"

KINDS = ("positive", "negative")
COLUMN_FOR_KIND = {"positive": "listings_url", "negative": "website"}

DEFAULT_CONCURRENCY = BROWSER_CONCURRENCY


def slugify(name: str) -> str:
    """Turn a company name into a safe file name, such as `MLK Community
    Healthcare` into `mlk-community-healthcare`. Returns the lowercased name
    with every run of non-alphanumeric characters collapsed into one hyphen,
    which `re.sub` does by replacing each match of that pattern."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def load_rows() -> list[dict[str, str]]:
    """Read the ground truth into a list of dictionaries keyed by column name.
    Raises `FileNotFoundError` if the CSV is missing, since every later step is
    meaningless without it. `csv.DictReader` is what turns each line into a
    dictionary using the header row as the keys."""
    with GROUND_TRUTH.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def html_path(kind: str, slug: str) -> Path:
    """Return the path of the rendered HTML for one page. The slash operator on
    `Path` objects joins path segments, so this builds
    `benchmark/pages/<kind>/<slug>.html`."""
    return PAGES_DIR / kind / f"{slug}.html"


def json_path(kind: str, slug: str) -> Path:
    """Return the path of the metadata sidecar for one page, which sits beside
    the HTML as `benchmark/pages/<kind>/<slug>.json`."""
    return PAGES_DIR / kind / f"{slug}.json"


def is_cached(kind: str, slug: str) -> bool:
    """Report whether both output files for one page already exist. Returns
    False if either is missing, so a run interrupted between the two writes
    renders that page again rather than leaving half a record behind."""
    return html_path(kind, slug).exists() and json_path(kind, slug).exists()


def collect_targets(
    rows: list[dict[str, str]], kind: str, only: str | None, force: bool
) -> list[tuple[str, str, str]]:
    """Decide which pages actually need rendering, as a list of
    `(kind, slug, url)` triples. Returns an empty list when everything asked for
    is already cached, or when `only` matches no company. The trailing comma in
    `(kind,)` is what makes it a one-element tuple rather than just parentheses,
    so the loop below can treat one kind and both kinds the same way."""
    wanted = KINDS if kind == "both" else (kind,)
    targets: list[tuple[str, str, str]] = []

    for row in rows:
        company = row["company"]
        if only and only.lower() not in company.lower():
            continue
        slug = slugify(company)
        for one_kind in wanted:
            url = (row[COLUMN_FOR_KIND[one_kind]] or "").strip()
            if not url:
                continue
            if not force and is_cached(one_kind, slug):
                continue
            targets.append((one_kind, slug, url))

    return targets


def save(kind: str, slug: str, url: str, snap: PageSnapshot) -> None:
    """Write one rendered page to disk as an HTML file and a JSON sidecar. The
    sidecar records the URL that was requested alongside the one actually
    reached, because redirects are common and that difference matters when
    reading results later. `asdict` turns each frozen `Link` dataclass into a
    plain dictionary that `json` knows how to write."""
    html_path(kind, slug).parent.mkdir(parents=True, exist_ok=True)
    html_path(kind, slug).write_text(snap.html, encoding="utf-8")

    payload = {
        "kind": kind,
        "slug": slug,
        "requested_url": url,
        "final_url": snap.url,
        "title": snap.title,
        "html_bytes": len(snap.html),
        "links": [asdict(link) for link in snap.links],
    }
    json_path(kind, slug).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


async def render_one(
    session: BrowserSession,
    limit: asyncio.Semaphore,
    kind: str,
    slug: str,
    url: str,
) -> str | None:
    """Render and save a single page, printing the outcome as it happens.
    Returns None when the page was saved, or a short failure description when it
    was not, so the caller can list every failure at the end. `async with limit`
    waits for a free slot in the semaphore without blocking the other pages,
    which is what keeps only a few browser contexts alive at once."""
    async with limit:
        try:
            snap = await session.snapshot(url)
        except Exception as exc:
            print(f"  ERROR   {kind:8} {slug:36} {type(exc).__name__}: {exc}")
            return f"{kind}/{slug}: {type(exc).__name__}"

        if snap is None:
            print(f"  FAILED  {kind:8} {slug:36} could not load {url}")
            return f"{kind}/{slug}: could not load"

        save(kind, slug, url, snap)
        print(
            f"  ok      {kind:8} {slug:36} "
            f"{len(snap.html):>9,} bytes  {len(snap.links):>4} links"
        )
        return None


async def dump(targets: list[tuple[str, str, str]], concurrency: int) -> list[str]:
    """Render every target through one shared browser and return the list of
    failure descriptions, which is empty when all of them succeeded. One
    `BrowserSession` is reused for the whole run so Chrome starts once rather
    than once per page, and `asyncio.gather` runs the page loads concurrently
    while the semaphore caps how many happen at a time."""
    session = BrowserSession()
    limit = asyncio.Semaphore(concurrency)
    try:
        results = await asyncio.gather(
            *(render_one(session, limit, *target) for target in targets)
        )
    finally:
        await session.close()
    return [failure for failure in results if failure is not None]


def parse_args() -> argparse.Namespace:
    """Define and read the command line options described at the top of this
    file. Returns a namespace whose attributes are the option names, so
    `--kind positive` arrives as `args.kind`."""
    parser = argparse.ArgumentParser(description="Cache rendered ground-truth pages.")
    parser.add_argument("--force", action="store_true", help="re-render cached pages")
    parser.add_argument("--kind", choices=("positive", "negative", "both"), default="both")
    parser.add_argument("--only", default=None, help="substring of a company name")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    return parser.parse_args()


def main() -> int:
    """Run the whole dump and return the process exit code, which is 1 when any
    page failed so a bad run is visible without reading the output. Failures are
    printed by name because the fix is usually to rerun just those with
    `--only`."""
    args = parse_args()
    rows = load_rows()
    targets = collect_targets(rows, args.kind, args.only, args.force)

    if not targets:
        print("Nothing to render. Everything asked for is already cached.")
        return 0

    print(f"Rendering {len(targets)} pages into {PAGES_DIR} ...")
    failures = asyncio.run(dump(targets, args.concurrency))

    saved = len(targets) - len(failures)
    print(f"\nSaved {saved}/{len(targets)} pages.")
    if failures:
        print("Failed:")
        for failure in failures:
            print(f"  {failure}")
        print("\nRerun just these with --only <company> --force")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
