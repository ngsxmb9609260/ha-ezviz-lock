"""「门前看一眼」双摄抓帧：从一条实时流同时解出上摄 / 下摄各一帧 JPEG。

背景（2026-11-14 实测，CS-Y3000FVX-Plus-V210-B）
------------------------------------------------
这台门锁把**上摄 1504×1968 竖屏**与**下摄 1920×1080 横屏**两路 HEVC 帧
严格奇偶交织在同一条流里，却只带一套上摄的 VPS/SPS/PPS。开放平台的
``device/capture`` 只返回上摄图（实测 1504×1968），``channelNo=2`` 直接报
「通道不存在」——平台没有「拍下摄」的独立接口。

解法：取一小段实时流 → 转 Annex-B → 按 VCL NAL 奇偶拆 A/B →
A 用原参数集、B 换成 App 导出的下摄参数集 → 各自解一帧存 JPEG。
两帧来自同一段码流，水印时刻一致（本机实测两张均为 17:22:19）。

本模块不依赖 Home Assistant 与第三方包，只调 ffmpeg，全部同步执行，
由 coordinator 放进 executor，避免阻塞事件循环。
"""

from __future__ import annotations

import re
import struct
import subprocess
import tempfile
from pathlib import Path

START_CODE = b"\x00\x00\x00\x01"
MIN_RAW_BYTES = 20_000
MIN_FRAMES = 8

# 下摄（1920×1080 横屏）的 VPS/SPS/PPS，来自萤石云 App「保存视频」导出的 b.mp4。
# 实时流只有上摄的 1504×1968 SPS，B 路帧必须换这三元参数集才能正常解码。
# 2026-11-14 在「实时预览流 type=1」上再次验证有效（此前只在云回放验证过）。
B_ROAD_PARAMS_HEX: dict[int, str] = {
    32: "40010c01ffff0160000003000003000003000003007bac09",
    33: "4201010160000003000003000003000003007ba003c0801107cb8aad3ba24bb2",
    34: "4401c072f03b24",
}


class DualPeekError(RuntimeError):
    """双路抓帧失败，调用方应降级为单路 device/capture。"""


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=timeout
        )
    except subprocess.TimeoutExpired as err:
        raise DualPeekError(f"ffmpeg 超时（{timeout}s）：{cmd[0]}") from err


def _fetch_live_flv(url: str, dest: Path, *, seconds: float, max_bytes: int, timeout: int) -> int:
    """用标准库拉一小段直播 FLV 落盘，按时间或字节数主动截断。

    为什么不让 ffmpeg 直接读 URL：HA core 容器里的 ffmpeg 3.x 连
    ``rtmp01open.ys7.com:9188`` 会一直卡在 TLS/取流上（实测 70 s 超时），
    而同容器里的 Python urllib 能正常连（SSH addon 里 curl 同样可以）。
    所以「网络取流」交给 urllib，「转码拆帧」交给只处理本地文件的 ffmpeg。
    """
    import time
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "ezviz-ha/1.6"})
    total = 0
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, dest.open("wb") as fh:
            while total < max_bytes and time.monotonic() - started < seconds:
                chunk = resp.read1(16384)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
    except (OSError, urllib.error.URLError) as err:
        raise DualPeekError(f"实时流连接失败：{err}") from err
    return total


def _iter_annexb_nals(data: bytes):
    """按 3/4 字节起始码切出 (payload, nal_unit_type)。"""
    marks = list(re.finditer(rb"\x00\x00\x00?\x01", data))
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(data)
        if start < end:
            yield data[start:end], (data[start] >> 1) & 0x3F


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """不依赖 Pillow，读 SOF 段拿到 JPEG 宽高；非 JPEG 返回 None。"""
    if not data or data[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = struct.unpack(">H", data[i + 5:i + 7])[0]
            w = struct.unpack(">H", data[i + 7:i + 9])[0]
            return (w, h)
        i += 2 + seg
    return None


def _to_jpeg(ffmpeg: str, stream: Path, out: Path, timeout: int = 60) -> tuple[int, int]:
    """从裸 HEVC 流解第一帧存成 JPEG，返回 (宽, 高)。"""
    if out.exists():
        out.unlink()
    res = _run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "hevc", "-r", "15", "-i", str(stream),
         "-frames:v", "1", "-q:v", "2", str(out)],
        timeout,
    )
    if res.returncode != 0 or not out.exists():
        raise DualPeekError(f"截帧失败：{(res.stderr or '')[-200:]}")
    data = out.read_bytes()
    size = jpeg_size(data)
    if not size or len(data) < 10_000 or size[0] < 640:
        raise DualPeekError(
            f"截帧产物不是有效画面：{len(data)} B / {size}"
        )
    return size


