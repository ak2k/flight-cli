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


class GfConsentError(GfBackendError):
    """Google served its consent interstitial instead of the search page.

    The consent page carries no `ds:1` payload, so there is nothing to parse.
    Seen from EU/EEA egress; a US-egress session normally never hits it."""


class GfPageShapeError(GfBackendError):
    """The search page loaded but no longer carries the rows we read.

    Either the `ds:1` blob is absent/undecodable, or it decoded and not one of
    its N rows parsed. Both mean Google changed the page and the extract needs
    re-deriving — never that the route has no flights."""


# Spelled out because the browser rung is the only transport that fails for
# reasons outside Google (no Chrome, a locked profile), where the fix is local
# rather than the "try the other backend" every other refusal offers.
_BROWSER_DEFAULT_REMEDY = "Retry, or use `--gf-transport http` (or `--backend matrix`)."


class GfBrowserUnavailableError(GfBackendError):
    """Rung 2 — a real Chrome navigating the same search page — could not run.

    Never a statement about the route: Chrome failed to launch, the navigation
    failed, or it handed back no body. Carries its own remedy text because both
    refusal renderers in `cli` print an unrecognized subclass as `str(e)` and
    nothing else — a remedy kept in the renderer would never reach the user.
    """

    def __init__(self, reason: str, *, remedy: str = _BROWSER_DEFAULT_REMEDY) -> None:
        """Name what failed, and what the user can do about it."""
        self.reason = reason
        self.remedy = remedy
        super().__init__(f"{reason} {remedy}")


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
