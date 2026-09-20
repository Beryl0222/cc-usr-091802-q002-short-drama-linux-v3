"""排期冲突、时区展示与拍摄来源固化测试。"""

import threading
import unittest

from studio import ConflictError, ProductionSystem, StateError
from tests.helpers import (
    approved_script,
    book_and_shoot,
    register_resources,
    seed_project,
)


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.s = ProductionSystem()
        seed_project(self.s)
        approved_script(self.s, markets=("sea",))
        register_resources(self.s)

    def test_conflict_reports_resource_and_local_window(self):
        self.s.book("b1", "prj-1", "sea", "set-hq",
                    "2026-03-01T02:00:00Z", "2026-03-01T10:00:00Z",
                    "正片", "pm-sea")
        with self.assertRaises(ConflictError) as ctx:
            self.s.book("b2", "prj-1", "sea", "set-hq",
                        "2026-03-01T09:00:00Z", "2026-03-01T12:00:00Z",
                        "补拍", "pm-sea")
        detail = ctx.exception.details
        self.assertEqual(detail["conflicts"][0]["reason"], "resource_busy")
        self.assertEqual(detail["conflicts"][0]["conflicting_booking_id"], "b1")
        # Macau = UTC+8，无夏令时
        self.assertIn("+08:00", detail["requested_local"][0])
        self.assertEqual(detail["conflicts"][0]["resource_type"], "set")

    def test_adjacent_bookings_do_not_conflict(self):
        self.s.book("b1", "prj-1", "sea", "set-hq",
                    "2026-03-01T02:00:00Z", "2026-03-01T10:00:00Z",
                    "正片", "pm-sea")
        check = self.s.check_availability(
            "set-hq", "2026-03-01T10:00:00Z", "2026-03-01T12:00:00Z")
        self.assertTrue(check["available"])

    def test_concurrent_booking_exactly_one_wins(self):
        errors = []

        def book(bid):
            try:
                self.s.book(bid, "prj-1", "sea", "set-hq",
                            "2026-04-01T02:00:00Z", "2026-04-01T08:00:00Z",
                            "抢档", "pm")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=book, args=(f"b{i}",))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(errors), 7)
        cal = self.s.calendar("set-hq")
        april = [b for b in cal["bookings"] if "2026-04-01" in b["window_utc"][0]]
        self.assertEqual(len(april), 1)

    def test_cannot_book_after_revocation(self):
        self.s.revoke_market_rights(
            "rights-lin", "撤权", ip_id="ip-1", market="sea")
        with self.assertRaises(StateError):
            self.s.book("b-late", "prj-1", "sea", "set-hq",
                        "2026-03-01T02:00:00Z", "2026-03-01T10:00:00Z",
                        "正片", "pm")

    def test_invalid_timezone_rejected(self):
        with self.assertRaises(Exception):
            self.s.register_resource("bad", "set", "坏时区", "Not/Zone")


class ShootProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.s = ProductionSystem()
        seed_project(self.s)
        approved_script(self.s, markets=("sea",))
        register_resources(self.s)

    def test_shot_freezes_source_survives_new_versions(self):
        _, shot_id = book_and_shoot(self.s, market="sea")
        # 剧本继续迭代到 v-3：已拍镜头来源不变
        self.s.create_script_version(
            "prj-1", "v-3",
            [{"item_id": "sc-1", "summary": "医院场景全面重写"},
             {"item_id": "sc-9", "summary": "新增支线"}],
            "writer-wei", parent_id="v-2", note="新一轮改稿")
        shot = self.s.store.require("shots", shot_id, "镜头")
        self.assertEqual(shot["version_id"], "v-2")
        snap = shot["source_snapshot"]
        self.assertEqual(snap["script_version_id"], "v-2")
        self.assertEqual(snap["script_item"]["item_id"], "sc-1")
        self.assertEqual(snap["script_approvers"]["compliance"], "comp-zhou")
        self.assertEqual(snap["script_approvers"]["rights"], "rights-lin")
        self.assertEqual(snap["script_approvers"]["production"], "prod-chen")
        self.assertEqual(snap["rights_window"][0], "2026-02-01T00:00:00Z")
        self.assertEqual(snap["grant_terms_at_shoot"][0]["grant_id"], "grant-sea")
        self.assertEqual(snap["booking_timezone"], "Asia/Macau")

    def test_cannot_shoot_without_released_version(self):
        # na 市场有锁但剧本从未下发
        self.s.book("b-na", "prj-1", "na", "team-la",
                    "2026-03-02T14:00:00Z", "2026-03-02T20:00:00Z",
                    "试拍", "pm-na")
        with self.assertRaises(StateError):
            self.s.capture_shot("shot-na-x", "prj-1", "na", "b-na",
                                "sc-1", "novel-ch1", "cam")

    def test_cannot_shoot_material_outside_boundary(self):
        self.s.book("b1", "prj-1", "sea", "set-hq",
                    "2026-03-01T02:00:00Z", "2026-03-01T10:00:00Z",
                    "正片", "pm-sea")
        with self.assertRaises(ConflictError):
            self.s.capture_shot("shot-x", "prj-1", "sea", "b1",
                                "sc-1", "novel-ch9", "cam")

    def test_cannot_shoot_after_revocation(self):
        self.s.book("b1", "prj-1", "sea", "set-hq",
                    "2026-03-01T02:00:00Z", "2026-03-01T10:00:00Z",
                    "正片", "pm-sea")
        self.s.revoke_market_rights(
            "rights-lin", "撤权", ip_id="ip-1", market="sea")
        with self.assertRaises(StateError):
            self.s.capture_shot("shot-late", "prj-1", "sea", "b1",
                                "sc-1", "novel-ch1", "cam")


if __name__ == "__main__":
    unittest.main()
