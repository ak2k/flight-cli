---
name: flight-search
description: Use when a user asks to find flights, search airfare, compare prices across dates, or invoke the `flight` CLI. Translates intent (origin, destination, dates, constraints like alliance / cabin / duration / layovers / region-rather-than-airport) into the right `flight search` / `calendar` / `detail` invocation with correct `--routing`, `--extension`, `--backend`, and multi-airport args. Provides Matrix's routing language, extension codes, and IATA metro/region groupings — knowledge that's not in `flight --help`.
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
| `flight search ORIGIN DEST --dep YYYY-MM-DD [--return YYYY-MM-DD]` | Specific-date search. Auto-picks Google Flights for plain cash queries and ITA Matrix when a constraint Google can't serve is set (ordered or positional routing, fare construction, `-REDEYES`/`-OVERNIGHTS`, multi-city slice, time-of-day buckets with a gap like `morning,evening`, infants, seniors, youth); a stop cap, a carrier include (`XX+`, `AIRLINES`) or one `ALLIANCE` (not both), `MAXDUR`, `MINCONNECT`/`MAXCONNECT`, one time-of-day window per leg, `--children`, carrier excludes (`~XX+`, `-AIRLINES`), operating carrier (`O:XX+`, `OPAIRLINES`), `-CODESHARE` and a lone flight number (`DL747`, `AS21+`, `AA1-3000`: the slice is that one flight) stay on Google, which serves its full board (`-n` above 30 works) and checks every row against them except the stop cap, `ALLIANCE` and `--children`, where Google's answer is taken as given. Carrier lists take space-separated codes (`-AIRLINES UA DL`); a comma list goes to Matrix, which refuses it. Force with `--backend matrix\|gflight`. PointsPath award overlay runs on both backends when tokens are present. |
| `flight calendar ORIGIN DEST --start YYYY-MM-DD [--end ...] [-d 5-7]` | Lowest-fare grid across a date window. Default round-trip; `--one-way` flips. Matrix answers first; a multi-airport grid names the airport pair behind each cell (`origin`/`destination` in JSON, a `route` column in the table), the two arguments `detail` takes; a table calendar then prints Google Flights' price graph for the same window, read through a headless Chrome while Matrix runs (one column per trip length of a range, each date priced at the set's cheapest for comma-lists or metro codes; at most 8 page loads). The graph takes a stop cap, cabin, adults, ONE carrier include (`XX+`, `AIRLINES`) or ONE `ALLIANCE`, `MAXDUR`, `MINCONNECT`/`MAXCONNECT`, and on a one-way a `--depart-times` window that ends at 23:59 (`night`, `evening,night`). A calendar Google can't ask gets a "not asked" line, a Google failure a "not shown" line, and neither changes Matrix's output or exit code; calendars run in parallel contend for one Chrome profile, and one that cannot open it prints "not shown" and still gets Matrix's answer. When the two lows differ, one stderr line after Google's table names each with its date pair, trip length and airports, says what both asked, and gives a `flight detail` and a `flight search --backend gflight` command, each repeating the calendar's flags, that show which fare is bookable; quote a fare from either table only after running its command. `--gf-transport http` skips Chrome. `--fast` shows the graph alone (one-way or one trip length like `-d 7`) and exits 1 rather than fall back. |
| `flight detail ORIGIN DEST --dep YYYY-MM-DD [--return YYYY-MM-DD] --start ... --end ... [-d 5-7]` | Phase-2 of the calendar flow: full itineraries for a date picked from the grid. Matrix only. Pass every filter the calendar was given — `--routing`/`--ext` (and `--routing-ret`/`--ext-ret`), `--depart-times`/`--return-times`, `--include-unavailable`, `--stops`, cabin, passengers — or its itineraries answer a wider question than the grid priced. |
| `flight explore ORIGIN [--month YYYY-MM] [--days A-B] [--max-price P]` | "Where can I fly from here, under this price?": Google Flights' explore page (Chrome), priced destinations cheapest first; `--days` must overlap exactly one of weekend (1-4), one week (6-9), two weeks (13-16) nights, and the trips listed span that whole length (5-7 lists 6-9 nights); `--month` must be in the next six months. For "who sells this itinerary cheapest", with each seller's bag fees and booking link, add `--sellers [--pick N]` to `flight search`. |
| `flight airport QUERY` | IATA / partial-name autocomplete. |
| `flight fare` / `flight gflight` | **Deprecated** aliases for `search --backend matrix` / `search --backend gflight` — still work for one release; emit a deprecation warning. Prefer `flight search` for new invocations. |

