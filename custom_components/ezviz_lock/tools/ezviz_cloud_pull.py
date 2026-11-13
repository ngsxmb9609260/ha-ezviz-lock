#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""萤石门锁「云存储录像回放」(recType=1 / type=3) -> HAOS /media 补录器。

与现有 SD 回放链路的关系
------------------------
**完全独立、互为兜底**：本脚本不 import、不修改 `ezviz_sd_pull.py`，
只往同一份 `clean_clips.json` 追加验收合格的记录。SD 链路一行不动。

  /api/lapp/video/by/time        recType=1  -> 云存储录像段（开门事件触发的约 8 秒短片）
  /api/lapp/v2/live/address/get  type=3     -> 云存储回放地址（FLV）
        |
        v
  ffmpeg -c copy              -> .mkv 原件（HEVC 29.92fps 交织 + AAC 16kHz mono）
  按帧 NAL 奇偶拆 A/B        -> 自动挑干净那一路（B 路实测 100% 绿，丢弃）
  ffmpeg 解码 + libx264       -> 15fps H.264
  原件里的 AAC 音轨           -> 与干净视频重新 mux（SD 回放链路没有音轨）
  逐帧验绿                   -> 合格才登记 clean_clips.json

为什么这条链路更值钱（2026-11-13 15:25–15:40 实测）
---------------------------------------------------
- 27 段云录像 vs HA 40 条开门记录，±15 秒内匹配 16 对，多数只差 0–2 秒：
  云段本身就是开门事件触发的短片，时间点精确，不需要 ±90 秒就近配。
- 首帧人工确认是真实掌静脉开门画面（人抬手、抱快递），无绿无黑。
- 云回放流带 AAC 16 kHz mono 音轨；SD 回放流从头到尾没有音轨。
- 全程不打扰设备（读云端已存好的文件），不占 SD 回放的会话配额。

产物
----
`<dest>/<YYYY-MM-DD>/<HHMMSS>_cloud.mp4`，H.264 + AAC，浏览器可直接播。
白名单记录带 `source=cloud` / `has_audio` / `usable_for_open=true`，
集成侧 `_clip_rank()` 把 `_cloud.mp4` 排在 `_sd.mp4` 之前（更高优先级）。

B 路状态（如实说）
------------------
换到云回放后 B 路 **依然 100% 绿**。29.92fps ≈ 2×15fps 双路交织，
360 个 slice 的 `layer_id` 全为 0，排除 SHVC 分层假设。B 路是跨路预测
还是私有加密（crypt=23）**未验证**，本脚本直接丢弃。

用法
----
  # HAOS 主容器（由 shell_command 后台调用）
  python3 /config/scripts/ezviz_cloud_pull.py --limit 1 --event-priority
  # 只列云段、不取流
  python3 /config/scripts/ezviz_cloud_pull.py --dry-run --hours 24
  # 指定段（绕过节流）
  python3 /config/scripts/ezviz_cloud_pull.py --at 173527 --no-throttle

