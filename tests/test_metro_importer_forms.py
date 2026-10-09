# pyright: reportPrivateUsage=false
"""The `_metro` docstring test finds an importer in every form Python allows.

`test_metro_docstring` promises that any new importer of `_metro` fails until
the docstring names it. These tests put one module of each import form in a
stand-in package and check the scanner reports every one."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import flight_cli
from test_metro_docstring import _expanding_modules

if TYPE_CHECKING:
    from pathlib import Path

_FORMS: dict[str, str] = {
    "from_name": "from ._metro import expand_airports\n",
    "from_absolute": "from flight_cli._metro import expand_airports\n",
    "from_package_relative": "from . import _metro\n",
    "from_package_absolute": "from flight_cli import _metro\n",
    "import_dotted": "import flight_cli._metro\n",
    "import_alias": "import flight_cli._metro as metro\n",
}


@pytest.fixture
def stand_in_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package = tmp_path / "flight_cli"
    package.mkdir()
    init = package / "__init__.py"
    init.write_text("")
    monkeypatch.setattr(flight_cli, "__file__", str(init))
    return package


def test_the_scanner_reports_an_importer_in_every_import_form(stand_in_package: Path) -> None:
    for stem, source in _FORMS.items():
        (stand_in_package / f"{stem}.py").write_text(source)
    (stand_in_package / "unrelated.py").write_text("from . import _config\nimport json\n")
    assert _expanding_modules() == set(_FORMS)
