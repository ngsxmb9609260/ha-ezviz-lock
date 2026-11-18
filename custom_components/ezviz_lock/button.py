"""按钮：门前看一眼。

按一下就调 ``device/capture`` 让门锁立刻抓一张当前画面，落盘到
``/media/tpcamera/door-lock/peek/``，并把结果挂到 ``coordinator.peek``，
供「门前一眼」图片实体和面板按钮共用。

为什么不做成「打开直播」：这台锁的实时流要 20~30 秒才出画面（实测），
而单张抓拍 2~3 秒就能拿到，门口有人时看的就是这一刻。
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EzvizLockCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EzvizLockCoordinator = entry.runtime_data.coordinator
    async_add_entities([PeekButton(coordinator, entry)])


class PeekButton(CoordinatorEntity[EzvizLockCoordinator], ButtonEntity):
    """门前看一眼（立即抓拍一张当前画面）。"""

    _attr_has_entity_name = True
    _attr_translation_key = "peek"
    _attr_icon = "mdi:cctv"

    def __init__(self, coordinator: EzvizLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.client.device_serial)},
            "name": (coordinator.data.device_name if coordinator.data else None) or "EZVIZ 门锁",
            "manufacturer": "EZVIZ",
            "model": "Y3000FVX Plus",
            "serial_number": coordinator.client.device_serial,
        }

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.client.device_serial}_peek"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.peek or {}

    async def async_press(self) -> None:
        result = await self.coordinator.async_peek()
        if result.get("error"):
            _LOGGER.warning("门前看一眼失败：%s", result["error"])
        else:
            _LOGGER.info("门前看一眼：%s", result.get("url"))
