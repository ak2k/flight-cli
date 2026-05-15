"""PointsPath integration: award prices joined to ITA Matrix cash itineraries.

Architecture mirrors the rest of flight_cli — Pydantic models in models.py,
async HTTP in client.py, CLI surface in cli.py. Auth + token refresh is
isolated in auth.py so the rest of the package stays unaware of Supabase.
"""
