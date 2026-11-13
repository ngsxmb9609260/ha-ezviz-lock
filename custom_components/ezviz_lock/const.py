"""ezviz_lock 集成常量定义。

所有取值均来自萤石开放平台官方文档：
- 开门记录   https://open.ys7.com/help/1353  （分页获取开门记录 keylock/open/list）
- 告警列表   https://open.ys7.com/help/987   （按设备获取告警消息列表 alarm/list）
- 存储文件   https://open.ys7.com/help/1422  （根据时间获取存储文件信息 video/by/time）
- 云录像点播 https://open.ys7.com/help/3973  （获取云录像点播地址 HLS/MP4）
"""

from __future__ import annotations

DOMAIN = "ezviz_lock"
NAME = "EZVIZ Lock"
VERSION = "1.0.0"

# ---- 配置键 ----
CONF_APP_KEY = "app_key"
CONF_APP_SECRET = "app_secret"
CONF_DEVICE_SERIAL = "device_serial"
CONF_CHANNEL_NO = "channel_no"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_MERGE_WINDOW = "merge_window"
CONF_SNAPSHOT_RETENTION = "snapshot_retention"
CONF_ALARM_TYPE_NAMES = "alarm_type_names"
CONF_FACE_NAMES = "face_names"
CONF_USER_ALIASES = "user_aliases"
CONF_REGION_HOST = "region_host"
CONF_AUTO_DOWNLOAD_CLIP = "auto_download_clip"
CONF_CAPTURE_ON_EVENT = "capture_on_event"

DEFAULT_CHANNEL_NO = 1
DEFAULT_SCAN_INTERVAL = 20          # 秒：越短，事件触发抓拍越可能拍到人
DEFAULT_MERGE_WINDOW = 12           # 秒：同一次通行事件的聚合窗口
DEFAULT_SNAPSHOT_RETENTION = 90     # 天
DEFAULT_AUTO_DOWNLOAD_CLIP = False
DEFAULT_CAPTURE_ON_EVENT = True     # 发现新开门时立即抓拍一张当前画面
DEFAULT_REGION_HOST = "https://open.ys7.com"

# 可选区域节点（海外/国内镜像），填入配置项 region_host
REGION_HOSTS = {
    "中国（默认）": "https://open.ys7.com",
    "中国（备用）": "https://open.ezviz.com",
    "欧洲": "https://isgpopen.ezvizlife.com",
    "北美": "https://iusopen.ezvizlife.com",
}

# ---- 开锁方式 openType（官方文档 help/1353）----
OPEN_TYPES: dict[int, str] = {
    0: "指纹",
    1: "密码",
    2: "门卡",
    3: "临时密码",
    4: "人脸",
    5: "室内开门",
    6: "远程开门",
    7: "掌静脉",
    8: "机械钥匙",
    # 9 不在官方文档里，是设备实测上报的未文档化取值。
    # 判定依据（Y3000FVX Plus 实测 24 条记录）：每条真实开门后 4~12 秒必出现
    # 一条 openType=9 且 lockUserName 为空的记录，数量约为真实开门的 1.4 倍，
    # 且从不出现在"有人开锁"的位置上 —— 综合判定为「关门」事件。
    9: "关门",
}

# 关门事件：不产生人员/人数/抓拍图，不计入开门次数
DOOR_CLOSE_TYPE = 9

# 是否属于"从门外进入"的开锁方式（室内开门/机械钥匙不产生门外抓拍，单独处理）
OUTSIDE_OPEN_TYPES = {0, 1, 2, 3, 4, 6, 7}

# 开锁方式 → 事件类型后缀（英文，保证 entity/自动化里稳定，不受中文 slug 影响）
OPEN_TYPE_SLUGS: dict[int, str] = {
    0: "fingerprint",
    1: "password",
    2: "card",
    3: "temp_password",
    4: "face",
    5: "inside",
    6: "remote",
    7: "palm_vein",
    8: "mechanical_key",
    9: "close",
}

# ---- 告警类型 alarmType ----
# 注意：萤石各设备固件上报的 alarmType 数值并不统一，这里仅提供常见默认值，
# 实际数值请以 【调试】→ 日志中 "unknown alarmType=xxx" 的输出为准，
# 然后在集成选项 "告警类型名称映射" 里覆盖（格式：10012:门铃呼叫）。
DEFAULT_ALARM_TYPE_NAMES: dict[int, str] = {
    10000: "移动侦测",
    10001: "门磁打开",
    10002: "门磁关闭",
    10004: "视频丢失",
    10005: "视频遮挡",
    10008: "人形检测",
    10012: "门铃呼叫",
    10013: "开门",
    10014: "门未关好",
    10017: "低电量",
    10018: "防撬告警",
    10019: "徘徊检测",
    10020: "人脸识别",
    10021: "陌生人检测",
    10025: "包裹看护",
    10026: "儿童出门检测",
}

# ---- 徘徊检测（录像）----
# 这台锁「徘徊唤醒」实测上报 alarmType=15504，并带 recState=4（SD 卡录像）。
# preTime / delayTime 就是「触发后延迟多久开始录、录多久」，实测 5 / 25。
ALARM_TYPE_LOITER = 15504
LOITER_DEFAULT_PRE_S = 5
LOITER_DEFAULT_POST_S = 25
# 告警结束后再多等一会再去取流：设备要把这段写完才能回放，取早了是空流
LOITER_CLIP_READY_EXTRA_S = 15
# 单段录像下载上限（流不主动关闭，靠空闲超时判结束）
LOITER_CLIP_MAX_BYTES = 120 * 1024 * 1024
LOITER_CLIP_IDLE_S = 20           # 连续 N 秒没有新数据 => 认为这段推完了
LOITER_CLIP_TOTAL_S = 240         # 硬上限，防止弱网下拖住后台任务
LOITER_KEEP_MAX = 120             # 最多保留多少条徘徊事件（含图与录像）

