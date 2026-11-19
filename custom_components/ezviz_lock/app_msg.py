"""萤石 App 账号体系「统一消息列表」客户端 —— 第三条事件通道。

背景（2026-11-18 实测）
----------------------
1. 开放平台 ``keylock/open/list`` 会周期性停更（11-16 中午、11-17 21:05），
   ``alarm/list`` 是第二条通道；本文件提供第三套认证体系下的读取入口。
2. ``GET /v3/unifiedmsg/list?stype=9904,2701`` 是手机萤石云视频 App 的「全部消息」。
   实测 11-17 与开放平台 alarm/list 的告警时间集合在分钟级完全一致（互不缺项），
   App 组合查询多带一条门铃呼叫(20002)。两边**端点与凭据独立、数据表现同源**。
3. 所以它只能救「开放平台那扇门断供」，救不了「门锁压根没把事件上报云端」。
   今天 8:03 / 9:02 两段本地 SD 录像在两条通道里都没有记录，即是明证。

凭据与终端绑定
--------------
配置文件 ``/config/ezviz_lock_app.json``：存 ``password_md5`` 而不是明文密码，
登录接口本来就要求传 md5；存 ``feature_code`` 这个**已绑定过终端的固定字符串**。

关键坑：pyezvizapi 的 FEATURE_CODE = md5(本机 MAC)。若集成在 HAOS 上按 HAOS
自己的 MAC 现算，会与已绑定的终端不匹配，触发 ``6002 硬件特征码校验失败`` 并
要求短信验证码 —— 自动化无法完成。因此必须把本机测得的固定值写进配置复用。

本模块不依赖 homeassistant，可单独在 PC 上用 aiohttp 直接跑通验证。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

APP_MSG_HOST = "https://api.ys7.com"
APP_MSG_CONFIG_FILE = "ezviz_lock_app.json"
APP_MSG_STYPE = "9904,2701"      # 9904=门锁告警  2701=门铃呼叫
APP_MSG_LIMIT = 50
APP_MSG_TIMEOUT = 20
APP_MSG_USER_AGENT = "VideoGo/7.7.3 (iPhone; iOS 26.5; Scale/3.00)"

LOGIN_PATH = "/v3/users/login/v3"
REFRESH_PATH = "/v3/apigateway/login"
MESSAGE_PATH = "/v3/unifiedmsg/list"
# v1.8.0：App 体系替代开放平台的两条主数据源（实测 2026-11-18）
# openEvents 的 gmtOpen 与开放平台 keylock/open/list 的 openTime 逐毫秒一致，
# 直接当 DoorRecord.open_time 用；records 对应 video/by/time 的 SD 录像段检索。
OPEN_EVENTS_PATH = "/v3/doorlocks/home/openEvents"
STREAMING_RECORDS_PATH = "/v3/streaming/records"
# v1.9.0：云存储录像列表 —— 每条片段自带 coverPic（带 ticket 的完整 URL），
# 可直接下载成 JPEG，不消耗开放平台配额、也不需要设备在线（实测 2026-11-19）。
CLOUD_VIDEOS_PATH = "/v3/clouds/videos/list"
CLOUD_CLIPS_LIMIT = 20          # 服务端硬上限：limit=50/100/200 都只回 20 条，且无分页参数
OPEN_EVENTS_NUM = 50            # 实测 num=10 有值、num=50 有时 data=null，取 50 覆盖全天再回退兜底
RECORDS_SIZE = 50


class AppMsgError(Exception):
    """App 消息通道不可用（调用方必须吞掉，不能让集成变 unavailable）。"""


class AppMsgConfigError(AppMsgError):
    """配置文件缺失/损坏/字段不全。"""


class AppDataUnavailable(AppMsgError):
    """App 数据端点临时无数据（data=null/设备网络异常）——调用方据此回退开放平台。"""


class AppMsgAuthError(AppMsgError):
    """HTTP 层鉴权失效（401/403）—— 调用方据此重登并重试一次。"""


@dataclass(slots=True)
class AppMessage:
    """一条归一化后的统一消息。"""

    msg_id: str
    alarm_type: int
    time_ms: int                 # 消息生成时刻
    event_time_ms: int           # 对外事件时刻 = time_ms（实测与开放平台 alarmTime 逐条 0 秒偏差）
    title: str
    pic_url: str | None
    customer_info: str | None    # 明文 JSON 字符串；_decode_customer_info 兼容
    is_video: bool
    rec_state: int
    pre_time: int
    delay_time: int
    sub_type: int
    device_serial: str
    channel: int


@dataclass(slots=True)
class AppOpenRecord:
    """App openEvents 的一条开门/关门记录。

    实测字段（2026-11-18）：gmtOpen(ms) / openType / userName / alarmId。
    gmtOpen 与开放平台 keylock/open/list 的 openTime 逐毫秒一致，可直接复用。
    """

    user_name: str
    open_type: int
    open_time: int          # 毫秒时间戳（gmtOpen）
    alarm_id: str | None = None
    device_serial: str = ""


@dataclass(slots=True)
class AppRecordSegment:
    """App streaming/records 的一段 SD 录像。

    实测字段：begin/end 为本地时间字符串 "2026-11-18T08:03:06"，type=ALARM，
    channelType=D。对应开放平台 video/by/time 的存储文件。
    """

    begin_local: str        # "YYYY-MM-DDTHH:MM:SS"
    end_local: str
    rec_type: str
    channel_type: str = "D"


@dataclass(slots=True)
class AppCloudClip:
    """云存储录像的一段片段（``/v3/clouds/videos/list``）。

    实测（2026-11-19）返回 ``videos[]``（**注意字段名是 videos 不是 data**），字段：
    ``startTime``/``stopTime`` 为设备本地时间字符串；``coverPic`` 是**带 ticket 的完整
    URL**，直接 GET 即得事件首帧 JPEG；``fileIndex`` 在多段之间会重复（一个底层文件被
    切成多段），因此唯一标识必须用 ``(file_index, start_ms)``，不能只用 fileIndex。
    """

    start_ms: int           # 片段起点（毫秒，按 UTC+8 解释）
    stop_ms: int            # 片段终点
    video_long_ms: int
    file_size: int
    cover_url: str          # coverPic 原样（内含 ticket，禁止重新 urlencode）
    file_index: str = ""
    cloud_type: int = 0
    expire_time: str = ""
    channel_no: int = 1

    @property
    def key(self) -> str:
        return f"{self.file_index}@{self.start_ms}"


def _parse_local_ms(text: str) -> int:
    """萤石返回的 "YYYY-MM-DD HH:MM:SS" 是**设备本地时间（UTC+8）**。

    不用 ``datetime.timestamp()``：那会把结果绑到进程本地时区，一旦 HAOS 时区
    被改（或容器是 UTC）整张时间表就错位 8 小时。这里显式按 UTC+8 换算。
    """
    s = (text or "").strip()
    if not s:
        return 0
    try:
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    epoch = datetime(1970, 1, 1)
    return int((dt - epoch).total_seconds() * 1000) - 8 * 3600 * 1000


def load_app_config(config_root: Path) -> dict[str, Any] | None:
    """读取可选配置文件。文件不存在 / enabled=false 返回 None（通道静默关闭）。

    这是同步函数：调用方须用 ``hass.async_add_executor_job`` 放到线程池，
    不要在事件循环里直接读磁盘。
    """
    path = Path(config_root) / APP_MSG_CONFIG_FILE
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise AppMsgConfigError(f"读取 {path} 失败：{err}") from err
    if not isinstance(raw, dict):
        raise AppMsgConfigError(f"{path} 顶层必须是 JSON 对象")
    if not raw.get("enabled", True):
        return None
    for key in ("account", "password_md5", "feature_code"):
        if not raw.get(key):
            raise AppMsgConfigError(f"{path} 缺少字段 {key}")
    return raw


def _login_headers(feature_code: str) -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Host": "api.ys7.com",
        "User-Agent": APP_MSG_USER_AGENT,
        "appId": "ys7",
        "clientNo": "",
        "clientType": "1",
        "clientVersion": "",
        "featureCode": feature_code,
        "sessionId": "",
        "ssid": "",
    }


def _session_headers(feature_code: str, session_id: str) -> dict[str, str]:
    return {
        "Accept": "*/*",
        "User-Agent": APP_MSG_USER_AGENT,
        "featureCode": feature_code,
        "clientType": "1",
        "osVersion": "",
        "clientVersion": "",
        "netType": "WIFI",
        "ssid": "",
        "appId": "ys7",
        "clientNo": "",
        "sessionId": session_id,
    }


def _parse_message(raw: dict[str, Any]) -> AppMessage | None:
    """把一条原始消息归一化；结构异常返回 None，绝不抛异常拖垮整批。"""
    try:
        ext = raw.get("ext") or {}
        if not isinstance(ext, dict):
            ext = {}
        msg_id = str(raw.get("msgId") or "").strip()
        if not msg_id:
            return None
        time_ms = int(raw.get("time") or 0)
        if time_ms <= 0:
            return None
        # 实测 2026-11-18（11-17 全量 7 条开门消息）：
        #   开放平台 alarmTime 与统一消息 time 逐条 **0.0 秒** 偏差；
        #   ext.alarmStartTime 则系统性晚 4.1~17.8 秒，会让 ±20s 去重窗口只剩 2 秒余量。
        # 事件时刻统一用 time，才能与 keylock / alarm 两条链路对齐、正确认领 SD 录像。
        event_time_ms = time_ms
        alarm_type = int(ext.get("alarmType") or raw.get("sysCode") or 0)
        return AppMessage(
            msg_id=msg_id,
            alarm_type=alarm_type,
            time_ms=time_ms,
            event_time_ms=event_time_ms,
            title=str(raw.get("title") or ""),
            pic_url=(str(raw["pic"]) if raw.get("pic") else None),
            customer_info=(str(ext["customerInfo"]) if ext.get("customerInfo") else None),
            is_video=bool(ext.get("isVideo")),
            rec_state=int(ext.get("recState") or 0),
            pre_time=int(ext.get("preTime") or 0),
            delay_time=int(ext.get("delayTime") or 0),
            sub_type=int(raw.get("subType") or 0),
            device_serial=str(raw.get("deviceSerial") or ""),
            channel=int(raw.get("channel") or 1),
        )
    except (TypeError, ValueError, KeyError):
        return None


class EzvizAppMsgClient:
    """App 账号统一消息客户端。session 只在内存，进程重启后重新登录（免验证码）。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        account: str,
        password_md5: str,
        feature_code: str,
        device_serial: str,
    ) -> None:
        self._session = session
        self._account = account
        self._password_md5 = password_md5
        self._feature_code = feature_code
        self._device_serial = device_serial.upper()
        self._session_id = ""
        self._rf_session_id = ""
        self._lock = asyncio.Lock()

    @property
    def logged_in(self) -> bool:
        return bool(self._session_id)

    async def async_login(self) -> None:
        """用账号 + md5 密码 + 固定 featureCode 登录，拿 sessionId / rfSessionId。"""
        payload = {
            "account": self._account,
            "password": self._password_md5,
            "featureCode": self._feature_code,
            "msgType": "0",
            "bizType": "",
            "cuName": "SGFzc2lv",
        }
        try:
            data = await self._request(
                "POST",
                APP_MSG_HOST + LOGIN_PATH,
                headers=_login_headers(self._feature_code),
                data=payload,
            )
        except AppMsgError:
            raise
        meta = data.get("meta") or {}
        if int(meta.get("code") or 0) != 200:
            code = meta.get("code")
            if str(code) == "6002":
                raise AppMsgError(
                    "6002 硬件特征码校验失败：配置里的 feature_code 与已绑定终端不匹配，"
                    "需要短信验证码人工绑定，自动化无法继续"
                )
            raise AppMsgError(f"App 账号登录失败 meta={meta}")
        info = data.get("sessionInfo") or {}
        sid = str(info.get("sessionId") or "")
        if not sid:
            raise AppMsgError("App 账号登录成功但未返回 sessionId")
        self._session_id = sid
        self._rf_session_id = str(info.get("rfSessionId") or "")
        _LOGGER.info(
            "App 消息通道登录成功（terminalBinded=%s）",
            (data.get("terminalStatus") or {}).get("terminalBinded"),
        )

    async def async_refresh(self) -> bool:
        """用 rfSessionId 续期；返回 False 表示需要重新登录。"""
        if not self._rf_session_id:
            return False
        try:
            data = await self._request(
                "PUT",
                APP_MSG_HOST + REFRESH_PATH,
                headers=_session_headers(self._feature_code, self._session_id),
                data={
                    "refreshSessionId": self._rf_session_id,
                    "featureCode": self._feature_code,
                },
            )
        except AppMsgError:
            return False
        meta = data.get("meta") or {}
        if int(meta.get("code") or 0) != 200:
            return False
        info = data.get("sessionInfo") or {}
        sid = str(info.get("sessionId") or "")
        if not sid:
            return False
        self._session_id = sid
        self._rf_session_id = str(info.get("refreshSessionId") or self._rf_session_id)
        return True

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=APP_MSG_TIMEOUT)
        try:
            async with self._session.request(
                method, url, headers=headers, data=data, params=params, timeout=timeout
            ) as resp:
                text = await resp.text()
                if resp.status in (401, 403):
                    # 实测 2026-11-18：session 失效时萤石返回 HTTP 401 + meta.code=401，
                    # 而不是 200 包一个错误码。必须单独识别，否则重登重试永远不触发。
                    raise AppMsgAuthError(f"HTTP {resp.status} {url} -> {text[:160]}")
                if resp.status != 200:
                    raise AppMsgError(f"HTTP {resp.status} {url} -> {text[:160]}")
                parsed = json.loads(text)
                if not isinstance(parsed, dict):
                    raise AppMsgError(f"{url} 返回不是 JSON 对象")
                return parsed
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise AppMsgError(f"请求 {url} 失败：{err}") from err

    async def async_get_messages(self, day: str) -> list[AppMessage]:
        """拉某一天（YYYYMMDD）的统一消息；会话失效自动重登并重试一次。"""
        async with self._lock:
            if not self._session_id:
                await self.async_login()

        params = {
            "serials": self._device_serial,
            "stype": APP_MSG_STYPE,
            "limit": APP_MSG_LIMIT,
            "date": day,
            "endTime": "",
        }

        async def _fetch() -> dict[str, Any]:
            return await self._request(
                "GET",
                APP_MSG_HOST + MESSAGE_PATH,
                headers=_session_headers(self._feature_code, self._session_id),
                params=params,
            )

        try:
            data = await _fetch()
        except AppMsgAuthError:
            data = None
        code = int(((data or {}).get("meta") or {}).get("code") or 0)
        if data is None or code != 200:
            # 会话过期：优先续期（国内实测该端点不可用），续不上就重登，然后只重试一次。
            # 终端已绑定过，重登免短信验证码。
            async with self._lock:
                if not await self.async_refresh():
                    await self.async_login()
            try:
                data = await _fetch()
            except AppMsgAuthError as err:
                raise AppMsgError(f"重登后仍鉴权失败：{err}") from err
            code = int((data.get("meta") or {}).get("code") or 0)
            if code != 200:
                raise AppMsgError(f"统一消息列表失败 meta={data.get('meta')}")

        rows = data.get("message") or []
        if not isinstance(rows, list):
            return []
        out: list[AppMessage] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            msg = _parse_message(raw)
            if msg is not None:
                out.append(msg)
        return out

    # ------------------------------------------------------------------ v1.8.0 主数据源
    async def _authed_get(
        self, path: str, params: dict[str, Any], *, label: str
    ) -> dict[str, Any]:
        """带登录/自动重登的 GET，返回整包 JSON（含 meta）。

        与 async_get_messages 同一套会话策略：session 只在内存，401 时重登重试一次。
        meta.code 非 200 不在这里抛，交给调用方判定（data=null 要能触发回退）。
        """
        async with self._lock:
            if not self._session_id:
                await self.async_login()

        async def _fetch() -> dict[str, Any]:
            return await self._request(
                "GET",
                APP_MSG_HOST + path,
                headers=_session_headers(self._feature_code, self._session_id),
                params=params,
            )

        try:
            data = await _fetch()
        except AppMsgAuthError:
            data = None
        if data is None:
            # HTTP 层鉴权失效 → 重登（终端已绑定，免短信）后只重试一次
            async with self._lock:
                if not await self.async_refresh():
                    await self.async_login()
            try:
                data = await _fetch()
            except AppMsgAuthError as err:
                raise AppMsgError(f"{label} 重登后仍鉴权失败：{err}") from err
        return data

    async def async_get_open_events(self, num: int = OPEN_EVENTS_NUM) -> list[AppOpenRecord]:
        """App 开门记录：对应开放平台 keylock/open/list。

        实测结构（2026-11-18）::

            data.openEvents[0].openEvents[] = [
              {"gmtOpen": 1794920712000, "openType": 9, "userName": "", ...},
              ...
            ]

        gmtOpen 是毫秒时间戳，与开放平台 openTime 逐毫秒一致。
        已知坑：data 会间歇性返回 null（code=200 但无数据）——此时抛
        AppDataUnavailable，让调用方回退开放平台，绝不当成「今天没开门」。
        """
        data = await self._authed_get(
            OPEN_EVENTS_PATH,
            {"deviceSerials": self._device_serial, "num": str(num)},
            label="openEvents",
        )
        meta = data.get("meta") or {}
        code = int(meta.get("code") or 0)
        if code != 200:
            raise AppDataUnavailable(f"openEvents meta={meta}")
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise AppDataUnavailable("openEvents data=null（临时无数据，回退开放平台）")
        groups = payload.get("openEvents")
        if not isinstance(groups, list) or not groups:
            raise AppDataUnavailable("openEvents 无分组（回退开放平台）")
        rows: list[dict[str, Any]] = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("openEvents"), list):
                rows.extend(x for x in group["openEvents"] if isinstance(x, dict))
        out: list[AppOpenRecord] = []
        for item in rows:
            try:
                ms = int(item.get("gmtOpen") or 0)
            except (TypeError, ValueError):
                continue
            if ms <= 0:
                continue
            out.append(
                AppOpenRecord(
                    user_name=str(item.get("userName") or ""),
                    open_type=int(item.get("openType") or 0),
                    open_time=ms,
                    alarm_id=(str(item["alarmId"]) if item.get("alarmId") else None),
                    device_serial=str(item.get("deviceSerial") or self._device_serial),
                )
            )
        if not out:
            raise AppDataUnavailable("openEvents 解析后无有效记录（回退开放平台）")
        out.sort(key=lambda r: r.open_time, reverse=True)
        return out

    async def async_get_records(
        self, start_local: str, stop_local: str, size: int = RECORDS_SIZE
    ) -> list[AppRecordSegment]:
        """App SD 录像检索：对应开放平台 video/by/time。

        start_local/stop_local 为本地时间字符串 "YYYY-MM-DD HH:MM:SS"。
        实测返回 data.records[]，含 begin/end（"YYYY-MM-DDTHH:MM:SS"）/type/channelType。
        该端点对设备网络状态敏感（实测多次 2009/2004），失败抛 AppDataUnavailable。
        """
        data = await self._authed_get(
            STREAMING_RECORDS_PATH,
            {
                "deviceSerial": self._device_serial,
                "channelNo": "1",
                "channelSerial": "",
                "startTime": start_local,
                "stopTime": stop_local,
                "size": str(size),
            },
            label="streaming/records",
        )
        meta = data.get("meta") or {}
        code = int(meta.get("code") or 0)
        if code != 200:
            raise AppDataUnavailable(f"streaming/records meta={meta}")
        payload = data.get("data")
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise AppDataUnavailable("streaming/records 无 records（临时无数据）")
        out: list[AppRecordSegment] = []
        for item in payload["records"]:
            if not isinstance(item, dict):
                continue
            begin = str(item.get("begin") or "")
            if not begin:
                continue
            out.append(
                AppRecordSegment(
                    begin_local=begin,
                    end_local=str(item.get("end") or ""),
                    rec_type=str(item.get("type") or ""),
                    channel_type=str(item.get("channelType") or "D"),
                )
            )
        return out

    async def async_get_cloud_clips(self, limit: int = CLOUD_CLIPS_LIMIT) -> list[AppCloudClip]:
        """云存储录像片段列表 —— 事件配图与回溯补图的免费来源。

        这是**云存储**（cloudType=4，购买套餐后保留 N 天）的片段，不是 SD 卡。
        实测要点（2026-11-19）：
          * 返回字段是 ``videos[]``，不是 ``data``；
          * 服务端硬上限 20 条，``limit`` 传再大也只回 20，``offset``/``page`` 无效；
          * ``coverPic`` 是带 ticket 的完整 URL，**必须原样使用** —— 重新
            urlencode 会破坏 ticket，服务端回 ``Check ticket err``；
          * 不需要设备在线，也不需要唤醒设备（走 alicloud HTTP）。

        失败抛 ``AppDataUnavailable``（调用方降级即可，不能拖垮轮询）。
        """
        data = await self._authed_get(
            CLOUD_VIDEOS_PATH,
            {
                "deviceSerial": self._device_serial,
                "channelNo": "1",
                "limit": str(limit),
                "videoType": "2",
                "supportMultiChannelSharedService": "0",
            },
            label="clouds/videos/list",
        )
        meta = data.get("meta") or {}
        code = int(meta.get("code") or 0)
        if code != 200:
            raise AppDataUnavailable(f"clouds/videos/list meta={meta}")
        raw = data.get("videos")
        if raw is None:
            # 没有云存储套餐 / 当天无录像时服务端可能给 null
            return []
        if not isinstance(raw, list):
            raise AppDataUnavailable("clouds/videos/list videos 非列表")

        out: list[AppCloudClip] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            start_ms = _parse_local_ms(str(item.get("startTime") or ""))
            if not start_ms:
                continue
            stop_ms = _parse_local_ms(str(item.get("stopTime") or ""))
            try:
                video_long = int(item.get("videoLong") or 0)
            except (TypeError, ValueError):
                video_long = 0
            if video_long <= 0 and stop_ms > start_ms:
                video_long = stop_ms - start_ms
            try:
                file_size = int(item.get("fileSize") or 0)
            except (TypeError, ValueError):
                file_size = 0
            try:
                cloud_type = int(item.get("cloudType") or 0)
            except (TypeError, ValueError):
                cloud_type = 0
            out.append(
                AppCloudClip(
                    start_ms=start_ms,
                    stop_ms=stop_ms or (start_ms + video_long),
                    video_long_ms=video_long,
                    file_size=file_size,
                    cover_url=str(item.get("coverPic") or ""),
                    file_index=str(item.get("fileIndex") or ""),
                    cloud_type=cloud_type,
                    expire_time=str(item.get("expireTime") or ""),
                    channel_no=int(item.get("channelNo") or 1),
                )
            )
        out.sort(key=lambda c: c.start_ms)
        return out
