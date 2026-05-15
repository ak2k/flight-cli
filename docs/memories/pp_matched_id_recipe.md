# PointsPath matched-id join — empirical recipe (2026-05-15)

Settled: PP's `enableGoogleFlightMatching=true` mode **works** and gives a
robust cash↔award join key (`matchedGoogleFlightId`), better than the
(flight#, date) + (route, time) fallbacks today. The earlier doc
(`pp_on_gflight.md`) ruled this out as a dead end; that conclusion was
based on a probe with a synthetic `flight_id`. With **real** Google
Flights opaque IDs, PP echoes them back populated.

## What unlocked it

Captured the live PP browser-extension traffic via Patchright + a snapshot
of the user's Chrome profile (with PP extension registered + Pro session
cookies). 549 events; 87 successful `/api/airline-search` responses, every
returned flight with `matchedGoogleFlightId` populated.

The capture also disproved the `store-flights` hypothesis from
`pp_on_gflight.md`: the extension does NOT pre-register flights via
`/api/store-flights` before querying. It goes straight to `/api/airline-search`
with `enableGoogleFlightMatching=true` and the hints carry their own opaque
IDs. PP looks the IDs up in its catalog at query time.

## The recipe

1. **Extract `data[0][17]`** from each flight in Google Flights' raw
   API response. fli's `_parse_flights_data` reads `data[0][2]` (legs),
   `data[0][9]` (duration), `data[0][1][−1]` (price) — but drops index `[17]`
   on the floor. We added `src/flight_cli/_gflight_ids.py` which reuses fli's
   encoder + curl_cffi client but parses our own response, capturing flightId.

2. **Hint payload shape** (per `CashFlightHint.to_payload()`):

   ```json
   {
     "origin": "MIA",
     "dest": "LHR",
     "startDateTime": "2026-06-30 18:05",     // YYYY-MM-DD HH:MM, no T, no TZ
     "endDateTime":   "2026-07-01 08:05",
     "flightId": "NbXSYb",                     // 5-6 char opaque from data[0][17]
     "airline": "Virgin Atlantic",             // HUMAN-READABLE name, not IATA
     "googleAirlines": ["Virgin Atlantic"],
     "numConnections": 0,
     "hasCarryOnBaggage": false,
     "firstFlightNumber": "VS6",               // IATA prefix + flight# concatenated
     "cashPrice": 802,
     "rawCashPriceString": "$802"
   }
   ```

   The two formatting gotchas that produce 0-flight responses if missed:
   - `firstFlightNumber` MUST be IATA-prefixed (`VS6` not `6`)
   - `airline` MUST be the human-readable name (`"Virgin Atlantic"` not `"VS"`)

3. **PP airline-search request** (`SearchSpec` with `enable_matching=True`):
   `airline` is the PP-side program being queried (`"American"`, `"Delta"`,
   etc.). PP returns flights matching ANY hint, regardless of whether the
   operating carrier matches the queried airline — useful for codeshare
   bridging (querying American can return a BA-operated flight if the hint
   refers to it).

4. **Response** has flights with `matchedGoogleFlightId` set to whichever
   hint `flightId` they matched. Join cash→award by string equality on that.

## Empirical evidence

`research/probe_pp_real_ids.py` produces:
```
PP query=American   → 2 flights, 2 matched
  *MATCHED*  AA38 matched='A1oq6c'      ← our supplied flightId
  *MATCHED*  BA208 matched='KTfE0c'     ← codeshare picked up via American
PP query=VirginAtlantic → 1 matched
  *MATCHED*  VS118 matched='w8PDCc'
```

Live capture saved at `research/capture/pp_extension_capture.json`
(gitignored). 87 successful airline-search responses for cross-reference.

## Why we'd want it

The (flight#, date) + (route, time) fallback we ship today works fine for
most flights but degrades on:
- Codeshares with marketing flight# != operating flight# (work-22az fix
  handles the common AA/BA case via route+time; not all edge cases)
- Flights with the same route+time on the same day but a different metal
  (rare but real — adjacent slot codeshares)

The matched-id path collapses all of that to string equality on opaque IDs
PP has already done the heavy lifting on.

## Implementation status

- ✅ `_gflight_ids.py` — fli wrapper that captures the opaque flightId
- ✅ Format recipe verified empirically (probe above)
- ⏳ Wire through to provider + matcher (work-?? follow-up):
  - Add `flight_id: str | None` field to `Slice` (or per-itinerary metadata)
  - Populate from fli via `gflight_adapter`
  - Provider takes `cash_hints` and sends with `enable_matching=True`
  - Provider populates `AwardFlight.matched_google_flight_id` from response
  - Matcher adds a third join index keyed on flight_id (highest priority)
  - The existing (flight#, date) + (route+time) keys stay as fallback

## Airline IATA → name mapping

PP's hint field `airline` requires the human-readable name. fli only exposes
the IATA code via its `Airline` enum. Two paths:
- Hardcode a mapping for the airlines PP services (`pp_pricing_info` already
  uses these names, e.g. `Delta`, `American`, `VirginAtlantic`)
- Extract `fl[22][3]` from Google's response (the airline name string)

The hardcoded mapping is simpler and PP's catalog is small + stable. Future
spike: extract `fl[22][3]` so the data flows from Google's truth rather than
a static lookup.
