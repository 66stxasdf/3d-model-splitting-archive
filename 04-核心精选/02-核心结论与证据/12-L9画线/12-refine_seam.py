#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""后台 agent：把线段式画线的面心/边中点折线加工得更平滑，并如实复验闭环。

用法
----
    python refine_seam.py my_seam_v2demo.json
    python refine_seam.py my_seam.json --iters 3 --ds 0.6 --max-dev 1.5
    python refine_seam.py my_seam.json --iters 0          # 只重采样+投影，不平滑（对照）

链路
----
    segments[].pts（前端真实产出的表面折线）
      → 按弧长均匀重采样 ds
      → 切平面内 Taubin 平滑（法向分量不动：只改形状、不推离表面）
      → closest_point 投影回网格表面，取 triangle_id
      → 面序列去重 + 断裂处用面邻接 BFS 补一跳
      → 与 completion_faces 拼成新 loop_faces
      → 用 split_from_drawing.py 同口径的 connected_components 复验大块数
      → 输出 *_refined.json / refine_report.json / smooth_comparison.png

口径与边界
----------
* 平滑对象是「面心/边中点折线的三角尺度锯齿」，不是把用户画的直线掰弯；
  用户的 anchors 与 bulge 是设计意图，脚本不改动。
* agent 补全段(completion_faces)不平滑：它是几何外推，不是人画的线。
* 平滑后**必须重跑闭环判据**；大块数 ≠ 2 或 Hausdorff > 阈值时如实告警，
  原 JSON 不覆盖，refined 只作为候选另存。
