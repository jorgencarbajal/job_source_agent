"""Playwright: render a page and report the links on it.

Static fetching was not enough. Real job boards render their listings client
side, and some sites refuse plain HTTP clients outright, so pages are loaded in
a real browser with a real viewport, locale, and timezone.

Returns links as (anchor text, absolute href) pairs rather than raw HTML -- a
few KB instead of megabytes, which is what makes handing them to an LLM cheap.
"""
