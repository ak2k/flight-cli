"""Parse Matrix routing-language (`--routing`) and extension codes
(`--extension`) into a flat predicate set, classified by how Google Flights can
honor each one:

  - Tier 1 (GF_NATIVE):    GF filters it server-side (airlines, alliances,
                           connecting airports, stops, max duration, max layover).
  - Tier 2 (GF_POSTFILTER): not a GF query knob, but evaluable on the base
                           response payload (operating carrier, -CODESHARE,
                           min layover, redeyes/overnights, specific flight #).
  - Tier 3 (MATRIX_ONLY):  fare-construction (fare basis, booking class),
                           anything GF can neither request nor reconstruct, and
                           any token this parser doesn't confidently recognize.

The Tier-3 bucket is the safety net: a query is served from GF alone only when
it carries NO Tier-3 predicate (see `ClassifiedConstraints.requires_matrix`).
We never honor part of a constraint on GF and silently drop the rest — an
unrecognized token escalates the whole query to Matrix.

`Tier` is the date grid's question ("could GF honor this at all"). The search
path asks a narrower one — `page_can_encode`, "can the public page's tfs=
parameter carry this" — because that transport has far fewer filter fields.

Routing language is *positional* (`BA AA` = BA then AA), so it's parsed
all-or-nothing per string: only single order-independent intents (one carrier
with a `+`/`*` quantifier, one connection-airport token, nonstop, one flight
number — placeholders ignored) are recognized; any ordered sequence, bare
single-segment carrier, country filter, or unknown token sends the whole
routing string to Matrix. Extension codes are order-independent and classified
per directive. Grammar: docs/memories/routing_language.md + extension_codes.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, assert_never

if TYPE_CHECKING:
    from collections.abc import Iterable


class Tier(IntEnum):
    """How Google Flights can honor a predicate (higher = harder)."""

    GF_NATIVE = 1
    GF_POSTFILTER = 2
    MATRIX_ONLY = 3


# ─────────────────────────── predicate types ───────────────────────────


@dataclass(frozen=True, slots=True)
class CarrierPred:
    """Marketing or operating carrier inclusion/exclusion. Marketing is a native
    GF filter (include directly, exclude via airline-complement); operating has
    no GF query knob so it's a post-filter on fl[22]."""

    codes: frozenset[str]
    exclude: bool
    operating: bool

    @property
    def tier(self) -> Tier:
        # Marketing *include* is a native GF filter. Exclude would need the
        # route's carrier set to complement, and operating has no GF query knob —
        # both are reliable post-filters on the result instead.
        return Tier.GF_NATIVE if not (self.operating or self.exclude) else Tier.GF_POSTFILTER


@dataclass(frozen=True, slots=True)
class AlliancePred:
    """One or more of oneworld/skyteam/star-alliance — native GF filter."""

    codes: frozenset[str]
    tier: Tier = field(default=Tier.GF_NATIVE, init=False)


@dataclass(frozen=True, slots=True)
class ConnectionAirportPred:
    """Connect only at / never at these airports — native GF filter
    (include list, or exclude via complement)."""

    codes: frozenset[str]
    exclude: bool

    @property
    def tier(self) -> Tier:
        # Include = native connecting-airports filter; exclude is a post-filter
        # (GF's connecting-airports control is an allow-list, not a deny-list).
        return Tier.GF_POSTFILTER if self.exclude else Tier.GF_NATIVE


@dataclass(frozen=True, slots=True)
class StopsPred:
    """Max connecting stops (0 = nonstop) — native GF filter."""

    max_stops: int
    tier: Tier = field(default=Tier.GF_NATIVE, init=False)


@dataclass(frozen=True, slots=True)
class MaxDurationPred:
    """Max itinerary duration in minutes — native GF filter."""

    minutes: int
    tier: Tier = field(default=Tier.GF_NATIVE, init=False)


@dataclass(frozen=True, slots=True)
class ConnectTimePred:
    """Layover-time bound in minutes. GF natively filters the *max* layover; a
    *min* layover has no query knob, so a min bound is a post-filter."""

    min_minutes: int | None
    max_minutes: int | None

    @property
    def tier(self) -> Tier:
        return Tier.GF_POSTFILTER if self.min_minutes is not None else Tier.GF_NATIVE


@dataclass(frozen=True, slots=True)
class ExcludeRedeyesPred:
    """No overnight (red-eye) flights — post-filter on segment times."""

    tier: Tier = field(default=Tier.GF_POSTFILTER, init=False)


