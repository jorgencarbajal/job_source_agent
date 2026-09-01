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
| `pipeline.py` | Stage 1 + Stage 2 behind the cache. One entry point. |
| `cache.py` | Disk cache. Credits are never spent twice on the same job id. |
| `config.py` | Settings and secrets. |
| `models.py` | `CompanyIdentity`, `JobSourceResult`. |
| `api.py` | FastAPI demo site. |

*(Per-function notes get written as each module is built.)*

