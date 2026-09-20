"""海外制片协作后端全量契约测试。"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.client import RemoteDisconnected
from http.server import ThreadingHTTPServer
from pathlib import Path

from production.api import make_handler
from production.backend import Backend
from production.errors import (ConflictState, SchedulingConflict,
                               ValidationError, VersionConflict)

W_2026 = ("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.b = Backend(":memory:")
        self._bootstrap()

    # ---------- 夹具：一个走完 IP→授权→锁定剧本→档期→镜头的项目 ----------
    def _bootstrap(self, *, markets=("north-america", "southeast-asia"),
                   materials=("m#ch1-10", "m#art01")):
        b = self.b
        b.register_ip("网文原作", "版权方", ip_id="ip1")
        p = b.create_project("ip1", "改编剧", list(markets), list(materials),
                             project_id="p1")
        b.create_team("team_us", "北美团队", "America/Los_Angeles")
        b.create_set("set_a", "A 棚", "Asia/Macau")
        b.create_talent("tal_maria", "Maria")
        b.assign_team("p1", "team_us")
        b.grant_license("p1", "north-america", "en", *W_2026)
        return p

    def _locked_version(self, project_id="p1", scenes=None, cultural_notes=1):
        b = self.b
        scenes = scenes or [
            {"scene_ref": "S1", "summary": "初遇", "material_refs": ["m#ch1-10"]}]
        head = b.list_versions(project_id)
        base = head[-1]["version_id"] if head else None
        v = b.create_draft(project_id, scenes, "writer",
                           based_on_version_id=base)
        for i in range(cultural_notes):
            ref = scenes[0]["scene_ref"]
            note = b.add_note(v["version_id"], f"顾问{i}", "local_consultant",
                              "cultural", f"文化意见{i}", scene_ref=ref)
            b.add_change(v["version_id"], ref, "localization", f"本地化改动{i}",
                         source_note_id=note["note_id"])
        b.submit_approval(v["version_id"], "rights", "rights_boss", "approved")
        b.submit_approval(v["version_id"], "compliance", "legal_boss", "approved")
        out = b.submit_approval(v["version_id"], "production", "prod_boss",
                                "approved")
        self.assertTrue(out["gate"]["locked"])
        return out

    def _shot_pipeline(self, day=1):
        v = self._locked_version()
        bk = self.b.create_booking(
            "p1", "S1", v["version_id"], team_id="team_us", set_id="set_a",
            talent_ids=["tal_maria"],
            starts_at=f"2026-03-{day:02d}T10:00:00Z",
            ends_at=f"2026-03-{day:02d}T11:00:00Z")
        shot = self.b.record_shot(bk["booking_id"], ["m#ch1-10"])
        return v, bk, shot

    def _market_format(self, cut, language, market, hash_suffix):
        """粗剪 → 字幕 → 市场版式的标准派生。"""
        sub = self.b.create_asset(
            "p1", "subtitle", f"hsub_{hash_suffix}", "ed",
            parent_asset_id=cut["asset_id"], language=language)
        return self.b.create_asset(
            "p1", "market_format", hash_suffix, "d",
            parent_asset_id=sub["asset_id"], language=language, market=market)


class TestIPAndLicenses(BackendTest):
    def test_project_locks_markets_and_material_boundary(self):
        with self.assertRaises(ValidationError):
            self.b.grant_license("p1", "europe", "en", *W_2026)
        with self.assertRaises(ValidationError):
            self.b.create_project("ip1", "另一部", ["north-america"], [],
                                  project_id="p2")

    def test_duplicate_license_conflicts_and_regrant_after_revoke(self):
        with self.assertRaises(ConflictState):
            self.b.grant_license("p1", "north-america", "en", *W_2026)
        lic = self.b.list_licenses("p1")[0]
        self.b.revoke_license(lic["license_id"], "调整")
        new = self.b.grant_license("p1", "north-america", "en", *W_2026)
        self.assertEqual(new["status"], "active")

    def test_window_must_be_ordered(self):
        with self.assertRaises(ValidationError):
            self.b.grant_license("p1", "southeast-asia", "th",
                                 "2027-06-01T00:00:00Z", "2026-01-01T00:00:00Z")

    def test_restricted_material_must_lie_inside_boundary(self):
        with self.assertRaises(ValidationError):
            self.b.grant_license("p1", "southeast-asia", "th", *W_2026,
                                 restricted_materials=["not-in-boundary"])

    def test_release_outside_window_rejected(self):
        v, _bk, shot = self._shot_pipeline()
        cut = self.b.create_asset("p1", "rough_cut", "h_cut", "ed",
                                  version_id=v["version_id"],
                                  root_shot_ids=[shot["shot_id"]])
        fmt = self._market_format(cut, "en", "north-america", "h_fmt_win")
        with self.assertRaises(ConflictState):
            self.b.schedule_release(
                "p1", fmt["asset_id"], "north-america", "en", "netflix",
                "2027-06-01T00:00:00Z")  # 授权 2027-01-01 到期


class TestScriptGate(BackendTest):
    def test_drafts_are_sequenced_and_deterministically_hashed(self):
        a = self.b.create_draft("p1", [{"scene_ref": "S1"}], "w")
        b1 = self.b.create_draft("p1", [{"scene_ref": "S1", "summary": "x"}],
                                 "w", based_on_version_id=a["version_id"])
        b2 = self.b.create_draft("p1", [{"scene_ref": "S1", "summary": "x"}],
                                 "w", based_on_version_id=b1["version_id"])
        self.assertEqual([a["seq"], b1["seq"], b2["seq"]], [1, 2, 3])
        # 相同内容哈希一致
        self.assertEqual(b1["content_hash"], b2["content_hash"])

    def test_concurrent_edits_raise_version_conflict(self):
        a = self.b.create_draft("p1", [{"scene_ref": "S1"}], "w")
        self.b.create_draft("p1", [{"scene_ref": "S1", "summary": "2"}],
                            "w2", based_on_version_id=a["version_id"])
        with self.assertRaises(VersionConflict) as ctx:
            self.b.create_draft("p1", [{"scene_ref": "S1", "summary": "3"}],
                                "w3", based_on_version_id=a["version_id"])
        self.assertEqual(ctx.exception.details["current_seq"], 2)

    def test_note_change_linkage_and_one_shot_resolution(self):
        v = self.b.create_draft("p1", [{"scene_ref": "S1"}], "w")
        note = self.b.add_note(v["version_id"], "顾问", "local_consultant",
                               "cultural", "意见", scene_ref="S1")
        chg = self.b.add_change(v["version_id"], "S1", "localization", "改动",
                                source_note_id=note["note_id"])
        self.assertEqual(chg["source_note_id"], note["note_id"])
        with self.assertRaises(ConflictState):
            self.b.add_change(v["version_id"], "S1", "plot", "再次处理同一意见",
                              source_note_id=note["note_id"])

    def test_rights_approval_blocks_material_outside_boundary(self):
        v = self.b.create_draft(
            "p1", [{"scene_ref": "S1", "material_refs": ["m#ch1-10", "越界素材"]}],
            "w")
        with self.assertRaises(ConflictState) as ctx:
            self.b.submit_approval(v["version_id"], "rights", "r", "approved")
        self.assertEqual(ctx.exception.details["blockers"][0]["rule"],
                         "material_outside_boundary")

    def test_compliance_approval_blocks_unresolved_compliance_note(self):
        v = self.b.create_draft("p1", [{"scene_ref": "S1"}], "w")
        self.b.add_note(v["version_id"], "审片", "censor", "compliance",
                        "暴力镜头", scene_ref="S1")
        with self.assertRaises(ConflictState) as ctx:
            self.b.submit_approval(v["version_id"], "compliance", "c", "approved")
        self.assertEqual(ctx.exception.details["blockers"][0]["rule"],
                         "unresolved_compliance_note")

    def test_production_approval_requires_cultural_notes_resolved(self):
        v = self.b.create_draft("p1", [{"scene_ref": "S1"}], "w")
        self.b.add_note(v["version_id"], "本地顾问", "local_consultant",
                        "cultural", "习俗需调整", scene_ref="S1")
        with self.assertRaises(ConflictState):
            self.b.submit_approval(v["version_id"], "production", "p", "approved")

    def test_rejection_prevents_lock_and_locked_version_is_immutable(self):
        v = self.b.create_draft("p1", [{"scene_ref": "S1"}], "w")
        self.b.submit_approval(v["version_id"], "rights", "r", "rejected")
        self.b.submit_approval(v["version_id"], "compliance", "c", "approved")
        self.b.submit_approval(v["version_id"], "production", "p", "approved")
        self.assertFalse(self.b.gate_status(v["version_id"])["locked"])
        locked = self._locked_version()
        with self.assertRaises(ConflictState):
            self.b.add_note(locked["version_id"], "x", "local_consultant",
                            "cultural", "锁后不能加意见")

    def test_lock_snapshots_active_licenses(self):
        v = self._locked_version()
        snap_markets = {(s["market"], s["language"]) for s in v["license_snapshot"]}
        self.assertIn(("north-america", "en"), snap_markets)


class TestScheduling(BackendTest):
    def test_unlocked_script_cannot_be_scheduled(self):
        v = self.b.create_draft("p1", [{"scene_ref": "S1"}], "w")
        with self.assertRaises(ConflictState):
            self.b.create_booking(
                "p1", "S1", v["version_id"], team_id="team_us", set_id="set_a",
                talent_ids=["tal_maria"], starts_at="2026-03-01T10:00:00Z",
                ends_at="2026-03-01T11:00:00Z")

    def test_conflict_reports_reasons_per_resource_with_timezones(self):
        v = self._locked_version()
        kwargs = dict(team_id="team_us", set_id="set_a",
                      talent_ids=["tal_maria"],
                      starts_at="2026-03-01T10:00:00Z",
                      ends_at="2026-03-01T11:00:00Z")
        self.b.create_booking("p1", "S1", v["version_id"], **kwargs)
        with self.assertRaises(SchedulingConflict) as ctx:
            self.b.create_booking("p1", "S1", v["version_id"], **{
                **kwargs, "starts_at": "2026-03-01T10:30:00Z",
                "ends_at": "2026-03-01T12:00:00Z"})
        kinds = {r["resource_kind"] for r in ctx.exception.reasons}
        self.assertEqual(kinds, {"set", "team", "talent"})
        set_reason = next(r for r in ctx.exception.reasons
                          if r["resource_kind"] == "set")
        self.assertEqual(set_reason["resource_timezone"], "Asia/Macau")
        self.assertTrue(set_reason["overlap_minutes_utc"])
        # 本地时间换算正确：UTC+8 的澳门 18:30
        self.assertIn("2026-03-01T18:30:00+08:00",
                      set_reason["requested_local_start"])

    def test_non_overlapping_bookings_coexist(self):
        v = self._locked_version()
        self.b.create_booking(
            "p1", "S1", v["version_id"], team_id="team_us", set_id="set_a",
            talent_ids=["tal_maria"], starts_at="2026-03-01T10:00:00Z",
            ends_at="2026-03-01T11:00:00Z")
        bk2 = self.b.create_booking(
            "p1", "S1", v["version_id"], team_id="team_us", set_id="set_a",
            talent_ids=["tal_maria"], starts_at="2026-03-01T11:00:00Z",
            ends_at="2026-03-01T12:00:00Z")
        self.assertEqual(bk2["status"], "confirmed")

    def test_cancel_after_shot_is_forbidden(self):
        _v, bk, _shot = self._shot_pipeline()
        with self.assertRaises(ConflictState):
            self.b.cancel_booking(bk["booking_id"], "改期")


class TestShotProvenance(BackendTest):
    def test_shot_pins_version_even_after_script_updates(self):
        v, _bk, shot = self._shot_pipeline()
        v2 = self.b.create_draft(
            "p1", [{"scene_ref": "S1", "summary": "重拍版",
                    "material_refs": ["m#ch1-10"]}], "writer2",
            based_on_version_id=v["version_id"])
        self.assertEqual(v2["seq"], 2)
        stored = self.b.get_shot(shot["shot_id"])
        self.assertEqual(stored["provenance"]["version_seq"], 1)
        self.assertEqual(stored["provenance"]["version_id"], v["version_id"])
        self.assertTrue(stored["immutable"])

    def test_shot_material_must_belong_to_locked_scene(self):
        v, bk, _shot = self._shot_pipeline()
        with self.assertRaises(ValidationError):
            self.b.record_shot(bk["booking_id"], ["m#art01"])


class TestLineageAndRelease(BackendTest):
    def _released(self, *, market="north-america", language="en",
                  platform="netflix", when="2026-04-01T00:00:00Z",
                  hash_suffix="us", day=1, publish=True):
        v, _bk, shot = self._shot_pipeline(day=day)
        cut = self.b.create_asset(
            "p1", "rough_cut", f"h_cut_{hash_suffix}", "ed",
            version_id=v["version_id"], root_shot_ids=[shot["shot_id"]])
        sub = self.b.create_asset(
            "p1", "subtitle", f"h_sub_{hash_suffix}", "ed",
            parent_asset_id=cut["asset_id"], language=language)
        dub = self.b.create_asset(
            "p1", "dub", f"h_dub_{hash_suffix}", "studio",
            parent_asset_id=cut["asset_id"], language=language)
        fmt = self.b.create_asset(
            "p1", "market_format", f"fmt_{hash_suffix}", "d",
            parent_asset_id=dub["asset_id"], language=language, market=market)
        rel = self.b.schedule_release(
            "p1", fmt["asset_id"], market, language, platform, when)
        out = self.b.publish_release(rel["release_id"]) if publish else rel
        return v, cut, sub, dub, fmt, out

    def test_lineage_rules_enforced(self):
        v, _bk, shot = self._shot_pipeline()
        with self.assertRaises(ValidationError):
            self.b.create_asset("p1", "rough_cut", "h", "ed",
                                parent_asset_id="x", version_id=v["version_id"])
        cut = self.b.create_asset("p1", "rough_cut", "h_cut", "ed",
                                  version_id=v["version_id"],
                                  root_shot_ids=[shot["shot_id"]])
        # 市场版式不能直接派生粗剪之外无语言声明……这里测非法父类型：
        sub = self.b.create_asset("p1", "subtitle", "h_sub", "ed",
                                  parent_asset_id=cut["asset_id"], language="en")
        with self.assertRaises(ValidationError):
            self.b.create_asset("p1", "dub", "h_dub_bad", "s",
                                parent_asset_id=sub["asset_id"], language="en")
        lineage = self.b.asset_lineage(sub["asset_id"])
        self.assertEqual([a["kind"] for a in lineage["chain"]],
                         ["rough_cut", "subtitle"])
        self.assertEqual(lineage["root_shots"][0]["shot_id"], shot["shot_id"])

    def test_rough_cut_rejects_shot_from_other_version(self):
        v, _bk, shot = self._shot_pipeline()
        # 锁定后产生下一版本（并发改稿走新草稿，意见只能挂在新草稿上）
        v2draft = self.b.create_draft(
            "p1", [{"scene_ref": "S1", "summary": "v2",
                    "material_refs": ["m#ch1-10"]}], "w",
            based_on_version_id=v["version_id"])
        note = self.b.add_note(v2draft["version_id"], "顾问",
                               "local_consultant", "cultural", "意见2",
                               scene_ref="S1")
        self.b.add_change(v2draft["version_id"], "S1", "localization", "改2",
                          source_note_id=note["note_id"])
        for role, who in [("rights", "r"), ("compliance", "c"),
                          ("production", "p")]:
            self.b.submit_approval(v2draft["version_id"], role, who, "approved")
        with self.assertRaises(ConflictState):
            self.b.create_asset("p1", "rough_cut", "h_mix", "ed",
                                version_id=v2draft["version_id"],
                                root_shot_ids=[shot["shot_id"]])

    def test_revoke_blocks_only_unreleased_same_market(self):
        _v, _cut, _sub, _dub, fmt1, rel1 = self._released(
            platform="netflix", hash_suffix="us1")
        _v2, _c2, _s2, _d2, fmt2, rel2_unpub = self._released(
            platform="youtube", hash_suffix="us2", day=2,
            when="2026-05-01T00:00:00Z", publish=False)
        self.assertEqual(rel2_unpub["status"], "scheduled")
        # 东南亚市场版本不受影响
        self.b.grant_license("p1", "southeast-asia", "th", *W_2026)
        lic = next(l for l in self.b.list_licenses("p1")
                   if l["market"] == "north-america" and l["language"] == "en")
        result = self.b.revoke_license(lic["license_id"], "权利争议")
        self.assertEqual(result["blocked_releases"], 1)
        self.assertEqual(self.b.get_release(rel1["release_id"])["status"],
                         "released")
        blocked = self.b.get_release(rel2_unpub["release_id"])
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("权利争议", blocked["blocked_reason"])
        # 资产不被删除
        self.assertGreaterEqual(len(self.b.list_assets("p1")), 8)
        with self.assertRaises(ConflictState):
            self.b.publish_release(rel2_unpub["release_id"])

    def test_market_specific_restricted_material_blocks_release(self):
        self.b.grant_license("p1", "southeast-asia", "th", *W_2026,
                             restricted_materials=["m#art01"])
        v = self._locked_version(
            scenes=[{"scene_ref": "S1", "material_refs":
                     ["m#ch1-10", "m#art01"]}])
        bk = self.b.create_booking(
            "p1", "S1", v["version_id"], team_id="team_us", set_id="set_a",
            talent_ids=["tal_maria"], starts_at="2026-03-02T10:00:00Z",
            ends_at="2026-03-02T11:00:00Z")
        shot = self.b.record_shot(bk["booking_id"],
                                  ["m#ch1-10", "m#art01"])
        cut = self.b.create_asset("p1", "rough_cut", "h_c", "e",
                                  version_id=v["version_id"],
                                  root_shot_ids=[shot["shot_id"]])
        fmt = self._market_format(cut, "th", "southeast-asia", "h_f_th")
        with self.assertRaises(ConflictState) as ctx:
            self.b.schedule_release(
                "p1", fmt["asset_id"], "southeast-asia", "th", "line",
                "2026-04-01T00:00:00Z")
        self.assertIn("violations", ctx.exception.details)


class TestReceiptsAndAccounts(BackendTest):
    def _release(self):
        v, _bk, shot = self._shot_pipeline()
        cut = self.b.create_asset("p1", "rough_cut", "h_cut", "ed",
                                  version_id=v["version_id"],
                                  root_shot_ids=[shot["shot_id"]])
        fmt = self._market_format(cut, "en", "north-america", "h_f")
        rel = self.b.schedule_release(
            "p1", fmt["asset_id"], "north-america", "en", "netflix",
            "2026-04-01T00:00:00Z")
        return self.b.publish_release(rel["release_id"])

    def test_duplicate_and_late_receipts_post_once(self):
        rel = self._release()
        first = self.b.record_receipt(
            rel["release_id"], "netflix", "2026-04-01", "2026-05-01",
            "1000.00", "USD", "platform-receipt-001")
        self.assertTrue(first["posted"])
        # 平台重复推送（相同幂等键、迟到）
        dup = self.b.record_receipt(
            rel["release_id"], "netflix", "2026-04-01", "2026-05-01",
            "1000.00", "USD", "platform-receipt-001")
        self.assertTrue(dup["duplicate"])
        self.assertFalse(dup["posted"])
        # 不同幂等键但同一结算周期，同样拒绝二次入账
        period = self.b.record_receipt(
            rel["release_id"], "netflix", "2026-04-01", "2026-05-01",
            "999.00", "USD", "another-key")
        self.assertTrue(period["duplicate"])
        accounts = self.b.market_accounts("p1")
        self.assertTrue(accounts["balanced"])
        self.assertEqual(accounts["accounts"][0]["total"], "1000.00")
        self.assertEqual(accounts["accounts"][0]["ledger_entries"], 1)

    def test_receipts_require_released_release(self):
        v, _bk, shot = self._shot_pipeline()
        cut = self.b.create_asset("p1", "rough_cut", "h_cut2", "ed",
                                  version_id=v["version_id"],
                                  root_shot_ids=[shot["shot_id"]])
        fmt = self._market_format(cut, "en", "north-america", "h_f2")
        rel = self.b.schedule_release(
            "p1", fmt["asset_id"], "north-america", "en", "netflix",
            "2026-04-01T00:00:00Z")
        with self.assertRaises(ConflictState):
            self.b.record_receipt(rel["release_id"], "netflix",
                                  "2026-04-01", "2026-05-01",
                                  "1", "USD", "k")

    def test_accounts_stay_split_per_market_after_cross_region_revoke(self):
        rel_na = self._release()
        self.b.grant_license("p1", "southeast-asia", "th", *W_2026)
        v = self.b.list_versions("p1")[-1]
        bk = self.b.create_booking(
            "p1", "S1", v["version_id"], team_id="team_us", set_id="set_a",
            talent_ids=["tal_maria"], starts_at="2026-03-03T10:00:00Z",
            ends_at="2026-03-03T11:00:00Z")
        shot = self.b.record_shot(bk["booking_id"], ["m#ch1-10"])
        cut = self.b.create_asset("p1", "rough_cut", "h_cut_th", "ed",
                                  version_id=v["version_id"],
                                  root_shot_ids=[shot["shot_id"]])
        fmt = self._market_format(cut, "th", "southeast-asia", "h_f_th")
        rel_sea = self.b.schedule_release(
            "p1", fmt["asset_id"], "southeast-asia", "th", "line",
            "2026-04-01T00:00:00Z")
        self.b.publish_release(rel_sea["release_id"])
        self.b.record_receipt(rel_na["release_id"], "netflix",
                              "2026-04-01", "2026-05-01", "100.00", "USD", "k1")
        self.b.record_receipt(rel_sea["release_id"], "line",
                              "2026-04-01", "2026-05-01", "200.00", "THB", "k2")
        lic_na = next(l for l in self.b.list_licenses("p1")
                      if l["market"] == "north-america")
        self.b.revoke_license(lic_na["license_id"], "撤权")
        accounts = self.b.market_accounts("p1")
        self.assertTrue(accounts["balanced"])
        totals = {(a["market"], a["currency"]): a["total"]
                  for a in accounts["accounts"]}
        self.assertEqual(totals[("north-america", "USD")], "100.00")
        self.assertEqual(totals[("southeast-asia", "THB")], "200.00")


class TestDisputeTrace(BackendTest):
    def test_trace_covers_rights_approvers_materials_window_and_timeline(self):
        v, _bk, shot = self._shot_pipeline()
        cut = self.b.create_asset("p1", "rough_cut", "h_cut", "ed",
                                  version_id=v["version_id"],
                                  root_shot_ids=[shot["shot_id"]])
        sub = self.b.create_asset("p1", "subtitle", "h_sub", "ed",
                                  parent_asset_id=cut["asset_id"], language="en")
        fmt = self.b.create_asset("p1", "market_format", "h_f", "d",
                                  parent_asset_id=sub["asset_id"],
                                  language="en", market="north-america")
        rel = self.b.schedule_release(
            "p1", fmt["asset_id"], "north-america", "en", "netflix",
            "2026-04-01T00:00:00Z")
        self.b.publish_release(rel["release_id"])
        trace = self.b.dispute_trace(rel["release_id"])
        approvers = {a["role"]: a["approver"]
                     for a in trace["adopted_script"]["approvals"]}
        self.assertEqual(approvers, {"rights": "rights_boss",
                                     "compliance": "legal_boss",
                                     "production": "prod_boss"})
        self.assertEqual(trace["source_ip"]["ip_id"], "ip1")
        self.assertEqual(trace["rights_window_at_release"]["market"],
                         "north-america")
        self.assertEqual(trace["asset_lineage"]["root_shots"][0]["shot_id"],
                         shot["shot_id"])
        self.assertEqual(trace["teams_timezones"][0]["timezone"],
                         "America/Los_Angeles")
        types = {e["event_type"] for e in trace["production_timeline"]}
        self.assertIn("license.granted", types)
        self.assertIn("script.locked", types)
        self.assertIn("shot.recorded", types)
        self.assertIn("release.published", types)
        # 排期事件携带团队本地时间
        sched = next(e for e in trace["production_timeline"]
                     if e["event_type"] == "schedule.confirmed")
        self.assertEqual(sched["team_timezone"], "America/Los_Angeles")
        self.assertIn("team_local_start", sched["payload"])


class TestFileBackedConcurrency(unittest.TestCase):
    """文件库 + 多线程：唯一约束与 IMMEDIATE 事务兜底的并发正确性。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "prod.db")
        b = Backend(self.path)
        b.register_ip("IP", "RH", ip_id="ip1")
        b.create_project("ip1", "T", ["north-america"], ["m1"], project_id="p1")
        b.create_team("t1", "T", "UTC")
        b.create_set("s1", "S", "UTC")
        b.create_talent("a1", "A")
        b.assign_team("p1", "t1")
        b.grant_license("p1", "north-america", "en", *W_2026)
        v = b.create_draft("p1", [{"scene_ref": "S1",
                                   "material_refs": ["m1"]}], "w")
        b.submit_approval(v["version_id"], "rights", "r", "approved")
        b.submit_approval(v["version_id"], "compliance", "c", "approved")
        b.submit_approval(v["version_id"], "production", "p", "approved")
        self.version_id = v["version_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_parallel_bookings_exactly_one_wins(self):
        results = []

        def attempt(i):
            bi = Backend(self.path)
            try:
                bi.create_booking(
                    "p1", "S1", self.version_id, team_id="t1", set_id="s1",
                    talent_ids=["a1"], starts_at="2026-03-01T10:00:00Z",
                    ends_at="2026-03-01T11:00:00Z", actor=f"t{i}")
                results.append("ok")
            except SchedulingConflict:
                results.append("conflict")
            except Exception as exc:  # noqa: BLE001
                results.append(f"error:{type(exc).__name__}")

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("ok"), 1, results)
        self.assertEqual(results.count("conflict"), 7)

    def test_parallel_drafts_never_share_seq(self):
        seqs, errors = [], []

        def draft(i):
            bi = Backend(self.path)
            head = bi.list_versions("p1")[-1]["version_id"]
            try:
                v = bi.create_draft(
                    "p1", [{"scene_ref": "S1", "summary": f"d{i}",
                            "material_refs": ["m1"]}], f"w{i}",
                    based_on_version_id=head)
                seqs.append(v["seq"])
            except VersionConflict:
                errors.append("conflict")

        threads = [threading.Thread(target=draft, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertGreaterEqual(len(errors), 1)

    def test_parallel_duplicate_receipts_post_once(self):
        b = Backend(self.path)
        bk = b.create_booking(
            "p1", "S1", self.version_id, team_id="t1", set_id="s1",
            talent_ids=["a1"], starts_at="2026-04-01T10:00:00Z",
            ends_at="2026-04-01T11:00:00Z")
        shot = b.record_shot(bk["booking_id"], ["m1"])
        cut = b.create_asset("p1", "rough_cut", "hc", "e",
                             version_id=self.version_id,
                             root_shot_ids=[shot["shot_id"]])
        sub = b.create_asset("p1", "subtitle", "hsub", "e",
                             parent_asset_id=cut["asset_id"], language="en")
        fmt = b.create_asset("p1", "market_format", "hf", "d",
                             parent_asset_id=sub["asset_id"],
                             language="en", market="north-america")
        rel = b.schedule_release("p1", fmt["asset_id"], "north-america", "en",
                                 "netflix", "2026-05-01T00:00:00Z")
        b.publish_release(rel["release_id"])
        posted = []

        def send():
            bi = Backend(self.path)
            r = bi.record_receipt(rel["release_id"], "netflix",
                                  "2026-05-01", "2026-06-01", "500.00",
                                  "USD", "same-platform-key")
            posted.append(r["posted"])

        threads = [threading.Thread(target=send) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(posted), 1)
        accounts = Backend(self.path).market_accounts("p1")
        self.assertEqual(accounts["accounts"][0]["total"], "500.00")
        self.assertTrue(accounts["balanced"])


class TestHttpApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = str(Path(cls.tmp.name) / "api.db")
        from http.server import ThreadingHTTPServer as THS

        cls.httpd = THS(("127.0.0.1", 0), make_handler(Backend(cls.db_path)))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_health_and_full_flow(self):
        status, body = self._req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "global-drama-production")

        s, ip = self._req("POST", "/api/ips",
                          {"title": "IP", "rightsholder": "RH", "ip_id": "ip1"})
        self.assertEqual(s, 200)
        s, p = self._req("POST", "/api/projects",
                         {"ip_id": "ip1", "title": "T",
                          "markets": ["north-america"],
                          "allowed_source_materials": ["m1"],
                          "project_id": "p1"})
        self.assertEqual(s, 200, p)
        self._req("POST", "/api/teams",
                  {"team_id": "t1", "name": "T", "timezone": "UTC"})
        self._req("POST", "/api/sets",
                  {"set_id": "s1", "name": "S", "timezone": "UTC"})
        self._req("POST", "/api/talent", {"talent_id": "a1", "name": "A"})
        self._req("POST", "/api/projects/p1/teams", {"team_id": "t1"})
        s, lic = self._req("POST", "/api/projects/p1/licenses",
                           {"market": "north-america", "language": "en",
                            "starts_at": W_2026[0], "expires_at": W_2026[1]})
        self.assertEqual(s, 200, lic)
        s, v = self._req("POST", "/api/projects/p1/scripts",
                         {"scenes": [{"scene_ref": "S1",
                                      "material_refs": ["m1"]}],
                          "created_by": "w"})
        self.assertEqual(s, 200)
        s, note = self._req("POST", f"/api/scripts/{v['version_id']}/notes",
                            {"author": "c", "author_role": "local_consultant",
                             "category": "cultural", "body": "x",
                             "scene_ref": "S1"})
        self.assertEqual(s, 200)
        s, chg = self._req("POST", f"/api/scripts/{v['version_id']}/changes",
                           {"scene_ref": "S1", "change_type": "localization",
                            "description": "y",
                            "source_note_id": note["note_id"]})
        self.assertEqual(s, 200)
        for role, who in [("rights", "r"), ("compliance", "c"),
                          ("production", "p")]:
            s, resp = self._req(
                "POST", f"/api/scripts/{v['version_id']}/approvals",
                {"role": role, "approver": who, "decision": "approved"})
            self.assertEqual(s, 200, resp)
        s, bk = self._req("POST", "/api/projects/p1/bookings",
                          {"scene_ref": "S1", "version_id": v["version_id"],
                           "team_id": "t1", "set_id": "s1",
                           "talent_ids": ["a1"],
                           "starts_at": "2026-03-01T10:00:00Z",
                           "ends_at": "2026-03-01T11:00:00Z"})
        self.assertEqual(s, 200, bk)
        s, shot = self._req("POST", f"/api/bookings/{bk['booking_id']}/shots",
                            {"material_refs": ["m1"]})
        self.assertEqual(s, 200)
        s, cut = self._req("POST", "/api/projects/p1/assets",
                           {"kind": "rough_cut", "content_hash": "hc",
                            "created_by": "e", "version_id": v["version_id"],
                            "root_shot_ids": [shot["shot_id"]]})
        self.assertEqual(s, 200)
        s, fmt = self._req("POST", "/api/projects/p1/assets",
                           {"kind": "market_format", "content_hash": "hf",
                            "created_by": "d",
                            "parent_asset_id": cut["asset_id"],
                            "language": "en", "market": "north-america"})
        self.assertEqual(s, 422)  # market_format 不能直接挂粗剪（无字幕/配音链）
        s, sub = self._req("POST", "/api/projects/p1/assets",
                           {"kind": "subtitle", "content_hash": "hs",
                            "created_by": "e",
                            "parent_asset_id": cut["asset_id"],
                            "language": "en"})
        self.assertEqual(s, 200)
        s, fmt = self._req("POST", "/api/projects/p1/assets",
                           {"kind": "market_format", "content_hash": "hf",
                            "created_by": "d",
                            "parent_asset_id": sub["asset_id"],
                            "language": "en", "market": "north-america"})
        self.assertEqual(s, 200)
        s, rel = self._req("POST", "/api/projects/p1/releases",
                           {"asset_id": fmt["asset_id"],
                            "market": "north-america", "language": "en",
                            "platform": "netflix",
                            "scheduled_at": "2026-04-01T00:00:00Z"})
        self.assertEqual(s, 200)
        s, pub = self._req("POST",
                           f"/api/releases/{rel['release_id']}/publish", {})
        self.assertEqual(s, 200, pub)
        s, rcp = self._req("POST",
                           f"/api/releases/{rel['release_id']}/receipts",
                           {"platform": "netflix",
                            "period_start": "2026-04-01",
                            "period_end": "2026-05-01", "amount": "10.00",
                            "currency": "USD", "idempotency_key": "k1"})
        self.assertEqual(s, 200)
        s, dup = self._req("POST",
                           f"/api/releases/{rel['release_id']}/receipts",
                           {"platform": "netflix",
                            "period_start": "2026-04-01",
                            "period_end": "2026-05-01", "amount": "10.00",
                            "currency": "USD", "idempotency_key": "k1"})
        self.assertTrue(dup["duplicate"])
        s, trace = self._req("GET",
                             f"/api/releases/{rel['release_id']}/trace")
        self.assertEqual(s, 200)
        self.assertEqual(len(trace["adopted_script"]["approvals"]), 3)
        s, accounts = self._req("GET", "/api/projects/p1/accounts")
        self.assertTrue(accounts["balanced"])
        # 撤权后已发行版本仍可查，未发行版本 blocked
        s, rev = self._req("POST", f"/api/licenses/{lic['license_id']}/revoke",
                           {"reason": "争议"})
        self.assertEqual(s, 200)
        # 不存在路由
        s, _ = self._req("GET", "/api/nope")
        self.assertEqual(s, 404)
        # 排期冲突经 HTTP 返回 409 + reasons
        s, conflict = self._req("POST", "/api/projects/p1/bookings",
                                {"scene_ref": "S1",
                                 "version_id": v["version_id"],
                                 "team_id": "t1", "set_id": "s1",
                                 "talent_ids": ["a1"],
                                 "starts_at": "2026-03-01T10:30:00Z",
                                 "ends_at": "2026-03-01T11:30:00Z"})
        self.assertEqual(s, 409)
        self.assertEqual(conflict["error"], "scheduling_conflict")
        self.assertGreaterEqual(len(conflict["details"]["reasons"]), 1)


if __name__ == "__main__":
    unittest.main()
