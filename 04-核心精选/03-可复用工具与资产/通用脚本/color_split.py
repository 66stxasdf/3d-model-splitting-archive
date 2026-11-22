# -*- coding: utf-8 -*-
"""
按颜色分件（3D 彩色打印 / 分材料工艺）:
  1. 从参考 GLB(带 UV+贴图) 采样每个顶点的颜色
  2. k-means 聚成材料色板
  3. 对齐(自动找旋转+缩放)后把颜色标签传到水密打印实体(STL, mm)
  4. 标签平滑 + 小岛合并
  5. 每个颜色区域生成闭合实体: 外壳 + 内壳(沿顶点法线内缩 wall mm) + 侧壁
  6. 导出每色一个 STL + manifest + 纯色拼装预览 GLB
"""
import argparse, io, json, os, struct, time
import numpy as np
from PIL import Image, ImageFilter
from scipy.spatial import cKDTree
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

GLB_MAG, GLB_VER, CHUNK_JSON, CHUNK_BIN = 0x46546C67, 2, 0x4E4F534A, 0x004E4942

# ---------------- glTF 基础 ----------------
def read_glb(path):
    data = open(path, 'rb').read()
    jl, _ = struct.unpack('<II', data[12:20])
    g = json.loads(data[20:20 + jl].decode())
    bo = 20 + jl
    bl, _ = struct.unpack('<II', data[bo:bo + 8])
    return g, data[bo + 8:bo + 8 + bl]

def read_acc(g, chunk, idx):
    a = g['accessors'][idx]
    b = g['bufferViews'][a['bufferView']]
    off = b.get('byteOffset', 0) + a.get('byteOffset', 0)
    n = a['count']
    ctype = {'VEC3': 3, 'VEC2': 2, 'SCALAR': 1}[a['type']]
    dt = {5126: np.float32, 5123: np.uint16, 5125: np.uint32,
          5121: np.uint8, 5122: np.int16, 5120: np.int8}[a['componentType']]
    itemsize = np.dtype(dt).itemsize
    stride = b.get('byteStride') or itemsize * ctype
    flat = np.frombuffer(chunk, dtype=dt, offset=off)
    if stride == itemsize * ctype:
        out = flat[:n * ctype].reshape(n, ctype).astype(np.float64)
    else:
        out = np.empty((n, ctype), dtype=np.float64)
        for c in range(ctype):
            out[:, c] = flat[c::stride // itemsize][:n]
    if a.get('normalized'):
        if np.iinfo(dt).min < 0:
            out = np.maximum(out / np.abs(np.iinfo(dt).min), -1.0)
        else:
            out = out / np.iinfo(dt).max
    return out

def get_basecolor(g, chunk):
    pbr = g['materials'][0]['pbrMetallicRoughness']
    ti = pbr['baseColorTexture']['index']
    img_i = g['textures'][ti]['source']
    bv = g['bufferViews'][g['images'][img_i]['bufferView']]
    raw = chunk[bv['byteOffset']:bv['byteOffset'] + bv['byteLength']]
    return Image.open(io.BytesIO(raw)).convert('RGB')

def sample_vert_colors(uv, img):
    """本批 GLB 的经验约定: 像素行 y = v*H (不翻转; 鞋底采样验证)"""
    W, H = img.size
    u = np.clip(uv[:, 0], 0, 0.99999)
    v = np.clip(uv[:, 1], 0, 0.99999)
    xi = (u * W).astype(np.int64)
    yi = (v * H).astype(np.int64)
    arr = np.asarray(img)
    return arr[yi, xi].astype(np.float32) / 255.0

# ---------------- 工具 ----------------
def kmeans(data, k, iters=25, seed=0):
    rng = np.random.default_rng(seed)
    sub = data if len(data) <= 120000 else data[rng.choice(len(data), 120000, replace=False)]
    C = sub[rng.choice(len(sub), k, replace=False)]
    for _ in range(iters):
        d = ((sub[:, None, :] - C[None, :, :]) ** 2).sum(-1)
        l = d.argmin(1)
        for j in range(k):
            m = l == j
            if m.any():
                C[j] = sub[m].mean(0)
    d = ((data[:, None, :] - C[None, :, :]) ** 2).sum(-1)
    return d.argmin(1), C

def name_color(c):
    r, g, b = c
    mx, mn = max(c), min(c)
    sat = 0 if mx == 0 else (mx - mn) / mx
    if mx < 0.30:
        return 'dark_gray'
    if sat < 0.16:
        return 'white' if mx > 0.72 else 'light_gray'
    if b > r and b > g:
        return 'purple' if r > 0.45 and g < r else 'blue'
    if r > 0.5 and g > 0.42 and b < 0.35:
        return 'blond'
    if r > g > b and r > 0.4 and sat < 0.6:
        return 'skin'
    return f'c{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}'

def weld(tris, tol=1e-4):
    key = np.round(tris.reshape(-1, 3) / tol).astype(np.int64)
    uniq, inv = np.unique(key, axis=0, return_inverse=True)
    return uniq * tol, inv.reshape(-1, 3)

def vertex_normals(verts, faces, smooth_iters=4):
    n = np.cross(verts[faces[:, 1]] - verts[faces[:, 0]],
                 verts[faces[:, 2]] - verts[faces[:, 0]])
    vn = np.zeros_like(verts)
    for c in range(3):
        np.add.at(vn, faces[:, c], n)
    vn /= np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-12)
    E = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    E = np.vstack([E, E[:, ::-1]])
    for _ in range(smooth_iters):
        acc = np.zeros_like(vn)
        np.add.at(acc, E[:, 0], vn[E[:, 1]])
        vn = vn + 0.5 * acc / np.maximum(np.bincount(E[:, 0], minlength=len(verts))[:, None], 1)
        vn /= np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-12)
    return vn

