# pyright: reportPrivateUsage=false
# DIVERGE: the Matrix client is given a MockTransport through `_http._client`,
# the pattern tests/test_verify.py follows; the constructor has no transport
# injection point.
"""Both checks of a Google row on Matrix, `--verify` and the merged search's
low check, call the row another itinerary or unpriced only from Matrix's whole
answer to its chain.

Each complete answer below is cut in one place: a stop, a flight or a slice
of one summary, a flight, a through flight's leg or a slice of one candidate's
booking details, the answer's count, or the end of its page. A cut in the
row's own solution, or in an answer where no solution is the row, leaves an
answer that may hide the row, so both paths give no verdict, with one
sentence. A cut elsewhere leaves the match standing."""

from __future__ import annotations

import copy
import io
from typing import TYPE_CHECKING, Any, NamedTuple

import anyio
import httpx
import pytest
import typer
from rich.console import Console

from flight_cli import _verify as v
from flight_cli import cli
from flight_cli.client import MatrixClient
from flight_cli.domain import SearchOptions
from test_verify import _chain, _flight, _Matrix, _row, _segment

if TYPE_CHECKING:
    import pathlib


class _Answer(NamedTuple):
    """Matrix's answer to `row`'s chain: the search body, booking details by
    solution id, and the id of the solution that is the row, if any."""

    row: v.Row
    chain: dict[str, Any]
    details: dict[str, dict[str, Any]]
    match: str | None


def _slice(
    frm: str, to: str, dep: str, arr: str, flights: list[str], stops: list[str]
) -> dict[str, Any]:
    return {
        "origin": {"code": frm},
        "destination": {"code": to},
        "departure": dep,
        "arrival": arr,
        "flights": flights,
        "stops": [{"code": s} for s in stops],
    }


def _summary(sid: str, *slices: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": sid,
        "ext": {"price": "USD284.00"},
        "displayTotal": "USD284.00",
        "itinerary": {"slices": list(slices)},
    }


def _booked(*slices: list[dict[str, Any]]) -> dict[str, Any]:
    return {"bookingDetails": {"itinerary": {"slices": [{"segments": s} for s in slices]}}}


def _through(code: str, *legs: tuple[str, str, str, str]) -> dict[str, Any]:
    """One booked segment flying `legs` under one number."""
    seg = _segment(code, legs[0][0], legs[-1][1], legs[0][2], legs[-1][3])
    seg["legs"] = [
        {"origin": {"code": f}, "destination": {"code": t}, "departure": d, "arrival": a}
        for f, t, d, a in legs
    ]
    return seg


_AS_ROW = _row(
    (
        _flight("AS21", "JFK", "SEA", "2026-10-20T07:22", "2026-10-20T10:31"),
        _flight("AS487", "SEA", "LAX", "2026-10-20T12:05", "2026-10-20T14:34"),
    )
)
_AS_DETAILS = [
    _segment("AS21", "JFK", "SEA", "2026-10-20T07:22-04:00", "2026-10-20T10:31-07:00"),
    _segment("AS487", "SEA", "LAX", "2026-10-20T12:05-07:00", "2026-10-20T14:34-07:00"),
]


def _as_out(lands: str = "2026-10-20") -> dict[str, Any]:
    return _slice(
        "JFK",
        "LAX",
        "2026-10-20T07:22-04:00",
        f"{lands}T14:34-07:00",
        ["AS21", "AS487"],
        ["SEA"],
    )


def _dl_back(lands: str = "2026-10-28") -> dict[str, Any]:
    return _slice("LAX", "JFK", "2026-10-27T23:59-07:00", f"{lands}T08:10-04:00", ["DL747"], [])


_RT_ROW = _row(
    _AS_ROW.slices[0],
    (_flight("DL747", "LAX", "JFK", "2026-10-27T23:59", "2026-10-28T08:10"),),
)
_RT_DETAILS = [
    _segment("DL747", "LAX", "JFK", "2026-10-27T23:59-07:00", "2026-10-28T08:10-04:00"),
]


def _l4_summary(sid: str, lands: str) -> dict[str, Any]:
    return _summary(
        sid,
        _slice(
            "JFK",
            "LAX",
            "2026-10-20T17:06-04:00",
            f"{lands}T23:51-07:00",
            ["AA3120", "AA1630", "AA2038"],
            ["CLT", "DFW"],
        ),
    )


def _l4_details(middle_day: str) -> list[dict[str, Any]]:
    return [
        _segment("AA3120", "JFK", "CLT", "2026-10-20T17:06-04:00", "2026-10-20T19:15-04:00"),
        _segment("AA1630", "CLT", "DFW", f"{middle_day}T19:50-04:00", f"{middle_day}T21:45-05:00"),
        _segment("AA2038", "DFW", "LAX", "2026-10-21T22:10-05:00", "2026-10-21T23:51-07:00"),
    ]


