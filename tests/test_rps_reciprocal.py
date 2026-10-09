"""A rate whose reciprocal overflows is refused where every other unusable rate is.

The limiter paces a sub-1 rate by a period of `1 / rps`; at or below about
5.56e-309 that period is `inf`, and the limiter divides by zero when the second
request has to wait, which a two-cabin search reported as one cabin's failure.
"""

from __future__ import annotations

import datetime as dt
import math

import httpx
import pytest
from typer.testing import CliRunner

from flight_cli import _config, _http, cli

_TOO_SMALL = ["5e-324", "1e-320", "5.5e-309"]


@pytest.mark.parametrize("value", _TOO_SMALL)
def test_a_rate_whose_reciprocal_overflows_is_refused_by_name(value: str) -> None:
    with pytest.raises(ValueError, match="too small") as excinfo:
        _config.checked_rps(float(value), "--rps")
    assert f"--rps={float(value)!r} is too small" in str(excinfo.value)
    assert "least 5.6e-309" in str(excinfo.value)


@pytest.mark.parametrize("value", [5.6e-309, 1e-3, 1.0, math.inf])
def test_every_other_rate_greater_than_0_passes(value: float) -> None:
    assert _config.checked_rps(value, "--rps") == value


@pytest.mark.parametrize("value", _TOO_SMALL)
def test_the_environment_rate_is_refused_as_the_flag_is(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(_config.RPS_ENV, value)
    with pytest.raises(ValueError, match="too small"):
        _config.http_rps()


@pytest.mark.parametrize("value", _TOO_SMALL)
def test_a_two_cabin_search_on_a_too_small_rate_is_refused_before_it_sends(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    def transport(**_kw: object) -> httpx.MockTransport:
        return httpx.MockTransport(handler)

    monkeypatch.setattr(_http, "AsyncCurlTransport", transport)
    monkeypatch.setenv("FLIGHT_API_KEY", "AIzaSy" + "E" * 33)
    depart = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    search = CliRunner().invoke(
        cli.app,
        [
            "search",
            "JFK",
            "LAX",
            "--dep",
            depart,
            "--backend",
            "matrix",
            "--cash-only",
            "--no-cache",
            "--format",
            "json",
            "--cabin",
            "economy,business",
            "--rps",
            value,
        ],
    )
    assert search.exit_code == 2, search.output
    line = " ".join(search.stderr.split())
    assert f"Bad rps configuration: --rps={float(value)!r} is too small" in line
    assert sent == []
