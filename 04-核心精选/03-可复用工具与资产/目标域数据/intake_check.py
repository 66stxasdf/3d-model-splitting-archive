# -*- coding: utf-8 -*-
"""intake_check：incoming/ 新样本落地体检（零安装、零联网、只读被检文件）。

定位（2026-11-11 定）：
  用户从 Rodin 等来源手动下载模型 → 扔进 incoming/ → 跑本脚本 →
  输出几何体检 + 元数据合并 + 分级结论，供台账更新与后续标注使用。
  本脚本不改被检文件、不训练、不联网；只写报告文件。

体检项（复用 错误模式编目.py 的 probe，含 merge_vertices 修复）：
  faces / vertices / watertight / components / degenerate_faces / bbox / volume
另加：
  sha1_12   文件指纹（查重：与已知错误模型同 hash = 同一個错误模型）
  verdict   几何分级：合格 / 关注 / 拒收（判据见下，人工可推翻）

判据（只判几何，不判语义——语义对错只有用户能判）：
  拒收：空网格/不可解析；faces < 500（碎块，不可能是完整人形）
  关注：非水密 / 多壳(>1) / 退化面>0 / 单位线索异常（bbox_max<10 疑似 m/dm 制）
  合格：水密 + 单壳 + 无退化面 + faces>=500

元数据：读 incoming/_meta.csv（首次运行自动生成模板，填好后重跑即并入）。
  列：file,prompt,gen_version,tier_cost,source_url,license,note

用法：
    python intake_check.py                  # 体检 incoming/ 全部模型文件
    python intake_check.py --dedupe-error   # 追加：对黑名单错误模型算 sha1 查重（慢，~GB 级）
"""

import csv
import hashlib
import importlib.util
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
INCOMING = os.path.join(BASE, "incoming")
ROOT = os.path.dirname(BASE)
OUT_MD = os.path.join(INCOMING, "_intake报告.md")
OUT_CSV = os.path.join(INCOMING, "_intake明细.csv")
META_CSV = os.path.join(INCOMING, "_meta.csv")
MODEL_EXTS = (".stl", ".obj", ".glb", ".fbx", ".ply", ".3mf")
MIN_FACES = 500


