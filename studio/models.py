"""领域实体工厂与状态常量。

实体统一使用 dict 承载，便于 JSON 序列化与快照；跨边界传递内容类数据
（剧本内容、意见、素材边界）时由仓储做深拷贝，保证"已下发/已拍摄"的
快照不被后续改稿改写。
"""

from .clock import iso, now, utc

# 角色
ROLE_RIGHTS = "rights"          # 版权方
ROLE_COMPLIANCE = "compliance"  # 合规
ROLE_PRODUCTION = "production"  # 制片
APPROVAL_ROLES = (ROLE_RIGHTS, ROLE_COMPLIANCE, ROLE_PRODUCTION)

# 剧本意见类型
ANN_LOCALIZATION = "localization"  # 本地顾问的文化本地化意见
ANN_PLOT_CHANGE = "plot_change"   # 编剧据此提出的剧情改动

# 资源类型
RES_SET = "set"
RES_ACTOR = "actor"
RES_TEAM = "team"

# 资产类型与允许的父资产类型（派生谱系）
ASSET_ROUGH_CUT = "rough_cut"
ASSET_SUBTITLE = "subtitle"
ASSET_DUB = "dub"
ASSET_MARKET_FORMAT = "market_format"
ASSET_PARENTS = {
    ASSET_ROUGH_CUT: (),
    ASSET_SUBTITLE: (ASSET_ROUGH_CUT,),
    ASSET_DUB: (ASSET_ROUGH_CUT, ASSET_SUBTITLE),
    ASSET_MARKET_FORMAT: (ASSET_ROUGH_CUT, ASSET_SUBTITLE, ASSET_DUB),
}

# 状态
GRANT_ACTIVE = "active"
GRANT_REVOKED = "revoked"

LOCK_ACTIVE = "active"
LOCK_REVOKED = "revoked"

VER_DRAFT = "draft"
VER_SUBMITTED = "submitted"
VER_APPROVED = "approved"
VER_REJECTED = "rejected"
VER_RELEASED = "released"

BOOKING_CONFIRMED = "confirmed"

ASSET_DRAFT = "draft"
ASSET_BLOCKED = "blocked"  # 撤权阻断：尚未发行，被冻结
RELEASE_RELEASED = "released"
RELEASE_RECALLED = "recalled"  # 已发行，收到撤权通知，资产保留不删
RELEASE_BLOCKED = "blocked"


def ip(ip_id, title, rights_holder, actor):
    return {
        "ip_id": ip_id,
        "title": title,
        "rights_holder": rights_holder,
        "created_by": actor,
        "created_at": iso(now()),
    }


def grant(grant_id, ip_id, market, languages, start_at, end_at,
          material_scope, actor):
    return {
        "grant_id": grant_id,
        "ip_id": ip_id,
        "market": market,
        "languages": sorted(set(languages)),
        "start_at": iso(start_at),
        "end_at": iso(end_at),
        "material_scope": sorted(set(material_scope)),
        "status": GRANT_ACTIVE,
        "granted_by": actor,
        "granted_at": iso(now()),
        "revoked_at": None,
        "revoked_by": None,
        "revoke_reason": None,
    }


def project(project_id, ip_id, name, markets, actor):
    return {
        "project_id": project_id,
        "ip_id": ip_id,
        "name": name,
        "status": "active",
        "created_by": actor,
        "created_at": iso(now()),
        # market -> 市场锁
        "markets": {m["market"]: m for m in markets},
        "version_ids": [],
        "head_version_id": None,
    }


def market_lock(market, languages, start_at, end_at, material_boundary,
                basis_grant_ids):
    return {
        "market": market,
        "languages": sorted(set(languages)),
        "term_start": iso(start_at),
        "term_end": iso(end_at),
        "material_boundary": sorted(set(material_boundary)),
        "basis_grant_ids": list(basis_grant_ids),
        "status": LOCK_ACTIVE,
        "locked_at": iso(now()),
        "revoked_at": None,
        "revoked_by": None,
        "revoke_reason": None,
    }


