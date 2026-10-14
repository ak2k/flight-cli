"""Plain-English reading of a Matrix routing-language string (`--routing`) and of
extension codes (`--extension`).

The grammars are `docs/memories/routing_language.md` and
`docs/memories/extension_codes.md`. This is a decoder for people,
not a predicate parser: `routing_predicates.parse_routing` answers "what can
Google post-filter" and returns one `UnsupportedPred` for an ordered chain, which
is exactly what a person asking "what does this mean" has. A token this module
does not read is reported as unread, never guessed. A routing token and an
extension code share `Line`.
"""

from __future__ import annotations

import re
from typing import Literal, NamedTuple

Noun = Literal["flight", "nonstop flight", "connection"]


class _Alt(NamedTuple):
    noun: Noun
    how: str  # the phrase the code completes: "operated by", "at", ...
    code: str  # empty for a placeholder that names nothing (F, N, X)


class Line(NamedTuple):
    token: str
    meaning: str | None  # None: the token is not in the grammar


_QUANTIFIERS = {"": "one", "+": "one or more", "*": "zero or more", "?": "zero or one"}

# A token is `~`, a comma group, and one quantifier, each optional.
_RE_TOKEN = re.compile(r"^(~?)([^~+*?]+)([+*?]?)$")
_RE_PREFIX = re.compile(r"^([COXL]):(.+)$")
# A two-character airline designator may hold digits (`3U`) but is never all
# digits; a flight number is a designator, digits and an optional range.
_DESIGNATOR = r"(?!\d\d)[A-Z0-9]{2}"
_RE_NONSTOP = re.compile(rf"^N:({_DESIGNATOR})$")
_RE_CARRIER = re.compile(rf"^{_DESIGNATOR}$")
_RE_AIRPORT = re.compile(r"^[A-Z]{3}$")
_RE_FLIGHT = re.compile(rf"^{_DESIGNATOR}([0-9]{{1,4}})(?:-([0-9]{{1,4}}))?$")
# `l:nUS` is the one country form the docs show and confirm; what the `n` stands
# for is not documented, so no other shape is read.
_RE_COUNTRY = re.compile(r"^N([A-Z]{2})$")


def _alt(code: str, lead: str) -> _Alt | None:
    """One comma alternative. `lead` is the first alternative's prefix, which
    each later alternative without a prefix of its own takes (`O:AA,UA`)."""
    m = _RE_PREFIX.match(code)
    prefix, value = (m[1], m[2]) if m else (lead, code)
    if prefix == "":
        if flight := _RE_FLIGHT.match(value):
            low, high = flight[1], flight[2]
            if high is not None and int(low) > int(high):
                return None
            return _Alt("flight", "numbered", value)
        prefix = "C" if _RE_CARRIER.match(value) else "X"
    if prefix in ("C", "O") and _RE_CARRIER.match(value):
        return _Alt("flight", "marketed by" if prefix == "C" else "operated by", value)
    if prefix == "X" and _RE_AIRPORT.match(value):
        return _Alt("connection", "at", value)
    if prefix == "L" and (country := _RE_COUNTRY.match(value)):
        return _Alt("connection", "in country", country[1])
    return None


def _alternatives(body: str) -> list[_Alt] | None:
    if body == "F":
        return [_Alt("flight", "", "")]
    if body == "X":
        return [_Alt("connection", "", "")]
    if body == "N":
        return [_Alt("nonstop flight", "", "")]
    if m := _RE_NONSTOP.match(body):
        return [_Alt("nonstop flight", "on", m[1])]
    parts = body.split(",")
    first = _RE_PREFIX.match(parts[0])
    alts = [_alt(p, first[1] if first else "") for p in parts]
    if None in alts or len({a.noun for a in alts if a}) != 1:
        return None
    return [a for a in alts if a]


def _or(codes: list[str]) -> str:
    return codes[0] if len(codes) == 1 else f"{', '.join(codes[:-1])} or {codes[-1]}"


def _meaning(token: str) -> str | None:
    if not token.isascii() or not (m := _RE_TOKEN.match(token.upper())):
        return None
    negated, body, quantifier = m.groups()
    alts = _alternatives(body)
    if alts is None:
        return None
    by_how: dict[str, list[str]] = {}
    for a in alts:
        if a.code:
            by_how.setdefault(a.how, []).append(a.code)
    if negated and (not by_how or _RE_NONSTOP.match(body)):
        return None
    noun = alts[0].noun
    plural = "s" if quantifier in ("+", "*") else ""
    head = f"{_QUANTIFIERS[quantifier]} {noun}{plural}"
    clause = " or ".join(f"{how} {_or(codes)}" for how, codes in by_how.items())
    return " ".join(p for p in (head, "not" if negated else "", clause) if p)


def decode_routing(routing: str) -> list[Line]:
    """One `Line` per space-separated token of `routing`, in order. One pair of
    brackets around the whole string, as Google's help page writes it, is
    dropped. A string with no token yields one unread line."""
    text = routing.strip()
    # A bracket inside means the outer ones belong to tokens, as in `[F] X [F]`.
    if text[:1] == "[" and text[-1:] == "]" and not {"[", "]"} & set(text[1:-1]):
        text = text[1:-1]
    return [Line(t, _meaning(t)) for t in text.split() or [routing]]


_COUNT = r"[0-9]{1,9}"
_TIME = r"[0-9]{1,2}:[0-5][0-9]"
# keyword -> (argument pattern, sentence with `{}` for the argument as typed)
_WITH_ARGUMENT = {
    "MAXSTOPS": (_COUNT, "at most {} {} in each direction"),
    "MAXDUR": (_TIME, "no itinerary longer than {} in each direction"),
    "MAXCONNECT": (_TIME, "no layover longer than {}"),
    "MINCONNECT": (_TIME, "no layover shorter than {}"),
    "MAXMILES": (_COUNT, "at most {} miles in each direction"),
    "MINMILES": (_COUNT, "at least {} miles in each direction"),
}
_WITHOUT_ARGUMENT = {
    "-REDEYES": "no red-eye flights",
    "-OVERNIGHTS": "no overnight stops at connections",
    "-CODESHARE": "no codeshare flights",
}


def _code_meaning(code: str) -> str | None:
    words = code.upper().split()
    if not code.isascii() or not words:
        return None
    keyword, *args = words
    if keyword in _WITHOUT_ARGUMENT:
        return None if args else _WITHOUT_ARGUMENT[keyword]
    if keyword not in _WITH_ARGUMENT:
        return None
    pattern, sentence = _WITH_ARGUMENT[keyword]
    if len(args) != 1 or not re.fullmatch(pattern, args[0]):
        return None
    if keyword == "MAXSTOPS":
        return sentence.format(args[0], "stop" if int(args[0]) == 1 else "stops")
    return sentence.format(args[0])


def _code_line(code: str) -> Line:
    meaning = _code_meaning(code)
    if meaning is None:
        return Line(code, None)
    # Each whitespace character shows as a space: `_safe_text` drops a carriage
    # return, joining the keyword to its argument, and a newline would split the
    # code's line in two.
    return Line("".join(" " if ch.isspace() else ch for ch in code), meaning)


def decode_extension(extension: str) -> list[Line]:
    """One `Line` per `;`-separated code of `extension`, in order, empty pieces
    skipped. A string with no code yields one unread line."""
    codes = [piece.strip() for piece in extension.split(";") if piece.strip()]
    return [_code_line(c) for c in codes or [extension]]
