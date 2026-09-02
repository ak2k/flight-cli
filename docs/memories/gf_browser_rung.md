# GF browser rung — real Chrome behind the same search page

Rung 2 of the Google Flights search transport: `--gf-transport browser` drives a
real Chrome (patchright, `channel="chrome"`) to the URL rung 1 already GETs, and
hands the navigation's response body to the same parser. Read before touching
`_gf_browser.py`, `_gflight_ids._one_call_laddered` / `_rows_from_page_html`, or
the `--gf-transport` plumbing in `cli`.

## Why a second rung at all

curl_cffi's chrome fingerprint passes Google's edge — we reach the backend and
get a structured refusal, never a CAPTCHA — but a thin client earns a small rate
budget, and past it the page comes back as a `/sorry/` interstitial. The budget
is keyed on **client context, not IP**: measured 2026-06-15, patchright with
`channel="chrome"` pulled 10/10 results from an IP that was *simultaneously*
code-13 throttling curl_cffi, re-probed the same minute. Everything else about
that history (the batchexecute `bgr` token, the abandoned header-mirroring and
token-replay designs) is in bd `work-udpp1`.

## One parser, two rungs

`_one_call_laddered(filters, transport)` picks the rung. Both hand a
`PageFetch(html, final_url, status_code)` to `_rows_from_page_html`:

```
rung 1  _fetch_page ─────────────┐
        (curl_cffi GET)          ├──▶ _rows_from_page_html ──▶ [GFlightWithId]
rung 2  GfBrowserSession.get_html┘        (the only parser)
```

**A rung supplies bytes; it never interprets them.** The lone exception is the
429 fli hides: `Client.get` calls `raise_for_status()` itself, so on rung 1 a
429 never arrives as a response at all and `_fetch_page` is the only place that
can name it. Everything visible in the bytes — the `/sorry/` redirect, the body
marker, a missing `ds:1`, the status Chrome does report — is decided
downstream, so both rungs reach identical verdicts from identical evidence.

Classification order inside `_rows_from_page_html` is load-bearing: throttle
(`/sorry/` or 429) → any other non-2xx as `GfPageShapeError` → missing `ds:1`
with consent markers as `GfConsentError` → missing `ds:1` as a shape error →
rows. Every one of those would otherwise decode as zero rows and read to the
user as "no flights on this route".

Parity is measured, not assumed. 2026-09-02, JFK-LAX 2026-10-14, same URL, same
minute: curl_cffi 30 rows / first `flight_id` `fuqYmc`; headless Chrome 30 rows
/ `fuqYmc`; headed Chrome the same. `research/probe_gf_browser_page.py`
(uncommitted) re-runs the comparison.

## Four settings that look arbitrary and are not

- **`response.text()`, never `page.content()`.** `content()` serializes the live
  DOM, which Google's own JavaScript has already rewritten. `text()` is the HTTP
  response body — the same bytes curl_cffi sees, `ds:1` blob intact.
- **`wait_until="commit"`.** `response.text()` waits for the body regardless, so
  a later readiness state buys nothing. Measured 2026-09-02: `commit` and
  `domcontentloaded` returned the same 30 rows and the same first id.
- **No warm-up navigation.** The June RPC experiment always landed on
  `google.com/travel/flights` first. Probed 2026-09-02 on two *fresh* profiles
  (headless and headed): the `tfs=` deep link returned rows directly, no consent
  landing, so the warm-up is a page load with nothing to show for it. It was
  also never the consent fix it looked like — a navigation does not *accept*
  consent. If rung 2 ever starts landing on the consent wall (EU/EEA egress is
  the candidate), re-run the probe before adding it back.
- **`channel="chrome"`, not a bundled Chromium.** The installed real Chrome is
  the whole point; a bundled build has the same thin fingerprint rung 1 already
  has. `FLIGHT_CLI_GF_BROWSER_BIN` overrides it with an explicit binary and
  drops the channel (patchright rejects both together).

