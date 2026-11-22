# -*- coding: utf-8 -*-
"""验收：这个内核到底算不算 Agent。

要证的不是「能跑通」，而是这几件事：

1. **换领域不改内核。** 同一份代码跑文本分块（demo）和分件（split3d），
   内核一行都没动。这是分工能否成立的前提。
2. **顺序是推出来的。** 换目标就换路径，不是我写了 if。
3. **缺能力如实报。** split3d 的实现全是桩，它必须说「能力未提供」，
   而不是假装成功、也不是崩。
4. **不自证。** 工具撒谎（自称完成但产物是假的）必须被独立校验拦住。
5. **失败是数据。** 故障注入后自己改道，不是我手动指路。
6. **契约错误提前暴露。** 写工具的人交上来一份坏契约，doctor 当场拦下。

跑法：python accept.py
记录：accept.json
"""

import io
import json
import os
import re
import shutil
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from agentcore import doctor as D          # noqa: E402
from agentcore import loop as L            # noqa: E402
from agentcore import pack as PK           # noqa: E402
from agentcore import planner as PL        # noqa: E402
from agentcore import task as T            # noqa: E402
from agentcore import verdict as V         # noqa: E402

GEN = os.path.join(BASE, "fixtures", "gen")
ACCEPT = os.path.join(BASE, "runs_accept")
SAMPLE = os.path.join(BASE, "fixtures", "sample_small.txt")
BIG = os.path.join(BASE, "fixtures", "sample_big.txt")


# ------------------------------------------------------------------ 工具

def _variant(name, edits=None, files=None):
    """复制 demo 包并改几处，用来做故障注入/替换实现/造假。

    用复制而不是在原包上加开关：加开关等于让工具自己决定要不要失败，
    那就测不出「失败了会不会改道」。
    """
    dst = os.path.join(GEN, name)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(os.path.join(BASE, "packs", "demo"), dst)
    mp = os.path.join(dst, "manifest.json")
    with open(mp, encoding="utf-8") as f:
        m = json.load(f)
    m["name"] = name
    m["title"] = f"变体：{name}"
    for t in m["tools"]:
        if edits and t["name"] in edits:
            t.update(edits[t["name"]])
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    for fn, body in (files or {}).items():
        with open(os.path.join(dst, fn), "w", encoding="utf-8") as f:
            f.write(body)
    return dst


def _run(pack_dir, inputs, goals=None, planner="rule", sequence=None,
         budget=None, tag="x"):
    p = PK.load(pack_dir)
    obj = dict(inputs=inputs, goals=goals, planner=planner)
    if sequence:
        obj["sequence"] = sequence
    if budget:
        obj["budget"] = budget
    t = T.parse(obj, p)
    rid = time.strftime("%H%M%S") + f"-{tag}"
    r = L.Runner(p, t, planner=PL.make(planner, p, sequence),
                 run_dir=os.path.join(ACCEPT, rid))
    return r.run()


def _steps(run):
    return [(d["tool"], d["result"]) for d in run["agent"]["decisions"]]


def _has_warn(run, key):
    return any(key in w for w in run["warnings"])


# ------------------------------------------------------------------ 用例

def t01_only_inspect():
    """最小目标：只要质检，不该跑完整条链。"""
    r = _run(os.path.join(BASE, "packs", "demo"),
             dict(src=SAMPLE), goals=["input_ok"], tag="t01")
    return (r["verdict"] in V.PASSING and r["agent"]["steps"] == 1
            and not r["artifacts"],
            dict(verdict=r["verdict"], steps=r["agent"]["steps"],
                 artifacts=len(r["artifacts"]), path=[t for t, _ in _steps(r)]))


def t02_deliver():
    """默认交付：路径应该是推出来的 8 步。

    断言写的是**依赖约束**，不是完整顺序。原来写死了每一步的位置，
    结果规划器改成「路线优先」后，ingest/check_blocks 和 route 的先后变了——
    这两者之间本来就没有依赖，谁先都成立，写死只会让用例变脆而不会更严格。
    """
    r = _run(os.path.join(BASE, "packs", "demo"),
             dict(src=SAMPLE), goals=["exported"], tag="t02")
    tools = [t for t, _ in _steps(r)]
    idx = {t: i for i, t in enumerate(tools)}
    ok = (r["verdict"] in V.PASSING and len(tools) == 8
          and tools[0] == "probe" and tools[-1] == "export"
          and idx.get("cut_fixed", -1) > idx.get("check_blocks", 99)
          and idx.get("cut_fixed", -1) > idx.get("route", 99)
          and "cut_adaptive" not in tools
          and idx.get("cleanup", -1) > idx.get("cut_fixed", 99)
          and idx.get("verify_parts", -1) > idx.get("cleanup", 99))
    return ok, dict(verdict=r["verdict"], path=tools,
                    parts=r["state"]["values"].get("parts_n"))


def t03_full_includes_assemble():
    """加一个目标（重新拼合），路径必须自动多一步，且插在正确位置。"""
    r = _run(os.path.join(BASE, "packs", "demo"),
             dict(src=SAMPLE), goals=["exported", "assembled"], tag="t03")
    tools = [t for t, _ in _steps(r)]
    ok = (r["verdict"] in V.PASSING and "assemble" in tools
          and tools.index("assemble") < tools.index("export"))
    return ok, dict(verdict=r["verdict"], path=tools)


def t04_condition_branch():
    """任务单指定自适应模式，走的必须是另一个切分工具。"""
    r = _run(os.path.join(BASE, "packs", "demo"),
             dict(src=BIG, mode="adaptive"), goals=["exported"], tag="t04")
    tools = [t for t, _ in _steps(r)]
    ok = (r["verdict"] in V.PASSING and "cut_adaptive" in tools
          and "cut_fixed" not in tools)
    return ok, dict(verdict=r["verdict"], path=tools,
                    mode=r["state"]["values"].get("mode"))


def t05_missing_input():
    """源文件不存在：必须主动拒收，零产物，不是走到一半崩。"""
    r = _run(os.path.join(BASE, "packs", "demo"),
             dict(src=os.path.join(BASE, "fixtures", "不存在的文件.txt")),
             goals=["exported"], tag="t05")
    return (r["verdict"] == V.REJECTED and not r["artifacts"]
            and r["reason_code"] == "INPUT_NOT_FOUND",
            dict(verdict=r["verdict"], code=r["reason_code"],
                 artifacts=len(r["artifacts"]), steps=r["agent"]["steps"]))


def t06_empty_input():
    """空文件：同样是拒收，但发生在第 1 步执行后（准入工具判死）。"""
    p = os.path.join(ACCEPT, "empty.txt")
    os.makedirs(ACCEPT, exist_ok=True)
    open(p, "w", encoding="utf-8").close()
    r = _run(os.path.join(BASE, "packs", "demo"), dict(src=p),
             goals=["exported"], tag="t06")
    return (r["verdict"] == V.REJECTED
            and r["reason_code"] in ("INPUT_EMPTY", "INPUT_BLANK"),
            dict(verdict=r["verdict"], code=r["reason_code"], steps=r["agent"]["steps"]))


def t07_budget():
    """预算只给 3 步：必须停在 BUDGET，且如实说目标没达成。"""
    r = _run(os.path.join(BASE, "packs", "demo"), dict(src=SAMPLE),
             goals=["exported"], budget=dict(max_steps=3, max_wall_s=60),
             tag="t07")
    return (r["verdict"] == V.UNDECIDABLE and r["reason_code"] == "BUDGET_EXHAUSTED"
            and r["agent"]["steps"] == 3,
            dict(verdict=r["verdict"], code=r["reason_code"],
                 steps=r["agent"]["steps"]))


