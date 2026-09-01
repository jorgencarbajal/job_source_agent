# Project Flow

*STAGE 1:* 
- LinkedIn job listing -> company website (using ScrapingDog)

*STAGE 2:*
- website -> careers page (our code)
- careers page -> listings board (our code)

## Structure

| file | role |
|---|---|
| `linkedin.py` | Stage 1. ScrapingDog: job id -> company name + website. The only paid code. |
| `browser.py` | Playwright. Renders a page, returns its links as (text, href). |
| `arrival.py` | Have we reached a listings page? Known board hosts + scoring. |
| `llm.py` | Haiku. Given a link list, picks the one heading toward jobs. |
| `navigator.py` | Stage 2. The hop loop: render -> extract -> arrived? -> choose -> follow. |
| `pipeline.py` | Stage 1 + Stage 2. One entry point. |
| `config.py` | Settings and secrets. |
| `models.py` | `CompanyIdentity`, `JobSourceResult`. |
| `api.py` | FastAPI demo site. |

*(Per-function notes get written as each module is built.)*

## src/job_source_agent/config.py

- A simple configuration file

## src/job_source_agent/models.py

- Holds data classes. `CompanyIdentity` is what stage 1 gets out of the linked in job posting URL. `Hop` keeps track of the path the LLM (crawler) takes through the browser and also notes reasoning. `JobSourceResult` contains the final outcome of the run.

## src/job_source_agent/linkedin.py

- Stage 1, and the only code that spends credits. Turns a LinkedIn job posting
  URL into the company behind it, website included.
- The default path is two calls, because ScrapingDog scrapes one page per call
  and the website only lives on the company profile. We extract the job id from
  the URL and send it to `SCRAPINGDOG_JOB_URL`, which returns the company name
  and a profile link; we parse the slug out of that link and send it to
  `SCRAPINGDOG_PROFILE_URL`, which returns the website.
- Run as a standalone file for a quick check, or import `resolve()`, which is
  what the rest of the project uses. `--slug` skips the first call when you
  already know the slug, halving the cost; `--raw` dumps every field both calls
  returned, for debugging.