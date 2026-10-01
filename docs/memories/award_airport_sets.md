# Award search over an airport set: pair by pair, capped (2026-10-01)

`flight search JFK,EWR LHR` and `flight search NYC LON` ask the award
providers about every airport of the set, one airport pair per query, because
neither client sends more than one airport per end (`originAirport` in
`pp/client.py`, `origin_airport` in `providers/seats_aero/client.py`). Code:
`cli._build_pp_legs` (the pairs), `pp/cli._plan_pair_queries` (the cap and its
order) and the route check in `pp/match.join`. Tests:
`tests/test_award_airport_pairs.py` and the matched-id cases in
`tests/pp/test_match.py`.

## The pairs

A leg's origins and destinations are expanded through `_metro.expand_airports`
(a metro code becomes its members, any other token stays as typed) and crossed
in typed order. A pair with one airport at both ends is skipped, unless it is
the only pair the leg has. Every query of a leg shares its `slice_index`, date
and label, and the label names the typed tokens (`one-way JFK,EWR→LHR
2026-11-04`), so a one-airport label is what it always was. `run_pp_for_search`
reads consecutive queries with one `slice_index` as one leg: it concatenates
their awards, joins and renders them as one leg, and writes one JSON entry per
leg. The matched table keeps one row per first flight, date and pair of
airports (`_dedupe_per_leg`), so two connections that share a first flight and
end at different airports each keep their row and award.

A query carries only the cash hints of rows on its own pair, at most 50
(`_HINTS_PER_QUERY`). The cap counts the pair's own rows: were it applied to
the whole slice first, a board with more than 50 rows on one airport would
send the next airport's pair with no hint, and PointsPath runs a query with
no hint with Google matching off.

## What one pair costs

- PointsPath: one `/api/airline-search` request per cabin and airline, through
  one semaphore of 5 per client. Cabins are 2 by default (`DEFAULT_CABINS`);
  the airlines are the account's, from its feature flags, and 13
  (`DEFAULT_AIRLINES`) when discovery falls back. About 26 requests a pair.
- seats.aero: one GET, which is one unit of the 1000-a-day quota.

Uncapped, `NYC LON` round trip is 18 pairs a leg, 36 in all: about 936
PointsPath requests and 36 seats.aero units for one search.

## The cap

`MAX_AWARD_PAIR_QUERIES` = 8 pair queries a search, or one a leg when a search
has more legs than that. Within a leg the pairs a cash row of the result flies
at that slice come first, in row order, then the rest in typed order. The cap
is dealt one query per leg per round, so every leg is asked at least its first
pair; the calls then go out grouped by leg, in leg order. `NYC LON` round trip
asks 4 pairs a leg.

A leg the cap cut gets one stderr line, in table and JSON runs alike:

    Awards for outbound NYC→LON 2026-11-04: asked 4 of 18 airport pairs (at most 8 a search); not asked: JFK→LTN, ...

and its JSON entry gains `"pairs_not_asked": [{"origin": "JFK", "destination":
"LTN"}, ...]` after `matches` or `awards`. A leg nothing was cut from keeps
exactly `leg`, `slice_index` and `matches` | `awards`. Only pairs from the
expanded set are named; a pair a cash row flies outside that set (a city code
`_metro` does not know, say) is neither asked nor named.

## The matched-id route check

`join` drops a candidate whose origin or destination differs from the cash
slice's before resolution, compared uppercased; a missing code on either side
is no evidence. The flight-number and route-time keys carry the route already,
so the check binds the matched-id key alone. Without it, an EWR→LHR award that
echoed a JFK row's Google id attached to the JFK row (4 of 4 variants measured
at the base, hermetically). Per-pair hints make that echo unlikely; the check
makes it impossible.

## seats.aero's comma form: documented, not measured

seats.aero's public reference for `/partnerapi/search`
(developers.seats.aero/reference/cached-search, read 2026-10-01) says
`origin_airport` and `destination_airport` take a comma list, "such as
"SFO,LAX"". That would make a set one GET a leg instead of one a pair. It is not
used: nothing has measured it, a set would share one first page that the
provider does not paginate past, and one mechanism serves both providers. One
live seats.aero call would settle it.
