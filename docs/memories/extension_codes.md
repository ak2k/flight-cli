# Extension codes (`commandLine` field, `--extension` flag)

Matrix's per-slice extension codes string. Lives in `slices[].commandLine`
(NOT `routeLanguage` — see [wire_format_quirks.md](wire_format_quirks.md)).
The CLI exposes it via `--extension` (alias `--ext`) on `flight fare` and
`flight calendar`, and the domain field is `Leg.extension`.

**This is the grammar for itinerary-level constraints** — max stops, min/max
connection times, duration caps, mileage caps, alliance/carrier filters,
overnight/redeye/prop exclusions, cabin requirements, fare-basis filters.

For "what carriers and segments the routing must contain" → use the
**routing language** instead (see [routing_language.md](routing_language.md)).

## Canonical reference

Best community reference (no first-party docs exist):
**https://www.uponarriving.com/ita-matrix-guide/**

## Wire format

Multiple extension codes are separated by **semicolon** (`;`). Args within
one code are separated by **space**. The wire string is the whole semicolon-
joined list — no outer brackets, no leading `[/`:

```
commandLine: "MAXCONNECT 2:00"
commandLine: "ALLIANCE star-alliance; -REDEYES; MAXSTOPS 1; +CABIN 2"
commandLine: "MAXDUR 18:00; -OVERNIGHTS"
```

## Units

- **Times**: `HH:MM` (e.g. `MAXCONNECT 2:00`, `MAXDUR 18:00`, `PADCONNECT 0:30`)
- **Distances**: plain integers in miles (e.g. `MAXMILES 2900`)
- **Counts**: plain integers (e.g. `MAXSTOPS 1`)
- **Carrier/airport codes**: IATA 2- or 3-letter, space-separated for lists

## Itinerary constraint codes

| Code | Example | Meaning |
|---|---|---|
| `MAXSTOPS n` | `MAXSTOPS 1` | Max connecting stops per direction |
| `MAXDUR hh:mm` | `MAXDUR 18:00` | Max itinerary duration per direction |
| `MAXMILES n` | `MAXMILES 8000` | Max miles flown per direction |
| `MINMILES n` | `MINMILES 2600` | Min miles flown per direction |
| `MINCONNECT hh:mm` | `MINCONNECT 1:00` | Min connection time at any layover |
| `MAXCONNECT hh:mm` | `MAXCONNECT 2:00` | Max connection time at any layover |
| `PADCONNECT hh:mm` | `PADCONNECT 0:30` | Extra padding over airline's min connection time |
| `-OVERNIGHTS` | `-OVERNIGHTS` | Exclude itineraries requiring overnight stops |
| `-REDEYES` | `-REDEYES` | Exclude overnight (red-eye) flights |
| `-PROPS` | `-PROPS` | Exclude propeller aircraft |
| `-CODESHARE` | `-CODESHARE` | Disallow codeshare flights |
| `-NOFIRSTCLASS` | `-NOFIRSTCLASS` | Require flights that have a first-class cabin (but you can still book in any cabin) |

## Carrier filters

| Code | Example | Meaning |
|---|---|---|
| `ALLIANCE code\|code\|…` | `ALLIANCE star-alliance` | Restrict to alliance(s). Multiple via `\|`. Values: `oneworld`, `skyteam`, `star-alliance` |
| `AIRLINES code …` | `AIRLINES BA AF` | Allow only these marketing carriers |
| `-AIRLINES code …` | `-AIRLINES AA BA` | Prohibit these marketing carriers |
| `OPAIRLINES code …` | `OPAIRLINES AA` | Allow only flights operated by these carriers |
| `-OPAIRLINES code …` | `-OPAIRLINES AA` | Prohibit flights operated by these carriers |

## Airport filters

| Code | Example | Meaning |
|---|---|---|
| `-CITIES code …` | `-CITIES DFW ORD` | Prohibit these connection cities |

## Cabin codes

| Code | Example | Meaning |
|---|---|---|
| `+CABIN n …` | `+CABIN 1` | Require booking in these cabin(s) |
| `-CABIN n …` | `-CABIN 3` | Prohibit booking in these cabin(s) |

Cabin values: `1` = first, `2` = business, `premium-coach` = premium economy,
`3` = economy.

## Fare-basis codes

Syntax: `F carrier.city1+city2.farebasis`. The pattern is
`F` followed by carrier, market (city pair joined by `+`), and fare basis,
each segment optional and dot-separated. Multiple alternates joined by `|`.

| Pattern | Example | Meaning |
|---|---|---|
| `F BC=code` | `F bc=y` | Prime booking code (any carrier, any market) |
| `F BC=code\|BC=code` | `F bc=y\|bc=b` | Alternative prime booking codes |
| `F CC.AAA+BBB.FFFFFF` | `F aa.lon+chi.yup` | Carrier + market + fare basis |
| `F ..FFFFFF` | `F ..yup\|..f` | Fare basis only |
| `F .AAA+BBB.` | `F .lon+chi.` | Market only |
| `F CC..FFFFFF` | `F aa..yup\|aa..f` | Carrier + fare basis, any market |
| `F ..F-` | `F ..y-\|..b-` | Wildcards — fare bases starting with the given letter |

**Caveat**: actual class used may differ from what you asked for, "due to being
overridden by the carrier's booking code exception table." Watch the fare
rules in returned itineraries to confirm.

## Combining

Semicolons join. Whitespace around the semicolon is allowed but not required:

```
ALLIANCE star-alliance; -REDEYES; MAXSTOPS 1; +CABIN 2; f bc=w
```

Order doesn't matter to the server.

## Per-direction application

Like `routeLanguage`, `commandLine` is **per-slice**. Outbound and return get
independent extension strings. The CLI exposes both via `--extension` /
`--extension-ret` (or `--ext` / `--ext-ret`). Multi-city sets one per slice.

## Pitfalls

1. **Don't put routing-language expressions here.** `LH+`, `BA AA`,
   `F* X:LHR F*` belong in `routeLanguage`. The error from Matrix is loud:
   `QPX Warning.  Illegal COMMAND-LINE prefix: ...`.
2. **`MAXSTOPS` is per direction.** Round-trip with `MAXSTOPS 1` on both
   slices = max 1 stop outbound AND max 1 stop return. There's no
   "total stops across the trip" code.
3. **Routing-language has overlapping global modifiers** that look similar
   but use different syntax. `[/ minconnect 45]` (routing-language, minutes
   int) vs `MINCONNECT 0:45` (extension code, HH:MM). Equivalent for the
   server; pick one per concept.
4. **`-OVERNIGHTS` excludes itineraries with overnight stops.** `-REDEYES`
   excludes overnight flights (red-eyes). Different concepts — use both if
   you want neither.
