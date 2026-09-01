"""Fill the `website` column of ground_truth.csv by running Stage 1.

Costs credits: two ScrapingDog calls per row. Rows that already have a website
are skipped, so re-running is cheap and safe -- only the blanks are paid for.

Results are written back to the CSV after every row. These are paid answers; a
crash on row 15 must not throw away the fourteen already bought.

    uv run python benchmark/fill_websites.py --dry-run   # show what would run
    uv run python benchmark/fill_websites.py             # spend credits
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from job_source_agent.linkedin import LinkedInError, resolve

CSV_PATH = Path(__file__).parent / "ground_truth.csv"
CALLS_PER_ROW = 2


def read_rows() -> tuple[list[dict], list[str]]:
    with CSV_PATH.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def write_rows(rows: list[dict], fieldnames: list[str]) -> None:
    """Rewrite the whole file. Called after every row so nothing paid for is lost."""
    tmp = CSV_PATH.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(CSV_PATH)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    rows, fieldnames = read_rows()

    todo = [r for r in rows if not r.get("website", "").strip()]
    print(f"{len(rows)} rows, {len(todo)} missing a website")
    print(f"cost if run: {len(todo)} x {CALLS_PER_ROW} = {len(todo) * CALLS_PER_ROW} calls\n")

    if dry_run:
        for r in todo:
            print(f"  would resolve  {r['company']:34} {r['linkedin_url']}")
        return 0

    if not todo:
        print("nothing to do")
        return 0

    failures: list[tuple[str, str]] = []
    for i, row in enumerate(todo, 1):
        company = row["company"]
        try:
            identity = resolve(row["linkedin_url"])
        except LinkedInError as exc:
            print(f"[{i}/{len(todo)}] FAILED  {company:34} {exc}")
            failures.append((company, str(exc)))
            continue

        row["website"] = identity.website or ""
        write_rows(rows, fieldnames)

        # LinkedIn's company name and our hand-typed one often disagree, which
        # is worth seeing now: arrival detection cannot lean on the name alone.
        note = ""
        if identity.company_name.lower() != company.lower():
            note = f"   (LinkedIn calls it {identity.company_name!r})"
        print(f"[{i}/{len(todo)}] {company:34} -> {identity.website or '(no website)'}{note}")

    print(f"\ndone. {len(todo) - len(failures)} filled, {len(failures)} failed")
    for company, err in failures:
        print(f"  {company}: {err}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
