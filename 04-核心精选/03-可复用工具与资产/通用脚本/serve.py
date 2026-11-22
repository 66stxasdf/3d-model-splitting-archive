"""静态文件服务器：修正 Windows 下 .js/.mjs/.glb 的 MIME 类型。"""
import functools
import http.server
import mimetypes
import os
import sys

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("image/png", ".png")
mimetypes.add_type("image/jpeg", ".jpg")

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

Handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)

class Quiet(Handler.__wrapped__ if hasattr(Handler, "__wrapped__") else http.server.SimpleHTTPRequestHandler):
    pass

import http.server as _h

class Server(_h.ThreadingHTTPServer):
    allow_reuse_address = True

httpd = Server(("127.0.0.1", PORT), Handler)
print(f"serving {ROOT} at http://127.0.0.1:{PORT}", flush=True)
httpd.serve_forever()
