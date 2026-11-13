"""事件实体：每次开门作为一个 HA 事件实体，供自动化用 state 触发器精准触发。

event_type 取值固定为英文（open_face / open_palm_vein / open_password …），
不随 UI 语言变化，写自动化时不用担心中文实体名被改。
"""

from __future__ import annotations

import logging

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, OPEN_TYPE_SLUGS
from .coordinator import DoorEvent, EzvizLockCoordinator

_LOGGER = logging.getLogger(__name__)

# open_face / open_palm_vein / … / door_opened（未知方式的兜底）
EVENT_TYPES = [f"open_{slug}" for slug in OPEN_TYPE_SLUGS.values()] + ["door_opened"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EzvizLockCoordinator = entry.runtime_data.coordinator
    async_add_entities([DoorEventEntity(coordinator, entry)])


class DoorEventEntity(CoordinatorEntity[EzvizLockCoordinator], EventEntity):
    """event_type 示例：open_face（人脸）、open_palm_vein（掌静脉）、open_password（密码）。"""

    _attr_has_entity_name = True
    _attr_translation_key = "door_event"
    _attr_event_types = EVENT_TYPES
    _attr_icon = "mdi:door"

    def __init__(self, coordinator: EzvizLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._last_uid: str | None = None
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.client.device_serial)},
            "name": (coordinator.data.device_name if coordinator.data else None) or "EZVIZ 门锁",
            "manufacturer": "EZVIZ",
            "model": "Y3000FVX Plus",
            "serial_number": coordinator.client.device_serial,
        }
        # 首次加载把当前最新事件标记为已处理，避免重启后补发一次
        last = coordinator.data.last if coordinator.data else None
        self._last_uid = last.uid if last else None

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.client.device_serial}_door_event"

    def _event_type_for(self, event: DoorEvent) -> str:
        slug = OPEN_TYPE_SLUGS.get(event.open_type)
        candidate = f"open_{slug}" if slug else None
        return candidate if candidate in EVENT_TYPES else "door_opened"

    @property
    def extra_state_attributes(self) -> dict | None:
        """每次读状态都从 coordinator 取最新值。

        EventEntity 默认把属性冻结在「触发那一刻」的快照里，而录像是在事件
        之后才抓完的，冻结就会导致 clip_url 永远停在 null。
        """
        attrs = dict(super().extra_state_attributes or {})
        last = self.coordinator.data.last if self.coordinator.data else None
        if last is None:
            return attrs or None
        attrs.update(
            {
                "open_time": last.open_time_iso,
                "user_name": last.user_name,
                "open_type": last.open_type,
                "open_type_name": last.open_type_name,
                "person_count": last.person_count,
                "person_count_source": last.person_count_source,
                "face_id": last.face_id,
                "snapshot_url": last.snapshot_url,
                "clip_url": last.clip_url,
                "clip_status": last.clip_status,
                "clip_bytes": last.clip_bytes,
                "alarm_type_name": last.alarm_type_name,
            }
        )
        return attrs

    @callback
    def _handle_coordinator_update(self) -> None:
        last: DoorEvent | None = self.coordinator.data.last if self.coordinator.data else None
        if last is not None and last.uid != self._last_uid:
            self._last_uid = last.uid
            self._trigger_event(
                self._event_type_for(last),
                {
                    "open_time": last.open_time_iso,
                    "user_name": last.user_name,
                    "open_type": last.open_type,
                    "open_type_name": last.open_type_name,
                    "person_count": last.person_count,
                    "person_count_source": last.person_count_source,
                    "face_id": last.face_id,
                    "snapshot_url": last.snapshot_url,
                    "clip_url": last.clip_url,
                    "alarm_type_name": last.alarm_type_name,
                },
            )
        super()._handle_coordinator_update()
