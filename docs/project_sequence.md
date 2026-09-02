# Project sequence

What each file does and why it exists. In roughly the order the data moves.

The path: a LinkedIn job URL goes to `linkedin.py`, which returns the company
and its website. `navigator.py` then walks that website, using `browser.py` to
render each page, `arrival.py` to score it, and `llm.py` to choose the next
link. `pipeline.py` joins the two halves and `api.py` puts them on the web.

The `benchmark/` files exist to make that measurable rather than hopeful.

## Signals

The eight things `arrival.py` looks at to decide if a page is a list of open
jobs. Counts in brackets are positives / negatives above the best cutoff, out of
20 each, measured by `benchmark/calibrate.py`.

**Tier 1 — are there job rows here?** These carry the score.

| signal | split | what it detects |
|---|---|---|
| `count_phrase` | 17 / 0 | The page states its own job count: "204 open positions", "Jobs (448)". Pages only say that when they are showing them. |
| `job_hrefs` | 15 / 0 | Links pointing at one specific posting (`/job/12345`, `jobid=`). Counts the rows through their destinations. |
| `title_nouns` | 15 / 1 | Job-title words as link text (engineer, welder, custodian). Precise, but blind when a board puts the title outside the anchor. |
| `title_text` | 19 / 3 | The same words anywhere in the page text. Rescues Oracle boards like Honeywell, where titles sit beside the links, not inside them. |
| `locations` | 19 / 4 | "City, ST" plus Remote/Hybrid. Job rows carry a location; so do retail and civic homepages, which is where it leaks. |

**Tier 2 — are we in the right neighbourhood?** Small points. Break ties, never decide.

| signal | split | what it detects |
|---|---|---|
| `title_keyword` | 19 / 1 | Job wording in the `<title>`. Says the page is jobs-related, not that jobs are on it. |
| `url_keyword` | 17 / 1 | `/careers`, `/jobs`, `/openings` in the address. Same idea, read off the URL. |
| `ats_host` | 8 / 0 | A known applicant tracking system. Recognises a job board, never guesses one, and cannot tell its front door from its listings. |

**Dropped:** `pagination` (too rare) and `href_family`, a repeated-URL-shape
detector (homepages have big link families too, and it duplicated `job_hrefs`).

**Why two tiers.** AMETEK's LinkedIn profile points at `ametek.com/careers`, a
careers page with no jobs on it. It scores full marks on Tier 2 and zero on Tier
1 — exactly the page the hop loop must not stop on.

## src/job_source_agent/config.py

- Settings and secrets in one place, read from `.env` once.
- Holds every tunable the other files share, so they cannot drift apart: hop budget, walk timeout, model name, and the two separate concurrency limits.
- `BROWSER_CONCURRENCY` is 2, measured. At 4 the benchmark scored 14/20 in 584s; at 2 it scored 16/20 in 423s. Starving the browser makes pages snapshot half-built, so walks wander and burn their whole budget.
- `SCRAPINGDOG_CONCURRENCY` is separate and tighter. The two limits are unrelated and one number cannot serve both.
- `require_*_key()` fails loudly at the start rather than as a confusing 401 halfway through a run.

## src/job_source_agent/models.py

- The data passed between stages. `CompanyIdentity` is what stage 1 gets from a LinkedIn URL. `Hop` is one step of the walk with its reason and score. `JobSourceResult` is the final answer for one URL.

## src/job_source_agent/linkedin.py

- Stage 1, and the only code that spends credits. LinkedIn job URL in, company name and website out.
- Two calls, because ScrapingDog scrapes one page per call and the website only exists on the company profile. Job id gives the company name and a profile link; the slug parsed from that link gives the website.
- Retries 429 and server errors with a growing wait. ScrapingDog rejects calls made too close together, and a rejection is temporary, not a failed lookup.
- Run standalone for one URL, or import `resolve()`. `--slug` skips the first call and halves the cost, `--raw` dumps every field for debugging.

## src/job_source_agent/browser.py