_XX_ROW = _row(
    (
        _flight("XX1", "JFK", "DEN", "2026-10-20T08:00", "2026-10-20T10:00"),
        _flight("XX1", "DEN", "LAX", "2026-10-20T11:00", "2026-10-20T13:00"),
    )
)

# The complete answers the suite checks both paths on: L2's pair, alone and
# together in either order, L4's two candidates, a through flight, and a
# round trip.
_ANSWERS = {
    "the row's own": _Answer(
        _AS_ROW,
        _chain(_summary("AS-1", _as_out())),
        {"AS-1": _booked(_AS_DETAILS)},
        "AS-1",
    ),
    "landing a day later": _Answer(
        _AS_ROW, _chain(_summary("AS-2", _as_out("2026-10-21"))), {}, None
    ),
    "the row's own, then a day later": _Answer(
        _AS_ROW,
        _chain(_summary("AS-1", _as_out()), _summary("AS-2", _as_out("2026-10-21"))),
        {"AS-1": _booked(_AS_DETAILS)},
        "AS-1",
    ),
    "a day later, then the row's own": _Answer(
        _AS_ROW,
        _chain(_summary("AS-2", _as_out("2026-10-21")), _summary("AS-1", _as_out())),
        {"AS-1": _booked(_AS_DETAILS)},
        "AS-1",
    ),
    "L4": _Answer(
        _row(
            (
                _flight("AA3120", "JFK", "CLT", "2026-10-20T17:06", "2026-10-20T19:15"),
                _flight("AA1630", "CLT", "DFW", "2026-10-21T19:50", "2026-10-21T21:45"),
                _flight("AA2038", "DFW", "LAX", "2026-10-21T22:10", "2026-10-21T23:51"),
            )
        ),
        _chain(
            _l4_summary("L4-1", "2026-10-20"),
            _l4_summary("L4-2", "2026-10-21"),
            _l4_summary("L4-3", "2026-10-21"),
        ),
        {
            "L4-2": _booked(_l4_details("2026-10-20")),
            "L4-3": _booked(_l4_details("2026-10-21")),
        },
        "L4-3",
    ),
    "a through flight": _Answer(
        _XX_ROW,
        _chain(
            _summary(
                "XX-1",
                _slice(
                    "JFK",
                    "LAX",
                    "2026-10-20T08:00-04:00",
                    "2026-10-20T13:00-07:00",
                    ["XX1", "XX1"],
                    ["DEN"],
                ),
            )
        ),
        {
            "XX-1": _booked(
                [
                    _through(
                        "XX1",
                        ("JFK", "DEN", "2026-10-20T08:00-04:00", "2026-10-20T10:00-06:00"),
                        ("DEN", "LAX", "2026-10-20T11:00-06:00", "2026-10-20T13:00-07:00"),
                    )
                ]
            )
        },
        "XX-1",
    ),
    "a round trip": _Answer(
        _RT_ROW,
        _chain(_summary("RT-1", _as_out(), _dl_back())),
        {"RT-1": _booked(_AS_DETAILS, _RT_DETAILS)},
        "RT-1",
    ),
    "a round trip returning a day later": _Answer(
        _RT_ROW, _chain(_summary("RT-2", _as_out(), _dl_back("2026-10-29"))), {}, None
    ),
}


def _cut(a: _Answer, side: str, *path: str | int | slice) -> _Answer:
    """`a` with the item at `path` in its chain or its booking details removed."""
    chain, details = copy.deepcopy(a.chain), copy.deepcopy(a.details)
    node: Any = chain if side == "chain" else details
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    return a._replace(chain=chain, details=details)


def _runs(flights: list[str]) -> list[tuple[int, int]]:
    """Each flight's tokens as a `[start, end)` span: a through flight may be
    written once per leg."""
    spans: list[tuple[int, int]] = []
    for i, code in enumerate(flights):
        if spans and flights[spans[-1][0]] == code:
            spans[-1] = (spans[-1][0], i + 1)
        else:
            spans.append((i, i + 1))
    return spans


