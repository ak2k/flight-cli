"""IATA carrier code → full airline name, for the legend under the Google table.

Google names a carrier by a short brand ("Delta", "JetBlue") when it names it at
all. The legend wants one spelling per code, so a code listed here is named from
this map and any other from the operating name Google sent for it. Add a code
when a table shows one with no name."""

from __future__ import annotations

CARRIER_NAMES: dict[str, str] = {
    "9E": "Endeavor Air",
    "AA": "American Airlines",
    "AC": "Air Canada",
    "AF": "Air France",
    "AS": "Alaska Airlines",
    "B6": "JetBlue",
    "BA": "British Airways",
    "DL": "Delta Air Lines",
    "EI": "Aer Lingus",
    "EK": "Emirates",
    "F9": "Frontier Airlines",
    "G4": "Allegiant Air",
    "HA": "Hawaiian Airlines",
    "IB": "Iberia",
    "KL": "KLM",
    "LH": "Lufthansa",
    "MQ": "Envoy Air",
    "MX": "Breeze Airways",
    "NK": "Spirit Airlines",
    "OH": "PSA Airlines",
    "OO": "SkyWest Airlines",
    "QF": "Qantas",
    "SY": "Sun Country Airlines",
    "UA": "United Airlines",
    "VS": "Virgin Atlantic",
    "WN": "Southwest Airlines",
    "WS": "WestJet",
    "YX": "Republic Airways",
}