def t08_loop_guard():
    """脚本模式硬塞重复动作：防环必须拦住，而不是空转到预算耗尽。"""
    r = _run(os.path.join(BASE, "packs", "demo"), dict(src=SAMPLE),
             goals=["exported"], planner="script",
             sequence=["probe", "probe", "probe"], tag="t08")
    dup = r["proof"]["duplicates_blocked"]
    return (r["verdict"] == V.FAILED and dup == 1
            and r["reason_code"] == "STUCK",
            dict(verdict=r["verdict"], code=r["reason_code"],
                 duplicates_blocked=dup, steps=r["agent"]["steps"]))


BROKEN_CLEANUP = '''

def cleanup_broken(inp):
    """故意失败：用来验证内核会不会自己改道，而不是等我指路。

    选 cleanup 而不是 cut_* 来注入，是因为它的回退目标 cleanup_keepall
    与它条件相同（都不带 when）——那才是真能走通的回退。
    cut_fixed / cut_adaptive 靠 mode 区分，互斥，回退本来就走不通。
    """
    return dict(ok=False, reason_code="CLEANUP_FAILED",
                message="注入故障：清理工具坏了")
'''

# 兼容旧名字，下面 T9 用的是 cleanup 版本
BROKEN_CUT = BROKEN_CLEANUP


def t09_fallback():
    """切分工具故障 + on_fail=fallback：内核应自己换成另一个切分工具重跑。"""
    d = _variant("flaky", edits={
        "cleanup": {"impl": {"type": "python", "ref": "broken_tools:cleanup_broken",
                             "timeout_s": 30}}},
        files={"broken_tools.py": BROKEN_CLEANUP})
    r = _run(d, dict(src=SAMPLE), goals=["exported"], tag="t09")
    tools = [t for t, _ in _steps(r)]
    ok = (r["verdict"] in V.PASSING and "cleanup" in tools
          and "cleanup_keepall" in tools
          and tools.index("cleanup_keepall") > tools.index("cleanup")
          and any("已停用并改走其它路径" in w for w in r["warnings"]))
    return ok, dict(verdict=r["verdict"], path=tools, warnings=r["warnings"])


LIAR_CUT = '''
import json, os

def cut_liar(inp):
    """自称切好了，但产物里一份都没有。校验器必须拦住这种。"""
    out = inp["params"]["out"]
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    json.dump({"spans": [[0, 10]], "parts": [], "n_blocks": 1},
              open(out, "w", encoding="utf-8"))
    return dict(ok=True, facts=["parts_cut"],
                values=dict(parts_path=out, parts_n=2, parts_chars_total=10),
                evidence=dict(mode="fixed", parts=2),
                message="我切好了（其实没有）")
'''


def t10_liar_caught():
    """工具撒谎：声称完成、产物是空的。必须被独立校验拦下。"""
    d = _variant("liar", edits={
        "cut_fixed": {"impl": {"type": "python", "ref": "liar_tools:cut_liar",
                               "timeout_s": 30}}},
        files={"liar_tools.py": LIAR_CUT})
    r = _run(d, dict(src=SAMPLE), goals=["exported"], tag="t10")
    caught = (r["verdict"] == V.FAILED
              and r["reason_code"] == "VERIFY_FAILED"
              and r["proof"]["rejected_by_verify"] >= 1)
    return caught, dict(verdict=r["verdict"], code=r["reason_code"],
                        rejected_by_verify=r["proof"]["rejected_by_verify"],
                        steps=r["agent"]["steps"])


ALT_CUT = '''
import json, os

def cut_three(inp):
    """同一份契约、不同实现：固定切 3 份。
    内核不该知道这件事，也不该因为换了实现就改行为。"""
    blocks = inp["world"]["values"]["blocks_path"]
    out = inp["params"]["out"]
    d = json.load(open(blocks, encoding="utf-8"))
    spans, chars = d["spans"], d["chars"]
    n = 3
    size, rem = divmod(len(spans), n)
    parts, idx = [], 0
    for i in range(n):
        take = size + (1 if i < rem else 0)
        sel = list(range(idx, idx + take)); idx += take
        parts.append(dict(id=f"p{i+1}", span_ids=sel,
                          chars=sum(chars[j] for j in sel)))
    json.dump(dict(src_sha=d.get("src_sha"), spans=spans, parts=parts,
                   n_blocks=len(spans)), open(out, "w", encoding="utf-8"))
    return dict(ok=True, facts=["parts_cut"],
                values=dict(parts_path=out, parts_n=n,
                            parts_chars_total=sum(p["chars"] for p in parts)),
                evidence=dict(mode="fixed", parts=n))
'''


def t11_swap_impl():
    """换一个工具实现（同一契约），内核零改动，结果跟着实现变。"""
    d = _variant("alt", edits={
        "cut_fixed": {"impl": {"type": "python", "ref": "alt_tools:cut_three",
                               "timeout_s": 30}}},
        files={"alt_tools.py": ALT_CUT})
    r = _run(d, dict(src=SAMPLE), goals=["exported"], tag="t11")
    return (r["verdict"] in V.PASSING and r["state"]["values"].get("parts_n") == 3,
            dict(verdict=r["verdict"], parts=r["state"]["values"].get("parts_n"),
                 note="demo 原实现是 2 份，换实现后是 3 份"))


def _ensure_probe_fixture():
    """生成一个真实、水密、已知尺寸的网格夹具（60×40×80mm 盒），供 T12/T23 等用例复用。

    放在 fixtures/ 下：它是可复算的测试资产，不是产品。首次运行自动生成。
    """
    import trimesh
    path = os.path.join(BASE, "fixtures", "probe_fig.stl")
    if os.path.isfile(path):
        return path
    box = trimesh.creation.box(extents=(60.0, 40.0, 80.0))
    box.export(path)
    return path


def t12_capability_gap():
    """probe_import 已落地：缺文件如实拒收（FILE_UNREADABLE），
    合法网格准入通过（input_ok）；其余工具仍是桩（parts_available 仍 NOT_IMPLEMENTED）。

    三件事同时验：① 真实实现会跑、会拒坏输入（不再是"能力等实现"的 NOT_IMPLEMENTED）；
    ② 合法网格能真正拿到 input_ok；③ 上游 lod_ready / segment_semantic 依旧是桩，
    所以"分件"目标仍如实报能力未提供——证明"能力未提供"这条纪律没因为填了一个桩而失效。
    """
    _ensure_probe_fixture()
    split3d = os.path.join(BASE, "packs", "split3d")
    fixture = os.path.join(BASE, "fixtures", "probe_fig.stl")

    # A) 缺文件：必须主动拒收，且是 FILE_UNREADABLE（不是 NOT_IMPLEMENTED、不是崩）
    r_miss = _run(split3d, dict(model=os.path.join(BASE, "fixtures", "missing.stl")),
                  goals=["input_ok"], tag="t12a")
    a_ok = (r_miss["verdict"] == V.REJECTED
            and r_miss["reason_code"] == "FILE_UNREADABLE")

    # B) 合法网格：入厂质检应通过（真实现 + 校验器独立重算一致）
    r_ok = _run(split3d, dict(model=fixture), goals=["input_ok"], tag="t12b")
    b_ok = (r_ok["verdict"] in V.PASSING
            and "input_ok" in r_ok["state"]["facts"])

    # C) 仍是桩：零件分件需要 lod_ready（prep_lod 桩）→ 仍如实报能力未提供
    r_gap = _run(split3d, dict(model=fixture, model_kind="figure"),
                 goals=["parts_available"], tag="t12c")
    c_ok = (r_gap["verdict"] == V.UNDECIDABLE
            and r_gap["reason_code"] == "NOT_IMPLEMENTED")

    return (a_ok and b_ok and c_ok,
            dict(a=dict(verdict=r_miss["verdict"], code=r_miss["reason_code"]),
                 b=dict(verdict=r_ok["verdict"], facts=r_ok["state"]["facts"]),
                 c=dict(verdict=r_gap["verdict"], code=r_gap["reason_code"])))


