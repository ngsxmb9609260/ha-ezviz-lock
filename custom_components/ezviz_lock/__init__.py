"""ezviz_lock 集成入口。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import EzvizApiClient
from .const import (
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_DEVICE_SERIAL,
    DEFAULT_REGION_HOST,
    DOMAIN,
    FIRST_REFRESH_TIMEOUT_S,
    SERVICE_DELETE_LOITER,
    SERVICE_DOWNLOAD_CLIP,
    SERVICE_GET_PLAYBACK_URL,
    SERVICE_REFETCH_LOITER,
    SERVICE_REFRESH,
    SERVICE_PEEK,
    VERSION,
)
from .coordinator import DoorEvent, EzvizLockCoordinator
from .lan_watch import LanWatcher

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.IMAGE,
    Platform.EVENT,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
]

ATTR_OPEN_TIME = "open_time"
ATTR_INDEX = "index"


@dataclass
class EzvizLockRuntimeData:
    """ConfigEntry.runtime_data（HA 2024.6+ 推荐写法）。"""

    client: EzvizApiClient
    coordinator: EzvizLockCoordinator


EzvizLockConfigEntry = ConfigEntry[EzvizLockRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """域名级 setup：注册服务（与具体条目解耦）。"""
    hass.data.setdefault(DOMAIN, {})

    async def _resolve_event(call: ServiceCall) -> tuple[EzvizLockCoordinator, DoorEvent] | None:
        entry_id = call.data.get("config_entry_id")
        entries = [
            e for e in hass.config_entries.async_entries(DOMAIN)
            if entry_id in (None, e.entry_id) and getattr(e, "runtime_data", None)
        ]
        if not entries:
            _LOGGER.warning("没有可用的 ezviz_lock 配置条目")
            return None
        runtime: EzvizLockRuntimeData = entries[0].runtime_data
        coordinator = runtime.coordinator

        open_time = call.data.get(ATTR_OPEN_TIME)
        index = int(call.data.get(ATTR_INDEX, 0))
        events = coordinator.data.events if coordinator.data else []
        if open_time:
            for ev in events:
                if ev.open_time_iso == open_time or ev.uid == str(open_time):
                    return coordinator, ev
            return None
        if not events:
            await coordinator.async_refresh()
            events = coordinator.data.events if coordinator.data else []
        if not events:
            return None
        return coordinator, events[min(index, len(events) - 1)]

    async def handle_refresh(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            runtime = getattr(entry, "runtime_data", None)
            if runtime:
                await runtime.coordinator.async_refresh()

    async def handle_download_clip(call: ServiceCall) -> dict[str, Any]:
        resolved = await _resolve_event(call)
        if resolved is None:
            return {"url": None, "error": "找不到对应事件"}
        coordinator, event = resolved
        url = await coordinator.async_archive_clip(event)
        return {
            "url": url,
            "open_time": event.open_time_iso,
            "user_name": event.user_name,
        }

    async def handle_refetch_loiter(call: ServiceCall) -> dict[str, Any]:
        """把所有还没拿到录像的徘徊事件重新排一次取流（含已被判为占位图的）。"""
        queued = 0
        force = bool(call.data.get("force"))
        entry_id = call.data.get("config_entry_id")
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry_id and entry.entry_id != entry_id:
                continue
            runtime = getattr(entry, "runtime_data", None)
            if runtime:
                queued += await runtime.coordinator.async_refetch_loiter_clips(force=force)
        return {"queued": queued}

    async def handle_delete_loiter(call: ServiceCall) -> dict[str, Any]:
        """删一条徘徊记录及其录像/抓拍图，留墓碑防止被云端轮询拉回来。

        真正的删文件与持久化都在 coordinator 里做，这里只负责把服务调用
        路由到正确的配置条目（多台门锁时按 entry_id 过滤）。
        """
        uid = call.data.get("uid")
        time_ms = call.data.get("time_ms")
        if not uid and time_ms is None:
            return {"deleted": False, "error": "需要 uid 或 time_ms 之一"}
        entry_id = call.data.get("config_entry_id")
        result: dict[str, Any] = {"deleted": False}
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry_id and entry.entry_id != entry_id:
                continue
            runtime = getattr(entry, "runtime_data", None)
            if not runtime:
                continue
            res = await runtime.coordinator.async_delete_loiter(
                uid=str(uid) if uid else None,
                time_ms=int(time_ms) if time_ms is not None else None,
            )
            if res.get("deleted"):
                return res
            result = res
        return result

    async def handle_playback_url(call: ServiceCall) -> dict[str, Any]:
        resolved = await _resolve_event(call)
        if resolved is None:
            return {"url": None, "error": "找不到对应事件"}
        coordinator, event = resolved
        url = await coordinator.async_playback_url(event)
        return {"url": url, "open_time": event.open_time_iso, "user_name": event.user_name}

    async def handle_peek(call: ServiceCall) -> dict[str, Any]:
        """门前看一眼：立刻抓拍一张并返回可访问地址（供面板按钮直接用）。"""
        entry_id = call.data.get("config_entry_id")
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry_id and entry.entry_id != entry_id:
                continue
            runtime = getattr(entry, "runtime_data", None)
            if runtime:
                return await runtime.coordinator.async_peek()
        return {"url": None, "path": None, "time": None, "error": "没有可用的 ezviz_lock 配置条目"}

    base_schema = {
        vol.Optional(ATTR_OPEN_TIME): cv.string,
        vol.Optional(ATTR_INDEX, default=0): vol.Coerce(int),
        vol.Optional("config_entry_id"): cv.string,
    }

    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, handle_refresh,
        schema=vol.Schema({
            vol.Optional("config_entry_id"): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DOWNLOAD_CLIP, handle_download_clip,
        schema=vol.Schema(base_schema),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_GET_PLAYBACK_URL, handle_playback_url,
        schema=vol.Schema(base_schema),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REFETCH_LOITER, handle_refetch_loiter,
        schema=vol.Schema({
            vol.Optional("config_entry_id"): cv.string,
            vol.Optional("force"): cv.boolean,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_LOITER, handle_delete_loiter,
        schema=vol.Schema({
            vol.Optional("uid"): cv.string,
            vol.Optional("time_ms"): vol.Coerce(int),
            vol.Optional("config_entry_id"): cv.string,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PEEK, handle_peek,
        schema=vol.Schema({
            vol.Optional("config_entry_id"): cv.string,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EzvizLockConfigEntry) -> bool:
    """按配置条目建立客户端与协调器。"""
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)
    client = EzvizApiClient(
        session,
        entry.data[CONF_APP_KEY],
        entry.data[CONF_APP_SECRET],
        entry.data[CONF_DEVICE_SERIAL],
        entry.data.get("region_host", DEFAULT_REGION_HOST),
    )

    config_root = Path(hass.config.config_dir)
    coordinator = EzvizLockCoordinator(hass, entry, client, config_root)
    await coordinator.async_load_state()

    try:
        # 硬超时：首刷一旦卡住，整个 bootstrap 会跟着等，前端页面就打不开了。
        # 超时后不抛异常，让实体带着本地缓存先上线，下一轮轮询自然补齐。
        async with asyncio.timeout(FIRST_REFRESH_TIMEOUT_S):
            await coordinator.async_config_entry_first_refresh()
    except (TimeoutError, asyncio.TimeoutError):
        _LOGGER.warning(
            "首次取数超过 %s 秒，先用本地缓存启动，稍后由轮询自动补齐", FIRST_REFRESH_TIMEOUT_S
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("EZVIZ Lock 首次取数失败：%s", err)
        raise

    entry.runtime_data = EzvizLockRuntimeData(client=client, coordinator=coordinator)
    hass.data[DOMAIN][entry.entry_id] = entry.runtime_data

    # 局域网被动探活：只听门锁周期性发出的 ARP 探测帧，一个包都不发，
    # 因此不会唤醒设备。必须赶在平台 setup 之前挂上，实体一建好就能读到状态。
    watcher = LanWatcher(
        lambda: coordinator.data.health.local_address if coordinator.data else None,
        client.device_serial,
        config_root / "ezviz_lan_watch.txt",
    )
    coordinator.lan_watcher = watcher
    watcher.start()

    async def _async_stop_lan_watch() -> None:
        await hass.async_add_executor_job(watcher.stop)

    entry.async_on_unload(_async_stop_lan_watch)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # 实体就绪后再后台补图：启动阶段不做任何图片下载，否则会拖慢甚至卡死启动
    coordinator.async_schedule_late_fill()
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    _LOGGER.info(
        "EZVIZ Lock %s 已就绪（设备 %s，轮询 %s 秒）",
        VERSION, client.device_serial, coordinator.update_interval,
    )
    return True


async def _async_update_options(hass: HomeAssistant, entry: EzvizLockConfigEntry) -> None:
    """选项变更后重载条目。"""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EzvizLockConfigEntry) -> bool:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is not None:
        runtime.coordinator.async_cancel_clean_verify()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