# 平台「编码不匹配」时推的占位提示画面（"视频编码类型非H264…"）体积固定在 144KB 上下。
# 低于该阈值且 codec 又不是 h265 的，一律当作占位图丢弃，别当录像存下来。
PLACEHOLDER_MAX_BYTES = 400_000

# 回放取流：/api/lapp/v2/live/address/get 的 type=2 = 本地录像（SD 卡）回放
PLAYBACK_TYPE_LOCAL = 2
PLAYBACK_PROTOCOL_FLV = 4         # 4=flv 3=rtmp 2=hls；flv 最好落盘（hls 是分片、需二次合并）

# ---- 实时直播抓录 ----
# 实测结论：这台锁的 SD 卡回放流（/v2/live/address/get type=2）只给「残帧」——
# 单帧中位数不到 1KB，解码只有上部约 20% 有画面，其余全绿；换画质档、换封装
# （FLV / LL-HLS / TS）都一样。所以改走「事件发生当时抓实时直播流」。
LIVE_PROTOCOL_HLS = 2
LIVE_TYPE_PREVIEW = 1             # /v2/live/address/get 的 type=1 = 实时预览

# 直播取流实测对比（同一台锁、同一时刻，各录 12 秒抽帧看）：
#   protocol=2 HLS → 画面中间出现整条绿色横带，且构图被切；分片边界丢宏块，
#                    参考帧一断，后面跟着大片绿直到下一个 IDR。
#   protocol=4 FLV → 全程干净完整。连续 TCP 流没有分片边界，不会出现这个问题。
# 所以抓录用 FLV，HLS 只在需要给浏览器直接播放时才考虑。
LIVE_PROTOCOL_FLV = 4

# 设备正忙 / 推流尚未就绪时，直播接口会拿一张 512x288 的提示图当画面推过来，
# ffmpeg 照单全收，录成 4 秒 / 约 7KB / 2fps 的「假录像」。真实主码流是 1080p，
# 所以按编码宽度一刀切：低于这个宽度的一律当占位图扔掉。
PLACEHOLDER_MIN_WIDTH = 640
CLIP_RETRY_DELAY_S = 6            # 抓到占位图后等几秒再试一次（设备会话释放要时间）
LOITER_LIVE_SECONDS = 15          # 徘徊：录 15 秒
DOOR_LIVE_SECONDS = 12            # 开门：录 12 秒
LIVE_MAX_AGE_S = 150              # 事件超过这么久就不再抓直播（画面已不是当时）

# 事件即时抓录（开门 / 徘徊一出现就去抓 type=1 直播流）已停用。
# 实测：取流 + NAL 拆流要几十秒，抓到的画面比开门时刻晚 20~30 秒，
# 与告警抓拍根本不是同一时刻，挂在「视频」列上就是错片。
# 开门录像统一改走 SD 卡回放下载（ezviz_sd_pull.py）→ clean_clips.json
# 逐帧验收白名单 → coordinator 按开门时刻 ±90s 认领，见 _async_backfill_clips。
# 需要临时恢复旧链路时把它改回 True 并重启 HA 即可。
ENABLE_LIVE_CAPTURE = False

EVENT_LOITER_DETECTED = f"{DOMAIN}_loiter_detected"

# ---- 浏览器/实体可访问路径 ----
# ---- 图片存储位置（额外存储 /media，不进 config、不占 HA 备份）----
MEDIA_ROOT = "/media"                            # HA media 根目录
DOORLOCK_SUBDIR = "tpcamera/door-lock"           # /media/tpcamera/door-lock/
MEDIA_URL_PREFIX = "/media/local"                # 前端访问前缀（media_source local）
SIGNED_URL_DAYS = 3650                           # /media 需 authSig 签名，给 10 年

WWW_SUBDIR = "ezviz_lock"                       # config/www/ezviz_lock/
SNAPSHOT_SUBDIR = f"{WWW_SUBDIR}/snapshots"     # config/www/ezviz_lock/snapshots/
CLIP_SUBDIR = f"{WWW_SUBDIR}/clips"             # config/www/ezviz_lock/clips/
LOCAL_SNAPSHOT_PREFIX = f"/local/{SNAPSHOT_SUBDIR}"
LOCAL_CLIP_PREFIX = f"/local/{CLIP_SUBDIR}"

# ---- 事件总线 ----
EVENT_DOOR_OPENED = f"{DOMAIN}_door_opened"

# ---- 服务 ----
SERVICE_DOWNLOAD_CLIP = "download_clip"
SERVICE_REFRESH = "refresh"
SERVICE_GET_PLAYBACK_URL = "get_playback_url"
SERVICE_REFETCH_LOITER = "refetch_loiter"
SERVICE_DELETE_LOITER = "delete_loiter"

# ---- 启动保护 ----
# 首次取数硬超时：卡住会连带 bootstrap 一起等，前端页面就打不开
FIRST_REFRESH_TIMEOUT_S = 90

# ---- 存储键 ----
STORAGE_KEY = f"{DOMAIN}.state"
STORAGE_VERSION = 1

# ---- 开放平台返回码 ----
CODE_OK = "200"
CODE_TOKEN_EXPIRED = {"10002"}          # accessToken 异常或过期 -> 重新取 token
CODE_APP_KEY_INVALID = {"10005"}        # appKey 被冻结 / 不存在
CODE_DEVICE_NOT_OWNED = {"20018"}       # 该用户不拥有该设备
CODE_NO_PERMISSION = {"10030", "20009"} # 无权限 / 未开通白名单
