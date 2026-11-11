"""Websocket API used by the Recorder Tuner panel."""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import voluptuous as vol
from sqlalchemy import text

from homeassistant.components import websocket_api
from homeassistant.components.recorder import get_instance, is_entity_recorded
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er

from . import purge, yaml_editor
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register websocket commands (idempotent)."""
    data = hass.data.setdefault(DOMAIN, {})
    if data.get("ws_registered"):
        return
    websocket_api.async_register_command(hass, ws_analyze)
    websocket_api.async_register_command(hass, ws_save)
    websocket_api.async_register_command(hass, ws_purge)
    data["ws_registered"] = True


# --------------------------------------------------------------------------- DB


def _query_counts(hass: HomeAssistant, since_ts: float, limit: int) -> dict[str, Any]:
    """Count state rows per entity since `since_ts` (runs in the recorder executor)."""
    with session_scope(hass=hass, read_only=True) as session:
        rows = session.execute(
            text(
                "SELECT sm.entity_id, COUNT(*) AS c "
                "FROM states s JOIN states_meta sm ON s.metadata_id = sm.metadata_id "
                "WHERE s.last_updated_ts >= :since "
                "GROUP BY sm.entity_id ORDER BY c DESC LIMIT :limit"
            ),
            {"since": since_ts, "limit": limit},
        ).all()
        total = session.execute(
            text("SELECT COUNT(*) FROM states WHERE last_updated_ts >= :since"),
            {"since": since_ts},
        ).scalar()
        oldest = session.execute(text("SELECT MIN(last_updated_ts) FROM states")).scalar()
    return {
        "rows": [(r[0], int(r[1])) for r in rows],
        "total": int(total or 0),
        "oldest": float(oldest) if oldest else None,
    }


def _db_size(db_url: str) -> int | None:
    """Size of a SQLite database incl. WAL, or None for other engines."""
    if not db_url.startswith("sqlite:///"):
        return None
    path = db_url[len("sqlite:///"):].split("?", 1)[0]
    size = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            size += os.path.getsize(path + suffix)
        except OSError:
            pass
    return size or None


async def _energy_entities(hass: HomeAssistant) -> set[str]:
    """Every entity id referenced by the Energy dashboard config."""
    found: set[str] = set()
    try:
        from homeassistant.components.energy.data import async_get_manager

        manager = await async_get_manager(hass)
        prefs = manager.data or {}
    except Exception:  # noqa: BLE001 - energy not set up / API changed
        return found

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)
        elif isinstance(node, str) and _ENTITY_ID_RE.match(node):
            found.add(node)

    walk(prefs)
    return found


# --------------------------------------------------------------------------- analyze


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/analyze",
        vol.Optional("hours", default=24): vol.All(vol.Coerce(float), vol.Range(min=1, max=24 * 31)),
        vol.Optional("limit", default=400): vol.All(vol.Coerce(int), vol.Range(min=10, max=5000)),
    }
)
@websocket_api.async_response
async def ws_analyze(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Return per-entity write counts plus YAML exclude state."""
    instance = get_instance(hass)
    now = time.time()
    since = now - msg["hours"] * 3600

    try:
        db = await instance.async_add_executor_job(_query_counts, hass, since, msg["limit"])
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Recorder query failed")
        connection.send_error(msg["id"], "query_failed", str(err))
        return

    db_size = await hass.async_add_executor_job(_db_size, str(instance.db_url))

    yaml_info: dict[str, Any]
    try:
        excl = await hass.async_add_executor_job(yaml_editor.read_excludes, hass.config.config_dir)
        yaml_info = {
            "ok": True,
            "file": excl.file,
            "mode": excl.mode,
            "entities": excl.entities,
            "entity_globs": excl.entity_globs,
            "domains": excl.domains,
            "has_include_filter": excl.has_include_filter,
        }
    except Exception as err:  # noqa: BLE001
        yaml_info = {"ok": False, "error": str(err), "entities": []}

    energy = await _energy_entities(hass)
    registry = er.async_get(hass)

    # Hours actually covered (DB may hold less history than requested).
    covered_from = max(since, db["oldest"]) if db["oldest"] else since
    hours_covered = max((now - covered_from) / 3600, 1 / 60)

    counts = dict(db["rows"])
    ids = list(counts)
    for ent in yaml_info.get("entities", []):
        if ent not in counts:
            ids.append(ent)

    entities = []
    for entity_id in ids:
        state = hass.states.get(entity_id)
        reg = registry.async_get(entity_id)
        attrs = state.attributes if state else {}
        entities.append(
            {
                "entity_id": entity_id,
                "count": counts.get(entity_id, 0),
                "per_day": round(counts.get(entity_id, 0) * 24 / hours_covered, 1),
                "name": attrs.get("friendly_name") or (reg.name or reg.original_name if reg else None),
                "platform": reg.platform if reg else None,
                "device_class": attrs.get("device_class") or (reg.device_class or reg.original_device_class if reg else None),
                "state_class": attrs.get("state_class"),
                "unit": attrs.get("unit_of_measurement"),
                "exists": state is not None,
                "recorded": is_entity_recorded(hass, entity_id),
                "in_yaml": entity_id in yaml_info.get("entities", []),
                "energy": entity_id in energy,
            }
        )

    connection.send_result(
        msg["id"],
        {
            "hours_requested": msg["hours"],
            "hours_covered": round(hours_covered, 2),
            "total_rows": db["total"],
            "total_per_day": round(db["total"] * 24 / hours_covered),
            "db_size": db_size,
            "db_engine": str(instance.db_url).split(":", 1)[0],
            "yaml": yaml_info,
            "entities": entities,
        },
    )


