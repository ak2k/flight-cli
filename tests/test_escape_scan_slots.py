"""A Rich table or panel slot filled by assignment is read like a print.

Rich parses a title, caption, column header or footer and panel subtitle as
markup when the object renders, so `t.title = <remote>` hands the parser the
same text `err.print(<remote>)` would, with no call anywhere near it."""

from __future__ import annotations

from test_calendar_split import escape_scan


def test_a_markup_slot_filled_by_assignment_is_scanned() -> None:
    missed: list[str] = []
    for slot in ("title", "caption", "header", "footer", "subtitle"):
        source = "def _render_search():\n    t." + slot + " = f'{res.price}'\n"
        if not any(f".{slot} " in fault for fault in escape_scan(source)):
            missed.append(slot)
    assert not missed, f"the scan does not read an assignment into: {missed}"


def test_a_wrapped_literal_or_unrelated_assignment_stays_silent() -> None:
    clean = (
        "def _render_search():\n"
        "    t.title = _safe_text(res.price)\n"
        "    t.caption = 'literal'\n"
        "    t.title = f'[b]{_safe_text(res.price)}[/]'\n"
        # Not a slot, and a slot name that is not the attribute assigned.
        "    obj.label = f'{res.price}'\n"
        "    self.title.text = f'{res.price}'\n"
        # Annotated without a value, and a plain local named like a slot.
        "    t.title: str\n"
        "    title = f'{res.price}'\n"
    )
    assert not escape_scan(clean)


def test_assignment_shapes_into_a_slot_are_scanned() -> None:
    shapes = {
        "annotated": "def _render_search():\n    t.title: str = f'{res.price}'\n",
        "augmented": "def _render_search():\n    t.title += f'{res.price}'\n",
        "subscripted column": "def _render_search():\n    t.columns[0].header = f'{res.price}'\n",
        "tuple target": "def _render_search():\n    t.title, t.caption = res.a, res.b\n",
    }
    missed = [name for name, source in shapes.items() if not escape_scan(source)]
    assert not missed, f"the scan does not catch: {missed}"