def t13_missing_seam():
    """要割线的目标，在没给割线时应该 0 步就诊断出来，而不是跑几步再说。

    注意这里的目标必须是**只能靠割线达成**的那一支。
    契约改成两条路汇合之后，「零件已分出」有两个来源：
    语义分件不需要割线，所以拿它当目标时缺割线不算阻塞——那是正确行为，
    不是缺陷。要测「缺输入提前报」，就得挑一个没有替代路径的目标。
    """
    r = _run(os.path.join(BASE, "packs", "split3d"),
             dict(model=os.path.join(BASE, "fixtures", "x.stl")),
             goals=["seam_ok"], tag="t13")
    halt = r["agent"]["halt"]
    chain = ((halt.get("detail") or {}).get("chain")) or []
    why = " ".join(str(v.get("why") or "")
                   for v in ((halt.get("detail") or {}).get("missing") or {}).values())
    return (r["verdict"] == V.UNDECIDABLE and r["agent"]["steps"] == 0
            and halt["kind"] == "NEED_INPUT"
            and ("seam" in why or "seam_ok" in chain),
            dict(verdict=r["verdict"], steps=r["agent"]["steps"],
                 halt=halt["kind"], chain=chain, why=why[:120]))


BAD_CONTRACT = {
    "schema": "agentcore.pack/v1", "name": "bad_contract", "version": "0",
    "title": "故意写错的契约",
    "facts": [{"id": "a", "label": "甲", "initial": False},
              {"id": "b", "label": "乙"}],
    "verifiers": {},
    "goals": {"g": ["a"]},
    "tools": [
        {"name": "t1", "produces": "没登记的事实", "requires": [],
         "impl": {"type": "python", "ref": "x:y"}},
        {"name": "t2", "produces": "a", "requires": ["b"],
         "impl": {"type": "python", "ref": "x:y"}},
        {"name": "t3", "produces": "b", "requires": ["a"],
         "impl": {"type": "python", "ref": "x:y"}},
        {"name": "t4", "produces": "b", "requires": [],
         "when": "a == ", "impl": {"type": "python", "ref": "x:y"}},
    ],
}


def t14_bad_contract():
    """契约写错：doctor 必须一次把结构错、缺校验器、成环、条件语法全指出来。"""
    d = os.path.join(GEN, "bad_contract")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(BAD_CONTRACT, f, ensure_ascii=False, indent=2)
    rep = D.check(PK.load(d), deep=False)
    joined = " ".join(rep["errors"])
    want = ["未登记的事实", "校验器", "成环", "语法"]
    hit = [w for w in want if w in joined]
    return (not rep["ok"] and len(hit) >= 3,
            dict(ok=rep["ok"], errors=rep["errors"][:8], hit=hit))


def t15_same_core_two_packs():
    """核心断言：两个完全不同的领域，用的是同一个 Loop/Planner/World，
    差别只在 pack。这里直接取两边的模块来源做证明。"""
    import inspect
    from agentcore import contract, planner, world
    mods = {m.__name__: inspect.getsourcefile(m) for m in
            (L, PL, world, contract)}
    # 两个包没有各自的循环/规划器实现
    extra = [m for m in sys.modules
             if m.startswith("agentpack_") and "loop" in m]
    return (len(set(mods.values())) == len(mods) and not extra,
            dict(core_modules=mods, per_pack_agent_modules=extra))


def t16_resume():
    """断点续跑：第一次做完的事实不该重做。

    分工之后这一步很要紧——工具动辄跑十分钟，
    中途因为别的原因停下来，不该从头再来。
    """
    p = PK.load(os.path.join(BASE, "packs", "demo"))
    t = T.parse(dict(inputs=dict(src=SAMPLE), goals=["input_ok"]), p)
    base = os.path.join(ACCEPT, time.strftime("%H%M%S") + "-t16")
    r1 = L.Runner(p, t, run_dir=base + "a").run()
    r2 = L.Runner(p, t, run_dir=base + "b", resume=base + "a").run()
    ok = (r1["agent"]["steps"] == 1 and r2["agent"]["steps"] == 0
          and r2["verdict"] in V.PASSING
          and "input_ok" in r2["state"]["facts"]
          and r2.get("resume_of") == os.path.basename(base + "a"))
    return ok, dict(first_steps=r1["agent"]["steps"],
                    resume_steps=r2["agent"]["steps"],
                    verdict=r2["verdict"], resume_of=r2.get("resume_of"))


# ---------------------------------------------------------------- 多件

MULTI = dict(src=SAMPLE)
MULTI_GOALS = ["book"]
# 12 章 × 3 个环节 + 2 个前置 = 38 步。给 60 是为了留余量，
# 用例要证的是「能跑完」，不是「预算刚好」。
MULTI_BUDGET = dict(max_steps=60, max_wall_s=300)


CLEAN_LIAR = '''
"""故意撒一个小谎：盘上的内容被改坏，但报的数字还是对的。

    这不是「整件事没做」，而是「做了但不合格却说自己合格」——
    后者危险得多，因为不做会被发现，谎报只有校验器能拦。
"""


def clean_chapter(inp):
    import demo_tools
    r = demo_tools.clean_chapter(inp)
    if not r.get("ok"):
        return r
    cid = (inp.get("params") or {}).get("chapter")
    if cid == "ch05":
        fp = r["values"]["clean_path"]
        with open(fp, "rb") as f:
            txt = f.read().decode("utf-8")
        # 只动一个字，长度不变——只比字符数的校验器会放它过去
        # 等长替换：报出去的字符数完全正确，只有逐字重算才能发现
        bad = txt.replace("第", "递", 1) if "第" in txt else txt[:-1] + "X"
        with open(fp, "wb") as f:
            f.write(bad.encode("utf-8"))
    return r
'''


CLEAN_FAIL_ONE = '''
"""只让第 5 章失败，其余正常。用来证「少一件不许判成功」。"""


def clean_chapter(inp):
    cid = (inp.get("params") or {}).get("chapter")
    if cid == "ch05":
        return dict(ok=False, reason_code="CHAPTER_BROKEN",
                    message=f"第 {cid} 章内容损坏，无法清洗")
    import demo_tools
    return demo_tools.clean_chapter(inp)
'''


LIST_LIAR = '''
"""清单撒谎：报 12 章，磁盘上只留 5 章。

    这是最阴的一种错——清单是后面所有展开的依据，
    清单错了后面全错，而每一件单独看都是合格的。
"""


def list_chapters(inp):
    import demo_tools
    import os
    r = demo_tools.list_chapters(inp)
    if not r.get("ok"):
        return r
    ids = r["values"]["chapter_ids"]
    d = os.path.join(inp["run_dir"], "chapters")
    for cid in ids[5:]:
        p = os.path.join(d, cid + ".txt")
        if os.path.isfile(p):
            os.remove(p)
    return r        # 清单照报 12 章
'''


def t17_multi_runs_all():
    """12 件必须跑满 12 次，一件都不能少。

    改之前的表现是：做 1 件就判 DONE 并交付。这条用例就是为那个缺陷立的碑。
    """
    r = _run(os.path.join(BASE, "packs", "demo"), MULTI, MULTI_GOALS,
             budget=MULTI_BUDGET, tag="t17")
    dom = r["state"]["domain"]
    arts = [a for a in r["artifacts"] if a["role"].startswith("chapter@")]
    ok = (r["verdict"] in V.PASSING
          and r["agent"]["steps"] == 38
          and all(dom[k]["state"] is True for k in dom)
          and all(len(dom[k]["instances"]) == 12 for k in dom)
          and len(arts) == 12
          and r["proof"]["instance_unique"] == 36
          and r["proof"]["duplicates_blocked"] == 0)
    return ok, dict(verdict=r["verdict"], steps=r["agent"]["steps"],
                    parts=len(arts),
                    instances={k: len(dom[k]["instances"]) for k in dom},
                    unique_actions=r["proof"]["instance_unique"],
                    dup_blocked=r["proof"]["duplicates_blocked"])


