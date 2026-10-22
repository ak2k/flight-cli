"""The seats_aero client docstring names the command that shows the rate limit."""

from __future__ import annotations

from flight_cli.providers.seats_aero import client


def test_the_client_docstring_names_the_real_whoami_command() -> None:
    doc = client.__doc__ or ""
    assert "`flight auth seats-aero whoami`" in doc
    assert "flight auth seats whoami" not in doc
