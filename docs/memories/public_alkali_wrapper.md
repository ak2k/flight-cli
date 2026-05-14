# This is (apparently) the only public Alkali wrapper

A thorough web survey in 2026-05 turned up **zero indexed results** for any
of the distinctive identifiers in this project's wire format:

- `x-alkali-application-key`
- `x-alkali-auth-apps-namespace`
- `content-alkalimatrix-pa.googleapis.com`
- `maxLegsRelativeToMin`
- `summarizerSet`
- `calendarRoundTrip` / `calendarOneWay`
- `bgProgramResponse`
- `currencyNotice.ext.price`

No blog posts, no Hacker News threads, no GitHub repos, no Stack Overflow
questions, no leaked internal Google docs, no reverse-engineering writeups.
The Alkali endpoint is genuinely undocumented in public.

## What that implies

1. **Our `tests/fixtures/*.json` are the closest thing to a spec.** If a
   field's behavior is unclear, the captured SPA bodies are the authoritative
   reference for the *current* generation of the wire format.

2. **The CLAUDE.md "load-bearing quirks" list is the consolidated knowledge
   base.** Every quirk is something we learned by capturing real SPA
   sessions and probing the endpoint — no upstream doc would tell us about
   `summarizers` ordering or the per-mode field-presence rules.

3. **No prior art for response-shape evolution.** If Matrix renames
   `solutionList` → `solutionsList` tomorrow, no one else's wrapper will
   catch it for us. Our golden-file tests + pyright strict mode are the
   only early-warning system.

4. **Forward compatibility is our problem.** `extra="ignore"` on every
   `_Loose` response model lets Matrix add fields without breaking us, but
   if they *change* an existing field's semantics we won't notice until
   prices or routes look wrong.

## Adjacent prior art (validates parts of the structure, not the whole)

| Source | What it validates | Vintage |
|---|---|---|
| [mayanez/flight_scraper#10](https://github.com/mayanez/flight_scraper/issues/10) | Slice/leg structure, `isArrivalDate`, `dateModifier`, `timeRanges`, summarizer naming pattern. Same backbone, older summarizer names. | 2014–2015 |
| [QPX Express Go client](https://pkg.go.dev/google.golang.org/api/qpxexpress/v1) | Semantic ancestry of `TripOptionsRequest` → `SliceInput` → response tree (`SliceInfo` → `SegmentInfo` → `LegInfo` → `FlightInfo`). Different field names; same conceptual model. | Deprecated 2018 |
| [Google's routing-language help](https://support.google.com/faqs/answer/2736497) | The `routeLanguage` grammar (`LH+`, `[F* X F*]`, alliance codes, `[/ minconnect ...]`). | Live, first-party |
| [uponarriving ITA Matrix guide](https://www.uponarriving.com/ita-matrix-guide/) | The `commandLine` extension codes (`MAXSTOPS`, `MAXDUR`, `ALLIANCE`, `+CABIN`, etc.). | Live, community |

For grammar-level fields (`routeLanguage`, `commandLine`) the references
above are good and we cite them in [routing_language.md](routing_language.md)
and [extension_codes.md](extension_codes.md).

For the request-envelope and response-tree shape (everything in `wire.py`
and `models.py`), we are the documentation.

## What to do if Matrix breaks us

1. **Run the golden-file tests first.** If `tests/test_wire_round_trip.py`
   still passes, the request shape is fine — the break is in the response.
2. **Capture a fresh SPA session** via `research/record_user_session.py`.
   Diff against the existing fixture in `tests/fixtures/`.
3. **Update `wire.py` and/or `models.py`** to match the new shape. Bump
   the affected fixture(s).
4. **Document the change** in [wire_format_quirks.md](wire_format_quirks.md)
   or a new memory file if it's a new category of behavior.

## What to do if someone else publishes a wrapper

Cross-link from this memory. The more independent wrappers exist, the more
quickly drift gets caught.
