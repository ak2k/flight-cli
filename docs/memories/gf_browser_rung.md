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

**A rung supplies bytes; it never interprets them.** Not even the status:
rung 1 goes around fli's `Client.get` and the `raise_for_status()` inside it, so
a 429 arrives as a response there exactly as it does on rung 2. Everything
visible in the bytes — the `/sorry/` redirect, the body marker, a missing
`ds:1`, the status itself — is decided downstream, so both rungs reach
identical verdicts from identical evidence.

Classification order inside `_rows_from_page_html` is load-bearing: throttle
(`/sorry/` or 429) → any other non-2xx as `GfUpstreamStatusError` → missing
`ds:1` with consent markers as `GfConsentError` → missing `ds:1` as a shape
error → rows. Every one of those would otherwise decode as zero rows and read to
the user as "no flights on this route". The 429 test has to come first, or the
non-2xx branch claims it and loses the one fact a caller can act on.

A non-2xx is deliberately NOT a shape error. "Google changed the page" sends a
reader to re-derive the extract; "Google declined to serve" is usually an outage
and needs no code change at all.

Parity is measured, not assumed. 2026-09-02, JFK-LAX 2026-10-14, same URL, same
minute: curl_cffi 30 rows / first `flight_id` `fuqYmc`; headless Chrome 30 rows
/ `fuqYmc`; headed Chrome the same. `research/probe_gf_browser_page.py`
(uncommitted) re-runs the comparison.

## Four settings that look arbitrary and are not

- **`response.text()`, never `page.content()`.** `content()` serializes the live
  DOM, which Google's own JavaScript has already rewritten. `text()` is the HTTP
  response body — the same bytes curl_cffi sees, `ds:1` blob intact.
- **`wait_until="domcontentloaded"`, not `commit`.** Both return the same 30
  rows and the same first id (measured 2026-09-02), so this is not about what
  arrives — it is about what bounds it. `response.text()` takes **no timeout at
  any layer**: patchright sends the body request with no deadline, and the
  driver arms its timer only when one is supplied, so a stalled body read hangs
  forever with nothing to interrupt it. `commit` returns before the body exists
  and leaves that read outside `goto`'s 30 s ceiling; `domcontentloaded` puts it
  inside. That ceiling is the only timeout rung 2 has, and a default round trip
  makes 11 of these reads.
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
search runs in that thread, so all of a round trip's navigations share one
launch (and one "opening Chrome" line).

**A round trip costs one navigation for the outbound board, then one per pinned
return leg**, not four. How many pins that is — and every other page-fetch count
this backend can run up — is derived in the GETs table and the paragraph under
it in `docs/memories/gf_routing_and_carriers.md`, which is the authority for the
arithmetic; what follows is this rung's own measurement. The pin cap named there
is why the count stops growing with `-n`. Measured here with a recorder in place
of the session and a 30-row board on each leg: `-n 1` → 2, `-n 3` → 4, `-n 10`
→ 11, `-n 25` → 11, `-n 100` → 11. Eleven is therefore the ceiling for any
`-n`, so at the 30 s nav ceiling the worst case is 330 s. `atexit` gets only a
best-effort close, and is structurally same-thread: thread-local storage means
interpreter shutdown on the main thread cannot see a worker's session.

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
response, unreadable body. `remedy` is a separate attribute, and **both**
renderings in `_gf_refusal` have to carry it. The full `message` gets it free
inside `str(e)`. The one-line `note` must append both `e.reason` AND `e.remedy`
itself, and that is the one that matters most: the enrich path is the default
search and prints only the note. Built from the remedy alone it read "Retry" for
all four launch-time failures at once — one useless line, with the only
actionable one (install Chrome) never named.

**Every interpolated string in a refusal is `escape`d.** The app runs typer with
`rich_markup_mode="rich"`, so `[browser]` in a remedy is read as a style tag and
DELETED: the install hint printed `uv pip install 'flight-cli'`, a command that
installs the package without the extra and leaves the user exactly where they
started. Worse, patchright's driver text is arbitrary — a `[/x]` it never opened
raises `MarkupError` from `print`, turning a typed refusal that should degrade
to Matrix into a crash. Literal markup in those templates is ours and stays
unescaped; anything off an exception is data. Same trap in `--gf-transport`'s
help string, where the fix is a backslash in the literal (`flight-cli\[browser]`).

A launch failure is not diagnosed from the profile state alone. A `SIGKILL`ed
run leaves `Singleton*` behind indefinitely, so `_profile_is_locked` keeps
returning true and every later failure read as a lock — a user with no Chrome
would delete lock files, retry, and hit the same wall with the real cause still
unnamed. The driver text has to name the singleton too. And `_profile_is_locked`
answering an `OSError` with "unlocked" is the deliberate direction: erring the
other way refuses the rung and tells the user to delete files the process just
proved it cannot see.