def t18_liar_instance_caught():
    """某一件谎报，必须被拦住，而且要能说出是哪一件。

    只报「有一件不通过」是不够用的——12 件里找 1 件，人得自己去翻。
    """
    d = _variant("multi_liar", files={"liar_tools.py": CLEAN_LIAR},
                 edits={"clean_chapter": {
                     "impl": {"type": "python", "ref": "liar_tools:clean_chapter",
                              "timeout_s": 30}}})
    r = _run(d, MULTI, MULTI_GOALS, budget=MULTI_BUDGET, tag="t18")
    msg = " ".join(r.get("reasons") or []) + " " + " ".join(r["warnings"])
    ok = (r["verdict"] not in V.PASSING
          and r["proof"]["rejected_by_verify"] >= 1
          and "ch05" in msg)
    return ok, dict(verdict=r["verdict"], code=r["reason_code"],
                    rejected=r["proof"]["rejected_by_verify"],
                    mentions_ch05="ch05" in msg, reasons=r["reasons"][:2])


def t19_one_missing_not_success():
    """少一件就不许判成功，哪怕其余 11 件全合格。"""
    d = _variant("multi_fail_one", files={"fail_tools.py": CLEAN_FAIL_ONE},
                 edits={"clean_chapter": {
                     "impl": {"type": "python", "ref": "fail_tools:clean_chapter",
                              "timeout_s": 30}}})
    r = _run(d, MULTI, MULTI_GOALS, budget=MULTI_BUDGET, tag="t19")
    dom = r["state"]["domain"]
    gone = r["state"]["abandoned"]
    # 三件事必须同时成立，缺一条这条用例就没意义：
    #   ① 结局不是成功（少一件就是没做完）
    #   ② 其余 11 件真的做完了，不是整批停在第 5 件
    #   ③ 放弃的那件在账上，且下游三个环节都知道它没了
    ok = (r["verdict"] not in V.PASSING
          and all(len(dom[k]["instances"]) == 11 for k in dom)
          and all("ch05" in (gone.get(k) or {}) for k in dom)
          and any("ch05" in x for x in (r.get("reasons") or []) + r["warnings"]))
    return ok, dict(verdict=r["verdict"], code=r["reason_code"],
                    done={k: len(dom[k]["instances"]) for k in dom},
                    abandoned={k: sorted(gone.get(k) or {}) for k in dom},
                    reasons=r["reasons"][:2])


def t20_list_liar_caught():
    """清单撒谎（报 12 章、磁盘 5 个）必须被校验器数出来。"""
    d = _variant("list_liar", files={"list_tools.py": LIST_LIAR},
                 edits={"list_chapters": {
                     "impl": {"type": "python", "ref": "list_tools:list_chapters",
                              "timeout_s": 30}}})
    r = _run(d, MULTI, MULTI_GOALS, budget=MULTI_BUDGET, tag="t20")
    ok = (r["verdict"] not in V.PASSING
          and r["proof"]["rejected_by_verify"] >= 1)
    return ok, dict(verdict=r["verdict"], code=r["reason_code"],
                    rejected=r["proof"]["rejected_by_verify"],
                    reasons=r["reasons"][:1])


def t21_three_state():
    """三态判定的单元级验证：不知道 / 空 / 部分 / 全部，四者不能混。

    这是整个多件语义的地基。它错了，上面所有用例都会以不同形式说谎。
    """
    from agentcore import contract as C
    from agentcore import world as W
    fac = {"x": C.FactSpec("x", "样本", domain=dict(C.domain_source("pool"), key="k"))}
    w = W.WorldState(fac)
    seen = {}
    seen["未知"] = w.satisfied("x")
    w.set("pool", [])
    seen["空清单"] = w.satisfied("x")
    w.set("pool", ["a", "b"])
    seen["一个没做"] = w.satisfied("x")
    w.add("x", instance="a")
    seen["做了一半"] = w.satisfied("x")
    w.add("x", instance="b")
    seen["全做完"] = w.satisfied("x")
    # 域事实必须拒绝整体置位，否则「做了几个」这个信息又没了
    refused = False
    try:
        w.add("x")
    except W.DomainError:
        refused = True
    ok = (seen["未知"] is None and seen["空清单"] is False
          and seen["一个没做"] is False and seen["做了一半"] is False
          and seen["全做完"] is True and refused)
    return ok, dict(states={k: v for k, v in seen.items()},
                    whole_fact_refused=refused)


def t22_plan_previews_multi():
    """预览必须预报「这里会跑 N 次」，而不是假装只是一步。"""
    p = PK.load(os.path.join(BASE, "packs", "demo"))
    t = T.parse(dict(inputs=MULTI, goals=MULTI_GOALS), p)
    steps = L.preview(p, t, max_steps=40)
    acts = [x for x in steps if x["kind"] == "action"]
    tools = [x["tool"] for x in acts]
    said = " ".join(x.get("why") or "" for x in acts)
    ok = (tools[:3] == ["probe", "list_chapters", "clean_chapter"]
          and "跑 N 次" in said
          and len(acts) == 5)      # 预览按占位推一遍形状，不假装知道件数
    return ok, dict(tools=tools, preview_steps=len(acts),
                    mentions_n="跑 N 次" in said)



def t23_two_entry_points():
    """同一个目标「零件已分出」，两条入口由前提决定，不由代码决定。

    契约里 parts_available 有两个产出者：
      segment_semantic 只要 input_ok（外加输入门 inputs.model_kind，见 T27）
      cut_plane        还要 route_known（也就是要有人工割线）
    所以给不给 seam，路径自己会变。这正是「顺序是算出来的」最直接的证据。

    两个输入都带 model_kind：本用例测的是**割线路由**，与类型声明无关；
    补上声明只是让输入完整（否则 segment_semantic 因输入门不可达，
    测的就不是「给 seam 走哪条」而是「没声明走哪条」了——那是 T27 的事）。
    """
    p = PK.load(os.path.join(BASE, "packs", "split3d"))
    model = os.path.join(BASE, "fixtures", "x.stl")

    t_free = T.parse(dict(inputs=dict(model=model, model_kind="figure"),
                          goals=["parts_available"]), p)
    free = [x["tool"] for x in L.preview(p, t_free, max_steps=12)
            if x["kind"] == "action"]

    t_seam = T.parse(dict(inputs=dict(model=model, seam="s.json",
                                      model_kind="figure"),
                          goals=["parts_available"]), p)
    with_seam = [x["tool"] for x in L.preview(p, t_seam, max_steps=12)
                 if x["kind"] == "action"]

    ok = ("segment_semantic" in free and "cut_plane" not in free
          and "cut_plane" in with_seam and "segment_semantic" not in with_seam
          and free[0] == with_seam[0] == "probe_import")
    return ok, dict(no_seam=free, with_seam=with_seam)


# ---------------------------------------------- 类型声明输入门（T27）
# §二.8 的裁决：模型类型（分手办/机器人）由人声明，Agent 可提议但须经人确认，
# 不确定就拒收。落成契约机制就是 segment_semantic 的 needs_fields:
# 没有 inputs.model_kind 时语义路线不可达——不是跑起来再猜，而是根本不开门。

