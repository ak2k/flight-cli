---
name: flight-search
description: Use this skill when a user wants to search for flights, find airfare deals, build a flight query, compare prices across dates, or invoke the `flight` CLI in this project. Translates user intent (origin, destination, dates, constraints like alliance / cabin / duration / layover preferences / region rather than airport / etc.) into the right `flight fare` / `flight calendar` / `flight detail` / `flight gflight` invocation with proper --routing, --extension, and multi-airport arguments. Grounds the agent in Matrix's routing language, extension codes, and IATA metro/region groupings — knowledge that's NOT in `flight --help` and that the agent will otherwise hallucinate. Invoke whenever a user mentions flights, airfare, ITA Matrix, or any of the project's CLI commands.
---

# Flight search with flight-cli

This project wraps ITA Matrix's undocumented Alkali backend. The CLI is
powerful but the most useful constraints — alliance filters, no-overnight,
fare-basis, multi-airport — live in two opaque DSLs (routing language and
extension codes). This skill is the cheat sheet so you can translate user
intent into the right invocation **on the first try**.

## CLI surface

| Command | Purpose |
|---|---|
| `flight fare ORIGIN DEST --dep YYYY-MM-DD [--return YYYY-MM-DD]` | Specific-date search. 1 leg = one-way; 2 = round-trip; multi-city via `--slice`. |
| `flight calendar ORIGIN DEST --start YYYY-MM-DD [--end ...] [-d 5-7]` | Lowest-fare grid across a date window. Default round-trip; `--one-way` flips. |
| `flight detail ORIGIN DEST --dep YYYY-MM-DD --start ... --end ...` | Phase-2 of the calendar flow: full itineraries for a date picked from the grid. |
| `flight gflight ORIGIN DEST --dep YYYY-MM-DD` | Google Flights handoff via `fli`. Useful for booking links. |
| `flight airport QUERY` | IATA / partial-name autocomplete. |

Global flags (every command):
- `-v` / `-vv` — verbose logging (cache hits, retries) to stderr
- `--json` — emit raw response JSON
- `--no-cache` — bypass the on-disk response cache
- `--matrix-url` / `--google-url` — toggle deep-link emission

## The two opaque DSLs

| DSL | CLI flag | Wire field | Goes here for |
|---|---|---|---|
| Routing language | `--routing 'EXPR'` | `routeLanguage` | Carrier / airport / segment-shape filters (`LH+`, `BA AA`, `F* X:LHR F*`) |
| Extension codes | `--extension 'CODES'` (alias `--ext`) | `commandLine` | Itinerary-level limits (`MAXSTOPS 1`, `MAXDUR 18:00`, `-OVERNIGHTS`, `+CABIN 2`) |

**Putting one in the other returns `QPX Warning. Illegal COMMAND-LINE prefix`.** Keep them straight.

`--routing-ret` / `--ext-ret` set the return-direction values when they differ from outbound (round-trip and calendar modes).

## Intent → flag cheat sheet

| User says… | Reach for… |
|---|---|
| "max 1 stop" / "at most one connection" | `--stops 1` |
| "nonstop only" | `--stops 0` |
| "Star Alliance only" | `--extension 'ALLIANCE star-alliance'` |
| "Oneworld" / "SkyTeam" | `--extension 'ALLIANCE oneworld'` / `ALLIANCE skyteam` |
| "no red-eyes" | `--extension '-REDEYES'` |
| "no overnight layovers" | `--extension '-OVERNIGHTS'` |
| "no propeller planes" | `--extension '-PROPS'` |
| "business class" | `--cabin business` (or `--extension '+CABIN 2'` to enforce) |
| "premium economy" | `--cabin premium-coach` |
| "max 18 hours total" | `--extension 'MAXDUR 18:00'` |
| "min 90 minute connections" | `--extension 'MINCONNECT 1:30'` |
| "max 2 hour layovers" | `--extension 'MAXCONNECT 2:00'` |
| "only Lufthansa" | `--routing 'LH+'` |
| "only operated by Lufthansa" (no codeshares) | `--routing 'O:LH+'` |
| "fly via LHR" | `--routing 'F* X:LHR F*'` |
| "anywhere but US connections" | `--routing '~l:nUS+'` |
| "avoid AA and DL" | `--extension '-AIRLINES AA DL'` |
| "avoid connecting in DFW or ORD" | `--extension '-CITIES DFW ORD'` |
| "any morning departure" | `--depart-times morning` (or comma list: `morning,early-morning`) |
| "from New York City" | `NYC` (Matrix-native metro code; expands to JFK/LGA/EWR) |
| "from anywhere in the US East Coast" | `JFK,LGA,EWR,BOS,IAD,DCA,BWI,PHL,ATL,MIA` (see Airport groups) |
| "to Europe" | `LHR,CDG,FRA,AMS,IST,MAD,BCN,FCO,MUC,ZRH,VIE,CPH,DUB` (see Airport groups) |
| "I want to see prices across dates" | `flight calendar` not `flight fare` |
| "what's the cheapest week to fly?" | `flight calendar` with a wide `--end` |

