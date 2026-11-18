"""图片实体：最近一次开门抓拍。

注意：这里**不继承 CoordinatorEntity**。
原因是 CoordinatorEntity.__init__ 内部会无参调用 super().__init__(...)，
而 ImageEntity.__init__ 需要 hass 参数，多重继承会在 MRO 上直接 TypeError。
改为单继承 ImageEntity + 手动挂 coordinator 监听，行为一致且稳定。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import DoorEvent, EzvizLockCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EzvizLockCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        [
            LastSnapshotImage(hass, coordinator, entry),
            PeekSnapshotImage(hass, coordinator, entry),
            PeekLowerSnapshotImage(hass, coordinator, entry),
        ]
    )


class LastSnapshotImage(ImageEntity):
    """展示最近一次开门时门锁上报的抓拍图。"""

    _attr_has_entity_name = True
    _attr_translation_key = "last_snapshot"
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EzvizLockCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(hass)
        self.coordinator = coordinator
        self._entry = entry
        self._cached_path: Path | None = None
        self._cached_bytes: bytes | None = None
        self._state_path: Path | None = None
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.client.device_serial)},
            "name": (coordinator.data.device_name if coordinator.data else None) or "EZVIZ 门锁",
            "manufacturer": "EZVIZ",
            "model": "Y3000FVX Plus",
            "serial_number": coordinator.client.device_serial,
        }

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.client.device_serial}_last_snapshot"

    @property
    def _last(self) -> DoorEvent | None:
        return self.coordinator.data.last if self.coordinator.data else None

    @property
    def image_last_updated(self) -> datetime | None:
        last = self._last
        if last is None:
            return None
        # 补拍图是在事件之后才拍的，用补拍时刻做版本号，前端才会重新拉图
        stamp = last.capture_ms or last.open_time_ms
        return dt_util.as_local(
            datetime.fromtimestamp(stamp / 1000, tz=dt_util.UTC)
        )

    async def async_image(self) -> bytes | None:
        last = self._last
        if last is None or not last.snapshot_path:
            return None

        path = Path(last.snapshot_path)
        if self._cached_path == path and self._cached_bytes is not None:
            return self._cached_bytes

        if not path.exists():
            _LOGGER.debug("抓拍图文件不存在：%s", path)
            return None

        try:
            data = await self.hass.async_add_executor_job(path.read_bytes)
        except OSError as err:
            _LOGGER.warning("读取抓拍图失败：%s", err)
            return None

        self._cached_path = path
        self._cached_bytes = data
        return data

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        last = self._last
        if last is None:
            return {}
        return {
            "snapshot_url": last.snapshot_url,
            # 前端/模板用：本地是否真的有一张可显示的抓拍图
            "has_snapshot": bool(last.snapshot_path) and Path(last.snapshot_path).exists(),
            "snapshot_path": last.snapshot_path,
            "snapshot_source": last.snapshot_source,
            "snapshot_delay_s": (
                max(0, (last.capture_ms - last.open_time_ms) // 1000)
                if last.capture_ms
                else None
            ),
            "clip_url": last.clip_url,
            "open_time": last.open_time_iso,
            "user_name": last.user_name,
            "open_type_name": last.open_type_name,
            "person_count": last.person_count,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        # 只有换了新图才让前端重新拉图，避免每次轮询都重复渲染
        last = self._last
        new_path = Path(last.snapshot_path) if last and last.snapshot_path else None
        if new_path != self._state_path:
            self._state_path = new_path
            self._cached_path = None
            self._cached_bytes = None
        self.async_write_ha_state()


class PeekSnapshotImage(ImageEntity):
    """「门前看一眼」的最近一张实时抓拍。

    与 LastSnapshotImage 的区别：这张图**不属于任何开门事件**，只由按钮/
    服务触发，用于随时确认门口情况（快递、访客、异响）。
    """

    _attr_has_entity_name = True
    _attr_translation_key = "peek_snapshot"
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EzvizLockCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(hass)
        self.coordinator = coordinator
        self._entry = entry
        self._cached_path: Path | None = None
        self._cached_bytes: bytes | None = None
        self._state_path: Path | None = None
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.client.device_serial)},
            "name": (coordinator.data.device_name if coordinator.data else None) or "EZVIZ 门锁",
            "manufacturer": "EZVIZ",
            "model": "Y3000FVX Plus",
            "serial_number": coordinator.client.device_serial,
        }

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.client.device_serial}_peek_snapshot"

    @property
    def _peek(self) -> dict | None:
        return self.coordinator.peek

    @property
    def image_last_updated(self) -> datetime | None:
        peek = self._peek
        if not peek or not peek.get("time"):
            return None
        parsed = dt_util.parse_datetime(str(peek["time"]))
        return dt_util.as_local(parsed) if parsed else None

    async def async_image(self) -> bytes | None:
        peek = self._peek
        if not peek or not peek.get("path"):
            return None
        path = Path(str(peek["path"]))
        if self._cached_path == path and self._cached_bytes is not None:
            return self._cached_bytes
        if not path.exists():
            return None
        try:
            data = await self.hass.async_add_executor_job(path.read_bytes)
        except OSError as err:
            _LOGGER.warning("读取门前快照失败：%s", err)
            return None
        self._cached_path = path
        self._cached_bytes = data
        return data

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        peek = self._peek
        if not peek:
            return {"has_snapshot": False}
        lower = peek.get("lower") or {}
        upper = peek.get("upper") or {}
        return {
            "has_snapshot": True,
            "snapshot_url": peek.get("url"),
            "snapshot_path": peek.get("path"),
            "peek_time": peek.get("time"),
            "mode": peek.get("mode") or "single",
            "source": peek.get("source"),
            "note": peek.get("note"),
            "upper_url": upper.get("url") or peek.get("url"),
            "upper_path": upper.get("path") or peek.get("path"),
            "lower_url": lower.get("url"),
            "lower_path": lower.get("path"),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        peek = self._peek
        new_path = Path(str(peek["path"])) if peek and peek.get("path") else None
        if new_path != self._state_path:
            self._state_path = new_path
            self._cached_path = None
            self._cached_bytes = None
        self.async_write_ha_state()


class PeekLowerSnapshotImage(ImageEntity):
    """「门前看一眼」的下摄画面：门口地面 / 脚垫 / 包裹（1920×1080 横屏）。

    双摄门锁的上摄看人脸、下摄看地面，平台抓拍接口只给上摄，这一路是从
    同一条实时流里按 NAL 奇偶拆出来的（见 peek_dual.py）。
    取流失败降级为单路时，本实体无图，属性 ``available`` 为假。
    """

    _attr_has_entity_name = True
    _attr_translation_key = "peek_lower_snapshot"
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EzvizLockCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(hass)
        self.coordinator = coordinator
        self._entry = entry
        self._cached_path: Path | None = None
        self._cached_bytes: bytes | None = None
        self._state_path: Path | None = None
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.client.device_serial)},
            "name": (coordinator.data.device_name if coordinator.data else None) or "EZVIZ 门锁",
            "manufacturer": "EZVIZ",
            "model": "Y3000FVX Plus",
            "serial_number": coordinator.client.device_serial,
        }

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.client.device_serial}_peek_lower_snapshot"

    @property
    def _peek(self) -> dict | None:
        return self.coordinator.peek

    @property
    def _lower(self) -> dict | None:
        peek = self._peek or {}
        return peek.get("lower") or None

    @property
    def image_last_updated(self) -> datetime | None:
        peek = self._peek
        if not peek or not peek.get("time") or not self._lower:
            return None
        parsed = dt_util.parse_datetime(str(peek["time"]))
        return dt_util.as_local(parsed) if parsed else None

    async def async_image(self) -> bytes | None:
        lower = self._lower
        if not lower or not lower.get("path"):
            return None
        path = Path(str(lower["path"]))
        if self._cached_path == path and self._cached_bytes is not None:
            return self._cached_bytes
        if not path.exists():
            return None
        try:
            data = await self.hass.async_add_executor_job(path.read_bytes)
        except OSError as err:
            _LOGGER.warning("读取下摄快照失败：%s", err)
            return None
        self._cached_path = path
        self._cached_bytes = data
        return data

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        lower = self._lower or {}
        peek = self._peek or {}
        return {
            "has_snapshot": bool(lower.get("path")),
            "snapshot_url": lower.get("url"),
            "snapshot_path": lower.get("path"),
            "peek_time": peek.get("time"),
            "mode": peek.get("mode") or "single",
            "note": peek.get("note"),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        lower = self._lower
        new_path = Path(str(lower["path"])) if lower and lower.get("path") else None
        if new_path != self._state_path:
            self._state_path = new_path
            self._cached_path = None
            self._cached_bytes = None
        self.async_write_ha_state()
