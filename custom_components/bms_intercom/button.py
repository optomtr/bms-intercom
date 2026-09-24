"""Buttons to drive the intercom: answer, reject, open door, probe the panel."""
from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
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
    entities: list[ButtonEntity] = [
        AnswerButton(device),
        RejectButton(device),
        OpenDoorButton(device),
        ProbeButton(device),
    ]
    # The "simulate call" button only makes sense without real hardware.
    if device.is_demo:
        entities.append(SimulateCallButton(device))
    else:
        entities.append(TestCallButton(device))
    async_add_entities(entities)


class TestCallButton(BMSIntercomEntity, ButtonEntity):
    """Проверить звонок, не выходя на улицу к кнопке вызова (реальный режим).

    Сначала просит панель позвонить по-настоящему, не вышло — запасной
    тестовый вызов с живым видео (testcall.py). Что именно произошло — в
    атрибуте `test_call_result`: владелец видит это без журнала.
    """

    _attr_name = "Тестовый звонок"
    _attr_translation_key = "test_call"
    _attr_icon = "mdi:phone-ring-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _intercom_role = "test_call"

    def __init__(self, device: BMSIntercomDevice) -> None:
        super().__init__(device, "test_call")

    async def async_press(self) -> None:
        await self.device.async_test_call()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **self.intercom_attributes,
            "test_call_result": self.device.test_call_result,
        }


class SimulateCallButton(BMSIntercomEntity, ButtonEntity):
    _attr_name = "Симулировать звонок"
    _attr_icon = "mdi:phone-plus"
    _intercom_role = "simulate"

    def __init__(self, device: BMSIntercomDevice) -> None:
        super().__init__(device, "simulate_call")

    async def async_press(self) -> None:
        await self.device.async_simulate_call()


class AnswerButton(BMSIntercomEntity, ButtonEntity):
    _attr_name = "Ответить"
    _attr_icon = "mdi:phone"
    _intercom_role = "answer"

    def __init__(self, device: BMSIntercomDevice) -> None:
        super().__init__(device, "answer")

    async def async_press(self) -> None:
        await self.device.async_answer()


class RejectButton(BMSIntercomEntity, ButtonEntity):
    _attr_name = "Сбросить"
    _attr_icon = "mdi:phone-hangup"
    _intercom_role = "reject"

    def __init__(self, device: BMSIntercomDevice) -> None:
        super().__init__(device, "reject")

    async def async_press(self) -> None:
        await self.device.async_reject()


class OpenDoorButton(BMSIntercomEntity, ButtonEntity):
    _attr_name = "Открыть дверь"
    _attr_icon = "mdi:door-open"
    _intercom_role = "open_door"

    def __init__(self, device: BMSIntercomDevice) -> None:
        super().__init__(device, "open_door")

    async def async_press(self) -> None:
        await self.device.async_open_door()


class ProbeButton(BMSIntercomEntity, ButtonEntity):
    """Diagnostics: ask the panel which ISAPI endpoints it actually supports.

    The report goes to the Home Assistant log and into this entity's
    attributes (`probe_report`). It never contains the password.
    """

    _attr_name = "Проверить панель"
    _attr_icon = "mdi:stethoscope"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _intercom_role = "probe"

    def __init__(self, device: BMSIntercomDevice) -> None:
        super().__init__(device, "probe")

    async def async_press(self) -> None:
        await self.device.async_probe()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {**self.intercom_attributes, **self.device.probe_attributes}
