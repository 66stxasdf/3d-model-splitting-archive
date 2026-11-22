"""从 GLB 抽样渲染带贴图的三角形，用 Python 验证 UV 是否正确。
用法: python tools/uv_check.py model.glb out.png [--faces 60000]
"""
import argparse
import io
import json
import struct

import numpy as np
from PIL import Image, ImageDraw


def load_glb(path):
    data = open(path, "rb").read()
    jl, _ = struct.unpack("<II", data[12:20])
    g = json.loads(data[20:20 + jl].decode("utf-8"))
    bin_off = 20 + jl
    bl, _ = struct.unpack("<II", data[bin_off:bin_off + 8])
    chunk = data[bin_off + 8:bin_off + 8 + bl]
    return g, chunk


def read_attr(g, chunk, accessor_index):
    a = g["accessors"][accessor_index]
    bv = g["bufferViews"][a["bufferView"]]
    off = bv.get("byteOffset", 0)
    comp = {5120: "i1", 5121: "u1", 5122: "i2", 5123: "u2", 5125: "u4", 5126: "f4"}[a["componentType"]]
    ncomp = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[a["type"]]
    arr = np.frombuffer(chunk[off:off + bv["byteLength"]], dtype=np.dtype(comp))
    arr = arr.reshape(a["count"], ncomp)
    # 归一化类型转 float
    if a["componentType"] in (5120, 5121, 5122, 5123):
        arr = arr.astype(np.float32)
        if "normalized" in a and a["normalized"]:
            arr = arr / 255.0 if a["componentType"] in (5121,) else arr / 32767.0
    return arr.astype(np.float32)


def sample_texture(g, chunk, texture_index, xy, tex_cache=None):
    """最近邻采样贴图。xy: (N,2) UV。返回 (N,3) uint8"""
    if tex_cache is not None:
        arr = tex_cache
    else:
        src = g["textures"][texture_index]["source"]
        img_info = g["images"][src]
        bv = g["bufferViews"][img_info["bufferView"]]
        off = bv.get("byteOffset", 0)
        im = Image.open(io.BytesIO(chunk[off:off + bv["byteLength"]])).convert("RGB")
        im.thumbnail((1024, 1024))
        arr = np.asarray(im)
    h, w = arr.shape[:2]
    x = np.clip((xy[:, 0] % 1.0 * w).astype(int), 0, w - 1)
    y = np.clip(((1.0 - xy[:, 1] % 1.0) * h).astype(int), 0, h - 1)  # glTF V 翻转
    return arr[y, x]


def render(g, chunk, out_path, n_faces, tex_path=None):
    prim = g["meshes"][0]["primitives"][0]
    V = read_attr(g, chunk, prim["attributes"]["POSITION"])
    UV = read_attr(g, chunk, prim["attributes"]["TEXCOORD_0"])
    F = read_attr(g, chunk, prim["indices"]).reshape(-1, 3).astype(np.int64)
    tex_i = g["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"]["index"]

    tex_cache = None
    if tex_path:
        tex_cache = np.asarray(Image.open(tex_path).convert("RGB"))
        print("tex override:", tex_path, tex_cache.shape)
    # 数据已提取，释放大缓冲引用（GLB bytes 可能上百 MB）
    del g, chunk
    print(f"V {V.shape} UV {UV.shape} F {F.shape}")
    print(f"UV range: {UV.min(0).round(4)} .. {UV.max(0).round(4)}")

    rng = np.random.default_rng(1)
    sel = rng.choice(len(F), size=min(n_faces, len(F)), replace=False)
    F = F[sel]

    tris = V[F]                      # (n,3,3)
    uvs = UV[F]                      # (n,3,2)
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    n /= (np.linalg.norm(n, axis=1, keepdims=True) + 1e-12)

    # 屏幕投影（绕 Y 轴，Z-up 模型直接画 x/z）
    center = V.mean(0)
    yaw = -0.5
    cy, sy = np.cos(yaw), np.sin(yaw)
    R = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    P = (V - center) @ R.T
    T = (tris - center) @ R.T
    order = np.argsort(-T[:, :, 1].mean(axis=1))

    S = 760
    xs, zs = P[:, 0], P[:, 2]
    span = max(xs.max() - xs.min(), zs.max() - zs.min()) * 1.1
    cx, cz = (xs.max() + xs.min()) / 2, (zs.max() + zs.min()) / 2

    img = Image.new("RGB", (S, S), (245, 246, 248))

    light = np.array([0.4, -0.7, 0.6]); light /= np.linalg.norm(light)
    for i in order:
        t = T[i]
        px = (t[:, 0] - cx) / span * S + S / 2
        py = S / 2 - (t[:, 2] - cz) / span * S
        if (px.max() < 0 or px.min() > S or py.max() < 0 or py.min() > S):
            continue
        rgb = sample_texture(None, None, tex_i, uvs[i], tex_cache=tex_cache).mean(axis=0)
        lam = abs(float(np.dot(n[i], light)))
        c = tuple(int(v) for v in (rgb.astype(float) * (0.35 + 0.75 * lam)).clip(0, 255))
        ImageDraw.Draw(img).polygon([(px[0], py[0]), (px[1], py[1]), (px[2], py[2])], fill=c)

    img.save(out_path)
    print("saved", out_path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("out")
    ap.add_argument("--faces", type=int, default=60000)
    ap.add_argument("--tex", default=None)
    a = ap.parse_args()
    g, chunk = load_glb(a.model)
    render(g, chunk, a.out, a.faces, tex_path=a.tex)
