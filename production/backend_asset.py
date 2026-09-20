"""派生谱系与发行域。

谱系：rough_cut（粗剪，根，来自已拍镜头）→ subtitle/dub → market_format。
每个资产沿父链回溯到同一粗剪根与同一锁定剧本版本；market_format 发行到
具体市场时再校验授权窗口与该市场的素材限制。撤权只把对应市场“尚未发行”
的发行单置 blocked，其他市场资产与已发行记录均不受影响。
"""

import sqlite3

from .audit import log_event
from .clock import iso, new_id, now_utc, parse_dt
from .db import dumps, loads
from .errors import ConflictState, IntegrityGuard, NotFound, ValidationError

KIND_ROUGH_CUT = "rough_cut"
KIND_SUBTITLE = "subtitle"
KIND_DUB = "dub"
KIND_MARKET_FORMAT = "market_format"
DERIVABLE_FROM = {
    KIND_SUBTITLE: {KIND_ROUGH_CUT},
    KIND_DUB: {KIND_ROUGH_CUT},
    # 市场版式必须建立在某语言的字幕/配音之上，不能跳过本地化直接挂粗剪
    KIND_MARKET_FORMAT: {KIND_SUBTITLE, KIND_DUB},
}
MAX_LINEAGE_DEPTH = 32


