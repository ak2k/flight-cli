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
| `inputs.startDate` / `endDate` / `layover` | present | present (preserves context) |

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
# → grid: cheapest June 1 @ USD1233 (6-night), USD1303 (5n) ...

flight detail MIA PAR --dep 2026-06-01 --return 2026-06-07 \
    --routing "LH+" --ext "MAXCONNECT 2:00" --duration 5-7
# → 2 LH solutions @ USD2175 for the picked date
```

The `--duration` flag on `detail` matters: followup needs to know the
original calendar's duration range to preserve session context. If the
user just says `--dep` and `--return`, we infer duration from the date
diff; but if they want different durations explored, pass `--duration`
explicitly.
