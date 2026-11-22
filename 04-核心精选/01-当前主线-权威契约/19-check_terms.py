# -*- coding: utf-8 -*-
"""术语自检：技能 ↔ 内核契约。

为什么需要它
------------
技能里写的是自然语言，内核认的是 id。两边一旦漂移，**不会报错，只会静默失效**：
技能说「走 cut_adaptive」，内核里根本没有这个工具，agent 会自己编一个流程出来。

五类检查
--------
1. 存在性   frontmatter 的 covers / facts 里每个 id 必须在 manifest 里真实存在
2. 完备性   全部工具、全部事实必须各自至少被一个技能覆盖（防遗漏）
3. 唯一性   一个工具只能归属一个技能（防两处抢同一件事，各自写法不同）
4. 幻觉 id  正文里出现形如 cut_xxx / verify_xxx 但 manifest 里没有的 id，报出来
5. 契约快照 refs/pack-contract.md 的工具表逐行对账 produces / requires / priority

6. 关键字段 refs/pack-contract.md 的字段快照表，对账 const / needs_fields / verifiers 全量
7. 未登记标识符 正文里所有 snake_case 反引号词，把没在契约里登记的列出来（INFO，供人扫一眼）

第 5 类是补的：前四类只看「id 存不存在」，看不出「requires 内容不一致」。
21:35 内核给 segment_semantic 补 lod_ready、文档还写 input_ok 时，脚本照样 PASS。

第 6 类是 11-12 补的：契约 11-11 新增了 `inputs.model_kind` 输入门、
`ref_parts` 评估器与两个语义阈值。**它们不是 id**，前五类检查全部看不见——
技能仓一个字没提，脚本却照样 PASS。第 7 类补的是第 4 类的反向盲区：
前缀法只能抓「长得像内核 id」的幻觉（如 `cut_adaptive`），
`merge_parts` 这种陌生前缀抓不到，改成全量列出让人过目。

外加结构检查：每份 SKILL.md 必须有「立即停手」一节，且 name 与目录名一致。

只读，快（<0.5s）。用法：
    python check_terms.py
    python check_terms.py --manifest <path> --skills <dir>
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 不是内核 id、但合法出现在正文里的标识符。
# semantic_vlm 是 provenance 的枚举值（定义在 refs/provenance.md），不属于 manifest。
NON_KERNEL_IDS = {"semantic_vlm"}

# 已判错的旧字段名：**原文保留不删**，但必须在同一 SKILL.md 里带作废标记。
# 标记消失也要 FAIL —— 和内核 T26「快照标记消失必须 FAIL」同一条规矩。
# 值 = 该词附近必须出现的标记文字。
DEPRECATED_TOKENS = {
    "scale_resolved": "口径误用",
}

# 内核 id 的构词前缀。用来从正文里捞「看起来像内核 id」的词，抓幻觉。
ID_PREFIXES = (
    "probe_", "prep_", "seam_", "route_", "cut_", "cleanup_", "verify_",
    "find_", "build_", "assemble_", "export_", "segment_",
    "input_", "lod_", "parts_", "contacts_", "joints_", "assembled_",
    "exported", "semantic_",
)


def read_text(path):
    """一律按字节读再 decode。Windows 下文本模式的 \\r\\n 转换会让口径对不上。"""
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


def parse_frontmatter(text):
    """不引入 yaml 依赖。只支持 key: value 与 key: [a, b] 两种写法。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None, text
    fm = {}
    for ln in lines[1:end]:
        if ":" not in ln or ln.strip().startswith("#"):
            continue
        k, v = ln.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            fm[k] = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
        else:
            fm[k] = v.strip("\"'")
    return fm, "\n".join(lines[end + 1:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--skills", default=os.path.join(HERE, "skills"))
    args = ap.parse_args()

    manifest_path = args.manifest or os.path.join(
        os.path.dirname(HERE), "18-分件Agent内核", "packs", "split3d", "manifest.json")

    errors, warns = [], []

    # ---------- 前置：契约必须可读 ----------
    if not os.path.isfile(manifest_path):
        print("FAIL 找不到内核契约：%s" % manifest_path)
        print("      路径不存在时不做任何降级判断——没有契约就没有可校验的术语。")
        return 2
    man = json.loads(read_text(manifest_path))

    tools = {t["name"]: t for t in man.get("tools", [])}
    facts = {f["id"]: f for f in man.get("facts", [])}
    verifiers = man.get("verifiers", {})

    # 工具往 values 里写的键名（part_ids / parts_dir / lod_path ...）也是合法标识符，
    # 它们在 manifest 的 writes 里声明过，不属于「幻觉」。
    writes = set()
    for t in tools.values():
        for w in (t.get("writes") or []):
            writes.add(w)

    print("[术语自检] %s" % HERE)
    print("  内核契约: %s" % manifest_path)
    print("  规模: %d 事实 / %d 工具 / %d 校验器" % (len(facts), len(tools), len(verifiers)))

    # 契约自身：每个事实都要有校验器，否则内核拒绝运行
    for fid in facts:
        if fid not in verifiers:
            errors.append("[契约] 事实 %s 没有绑定校验器（内核会拒绝运行）" % fid)

    # ---------- 契约快照：refs 里的工具表必须与 manifest 逐行对得上 ----------
    # 存在的漏洞：技能正文与 manifest 各自的 id 都存在，但 requires 内容不一致。
    # 这类漂移四个存在性检查全都抓不到——21:35 内核给 segment_semantic 补了
    # lod_ready，而 refs/pack-contract.md 还写着只有 input_ok，脚本照样 PASS。
    def _ids(cell):
        return {x.strip() for x in re.findall(r"`([^`]+)`", cell)}

    def check_snapshot(path, tools):
        errs = []
        if not os.path.isfile(path):
            return ["[快照] 找不到 %s —— 它是契约副本，缺了就无从对账" % path]
        text = read_text(path)
        m = re.search(r"^## 二、[^\n]*\n(.*?)(?=^## )", text, re.S | re.M)
        if not m:
            return ["[快照] %s 里找不到「## 二、N 个工具」这张表" % os.path.basename(path)]
        seen = set()
        for line in m.group(1).splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 6:
                continue
            name = cells[0].strip("`* ")
            if name not in tools:
                continue
            seen.add(name)
            t = tools[name]
            want_p = {t["produces"]}
            got_p = _ids(cells[1])
            if got_p != want_p:
                errs.append("[快照] %s 的 produces：表里 %s，契约 %s"
                            % (name, sorted(got_p) or "空", sorted(want_p)))
            want_r = set(t.get("requires") or [])
            got_r = _ids(cells[2])
            if got_r != want_r:
                errs.append("[快照] %s 的 requires：表里 %s，契约 %s"
                            % (name, sorted(got_r) or "空", sorted(want_r) or "空"))
            pm = re.search(r"\d+", cells[4])
            want_pr = t.get("priority", 0)
            if pm and int(pm.group()) != want_pr:
                errs.append("[快照] %s 的 priority：表里 %s，契约 %s"
                            % (name, pm.group(), want_pr))
        for name in sorted(set(tools) - seen):
            errs.append("[快照] 工具 %s 不在 %s 的表里" % (name, os.path.basename(path)))
        return errs

    def check_keyfields(path, man, tools):
        """对账「不是 id」的契约字段：const / needs_fields / verifiers。

        11-11 内核加了 model_kind 输入门、真值评估器与两个语义阈值，
        它们不在 tools/facts 里，前五类检查一个都看不见。责任落到两张表：
          「## 四、N 个常量」          → const 全量 + 值
          「## 四之二、输入门与校验器」 → needs_fields / verifiers 全量
        契约里每多一个字段，表里必须多一行；表里多出来的陈旧行也报错。
        """
        errs = []
        if not os.path.isfile(path):
            return ["[关键字段] 找不到 %s" % path]
        text = read_text(path)

        def rows_of(header):
            m = re.search(r"^## " + header + r"[^\n]*\n(.*?)(?=^## )", text, re.S | re.M)
            if not m:
                return None
            out = []
            for line in m.group(1).splitlines():
                if not line.lstrip().startswith("|"):
                    continue
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) < 2:
                    continue
                k, v = cells[0].strip("`* "), cells[1].strip("`* ")
                if not k or set(k) <= set("-: "):   # 跳过 |---|---| 分隔行
                    continue
                out.append((k, v))
            return out

        base = os.path.basename(path)
        cr = rows_of("四、")
        if cr is None:
            errs.append("[关键字段] %s 里找不到「## 四、N 个常量」表" % base)
        else:
            got = {k: v for k, v in cr if k != "常量"}
            want = {k: str(v) for k, v in (man.get("const") or {}).items()}
            for k in sorted(set(want) - set(got)):
                errs.append("[关键字段] const.%s 契约里有，常量表里没有 —— 技能仓对此无记载" % k)
            for k in sorted(set(got) - set(want)):
                errs.append("[关键字段] const.%s 常量表里有，契约里已不存在" % k)
            for k in sorted(set(got) & set(want)):
                if got[k] != want[k]:
                    errs.append("[关键字段] const.%s：表里 %s，契约 %s" % (k, got[k], want[k]))

        gr = rows_of("四之二、")
        if gr is None:
            errs.append("[关键字段] %s 里找不到「## 四之二、输入门与校验器」表" % base)
        else:
            got = {(k, v) for k, v in gr if k in ("needs_fields", "verifiers")}
            want = set()
            for t in tools.values():
                for f in (t.get("needs_fields") or []):
                    want.add(("needs_fields", f))
            for v in (man.get("verifiers") or {}):
                want.add(("verifiers", v))
            for kind, key in sorted(want - got):
                errs.append("[关键字段] 契约有 %s.%s，表里没有" % (kind, key))
            for kind, key in sorted(got - want):
                errs.append("[关键字段] 表里的 %s.%s 契约里已不存在" % (kind, key))

        # writes 也是「不是 id」的字段，且是技能读错键名的高发处（scale_resolved）
        mw = re.search(r"^### 2\.2[^\n]*\n(.*?)(?=^### |^## )", text, re.S | re.M)
        if not mw:
            errs.append("[关键字段] %s 里找不到「### 2.2 工具写入值」表" % base)
        else:
            got_w = {}
            for line in mw.group(1).splitlines():
                if not line.lstrip().startswith("|"):
                    continue
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) < 2:
                    continue
                name = cells[0].strip("`* ")
                if name in tools:
                    got_w[name] = set(_ids(cells[1]))
            for name, t in tools.items():
                want_w = set(t.get("writes") or [])
                if name not in got_w:
                    errs.append("[关键字段] 工具 %s 不在 2.2 表里" % name)
                elif got_w[name] != want_w:
                    errs.append("[关键字段] %s 的 writes：表里 %s，契约 %s"
                                % (name, sorted(got_w[name]) or "空", sorted(want_w) or "空"))
        return errs

    contract_ref = os.path.join(HERE, "refs", "pack-contract.md")
    errors += check_snapshot(contract_ref, tools)
    errors += check_keyfields(contract_ref, man, tools)

    # ---------- 逐个技能 ----------
    if not os.path.isdir(args.skills):
        print("FAIL 找不到技能目录：%s" % args.skills)
        return 2

    dirs = sorted(d for d in os.listdir(args.skills)
                  if os.path.isdir(os.path.join(args.skills, d)))
    if not dirs:
        print("FAIL 技能目录为空")
        return 2

    owner_of = {}          # 事实 -> 技能（唯一性）
    covered_tools = {}     # 工具 -> 技能
    covered_facts = {}

    # 合法标识符 = 契约登记过的全部名字（含 const / needs_fields 这类非 id 字段）
    legal = (set(tools) | set(facts) | writes | set(verifiers)
             | set(man.get("const") or {}) | NON_KERNEL_IDS)
    for t in tools.values():
        for f in (t.get("needs_fields") or []):
            legal.add(f)
            legal.add(f.split(".")[-1])
    # 前缀表动态补全：契约里新增工具时，它的首段自动进入幻觉检查范围
    all_prefixes = ID_PREFIXES + tuple(sorted(
        {x.split("_")[0] + "_" for x in legal if "_" in x}))
    unregistered = set()

    print("\n  技能: %d 个" % len(dirs))
    for d in dirs:
        p = os.path.join(args.skills, d, "SKILL.md")
        if not os.path.isfile(p):
            errors.append("[%s] 缺 SKILL.md" % d)
            continue
        text = read_text(p)
        fm, body = parse_frontmatter(text)
        if fm is None:
            errors.append("[%s] 缺 frontmatter（文件必须以 --- 开头）" % d)
            continue

        name = fm.get("name", "")
        if name != d:
            errors.append("[%s] frontmatter name=%r 与目录名不一致" % (d, name))
        if not fm.get("description"):
            errors.append("[%s] description 为空（检索不到就等于不存在）" % d)
        if not fm.get("whenToUse"):
            warns.append("[%s] 缺 whenToUse（agent 不知道何时加载它）" % d)
        if "立即停手" not in body:
            errors.append("[%s] 缺「立即停手」一节——没有它，技能退化成流程说明" % d)

        covers = fm.get("covers") or []
        own_facts = fm.get("facts") or []
        if isinstance(covers, str):
            covers = [covers]
        if isinstance(own_facts, str):
            own_facts = [own_facts]

        for t in covers:
            if t not in tools:
                errors.append("[%s] covers 里的 %r 在内核工具表里不存在" % (d, t))
            elif t in covered_tools:
                errors.append("[%s] 工具 %s 已被 %s 覆盖，重复归属" % (d, t, covered_tools[t]))
            else:
                covered_tools[t] = d

        for f in own_facts:
            if f not in facts:
                errors.append("[%s] facts 里的 %r 在内核事实表里不存在" % (d, f))
            else:
                # 事实允许多技能覆盖：parts_available 是两个路径的汇合点，
                # 语义分件与人工切割都必须理解它。工具归属仍必须唯一。
                covered_facts.setdefault(f, []).append(d)

        # 第 4 类（硬错误）：形似内核 id 却不存在的，如 cut_adaptive
        # 第 7 类（INFO）：所有未登记的 snake_case 词都收进来，末尾列出供人扫一眼
        ident = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
        for cand in sorted({m.strip("`") for m in re.findall(r"`([^`\n]{3,40})`", body)}):
            if not ident.match(cand):        # 排除 `lod_faces = 89954` 这类整句
                continue
            if cand.endswith(".py") or "/" in cand or cand.endswith(".json"):
                continue
            if cand in legal:
                continue
            if cand in DEPRECATED_TOKENS:
                # 旧字段名留痕是允许的，但「标记」不许被后人删掉
                mark = DEPRECATED_TOKENS[cand]
                if mark in body:
                    warns.append("[%s] `%s` 是已标记的作废字段，原文保留不删" % (d, cand))
                else:
                    errors.append("[%s] `%s` 属已作废字段，却缺 %r 标记——删标记等于销毁判错依据"
                                  % (d, cand, mark))
                continue
            unregistered.add(cand)
            if not cand.startswith(all_prefixes):
                continue
            errors.append("[%s] 正文引用了不存在的内核 id：%r" % (d, cand))

        print("  %-16s covers=%-2d facts=%-2d %s" % (
            d, len(covers), len(own_facts), "" if not errors else ""))

    # ---------- 完备性 ----------
    miss_t = [t for t in tools if t not in covered_tools]
    miss_f = [f for f in facts if f not in covered_facts]
    if miss_t:
        errors.append("[完备] %d 个工具没有任何技能覆盖：%s" % (len(miss_t), "、".join(miss_t)))
    if miss_f:
        errors.append("[完备] %d 个事实没有任何技能覆盖：%s" % (len(miss_f), "、".join(miss_f)))

    print("\n  覆盖: %d/%d 工具, %d/%d 事实" % (
        len(covered_tools), len(tools), len(covered_facts), len(facts)))

    shared = {f: who for f, who in covered_facts.items() if len(who) > 1}
    for f, who in sorted(shared.items()):
        print("  共享事实 %s ← %s" % (f, "、".join(who)))

    # 同一事实由多个工具产出：规划器按 priority 选，技能必须知道这件事
    prod = {}
    for t in tools.values():
        prod.setdefault(t["produces"], []).append(t["name"])
    for fid, ts in sorted(prod.items()):
        if len(ts) > 1:
            pr = "、".join("%s(p=%s)" % (t, tools[t].get("priority")) for t in ts)
            print("  竞争产出 %s ← %s" % (fid, pr))

    # 第 7 类：前缀法抓不到陌生前缀（如 merge_parts），至少列出来让人过目
    listed = sorted(x for x in unregistered if "_" in x)
    if listed:
        print("\n  未登记标识符 %d 个（INFO，非错误；契约里没有，属正文自造词或待登记概念）:"
              % len(listed))
        print("    " + "、".join(listed[:40]) + (" …" if len(listed) > 40 else ""))

    # ---------- 输出 ----------
    for w in warns:
        print("  WARN  %s" % w)
    if errors:
        print("\n  FAIL  %d 项：" % len(errors))
        for e in errors:
            print("    - %s" % e)
        print("\n结论: FAIL")
        return 1

    print("\n结论: PASS  术语与内核契约一致，%d 个工具全覆盖" % len(tools))
    return 0


if __name__ == "__main__":
    sys.exit(main())
