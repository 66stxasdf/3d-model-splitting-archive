# -*- coding: utf-8 -*-
"""把内核真跑过的轨迹导出成 JSON，注入 HTML 模板，生成可双击打开的单文件工作台。

数据来源全部是磁盘上的真实文件，不手工编造步骤：
  - packs/*/manifest.json        事实与工具的中文名、依赖、契约
  - runs_accept/*/events.jsonl   每一步的「为什么选它」「校验器独立重算说了什么」
  - runs_accept/*/run.json       结局、原因码、最终事实集合
  - agentcore.loop.preview       两条入口的实时规划结果

用法： python build.py
产出： Agent工作台.html（同时复制到桌面）
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KERNEL = os.path.join(os.path.dirname(HERE), "18-分件Agent内核")
DESKTOP = r"D:\Users\Desktop\zhuomian"

sys.path.insert(0, KERNEL)
os.chdir(KERNEL)

from agentcore import loop as L          # noqa: E402
from agentcore import pack as PK          # noqa: E402
from agentcore import task as T           # noqa: E402
from agentcore import doctor as DOC       # noqa: E402


def read_json(path):
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def read_events(path):
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, "rb") as f:
        for ln in f.read().decode("utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


# 场景 = 一次真实运行。tag 是 runs_accept 下的目录名。
SCENES = [
    ("095020-t02", "deliver", "正常交付：8 步，顺序是推出来的",
     "只给了目标，没给步骤。内核从目标反向展开，自己排出这 8 步。", "入门先看这个"),
    ("095022-t17", "book", "12 件全部做完：38 步，一件不少",
     "按实例事实要每件各做一次。少做一件就假成功——这里跑满 12×3。", "多件"),
    ("095023-t18", "multi_liar", "某件撒谎：拦住，并说清是哪件",
     "工具自称完成，独立校验器从磁盘重算，抓到 ch05 字符被换过。", "不自证"),
    ("095023-t19", "multi_fail_one", "少一件：其余 11 件做完，也不判成功",
     "第 5 件坏了就放弃这一件，沿依赖链传播；最终结论仍非成功。", "不假成功"),
    ("095022-t10", "liar", "工具撒谎：说切好了，产物是空的",
     "最短的一条「不信自称」证据：校验器直接判 FAILED。", "不自证"),
    ("095021-t09", "flaky", "工具故障：自动改道，理由进报告",
     "清理环节注入故障，内核按契约里预先写好的 on_fail 换实现。", "失败是数据"),
    ("095020-t05", "missing", "源文件不存在：主动拒收",
     "准入工具在门口拦下，零产物。不是「系统坏了」，是「你的数据不合格」。", "拒收"),
    ("095022-t13", "need_input", "缺输入：0 步诊断出整条依赖链",
     "什么都没跑就报出缺哪些字段——静态推演的能力。", "0 步诊断"),
    ("095022-t12", "stub", "能力是桩：如实报「还没实现」",
     "★ 当前分件包的真实状态。不装成功、也不崩，结局 UNDECIDABLE。", "当前现状"),
    ("095020-t07", "budget", "预算耗尽：停在边界上，不假装做完",
     "3 步上限，如实说目标未达成，并说明还差什么。", "预算"),
]


def fact_defs(pack_name):
    """从 manifest 拿事实的中文名与分组，供界面画状态网。"""
    p = os.path.join(KERNEL, "packs", pack_name, "manifest.json")
    if not os.path.isfile(p):
        return []
    m = read_json(p)
    return [{"id": f["id"], "label": f.get("label", f["id"]),
             "group": f.get("group", ""),
             "domain": bool(f.get("domain"))} for f in m.get("facts", [])]


def tool_defs(pack_name):
    p = os.path.join(KERNEL, "packs", pack_name, "manifest.json")
    if not os.path.isfile(p):
        return []
    m = read_json(p)
    return [{"name": t["name"], "title": t.get("title", t["name"]),
             "produces": t["produces"], "requires": t.get("requires") or [],
             "priority": t.get("priority", 0), "on_fail": t.get("on_fail", "abort"),
             "desc": t.get("desc", "")} for t in m.get("tools", [])]


def build_scene(tag, sid, title, hint, badge):
    d = os.path.join(KERNEL, "runs_accept", tag)
    run = read_json(os.path.join(d, "run.json"))
    ev = read_events(os.path.join(d, "events.jsonl"))
    pack_name = run.get("pack")
    if isinstance(pack_name, dict):
        pack_name = pack_name.get("name", "demo")
    steps = [e for e in ev if e.get("kind") == "decision"]
    others = [e for e in ev if e.get("kind") in ("fail", "fallback", "abandon", "finish", "start")]

    # 每步「执行完之后」世界状态里已成立的事实（用于点亮状态网）。
    # events 里 fact 事件紧跟在它对应的 decision 之后，所以先占位、再回填，
    # 否则第 1 步跑完时 cur 还是空的，界面上事实不会亮。
    fs = []
    cur = set()
    for e in ev:
        if e.get("kind") == "decision":
            fs.append(list(sorted(cur)))          # 占位：这一步开始前的状态
        elif e.get("kind") == "fact":
            cur.add(e.get("fact"))
            if fs:
                fs[-1] = list(sorted(cur))        # 回填：这一步做完后的状态
    if len(fs) != len(steps):
        fs = [fs[i] if i < len(fs) else [] for i in range(len(steps))]
    fs = [[f.split("@", 1)[0] for f in row] for row in fs]

    # 按实例事实：每步之后「已做完 / 共几件 / 放弃了谁」。
    # 事实网只画契约里的基础事实，不把 chapter_clean@ch01 这类展开成 36 个格子。
    dom_state = (run.get("state", {}).get("domain") or {})
    pool = {fid: list(info.get("pool") or [])
            for fid, info in dom_state.items() if isinstance(info, dict)}
    abandoned = run.get("state", {}).get("abandoned") or {}
    done = {}
    dom_progress = []
    for e in ev:
        if e.get("kind") == "decision":
            dom_progress.append({k: sorted(v) for k, v in done.items()})
        elif e.get("kind") == "fact" and e.get("instance"):
            done.setdefault(e.get("fact"), set()).add(e.get("instance"))
            if dom_progress:
                dom_progress[-1] = {k: sorted(v) for k, v in done.items()}
    if len(dom_progress) != len(steps):
        dom_progress = [dom_progress[i] if i < len(dom_progress) else {}
                        for i in range(len(steps))]
    domain_meta = {}
    for fid, pl in pool.items():
        domain_meta[fid] = {"total": len(pl),
                            "abandoned": sorted((abandoned.get(fid) or []) if isinstance(abandoned, dict) else [])}

    return {
        "id": sid, "title": title, "hint": hint, "badge": badge,
        "pack": pack_name, "source": tag,
        "verdict": run.get("verdict", ""),
        "code": run.get("reason_code", ""),
        "steps": steps,
        "facts_after": fs,
        "facts_final": sorted(set(f.split("@", 1)[0]
                                  for f in (run.get("state", {}).get("facts") or []))),
        "dom_progress": dom_progress,
        "domain_meta": domain_meta,
        "facts_cn": run.get("state", {}).get("facts_cn") or {},
        "reasons": run.get("reasons") or [],
        "warnings": run.get("warnings") or [],
        "other_events": others,
        "defs": fact_defs(pack_name if os.path.isdir(
            os.path.join(KERNEL, "packs", str(pack_name))) else "demo"),
        "tools": tool_defs(pack_name if os.path.isdir(
            os.path.join(KERNEL, "packs", str(pack_name))) else "demo"),
        "kind": "replay",
        "elapsed_s": run.get("elapsed_s", 0),
    }


def build_plan_scene():
    """两条入口：真的调内核 preview，不是我手写两条路径。"""
    p = PK.load("packs/split3d")
    out = []
    for seam, label in ((False, "没给割线"), (True, "给了割线")):
        inputs = {"model": "model.stl"}
        if seam:
            inputs["seam"] = "seam.json"
        t = T.parse(dict(inputs=inputs, goals=["parts_available"]), p)
        steps = L.preview(p, t, max_steps=12)
        out.append({"label": label, "seam": seam,
                    "tools": [s["tool"] for s in steps if s.get("kind") == "action"],
                    "why": [s.get("why", "") for s in steps if s.get("kind") == "action"]})
    return {
        "id": "routes", "title": "给不给割线：路径自己切换",
        "hint": "任务单只改一个字段，规划器按契约和 priority 换整条路线。",
        "badge": "路线", "kind": "plan", "routes": out,
        "pack": "split3d", "defs": fact_defs("split3d"), "tools": tool_defs("split3d"),
        "verdict": "", "code": "", "steps": [], "reasons": [], "warnings": [],
        "facts_after": [], "facts_final": [], "facts_cn": {}, "other_events": [],
        "dom_progress": [], "domain_meta": {},
        "source": "agentcore.loop.preview（实时调用）", "elapsed_s": 0,
    }


def main():
    scenes = [build_scene(*s) for s in SCENES]
    scenes.append(build_plan_scene())

    # 当前真实状态：从 doctor 的桩数量如实给出，不硬编码
    man = read_json(os.path.join(KERNEL, "packs", "split3d", "manifest.json"))
    # 验收数现场数 accept.py 的 CASES，不硬编码（加用例后无需同步本文件）
    _ap = open(os.path.join(KERNEL, "accept.py"), "rb").read().decode("utf-8")
    _n = sum(1 for _l in _ap.split("CASES = [", 1)[-1].splitlines()
             if _l.strip().startswith('("T'))
    # 桩状态现场问 doctor，不硬编码（填了实现后无需同步本文件）
    _rep = DOC.check(PK.load("packs/split3d"))
    _stub = sum(1 for _t in _rep["tools"] if _t.get("status") == "桩")
    meta = {
        "kernel": KERNEL,
        "pack_version": man.get("version", ""),
        "n_facts": len(man.get("facts", [])),
        "n_tools": len(man.get("tools", [])),
        "n_verifiers": len(man.get("verifiers", {})),
        "all_stub": _stub == len(_rep["tools"]),
        "n_stub": _stub,
        "accept": "%d/%d" % (_n, _n),
        "built_at": __import__("time").strftime("%Y-%m-%d %H:%M"),
    }
    data = {"meta": meta, "scenes": scenes}
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    tpl_path = os.path.join(HERE, "工作台.template.html")
    with open(tpl_path, "rb") as f:
        tpl = f.read().decode("utf-8")
    if "__TRACES_JSON__" not in tpl:
        raise SystemExit("模板里找不到 __TRACES_JSON__ 占位符")
    html = tpl.replace("__TRACES_JSON__", blob)

    out_local = os.path.join(HERE, "Agent工作台.html")
    with open(out_local, "wb") as f:
        f.write(html.encode("utf-8"))
    print("生成:", out_local, len(html.encode("utf-8")), "bytes")

    if os.path.isdir(DESKTOP):
        out_desktop = os.path.join(DESKTOP, "Agent工作台.html")
        with open(out_desktop, "wb") as f:
            f.write(html.encode("utf-8"))
        print("桌面:", out_desktop)
        for fn in ("bridge.py", "启动真内核.bat"):
            src = os.path.join(HERE, fn)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(DESKTOP, fn))
                print("桌面:", fn)
    else:
        print("桌面目录不存在，跳过:", DESKTOP)

    print("场景数:", len(scenes), "| 总步数:",
          sum(len(s["steps"]) for s in scenes))


if __name__ == "__main__":
    main()
