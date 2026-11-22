"""
确保 127.0.0.1:8765 由本工作台独占。

背景：另一款 demo（main_workbench.py）也会绑 8765，且 Windows 的
SO_REUSEADDR 允许两个进程绑同一地址，后启动者会随机抢走 HTTP 请求，
表现为「打不开了」/ 404。server.py 已改用 SO_EXCLUSIVEADDRUSE 独占端口，
本脚本负责在启动前把「占着端口但不是我们」的进程清掉。

用法：python ensure_port.py 8765
退出码 0 = 端口可用（或本来就是我们的服务）；1 = 清理失败。
"""
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
HOST = "127.0.0.1"


def ours_is_up():
    """我们的服务在线：/api/graph 返回 200（这是本工作台独有的端点）。"""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/graph", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def port_is_free():
    s = socket.socket()
    try:
        s.bind((HOST, PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def listener_pids():
    """从 netstat 找出所有 LISTENING 在 PORT 上的 PID。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return []
    pids = []
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 5 and parts[3].upper() == "LISTENING":
            addr = parts[1]
            if addr.endswith(f":{PORT}") and (addr.startswith(HOST) or addr.startswith("0.0.0.0")):
                try:
                    pids.append(int(parts[4]))
                except ValueError:
                    pass
    return sorted(set(pids))


def kill(pid):
    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                   capture_output=True, timeout=10)


def main():
    if ours_is_up():
        print("our server already up, nothing to do")
        return 0
    for attempt in range(8):
        if port_is_free():
            print("port free")
            return 0
        pids = listener_pids()
        print(f"foreign listener(s) on {PORT}: {pids}")
        # 二次确认不是我们的服务（避免在竞态里误杀刚起来的自己）
        if ours_is_up():
            print("our server came up, nothing to do")
            return 0
        for pid in pids:
            if pid != os.getpid():
                kill(pid)
        time.sleep(0.6)
    ok = port_is_free() or ours_is_up()
    print("port usable" if ok else "FAILED to free port")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
