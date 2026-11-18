"""二进制传感器：门锁在线状态。

数据来源是 ``/api/lapp/device/info`` 的 ``status`` 字段（1=在线，0=离线）。
这个接口同时给出 ``signal``（WiFi 信号），所以「在线」与「弱网」是同一份数据，
不需要额外请求。

设计取舍：云端请求异常时**不**把实体置为 off，而是保留上一次的在线状态，
并把错误写进 ``last_error``。否则一次云端抖动就会误报「门锁离线」，
把真正的离线告警淹没在噪音里。
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    async_add_entities([OnlineBinarySensor(coordinator, entry)])


class OnlineBinarySensor(CoordinatorEntity[EzvizLockCoordinator], BinarySensorEntity):
    """门锁是否在线。"""

    _attr_has_entity_name = True
    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

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
        return f"{self.coordinator.client.device_serial}_online"

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        return bool(data.health.online) if data else True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if data is None:
            return {}
        health = data.health
        return {
            "signal_pct": health.signal_pct,
            "ssid": health.ssid,
            "net_type": health.net_type,
            "local_address": health.local_address,
            "last_update": health.last_update,
            "last_error": health.last_error,
        }
