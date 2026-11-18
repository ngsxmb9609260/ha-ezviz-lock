"""门锁健康度 + 成员凭证监控。

这一层刻意与 coordinator 的「开门记录」主链路解耦：
- 任何一次探测失败都只记录到 ``last_error``，**绝不**抛给 coordinator，
  否则一次电量接口抖动就会让整台锁的实体变成 unavailable。
- 电量 / 充电状态每 10 分钟一次，成员名单每 30 分钟一次，
  都基于 ``time.monotonic()`` 节流，不受轮询间隔（默认 20 秒）影响。

接口依据（萤石开放平台，均已实测返回 200）：
- 电量  ``GET /api/v3/keyValue/{sn}/{ch}/op?accessToken=..&key=multiPower``
        返回 ``multiPower`` 字符串，内含 Type 0=主电源 / 1=蓄电池 的 Remaining 百分比
- 成员  ``POST /api/lapp/keylock/user/list``
        返回每个成员的人脸 / 掌静脉 / 指纹 / 密码 / 门卡数量，可用于变更比对
- 在线与信号  ``POST /api/lapp/device/info`` 的 ``status`` / ``signal``
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import EzvizApiClient, EzvizApiError
from .const import (
    BATTERY_POLL_INTERVAL_S,
    CREDENTIAL_LABELS,
    DEVICE_POLL_INTERVAL_S,
    EVENT_MEMBERS_CHANGED,
    MEMBERS_POLL_INTERVAL_S,
    POWER_STATUS_NAMES,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

MAX_CHANGE_HISTORY = 50


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _signal_pct(raw: Any) -> int | None:
    """device/info 的 signal 形如 "68%"，个别固件直接给数字。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return max(0, min(100, int(raw)))
    text = str(raw).strip().rstrip("%")
    try:
        return max(0, min(100, int(float(text))))
    except ValueError:
        return None


