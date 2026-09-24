"""Binary sensor showing whether the intercom is ringing / in a call."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .device import BMSIntercomDevice
from .entity import BMSIntercomEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device: BMSIntercomDevice = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([CallBinarySensor(device)])


class CallBinarySensor(BMSIntercomEntity, BinarySensorEntity):
    """On while the panel is ringing or the call is answered."""

    _attr_name = "Вызов"
    _attr_icon = "mdi:phone-ring"
    _intercom_role = "call"
    # panel_events — диагностика, меняется от каждого события панели:
    # в историю (recorder) не пишем, чтобы не раздувать базу.
    _unrecorded_attributes = frozenset({"panel_events"})

    def __init__(self, device: BMSIntercomDevice) -> None:
        super().__init__(device, "call")

    @property
    def is_on(self) -> bool | None:
        """None (unknown) while the panel is unreachable — never a false 'off'."""
        if self.device.panel_available is False:
            return None
        return self.device.call_active

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **self.intercom_attributes,
            "call_state": self.device.call_state,
            # Последние 10 нераспознанных событий панели (без персональных
            # полей): по ним видно код нажатия «Вызов», если мы его не узнали.
            "panel_events": self.device.panel_event_log.items,
        }