Global flags (every search-printing command):
- `-v` / `-vv` — verbose logging (cache hits, retries) to stderr
- `--json` — emit raw response JSON
- `--no-cache` — bypass the on-disk response cache
- `--matrix-url` / `--google-url` — toggle deep-link emission
- `--cash-only` — skip all award providers; show only the cash table
- `--format json --enrich --cash-only` — `search` on Google Flights: the default table's Google-vs-Matrix cross-check as `{"search": <the plain document>, "cross_check": {…}}` (`delta` = Google − Matrix only for the same trip in one currency, `reasons` on every other row, Matrix's `listed` of `solution_count`); plain `--format json` does not cross-check, though on auto a failed Google query is still handed to Matrix
- `--awards-only` — skip the cash table; show only the award provider output
- `--currency EUR` — price in that ISO 4217 currency on both backends (`search`, `calendar`, `detail`)
- `--fare-rules [--pick N]` — `search` only, routes to Matrix: fare basis, booking codes and refund/change penalties for itinerary N (default 1)
- `--verify [--pick N]` — `search` only, on a Google Flights table: "is Google's price for row N real?" Asks Matrix for exactly that itinerary (a routing chain of its flight numbers, then a flight-by-flight check of the booking details) and prints both prices, the fare basis, booking codes and rules; or the reason Matrix does not price it (`other-itinerary`, `no-solution`, `carrier-absent`). JSON: `{"search", "verify"}`, `delta` = Google − Matrix. One `--cabin`; not with `--bags`, `--sellers`, `--fare-rules` or a search that runs on Matrix. A Matrix chain search takes 30-45 s.
- `--max-price N` — `search` only: fares at or under N in the search's currency (`--currency`, default USD), compared with the printed price (a party's total). Google is asked for it in USD and every row is checked; Matrix is asked in the cap's currency and its answer cut to it. One `--cabin`.
- `--bags CHECKED[,CARRY]` — `search` only, Google Flights only: prices fares with CHECKED checked bags and CARRY (0 or 1, default 0) carry-ons, and labels each row with the bags Google says its price includes (`incl.` / `not incl.` / `unknown`; JSON `bags_included`). Refused rather than sent to Matrix (which prices no bags); one traveler, one `--cabin`, no `--sellers`.
- Google rows carry Google's own CO2 estimate, no flag needed: JSON `co2_emissions_g`, `co2_emissions_typical_g` (the route's typical), `co2_emissions_delta_pct` and `emissions_tag` (`lower` / `typical` / `higher`), plus each leg's `co2_emissions_g`; the table's `CO2 kg` column shows kilograms and the percent (green lower, red higher). Null or blank where Google states none.
- `--providers pp[,seats]` — restrict to a named subset of award providers (default: all configured)
- Awards over an airport set or metro code (`JFK,EWR`, `NYC`) are asked pair by pair, at most 8 pairs a search (pairs the cash rows fly first); the pairs left out are named on stderr and in the JSON leg's `pairs_not_asked`, so narrow the set rather than trust a missing award there
- `--provider-opt KEY=VAL` — per-provider override, repeatable, e.g. `--provider-opt pp.airlines=United,Delta` or `--provider-opt pp.cabins=Economy,Business`. Defaults live in `~/.config/flight-cli/config.toml` under `[providers.<name>]` tables.

## Intent → flag cheat sheet

| User says… | Reach for… |
|---|---|
| "max 1 stop" / "at most one connection" | `--stops 1` (per direction) |
| "nonstop only" | `--stops 0` (per direction) |
| "Star Alliance only" | `--extension 'ALLIANCE star-alliance'` |
| "Oneworld" / "SkyTeam" | `--extension 'ALLIANCE oneworld'` / `ALLIANCE skyteam` |
| "no red-eyes" | `--extension '-REDEYES'` |
| "no overnight layovers" | `--extension '-OVERNIGHTS'` |
| "no propeller planes" | `--extension '-PROPS'` |
| "business class" / "premium economy" / "first class" | `--cabin business` / `premium-coach` / `first` (or `--extension '+CABIN N'` to enforce; see Cabin filters below) |
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
| "I want to see prices across dates" | `flight calendar` not `flight search` |
| "what's the cheapest week to fly?" | `flight calendar` with a wide `--end` |
| "just give me Google Flights booking results" | `flight search` (auto-picks gflight for plain cash queries) |
| "force Matrix even though my query is plain" | `flight search --backend matrix` |

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

**Round trips.** Each slice reads its routing from its own origin. `--routing`
is the outbound's and `--routing-ret` the return's (`''` for none), on
`search`, `calendar` and `detail`. Unset, the return gets `--routing` only when
it reads the same both ways (`AA+`, `~BA+`, `N`, `F* X:LHR F*`,
`DFW,DEN DEN,DFW`). An ordered chain (`UA LH`, `F+ X:LHR F*`) or a flight
number (`DL747`) on a round trip needs `--routing-ret`, or the command exits 2:
give the chain reversed (`--routing 'UA LH' --routing-ret 'LH UA'`), the
return flight's number, or `''`. Extension codes are not positional, so `--ext`
is copied unless `--ext-ret` replaces it. Legs with different codes go to
Matrix, since Google Flights writes one filter set on both legs.

## Extension codes (`--extension`) — compressed reference

Multiple codes joined by **semicolon** (`;`). Args within a code by **space**. Times = `HH:MM`. Distances/counts = integers.

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

