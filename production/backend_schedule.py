"""排期与拍摄域。

冲突检测靠 ``slot_minutes`` 分钟级占用表（(资源, 分钟) 主键）+ IMMEDIATE 事务：
两个团队并发抢同一场地/演员时，必有一方收到逐条原因。拍摄产生的镜头把
批准版本号与内容哈希一起钉死，之后剧本更新不影响已拍镜头的来源。
"""

from datetime import timedelta

from .audit import log_event
from .clock import in_timezone, iso, iso_local, new_id, now_utc, parse_dt
from .db import dumps, loads
from .errors import ConflictState, NotFound, SchedulingConflict, ValidationError

MAX_BOOKING_MINUTES = 24 * 60  # 单次排期上限，防止分钟表膨胀


class ScheduleMixin:
    def create_booking(self, project_id, scene_ref, version_id, *,
                       team_id, set_id, talent_ids, starts_at, ends_at,
                       actor="scheduler"):
        """请求档期；无冲突原子确认，有冲突返回 SchedulingConflict（含逐条原因）。"""
        talent_ids = self._normalize_talents(talent_ids)
        start, end = parse_dt(starts_at), parse_dt(ends_at)
        minutes = self._minute_range(start, end)
        with self.tx() as conn:
            self._project(conn, project_id)
            v = self._approved_version(conn, version_id)
            self._scene_in_version(v, scene_ref)
            team = self._must(conn, "teams", "team_id", team_id, "团队")
            setrow = self._must(conn, "sets", "set_id", set_id, "场景")
            self._team_on_project(conn, project_id, team_id)
            for tid in talent_ids:
                self._must(conn, "talent", "talent_id", tid, "演员")
            resources = [("set", set_id), ("team", team_id)] + \
                        [("talent", t) for t in talent_ids]
            reasons = self._detect_conflicts(conn, resources, start, end, minutes)
            if reasons:
                raise SchedulingConflict("档期冲突，无法排期", reasons)
            booking_id = new_id("bkg")
            conn.execute(
                """INSERT INTO schedule_bookings (booking_id, project_id, scene_ref,
                     version_id, status, created_at, confirmed_at)
                   VALUES(?,?,?,?,'confirmed',?,?)""",
                (booking_id, project_id, scene_ref, version_id,
                 iso(now_utc()), iso(now_utc())))
            for kind, rid in resources:
                slot_id = new_id("slt")
                conn.execute(
                    """INSERT INTO schedule_slots (slot_id, booking_id, project_id,
                         resource_kind, resource_id, start_minute, end_minute,
                         holds_minutes)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (slot_id, booking_id, project_id, kind, rid,
                     start.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M"),
                     len(minutes)))
                conn.executemany(
                    "INSERT INTO slot_minutes(resource_kind, resource_id, minute, slot_id) "
                    "VALUES(?,?,?,?)",
                    [(kind, rid, mm, slot_id) for mm in minutes])
            log_event(conn, "schedule.confirmed", actor, project_id=project_id,
                      timezone=team["timezone"], entity_type="booking",
                      entity_id=booking_id,
                      payload={"scene_ref": scene_ref, "version_id": version_id,
                               "set_id": set_id, "team_id": team_id,
                               "talent_ids": talent_ids,
                               "starts_at": iso(start), "ends_at": iso(end),
                               "team_local_start": iso_local(in_timezone(start, team["timezone"])),
                               "set_local_start": iso_local(in_timezone(start, setrow["timezone"]))})
        return self.get_booking(booking_id)

    def cancel_booking(self, booking_id, reason, *, actor="scheduler"):
        with self.tx() as conn:
            b = self._must(conn, "schedule_bookings", "booking_id", booking_id, "档期")
            if b["status"] != "confirmed":
                raise ConflictState("档期不是 confirmed 状态，无需取消",
                                    {"status": b["status"]})
            shot = conn.execute(
                "SELECT 1 FROM shots WHERE booking_id=? LIMIT 1", (booking_id,)).fetchone()
            if shot:
                raise ConflictState("已有镜头基于该档期拍摄，不能取消（镜头不可变）",
                                    {"booking_id": booking_id})
            conn.execute(
                "UPDATE schedule_bookings SET status='cancelled', reason=? "
                "WHERE booking_id=?", (reason, booking_id))
            conn.execute("DELETE FROM slot_minutes WHERE slot_id IN "
                         "(SELECT slot_id FROM schedule_slots WHERE booking_id=?)",
                         (booking_id,))
            conn.execute("DELETE FROM schedule_slots WHERE booking_id=?", (booking_id,))
            team = conn.execute(
                """SELECT t.timezone FROM teams t JOIN project_teams pt
                   ON t.team_id=pt.team_id WHERE pt.project_id=? LIMIT 1""",
                (b["project_id"],)).fetchone()
            log_event(conn, "schedule.cancelled", actor, project_id=b["project_id"],
                      timezone=team["timezone"] if team else None,
                      entity_type="booking", entity_id=booking_id,
                      payload={"scene_ref": b["scene_ref"], "reason": reason})
        return {"booking_id": booking_id, "status": "cancelled", "reason": reason}

    def get_booking(self, booking_id):
        with self.conn() as conn:
            b = self._must(conn, "schedule_bookings", "booking_id", booking_id, "档期")
            return self._booking_dict(conn, b)

    def list_bookings(self, project_id):
        with self.conn() as conn:
            self._project(conn, project_id)
            rows = conn.execute(
                "SELECT * FROM schedule_bookings WHERE project_id=? ORDER BY created_at",
                (project_id,)).fetchall()
            return [self._booking_dict(conn, r) for r in rows]

    # ---------------- 拍摄：镜头钉住批准版本 ----------------
    def record_shot(self, booking_id, material_refs, *, shot_id=None, actor="crew"):
        """登记已拍镜头。版本 seq/哈希取自档期对应版本，此后不可修改。"""
        if not isinstance(material_refs, list) or not material_refs:
            raise ValidationError("镜头必须登记使用的素材 material_refs")
        shot_id = shot_id or new_id("shot")
        with self.tx() as conn:
            b = self._must(conn, "schedule_bookings", "booking_id", booking_id, "档期")
            if b["status"] != "confirmed":
                raise ConflictState("档期未确认，不能拍摄", {"status": b["status"]})
            v = conn.execute("SELECT * FROM script_versions WHERE version_id=?",
                             (b["version_id"],)).fetchone()
            body = loads(v["scenes_json"])
            scene = next((s for s in body["scenes"]
                          if s["scene_ref"] == b["scene_ref"]), None)
            if scene is None:
                raise ConflictState("批准版本中的场景缺失，数据异常",
                                    {"scene_ref": b["scene_ref"]})
            allowed = set(scene["material_refs"])
            for ref in material_refs:
                if ref not in allowed:
                    raise ValidationError(
                        f"素材 {ref} 不属于锁定版本场景 {b['scene_ref']} 的素材清单",
                        {"allowed": sorted(allowed)})
            team_row = conn.execute(
                "SELECT resource_id FROM schedule_slots WHERE booking_id=? "
                "AND resource_kind='team' LIMIT 1", (booking_id,)).fetchone()
            conn.execute(
                """INSERT INTO shots (shot_id, project_id, booking_id, scene_ref,
                     version_id, version_seq, content_hash, material_refs_json,
                     taken_at, team_id, immutable)
                   VALUES(?,?,?,?,?,?,?,?,?,? ,1)""",
                (shot_id, b["project_id"], booking_id, b["scene_ref"],
                 v["version_id"], v["seq"], v["content_hash"],
                 dumps(material_refs), iso(now_utc()),
                 team_row["resource_id"] if team_row else None))
            tz = None
            if team_row:
                t = conn.execute("SELECT timezone FROM teams WHERE team_id=?",
                                 (team_row["resource_id"],)).fetchone()
                tz = t["timezone"] if t else None
            log_event(conn, "shot.recorded", actor, project_id=b["project_id"],
                      timezone=tz, entity_type="shot", entity_id=shot_id,
                      payload={"booking_id": booking_id, "scene_ref": b["scene_ref"],
                               "version_id": v["version_id"], "version_seq": v["seq"],
                               "content_hash": v["content_hash"]})
        return self.get_shot(shot_id)

    def get_shot(self, shot_id):
        with self.conn() as conn:
            row = self._must(conn, "shots", "shot_id", shot_id, "镜头")
            return self._shot_dict(row)

    def list_shots(self, project_id):
        with self.conn() as conn:
            self._project(conn, project_id)
            return [self._shot_dict(r) for r in conn.execute(
                "SELECT * FROM shots WHERE project_id=? ORDER BY taken_at",
                (project_id,))]

    # ---------------- 冲突检测 ----------------
    def _detect_conflicts(self, conn, resources, start, end, minutes):
        first, last = minutes[0], minutes[-1]
        reasons = []
        names = {"set": "sets", "team": "teams", "talent": "talent"}
        for kind, rid in resources:
            rows = conn.execute(
                """SELECT m.minute, b.booking_id, b.project_id, b.scene_ref,
                          b.version_id, sl.start_minute, sl.end_minute
                   FROM slot_minutes m
                   JOIN schedule_slots sl ON sl.slot_id = m.slot_id
                   JOIN schedule_bookings b ON b.booking_id = sl.booking_id
                   WHERE m.resource_kind=? AND m.resource_id=?
                     AND m.minute BETWEEN ? AND ?
                     AND b.status='confirmed'""",
                (kind, rid, first, last)).fetchall()
            if not rows:
                continue
            by_booking = {}
            for r in rows:
                by_booking.setdefault(r["booking_id"], []).append(r["minute"])
            tzname = None
            res_name = rid
            if kind == "talent":
                trow = conn.execute("SELECT name FROM talent WHERE talent_id=?",
                                    (rid,)).fetchone()
                if trow:
                    res_name = trow["name"]
            else:
                id_col = {"set": "set_id", "team": "team_id"}[kind]
                trow = conn.execute(
                    f"SELECT name, timezone FROM {names[kind]} WHERE {id_col}=?",
                    (rid,)).fetchone()
                if trow:
                    res_name, tzname = trow["name"], trow["timezone"]
            for other_id, mins in by_booking.items():
                sample = next(r for r in rows if r["booking_id"] == other_id)
                reason = {
                    "resource_kind": kind, "resource_id": rid,
                    "resource_name": res_name,
                    "other_booking_id": other_id,
                    "other_project_id": sample["project_id"],
                    "other_scene_ref": sample["scene_ref"],
                    "other_version_id": sample["version_id"],
                    "overlap_minutes_utc": sorted(mins),
                    "requested_start_utc": iso(start),
                    "requested_end_utc": iso(end),
                }
                if tzname:
                    reason["resource_timezone"] = tzname
                    reason["requested_local_start"] = iso_local(
                        in_timezone(start, tzname))
                    reason["requested_local_end"] = iso_local(
                        in_timezone(end, tzname))
                reasons.append(reason)
        return reasons

    # ---------------- 工具 ----------------
    @staticmethod
    def _team_on_project(conn, project_id, team_id):
        row = conn.execute(
            "SELECT 1 FROM project_teams WHERE project_id=? AND team_id=?",
            (project_id, team_id)).fetchone()
        if row is None:
            raise ValidationError(f"团队 {team_id} 未分配到该项目",
                                  {"team_id": team_id})

    @staticmethod
    def _scene_in_version(version_row, scene_ref):
        body = loads(version_row["scenes_json"])
        refs = {s["scene_ref"] for s in body["scenes"]}
        if scene_ref not in refs:
            raise ValidationError(f"场景 {scene_ref} 不在锁定版本中",
                                  {"known_scenes": sorted(refs)})

    @staticmethod
    def _normalize_talents(talent_ids):
        if not isinstance(talent_ids, list) or not talent_ids:
            raise ValidationError("至少指定一位演员 talent_ids")
        out = []
        for t in talent_ids:
            if t not in out:
                out.append(t)
        return out

    @staticmethod
    def _minute_range(start, end):
        if start >= end:
            raise ValidationError("排期开始时间必须早于结束时间")
        total = int((end - start).total_seconds() // 60)
        if total < 1:
            raise ValidationError("排期时长至少 1 分钟")
        if total > MAX_BOOKING_MINUTES:
            raise ValidationError(f"单次排期不能超过 {MAX_BOOKING_MINUTES} 分钟")
        return [(start + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M")
                for i in range(total)]

    @staticmethod
    def _shot_dict(row):
        d = dict(row)
        d["immutable"] = bool(d["immutable"])
        d["material_refs"] = loads(d.pop("material_refs_json"))
        d["provenance"] = {
            "version_id": d.pop("version_id"), "version_seq": d.pop("version_seq"),
            "content_hash": d.pop("content_hash"),
            "note": "镜头来源固化于拍摄时的批准版本，后续剧本更新不改变本字段",
        }
        return d

    def _booking_dict(self, conn, row):
        d = dict(row)
        slots = conn.execute(
            "SELECT resource_kind, resource_id, start_minute, end_minute "
            "FROM schedule_slots WHERE booking_id=?", (row["booking_id"],)).fetchall()
        grouped = {"set": [], "team": [], "talent": []}
        for s in slots:
            grouped[s["resource_kind"]].append(
                {"resource_id": s["resource_id"], "start_minute": s["start_minute"],
                 "end_minute": s["end_minute"]})
        d["resources"] = grouped
        d["set_id"] = grouped["set"][0]["resource_id"] if grouped["set"] else None
        d["team_id"] = grouped["team"][0]["resource_id"] if grouped["team"] else None
        d["talent_ids"] = [x["resource_id"] for x in grouped["talent"]]
        return d
