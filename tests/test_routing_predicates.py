"""Tests for the routing-language / extension-code parser + tier classifier."""

from __future__ import annotations

import pytest

from flight_cli.routing_predicates import (
    AlliancePred,
    CarrierPred,
    ConnectionAirportPred,
    ConnectTimePred,
    ExcludeCodesharePred,
    ExcludeOvernightsPred,
    ExcludeRedeyesPred,
    MaxDurationPred,
    SpecificFlightPred,
    StopsPred,
    Tier,
    UnsupportedPred,
    classify,
    direction_dependence,
    mirrored_routing,
    parse_extension,
    parse_routing,
)

# ─────────────────────────── routing: carriers ─────────────────────────


def test_routing_carrier_include() -> None:
    (p,) = parse_routing("LH+")
    assert p == CarrierPred(frozenset({"LH"}), exclude=False, operating=False)
    assert p.tier is Tier.GF_NATIVE


def test_routing_carrier_exclude_is_postfilter() -> None:
    """Exclude has no GF allow-list knob (it'd need the route's carrier set to
    complement), so it's honored as a reliable post-filter."""
    (p,) = parse_routing("~UA+")
    assert p == CarrierPred(frozenset({"UA"}), exclude=True, operating=False)
    assert p.tier is Tier.GF_POSTFILTER


def test_routing_operating_carrier_is_postfilter() -> None:
    (p,) = parse_routing("O:LH+")
    assert p == CarrierPred(frozenset({"LH"}), exclude=False, operating=True)
    assert p.tier is Tier.GF_POSTFILTER


def test_routing_is_case_insensitive() -> None:
    assert parse_routing("lh+") == parse_routing("LH+")


def test_routing_bare_carrier_without_quantifier_escalates() -> None:
    """`LH` alone = exactly one direct LH segment — segment-count semantics GF
    can't honor, so it goes to Matrix (use `LH+` for all-LH)."""
    (p,) = parse_routing("LH")
    assert isinstance(p, UnsupportedPred)
    assert p.tier is Tier.MATRIX_ONLY


# ─────────────────────────── routing: stops / flights ──────────────────


def test_routing_nonstop() -> None:
    assert parse_routing("N") == [StopsPred(max_stops=0)]


def test_routing_nonstop_on_carrier() -> None:
    preds = parse_routing("N:UA")
    assert StopsPred(max_stops=0) in preds
    assert CarrierPred(frozenset({"UA"}), exclude=False, operating=False) in preds


def test_routing_specific_flight() -> None:
    (p,) = parse_routing("UA882")
    assert p == SpecificFlightPred(carrier="UA", low=882, high=882)
    assert p.tier is Tier.GF_POSTFILTER


def test_routing_flight_range() -> None:
    (p,) = parse_routing("UA1000-2000+")
    assert p == SpecificFlightPred(carrier="UA", low=1000, high=2000, quantifier="+")


@pytest.mark.parametrize(
    ("token", "several", "text"),
    [
        ("UA882", False, "UA882"),
        ("UA882?", False, "UA882?"),
        ("ua882+", True, "UA882+"),
        ("UA1000-2000*", True, "UA1000-2000*"),
    ],
)
def test_a_flight_numbers_quantifier_says_one_flight_or_several(
    token: str, several: bool, text: str
) -> None:
    (p,) = parse_routing(token)
    assert isinstance(p, SpecificFlightPred)
    assert (p.several, p.text) == (several, text)


def test_routing_flight_exclusion_escalates() -> None:
    (p,) = parse_routing("~UA882+")
    assert isinstance(p, UnsupportedPred)


# ─────────────────────────── routing: connection airports ──────────────


def test_routing_via_airport_idiom() -> None:
    (p,) = parse_routing("F* X:LHR F*")
    assert p == ConnectionAirportPred(frozenset({"LHR"}), exclude=False)
    assert p.tier is Tier.GF_NATIVE


def test_routing_via_airport_alternatives() -> None:
    (p,) = parse_routing("F* DFW,DEN F*")
    assert p == ConnectionAirportPred(frozenset({"DFW", "DEN"}), exclude=False)


def test_routing_avoid_airport() -> None:
    (p,) = parse_routing("F* ~DFW F*")
    assert p == ConnectionAirportPred(frozenset({"DFW"}), exclude=True)


