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

## Where this falls short, and what I would do next

Everything below is known rather than discovered later. Ordered roughly by how
much it would change the result.

**A job aggregator can still win.** Nothing currently tells the agent that
LinkedIn, Indeed, Glassdoor and the rest are not the company's own site. In one
run it walked from a company's homepage onto that company's LinkedIn page, which
the arrival scorer rated 52 — correctly, in a sense, because a LinkedIn company
page really does list jobs. It is the wrong answer under the brief, and slightly
absurd given we started from a LinkedIn URL. The fix is a shared list of
aggregator hosts, penalised hard in the link ranker so they never reach the
model, and refused outright as a final answer. Small change, and the one I would
make first.

**Two companies stop one hop short.** CarMax and Reyes both land on a careers
page that genuinely lists some jobs, scores well, and so ends the walk — while
the full board is one click further on ("Explore all 216 jobs"). Arrival
detection deliberately treats every signal as yes/no rather than as a quantity,
so that a board with six jobs is not drowned by one with fifteen hundred. That
choice is right in general and costs us here. A tiebreaker between two pages
that both clear arrival, decided on how many jobs each actually lists, would
recover both without reintroducing magnitude into the scoring itself.

**It is slower than it should be.** Around twenty seconds per company, so ten
URLs at two workers is roughly a hundred seconds. Almost none of that is the
model — a hop costs about a thousand input tokens and returns sixty, which is
well under a second. The time is browser time: launching a context, rendering,
and waiting for a JavaScript page to stop changing. The honest ways to attack it
are fewer hops, not faster inference — recognising a known job-board host and
stopping immediately, remembering the answer for a company already resolved, and
raising concurrency on hardware with the memory to support it. Renting the
rendering from a browser service would remove the memory ceiling entirely at the
cost of a per-page fee.

**Measure what the model is actually buying.** The cheap ranker keeps a careers
link in its top thirty on all twenty development companies, and ranks it first
whenever the answer is one hop away. The model earns its place on ties the
ranker cannot break — Honeywell's homepage has five links tied on score and the
right answer sits sixth, in the footer, which the model reached past all five to
find. But "it helps on ties" is an observation from a handful of cases, not a
measurement. A mode that always takes the top-ranked link, run against the same
benchmark, would say exactly what the model is worth. If the gap turns out to be
small, dropping it removes an API dependency, a cost, and a source of
non-determinism from the system. If the gap is large, that is worth knowing too.
Either way it is a free experiment against data already on disk.

**Validate the thresholds honestly.** Every cutoff in the arrival scorer was
chosen by looking at the same twenty companies it is then judged on, which is
fitting and grading on one exam. The fix is not more companies — a hundred
hand-walked sites would have the identical problem at a larger scale. It is
leave-one-out: pick the cutoffs from nineteen, test on the twentieth, repeat
twenty times. Cutoffs that hold steady generalise; cutoffs that swing depending
on which company is dropped were memorising. It runs against cached pages and
costs nothing.

**Collect better negatives, not more positives.** Nineteen of the twenty pages
the scorer is asked to reject are company homepages, which are easy to beat. The
genuinely hard case is a careers landing page with no jobs on it — AMETEK's, for
instance — because that is exactly what the walk must refuse to stop on. More
hand-collected examples of *listings* pages would add little. What would help is
free: every intermediate page the agent walks through during a run is, by
definition, a page that was not the answer, and saving them harvests exactly the
kind of negative the set is short of.

**The benchmark's scoring is too strict.** It compares normalised URLs exactly,
which marks two verified-correct answers wrong: Honeywell's vanity domain in
front of the same Oracle board, and an Ashby board that is arguably a better
answer than the one recorded by hand. Loosening the comparison would hide real
mistakes, so the better fix is an `also_accept` column in the ground truth —
unmatched pairs get printed once for a human to check, and a verified equivalent
is recorded and matches automatically thereafter. A loose matcher's errors are
invisible and inflate the number; a strict matcher's errors are loud and cost a
minute each.

**The demo is one process holding state in memory.** The daily budget lives in a
variable and resets when the app restarts, which is correct for one instance and
wrong for several — each would get its own allowance. Anything beyond a single
box needs that counter in a shared store, and the browser work moved behind a
queue, which is the natural shape for it anyway: rendering is stateless and
embarrassingly parallel.

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

**Guarding a public demo.** The deployed copy is a public URL and every lookup
spends real money, so the part that costs anything asks for a key. Set
`DEMO_ACCESS_KEY` in `.env` and the page still loads for anyone — they can read
what the tool is — but the paste box is replaced by a short key field until the
right key is entered. Sending the link as `…/?key=<key>` unlocks it in one step
and then wipes the key from the address bar, so a screenshot does not leak it.

The key is checked on the server, not just in the page, because the page can be
skipped entirely by posting straight to `/api/resolve`. Leave `DEMO_ACCESS_KEY`
blank and the gate is off, which is what you want locally.

Behind it, `DEMO_DAILY_CREDITS` caps what can be spent in a day, and `NTFY_TOPIC`
pushes one notification to your phone when that cap is reached — once per day,
never more, and a failure to send is swallowed rather than allowed to take the
demo down. See `.env.example` for all three.

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
