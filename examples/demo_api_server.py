"""示例后端：一个假的「IT 报修」系统，给 custom_tools.example.json 里的三个工具调用。

    python examples/demo_api_server.py        # 监听 127.0.0.1:8090

真实使用时，把 custom_tools.json 里的地址换成你自己的内部系统即可，这个文件不需要。
"""

from __future__ import annotations

import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

KB = [
    {"id": "KB-101", "title": "VPN 连不上", "summary": "先确认账号没过期，再重装客户端证书；仍不行提交工单。", "internal_note": "二线值班表见运维手册"},
    {"id": "KB-102", "title": "打印机脱机", "summary": "在系统设置里删除打印机后重新添加，驱动选通用 PCL。", "internal_note": ""},
    {"id": "KB-103", "title": "邮箱容量满", "summary": "归档一年前的邮件到本地，或申请扩容。", "internal_note": ""},
    {"id": "KB-104", "title": "申请软件安装权限", "summary": "提交工单并写明软件名称和用途，主管审批后开通。", "internal_note": ""},
]
TICKETS: dict = {}


class Handler(BaseHTTPRequestHandler):
    token = "demo-token"

    def log_message(self, *a):
        pass

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if self.headers.get("X-Api-Token") == self.token:
            return True
        self._send(401, {"error": "token 不对"})
        return False

    def do_GET(self):
        if not self._authed():
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/kb":
            word = (q.get("q") or [""])[0]
            limit = int((q.get("limit") or ["5"])[0])
            hits = [x for x in KB if not word or word in x["title"] or word in x["summary"]]
            return self._send(200, {"total": len(hits), "items": hits[:limit], "took_ms": 3, "trace_id": "demo"})
        if u.path.startswith("/tickets/"):
            t = TICKETS.get(urllib.parse.unquote(u.path.split("/", 2)[2]))
            return self._send(200, t) if t else self._send(404, {"error": "没有这张工单"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return
        if self.path != "/tickets":
            return self._send(404, {"error": "not found"})
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "请求体不是 JSON"})
        if not data.get("title"):
            return self._send(400, {"error": "缺少标题"})
        tid = "T-%04d" % (len(TICKETS) + 1)
        TICKETS[tid] = {"ticket_id": tid, "title": data["title"], "detail": data.get("detail", ""),
                        "urgency": data.get("urgency") or "普通", "status": "待处理"}
        self._send(200, {"code": 0, "data": {
            "ticket_id": tid,
            "summary_md": "**工单 %s 已创建**\n- 标题：%s\n- 紧急程度：%s\n- 状态：待处理" % (
                tid, data["title"], TICKETS[tid]["urgency"]),
        }})


def serve(port: int = 8090, token: str = "") -> HTTPServer:
    Handler.token = token or os.getenv("DEMO_API_TOKEN", "demo-token")
    return HTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    print("示例后端已启动：http://127.0.0.1:8090  (token 取环境变量 DEMO_API_TOKEN，默认 demo-token)")
    serve().serve_forever()
