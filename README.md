# Job Source Agent

Give it a LinkedIn job posting. It finds the company's own job board.

```
https://www.linkedin.com/jobs/view/4456337928/
                    ↓
        Honeywell → honeywell.com
                    ↓
https://careers.honeywell.com/en/sites/Honeywell/jobs
```

Two hops, about twenty seconds, and it shows its reasoning at every step.

---

## How it works

Two stages, deliberately separated because they fail in completely different ways.

**Stage 1 — who is this company?** A LinkedIn job page names the employer but not
their website; that only exists on the company profile. So it takes two API
calls, mirroring exactly what a person does by hand: open the posting, click the
company, read the About section.

**Stage 2 — walk to the job board.** Open the company's website in a real
browser, look at every link, pick the one heading toward jobs, follow it, and
repeat until the listings are found. Up to five hops.

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌───────────┐
│ browser  │──▶│ arrival  │──▶│   llm    │──▶│ navigator │
│  render  │   │  score   │   │  choose  │   │   loop    │
└──────────┘   └──────────┘   └──────────┘   └───────────┘
  Playwright     8 signals       Haiku 4.5     best-so-far
```

Each part answers one question. *What is on this page? Is this the job board?
Where should we go next? Should we stop?*

---

## Why not just guess the URL

The obvious approach is to guess: most companies use a hosted job board, so try
`jobs.ashbyhq.com/{company}` and check the title. That was built first, then
deleted. Measured against 20 real companies:

| finding | count |
|---|---|
| Sit on a guessable job board URL | **1 of 20** |
| Render their listings with JavaScript, invisible to a plain fetch | 9 |
| Page title never mentions the company name | 6 |
| Refuse a non-browser client outright (403) | 2 |

Most real employers are on Oracle HCM, Workday, iCIMS, Cadient, GovernmentJobs
or edjoin — hosts whose tenant IDs cannot be derived from a company name.
Honeywell's is `ibqbjb.fa.ocs.oraclecloud.com`.

Title matching cannot rescue it either. Esri's board is titled "IP Global Career
Site", TRL11's says "Carrers", and Workiva's is empty.

Hence: **browser-first, and no guessing.** The list of known job board hosts
survives only as a *recogniser* — "is this URL a job board?" — never as a
generator.

---

## Deciding we have arrived

The hard part isn't navigating. It's knowing when to stop.

Eight independent signals score each page, in two tiers.

**Tier 1 — are there actual job rows here?** This carries the score.

| signal | what it looks for |
|---|---|
| `count_phrase` | the page stating its own count: "204 open positions", "Jobs (448)" |
| `job_hrefs` | links pointing at one specific posting |
| `title_nouns` | job titles as link text — engineer, welder, custodian |
| `title_text` | the same words anywhere on the page |
| `locations` | "City, ST" and Remote/Hybrid repeated down the page |

**Tier 2 — are we even in the right neighbourhood?** Worth little.

`title_keyword`, `url_keyword`, `ats_host`.

**The split is the whole design.** AMETEK's LinkedIn profile points at
`ametek.com/careers` — a careers page with no jobs on it, exactly the page the
agent must not stop on. It scores **full marks on Tier 2 and zero on Tier 1.**
Weight "this page says Careers" heavily and the agent stops there and is wrong.

Every cutoff was measured, not guessed. `benchmark/calibrate.py` scores 20 real
job boards against 20 pages that aren't, and the weights were read off that
table. Two signals were dropped for failing to separate.

---

## Results

Twenty companies, walked from their homepage to their job board.

**16/20 exact URL match. 18/20 verified correct. Zero navigation failures.**

Every one of the twenty reached a page with real job listings on it. The four
strict misses:

| company | returned | verdict |
|---|---|---|
| Honeywell | `careers.honeywell.com/…/jobs` | **correct** — vanity domain over the same Oracle board |
| Realm Alliance | `jobs.ashbyhq.com/realmalliance` | **correct** — arguably better than the recorded answer |
| CarMax | `careers.carmax.com/` | **partial** — lists open positions, but not the full board |
| Reyes | `reyesbeveragegroup.com/careers` | **partial** — shows recent jobs; the portal has 216 |

Median 3 hops. Median 20 seconds per company.

> These twenty are the **development set** — every threshold was tuned against
> them, so quoting a rate from them would be marking my own exam. The reported
> number comes from a fresh, unseen set.

---

## Things that only show up in production

Every one of these was found the hard way and cost real debugging time.

**Real Chrome, not bundled Chromium.** CarMax's CDN answers Chromium with `403
Access Denied` and real Chrome with `200`. Spoofing `navigator.webdriver`,
`plugins` and `window.chrome` changed nothing — only the actual binary works.

**Pages can load successfully and still be empty.** Esri cached once with the
right URL, the right title, and zero jobs, because the site's own background
request failed that one time. Nothing reported an error. A page reading zero on
every Tier 1 signal is now rendered a second time before it is believed.

**Fewer workers was faster *and* more accurate.** At 4 concurrent browsers the
benchmark scored 14/20 in 584s; at 2 it scored 16/20 in 423s. Starved of
resources, pages get snapshotted half-built, the right link is missing from the
list, and the agent wanders — burning its whole hop budget. Three companies
flipped to passing on this alone.

**A regex that was too generous.** `/job-` matched `/job-descriptions`, so a
school district's HR policy pages looked like six job postings. Its homepage
outscored its own job board. Tightening to `/job-\d` cost one true positive and
removed both false ones.

**`--reload` breaks Playwright on Windows.** Uvicorn picks its event loop with
`use_subprocess = reload or workers > 1`, which on Windows swaps in a loop that
cannot spawn subprocesses at all — and Playwright's driver is a subprocess. The
traceback mentions neither uvicorn nor the flag.

---

## Running it

```bash
uv sync
uv run playwright install chrome        # real Chrome, not just chromium
cp .env.example .env                    # add your API keys
```

**The demo**

```bash
uv run uvicorn job_source_agent.api:app     # no --reload on Windows
```

Then open http://127.0.0.1:8000, paste up to 10 LinkedIn job URLs, and watch each
row fill in as its walk finishes.

**One URL, end to end**

```bash
uv run python -m job_source_agent.pipeline https://www.linkedin.com/jobs/view/4456337928/
```

**Free tools** — no credits, useful for seeing inside the agent

```bash
uv run python -m job_source_agent.browser  <url>            # what links a page has
uv run python -m job_source_agent.arrival  <url>            # is this a job board?
uv run python -m job_source_agent.llm      <url>            # how links are ranked
uv run python -m job_source_agent.navigator <company site>  # the full walk, hop by hop
```

**The benchmark**

```bash
uv run python benchmark/run.py              # 20 companies, free
uv run python benchmark/calibrate.py        # how well each signal separates
```

---

## Layout

```
src/job_source_agent/
  linkedin.py    stage 1 — job URL → company + website     the only paid code
  browser.py     render a page, extract every link         Playwright
  arrival.py     is this a job listings page?              owns the signals
  llm.py         rank links, then Haiku picks one          the only LLM call
  navigator.py   the hop loop — when to stop, what to keep
  pipeline.py    stage 1 + stage 2
  api.py         the demo, streamed
  config.py      every tunable in one place
  models.py      what moves between stages

benchmark/
  ground_truth.csv   20 companies, both ends walked by hand
  dump_pages.py      cache 40 rendered pages for offline work
  calibrate.py       measure every signal, positives vs negatives
  run.py             score the agent, report the rate

docs/project_sequence.md   what every file does and why
```

---

## Stack

Python 3.12 · uv · Playwright · FastAPI · Claude Haiku 4.5 · ScrapingDog

Deployed self-hosted behind a Cloudflare Tunnel — the machine dials outward, so
there are no open ports, no port forwarding, and the host's IP never appears in
DNS.

---

Part 2 of the Jobnova AI Engineer take-home.
