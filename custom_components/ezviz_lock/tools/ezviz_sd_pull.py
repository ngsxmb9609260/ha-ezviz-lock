#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""萤石门锁 SD 卡录像 -> Home Assistant OS /media 拉取器（HAOS 原生版，双路拆流）。

在哪儿跑
--------
本版同时支持两种执行环境，自动识别，无需改代码：

  1) **HAOS 内（推荐）**：跑在 HA 主容器里（/config、/media 都可读写，root 身份），
     通过 `shell_command` + 自动化定时调用，不依赖 Samba、不依赖本机开机。
        python3 /config/scripts/ezviz_sd_pull.py --limit 1 --event-priority
  2) **Windows 调试机**：挂好 Samba（/config -> Y:，/media -> M:）后照旧跑。
        python ezviz_sd_pull.py --dest M:/tpcamera/door-lock --limit 1

路径默认值按环境自动选：存在 /config/.storage 就用 HAOS 路径，否则回退 Y:/M:，
也可用 --dest / --config-entries / --state-glob 或环境变量显式覆盖。

做什么
------
把门锁本地 SD 卡里已录好的事件录像，按时间段从设备回放流原样拉下来，
落到 HAOS 的 /media/tpcamera/door-lock/<日期>/，生成前端可直接播放的
H.264 mp4。不依赖"事件发生时抓直播流"，也不需要门锁的局域网 IP。

数据流
------
open.ys7.com
  /api/lapp/token/get                    -> accessToken
  /api/lapp/video/by/time (recType=2)    -> SD 卡录像时间段清单
  /api/lapp/v2/live/address/get (type=2) -> 让门锁把该段 SD 录像推成 FLV 流
        |
        v
  ffmpeg -c copy                    -> .mkv 原件（HEVC，字节级保真，可留档）
  ffmpeg hevc_mp4toannexb           -> Annex-B 裸流
  按帧 NAL 奇偶拆成 A / B 两路     -> 自动挑干净那一路
  ffmpeg 正常解码 + libx264         -> .mp4（约 15fps H.264，逐帧无绿）

为什么必须拆流（2026-11-12 11:10 实测结论，替代此前错误的关键帧方案）
---------------------------------------------------------------------
平台把「一路正常 HEVC」和「一路不可解码的 NAL」严格交替塞进同一条流。
解码器把后者的帧当成 Duplicate POC 丢包，未解码宏块输出成纯绿：
  - 改前基线（11-10 12:48 段）：hevc 1504x1968 30fps，60 帧里 59 帧绿，
    均值 87.0%，Duplicate POC 36 次
  - 软解 / d3d11va / dxva2 / +genpts / ignore_err / 关键帧重解 全部无效
  - 按奇偶拆开后：A 路 76 帧 0% 绿 / B 路 76 帧 93.6% 绿
  - 换 11-12 08:03 段复验：A 路 45 帧 0% 绿 / B 路 45 帧 92.3% 绿
  - A 路转码成品：76 帧 / 5.07s / 15fps，逐帧绿占比 0.00%，相邻帧有真实变化

B 路到底是什么（双摄另一路？加密数据？）**未验证**，本脚本直接丢弃它。
A 路是主画面（门口、人、时间戳），帧率约为标称 30fps 的一半。

其它实测结论
------------
- `PUT /api/v3/device/video/encodeType` 把主码流切 H264：设备回
  `60020 不支持该信令`（HTTP 422），门锁不接受编码切换；调用无副作用。
- 云点播 VOD 回 400；云录像 downloadPath 只是 host:port，不是直链。
- 调试机到门锁局域网 IP 的 554/8000 等端口全部超时（网络只通 HAOS 一台），
  因此走开放平台回放流，不直连设备。

重要限制（实测，别踩）
----------------------
回放通道对「短时间高频取流」很敏感。09:15 起一次性批量补录 13 段，
13 段全部拿到 0 字节；诊断发现 FLV 回放 URL 变成 HTTP 404，HLS/LL-HLS
虽然 200 但只给 512x288 提示图。09:31 复测自动恢复，冷却约 15~20 分钟。
确切成因（平台限流 / 电池门锁省电保护 / 会话配额）**未验证**。
因此本脚本默认低频单段运行，通道异常立即整批中止，不猛打设备。

HAOS 部署（本版新增）
--------------------
```
/config/scripts/ezviz_sd_pull.py      # 本脚本（root 可读可执行即可）
/config/scripts/run_ezviz_pull.sh     # 包装器：挑 ffmpeg、写日志、传参
configuration.yaml:
  shell_command:
    ezviz_sd_pull: "/bin/sh /config/scripts/run_ezviz_pull.sh --limit 1 --event-priority"
automation: 每 30 分钟 -> shell_command.ezviz_sd_pull
```
ffmpeg 查找顺序：`--ffmpeg` > `FFMPEG_BIN` > `/config/bin/ffmpeg` > `PATH` >
`imageio_ffmpeg`。HAOS 自带 ffmpeg 太旧时，把新版单文件放到 `/config/bin/ffmpeg` 即可，
**不用改脚本、不用重启 HA**（每次运行现查）。

