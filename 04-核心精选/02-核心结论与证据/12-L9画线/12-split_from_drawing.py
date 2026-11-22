#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""后台 agent：读取画线器导出的 JSON，按人画的割线把模型真正切成两件。

用法：
    python split_from_drawing.py demo_seam.json
    python split_from_drawing.py my_seam.json --cap fan --out 输出目录

链路：
  loop_faces（人画 + agent 补全的闭环面带）
    → 按 band_width 扩带
    → 从面图删掉带面，connected_components 分块
    → 取最大两块，带面按邻居多数回分配
    → 各自导出 STL / GLB，报告边界边、体积、水密性
    → 可选 fan 封盖（按边界环质心扇形三角化）

兼容：seam_draw/v1、v2（线段式）、v2+refined（refine_seam.py 平滑后）。
本脚本只读 loop_faces / band_width / nf，三者的切分口径完全一致。

口径：分块只依赖面邻接，不含任何代价场 / 语义预测 / cut_cost。
割线来自人的设计意图；本脚本只执行，不评判这条缝好不好。
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import trimesh
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

LAB = r"D:\workbuddy产物\04-分件结构分析\parts_struct\_labels.npz"
HERE = os.path.dirname(os.path.abspath(__file__))


def bfs_groups(NF, A, band):
    """删掉 band 面后分块，返回 (lab, sizes, order)。lab=-1 表示带面/未分配。"""
    a0, a1 = A[:, 0], A[:, 1]
    ok = (~band[a0]) & (~band[a1])
    g = csr_matrix((np.ones(int(ok.sum()), dtype=np.uint8), (a0[ok], a1[ok])),
                   shape=(NF, NF))
    n, comp = connected_components(g, directed=False)
    sizes = np.bincount(comp[~band], minlength=n)
    order = np.argsort(-sizes)
    lab = np.full(NF, -1, np.int32)
    lab[~band] = comp[~band]
    return lab, sizes, order, n


def assign_band(NF, A, band, lab):
    """带面按邻接多数回分配给某一块；无邻居的丢弃。"""
    from collections import defaultdict
    votes = defaultdict(lambda: defaultdict(int))
    a0, a1 = A[:, 0], A[:, 1]
    for x, y in zip(a0, a1):
        if band[x] and lab[y] >= 0:
            votes[x][lab[y]] += 1
        if band[y] and lab[x] >= 0:
            votes[y][lab[x]] += 1
    for f, cnt in votes.items():
        lab[f] = max(cnt.items(), key=lambda kv: kv[1])[0]
    return lab


def boundary_rings(mesh):
    """从边界边串出有序顶点环。"""
    from collections import defaultdict
    # trimesh 版本间 edges_boundary 不稳定，直接按边出现次数取 c==1
    e = np.sort(mesh.edges, axis=1)
    u, c = np.unique(e, axis=0, return_counts=True)
    be = u[c == 1]
    if len(be) == 0:
        return []
    adj = defaultdict(list)
    edges = set()
    for i, (u, v) in enumerate(be):
        adj[u].append((v, i))
        adj[v].append((u, i))
        edges.add(i)
    rings = []
    while edges:
        e0 = next(iter(edges))
        u0 = int(be[e0][0])
        ring, used, cur, prev_e = [u0], set(), u0, e0
        while True:
            nxt = None
            for v, e in adj[cur]:
                if e in edges:
                    nxt, ne = v, e
                    break
            if nxt is None:
                break
            edges.discard(ne)
            used.add(ne)
            if nxt == u0:
                break
            ring.append(nxt)
            cur = nxt
            if len(ring) > len(be) + 2:
                break
        if len(ring) >= 3:
            rings.append(ring)
    return rings


def cap_fan(mesh):
    """按边界环质心做扇形封盖；返回 (新 mesh, 环数, 环长列表)。"""
    rings = boundary_rings(mesh)
    if not rings:
        return mesh, 0, []
    verts = list(mesh.vertices)
    faces = list(mesh.faces)
    sizes = []
    for ring in rings:
        c = np.mean(np.asarray(verts)[ring], axis=0)
        ci = len(verts)
        verts.append(c)
        for i in range(len(ring)):
            faces.append((ring[i], ring[(i + 1) % len(ring)], ci))
        sizes.append(len(ring))
    out = trimesh.Trimesh(vertices=np.asarray(verts), faces=np.asarray(faces),
                          process=False)
    return out, len(rings), sizes


