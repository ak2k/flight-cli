# flight-cli — Project Memory Index

Topical deep-dives. CLAUDE.md is the always-loaded overview; these files
go into detail and are loaded on demand.

- [wire_format_quirks.md](wire_format_quirks.md) — Per-mode field rules,
  `routeLanguage` vs `commandLine`, summarizer ordering, page semantics,
  timeRanges flexibility. Read before touching `wire.py` or `links.py`. It
  also carries what a black-box caller can read off `flight calendar` — which
  exit code means what, and which stream carries the answer — a CLI contract
  rather than a wire one, and closer to `calendar_two_phase.md`'s subject than
  to the rest of this entry's: read it before touching a calendar exit path too.
  Its last section does the same for `flight search --format json`: which
  document shape each flag set writes, and what a `Using Matrix:` line says.
- [routing_language.md](routing_language.md) — Full grammar of the
  `routeLanguage` field (`LH+`, `BA AA`, `F* X:LHR F*`, alliance codes,
  per-segment carrier/airport filters). What goes into `--routing`.
- [extension_codes.md](extension_codes.md) — Full table of `commandLine`
  extension codes (`MAXSTOPS`, `MAXDUR`, `ALLIANCE`, `-OVERNIGHTS`,
  `+CABIN`, fare-basis, `AIRCRAFT`, etc.). What goes into `--extension`.
- [matrix_help_docs.md](matrix_help_docs.md) — Verbatim copy of
  matrix.itasoftware.com's in-app help dialog (Itineraries / Faring /
  Aircraft Types tabs), captured by paste from the SPA UI. Canonical
  reference when the curated tables in `extension_codes.md` /
  `routing_language.md` are ambiguous.
- [airport_groups.md](airport_groups.md) — Metro IATA codes Matrix accepts
  natively (NYC, LON, PAR…) and manual region expansions (Europe, US
  East Coast, East Asia…) for users who ask "find me a flight to
  Europe".
- [api_key_bootstrap.md](api_key_bootstrap.md) — 7 keys in Matrix's SPA
  bundle, how the regex targets the prod one, cache + env-var fallback,
  what to do if Google rotates.
- [calendar_two_phase.md](calendar_two_phase.md) — Calendar mode + the
  `calendarFollowup` second-phase request (how the SPA's date-picker
  click triggers a different shape on the same endpoint).
- [validation_through_errors.md](validation_through_errors.md) — Matrix
  surfaces input-validation as HTTP 200 + `{"error":...}` payloads (not
  4xx). How `MatrixApiError` catches it; useful error patterns to
  recognize.
- [public_alkali_wrapper.md](public_alkali_wrapper.md) — As far as 2026-05
  web search shows, this project is the only public wrapper of the Alkali
  endpoint. Implications: our fixtures are the spec; forward-compat is
  on us. What to do if Matrix changes shape.
- [pp_on_gflight.md](pp_on_gflight.md) — Why PointsPath overlay now
  rides both backends. Decision: `enable_matching=False` + the existing
  matcher keys, not the matched-Google-flight-id path. Empirical evidence
  behind the choice and the upgrade path if it turns out worth it later.
- [award_airport_sets.md](award_airport_sets.md) — An award search over
  `JFK,EWR` or a metro code asks the providers one airport pair at a time:
  per-pair cost (PointsPath cabins × airlines requests, one seats.aero quota
  unit), the cap of 8 pair queries a search and its order (cash-flown pairs
  first, a pair per leg per round), the stderr line and `pairs_not_asked`, the
  matched-id route check in `join`, and seats.aero's documented but unmeasured
  comma-list form.
