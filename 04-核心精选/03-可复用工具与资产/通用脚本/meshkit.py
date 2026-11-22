#!/usr/bin/env python
"""
meshkit - 本地 3D 资产流水线（借鉴混元3D平台的能力设计，纯本地实现，不调用其私有接口）

子命令:
  prep    多视角参考图切分 + 背景去除 + 生成 views.json（含 ViewType 标注）
  verify  网格/材质体检：面数、水密、亏格、边界环、体积、纹理、UV 密度、金属度
  fix     修复：剔碎片、封洞、法线朝外、按高度归一、导出 STL/OBJ/GLB
  remesh  减面 / 四边面重建（需要 pymeshlab）
  viewer  生成 model-viewer 交互预览页
  manifest 记录一次生成任务（输入哈希+参数+任务号），保证可复现

用法示例:
  python meshkit.py verify --mesh model.glb
  python meshkit.py fix --mesh model.glb --out model_print.stl --height 100
  python meshkit.py remesh --mesh model_print.stl --out model_quad.obj --target 200000 --quads
  python meshkit.py viewer --mesh model.glb --out viewer.html
"""
import argparse
import base64
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile

import numpy as np


# ---------------------------------------------------------------- helpers
def _load_trimesh():
    try:
        import trimesh  # noqa: F401
        return trimesh
    except ImportError:
        sys.exit("[meshkit] 需要 trimesh: pip install trimesh")


def _pymeshlab_python():
    """找到能 import pymeshlab 的解释器；可用 MESHKIT_MLAB_PY 覆盖。"""
    env = os.environ.get("MESHKIT_MLAB_PY")
    if env and os.path.exists(env):
        return env
    candidates = [sys.executable, r"D:\Mangshe\python.exe"]
    for c in candidates:
        if not c or not os.path.exists(c):
            continue
        try:
            r = subprocess.run([c, "-c", "import pymeshlab"], capture_output=True, timeout=60)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return None