* 不加载 cut_cost / 不预测语义 / 不评判缝的合理性与可装配性。
"""
import argparse
import json
import os
import sys
import time
from collections import deque

import numpy as np
import trimesh
from PIL import Image, ImageDraw, ImageFont
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

LAB = r"D:\workbuddy产物\04-分件结构分析\parts_struct\_labels.npz"
HERE = os.path.dirname(os.path.abspath(__file__))
FONT = "C:/Windows/Fonts/msyh.ttc"


# ---------------------------------------------------------------- 折线工具
def load_polyline(d):
    """从 v2 segments / v1 strokes 取用户折线；返回 (N,3) 与是否闭合。"""
    pts = []
    if d.get("segments"):
        for s in d["segments"]:
            for p in s.get("pts", []):
                pts.append([float(x) for x in p])
    elif d.get("strokes"):
        for s in d["strokes"]:
            for p in s.get("pts", []):
                pts.append([float(x) for x in p])
    if len(pts) < 3:
        raise ValueError("JSON 里没有可用的用户折线（segments[].pts / strokes[].pts）")
    P = np.asarray(pts, np.float64)
    keep = [0]
    for i in range(1, len(P)):
        if np.linalg.norm(P[i] - P[keep[-1]]) > 1e-6:
            keep.append(i)
    return P[keep], bool(d.get("closed", False))


def resample(P, ds, closed=False):
    """按弧长均匀重采样；点数由总长决定，保证前后可比。"""
    Q = np.vstack([P, P[0]]) if closed else P
    d = np.linalg.norm(np.diff(Q, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(d)])
    total = cum[-1]
    if total < 1e-9:
        return P.copy()
    n = max(3, int(round(total / ds)) + 1)
    s = np.linspace(0.0, total, n)
    out = np.zeros((n, 3))
    j = 0
    for i, si in enumerate(s):
        while j < len(cum) - 2 and cum[j + 1] < si:
            j += 1
        t = 0.0 if cum[j + 1] - cum[j] < 1e-12 else (si - cum[j]) / (cum[j + 1] - cum[j])
        out[i] = Q[j] * (1 - t) + Q[j + 1] * t
    return out


def turning_angle(P, closed=False):
    """相邻方向夹角之和（度）。点数/弧长一致时才有可比性。"""
    Q = np.vstack([P, P[0]]) if closed else P
    if len(Q) < 3:
        return 0.0
    a = np.diff(Q, axis=0)
    la = np.linalg.norm(a, axis=1)
    ok = (la[:-1] > 1e-9) & (la[1:] > 1e-9)
    if not ok.any():
        return 0.0
    co = np.sum(a[:-1] * a[1:], axis=1) / (la[:-1] * la[1:] + 1e-18)
    return float(np.degrees(np.arccos(np.clip(co[ok], -1, 1))).sum())


def polyline_len(P, closed=False):
    Q = np.vstack([P, P[0]]) if closed else P
    return float(np.linalg.norm(np.diff(Q, axis=0), axis=1).sum())


def taubin_tangent(P, normals, iters, lam=0.5, mu=-0.53, closed=False):
    """Taubin λ|μ 平滑，但只保留切平面内分量 —— 法向分量不动，防止推离表面。"""
    out = P.copy()
    n = len(out)
    for _ in range(iters):
        for w in (lam, mu):
            d = np.zeros_like(out)
            if closed:
                d = 0.5 * (np.roll(out, 1, axis=0) + np.roll(out, -1, axis=0)) - out
            else:
                d[1:-1] = 0.5 * (out[:-2] + out[2:]) - out[1:-1]
                d[0] = 0.0
                d[-1] = 0.0
            # 去掉法向分量：δ_t = δ - (δ·n)n
            dot = np.sum(d * normals, axis=1, keepdims=True)
            d = d - dot * normals
            out += w * d
    return out


# ---------------------------------------------------------------- 面图工具
class FaceGraph:
    def __init__(self, A, NF):
        a0, a1 = A[:, 0].astype(np.int64), A[:, 1].astype(np.int64)
        # 按源面稳定排序构造 CSR。注意不能用 nbr[cur[a0]]=a1 的向量化写法：
        # a0 里有重复面号，fancy-index 赋值只保留最后一次，会留下未初始化的空洞。
        src = np.concatenate([a0, a1])
        dst = np.concatenate([a1, a0])
        order = np.argsort(src, kind="stable")
        self.nbr = np.asarray(dst[order], np.int32)
        self.off = np.concatenate([[0], np.cumsum(np.bincount(src, minlength=NF))])
        self.NF = NF

    def adjacent(self, x, y):
        return bool(np.any(self.nbr[self.off[x]:self.off[x + 1]] == y))

    def bfs(self, src, dst, limit=14):
        if src == dst:
            return [src]
        seen = {int(src)}
        q = deque([(int(src), [int(src)])])
        while q:
            f, path = q.popleft()
            if len(path) > limit:
                continue
            for g in self.nbr[self.off[f]:self.off[f + 1]]:
                g = int(g)
                if g in seen:
                    continue
                seen.add(g)
                npth = path + [g]
                if g == dst:
                    return npth
                q.append((g, npth))
        return None


def expand_neighborhood(seed, G, k=3):
    """原割线面带的 k 环邻域。投影只在这个范围内找最近点，防止穿到模型背面。"""
    s = set(int(x) for x in seed)
    for _ in range(k):
        add = set()
        for f in s:
            for g in G.nbr[G.off[f]:G.off[f + 1]]:
                add.add(int(g))
        s |= add
    return np.asarray(sorted(s), np.int64)


def patch_gaps(faces, G):
    """面序列去重 + 相邻不接处 BFS 补一跳；返回 (新序列, 补的面数, 补不上的断点数)。"""
    ded = []
    for f in faces:
        f = int(f)
        if not ded or ded[-1] != f:
            ded.append(f)
    if len(ded) < 2:
        return ded, 0, 0
    out = [ded[0]]
    added, broken = 0, 0
    for f in ded[1:]:
        if f == out[-1]:
            continue
        if G.adjacent(out[-1], f):
            out.append(f)
            continue
        path = G.bfs(out[-1], f)
        if path:
            out.extend(path[1:])
            added += len(path) - 2
        else:
            out.append(f)
            broken += 1
    return out, added, broken


def big_blocks(loop_faces, A, NF, bw=1):
    """与 split_from_drawing.py bfs_groups 同口径复验闭环。"""
    band = np.zeros(NF, bool)
    band[np.unique(np.asarray(loop_faces, np.int64))] = True
    if bw > 1:
        for _ in range(bw - 1):
            edge = np.flatnonzero(band[A[:, 0]] ^ band[A[:, 1]])
            for e in edge:
                x, y = A[e]
                g = y if band[x] else x
                band[g] = True
    a0, a1 = A[:, 0], A[:, 1]
    ok = (~band[a0]) & (~band[a1])
    g = csr_matrix((np.ones(int(ok.sum()), np.uint8), (a0[ok], a1[ok])), shape=(NF, NF))
    n, comp = connected_components(g, directed=False)
    sizes = np.bincount(comp[~band], minlength=n)
    big = sizes[sizes > NF * 0.01]
    return int(len(big)), [int(x) for x in np.sort(big)[::-1][:3]], int(band.sum())


# ---------------------------------------------------------------- 对照图
def draw_comparison(P0, P1, rep, out_png):
    W, H = 1240, 820
    img = Image.new("RGB", (W, H), (20, 22, 28))
    dr = ImageDraw.Draw(img)
    try:
        f16 = ImageFont.truetype(FONT, 16)
        f13 = ImageFont.truetype(FONT, 13)
        f22 = ImageFont.truetype(FONT, 22)
    except OSError:
        f16 = f13 = f22 = ImageFont.load_default()
    fg, mut, yellow, grey = (233, 236, 243), (152, 160, 179), (255, 214, 70), (136, 142, 158)
    dr.text((22, 16), "线段式割线 · 后台平滑前后对照", font=f22, fill=fg)
    m = rep["metrics"]
    dr.text((22, 50),
            f"转折角 {m['turning_before_deg']:.0f}° → {m['turning_after_deg']:.0f}° "
            f"({m['turning_reduction_pct']:+.1f}%)   |   "
            f"弧长 {m['len_before_mm']:.2f} → {m['len_after_mm']:.2f} mm   |   "
            f"最大离面 {m['max_off_surface_mm']:.3f} mm   |   "
            f"Hausdorff {m['hausdorff_max_mm']:.3f} mm",
            font=f13, fill=(102, 215, 154))
    dr.text((22, 74),
            f"闭环大块数 {rep['big_blocks_before']} → {rep['big_blocks_after']}   |   "
            f"面带 {rep['band_faces_before']} → {rep['band_faces_after']}   |   "
            f"补断点 {rep['n_patched_faces']} / 未补上 {rep['n_unpatched_gaps']}   |   {rep['verdict']}",
            font=f13, fill=(80, 205, 235))

    allp = np.vstack([P0, P1])
    lo, hi = allp.min(0), allp.max(0)
    panels = [("XY 视图（俯视）", 0, 1), ("XZ 视图（正视）", 0, 2)]
    pw, ph, top = 580, 620, 108
    for i, (title, ax, ay) in enumerate(panels):
        ox = 22 + i * (pw + 20)
        dr.rounded_rectangle([ox, top, ox + pw, top + ph], radius=8,
                             fill=(28, 31, 40), outline=(46, 51, 64), width=1)
        dr.text((ox + 14, top + 10), title, font=f16, fill=fg)
        span = max(float(hi[ax] - lo[ax]), float(hi[ay] - lo[ay]), 1e-6)
        scale = (pw - 60) / span
        cx = ox + pw / 2 - (float(lo[ax] + hi[ax]) / 2) * scale
        cy = top + ph / 2 + (float(lo[ay] + hi[ay]) / 2) * scale

        def proj(P):
            return [(cx + float(p[ax]) * scale, cy - float(p[ay]) * scale) for p in P]

        a, b = proj(P0), proj(P1)
        if len(a) > 1:
            dr.line(a, fill=grey, width=2)
        if len(b) > 1:
            dr.line(b, fill=yellow, width=2)
        # 端点标记
        for p in (a[0], a[-1]):
            dr.ellipse([p[0] - 3, p[1] - 3, p[0] + 3, p[1] + 3], fill=(255, 255, 255))
        dr.line([ox + 14, top + ph - 40, ox + 44, top + ph - 40], fill=grey, width=2)
        dr.text((ox + 50, top + ph - 49), "平滑前（原始表面折线）", font=f13, fill=mut)
        dr.line([ox + 14, top + ph - 20, ox + 44, top + ph - 20], fill=yellow, width=2)
        dr.text((ox + 50, top + ph - 29), "平滑后（Taubin + 投影回表面）", font=f13, fill=mut)
    dr.text((22, H - 68),
            "口径：前后都重采样到同一弧长步长再算转折角，避免点数差异造成假改善；"
            "平滑只改形状、不改 anchors 与 bulge。",
            font=f13, fill=mut)
    dr.text((22, H - 44),
            "平滑后重跑闭环判据；大块数≠2 或 Hausdorff 超阈值时告警，原 JSON 不覆盖。",
            font=f13, fill=(255, 180, 84))
    img.save(out_png)
    return out_png


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json", nargs="?", default=os.path.join(HERE, "my_seam_v2demo.json"))
    ap.add_argument("--ds", type=float, default=0.6, help="弧长重采样步长 mm（默认 0.6）")
    ap.add_argument("--iters", type=int, default=8,
                    help="Taubin 迭代次数（默认 8：迭代扫描的收益拐点；0 = 不平滑，作对照）")
    ap.add_argument("--lam", type=float, default=0.5)
    ap.add_argument("--mu", type=float, default=-0.53)
    ap.add_argument("--max-dev", type=float, default=1.5, help="Hausdorff 告警阈值 mm")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    t0 = time.time()
    src = args.json
    d = json.load(open(src, encoding="utf-8"))
    stem = os.path.splitext(os.path.basename(src))[0]
    out = args.out or HERE
    os.makedirs(out, exist_ok=True)

    z = np.load(LAB, allow_pickle=True)
    V, F = z["verts"], z["faces"]
    NF = len(F)
    assert int(d.get("nf", NF)) == NF, "面数不符"
    m = trimesh.Trimesh(vertices=V, faces=F, process=False)
    A = m.face_adjacency
    G = FaceGraph(A, NF)

    P0, closed = load_polyline(d)
    n0 = len(P0)
    # 公平基线：平滑前也重采样到同一 ds
    Pbase = resample(P0, args.ds, closed)
    cp0, _, tid0 = trimesh.proximity.closest_point(m, Pbase)
    nrm = np.asarray(m.face_normals[np.clip(tid0, 0, NF - 1)], np.float64)
    nl = np.linalg.norm(nrm, axis=1, keepdims=True)
    nrm = nrm / np.where(nl < 1e-12, 1.0, nl)

    Pwork = cp0.copy()                      # 从表面上的重采样点出发
    if args.iters > 0:
        Pwork = taubin_tangent(Pwork, nrm, args.iters, args.lam, args.mu, closed)

    # 投影限制在「原割线面带的 3 环邻域」：薄壁处全局最近点会穿到背面，
    # 那等于平滑改变了设计意图，也会让面带断裂。实测这一约束是闭环能否保住的关键。
    seed = np.asarray(d.get("user_faces", []), np.int64)
    cand = expand_neighborhood(seed, G, k=3)
    sub = trimesh.Trimesh(vertices=V, faces=F[cand], process=False)
    # closest_point 返回 (最近点数组, 距离, triangle_id)；距离是第二项，别解包错
    _, off, _ = trimesh.proximity.closest_point(sub, Pwork)
    max_off = float(np.max(off))
    cp2, dist2, tid2 = trimesh.proximity.closest_point(sub, Pwork)
    P1 = np.asarray(cp2, np.float64)
    tid_orig = cand[np.asarray(tid2, np.int64)]

    completion = [int(x) for x in d.get("completion_faces", [])]
    loop_old = [int(x) for x in d.get("loop_faces", [])]
    user_new, _, _ = patch_gaps(list(tid_orig), G)
    # 用户段 + 补全段必须一起串成连续面带，否则平滑换掉首尾面后闭环会漏
    loop_new, n_patch, n_broken = patch_gaps(user_new + completion, G)
    faces = user_new

    bw = int(d.get("band_width", 1))
    big0, sizes0, band0 = big_blocks(loop_old, A, NF, bw)
    big1, sizes1, band1 = big_blocks(loop_new, A, NF, bw)

    # Hausdorff：平滑后每点到「原始折线」的最近距离（原折线密采样近似连续曲线）
    Pdense = resample(P0, 0.08, closed)
    tree = cKDTree(Pdense)
    hd, _ = tree.query(P1)
    hd_max, hd_med = float(np.max(hd)), float(np.median(hd))

    tb = turning_angle(Pbase, closed)
    ta = turning_angle(resample(P1, args.ds, closed), closed)
    lb, la = polyline_len(Pbase, closed), polyline_len(resample(P1, args.ds, closed), closed)
    reduction = 100.0 * (tb - ta) / tb if tb > 1e-9 else 0.0

    warn = []
    if big1 != 2:
        warn.append(f"平滑后大块数={big1}（期望 2），闭环被破坏，不得直接拿去切")
    if hd_max > args.max_dev:
        warn.append(f"Hausdorff {hd_max:.3f} mm > 阈值 {args.max_dev} mm，平滑可能改变了设计意图")
    if n_broken:
        warn.append(f"有 {n_broken} 处面序列断点未能补齐，闭环可能漏")
    if args.iters == 0:
        warn.append("iters=0：本次只做重采样+投影，未做 Taubin 平滑（对照运行）")
    verdict = "通过：平滑后闭环仍成立，偏差在阈值内" if not warn else "告警：" + "；".join(warn)

    rep = dict(
        source_json=os.path.basename(src), schema_in=d.get("schema"),
        params=dict(ds_mm=args.ds, iters=args.iters, lam=args.lam, mu=args.mu,
                    max_dev_mm=args.max_dev, point_repr="edge-mid (v2) / face-centroid (v1)"),
        n_input_pts=n0, n_resampled_pts=int(len(Pbase)), n_output_pts=int(len(P1)),
        metrics=dict(
            turning_before_deg=round(tb, 2), turning_after_deg=round(ta, 2),
            turning_reduction_pct=round(reduction, 2),
            len_before_mm=round(lb, 3), len_after_mm=round(la, 3),
            turning_per_mm_before=round(tb / max(lb, 1e-9), 2),
            turning_per_mm_after=round(ta / max(la, 1e-9), 2),
            max_off_surface_mm=round(max_off, 4),
            mean_projection_dist_mm=round(float(np.mean(dist2)), 4),
            hausdorff_max_mm=round(hd_max, 4), hausdorff_median_mm=round(hd_med, 4)),
        band_faces_before=band0, band_faces_after=band1,
        n_candidate_faces=int(len(cand)), projection_scope="user_faces 3-ring neighborhood",
        user_faces_before=len(d.get("user_faces", [])), user_faces_after=len(faces),
        n_patched_faces=int(n_patch), n_unpatched_gaps=int(n_broken),
        big_blocks_before=big0, big_blocks_after=big1,
        big_sizes_before=sizes0, big_sizes_after=sizes1,
        warnings=warn, verdict=verdict,
        caveats=[
            "平滑对象是面心/边中点折线的三角尺度锯齿，不是把用户画的直线掰弯",
            "anchors 与 bulge 未改动；agent 补全段未平滑（它是几何外推）",
            "前后都重采样到同一弧长再算转折角，避免点数差异造成假改善",
            "闭环用 split_from_drawing.py 同口径 connected_components 复验",
            "不评判缝的语义合理性 / 可装配性 / 可打印性"],
        elapsed_sec=round(time.time() - t0, 1))

    # refined JSON：原文件不覆盖，另存
    d2 = dict(d)
    d2["schema"] = "seam_draw/v2+refined"
    d2["loop_faces"] = loop_new
    d2["user_faces"] = faces
    d2["refine"] = dict(source=os.path.basename(src), report="refine_report.json",
                        params=rep["params"], metrics=rep["metrics"],
                        verdict=verdict, warnings=warn)
    # segments 的 pts 换成平滑后的对应片段（按弧长比例切回）；faces 保持不动，
    # 因为后台切分只认 loop_faces，这里只更新可视化用的点。
    d2["segments"] = [dict(s, pts_refined_note="见 refine.segments_pts") for s in d.get("segments", [])]
    d2["refine"]["segments_pts"] = [np.round(x, 4).tolist()
                                    for x in np.array_split(P1, max(1, len(d.get("segments", [])) or 1))]
    p_ref = os.path.join(out, stem + "_refined.json")
    json.dump(d2, open(p_ref, "w", encoding="utf-8"), ensure_ascii=False)
    p_rep = os.path.join(out, "refine_report.json")
    json.dump(rep, open(p_rep, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    p_png = draw_comparison(Pbase, resample(P1, args.ds, closed), rep,
                            os.path.join(out, "smooth_comparison.png"))

    print(f"[{'OK' if not warn else 'WARN'}] {verdict}")
    print(f"     转折角 {tb:.0f}° → {ta:.0f}° ({reduction:+.1f}%) | "
          f"弧长 {lb:.2f} → {la:.2f} mm | Hausdorff max {hd_max:.3f} mm")
    print(f"     面带 {band0} → {band1} | 大块 {big0} → {big1} | "
          f"补断点 {n_patch} / 未补上 {n_broken}")
    print(f"     {p_ref}\n     {p_rep}\n     {p_png}  ({rep['elapsed_sec']}s)")
    return 0 if not warn else 2


if __name__ == "__main__":
    sys.exit(main())
