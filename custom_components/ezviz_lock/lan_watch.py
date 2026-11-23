"""局域网被动探活（lan_watch）—— 只听，不发。

萤石门锁休眠时会周期性发出 ARP 探测帧（实测 2026-11-23：每 55 秒一次，
``who-has <自己的IP> tell <自己的IP>``，用于地址冲突检测兼 WiFi 保活）。
本模块只在二层「听」这些帧，**不向设备发送任何数据**，因此不会把设备唤醒。

与云端 ``online`` 的分工：

* ``online``      —— 设备到萤石云的心跳，10 分钟粒度，看不到局域网状态；
* ``lan_present`` —— 本模块，秒级反映设备是否还挂在 WiFi 上。

注意：ARP 广播在 WiFi 省电模式下会被门锁固件丢弃，所以「问它」是问不出来的
（``arping``/``nmap -sn`` 实测全无应答），只有「听它自己说」才可靠。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

ETH_P_ARP = 0x0806
PACKET_OUTGOING = 4
# 以太头 14 + ARP 28；短于此的直接丢弃
_ARP_FRAME_MIN = 42


class LanWatcher:
    """旁路监听门锁发出的 ARP 帧。线程常驻，stop() 时退出。"""

    def __init__(
        self,
        get_target_ip: Callable[[], str | None],
        serial: str,
        diag_path: Path | None = None,
    ) -> None:
        self._get_target_ip = get_target_ip
        self._serial = serial
        self._diag_path = diag_path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_seen_mono: float | None = None
        self._last_seen_wall: float | None = None
        self._last_gap: float | None = None
        self._mac: str | None = None
        self._iface: str | None = None
        self._source: str | None = None
        self._hits = 0
        self._error: str | None = None
        self.started_at = time.monotonic()

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"ezviz-lan-{self._serial}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------ 只读状态
    @property
    def error(self) -> str | None:
        return self._error

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def age_s(self) -> float | None:
        """距上一次见到门锁的秒数；从未见到时为 None。"""
        mono = self._last_seen_mono
        return None if mono is None else time.monotonic() - mono

    def snapshot(self) -> dict[str, Any]:
        mono = self._last_seen_mono
        age = None if mono is None else time.monotonic() - mono
        seen = None
        if self._last_seen_wall is not None:
            seen = datetime.fromtimestamp(self._last_seen_wall).astimezone().isoformat(
                timespec="seconds"
            )
        return {
            "last_seen": seen,
            "age_s": None if age is None else round(age, 1),
            "gap_s": None if self._last_gap is None else round(self._last_gap, 1),
            "source": self._source,
            "mac": self._mac,
            "iface": self._iface,
            "packets": self._hits,
            "target_ip": self._get_target_ip(),
            "running": self.running,
            "error": self._error,
        }

    # ------------------------------------------------------------ 线程主体
    def _run(self) -> None:
        try:
            sock = socket.socket(
                socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ARP)
            )
        except PermissionError:
            self._error = "缺少 NET_RAW 权限，局域网探活不可用"
            _LOGGER.warning("门锁局域网探活不可用：%s", self._error)
            self._write_diag()
            return
        except OSError as err:
            self._error = f"原始套接字创建失败：{err}"
            _LOGGER.warning("门锁局域网探活不可用：%s", self._error)
            self._write_diag()
            return

        # 不 bind 到具体网卡：只收 ARP，量极小，且无需猜测接口名
        sock.settimeout(1.0)
        _LOGGER.info("门锁局域网探活已启动（监听 ARP，等待门锁的周期探测帧）")
        try:
            while not self._stop.is_set():
                try:
                    frame, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError as err:
                    self._error = f"读取失败：{err}"
                    break
                try:
                    self._handle(frame, addr)
                except Exception:  # noqa: BLE001 - 单帧解析失败不能终止监听
                    continue
        finally:
            sock.close()
            self._write_diag()

    def _handle(self, frame: bytes, addr: tuple) -> None:
        if len(frame) < _ARP_FRAME_MIN:
            return
        # addr = (ifname, proto, pkttype, hatype, hwaddr)
        if len(addr) > 2 and addr[2] == PACKET_OUTGOING:
            return
        target = self._get_target_ip()
        if not target:
            return

        oper = int.from_bytes(frame[20:22], "big")
        sha = frame[22:28]
        spa = socket.inet_ntoa(frame[28:32])
        tpa = socket.inet_ntoa(frame[38:42])

        # 只认「门锁自己发出的」帧：源 IP 必须是它。
        # 别人 who-has 门锁（spa 是别人）不算它在线 —— 那正是 AP/主机在问它。
        if spa != target:
            return

        now = time.monotonic()
        with self._lock:
            if self._last_seen_mono is not None:
                self._last_gap = now - self._last_seen_mono
            self._last_seen_mono = now
            self._last_seen_wall = time.time()
            self._hits += 1
            self._mac = ":".join(f"{b:02x}" for b in sha)
            self._iface = addr[0] if addr else None
            if spa == tpa:
                self._source = "arp-probe"
            elif oper == 2:
                self._source = "arp-reply"
            else:
                self._source = "arp-request"
            self._error = None

    def _write_diag(self) -> None:
        """把当前状态落一份到 /config，方便远程排查（只在启停/出错时写）。"""
        if not self._diag_path:
            return
        try:
            self._diag_path.write_text(
                "\n".join(
                    [
                        f"time={datetime.now().isoformat(timespec='seconds')}",
                        f"serial={self._serial}",
                        f"running={self.running}",
                        f"hits={self._hits}",
                        f"target_ip={self._get_target_ip()}",
                        f"mac={self._mac}",
                        f"iface={self._iface}",
                        f"source={self._source}",
                        f"age_s={self.age_s}",
                        f"error={self._error}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 - 诊断文件写不进去不影响主功能
            pass
