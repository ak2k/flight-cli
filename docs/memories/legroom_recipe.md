# Legroom & amenities — recipe

How `flight search` (gflight backend) surfaces per-leg legroom, cabin, and
in-flight amenities, mirroring what the Legrooms+ Chrome extension shows.

**Headline finding (work-5ewe, 2026-05):** This data isn't fetched from a
third-party API. Google Flights' own `/GetShoppingResults` response already
carries it in the per-leg tuple at `data[0][2][i]` indices 12–17. The
Legrooms+ extension's `load_flight_data.js` hooks XHR and parses exactly
these indices. Our gflight backend hits the same endpoint (via `fli`), so
the same fields are available — we just had to read them.

## Per-leg tuple indices

In `data[0][2][i]` (each physical flight within an itinerary):

| Index | Type            | Meaning                                                                         |
| ----- | --------------- | ------------------------------------------------------------------------------- |
| 12    | list (sparse)   | Amenities. Positional bits decoded below.                                       |
| 13    | int             | Legroom class enum (see table).                                                 |
| 14    | str             | Pitch — e.g. `"31 in"`. Occasionally bare int.                                  |
| 15    | list            | Codeshare carriers — `[['KL', '6101', None, 'KLM'], ...]`. We don't read it.    |
| 16    | int             | Cabin enum (see table).                                                         |
| 17    | str             | Aircraft type — e.g. `"Airbus A330"`, `"Boeing 777-300ER"`.                     |

Indices 3, 6, 8, 10, 11, 20, 21, 22 are already used by `fli`; see
`src/flight_cli/_gflight_ids.py` for the full map.

## Legroom class enum (index 13)

```
1 → AVERAGE         ← pitch shown in default color
2 → BELOW           ← pitch tagged [red]
3 → ABOVE           ← pitch tagged [green]
4 → Extra Reclining
5 → Lie Flat
6 → Suite
8 → Reclining
9 → Angled Flat
```

For coach, AVERAGE/BELOW/ABOVE pair with the pitch number — the renderer
collapses these to color on the pitch token (no text label) since they're
pitch-relative judgments and the number itself is the signal. For premium
cabins, the seat-type enums (Lie Flat / Suite / Angled Flat / Reclining)
print as text because they convey seat *construction* not relative pitch.

## Cabin enum (index 16)

```
1 → ECONOMY
2 → PREMIUM     (premium economy)
3 → BUSINESS
4 → FIRST
```

Mixed-cabin itineraries are real and worth surfacing: a "business" search
can return an outbound with `J Suite` on the long-haul leg and `Y 32" ABOVE`
on the regional connector.

## Amenities array (index 12)

A 12-element list with booleans (`True`/`None`) at fixed positions.
Decoded by `_decode_wifi` / `_decode_power` / `_decode_video`.

**Our mapping diverges from the Legrooms+ extension v11.5.0** because
Google's bit positions have shifted since the extension was last
updated (v11.5.0 dates from before mid-2024). Empirically calibrated
2026-05 by driving Google Flights via Patchright, expanding flight
detail panels, scraping the rendered amenity labels, and correlating
against the bit array we receive from `/GetShoppingResults`.
See `research/probe_wifi_correlation.py` for the calibration tool.

The key finding: **`t[12] = []` (empty array) when the aircraft has none
of {wifi, IFE, in-seat power}**. Frontier is the canonical wifi-less
mainline carrier and consistently returns `[]`. Every wifi-equipped
carrier returns a populated 12-element array.

Within a populated array:

| Position | Our reading                        | Google's UI label                 |
| -------- | ---------------------------------- | --------------------------------- |
| `[0]`    | (deprecated, always None)          | —                                 |
| `[1]`    | In-seat plug power                 | "In-seat power & USB outlets"     |
| `[3]`    | In-seat plug (alt — older aircraft)| "In-seat power & USB outlets"     |
| `[5]`    | USB-only power (when no plug)      | "USB outlets"                     |
| `[8]`    | Seatback live-TV stream            | "Live TV"                         |
| `[9]`    | Seatback on-demand video           | "On-demand video"                 |
| `[10]`   | BYOD streaming (to your device)    | "Stream media to your device"     |
| `[11]`   | **Wifi enum: 1=none, 2=free, 3=paid** | (none) / "Free Wi-Fi" / "Wi-Fi for a fee" |

