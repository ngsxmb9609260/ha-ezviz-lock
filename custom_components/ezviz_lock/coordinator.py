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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import AlarmRecord, DoorRecord, EzvizApiClient, EzvizApiError
from .hevc_split import SplitError, green_frame_ratio, repair_interleaved_hevc
from .const import (
    ALARM_TYPE_LOITER,
    CONF_ALARM_TYPE_NAMES,
    CONF_AUTO_DOWNLOAD_CLIP,
    CONF_CAPTURE_ON_EVENT,
    CONF_CHANNEL_NO,
    CONF_DEVICE_SERIAL,
    CONF_MERGE_WINDOW,
    CONF_SCAN_INTERVAL,
    CONF_SNAPSHOT_RETENTION,
    CONF_USER_ALIASES,
    DEFAULT_ALARM_TYPE_NAMES,
    DEFAULT_AUTO_DOWNLOAD_CLIP,
    DEFAULT_CAPTURE_ON_EVENT,
    DEFAULT_CHANNEL_NO,
    DEFAULT_MERGE_WINDOW,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SNAPSHOT_RETENTION,
    DOOR_CLOSE_TYPE,
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
    PLACEHOLDER_MAX_BYTES,
    PLACEHOLDER_MIN_WIDTH,
    CLIP_RETRY_DELAY_S,
    SIGNED_URL_DAYS,
    STORAGE_KEY,
    STORAGE_VERSION,
    WWW_SUBDIR,
)

_LOGGER = logging.getLogger(__name__)

ALARM_LOOKBACK_MS = 15_000    # 开门时刻之前多久内的告警也算同一次通行
ALARM_LOOKAHEAD_MS = 20_000   # 实测人脸告警与开门记录几乎同秒，窗口不能放大：
                              # 放到 45s 时，室内开门会抢走 20 秒后那次人脸开门的图
