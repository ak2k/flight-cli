# pyright: reportPrivateUsage=false
"""`flight doctor`: every check's pass, fail and skip, without the network.

Matrix answers through `httpx.MockTransport`, the Google page through the
suite's fake curl_cffi session and real parser, Chrome through a fake session
handing back page bytes, and each provider through its client with the
transport substituted. The conftest guard against a real browser launch stays
on for every test here.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import click
import httpx
import pytest
import stamina
import typer
from typer.testing import CliRunner

from conftest import _ds1, _page
from flight_cli import _api_key, _doctor, _gf_browser, cli
from flight_cli import _gflight_ids as gfid
from flight_cli._gf_common import PageFetch
from flight_cli._gf_errors import GfBrowserUnavailableError
from flight_cli.client import MatrixClient
from flight_cli.pp import auth as pp_auth
from flight_cli.pp import client as pp_client
from flight_cli.pp.client import PPApiError
from flight_cli.pp.models import PricingInfoResponse
from flight_cli.providers.seats_aero import auth as seats_auth
from flight_cli.providers.seats_aero import client as seats_client

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result

    from flight_cli.providers.seats_aero.client import SeatsAeroClient

_FIXTURES = pathlib.Path(__file__).parent / "fixtures"
_MATRIX_OK: dict[str, Any] = json.loads(
    (_FIXTURES / "matrix_currency" / "specific_jfk_lhr_rt_gbp_resp.json").read_text()
)
_KEY_IN_USE = "AIzaSy" + "U" * 33
_SPA_KEY = "AIzaSy" + "S" * 33
_HOME = '<html><script src="//www.gstatic.com/alkali/app.js"></script></html>'
_BUNDLE = f'var a={{}};a.matrix="{_SPA_KEY}";a["matrix-nightly"]="AIzaSy{"N" * 33}";'
_GF_URL = "https://www.google.com/travel/flights?tfs=abc"
_PP_ACCESS = "pp-access-token-" + "a" * 24
_PP_REFRESH = "pp-refresh-token-" + "r" * 24
_PP_EMAIL = "someone@example.com"
_SEATS_KEY = "pro_" + "k" * 28
_SECRET_ENV = ("FLIGHT_API_KEY", "PP_ACCESS_TOKEN", "PP_REFRESH_TOKEN", "SEATS_AERO_API_KEY")


def _priced_page() -> str:
    return _page(_ds1("ds1_jfk_lax_3rows.json"))


def _seats_ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": [], "count": 0, "hasMore": False},
        headers={
            "x-ratelimit-limit": "1000",
            "x-ratelimit-remaining": "994",
            "x-ratelimit-reset": "3600",
        },
    )


class World:
    """One machine's worth of answers, each of them passing until a test
    changes it. The seams read these at call time, so a test changes a field
    and runs."""

    def __init__(self, tmp: pathlib.Path) -> None:
        self.tmp = tmp
        self.spa: Callable[[httpx.Request], httpx.Response] = self.spa_ok
        self.spa_gets: list[str] = []
        self.matrix: Callable[[httpx.Request], httpx.Response] = lambda _r: httpx.Response(
            200, json=_MATRIX_OK
        )
        self.matrix_requests: list[httpx.Request] = []
        self.browser: PageFetch | Exception = PageFetch(_priced_page(), _GF_URL, 200)
        self.browser_urls: list[str] = []
        self.pp: Exception | None = None
        self.pp_calls: list[bool] = []
        self.seats: Callable[[httpx.Request], httpx.Response] = _seats_ok
        self.seats_requests: list[httpx.Request] = []

    @staticmethod
    def spa_ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_HOME if request.url.path == "/search" else _BUNDLE)

    def spa_handler(self, request: httpx.Request) -> httpx.Response:
        self.spa_gets.append(str(request.url))
        return self.spa(request)

    def matrix_handler(self, request: httpx.Request) -> httpx.Response:
        self.matrix_requests.append(request)
        return self.matrix(request)

    def seats_handler(self, request: httpx.Request) -> httpx.Response:
        self.seats_requests.append(request)
        return self.seats(request)


class _FakeBrowser:
    def __init__(self, world: World) -> None:
        self._world = world

    def get_html(self, url: str) -> PageFetch:
        self._world.browser_urls.append(url)
        if isinstance(self._world.browser, Exception):
            raise self._world.browser
        return self._world.browser


@pytest.fixture
def world(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    gf_session: Callable[..., object],
) -> World:
    """Every check configured and passing, and nothing read from or written to
    the developer's own stores."""
    for var in (*_SECRET_ENV, "FLIGHT_CLI_GF_BROWSER_BIN", "PP_SUPABASE_ANON_KEY"):
        monkeypatch.delenv(var, raising=False)
    gf_session(_priced_page())  # also points MATRIX_CACHE_DIR at tmp_path
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("FLIGHT_CLI_CONFIG_DIR", str(config))
    monkeypatch.setattr(_api_key, "_CACHE_PATH", tmp_path / ".matrix-key")
    monkeypatch.setattr(pp_auth, "CONFIG_DIR", config)
    monkeypatch.setattr(pp_auth, "TOKENS_PATH", config / "pp.json")
    monkeypatch.setattr(seats_auth, "CONFIG_DIR", config)
    monkeypatch.setattr(seats_auth, "KEY_PATH", config / "seats.json")

    w = World(tmp_path)
    _api_key._CACHE_PATH.write_text(_KEY_IN_USE + "\n")
    pp_auth.save_tokens(
        pp_auth.Tokens(
            access_token=_PP_ACCESS,
            refresh_token=_PP_REFRESH,
            expires_at=int(time.time()) + 30 * 86400,
            user_email=_PP_EMAIL,
        )
    )
    seats_auth.save_key(_SEATS_KEY)
    chrome = tmp_path / "chrome"
    chrome.write_text("#!/bin/sh\n")
    chrome.chmod(0o755)
    monkeypatch.setenv("FLIGHT_CLI_GF_BROWSER_BIN", str(chrome))
    monkeypatch.setattr(_doctor, "_patchright_installed", lambda: True)

    def spa_client() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(w.spa_handler), follow_redirects=True)

    def matrix_client(**kw: Any) -> MatrixClient:
        c = MatrixClient(**kw, rps=1000.0)
        c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(w.matrix_handler))
        return c

    def browser_session(*, headed: bool) -> _FakeBrowser:
        del headed
        return _FakeBrowser(w)

    class FakePP:
        def __init__(self, tokens: pp_auth.Tokens) -> None:
            self._tokens = tokens

        async def pricing_info(self, *, force_refresh: bool = False) -> PricingInfoResponse:
            w.pp_calls.append(force_refresh)
            if w.pp is not None:
                raise w.pp
            return PricingInfoResponse.model_validate(
                {"pricingInfos": [{"airline": "United"}, {"airline": "Delta"}]}
            )

        async def aclose(self) -> None:
            return None

    def seats_aero(**kw: Any) -> SeatsAeroClient:
        c = seats_client.SeatsAeroClient(**kw)
        c._client = httpx.AsyncClient(
            transport=httpx.MockTransport(w.seats_handler), base_url=seats_client.API_BASE
        )
        return c

    monkeypatch.setattr(_doctor, "_spa_client", spa_client)
    monkeypatch.setattr(_doctor, "MatrixClient", matrix_client)
    monkeypatch.setattr(_gf_browser, "session", browser_session)
    monkeypatch.setattr(_doctor, "PPClient", FakePP)
    monkeypatch.setattr(_doctor, "SeatsAeroClient", seats_aero)
    return w