Position `[11]` is the ground-internet wifi enum — empirically validated
2026-05 against AA 952 ([11]=2 → "Free Wi-Fi"), B6 1072 ([11]=2 → "Free
Wi-Fi"), F9 1875 (`[12]=[]` → no label), and additional probes covering
DL/EK/UA/AS/AC/OS/WN/KL/QR/RJ. The Legrooms+ extension reads `[0]` for
wifi which is consistently None on 2026 responses — its wifi icon
doesn't fire on current data.

Mapped to a string per category, then to a glyph at render time:

| Category        | Decoder rule                          | Glyph              |
| --------------- | ------------------------------------- | ------------------ |
| wifi=free       | `[11] == 2`                           | `📶`               |
| wifi=paid       | `[11] == 3`                           | `📶$` (yellow)     |
| power=plug      | `[1]` or `[3]` truthy                 | `↯`                |
| power=usb       | `[5]` truthy (when no plug)           | `⌁`                |
| video=stream    | `[8]` truthy                          | `▶`  (live TV)     |
| video=ondemand  | `[9]` truthy (when no stream)         | `▷`  (seatback IFE)|
| video=byod      | `[10]` truthy (when no [8]/[9])       | `◰`  (your device) |

Cabin letter (Y/W/J/F for Economy/Premium/Business/First) prefixes the
pitch token. So a coach leg with free wifi + plug + on-demand seatback
reads `Y 31" 📶↯▷`; the same leg with paid wifi reads `Y 31" 📶$↯▷`
(with the `📶$` rendered in yellow). Wifi uses the 2-col emoji `📶`
because it's the highest-value binary signal and 1-col abstract chars
weren't at-a-glance readable. Power and video keep 1-col Unicode chars
to preserve the finer distinctions without bloating column width.

`flight search` prints a dim glyph-legend footer (`_LEGROOM_KEY` in
`src/flight_cli/cli.py`) below the gflight table whenever any legroom data
renders, so users don't have to memorize the mapping.

## Ground-truth validation set (2026-05)

Captured via Patchright by expanding flight detail panels in Google's web
UI and recording the visible labels. All decoded fields match exactly:

| Flight | Aircraft | Google's UI labels | t[12] |
| ------ | -------- | ------------------ | ----- |
| AA 952 MIA→LGA | 737MAX 8 | Average legroom (30 in), Free Wi-Fi, In-seat power & USB outlets, Stream media to your device | `[N,T,N,N,N,N,N,N,N,N,T,2]` |
| B6 1072 FLL→LGA | A320 | Above average legroom (32 in), Free Wi-Fi, In-seat power & USB outlets, Live TV | `[N,T,N,N,N,N,N,N,T,N,N,2]` |
| F9 1875 MCO→LAS | A320neo | (no amenity labels — 28 in pitch is below-average) | `[]` |
| EK Emirates A380 | (full IFE biz) | (paid wifi, seatback ondemand expected) | `[N,T,N,N,N,N,N,N,N,T,N,3]` |
| WN Southwest 737MAX | (BYOD only) | (free wifi for streaming, USB power) | `[N,N,N,N,N,T,N,N,N,N,T,2]` |

Positions `[2]`, `[4]`, `[6]`, `[7]` carry signals we don't decode. Most
remain None across all probed routes; speculatively duty-free / meal /
other carrier-info flags but no labeled reference to anchor the meaning.

## Code locations

- `src/flight_cli/_gflight_ids.py` — `_parse_leg_amenities`, `LegAmenities`
  dataclass, `_LEGROOM_CLASS` / `_CABIN` decoders, `_decode_*` helpers.
- `src/flight_cli/pp/gflight_adapter.py` — `_leg_info()` packs `LegAmenities`
  into `Slice.legs[i]: LegInfo` for PP matched-rendering.
- `src/flight_cli/cli.py:_fmt_gflight_legroom` — renders the inline column
  in the `Google Flights` table.

## Seatmap URL — separate concern

The legroom data is in-band on the search response. The **seatmap** is
not — it's a separate per-flight artifact at `seatmaps.com`, fetched via
one HTTP round-trip to `https://www.travelarrow.io/api/s`.

```
GET https://www.travelarrow.io/api/s?from=JFK&to=LHR&flightno=1&carrier=DL&date=8/15/2026&aircraft=Airbus+A330
→ {"seatmap_url": "airlines/dl-delta/airbus-a330-200/#<hash>"}

Open as: https://seatmaps.com/<seatmap_url>
```

**Date format is `M/D/YYYY`** with no zero-padding — that's what the
Legrooms+ extension sends (`load_flight_data.js: date=${n}/${i}/${e}` where
`[e,n,i] = raw[20]` is `[year, month, day]`). The travelarrow API also
accepts ISO, but we match the extension's wire shape to minimize
divergence risk.

**Flight number is the bare number** ("1", not "DL1") when the
IATA-prefix matches the carrier; we keep prefixes intact for codeshares
(operating carrier ≠ marketing prefix).

`src/flight_cli/seatmap.py`:
- `seatmap_api_url(...)` — deterministic URL builder, no HTTP.
- `fetch_seatmap_url(...)` — one GET, returns the seatmaps.com URL or
  `None` (no seatmap on file for this aircraft).

`flight seatmap <ORIG> <DEST> <FLIGHTNO> --date YYYY-MM-DD [--aircraft ...]`
exposes the resolution flow. `--no-fetch` skips the HTTP and just prints
the travelarrow URL.

## What we DON'T parse

Decoding more amenity bits (`[2]`, `[4]`, `[6-8]`, `[11]`) would require
either:
1. A new Legrooms+ extension version that maps them in `load_flight_data.js`
   (current 11.5.0 ignores them too).
2. Empirical probing against routes where we know the truth (e.g. comparing
   a known meal-included flight against the bit pattern).

Not worth the effort until a downstream feature actually needs them.

## What CHANGED about the bd work-5ewe plan

The original `work-5ewe` issue assumed we'd need to capture an external
API (`api.travelarrow.io/v3/deals`, `/metadata`) via Patchright + raw CDP
attach. That was based on partial information from a prior session that
saw outbound requests to `api.travelarrow.io` but couldn't capture
response bodies. **Those requests are for hotel pricing and analytics
telemetry, not legroom data.** The legroom-overlay path in the extension
is entirely client-side parsing of Google's response — no external API
involved beyond the seatmap link.

We dropped the capture work entirely. Estimated effort: 5h. Actual: ~3h
(mostly because the work was upstream/visible in the extension source
and we found the in-band path during research instead of after capture).