def t27_model_kind_gate():
    """没声明类型时语义分件不可达：0 步 NEED_INPUT，且说清缺什么。

    这条守的是「不确定就拒收」。规划器在跑任何工具之前就该诊断出来，
    而不是跑完 probe/prep 之后再在某个环节崩。
    """
    p = PK.load(os.path.join(BASE, "packs", "split3d"))
    model = os.path.join(BASE, "fixtures", "x.stl")

    t_no = T.parse(dict(inputs=dict(model=model), goals=["parts_available"]), p)
    rid = time.strftime("%H%M%S") + "-t27"
    r = L.Runner(p, t_no, planner=PL.make("rule", p, None),
                 run_dir=os.path.join(ACCEPT, rid)).run()
    halt = r["agent"]["halt"] or {}
    why = " ".join(str(v.get("why") or "")
                   for v in ((halt.get("detail") or {}).get("missing") or {}).values())
    no_kind_ok = (r["verdict"] == V.UNDECIDABLE and r["agent"]["steps"] == 0
                  and halt.get("kind") == "NEED_INPUT"
                  and "model_kind" in why)

    t_yes = T.parse(dict(inputs=dict(model=model, model_kind="figure"),
                         goals=["parts_available"]), p)
    with_kind = [x["tool"] for x in L.preview(p, t_yes, max_steps=12)
                 if x["kind"] == "action"]
    yes_ok = "segment_semantic" in with_kind and "cut_plane" not in with_kind

    ok = no_kind_ok and yes_ok
    return ok, dict(no_kind=dict(verdict=r["verdict"], steps=r["agent"]["steps"],
                                 halt=halt.get("kind"), why=why[:120]),
                    with_kind_path=with_kind)


# ---------------------------------------------- parts_available 校验器（T28）
# A-3 的落地验证。跑不到真的 segment_semantic（实现是别人的活，现在是桩），
# 所以这一条在**校验器层**验证：造工具产物 → 手工构造 payload → 调校验器，
# 用 truth/BOLT01 当真值。内核接线由 T23/T27 守着，这里专 courts 度量本身。

def _pa_payload(values, inputs, const):
    """按 loop._verify 的构造复刻一个最小 payload（只填本校验器要用的）。"""
    return dict(fact="parts_available", instance=None,
                declared=["parts_available"],
                values=values, artifacts=[], evidence={}, metrics={},
                const=const, run_dir="", workspace="",
                world=dict(values={}, facts={}),
                task=dict(task_id="t28", inputs=inputs))


def t28_parts_available_verifier():
    """四断言：① 产物=真值 → 过；② 漏分/低 IoU → 必须报出差异；
    ③ 无 ref_parts → UNKNOWN 拒收；④ 清单与磁盘不符 → 拒收。"""
    import shutil
    import tempfile
    sys.path.insert(0, os.path.join(BASE, "packs", "split3d"))
    import split3d_verify as V3

    p = PK.load(os.path.join(BASE, "packs", "split3d"))
    ref_dir = os.path.join(BASE, "truth", "BOLT01")
    man = json.load(open(os.path.join(ref_dir, "manifest.json"),
                         encoding="utf-8"))
    id2file = {pt["id"]: pt["file"] for pt in man["parts"]}
    ids = list(id2file)
    src = os.path.join(ref_dir, "parts")
    tmp = tempfile.mkdtemp(prefix="t28-")
    try:
        def stage(sub, keep):
            """真值文件（BOLT01-ANTENNA.stl 之类）复制成 <id>.stl——
            校验器约定的产物命名，与真值目录内的命名无关。"""
            d = os.path.join(tmp, sub)
            os.makedirs(d, exist_ok=True)
            for i in keep:
                shutil.copyfile(os.path.join(src, id2file[i]),
                                os.path.join(d, i + ".stl"))
            return d

        # ① 产物 = 真值（11 件全对上）→ 必须过，且 IoU 应接近 1
        d1 = stage("full", ids)
        r1 = V3.parts_available(_pa_payload(
            dict(part_ids=list(ids), parts_dir=d1),
            dict(ref_parts=ref_dir), p.const))
        ok1 = (r1["ok"] and len(r1["detail"]["pairs"]) == len(ids)
               and all(x["iou"] > 0.99 for x in r1["detail"]["pairs"])
               and not r1["detail"]["missed_ref"]
               and not r1["detail"]["extra_tool"])

        # ②a 漏分：产物少一件（清单也跟着少报一件，形态层放行，质量层必须抓）
        keep2 = [i for i in ids if i != "ARM_L"]
        d2 = stage("miss", keep2)
        r2 = V3.parts_available(_pa_payload(
            dict(part_ids=list(keep2), parts_dir=d2),
            dict(ref_parts=ref_dir), p.const))
        ok2 = (not r2["ok"] and "ARM_L" in r2["detail"]["missed_ref"]
               and "漏分" in r2["why"])

        # ②b 张冠李戴：ANTENNA 的文件内容换成 ARM_L 的
        # ——件数对得上、每件单独看都合法，只有矩阵能暴露。
        # 等价于「少真值 ANTENNA + 多一份 ARM_L」：报 missed/extra/低 IoU
        # 任一种都算抓住（度量从几何出发，报哪种都是把差异摆上桌）
        keep3 = list(ids)
        d3 = stage("swap", keep3)
        shutil.copyfile(os.path.join(src, id2file["ARM_L"]),
                        os.path.join(d3, "ANTENNA.stl"))
        r3 = V3.parts_available(_pa_payload(
            dict(part_ids=list(keep3), parts_dir=d3),
            dict(ref_parts=ref_dir), p.const))
        ok3 = (not r3["ok"]
               and ("ANTENNA" in r3["detail"]["missed_ref"]
                    or "ANTENNA" in r3["detail"]["extra_tool"]
                    or any(x["ref"] == "ANTENNA" and x["iou"] < 0.9
                           for x in r3["detail"]["pairs"])))

        # ②c 度量边界（钉住，不是缺陷）：ANTENNA 与 ARM_L 只换文件名、
        # 几何不动。IoU 按形状匹配，名字交叉它发现不了——会匹成
        # (ref=ANTENNA, tool=ARM_L) 且 IoU≈1。**这正是设计边界**：
        # 指标只判「与这套设计的几何一致度」，命名对不对归人审，
        # 所以这条断言「度量对名字交叉沉默」本身，把边界变成可测判据。
        keep4 = list(ids)
        d4 = stage("cross", keep4)
        shutil.copyfile(os.path.join(src, id2file["ARM_L"]),
                        os.path.join(d4, "ANTENNA.stl"))
        shutil.copyfile(os.path.join(src, id2file["ANTENNA"]),
                        os.path.join(d4, "ARM_L.stl"))
        r4 = V3.parts_available(_pa_payload(
            dict(part_ids=list(keep4), parts_dir=d4),
            dict(ref_parts=ref_dir), p.const))
        crossed = [x for x in r4["detail"]["pairs"]
                   if {x["ref"], x["tool"]} == {"ANTENNA", "ARM_L"}
                   and x["iou"] > 0.99]
        ok4 = bool(r4["ok"] and len(crossed) == 2
                   and not r4["detail"]["missed_ref"]
                   and not r4["detail"]["extra_tool"])

        # ③ 无 ref_parts → UNKNOWN 拒收（且病因要能和"工具错了"区分）
        r5 = V3.parts_available(_pa_payload(
            dict(part_ids=list(ids), parts_dir=d1), {}, p.const))
        ok5 = (not r5["ok"] and "UNKNOWN" in r5["why"]
               and r5["detail"].get("unverifiable") == "no_ref_parts")

        # ④ 清单与磁盘不符：清单多报一件磁盘上没有的 → 形态层拒收
        r6 = V3.parts_available(_pa_payload(
            dict(part_ids=list(ids) + ["GHOST"], parts_dir=d1),
            dict(ref_parts=ref_dir), p.const))
        ok6 = not r6["ok"] and "GHOST" in (r6["detail"].get("missing_files") or [])

        ok = ok1 and ok2 and ok3 and ok4 and ok5 and ok6
        return ok, dict(full_match=ok1, missed_caught=ok2,
                        swap_caught=ok3, naming_boundary_pinned=ok4,
                        no_ref_unknown=ok5,
                        disk_mismatch_caught=ok6,
                        full_ious=[x["iou"] for x in r1["detail"]["pairs"]][:4],
                        miss_why=r2["why"][:100], swap_why=r3["why"][:100])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



