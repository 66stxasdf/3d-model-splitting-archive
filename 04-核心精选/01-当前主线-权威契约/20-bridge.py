# -*- coding: utf-8 -*-
"""可视化工作台 ↔ 真内核的本地桥（可选）。

只监听 127.0.0.1:8765，不联网、不上传、不写共享目录。
不启动它，Agent工作台.html 双击也能用（真实轨迹回放模式）。

路由：
  GET /                        托管同目录的 Agent工作台.html（同源，无跨域问题）
  GET /api/health              内核版本与状态
  GET /api/plan?goal=...       真的调 agentcore.loop.preview 做规划
  GET /api/run?pack=...        真的跑一次 Runner，返回结局摘要（会写 runs/）

启动： python bridge.py
"""
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))

# 内核可能在产物主目录的兄弟目录，也可能在固定绝对路径（桌面副本的情况）
_CANDIDATES = [
    os.path.join(os.path.dirname(HERE), "18-分件Agent内核"),
    r"D:\workbuddy产物\18-分件Agent内核",
]
KERNEL = next((p for p in _CANDIDATES if os.path.isdir(p)), None)
if KERNEL is None:
    print("找不到内核目录，试过：")
    for p in _CANDIDATES:
        print("  ", p)
    sys.exit(2)

sys.path.insert(0, KERNEL)
os.chdir(KERNEL)

from agentcore import loop as L          # noqa: E402
from agentcore import pack as PK         # noqa: E402
from agentcore import task as T          # noqa: E402

PORT = 8765


def do_plan(q):
    goal = (q.get("goal") or ["parts_available"])[0]
    seam = (q.get("seam") or [""])[0]
    pack_name = (q.get("pack") or ["split3d"])[0]
    p = PK.load(os.path.join("packs", pack_name))
    inputs = {"model": "model.stl"}
    if seam:
        inputs["seam"] = seam
    t = T.parse(dict(inputs=inputs, goals=[goal]), p)
    steps = L.preview(p, t, max_steps=16)
    return {"pack": pack_name, "goal": goal, "seam": bool(seam),
            "steps": steps, "kernel": KERNEL}


def do_run(q):
    pack_name = (q.get("pack") or ["split3d"])[0]
    goal = (q.get("goal") or ["parts"])[0]
    p = PK.load(os.path.join("packs", pack_name))
    obj = dict(inputs={"model": os.path.abspath("fixtures/sample_small.txt")},
               goals=[goal], budget=dict(max_steps=12, max_wall_s=30))
    t = T.parse(obj, p)
    import time
    run_dir = os.path.join(KERNEL, "runs_bridge", time.strftime("%H%M%S"))
    r = L.Runner(p, t, run_dir=run_dir).run()
    return {"verdict": r.get("verdict"), "code": r.get("reason_code"),
            "steps": r.get("agent", {}).get("steps"),
            "reasons": r.get("reasons"), "run_dir": r.get("run_dir")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[bridge] " + (fmt % args) + "\n")

    def _cors(self):
        # file:// 打开的页面 Origin 是 null；允许它读，也处理 Chrome 的私有网络预检
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path):
        if not os.path.isfile(path):
            self._json({"error": "同目录找不到 Agent工作台.html"}, 404)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self._cors()
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html", "/Agent工作台.html"):
                # 优先用桥所在目录的 HTML（桌面副本也一样）
                self._html(os.path.join(HERE, "Agent工作台.html"))
            elif u.path == "/api/health":
                self._json({"ok": True, "kernel": KERNEL,
                            "kernel_version": "0.2.0", "port": PORT})
            elif u.path == "/api/plan":
                self._json(do_plan(q))
            elif u.path == "/api/run":
                self._json(do_run(q))
            elif u.path == "/favicon.ico":
                self.send_response(204); self.end_headers()
            else:
                self._json({"error": "未知路径 " + u.path}, 404)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)


if __name__ == "__main__":
    print("内核:", KERNEL)
    print("页面: http://127.0.0.1:%d/" % PORT)
    print("Ctrl+C 结束。这个服务只监听本机，不联网。")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