def extract_dual_frames(
    ffmpeg: str,
    live_url: str,
    out_up: Path,
    out_down: Path,
    *,
    seconds: float = 3.0,
    stream_timeout: int = 20,
) -> dict:
    """拉一小段实时流，拆出上下两摄各一帧 JPEG。

    ``out_up`` / ``out_down`` 由调用方指定（落在 /media 下）；任一步失败抛
    :class:`DualPeekError`，且不会留下半张图（临时文件在 with 里自动清）。
    """
    if not ffmpeg:
        raise DualPeekError("系统里没有 ffmpeg")
    out_up.parent.mkdir(parents=True, exist_ok=True)
    out_down.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ezviz_peek_") as td:
        work = Path(td)
        flv = work / "live.flv"
        raw = work / "raw.mkv"
        annexb = work / "raw.h265"

        # ① 网络取流（urllib）+ 原样 -c copy 成本地 mkv（ffmpeg 不碰网络）
        fetched = _fetch_live_flv(
            live_url, flv,
            seconds=max(seconds + 5.0, 8.0),
            max_bytes=4_000_000,
            timeout=stream_timeout,
        )
        if fetched < MIN_RAW_BYTES:
            raise DualPeekError(
                f"实时流只拿到 {fetched} B（设备可能在休眠，或上行带宽不足）"
            )
        res = _run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(flv), "-t", str(seconds),
             "-map", "0:v:0", "-c", "copy", "-f", "matroska", str(raw)],
            60,
        )
        if not raw.is_file() or raw.stat().st_size < MIN_RAW_BYTES:
            raise DualPeekError(
                f"本地转封装后数据不足（{raw.stat().st_size if raw.exists() else 0} B）："
                f"{(res.stderr or '')[-160:]}"
            )

        # ② 转 Annex-B，才能按起始码切 NAL
        res = _run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(raw), "-c", "copy", "-bsf:v", "hevc_mp4toannexb",
             "-f", "hevc", str(annexb)],
            60,
        )
        if not annexb.is_file() or annexb.stat().st_size < MIN_RAW_BYTES:
            raise DualPeekError("转 Annex-B 失败，可能不是 HEVC 流")

        params: list[bytes] = []
        frames: list[bytes] = []
        for payload, nal_type in _iter_annexb_nals(annexb.read_bytes()):
            if nal_type in (32, 33, 34):
                params.append(payload)
            elif 1 <= nal_type <= 21:
                frames.append(payload)
        if len(frames) < MIN_FRAMES or not params:
            raise DualPeekError(
                f"码流不是预期交织结构（参数集 {len(params)} / 帧 {len(frames)}）"
            )

        # ③ A 上摄用原参数集；B 下摄换参数集，IDR 前补一份便于解码起播
        a_head = b"".join(START_CODE + p for p in params)
        b_head = b"".join(
            START_CODE + bytes.fromhex(B_ROAD_PARAMS_HEX[t]) for t in (32, 33, 34)
        )
        stream_a = work / "stream_A.h265"
        stream_b = work / "stream_B_fixed.h265"
        stream_a.write_bytes(a_head + b"".join(START_CODE + f for f in frames[::2]))
        buf = bytearray(b_head)
        for frame in frames[1::2]:
            if ((frame[0] >> 1) & 0x3F) in (19, 20, 21):
                buf += b_head
            buf += START_CODE + frame
        stream_b.write_bytes(bytes(buf))

        # ④ 各解一帧；先写在临时目录，全部成功再原子落到目标，
        #    这样取流超时 / 中途失败时 /media 下不会留下半张图。
        up_jpeg = work / "frame_up.jpg"
        down_jpeg = work / "frame_down.jpg"
        up_size = _to_jpeg(ffmpeg, stream_a, up_jpeg)
        down_size = _to_jpeg(ffmpeg, stream_b, down_jpeg)
        out_up.write_bytes(up_jpeg.read_bytes())
        out_down.write_bytes(down_jpeg.read_bytes())
        return {
            "frames": len(frames),
            "up": up_size,
            "down": down_size,
            "up_bytes": out_up.stat().st_size,
            "down_bytes": out_down.stat().st_size,
            "raw_bytes": raw.stat().st_size,
        }