## Session lifetime: thread-local, closed by its owner

The CLI runs the query inside `anyio.to_thread.run_sync`, and a playwright
object touched from a thread other than its creator raises `greenlet.error` and
strands a live Chrome. So the session is **thread-local**, created lazily by
`_gf_browser.session(headed=…)`, and closed in `_gflight_results`' `finally` —
the owning thread, on every path that function returns from. Every leg of one
search runs in that thread, so a round trip's four navigations share one launch
(and one "opening Chrome" line). `atexit` gets only a best-effort close, and is
structurally same-thread: thread-local storage means interpreter shutdown on the
main thread cannot see a worker's session.

Ctrl-C is outside any `finally`'s reach while a thread sits in `page.goto`.
Probed 2026-09-02 (`kill -INT` mid-navigation on the `--fast` path): exit 130,
**no orphan Chrome, no `Singleton*` left behind** — the interrupt unwinds
through the `finally` — plus one line of patchright teardown noise on stderr
(`Future exception was never retrieved … TargetClosedError`). A `SIGKILL` would
strand both, which is why the lock message names that case too.

## The profile, and its lock

`<cache>/gf-browser-profile` (`MATRIX_CACHE_DIR` moves it). Deliberately **not**
`pp.auth.BROWSER_PROFILE_DIR`: that one holds a logged-in PointsPath session,
which a search backend has no business reading, and sharing it would collide
with the login flow over Chromium's single-instance lock.

Chromium single-instances a profile directory, so a second `flight` at rung 2
fails. Confirmed live 2026-09-02 — patchright reports *"Failed to create a
ProcessSingleton for your profile directory"* — and `_profile_is_locked` turns
it into a refusal naming both ways in (a concurrent run, or an interrupted one)
and the recovery: remove `<profile>/Singleton*`. Note `Path.exists()` is the
wrong test there: `SingletonLock` is a symlink to `<host>-<pid>` and reads as
missing once that pid is gone, which is precisely the interrupted-run case.

That same lock is why multi-cabin stays rung-1-only: its fan-out would want
concurrent sessions.

## Refusals and the two renderers

`GfBrowserUnavailableError(GfBackendError)` covers every way rung 2 fails to
produce bytes — no patchright, no Chrome, locked profile, nav timeout, null
response, unreadable body. It carries `remedy` text **inside the message**,
because `cli`'s `_gf_refusal` renders an unrecognized subclass as `str(e)` and
nothing else; a remedy kept in the renderer would never print. Both the note and
the full message have a `GfBrowserUnavailableError` arm.

## What it costs

Measured 2026-09-02 on this Mac: cold launch plus one navigation, start to
rendered table, **under 3 s**; a round trip's launch plus four navigations under
7 s. That is far cheaper than the ~60 s the design brief budgeted, and still
far cheaper than Matrix (~45 s). The 30 s nav timeout is a ceiling, not a
typical cost.

## Tests never launch a browser

`tests/conftest.py` replaces `_gf_browser._playwright_factory` for **every**
test with a callable that `pytest.fail`s; `@pytest.mark.gf_browser` opts out and
those tests drive a fake playwright object graph. `pytest.fail` raises a
`BaseException` on purpose — production code wraps launch failures in `except
Exception`, and a guard the code under test could swallow would be no guard.

Two seams make the ladder testable, and both are easy to break by "tidying":
`_gflight_ids` reaches rung 2 as `from . import _gf_browser` then
`_gf_browser.session(...)`, so the attribute is looked up per call; rewriting it
as `from ._gf_browser import session` would silently defeat every ladder test.
And `_one_call_with_retry` calls the module-global `_one_call`, which is what
lets the throttle tests substitute it.

## What is not here

`auto` is accepted and documented as identical to `http`; escalate-on-persistent-
throttle plus the once-per-process latch is the follow-up. Also out, each a bd
follow-up under `work-udpp1`: the calendar date grid through the browser,
booking options, multi-cabin at rung 2, and parallel tabs.
