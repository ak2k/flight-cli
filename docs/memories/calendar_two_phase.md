# Calendar mode is two-phase

Matrix's "lowest fare each starting day" calendar uses a TWO-REQUEST flow,
not one. The CLI exposes them as `flight calendar` (phase 1) and
`flight detail` (phase 2).

## Phase 1: `name="calendar"`

User specifies: origin, destination, calendar window (`startDate` / `endDate`),
duration range (`layover.min` / `layover.max`), pax, cabin.

Returns: a `calendar` object with `months[].weeks[].days[]`, each priced
day having `minPrice`, `solutionCount`, and `tripDuration.options[]`
listing one priced option per duration in the requested range.

```jsonc
{
  "name": "calendar",
  "summarizerSet": "calendarRoundTrip",   // or "calendarOneWay"
  "summarizers": ["calendar", "overnightFlightsCalendar",
                  "itineraryStopCountList", "itineraryCarrierList",
                  "currencyNotice"],
  "inputs": {
    "startDate": "2026-08-15",
    "endDate":   "2026-09-15",
    "layover":   {"min": 5, "max": 7},
    "slices": [{ "origins":[...], "destinations":[...], ... }, ...]
    // legs have NO `date` field; calendar window owns dates
  }
}
```

## Phase 2: `name="calendarFollowup"`

User clicks a date in the calendar grid. The SPA fires a SECOND request
with the same window context PLUS per-slice `date` values for the picked
trip. Returns full itineraries (same shape as a specific-date search).

```jsonc
{
  "name": "calendarFollowup",
  "summarizerSet": "wholeTrip",
  "summarizers": [...specific-date summarizers...],
  "inputs": {
    "startDate": "2026-08-15",    // preserves window context
    "endDate":   "2026-09-15",
    "layover":   {"min": 5, "max": 7},
    "slices": [
      {"origins":[...], "date":"2026-08-22", ... },  // picked dates
      {"origins":[...], "date":"2026-08-29", ... },
    ]
  }
}
```

## What differs between calendar and followup

| Field | calendar | followup |
|---|---|---|
| `name` | `"calendar"` | `"calendarFollowup"` |
| `summarizerSet` | `"calendarRoundTrip"` / `"calendarOneWay"` | `"wholeTrip"` |
| `summarizers` | 5-element calendar set | 12-element specific-date set |
| `slices[].date` | omitted | required |
| `slices[].dateModifier` | omitted | omitted (!) |
| `inputs.filter` | `{}` (empty obj) | omitted entirely |
| `inputs.page` | `{size}` | `{current:1, size}` |
| `inputs.startDate` / `endDate` | present | present (preserves context) |
| `inputs.layover` | round-trip only | round-trip only |

`layover` is the trip LENGTH (nights between outbound and return), so it goes on
a body only when there is a return leg. Send it one-way and Matrix answers HTTP
200 `"Internal server error"` for both `calendar` and `calendarFollowup` —
verified live 2026-09-02, the identical bodies without it return a grid and 10
solutions respectively (work-h70kv.7).

That makes `--duration` moot on a one-way, and the CLI resolves the trip shape
BEFORE parsing it (`cli._resolve_duration`, used by `calendar` and `detail`
alike). Order matters: parsing first meant `--duration 9-3` on a one-way hit
`CalendarWindow`'s reversed-range validator and printed a pydantic traceback for
a flag that was about to be ignored — two contradictory answers to one flag. A
one-way now prints a single dim "ignored" line on **stderr** (every `--format`,
so `--format json` still sees it and stdout still carries only the document) and
takes the default range, which nothing downstream reads. Round-trip does read it,
so a reversed or unparseable range there is a typed usage error: exit 2 and one
line, never a traceback.

Whether a one-way value counts as "the default" is a textual comparison, over the
spellings `_normalize_duration` folds together: `..` for `-`, blanks around either
bound, and the zero-padded or signed writings of a number. A bound is only
canonicalized if it is one to nine digits, which is our bound, not `int()`'s —
`int()` refuses only at 4300+ digits (CPython's int/str conversion cap), and nine
is chosen as past any trip anyone will take and comfortably inside that. So
`0000000005-0000000007` is ten digits a side, stays as typed, and draws the
"ignored" line. True, and only cosmetic: a one-way ignores the value either way,
and the same spelling on a round trip is a typed exit 2 naming the width.

## Why bother with followup vs. just calling `name: "specificDatesSlice"`?

Both work; both return the same shape. The SPA uses `calendarFollowup`
to preserve session context — Matrix's backend reuses the calendar's
priced-options index to serve the detail page faster than a cold
specific-date search.

For our CLI, `flight detail` mirrors the SPA. If you ever find that
`flight fare` for the same dates is slower than `flight detail`, the
calendar-followup path is the optimization.

## UX implication

Cheap calendar discovery → pick a date → detail-fetch is a natural flow:

```
flight calendar MIA PAR --start 2026-06-07 -d 5-7 --routing "LH+" \
    --ext "MAXCONNECT 2:00"
# → grid: PAR split into CDG, ORY and BVA, one query per pair; each day's
#   route column names the pair that priced it, e.g. MIA→CDG

flight detail MIA CDG --dep 2026-06-10 --return 2026-06-16 \
    --routing "LH+" --ext "MAXCONNECT 2:00" --duration 5-7
# → the itineraries behind that cell
```

`detail` asks the grid's question only when it is given the calendar's
filters: the routing and extension codes (with `--routing-ret`/`--ext-ret`),
`--depart-times`/`--return-times`, `--include-unavailable`, `--stops`, the
cabin and the passengers. Left out, its itineraries answer a wider question
than the grid priced. The followup body carries a time window as each slice's
`timeRanges` and availability as `checkAvailability`, and Matrix applies the
window: live 2026-10-01, a `calendarFollowup` JFK-LAX 2026-10-20 one-way with
the morning window (`8:00`-`11:00`) gave 8 of 8 solutions departing 08:00 to
11:00, both ends inclusive.

A multi-airport or metro calendar is asked one airport pair per query, so
`detail` also needs the pair that priced the picked cell, not the calendar's
own codes: the table's `route` column (`MIA→CDG`) for the day's minimum, the
pair printed beside a trip length another pair priced, or `origin`/`destination`
on each day and trip length in `--json`. Given the calendar's codes, it asks
the combined query that AGENTS.md quirk #7 says under-reports. A cell that a
round trip's combined query priced names the calendar's codes, and a calendar
of one airport pair has no `route` column; for both, give `detail` the
calendar's codes.

The `--duration` flag on `detail` matters: followup needs to know the
original calendar's duration range to preserve session context. If the
user just says `--dep` and `--return`, we infer duration from the date
diff; but if they want different durations explored, pass `--duration`
explicitly.