class AssetMixin:
    def create_asset(self, project_id, kind, content_hash, created_by, *,
                     language=None, market=None, parent_asset_id=None,
                     root_shot_ids=None, version_id=None):
        if kind not in (KIND_ROUGH_CUT, KIND_SUBTITLE, KIND_DUB, KIND_MARKET_FORMAT):
            raise ValidationError(f"未知资产类型 {kind}")
        if not content_hash:
            raise ValidationError("content_hash 不能为空")
        with self.tx() as conn:
            self._project(conn, project_id)
            parent = None
            if kind == KIND_ROUGH_CUT:
                if parent_asset_id is not None:
                    raise ValidationError("粗剪是谱系根，不能有父资产")
                if not version_id:
                    raise ValidationError("粗剪必须指定基于的锁定剧本版本 version_id")
                v = self._approved_version(conn, version_id)
                shots = self._load_shots(conn, project_id, root_shot_ids)
                for s in shots:
                    if s["version_id"] != v["version_id"]:
                        raise ConflictState(
                            f"镜头 {s['shot_id']} 拍摄自版本 {s['version_seq']}，"
                            f"不属于粗剪声明的锁定版本 {v['seq']}",
                            {"shot_id": s["shot_id"],
                               "shot_version_seq": s["version_seq"],
                               "cut_version_seq": v["seq"]})
                lineage_version = v["version_id"]
            else:
                allowed_parents = DERIVABLE_FROM[kind]
                if not parent_asset_id:
                    raise ValidationError(f"{kind} 必须指定父资产 parent_asset_id")
                parent = self._must(conn, "assets", "asset_id", parent_asset_id, "父资产")
                if parent["project_id"] != project_id:
                    raise ValidationError("父资产不属于该项目")
                if parent["kind"] not in allowed_parents:
                    raise ValidationError(
                        f"{kind} 只能派生自 {'/'.join(sorted(allowed_parents))}，"
                        f"父资产是 {parent['kind']}")
                if kind in (KIND_SUBTITLE, KIND_DUB, KIND_MARKET_FORMAT) and not language:
                    language = parent["language"]
                if kind in (KIND_SUBTITLE, KIND_DUB) and not language:
                    raise ValidationError(f"{kind} 必须声明语言 language")
                if kind == KIND_MARKET_FORMAT:
                    if not market:
                        raise ValidationError("市场版式必须声明目标市场 market")
                    if not language:
                        raise ValidationError("市场版式必须声明语言 language")
                    self._ensure_project_market(conn, project_id, market)
                root = self._lineage_root(conn, parent_asset_id)
                lineage_version = root["version_id"]
                root_shot_ids = None  # 派生资产的镜头集合由谱系根决定
            asset_id = new_id("ast")
            try:
                conn.execute(
                    """INSERT INTO assets (asset_id, project_id, kind, language, market,
                         parent_asset_id, version_id, root_shot_ids_json, content_hash,
                         created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (asset_id, project_id, kind,
                     clock_norm_language(language) if language else None,
                     market, parent_asset_id, lineage_version,
                     dumps(sorted(root_shot_ids) if root_shot_ids else []),
                     content_hash, created_by, iso(now_utc())))
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    "SELECT * FROM assets WHERE project_id=? AND content_hash=?",
                    (project_id, content_hash)).fetchone()
                if existing:
                    # 相同内容重复登记按幂等处理，返回既有资产
                    return self._asset_dict(conn, existing)
                raise IntegrityGuard("资产写入唯一约束冲突")
            log_event(conn, "asset.created", created_by, project_id=project_id,
                      entity_type="asset", entity_id=asset_id,
                      payload={"kind": kind, "language": language, "market": market,
                               "parent_asset_id": parent_asset_id,
                               "version_id": lineage_version,
                               "root_shot_ids": root_shot_ids or []})
            row = conn.execute("SELECT * FROM assets WHERE asset_id=?",
                               (asset_id,)).fetchone()
            return self._asset_dict(conn, row)

    def get_asset(self, asset_id):
        with self.conn() as conn:
            return self._asset_dict(conn, self._must(
                conn, "assets", "asset_id", asset_id, "资产"))

    def asset_lineage(self, asset_id):
        """沿父链回溯：返回链、粗剪根、根镜头及其钉住的剧本来源。"""
        with self.conn() as conn:
            chain = []
            cur = self._must(conn, "assets", "asset_id", asset_id, "资产")
            seen = set()
            for _ in range(MAX_LINEAGE_DEPTH + 1):
                chain.append(self._asset_dict(conn, cur))
                if cur["asset_id"] in seen:
                    raise ConflictState("资产谱系存在环，数据异常")
                seen.add(cur["asset_id"])
                if cur["parent_asset_id"] is None:
                    break
                cur = conn.execute("SELECT * FROM assets WHERE asset_id=?",
                                   (cur["parent_asset_id"],)).fetchone()
                if cur is None:
                    raise NotFound("谱系父资产缺失")
            if len(chain) > MAX_LINEAGE_DEPTH:
                raise ConflictState("资产谱系深度超限")
            root = chain[-1]
            shots = [dict(r) for r in conn.execute(
                "SELECT shot_id, scene_ref, version_id, version_seq, content_hash, "
                "material_refs_json, taken_at FROM shots WHERE shot_id IN "
                "(SELECT value FROM json_each(?)) ORDER BY taken_at",
                (dumps(root["root_shot_ids"]),))]
            for s in shots:
                s["material_refs"] = loads(s.pop("material_refs_json"))
            return {"asset_id": asset_id, "root_asset_id": root["asset_id"],
                    "root_kind": root["kind"], "script_version_id": root["version_id"],
                    "chain": list(reversed(chain)), "root_shots": shots}

    def list_assets(self, project_id, kind=None):
        with self.conn() as conn:
            self._project(conn, project_id)
            sql = "SELECT * FROM assets WHERE project_id=?"
            params = [project_id]
            if kind:
                sql += " AND kind=?"
                params.append(kind)
            sql += " ORDER BY created_at"
            return [self._asset_dict(conn, r) for r in conn.execute(sql, params)]

    # ---------------- 发行 ----------------
    def schedule_release(self, project_id, asset_id, market, language, platform,
                         scheduled_at, *, actor="distributor"):
        """为市场版式建立发行单：校验授权窗口与该市场素材限制。"""
        language = clock_norm_language(language)
        when = parse_dt(scheduled_at)
        with self.tx() as conn:
            self._project(conn, project_id)
            asset = self._must(conn, "assets", "asset_id", asset_id, "资产")
            if asset["project_id"] != project_id:
                raise ValidationError("资产不属于该项目")
            if asset["kind"] != KIND_MARKET_FORMAT:
                raise ValidationError("只有 market_format 市场版式可以排发行",
                                      {"asset_kind": asset["kind"]})
            if asset["market"] != market or asset["language"] != language:
                raise ValidationError(
                    "发行市场/语言必须与市场版式声明一致",
                    {"asset_market": asset["market"], "asset_language": asset["language"],
                     "requested_market": market, "requested_language": language})
            lic, boundary = self.check_grant(conn, project_id, market, language, at=when)
            self._assert_materials_allowed(conn, asset, lic)
            terms = {"license_id": lic["license_id"], "market": market,
                     "language": language,
                     "starts_at": lic["starts_at"], "expires_at": lic["expires_at"],
                     "license_version": lic["version"],
                     "restricted_materials": loads(lic["restricted_materials_json"], [])}
            release_id = new_id("rel")
            try:
                conn.execute(
                    """INSERT INTO releases (release_id, project_id, asset_id, market,
                         language, platform, scheduled_at, status, license_id,
                         license_terms_json, created_at)
                       VALUES(?,?,?,?,?,?,?, 'scheduled', ?, ?, ?)""",
                    (release_id, project_id, asset_id, market, language, platform,
                     iso(when), lic["license_id"], dumps(terms), iso(now_utc())))
            except sqlite3.IntegrityError:
                raise ConflictState("该资产在此平台已有发行单",
                                    {"asset_id": asset_id, "platform": platform})
            log_event(conn, "release.scheduled", actor, project_id=project_id,
                      entity_type="release", entity_id=release_id,
                      payload={"asset_id": asset_id, "market": market,
                               "language": language, "platform": platform,
                               "scheduled_at": iso(when),
                               "license_id": lic["license_id"]})
        return self.get_release(release_id)

    def publish_release(self, release_id, *, actor="distributor", at=None):
        """上线：再次核验授权仍有效（撤权并发时 scheduled 可能已被 blocked）。"""
        at = at or now_utc()
        with self.tx() as conn:
            r = self._must(conn, "releases", "release_id", release_id, "发行单")
            if r["status"] == "released":
                return self.get_release(release_id)
            if r["status"] == "blocked":
                raise ConflictState("发行单已被阻断（授权撤销），不能上线",
                                    {"blocked_reason": r["blocked_reason"]})
            lic, _ = self.check_grant(conn, r["project_id"], r["market"],
                                      r["language"], at=at)
            asset = conn.execute("SELECT * FROM assets WHERE asset_id=?",
                                 (r["asset_id"],)).fetchone()
            self._assert_materials_allowed(conn, asset, lic)
            conn.execute(
                "UPDATE releases SET status='released', released_at=?, license_id=? "
                "WHERE release_id=?", (iso(at), lic["license_id"], release_id))
            log_event(conn, "release.published", actor, project_id=r["project_id"],
                      entity_type="release", entity_id=release_id,
                      payload={"market": r["market"], "language": r["language"],
                               "platform": r["platform"],
                               "license_id": lic["license_id"]})
        return self.get_release(release_id)

    def get_release(self, release_id):
        with self.conn() as conn:
            r = self._must(conn, "releases", "release_id", release_id, "发行单")
            d = dict(r)
            d["license_terms"] = loads(d.pop("license_terms_json"))
            return d

    def list_releases(self, project_id, market=None):
        with self.conn() as conn:
            self._project(conn, project_id)
            sql = "SELECT * FROM releases WHERE project_id=?"
            params = [project_id]
            if market:
                sql += " AND market=?"
                params.append(market)
            sql += " ORDER BY created_at"
            out = []
            for r in conn.execute(sql, params):
                d = dict(r)
                d["license_terms"] = loads(d.pop("license_terms_json"))
                out.append(d)
            return out

    # ---------------- 内部工具 ----------------
    def _lineage_root(self, conn, asset_id):
        cur = conn.execute("SELECT * FROM assets WHERE asset_id=?",
                           (asset_id,)).fetchone()
        seen = set()
        while cur["parent_asset_id"]:
            if cur["asset_id"] in seen:
                raise ConflictState("资产谱系存在环，数据异常")
            seen.add(cur["asset_id"])
            cur = conn.execute("SELECT * FROM assets WHERE asset_id=?",
                               (cur["parent_asset_id"],)).fetchone()
            if cur is None:
                raise NotFound("谱系父资产缺失")
        if cur["kind"] != KIND_ROUGH_CUT:
            raise ConflictState("谱系根不是粗剪", {"root_kind": cur["kind"]})
        return cur

    def _assert_materials_allowed(self, conn, asset, lic):
        """市场版式回溯到根镜头；任何镜头使用了该市场限制素材即拒绝发行。"""
        root = self._lineage_root(conn, asset["asset_id"])
        restricted = set(loads(lic["restricted_materials_json"], []))
        if not restricted:
            return
        rows = conn.execute(
            "SELECT shot_id, material_refs_json FROM shots WHERE shot_id IN "
            "(SELECT value FROM json_each(?))",
            (root["root_shot_ids_json"],)).fetchall()
        violations = []
        for r in rows:
            used = set(loads(r["material_refs_json"]))
            bad = sorted(used & restricted)
            if bad:
                violations.append({"shot_id": r["shot_id"], "restricted_used": bad})
        if violations:
            raise ConflictState(
                "该市场授权排除了部分素材，而谱系中的镜头使用了它们",
                {"market": lic["market"], "violations": violations})

    @staticmethod
    def _load_shots(conn, project_id, shot_ids):
        if not isinstance(shot_ids, list) or not shot_ids:
            raise ValidationError("粗剪必须提供 root_shot_ids 镜头列表")
        out = []
        for sid in shot_ids:
            row = conn.execute("SELECT * FROM shots WHERE shot_id=?", (sid,)).fetchone()
            if row is None:
                raise NotFound(f"镜头 {sid} 不存在")
            if row["project_id"] != project_id:
                raise ValidationError(f"镜头 {sid} 不属于该项目")
            out.append(row)
        return out

    @staticmethod
    def _asset_dict(conn, row):
        d = dict(row)
        d["root_shot_ids"] = loads(d.pop("root_shot_ids_json"), [])
        return d


def clock_norm_language(code):
    # 延迟引用避免模块环
    from .clock import normalize_language

    return normalize_language(code)