def mesh_stats(m, tag):
    e = np.sort(m.edges, axis=1)
    _, c = np.unique(e, axis=0, return_counts=True)
    nb = int((c == 1).sum())
    nn = int((c > 2).sum())
    return dict(tag=tag, faces=int(len(m.faces)), vertices=int(len(m.vertices)),
                boundary_edges=nb, nonmanifold_edges=nn,
                watertight=bool(m.is_watertight),
                winding_consistent=bool(m.is_winding_consistent),
                volume_cm3=round(float(abs(m.volume)) / 1000, 3) if m.is_watertight else None,
                extents_mm=[round(float(x), 1) for x in m.extents])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json", nargs="?", default=os.path.join(HERE, "demo_seam.json"))
    ap.add_argument("--cap", choices=("none", "fan"), default="none")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    t0 = time.time()
    d = json.load(open(args.json, encoding="utf-8"))
    if d.get("schema") not in ("seam_draw/v1", "seam_draw/v2", "seam_draw/v2+refined"):
        print(f"[警告] schema={d.get('schema')}，仍按 loop_faces 契约解析")
    out = args.out or os.path.join(HERE, "split_" + os.path.splitext(
        os.path.basename(args.json))[0])
    os.makedirs(out, exist_ok=True)

    z = np.load(LAB, allow_pickle=True)
    V, F = z["verts"], z["faces"]
    NF = len(F)
    assert d.get("nf", NF) == NF, f"面数不符：JSON {d.get('nf')} vs 网格 {NF}"
    m = trimesh.Trimesh(vertices=V, faces=F, process=False)
    A, EV = m.face_adjacency, m.face_adjacency_edges

    loop = np.asarray(d["loop_faces"], dtype=np.int64)
    loop = np.unique(loop)                      # 去重，闭环顺序不影响分块
    bw = int(d.get("band_width", 1))
    band = np.zeros(NF, bool)
    band[loop] = True
    if bw > 1:
        frontier = loop
        for _ in range(bw - 1):
            nxt = []
            for e in np.flatnonzero(band[A[:, 0]] ^ band[A[:, 1]]):
                x, y = A[e]
                g = y if band[x] else x
                if not band[g]:
                    band[g] = True
                    nxt.append(g)
            frontier = np.asarray(nxt, np.int64)
        del frontier

    lab, sizes, order, ncomp = bfs_groups(NF, A, band)
    big = [int(i) for i in order if sizes[i] > NF * 0.01]
    ok_two = len(big) >= 2
    if not ok_two:
        print(f"[失败] 删带后只有 {len(big)} 个大块（>1% 面），割线未闭合。")
        print("       请回到画线器多画几段、切换『绕背补全』，或把面带宽度调回 1。")
        rep = dict(ok=False, n_components=int(ncomp), big_blocks=len(big),
                   sizes=[int(s) for s in sizes[order[:10]]])
        json.dump(rep, open(os.path.join(out, "split_report.json"), "w",
                            encoding="utf-8"), ensure_ascii=False, indent=2)
        return 1

    ia, ib = big[0], big[1]
    lab = assign_band(NF, A, band, lab)
    fa = np.flatnonzero(lab == ia)
    fb = np.flatnonzero(lab == ib)
    dropped = int((lab < 0).sum())

    parts = {}
    for tag, ids in (("A", fa), ("B", fb)):
        sub = m.submesh([ids], append=True, repair=False)
        if isinstance(sub, list):
            sub = sub[0]
        parts[tag] = sub

    # 封盖
    capinfo = {}
    if args.cap == "fan":
        for tag in ("A", "B"):
            capped, nr, sz = cap_fan(parts[tag])
            parts[tag] = capped
            capinfo[tag] = dict(rings=nr, ring_sizes=sz)

    stats = {tag: mesh_stats(parts[tag], tag) for tag in ("A", "B")}
    for tag in ("A", "B"):
        parts[tag].export(os.path.join(out, f"part_{tag}.stl"))

    # 彩色预览 GLB（两件按组染色，合并为一个场景）
    ca = trimesh.Scene()
    for tag, col in (("A", [80, 190, 220, 255]), ("B", [240, 130, 80, 255])):
        pm = parts[tag].copy()
        pm.visual.face_colors = np.tile(np.array(col, np.uint8), (len(pm.faces), 1))
        ca.add_geometry(pm, node_name=f"part_{tag}")
    ca.export(os.path.join(out, "split_preview.glb"))

    # 割线几何长度（loop 面之间相邻的对偶边对应的原始网格边）
    pair = {}
    for e, (x, y) in enumerate(A):
        pair[(min(x, y), max(x, y))] = e
    cut_len = 0.0
    ncut = 0
    for i in range(len(loop)):
        key = (min(loop[i], loop[(i + 1) % len(loop)]),
               max(loop[i], loop[(i + 1) % len(loop)]))
        if key in pair:
            e = pair[key]
            cut_len += float(np.linalg.norm(V[EV[e, 0]] - V[EV[e, 1]]))
            ncut += 1

    rep = dict(
        ok=True, source_json=os.path.basename(args.json),
        cap_mode=args.cap, band_width=bw,
        n_loop_faces=int(len(loop)), n_band_faces=int(band.sum()),
        n_components_after_band=int(ncomp),
        big_block_count=len(big),
        part_A_faces=int(len(fa)), part_B_faces=int(len(fb)),
        dropped_faces=dropped,
        balance_ratio=round(min(len(fa), len(fb)) / max(len(fa), len(fb)), 4),
        seam_loop_edges=int(ncut), seam_length_mm=round(cut_len, 2),
        parts=stats, cap_info=capinfo,
        verdict=("按人画的割线成功分成两块；" +
                 ("已 fan 封盖。" if args.cap == "fan" else
                  "默认导出开放曲面（未封盖）—— 边界边数见 parts，打印前需自行封盖或加 --cap fan。")),
        caveats=["分块只用面邻接，不评判缝的语义合理性 / 可装配性",
                 "fan 封盖是质心扇形三角化，凹陷环可能自交；水密与绕序已如实报告",
                 "体积仅在水密时报告，未扣打印填充率"],
        elapsed_sec=round(time.time() - t0, 1))
    json.dump(rep, open(os.path.join(out, "split_report.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"[OK] {rep['part_A_faces']:,} / {rep['part_B_faces']:,} 面 "
          f"(均衡度 {rep['balance_ratio']:.3f})，丢弃 {dropped} 面")
    for tag in ("A", "B"):
        s = stats[tag]
        print(f"     part_{tag}: 水密={s['watertight']} 边界边={s['boundary_edges']:,} "
              f"非流形={s['nonmanifold_edges']} 体积={s['volume_cm3']} cm3")
    print(f"     输出目录 {out}  ({rep['elapsed_sec']}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
