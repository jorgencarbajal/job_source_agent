"""FastAPI demo site: paste a LinkedIn job URL, get the company's job board.

This is the deployed URL Jobnova uses to test their own 10 links, so it shows
the hops taken and not just the final answer -- a visible trail is the
difference between a result and a demonstration.

Results stream rather than arriving together. Ten URLs takes around a hundred
seconds, and a page that sits blank that long reads as broken. `run_many` hands
back each result the moment it is ready, and the endpoint forwards each one as a
line of JSON, so rows fill in as the agent works.

The demo is public and every submission spends credits, so it stops itself. A
daily budget is tracked in memory and refuses new work once the day's allowance
is gone. That guards the actual risk -- an unbounded bill -- without putting
anything between Jobnova and the demo.

The budget resets when the process restarts, which is fine for a demo on one
instance and would need a real store if this ever ran on several.

Running it:

    uv run uvicorn job_source_agent.api:app --reload

    1. Open http://127.0.0.1:8000.
    2. Paste up to 10 LinkedIn job URLs, one per line.
    3. Watch each row fill in as its walk finishes.

    Every submitted URL costs about 55 ScrapingDog credits, roughly 2.2 cents.
"""

from __future__ import annotations

import json
import re
from datetime import date

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from job_source_agent import pipeline
from job_source_agent.config import (
    CREDITS_PER_URL,
    DEMO_DAILY_CREDITS,
    DEMO_MAX_URLS,
)
from job_source_agent.models import JobSourceResult

app = FastAPI(title="Job source agent", docs_url="/docs")

JOB_URL_RE = re.compile(r"linkedin\.com/jobs/view/\d+|[?&]currentJobId=\d+", re.IGNORECASE)


class Submission(BaseModel):
    """What the page posts: the raw contents of the textarea."""

    urls: str


class Budget:
    """The day's credit allowance, counted down as work is accepted.

    Held in memory and reset whenever the date changes. One instance is enough
    for a demo; several instances would each get their own allowance, which is
    the thing to fix if this is ever scaled out.
    """

    def __init__(self, daily: int) -> None:
        self.daily = daily
        self.day = date.today()
        self.spent = 0

    def _roll(self) -> None:
        """Start a new day's allowance if the date has changed. Called before
        every read so the budget never has to be reset by hand."""
        today = date.today()
        if today != self.day:
            self.day, self.spent = today, 0

    @property
    def remaining(self) -> int:
        """How many credits are left to spend today."""
        self._roll()
        return max(0, self.daily - self.spent)

    def take(self, credits: int) -> bool:
        """Reserve credits for a submission, returning False if the day's
        allowance cannot cover it. Reserving up front rather than charging as we
        go means a run cannot start and then be cut off halfway."""
        self._roll()
        if credits > self.daily - self.spent:
            return False
        self.spent += credits
        return True


budget = Budget(DEMO_DAILY_CREDITS)


def parse_urls(raw: str) -> tuple[list[str], list[str]]:
    """Split the textarea into LinkedIn job URLs and everything that did not
    look like one. Returns the two lists so the page can show what was rejected
    instead of silently dropping a typo."""
    good: list[str] = []
    bad: list[str] = []
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        (good if JOB_URL_RE.search(candidate) else bad).append(candidate)
    return good, bad


def as_row(result: JobSourceResult) -> dict:
    """Flatten one finished result into the shape the page renders. Returns a
    plain dict so it can go straight through `json.dumps`."""
    return {
        "type": "result",
        "linkedin_url": result.linkedin_url,
        "company": result.identity.company_name if result.identity else None,
        "website": result.identity.website if result.identity else None,
        "listings_url": result.listings_url,
        "outcome": result.outcome,
        "ok": result.ok,
        "hops": [
            {"url": hop.url, "reason": hop.reason, "score": hop.score}
            for hop in result.hops
        ],
    }


