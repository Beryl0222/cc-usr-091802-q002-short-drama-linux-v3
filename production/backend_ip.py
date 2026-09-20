"""Backend 基座、资源主数据与 IP/市场授权域。"""

import sqlite3
from contextlib import contextmanager
from decimal import Decimal

from . import clock
from .audit import log_event
from .clock import iso, new_id, now_utc, parse_dt
from .db import connect, dumps, loads, write_tx
from .errors import ConflictState, IntegrityGuard, NotFound, ValidationError

ROLE_RIGHTS = "rights"
ROLE_COMPLIANCE = "compliance"
ROLE_PRODUCTION = "production"
GATE_ROLES = (ROLE_RIGHTS, ROLE_COMPLIANCE, ROLE_PRODUCTION)


class BackendBase:
    def __init__(self, path):
        # 测试可用 :memory: 的单连接库；线上请给文件路径以获得 WAL 并发
        self.path = path
        self._shared_mem = None
        if path == ":memory:":
            self._shared_mem = connect(":memory:")
        else:
            connect(path).close()  # 确保 schema 落盘

    @contextmanager
    def conn(self):
        if self._shared_mem is not None:
            yield self._shared_mem
            return
        c = connect(self.path)
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def tx(self):
        if self._shared_mem is not None:
            with write_tx(self._shared_mem):
                yield self._shared_mem
            return
        c = connect(self.path)
        try:
            with write_tx(c):
                yield c
        finally:
            c.close()

    # ---------- 读取小工具 ----------
    def _must(self, conn, table, key_field, key_value, label=None):
        row = conn.execute(f"SELECT * FROM {table} WHERE {key_field}=?", (key_value,)).fetchone()
        if row is None:
            raise NotFound(f"{label or table} {key_value} 不存在", {key_field: key_value})
        return row

    def _project(self, conn, project_id):
        return self._must(conn, "projects", "project_id", project_id, "项目")

    def _active_license(self, conn, project_id, market, language):
        return conn.execute(
            """SELECT * FROM licenses
               WHERE project_id=? AND market=? AND language=? AND status='active'""",
            (project_id, market, language),
        ).fetchone()


# ============================ 资源主数据 ============================

class ResourceMixin:
    def create_team(self, team_id, name, timezone):
        clock.check_id(team_id, "team_id")
        clock.validate_timezone(timezone)
        with self.tx() as conn:
            self._insert(conn, "teams",
                         {"team_id": team_id, "name": name, "timezone": timezone})
        return {"team_id": team_id, "name": name, "timezone": timezone}

    def create_set(self, set_id, name, timezone):
        clock.check_id(set_id, "set_id")
        clock.validate_timezone(timezone)
        with self.tx() as conn:
            self._insert(conn, "sets", {"set_id": set_id, "name": name, "timezone": timezone})
        return {"set_id": set_id, "name": name, "timezone": timezone}

    def create_talent(self, talent_id, name):
        clock.check_id(talent_id, "talent_id")
        with self.tx() as conn:
            self._insert(conn, "talent", {"talent_id": talent_id, "name": name})
        return {"talent_id": talent_id, "name": name}

    def assign_team(self, project_id, team_id):
        with self.tx() as conn:
            self._project(conn, project_id)
            self._must(conn, "teams", "team_id", team_id, "团队")
            conn.execute(
                "INSERT OR IGNORE INTO project_teams(project_id, team_id) VALUES(?,?)",
                (project_id, team_id))
            log_event(conn, "team.assigned", "system", project_id=project_id,
                      entity_type="team", entity_id=team_id, payload={"team_id": team_id})
        return {"project_id": project_id, "team_id": team_id}

    @staticmethod
    def _insert(conn, table, row):
        try:
            cols = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
        except sqlite3.IntegrityError as exc:
            raise IntegrityGuard(f"{table} 唯一约束冲突: {exc}")


# ============================ IP 与项目 ============================

