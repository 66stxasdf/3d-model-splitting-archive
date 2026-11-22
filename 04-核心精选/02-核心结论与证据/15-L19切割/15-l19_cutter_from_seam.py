#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""由「画线器导出的闭环割线 JSON」构造曲面切割体（不依赖预设曲面）。

输入：任意含 loop_points / loop_faces、closed=true 的 seam_draw/v* JSON。
做法：闭环点投影 XY -> 大圆外框 -> 2D Delaunay 三角化 -> 高度场膜 ->
      凸包侧壁 + 顶盖挤出成封闭实体 -> 水密/流形自检。
输出：L19_cutter_from_seam.stl，交给 l19_curved_cut.py 做布尔。
"""
import json
import os
import struct
import sys

import numpy as np
import trimesh
from scipy.spatial import Delaunay

HERE = os.path.dirname(os.path.abspath(__file__))
SEAM = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "demo_seam_curved.json")
OUT = os.path.join(HERE, "L19_cutter_from_seam.stl")

d = json.load(open(SEAM, encoding="utf-8"))
assert d.get("closed"), "割线必须闭合"
loop = np.asarray(d.get("loop_points") or d["strokes"][0]["pts"], dtype=np.float64)
assert len(loop) >= 12, "闭环点不足"
print("SEAM", os.path.basename(SEAM), "loop_points", len(loop), flush=True)

cent = loop.mean(0)
_, _, vh = np.linalg.svd(loop - cent, full_matrices=False)
n = vh[-1]
if n[2] < 0:
    n = -n
rms = float(np.sqrt((((loop - cent) @ n) ** 2).mean()))
plane_z = lambda x, y: cent[2] - (n[0] * (x - cent[0]) + n[1] * (y - cent[1])) / n[2]
print("COPLANAR RMS", round(rms, 4), "mm", flush=True)

R = loop[:, :2]
qc = R.mean(0)
RZ = loop[:, 2].copy()
if np.linalg.norm(R[0] - R[-1]) < 1e-6:
    R, RZ = R[:-1], RZ[:-1]
NR = len(R)

M = 52
th = np.linspace(0, 2 * np.pi, M, endpoint=False)
OUTXY = np.column_stack([qc[0] + 300 * np.cos(th), qc[1] + 300 * np.sin(th)])
OUTZ = np.array([plane_z(x, y) for x, y in OUTXY])
pts2 = np.vstack([R, OUTXY])
pts_z = np.concatenate([RZ, OUTZ])
mem = Delaunay(pts2).simplices.astype(np.int64)
verts = np.column_stack([pts2, pts_z])

TOP = float(max(verts[:, 2].max(), 101.2)) + 260.0
top_ring = np.column_stack([OUTXY, np.full(M, TOP)])
top_center = np.array([[qc[0], qc[1], TOP]])
allv = np.vstack([verts, top_ring, top_center])
toff, tci = len(verts), len(allv) - 1
side, lid = [], []
for i in range(M):
    j = (i + 1) % M
    a, b = NR + i, NR + j
    side += [(a, b, toff + j), (a, toff + j, toff + i)]
    lid.append((toff + i, tci, toff + j))
allf = np.vstack([mem, np.asarray(side + lid, dtype=np.int64)])

cutter = trimesh.Trimesh(vertices=allv, faces=allf, process=True)
cutter.fix_normals()
e = cutter.edges_unique
cnt = np.bincount(cutter.edges_unique_inverse, minlength=len(e))
info = dict(vertices=int(len(cutter.vertices)), faces=int(len(cutter.faces)),
            watertight=bool(cutter.is_watertight), winding=bool(cutter.is_winding_consistent),
            boundary_edges=int((cnt == 1).sum()), nonmanifold_edges=int((cnt > 2).sum()),
            components=len(cutter.split(only_watertight=False)),
            volume=float(cutter.volume), bounds=cutter.bounds.round(3).tolist())


def write_stl(path, m, name):
    v = np.asarray(m.vertices, np.float64)
    f = np.asarray(m.faces, np.int64)
    t = v[f].astype(np.float32)
    nr = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    ln = np.linalg.norm(nr, axis=1)
    ln[ln == 0] = 1.0
    nr /= ln[:, None]
    dt = np.dtype([("n", "<f4", (3,)), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec = np.zeros(len(f), dtype=dt)
    rec["n"], rec["v"] = nr, t
    hdr = (name.encode("ascii", "replace") + b" " * 80)[:80]
    with open(path, "wb") as fh:
        fh.write(hdr)
        fh.write(struct.pack("<I", len(f)))
        fh.write(rec.tobytes())
    return os.path.getsize(path)


size = write_stl(OUT, cutter, "cutter_from_seam")
usable = bool(info["watertight"] and info["winding"] and
              info["nonmanifold_edges"] == 0 and info["components"] == 1)
rep = dict(source_seam=os.path.basename(SEAM), loop_points=len(loop),
           coplanar_rms_mm=rms, plane_normal=[float(x) for x in n],
           delaunay_triangles=len(mem), outer_ring_points=M,
           cutter=dict(stl=OUT, bytes=size, **info), usable=usable,
           note="cutter 完全由闭环点构造，不含任何预设曲面方程")
json.dump(rep, open(os.path.join(HERE, "l19_cutter_from_seam_report.json"), "w",
                    encoding="utf-8"), ensure_ascii=False, indent=2)
print("CUTTER", json.dumps(info, ensure_ascii=False), flush=True)
print("USABLE", usable, "bytes", size, flush=True)
sys.exit(0 if usable else 1)