@dataclass(frozen=True, slots=True)
class ExcludeOvernightsPred:
    """No overnight stops at hubs — post-filter on layover spans."""

    tier: Tier = field(default=Tier.GF_POSTFILTER, init=False)


@dataclass(frozen=True, slots=True)
class ExcludeCodesharePred:
    """No codeshare flights — post-filter (a marketing code != the operating
    carrier on any leg)."""

    tier: Tier = field(default=Tier.GF_POSTFILTER, init=False)


@dataclass(frozen=True, slots=True)
class SpecificFlightPred:
    """A specific flight number or range must appear — post-filter on flight #.
    A single number has low == high."""

    carrier: str
    low: int
    high: int
    tier: Tier = field(default=Tier.GF_POSTFILTER, init=False)


@dataclass(frozen=True, slots=True)
class UnsupportedPred:
    """A token we can neither request on GF nor reconstruct from its payload
    (fare basis, mileage, country filter, ordered routing, unknown). Forces
    Matrix. `reason` is for diagnostics / the GF-preview caveat."""

    token: str
    reason: str
    tier: Tier = field(default=Tier.MATRIX_ONLY, init=False)


Predicate = (
    CarrierPred
    | AlliancePred
    | ConnectionAirportPred
    | StopsPred
    | MaxDurationPred
    | ConnectTimePred
    | ExcludeRedeyesPred
    | ExcludeOvernightsPred
    | ExcludeCodesharePred
    | SpecificFlightPred
    | UnsupportedPred
)


@dataclass(frozen=True, slots=True)
class ClassifiedConstraints:
    """The full predicate set parsed from a slice's routing + extension."""

    predicates: tuple[Predicate, ...]

    @property
    def tier1(self) -> tuple[Predicate, ...]:
        return tuple(p for p in self.predicates if p.tier is Tier.GF_NATIVE)

    @property
    def tier2(self) -> tuple[Predicate, ...]:
        return tuple(p for p in self.predicates if p.tier is Tier.GF_POSTFILTER)

    @property
    def matrix_only(self) -> tuple[Predicate, ...]:
        return tuple(p for p in self.predicates if p.tier is Tier.MATRIX_ONLY)

    @property
    def requires_matrix(self) -> bool:
        """True iff any predicate can't be honored by GF (native or post-filter).
        When False, the query can be served from Google Flights alone."""
        return any(p.tier is Tier.MATRIX_ONLY for p in self.predicates)

    @property
    def matrix_reasons(self) -> tuple[str, ...]:
        return tuple(p.reason for p in self.predicates if isinstance(p, UnsupportedPred))


# ─────────────────────────── routing parser ────────────────────────────

_ALLIANCES = frozenset({"oneworld", "skyteam", "star-alliance"})

# Only the *unconstrained* flank placeholders (F* = 0+, F+ = 1+ segments). The
# count-bearing ones (F, F?, X, X+, X?) change the itinerary shape, so a routing
# using them isn't reduced to a flat GF filter — it escalates to Matrix.
_RE_PLACEHOLDER = re.compile(r"^F[+*]$", re.IGNORECASE)
_RE_NONSTOP = re.compile(r"^N(?::([A-Za-z]{2}))?$", re.IGNORECASE)
_RE_CARRIER = re.compile(r"^(~?)(O:|C:)?([A-Za-z]{2})([+*])$", re.IGNORECASE)
_RE_FLIGHTNUM = re.compile(r"^(~?)([A-Za-z]{2})(\d+)(?:-(\d+))?[+*?]?$", re.IGNORECASE)
_RE_AIRPORT = re.compile(r"^(~?)(?:X:)?([A-Za-z]{3}(?:,[A-Za-z]{3})*)$", re.IGNORECASE)


def _airport_codes(group: str) -> frozenset[str]:
    return frozenset(c.upper() for c in group.split(","))


def _carrier_pred(tok: str) -> CarrierPred | None:
    m = _RE_CARRIER.match(tok)
    if not m:
        return None
    return CarrierPred(
        frozenset({m.group(3).upper()}),
        exclude=m.group(1) == "~",
        operating=(m.group(2) or "").upper() == "O:",
    )


def _airport_pred(tok: str) -> ConnectionAirportPred | None:
    m = _RE_AIRPORT.match(tok)
    if not m:
        return None
    return ConnectionAirportPred(_airport_codes(m.group(2)), exclude=m.group(1) == "~")