用法
----
  python3 ezviz_sd_pull.py --check                     # 只做环境自检（凭据/ffmpeg/目录）
  python3 ezviz_sd_pull.py --limit 1 --event-priority   # 补一段（推荐低频）
  python3 ezviz_sd_pull.py --dry-run --hours 24         # 只列清单
  python3 ezviz_sd_pull.py --at 173528 --limit 1        # 只补指定段

可选环境变量：EZVIZ_APP_KEY / EZVIZ_APP_SECRET / EZVIZ_SERIAL /
EZVIZ_REGION_HOST / FFMPEG_BIN / EZVIZ_CONFIG_ENTRIES / EZVIZ_DEST /
EZVIZ_STATE_GLOB / EZVIZ_WORK_DIR

只写媒体文件与 clean_clips.json / .pull_failures.json，不改 HA 配置、
不写 .storage、不重启 HA；目标 mp4 已存在即跳过。

退出码：0 全部成功 / 1 有单段失败 / 2 回放通道不可用整批中止 /
        3 已有实例在跑（文件锁占用）/ 4 环境自检不通过。
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

VERSION = "2026-11-12-haos"

DEFAULT_REGION_HOST = "https://open.ys7.com"
REQUEST_TIMEOUT = 20
PLACEHOLDER_RESOLUTIONS = ("512x288", "512x272", "640x360")
MIN_RAW_BYTES = 20_000
MAX_LOG_BYTES = 5 * 1024 * 1024

# ---------------------------------------------------------------------------
# 平台路径：HAOS 用 /config、/media；Windows 调试机回退 Y:/M:
# ---------------------------------------------------------------------------
HA_CONFIG_DIR = Path("/config")
HA_MEDIA_DIR = Path("/media/tpcamera/door-lock")


def _is_haos() -> bool:
    """有 /config/.storage 就认为是 HAOS（HA 主容器或 addon 里都成立）。"""
    return (HA_CONFIG_DIR / ".storage").is_dir()


def default_config_entries() -> str:
    env = os.environ.get("EZVIZ_CONFIG_ENTRIES")
    if env:
        return env
    if _is_haos():
        return str(HA_CONFIG_DIR / ".storage" / "core.config_entries")
    return r"Y:/.storage/core.config_entries"


def default_dest() -> str:
    env = os.environ.get("EZVIZ_DEST")
    if env:
        return env
    if HA_MEDIA_DIR.is_dir() or (Path("/media").is_dir() and _is_haos()):
        return str(HA_MEDIA_DIR)
    return "M:/tpcamera/door-lock"


def default_state_glob() -> str:
    env = os.environ.get("EZVIZ_STATE_GLOB")
    if env:
        return env
    if _is_haos():
        return str(HA_CONFIG_DIR / ".storage" / "ezviz_lock.state.*")
    return "Y:/.storage/ezviz_lock.state.*"


class ChannelUnavailable(RuntimeError):
    """回放通道整体不可用（404/405/提示图）。整批必须立即中止，别继续打设备。"""


# ---------------------------------------------------------------------------
# 日志：同时写 stdout 与可选文件（HAOS 里 shell_command 的 stdout 不好翻）
# ---------------------------------------------------------------------------
_LOG_FILE: Path | None = None


def _rotate(path: Path) -> None:
    try:
        if path.is_file() and path.stat().st_size > MAX_LOG_BYTES:
            shutil.move(str(path), str(path) + ".1")
    except OSError:
        pass


def set_log_file(path: str | None) -> None:
    global _LOG_FILE
    if not path:
        _LOG_FILE = None
        return
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _rotate(p)
        _LOG_FILE = p
    except OSError as err:
        print(f"[warn] 日志文件不可写，仅输出到终端：{err}", flush=True)
        _LOG_FILE = None


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    if _LOG_FILE is not None:
        try:
            with _LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 环境自检 / 工具
