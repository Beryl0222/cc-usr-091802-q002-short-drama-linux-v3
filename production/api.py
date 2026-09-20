"""HTTP/JSON API。

路由声明为 (方法, 路径模板, 处理函数名)；模板段以 ``{name}`` 捕获。
所有处理函数签名为 fn(backend, body, **captured)，写操作要求 JSON body。
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .backend import Backend
from .errors import DomainError


# ---------------- 处理函数：直接转调 Backend 领域方法 ----------------

def _f(backend, body, **kw):
    return backend.register_ip(
        title=body["title"], rightsholder=body["rightsholder"],
        ip_id=body.get("ip_id"), actor=body.get("actor", "rights"))


def _project_create(b, body, **kw):
    return b.create_project(
        ip_id=body["ip_id"], title=body["title"], markets=body["markets"],
        allowed_source_materials=body["allowed_source_materials"],
        project_id=body.get("project_id"), actor=body.get("actor", "producer"))


def _team_create(b, body, **kw):
    return b.create_team(body["team_id"], body["name"], body["timezone"])


def _set_create(b, body, **kw):
    return b.create_set(body["set_id"], body["name"], body["timezone"])


def _talent_create(b, body, **kw):
    return b.create_talent(body["talent_id"], body["name"])


def _team_assign(b, body, project_id):
    return b.assign_team(project_id, body["team_id"])


def _license_grant(b, body, project_id):
    return b.grant_license(
        project_id, body["market"], body["language"],
        body["starts_at"], body["expires_at"],
        restricted_materials=body.get("restricted_materials"),
        license_id=body.get("license_id"), actor=body.get("actor", "rights"))


def _license_renew(b, body, license_id):
    return b.renew_license(license_id, body["starts_at"], body["expires_at"],
                           actor=body.get("actor", "rights"))


def _license_revoke(b, body, license_id):
    return b.revoke_license(license_id, body["reason"],
                            actor=body.get("actor", "rights"))


def _draft_create(b, body, project_id):
    return b.create_draft(
        project_id, body["scenes"], body.get("created_by", "writer"),
        based_on_version_id=body.get("based_on_version_id"),
        change_summary=body.get("change_summary", ""),
        target_markets=body.get("target_markets"),
        force=bool(body.get("force", False)))


def _note_add(b, body, version_id):
    return b.add_note(version_id, body["author"], body["author_role"],
                      body["category"], body["body"],
                      scene_ref=body.get("scene_ref"), actor=body.get("actor"))


def _change_add(b, body, version_id):
    return b.add_change(version_id, body["scene_ref"], body["change_type"],
                        body["description"],
                        source_note_id=body.get("source_note_id"),
                        actor=body.get("actor", "writer"))


def _approval_submit(b, body, version_id):
    return b.submit_approval(version_id, body["role"], body["approver"],
                             body.get("decision", "approved"),
                             comment=body.get("comment", ""),
                             actor=body.get("actor"))


def _booking_create(b, body, project_id):
    return b.create_booking(
        project_id, body["scene_ref"], body["version_id"],
        team_id=body["team_id"], set_id=body["set_id"],
        talent_ids=body["talent_ids"], starts_at=body["starts_at"],
        ends_at=body["ends_at"], actor=body.get("actor", "scheduler"))


def _booking_cancel(b, body, booking_id):
    return b.cancel_booking(booking_id, body["reason"],
                            actor=body.get("actor", "scheduler"))


def _shot_create(b, body, booking_id):
    return b.record_shot(booking_id, body["material_refs"],
                         shot_id=body.get("shot_id"),
                         actor=body.get("actor", "crew"))


def _asset_create(b, body, project_id):
    return b.create_asset(
        project_id, body["kind"], body["content_hash"],
        body.get("created_by", "producer"), language=body.get("language"),
        market=body.get("market"), parent_asset_id=body.get("parent_asset_id"),
        root_shot_ids=body.get("root_shot_ids"),
        version_id=body.get("version_id"))


def _release_schedule(b, body, project_id):
    return b.schedule_release(
        project_id, body["asset_id"], body["market"], body["language"],
        body["platform"], body["scheduled_at"],
        actor=body.get("actor", "distributor"))


def _release_publish(b, body, release_id):
    return b.publish_release(release_id, actor=body.get("actor", "distributor"))


def _receipt_create(b, body, release_id):
    return b.record_receipt(
        release_id, body["platform"], body["period_start"],
        body["period_end"], body["amount"], body["currency"],
        body["idempotency_key"], payload=body.get("payload"),
        actor=body.get("actor", "platform"))


# (method, 段模板, 函数, 是否需要 JSON body)
ROUTES = [
    ("POST", ["api", "ips"], _f, True),
    ("GET", ["api", "projects", "{project_id}"],
        lambda b, body, project_id: b.get_project(project_id), False),
    ("POST", ["api", "projects"], _project_create, True),
    ("POST", ["api", "teams"], _team_create, True),
    ("POST", ["api", "sets"], _set_create, True),
    ("POST", ["api", "talent"], _talent_create, True),
    ("POST", ["api", "projects", "{project_id}", "teams"], _team_assign, True),
    ("POST", ["api", "projects", "{project_id}", "licenses"], _license_grant, True),
    ("GET", ["api", "projects", "{project_id}", "licenses"],
        lambda b, body, project_id: {"licenses": b.list_licenses(project_id)}, False),
    ("GET", ["api", "licenses", "{license_id}"],
        lambda b, body, license_id: b.get_license(license_id), False),
    ("POST", ["api", "licenses", "{license_id}", "renew"], _license_renew, True),
    ("POST", ["api", "licenses", "{license_id}", "revoke"], _license_revoke, True),
    ("POST", ["api", "projects", "{project_id}", "scripts"], _draft_create, True),
    ("GET", ["api", "projects", "{project_id}", "scripts"],
        lambda b, body, project_id: {"versions": b.list_versions(project_id)}, False),
    ("GET", ["api", "scripts", "{version_id}"],
        lambda b, body, version_id: b.get_version(version_id), False),
    ("GET", ["api", "scripts", "{version_id}", "gate"],
        lambda b, body, version_id: b.gate_status(version_id), False),
    ("POST", ["api", "scripts", "{version_id}", "notes"], _note_add, True),
    ("POST", ["api", "scripts", "{version_id}", "changes"], _change_add, True),
    ("POST", ["api", "scripts", "{version_id}", "approvals"], _approval_submit, True),
    ("POST", ["api", "projects", "{project_id}", "bookings"], _booking_create, True),
    ("GET", ["api", "projects", "{project_id}", "bookings"],
        lambda b, body, project_id: {"bookings": b.list_bookings(project_id)}, False),
    ("GET", ["api", "bookings", "{booking_id}"],
        lambda b, body, booking_id: b.get_booking(booking_id), False),
    ("POST", ["api", "bookings", "{booking_id}", "cancel"], _booking_cancel, True),
    ("POST", ["api", "bookings", "{booking_id}", "shots"], _shot_create, True),
    ("GET", ["api", "shots", "{shot_id}"],
        lambda b, body, shot_id: b.get_shot(shot_id), False),
    ("GET", ["api", "projects", "{project_id}", "shots"],
        lambda b, body, project_id: {"shots": b.list_shots(project_id)}, False),
    ("POST", ["api", "projects", "{project_id}", "assets"], _asset_create, True),
    ("GET", ["api", "projects", "{project_id}", "assets"],
        lambda b, body, project_id, **q:
            {"assets": b.list_assets(project_id, kind=q.get("kind"))}, False),
    ("GET", ["api", "assets", "{asset_id}"],
        lambda b, body, asset_id: b.get_asset(asset_id), False),
    ("GET", ["api", "assets", "{asset_id}", "lineage"],
        lambda b, body, asset_id: b.asset_lineage(asset_id), False),
    ("POST", ["api", "projects", "{project_id}", "releases"], _release_schedule, True),
    ("GET", ["api", "projects", "{project_id}", "releases"],
        lambda b, body, project_id, **q:
            {"releases": b.list_releases(project_id, market=q.get("market"))}, False),
    ("GET", ["api", "releases", "{release_id}"],
        lambda b, body, release_id: b.get_release(release_id), False),
    ("POST", ["api", "releases", "{release_id}", "publish"], _release_publish, True),
    ("POST", ["api", "releases", "{release_id}", "receipts"], _receipt_create, True),
    ("GET", ["api", "releases", "{release_id}", "receipts"],
        lambda b, body, release_id: {"receipts": b.list_receipts(release_id)}, False),
    ("GET", ["api", "releases", "{release_id}", "trace"],
        lambda b, body, release_id: b.dispute_trace(release_id), False),
    ("GET", ["api", "projects", "{project_id}", "accounts"],
        lambda b, body, project_id: b.market_accounts(project_id), False),
    ("GET", ["api", "projects", "{project_id}", "events"],
        lambda b, body, project_id, **q:
            {"events": b.list_events(project_id, event_type=q.get("type"))}, False),
]


def _match(method, segments):
    for m, template, fn, needs_body in ROUTES:
        if m != method or len(template) != len(segments):
            continue
        captured = {}
        for t, s in zip(template, segments):
            if t.startswith("{") and t.endswith("}"):
                captured[t[1:-1]] = s
            elif t != s:
                break
        else:
            return fn, needs_body, captured
    return None


def make_handler(backend):
    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "GlobalDramaBackend/1.0"

        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            from urllib.parse import parse_qs, urlsplit

            parts = urlsplit(self.path)
            if parts.path == "/health":
                from service import health_payload

                self._send(200, health_payload())
                return
            segments = [s for s in parts.path.strip("/").split("/") if s]
            match = _match(method, segments)
            if match is None:
                self._send(404, {"error": "not_found",
                                 "message": f"无此路由: {method} {parts.path}"})
                return
            fn, needs_body, captured = match
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            captured["q"] = query
            body = {}
            if needs_body:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    body = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(body, dict):
                        raise ValueError
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"error": "bad_json",
                                     "message": "请求体必须是 JSON 对象"})
                    return
            try:
                import inspect

                kwargs = dict(captured)
                # 仅向显式声明 q 的处理函数传查询参数
                if "q" not in inspect.signature(fn).parameters:
                    kwargs.pop("q", None)
                result = fn(backend, body, **kwargs)
            except DomainError as exc:
                self._send(exc.http_status, exc.to_dict())
                return
            self._send(200, result if result is not None else {"ok": True})

        def log_message(self, *_args):
            return

    return ApiHandler


def serve(path, port):
    backend = Backend(path)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(backend)).serve_forever()