## What it costs

Measured 2026-09-02 on this Mac: cold launch plus one navigation, start to
rendered table, **under 3 s**; a `-n 1` round trip (launch plus two navigations)
under 7 s. That is far cheaper than the ~60 s the design brief budgeted, and
still far cheaper than Matrix (~45 s). The 30 s nav timeout is a ceiling, not a
typical cost — but see the navigation count above before assuming a default
round trip is as cheap as the one that was timed.

**Single-cabin only.** `--gf-transport browser` with a multi-cabin `--cabin`
list prints a dim line and uses http. The fan-out runs a thread per cabin, and
Chromium single-instances the profile directory, so the second cabin would fail
on the first one's lock — one profile is the constraint, not a missing wire.

## The shared leaf

`PageFetch`, `cache_dir()` and the transport vocabulary (`GfTransportMode`, the
`TRANSPORT_*` constants, `VALID_TRANSPORT_MODES`) live in `_gf_common.py`, which
imports nothing from this package. The first two were in `_gflight_ids`, and that
made the two rungs import each other: `_gf_browser` needs the record type the
parser reads and the cache dir its profile sits under, while `_gflight_ids`
reaches `_gf_browser` to run rung 2. Only the deferred import inside
`_one_call_browser` hid it, and both are defined well below that module's import
block — so hoisting the import to the top of the file raised `ImportError` on a
half-initialized module, a failure that looks exactly like a broken optional
dependency and sends the reader somewhere else entirely.

With the cycle gone the deferred import went too. `_gflight_ids` imports
`_gf_browser` at the top of the file like any other module: it costs 0.2 ms and
pulls no optional dependency, because the guarded `patchright` import lives
inside `_playwright_factory` and runs at launch, not at import. Deferring the
import would not add to that: `import flight_cli._gf_browser` leaves
`sys.modules` patchright-free with the extra installed.

The vocabulary is here for a second reason: `cli` validates `--gf-transport` on
EVERY search, Matrix-only ones included, and `_gflight_ids` costs fli's import
(~95 ms). A standard-library-only leaf is free, so `cli._resolve_gf_transport`
derives the modes it accepts from `get_args(GfTransportMode.__value__)` and
returns the narrowed type — one definition, no `cast` at any call site.

`_gf_errors` solves the same shape of problem for the refusal types and says so
in its own docstring; these are values rather than exceptions, so they get a leaf
named for what they are.

`import flight_cli.cli` still loads neither `_gflight_ids` nor `_gf_browser` —
`_gf_errors` and `_gf_common` alone, for the exception catches and the transport
vocabulary, neither of which imports anything outside the standard library. That
is what keeps fli's ~95 ms off a Matrix-only search, and
`test_resolving_a_transport_does_not_load_the_google_flights_stack` holds the
line in a subprocess.

## Tests never launch a browser

`tests/conftest.py` replaces `_gf_browser._playwright_factory` for **every**
test with a callable that `pytest.fail`s. `pytest.fail` raises a `BaseException`
on purpose — production code wraps launch failures in `except Exception`, and a
guard the code under test could swallow would be no guard.

`@pytest.mark.gf_browser` opts out, and almost nothing needs it: a test that
installs its own fake playwright has already replaced the same seam, so the
marker would only widen the hole. It is for the one test that calls the real
`_playwright_factory` to prove a missing patchright names its install.

The opt-out reads the **marker** (`request.node.get_closest_marker`), not
`request.keywords`. `keywords` also carries the node's name, its parametrize ids
and its containing directory, so on that predicate a test merely *parametrized*
with the string `gf_browser` disarmed the guard for itself with no marker in
sight — and a directory rename would have done it to a whole subtree.

Two seams make the ladder testable, and both are easy to break by "tidying":
`_gflight_ids` reaches rung 2 as `from . import _gf_browser` then
`_gf_browser.session(...)`, so the attribute is looked up per call. Rewriting it
as `from ._gf_browser import session` binds the function at import time, and
every ladder test that substitutes `_gf_browser.session` would then be
monkeypatching a name the code under test no longer reads — so the tests keep
passing while proving nothing.
And `_one_call_with_retry` calls the module-global `_one_call`, which is what
lets the throttle tests substitute it.

## What is not here

`auto` is accepted and documented as identical to `http`; escalate-on-persistent-
throttle plus the once-per-process latch is the follow-up. Also out, each a bd
follow-up under `work-udpp1`: the calendar date grid through the browser,
booking options, multi-cabin at rung 2, and parallel tabs.