def _run(lines: list[str] | None = None) -> _doctor.Report:
    return _doctor.run(on_start=(lines if lines is not None else []).append)


def _by_id(report: _doctor.Report) -> dict[str, _doctor.Check]:
    return {c.id: c for c in report.checks}


def _fails_as(report: _doctor.Report, cid: str, cause: str) -> _doctor.Check:
    c = _by_id(report)[cid]
    assert (c.status, c.cause) == ("fail", cause), c
    assert c.retryable == (cause in {"throttled", "unreachable", "upstream", "brownout"})
    return c


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(cli.app, ["doctor", *args])


def _table_rows(out: str) -> dict[str, str]:
    """The table's status cell by check id. `check` and `status` never wrap, so
    each row's first line carries both."""
    rows: dict[str, str] = {}
    for line in out.splitlines():
        if not line.startswith("│"):
            continue
        cells = [c.strip() for c in line.strip("│").split("│")]
        if cells[0]:
            rows[cells[0]] = cells[1]
    return rows


# ───────────────────────────── the report ─────────────────────────────


def test_every_check_passes_in_order_when_every_backend_answers(world: World) -> None:
    lines: list[str] = []
    report = _run(lines)
    assert [c.id for c in report.checks] == list(_doctor.CHECK_IDS)
    assert [c.status for c in report.checks] == ["pass"] * 10, report.checks
    assert report.exit_code == 0
    assert report.ok
    # One progress line per live probe, as it starts.
    assert [ln.split(":")[0] for ln in lines] == list(_doctor.CHECK_IDS[4:])
    local = {c.id: c.seconds for c in report.checks[:4]}
    assert local == dict.fromkeys(_doctor.CHECK_IDS[:4])
    assert all(c.seconds is not None for c in report.checks[4:])


def test_the_document_carries_ok_the_run_date_and_every_check(world: World) -> None:
    doc = _run().document()
    assert set(doc) == {"ok", "date", "checks"}
    assert doc["ok"] is True
    assert doc["date"] == dt.date.today().isoformat()
    assert [c["id"] for c in doc["checks"]] == list(_doctor.CHECK_IDS)
    assert set(doc["checks"][0]) == {"id", "status", "detail", "cause", "retryable", "seconds"}


