# Extension codes (`commandLine` field, `--extension` flag)

Matrix's per-slice extension codes string. Lives in `slices[].commandLine`
(NOT `routeLanguage` — see [wire_format_quirks.md](wire_format_quirks.md)).
The CLI exposes it via `--extension` (alias `--ext`) on `flight fare` and
`flight calendar`; the domain field is `Leg.extension`.

This is the grammar for **itinerary-level constraints** — max stops, min/max
connection times, duration caps, mileage caps, alliance/carrier filters,
overnight/redeye/prop exclusions, cabin requirements, fare-basis filters,
aircraft-type filters.

For "what carriers and segments the routing must contain" → use the
**routing language** instead (see [routing_language.md](routing_language.md)).

## Canonical reference

Google's official ITA Matrix help has the extension codes spread across
three tabs of **https://support.google.com/faqs/answer/2736497**:

- **Itineraries tab**: the bulk of the codes (MAXSTOPS, MAXDUR, ALLIANCE,
  AIRLINES, -CITIES, -REDEYES, -OVERNIGHTS, -PROPS, AIRCRAFT, etc.).
- **Faring tab**: cabin codes (+CABIN/-CABIN) and the `F …` fare-basis
  syntax.
- **Aircraft Types tab**: the full IATA aircraft-code list (hundreds of
  entries) with CODE + PARENT columns, used with the AIRCRAFT extension.

Codes below marked **[Google]** are in those official tabs. Codes marked
**[community]** are from uponarriving or wider use and appear to work
empirically but aren't in the first-party docs (`PADCONNECT` is the only
one).

## Wire format

Multiple extension codes are separated by **semicolon** (`;`). Args within
one code are separated by **space**. The wire string is the whole semicolon-
joined list — no outer brackets, no leading `[/`:

```
commandLine: "MAXCONNECT 2:00"
commandLine: "ALLIANCE star-alliance; -REDEYES; MAXSTOPS 1; +CABIN 2"
commandLine: "MAXDUR 18:00; -OVERNIGHTS; -AIRCRAFT T:738"
```

Codes are **case-insensitive** for the keyword (`ALLIANCE` ≡ `alliance`).

## Units

- **Times**: `HH:MM` (e.g. `MAXCONNECT 2:00`, `MAXDUR 18:00`, `PADCONNECT 0:30`)
- **Distances**: plain integers in miles (e.g. `MAXMILES 2900`)
- **Counts**: plain integers (e.g. `MAXSTOPS 1`)
- **Carrier/airport/aircraft codes**: IATA-style, space-separated for lists

## Itinerary constraint codes

| Code | Example | Meaning | Source |
|---|---|---|---|
| `MAXSTOPS n` | `MAXSTOPS 1` | Max connecting stops per direction | [Google] |
| `MAXDUR hh:mm` | `MAXDUR 18:00` | Max itinerary duration per direction | [Google] |
| `MAXMILES n` | `MAXMILES 8000` | Max miles flown per direction | [Google] |
| `MINMILES n` | `MINMILES 2600` | Min miles flown per direction | [Google] |
| `MINCONNECT hh:mm` | `MINCONNECT 1:00` | Min connection time at any layover | [Google] |
| `MAXCONNECT hh:mm` | `MAXCONNECT 2:00` | Max connection time at any layover | [Google] |
| `PADCONNECT hh:mm` | `PADCONNECT 0:30` | Extra padding over airline's min connection time | [community] |
| `-OVERNIGHTS` | `-OVERNIGHTS` | Exclude itineraries requiring overnight stops at hubs | [Google] |
| `-REDEYES` | `-REDEYES` | Exclude overnight (red-eye) flights | [Google] |
| `-PROPS` | `-PROPS` | Exclude propeller aircraft (shortcut; see `AIRCRAFT` for finer control) | [Google] |
| `-CODESHARE` | `-CODESHARE` | Disallow codeshare flights | [Google] |
| `-NOFIRSTCLASS` | `-NOFIRSTCLASS` | Require flights that have a first-class cabin (book in any cabin) | [Google] |

## Carrier filters [Google]

| Code | Example | Meaning |
|---|---|---|
| `ALLIANCE code\|code\|…` | `ALLIANCE star-alliance` | Restrict to alliance(s). Multiple via `\|`. Values: `oneworld`, `skyteam`, `star-alliance` |
| `AIRLINES code …` | `AIRLINES BA AF` | Allow only these marketing carriers |
| `-AIRLINES code …` | `-AIRLINES AA BA` | Prohibit these marketing carriers |
| `OPAIRLINES code …` | `OPAIRLINES AA` | Allow only flights operated by these carriers |
| `-OPAIRLINES code …` | `-OPAIRLINES AA` | Prohibit flights operated by these carriers |

## Airport filters [Google]

| Code | Example | Meaning |
|---|---|---|
| `-CITIES code …` | `-CITIES DFW ORD` | Prohibit these connection cities |

## Aircraft / equipment filters [Google]

| Code | Example | Meaning |
|---|---|---|
| `AIRCRAFT t1 t2 …` | `AIRCRAFT T:737 C:JET` | Allow only listed equipment types (`T:` prefix) or categories (`C:` prefix). Combine multiple with spaces. |
| `-AIRCRAFT t1 t2 …` | `-AIRCRAFT C:TURBOPROP T:738` | Negation form: prohibit listed types/categories. |

**Equipment types** use `T:<CODE>` where `CODE` is from the IATA aircraft-
type table on Google's "Aircraft Types" tab. The table has two columns:

- **CODE**: a specific aircraft type (e.g. `T:738` = 737-800, `T:31N` = A319neo).
- **PARENT**: the broader family that code belongs to. Using the parent
  matches **every variant** in the family.

Concretely:

- `T:738` → only 737-800.
- `T:737` → any 737 (the parent of 731/732/733/734/735/736/737/738/739
  and MAX 7/8/9/10 variants — confirm by checking the table).
- `T:32S` → entire A318/A319/A320/A321 family **including** their neo
  and sharklets variants (since `32S` is the parent of `318`, `31A`,
  `319`, `31B`, `31N`, `320`, `32A`, `32N`, `321`, `32B`, `32Q`).
- `T:32N` → specifically A320neo (no other variants).
- `T:32A` → specifically A320 with sharklets (no other variants).

**Categories** (`C:` prefix):

- `C:JET`
- `C:TURBOPROP`
- `C:PISTON`
- `C:TRAIN` (rail-substitute)
- `C:HELICOPTER`
- `C:AMPHIBIAN`
- `C:SURFACE` (bus/road feeder)

**Common type codes** (curated; see Google's Aircraft Types tab for the
full ~500-entry list):

| Code | Type | Code | Type |
|---|---|---|---|
| `T:737` | 737 family (parent) | `T:32S` | A318/319/320/321 family (parent) |
| `T:738` | 737-800 | `T:320` | A320 (CEO, no winglets) |
| `T:73H` | 737-800 with winglets | `T:32A` | A320 (sharklets) |
| `T:7M8` | 737 MAX 8 | `T:32N` | A320neo |
| `T:744` | 747-400 | `T:321` | A321 |
| `T:74H` | 747-8 | `T:32Q` | A321neo |
| `T:752` | 757-200 | `T:333` | A330-300 |
| `T:762` | 767-200 | `T:339` | A330-900neo |
| `T:763` | 767-300 | `T:359` | A350-900 |
| `T:772` | 777-200/200ER | `T:351` | A350-1000 |
| `T:77W` | 777-300ER | `T:388` | A380 |
| `T:77L` | 777-200LR | `T:223` | A220-300 |
| `T:788` | 787-8 | `T:CRJ` | CRJ family |
| `T:789` | 787-9 | `T:DH8` | Dash 8 family |
| `T:781` | 787-10 | `T:EMJ` | E-Jet family (170-195) |

For exact codes (especially regional jets, freighters, and exotic
equipment), pull from Google's table. `-AIRCRAFT C:PROP` is equivalent to
`-AIRCRAFT C:TURBOPROP C:PISTON` (= `-PROPS`).

## Cabin codes [Google]

(From the Faring tab.)

| Code | Example | Meaning |
|---|---|---|
| `+CABIN n …` | `+CABIN 1` | Require booking in these cabin(s) |
| `-CABIN n …` | `-CABIN 3` | Prohibit booking in these cabin(s) |

Cabin code values:

- `1` — first class
- `2` — business class (second class)
- `premium-coach` or `pe` — premium economy
- `3` — economy

Multiple cabin codes space-separated: `+CABIN 1 2` requires first OR business.

## Fare-basis codes [Google]

(From the Faring tab.) Syntax: `F carrier.city1+city2.farebasis`. The
pattern is `F` followed by carrier, market (city pair joined by `+`), and
fare basis, each segment optional and dot-separated. Multiple alternates
joined by `|` (vertical bar).

| Pattern | Example | Meaning |
|---|---|---|
| `F BC=code` | `F bc=y` | Prime booking code (any carrier, any market) |
| `F BC=code\|BC=code` | `F bc=y\|bc=b` | Alternative prime booking codes |
| `F CC.AAA+BBB.FFFFFF` | `F aa.lon+chi.yup` | Carrier + market + fare basis |
| `F ..FFFFFF` | `F ..yup\|..f` | Fare basis only |
| `F .AAA+BBB.` | `F .lon+chi.` | Market only |
| `F CC..FFFFFF` | `F aa..yup\|aa..f` | Carrier + fare basis, any market |
| `F ..F-` | `F ..y-\|..b-` | Wildcards — fare bases starting with the given letter |

**Caveat**: actual class used may differ from what you asked for, "due to
being overridden by the carrier's booking code exception table" (per
Google). Watch the fare rules in returned itineraries to confirm.

## Combining

Semicolons join. Whitespace around the semicolon is allowed but not required:

```
ALLIANCE star-alliance; -REDEYES; MAXSTOPS 1; +CABIN 2; F bc=w; AIRCRAFT T:359
```

Order doesn't matter to the server.

## Per-direction application

Like `routeLanguage`, `commandLine` is **per-slice**. Outbound and return get
independent extension strings. The CLI exposes both via `--extension` /
`--extension-ret` (or `--ext` / `--ext-ret`). Multi-city sets one per slice.

## Pitfalls

1. **Don't put routing-language expressions here.** `LH+`, `BA AA`,
   `F* X:LHR F*` belong in `routeLanguage`. The error from Matrix is loud:
   `QPX Warning. Illegal COMMAND-LINE prefix: …`.
2. **`MAXSTOPS` is per direction.** Round-trip with `MAXSTOPS 1` on both
   slices = max 1 stop outbound AND max 1 stop return. There's no
   "total stops across the trip" code.
3. **`-OVERNIGHTS` excludes itineraries with overnight stops at hubs.**
   `-REDEYES` excludes overnight flights (red-eyes). Different concepts —
   use both if you want neither.
4. **`AIRCRAFT T:` parent vs child codes.** Picking the parent matches the
   whole family; picking a child code matches only that specific variant.
   When in doubt, use the parent; you can always narrow later.
5. **Case of the keyword doesn't matter, but case of code-args sometimes
   does.** `ALLIANCE star-alliance` and `alliance STAR-ALLIANCE` both work
   for the keyword, but alliance code values are lowercase only
   (`star-alliance`, `oneworld`, `skyteam`). IATA codes are conventionally
   uppercase.
