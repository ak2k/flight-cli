"""The README and the Matrix memo name every call that keeps a refused body."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _flat(rel: str) -> str:
    return " ".join((_ROOT / rel).read_text().split())


def test_docs_name_every_call_that_keeps_a_refused_matrix_body() -> None:
    readme = _flat("README.md")
    assert (
        "When Matrix answers with a body the response models cannot read, a search, "
        "a booking-details or fare-rules follow-up, or an airport lookup stops with a line "
        "naming the field, the issue tracker and a saved copy of the body under "
        "`~/.cache/flight-cli/shape-changes/`" in readme
    )
    memo = _flat("docs/memories/public_alkali_wrapper.md")
    assert (
        "A Matrix call whose answer the models refuse (a search, a booking-details or "
        "fare-rules follow-up, or an airport lookup) saves it as "
        "`shape-changes/matrix-<UTC stamp>.json`" in memo
    )
    assert "A search whose answer the models refuse" not in memo
