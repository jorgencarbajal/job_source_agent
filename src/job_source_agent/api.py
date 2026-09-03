"""FastAPI demo site: paste a LinkedIn job URL, get the company's job board.

This is the deployed URL Jobnova uses to test their own 10 links, so it shows
the hops taken and not just the final answer -- a visible trail is the
difference between a result and a demonstration.

Results stream rather than arriving together. Ten URLs takes around a hundred
seconds, and a page that sits blank that long reads as broken. `run_many` hands
back each result the moment it is ready, and the endpoint forwards each one as a
line of JSON, so rows fill in as the agent works.

The demo is public and every submission spends credits, so two things guard the
bill. An access key sent along with the link unlocks the part that costs money,
and a daily budget refuses new work once the day's allowance is gone.

The key is the useful one. The page itself stays readable by anyone, so the URL
is never dead, but a crawler that finds the hostname cannot spend a credit. The
budget behind it is a backstop for the key leaking rather than the defence
itself, which is why it can stay generous: a cap tight enough to stop a stranger
is also tight enough to be empty when Jobnova arrives.

`DEMO_ACCESS_KEY` blank switches the gate off, which is what local development
wants. Both settings are environment variables, so the key can be rotated and
the budget raised for the day Jobnova tests without touching this file.

When the budget does run out, one line goes to ntfy.sh -- a free relay that
pushes to a phone subscribed to the topic -- so an empty budget is something you
hear about rather than discover. It fires once a day, never more.

The budget resets when the process restarts, which is fine for a demo on one
instance and would need a real store if this ever ran on several.

Running it:

    uv run uvicorn job_source_agent.api:app

    1. Open http://127.0.0.1:8000.
    2. Paste up to 10 LinkedIn job URLs, one per line.
    3. Watch each row fill in as its walk finishes.

    Every submitted URL costs about 55 ScrapingDog credits, roughly 2.2 cents.

Do not add `--reload` on Windows, and do not run more than one worker. Uvicorn
picks its event loop with `use_subprocess = reload or workers > 1`, and on
Windows that switches it from `ProactorEventLoop` to `SelectorEventLoop`, which
cannot spawn subprocesses at all. Playwright starts its driver as a subprocess,
so the first page load dies with a bare `NotImplementedError` that names neither
uvicorn nor the flag. Linux is unaffected, so this only bites in local
development.

Multiple workers are wrong here for a second reason regardless of platform: each
would hold its own browser and its own copy of the daily budget below.
"""

from __future__ import annotations

import hmac
import json
import re
from datetime import date

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from job_source_agent import pipeline
from job_source_agent.config import (
    CREDITS_PER_URL,
    DEMO_ACCESS_KEY,
    DEMO_DAILY_CREDITS,
    DEMO_MAX_URLS,
    NTFY_TIMEOUT,
    NTFY_TOPIC,
    NTFY_URL,
)
from job_source_agent.models import JobSourceResult

app = FastAPI(title="Job source agent", docs_url="/docs")

JOB_URL_RE = re.compile(r"linkedin\.com/jobs/view/\d+|[?&]currentJobId=\d+", re.IGNORECASE)


class Submission(BaseModel):
    """What the page posts: the raw contents of the textarea and the access key.

    `key` defaults to empty so a request without one is a normal refusal rather
    than a 422 from FastAPI, which would reach the page as an unreadable
    validation error instead of the sentence explaining what is missing.
    """

    urls: str
    key: str = ""


class KeyCheck(BaseModel):
    """What the unlock box posts: just the key it wants tested."""

    key: str = ""


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
        self.warned = False

    def _roll(self) -> None:
        """Start a new day's allowance if the date has changed. Called before
        every read so the budget never has to be reset by hand."""
        today = date.today()
        if today != self.day:
            self.day, self.spent, self.warned = today, 0, False

    def claim_warning(self) -> bool:
        """Report whether this is the first refusal of the day, and record that
        it happened. Returns True once per day and False every time after, so a
        client retrying against an empty budget cannot send a hundred alerts."""
        self._roll()
        if self.warned:
            return False
        self.warned = True
        return True

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


