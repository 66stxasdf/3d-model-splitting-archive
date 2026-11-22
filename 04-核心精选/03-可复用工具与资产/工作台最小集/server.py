"""
多模型智能体工作台 —— 核心零第三方依赖；仅 /api/export 产物导出用到
可选文档库（python-docx / openpyxl / python-pptx / reportlab / Pillow）。
功能：
  1) 模型库：可以注册任意多家模型（OpenAI 兼容接口 / 本地 ollama / 演示用 mock）
  2) 团队：给每个 agent 单独指定模型、角色、允许使用的工具
  3) 任务：输入一句话 → 规划 → 分工执行 → 代码验收 → 不合格带诊断回退
  4) 全程落盘：runs/<id>.json + trace.jsonl，随时可回放

启动： python server.py            （默认 http://127.0.0.1:8765，本机模式）
       python server.py 9000       （指定端口）
上线： 设置 WORKBENCH_TOKEN（≥16 字符）后再启动，详见 security.py 顶部说明

安全与隐私由 security.py 统一提供：Key 加密存储、访问鉴权、Host/Origin 校验、
请求体与限流、SSRF 防护、错误脱敏、审计日志、数据清除与留存清理。
"""
import json, os, re, sys, time, threading, urllib.request, urllib.error, traceback, subprocess, hmac
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

import exporters  # 结果多格式导出（产物落 D:/workbuddy产物/workbench）
import security   # 安全与隐私层

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
GRAPH = HERE / "graph.json"
PREVIEWS = HERE / "previews"
RUNS_DIR = HERE / "runs"
ASSET_ROOT = Path(r"D:\workbuddy产物")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(
    os.environ.get("WORKBENCH_PORT", "8765"))

RUNS_DIR.mkdir(exist_ok=True)
LOCK = threading.RLock()
RUNS = {}          # run_id -> {"events": [...], "done": bool, "verdict": str, "task": str}
ABORT = set()      # run_id 需要中止

# ---------------------------------------------------------------- 配置
DEFAULT_CONFIG = {
    "models": [
        {"id": "mock-fast", "name": "演示模型 A（快）", "provider": "mock",
         "base_url": "", "model": "mock-a", "api_key": "", "note": "无需网络，规则模拟"},
        {"id": "mock-deep", "name": "演示模型 B（慢）", "provider": "mock",
         "base_url": "", "model": "mock-b", "api_key": "", "note": "无需网络，规则模拟"},
        {"id": "deepseek", "name": "DeepSeek", "provider": "openai",
         "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
         "api_key": "", "note": "填 key 后即可真调"},
        {"id": "ollama", "name": "本地 Ollama", "provider": "ollama",
         "base_url": "http://127.0.0.1:11434", "model": "qwen2.5:7b",
         "api_key": "", "note": "本机有 ollama 就能用"},
    ],
    "agents": [
        {"id": "planner", "name": "规划者", "role": "planner", "model_id": "mock-deep",
         "sys": "你负责把用户任务拆成可执行的步骤，每步必须指明由哪个 agent 用哪个工具完成。只输出 JSON。",
         "tools": []},
        {"id": "qc", "name": "质检员", "role": "worker", "model_id": "mock-fast",
         "sys": "你负责网格入厂质检，判断是假缝还是真破洞。",
         "tools": ["mesh_qc", "asset_list"]},
        {"id": "splitter", "name": "切分员", "role": "worker", "model_id": "mock-fast",
         "sys": "你负责按指定位置把网格切成两件，并保证两件都水密。",
         "tools": ["slice_two", "asset_list"]},
        {"id": "reviewer", "name": "验收员", "role": "reviewer", "model_id": "mock-deep",
         "sys": "你不做计算，只汇总代码验收结果并给出结论。",
         "tools": []},
    ],
}

def load_config():
    cfg = None
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            pass
    if not isinstance(cfg, dict):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    # 历史版本把 Key 明文放在 config.json：读到即迁移进加密库，并把字段彻底抹掉
    has_field = any(isinstance(m, dict) and "api_key" in m
                    for m in (cfg.get("models") or []))
    if has_field:
        with LOCK:
            moved = security.STORE.migrate_from_config(cfg)
            _write_config(cfg)
            if moved:
                security.audit("key_migrate", "local", "BOOT", "/", 200,
                               "moved=%s" % moved)
    cfg = normalize_cfg(cfg)
    normalize_team(cfg)
    return cfg


def normalize_cfg(cfg):
    """Old configs lack manager / workers; fill them so delegation always works."""
    cfg = cfg or {}
    models = cfg.get("models") or []
    cfg["models"] = models
    ids = [m.get("id") for m in models]
    def pick(*wanted):
        for w in wanted:
            if w in ids:
                return w
        return ids[0] if ids else ""
    if not isinstance(cfg.get("manager"), dict):
        cfg["manager"] = {"model_id": pick("mock-deep"), "label": "管理者模型"}
    if not isinstance(cfg.get("workers"), list) or not cfg["workers"]:
        # 兼容旧 /api/run、/api/delegate：这里只是兜底占位，新版界面只用 cfg["team"]
        cfg["workers"] = [
            {"id": "w1", "name": "子 Agent 1", "model_id": pick("mock-fast"),
             "duty": "完成规划者分派的子任务"},
            {"id": "w2", "name": "子 Agent 2", "model_id": pick("mock-deep"),
             "duty": "完成规划者分派的子任务"},
        ]
    # 主从自主编排预算（旧配置没有就补默认值）
    o = cfg.get("orchestration")
    if not isinstance(o, dict):
        o = {}
    o.setdefault("max_rounds", 3)
    o.setdefault("max_subtasks", 12)
    o.setdefault("max_seconds", 180)
    o.setdefault("parallel", True)
    o.setdefault("step_delay", 0.45)
    o.setdefault("label", "主从自主编排（GPT Pro / Deep Research 式）")
    cfg["orchestration"] = o
    return cfg


# ---------------------------------------------------------------- 协作团队（规划者 + 子 Agent）
# 新版界面只认 cfg["team"]：每个节点是一个 Agent，各自拥有独立的 API 接口条目。
# 旧版 agents / manager / workers 原样保留，只作为 /api/run、/api/delegate 的兼容通道。
TEAM_DEFAULT = [
    {"id": "pl-1", "kind": "planner", "name": "规划者 1", "lead": True,
     "duty": "分析任务、翻译成子 Agent 能执行的指令、复核并汇总结果",
     "x": 60, "y": 60,
     "params": {"max_rounds": 2, "review": True, "allow_handoff": True}},
    {"id": "wk-1", "kind": "worker", "name": "子 Agent 1", "planner_id": "pl-1",
     "duty": "识别执行指令，独立思考并交付可复核的结果",
     "x": 430, "y": 60,
     "params": {"temperature": 0.3, "max_tokens": 2048, "allow_handoff": True}},
    {"id": "wk-2", "kind": "worker", "name": "子 Agent 2", "planner_id": "pl-1",
     "duty": "识别执行指令，独立思考并交付可复核的结果",
     "x": 430, "y": 330,
     "params": {"temperature": 0.3, "max_tokens": 2048, "allow_handoff": True}},
]
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


def _agent_model_defaults(kind):
    return {"provider": "mock", "base_url": "",
            "model": "demo-planner" if kind == "planner" else "demo-worker",
            "note": "节点独立接口（Key 按此 ID 加密保存）"}


def normalize_team(cfg):
    """保证 team 里每个 Agent 都有唯一的模型接口条目，且上下级连线合法。

    模型 ID 固定为 "ag-" + agent.id，/api/key 就按这个 ID 把 Key 写进加密库，
    因此「节点标签上的 API」天然是逐节点隔离的，绝不会写进 config.json。
    """
    models = cfg.setdefault("models", [])
    team = cfg.get("team")
    if not isinstance(team, list) or not team:
        team = json.loads(json.dumps(TEAM_DEFAULT))
    clean, seen = [], set()
    for a in team[:60]:
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "").strip()
        if not AGENT_ID_RE.match(aid) or aid in seen:
            continue
        seen.add(aid)
        kind = "planner" if str(a.get("kind")) == "planner" else "worker"
        mid = "ag-" + aid
        entry = next((m for m in models
                      if isinstance(m, dict) and m.get("id") == mid), None)
        if entry is None:
            entry = dict(_agent_model_defaults(kind))
            entry.update({"id": mid,
                          "name": str(a.get("name") or aid) + " · 接口"})
            models.append(entry)
        p = a.get("params") if isinstance(a.get("params"), dict) else {}
        try:
            x = max(0, min(6000, int(float(a.get("x", 0) or 0))))
            y = max(0, min(6000, int(float(a.get("y", 0) or 0))))
        except (TypeError, ValueError):
            x = y = 0
        item = {
            "id": aid, "kind": kind,
            "name": str(a.get("name") or ("规划者" if kind == "planner" else "子 Agent"))[:40],
            "model_id": mid,
            "duty": str(a.get("duty") or "")[:400],
            "x": x, "y": y, "params": p,
        }
        if kind == "worker":
            item["planner_id"] = str(a.get("planner_id") or "")[:24]
        else:
            item["lead"] = bool(a.get("lead"))
        clean.append(item)
    # 兜底：至少一个规划者、至少一个子 Agent
    if not any(a["kind"] == "planner" for a in clean):
        clean.insert(0, json.loads(json.dumps(TEAM_DEFAULT[0])))
    if not any(a["kind"] == "worker" for a in clean):
        clean.append(json.loads(json.dumps(TEAM_DEFAULT[1])))
    planners = [a for a in clean if a["kind"] == "planner"]
    for i, p in enumerate(planners):
        p["lead"] = (i == 0)                 # 第一个规划者负责最终复核汇总
    pids = [p["id"] for p in planners]
    for a in clean:
        if a["kind"] == "worker" and a.get("planner_id") not in pids:
            a["planner_id"] = pids[0]
    # 补一遍接口条目（覆盖上面兜底插入的节点）
    for a in clean:
        mid = a.get("model_id") or ("ag-" + a["id"])
        a["model_id"] = mid
        if not any(isinstance(m, dict) and m.get("id") == mid for m in models):
            entry = dict(_agent_model_defaults(a["kind"]))
            entry.update({"id": mid, "name": a["name"] + " · 接口"})
            models.append(entry)
    cfg["team"] = clean
    return clean


# ---------------------------------------------------------------- 画布连线 → 协作关系
# 端口格式：<节点ID>|in|out|<端口名>；连线是「输出端口 → 另一个节点的输入端口」。
SOCKET_RE = re.compile(r"^([A-Za-z0-9_-]{1,24})\|(in|out)\|([A-Za-z0-9_]{1,16})$")
LINK_KIND_CN = {
    "dispatch": "规划者分派执行指令", "relay": "规划者级联分析",
    "handoff": "Agent 转交产出（接力协作）", "feedback": "回传规划者复核",
    "result": "纳入结果汇总", "review": "复核结论进入结果",
}


def classify_link(a, b):
    """与前端 linkKind() 保持一致的端口兼容规则；返回连线语义或 None。"""
    pa, pb = SOCKET_RE.match(str(a or "")), SOCKET_RE.match(str(b or ""))
    if not pa or not pb:
        return None
    na, da, ka = pa.groups()
    nb, db, kb = pb.groups()
    if da != "out" or db != "in" or na == nb:
        return None
    if ka == "dispatch" and kb == "instruction":
        return "dispatch"
    if ka == "dispatch" and kb == "task":
        return "relay"
    if ka == "result" and kb == "instruction":
        return "handoff"
    if ka == "result" and kb == "task":
        return "feedback"
    if ka == "result" and kb.startswith("r"):
        return "result"
    if ka == "review" and kb == "review":
        return "review"
    if ka == "review" and kb in ("memory", "task", "instruction"):
        return "handoff"
    if ka == "memory" and kb == "memory":
        return "handoff"
    return None


def effective_links(cfg, team=None):
    """把画布上的结构连线（自动）+ 用户拖出来的连线（cfg.links）- 断开记录（cfg.cuts）
    合并成后端真正使用的协作关系。配置里没有 links/cuts 时行为与旧版完全一致。"""
    team = team if team is not None else normalize_team(cfg)
    planners = [a for a in team if a["kind"] == "planner"]
    workers = [a for a in team if a["kind"] == "worker"]
    node_ids = {a["id"] for a in team} | {"result"}
    cuts = {str(x)[:120] for x in (cfg.get("cuts") or [])[:200]
            if isinstance(x, (str, int))}
    out, seen = [], set()

    def add(lid, a, b, kind):
        if lid in cuts:
            return
        src, dst = str(a).split("|")[0], str(b).split("|")[0]
        if src not in node_ids or dst not in node_ids:
            return
        key = (a, b)
        if key in seen:
            return
        seen.add(key)
        out.append({"id": lid, "a": a, "b": b, "kind": kind,
                    "src": src, "dst": dst})

    for i, w in enumerate(workers):
        add("auto:pw:" + w["id"],
            "%s|out|dispatch" % w["planner_id"], "%s|in|instruction" % w["id"], "dispatch")
        add("auto:wr:" + w["id"],
            "%s|out|result" % w["id"], "result|in|r%d" % i, "result")
    if planners:
        add("auto:pr:lead", "%s|out|review" % planners[0]["id"],
            "result|in|review", "review")
    for i, l in enumerate((cfg.get("links") or [])[:200]):
        if not isinstance(l, dict):
            continue
        a, b = str(l.get("a") or ""), str(l.get("b") or "")
        kind = classify_link(a, b)
        if kind:
            add("usr:%d:%s:%s" % (i, a, b), a, b, kind)
    return out


def _latest_worker_text(outs, wid):
    for o in reversed(outs):
        if o.get("to") == wid or o.get("id") == wid:
            return str(o.get("text") or "")
    return ""


def _write_config(cfg):
    """原子写 + 收紧文件权限，避免并发写坏配置。"""
    tmp = CONFIG.with_name(CONFIG.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, CONFIG)
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass


def save_config(cfg):
    """任何落盘配置都必须先消毒：字段/长度校验、SSRF 校验、剥离 api_key。"""
    clean = security.validate_config(cfg)
    with LOCK:
        _write_config(clean)
    return clean

# ---------------------------------------------------------------- 工作流图（ComfyUI 式：节点 + 连线）
DEFAULT_GRAPH = {
    "nodes": [
        {"id": "in", "type": "input", "title": "任务", "x": 40, "y": 320,
         "params": {"task": "检查 astronaut_kid.glb 的入厂状态，把它切成两件", "require_asset": True}},
        {"id": "probe", "type": "tool", "title": "模型特征", "x": 320, "y": 60,
         "params": {"tool": "asset_profile", "args": {"path": "$asset"}}},
        {"id": "pv1", "type": "tool", "title": "预览·入厂", "x": 320, "y": 560,
         "params": {"tool": "render_preview",
                    "args": {"path": "$asset", "mode": "solid", "caption": "入厂状态（焊合后）"}}},
        {"id": "plan", "type": "agent", "title": "规划者·思考推敲", "x": 660, "y": 60,
         "params": {"model_id": "mock-deep",
                    "sys": ("你是拆件规划者。动刀前先想清楚，把推敲过程写在 analysis 里：\n"
                            "1. 这件东西是什么形状？最长的边是哪一维（就是切面法向的候选）\n"
                            "2. 从哪里掰开最不显眼？切完两件是不是都还立得住\n"
                            "3. 有什么风险：薄壁、破洞、纹理会被切开\n"
                            "然后给 split_plan（axis 切面法向：0=X、1=Y、2=Z；height_ratio 0~1 切面高度占比）\n"
                            "再把要做的事拆成 subtasks 清单，每条写 title 和 why。\n"
                            "只回答一个 JSON，例如：\n"
                            "{\"analysis\": \"这是一个站立的人形，最长边是 Y…\", \n"
                            " \"split_plan\": {\"axis\": 1, \"height_ratio\": 0.5}, \n"
                            " \"subtasks\": [{\"id\": \"T1\", \"title\": \"先质检看网格干不干净\", "
                            "\"tool\": \"mesh_qc\", \"why\": \"有破洞直接切会崩\"}]}"),
                    "prompt": "看任务和模型特征，先想，再拆子任务。没有特别要求就沿最长边、从正中间切。"}},
        {"id": "split", "type": "code", "title": "拆解子任务", "x": 960, "y": 60,
         "params": {"fn": "build_subtasks"}},
        {"id": "assign", "type": "agent", "title": "分派员·结合特征", "x": 1260, "y": 60,
         "params": {"model_id": "mock-fast",
                    "sys": ("你是分派员。上游给了子任务清单和模型特征，你要结合特征把活派下去：\n"
                            "· 先质检再切：网格不干净就先修，别带着破洞切\n"
                            "· 切面法向用上游根据特征算出来的 suggested_axis（最长边方向），不要自己另选\n"
                            "· 每个子任务都要指明交给哪个工具，并写一句 why\n"
                            "只回答一个 JSON，例如：\n"
                            "{\"axis\": 1, \"height\": null, \"suggested_axis\": 1, "
                            "\"assignments\": [{\"id\": \"T1\", \"to\": \"mesh_qc\", "
                            "\"why\": \"先确认水密\"}], "
                            "\"why\": \"按最长边 Y 从中间切，头身各成一件\"}"),
                    "prompt": "结合模型特征，把子任务分给具体工具，并给出切分参数。"}},
        {"id": "qc", "type": "tool", "title": "入厂质检", "x": 1560, "y": 60,
         "params": {"tool": "mesh_qc", "args": {"path": "$asset"}}},
        {"id": "sl", "type": "tool", "title": "切成两件", "x": 1560, "y": 520,
         "params": {"tool": "slice_two", "args": {"path": "$asset", "axis": "$axis", "height": "$height"}}},
        {"id": "pv2", "type": "tool", "title": "预览·切分", "x": 1560, "y": 900,
         "params": {"tool": "render_preview",
                    "args": {"path": "$asset", "mode": "split", "axis": "$axis", "height": "$height",
                             "caption": "切分结果：蓝=上半，橙=下半"}}},
        {"id": "gate", "type": "gate", "title": "代码闸门", "x": 1860, "y": 320, "params": {}},
        {"id": "out", "type": "output", "title": "结果", "x": 2140, "y": 320, "params": {}},
    ],
    "links": [{"from": "in", "to": "probe"}, {"from": "in", "to": "plan"},
              {"from": "probe", "to": "plan"}, {"from": "probe", "to": "pv1"},
              {"from": "plan", "to": "split"}, {"from": "probe", "to": "split"},
              {"from": "split", "to": "assign"}, {"from": "probe", "to": "assign"},
              {"from": "assign", "to": "qc"}, {"from": "assign", "to": "sl"},
              {"from": "sl", "to": "pv2"},
              {"from": "qc", "to": "gate"}, {"from": "sl", "to": "gate"},
              {"from": "gate", "to": "out"}],
}


def load_graph():
    if GRAPH.exists():
        try:
            g = json.loads(GRAPH.read_text(encoding="utf-8"))
            if isinstance(g.get("nodes"), list):
                return g
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_GRAPH))

def save_graph(g):
    GRAPH.write_text(json.dumps(g, ensure_ascii=False, indent=1), encoding="utf-8")

# ---------------------------------------------------------------- 工具层（确定性计算）
def _trimesh():
    import trimesh
    return trimesh

_GEOM_CACHE = {}
_GEOM_LOCK = threading.Lock()

def _geom(path):
    """一次运行里同一份模型只加载/焊合一次（按 mtime+size 缓存）。返回原始 + 焊后 + 边统计。"""
    trimesh = _trimesh()
    import numpy as np
    key = os.path.abspath(str(path))
    st = os.stat(key)
    sig = (st.st_mtime_ns, st.st_size)
    with _GEOM_LOCK:
        hit = _GEOM_CACHE.get(key)
    if hit and hit["sig"] == sig:
        return {"raw": hit["raw"].copy(), "welded": hit["welded"].copy(),
                "bnd": hit["bnd"], "nm": hit["nm"], "comps": hit["comps"], "cached": True}
    raw = trimesh.load(key, force="mesh")
    welded = raw.copy()
    welded.merge_vertices(merge_tex=True, merge_norm=True)
    uniq, inv = np.unique(welded.edges_sorted, axis=0, return_inverse=True)
    cnt = np.bincount(inv)
    bnd = int((cnt == 1).sum()); nm = int((cnt > 2).sum())
    comps = int(len(trimesh.graph.connected_components(
        welded.face_adjacency, nodes=np.arange(len(welded.faces)), min_len=1, engine="scipy")))
    entry = {"sig": sig, "raw": raw, "welded": welded, "bnd": bnd, "nm": nm, "comps": comps}
    with _GEOM_LOCK:
        _GEOM_CACHE[key] = entry
        while len(_GEOM_CACHE) > 6:
            _GEOM_CACHE.pop(next(iter(_GEOM_CACHE)))
    return {"raw": raw.copy(), "welded": welded.copy(), "bnd": bnd, "nm": nm,
            "comps": comps, "cached": False}