def _glb_texture_info(path):
    """从 GLB 头解析贴图数量/字节数/材质通道，失败返回 None。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
        if data[:4] != b"glTF":
            return None
        json_len, _ = struct.unpack("<II", data[12:20])
        g = json.loads(data[20:20 + json_len].decode("utf-8"))
        images = []
        for img in g.get("images", []):
            bv = g["bufferViews"][img["bufferView"]]
            images.append({"mime": img.get("mimeType"), "bytes": bv["byteLength"]})
        mats = []
        for mat in g.get("materials", []):
            pbr = mat.get("pbrMetallicRoughness", {})
            mats.append({
                "name": mat.get("name"),
                "baseColor": "baseColorTexture" in pbr,
                "metallicRoughness": "metallicRoughnessTexture" in pbr,
                "normal": "normalTexture" in mat,
                "occlusion": "occlusionTexture" in mat,
                "metallicFactor": pbr.get("metallicFactor"),
            })
        return {"images": images, "materials": mats}
    except Exception:
        return None


def _uv_density(m):
    """UV 面积占比 vs 3D 面积占比，衡量贴图分配是否均匀（越接近1越均衡）。"""
    try:
        uv = m.visual.uv
        if uv is None:
            return None
        tri_uv = uv[m.faces]
        a_uv = 0.5 * np.abs(
            (tri_uv[:, 1, 0] - tri_uv[:, 0, 0]) * (tri_uv[:, 2, 1] - tri_uv[:, 0, 1])
            - (tri_uv[:, 2, 0] - tri_uv[:, 0, 0]) * (tri_uv[:, 1, 1] - tri_uv[:, 0, 1])
        )
        a_3d = m.area_faces
        if a_uv.sum() <= 0 or a_3d.sum() <= 0:
            return None
        ratio = (a_uv / a_uv.sum()) / (a_3d / a_3d.sum())
        return {
            "uv_area_total": float(a_uv.sum()),
            "density_p05": float(np.percentile(ratio, 5)),
            "density_p95": float(np.percentile(ratio, 95)),
            "density_median": float(np.median(ratio)),
            "uneven_faces_pct": float((ratio > 4).mean() * 100 + (ratio < 0.25).mean() * 100),
        }
    except Exception:
        return None


def _boundary_loops(m):
    edges = m.edges_sorted
    uniq, counts = np.unique(edges, axis=0, return_counts=True)
    border = uniq[counts == 1]
    if len(border) == 0:
        return 0, 0
    adj = {}
    for a, b in border:
        adj.setdefault(int(a), []).append(int(b))
        adj.setdefault(int(b), []).append(int(a))
    seen, loops = set(), 0
    for v in adj:
        if v in seen:
            continue
        loops += 1
        stack = [v]
        seen.add(v)
        while stack:
            cur = stack.pop()
            for nxt in adj[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
    return len(border), loops


# ---------------------------------------------------------------- prep
def cmd_prep(args):
    from PIL import Image
    try:
        import scipy.ndimage as ndi
    except ImportError:
        sys.exit("[meshkit] prep 需要 scipy: pip install scipy")

    src = Image.open(args.input).convert("RGB")
    arr = np.asarray(src).astype(int)
    w, h = src.size
    corner = np.concatenate([
        arr[:30, :30].reshape(-1, 3), arr[:30, -30:].reshape(-1, 3),
        arr[-30:, :30].reshape(-1, 3), arr[-30:, -30:].reshape(-1, 3),
    ])
    bg = corner.mean(axis=0)
    mask = (np.abs(arr - bg).sum(axis=2) > 60)

    # 按列占用找视角分界（取谷底，避免相邻视角粘连）
    col = mask.mean(axis=0)
    n = args.views
    cuts = [0]
    for k in range(1, n):
        guess = int(w * k / n)
        lo, hi = max(1, guess - int(w / n * 0.45)), min(w - 1, guess + int(w / n * 0.45))
        if hi - lo < 5:
            cuts.append(guess)
            continue
        cuts.append(lo + int(np.argmin(col[lo:hi])))
    cuts.append(w)

    os.makedirs(args.out, exist_ok=True)
    labels = args.labels.split(",") if args.labels else []
    views = []
    for i in range(n):
        x0, x1 = cuts[i], cuts[i + 1]
        crop = src.crop((x0, 0, x1, h))
        a = np.asarray(crop).astype(int)
        dist = np.abs(a - bg).sum(axis=2)
        near_bg = dist < 110
        lab, _ = ndi.label(near_bg)
        border = set(lab[0, :]) | set(lab[-1, :]) | set(lab[:, 0]) | set(lab[:, -1])
        border.discard(0)
        bgmask = np.isin(lab, list(border))
        soft = ndi.gaussian_filter(bgmask.astype(float), 1.2)
        alpha = np.clip((1 - soft) * 255, 0, 255)
        alpha[dist > 150] = 255
        white = np.full_like(a, 255)
        al = (alpha / 255.0)[:, :, None]
        out = (a * al + white * (1 - al)).astype(np.uint8)
        img = Image.fromarray(out)
        mm = alpha > 40
        rows = np.where(mm.mean(axis=1) > 0.01)[0]
        cols = np.where(mm.mean(axis=0) > 0.01)[0]
        if len(rows) and len(cols):
            img = img.crop((cols.min(), rows.min(), cols.max() + 1, rows.max() + 1))
        cw, ch = img.size
        side = max(cw, ch) + 24
        canvas = Image.new("RGB", (side, side), (255, 255, 255))
        canvas.paste(img, ((side - cw) // 2, (side - ch) // 2))
        name = labels[i] if i < len(labels) else f"view{i}"
        path = os.path.join(args.out, f"{name}.png")
        canvas.save(path)
        views.append({"name": name, "path": path, "size": canvas.size, "x_range": [x0, x1]})
        print(f"[prep] {name}: {canvas.size} -> {path}")

    meta = {
        "source": os.path.abspath(args.input),
        "source_sha1": hashlib.sha1(open(args.input, "rb").read()).hexdigest(),
        "background_rgb": [round(float(x), 1) for x in bg],
        "cuts": cuts,
        "views": views,
        "note": "ViewType 建议: front/left/right/back/top/bottom/left_front/right_front",
    }
    mpath = os.path.join(args.out, "views.json")
    json.dump(meta, open(mpath, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[prep] wrote {mpath}")


# ---------------------------------------------------------------- verify
def cmd_verify(args):
    trimesh = _load_trimesh()
    for path in args.mesh:
        m = trimesh.load(path, force="mesh")
        print(f"\n== {os.path.basename(path)}  ({os.path.getsize(path)/1e6:.1f} MB)")
        print(f"   faces {len(m.faces):,} | verts {len(m.vertices):,}")

        # GLB/OBJ 在 UV 接缝与外法线分割处会拆分顶点，直接判水密会误报。
        # 先做"缝合"（保留属性合并）再判拓扑，才是真实几何状态。
        welded = m.copy()
        welded.merge_vertices(merge_tex=True, merge_norm=True)
        seam_edges, seam_loops = _boundary_loops(m)
        real_edges, real_loops = _boundary_loops(welded)
        print(f"   缝合后 faces {len(welded.faces):,} | verts {len(welded.vertices):,} "
              f"(合并 {len(m.vertices) - len(welded.vertices):,} 个接缝重复点)")
        print(f"   watertight {welded.is_watertight} | winding_ok {welded.is_winding_consistent} "
              f"| euler {welded.euler_number}")
        if real_edges:
            print(f"   真实开放边 {real_edges} 条 / 边界环 {real_loops} 个  <-- 需要补洞")
        elif seam_edges:
            print(f"   原始统计的 {seam_edges} 条开放边 / {seam_loops} 个环 均为 UV 接缝，几何本身闭合")

        # 非流形边（被 >2 个面共享）：四边面转换、布尔运算后的典型副产物
        edge_key = welded.edges_sorted
        uniq, cnt = np.unique(edge_key, axis=0, return_counts=True)
        nonmanifold = int((cnt > 2).sum())
        if nonmanifold:
            pct = nonmanifold / max(1, len(uniq)) * 100
            print(f"   非流形边 {nonmanifold:,} 条 ({pct:.2f}%)  <-- 编辑/布尔前建议清理")

        print(f"   extents {np.round(welded.extents, 3).tolist()} | volume {float(welded.volume):.4f}")
        degenerate = int((welded.area_faces < 1e-12).sum())
        if degenerate:
            print(f"   退化面 {degenerate} 个")
        info = _glb_texture_info(path)
        if info:
            for img in info["images"]:
                print(f"   texture {img['mime']} {img['bytes']/1e6:.1f} MB")
            for mat in info["materials"]:
                flags = [k for k in ("baseColor", "metallicRoughness", "normal", "occlusion") if mat[k]]
                print(f"   material {mat['name']}: {flags} metallicFactor={mat['metallicFactor']}")
                if mat["metallicFactor"] and mat["metallicFactor"] > 0.2:
                    print("   [warn] 金属度偏高，白色/布料材质会发灰，建议置 0")
        uv = _uv_density(m)
        if uv:
            print(f"   UV: 覆盖 {uv['uv_area_total']:.3f} | 密度中位 {uv['density_median']:.2f} "
                  f"| p05-p95 {uv['density_p05']:.2f}-{uv['density_p95']:.2f} "
                  f"| 不均衡面 {uv['uneven_faces_pct']:.1f}%")


# ---------------------------------------------------------------- fix
def cmd_fix(args):
    trimesh = _load_trimesh()
    m = trimesh.load(args.mesh, force="mesh")
    print(f"[fix] 载入 {len(m.faces):,} 面, watertight={m.is_watertight}")

    # 几何修复不需要贴图；先丢弃材质避免大图深拷贝爆内存
    try:
        m.visual = None
    except Exception:
        pass
    # 关键：GLB/OBJ 在 UV 接缝处顶点重复，不缝合会把一个整体切成上千个"碎块"，
    # 后续连通域筛选会误删 99% 的面。必须先焊点。
    before = len(m.vertices)
    m.merge_vertices()
    if before != len(m.vertices):
        print(f"[fix] 焊接接缝顶点 {before - len(m.vertices):,} 个 -> {len(m.vertices):,}")

    parts = None
    # 注意：不要用 m.split()，它会深拷贝材质/贴图，几十 MB 的 GLB 会直接 MemoryError。
    # 这里只按连通域标签筛面，导出几何用（贴图不参与）。
    try:
        import trimesh.graph as tgraph
        labels = tgraph.connected_component_labels(m.face_adjacency, node_count=len(m.faces))
        uniq, counts = np.unique(labels, return_counts=True)
        if len(uniq) > 1:
            keep = uniq[np.argmax(counts)]
            sel = labels == keep
            dropped = len(uniq) - 1
            m = trimesh.Trimesh(vertices=m.vertices, faces=m.faces[sel], process=False)
            print(f"[fix] 剔除 {dropped} 个游离组件，保留主网格 {len(m.faces):,} 面")
    except Exception as e:
        print(f"[fix] 组件拆分跳过（{type(e).__name__}）")

    if not m.is_watertight:
        trimesh.repair.fill_holes(m)
        print(f"[fix] 补洞后 watertight={m.is_watertight}")
    trimesh.repair.fix_normals(m)

    if args.height:
        # glTF 约定 Y 轴朝上，STL/切片器约定 Z 轴朝上；先把最长轴（站立高度）转到 Z。
        ext = m.extents
        up_axis = int(np.argmax(ext))
        if up_axis == 1:
            import trimesh.transformations as tf
            m.apply_transform(tf.rotation_matrix(np.pi / 2, [1, 0, 0]))
            print("[fix] Y-up -> Z-up（旋转 +90° 绕 X）")
        elif up_axis == 0:
            import trimesh.transformations as tf
            m.apply_transform(tf.rotation_matrix(np.pi / 2, [0, 1, 0]))
            print("[fix] X-up -> Z-up（旋转 90° 绕 Y）")
        s = args.height / float(m.extents[2])
        m.apply_scale(s)
        print(f"[fix] 缩放 x{s:.4f} -> extents {np.round(m.extents, 1).tolist()}")

    if m.volume < 0:
        m.invert()
        print("[fix] 法线反向 -> 已翻转为朝外")

    out = args.out
    if args.decimate:
        out = _decimate(m, out, args.decimate, args.quads)
    else:
        m.export(out)
    print(f"[fix] 导出 {out}")

    chk = trimesh.load(out, force="mesh")
    print(f"[fix] 验收: faces {len(chk.faces):,} | watertight {chk.is_watertight} | "
          f"volume {float(chk.volume):.3f} | extents {np.round(chk.extents, 1).tolist()}")


def _decimate(m, out_path, target, quads):
    """用 pymeshlab 做减面/四边面重建（通过外部解释器）。"""
    py = _pymeshlab_python()
    if not py:
        print("[fix] 未找到 pymeshlab 解释器，跳过减面（可用 MESHKIT_MLAB_PY 指定）")
        m.export(out_path)
        return out_path

    tmp_in = os.path.join(tempfile.gettempdir(), "meshkit_in.obj")
    m.export(tmp_in)
    ext = out_path.lower().rsplit(".", 1)[-1]
    save_line = (f'ms.save_current_mesh(r"{out_path}", binary=True)'
                 if ext in ("stl", "ply") else
                 f'ms.save_current_mesh(r"{out_path}")')
    quad_line = ("ms.meshing_tri_to_quad_dominant(level='Better quad shape')"
                 if quads else "pass")
    code = f"""
