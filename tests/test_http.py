"""HTTP 层端到端冒烟：真实启动服务并走完整业务流。"""

import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

import studio.api as api
from service import Handler


def _free_port_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


class HttpSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 每个测试进程使用全新的领域状态
        api.SYSTEM = type(api.SYSTEM)()
        cls.server, cls.port = _free_port_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        data = json.loads(raw) if raw else {}
        return resp.status, data

    def post(self, path, body):
        return self.request("POST", path, body)

    def get(self, path):
        return self.request("GET", path)

    def test_full_workflow_over_http(self):
        # 健康检查保留基线契约
        status, health = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["service"], "global-drama-production")

        actor = {"actor": "ops"}
        self.assertEqual(self.post("/api/ips", {
            **actor, "ip_id": "ip-1", "title": "Reborn Tycoon",
            "rights_holder": "阅文"})[0], 201)
        for market in ("sea", "na"):
            self.assertEqual(self.post("/api/grants", {
                **actor, "grant_id": f"grant-{market}", "ip_id": "ip-1",
                "market": market, "languages": ["en", "zh"],
                "start_at": "2026-01-01T00:00:00Z",
                "end_at": "2027-12-31T23:59:59Z",
                "material_scope": ["novel-ch1", "novel-ch2"]})[0], 201)
        status, project = self.post("/api/projects", {
            **actor, "project_id": "prj-1", "ip_id": "ip-1",
            "name": "重生大亨海外版",
            "markets": [
                {"market": "sea", "languages": ["en"],
                 "term_start": "2026-02-01T00:00:00Z",
                 "term_end": "2027-06-30T23:59:59Z",
                 "material_boundary": ["novel-ch1", "novel-ch2"]},
                {"market": "na", "languages": ["en"],
                 "term_start": "2026-02-01T00:00:00Z",
                 "term_end": "2027-06-30T23:59:59Z",
                 "material_boundary": ["novel-ch1", "novel-ch2"]}]})
        self.assertEqual(status, 201)
        self.assertEqual(set(project["markets"]), {"sea", "na"})

        # 越权语言立项 → 409
        status, err = self.post("/api/projects", {
            **actor, "project_id": "prj-bad", "ip_id": "ip-1", "name": "越界",
            "markets": [{"market": "sea", "languages": ["th"],
                         "term_start": "2026-02-01T00:00:00Z",
                         "term_end": "2027-06-30T23:59:59Z",
                         "material_boundary": ["novel-ch1"]}]})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "conflict")

        # 剧本与审批
        self.post("/api/script-versions", {
            "project_id": "prj-1", "version_id": "v-1", "created_by": "wei",
            "content": [{"item_id": "sc-1", "summary": "重生醒来"}]})
        self.post("/api/projects/prj-1/localization-notes", {
            "actor": "farida", "advisor": "farida", "note_id": "note-1",
            "target_item_id": "sc-1", "content": "避免宗教符号",
            "basis_ref": "guide#1"})
        self.post("/api/projects/prj-1/plot-changes", {
            "actor": "wei", "writer": "wei", "change_id": "chg-1",
            "note_id": "note-1", "content": "改为医院"})
        self.post("/api/script-versions", {
            "project_id": "prj-1", "version_id": "v-2", "created_by": "wei",
            "parent_id": "v-1", "content": [
                {"item_id": "sc-1", "summary": "医院醒来(本地化)"}]})
        self.post("/api/script-versions/v-2/submit", {"actor": "wei"})
        self.post("/api/script-versions/v-2/approve",
                  {"role": "rights", "actor": "lin"})
        self.post("/api/script-versions/v-2/approve",
                  {"role": "compliance", "actor": "zhou"})
        self.post("/api/script-versions/v-2/approve",
                  {"role": "production", "actor": "chen"})
        self.post("/api/script-versions/v-2/release",
                  {"market": "sea", "actor": "chen"})

        # 资源/档期/镜头
        self.post("/api/resources", {
            "resource_id": "set-hq", "type": "set", "name": "A棚",
            "timezone": "Asia/Macau", "market": "sea", "actor": "ops"})
        self.post("/api/bookings", {
            "booking_id": "b1", "project_id": "prj-1", "market": "sea",
            "resource_id": "set-hq", "start_at": "2026-03-01T02:00:00Z",
            "end_at": "2026-03-01T10:00:00Z", "purpose": "正片",
            "actor": "pm"})
        status, clash = self.post("/api/bookings", {
            "booking_id": "b2", "project_id": "prj-1", "market": "sea",
            "resource_id": "set-hq", "start_at": "2026-03-01T09:00:00Z",
            "end_at": "2026-03-01T12:00:00Z", "purpose": "补拍",
            "actor": "pm"})
        self.assertEqual(status, 409)
        self.assertEqual(clash["details"]["conflicts"][0]["reason"],
                         "resource_busy")

        status, shot = self.post("/api/shots", {
            "shot_id": "shot-1", "project_id": "prj-1", "market": "sea",
            "booking_id": "b1", "item_id": "sc-1", "material_id": "novel-ch1",
            "actor": "cam"})
        self.assertEqual(status, 201)
        self.assertEqual(shot["version_id"], "v-2")

        # 派生谱系与发行
        self.post("/api/assets", {"asset_id": "rough", "project_id": "prj-1",
                                  "market": "sea", "type": "rough_cut",
                                  "created_by": "ed",
                                  "source_shot_ids": ["shot-1"]})
        self.post("/api/assets", {"asset_id": "sub", "project_id": "prj-1",
                                  "market": "sea", "type": "subtitle",
                                  "created_by": "loc", "parent_asset_id": "rough",
                                  "language": "en"})
        self.post("/api/assets", {"asset_id": "dub", "project_id": "prj-1",
                                  "market": "sea", "type": "dub",
                                  "created_by": "loc", "parent_asset_id": "rough",
                                  "language": "en"})
        self.post("/api/assets", {"asset_id": "fmt", "project_id": "prj-1",
                                  "market": "sea", "type": "market_format",
                                  "created_by": "pm", "parent_asset_id": "dub",
                                  "language": "en"})
        status, rel = self.post("/api/publish",
                                {"asset_id": "fmt", "platform": "streamix",
                                 "actor": "dist"})
        self.assertEqual(status, 201)

        # 重复/迟到回执只入账一次
        receipt = {"project_id": "prj-1", "release_id": rel["release_id"],
                   "receipt_no": "R-1", "amount": "100.00", "currency": "USD",
                   "period": "2026-03", "idem_key": "K-1", "actor": "dist"}
        s1, first = self.post("/api/receipts", receipt)
        s2, second = self.post("/api/receipts", receipt)
        self.assertEqual(s1, 201)
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])

        # 反查上线版本
        status, report = self.get(f"/api/trace/release/{rel['release_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(report["adopted_versions"][0]["approvers"]["rights"],
                         "lin")

        # 撤 na 不影响 sea
        status, result = self.post("/api/revoke", {
            "actor": "lin", "reason": "北美争议", "ip_id": "ip-1",
            "market": "na"})
        self.assertEqual(status, 200)
        self.assertIn("grant-na", result["revoked_grant_ids"])
        status, accounts = self.get("/api/projects/prj-1/accounts")
        self.assertEqual(status, 200)
        self.assertTrue(accounts["na"]["frozen"])
        self.assertFalse(accounts["sea"]["frozen"])

        # 谱系查询
        status, lineage = self.get("/api/assets/fmt/lineage")
        self.assertEqual(status, 200)
        self.assertEqual([a["type"] for a in lineage["ancestors"]],
                         ["market_format", "dub", "rough_cut"])

        # 事件日志：项目级能看到市场撤权与资产阻断；全量日志含授权撤销
        status, events = self.get("/api/events?project_id=prj-1")
        self.assertEqual(status, 200)
        types = {e["type"] for e in events}
        self.assertIn("market.revoked", types)
        status, all_events = self.get("/api/events")
        self.assertEqual(status, 200)
        self.assertTrue(any(e["type"] == "grant.revoked" for e in all_events))

    def test_malformed_json_is_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/ips", body=b"{not json",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        resp.read()
        conn.close()

    def test_unknown_route_is_404(self):
        status, _ = self.get("/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
