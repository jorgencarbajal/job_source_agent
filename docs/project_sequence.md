## Signals

The nine things `arrival.py` looks at to decide whether a page is a list of open
jobs. Measured against the ground truth by `benchmark/calibrate.py`; the counts
in brackets are how many of the 20 positives and 20 negatives scored above the
best cutoff.

**Tier 1 — is this page actually showing job rows?** These carry the score.

- `count_phrase` [17/20 vs 0/20] — the page stating its own job count: "204 open positions", "1-25 of 1,203 jobs", or Oracle's "Jobs (448)". A page only announces how many jobs it has when it is showing them, which is why no negative scored above zero.
- `job_hrefs` [15/20 vs 2/20] — links whose URL points at one specific posting (`/job/12345`, `jobid=`, `/requisition`). A list of jobs is built out of links to individual jobs, so this counts the rows through their destinations.
- `title_nouns` [15/20 vs 1/20] — links whose *text* is a job title (engineer, welder, nurse, forklift, custodian). The precise version of "are there rows here", since on most boards the clickable text is the job name. Goes blind when a board puts the title outside the anchor.
- `title_text` [19/20 vs 3/20] — the same job-title words anywhere in the visible page text rather than only inside links. This is what rescues Oracle HCM boards like Honeywell and International Paper, where the titles sit next to the links instead of inside them.
- `locations` [19/20 vs 4/20] — "City, ST" patterns plus Remote/Hybrid/On-site. Job rows almost always carry a location beside the title, so many locations means many rows. Leaks on civic and retail homepages, which list locations too.

**Tier 2 — are we even in the right neighbourhood?** Small points. These break
ties and keep the navigator pointed the right way; they can never decide arrival
on their own.

- `title_keyword` [19/20 vs 1/20] — job/career/opening/hiring words in the page `<title>`. Confirms the page is jobs-related, says nothing about whether jobs are on it.
- `url_keyword` [17/20 vs 1/20] — `/careers`, `/jobs`, `/search-jobs`, `/openings` in the final URL. Same purpose and same limitation, read off the address instead of the page.
- `ats_host` [8/20 vs 0/20] — the URL sits on a known applicant tracking system (Workday, Ashby, Oracle HCM, iCIMS, GovernmentJobs, edjoin). Recognises that you have landed on a job board. Never used to guess a URL, and it cannot tell a board's front door from its actual listing.

**Why two tiers.** AMETEK's LinkedIn profile points at `ametek.com/careers`, a
careers page with no jobs on it. It scores 1 on both Tier 2 signals and near
zero on every Tier 1 signal — exactly the page the hop loop must not stop on.
That split is the whole design.

## src/job_source_agent/config.py

- A simple configuration file

## src/job_source_agent/models.py

- Holds data classes. `CompanyIdentity` is what stage 1 gets out of the linked in job posting URL. `Hop` keeps track of the path the LLM (crawler) takes through the browser and also notes reasoning. `JobSourceResult` contains the final outcome of the run.

## src/job_source_agent/linkedin.py

- Stage 1, and the only code that spends credits. Turns a LinkedIn job posting URL into the company behind it, website included.
- The default path is two calls, because ScrapingDog scrapes one page per call and the website only lives on the company profile. We extract the job id from the URL and send it to `SCRAPINGDOG_JOB_URL`, which returns the company name and a profile link; we parse the slug out of that link and send it to `SCRAPINGDOG_PROFILE_URL`, which returns the website.
- Run as a standalone file for a quick check, or import `resolve()`, which is what the rest of the project uses. `--slug` skips the first call when you already know the slug, halving the cost; `--raw` dumps every field both calls returned, for debugging.

## src/job_source_agent/browser.py