def tool_asset_profile(path, nozzle_mm=0.4, target_mm=200.0):
    """量一件模型的"特征"：尺寸、最长边、壁厚、网格状态。分配环节据此决定怎么切。"""
    trimesh = _trimesh()
    import numpy as np
    g = _geom(path)
    m = g["welded"]
    b = m.bounds
    ext = (b[1] - b[0]).astype(float)
    diag = float(np.linalg.norm(ext))
    area = float(m.area); vol = float(abs(m.volume))
    bnd, nm, comps = g["bnd"], g["nm"], g["comps"]
    order = np.argsort(ext)[::-1]
    longest = "XYZ"[int(order[0])]
    thin = 2.0 * vol / max(area, 1e-12)          # 平均壁厚估计（体积/表面积 ×2）
    scale = (target_mm / diag) if diag > 0 else 0.0   # 假定成品最长边 = target_mm
    try:
        mean_edge_mm = float(np.mean(m.edges_unique_length)) * scale
    except Exception:
        mean_edge_mm = 0.0
    # 切面候选：真的去和网格求交（切片线长度 = 切口周长），再按"周长 × 偏心惩罚"打分。
    # 只扫每个轴 30%~70% 的高度带：太靠边的切法周长天然小，但只会削下一片薄壳，没有意义。
    cands = []
    for ax in range(3):
        lo_, hi_ = float(b[0][ax]), float(b[1][ax])
        if hi_ - lo_ < 1e-9:
            continue
        nv = [0.0, 0.0, 0.0]; nv[ax] = 1.0
        nv = np.array(nv)
        for ratio in np.linspace(0.30, 0.70, 9):
            h = lo_ + (hi_ - lo_) * ratio
            try:
                seg = trimesh.intersections.mesh_plane(m, nv, nv * h)
                per = float(np.linalg.norm(seg[:, 1] - seg[:, 0], axis=1).sum()) if len(seg) else 0.0
            except Exception:
                per = 0.0
            if per <= 0:
                continue
            # 偏心惩罚：越接近正中越划算（两件体积接近，好摆放）
            cost = per * (1.0 + 2.0 * abs(ratio - 0.5))
            cands.append({"axis": ax, "axis_name": "XYZ"[ax],
                          "height": round(h, 4), "height_ratio": round(float(ratio), 3),
                          "perimeter": round(per, 3), "cost": round(cost, 3)})
    best = min(cands, key=lambda c: c["cost"]) if cands else None
    # 先回答最根本的问题：整件能不能直接打？能打就不该切（少一刀少一条痕）。
    bed_mm = 220.0
    fits_whole = bool(max(ext) * scale <= bed_mm) if scale else False
    advice = [f"最长边是 {longest}（{ext[order[0]]:.3f} 单位），切面法向优先取它，两件尺寸更均衡"]
    if best and not fits_whole:
        same_axis = [c for c in cands if c["axis"] == best["axis"]]
        advice.append(f"最省的切面在 {best['axis_name']} 轴 {best['height_ratio']*100:.0f}% 高度处，"
                      f"切口周长 {best['perimeter']}（该轴最好 {best['perimeter']}，最差 "
                      f"{max(c['perimeter'] for c in same_axis)}）")
    elif best:
        advice.append(f"整件缩放后最长边 {max(ext)*scale:.0f} mm，能放进 {bed_mm:.0f} mm 的床，"
                      f"不必切；真要切，最省的一刀在 {best['axis_name']} 轴 "
                      f"{best['height_ratio']*100:.0f}% 高度（切口周长 {best['perimeter']}）")
    if thin * scale < 0.8:
        advice.append(f"等效厚度约 {thin*scale:.2f} mm，切完边缘会很脆")
    if not bool(m.is_watertight):
        advice.append("焊合后仍不水密，先修再切，否则切面补不严")
    if nm:
        advice.append(f"有 {nm} 条非流形边（双重面/薄板），切片算法可能在这里出错")
    return {"kind": "profile", "path": path,
            "faces": len(m.faces), "vertices": len(m.vertices),
            "size": {"X": round(float(ext[0]), 4), "Y": round(float(ext[1]), 4),
                     "Z": round(float(ext[2]), 4)},
            "diag": round(diag, 4), "longest_axis": longest,
            "axis_lengths_desc": [["XYZ"[int(i)], round(float(ext[i]), 4)] for i in order],
            "aspect": round(float(ext[order[0]] / max(ext[order[-1]], 1e-12)), 2),
            "surface_area": round(area, 4), "volume": round(vol, 6),
            "equiv_thickness_mm": round(thin * scale, 3), "mean_edge_mm": round(mean_edge_mm, 4),
            "watertight": bool(m.is_watertight), "boundary_edges": bnd, "nonmanifold_edges": nm,
            "components": comps,
            "scale_to_mm": round(float(scale), 4),
            "cut_candidates": cands, "best_cut": best,
            "fits_whole": fits_whole, "bed_mm": bed_mm,
            "advice": advice, "pass": True}

_SAMPLE_CACHE = {}

def _shade_points(meshes, colors, size=520, samples=1200000):
    """把若干网格画成一张图：表面采样点 + z 缓冲 + 平行光，纯 numpy + PIL，不依赖 OpenGL。"""
    trimesh = _trimesh()
    import numpy as np
    from PIL import Image
    S = int(size * 1.6)                       # 超采样，缩小后更干净
    total = sum(max(1, len(mm.faces)) for mm in meshes)
    P_, N_, C_ = [], [], []
    bs = np.array([mm.bounds for mm in meshes])
    lo, hi = bs[:, 0, :].min(0), bs[:, 1, :].max(0)
    center, extent = (lo + hi) / 2.0, float(np.max(hi - lo))
    for i, mm in enumerate(meshes):
        n_s = max(60000, int(samples * max(1, len(mm.faces)) / total))
        p, fi = trimesh.sample.sample_surface(mm, n_s)
        p = p.copy()
        p[:, 0] += (i - (len(meshes) - 1) / 2.0) * extent * 1.12
        P_.append(p); N_.append(mm.face_normals[fi])
        C_.append(np.tile(colors[i], (len(p), 1)))
    Pp = np.vstack(P_); Nn = np.vstack(N_); Cc = np.vstack(C_).astype(float)

    az, el = np.radians(28), np.radians(14)   # 绕 Y 轴转方位，绕 X 轴抬俯仰（模型是 Y 朝上）
    ca, sa, ce, se = np.cos(az), np.sin(az), np.cos(el), np.sin(el)
    Ry = np.array([[ca, 0, sa], [0, 1, 0], [-sa, 0, ca]])
    Rx = np.array([[1, 0, 0], [0, ce, -se], [0, se, ce]])
    R = Rx @ Ry
    Q = (Pp - center) @ R.T
    NQ = Nn @ R.T
    span = max(float(np.ptp(Q[:, 0])), float(np.ptp(Q[:, 1]))) or 1.0
    k = (S * 0.88) / span
    px = np.round(Q[:, 0] * k + S / 2).astype(np.int64)
    py = np.round(S / 2 - Q[:, 1] * k).astype(np.int64)
    z = Q[:, 2]
    ok = (px >= 0) & (px < S) & (py >= 0) & (py < S)
    px, py, z, NQ, Cc = px[ok], py[ok], z[ok], NQ[ok], Cc[ok]
    key = py * S + px
    order = np.lexsort((-z, key))          # 每个像素取离镜头最近的点（z 越大越近）
    k2 = key[order]
    first = np.r_[True, k2[1:] != k2[:-1]] if len(k2) else np.array([], bool)
    idx = order[first]
    L = np.array([0.3, 0.55, 0.78]); L = L / np.linalg.norm(L)
    lam = np.clip(NQ[idx] @ L, 0, 1)
    shade = 0.42 + 0.72 * lam
    rgb = np.clip(Cc[idx] * shade[:, None], 0, 255).astype(np.uint8)
    img = np.zeros((S, S, 3), np.uint8); img[...] = 14
    img[py[idx], px[idx]] = rgb
    mask = img.any(2)
    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1)):   # 膨胀，补点云小孔
        sh = np.roll(np.roll(img, dy, 0), dx, 1)
        mk = np.roll(np.roll(mask, dy, 0), dx, 1)
        fill = (~mask) & mk
        img[fill] = sh[fill]; mask = mask | fill
    return Image.fromarray(img).resize((int(size), int(size)), Image.LANCZOS)

_PREVIEW_CACHE = {}

def tool_render_preview(path, mode="solid", axis=1, height=None, size=520, caption=""):
    """把模型（或切完的两件）画成图，返回 URL；同一份几何重复调用直接命中缓存。"""
    trimesh = _trimesh()
    import numpy as np
    if isinstance(axis, str) and axis.strip().upper() in ("X", "Y", "Z"):
        axis = "XYZ".index(axis.strip().upper())
    try:
        axis = int(axis)
    except Exception:
        axis = 1
    if axis not in (0, 1, 2):
        axis = 1
    m = _geom(path)["welded"]

    split = str(mode) == "split"
    h = None
    if split:
        lo, hi = float(m.bounds[0][axis]), float(m.bounds[1][axis])
        try:
            h = 0.5 * (lo + hi) if height in (None, "") else float(height)
        except Exception:
            h = 0.5 * (lo + hi)

    try:
        sig = os.stat(os.path.abspath(str(path))).st_mtime_ns
    except Exception:
        sig = 0
    key = (str(path), sig, split, axis, None if h is None else round(h, 6), int(size))
    with _GEOM_LOCK:
        hit = _PREVIEW_CACHE.get(key)
    if hit and (PREVIEWS / hit).is_file():
        cap = caption or (f"切分结果：{'XYZ'[axis]} 轴，切面高度 {h:.4f}" if split else "当前模型（焊合后）")
        return {"kind": "preview", "mode": mode, "url": f"/previews/{hit}",
                "caption": cap, "size": int(size), "pass": True, "cached": True}

    meshes, cols = [], []
    if split:
        n = [0.0, 0.0, 0.0]; n[axis] = 1.0
        for sign, col in ((1, (120, 160, 255)), (-1, (255, 175, 95))):
            nn = [sign * v for v in n]
            p = trimesh.intersections.slice_mesh_plane(m, nn, np.array(n) * h, cap=True)
            p.fill_holes()
            meshes.append(p); cols.append(col)
        caption = caption or f"切分结果：{'XYZ'[axis]} 轴，切面高度 {h:.4f}"
    else:
        meshes, cols = [m], [(150, 172, 214)]
        caption = caption or "当前模型（焊合后）"

    _n = int(min(4_000_000, max(1_200_000, int(size) * int(size) * 4)))
    img = _shade_points(meshes, cols, size=int(size), samples=_n)
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    olds = sorted(PREVIEWS.glob("*.png"), key=lambda f: f.stat().st_mtime)
    for f in olds[:-60]:
        try:
            f.unlink()
        except OSError:
            pass
    name = f"prev-{int(time.time() * 1000)}-{len(m.faces) % 997}.png"
    img.save(PREVIEWS / name)
    with _GEOM_LOCK:
        _PREVIEW_CACHE[key] = name
        while len(_PREVIEW_CACHE) > 12:
            _PREVIEW_CACHE.pop(next(iter(_PREVIEW_CACHE)))
    return {"kind": "preview", "mode": mode, "url": f"/previews/{name}",
            "caption": caption, "size": int(size), "pass": True}

def tool_asset_list(limit=40):
    out = []
    if ASSET_ROOT.exists():
        for p in sorted(ASSET_ROOT.rglob("*")):
            if p.suffix.lower() in (".glb", ".gltf", ".obj", ".stl", ".ply"):
                out.append(str(p))
                if len(out) >= limit:
                    break
    return {"kind": "list", "count": len(out), "assets": out}

_ASSET_CACHE = {"t": 0, "v": []}
def all_assets():
    """全量资产清单（带 5 分钟缓存，避免每次 rglob）"""
    if time.time() - _ASSET_CACHE["t"] < 300 and _ASSET_CACHE["v"]:
        return _ASSET_CACHE["v"]
    v = tool_asset_list(5000)["assets"]
    _ASSET_CACHE.update(t=time.time(), v=v)
    return v

def tool_mesh_qc(path, digits=8):
    trimesh = _trimesh()
    import numpy as np
    g = _geom(path)
    m0, welded = g["raw"], g["welded"]
    raw = {"vertices": len(m0.vertices), "faces": len(m0.faces),
           "watertight": bool(m0.is_watertight), "volume": round(float(m0.volume), 6)}
    bnd, nm, comps = g["bnd"], g["nm"], g["comps"]
    cls = "fake_seam" if (bnd == 0 and nm == 0) else ("real_nonmanifold" if nm > 0 else "real_hole")
    route = {"fake_seam": "weld_only", "real_nonmanifold": "repair_nonmanifold",
             "real_hole": "fill_holes"}[cls]
    return {"kind": "qc", "path": path, "raw": raw,
            "welded": {"vertices": len(welded.vertices), "boundary_edges": bnd,
                       "nonmanifold_edges": nm, "watertight": bool(welded.is_watertight),
                       "components": comps},
            "class": cls, "repair_route": route,
            "pass": bool(bnd == 0 and nm == 0 and welded.is_watertight)}


def _section_cost(m, ax, ratio):
    """Cut once at a relative height and measure the section perimeter."""
    trimesh = _trimesh()
    import numpy as np
    lo, hi = float(m.bounds[0][ax]), float(m.bounds[1][ax])
    if hi - lo < 1e-9:
        return None
    n = np.zeros(3); n[ax] = 1.0
    h = lo + (hi - lo) * ratio
    try:
        seg = trimesh.intersections.mesh_plane(m, n, n * h)
    except Exception:
        return None
    if not len(seg):
        return None
    per = float(np.linalg.norm(seg[:, 1] - seg[:, 0], axis=1).sum())
    if per <= 0:
        return None
    return {"axis": ax, "ratio": float(ratio), "height": float(h),
            "perimeter": round(per, 3),
            "cost": round(per * (1.0 + 2.0 * abs(ratio - 0.5)), 3)}


def _best_cut(m, lo_ratio=0.30, hi_ratio=0.70, steps=9):
    """Smallest seam inside the middle 40% band, biased towards a balanced split."""
    import numpy as np
    cands = []
    for ax in range(3):
        for r in np.linspace(lo_ratio, hi_ratio, steps):
            c = _section_cost(m, ax, float(r))
            if c:
                cands.append(c)
    return (min(cands, key=lambda c: c["cost"]), cands) if cands else (None, [])


def _cut_in_two(m, axis, height):
    """Slice into two capped halves."""
    trimesh = _trimesh()
    import numpy as np
    n = np.zeros(3); n[axis] = 1.0
    o = n * float(height)
    a = trimesh.intersections.slice_mesh_plane(m, n, o, cap=True)
    b = trimesh.intersections.slice_mesh_plane(m, -n, o, cap=True)
    a.fill_holes(); b.fill_holes()
    return a, b


def tool_split_n(path, n=2, min_faces=200, export=True):
    """Recursively cut the biggest piece until we have n watertight parts."""
    import numpy as np
    n = max(2, min(int(n or 2), 12))
    g = _geom(path)
    parts = [g["welded"]]
    used, notes = [], []
    guard = 0
    while len(parts) < n and guard < 40:
        guard += 1
        idx = max(range(len(parts)), key=lambda i: abs(float(parts[i].volume)))
        m = parts[idx]
        best, cands = _best_cut(m)
        if not best:
            notes.append("no usable cut plane left")
            break
        a, b = _cut_in_two(m, best["axis"], best["height"])
        if len(a.faces) < min_faces or len(b.faces) < min_faces:
            notes.append("largest piece too thin, stop to avoid slivers")
            break
        parts[idx:idx + 1] = [a, b]
        used.append({"axis": best["axis"], "axis_name": "XYZ"[best["axis"]],
                     "height": round(best["height"], 4),
                     "perimeter": best["perimeter"], "cost": best["cost"]})

    vols = [abs(float(x.volume)) for x in parts]
    total = sum(vols)
    orig = abs(float(g["welded"].volume))
    info = []
    outdir = HERE / "out" / ("split-" + time.strftime("%Y%m%d-%H%M%S"))
    if export:
        outdir.mkdir(parents=True, exist_ok=True)
    for i, x in enumerate(parts):
        fname = ""
        if export:
            f = outdir / ("part%d.stl" % (i + 1))
            try:
                x.export(str(f)); fname = str(f)
            except Exception:
                fname = ""
        info.append({"index": i + 1, "faces": len(x.faces),
                     "watertight": bool(x.is_watertight),
                     "volume": round(abs(float(x.volume)), 6),
                     "size": [round(float(v), 4) for v in (x.bounds[1] - x.bounds[0])],
                     "file": fname})
    prev_url = ""
    try:
        if 2 <= len(parts) <= 6:
            palette = [(120, 160, 255), (255, 175, 95), (140, 220, 160),
                       (235, 140, 200), (200, 200, 120), (170, 150, 235)]
            img = _shade_points(parts, palette[:len(parts)], size=560, samples=2400000)
            PREVIEWS.mkdir(parents=True, exist_ok=True)
            nm = "nparts-%d-%d.png" % (int(time.time() * 1000), len(parts))
            img.save(PREVIEWS / nm)
            prev_url = "/previews/" + nm
    except Exception:
        prev_url = ""
    ok = (all(q["watertight"] for q in info)
          and all(q["faces"] >= min_faces for q in info)
          and abs(total - orig) / max(orig, 1e-12) < 1e-3)
    return {"kind": "split_n", "path": path, "n": len(parts), "parts": info,
            "cuts": used, "notes": notes,
            "volume_sum": round(total, 6), "volume_original": round(orig, 6),
            "volume_err": round(abs(total - orig) / max(orig, 1e-12), 9),
            "outdir": str(outdir) if export else "", "preview": prev_url,
            "pass": bool(ok)}


def tool_slice_two(path, axis=1, height=None):
    trimesh = _trimesh()
    import numpy as np
    m = _geom(path)["welded"]
    lo, hi = float(m.bounds[0][axis]), float(m.bounds[1][axis])
    h = 0.5 * (lo + hi) if height is None else float(height)
    n = [0.0, 0.0, 0.0]; n[axis] = 1.0
    parts = []
    for sign in (1, -1):
        nn = [sign * v for v in n]
        # 只返回法线正侧那一半，所以要调两次（+n / -n）；平面过点 = n * h
        p = trimesh.intersections.slice_mesh_plane(m, nn, np.array(n) * h, cap=True)
        p.fill_holes()
        uniq, inv = np.unique(p.edges_sorted, axis=0, return_inverse=True)
        cnt = np.bincount(inv)
        parts.append({"faces": len(p.faces), "boundary_edges": int((cnt == 1).sum()),
                      "nonmanifold_edges": int((cnt > 2).sum()),
                      "watertight": bool(p.is_watertight), "volume": round(float(p.volume), 6)})
    total = sum(x["volume"] for x in parts)
    err = abs(total - float(m.volume)) / max(float(m.volume), 1e-12)
    ok = all(x["faces"] > 0 and x["watertight"] and x["boundary_edges"] == 0
             and x["nonmanifold_edges"] == 0 and x["volume"] > 0 for x in parts) and err < 1e-3
    return {"kind": "slice", "path": path, "axis": "XYZ"[axis], "height": round(h, 6),
            "parts": parts, "volume_sum": round(total, 6), "volume_original": round(float(m.volume), 6),
            "volume_err": err, "pass": bool(ok)}

# ---------------------------------------------------------------- 通用工具（与 3D 无关，任何问题都能用）
def _py_exe():
    """服务可能跑在 pythonw.exe 下（无控制台），子进程要换成同目录的 python.exe 才能拿到 stdout。"""
    p = Path(sys.executable)
    c = p.with_name("python.exe")
    return str(c) if c.is_file() else sys.executable


def tool_python(code="", timeout=25):
    """万能工具：在独立进程里跑一段 Python，返回 stdout / stderr / 退出码。"""
    if not isinstance(code, str) or not code.strip():
        return {"kind": "python", "pass": False, "error": "code 为空"}
    t0 = time.time()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"   # Windows 子进程默认 GBK，会把中文 stdout 读成乱码
    try:
        r = subprocess.run([_py_exe(), "-c", code], capture_output=True,
                           text=True, timeout=float(timeout), cwd=str(HERE),
                           encoding="utf-8", errors="replace", env=env)
        out, err, rc = (r.stdout or "")[-8000:], (r.stderr or "")[-4000:], r.returncode
    except subprocess.TimeoutExpired:
        return {"kind": "python", "pass": False, "error": "超时（>%ss）" % timeout,
                "seconds": round(time.time() - t0, 2)}
    except Exception as e:
        return {"kind": "python", "pass": False,
                "error": "%s: %s" % (type(e).__name__, e)}
    return {"kind": "python", "stdout": out, "stderr": err, "exit": rc,
            "pass": bool(rc == 0), "seconds": round(time.time() - t0, 2)}


def tool_http(url="", method="GET", body="", timeout=15):
    if not str(url).startswith(("http://", "https://")):
        return {"kind": "http", "pass": False, "error": "url 必须是 http(s)://"}
    try:
        req = urllib.request.Request(str(url),
              data=body.encode() if body else None, method=str(method).upper(),
              headers={"User-Agent": "workbench/1"})
        with urllib.request.urlopen(req, timeout=float(timeout)) as r:
            raw = r.read(300000)
            text = raw.decode("utf-8", "replace")
            return {"kind": "http", "url": url, "status": r.status,
                    "text": text[:8000], "length": len(text), "pass": r.status < 400}
    except urllib.error.HTTPError as e:
        return {"kind": "http", "url": url, "status": e.code,
                "text": e.read(2000).decode("utf-8", "replace"), "length": 0, "pass": False}
    except Exception as e:
        return {"kind": "http", "url": url, "pass": False,
                "error": "%s: %s" % (type(e).__name__, e)}


def tool_file_write(name="", content=""):
    try:
        d = HERE / "out"
        d.mkdir(exist_ok=True)
        p = (d / Path(str(name)).name).resolve()
        if p.parent != d.resolve():
            raise ValueError("只能写到 out/ 目录")
        p.write_text(str(content), encoding="utf-8")
        return {"kind": "file", "path": str(p), "url": "/out/" + p.name,
                "bytes": p.stat().st_size, "pass": True}
    except Exception as e:
        return {"kind": "file", "pass": False, "error": "%s: %s" % (type(e).__name__, e)}


def tool_file_read(path="", limit=200000):
    try:
        p = Path(str(path))
        if not p.is_file():
            return {"kind": "file", "pass": False, "error": "文件不存在：%s" % p}
        data = p.read_text(encoding="utf-8", errors="replace")[:int(limit)]
        return {"kind": "file", "path": str(p), "text": data,
                "length": len(data), "pass": True}
    except Exception as e:
        return {"kind": "file", "pass": False, "error": "%s: %s" % (type(e).__name__, e)}