def script_item(item_id, index, kind, summary, source_ref):
    return {
        "item_id": item_id,
        "index": index,
        "kind": kind,
        "summary": summary,
        "source_ref": source_ref,  # 回到来源网文的章节/段落
    }


def annotation(ann_id, kind, target_item_id, author_role, content,
               basis_ref, related_id=None):
    return {
        "annotation_id": ann_id,
        "kind": kind,
        "target_item_id": target_item_id,
        "author_role": author_role,
        "content": content,
        "basis_ref": basis_ref,
        "related_id": related_id,  # 本地化意见 <-> 剧情改动 互相挂钩
        "created_at": iso(now()),
    }


def script_version(version_id, project_id, parent_id, sequence, created_by,
                   note, content, annotations):
    return {
        "version_id": version_id,
        "project_id": project_id,
        "parent_id": parent_id,
        "sequence": sequence,
        "created_by": created_by,
        "created_at": iso(now()),
        "note": note,
        "content": content,          # 不可变快照
        "annotations": annotations,  # 不可变快照（随版本逐条关联）
        "state": VER_DRAFT,
        "approvals": {},             # role -> {by, at}
        "reject": None,
        "releases": {},              # market -> {released_at, by}
    }


def resource(resource_id, type_, name, timezone, team_id=None, market=None):
    return {
        "resource_id": resource_id,
        "type": type_,
        "name": name,
        "timezone": timezone,  # IANA，团队/演员所在时区
        "team_id": team_id,
        "market": market,
    }


def booking(booking_id, project_id, market, resource_id, start_at, end_at,
             purpose, timezone):
    return {
        "booking_id": booking_id,
        "project_id": project_id,
        "market": market,
        "resource_id": resource_id,
        "start_at": iso(start_at),
        "end_at": iso(end_at),
        "purpose": purpose,
        "timezone": timezone,
        "status": BOOKING_CONFIRMED,
        "created_at": iso(now()),
    }


def shot(shot_id, project_id, market, booking_id, version_id, item_id,
         material_id, source_snapshot):
    return {
        "shot_id": shot_id,
        "project_id": project_id,
        "market": market,
        "booking_id": booking_id,
        "version_id": version_id,
        "item_id": item_id,
        "material_id": material_id,
        "source_snapshot": source_snapshot,  # 拍摄时固化，事后不改
        "shot_at": iso(now()),
    }


def asset(asset_id, project_id, market, type_, parent_asset_id, language,
          created_by, source_shot_ids, fingerprint):
    return {
        "asset_id": asset_id,
        "project_id": project_id,
        "market": market,
        "type": type_,
        "parent_asset_id": parent_asset_id,
        "source_shot_ids": list(source_shot_ids),
        "language": language,
        "created_by": created_by,
        "created_at": iso(now()),
        "fingerprint": fingerprint,
        "status": ASSET_DRAFT,
        "blocked_reason": None,
    }


def release(release_id, asset_id, project_id, market, platform, actor):
    return {
        "release_id": release_id,
        "asset_id": asset_id,
        "project_id": project_id,
        "market": market,
        "platform": platform,
        "status": RELEASE_RELEASED,
        "released_by": actor,
        "released_at": iso(now()),
        "receipt_entry_ids": [],
    }


def ledger_entry(entry_id, project_id, market, release_id, platform,
                 receipt_no, amount, currency, period, idem_key, actor):
    return {
        "entry_id": entry_id,
        "project_id": project_id,
        "market": market,
        "release_id": release_id,
        "platform": platform,
        "receipt_no": receipt_no,
        "amount": str(amount),
        "currency": currency,
        "period": period,
        "idem_key": idem_key,
        "posted_by": actor,
        "posted_at": iso(now()),
    }
