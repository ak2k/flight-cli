# Routing language (`routeLanguage` field, `--routing` flag)

Matrix's per-slice routing-language string. Lives in `slices[].routeLanguage`
(NOT `commandLine` — see [wire_format_quirks.md](wire_format_quirks.md) for
that distinction). The CLI exposes it via `--routing` on `flight fare` and
`flight calendar`, and the domain field is `Leg.route_language`.

**This is the grammar for what carriers/airports/segments the itinerary must
include.** For constraints like "max duration", "no overnights", "min
connection time" → use [extension_codes.md](extension_codes.md) instead.

## Canonical reference

Google's official help page documents the full grammar:
**https://support.google.com/faqs/answer/2736497**

Pin that link — it's the only first-party reference for this DSL and it covers
everything below in more depth.

## Wire format

Brackets `[ ]` are illustrative in the Google docs — the actual API takes the
string **without** outer brackets:

```
routeLanguage: "LH+"            # not "[LH+]"
routeLanguage: "BA AA"          # not "[BA AA]"
routeLanguage: "F* X F*"        # not "[F* X F*]"
```

Multiple segments inside one routeLanguage are separated by **space**.
Alternatives within one segment are separated by **comma, no spaces**.

## Operators

| Op | Meaning |
|---|---|
| `~` | Negation (`~UA` = not on UA, `~DFW` = not via DFW) |
| `+` | One or more matching flights/airports |
| `*` | Zero or more matching flights/airports |
| `?` | Zero or one matching flight/airport |
| ` ` (space) | Separate flight segments |
| `,` (comma, no spaces) | Alternatives within one segment |

## Prefixes

Codes get a prefix that defaults sensibly:

| Prefix | Default for | Meaning |
|---|---|---|
| `C:` | 2-letter codes | Marketing carrier (the one whose flight number it is) |
| `O:` | — | Operating carrier (the one whose plane it is) |
| `X:` | 3-letter codes | Connection airport |
| `F` | — | Flight-segment placeholder (`F` = one segment, `F+` = one or more) |
| `N` | — | Non-stop flight (single takeoff/landing) |
| `L:` | — | Country (ISO 2-letter), used with `~` to exclude e.g. `~l:nUS+` (no US connections) |

Naming:
- Airlines: 2-letter IATA (AA, BA, LH, UA, DL)
- Airports: 3-letter IATA (JFK, LHR, DFW, ORD)
- Countries: 2-letter ISO (US, GB, DE, JP)
- Alliances: lowercase strings (`oneworld`, `skyteam`, `star-alliance`)

## Global itinerary modifiers (`/`-prefix) — documented, but broken via the API

Google's help page documents slash-modifiers that go inside the routing
language to express itinerary-level constraints:

```
[/ minconnect 45]            # 45 minutes minimum connection time
[/ minconnect 60; maxconnect 120]
[/ padconnect 20]            # +20 min on top of airline minimum
[/ maxdur 240]               # 240 minutes (= 4 hours) max total duration
[/ alliance oneworld]
[/ alliance star-alliance]
[/ -overnight]
[/ -overnight;-redeye]
[/ -prop]
```

**These DO NOT WORK via the API endpoint this CLI hits** (verified
empirically 2026-05). Every variant tested — with/without surrounding
brackets, with/without spaces, with various route prefixes (`F+`, bare,
etc.) — returns:

```
QPX Warning.  Bad route specification
```

**Original hypothesis (refuted)**: that the matrix.itasoftware.com SPA
preprocesses slash-modifiers client-side and we just don't reproduce that
preprocessing. Inspecting the SPA bundle (2026-05) refuted this. The
serializer is literally:

```js
{ commandLine: c.ext || void 0, routeLanguage: c.routing || void 0, ... }
```

where `c.ext` is the user-typed content of the SPA's "Extension codes"
input and `c.routing` is the user-typed content of the "Routing
language" input. The strings are passed through verbatim — no
preprocessing, no shortcut expansion, no slash-modifier splitting. (Bundle
also has zero matches for any plausible-named preprocessing function:
`parseRouting`, `expandRouting`, `splitSlashModifiers`, etc.)

**Conclusion**: slash-modifier syntax doesn't work in the SPA either. The
Google routing-language help page documents idioms that aren't (or are no
longer) accepted by Matrix's actual server. Treat the slash-modifier
section of that page as misleading.

**Resolution**: use the [extension_codes.md](extension_codes.md)
equivalent instead. They cover the same constraints reliably:

| Routing-language slash form (broken) | Extension-code equivalent (works) |
|---|---|
| `/ minconnect 45` (mins) | `MINCONNECT 0:45` (HH:MM) |
| `/ maxconnect 180` (mins) | `MAXCONNECT 3:00` (HH:MM) |
| `/ padconnect 20` (mins) | `PADCONNECT 0:20` (HH:MM) |
| `/ maxdur 480` (mins) | `MAXDUR 8:00` (HH:MM) |
| `/ alliance star-alliance` | `ALLIANCE star-alliance` |
| `/ -overnight; -redeye` | `-OVERNIGHTS; -REDEYES` |
| `/ -prop` | `-PROPS` |

