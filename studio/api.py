"""HTTP JSON 适配层。

把 :class:`studio.app.ProductionSystem` 的领域方法暴露为 REST 风格接口；
仓储为进程内状态，进程重启即清空（与基线脚手架一致）。领域错误统一转成
4xx 与结构化错误体，未知输入返回 400，未找到资源返回 404。
"""

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .app import ProductionSystem
from .errors import ConflictError, DomainError, NotFoundError, StateError, ValidationError

SYSTEM = ProductionSystem()


def _error_status(exc):
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, (ConflictError, StateError)):
        return 409
    if isinstance(exc, ValidationError):
        return 400
    return 400


# (method, path 片段匹配函数, 处理器)
def _segments(path):
    return [p for p in path.strip("/").split("/") if p]


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "GlobalDramaProduction/1.0"

    # ---- 基础工具 ------------------------------------------------------

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValidationError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def log_message(self, *_args):
        return

    # ---- 路由 ----------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        segs, query = _segments(parsed.path), parse_qs(parsed.query)
        try:
            if segs == ["health"]:
                from service import health_payload
                self._send(200, health_payload()); return
            if segs == ["api", "events"]:
                self._send(200, SYSTEM.event_log(query.get("project_id", [None])[0])); return
            if len(segs) == 4 and segs[:2] == ["api", "resources"] \
                    and segs[3] == "calendar":
                self._send(200, SYSTEM.calendar(segs[2])); return
            if len(segs) == 4 and segs[:2] == ["api", "assets"] \
                    and segs[3] == "lineage":
                self._send(200, SYSTEM.lineage(segs[2])); return
            if len(segs) == 4 and segs[:2] == ["api", "projects"] \
                    and segs[3] == "accounts":
                self._send(200, SYSTEM.all_accounts(segs[2])); return
            if len(segs) == 4 and segs[:2] == ["api", "trace"] \
                    and segs[2] in ("shot", "asset", "release"):
                kind, ident = segs[2], segs[3]
                market = query.get("market", [None])[0]
                if kind == "shot":
                    self._send(200, SYSTEM.trace_shot(ident)); return
                if kind == "asset":
                    self._send(200, SYSTEM.trace_asset(ident)); return
                if kind == "release":
                    self._send(200, SYSTEM.trace_release(ident)); return
            if len(segs) == 6 and segs[:2] == ["api", "trace"] \
                    and segs[2] == "project" and segs[4] == "market":
                self._send(200, SYSTEM.trace_market(segs[3], segs[5])); return
            self._send(404, {"error": "not_found", "message": f"无此路由: {parsed.path}"})
        except DomainError as exc:
            self._send(_error_status(exc), exc.to_dict())
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": "internal_error", "message": str(exc)})

    def do_POST(self):
        parsed = urlparse(self.path)
        segs = _segments(parsed.path)
        try:
            body = self._read_json()
            s = SYSTEM
            if segs == ["api", "ips"]:
                self._send(201, s.register_ip(
                    body["ip_id"], body["title"], body["rights_holder"],
                    body.get("actor", "system"))); return
            if segs == ["api", "grants"]:
                self._send(201, s.create_grant(
                    body["grant_id"], body["ip_id"], body["market"],
                    body["languages"], body["start_at"], body["end_at"],
                    body["material_scope"], body.get("actor", "system"))); return
            if segs == ["api", "projects"]:
                self._send(201, s.create_project(
                    body["project_id"], body["ip_id"], body["name"],
                    body["markets"], body.get("actor", "system"))); return
            if len(segs) == 4 and segs[:2] == ["api", "projects"]:
                pid = segs[2]
                if segs[3] == "localization-notes":
                    self._send(201, s.add_localization_note(
                        pid, body["note_id"], body["target_item_id"],
                        body.get("advisor", body.get("actor")),
                        body["content"], body.get("basis_ref", ""),
                        actor=body.get("actor"))); return
                if segs[3] == "plot-changes":
                    self._send(201, s.add_plot_change(
                        pid, body["change_id"], body["note_id"],
                        body.get("writer", body.get("actor")),
                        body["content"], actor=body.get("actor"))); return
            if segs == ["api", "script-versions"]:
                self._send(201, s.create_script_version(
                    body["project_id"], body["version_id"], body["content"],
                    body["created_by"], body.get("parent_id"),
                    body.get("note", ""))); return
            if len(segs) == 4 and segs[:2] == ["api", "script-versions"]:
                vid, action = segs[2], segs[3]
                if action == "submit":
                    self._send(200, s.submit_script(vid, body.get("actor", "system"))); return
                if action == "approve":
                    self._send(200, s.approve_script(
                        vid, body["role"], body.get("actor", body["role"]))); return
                if action == "reject":
                    self._send(200, s.reject_script(
                        vid, body["role"], body.get("actor", body["role"]),
                        body.get("reason", ""))); return
                if action == "withdraw":
                    self._send(200, s.withdraw_script(vid, body.get("actor", "system"))); return
                if action == "release":
                    self._send(200, s.release_script_to_market(
                        vid, body["market"], body.get("actor", "system"))); return
            if segs == ["api", "resources"]:
                self._send(201, s.register_resource(
                    body["resource_id"], body["type"], body["name"],
                    body["timezone"], body.get("team_id"), body.get("market"),
                    body.get("actor", "system"))); return
            if segs == ["api", "bookings"]:
                self._send(201, s.book(
                    body["booking_id"], body["project_id"], body["market"],
                    body["resource_id"], body["start_at"], body["end_at"],
                    body.get("purpose", ""), body.get("actor", "system"))); return
            if segs == ["api", "shots"]:
                self._send(201, s.capture_shot(
                    body["shot_id"], body["project_id"], body["market"],
                    body["booking_id"], body["item_id"], body["material_id"],
                    body.get("actor", "system"))); return
            if segs == ["api", "assets"]:
                self._send(201, s.create_asset(
                    body["asset_id"], body["project_id"], body["market"],
                    body["type"], body.get("created_by", body.get("actor", "system")),
                    body.get("parent_asset_id"), body.get("language"),
                    body.get("source_shot_ids"), body.get("fingerprint"),
                    actor=body.get("actor"))); return
            if segs == ["api", "publish"]:
                self._send(201, s.publish(
                    body["asset_id"], body["platform"],
                    body.get("actor", "system"))); return
            if segs == ["api", "revoke"]:
                self._send(200, s.revoke_market_rights(
                    body.get("actor", "system"), body.get("reason", ""),
                    grant_ids=body.get("grant_ids"),
                    ip_id=body.get("ip_id"),
                    market=body.get("market"))); return
            if segs == ["api", "receipts"]:
                self._send(201, s.post_receipt(
                    body["project_id"], body["release_id"], body["receipt_no"],
                    body["amount"], body["currency"], body["period"],
                    body["idem_key"], body.get("actor", "system"))); return
            self._send(404, {"error": "not_found", "message": f"无此路由: {parsed.path}"})
        except KeyError as exc:
            self._send(400, {"error": "validation_error",
                             "message": f"缺少必填字段: {exc.args[0]}"})
        except DomainError as exc:
            self._send(_error_status(exc), exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - 兜底，保证连接有响应
            self._send(500, {"error": "internal_error", "message": str(exc)})
