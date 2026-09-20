"""海外短剧制片协作的运行入口。

- ``GET /health``：服务身份健康检查（基线契约）
- ``/api/*``：海外制片协作领域接口，见 :mod:`studio.api`
"""

import argparse
import json
from http.server import ThreadingHTTPServer

from studio.api import ApiHandler

SERVICE_ID = "global-drama-production"
SERVICE_NAME = "海外短剧制片协作"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(ApiHandler):
    """在领域 API 之外保留根级健康检查。"""

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps(health_payload(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
