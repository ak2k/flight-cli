# Wire format quirks

Field-level rules for `wire.py → to_wire()` that aren't obvious from the
code. Each item is something that already burned us or would burn a future
edit.

## Two slice fields for "routing", not one

Matrix's slice has **two distinct fields** that the SPA URL state names
differently than the API:

| User UI field | SPA URL state | API request field | What goes in |
|---|---|---|---|
| Routing language box | `routing` | `routeLanguage` | `"LH+"`, `"BA AA"`, `"[F* X F*]"` |
| Extension codes box | `ext` | `commandLine` | `"MAXCONNECT 5:00"`, `"MAXSTOPS 1"` |

**Failure mode**: putting `"LH+"` in `commandLine` returns:
```
{"error":{"message":"QPX Warning.  Illegal COMMAND-LINE prefix: LH+", "type":"input"}}
```
The error is HTTP 200 with the failure in the body — `MatrixApiError`
catches it.

## Per-mode field rules

| Field | specific | calendar | followup |
|---|---|---|---|
| `slices[].date` | always | omitted | always |
| `slices[].dateModifier` | always (even `{0,0}`) | omitted | omitted |
| `slices[].isArrivalDate` | always (even `false`) | omitted | omitted |
| `inputs.filter` | `{}` | `{}` | OMITTED |
| `inputs.page` | `{current:1, size}` | `{size}` | `{current:1, size}` |
| `inputs.startDate / endDate / layover` | omitted | required | required |

Golden-file tests at `tests/fixtures/` will fail loudly if you over-emit
or under-emit these.

## Summarizer ORDER matters for tests (not for the server)

The server doesn't care about the order of the `summarizers` array, but
the captured SPA bodies use a specific order:

```python
["carrierStopMatrix", "currencyNotice", "solutionList", "itineraryPriceSlider",
 "itineraryCarrierList", "itineraryDepartureTimeRanges",
 "itineraryArrivalTimeRanges", "durationSliderItinerary", "itineraryOrigins",
 "itineraryDestinations", "itineraryStopCountList", "warningsItinerary"]
```

If you reorder, golden-file tests break. Either match the order or rebase
the fixtures.

## `maxLegsRelativeToMin` default = 1

The SPA's "No limit" stops setting maps to `maxLegsRelativeToMin: 1` on
the wire — i.e., up to 1 connection. Higher values DO work but exceed
what the consumer UI permits. Don't pass anything > 2 in production
without checking that Matrix's backend accepts it.

## `timeRanges` accepts arbitrary minute-granular ranges

The UI offers 6 fixed buckets, but the API takes any well-formed
`[{min: "HH:MM", max: "HH:MM"}, …]`:

- Both zero-padded (`"08:00"`) and single-digit (`"8:00"`) hours work
- Off-bucket ranges work (`{min:"9:30", max:"13:45"}` → 38 sols vs 64
  with no filter)
- Multi-range arrays work (`[{morning}, {evening}]`)
- `min < max` is required; `{min:"14:00",max:"10:00"}` errors with
  `QPX Warning. Unexpected format for DEP-TOFD-RANGES-LOCAL`
- Field name is **`timeRanges`** on the slice, NOT the SPA URL state's
  `departureDatePreferredTimes` (those are different things)

The `TimeOfDay` enum exposes the 6 named buckets. If we want
power-user arbitrary-range support, expose a parser that accepts
`"09:30-13:45"` in addition to the named values.

## `commandLine` order quirk in the SPA-state URL

The deep-link URL state for calendar mode folds round-trip into a SINGLE
slice with `routing/ext` for outbound and `routingRet/extRet` for return.
The API request, however, uses TWO slices (one per direction) and each
has its own `routeLanguage/commandLine`.

`links.matrix_deep_link()` collapses; `wire.to_wire()` expands. Don't
confuse the two shapes.

## SPA-side `slices[1].commandLine` is always None

Even when the SPA's URL state has a non-empty `routingRet`/`extRet`, the
API request only sets `routeLanguage`/`commandLine` on `slice[0]` (the
outbound). The return slice's routing is **inferred** by the server from
slice[0]. We mirror that: `wire._leg_to_wire()` sets fields per leg
faithfully, but when reconstructing from captured fixtures, expect
slice[1] to lack routing.

## Field-validation comes back as HTTP 200 (not 4xx)

All input validation errors arrive as `{"error": {"message": ..., "type": "input"}}`
inside a 200 response. The HTTP layer doesn't see them; `_raise_if_api_error`
in client.py catches and re-raises as `MatrixApiError` with the message
intact. **Don't skip this check** — quietly-empty responses are a real
failure mode otherwise.

## Calendar `solutionCount: 0` can mean three different things

1. **Real timeout**: query is too complex; server gives up. UI shows
   "Query Timeout" modal. We see an empty grid + 0 sols.
2. **Brownout**: temporary backend degradation. Same query works
   minutes later. Not specific to us — affects the real SPA too.
3. **No flights matched**: the search is valid but no fares exist for
   that combination.

These are indistinguishable from the response. Surface a helpful message
("Calendar empty. Matrix's calendar mode brownouts regularly; retry") and
include the deep-link URL so users can verify in the UI.