def test_routing_bare_single_airport_escalates() -> None:
    """`X:DFW` alone = exactly one connection at DFW; GF can only do via-DFW
    (any stops), a superset — so escalate rather than over-return."""
    (p,) = parse_routing("X:DFW")
    assert isinstance(p, UnsupportedPred)


# ─────────────────────────── routing: escalation ───────────────────────


def test_routing_ordered_carrier_chain_escalates() -> None:
    (p,) = parse_routing("BA AA")
    assert isinstance(p, UnsupportedPred)
    assert p.tier is Tier.MATRIX_ONLY


def test_routing_ordered_airport_chain_escalates() -> None:
    (p,) = parse_routing("DFW DEN")
    assert isinstance(p, UnsupportedPred)


def test_routing_country_filter_escalates() -> None:
    (p,) = parse_routing("~l:nUS+")
    assert isinstance(p, UnsupportedPred)


def test_routing_flanked_carrier_escalates() -> None:
    """`F* LH+ F*` means at-least-one-LH (not all-LH); GF airlines=LH would
    under-return, so escalate."""
    (p,) = parse_routing("F* LH+ F*")
    assert isinstance(p, UnsupportedPred)


def test_routing_empty_is_no_predicates() -> None:
    assert parse_routing("") == []
    assert parse_routing("   ") == []


# ─────────────────────────── extension codes ───────────────────────────


def test_extension_maxstops() -> None:
    assert parse_extension("MAXSTOPS 1") == [StopsPred(max_stops=1)]


def test_extension_maxdur_hhmm() -> None:
    (p,) = parse_extension("MAXDUR 18:00")
    assert p == MaxDurationPred(minutes=1080)


def test_extension_maxconnect_is_native_minconnect_is_postfilter() -> None:
    (mx,) = parse_extension("MAXCONNECT 2:00")
    assert mx == ConnectTimePred(min_minutes=None, max_minutes=120)
    assert mx.tier is Tier.GF_NATIVE
    (mn,) = parse_extension("MINCONNECT 1:30")
    assert mn == ConnectTimePred(min_minutes=90, max_minutes=None)
    assert mn.tier is Tier.GF_POSTFILTER


def test_extension_alliance() -> None:
    (p,) = parse_extension("ALLIANCE star-alliance")
    assert p == AlliancePred(codes=frozenset({"star-alliance"}))
    assert p.tier is Tier.GF_NATIVE


def test_extension_alliance_multiple_and_unknown() -> None:
    (ok,) = parse_extension("ALLIANCE oneworld|skyteam")
    assert ok == AlliancePred(codes=frozenset({"oneworld", "skyteam"}))
    (bad,) = parse_extension("ALLIANCE galactic")
    assert isinstance(bad, UnsupportedPred)


@pytest.mark.parametrize("directive", ["ALLIANCE |", "ALLIANCE | |", "ALLIANCE ||"])
def test_an_alliance_directive_naming_none_escalates_as_a_bare_one_does(directive: str) -> None:
    """An empty alliance list would filter nothing wherever it was honored."""
    (p,) = parse_extension(directive)
    reason = f"extension {directive!r} not expressible on GF"
    assert p == UnsupportedPred(token=directive, reason=reason)
    assert classify(None, directive).requires_matrix


def test_extension_airlines_include_exclude_operating() -> None:
    assert parse_extension("AIRLINES BA AF") == [
        CarrierPred(frozenset({"BA", "AF"}), exclude=False, operating=False)
    ]
    assert parse_extension("-AIRLINES AA") == [
        CarrierPred(frozenset({"AA"}), exclude=True, operating=False)
    ]
    (op,) = parse_extension("OPAIRLINES UA")
    assert op == CarrierPred(frozenset({"UA"}), exclude=False, operating=True)
    assert op.tier is Tier.GF_POSTFILTER


