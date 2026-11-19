"""数据协调器：轮询开门记录 → 关联抓拍图 → 归档 → 触发事件。

核心难点与处理方式
------------------
1. keylock/open/list 只给「谁 / 怎么开的 / 什么时候」，没有图片；
   alarm/list 只给「抓拍图 / 告警时间」，两者没有外键关联。
   → 用时间窗匹配：以开门时刻为锚点，在 [t-8s, t+20s] 内找时间最近的带图告警。
2. 告警通道经常是空的（实测 365 天仅 1 条），所以再加一条兜底：
   发现新开门时立刻调 device/capture 补拍一张，保证每条记录都有图。
3. openType=9 是「关门」事件（文档未定义，实测判定），没有人员也没有图，
   必须与真实开门分开统计，否则列表里会塞满「未知用户」。
4. 「几人」开放平台没有该字段。
   → 三级降级：透传字段 customerInfo 里的人数 > 聚合窗内不同 faceId 计数 > 记 1。
   结果里用 person_count_source 明确标注来源，避免把估算值当精确值。
5. 抓拍图 URL 带签名且有时效，必须落盘，否则过期后无法回看。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AlarmRecord, DoorRecord, EzvizApiClient, EzvizApiError
from .app_msg import (
    AppCloudClip,
    AppDataUnavailable,
    AppMsgConfigError,
    AppMsgError,
    EzvizAppMsgClient,
    load_app_config,
)
from .health import HealthMonitor, LockHealth, MemberInfo
from .hevc_split import SplitError, green_frame_ratio, repair_interleaved_hevc
from .peek_dual import DualPeekError, extract_dual_frames
from .const import (
    APP_MSG_CONFIG_FILE,
    APP_MSG_LOITER_WINDOW_MS,
    APP_MSG_OPEN_LOOKAHEAD_MS,
    APP_MSG_OPEN_LOOKBACK_MS,
    APP_MSG_SOURCE,
    APP_MSG_SYNC_INTERVAL_S,
    ALARM_TYPE_LOITER,
    ALARM_TYPE_OPEN,
    OPEN_ALARM_TYPES,
    OPEN_WAY_KEYS,
    OPEN_WAY_TO_TYPE,
    OTHER_ALARMS_KEEP_MAX,
    USER_KEYS,
    CONF_ALARM_TYPE_NAMES,
    CONF_AUTO_DOWNLOAD_CLIP,
    CONF_CAPTURE_ON_EVENT,
    CONF_CHANNEL_NO,
    CONF_COVER_FIRST,
    CONF_DEVICE_SERIAL,
    CONF_MERGE_WINDOW,
    CONF_SCAN_INTERVAL,
    CONF_SNAPSHOT_RETENTION,
    CONF_USER_ALIASES,
    DEFAULT_ALARM_TYPE_NAMES,
    DEFAULT_AUTO_DOWNLOAD_CLIP,
    DEFAULT_CAPTURE_ON_EVENT,
    DEFAULT_CHANNEL_NO,
    DEFAULT_COVER_FIRST,
    DEFAULT_MERGE_WINDOW,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SNAPSHOT_RETENTION,
    DOOR_CLOSE_TYPE,
    DOOR_INSIDE_TYPE,
    DOORLOCK_SUBDIR,
    DOMAIN,
    EVENT_DOOR_OPENED,
    EVENT_LOITER_DETECTED,
    ENABLE_LIVE_CAPTURE,
    LOCAL_CLIP_PREFIX,
    LOCAL_SNAPSHOT_PREFIX,
    LOITER_CLIP_READY_EXTRA_S,
    LOITER_DEFAULT_POST_S,
    LOITER_DEFAULT_PRE_S,
    LIVE_MAX_AGE_S,
    DOOR_LIVE_SECONDS,
    LOITER_LIVE_SECONDS,
    LOITER_KEEP_MAX,
    MEDIA_ROOT,
    MEDIA_URL_PREFIX,
    OPEN_TYPES,
    OPEN_TYPE_SLUGS,
    OUTSIDE_OPEN_TYPES,
    PEEK_KEEP,
    PEEK_SUBDIR,
    PLACEHOLDER_MAX_BYTES,
    PLACEHOLDER_MIN_WIDTH,
    CLIP_RETRY_DELAY_S,
    COVER_BACKFILL_AGE_S,
    COVER_MATCH_AFTER_S,
    COVER_MATCH_BEFORE_S,
    COVER_MAX_PER_CYCLE,
    COVER_MIN_BYTES,
    COVER_POLL_INTERVAL_S,
    COVER_WAIT_S,
    SIGNED_URL_DAYS,
    STORAGE_KEY,
    STORAGE_VERSION,
    WWW_SUBDIR,
)

_LOGGER = logging.getLogger(__name__)

ALARM_LOOKBACK_MS = 15_000    # 开门时刻之前多久内的告警也算同一次通行
ALARM_LOOKAHEAD_MS = 20_000   # 实测人脸告警与开门记录几乎同秒，窗口不能放大：
                              # 放到 45s 时，室内开门会抢走 20 秒后那次人脸开门的图
CLIP_MATCH_WINDOW_S = 20      # SD 回放段与开门时刻的最大允许偏差（秒）。
                              # 实测 6 次真实匹配的偏差是 1/2/2/3/5/12 秒；20 秒足够覆盖，
                              # 且比旧的 30 秒更严。放宽到 90 秒会把「58 秒后另一件事的录像」
                              # 贴给这次开门 —— 那是造假，宁可显示抓拍图。
                              # 主人 11-18 08:57 要求由 30 收紧到 20。
LOITER_MATCH_WINDOW_S = 45    # 徘徊唤醒的 SD 段窗口。徘徊录像本身是「触发前 5 秒 +
                              # 之后 25 秒」共 30 秒，段起点落在 [t-5, t+25] 都算这段；
                              # 取 45 秒覆盖并留切段偏移余量。同样不许越界贴片
MAX_KEEP_EVENTS = 300         # 内存 + 持久化保留的事件条数
FACE_ID_KEYS = ("faceId", "face_id", "faceID", "faceid")
PERSON_COUNT_KEYS = ("personNum", "personCount", "peopleNum", "humanCount", "personCnt")


@dataclass
class DoorEvent:
    """一条归档后的开门事件。"""

    uid: str
    open_time_ms: int
    user_name: str
    open_type: int
    open_type_name: str
    person_count: int = 1
    person_count_source: str = "default"  # customer_info / face_ids / default / n/a
    face_id: str | None = None
    snapshot_url: str | None = None
    snapshot_path: str | None = None
    snapshot_source: str | None = None  # alarm（告警原图）/ capture（发现时补拍）
    clip_url: str | None = None
    clip_remote_url: str | None = None
    clip_path: str | None = None
    clip_status: str | None = None   # recording / ready / too_late / failed
    clip_bytes: int = 0
    alarm_id: str | None = None
    alarm_type: int | None = None
    alarm_type_name: str | None = None
    channel_no: int = 1
    is_close: bool = False           # openType=9 关门事件
    capture_ms: int | None = None    # 补拍发生的时刻（用于判断图片时效）
    user_name_raw: str | None = None  # 设备原始上报名（映射前），便于排查
    source: str = "keylock"          # keylock=开门记录接口 / alarm=告警兜底

    @property
    def open_time_iso(self) -> str:
        return dt_util.as_local(
            datetime.fromtimestamp(self.open_time_ms / 1000, tz=dt_util.UTC)
        ).isoformat()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["open_time_iso"] = self.open_time_iso
        return data


@dataclass
class LoiterEvent:
    """一次「徘徊唤醒」：人靠近门但没开门，设备被唤醒并录一段。

    数据来源是 alarm/list 里 alarmType=15504 的告警：
    - alarmTime 是「触发时刻」，preTime / delayTime 决定录像窗口；
    - 这台锁的徘徊录像存在 SD 卡（recState=4），要走本地回放取流，
      不能像开门那样直接用图片 URL。
    """

    uid: str                       # 告警 alarmId，去重主键
    alarm_type: int
    alarm_type_name: str
    time_ms: int                   # 触发时刻
    alarm_id: str | None = None
    pre_s: int = LOITER_DEFAULT_PRE_S
    post_s: int = LOITER_DEFAULT_POST_S
    snapshot_url: str | None = None
    snapshot_path: str | None = None
    clip_url: str | None = None
    clip_path: str | None = None
    clip_status: str = "pending"   # pending / ready / no_stream / encrypt / failed / skipped
    clip_bytes: int = 0
    clip_retries: int = 0

    @property
    def time_iso(self) -> str:
        return dt_util.as_local(
            datetime.fromtimestamp(self.time_ms / 1000, tz=dt_util.UTC)
        ).isoformat()

    @property
    def duration_s(self) -> int:
        return max(self.pre_s, 0) + max(self.post_s, 0)

    @property
    def clip_start_ms(self) -> int:
        return self.time_ms - max(self.pre_s, 0) * 1000

    @property
    def clip_end_ms(self) -> int:
        return self.time_ms + max(self.post_s, 0) * 1000

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "alarm_type": self.alarm_type,
            "alarm_type_name": self.alarm_type_name,
            "time": self.time_iso,
            "time_ms": self.time_ms,
            "duration_s": self.duration_s,
            "snapshot_url": self.snapshot_url,
            "snapshot_path": self.snapshot_path,
            "clip_url": self.clip_url,
            "clip_path": self.clip_path,
            "clip_status": self.clip_status,
            "clip_bytes": self.clip_bytes,
        }


@dataclass
class LockData:
    """协调器一次刷新的结果。"""

    events: list[DoorEvent] = field(default_factory=list)
    last: DoorEvent | None = None
    today_count: int = 0
    today_close_count: int = 0
    today_events: list[DoorEvent] = field(default_factory=list)   # 今日全部开门（倒序）
    today_close_raw: int = 0                                      # 关门原始条数（未去重）
    last_close: DoorEvent | None = None
    total_count: int = 0
    online: bool = True
    device_name: str | None = None
    # ---- 徘徊唤醒 ----
    loiter_events: list[LoiterEvent] = field(default_factory=list)     # 倒序
    loiter_today_count: int = 0
    loiter_last: LoiterEvent | None = None
    # ---- 其它告警（只留痕：不进开门表/徘徊表、不配视频）----
    other_alarms: list[dict[str, Any]] = field(default_factory=list)
    # ---- 健康度（电量 / 信号 / 成员凭证，见 health.py）----
    health: LockHealth = field(default_factory=LockHealth)


def _decode_customer_info(raw: Any) -> dict[str, Any]:
    """告警 customerInfo 解码：设备上报的是 base64(JSON)，也可能已是 dict/JSON 串。

    Y3000FVX 的 17011 开门告警形如
    base64('{"user":"用户2","IOT1SmartlockOpenDoorWay":"face"}')。
    解析失败返回空 dict，调用方按"未知"处理，绝不因此中断同步。
    """
    if not raw:
        return {}
    data: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            padded = text + "=" * (-len(text) % 4)
            decoded = base64.b64decode(padded, validate=True).decode("utf-8", "replace")
            data = json.loads(decoded)
        except (ValueError, json.JSONDecodeError):
            try:
                data = json.loads(text)
            except (ValueError, json.JSONDecodeError):
                return {}
    if not isinstance(data, dict):
        return {}
    return data


def _open_way_to_type(info: dict[str, Any]) -> tuple[int, str]:
    """从解码后的 customerInfo 取 (open_type, 原始开门方式字符串)。"""
    way_raw = ""
    for key in OPEN_WAY_KEYS:
        val = info.get(key)
        if val:
            way_raw = str(val)
            break
    way = way_raw.strip().lower()
    return OPEN_WAY_TO_TYPE.get(way, 4), way_raw  # 认不出默认按人脸(4)，门外开门


def _deep_find(data: Any, keys: tuple[str, ...], depth: int = 0) -> Any:
    """在嵌套 dict/list 中按 key 名查找第一个命中的值。"""
    if depth > 4:
        return None
    if isinstance(data, dict):
        for k, v in data.items():
            if k in keys and v not in (None, "", []):
                return v
        for v in data.values():
            found = _deep_find(v, keys, depth + 1)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _deep_find(item, keys, depth + 1)
            if found is not None:
                return found
    return None


class EzvizLockCoordinator(DataUpdateCoordinator[LockData]):
    """负责取数、归档与事件派发。"""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EzvizApiClient,
        config_root: Path,
    ) -> None:
        self.entry = entry
        self.client = client
        self._config_root = config_root
        # 旧位置：config/www/ezviz_lock（仅作迁移源与兜底访问）
        self._www_root = config_root / "www" / WWW_SUBDIR
        # 新位置：额外存储 /media/tpcamera/door-lock —— 不占 config，也不进 HA 备份
        self._media_root = Path(MEDIA_ROOT) / DOORLOCK_SUBDIR
        self._snapshot_root = self._media_root
        self._clip_root = self._media_root / "clips"
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{client.device_serial}")

        self._seen: set[str] = set()
        self._events: list[DoorEvent] = []
        self._initialized = False
        self._today_synced = False
        # 一条告警（一张图）只能配给一个开门事件，否则会出现「室内开门拿着别人的人脸图」
        self._used_alarm_ids: set[str] = set()
        # v1.9.3：App 消息通道入池的开门事件，告警原图走后台下载（与徘徊分支对称）。
        # 之前这条路径只建行不配图，还先把 uid 塞进 _seen，导致 keylock 主循环
        # 再拿到同一条时判「已见」跳过，抓拍/下载两条路全断 —— 整批事件无图。
        self._open_snap_tasks: set[str] = set()   # 正在后台下载的 uid，防并发重复
        self._open_img_fail: set[str] = set()     # 图本身下载失败，别每轮都重试同一张死图
        # 负缓存：这些事件「云端确实没有图」，以后不再重复请求（避免每次启动白拉一遍）
        self._no_image_uids: set[str] = set()
        self._late_fill_started = False
        self._device_name: str | None = None
        # 逐帧验收过的干净录像索引（前端按时间就近匹配开门记录），见 clean_clips.json
        self.clean_clips: list[dict[str, Any]] = []
        self._clean_index_stamp: tuple[int, int] | None = None
        # v1.9.2：上次全量 MD5 验证通过的索引指纹（持久化），冷启动直接复用，
        # 不再让 bootstrap 等 30 段录像的 MD5；后台仍会做一次完整复核。
        self._clean_cache: dict[str, Any] = {}
        self._clean_verify_task: asyncio.Task | None = None
        self._clean_verify_cancel: Any = None

        # ---- 徘徊唤醒 ----
        self._loiter: list[LoiterEvent] = []
        self._loiter_seen: set[str] = set()
        # 用户手动删掉的徘徊告警：留墓碑，防止下一轮 alarm/list 又把它拉回来
        self._deleted_loiter: set[str] = set()
        self._loiter_tasks: dict[str, asyncio.Task] = {}
        self._loiter_snap_tasks: set[str] = set()
        self._loiter_img_fail: set[str] = set()
        self._loiter_synced = False
        # ---- 告警驱动 ----
        # 已用告警兜底补过开门行的 alarmId，避免重复补；与 keylock 记录按时间窗去重
        self._alarm_open_seen: set[str] = set()
        # 非开门/徘徊告警：只留痕，不进开门表/徘徊表、不配视频
        self._other_alarms: list[dict[str, Any]] = []
        self._other_alarm_seen: set[str] = set()
        # 徘徊录像靠 ffmpeg 把推流转封装成 mp4；没有就退化成存 .flv
        self._ffmpeg = shutil.which("ffmpeg")
        if self._ffmpeg:
            _LOGGER.info("徘徊录像将用 ffmpeg 转封装为 mp4：%s", self._ffmpeg)
        else:
            _LOGGER.warning(
                "系统里找不到 ffmpeg，徘徊录像只能保存为 flv，浏览器无法直接播放"
            )

        interval = int(entry.options.get(CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)))
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{client.device_serial}",
            update_interval=timedelta(seconds=max(interval, 10)),
            config_entry=entry,
        )
        self._channel_no = int(entry.options.get(CONF_CHANNEL_NO, entry.data.get(CONF_CHANNEL_NO, DEFAULT_CHANNEL_NO)))
        self._merge_window = int(entry.options.get(CONF_MERGE_WINDOW, DEFAULT_MERGE_WINDOW))
        self._retention = int(entry.options.get(CONF_SNAPSHOT_RETENTION, DEFAULT_SNAPSHOT_RETENTION))
        self._auto_clip = bool(entry.options.get(CONF_AUTO_DOWNLOAD_CLIP, DEFAULT_AUTO_DOWNLOAD_CLIP))
        self._capture_enabled = bool(
            entry.options.get(CONF_CAPTURE_ON_EVENT, DEFAULT_CAPTURE_ON_EVENT)
        )
        self._alarm_names = self._build_alarm_name_map(entry.options.get(CONF_ALARM_TYPE_NAMES, ""))
        self._user_aliases = self._build_user_alias_map(entry.options.get(CONF_USER_ALIASES, ""))

        # 电量 / 信号 / 在线 / 成员凭证：独立监控器，失败不影响开门主链路
        self.health = HealthMonitor(hass, client)
        # 「门前看一眼」最近一次的实时抓拍（url + 本地路径 + 时刻）
        self.peek: dict[str, Any] | None = None

        # ---- App 通道（v1.7.0 消息 / v1.8.0 升为主数据源）----
        # 无配置文件时完全不启用，行为与 1.6.0 一致；有文件时 openEvents /
        # streaming/records / unifiedmsg 取代开放平台的高频轮询，失败自动回退。
        self._app_msg_seen: set[str] = set()
        self._app_msg_client: EzvizAppMsgClient | None = None
        self._app_cfg_checked = False
        self._app_msg_status: dict[str, Any] = {"enabled": False}
        # v1.8.0 主数据源统计：开放平台到底省了多少次调用，全靠这几个计数器对外可见
        self._api_call_stats: dict[str, int] = {
            "open_door_app": 0,        # openEvents 成功次数（替代 keylock/open/list）
            "open_door_fallback": 0,   # openEvents 失败回退开放平台次数
            "loiter_fallback": 0,      # 徘徊告警回退开放平台次数
            "alarm_fallback": 0,       # 告警兜底回退开放平台次数
        }
        # 健康度 device/info 降频到 10 分钟（原来每 20s 一次，独占 4320 次/天）
        self._last_device_info_probe = 0.0
        self._device_probe_interval_s = 600.0

        # ---- v1.9.1 配图策略：capture 保底 + 云封面兜底 ----
        # 新事件仍然立刻 device/capture 抓一张（到得最快、对得上人）；只有抓拍失败
        # 或事件本来就拍不到图时，才去云存储片段里取 coverPic 兜底。封面走
        # alicloud HTTP，不吃开放平台配额、不需要设备在线，但与告警图同源
        # （设备没拍的事件云里也没有），所以只能兜底。
        self._cover_first = bool(
            entry.options.get(CONF_COVER_FIRST, DEFAULT_COVER_FIRST)
        )
        self._cover_pending: dict[str, DoorEvent] = {}   # uid → 事件（待补封面）
        self._cover_deadline: dict[str, float] = {}       # uid → 放弃时刻（monotonic）
        self._cover_done: set[str] = set()                # 已成功或已放弃，不再重试
        self._cover_tasks: set[str] = set()               # 正在下载中的 uid
        self._cloud_clips: list[AppCloudClip] = []
        self._cloud_clips_at = 0.0                        # 上次拉列表的 monotonic
        self._cloud_clip_seen: set[str] = set()           # 已用过的片段 key
        self._cover_stats: dict[str, int] = {
            "cloud_ok": 0,      # 云封面兜底成功
            "cloud_miss": 0,    # 等到过期也没匹配到片段
            "capture_fail": 0,  # 抓拍失败转封面兜底的次数
            "capture_fallback": 0,  # 仅 C 模式：等不到封面后回落抓拍的次数
            "backfill": 0,      # 历史事件回溯补图成功
        }
        self._backfill_queued = False    # 首轮回溯只跑一次
        self._last_capture_at = 0.0      # C 模式兜底抓拍 60 秒节流

    @property
    def app_msg_status(self) -> dict[str, Any]:
        """App 通道健康快照（供传感器属性与巡检观察）。"""
        return dict(self._app_msg_status)

    @property
    def api_call_stats(self) -> dict[str, int]:
        """开放平台调用节省统计（只读快照）。"""
        return dict(self._api_call_stats)

    @property
    def cover_stats(self) -> dict[str, Any]:
        """云封面补图统计（只读快照，供传感器属性观察）。"""
        return {
            **self._cover_stats,
            "cover_first": self._cover_first,
            "pending": len(self._cover_pending),
            "cloud_clips": len(self._cloud_clips),
        }

    # ------------------------------------------------------------------ 配置辅助
    def _build_user_alias_map(self, raw: str) -> dict[str, str]:
        """解析「锁上原名=显示名」映射，例如：用户1=爸爸,用户=妈妈。

        支持逗号 / 分号 / 换行分隔多个条目，等号中英文均可。
        """
        mapping: dict[str, str] = {}
        if not raw:
            return mapping
        for part in re.split(r"[,;，；\n]+", raw):
            part = part.strip()
            if not part or ("=" not in part and "＝" not in part):
                continue
            key, _, val = part.replace("＝", "=").partition("=")
            key, val = key.strip(), val.strip()
            if key and val:
                mapping[key] = val
            else:
                _LOGGER.warning("忽略无法解析的用户别名：%s", part)
        return mapping

    def display_user_name(self, raw_name: str | None) -> str:
        """把设备上报的「用户1」换成配置里的真名；没配置就原样返回。"""
        name = (raw_name or "").strip()
        if not name:
            return "未知用户"
        return self._user_aliases.get(name, name)


    def _build_alarm_name_map(self, raw: str) -> dict[int, str]:
        mapping = dict(DEFAULT_ALARM_TYPE_NAMES)
        if raw:
            for line in re.split(r"[,;\n]", raw):
                line = line.strip()
                if not line or ":" not in line and "：" not in line:
                    continue
                key, _, val = line.replace("：", ":").partition(":")
                try:
                    mapping[int(key.strip())] = val.strip()
                except ValueError:
                    _LOGGER.warning("忽略无法解析的告警类型映射：%s", line)
        return mapping

    def alarm_type_name(self, alarm_type: int | None) -> str | None:
        if alarm_type is None:
            return None
        if alarm_type in self._alarm_names:
            return self._alarm_names[alarm_type]
        _LOGGER.debug("unknown alarmType=%s，请在集成选项里补充名称映射", alarm_type)
        return f"类型{alarm_type}"

    @staticmethod
    def _dedupe_close_events(events: list[DoorEvent], window_sec: int = 60) -> list[DoorEvent]:
        """关事件去重。

        实测这台锁会在同一秒/相邻几秒重复上报「关门」（例如 18:01:59 与 18:02:05），
        直接按条数统计会出现「关门次数 > 开门次数」的明显错误。
        这里把间隔小于 window_sec 的连续关事件合并为一次。
        """
        if not events:
            return []
        ordered = sorted(events, key=lambda e: e.open_time_ms)
        result = [ordered[0]]
        for ev in ordered[1:]:
            if ev.open_time_ms - result[-1].open_time_ms > window_sec * 1000:
                result.append(ev)
        return result

    # ------------------------------------------------------------------ 持久化
    async def async_load_state(self) -> None:
        stored = await self._store.async_load() or {}
        # v1.8.1：seen 里存的是旧版毫秒级 uid，先统一换算成秒级主键，
        # 否则历史去重后这些事件会被当成「新记录」再推一遍通知。
        self._seen = {
            self._normalize_event_uid(u) for u in (stored.get("seen") or [])
        }
        for raw in stored.get("events") or []:
            try:
                raw.pop("open_time_iso", None)
                ev = DoorEvent(**raw)
            except TypeError:
                continue
            # 兼容旧版本已持久化的事件：早期版本没识别 openType=9，会把它当普通开门
            ev.is_close = ev.open_type == DOOR_CLOSE_TYPE
            if ev.is_close:
                ev.person_count = 0
                ev.person_count_source = "n/a"
            else:
                # 历史事件可能在改动别名之前就存下来了，这里按当前配置重新映射显示名
                if not ev.user_name_raw and ev.user_name and ev.user_name != "未知用户":
                    ev.user_name_raw = ev.user_name
                if ev.user_name_raw:
                    ev.user_name = self.display_user_name(ev.user_name_raw)
            self._events.append(ev)
        # v1.8.1 迁移：旧版本把同一次开门按毫秒精度存成了多条（keylock 整秒 vs
        # App/alarm 毫秒），历史数据不会自己消失。按新的秒级主键合并一次，
        # 保留信息最完整的那条（有图 > 有告警号 > 有录像 > 有真实人数）。
        _before_events = len(self._events)
        self._events = self._dedupe_events_by_second(self._events)
        # 去重后把新主键并进 seen：既覆盖被合并掉的那批旧 uid，也不会漏掉存活记录
        self._seen.update(ev.uid for ev in self._events)
        if len(self._events) != _before_events:
            _LOGGER.info(
                "历史开门记录去重：%d 条 → %d 条（合并了 %d 条同一次开门的重复行）",
                _before_events,
                len(self._events),
                _before_events - len(self._events),
            )
        self._initialized = bool(stored.get("initialized"))
        # v1.9.2：复用上次已全量验过的干净录像索引指纹（见 _async_load_clean_clips）
        cache = stored.get("clean_clips_cache")
        self._clean_cache = cache if isinstance(cache, dict) else {}
        # 「云端确认没有图」的事件，持久化下来，避免每次启动都去白拉一遍
        # v1.8.1：同样换算成秒级主键，否则去重后这张「没图」名单会全部失效
        self._no_image_uids = {
            self._normalize_event_uid(u) for u in (stored.get("no_image") or [])
        }
        # ---- 徘徊唤醒 ----
        self._deleted_loiter = set(stored.get("deleted_loiter") or [])
        for raw in stored.get("loiter") or []:
            try:
                lo = LoiterEvent(
                    uid=str(raw["uid"]),
                    alarm_type=int(raw.get("alarm_type") or ALARM_TYPE_LOITER),
                    alarm_type_name=str(raw.get("alarm_type_name") or "徘徊检测"),
                    time_ms=int(raw.get("time_ms") or 0),
                    alarm_id=raw.get("alarm_id") or raw.get("uid"),
                    pre_s=int(raw.get("pre_s") or LOITER_DEFAULT_PRE_S),
                    post_s=int(raw.get("post_s") or LOITER_DEFAULT_POST_S),
                    snapshot_path=raw.get("snapshot_path"),
                    clip_path=raw.get("clip_path"),
                    clip_status=str(raw.get("clip_status") or "pending"),
                    clip_bytes=int(raw.get("clip_bytes") or 0),
                    clip_retries=int(raw.get("clip_retries") or 0),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._loiter.append(lo)
            self._loiter_seen.add(lo.uid)
        self._loiter.sort(key=lambda x: x.time_ms, reverse=True)
        # v1.8.1 迁移：同一次徘徊被 App 与 alarm/list 各存了一条（msg_id 不同、时间完全相同），
        # 历史数据按「同类型 + 时间一致」合并，并重建去重索引。
        _before_loiter = len(self._loiter)
        self._loiter = self._dedupe_loiter_by_time(self._loiter)
        self._loiter_seen = {lo.uid for lo in self._loiter}
        if len(self._loiter) != _before_loiter:
            _LOGGER.info(
                "历史徘徊记录去重：%d 条 → %d 条（合并了 %d 条同一次徘徊的重复行）",
                _before_loiter,
                len(self._loiter),
                _before_loiter - len(self._loiter),
            )
        # ---- 告警驱动：兜底开门 + 其它告警留痕 ----
        self._alarm_open_seen = set(stored.get("alarm_open_seen") or [])
        self._other_alarms = list(stored.get("other_alarms") or [])
        self._other_alarm_seen = {
            str(a.get("alarm_id")) for a in self._other_alarms if a.get("alarm_id")
        }
        # App 消息去重集也持久化：重启后不会把今日消息重新补一遍
        self._app_msg_seen = set(stored.get("app_msg_seen") or [])
        # 把 config/www 下的旧图片搬到额外存储，然后回填 URL
        await self._async_migrate_legacy_snapshots()
        # 统一重签：持久化下来的可能是旧的 /local 地址或早先的签名
        await self._async_resign_snapshot_urls()
        # 启动即回填：上次运行已经把文件下好了，只是 URL 在重建时被冲掉
        await self._async_backfill_snapshots(self._events)
        # 录像同理：mp4 还在日期目录里，把签名地址认回来
        await self._async_backfill_clips(self._events)
        # 徘徊的图片 / 录像也要重签，否则重启后全是 401
        await self._async_resign_loiter_urls()
        await self._async_prune_loiter()
        # 健康度：恢复上次的成员名单基线，否则重启后第一次比对会把所有人报成「新增」
        try:
            await self.health.async_load()
        except Exception:  # noqa: BLE001 - 基线读不出来最多是不报警，不影响主流程
            _LOGGER.exception("读取门锁健康度基线失败")
        _LOGGER.debug(
            "恢复 %d 条历史事件、%d 条徘徊事件，去重索引 %d 条",
            len(self._events), len(self._loiter), len(self._seen),
        )

    @staticmethod
    def _normalize_event_uid(uid: str) -> str:
        """把旧版毫秒级事件主键换算成 v1.8.1 的秒级主键。

        形如 `1795047001602|5|用户` → `1795047001|5|用户`。
        不是三段的（例如 appmsg:xxx 这类别的主键）原样返回，不做任何猜测。
        """
        parts = str(uid).split("|")
        if len(parts) == 3 and parts[0].isdigit():
            return f"{int(parts[0]) // 1000}|{parts[1]}|{parts[2]}"
        return str(uid)

    @staticmethod
    def _dedupe_events_by_second(events: list[DoorEvent]) -> list[DoorEvent]:
        """按「秒级主键」合并同一次开门的历史重复行（v1.8.1）。

        主键与 DoorRecord.uid 保持一致：秒 | 方式 | 原始人名。
        DoorEvent 持久化的是显示名，所以 user_name_raw 优先；关门事件没有人员名，
        keylock 侧统一是「未知用户」，同样能对齐。合并后把 uid 就地改写成新主键，
        这样 _seen 的精确匹配才会生效。
        """

        def _key(ev: DoorEvent) -> str:
            name = ev.user_name_raw or ev.user_name
            return f"{ev.open_time_ms // 1000}|{ev.open_type}|{name}"

        def _score(ev: DoorEvent) -> tuple[int, ...]:
            # 信息完整度排序：有图 > 有告警号 > 有录像 > 真实人数
            return (
                1 if (ev.snapshot_path or ev.snapshot_url) else 0,
                1 if ev.alarm_id else 0,
                1 if (ev.clip_path or ev.clip_url) else 0,
                0 if ev.person_count_source in ("default", "n/a") else 1,
                ev.person_count or 0,
            )

        best: dict[str, DoorEvent] = {}
        for ev in events:
            key = _key(ev)
            cur = best.get(key)
            if cur is None or _score(ev) > _score(cur):
                best[key] = ev
        for key, ev in best.items():
            ev.uid = key
        return sorted(best.values(), key=lambda e: e.open_time_ms, reverse=True)

    @staticmethod
    def _dedupe_loiter_by_time(items: list[LoiterEvent]) -> list[LoiterEvent]:
        """合并「同类型 + 时间完全相同」的徘徊重复行（v1.8.1）。

        两种消息号（App 与 alarm/list）的 time_ms 实测完全一致，所以历史数据用
        精确相等即可，绝不会误合并真实的两起徘徊。运行期的判重仍用时间窗
        （见 _async_sync_loiter），两者用途不同。
        """

        def _score(lo: LoiterEvent) -> tuple[int, ...]:
            return (
                1 if lo.snapshot_path else 0,
                1 if lo.clip_path else 0,
                1 if lo.clip_status == "ready" else 0,
            )

        best: dict[tuple[int, int], LoiterEvent] = {}
        for lo in items:
            key = (lo.alarm_type, lo.time_ms)
            cur = best.get(key)
            if cur is None or _score(lo) > _score(cur):
                best[key] = lo
        return sorted(best.values(), key=lambda x: x.time_ms, reverse=True)

    async def _async_save_state(self) -> None:
        await self._store.async_save(
            {
                "initialized": self._initialized,
                "seen": list(self._seen)[-2000:],
                "events": [e.to_dict() for e in self._events[:MAX_KEEP_EVENTS]],
                "no_image": sorted(self._no_image_uids)[-800:],
                "deleted_loiter": sorted(self._deleted_loiter)[-500:],
                "loiter": [
                    {
                        "uid": lo.uid,
                        "alarm_type": lo.alarm_type,
                        "alarm_type_name": lo.alarm_type_name,
                        "time_ms": lo.time_ms,
                        "alarm_id": lo.alarm_id,
                        "pre_s": lo.pre_s,
                        "post_s": lo.post_s,
                        "snapshot_path": lo.snapshot_path,
                        "clip_path": lo.clip_path,
                        "clip_status": lo.clip_status,
                        "clip_bytes": lo.clip_bytes,
                        "clip_retries": lo.clip_retries,
                    }
                    for lo in self._loiter[:LOITER_KEEP_MAX]
                ],
                "alarm_open_seen": sorted(self._alarm_open_seen)[-1000:],
                "other_alarms": self._other_alarms[:OTHER_ALARMS_KEEP_MAX],
                "app_msg_seen": sorted(self._app_msg_seen)[-1000:],
                "clean_clips_cache": self._clean_cache,
            }
        )

    # ------------------------------------------------------------------ 主循环
    async def _async_ensure_app_client(self) -> None:
        """懒加载 App 通道客户端（配置文件只读一次）。

        v1.8.0 起主循环开头就要用它取开门记录，所以从 _async_sync_app_messages
        里抽出来共用。文件不存在 / enabled=false 时 _app_msg_client 保持 None，
        集成行为与 1.6.0 完全一致（全部走开放平台）。
        """
        if self._app_cfg_checked:
            return
        self._app_cfg_checked = True
        cfg: dict[str, Any] | None = None
        try:
            cfg = await self.hass.async_add_executor_job(load_app_config, self._config_root)
        except AppMsgConfigError as err:
            self._app_msg_status = {"enabled": False, "last_error": str(err)}
            _LOGGER.warning("App 通道配置不可用：%s", err)
        if cfg:
            self._app_msg_client = EzvizAppMsgClient(
                async_get_clientsession(self.hass),
                str(cfg["account"]),
                str(cfg["password_md5"]),
                str(cfg["feature_code"]),
                self.client.device_serial,
            )
            self._app_msg_status = {"enabled": True, "last_error": None}
            _LOGGER.info(
                "App 通道已启用（配置文件 %s，v1.8.0 作为主数据源）", APP_MSG_CONFIG_FILE
            )
        else:
            self._app_msg_status = {"enabled": False}

    async def _async_get_door_records(self, start_ms: int, end_ms: int) -> list[DoorRecord]:
        """取开门记录（v1.8.0 降调用量核心）。

        实测 2026-11-18 的关键发现（决定了这里的策略）：
          - App unifiedmsg/list（云端消息表）**稳定**：keylock 断供期间仍有今天 3 条开门，
            且 customerInfo 带 user + IOT1SmartlockOpenDoorWay，信息完整。
          - App openEvents / 开放平台 keylock 都依赖**设备在线查询**：门锁休眠时 data=null。
            实测 keylock 从 11-17 21:05 停更、openEvents 同期 0 条。

        所以：
          * 今天的开门实时性 → 交给 _async_sync_app_messages（每 30s 拉 unifiedmsg，
            走 api.ys7.com 不占开放平台配额，v1.7.0 已实战验证能补开门行）。
          * 这里只负责**冷启动重建历史 + 关门事件(9)**，用开放平台 keylock：
            - 冷启动必拉一次（全量历史）
            - App 通道启用后降频到 10 分钟一次（原来每 20s = 4320 次/天 → 144 次/天）
          * App 未配置时回到原来的每轮都拉（行为与 1.6.0 一致）。
        """
        now_mono = time.monotonic()
        app_on = self._app_msg_client is not None
        cold = not getattr(self, "_door_records_synced", False)
        if app_on and not cold:
            elapsed = now_mono - getattr(self, "_last_door_records_sync", 0.0)
            if elapsed < self._device_probe_interval_s:
                # 本轮不拉 keylock，返回空；已有事件池不受影响，
                # 今天的开门由 _async_sync_app_messages 持续补入。
                self._app_msg_status["source"] = "app(unifiedmsg)"
                return []

        self._last_door_records_sync = now_mono
        self._door_records_synced = True
        try:
            records = await self.client.async_get_door_records_range(
                start_ms, end_ms, max_pages=6
            )
            if app_on:
                self._api_call_stats["open_door_fallback"] += 1
            self._app_msg_status["source"] = "keylock" if records else "keylock(empty)"
            return records
        except EzvizApiError as err:
            # keylock 断供/异常时不能让整个集成 unavailable —— unifiedmsg 会兜住今天的开门
            _LOGGER.debug("keylock/open/list 拉取失败（今天的开门由 App 消息通道兜底）：%s", err)
            return []

    async def _async_update_data(self) -> LockData:
        _t0 = time.perf_counter()
        _ph = {}

        def _mark(name: str) -> None:
            _ph[name] = time.perf_counter() - _t0

        # 干净录像白名单：只把逐帧验收 0% 绿的片子交给前端匹配开门记录
        try:
            await self._async_load_clean_clips()
        except Exception as err:  # noqa: BLE001 - 索引异常不能拖垮整个集成
            _LOGGER.debug("加载干净录像索引失败：%s", err)

        # v1.8.0：主循环开头就要用 App 客户端取开门记录，先确保它已初始化
        try:
            await self._async_ensure_app_client()
        except Exception as err:  # noqa: BLE001 - 初始化失败不能拖垮集成
            _LOGGER.debug("App 通道初始化失败：%s", err)

        # 首轮：把最近 24 小时「有开门但没图」的事件排进云封面回溯队列。
        # 这些多是重启后从历史重建的记录（重建路径不抓拍），云封面不依赖设备在线，
        # 正好能补上；跑一次即可，之后新事件走实时队列。
        if not self._backfill_queued:
            self._backfill_queued = True
            try:
                self._queue_backfill_covers()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("云封面回溯排队失败")

        try:
            # 设备名只需取一次；设备信息探测（在线/WiFi/信号）本身由 health 负责，
            # 那里已降到 10 分钟一次。这里只在首次补个名字。
            if self._device_name is None:
                try:
                    info = await self.client.async_get_device_info()
                    self._device_name = info.get("deviceName") or info.get("name")
                except EzvizApiError as err:
                    _LOGGER.debug("获取设备信息失败（不影响主流程）：%s", err)
                _mark("设备信息")

            # 今日 00:00（本地时区）作为统计起点；再往前多看 6 小时，
            # 覆盖「昨天深夜开门、今天凌晨才关门」这类跨天配对。
            today_start = dt_util.start_of_local_day()
            today_start_ms = int(today_start.timestamp() * 1000)
            now_ms = int(dt_util.now().timestamp() * 1000)
            # v1.8.0：App openEvents 优先，失败回退开放平台 keylock/open/list
            records = await self._async_get_door_records(
                today_start_ms - 6 * 3_600_000,
                now_ms + 60_000,
            )
            _mark("开门记录")
        except EzvizApiError as err:
            raise UpdateFailed(str(err)) from err

        fresh = [r for r in records if r.uid not in self._seen]

        if not self._today_synced:
            # 重建失败（网络抖动、接口异常、磁盘问题）不能拖垮整个集成，
            # 否则实体会变成「不可用」，前端页面直接打不开。
            try:
                # 本轮重新分配「哪张告警图属于哪次开门」，先清空上一轮的占用记录
                self._used_alarm_ids.clear()
                # 每次进程启动（含重启）都用云端今日记录重建统计：
                # 旧版只在「首次安装」时建一次，重启后今天的计数就丢了 -> 数字偏小。
                self._seen.update(r.uid for r in records)
                today_recs = [r for r in records if r.open_time >= today_start_ms]
                # 非今天的旧缓存保留；今天的部分以云端为准重建，避免重复或残缺
                kept = [ev for ev in self._events if ev.open_time_ms < today_start_ms]
                rebuilt = await self._build_events(today_recs, download_media=False, lightweight=True)
                # 最近 8 次「门外开门」走完整流程（配对告警、算人数），但**不下载图片**：
                # 启动阶段绝不能做网络下载，否则 bootstrap 会被拖到几百秒，前端直接打不开。
                # 图片统一交给启动完成后的后台任务 _async_late_fill_snapshots 补齐。
                outside_recs = sorted(
                    (r for r in today_recs if r.open_type in OUTSIDE_OPEN_TYPES),
                    key=lambda r: r.open_time,
                )
                # 按时间升序处理：早的开门先占走属于自己的告警图，晚的才不会抢错
                rich = outside_recs[-8:]
                if rich:
                    rich_events = await self._build_events(rich, download_media=False)
                    by_uid = {e.uid: e for e in rich_events}
                    rebuilt = [by_uid.get(e.uid, e) for e in rebuilt]

                # 重建时云端可能已经拿不到旧告警图了，沿用本地缓存里的图，避免重启后图片消失
                prev_by_uid = {ev.uid: ev for ev in self._events}
                for ev in rebuilt:
                    # 室内按键 / 机械钥匙本来就没有门外画面，历史遗留的错图要主动清掉，
                    # 否则「21:31:37 室内开门拿着 21:31:57 人脸图」会一直粘在记录上
                    if ev.open_type not in OUTSIDE_OPEN_TYPES:
                        ev.snapshot_url = None
                        ev.snapshot_path = None
                        ev.snapshot_source = None
                        ev.capture_ms = None
                        continue
                    prev = prev_by_uid.get(ev.uid)
                    if prev is None:
                        continue
                    if not ev.snapshot_url and prev.snapshot_url:
                        ev.snapshot_url = prev.snapshot_url
                        ev.snapshot_path = prev.snapshot_path
                        ev.snapshot_source = prev.snapshot_source
                        ev.capture_ms = prev.capture_ms
                    # 不再沿用旧 clip_url：历史上它是「事件即时抓直播」的错片，
                    # 重启后统一由 _async_backfill_clips 按 SD 白名单重新认领，
                    # 避免错录像粘在开门记录上复活。
                    if not ev.alarm_id and prev.alarm_id:
                        ev.alarm_id = prev.alarm_id
                        ev.alarm_type = prev.alarm_type
                        ev.alarm_type_name = prev.alarm_type_name
                    if ev.person_count_source == "default" and prev.person_count_source not in ("default", "n/a"):
                        ev.person_count = prev.person_count
                        ev.person_count_source = prev.person_count_source
                merged: dict[str, DoorEvent] = {e.uid: e for e in kept}
                merged.update({e.uid: e for e in rebuilt})
                self._events = sorted(
                    merged.values(), key=lambda e: e.open_time_ms, reverse=True
                )[:MAX_KEEP_EVENTS]
                # 云端告警图有时效，过期就再也拿不回来；本地 www 下的文件才是可靠存档
                await self._async_backfill_snapshots(self._events)
                await self._async_backfill_clips(self._events)
                self._today_synced = True
                self._initialized = True
                await self._async_save_state()
                _mark("今日重建")
                _LOGGER.info(
                    "EZVIZ Lock 同步完成：登记 %d 条记录（其中今日 %d 条），后续新增记录将实时推送",
                    len(records), len(today_recs),
                )
                # 这批记录已并入内存与统计，不再作为「新事件」推送，避免重启后刷一堆历史通知
                fresh = []
            except Exception:  # noqa: BLE001 - 兜底：改用本地缓存继续运行
                _LOGGER.exception("今日记录重建失败，先用本地缓存继续运行")
                self._today_synced = True
                self._initialized = True
                fresh = []

        if fresh:
            _LOGGER.info("发现 %d 条新开门记录", len(fresh))
            new_events = await self._build_events(fresh, download_media=True)

            # 告警通道经常没有图，这里对「刚发现、且没拿到告警原图」的真实开门补拍一张。
            # 一轮里最多补拍一次（多次抓拍拿到的是几乎同一帧，浪费且加重设备唤醒）。
            await self._async_capture_for_new(new_events)

            for ev in new_events:
                self._seen.add(ev.uid)
                self._events.insert(0, ev)
                # 事件即时抓录已停用：取流太慢，抓到的画面比开门晚 20~30 秒，
                # 与告警抓拍对不上（主人 11-12 19:26 反馈「录像都是错的」）。
                # 开门录像统一由 _async_backfill_clips 从 SD 回放白名单按开门
                # 时刻 ±CLIP_MATCH_WINDOW_S（20 秒）认领；本轮新事件在下方节流回填里自动挂上。
                if not ev.is_close and ENABLE_LIVE_CAPTURE:
                    self.hass.async_create_task(self._async_record_door_live(ev))
                if ev.is_close:
                    _LOGGER.debug("关门事件：%s", ev.open_time_iso)
                    continue
                self.hass.bus.async_fire(EVENT_DOOR_OPENED, ev.to_dict())
                _LOGGER.info(
                    "开门事件：%s / %s / %s / %d 人 / 图片=%s(%s)",
                    ev.open_time_iso, ev.user_name, ev.open_type_name,
                    ev.person_count, bool(ev.snapshot_url), ev.snapshot_source or "无",
                )
            self._events = self._events[:MAX_KEEP_EVENTS]
            await self._async_save_state()

        # 目录遍历走线程池，避免在事件循环里做阻塞 IO
        await self.hass.async_add_executor_job(self._cleanup_snapshots)
        _mark("清理")

        # 告警驱动的开门兜底：keylock/open/list 停更时，用 alarm/list 的开门告警
        # 补开门行（含人员/方式/视频）；其它类型告警只留痕。失败不影响开门主流程。
        try:
            await self._async_sync_alarm_opens(today_start_ms, now_ms)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 兜底出问题不能让集成不可用
            _LOGGER.exception("告警兜底开门同步失败")
        _mark("告警兜底")

        # App 账号统一消息：第三条独立认证通道。可选增强——没有配置文件时
        # 下面这个方法直接返回，集成行为与 1.6.0 一字不差。
        try:
            await self._async_sync_app_messages(today_start_ms, now_ms)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 第三通道必须完全隔离
            _LOGGER.exception("App 消息通道同步失败")
        _mark("App消息")

        # 徘徊唤醒：与开门记录是两条独立数据源，单独同步（失败不影响开门主流程）
        try:
            await self._async_sync_loiter(today_start_ms, now_ms)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 徘徊出问题不能让整个集成不可用
            _LOGGER.exception("同步徘徊事件失败")
        _mark("徘徊同步")

        # v1.9.0 云封面补图：给「等封面」的事件匹配云录像片段并下载。
        # 拉列表按 60 秒节流，一次请求可匹配多条事件；失败只降级不报错。
        try:
            await self._async_sync_cloud_covers()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 补图失败不能影响主链路
            _LOGGER.exception("云封面补图失败")
        _mark("云封面")

        # SD 回放录像认领：拉取器每小时补一段，补完无需重启就能挂到对应开门记录。
        # 节流 120 秒，避免每轮 20s 都遍历媒体目录 + 复核白名单 md5。
        _now_mono = time.monotonic()
        if _now_mono - getattr(self, "_last_clip_backfill", 0.0) >= 120:
            self._last_clip_backfill = _now_mono
            try:
                changed = await self._async_backfill_clips(self._events)
                if changed:
                    await self._async_save_state()
                    self.async_update_listeners()
            except Exception:  # noqa: BLE001 - 回填失败不能让实体变不可用
                _LOGGER.exception("认领 SD 开门录像失败")
        _mark("录像认领")

        # 健康度（电量 / WiFi 信号 / 在线 / 成员凭证）：内部自带节流与异常隔离，
        # 任何失败都只落到 health.last_error，绝不让整台锁变 unavailable。
        try:
            await self.health.async_tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("门锁健康度探测失败")
        _mark("健康度")

        today = dt_util.now().date()

        def _is_today(ev: DoorEvent) -> bool:
            return dt_util.as_local(
                datetime.fromtimestamp(ev.open_time_ms / 1000, tz=dt_util.UTC)
            ).date() == today

        # 只有真实开门才算「开门」，关门事件单独统计
        opens = [ev for ev in self._events if not ev.is_close]
        closes = [ev for ev in self._events if ev.is_close]

        today_opens = [ev for ev in opens if _is_today(ev)]
        today_closes = [ev for ev in closes if _is_today(ev)]
        # 同一扇门会重复上报关门（实测 6 秒内能报两条），去重后才接近真实「关了几次」
        deduped_closes = self._dedupe_close_events(today_closes)

        def _loiter_is_today(lo: LoiterEvent) -> bool:
            return dt_util.as_local(
                datetime.fromtimestamp(lo.time_ms / 1000, tz=dt_util.UTC)
            ).date() == today

        today_loiter = [lo for lo in self._loiter if _loiter_is_today(lo)]

        _mark("组装")
        total = time.perf_counter() - _t0
        if total > 10:
            _LOGGER.warning("本轮刷新偏慢 %.1fs：%s", total, _ph)
        return LockData(
            events=opens[:100],
            last=opens[0] if opens else None,
            today_count=len(today_opens),
            today_close_count=len(deduped_closes),
            today_close_raw=len(today_closes),
            today_events=today_opens,
            last_close=closes[0] if closes else None,
            total_count=len(self._events),
            online=self.health.data.online,
            device_name=self._device_name,
            loiter_events=self._loiter[:LOITER_KEEP_MAX],
            loiter_today_count=len(today_loiter),
            loiter_last=self._loiter[0] if self._loiter else None,
            other_alarms=self._other_alarms,
            health=self.health.data,
        )

    async def _async_capture_for_new(self, events: list[DoorEvent]) -> None:
        """给刚发现的真实开门配图（v1.9.1：capture 保底 + 云封面兜底）。

        顺序固定「先抓拍、后补封面」：
          1. 立刻 ``device/capture`` 拍一张实时画面 —— 到得最快，也对得上门外的人；
          2. 抓拍失败（设备睡着 / 平台报错 / 返回空）时，这条事件才排进「等云封面」
             队列，由轮询去云存储片段取 ``coverPic`` 兜底（不吃配额、不依赖设备在线）；
          3. 抓拍成功时，同一批里其余还没图的事件也排进封面队列（它们本来就无图）。

        云封面与告警抓拍同源：设备没拍的事件云里也没有，所以只作兜底。
        ``CONF_COVER_FIRST=True`` 才走旧的 C 模式（先等封面、超时再抓拍），默认关闭。
        """
        if not self._capture_enabled:
            return
        # 室内按键（5）/机械钥匙（8）补拍到的是同一个门外镜头，拍了也对不上人，直接不拍
        targets = [
            e
            for e in events
            if not e.is_close and not e.snapshot_path and e.open_type in OUTSIDE_OPEN_TYPES
        ]
        if not targets:
            return

        if self._cover_first and self._app_msg_client is not None:
            # 旧 C 模式：全部入队等封面，一轮里多条事件各自去匹配各自的片段。
            for ev in targets:
                self._queue_cover(ev)
            _LOGGER.debug("已排入云封面等待队列 %d 条", len(targets))
            return

        # B 模式：立刻抓拍最新一条（画面最接近本次开门）
        target = max(targets, key=lambda e: e.open_time_ms)
        url: str | None = None
        try:
            url = await self.client.async_capture(channel_no=self._channel_no)
        except EzvizApiError as err:
            _LOGGER.warning("实时抓拍失败，改用云封面兜底（%s）：%s", target.open_time_iso, err)

        if not url:
            self._cover_stats["capture_fail"] += 1
            if self._app_msg_client is None:
                _LOGGER.warning("实时抓拍未拿到图片，且 App 通道未启用 → 该批记录将没有图片")
                return
            for ev in targets:
                self._queue_cover(ev)
            _LOGGER.info("实时抓拍无图 %d 条，已排入云封面兜底队列", len(targets))
            return

        await self._async_archive_capture(target, url)

        # 抓拍只覆盖了最新一条；同批其余事件本来就无图，交给云封面补（有就补，没有算了）
        rest = [e for e in targets if e is not target]
        if rest and self._app_msg_client is not None:
            for ev in rest:
                self._queue_cover(ev)
            _LOGGER.debug("同批其余 %d 条无图事件已排入云封面队列", len(rest))

    # ------------------------------------------------------------------ 云封面补图
    def _queue_cover(self, ev: DoorEvent, *, force: bool = False) -> None:
        """把一条无图事件排进「等云封面」队列。

        force=True 用于历史回溯：即使已过 wait 窗口也排一次，deadline 立即生效
        （下一轮就会拉列表尝试，拉不到就结束，不会长时间占着队列）。
        """
        if not ev.uid or ev.snapshot_path:
            return
        if ev.uid in self._cover_done or ev.uid in self._cover_pending:
            return
        self._cover_pending[ev.uid] = ev
        wait = COVER_WAIT_S if not force else 0
        self._cover_deadline[ev.uid] = time.monotonic() + wait

    def _queue_backfill_covers(self) -> None:
        """启动后回溯：最近 24 小时内「门外开门但没有图」的事件也去试着补一张。

        这些记录多半是集成重启后从历史重建的（重建路径不抓拍），或者当时
        capture 失败。云封面不依赖设备在线，正好能补上；纯 HTTP 拉图，不动设备。
        """
        if self._app_msg_client is None:
            return
        cutoff = int(time.time() * 1000) - COVER_BACKFILL_AGE_S * 1000
        n = 0
        for ev in self._events:
            if ev.is_close or ev.open_type not in OUTSIDE_OPEN_TYPES:
                continue
            if ev.snapshot_path or ev.open_time_ms < cutoff:
                continue
            if ev.uid in self._cover_done or ev.uid in self._cover_pending:
                continue
            self._queue_cover(ev, force=True)
            n += 1
        if n:
            _LOGGER.info("云封面回溯：%d 条历史无图事件加入补图队列", n)

    def _match_cloud_clip(self, open_ms: int) -> AppCloudClip | None:
        """在云录像片段里找与开门时刻对应的那一段。

        判定用「片段区间 ± 容差」，不是「最近片段」：宁可没图，也不许把
        58 秒后另一件事的录像贴给这次开门（那是造假）。
        """
        best: AppCloudClip | None = None
        for c in self._cloud_clips:
            if not c.cover_url or c.key in self._cloud_clip_seen:
                continue
            lo = c.start_ms - COVER_MATCH_BEFORE_S * 1000
            hi = c.start_ms + max(c.video_long_ms, 1000) + COVER_MATCH_AFTER_S * 1000
            if lo <= open_ms <= hi:
                if best is None or abs(open_ms - c.start_ms) < abs(open_ms - best.start_ms):
                    best = c
        return best

    async def _async_sync_cloud_covers(self) -> None:
        """给「等封面」的事件匹配云录像片段并下载。

        每个轮询周期调用一次；拉列表本身按 ``COVER_POLL_INTERVAL_S`` 节流
        （一次请求可匹配多条事件，成本极低）。等到 deadline 仍无片段 → 记 miss；
        只有 C 模式（``_cover_first``）才会在此时回落 device/capture，B 模式下
        抓拍已经在前面做过了，这里直接放弃（该条记录无图）。
        """
        if self._app_msg_client is None:
            return
        if not self._cover_pending:
            return

        now_mono = time.monotonic()
        if now_mono - self._cloud_clips_at >= COVER_POLL_INTERVAL_S:
            self._cloud_clips_at = now_mono
            try:
                clips = await self._app_msg_client.async_get_cloud_clips()
                self._cloud_clips = clips
                _LOGGER.debug("云录像列表：%d 段（最早 %s）", len(clips), clips[0].start_ms if clips else "-")
            except (AppDataUnavailable, AppMsgError, AppMsgConfigError) as err:
                # 没开通云存储 / 设备刚睡着都可能走到这里，降级即可，不打扰用户
                _LOGGER.debug("云录像列表暂不可用：%s", err)

        started = 0
        for uid, ev in list(self._cover_pending.items()):
            if uid in self._cover_tasks:
                continue
            clip = self._match_cloud_clip(ev.open_time_ms)
            if clip is not None and started < COVER_MAX_PER_CYCLE:
                self._cloud_clip_seen.add(clip.key)
                self._cover_tasks.add(uid)
                started += 1
                self.hass.async_create_task(self._async_fetch_cover(uid, ev, clip))
                continue
            # 过了等待窗口还没匹配到 → 放弃（C 模式才回落 capture，B 模式到此为止）
            if now_mono >= self._cover_deadline.get(uid, 0):
                self._cover_pending.pop(uid, None)
                self._cover_deadline.pop(uid, None)
                self._cover_done.add(uid)
                self._cover_stats["cloud_miss"] += 1
                _LOGGER.info(
                    "云封面未匹配到片段（%s，%s），%s",
                    ev.open_time_iso,
                    ev.open_type_name,
                    "改用 device/capture 补拍"
                    if (self._cover_first and self._capture_enabled)
                    else "该条记录将无图",
                )
                if self._cover_first and self._capture_enabled:
                    self.hass.async_create_task(self._async_capture_fallback(ev))

    async def _async_fetch_cover(self, uid: str, ev: DoorEvent, clip: AppCloudClip) -> None:
        """下载封面图落盘，并挂到事件上。"""
        try:
            if not clip.cover_url:
                raise EzvizApiError("封面 URL 为空")
            stamp = dt_util.as_local(
                datetime.fromtimestamp(ev.open_time_ms / 1000, tz=dt_util.UTC)
            )
            date_dir = self._snapshot_root / stamp.strftime("%Y-%m-%d")
            slug = OPEN_TYPE_SLUGS.get(ev.open_type, "unknown")
            dest = date_dir / f"{stamp.strftime('%H%M%S')}_{slug}_cloud.jpg"
            written = await self.client.async_download(
                clip.cover_url, dest, max_bytes=20 * 1024 * 1024
            )
            if written < COVER_MIN_BYTES:
                raise EzvizApiError(f"封面图过小（{written}B）")

            old_path = ev.snapshot_path
            ev.snapshot_path = str(dest)
            ev.snapshot_url = self._abs_to_url(dest)
            ev.snapshot_source = "cloud"
            # 图片的真实拍摄时刻 = 片段起点，不是「我发现它的时刻」
            ev.capture_ms = clip.start_ms
            if old_path and old_path != str(dest):
                await self._async_remove_file(old_path)

            self._cover_stats["cloud_ok"] += 1
            # 回溯 = 图片对应的开门时刻已经过去很久，不是这次轮询实时发现的
            if int(time.time() * 1000) - ev.open_time_ms > COVER_WAIT_S * 1000:
                self._cover_stats["backfill"] += 1
            _LOGGER.info(
                "云封面补图成功：%s（片段 %s，偏差 %+d 秒，%d KB）",
                ev.snapshot_url,
                dt_util.as_local(datetime.fromtimestamp(clip.start_ms / 1000, tz=dt_util.UTC)).strftime("%H:%M:%S"),
                int((ev.open_time_ms - clip.start_ms) / 1000),
                written // 1024,
            )
        except (EzvizApiError, OSError) as err:
            _LOGGER.warning("云封面下载失败（%s）：%s", ev.open_time_iso, err)
            self._cover_stats["cloud_miss"] += 1
        except Exception:  # noqa: BLE001 - 补图失败绝不能影响主循环
            _LOGGER.exception("云封面补图异常：%s", ev.open_time_iso)
            self._cover_stats["cloud_miss"] += 1
        finally:
            self._cover_tasks.discard(uid)
            self._cover_pending.pop(uid, None)
            self._cover_deadline.pop(uid, None)
            self._cover_done.add(uid)
            if ev.snapshot_path:
                await self._async_save_state()

    async def _async_capture_fallback(self, ev: DoorEvent) -> None:
        """【仅 C 模式】等满窗口仍无云封面时，才动 device/capture。

        B 模式（默认，capture 保底 + 封面兜底）下抓拍在事件发现时已经做过，
        不会再走到这里 —— 抓不到就是抓不到，不重复打扰设备。

        同一批里可能有好几条同时过期（比如一轮同步发现 4 条开门），这里做 60 秒
        节流：相邻两次兜底抓拍至少隔 60 秒，且只给最早的那条拍（抓到的画面
        对后来的事件来说已经晚了，拍了也对不上人）。
        """
        now_mono = time.monotonic()
        if now_mono - getattr(self, "_last_capture_at", 0.0) < 60:
            _LOGGER.info("兜底抓拍已在上分钟内执行过，跳过（%s）", ev.open_time_iso)
            return
        # 让同批里最早的事件先拍；后面的在 60 秒节流里自动跳过
        self._last_capture_at = now_mono
        try:
            url = await self.client.async_capture(channel_no=self._channel_no)
        except EzvizApiError as err:
            _LOGGER.warning("兜底抓拍失败（%s）：%s", ev.open_time_iso, err)
            return
        if not url:
            _LOGGER.warning("兜底抓拍未返回图片地址（%s）", ev.open_time_iso)
            return
        self._cover_stats["capture_fallback"] += 1
        await self._async_archive_capture(ev, url)

    # ------------------------------------------------------------------ 告警兜底
    async def _async_sync_alarm_opens(self, today_start_ms: int, now_ms: int) -> None:
        """告警驱动的开门兜底 + 其它告警留痕。

        背景：keylock/open/list 会周期性停更（实测 11-16 中午、11-17 21:05），
        但同一时刻 alarm/list 的 17011 开门告警仍在上报。本方法把开门类告警
        (OPEN_ALARM_TYPES) 转成开门事件补进开门表，使页面继续更新并认领 SD 录像；
        customerInfo 里带 user + 开门方式，正好补上告警"没人名/方式"的短板。

        分流规则（主人 11-18 08:40 明确要求）：
          - 开门类(17011/10013) → 进开门表 + 匹配视频
          - 徘徊(15504)         → 交给 _async_sync_loiter（本方法跳过，不重复建）
          - 其它告警类型        → 只存 self._other_alarms 留痕，不进表、不配视频

        去重：alarm_id 记忆 + 与 keylock 事件按 ALARM_LOOKBACK/AHEAD 时间窗比对，
        避免同一开门在 keylock 与 alarm 两条链路各生成一行。绝不下载媒体（轮询内）。

        节流：alarm/list 每 60 秒拉一次即可（告警上报本身有几秒延迟）；首轮必跑。
        """
        now_mono = time.monotonic()
        # v1.8.0：App 通道启用时，unifiedmsg 已完整覆盖开门兜底+其它告警留痕，
        # 开放平台 alarm/list 只作回退，降频到 10 分钟一次（省约 1400 次/天）。
        # App 未启用时维持原来的 60 秒。
        interval = 600 if self._app_msg_client is not None else 60
        if (
            getattr(self, "_alarm_opens_synced", False)
            and now_mono - getattr(self, "_last_alarm_opens_sync", 0.0) < interval
        ):
            return
        self._last_alarm_opens_sync = now_mono
        self._alarm_opens_synced = True
        try:
            alarms = await self.client.async_get_alarms(
                today_start_ms - ALARM_LOOKBACK_MS, now_ms + 60_000,
                channel_no=self._channel_no, page_size=50,
            )
            if self._app_msg_client is not None:
                self._api_call_stats["alarm_fallback"] += 1
        except EzvizApiError as err:
            _LOGGER.debug("拉取告警（开门兜底）失败：%s", err)
            return

        added: list[DoorEvent] = []
        other_new: list[dict[str, Any]] = []

        for alarm in alarms:
            atype = alarm.alarm_type
            aid = str(alarm.alarm_id or "") or f"{alarm.alarm_time}|{atype}"

            # ---- 徘徊单列：交给专门的同步方法，这里跳过 ----
            if atype == ALARM_TYPE_LOITER:
                continue

            # ---- 开门类：转 DoorEvent 补开门行 ----
            if atype in OPEN_ALARM_TYPES:
                if aid in self._alarm_open_seen:
                    continue
                # 与 keylock 已登记的开门按时间窗去重（同一时刻不补第二行）
                lo = alarm.alarm_time - ALARM_LOOKBACK_MS
                hi = alarm.alarm_time + ALARM_LOOKAHEAD_MS
                if any(
                    not ev.is_close and lo <= ev.open_time_ms <= hi
                    for ev in self._events
                ):
                    self._alarm_open_seen.add(aid)
                    continue

                info = _decode_customer_info(alarm.customer_info)
                otype, way_raw = _open_way_to_type(info)
                raw_name = ""
                for key in USER_KEYS:
                    if info.get(key):
                        raw_name = str(info[key]).strip()
                        break
                user_name = self.display_user_name(raw_name) if raw_name else "未知用户"

                record = DoorRecord(
                    user_name=user_name,
                    open_type=otype,
                    open_time=alarm.alarm_time,
                )
                try:
                    ev = await self._build_one(
                        record, download_media=False, alarms_all=[alarm]
                    )
                except Exception:  # noqa: BLE001 - 单条失败不能拖垮兜底
                    _LOGGER.exception("告警兜底构建开门事件失败：%s", aid)
                    continue
                # 打上来源标记，页面/排查都能区分是 keylock 还是告警补的
                ev.source = "alarm"
                ev.alarm_id = ev.alarm_id or aid
                ev.alarm_type = ev.alarm_type or atype
                ev.alarm_type_name = ev.alarm_type_name or self.alarm_type_name(atype)
                added.append(ev)
                self._alarm_open_seen.add(aid)
                continue

            # ---- 其它告警：只留痕，不进开门表/徘徊表、不配视频 ----
            if aid in self._other_alarm_seen:
                continue
            when = dt_util.as_local(
                datetime.fromtimestamp(alarm.alarm_time / 1000, tz=dt_util.UTC)
            ).isoformat()
            self._other_alarm_seen.add(aid)
            other_new.append({
                "alarm_id": aid,
                "alarm_type": atype,
                "alarm_type_name": self.alarm_type_name(atype) or alarm.alarm_name or "未知告警",
                "time": when,
                "time_ms": alarm.alarm_time,
                "has_pic": bool(alarm.pic_url),
                "rec_state": alarm.rec_state,
            })

        # 开门兜底事件并入事件池（新→旧），并触发实体刷新
        if added:
            known = {e.uid for e in self._events}
            for ev in sorted(added, key=lambda e: e.open_time_ms, reverse=True):
                if ev.uid in known:
                    continue
                self._events.append(ev)
                known.add(ev.uid)
                self._seen.add(ev.uid)
                self.hass.bus.async_fire(EVENT_DOOR_OPENED, ev.to_dict())
                _LOGGER.info(
                    "告警兜底补开门行：%s / %s / %s（来源 alarm %s）",
                    ev.open_time_iso, ev.user_name, ev.open_type_name, ev.alarm_id,
                )
            self._events.sort(key=lambda e: e.open_time_ms, reverse=True)
            self._events = self._events[:MAX_KEEP_EVENTS]
            # 让新补的开门行尽快认领 SD 录像（不等 120s 节流）
            try:
                await self._async_backfill_clips(self._events)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("告警兜底后认领录像失败（下轮重试）")

        if other_new:
            self._other_alarms = (other_new + self._other_alarms)[:OTHER_ALARMS_KEEP_MAX]
            _LOGGER.debug("其它告警留痕 %d 条（不配视频）", len(other_new))

        if added or other_new:
            await self._async_save_state()

    # ------------------------------------------------------- App 消息第三通道
    async def _async_sync_app_messages(self, today_start_ms: int, now_ms: int) -> None:
        """从 App 账号「统一消息列表」补开门 / 徘徊行，其它类型只留痕。

        定位（2026-11-18 实测）：第三条独立认证通道。端点
        ``api.ys7.com/v3/unifiedmsg/list?stype=9904,2701`` 与开放平台
        ``open.ys7.com/api/lapp/alarm/list`` 数据表现同源但凭据体系独立。
        只有当 keylock/open/list 与 alarm/list 两条链路都没登记该事件时才补一行，
        平时完全不干扰现有逻辑。

        去重三层：
          1. msgId 记忆（天然主键，且已持久化，重启不重复）
          2. 与 self._events 已有开门按 ±20 秒窗比对
          3. DoorEvent.uid 由 _build_one 依据时刻/方式/人员生成，入池前再查一次
        分流与告警兜底一致：开门类(17011/10013)进开门表、15504 进徘徊表、
        其余（含门铃呼叫 20002）只存 other_alarms，不配视频。
        室内开门(5) 不配图/视频：_build_one 的 OUTSIDE_OPEN_TYPES 已保证。
        v1.9.3：开门/徘徊的告警原图交给后台任务下载（不阻塞本方法），
        SD 录像回填仍只走本地存档与白名单。
        """
        now_mono = time.monotonic()
        if (
            getattr(self, "_app_msg_synced", False)
            and now_mono - getattr(self, "_last_app_msg_sync", 0.0) < APP_MSG_SYNC_INTERVAL_S
        ):
            return
        self._last_app_msg_sync = now_mono
        self._app_msg_synced = True

        # 客户端已在主循环开头由 _async_ensure_app_client 初始化
        if self._app_msg_client is None:
            return

        day = dt_util.now().strftime("%Y%m%d")
        try:
            messages = await self._app_msg_client.async_get_messages(day)
        except AppMsgError as err:
            self._app_msg_status["last_error"] = str(err)
            _LOGGER.debug("拉取 App 统一消息失败：%s", err)
            return

        self._app_msg_status["last_error"] = None
        self._app_msg_status["last_sync"] = dt_util.now().isoformat()
        self._app_msg_status["messages_today"] = len(messages)

        added_opens: list[DoorEvent] = []
        # v1.9.3：开门事件入池时一起记下它自己的告警，入池后后台下载原图。
        # 没有这一步，appmsg 建的行就永远没有 snapshot_url（keylock 主循环
        # 因 uid 已在 _seen 里而不再处理它）。
        pending_open_alarm: dict[str, AlarmRecord] = {}
        added_loiter: list[LoiterEvent] = []
        other_new: list[dict[str, Any]] = []
        existing_other_ids = {
            str(a.get("alarm_id")) for a in self._other_alarms if a.get("alarm_id")
        }

        for msg in messages:
            if msg.msg_id in self._app_msg_seen:
                continue
            self._app_msg_seen.add(msg.msg_id)
            atype = msg.alarm_type
            t = msg.event_time_ms
            if t <= 0 or t < today_start_ms - 60_000:
                continue

            # ---- 开门类：两条现有链路都没登记才补 ----
            if atype in OPEN_ALARM_TYPES:
                lo = t - APP_MSG_OPEN_LOOKBACK_MS
                hi = t + APP_MSG_OPEN_LOOKAHEAD_MS
                if any(
                    not ev.is_close and lo <= ev.open_time_ms <= hi
                    for ev in self._events
                ):
                    continue
                info = _decode_customer_info(msg.customer_info)
                otype, _way = _open_way_to_type(info)
                raw_name = ""
                for key in USER_KEYS:
                    if info.get(key):
                        raw_name = str(info[key]).strip()
                        break
                record = DoorRecord(
                    user_name=raw_name or "未知用户",
                    open_type=otype,
                    open_time=t,
                )
                # 构造一条等价告警，复用 _build_one 的人数估算与图片配对逻辑
                alarm = AlarmRecord(
                    alarm_id=msg.msg_id,
                    alarm_type=atype,
                    alarm_time=t,
                    pic_url=msg.pic_url,
                    channel_no=msg.channel,
                    rec_state=msg.rec_state,
                    pre_time=msg.pre_time,
                    delay_time=msg.delay_time,
                    customer_info=msg.customer_info,
                )
                try:
                    ev = await self._build_one(
                        record, download_media=False, alarms_all=[alarm]
                    )
                except Exception:  # noqa: BLE001 - 单条失败不拖垮整批
                    _LOGGER.exception("App 消息补开门行失败：%s", msg.msg_id)
                    continue
                ev.source = APP_MSG_SOURCE
                ev.alarm_id = ev.alarm_id or msg.msg_id
                ev.alarm_type = ev.alarm_type or atype
                ev.alarm_type_name = ev.alarm_type_name or self.alarm_type_name(atype)
                added_opens.append(ev)
                # v1.9.3：记下这条事件自己的告警，入池后后台下载那张门外原图。
                if (
                    msg.pic_url
                    and not ev.snapshot_path
                    and ev.open_type in OUTSIDE_OPEN_TYPES
                ):
                    pending_open_alarm[ev.uid] = alarm
                continue

            # ---- 徘徊：与已有徘徊行按时间窗去重；不主动触发录像下载 ----
            if atype == ALARM_TYPE_LOITER:
                if any(
                    abs(item.time_ms - t) <= APP_MSG_LOITER_WINDOW_MS
                    for item in self._loiter
                ):
                    continue
                uid = f"appmsg:{msg.msg_id}"
                if uid in self._loiter_seen:
                    continue
                lo_ev = LoiterEvent(
                    uid=uid,
                    alarm_type=atype,
                    alarm_type_name=self.alarm_type_name(atype) or "徘徊检测",
                    time_ms=t,
                    alarm_id=msg.msg_id,
                    pre_s=msg.pre_time or LOITER_DEFAULT_PRE_S,
                    post_s=msg.delay_time or LOITER_DEFAULT_POST_S,
                )
                self._loiter.append(lo_ev)
                self._loiter_seen.add(uid)
                added_loiter.append(lo_ev)
                # v1.8.0：App 通道成为主数据源后，徘徊抓拍图也走 App 的 pic 字段，
                # 否则关掉开放平台 alarm/list 轮询后徘徊记录会没有图。
                if (
                    msg.pic_url
                    and not lo_ev.snapshot_path
                    and uid not in self._loiter_snap_tasks
                    and uid not in self._loiter_img_fail
                ):
                    self._loiter_snap_tasks.add(uid)
                    self.hass.async_create_task(
                        self._async_bg_loiter_snapshot(lo_ev, msg.pic_url)
                    )
                continue

            # ---- 其它（含门铃呼叫 20002）：只留痕，不进表、不配视频 ----
            if msg.msg_id in existing_other_ids:
                continue
            existing_other_ids.add(msg.msg_id)
            when = dt_util.as_local(
                datetime.fromtimestamp(t / 1000, tz=dt_util.UTC)
            ).isoformat()
            other_new.append({
                "alarm_id": msg.msg_id,
                "alarm_type": atype,
                "alarm_type_name": self.alarm_type_name(atype) or "未知告警",
                "time": when,
                "time_ms": t,
                "has_pic": bool(msg.pic_url),
                "rec_state": msg.rec_state,
                "title": msg.title,
                "source": APP_MSG_SOURCE,
            })

        if added_opens:
            known = {e.uid for e in self._events}
            appended_uids: set[str] = set()
            for ev in sorted(added_opens, key=lambda e: e.open_time_ms, reverse=True):
                if ev.uid in known:
                    continue
                self._events.append(ev)
                known.add(ev.uid)
                appended_uids.add(ev.uid)
                self._seen.add(ev.uid)
                self.hass.bus.async_fire(EVENT_DOOR_OPENED, ev.to_dict())
                _LOGGER.info(
                    "App 消息补开门行：%s / %s / %s（msgId %s）",
                    ev.open_time_iso, ev.user_name, ev.open_type_name, ev.alarm_id,
                )
            self._events.sort(key=lambda e: e.open_time_ms, reverse=True)
            self._events = self._events[:MAX_KEEP_EVENTS]
            # v1.9.3：只给真正入池的事件补图；重复被丢弃的别白下一遍。
            # 后台下载不阻塞本方法（轮询里 await 下载曾把启动拖到 90 秒）。
            for ev in added_opens:
                if ev.uid not in appended_uids or ev.uid not in pending_open_alarm:
                    continue
                if ev.uid in self._open_snap_tasks or ev.uid in self._open_img_fail:
                    continue
                self._open_snap_tasks.add(ev.uid)
                self.hass.async_create_task(
                    self._async_bg_open_snapshot(ev, pending_open_alarm[ev.uid])
                )

        if added_loiter:
            self._loiter.sort(key=lambda x: x.time_ms, reverse=True)
            for lo_ev in added_loiter:
                self.hass.bus.async_fire(EVENT_LOITER_DETECTED, lo_ev.to_dict())
            _LOGGER.info("App 消息补徘徊行 %d 条", len(added_loiter))

        if other_new:
            self._other_alarms = (other_new + self._other_alarms)[:OTHER_ALARMS_KEEP_MAX]
            _LOGGER.debug("App 消息其它告警留痕 %d 条（不配视频）", len(other_new))

        self._app_msg_status["added_opens"] = len(added_opens)
        self._app_msg_status["added_loiter"] = len(added_loiter)
        self._app_msg_status["other_logged"] = len(other_new)

        # v1.9.3：10:15 起那批无图事件的 msgId 已在 _app_msg_seen 里，正常分支
        # 不会再处理它们。这里拿本轮消息按 alarm_id / 时间窗配对，把当时漏下的
        # 告警原图补回来（纯 HTTP，不触发设备抓拍）。
        self._backfill_app_open_snapshots(messages)

        if added_opens or added_loiter or other_new:
            # 回填只走本地存档与 SD 白名单，不再额外请求云端
            try:
                await self._async_backfill_snapshots(self._events)
                await self._async_backfill_clips(self._events)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("App 消息补录后回填失败（下轮重试）")
            await self._async_save_state()
            self.async_update_listeners()

    def _backfill_app_open_snapshots(self, messages: list[Any]) -> None:
        """给今日已入池但无图的开门事件补下 App 消息里的告警原图。

        只匹配本轮拉到的消息：先按 alarm_id 精确配，配不上再按
        ±APP_MSG_OPEN_LOOKBACK_MS 时间窗取最近一张带图开门告警。
        绝不把别的时间点的图贴上来（宁可无图，不许造假）。
        """
        targets = [
            ev
            for ev in self._events
            if not ev.is_close
            and not ev.snapshot_path
            and ev.open_type in OUTSIDE_OPEN_TYPES
            and ev.uid not in self._open_snap_tasks
            and ev.uid not in self._open_img_fail
        ]
        if not targets or not messages:
            return

        def _to_alarm(msg: Any) -> AlarmRecord:
            return AlarmRecord(
                alarm_id=msg.msg_id,
                alarm_type=msg.alarm_type,
                alarm_time=msg.event_time_ms,
                pic_url=msg.pic_url,
                channel_no=msg.channel,
                rec_state=msg.rec_state,
                pre_time=msg.pre_time,
                delay_time=msg.delay_time,
                customer_info=msg.customer_info,
            )

        n = 0
        for ev in targets:
            alarm: AlarmRecord | None = None
            if ev.alarm_id:
                alarm = next(
                    (
                        _to_alarm(m)
                        for m in messages
                        if m.msg_id == ev.alarm_id and m.pic_url
                    ),
                    None,
                )
            if alarm is None:
                best_delta: int | None = None
                for m in messages:
                    if not m.pic_url or m.alarm_type not in OPEN_ALARM_TYPES:
                        continue
                    delta = abs(m.event_time_ms - ev.open_time_ms)
                    if delta > APP_MSG_OPEN_LOOKBACK_MS:
                        continue
                    if best_delta is None or delta < best_delta:
                        best_delta = delta
                        alarm = _to_alarm(m)
            if alarm is None:
                continue
            self._open_snap_tasks.add(ev.uid)
            self.hass.async_create_task(self._async_bg_open_snapshot(ev, alarm))
            n += 1
        if n:
            _LOGGER.info("App 消息补下开门告警图 %d 条（后台）", n)

    async def _async_bg_open_snapshot(self, ev: DoorEvent, alarm: AlarmRecord) -> None:
        """后台下载一张 App 消息带来的开门告警原图，失败只记一次不再重试。"""
        try:
            await self._async_archive_snapshot(ev, alarm)
            if ev.snapshot_path:
                await self._async_save_state()
                _LOGGER.info("开门告警图已存档：%s / %s", ev.open_time_iso, ev.snapshot_url)
            else:
                self._open_img_fail.add(ev.uid)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - 单张图失败不影响任何主流程
            _LOGGER.warning("开门告警图保存失败（%s）：%s", ev.open_time_iso, err)
            self._open_img_fail.add(ev.uid)
        finally:
            self._open_snap_tasks.discard(ev.uid)
            self.async_update_listeners()

    # ------------------------------------------------------------------ 徘徊唤醒
    async def _async_sync_loiter(self, today_start_ms: int, now_ms: int) -> None:
        """同步「徘徊唤醒」：拉告警 → 建事件 → 把图与录像丢给后台任务。

        徘徊和开门是两条独立链路：人只是路过、没开门，所以 keylock/open/list
        里什么都没有，只能从 alarm/list 里按 alarmType 认出来。

        **这里绝不做任何下载**：本方法在轮询里被 await，一旦下载就会拖住
        首次刷新（曾经把启动拖到 90 秒超时、所有实体变 unavailable）。
        """
        # v1.8.0：App 通道启用时，unifiedmsg 的 15504 分支已完整覆盖徘徊建行+抓拍图，
        # 开放平台 alarm/list 只作回退，降频到 10 分钟一次（原来每 20s，省约 4200 次/天）。
        now_mono = time.monotonic()
        interval = 600 if self._app_msg_client is not None else 0
        if (
            interval
            and getattr(self, "_loiter_synced", False)
            and now_mono - getattr(self, "_last_loiter_sync", 0.0) < interval
        ):
            return
        self._last_loiter_sync = now_mono

        # 首轮拉今天的全量做重建；之后只回看最近 15 分钟（覆盖几轮轮询的遗漏）
        lo_ms = today_start_ms if not self._loiter_synced else max(today_start_ms, now_ms - 900_000)
        try:
            alarms = await self.client.async_get_alarms(
                lo_ms, now_ms + 60_000, channel_no=self._channel_no
            )
            if self._app_msg_client is not None:
                self._api_call_stats["loiter_fallback"] += 1
        except EzvizApiError as err:
            _LOGGER.debug("拉取徘徊告警失败：%s", err)
            return

        existing_by_uid = {lo.uid: lo for lo in self._loiter}
        fresh: list[LoiterEvent] = []
        need_clip: list[LoiterEvent] = []
        cold_start = not self._loiter_synced

        for alarm in alarms:
            if alarm.alarm_type != ALARM_TYPE_LOITER:
                continue
            uid = alarm.alarm_id or f"{alarm.alarm_time}|{alarm.alarm_type}"
            # 手动删掉的记录不再复活：云端 alarm/list 在回看窗口内仍会返回这条告警
            if uid in self._deleted_loiter or (
                alarm.alarm_id and str(alarm.alarm_id) in self._deleted_loiter
            ):
                continue
            lo = existing_by_uid.get(uid)
            if lo is None:
                # v1.8.1：App 与 alarm/list 对同一次徘徊下发的是两个不同的 msg_id
                # （实测 MWMSEUS5BYO_83S0P3I00 与 MWMSEUS5BYO_85N62O200，time_ms 完全相同），
                # 仅按 uid 精确匹配挡不住，页面上就是两条一模一样的徘徊记录。
                # 这里再按「同类型 + 时间窗」认一次双胞胎：认到就沿用已有记录，
                # 后续补抓拍图 / 补录像仍走 lo，不会丢功能。
                twin = next(
                    (
                        item
                        for item in self._loiter
                        if item.alarm_type == alarm.alarm_type
                        and abs(item.time_ms - alarm.alarm_time)
                        <= APP_MSG_LOITER_WINDOW_MS
                    ),
                    None,
                )
                if twin is not None:
                    self._loiter_seen.add(uid)
                    existing_by_uid[uid] = twin
                    lo = twin
                else:
                    lo = LoiterEvent(
                        uid=uid,
                        alarm_type=alarm.alarm_type,
                        alarm_type_name=self.alarm_type_name(alarm.alarm_type) or "徘徊检测",
                        time_ms=alarm.alarm_time,
                        alarm_id=alarm.alarm_id or None,
                        pre_s=alarm.pre_time or LOITER_DEFAULT_PRE_S,
                        post_s=alarm.delay_time or LOITER_DEFAULT_POST_S,
                    )
                    self._loiter.append(lo)
                    self._loiter_seen.add(uid)
                    existing_by_uid[uid] = lo
                    fresh.append(lo)

            # 抓拍图与录像都交给后台，主循环只登记任务
            if (
                alarm.pic_url
                and not lo.snapshot_path
                and lo.uid not in self._loiter_snap_tasks
                and lo.uid not in self._loiter_img_fail
            ):
                self._loiter_snap_tasks.add(lo.uid)
                self.hass.async_create_task(
                    self._async_bg_loiter_snapshot(lo, alarm.pic_url)
                )
            if ENABLE_LIVE_CAPTURE and not lo.clip_url and lo.clip_status in ("pending", "failed"):
                need_clip.append(lo)

        self._loiter_synced = True

        if fresh:
            self._loiter.sort(key=lambda x: x.time_ms, reverse=True)
            _LOGGER.info("发现 %d 条徘徊唤醒（累计 %d 条）", len(fresh), len(self._loiter))
            for lo in fresh:
                self.hass.bus.async_fire(EVENT_LOITER_DETECTED, lo.to_dict())
            await self._async_prune_loiter()
            await self._async_save_state()
        elif cold_start:
            _LOGGER.debug("徘徊事件同步完成，共 %d 条", len(self._loiter))

        # 录像走后台任务：要等设备把这段录完、还要拉流，绝不能卡在轮询里。
        # 冷启动时额外延后，避免和实体初始化抢带宽与 CPU。
        extra = 30.0 if cold_start else 0.0
        for lo in need_clip:
            if lo.uid in self._loiter_tasks:
                continue
            self._loiter_tasks[lo.uid] = self.hass.async_create_task(
                self._async_queue_loiter_clip(lo, extra_delay=extra)
            )

    async def _async_bg_loiter_snapshot(self, lo: LoiterEvent, pic_url: str) -> None:
        """后台下载一张徘徊抓拍图。"""
        try:
            await self._async_archive_loiter_snapshot(lo, pic_url)
            await self._async_save_state()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - 单张图失败不影响任何主流程
            _LOGGER.warning("徘徊抓拍图保存失败（%s）：%s", lo.time_iso, err)
            # 记成失败，别每轮都去重试同一张死图
            self._loiter_img_fail.add(lo.uid)
        finally:
            self._loiter_snap_tasks.discard(lo.uid)
            self.async_update_listeners()

    async def _async_queue_loiter_clip(self, lo: LoiterEvent, *, extra_delay: float = 0.0) -> None:
        """等到录像窗口结束再取流，避免拿到半截。"""
        try:
            ready_at = lo.clip_end_ms + LOITER_CLIP_READY_EXTRA_S * 1000
            now_ms = int(dt_util.now().timestamp() * 1000)
            delay = max((ready_at - now_ms) / 1000, extra_delay)
            if delay > 0:
                # 刚发生的徘徊：等它录完再取（最多等 5 分钟，更久就交回下轮轮询）
                await asyncio.sleep(min(delay, 300))
            async with self._loiter_semaphore():
                await self._async_fetch_loiter_clip(lo)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("获取徘徊录像异常：%s", lo.time_iso)
            lo.clip_status = "failed"
        finally:
            self._loiter_tasks.pop(lo.uid, None)
            # 录像到位或失败都要让前端知道，否则列表上的「录制中」永远不消失
            self.async_update_listeners()

    def _loiter_semaphore(self) -> asyncio.Semaphore:
        """一次只取一路：设备同时只允许一路本地回放，并发会直接拿不到流。"""
        sem = getattr(self, "_loiter_sem", None)
        if sem is None:
            sem = asyncio.Semaphore(1)
            self._loiter_sem = sem
        return sem

    async def _async_archive_loiter_snapshot(self, lo: LoiterEvent, pic_url: str) -> None:
        local_dt = dt_util.as_local(
            datetime.fromtimestamp(lo.time_ms / 1000, tz=dt_util.UTC)
        )
        date_dir = self._snapshot_root / local_dt.strftime("%Y-%m-%d")
        dest = date_dir / f"{local_dt.strftime('%H%M%S')}_loiter.jpg"
        await self.client.async_download(pic_url, dest, max_bytes=20 * 1024 * 1024)
        lo.snapshot_path = str(dest)
        lo.snapshot_url = self._abs_to_url(dest)
        _LOGGER.debug("徘徊抓拍已存档：%s", lo.snapshot_url)

    async def _async_run_ffmpeg(self, cmd: list[str], timeout: int) -> bool:
        """跑一次 ffmpeg 子进程，成功返回 True，失败只记日志不抛。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            _LOGGER.warning("ffmpeg 超时（%d 秒）", timeout)
            return False
        except OSError as err:
            _LOGGER.warning("ffmpeg 启动失败：%s", err)
            return False
        if proc.returncode != 0:
            _LOGGER.warning(
                "ffmpeg 失败：%s", (stderr or b"").decode("utf-8", "replace")[-300:]
            )
            return False
        return True

    async def async_record_live_clip(self, dest: Path, seconds: int) -> int:
        """从实时直播流录一段 mp4（H.264，浏览器可直接播），返回字节数。

        2026-11-12 改动：这台锁的 HEVC 流是「两路帧奇偶交织」，直接解码必然
        大片绿（见知识库 §12）。先 ``-c copy`` 原样落盘，再交给 hevc_split
        拆出干净那一路重编码；若拆流不适用，就从这份原件直接转码 —— 不重新
        取流，避免触发设备侧的回放/直播通道冷却，也不比改动前更差。
        """
        if not self._ffmpeg:
            _LOGGER.warning("系统里没有 ffmpeg，无法抓录直播")
            return 0
        url = await self.client.async_get_live_url(channel_no=self._channel_no)
        if not url:
            _LOGGER.warning("取直播地址失败，无法抓录")
            return 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        raw = dest.with_name(dest.stem + ".raw.mkv")
        await self._async_remove_file(str(raw))
        duration = max(3, seconds)

        # ① 原样直拷（不解码），只向设备取一次流
        ok = await self._async_run_ffmpeg(
            [
                self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", url, "-t", str(duration),
                "-map", "0:v:0", "-c", "copy", "-f", "matroska", str(raw),
            ],
            duration + 90,
        )
        try:
            if not ok or not raw.is_file() or raw.stat().st_size < 20_000:
                _LOGGER.warning("直播流原件没取到有效数据：%s", raw.name)
                return 0

            # ② 拆流修复；自适应判定不适用时回退为「从原件直接转码」
            try:
                stat = await self.hass.async_add_executor_job(
                    repair_interleaved_hevc, self._ffmpeg, raw, dest, 15
                )
                _LOGGER.info(
                    "直播抓录已拆流修复：%s 路 / %d 帧 / 绿帧 %.1f%% / %d B",
                    stat["stream"], stat["frames"], stat["green"], stat["out_bytes"],
                )
            except (SplitError, OSError) as err:
                _LOGGER.info("拆流不适用，从原件直接转码：%s", err)
                if not await self._async_run_ffmpeg(
                    [
                        self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(raw), "-map", "0:v:0",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                        "-pix_fmt", "yuv420p", "-an",
                        "-movflags", "+faststart", "-f", "mp4", str(dest),
                    ],
                    duration + 120,
                ):
                    return 0
        finally:
            await self._async_remove_file(str(raw))

        size = self._file_size(dest)
        if size <= 0:
            return 0
        # 设备忙的时候拿到的「画面」其实是 512x288 的提示图，ffmpeg 会老老实实
        # 录成 4 秒 / 几 KB 的静帧视频。这种不能当录像交出去。
        width = await self._probe_video_width(dest)
        if width and width < PLACEHOLDER_MIN_WIDTH:
            _LOGGER.warning(
                "直播流返回占位画面（%dpx 宽 / %d 字节），丢弃本次抓录：%s",
                width, size, dest.name,
            )
            await self._async_remove_file(str(dest))
            return 0

        # ④ 成品逐帧兜底：拆流路径和「拆流不适用」的回退路径都过这一关。
        #    实测（14:5x）旧回退路径会产出 97.8% 绿帧的 mp4，不能让它挂到记录上。
        try:
            frames, green = await self.hass.async_add_executor_job(
                green_frame_ratio, self._ffmpeg, dest
            )
        except Exception as err:  # noqa: BLE001 - 自检本身失败不阻断交付
            _LOGGER.debug("抓录成品绿帧自检失败（按未检出处理）：%s", err)
        else:
            if frames and green > 0.2:
                _LOGGER.warning(
                    "抓录成品仍有 %.1f%% 绿帧，丢弃不交付：%s",
                    green * 100, dest.name,
                )
                await self._async_remove_file(str(dest))
                return 0
        return size

    async def _probe_video_width(self, path: Path) -> int:
        """读 mp4 编码宽度；读不到就返回 0（当作未知，不拦截）。"""
        if not self._ffmpeg:
            return 0
        try:
            proc = await asyncio.create_subprocess_exec(
                self._ffmpeg, "-hide_banner", "-i", str(path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
        except (asyncio.TimeoutError, OSError):
            return 0
        text = (stderr or b"").decode("utf-8", "replace")
        hit = re.search(r"Video:.*?,\s*(\d+)x(\d+)", text)
        return int(hit.group(1)) if hit else 0

    async def _async_record_door_live(self, ev: DoorEvent) -> None:
        """（已停用）开门事件抓一段直播录像；现统一走 SD 回放白名单认领。"""
        if not ENABLE_LIVE_CAPTURE:
            return
        if ev.clip_url or ev.is_close:
            return
        age_s = dt_util.utcnow().timestamp() - ev.open_time_ms / 1000
        if age_s > LIVE_MAX_AGE_S:
            ev.clip_status = "too_late"
            return

        local_dt = dt_util.as_local(
            datetime.fromtimestamp(ev.open_time_ms / 1000, tz=dt_util.UTC)
        )
        date_dir = self._snapshot_root / local_dt.strftime("%Y-%m-%d")
        slug = OPEN_TYPE_SLUGS.get(ev.open_type, "unknown")
        tail = ev.uid.split("|")[1] if "|" in ev.uid else "unknown"
        dest = date_dir / f"{local_dt.strftime('%H%M%S')}_{slug}_{tail}.mp4"

        ev.clip_status = "recording"
        self.async_update_listeners()
        size = 0
        for attempt in range(2):
            async with self._loiter_semaphore():
                size = await self.async_record_live_clip(dest, DOOR_LIVE_SECONDS)
            if size > 0:
                break
            if attempt == 0:
                # 连着两次开门时，上一段刚录完、设备侧会话还没释放，
                # 再取流往往只拿到占位图。等几秒重来一次。
                _LOGGER.info(
                    "开门录像首次没拿到有效画面，%d 秒后重试", CLIP_RETRY_DELAY_S
                )
                await asyncio.sleep(CLIP_RETRY_DELAY_S)
        if size <= 0:
            ev.clip_status = "failed"
            await self._async_save_state()
            return
        ev.clip_path = str(dest)
        ev.clip_url = self._abs_to_url(dest)
        ev.clip_bytes = size
        ev.clip_status = "ready"
        await self._async_save_state()
        _LOGGER.info(
            "开门录像已抓录：%s（%.1f MB / %d 秒）",
            ev.clip_url, size / 1048576, DOOR_LIVE_SECONDS,
        )

    async def _async_fetch_loiter_clip(self, lo: LoiterEvent) -> None:
        """抓一段「徘徊当时」的录像。

        不用 SD 卡回放流：实测那路只给残帧（单帧中位数不到 1KB，解码只有
        上部 20% 有画面）。改成事件一出现就抓实时直播流 —— 设备被徘徊唤醒
        的这段时间本来就在推流，抓到的是同一场景的完整画面。
        """
        if not ENABLE_LIVE_CAPTURE:
            lo.clip_status = "disabled"
            return
        if lo.clip_url:
            return

        # 事件过去太久：直播流已经不在那一刻，补不回来了
        age_s = dt_util.utcnow().timestamp() - lo.time_ms / 1000
        if age_s > LIVE_MAX_AGE_S:
            lo.clip_status = "too_late"
            await self._async_save_state()
            return

        local_dt = dt_util.as_local(
            datetime.fromtimestamp(lo.time_ms / 1000, tz=dt_util.UTC)
        )
        date_dir = self._snapshot_root / local_dt.strftime("%Y-%m-%d")
        dest = date_dir / f"{local_dt.strftime('%H%M%S')}_loiter.mp4"

        lo.clip_status = "recording"
        self.async_update_listeners()
        lo.clip_retries += 1
        size = await self.async_record_live_clip(dest, LOITER_LIVE_SECONDS)
        if size <= 0:
            lo.clip_status = "failed"
            await self._async_save_state()
            return

        lo.clip_path = str(dest)
        lo.clip_url = self._abs_to_url(dest)
        lo.clip_bytes = size
        lo.clip_status = "ready"
        _LOGGER.info(
            "徘徊录像已抓录：%s（%.1f MB / %d 秒）",
            lo.clip_url, size / 1048576, LOITER_LIVE_SECONDS,
        )
        await self._async_save_state()

    @staticmethod
    def _flv_codec(path: Path) -> str:
        """读 FLV 第一个 video tag 的 codecID，判断是 h264 还是 h265。

        FLV 结构：9 字节文件头 + 4 字节 PrevTagSize，之后是 tag：
        type(1) + dataSize(3) + timestamp(3) + streamId(3) + data。
        video tag 的 data[0] 低 4 位就是 codecID（7=AVC/H264，12=HEVC/H265）。
        """
        try:
            with path.open("rb") as fh:
                head = fh.read(13)
                if len(head) < 13 or head[:3] != b"FLV":
                    return "unknown"
                while True:
                    tag = fh.read(11)
                    if len(tag) < 11:
                        return "unknown"
                    tag_type = tag[0] & 0x1F
                    data_size = int.from_bytes(tag[1:4], "big")
                    if tag_type == 9 and data_size >= 1:
                        body = fh.read(min(data_size, 16))
                        if not body:
                            return "unknown"
                        codec_id = body[0] & 0x0F
                        if codec_id == 12:
                            return "h265"
                        if codec_id == 7:
                            # 增强型 FLV 里 H265 也用 codecID=7 + 扩展头标记
                            if len(body) > 4 and body[4] & 0x80:
                                return "h265"
                            return "h264"
                        return f"codec{codec_id}"
                    if data_size <= 0:
                        return "unknown"
                    fh.seek(data_size + 4, 1)  # 跳过 body + PrevTagSize
        except OSError:
            return "unknown"

    @staticmethod
    def _file_size(path: Path) -> int:
        """取文件大小；不存在或读不到就返回 0（不能让它抛异常打断取流流程）。"""
        try:
            return path.stat().st_size
        except OSError:
            return 0

    async def _async_remux_mp4(self, src: Path, dest: Path, *, force_h264: bool = False) -> bool:
        """flv → mp4。

        这台锁推的是 H265，直接 `-c copy` 出来的是 hevc 的 mp4，
        浏览器兼容性差（Chrome 要系统装 HEVC 扩展才放得了）。
        所以默认优先转成 H264：512x288、30 秒的片段在普通 x86 上几秒就完了。
        转码不可用（镜像里没有 libx264）时再退回 `-c copy`。
        """
        if not self._ffmpeg:
            _LOGGER.info("系统没有 ffmpeg，徘徊录像只能保存为 flv")
            return False

        attempts: list[tuple[str, list[str]]] = []
        if force_h264:
            attempts.append((
                "转码H264",
                ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p"],
            ))
        attempts.append(("直接封装", ["-c", "copy"]))
        if not force_h264:
            attempts.append((
                "转码H264",
                ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p"],
            ))

        last_err = ""
        for label, codec_args in attempts:
            # hevc 进 mp4 要打 hvc1 tag，否则 Safari / 部分播放器认不出来
            tag_args = ["-tag:v", "hvc1"] if codec_args == ["-c", "copy"] else []
            cmd = [
                self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(src),
                *codec_args,
                *tag_args,
                "-movflags", "+faststart",
                "-f", "mp4",
                str(dest),
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=240)
            except asyncio.TimeoutError:
                last_err = "超时"
                continue
            except OSError as err:
                last_err = str(err)
                continue
            if proc.returncode == 0 and self._file_size(dest) > 0:
                if label != attempts[0][0]:
                    _LOGGER.debug("记录 %s 使用「%s」成功", dest.name, label)
                return True
            last_err = (stderr or b"").decode("utf-8", "replace")[-300:]
            _LOGGER.debug("ffmpeg「%s」失败：%s", label, last_err)
            # 上一次失败可能留下 0 字节文件，清掉再来
            try:
                if dest.exists():
                    dest.unlink()
            except OSError:
                pass

        _LOGGER.warning("ffmpeg 转 mp4 全部失败（%s），保留 flv", last_err)
        return False

    async def async_delete_loiter(
        self, *, uid: str | None = None, time_ms: int | None = None
    ) -> dict[str, Any]:
        """删除一条徘徊记录及其录像/抓拍图，并留墓碑防止云端轮询把它拉回来。

        安全边界（删文件不可逆，所以卡死三道）：
        1. 只删这一条事件自己记下的路径，以及由它的 URL 反推出来的路径；
        2. 路径必须落在 _media_root / _www_root 之内，且不是符号链接；
        3. 绝不做目录级删除、绝不按时间戳模糊匹配文件。

        删完立刻更新 coordinator.data 并通知 listeners，前端不用等下一轮轮询。
        """
        target = next(
            (
                lo for lo in self._loiter
                if (uid and lo.uid == uid)
                or (time_ms is not None and lo.time_ms == time_ms)
            ),
            None,
        )
        if target is None:
            return {"deleted": False, "error": "找不到对应的徘徊记录"}

        # 1. 收集候选：落盘 path 优先，URL 反推兜底（URL 带签名，_url_to_abs 会剥掉）
        candidates: set[Path] = set()
        for raw in (target.clip_path, target.snapshot_path):
            if raw:
                candidates.add(Path(raw))
        for url in (target.clip_url, target.snapshot_url):
            mapped = self._url_to_abs(url)
            if mapped is not None:
                candidates.add(mapped)

        roots = (Path(self._media_root).resolve(), Path(self._www_root).resolve())
        safe_paths: list[Path] = []
        for cand in sorted(candidates):
            try:
                real = cand.resolve()
            except OSError:
                continue
            if not any(real == root or root in real.parents for root in roots):
                _LOGGER.warning("拒绝删除越界路径：%s", cand)
                continue
            if real.is_file() and not real.is_symlink():
                safe_paths.append(real)

        def _rm(paths: list[Path]) -> int:
            count = 0
            for path in paths:
                try:
                    path.unlink()
                    count += 1
                except OSError as err:
                    _LOGGER.warning("删除徘徊媒体失败 %s：%s", path, err)
            return count

        removed = await self.hass.async_add_executor_job(_rm, safe_paths)

        # 2. 内存里摘掉、留墓碑、掐掉还在跑的后台任务（否则会被写回来）
        self._loiter = [lo for lo in self._loiter if lo.uid != target.uid]
        self._deleted_loiter.add(target.uid)
        if target.alarm_id:
            self._deleted_loiter.add(str(target.alarm_id))
        self._loiter_seen.add(target.uid)
        task = self._loiter_tasks.pop(target.uid, None)
        if task is not None and not task.done():
            task.cancel()
        self._loiter_snap_tasks.discard(target.uid)
        self._loiter_img_fail.discard(target.uid)
        await self._async_save_state()

        # 3. 立即刷新前端
        if self.data is not None:
            self.data.loiter_events = [
                lo for lo in self.data.loiter_events if lo.uid != target.uid
            ]
            if self.data.loiter_last and self.data.loiter_last.uid == target.uid:
                self.data.loiter_last = self._loiter[0] if self._loiter else None
            today = dt_util.now().date()
            self.data.loiter_today_count = sum(
                1 for lo in self._loiter
                if dt_util.as_local(
                    datetime.fromtimestamp(lo.time_ms / 1000, tz=dt_util.UTC)
                ).date() == today
            )
            self.async_update_listeners()

        _LOGGER.info(
            "已删除徘徊记录 %s（%s），清理 %d 个媒体文件",
            target.uid, target.time_iso, removed,
        )
        return {
            "deleted": True,
            "uid": target.uid,
            "time_ms": target.time_ms,
            "files": removed,
        }

    async def async_refetch_loiter_clips(self, *, force: bool = False) -> int:
        """把所有还没拿到录像的徘徊事件重新排一次取流。

        场景：一开始设备编码是 H265、平台只给占位提示图，用户改完编码后
        点一下就能把历史几条重新抓一遍，不用等新事件。
        """
        queued = 0
        for lo in self._loiter:
            if lo.uid in self._loiter_tasks:
                continue
            if lo.clip_url and not force:
                continue
            if force:
                # 强制重取：旧文件可能是占位图，先删干净再从云端拉
                for path in (lo.clip_path,):
                    if path:
                        await self._async_remove_file(path)
                lo.clip_url = None
                lo.clip_path = None
                lo.clip_bytes = 0
            lo.clip_status = "pending"
            lo.clip_retries = 0
            self._loiter_tasks[lo.uid] = self.hass.async_create_task(
                self._async_queue_loiter_clip(lo, extra_delay=queued * 3.0)
            )
            queued += 1
        if queued:
            _LOGGER.info("已重新排队 %d 条徘徊录像", queued)
            self.async_update_listeners()
        return queued

    async def async_archive_loiter_clip(self, loiter: LoiterEvent | None = None) -> str | None:
        """按需补取一条徘徊录像（服务 / 前端按钮用）。"""
        target = loiter or (self._loiter[0] if self._loiter else None)
        if target is None:
            return None
        if target.clip_url:
            return target.clip_url
        async with self._loiter_semaphore():
            await self._async_fetch_loiter_clip(target)
        if target.clip_url:
            self.async_update_listeners()
        return target.clip_url

    async def _async_resign_loiter_urls(self) -> None:
        """启动后重签徘徊的图片 / 录像地址（持久化下来的是旧签名）。"""
        for lo in self._loiter:
            if lo.snapshot_path:
                p = Path(lo.snapshot_path)
                if await self.hass.async_add_executor_job(p.exists):
                    lo.snapshot_url = self._abs_to_url(p)
                else:
                    lo.snapshot_url = None
                    lo.snapshot_path = None
            if lo.clip_path:
                p = Path(lo.clip_path)
                if await self.hass.async_add_executor_job(p.exists):
                    lo.clip_url = self._abs_to_url(p)
                else:
                    lo.clip_url = None
                    lo.clip_path = None
                    if lo.clip_status in ("ready", "ready_flv"):
                        lo.clip_status = "pending"

    async def _async_prune_loiter(self) -> None:
        """超出保留条数的徘徊事件，连同文件一起清掉。"""
        if len(self._loiter) <= LOITER_KEEP_MAX:
            return
        dropped = self._loiter[LOITER_KEEP_MAX:]
        self._loiter = self._loiter[:LOITER_KEEP_MAX]
        for lo in dropped:
            self._loiter_seen.discard(lo.uid)
            self._loiter_img_fail.discard(lo.uid)
            for path in (lo.snapshot_path, lo.clip_path):
                if path:
                    await self._async_remove_file(path)

    # ------------------------------------------------------------------ 事件构建
    async def _build_events(
        self,
        records: list[DoorRecord],
        *,
        download_media: bool,
        lightweight: bool = False,
    ) -> list[DoorEvent]:
        events: list[DoorEvent] = []
        # 关键性能点：整批只拉一次告警，本地按时间窗匹配。
        # 之前每条事件都单独调一次 alarm/list，重建 10 条就是 10 次请求，
        # 直接把启动拖到 200 秒以上（bootstrap 报 waiting for integrations）。
        alarms_all: list[AlarmRecord] | None = None
        if not lightweight and records:
            lo = min(r.open_time for r in records) - ALARM_LOOKBACK_MS
            hi = max(r.open_time for r in records) + ALARM_LOOKAHEAD_MS
            try:
                alarms_all = await self.client.async_get_alarms(
                    lo, hi, channel_no=self._channel_no
                )
                _LOGGER.debug("批量预取告警 %d 条（%d 条记录共用）", len(alarms_all), len(records))
            except EzvizApiError as err:
                _LOGGER.debug("批量预取告警失败（本次无图）：%s", err)
                alarms_all = None

        for record in records:
            try:
                events.append(
                    await self._build_one(
                        record,
                        download_media=download_media,
                        lightweight=lightweight,
                        alarms_all=alarms_all,
                    )
                )
            except Exception:  # noqa: BLE001 - 单条失败不能影响整批
                _LOGGER.exception("构建开门事件失败：%s", record)
        return events

    async def _build_one(
        self,
        record: DoorRecord,
        *,
        download_media: bool,
        lightweight: bool = False,
        fetch_clip: bool = False,
        alarms_all: list[AlarmRecord] | None = None,
    ) -> DoorEvent:
        is_close = record.open_type == DOOR_CLOSE_TYPE
        raw_name = (record.user_name or "").strip()
        event = DoorEvent(
            uid=record.uid,
            open_time_ms=record.open_time,
            # 关门记录不带人员名；开门记录按配置映射成真名
            user_name="" if is_close else self.display_user_name(raw_name),
            user_name_raw=raw_name or None,
            open_type=record.open_type,
            open_type_name=OPEN_TYPES.get(record.open_type, f"未知({record.open_type})"),
            channel_no=self._channel_no,
            is_close=is_close,
        )

        # 关门事件没有人员、人数、图片，直接返回：省掉 2~3 次无意义的接口调用
        if is_close:
            event.person_count = 0
            event.person_count_source = "n/a"
            return event

        # 轻量模式：回填历史统计用，不查告警、不查人数、不抓拍，避免一次拉几百条时打爆接口
        if lightweight:
            event.person_count = 1
            event.person_count_source = "default"
            return event

        # ---- 1. 找同一次通行的告警（拿图 / 拿人数 / 拿录像时间段）
        # 优先用整批预取的结果在本地筛窗口，避免逐条请求接口
        alarms: list[AlarmRecord] = []
        if alarms_all is not None:
            lo = record.open_time - ALARM_LOOKBACK_MS
            hi = record.open_time + ALARM_LOOKAHEAD_MS
            alarms = [a for a in alarms_all if lo <= a.alarm_time <= hi]
        else:
            try:
                alarms = await self.client.async_get_alarms(
                    record.open_time - ALARM_LOOKBACK_MS,
                    record.open_time + ALARM_LOOKAHEAD_MS,
                    channel_no=self._channel_no,
                )
            except EzvizApiError as err:
                _LOGGER.debug("拉取告警失败（图片将缺失）：%s", err)

        # 只有「门外开门」才会有可信的门外画面。室内按键（5）、机械钥匙（8）、
        # 关门（9）如果也去配图，只会把别人的告警图错认成自己的。
        outside_open = record.open_type in OUTSIDE_OPEN_TYPES

        best: AlarmRecord | None = None
        if outside_open:
            for alarm in alarms:
                if not alarm.pic_url:
                    continue
                # 一张图只能给一个事件：21:31:37 室内开门抢走 21:31:57 人脸图就是漏了这一步
                if alarm.alarm_id and alarm.alarm_id in self._used_alarm_ids:
                    continue
                if best is None or abs(alarm.alarm_time - record.open_time) < abs(
                    best.alarm_time - record.open_time
                ):
                    best = alarm

        if best is not None:
            event.alarm_id = best.alarm_id
            event.alarm_type = best.alarm_type
            event.alarm_type_name = self.alarm_type_name(best.alarm_type)
            if best.alarm_id:
                self._used_alarm_ids.add(best.alarm_id)
        elif outside_open and record.open_time < int(dt_util.now().timestamp() * 1000) - 300_000:
            # 5 分钟前的事件还没有任何带图告警 -> 云端确实没有，记下来不再取。
            # 刚发生的事件不记：告警上报本身有几秒到几十秒延迟，要给它机会。
            self._no_image_uids.add(event.uid)

        # ---- 2. 人数估算（三级降级）
        event.person_count, event.person_count_source = self._estimate_person_count(
            record, alarms, best
        )

        # ---- 3. 人脸 ID（用于在萤石 App 之外做人员映射）
        for alarm in alarms:
            face_id = _deep_find(alarm.extra, FACE_ID_KEYS)
            if face_id:
                event.face_id = str(face_id)
                break

        # ---- 4. 抓拍图落盘
        if download_media and best is not None and best.pic_url:
            await self._async_archive_snapshot(event, best)

        # ---- 5. 云端回放地址（不落盘，仅在需要在线回看时使用）
        # 单独开关：重建时要处理十几条事件，逐条请求 vod 会把启动拖到超时
        if fetch_clip and download_media and best is not None:
            try:
                event.clip_remote_url = await self.client.async_get_vod_url(
                    best.clip_start_ms, best.clip_end_ms, channel_no=self._channel_no
                )
            except EzvizApiError as err:
                _LOGGER.debug("获取回放地址失败：%s", err)

        return event

    def _estimate_person_count(
        self,
        record: DoorRecord,
        alarms: list[AlarmRecord],
        best: AlarmRecord | None,
    ) -> tuple[int, str]:
        # ① 设备透传字段里的人数（部分固件会上报）
        for alarm in alarms:
            value = _deep_find(alarm.extra, PERSON_COUNT_KEYS)
            if value is not None:
                try:
                    count = int(value)
                    if count > 0:
                        return count, "customer_info"
                except (TypeError, ValueError):
                    pass

        # ② 聚合窗内不同 faceId 的数量（同一次通行多张人脸 = 多人同行）
        window_ms = self._merge_window * 1000
        face_ids: set[str] = set()
        for alarm in alarms:
            if abs(alarm.alarm_time - record.open_time) > window_ms:
                continue
            face_id = _deep_find(alarm.extra, FACE_ID_KEYS)
            if face_id:
                face_ids.add(str(face_id))
        if len(face_ids) > 1:
            return len(face_ids), "face_ids"

        # ③ 无法判断时按 1 人计（不臆造）
        return 1, "default"

    # ------------------------------------------------------------------ 媒体归档
    async def _async_load_clean_clips(self, *, force_full: bool = False) -> list[dict[str, Any]]:
        """读取逐帧验收白名单，生成前端可播的「干净开门录像」索引。

        clean_clips.json 由 ezviz_sd_pull.py 在成品逐帧自检合格后写入；集成再核
        size + md5，文件被换过就不认 —— 绿片绝对进不了匹配池。

        v1.9.2 性能修复：冷启动不再阻塞在 30 段录像的全文件 MD5 上（实测 58.6s，
        导致 HA 打 "Waiting for integrations to complete setup"）。改为：
          ① 上次全量验证通过时把索引 stamp + 每段 (size, mtime_ns) 存进状态；
          ② 冷启动只做 stat 比对，指纹全对就复用已验证结果；
          ③ 无论命中与否，启动后都由后台任务再做一次完整 MD5 复核并刷新缓存。
        stat 指纹任一不符就当缓存失效：当轮不挂任何录像，等后台验完再挂。
        """
        idx_path = self._media_root / "clean_clips.json"

        def _read_stamp() -> tuple[int, int] | None:
            try:
                st = idx_path.stat()
                return (st.st_mtime_ns, st.st_size)
            except OSError:
                return None

        def _read_entries() -> list[dict[str, Any]] | None:
            try:
                idx = json.loads(idx_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            out: list[dict[str, Any]] = []
            for c in idx.get("clean") or []:
                if not c.get("usable_for_open") or not c.get("start_ms"):
                    continue
                # green_ratio 合法值就是 0.0，不能用 `or 1` 兜底（会把 0 当成缺失）
                gr, fr = c.get("green_ratio"), c.get("frames")
                if gr is None or fr is None or float(gr) > 0.02 or int(fr) < 15:
                    continue
                rel = str(c.get("rel") or "")
                if "/" not in rel or ".." in rel or not rel:
                    continue
                out.append(c)
            # 只校验/展示最近 30 段：全量复核也必须限定范围，
            # 否则 117 段录像的 md5 会把启动后的后台任务拖成几分钟。
            return sorted(out, key=lambda x: int(x["start_ms"]), reverse=True)[:30]

        def _md5(p: Path) -> str:
            h = hashlib.md5()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()

        def _scan_full():
            """完整复核（MD5），只在后台任务里跑。"""
            stamp = _read_stamp()
            if stamp is None:
                return None
            entries = _read_entries()
            if entries is None:
                return None
            ok: list[dict[str, Any]] = []
            fp: dict[str, list[int]] = {}
            for c in entries:
                rel = c["rel"]
                p = self._media_root / rel
                try:
                    if not p.is_file():
                        continue
                    st = p.stat()
                    if st.st_size != int(c.get("size") or -1):
                        continue
                    if _md5(p) != c.get("md5"):
                        _LOGGER.warning("白名单里的录像已被改动，拒绝使用：%s", rel)
                        continue
                except OSError:
                    continue
                ok.append(c)
                fp[rel] = [st.st_size, st.st_mtime_ns]
            return stamp, ok, fp

        def _scan_quick():
            """冷启动快路径：只 stat，不读录像内容。

            v1.9.4 修复：以前只要索引里**有一条**指纹缺失或不符，就整份判失效，
            而拉流脚本每新增一段录像都会改写 clean_clips.json —— 于是每次新增
            都把全部录像清空，前端表现为「有录像文件却什么都打不开」。
            现在改为逐条筛：有指纹且 stat 一致的片段照常上线，
            新增/变动的片段本轮先不挂，交给后台全量复核补。
            """
            stamp = _read_stamp()
            if stamp is None:
                return None
            entries = _read_entries()
            if entries is None:
                return None
            files = self._clean_cache.get("files")
            if not isinstance(files, dict) or not files:
                return None
            safe: list[dict[str, Any]] = []
            for c in entries:
                expect = files.get(c["rel"])
                if not isinstance(expect, list) or len(expect) != 2:
                    continue  # 缓存里还没有它的指纹（新片段）→ 等复核
                p = self._media_root / c["rel"]
                try:
                    if not p.is_file():
                        continue
                    st = p.stat()
                except OSError:
                    continue
                if [st.st_size, st.st_mtime_ns] != list(expect):
                    continue  # 文件被换过 → 不可信
                safe.append(c)
            if not safe:
                return None
            return stamp, safe, None

        # 同一进程内的轮询直接复用；全量复核必须绕过这一层
        if not force_full and self._clean_index_stamp is not None:
            return self.clean_clips

        try:
            res = await self.hass.async_add_executor_job(
                _scan_full if force_full else _scan_quick
            )
        except OSError as err:
            _LOGGER.debug("读取干净录像白名单失败：%s", err)
            return self.clean_clips

        if res is None:
            if not force_full:
                # 没有可信缓存：先空着上线，后台验完再挂（绝不拿未验的片冒险）
                self.clean_clips = []
                self._clean_index_stamp = None
                self._schedule_clean_verify()
            return self.clean_clips

        stamp, ok, fp = res
        clips: list[dict[str, Any]] = []
        for c in sorted(ok, key=lambda x: int(x["start_ms"]), reverse=True)[:30]:
            try:
                url = self._abs_to_url(self._media_root / c["rel"])
            except (ValueError, OSError):
                continue
            clips.append({
                "name": c["name"],
                "rel": c["rel"],
                "url": url,
                "start_ms": int(c["start_ms"]),
                "duration_s": float(c.get("duration_s") or 0),
                "frames": int(c.get("frames") or 0),
                "green_ratio": float(c.get("green_ratio") or 0),
            })
        self.clean_clips = clips
        self._clean_index_stamp = stamp
        if fp is not None:
            self._clean_cache = {"stamp": list(stamp), "files": fp}
            await self._async_save_state()
            _LOGGER.info("干净录像白名单已在后台完成全量 MD5 复核（%d 段）", len(clips))
        elif clips:
            _LOGGER.info("复用已验证的干净录像索引 %d 段，供前端按时间匹配", len(clips))
            # v1.9.4：快路径只挂「有指纹且 stat 一致」的片段，索引里的新增片段
            # 还没验过。这里补一次后台全量复核，把新增的也验进来（不占启动路径）。
            self._schedule_clean_verify()
        return clips

    def _abs_to_url(self, path: Path) -> str:
        """本地文件 → 前端可访问 URL。

        /media 下的文件必须带 authSig 签名才能访问（否则 401/404），
        这里签 10 年，等于长期有效；旧 www 下的文件仍走 /local。
        """
        try:
            rel = path.relative_to(self._media_root).as_posix()
        except ValueError:
            rel = None
        if rel is not None:
            abs_path = f"{MEDIA_URL_PREFIX}/{DOORLOCK_SUBDIR}/{rel}"
            signed = async_sign_path(
                self.hass, abs_path, timedelta(days=SIGNED_URL_DAYS)
            )
            # 历史文件名可能带中文（213157_爸爸_4.jpg）：签名必须用未编码的路径，
            # 但返回给前端的 URL 必须编码，否则客户端直接报 ascii 编码错误 / 404。
            base, _, qs = signed.partition("?")
            return f"{quote(base, safe='/')}?{qs}"
        rel = path.relative_to(self._www_root).as_posix()
        return f"/local/{WWW_SUBDIR}/{quote(rel)}"

    def _url_to_abs(self, url: str | None) -> Path | None:
        """前端 URL → 本地绝对路径（`_abs_to_url` 的逆运算）。

        只处理 /media/local 与 /local 两种前缀；签名参数直接丢弃。
        认不出来的地址返回 None，调用方按「文件不存在」处理。
        """
        if not url:
            return None
        raw = url.split("?", 1)[0]
        rel = unquote(raw)
        media_prefix = f"{MEDIA_URL_PREFIX}/{DOORLOCK_SUBDIR}/"
        if rel.startswith(media_prefix):
            return Path(self._media_root) / rel[len(media_prefix):]
        if rel.startswith(f"/local/{WWW_SUBDIR}/"):
            return Path(self._www_root) / rel[len(f"/local/{WWW_SUBDIR}/"):]
        return None

    async def _async_remove_file(self, path: str | None) -> None:
        """删掉不再使用的图片：一个事件只保留一张图。"""
        if not path:
            return

        def _rm() -> None:
            try:
                p = Path(path)
                if p.is_file():
                    p.unlink()
            except OSError:
                pass

        try:
            await self.hass.async_add_executor_job(_rm)
        except OSError:
            pass

    async def _async_resign_snapshot_urls(self) -> None:
        """按当前存储位置重新签名所有图片 URL（文件不存在的保留原样）。"""

        def _check() -> dict[str, bool]:
            out: dict[str, bool] = {}
            for ev in self._events:
                if not ev.snapshot_path:
                    continue
                try:
                    out[ev.uid] = Path(ev.snapshot_path).is_file()
                except OSError:
                    out[ev.uid] = False
            return out

        try:
            exists = await self.hass.async_add_executor_job(_check)
        except OSError:
            return
        resigned = 0
        for ev in self._events:
            if not exists.get(ev.uid) or not ev.snapshot_path:
                continue
            try:
                ev.snapshot_url = self._abs_to_url(Path(ev.snapshot_path))
                resigned += 1
            except (ValueError, OSError):
                continue
        if resigned:
            _LOGGER.debug("已重签 %d 条抓拍图地址", resigned)

    async def _async_migrate_legacy_snapshots(self) -> int:
        """把 config/www/ezviz_lock 下的旧图片搬到 /media（幂等，只搬一次）。"""
        legacy = self._www_root
        if not legacy.is_dir():
            return 0
        mapping: dict[str, str] = {}

        def _move() -> int:
            moved = 0
            for src in sorted(legacy.rglob("*")):
                if not src.is_file() or src.suffix.lower() not in (".jpg", ".jpeg", ".png", ".mp4"):
                    continue
                rel = src.relative_to(legacy)
                # snapshots 这层在新结构里去掉：door-lock/2026-11-10/x.jpg
                parts = list(rel.parts)
                if parts and parts[0] == "snapshots":
                    parts = parts[1:]
                dest = self._media_root.joinpath(*parts)
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        src.unlink()
                    else:
                        shutil.move(str(src), str(dest))
                    mapping[str(src)] = str(dest)
                    moved += 1
                except OSError:
                    continue
            return moved

        try:
            n = await self.hass.async_add_executor_job(_move)
        except OSError:
            return 0
        if not n:
            return 0

        # 事件里存的是旧绝对路径，跟着改掉，否则重建时会被当成「文件已丢失」
        for ev in self._events:
            new_path = mapping.get(ev.snapshot_path or "")
            if not new_path:
                continue
            ev.snapshot_path = new_path
            ev.snapshot_url = self._abs_to_url(Path(new_path))
            if ev.snapshot_source == "alarm":
                ev.snapshot_source = "cache"
        _LOGGER.info("已把 %d 个旧媒体文件迁移到 %s", n, self._media_root)
        return n

    async def _async_archive_snapshot(self, event: DoorEvent, alarm: AlarmRecord) -> None:
        local_dt = dt_util.as_local(
            datetime.fromtimestamp(event.open_time_ms / 1000, tz=dt_util.UTC)
        )
        # 这一刻的图可能早就落过盘（含旧的中文命名），直接复用，别再下一遍
        existing = await self._async_find_local_snapshot(
            local_dt.strftime("%Y-%m-%d"), local_dt.strftime("%H%M%S")
        )
        if existing is not None:
            event.snapshot_path = str(existing)
            event.snapshot_url = self._abs_to_url(existing)
            event.snapshot_source = "cache"
            return
        date_dir = self._snapshot_root / local_dt.strftime("%Y-%m-%d")
        # 文件名只用 ASCII：中文用户名会让 URL 变成 %E7%88%B8... 一旦没编码就打不开
        slug = OPEN_TYPE_SLUGS.get(event.open_type, "unknown")
        filename = f"{local_dt.strftime('%H%M%S')}_{slug}_{event.uid.split('|')[1]}.jpg"
        dest = date_dir / filename

        if not dest.exists():
            try:
                await self.client.async_download(alarm.pic_url, dest, max_bytes=20 * 1024 * 1024)
            except EzvizApiError as err:
                _LOGGER.warning("抓拍图下载失败：%s", err)
                return

        # 一个事件只留一张图：换成告警原图后，把之前的补拍图删掉
        old_path = event.snapshot_path
        event.snapshot_path = str(dest)
        event.snapshot_url = self._abs_to_url(dest)
        event.snapshot_source = "alarm"
        if old_path and old_path != str(dest):
            await self._async_remove_file(old_path)

    def async_schedule_late_fill(self) -> None:
        """启动完成后调度「补图 / 录像复核」后台任务。

        启动阶段只做接口查询和 stat（秒级），图片下载和大文件 MD5 全部推迟到
        这里，避免 bootstrap 等待超时导致前端打不开页面。
        """
        if getattr(self, "_late_fill_started", False):
            return
        self._late_fill_started = True
        self.hass.async_create_task(self._async_late_fill_snapshots())

    def _schedule_clean_verify(self) -> None:
        """缓存失效时，启动完成后再做全量 MD5（定时器回调，不占用启动任务）。"""
        if self._clean_verify_cancel is not None:
            return
        self._clean_verify_cancel = async_call_later(
            self.hass, 30, self._async_start_clean_verify
        )

    @callback
    def _async_start_clean_verify(self, _now: Any) -> None:
        """定时器回调：必须 @callback，否则会在事件循环外执行，
        async_create_task 拿不到 loop，协程被丢弃（日志 "was never awaited"），
        白名单就永远补不回来。"""
        self._clean_verify_cancel = None
        task = self._clean_verify_task
        if task is not None and not task.done():
            return
        self._clean_verify_task = self.hass.async_create_task(
            self._async_verify_clean_clips()
        )

    def async_cancel_clean_verify(self) -> None:
        """卸载条目时取消尚未执行的后台复核。"""
        if self._clean_verify_cancel is not None:
            self._clean_verify_cancel()
            self._clean_verify_cancel = None
        task = self._clean_verify_task
        if task is not None and not task.done():
            task.cancel()

    async def _async_verify_clean_clips(self) -> None:
        """启动后做一次全量 MD5，结果写回状态供下次冷启动复用。"""
        before = len(self.clean_clips)
        try:
            await self._async_load_clean_clips(force_full=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 复核失败绝不能影响主循环
            _LOGGER.exception("后台复核干净录像白名单失败")
            return
        # v1.9.4：启动快路径只挂「有指纹」的旧片段，新增片段要等这次复核。
        # 复核补齐白名单后必须重新认领一次录像地址，否则启动时被清掉的
        # clip_url 永远挂不回来 —— 前端表现就是「有录像文件但打不开」。
        if before > 0 and len(self.clean_clips) == before:
            return
        try:
            await self._async_backfill_clips(self._events)
            await self._async_save_state()
            self.async_update_listeners()
            _LOGGER.info(
                "复核完成后重新认领录像：白名单 %d 段，事件带录像 %d 条",
                len(self.clean_clips),
                sum(1 for e in self._events if e.clip_url),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("复核后重新认领录像失败")

    async def _async_late_fill_snapshots(self) -> None:
        """给今天缺图的门外开门补下载告警原图（后台执行，失败不影响实体）。"""
        await asyncio.sleep(15)  # 等 HA 启动彻底稳定，别跟实体初始化抢资源
        try:
            # 只处理「本来就没有图」的事件：已有图片的一律不再从云端取。
            # 最近 7 天内、最多 8 条，避免一次把范围拉太宽。
            cutoff_ms = int(dt_util.start_of_local_day().timestamp() * 1000) - 7 * 86_400_000
            targets = [
                ev
                for ev in self._events
                if ev.open_time_ms >= cutoff_ms
                and ev.open_type in OUTSIDE_OPEN_TYPES
                and not ev.snapshot_path
                and ev.uid not in self._no_image_uids
            ]
            if not targets:
                return
            targets = targets[:8]
            lo = min(ev.open_time_ms for ev in targets) - ALARM_LOOKBACK_MS
            hi = max(ev.open_time_ms for ev in targets) + ALARM_LOOKAHEAD_MS
            alarms_all = await self.client.async_get_alarms(
                lo, hi, channel_no=self._channel_no
            )
        except EzvizApiError as err:
            _LOGGER.debug("后台补图：拉取告警失败 %s", err)
            return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("后台补图任务启动失败")
            return

        changed = 0
        # 按时间升序：早的开门先占走属于自己的告警图
        for ev in sorted(targets, key=lambda e: e.open_time_ms):
            best = None
            if ev.alarm_id:
                best = next(
                    (a for a in alarms_all if a.alarm_id == ev.alarm_id and a.pic_url), None
                )
            if best is None:
                for alarm in alarms_all:
                    if not alarm.pic_url:
                        continue
                    if alarm.alarm_id and alarm.alarm_id in self._used_alarm_ids:
                        continue
                    if abs(alarm.alarm_time - ev.open_time_ms) > ALARM_LOOKAHEAD_MS:
                        continue
                    if best is None or abs(alarm.alarm_time - ev.open_time_ms) < abs(
                        best.alarm_time - ev.open_time_ms
                    ):
                        best = alarm
            if best is None:
                # 云端确实没有这次通行的图：记入负缓存，以后不再重复请求
                self._no_image_uids.add(ev.uid)
                _LOGGER.debug("云端无图，记为不再取：%s", ev.open_time_iso)
                continue
            if best.alarm_id:
                self._used_alarm_ids.add(best.alarm_id)
            ev.alarm_id = best.alarm_id
            ev.alarm_type = best.alarm_type
            ev.alarm_type_name = self.alarm_type_name(best.alarm_type)
            before = ev.snapshot_url
            try:
                await self._async_archive_snapshot(ev, best)
            except Exception:  # noqa: BLE001 - 单张失败不影响其余
                _LOGGER.warning("后台补图失败：%s", ev.uid, exc_info=True)
                continue
            if ev.snapshot_url and ev.snapshot_url != before:
                changed += 1

        # 即使一张都没补到，也要存一下：负缓存必须落地，否则下次启动又重来一遍
        await self._async_save_state()
        if changed:
            self.async_update_listeners()
            _LOGGER.info(
                "后台补图完成：新增 %d 张抓拍图，%d 条确认云端无图",
                changed,
                len(self._no_image_uids),
            )

    async def _async_find_local_snapshot(self, date_str: str, hhmmss: str) -> Path | None:
        """在当天目录里按 HHMMSS 前缀找已存在的图片（新旧命名都认）。"""

        def _find() -> Path | None:
            date_dir = self._snapshot_root / date_str
            if not date_dir.is_dir():
                return None
            for item in sorted(date_dir.iterdir()):
                if (
                    item.is_file()
                    and item.name.startswith(f"{hhmmss}_")
                    and item.suffix.lower() in (".jpg", ".jpeg", ".png")
                ):
                    return item
            return None

        try:
            return await self.hass.async_add_executor_job(_find)
        except OSError:
            return None

    async def _async_backfill_snapshots(self, events: list[DoorEvent]) -> int:
        """从本地已经下载好的图片里，把丢失的 URL 重新挂回事件。

        云端告警图的 picUrl 有有效期，过期后重新拉取只会失败；但文件早就落在
        `www/ezviz_lock/snapshots/<日期>/` 下，按「日期目录 + HHMMSS 前缀」就能认回来。
        """

        def _scan() -> dict[tuple[str, str], Path]:
            index: dict[tuple[str, str], Path] = {}
            root = self._snapshot_root
            if not root.is_dir():
                return index
            for date_dir in root.iterdir():
                if not date_dir.is_dir():
                    continue
                for item in date_dir.iterdir():
                    if not item.is_file() or item.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                        continue
                    index.setdefault((date_dir.name, item.name[:6]), item)
            return index

        try:
            index = await self.hass.async_add_executor_job(_scan)
        except OSError as err:
            _LOGGER.debug("扫描本地抓拍目录失败：%s", err)
            return 0

        if not index:
            return 0

        restored = 0
        claimed: set[str] = set()
        for event in events:
            if event.snapshot_url or event.open_type not in OUTSIDE_OPEN_TYPES:
                continue
            local_dt = dt_util.as_local(
                datetime.fromtimestamp(event.open_time_ms / 1000, tz=dt_util.UTC)
            )
            path = index.get((local_dt.strftime("%Y-%m-%d"), local_dt.strftime("%H%M%S")))
            if path is None or str(path) in claimed:
                continue
            claimed.add(str(path))
            event.snapshot_path = str(path)
            event.snapshot_url = self._abs_to_url(path)
            event.snapshot_source = "cache"
            restored += 1

        if restored:
            _LOGGER.info("从本地存档回填了 %d 张开门抓拍", restored)
        return restored

    @staticmethod
    def _clip_rank(path: Path) -> int:
        """录像优先级：云存储回放(带声音、精确到秒) > 干净 SD 回放 > 集成抓录；废片不认。

        2026-11-13 新增 `_cloud.mp4`：来自 `ezviz_cloud_pull.py` 的 type=3 云回放，
        段起点就是开门事件时刻（实测多数只差 0–2 秒），且自带 AAC 音轨。
        它和 `_sd.mp4` 同走逐帧验收白名单，但优先级更高，能顶替已认领的 SD 片。
        """
        name = path.name.lower()
        if ".broken." in name:
            return -1
        if name.endswith("_cloud.mp4"):
            return 3
        if name.endswith("_sd.mp4"):
            return 2
        return 1

    @staticmethod
    def _name_seconds(name: str) -> int | None:
        """从 `HHMMSS_xxx.mp4` 取当天秒数；文件名不规范返回 None。"""
        head = name[:6]
        if len(head) != 6 or not head.isdigit():
            return None
        h, m, s = int(head[:2]), int(head[2:4]), int(head[4:6])
        if h > 23 or m > 59 or s > 59:
            return None
        return h * 3600 + m * 60 + s

    async def _async_backfill_clips(self, events: list[DoorEvent]) -> int:
        """从本地存档认领开门录像，优先认领干净的 `<HHMMSS>_sd.mp4`。

        `clip_url` 是运行时算出来的签名地址，不落盘；重启后不认回来，
        界面会显示「无录像」。独立拉取器产出的 `_sd.mp4` 是拆流修复过的
        干净 H.264，优先级高于旧集成「事件时抓直播」的绿屏 mp4：只要
        同一时刻附近（±CLIP_MATCH_WINDOW_S = 20 秒）找到它，就把事件指过去。

        返回本次变更条数（清掉的错片 + 认领的干净片），供调用方决定是否落盘。
        """

        # 只认逐帧验收白名单：没有验收记录、文件被换过、或旧集成抓直播
        # 产出的绿屏 mp4，一律不能挂到开门记录上（主人要求「录像必须无绿」）。
        await self._async_load_clean_clips()
        allowed = {c["rel"] for c in self.clean_clips}

        # 先收拾状态和磁盘对不上的：文件被删了（比如当时的录像是占位图），
        # 事件上却还挂着 ready + 一个打不开的地址；顺带清掉非白名单的旧录像。
        dropped = 0
        inside_cleared = 0
        for ev in events:
            if not ev.clip_url:
                continue
            # 室内开门（type5）物理上没有门外画面：主动清掉历史错挂的视频，
            # 且不参与本轮认领（主人 11-18 08:58 要求）。
            if ev.open_type == DOOR_INSIDE_TYPE:
                ev.clip_url = None
                ev.clip_path = None
                ev.clip_bytes = 0
                ev.clip_status = "no_stream"
                inside_cleared += 1
                continue
            if ev.is_close:
                continue
            local = Path(ev.clip_path) if ev.clip_path else self._url_to_abs(ev.clip_url)
            rel = None
            if local is not None:
                try:
                    rel = local.relative_to(self._media_root).as_posix()
                except ValueError:
                    rel = None
            if (
                local is not None
                and rel in allowed
                and await self.hass.async_add_executor_job(local.is_file)
            ):
                continue
            ev.clip_url = None
            ev.clip_path = None
            ev.clip_bytes = 0
            ev.clip_status = "no_stream"
            dropped += 1
        if dropped:
            _LOGGER.info("清理了 %d 条录像不存在或未经逐帧验收的记录", dropped)
        if inside_cleared:
            _LOGGER.info("清掉了 %d 条室内开门事件上错挂的录像（室内开门不配视频）", inside_cleared)

        # 室内开门（type5）不参与视频认领：没有门外画面，配了就是造假。
        need = [
            ev for ev in events
            if not ev.is_close and ev.open_type != DOOR_INSIDE_TYPE
        ]
        # 只有徘徊、没有开门时也要继续往下走（以前这里会直接返回，徘徊永远挂不上）
        if not need and not self._loiter:
            return dropped

        def _scan() -> dict[str, list[Path]]:
            index: dict[str, list[Path]] = {}
            root = self._snapshot_root
            if not root.is_dir():
                return index
            for date_dir in root.iterdir():
                if not date_dir.is_dir():
                    continue
                for item in date_dir.iterdir():
                    if not item.is_file() or item.suffix.lower() != ".mp4":
                        continue
                    if f"{date_dir.name}/{item.name}" not in allowed:
                        continue
                    if self._clip_rank(item) < 0:
                        continue
                    index.setdefault(date_dir.name, []).append(item)
            return index

        try:
            index = await self.hass.async_add_executor_job(_scan)
        except OSError as err:
            _LOGGER.debug("扫描本地录像目录失败：%s", err)
            return 0
        if not index:
            return dropped

        claimed: set[str] = set()
        restored = 0
        upgraded = 0
        for ev in need:
            local_dt = dt_util.as_local(
                datetime.fromtimestamp(ev.open_time_ms / 1000, tz=dt_util.UTC)
            )
            date_key = local_dt.strftime("%Y-%m-%d")
            target = self._name_seconds(local_dt.strftime("%H%M%S"))
            if target is None:
                continue
            # 已有认领先复检：早期 ±90 秒窗口贴上的「远处片」必须撤下重配。
            # 实测 19:00:14 被 19:01:12 那段（晚 58 秒）占住，等 19:00:09 这段
            # （差 5 秒）补进来时，因为「同档位不替换」永远换不回来 —— 那就是假视频。
            if ev.clip_url:
                cur = Path(ev.clip_path) if ev.clip_path else self._url_to_abs(ev.clip_url)
                if cur is not None and await self.hass.async_add_executor_job(cur.is_file):
                    cur_sec = self._name_seconds(cur.name)
                    if (
                        self._clip_rank(cur) >= 2
                        and cur_sec is not None
                        and abs(cur_sec - target) > CLIP_MATCH_WINDOW_S
                    ):
                        ev.clip_path = None
                        ev.clip_url = None
                        ev.clip_bytes = 0
                        ev.clip_status = "no_stream"
                        dropped += 1

            best: tuple[tuple[int, int], Path] | None = None
            for path in index.get(date_key) or []:
                if str(path) in claimed:
                    continue
                sec = self._name_seconds(path.name)
                if sec is None:
                    continue
                delta = abs(sec - target)
                rank = self._clip_rank(path)
                # SD 回放段起点可能比开门记录早几秒；集成抓录仍是精确 HHMMSS
                if rank >= 2:
                    if delta > CLIP_MATCH_WINDOW_S:
                        continue
                elif delta != 0:
                    continue
                key = (-rank, delta)
                if best is None or key < best[0]:
                    best = (key, path)
            if best is None:
                continue
            path = best[1]
            rank = self._clip_rank(path)
            best_sec = self._name_seconds(path.name)
            best_delta = abs(best_sec - target) if best_sec is not None else 10 ** 6
            if ev.clip_url:
                cur = Path(ev.clip_path) if ev.clip_path else self._url_to_abs(ev.clip_url)
                if cur is not None and await self.hass.async_add_executor_job(cur.is_file):
                    cur_sec = self._name_seconds(cur.name)
                    cur_delta = abs(cur_sec - target) if cur_sec is not None else 10 ** 6
                    cur_rank = self._clip_rank(cur)
                    # 同档位也允许「更贴近开门时刻」的替换；留 5 秒余量避免来回抖动
                    if cur_rank > rank or (cur_rank == rank and cur_delta <= best_delta + 5):
                        claimed.add(str(cur))
                        continue
                    upgraded += 1
            claimed.add(str(path))
            ev.clip_path = str(path)
            ev.clip_url = self._abs_to_url(path)
            ev.clip_bytes = self._file_size(path)
            ev.clip_status = "ready"
            restored += 1

        if restored:
            _LOGGER.info(
                "从本地存档认领了 %d 段开门录像（其中 %d 段升级为干净的 SD 回放）",
                restored, upgraded,
            )

        # ---- 徘徊唤醒：与开门共用白名单与语法，只是窗口更宽 ----
        # 徘徊是「人靠近但没开门」，录像在 SD 卡（recState=4），原来靠事件当场抓直播，
        # 那条链路已停用（截图比事件晚 20-30 秒），所以改为和开门一样认 SD 回放。
        # claimed 与开门分开：徘徊和开门是两次不同事件，允许指向同一时段的那段录像。
        lo_claimed: set[str] = set()
        lo_restored = 0
        lo_changed = 0

        # 徘徊也守同一条规矩：只认「逐帧验收白名单」里的文件。
        # 旧的 `<HHMMSS>_loiter.mp4` 是「事件后抓直播」的产物（比事件晚 20-30 秒、
        # 部分还是绿帧），不在白名单就一律撤下 —— 宁可显示抓拍图，也不挂假录像。
        for lo in self._loiter:
            if not lo.clip_url:
                continue
            local = Path(lo.clip_path) if lo.clip_path else self._url_to_abs(lo.clip_url)
            rel = None
            if local is not None:
                try:
                    rel = local.relative_to(self._media_root).as_posix()
                except ValueError:
                    rel = None
            if (
                local is not None
                and rel in allowed
                and await self.hass.async_add_executor_job(local.is_file)
            ):
                continue
            lo.clip_url = None
            lo.clip_path = None
            lo.clip_bytes = 0
            lo.clip_status = "no_stream"
            lo_changed += 1

        for lo in self._loiter:
            local_dt = dt_util.as_local(
                datetime.fromtimestamp(lo.time_ms / 1000, tz=dt_util.UTC)
            )
            date_key = local_dt.strftime("%Y-%m-%d")
            target = self._name_seconds(local_dt.strftime("%H%M%S"))
            if target is None:
                continue
            if lo.clip_url:
                cur = Path(lo.clip_path) if lo.clip_path else self._url_to_abs(lo.clip_url)
                if cur is not None and await self.hass.async_add_executor_job(cur.is_file):
                    cur_sec = self._name_seconds(cur.name)
                    if (
                        self._clip_rank(cur) >= 2
                        and cur_sec is not None
                        and abs(cur_sec - target) > LOITER_MATCH_WINDOW_S
                    ):
                        lo.clip_url = None
                        lo.clip_path = None
                        lo.clip_bytes = 0
                        lo.clip_status = "no_stream"
                        lo_changed += 1

            best: tuple[tuple[int, int], Path] | None = None
            for path in index.get(date_key) or []:
                if str(path) in lo_claimed:
                    continue
                sec = self._name_seconds(path.name)
                if sec is None:
                    continue
                delta = abs(sec - target)
                rank = self._clip_rank(path)
                if rank >= 2:
                    if delta > LOITER_MATCH_WINDOW_S:
                        continue
                elif delta != 0:
                    continue
                key = (-rank, delta)
                if best is None or key < best[0]:
                    best = (key, path)
            if best is None:
                continue
            path = best[1]
            rank = self._clip_rank(path)
            best_sec = self._name_seconds(path.name)
            best_delta = abs(best_sec - target) if best_sec is not None else 10 ** 6
            if lo.clip_url:
                cur = Path(lo.clip_path) if lo.clip_path else self._url_to_abs(lo.clip_url)
                if cur is not None and await self.hass.async_add_executor_job(cur.is_file):
                    cur_sec = self._name_seconds(cur.name)
                    cur_delta = abs(cur_sec - target) if cur_sec is not None else 10 ** 6
                    if self._clip_rank(cur) > rank or (
                        self._clip_rank(cur) == rank and cur_delta <= best_delta + 5
                    ):
                        lo_claimed.add(str(cur))
                        continue
                    lo_changed += 1
            lo_claimed.add(str(path))
            lo.clip_path = str(path)
            lo.clip_url = self._abs_to_url(path)
            lo.clip_bytes = self._file_size(path)
            lo.clip_status = "ready"
            lo_restored += 1

        if lo_restored or lo_changed:
            _LOGGER.info("徘徊录像：认领 %d 段、撤下 %d 段", lo_restored, lo_changed)
        return dropped + restored + lo_restored + lo_changed

    async def _async_archive_capture(self, event: DoorEvent, url: str) -> None:
        """把 device/capture 拿到的实时画面落盘，作为该事件的补拍图。"""
        now = dt_util.now()
        date_dir = self._snapshot_root / now.strftime("%Y-%m-%d")
        slug = OPEN_TYPE_SLUGS.get(event.open_type, "unknown")
        dest = date_dir / f"{now.strftime('%H%M%S')}_{slug}_cap.jpg"

        try:
            await self.client.async_download(url, dest, max_bytes=20 * 1024 * 1024)
        except EzvizApiError as err:
            _LOGGER.warning("补拍图下载失败：%s", err)
            return
        except OSError as err:
            _LOGGER.warning("补拍图写入失败：%s", err)
            return

        old_path = event.snapshot_path
        event.snapshot_path = str(dest)
        event.snapshot_url = self._abs_to_url(dest)
        event.snapshot_source = "capture"
        if old_path and old_path != str(dest):
            await self._async_remove_file(old_path)
        event.capture_ms = int(now.timestamp() * 1000)
        _LOGGER.info(
            "已补拍开门画面：%s（较真实开门晚 %d 秒）",
            event.snapshot_url,
            max(0, (event.capture_ms - event.open_time_ms) // 1000),
        )

    async def async_archive_clip(self, event: DoorEvent) -> str | None:
        """下载事件对应录像到本地 mp4，返回 /local/... 地址。"""
        if event.clip_url:
            return event.clip_url
        await self._async_archive_clip(event)
        return event.clip_url

    async def _async_archive_clip(self, event: DoorEvent) -> None:
        if event.clip_url:
            return

        local_dt = dt_util.as_local(
            datetime.fromtimestamp(event.open_time_ms / 1000, tz=dt_util.UTC)
        )
        date_dir = self._clip_root / local_dt.strftime("%Y-%m-%d")
        safe_user = re.sub(r'[\\/:*?"<>|\s]+', "_", event.user_name) or "unknown"
        dest = date_dir / f"{local_dt.strftime('%H%M%S')}_{safe_user}.mp4"

        if dest.exists() and dest.stat().st_size > 0:
            event.clip_url = self._abs_to_url(dest)
            return

        # 云录像落盘有延迟，给设备 15 秒缓冲
        await asyncio.sleep(15)
        try:
            url = await self.client.async_get_vod_url(
                event.open_time_ms - 5_000,
                event.open_time_ms + 15_000,
                channel_no=self._channel_no,
                fmt="MP4",
                expire_seconds=1800,
            )
        except EzvizApiError as err:
            _LOGGER.warning("获取录像下载地址失败：%s", err)
            return

        if not url:
            _LOGGER.info("事件 %s 无云录像可下载（未开通云存储 / 时段无录像）", event.open_time_iso)
            return

        try:
            await self.client.async_download(url, dest)
        except EzvizApiError as err:
            _LOGGER.warning("录像下载失败：%s", err)
            return

        event.clip_url = self._abs_to_url(dest)
        _LOGGER.info("录像已存档：%s", event.clip_url)
        await self._async_save_state()

    def _cleanup_snapshots(self) -> None:
        """清理超过保留期的抓拍图与录像目录。"""
        cutoff = dt_util.now().date() - timedelta(days=self._retention)
        for root in (self._snapshot_root, self._clip_root):
            if not root.exists():
                continue
            for day_dir in root.iterdir():
                if not day_dir.is_dir():
                    continue
                try:
                    day = dt_util.parse_date(day_dir.name)
                except ValueError:
                    continue
                if day and day < cutoff:
                    shutil.rmtree(day_dir, ignore_errors=True)
                    _LOGGER.debug("已清理过期目录：%s", day_dir)

    # ------------------------------------------------------------------ 对外服务
    async def async_refresh_now(self) -> None:
        await self.async_request_refresh()

    async def async_playback_url(self, event: DoorEvent | None = None) -> str | None:
        """返回可直接播放的地址：优先本地 mp4，其次云端 MP4 点播地址。"""
        target = event or (self._events[0] if self._events else None)
        if target is None:
            return None
        if target.clip_url:
            return target.clip_url
        try:
            return await self.client.async_get_vod_url(
                target.open_time_ms - 5_000,
                target.open_time_ms + 15_000,
                channel_no=self._channel_no,
                fmt="MP4",
            )
        except EzvizApiError as err:
            _LOGGER.warning("获取回放地址失败：%s", err)
            return None

    async def async_peek(self, *, keep: int = PEEK_KEEP) -> dict[str, Any]:
        """「门前看一眼」：一次抓出上摄 / 下摄两路画面并落盘。

        平台 ``device/capture`` 只给上摄（1504×1968），``channelNo=2`` 报
        「通道不存在」——没有拍下摄的独立接口。下摄只能从实时流里按 VCL NAL
        奇偶拆出来（见 peek_dual.py），两帧来自同一段码流、水印时刻一致。

        流程：
          ① device/capture 抓上摄 —— 快（2~3 s），兼作唤醒设备与兜底；
          ② 取一小段实时流拆出同刻上下两摄 —— 慢，失败不阻断；
          ③ ②成功用双路同刻图；②失败退回 ① 的单路上摄并标注。
        """
        started = time.monotonic()
        now = dt_util.now()
        stamp = now.strftime("%Y%m%d_%H%M%S")
        folder = self._snapshot_root / PEEK_SUBDIR
        up_path = folder / f"peek_{stamp}_up.jpg"
        down_path = folder / f"peek_{stamp}_down.jpg"

        # ① 快通道：唤醒设备 + 兜底单路图
        remote: str | None = None
        capture_err: str | None = None
        try:
            remote = await self.client.async_capture(channel_no=self._channel_no)
        except EzvizApiError as err:
            capture_err = str(err)

        # ② 双路：一次取流，拆出同刻上下两摄
        dual_stat: dict | None = None
        dual_err: str | None = None
        if self._ffmpeg:
            try:
                live = await asyncio.wait_for(
                    self.client.async_get_live_url(
                        channel_no=self._channel_no, expire_seconds=600
                    ),
                    timeout=20,
                )
                if not live:
                    raise DualPeekError("取直播地址失败")
                dual_stat = await asyncio.wait_for(
                    self.hass.async_add_executor_job(
                        extract_dual_frames, self._ffmpeg, live, up_path, down_path
                    ),
                    timeout=50,
                )
            except (asyncio.TimeoutError, DualPeekError, EzvizApiError, OSError) as err:
                dual_err = str(err) or type(err).__name__
        else:
            dual_err = "系统里没有 ffmpeg"

        if dual_err:
            _LOGGER.info("门前看一眼：双路取流未成功（%s），降级单路上摄", dual_err)
            # 万一超时前只写出了一张，清掉半成品再走单路
            for path in (up_path, down_path):
                if path.exists():
                    await self._async_remove_file(str(path))
            if not remote:
                return {
                    "url": None, "path": None, "time": None,
                    "error": (
                        f"抓拍失败：{capture_err or '设备未响应'}；"
                        f"双路取流也失败：{dual_err}"
                    ),
                }
            try:
                await self.client.async_download(
                    remote, up_path, max_bytes=20 * 1024 * 1024
                )
            except (EzvizApiError, OSError) as err:
                return {"url": None, "path": None, "time": None, "error": f"图片下载失败：{err}"}
            mode = "single"
            note = f"仅上摄（下摄取流失败：{dual_err}）"
        else:
            mode = "dual"
            note = (
                f"双摄同刻：上摄 {dual_stat['up'][0]}×{dual_stat['up'][1]} / "
                f"下摄 {dual_stat['down'][0]}×{dual_stat['down'][1]}"
                if dual_stat
                else "双摄"
            )
            _LOGGER.info("门前看一眼：双路拆流成功（%s）", note)

        up_url = self._abs_to_url(up_path)
        lower = None
        if mode == "dual" and down_path.is_file():
            lower = {"url": self._abs_to_url(down_path), "path": str(down_path)}
        self.peek = {
            "url": up_url,                 # 兼容旧前端 / 图片实体：主图 = 上摄
            "path": str(up_path),
            "time": now.isoformat(timespec="seconds"),
            "mode": mode,
            "upper": {"url": up_url, "path": str(up_path)},
            "lower": lower,
            "note": note,
            "source": "live_split" if mode == "dual" else "device_capture",
        }
        # 一次双路会写两张，按「组」保留最近 keep 次抓拍
        await self.hass.async_add_executor_job(self._prune_peek, keep)
        self.async_update_listeners()
        _LOGGER.info(
            "门前看一眼：%s（耗时 %.1fs）", mode, time.monotonic() - started
        )
        return {**self.peek, "error": None}

    def _prune_peek(self, keep: int) -> None:
        """按「一次抓拍」分组清理，不能按张数截。

        文件名形如 ``peek_<ts>_up.jpg`` / ``peek_<ts>_down.jpg``；旧版
        ``peek_<ts>.jpg`` 无后缀，单独成组，一并纳入清理。
        """
        folder = self._snapshot_root / PEEK_SUBDIR
        try:
            files = [p for p in folder.iterdir() if p.is_file()]
        except OSError:
            return
        groups: dict[str, list[Path]] = {}
        for p in files:
            name = p.stem
            for suffix in ("_up", "_down"):
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
                    break
            groups.setdefault(name, []).append(p)
        ordered = sorted(
            groups.items(),
            key=lambda kv: max((p.stat().st_mtime for p in kv[1]), default=0),
            reverse=True,
        )
        for _name, paths in ordered[keep:]:
            for old in paths:
                try:
                    old.unlink()
                except OSError:
                    pass
