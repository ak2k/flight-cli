# flight-cli

A power-user CLI for airfare discovery. Wraps ITA Matrix's undocumented
backend for full routing-language and extension-code support, hands off to
Google Flights for booking, with on-disk caching and golden-file regression
tests against captured wire bodies.

> Not affiliated with Google, ITA Software, or ITA Matrix. Uses Matrix's
> public-API-key endpoint the same way the web UI does.

## Install

```sh
git clone https://github.com/ak2k/flight-cli
cd flight-cli
uv venv && uv pip install -e .
```

Requires Python 3.11+.

## What it does

```sh
# specific-date search — auto-picks the backend.
# Plain cash search → Google Flights (fast, broad coverage).
flight search JFK LHR --dep 2026-08-15 --return 2026-08-22

# Power-user flag → auto-flips to ITA Matrix (routing language,
# extension codes, multi-city slices, time-of-day filters all live here).
flight search MIA PAR --dep 2026-06-15 \
    --routing "LH+" --ext "MAXCONNECT 2:00"

# Force a backend explicitly:
flight search JFK LHR --dep 2026-08-15 --backend matrix
flight search JFK LHR --dep 2026-08-15 --backend gflight

# lowest-fare calendar across a date window (one Matrix call returns
# 30 days × N durations of priced options)
flight calendar MIA PAR --start 2026-06-07 -d 5-7 \
    --routing "LH+" --ext "MAXCONNECT 2:00"

# phase-2 of the calendar flow: full itineraries for a picked date
flight detail MIA PAR --dep 2026-06-01 --return 2026-06-07 \
    --routing "LH+" --ext "MAXCONNECT 2:00" --duration 5-7

# IATA autocomplete
flight airport LON

# Award overlay (PointsPath, seats.aero) is implicit on BOTH backends once
# you've logged in (`flight auth pp login`). --cash-only skips it;
# --awards-only shows only the award table.
flight search JFK LHR --dep 2026-08-15
```

`flight fare` and `flight gflight` are deprecated aliases for `flight search
--backend matrix` and `flight search --backend gflight` respectively. They
still work for one release; --help marks them deprecated.

Every result-printing command supports:

- `--matrix-url` — print a deep-link that opens the same search in ITA Matrix's web UI
- `--google-url` — print a structured Google Flights URL (`tfs=` protobuf) that opens directly to the search
- `--pick N` — pin itinerary #N (1-based, as shown in the table) in the `--matrix-url` / `--google-url` deep links instead of the cheapest
- `--json` — machine-readable output
- `--no-cache` — bypass the on-disk response cache (`~/.cache/flight-cli/`)

### Power-user features