@pytest.mark.parametrize(
    ("directive", "named"),
    [
        ("-AIRLINES UA,DL", "'UA,DL', which is not an airline code"),
        ("-AIRLINES UA, DL", "'UA,', which is not an airline code"),
        ("-AIRLINES |", "'|', which is not an airline code"),
        ("-OPAIRLINES UA,DL", "'UA,DL', which is not an airline code"),
        ("OPAIRLINES |", "'|', which is not an airline code"),
        ("AIRLINES ua,dl", "'ua,dl', which is not an airline code"),
        ("-AIRLINES UAL DL 12", "'UAL' and '12', which are not airline codes"),
    ],
)
def test_a_carrier_list_naming_a_token_that_is_not_an_airline_code_escalates(
    directive: str, named: str
) -> None:
    """Matrix refuses a comma list ("UA,DL" is not a carrier), and Google
    matches no row to such a token: `-AIRLINES UA,DL` excluded nothing, and
    `-AIRLINES UA, DL` DL alone."""
    (p,) = parse_extension(directive)
    assert p == UnsupportedPred(
        token=directive, reason=f"a carrier list naming {named} ({directive!r})"
    )
    assert p.tier is Tier.MATRIX_ONLY


@pytest.mark.parametrize(
    ("directive", "codes"),
    [
        ("-AIRLINES UA DL", {"UA", "DL"}),
        ("-AIRLINES B6 9K", {"B6", "9K"}),
        ("-airlines ua", {"UA"}),
    ],
)
def test_a_space_separated_carrier_list_stays_a_carrier_predicate(
    directive: str, codes: set[str]
) -> None:
    assert parse_extension(directive) == [
        CarrierPred(frozenset(codes), exclude=True, operating=False)
    ]


def test_extension_cities_exclude() -> None:
    (p,) = parse_extension("-CITIES DFW ORD")
    assert p == ConnectionAirportPred(frozenset({"DFW", "ORD"}), exclude=True)


def test_a_cities_list_is_not_held_to_the_airline_code_rule() -> None:
    """`-CITIES` names airports, and the list is parsed as it always was."""
    (p,) = parse_extension("-CITIES DUB,LHR")
    assert p == ConnectionAirportPred(frozenset({"DUB,LHR"}), exclude=True)


def test_extension_exclusion_flags() -> None:
    assert parse_extension("-REDEYES") == [ExcludeRedeyesPred()]
    assert parse_extension("-OVERNIGHTS") == [ExcludeOvernightsPred()]
    assert parse_extension("-CODESHARE") == [ExcludeCodesharePred()]


def test_extension_fare_basis_and_mileage_are_matrix_only() -> None:
    for code in ("F bc=y", "MAXMILES 8000", "PADCONNECT 0:30", "-NOFIRSTCLASS", "AIRCRAFT T:359"):
        (p,) = parse_extension(code)
        assert isinstance(p, UnsupportedPred), code
        assert p.tier is Tier.MATRIX_ONLY


def test_extension_multiple_semicolon_separated() -> None:
    preds = parse_extension("ALLIANCE star-alliance; -REDEYES; MAXSTOPS 1")
    assert len(preds) == 3
    assert AlliancePred(codes=frozenset({"star-alliance"})) in preds
    assert ExcludeRedeyesPred() in preds
    assert StopsPred(max_stops=1) in preds


def test_extension_malformed_args_escalate() -> None:
    (p,) = parse_extension("MAXSTOPS notanumber")
    assert isinstance(p, UnsupportedPred)


@pytest.mark.parametrize(
    ("directive", "keyword"),
    [
        ("MAXDUR 9:00 MAXCONNECT 1:00", "MAXDUR"),
        ("MAXSTOPS 1 MAXDUR 9:00", "MAXSTOPS"),
        ("MAXSTOPS 1 MAXCONNECT 1:00", "MAXSTOPS"),
        ("MAXSTOPS 1 9", "MAXSTOPS"),
        ("MAXCONNECT 1:00 MINCONNECT 3:00", "MAXCONNECT"),
        ("MAXCONNECT 1:00 2:00", "MAXCONNECT"),
        ("MINCONNECT 3:00 -CODESHARE", "MINCONNECT"),
        ("MAXDUR 9:00 -CODESHARE", "MAXDUR"),
        ("-CODESHARE MAXDUR 9:00", "-CODESHARE"),
        ("-CODESHARE MAXDUR", "-CODESHARE"),
        ("-REDEYES MAXDUR 9:00", "-REDEYES"),
        ("-OVERNIGHTS MAXSTOPS 1", "-OVERNIGHTS"),
    ],
)
def test_a_code_given_more_arguments_than_it_takes_escalates_quoting_it(
    directive: str, keyword: str
) -> None:
    """Two codes missing their `;` read as the first alone would ask Google a
    wider question than the one typed. Matrix answers `MAXDUR 9:00 MAXCONNECT
    1:00` with "MAXDUR expects exactly one argument"."""
    (p,) = parse_extension(directive)
    reason = f"{keyword} with more arguments than it takes ({directive!r})"
    assert p == UnsupportedPred(token=directive, reason=reason)
    assert p.tier is Tier.MATRIX_ONLY
    assert classify(None, directive).requires_matrix


