"""派生资产谱系：粗剪 → 字幕/配音 → 市场版式，以及发布与撤权阻断。

规则：

- 每条资产只有一个父资产（粗剪除外），父资产必须同项目同市场且类型合法，
  从而形成可回溯到镜头与已下发剧本版本的派生谱系（DAG，实际为树/林）。
- 撤权按市场级联：**尚未发行**的资产一律 ``blocked`` 冻结；**已发行**的
  版本标记 ``recalled``（收到撤权通知）但资产、回执、其他市场资产全部保留。
"""

from . import clock
from .errors import ConflictError, StateError, ValidationError
from .models import (
    ASSET_BLOCKED,
    ASSET_DRAFT,
    ASSET_MARKET_FORMAT,
    ASSET_PARENTS,
    ASSET_ROUGH_CUT,
    ASSET_SUBTITLE,
    ASSET_DUB,
    RELEASE_RECALLED,
    asset,
    release,
)


class AssetService:
    def __init__(self, store, events, licensing):
        self.store = store
        self.events = events
        self.licensing = licensing

    # ---- 派生谱系 ------------------------------------------------------

    def create_asset(self, asset_id, project_id, market, type_, created_by,
                     parent_asset_id=None, language=None,
                     source_shot_ids=None, fingerprint=None, actor=None):
        if type_ not in ASSET_PARENTS:
            raise ValidationError(f"未知资产类型: {type_}")
        with self.store.tx():
            if self.store.get("assets", asset_id) is not None:
                raise ConflictError(f"资产已存在: {asset_id}")
            _, lock = self.licensing.get_lock(project_id, market)
            # 撤权或权利窗口过期后不能再制作新的派生版本
            self.licensing.assert_lock_active(project_id, market)

            shot_ids = list(source_shot_ids or [])
            if type_ == ASSET_ROUGH_CUT:
                if parent_asset_id is not None:
                    raise ValidationError("粗剪不能有父资产")
                if not shot_ids:
                    raise ValidationError("粗剪必须至少包含一个已拍摄镜头")
            else:
                if parent_asset_id is None:
                    raise ValidationError(f"{type_} 必须指定父资产")
                parent = self.store.require("assets", parent_asset_id, "父资产")
                if parent["project_id"] != project_id or parent["market"] != market:
                    raise ConflictError("父资产必须属于同一项目与市场")
                if parent["status"] == ASSET_BLOCKED:
                    raise StateError("父资产已被撤权阻断，不能继续派生")
                if parent["type"] not in ASSET_PARENTS[type_]:
                    raise ConflictError(
                        f"{type_} 不能派生自 {parent['type']}，"
                        f"允许的父类型: {ASSET_PARENTS[type_]}",
                    )
                if not shot_ids:
                    shot_ids = list(parent["source_shot_ids"])
                elif not set(shot_ids) <= set(parent["source_shot_ids"]):
                    raise ConflictError("派生资产的镜头不能超出父资产来源镜头")

            if type_ in (ASSET_SUBTITLE, ASSET_DUB, ASSET_MARKET_FORMAT):
                if not language:
                    raise ValidationError(f"{type_} 必须指定语言")
                if language not in lock["languages"]:
                    raise ConflictError(
                        f"语言 {language} 不在市场 {market} 锁定语言范围内",
                        details={"allowed": lock["languages"]},
                    )

            for sid in shot_ids:
                shot = self.store.require("shots", sid, "来源镜头")
                if shot["project_id"] != project_id or shot["market"] != market:
                    raise ConflictError(f"镜头 {sid} 不属于该项目/市场")

            fp = fingerprint or self._fingerprint(
                type_, parent_asset_id, language, shot_ids)
            entity = asset(asset_id, project_id, market, type_,
                           parent_asset_id, language, created_by or actor,
                           shot_ids, fp)
            self.store.save("assets", asset_id, entity)
            self.events.append(
                "asset.created", created_by or actor or "system",
                {"project_id": project_id, "market": market,
                 "asset_id": asset_id},
                {"type": type_, "parent_asset_id": parent_asset_id,
                 "language": language, "source_shot_ids": shot_ids},
            )
            return self.store.snapshot(entity)

    @staticmethod
    def _fingerprint(type_, parent_id, language, shot_ids):
        basis = f"{type_}|{parent_id or '-'}|{language or '-'}|{','.join(sorted(shot_ids))}"
        return f"fp-{abs(hash(basis)) % (10 ** 12):012d}"

    def lineage(self, asset_id):
        """返回资产的完整祖先链（直到粗剪）与底层镜头来源。"""
        with self.store.tx():
            chain = []
            current = self.store.require("assets", asset_id, "资产")
            while current is not None:
                chain.append({
                    "asset_id": current["asset_id"],
                    "type": current["type"],
                    "language": current["language"],
                    "status": current["status"],
                    "parent_asset_id": current["parent_asset_id"],
                    "source_shot_ids": list(current["source_shot_ids"]),
                    "created_by": current["created_by"],
                    "created_at": current["created_at"],
                })
                if current["parent_asset_id"] is None:
                    break
                current = self.store.get("assets", current["parent_asset_id"])
            shots = {}
            for sid in chain[0]["source_shot_ids"]:
                shot = self.store.get("shots", sid)
                if shot is not None:
                    shots[sid] = {
                        "shot_id": sid,
                        "version_id": shot["version_id"],
                        "item_id": shot["item_id"],
                        "material_id": shot["material_id"],
                        "shot_at": shot["shot_at"],
                    }
            return {"asset_id": asset_id, "ancestors": chain,
                    "source_shots": shots}

    # ---- 发行 ----------------------------------------------------------

    def publish(self, asset_id, platform, actor):
        if not platform:
            raise ValidationError("发行平台不能为空")
        with self.store.tx():
            entity = self.store.require("assets", asset_id, "资产")
            if entity["status"] == ASSET_BLOCKED:
                raise StateError(
                    "资产因撤权被阻断，尚未发行，禁止发布",
                    details={"asset_id": asset_id,
                             "blocked_reason": entity.get("blocked_reason")},
                )
            key = (asset_id, entity["market"], platform)
            existing = self.store.release_keys.get(key)
            if existing is not None:
                prior = self.store.require("releases", existing, "发布记录")
                if prior["status"] == RELEASE_RECALLED:
                    raise StateError(
                        "该发布已随撤权召回；权利恢复前不能重新发布",
                        details={"release_id": existing},
                    )
                raise ConflictError(f"资产已在 {platform} 发布",
                                    details={"release_id": existing})
            self.licensing.assert_lock_active(entity["project_id"],
                                              entity["market"])
            release_id = f"rel-{asset_id}-{platform}"
            rec = release(release_id, asset_id, entity["project_id"],
                          entity["market"], platform, actor)
            self.store.save("releases", release_id, rec)
            self.store.release_keys[key] = release_id
            self.events.append(
                "asset.published", actor,
                {"project_id": entity["project_id"],
                 "market": entity["market"], "asset_id": asset_id,
                 "release_id": release_id},
                {"platform": platform, "type": entity["type"]},
            )
            return self.store.snapshot(rec)

    # ---- 撤权级联 ------------------------------------------------------

    def block_market(self, project_id, market, reason, actor):
        """冻结未发行资产、召回已发行版本；其他市场资产一律不动。

        必须在撤掉市场锁之后调用（外观层在同一事务里完成）。
        """
        blocked, recalled = [], []
        for a in self.store.list(
                "assets", lambda a: a["project_id"] == project_id
                and a["market"] == market):
            releases = self.store.list(
                "releases", lambda r, aid=a["asset_id"]:
                r["asset_id"] == aid and r["status"] != RELEASE_RECALLED)
            if releases:
                for r in releases:
                    r["status"] = RELEASE_RECALLED
                    r["recalled_by"] = actor
                    r["recalled_at"] = clock.iso(clock.now())
                    r["recall_reason"] = reason
                    self.store.save("releases", r["release_id"], r)
                    recalled.append(r["release_id"])
                    self.events.append(
                        "release.recalled", actor,
                        {"project_id": project_id, "market": market,
                         "asset_id": a["asset_id"],
                         "release_id": r["release_id"]},
                        {"reason": reason},
                    )
            else:
                a["status"] = ASSET_BLOCKED
                a["blocked_reason"] = reason
                self.store.save("assets", a["asset_id"], a)
                blocked.append(a["asset_id"])
                self.events.append(
                    "asset.blocked", actor,
                    {"project_id": project_id, "market": market,
                     "asset_id": a["asset_id"]},
                    {"reason": reason, "type": a["type"]},
                )
        return {"blocked_asset_ids": blocked, "recalled_release_ids": recalled}
