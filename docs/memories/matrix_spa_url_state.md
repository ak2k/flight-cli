# Matrix SPA URL state vs the /batch API

Captured 2026-08-01 by driving the real Matrix UI (patchright + real Chrome,
Advanced controls, one-way JFK→LHR with `Routing=BA+`, `Extension=MAXSTOPS 0`).

## The trap

The SPA's URL state and the `/batch` API use **different names for the same
values**. Guessing from the API side gets you a link the app ignores.

| Concept | `/batch` API (`wire.py`) | SPA URL state (`links.py`) |
|---|---|---|
| Routing language | `routeLanguage` | `routing` |
| Extension codes | `commandLine` | `ext` |
| Return-leg routing | (own slice) | `routingRet` |
| Return-leg extension | (own slice) | `extRet` |
| Arrival-date intent | `isArrivalDate: bool` | `departureDateType: "depart"｜"arrive"` |
| Flexible date | `dateModifier: {minus, plus}` | `departureDateModifier: "<minus*10 + plus>"` |

Round trip folds into ONE slice, which is why the inbound leg needs the
separate `*Ret` keys rather than a second slice.

## Date options: one string per direction, two numbers on the wire

The form's date-option select, beside "Departure | Arrival", offers exactly
five values for the outbound and five for the return, and no 3-day choice:

| URL state | Form label | `dateModifier` | `flight search` |
|---|---|---|---|
| `"0"` | This day only | `{minus: 0, plus: 0}` | (none) |
| `"10"` | Or day before | `{minus: 1, plus: 0}` | `--flex before` |
| `"1"` | Or day after | `{minus: 0, plus: 1}` | `--flex after` |
| `"11"` | +/- 1 day | `{minus: 1, plus: 1}` | `--flex 1` |
| `"22"` | +/- 2 days | `{minus: 2, plus: 2}` | `--flex 2` |

The SPA bundle (May 2026) reads `departureDateModifier` m as
`{minus: Math.floor(m/10), plus: m%10}`, and a round trip's return slice reads
`returnDateModifier` the same way; `isArrivalDate` is `departureDateType ===
"arrive"`. `links._spa_date_modifier` writes `minus*10 + plus`, and a one-way
writes `returnDateModifier: "0"`, as the form does. Calendar slices carry
neither field.

Checked in real Chrome on 2026-10-01 (fixtures `spa_rt_flex_jfk_lhr.txt` and
`spa_ow_arrive_jfk_lhr.txt` in `tests/fixtures/matrix_url/`, bodies
`specific_jfk_lhr_{rt_flex,ow_arrive}.json`): `/search` opened on our link showed
"+/- 2 days" / "Or day after" and "Arrival"; after Search the SPA posted
`to_wire()`'s body key for key except `bgProgramResponse`, and its own URL state
was our link's. The one difference: the SPA omits a one-way's `returnDate`,
which our one-way links write as `""`; both open the same form.

An arrival-date slice's `departureDatePreferredTimes` are arrival times: the
SPA builds `timeRanges` from them whatever `departureDateType` is, and Matrix
holds an arrival-date slice's `timeRanges` to the arrival (see
`wire_format_quirks.md`).

## Presence is conditional

With no routing codes set the SPA **omits all four keys**; with any set it
emits all four (blank string for the unused ones). `_spa_routing_fields`
mirrors that, so our links stay byte-identical to the app's own in both cases
— the tracked fixtures in `tests/fixtures/matrix_url/` cover both shapes.

## Capture recipe

`research/capture_matrix_spa.py` does this **unattended** — re-run it whenever
the SPA changes:

    uv run --with patchright python research/capture_matrix_spa.py

Headless is blocked by `waa-pa` bot attestation, so it drives real Chrome via
patchright, but needs no human. It records both surfaces at once: `page.url`
(decode the `search=` base64) and `page.on("request")` filtered to
`alkali`/`batch` (the API body).

Four form-driving traps, each of which silently leaves Search **disabled**:

1. Airports are an autocomplete — type, then **click the `mat-option`**.
   `fill()` leaves the underlying model empty.
2. The date input has **no placeholder**; select it by
   `input.mat-datepicker-input`.
3. The date must be typed with **`press_sequentially`**. `fill()` sets the
   visible value but does not fire the events Angular's form model listens
   for, so Search stays disabled with a date plainly showing — the most
   misleading of the four.
4. `mat-input-*` ids are regenerated per render. Never select on them;
   `input[placeholder="Routing"]` / `"Extension"` are stable.

Order matters too: pick airports **before** switching to One way, or the date
control isn't rendered yet.