def test_a_run_on_in_lower_case_names_the_code_and_quotes_it_as_typed() -> None:
    typed = "maxdur 9:00 maxconnect 1:00"
    assert parse_extension(typed) == [
        UnsupportedPred(
            token=typed,
            reason="MAXDUR with more arguments than it takes ('maxdur 9:00 maxconnect 1:00')",
        )
    ]


@pytest.mark.parametrize(
    ("extension", "predicates"),
    [
        (
            "MAXDUR 9:00; MAXCONNECT 1:00",
            [MaxDurationPred(minutes=540), ConnectTimePred(min_minutes=None, max_minutes=60)],
        ),
        ("MAXSTOPS 1; MAXDUR 9:00", [StopsPred(max_stops=1), MaxDurationPred(minutes=540)]),
        (
            "MINCONNECT 3:00; -CODESHARE",
            [ConnectTimePred(min_minutes=180, max_minutes=None), ExcludeCodesharePred()],
        ),
        (
            "ALLIANCE star-alliance; -REDEYES; MAXSTOPS 1",
            [
                AlliancePred(codes=frozenset({"star-alliance"})),
                ExcludeRedeyesPred(),
                StopsPred(max_stops=1),
            ],
        ),
        ("AIRLINES AA DL", [CarrierPred(frozenset({"AA", "DL"}), exclude=False, operating=False)]),
        ("ALLIANCE skyteam|oneworld", [AlliancePred(codes=frozenset({"skyteam", "oneworld"}))]),
        ("-CITIES DFW ORD", [ConnectionAirportPred(frozenset({"DFW", "ORD"}), exclude=True)]),
    ],
)
def test_codes_with_their_separators_parse_whole(extension: str, predicates: list[object]) -> None:
    assert parse_extension(extension) == predicates


@pytest.mark.parametrize(
    ("directive", "reason"),
    [
        ("MAXDUR", "extension 'MAXDUR' not expressible on GF"),
        ("MAXSTOPS x", "extension 'MAXSTOPS x' not expressible on GF"),
        ("ALLIANCE skyteam MAXDUR 9:00", "unknown alliance in 'ALLIANCE skyteam MAXDUR 9:00'"),
        (
            "AIRLINES AA MAXDUR 9:00",
            "a carrier list naming 'MAXDUR' and '9:00', which are not airline codes"
            " ('AIRLINES AA MAXDUR 9:00')",
        ),
    ],
)
def test_a_missing_or_bad_argument_and_a_variable_length_code_keep_their_reasons(
    directive: str, reason: str
) -> None:
    assert parse_extension(directive) == [UnsupportedPred(token=directive, reason=reason)]


# ─────────────────────────── classify + gate ───────────────────────────


def test_classify_all_gf_expressible_does_not_require_matrix() -> None:
    c = classify("LH+", "MAXSTOPS 1; -REDEYES; MAXCONNECT 2:00")
    assert not c.requires_matrix
    assert {p.tier for p in c.predicates} == {Tier.GF_NATIVE, Tier.GF_POSTFILTER}


def test_classify_fare_basis_requires_matrix() -> None:
    c = classify("LH+", "F bc=y")
    assert c.requires_matrix
    assert c.matrix_reasons  # carries a human-readable reason for the caveat


def test_classify_partitions_tiers() -> None:
    c = classify("O:LH+", "AIRLINES BA AF; MINCONNECT 1:00")
    assert any(isinstance(p, CarrierPred) and p.operating for p in c.tier2)
    assert any(isinstance(p, CarrierPred) and not p.operating for p in c.tier1)
    assert not c.requires_matrix


