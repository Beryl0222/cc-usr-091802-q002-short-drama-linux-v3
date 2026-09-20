"""剧本版本链、本地化意见与剧情改动、三方审批汇合。

关键规则：

- 版本内容一旦创建即为不可变快照；并发改稿通过"父版本必须是当前 head"
  做乐观并发控制，第二个提交者会收到带最新 head 的冲突，需改投新版本。
- 文化本地化意见与剧情改动 1:1 逐条关联（同一剧本条目，互相引用）。
- 版权、合规、制片三方全部批准，版本才自动汇合为 approved；任一方可在
  汇合前驳回。审批中的版本不可直接改稿，须先撤回或被驳回。
- approved 的版本按市场"下发拍摄"；下发要求该市场权利锁仍有效。
"""

import copy

from . import clock
from .errors import ConflictError, StateError, ValidationError
from .models import (
    ANN_LOCALIZATION,
    ANN_PLOT_CHANGE,
    APPROVAL_ROLES,
    LOCK_ACTIVE,
    VER_APPROVED,
    VER_DRAFT,
    VER_REJECTED,
    VER_RELEASED,
    VER_SUBMITTED,
    annotation,
    script_item,
    script_version,
)


def _normalize_items(raw_items):
    items = []
    seen = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValidationError("剧本条目必须是对象")
        item_id = raw.get("item_id") or f"item-{index + 1}"
        if item_id in seen:
            raise ValidationError(f"剧本条目标识重复: {item_id}")
        seen.add(item_id)
        kind = raw.get("kind") or "scene"
        summary = raw.get("summary")
        if not summary:
            raise ValidationError(f"剧本条目 {item_id} 缺少摘要")
        items.append(script_item(
            item_id, index, kind, summary,
            source_ref=raw.get("source_ref", ""),
        ))
        items[-1]["material_refs"] = sorted(set(raw.get("material_refs") or []))
    if not items:
        raise ValidationError("剧本内容不能为空")
    return items