def _cases() -> list[Any]:
    out: list[Any] = []
    for name, a in _ANSWERS.items():
        out.append(pytest.param(a, "match" if a.match else "other-itinerary", id=f"{name}: whole"))
        sols: list[dict[str, Any]] = a.chain["solutionList"]["solutions"]
        for i, sol in enumerate(sols):
            sid: str = sol["id"]
            want = "match" if a.match not in (None, sid) else "no-answer"
            where = ("solutionList", "solutions", i, "itinerary", "slices")
            for j, sl in enumerate(sol["itinerary"]["slices"]):
                for k, stop in enumerate(sl["stops"]):
                    out.append(
                        pytest.param(
                            _cut(a, "chain", *where, j, "stops", k),
                            want,
                            id=f"{name}: {sid}'s summary without stop {stop['code']}",
                        )
                    )
                for start, end in _runs(sl["flights"]):
                    out.append(
                        pytest.param(
                            _cut(a, "chain", *where, j, "flights", slice(start, end)),
                            want,
                            id=f"{name}: {sid}'s summary without {sl['flights'][start]}",
                        )
                    )
                out.append(
                    pytest.param(
                        _cut(a, "chain", *where, j),
                        want,
                        id=f"{name}: {sid}'s summary without slice {j}",
                    )
                )
        for sid, d in a.details.items():
            want = "match" if a.match not in (None, sid) else "no-answer"
            where = (sid, "bookingDetails", "itinerary", "slices")
            for j, sl in enumerate(d["bookingDetails"]["itinerary"]["slices"]):
                for k, seg in enumerate(sl["segments"]):
                    code = f"{seg['carrier']['code']}{seg['flight']['number']}"
                    out.append(
                        pytest.param(
                            _cut(a, "details", *where, j, "segments", k),
                            want,
                            id=f"{name}: {sid}'s details without {code}",
                        )
                    )
                    for leg in range(len(seg["legs"]) if len(seg["legs"]) > 1 else 0):
                        out.append(
                            pytest.param(
                                _cut(a, "details", *where, j, "segments", k, "legs", leg),
                                want,
                                id=f"{name}: {sid}'s details without {code}'s leg {leg}",
                            )
                        )
                out.append(
                    pytest.param(
                        _cut(a, "details", *where, j),
                        want,
                        id=f"{name}: {sid}'s details without slice {j}",
                    )
                )
        out.append(
            pytest.param(
                _cut(a, "chain", "solutionCount"),
                "match" if a.match else "no-answer",
                id=f"{name}: without solutionCount",
            )
        )
        for p in range(len(sols)):
            kept = [s["id"] for s in sols[:p]]
            out.append(
                pytest.param(
                    _cut(a, "chain", "solutionList", "solutions", slice(p, None)),
                    "match" if a.match in kept else "no-answer",
                    id=f"{name}: page cut to {p:d} of {len(sols):d}",
                )
            )
    return out


def _fake(a: _Answer) -> _Matrix:
    fake = _Matrix()
    fake.chain, fake.details = a.chain, a.details
    return fake


def _client(fake: _Matrix, tmp_path: pathlib.Path) -> MatrixClient:
    c = MatrixClient(api_key="test-key", cache_dir=str(tmp_path), rps=1000.0)
    c._http._client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return c


def _verify_path(
    a: _Answer, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> tuple[str, str | None]:
    """`--verify`'s check of the row: its outcome, or "no-answer" and the
    last line it printed before exit 1."""
    fake = _fake(a)

    def _matrix(**_kw: Any) -> MatrixClient:
        return _client(fake, tmp_path)

    monkeypatch.setattr(cli, "MatrixClient", _matrix)
    buf = io.StringIO()
    monkeypatch.setattr(cli, "err", Console(file=buf, width=500, no_color=True))
    try:
        checked = cli._check_on_matrix(a.row, 1, SearchOptions(), rps=None, impersonate=None)
    except typer.Exit as e:
        assert e.exit_code == 1, buf.getvalue()
        return "no-answer", buf.getvalue().strip().splitlines()[-1]
    return checked.verdict.outcome, None


def _low_check_path(a: _Answer, tmp_path: pathlib.Path) -> tuple[str, str | None]:
    """The low check's question of the row: its outcome, or "no-answer" and
    its reason."""
    fake = _fake(a)

    async def go() -> v.Verdict:
        async with _client(fake, tmp_path) as c:
            return await cli._exact_flights_on(c, a.row, SearchOptions())

    try:
        verdict = anyio.run(go)
    except cli._UncheckableAnswerError as e:
        return "no-answer", str(e)
    return verdict.outcome, None


@pytest.mark.parametrize(("answer", "want"), _cases())
def test_an_answer_cut_anywhere_is_no_answer_on_both_paths_unless_the_match_stands(
    answer: _Answer, want: str, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    verify = _verify_path(answer, monkeypatch, tmp_path)
    low = _low_check_path(answer, tmp_path)
    assert verify[0] == want, verify
    assert low == verify
