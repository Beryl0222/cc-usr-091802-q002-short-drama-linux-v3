"""场景、演员、团队档期排期。

三类资源统一处理，各自带 IANA 时区。预订在同一把仓储锁内完成
"窗口校验—重叠判定—写入"，并发抢档时只有一方成功，另一方拿到包含
冲突方与本地时间的明确原因。
"""

from . import clock
from .errors import ConflictError, ValidationError
from .models import (
    RES_ACTOR,
    RES_SET,
    RES_TEAM,
    booking,
    resource,
)

RESOURCE_TYPES = (RES_SET, RES_ACTOR, RES_TEAM)


def _local_label(resource_, at):
    """把 UTC 时间标注成资源所在时区的本地时间，冲突原因直接可读。"""
    local = clock.utc(at).astimezone(clock.zone(resource_["timezone"]))
    return f"{local.isoformat(timespec='minutes')} ({resource_['timezone']})"


class ScheduleService:
    def __init__(self, store, events, licensing):
        self.store = store
        self.events = events
        self.licensing = licensing

    def register_resource(self, resource_id, type_, name, timezone,
                          team_id=None, market=None, actor="system"):
        if type_ not in RESOURCE_TYPES:
            raise ValidationError(f"资源类型必须是 {RESOURCE_TYPES} 之一",
                                  details={"type": type_})
        try:
            clock.zone(timezone)
        except Exception:
            raise ValidationError(f"非法 IANA 时区: {timezone}")
        with self.store.tx():
            if self.store.get("resources", resource_id) is not None:
                raise ConflictError(f"资源已存在: {resource_id}")
            entity = resource(resource_id, type_, name, timezone, team_id, market)
            self.store.save("resources", resource_id, entity)
            self.events.append(
                "schedule.resource_registered", actor,
                {"resource_id": resource_id},
                {"type": type_, "name": name, "timezone": timezone,
                 "team_id": team_id, "market": market},
            )
            return self.store.snapshot(entity)

    def _conflicts_on(self, resource_id, start, end):
        return self.store.list(
            "bookings",
            lambda b: b["resource_id"] == resource_id
            and clock.overlap(b["start_at"], b["end_at"], start, end),
        )

    def book(self, booking_id, project_id, market, resource_id, start_at,
             end_at, purpose, actor):
        start, end = clock.utc(start_at), clock.utc(end_at)
        if not start < end:
            raise ValidationError("档期开始必须早于结束")
        if not purpose:
            raise ValidationError("排期用途不能为空")
        with self.store.tx():
            res = self.store.require("resources", resource_id, "排期资源")
            # 权利锁必须仍有效（含窗口检查）
            self.licensing.assert_lock_active(project_id, market, at=start)
            clashes = self._conflicts_on(resource_id, start, end)
            if clashes:
                reasons = []
                for c in clashes:
                    reasons.append({
                        "reason": "resource_busy",
                        "resource_type": res["type"],
                        "resource_name": res["name"],
                        "conflicting_booking_id": c["booking_id"],
                        "conflicting_project_id": c["project_id"],
                        "conflicting_market": c["market"],
                        "busy_window_utc": [c["start_at"], c["end_at"]],
                        "busy_window_local": [
                            _local_label(res, c["start_at"]),
                            _local_label(res, c["end_at"]),
                        ],
                    })
                raise ConflictError(
                    f"{res['name']} 档期冲突：{len(clashes)} 个已确认排期重叠",
                    details={
                        "requested_utc": [clock.iso(start), clock.iso(end)],
                        "requested_local": [_local_label(res, start),
                                            _local_label(res, end)],
                        "conflicts": reasons,
                    },
                )
            entity = booking(booking_id, project_id, market, resource_id,
                             start, end, purpose, res["timezone"])
            self.store.save("bookings", booking_id, entity)
            self.events.append(
                "schedule.booked", actor,
                {"project_id": project_id, "market": market,
                 "resource_id": resource_id, "booking_id": booking_id},
                {"start_at": clock.iso(start), "end_at": clock.iso(end),
                 "purpose": purpose, "resource_timezone": res["timezone"]},
            )
            return self.store.snapshot(entity)

    def calendar(self, resource_id):
        """返回资源的档期日历（UTC 与资源本地时间并列）。"""
        with self.store.tx():
            res = self.store.require("resources", resource_id, "排期资源")
            entries = []
            for b in self.store.list(
                    "bookings", lambda b: b["resource_id"] == resource_id):
                entries.append({
                    "booking_id": b["booking_id"],
                    "project_id": b["project_id"],
                    "market": b["market"],
                    "purpose": b["purpose"],
                    "window_utc": [b["start_at"], b["end_at"]],
                    "window_local": [_local_label(res, b["start_at"]),
                                     _local_label(res, b["end_at"])],
                    "status": b["status"],
                })
            entries.sort(key=lambda e: e["window_utc"][0])
            return {"resource": self.store.snapshot(res), "bookings": entries}

    def check_availability(self, resource_id, start_at, end_at):
        """不落单的冲突预检，返回可用与否和原因。"""
        start, end = clock.utc(start_at), clock.utc(end_at)
        with self.store.tx():
            res = self.store.require("resources", resource_id, "排期资源")
            clashes = self._conflicts_on(resource_id, start, end)
            return {
                "available": not clashes,
                "resource_id": resource_id,
                "requested_utc": [clock.iso(start), clock.iso(end)],
                "requested_local": [_local_label(res, start),
                                    _local_label(res, end)],
                "conflicts": [
                    {"booking_id": c["booking_id"], "project_id": c["project_id"],
                     "market": c["market"]} for c in clashes
                ],
            }