def key_ok(supplied: str) -> bool:
    """Report whether a submitted access key matches the configured one.
    Returns True for everything when `DEMO_ACCESS_KEY` is blank, which is how
    the gate is switched off for local development. `hmac.compare_digest`
    compares the two strings in constant time, so a caller cannot learn the key
    one character at a time by measuring how long each guess took."""
    if not DEMO_ACCESS_KEY:
        return True
    return hmac.compare_digest(supplied.strip(), DEMO_ACCESS_KEY)


async def notify(message: str) -> None:
    """Push one line to whatever phone is subscribed to `NTFY_TOPIC`, and do
    nothing at all if no topic is configured. Returns None and swallows every
    error, because a notification service being unreachable must never take the
    demo down with it; `async with` closes the HTTP client afterwards without
    blocking the event loop while the request is in flight."""
    if not NTFY_TOPIC:
        return
    try:
        async with httpx.AsyncClient(timeout=NTFY_TIMEOUT) as client:
            await client.post(
                f"{NTFY_URL.rstrip('/')}/{NTFY_TOPIC}",
                content=message.encode("utf-8"),
                headers={"Title": "Job source agent", "Priority": "high"},
            )
    except Exception:
        pass


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


@app.post("/api/check")
async def check(body: KeyCheck) -> dict:
    """Say whether an access key is the right one, without running anything.
    Returns `{"ok": true}` or `{"ok": false}` so the unlock box can tell someone
    their key is wrong straight away, rather than letting them paste ten URLs
    and wait before finding out."""
    return {"ok": key_ok(body.key)}


@app.post("/api/resolve")
async def resolve(submission: Submission) -> StreamingResponse:
    """Take the pasted URLs and stream a result for each one. Refuses before
    spending anything when the access key is wrong, when there is nothing valid
    to run, when more than `DEMO_MAX_URLS` were given, or when the day's credit
    budget is gone.

    The key is checked here and not only on the page, because the page can be
    skipped entirely -- anyone can post straight to this endpoint.
    """

    def refusal(message: str, status: int = 200, **extra) -> StreamingResponse:
        body = json.dumps({"type": "error", "message": message, **extra})
        return StreamingResponse(
            iter([body + "\n"]),
            media_type="application/x-ndjson",
            status_code=status,
        )

    if not key_ok(submission.key):
        return refusal(
            "That access key is not right. Use the key that came with the link.",
            status=401,
            unauthorized=True,
        )

    urls, rejected = parse_urls(submission.urls)

    if not urls:
        return refusal(
            "No LinkedIn job URLs found. They look like linkedin.com/jobs/view/1234567890.",
            rejected=rejected,
        )
    if len(urls) > DEMO_MAX_URLS:
        return refusal(
            f"{len(urls)} URLs given; this demo takes up to {DEMO_MAX_URLS} at a time.",
            rejected=rejected,
        )
    if not budget.take(len(urls) * CREDITS_PER_URL):
        # Fires at most once a day. Someone retrying against an empty budget
        # would otherwise send a notification per attempt.
        if budget.claim_warning():
            await notify(
                f"Daily lookup budget spent. {budget.remaining} credits left, resets tomorrow."
            )
        return refusal(
            f"The demo's daily lookup budget is spent ({budget.remaining} credits left). "
            "It resets tomorrow.",
            rejected=rejected,
        )

    return StreamingResponse(stream(urls, rejected), media_type="application/x-ndjson")