def tool_llm(prompt="", system="", model_id="", timeout=60):
    try:
        cfg = load_config()
        models = {m.get("id"): m for m in cfg.get("models", [])}
        m = models.get(model_id) or (cfg.get("models") or [{}])[0]
        text = str(call_model(m, [
            {"role": "system", "content": system or "你是通用助手，直接回答用户的问题。"},
            {"role": "user", "content": str(prompt)}], timeout=float(timeout)))
        return {"kind": "llm", "text": text[:8000], "model": m.get("model"),
                "pass": bool(text.strip())}
    except Exception as e:
        return {"kind": "llm", "pass": False, "error": "%s: %s" % (type(e).__name__, e)}

PATH_TOOLS = {"mesh_qc", "slice_two", "asset_profile", "render_preview", "split_n"}
TOOLS = {"split_n": tool_split_n, "asset_list": tool_asset_list,
         "asset_profile": tool_asset_profile, "render_preview": tool_render_preview,
         "mesh_qc": tool_mesh_qc, "slice_two": tool_slice_two,
         "python": tool_python, "http": tool_http, "file_write": tool_file_write,
         "file_read": tool_file_read, "llm": tool_llm}


def code_build_subtasks(ctx):
    """把规划者给的 subtasks 收拾成清单，并按模型特征算出建议切面法向（确定性，不问模型）。"""
    feats = ctx.get("features") or {}
    subs = ctx.get("subtasks")
    if not isinstance(subs, list):
        subs = ((ctx.get("split_plan") or {}).get("subtasks")) or []
    clean = []
    for i, s in enumerate(subs, 1):
        if isinstance(s, str):
            s = {"title": s}
        if not isinstance(s, dict):
            continue
        clean.append({"id": str(s.get("id") or f"T{i}"),
                      "title": str(s.get("title") or s.get("name") or f"子任务{i}"),
                      "tool": str(s.get("tool") or ""),
                      "why": str(s.get("why") or "")})
    longest = feats.get("longest_axis") or ctx.get("longest_axis") or "Y"
    axis = {"X": 0, "Y": 1, "Z": 2}.get(str(longest).upper(), 1)
    axis_name = "XYZ"[axis]
    return {"kind": "code", "fn": "build_subtasks",
            "subtask_count": len(clean), "subtasks": clean,
            "suggested_axis": axis, "axis_name": axis_name,
            "note": f"按最长边 {longest} 建议切面法向 = {axis}（{axis_name} 轴）",
            "pass": bool(len(clean) > 0)}

CODE_FNS = {"build_subtasks": code_build_subtasks}

# ---------------------------------------------------------------- 模型层（可插拔）
def _api_key(model):
    """Key 优先级：加密库（按模型 ID） > 环境变量(按 provider) > 通用 OPENAI_API_KEY。
    加密库由 /api/key 写入，config.json 里永远不含 Key。"""
    k = security.STORE.get(model.get("id", "")).strip()
    if k:
        return k
    prov = (model.get("provider") or "openai").lower()
    k = os.environ.get((prov + "_api_key").upper()) or os.environ.get("OPENAI_API_KEY") or ""
    return k.strip()


def model_state(model):
    """模型就绪判定 —— 必须与前端徽标完全一致。

    返回 (ready, reason)。未就绪时 call_model 统一回退本地演示模型，
    绝不带着空 Key 去请求真实接口（那会 401 并让整轮编排拿不到任何结果）。
    """
    prov = (model.get("provider") or "mock").lower()
    if prov == "mock":
        return True, "演示模型（本地规则模拟）"
    if prov == "ollama":
        return ((True, "本地 Ollama 已启动") if ollama_online()
                else (False, "本地 Ollama 未启动（127.0.0.1:11434 无响应）"))
    return ((True, "已配置 API Key") if _api_key(model)
            else (False, "未配置 API Key"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用 302：否则公网域名可把带 Key 的请求重定向到内网，绕过 SSRF 校验。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code,
                                     "重定向已禁用（SSRF 防护）", headers, fp)


_HTTP = urllib.request.build_opener(_NoRedirect)


def _read_limited(resp):
    raw = resp.read(security.MAX_RESPONSE_BYTES + 1)
    if len(raw) > security.MAX_RESPONSE_BYTES:
        raise ValueError("模型响应超出大小上限")
    return raw


def _extract_tool_args(msg):
    """兼容 function-calling 形态：从 tool_calls 里取出第一个函数的 arguments(JSON 字符串)。"""
    tcs = (msg or {}).get("tool_calls") or []
    for tc in tcs:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str) and args.strip():
            return args
        if isinstance(args, dict):
            return json.dumps(args, ensure_ascii=False)
    return (msg or {}).get("content") or ""


