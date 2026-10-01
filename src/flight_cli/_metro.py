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

# A leg over the airport bound is asked as several pages, each its own GET (and
# on a round trip its own pins). Eight keeps Example 6's 21 airports (4 pages)
# and a region-to-region list well inside it while a typo'd 40-airport list is
# still refused before it costs a minute of page loads.
MAX_GF_PAGES: Final = 8

# One page: the origins and the destinations it asks for.
type GfPage = tuple[tuple[str, ...], tuple[str, ...]]


def expand_airports(codes: Iterable[str]) -> tuple[str, ...]:
    """`codes` with each metro code replaced by its members, in order, repeats dropped."""
    return tuple(dict.fromkeys(a for c in codes for a in METRO_MEMBERS.get(c, (c,))))


def gf_leg_refusal(origins: Iterable[str], destinations: Iterable[str]) -> str | None:
    """Why ONE Google page can't take this leg's expanded airport sets, or None.

    The bound for a surface that is one page: the pinned link falls back to the
    itinerary's own airports on it, and the backend picker refuses a
    multi-cabin search and the calendar refuses `--fast` on it. A single-cabin
    search splits a leg over it into pages instead (`gf_pages_refusal`). An
    airport on both ends is refused because fli validates a leg by its FIRST
    airport on each side only, so `NYC JFK` raises inside the bridge while
    `JFK,EWR EWR,LHR` is sent with EWR on both ends."""
    o, d = expand_airports(origins), expand_airports(destinations)
    if len(o) + len(d) > MAX_GF_LEG_AIRPORTS:
        return f"{len(o) + len(d)} airports on one leg (its limit is {MAX_GF_LEG_AIRPORTS})"
    return _shared_refusal(o, d)


def _shared_refusal(o: tuple[str, ...], d: tuple[str, ...]) -> str | None:
    shared = [a for a in o if a in d]
    if shared:
        return f"an airport at both ends of a leg ({', '.join(shared)})"
    return None


def _groups(codes: tuple[str, ...], k: int) -> tuple[tuple[str, ...], ...]:
    """`codes` in `k` contiguous groups, in order, the first ones one longer
    when they do not divide evenly."""
    q, r = divmod(len(codes), k)
    out: list[tuple[str, ...]] = []
    at = 0
    for i in range(k):
        size = q + (1 if i < r else 0)
        out.append(codes[at : at + size])
        at += size
    return tuple(out)


def gf_leg_pages(origins: Iterable[str], destinations: Iterable[str]) -> tuple[GfPage, ...] | None:
    """The fewest Google pages that together ask for every (origin, destination)
    pair of this leg once, or None when that takes more than `MAX_GF_PAGES`.

    Origins are split into `a` contiguous groups and destinations into `b`, and
    page (i, j) asks group i against group j, so every pair is on exactly one
    page. `a * b` is the page count and is minimized with each page at most
    `MAX_GF_LEG_AIRPORTS` airports; a tie goes to fewer groups on the side with
    more airports, so the plan is one answer and not an enumeration order. A
    leg that fits is one page equal to the leg. An airport on both ends is not
    checked here (`gf_pages_refusal`)."""
    o, d = expand_airports(origins), expand_airports(destinations)
    if not o or not d:
        return ((o, d),)
    best: tuple[int, int, int, int] | None = None
    for a in range(1, min(len(o), MAX_GF_PAGES) + 1):
        for b in range(1, min(len(d), MAX_GF_PAGES // a) + 1):
            if -(-len(o) // a) + -(-len(d) // b) > MAX_GF_LEG_AIRPORTS:
                continue
            larger = a if len(o) >= len(d) else b
            key = (a * b, larger, a, b)
            if best is None or key < best:
                best = key
    if best is None:
        return None
    _, _, a, b = best
    return tuple((og, dg) for og in _groups(o, a) for dg in _groups(d, b))


def gf_pages_refusal(origins: Iterable[str], destinations: Iterable[str]) -> str | None:
    """Why Google can't answer this leg even as several pages, or None.

    The single-cabin search's bound; `gf_leg_refusal` stays the one-page bound
    for the link and for a multi-cabin search, whose cabins share one page's
    pins."""
    o, d = expand_airports(origins), expand_airports(destinations)
    if gf_leg_pages(o, d) is None:
        return (
            f"{len(o) + len(d)} airports on one leg (more than {MAX_GF_PAGES} pages "
            f"of at most {MAX_GF_LEG_AIRPORTS})"
        )
    return _shared_refusal(o, d)
