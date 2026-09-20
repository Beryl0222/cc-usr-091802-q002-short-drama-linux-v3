"""剧本版本链、逐条关联与三方审批汇合测试。"""

import threading
import unittest

from studio import ConflictError, ProductionSystem, StateError
from tests.helpers import approved_script, seed_project


class ScriptFlowTest(unittest.TestCase):
    def setUp(self):
        self.s = ProductionSystem()
        seed_project(self.s, markets=("na", "sea"))

    def test_annotation_pair_carried_into_version(self):
        approved_script(self.s)
        v = self.s.get_version("v-2")
        kinds = {a["kind"]: a for a in v["annotations"]}
        self.assertIn("localization", kinds)
        self.assertIn("plot_change", kinds)
        self.assertEqual(kinds["plot_change"]["related_id"], "note-1")
        self.assertEqual(kinds["localization"]["related_id"], "chg-1")
        self.assertEqual(kinds["plot_change"]["target_item_id"], "sc-1")

    def test_only_converged_version_can_be_released(self):
        self.s.create_script_version(
            "prj-1", "v-1",
            [{"item_id": "sc-1", "summary": "开场"}], "writer-wei")
        self.s.submit_script("v-1", "writer-wei")
        self.s.approve_script("v-1", "rights", "rights-lin")
        self.s.approve_script("v-1", "compliance", "comp-zhou")
        # 制片尚未批准：不能下发
        with self.assertRaises(StateError):
            self.s.release_script_to_market("v-1", "sea", "prod-chen")
        self.s.approve_script("v-1", "production", "prod-chen")
        self.s.release_script_to_market("v-1", "sea", "prod-chen")
        self.assertEqual(self.s.get_version("v-1")["state"], "released")

    def test_reject_reopens_editing_chain(self):
        self.s.create_script_version(
            "prj-1", "v-1",
            [{"item_id": "sc-1", "summary": "开场"}], "writer-wei")
        self.s.submit_script("v-1", "writer-wei")
        self.s.reject_script("v-1", "compliance", "comp-zhou", "台词违规")
        # 被驳回后才能基于它改稿
        self.s.create_script_version(
            "prj-1", "v-2",
            [{"item_id": "sc-1", "summary": "开场(修订)"}], "writer-wei",
            parent_id="v-1")
        self.assertEqual(self.s.get_version("v-2")["sequence"], 2)

    def test_duplicate_approval_rejected(self):
        self.s.create_script_version(
            "prj-1", "v-1",
            [{"item_id": "sc-1", "summary": "开场"}], "writer-wei")
        self.s.submit_script("v-1", "writer-wei")
        self.s.approve_script("v-1", "rights", "rights-lin")
        with self.assertRaises(ConflictError):
            self.s.approve_script("v-1", "rights", "rights-lin")

    def test_plot_change_without_localization_note_rejected(self):
        self.s.create_script_version(
            "prj-1", "v-1",
            [{"item_id": "sc-1", "summary": "开场"}], "writer-wei")
        with self.assertRaises(ConflictError):
            self.s.add_plot_change("prj-1", "chg-x", "missing-note",
                                   "writer-wei", "凭空改动")

    def test_one_note_cannot_bind_two_changes(self):
        self.s.create_script_version(
            "prj-1", "v-1",
            [{"item_id": "sc-1", "summary": "开场"}], "writer-wei")
        self.s.add_localization_note("prj-1", "note-1", "sc-1", "advisor",
                                     "意见", "ref")
        self.s.add_plot_change("prj-1", "chg-1", "note-1", "writer-wei", "改1")
        with self.assertRaises(ConflictError):
            self.s.add_plot_change("prj-1", "chg-2", "note-1",
                                   "writer-wei", "改2")

    def test_submitted_version_blocks_new_edits(self):
        self.s.create_script_version(
            "prj-1", "v-1",
            [{"item_id": "sc-1", "summary": "开场"}], "writer-wei")
        self.s.submit_script("v-1", "writer-wei")
        with self.assertRaises(StateError):
            self.s.create_script_version(
                "prj-1", "v-2",
                [{"item_id": "sc-1", "summary": "并行改稿"}], "writer-wei",
                parent_id="v-1")

    def test_withdraw_allows_resubmission(self):
        self.s.create_script_version(
            "prj-1", "v-1",
            [{"item_id": "sc-1", "summary": "开场"}], "writer-wei")
        self.s.submit_script("v-1", "writer-wei")
        self.s.approve_script("v-1", "rights", "rights-lin")
        self.s.withdraw_script("v-1", "writer-wei")
        self.assertEqual(self.s.get_version("v-1")["state"], "draft")
        self.assertEqual(self.s.get_version("v-1")["approvals"], {})

    def test_concurrent_edits_only_one_wins(self):
        """两名编剧同时基于 v-1 提交：先到成为 v-2，后到必须基于新 head 修订。"""
        self.s.create_script_version(
            "prj-1", "v-1",
            [{"item_id": "sc-1", "summary": "开场"}], "writer-wei")
        content = [{"item_id": "sc-1", "summary": "改稿"}]
        errors = []

        def edit(version_id):
            try:
                self.s.create_script_version(
                    "prj-1", version_id, content, "writer",
                    parent_id="v-1")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=edit, args=("v-a",))
        t2 = threading.Thread(target=edit, args=("v-b",))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ConflictError)
        head = self.s.store.require("projects", "prj-1", "项目")["head_version_id"]
        self.assertIn(head, ("v-a", "v-b"))
        # 失败者拿到的 expected_parent 正是实际赢家
        self.assertEqual(errors[0].details["expected_parent"], head)
        self.assertEqual(errors[0].details["submitted_parent"], "v-1")


if __name__ == "__main__":
    unittest.main()
