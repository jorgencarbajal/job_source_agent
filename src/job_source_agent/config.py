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

# Measured, not documented: two calls fired back to back came back 429 "Too many
# requests" even with only two URLs in flight. The limit is on request rate, not
# on concurrency, so stage 1 both waits between retries and caps how many URLs
# may be looking themselves up at once.
SCRAPINGDOG_ATTEMPTS = 4
SCRAPINGDOG_BACKOFF = 2.0
SCRAPINGDOG_CONCURRENCY = 2
SCRAPINGDOG_RETRY_STATUS = (429, 500, 502, 503, 504)


BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "1") != "0"
PAGE_TIMEOUT = 30.0

# How many pages may render at once. Measured, not guessed: at 4 the benchmark
# scored 14/20 in 584s, at 2 it scored 16/20 in 423s. Starving the browser makes
# pages snapshot half-built, so walks wander and burn their whole hop budget --
# fewer workers was both more accurate and faster. Treat 2 as a ceiling on
# smaller hardware, not a target.
BROWSER_CONCURRENCY = int(os.getenv("BROWSER_CONCURRENCY", "2"))

MAX_HOPS = 5

# A whole walk must finish or be abandoned. When Playwright's driver subprocess
# dies, a page load in flight has nothing left to answer it and waits forever --
# one stuck company hung a 20-company benchmark, and would hang all ten URLs in
# the demo. Five hops at roughly 8s each plus model calls fits comfortably.
WALK_TIMEOUT = 120.0

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL = "claude-haiku-4-5"
LLM_MAX_TOKENS = 512
LLM_TIMEOUT = 30.0

# An identity-linked key must name the workspace it acts in, or every call comes
# back 400. Ordinary keys ignore this, so it is optional.
ANTHROPIC_WORKSPACE_ID = os.getenv("ANTHROPIC_WORKSPACE_ID", "")


# The demo is a public URL and every submission spends real credits, so the app
# stops itself rather than trusting that nobody finds it. Anyone may use the
# demo; nobody can run the bill past this in a day.
DEMO_MAX_URLS = 10
DEMO_DAILY_CREDITS = int(os.getenv("DEMO_DAILY_CREDITS", "5000"))
CREDITS_PER_URL = 55

# The page itself stays public -- anyone may read what the demo is and how it
# works -- but the endpoint that spends money asks for a key first, so a link
# found by a crawler cannot run up a bill. The key is sent to Jobnova alongside
# the URL. Leaving this blank turns the gate off entirely, which is what local
# development wants.
DEMO_ACCESS_KEY = os.getenv("DEMO_ACCESS_KEY", "")

# Where to push a one-line alert when the day's budget runs out. ntfy.sh is a
# free relay with no account: anything posted to a topic arrives on every phone
# subscribed to that topic. The topic name is the only thing keeping strangers
# out, so make it long and random, and never put a key or a URL in a message.
# Blank means no notification is sent.
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_URL = os.getenv("NTFY_URL", "https://ntfy.sh")
NTFY_TIMEOUT = 10.0


def require_anthropic_key() -> str:
    """Fail loudly and early rather than on a confusing 401 mid-run."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example)."
        )
    return ANTHROPIC_API_KEY


def require_scrapingdog_key() -> str:
    """Fail loudly and early rather than on a confusing 401 mid-run."""
    if not SCRAPINGDOG_API_KEY:
        raise RuntimeError(
            "SCRAPINGDOG_API_KEY is not set. Add it to .env (see .env.example)."
        )
    return SCRAPINGDOG_API_KEY