def _parse_single_routing_token(tok: str) -> list[Predicate] | None:
    """Predicates for a one-token routing, or None if it's not a recognized
    single form (carrier with quantifier, nonstop, or a specific flight #)."""
    if carrier := _carrier_pred(tok):
        return [carrier]
    if m := _RE_NONSTOP.match(tok):
        preds: list[Predicate] = [StopsPred(max_stops=0)]
        if cc := m.group(1):
            preds.append(CarrierPred(frozenset({cc.upper()}), exclude=False, operating=False))
        return preds
    if (m := _RE_FLIGHTNUM.match(tok)) and m.group(1) != "~":
        low = int(m.group(3))
        high = int(m.group(4)) if m.group(4) else low
        return [SpecificFlightPred(carrier=m.group(2).upper(), low=low, high=high)]
    return None


def parse_routing(routing: str) -> list[Predicate]:
    """Parse one slice's routing-language string into flat predicates.

    Routing language is positional, so only unambiguous order-independent forms
    map to GF; anything else (ordered chains like `BA AA` / `DFW DEN`, bare
    single-segment carriers, country filters, count placeholders, unknowns)
    becomes a single Tier-3 UnsupportedPred — the whole routing goes to Matrix,
    never partially honored.

    Mapped forms (case-insensitive):
      - single token `LH+` / `~UA+` / `O:LH+`          -> carrier include/exclude
      - single token `N` / `N:UA`                      -> nonstop (+ carrier)
      - single token `UA882` / `UA1000-2000`           -> specific flight #
      - `F* X:LHR F*` / `F* ~DFW F*` / `F* DFW,DEN F*`  -> connect at / not at
    """
    tokens = routing.split()
    if not tokens:
        return []
    if len(tokens) == 1 and (single := _parse_single_routing_token(tokens[0])) is not None:
        return single
    # Multi-token: only the canonical flanked-airport idiom maps — F*/F+
    # placeholders around exactly one connection-airport token (`F* X:LHR F*`).
    # A flanked carrier would mean "at least one" (not "all"), so it's excluded.
    if len(tokens) > 1:
        core = [t for t in tokens if not _RE_PLACEHOLDER.match(t)]
        if len(core) == 1 and (airport := _airport_pred(core[0])):
            return [airport]
    return [UnsupportedPred(token=routing, reason=f"routing {routing!r} not GF-expressible")]


# ─────────────────────────── extension parser ──────────────────────────

_RE_HHMM = re.compile(r"^(\d{1,2}):([0-5]\d)$")


def _parse_hhmm(arg: str) -> int | None:
    if m := _RE_HHMM.match(arg.strip()):
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


def _carrier_codes(args: list[str]) -> frozenset[str]:
    return frozenset(a.upper() for a in args)


def _parse_extension_code(directive: str) -> Predicate | None:  # noqa: PLR0911, PLR0912 - flat keyword dispatch over the extension grammar
    """Parse one extension directive (already split on ';'). None for an empty
    directive."""
    parts = directive.split()
    if not parts:
        return None
    keyword = parts[0].upper()
    args = parts[1:]
    raw = directive.strip()

    match keyword:
        case "MAXSTOPS" if args and args[0].isdigit():
            return StopsPred(max_stops=int(args[0]))
        case "MAXDUR" if args and (mins := _parse_hhmm(args[0])) is not None:
            return MaxDurationPred(minutes=mins)
        case "MAXCONNECT" if args and (mins := _parse_hhmm(args[0])) is not None:
            return ConnectTimePred(min_minutes=None, max_minutes=mins)
        case "MINCONNECT" if args and (mins := _parse_hhmm(args[0])) is not None:
            return ConnectTimePred(min_minutes=mins, max_minutes=None)
        case "-OVERNIGHTS":
            return ExcludeOvernightsPred()
        case "-REDEYES":
            return ExcludeRedeyesPred()
        case "-CODESHARE":
            return ExcludeCodesharePred()
        case "ALLIANCE" if args:
            codes = frozenset(c.lower() for c in " ".join(args).split("|") if c.strip())
            if codes <= _ALLIANCES:
                return AlliancePred(codes=codes)
            return UnsupportedPred(token=raw, reason=f"unknown alliance in {raw!r}")
        case "AIRLINES" if args:
            return CarrierPred(_carrier_codes(args), exclude=False, operating=False)
        case "-AIRLINES" if args:
            return CarrierPred(_carrier_codes(args), exclude=True, operating=False)
        case "OPAIRLINES" if args:
            return CarrierPred(_carrier_codes(args), exclude=False, operating=True)
        case "-OPAIRLINES" if args:
            return CarrierPred(_carrier_codes(args), exclude=True, operating=True)
        case "-CITIES" if args:
            return ConnectionAirportPred(_carrier_codes(args), exclude=True)
        case _:
            return UnsupportedPred(token=raw, reason=f"extension {raw!r} not expressible on GF")