def _model_num(model, key, default, lo, hi):
    """节点上的温度/最大输出控件：夹到安全范围后写进真实接口请求体。"""
    try:
        v = float(model.get(key, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def call_model(model, messages, timeout=90, json_mode=False):
    """调一个模型。json_mode=True 时强制真实模型输出合法 JSON 对象（DeepSeek 走
    response_format=json_object；Ollama 走 format:json），与 validate_decision 配合实现强校验。"""
    prov = model.get("provider", "mock")
    if prov == "mock":
        return mock_reply(model, messages)
    # 未就绪（无 Key / 本机 Ollama 未启动）→ 自动回退演示，并在输出里显式标注来源
    ready, why = model_state(model)
    if not ready:
        fb = dict(model)
        fb["model"] = "demo-fallback"
        return mock_reply(fb, messages)
    if prov == "ollama":
        url = model.get("base_url", "http://127.0.0.1:11434").rstrip("/") + "/api/chat"
        security.assert_outbound_url(url, "ollama")
        body = {"model": model.get("model", "qwen2.5:7b"), "messages": messages, "stream": False,
                "options": {"temperature": _model_num(model, "_temperature", 0.3, 0.0, 2.0),
                            "num_predict": int(_model_num(model, "_max_tokens", 2048, 128, 8192))}}
        if json_mode:
            body["format"] = "json"
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with _HTTP.open(req, timeout=timeout) as r:
            return _extract_tool_args(json.loads(_read_limited(r).decode())["message"])
    # OpenAI 兼容（DeepSeek / OpenAI / 任意兼容端点）
    url = model.get("base_url", "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    security.assert_outbound_url(url, "openai")
    body = {"model": model.get("model", "gpt-4o-mini"), "messages": messages,
            "temperature": _model_num(model, "_temperature", 0.2, 0.0, 2.0),
            "max_tokens": int(_model_num(model, "_max_tokens", 2048, 128, 8192))}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + _api_key(model)})
    with _HTTP.open(req, timeout=timeout) as r:
        return _extract_tool_args(json.loads(_read_limited(r).decode())["choices"][0]["message"])

def test_model_connection(model):
    """节点接口连通性自检：只按 model_id 读取已校验的配置与加密库里的 Key，
    绝不接受请求体里夹带的 URL / Key，也不会把凭据写进日志或回显。"""
    prov = (model.get("provider") or "mock").lower()
    mid = model.get("id", "")
    if prov == "mock":
        return {"ok": True, "demo": True, "code": "demo",
                "message": "演示模型（本地规则模拟），不需要连接外部接口。填入有效 API Key 后会自动切换真实推理。"}
    if prov == "ollama":
        if not ollama_online():
            return {"ok": False, "code": "ollama_offline",
                    "message": "本地 Ollama 没启动：127.0.0.1:11434 无响应。请先安装并运行 ollama serve，再点测试。"}
        url = (model.get("base_url") or "http://127.0.0.1:11434").rstrip("/") + "/api/tags"
        security.assert_outbound_url(url, "ollama")
        try:
            with _HTTP.open(urllib.request.Request(url), timeout=6) as r:
                r.read(2048)
            return {"ok": True, "code": "ok",
                    "message": "本地 Ollama 已连通；模型名「%s」在运行时会按需拉取。" %
                               str(model.get("model") or "qwen2.5:7b")[:60]}
        except Exception as e:
            return {"ok": False, "code": "ollama_error",
                    "message": "本地 Ollama 端口有响应但 /api/tags 读取失败：%s" % type(e).__name__}
    key = _api_key(model)
    if not key:
        return {"ok": False, "code": "no_key",
                "message": "这个节点还没填 API Key，运行时只会回退演示模型。粘贴 Key 并点「保存并接入」后再测试。"}
    url = (model.get("base_url") or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    security.assert_outbound_url(url, "openai")
    body = {"model": model.get("model") or "gpt-4o-mini",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8, "temperature": 0}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with _HTTP.open(req, timeout=12) as r:
            data = json.loads(_read_limited(r).decode("utf-8", "replace"))
        choices = data.get("choices") or []
        if not choices:
            return {"ok": False, "code": "empty_response",
                    "message": "接口连通了，但返回的 choices 为空；检查模型名是否正确、账户是否有该模型权限。"}
        return {"ok": True, "code": "ok",
                "message": "连接成功：%s 已返回有效响应，该节点可以真实推理。" %
                           str(model.get("model") or "")[:60]}
    except urllib.error.HTTPError as e:
        m = {
            400: "请求被拒（400）：接口地址或模型名不对，检查是否漏写 /v1、模型 ID 是否拼写正确。",
            401: "Key 无效或已失效（401）：换一个有效 Key，或在弹层点「清除该节点 Key」重新粘贴。",
            403: "无权限（403）：账户没有该模型/接口的访问权限，或 Key 被禁用。",
            404: "找不到接口（404）：接口地址通常用 https://api.deepseek.com/v1，路径不要多写 /chat/completions。",
            429: "被限流或余额不足（429）：稍后再试，或检查账户额度。",
            500: "服务端错误（500/502/503）：模型服务暂时不可用，稍后重试。",
        }
        return {"ok": False, "code": "http_%d" % e.code,
                "message": m.get(e.code, "接口返回 HTTP %d，请检查地址、模型名与账户状态。" % e.code)}
    except Exception as e:
        name = type(e).__name__
        if name in ("TimeoutError", "socket.timeout") or "timed out" in str(e).lower():
            return {"ok": False, "code": "timeout",
                    "message": "连接超时：接口地址不通或网络被拦截，检查代理/防火墙后重试。"}
        if "SSL" in name or "certificate" in str(e).lower():
            return {"ok": False, "code": "tls", "message": "TLS 证书校验失败：检查系统时间或网络代理。"}
        if "gaierror" in str(e).lower() or "NameResolution" in name:
            return {"ok": False, "code": "dns", "message": "域名解析失败：接口地址拼错或当前无法访问外网。"}
        return {"ok": False, "code": "network", "message": "连不上接口（%s）：检查接口地址与网络。" % name}


def mock_reply(model, messages):
    """演示用：不联网，按规则给出"像模型输出的东西"，并显式标注来源。"""
    user = messages[-1]["content"] if messages else ""
    sysmsg = messages[0]["content"] if messages else ""
    tag = f"[{model.get('model','mock')}] "
    # ---- 主从自主编排演示分支（必须排在"分派"之前）----
    if "管理者模型 · 编排" in sysmsg:
        mm = re.search(r"第\s*(\d+)\s*轮\s*/\s*最多\s*(\d+)\s*轮", sysmsg)
        rnd = int(mm.group(1)) if mm else 1
        mx = int(mm.group(2)) if mm else 3
        wids = re.findall(r"^-\s*(\w+)（", sysmsg, re.M) or ["w1", "w2", "w3"]
        has_results = ("· 回传]" in sysmsg)
        if has_results and rnd >= mx:
            return json.dumps({
                "thinking": ("共享记忆里已有分析、执行、审校三路回传，转交复核也做完了；"
                             "再开一轮只是重复劳动，信息已足够 → 收尾并直接给最终答案。"),
                "status": "finish",
                "memory_note": "多轮回传齐备，管理者判定可以收尾。",
                "subtasks": [],
                "answer": ("【管理者最终答案（第 %d 轮收尾）】\n"
                           "结论：任务已按「并行分派 → 结果回写共享记忆 → 转交复核 → 再决策」自主跑完 %d 轮，"
                           "各子模型要点已归并。\n\n"
                           "① 分析员：明确了目标、已知信息与约束，并留了歧义待确认。\n"
                           "② 执行员：给出可落地的方案/产物路径与关键假设。\n"
                           "③ 审校员（经转交复核）：一致性、可复核性、风险三项均已检查。\n\n"
                           "风险与待确认：若当前使用演示模型，以上仅验证编排链路；"
                           "在「模型库」填入 Key 或启动 Ollama 后，同一多轮流程会由真实模型在后台推理。") % (rnd, rnd),
                "confidence": 0.86}, ensure_ascii=False)
        if not has_results:
            duties = {"w1": "拆目标、列已知与约束", "w2": "出可执行方案/计算", "w3": "查风险与待确认"}
            subs = []
            for i, wid in enumerate(wids[:3], 1):
                subs.append({"id": "S%d" % i, "to": wid,
                             "task": (user[:140] + " —— 请只从你的职责角度完成这一块"),
                             "why": "交给 %s：%s" % (wid, duties.get(wid, "完成子任务"))})
            return json.dumps({
                "thinking": ("第一轮先把任务拆成三条互不重叠的线并行跑：分析定边界、执行出方案、审校查风险；"
                             "结果全部写进共享记忆，下一轮我再决定是补做、转交复核，还是收尾。"),
                "status": "continue",
                "memory_note": "已并行分派 3 个子任务（分析/执行/审校），等待回传后基于共享记忆再决策。",
                "subtasks": subs, "answer": "", "confidence": 0.55}, ensure_ascii=False)
        # 有结果但没到最后一轮 → 做一次转交复核，体现 handoff
        last = wids[-1] if wids else "w3"
        return json.dumps({
            "thinking": ("上一轮三路结果已回写共享记忆：分析给了边界、执行给了方案，"
                         "但方案里的假设还没人对照着复核。所以这一轮不重复劳动，"
                         "把执行员的产出转交给审校员做针对性复核与深化，再据结果收尾。"),
            "status": "continue",
            "memory_note": "管理者决定：不重复开新题，改做一次转交（handoff）复核。",
            "subtasks": [{"id": "H%d" % rnd, "to": last,
                          "task": "针对上游执行员的方案做一致性/可复核性/风险复核，并补一句待确认",
                          "why": "上游方案还没被对照复核，转交比重新派一遍更省预算",
                          "handoff_from": "S2"}],
            "answer": "", "confidence": 0.72}, ensure_ascii=False)
    # ---- 协作团队演示分支：规划者分析/复核、子 Agent 执行 ----
    if "协作规划者 · 分析" in sysmsg:
        ids = re.findall(r"^-\s*([A-Za-z0-9_-]+)（", sysmsg, re.M) or ["wk-1"]
        tm = re.search(r"^任务：(.+)$", sysmsg, re.M)
        real_task = (tm.group(1).strip() if tm else user)[:120]
        focus = ["把目标、已知信息、约束列清楚，并给出一个可检验的输出格式",
                 "给出能直接落地的步骤 / 计算 / 内容，写清关键假设",
                 "核对已有结论的一致性，列出风险与待确认处",
                 "把结论压成 3~5 条短条目，便于规划者汇总"]
        ins = []
        for i, wid in enumerate(ids[:6], 1):
            ins.append({
                "to": wid,
                "prompt": ("针对任务「%s」：%s。输出用 3~5 条短条目，"
                           "每条写清结论与依据，不要写空话。" % (real_task[:90], focus[(i - 1) % 4])),
                "why": "把整体任务翻译成 %s 能独立照做的一条执行指令" % wid,
                "context_from": ""})
        return json.dumps({
            "analysis": ("我先把问题拆成目标 / 已知 / 约束三层，再把每一层翻译成子 Agent "
                         "能直接照做的执行指令（写明目标、可用输入、输出格式、验收点）。"
                         "各 Agent 结果回写共享记忆后由我逐条复核，必要时做转交补做。"),
            "instructions": ins,
            "gaps": "演示模型按规则拆分；节点 API 接入真实模型后会按任务语义动态生成指令。",
            "confidence": 0.62}, ensure_ascii=False)
    if "协作规划者 · 复核" in sysmsg:
        ids = re.findall(r"^-\s*([A-Za-z0-9_-]+)（", sysmsg, re.M) or ["wk-1"]
        tm = re.search(r"^任务：(.+)$", sysmsg, re.M)
        real_task = (tm.group(1).strip() if tm else user)[:120]
        mm = re.search(r"第\s*(\d+)\s*轮\s*/\s*最多\s*(\d+)\s*轮", sysmsg)
        rnd = int(mm.group(1)) if mm else 1
        mx = int(mm.group(2)) if mm else 2
        tail = sysmsg.split("子 Agent 结果：", 1)[-1].strip()
        if rnd >= mx:
            return json.dumps({
                "status": "finish",
                "review": ("逐条复核：各子 Agent 的产出都覆盖了被分派的指令，未见互相矛盾；"
                           "共享记忆已写入，风险与待确认项已在答案里标注。"),
                "answer": ("【规划者复核后的最终答案】\n任务：%s\n\n"
                           "① 分析：由规划者完成，已把任务拆成目标 / 已知 / 约束，"
                           "并翻译成子 Agent 能听懂的执行指令。\n"
                           "② 执行：各子 Agent 依指令独立思考并回传，结果已写进共享记忆，"
                           "转交复核也已跑过。\n"
                           "③ 复核：由规划者完成，结论一致、可复核。\n\n"
                           "各 Agent 要点：\n%s\n\n"
                           "风险与待确认：若节点接口仍是演示模型，以上只验证协作链路；"
                           "在节点标签的「API」里填入有效 Key 后，同一流程会由真实模型推理。")
                          % (real_task[:100], tail[:1200] or "（逐条结果见右侧消息栏）"),
                "followups": [], "confidence": 0.84}, ensure_ascii=False)
        return json.dumps({
            "status": "continue",
            "review": ("首轮结果已齐，但各 Agent 只覆盖了单条指令，还没人拿到别人的产出做交叉验证。"
                       "下一轮做一次转交：把一个 Agent 的回传交给另一个继续深化。"),
            "answer": "",
            "followups": [{"to": ids[0],
                           "prompt": ("读取共享记忆里其他 Agent 的回传，做交叉验证，"
                                      "补齐被忽略的约束，并写一句待确认处"),
                           "why": "把别人的产出转交给它，形成协作而不是各做各的",
                           "context_from": "latest"}],
            "confidence": 0.7}, ensure_ascii=False)
    if "执行 Agent ·" in sysmsg:
        nm = re.search(r"执行 Agent ·\s*([^\n」]+)", sysmsg)
        name = (nm.group(1).strip() if nm else "子 Agent")
        hand = "（已收到其他 Agent 的转交产出，先读再接着做）\n" if "转交自" in user else ""
        im = re.search(r"^执行指令：(.+)$", user, re.M)
        brief = (im.group(1).strip() if im else user)[:110]
        return (tag + "「%s」执行回传：\n" % name + hand
                + "① 指令识别：%s\n" % brief
                + "② 思考：先确认目标与可用输入，拆成能验证的小步，再逐条给结论；"
                  "没有外部资料时以任务文本与共享记忆为准，不臆造事实。\n"
                  "③ 结果：\n"
                  "  · 结论：3~5 条可复核的短条目，覆盖指令要求的输出格式。\n"
                  "  · 依据：任务文本、共享记忆中的上游回传。\n"
                  "  · 假设：未指定格式时按条目化文本交付。\n"
                  "④ 待确认：任务里若有歧义，列一句需要用户补充的信息。\n"
                  "（当前为演示模型输出；节点 API 填入有效 Key 后为真实模型推理。）")
    # ---- Delegation demo branches (used only when the slot is a mock model) ----
    if "管理者模型 · 分派" in sysmsg:
        wids = re.findall(r"^-\s*(\w+)（", sysmsg, re.M) or ["w1", "w2", "w3"]
        duties = {"w1": "拆清楚目标、已知信息和约束",
                  "w2": "给出可执行的方案或计算",
                  "w3": "复核结果、指出风险与待确认处"}
        subs = []
        for i, wid in enumerate(wids[:4], 1):
            subs.append({"id": "S%d" % i, "to": wid,
                         "task": (user[:160] + " —— 请从你的职责角度完成这一块"),
                         "why": "交给 %s：%s" % (wid, duties.get(wid, "完成子任务"))})
        return json.dumps({
            "thinking": ("先在后台把任务拆成目标、约束、产出三层；再按子模型职责分派："
                         "分析员先定边界，执行员出方案，审校员查风险。这样每一块都有人负责，"
                         "结果回来后我再汇总，不会出现谁都做了、谁都没做透。"),
            "subtasks": subs,
            "final_hint": "汇总时先给结论，再列各子模型要点，最后写风险与待确认处。",
            "task": user[:200]}, ensure_ascii=False)
    if "子模型 ·" in sysmsg:
        nm = "子模型"
        mm = re.search(r"子模型 ·\s*([^\s，。」]+)", sysmsg)
        if mm:
            nm = mm.group(1)
        if "分析" in sysmsg:
            body = ("【后台运行 · 分析】\n"
                    "1. 目标：把用户这句话变成一个能被检查的产出，而不是一句解释。\n"
                    "2. 已知：任务文本本身；没有额外文件时以常识与规则为准。\n"
                    "3. 约束：结果要能回传给管理者汇总，所以写成短条目、不要长篇散文。\n"
                    "4. 关键风险：需求可能有歧义，结论里要留一句待确认。")
        elif "审校" in sysmsg or "复核" in sysmsg:
            body = ("【后台运行 · 审校】\n"
                    "· 一致性：各子模型结论没有互相矛盾。\n"
                    "· 可复核：数字、文件名、步骤都写清楚了，能被追问。\n"
                    "· 风险：若接入的是演示模型，结论只演示链路，不代表真实推理。\n"
                    "· 待确认：用户没给具体格式/范围，汇总里应提示可补一句更明确的要求。")
        else:
            body = ("【后台运行 · 执行】\n"
                    "给出可执行方案：\n"
                    "① 先落一个能打开/能运行的产物，避免只停留在文字说明；\n"
                    "② 把关键结果用 print/条目写清楚，便于复核；\n"
                    "③ 把用到的假设列在最后。\n"
                    "（当前为演示模型输出，接入真实模型后这里会是模型自己的推理结果。）")
        return (tag + "「%s」返回结果：\n" % nm
                + ("【已收到转交产出】先接着上游结论继续，再按本职责给结果。\n" if "转交自" in user else "")
                + body)
    if "管理者模型 · 汇总" in sysmsg:
        tail = sysmsg.split("子模型结果：", 1)[-1].strip() if "子模型结果：" in sysmsg else ""
        return ("【管理者汇总回传】\n"
                "结论：任务已在后台走完「管理者分派 → 各子模型运行 → 结果回传」整条链，"
                "下面按子模型归并要点。\n\n"
                + (tail[:1500] if tail else "（各子模型结果见上方时间线）")
                + "\n\n风险与待确认：当前若使用演示模型，仅验证链路；"
                  "在「模型库」填入 Key 或启动 Ollama 后，同一流程会由真实模型在后台推理。")
    if "通用问题分析师" in sysmsg:
        return json.dumps({
            "analysis": ("先把问题拆成三层：要什么（一个能被检查的产出）、有什么（输入与约束）、"
                         "怎么做（计算、检索、生成文件，还是调用专用工具）。"
                         "再挑一条能真正跑出结果的路径，而不是只写一段说明。"),
            "approach": "先由工具产出可验证的结果，再由闸门按判据复核，最后把答案回传。",
            "risk": "离线演示规划器只认识几类常见任务；接入真实模型后可任意规划。",
            "task": user[:200]}, ensure_ascii=False)
    if "拆件规划者" in sysmsg or "analysis" in sysmsg:
        return json.dumps({
            "analysis": ("先看形状：这是一件立体模型，最长边决定切面法向——沿它切，两件尺寸最接近，"
                         "也不容易把细长处拦腰截断。再看风险：入厂状态没确认之前不切，"
                         "有破洞的话切面补不严，会得到两件漏的；纹理和细节尽量别落在切线上。"
                         "最后定方案：先质检确认干净，再沿最长边从中间切一刀，两件各自封底都立得住。"),
            "split_plan": {"axis": 1, "height_ratio": 0.5},
            "subtasks": [
                {"id": "T1", "title": "入厂质检：确认焊合后没有边界边和非流形边",
                 "tool": "mesh_qc", "why": "带破洞切下去，两件都漏，后面封盖也救不回来"},
                {"id": "T2", "title": "沿最长边中线切一刀，两件都要水密且体积守恒",
                 "tool": "slice_two", "why": "沿最长边切，两件高度接近、各自平整底面"},
            ]}, ensure_ascii=False)
    if "分派员" in sysmsg or "assignments" in sysmsg:
        mm = re.search(r'"suggested_axis"\s*:\s*(\d)', user) or re.search(r'suggested_axis.{0,6}?(\d)', user)
        axis = int(mm.group(1)) if mm else 1
        return json.dumps({
            "axis": axis, "height": None, "suggested_axis": axis,
            "assignments": [
                {"id": "T1", "to": "mesh_qc", "why": "先确认焊合后 bnd=0 且无非流形边，干净才动刀"},
                {"id": "T2", "to": "slice_two", "why": f"切面法向取 {axis}（模型中位最长的那一维），高度留空取中点"},
            ],
            "why": f"模型特征给的最长边方向是 {axis}，沿它从中间切，两件尺寸最均衡"}, ensure_ascii=False)
    if "axis" in sysmsg or "axis" in user:
        return json.dumps({"axis": 1, "height": None,
                           "why": "沿 Y 轴取包围盒中线，两件高度接近、便于各自封底"},
                          ensure_ascii=False)
    if '"steps"' in user or "拆成" in sysmsg:
        return json.dumps({"steps": [
            {"agent": "qc", "tool": "mesh_qc", "args": {}, "why": "先确认入厂状态，决定是否需要修复"},
            {"agent": "splitter", "tool": "slice_two", "args": {"axis": 1}, "why": "沿 Y 轴中线切成两件"},
        ]}, ensure_ascii=False)
    if "验收" in user or "汇总" in user:
        return tag + "已收到代码验收结果，据此给结论。"
    return tag + "执行完成：" + user[:120]

# ---------------------------------------------------------------- 事件与运行
def emit(run_id, agent, kind, text, detail=None, node=None):
    text = str(text or "")
    if kind == "error":
        # 事件流会回给浏览器：堆栈、绝对路径、异常原文一律不回显
        if "Traceback" in text or 'File "' in text or "\\workbench\\" in text.lower():
            ref = security.fail_ref(Exception(text))
            text = "运行内部错误，详情仅写入服务端日志（ref=%s）" % ref
        else:
            text = security.scrub(text)[:300]
        detail = None
    with LOCK:
        run = RUNS.get(run_id)
        if not run:            # 运行记录被清除后，后台线程仍可能发事件
            return
        ev = {"seq": len(run["events"]), "t": round(time.time() - run["t0"], 2),
              "agent": agent, "kind": kind, "text": text}
        if detail is not None:
            ev["detail"] = detail
        if node is not None:
            ev["node"] = node
        run["events"].append(ev)

def resolve_asset(task):
    """优先用 UI 下拉框指定的资产；否则按任务文本里的文件名匹配。
    只接受已登记资产，杜绝「（资产：任意本地路径）」造成的文件读取与路径探测。"""
    m = re.search(r"（资产：(.+?)）", task)
    if m:
        want = m.group(1)
        for a in all_assets():
            if a == want or Path(a).name == Path(want).name:
                return a
    low = task.lower()
    cands = all_assets()
    hits = [a for a in cands if Path(a).stem.lower() in low]
    if hits:
        return sorted(hits, key=len)[0]
    hits = [a for a in cands if Path(a).name.lower() in low]
    if hits:
        return sorted(hits, key=len)[0]
    # 任务里点了一个像文件名的东西却没匹配上 —— 不能偷偷换成别的模型，否则用户以为跑的是自己的文件
    named = re.findall(r"[\w\-.]+\.(?:glb|gltf|stl|obj|ply|off|3mf)", low)
    if named:
        return ""
    if len(cands) == 1:
        return cands[0]
    return ""

def _brief_result(d):
    """给日志看的简短摘要，避免把整段 stdout 塞进 checks。"""
    for k in ("stdout", "text", "url", "path", "status", "exit", "error"):
        if d.get(k) is not None:
            return {k: str(d[k])[:300]}
    return {"kind": d.get("kind")}


def verify_code(results, criteria=None):
    """通用验收：对每一个工具产出都生成一条判据，再叠加计划自带的 criteria。"""
    if not results:
        return {"ok": False, "checks": [{"step": "gate",
                "check": "没有任何验收项：闸门前至少接一个能产出结果的节点",
                "value": None, "ok": False}]}
    checks = []
    for r in results:
        d = r.get("result") or {}
        kind = d.get("kind")
        step = r.get("tool") or r.get("node") or "step"
        if kind == "qc":
            good = bool(d.get("pass"))
            checks.append({"step": step, "check": "焊后 bnd=0 ∧ nm=0 ∧ 水密",
                           "value": d.get("welded"), "ok": good})
        elif kind == "split_n":
            good = bool(d.get("pass"))
            checks.append({"step": step, "check": "每件水密/非空/体积守恒<1e-3",
                           "value": {"n": d.get("n"), "volume_err": d.get("volume_err")},
                           "ok": good})
        elif kind == "slice":
            good = bool(d.get("pass"))
            checks.append({"step": step, "check": "两件水密/非空/体积守恒<1e-3",
                           "value": {"volume_err": round(d.get("volume_err", 1), 9)},
                           "ok": good})
        elif kind == "python":
            good = bool(d.get("pass"))
            checks.append({"step": step, "check": "Python 退出码 0（无异常）",
                           "value": {"exit": d.get("exit"),
                                     "stdout": (d.get("stdout") or "")[:260],
                                     "stderr": (d.get("stderr") or "")[:200]}, "ok": good})
        else:
            # profile / preview / list / file / http / llm / code / error 等
            good = bool(d.get("pass", True)) and kind != "error"
            checks.append({"step": step,
                           "check": "节点执行成功" if kind != "error" else "节点报错",
                           "value": _brief_result(d), "ok": good})
    crit = criteria or {}
    ok = all(c["ok"] for c in checks) if crit.get("all_pass", True) else True
    hay = json.dumps(results, ensure_ascii=False)
    for needle in (crit.get("expect_contains") or []):
        hit = str(needle) in hay
        checks.append({"step": "criteria", "check": "输出包含「%s」" % needle,
                       "value": hit, "ok": hit})
        ok = ok and hit
    need = int(crit.get("min_checks") or 0)
    if need:
        hit = len(results) >= need
        checks.append({"step": "criteria", "check": "至少 %d 个工具产出" % need,
                       "value": len(results), "ok": hit})
        ok = ok and hit
    return {"ok": ok, "checks": checks}


def run_task(run_id, task, cfg):
    agents = {a["id"]: a for a in cfg["agents"]}
    models = {m["id"]: m for m in cfg["models"]}
    def model_of(aid):
        a = agents[aid]; return models.get(a.get("model_id")) or cfg["models"][0]
    try:
        asset = resolve_asset(task)
        emit(run_id, "orchestrator", "task", f"任务：{task}")
        emit(run_id, "orchestrator", "info", f"资产定位：{Path(asset).name if asset else '（未找到）'}")

        # 1) 规划
        emit(run_id, "planner", "think", "拆解任务中…")
        plan_prompt = f"把下面任务拆成可执行步骤，输出 JSON：{{\"steps\":[...]}}\n任务：{task}\n资产：{asset}"
        raw = call_model(model_of("planner"), [
            {"role": "system", "content": agents["planner"]["sys"]},
            {"role": "user", "content": plan_prompt}])
        try:
            steps = json.loads(raw[raw.find("{"): raw.rfind("}") + 1]).get("steps", [])
        except Exception:
            steps = [{"agent": "qc", "tool": "mesh_qc", "args": {}},
                     {"agent": "splitter", "tool": "slice_two", "args": {"axis": 1}}]
        emit(run_id, "planner", "plan", f"得到 {len(steps)} 个步骤", steps)

        # 2) 分工执行
        results = []
        for i, st in enumerate(steps, 1):
            if run_id in ABORT:
                emit(run_id, "orchestrator", "abort", "收到中止指令")
                break
            aid = st.get("agent") if st.get("agent") in agents else "qc"
            tool = st.get("tool")
            args = dict(st.get("args") or {})
            if tool in ("mesh_qc", "slice_two"):
                args.setdefault("path", asset)
            emit(run_id, aid, "step", f"步骤 {i}/{len(steps)}：{tool} {json.dumps(args, ensure_ascii=False)}",
                 {"why": st.get("why", "")})
            t0 = time.time()
            try:
                if tool not in TOOLS:
                    raise ValueError(f"未知工具 {tool}")
                if tool not in (agents[aid].get("tools") or []):
                    raise PermissionError(f"{aid} 没有 {tool} 的工具权限")
                res = TOOLS[tool](**args)
            except Exception as e:
                emit(run_id, aid, "error", f"{type(e).__name__}: {e}")
                res = {"kind": "error", "pass": False, "error": str(e)}
            dur = round(time.time() - t0, 2)
            results.append({"agent": aid, "tool": tool, "result": res, "seconds": dur,
                            "raw": raw if i == 1 else None})
            brief = json.dumps(res, ensure_ascii=False)
            emit(run_id, aid, "result", brief[:600] + ("…" if len(brief) > 600 else ""),
                 {"seconds": dur, "pass": res.get("pass")})
            # worker 用它的模型说一句（真实模型会真调）
            try:
                say = call_model(model_of(aid), [
                    {"role": "system", "content": agents[aid]["sys"]},
                    {"role": "user", "content": f"工具 {tool} 返回：{brief}\n用一句话说明结论。"}])
                emit(run_id, aid, "say", str(say)[:400])
            except Exception as e:
                emit(run_id, aid, "warn", f"该 agent 的模型不可用（{type(e).__name__}），已跳过发言")

        # 3) 代码验收（不是模型自评）
        verdict = verify_code(results)
        emit(run_id, "reviewer", "verify", "代码验收：" + ("通过" if verdict["ok"] else "未通过"),
             verdict)
        if not verdict["ok"]:
            emit(run_id, "reviewer", "hint", "诊断：见 checks 里 ok=false 的项，按错因重跑或改参数（预算 1 次）")

        # 4) 落盘
        with LOCK:
            RUNS[run_id]["verdict"] = "pass" if verdict["ok"] else "fail"
            RUNS[run_id]["done"] = True
            RUNS[run_id]["results"] = results
            RUNS[run_id]["verdict_detail"] = verdict
        (RUNS_DIR / f"{run_id}.json").write_text(json.dumps({
            "run_id": run_id, "task": task, "asset": asset,
            "events": RUNS[run_id]["events"], "verdict": verdict,
            "results": [{k: v for k, v in r.items() if k != "raw"} for r in results],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        emit(run_id, "orchestrator", "done", f"运行结束：{RUNS[run_id]['verdict']}")
    except Exception as e:
        if isinstance(e, ValueError):
            emit(run_id, "orchestrator", "error", str(e))
        else:
            emit(run_id, "orchestrator", "error", traceback.format_exc()[-800:])
        with LOCK:
            RUNS[run_id]["done"] = True
            RUNS[run_id]["verdict"] = "error"

# ---------------------------------------------------------------- 图执行器
def _parse_json(text):
    if not isinstance(text, str):
        return None
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        v = json.loads(text[i:j + 1])
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def validate_decision(raw):
    """把管理者模型的原始输出规整成符合 ORCH_MANAGER_SYS 契约的决策字典。

    支持三种形态：纯 JSON / ```json 围栏 / function-calling(arguments)；
    并对字段做 schema 校验与类型强转。不合规返回 None（编排循环据此触发重试/降级）。
    """
    if not isinstance(raw, str):
        raw = str(raw or "")
    s = raw.strip()
    # 1) 去掉 ```json ... ``` 围栏
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    # 2) function-calling 形态：{"tool_calls":[{"function":{"arguments":"..."}}]}
    try:
        probe = json.loads(s)
        if isinstance(probe, dict) and isinstance(probe.get("tool_calls"), list) and probe["tool_calls"]:
            args = probe["tool_calls"][0].get("function", {}).get("arguments")
            if isinstance(args, str) and args.strip():
                s = args.strip()
            elif isinstance(args, dict):
                s = json.dumps(args, ensure_ascii=False)
    except Exception:
        pass
    # 3) 朴素兜底：取第一个 { 到最后一个 }
    data = _parse_json(s)
    if not isinstance(data, dict):
        return None
    status = str(data.get("status") or "").strip().lower()
    if status not in ("continue", "finish"):
        # 容错：语义上像收尾就判 finish
        if re.search(r"收尾|结束|完成|finish", str(data.get("thinking", "")) + str(data.get("answer", ""))):
            status = "finish"
        else:
            status = "continue"
    subs = []
    for x in (data.get("subtasks") or []):
        if not isinstance(x, dict):
            continue
        to = str(x.get("to") or "").strip()
        if not to:
            continue
        subs.append({
            "id": str(x.get("id") or ("S%d" % (len(subs) + 1))),
            "to": to,
            "task": str(x.get("task") or ""),
            "why": str(x.get("why") or ""),
            "handoff_from": str(x.get("handoff_from") or "").strip(),
            "context": str(x.get("context") or "").strip(),
        })
    conf = data.get("confidence")
    try:
        conf = float(conf) if conf is not None else 0.0
    except Exception:
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    return {
        "thinking": str(data.get("thinking") or "").strip(),
        "status": status,
        "memory_note": str(data.get("memory_note") or "").strip(),
        "subtasks": subs,
        "answer": str(data.get("answer") or "").strip(),
        "confidence": conf,
    }

def topo_order(nodes, links):
    ids = list(nodes)
    indeg = {i: 0 for i in ids}
    adj = {i: [] for i in ids}
    for l in links:
        a, b = l.get("from"), l.get("to")
        if a in nodes and b in nodes and a != b:
            adj[a].append(b); indeg[b] += 1
    q = [i for i in ids if indeg[i] == 0]; out = []
    while q:
        x = q.pop(0); out.append(x)
        for y in adj[x]:
            indeg[y] -= 1
            if indeg[y] == 0:
                q.append(y)
    return out, len(out) != len(ids)

def run_graph(run_id, task, cfg, graph):
    """按拓扑序执行节点图。节点输出会沿连线累积成下游的上下文。"""
    models = {m["id"]: m for m in cfg.get("models", [])}
    fallback_model = (cfg.get("models") or [{}])[0]
    nodes = {n["id"]: n for n in (graph.get("nodes") or []) if n.get("id")}
    links = [l for l in (graph.get("links") or [])
             if l.get("from") in nodes and l.get("to") in nodes]
    order, cyclic = topo_order(nodes, links)
    payload, results, gate = {}, [], None
    asset = ""
    aborted = False
    try:
        emit(run_id, "orchestrator", "task", f"工作流：{len(nodes)} 节点 / {len(links)} 连线")
        if cyclic:
            raise ValueError("连线成环，无法确定执行顺序")
        for nid in order:
            if run_id in ABORT:
                emit(run_id, "orchestrator", "abort", "收到中止指令")
                aborted = True
                break
            n = nodes[nid]; t = n.get("type"); p = n.get("params") or {}
            title = n.get("title") or nid
            ctx = {}
            for l in links:
                if l["to"] == nid:
                    ctx.update(payload.get(l["from"]) or {})
            emit(run_id, title, "node_start", f"▶ {title}", None, nid)

            if t == "input":
                # 本次提交的任务优先；节点里存的只是上一次的草稿，不能盖掉用户刚输入的内容
                task = (task or p.get("task") or "").strip()
                asset = resolve_asset(task)
                out = {"task": task, "asset": asset}
                if asset:
                    emit(run_id, title, "info", f"资产：{Path(asset).name}", None, nid)
                elif p.get("require_asset"):
                    emit(run_id, title, "error",
                         "没在任务里找到模型文件：任务里写上文件名（例如 astronaut_kid.glb），"
                         "或者从上面的下拉框挑一个。", None, nid)
                    raise ValueError("资产未找到，运行终止（先把模型指定清楚）")
                else:
                    emit(run_id, title, "info", "通用任务：不依赖 3D 资产", None, nid)

            elif t == "agent":
                model = models.get(p.get("model_id")) or fallback_model
                msgs = [{"role": "system", "content": p.get("sys", "")},
                        {"role": "user", "content": (p.get("prompt") or "") +
                         "\n任务：" + str(ctx.get("task") or task) +
                         "\n资产：" + str(ctx.get("asset") or asset) +
                         "\n上游：" + json.dumps({k: v for k, v in ctx.items() if k != "text"},
                                                ensure_ascii=False)[:1200]}]
                text = str(call_model(model, msgs))
                js = _parse_json(text) or {}
                out = dict(js); out["text"] = text
                if ctx.get("asset") or asset:
                    out.setdefault("asset", ctx.get("asset") or asset)
                if ctx.get("features"):
                    out.setdefault("features", ctx.get("features"))
                emit(run_id, title, "think", text[:500],
                     {"model": model.get("model"), "json": js or None}, nid)

            elif t == "tool":
                tool = p.get("tool") or "asset_list"
                args = {}
                for k, v in (p.get("args") or {}).items():
                    args[k] = ctx.get(v[1:]) if (isinstance(v, str) and v.startswith("$")) else v
                if tool in PATH_TOOLS and not args.get("path"):
                    args["path"] = asset or ctx.get("asset") or ""
                if tool in PATH_TOOLS and not args.get("path"):
                    raise ValueError("这个工具需要模型文件：请在任务里写明文件名，或用「载入默认」的 3D 工作流")
                args = {k: v for k, v in args.items() if v is not None}
                emit(run_id, title, "step", f"{tool} {json.dumps(args, ensure_ascii=False)}", None, nid)
                t0 = time.time()
                try:
                    if tool not in TOOLS:
                        raise ValueError(f"未知工具 {tool}")
                    res = TOOLS[tool](**args)
                except Exception as e:
                    emit(run_id, title, "error", f"{type(e).__name__}: {e}", None, nid)
                    res = {"kind": "error", "pass": False, "error": str(e)}
                dur = round(time.time() - t0, 2)
                results.append({"agent": title, "node": nid, "tool": tool,
                                "result": res, "seconds": dur})
                out = dict(res); out["tool"] = tool
                if tool == "asset_profile":
                    out["features"] = {k: res.get(k) for k in
                                       ("size", "diag", "longest_axis", "axis_lengths_desc",
                                        "aspect", "equiv_thickness_mm", "watertight",
                                        "boundary_edges", "nonmanifold_edges", "components")}
                    emit(run_id, title, "info",
                         "尺寸 " + json.dumps(res.get("size"), ensure_ascii=False) +
                         f"，最长边 {res.get('longest_axis')}，等效厚度约 {res.get('equiv_thickness_mm')} mm",
                         {"advice": res.get("advice")}, nid)
                brief = json.dumps(res, ensure_ascii=False)
                emit(run_id, title, "result", brief[:600] + ("…" if len(brief) > 600 else ""),
                     {"seconds": dur, "pass": res.get("pass")}, nid)
                if tool == "render_preview":
                    emit(run_id, title, "preview", res.get("caption") or "预览已更新",
                         {"url": res.get("url"), "caption": res.get("caption")}, nid)
                if res.get("preview"):
                    cap = "切成 %d 件，已导出到 out/" % res.get("n", 0)
                    emit(run_id, title, "preview", cap,
                         {"url": res["preview"], "caption": cap}, nid)

            elif t == "code":
                fn = p.get("fn") or "build_subtasks"
                emit(run_id, title, "step", f"代码：{fn}（确定性，不问模型）", None, nid)
                try:
                    if fn not in CODE_FNS:
                        raise ValueError(f"未注册的代码函数 {fn}")
                    res = CODE_FNS[fn](ctx)
                except Exception as e:
                    res = {"kind": "code", "fn": fn, "pass": False,
                           "error": f"{type(e).__name__}: {e}"}
                    emit(run_id, title, "error", res["error"], None, nid)
                out = dict(res)
                if ctx.get("asset") or asset:
                    out.setdefault("asset", ctx.get("asset") or asset)
                emit(run_id, title, "info",
                     f"拆出 {res.get('subtask_count', 0)} 个子任务；{res.get('note', '')}",
                     {"subtasks": res.get("subtasks")}, nid)

            elif t == "gate":
                gate = verify_code(results, p.get("criteria") or {})
                out = {"ok": gate["ok"], "verdict": gate}
                emit(run_id, title, "verify",
                     "代码验收：" + ("通过" if gate["ok"] else "未通过"), gate, nid)

            elif t == "output":
                answer = ""
                for r in reversed(results):
                    d = r.get("result") or {}
                    if d.get("kind") in ("python", "llm", "file", "http"):
                        answer = str(d.get("stdout") or d.get("text")
                                     or d.get("url") or "").strip()
                        if answer:
                            break
                if not answer:
                    answer = str(ctx.get("text", ""))[:400]
                out = {"summary": answer[:400], "answer": answer[:4000]}
                final = gate or verify_code(results)
                emit(run_id, title, "answer", answer[:1200] or "（本次没有文本产出）",
                     {"answer": answer[:4000]}, nid)
                emit(run_id, title, "done",
                     "结果：" + ("通过" if final["ok"] else "未通过"), None, nid)

            else:
                out = {}
                emit(run_id, title, "warn", f"未知节点类型：{t}", None, nid)

            payload[nid] = out
            okk = None
            if t == "tool":
                okk = bool(out.get("pass"))
            elif t == "gate":
                okk = bool(out.get("ok"))
            elif t == "output":
                okk = bool((gate or verify_code(results)).get("ok"))
            emit(run_id, title, "node_done", f"{title} 完成", {"ok": okk}, nid)

        final = gate or verify_code(results)
        if aborted:
            final = {"ok": False,
                     "checks": (final.get("checks") or []) + [
                         {"step": "abort", "check": "运行被用户中止，结果不完整", "ok": False}]}
        with LOCK:
            RUNS[run_id]["verdict"] = "pass" if final["ok"] else "fail"
            RUNS[run_id]["done"] = True
            RUNS[run_id]["results"] = results
            RUNS[run_id]["verdict_detail"] = final
        (RUNS_DIR / f"{run_id}.json").write_text(json.dumps({
            "run_id": run_id, "task": task, "asset": asset, "graph": graph,
            "events": RUNS[run_id]["events"], "verdict": final, "results": results,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        emit(run_id, "orchestrator", "done", f"运行结束：{RUNS[run_id]['verdict']}")
    except Exception:
        emit(run_id, "orchestrator", "error", traceback.format_exc()[-800:])
        with LOCK:
            RUNS[run_id]["done"] = True
            RUNS[run_id]["verdict"] = "error"

# ---------------------------------------------------------------- 通用求解器（任意问题）
SOLVE_SYS = """你是通用问题求解规划器。把用户的任意问题拆成一张可执行的工作流图，只输出一个 JSON：
{"goal":"一句话目标","analysis":"推敲过程","steps":[...],"criteria":{"all_pass":true,"expect_contains":[]},"deliverable":"最终产物说明"}
每个 step 形如：
{"type":"agent","title":"分析","sys":"...","prompt":"..."}
{"type":"tool","title":"执行","tool":"python","args":{"code":"print(1+1)"}}
{"type":"tool","title":"取资料","tool":"http","args":{"url":"https://..."}}
{"type":"tool","title":"写文件","tool":"file_write","args":{"name":"a.txt","content":"..."}}
{"type":"tool","title":"问模型","tool":"llm","args":{"prompt":"..."}}
可用工具：python / http / file_read / file_write / llm / asset_list / asset_profile / mesh_qc / slice_two / split_n / render_preview。
要求：至少有一个 tool 步骤真正产出结果；python 步骤用 print 输出答案，生成文件时把路径打印成 OUT:/out/xxx；steps 最多 6 个。
只输出 JSON，不要解释。"""

ANALYST_SYS = ("你是通用问题分析师。先想清楚目标、可用信息和最省事的可执行路径，"
               "只输出一个 JSON：{\"analysis\":\"推敲过程\",\"approach\":\"采用的方法\",\"risk\":\"风险\"}。")

GAME_HTML = """<!doctype html><html lang="zh"><meta charset="utf-8">
<title>贪吃蛇 · 工作台生成</title>
<style>body{margin:0;background:#0f1116;color:#e6e9ef;font:14px system-ui;display:grid;place-items:center;height:100vh}
canvas{background:#161a24;border:1px solid #2a3040;border-radius:10px}
b{color:#3ddc97}</style>
<div><h3>贪吃蛇（方向键 / WASD，回车重开）</h3>
<canvas id="c" width="400" height="400"></canvas><p>得分 <b id="s">0</b></p></div>
<script>
const c=document.getElementById('c'),x=c.getContext('2d'),S=20,N=20;
let d=[1,0],snake=[[5,5]],food=[10,10],score=0,over=false;
function draw(){x.fillStyle='#161a24';x.fillRect(0,0,400,400);
 x.fillStyle='#ff6b6b';x.fillRect(food[0]*S,food[1]*S,S-2,S-2);
 snake.forEach((p,i)=>{x.fillStyle=i?'#5b8cff':'#3ddc97';x.fillRect(p[0]*S,p[1]*S,S-2,S-2)});
 document.getElementById('s').textContent=score;}
function step(){if(over)return;const h=[snake[0][0]+d[0],snake[0][1]+d[1]];
 if(h[0]<0||h[1]<0||h[0]>=N||h[1]>=N||snake.some(p=>p[0]===h[0]&&p[1]===h[1])){over=true;return}
 snake.unshift(h);
 if(h[0]===food[0]&&h[1]===food[1]){score++;food=[Math.floor(Math.random()*N),Math.floor(Math.random()*N)]}else snake.pop();
 draw()}
function reset(){d=[1,0];snake=[[5,5]];food=[10,10];score=0;over=false;draw()}
addEventListener('keydown',e=>{const k=e.key.toLowerCase();
 const m={arrowright:[1,0],d:[1,0],arrowleft:[-1,0],a:[-1,0],arrowup:[0,-1],w:[0,-1],arrowdown:[0,1],s:[0,1]};
 if(m[k]&&!(m[k][0]===-d[0]&&m[k][1]===-d[1]))d=m[k];
 if(k==='enter')reset();});
reset();setInterval(step,120);
</script></html>"""


def _analysis_step(task, topic):
    return {"type": "agent", "title": "推敲 · " + topic[:6], "sys": ANALYST_SYS,
            "prompt": "任务：" + task[:300] + "\n请给出推敲过程。",
            "model_id": "mock-deep", "why": "先把问题拆清楚再动手"}


def mock_plan(task):
    """离线演示规划器：按任务特征挑一条真能跑出结果的路径。接真实模型后由 SOLVE_SYS 接管。"""
    t = (task or "").strip()
    low = t.lower()
    base = {"goal": t[:80], "analysis": "", "steps": [],
            "criteria": {"all_pass": True}, "deliverable": ""}
    asset = resolve_asset(t)
    if asset:
        base["_asset"] = asset

    # 1) 3D 网格类 —— 复用原来的专用工具链
    if re.search(r"\.glb|\.gltf|\.stl|\.obj|水密|拆件|分件|切成|切.*件|入厂", low):
        base["analysis"] = ("这是网格几何问题，不能靠文字回答：先量尺寸与最长边定切面，"
                            "再确认焊合后没有破洞/非流形边，最后切一刀并保证两件各自水密、体积守恒。"
                            "切面法向取包围盒最长的那一维，比随手选轴更稳。")
        base["steps"] = [
            {"type": "agent", "title": "切分推敲",
             "sys": "你是拆件规划者。只输出一个 JSON：{\"axis\":1,\"height\":null,\"why\":\"沿最长边中线切\"}。",
             "prompt": "看任务决定切面方向。", "model_id": "mock-deep", "why": "先定切面再动刀"},
            {"type": "tool", "title": "模型体检", "tool": "asset_profile",
             "args": {"path": "$asset"}, "why": "量尺寸、体积、等效厚度"},
            {"type": "tool", "title": "入厂质检", "tool": "mesh_qc",
             "args": {"path": "$asset"}, "why": "带破洞切下去两件都漏"},
            {"type": "tool", "title": "切成两件", "tool": "slice_two",
             "args": {"path": "$asset", "axis": 1}, "why": "切完两件都要水密且体积守恒"},
        ]
        base["deliverable"] = "两件水密网格 + 体积守恒校验"
        return base

    # 2) 生成小游戏 / 网页 —— 产出一个能直接打开的可玩文件
    if re.search(r"小游戏|游戏|网页|html|贪吃蛇|打砖块", low):
        base["analysis"] = ("用户要的是一个能玩的东西，不是一段说明。最省事的路径是"
                            "生成一个自带 canvas、键盘事件和计分的单文件 HTML5，"
                            "写到 out/ 里让用户点开即玩；验收看文件是否真的生成。")
        base["steps"] = [
            _analysis_step(t, "生成小游戏"),
            {"type": "tool", "title": "生成游戏文件", "tool": "file_write",
             "args": {"name": "snake.html", "content": GAME_HTML},
             "why": "把可玩的 HTML5 真正写出来，而不是只给代码文本"},
        ]
        base["criteria"] = {"all_pass": True, "min_checks": 1}
        base["deliverable"] = "一个可直接打开的 HTML5 贪吃蛇小游戏"
        return base

    # 3) 数字 / 统计 —— 交给万能的 Python 工具真算一遍
    nums = re.findall(r"-?\d+(?:\.\d+)?", t)
    if nums and re.search(r"平均|求和|总和|最大|最小|方差|中位数|计算|算|多少|加|减|乘|除|[\+=]", t):
        ns = [float(x) for x in nums]
        code = ("nums = %r\n" % ns +
                "print('识别到的数值：', nums)\n"
                "print('个数：', len(nums))\n"
                "print('求和：', sum(nums))\n"
                "print('平均：', sum(nums)/len(nums))\n"
                "print('最大：', max(nums))\n"
                "print('最小：', min(nums))\n")
        base["analysis"] = ("这是可计算的问题：把任务里的数值抽出来，用独立 Python 进程"
                            "算求和/平均/极值，并把 stdout 作为可复核的证据；不靠模型口算。")
        base["steps"] = [
            _analysis_step(t, "数值统计"),
            {"type": "tool", "title": "执行计算", "tool": "python",
             "args": {"code": code}, "why": "让代码真算一遍，结果可复现"},
        ]
        base["criteria"] = {"all_pass": True, "expect_contains": ["求和："]}
        base["deliverable"] = "求和 / 平均 / 最大 / 最小的计算结果"
        return base

    # 4) 兜底 —— 任意主题都产出一份结构化的求解报告
    report = "\n".join([
        "# 任务求解报告", "", "## 原始任务", t, "",
        "## 目标拆解",
        "1. 明确输入与期望产出（要一个能被检查的东西，而不是一句解释）",
        "2. 列出已知信息与约束",
        "3. 选择能真正跑出结果的方法：计算 / 检索 / 生成文件 / 调用专用工具",
        "4. 用工具执行并留下可检查的产物",
        "5. 按验收判据复核", "",
        "## 执行记录",
        "- 规划来源：离线演示规划器（规则驱动）",
        "- 接入真实模型后，同一张图会由模型自行生成步骤与代码", "",
        "## 验收", "- 已生成可打开的 Markdown 报告文件", "",
    ])
    base["analysis"] = ("没有命中专用工具，就走通用路径：把问题整理成目标拆解、"
                        "可执行步骤、验收项三段的报告文件，保证任何输入都能留下一个"
                        "可打开的产物；同时在界面里标明当前是离线规划，避免假装成万能 AI。")
    base["steps"] = [
        _analysis_step(t, "通用求解"),
        {"type": "tool", "title": "生成求解报告", "tool": "file_write",
         "args": {"name": "report.md", "content": report},
         "why": "任何问题都先落一份可检查的交付物"},
    ]
    base["criteria"] = {"all_pass": True, "min_checks": 1}
    base["deliverable"] = "一份 Markdown 求解报告"
    return base


def plan_for_task(task, cfg):
    """有真实模型就让模型规划；否则回落到离线演示规划器。"""
    models = cfg.get("models") or []
    cands = [m for m in models
             if m.get("provider") != "mock"
             and (_api_key(m) or (m.get("provider") == "ollama" and ollama_online()))]
    for m in cands:
        try:
            raw = call_model(m, [
                {"role": "system", "content": SOLVE_SYS},
                {"role": "user", "content": task}], timeout=45, json_mode=True)
            p = _parse_json(raw)
            if p and isinstance(p.get("steps"), list) and p["steps"]:
                p["_source"] = m.get("name") or m.get("model")
                return p
        except Exception:
            pass
    p = mock_plan(task)
    p["_source"] = "离线演示规划器（未接入真实模型）"
    return p


def build_graph_from_plan(task, plan):
    """把规划结果变成一张 ComfyUI 式节点图：任务 -> 若干步骤 -> 验收 -> 结论。"""
    steps = [x for x in (plan.get("steps") or []) if isinstance(x, dict)][:8]
    task_text = task
    if not resolve_asset(task_text) and plan.get("_asset"):
        task_text = task_text + "（资产：" + plan["_asset"] + "）"
    nodes = [{"id": "in", "type": "input", "title": "任务", "x": 40, "y": 260,
              "params": {"task": task_text, "require_asset": False}}]
    for i, st in enumerate(steps, 1):
        typ = st.get("type") or ("tool" if st.get("tool") else "agent")
        if typ not in ("agent", "tool", "code"):
            typ = "tool" if st.get("tool") else "agent"
        n = {"id": "s%d" % i, "type": typ,
             "title": str(st.get("title") or ("步骤 %d" % i))[:24],
             "x": 40 + i * 320, "y": 40 + (i % 2) * 190, "params": {}}
        if typ == "agent":
            n["params"] = {"model_id": st.get("model_id") or "mock-deep",
                           "sys": st.get("sys") or ANALYST_SYS,
                           "prompt": st.get("prompt") or task}
        elif typ == "tool":
            n["params"] = {"tool": st.get("tool") or "python",
                           "args": st.get("args") or {}}
        else:
            n["params"] = {"fn": st.get("fn") or "build_subtasks"}
        nodes.append(n)
    gx = 40 + (len(steps) + 1) * 320
    nodes.append({"id": "gate", "type": "gate", "title": "验收", "x": gx, "y": 260,
                  "params": {"criteria": plan.get("criteria") or {"all_pass": True}}})
    nodes.append({"id": "out", "type": "output", "title": "结论",
                  "x": gx + 320, "y": 260, "params": {}})
    seen, links = set(), []
    def add(a, b):
        if (a, b) not in seen:
            seen.add((a, b)); links.append({"from": a, "to": b})
    ids = [n["id"] for n in nodes]
    for a, b in zip(ids, ids[1:]):
        add(a, b)
    for n in nodes:                      # 每个工具节点都能拿到 input 里的 $asset
        if n["type"] == "tool":
            add("in", n["id"])
    return {"nodes": nodes, "links": links}



# ---------------------------------------------------------------- 任务委派：管理者模型 -> 子模型
ORCH_MANAGER_SYS = """你是「管理者模型 · 编排」。你是一个自主的多智能体编排者：可以分派并行子任务、根据回传结果决定再开一轮、把某个子模型的产出转交(handoff)给另一个子模型复核或深化，最后自己判断何时收尾。
用户原始任务：
{task}

当前进度：第 {round} 轮 / 最多 {max_rounds} 轮；已用子任务 {used}/{max_subtasks}；剩余预算约 {left}s。
可用子模型：
{workers_desc}

共享记忆（按时间顺序，前面各轮的结论与子模型结果都在这里）：
{memory}

请只输出一个 JSON，不要解释、不要代码块：
{"thinking":"你的推敲：还缺什么、为什么这样安排","status":"continue 或 finish","memory_note":"本轮要写入共享记忆的结论要点","subtasks":[{"id":"S1","to":"w1","task":"具体要它做什么","why":"为什么","handoff_from":"可选：上一轮某个子任务 id，表示把它的结果转交给本子任务","context":"可选：给它补充的背景"}],"answer":"仅 finish 时填最终答案；留空则由你稍后基于共享记忆汇总","confidence":0.0~1.0}
规则：
- status=continue 时 subtasks 至少 1 条、最多 4 条，它们会在本轮并行后台运行。
- 需要复核/深化别人的产出时，用 handoff_from 指向那个子任务的 id。
- 觉得信息已足够、或预算快用完时，status=finish 并直接写出最终答案。
- 不要重复已经做过的子任务；每轮都要基于共享记忆往前推进。
"""

ORCH_WORKER_SYS = """你是「子模型 · {name}」，职责：{duty}。
管理者在后台交给你一个子任务。请直接给出你的结果/结论，写成短条目，不要泛泛而谈，不要输出 JSON。用中文回答。
"""

ORCH_FINAL_SYS = """你是「管理者模型 · 汇总」。下面是多轮自主编排后共享记忆里的全部发现与子模型结果。
请把它们汇总成一份给用户的最终答案：先给结论，再列关键要点（注明来自哪个子模型/哪一轮），最后写风险与待确认处。
用中文，不要输出 JSON，不要写代码。
原任务：{task}
共享记忆：
{memory}
"""

# 兼容旧名（若别处仍引用）
DELEGATE_MANAGER_SYS = ORCH_MANAGER_SYS
DELEGATE_WORKER_SYS = ORCH_WORKER_SYS
DELEGATE_FINAL_SYS = ORCH_FINAL_SYS


def _model_by_id(cfg, mid):
    for m in (cfg.get("models") or []):
        if m.get("id") == mid:
            return m
    return (cfg.get("models") or [{}])[0]


DEMO_FB = "DEMO_FALLBACK|"


def _call_error_reason(msg):
    """把异常归一化成不泄漏任何凭据/地址的简短原因。"""
    low = (msg or "").lower()
    if "401" in low or "403" in low:
        return "API Key 无效或权限不足（401/403）"
    if "404" in low:
        return "接口地址或模型名不存在（404）"
    if "429" in low:
        return "请求过于频繁或额度不足（429）"
    if any(k in low for k in ("timed out", "timeout", "refused", "connect", "unreachable")):
        return "模型服务连不上（超时 / 拒绝连接）"
    return "模型调用失败"


def _safe_call(model, messages, timeout=90, json_mode=False):
    """单次模型调用。真实模型鉴权/连接失败时自动回退演示模型，
    用 DEMO_FALLBACK| 前缀把原因交给上层写进时间线，绝不因一个坏 Key 让整轮空转。"""
    try:
        t0 = time.time()
        text = str(call_model(model, messages, timeout=timeout, json_mode=json_mode))
        return text, None, round(time.time() - t0, 2)
    except Exception as e:
        msg = "%s: %s" % (type(e).__name__, e)
        if (model.get("provider") or "mock") != "mock":
            fb = dict(model)
            fb["model"] = "demo-fallback"
            return str(mock_reply(fb, messages)), DEMO_FB + _call_error_reason(msg), 0.0
        return "", msg, 0.0


def _orch_budget(cfg):
    o = cfg.get("orchestration") or {}

    def gi(k, d, lo, hi):
        try:
            v = int(o.get(k, d))
        except Exception:
            v = d
        return max(lo, min(hi, v))

    try:
        sd = float(o.get("step_delay", 0.45))
    except Exception:
        sd = 0.45
    return {"max_rounds": gi("max_rounds", 3, 1, 8),
            "max_subtasks": gi("max_subtasks", 12, 1, 48),
            "max_seconds": gi("max_seconds", 180, 10, 900),
            "parallel": bool(o.get("parallel", True)),
            "step_delay": max(0.0, min(3.0, sd))}


def _pace(run_id, delay):
    """Demo pacing: make each orchestration step visible; abort stays responsive."""
    if delay <= 0:
        return
    end = time.time() + delay
    while time.time() < end:
        if run_id in ABORT:
            return
        time.sleep(min(0.05, max(0.0, end - time.time())))


def _memory_view(mem, limit=6000):
    if not mem:
        return "（空：这是第一轮）"
    parts = []
    for m in mem:
        parts.append("[第%s轮 · %s · %s] %s" % (
            m.get("round"), m.get("by"), m.get("kind"), str(m.get("text"))[:900]))
    return "\n".join(parts)[-limit:]


def _mem_append(mem, rnd, by, kind, text):
    with LOCK:
        mem.append({"round": rnd, "by": by, "kind": kind, "text": str(text)[:1500]})


def _emit_memory(run_id, rnd, mem):
    with LOCK:
        snap = [dict(x) for x in mem[-12:]]
    emit(run_id, "共享记忆", "memory",
         "共享记忆已累计 %d 条（管理者与子模型都能读到）" % len(mem),
         {"round": rnd, "count": len(mem), "entries": snap})


def _delegation_finish(run_id, task, ok, workers_out, answer,
                       memory=None, rounds=0, reason=""):
    with LOCK:
        RUNS[run_id]["done"] = True
        RUNS[run_id]["verdict"] = "pass" if ok else "fail"
        RUNS[run_id]["results"] = workers_out
        RUNS[run_id]["memory"] = memory or []
        RUNS[run_id]["rounds"] = rounds
        RUNS[run_id]["verdict_detail"] = reason
    try:
        (RUNS_DIR / ("%s.json" % run_id)).write_text(json.dumps({
            "run_id": run_id, "task": task, "mode": "orchestration",
            "rounds": rounds, "budget_reason": reason,
            "events": RUNS[run_id]["events"], "memory": memory or [],
            "workers": workers_out, "answer": answer,
            "verdict": "pass" if ok else "fail",
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    emit(run_id, "orchestrator", "done",
         "运行结束：%s · %s" % ("完成" if ok else "未通过", reason or ""))


def run_delegation(run_id, task, cfg):
    """多轮主从自主编排：管理者决策循环 → 并行子任务/转交 → 共享记忆回写 → 再决策 → 收尾。"""
    from concurrent.futures import ThreadPoolExecutor
    cfg = normalize_cfg(cfg)
    b = _orch_budget(cfg)
    manager = _model_by_id(cfg, (cfg.get("manager") or {}).get("model_id"))
    workers = [w for w in (cfg.get("workers") or []) if isinstance(w, dict)]
    wmap = {str(w.get("id")): w for w in workers}
    mname = manager.get("name") or manager.get("model") or "管理者"
    # 开跑前统一做就绪判定：未就绪的模型自动回退演示，并把原因写进时间线与答案。
    # 这样「界面标演示 / 后端却发 401」不会再发生，也不会出现「没有收到子模型结果」。
    mgr_ok, mgr_why = model_state(manager)
    fallbacks = []
    if not mgr_ok:
        fallbacks.append("管理者「%s」：%s" % (mname, mgr_why))
    for _w in workers:
        _wm = _model_by_id(cfg, _w.get("model_id"))
        _ok, _why = model_state(_wm)
        if not _ok:
            fallbacks.append("%s「%s」：%s" % (_w.get("name") or _w.get("id"),
                                              _wm.get("name") or _wm.get("id"), _why))
    fallback_note = ""
    if fallbacks:
        fallback_note = ("⚠ 演示模式回退（本次不联网、不产生费用）\n"
                         + "".join("  · %s\n" % s for s in fallbacks[:8])
                         + "  以下为本地规则模拟结果，不代表真实模型输出；"
                           "填入有效 API Key 或启动本地 Ollama 后自动切换真实推理。\n\n")
    # 演示模型瞬时返回，给每步一点节奏，让多轮过程看得见；真实模型不额外等待
    pace = b["step_delay"] if (manager.get("provider") == "mock" or not mgr_ok) else 0.0
    t_start = time.time()
    mem, all_outs, prev = [], [], {}
    used = rounds = 0
    answer = reason = ""
    fb_alerted = False
    wdesc = "\n".join("- %s（%s）：%s" % (w.get("id"), w.get("name"), w.get("duty"))
                      for w in workers) or "- w1（分析员）：完成任务"
    try:
        emit(run_id, "任务", "dag_start",
             "① 任务已送达管理者模型「%s」，进入自主编排（最多 %d 轮 / %d 个子任务 / %ds）…"
             % (mname, b["max_rounds"], b["max_subtasks"], b["max_seconds"]),
             {"model": manager.get("model"), "provider": manager.get("provider"),
              "is_mock": manager.get("provider") == "mock" or not mgr_ok, "budget": b})
        if fallbacks:
            emit(run_id, "系统", "fallback",
                 "⚠ %d 个模型未就绪，已自动回退演示模型：%s"
                 % (len(fallbacks), "；".join(fallbacks[:6])),
                 {"fallbacks": fallbacks, "demo": True})
        _pace(run_id, pace)

        for rnd in range(1, b["max_rounds"] + 1):
            rounds = rnd
            if run_id in ABORT:
                reason = "运行被用户中止"
                emit(run_id, "orchestrator", "abort", "收到中止指令")
                _delegation_finish(run_id, task, False, all_outs, "", mem, rounds, reason)
                return
            left = b["max_seconds"] - (time.time() - t_start)
            if left <= 5:
                reason = "时间预算用尽，提前收尾"
                break
            emit(run_id, "编排器", "round_start",
                 "▸ 第 %d 轮开始（剩余预算约 %ds，共享记忆 %d 条）" % (rnd, int(left), len(mem)),
                 {"round": rnd, "memory_count": len(mem), "budget_left": int(left),
                  "max_rounds": b["max_rounds"]})
            _pace(run_id, pace)

            sysmsg = (ORCH_MANAGER_SYS
                      .replace("{task}", task)
                      .replace("{round}", str(rnd))
                      .replace("{max_rounds}", str(b["max_rounds"]))
                      .replace("{used}", str(used))
                      .replace("{max_subtasks}", str(b["max_subtasks"]))
                      .replace("{left}", str(int(left)))
                      .replace("{workers_desc}", wdesc)
                      .replace("{memory}", _memory_view(mem)))
            use_json = manager.get("provider") != "mock" and mgr_ok
            user_prompt = "原任务：%s\n请给出本轮决策（结构化 JSON）。" % task
            raw, err, sec = _safe_call(manager, [
                {"role": "system", "content": sysmsg},
                {"role": "user", "content": user_prompt}], json_mode=use_json)
            if err:
                if err.startswith(DEMO_FB):
                    why = err[len(DEMO_FB):]
                    if not fallback_note:
                        fallback_note = ("⚠ 演示模式回退（真实模型调用失败）\n"
                                         "  · %s\n  以下为本地规则模拟结果，不代表真实模型输出；"
                                         "请在「🔑 API Key」换成有效 Key。\n\n" % why)
                    if not fb_alerted:
                        fb_alerted = True
                        emit(run_id, "系统", "fallback",
                             "⚠ 模型调用失败（%s），已回退演示模型继续运行" % why,
                             {"reason": why, "demo": True})
                    err = None
                else:
                    emit(run_id, "管理者", "error", "第 %d 轮管理者调用失败：%s" % (rnd, err),
                         {"round": rnd, "error": err})
                    reason = "管理者模型调用失败"
                    break
            plan = validate_decision(raw)
            # 真实模型若首轮输出不合规，补一次重试；mock 不需要（始终合法）
            if plan is None and use_json:
                raw2, err2, sec2 = _safe_call(manager, [
                    {"role": "system", "content": sysmsg},
                    {"role": "user", "content": user_prompt +
                     "\n上轮输出不是合法 JSON，请严格只输出符合 schema 的 JSON 对象。"}],
                    json_mode=True)
                if not err2 or err2.startswith(DEMO_FB):
                    raw, sec = raw2, sec2
                    plan = validate_decision(raw)
            if plan is None:
                emit(run_id, "管理者", "error", "第 %d 轮管理者决策 JSON 无法解析/校验" % rnd,
                     {"round": rnd})
                reason = "管理者决策 JSON 解析失败"
                break
            thinking = str(plan.get("thinking") or raw[:400]).strip()
            status = "finish" if str(plan.get("status") or "").lower().startswith("finish") else "continue"
            note = str(plan.get("memory_note") or "").strip()
            subs = [x for x in (plan.get("subtasks") or []) if isinstance(x, dict)][:4]
            ans = str(plan.get("answer") or "").strip()
            conf = plan.get("confidence")
            room = b["max_subtasks"] - used
            if room <= 0:
                subs = []
            elif len(subs) > room:
                subs = subs[:room]
            if status == "continue" and not subs:
                status = "finish"
            emit(run_id, "管理者", "manager_think",
                 "② 第 %d 轮决策（%.1fs）：%s，本轮 %d 个子任务" %
                 (rnd, sec, "继续推进" if status == "continue" else "收尾", len(subs)),
                 {"round": rnd, "status": status, "thinking": thinking, "subtasks": subs,
                  "confidence": conf, "model": manager.get("model"),
                  "provider": manager.get("provider"), "seconds": sec, "raw": raw[:800]})
            _pace(run_id, pace)
            if note:
                _mem_append(mem, rnd, "管理者", "结论", note)

            if status == "finish":
                answer = ans
                reason = "管理者判断信息已足够（第 %d 轮收尾）" % rnd
                _emit_memory(run_id, rnd, mem)
                emit(run_id, "编排器", "manager_decide", "管理者决定收尾：%s" % reason,
                     {"round": rnd, "status": "finish", "confidence": conf})
                break

            def one(s, rnd=rnd, used_snapshot=used):
                wid = str(s.get("to") or "")
                w = wmap.get(wid) or (workers[0] if workers else {})
                m = _model_by_id(cfg, w.get("model_id"))
                name = str(w.get("name") or wid or "子模型")
                sub = str(s.get("task") or task)
                sid = str(s.get("id") or ("S%d" % (used_snapshot + 1)))
                hfrom = str(s.get("handoff_from") or "").strip()
                payload = ""
                if hfrom and hfrom in prev:
                    p = prev[hfrom]
                    payload = "\n\n【转交自 %s（%s）的产出】\n%s" % (
                        hfrom, p.get("name"), str(p.get("text"))[:1200])
                    emit(run_id, "编排器", "handoff",
                         "⇄ 转交：把「%s」的结果交给「%s」继续处理" % (p.get("name"), name),
                         {"round": rnd, "from": hfrom, "to": sid, "to_name": name,
                          "from_name": p.get("name"), "preview": str(p.get("text"))[:300]})
                    _pace(run_id, pace)
                extra = str(s.get("context") or "").strip()
                emit(run_id, "子模型·" + name, "worker_start",
                     "③ 第 %d 轮 → 「%s」（%s）后台运行…%s" %
                     (rnd, name, m.get("model"), "（含转交）" if payload else ""),
                     {"round": rnd, "id": sid, "to": wid, "name": name, "subtask": sub,
                      "why": s.get("why"), "model": m.get("model"),
                      "provider": m.get("provider"), "handoff_from": hfrom or None})
                _pace(run_id, pace)
                sysmsg2 = (ORCH_WORKER_SYS.replace("{name}", name)
                           .replace("{duty}", str(w.get("duty") or "完成子任务")))
                u = ("子任务：%s\n原任务：%s\n共享记忆：\n%s%s%s" %
                     (sub, task, _memory_view(mem, 2500), payload,
                      ("\n补充背景：" + extra) if extra else ""))
                text, err2, sec2 = _safe_call(m, [
                    {"role": "system", "content": sysmsg2},
                    {"role": "user", "content": u}])
                if err2 and err2.startswith(DEMO_FB):
                    text = "[演示回退 · %s]\n%s" % (err2[len(DEMO_FB):], text)
                    err2 = None
                elif err2:
                    text = "（该子模型调用失败：%s；检查模型库里的 Key / 地址 / 网络）" % err2
                emit(run_id, "子模型·" + name, "worker_done",
                     "④ 第 %d 轮「%s」结果回传（%.1fs）" % (rnd, name, sec2),
                     {"round": rnd, "id": sid, "to": wid, "name": name,
                      "text": text[:3000], "ok": not err2,
                      "model": m.get("model"), "seconds": sec2})
                _pace(run_id, pace)
                return {"id": sid, "round": rnd, "to": wid, "name": name,
                        "task": sub, "handoff_from": hfrom, "text": text, "ok": not err2}

            nworkers = max(1, min(6, len(subs))) if b["parallel"] else 1
            with ThreadPoolExecutor(max_workers=nworkers) as ex:
                futs = [ex.submit(one, s) for s in subs]
                outs = []
                for f in futs:
                    try:
                        outs.append(f.result())
                    except Exception as e:
                        outs.append({"id": "ERR", "round": rnd, "name": "子模型",
                                     "text": "子模型异常：%s" % e, "ok": False})
            for o in outs:
                all_outs.append(o)
                used += 1
                sid = str(o.get("id") or "")
                if sid and sid != "ERR":
                    prev[sid] = {"name": o.get("name"), "text": o.get("text")}
                _mem_append(mem, rnd, o.get("name") or "子模型", "回传", str(o.get("text"))[:1200])
            _emit_memory(run_id, rnd, mem)
            _pace(run_id, pace)
            if run_id in ABORT:
                reason = "运行被用户中止"
                emit(run_id, "orchestrator", "abort", "收到中止指令")
                _delegation_finish(run_id, task, False, all_outs, "", mem, rounds, reason)
                return
            emit(run_id, "编排器", "manager_decide",
                 "本轮 %d 个结果已写入共享记忆（累计 %d 条），交管理者再决策…" % (len(outs), len(mem)),
                 {"round": rnd, "status": "continue", "memory_count": len(mem)})
            _pace(run_id, pace)
            if rnd == b["max_rounds"]:
                reason = "已达最大轮次 %d，自动收尾" % b["max_rounds"]

        # -------- 收尾：没有现成 answer 就由管理者基于共享记忆汇总 --------
        if not answer:
            fsys = (ORCH_FINAL_SYS.replace("{task}", task)
                    .replace("{memory}", _memory_view(mem, 8000)))
            final, err3, sec3 = _safe_call(manager, [
                {"role": "system", "content": fsys},
                {"role": "user", "content": "请汇总回传给用户的最终答案。"}])
            if (err3 and not err3.startswith(DEMO_FB)) or not str(final).strip():
                final = ("\n\n".join("[%s] %s" % (o.get("name"), str(o.get("text"))[:900])
                                     for o in all_outs)
                         or ("⚠ 本次运行没有取得任何子模型结果。\n"
                             "可能原因：模型调用全部失败，或管理者未分派子任务"
                             + ("（%s）" % reason if reason else "")
                             + "。\n建议：在「🔑 API Key」填入有效 Key，或把节点模型切到演示模型 A/B；"
                               "本地 Ollama 需先启动服务并拉取模型。"))
            answer = fallback_note + str(final)
            if not reason:
                reason = "管理者汇总完成"
            emit(run_id, "管理者", "final",
                 "⑤ 管理者汇总完成，最终答案已回传（%.1fs）" % sec3,
                 {"answer": answer[:5000], "workers": all_outs, "memory": mem,
                  "model": manager.get("model"), "seconds": sec3, "rounds": rounds})
        else:
            answer = fallback_note + answer
            emit(run_id, "管理者", "final",
                 "⑤ 最终答案已回传（管理者在第 %d 轮直接给出）" % rounds,
                 {"answer": answer[:5000], "workers": all_outs, "memory": mem,
                  "model": manager.get("model"), "rounds": rounds})
        ok = (bool(answer.strip()) and bool(all_outs)
              and all(o.get("ok") is not False for o in all_outs)
              and run_id not in ABORT)
        if run_id in ABORT:
            reason, ok, answer = "运行被用户中止", False, "运行被用户中止，结果不完整"
        _delegation_finish(run_id, task, ok, all_outs, answer[:5000], mem, rounds,
                           reason or "完成")
    except Exception:
        emit(run_id, "orchestrator", "error", traceback.format_exc()[-700:])
        _delegation_finish(run_id, task, False, all_outs, "", mem, rounds, "运行异常")


# ---------------------------------------------------------------- 协作编排
# 规划者：分析问题 → 翻译成子 Agent 能听懂的执行指令 → 复核汇总（分析/审核都归规划者）
# 子 Agent：识别指令 → 思考 → 交付结果（执行归子 Agent）
# 共享记忆 + 转交(handoff) + 多轮复核 = Agent 之间真正联合起来
TEAM_PLANNER_SYS = """你是「协作规划者 · 分析」：分析问题，并把它翻译成子 Agent 能直接照做的执行指令提示词。
任务：{task}
当前：第 {round} 轮 / 最多 {max_rounds} 轮
你负责的子 Agent（只能给这些 id 派指令）：
{workers_desc}
共享记忆（所有 Agent 都能读到）：
{memory}
上一轮子 Agent 结果：
{results}

只输出一个 JSON 对象：
{"analysis":"……","instructions":[{"to":"wk-1","prompt":"……","why":"……","context_from":""}],"gaps":"……","confidence":0.6}
要求：
1. analysis 写你对问题的分析（目标、已知、约束、拆分思路），不要客套话。
2. 每条 prompt 必须是「子 Agent 能听懂的执行指令」：写明具体目标、可用输入、输出格式、验收点；禁止只写「分析一下」「处理一下」。
3. context_from 可填上一轮某个结果的 id，把那个 Agent 的产出转交给本条指令的 Agent，实现协作；没有就留空字符串。
4. 只给你负责的子 Agent 派指令；不要输出 JSON 之外的文字。"""

TEAM_WORKER_SYS = """你是「执行 Agent · {name}」。
职责：{duty}
工作方式：① 复述你理解的指令 → ② 列出关键约束与可用输入 → ③ 说明思考步骤 → ④ 给出可复核的结果（结论、依据、假设、待确认）。
你会收到规划者翻译好的执行指令、原任务、共享记忆，可能还有另一个 Agent 转交过来的产出。
不要输出 JSON，直接给结构化的可读结论；不要编造没有依据的事实、文件或数字。"""

TEAM_REVIEW_SYS = """你是「协作规划者 · 复核」：审核所有子 Agent 的产出，并给出最终答案。
任务：{task}
当前：第 {round} 轮 / 最多 {max_rounds} 轮
可继续分派的子 Agent：
{workers_desc}
共享记忆：
{memory}
子 Agent 结果：
{results}

只输出一个 JSON 对象：
{"status":"finish|continue","review":"逐条复核意见","answer":"最终答案（finish 时必填）","followups":[{"to":"wk-1","prompt":"……","why":"……","context_from":""}],"confidence":0.8}
复核要看：是否答到指令、是否互相矛盾、是否可复核、风险与待确认。
信息已足够或轮次将用尽时 status=finish 并写完整 answer；
仍需补做时 status=continue，followups 给出下一轮执行指令（可带 context_from 做转交）。"""


def _strip_fence(raw):
    s = str(raw or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    return m.group(1).strip() if m else s


def _team_plan(raw, owned, task):
    """校验规划者的「分析 + 执行指令」JSON；不合规返回 None 交给上层降级。"""
    d = _parse_json(_strip_fence(raw))
    if not isinstance(d, dict):
        return None
    ins = []
    for x in (d.get("instructions") or []):
        if not isinstance(x, dict):
            continue
        to = str(x.get("to") or "").strip()
        prompt = str(x.get("prompt") or "").strip()
        if to not in owned or not prompt:
            continue
        ins.append({"to": to, "prompt": prompt[:1500],
                    "why": str(x.get("why") or "")[:300],
                    "context_from": str(x.get("context_from") or "").strip()[:40]})
    if not ins:
        return None
    return {"analysis": str(d.get("analysis") or "")[:2000],
            "instructions": ins[:8],
            "gaps": str(d.get("gaps") or "")[:500],
            "confidence": d.get("confidence")}


def _team_fallback_plan(owned, task):
    focus = ["把目标、已知信息、约束列清楚，并给出可检验的输出格式",
             "给出可落地的执行步骤 / 计算 / 内容，写清关键假设",
             "核对一致性、列出风险与待确认处",
             "把结论整理成 3~5 条可复核的短条目"]
    return {"analysis": "（模型未给出合规计划）按目标 / 已知 / 约束三层拆分，逐条翻译成执行指令。",
            "instructions": [
                {"to": wid,
                 "prompt": "针对任务「%s」：%s。输出 3~5 条短条目，写清结论与依据。" %
                           (task[:120], focus[i % 4]),
                 "why": "规划者按职责翻译的执行指令", "context_from": ""}
                for i, wid in enumerate(owned[:6])],
            "gaps": "规划者输出未通过 JSON 校验，已启用确定性兜底分派。",
            "confidence": 0.5}


def _team_review(raw, owned, rnd, mx):
    d = _parse_json(_strip_fence(raw))
    if not isinstance(d, dict):
        return None
    status = "finish" if str(d.get("status") or "").lower().startswith("finish") else "continue"
    followups = []
    for x in (d.get("followups") or []):
        if not isinstance(x, dict):
            continue
        to = str(x.get("to") or "").strip()
        prompt = str(x.get("prompt") or "").strip()
        if to in owned and prompt:
            followups.append({"to": to, "prompt": prompt[:1200],
                              "why": str(x.get("why") or "")[:300],
                              "context_from": str(x.get("context_from") or "").strip()[:40]})
    if rnd >= mx or (status == "continue" and not followups):
        status = "finish"
    return {"status": status,
            "review": str(d.get("review") or "")[:2000],
            "answer": str(d.get("answer") or "").strip()[:6000],
            "followups": followups[:6],
            "confidence": d.get("confidence")}


def _team_results_view(outs, limit=7000):
    if not outs:
        return "（本轮还没有任何子 Agent 结果）"
    s = "\n".join("[%s · %s] %s" % (o.get("id"), o.get("name"),
                                   str(o.get("text") or "")[:700])
                  for o in outs[-8:])
    return s[-limit:]


def _team_finish(run_id, task, ok, outs, answer, memory, rounds, reason, team):
    with LOCK:
        RUNS[run_id]["done"] = True
        RUNS[run_id]["verdict"] = "pass" if ok else "fail"
        RUNS[run_id]["results"] = outs
        RUNS[run_id]["memory"] = memory
        RUNS[run_id]["rounds"] = rounds
        RUNS[run_id]["verdict_detail"] = reason
    try:
        (RUNS_DIR / ("%s.json" % run_id)).write_text(json.dumps({
            "run_id": run_id, "task": task, "mode": "team_collab",
            "rounds": rounds, "budget_reason": reason,
            "team": [{"id": a["id"], "name": a["name"], "kind": a["kind"]}
                     for a in team],
            "events": RUNS[run_id]["events"], "memory": memory,
            "results": outs, "answer": answer,
            "verdict": "pass" if ok else "fail",
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    emit(run_id, "编排器", "done",
         "运行结束：%s · %s" % ("完成" if ok else "未通过", reason or ""))


def run_team(run_id, task, cfg):
    """协作主循环：规划者分析/翻译 → 子 Agent 思考执行 → 共享记忆 → 规划者复核 → 多轮转交。"""
    from concurrent.futures import ThreadPoolExecutor
    team = normalize_team(cfg)
    planners = [a for a in team if a["kind"] == "planner"]
    workers = [a for a in team if a["kind"] == "worker"]
    lead = planners[0]
    by_id = {a["id"]: a for a in team}
    wids = [w["id"] for w in workers]
    o = cfg.get("orchestration") or {}
    merge = str(cfg.get("result_merge") or "planner")
    try:
        threshold = max(0.0, min(1.0, float(cfg.get("confidence") or 0.6)))
    except (TypeError, ValueError):
        threshold = 0.6

    def _int(k, d, lo, hi):
        try:
            return max(lo, min(hi, int(o.get(k, d))))
        except (TypeError, ValueError):
            return d

    mx = _int("max_rounds", 2, 1, 4)
    try:
        pace = max(0.0, min(1.0, float(o.get("step_delay", 0.3))))
    except (TypeError, ValueError):
        pace = 0.3
    # 每个节点一个独立的模型接口，就绪状态逐节点判定
    state = {}
    for a in team:
        m = _model_by_id(cfg, a.get("model_id"))
        ok, why = model_state(m)
        state[a["id"]] = (m, ok, why)
    all_mock = all(not st[1] or (st[0].get("provider") == "mock") for st in state.values())
    if not all_mock:
        pace = 0.0                       # 有真实模型时不加人为节奏
    fallbacks = []
    for a in team:
        m, ok, why = state[a["id"]]
        if m.get("provider") == "mock":
            fallbacks.append("%s「%s」：演示模型（本地规则模拟）" %
                             (a["name"], m.get("name") or a["model_id"]))
        elif not ok:
            fallbacks.append("%s「%s」：%s" %
                             (a["name"], m.get("name") or a["model_id"], why))
    fallback_note = ""
    if fallbacks:
        fallback_note = ("⚠ 演示模式（本次不联网、不产生费用）\n"
                         + "".join("  · %s\n" % s for s in fallbacks[:8])
                         + "  以下为本地规则模拟结果，不代表真实模型输出；"
                           "点节点标签上的「API」填入有效 Key 即自动切换真实推理。\n\n")

    # ---- 画布连线决定协作关系：谁分派给谁、谁把产出转交给谁、哪些回传进入结果 ----
    LINKS = effective_links(cfg, team)
    worker_ids = set(wids)
    custom_dispatch = {l["dst"]: l["src"] for l in LINKS
                       if l["kind"] == "dispatch" and l["id"].startswith("usr:")}
    auto_dispatch = {l["id"][8:] for l in LINKS
                     if l["id"].startswith("auto:pw:")}   # "auto:pw:" 长度 8
    dispatch_targets = {p["id"]: [] for p in planners}
    for w in workers:
        if w["id"] in custom_dispatch:
            src = custom_dispatch[w["id"]]
            if src in dispatch_targets:
                dispatch_targets[src].append(w["id"])
        elif w["id"] in auto_dispatch and w["planner_id"] in dispatch_targets:
            dispatch_targets[w["planner_id"]].append(w["id"])
    handoff_map = {}
    for l in LINKS:
        if l["kind"] in ("handoff", "feedback") and l["src"] in worker_ids \
                and l["dst"] in worker_ids:
            handoff_map.setdefault(l["dst"], []).append(l["src"])
    result_workers = {l["src"] for l in LINKS
                      if l["kind"] == "result" and l["dst"] == "result"}
    if not result_workers:
        result_workers = set(wids)       # 全部断开时保底汇总，避免产出空答案
    custom_links = [l for l in LINKS if l["id"].startswith("usr:")]
    link_cuts = [str(x) for x in (cfg.get("cuts") or [])[:200]]

    mem, all_outs, prev = [], [], {}
    answer = reason = ""
    rounds = 0
    carry = []                           # 复核后追加给下一轮的转交指令
    try:
        emit(run_id, "编排器", "team_start",
             "任务已送达：%d 个规划者 · %d 个子 Agent · 最多 %d 轮协作"
             % (len(planners), len(workers), mx),
             {"planners": [p["id"] for p in planners],
              "workers": wids, "max_rounds": mx, "lead": lead["id"]})
        for a in team:
            m, ok, why = state[a["id"]]
            mode = ("演示模型" if m.get("provider") == "mock"
                    else ("真实接口" if ok else "未接入 → 演示回退"))
            emit(run_id, a["name"], "agent_ready",
                 "%s 就绪：%s（%s）" % ("规划者" if a["kind"] == "planner" else "子 Agent",
                                      m.get("name") or a["model_id"], mode),
                 {"kind": a["kind"], "model": m.get("model"),
                  "provider": m.get("provider"), "ready": ok,
                  "mode": mode, "reason": why, "name": a["name"]}, node=a["id"])
        if fallbacks:
            emit(run_id, "系统", "fallback",
                 "⚠ %d 个节点使用演示模型，本次不联网、不产生费用" % len(fallbacks),
                 {"fallbacks": fallbacks, "demo": True})
        if custom_links or link_cuts:
            emit(run_id, "编排器", "link_route",
                 "按画布连线组织协作：%d 条自定义连线 · %d 处断开 · %d 个 Agent 的回传进入结果"
                 % (len(custom_links), len(link_cuts), len(result_workers)),
                 {"links": [{"a": l["a"], "b": l["b"], "kind": l["kind"],
                             "label": LINK_KIND_CN.get(l["kind"], l["kind"])}
                            for l in custom_links],
                  "cuts": link_cuts, "result_workers": sorted(result_workers),
                  "dispatch": dispatch_targets})
        _pace(run_id, pace)

        for rnd in range(1, mx + 1):
            rounds = rnd
            if run_id in ABORT:
                reason = "运行被用户中止"
                emit(run_id, "编排器", "abort", "收到中止指令")
                _team_finish(run_id, task, False, all_outs, "", mem, rounds, reason, team)
                return
            emit(run_id, "编排器", "round_start",
                 "▸ 第 %d / %d 轮：规划者分析并翻译执行指令（共享记忆 %d 条）"
                 % (rnd, mx, len(mem)),
                 {"round": rnd, "max_rounds": mx, "memory_count": len(mem)})
            _pace(run_id, pace)
            jobs = []

            # ---- ① 每个规划者分析问题，并给自己的子 Agent 翻译执行指令 ----
            for p in planners:
                pp = p.get("params") or {}
                # 非主规划者节点的「最大轮次」：到轮后不再参与分派（主规划者的该控件映射全局轮次）
                try:
                    p_rounds = max(1, min(mx, int(pp.get("max_rounds", mx))))
                except (TypeError, ValueError):
                    p_rounds = mx
                if p["id"] != lead["id"] and rnd > p_rounds:
                    emit(run_id, p["name"], "planner_analysis",
                         "该规划者节点最大轮次为 %d，本轮不再分派指令" % p_rounds,
                         {"round": rnd, "instructions": [], "skipped": True}, node=p["id"])
                    continue
                owned = dispatch_targets.get(p["id"], [])
                if not owned:
                    emit(run_id, p["name"], "planner_analysis",
                         "本轮没有连接到该规划者的子 Agent，跳过分派",
                         {"round": rnd, "instructions": []}, node=p["id"])
                    continue
                wdesc = "\n".join("- %s（%s）：%s" %
                                  (w["id"], w["name"], w.get("duty") or "执行指令")
                                  for w in workers if w["id"] in owned)
                m, ok, _ = state[p["id"]]
                real = ok and m.get("provider") != "mock"
                sysmsg = (TEAM_PLANNER_SYS
                          .replace("{task}", task).replace("{round}", str(rnd))
                          .replace("{max_rounds}", str(mx))
                          .replace("{workers_desc}", wdesc)
                          .replace("{memory}", _memory_view(mem, 3500))
                          .replace("{results}", _team_results_view(all_outs, 3500)))
                pr = p.get("params") or {}
                pnotes = []
                if pr.get("style"):
                    pnotes.append("指令风格：%s" % str(pr["style"])[:200])
                if pr.get("depth"):
                    pnotes.append("分析深度：%s/5（数值越大拆得越细）" % str(pr["depth"])[:8])
                if pnotes:
                    sysmsg += "\n\n本规划者节点参数：\n" + "\n".join(pnotes)
                raw, err, sec = _safe_call(m, [
                    {"role": "system", "content": sysmsg},
                    {"role": "user", "content": "原任务：%s\n请给出分析与执行指令 JSON。" % task}],
                    json_mode=real)
                plan = None if err else _team_plan(raw, owned, task)
                if plan is None and real and not err:
                    raw2, err2, sec2 = _safe_call(m, [
                        {"role": "system", "content": sysmsg},
                        {"role": "user", "content": "请严格只输出符合 schema 的 JSON 对象。"}],
                        json_mode=True)
                    if not err2:
                        plan = _team_plan(raw2, owned, task)
                if plan is None:
                    plan = _team_fallback_plan(owned, task)
                    emit(run_id, p["name"], "planner_fallback",
                         "规划者输出未通过校验，已启用确定性指令兜底（不影响子 Agent 执行）",
                         {"round": rnd, "reason": err or "JSON 不合规"}, node=p["id"])
                emit(run_id, p["name"], "planner_analysis",
                     "分析完成，翻译成 %d 条执行指令（%.1fs）" % (len(plan["instructions"]), sec),
                     {"round": rnd, "analysis": plan["analysis"],
                      "gaps": plan["gaps"], "confidence": plan["confidence"],
                      "instructions": plan["instructions"], "model": m.get("model"),
                      "seconds": sec}, node=p["id"])
                _mem_append(mem, rnd, p["name"], "分析", plan["analysis"])
                for k, ins in enumerate(plan["instructions"], 1):
                    if pr.get("allow_handoff") is False:
                        ins["context_from"] = ""      # 节点关闭了转交
                    wname = (by_id.get(ins["to"]) or {}).get("name", ins["to"])
                    emit(run_id, p["name"], "instruction",
                         "→ %s：%s" % (wname, str(ins["prompt"])[:110]),
                         {"round": rnd, "to": ins["to"], "to_name": wname,
                          "prompt": ins["prompt"], "why": ins["why"],
                          "context_from": ins["context_from"],
                          "from": p["id"], "from_name": p["name"]}, node=p["id"])
                    jobs.append({"id": "S%d-%d-%d" % (rnd, len(jobs) + 1, k),
                                 **ins, "from": p["id"]})

            # ---- 复核追加的转交指令（让 Agent 之间真正接力）----
            if carry:
                emit(run_id, lead["name"], "followup",
                     "复核后追加 %d 条转交指令进入第 %d 轮" % (len(carry), rnd),
                     {"round": rnd, "count": len(carry), "jobs": carry}, node=lead["id"])
                for ins in carry:
                    wname = (by_id.get(ins["to"]) or {}).get("name", ins["to"])
                    emit(run_id, lead["name"], "instruction",
                         "↻ 转交给 %s：%s" % (wname, str(ins["prompt"])[:110]),
                         {"round": rnd, "to": ins["to"], "to_name": wname,
                          "prompt": ins["prompt"], "why": ins["why"],
                          "context_from": ins["context_from"],
                          "from": lead["id"], "from_name": lead["name"]},
                         node=lead["id"])
                    jobs.append({"id": "H%d-%d" % (rnd, len(jobs) + 1),
                                 **ins, "from": lead["id"]})
                carry = []
            # 自定义「Agent → Agent」连线：即使该 Agent 没被规划者分派，也要接力执行
            dispatched = {j.get("to") for j in jobs}
            for dst, srcs in handoff_map.items():
                if dst in dispatched or dst not in by_id:
                    continue
                src_names = "、".join((by_id.get(s) or {}).get("name", s) for s in srcs)
                ins = {"to": dst,
                       "prompt": ("阅读共享记忆与上游 Agent（%s）按连线转交的产出，"
                                  "接着完成本节点职责并给出可复核结论。" % src_names[:100]),
                       "why": "按画布 Agent→Agent 连线接力", "context_from": ""}
                emit(run_id, "编排器", "instruction",
                     "按连线给 %s 追加接力任务" % by_id[dst]["name"],
                     {"round": rnd, "to": dst, "to_name": by_id[dst]["name"],
                      "prompt": ins["prompt"], "why": ins["why"],
                      "context_from": "", "from": "link", "from_name": "画布连线"},
                     node=dst)
                jobs.append({"id": "L%d-%d" % (rnd, len(jobs) + 1),
                             **ins, "from": "link"})
            if not jobs:
                jobs = [{"id": "S%d-1" % rnd, "to": wids[0],
                         "prompt": "针对任务「%s」给出 3~5 条可复核的结论与依据。" % task[:120],
                         "why": "无合规指令时的保底分派", "context_from": "",
                         "from": lead["id"]}]
                emit(run_id, lead["name"], "instruction",
                     "本轮未产生指令，已给 %s 一条保底执行指令" % (by_id[wids[0]]["name"]),
                     {"round": rnd, "to": wids[0], "prompt": jobs[0]["prompt"]},
                     node=lead["id"])
            _pace(run_id, pace)

            # ---- ② 子 Agent 识别指令 → 思考 → 执行 → 回传 ----
            def one(job):
                w = by_id.get(job["to"]) or workers[0]
                wp = w.get("params") or {}
                m_w0, ok_w, _ = state.get(w["id"], state[wids[0]])
                m_w = dict(m_w0)
                m_w["_temperature"] = wp.get("temperature", 0.3)
                m_w["_max_tokens"] = wp.get("max_tokens", 2048)
                hfrom = "" if wp.get("allow_handoff") is False else \
                        str(job.get("context_from") or "").strip()
                payload = ""
                if hfrom:
                    src = (prev.get(hfrom) if hfrom != "latest"
                           else (list(prev.values())[-1] if prev else None))
                    if src:
                        payload = ("\n\n【转交自 %s（%s）的产出】\n%s" %
                                   (src["name"], src["id"], str(src["text"])[:1200]))
                        emit(run_id, "编排器", "handoff",
                             "⇄ 转交：把「%s」的回传交给「%s」继续处理" % (src["name"], w["name"]),
                             {"round": rnd, "from": src["id"], "from_name": src["name"],
                              "to": w["id"], "to_name": w["name"]}, node=w["id"])
                # 画布上「Agent → Agent」自定义连线：把上游 Agent 的最近产出注入本次思考
                if wp.get("allow_handoff") is not False:
                    for src_id in handoff_map.get(w["id"], []):
                        src_name = (by_id.get(src_id) or {}).get("name", src_id)
                        txt = _latest_worker_text(all_outs, src_id)
                        if txt:
                            payload += ("\n\n【按连线转交 · %s 的产出】\n%s" %
                                        (src_name, txt[:1200]))
                        emit(run_id, "编排器", "handoff",
                             "⇄ 连线转交：「%s」→「%s」（%s）" %
                             (src_name, w["name"], "带产出接力" if txt else "等待首轮产出"),
                             {"round": rnd, "from": src_id, "from_name": src_name,
                              "to": w["id"], "to_name": w["name"],
                              "via_link": True, "has_output": bool(txt)}, node=w["id"])
                emit(run_id, w["name"], "dispatch",
                     "收到「%s」的执行指令，开始识别与思考…" %
                     (by_id.get(job["from"], {}).get("name") or "规划者"),
                     {"round": rnd, "id": job["id"], "prompt": job["prompt"],
                      "why": job.get("why"), "from": job.get("from"),
                      "handoff_from": hfrom or None}, node=w["id"])
                _pace(run_id, pace)
                sysmsg2 = (TEAM_WORKER_SYS.replace("{name}", w["name"])
                           .replace("{duty}", w.get("duty") or "完成执行指令"))
                mem_view = ("（该节点设置：不读取共享记忆）" if wp.get("read_memory") is False
                            else _memory_view(mem, 2500))
                u = ("执行指令：%s\n原任务：%s\n共享记忆：\n%s%s" %
                     (job["prompt"], task, mem_view, payload))
                text, err2, sec2 = _safe_call(m_w, [
                    {"role": "system", "content": sysmsg2},
                    {"role": "user", "content": u}])
                if err2 and err2.startswith(DEMO_FB):
                    text = "[演示回退 · %s]\n%s" % (err2[len(DEMO_FB):], text)
                    err2 = None
                elif err2:
                    text = "（该子 Agent 调用失败：%s）" % err2
                emit(run_id, w["name"], "worker_done",
                     "结果回传（%.1fs）：%s" % (sec2, str(text)[:90].replace("\n", " ")),
                     {"round": rnd, "id": job["id"], "to": w["id"], "name": w["name"],
                      "text": str(text)[:3000], "ok": not err2,
                      "model": m_w.get("model"), "seconds": sec2,
                      "prompt": job["prompt"]}, node=w["id"])
                return {"id": job["id"], "round": rnd, "to": w["id"],
                        "name": w["name"], "prompt": job["prompt"],
                        "text": str(text), "ok": not err2}

            with ThreadPoolExecutor(max_workers=max(1, min(6, len(jobs)))) as ex:
                futs = [ex.submit(one, j) for j in jobs]
                outs = []
                for f in futs:
                    try:
                        outs.append(f.result())
                    except Exception as e:
                        outs.append({"id": "ERR", "round": rnd, "name": "子 Agent",
                                     "text": "执行异常：%s" % type(e).__name__, "ok": False,
                                     "to": wids[0], "prompt": ""})
            for r in outs:
                all_outs.append(r)
                if r.get("id") and r["id"] != "ERR":
                    prev[r["id"]] = {"name": r["name"], "text": r["text"], "id": r["id"]}
                _mem_append(mem, rnd, r["name"], "回传",
                            "[%s] %s" % (str(r.get("prompt") or "")[:80],
                                        str(r.get("text") or "")[:1000]))
            with LOCK:
                mem_snap = [dict(x) for x in mem[-12:]]
            emit(run_id, "共享记忆", "memory",
                 "共享记忆已累计 %d 条（所有规划者与子 Agent 都能读到）" % len(mem),
                 {"round": rnd, "count": len(mem), "entries": mem_snap})
            _pace(run_id, pace)
            if run_id in ABORT:
                reason = "运行被用户中止"
                emit(run_id, "编排器", "abort", "收到中止指令")
                _team_finish(run_id, task, False, all_outs, "", mem, rounds, reason, team)
                return

            # ---- ③ 主规划者复核（分析与审核都归规划者）----
            lp = lead.get("params") or {}
            if lp.get("review") is False:
                emit(run_id, lead["name"], "planner_review",
                     "该规划者节点已关闭「负责复核」，本轮直接汇总子 Agent 回传",
                     {"round": rnd, "status": "finish", "skipped": True,
                      "review": "复核已由该节点参数关闭，改为结果节点汇总。",
                      "followups": [], "confidence": 1.0}, node=lead["id"])
                answer = ""
                reason = "规划者节点关闭复核，按回传汇总"
                break
            m_l, ok_l, _ = state[lead["id"]]
            real_l = ok_l and m_l.get("provider") != "mock"
            wdesc_all = "\n".join("- %s（%s）：%s" % (w["id"], w["name"], w.get("duty") or "")
                                  for w in workers)
            rsys = (TEAM_REVIEW_SYS.replace("{task}", task)
                    .replace("{round}", str(rnd)).replace("{max_rounds}", str(mx))
                    .replace("{workers_desc}", wdesc_all)
                    .replace("{memory}", _memory_view(mem, 4000))
                    .replace("{results}", _team_results_view(all_outs, 5000)))
            raw3, err3, sec3 = _safe_call(m_l, [
                {"role": "system", "content": rsys},
                {"role": "user", "content": "请逐条复核子 Agent 的产出，并决定收尾还是补做。"}],
                json_mode=real_l)
            rev = None if err3 else _team_review(raw3, wids, rnd, mx)
            if rev is None:
                rev = {"status": "finish" if rnd >= mx else "continue",
                       "review": "复核输出未通过 JSON 校验，按轮次规则决定收尾。",
                       "answer": "",
                       "followups": ([{"to": wids[0],
                                       "prompt": "结合共享记忆里其他 Agent 的回传做交叉验证，补一句待确认。",
                                       "why": "复核降级后的转交补做",
                                       "context_from": "latest"}]
                                     if rnd < mx else []),
                       "confidence": 0.5}
            rev["status"] = "finish" if rnd >= mx else rev["status"]
            emit(run_id, lead["name"], "planner_review",
                 "复核完成（%.1fs）：%s" %
                 (sec3, "判定可以收尾" if rev["status"] == "finish" else "需要补做一轮"),
                 {"round": rnd, "status": rev["status"], "review": rev["review"],
                  "followups": rev["followups"], "confidence": rev["confidence"],
                  "model": m_l.get("model"), "seconds": sec3}, node=lead["id"])
            _mem_append(mem, rnd, lead["name"], "复核", rev["review"])
            if rev["status"] == "finish":
                answer = rev["answer"]
                reason = "规划者复核通过（第 %d 轮收尾）" % rnd
                break
            carry = rev["followups"]
            _pace(run_id, pace)

        # ---- ④ 收尾：只有连到「结果」节点的回传才进入最终答案 ----
        result_outs = [r for r in all_outs if r.get("to") in result_workers]
        if not result_outs:
            result_outs = all_outs[-len(wids):] if all_outs else []
        if not str(answer).strip():
            answer = ("【规划者复核后的最终答案】\n任务：%s\n\n" % task[:200]
                      + "分析由规划者完成并翻译成执行指令；执行由各子 Agent 完成，"
                        "结果已写入共享记忆供彼此读取。\n\n各 Agent 回传：\n"
                      + "\n\n".join("[%s] %s" % (r.get("name"), str(r.get("text") or "")[:900])
                                    for r in result_outs)
                      + "\n\n风险与待确认：若节点接口仍为演示模型，以上仅验证协作链路；"
                        "点节点标签上的「API」填入有效 Key 即切换真实推理。")
            if not reason:
                reason = "按最大轮次收尾，由规划者汇总"
        answer = fallback_note + answer
        emit(run_id, lead["name"], "final",
             "最终答案已回传（%d 轮 · %d 条结果 · 共享记忆 %d 条）" %
             (rounds, len(result_outs), len(mem)),
             {"answer": answer[:5000], "results": result_outs, "memory": mem,
              "rounds": rounds, "model": state[lead["id"]][0].get("model"),
              "result_workers": sorted(result_workers)},
             node=lead["id"])
        ok = (bool(result_outs) and all(r.get("ok") is not False for r in result_outs)
              and run_id not in ABORT)
        if run_id in ABORT:
            reason, ok, answer = "运行被用户中止", False, "运行被用户中止，结果不完整"
        _team_finish(run_id, task, ok, result_outs, answer[:5000], mem, rounds,
                     reason or "完成", team)
    except Exception:
        emit(run_id, "编排器", "error", traceback.format_exc()[-700:])
        _team_finish(run_id, task, False, all_outs, "", mem, rounds, "运行异常", team)


# ---------------------------------------------------------------- HTTP
from urllib.parse import unquote

SAFE_STATIC_EXTS = {".html", ".htm", ".md", ".txt", ".json", ".csv", ".yaml",
                    ".mmd", ".svg", ".png", ".jpg", ".jpeg", ".glb", ".pdf",
                    ".docx", ".xlsx", ".pptx", ".zip"}
STATIC_CTYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".md": "text/markdown; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".csv": "text/csv; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8", ".mmd": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".glb": "model/gltf-binary", ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
}


def new_run_id():
    """8 字节随机数：旧版仅 2 字节，run_id 可枚举导致事件流被越权读取。"""
    return "run-" + time.strftime("%H%M%S") + "-" + os.urandom(8).hex()


def active_runs():
    with LOCK:
        return sum(1 for r in RUNS.values() if not r.get("done"))


_OLLAMA_CACHE = {"t": 0.0, "ok": False}


def ollama_online():
    """探测本机 Ollama 是否真在跑（缓存 30 秒），避免界面标「真实」却连不上。"""
    now = time.time()
    if now - _OLLAMA_CACHE["t"] < 30:
        return _OLLAMA_CACHE["ok"]
    ok = False
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=0.4) as r:
            ok = (r.status == 200)
    except Exception:
        ok = False
    _OLLAMA_CACHE.update({"t": now, "ok": ok})
    return ok


class Handler(BaseHTTPRequestHandler):
    _ctx = {}

    def log_message(self, *a):
        pass   # 访问明细由 security.audit 按脱敏规则记录，不打原始请求行

    # ------------------------------------------------------------ 统一响应
    def _begin(self, code, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        for k, v in security.SECURITY_HEADERS:
            self.send_header(k, v)
        ctx = getattr(self, "_ctx", {}) or {}
        if ctx.get("set_cookie"):
            self.send_header("Set-Cookie", ctx["set_cookie"])
        for k, v in (extra or []):
            self.send_header(k, v)

    def _raw(self, code, ctype, body, extra=None):
        self._begin(code, ctype, extra)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _json(self, obj, code=200):
        self._raw(code, "application/json; charset=utf-8",
                  json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _html(self, text, code=200):
        self._raw(code, "text/html; charset=utf-8", str(text).encode("utf-8"))

    def _file(self, path):
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"error": "not found"}, 404)
            return
        ctype = STATIC_CTYPES.get(path.suffix.lower(), "text/html; charset=utf-8")
        self._raw(200, ctype, body)

    def _fail(self, exc, code=500, label="处理失败"):
        """异常只写服务端日志，对外给可关联的 ref，不回显堆栈/路径/Key。"""
        ref = security.fail_ref(exc)
        self._json({"error": "%s（ref=%s），详情仅写入服务端日志" % (label, ref)}, code)

    # ------------------------------------------------------------ 访问守卫
    def _guard(self, method):
        u = urlparse(self.path)
        g = security.GUARD.check(self, method, u.path, u.query)
        self._ctx = dict(g)
        if g["ok"]:
            if g.get("redirect"):
                # 首访 ?token= 立刻换成 HttpOnly 会话并 302 去掉地址栏里的令牌
                self._raw(302, "text/plain; charset=utf-8", b"",
                          [("Location", g["redirect"])])
                return None
            return u
        self._ctx["set_cookie"] = None
        code = g["code"]
        if code == 429:
            self._raw(429, "application/json; charset=utf-8",
                      json.dumps({"error": "请求过于频繁，请稍后再试"},
                                 ensure_ascii=False).encode("utf-8"),
                      [("Retry-After", str(g.get("retry_after", 1)))])
        elif code == 401:
            if method == "GET":
                self._html(security.LOGIN_HTML, 401)
            else:
                self._json({"error": "unauthorized"}, 401)
        else:
            self._json({"error": "请求来源不被允许（%s）" % g["reason"]}, 403)
        return None

    # ------------------------------------------------------------ 请求体
    def _read_json(self):
        if self.headers.get("Transfer-Encoding"):
            raise security.SecurityError("不支持分块传输")
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            raise security.SecurityError("缺少 Content-Length")
        if not raw_len.isdigit():
            raise security.SecurityError("Content-Length 不合法")
        n = int(raw_len)
        if n > security.S.max_body:
            raise security.SecurityError("请求体超出大小上限")
        try:
            self.connection.settimeout(10)
        except OSError:
            pass
        body = self.rfile.read(n) if n else b"{}"
        if len(body) != n:
            raise security.SecurityError("请求体读取不完整")
        try:
            obj = json.loads(body.decode("utf-8"))
        except Exception:
            raise security.SecurityError("JSON 格式不合法")
        if not isinstance(obj, dict):
            raise security.SecurityError("JSON 顶层必须是对象")
        return obj

    # ------------------------------------------------------------ 静态文件
    def _serve_static(self, root, rel):
        root = root.resolve()
        parts = [p for p in unquote(rel).replace("\\", "/").split("/") if p]
        bad = (not parts or len(parts) > 6 or
               any(p in (".", "..") or p.startswith(".") or
                   not re.match(r"^[A-Za-z0-9_\- .]{1,120}$", p) for p in parts))
        if bad:
            self._json({"error": "not found"}, 404)
            return
        fp = root.joinpath(*parts).resolve()
        if not fp.is_relative_to(root) or not fp.is_file():
            self._json({"error": "not found"}, 404)
            return
        ext = fp.suffix.lower()
        if ext not in SAFE_STATIC_EXTS:
            self._json({"error": "not found"}, 404)
            return
        ctype = STATIC_CTYPES.get(ext, "application/octet-stream")
        extra = []
        if ctype == "application/octet-stream":
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", fp.name)
            extra.append(("Content-Disposition",
                          'attachment; filename="%s"' % safe_name))
        try:
            self._raw(200, ctype, fp.read_bytes(), extra)
        except OSError:
            self._json({"error": "not found"}, 404)

    def _page(self, name):
        self._file(HERE / name)

    # ------------------------------------------------------------ GET
    def do_GET(self):
        u = self._guard("GET")
        if u is None:
            return
        p = u.path
        if p in ("/", "/index.html", "/console", "/console.html"):
            self._page("console.html"); return
        if p in ("/graph", "/graph.html"):
            self._page("graph.html"); return
        if p == "/classic":
            self._page("index.html"); return
        if p.startswith("/out/"):
            self._serve_static(HERE / "out", p[len("/out/"):]); return
        if p.startswith("/previews/"):
            self._serve_static(PREVIEWS, p[len("/previews/"):]); return
        if p == "/api/export/file":
            q = parse_qs(u.query)
            name = (q.get("name") or [""])[0]
            if not security.EXPORT_NAME_RE.match(name):
                self._json({"error": "文件名不合法"}, 400); return
            root = exporters.EXPORT_DIR.resolve()
            fp = (root / name).resolve()
            if not fp.is_relative_to(root) or not fp.is_file():
                self._json({"error": "not found"}, 404); return
            ext = fp.suffix.lower().lstrip(".")
            mime = next((m for e, m in exporters.FORMAT_META.values() if e == ext),
                        "application/octet-stream")
            inline = (q.get("inline") or [""])[0] == "1"
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", fp.name)
            extra = [] if inline else [(
                "Content-Disposition", 'attachment; filename="%s"' % safe_name)]
            try:
                self._raw(200, mime, fp.read_bytes(), extra)
            except OSError:
                self._json({"error": "not found"}, 404)
            return
        if p == "/api/graph":
            self._json({"graph": load_graph(), "tools": list(TOOLS),
                        "node_types": ["input", "agent", "tool", "code", "gate", "output"]})
            return
        if p == "/api/bootstrap":
            cfg = load_config()
            assets = []
            try:
                assets = tool_asset_list(60)["assets"]
            except Exception:
                pass
            # 对外视图不含任何密钥、解释器绝对路径、端口等内部信息
            self._json({"config": security.public_config(cfg), "assets": assets,
                        "graph": load_graph(), "tools": list(TOOLS),
                        "runtime": {"ollama": ollama_online()}})
            return
        if p == "/api/privacy/status":
            self._json(security.privacy_status(RUNS_DIR, exporters.EXPORT_DIR))
            return
        if p == "/api/events":
            q = parse_qs(u.query)
            rid = (q.get("run") or [""])[0]
            if not security.RUN_ID_RE.match(rid):
                self._json({"error": "运行 ID 不合法"}, 400); return
            try:
                since = max(0, int((q.get("since") or ["0"])[0]))
            except (TypeError, ValueError):
                since = 0
            with LOCK:
                run = RUNS.get(rid)
                if not run or run.get("owner") != self._ctx["session"]:
                    self._json({"error": "no such run"}, 404); return
                evs = [e for e in run["events"] if e["seq"] >= since]
                self._json({"events": evs, "done": run["done"],
                            "verdict": run.get("verdict", ""),
                            "verdict_detail": run.get("verdict_detail")})
            return
        self._json({"error": "not found"}, 404)

    # ------------------------------------------------------------ POST
    def _cfg_from(self, payload):
        """运行类接口接收的配置同样要消毒，禁止借 config 字段注入 base_url。"""
        incoming = payload.get("config")
        if not incoming:
            return load_config()
        return normalize_cfg(security.validate_config(incoming))

    def _start_run(self, target, task, cfg, extra=None):
        if active_runs() >= security.S.max_concurrent:
            self._json({"error": "同时运行的任务已达上限，请稍后再试"}, 429)
            return None
        rid = new_run_id()
        with LOCK:
            RUNS[rid] = {"events": [], "done": False, "verdict": "", "task": task,
                         "t0": time.time(), "owner": self._ctx["session"]}
            if extra:
                RUNS[rid].update(extra)
            while len(RUNS) > 40:
                RUNS.pop(next(iter(RUNS)), None)
        threading.Thread(target=target, args=(rid, task, cfg), daemon=True).start()
        return rid

    def do_POST(self):
        u = self._guard("POST")
        if u is None:
            return
        try:
            payload = self._read_json()
        except security.SecurityError as e:
            code = 413 if "超出大小上限" in str(e) else 400
            self._json({"error": str(e)}, code); return
        except Exception as e:
            self._fail(e, 400, "请求读取失败"); return
        p = u.path
        ip = self._ctx["ip"]

        # 登录：令牌换 HttpOnly 会话，令牌本身不落 Cookie
        if p == "/api/auth":
            if not security.S.require_auth:
                self._json({"ok": True, "mode": "local"}); return
            t = str(payload.get("token") or "")
            if not hmac.compare_digest(t, security.S.token):
                security.audit("auth_fail", ip, "POST", p, 401)
                self._json({"error": "令牌不正确"}, 401); return
            expected = security.session_value(security.S.token)
            parts = ["wb_session=%s" % expected, "HttpOnly", "SameSite=Strict",
                     "Path=/", "Max-Age=604800"]
            if security.S.secure_cookie:
                parts.append("Secure")
            security.audit("auth_ok", ip, "POST", p, 200)
            self._raw(200, "application/json; charset=utf-8", b'{"ok":true}',
                      [("Set-Cookie", "; ".join(parts))])
            return

        # 节点接口连通性自检：只传 model_id，URL/Key 一律取本机已校验配置
        if p == "/api/connect/test":
            mid = security._clean_str(payload.get("model_id"), 64)
            if not security.KEY_ID_RE.match(mid):
                self._json({"error": "模型 ID 不合法"}, 400); return
            model = next((m for m in (load_config().get("models") or [])
                          if isinstance(m, dict) and m.get("id") == mid), None)
            if not model:
                self._json({"error": "模型不存在（先保存配置）"}, 404); return
            try:
                res = test_model_connection(model)
            except security.SecurityError as e:
                res = {"ok": False, "code": "blocked", "message": "该接口地址被安全策略拒绝：" + str(e)}
            except Exception as e:
                res = {"ok": False, "code": "error",
                       "message": "测试异常：%s" % type(e).__name__}
            security.audit("connect_test", ip, "POST", p, 200,
                           "model=%s code=%s" % (mid, res.get("code")))
            self._json(res); return

        # 密钥独立接口：永远不经过 config.json，也不回显明文
        if p == "/api/key":
            mid = security._clean_str(payload.get("model_id"), 64)
            if not security.KEY_ID_RE.match(mid) or not any(
                    m.get("id") == mid for m in (load_config().get("models") or [])):
                self._json({"error": "模型不存在"}, 400); return
            key = str(payload.get("key") or "")
            if len(key) > 512:
                self._json({"error": "密钥长度不合法"}, 400); return
            if key != security.KEEP:
                security.STORE.set(mid, "" if key == security.CLEAR else key)
                security.audit("key_change", ip, "POST", p, 200,
                               "model=%s action=%s" %
                               (mid, "clear" if not key or key == security.CLEAR else "set"))
            k = security.STORE.get(mid)
            self._json({"ok": True, "has_key": bool(k),
                        "key_hint": security.key_hint(k)})
            return

        # 隐私：状态查询与一键清除
        if p == "/api/privacy/purge":
            if str(payload.get("confirm") or "") != "PURGE":
                self._json({"error": "需要显式确认（confirm=PURGE）"}, 400); return
            scope = str(payload.get("scope") or "all")
            try:
                removed = security.purge(scope, RUNS_DIR, exporters.EXPORT_DIR)
            except security.SecurityError as e:
                self._json({"error": str(e)}, 400); return
            if scope in ("runs", "all"):
                with LOCK:
                    RUNS.clear()
                    ABORT.clear()
            security.audit("privacy_purge", ip, "POST", p, 200, "scope=%s" % scope)
            self._json({"ok": True, "removed": removed}); return

        if p == "/api/export":
            try:
                safe = security.clean_export_payload(payload)
                res = exporters.export(safe.get("format") or "markdown", safe)
                res["url"] = "/api/export/file?name=" + quote(res["name"])
                res["preview_url"] = res["url"] + "&inline=1"
                security.audit("export", ip, "POST", p, 200,
                               "format=%s" % res["format"])
                self._json(res); return
            except security.SecurityError as e:
                self._json({"error": str(e)}, 400); return
            except Exception as e:
                self._fail(e, 500, "导出失败"); return

        if p == "/api/preview":
            task = security.clean_task(payload.get("task"))
            asset = resolve_asset(task)
            if not asset:
                self._json({"error": "没找到模型：任务里写上文件名"}, 400); return
            try:
                res = tool_render_preview(asset, mode="solid", size=760,
                                          caption="%s（%d 面）" % (
                                              Path(asset).name,
                                              len(_geom(asset)["welded"].faces)))
            except Exception as e:
                self._fail(e, 500, "预览生成失败"); return
            self._json(res); return

        # 新版协作编排：规划者分析/翻译/复核 + 子 Agent 执行，事件按 node 归属到消息栏
        if p == "/api/team/run":
            task = security.clean_task(payload.get("task"))
            if not task:
                self._json({"error": "empty task"}, 400); return
            try:
                cfg = self._cfg_from(payload)
                normalize_team(cfg)
                if not [a for a in cfg["team"] if a["kind"] == "planner"]:
                    self._json({"error": "至少需要一个规划者"}, 400); return
                if not [a for a in cfg["team"] if a["kind"] == "worker"]:
                    self._json({"error": "至少需要一个子 Agent"}, 400); return
            except security.SecurityError as e:
                self._json({"error": str(e)}, 400); return
            rid = self._start_run(run_team, task, cfg)
            if rid:
                security.audit("run_start", ip, "POST", p, 200, "kind=team")
                self._json({"run_id": rid})
            return

        if p == "/api/delegate":
            task = security.clean_task(payload.get("task"))
            if not task:
                self._json({"error": "empty task"}, 400); return
            try:
                cfg = self._cfg_from(payload)
            except security.SecurityError as e:
                self._json({"error": str(e)}, 400); return
            bud = payload.get("budget")
            if isinstance(bud, dict):
                cfg["orchestration"].update({k: v for k, v in bud.items()
                                             if k in ("max_rounds", "max_subtasks",
                                                      "max_seconds", "parallel",
                                                      "step_delay")})
            rid = self._start_run(run_delegation, task, cfg)
            if rid:
                security.audit("run_start", ip, "POST", p, 200, "kind=delegate")
                self._json({"run_id": rid})
            return

        if p == "/api/solve":
            task = security.clean_task(payload.get("task"))
            if not task:
                self._json({"error": "empty task"}, 400); return
            try:
                cfg = self._cfg_from(payload)
                if payload.get("config"):
                    save_config(payload["config"])
                plan = plan_for_task(task, cfg)
                graph = build_graph_from_plan(task, plan)
            except security.SecurityError as e:
                self._json({"error": str(e)}, 400); return
            except Exception as e:
                self._fail(e, 500, "规划失败"); return
            rid = self._start_run(
                lambda rid, task, cfg: run_graph(rid, task, cfg, graph),
                task, cfg, {"graph": graph})
            if rid:
                emit(rid, "planner", "plan",
                     "求解计划：" + str(plan.get("goal") or task)[:120],
                     {"analysis": plan.get("analysis"),
                      "source": plan.get("_source") or "离线演示规划器",
                      "steps": [x.get("title") for x in (plan.get("steps") or [])],
                      "deliverable": plan.get("deliverable")})
                security.audit("run_start", ip, "POST", p, 200, "kind=solve")
                self._json({"run_id": rid, "graph": graph,
                            "source": plan.get("_source") or "离线演示规划器"})
            return

        if p == "/api/graph/run":
            try:
                cfg = self._cfg_from(payload)
                if payload.get("config"):
                    save_config(payload["config"])
                graph = security.validate_graph(payload.get("graph") or load_graph())
                if payload.get("save"):
                    save_graph(graph)
            except security.SecurityError as e:
                self._json({"error": str(e)}, 400); return
            task = security.clean_task(payload.get("task"))
            rid = self._start_run(
                lambda rid, task, cfg: run_graph(rid, task, cfg, graph), task, cfg)
            if rid:
                security.audit("run_start", ip, "POST", p, 200, "kind=graph")
                self._json({"run_id": rid})
            return

        if p == "/api/graph/save":
            try:
                save_graph(security.validate_graph(payload.get("graph") or {}))
            except security.SecurityError as e:
                self._json({"error": str(e)}, 400); return
            security.audit("graph_save", ip, "POST", p, 200)
            self._json({"ok": True}); return

        if p == "/api/run":
            task = security.clean_task(payload.get("task"))
            if not task:
                self._json({"error": "empty task"}, 400); return
            try:
                cfg = self._cfg_from(payload)
                if payload.get("config"):
                    save_config(payload["config"])
            except security.SecurityError as e:
                self._json({"error": str(e)}, 400); return
            rid = self._start_run(run_task, task, cfg)
            if rid:
                security.audit("run_start", ip, "POST", p, 200, "kind=run")
                self._json({"run_id": rid})
            return

        if p == "/api/abort":
            rid = str(payload.get("run") or "")
            if not security.RUN_ID_RE.match(rid):
                self._json({"error": "运行 ID 不合法"}, 400); return
            with LOCK:
                run = RUNS.get(rid)
                if run and run.get("owner") != self._ctx["session"]:
                    self._json({"error": "no such run"}, 404); return
                ABORT.add(rid)
                while len(ABORT) > 200:
                    ABORT.pop()
            self._json({"ok": True}); return

        if p == "/api/save":
            cfg = payload.get("config")
            if not isinstance(cfg, dict):
                self._json({"error": "缺少 config"}, 400); return
            try:
                save_config(cfg)
            except security.SecurityError as e:
                self._json({"error": str(e)}, 400); return
            security.audit("config_save", ip, "POST", p, 200)
            self._json({"ok": True}); return

        self._json({"error": "not found"}, 404)

    def _method_not_allowed(self):
        u = self._guard(self.command)
        if u is None:
            return
        self._json({"error": "method not allowed"}, 405)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed

    def do_HEAD(self):
        u = self._guard("HEAD")
        if u is None:
            return
        self._begin(405, "application/json; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()


class WorkbenchServer(ThreadingHTTPServer):
    # SO_REUSEADDR 在 Windows 上允许多个进程绑同一个 127.0.0.1:8765，
    # 后启动者会随机抢走请求（表现为「打不开了」/ 404）。关掉它即可独占端口：
    # 本进程活着时，任何别的程序都绑不上来；端口被别人占着时，我们立刻报错退出。
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        # 独占端口：我们活着时，别的程序即使开了 SO_REUSEADDR 也绑不上来抢请求。
        # 必须在 bind() 之前设置；个别系统不支持就跳过，不影响启动。
        import socket
        opt = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if opt is not None:
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, opt, 1)
            except OSError:
                pass
        super().server_bind()


def main():
    try:
        status_lines = security.S.startup_check()
    except security.SecurityError as e:
        print("安全自检失败：%s" % e, flush=True)
        sys.exit(2)
    load_config()   # 顺带把历史明文 Key 迁移进加密库并从 config.json 抹掉
    cleaned = security.cleanup_retention(RUNS_DIR)
    srv = None
    # 关掉 SO_REUSEADDR 后，刚被杀掉的旧连接可能处于 TIME_WAIT，短暂重试几次。
    for attempt in range(12):
        try:
            srv = WorkbenchServer((security.S.bind, PORT), Handler)
            break
        except OSError as e:
            if attempt == 11:
                print("端口 %d 已被其它程序占用，工作台未启动：%s" % (PORT, e),
                      flush=True)
                sys.exit(1)
            time.sleep(0.5)
    print("工作台已启动: http://%s:%d" % (security.S.bind, PORT), flush=True)
    for line in status_lines:
        print("  [安全] " + line, flush=True)
    if cleaned:
        print("  [隐私] 已清理 %d 份过期运行记录" % cleaned, flush=True)
    srv.serve_forever()

if __name__ == "__main__":
    main()