# ------------------------------------------------- values_global 通道（T24）
# 这条用例为一次静默缺陷立的碑：executor._normalize 曾经把工具返回的
# values_global 直接丢掉，于是 loop._commit 里的全局合并是死代码。
# 表现最阴——按实例工具把 clean_dir 写进实例作用域，下游一次性工具
# 用 $value.clean_dir 读不到，报的是「参数取不到」而不是「值被丢了」，
# 排查方向会被完全带偏。这里用最小场景把整条通道锁住。

VG_MANIFEST = {
    "schema": "agentcore.pack/v1", "name": "vg_pack", "version": "0",
    "title": "验证 values_global 通道",
    "facts": [
        {"id": "parts_listed", "label": "清单已产出"},
        {"id": "part_built", "label": "单件已生成",
         "domain": {"from": "part_ids", "key": "part"}},
        {"id": "summary_ok", "label": "汇总完成"},
    ],
    "verifiers": {
        "parts_listed": "vg_verify:parts_listed",
        "part_built": "vg_verify:part_built",
        "summary_ok": "vg_verify:summary_ok",
    },
    "goals": {"all": ["summary_ok"]},
    "tools": [
        {"name": "list_parts", "produces": "parts_listed", "requires": [],
         "writes": ["part_ids"],
         "impl": {"type": "python", "ref": "vg_tools:list_parts", "timeout_s": 30},
         "on_fail": "abort", "cost_s": 0.1},
        {"name": "build_part", "produces": "part_built", "requires": ["parts_listed"],
         "writes": ["build_root"],
         "impl": {"type": "python", "ref": "vg_tools:build_part", "timeout_s": 30},
         "on_fail": "abort", "cost_s": 0.1},
        {"name": "summarize", "produces": "summary_ok", "requires": ["part_built"],
         "params": {"root": "$value.build_root"},
         "impl": {"type": "python", "ref": "vg_tools:summarize", "timeout_s": 30},
         "on_fail": "abort", "cost_s": 0.1},
    ],
}

VG_TOOLS = '''
import json, os

def list_parts(inp):
    return dict(ok=True, facts=["parts_listed"],
                values={"part_ids": ["a", "b", "c"]},
                message="清单 3 件")

def build_part(inp):
    part = inp["params"]["part"]           # 内核按 domain.key 注入
    root = os.path.join(inp["run_dir"], "built")
    os.makedirs(root, exist_ok=True)
    fp = os.path.join(root, part + ".txt")
    with open(fp, "wb") as f:
        f.write(("body-" + part).encode("utf-8"))
    # 每件自己的路径走 values（实例作用域），共享目录走 values_global（全局）。
    # 下游 summarize 是一次性工具，只能看见全局视图。
    return dict(ok=True, facts=["part_built"],
                values={"part_path": fp},
                values_global={"build_root": root},
                message=part)

def summarize(inp):
    root = inp["params"]["root"]           # 来自 $value.build_root
    names = sorted(x for x in os.listdir(root) if x.endswith(".txt"))
    out = os.path.join(inp["run_dir"], "summary.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"n": len(names), "names": names}, f)
    return dict(ok=True, facts=["summary_ok"],
                values={"summary_path": out, "summary_n": len(names)},
                artifacts=[{"role": "summary", "path": out}],
                message="汇总 %d 件" % len(names))
'''

VG_VERIFY = '''
import json, os

def parts_listed(p):
    ids = (p.get("values") or {}).get("part_ids")
    ok = isinstance(ids, list) and len(ids) > 0
    return {"ok": ok, "why": "" if ok else "实例清单为空"}

def part_built(p):
    inst = p.get("instance")               # 按实例校验器必须认这个
    if not inst:
        return {"ok": False, "why": "校验器没收到 instance，无法定位是哪一件"}
    fp = os.path.join(p["run_dir"], "built", str(inst) + ".txt")
    ok = os.path.isfile(fp) and os.path.getsize(fp) > 0   # 从磁盘重算，不信工具报的路径
    return {"ok": ok, "why": "" if ok else "%s 的产物不存在或为空" % inst}

def summary_ok(p):
    fp = os.path.join(p["run_dir"], "summary.json")
    if not os.path.isfile(fp):
        return {"ok": False, "why": "汇总文件不存在"}
    with open(fp, encoding="utf-8") as f:
        d = json.load(f)
    ok = d.get("n") == 3
    return {"ok": ok, "why": "" if ok else "汇总件数 %s != 3" % d.get("n")}
'''


def _vg_pack():
    d = os.path.join(GEN, "vg_pack")
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(VG_MANIFEST, f, ensure_ascii=False, indent=2)
    with open(os.path.join(d, "vg_tools.py"), "w", encoding="utf-8") as f:
        f.write(VG_TOOLS)
    with open(os.path.join(d, "vg_verify.py"), "w", encoding="utf-8") as f:
        f.write(VG_VERIFY)
    return d


def t24_values_global_channel():
    """按实例工具写 values_global，下游一次性工具必须能从 $value 读到。

    同时证作用域隔离：实例私有值 part_path 不能污染全局视图，
    否则又回到「第 3 件盖掉前 2 件」那种静默丢数据。
    """
    d = _vg_pack()
    r = _run(d, dict(tag="vg"), goals=["all"],
             budget=dict(max_steps=20, max_wall_s=60), tag="t24")
    vals = r["state"]["values"]
    scoped = r["state"]["scoped"]
    ok = (r["verdict"] in V.PASSING
          and bool(vals.get("build_root"))            # 全局通道确实生效
          and "part_path" not in vals                 # 实例值没污染全局
          and any("part_path" in (v or {}) for v in scoped.values())
          and vals.get("summary_n") == 3
          and r["proof"]["instance_actions"] == 3)
    return ok, dict(verdict=r["verdict"], code=r["reason_code"],
                    steps=r["agent"]["steps"],
                    global_has_build_root=bool(vals.get("build_root")),
                    global_leaked_part_path="part_path" in vals,
                    scoped_keys=sorted(scoped),
                    summary_n=vals.get("summary_n"),
                    instance_actions=r["proof"]["instance_actions"])


# ---------------------------------------------- 隐式依赖（T25）
# 技能文档曾把「调用前自己确认 lod_ready 已成立」写成技能侧纪律。
# 但「上游事实是否在依赖链上」能被静态判定，属于内核；
# 靠提示词自觉等于把一件可判定的事交给不可靠的执行者。
# doctor 必须自己判死，契约必须自己声明。

def t25_implicit_value_dependency():
    """$value 引用的键必须有 writes 声明，且写入者产出的事实要在依赖链上。"""
    import copy
    from agentcore import contract as C

    p = PK.load(os.path.join(BASE, "packs", "split3d"))
    e0, _ = C.validate(p)

    # 1) 漏 requires：lod_path 由 prep_lod 写，但 segment_semantic 不声明依赖它
    p1 = copy.deepcopy(p)
    for t in p1.tools:
        if t.name == "segment_semantic":
            t.requires = ("input_ok",)
    e1, _ = C.validate(p1)

    # 2) 漏 writes：谁都没声明会写 model_path
    p2 = copy.deepcopy(p)
    for t in p2.tools:
        if t.name == "probe_import":
            t.writes = ()
    e2, _ = C.validate(p2)

    hit1 = any("隐式依赖" in x and "segment_semantic" in x for x in e1)
    hit2 = any("writes 声明会写入" in x and "prep_lod" in x for x in e2)
    ok = (not e0 and hit1 and hit2)
    return ok, dict(current_errors=e0,
                    missing_requires=e1, missing_writes=e2,
                    caught_implicit=hit1, caught_orphan=hit2)