def _report_failing(*causes: str) -> _doctor.Report:
    checks = [
        _doctor.Check(
            id=cid,
            status="fail" if i < len(causes) else "pass",
            detail="x",
            cause=causes[i] if i < len(causes) else None,  # pyright: ignore[reportArgumentType]
            retryable=i < len(causes) and causes[i] in _doctor.RETRYABLE,
        )
        for i, cid in enumerate(_doctor.CHECK_IDS)
    ]
    return _doctor.Report(date=dt.date(2026, 9, 30), checks=tuple(checks))


@pytest.mark.parametrize(
    ("causes", "code"),
    [
        ((), 0),
        (("throttled",), 75),
        (("unreachable", "upstream", "brownout"), 75),
        (("shape",), 1),
        (("brownout", "shape"), 1),
        (("throttled", "auth"), 1),
        (("error",), 1),
    ],
)
def test_exit_code_is_75_only_when_every_failure_is_retryable(
    causes: tuple[str, ...], code: int
) -> None:
    assert _report_failing(*causes).exit_code == code


def test_a_check_cannot_carry_a_cause_its_status_or_retryable_contradicts() -> None:
    with pytest.raises(ValueError, match="cause"):
        _doctor.Check(id="config", status="pass", detail="x", cause="config")
    with pytest.raises(ValueError, match="retryable"):
        _doctor.Check(id="config", status="fail", detail="x", cause="shape", retryable=True)
    with pytest.raises(ValueError, match="one non-empty line"):
        _doctor.Check(id="config", status="pass", detail="two\nlines")


def test_a_report_must_carry_every_check_in_order() -> None:
    one = _doctor.Check(id="config", status="pass", detail="x")
    with pytest.raises(ValueError, match="expected"):
        _doctor.Report(date=dt.date(2026, 9, 30), checks=(one,))


def test_a_check_that_raises_something_unclassified_is_an_error_naming_its_type(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_kw: object) -> PageFetch:
        raise RuntimeError("the parser tripped\nsecond line of detail")

    monkeypatch.setattr(gfid, "_fetch_page", boom)
    c = _fails_as(_run(), "google-http", "error")
    assert c.detail == "RuntimeError: the parser tripped"


# ───────────────────────────── local checks ─────────────────────────────


def test_config_absent_passes_and_a_parse_error_fails_naming_the_path(world: World) -> None:
    assert "no config file" in _by_id(_run())["config"].detail
    path = pathlib.Path(os.environ["FLIGHT_CLI_CONFIG_DIR"]) / "config.toml"
    path.write_text("[http]\nrps = 2\n")
    assert _by_id(_run())["config"].status == "pass"
    path.write_text("[http\nrps = \n")
    c = _fails_as(_run(), "config", "config")
    assert str(path) in c.detail