**Original verification plan**: capture a real SPA session to see what
wire body the SPA emits when slash-modifiers are typed. **Skipped** in
favor of inspecting the SPA bundle directly, which gave a definitive
answer faster (the serializer is exposed in the minified JS as a simple
pass-through of `c.ext` and `c.routing`). If someone wants to confirm via
a live capture anyway, `research/record_user_session.py` is the tool —
type `[/ alliance star-alliance]` in the routing box and observe whether
Matrix's UI even renders results (it shouldn't, based on the bundle).

## Other API-rejected idioms

Same caveat — documented in Google's help but rejected by the API:

- **Alliance carrier-shortcuts** like `STAR+`, `oneworld+`, `skyteam+`
  return `Bad route specification`. Use `--extension 'ALLIANCE …'`
  instead.
- Anything starting with a bare `/` (e.g. `/alliance ...`) without a
  preceding route segment is rejected.

## What does work in routing — confirmed empirically

- Bare carrier codes: `LH+`, `BA AA`, `~UA+`, `O:LH+` ✅
- Connection-airport filters: `F* X:LHR F*`, `~DFW`, `DFW,DEN` ✅
- Country filters: `~l:nUS+` ✅
- Flight-number filters: `UA882`, `UA1000-2000+` ✅
- Case-insensitive: `lh+` ≡ `LH+`, `x:icn` ≡ `X:ICN` ✅
- Non-stop forms: `N`, `N:UA` ✅
- Segment placeholders: `F`, `F+`, `F?`, `F*` ✅

## Worked examples

### Airline filters
| Expression | Meaning |
|---|---|
| `AA` or `C:AA` | Direct flight marketed by AA |
| `AA+` | Any number of flights, all marketed by AA |
| `AA,CO,DL` | Direct on AA, CO, or DL |
| `O:AA` | Operated by AA (not codeshare) |
| `O:AA,O:UA,O:DL` | Operated by one of these |
| `~UA` | Direct, not UA |
| `~UA+` | Itinerary that has zero UA flights |
| `~AA,UA,DL` | Direct, excluding any of these as marketing carriers |

### Segment shapes
| Expression | Meaning |
|---|---|
| `N` | Non-stop only |
| `N:UA` | Non-stop on UA |
| `F` | Direct (one flight number, may have one stop) |
| `X+` | One or more connections |
| `X X` | Exactly 2 connections |
| `X? X?` | Two or fewer connections |
| `F+ AA F+` | Any number of flights, at least one on AA |
| `F? US F?` | Up to 3 flights, at least one on US |

### Connection airport filters
| Expression | Meaning |
|---|---|
| `DFW` or `X:DFW` | Connect at DFW only |
| `F? DFW F?` | DFW connection plus other segments allowed |
| `DFW,DEN` | Connect at DFW or DEN, no other connections |
| `N DFW,DEN N` | Non-stop to DFW or DEN, non-stop onward |
| `DFW DEN X?` | DFW → DEN → 0+ stops (order matters; use `DFW,DEN DFW,DEN` for either order) |
| `~DFW` | One connection, not at DFW |
| `~l:nUS+` | No US connections (doesn't affect direct flights) |

### Flight-number filters
| Expression | Meaning |
|---|---|
| `UA882` | Specific flight |
| `~UA882+` | Any flights except UA882 |
| `UA882 F+` | UA882 followed by any |
| `UA1000-2000+` | One or more UA flights with numbers 1000–2000 |

### Combined
| Expression | Meaning |
|---|---|
| `LH+` | Any number of LH (Lufthansa) flights |
| `BA AA` | BA flight then AA flight |
| `F* X:LHR F*` | Itinerary with LHR as a connection |
| `O:LH F* / alliance star-alliance` | LH-operated, any number of flights, Star Alliance overall |

## Per-direction application

Routing language is **per-slice (per direction)** — outbound and return each get
their own. For round-trip, both `--routing` (outbound) and `--routing-ret`
(return) are exposed in the CLI. Multi-city sets one per slice.

If you only want a constraint on the outbound, leave the return blank
(`""` / unset) — `routeLanguage` is optional per slice.

## Pitfalls

1. **Don't put extension codes here.** `MAXCONNECT 2:00`, `MAXSTOPS 1`,
   `ALLIANCE star-alliance` (without the `/` prefix), etc. belong in
   `commandLine` — see [extension_codes.md](extension_codes.md). Putting them
   in `routeLanguage` returns:
   ```
   {"error":{"message":"QPX Warning.  Illegal COMMAND-LINE prefix: ...", "type":"input"}}
   ```
2. **Order matters in segment chains.** `DEN ATL` ≠ `ATL DEN`. Use
   `DEN,ATL DEN,ATL` if either order is acceptable.
3. **Brackets in the Google docs are illustrative.** Don't include `[` `]`
   in the wire string.
4. **Country filters use lowercase `l:`** (lowercase L, colon). `[~l:nUS+]`
   means "exclude connections in US, allow 0+ non-US connections".
