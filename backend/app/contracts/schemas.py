"""The generated files in `contracts/`: JSON Schemas of each format and the session OpenAPI."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from app.contracts.changes import ChangeSet, RiskFlags
from app.contracts.checks import CheckReport
from app.contracts.maps import AnalysisMap
from app.contracts.mesh import AssetExtras, PartExtras
from app.contracts.models import FRONTEND_MODELS_PATH, MODEL_CATALOG
from app.contracts.session import event_json_schema
from app.contracts.telemetry import telemetry_json_schema


def _schema(model: type[BaseModel], title: str) -> dict:
    schema = model.model_json_schema(mode="serialization")
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema, "title": title}


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def generated() -> dict[str, str]:
    from app.mock_api import contract_openapi_document

    mesh = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Mesh contract extras (glTF asset.extras and part node.extras)",
        "type": "object",
        "properties": {
            "asset": AssetExtras.model_json_schema(),
            "part": PartExtras.model_json_schema(),
        },
    }
    events = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **event_json_schema(),
        "title": "Session server-sent event (the JSON in each event's data line)",
    }
    telemetry = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **telemetry_json_schema(),
        "title": "Telemetry event (one line of a session's telemetry log)",
    }
    return {
        "mesh-extras.schema.json": _dump(mesh),
        "check-results.schema.json": _dump(_schema(CheckReport, "Check results")),
        "analysis-map.schema.json": _dump(_schema(AnalysisMap, "Analysis map")),
        "session-events.schema.json": _dump(events),
        "change-set.schema.json": _dump(_schema(ChangeSet, "Change set")),
        "risk-flags.schema.json": _dump(_schema(RiskFlags, "Risk flags")),
        "telemetry-event.schema.json": _dump(telemetry),
        "session-api.openapi.json": _dump(contract_openapi_document()),
        "models.json": _dump(MODEL_CATALOG.model_dump()),
    }


def frontend_copies() -> dict[Path, str]:
    """Generated files the frontend imports, since its build cannot read `contracts/`."""
    return {FRONTEND_MODELS_PATH: generated()["models.json"]}
