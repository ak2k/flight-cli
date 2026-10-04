"""A calendar search's Matrix link states each direction's own routing and
extension codes, as the specific-date link does. The wire gives a copied
return the outbound's codes and `--routing-ret ''` none, so the two links must
differ; `""` is the only value left to mean "no codes on the return"."""

from __future__ import annotations

import base64
import json
import urllib.parse
from datetime import date
from typing import Any, cast

from flight_cli.domain import (
    Cabin,
    CalendarSearch,
    CalendarWindow,
    Leg,
    Pax,
    SearchOptions,
)
from flight_cli.links import matrix_deep_link

_CODE_KEYS = ("routing", "ext", "routingRet", "extRet")


def _calendar_slice(out: Leg, ret: Leg | None) -> dict[str, Any]:
    legs = (out,) if ret is None else (out, ret)
    cal = CalendarSearch(
        legs=legs,
        window=CalendarWindow(
            start=date(2026, 10, 20), end=date(2026, 10, 27), duration_min=7, duration_max=7
        ),
        options=SearchOptions(cabin=Cabin.COACH, pax=Pax(adults=1)),
    )
    qs = urllib.parse.urlparse(matrix_deep_link(cal)).query
    b64 = urllib.parse.parse_qs(qs)["search"][0]
    payload = cast("dict[str, Any]", json.loads(base64.b64decode(b64)))
    return cast("dict[str, Any]", payload["slices"][0])


def _codes(slice_: dict[str, Any]) -> dict[str, str]:
    return {k: slice_[k] for k in _CODE_KEYS}


def _out(route_language: str | None = None, extension: str | None = None) -> Leg:
    return Leg(
        origins=("JFK",),
        destinations=("LHR",),
        route_language=route_language,
        extension=extension,
    )


def _ret(route_language: str | None = None, extension: str | None = None) -> Leg:
    return Leg(
        origins=("LHR",),
        destinations=("JFK",),
        route_language=route_language,
        extension=extension,
    )


def test_a_copied_return_routing_and_an_empty_routing_ret_give_different_links() -> None:
    copied = _codes(_calendar_slice(_out(route_language="AA+"), _ret(route_language="AA+")))
    unconstrained = _codes(_calendar_slice(_out(route_language="AA+"), _ret()))
    assert copied == {"routing": "AA+", "ext": "", "routingRet": "AA+", "extRet": ""}
    assert unconstrained == {"routing": "AA+", "ext": "", "routingRet": "", "extRet": ""}


def test_a_copied_extension_is_stated_for_the_return() -> None:
    slice_ = _calendar_slice(_out(extension="MAXSTOPS 0"), _ret(extension="MAXSTOPS 0"))
    assert slice_["ext"] == slice_["extRet"] == "MAXSTOPS 0"


def test_a_return_only_code_constrains_the_link() -> None:
    slice_ = _calendar_slice(_out(), _ret(route_language="LH UA", extension="MAXSTOPS 0"))
    assert _codes(slice_) == {
        "routing": "",
        "ext": "",
        "routingRet": "LH UA",
        "extRet": "MAXSTOPS 0",
    }


def test_a_one_way_leaves_the_return_codes_blank() -> None:
    slice_ = _calendar_slice(_out(route_language="AA+", extension="MAXSTOPS 0"), None)
    assert _codes(slice_) == {
        "routing": "AA+",
        "ext": "MAXSTOPS 0",
        "routingRet": "",
        "extRet": "",
    }


def test_no_codes_on_either_leg_omits_the_code_keys() -> None:
    slice_ = _calendar_slice(_out(), _ret())
    assert not set(_CODE_KEYS) & slice_.keys()


def test_a_copied_return_keeps_the_captured_key_order() -> None:
    slice_ = _calendar_slice(_out(route_language="AA+"), _ret(route_language="AA+"))
    assert list(slice_) == ["origin", "dest", "routing", "ext", "routingRet", "extRet", "dates"]