def parse_extension(extension: str) -> list[Predicate]:
    """Parse one slice's extension-codes string (semicolon-separated)."""
    out: list[Predicate] = []
    for directive in extension.split(";"):
        if pred := _parse_extension_code(directive):
            out.append(pred)
    return out


# ───────────────── search-page transport encodability ──────────────────
#
# `Tier` above answers "could Google Flights honor this at all" — the date
# grid's question of its RPC. The search path's transport, the public page's
# tfs= parameter, carries a much narrower filter set, so it gets its own
# question: can `links.build_search_tfs` encode this?
#
# Today the answer is yes only for stops. Carrier/alliance/layover-airport
# filters DO have tfs fields (3.6 / 3.7 / 3.15 / 3.17 / 3.18) and are the
# obvious next widening; until they are encoded and verified, a query carrying
# one goes to Matrix with the reason printed, because the alternative — post-
# filtering Google's fixed ~30-row board with no back-fill — silently answers
# a constrained search with "no results".


# fli's MaxStops enum stops at TWO_OR_FEWER_STOPS; anything above is ANY.
_MAX_ENCODABLE_STOPS = 2


def _page_reason(pred: Predicate) -> str | None:  # noqa: PLR0911, PLR0912 — one arm per predicate type
    """Why `pred` can't ride the search page's tfs= parameter, or None if it can."""
    match pred:
        # fli's MaxStops tops out at "two or fewer", so a higher ceiling maps to
        # ANY, which omits tfs field 3.5 entirely. Certifying it as encodable
        # would drop the constraint with neither a native filter nor a reason.
        case StopsPred() if pred.max_stops <= _MAX_ENCODABLE_STOPS:
            return None
        case StopsPred():
            return f"a stop ceiling above {_MAX_ENCODABLE_STOPS} ({pred.max_stops})"
        case UnsupportedPred():
            return pred.reason
        case CarrierPred():
            codes = ", ".join(sorted(pred.codes))
            article, kind = ("an", "operating carrier") if pred.operating else ("a", "carrier")
            verb = "exclusion" if pred.exclude else "filter"
            return f"{article} {kind} {verb} ({codes})"
        case AlliancePred():
            return f"an alliance filter ({', '.join(sorted(pred.codes))})"
        case ConnectionAirportPred():
            verb = "exclusion" if pred.exclude else "filter"
            return f"a connecting-airport {verb} ({', '.join(sorted(pred.codes))})"
        case MaxDurationPred():
            return f"a maximum trip duration ({pred.minutes} min)"
        case ConnectTimePred():
            return "a layover-time bound"
        case ExcludeRedeyesPred():
            return "a red-eye exclusion"
        case ExcludeOvernightsPred():
            return "an overnight-stop exclusion"
        case ExcludeCodesharePred():
            return "a codeshare exclusion"
        case SpecificFlightPred():
            return f"a specific flight number ({pred.carrier}{pred.low})"
        case _:
            assert_never(pred)


def page_can_encode(predicates: Iterable[Predicate]) -> tuple[bool, list[str]]:
    """Whether the search-page tfs= encoder can honor every predicate natively.

    Returns (encodable, reasons). `reasons` names each constraint that forces
    Matrix, in the user's own vocabulary, so the caller can print why the fast
    backend was declined instead of a generic apology."""
    reasons = [reason for p in predicates if (reason := _page_reason(p)) is not None]
    return not reasons, reasons


def classify(routing: str | None, extension: str | None) -> ClassifiedConstraints:
    """Parse and classify a slice's routing + extension into a predicate set."""
    preds: list[Predicate] = []
    if routing:
        preds.extend(parse_routing(routing))
    if extension:
        preds.extend(parse_extension(extension))
    return ClassifiedConstraints(predicates=tuple(preds))
