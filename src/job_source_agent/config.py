"""Settings and secrets, read once from the environment.

Everything tunable lives here so that the navigator, the API, and the benchmark
cannot drift apart on constants.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


SCRAPINGDOG_API_KEY = os.getenv("SCRAPINGDOG_API_KEY", "")

# scraping dog needs both the job and profile endpoints
SCRAPINGDOG_JOB_URL = "https://api.scrapingdog.com/linkedinjobs"
SCRAPINGDOG_PROFILE_URL = "https://api.scrapingdog.com/linkedin"

SCRAPINGDOG_TIMEOUT = 60.0


BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "1") != "0"
PAGE_TIMEOUT = 30.0

MAX_HOPS = 4

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL = "claude-haiku-4-5-20251001"


def require_scrapingdog_key() -> str:
    """Fail loudly and early rather than on a confusing 401 mid-run."""
    if not SCRAPINGDOG_API_KEY:
        raise RuntimeError(
            "SCRAPINGDOG_API_KEY is not set. Add it to .env (see .env.example)."
        )
    return SCRAPINGDOG_API_KEY