凭据：优先环境变量 EZVIZ_APP_KEY / EZVIZ_APP_SECRET / EZVIZ_SERIAL，
否则读 HA 的 `/config/.storage/core.config_entries`（本机调试回退 Y:）。
"""

from __future__ import annotations

import argparse
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
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

VERSION = "2026-11-13-cloud1"
DEFAULT_REGION_HOST = "https://open.ys7.com"
REQUEST_TIMEOUT = 20
PLACEHOLDER_RESOLUTIONS = ("512x288", "512x272", "640x360")
MIN_RAW_BYTES = 20_000
CLOUD_SUFFIX = "_cloud.mp4"

HA_CONFIG_DIR = Path("/config")
HA_MEDIA_DIR = Path("/media/tpcamera/door-lock")

_log_file: str | None = None


def _is_haos() -> bool:
    return (HA_CONFIG_DIR / ".storage").is_dir()


def default_dest() -> str:
    return str(HA_MEDIA_DIR) if _is_haos() else "M:/tpcamera/door-lock"


def default_config_entries() -> str:
    return str(HA_CONFIG_DIR / ".storage" / "core.config_entries") if _is_haos() else r"Y:/.storage/core.config_entries"


def default_state_glob() -> str:
    return str(HA_CONFIG_DIR / ".storage" / "ezviz_lock.state.*") if _is_haos() else r"Y:/.storage/ezviz_lock.state.*"


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if _log_file:
        try:
            p = Path(_log_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > 5 * 1024 * 1024:
                p.replace(p.with_suffix(p.suffix + ".1"))
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def set_log_file(path: str | None) -> None:
    global _log_file
    _log_file = path


def find_ffmpeg(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("FFMPEG_BIN"):
        return os.environ["FFMPEG_BIN"]
    for c in ("/config/bin/ffmpeg",):
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    w = shutil.which("ffmpeg")
    if w:
        return w
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


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
        raise SystemExit(f"找不到 HA 配置文件 {cfg_path}，请挂载 /config 或用环境变量提供凭据")
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
    raise SystemExit("HA 配置里没有 ezviz_lock 条目")


def atomic_write(path: Path, text: str) -> None:
    """临时文件 + replace：集成每 20 秒读一次白名单，绝不能读到半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class RunLock:
    """非阻塞进程锁：和 SD 链路用不同的锁文件，两条链路互不阻塞。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        if os.name == "nt":
            return True
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


class ChannelUnavailable(RuntimeError):
    """云回放地址整体不可用（404/405/提示图），整批立即中止。"""


class EzvizCloud:
    def __init__(self, cred: dict) -> None:
        self.cred = cred
        self.base = cred["region_host"].rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._token: str | None = None

    def post(self, path: str, params: dict) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=urllib.parse.urlencode(params).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
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

    def cloud_segments(self, start_ms: int, end_ms: int) -> list[dict]:
        """recType=1 = 云存储录像（recType=2 才是本地 SD）。

        实测 2026-11-13 15:55：这个接口对「长时间跨度」只回最早的一批
        （24h 查询只给 11-12 的 19 段，漏掉今天 10:42–15:05 的 12 段；
        6h 查询反而能全给）。所以按 6 小时分片查，再按 startTime 去重合并。
        返回的字段名是 `endTime`，不是 `stopTime`（`stopTime` 只是取流请求的参数名）。
        """
        step = 6 * 3600 * 1000
        merged: dict[int, dict] = {}
        cursor = start_ms
        first_error = None
        while cursor < end_ms:
            stop = min(cursor + step, end_ms)
            try:
                res = self.post(
                    "/api/lapp/video/by/time",
                    {
                        "deviceSerial": self.cred["device_serial"],
                        "channelNo": "1",
                        "startTime": str(cursor),
                        "endTime": str(stop),
                        "recType": "1",
                        "accessToken": self.token,
                    },
                )
            except Exception as err:  # noqa: BLE001 - 单片失败不影响其它片
                first_error = first_error or err
                cursor = stop
                continue
            if str(res.get("code")) != "200":
                first_error = first_error or RuntimeError(
                    f"查询云录像失败：{res.get('code')} {res.get('msg')}")
                cursor = stop
                continue
            data = res.get("data")
            rows = [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []
            for s in rows:
                s = dict(s)
                # 兼容万一某天接口改回 stopTime
                if not s.get("endTime") and s.get("stopTime"):
                    s["endTime"] = s["stopTime"]
                st = s.get("startTime")
                if st and s.get("endTime"):
                    merged[int(st)] = s
            cursor = stop
            if stop < end_ms:
                time.sleep(0.2)
        if not merged and first_error:
            raise first_error
        return [merged[k] for k in sorted(merged)]

    @staticmethod
    def seg_end(seg: dict) -> int:
        return int(seg.get("endTime") or seg.get("stopTime") or 0)

    def playback_url(self, seg: dict) -> str | None:
        """type=3 = 云存储录像回放（type=1 预览 / type=2 本地 SD 回放）。"""
        fmt = lambda ms: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))
        res = self.post(
            "/api/lapp/v2/live/address/get",
            {
                "deviceSerial": self.cred["device_serial"],
                "channelNo": "1",
                "protocol": "4",           # FLV
                "type": "3",               # 云存储录像回放
                "quality": "1",
                "startTime": fmt(seg["startTime"]),
                "stopTime": fmt(self.seg_end(seg)),
                "supportH265": "1",        # 不给就只回 512x288 提示图
                "expireTime": "3600",
                "accessToken": self.token,
            },
        )
        if str(res.get("code")) != "200":
            log(f"  取云回放地址失败 code={res.get('code')} msg={res.get('msg')}")
            return None
        data = res.get("data") or {}
        return str(data["url"]) if isinstance(data, dict) and data.get("url") else None


def probe_stream(ffmpeg: str, path: Path) -> str:
    res = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, errors="replace", timeout=30,
    )
    lines = [l.strip() for l in (res.stderr or "").splitlines() if "Stream #" in l and "Video:" in l]
    return lines[0] if lines else ""


def has_audio_stream(ffmpeg: str, path: Path) -> bool:
    res = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, errors="replace", timeout=30,
    )
    return any("Stream #" in l and "Audio:" in l for l in (res.stderr or "").splitlines())


def is_placeholder(info: str) -> bool:
    if not info:
        return True
    if any(r in info for r in PLACEHOLDER_RESOLUTIONS):
        return True
    m = re.search(r"(\d{3,4})x(\d{3,4})", info)
    return bool(m and int(m.group(1)) < 640)


def pull_raw(ffmpeg: str, url: str, dest: Path, seconds: int) -> bool:
    """原样直拷：保留 HEVC 视频 + AAC 音轨（SD 版只 map 视频）。"""
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-rw_timeout", "30000000",
        "-i", url,
        "-t", str(max(3, seconds)),
        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
        "-f", "matroska", str(dest),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                         timeout=seconds + 120)
    err = res.stderr or ""
    ok = dest.exists() and dest.stat().st_size > 0
    if res.returncode != 0 and not ok:
        if any(code in err for code in ("404", "405", "403", "401")):
            raise ChannelUnavailable("云回放 URL 被拒绝（" + ", ".join(
                c for c in ("404", "405", "403", "401") if c in err) + "）")
        raise RuntimeError(f"ffmpeg 取流失败：{err[-300:]}")
    return has_audio_stream(ffmpeg, dest) if ok else False


def extract_audio(ffmpeg: str, raw: Path, dest: Path) -> bool:
    """把云回放自带 AAC 抽成 m4a，供清洗后的视频重新 mux。"""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(raw), "-map", "0:a:0", "-vn",
           "-c:a", "aac", "-b:a", "32k", "-ar", "16000", "-ac", "1",
           "-f", "mp4", str(dest)]
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120)
    return res.returncode == 0 and dest.exists() and dest.stat().st_size > 1000


def iter_annexb_nals(data: bytes):
    matches = list(re.finditer(rb"\x00\x00\x00?\x01", data))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(data)
        if start < end:
            yield data[start:end], (data[start] >> 1) & 0x3F


def green_frame_ratio(ffmpeg: str, path: Path) -> tuple[int, float]:
    """解码到 94x123 RGB，逐帧统计纯绿像素；返回 (帧数, 绿帧占比)。"""
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
    """云回放是 29.92fps ≈ 2×15fps 双路交织；按帧 NAL 奇偶拆开自动挑干净路。"""
    annexb = workdir / "raw.h265"
    res = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
         "-map", "0:v:0", "-c", "copy", "-bsf:v", "hevc_mp4toannexb",
         "-f", "hevc", str(annexb)],
        capture_output=True, text=True, errors="replace", timeout=180,
    )
    if not annexb.exists() or annexb.stat().st_size < MIN_RAW_BYTES:
        raise RuntimeError(f"转 Annex-B 失败：{(res.stderr or '')[-300:]}")

    data = annexb.read_bytes()
    params: list[bytes] = []
    frames: list[bytes] = []
    for payload, nal_type in iter_annexb_nals(data):
        if nal_type in (32, 33, 34):
            params.append(payload)
        elif 1 <= nal_type <= 21:
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
        raise RuntimeError("两路都不干净，拆流方法对这段云码流不适用")
    best = min(usable, key=lambda c: (c[3], -c[2]))
    desc = f"{best[0]} 路 / {best[2]} 帧 / 绿帧 {best[3] * 100:.1f}%"
    log(f"  采用 {desc}")
    return best[1], desc


def to_playable(ffmpeg: str, h265: Path, out: Path, audio: Path | None,
                fps: int = 15) -> bool:
    """干净 HEVC 正常解码 + H.264 重编码；有音轨就一起 mux（SD 版是 -an）。"""
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-r", str(fps), "-i", str(h265),
    ]
    if audio is not None:
        cmd += ["-i", str(audio)]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p"]
    if audio is not None:
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:a", "copy", "-shortest"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", "-f", "mp4"]

    for mode in (["-fps_mode", "passthrough"], ["-vsync", "0"], []):
        if out.exists():
            out.unlink()
        res = subprocess.run(cmd + mode + [str(out)], capture_output=True,
                             text=True, errors="replace", timeout=300)
        err = res.stderr or ""
        if res.returncode == 0 and out.exists() and out.stat().st_size > MIN_RAW_BYTES:
            return audio is not None
        if "Unrecognized option" not in err and "Option not found" not in err:
            raise RuntimeError(f"H.264 重编码失败：{err[-400:]}")
    raise RuntimeError("H.264 重编码失败（新旧参数均不可用）")


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mark_clean(root: Path, out: Path, frames: int, green: float,
               has_audio: bool) -> None:
    """登记进与 SD 链路共用的逐帧验收白名单，附带云源/音轨标记。"""
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
        "duration_s": round(frames / 15.0, 2),
        "start_ms": start_ms,
        "codec": "h264+aac" if has_audio else "h264",
        "source": "cloud",
        "has_audio": bool(has_audio),
        "usable_for_open": bool(re.fullmatch(r"\d{6}_cloud\.mp4", out.name)),
        "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "verified_by": f"ezviz_cloud_pull.py 逐帧自检（{VERSION}）",
    }
    clean = [c for c in idx.get("clean", []) if c.get("rel") != rel]
    clean.append(rec)
    idx["clean"] = sorted(clean, key=lambda c: c["rel"])
    idx["generated_at"] = rec["verified_at"]
    idx["rule"] = {
        "max_green_ratio": 0.02,
        "min_frames": 15,
        "note": "逐帧解码统计纯绿像素占比；绿片一律不进匹配池（SD / 云回放共用）",
    }
    atomic_write(idx_path, json.dumps(idx, ensure_ascii=False, indent=1))
    log(f"  已登记干净白名单：{rel}（{frames} 帧 / 绿 {green * 100:.2f}% / 音轨 {has_audio}）")


def event_priority(pending: list[dict], state_glob: str, window_s: int) -> list[dict]:
    """与开门/徘徊时刻接近的云段排最前（云段本身即事件触发，通常 0–2 秒）。"""
    import glob
    times: list[int] = []
    for p in sorted(glob.glob(state_glob)):
        try:
            d = json.loads(Path(p).read_text(encoding="utf-8"))["data"]
            times += [int(e["open_time_ms"]) for e in (d.get("events") or [])
                      if not e.get("is_close") and e.get("open_time_ms")]
            times += [int(e["time_ms"]) for e in (d.get("loiter") or [])
                      if e.get("time_ms")]
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if not times:
        return pending
    def nearest(seg: dict) -> float:
        t = int(seg["startTime"])
        return min(abs(t - x) for x in times) / 1000.0
    hit = [s for s in pending if nearest(s) <= window_s]
    rest = [s for s in pending if nearest(s) > window_s]
    if hit:
        log(f"  按开门/徘徊时刻优先：{len(hit)} 段命中 ±{window_s}s（读取 {len(times)} 个事件）")
    return sorted(hit, key=lambda s: -int(s["startTime"])) + sorted(
        rest, key=lambda s: -int(s["startTime"]))


def main() -> int:
    ap = argparse.ArgumentParser(description="萤石云存储录像(type=3)补录到 HAOS /media")
    ap.add_argument("--dest", default=None, help="目标根目录（HAOS 默认 /media/tpcamera/door-lock）")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--limit", type=int, default=1, help="每轮最多处理几段，0=不限")
    ap.add_argument("--retry", type=int, default=1)
    ap.add_argument("--retry-wait", type=int, default=30)
    ap.add_argument("--cooldown", type=int, default=15)
    ap.add_argument("--ffmpeg", default=None)
    ap.add_argument("--config-entries", default=None)
    ap.add_argument("--state-glob", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--at", default="", help="只补指定段 HHMMSS 或 'YYYY-MM-DD HHMMSS'")
    ap.add_argument("--event-priority", action="store_true")
    ap.add_argument("--event-window", type=int, default=30)
    ap.add_argument("--log-file", default="")
    ap.add_argument("--work-dir", default="")
    ap.add_argument("--min-interval", type=int, default=300,
                    help="两次取流的最小间隔秒数（默认 300，独立于 SD 链路）")
    ap.add_argument("--no-throttle", action="store_true")
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--no-index", action="store_true")
    ap.add_argument("--keep-raw", action="store_true", help="保留 -c copy 的 .mkv 原件")
    args = ap.parse_args()

    set_log_file(args.log_file or os.environ.get("EZVIZ_LOG_FILE") or None)
    log(f"=== ezviz_cloud_pull {VERSION} 启动（{sys.platform}） ===")

    dest_arg = args.dest or default_dest()
    cfg_arg = args.config_entries or default_config_entries()
    state_glob = args.state_glob or default_state_glob()
    ffmpeg = find_ffmpeg(args.ffmpeg)
    root = Path(dest_arg)
    cred = load_credentials(cfg_arg)
    log(f"设备 {cred['device_serial']}  凭据 {cfg_arg}  目标 {root}  ffmpeg {ffmpeg}")
    if args.check:
        try:
            root.mkdir(parents=True, exist_ok=True)
            log("环境自检通过（--check，不取流）")
            return 0
        except OSError as err:
            log(f"环境自检失败：{err}")
            return 4

    lock = RunLock(root / ".cloud_pull.lock")
    if not args.no_lock and not lock.acquire():
        log("另一个云补录进程正在运行，本轮跳过")
        return 3
    try:
        return _run(args, cred, ffmpeg, root, state_glob)
    finally:
        lock.release()


def _run(args, cred: dict, ffmpeg: str, root: Path, state_glob: str) -> int:
    api = EzvizCloud(cred)
    now_ms = int(time.time() * 1000)
    segs = api.cloud_segments(now_ms - args.hours * 3600 * 1000, now_ms)
    log(f"近 {args.hours}h 的云存储录像共 {len(segs)} 段")

    def out_path(seg: dict) -> Path:
        st = datetime.fromtimestamp(seg["startTime"] / 1000)
        return root / st.strftime("%Y-%m-%d") / (st.strftime("%H%M%S") + CLOUD_SUFFIX)

    def already_done(seg: dict) -> bool:
        p = out_path(seg)
        return p.exists() and p.stat().st_size > MIN_RAW_BYTES

    pending = [s for s in segs if not already_done(s)]
    log(f"其中待补 {len(pending)} 段（已有 {len(segs) - len(pending)} 段）")
    fail_path = root / ".cloud_pull_failures.json"
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
        pending = event_priority(pending, state_glob, args.event_window)

    # 1 小时内失败过的段降权，避免定时任务反复卡在同一段
    now = time.time()
    def recently_failed(seg: dict) -> bool:
        st = datetime.fromtimestamp(seg["startTime"] / 1000)
        key = st.strftime("%Y-%m-%d/%H%M%S")
        return now - float(failures.get(key, 0)) < 3600
    fresh = [s for s in pending if not recently_failed(s)]
    stale = [s for s in pending if recently_failed(s)]
    pending = fresh + sorted(stale, key=lambda s: -int(s["startTime"]))

    if args.dry_run:
        for s in pending:
            t = datetime.fromtimestamp(s["startTime"] / 1000)
            log(f"  待补 {t:%Y-%m-%d %H:%M:%S}  {(api.seg_end(s)-s['startTime'])/1000:.0f}s"
                f"  fileId={s.get('fileId') or '-'}")
        return 0
    todo = pending[: args.limit] if args.limit else pending
    if not todo:
        log("没有待补云录像，结束")
        return 0

    if args.min_interval > 0 and not args.at and not args.no_throttle:
        st_path = root / ".cloud_pull_state.json"
        last_ms = 0
        try:
            last_ms = int((json.loads(st_path.read_text(encoding="utf-8")) or {}).get("last_pull_ms") or 0)
        except (OSError, ValueError, AttributeError):
            last_ms = 0
        if last_ms:
            gap = time.time() - last_ms / 1000.0
            if gap < args.min_interval:
                log(f"距上次云取流 {gap:.0f}s，未到最小间隔 {args.min_interval}s："
                    f"本轮跳过，待补 {len(pending)} 段留待下次")
                return 5
    try:
        atomic_write(root / ".cloud_pull_state.json",
                     json.dumps({"last_pull_ms": int(time.time() * 1000),
                                 "min_interval_s": args.min_interval}, ensure_ascii=False))
    except OSError as err:
        log(f"  节流状态写入跳过：{err}")

    work_root = Path(args.work_dir) if args.work_dir else root / ".work_cloud"
    work_root.mkdir(parents=True, exist_ok=True)
    ok = failed = 0
    abort = False
    for idx, seg in enumerate(todo, 1):
        if abort:
            break
        start = datetime.fromtimestamp(seg["startTime"] / 1000)
        duration = max(3, int((api.seg_end(seg) - seg["startTime"]) / 1000))
        out_mp4 = out_path(seg)
        stem = out_mp4.stem
        if out_mp4.exists() and out_mp4.stat().st_size > MIN_RAW_BYTES:
            log(f"[{idx}/{len(todo)}] {stem} 已存在，跳过")
            continue
        log(f"[{idx}/{len(todo)}] {start:%m-%d %H:%M:%S} 时长 {duration}s -> {out_mp4}")
        good = False
        with tempfile.TemporaryDirectory(prefix="ezviz_cloud_", dir=str(work_root)) as td:
            wd = Path(td)
            raw_mkv = wd / f"{stem}.mkv"
            for attempt in range(1, args.retry + 2):
                try:
                    url = api.playback_url(seg)
                    if not url:
                        raise RuntimeError("云回放地址为空")
                    out_mp4.parent.mkdir(parents=True, exist_ok=True)
                    raw_mkv.unlink(missing_ok=True)
                    audio_present = pull_raw(ffmpeg, url, raw_mkv, duration)
                    if not raw_mkv.exists() or raw_mkv.stat().st_size < MIN_RAW_BYTES:
                        size = raw_mkv.stat().st_size if raw_mkv.exists() else 0
                        raise ChannelUnavailable(f"云回放流无数据/仅提示图（{size} B）")
                    info = probe_stream(ffmpeg, raw_mkv)
                    if is_placeholder(info):
                        raise ChannelUnavailable(f"只拿到 512x288 提示流（{info[:70]}）")
                    log(f"  原件 {raw_mkv.stat().st_size} B | {info[:100]} | 音轨 {audio_present}")

                    h265, desc = split_interleaved_hevc(ffmpeg, raw_mkv, wd)
                    audio_m4a = None
                    if audio_present:
                        audio_m4a = wd / f"{stem}.m4a"
                        if not extract_audio(ffmpeg, raw_mkv, audio_m4a):
                            log("  音轨抽取失败，本段退化为无声音（不影响画面）")
                            audio_m4a = None
                    has_audio = to_playable(ffmpeg, h265, out_mp4, audio_m4a)

                    n, green = green_frame_ratio(ffmpeg, out_mp4)
                    if n == 0 or n < 15 or green > 0.02:
                        out_mp4.unlink(missing_ok=True)
                        raise RuntimeError(
                            f"成品逐帧自检不合格（{n} 帧 / 绿帧 {green * 100:.2f}%）"
                        )
                    log(f"  完成 {out_mp4.stat().st_size} B（{desc}，15fps H.264"
                        f"{' + AAC' if has_audio else ''}，逐帧绿 {green * 100:.2f}%）")
                    if not args.no_index:
                        mark_clean(root, out_mp4, n, green, has_audio)
                    if args.keep_raw:
                        shutil.copyfile(raw_mkv, out_mp4.with_suffix(".mkv"))
                    good = True
                    break
                except ChannelUnavailable as err:
                    log(f"  云回放通道不可用：{err}；整批中止")
                    abort = True
                    break
                except Exception as err:  # noqa: BLE001
                    log(f"  第 {attempt} 次失败：{err}")
                    out_mp4.unlink(missing_ok=True)
                    if attempt <= args.retry:
                        time.sleep(args.retry_wait * attempt)
        key = start.strftime("%Y-%m-%d/%H%M%S")
        if good:
            failures.pop(key, None)
            ok += 1
            if idx < len(todo):
                time.sleep(args.cooldown)
        else:
            failures[key] = time.time()
            failed += 1
        try:
            atomic_write(fail_path, json.dumps(failures, ensure_ascii=False))
        except OSError as err:
            log(f"  失败降权记录写入跳过：{err}")

    log(f"汇总：成功 {ok} / 失败 {failed}，输出目录 {root}")
    if abort:
        log("结论：云回放通道当前不可用（非脚本故障），稍后低频重试即可")
        return 2
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
