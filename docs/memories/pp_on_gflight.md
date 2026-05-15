# PointsPath overlay rides both backends (work-qmx1)

The PP augmenter is **backend-agnostic** at the matcher level. `pp/match.py`
joins by structural keys (flight#+date and route+time) that don't care
whether the cash side came from Matrix's Alkali response or fli's Google
Flights scrape. The adapter at `pp/gflight_adapter.py` wraps fli output
in a `SearchResult` shape so the existing matcher and renderer reuse
end-to-end — no duplication.

## Decision: enable_matching=False, not True

PointsPath's `airline-search` endpoint accepts a `CashFlightHint` payload
with `enableGoogleFlightMatching=true`, which is supposed to echo a
`matchedGoogleFlightId` per result so cash↔award join becomes trivial. We
considered using it but **didn't**, for three reasons:

1. **fli has no opaque Google Flights ID**. `fli.models.FlightResult`
   only carries structured fields (airline, flight_number, datetimes,
   route, price). The "real Google Flights ID" framing in the bd issue
   prompt doesn't map to a field fli actually exposes.
2. **The previous probe** (`research/probe_pp_matching.py`,
   gitignored capture at `research/capture/pp_matching_probe.json`)
   showed that synthetic `flight_id` values cause PP to return an empty
   `outboundFlights` list entirely — PP filters on the hint, doesn't
   just echo it. Without a verified real-ID format the matching path is
   a footgun.
3. **The existing (flight#, date) + (route+time) keys already work**
   on the Matrix backend, including for codeshare bridging (work-22az).
   The same keys carry over to fli output unchanged.

If a future spike confirms `matchedGoogleFlightId` populates for some
hint format that fli can produce, the upgrade path is small: add
`cash_hints=...` to `SearchSpec` in `pp/cli.py:_gather_pp` and a new
hit-merging branch in `match.py:join` that consumes the echoed match id.
Worth it only if codeshare-bridging on the gflight backend turns out to
be common — gflight is mostly used for plain ULCC/Frontier/Spirit kind
of queries, where matched-id robustness isn't load-bearing.

`research/probe_pp_real_hints.py` (gitignored) is the prepared probe
for the upgrade question. It runs a real fli search, builds CashFlightHints
from the structured output, and sends them to PP with enable_matching=True.
Attempting it during work-qmx1 hit a stale refresh token (Supabase rotates
single-use; a parallel Chrome session had consumed the chain), so the
empirical answer is deferred. Re-run after `flight auth pp login` to settle.

## Why: was forcing matrix on every PP query

Before this change, any presence of a `--pp-*` flag forced the matrix
backend. That was a 26s+ tax (vs gflight's ~0.7s) for queries that
didn't need Matrix's richer routing/extension semantics. Plain
`--pp-only` users — the canonical "is there an award for this trip?"
gut-check — paid that tax in full.

## How to apply

- New search modes should reuse the matcher unchanged; add to
  `gflight_adapter` only when adapting a new source's output.
- `_should_run_pp` is now backend-agnostic — only token presence and
  `--no-pp/--pp-only` flag coherence gate the overlay.
- `_pick_backend` no longer accepts PP-related parameters; PP-* flags
  don't influence backend choice.
