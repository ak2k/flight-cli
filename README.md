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
# specific-date search — one-way, round-trip, or multi-city
flight fare JFK LHR --dep 2026-08-15 --return 2026-08-22

# lowest-fare calendar across a date window (one API call returns
# 30 days × N durations of priced options)
flight calendar MIA PAR --start 2026-06-07 -d 5-7 \
    --routing "LH+" --ext "MAXCONNECT 2:00"

# phase-2 of the calendar flow: full itineraries for a picked date
flight detail MIA PAR --dep 2026-06-01 --return 2026-06-07 \
    --routing "LH+" --ext "MAXCONNECT 2:00" --duration 5-7

# hand off to Google Flights for actual booking results
flight gflight JFK LHR --dep 2026-08-15 --return 2026-08-22

# IATA autocomplete
flight airport LON
```

Every result-printing command supports:

- `--matrix-url` — print a deep-link that opens the same search in ITA Matrix's web UI
- `--google-url` — print a structured Google Flights URL (`tfs=` protobuf) that opens directly to the search
- `--json` — machine-readable output
- `--no-cache` — bypass the on-disk response cache (`~/.cache/flight-cli/`)

### Power-user features

- **Routing language** (`--routing`): `LH+` (any Lufthansa-group leg), `BA AA` (BA or AA only), `[F* X F*]` (any flight, then X, then any). [More codes →](https://www.nicethis.com/itamatrix.aspx)
- **Extension codes** (`--extension`): `MAXCONNECT 5:00`, `MAXSTOPS 1`, `MINMILES 3000`, `-REDEYES`, `-OVERNIGHTS`, `ALLIANCE oneworld`.
- **Multi-airport**: `flight calendar MIA VIE,PAR,FCO,MAD --start ...` — search across N European cities at once.
- **Time-of-day filters** (`--depart-times`, `--return-times`): `morning,evening` etc.
- **Calendar-mode duration ranges** (`-d 5-7`): one search returns prices for 5-, 6-, and 7-night trips at every starting day.

## Architecture

The codebase is a small pydantic discriminated union with match-based
adapters — adding a new search mode or a new backend is mechanical and
type-checked.

```
src/flight_cli/
  domain.py        SpecificDateSearch | CalendarSearch | CalendarFollowup
                   + SearchOptions + Leg + TimeOfDay
  wire.py          to_wire(search) → typed WireBody (Matrix API request)
  links.py         to_matrix_deep_link, google_flights_url
  client.py        MatrixClient.execute(search)
  fli_bridge.py    Google Flights handoff via the flights pypi package
  cli.py           typer commands
  models.py        response models
  _http.py         httpx + curl_cffi + aiolimiter + stamina
tests/
  fixtures/        captured SPA wire bodies (golden files)
  test_wire_round_trip.py
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