async def stream(urls: list[str], rejected: list[str]):
    """Yield one line of JSON per event: a header, then a result per URL as it
    finishes, then a summary. Newline-delimited JSON is used rather than a
    single response so the page can render each row on arrival."""
    yield json.dumps({"type": "start", "count": len(urls), "rejected": rejected}) + "\n"

    found = 0
    async for result in pipeline.run_many(urls):
        if result.ok:
            found += 1
        yield json.dumps(as_row(result)) + "\n"

    yield json.dumps(
        {"type": "done", "found": found, "total": len(urls),
         "credits_left": budget.remaining}
    ) + "\n"


@app.post("/api/resolve")
async def resolve(submission: Submission) -> StreamingResponse:
    """Take the pasted URLs and stream a result for each one. Refuses before
    spending anything when there is nothing valid to run, when more than
    `DEMO_MAX_URLS` were given, or when the day's credit budget is gone."""
    urls, rejected = parse_urls(submission.urls)

    def refusal(message: str) -> StreamingResponse:
        body = json.dumps({"type": "error", "message": message, "rejected": rejected})
        return StreamingResponse(iter([body + "\n"]), media_type="application/x-ndjson")

    if not urls:
        return refusal("No LinkedIn job URLs found. They look like linkedin.com/jobs/view/1234567890.")
    if len(urls) > DEMO_MAX_URLS:
        return refusal(f"{len(urls)} URLs given; this demo takes up to {DEMO_MAX_URLS} at a time.")
    if not budget.take(len(urls) * CREDITS_PER_URL):
        return refusal(
            f"The demo's daily lookup budget is spent ({budget.remaining} credits left). "
            "It resets tomorrow."
        )

    return StreamingResponse(stream(urls, rejected), media_type="application/x-ndjson")


