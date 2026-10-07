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

Parity is measured, not assumed, and it holds per User-Agent token. For a
multi-airport search Google serves a different board by the UA's token: under
`HeadlessChrome` the 300 cheapest rows across every airport pair, under plain
`Chrome` a curated ~75. Measured 2026-10-05 on NYC-LON 2026-11-04, same URL:
curl_cffi's own `Chrome/146` UA and a headed Chrome read the same 72 rows from
USD679; headless Chrome, and curl_cffi with only the token changed, read 300
rows from USD488 (TP212/TP1328 EWR-OPO-LGW). A single airport pair (BOS-LHR,
EWR-LGW, JFK-LAX) reads one board under either token. So every reader of a
search page sends the token: rung 1 on each GET (`_gflight_ids._SEARCH_PAGE_UA`,
curl_cffi's `chrome` UA with the token added, its version pinned by a test to
curl_cffi's default Chrome profile and set per request so no other request on
fli's session changes), headless Chrome by its own UA, and a `--gf-headed`
window through a CDP `Emulation.setUserAgentOverride` set once in
`_ensure_page`, which every later navigation on that page carries (booking,
sellers, explore and the price graph too). Never set a headless page to plain
`Chrome/`: that reads the curated board and loses the USD488 fare.
`tests/test_gf_rung_parity.py` replays the nine pages behind these figures.

Under the token Google sometimes serves a page with no `ds:1` on it. On
2026-10-05 that happened to 2 of 7 rung-1 reads of q01 and 1 of 4 headless
Chrome reads, and the next read of the same URL carried the board. No failing
body was captured. Rung 1 therefore reads such a page up to three times, each
with the token (`_gflight_ids._read_search_page`, raised as
`_BoardlessPageError`), and then refuses it. It never reads without the token:
that gets the curated board, which can leave out the fare rung 2 lists (USD679
where the token board starts at USD488). A `ds:1` that decodes to a layout we
cannot read is still refused at one read. Rung 2 navigates such a page once
more, two navigations in all (`_BOARDLESS_NAVIGATIONS`), since a navigation
costs seconds where a GET costs one request.

A page whose `ds:1` holds Google's server error in place of the board is typed
apart from one with no `ds:1` (`GfSearchServerError`; see
[gf_page_refusals_and_ds1.md](gf_page_refusals_and_ds1.md)). Rung 1 reads it
again after a pause, under `retry_throttled`; rung 2 navigates it once more at
once, two navigations in all, as it does a page with no `ds:1`.

A page of 300 raw rows, unread ones included, stopped at Google's cap
(`_ROW_CAP`), so its board records its highest fare as `Board.capped_at`, in
the page's own currency, and each path that shows it prints one stderr line:
`Google Flights stops at 300 rows for this search: fares above USD1006.00 may
be missing.` The currency is the page's, not the rows', since a filter can
leave only rows of another page; pages capped in two currencies name both
(`fares above EUR1006.00 and USD1006.00`). A round trip
names its outbound page's figure, and a board merged from pages the lowest in
each currency of every page read, one that holds no pin included. A board that shows separate
tickets takes the lower of its own figure and the Cheapest tab's. Each one-way
board a multi-city search or `--split` reads (`cli._one_way_boards`) prints its own
line, labeled by its slice (`Google Flights CDG→JFK one-way stops at ...`) or
leg (`return one-way`): once every leg answered, since the tickets drawn from
it stop at its cap, or alone when it holds no ticket. It is a
note, not a narrowing (`complete` stays true): on five same-run pairs every
curated row priced at or below the cap was on the token board at the same
price, and the curated-only rows started at USD1035. Two consequences follow.
The Google Flights link the CLI prints opens the curated board in the user's
own Chrome, so a multi-airport search can list a fare (USD488) that page does
not show. And a routing-filtered multi-airport search loses the curated-only
rows above the cap, which the cap line names, on a board the filter emptied
too. The merged table is the exception: with Google's board empty it shows
Matrix's rows, and prints no line. So does a search handed to Matrix, on every
hand-off arm: the cap bounds the board Matrix replaced, not Matrix's answer
(`tests/test_gf_throttle_handoff.py`). The cross-check document (`--enrich
--format json` or `envelope`) prints it, since its `search` half is Google's
board.

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
`_gf_browser.session(headed=…)`, and closed in `_gflight_query`'s `finally` —
the owning thread, on every path that leaves the query. Every leg of one
search runs in that thread, so all of a round trip's navigations share one
launch (and one "opening Chrome" line).

**A round trip costs one navigation for the outbound board, then one per pinned
return leg**, not four. How many pins that is — and every other page-fetch count
this backend can run up — is derived in the GETs table and the paragraph under
it in `docs/memories/gf_request_budget.md`, which is the authority for the
arithmetic; what follows is this rung's own measurement. The pin cap named there
is why the count stops growing with `-n`. Measured here with a recorder in place
of the session and a 30-row board on each leg: `-n 1` → 2, `-n 3` → 4, `-n 10`
→ 11, `-n 25` → 11, `-n 100` → 11. Eleven is therefore the ceiling for any
`-n`, so at the 30 s nav ceiling the worst case is 330 s. A page with no board
on it is navigated twice, so the eleven become at most 22 when every page comes
back boardless the first time. `atexit` gets only a
best-effort close, and is structurally same-thread: thread-local storage means
interpreter shutdown on the main thread cannot see a worker's session.

## Ctrl-C

**A SIGINT at any point of a browser search exits 130 within 0.06 s, on both
arms.** The session closes exactly once, and within about a tenth of a second
of that exit no Chrome survives and no `Singleton*` is left where it would block
the next search — Chrome's exit is asynchronous to the CLI's by design, so that
is a tail with a bound and not a property of the instant. A bound is all it is:
the check polls at 0.1 s, which cannot resolve a span of that size, and the
settle is load-dependent — a loaded machine moves it, so a second decimal here
would be arithmetic on the poll interval rather than a measurement. stdout carries nothing partial —
a table already painted before the interrupt is a whole answer and stays — and
stderr carries no line about the interruption at all, only the notices the run
had already printed. Measured across fourteen cases: both arms at
0.3/0.8/1.2/2.5 s and mid-pin-loop, `--format json`, one `--gf-headed` window,
and one case per arm whose second Ctrl-C, aimed 50 ms into the shutdown,
arrived after it was already over — so those two graded as ordinary interrupts.
A second Ctrl-C during the shutdown changes none of it, and that is held by the
disposition rather than by a live case: the handler installs `SIG_IGN` before
it does anything else, and the shutdown is over inside 60 ms — faster than a
second signal can be aimed into it. The ignore stays only when a driver was
open; with none (an `auto` search that never escalated) the handler gives the
previous disposition back after its stop, so a second Ctrl-C ends the wait on
the worker.

The mechanism, because a hang here is otherwise re-derived from scratch: an
interrupt that unwinds a patchright call kills the greenlet running that call's
event loop, so every later call through the sync API posts to a loop nobody
drives and spins on a dead greenlet — which is why teardown after an interrupt
kills the driver process rather than closing anything. Chrome exits with it,
being the peer of the pipe the driver holds.

**A process-group signal cannot test any of this.** patchright's node driver
installs its own SIGINT handler and closes Chrome itself, so `killpg` — and a
bare Ctrl-C in a job-controlled shell — repairs the defect it is meant to
detect. Signal the CLI process alone. Two more measurement traps sit next to it:
`subprocess.communicate()` returns when the last holder of the inherited stderr
fd goes away, and the driver and Chrome hold it, so it timed a 0.05 s exit at
4.08 s — poll for the exit instead; and a `Singleton*` count taken with
`pgrep -f gf-browser-profile` counts the counting command.

A Ctrl-C therefore leaves no `Singleton*` behind, which narrows what the lock
message below means: the "interrupted run" it names is now one killed from
outside, where the residue is real and the user does have to clear it.

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

That same lock is why a multi-cabin search cannot FAN OUT at rung 2: a thread
per cabin is a session per cabin, and the second one fails on the first one's
lock. It runs the cabins in series instead, through one session — see "What it
costs" for what that trade buys and what it charges.

## Refusals and the two renderers

`GfBrowserUnavailableError(GfBackendError)` covers every way rung 2 fails to
produce bytes — no patchright, no Chrome, locked profile, nav timeout, null
response, unreadable body. `remedy` is a separate attribute, and **both**
renderings in `_gf_refusal` have to carry it. The full `message` gets it free
inside `str(e)`. The one-line `note` must append both `e.reason` AND `e.remedy`
itself, and that is the one that matters most: the enrich path is the default
search and prints only the note. Built from the remedy alone it read "Retry"
for all four launch-time failures at once — one useless line, with the only
actionable one (install Chrome) never named.

The multi-cabin downgrade line is the only one that leads with its verdict. It
is not a refusal — the search succeeds over http — so the verdict comes first
and the reason and the remedy follow it. Half that remedy is `--gf-transport
http`, the move the line has just announced; the other half, install Chrome and
point the binary at it, is what a user whose http rung is ALSO refused has left
to try. Leading with the fact is what makes it survive a narrow terminal: a
phrase at the head of a line cannot be broken by any width, and a phrase
appended after a driver's own sentence can be, at widths that have nothing to do
with its length. Measured on the three longest refusals this line carries, at
1000, 400, 200 and 80 columns: the phrase renders whole in all twelve.

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

A driver that cannot START is a different refusal from a Chrome that cannot
launch: `start()` raising means no launch was tried, so `_ensure_page` raises
`_driver_failure`, whose remedy is `_INSTALL_HINT` (patchright's own install)
rather than `_LAUNCH_REMEDY`. patchright also parks the start error where
nothing awaits it — the transport's `on_error_future` when node cannot be
spawned, the connection's `init` task when node runs and exits — so asyncio
would print it as "exception was never retrieved" after the refusal.
`_quiet_driver_failure` gives the loop patchright made for that manager an
exception handler that drops those reports: one handler covers every future on
the loop, where retrieving each would take one private attribute per place
patchright parks an error. It reads `_loop` and `_own_loop`, guarded like
`_driver_process_id`, and leaves a loop patchright did not make (the caller's)
alone.

## What it costs

Measured 2026-09-02 on this Mac: cold launch plus one navigation, start to
rendered table, **under 3 s**; a `-n 1` round trip (launch plus two navigations)
under 7 s. That is far cheaper than the ~60 s the design brief budgeted, and
still far cheaper than Matrix (~45 s). The 30 s nav timeout is a ceiling, not a
typical cost — but see the navigation count above before assuming a default
round trip is as cheap as the one that was timed.

**Multi-cabin is serial, and that is the cost.** `--gf-transport browser` with
a multi-cabin `--cabin` list runs the cabins one at a time, through one session,
on the thread that called the search. Measured 2026-09-07: a two-cabin round
trip at `-n 6` is **10.5 s** and a three-cabin one **14.1 s**, against **1.3 s**
for the same shape on rung 1. One profile is what makes it serial, and `--help`
quotes the price so nobody buys the rung expecting the fan-out's latency.

A Ctrl-C anywhere in that sequence is answered where it lands: the loop runs on
the thread the signal is delivered to, with no worker to wait out, and the guard
is entered ONCE for the whole list. A second entry would clear the interrupt
latch and re-arm a SIGINT the first cabin had set to be ignored.

When the browser cannot open at all and no cabin has been served, the whole
fan-out runs rung 1, says so once on stderr and answers; the fall-through
coerces the transport, or every cabin attempts the rung that just failed and the
run ends with an empty stdout. Once a cabin HAS rows, a later failure stays that
cabin's note — re-running the fan-out would discard them, and a table whose
columns came from two different rungs is not one answer.

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

`auto` escalates a search to this rung once a throttle outlasts rung 1's
ladder; the order is in [gf_throttle_ladder.md](gf_throttle_ladder.md). Also
out, each a bd follow-up under `work-udpp1`: booking options and parallel tabs. The calendar
date grid through the browser is in
[gf_date_grid.md](gf_date_grid.md) (`--fast`).
