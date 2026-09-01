"""Stage 2: walk from a company website to its job listings page.

One loop, run until we arrive or run out of hops:

    render -> extract links -> arrived? -> choose a link -> follow

This is the whole of stages 2 and 3. Reaching the careers page and then reaching
the listings behind it are the same operation run twice, which is what the
collected data showed: careers -> "search jobs" -> listings, occasionally with
one more step in between.

Hops are capped, so a site that leads us in circles fails fast.
"""