## Routing language (`--routing`) — compressed reference

Multiple segments separated by **space**; alternatives within a segment by **comma, no spaces**. Brackets `[ ]` in Google's docs are illustrative; the wire string omits them.

**Operators:**
- `~` — negation (`~UA`, `~DFW`)
- `+` — one or more
- `*` — zero or more
- `?` — zero or one

**Prefixes** (all optional — bare 2-letter codes default to `C:`, bare 3-letter to `X:`):
- `C:` — marketing carrier (`AA` ≡ `C:AA`)
- `O:` — operating carrier (`O:LH` = LH metal, not codeshare)
- `X:` — connection airport (`NYC` ≡ `X:NYC`)
- `N`  — non-stop flight (`N:UA` = non-stop on UA)
- `F`  — any single flight; segment placeholder (`F+`, `F?`, `F*` work too)
- `L:` — country filter (`~l:nUS+` = no US connections)

There are **no `STAR+` / `oneworld+` carrier shortcuts** for alliances —
use `--extension 'ALLIANCE star-alliance'` instead.

**Canonical worked examples** (Google's routing-codes help dialog,
verbatim — see `docs/memories/matrix_help_docs.md` for the full
extracted text):

| Expression | Meaning |
|---|---|
| `N` | Non-stop flight only |
| `NYC` | Single stop in New York |
| `~NYC` | Single stop, not in New York |
| `DEN?` | Direct flight or one stop in Denver |
| `X?` | Direct flight or one stop anywhere |
| `~DEN?` | Direct flight or one stop anywhere but Denver |
| `EWR CVG SLC` | Stops in Newark, Cincinnati, and Salt Lake City |
| `AA` | Direct flight on AA (American) |
| `AA+` | Any number of flights on AA |
| `AA,UA` | Direct flight on either AA or UA |
| `~AA` | Direct flight, not on AA |
| `~AA,UA,DL` | Direct flight, not on AA, UA, or DL |
| `~AA,UA,DL+` | Any number of flights not on AA, UA, or DL |
| `AA+ DL+` | One or more flights on AA, then one or more on DL |
| `AA DL,AF` | Flight on AA, then flight on either DL or AF |
| `AA UA?` | One AA flight, optionally followed by a UA flight |
| `AA N?` | One AA flight, optionally followed by a non-stop on any airline |
| `AA25 UA814` | Flight AA25 followed by UA814 |
| `AA25 UA+` | Flight AA25 followed by one or more UA flights |
| `AA25 F+` | Flight AA25 followed by one or more flights on any airline |
| `DL CHI DL` | Two DL flights with a connection in Chicago |
| `O:UA` | Single flight operated by UA (not codeshares or UA subsidiaries) |
| `~UA882` | Single flight, but not UA882 |
| `UA1000-2000+` | One or more UA flights with numbers 1000–2000 |
| `~UA5000-9999,AA,DL+` | Any number of flights, none on AA/DL/UA-5000-9999 |

Routing language is for **shape** filters (which carriers, which
airports, which segment count). Aggregate constraints (alliance,
max-duration, connection times, no-overnights, cabin, fare-basis,
aircraft) all go in `--extension` — never in `--routing`. Codes are
**case-insensitive** (`lh+` ≡ `LH+`).

## Extension codes (`--extension`) — compressed reference

Multiple codes joined by **semicolon** (`;`). Args within a code by **space**. Times = `HH:MM`. Distances/counts = integers. Codes are case-insensitive.

**Itinerary constraints:**

| Code | Example | Meaning |
|---|---|---|
| `MAXSTOPS n` | `MAXSTOPS 1` | Max connecting stops per direction |
| `MAXDUR hh:mm` | `MAXDUR 18:00` | Max itinerary duration per direction |
| `MAXMILES n` / `MINMILES n` | `MAXMILES 8000` | Mileage bounds per direction |
| `MINCONNECT hh:mm` | `MINCONNECT 1:00` | Min layover |
| `MAXCONNECT hh:mm` | `MAXCONNECT 2:00` | Max layover |
| `PADCONNECT hh:mm` | `PADCONNECT 0:30` | Add buffer to airline minimum |
| `-OVERNIGHTS` | `-OVERNIGHTS` | Exclude overnight stays at hubs |
| `-REDEYES` | `-REDEYES` | Exclude overnight flights |
| `-PROPS` | `-PROPS` | Exclude propeller aircraft |
| `-CODESHARE` | `-CODESHARE` | Disallow codeshares |
| `-NOFIRSTCLASS` | `-NOFIRSTCLASS` | Require flights that have a first-class cabin |