@app.get("/health")
async def health() -> dict:
    """Report that the app is up and how much budget is left. Fly uses this to
    decide whether an instance is healthy."""
    return {"ok": True, "credits_left": budget.remaining, "max_urls": DEMO_MAX_URLS}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    """Serve the single page. The HTML is inline rather than a static file so
    the Docker image has one fewer thing to copy and get wrong."""
    return PAGE


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job source agent</title>
<style>
  :root { color-scheme: light dark; --line:#d5d7dd; --muted:#6b7280; --bg:#fbfbfc; --card:#fff; --accent:#2f6feb; }
  @media (prefers-color-scheme: dark) {
    :root { --line:#2c313a; --muted:#9aa1ac; --bg:#14161a; --card:#1b1e24; --accent:#6f9bff; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 32px 20px 72px; }
  h1 { font-size: 22px; margin: 0 0 6px; }
  .sub { color: var(--muted); margin: 0 0 24px; }
  textarea { width:100%; min-height:130px; padding:12px; border:1px solid var(--line); border-radius:8px;
             background:var(--card); color:inherit; font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; resize:vertical; }
  .row { display:flex; align-items:center; gap:12px; margin-top:12px; }
  button { background:var(--accent); color:#fff; border:0; border-radius:8px; padding:10px 18px; font-size:15px; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  .note { color:var(--muted); font-size:13px; }
  .card { border:1px solid var(--line); border-radius:10px; background:var(--card); padding:14px 16px; margin-top:14px; }
  .card h3 { margin:0 0 2px; font-size:15px; }
  .src { color:var(--muted); font-size:12px; word-break:break-all; }
  .answer { margin-top:10px; font-size:14px; word-break:break-all; }
  .answer a { color:var(--accent); }
  .tag { display:inline-block; font-size:11px; padding:2px 8px; border-radius:999px; border:1px solid var(--line); color:var(--muted); margin-left:8px; }
  .ok { border-color:#2e7d4f; color:#2e7d4f; }
  .bad { border-color:#b3452f; color:#b3452f; }
  details { margin-top:10px; }
  summary { cursor:pointer; color:var(--muted); font-size:13px; }
  .hop { margin:8px 0 0 14px; padding-left:12px; border-left:2px solid var(--line); }
  .hop .u { font-size:12px; word-break:break-all; }
  .hop .r { color:var(--muted); font-size:12px; }
  .err { border-color:#b3452f; }
  .spin { display:inline-block; width:13px; height:13px; border:2px solid var(--line); border-top-color:var(--accent);
          border-radius:50%; animation:spin .8s linear infinite; vertical-align:-2px; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="wrap">
  <h1>Job source agent</h1>
  <p class="sub">Paste LinkedIn job URLs, one per line. Each one is resolved to its company, then a browser agent walks that company's own site to its job listings.</p>

  <textarea id="urls" placeholder="https://www.linkedin.com/jobs/view/4427628688/
https://www.linkedin.com/jobs/view/4456337928/"></textarea>

  <div class="row">
    <button id="go">Find job boards</button>
    <span class="note" id="status">Up to 10 at a time. Around 10-30 seconds each.</span>
  </div>

  <div id="out"></div>
</div>

<script>
const go = document.getElementById('go');
const out = document.getElementById('out');
const status = document.getElementById('status');

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function card(r) {
  const el = document.createElement('div');
  el.className = 'card';
  const tag = r.ok ? '<span class="tag ok">' + esc(r.outcome) + '</span>'
                   : '<span class="tag bad">' + esc(r.outcome) + '</span>';
  const answer = r.listings_url
    ? '<a href="' + esc(r.listings_url) + '" target="_blank" rel="noopener">' + esc(r.listings_url) + '</a>'
    : '<span class="note">no job listings page found</span>';

  let hops = '';
  if (r.hops && r.hops.length) {
    hops = '<details><summary>' + r.hops.length + ' hop' + (r.hops.length > 1 ? 's' : '') + '</summary>'
      + r.hops.map((h, i) =>
          '<div class="hop"><div class="u">' + (i + 1) + '. ' + esc(h.url) + ' <span class="note">[' + h.score + ']</span></div>'
          + '<div class="r">' + esc(h.reason) + '</div></div>').join('')
      + '</details>';
  }

  el.innerHTML =
      '<h3>' + esc(r.company || 'Unknown company') + tag + '</h3>'
    + '<div class="src">' + esc(r.linkedin_url) + (r.website ? ' &rarr; ' + esc(r.website) : '') + '</div>'
    + '<div class="answer">' + answer + '</div>'
    + hops;
  return el;
}

function message(text, bad) {
  const el = document.createElement('div');
  el.className = 'card' + (bad ? ' err' : '');
  el.textContent = text;
  return el;
}

go.onclick = async () => {
  const urls = document.getElementById('urls').value;
  out.innerHTML = '';
  go.disabled = true;
  status.innerHTML = '<span class="spin"></span> working...';

  try {
    const res = await fetch('/api/resolve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({urls})
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '', done = 0, total = 0;

    while (true) {
      const {value, done: finished} = await reader.read();
      if (finished) break;
      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split('\\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.trim()) continue;
        const ev = JSON.parse(line);

        if (ev.type === 'error') {
          out.appendChild(message(ev.message, true));
          status.textContent = '';
        } else if (ev.type === 'start') {
          total = ev.count;
          if (ev.rejected && ev.rejected.length) {
            out.appendChild(message('Skipped ' + ev.rejected.length + ' line(s) that were not LinkedIn job URLs.', true));
          }
          status.innerHTML = '<span class="spin"></span> 0 of ' + total + ' done';
        } else if (ev.type === 'result') {
          out.appendChild(card(ev));
          done++;
          status.innerHTML = '<span class="spin"></span> ' + done + ' of ' + total + ' done';
        } else if (ev.type === 'done') {
          status.textContent = 'Found job boards for ' + ev.found + ' of ' + ev.total + '.';
        }
      }
    }
  } catch (e) {
    out.appendChild(message('Something went wrong: ' + e.message, true));
    status.textContent = '';
  } finally {
    go.disabled = false;
  }
};
</script>
</body>
</html>
"""
