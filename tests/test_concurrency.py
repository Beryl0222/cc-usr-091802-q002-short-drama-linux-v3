"""撤权与发布/入账并发竞争下的一致性压力测试。

不变量：撤权与发布在同一把仓储锁内串行，因此撤权完成后该市场不存在
"已发行且仍有效"的发布；每笔已存在的发布要么 recalled（发布先发生），
要么根本没创建（撤权先发生，发布被拒）。
"""

import threading
import unittest
from collections import Counter

from studio import DomainError
from tests.helpers import (
    book_and_shoot,
    build_asset_tree,
    register_resources,
    seed_project,
    approved_script,
)
from studio.models import RELEASE_RECALLED, RELEASE_RELEASED


def _build_market_ready(s, market):
    seed_project(s, markets=(market,))
    approved_script(s, version_id=f"v-{market}", markets=(market,))
    register_resources(s)
    rid = "set-hq" if market == "sea" else "team-la"
    window = ("2026-03-01T02:00:00Z", "2026-03-01T10:00:00Z")
    _, shot_id = book_and_shoot(s, market=market, version_id=f"v-{market}",
                                resource_id=rid, tz_window=window)
    tree = build_asset_tree(s, market=market, shot_id=shot_id)
    return tree


class RevokePublishRaceTest(unittest.TestCase):
    def test_revoke_vs_many_publishers_consistent(self):
        from studio import ProductionSystem
        s = ProductionSystem()
        tree = _build_market_ready(s, "sea")
        outcomes = Counter()
        errors = []
        barrier = threading.Barrier(13)

        def publish(i):
            barrier.wait()
            try:
                s.publish(tree["market_format"], f"streamix-{i}", f"dist-{i}")
                outcomes["published"] += 1
            except DomainError as exc:
                outcomes[exc.code] += 1
                errors.append(exc)

        def revoke():
            barrier.wait()
            s.revoke_market_rights("rights-lin", "权利争议",
                                   ip_id="ip-1", market="sea")
            outcomes["revoked"] += 1

        # 多个发布者与撤权同时被释放；同一 asset 每个平台只发一次，
        # 撤权先到则发布被拒，发布先到则随撤权被召回
        threads = [threading.Thread(target=publish, args=(i,))
                   for i in range(12)]
        revoker = threading.Thread(target=revoke)
        for t in threads:
            t.start()
        revoker.start()
        for t in threads:
            t.join()
        revoker.join()

        self.assertEqual(outcomes["revoked"], 1)

        # 一致性断言：该市场所有发布记录最终都是 recalled，不存在仍 active 的
        releases = s.store.list("releases", lambda r: r["market"] == "sea")
        statuses = {r["status"] for r in releases}
        self.assertTrue(statuses <= {RELEASE_RECALLED})

        # 所有已成功发布随后都被召回；撤权后的发布则被拒绝，无漏网 active
        self.assertEqual(outcomes["published"],
                         sum(1 for r in releases
                             if r["status"] == RELEASE_RECALLED))
        self.assertEqual(
            12, outcomes["published"]
            + outcomes.get("state_error", 0) + outcomes.get("conflict", 0))

        # 资产本体仍在（未误删）
        fmt = s.store.require("assets", tree["market_format"], "资产")
        self.assertNotEqual(fmt["status"], "deleted")

    def test_cross_market_accounts_consistent_under_chaos(self):
        """sea 撤权混乱进行时，na 的账目不受影响且金额精确。"""
        from studio import ProductionSystem
        s = ProductionSystem()
        seed_project(s, markets=("na", "sea"))
        approved_script(s, markets=("na", "sea"))
        register_resources(s)
        _, sea_shot = book_and_shoot(
            s, market="sea", resource_id="set-hq",
            tz_window=("2026-03-01T02:00:00Z", "2026-03-01T10:00:00Z"))
        _, na_shot = book_and_shoot(
            s, market="na", resource_id="team-la",
            tz_window=("2026-03-02T14:00:00Z", "2026-03-02T22:00:00Z"))
        sea_tree = build_asset_tree(s, market="sea", shot_id=sea_shot,
                                    publish=True)
        na_tree = build_asset_tree(s, market="na", shot_id=na_shot,
                                   publish=True)
        sea_rel, na_rel = sea_tree["release_id"], na_tree["release_id"]

        barrier = threading.Barrier(8)

        def chaos_revoke_sea():
            barrier.wait()
            s.revoke_market_rights("rights-lin", "争议",
                                   ip_id="ip-1", market="sea")

        def chaos_receipts_na(worker):
            barrier.wait()
            for i in range(20):
                idx = worker * 20 + i
                s.post_receipt(
                    "prj-1", na_rel, f"NA-{idx:03d}", "100.00", "USD",
                    f"2026-{(idx % 12) + 1:02d}", f"na-key-{idx}", "dist-na")

        def chaos_dup_receipts_sea(worker):
            barrier.wait()
            for _ in range(20):
                try:
                    s.post_receipt(
                        "prj-1", sea_rel, "SEA-DUP", "7.00", "USD",
                        "2026-03", "sea-key-dup", "dist-sea")
                except DomainError:
                    pass

        threads = ([threading.Thread(target=chaos_revoke_sea)]
                   + [threading.Thread(target=chaos_receipts_na, args=(w,))
                      for w in range(3)]
                   + [threading.Thread(target=chaos_dup_receipts_sea,
                                      args=(w,))
                      for w in range(4)])
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        na_account = s.market_account("prj-1", "na")
        # 3 个线程各 20 笔、单号 NA-000..NA-019，全部不同键 → 60 笔，无串市
        self.assertEqual(na_account["entry_count"], 60)
        self.assertEqual(na_account["totals"][0]["amount"], "6000.00")
        self.assertFalse(na_account["frozen"])

        sea_account = s.market_account("prj-1", "sea")
        # 撤权可能先于或晚于首笔重复回执：sea 至多 1 笔
        self.assertLessEqual(sea_account["entry_count"], 1)
        self.assertTrue(sea_account["frozen"])
        # 没有任何账目串到错误市场
        self.assertTrue(all(e["market"] == "na" for e in na_account["entries"]))
        self.assertTrue(all(e["market"] == "sea" for e in sea_account["entries"]))
        # 全部发布状态最终一致
        na_release = s.store.require("releases", na_rel, "发布")
        sea_release = s.store.require("releases", sea_rel, "发布")
        self.assertEqual(na_release["status"], RELEASE_RELEASED)
        self.assertEqual(sea_release["status"], RELEASE_RECALLED)


if __name__ == "__main__":
    unittest.main()
