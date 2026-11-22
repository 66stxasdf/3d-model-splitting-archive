# -*- coding: utf-8 -*-
"""
按结构分层分割公仔 (不看颜色, 纯几何规则) -> 每零件一个水密实体 -> STL + STEP

层级逻辑 (用户定义):
  公仔
   ├─ 脑袋 head
   │   ├─ 帽子(头盔+面罩+耳罩) helmet
   │   ├─ 头发 hair          (前方最前沿凸出层)
   │   └─ 脸 face
   │       ├─ 左眼 eye_L      (脸部凸包, 镜像成对)
   │       └─ 右眼 eye_R
   ├─ 躯干 torso
   ├─ 背包 backpack
   ├─ 左臂 arm_L / 右臂 arm_R
   ├─ 武器 weapon            (躯干前方突出物, z 31~57, y<-15)
   └─ 左腿 leg_L / 右腿 leg_R
       └─ 左靴 boot_L / 右靴 boot_R   (z<16)

规则全部为几何量: 高度带 z / 前后 y / 左右 x / 局部凸出深度。
"""
import json, os, struct, sys, time
import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

sys.path.insert(0, os.path.dirname(__file__))
from skeleton_l1 import read_stl

WALL = 1.2          # 壁厚 mm
Z_BOOT = 16.0       # 靴/腿分界
Z_NECK = 63.0
GUN_Y = -15.0       # 枪分界面 (胸口前缘 ~-14, 枪体 <-16)
HEAD_Z0 = 60.0
DECIMATE_TO = 90000

# ---------------- 基础 ----------------
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
    gltf = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}], "meshes": [{"primitives": []}],
            "materials": [], "buffers": [], "bufferViews": [], "accessors": []}
    blob = bytearray()
    def add(arr, comp, typ, count):
        pad = (4 - len(blob) % 4) % 4
        blob.extend(b'\0' * pad)
        vi = len(gltf['bufferViews'])
        gltf['bufferViews'].append({"buffer": 0, "byteOffset": len(blob), "byteLength": arr.nbytes})
        blob.extend(arr.tobytes())
        ai = len(gltf['accessors'])
        gltf['accessors'].append({"bufferView": vi, "componentType": comp,
                                  "count": count, "type": typ})
        return ai
    for p in parts:
        v = p['verts']
        pos = np.column_stack([v[:, 0], v[:, 2], -v[:, 1]]).astype('<f4')
        idx = p['faces'].astype(np.uint32)
        pai = add(pos, 5126, 'VEC3', len(pos))
        iai = add(idx, 5125, 'SCALAR', idx.size)
        r, g, b = p['rgb']
        mi = len(gltf['materials'])
        gltf['materials'].append({"name": p['name'], "pbrMetallicRoughness": {
            "baseColorFactor": [float(r), float(g), float(b), 1.0],
            "metallicFactor": 0.0, "roughnessFactor": 0.7}})
        gltf['meshes'][0]['primitives'].append(
            {"attributes": {"POSITION": pai}, "indices": iai, "material": mi})
    pad = (4 - len(blob) % 4) % 4
    blob.extend(b'\0' * pad)
    gltf['buffers'] = [{"byteLength": len(blob)}]
    js = json.dumps(gltf, separators=(',', ':')).encode()
    js += b' ' * ((4 - len(js) % 4) % 4)
    total = 12 + 8 + len(js) + 8 + len(blob)
    with open(path, 'wb') as f:
        f.write(struct.pack('<III', 0x46546C67, 2, total))
        f.write(struct.pack('<II', len(js), 0x4E4F534A) + js)
        f.write(struct.pack('<II', len(blob), 0x004E4942) + bytes(blob))

