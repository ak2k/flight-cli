# SPA capture & RE workflow — Matrix and Google Flights

How to drive Matrix's and Google Flights' SPAs headlessly to capture
URL-state schemas, wire payloads, and itinerary identifiers. Use this
when our generators drift from the live SPAs (round-trip URL bug,
Google `tfs=` schema changes, Matrix `/itinerary` IDs, etc.) — the
captured emission is ground truth.

## Two URL-state schemas (don't conflate them)

- **Matrix `search=`** — base64 of a small JSON object. Round-trips
  through the SPA's form (one slice with `departureDate` + `returnDate`
  in round-trip, N slices in multi-city, see `links.py` for the exact
  shape and `wire_format_quirks.md` for traps).
- **Google Flights `tfs=`** — base64 of a protobuf. Two flavors:
  search-preload (slices with origin/dest/date) and pinned (slices
  also carry per-segment carrier + flight#). RE'd schema lives in
  `links.py` as `_PbWriter` / `_encode_gflight_pinned_tfs`.

Both are unrelated to the **wire API** payloads (`POST /v1/search` for
Matrix; Google's internal search). The SPA translates URL state →
wire on submit; our generators bypass the SPA and build the wire
payload directly. Drift in either layer is independent.

## Capture chassis: `research/record_user_session.py`

Headed Playwright session that logs every `framenavigated`, decodes
`tfs=`/`search=` payloads to sidecar files, and captures `/v1/search`
POSTs from the alkalimatrix endpoint live (no need to dig the HAR
afterward). One file, four modes — each builds on the URL-transition
logger, so any mode produces a complete trail in `research/capture/`.

| Mode | Invocation | What it does |
|---|---|---|
| Manual | `record_user_session.py [URL]` | Opens browser, you drive, close when done |
| Auto-click | `record_user_session.py URL --auto` | Clicks first itinerary card; for `/flights` pinning RE |
| Form-fill (round-trip / one-way) | `record_user_session.py /search --fill=ORIG,DEST,DEP,RET` | Fills the search form, clicks Search, captures emitted URL |
| Form-fill (multi-city) | `--fill-mc=O/D/DATE,O/D/DATE,...` | Clicks Multi City tab, fills N legs with Add Flight, captures URL |
| Drive past Search | add `--pin` to either fill mode | Click cheapest result row, reach `/itinerary`, capture pinned URL |
| Snapshot loaded URL | `record_user_session.py URL --snapshot` | Goes to URL, dumps form/body state, exits — fast diff vs our emission |

The dates use `YYYY-MM-DD` on the CLI; the driver converts to MM/DD/YYYY
when typing. `research/capture/` is gitignored; promote ground truth
into `tests/fixtures/matrix_url/` (one fixture per captured URL with
a header comment recording the exact command + capture timestamp + the
captured-then-verified click semantics).

## SPA-driving gotchas (battle-tested)

1. **Matrix's date-range picker opens a modal overlay** that intercepts
   all subsequent clicks. `.fill()` after `.click()` triggers it; set
   `<input>.value` directly via JS and dispatch `input`/`change`/`blur`
   instead. See `_set_date()` in the driver.
2. **Airport chip-input is finicky on per-leg fills** (multi-city). Use
   `.type(code, delay=150)` not `.fill()`, sleep ≥1s after typing for
   the autocomplete list to populate, then press `Enter` (not click
   `mat-option:has-text` — that races on Add Flight'd legs).
3. **Direct `goto('/flights?search=…')` with a long URL gets `ERR_ABORTED`**
   because the SPA does a client-side reload mid-load. Drive from
   `/search` and let the SPA navigate itself after Search click.
4. **The SPA renders prices as `$680`, not `USD680`.** `document.body.innerText`
   pattern: `\$\d[\d,]*`. Don't waste a debug round looking for `USD\d`.
5. **Body text misses virtualized result rows.** `document.body.innerText` is
   ~2.6 KB on a 18-solution result page; the rows themselves render but
   aren't in the flat innerText. Use `document.querySelectorAll('tr.mat-mdc-row a.mdc-button')`
   and filter by `^\$\d` on the anchor's text.
6. **The clickable element is `<a class="mdc-button">`**, not the `<tr>` or
   `<td>`. Cursor is `pointer` on the anchor; `auto` everywhere else
   up the row. Clicking the row does nothing.
7. **Matrix's API surfaces input errors as HTTP 200 with `{"error":{…}}` payloads**
   (`validation_through_errors.md`). The captured POST body is the
   primary diagnostic; the response is usually the explanation.

## Matrix `/itinerary` pinning — ID flow

The `/itinerary` route URL state adds a `solution` block to the regular
`search=` payload. Three IDs come from the originating `/v1/search`
response:

| URL `solution.<key>` | API response field | Captured on |
|---|---|---|
| `sessionId` | `body.session` | `SearchResult.session` |
| `rh`        | `body.solutionSet` | `SearchResult.solution_set` |
| `Si`        | `body.solutionList.solutions[i].id` | `Itinerary.id` |
| `xd`        | (constant `true`) | — |

Sub-key order matters for byte-exact reproduction: `sessionId`, `xd`,
`rh`, `Si` (the order the SPA emits, not alphabetical).

**Session-scoped lifetime.** IDs expire ~10–30 min after the originating
search. Stale URLs render with `Input error for "bookingDetails"
(SolutionSummarizer), "x.solution" is required` and the SPA shows no
booking details. There is no longer-lived form (no carrier + flight#
encoding like Google Flights `tfs=` pinned URLs). For durable pinning,
the open exploration path is RE'ing Matrix's own "Open in Google
Flights" button on the `/itinerary` page, which translates the
selection into a `tfs=` pinned URL.

## Recapture workflow when our generators drift

1. Pick a small canonical example (HNL→MIA round-trip 10/14–10/24 is the
   default — it's the same example the existing fixtures use, so diffs
   stay surgical).
2. Run the matching driver mode. Compare the captured URL's decoded
   JSON against our generator's output (decode both via
   `base64.b64decode(parse_qs(urlparse(u).query)['search'][0])`).
3. Diff is usually one of: a renamed field, a flipped two-slice ↔
   one-slice round-trip encoding (the 2026-05 drift), a new top-level
   block (the `solution` block on `/itinerary`), or a swapped sub-key
   order. Anchor the new shape with a byte-exact regression test
   against the captured fixture.
4. Promote captured URLs to `tests/fixtures/matrix_url/` with a header
   comment recording the exact `record_user_session.py` command, the
   capture directory (e.g. `research/capture/manual-…`), and what
   click sequence produced the final URL. The header is load-bearing —
   without it the next agent can't reproduce the capture.

## Anti-patterns

- Don't rely on `document.body.innerText` for detecting "results
  rendered" — Material's virtualized lists fool it.
- Don't try direct-`goto` to `/flights?search=<long-URL>` to skip the
  fill step; the abort + reload makes everything downstream flaky.
- Don't conflate the SPA's URL-state schema with our wire-API payload
  schema. They drift independently; passing one through the other will
  silently corrupt either side. The wire layer is locked by
  `tests/fixtures/specific_jfk_lhr_*.json`; the URL state by
  `tests/fixtures/matrix_url/`.
- Don't add a click handler that walks up looking for a clickable
  ancestor of `$\d` text — you'll hit the matrix-grid filter cell, not
  the results-table row. Target `a.mdc-button` directly.
