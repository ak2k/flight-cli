"""PointsPath as an AwardProvider.

The provider lives in providers/pointspath/provider.py. Reads from
flight_cli.pp.{auth,client,models} for now; those files will physically
move into this package in a follow-up rename PR, with pp/ kept as a
deprecation shim re-exporting from here for one release.
"""

from .provider import PointsPathProvider

__all__ = ["PointsPathProvider"]