- **Routing language** (`--routing`): `LH+` (any Lufthansa-group leg), `BA AA` (BA or AA only), `[F* X F*]` (any flight, then X, then any). [More codes →](https://www.nicethis.com/itamatrix.aspx)
- **Extension codes** (`--extension`): `MAXCONNECT 5:00`, `MAXSTOPS 1`, `MINMILES 3000`, `-REDEYES`, `-OVERNIGHTS`, `ALLIANCE oneworld`.
- **Multi-airport**: `flight calendar MIA VIE,PAR,FCO,MAD --start ...` — search across N European cities at once.
- **Time-of-day filters** (`--depart-times`, `--return-times`): `morning,evening` etc.
- **Stop limits** (`--stops N`): `0` = nonstop only, `1` = up to one stop, … Honored on both backends.
- **Calendar-mode duration ranges** (`-d 5-7`): one search returns prices for 5-, 6-, and 7-night trips at every starting day.

## Award overlay

When you've logged in (`flight auth pp login`), `flight search` automatically
overlays award availability onto each cash itinerary it returns — on **both**
backends. Each row shows the airline-native miles cost, taxes, the banks whose
points transfer to that program, cents-per-mile valuation, and a stops marker
so a nonstop award is distinguishable from a connection at a glance.
Round-trips render one table per leg.

Award data comes from a provider registry behind a common `AwardProvider`
interface. Two providers ship today:

- **[PointsPath](https://pointspath.com)** — transferable-points award pricing
  (requires a paid subscription; see Setup below).
- **[seats.aero](https://seats.aero)** — award availability across programs
  (requires an API key).

Each configured provider auto-enables and fans out per leg; the cash↔award
matcher and renderers are provider-blind.

```sh
# implicit overlay — any search adds the award table when a provider is configured
flight search JFK LHR --dep 2026-08-15

# skip the overlay even when configured (cash only)
flight search JFK LHR --dep 2026-08-15 --cash-only

# award-only listing (skip the cash table render)
flight search JFK LHR --dep 2026-08-15 --awards-only

# restrict to specific providers
flight search JFK LHR --dep 2026-08-15 --providers pp

# limit the cabin set (default: Economy + Business)
flight search JFK LHR --dep 2026-08-15 --cabin Economy

# per-provider override (e.g. PointsPath airline set); repeatable
flight search JFK LHR --dep 2026-08-15 --provider-opt 'pp.airlines=United,Delta,American'
```

### Setup

PointsPath requires a paid subscription (free tier is the browser extension only). Three login modes:

**1. Headed browser login (default, recommended).** Opens a Patchright Chrome so you can sign in normally; the CLI captures the resulting session into `~/.config/flight-cli/pp.json`. Independent of any Chrome PP session you have open elsewhere — different server-side Supabase session, so the refresh chains never race.

We use [Patchright](https://pypi.org/project/patchright/) (a drop-in Playwright fork that patches the CDP `Runtime.enable` leak and the `navigator.webdriver` flag) because pointspath.com is behind Cloudflare's bot fingerprint check, which stock Playwright fails. The browser profile is persisted at `~/.cache/flight-cli/browser-profile/` so the Cloudflare `cf_clearance` cookie survives across login sessions — you usually only have to clear the human-check once.

```sh
# One-time: download real Chrome (~150MB) into Patchright's cache.
# `channel="chrome"` uses the real Chrome binary because its TLS
# fingerprint matches real Chrome traffic — bundled Chromium doesn't.
uvx --from patchright patchright install chrome

# Then log in. `--with patchright` adds the Python package ephemerally
# for this one invocation — no need to mutate flight-cli's venv.
uv run --with patchright flight auth pp login
flight auth pp whoami     # confirm
```

If you'd rather make patchright a permanent venv resident (skip `--with` every time), there's an optional install extra: `uv pip install -e '.[browser-login]'`. Most users don't need this.

**2. `--from-chrome` (cookie import).** Reads Supabase cookies from your local Chrome profile via `rookiepy`. Quicker than headed login since you don't sign in again — but the CLI then *shares* Chrome's refresh-token chain. Supabase rotates refresh tokens single-use, so a refresh on one side will eventually invalidate the other. Use this when you don't mind re-importing periodically.

```sh
flight auth pp login --from-chrome
```

**3. `--tokens-file PATH` (JSON import).** Bring your own session JSON. Useful when you've captured tokens with another tool (CDP cookie sniff, browser DevTools, etc.).

```sh
flight auth pp login --tokens-file ~/Downloads/pp_tokens.json
# Expected file shape:
# {"access_token": "...", "refresh_token": "...", "user": {"email": "..."}}
```

Once tokens are saved, refresh is automatic for the lifetime of the refresh-token chain (~indefinite, modulo the rotation race in mode 2).

### How PointsPath airline selection works

On each award overlay (cached for 24h / 7d respectively):

1. `GET /api/pricing-info` — universe of supported airlines + their transfer-partner banks
2. `GET /api/extension-config` — your account's enabled feature flags
3. The airlines fanned out are: pricing-info entries minus those with `enable<Airline>=0` in the feature flags. Always-on airlines (American, Delta, United, JetBlue, Alaska) have no toggle and are always included.

Pass `--provider-opt 'pp.airlines=United,Delta,...'` to skip discovery and call only the named set.

### What it doesn't do

- ~~Browser-based login~~ (now the default — see Setup above)
- Award overlay on `calendar` (lowest-fare-calendar) — fan-out is N days × M airlines; deserves its own design
- Match against airlines we don't yet support (the few in pricing-info but not enabled for your tier are silently skipped)

## Architecture

The codebase is a small pydantic discriminated union with match-based
adapters — adding a new search mode or a new backend is mechanical and
type-checked.

```
src/flight_cli/
  domain.py        SpecificDateSearch | CalendarSearch | CalendarFollowup
                   + SearchOptions + Leg + TimeOfDay
  wire.py          to_wire(search) → typed WireBody (Matrix API request)
  links.py         matrix_deep_link / matrix_itinerary_url, google_flights_url
                   + pinned (--pick N) deep-link encoders
  client.py        MatrixClient.execute(search)
  fli_bridge.py    Google Flights handoff via the `flights` (fli) pypi package
  _gflight_ids.py  gflight query wrapper: captures opaque flight ids;
                   persists the session NID cookie (TTL'd) so each run starts
                   warm, and retries cold-session empties as a fallback
  cli.py           typer commands (search / calendar / detail / airport + auth)
  models.py        response models
  _http.py         httpx + curl_cffi + aiolimiter + stamina
  providers/       award-provider registry behind a common AwardProvider protocol
    base.py        AwardFlight / AwardProvider / LegQuery
    registry.py    gather_awards: construct enabled providers, fan out per leg
    pointspath/    PointsPath provider
    seats_aero/    seats.aero provider
  pp/              PointsPath client + cash↔award matcher + `auth pp` subapp
    auth.py        Supabase JWT store + refresh
    client.py      airline-search / pricing-info / extension-config (cached)
    match.py       cash↔award join by (flight#, date) / (route, time) / matched id
    cli.py         auth subapp + award overlay wired into `search`
    models.py      PointsPath response shapes
tests/
  fixtures/        captured SPA wire bodies (golden files)
  test_wire_round_trip.py
  pp/              PointsPath model + match + helper unit tests
  seats_aero/      seats.aero provider unit tests
```

Run tests with `pytest tests/`.

## Why does this exist

ITA Matrix is dramatically more powerful than consumer flight-search sites —
routing language, extension codes, lowest-fare calendars — but the web UI is
clunky and there's no published API. This CLI captures everything Matrix can
do behind a fluent command-line interface, plus hands off to Google Flights
for the actual booking flow.

## Acknowledgements

- [AWeirdDev/fast-flights](https://github.com/AWeirdDev/fast-flights) — Google Flights `tfs=` protobuf encoder
- [punitarani/fli](https://github.com/punitarani/fli) — Google Flights API client (`flights` on PyPI)
- [adamhwang/ita-matrix-powertools](https://github.com/adamhwang/ita-matrix-powertools) — userscript that documented several Matrix internals

## License

MIT
