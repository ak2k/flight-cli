"""IATA airline code → human-readable name (PP's hint payload format).

PP's `/api/airline-search` hint object expects `airline` and `googleAirlines`
fields as the human-readable name (e.g. "Virgin Atlantic"), NOT the IATA
code ("VS"). With the IATA code, PP returns 0 flights — verified empirically
via the browser-extension capture (research/capture/pp_extension_capture.json).

We mirror the names PP uses in its own pricing-info catalog so the hint
metadata aligns with PP's internal records. Coverage is the airlines PP
supports — for anything else we fall back to the IATA code, accepting that
PP may not return a match (which is fine; the matcher falls back to
flight#+date / route+time).
"""

# Keys are fli's `Airline.name` (== IATA two-letter), values are PP's
# canonical airline name from its /api/pricing-info response.
_IATA_TO_PP_NAME: dict[str, str] = {
    "AA": "American",
    "AC": "Air Canada",
    "AF": "Air France",
    "AS": "Alaska",
    "B6": "JetBlue",
    "BA": "British Airways",
    "DL": "Delta",
    "EI": "Aer Lingus",
    "EK": "Emirates",
    "EY": "Etihad",
    "IB": "Iberia",
    "KL": "KLM",
    "LH": "Lufthansa",
    "NK": "Spirit",
    "QF": "Qantas",
    "QR": "Qatar",
    "SQ": "Singapore",
    "TP": "Tap Air Portugal",
    "UA": "United",
    "VA": "Virgin Australia",
    "VS": "Virgin Atlantic",
}


def pp_airline_name(iata: str) -> str:
    """Map a 2-letter IATA airline code to PP's human-readable name.

    Falls back to the input on unknown codes — PP will likely return no
    match for those, which the matcher tolerates (flight#+date and
    route+time stay available as fallback join keys)."""
    return _IATA_TO_PP_NAME.get(iata.upper(), iata)
