"""What the session API serves for one build: its mesh, check report, CAD source and maps.

The session API builds these for imported, stored and fixture builds alike, and the telemetry
log keeps a copy of each ready build's (`app.telemetry.log`).
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field


class BuildArtifacts(BaseModel):
    """What the session API serves for one build."""

    mesh: str
    checks: dict
    source: dict
    maps: dict[str, dict] = Field(default_factory=dict)

    def face_ids(self) -> frozenset[str]:
        doc = json.loads(self.mesh)
        return frozenset(
            f["id"] for n in doc.get("nodes", []) for f in n.get("extras", {}).get("faces", [])
        )
