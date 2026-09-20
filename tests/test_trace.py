"""权利争议反查测试。"""

import unittest

from tests.helpers import full_pipeline


class TraceTest(unittest.TestCase):
    def setUp(self):
        from studio import ProductionSystem
        self.s = ProductionSystem()
        full_pipeline(self.s, markets=("na", "sea"), publish=("sea",))

    def test_trace_release_reconstructs_plot_approvers_rights_timezone(self):
        report = self.s.trace_release("rel-ast-sea-fmt-streamix")
        self.assertEqual(report["scope"], "release")
        self.assertEqual(report["market"], "sea")
        # 采用的剧情
        version = report["adopted_versions"][0]
        self.assertEqual(version["version_id"], "v-2")
        summaries = [i["summary"] for i in version["plot_items"]]
        self.assertTrue(any("医院" in x for x in summaries))
        # 三方批准人与下发人
        self.assertEqual(version["approvers"]["rights"], "rights-lin")
        self.assertEqual(version["approvers"]["compliance"], "comp-zhou")
        self.assertEqual(version["approvers"]["production"], "prod-chen")
        self.assertEqual(version["release_to_market"]["by"], "prod-chen")
        # 本地化意见与剧情改动随版本可查
        kinds = {a["kind"] for a in version["annotations"]}
        self.assertEqual(kinds, {"localization", "plot_change"})
        # 权利条款与窗口
        rights = report["rights"]
        self.assertEqual(rights["market_lock"]["status"], "active")
        self.assertEqual(rights["grant_terms"][0]["grant_id"], "grant-sea")
        self.assertEqual(rights["grant_terms"][0]["window"][1],
                         "2027-12-31T23:59:59Z")
        # 团队时区（sea 用的是澳门棚）
        tz = next(iter(report["team_timezones"].values()))
        self.assertEqual(tz["timezone"], "Asia/Macau")
        # 制作事件链覆盖关键动作
        types = {e["type"] for e in report["production_events"]}
        self.assertIn("script.approved", types)
        self.assertIn("shot.captured", types)
        self.assertIn("asset.published", types)

    def test_trace_shot_keeps_terms_at_shoot_after_revocation(self):
        self.s.revoke_market_rights(
            "rights-lin", "事后争议", ip_id="ip-1", market="sea")
        report = self.s.trace_shot("shot-sea-1")
        # 拍摄时固化的条款仍在
        self.assertEqual(
            report["rights"]["window_at_shoot"][0], "2026-02-01T00:00:00Z")
        self.assertEqual(
            report["rights"]["grant_terms_at_shoot"][0]["grant_id"],
            "grant-sea")
        # 当前状态标注为已撤销，便于判断争议是否成立
        current = report["rights"]["current"]
        self.assertEqual(current["market_lock"]["status"], "revoked")
        self.assertEqual(current["grant_terms"][0]["status"], "revoked")
        self.assertEqual(current["grant_terms"][0]["revoke_reason"], "事后争议")
        # 批准人与剧情不丢
        self.assertEqual(report["approvers"]["compliance"], "comp-zhou")
        self.assertIn("医院", report["adopted_plot"]["item"]["summary"])

    def test_trace_market_lists_all_versions_shots_and_lineage(self):
        report = self.s.trace_market("prj-1", "na")
        self.assertEqual({s["shot_id"] for s in report["shots"]},
                         {"shot-na-1"})
        self.assertEqual(report["materials_used"], ["novel-ch1"])
        self.assertIsNone(report["asset_lineage"])  # 市场级反查不挂单资产

    def test_trace_asset_lineage_order(self):
        report = self.s.trace_asset("ast-sea-sub")
        types = [a["type"] for a in report["asset_lineage"]]
        self.assertEqual(types, ["subtitle", "rough_cut"])


if __name__ == "__main__":
    unittest.main()
