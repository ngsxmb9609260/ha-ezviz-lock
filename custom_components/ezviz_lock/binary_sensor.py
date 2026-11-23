"""二进制传感器：门锁在线状态 + 门锁在网状态。

``online``（云端心跳）
    数据来源是 ``/api/lapp/device/info`` 的 ``status`` 字段（1=在线，0=离线），
    10 分钟轮询一次。这个接口同时给出 ``signal``（WiFi 信号），所以「在线」与
    「弱网」是同一份数据，不需要额外请求。
    设计取舍：云端请求异常时**不**把实体置为 off，而是保留上一次的在线状态，
    并把错误写进 ``last_error``。否则一次云端抖动就会误报「门锁离线」。

``lan_present``（局域网被动探活）
    只听门锁自己周期性发出的 ARP 探测帧（实测约 55 秒一次），一个包都不发，
    因此不会唤醒设备。它是秒级判据，能区分「休眠」和「真断网」——休眠时设备
    照样发帧，断网时帧消失。
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    LAN_WATCH_GRACE_S,
    LAN_WATCH_SCAN_S,
    LAN_WATCH_TIMEOUT_S,
)
from .coordinator import EzvizLockCoordinator

# 只影响本平台里 should_poll 的实体（云端那条是 CoordinatorEntity，不受影响）
SCAN_INTERVAL = timedelta(seconds=LAN_WATCH_SCAN_S)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EzvizLockCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        [
            OnlineBinarySensor(coordinator, entry),
            LanPresentBinarySensor(coordinator, entry),
        ]
    )


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


class LanPresentBinarySensor(BinarySensorEntity):
    """门锁是否还在局域网里（被动监听，不唤醒设备）。"""

    _attr_has_entity_name = True
    _attr_translation_key = "lan_present"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = True

    def __init__(self, coordinator: EzvizLockCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
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
        return f"{self._coordinator.client.device_serial}_lan_present"

    @property
    def available(self) -> bool:
        watcher = self._coordinator.lan_watcher
        # 监听线程没起来（多半是容器缺 NET_RAW）时保持 unavailable，
        # 而不是伪装成「不在网」——那会把权限问题说成设备掉线。
        return watcher is not None and watcher.running and not watcher.error

    @property
    def is_on(self) -> bool:
        watcher = self._coordinator.lan_watcher
        if watcher is None:
            return False
        age = watcher.age_s
        if age is None:
            # 一帧都还没见到：刚启动时给宽限，之后就算掉线
            return (time.monotonic() - watcher.started_at) < LAN_WATCH_GRACE_S
        return age <= LAN_WATCH_TIMEOUT_S

    @property
    def icon(self) -> str:
        return "mdi:lan-connect" if self.is_on else "mdi:lan-disconnect"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        watcher = self._coordinator.lan_watcher
        if watcher is None:
            return {}
        attrs = watcher.snapshot()
        attrs["timeout_s"] = LAN_WATCH_TIMEOUT_S
        return attrs
