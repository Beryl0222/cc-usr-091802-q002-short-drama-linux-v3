"""海外短剧制片协作的运行入口。

- ``python3 service.py --check``         基础自检
- ``python3 service.py --port 8000``     启动 HTTP/JSON API（默认 data/production.db）
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_ID = "global-drama-production"
SERVICE_NAME = "海外短剧制片协作"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """仅提供健康检查；业务路由见 production.api。"""

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(health_payload(), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="data/production.db",
                        help="SQLite 数据库路径（:memory: 仅用于测试）")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 进一步确保领域后端可在空库上初始化
        from production.backend import Backend

        Backend(":memory:")
        print("基础检查通过")
        return
    from production.api import serve

    serve(args.db, args.port)


if __name__ == "__main__":
    main()