# ---------------------------------------------- 文档契约快照（T26）
# 技能侧用第 5 类检查对账「refs 工具表 ↔ manifest」，抓的是「id 都在、内容漂移」。
# 内核侧同一个洞：接口书 PACK-SPEC 里的契约示例没人对账。21:35 改了
# segment_semantic.requires，3.11 的示例却还写着旧值，而没有任何一步会报错。
# 这里给带 CONTRACT-SNAPSHOT 标记的代码块做逐字段对账，并用负向实验证明它真会 FAIL。
_SNAPSHOT_RE = re.compile(
    r"<!--\s*CONTRACT-SNAPSHOT:\s*(\S+)\s*-->\s*"
    r"```json\s*(.*?)\s*```\s*"
    r"<!--\s*CONTRACT-SNAPSHOT-END\s*-->", re.S)


def _snapshot_errors(doc_text, manifest):
    """返回 (错误列表, 快照里的工具名集合)。空列表 = 文档与契约一致。"""
    errs = []
    m = _SNAPSHOT_RE.search(doc_text)
    if not m:
        return ["找不到带 CONTRACT-SNAPSHOT 标记的契约快照——"
                "检查器不能因为标记消失就静默 PASS"], set()
    pack_name, blob = m.group(1), m.group(2)
    if pack_name != "split3d":
        errs.append("快照标记指向 %r，验收只对 split3d 对账" % pack_name)
    try:
        rows = json.loads(blob)
    except Exception as e:
        return ["快照里的 json 解析不了：%s" % e], set()
    if not isinstance(rows, list) or not rows:
        return ["快照必须是非空 JSON 数组"], set()

    tools = {t["name"]: t for t in manifest.get("tools", [])}
    seen = set()
    for r in rows:
        name = r.get("name")
        if not name:
            errs.append("快照里有一行没有 name")
            continue
        seen.add(name)
        t = tools.get(name)
        if t is None:
            errs.append("快照里的工具 %s 不在 manifest 里" % name)
            continue
        for field in ("produces", "priority"):
            if r.get(field) != t.get(field):
                errs.append("%s 的 %s：文档 %r，契约 %r"
                            % (name, field, r.get(field), t.get(field)))
        want = sorted(t.get("requires") or [])
        got = sorted(r.get("requires") or [])
        if got != want:
            errs.append("%s 的 requires：文档 %s，契约 %s"
                        % (name, got or "空", want or "空"))
    return errs, seen


def t26_doc_contract_snapshot():
    """接口书里的契约示例必须与 manifest 一致；标记消失或内容漂移都要 FAIL。"""
    spec_path = os.path.join(BASE, "PACK-SPEC.md")
    man_path = os.path.join(BASE, "packs", "split3d", "manifest.json")
    with open(spec_path, "rb") as f:
        doc = f.read().decode("utf-8")
    with open(man_path, "rb") as f:
        manifest = json.loads(f.read().decode("utf-8"))

    e0, seen = _snapshot_errors(doc, manifest)
    # 3.2 那个「// 错：」是故意写错的反例，不该被当成契约吞进来
    counterexample_untouched = ("// 错：" in doc and
                                seen == {"cut_plane", "segment_semantic"})

    # 负向 A：把快照里的 requires 改回旧值（正是 21:35 真实发生过的漂移）
    # 坑：同样的串在 3.2 节「对：」示例里也有一份、位置还在快照之前，
    # 直接 replace(..., 1) 改的是示例、快照原封不动，检查就空转了。
    # 必须先切出快照段，只在段内替换。
    m = _SNAPSHOT_RE.search(doc)
    lo, hi = m.span()
    bad_a = (doc[:lo]
             + doc[lo:hi].replace('"requires": ["input_ok", "lod_ready"],',
                                  '"requires": ["input_ok"],', 1)
             + doc[hi:])
    ea, _ = _snapshot_errors(bad_a, manifest)
    drift_caught = any("segment_semantic" in x and "requires" in x for x in ea)

    # 负向 B：把标记删掉——检查器必须喊，不能静默 PASS
    bad_b = doc.replace("<!-- CONTRACT-SNAPSHOT: split3d -->", "<!-- 忘了标记 -->", 1)
    eb, _ = _snapshot_errors(bad_b, manifest)
    marker_caught = bool(eb)

    ok = (not e0 and counterexample_untouched and drift_caught and marker_caught)
    return ok, dict(current_errors=e0, tools_in_snapshot=sorted(seen),
                    counterexample_untouched=counterexample_untouched,
                    drift_caught=drift_caught, marker_caught=marker_caught,
                    negative_a=ea, negative_b=eb)


CLEAN_FAIL_SEVEN = '''
"""只让第 7 章失败，其余正常。与 ch05 版配成对：
同一个 CHAPTER_BROKEN、不同实例 = 同码不同病因，两条记忆必须互不误命中。"""


def clean_chapter(inp):
    cid = (inp.get("params") or {}).get("chapter")
    if cid == "ch07":
        return dict(ok=False, reason_code="CHAPTER_BROKEN",
                    message=f"第 {cid} 章内容损坏，无法清洗")
    import demo_tools
    return demo_tools.clean_chapter(inp)
'''


# ---------------------------------------------- 错误记忆最小版（T29/T30）
# B 线三条例行公事：同错第二次必须认出（T29）、同码不同病因不得误命中（T30）、
# 修好跑通即作废、作废后再犯重新计数（T29 后半）。
# 索引写在临时文件里，不碰仓库的 runs/ 与 error_index.jsonl。

def _em_variant(name, module, fn="clean_chapter"):
    return _variant(name, files={module + ".py": (
        CLEAN_FAIL_ONE if "ch05" in name else CLEAN_FAIL_SEVEN)},
        edits={fn: {"impl": {"type": "python",
                             "ref": f"{module}:{fn}", "timeout_s": 30}}})


def t29_errmem_second_hit():
    """同一错误：第一次只记录、第二次警示、第三次拒收、修好作废、再犯重开。"""
    import tempfile
    from agentcore import __version__
    from agentcore import errmem as EM

    # 必须与 run.json 的 kernel_version 同值——它就是纯版本号（如 "0.1.0"），
    # 没有 "agentcore " 前缀（那个格式是 accept.json 自己的 kernel 字段）。
    # 标签对不上，索引全都匹配不上，断言会假 PASS——这个坑踩过一次。
    kernel_tag = __version__
    idx = os.path.join(tempfile.mkdtemp(prefix="t29-"), "idx.jsonl")
    d = _em_variant("b_ch05", "fail_tools")
    pack = PK.load(d)
    task = T.parse(dict(inputs=dict(MULTI), goals=MULTI_GOALS), pack)

    r1 = _run(d, MULTI, MULTI_GOALS, budget=MULTI_BUDGET, tag="t29a")
    EM.record_run(r1, idx)
    e1 = EM.load(idx)
    open1 = [e for e in e1.values() if e.get("status") == "open"]
    _, act1 = EM.precheck(e1, task, pack, kernel_tag)
    # 第一次：有条目、计数都是 1、预检安静（不许拿"匹配不上"冒充安静）
    first_quiet = (bool(open1) and act1 is None
                   and all(int(e.get("count") or 0) == 1 for e in open1))

    r2 = _run(d, MULTI, MULTI_GOALS, budget=MULTI_BUDGET, tag="t29b")
    EM.record_run(r2, idx)
    e2 = EM.load(idx)
    hits2, act2 = EM.precheck(e2, task, pack, kernel_tag)
    second_warn = act2 == "warn" and bool(hits2) and bool(EM.warning_text(hits2))

    r3 = _run(d, MULTI, MULTI_GOALS, budget=MULTI_BUDGET, tag="t29c")
    EM.record_run(r3, idx)
    e3 = EM.load(idx)
    hits3, act3 = EM.precheck(e3, task, pack, kernel_tag)
    third_reject = act3 == "reject" and len(hits3) >= 1

    # 修好 = 同名变体但不注入故障（demo 原实现）。必须同名：
    # resolved 按 pack@version 匹配，换个包名就作废不了这条记忆。
    d_fixed = _variant("b_ch05")
    r4 = _run(d_fixed, MULTI, MULTI_GOALS, budget=MULTI_BUDGET, tag="t29d")
    EM.record_run(r4, idx)
    e4 = EM.load(idx)
    healed = (r4["verdict"] in V.PASSING
              and any(e.get("status") == "resolved" for e in e4.values()))
    _, act4 = EM.precheck(e4, task, pack, kernel_tag)
    quiet_after_heal = act4 is None

    # 作废后再犯 → 重新打开、重新计数（记忆不是一次性的）。
    # 注意：d_fixed 把同名目录重建成无故障版了，这里要重新注入故障。
    d_again = _em_variant("b_ch05", "fail_tools")
    r5 = _run(d_again, MULTI, MULTI_GOALS, budget=MULTI_BUDGET, tag="t29e")
    EM.record_run(r5, idx)
    e5 = EM.load(idx)
    reopened = any(e.get("status") == "open" for e in e5.values())

    ok = (first_quiet and second_warn and third_reject and healed
          and quiet_after_heal and reopened)
    return ok, dict(first_quiet=first_quiet, second_warn=second_warn,
                    third_reject=third_reject, healed=healed,
                    quiet_after_heal=quiet_after_heal, reopened=reopened,
                    warn_text=EM.warning_text(hits2)[:120] if hits2 else "")