**Carrier filters:**

| Code | Example | Meaning |
|---|---|---|
| `ALLIANCE x\|y\|…` | `ALLIANCE star-alliance` | Restrict to alliance(s). Multiple via `\|`. |
| `AIRLINES x y …` | `AIRLINES BA AF KL` | Only these marketing carriers |
| `-AIRLINES x y …` | `-AIRLINES AA UA DL` | Exclude these marketing carriers |
| `OPAIRLINES x y …` | `OPAIRLINES LH` | Only these operating carriers (no codeshares from non-LH metal) |
| `-OPAIRLINES x y …` | `-OPAIRLINES YV` | Exclude these operating carriers |

**Airport filters:**

| Code | Example | Meaning |
|---|---|---|
| `-CITIES x y …` | `-CITIES DFW ORD` | Don't connect at these cities |

**Cabin filters:**

| Code | Example | Meaning |
|---|---|---|
| `+CABIN n …` | `+CABIN 1 2` | Require booking in first or business cabin |
| `-CABIN n …` | `-CABIN 3` | Prohibit booking in economy |

Cabin values: `1`=first, `2`=business, `premium-coach`=premium economy, `3`=economy.

**Fare-basis filters:**

| Pattern | Example | Meaning |
|---|---|---|
| `F BC=code\|BC=code` | `F bc=y\|bc=b` | Specific prime booking codes |
| `F CC.AAA+BBB.FFFFFF` | `F aa.lon+chi.yup` | Carrier + market + fare basis |
| `F ..FFFFFF` | `F ..yup` | Fare basis only (any carrier, any market) |
| `F ..F-` | `F ..y-` | Wildcard — fare bases starting with letter |

**Combining example:**

```
ALLIANCE star-alliance; -REDEYES; MAXSTOPS 1; +CABIN 2; MINCONNECT 1:00; MAXDUR 14:00
```

