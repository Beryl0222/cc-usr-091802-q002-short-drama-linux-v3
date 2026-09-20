"""测试辅助：构造一条完整的"IP→授权→剧本→审批→排期→拍摄→派生→发行→回执"流水线。"""

from studio import ProductionSystem

TERM_START = "2026-01-01T00:00:00Z"
TERM_END = "2027-12-31T23:59:59Z"


def make_system():
    return ProductionSystem()


def seed_project(s, markets=("na", "sea"), project_id="prj-1"):
    """登记 IP 与各市场授权并立项锁定。"""
    s.register_ip("ip-1", "Reborn Tycoon", "阅文版权方", "ops")
    specs = []
    for m in markets:
        s.create_grant(
            f"grant-{m}", "ip-1", m, ["en", "zh"],
            "2026-01-01T00:00:00Z", "2027-12-31T23:59:59Z",
            ["novel-ch1", "novel-ch2", "char-set-a"], "rights-lin")
        specs.append({
            "market": m,
            "languages": ["en"],
            "term_start": "2026-02-01T00:00:00Z",
            "term_end": "2027-06-30T23:59:59Z",
            "material_boundary": ["novel-ch1", "novel-ch2"],
        })
    s.create_project(project_id, "ip-1", "重生大亨(海外版)", specs, "ops")
    return project_id


def approved_script(s, project_id="prj-1", version_id="v-2", markets=("na", "sea")):
    """建两版剧本（第二版含逐条本地化意见与剧情改动），三方批准并下发。"""
    s.create_script_version(
        project_id, "v-1",
        [{"item_id": "sc-1", "kind": "scene", "summary": "主角重生醒来",
          "source_ref": "novel://ch1#p1",
          "material_refs": ["novel-ch1"]},
         {"item_id": "sc-2", "kind": "scene", "summary": "商战初胜",
          "source_ref": "novel://ch2#p3",
          "material_refs": ["novel-ch2"]}],
        "writer-wei", note="初稿")
    s.add_localization_note(
        project_id, "note-1", "sc-1", "advisor-farida",
        "葬礼场景在东南亚市场需避免特定宗教符号", "culture-guide#funeral")
    s.add_plot_change(
        project_id, "chg-1", "note-1", "writer-wei",
        "将灵堂改为医院病房，保留重生反差")
    s.create_script_version(
        project_id, version_id,
        [{"item_id": "sc-1", "kind": "scene",
          "summary": "主角在医院醒来（本地化调整）",
          "source_ref": "novel://ch1#p1", "material_refs": ["novel-ch1"]},
         {"item_id": "sc-2", "kind": "scene", "summary": "商战初胜",
          "source_ref": "novel://ch2#p3", "material_refs": ["novel-ch2"]}],
        "writer-wei", parent_id="v-1", note="按本地化意见修订")
    s.submit_script(version_id, "writer-wei")
    s.approve_script(version_id, "rights", "rights-lin")
    s.approve_script(version_id, "compliance", "comp-zhou")
    s.approve_script(version_id, "production", "prod-chen")
    for m in markets:
        s.release_script_to_market(version_id, m, "prod-chen")
    return version_id


def register_resources(s):
    s.register_resource("set-hq", "set", "横琴A棚", "Asia/Macau",
                        team_id="team-sea", market="sea")
    s.register_resource("actor-li", "actor", "李安娜", "Asia/Shanghai",
                        team_id="team-sea")
    s.register_resource("team-la", "team", "洛杉矶摄制组", "America/Los_Angeles",
                        team_id="team-na", market="na")


def book_and_shoot(s, market="sea", project_id="prj-1", version_id="v-2",
                   resource_id="set-hq", tz_window=("2026-03-01T02:00:00Z",
                                                    "2026-03-01T10:00:00Z")):
    bid = f"book-{market}-1"
    s.book(bid, project_id, market, resource_id,
           tz_window[0], tz_window[1], "正片拍摄", f"pm-{market}")
    shot_id = f"shot-{market}-1"
    s.capture_shot(shot_id, project_id, market, bid, "sc-1",
                   "novel-ch1", f"cam-{market}")
    return bid, shot_id


def build_asset_tree(s, market="sea", project_id="prj-1", shot_id=None,
                     publish=False, platform="streamix"):
    shot_id = shot_id or f"shot-{market}-1"
    rough = f"ast-{market}-rough"
    sub = f"ast-{market}-sub"
    dub = f"ast-{market}-dub"
    fmt = f"ast-{market}-fmt"
    s.create_asset(rough, project_id, market, "rough_cut", f"editor-{market}",
                   source_shot_ids=[shot_id])
    s.create_asset(sub, project_id, market, "subtitle", f"loc-{market}",
                   parent_asset_id=rough, language="en")
    s.create_asset(dub, project_id, market, "dub", f"loc-{market}",
                   parent_asset_id=rough, language="en")
    s.create_asset(fmt, project_id, market, "market_format", f"pm-{market}",
                   parent_asset_id=dub, language="en")
    rel_id = None
    if publish:
        rel = s.publish(fmt, platform, f"dist-{market}")
        rel_id = rel["release_id"]
    return {"rough_cut": rough, "subtitle": sub, "dub": dub,
            "market_format": fmt, "release_id": rel_id}


def full_pipeline(s, markets=("na", "sea"), publish=("sea",),
                  post_receipts=True):
    """端到端样例；publish 中的市场会实际发行，默认再回一笔回执。"""
    seed_project(s, markets=markets)
    vid = approved_script(s, markets=markets)
    register_resources(s)
    trees = {}
    windows = {
        "sea": ("2026-03-01T02:00:00Z", "2026-03-01T10:00:00Z"),
        "na": ("2026-03-02T14:00:00Z", "2026-03-02T22:00:00Z"),
    }
    resources = {"sea": "set-hq", "na": "team-la"}
    for m in markets:
        bid, shot_id = book_and_shoot(
            s, market=m, resource_id=resources.get(m, "set-hq"),
            tz_window=windows.get(m, windows["sea"]))
        trees[m] = build_asset_tree(s, market=m, shot_id=shot_id,
                                    publish=(m in publish))
        if m in publish and post_receipts:
            s.post_receipt(
                "prj-1", trees[m]["release_id"], f"rcp-{m}-001",
                "12000.00", "USD", "2026-03", f"idem-{m}-001",
                f"dist-{m}")
    return trees