class IPMixin:
    def register_ip(self, title, rightsholder, *, ip_id=None, actor="system"):
        ip_id = ip_id or new_id("ip")
        clock.check_id(ip_id, "ip_id")
        if not title or not rightsholder:
            raise ValidationError("来源作品名称与版权方不能为空")
        with self.tx() as conn:
            self._insert(conn, "ips", {"ip_id": ip_id, "title": title,
                                      "rightsholder": rightsholder,
                                      "created_at": iso(now_utc())})
            log_event(conn, "ip.registered", actor, entity_type="ip", entity_id=ip_id,
                      payload={"title": title, "rightsholder": rightsholder})
        return {"ip_id": ip_id, "title": title, "rightsholder": rightsholder}

    def create_project(self, ip_id, title, markets, allowed_source_materials, *,
                       project_id=None, actor="producer"):
        """锁定来源 IP、可改编市场与素材边界——项目一切权利判断的基准。"""
        project_id = project_id or new_id("prj")
        clock.check_id(project_id, "project_id")
        markets = self._normalize_markets(markets)
        materials = self._normalize_materials(allowed_source_materials)
        with self.tx() as conn:
            self._must(conn, "ips", "ip_id", ip_id, "来源 IP")
            self._insert(conn, "projects", {
                "project_id": project_id, "ip_id": ip_id, "title": title,
                "allowed_source_materials_json": dumps(materials),
                "status": "active", "created_at": iso(now_utc())})
            conn.executemany(
                "INSERT INTO project_markets(project_id, market) VALUES(?,?)",
                [(project_id, m) for m in markets])
            log_event(conn, "project.created", actor, project_id=project_id,
                      entity_type="project", entity_id=project_id,
                      payload={"ip_id": ip_id, "title": title, "markets": markets,
                               "allowed_source_materials": materials})
        return {"project_id": project_id, "ip_id": ip_id, "title": title,
                "markets": markets, "allowed_source_materials": materials}

    def get_project(self, project_id):
        with self.conn() as conn:
            p = self._project(conn, project_id)
            markets = [r["market"] for r in conn.execute(
                "SELECT market FROM project_markets WHERE project_id=? ORDER BY market",
                (project_id,))]
            licenses = [self._license_dict(r) for r in conn.execute(
                "SELECT * FROM licenses WHERE project_id=? ORDER BY market, language",
                (project_id,))]
        d = dict(p)
        d["allowed_source_materials"] = loads(p["allowed_source_materials_json"])
        d.pop("allowed_source_materials_json", None)
        d["markets"] = markets
        d["licenses"] = licenses
        return d

    @staticmethod
    def _normalize_markets(markets):
        if not markets or not isinstance(markets, list):
            raise ValidationError("至少指定一个可改编市场")
        out = []
        for m in markets:
            if not isinstance(m, str) or not m.strip():
                raise ValidationError("市场标识不能为空")
            token = m.strip().lower()
            if token not in out:
                out.append(token)
        return out

    @staticmethod
    def _normalize_materials(materials):
        if not isinstance(materials, list) or not materials:
            raise ValidationError("素材边界 allowed_source_materials 必须是非空数组")
        out = []
        for m in materials:
            if not isinstance(m, str) or not m.strip():
                raise ValidationError("素材标识不能为空")
            if m not in out:
                out.append(m)
        return out


# ============================ 授权与撤权 ============================

