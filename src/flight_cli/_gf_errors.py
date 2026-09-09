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
    """Google Flights rate-limited the request (a `/sorry/` interstitial or 429).

    Not "this IP": the budget is keyed on client context, which is the whole
    premise of the browser rung — real Chrome keeps pulling results from an IP
    that is simultaneously throttling curl_cffi, so naming the IP would tell the
    user the one thing that is not true and hide the move that works.

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
    extract needs re-deriving — never that the route has no flights.

    A page that never loaded is NOT one of these: an upstream non-2xx is
    `GfUpstreamStatusError`, because "Google changed the page" and "Google
    refused to serve it" send the reader to different work."""


class GfUpstreamStatusError(GfBackendError):
    """Google answered the search page with a non-2xx that is not a throttle.

    Either rung raises it. Rung 1 goes around the `raise_for_status()` in fli's
    client, so it reads the status off the response; rung 2 off the navigation.

    Its own type because a 403 or a 503 is Google declining to serve, which is
    usually transient and needs no investigation, while `GfPageShapeError` says
    the extract is broken and someone must re-derive the indices. Reading one as
    the other sends a reader hunting a parser bug during an outage."""

    def __init__(self, status_code: int) -> None:
        """Name the status; the caller has no other detail worth carrying."""
        self.status_code = status_code
        super().__init__(f"Google Flights' search page returned HTTP {status_code}")


# Spelled out because the browser rung is the only transport that fails for
# reasons outside Google (no Chrome, a locked profile), where the fix is local
# rather than the "try the other backend" every other refusal offers.
BROWSER_DEFAULT_REMEDY = "Retry, or use `--gf-transport http` (or `--backend matrix`)."


class GfBrowserUnavailableError(GfBackendError):
    """Rung 2 — a real Chrome navigating the same search page — could not run.

    Never a statement about the route: Chrome failed to launch, the navigation
    failed, or it handed back no body. `remedy` is kept as its own attribute
    because the two renderings in `cli` need it in different places — the full
    message quotes `str(e)`, and the one-line note the enrich path prints has to
    append `e.remedy` itself or the user never learns what to install.
    """

    def __init__(self, reason: str, *, remedy: str = BROWSER_DEFAULT_REMEDY) -> None:
        """Name what failed, and what the user can do about it."""
        self.reason = reason
        self.remedy = remedy
        super().__init__(f"{reason} {remedy}")


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
