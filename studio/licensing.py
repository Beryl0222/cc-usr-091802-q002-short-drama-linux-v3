"""来源 IP、市场授权窗口与项目立项锁定。

一个项目按市场锁定：可改编市场、语言、期限与素材边界。锁定时必须存在
覆盖该市场的有效授权（语言为授权语言子集、期限落在授权窗口内、素材不
越出授权素材边界）。撤权按市场进行，级联阻断由外观层协调资产模块。
"""

from . import clock
from .errors import ConflictError, StateError, ValidationError
from .models import (
    GRANT_ACTIVE,
    GRANT_REVOKED,
    LOCK_ACTIVE,
    LOCK_REVOKED,
    grant,
    ip,
    market_lock,
    project,
)


def _require_nonempty(value, label):
    if value is None or (isinstance(value, str) and not value.strip()) or value == []:
        raise ValidationError(f"{label}不能为空")


class LicensingService:
    def __init__(self, store, events):
        self.store = store
        self.events = events

    # ---- 来源 IP -------------------------------------------------------

    def register_ip(self, ip_id, title, rights_holder, actor):
        _require_nonempty(ip_id, "IP 标识")
        _require_nonempty(title, "作品名称")
        _require_nonempty(rights_holder, "版权方")
        with self.store.tx():
            if self.store.get("ips", ip_id) is not None:
                raise ConflictError(f"IP 已登记: {ip_id}")
            entity = ip(ip_id, title, rights_holder, actor)
            self.store.save("ips", ip_id, entity)
            self.events.append("ip.registered", actor, {"ip_id": ip_id},
                               {"title": title, "rights_holder": rights_holder})
            return self.store.snapshot(entity)

    # ---- 授权窗口 ------------------------------------------------------

    def create_grant(self, grant_id, ip_id, market, languages, start_at,
                     end_at, material_scope, actor):
        _require_nonempty(grant_id, "授权标识")
        _require_nonempty(market, "市场")
        languages = list(languages or [])
        material_scope = list(material_scope or [])
        if not languages:
            raise ValidationError("授权语言不能为空")
        if not material_scope:
            raise ValidationError("素材边界不能为空")
        start, end = clock.utc(start_at), clock.utc(end_at)
        if not start < end:
            raise ValidationError("授权期限开始必须早于结束")
        with self.store.tx():
            self.store.require("ips", ip_id, "来源 IP")
            if self.store.get("grants", grant_id) is not None:
                raise ConflictError(f"授权已存在: {grant_id}")
            entity = grant(grant_id, ip_id, market, languages, start, end,
                           material_scope, actor)
            self.store.save("grants", grant_id, entity)
            self.events.append(
                "grant.created", actor,
                {"grant_id": grant_id, "ip_id": ip_id, "market": market},
                {"languages": entity["languages"],
                 "start_at": entity["start_at"], "end_at": entity["end_at"],
                 "material_scope": entity["material_scope"]},
            )
            return self.store.snapshot(entity)

    def covering_grants(self, ip_id, market, languages, start, end, materials):
        """返回覆盖给定锁定参数的、当前仍有效的授权。"""
        result = []
        for g in self.store.list("grants", lambda g: g["ip_id"] == ip_id
                                 and g["market"] == market
                                 and g["status"] == GRANT_ACTIVE):
            if not (clock.utc(g["start_at"]) <= start
                    and end <= clock.utc(g["end_at"])):
                continue
            if not set(languages) <= set(g["languages"]):
                continue
            if not set(materials) <= set(g["material_scope"]):
                continue
            result.append(g)
        return result

    # ---- 项目立项 ------------------------------------------------------

    def create_project(self, project_id, ip_id, name, market_specs, actor):
        """立项并逐市场锁定。

        ``market_specs``::

            [{"market", "languages", "term_start", "term_end",
              "material_boundary"}]
        """
        _require_nonempty(project_id, "项目标识")
        _require_nonempty(name, "项目名称")
        specs = list(market_specs or [])
        if not specs:
            raise ValidationError("至少锁定一个可改编市场")
        normalized = []
        for spec in specs:
            m = spec.get("market")
            langs = list(spec.get("languages") or [])
            materials = list(spec.get("material_boundary") or [])
            _require_nonempty(m, "市场")
            if not langs:
                raise ValidationError(f"市场 {m} 的语言不能为空")
            if not materials:
                raise ValidationError(f"市场 {m} 的素材边界不能为空")
            start, end = clock.utc(spec["term_start"]), clock.utc(spec["term_end"])
            if not start < end:
                raise ValidationError(f"市场 {m} 的期限开始必须早于结束")
            normalized.append((m, langs, start, end, materials))
        markets = [s[0] for s in normalized]
        if len(markets) != len(set(markets)):
            raise ValidationError("同一项目内市场不能重复锁定")

        with self.store.tx():
            self.store.require("ips", ip_id, "来源 IP")
            if self.store.get("projects", project_id) is not None:
                raise ConflictError(f"项目已存在: {project_id}")
            locks = []
            for m, langs, start, end, materials in normalized:
                basis = self.covering_grants(ip_id, m, langs, start, end, materials)
                if not basis:
                    raise ConflictError(
                        f"市场 {m} 缺少覆盖语言/期限/素材边界的有效授权",
                        details={"market": m, "languages": langs,
                                 "term": [clock.iso(start), clock.iso(end)],
                                 "material_boundary": materials},
                    )
                locks.append(market_lock(m, langs, start, end, materials,
                                         [g["grant_id"] for g in basis]))
            entity = project(project_id, ip_id, name, locks, actor)
            self.store.save("projects", project_id, entity)
            self.events.append(
                "project.created", actor,
                {"project_id": project_id, "ip_id": ip_id},
                {"name": name, "markets": list(entity["markets"].keys())},
            )
            for lock in locks:
                self.events.append(
                    "market.locked", actor,
                    {"project_id": project_id, "market": lock["market"]},
                    {"languages": lock["languages"],
                     "term_start": lock["term_start"],
                     "term_end": lock["term_end"],
                     "material_boundary": lock["material_boundary"],
                     "basis_grant_ids": lock["basis_grant_ids"]},
                )
            return self.store.snapshot(entity)

    def get_lock(self, project_id, market):
        project_ = self.store.require("projects", project_id, "项目")
        lock = project_["markets"].get(market)
        if lock is None:
            raise ConflictError(f"项目未锁定该市场: {market}")
        return project_, lock

    def assert_lock_active(self, project_id, market, at=None):
        """断言市场锁当前有效且未过权利窗口（拍摄/发布前调用）。"""
        _, lock = self.get_lock(project_id, market)
        if lock["status"] != LOCK_ACTIVE:
            raise StateError(
                f"市场 {market} 授权已撤销，不能继续制作或发行",
                details={"market": market, "revoke_reason": lock.get("revoke_reason")},
            )
        moment = clock.utc(at or clock.now())
        if not clock.in_window(moment, lock["term_start"], lock["term_end"]):
            raise StateError(
                f"市场 {market} 的权利窗口已失效",
                details={"market": market,
                         "term": [lock["term_start"], lock["term_end"]]},
            )
        return lock

    # ---- 按市场撤权 ----------------------------------------------------

    def revoke_grants(self, grant_ids, actor, reason):
        """撤销一批授权（须属同一 IP 的同一市场），并据此处理项目锁。

        检查其余 *未撤销* 授权是否仍完整覆盖锁：仍覆盖则保留锁并更新依据，
        否则撤锁。返回 ``(grant_ids, project_markets)``，由外观层据此阻断
        尚未发行的版本。
        """
        grant_ids = list(grant_ids or [])
        if not grant_ids:
            raise ValidationError("撤权必须指定至少一条授权")
        _require_nonempty(reason, "撤权原因")
        with self.store.tx():
            revoked_grants = []
            ip_market = None
            for gid in grant_ids:
                g = self.store.require("grants", gid, "授权")
                if g["status"] != GRANT_ACTIVE:
                    raise ConflictError(
                        f"授权 {gid} 已撤销，不能重复撤权",
                        details={"status": g["status"]},
                    )
                key = (g["ip_id"], g["market"])
                if ip_market is not None and key != ip_market:
                    raise ValidationError("一次撤权只能针对同一 IP 的同一市场")
                ip_market = key
                g["status"] = GRANT_REVOKED
                g["revoked_at"] = clock.iso(clock.now())
                g["revoked_by"] = actor
                g["revoke_reason"] = reason
                self.store.save("grants", gid, g)
                revoked_grants.append(gid)
                self.events.append(
                    "grant.revoked", actor,
                    {"grant_id": gid, "ip_id": key[0], "market": key[1]},
                    {"reason": reason},
                )
            ip_id, market = ip_market
            affected = []
            for p in self.store.list("projects", lambda p: p["ip_id"] == ip_id):
                lock = p["markets"].get(market)
                if not lock or lock["status"] != LOCK_ACTIVE:
                    continue
                if not (set(lock["basis_grant_ids"]) & set(revoked_grants)):
                    continue
                # 项目锁的依据授权仍被其他有效授权完整覆盖时，只更新依据
                still_covered = self.covering_grants(
                    ip_id, market, lock["languages"],
                    clock.utc(lock["term_start"]),
                    clock.utc(lock["term_end"]),
                    lock["material_boundary"])
                if still_covered:
                    lock["basis_grant_ids"] = [g["grant_id"] for g in still_covered]
                    p["markets"][market] = lock
                    self.store.save("projects", p["project_id"], p)
                    self.events.append(
                        "market.lock_rebased", actor,
                        {"project_id": p["project_id"], "ip_id": ip_id,
                         "market": market},
                        {"basis_grant_ids": lock["basis_grant_ids"]},
                    )
                    continue
                lock["status"] = LOCK_REVOKED
                lock["revoked_at"] = clock.iso(clock.now())
                lock["revoked_by"] = actor
                lock["revoke_reason"] = reason
                p["markets"][market] = lock
                self.store.save("projects", p["project_id"], p)
                affected.append((p["project_id"], market))
                self.events.append(
                    "market.revoked", actor,
                    {"project_id": p["project_id"], "ip_id": ip_id,
                     "market": market},
                    {"reason": reason},
                )
            return revoked_grants, affected

    def active_grant_ids(self, ip_id, market):
        return [g["grant_id"] for g in self.store.list(
            "grants", lambda g: g["ip_id"] == ip_id and g["market"] == market
            and g["status"] == GRANT_ACTIVE)]