class LicenseMixin:
    def grant_license(self, project_id, market, language, starts_at, expires_at,
                      *, restricted_materials=None, license_id=None, actor="rights"):
        """授予某市场+语言在期限与素材边界内的改编授权。"""
        clock.check_id(market, "market")
        language = clock.normalize_language(language)
        start, end = self._validate_window(starts_at, expires_at)
        with self.tx() as conn:
            project = self._project(conn, project_id)
            self._ensure_project_market(conn, project_id, market)
            boundary = loads(project["allowed_source_materials_json"])
            restricted = self._check_restricted(restricted_materials, boundary)
            license_id = license_id or new_id("lic")
            try:
                conn.execute(
                    """INSERT INTO licenses (license_id, project_id, market, language,
                         starts_at, expires_at, restricted_materials_json, status,
                         version, created_at)
                       VALUES(?,?,?,?,?,?,?, 'active', 1, ?)""",
                    (license_id, project_id, market, language, iso(start), iso(end),
                     dumps(restricted), iso(now_utc())))
            except sqlite3.IntegrityError:
                existing = self._active_license(conn, project_id, market, language)
                raise ConflictState(
                    f"市场 {market}/{language} 已存在有效授权，续约请用 renew，撤销后才可重新授予",
                    {"existing_license_id": existing["license_id"] if existing else None})
            log_event(conn, "license.granted", actor, project_id=project_id,
                      entity_type="license", entity_id=license_id,
                      payload={"market": market, "language": language,
                               "starts_at": iso(start), "expires_at": iso(end),
                               "restricted_materials": restricted})
        return self.get_license(license_id)

    def renew_license(self, license_id, starts_at, expires_at, *, actor="rights"):
        start, end = self._validate_window(starts_at, expires_at)
        with self.tx() as conn:
            lic = self._must(conn, "licenses", "license_id", license_id, "授权")
            if lic["status"] != "active":
                raise ConflictState("授权已撤销，不能续约", {"license_id": license_id})
            conn.execute(
                "UPDATE licenses SET starts_at=?, expires_at=?, version=version+1 WHERE license_id=?",
                (iso(start), iso(end), license_id))
            log_event(conn, "license.renewed", actor, project_id=lic["project_id"],
                      entity_type="license", entity_id=license_id,
                      payload={"starts_at": iso(start), "expires_at": iso(end),
                               "version": lic["version"] + 1})
        return self.get_license(license_id)

    def revoke_license(self, license_id, reason, *, actor="rights", at=None):
        """撤销某地区授权：阻断该市场/语言下尚未发行的版本，不碰其他市场资产。"""
        at = at or now_utc()
        with self.tx() as conn:
            lic = self._must(conn, "licenses", "license_id", license_id, "授权")
            if lic["status"] == "revoked":
                raise ConflictState("授权已经是撤销状态", {"license_id": license_id})
            project_id = lic["project_id"]
            conn.execute(
                """UPDATE licenses SET status='revoked', revoked_at=?, revoke_reason=?,
                       version=version+1 WHERE license_id=?""",
                (iso(at), reason, license_id))
            # 只阻断同一 project + market + language 且尚未发行的排期；
            # released 保留（已成事实），其他市场/语言行根本不在命中范围
            blocked = conn.execute(
                """UPDATE releases SET status='blocked',
                       blocked_reason=?
                   WHERE project_id=? AND market=? AND language=? AND status='scheduled'""",
                (f"授权 {license_id} 撤销: {reason}", project_id,
                 lic["market"], lic["language"])).rowcount
            log_event(conn, "license.revoked", actor, project_id=project_id,
                      entity_type="license", entity_id=license_id,
                      payload={"market": lic["market"], "language": lic["language"],
                               "reason": reason, "blocked_releases": blocked})
        return {"license_id": license_id, "status": "revoked",
                "market": lic["market"], "language": lic["language"],
                "blocked_releases": blocked}

    def get_license(self, license_id):
        with self.conn() as conn:
            return self._license_dict(self._must(conn, "licenses", "license_id",
                                                 license_id, "授权"))

    def list_licenses(self, project_id):
        with self.conn() as conn:
            self._project(conn, project_id)
            return [self._license_dict(r) for r in conn.execute(
                "SELECT * FROM licenses WHERE project_id=? ORDER BY market, language",
                (project_id,))]

    def check_grant(self, conn, project_id, market, language, at=None):
        """授权门禁：返回 (license_row, boundary)。供发行/版式调用。"""
        at = at or now_utc()
        project = self._project(conn, project_id)
        self._ensure_project_market(conn, project_id, market)
        lic = self._active_license(conn, project_id, market, language)
        if lic is None:
            raise ConflictState(
                f"市场 {market}/{language} 无有效授权（未授予或已撤销），禁止下发",
                {"market": market, "language": language})
        if at < parse_dt(lic["starts_at"]):
            raise ConflictState("授权窗口尚未开始",
                                {"starts_at": lic["starts_at"], "at": iso(at)})
        if at > parse_dt(lic["expires_at"]):
            raise ConflictState("授权窗口已过期",
                                {"expires_at": lic["expires_at"], "at": iso(at)})
        boundary = [m for m in loads(project["allowed_source_materials_json"])
                    if m not in loads(lic["restricted_materials_json"], [])]
        return lic, boundary

    @staticmethod
    def _ensure_project_market(conn, project_id, market):
        row = conn.execute(
            "SELECT 1 FROM project_markets WHERE project_id=? AND market=?",
            (project_id, market)).fetchone()
        if row is None:
            raise ValidationError(f"市场 {market} 不在项目可改编市场清单内",
                                  {"field": "market", "value": market})

    @staticmethod
    def _check_restricted(restricted, boundary):
        restricted = restricted or []
        if not isinstance(restricted, list):
            raise ValidationError("restricted_materials 必须是数组")
        for m in restricted:
            if m not in boundary:
                raise ValidationError(
                    f"限制素材 {m} 不在项目素材边界内，无法对某市场单独剔除",
                    {"material": m, "boundary": boundary})
        return restricted

    @staticmethod
    def _validate_window(starts_at, expires_at):
        start, end = parse_dt(starts_at), parse_dt(expires_at)
        if start >= end:
            raise ValidationError("授权开始时间必须早于结束时间",
                                  {"starts_at": iso(start), "expires_at": iso(end)})
        return start, end

    @staticmethod
    def _license_dict(row):
        d = dict(row)
        d["restricted_materials"] = loads(d.pop("restricted_materials_json"), [])
        return d