Full reference: [uponarriving.com ITA Matrix guide](https://www.uponarriving.com/ita-matrix-guide/).

## Airport groups (region/metro → IATA expansion)

When users mention regions / metros, expand to the right IATA list. Two flavors:

**IATA metro codes Matrix accepts as a single token** (prefer these):

| Metro | Code | Airports it covers |
|---|---|---|
| New York City | `NYC` | JFK, LGA, EWR |
| London | `LON` | LHR, LGW, STN, LTN, LCY, SEN |
| Paris | `PAR` | CDG, ORY, BVA |
| Tokyo | `TYO` | NRT, HND |
| Moscow | `MOW` | SVO, DME, VKO |
| Stockholm | `STO` | ARN, BMA, NYO |
| Milan | `MIL` | MXP, LIN, BGY |
| Rome | `ROM` | FCO, CIA |
| Buenos Aires | `BUE` | EZE, AEP |
| São Paulo | `SAO` | GRU, CGH, VCP |
| Washington DC | `WAS` | IAD, DCA, BWI |
| Chicago | `CHI` | ORD, MDW |
| Houston | `HOU` | IAH, HOU |
| Bay Area | `QSF` | SFO, OAK, SJC |
| Osaka | `OSA` | KIX, ITM, UKB |
| Seoul | `SEL` | ICN, GMP |
| Beijing | `BJS` | PEK, PKX |

**Manual region expansions** (Matrix has no single code for these):

| Region | Comma-list |
|---|---|
| US East Coast | `JFK,LGA,EWR,BOS,IAD,DCA,BWI,PHL,ATL,MIA,FLL,CLT` |
| US West Coast | `LAX,SFO,SEA,PDX,SAN,OAK,SJC,LAS,PHX` |
| US major hubs (top 15) | `JFK,LGA,EWR,LAX,ORD,ATL,DFW,DEN,SFO,SEA,LAS,MIA,BOS,IAD,PHX` |
| Canada major | `YYZ,YVR,YUL,YYC,YEG` |
| Europe major hubs | `LHR,CDG,FRA,AMS,IST,MAD,BCN,FCO,MUC,ZRH,VIE,CPH,DUB` |
| UK & Ireland | `LHR,LGW,STN,LTN,MAN,EDI,GLA,DUB,ORK` |
| France & Iberia | `CDG,ORY,NCE,LYS,MAD,BCN,LIS,OPO,VLC,SVQ` |
| Germany / Switzerland / Austria | `FRA,MUC,BER,DUS,HAM,STR,ZRH,GVA,VIE` |
| Italy & Greece | `FCO,MXP,LIN,VCE,NAP,ATH,SKG,HER` |
| Nordics | `CPH,ARN,OSL,HEL,RIX` |
| East Asia hubs | `HND,NRT,KIX,ICN,PEK,PVG,HKG,TPE` |
| Southeast Asia hubs | `SIN,BKK,KUL,CGK,DPS,MNL,HAN,SGN` |
| South Asia | `DEL,BOM,BLR,MAA,HYD,KTM,CMB` |
| Middle East hubs | `DXB,AUH,DOH,RUH,JED,TLV,AMM` |
| Australia & NZ | `SYD,MEL,BNE,PER,AKL,WLG,CHC` |
| South America hubs | `GRU,GIG,SCL,EZE,LIM,BOG,UIO,PTY` |
| Africa hubs | `JNB,CPT,NBO,ADD,LOS,CMN,CAI` |
| Hawaii | `HNL,OGG,KOA,LIH` |

**Alliance-hub shortcuts** (useful with `ALLIANCE` extension):

| Alliance | European hubs | Asian hubs | US hubs |
|---|---|---|---|
| Star Alliance | `FRA,MUC,ZRH,VIE,CPH,IST,LIS` | `ICN,NRT,PEK,SIN` | `EWR,ORD,IAH,DEN,SFO,LAX` |
| Oneworld | `LHR,MAD,HEL,DUB` | `HKG,DOH,NRT` | `JFK,DFW,ORD,LAX,MIA` |
| SkyTeam | `CDG,AMS,FCO,PRG` | `ICN,CDG,PVG` | `JFK,ATL,DTW,MSP,SLC,LAX` |

Full reference: [airport_groups.md](../../docs/memories/airport_groups.md).

## Worked examples (user request → CLI invocation)

### Example 1: simple round-trip with stops cap
User: "find me round-trip JFK to LHR in mid-August, max 1 stop"
```bash
flight fare JFK LHR --dep 2026-08-15 --return 2026-08-22 --stops 1
```

### Example 2: alliance + cabin + duration
User: "Star Alliance from New York to Munich, business class, max 14 hours per direction"
```bash
flight fare NYC MUC --dep 2026-09-05 --return 2026-09-12 \
  --cabin business \
  --extension 'ALLIANCE star-alliance; MAXDUR 14:00; +CABIN 2'
```

### Example 3: lowest fare across a date window
User: "what's the cheapest week to fly NYC to Paris in October for a 5-7 night trip"
```bash
flight calendar NYC PAR --start 2026-10-01 --end 2026-10-31 -d 5-7
```

### Example 4: connect via a specific airport
User: "I want to fly JFK to Tokyo via Seoul on Star Alliance"
```bash
flight fare JFK TYO --dep 2026-11-01 \
  --routing 'F* X:ICN F*' \
  --extension 'ALLIANCE star-alliance'
```

### Example 5: avoid red-eyes + overnight layovers + props
User: "I hate red-eyes and don't want to overnight in a connecting city, and please no propeller planes"
```bash
flight fare LAX BOS --dep 2026-07-04 \
  --extension '-REDEYES; -OVERNIGHTS; -PROPS'
```

### Example 6: regional search
User: "find me a cheap flight from anywhere on the east coast to anywhere in Europe in September"
```bash
flight calendar 'JFK,LGA,EWR,BOS,IAD,DCA,BWI,PHL' \
                'LHR,CDG,FRA,AMS,IST,MAD,BCN,FCO,MUC,ZRH,VIE,CPH,DUB' \
                --start 2026-09-01 --end 2026-09-30 -d 7-10
```

### Example 7: specific carrier + connection time
User: "JFK to LHR on Lufthansa-operated metal only, with at least 1.5 hours between flights"
```bash
flight fare JFK LHR --dep 2026-08-15 --return 2026-08-22 \
  --routing 'O:LH+' \
  --extension 'MINCONNECT 1:30'
```

### Example 8: morning departure preference
User: "I want a morning departure from JFK to LHR"
```bash
flight fare JFK LHR --dep 2026-08-15 --return 2026-08-22 \
  --depart-times morning
```

### Example 9: multi-city (single-ticket — needs alliance constraint)
User: "round-the-world: NYC→Frankfurt→Singapore→Tokyo→NYC, all in August"
```bash
flight fare --slice 'EWR-FRA:2026-08-01' \
            --slice 'FRA-SIN:2026-08-08' \
            --slice 'SIN-NRT:2026-08-15' \
            --slice 'NRT-EWR:2026-08-22' \
            --extension 'ALLIANCE star-alliance'
```
**Why the alliance constraint matters:** Matrix can only return a multi-city
itinerary that books as a single ticket. That requires every leg's airline
to share an interline / fare-construction agreement. Without an alliance
constraint, the solver hunts across non-interlining carriers (e.g. Emirates
+ JAL + AA) and frequently returns "No solutions returned" even though each
leg individually has flights. Cross-alliance round-the-world tickets
generally have to be **stitched together as separate one-ways**, not done
as a single multi-city in Matrix. For single-ticket multi-city, always:
- Pin to one alliance via `--extension 'ALLIANCE oneworld|skyteam|star-alliance'`, OR
- Limit slices to a single carrier's network (e.g. all on LH+UA via Star), OR
- Run as separate `flight fare` queries and present prices summed.

### Example 10: fare-basis hunt
User: "find me business class (J-class) JFK to LHR"
```bash
flight fare JFK LHR --dep 2026-08-15 --return 2026-08-22 \
  --extension 'F BC=j'
```
Or for a carrier+booking-class combo (e.g. "AA in W class"):
```bash
flight fare JFK LHR --dep 2026-08-15 --return 2026-08-22 \
  --extension 'F aa..w-'
```
Note the trailing `-` on `w-` — it's the wildcard that matches any fare
basis starting with W (e.g. WCXLA, WQLOWUS). Without the dash, `F aa..w`
means "fare basis literally named 'w'", which usually doesn't exist and
returns no results.

### Example 11: paste-back from calendar to detail
User: "use that 2026-09-15 date from the calendar grid I just looked at"
```bash
flight detail NYC PAR --dep 2026-09-15 \
  --start 2026-09-01 --end 2026-09-30 -d 5-7
```
**Caveat:** the `calendarFollowup` endpoint can be flaky. If Matrix returns
`Internal server error` or empty results, the followup query needs the
exact `--start`/`--end`/`-d` of the original calendar that surfaced the
date. When in doubt, fall back to a plain `flight fare` for the specific
date — it returns the same shape of result without the followup
machinery.

## When a query returns nothing

Matrix sometimes returns zero solutions for queries that *should* match —
the calendar grid in particular has occasional server-side brownouts.
Recovery sequence:

1. **Simplify constraints** — drop the narrowest one (specific
   `--routing`, narrow fare-basis, alliance restriction) and rerun.
2. **Narrow date range OR origin/destination range, not both.** Wide ×
   wide queries are most brownout-prone.
3. **Fall back from `flight calendar` to `flight fare`** on a specific
   date — same shape, fewer moving parts.
4. **For multi-city**, always include `--extension 'ALLIANCE <name>'` so
   the solver can construct a single ticketable fare. Cross-alliance
   multi-city silently returns nothing even when each leg has flights.

## Where to dig deeper

Pin these for follow-up reading:

- [docs/memories/matrix_help_docs.md](../../docs/memories/matrix_help_docs.md) — **canonical text** extracted verbatim from Matrix's in-app help dialog: Itineraries / Faring / Aircraft Types / Routing Codes (Syntax + Examples + Glossary). Includes the **~500-row IATA aircraft-type code table** for `AIRCRAFT T:…` filtering — not inlined here for size. Source of truth when this skill's compressed tables are ambiguous.
- [docs/memories/routing_language.md](../../docs/memories/routing_language.md) — full grammar narrative + pitfalls
- [docs/memories/extension_codes.md](../../docs/memories/extension_codes.md) — full code table with curated common aircraft-code subset
- [docs/memories/airport_groups.md](../../docs/memories/airport_groups.md) — more region groupings and notes
- [docs/memories/wire_format_quirks.md](../../docs/memories/wire_format_quirks.md) — wire-level subtleties (mostly relevant when *extending* the CLI, less when invoking it)
- [docs/memories/public_alkali_wrapper.md](../../docs/memories/public_alkali_wrapper.md) — context on the project's role
- [CLAUDE.md](../../CLAUDE.md) — top-level project notes

## When this skill is the wrong tool

- User wants to **add a new search mode** (new `Search` variant in domain.py) → read CLAUDE.md and wire_format_quirks.md first; that's a coding task, not a query task.
- User wants to **debug a Matrix response** → start from raw `--json` output and `wire_format_quirks.md`, not this skill.
- User wants to **understand pricing logic** → Matrix's pricing isn't documented anywhere; this skill won't help.
