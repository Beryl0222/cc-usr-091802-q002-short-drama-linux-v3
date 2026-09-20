"""派生谱系、撤权级联与幂等回执测试。"""

import threading
import unittest

from studio import ConflictError, ProductionSystem, StateError
from tests.helpers import (
    book_and_shoot,
    build_asset_tree,
    full_pipeline,
    register_resources,
    seed_project,
    approved_script,
)


class LineageTest(unittest.TestCase):
    def setUp(self):
        self.s = ProductionSystem()
        seed_project(self.s)
        approved_script(self.s)
        register_resources(self.s)
        book_and_shoot(self.s, market="sea")

    def test_lineage_chains_back_to_rough_cut_and_shot(self):
        tree = build_asset_tree(self.s, market="sea")
        lineage = self.s.lineage(tree["market_format"])
        types = [a["type"] for a in lineage["ancestors"]]
        self.assertEqual(types, ["market_format", "dub", "rough_cut"])
        self.assertIn("shot-sea-1", lineage["source_shots"])
        self.assertEqual(
            lineage["source_shots"]["shot-sea-1"]["version_id"], "v-2")

    def test_illegal_derivation_rejected(self):
        tree = build_asset_tree(self.s, market="sea")
        with self.assertRaises(ConflictError):
            # 字幕只能派生自粗剪，不能挂到市场版式下
            self.s.create_asset(
                "ast-bad", "prj-1", "sea", "subtitle", "loc",
                parent_asset_id=tree["market_format"], language="en")
        with self.assertRaises(Exception):
            # 粗剪不能有父资产
            self.s.create_asset(
                "ast-bad2", "prj-1", "sea", "rough_cut", "ed",
                parent_asset_id=tree["rough_cut"],
                source_shot_ids=["shot-sea-1"])

    def test_subtitle_requires_locked_language(self):
        self.s.create_asset("r", "prj-1", "sea", "rough_cut", "ed",
                            source_shot_ids=["shot-sea-1"])
        with self.assertRaises(ConflictError):
            self.s.create_asset("sub-th", "prj-1", "sea", "subtitle", "loc",
                                parent_asset_id="r", language="th")


