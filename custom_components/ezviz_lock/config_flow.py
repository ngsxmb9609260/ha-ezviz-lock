"""配置流程：AppKey / AppSecret / 设备序列号 → 校验 → 建立条目。"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EzvizApiClient, EzvizApiError, EzvizAuthError, EzvizPermissionError
from .const import (
    CONF_ALARM_TYPE_NAMES,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_AUTO_DOWNLOAD_CLIP,
    CONF_CAPTURE_ON_EVENT,
    CONF_CHANNEL_NO,
    CONF_DEVICE_SERIAL,
    CONF_MERGE_WINDOW,
    CONF_REGION_HOST,
    CONF_SCAN_INTERVAL,
    CONF_SNAPSHOT_RETENTION,
    CONF_USER_ALIASES,
    DEFAULT_AUTO_DOWNLOAD_CLIP,
    DEFAULT_CAPTURE_ON_EVENT,
    DEFAULT_CHANNEL_NO,
    DEFAULT_MERGE_WINDOW,
    DEFAULT_REGION_HOST,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SNAPSHOT_RETENTION,
    DOMAIN,
    REGION_HOSTS,
)

_LOGGER = logging.getLogger(__name__)

REGION_OPTIONS = {label: url for label, url in REGION_HOSTS.items()}

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_APP_KEY): cv.string,
        vol.Required(CONF_APP_SECRET): cv.string,
        vol.Required(CONF_DEVICE_SERIAL): cv.string,
        vol.Optional(CONF_CHANNEL_NO, default=DEFAULT_CHANNEL_NO): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=16)
        ),
        vol.Optional(CONF_REGION_HOST, default=DEFAULT_REGION_HOST): vol.In(
            list(REGION_HOSTS.values())
        ),
    }
)


class EzvizLockConfigFlow(ConfigFlow, domain=DOMAIN):
    """添加集成。"""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input[CONF_DEVICE_SERIAL] = user_input[CONF_DEVICE_SERIAL].strip().upper()
            user_input[CONF_APP_KEY] = user_input[CONF_APP_KEY].strip()
            user_input[CONF_APP_SECRET] = user_input[CONF_APP_SECRET].strip()

            await self.async_set_unique_id(user_input[CONF_DEVICE_SERIAL])
            self._abort_if_unique_id_configured()

            client = EzvizApiClient(
                async_get_clientsession(self.hass),
                user_input[CONF_APP_KEY],
                user_input[CONF_APP_SECRET],
                user_input[CONF_DEVICE_SERIAL],
                user_input.get(CONF_REGION_HOST, DEFAULT_REGION_HOST),
            )

            try:
                info = await client.async_get_device_info()
            except EzvizAuthError:
                errors["base"] = "invalid_auth"
            except EzvizPermissionError:
                errors["base"] = "device_not_owned"
            except EzvizApiError as err:
                _LOGGER.warning("校验连接失败：%s", err)
                errors["base"] = "cannot_connect"
            else:
                device_name = info.get("deviceName") or info.get("name") or user_input[CONF_DEVICE_SERIAL]
                # 探测开门记录接口是否已开通白名单（失败不作为阻断条件，给出提示）
                warn_key: str | None = None
                try:
                    await client.async_get_door_records(0, 1)
                except EzvizPermissionError:
                    warn_key = "no_keylock_permission"
                except EzvizApiError as err:
                    _LOGGER.info("开门记录接口探测失败：%s", err)
                    warn_key = "no_keylock_permission"

                if warn_key:
                    self.context["pending_data"] = user_input
                    return self.async_show_form(
                        step_id="confirm",
                        errors={},
                        description_placeholders={
                            "name": device_name,
                            "warning": (
                                "开门记录接口(keylock/open/list)返回权限错误。"
                                "该接口需要萤石侧为你的 AppKey 开通白名单，"
                                "未开通时只能拿到告警与抓拍图，拿不到「人员/开锁方式」。"
                                "可先完成添加，后续联系萤石开放平台支持开通。"
                            ),
                        },
                        data_schema=vol.Schema({}),
                        last_step=True,
                    )

                return self.async_create_entry(
                    title=f"{device_name} ({user_input[CONF_DEVICE_SERIAL]})",
                    data=user_input,
                )

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """开门记录接口权限不足时的二次确认页。"""
        if user_input is not None:
            data = self.context.get("pending_data") or {}
            return self.async_create_entry(
                title=f"EZVIZ Lock ({data.get(CONF_DEVICE_SERIAL, 'lock')})",
                data=data,
            )
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EzvizLockOptionsFlow(config_entry)


class EzvizLockOptionsFlow(OptionsFlow):
    """选项：轮询间隔、聚合窗口、保留天数、告警类型名称映射。"""

    def __init__(self, config_entry: ConfigEntry) -> None:
        # HA 2024.11+ 由框架注入只读 property，不允许赋值；更早版本需自行传入。
        try:
            self.config_entry = config_entry
        except AttributeError:
            self._legacy_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        data = self.config_entry.data

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
                vol.Optional(
                    CONF_MERGE_WINDOW,
                    default=options.get(CONF_MERGE_WINDOW, DEFAULT_MERGE_WINDOW),
                ): vol.All(vol.Coerce(int), vol.Range(min=3, max=120)),
                vol.Optional(
                    CONF_SNAPSHOT_RETENTION,
                    default=options.get(CONF_SNAPSHOT_RETENTION, DEFAULT_SNAPSHOT_RETENTION),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=3650)),
                vol.Optional(
                    CONF_CHANNEL_NO,
                    default=options.get(CONF_CHANNEL_NO, data.get(CONF_CHANNEL_NO, DEFAULT_CHANNEL_NO)),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=16)),
                vol.Optional(
                    CONF_CAPTURE_ON_EVENT,
                    default=options.get(CONF_CAPTURE_ON_EVENT, DEFAULT_CAPTURE_ON_EVENT),
                ): cv.boolean,
                vol.Optional(
                    CONF_AUTO_DOWNLOAD_CLIP,
                    default=options.get(CONF_AUTO_DOWNLOAD_CLIP, DEFAULT_AUTO_DOWNLOAD_CLIP),
                ): cv.boolean,
                vol.Optional(
                    CONF_ALARM_TYPE_NAMES,
                    default=options.get(CONF_ALARM_TYPE_NAMES, ""),
                    description={"suggested_value": options.get(CONF_ALARM_TYPE_NAMES, "")},
                ): cv.string,
                vol.Optional(
                    CONF_USER_ALIASES,
                    default=options.get(CONF_USER_ALIASES, ""),
                    description={"suggested_value": options.get(CONF_USER_ALIASES, "")},
                ): cv.string,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