import pymeshlab
ms = pymeshlab.MeshSet()
ms.load_new_mesh(r"{tmp_in}")
ms.meshing_decimation_quadric_edge_collapse(targetfacenum={target}, preservenormal=True)
ms.meshing_repair_non_manifold_edges()
ms.meshing_close_holes(maxholesize=30)
ms.meshing_re_orient_faces_coherently()
{quad_line}
{save_line}
m = ms.current_mesh()
print(f"[remesh] faces {{m.face_number():,}}")
"""
    r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=1200)
    print(r.stdout.strip() or r.stderr.strip()[-500:])
    return out_path


# ---------------------------------------------------------------- remesh
def cmd_remesh(args):
    trimesh = _load_trimesh()
    m = trimesh.load(args.mesh, force="mesh")
    print(f"[remesh] 输入 {len(m.faces):,} 面")
    _decimate(m, args.out, args.target, args.quads)
    chk = trimesh.load(args.out, force="mesh")
    print(f"[remesh] 输出 faces {len(chk.faces):,} | watertight {chk.is_watertight} | "
          f"extents {np.round(chk.extents, 1).tolist()}")


# ---------------------------------------------------------------- viewer
def cmd_viewer(args):
    mesh_name = os.path.basename(args.mesh)
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{args.title or mesh_name}</title>
<script type="module" src="https://unpkg.com/@google/model-viewer@3.5.0/dist/model-viewer.min.js"></script>
<style>
 body{{margin:0;font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#f5f6f8;color:#1f2328;
      display:flex;flex-direction:column;align-items:center;min-height:100vh}}
 h1{{font-size:20px;margin:24px 0 4px}} p.sub{{margin:0 0 16px;color:#57606a;font-size:13px}}
 model-viewer{{width:min(92vw,780px);height:min(70vh,640px);background:#fff;border-radius:16px;
      box-shadow:0 4px 24px rgba(0,0,0,.08)}}
 .tips{{margin:14px 0 24px;font-size:13px;color:#57606a}}
</style></head><body>
 <h1>{args.title or mesh_name}</h1>
 <p class="sub">{args.subtitle or ''}</p>
 <model-viewer src="{mesh_name}" camera-controls auto-rotate shadow-intensity="1"
   exposure="1" environment-image="neutral"></model-viewer>
 <div class="tips">左键拖动旋转 · 滚轮缩放 · 右键平移</div>
</body></html>"""
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[viewer] wrote {args.out} (引用同目录 {mesh_name})")


