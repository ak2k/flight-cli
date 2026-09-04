"""Typed refusals from the Google Flights backend.

A refusal is not a result. Google answers with a captcha interstitial, a
consent wall, or a re-shaped page in exactly the situations where returning an
empty list would read as "this route has no flights" — the worst failure mode
this CLI has, because it is indistinguishable from a real answer. So each
refusal gets its own type and they share `GfBackendError`, which is what the
Matrix-fallback seams in `cli` catch.

Parsed-empty stays a *result*: a search page that decodes with zero rows is
Google's answer, not a refusal, and never raises from here.

Lives in its own module because `links.build_search_tfs` raises
`GfTfsUnsupportedError` and `_gflight_ids` imports `links` — a shared leaf
keeps that from becoming a cycle.
"""

from __future__ import annotations


class GfBackendError(Exception):
    """Google Flights declined to serve the request.

    Catch this at a fallback seam to degrade to Matrix; catch a subclass to
    tell the user which wall they hit."""


class GfThrottledError(GfBackendError):
    """Google Flights rate-limited this IP (a `/sorry/` interstitial or 429).

    Distinct from a transport error and from a parsed-empty board. Recovery is
    usually fast; callers may retry shortly or fall back to Matrix."""


class GfTransportError(GfBackendError):
    """Google Flights could not be REACHED — the retries for it are spent.

    A condition of the route rather than of the request: DNS, TLS, a reset
    socket, a stalled body. Typed apart from the other refusals because it says
    nothing about the query, so a caller iterating over related queries learns
    from one of these that the rest will fail the same way."""


class GfConsentError(GfBackendError):
    """Google served its consent interstitial instead of the search page.

    The consent page carries no `ds:1` payload, so there is nothing to parse.
    Seen from EU/EEA egress; a US-egress session normally never hits it."""


class GfPageShapeError(GfBackendError):
    """The search page loaded but no longer carries the rows we read.

    The `ds:1` blob is absent or undecodable; or it decoded but holds its
    flight rows somewhere other than the indices we read; or rows were found
    and not one of the N parsed. All mean Google changed the page and the
    extract needs re-deriving — never that the route has no flights."""


class GfPinIgnoredError(GfBackendError):
    """A well-formed page, read completely, answering a leg nobody asked for.

    A pin goes out as `selected_flight` and the response says nothing about
    which pin it belongs to, so a page that dropped it arrives shaped exactly
    like one that honoured it — and only the served legs tell them apart. The
    rows parsed; what they describe is a different segment.

    A direct child of `GfBackendError` rather than of `GfPageShapeError`,
    because a nested type inherits its parent's `match` arm: this would then
    render as a layout change and send the next reader to re-derive an extract
    that is working.
    """


class GfTfsUnsupportedError(GfBackendError):
    """A filter the `tfs` search-page encoder cannot express.

    `page_can_encode` keeps these queries on Matrix, so in normal operation
    this never escapes; it is the loud backstop for a constraint that reached
    the encoder anyway, in place of silently dropping it."""

    def __init__(self, field: str, reason: str) -> None:
        """Name the offending filter field and why the page can't carry it."""
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")
