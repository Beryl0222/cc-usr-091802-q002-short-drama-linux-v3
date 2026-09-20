"""镜头拍摄与来源固化。

镜头只能在"市场锁有效 + 该市场已下发剧本版本 + 已确认档期 + 素材未越出
锁定边界"的条件下拍摄。拍摄时把所依据的剧本条目、批准人、授权条款、
权利窗口、档期与素材边界整体快照进镜头——此后剧本再出新版本或改稿，
已拍镜头的来源不会被改写或丢失。
"""

from .errors import ConflictError, StateError, ValidationError


class ShootService:
    def __init__(self, store, events, licensing, scripts):
        self.store = store
        self.events = events
        self.licensing = licensing
        self.scripts = scripts

    def shoot(self, shot_id, project_id, market, booking_id, item_id,
              material_id, actor):
        if not material_id:
            raise ValidationError("镜头必须登记所用素材标识")
        with self.store.tx():
            if self.store.get("shots", shot_id) is not None:
                raise ConflictError(f"镜头已存在: {shot_id}")

            project_, lock = self.licensing.get_lock(project_id, market)
            # 权利窗口：撤权或窗口过期后不能再拍
            self.licensing.assert_lock_active(project_id, market)

            booking = self.store.require("bookings", booking_id, "档期")
            if booking["project_id"] != project_id or booking["market"] != market:
                raise ConflictError("档期不属于该项目/市场，不能据此拍摄")
            if booking["status"] != "confirmed":
                raise StateError("档期未确认，不能拍摄")

            version = self.scripts.assert_released(project_id, market)
            item = next((i for i in version["content"]
                         if i["item_id"] == item_id), None)
            if item is None:
                raise ConflictError(
                    f"已下发版本 {version['version_id']} 中不存在剧本条目 "
                    f"{item_id}",
                    details={"version_id": version["version_id"]},
                )
            if material_id not in lock["material_boundary"]:
                raise ConflictError(
                    f"素材 {material_id} 超出市场 {market} 锁定的素材边界",
                    details={"allowed": lock["material_boundary"]},
                )

            # 固化来源快照：授权条款 + 批准人 + 版本内容 + 档期
            grants = [self.store.require("grants", gid, "授权")
                      for gid in lock["basis_grant_ids"]]
            snapshot = {
                "project_id": project_id,
                "market": market,
                "locked_languages": lock["languages"],
                "rights_window": [lock["term_start"], lock["term_end"]],
                "material_boundary": lock["material_boundary"],
                "basis_grant_ids": lock["basis_grant_ids"],
                "grant_terms_at_shoot": [
                    {"grant_id": g["grant_id"], "ip_id": g["ip_id"],
                     "market": g["market"], "languages": g["languages"],
                     "window": [g["start_at"], g["end_at"]],
                     "material_scope": g["material_scope"],
                     "granted_by": g["granted_by"]}
                    for g in grants
                ],
                "script_version_id": version["version_id"],
                "script_sequence": version["sequence"],
                "script_item": self.store.snapshot(item),
                "script_approvers": {
                    role: record["by"]
                    for role, record in version["approvals"].items()
                },
                "released_to_market_by": version["releases"][market]["by"],
                "released_to_market_at": version["releases"][market]["released_at"],
                "booking_id": booking_id,
                "booking_window": [booking["start_at"], booking["end_at"]],
                "booking_timezone": booking["timezone"],
                "material_id": material_id,
            }
            from .models import shot as make_shot
            entity = make_shot(shot_id, project_id, market, booking_id,
                               version["version_id"], item_id, material_id,
                               snapshot)
            self.store.save("shots", shot_id, entity)
            self.events.append(
                "shot.captured", actor,
                {"project_id": project_id, "market": market, "shot_id": shot_id,
                 "version_id": version["version_id"],
                 "booking_id": booking_id},
                {"item_id": item_id, "material_id": material_id},
            )
            return self.store.snapshot(entity)

    def shots_for(self, project_id=None, market=None):
        def predicate(shot):
            return (project_id is None or shot["project_id"] == project_id) and (
                market is None or shot["market"] == market)
        return self.store.list("shots", predicate)