def test_classify_empty_is_empty() -> None:
    c = classify(None, None)
    assert c.predicates == ()
    assert not c.requires_matrix


# ─────────────────────────── direction ─────────────────────────────────


@pytest.mark.parametrize(
    "routing",
    [
        "BA AA",
        "UA LH",
        "AA+ DL+",
        "AA25 UA814",
        "F+ AA",
        "DFW DEN X?",
        "F+ X:LHR F*",
        "DL747",
        "ua882+",
        "B6323",
        "f91234",
    ],
)
def test_an_ordered_chain_or_a_flight_number_depends_on_direction(routing: str) -> None:
    assert direction_dependence(routing) is not None


@pytest.mark.parametrize(
    "routing",
    [
        "~B6323",
        "AA+",
        "~BA+",
        "O:LH+",
        "N",
        "N:UA",
        "AA,UA",
        "F* X:LHR F*",
        "f* x:lhr F*",
        "F+ AA F+",
        "X? X?",
        "DL CHI DL",
        "DFW,DEN DEN,DFW",
        "~l:nUS+",
        "~UA882",
        "UA1000-2000+",
    ],
)
def test_an_expression_that_reads_the_same_both_ways_is_independent(routing: str) -> None:
    assert direction_dependence(routing) is None


@pytest.mark.parametrize(
    ("routing", "dependent"),
    [
        ("[F* X F*]", False),
        ("[BA AA]", True),
        ("~DFW,DEN F ~DEN,DFW", False),
        ("X:DFW,DEN F X:DEN,DFW", False),
        ("~DFW,DEN F DEN,DFW", True),
    ],
)
def test_one_enclosing_bracket_and_a_group_prefix_come_off_before_comparing(
    routing: str, dependent: bool
) -> None:
    assert (direction_dependence(routing) is not None) is dependent


@pytest.mark.parametrize(
    ("routing", "dependent"),
    [
        ("~AA,UA+ ~UA,AA+", False),
        ("AA,UA+ X UA,AA+", False),
        ("~AA,UA+ ~UA,AA*", True),
        ("AA,UA+ X UA,AA", True),
    ],
)
def test_a_quantifier_belongs_to_the_whole_comma_group(routing: str, dependent: bool) -> None:
    assert (direction_dependence(routing) is not None) is dependent


@pytest.mark.parametrize(
    ("routing", "dependent"),
    [
        ("AA882-882", True),
        ("aa882-0882+", True),
        ("F+ AA882-882 F+", True),
        ("~AA882-882", False),
        ("AA1-3000", False),
        ("AA25-30", False),
    ],
)
def test_a_range_names_one_flight_only_when_its_ends_are_equal(
    routing: str, dependent: bool
) -> None:
    assert (direction_dependence(routing) is not None) is dependent


@pytest.mark.parametrize(
    ("routing", "dependent"),
    [
        ("F:AA1-3000,F:UA882", True),
        ("AA1-3000,F:UA882", True),
        ("AA1-3000,C:UA882", True),
        ("F+ AA1-3000,F:UA882 F+", True),
        ("~F:AA1-3000,F:UA882", False),
        ("F:AA1-3000,F:UA1000-2000", False),
    ],
)
def test_a_flight_number_is_found_behind_its_own_prefix(routing: str, dependent: bool) -> None:
    assert (direction_dependence(routing) is not None) is dependent


def test_the_reason_names_the_flight_or_the_order() -> None:
    assert direction_dependence("F+ AA25 F+") == "names flight 'AA25', which flies one way"
    assert direction_dependence("UA LH") == (
        "is an ordered chain, which the return would fly in the outbound's order"
    )


@pytest.mark.parametrize(
    ("routing", "mirror"),
    [
        ("UA LH", "LH UA"),
        ("DFW DEN X?", "X? DEN DFW"),
        ("F+ X:LHR F*", "F* X:LHR F+"),
        ("AA25 UA814", None),
        ("DL747", None),
        ("AA882-882 F+", None),
        ("AA1-3000,F:UA882 F+", None),
        ("[BA AA]", None),
    ],
)
def test_the_mirror_reverses_a_chain_of_no_flight_and_no_bracket(
    routing: str, mirror: str | None
) -> None:
    assert mirrored_routing(routing) == mirror
