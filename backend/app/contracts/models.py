"""The models a message can ask for: the one list behind the model picker and the session API.

`MODEL_CATALOG` is the source of truth. `python -m app.contracts` writes it to
`contracts/models.json` and to `frontend/src/generated/models.json`, which the picker imports
(the frontend build cannot read files outside `frontend/`). `MessageCreate.model` accepts only
IDs listed here. The picker offers only those the server's API key can use
(`app.model_availability`, `GET /api/models`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.contracts.common import CONTRACTS_DIR, REPO_ROOT, Contract, ContractError

MODELS_PATH = CONTRACTS_DIR / "models.json"
FRONTEND_MODELS_PATH = REPO_ROOT / "frontend" / "src" / "generated" / "models.json"


class ModelOption(Contract):
    id: str = Field(min_length=1, description="Model ID sent as `MessageCreate.model`")
    name: str = Field(min_length=1, description="Shown on the picker button and in the menu")
    blurb: str = Field(default="", description="One line under the name in the menu")


class ModelCatalog(Contract):
    contract: Literal["models/1"] = "models/1"
    default: str = Field(description="Model a new chat starts with; one of `models`")
    models: list[ModelOption] = Field(min_length=1, description="The picker's main list")
    more_models: list[ModelOption] = Field(
        default_factory=list,
        description='Under "More models"; the item is hidden when this is empty',
    )

    @model_validator(mode="after")
    def _consistent(self) -> ModelCatalog:
        ids = [m.id for m in self.models + self.more_models]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"model IDs listed twice: {dupes}")
        if self.default not in {m.id for m in self.models}:
            raise ValueError(f"default {self.default!r} is not in the main list")
        return self

    def ids(self) -> set[str]:
        return {m.id for m in self.models + self.more_models}


MODEL_CATALOG = ModelCatalog(
    # What a new chat uses. Tests that call Claude pin the cheapest model themselves
    # (`tests/test_agent_live.py`).
    default="claude-opus-5-5",
    models=[
        ModelOption(id="claude-fable-5-2", name="Fable 5.2", blurb="For your toughest challenges"),
        ModelOption(
            id="claude-opus-5-5", name="Opus 5.5", blurb="For complex work and everyday tasks"
        ),
        ModelOption(
            id="claude-sonnet-5-5", name="Sonnet 5.5", blurb="Most efficient for simpler tasks"
        ),
        ModelOption(id="claude-haiku-5-5", name="Haiku 5.5", blurb="Fastest for quick answers"),
    ],
    more_models=[
        # Previous generation, as named on the API (checked against the key's models at
        # startup like the rest: `app.model_availability`).
        ModelOption(id="claude-opus-4-8", name="Opus 4.8", blurb="Previous generation"),
        ModelOption(id="claude-sonnet-4-6", name="Sonnet 4.6", blurb="Previous generation"),
        ModelOption(id="claude-haiku-4-5-20251001", name="Haiku 4.5", blurb="Previous generation"),
    ],
)


def check_model_id(model: str | None, catalog: ModelCatalog = MODEL_CATALOG) -> str | None:
    """`None` (the server picks) or a model ID from the catalogue."""
    if model is not None and model not in catalog.ids():
        raise ContractError(f"unknown model {model!r}; see contracts/models.json")
    return model