@app.get("/health")
async def health() -> dict:
    """Report that the app is up, how much budget is left, and whether a key is
    needed to spend it. Says only that a key is required, never what it is."""
    return {
        "ok": True,
        "credits_left": budget.remaining,
        "max_urls": DEMO_MAX_URLS,
        "key_required": bool(DEMO_ACCESS_KEY),
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    """Serve the single page, telling it whether the access gate applies. The
    flag is substituted into the HTML rather than fetched by the page, so the
    page never flickers between locked and unlocked while it works out which it
    is; `str.replace` is used instead of an f-string because the page's CSS is
    full of braces an f-string would try to read as fields."""
    return PAGE.replace("__KEY_REQUIRED__", "true" if DEMO_ACCESS_KEY else "false")


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
  input { flex:1; max-width:300px; padding:10px 12px; border:1px solid var(--line); border-radius:8px;
          background:var(--bg); color:inherit; font:14px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; }
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

  <div id="gate" hidden>
    <div class="card">
      <h3>Access key</h3>
      <p class="note">Every lookup spends real API credits, so this demo asks for the
      key that came with the link.</p>
      <div class="row">
        <input id="key" type="password" placeholder="access key" autocomplete="off" spellcheck="false">
        <button id="unlock">Unlock</button>
      </div>
      <p class="note" id="keyerr"></p>
    </div>
  </div>

  <div id="demo" hidden>
    <textarea id="urls" placeholder="https://www.linkedin.com/jobs/view/4427628688/
https://www.linkedin.com/jobs/view/4456337928/"></textarea>

    <div class="row">
      <button id="go">Find job boards</button>
      <span class="note" id="status">Up to 10 at a time. Around 10-30 seconds each.</span>
    </div>
  </div>

  <div id="out"></div>
</div>

<script>
// Substituted by index(). False when DEMO_ACCESS_KEY is unset, which is how the
// gate stays out of the way during local development.
const KEY_REQUIRED = __KEY_REQUIRED__;
const STORE = 'jsa_access_key';

const go = document.getElementById('go');
const out = document.getElementById('out');
const status = document.getElementById('status');
const gate = document.getElementById('gate');
const demo = document.getElementById('demo');
const keyInput = document.getElementById('key');
const keyErr = document.getElementById('keyerr');
const unlock = document.getElementById('unlock');

let accessKey = '';

// localStorage throws outright in some privacy modes rather than returning
// null, so every touch of it is wrapped. A demo that cannot remember a key is
// mildly annoying; one that fails to load at all is broken.
function readStored() {
  try { return localStorage.getItem(STORE) || ''; } catch (e) { return ''; }
}
function writeStored(value) {
  try {
    if (value) { localStorage.setItem(STORE, value); } else { localStorage.removeItem(STORE); }
  } catch (e) {}
}

function showDemo() { gate.hidden = true; demo.hidden = false; }

function showGate(note) {
  demo.hidden = true;
  gate.hidden = false;
  keyErr.textContent = note || '';
  keyInput.focus();
}

async function verify(candidate) {
  const res = await fetch('/api/check', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key: candidate})
  });
  const data = await res.json();
  return data.ok === true;
}

async function start() {
  if (!KEY_REQUIRED) { showDemo(); return; }

  // A key may ride in on the address bar, so one link is all Jobnova needs. It
  // is wiped from the bar as soon as it is read, so a screenshot of the demo
  // does not hand the key to whoever sees it.
  const fromUrl = new URLSearchParams(location.search).get('key');
  if (fromUrl) { history.replaceState(null, '', location.pathname); }

  const candidate = (fromUrl || readStored()).trim();
  if (!candidate) { showGate(''); return; }

  let good = false;
  try { good = await verify(candidate); } catch (e) { good = false; }

  if (good) {
    accessKey = candidate;
    writeStored(candidate);
    showDemo();
  } else {
    writeStored('');
    showGate(fromUrl ? 'The key in that link is not right.' : '');
  }
}

unlock.onclick = async () => {
  const candidate = keyInput.value.trim();
  if (!candidate) { keyErr.textContent = 'Enter the key that came with the link.'; return; }

  unlock.disabled = true;
  keyErr.textContent = 'checking...';
  try {
    if (await verify(candidate)) {
      accessKey = candidate;
      writeStored(candidate);
      keyErr.textContent = '';
      keyInput.value = '';
      showDemo();
    } else {
      keyErr.textContent = 'That key is not right.';
    }
  } catch (e) {
    keyErr.textContent = 'Could not reach the server.';
  } finally {
    unlock.disabled = false;
  }
};

keyInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { unlock.click(); } });

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
      body: JSON.stringify({urls, key: accessKey})
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
          if (ev.unauthorized) {
            accessKey = '';
            writeStored('');
            showGate(ev.message);
          }
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

start();
</script>
</body>
</html>
"""