CLIP_MATCH_WINDOW_S = 30      # SD 回放段与开门时刻的最大允许偏差（秒）。
                              # 实测 6 次真实匹配的偏差是 1/2/2/3/5/12 秒；放宽到 90 秒会把
                              # 「58 秒后另一件事的录像」贴给这次开门 —— 那是造假，宁可显示抓拍图
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
        # 负缓存：这些事件「云端确实没有图」，以后不再重复请求（避免每次启动白拉一遍）
        self._no_image_uids: set[str] = set()
        self._late_fill_started = False
        self._device_name: str | None = None
        # 逐帧验收过的干净录像索引（前端按时间就近匹配开门记录），见 clean_clips.json
        self.clean_clips: list[dict[str, Any]] = []
        self._clean_index_stamp: tuple[int, int] | None = None

        # ---- 徘徊唤醒 ----
        self._loiter: list[LoiterEvent] = []
        self._loiter_seen: set[str] = set()
        # 用户手动删掉的徘徊告警：留墓碑，防止下一轮 alarm/list 又把它拉回来
        self._deleted_loiter: set[str] = set()
        self._loiter_tasks: dict[str, asyncio.Task] = {}
        self._loiter_snap_tasks: set[str] = set()
        self._loiter_img_fail: set[str] = set()
        self._loiter_synced = False
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
        self._seen = set(stored.get("seen") or [])
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
        self._initialized = bool(stored.get("initialized"))
        # 「云端确认没有图」的事件，持久化下来，避免每次启动都去白拉一遍
        self._no_image_uids = set(stored.get("no_image") or [])
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
        _LOGGER.debug(
            "恢复 %d 条历史事件、%d 条徘徊事件，去重索引 %d 条",
            len(self._events), len(self._loiter), len(self._seen),
        )

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
            }
        )

    # ------------------------------------------------------------------ 主循环
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

        try:
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
            records = await self.client.async_get_door_records_range(
                today_start_ms - 6 * 3_600_000,
                now_ms + 60_000,
                max_pages=6,
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
                # 时刻 ±90s 认领；本轮新事件在下方节流回填里自动挂上。
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

        # 徘徊唤醒：与开门记录是两条独立数据源，单独同步（失败不影响开门主流程）
        try:
            await self._async_sync_loiter(today_start_ms, now_ms)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 徘徊出问题不能让整个集成不可用
            _LOGGER.exception("同步徘徊事件失败")
        _mark("徘徊同步")

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
            device_name=self._device_name,
            loiter_events=self._loiter[:LOITER_KEEP_MAX],
            loiter_today_count=len(today_loiter),
            loiter_last=self._loiter[0] if self._loiter else None,
        )

    async def _async_capture_for_new(self, events: list[DoorEvent]) -> None:
        """给刚发现的真实开门补拍当前画面（只在没有告警原图时）。"""
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
        # 同一轮多条时，只给最新那条拍（其余共用不了不同时刻的画面）
        target = max(targets, key=lambda e: e.open_time_ms)
        try:
            url = await self.client.async_capture(channel_no=self._channel_no)
        except EzvizApiError as err:
            _LOGGER.warning("实时抓拍失败（该条记录将没有图片）：%s", err)
            return
        if not url:
            _LOGGER.warning("实时抓拍未返回图片地址")
            return
        await self._async_archive_capture(target, url)

    # ------------------------------------------------------------------ 徘徊唤醒
    async def _async_sync_loiter(self, today_start_ms: int, now_ms: int) -> None:
        """同步「徘徊唤醒」：拉告警 → 建事件 → 把图与录像丢给后台任务。

        徘徊和开门是两条独立链路：人只是路过、没开门，所以 keylock/open/list
        里什么都没有，只能从 alarm/list 里按 alarmType 认出来。

        **这里绝不做任何下载**：本方法在轮询里被 await，一旦下载就会拖住
        首次刷新（曾经把启动拖到 90 秒超时、所有实体变 unavailable）。
        """
        # 首轮拉今天的全量做重建；之后只回看最近 15 分钟（覆盖几轮轮询的遗漏）
        lo_ms = today_start_ms if not self._loiter_synced else max(today_start_ms, now_ms - 900_000)
        try:
            alarms = await self.client.async_get_alarms(
                lo_ms, now_ms + 60_000, channel_no=self._channel_no
            )
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
    async def _async_load_clean_clips(self) -> list[dict[str, Any]]:
        """读取逐帧验收白名单，生成前端可播的「干净开门录像」索引。

        clean_clips.json 由 ezviz_sd_pull.py 在成品逐帧自检合格后写入；
        这里再核一次 size + md5，文件被换过就不认 —— 绿片绝对进不了匹配池。
        """

        def _md5(p: Path) -> str:
            h = hashlib.md5()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()

        idx_path = self._media_root / "clean_clips.json"

        def _scan():
            try:
                st = idx_path.stat()
                stamp = (st.st_mtime_ns, st.st_size)
            except OSError:
                return None
            if stamp == self._clean_index_stamp and self.clean_clips:
                return "unchanged"
            try:
                idx = json.loads(idx_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            ok: list[dict[str, Any]] = []
            for c in idx.get("clean") or []:
                if not c.get("usable_for_open") or not c.get("start_ms"):
                    continue
                # 注意：green_ratio 合法值就是 0.0，不能用 `or 1` 兜底（会把 0 当成缺失）
                gr, fr = c.get("green_ratio"), c.get("frames")
                if gr is None or fr is None or float(gr) > 0.02 or int(fr) < 15:
                    continue
                rel = str(c.get("rel") or "")
                if "/" not in rel or ".." in rel:
                    continue
                p = self._media_root / rel
                try:
                    if not p.is_file() or p.stat().st_size != int(c.get("size") or -1):
                        continue
                    if _md5(p) != c.get("md5"):
                        _LOGGER.warning("白名单里的录像已被改动，拒绝使用：%s", rel)
                        continue
                except OSError:
                    continue
                ok.append(c)
            return stamp, ok

        try:
            res = await self.hass.async_add_executor_job(_scan)
        except OSError as err:
            _LOGGER.debug("读取干净录像白名单失败：%s", err)
            return self.clean_clips
        if res == "unchanged":
            return self.clean_clips
        if res is None:
            self.clean_clips = []
            self._clean_index_stamp = None
            return self.clean_clips

        stamp, ok = res
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
        if clips:
            _LOGGER.info("加载 %d 段逐帧验收合格的开门录像，供前端按时间匹配", len(clips))
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
        """启动完成后调度「补图」后台任务。

        启动阶段只做接口查询（秒级），图片下载全部推迟到这里，
        避免 bootstrap 等待超时导致前端打不开页面。
        """
        if getattr(self, "_late_fill_started", False):
            return
        self._late_fill_started = True
        self.hass.async_create_task(self._async_late_fill_snapshots())

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
        """录像优先级：独立拉取器的干净 SD 回放 > 集成抓录；废片不认。"""
        name = path.name.lower()
        if ".broken." in name:
            return -1
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
        同一时刻附近（±90 秒）找到它，就把事件指过去。

        返回本次变更条数（清掉的错片 + 认领的干净片），供调用方决定是否落盘。
        """

        # 只认逐帧验收白名单：没有验收记录、文件被换过、或旧集成抓直播
        # 产出的绿屏 mp4，一律不能挂到开门记录上（主人要求「录像必须无绿」）。
        await self._async_load_clean_clips()
        allowed = {c["rel"] for c in self.clean_clips}

        # 先收拾状态和磁盘对不上的：文件被删了（比如当时的录像是占位图），
        # 事件上却还挂着 ready + 一个打不开的地址；顺带清掉非白名单的旧录像。
        dropped = 0
        for ev in events:
            if ev.is_close or not ev.clip_url:
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

        need = [ev for ev in events if not ev.is_close]
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
                        self._clip_rank(cur) == 2
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
                if rank == 2:
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
                        self._clip_rank(cur) == 2
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
                if rank == 2:
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