def face_adjacency(faces):
    fe = np.sort(np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1)
    fe_key = fe[:, 0].astype(np.int64) * (faces.max() + 1) + fe[:, 1]
    _, inv_e = np.unique(fe_key, return_inverse=True)
    face_id = np.tile(np.arange(len(faces)), 3)
    order2 = np.argsort(inv_e, kind='stable')
    inv_s, fid_s = inv_e[order2], face_id[order2]
    cnt_e = np.bincount(inv_e)
    sm = cnt_e[inv_s] == 2
    return np.column_stack([fid_s[sm][0::2], fid_s[sm][1::2]])

# ---------------- 分割规则 ----------------
def assign_labels(verts, faces, l1):
    """l1: 每面 L1 标签(0..8). 返回零件标签数组"""
    NAMES = ['helmet', 'hair', 'face', 'eye_L', 'eye_R', 'torso', 'arm_L', 'arm_R',
             'leg_L', 'leg_R', 'boot_L', 'boot_R', 'weapon', 'backpack']
    NID = {n: i for i, n in enumerate(NAMES)}
    K = len(NAMES)
    lab = np.full(len(faces), -1, dtype=np.int64)
    cent = verts[faces].mean(1)
    cx, cy, cz = cent[:, 0], cent[:, 1], cent[:, 2]
    L1 = {n: i for i, n in enumerate(
        ['head', 'torso', 'arm_L', 'arm_R', 'leg_L', 'leg_R', 'weapon', 'backpack', 'noise'])}

    # ---- 1. 躯干/背/臂/腿 直通 ----
    lab[l1 == L1['torso']] = NID['torso']
    lab[l1 == L1['arm_L']] = NID['arm_L']
    lab[l1 == L1['arm_R']] = NID['arm_R']
    lab[l1 == L1['leg_L']] = NID['leg_L']
    lab[l1 == L1['leg_R']] = NID['leg_R']
    lab[l1 == L1['backpack']] = NID['backpack']

    # ---- 2. 靴: 腿区 z<Z_BOOT ----
    for side in ('L', 'R'):
        m = (l1 == L1['leg_' + side]) & (cz < Z_BOOT)
        lab[m] = NID['boot_' + side]

    # ---- 3. 武器: 躯干/臂 前方突出 z 31~57, y<GUN_Y ----
    mw = (cz > 31) & (cz < 57) & (cy < GUN_Y) & np.isin(l1, [L1['torso'], L1['arm_L'], L1['arm_R']])
    lab[mw] = NID['weapon']

    # ---- 4. 头部细分 ----
    hm = l1 == L1['head']
    hv = np.unique(faces[hm])

    def head_faces(mask_cent):
        m = hm & mask_cent
        return m

    # 4a. 眼睛: 椭圆核 + 环带二次曲面拟合, 凸出 >0.55mm 的最大连通域
    def detect_eye(cx0, cz0, rx=3.6, rz=4.0):
        dx = (cx - cx0) / rx
        dz = (cz - cz0) / rz
        core = hm & (cy < -8) & (dx * dx + dz * dz <= 1.0)
        ring = hm & (cy < -8) & (dx * dx + dz * dz <= 3.2) & ~core
        fi = np.flatnonzero(ring)
        X, Y, Z = cx[fi], cy[fi], cz[fi]
        A = np.column_stack([np.ones_like(X), X, Z, X * X, X * Z, Z * Z])
        keep = np.ones(len(fi), bool)
        for _ in range(4):
            coef, *_ = np.linalg.lstsq(A[keep], Y[keep], rcond=None)
            r = Y - A @ coef
            nk = r > -0.8
            if (nk == keep).all():
                break
            keep = nk
        fc = np.flatnonzero(core)
        Ac = np.column_stack([np.ones_like(fc), cx[fc], cz[fc],
                              cx[fc] ** 2, cx[fc] * cz[fc], cz[fc] ** 2])
        rc = cy[fc] - Ac @ coef
        eye = fc[rc < -0.55]
        # 最大连通域
        if len(eye) > 3:
            fe = np.sort(np.vstack([faces[eye][:, [0, 1]], faces[eye][:, [1, 2]],
                                    faces[eye][:, [2, 0]]]), axis=1)
            _, inv_e = np.unique(fe[:, 0].astype(np.int64) * (len(verts) + 1) + fe[:, 1],
                                 return_inverse=True)
            fid = np.tile(np.arange(len(eye)), 3)
            order = np.argsort(inv_e, kind='stable')
            inv_s, fid_s = inv_e[order], fid[order]
            sel = np.bincount(inv_e)[inv_s] == 2
            pr = np.column_stack([fid_s[sel][0::2], fid_s[sel][1::2]])
            nc, comp = connected_components(coo_matrix(
                (np.ones(len(pr)), (pr[:, 0], pr[:, 1])), shape=(len(eye), len(eye))), directed=False)
            sizes = np.bincount(comp)
            eye = eye[comp == sizes.argmax()]
        if len(eye) < 6:      # 低面数兜底: 直接取椭圆核内凸出面
            eye = fc[rc < -0.25]
            if len(eye) < 4:
                eye = fc
        return eye

    eyeL = detect_eye(-3.9, 77.6, rx=3.0, rz=3.4)
    eyeR = detect_eye(1.9, 77.6, rx=3.0, rz=3.4)
    print(f'  eye_L faces {len(eyeL)}, eye_R faces {len(eyeR)}')
    lab[eyeL] = NID['eye_L']
    lab[eyeR] = NID['eye_R']

    # 4b. 头发: 前沿层 ∩ 相对面部趋势面凸出 >1.0mm 的大连通域
    front = hm & (cy < -7) & (cz > 63) & (cz < 88) & (np.abs(cx) < 17) & (lab == -1)
    fi = np.flatnonzero(front)
    # 4b-1. 鲁棒二次趋势面 y(x,z) (迭代剔除前凸的头发/五官)
    X, Y, Z = cx[fi], cy[fi], cz[fi]
    A = np.column_stack([np.ones_like(X), X, Z, X * X, X * Z, Z * Z])
    keep = np.ones(len(fi), bool)
    for _ in range(6):
        coef, *_ = np.linalg.lstsq(A[keep], Y[keep], rcond=None)
        r = Y - A @ coef
        nk = r > -1.0
        if (nk == keep).all():
            break
        keep = nk
    r_all = Y - A @ coef
    # 4b-2. 前沿层 (1mm xz 网格包络)
    bx = np.floor(X).astype(int)
    bz = np.floor(Z).astype(int)
    key = (bz - bz.min()) * 100 + (bx - bx.min())
    ymin = np.full(key.max() + 1, 1e9)
    np.minimum.at(ymin, key, Y)
    layer = (Y < ymin[key] + 1.0) & (r_all < -1.0)
    cand = fi[layer]
    # 4b-3. 连通域过滤: 面积 >= 80mm^2 的才算头发 (小凸起=鼻/唇 归脸)
    if len(cand) > 3:
        fe = np.sort(np.vstack([faces[cand][:, [0, 1]], faces[cand][:, [1, 2]],
                                faces[cand][:, [2, 0]]]), axis=1)
        _, inv_e = np.unique(fe[:, 0].astype(np.int64) * (len(verts) + 1) + fe[:, 1],
                             return_inverse=True)
        fid = np.tile(np.arange(len(cand)), 3)
        order = np.argsort(inv_e, kind='stable')
        inv_s, fid_s = inv_e[order], fid[order]
        sel = np.bincount(inv_e)[inv_s] == 2
        pr = np.column_stack([fid_s[sel][0::2], fid_s[sel][1::2]])
        nc, comp = connected_components(coo_matrix(
            (np.ones(len(pr)), (pr[:, 0], pr[:, 1])), shape=(len(cand), len(cand))), directed=False)
        ar = np.zeros(len(cand))
        triv = verts[faces[cand]]
        ar = np.linalg.norm(np.cross(triv[:, 1] - triv[:, 0], triv[:, 2] - triv[:, 0]), axis=1) / 2
        carr = np.bincount(comp, weights=ar, minlength=nc)
        big = carr >= 80.0
        print(f'  hair 候选连通域: {nc}, 保留大域 {int(big.sum())} '
              f'(面积 {np.sort(carr)[::-1][:6].round(0)} mm^2)')
        cand = cand[big[comp]]
    lab[cand] = NID['hair']

    # 4c. 脸: 头部前方其余 (z<85, y<-8, |x|<16)
    face_m = hm & (cy < -8) & (cz < 85) & (np.abs(cx) < 16) & (lab == -1)
    lab[face_m] = NID['face']

    # 4d. 盔: 头部剩余 (含耳罩 |x|>15.5 段自动落入)
    lab[hm & (lab == -1)] = NID['helmet']

    # ---- 5. 兜底: L1 noise 面归最近已标注邻面 ----
    miss = lab == -1
    if miss.any():
        from scipy.spatial import cKDTree
        ok = ~miss
        tree = cKDTree(cent[ok])
        _, nn = tree.query(cent[miss], workers=-1)
        lab[miss] = lab[np.flatnonzero(ok)[nn]]
        print(f'  noise 回填 {miss.sum()} 面')

    assert (lab >= 0).all(), '存在未标注面'
    return lab, NAMES