class ScriptService:
    def __init__(self, store, events, licensing):
        self.store = store
        self.events = events
        self.licensing = licensing
        # project_id -> 注解池（版本创建时快照进版本）
        self._annotations = {}

    # ---- 注解池 --------------------------------------------------------

    def annotations(self, project_id):
        return self._annotations.setdefault(project_id, [])

    def _find_note(self, project_id, note_id):
        for ann in self.annotations(project_id):
            if ann["annotation_id"] == note_id:
                return ann
        return None

    def add_localization_note(self, project_id, note_id, target_item_id,
                              advisor, content, basis_ref, actor=None):
        """本地顾问就某条剧情提出文化本地化意见。"""
        with self.store.tx():
            head = self._head(project_id)
            if head is None:
                raise StateError("项目还没有剧本版本，不能先提本地化意见")
            item_ids = {i["item_id"] for i in head["content"]}
            if target_item_id not in item_ids:
                raise ConflictError(
                    f"当前剧本 head 中不存在条目 {target_item_id}，意见无法逐条关联",
                    details={"head_version_id": head["version_id"],
                             "known_items": sorted(item_ids)},
                )
            pool = self.annotations(project_id)
            if any(a["annotation_id"] == note_id for a in pool):
                raise ConflictError(f"意见已存在: {note_id}")
            ann = annotation(note_id, ANN_LOCALIZATION, target_item_id,
                             "local_advisor", content, basis_ref)
            ann["advisor"] = advisor
            pool.append(ann)
            self.events.append(
                "script.note_added", actor or advisor,
                {"project_id": project_id, "target_item_id": target_item_id},
                {"note_id": note_id, "basis_ref": basis_ref,
                 "content": content},
            )
            return copy.deepcopy(ann)

    def add_plot_change(self, project_id, change_id, note_id, writer,
                        content, actor=None):
        """编剧根据本地化意见提出剧情改动，与意见 1:1 挂钩。"""
        with self.store.tx():
            note = self._find_note(project_id, note_id)
            if note is None:
                raise ConflictError(f"本地化意见不存在: {note_id}")
            if note["kind"] != ANN_LOCALIZATION:
                raise ValidationError("剧情改动必须关联到本地化意见")
            if note.get("related_id"):
                raise ConflictError(
                    f"意见 {note_id} 已有关联的剧情改动 {note['related_id']}",
                )
            pool = self.annotations(project_id)
            if any(a["annotation_id"] == change_id for a in pool):
                raise ConflictError(f"剧情改动已存在: {change_id}")
            change = annotation(change_id, ANN_PLOT_CHANGE,
                                note["target_item_id"], "writer", content,
                                basis_ref=note_id, related_id=note_id)
            change["writer"] = writer
            change["advisor_note_id"] = note_id
            pool.append(change)
            note["related_id"] = change_id
            self.events.append(
                "script.change_added", actor or writer,
                {"project_id": project_id,
                 "target_item_id": note["target_item_id"]},
                {"change_id": change_id, "note_id": note_id,
                 "content": content},
            )
            return copy.deepcopy(change)

    # ---- 版本链 --------------------------------------------------------

    def _head(self, project_id):
        project_ = self.store.require("projects", project_id, "项目")
        head_id = project_.get("head_version_id")
        if head_id is None:
            return None
        return self.store.require("versions", head_id, "剧本 head 版本")

    def create_version(self, project_id, version_id, content, created_by,
                       parent_id=None, note=""):
        items = _normalize_items(content)
        with self.store.tx():
            project_ = self.store.require("projects", project_id, "项目")
            if self.store.get("versions", version_id) is not None:
                raise ConflictError(f"剧本版本已存在: {version_id}")
            head_id = project_.get("head_version_id")
            if head_id is None:
                if parent_id is not None:
                    raise ConflictError("首个版本不能有父版本")
                sequence = 1
            else:
                if parent_id != head_id:
                    raise ConflictError(
                        "并发改稿：父版本不是当前 head，请基于最新版本重新修订",
                        details={"expected_parent": head_id,
                                 "submitted_parent": parent_id},
                    )
                head = self.store.require("versions", head_id, "剧本版本")
                if head["state"] == VER_SUBMITTED:
                    raise StateError(
                        "当前版本正在审批汇合中，不能直接改稿；请先撤回或等待驳回",
                        details={"submitted_version": head_id},
                    )
                sequence = head["sequence"] + 1

            # 只快照仍能与本版条目逐条对上的意见/改动，并校验两两挂钩完整
            item_ids = {i["item_id"] for i in items}
            pool = self.annotations(project_id)
            snapshot = []
            by_id = {}
            for ann in pool:
                if ann["target_item_id"] in item_ids:
                    snap = copy.deepcopy(ann)
                    snapshot.append(snap)
                    by_id[snap["annotation_id"]] = snap
            for ann in snapshot:
                related = ann.get("related_id")
                if related and related not in by_id:
                    raise ConflictError(
                        f"注解 {ann['annotation_id']} 的关联对象 {related} "
                        "不在本版剧本中，无法保持逐条关联",
                    )
            notes = {a["annotation_id"] for a in snapshot
                     if a["kind"] == ANN_LOCALIZATION}
            for ann in snapshot:
                if ann["kind"] == ANN_PLOT_CHANGE:
                    basis = ann.get("advisor_note_id") or ann.get("basis_ref")
                    if basis not in notes:
                        raise ConflictError(
                            f"剧情改动 {ann['annotation_id']} 缺少逐条关联的"
                            "文化本地化意见",
                        )

            version = script_version(
                version_id, project_id,
                parent_id=head_id, sequence=sequence,
                created_by=created_by, note=note,
                content=items, annotations=snapshot,
            )
            self.store.save("versions", version_id, version)
            project_["version_ids"].append(version_id)
            project_["head_version_id"] = version_id
            self.store.save("projects", project_id, project_)
            self.events.append(
                "script.version_created", created_by,
                {"project_id": project_id, "version_id": version_id},
                {"parent_id": head_id, "sequence": sequence,
                 "items": len(items), "annotations": len(snapshot)},
            )
            return self.store.snapshot(version)

    def get_version(self, version_id):
        return self.store.snapshot(
            self.store.require("versions", version_id, "剧本版本"))

    def _require_state(self, version, *states):
        if version["state"] not in states:
            raise StateError(
                f"版本 {version['version_id']} 当前状态 {version['state']}，"
                f"要求 {sorted(states)}",
                details={"version_id": version["version_id"],
                         "state": version["state"]},
            )

    # ---- 提交与审批 ----------------------------------------------------

    def submit(self, version_id, actor):
        with self.store.tx():
            version = self.store.require("versions", version_id, "剧本版本")
            self._require_state(version, VER_DRAFT, VER_REJECTED)
            project_ = self.store.require("projects", version["project_id"], "项目")
            active = [m for m, l in project_["markets"].items()
                      if l["status"] == LOCK_ACTIVE]
            if not active:
                raise StateError("项目已无任何有效市场锁，不能提交审批")
            version["state"] = VER_SUBMITTED
            version["approvals"] = {}
            version["reject"] = None
            self.store.save("versions", version_id, version)
            self.events.append(
                "script.submitted", actor,
                {"project_id": version["project_id"], "version_id": version_id},
                {"active_markets": sorted(active)},
            )
            return self.store.snapshot(version)

    def approve(self, version_id, role, actor):
        if role not in APPROVAL_ROLES:
            raise ValidationError(
                f"审批角色必须是 {APPROVAL_ROLES} 之一", details={"role": role})
        with self.store.tx():
            version = self.store.require("versions", version_id, "剧本版本")
            self._require_state(version, VER_SUBMITTED)
            prior = version["approvals"].get(role)
            if prior is not None:
                raise ConflictError(
                    f"{role} 已在 {prior['at']} 由 {prior['by']} 批准",
                )
            version["approvals"][role] = {"by": actor, "at": clock.iso(clock.now())}
            converged = all(r in version["approvals"] for r in APPROVAL_ROLES)
            if converged:
                version["state"] = VER_APPROVED
            self.store.save("versions", version_id, version)
            self.events.append(
                "script.approved", actor,
                {"project_id": version["project_id"], "version_id": version_id},
                {"role": role, "converged": converged},
            )
            return self.store.snapshot(version)

    def reject(self, version_id, role, actor, reason):
        if role not in APPROVAL_ROLES:
            raise ValidationError("驳回角色不合法", details={"role": role})
        if not reason:
            raise ValidationError("驳回原因不能为空")
        with self.store.tx():
            version = self.store.require("versions", version_id, "剧本版本")
            self._require_state(version, VER_SUBMITTED)
            version["state"] = VER_REJECTED
            version["reject"] = {"by": actor, "role": role,
                                 "at": clock.iso(clock.now()), "reason": reason}
            self.store.save("versions", version_id, version)
            self.events.append(
                "script.rejected", actor,
                {"project_id": version["project_id"], "version_id": version_id},
                {"role": role, "reason": reason},
            )
            return self.store.snapshot(version)

    def withdraw(self, version_id, actor):
        """提交者在三方汇合前撤回，回到草稿。"""
        with self.store.tx():
            version = self.store.require("versions", version_id, "剧本版本")
            self._require_state(version, VER_SUBMITTED)
            if version["created_by"] != actor:
                raise ConflictError("只有版本提交人可以撤回")
            version["state"] = VER_DRAFT
            version["approvals"] = {}
            self.store.save("versions", version_id, version)
            self.events.append(
                "script.withdrawn", actor,
                {"project_id": version["project_id"], "version_id": version_id},
                {},
            )
            return self.store.snapshot(version)

    def release_for_shooting(self, version_id, market, actor):
        """把已汇合批准的版本按市场下发拍摄。"""
        with self.store.tx():
            version = self.store.require("versions", version_id, "剧本版本")
            self._require_state(version, VER_APPROVED, VER_RELEASED)
            self.licensing.assert_lock_active(version["project_id"], market)
            if market in version["releases"]:
                raise ConflictError(f"版本已下发至市场 {market}")
            version["releases"][market] = {"by": actor,
                                           "released_at": clock.iso(clock.now())}
            version["state"] = VER_RELEASED
            self.store.save("versions", version_id, version)
            self.events.append(
                "script.released_to_market", actor,
                {"project_id": version["project_id"], "version_id": version_id,
                 "market": market},
                {},
            )
            return self.store.snapshot(version)

    def assert_released(self, project_id, market):
        """取该市场已下发拍摄的剧本版本（拍摄前校验）。"""
        project_ = self.store.require("projects", project_id, "项目")
        head_id = project_.get("head_version_id")
        if head_id is None:
            raise StateError("项目还没有剧本版本")
        # 下发记录只可能存在于已成为过 head 的版本；沿链找最近下发到该市场的
        current_id = head_id
        while current_id:
            version = self.store.require("versions", current_id, "剧本版本")
            if market in version["releases"]:
                return version
            current_id = version["parent_id"]
        raise StateError(
            f"市场 {market} 还没有汇合批准并下发拍摄的剧本版本",
            details={"market": market},
        )
