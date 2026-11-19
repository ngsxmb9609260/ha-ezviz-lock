"""传感器：最近一次开门、今日次数、人数、开锁方式。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import DoorEvent, EzvizLockCoordinator, LoiterEvent, LockData

_LOGGER = logging.getLogger(__name__)


def _brief_loiter(lo: LoiterEvent) -> dict[str, Any]:
    """徘徊事件的对外精简结构（前端卡片按这个渲染）。"""
    return {
        "uid": lo.uid,
        "time": lo.time_iso,
        "time_ms": lo.time_ms,
        "duration_s": lo.duration_s,
        "alarm_type_name": lo.alarm_type_name,
        "snapshot_url": lo.snapshot_url,
        "clip_url": lo.clip_url,
        "clip_status": lo.clip_status,
        "clip_bytes": lo.clip_bytes,
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    coordinator: EzvizLockCoordinator = runtime.coordinator
    async_add_entities(
        [
            LastOpenUserSensor(coordinator, entry),
            LastOpenTimeSensor(coordinator, entry),
            LastOpenTypeSensor(coordinator, entry),
            PersonCountSensor(coordinator, entry),
            TodayOpenCountSensor(coordinator, entry),
            LoiterCountSensor(coordinator, entry),
            BatterySensor(coordinator, entry),
            WifiSignalSensor(coordinator, entry),
            PowerStatusSensor(coordinator, entry),
            MembersSensor(coordinator, entry),
        ]
    )


class _BaseLockSensor(CoordinatorEntity[EzvizLockCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EzvizLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.client.device_serial)},
            "name": coordinator.data.device_name if coordinator.data else "EZVIZ 门锁",
            "manufacturer": "EZVIZ",
            "model": "Y3000FVX Plus",
            "serial_number": coordinator.client.device_serial,
        }

    @property
    def _last(self) -> DoorEvent | None:
        return self.coordinator.data.last if self.coordinator.data else None

    def _event_attributes(self, event: DoorEvent | None) -> dict[str, Any]:
        if event is None:
            return {}
        return {
            "open_time": event.open_time_iso,
            "user_name": event.user_name,
            "open_type": event.open_type,
            "open_type_name": event.open_type_name,
            "person_count": event.person_count,
            "person_count_source": event.person_count_source,
            "face_id": event.face_id,
            "snapshot_url": event.snapshot_url,
            "snapshot_path": event.snapshot_path,
            "snapshot_source": event.snapshot_source,
            "snapshot_delay_s": (
                max(0, (event.capture_ms - event.open_time_ms) // 1000)
                if event.capture_ms
                else None
            ),
            "clip_url": event.clip_url,
            "clip_remote_url": event.clip_remote_url,
            "alarm_id": event.alarm_id,
            "alarm_type": event.alarm_type,
            "alarm_type_name": event.alarm_type_name,
            "channel_no": event.channel_no,
            "event_uid": event.uid,
            "source": event.source,
        }


class LastOpenUserSensor(_BaseLockSensor):
    """state = 最近一次开门的人员名称。"""

    _attr_translation_key = "last_open_user"
    _attr_icon = "mdi:account-key"

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.client.device_serial}_last_open_user"

    @property
    def native_value(self) -> str | None:
        last = self._last
        return last.user_name if last else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._event_attributes(self._last)


class LastOpenTimeSensor(_BaseLockSensor):
    """state = 最近一次开门时间（timestamp）。"""

    _attr_translation_key = "last_open_time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-outline"

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.client.device_serial}_last_open_time"

    @property
    def native_value(self) -> datetime | None:
        last = self._last
        if last is None:
            return None
        return dt_util.as_local(
            datetime.fromtimestamp(last.open_time_ms / 1000, tz=dt_util.UTC)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._event_attributes(self._last)


class LastOpenTypeSensor(_BaseLockSensor):
    """state = 最近一次开锁方式（人脸 / 掌静脉 / 密码 / 室内开门 …）。"""

    _attr_translation_key = "last_open_type"
    _attr_icon = "mdi:lock-smart"

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.client.device_serial}_last_open_type"

    @property
    def native_value(self) -> str | None:
        last = self._last
        return last.open_type_name if last else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._event_attributes(self._last)


class PersonCountSensor(_BaseLockSensor):
    """state = 最近一次通行的人数（估算，见 person_count_source）。"""

    _attr_translation_key = "person_count"
    _attr_icon = "mdi:account-group"
    _attr_native_unit_of_measurement = "人"

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.client.device_serial}_person_count"

    @property
    def native_value(self) -> int | None:
        last = self._last
        return last.person_count if last else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = self._event_attributes(self._last)
        last = self._last
        if last is not None:
            attrs["source_note"] = {
                "customer_info": "设备透传字段上报的精确人数",
                "face_ids": "按聚合窗口内识别到的不同人脸数推算",
                "default": "无可用人数信息，按 1 人计",
            }.get(last.person_count_source, last.person_count_source)
        return attrs


class TodayOpenCountSensor(_BaseLockSensor):
    """state = 今日开门次数。"""

    _attr_translation_key = "today_open_count"
    _attr_icon = "mdi:door-open"
    # 今日次数每天归零，不能用 TOTAL_INCREASING（会被统计模块当成异常 reset）
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "次"

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.client.device_serial}_today_open_count"

    @property
    def native_value(self) -> int:
        data: LockData | None = self.coordinator.data
        return data.today_count if data else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if data is None:
            return {}
        last_close = data.last_close

        def _brief(ev: DoorEvent) -> dict[str, Any]:
            return {
                "open_time": ev.open_time_iso,
                "user_name": ev.user_name,
                "open_type_name": ev.open_type_name,
                "person_count": ev.person_count,
                "person_count_source": ev.person_count_source,
                "snapshot_url": ev.snapshot_url,
                "snapshot_source": ev.snapshot_source,
                "clip_url": ev.clip_url,
                "source": ev.source,
            }

        return {
            "total_cached_events": data.total_count,
            "today_close_count": data.today_close_count,
            "today_close_raw": data.today_close_raw,
            "last_close_time": last_close.open_time_iso if last_close else None,
            # 今日全部开门，供仪表盘展开成完整列表
            "today_events": [_brief(ev) for ev in data.today_events[:60]],
            "today_events_total": len(data.today_events),
            "recent_events": [_brief(ev) for ev in data.events[:30]],
            # 逐帧验收 0% 绿的开门录像池（coordinator 已核 size+md5），
            # 前端按 start_ms 与开门时刻就近匹配，绿片不在此列表里
            "sd_clips": self.coordinator.clean_clips,
            # 徘徊唤醒：前端卡片同屏切换，不必再引一个实体
            "loiter_events": [_brief_loiter(lo) for lo in data.loiter_events[:60]],
            "loiter_today_count": data.loiter_today_count,
            # 其它告警：只留痕，不进开门/徘徊表、不配视频（主人 11-18 要求单独存一份）
            "other_alarms": data.other_alarms[:60],
            "other_alarms_total": len(data.other_alarms),
            # App 通道健康快照（enabled / source / last_sync / last_error / 今日消息数 / 补行数）
            "app_msg_status": self.coordinator.app_msg_status,
            # v1.8.0 调用量统计：App 命中 vs 开放平台回退次数，用来直接核对降幅
            "api_call_stats": self.coordinator.api_call_stats,
            # v1.9.0 云封面补图统计：成功/未匹配/回落抓拍/回溯，以及待补条数
            "cover_stats": self.coordinator.cover_stats,
        }


class LoiterCountSensor(_BaseLockSensor):
    """state = 今日徘徊唤醒次数，属性带完整的徘徊列表（含录像地址）。"""

    _attr_translation_key = "loiter_count"
    _attr_icon = "mdi:motion-sensor"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "次"

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.client.device_serial}_loiter_count"

    @property
    def native_value(self) -> int:
        data: LockData | None = self.coordinator.data
        return data.loiter_today_count if data else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if data is None:
            return {}
        events = data.loiter_events
        last = data.loiter_last
        return {
            "last_loiter_time": last.time_iso if last else None,
            "last_loiter_clip": last.clip_url if last else None,
            "last_loiter_status": last.clip_status if last else None,
            "loiter_total": len(events),
            "loiter_today_count": data.loiter_today_count,
            "loiter_events": [_brief_loiter(lo) for lo in events[:60]],
        }


# ---------------------------------------------------------------- 健康度传感器
class _HealthSensor(_BaseLockSensor):
    """健康度传感器基类：统一取 coordinator.data.health。"""

    @property
    def _health(self):
        data: LockData | None = self.coordinator.data
        return data.health if data else None


class BatterySensor(_HealthSensor):
    """state = 门锁电池剩余电量（%）。"""

    _attr_translation_key = "battery"
    _attr_icon = "mdi:battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.client.device_serial}_battery"

    @property
    def native_value(self) -> int | None:
        health = self._health
        return health.battery_pct if health else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        health = self._health
        if health is None:
            return {}
        return {
            # 原始 multiPower 条目：Type 0=主电源 1=蓄电池
            "battery_items": health.battery_items,
            "spare_battery_pct": health.spare_battery_pct,
            "power_status": health.power_status,
            "power_status_name": health.power_status_name,
            "last_update": health.last_update,
            "last_error": health.last_error,
            "source": "萤石开放平台 /api/v3/keyValue/.../op?key=multiPower",
        }


class WifiSignalSensor(_HealthSensor):
    """state = 门锁 WiFi 信号强度（%），用于判断弱网。"""

    _attr_translation_key = "wifi_signal"
    _attr_icon = "mdi:wifi"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.client.device_serial}_wifi_signal"

    @property
    def native_value(self) -> int | None:
        health = self._health
        return health.signal_pct if health else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        health = self._health
        if health is None:
            return {}
        return {
            "ssid": health.ssid,
            "net_type": health.net_type,
            "local_address": health.local_address,
            "online": health.online,
            "last_update": health.last_update,
            "source": "萤石开放平台 /api/lapp/device/info → signal",
        }


class PowerStatusSensor(_HealthSensor):
    """state = 充电状态（未充电 / 充电中 / 已充满 / 无电池 / 电池异常）。"""

    _attr_translation_key = "power_status"
    _attr_icon = "mdi:power-plug"

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.client.device_serial}_power_status"

    @property
    def native_value(self) -> str | None:
        health = self._health
        return health.power_status_name if health else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        health = self._health
        if health is None:
            return {}
        return {"power_status_code": health.power_status}


class MembersSensor(_HealthSensor):
    """state = 门锁成员数，属性带每个成员的凭证数量与最近变更记录。"""

    _attr_translation_key = "members"
    _attr_icon = "mdi:account-multiple-check"
    _attr_native_unit_of_measurement = "人"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.client.device_serial}_members"

    @property
    def native_value(self) -> int | None:
        health = self._health
        return len(health.members) if health else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        health = self._health
        if health is None:
            return {}
        members = health.members
        return {
            "members": [m.to_dict() for m in members],
            "credential_total": sum(m.credential_total for m in members),
            "face_total": sum(m.face for m in members),
            "palm_total": sum(m.palm for m in members),
            "finger_total": sum(m.finger for m in members),
            "password_total": sum(m.password for m in members),
            "card_total": sum(m.card for m in members),
            "last_change": health.last_member_change,
            "recent_changes": health.member_changes[:20],
            "last_update": health.last_update,
            "source": "萤石开放平台 /api/lapp/keylock/user/list",
        }