def signed_volume(verts, faces):
    v0, v1, v2 = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    return np.einsum('ij,ij->i', v0, np.cross(v1, v2)).sum() / 6.0

def write_stl(path, verts, faces):
    n = np.cross(verts[faces[:, 1]] - verts[faces[:, 0]],
                 verts[faces[:, 2]] - verts[faces[:, 0]])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    F = len(faces)
    buf = np.zeros((F, 50), dtype=np.uint8)
    buf[:, 0:12] = n.astype('<f4').view(np.uint8).reshape(F, 12)
    buf[:, 12:48] = verts[faces].astype('<f4').view(np.uint8).reshape(F, 36)
    with open(path, 'wb') as f:
        f.write(b'\0' * 80 + struct.pack('<I', F) + buf.tobytes())

def write_glb_parts(path, parts):
    """parts: list of dict(verts Nx3, faces Mx3, rgb) -> 单 mesh 多 primitive 预览 GLB"""
    import json as _json
    gltf = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}], "meshes": [{"primitives": []}],
            "materials": [], "buffers": [], "bufferViews": [], "accessors": [],
            "images": [], "textures": []}
    blob = bytearray()
    def add_acc(arr, comp_type, acc_type, count):
        pad = (4 - len(blob) % 4) % 4
        blob.extend(b'\0' * pad)
        vi = len(gltf['bufferViews'])
        gltf['bufferViews'].append({"buffer": 0, "byteOffset": len(blob), "byteLength": arr.nbytes})
        blob.extend(arr.tobytes())
        ai = len(gltf['accessors'])
        gltf['accessors'].append({"bufferView": vi, "componentType": comp_type,
                                  "count": count, "type": acc_type})
        return ai
    for p in parts:
        v = p['verts']
        pos = np.column_stack([v[:, 0], v[:, 2], -v[:, 1]]).astype('<f4')  # Z-up -> Y-up
        idx = p['faces'].astype(np.uint32)
        pai = add_acc(pos, 5126, 'VEC3', len(pos))
        iai = add_acc(idx, 5125, 'SCALAR', idx.size)
        r, g, b = p['rgb']
        mi = len(gltf['materials'])
        gltf['materials'].append({"name": p['name'], "pbrMetallicRoughness": {
            "baseColorFactor": [float(r), float(g), float(b), 1.0], "metallicFactor": 0.0, "roughnessFactor": 0.7}})
        gltf['meshes'][0]['primitives'].append(
            {"attributes": {"POSITION": pai}, "indices": iai, "material": mi})
    pad = (4 - len(blob) % 4) % 4
    blob.extend(b'\0' * pad)
    gltf['buffers'] = [{"byteLength": len(blob)}]
    js = _json.dumps(gltf, separators=(',', ':')).encode()
    js += b' ' * ((4 - len(js) % 4) % 4)
    total = 12 + 8 + len(js) + 8 + len(blob)
    with open(path, 'wb') as f:
        f.write(struct.pack('<III', GLB_MAG, GLB_VER, total))
        f.write(struct.pack('<II', len(js), CHUNK_JSON) + js)
        f.write(struct.pack('<II', len(blob), CHUNK_BIN) + bytes(blob))

# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--solid', default='astronaut_kid_print_300k.stl')
    ap.add_argument('--ref', default='astronaut_kid_upload_purple.glb')
    ap.add_argument('--outdir', default='parts_color')
    ap.add_argument('--wall', type=float, default=1.2, help='壁厚 mm')
    ap.add_argument('--k', type=int, default=10)
    ap.add_argument('--min-area', type=float, default=0.002)
    ap.add_argument('--min-part', type=float, default=0.003, help='材料最小面积占比')
    args = ap.parse_args()
    t0 = time.time()
    os.makedirs(args.outdir, exist_ok=True)

    # 1. 参考几何 + 顶点颜色
    g, chunk = read_glb(args.ref)
    prim = g['meshes'][0]['primitives'][0]
    dpos = read_acc(g, chunk, prim['attributes']['POSITION'])
    duv = read_acc(g, chunk, prim['attributes']['TEXCOORD_0'])
    didx = read_acc(g, chunk, prim['indices']).astype(np.int64).ravel().reshape(-1, 3)
    img = get_basecolor(g, chunk)
    img = img.filter(ImageFilter.GaussianBlur(15))
    dcol = sample_vert_colors(duv, img)
    ref_h = dpos[:, 1].max() - dpos[:, 1].min()          # Y-up
    print(f'ref: {len(dpos)} verts {len(didx)} faces, tex {img.size}, Y-height {ref_h:.3f}')
    print(f'ref bbox min {dpos.min(0).round(3)} max {dpos.max(0).round(3)}')

    # 2. 实体 STL
    data = open(args.solid, 'rb').read()
    n = struct.unpack('<I', data[80:84])[0]
    arr = np.frombuffer(data, dtype=np.uint8, count=n * 50, offset=84).reshape(n, 50)
    tris = arr[:, 12:48].copy().view('<f4').reshape(n, 3, 3).astype(np.float64)
    verts, faces = weld(tris)
    vol = signed_volume(verts, faces)
    if vol < 0:
        faces = faces[:, ::-1]
        vol = -vol
    print(f'solid: {len(faces)} faces, {len(verts)} verts, volume {vol/1000:.1f} cm^3')

    # 3. 对齐: STL(Z-up, mm) -> ref(Y-up)。候选旋转
    s = ref_h / 100.0
    cands = {
        'x=z,y=-y': lambda p: np.column_stack([p[:, 0], p[:, 2], -p[:, 1]]),
        'x=z,y=y':  lambda p: np.column_stack([p[:, 0], p[:, 2], p[:, 1]]),
    }
    sub = verts[np.random.default_rng(0).choice(len(verts), min(20000, len(verts)), replace=False)]
    tree0 = cKDTree(dpos)
    best, best_d = None, 1e9
    for nm, fn in cands.items():
        d, _ = tree0.query(fn(sub) * s, workers=-1)
        print(f'  align {nm}: mean NN {d.mean()*100:.2f} mm')
        if d.mean() < best_d:
            best, best_d = fn, d.mean()
    assert best_d < 0.03, f'对齐失败 mean NN {best_d*100:.2f} mm'
    verts_ref = best(verts) * s                           # 应用旋转+缩放到 ref 单位系
    d, nn = cKDTree(dpos).query(verts_ref, workers=-1)
    vcol = dcol[nn]
    print(f'align ok ({best_d*100:.2f} mm): final mean NN {d.mean()*100:.3f} mm, p95 {np.percentile(d,95)*100:.3f} mm')

    # 4. 材料分类(色度归一化 + 明度分箱, 对 AO 阴影鲁棒) —— 每类 = 一种打印材料
    MAT_NAMES = ['white', 'light_gray', 'dark_gray', 'purple', 'blue', 'light_blue', 'blond', 'skin']
    name_idx = {n: i for i, n in enumerate(MAT_NAMES)}
    mx = vcol.max(1)
    mn = vcol.min(1)
    val = mx
    cn = vcol / np.maximum(mx[:, None], 1e-6)            # 色度归一化(最大通道=1)
    cnr, cng, cnb = cn[:, 0], cn[:, 1], cn[:, 2]
    warm = (cnr - cng) > 0.15                            # 明显暖偏(金发/肤色)
    cool = (cnb - np.maximum(cnr, cng)) > 0.15           # 明显冷偏(紫/蓝)
    lab = np.full(len(vcol), name_idx['white'], dtype=np.int64)
    lab[val < 0.25] = name_idx['dark_gray']
    neutral = ~warm & ~cool & (lab != name_idx['dark_gray'])
    lab[neutral & (val < 0.66)] = name_idx['light_gray']
    blond_m = warm & (cnb < 0.45)
    skin_m = warm & ~blond_m & ((cnr - cnb) > 0.19)
    purple_m = cool & ((cnr - cng) > 0.05)
    lightblue_m = cool & ~purple_m & (val > 0.70) & (cng > cnr)
    blue_m = cool & ~purple_m & ~lightblue_m
    lab[blond_m] = name_idx['blond']
    lab[skin_m] = name_idx['skin']
    lab[purple_m] = name_idx['purple']
    lab[lightblue_m] = name_idx['light_blue']
    lab[blue_m] = name_idx['blue']
    Cm = np.zeros((len(MAT_NAMES), 3))
    K = len(MAT_NAMES)
    sat_all = (mx - mn) / np.maximum(mx, 1e-6)
    for i, n in enumerate(MAT_NAMES):
        m = lab == i
        if not m.any():
            Cm[i] = np.array([0.5, 0.5, 0.5])
            continue
        sub = vcol[m]
        s_sub = sat_all[m]
        # 代表色取类内饱和度前 50% 的均值, 避免被碎斑灰稀释
        thr = np.median(s_sub)
        strong = sub[s_sub >= thr] if (s_sub >= thr).any() else sub
        Cm[i] = strong.mean(0)
    for i, n in enumerate(MAT_NAMES):
        print(f'  material {n:10s} rgb({Cm[i][0]:.2f},{Cm[i][1]:.2f},{Cm[i][2]:.2f})  {(lab==i).mean()*100:5.1f}%')

    # 5. 标签平滑
    E = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    E = np.vstack([E, E[:, ::-1]])
    for _ in range(4):
        cnt = np.bincount(E[:, 0] * K + lab[E[:, 1]],
                          minlength=len(verts) * K).reshape(len(verts), K)
        cnt[np.arange(len(verts)), lab] += 1
        lab = cnt.argmax(1)

    # 6. 面标签 + 同标签连通域小岛合并 + 边界清理
    def face_labels(lab_v):
        fc = np.zeros((len(faces), K), dtype=np.int64)
        for c in range(3):
            np.add.at(fc, (np.arange(len(faces)), lab_v[faces[:, c]]), 1)
        return fc.argmax(1)

    flab = face_labels(lab)
    # 面邻接对
    fe = np.sort(np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1)
    fe_key = fe[:, 0].astype(np.int64) * (len(verts) + 1) + fe[:, 1]
    _, inv_e = np.unique(fe_key, return_inverse=True)
    face_id = np.tile(np.arange(len(faces)), 3)
    order2 = np.argsort(inv_e, kind='stable')
    inv_s = inv_e[order2]; fid_s = face_id[order2]
    cnt_e = np.bincount(inv_e)
    sm = cnt_e[inv_s] == 2
    pairs_all = np.column_stack([fid_s[sm][0::2], fid_s[sm][1::2]])
    v0, v1, v2 = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    farea = np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1) / 2
    total_area = farea.sum()
    fc_cent = (v0 + v1 + v2) / 3

    def merge_small_islands(flab_in, min_frac):
        """同标签面连通域 < min_frac*总面积 的岛, 沿边界反复并入实际接触的最大邻岛"""
        flab_in = flab_in.copy()
        total_merged = 0
        for _ in range(30):
            same = flab_in[pairs_all[:, 0]] == flab_in[pairs_all[:, 1]]
            pairs = pairs_all[same]
            nc, comp = connected_components(coo_matrix(
                (np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])),
                shape=(len(faces), len(faces))), directed=False)
            comp_area = np.bincount(comp, weights=farea, minlength=nc)
            small = comp_area < min_frac * total_area
            if not small.any():
                break
            # 岛间邻接(跨标签的内部边)
            diff = pairs_all[~same]
            if len(diff) == 0:
                break
            a_c, b_c = comp[diff[:, 0]], comp[diff[:, 1]]
            cross = a_c != b_c
            ep = np.unique(np.sort(np.column_stack([a_c[cross], b_c[cross]]), axis=1), axis=0)
            # 每个小岛: 邻岛中面积最大者(向量化)
            both = np.vstack([ep, ep[:, ::-1]])
            small_both = small[both[:, 0]]
            both = both[small_both]
            if len(both) == 0:
                break
            order = np.lexsort((-comp_area[both[:, 1]], both[:, 0]))
            both = both[order]
            first = np.ones(len(both), dtype=bool)
            first[1:] = both[1:, 0] != both[:-1, 0]
            sel = both[first]
            newlab = np.arange(nc)
            newlab[sel[:, 0]] = sel[:, 1]
            # 传播(小岛可能只邻小岛)
            for _ in range(60):
                nxt = newlab[newlab]
                if (nxt == newlab).all():
                    break
                newlab = nxt
            comp_lab = np.zeros(nc, dtype=np.int64)
            cc = np.zeros((nc, K), dtype=np.int64)
            np.add.at(cc, (comp, flab_in), 1)
            comp_lab = cc.argmax(1)
            flab_in = comp_lab[newlab][comp]
            total_merged += int(small.sum())
        return flab_in, total_merged, nc

    flab, nm1, nc1 = merge_small_islands(flab, args.min_area)
    print(f'islands pass1: components {nc1}, merged {nm1}')

    # 边界再平滑: 顶点级多数票 5 轮 -> 面标签
    for _ in range(20):
        cnt = np.bincount(E[:, 0] * K + lab[E[:, 1]],
                          minlength=len(verts) * K).reshape(len(verts), K)
        cnt[np.arange(len(verts)), lab] += 1
        lab = cnt.argmax(1)
    flab = face_labels(lab)
    flab, nm2, nc2 = merge_small_islands(flab, args.min_area)
    print(f'islands pass2: components {nc2}, merged {nm2}')

    # 几何先验: 靴子高度带(z<16mm)内, 非蓝色面(鞋底/环带)一律归紫色鞋面
    idx = {nm: i for i, nm in enumerate(MAT_NAMES)}
    fz = (verts[faces[:, 0]] + verts[faces[:, 1]] + verts[faces[:, 2]])[:, 2] / 3
    boot_band = fz < 16.0
    blueish = np.isin(flab, [idx['blue'], idx['light_blue']])
    flab[boot_band & ~blueish] = idx['purple']
    print(f'boot band: {boot_band.sum()} faces -> purple (except blue sole/ring)')
    # 丢掉过小的整个标签 -> 并入面积最近的大标签(按中心色距)
    area_share = np.array([farea[flab == j].sum() for j in range(K)]) / farea.sum()
    keep = area_share > args.min_part
    print('materials:')
    for j in range(K):
        print(f'  [{j}] {name_color(Cm[j]):10s} rgb({Cm[j][0]:.2f},{Cm[j][1]:.2f},{Cm[j][2]:.2f})  area {area_share[j]*100:5.1f}%  keep={keep[j]}')
    if (~keep).any():
        for j in np.flatnonzero(~keep):
            m = flab == j
            if not m.any():
                continue
            d2 = ((Cm[j] - Cm[keep]) ** 2).sum(1)
            flab[m] = np.flatnonzero(keep)[d2.argmin()]
    Cm, K = Cm[keep], int(keep.sum())
    mat_names = [n for n, k in zip(MAT_NAMES, keep) if k]
    flab = np.searchsorted(np.flatnonzero(keep), flab) if (~keep).any() else flab

    # 7. 顶点法线(平滑) + 分件实体化
    vn = vertex_normals(verts, faces)
    np.savez(os.path.join(args.outdir, '_cache.npz'), verts=verts, faces=faces, flab=flab)
    parts = []
    manifest = []
    for j in range(K):
        Fj = faces[flab == j]
        if len(Fj) == 0:
            continue
        used, local = np.unique(Fj, return_inverse=True)
        local = local.reshape(-1, 3)
        nv = len(used)
        vo = verts[used]
        vi = vo - vn[used] * args.wall
        # 边界: 组内 undirected 边只出现一次
        ekey = np.sort(np.vstack([local[:, [0, 1]], local[:, [1, 2]], local[:, [2, 0]]]), axis=1)
        uk, inv_ek, cnts = np.unique(ekey, axis=0, return_inverse=True, return_counts=True)
        bnd = cnts[inv_ek] == 1
        # 边界顶点的边界边数; >2 的交汇顶点需按扇区拆分复制
        bdeg = np.bincount(uk[cnts == 1].reshape(-1), minlength=nv)
        junction = np.flatnonzero(bdeg > 2)
        corner = local.copy()                          # 每面每角的顶点(交汇处替换为副本)
        extra = []                                     # 副本坐标(外/内同形)
        for w in junction:
            fis = np.flatnonzero((local == w).any(1))
            parent = {f: f for f in fis}
            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x
            # w 的内部边(两侧面都在本组)连接相邻面 -> 扇区
            sel_w = (ekey[:, 0] == w) | (ekey[:, 1] == w)
            for ea, eb, is_in in zip(ekey[sel_w][:, 0], ekey[sel_w][:, 1], cnts[inv_ek[sel_w]] == 2):
                if not is_in:
                    continue
                x = eb if ea == w else ea
                fs = [f for f in fis if (local[f] == w).any() and (local[f] == x).any()]
                if len(fs) == 2:
                    ra, rb = find(fs[0]), find(fs[1])
                    if ra != rb:
                        parent[ra] = rb
            roots = {}
            for f in fis:
                roots.setdefault(find(f), len(roots))
            for f in fis:
                k = roots[find(f)]
                if k > 0:
                    corner[f, np.flatnonzero(local[f] == w)[0]] = nv + len(extra) + k - 1
            for _ in range(len(roots) - 1):
                extra.append(w)
        nvc = nv + len(extra)
        if extra:
            extra = np.array(extra)
            vo = np.vstack([vo, vo[extra]])
            vi = np.vstack([vi, vi[extra]])
        # 内壳(反转绕向, 用重映射后的角点)
        inner = corner[:, ::-1] + nvc
        # 侧壁: 重映射后的有向边界边 (a -> b)
        de = np.vstack([corner[:, [0, 1]], corner[:, [1, 2]], corner[:, [2, 0]]])
        ab = de[bnd]
        a, b = ab[:, 0], ab[:, 1]
        walls = np.vstack([
            np.column_stack([a, a + nvc, b + nvc]),
            np.column_stack([a, b + nvc, b]),
        ])
        pf = np.vstack([corner, inner, walls])
        pv = np.vstack([vo, vi])
        pv_vol = signed_volume(pv, pf)
        if pv_vol < 0:
            pf = pf[:, ::-1]
            pv_vol = -pv_vol
        nm = mat_names[j]
        rgb = Cm[j]
        area_pct = farea[flab == j].sum() / farea.sum() * 100
        vol_cm3 = pv_vol / 1000
        # 水密校验
        pe = np.sort(np.vstack([pf[:, [0, 1]], pf[:, [1, 2]], pf[:, [2, 0]]]), axis=1)
        _, ec = np.unique(pe, axis=0, return_counts=True)
        watertight = (ec == 2).all()
        msg = f'part {j} {nm:10s} faces {len(pf):7d} vol {vol_cm3:7.2f} cm3 area {area_pct:5.1f}% watertight={watertight}'
        if not watertight:
            hist = dict(zip(*np.unique(ec, return_counts=True)))
            msg += f'  edge_deg {hist}'
        print(msg)
        fname = f'part_{j:02d}_{nm}.stl'
        write_stl(os.path.join(args.outdir, fname), pv, pf)
        parts.append({'verts': vo, 'faces': local, 'rgb': rgb, 'name': nm})
        manifest.append({'file': fname, 'name': nm,
                         'rgb': [round(float(x), 3) for x in rgb],
                         'hex': '#%02x%02x%02x' % tuple(int(x * 255) for x in rgb),
                         'surface_area_pct': round(float(area_pct), 2),
                         'volume_cm3': round(float(vol_cm3), 2),
                         'watertight': bool(watertight)})
    with open(os.path.join(args.outdir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump({'wall_mm': args.wall, 'parts': manifest}, f, ensure_ascii=False, indent=2)
    write_glb_parts(os.path.join(args.outdir, 'parts_preview.glb'), parts)
    print(f'\n{len(manifest)} parts -> {args.outdir}/, total {time.time()-t0:.1f}s')
    print(f'preview: {args.outdir}/parts_preview.glb')

if __name__ == '__main__':
    main()