- The eyes of stage 2. Loads a URL in a real browser and returns a `PageSnapshot`: final URL, title, rendered HTML, and every link. Filters nothing — choosing is `llm.py`'s job, deciding we arrived is `arrival.py`'s.
- Plain HTTP was not enough. Half the boards build their listings with JavaScript and two refuse non-browser clients outright.
- `BrowserSession` keeps one Chrome alive and opens a fresh context per page, so a run of many pages pays startup once.
- Four non-obvious fixes, each from a site that broke without it: real Chrome rather than bundled Chromium (CarMax returns 403 to Chromium), retry with a looser wait each time (Honeywell's CDN fails intermittently), wait for a link to exist before reading (a committed page has none yet), and add a scheme to bare domains.
- Run standalone against any URL to see what it extracts. `--all` shows every link.

## src/job_source_agent/arrival.py

- Decides whether a page is a list of open jobs. Ends the hop loop.
- Eight signals vote and the votes are added. No single test survives real career sites: titles lie, ATS hosts mean nothing on their own, half the pages are JavaScript.
- Each signal is yes-or-no, not a quantity. Gopuff shows 1,523 locations and MLK shows 6; both are listings pages, and letting magnitude in would drown the small one.
- Owns the regexes. `calibrate.py` imports them, so calibration can never drift into measuring a pattern the agent no longer uses.
- Measured on all 40 cached pages: every company's listings page outscores its own homepage. The score compares within one company, not across companies.
- Run standalone on a URL, or `--cached PATH` on a saved page. `--quiet` drops the signal table.

## src/job_source_agent/llm.py

- Picks the next link. The only file in stage 2 that costs money.
- Two passes. A free ranker scores every link on wording, address and nav/footer position, then the top 30 go to Haiku, which picks one and says why.
- The ranker exists because homepages carry 100-800 links. Measured on all 20 homepages, the top 30 always still holds a careers link.
- The model exists because the ranker produces ties it cannot break. Honeywell's homepage has five links tied at the same score and the right answer sits sixth, in the footer. The model found it.
- The model answers with a number from the list, not a URL. A bad number fails a range check; a made-up URL would send the browser nowhere real.
- If the API fails it returns the top-ranked link and records why, so a network problem costs one worse hop rather than the run.
- Run standalone to see the ranking. `--cached PATH` uses a saved page, `--all` shows every scoring link, `--choose` also calls the model and is the only part that costs anything.

## src/job_source_agent/navigator.py

- The loop: render, score, pick a link, repeat, up to 5 pages.
- Holds no judgment of its own. It only decides when to stop and which page to keep.
- Keeps the best page seen rather than the first decent one, because scores compare only within a single company.
- Stops early only at 46 or above, where a page is unmistakably a job board. Below that it spends the budget and lets best-so-far decide — slower, never wrong.
- At a dead end it backs up and takes the next-best link from the previous page. Human paths run 1 to 3 hops against a budget of 5, so the slack was already there.
- Renders a page twice if it shows no sign of jobs at all, once per walk. That is the Esri case: the page loaded fine but its jobs never arrived and nothing looked wrong.
- Returns a `Walk`: best URL, score, every hop with its reason, and an outcome saying whether the result is certain, likely, or nothing.
- Gives up after `WALK_TIMEOUT` and returns what it found. When the browser subprocess dies, a page load in flight has nothing to answer it and waits forever -- one stuck company hung a whole benchmark run.
- Run standalone on any company website. `--max-hops N` changes the budget, `--quiet` prints just the answer.

## src/job_source_agent/pipeline.py

- The whole product in one function: LinkedIn URL in, job listings page out.
- Thin by design. It calls `resolve()` then `walk()` and records what came back.
- `resolve()` is synchronous so it runs on a worker thread, or one slow ScrapingDog call stalls every other URL in the batch.
- The two stages fail differently and the outcome says which. No website on the profile is a stage 1 problem; an unreachable site is a stage 2 problem.
- A failed URL is a result, not an exception, so one bad posting cannot take down a batch of ten.
- `run_many()` shares one browser and hands back each result as it finishes, which is what lets the demo stream rows in.
- Run standalone on one or more URLs. It prints the cost first; `--dry-run` spends nothing.

## src/job_source_agent/api.py

- The public demo. Paste up to 10 LinkedIn job URLs, get each company's job board back.
- Results stream as NDJSON, one JSON object per line. Ten URLs takes around 100 seconds and a page blank that long reads as broken; `run_many` already yields each result on arrival, so the endpoint just forwards them.
- Every row shows the company, the website stage 1 found, the answer as a link, and a collapsible hop trail with each score and the model's own sentence. The trail is the point -- it is what makes it a demonstration of an agent rather than a box that emits a URL.
- The page is one self-contained HTML string inside the module. No build step, no static files, no framework.
- A daily credit cap stops the demo spending without limit, since anyone can reach it. Credits are reserved before work starts, so a run cannot begin and then be cut off. The counter lives in memory and resets on restart, which is fine for one instance and wrong for several.
- Bad input is refused before anything is spent: no valid URLs, more than the limit, or the day's budget gone.
- Run with `uv run uvicorn job_source_agent.api:app`. Do not add `--reload` on Windows -- it switches uvicorn to an event loop that cannot spawn subprocesses, and Playwright's driver is a subprocess.

## benchmark/ground_truth.csv

- The answer key. 20 companies walked by hand: `company, website, linkedin_url, listings_url, hops`.
- Makes two loops possible. Starting at `website` tests stage 2 for free. Starting at `linkedin_url` tests everything and costs credits.
- Three rows were corrected after the agent disagreed with them, so it is not infallible — it is a record of what a human found.

## benchmark/fill_websites.py

- Filled the `website` column by running stage 1 over every row. Costs credits.
- Skips rows that already have a website, so re-running only pays for blanks.
- Writes back after every row. A crash on row 15 must not discard the fourteen already bought.
- `--dry-run` shows what it would look up and spends nothing.

## benchmark/dump_pages.py

- Renders all 40 ground-truth pages once and caches them, so signal work happens in milliseconds instead of waiting on Chrome. Each row gives a listings page as a positive and a homepage as a negative.
- Writes `.html` for the text and `.json` for the links with their nav and footer flags — those are computed inside the browser and cannot be recovered by re-parsing the HTML.
- Resumable: anything already cached is skipped, so one flaky site does not cost a full rerun.
- `--force` re-renders, `--only TEXT` limits it to one company, `--kind` picks positives or negatives, `--concurrency` sets how many load at once.
- Change a URL in the ground truth and you must re-dump before any calibration number means anything.

## benchmark/calibrate.py

- Measures every candidate signal against the cached pages and shows how well each separates listings pages from everything else. This is how the signals stopped being guesses.
- Reads only from disk, so it reruns in about a second after any change to a regex or a weight. It never renders anything.
- Prints three things: raw counts per company, a per-signal summary with the cleanest cutoff, and a list of suspect captures — positives with no job evidence at all, which usually means the page failed to load its jobs rather than that it has none.
- That last check exists because two bad pages slipped through by looking fine: Esri cached with zero jobs, and International Paper's recorded URL turned out to be a careers landing page.
- `--detail SIGNAL` shows the strings behind a number, `--csv PATH` writes the counts out, `--pages DIR` reads a different cache.

## benchmark/run.py

- Runs the agent against the answer key and reports the success rate. This is the number the take-home asks for.
- Two modes. The default starts at each `website` and is free apart from a few cents of model calls, so it can be run constantly. `--full` starts at each `linkedin_url` and pays for stage 1 — run that once at the end.
- Matching is strict: scheme, `www.`, trailing slash, query and fragment are normalised away, then host and path must match. Nothing looser, because a permissive comparison fails silently and inflates the result, while a strict one fails loudly and prints the pair to check.
- The known cost of strict matching: a company reached through a vanity domain counts as a miss even when the page is right, so the reported rate is a floor.
- Writes each row to disk the moment it finishes, so a crash during a paid run never discards what was already bought.
- A company that throws becomes a recorded miss rather than ending the run, and `--only NAME` reruns a single company for diagnosis.
- Result on the development set: 16/20 strict, 18/20 verified by hand, 0 navigation failures. The four strict misses are two correct answers at different URLs and two careers pages showing a subset of jobs.
- `--dry-run` lists the plan, `--only` and `--limit` narrow it, `--concurrency` and `--max-hops` tune it, `--out` sets the results file.
