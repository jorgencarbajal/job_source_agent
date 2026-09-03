"""Playwright: render a page and report the links on it.

Static fetching was not enough. Nearly half the ground-truth boards render their
listings client side, and two refuse plain HTTP clients outright, so pages are
loaded in a real browser with a real viewport, locale, and timezone.

What comes back is a `PageSnapshot`: the final URL after redirects, the title,
the rendered HTML for `arrival.py` to score, and every link as (text, href).

Deliberately filters nothing. A homepage carries hundreds of links, and trimming
here would risk discarding the careers link before anything has looked at it --
choosing is `llm.py`'s job, deciding we have arrived is `arrival.py`'s.

    uv run python -m job_source_agent.browser <url> [--all]
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from playwright.async_api import (
    Browser,
    Playwright,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from job_source_agent.config import BROWSER_HEADLESS, PAGE_TIMEOUT

# Headless Chrome announces "HeadlessChrome" in its user agent, which is an
# obvious bot signal, so that one word is replaced at runtime. Everything else is
# left as Chrome reports it.
#
# A hardcoded string was used here and had to go. It claimed Windows Chrome 151,
# which was true on the development laptop and a lie on the Linux server running
# Chrome 152 -- wrong platform, wrong version, and drifting further with every
# release. Chrome sends `Sec-CH-UA-Platform` honestly regardless, so any site
# that cross-checks the two sees the mismatch.
#
# The constant below is only a fallback for when the real agent cannot be read.
HEADLESS_MARKER = "HeadlessChrome"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

VIEWPORT = {"width": 1366, "height": 900}
LOCALE = "en-US"
TIMEZONE = "America/Los_Angeles"

# Images, video and fonts are irrelevant to link extraction and cost most of the
# load time and memory. Blocking them roughly halves both. This is the first
# thing to turn off if a site renders strangely.
BLOCKED_RESOURCES = {"image", "media", "font"}

# After the DOM is ready, give client-rendered content a chance to appear.
#
# `networkidle` was used here and had to go. It returns after ~500ms of network
# quiet, and a JavaScript app has exactly such a gap while it boots -- before it
# has asked for any data. On a quiet machine that gap wins the race and the page
# is captured empty: Workiva's Workday board came back with no title and zero
# links on the Linux server while the identical URL rendered 34 links with a flat
# 6-second wait. The laptop passed only because its network was noisier.
#
# So instead: watch the page until it stops changing. Sample the anchor count
# every SETTLE_POLL_MS and stop once it repeats, or once SETTLE_TIMEOUT_MS is up.
# That handles both failure directions -- pages that go quiet too early, and
# pages that need longer than any fixed wait.
SETTLE_TIMEOUT_MS = 12000
SETTLE_POLL_MS = 400
SETTLE_STABLE_SAMPLES = 2

# Some sites fail a navigation intermittently and succeed on the next try --
# Honeywell's CDN breaks the HTTP/2 handshake perhaps half the time. Each retry
# also relaxes what we wait for: a page that never reaches `domcontentloaded`
# will still render its links after `commit`.
NAVIGATION_ATTEMPTS = ("domcontentloaded", "commit", "commit")

MAX_LINK_TEXT = 120


def normalise_url(url: str) -> str:
    """
    Add http:// if necessary
    """
    url = url.strip()
    if not url:
        return url
    if "//" not in url.split("?", 1)[0]:
        return "https://" + url.lstrip("/")
    return url


@dataclass(frozen=True)
class Link:
    """One anchor on the page."""

    text: str
    href: str
    in_footer: bool
    in_nav: bool

    def __str__(self) -> str:
        where = " [footer]" if self.in_footer else (" [nav]" if self.in_nav else "")
        return f"{self.text or '(no text)'}{where} -> {self.href}"


@dataclass(frozen=True)
class PageSnapshot:
    """Everything one rendered page tells us."""

    url: str  # final URL, after redirects
    title: str
    html: str
    links: list[Link]


# JavaScript that runs inside the browser page, extracting every link as a structured object. Playwright hands this string to Chrome via page.evaluate().
_EXTRACT_LINKS_JS = """
() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
    text: (
        a.innerText || a.textContent || a.getAttribute('aria-label') ||
        a.getAttribute('title') || ''
    ).replace(/\\s+/g, ' ').trim().slice(0, %d),
    href: a.href,
    in_footer: !!a.closest('footer, [class*="footer" i], [id*="footer" i]'),
    in_nav: !!a.closest('nav, header, [role="navigation"]'),
}))
""" % MAX_LINK_TEXT


class BrowserSession:
    """A single long-lived Chromium, with a fresh context per page load.

    Playwright is the driver connection. async_playwright().start() spawns a Node.js subprocess — Playwright's actual implementation is JavaScript — and opens a pipe to it. That object is your handle on that subprocess. It's not a browser; it's the thing that can launch browsers, and it also gives you .chromium, .firefox, .webkit.

    Browser is one launched Chrome process, created by asking the driver to start one.
    """
    
    def __init__(self) -> None:

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._user_agent: str | None = None
        self._lock = asyncio.Lock()

    async def user_agent(self) -> str:
        """Return the user agent every page load should send, reading it from
        Chrome itself the first time and remembering it afterwards.

        Takes whatever this Chrome reports and replaces only `HeadlessChrome`
        with `Chrome`, so the platform and version stay true. Falls back to the
        `USER_AGENT` constant if the real one cannot be read, which is better
        than sending nothing.
        """
        if self._user_agent is not None:
            return self._user_agent

        browser = await self.start()
        try:
            context = await browser.new_context()
            page = await context.new_page()
            reported = await page.evaluate("navigator.userAgent")
            await context.close()
            self._user_agent = str(reported).replace(HEADLESS_MARKER, "Chrome")
        except PlaywrightError:
            self._user_agent = USER_AGENT
        return self._user_agent

    async def start(self) -> Browser:
        """
        Start up playwright process and launch the browswer. Returning Browser object.
        """
        async with self._lock:
            if self._browser is None:
                self._playwright = await async_playwright().start()
                self._browser = await self._launch()
            return self._browser
    
    async def _launch(self) -> Browser:
        """
        Launch a browser and return the `Browser` object, preferring real
        Chrome (`channel="chrome"`) over Playwright's bundled Chromium. If Chrome
        is not installed on this machine, `launch()` raises `PlaywrightError`, so
        the `except` catches that and launches Chromium instead.
        """
        assert self._playwright is not None
        try:
            return await self._playwright.chromium.launch(
                headless=BROWSER_HEADLESS, channel="chrome"
            )
        except PlaywrightError:
            return await self._playwright.chromium.launch(headless=BROWSER_HEADLESS)

    async def close(self) -> None:
        """
        Close the playwright and browser processes.

        Never raises. A crashed driver makes `close()` fail with "Connection
        closed while reading from the driver", and a shutdown error thrown from
        a `finally` block will throw away a run's results on the way out. The
        handles are cleared either way, so a later call cannot use a dead one.
        """
        async with self._lock:
            browser, self._browser = self._browser, None
            playwright, self._playwright = self._playwright, None

            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception:
                    pass

    async def snapshot(self, url: str) -> PageSnapshot | None:
        """
        Render one URL in a fresh browser context and return a `PageSnapshot`
        with the final URL, title, HTML and links, or `None` if the page cannot
        be loaded or read at all. The `try/finally` guarantees the context is
        closed on every path out, including the two early `return None`s.
        """
        browser = await self.start()
        context = await browser.new_context(
            viewport=VIEWPORT,
            locale=LOCALE,
            timezone_id=TIMEZONE,
            user_agent=await self.user_agent(),
            ignore_https_errors=True,
        )
        try:
            await context.route("**/*", _block_heavy_resources)
            page = await context.new_page()

            # navigates and blocks until the page reaches the readiness state named by wait_until, if successful break
            for attempt, wait_until in enumerate(NAVIGATION_ATTEMPTS):
                try:
                    await page.goto(
                        normalise_url(url),
                        wait_until=wait_until,
                        timeout=PAGE_TIMEOUT * 1000,
                    )
                    break
                except (PlaywrightTimeout, PlaywrightError):
                    if attempt == len(NAVIGATION_ATTEMPTS) - 1:
                        return None
                    await page.wait_for_timeout(500)

            # A page committed but not finished has no anchors yet, so wait for one to exist rather than for a fixed interval.
            try:
                await page.wait_for_selector("a[href]", timeout=SETTLE_TIMEOUT_MS)
            except PlaywrightTimeout:
                pass

            # Then wait for the page to stop growing, rather than for the network to go quiet.
            await _settle(page)

            # pass in JavaScript expression to be evaluated in the browser context. create the object holding all the information related to that page.
            try:
                raw_links = await page.evaluate(_EXTRACT_LINKS_JS)
                return PageSnapshot(
                    url=page.url,
                    title=(await page.title() or "").strip(),
                    html=await page.content(),
                    links=_clean_links(raw_links),
                )
            except PlaywrightError:
                return None
        finally:
            await context.close()


async def _settle(page) -> None:
    """Wait until the page stops adding links, or until the time limit is up.

    Returns nothing; it either settles or gives up, and the caller reads whatever
    the page holds by then. Samples the anchor count every `SETTLE_POLL_MS` and
    stops once the same number comes back `SETTLE_STABLE_SAMPLES` times in a row,
    which is a page that has finished building itself.

    This replaces `networkidle`, which measures the wrong thing: a JavaScript app
    is briefly quiet while booting, before it has requested any data, and on a
    quiet machine that gap satisfies `networkidle` and the page is captured empty.
    Counting what is actually on the page cannot be fooled that way.
    """
    previous = -1
    repeats = 0
    waited = 0

    while waited < SETTLE_TIMEOUT_MS:
        try:
            count = await page.evaluate("document.querySelectorAll('a[href]').length")
        except PlaywrightError:
            return

        if count == previous and count > 0:
            repeats += 1
            if repeats >= SETTLE_STABLE_SAMPLES:
                return
        else:
            repeats = 0
        previous = count

        await page.wait_for_timeout(SETTLE_POLL_MS)
        waited += SETTLE_POLL_MS


async def _block_heavy_resources(route, request) -> None:
    if request.resource_type in BLOCKED_RESOURCES:
        await route.abort()
    else:
        await route.continue_()


def _clean_links(raw: list[dict]) -> list[Link]:
    """Drop links that cannot be navigated to, and dedupe by href.

    Fragment links are kept on purpose: at least one company in the ground truth
    lists its jobs at `/careers#open-roles`, so a fragment can be the answer.
    """
    seen: dict[str, Link] = {}
    for item in raw:
        href = (item.get("href") or "").strip()
        if not href:
            continue
        low = href.lower()
        if low.startswith(("mailto:", "tel:", "javascript:", "data:", "blob:")):
            continue
        if low in ("#", "/#"):
            continue

        link = Link(
            text=(item.get("text") or "").strip(),
            href=href,
            in_footer=bool(item.get("in_footer")),
            in_nav=bool(item.get("in_nav")),
        )
        # Same destination reached twice: keep whichever copy has real text.
        existing = seen.get(href)
        if existing is None or (not existing.text and link.text):
            seen[href] = link
    return list(seen.values())


async def snapshot(url: str) -> PageSnapshot | None:
    """
    Render one URL in a throwaway `BrowserSession` and return its
    `PageSnapshot`, or `None` if the page cannot be loaded. The `try/finally`
    closes the session on every path out, so Chrome and the Playwright driver
    are shut down even if `snapshot()` raises.
    """
    session = BrowserSession()
    try:
        return await session.snapshot(url)
    finally:
        await session.close()


if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    args = [a for a in argv if not a.startswith("--")]
    show_all = "--all" in argv

    if not args:
        print("usage: browser.py <url> [--all]")
        raise SystemExit(1)

    snap = asyncio.run(snapshot(args[0]))
    if snap is None:
        print("failed to load")
        raise SystemExit(1)

    print(f"url    : {snap.url}")
    print(f"title  : {snap.title!r}")
    print(f"html   : {len(snap.html):,} bytes")
    print(f"links  : {len(snap.links)}")

    shown = snap.links if show_all else snap.links[:25]
    for link in shown:
        print(f"  {link}")
    if not show_all and len(snap.links) > len(shown):
        print(f"  ... {len(snap.links) - len(shown)} more (--all to see them)")