# ---------------- 平滑 + 岛合并 ----------------
def smooth_and_merge(verts, faces, lab, K, min_frac=0.0015, protect=()):
    # 顶点级多数票 4 轮
    E = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    E = np.vstack([E, E[:, ::-1]])
    vlab = np.zeros(len(verts), dtype=np.int64)
    vlab[faces[:, 0]] = lab          # 初始化: 顶点取引用它的第一个面的标签
    for _ in range(4):
        cnt = np.zeros((len(verts), K), dtype=np.int32)
        np.add.at(cnt, (E[:, 0], vlab[E[:, 1]]), 1)
        cnt[np.arange(len(verts)), vlab] += 1
        vlab = cnt.argmax(1)
    fc = np.zeros((len(faces), K), dtype=np.int32)
    for c in range(3):
        np.add.at(fc, (np.arange(len(faces)), vlab[faces[:, c]]), 1)
    lab = fc.argmax(1)

    pairs = face_adjacency(faces)
    v0, v1, v2 = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    farea = np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1) / 2
    total_area = farea.sum()
    for it in range(30):
        same = lab[pairs[:, 0]] == lab[pairs[:, 1]]
        p = pairs[same]
        nc, comp = connected_components(coo_matrix(
            (np.ones(len(p)), (p[:, 0], p[:, 1])), shape=(len(faces), len(faces))), directed=False)
        comp_area = np.bincount(comp, weights=farea, minlength=nc)
        cc0 = np.zeros((nc, K), dtype=np.int64)
        np.add.at(cc0, (comp, lab), 1)
        comp_lab0 = cc0.argmax(1)
        small = (comp_area < min_frac * total_area) & ~np.isin(comp_lab0, list(protect))
        if not small.any():
            break
        diff = pairs[~same]
        if len(diff) == 0:
            break
        a_c, b_c = comp[diff[:, 0]], comp[diff[:, 1]]
        cross = a_c != b_c
        if not cross.any():
            break
        ep = np.unique(np.sort(np.column_stack([a_c[cross], b_c[cross]]), axis=1), axis=0)
        both = np.vstack([ep, ep[:, ::-1]])
        both = both[small[both[:, 0]]]
        if len(both) == 0:
            break
        order = np.lexsort((-comp_area[both[:, 1]], both[:, 0]))
        both = both[order]
        first = np.ones(len(both), bool)
        first[1:] = both[1:, 0] != both[:-1, 0]
        sel = both[first]
        newlab = np.arange(nc)
        newlab[sel[:, 0]] = sel[:, 1]
        for _ in range(60):
            nxt = newlab[newlab]
            if (nxt == newlab).all():
                break
            newlab = nxt
        cc = np.zeros((nc, K), dtype=np.int64)
        np.add.at(cc, (comp, lab), 1)
        comp_lab = cc.argmax(1)
        lab = comp_lab[newlab][comp]
    return lab, farea

