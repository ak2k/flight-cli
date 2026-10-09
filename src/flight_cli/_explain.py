"""Plain-English reading of a Matrix routing-language string (`--routing`).

The grammar is `docs/memories/routing_language.md`. This is a decoder for people,
not a predicate parser: `routing_predicates.parse_routing` answers "what can
Google post-filter" and returns one `UnsupportedPred` for an ordered chain, which
is exactly what a person asking "what does this mean" has. A token this module
does not read is reported as unread, never guessed.
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
