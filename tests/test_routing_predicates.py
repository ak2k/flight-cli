"""Tests for the routing-language / extension-code parser + tier classifier."""

from __future__ import annotations

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
    assert p == SpecificFlightPred(carrier="UA", low=1000, high=2000)


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


def test_extension_cities_exclude() -> None:
    (p,) = parse_extension("-CITIES DFW ORD")
    assert p == ConnectionAirportPred(frozenset({"DFW", "ORD"}), exclude=True)


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