# ---------------- 实体化 ----------------
def solidify(verts, faces, vn, wall=WALL):
    """外壳+内壁+侧壁 -> 水密三角网格"""
    # 清理退化面 (有两个顶点重合 / 面积为 0)
    keep = ((faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2]))
    if keep.any():
        f_ = faces[keep]
        ar = np.linalg.norm(np.cross(verts[f_[:, 1]] - verts[f_[:, 0]],
                                     verts[f_[:, 2]] - verts[f_[:, 0]]), axis=1) / 2
        faces = f_[ar > 1e-6]
        # 去重复面 (同一三角形出现两次会破坏水密性)
        fs = np.sort(faces, axis=1)
        _, uidx = np.unique(fs, axis=0, return_index=True)
        faces = faces[np.sort(uidx)]
    used, local = np.unique(faces, return_inverse=True)
    local = local.reshape(-1, 3)
    nv = len(used)
    vo = verts[used]
    vi = vo - vn[used] * wall
    ekey = np.sort(np.vstack([local[:, [0, 1]], local[:, [1, 2]], local[:, [2, 0]]]), axis=1)
    uk, inv_ek, cnts = np.unique(ekey, axis=0, return_inverse=True, return_counts=True)
    bnd = cnts[inv_ek] == 1
    bdeg = np.bincount(uk[cnts == 1].reshape(-1), minlength=nv)
    junction = np.flatnonzero(bdeg > 2)
    corner = local.copy()
    extra = []
    for w in junction:
        fis = np.flatnonzero((local == w).any(1))
        parent = {f: f for f in fis}
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
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
    inner = corner[:, ::-1] + nvc
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
    pe = np.sort(np.vstack([pf[:, [0, 1]], pf[:, [1, 2]], pf[:, [2, 0]]]), axis=1)
    _, ec = np.unique(pe, axis=0, return_counts=True)
    watertight = bool((ec == 2).all())
    return pv, pf, pv_vol / 1000, watertight