def t30_same_code_different_cause():
    """同码不同病因不得误命中：ch05 的 CHAPTER_BROKEN 与 ch07 的是两条记忆。"""
    import tempfile
    from agentcore import __version__
    from agentcore import errmem as EM

    kernel_tag = __version__      # 同 run.json 的 kernel_version：纯版本号
    idx = os.path.join(tempfile.mkdtemp(prefix="t30-"), "idx.jsonl")
    d5 = _em_variant("b_ch05x", "fail_tools")
    d7 = _em_variant("b_ch07x", "fail7_tools")
    r5 = _run(d5, MULTI, MULTI_GOALS, budget=MULTI_BUDGET, tag="t30a")
    r7 = _run(d7, MULTI, MULTI_GOALS, budget=MULTI_BUDGET, tag="t30b")
    EM.record_run(r5, idx)
    EM.record_run(r7, idx)
    entries = EM.load(idx)

    fp5 = {e["fp"] for e in entries.values() if e.get("instance") == "ch05"}
    fp7 = {e["fp"] for e in entries.values() if e.get("instance") == "ch07"}
    distinct = bool(fp5 and fp7 and not (fp5 & fp7))

    # 拿 ch05 的包+任务预检：命中的必须只有 ch05 的条目
    pack5 = PK.load(d5)
    task5 = T.parse(dict(inputs=dict(MULTI), goals=MULTI_GOALS), pack5)
    hits5, _ = EM.precheck(entries, task5, pack5, kernel_tag)
    only_ch05 = (len(hits5) >= 1
                 and all(e.get("instance") in (None, "ch05") for e in hits5))

    # 同一个工具、同一个错误码，只因实例不同就必须认作两件事
    ok = distinct and only_ch05
    return ok, dict(distinct_fp=distinct, only_ch05_hit=only_ch05,
                    n_ch05_entries=len(fp5), n_ch07_entries=len(fp7),
                    hits5=[e.get("instance") for e in hits5])


CASES = [
    ("T1", "只做质检，1 步收工", t01_only_inspect),
    ("T2", "默认交付路径是推出来的 8 步", t02_deliver),
    ("T3", "加目标就多一步且插对位置", t03_full_includes_assemble),
    ("T4", "条件分支：换模式走另一个工具", t04_condition_branch),
    ("T5", "源文件不存在 → 主动拒收", t05_missing_input),
    ("T6", "空文件 → 准入拦下", t06_empty_input),
    ("T7", "预算耗尽 → 如实报 BUDGET", t07_budget),
    ("T8", "重复动作 → 防环拦截", t08_loop_guard),
    ("T9", "工具故障 → 自己改道，且理由写进报告", t09_fallback),
    ("T10", "工具撒谎 → 校验器拦下", t10_liar_caught),
    ("T11", "换实现不改内核", t11_swap_impl),
    ("T12", "probe_import 已落地：缺文件拒收 + 合法准入；其余仍是桩", t12_capability_gap),
    ("T13", "缺输入 → 0 步诊断出依赖链", t13_missing_seam),
    ("T14", "坏契约 → doctor 一次说清", t14_bad_contract),
    ("T15", "两个领域共用同一内核", t15_same_core_two_packs),
    ("T16", "断点续跑不重做已完成的事", t16_resume),
    ("T17", "12 件跑满 12 次，一件不少", t17_multi_runs_all),
    ("T18", "某件谎报 → 拦住并说清是哪件", t18_liar_instance_caught),
    ("T19", "少一件 → 不许判成功", t19_one_missing_not_success),
    ("T20", "清单撒谎 → 数磁盘拦住", t20_list_liar_caught),
    ("T21", "三态：未知/空/部分/全部不混", t21_three_state),
    ("T22", "预览预报会跑 N 次", t22_plan_previews_multi),
    ("T23", "给不给割线 → 两条入口自动切换", t23_two_entry_points),
    ("T24", "按实例工具写全局值 → 下游读得到，且实例值不串台", t24_values_global_channel),
    ("T25", "隐式依赖 → doctor 静态判死，不靠技能自觉", t25_implicit_value_dependency),
    ("T26", "接口书契约快照 ↔ manifest，漂移/标记消失都会 FAIL", t26_doc_contract_snapshot),
    ("T27", "没声明类型 → 语义分件不可达，0 步 NEED_INPUT", t27_model_kind_gate),
    ("T28", "parts_available 校验器：漏分/错分/无真值/磁盘不符全抓住", t28_parts_available_verifier),
    ("T29", "错误记忆：同错二次认出、三次拒收、修好作废、再犯重开", t29_errmem_second_hit),
    ("T30", "同码不同病因不得误命中（ch05 ≠ ch07）", t30_same_code_different_cause),
]


def main(only=None):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")
    except Exception:  # noqa: BLE001
        pass
    os.makedirs(GEN, exist_ok=True)
    os.makedirs(ACCEPT, exist_ok=True)
    only = set(only or [])
    results, t0 = [], time.time()
    for cid, desc, fn in CASES:
        if only and cid not in only:
            continue
        st = time.time()
        try:
            ok, got = fn()
            err = ""
        except Exception as e:  # noqa: BLE001
            import traceback
            ok, got, err = False, {}, f"{type(e).__name__}: {e}"
            tb = traceback.format_exc()
        rec = dict(id=cid, desc=desc, ok=bool(ok), got=got, err=err,
                   elapsed_s=round(time.time() - st, 2))
        if err:
            rec["traceback"] = tb[-2000:]
        results.append(rec)
        mark = "通过" if ok else "**不通过**"
        print(f"{cid:4s} {mark:8s} {desc}")
        if not ok:
            print(f"     实测：{json.dumps(got, ensure_ascii=False, default=str)[:300]}")
            if err:
                print(f"     异常：{err}")
    passed = sum(1 for r in results if r["ok"])
    total = round(time.time() - t0, 1)
    print()
    print(f"结果 {passed}/{len(results)}，{total}s")
    out = dict(at=time.strftime("%Y-%m-%dT%H:%M:%S"), passed=passed,
               n=len(results), elapsed_s=total,
               kernel="agentcore " + __import__("agentcore").__version__,
               results=results)
    with open(os.path.join(BASE, "accept.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    raise SystemExit(main(args))
