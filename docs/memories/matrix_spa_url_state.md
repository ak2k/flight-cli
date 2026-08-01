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

Round trip folds into ONE slice, which is why the inbound leg needs the
separate `*Ret` keys rather than a second slice.

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
