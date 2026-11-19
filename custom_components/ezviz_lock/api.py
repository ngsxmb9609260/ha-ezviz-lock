"""萤石开放平台 API 异步客户端。

设计要点：
1. accessToken 有效期 7 天，本地提前 1 小时刷新，避免轮询中途掉鉴权；
2. 所有请求带超时，遇到 token 失效(10002)自动重取并重试一次，其余错误直接抛出；
3. 萤石不同接口/固件的 data 结构不一致（数组 / 对象 / 分页），统一在此层归一化，
   上层只拿到结构稳定的 dataclass。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import (
    CODE_APP_KEY_INVALID,
    CODE_DEVICE_NOT_OWNED,
    CODE_NO_PERMISSION,
    CODE_OK,
    CODE_TOKEN_EXPIRED,
    LOITER_CLIP_IDLE_S,
    LOITER_CLIP_MAX_BYTES,
    LOITER_CLIP_TOTAL_S,
    PLAYBACK_PROTOCOL_FLV,
    LIVE_PROTOCOL_HLS,
    LIVE_PROTOCOL_FLV,
    LIVE_TYPE_PREVIEW,
    PLAYBACK_TYPE_LOCAL,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20
TOKEN_REFRESH_MARGIN = 3600  # 提前 1 小时刷新


class EzvizApiError(Exception):
    """接口调用失败。"""


class EzvizAuthError(EzvizApiError):
    """鉴权失败，需要用户重新检查 AppKey / AppSecret。"""


class EzvizPermissionError(EzvizApiError):
    """设备不在账号下，或接口未开通白名单。"""


@dataclass(slots=True)
class DoorRecord:
    """一条开门记录（keylock/open/list）。"""

    user_name: str
    open_type: int
    open_time: int  # 毫秒时间戳

    @property
    def uid(self) -> str:
        """去重主键：**按秒归一化**后「同方式同人员」视为同一条。

        v1.8.1 修正（实测 2026-11-19）：三条链路上报的时间精度不一致——
          * keylock/open/list 是整秒（毫秒位恒为 000，如 1795047001000）
          * App unifiedmsg 是真实毫秒（如 1795047001602）
          * alarm/list 的开门告警同样是毫秒
        同一次开门因此会得到两个只差几百毫秒、却完全不同的主键，
        `_seen` / `known` 的精确匹配全都挡不住，页面上就出现两条相同的记录。
        统一截断到秒即可对齐；同方式同人员的两次真实开门物理上不可能落在同一秒内
        （中间必须经历关门），所以秒级粒度不会误合并。
        """
        return f"{self.open_time // 1000}|{self.open_type}|{self.user_name}"


@dataclass(slots=True)
class AlarmRecord:
    """一条告警消息（alarm/list）。"""

    alarm_id: str
    alarm_type: int
    alarm_time: int  # 毫秒时间戳
    pic_url: str | None = None
    channel_no: int = 1
    rec_state: int = 0
    pre_time: int = 0
    delay_time: int = 0
    alarm_name: str | None = None
    customer_type: str | None = None
    customer_info: str | None = None

    @property
    def clip_start_ms(self) -> int:
        return self.alarm_time - max(self.pre_time, 0) * 1000

    @property
    def clip_end_ms(self) -> int:
        return self.alarm_time + max(self.delay_time, 0) * 1000

    @property
    def extra(self) -> dict[str, Any]:
        """解析 / 解析透传字段 customerInfo（不同固件里可能是 JSON 字符串）。"""
        if not self.customer_info:
            return {}
        try:
            parsed = json.loads(self.customer_info)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}


@dataclass(slots=True)
class StorageFile:
    """一段可回放录像（video/by/time）。"""

    start_ms: int
    end_ms: int
    file_type: int = 0


@dataclass
class _Token:
    value: str = ""
    expires_at: float = 0.0  # unix 秒

    @property
    def valid(self) -> bool:
        return bool(self.value) and self.expires_at - TOKEN_REFRESH_MARGIN > time.time()


class EzvizApiClient:
    """一个设备序列号对应一个客户端实例。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        app_key: str,
        app_secret: str,
        device_serial: str,
        base_url: str,
    ) -> None:
        self._session = session
        self._app_key = app_key
        self._app_secret = app_secret
        self._device_serial = device_serial.upper()
        self._base_url = base_url.rstrip("/")
        self._token = _Token()
        self._token_lock = asyncio.Lock()

    @property
    def device_serial(self) -> str:
        return self._device_serial

    # ------------------------------------------------------------------ token
    async def async_get_token(self, force: bool = False) -> str:
        if self._token.valid and not force:
            return self._token.value

        async with self._token_lock:
            if self._token.valid and not force:
                return self._token.value

            payload = await self._post(
                "/api/lapp/token/get",
                {"appKey": self._app_key, "appSecret": self._app_secret},
                with_token=False,
            )
            data = payload.get("data") or {}
            token = data.get("accessToken")
            if not token:
                raise EzvizAuthError("token/get 未返回 accessToken，请检查 AppKey/AppSecret")

            expire_ms = data.get("expireTime") or 0
            # expireTime 为毫秒时间戳；缺失时按官方 7 天有效期保守估计
            self._token = _Token(
                value=token,
                expires_at=(expire_ms / 1000) if expire_ms else time.time() + 6 * 86400,
            )
            _LOGGER.debug("萤石 accessToken 获取成功，剩余有效期约 %.1f 小时",
                          (self._token.expires_at - time.time()) / 3600)
            return token

    # ------------------------------------------------------------------ 基础请求
    async def _post(
        self,
        path: str,
        data: dict[str, Any],
        *,
        with_token: bool = True,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        body = dict(data)
        if with_token:
            body["accessToken"] = await self.async_get_token()

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.post(url, data=body, headers=headers or {}) as resp:
                    if resp.status != 200:
                        raise EzvizApiError(f"{path} HTTP {resp.status}")
                    payload = await resp.json(content_type=None)
        except asyncio.TimeoutError as err:
            raise EzvizApiError(f"{path} 请求超时（{REQUEST_TIMEOUT}s）") from err
        except aiohttp.ClientError as err:
            raise EzvizApiError(f"{path} 网络错误：{err}") from err

        code = str(payload.get("code", ""))
        if code == CODE_OK:
            return payload

        if code in CODE_TOKEN_EXPIRED and with_token:
            _LOGGER.debug("%s 返回 token 失效，强制刷新后重试", path)
            await self.async_get_token(force=True)
            return await self._post(path, data, with_token=with_token, headers=headers)

        msg = payload.get("msg", "")
        if code in CODE_APP_KEY_INVALID:
            raise EzvizAuthError(f"{path} AppKey 无效：{msg}")
        if code in CODE_DEVICE_NOT_OWNED:
            raise EzvizPermissionError(
                f"{path} 该 AppKey 所属账号不拥有设备 {self._device_serial}：{msg}"
            )
        raise EzvizApiError(f"{path} 返回 {code}：{msg}")

    # ------------------------------------------------------------------ 健康度
    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        token_in_query: bool = True,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """/api/v3 系列的 GET 请求。

        与 lapp 的 POST 表单有三处不同，必须分开实现：
        1. keyValue 要求 accessToken 放在 **query string**；
           power/status/get 与 otap 要求放在 **header**；
        2. 成功判定可能是顶层 ``{"code":"200"}``，也可能是 ``{"meta":{"code":200}}``；
        3. 失败信息同样分散在 ``msg`` / ``meta.message`` 两处。
        """
        token = await self.async_get_token()
        query = dict(params or {})
        hdrs = dict(headers or {})
        if token_in_query:
            query["accessToken"] = token
        else:
            hdrs["accessToken"] = token

        url = f"{self._base_url}{path}"
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.get(url, params=query, headers=hdrs) as resp:
                    if resp.status != 200:
                        raise EzvizApiError(f"{path} HTTP {resp.status}")
                    payload = await resp.json(content_type=None)
        except asyncio.TimeoutError as err:
            raise EzvizApiError(f"{path} 请求超时（{REQUEST_TIMEOUT}s）") from err
        except aiohttp.ClientError as err:
            raise EzvizApiError(f"{path} 网络错误：{err}") from err

        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        raw_code = payload.get("code", meta.get("code", ""))
        code = "" if raw_code is None else str(raw_code)
        if code in (CODE_OK, "", "0"):
            return payload
        if code in CODE_TOKEN_EXPIRED:
            await self.async_get_token(force=True)
            return await self._get(path, params, token_in_query=token_in_query, headers=headers)
        msg = payload.get("msg") or meta.get("message") or ""
        if code in CODE_NO_PERMISSION:
            raise EzvizPermissionError(f"{path} 无权限或未开通白名单（{code}：{msg}）")
        raise EzvizApiError(f"{path} 返回 {code}：{msg}")

    async def async_get_multi_power(
        self, channel_no: int = 1
    ) -> list[dict[str, Any]]:
        """获取电量（multiPower），返回归一化后的电池条目列表。

        文档：https://open.ys7.com/help/5009 「获取电量」（设备键值对查询）
        实测：``GET /api/v3/keyValue/{sn}/{ch}/op?accessToken=..&key=multiPower``
        返回 ``valueInfo.multiPower`` 是**字符串形式的 JSON 数组**：
            [{"Type":0,"Name":"VideoLock","Remaining":100,"Status":0},
             {"Type":1,"Name":"Door","Remaining":255,"Status":0}]
        Type：0=主电源（电池组）、1=蓄电池；
        Remaining：0~100 百分比，255 表示该电池不存在/未知；
        Status：0 正常 / 1 充电中 / 2 充满 / 3 无电池 / 4 电池异常。
        """
        payload = await self._get(
            f"/api/v3/keyValue/{self._device_serial}/{channel_no}/op",
            {"key": "multiPower"},
        )
        value_info = payload.get("data") or {}
        raw = value_info.get("valueInfo") if isinstance(value_info, dict) else None
        raw = (raw or {}).get("multiPower") if isinstance(raw, dict) else None
        if not raw:
            return []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                _LOGGER.debug("multiPower 不是合法 JSON：%s", raw[:120])
                return []
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    async def async_get_power_status(self) -> str | None:
        """查询电池充电状态（multiPower 不可用时的兜底）。

        文档：https://open.ys7.com/help/1739
        ``GET /api/v3/device/power/status/get``，token 与 deviceSerial 都放 header。
        返回 "0"~"4"，含义见 const.POWER_STATUS_NAMES。
        """
        payload = await self._get(
            "/api/v3/device/power/status/get",
            token_in_query=False,
            headers={"deviceSerial": self._device_serial},
        )
        data = payload.get("data") or {}
        value_info = data.get("valueInfo") if isinstance(data, dict) else None
        status = (value_info or {}).get("powerStatus") if isinstance(value_info, dict) else None
        return None if status is None else str(status)

    async def async_get_lock_users(self) -> list[dict[str, Any]]:
        """门锁成员名单（含每类凭证数量）。

        文档：https://open.ys7.com/help/1352
        返回字段：lockUserIndex / lockUserName / faceCount / volarVeinCount /
                 fingerCount / pwdCount / cardCount / doubleAuthStatus /
                 expiredEnable / beginTime / endTime。
        注意：该接口只吃 form 表单，传 JSON 会得到 50000「服务器异常」。
        """
        payload = await self._post(
            "/api/lapp/keylock/user/list", {"deviceSerial": self._device_serial}
        )
        items = payload.get("data")
        if isinstance(items, dict):
            items = items.get("list") or items.get("userList")
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    # ------------------------------------------------------------------ 设备信息
    async def async_get_device_info(self) -> dict[str, Any]:
        payload = await self._post(
            "/api/lapp/device/info", {"deviceSerial": self._device_serial}
        )
        return payload.get("data") or {}

    async def async_get_door_records_range(
        self, start_ms: int, end_ms: int, max_pages: int = 4
    ) -> list[DoorRecord]:
        """按时间范围分页拉取开门记录（每页 50 条，最多 max_pages 页）。

        用于「今日统计」：单页 50 条在高频使用日会截断，导致次数偏少。
        """
        collected: list[DoorRecord] = []
        seen: set[str] = set()
        for page in range(max_pages):
            batch = await self.async_get_door_records(
                page_start=page * 50,
                page_size=50,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            if not batch:
                break
            for rec in batch:
                if rec.uid in seen:
                    continue
                seen.add(rec.uid)
                collected.append(rec)
            if len(batch) < 50:
                break
        collected.sort(key=lambda r: r.open_time, reverse=True)
        return collected

    # ------------------------------------------------------------------ 开门记录
    async def async_get_door_records(
        self,
        page_start: int = 0,
        page_size: int = 30,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[DoorRecord]:
        """分页获取开门记录。

        文档：https://open.ys7.com/help/1353
        仅 CS-DL30-V100 / CS-Y3000F-V100 系列保证可用，且需要萤石侧开通接口白名单。
        """
        body: dict[str, str] = {
            "deviceSerial": self._device_serial,
            "pageStart": str(page_start),
            "pageSize": str(min(page_size, 50)),
        }
        if start_ms is not None:
            body["startTime"] = str(int(start_ms))
        if end_ms is not None:
            body["endTime"] = str(int(end_ms))
        payload = await self._post("/api/lapp/keylock/open/list", body)
        raw = payload.get("data")
        items: list[dict[str, Any]] = []
        # 兼容两种返回：data 为数组 / data 为 {list:[...]} 或 {records:[...]}
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            for key in ("list", "records", "rows", "data"):
                if isinstance(raw.get(key), list):
                    items = raw[key]
                    break

        records: list[DoorRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                records.append(
                    DoorRecord(
                        user_name=str(item.get("lockUserName") or "未知用户"),
                        open_type=int(item.get("openType") or 0),
                        open_time=int(item.get("openTime") or 0),
                    )
                )
            except (TypeError, ValueError):
                _LOGGER.debug("跳过无法解析的开门记录：%s", item)
        records.sort(key=lambda r: r.open_time, reverse=True)
        return records

    # ------------------------------------------------------------------ 实时抓拍
    async def async_capture(self, channel_no: int = 1) -> str | None:
        """让设备立即抓拍一张当前画面，返回图片 URL。

        文档：https://open.ys7.com/help/368
        注意：拿到的是「此刻」的画面，不是历史某一刻的回放帧。
        门锁的历史抓拍只能来自 alarm/list 的 alarmPicUrl，而该通道在未订阅
        云存储/未开启告警上报时基本为空，所以实时补拍是拿到人脸的可行手段。
        """
        payload = await self._post(
            "/api/lapp/device/capture",
            {"deviceSerial": self._device_serial, "channelNo": str(channel_no)},
        )
        data = payload.get("data") or {}
        if isinstance(data, dict):
            return data.get("picUrl") or data.get("picURL") or None
        return None

    # ------------------------------------------------------------------ 告警
    async def async_get_alarms(
        self,
        start_ms: int,
        end_ms: int,
        *,
        channel_no: int = 1,
        page_size: int = 50,
    ) -> list[AlarmRecord]:
        """按设备拉取指定时间窗内的告警消息（含抓拍图）。"""
        payload = await self._post(
            "/api/lapp/alarm/list",
            {
                "deviceSerial": self._device_serial,
                "channelNo": str(channel_no),
                "startTime": str(start_ms),
                "endTime": str(end_ms),
                "status": "2",          # 2 = 全部（不区分已读）
                "pageStart": "0",
                "pageSize": str(min(page_size, 50)),
            },
        )
        items = payload.get("data")
        if not isinstance(items, list):
            items = []

        alarms: list[AlarmRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                alarms.append(
                    AlarmRecord(
                        alarm_id=str(item.get("alarmId") or ""),
                        alarm_type=int(item.get("alarmType") or 0),
                        alarm_time=int(item.get("alarmTime") or 0),
                        pic_url=item.get("alarmPicUrl"),
                        channel_no=int(item.get("channelNo") or channel_no),
                        rec_state=int(item.get("recState") or 0),
                        pre_time=int(item.get("preTime") or 0),
                        delay_time=int(item.get("delayTime") or 0),
                        alarm_name=item.get("alarmName"),
                        customer_type=item.get("customerType"),
                        customer_info=item.get("customerInfo"),
                    )
                )
            except (TypeError, ValueError):
                _LOGGER.debug("跳过无法解析的告警：%s", item)
        alarms.sort(key=lambda a: a.alarm_time)
        return alarms

    # ------------------------------------------------------------------ 录像
    async def async_get_storage_files(
        self, start_ms: int, end_ms: int, *, channel_no: int = 1, rec_type: int = 0
    ) -> list[StorageFile]:
        """查询时间段内的可回放录像（rec_type: 0自动 1云存储 2本地录像）。"""
        payload = await self._post(
            "/api/lapp/video/by/time",
            {
                "deviceSerial": self._device_serial,
                "channelNo": str(channel_no),
                "startTime": str(start_ms),
                "endTime": str(end_ms),
                "recType": str(rec_type),
            },
        )
        items = payload.get("data")
        if not isinstance(items, list):
            return []
        files: list[StorageFile] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                files.append(
                    StorageFile(
                        start_ms=int(item.get("startTime") or 0),
                        end_ms=int(item.get("stopTime") or 0),
                        file_type=int(item.get("fileType") or 0),
                    )
                )
            except (TypeError, ValueError):
                continue
        return files

    async def async_get_vod_url(
        self,
        start_ms: int,
        end_ms: int,
        *,
        channel_no: int = 1,
        fmt: str = "MP4",
        expire_seconds: int = 3600,
    ) -> str | None:
        """获取云录像点播地址（HLS / MP4）。

        文档：https://open.ys7.com/help/3973
        前置条件：设备已开通云存储，且该时段有云录像；不支持子账号 / 托管账号。
        """
        token = await self.async_get_token()
        start_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_ms / 1000))
        end_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_ms / 1000))

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.post(
                    f"{self._base_url}/api/service/open/vod/file/address/hls",
                    data={
                        "startTime": start_str,
                        "endTime": end_str,
                        "format": fmt.upper(),
                        "expireSeconds": str(min(max(expire_seconds, 1), 7 * 86400)),
                    },
                    headers={
                        "accessToken": token,
                        "deviceSerial": self._device_serial,
                        "localIndex": str(channel_no),
                    },
                ) as resp:
                    payload = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            _LOGGER.warning("获取云录像点播地址超时（%s ~ %s）", start_str, end_str)
            return None
        except aiohttp.ClientError as err:
            _LOGGER.warning("获取云录像点播地址失败：%s", err)
            return None

        if str(payload.get("code")) != CODE_OK:
            _LOGGER.info(
                "云录像点播不可用（code=%s msg=%s）。常见原因：未开通云存储 / 该时段无录像 / 账号为子账号",
                payload.get("code"),
                payload.get("msg"),
            )
            return None

        data = payload.get("data")
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in ("url", "playUrl", "address", "hlsUrl", "mp4Url"):
                if data.get(key):
                    return str(data[key])
        return None

    async def async_get_live_url(
        self,
        *,
        channel_no: int = 1,
        protocol: int = LIVE_PROTOCOL_FLV,
        quality: int = 1,
        expire_seconds: int = 3600,
    ) -> str | None:
        """获取实时直播取流地址（type=1 预览）。

        默认走 FLV：实测 HLS 分片会丢宏块，画面中间横着一条绿带；FLV 是连续流，
        全程干净（对比见 const.LIVE_PROTOCOL_FLV 的注释）。

        为什么不用回放流：实测这台锁的 SD 回放只给残帧，解码出来只有上部
        一小条画面，其余全绿。直播流是完整的，所以事件发生时直接抓直播。
        protocol=2 得到 HLS 播放列表，ffmpeg 能直接当输入读。
        """
        payload = await self._post(
            "/api/lapp/v2/live/address/get",
            {
                "deviceSerial": self._device_serial,
                "channelNo": str(channel_no),
                "protocol": str(protocol),
                "type": str(LIVE_TYPE_PREVIEW),
                "quality": str(quality),
                "expireTime": str(min(max(expire_seconds, 1), 86400)),
                "supportH265": "1",
            },
        )
        if not payload:
            return None
        data = payload.get("data") or {}
        url = data.get("url") if isinstance(data, dict) else None
        return str(url) if url else None

    async def async_get_playback_url(
        self,
        start_ms: int,
        end_ms: int,
        *,
        channel_no: int = 1,
        protocol: int = PLAYBACK_PROTOCOL_FLV,
        quality: int = 1,
        support_h265: bool = True,
        expire_seconds: int = 3600,
    ) -> str | None:
        """获取「SD 卡本地录像」的回放取流地址。

        文档：https://open.ys7.com/help/1599
        /api/lapp/v2/live/address/get，type=2 表示本地录像回放。
        与云存储点播（vod/file/address）是两条完全不同的链路：
        - 云存储：需要订阅，拿的是 mp4/hls 点播地址，能直接下载；
        - 本地回放：不需要订阅，走设备推流，是 flv/rtmp 实时流，
          只能边推边收，收完再转封装成 mp4。

        **supportH265 必须传 1**：这台锁出的是 H265 码流，若声明「不要求 H265」，
        平台不会转码，而是直接推一段写着「视频编码类型非H264，请修改为H264」的
        提示画面（固定约 144KB），看起来像录像其实是张图。

        注意：设备开启「视频加密」(isEncrypt=1) 时，本接口返回 60090
        「设备已加密，回放场景暂不支持取流」，此时无法取录像。
        """
        start_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_ms / 1000))
        end_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_ms / 1000))
        payload = await self._post(
            "/api/lapp/v2/live/address/get",
            {
                "deviceSerial": self._device_serial,
                "channelNo": str(channel_no),
                "protocol": str(protocol),
                "type": str(PLAYBACK_TYPE_LOCAL),
                "quality": str(quality),
                "startTime": start_str,
                "stopTime": end_str,
                "supportH265": "1" if support_h265 else "0",
                "expireTime": str(min(max(expire_seconds, 1), 86400)),
            },
        )
        code = str(payload.get("code"))
        if code != CODE_OK:
            msg = str(payload.get("msg") or "")
            if code == "60090":
                _LOGGER.warning(
                    "取不到徘徊录像：设备开启了「视频加密」。请在萤石 App 里关闭视频加密后重试（%s）",
                    msg,
                )
            else:
                _LOGGER.info(
                    "本地录像回放取流失败 code=%s msg=%s（%s ~ %s）", code, msg, start_str, end_str
                )
            return None
        data = payload.get("data") or {}
        if isinstance(data, dict):
            for key in ("url", "playUrl", "address"):
                if data.get(key):
                    return str(data[key])
        return None

    async def async_stream_to_file(
        self,
        url: str,
        dest,
        *,
        idle_timeout: float = LOITER_CLIP_IDLE_S,
        total_timeout: float = LOITER_CLIP_TOTAL_S,
        max_bytes: int = LOITER_CLIP_MAX_BYTES,
    ) -> int:
        """把一条「不会主动结束」的推流 URL 落到本地文件，返回字节数。

        与 async_download 的区别：这里 URL 是设备回放推流，收完内容后
        服务端**不会断开连接**，`iter_chunked` 会一直挂着等。
        所以判结束的条件是「连续 idle_timeout 秒没有新数据」，
        而不是 EOF —— 用 sock_read 超时天然表达这件事。
        """
        loop = asyncio.get_running_loop()
        timeout = aiohttp.ClientTimeout(total=total_timeout, sock_read=idle_timeout)
        fh = None
        written = 0
        try:
            await loop.run_in_executor(None, lambda: dest.parent.mkdir(parents=True, exist_ok=True))
            fh = await loop.run_in_executor(None, dest.open, "wb")
            try:
                async with self._session.get(url, timeout=timeout) as resp:
                    if resp.status != 200:
                        raise EzvizApiError(f"取流失败 HTTP {resp.status}")
                    async for chunk in resp.content.iter_chunked(32768):
                        written += len(chunk)
                        if written > max_bytes:
                            _LOGGER.warning("录像流超过大小上限，截断保存")
                            break
                        await loop.run_in_executor(None, fh.write, chunk)
            except asyncio.TimeoutError:
                # 正常收尾路径：内容推完了，连接还挂着
                _LOGGER.debug("录像流空闲超时，视为推送结束（已收 %d 字节）", written)
            return written
        except aiohttp.ClientError as err:
            if written > 0:
                _LOGGER.debug("录像流中断（已收 %d 字节）：%s", written, err)
                return written
            raise EzvizApiError(f"取流网络错误：{err}") from err
        finally:
            if fh is not None:
                try:
                    await loop.run_in_executor(None, fh.close)
                except Exception:  # noqa: BLE001 - 关闭失败不影响已落盘内容
                    pass

    async def async_download(self, url: str, dest, *, max_bytes: int = 300 * 1024 * 1024) -> int:
        """下载 URL 到本地文件，返回字节数。用于抓拍图与录像存档。

        写盘必须走 executor：直接在事件循环里 open()/write() 会被 HA 判为
        blocking call，启动阶段尤其容易拖慢甚至拖死整个 bootstrap。
        """
        loop = asyncio.get_running_loop()
        fh = None
        try:
            async with asyncio.timeout(120):
                async with self._session.get(url) as resp:
                    if resp.status != 200:
                        raise EzvizApiError(f"下载失败 HTTP {resp.status}")
                    written = 0
                    await loop.run_in_executor(
                        None, lambda: dest.parent.mkdir(parents=True, exist_ok=True)
                    )
                    fh = await loop.run_in_executor(None, dest.open, "wb")
                    async for chunk in resp.content.iter_chunked(65536):
                        written += len(chunk)
                        if written > max_bytes:
                            raise EzvizApiError("下载内容超出大小上限")
                        await loop.run_in_executor(None, fh.write, chunk)
                    return written
        except asyncio.TimeoutError as err:
            raise EzvizApiError("下载超时") from err
        except aiohttp.ClientError as err:
            raise EzvizApiError(f"下载网络错误：{err}") from err
        finally:
            if fh is not None:
                try:
                    await loop.run_in_executor(None, fh.close)
                except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
                    pass
