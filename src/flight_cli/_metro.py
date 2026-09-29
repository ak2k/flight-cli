"""IATA metro codes and the airport sets Google Flights is asked for.

Matrix takes a metro code (`NYC`) as one token; Google's search page takes
airports only, repeated per leg. The two sites that build a Google request or
link (`fli_bridge`, the pinned link) expand metro codes through this table, and
the backend picker and the link caveats count them the same way. The domain
`Search`, the Matrix request and the Matrix deep link keep the user's tokens."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# The table in docs/memories/airport_groups.md ("IATA metro codes"), minus the six
# codes that are also an airport (HOU, LAX, BER, SHA, BKK, DPS): Google serves
# those as that one airport. tests/test_metro.py parses the memo and fails when
# the two differ. QSF and SAO are here although fli's airport table has them,
# because there they are Ain Arnat (Algeria) and Campo de Marte, not the metro.
METRO_MEMBERS: Final[Mapping[str, tuple[str, ...]]] = {
    "NYC": ("JFK", "LGA", "EWR"),
    "LON": ("LHR", "LGW", "STN", "LTN", "LCY", "SEN"),
    "PAR": ("CDG", "ORY", "BVA"),
    "TYO": ("NRT", "HND"),
    "MOW": ("SVO", "DME", "VKO"),
    "STO": ("ARN", "BMA", "NYO"),
    "MIL": ("MXP", "LIN", "BGY"),
    "ROM": ("FCO", "CIA"),
    "BUE": ("EZE", "AEP"),
    "SAO": ("GRU", "CGH", "VCP"),
    "RIO": ("GIG", "SDU"),
    "WAS": ("IAD", "DCA", "BWI"),
    "CHI": ("ORD", "MDW"),
    "QSF": ("SFO", "OAK", "SJC"),
    "OSA": ("KIX", "ITM", "UKB"),
    "SEL": ("ICN", "GMP"),
    "BJS": ("PEK", "PKX"),
    "JKT": ("CGK", "HLP"),
}

# Measured on Google's search page: 10 origins -> 1 served (86 rows), and a round
# trip at 11 per slice served; 15 -> 1 came back twice as Google's typed
# ErrorResponse in place of a board. The page's own airport pickers stop at 7.
# Per leg, not per URL, because both slices of a round trip carry their own sets.
MAX_GF_LEG_AIRPORTS: Final = 11


def expand_airports(codes: Iterable[str]) -> tuple[str, ...]:
    """`codes` with each metro code replaced by its members, in order, repeats dropped."""
    return tuple(dict.fromkeys(a for c in codes for a in METRO_MEMBERS.get(c, (c,))))


def gf_leg_refusal(origins: Iterable[str], destinations: Iterable[str]) -> str | None:
    """Why Google's page can't take this leg's expanded airport sets, or None.

    The one place that decides it: the backend picker refuses the search on it,
    and the pinned link falls back to the itinerary's own airports on it. An
    airport on both ends is refused because fli validates a leg by its FIRST
    airport on each side only, so `NYC JFK` raises inside the bridge while
    `JFK,EWR EWR,LHR` is sent with EWR on both ends."""
    o, d = expand_airports(origins), expand_airports(destinations)
    if len(o) + len(d) > MAX_GF_LEG_AIRPORTS:
        return f"{len(o) + len(d)} airports on one leg (its limit is {MAX_GF_LEG_AIRPORTS})"
    shared = [a for a in o if a in d]
    if shared:
        return f"an airport at both ends of a leg ({', '.join(shared)})"
    return None
