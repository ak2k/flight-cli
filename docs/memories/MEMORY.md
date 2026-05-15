# flight-cli — Project Memory Index

Topical deep-dives. CLAUDE.md is the always-loaded overview; these files
go into detail and are loaded on demand.

- [wire_format_quirks.md](wire_format_quirks.md) — Per-mode field rules,
  `routeLanguage` vs `commandLine`, summarizer ordering, page semantics,
  timeRanges flexibility. Read before touching `wire.py` or `links.py`.
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
- [pp_matched_id_recipe.md](pp_matched_id_recipe.md) — **Supersedes the
  "dead end" framing in `pp_on_gflight.md`.** The matched-id join *does*
  work; the previous "no" was because we sent synthetic flight_ids and
  the wrong hint shape. Recipe (real `data[0][17]` from fli + IATA-prefixed
  flight#, human-readable airline name, space-separated times), empirical
  proof, and wire-through implementation notes.

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