# --------------------------------------------------------------------------- save


async def _config_errors(hass: HomeAssistant) -> set[str]:
    from homeassistant.helpers.check_config import async_check_ha_config_file

    res = await async_check_ha_config_file(hass)
    return {f"{e.domain or 'config'}: {e.message}" for e in res.errors}


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/save",
        vol.Optional("add", default=[]): [cv.entity_id],
        vol.Optional("remove", default=[]): [cv.entity_id],
    }
)
@websocket_api.async_response
async def ws_save(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Write exclude changes to YAML, validate, and roll back on new errors."""
    errors_before = await _config_errors(hass)

    try:
        result = await hass.async_add_executor_job(
            yaml_editor.apply_changes, hass.config.config_dir, msg["add"], msg["remove"]
        )
    except yaml_editor.RecorderYamlError as err:
        connection.send_error(msg["id"], "yaml_error", str(err))
        return
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Writing recorder config failed")
        connection.send_error(msg["id"], "write_failed", str(err))
        return

    if result.backup is None:
        connection.send_result(msg["id"], {"changed": False, "file": result.file})
        return

    new_errors = (await _config_errors(hass)) - errors_before
    if new_errors:
        await hass.async_add_executor_job(yaml_editor.restore, result.file, result.original_text)
        connection.send_error(
            msg["id"],
            "config_invalid",
            "Config check failed after the edit, so it was rolled back: " + "; ".join(sorted(new_errors)),
        )
        return

    _LOGGER.info(
        "Recorder Tuner updated %s (added %s, removed %s, backup %s)",
        result.file, result.added, result.removed, result.backup,
    )
    connection.send_result(
        msg["id"],
        {
            "changed": True,
            "file": result.file,
            "backup": result.backup,
            "added": result.added,
            "removed": result.removed,
            "preexisting_errors": sorted(errors_before),
        },
    )


# --------------------------------------------------------------------------- purge


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/purge",
        vol.Optional("entity_ids", default=[]): [cv.entity_id],
        vol.Optional("when", default="now"): vol.In(["now", "after_restart"]),
    }
)
@websocket_api.async_response
async def ws_purge(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Purge + repack now, or restart HA and purge once it is back up."""
    ids: list[str] = msg["entity_ids"]

    if msg["when"] == "now":
        try:
            await purge.async_purge_now(hass, ids)
        except Exception as err:  # noqa: BLE001
            connection.send_error(msg["id"], "purge_failed", str(err))
            return
        connection.send_result(msg["id"], {"queued": len(ids), "restarting": False})
        return

    # after_restart: HA refuses to restart with an invalid config, so check first
    # rather than leaving a purge pending for some later boot.
    errors = await _config_errors(hass)
    if errors:
        connection.send_error(
            msg["id"], "config_invalid",
            "Home Assistant won't restart with config errors: " + "; ".join(sorted(errors)),
        )
        return

    await purge.async_schedule_after_restart(hass, ids)
    connection.send_result(msg["id"], {"queued": len(ids), "restarting": True})
    hass.async_create_task(
        hass.services.async_call("homeassistant", "restart", {}, blocking=False),
        f"{DOMAIN}_restart",
    )
