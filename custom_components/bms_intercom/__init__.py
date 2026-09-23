"""The BMS Intercom integration."""
from __future__ import annotations

import logging
import os

import voluptuous as vol

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv

from .const import (
    ATTR_TEST_DOOR,
    CONF_PROXY_PORT,
    DEFAULT_PROXY_PORT,
    DOMAIN,
    PLATFORMS,
    SERVICE_PROBE,
)
from .device import BMSIntercomDevice
from .proxy import HTTPSProxy

_LOGGER = logging.getLogger(__name__)

_PROXY_KEY = f"{DOMAIN}_https_proxy"
_FRONTEND_FLAG = f"{DOMAIN}_frontend_registered"
_STATIC_URL = f"/{DOMAIN}_static"
# Bump on any frontend change so browsers reload the cached module.
_CARD_VERSION = "0.6.0"
_CARD_URL = f"{_STATIC_URL}/bms_intercom_card.js?v={_CARD_VERSION}"


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve and auto-load the bundled popup module (once per HA run)."""
    if hass.data.get(_FRONTEND_FLAG):
        return
    hass.data[_FRONTEND_FLAG] = True
    frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
    await hass.http.async_register_static_paths(
        [StaticPathConfig(_STATIC_URL, frontend_dir, False)]
    )
    add_extra_js_url(hass, _CARD_URL)
    _LOGGER.debug("Поп-ап домофона зарегистрирован: %s", _CARD_URL)


async def _async_start_proxy(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Start the built-in local HTTPS endpoint once (for the microphone)."""
    if hass.data.get(_PROXY_KEY) is not None:
        return
    port = entry.options.get(CONF_PROXY_PORT, DEFAULT_PROXY_PORT)
    proxy = HTTPSProxy(hass, port)
    hass.data[_PROXY_KEY] = proxy
    await proxy.async_start()


_PROBE_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        # Off by default: a diagnostic must not unlock the entrance door.
        vol.Optional(ATTR_TEST_DOOR, default=False): cv.boolean,
    }
)


async def _async_register_services(hass: HomeAssistant) -> None:
    """Expose bms_intercom.probe (same diagnostics as the button)."""
    if hass.services.has_service(DOMAIN, SERVICE_PROBE):
        return

    async def _handle_probe(call: ServiceCall) -> None:
        entry_id = call.data.get("entry_id")
        devices = hass.data.get(DOMAIN, {})
        targets = (
            [devices[entry_id]] if entry_id and entry_id in devices
            else list(devices.values())
        )
        if not targets:
            _LOGGER.warning("bms_intercom.probe: нет настроенных домофонов")
        for device in targets:
            await device.async_probe(test_door=call.data.get(ATTR_TEST_DOOR, False))

    hass.services.async_register(
        DOMAIN, SERVICE_PROBE, _handle_probe, schema=_PROBE_SCHEMA
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BMS Intercom from a config entry."""
    await _async_register_frontend(hass)
    await _async_register_services(hass)
    await _async_start_proxy(hass, entry)

    device = BMSIntercomDevice(hass, entry)
    await device.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = device

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        device: BMSIntercomDevice = hass.data[DOMAIN].pop(entry.entry_id)
        await device.async_shutdown()
        # Stop the shared HTTPS proxy when the last intercom is removed.
        if not hass.data.get(DOMAIN):
            proxy = hass.data.pop(_PROXY_KEY, None)
            if proxy is not None:
                await proxy.async_stop()
            hass.services.async_remove(DOMAIN, SERVICE_PROBE)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
