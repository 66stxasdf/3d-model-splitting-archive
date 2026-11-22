"""NDBG 可拆性分析 —— 「这个零件到底拆得出来吗」

为什么需要它
------------
「按颜色可差分」不是把颜色切开就完事。一个零件即使几何上独立，装配后也可能
被邻居卡死（插销在孔里、嵌件被边框压住、壳片被面罩顶住）。靠眼睛看图纸判断不可靠。

严格判据来自装配规划领域的 **NDBG（Non-Directional Blocking Graph）**：

  · 固定一个平移方向 d，构建有向图 G(d)：节点是零件，边 i→j 表示
    「i 沿 d 平移会撞上 j」。
  · G(d) 里 **出度为 0** 的节点，就是能沿 d 直接抽出的零件。
  · 抽走它，重算 G(d)，重复 —— 得到**拆解序列**。
  · 某一轮所有节点出度都 > 0 ⇒ 结构锁死（拆不开也装不进，除非有弹性变形）。

NDBG 覆盖全部方向后，一个零件可拆 ⇔ 存在某个方向使它在 G(d) 里出度为 0。

实现：体素扫描线
----------------
不用逐对求 Minkowski 差（O(n²) 布尔运算，慢且不稳）。改成体素 + 扫描线：

  1. 所有零件体素化到同一张全局格（pitch 默认 0.8mm）。
  2. 对方向 d，取两个与 d 垂直的整数泛函 u、v。**同一个 (u·p, v·p) 的体素
     必然落在一条与 d 平行的直线上**（因为沿 d 移动时 u·p、v·p 不变），
     反之不同直线必然不同 key。
  3. 每条线上按 d 排序。线的**最前端**（d·p 最大）的零件不被这条线阻挡；
     线上其余零件全部被「挡在它前面」的零件堵住。
  4. 一个零件沿 d 可拆 ⇔ 它占据的**每一条**线它都最靠前。

这套判据只做整数索引运算，26 个方向 × 十万体素在秒级完成，且不会出现
布尔运算的退化失败。

已知边界
--------
· 只覆盖**平移**拆解。需要旋出/弹性变形的零件会被判为不可拆（保守，安全）。
· 体素离散化会在斜方向上产生 1 格误差；本工具偏向「判为不可拆」，不会放行。
· 零件贴合面共面时体素可能落在同一格，标记为 -2（多件共用），视为空隙。
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import numpy as np

# 26 个方向（6 面 + 12 棱 + 8 角）
DIRS26 = np.array([d for d in itertools.product((-1, 0, 1), repeat=3) if any(d)],
                  dtype=np.int64)
DIRS6 = np.array([[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]],
                 dtype=np.int64)

DIR_NAMES = {
    (1, 0, 0): '+X', (-1, 0, 0): '-X', (0, 1, 0): '+Y', (0, -1, 0): '-Y',
    (0, 0, 1): '+Z', (0, 0, -1): '-Z',
}


def dir_name(d) -> str:
    t = tuple(int(x) for x in d)
    return DIR_NAMES.get(t, f'({t[0]:+d},{t[1]:+d},{t[2]:+d})')


# ---------------------------------------------------------------- 体素化


def _mesh_of(solid, tol=0.25):
    import trimesh
    vs, fs = solid.tessellate(tol, 0.35)
    v = np.array([[q.X, q.Y, q.Z] for q in vs], dtype=np.float64)
    f = np.array(fs, dtype=np.int64)
    return trimesh.Trimesh(vertices=v, faces=f, process=True)


def voxelize(solids, pitch: float = 0.6, pad: float = 1.5):
    """稀疏体素化：返回 (idx, lab, origin, dims)。

    idx: (M,3) 整格坐标；lab: (M,) 零件索引。**同一格允许多个零件**。

    为什么不用「一张网格 + 冲突标记」：早期版本把两件共占的格标成 -2 并当空隙，
    结果贴合面（间隙 0.15mm < 体素 0.8mm）上的零件全被判成"互不阻挡"，
    连"套在柱上的环能侧向抽出"这种明显错误都放行了。保留全部占用记录后，
    同深度元素天然互不阻挡，而真正一前一后的仍然阻挡。
    """
    meshes = [_mesh_of(s) for s in solids]
    lo = np.min([m.bounds[0] for m in meshes], axis=0) - pad
    hi = np.max([m.bounds[1] for m in meshes], axis=0) + pad
    dims = np.ceil((hi - lo) / pitch).astype(np.int64)

    idxs, labs = [], []
    for i, m in enumerate(meshes):
        vg = m.voxelized(pitch).fill()
        if len(vg.points) == 0:
            continue
        ix = np.floor((np.asarray(vg.points) - lo) / pitch).astype(np.int64)
        ok = np.all((ix >= 0) & (ix < dims), axis=1)
        ix = ix[ok]
        # 同一零件内部去重
        flat = np.unique(np.ravel_multi_index(ix.T, tuple(dims)))
        ix = np.column_stack(np.unravel_index(flat, tuple(dims)))
        idxs.append(ix)
        labs.append(np.full(len(ix), i, dtype=np.int64))
    return np.vstack(idxs), np.concatenate(labs), lo, dims


# ---------------------------------------------------------------- 方向基


def perp_uv(d):
    """给方向 d 找两个整系数泛函 u、v（都垂直 d、线性无关）。

    同一条与 d 平行的直线上，(u·p, v·p) 恒定；不同直线必然不同。
    """
    d = np.asarray(d, dtype=np.int64)
    for a in np.eye(3, dtype=np.int64):
        u = np.cross(d, a)
        if np.any(u):
            break
    g = math.gcd(abs(int(u[0])), math.gcd(abs(int(u[1])), abs(int(u[2]))))
    u //= g
    v = np.cross(d, u)
    g = math.gcd(abs(int(v[0])), math.gcd(int(v[1])), abs(int(v[2])))
    v //= g
    return u, v


# ---------------------------------------------------------------- 阻塞图


def blocking(idx, lab, dirs=DIRS26, alive: np.ndarray | None = None) -> np.ndarray:
    """返回 blocked[i, k]：零件 i 沿 dirs[k]（无限平移）是否被挡住。

    判据：把体素投影到「与 d 垂直的两条整数泛函 (u,v)」上分组，每组就是一条
    平行于 d 的直线。线上 depth（d·p）最大的元素是"最前端"。凡 depth 严格小于
    本线最大值的元素，其前方有东西挡着 —— 该零件沿 d 出不去。
    depth 相等的元素（贴合面）互不阻挡。
    """
    npart = int(lab.max()) + 1 if alive is None else len(alive)
    if alive is not None:
        keep = alive[lab]
        idx, lab = idx[keep], lab[keep]
    blocked = np.zeros((npart, len(dirs)), dtype=bool)
    if len(idx) == 0:
        return blocked

    for k, d in enumerate(dirs):
        u, v = perp_uv(d)
        cu = idx @ u
        cv = idx @ v
        cd = idx @ d
        order = np.lexsort((cd, cv, cu))            # 主序 (cu,cv)，次序 depth
        cu_s, cv_s, cd_s, lab_s = cu[order], cv[order], cd[order], lab[order]
        newline = np.r_[False,
                        (cu_s[1:] != cu_s[:-1]) | (cv_s[1:] != cv_s[:-1])]
        is_last = newline.copy()
        is_last[-1] = True
        ends = np.flatnonzero(is_last)
        line_max = np.repeat(cd_s[ends], np.diff(np.r_[-1, ends]))
        hit = cd_s < line_max
        blocked[lab_s[hit], k] = True
    return blocked


@dataclass
class DisassemblyReport:
    ok: bool
    batches: list = field(default_factory=list)     # [[(idx, dir), ...], ...]
    locked: list = field(default_factory=list)      # 锁死拆不出的零件
    free_dirs: dict = field(default_factory=dict)   # pid -> [可拆方向]
    pids: list = field(default_factory=list)

    def summary(self, names=None) -> str:
        nm = names or (lambda i: self.pids[i] if i < len(self.pids) else str(i))
        lines = []
        if not self.ok:
            lines.append(f'✗ 不可完全拆解：{len(self.locked)} 件锁死 -> '
                         + ', '.join(nm(i) for i in self.locked))
        else:
            lines.append(f'✓ 可完全拆解，共 {sum(len(b) for b in self.batches)} 件 / '
                         f'{len(self.batches)} 批')
            for bi, b in enumerate(self.batches, 1):
                desc = ', '.join(f'{nm(p)}→{dir_name(d)}' for p, d in b)
                lines.append(f'  第{bi}批 ({len(b)}件): {desc}')
        return '\n'.join(lines)


def disassemble(idx, lab, pids, dirs=DIRS26, batch: bool = True,
                volumes=None) -> DisassemblyReport:
    """贪心拆解：每轮取所有出度为 0 的零件（batch）或体积最小的一个。"""
    n = len(pids)
    alive = np.ones(n, dtype=bool)
    rep = DisassemblyReport(ok=True, pids=list(pids))
    guard = 0
    while alive.any() and guard < n + 5:
        guard += 1
        blk = blocking(idx, lab, dirs, alive)
        free = []
        for i in range(n):
            if not alive[i]:
                continue
            ok_dirs = [int(k) for k in np.flatnonzero(~blk[i])]
            if ok_dirs:
                free.append((i, ok_dirs))
        if not free:
            rep.ok = False
            rep.locked = [i for i in range(n) if alive[i]]
            return rep
        if not batch and volumes is not None:
            free.sort(key=lambda t: volumes[t[0]])
            free = free[:1]
        b = []
        for i, ok_dirs in free:
            # 优先选轴向（工程师能理解的方向），否则取第一个
            pref = None
            for k in ok_dirs:
                if tuple(int(x) for x in dirs[k]) in DIR_NAMES:
                    pref = k
                    break
            if pref is None:
                pref = ok_dirs[0]
            b.append((i, tuple(int(x) for x in dirs[pref])))
            rep.free_dirs[pids[i]] = [dir_name(dirs[k]) for k in ok_dirs]
            alive[i] = False
        rep.batches.append(b)
    return rep


# ---------------------------------------------------------------- 精确判据
#
# 体素扫描线快，但有个硬限制：装配间隙 0.15mm 远小于体素 0.6mm，两个零件的
# 体素会落进同一格，扫描线分不出「谁在前」。实测这会让整套判据失效
# （37 件全部"一批拆完"，连套在柱上的环都能侧向抽出）。
#
# 所以最终判据换成几何精确的：把零件 i 沿 d 平移一步 delta，直接布尔求交。
# 相交 ⇒ 该方向上 i 起步就被 j 卡住 ⇒ 沿 d 不可拆。
# delta 取 2mm：大于最大装配间隙（0.15）、小于最小特征（3mm 销）。
#
# 代价是布尔次数，所以只算一次全场的 blocker 表；之后靠单调性推导拆解序列
# ——「移除零件只会减少阻挡」，所以 blockers 里只要有活着的成员就还挡着，
# 全部移除即解锁，不必每轮重算布尔。


# 平移采样距离（mm）。必须多级：只试 2mm 会漏掉「滑动 2mm 没事、滑 6mm 撞墙」
# 的情况 —— 胸核三层套筒正是这样被误判成能沿轴向穿出的。
# 间距取几何级数并加密，保证薄壁（最薄 0.65mm）不会被整段跨过去。
DELTAS = (2.0, 3.0, 4.5, 6.5, 9.5, 14.0, 20.0, 30.0, 45.0, 67.0,
          100.0, 150.0, 225.0, 330.0)


def _aabb(b):
    return np.array([[b.min.X, b.min.Y, b.min.Z],
                     [b.max.X, b.max.Y, b.max.Z]])


def _aabb_hit(lo1, hi1, lo2, hi2):
    return bool(np.all(lo1 <= hi2) and np.all(hi1 >= lo2))


def escape_distance(bb_i, union, d):
    """沿 d 平移，零件包围盒要完全脱离整体包围盒所需的最短距离（O(1)）。

    纯粹用来给**可行方向**排序：人手拆的时候一定选"最近就能脱开"的方向，
    而不是"理论上也能出、但得横穿整台机器"的方向。
    可行性由 blockers 保证，这里只管优先级。
    """
    lo, hi = bb_i
    ulo, uhi = union
    best = float('inf')
    for a in range(3):
        if d[a] > 0:
            t = (uhi[a] - lo[a]) / d[a]
        elif d[a] < 0:
            t = (ulo[a] - hi[a]) / d[a]
        else:
            if hi[a] <= ulo[a] or lo[a] >= uhi[a]:
                return 0.0
            continue
        best = min(best, max(t, 0.0))
    return best


def blockers_exact(parts, dirs=DIRS26, deltas=DELTAS, tol_mm3: float = 0.02,
                   verbose: bool = False):
    """返回 blockers[i][k] = 阻挡零件 i 沿 dirs[k] 平移的零件索引集合。"""
    from build123d import Pos
    from cpkit import _inter_volume
    n = len(parts)
    nd = len(dirs)
    bbs = [_aabb(p.solid.bounding_box()) for p in parts]
    blockers = [[set() for _ in range(nd)] for _ in range(n)]
    for i, pi in enumerate(parts):
        for k, d in enumerate(dirs):
            dv = np.array([float(x) for x in d])
            hit = set()
            for j, pj in enumerate(parts):
                if j == i or j in hit:
                    continue
                bj = bbs[j]
                for t in deltas:
                    lo, hi = bbs[i][0] + dv * t, bbs[i][1] + dv * t
                    if not _aabb_hit(lo, hi, bj[0], bj[1]):
                        continue
                    moved = Pos(*(dv * t)) * pi.solid
                    if _inter_volume(moved, pj.solid) > tol_mm3:
                        hit.add(j)
                        break
            blockers[i][k] = hit
        if verbose:
            print(f'  {i + 1}/{n} {parts[i].pid}')
    return blockers


def disassemble_blockers(blockers, pids, dirs=None, parts=None, volumes=None):
    """由精确 blocker 表推出拆解序列（靠单调性，无需重算几何）。"""
    dirs = DIRS26 if dirs is None else dirs
    # 方向优先级：脱开距离最短者优先
    esc = None
    if parts is not None:
        bbs = [_aabb(p.solid.bounding_box()) for p in parts]
        union = (np.min([b[0] for b in bbs], axis=0),
                 np.max([b[1] for b in bbs], axis=0))
        esc = [[escape_distance(bbs[i], union, np.array([float(x) for x in d]))
                for d in dirs] for i in range(len(parts))]
    n = len(pids)
    alive = np.ones(n, dtype=bool)
    rep = DisassemblyReport(ok=True, pids=list(pids))
    guard = 0
    while alive.any() and guard < n + 5:
        guard += 1
        free = []
        for i in range(n):
            if not alive[i]:
                continue
            ok = [k for k in range(len(dirs)) if not (blockers[i][k] & set(np.flatnonzero(alive)))]
            if ok:
                if esc is not None:
                    ok.sort(key=lambda k: esc[i][k])
                free.append((i, ok))
        if not free:
            rep.ok = False
            rep.locked = [i for i in range(n) if alive[i]]
            return rep
        b = []
        for i, ok in free:
            pref = ok[0]
            b.append((i, tuple(int(x) for x in dirs[pref])))
            rep.free_dirs[pids[i]] = [dir_name(dirs[k]) for k in ok]
            alive[i] = False
        rep.batches.append(b)
    return rep