class RevocationCascadeTest(unittest.TestCase):
    def setUp(self):
        self.s = ProductionSystem()
        full_pipeline(self.s, markets=("na", "sea"), publish=("sea",))

    def test_revocation_blocks_unreleased_keeps_released_and_other_markets(self):
        # na 全部未发行；sea 的 market_format 已发行，其余为未发行中间产物
        result = self.s.revoke_market_rights(
            "rights-lin", "东南亚权利争议", ip_id="ip-1", market="na")
        affected = result["affected_projects"][0]
        self.assertEqual(set(affected["blocked_asset_ids"]),
                         {"ast-na-rough", "ast-na-sub", "ast-na-dub",
                          "ast-na-fmt"})
        self.assertEqual(affected["recalled_release_ids"], [])

        # sea 的已发行版本：召回但不删资产、不删回执
        result = self.s.revoke_market_rights(
            "rights-lin", "东南亚权利争议", ip_id="ip-1", market="sea")
        sea = result["affected_projects"][0]
        self.assertIn("rel-ast-sea-fmt-streamix", sea["recalled_release_ids"])
        self.assertNotIn("ast-sea-fmt", sea["blocked_asset_ids"])
        # 已发行的市场版式资产本体保留
        fmt = self.s.store.require("assets", "ast-sea-fmt", "资产")
        self.assertNotEqual(fmt["status"], "blocked")
        # 历史回执保留，账目仍可查
        account = self.s.market_account("prj-1", "sea")
        self.assertEqual(account["entry_count"], 1)
        self.assertTrue(account["frozen"])

    def test_blocked_asset_cannot_be_published_or_derived(self):
        self.s.revoke_market_rights(
            "rights-lin", "争议", ip_id="ip-1", market="na")
        with self.assertRaises(StateError):
            self.s.publish("ast-na-fmt", "streamix", "dist")
        with self.assertRaises(StateError):
            self.s.create_asset(
                "ast-na-x", "prj-1", "na", "market_format", "pm",
                parent_asset_id="ast-na-dub", language="en")

    def test_other_market_assets_untouched(self):
        self.s.revoke_market_rights(
            "rights-lin", "争议", ip_id="ip-1", market="na")
        sea_assets = self.s.store.list(
            "assets", lambda a: a["market"] == "sea")
        self.assertTrue(sea_assets)
        self.assertTrue(all(a["status"] == "draft" for a in sea_assets
                            if a["asset_id"] != "ast-sea-fmt"))
        # sea 仍可正常发布与制作
        rel = self.s.publish("ast-sea-dub", "shorttv", "dist-sea")
        self.assertEqual(rel["market"], "sea")

    def test_no_new_asset_after_revocation(self):
        self.s.revoke_market_rights(
            "rights-lin", "争议", ip_id="ip-1", market="na")
        with self.assertRaises(StateError):
            self.s.create_asset(
                "ast-new", "prj-1", "na", "rough_cut", "ed",
                source_shot_ids=["shot-na-1"])

    def test_republish_after_recall_rejected(self):
        self.s.revoke_market_rights(
            "rights-lin", "争议", ip_id="ip-1", market="sea")
        with self.assertRaises(StateError):
            self.s.publish("ast-sea-fmt", "streamix", "dist")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.s = ProductionSystem()
        full_pipeline(self.s, markets=("sea",), publish=("sea",),
                      post_receipts=False)
        self.release_id = "rel-ast-sea-fmt-streamix"

    def _post(self, idem_key, receipt_no="rcp-sea-001", amount="12000.00"):
        return self.s.post_receipt(
            "prj-1", self.release_id, receipt_no, amount, "USD",
            "2026-03", idem_key, "dist-sea")

    def test_duplicate_idempotency_key_posted_once(self):
        first = self._post("dup-key")
        self.assertFalse(first["deduplicated"])
        second = self._post("dup-key")
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["entry_id"], first["entry_id"])
        account = self.s.market_account("prj-1", "sea")
        self.assertEqual(account["entry_count"], 1)  # helper 已入一笔
        self.assertEqual(account["totals"][0]["amount"], "12000.00")

    def test_late_duplicate_receipt_number_deduped_even_new_key(self):
        self._post("key-1")
        # 迟到的同一回执，换了幂等键：仍只能入账一次
        again = self._post("key-2-late")
        self.assertTrue(again["deduplicated"])
        self.assertEqual(again["matched_by"], "receipt_no")

    def test_distinct_receipts_accumulate(self):
        self._post("key-1")  # rcp-sea-001 12000.00
        self.s.post_receipt(
            "prj-1", self.release_id, "rcp-sea-002", "800.50", "USD",
            "2026-04", "idem-002", "dist-sea")
        account = self.s.market_account("prj-1", "sea")
        self.assertEqual(account["entry_count"], 2)
        self.assertEqual(account["totals"][0]["amount"], "12800.50")

    def test_concurrent_duplicate_receipts_post_once(self):
        errors, winners = [], []

        def post():
            try:
                r = self.s.post_receipt(
                    "prj-1", self.release_id, "RCP-CONC-1", "500", "USD",
                    "2026-05", "idem-conc-1", "dist-sea")
                winners.append(r)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=post) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        real = [w for w in winners if not w["deduplicated"]]
        self.assertEqual(len(real), 1)
        self.assertEqual(len(winners), 10)  # 其余 9 笔拿到同一首账
        account = self.s.market_account("prj-1", "sea")
        conc_entries = [e for e in account["entries"]
                        if e["receipt_no"] == "RCP-CONC-1"]
        self.assertEqual(len(conc_entries), 1)

    def test_accounts_frozen_market_does_not_affect_others(self):
        s = ProductionSystem()
        full_pipeline(s, markets=("na", "sea"), publish=("na", "sea"))
        s.revoke_market_rights("rights-lin", "争议", ip_id="ip-1", market="na")
        accounts = s.all_accounts("prj-1")
        self.assertTrue(accounts["na"]["frozen"])
        self.assertFalse(accounts["sea"]["frozen"])
        # 召回后 na 不能再入账
        with self.assertRaises(StateError):
            s.post_receipt(
                "prj-1", accounts["na"]["entries"][0]["release_id"],
                "rcp-na-late", "10", "USD", "2026-04", "idem-na-late",
                "dist-na")
        # sea 不受影响
        sea_rel = [e["release_id"] for e in accounts["sea"]["entries"]][0]
        ok = s.post_receipt(
            "prj-1", sea_rel, "rcp-sea-009", "10", "USD", "2026-04",
            "idem-sea-009", "dist-sea")
        self.assertFalse(ok["deduplicated"])


if __name__ == "__main__":
    unittest.main()
