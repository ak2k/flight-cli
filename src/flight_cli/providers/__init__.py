"""Award providers (PointsPath, seats.aero, ...).

`base.py` defines the provider-neutral types and the `AwardProvider` Protocol.
`registry.py` discovers which providers are configured + enabled.
`pointspath/` holds the PointsPath-specific shim that adapts `pp/client.py` to
the Protocol.

Existing imports from `flight_cli.pp` still work — the move of pp/{auth,client,
match,models}.py into providers/pointspath/ is a separate, mechanical follow-up.
"""