# ---------------------------------------------------------------------------
def force_utf8() -> None:
    """HAOS 里 LANG 常为空，避免中文日志抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def ensure_tz() -> None:
    """HAOS 容器一般已设 TZ=Asia/Shanghai；缺了就补，避免文件名差 8 小时。

    ⚠️ 只在 POSIX 上补。Windows 的 CRT 会把 TZ 当固定偏移解析，
    设成 "Asia/Shanghai" 反而让 localtime 变成 UTC（实测差 8 小时）。
    """
    if os.name == "nt":
        return
    if not os.environ.get("TZ"):
        os.environ["TZ"] = "Asia/Shanghai"
        try:
            time.tzset()
        except AttributeError:
            return
        log("未检测到 TZ，已按 Asia/Shanghai 处理（文件名依赖本地时区）")


def find_ffmpeg(explicit: str | None) -> str:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("FFMPEG_BIN"):
        candidates.append(os.environ["FFMPEG_BIN"])
    # HAOS 建议把新版单文件放这里：不用装包、重启不丢、脚本自动优先用
    candidates.append("/config/bin/ffmpeg")
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    which = shutil.which("ffmpeg")
    if which:
        return which
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    return explicit or "ffmpeg"


def check_ffmpeg(ffmpeg: str) -> tuple[str, list[str]]:
    """返回 (版本行, 缺失能力列表)。缺 libx264 / hevc 解码 / annexb 就没法干活。"""
    problems: list[str] = []
    try:
        res = subprocess.run(
            [ffmpeg, "-hide_banner", "-version"],
            capture_output=True, text=True, errors="replace", timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as err:
        return "", [f"无法执行 ffmpeg（{ffmpeg}）：{err}"]
    if res.returncode != 0:
        return "", [f"ffmpeg 执行失败（{ffmpeg}）：{(res.stderr or '').strip()[:160]}"]
    version = (res.stdout or res.stderr or "").splitlines()[0].strip()

    def has(args: list[str], needle: str) -> bool:
        try:
            r = subprocess.run([ffmpeg, "-hide_banner"] + args,
                               capture_output=True, text=True, errors="replace", timeout=25)
        except (OSError, subprocess.SubprocessError):
            return False
        text = (r.stdout or "") + (r.stderr or "")
        return needle in text

    if not has(["-encoders"], "libx264"):
        problems.append("缺少 libx264 编码器（无法产出浏览器可播的 H.264）")
    if not has(["-decoders"], "hevc"):
        problems.append("缺少 HEVC 解码器")
    if not has(["-bsfs"], "hevc_mp4toannexb"):
        problems.append("缺少 hevc_mp4toannexb（无法拆流）")
    return version, problems


def check_writable(path: Path) -> str | None:
    """返回 None 表示可写，否则返回人话原因。"""
    probe = path / ".wtest"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"ok")
        probe.unlink()
        return None
    except PermissionError:
        return (f"{path} 不可写。HAOS 上 /media 与 /config 属 root："
                f"请以 root 运行（HA 主容器内），或前面加 sudo")
    except OSError as err:
        return f"{path} 不可写：{err}"


class RunLock:
    """进程级互斥：自动化每 30 分钟一次，上一轮没跑完时直接跳过而不是叠加取流。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        if os.name == "nt":
            return True  # Windows 调试机不折腾锁
        try:
            import fcntl
        except ImportError:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a+")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, ImportError):
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            return False

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