# ---------------- 主流程 ----------------
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--decimate', type=int, default=90000, help='目标面数')
    ap.add_argument('--outdir', default='parts_struct')
    args = ap.parse_args()
    t0 = time.time()
    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)

    tris = read_stl('astronaut_kid_print_300k.stl')
    verts, faces = weld(tris)
    print(f'原始: {len(faces)} 面 {len(verts)} 顶点')

    # 简化 (控制 STEP 面数)
    try:
        import fast_simplification
        pts, fcs = fast_simplification.simplify(
            verts.astype(np.float32), faces.astype(np.int64),
            target_reduction=1 - args.decimate / len(faces))
        tv = pts.astype(np.float64).reshape(-1, 3)
        tf = fcs.reshape(-1, 3)
        verts, faces = weld(tv[tf])
        print(f'简化后: {len(faces)} 面 {len(verts)} 顶点')
    except Exception as e:
        print('简化跳过:', e)

    # L1 体素标签 -> 面
    PITCH = 0.6
    vlabel = np.load('parts_hier/_vlabel.npy')
    mn = np.load('parts_hier/_skel.npz')['mn'].astype(float)
    cent = verts[faces].mean(1)
    gi = np.clip(np.floor((cent - mn) / PITCH).astype(int), 0, np.array(vlabel.shape) - 1)
    l1 = vlabel[gi[:, 0], gi[:, 1], gi[:, 2]]
    if (l1 < 0).any():
        from scipy.spatial import cKDTree
        vp = np.argwhere(vlabel >= 0)
        tree = cKDTree(vp)
        miss = l1 < 0
        _, nn = tree.query(gi[miss], workers=-1)
        l1[miss] = vlabel[vp[nn][:, 0], vp[nn][:, 1], vp[nn][:, 2]]

    # 规则分割
    lab, NAMES = assign_labels(verts, faces, l1)
    K = len(NAMES)

    # 平滑 + 岛合并
    lab, farea = smooth_and_merge(verts, faces, lab, K, min_frac=0.0015,
                                  protect=(NAMES.index('eye_L'), NAMES.index('eye_R')))
    print('面片统计:')
    for i, n in enumerate(NAMES):
        c = (lab == i).sum()
        if c:
            print('  %-9s %7d 面 (%.1f%%)' % (n, c, c / len(faces) * 100))

    # 预览色 (与材料无关, 仅区分零件)
    PREVIEW = {
        'helmet': '#dfe5ee', 'hair': '#f7b32b', 'face': '#f4a988', 'eye_L': '#7f4fc9',
        'eye_R': '#7f4fc9', 'torso': '#cfd6e0', 'arm_L': '#88b4de', 'arm_R': '#de9d7c',
        'leg_L': '#9fd6a8', 'leg_R': '#e8d98a', 'boot_L': '#a86fd6', 'boot_R': '#6f8fd6',
        'weapon': '#5a6a8a', 'backpack': '#e08bb0'}

    vn = vertex_normals(verts, faces)
    parts, manifest = [], []
    STEP_SOLIDS = []
    for i, n in enumerate(NAMES):
        Fsel = faces[lab == i]
        if len(Fsel) == 0:
            continue
        # 连通分量
        pairs = face_adjacency(Fsel)
        nc, comp = connected_components(coo_matrix(
            (np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(len(Fsel), len(Faces_c := Fsel))),
            directed=False) if len(pairs) else (1, np.zeros(len(Fsel), int))
        rgb = [int(PREVIEW[n][1:3], 16) / 255, int(PREVIEW[n][3:5], 16) / 255, int(PREVIEW[n][5:7], 16) / 255]
        fname = f'part_{i:02d}_{n}.stl'
        solids = []
        total_vol = 0.0
        all_wt = True
        for ci in range(nc):
            Fc = Fsel[comp == ci]
            if len(Fc) < 4:
                continue
            pv, pf, vol, wt = solidify(verts, Fc, vn)
            all_wt &= wt
            total_vol += vol
            solids.append((pv, pf))
        if not solids:
            continue
        # 合并写 STL
        Voff = 0
        mv, mf = [], []
        for pv, pf in solids:
            mv.append(pv)
            mf.append(pf + Voff)
            Voff += len(pv)
        mv = np.vstack(mv); mf = np.vstack(mf)
        write_stl(os.path.join(outdir, fname), mv, mf)
        parts.append({'verts': mv, 'faces': mf, 'rgb': rgb, 'name': n})
        manifest.append({'file': fname, 'name': n, 'cn': CN[n],
                         'surface_faces': int((lab == i).sum()),
                         'area_pct': round(float(farea[lab == i].sum() / farea.sum() * 100), 2),
                         'volume_cm3': round(total_vol, 2),
                         'pieces': len(solids), 'watertight': all_wt,
                         'rgb': rgb})
        STEP_SOLIDS.append((n, solids))
        print(f'  {fname:32s} vol {total_vol:7.2f} cm3 pieces {len(solids)} wt={all_wt}')

    json.dump({'wall_mm': WALL, 'parts': manifest}, open(
        os.path.join(outdir, 'manifest.json'), 'w', encoding='utf-8'),
        ensure_ascii=False, indent=2)
    write_glb_parts(os.path.join(outdir, 'parts_preview.glb'), parts)
    np.savez(os.path.join(outdir, '_labels.npz'), verts=verts, faces=faces, lab=lab, names=NAMES)
    print(f'\n{len(manifest)} 零件 -> {outdir}/ ({time.time()-t0:.1f}s)')

    # STEP 导出由 tools/export_step.py 负责
    print('STEP 导出请运行: python tools/export_step.py')


CN = {'helmet': '头盔', 'hair': '头发', 'face': '脸', 'eye_L': '左眼', 'eye_R': '右眼',
      'torso': '躯干', 'arm_L': '左臂', 'arm_R': '右臂', 'leg_L': '左腿', 'leg_R': '右腿',
      'boot_L': '左靴', 'boot_R': '右靴', 'weapon': '武器', 'backpack': '背包'}


def export_step_all(step_solids, outdir, manifest):
    """每个零件 sew 成 Solid, 导出单件 STEP + 总装 STEP"""
    from OCP.gp import gp_Pnt
    from OCP.BRepBuilderAPI_Sewing import BRepBuilderAPI_Sewing
    from OCP.BRepBuilderAPI_MakePolygon import BRepBuilderAPI_MakePolygon
    from OCP.BRepBuilderAPI_MakeFace import BRepBuilderAPI_MakeFace
    from OCP.BRepBuilderAPI_MakeSolid import BRepBuilderAPI_MakeSolid
    from OCP.BRepLib import BRepLib_OrientClosedSolid_s
    from OCP.TopoDS import TopoDS
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from build123d import Solid, Compound
    from build123d import export_step

    def tri_face(p1, p2, p3):
        poly = BRepBuilderAPI_MakePolygon(
            gp_Pnt(*p1), gp_Pnt(*p2), gp_Pnt(*p3), True)
        return BRepBuilderAPI_MakeFace(poly.Wire()).Face()

    def mesh_to_solid(pv, pf, tol=0.02):
        sew = BRepBuilderAPI_Sewing(tol, True, True, True, False)
        for f in pf:
            sew.Add(tri_face(pv[f[0]], pv[f[1]], pv[f[2]]))
        sew.Perform()
        shell = TopoDS.Shell_s(sew.SewedShape())
        mk = BRepBuilderAPI_MakeSolid(shell)
        solid = mk.Solid()
        BRepLib_OrientClosedSolid_s(solid)
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(solid, props)
        if props.Mass() < 0:
            solid = solid.Reversed()
        return Solid(solid)

    for (n, solids), m in zip(step_solids, manifest):
        try:
            bs = [mesh_to_solid(pv, pf) for pv, pf in solids]
            if len(bs) == 1:
                export_step(bs[0], os.path.join(outdir, f'part_{n}.step'))
            else:
                comp = Compound(children=[Solid(s.wrapped) for s in bs])
                comp.label = n
                export_step(comp, os.path.join(outdir, f'part_{n}.step'))
            m['step'] = f'part_{n}.step'
            print(f'  STEP part_{n}.step ok ({len(bs)} solid)')
        except Exception as e:
            print(f'  STEP {n} 失败: {e}')

    # 总装
    try:
        children = []
        for (n, solids), m in zip(step_solids, manifest):
            for k, (pv, pf) in enumerate(solids):
                s = mesh_to_solid(pv, pf)
                s.label = n if len(solids) == 1 else f'{n}_{k+1}'
                children.append(s)
        asm = Compound(children=children)
        asm.label = 'astronaut_kid_structured'
        export_step(asm, os.path.join(outdir, 'astronaut_kid_parts.step'))
        print('  STEP 总装 astronaut_kid_parts.step ok')
        m2 = json.load(open(os.path.join(outdir, 'manifest.json'), encoding='utf-8'))
        m2['assembly_step'] = 'astronaut_kid_parts.step'
        json.dump(m2, open(os.path.join(outdir, 'manifest.json'), 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=2)
    except Exception as e:
        print('  STEP 总装失败:', e)


if __name__ == '__main__':
    main()
