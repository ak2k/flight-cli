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

Headless is blocked by `waa-pa` bot attestation; use real Chrome via
patchright (`AGENTS.md` has the full pattern). Watch two things at once:

- `page.url` → decode the `search=` base64 for URL state
- `page.on("request")` filtered to `alkali`/`batch` → the API body

Selector notes: `mat-input-*` ids are regenerated per render and useless;
`input[placeholder="Routing"]` / `"Extension"` are stable. Driving the whole
form programmatically is unreliable — the Search button stays disabled unless
the airport/date fields commit the way the Angular form expects. Filling the
routing boxes and having a human complete the search worked.
