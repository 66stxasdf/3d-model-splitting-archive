# -*- coding: utf-8 -*-
"""权威源一致性检查（只读）。

用途：回答任何关于「分件 Agent 现状 / 契约 / 流程」的问题之前跑一遍，
      把「同一事实的多个副本有没有悄悄过期」变成可复算的动作。

设计约束：
  - 只读：不写文件、不跑 accept.py、不重建 HTML、不碰资产库。
  - 精确锚点：只在指定文件的指定句式里找数字，避免把 23/23、9/9 等别的含义误判。
  - 找不到锚点也报错（锚点丢失本身就是漂移），但明确标为「可选」的副本才 SKIP。
  - 动态生成优于硬编码：状态类事实必须来自 doctor / accept.py 的活结果。
  - `--selftest` 用内存样本做负向实验，证明检查器不是永远 PASS，全程不碰磁盘。

用法： python check_sources.py               # 退出码 0=全过，1=有漂移
       python check_sources.py --selftest   # 负向自检
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
K18 = ROOT / "18-分件Agent内核"
K19 = ROOT / "19-分件技能"
K20 = ROOT / "20-可视化Agent工作台"
DESKTOP = Path(r"D:\Users\Desktop\zhuomian")

# 工作区记忆不绑死某个会话目录：环境变量优先，其次依次找候选
if "WB_MEMORY_MD" in os.environ:
    MEMORY_CANDIDATES = [Path(os.environ["WB_MEMORY_MD"])]
else:
    MEMORY_CANDIDATES = [
        ROOT / ".workbuddy" / "memory" / "MEMORY.md",
        Path(r"D:\workbd\2026-11-10-11-18-15\.workbuddy\memory\MEMORY.md"),
    ]


def memory_md():
    for p in MEMORY_CANDIDATES:
        if p.exists():
            return p
    return MEMORY_CANDIDATES[-1]


_results = []


def _read(p):
    return p.read_bytes().decode("utf-8")


def _rel(p):
    try:
        return str(Path(p).relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def rec(rid, ok, msg, skipped=False):
    tag = "SKIP" if skipped else ("PASS" if ok else "FAIL")
    _results.append((rid, tag, msg))
    print("  [%s] %-3s %s" % (rid, tag, msg))


def anchored(rid, path, pattern, check, label, optional=False):
    """在指定文件里按精确锚点取值，交给 check(value) 判定。"""
    p = Path(path)
    if not p.exists():
        rec(rid, False, "%s 不存在：%s" % (label, _rel(p)), skipped=optional)
        return
    m = re.search(pattern, _read(p), re.M)
    if not m:
        rec(rid, False, "%s 找不到锚点 %s（副本可能改了句式）" % (label, pattern),
            skipped=optional)
        return
    val = m.groups() if m.groups() else m.group(0)
    ok, detail = check(val)
    rec(rid, ok, "%s：%s" % (label, detail))


def eq_ints(expected):
    def _check(groups):
        got = tuple(int(x) for x in groups)
        want = tuple(expected)
        return got == want, "期望 %s，实际 %s" % ("/".join(map(str, want)),
                                                "/".join(map(str, got)))
    return _check


_ALLSTUB_RE = re.compile(r'"all_stub"\s*:\s*([^,\n]+)')
_STATIC_STUB_RE = re.compile(r'>\s*\d+ 个工具[^<]{0,24}(?:仍是桩|都是桩)')


def _all_stub_expr(txt):
    m = _ALLSTUB_RE.search(txt)
    return m.group(1).strip() if m else None


def _all_stub_ok(txt):
    e = _all_stub_expr(txt)
    return e is not None and e not in ("True", "False")


# ---------------------------------------------------------------- R1 验收用例数
def r1():
    src = _read(K18 / "accept.py")
    ids = re.findall(r'^\s*\("(T\d+[a-z]?)"\s*,', src, re.M)
    n = len(ids)
    if not ids:
        rec("R1", False, "accept.py 里数不出 CASES，权威源解析失败")
        return n
    print("\n[R1] 验收用例数：权威 accept.py CASES = %d（%s … %s）" %
          (n, ids[0], ids[-1]))
    eq = eq_ints((n, n))
    anchored("R1", K18 / "DESIGN.md", r"验收现状：[^\n]*?\*\*(\d+)/(\d+)\*\*", eq, "DESIGN.md 末尾")
    anchored("R1", K18 / "README.md", r"## 验收：(\d+)/(\d+)", eq, "README.md §验收")
    anchored("R1", K18 / "ROADMAP.md", r"18 仓 (\d+)/(\d+)", eq, "ROADMAP.md §一")

    # 20 仓两处：硬编码必须等于权威值；改成动态则更优
    bp = K20 / "build.py"
    btxt = _read(bp) if bp.exists() else ""
    m = re.search(r'"accept"\s*:\s*"(\d+)/(\d+)"', btxt)
    if m:
        ok, detail = eq((m.group(1), m.group(2)))
        rec("R1", ok, "build.py meta：%s（硬编码，建议改动态）" % detail)
    else:
        rec("R1", "accept.py" in btxt and "CASES" in btxt,
            "build.py meta：%s" % ("已现场读 accept.py 的 CASES" if
                                  ("accept.py" in btxt and "CASES" in btxt)
                                  else "既无硬编码也看不出动态来源，字段可能被删"))

    hp = K20 / "Agent工作台.html"
    if hp.exists():
        anchored("R1", hp, r'"accept"\s*:\s*"(\d+)/(\d+)"', eq, "工作台 HTML meta")
    else:
        rec("R1", False, "工作台 HTML 不存在", skipped=True)
    return n


# ------------------------------------------------- R2 split3d 契约数量
def r2():
    m = json.loads(_read(K18 / "packs" / "split3d" / "manifest.json"))
    nf, nt = len(m["facts"]), len(m["tools"])
    nv, ng = len(m["verifiers"]), len(m["goals"])
    print("\n[R2] split3d 契约：facts=%d tools=%d verifiers=%d goals=%d" %
          (nf, nt, nv, ng))
    anchored("R2", K19 / "README.md",
             r"pack-contract\.md\s+技能 ↔ 内核：(\d+) 事实 / (\d+) 工具",
             eq_ints((nf, nt)), "19/README.md 目录树")
    for pat, want, label in (
        (r"## 一、(\d+) 个事实", nf, "pack-contract.md §一"),
        (r"## 二、(\d+) 个工具", nt, "pack-contract.md §二"),
        (r"「(\d+) 个工具是桩」", nt, "pack-contract.md 桩说明"),
    ):
        def chk(g, want=want):
            got = int(g[0])
            return got == want, "期望 %d，实际 %d" % (want, got)
        anchored("R2", K19 / "refs" / "pack-contract.md", pat, chk, label)


# ------------------------------------------------------- R4 流程阶段
def r4():
    rd = _read(K18 / "ROADMAP.md")
    stages = sorted({int(x) for x in re.findall(r"^### 阶段 (\d+)", rd, re.M)})
    # 阶段 0 已关闭，正文以「**阶段 0 已关闭**」陈述，不再是 ### 标题
    closed0 = "阶段 0 已关闭" in rd
    ok = stages == [1, 2, 3, 4] and closed0
    rec("R4", ok, "ROADMAP §三 待执行阶段标题 %s，阶段 0 已关闭=%s" % (stages, closed0))

    # 派生副本不得把已撤销的「阶段 1.5」当待执行阶段
    for p, label in ((memory_md(), "工作区 MEMORY.md"),
                     (K18 / "README.md", "18/README.md"),
                     (K19 / "refs" / "process-profiles.md", "19/process-profiles.md")):
        if not p.exists():
            rec("R4", False, "%s 不存在，跳过" % label, skipped=True)
            continue
        t = _read(p)
        bad = "阶段 1.5" in t or "1.5 错误记忆" in t
        rec("R4", not bad,
            "%s：%s" % (label, "出现已撤销的『阶段 1.5』" if bad else "无已撤销阶段口径"))

    rec("R4", "不再设独立" in rd or "不是一次性阶段" in rd,
        "ROADMAP 明确写了错误记忆是贯穿机制而非独立阶段")
    rec("R4", "落点裁决" in rd and "不改内核" in rd,
        "ROADMAP 阶段 1 B 线写清了命中注入的落点")


# ------------------------------------------- R5 口径锚（只查存在，不判对错）
def r5():
    for p, pat, label in (
        (K18 / "ROADMAP.md", r"### 3\.1|§3\.1", "ROADMAP 学习/错误记忆口径"),
        (K18 / "PACK-SPEC.md", r"§12\.1|12\.1", "PACK-SPEC 通道边界"),
        (K19 / "skills" / "part-semantics" / "SKILL.md", r"MISSING_PARAM", "技能保留运行时通道语义"),
    ):
        ok = p.exists() and re.search(pat, _read(p)) is not None
        rec("R5", ok, "%s：%s" % (label, "锚存在" if ok else "锚缺失"))
    print("       （判定标准本身归技能 / 人，脚本不判）")


# ----------------------------------------------- R6 入口
def r6():
    ssot = ROOT / "00-权威源登记表.md"
    rec("R6", ssot.exists(), "00-权威源登记表.md %s" % ("存在" if ssot.exists() else "缺失"))
    idx = ROOT / "00-索引.md"
    if idx.exists():
        t = _read(idx)
        rec("R6", "00-权威源登记表.md" in t and "check_sources.py" in t,
            "00-索引.md 顶部有指向本表与检查器的入口行")
        rec("R6", "当前主线" in t and "不参与当前判定" in t,
            "00-索引.md 声明了归档区边界")
        rec("R6", "不是今天的现状" in t,
            "00-索引.md 声明了正文「当前 / 最新」的时态")


# ----------------------------------------------- R7 桌面三件套
def r7():
    for name in ("Agent工作台.html", "bridge.py", "启动真内核.bat"):
        a, b = K20 / name, DESKTOP / name
        if not a.exists() or not b.exists():
            rec("R7", False, "%s：主副本或桌面副本缺失，跳过" % name, skipped=True)
            continue
        ha = hashlib.sha256(a.read_bytes()).hexdigest()
        hb = hashlib.sha256(b.read_bytes()).hexdigest()
        rec("R7", ha == hb, "%s：主 %s / 桌面 %s" % (name, ha[:10], hb[:10]))


# ----------------------------------------------- R8 状态类文案
def r8():
    bp = K20 / "build.py"
    bt = _read(bp) if bp.exists() else ""
    ok = _all_stub_ok(bt) and "doctor" in bt.lower()
    rec("R8", ok, "build.py all_stub：%s" %
        ("现场问 doctor" if ok else "仍是字面量或来源不明（%s）" % _all_stub_expr(bt)))
    tpl = K20 / "工作台.template.html"
    tt = _read(tpl) if tpl.exists() else ""
    rec("R8", "META.all_stub" in tt, "模板 JS 按 all_stub 分支生成状态文案")
    bad = _STATIC_STUB_RE.findall(tt)
    rec("R8", not bad, "模板静态文案：%s" %
        ("无写死的桩状态" if not bad else "仍写死 " + bad[0].strip()))


# ----------------------------------------------- 负向自检
def selftest():
    print("负向自检（内存样本，不读磁盘、不写文件）")
    checks = [
        ("all_stub 字面量 True 应被判 FAIL", not _all_stub_ok('"all_stub": True,')),
        ("all_stub 动态表达式应被判 PASS",
         _all_stub_ok('"all_stub": _stub == len(_rep["tools"]),')),
        ("验收数写错应被判 FAIL", eq_ints((26, 26))(("24", "24"))[0] is False),
        ("验收数正确应被判 PASS", eq_ints((26, 26))(("26", "26"))[0] is True),
        ("静态桩文案应被抓到", bool(_STATIC_STUB_RE.findall('>13 个工具实现仍是桩</span>'))),
        ("动态 JS 文案不应误报", not bool(_STATIC_STUB_RE.findall('META.n_tools + " 个工具实现仍是桩"'))),
    ]
    ok = all(v for _, v in checks)
    for label, v in checks:
        print("  [%s] %s" % ("PASS" if v else "FAIL", label))
    print("\n负向自检：%s" % ("全部通过，检查器不是永远 PASS" if ok else "存在失效规则"))
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        return selftest()
    print("权威源一致性检查（只读）· 根：%s" % ROOT)
    r1()
    r2()
    r4()
    r5()
    r6()
    r7()
    r8()
    n_fail = sum(1 for _, t, _ in _results if t == "FAIL")
    n_pass = sum(1 for _, t, _ in _results if t == "PASS")
    n_skip = sum(1 for _, t, _ in _results if t == "SKIP")
    print("\n汇总：%d PASS / %d FAIL / %d SKIP" % (n_pass, n_fail, n_skip))
    if n_fail:
        print("有漂移：按 00-权威源登记表.md §三 逐项同步派生副本，或从权威源动态读取。")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
