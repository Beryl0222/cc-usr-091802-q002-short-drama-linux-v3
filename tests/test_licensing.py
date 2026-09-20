"""授权与立项锁定测试。"""

import unittest

from studio import ConflictError, ProductionSystem, ValidationError
from tests.helpers import seed_project


class LicensingTest(unittest.TestCase):
    def setUp(self):
        self.s = ProductionSystem()

    def test_project_locks_markets_languages_term_materials(self):
        seed_project(self.s)
        project = self.s.store.require("projects", "prj-1", "项目")
        self.assertEqual(set(project["markets"]), {"na", "sea"})
        sea = project["markets"]["sea"]
        self.assertEqual(sea["languages"], ["en"])
        self.assertEqual(sea["material_boundary"], ["novel-ch1", "novel-ch2"])
        self.assertEqual(sea["basis_grant_ids"], ["grant-sea"])

    def test_language_outside_grant_rejected(self):
        seed_project(self.s, markets=("sea",))
        with self.assertRaises(ConflictError) as ctx:
            self.s.create_project(
                "prj-bad", "ip-1", "越界语言",
                [{"market": "sea", "languages": ["th"],
                  "term_start": "2026-02-01T00:00:00Z",
                  "term_end": "2027-06-30T23:59:59Z",
                  "material_boundary": ["novel-ch1"]}], "ops")
        self.assertEqual(ctx.exception.details["market"], "sea")

    def test_term_outside_grant_window_rejected(self):
        seed_project(self.s, markets=("sea",))
        with self.assertRaises(ConflictError):
            self.s.create_project(
                "prj-bad", "ip-1", "超期",
                [{"market": "sea", "languages": ["en"],
                  "term_start": "2026-01-01T00:00:00Z",
                  "term_end": "2028-12-31T23:59:59Z",
                  "material_boundary": ["novel-ch1"]}], "ops")

    def test_material_beyond_scope_rejected(self):
        seed_project(self.s, markets=("sea",))
        with self.assertRaises(ConflictError):
            self.s.create_project(
                "prj-bad", "ip-1", "越界素材",
                [{"market": "sea", "languages": ["en"],
                  "term_start": "2026-02-01T00:00:00Z",
                  "term_end": "2027-06-30T23:59:59Z",
                  "material_boundary": ["novel-ch9"]}], "ops")

    def test_bad_grant_inputs(self):
        self.s.register_ip("ip-1", "X", "holder", "ops")
        with self.assertRaises(ValidationError):
            self.s.create_grant("g1", "ip-1", "sea", [],
                               "2026-01-01T00:00:00Z",
                               "2027-01-01T00:00:00Z", ["m"], "ops")
        with self.assertRaises(ValidationError):
            self.s.create_grant("g2", "ip-1", "sea", ["en"],
                               "2027-01-01T00:00:00Z",
                               "2026-01-01T00:00:00Z", ["m"], "ops")

    def test_overlapping_grant_keeps_lock_when_one_revoked(self):
        seed_project(self.s, markets=("sea",))
        # 第二份完全覆盖的授权
        self.s.create_grant(
            "grant-sea-backup", "ip-1", "sea", ["en", "zh"],
            "2025-01-01T00:00:00Z", "2029-12-31T23:59:59Z",
            ["novel-ch1", "novel-ch2", "char-set-a"], "rights-lin")
        result = self.s.revoke_market_rights(
            "rights-lin", "第一份授权争议", grant_ids=["grant-sea"])
        self.assertEqual(result["revoked_grant_ids"], ["grant-sea"])
        # 锁仍被备份授权覆盖：不受影响
        self.assertEqual(result["affected_projects"], [])
        lock = self.s.store.require("projects", "prj-1", "项目")["markets"]["sea"]
        self.assertEqual(lock["status"], "active")
        self.assertEqual(lock["basis_grant_ids"], ["grant-sea-backup"])

    def test_revoke_last_covering_grant_revokes_lock(self):
        seed_project(self.s, markets=("sea",))
        result = self.s.revoke_market_rights(
            "rights-lin", "版权争议", ip_id="ip-1", market="sea")
        self.assertEqual(result["revoked_grant_ids"], ["grant-sea"])
        self.assertEqual(result["affected_projects"][0]["project_id"], "prj-1")
        self.assertEqual(result["affected_projects"][0]["market"], "sea")

    def test_double_revoke_is_conflict(self):
        seed_project(self.s, markets=("sea",))
        self.s.revoke_market_rights("rights-lin", "撤", grant_ids=["grant-sea"])
        with self.assertRaises(ConflictError):
            self.s.revoke_market_rights(
                "rights-lin", "再撤", grant_ids=["grant-sea"])


if __name__ == "__main__":
    unittest.main()
