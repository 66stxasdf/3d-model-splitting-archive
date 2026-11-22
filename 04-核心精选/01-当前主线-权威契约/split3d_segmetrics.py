# -*- coding: utf-8 -*-
"""语义分件质量的表面度量（阶段 1 A 线评估器的算 core）。

为什么用表面采样 + 最近件归属，而不是体素 IoU / 布尔：
真值集里大量件是「主壳 + 游离碎面」的开放壳（BOLT01 水密仅 6/11、MK02 仅 9/37），
体素化与布尔都要求封闭，对开放壳要么直接失败、要么静默给出错的值。
表面采样只需要三角面，对非水密网格同样成立；而且它量的正是
「件与件的边界落在哪」——这恰恰是语义分件质量的本质，边界对了件就对了。

度量定义（匹配对 ref_i / tool_j，双向一致）：
    a = ref_i 表面采样点中，最近工具件是 tool_j 的比例
    b = tool_j 表面采样点中，最近真值件是 ref_i 的比例
    g = (a + b) / 2                      双向一致度 ∈ [0,1]
    IoU = g / (2 - g)                    完全一致 → 1；毫不相干 → 0；各半 → 1/3
边界偏差 = 匹配对之间的双向 chamfer 距离（mean / p95 / max，mm）

确定性：采样种子与点数固定；同一机器、同样输入，必得同样数字。
匹配：scipy 线性分配最大化总 IoU，低于 match_floor 的对子不计匹配
（记漏分 / 多分）。floor 是算法参数（const.semantic_match_floor），
「多少算可采信」的合格线是另一回事，由人声明（任务单 min_iou），不在这里。

**用法禁忌（实测结论，勿忘）**：单对比较（1 个真值件 vs 1 个工具件）恒为 IoU=1，
没有分辨力——只有一个候选时「最近件」必然是它。度量只在**多件矩阵**里有意义：
错分通过「该匹配的对子 IoU 下降 + 漏分 / 多分计数」暴露，不会伪装成高分。
"""

import numpy as np

DEFAULT_SAMPLES = 4000
DEFAULT_MATCH_FLOOR = 0.5


def sample(mesh, n=DEFAULT_SAMPLES, seed=0):
    """在网格表面按面积加权采 n 个点。空网格返回 (0,3) 不炸。"""
    import trimesh
    if mesh is None or len(mesh.faces) == 0:
        return np.zeros((0, 3), dtype=float)
    pts, _ = trimesh.sample.sample_surface(mesh, int(n), seed=seed)
    return np.asarray(pts, dtype=float)


def _safe_div_rows(A, denom):
    """A[i, :] / denom[i]；0 分母的行保持 0（不产 nan——nan 会静默污染判定）。"""
    out = np.zeros_like(A, dtype=float)
    ok = denom > 0
    out[ok, :] = A[ok, :] / denom[ok, None]
    return out


def _safe_div_cols(A, denom):
    """A[:, j] / denom[j]；0 分母的列保持 0。"""
    out = np.zeros_like(A, dtype=float)
    ok = denom > 0
    out[:, ok] = A[:, ok] / denom[ok][None, :]
    return out


def agreement_matrix(ref_meshes, tool_meshes, n=DEFAULT_SAMPLES, seed=0):
    """返回 (n_ref × n_tool) 的表面 IoU 矩阵。任一为空 → 全零矩阵。"""
    ref_pts = [sample(m, n, seed) for m in ref_meshes]
    tool_pts = [sample(m, n, seed) for m in tool_meshes]
    nref, ntool = len(ref_pts), len(tool_pts)
    iou = np.zeros((nref, ntool), dtype=float)
    if nref == 0 or ntool == 0:
        return iou
    ref_all = np.vstack(ref_pts)
    tool_all = np.vstack(tool_pts)
    ref_len = np.array([len(p) for p in ref_pts], dtype=float)
    tool_len = np.array([len(p) for p in tool_pts], dtype=float)
    if ref_len.sum() <= 0 or tool_len.sum() <= 0:
        return iou

    ref_owner = np.concatenate([np.full(int(l), i, dtype=int)
                                for i, l in enumerate(ref_len)])
    tool_owner = np.concatenate([np.full(int(l), j, dtype=int)
                                 for j, l in enumerate(tool_len)])

    from scipy.spatial import cKDTree
    _, near_tool = cKDTree(tool_all).query(ref_all, k=1)
    A = np.zeros((nref, ntool), dtype=float)
    np.add.at(A, (ref_owner, tool_owner[near_tool]), 1.0)
    a = _safe_div_rows(A, ref_len)

    _, near_ref = cKDTree(ref_all).query(tool_all, k=1)
    B = np.zeros((nref, ntool), dtype=float)
    np.add.at(B, (ref_owner[near_ref], tool_owner), 1.0)
    b = _safe_div_cols(B, tool_len)

    g = np.clip((a + b) / 2.0, 0.0, 1.0)
    return g / (2.0 - g)


def pair_chamfer(ref_mesh, tool_mesh, n=DEFAULT_SAMPLES, seed=0):
    """匹配对的双向 chamfer：mean / p95 / max，单位 mm。空网格 → inf。"""
    r = sample(ref_mesh, n, seed)
    t = sample(tool_mesh, n, seed)
    if len(r) == 0 or len(t) == 0:
        return dict(mean_mm=float("inf"), p95_mm=float("inf"), max_mm=float("inf"))
    from scipy.spatial import cKDTree
    d1, _ = cKDTree(t).query(r, k=1)
    d2, _ = cKDTree(r).query(t, k=1)
    d = np.concatenate([np.atleast_1d(d1), np.atleast_1d(d2)])
    return dict(mean_mm=float(d.mean()),
                p95_mm=float(np.percentile(d, 95)),
                max_mm=float(d.max()))


def match(iou, floor=DEFAULT_MATCH_FLOOR):
    """匈牙利匹配 + floor 过滤。返回 (pairs, missed_ref_idx, extra_tool_idx)。"""
    iou = np.asarray(iou, dtype=float)
    nref, ntool = iou.shape
    if nref == 0 or ntool == 0:
        return [], list(range(nref)), list(range(ntool))
    from scipy.optimize import linear_sum_assignment
    rows, cols = linear_sum_assignment(-iou)
    pairs, mr, mt = [], set(), set()
    for i, j in zip(rows, cols):
        if float(iou[i, j]) >= floor:
            pairs.append((int(i), int(j), float(iou[i, j])))
            mr.add(int(i))
            mt.add(int(j))
    return (pairs,
            [i for i in range(nref) if i not in mr],
            [j for j in range(ntool) if j not in mt])
