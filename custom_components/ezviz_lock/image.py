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
    async_add_entities([LastSnapshotImage(hass, coordinator, entry)])


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