**IATA metro codes Matrix accepts as a single token** (prefer these). `flight
search` and `flight calendar --fast` serve them on Google Flights over every
airport listed; `HOU`, `LAX`, `BER`, `SHA`, `BKK` and `DPS` are also airport
codes and stay that one airport there. A leg of more than 11 airports (origins
plus destinations, metro codes counted as their members) goes to Matrix, and
`--fast` refuses it:

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
flight search JFK LHR --dep 2026-08-15 --return 2026-08-22 --stops 1
```
This is a plain cash query, so `flight search` auto-picks the Google Flights
backend (faster, broader carrier coverage). To force ITA Matrix instead — say
the user is logged into PointsPath and wants the award overlay — add
`--backend matrix`.

### Example 2: alliance + cabin + duration
User: "Star Alliance from New York to Munich, business class, max 14 hours per direction"
```bash
flight search NYC MUC --dep 2026-09-05 --return 2026-09-12 \
  --cabin business \
  --extension 'ALLIANCE star-alliance; MAXDUR 14:00; +CABIN 2'
```

### Example 3: lowest fare across a date window
User: "what's the cheapest week to fly NYC to Paris in October for a 5-7 night trip"
```bash
flight calendar NYC PAR --start 2026-10-01 --end 2026-10-31 -d 5-7
```
Matrix's grid prints first, then Google Flights' price graph. If their lows
differ, the stderr note names both and the two searches to run before quoting
either: Google's low can be a fare with more stops than Matrix's grid allows,
or one Matrix's calendar leaves out.

### Example 4: connect via a specific airport
User: "I want to fly JFK to Tokyo via Seoul on Star Alliance"
```bash
flight search JFK TYO --dep 2026-11-01 \
  --routing 'F* X:ICN F*' \
  --extension 'ALLIANCE star-alliance'
```

### Example 5: avoid red-eyes + overnight layovers + props
User: "I hate red-eyes and don't want to overnight in a connecting city, and please no propeller planes"
```bash
flight search LAX BOS --dep 2026-07-04 \
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
flight search JFK LHR --dep 2026-08-15 --return 2026-08-22 \
  --routing 'O:LH+' \
  --extension 'MINCONNECT 1:30'
```

### Example 8: morning departure preference
User: "I want a morning departure from JFK to LHR"
```bash
flight search JFK LHR --dep 2026-08-15 --return 2026-08-22 \
  --depart-times morning
```

### Example 9: multi-city (always pin to one alliance)
User: "round-the-world: NYC→Frankfurt→Singapore→Tokyo→NYC, all in August"
```bash
flight search --slice 'EWR-FRA:2026-08-01' \
            --slice 'FRA-SIN:2026-08-08' \
            --slice 'SIN-NRT:2026-08-15' \
            --slice 'NRT-EWR:2026-08-22' \
            --extension 'ALLIANCE star-alliance'
```

### Example 10: fare-basis (use wildcard `-` suffix or `BC=`)
User: "find me business class (J-class) JFK to LHR"
```bash
flight search JFK LHR --dep 2026-08-15 --return 2026-08-22 \
  --extension 'F BC=j'
```
AA in W class:
```bash
flight search JFK LHR --dep 2026-08-15 --return 2026-08-22 \
  --extension 'F aa..w-'
```

### Example 11: paste-back from calendar to detail
User: "use that 2026-09-15, 6-night cell from the calendar grid I just looked at"
(the grid was `flight calendar NYC PAR --start 2026-09-01 --end 2026-09-30 -d 5-7
--routing 'AF+' --depart-times morning`). Repeat the calendar's window and every
filter it was given. NYC and PAR are metro codes, so the calendar asked one
query per airport pair and each cell names the pair that priced it: the `route`
column for the day's minimum, the pair printed beside a trip length another pair
priced, and `origin`/`destination` on each day and trip length in `--json`. Give
`detail` that pair, not the calendar's codes. Here the 6n price has no pair
beside it and the row's route is `JFK→CDG`:
```bash
flight detail JFK CDG --dep 2026-09-15 --return 2026-09-21 \
  --start 2026-09-01 --end 2026-09-30 -d 5-7 \
  --routing 'AF+' --depart-times morning
```
A calendar of one airport pair has no `route` column: give `detail` its codes.
A cell that a round trip's combined query priced names the calendar's own codes
(`NYC→PAR`); give `detail` those.

## When a query returns nothing

Matrix sometimes returns zero solutions for queries that *should* match —
the calendar grid in particular has occasional server-side brownouts. When a
search fails outright rather than coming back empty, run `flight doctor` first:
it names the broken backend or credential. Recovery sequence:

1. **Simplify constraints** — drop the narrowest one (specific
   `--routing`, narrow fare-basis, alliance restriction) and rerun.
2. **Narrow date range OR origin/destination range, not both.** Wide ×
   wide queries are most brownout-prone.
3. **Fall back from `flight calendar` to `flight search --backend matrix`**
   on a specific date — same shape, fewer moving parts.
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
