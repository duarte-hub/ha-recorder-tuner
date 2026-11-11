"""Recorder Tuner: find chatty entities and exclude them from the recorder."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.loader import async_get_integration

from . import websocket
from .const import (
    DOMAIN,
    PANEL_COMPONENT,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL_PATH,
    STATIC_URL,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_FRONTEND_DIR = Path(__file__).parent / "frontend"


async def _register_static(hass: HomeAssistant) -> None:
    """Serve the panel JS once per HA run (aiohttp routes can't be removed)."""
    data = hass.data.setdefault(DOMAIN, {})
    if data.get("static_registered"):
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(_FRONTEND_DIR), cache_headers=False)]
    )
    data["static_registered"] = True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Recorder Tuner from a config entry."""
    await _register_static(hass)
    websocket.async_register(hass)

    version = str((await async_get_integration(hass, DOMAIN)).version)

    await panel_custom.async_register_panel(
        hass,
        webcomponent_name=PANEL_COMPONENT,
        frontend_url_path=PANEL_URL_PATH,
        module_url=f"{STATIC_URL}/recorder-tuner-panel.js?v={version}",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        require_admin=True,
        config={},
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove the sidebar panel."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
    return True
