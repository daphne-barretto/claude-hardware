"""Which catalogue models the server's API key can use, for the model picker (`GET /api/models`).

`MODEL_CATALOG` (`app.contracts.models`) names every model the app knows; not every key can use
every model. At startup, and again once the list is `REFRESH_SECONDS` old, the server asks the
Models API (`GET /v1/models`, page by page) which models its key has, and the picker is served
only the catalogue entries on that list, with the catalogue's names, blurbs and order.

- If the Models API can't be reached (or there is no API key), the whole catalogue is served,
  and a run on a model the key can't use is handled when it fails.
- A run that fails because its model isn't available (`app.agent.limits`, `model_unavailable`)
  marks it unavailable for everyone at once (`mark_unavailable`), until the next successful
  refresh decides again. Runs asked for an unavailable model go on with the default (`usable`).

Tests swap `fetch` for a fake Models API.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.contracts.models import MODEL_CATALOG, ModelCatalog

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/models"
API_VERSION = "2023-06-01"
REFRESH_SECONDS = 6 * 3600
PAGE_SIZE = 1000


def list_models(api_key: str, *, transport: Any = None, timeout: float = 10.0) -> list[str]:
    """Every model ID the key can use, following the pages (`has_more`, `after_id`)."""
    headers = {"x-api-key": api_key, "anthropic-version": API_VERSION}
    ids: list[str] = []
    params: dict[str, Any] = {"limit": PAGE_SIZE}
    with httpx.Client(transport=transport, timeout=timeout, headers=headers) as client:
        while True:
            r = client.get(API_URL, params=params)
            r.raise_for_status()
            body = r.json()
            ids += [m["id"] for m in body.get("data", [])]
            if not body.get("has_more") or not body.get("last_id"):
                return ids
            params = {"limit": PAGE_SIZE, "after_id": body["last_id"]}


class ModelAvailability:
    def __init__(self, fetch: Callable[[str], list[str]] = list_models) -> None:
        self.fetch = fetch
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.available: set[str] | None = None  # None: unknown, so every catalogue model
        self.unavailable: set[str] = set()  # failed in a run since the last refresh
        self.refreshed_at: float | None = None

    def stale(self) -> bool:
        return self.refreshed_at is None or time.monotonic() - self.refreshed_at > REFRESH_SECONDS

    async def refresh(self) -> None:
        """Ask the Models API again. A failure keeps what was known (logged)."""
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        self.refreshed_at = time.monotonic()  # tried: don't ask again on every request
        if not key:
            return
        try:
            ids = await asyncio.to_thread(self.fetch, key)
        except Exception:
            log.warning(
                "can't list the API key's models; the picker offers the whole catalogue",
                exc_info=True,
            )
            return
        with self._lock:
            self.available = set(ids)
            self.unavailable = set()
        hidden = sorted(MODEL_CATALOG.ids() - self.available)
        if hidden:
            log.warning("the API key can't use %s: left out of the model picker", hidden)

    async def refresh_if_stale(self) -> None:
        if self.stale():
            await self.refresh()

    def is_available(self, model: str) -> bool:
        with self._lock:
            if model in self.unavailable:
                return False
            return self.available is None or model in self.available

    def mark_unavailable(self, model: str | None) -> None:
        """A run found `model` unavailable: leave it out of every picker until the next
        successful refresh."""
        if model:
            with self._lock:
                self.unavailable.add(model)
            log.warning("model %s isn't available: left out of the picker", model)

    def catalog(self) -> ModelCatalog:
        """The catalogue entries the key can use, in order. The default stays unless it is
        unavailable; then the first available main model. Nothing usable: the whole catalogue."""
        models = [m for m in MODEL_CATALOG.models if self.is_available(m.id)]
        more = [m for m in MODEL_CATALOG.more_models if self.is_available(m.id)]
        if not models:
            if not more:
                return MODEL_CATALOG
            models, more = more[:1], more[1:]
        ids = [m.id for m in models]
        default = MODEL_CATALOG.default if MODEL_CATALOG.default in ids else ids[0]
        return ModelCatalog(default=default, models=models, more_models=more)

    def usable(self, model: str | None) -> str | None:
        """`model`, or the default when it isn't available (None: the server picks)."""
        if model is None or self.is_available(model):
            return model
        return self.catalog().default

    def default_name(self) -> str:
        catalog = self.catalog()
        return next(m.name for m in catalog.models if m.id == catalog.default)


AVAILABILITY = ModelAvailability()
