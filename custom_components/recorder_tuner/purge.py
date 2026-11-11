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


async def async_excluded_ids(hass: HomeAssistant) -> list[str]:
    """Entities with stored rows that the recorder currently does not record."""
    from homeassistant.components.recorder import get_instance, is_entity_recorded
    from homeassistant.components.recorder.util import session_scope
    from sqlalchemy import text

    def _known() -> list[str]:
        with session_scope(hass=hass, read_only=True) as session:
            return [r[0] for r in session.execute(text("SELECT entity_id FROM states_meta")).all() if r[0]]

    known = await get_instance(hass).async_add_executor_job(_known)
    return sorted(e for e in known if not is_entity_recorded(hass, e))


async def async_schedule_after_restart(hass: HomeAssistant) -> None:
    """Remember a purge to run once HA has started again."""
    await _store(hass).async_save({"all_excluded": True, "repack": True})


async def async_run_pending(hass: HomeAssistant) -> None:
    """On startup, run a purge scheduled before the restart (if any)."""
    store = _store(hass)
    pending = await store.async_load()
    if not pending:
        return

    async def _run(_hass: HomeAssistant) -> None:
        # Clear first so a crash during purge can't cause a purge loop on every boot.
        await store.async_remove()
        try:
            ids = (
                await async_excluded_ids(hass)
                if pending.get("all_excluded")
                else list(pending.get("entity_ids", []))
            )
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
            f"Purging the stored history of {len(ids)} entities that are no longer recorded, "
            "then repacking the database. "
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
