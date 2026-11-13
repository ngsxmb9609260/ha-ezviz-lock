"""HEVC 双路交织流的拆流修复（纯标准库，HAOS / 本机通用）。

背景（2026-11-12 实测，CS-Y3000FVX-Plus-V210-B）
------------------------------------------------
这台门锁推的 HEVC 流把**两路帧严格按奇偶交织**在同一条流里：两路各自有
IDR / GOP，POC 各自从 0 递增。解码器收到重复 POC 会丢弃后一路，
未解码宏块输出纯绿（ffmpeg 报 ``Duplicate POC in a sequence``）。

处理：原样 ``-c copy`` 落盘 → 转 Annex-B → 按 VCL NAL 奇偶拆成 A/B →
两路各自解码、逐帧统计绿色像素 → 选干净那一路正常重编码成 H.264。

**自适应保护**：只有「一路干净、另一路绿」时才采用拆流；两路都干净
（正常码流）或都不干净（别的毛病）时抛 SplitError，让调用方回退直接转码，
避免把好流误拆掉一半帧。

本模块不依赖 Home Assistant，也不依赖第三方包，只调 ffmpeg。
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Iterator

START_CODE = b"\x00\x00\x00\x01"
MIN_RAW_BYTES = 20_000
GREEN_FRAME_THRESHOLD = 0.30      # 绿像素 >30% 判为坏帧
MAX_STREAM_GREEN = 0.20           # 整路绿帧占比 <20% 才算干净
_PROBE_W, _PROBE_H = 94, 123      # 缩到这么大只做绿检测，快且够用


class SplitError(RuntimeError):
    """拆流不适用 / 失败，调用方应回退到直接转码。"""


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, errors="replace", timeout=timeout
    )


def iter_annexb_nals(data: bytes) -> Iterator[tuple[bytes, int]]:
    """按 3/4 字节起始码切出 (payload, nal_unit_type)。"""
    marks = list(re.finditer(rb"\x00\x00\x00?\x01", data))
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(data)
        if start < end:
            yield data[start:end], (data[start] >> 1) & 0x3F


def green_frame_ratio(ffmpeg: str, path: Path, max_frames: int = 900) -> tuple[int, float]:
    """解码到小尺寸 RGB，逐帧统计纯绿像素，返回 (帧数, 绿帧占比)。"""
    w, h = _PROBE_W, _PROBE_H
    chunk = w * h * 3
    res = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
         "-vf", f"scale={w}:{h}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        capture_output=True, timeout=180,
    )
    data = res.stdout[: max_frames * chunk]
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
        if green * 100.0 / (w * h) > GREEN_FRAME_THRESHOLD * 100:
            bad += 1
    return n, bad / n


def _to_playable(ffmpeg: str, h265: Path, out: Path, fps: int) -> None:
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-r", str(fps), "-i", str(h265),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", "-f", "mp4",
    ]
    last = ""
    for mode in (["-fps_mode", "passthrough"], ["-vsync", "0"]):
        if out.exists():
            out.unlink()
        res = _run(cmd + mode + [str(out)], 300)
        err = res.stderr or ""
        if res.returncode == 0 and out.exists() and out.stat().st_size > MIN_RAW_BYTES:
            return
        last = err
        if "Unrecognized option" not in err and "Option not found" not in err:
            raise SplitError(f"H.264 重编码失败：{err[-300:]}")
    raise SplitError(f"H.264 重编码失败（新旧参数均不可用）：{last[-300:]}")


def repair_interleaved_hevc(
    ffmpeg: str,
    raw: Path,
    out: Path,
    fps: int = 15,
    log: Callable[[str], None] | None = None,
) -> dict:
    """把 ``raw``（原始 HEVC 容器）拆流修复成浏览器可播的 H.264 ``out``。

    成功返回统计 dict；不适用 / 失败抛 :class:`SplitError`，调用方回退。
    """
    say = log or (lambda _msg: None)
    if not raw.is_file() or raw.stat().st_size < MIN_RAW_BYTES:
        raise SplitError(f"原始码流为空或过小（{raw.stat().st_size if raw.exists() else 0} B）")

    with tempfile.TemporaryDirectory(prefix="ezviz_split_") as td:
        work = Path(td)
        annexb = work / "raw.h265"
        res = _run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
             "-c", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(annexb)],
            180,
        )
        if not annexb.exists() or annexb.stat().st_size < MIN_RAW_BYTES:
            raise SplitError(f"转 Annex-B 失败（可能不是 HEVC）：{(res.stderr or '')[-200:]}")

        params: list[bytes] = []
        frames: list[bytes] = []
        for payload, nal_type in iter_annexb_nals(annexb.read_bytes()):
            if nal_type in (32, 33, 34):        # VPS / SPS / PPS
                params.append(payload)
            elif 1 <= nal_type <= 21:           # VCL
                frames.append(payload)
        if len(frames) < 4:
            raise SplitError(f"只找到 {len(frames)} 个帧 NAL，不是交织流")
        if not params:
            raise SplitError("流里没有 VPS/SPS/PPS，无法重组")

        head = b"".join(START_CODE + p for p in params)
        candidates: list[tuple[str, Path, int, float]] = []
        for name, group in (("A", frames[::2]), ("B", frames[1::2])):
            stream = work / f"stream_{name}.h265"
            stream.write_bytes(head + b"".join(START_CODE + f for f in group))
            n, ratio = green_frame_ratio(ffmpeg, stream)
            candidates.append((name, stream, n, ratio))
            say(f"拆流 {name}：{len(group)} 个 NAL -> {n} 帧，绿帧 {ratio * 100:.1f}%")

        clean = [c for c in candidates if c[2] > 0 and c[3] < MAX_STREAM_GREEN]
        dirty = [c for c in candidates if c[2] == 0 or c[3] > 1 - MAX_STREAM_GREEN]
        if len(clean) != 1 or not dirty:
            greens = ", ".join(f"{c[0]}={c[3] * 100:.1f}%" for c in candidates)
            raise SplitError(f"不是「一干净一绿」的交织结构（{greens}），回退直接转码")
        best = clean[0]
        _to_playable(ffmpeg, best[1], out, fps)
        return {
            "stream": best[0],
            "frames": best[2],
            "green": round(best[3] * 100, 2),
            "out_bytes": out.stat().st_size,
            "fps": fps,
        }
