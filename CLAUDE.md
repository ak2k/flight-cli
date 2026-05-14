# flight-cli — Claude project notes

Power-user CLI wrapping ITA Matrix's undocumented Alkali backend.
Architecture: pydantic discriminated union → match-based wire adapters →
thin typer CLI shells. Small (~2K LOC, single-package layout)
+ golden-file regression tests + pyright-strict type checking.

## Workflow

- **Install**: `uv venv && uv pip install -e .`
- **Test**: `pytest tests/` (golden-file regression net, <0.1s)
- **Smoke a live search**: `flight fare JFK LHR --dep 2026-08-15 --return 2026-08-22 -n 2`
- **API key**: resolved at runtime from Matrix's SPA bundle and cached at
  `~/.cache/flight-cli/.matrix-key` (30-day TTL). Never hardcode keys in
  source. Override via `FLIGHT_API_KEY` env var.

## Editing rules

- **Run pytest after any change to `wire.py`, `links.py`, `domain.py`.**
  The golden-file tests at `tests/fixtures/` catch field-name and ordering
  regressions in <100ms — exactly the class of bugs that hit us during the
  initial build.
- **New SPA captures go in `research/`** (gitignored). Use
  `research/record_user_session.py` to drive a real browser, capture a wire
  body, drop into `tests/fixtures/`, write a reconstruction test.
- **Never commit anything under `research/`.** It's the paper trail.
  `tests/fixtures/` is the tracked canonical set.
- **No hardcoded credentials** — `_api_key.py` does runtime resolution.

## Load-bearing quirks (read before touching wire.py)

Full detail at [docs/memories/MEMORY.md](docs/memories/MEMORY.md). Highlights:

1. **`routeLanguage` ≠ `commandLine`.** Matrix slice has TWO distinct fields:
   `routeLanguage` for routing language (`"LH+"`, `"BA AA"`, `"[F* X F*]"`)
   and `commandLine` for extension codes (`"MAXCONNECT 2:00"`, `"MAXSTOPS 1"`).
   Confusing them returns `QPX Warning. Illegal COMMAND-LINE prefix`.
2. **Per-mode field rules.** Specific-date emits `dateModifier` +
   `isArrivalDate` always; calendar + followup omit them. Followup omits
   `inputs.filter`. Calendar has `page: {size}`; specific + followup have
   `page: {current, size}`.
3. **`maxLegsRelativeToMin` defaults to 1**, not 10. Matches the SPA's
   "No limit" UI default. User override via `--stops N`.
4. **`timeRanges` is more flexible than the 6 named buckets.** Matrix
   accepts arbitrary `{min: "HH:MM", max: "HH:MM"}` ranges and multi-range
   arrays. The 6-bucket UI is one interface; the underlying API takes any
   well-formed range with `min < max`. Zero-padded and single-digit hours
   both work.
5. **Summarizer order is asserted by golden-file tests.** The captured
   SPA order is `["carrierStopMatrix", "currencyNotice", "solutionList", ...]`.
   Don't reorder unless you also rebase the fixtures.
6. **Multiple AIza keys in the SPA bundle.** Only the entry tagged
   `matrix` (e.g. `.matrix="AIza..."` or `"matrix":"AIza..."`) is the prod
   search key — siblings `matrix-nightly` / `matrix-uat` / `matrix-dev`
   share the prefix but route to different backends. Bootstrap regex
   anchors on the bare `matrix` label so it excludes the variants; a
   first-match-any-AIzaSy approach would pick the People API key and 403.
7. **Calendar brownouts are real, not our bug.** Matrix's own UI returns
   empty grids for complex queries sometimes. Surface error message
   clearly; retry; consider simpler query.
8. **Two-phase calendar flow.** `name: "calendar"` returns the date grid;
   user picks a date in the UI; `name: "calendarFollowup"` returns full
   itineraries for that date. Both use the same `/v1/search` endpoint.

## Adding a search mode

1. New pydantic class in `domain.py` (extend `_SearchBase`, add `kind`
   Literal, register in `Search` union).
2. Add a `case` in `wire.to_wire()`, `client._parse_response`,
   `links.matrix_deep_link`, `links.google_flights_url`, `fli_bridge.to_fli_filter`.
3. `assert_never` ensures pyright catches forgotten branches.
4. Capture a real SPA body to use as a golden-file fixture.
5. Write a reconstruction test in `tests/test_wire_round_trip.py`.

## Project memory

See [docs/memories/MEMORY.md](docs/memories/MEMORY.md) for the topic index.