- [pp_matched_id_recipe.md](pp_matched_id_recipe.md) — **Supersedes the
  "dead end" framing in `pp_on_gflight.md`.** The matched-id join *does*
  work; the previous "no" was because we sent synthetic flight_ids and
  the wrong hint shape. Recipe (real `data[0][17]` from fli + IATA-prefixed
  flight#, human-readable airline name, space-separated times), empirical
  proof, and wire-through implementation notes.
- [gf_routing_and_carriers.md](gf_routing_and_carriers.md) — How
  `--routing`/`--extension` reach Google Flights: the search-page `tfs=`
  transport that replaced the gated `GetShoppingResults` RPC (field layout:
  carrier/alliance include, hour windows, duration, layover minutes, passenger
  kinds with infant 3 = lap and 4 = seat; the zero-based stop ceiling, typed
  refusals), the price cap (12) and bags (13) behind `search --max-price` and
  `--bags`, with each row's bag statement `row[4][6]` and where Matrix stands on
  both, the `fl[15]`/`fl[18]`/`fl[22]`
  booking-carrier rule (marketing vs operating), the Tier-1/2/3 classification
  (`routing_predicates`) that the date grids still use, the search path's
  per-predicate gate (`_gf_postfilter.search_page_reasons`: page-encodable or
  post-filtered on the full board), the post-filter (`_gf_postfilter`) and the
  raw-row checks that re-verify what the page encodes, the concurrent GF-fast-paint + Matrix-enrich flow
  (`_run_enriched_path`), and codeshare-aware display. Read before touching
  `routing_predicates.py`, `_gf_postfilter.py`, `links.build_search_tfs`, or
  `_gflight_ids` carrier parsing.
- [console_sanitizing.md](console_sanitizing.md) — **Read before adding any
  print to `cli.py`.** Which values are markup on a Rich console (user flags,
  Matrix fields, third-party exceptions), the `_quote` / `_safe_text` /
  sanitize-inside-the-formatter rule and the orderings that make each work, and
  the `escape_scan` AST guard: what it reads (every `console.print` / `err.print`
  and `.log` / `.rule` / `.status`, bare `print`, table titles and captions, column
  headers and footers, every cell, and Typer `help=` / `epilog=` strings), what
  its allowlist claims and what holds the values behind it, and what it does not
  model.
- [gf_browser_rung.md](gf_browser_rung.md) — The second search transport:
  `--gf-transport browser` drives a real Chrome to the URL rung 1 GETs, because
  Google's rate budget is keyed on client context, not IP. Measured parity
  between the two rungs, why `response.text()` / `wait_until="domcontentloaded"`
  / no warm-up / `channel="chrome"`, the thread-local session and what survives
  a Ctrl-C, the profile lock and its recovery, and the conftest guard that keeps
  tests from launching a browser. Read before touching `_gf_browser.py` or
  `_gflight_ids._one_call_laddered`.
- [legroom_recipe.md](legroom_recipe.md) — Per-leg legroom + amenities +
  aircraft come back in-band in Google Flights' own rows — now read from the
  search page's `ds:1` blob, with the indices unchanged (no travelarrow.io API
  call needed for the data itself). Index map for
  `data[0][2][i]` 12-17, enum decodings, amenity bit positions, and the
  seatmap URL contract (`/api/s` with M/D/YYYY dates). Read before
  touching `_gflight_ids._parse_leg_amenities` or `seatmap.py`.
- [spa_capture_workflow.md](spa_capture_workflow.md) — Headed-browser
  capture chassis (`research/record_user_session.py` with `--fill` /
  `--fill-mc` / `--snapshot` / `--pin` modes), the two URL-state
  schemas (Matrix `search=` JSON vs Google Flights `tfs=` protobuf),
  Matrix `/itinerary` pinning ID flow (`session`/`solutionSet`/`id` →
  `sessionId`/`rh`/`Si`), session-scoped lifetime caveat, and the
  battle-tested SPA-driving gotchas. Read before doing any new RE or
  recapture work.
- [doctor.md](doctor.md) — `flight doctor`: the ten checks, the cause
  each failure is filed under and which are retryable, exit codes 0/75/1, and
  the canary contract (a persistent matrix-search `brownout` is a shape
  suspect). Read before adding a check or a cause.

## When to add a new memory file

Add one when you learn something **non-obvious from the code alone**
that you'd want a future agent (or you in 6 months) to know. Examples
worth a new file:

- A field whose name doesn't match the SPA URL state field name
- A wire-shape detail that changes between captured-vs-our-output
- A subtle Matrix server behaviour discovered via probing
- A field that's documented behaviour but only used in one mode

Code comments are deep dives; memory files are the map. Each new file
should appear here as a one-line summary linking to the file.