def test_matrix_key_from_the_environment_passes_with_its_fingerprint(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "AIzaSy" + "E" * 33
    monkeypatch.setenv("FLIGHT_API_KEY", f" {key} ")
    c = _by_id(_run())["matrix-key"]
    assert c.status == "pass"
    assert c.detail == f"FLIGHT_API_KEY, {_doctor.fingerprint(key)}"
    assert f"key={key}" in str(world.matrix_requests[0].url)


def test_a_malformed_matrix_key_in_the_environment_fails_as_config(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLIGHT_API_KEY", "not-a-google-api-key")
    c = _fails_as(_run(), "matrix-key", "config")
    assert "FLIGHT_API_KEY" in c.detail
    assert "not-a-google-api-key" not in c.detail


def test_matrix_key_reports_the_cache_and_its_age(world: World) -> None:
    detail = _by_id(_run())["matrix-key"].detail
    assert detail.startswith(f"cached at {_api_key._CACHE_PATH}, 0.0 of 30 days old, sha256:")


def test_a_stale_or_missing_cached_key_passes_saying_the_next_search_reads_one(
    world: World,
) -> None:
    month_ago = time.time() - 31 * 86400
    os.utime(_api_key._CACHE_PATH, (month_ago, month_ago))
    report = _run()
    assert "past its 30 days" in _by_id(report)["matrix-key"].detail
    # A stale key is not in use, so the search runs on the key Matrix's page serves.
    assert f"key={_SPA_KEY}" in str(world.matrix_requests[-1].url)
    _api_key._CACHE_PATH.unlink()
    report = _run()
    assert _by_id(report)["matrix-key"].detail.startswith("none cached")
    assert "no key is in use" in _by_id(report)["matrix-spa-key"].detail
    assert report.exit_code == 0


def test_an_unreadable_cached_key_fails_as_config(world: World) -> None:
    _api_key._CACHE_PATH.unlink()
    _api_key._CACHE_PATH.mkdir()
    c = _fails_as(_run(), "matrix-key", "config")
    assert str(_api_key._CACHE_PATH) in c.detail


def test_the_response_cache_opens_and_a_broken_one_fails_naming_its_directory(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _by_id(_run())["cache"].detail.endswith("opens, 0 entries")
    blocked = world.tmp / "not-a-dir"
    blocked.write_text("")
    monkeypatch.setenv("MATRIX_CACHE_DIR", str(blocked))
    c = _fails_as(_run(), "cache", "config")
    assert str(blocked) in c.detail


def test_google_cookies_absent_present_stale_and_unreadable(world: World) -> None:
    assert _by_id(_run())["google-cookies"].detail.startswith("none saved at")
    jar = gfid._cookie_path()
    jar.parent.mkdir(parents=True, exist_ok=True)
    cookie = {"name": "NID", "value": "v", "domain": ".google.com"}
    jar.write_text(json.dumps({"saved_at": time.time() - 86400, "cookies": [cookie]}))
    assert _by_id(_run())["google-cookies"].detail == "1 NID cookie(s), 1.0 of 14 days old"
    jar.write_text(json.dumps({"saved_at": time.time() - 15 * 86400, "cookies": [cookie]}))
    assert "past its TTL" in _by_id(_run())["google-cookies"].detail
    jar.write_text("{not json")
    c = _fails_as(_run(), "google-cookies", "config")
    assert str(jar) in c.detail


# ──────────────────────────────── Matrix ────────────────────────────────


def test_the_spa_key_check_compares_fingerprints_and_writes_no_key_cache(world: World) -> None:
    before = (_api_key._CACHE_PATH.read_text(), _api_key._CACHE_PATH.stat().st_mtime_ns)
    c = _by_id(_run())["matrix-spa-key"]
    assert c.detail == (
        f"{_doctor.fingerprint(_SPA_KEY)}, not the key in use ({_doctor.fingerprint(_KEY_IN_USE)})"
    )
    assert world.spa_gets == [
        "https://matrix.itasoftware.com/search",
        "https://www.gstatic.com/alkali/app.js",
    ]
    assert (_api_key._CACHE_PATH.read_text(), _api_key._CACHE_PATH.stat().st_mtime_ns) == before
    _api_key._CACHE_PATH.unlink()
    _run()
    assert not _api_key._CACHE_PATH.exists()


@pytest.mark.parametrize(
    ("status", "cause"), [(429, "throttled"), (503, "upstream"), (500, "upstream"), (404, "error")]
)
def test_a_spa_page_that_is_not_served_is_never_a_shape_change(
    world: World, status: int, cause: str
) -> None:
    world.spa = lambda _r: httpx.Response(status, text="<html>no bundle here</html>")
    c = _fails_as(_run(), "matrix-spa-key", cause)
    assert f"HTTP {status}" in c.detail


def test_a_served_spa_page_without_the_bundle_or_the_key_is_a_shape_change(world: World) -> None:
    world.spa = lambda _r: httpx.Response(200, text="<html>restructured</html>")
    assert "no SPA bundle" in _fails_as(_run(), "matrix-spa-key", "shape").detail
    world.spa = lambda r: httpx.Response(200, text=_HOME if r.url.path == "/search" else "var x=1")
    assert "no key tagged 'matrix'" in _fails_as(_run(), "matrix-spa-key", "shape").detail


def test_an_unreachable_spa_page_is_retryable(world: World) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    world.spa = refuse
    _fails_as(_run(), "matrix-spa-key", "unreachable")


def test_matrix_search_passes_on_a_priced_solution_naming_its_flight(world: World) -> None:
    c = _by_id(_run())["matrix-search"]
    assert c.status == "pass"
    assert "first priced GBP618.00 on AA142" in c.detail
    assert f"key={_KEY_IN_USE}" in str(world.matrix_requests[0].url)


def test_matrix_search_neither_reads_nor_writes_the_response_cache(world: World) -> None:
    import diskcache  # pyright: ignore[reportMissingTypeStubs]

    _run()
    _run()
    assert len(world.matrix_requests) == 2  # the second run was not served from a cache
    cache: Any = diskcache.Cache(  # pyright: ignore[reportUnknownMemberType]
        str(pathlib.Path(os.environ["MATRIX_CACHE_DIR"]) / "http")
    )
    try:
        assert len(cache) == 0
    finally:
        cache.close()


def test_with_no_key_in_use_the_search_runs_on_the_spa_key_or_is_skipped(world: World) -> None:
    _api_key._CACHE_PATH.unlink()
    _run()
    assert f"key={_SPA_KEY}" in str(world.matrix_requests[-1].url)
    world.spa = lambda _r: httpx.Response(503)
    c = _by_id(_run())["matrix-search"]
    assert (c.status, c.seconds) == ("skip", None)
    assert "matrix-spa-key" in c.detail
    assert len(world.matrix_requests) == 1


def _solutions_stripped_of(field: str) -> dict[str, Any]:
    body = json.loads(json.dumps(_MATRIX_OK))
    for s in body["solutionList"]["solutions"]:
        if field == "price":
            s.pop("ext", None)
            s.pop("displayTotal", None)
        else:
            for sl in s["itinerary"]["slices"]:
                sl["flights"] = []
    return body


@pytest.mark.parametrize(
    ("body", "cause", "said"),
    [
        ({"solutionCount": 0, "session": "s"}, "shape", "without a solutionList"),
        ({"solutionList": {"solutions": []}}, "brownout", "holds no solution"),
        (_solutions_stripped_of("price"), "shape", "has a price and a flight"),
        (_solutions_stripped_of("flights"), "shape", "has a price and a flight"),
        (
            {"error": {"message": "Internal server error", "type": "input"}},
            "brownout",
            "Internal server error",
        ),
        ({"error": {"message": "overloaded", "status": "UNAVAILABLE"}}, "brownout", "UNAVAILABLE"),
        (
            {"error": {"message": "Illegal COMMAND-LINE prefix", "type": "input"}},
            "rejected",
            "Illegal COMMAND-LINE prefix",
        ),
    ],
)
def test_matrix_answers_are_classified_by_what_they_say(
    world: World, body: dict[str, Any], cause: str, said: str
) -> None:
    world.matrix = lambda _r: httpx.Response(200, json=body)
    assert said in _fails_as(_run(), "matrix-search", cause).detail


@pytest.mark.parametrize("error", [{"code": 13, "message": None}, {"message": "x", "type": 7}])
def test_a_matrix_error_with_a_null_message_or_a_numeric_kind_is_still_reported(
    world: World, error: dict[str, Any]
) -> None:
    world.matrix = lambda _r: httpx.Response(200, json={"error": error})
    result = _invoke("--format", "json")
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    check = json.loads(result.stdout)["checks"][5]
    assert (check["id"], check["status"], check["cause"]) == ("matrix-search", "fail", "rejected")


def test_three_matrix_500s_are_an_outage_worth_retrying(world: World) -> None:
    world.matrix = lambda _r: httpx.Response(500, text="oops")
    with stamina.set_testing(True, attempts=3):
        report = _run()
    c = _fails_as(report, "matrix-search", "upstream")
    assert len(world.matrix_requests) == 3
    assert "500" in c.detail
    assert f"key={_KEY_IN_USE}" not in c.detail
    assert report.exit_code == 75


def test_a_matrix_429_is_a_throttle_and_a_timeout_is_a_brownout(world: World) -> None:
    world.matrix = lambda _r: httpx.Response(429, text="slow down")
    _fails_as(_run(), "matrix-search", "throttled")

    def stall(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    world.matrix = stall
    with stamina.set_testing(True, attempts=3):
        _fails_as(_run(), "matrix-search", "brownout")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    world.matrix = refuse
    with stamina.set_testing(True, attempts=3):
        _fails_as(_run(), "matrix-search", "unreachable")


def test_a_matrix_timeout_names_the_limit_on_each_attempt(world: World) -> None:
    def stall(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    world.matrix = stall
    with stamina.set_testing(True, attempts=3):
        c = _fails_as(_run(), "matrix-search", "brownout")
    assert len(world.matrix_requests) == 3
    assert c.detail == "Matrix did not answer within 60 s, the limit on each attempt"


def test_a_key_matrix_refuses_twice_is_auth_and_never_exits_75(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_api_key, "_bootstrap_from_spa", lambda: _SPA_KEY)
    world.matrix = lambda _r: httpx.Response(403, text="forbidden")
    report = _run()
    _fails_as(report, "matrix-search", "auth")
    assert report.exit_code == 1


def test_a_refused_key_whose_refetch_cannot_connect_is_unreachable(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network() -> str:
        try:
            raise httpx.ConnectError("no route to host")
        except httpx.ConnectError as e:
            raise _api_key.ApiKeyResolutionError("Network error contacting Matrix\n\nFix") from e

    monkeypatch.setattr(_api_key, "_bootstrap_from_spa", no_network)
    world.matrix = lambda _r: httpx.Response(403, text="forbidden")
    c = _fails_as(_run(), "matrix-search", "unreachable")
    assert c.detail == "Network error contacting Matrix"


# ──────────────────────────────── Google ────────────────────────────────


def test_google_passes_only_on_a_priced_row(world: World) -> None:
    report = _by_id(_run())
    assert report["google-http"].detail == "3 rows; first priced USD 179"
    assert report["google-browser"].detail == "3 rows; first priced USD 179"
    assert len(world.browser_urls) == 1


def test_a_zero_row_board_on_the_probe_route_is_a_shape_change(
    world: World, gf_session: Callable[..., object]
) -> None:
    gf_session(_page(_ds1("ds1_zero_rows.json")))
    world.browser = PageFetch(_page(_ds1("ds1_zero_rows.json")), _GF_URL, 200)
    report = _run()
    assert "empty board" in _fails_as(report, "google-http", "shape").detail
    _fails_as(report, "google-browser", "shape")


def test_a_page_without_ds1_is_a_shape_change(
    world: World, gf_session: Callable[..., object]
) -> None:
    gf_session("<html><body>no payload</body></html>")
    assert "ds:1" in _fails_as(_run(), "google-http", "shape").detail


def test_a_sorry_page_is_a_throttle_on_either_transport(
    world: World, gf_session: Callable[..., object]
) -> None:
    sorry = "<html>Our systems have detected unusual traffic from your network</html>"
    gf_session(sorry)
    world.browser = PageFetch(sorry, "https://www.google.com/sorry/index?continue=x", 200)
    report = _run()
    _fails_as(report, "google-http", "throttled")
    _fails_as(report, "google-browser", "throttled")
    assert report.exit_code == 75


def test_a_503_page_is_an_outage_not_a_shape_change(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(*_a: object, **_kw: object) -> PageFetch:
        return PageFetch("<html>busy</html>", _GF_URL, 503)

    monkeypatch.setattr(gfid, "_fetch_page", busy)
    world.browser = PageFetch("<html>busy</html>", _GF_URL, 503)
    report = _run()
    _fails_as(report, "google-http", "upstream")
    _fails_as(report, "google-browser", "upstream")


def test_a_consent_wall_and_a_dead_browser_are_their_own_causes(world: World) -> None:
    world.browser = PageFetch("<html>consent</html>", "https://consent.google.com/ml?x=1", 200)
    _fails_as(_run(), "google-browser", "consent")
    world.browser = GfBrowserUnavailableError("Chrome failed to launch.", remedy="Install Chrome.")
    c = _fails_as(_run(), "google-browser", "browser")
    assert c.detail == "Chrome failed to launch. Install Chrome."


def test_an_unreachable_google_is_retryable(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def reset(*_a: object, **_kw: object) -> PageFetch:
        raise gfid._RetryableTransportError("connection reset by peer")

    monkeypatch.setattr(gfid, "_fetch_page", reset)
    _fails_as(_run(), "google-http", "unreachable")


def test_a_missing_browser_override_fails_before_any_launch(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLIGHT_CLI_GF_BROWSER_BIN", "/nonexistent/flight-cli-no-chrome")
    lines: list[str] = []
    c = _fails_as(_run(lines), "google-browser", "config")
    assert "FLIGHT_CLI_GF_BROWSER_BIN" in c.detail
    assert "/nonexistent/flight-cli-no-chrome" in c.detail
    assert world.browser_urls == []
    assert not any(ln.startswith("google-browser") for ln in lines)


def test_the_browser_check_skips_without_patchright_or_without_chrome(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_doctor, "_patchright_installed", lambda: False)
    c = _by_id(_run())["google-browser"]
    assert (c.status, c.seconds) == ("skip", None)
    assert _gf_browser._INSTALL_HINT in c.detail
    monkeypatch.setattr(_doctor, "_patchright_installed", lambda: True)
    monkeypatch.delenv("FLIGHT_CLI_GF_BROWSER_BIN")
    monkeypatch.setattr(_doctor, "_chrome_channel_path", lambda: None)
    c = _by_id(_run())["google-browser"]
    assert c.status == "skip"
    assert "no Chrome" in c.detail
    assert world.browser_urls == []


def test_the_chrome_channel_path_is_where_patchright_looks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    import sys

    chrome = tmp_path / "Google Chrome"
    monkeypatch.setattr(_doctor, "_CHROME_CHANNEL", {sys.platform: str(chrome)})
    assert _doctor._chrome_channel_path() is None
    chrome.write_text("")
    chrome.chmod(0o755)
    assert _doctor._chrome_channel_path() == chrome
    monkeypatch.setattr(_doctor, "_CHROME_CHANNEL", {})
    assert _doctor._chrome_channel_path() is None


# ─────────────────────────────── providers ───────────────────────────────


def test_unconfigured_providers_skip_naming_the_auth_command(world: World) -> None:
    pp_auth.TOKENS_PATH.unlink()
    seats_auth.KEY_PATH.unlink()
    report = _by_id(_run())
    assert (report["pointspath"].status, report["seats-aero"].status) == ("skip", "skip")
    assert "`flight auth pp login`" in report["pointspath"].detail
    assert "`flight auth seats-aero key <KEY>`" in report["seats-aero"].detail
    assert world.pp_calls == []
    assert world.seats_requests == []


def test_providers_pass_with_the_expiry_and_the_quota_never_the_email(world: World) -> None:
    report = _by_id(_run())
    assert report["pointspath"].detail.startswith("token valid until ")
    assert "pricing-info lists 2 programs" in report["pointspath"].detail
    assert _PP_EMAIL not in report["pointspath"].detail
    assert world.pp_calls == [True]
    assert report["seats-aero"].detail == "the key works; 994 of 1000 requests left in the quota"
    assert world.seats_requests[0].url.params["take"] == "1"


def test_the_pointspath_expiry_is_the_token_that_answered_after_a_refresh_on_401(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored token looks fresh, PointsPath refuses it, and the client
    refreshes and retries on its own."""
    fresh = pp_auth.Tokens(
        access_token="pp-access-refreshed-" + "z" * 20,
        refresh_token=_PP_REFRESH,
        expires_at=int(time.time()) + 3600,
        user_email=_PP_EMAIL,
    )

    def refresh(_old: pp_auth.Tokens) -> pp_auth.Tokens:
        pp_auth.save_tokens(fresh)
        return fresh

    answers = iter(
        [httpx.Response(401, text="expired"), httpx.Response(200, json={"pricingInfos": []})]
    )

    def real_pp(tokens: pp_auth.Tokens) -> pp_client.PPClient:
        c = pp_client.PPClient(tokens)
        c._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: next(answers)), base_url=pp_client.API_BASE
        )
        return c

    monkeypatch.setattr(pp_client, "refresh_tokens", refresh)
    monkeypatch.setattr(pp_client, "PRICING_CACHE", world.tmp / "pp_pricing.json")
    monkeypatch.setattr(_doctor, "PPClient", real_pp)
    c = _by_id(_run())["pointspath"]
    expires = dt.datetime.fromtimestamp(fresh.expires_at, tz=dt.UTC)
    assert c.detail == (
        f"token valid until {expires:%Y-%m-%d %H:%M} UTC; pricing-info lists 0 programs"
    )


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_provider_credential_is_auth(world: World, status: int) -> None:
    world.pp = PPApiError("nope", endpoint="/api/pricing-info", status=status)
    world.seats = lambda _r: httpx.Response(status, text="bad key")
    report = _run()
    _fails_as(report, "pointspath", "auth")
    _fails_as(report, "seats-aero", "auth")
    assert report.exit_code == 1


def test_a_throttled_seats_aero_is_retryable(world: World) -> None:
    world.seats = lambda _r: httpx.Response(429, text="quota")
    _fails_as(_run(), "seats-aero", "throttled")


def test_a_stale_pointspath_token_whose_refresh_fails_is_a_failure_not_a_skip(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = pp_auth.load_tokens()
    assert tokens is not None
    tokens.expires_at = 0
    pp_auth.save_tokens(tokens)

    def refused(*_a: object, **_kw: object) -> httpx.Response:
        return httpx.Response(400, text="invalid refresh token")

    monkeypatch.setattr(pp_auth, "httpx", SimpleNamespace(post=refused))
    c = _fails_as(_run(), "pointspath", "auth")
    assert "refresh failed" in c.detail
    assert world.pp_calls == []


@pytest.mark.parametrize(("status", "cause"), [(429, "throttled"), (503, "upstream")])
def test_a_pointspath_refresh_supabase_throttles_or_cannot_serve_is_retryable(
    world: World, monkeypatch: pytest.MonkeyPatch, status: int, cause: str
) -> None:
    tokens = pp_auth.load_tokens()
    assert tokens is not None
    tokens.expires_at = 0
    pp_auth.save_tokens(tokens)

    def answered(*_a: object, **_kw: object) -> httpx.Response:
        return httpx.Response(status, text="Service Unavailable")

    monkeypatch.setattr(pp_auth, "httpx", SimpleNamespace(post=answered))
    report = _run()
    assert f"HTTP {status}" in _fails_as(report, "pointspath", cause).detail
    assert report.exit_code == 75


# ───────────────────────────── no secret leaves ─────────────────────────────


def test_no_stored_secret_reaches_either_stream_in_either_format(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every store holds a sentinel and every probe fails with text quoting one."""
    env_key = "sentinel-flight-api-key-0001"
    cached = "AIzaSy" + "C" * 33
    env_access, env_refresh = "sentinel-pp-access-0002", "sentinel-pp-refresh-0003"
    env_seats, disk_seats = "sentinel-seats-env-0004", "sentinel-seats-disk-0005"
    monkeypatch.setenv("FLIGHT_API_KEY", env_key)
    monkeypatch.setenv("PP_ACCESS_TOKEN", env_access)
    monkeypatch.setenv("PP_REFRESH_TOKEN", env_refresh)
    monkeypatch.setenv("SEATS_AERO_API_KEY", env_seats)
    _api_key._CACHE_PATH.write_text(cached + "\n")
    seats_auth.KEY_PATH.write_text(json.dumps({"api_key": disk_seats}))
    stored = [env_key, cached, env_access, env_refresh, env_seats, disk_seats, _PP_ACCESS]
    stored += [_PP_REFRESH]
    quoted = " ".join(stored)

    jar = gfid._cookie_path()
    jar.parent.mkdir(parents=True, exist_ok=True)
    jar.write_text(json.dumps({"saved_at": env_refresh, "cookies": []}))

    def spa_refuses(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {quoted}", request=request)

    def pp_refresh(*_a: object, **_kw: object) -> httpx.Response:
        return httpx.Response(400, text=f"invalid refresh token {env_refresh}")

    def page_raises(*_a: object, **_kw: object) -> PageFetch:
        raise RuntimeError(f"page {quoted}")

    world.spa = spa_refuses
    world.matrix = lambda _r: httpx.Response(500, text=quoted)
    world.browser = GfBrowserUnavailableError(f"Chrome said {quoted}")
    world.seats = lambda _r: httpx.Response(401, text=quoted)
    monkeypatch.setattr(pp_auth, "httpx", SimpleNamespace(post=pp_refresh))
    monkeypatch.setattr(gfid, "_fetch_page", page_raises)

    for fmt in ("table", "json"):
        with stamina.set_testing(True, attempts=3):
            result = _invoke("--format", fmt)
        assert result.exit_code == 1, result.output
        assert "retry_scheduled" in result.stderr  # the retry log ran, redacted
        for secret in stored:
            assert secret not in result.stdout, (fmt, secret)
            assert secret not in result.stderr, (fmt, secret)
    assert "<redacted>" in _by_id(_run())["matrix-search"].detail


def test_the_key_matrix_page_served_is_redacted_from_a_later_failure(world: World) -> None:
    _api_key._CACHE_PATH.unlink()
    body = {"error": {"message": f"key {_SPA_KEY} is not enabled", "type": "input"}}
    world.matrix = lambda _r: httpx.Response(200, json=body)
    for fmt in ("table", "json"):
        result = _invoke("--format", fmt)
        assert result.exit_code == 1
        assert _SPA_KEY not in result.stdout + result.stderr
    assert _doctor.fingerprint(_SPA_KEY) in _by_id(_run())["matrix-search"].detail


def test_an_unknown_key_query_value_is_redacted(world: World) -> None:
    doc = _doctor._Doctor(dt.date(2026, 9, 30), lambda _s: None)
    assert doc.redact("GET https://x.example/v1?alt=json&key=AIzaUNKNOWN123&b=2") == (
        "GET https://x.example/v1?alt=json&key=<redacted>&b=2"
    )


# ───────────────────────────────── the command ─────────────────────────────────


def test_table_and_json_carry_the_same_checks_in_the_same_order(world: World) -> None:
    world.spa = lambda _r: httpx.Response(503)
    pp_auth.TOKENS_PATH.unlink()
    table = _invoke()
    doc = json.loads(_invoke("--format", "json").stdout)
    rows = _table_rows(table.stdout)
    assert list(rows) == [c["id"] for c in doc["checks"]] == list(_doctor.CHECK_IDS)
    shown = {"pass": "pass", "fail": "FAIL", "skip": "skip"}
    assert list(rows.values()) == [shown[c["status"]] for c in doc["checks"]]
    assert "FAIL" in rows.values()
    assert "skip" in rows.values()
    assert "flight doctor" in table.stdout
    assert "8 passed, 1 failed, 1 skipped" in table.stdout


def test_the_command_exits_0_75_and_1(world: World, gf_session: Callable[..., object]) -> None:
    result = _invoke()
    assert result.exit_code == 0, result.output
    world.spa = lambda _r: httpx.Response(429)
    result = _invoke()
    assert result.exit_code == 75, result.output
    assert "every failure is retryable" in result.stdout
    gf_session(_page(_ds1("ds1_zero_rows.json")))
    assert _invoke("--format", "json").exit_code == 1


def test_each_live_probe_prints_one_dim_line_as_it_starts(world: World) -> None:
    result = _invoke("--format", "json")
    heads = [ln.split(":")[0] for ln in result.stderr.splitlines()]
    assert [h for h in heads if h in _doctor.CHECK_IDS] == list(_doctor.CHECK_IDS[4:])
    assert "opening Chrome" not in result.stderr


def test_a_hostile_detail_renders_escaped_with_no_traceback(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hostile(*_a: object, **_kw: object) -> PageFetch:
        raise RuntimeError("bad [/x] markup [bold]and\x1b[2J an escape")

    monkeypatch.setattr(gfid, "_fetch_page", hostile)
    result = _invoke()
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "[/x]" in result.stdout
    assert "[bold]" in result.stdout
    assert "\x1b" not in result.stdout
    assert "Traceback" not in result.output
    doc = json.loads(_invoke("--format", "json").stdout)
    assert "\x1b" in doc["checks"][6]["detail"]  # the document keeps it, JSON-escaped


def test_a_bad_format_exits_2(world: World) -> None:
    assert _invoke("--format", "yaml").exit_code == 2
    assert world.spa_gets == []


def test_help_adds_the_doctor_line_and_every_existing_command_stays() -> None:
    group = typer.main.get_command(cli.app)
    assert isinstance(group, click.Group)
    assert list(group.commands) == [
        *("search", "fare", "calendar", "detail", "explore", "gflight", "airport", "seatmap"),
        "doctor",
        "auth",
    ]
    out = CliRunner().invoke(cli.app, ["--help"]).stdout
    row = next(ln for ln in out.splitlines() if ln.startswith("│ doctor "))
    assert "Pass, fail or skip for every backend" in row
