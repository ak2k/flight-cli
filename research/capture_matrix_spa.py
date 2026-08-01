#!/usr/bin/env python
"""Capture the Matrix SPA's URL state + /batch request body, unattended.

Why this exists: the SPA names things differently from the /batch API
(`routing`/`ext` vs `routeLanguage`/`commandLine`), so `links.py` cannot be
written from the API side — the field names have to be observed. See
docs/memories/matrix_spa_url_state.md.

Run:  uv run --with patchright python research/capture_matrix_spa.py

Headless is blocked by Google's waa-pa bot attestation, so this drives real
Chrome via patchright. It still runs without supervision.

Form-driving notes, each learned the hard way:
  * Airports are an autocomplete: type, then CLICK the mat-option. `fill()`
    leaves the model empty and Search stays disabled.
  * The date input has NO placeholder — select it by `input.mat-datepicker-input`.
  * The date must be typed with `press_sequentially`; `fill()` sets the value
    but doesn't fire the events Angular's form model listens for, so Search
    stays disabled with a date visibly present.
  * `mat-input-*` ids are regenerated per render — never select on them.
"""

from __future__ import annotations

import base64
import json
import pathlib
import sys
import urllib.parse

from patchright.sync_api import Page, sync_playwright

PROFILE = "/tmp/mx-capture-profile"
URL_OUT = pathlib.Path("/tmp/matrix_spa_state.json")
REQ_OUT = pathlib.Path("/tmp/matrix_batch_body.json")


def _decode_state(url: str) -> dict | None:
    if "search=" not in url:
        return None
    q = urllib.parse.unquote(url.split("search=", 1)[1].split("&")[0])
    q += "=" * (-len(q) % 4)
    try:
        return json.loads(base64.b64decode(q))
    except Exception:
        return None


def _pick_airport(pg: Page, index: int, code: str) -> None:
    box = pg.locator('input[placeholder="Add airport"]').nth(index)
    box.click()
    box.type(code, delay=120)
    pg.wait_for_timeout(1800)
    option = pg.locator("mat-option, [role=option]").first
    option.wait_for(state="visible", timeout=10_000)
    option.click()
    pg.wait_for_timeout(700)


def capture(origin: str, dest: str, date_mmddyyyy: str, routing: str, extension: str) -> int:
    bodies: list[dict] = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, channel="chrome", headless=False,
            viewport={"width": 1500, "height": 1000},
        )
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_request(req):
            if "alkali" not in req.url and "batch" not in req.url:
                return
            body = req.post_data
            if body and ("routeLanguage" in body or "commandLine" in body):
                bodies.append({"url": req.url.split("?")[0], "body": body})

        pg.on("request", on_request)
        pg.goto("https://matrix.itasoftware.com/", wait_until="domcontentloaded")
        pg.wait_for_timeout(5000)

        _pick_airport(pg, 0, origin)
        _pick_airport(pg, 1, dest)

        pg.locator("text=One way").first.click()
        pg.wait_for_timeout(1500)

        date_in = pg.locator("input.mat-datepicker-input").first
        date_in.click()
        pg.wait_for_timeout(600)
        date_in.press_sequentially(date_mmddyyyy, delay=90)
        pg.wait_for_timeout(800)
        pg.keyboard.press("Tab")
        pg.wait_for_timeout(1200)

        pg.locator("text=Advanced controls").first.click()
        pg.wait_for_timeout(1500)
        pg.locator('input[placeholder="Routing"]').first.fill(routing)
        pg.locator('input[placeholder="Extension"]').first.fill(extension)
        pg.wait_for_timeout(600)

        search = pg.locator('button:has-text("Search")').first
        if search.get_attribute("disabled"):
            print("Search still disabled — the form shape changed", file=sys.stderr)
            ctx.close()
            return 1
        search.click()

        state = None
        for _ in range(90):
            pg.wait_for_timeout(1000)
            found = _decode_state(pg.url)
            if found and (found.get("slices") or [{}])[0].get("routing"):
                state = found
                break
            if found:
                state = found
            if bodies:
                break

        if state:
            URL_OUT.write_text(json.dumps(state, indent=1))
            print(f"URL state    -> {URL_OUT}")
            print(json.dumps(state["slices"][0], indent=1))
        if bodies:
            REQ_OUT.write_text(json.dumps(bodies, indent=1))
            print(f"batch bodies -> {REQ_OUT} ({len(bodies)})")
        ctx.close()
        return 0 if state else 1


if __name__ == "__main__":
    raise SystemExit(
        capture(
            origin="JFK", dest="LHR", date_mmddyyyy="09/01/2026",
            routing="BA+", extension="MAXSTOPS 0",
        )
    )
