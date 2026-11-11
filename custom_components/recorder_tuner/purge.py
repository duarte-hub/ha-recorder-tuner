"""Purge + repack, either now or once Home Assistant has restarted."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.pending_purge"
STORAGE_VERSION = 1
NOTIFICATION_ID = f"{DOMAIN}_purge"


def _store(hass: HomeAssistant) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, STORAGE_KEY)


async def async_purge_now(hass: HomeAssistant, entity_ids: list[str], repack: bool = True) -> None:
    """Queue recorder.purge_entities for the entities, then a purge with repack.

    Both calls only queue work on the recorder thread and return immediately.
    """
    if entity_ids:
        await hass.services.async_call(
            "recorder", "purge_entities", {"entity_id": entity_ids, "keep_days": 0}, blocking=True
        )
    if repack:
        # keep_days left out -> recorder uses the configured purge_keep_days.
        await hass.services.async_call("recorder", "purge", {"repack": True}, blocking=True)
    _LOGGER.info("Recorder Tuner queued purge for %d entities (repack=%s)", len(entity_ids), repack)


async def async_schedule_after_restart(hass: HomeAssistant, entity_ids: list[str]) -> None:
    """Remember a purge to run once HA has started again."""
    await _store(hass).async_save({"entity_ids": sorted(set(entity_ids)), "repack": True})


async def async_run_pending(hass: HomeAssistant) -> None:
    """On startup, run a purge scheduled before the restart (if any)."""
    store = _store(hass)
    pending = await store.async_load()
    if not pending:
        return

    async def _run(_hass: HomeAssistant) -> None:
        # Clear first so a crash during purge can't cause a purge loop on every boot.
        await store.async_remove()
        ids = list(pending.get("entity_ids", []))
        try:
            await async_purge_now(hass, ids, bool(pending.get("repack", True)))
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Scheduled purge failed")
            persistent_notification.async_create(
                hass,
                f"The purge scheduled before the restart failed: {err}",
                title="Recorder Tuner",
                notification_id=NOTIFICATION_ID,
            )
            return
        persistent_notification.async_create(
            hass,
            f"Purging the old history of {len(ids)} excluded entities and repacking the database. "
            "This runs in the background and can take several minutes on a large database; "
            "the recorder catches up afterwards.",
            title="Recorder Tuner",
            notification_id=NOTIFICATION_ID,
        )

    @callback
    def _schedule(_hass: HomeAssistant) -> None:
        hass.async_create_task(_run(_hass), f"{DOMAIN}_pending_purge")

    if hass.state is CoreState.running:
        _schedule(hass)
    else:
        async_at_started(hass, _schedule)