- The eyes of stage 2. Loads one URL in a real browser and hands back a `PageSnapshot`: the final URL after redirects, the title, the rendered HTML, and every link as text plus href. It filters nothing, because choosing a link is `llm.py`'s job and deciding we have arrived is `arrival.py`'s.
- A plain HTTP fetch was not enough. Nearly half the ground-truth boards build their listings with JavaScript, and two refuse non-browser clients outright, so pages have to be rendered rather than downloaded.
- `BrowserSession` keeps one Chrome running and opens a fresh context per page, so the hop loop pays the browser startup cost once instead of once per hop. The module-level `snapshot()` is the throwaway version for one-off calls and the CLI.
- Four things it does that are not obvious, each learned from a site that broke without them: it launches real Chrome rather than bundled Chromium (CarMax's CDN returns 403 to Chromium), it retries with a progressively looser wait (Honeywell's CDN fails intermittently), it waits for a link to exist before reading the page (a committed page has no anchors yet), and it adds a scheme to bare domains (`page.goto()` rejects them outright).
- Run as a standalone file against any URL to see what it extracts. `--all` prints every link instead of the first 25.

## src/job_source_agent/arrival.py

- The thing that ends the hop loop. `navigator.py` follows links until it runs out of budget; this module is what tells it which of the pages it saw was the answer.
- Takes a `PageSnapshot` from `browser.py` and returns an `ArrivalScore` — a total, what each signal read, what each one paid, and a one-line reason that feeds `Hop.reason` and the demo output.
- Nothing here is a gate. Eight signals each vote and the votes are added, because no single test survives real career sites: titles lie ("IP Global Career Site", "Carrers"), job boards sit on hosts whose names mean nothing, and half of them build their listings with JavaScript.
- Each signal is yes-or-no, not a quantity. A signal pays its points once it clears its cutoff and how far past it landed is discarded. Gopuff shows 1,523 locations and MLK shows 6, and both are listings pages — letting magnitude into the score would drown the small one.
- The cutoffs come from `benchmark/calibrate.py`, measured against the 20 hand-walked companies. This file **owns** the regexes and the benchmark imports them, so calibration can never drift into measuring a pattern the agent no longer uses.
- Measured across all 40 cached pages: 20/20 listings pages clear the arrival score, 1/20 homepages do, and no company's homepage outscores its own board. The score is a comparator within one company rather than a global classifier, which is why the loop keeps the best page it saw instead of stopping at a threshold.
- Run as a standalone file to see the full breakdown for any page. `--cached PATH` scores a page already saved by `dump_pages.py` (free and instant, give it the `.json`), and `--quiet` prints just the score and reason without the signal table.

## benchmark/dump_pages.py

- Renders all 40 ground-truth pages once and caches them to disk, so the signal work can be done against real pages without waiting on a browser every time. Each CSV row contributes its `listings_url` as a positive example and its `website` as a negative one.
- Writes two files per page into `benchmark/pages/<kind>/`. The `.html` is the rendered DOM. The `.json` holds the final URL, the title, and every link with its `in_nav` and `in_footer` flags — those flags are computed inside the browser, so they cannot be recovered by re-parsing the saved HTML later.
- Uses one `BrowserSession` for all 40 pages and loads several at a time, which is the case that session object exists for. Costs nothing; it only drives Chrome.
- It is resumable: anything already cached is skipped, so one flaky site does not cost a full rerun. `--force` re-renders anyway, `--only TEXT` limits it to companies matching a name, `--kind` picks positives or negatives, `--concurrency` sets how many pages load at once.
- **This is the only file that talks to a browser.** Change a URL in the CSV and you must re-dump before the numbers mean anything.

## benchmark/calibrate.py

- Measures every candidate signal against the cached pages and prints how well each one separates listings pages from the rest. This is how the arrival signals stopped being guesses.
- Reads only from disk, so it runs in about a second and can be rerun after every tweak to a regex or a weight. It never renders anything.
- Prints three things: a per-company table of raw counts, a per-signal summary with the cutoff that splits the two sets most cleanly, and a list of suspect captures — positives showing no job evidence at all, which almost always means the page failed to load its jobs rather than that it has none.
- That suspect check exists because two bad pages slipped through by looking fine. Esri cached with zero jobs on it, and International Paper's recorded URL turned out to be a careers landing page rather than a listing. Both were caught by eye; now the script says it out loud.
- `--detail SIGNAL` shows the actual strings behind a number, which is how you confirm a signal measures what you think. `--csv PATH` writes the raw counts out for sorting elsewhere. `--pages DIR` reads a different cache.

