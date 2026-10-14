# pyright: reportPrivateUsage=false
"""On Windows the browser rung refuses before it launches.

`stop_driver` ends the patchright driver with SIGKILL, which Windows lacks, so
the first interrupt of a launched session would fail there. The refusal comes
first, as the typed error every caller already renders. The launcher seam is
replaced by a recorder and `sys.platform` is patched through `gfb.sys`, so
nothing here starts a browser or depends on the host."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

import pytest

from flight_cli import _gf_browser as gfb
from flight_cli._gf_errors import GfBrowserUnavailableError

if TYPE_CHECKING:
    import pathlib


def _record_launcher_reached(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> list[str]:
    """Replace the launcher seam with a recorder that refuses, and aim the cache
    dir (where the profile would be made) at `tmp_path`."""
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(tmp_path))
    reached: list[str] = []

    def _factory() -> NoReturn:
        reached.append("launcher")
        raise GfBrowserUnavailableError("the launcher was reached")

    monkeypatch.setattr(gfb, "_playwright_factory", _factory)
    return reached


def test_on_windows_the_session_refuses_before_it_launches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    reached = _record_launcher_reached(monkeypatch, tmp_path)
    monkeypatch.setattr(gfb.sys, "platform", "win32")
    session = gfb.GfBrowserSession(headed=False)

    with pytest.raises(GfBrowserUnavailableError) as refused:
        session.get_html("https://www.google.com/travel/flights")

    assert str(refused.value) == (
        "Google Flights' browser rung does not run on Windows. Use --gf-transport http."
    )
    assert refused.value.remedy == "Use --gf-transport http."
    assert reached == []
    assert session not in gfb._live
    assert not (tmp_path / gfb._PROFILE_DIR_NAME).exists()
    assert capfd.readouterr().err == ""


def test_off_windows_the_session_still_reaches_the_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    reached = _record_launcher_reached(monkeypatch, tmp_path)
    for platform in ("linux", "darwin"):
        monkeypatch.setattr(gfb.sys, "platform", platform)
        session: Any = gfb.GfBrowserSession(headed=False)

        with pytest.raises(GfBrowserUnavailableError, match="the launcher was reached"):
            session._ensure_page()

    assert reached == ["launcher", "launcher"]


def test_on_windows_an_auto_escalation_announces_no_chrome(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gfb.sys, "platform", "win32")

    gfb.announce_escalation()

    assert capsys.readouterr().err == ""
    assert gfb._notice_state["printed"] is False

    monkeypatch.setattr(gfb.sys, "platform", "linux")

    gfb.announce_escalation()

    assert "opening Chrome" in capsys.readouterr().err