def load_probe():
    """从 错误模式编目.py 复用 probe()（同一目录，含 merge_vertices 修复）。"""
    path = os.path.join(BASE, "错误模式编目.py")
    spec = importlib.util.spec_from_file_location("errcat", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.probe, mod.unit_hint


def sha1_12(path, limit=None):
    h = hashlib.sha1()
    n = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            n += len(b)
            if limit and n >= limit:
                break
    return h.hexdigest()[:12]


def verdict(row):
    if row["faces"] == 0:
        return "拒收", "空网格/不可解析"
    if row["faces"] < MIN_FACES:
        return "拒收", f"faces={row['faces']} < {MIN_FACES}（碎块，非完整人形）"
    bad = []
    if not row["watertight"]:
        bad.append("非水密")
    if (row["components"] or 0) > 1:
        bad.append(f"多壳({row['components']})")
    if (row["degenerate_faces"] or 0) > 0:
        bad.append(f"退化面({row['degenerate_faces']})")
    if bad:
        return "关注", "；".join(bad)
    if row["bbox_max"] is not None and row["bbox_max"] < 10:
        return "关注", f"单位线索异常（bbox_max={row['bbox_max']}，疑似 m/dm 制，需人工定标）"
    return "合格", "水密+单壳+无退化面"


def load_meta():
    meta = {}
    if os.path.exists(META_CSV):
        with open(META_CSV, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r.get("file"):
                    meta[r["file"].strip()] = r
    return meta


def write_meta_template(files):
    cols = ["file", "prompt", "gen_version", "tier_cost", "source_url", "license", "note"]
    with open(META_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for fn in files:
            w.writerow({c: (fn if c == "file" else "") for c in cols})


def blacklist_hashes():
    """黑名单错误模型的 sha1 集合（查重用；--dedupe-error 才跑）。"""
    from glob import glob
    hashes = {}
    dirs = [
        "01-ColorPartsKit-彩色手办建模", "02-AstronautKid-宇航员", "03-Minecraft-角色",
        "04-分件结构分析", "08-杂项", "09-最小证伪实验", "11-分件打印交付包",
        "12-交互式画线分割", "13-绑骨拆分-23件刚性蒙皮", "15-新模型画线分割链路",
    ]
    for d in dirs:
        for p in glob(os.path.join(ROOT, d, "**", "*"), recursive=True):
            if os.path.isfile(p) and p.lower().endswith(".stl"):
                hashes.setdefault(sha1_12(p), os.path.relpath(p, ROOT))
    return hashes


def main():
    dedupe = "--dedupe-error" in sys.argv
    if not os.path.isdir(INCOMING):
        print("incoming/ 不存在：", INCOMING)
        return 1
    probe, unit_hint = load_probe()
    files = sorted(
        os.path.join(INCOMING, fn)
        for fn in os.listdir(INCOMING)
        if fn.lower().endswith(MODEL_EXTS) and not fn.startswith("_")
    )
    if not files:
        print("incoming/ 里还没有模型文件。")
        return 0
    meta = load_meta()
    if not os.path.exists(META_CSV):
        write_meta_template([os.path.basename(p) for p in files])
        print(f"已生成元数据模板 → {META_CSV}（填写后重跑即并入报告）")

    err_hashes = blacklist_hashes() if dedupe else {}
    rows = []
    for p in files:
        fn = os.path.basename(p)
        r = probe(p)
        r["file"] = fn
        r["sha1_12"] = sha1_12(p)
        r["unit_hint"] = unit_hint(r["bbox_max"])
        v, why = verdict(r)
        r["verdict"], r["verdict_why"] = v, why
        if dedupe and r["sha1_12"] in err_hashes:
            r["verdict"] = "拒收"
            r["verdict_why"] = f"与已知错误模型同 hash：{err_hashes[r['sha1_12']]}"
        m = meta.get(fn, {})
        r["prompt"] = (m.get("prompt") or "").strip()
        r["gen_version"] = (m.get("gen_version") or "").strip()
        r["tier_cost"] = (m.get("tier_cost") or "").strip()
        r["source_url"] = (m.get("source_url") or "").strip()
        rows.append(r)

    cols = ["file", "sha1_12", "size_mb", "faces", "vertices", "watertight",
            "components", "degenerate_faces", "bbox", "bbox_max", "unit_hint",
            "verdict", "verdict_why", "prompt", "gen_version", "tier_cost",
            "source_url", "note"]
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    n_ok = sum(1 for r in rows if r["verdict"] == "合格")
    n_warn = sum(1 for r in rows if r["verdict"] == "关注")
    n_bad = sum(1 for r in rows if r["verdict"] == "拒收")
    lines = [
        "# incoming 落地体检报告（intake_check 自动生成，勿手改）",
        "",
        f"- 体检模型 **{len(rows)}** 个：合格 {n_ok} / 关注 {n_warn} / 拒收 {n_bad}",
        f"- 明细：`_intake明细.csv`；元数据：`_meta.csv`（未填的字段报告里为空）",
        "- 判据只判几何；语义对错（是不是目标域、部件划分对不对）由用户判，脚本不臆测",
        "",
        "| 文件 | 指纹 | 面数 | 水密 | 壳 | 退化面 | bbox | 单位线索 | 结论 | 依据 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['file']} | {r['sha1_12']} | {r['faces']} | "
            f"{'是' if r['watertight'] else '否'} | {r['components']} | "
            f"{r['degenerate_faces']} | {r['bbox']} | {r['unit_hint']} | "
            f"**{r['verdict']}** | {r['verdict_why']} |"
        )
    filled = [r for r in rows if r["prompt"] or r["gen_version"]]
    lines += ["", "## 元数据覆盖", "",
              f"- 已填 prompt/版本 的模型：{len(filled)}/{len(rows)}",
              "- 空缺去 `_meta.csv` 补（下载页面复制 prompt、记 Gen 版本与档位）"]
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"体检 {len(rows)} 个：合格 {n_ok} / 关注 {n_warn} / 拒收 {n_bad}")
    print(f"报告 → {OUT_MD}")
    print(f"明细 → {OUT_CSV}")
    for r in rows:
        print(f"  [{r['verdict']}] {r['file']}  faces={r['faces']} "
              f"watertight={r['watertight']} shells={r['components']}  {r['verdict_why']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
