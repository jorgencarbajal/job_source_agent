# Benchmark data

## `ground_truth.csv` — 20 rows, hand-collected

Each row was walked by hand: LinkedIn job posting -> company profile -> About ->
company website, then following that site until the job listings appeared. So both
ends of the pipeline are known, and every intermediate answer can be checked.

| column | meaning |
|---|---|
| `company` | company name, as LinkedIn gives it |
| `website` | company homepage — the Stage 2 entry point |
| `linkedin_url` | the job posting the walk started from — the Stage 1 input |
| `listings_url` | the correct answer |
| `hops` | clicks the manual path took, a sanity check on the navigator's hop cap |

`website` is filled for 4 of 20. The other 16 get filled by running Stage 1 once
over `linkedin_url` and caching the result — one credit spend, no manual lookup.

Having both `linkedin_url` and `website` is what makes two different test loops
possible from one file:

- **Stage 2 only** — start at `website`, free, run as often as you like.
- **Full pipeline** — start at `linkedin_url`, costs ScrapingDog credits.

`hops` runs 1 to 3, so a hop cap of 4 covers every case observed with a margin.

## What this data says about the design

Measured by fetching all 20 `listings_url` values with a plain HTTP client and the
title-plus-job-count validator from the earlier prototype:

| finding | count |
|---|---|
| Refuse a plain HTTP client outright (403) | 2 — Gopuff, Leidos |
| Title never names the company | 6 |
| Fewer than 2 job rows in raw HTML | 9 |
| Sit on a slug-guessable ATS | 1 — Binance |

Each line killed a piece of the original approach:

- **Slug guessing is irrelevant.** 1 of 20. The rest are on Oracle HCM, Workday,
  iCIMS, Cadient, GovernmentJobs, edjoin, AppOne, or the company's own domain —
  hosts whose tenant IDs cannot be derived from a company name.
- **Title matching cannot be a gate.** Esri's board is titled "IP Global Career
  Site", TRL11's says "Carrers" (their typo), Workiva's is empty, and IEHP's spells
  out "Inland Empire Health Plan" while LinkedIn calls it "IEHP". Six correct
  answers would have been rejected.
- **Raw HTML is not enough.** Nearly half show under 2 job rows because the
  listings render client-side. Rendering is required, not optional.
- **Some sites need a real browser to respond at all.** Two returned 403.

Hence: browser-first, with arrival detection scored across several signals rather
than gated on any one.

## `urls.txt` — costs credits

The 20 LinkedIn job URLs from `ground_truth.csv`, one per line, for the official
success-rate run. Two ScrapingDog calls each.