@dataclass
class MemberInfo:
    """门锁上的一个成员（含各类凭证数量）。"""

    index: str
    name: str
    remark: str = ""
    face: int = 0
    palm: int = 0
    finger: int = 0
    password: int = 0
    card: int = 0
    double_auth: int = 0
    expired_enable: int = 0
    lock_type: int = 0
    begin_time: int = -1
    end_time: int = -1

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> MemberInfo:
        return cls(
            index=str(raw.get("lockUserIndex") or ""),
            name=str(raw.get("lockUserName") or raw.get("lockRemarkName") or "未命名"),
            remark=str(raw.get("lockRemarkName") or ""),
            face=_as_int(raw.get("faceCount")),
            palm=_as_int(raw.get("volarVeinCount")),
            finger=_as_int(raw.get("fingerCount")),
            password=_as_int(raw.get("pwdCount")),
            card=_as_int(raw.get("cardCount")),
            double_auth=_as_int(raw.get("doubleAuthStatus")),
            expired_enable=_as_int(raw.get("expiredEnable")),
            lock_type=_as_int(raw.get("lockType")),
            begin_time=_as_int(raw.get("beginTime"), -1),
            end_time=_as_int(raw.get("endTime"), -1),
        )

    @property
    def credential_total(self) -> int:
        return self.face + self.palm + self.finger + self.password + self.card

    def credential_summary(self) -> str:
        parts = [
            f"{label}{getattr(self, attr)}"
            for attr, label in CREDENTIAL_LABELS
            if getattr(self, attr)
        ]
        return "、".join(parts) or "无凭证"

    def diff(self, other: MemberInfo) -> list[str]:
        """self 是旧值，other 是新值；返回人类可读的差异项。"""
        out: list[str] = []
        if self.name != other.name:
            out.append(f"名称 {self.name}→{other.name}")
        for attr, label in CREDENTIAL_LABELS:
            old, new = getattr(self, attr), getattr(other, attr)
            if old != new:
                out.append(f"{label} {old}→{new}")
        if self.double_auth != other.double_auth:
            out.append(f"双重认证 {'开' if self.double_auth else '关'}→{'开' if other.double_auth else '关'}")
        if self.expired_enable != other.expired_enable:
            out.append(f"有效期限制 {'开' if self.expired_enable else '关'}→{'开' if other.expired_enable else '关'}")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "remark": self.remark,
            "face": self.face,
            "palm": self.palm,
            "finger": self.finger,
            "password": self.password,
            "card": self.card,
            "double_auth": self.double_auth,
            "expired_enable": self.expired_enable,
            "lock_type": self.lock_type,
            "begin_time": self.begin_time,
            "end_time": self.end_time,
            "credential_total": self.credential_total,
            "credential_summary": self.credential_summary(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MemberInfo:
        return cls(
            index=str(raw.get("index") or ""),
            name=str(raw.get("name") or "未命名"),
            remark=str(raw.get("remark") or ""),
            face=_as_int(raw.get("face")),
            palm=_as_int(raw.get("palm")),
            finger=_as_int(raw.get("finger")),
            password=_as_int(raw.get("password")),
            card=_as_int(raw.get("card")),
            double_auth=_as_int(raw.get("double_auth")),
            expired_enable=_as_int(raw.get("expired_enable")),
            lock_type=_as_int(raw.get("lock_type")),
            begin_time=_as_int(raw.get("begin_time"), -1),
            end_time=_as_int(raw.get("end_time"), -1),
        )


@dataclass
class LockHealth:
    """一次健康度探测的结果。"""

    online: bool = True
    signal_pct: int | None = None
    net_type: str | None = None
    ssid: str | None = None
    local_address: str | None = None
    battery_pct: int | None = None
    spare_battery_pct: int | None = None
    battery_items: list[dict[str, Any]] = field(default_factory=list)
    power_status: int | None = None
    power_status_name: str | None = None
    members: list[MemberInfo] = field(default_factory=list)
    member_changes: list[dict[str, Any]] = field(default_factory=list)
    last_member_change: dict[str, Any] | None = None
    last_update: str | None = None
    last_error: str | None = None


class HealthMonitor:
    """电量 / 信号 / 在线 / 成员凭证 的采集与变更比对。"""

    def __init__(self, hass: HomeAssistant, client: EzvizApiClient) -> None:
        self.hass = hass
        self._client = client
        self.serial = client.device_serial
        self.data = LockHealth()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{self.serial}.health"
        )
        self._loaded = False
        self._has_baseline = False
        self._baseline_saved = False
        self._last_battery = 0.0
        self._last_members = 0.0
        # v1.8.0：device/info 从每轮降到 10 分钟一次（省约 4260 次/天）
        self._last_device = 0.0

    # ---------------------------------------------------------------- 持久化
    async def async_load(self) -> None:
        raw = await self._store.async_load() or {}
        members = raw.get("members") or []
        self.data.members = [MemberInfo.from_dict(m) for m in members if isinstance(m, dict)]
        self.data.member_changes = list(raw.get("changes") or [])[:MAX_CHANGE_HISTORY]
        self.data.last_member_change = raw.get("last_change")
        # 只有存过名单才允许比对，否则首次启动会把「全部成员」误报成新增
        self._has_baseline = bool(members)
        self._baseline_saved = bool(members)
        self._loaded = True

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "members": [m.to_dict() for m in self.data.members],
                "changes": self.data.member_changes[:MAX_CHANGE_HISTORY],
                "last_change": self.data.last_member_change,
                "saved_at": dt_util.now().isoformat(timespec="seconds"),
            }
        )

    # ---------------------------------------------------------------- 主入口
    async def async_tick(self) -> None:
        """由 coordinator 每轮调用；内部按各自节流周期决定是否真正请求。"""
        if not self._loaded:
            await self.async_load()

        now = time.monotonic()
        # 首次必查；之后每 10 分钟查一次（电池门锁状态变化慢，无需每 20s 打）
        if now - self._last_device >= DEVICE_POLL_INTERVAL_S:
            self._last_device = now
            await self._async_refresh_device()

        if now - self._last_battery >= BATTERY_POLL_INTERVAL_S:
            self._last_battery = now
            await self._async_refresh_power()

        if now - self._last_members >= MEMBERS_POLL_INTERVAL_S:
            self._last_members = now
            await self._async_refresh_members()

        self.data.last_update = dt_util.now().isoformat(timespec="seconds")

    # ---------------------------------------------------------------- 设备信息
    async def _async_refresh_device(self) -> None:
        try:
            info = await self._client.async_get_device_info()
        except (EzvizApiError, TimeoutError) as err:
            # 云端不可达时不要武断判「门锁离线」：保持上一次的 online，只记错误
            self.data.last_error = f"设备信息获取失败：{err}"
            _LOGGER.debug("健康度：设备信息获取失败 %s", err)
            return
        except Exception as err:  # noqa: BLE001 - 探测层绝不外抛
            self.data.last_error = f"设备信息异常：{err}"
            _LOGGER.debug("健康度：设备信息异常 %s", err)
            return

        self.data.last_error = None
        self.data.online = str(info.get("status")) == "1"
        self.data.signal_pct = _signal_pct(info.get("signal"))
        self.data.net_type = info.get("netType")
        self.data.ssid = info.get("ssid")
        self.data.local_address = info.get("localAddress")

    # ---------------------------------------------------------------- 电量
    async def _async_refresh_power(self) -> None:
        try:
            items = await self._client.async_get_multi_power()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("健康度：电量接口失败 %s", err)
            items = []

        if not items:
            # 退一步：至少把充电状态拿到
            try:
                status = await self._client.async_get_power_status()
            except Exception:  # noqa: BLE001
                return
            self._apply_power_status(status)
            return

        self.data.battery_items = items
        main = next((i for i in items if _as_int(i.get("Type"), -1) == 0), None)
        if main is not None:
            remain = _as_int(main.get("Remaining"), -1)
            # 255 是「无此电池 / 未知」的哨兵值，不能当 255% 显示
            self.data.battery_pct = remain if 0 <= remain <= 100 else None
            self._apply_power_status(main.get("Status"))
        spare = next((i for i in items if _as_int(i.get("Type"), -1) == 1), None)
        if spare is not None:
            remain = _as_int(spare.get("Remaining"), -1)
            self.data.spare_battery_pct = remain if 0 <= remain <= 100 else None

    def _apply_power_status(self, raw: Any) -> None:
        if raw is None:
            return
        code = _as_int(raw, -1)
        self.data.power_status = code
        self.data.power_status_name = POWER_STATUS_NAMES.get(str(code))

    # ---------------------------------------------------------------- 成员
    async def _async_refresh_members(self) -> None:
        try:
            users = await self._client.async_get_lock_users()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("健康度：成员名单接口失败 %s", err)
            return

        new_members = [MemberInfo.from_raw(u) for u in users if isinstance(u, dict)]
        if not new_members and not self.data.members:
            return

        changes = self._diff_members(self.data.members, new_members) if self._has_baseline else []
        self.data.members = new_members
        self._has_baseline = True

        if not changes:
            # 第一次拿到名单时落一次基线：否则重启后「基线」丢失，
            # 停机期间发生的成员变更就永远发现不了。
            if not self._baseline_saved:
                self._baseline_saved = True
                await self._async_save()
                _LOGGER.debug("已建立门锁成员基线（%d 人）", len(new_members))
            return

        stamp = dt_util.now().isoformat(timespec="seconds")
        for item in changes:
            item["time"] = stamp
        self.data.member_changes = (changes + self.data.member_changes)[:MAX_CHANGE_HISTORY]
        self.data.last_member_change = {**changes[0], "count": len(changes), "time": stamp}
        self._baseline_saved = True
        await self._async_save()
        _LOGGER.warning("门锁成员/凭证发生变更：%s", changes)
        self.hass.bus.async_fire(
            EVENT_MEMBERS_CHANGED,
            {"device_serial": self.serial, "time": stamp, "count": len(changes), "changes": changes},
        )

    @staticmethod
    def _diff_members(old: list[MemberInfo], new: list[MemberInfo]) -> list[dict[str, Any]]:
        old_map = {m.index: m for m in old}
        new_map = {m.index: m for m in new}
        out: list[dict[str, Any]] = []
        for index, cur in new_map.items():
            prev = old_map.get(index)
            if prev is None:
                out.append(
                    {
                        "kind": "added",
                        "index": index,
                        "name": cur.name,
                        "detail": f"新增成员「{cur.name}」（{cur.credential_summary()}）",
                    }
                )
                continue
            diffs = prev.diff(cur)
            if diffs:
                out.append(
                    {
                        "kind": "changed",
                        "index": index,
                        "name": cur.name,
                        "detail": f"成员「{cur.name}」凭证变更：{'；'.join(diffs)}",
                    }
                )
        for index, prev in old_map.items():
            if index not in new_map:
                out.append(
                    {
                        "kind": "removed",
                        "index": index,
                        "name": prev.name,
                        "detail": f"成员「{prev.name}」被删除（原有 {prev.credential_summary()}）",
                    }
                )
        return out