# ---------------------------------------------------------------- manifest
def cmd_manifest(args):
    record = {
        "job_id": args.job_id,
        "engine": args.engine,
        "params": json.loads(args.params) if args.params else {},
        "inputs": [],
        "outputs": args.outputs.split(",") if args.outputs else [],
        "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    for p in (args.input or []):
        record["inputs"].append({
            "path": os.path.abspath(p),
            "sha1": hashlib.sha1(open(p, "rb").read()).hexdigest(),
        })
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    existing = []
    if os.path.exists(args.out):
        try:
            existing = json.load(open(args.out, encoding="utf-8"))
        except Exception:
            existing = []
    existing.append(record)
    json.dump(existing, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[manifest] appended -> {args.out} (共 {len(existing)} 条)")


# ---------------------------------------------------------------- cli
def main():
    p = argparse.ArgumentParser(prog="meshkit", description="本地 3D 资产流水线")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("prep", help="多视角参考图切分+去背景")
    s.add_argument("--input", required=True)
    s.add_argument("--views", type=int, default=3)
    s.add_argument("--labels", default="", help="逗号分隔，如 front,left,back")
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_prep)

    s = sub.add_parser("verify", help="网格/材质体检")
    s.add_argument("--mesh", nargs="+", required=True)
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("fix", help="修复+归一+导出")
    s.add_argument("--mesh", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--height", type=float, default=None, help="归一化高度（mm）")
    s.add_argument("--decimate", type=int, default=None, help="目标面数")
    s.add_argument("--quads", action="store_true", help="减面后做四边面主导重建")
    s.set_defaults(func=cmd_fix)

    s = sub.add_parser("remesh", help="减面/四边面重建")
    s.add_argument("--mesh", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--target", type=int, required=True)
    s.add_argument("--quads", action="store_true")
    s.set_defaults(func=cmd_remesh)

    s = sub.add_parser("viewer", help="生成 3D 预览页")
    s.add_argument("--mesh", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--title", default=None)
    s.add_argument("--subtitle", default=None)
    s.set_defaults(func=cmd_viewer)

    s = sub.add_parser("manifest", help="记录生成任务，保证可复现")
    s.add_argument("--job-id", default=None)
    s.add_argument("--engine", default=None)
    s.add_argument("--params", default=None, help='JSON 字符串，如 {"pbr":true}')
    s.add_argument("--input", action="append")
    s.add_argument("--outputs", default=None, help="逗号分隔")
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_manifest)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
