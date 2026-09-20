"""剧本版本域：哈希版本、乐观并发、本地化意见逐条关联、三方审批汇合门禁。"""

import hashlib
import sqlite3

from .audit import log_event
from .clock import iso, new_id, now_utc
from .db import dumps, loads
from .errors import ConflictState, IntegrityGuard, NotFound, ValidationError, VersionConflict
from .backend_ip import GATE_ROLES, ROLE_COMPLIANCE, ROLE_PRODUCTION, ROLE_RIGHTS


class ScriptMixin:
    # ---------------- 版本 ----------------
    def create_draft(self, project_id, scenes, created_by, *,
                     based_on_version_id=None, change_summary="",
                     target_markets=None, force=False):
        """新建剧本草稿。

        based_on_version_id 必须是当前最新版本（乐观并发）：两个编剧同时基于 v3
        改稿时，后提交者收到 version_conflict，可先合并再以 head 重提。
        """
        scenes = self._normalize_scenes(scenes)
        content_hash = self._hash_scenes(scenes)
        with self.tx() as conn:
            project = self._project(conn, project_id)
            head = conn.execute(
                "SELECT version_id, seq, locked_at FROM script_versions "
                "WHERE project_id=? ORDER BY seq DESC LIMIT 1", (project_id,)).fetchone()
            if based_on_version_id:
                base = conn.execute(
                    "SELECT * FROM script_versions WHERE version_id=?",
                    (based_on_version_id,)).fetchone()
                if base is None:
                    raise NotFound(f"基准剧本版本 {based_on_version_id} 不存在")
                if base["project_id"] != project_id:
                    raise ValidationError("基准版本不属于该项目")
            else:
                base = None
            if head and not force:
                expected = head["version_id"]
                if based_on_version_id != expected:
                    raise VersionConflict(
                        "剧本已有更新版本，当前改稿基于过期版本，请合并后重试",
                        {"submitted_base": based_on_version_id,
                         "current_head": expected, "current_seq": head["seq"]})
            seq = (head["seq"] + 1) if head else 1
            markets = self._draft_markets(conn, project, target_markets)
            version_id = new_id("scr")
            try:
                conn.execute(
                    """INSERT INTO script_versions (version_id, project_id, seq,
                         parent_version_id, based_on_version_id, content_hash,
                         scenes_json, change_summary, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (version_id, project_id, seq,
                     based_on_version_id, based_on_version_id, content_hash,
                     dumps({"scenes": scenes, "target_markets": markets}),
                     change_summary, created_by, iso(now_utc())))
            except sqlite3.IntegrityError as exc:
                raise IntegrityGuard(f"剧本版本写入冲突: {exc}")
            log_event(conn, "script.draft_created", created_by, project_id=project_id,
                      entity_type="script_version", entity_id=version_id,
                      payload={"seq": seq, "based_on": based_on_version_id,
                               "content_hash": content_hash,
                               "change_summary": change_summary})
        return self.get_version(version_id)

    def get_version(self, version_id):
        with self.conn() as conn:
            row = self._must(conn, "script_versions", "version_id", version_id, "剧本版本")
            return self._version_dict(conn, row)

    def list_versions(self, project_id):
        with self.conn() as conn:
            self._project(conn, project_id)
            rows = conn.execute(
                "SELECT * FROM script_versions WHERE project_id=? ORDER BY seq",
                (project_id,)).fetchall()
            return [self._version_dict(conn, r, light=True) for r in rows]

    # ---------------- 本地化意见与逐条改动 ----------------
    def add_note(self, version_id, author, author_role, category, body, *,
                 scene_ref=None, actor=None):
        """本地顾问等对剧本逐条提意见（文化/合规/剧情）。"""
        if category not in ("cultural", "compliance", "plot"):
            raise ValidationError("category 须为 cultural/compliance/plot")
        if not body or not body.strip():
            raise ValidationError("意见内容不能为空")
        with self.tx() as conn:
            v = self._editable_version(conn, version_id)
            self._scene_exists(v, scene_ref)
            note_id = new_id("note")
            conn.execute(
                """INSERT INTO script_notes (note_id, project_id, version_id, author,
                     author_role, category, body, created_at, scene_ref)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (note_id, v["project_id"], version_id, author, author_role,
                 category, body, iso(now_utc()), scene_ref))
            log_event(conn, "script.note_added", actor or author,
                      project_id=v["project_id"], entity_type="script_note",
                      entity_id=note_id,
                      payload={"version_id": version_id, "category": category,
                               "scene_ref": scene_ref, "author_role": author_role})
        return {"note_id": note_id, "version_id": version_id, "author": author,
                "author_role": author_role, "category": category, "body": body,
                "scene_ref": scene_ref, "resolved_in_version_id": None}

    def add_change(self, version_id, scene_ref, change_type, description, *,
                   source_note_id=None, actor="writer"):
        """记录剧情/本地化改动；source_note_id 将改动逐条关联回具体意见。"""
        if change_type not in ("plot", "localization", "compliance_fix"):
            raise ValidationError("change_type 须为 plot/localization/compliance_fix")
        if not description or not description.strip():
            raise ValidationError("改动说明不能为空")
        with self.tx() as conn:
            v = self._editable_version(conn, version_id)
            self._scene_exists(v, scene_ref)
            if source_note_id:
                note = conn.execute(
                    "SELECT * FROM script_notes WHERE note_id=?",
                    (source_note_id,)).fetchone()
                if note is None:
                    raise NotFound(f"关联意见 {source_note_id} 不存在")
                if note["project_id"] != v["project_id"]:
                    raise ValidationError("关联意见不属于该项目")
                if note["resolved_in_version_id"]:
                    raise ConflictState(
                        "该意见已在另一版本中处理",
                        {"resolved_in_version_id": note["resolved_in_version_id"]})
            change_id = new_id("chg")
            conn.execute(
                """INSERT INTO script_changes (change_id, project_id, version_id,
                     scene_ref, change_type, description, source_note_id, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (change_id, v["project_id"], version_id, scene_ref, change_type,
                 description, source_note_id, iso(now_utc())))
            if source_note_id:
                conn.execute(
                    "UPDATE script_notes SET resolved_in_version_id=? WHERE note_id=?",
                    (version_id, source_note_id))
            log_event(conn, "script.change_added", actor, project_id=v["project_id"],
                      entity_type="script_change", entity_id=change_id,
                      payload={"version_id": version_id, "scene_ref": scene_ref,
                               "change_type": change_type,
                               "source_note_id": source_note_id})
        return {"change_id": change_id, "version_id": version_id,
                "scene_ref": scene_ref, "change_type": change_type,
                "description": description, "source_note_id": source_note_id}

    # ---------------- 三方审批汇合 ----------------
    def submit_approval(self, version_id, role, approver, decision, *,
                        comment="", actor=None):
        if role not in GATE_ROLES:
            raise ValidationError(f"审批角色须为 {('/'.join(GATE_ROLES))}")
        if decision not in ("approved", "rejected"):
            raise ValidationError("decision 须为 approved/rejected")
        with self.tx() as conn:
            v = self._must(conn, "script_versions", "version_id", version_id, "剧本版本")
            if v["locked_at"]:
                raise ConflictState("该版本已汇合审批并锁定，决定不可更改",
                                    {"locked_at": v["locked_at"]})
            blockers = []
            if decision == "approved":
                blockers = self._approval_blockers(conn, v, role)
                if blockers:
                    raise ConflictState(
                        f"{role} 批准前仍有未满足条件", {"blockers": blockers})
            conn.execute(
                """INSERT INTO approvals (version_id, role, approver, decision,
                     comment, decided_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(version_id, role) DO UPDATE SET
                     approver=excluded.approver, decision=excluded.decision,
                     comment=excluded.comment, decided_at=excluded.decided_at""",
                (version_id, role, approver, decision, comment, iso(now_utc())))
            log_event(conn, "script.approval_submitted", actor or approver,
                      project_id=v["project_id"], entity_type="script_version",
                      entity_id=version_id,
                      payload={"role": role, "decision": decision,
                               "approver": approver, "blockers": blockers})
            locked = self._try_lock(conn, v)
            gate = self._gate(conn, v)
        out = self.get_version(version_id)
        out["just_locked"] = locked
        return out

    def gate_status(self, version_id):
        with self.conn() as conn:
            v = self._must(conn, "script_versions", "version_id", version_id, "剧本版本")
            return self._gate(conn, v)

    # ---------------- 内部规则 ----------------
    def _try_lock(self, conn, v):
        """三方批准汇合且无拒绝：固化版本并快照授权条款。"""
        gate = self._gate(conn, v)
        if gate["locked"]:
            return False
        if not gate["can_lock"]:
            return False
        snapshot, _ = self._license_snapshot(conn, v)
        conn.execute(
            "UPDATE script_versions SET locked_at=?, license_snapshot_json=? "
            "WHERE version_id=?",
            (iso(now_utc()), dumps(snapshot), v["version_id"]))
        log_event(conn, "script.locked", "system", project_id=v["project_id"],
                  entity_type="script_version", entity_id=v["version_id"],
                  payload={"seq": v["seq"], "license_snapshot": snapshot})
        return True

    def _approval_blockers(self, conn, v, role):
        """各角色批准时系统强制校验的前置条件。

        版权门禁关注素材是否越过项目边界；具体市场/语言/期限的授权窗口
        在排发行时由 check_grant 强制，避免剧本锁定被某市场续约节奏卡住。
        """
        blockers = []
        body = loads(v["scenes_json"])
        scenes = body["scenes"]
        if role == ROLE_RIGHTS:
            project = self._project(conn, v["project_id"])
            boundary = loads(project["allowed_source_materials_json"])
            for sc in scenes:
                for ref in sc.get("material_refs", []):
                    if ref not in boundary:
                        blockers.append({
                            "rule": "material_outside_boundary",
                            "scene_ref": sc["scene_ref"], "material_ref": ref})
        elif role == ROLE_COMPLIANCE:
            rows = conn.execute(
                "SELECT note_id, category, scene_ref, body FROM script_notes "
                "WHERE version_id=? AND category='compliance' "
                "AND resolved_in_version_id IS NULL", (v["version_id"],)).fetchall()
            for r in rows:
                blockers.append({"rule": "unresolved_compliance_note",
                                 "note_id": r["note_id"], "scene_ref": r["scene_ref"]})
        elif role == ROLE_PRODUCTION:
            # 制片批准要求所有文化本地化意见都已被某条改动逐条处理
            rows = conn.execute(
                """SELECT n.note_id, n.scene_ref FROM script_notes n
                   WHERE n.version_id=? AND n.category='cultural'
                     AND n.resolved_in_version_id IS NULL""",
                (v["version_id"],)).fetchall()
            for r in rows:
                blockers.append({"rule": "unresolved_cultural_note",
                                 "note_id": r["note_id"], "scene_ref": r["scene_ref"]})
        return blockers

    def _gate(self, conn, v):
        rows = conn.execute(
            "SELECT role, decision, approver, decided_at FROM approvals "
            "WHERE version_id=?", (v["version_id"],)).fetchall()
        decisions = {r["role"]: dict(r) for r in rows}
        approved = all(decisions.get(role, {}).get("decision") == "approved"
                       for role in GATE_ROLES)
        rejected = [role for role in GATE_ROLES
                    if decisions.get(role, {}).get("decision") == "rejected"]
        pending = [role for role in GATE_ROLES if role not in decisions]
        return {
            "version_id": v["version_id"],
            "locked": bool(v["locked_at"]),
            "locked_at": v["locked_at"],
            "approvals": {role: {"decision": d["decision"], "approver": d["approver"],
                                  "decided_at": d["decided_at"]}
                          for role, d in decisions.items()},
            "pending_roles": pending,
            "rejected_roles": rejected,
            "can_lock": approved and not rejected,
        }

    def _license_snapshot(self, conn, v):
        body = loads(v["scenes_json"])
        markets = body["target_markets"]
        snap = []
        project = self._project(conn, v["project_id"])
        for market in markets:
            for lic in conn.execute(
                    "SELECT * FROM licenses WHERE project_id=? AND market=? "
                    "AND status='active' ORDER BY language",
                    (v["project_id"], market)).fetchall():
                snap.append({
                    "license_id": lic["license_id"], "market": market,
                    "language": lic["language"], "starts_at": lic["starts_at"],
                    "expires_at": lic["expires_at"], "version": lic["version"],
                    "restricted_materials": loads(lic["restricted_materials_json"], []),
                    "rightsholder": project["ip_id"],
                })
        return snap, markets

    def _approved_version(self, conn, version_id):
        """供排期/资产域：只有锁定版本才能下发拍摄与派生。"""
        v = conn.execute(
            "SELECT * FROM script_versions WHERE version_id=?",
            (version_id,)).fetchone()
        if v is None:
            raise NotFound(f"剧本版本 {version_id} 不存在")
        if not v["locked_at"]:
            gate = self._gate(conn, v)
            raise ConflictState(
                "剧本版本尚未完成版权/合规/制片三方汇合审批，不能下发拍摄",
                {"version_id": version_id, "pending_roles": gate["pending_roles"],
                 "rejected_roles": gate["rejected_roles"]})
        return v

    @staticmethod
    def _editable_version(conn, version_id):
        v = conn.execute(
            "SELECT * FROM script_versions WHERE version_id=?",
            (version_id,)).fetchone()
        if v is None:
            raise NotFound(f"剧本版本 {version_id} 不存在")
        if v["locked_at"]:
            raise ConflictState("版本已锁定，意见与改动只能记录在后续版本",
                                {"version_id": version_id})
        return v

    @staticmethod
    def _scene_exists(version_row, scene_ref):
        if scene_ref is None:
            return
        body = loads(version_row["scenes_json"])
        refs = {s["scene_ref"] for s in body["scenes"]}
        if scene_ref not in refs:
            raise ValidationError(f"场景 {scene_ref} 不在该版本中",
                                  {"known_scenes": sorted(refs)})

    @staticmethod
    def _draft_markets(conn, project_row, markets):
        project_id = project_row["project_id"]
        rows = [r["market"] for r in conn.execute(
            "SELECT market FROM project_markets WHERE project_id=? ORDER BY market",
            (project_id,))]
        if markets is None:
            return rows
        for m in markets:
            if m not in rows:
                raise ValidationError(f"目标市场 {m} 不在项目可改编市场清单内",
                                      {"allowed": rows})
        return markets

    @staticmethod
    def _normalize_scenes(scenes):
        if not isinstance(scenes, list) or not scenes:
            raise ValidationError("scenes 必须是非空数组")
        out, seen = [], set()
        for i, sc in enumerate(scenes):
            if not isinstance(sc, dict):
                raise ValidationError(f"第 {i + 1} 个场景必须是对象")
            ref = sc.get("scene_ref")
            if not isinstance(ref, str) or not ref.strip():
                raise ValidationError(f"第 {i + 1} 个场景缺少 scene_ref")
            if ref in seen:
                raise ValidationError(f"场景标识重复: {ref}")
            seen.add(ref)
            norm = {"scene_ref": ref,
                    "summary": str(sc.get("summary", "")),
                    "material_refs": list(sc.get("material_refs", []) or [])}
            out.append(norm)
        return out

    @staticmethod
    def _hash_scenes(scenes):
        canon = dumps(scenes)
        return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def _version_dict(self, conn, row, *, light=False):
        body = loads(row["scenes_json"])
        d = {
            "version_id": row["version_id"], "project_id": row["project_id"],
            "seq": row["seq"], "content_hash": row["content_hash"],
            "parent_version_id": row["parent_version_id"],
            "based_on_version_id": row["based_on_version_id"],
            "change_summary": row["change_summary"],
            "created_by": row["created_by"], "created_at": row["created_at"],
            "locked_at": row["locked_at"],
            "scenes": body["scenes"], "target_markets": body["target_markets"],
            "license_snapshot": loads(row["license_snapshot_json"], []),
        }
        if light:
            d["gate"] = {"locked": bool(row["locked_at"])}
            return d
        d["notes"] = [dict(r) for r in conn.execute(
            "SELECT note_id, author, author_role, category, body, scene_ref, "
            "resolved_in_version_id FROM script_notes WHERE version_id=? "
            "ORDER BY created_at", (row["version_id"],))]
        d["changes"] = [dict(r) for r in conn.execute(
            "SELECT change_id, scene_ref, change_type, description, source_note_id "
            "FROM script_changes WHERE version_id=? ORDER BY created_at",
            (row["version_id"],))]
        d["gate"] = self._gate(conn, row)
        return d
