"""权利争议反查。

任一上线版本出现争议时，沿现有授权条款、团队时区和制作事件，重建该版本
实际采用的剧情、三方批准人、素材边界与权利窗口。所有信息取自已固化的
镜头快照与不可变事件日志，不依赖可变的当前状态（当前授权状态会另外标注）。
"""

from .models import RELEASE_RECALLED, RELEASE_RELEASED


class TraceService:
    def __init__(self, store, events):
        self.store = store
        self.events = events

    def _rights_view(self, project_id, market, basis_grant_ids=None):
        project_ = self.store.require("projects", project_id, "项目")
        lock = project_["markets"].get(market)
        grants = []
        basis = set(basis_grant_ids or (lock or {}).get("basis_grant_ids") or [])
        for gid in sorted(basis):
            g = self.store.get("grants", gid)
            if g is None:
                grants.append({"grant_id": gid, "missing": True})
                continue
            grants.append({
                "grant_id": gid,
                "ip_id": g["ip_id"],
                "market": g["market"],
                "languages": g["languages"],
                "window": [g["start_at"], g["end_at"]],
                "material_scope": g["material_scope"],
                "granted_by": g["granted_by"],
                "status": g["status"],
                "revoked_at": g["revoked_at"],
                "revoke_reason": g["revoke_reason"],
            })
        return {
            "market_lock": (None if lock is None else {
                "status": lock["status"],
                "languages": lock["languages"],
                "window": [lock["term_start"], lock["term_end"]],
                "material_boundary": lock["material_boundary"],
                "basis_grant_ids": lock["basis_grant_ids"],
                "revoked_at": lock.get("revoked_at"),
                "revoke_reason": lock.get("revoke_reason"),
            }),
            "grant_terms": grants,
        }

    def _team_timezones(self, project_id, market, booking_ids):
        result = {}
        for bid in sorted(set(booking_ids)):
            b = self.store.get("bookings", bid)
            if b is None:
                continue
            r = self.store.get("resources", b["resource_id"])
            result[bid] = {
                "booking_id": bid,
                "window_utc": [b["start_at"], b["end_at"]],
                "resource_id": b["resource_id"],
                "resource_type": r["type"] if r else None,
                "resource_name": r["name"] if r else None,
                "team_id": r["team_id"] if r else None,
                "timezone": r["timezone"] if r else b.get("timezone"),
                "market": r["market"] if r else None,
            }
        return result

    def _production_events(self, project_id, market=None, version_id=None):
        events = self.events.by_ref(project_id=project_id)
        out = []
        for e in events:
            if market is not None and "market" in e.refs and e.refs["market"] != market:
                continue
            if version_id is not None and "version_id" in e.refs \
                    and e.refs["version_id"] != version_id:
                continue
            out.append(e.to_dict())
        return out

    def trace_shot(self, shot_id):
        shot = self.store.require("shots", shot_id, "镜头")
        snap = shot["source_snapshot"]
        return {
            "scope": "shot",
            "shot_id": shot_id,
            "project_id": shot["project_id"],
            "market": shot["market"],
            "adopted_plot": {
                "script_version_id": snap["script_version_id"],
                "script_sequence": snap["script_sequence"],
                "item": snap["script_item"],
            },
            "approvers": {
                "rights": snap["script_approvers"].get("rights"),
                "compliance": snap["script_approvers"].get("compliance"),
                "production": snap["script_approvers"].get("production"),
                "released_to_market_by": snap["released_to_market_by"],
                "released_to_market_at": snap["released_to_market_at"],
            },
            "materials": {
                "material_id": snap["material_id"],
                "boundary_at_shoot": snap["material_boundary"],
            },
            "rights": {
                "window_at_shoot": snap["rights_window"],
                "languages_at_shoot": snap["locked_languages"],
                "grant_terms_at_shoot": snap["grant_terms_at_shoot"],
                "current": self._rights_view(
                    shot["project_id"], shot["market"],
                    snap["basis_grant_ids"]),
            },
            "team_timezone": self._team_timezones(
                shot["project_id"], shot["market"], [snap["booking_id"]]),
            "production_events": self._production_events(
                shot["project_id"], shot["market"], snap["script_version_id"]),
        }

    def trace_asset(self, asset_id):
        asset = self.store.require("assets", asset_id, "资产")
        return self._build(
            scope="asset", project_id=asset["project_id"],
            market=asset["market"], asset=asset, shot_ids=asset["source_shot_ids"])

    def trace_release(self, release_id):
        rel = self.store.require("releases", release_id, "发布记录")
        asset = self.store.require("assets", rel["asset_id"], "资产")
        report = self._build(
            scope="release", project_id=rel["project_id"],
            market=rel["market"], asset=asset, shot_ids=asset["source_shot_ids"])
        report["release"] = {
            "release_id": release_id,
            "platform": rel["platform"],
            "status": rel["status"],
            "released_by": rel["released_by"],
            "released_at": rel["released_at"],
            "recalled_at": rel.get("recalled_at"),
            "recall_reason": rel.get("recall_reason"),
        }
        return report

    def trace_market(self, project_id, market):
        self.store.require("projects", project_id, "项目")
        shot_ids = [s["shot_id"] for s in self.store.list(
            "shots", lambda s: s["project_id"] == project_id
            and s["market"] == market)]
        report = self._build(
            scope="market", project_id=project_id, market=market,
            asset=None, shot_ids=shot_ids)
        return report

    def _build(self, scope, project_id, market, asset, shot_ids):
        shots = [self.store.require("shots", sid, "镜头") for sid in shot_ids]
        # 以该市场最新下发版本为准，同时列出各镜头实际依据的版本
        version_ids = sorted({s["version_id"] for s in shots})
        versions = []
        booking_ids = set()
        for s in shots:
            booking_ids.add(s["booking_id"])
        for vid in version_ids:
            v = self.store.get("versions", vid)
            if v is None:
                continue
            versions.append({
                "version_id": vid,
                "sequence": v["sequence"],
                "state": v["state"],
                "created_by": v["created_by"],
                "approvers": {role: rec["by"]
                              for role, rec in v["approvals"].items()},
                "release_to_market": v["releases"].get(market),
                "plot_items": [
                    {"item_id": i["item_id"], "kind": i["kind"],
                     "summary": i["summary"], "source_ref": i["source_ref"]}
                    for i in v["content"]
                ],
                "annotations": [
                    {"annotation_id": a["annotation_id"], "kind": a["kind"],
                     "target_item_id": a["target_item_id"],
                     "related_id": a.get("related_id"),
                     "author_role": a["author_role"],
                     "content": a["content"]}
                    for a in v["annotations"]
                ],
            })
        materials = sorted({s["material_id"] for s in shots})
        lineage = None
        if asset is not None:
            chain, current = [], asset
            while current is not None:
                chain.append({"asset_id": current["asset_id"],
                              "type": current["type"],
                              "language": current["language"],
                              "status": current["status"],
                              "parent_asset_id": current["parent_asset_id"]})
                current = (self.store.get("assets", current["parent_asset_id"])
                           if current["parent_asset_id"] else None)
            lineage = chain
        return {
            "scope": scope,
            "project_id": project_id,
            "market": market,
            "asset_lineage": lineage,
            "adopted_versions": versions,
            "materials_used": materials,
            "shots": [{"shot_id": s["shot_id"], "version_id": s["version_id"],
                       "item_id": s["item_id"], "material_id": s["material_id"],
                       "booking_id": s["booking_id"], "shot_at": s["shot_at"]}
                      for s in shots],
            "rights": self._rights_view(project_id, market),
            "team_timezones": self._team_timezones(
                project_id, market, booking_ids),
            "production_events": self._production_events(project_id, market),
        }