def atomic_write(path: Path, text: str) -> None:
    """先写临时文件再 replace：集成每 20 秒读一次，绝不能读到半截 JSON。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 萤石开放平台
# ---------------------------------------------------------------------------
def load_credentials(path: str) -> dict:
    """凭据只从 HA 配置文件 / 环境变量读取，绝不写进代码或日志。"""
    env_map = {
        "app_key": "EZVIZ_APP_KEY",
        "app_secret": "EZVIZ_APP_SECRET",
        "device_serial": "EZVIZ_SERIAL",
    }
    if all(os.environ.get(v) for v in env_map.values()):
        return {
            "app_key": os.environ["EZVIZ_APP_KEY"],
            "app_secret": os.environ["EZVIZ_APP_SECRET"],
            "device_serial": os.environ["EZVIZ_SERIAL"],
            "region_host": os.environ.get("EZVIZ_REGION_HOST", DEFAULT_REGION_HOST),
        }
    cfg_path = Path(os.environ.get("EZVIZ_CONFIG_ENTRIES", path))
    if not cfg_path.is_file():
        raise SystemExit(
            f"找不到 HA 配置文件 {cfg_path}。\n"
            f"  HAOS：默认读 /config/.storage/core.config_entries，请确认路径存在\n"
            f"  Windows：先挂 Samba（/config -> Y:），或用环境变量 EZVIZ_APP_KEY 等提供凭据"
        )
    store = json.load(io.open(cfg_path, encoding="utf-8"))
    for entry in store.get("data", {}).get("entries", []):
        if entry.get("domain") == "ezviz_lock":
            data = entry["data"]
            return {
                "app_key": data["app_key"],
                "app_secret": data["app_secret"],
                "device_serial": data["device_serial"],
                "region_host": data.get("region_host") or DEFAULT_REGION_HOST,
            }
    raise SystemExit(f"{cfg_path} 里没有 ezviz_lock 条目")


class Ezviz:
    def __init__(self, cred: dict) -> None:
        self.cred = cred
        self.base = cred["region_host"].rstrip("/")
        # 绕开 Agent 沙箱注入的代理；open.ys7.com 走国内直连
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._token: str | None = None

    def post(self, path: str, params: dict, headers: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=urllib.parse.urlencode(params).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})},
        )
        with self.opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @property
    def token(self) -> str:
        if self._token:
            return self._token
        res = self.post(
            "/api/lapp/token/get",
            {"appKey": self.cred["app_key"], "appSecret": self.cred["app_secret"]},
        )
        if str(res.get("code")) != "200":
            raise RuntimeError(f"取 token 失败：code={res.get('code')} msg={res.get('msg')}")
        self._token = res["data"]["accessToken"]
        return self._token

    def sd_segments(self, start_ms: int, end_ms: int) -> list[dict]:
        """recType=2 = 门锁本地 SD 卡录像（不是云存储）。"""
        res = self.post(
            "/api/lapp/video/by/time",
            {
                "deviceSerial": self.cred["device_serial"],
                "channelNo": "1",
                "startTime": str(start_ms),
                "endTime": str(end_ms),
                "recType": "2",
                "accessToken": self.token,
            },
        )
        if str(res.get("code")) != "200":
            raise RuntimeError(f"查询 SD 录像失败：{res.get('code')} {res.get('msg')}")
        data = res.get("data")
        return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []

    def playback_url(self, seg: dict) -> str | None:
        """type=2 让设备回放 SD 卡上 [startTime, stopTime] 这段录像。"""
        fmt = lambda ms: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))
        res = self.post(
            "/api/lapp/v2/live/address/get",
            {
                "deviceSerial": self.cred["device_serial"],
                "channelNo": "1",
                "protocol": "4",          # FLV，连续流，最易直拷
                "type": "2",              # 本地 SD 回放
                "quality": "1",
                "startTime": fmt(seg["startTime"]),
                "stopTime": fmt(seg["endTime"]),
                "supportH265": "1",       # 必须传 1，否则只给 512x288 提示图
                "expireTime": "3600",
                "accessToken": self.token,
            },
        )
        if str(res.get("code")) != "200":
            log(f"  取流失败 code={res.get('code')} msg={res.get('msg')}")
            return None
        data = res.get("data") or {}
        return str(data["url"]) if isinstance(data, dict) and data.get("url") else None


# ---------------------------------------------------------------------------
# ffmpeg 处理链
# ---------------------------------------------------------------------------
def probe_stream(ffmpeg: str, path: Path) -> str:
    """读 ffmpeg -i 的流信息，用于识别占位图。"""
    res = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, errors="replace", timeout=30,
    )
    lines = [l.strip() for l in (res.stderr or "").splitlines() if "Stream #" in l and "Video:" in l]
    return lines[0] if lines else ""


def is_placeholder(info: str) -> bool:
    if not info:
        return True
    if any(r in info for r in PLACEHOLDER_RESOLUTIONS):
        return True
    m = re.search(r"(\d{3,4})x(\d{3,4})", info)
    return bool(m and int(m.group(1)) < 640)


def pull_raw(ffmpeg: str, url: str, dest: Path, seconds: int) -> None:
    """原样直拷，不解码不转码：保留设备 SD 卡里的原始码流。"""
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-rw_timeout", "30000000",
        "-i", url,
        "-t", str(max(3, seconds)),
        "-map", "0:v:0", "-c", "copy", "-f", "matroska", str(dest),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=seconds + 120)
    err = res.stderr or ""
    has_data = dest.exists() and dest.stat().st_size > 0
    if res.returncode != 0 and not has_data:
        if any(code in err for code in ("404", "405", "403", "401")):
            raise ChannelUnavailable(
                "回放 URL 被流服务器拒绝（" + ", ".join(c for c in ("404", "405", "403", "401") if c in err) + "）"
            )
        raise RuntimeError(f"ffmpeg 取流失败：{err[-300:]}")


def iter_annexb_nals(data: bytes):
    """按 3/4 字节起始码切出 (payload, nal_unit_type)。"""
    matches = list(re.finditer(rb"\x00\x00\x00?\x01", data))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(data)
        if start < end:
            yield data[start:end], (data[start] >> 1) & 0x3F


def green_frame_ratio(ffmpeg: str, path: Path) -> tuple[int, float]:
    """解码到小尺寸 RGB，逐帧统计纯绿像素；返回 (帧数, 绿帧占比)。"""
    w, h = 94, 123
    chunk = w * h * 3
    res = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
         "-vf", f"scale={w}:{h}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        capture_output=True, timeout=180,
    )
    data = res.stdout
    n = len(data) // chunk
    if n == 0:
        return 0, 1.0
    bad = 0
    for k in range(n):
        frame = data[k * chunk:(k + 1) * chunk]
        green = sum(
            1 for j in range(0, len(frame), 3)
            if frame[j + 1] > 110 and frame[j] < 95 and frame[j + 2] < 95
        )
        if green * 100.0 / (w * h) > 30:
            bad += 1
    return n, bad / n


def split_interleaved_hevc(ffmpeg: str, raw: Path, workdir: Path) -> tuple[Path, str]:
    """拆开回放流里交织的两路 NAL，返回干净那一路的 Annex-B 裸流。

    实测（2026-11-12，CS-Y3000FVX-Plus-V210-B）：
      11-10 12:48 段：A 76 帧 0% 绿 / B 76 帧 93.6% 绿
      11-12 08:03 段：A 45 帧 0% 绿 / B 45 帧 92.3% 绿
    两组都自行解码逐帧统计，自动选绿帧少的那组；若都不干净则失败退出。
    """
    annexb = workdir / "raw.h265"
    res = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
         "-c", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(annexb)],
        capture_output=True, text=True, errors="replace", timeout=180,
    )
    if not annexb.exists() or annexb.stat().st_size < MIN_RAW_BYTES:
        raise RuntimeError(f"转 Annex-B 失败：{(res.stderr or '')[-300:]}")

    data = annexb.read_bytes()
    params: list[bytes] = []
    frames: list[bytes] = []
    for payload, nal_type in iter_annexb_nals(data):
        if nal_type in (32, 33, 34):          # VPS / SPS / PPS
            params.append(payload)
        elif 1 <= nal_type <= 21:             # VCL：TRAIL / IDR / BLA / CRA
            frames.append(payload)
    if len(frames) < 4:
        raise RuntimeError(f"只找到 {len(frames)} 个帧 NAL，无法拆流")

    start_code = b"\x00\x00\x00\x01"
    head = b"".join(start_code + p for p in params)
    candidates = []
    for name, group in (("A", frames[::2]), ("B", frames[1::2])):
        out = workdir / f"stream_{name}.h265"
        out.write_bytes(head + b"".join(start_code + f for f in group))
        n, ratio = green_frame_ratio(ffmpeg, out)
        candidates.append((name, out, n, ratio))
        log(f"  拆流 {name}：{len(group)} 个 NAL -> {n} 帧，绿帧 {ratio * 100:.1f}%")

    usable = [c for c in candidates if c[2] > 0 and c[3] < 0.2]
    if not usable:
        raise RuntimeError("两路都不干净，拆流方法对这段码流不适用（可能换了封装）")
    best = min(usable, key=lambda c: (c[3], -c[2]))
    desc = f"{best[0]} 路 / {best[2]} 帧 / 绿帧 {best[3] * 100:.1f}%"
    log(f"  采用 {desc}")
    return best[1], desc


def to_playable(ffmpeg: str, h265: Path, out: Path, fps: int = 15) -> None:
    """把拆好的干净 HEVC 正常解码 + 重编码成 H.264（不再只留关键帧）。"""
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-r", str(fps), "-i", str(h265),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", "-f", "mp4",
    ]
    for mode in (["-fps_mode", "passthrough"], ["-vsync", "0"]):
        if out.exists():
            out.unlink()
        res = subprocess.run(
            cmd + mode + [str(out)],
            capture_output=True, text=True, errors="replace", timeout=300,
        )
        err = res.stderr or ""
        if res.returncode == 0 and out.exists() and out.stat().st_size > MIN_RAW_BYTES:
            return
        if "Unrecognized option" not in err and "Option not found" not in err:
            raise RuntimeError(f"H.264 重编码失败：{err[-400:]}")
    raise RuntimeError("H.264 重编码失败（新旧参数均不可用）")


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mark_clean(root: Path, out: Path, frames: int, green: float, duration_s: int) -> None:
    """把逐帧验收合格的成品登记进干净白名单 clean_clips.json。

    集成侧只读这份白名单生成前端可播的录像索引，任何绿片都进不来。
    写入用「临时文件 + rename」，避免集成 20 秒轮询读到半截 JSON。
    """
    idx_path = root / "clean_clips.json"
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        idx = {"version": 1, "clean": []}
    rel = f"{out.parent.name}/{out.name}"
    stem = out.name[:6]
    start_ms = None
    if len(stem) == 6 and stem.isdigit():
        try:
            d = datetime.strptime(out.parent.name, "%Y-%m-%d")
            start_ms = int(
                d.replace(hour=int(stem[:2]), minute=int(stem[2:4]),
                          second=int(stem[4:6])).timestamp() * 1000
            )
        except ValueError:
            pass
    rec = {
        "rel": rel,
        "name": out.name,
        "date": out.parent.name,
        "size": out.stat().st_size,
        "md5": _md5(out),
        "frames": frames,
        "green_ratio": round(green, 4),
        "duration_s": round(frames / 15.0, 2) if not duration_s else duration_s,
        "start_ms": start_ms,
        "codec": "h264",
        "usable_for_open": bool(re.fullmatch(r"\d{6}_sd\.mp4", out.name)),
        "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "verified_by": f"ezviz_sd_pull.py 逐帧自检（{VERSION}）",
    }
    clean = [c for c in idx.get("clean", []) if c.get("rel") != rel]
    clean.append(rec)
    idx["clean"] = sorted(clean, key=lambda c: c["rel"])
    idx["generated_at"] = rec["verified_at"]
    idx["rule"] = {
        "max_green_ratio": 0.02,
        "min_frames": 15,
        "note": "逐帧解码统计纯绿像素占比；绿片一律不进匹配池",
    }
    atomic_write(idx_path, json.dumps(idx, ensure_ascii=False, indent=1))
    log(f"  已登记干净白名单：{rel}（{frames} 帧 / 绿 {green * 100:.2f}%）")


def event_priority(pending: list[dict], state_glob: str, window_s: int,
                   failures: dict | None = None) -> list[dict]:
    """把与开门 / 徘徊时刻接近的待补段排到最前，让低频补录优先产出门前录像。

    1 小时内取流失败过的段降到最后，避免定时任务一直卡在同一段 404 上。
    """
    times: list[int] = []
    lo_times: list[int] = []
    for p in sorted(glob.glob(state_glob)):
        try:
            d = json.loads(Path(p).read_text(encoding="utf-8"))["data"]
            times += [int(e["open_time_ms"]) for e in (d.get("events") or [])
                      if not e.get("is_close") and e.get("open_time_ms")]
            # 徘徊唤醒同样要优先：它和开门是两条独立事件流，录像也在 SD 卡上。
            lo_times += [int(x["time_ms"]) for x in (d.get("loiter") or [])
                         if x.get("time_ms")]
        except (OSError, ValueError, KeyError, TypeError):
            continue
    times += lo_times
    if not times:
        log(f"  未读到开门/徘徊事件（{state_glob}），按时间倒序处理")
        return pending
    failures = failures or {}
    now = time.time()

    def nearest(seg: dict) -> float:
        t = int(seg["startTime"])
        return min(abs(t - x) for x in times) / 1000.0

    def recently_failed(seg: dict) -> bool:
        st = datetime.fromtimestamp(seg["startTime"] / 1000)
        key = st.strftime("%Y-%m-%d/%H%M%S")
        return now - float(failures.get(key, 0)) < 3600

    hit = [s for s in pending if nearest(s) <= window_s]
    rest = [s for s in pending if nearest(s) > window_s]
    fresh = [s for s in hit if not recently_failed(s)]
    stale = [s for s in hit if recently_failed(s)]
    if hit:
        log(f"  按事件时刻优先：{len(hit)} 段命中 ±{window_s}s"
            f"（其中 {len(stale)} 段 1h 内失败过，降权；"
            f"读取 {len(times) - len(lo_times)} 条开门 + {len(lo_times)} 条徘徊）")
    by_newest = lambda s: -int(s["startTime"])
    return sorted(fresh, key=by_newest) + rest + sorted(stale, key=by_newest)


def main() -> int:
    force_utf8()
    ensure_tz()
    ap = argparse.ArgumentParser(description="萤石门锁 SD 卡录像拉取到 HAOS /media（双路拆流版）")
    ap.add_argument("--dest", default=None,
                    help="目标根目录（HAOS 默认 /media/tpcamera/door-lock；Windows 默认 M:/tpcamera/door-lock）")
    ap.add_argument("--hours", type=int, default=24, help="向前查询多少小时")
    ap.add_argument("--limit", type=int, default=1, help="最多处理最近的几段，0=不限；默认 1 段低频运行")
    ap.add_argument("--retry", type=int, default=1, help="单段失败重试次数（通道不可用时不重试）")
    ap.add_argument("--retry-wait", type=int, default=60, help="重试退避基数（秒），逐次递增")
    ap.add_argument("--cooldown", type=int, default=30,
                    help="两段之间的冷却秒数。设备同时只允许一路取流，紧接着拉下一段会失败")
    ap.add_argument("--ffmpeg", default=None, help="ffmpeg 可执行文件，默认自动探测（HAOS 优先 /config/bin/ffmpeg）")
    ap.add_argument("--config-entries", default=None, help="HA 配置文件路径（默认按平台自动）")
    ap.add_argument("--state-glob", default=None, help="HA 持久化状态文件（读开门事件时间）")
    ap.add_argument("--keep-raw", action="store_true", help="保留 -c copy 的 .mkv 原件（默认删）")
    ap.add_argument("--dry-run", action="store_true", help="只列 SD 录像清单，不下载")
    ap.add_argument("--check", action="store_true", help="只做环境自检（凭据/ffmpeg/目录），不取流")
    ap.add_argument("--at", default="",
                    help="只补指定段，逗号分隔 HHMMSS 或 'YYYY-MM-DD HHMMSS'（配合 --limit）")
    ap.add_argument("--event-priority", action="store_true",
                    help="优先补与开门时刻接近的段，让低频补录先产出开门录像")
    ap.add_argument("--event-window", type=int, default=90, help="开门时刻匹配窗口（秒）")
    ap.add_argument("--log-file", default="", help="把日志同时追加到这个文件（HAOS 定时任务建议开）")
    ap.add_argument("--work-dir", default="",
                    help="中间文件目录（默认 <dest>/.work，与目标同盘，退出即清）")
    ap.add_argument("--min-interval", type=int, default=600,
                    help="两次真正取流之间的最小间隔秒数（默认 600）。门锁开门事件会频繁触发补录，"
                         "靠它挡住连续取流打爆回放通道（§8.5 冷却 15-20 分钟）。"
                         "取值必须小于 30 分钟定时周期，否则定时那轮会被自己的上一轮挡住")
    ap.add_argument("--no-throttle", action="store_true",
                    help="忽略最小间隔节流（手动补录时用）")
    ap.add_argument("--no-lock", action="store_true", help="不检查进程互斥锁")
    ap.add_argument("--no-index", action="store_true", help="不写干净白名单 clean_clips.json")
    args = ap.parse_args()

    set_log_file(args.log_file or os.environ.get("EZVIZ_LOG_FILE") or None)
    log(f"=== ezviz_sd_pull {VERSION} 启动（{sys.platform}/{os.uname().machine if hasattr(os, 'uname') else '?'}） ===")

    dest_arg = args.dest or default_dest()
    cfg_arg = args.config_entries or default_config_entries()
    state_glob = args.state_glob or default_state_glob()
    ffmpeg = find_ffmpeg(args.ffmpeg)
    root = Path(dest_arg)

    # ---- 环境自检：一次把坑说清，别跑到一半才炸 ----
    cred = load_credentials(cfg_arg)
    version, problems = check_ffmpeg(ffmpeg)
    log(f"ffmpeg：{ffmpeg}")
    log(f"  版本：{version or '(未知)'}")
    log(f"设备：{cred['device_serial']}  凭据：{cfg_arg}")
    log(f"目标：{root}")
    if problems:
        for p in problems:
            log(f"  ✗ {p}")
        log("环境自检不通过：请换用带 libx264 的 ffmpeg（放到 /config/bin/ffmpeg 或指定 --ffmpeg）")
        return 4
    log("  能力：libx264 / hevc 解码 / hevc_mp4toannexb 全部就绪")
    bad = check_writable(root)
    if bad:
        log(f"  ✗ {bad}")
        return 4
    if args.check:
        log("环境自检通过（--check 模式，不取流）")
        return 0

    lock = RunLock(root / ".pull.lock")
    if not args.no_lock and not lock.acquire():
        log("另一个补录进程正在运行（.pull.lock 被占用），本轮跳过")
        return 3
    try:
        return _run(args, cred, ffmpeg, root, state_glob)
    finally:
        lock.release()


def _run(args, cred: dict, ffmpeg: str, root: Path, state_glob: str) -> int:
    api = Ezviz(cred)
    now_ms = int(time.time() * 1000)
    segs = api.sd_segments(now_ms - args.hours * 3600 * 1000, now_ms)
    log(f"设备 {cred['device_serial']}：近 {args.hours}h 的 SD 卡录像共 {len(segs)} 段")

    def already_done(seg: dict) -> bool:
        st = datetime.fromtimestamp(seg["startTime"] / 1000)
        target = root / st.strftime("%Y-%m-%d") / (st.strftime("%H%M%S") + "_sd.mp4")
        return target.exists() and target.stat().st_size > MIN_RAW_BYTES

    # 只选「还没落盘」的段：定时任务每次拉一段，就能逐段把历史补齐，
    # 而不是永远盯着最新那一段、发现已存在就直接跳过。
    pending = [s for s in segs if not already_done(s)]
    log(f"其中待补 {len(pending)} 段（已有 {len(segs) - len(pending)} 段）")
    fail_path = root / ".pull_failures.json"
    try:
        failures: dict = json.loads(fail_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        failures = {}

    if args.at:
        wanted = {x.strip() for x in args.at.split(",") if x.strip()}
        def _wanted(seg: dict) -> bool:
            st = datetime.fromtimestamp(seg["startTime"] / 1000)
            return (st.strftime("%H%M%S") in wanted
                    or st.strftime("%Y-%m-%d %H%M%S") in wanted)
        pending = [s for s in pending if _wanted(s)]
        log(f"  --at 指定 {len(wanted)} 个时刻，命中待补 {len(pending)} 段")
    elif args.event_priority:
        pending = event_priority(pending, state_glob, args.event_window, failures)

    if args.dry_run:
        for s in pending:
            t = datetime.fromtimestamp(s["startTime"] / 1000)
            log(f"  待补 {t:%Y-%m-%d %H:%M:%S}  {(s['endTime']-s['startTime'])/1000:.0f}s")
        return 0

    # 事件优先模式取「排在最前」的段；旧模式仍取最新的一段
    if args.event_priority and not args.at:
        segs = pending[: args.limit] if args.limit else pending
    else:
        segs = pending[-args.limit :] if args.limit else pending
    if not segs:
        log("没有待补录像，结束")
        return 0

    # 最小间隔节流：开门事件会把补录触发得很密集（一次连开三次门就是三个触发），
    # 而回放通道连续取流 12-14 段后必然进入 15-20 分钟冷却（表现为 404 / 512x288 提示图）。
    # 这里卡一道闸：距上次真正取流不足 min-interval 就整轮跳过，待补段留到下一轮，
    # 既不丢段，也不会把设备打到冷却。--at（手动指定）与 --no-throttle 可绕过。
    if args.min_interval > 0 and not args.at and not args.no_throttle:
        st_path = root / ".pull_state.json"
        last_ms = 0
        try:
            last_ms = int((json.loads(st_path.read_text(encoding="utf-8")) or {}).get("last_pull_ms") or 0)
        except (OSError, ValueError, AttributeError):
            last_ms = 0
        if last_ms:
            gap = time.time() - last_ms / 1000.0
            if gap < args.min_interval:
                log(f"距上次取流 {gap:.0f}s，未到最小间隔 {args.min_interval}s："
                    f"本轮跳过，待补 {len(pending)} 段留待下次")
                return 5
    try:
        atomic_write(root / ".pull_state.json",
                     json.dumps({"last_pull_ms": int(time.time() * 1000),
                                 "min_interval_s": args.min_interval}, ensure_ascii=False))
    except OSError as err:
        log(f"  节流状态写入跳过：{err}")

    # 中间文件：默认与目标同盘（HAOS 上 /tmp 在 overlay，够用但同盘更稳）
    work_root = Path(args.work_dir) if args.work_dir else root / ".work"
    try:
        work_root.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        log(f"中间目录不可用（{err}），改用系统临时目录")
        work_root = Path(tempfile.gettempdir())

    ok = skipped = failed = 0
    abort = False
    for idx, seg in enumerate(segs, 1):
        if abort:
            break
        start = datetime.fromtimestamp(seg["startTime"] / 1000)
        duration = max(3, int((seg["endTime"] - seg["startTime"]) / 1000))
        date_dir = root / start.strftime("%Y-%m-%d")
        stem = start.strftime("%H%M%S") + "_sd"
        out_mp4 = date_dir / f"{stem}.mp4"
        keep_mkv = date_dir / f"{stem}.mkv"   # 仅 --keep-raw 时才复制回交付目录
        if out_mp4.exists() and out_mp4.stat().st_size > MIN_RAW_BYTES:
            log(f"[{idx}/{len(segs)}] {stem} 已存在，跳过")
            skipped += 1
            continue

        log(f"[{idx}/{len(segs)}] {start:%m-%d %H:%M:%S} 时长 {duration}s -> {out_mp4}")
        good = False
        # 原始交织流（实测 98% 绿帧）只落中间目录，用完必删：
        # 就算进程被中断，也不会在 /media 里留一个点开全是绿的 .mkv。
        td = tempfile.mkdtemp(prefix="ezviz_pull_", dir=str(work_root))
        try:
            raw_mkv = Path(td) / f"{stem}.mkv"
            for attempt in range(1, args.retry + 2):
                try:
                    url = api.playback_url(seg)
                    if not url:
                        raise RuntimeError("取流地址为空")
                    try:
                        date_dir.mkdir(parents=True, exist_ok=True)
                    except PermissionError:
                        raise RuntimeError(
                            f"{date_dir} 不可写：HAOS 上请以 root 运行（HA 主容器内）或加 sudo"
                        ) from None
                    raw_mkv.unlink(missing_ok=True)
                    pull_raw(ffmpeg, url, raw_mkv, duration)
                    if not raw_mkv.exists() or raw_mkv.stat().st_size < MIN_RAW_BYTES:
                        size = raw_mkv.stat().st_size if raw_mkv.exists() else 0
                        raise ChannelUnavailable(f"回放流无数据/仅提示图（{size} B）")
                    info = probe_stream(ffmpeg, raw_mkv)
                    if is_placeholder(info):
                        raise ChannelUnavailable(f"只拿到 512x288 提示流（{info[:70]}）")
                    log(f"  原件 {raw_mkv.stat().st_size} B | {info[:100]}")
                    h265, desc = split_interleaved_hevc(ffmpeg, raw_mkv, Path(td))
                    to_playable(ffmpeg, h265, out_mp4)
                    # 成品逐帧兜底：拆流选错路 / 编码异常时，绿片绝不落到交付目录
                    n, green = green_frame_ratio(ffmpeg, out_mp4)
                    if n == 0 or green > 0.2:
                        out_mp4.unlink(missing_ok=True)
                        raise RuntimeError(
                            f"成品逐帧自检不合格（{n} 帧 / 绿帧 {green * 100:.1f}%）"
                        )
                    log(
                        f"  完成 {out_mp4.stat().st_size} B（{desc}，15fps H.264，"
                        f"逐帧绿 {green * 100:.1f}%）"
                    )
                    if not args.no_index:
                        mark_clean(root, out_mp4, n, green, 0)
                    if args.keep_raw:
                        shutil.copyfile(raw_mkv, keep_mkv)
                    good = True
                    break
                except ChannelUnavailable as err:
                    log(f"  回放通道不可用：{err}")
                    log("  整批中止，不再高频打设备；请隔一段时间用 --limit 1 重试")
                    abort = True
                    break
                except Exception as err:  # noqa: BLE001 - 单段失败不能中断整批
                    log(f"  第 {attempt} 次失败：{err}")
                    if attempt <= args.retry:
                        time.sleep(args.retry_wait * attempt)
        finally:
            shutil.rmtree(td, ignore_errors=True)

        key = start.strftime("%Y-%m-%d/%H%M%S")
        if good:
            failures.pop(key, None)
            ok += 1
            if idx < len(segs):
                time.sleep(args.cooldown)
        else:
            failures[key] = time.time()
            failed += 1
            try:
                out_mp4.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            atomic_write(fail_path, json.dumps(failures, ensure_ascii=False))
        except OSError as err:
            log(f"  失败降权记录写入跳过：{err}")

    log(f"汇总：成功 {ok} / 跳过 {skipped} / 失败 {failed}，输出目录 {root}")
    if abort:
        log("结论：回放通道当前不可用（非脚本故障），稍后低频重试即可")
        return 2
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
