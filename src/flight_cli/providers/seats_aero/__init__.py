"""Seats.aero AwardProvider — second provider alongside PointsPath.

Complementary coverage:
  - Wider airline catalog (American, British, United, Virgin Atlantic,
    Finnair, plus many programs not in PointsPath's enabled-airlines tier)
  - Per-program-per-day availability with optional trip-level detail
  - 1000 calls/day Pro tier; X-RateLimit-Remaining on every response

Auth: Partner-Authorization header. Key sources, in order:
  - SEATS_AERO_API_KEY env var (highest priority — useful for one-off shells)
  - ~/.config/flight-cli/seats.json (persistent, set via `flight auth seats key`)

Registry integration goes through providers/registry.py:_construct_enabled.
See providers/base.py for the AwardProvider Protocol contract.
"""
