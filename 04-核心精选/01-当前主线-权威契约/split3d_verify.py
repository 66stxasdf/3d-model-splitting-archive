# -*- coding: utf-8 -*-
"""分件领域的校验器 —— **全部待填**。

校验器是这套东西里唯一能挡住「工具自称完成」的机制，所以它必须**独立**：
从磁盘上的产物重新算，不采信工具报的数字。

写校验器时容易犯的四个错，这里先写明：

1. **拿工具报的值去校验工具报的值。** 比如工具说"体积 100"，校验器读 values
   看到 100 就通过——这不是校验，这是确认别人刚说过的话。
   正确做法：从 STL 重新算体积，再和 values 里的比。

2. **用文本模式读文件。** Windows 上 \\r\\n → \\n 的转换会让两侧算出的字节数差几十，
   守恒式永远对不平。文本一律按字节读（见 `_text`）。

3. **把"没算出来"当成"通过"。** 校验器抛异常时，内核会判 VERIFY_ERROR 并停止，
   这是对的。千万不要 try/except 之后返回 ok=True。

4. **不认实例。** 按实例的校验器必须用 `payload["instance"]`，
   否则可能拿第 3 件的产物去证明第 5 件做对了。

签名：fn(payload) -> {"ok": bool, "why": str, "detail": {...}}
payload 里有：
    payload["fact"]        被校验的事实 id
    payload["instance"]    本次是哪一个实例（一次性事实为 None）
    payload["values"]      **本次**工具返回的 values
    payload["artifacts"]   本次工具报的产物
    payload["world"]["values"]  **整个世界**的值，且已合并当前实例的私有值
    payload["world"]["facts"]   已完成的事实（按实例的形如 `part_clean@head`）
    payload["const"]       包常量（阈值都在这里，不要在校验器里写魔法数字）
    payload["run_dir"] / payload["workspace"] / payload["task"]
"""

import hashlib
import json
import os

import trimesh


def _R(ok, why="", **detail):
    return dict(ok=bool(ok), why=why, detail=detail)


def _all(payload):
    """整个世界值 + 本次产出（后者覆盖前者）。

    校验器要判的是「世界在这一点是否成立」，不是「这个工具自己报了什么」。
    只看本次返回值，就变成让工具自己给答案对答案了。
    """
    v = dict(((payload.get("world") or {}).get("values") or {}))
    v.update(payload.get("values") or {})
    return v


def _text(path):
    """按字节读。理由见本文件顶部第 2 条。"""
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def _todo(name, what):
    raise NotImplementedError(f"校验器 {name} 尚未实现：{what}")


# ------------------------------------------------------------------ 准入

def _sha256(path, blocksize=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(blocksize), b""):
            h.update(chunk)
    return h.hexdigest()


def _scene_to_mesh(scene):
    try:
        geoms = [g for g in scene.geometry.values() if hasattr(g, "faces")]
        if not geoms:
            return None
        if len(geoms) == 1:
            return geoms[0]
        return trimesh.util.concatenate(geoms)
    except Exception:
        return None


# 单位 plausibility 守卫：AIGC 网格常把米当毫米导出（症状恰好是"全线崩盘"，
# 见 parts_available 校验器 hint）。超过此范围直接拒，逼人确认单位/重导出。
DIM_MIN_MM = 0.5
DIM_MAX_MM = 5000.0


def input_ok(payload):
    """入厂质检的校验：不信工具报的任何数，从磁盘独立重算。

    重算项：文件存在且非空、sha256 与 values.src_sha 一致、面数与工具报的相符、
    包围盒×scale 得到的最大尺寸落在合理 mm 范围（单位 plausibility 守卫）。
    水密性只重算并回报，**不**据此拒收——非水密网格由下游 prep_lod 焊接修复，
    input_ok 含义是"被准入处理"，与工具侧阈值口径一致。

    注意：这是"校验器判死"机制唯一能拦住"工具自称完成"的地方，
    所以宁可保守——任何拿不准的就返回 ok=False 让内核停下，绝不让 try/except 吞掉。
    """
    v = _all(payload)
    model_path = v.get("model_path")
    if not model_path or not os.path.isfile(str(model_path)):
        return _R(False, "input_ok 校验失败：model_path 不存在或不是文件",
                  model_path=model_path)

    # 独立重算 sha256（工具报的数字不可信）
    try:
        actual_sha = _sha256(str(model_path))
    except Exception as e:
        return _R(False, f"input_ok 校验失败：读取文件失败 {e}")
    src_sha = v.get("src_sha")
    if src_sha and str(src_sha) != actual_sha:
        return _R(False, "input_ok 校验失败：src_sha 与磁盘文件不一致（文件被改过？）",
                  want=str(src_sha), got=actual_sha)

    # 独立重算几何
    try:
        mesh = trimesh.load(str(model_path), process=False)
    except Exception as e:
        return _R(False, f"input_ok 校验失败：网格加载失败 {e}")
    if isinstance(mesh, trimesh.Scene):
        mesh = _scene_to_mesh(mesh)
    if mesh is None or not hasattr(mesh, "faces") or len(mesh.faces) == 0:
        return _R(False, "input_ok 校验失败：网格为空或无法解析为三角网格")

    # 焊接重合顶点：STL 按面存顶点，不 weld 水密性/边界边会全误判。
    # 与工具侧口径一致——校验器重算时也要 weld，否则会和自己算的不符。
    try:
        mesh.merge_vertices()
    except Exception:
        pass

    faces = int(len(mesh.faces))
    tool_faces = v.get("faces")
    if tool_faces is not None and int(tool_faces) != faces:
        return _R(False,
                  f"input_ok 校验失败：面数与工具报的不符"
                  f"（工具 {tool_faces} / 实算 {faces}）",
                  tool_faces=int(tool_faces), actual_faces=faces)

    watertight = bool(mesh.is_watertight)

    # 单位 plausibility：独立重算包围盒 × scale
    scale = v.get("scale")
    dim_max_mm = None
    try:
        if mesh.bounds is not None and scale:
            ext_vec = mesh.bounds[1] - mesh.bounds[0]
            dim_max_mm = float(max(ext_vec)) * float(scale)
    except Exception:
        dim_max_mm = None
    if dim_max_mm is not None and (dim_max_mm < DIM_MIN_MM or dim_max_mm > DIM_MAX_MM):
        return _R(False,
                  f"SCALE_SUSPECT: 最大尺寸 {dim_max_mm:.1f}mm 超出合理范围 "
                  f"[{DIM_MIN_MM}, {DIM_MAX_MM}]——疑似单位制错误"
                  f"（AIGC 常导出米制未归一）。请确认单位或重导出。",
                  dim_max_mm=round(dim_max_mm, 2),
                  scale=scale, scale_source=v.get("scale_source"))

    return _R(True,
              f"input_ok：文件存在、sha 一致、面数自洽、尺度合理"
              f"（{faces} 面 / 水密={watertight} / 最大≈{dim_max_mm:.1f}mm）",
              faces=faces, watertight=watertight,
              dim_max_mm=None if dim_max_mm is None else round(dim_max_mm, 2))


def lod_ready(payload):
    """降面结果的校验：面数是否接近约定值、降面后是否仍然水密。

    只检查工具报的 lod_faces 是不够的——面数和文件大小必须自洽。
    """
    _todo("lod_ready", "重算面数与水密性；面数与文件大小必须自洽")


def seam_ok(payload):
    """割线准入的校验：闭合、首尾间距、回折、平面拟合残差。

    这些几何判断必须自己重算。工具报 rms=0.1 而实际是 3.0 的情况，
    只有独立拟合才能发现。
    """
    _todo("seam_ok", "自己重算首尾间距、回折角、平面拟合 rms")


def route_known(payload):
    """分支选择的校验：branch 的取值必须与 rms 和阈值自洽。

    这一条最容易被忽略：工具完全可能选了 plane 而 rms 远超阈值，
    之后切出来的东西不对，却没人知道是路由那一步就错了。
    """
    _todo("route_known", "用 rms 与 const.coplanar_rms_mm 重算应有分支，"
                         "与 values.branch 比对")


# ------------------------------------------------------------- 路线 A 的产出

def _load_stems(parts_dir):
    """parts_dir 下的 STL 文件名（去扩展名）集合。

    按磁盘实数，不信工具报的清单——清单撒谎正是要拦的那种错。
    """
    import glob
    out = set()
    for p in glob.glob(os.path.join(parts_dir, "*")):
        if os.path.isfile(p) and p.lower().endswith(".stl"):
            out.add(os.path.splitext(os.path.basename(p))[0])
    return out


def _load_meshes(paths):
    """读 STL 为 trimesh 网格。process=False：不重合法向/并顶点，
    我们要的就是盘上原样的几何；加载失败照常抛异常——
    校验器抛异常内核会判 VERIFY_ERROR 并停止，这比"try 掉之后放行"正确。"""
    import trimesh
    return [trimesh.load(p, process=False) for p in paths]


_SEMANTIC_WORDS = ("head", "arm", "leg", "hand", "foot", "torso", "body",
                   "neck", "eye", "hair", "shoe", "bag", "face", "ear",
                   "头", "手", "臂", "腿", "脚", "躯干", "眼", "发", "鞋", "背")


def _verify_cut_pieces(v, part_ids, parts_dir, spec, const):
    """割线切割产物的几何判定支——**不需要语义真值**，因为这一支本来就不产生语义。

    为什么可以判：`cut_plane` 自己承诺了四条几何性质，每一条都能从磁盘重算，
    与「分对没分对」无关：

      1. 命名只能是位置（piece_a/piece_b），出现 head/arm 这类词即违约——
         它切出来的是几何块，编语义名会污染下游，让人以为语义分件成功了
      2. 每件必须水密、单个连通体（否则后面 clean/verify 全是空中楼阁）
      3. 每件顶点必须落在平面**同一侧**（容差 diag×1e-4，覆盖 float32 量化），
         且两件分居两侧——这是"切开了"的定义，不是"体积差不多"的近似
      4. 体积守恒 ΣV_i ≈ V_src（容差 const.cut_volume_tol），
         且并集包围盒 ≈ 源包围盒——两件合起来必须是原来那个东西

    注意 3 与 4 的分工：单看体积守恒，把一个球切成两半再各自平移也是守恒的；
    单看分侧，切下一片薄皮也算分侧。四条一起才等于「沿该平面切成两块」。
    """
    import numpy as np

    n = np.asarray(spec.get("n"), dtype=float)
    c = np.asarray(spec.get("c"), dtype=float)
    ln = float(np.linalg.norm(n))
    if ln < 1e-12:
        return _R(False, "cut_plane_spec.n 是零向量，无法判定切割面", spec=spec)
    n = n / ln

    # ---- 1) 命名：只能是位置 ----
    bad_names = [p for p in part_ids
                 if any(w in str(p).lower() for w in _SEMANTIC_WORDS)]
    if bad_names:
        return _R(False,
                  f"cut_plane 的产物不能用语义名：{bad_names}。"
                  f"它切出来的是几何块，只能按位置命名（piece_a/piece_b）——"
                  f"编 head/arm 会让人误以为语义分件成功",
                  semantic_names=bad_names, part_ids=part_ids)
    if len(part_ids) != 2:
        return _R(False,
                  f"单平面切分应恰好 2 件，实际 {len(part_ids)} 件：{part_ids}",
                  part_ids=part_ids)

    # ---- 2) 逐件：水密 + 单连通 + 体积 ----
    vols, side_mean, side_min, side_max, nverts = {}, {}, {}, {}, {}
    for pid in part_ids:
        path = os.path.join(parts_dir, pid + ".stl")
        m = trimesh.load(path, process=False)
        if isinstance(m, trimesh.Scene):
            m = trimesh.util.concatenate(
                [g for g in m.geometry.values() if hasattr(g, "faces")])
        try:
            m.merge_vertices()
        except Exception:
            pass
        if len(m.faces) == 0:
            return _R(False, f"{pid} 读出来是空网格：{path}", part=pid)
        if not m.is_watertight:
            return _R(False, f"{pid} 不水密（边界边非 0），切完的口没封上",
                      part=pid, faces=int(len(m.faces)))
        comps = m.split(only_watertight=False)
        if len(comps) != 1:
            return _R(False, f"{pid} 不是单个连通体（{len(comps)} 块），"
                             f"说明布尔碎屑没剔干净", part=pid, components=len(comps))
        vols[pid] = abs(float(m.volume))
        if vols[pid] <= 0:
            return _R(False, f"{pid} 体积为 0", part=pid)

        # ---- 3) 分侧 ----
        dots = (np.asarray(m.vertices, dtype=float) - c) @ n
        nverts[pid] = int(len(dots))
        side_mean[pid] = float(dots.mean())
        side_min[pid] = float(dots.min())
        side_max[pid] = float(dots.max())

    diag = float(spec.get("diag") or 0.0)
    if diag <= 0:
        bb = np.vstack([trimesh.load(os.path.join(parts_dir, p + ".stl"),
                                     process=False).bounds for p in part_ids])
        diag = float(np.linalg.norm(bb[1] - bb[0]))
    eps = max(diag * 1e-4, 1e-12)

    beyond = {}
    for pid in part_ids:
        over_pos = side_max[pid] - eps          # 越过 +n 侧的量
        over_neg = -side_min[pid] - eps         # 越过 -n 侧的量
        beyond[pid] = dict(pos=max(0.0, over_pos), neg=max(0.0, over_neg),
                           side=float(np.sign(side_mean[pid])) or 0.0)
    crossed = [pid for pid in part_ids
               if beyond[pid]["pos"] > 0 and beyond[pid]["neg"] > 0]
    if crossed:
        return _R(False,
                  f"{crossed} 在平面两侧都有顶点——没沿这个平面切开"
                  f"（刀片半宽不足或刀没覆盖整个截面）",
                  beyond=beyond, eps=eps)
    sides = [beyond[pid]["side"] for pid in part_ids]
    if sides[0] * sides[1] >= 0:
        return _R(False, f"两件落在平面同侧（{sides}），不是一切两块",
                  beyond=beyond)

    # ---- 4) 体积守恒 + 包围盒 ----
    vol_sum = float(sum(vols.values()))
    vol_src = spec.get("vol_src")
    src_path = spec.get("src")
    if src_path and os.path.isfile(str(src_path)):
        sm = trimesh.load(str(src_path), process=False)
        if isinstance(sm, trimesh.Scene):
            sm = trimesh.util.concatenate(
                [g for g in sm.geometry.values() if hasattr(g, "faces")])
        try:
            sm.merge_vertices()
        except Exception:
            pass
        vol_src = abs(float(sm.volume))
        bbox_src = np.asarray(sm.bounds, dtype=float)
    else:
        bbox_src = None
    if not vol_src or float(vol_src) <= 0:
        return _R(False, "拿不到源体积（spec.vol_src 与 spec.src 都不可用），"
                         "守恒式无从校验", vols=vols)
    vol_src = float(vol_src)
    rel = abs(vol_sum - vol_src) / vol_src
    tol = float(const.get("cut_volume_tol", 1e-3))
    detail = dict(vol_pieces=vols, vol_sum=vol_sum, vol_src=vol_src,
                  vol_rel_err=rel, tol_volume=tol,
                  side_mean=side_mean, eps=eps, n_vertices=nverts,
                  plane_from=spec.get("plane_from"), R=spec.get("R"),
                  section_extent=spec.get("section_extent"))
    if rel > tol:
        return _R(False,
                  f"体积不守恒：两件合计 {vol_sum:.6g} vs 源 {vol_src:.6g}，"
                  f"相对误差 {rel:.3g} > {tol:g}", **detail)

    if bbox_src is not None and len(part_ids) == 2:
        bb = []
        for pid in part_ids:
            mm = trimesh.load(os.path.join(parts_dir, pid + ".stl"), process=False)
            bb.append(np.asarray(mm.bounds, dtype=float))
        uni = np.vstack(bb)
        lo, hi = uni.min(axis=0), uni.max(axis=0)
        d = float(np.abs(np.r_[lo - bbox_src[0], hi - bbox_src[1]]).max())
        detail["bbox_union_gap"] = d
        if d > max(diag * 1e-3, 1e-9):
            return _R(False,
                      f"两件并集包围盒与源差 {d:.6g}（> 对角线 1e-3），"
                      f"说明有几何丢失", **detail)

    return _R(True,
              f"沿平面切成 2 块：体积守恒误差 {rel:.2g}，两件分居两侧，"
              f"各为单个封闭体，命名合规", **detail)


def parts_available(payload):
    """零件清单的校验：这是后面所有按实例展开的依据，错了后面全错。

    分三层，顺序不能反：

    1. **形态层**（与有没有真值无关，总是先查）：
       part_ids 是非空无重复的字符串列表；parts_dir 里清单内外一致
       （每件都有文件、磁盘上没有清单外的文件）。
    1.5 **割线切割支**（values 里有 cut_plane_spec、且没带 ref_parts 时走这条）：
       人工割线切出来的件没有语义，所以不能拿语义真值卡它。但它承诺了四条
       几何性质——只能按位置命名、每件水密且单连通、两件分居平面两侧、
       体积守恒且并集包围盒对上源——四条都能从磁盘重算（`_verify_cut_pieces`）。
       有规范不判＝放行，所以这条必须走，不能因为"没真值"就一律 UNKNOWN。
    2. **质量层**（有 ref_parts 才做）：
       用 `split3d_segmetrics` 的表面采样 + 最近件归属算 per-part IoU
       与边界偏差（chamfer），匈牙利匹配后：
         · 漏分（真值有、产物无）/ 多分（产物有、真值无）→ 不过
         · 匹配对 IoU 低于合格线 → 不过
       合格线 min_iou 由人声明（任务单 inputs.min_iou），
       缺省取 const.semantic_min_iou；const.semantic_match_floor 是
       「多少算可匹配」的算法参数，两者不是一回事，不要混。
    3. **无 ref_parts → UNKNOWN 拒收**：
       「零件是否分对」的判定标准只能来自人标的真值。没有尺子就不判通过，
       报 UNKNOWN——这是任务单没带尺子，不是工具的错，why 里写清，
       病因要和 VERIFY_FAILED 类（工具真错了）区分开。

    实现要点：
      · 全部数字从磁盘重算（采样种子固定，可复算），不采信工具报的值
      · 单对比较（1 真值件 vs 1 工具件）恒为 IoU=1、没有分辨力，
       这是度量性质不是缺陷；detail 里记 single_pair 提示，不拦
      · 若所有 IoU 都极低（<0.1），why 里提示先查单位制
        （AIGC 导出常是 m 而不是 mm，症状恰好是"全线崩盘"）
    """
    import split3d_segmetrics as SM   # 包内模块：pack root 已在 sys.path

    v = _all(payload)
    part_ids = v.get("part_ids")
    parts_dir = v.get("parts_dir")
    inputs = ((payload.get("task") or {}).get("inputs")) or {}
    const = payload.get("const") or {}

    # ---- 1) 形态层 ----
    if not isinstance(part_ids, list) or not part_ids:
        return _R(False, "part_ids 必须是非空列表", part_ids=part_ids)
    if not all(isinstance(x, str) and x.strip() for x in part_ids):
        return _R(False, "part_ids 必须全是非空字符串", part_ids=part_ids)
    dup = sorted({x for x in part_ids if part_ids.count(x) > 1})
    if dup:
        return _R(False, f"part_ids 有重复：{dup}", duplicates=dup)
    if not parts_dir or not os.path.isdir(str(parts_dir)):
        return _R(False, f"parts_dir 不存在或不是目录：{parts_dir}")
    stems = _load_stems(str(parts_dir))
    want = set(part_ids)
    missing_files = sorted(want - stems)
    extra_files = sorted(stems - want)
    if missing_files or extra_files:
        return _R(False,
                  f"清单与磁盘不一致：缺文件 {missing_files or '无'}，"
                  f"清单外文件 {extra_files or '无'}",
                  missing_files=missing_files, extra_files=extra_files)

    # ---- 2) 割线切割支：有 cut_plane_spec 就走几何判定 ----
    # 割线切割本来就不产生语义，所以不该拿语义真值去卡它；但它承诺了四条
    # 几何性质（命名按位置、各自水密单连通、分居两侧、体积守恒），
    # 这四条都能从磁盘重算，见 _verify_cut_pieces。放着不判等于放行。
    cut_spec = v.get("cut_plane_spec")
    if isinstance(cut_spec, dict) and cut_spec.get("n") is not None \
            and not inputs.get("ref_parts"):
        return _verify_cut_pieces(v, part_ids, str(parts_dir), cut_spec, const)

    # ---- 3) 无真值：UNKNOWN 拒收 ----
    ref_dir = inputs.get("ref_parts")
    if not ref_dir:
        return _R(False,
                  "UNKNOWN: 任务单未提供 ref_parts（真值标签），"
                  "「零件是否分对」没有判定标准——拒收。"
                  "注意：这是这次运行没带尺子，不是工具做错了",
                  unverifiable="no_ref_parts",
                  n_parts=len(part_ids), part_ids=part_ids)
    if not os.path.isdir(str(ref_dir)):
        return _R(False, f"ref_parts 指向的目录不存在：{ref_dir}")
    man_path = os.path.join(str(ref_dir), "manifest.json")
    if not os.path.isfile(man_path):
        return _R(False, f"真值目录里没有 manifest.json：{ref_dir}")
    man = json.loads(_text(man_path))
    ref_parts = man.get("parts") or []
    if not ref_parts:
        return _R(False, f"真值 manifest 没有 parts 清单：{ref_dir}")
    ref_files, ref_ids = [], []
    for p in ref_parts:
        fp = os.path.join(str(ref_dir), "parts", str(p.get("file") or ""))
        if not os.path.isfile(fp):
            return _R(False, f"真值文件缺失：{p.get('file')}")
        ref_files.append(fp)
        ref_ids.append(str(p.get("id")))

    # ---- 3) 质量层：表面采样 IoU + 边界偏差 ----
    tool_files = [os.path.join(str(parts_dir), i + ".stl") for i in part_ids]
    ref_meshes = _load_meshes(ref_files)
    tool_meshes = _load_meshes(tool_files)
    iou = SM.agreement_matrix(ref_meshes, tool_meshes)
    floor = float(const.get("semantic_match_floor", SM.DEFAULT_MATCH_FLOOR))
    min_iou = float(inputs.get("min_iou") or const.get("semantic_min_iou") or 0.6)
    pairs, missed, extra = SM.match(iou, floor=floor)

    pair_rows = []
    for i, j, s in pairs:
        ch = SM.pair_chamfer(ref_meshes[i], tool_meshes[j])
        pair_rows.append(dict(ref=ref_ids[i], tool=part_ids[j],
                              iou=round(float(s), 4), **ch))
    pair_rows.sort(key=lambda r: r["iou"])

    detail = dict(
        n_ref=len(ref_ids), n_tool=len(part_ids),
        match_floor=floor, min_iou=min_iou,
        pairs=pair_rows,
        missed_ref=[ref_ids[i] for i in missed],
        extra_tool=[part_ids[j] for j in extra],
        single_pair_note=(len(ref_ids) == 1 or len(part_ids) == 1),
    )
    if all(r["iou"] < 0.1 for r in pair_rows) and pair_rows:
        detail["hint"] = ("所有匹配对 IoU 都低于 0.1——先查单位制"
                          "（AIGC 导出常为米制未归一化，症状恰好是全线崩盘）")

    if missed:
        return _R(False,
                  f"漏分 {len(missed)} 件（真值里有、产物里没有）："
                  f"{detail['missed_ref']}", **detail)
    if extra:
        return _R(False,
                  f"多分 {len(extra)} 件（产物里有、真值里没有）："
                  f"{detail['extra_tool']}", **detail)
    bad = [r for r in pair_rows if r["iou"] < min_iou]
    if bad:
        return _R(False,
                  f"{len(bad)} 对 IoU 低于合格线 {min_iou}："
                  + "，".join(f"{r['ref']}↔{r['tool']}={r['iou']:.3f}"
                             for r in bad[:5]), **detail)
    return _R(True, f"{len(pair_rows)} 对全部达到 IoU≥{min_iou}，"
                    f"无漏分无多分", **detail)


# ------------------------------------------------------------- 按实例（零件）

def _inst(payload):
    v = payload.get("instance")
    if not v:
        raise ValueError("这次动作没有实例信息——按实例的事实必须带实例")
    return v


def part_clean(payload):
    """清理结果的校验：必须是单个封闭体，不能残留碎片。

    独立做法：重新读盘算连通块数、边界边数、体积；
    与工具报的 clean_volume 比对（相对误差应小于 1e-3）。
    """
    _todo("part_clean", "从盘上重算连通块/边界边/体积，与工具报的比对")


def part_verified(payload):
    """复验结果的校验：壁厚要**沿法向射线投射**重算。

    减分点提示：用「到最近表面的距离」算壁厚，在凹处会给出错误的大值。
    必须沿法向双向投射，取最近命中距离。
    """
    _todo("part_verified", "沿法向射线投射重算最小壁厚，与工具报的比对")


# ------------------------------------------------------------- 按实例（连接）

def contacts_found(payload):
    """接触面的校验：两两检查，且双向结果必须一致。

    要查：
      · pair_ids 格式正确（`A__B`），A/B 都在 part_ids 里
      · 每一对在 contacts_dir 里都有数据
      · 双向交叉验证：A 侧测出的面积与 B 侧独立测出的差 < 1%
        （差太多说明是采样噪声，不是真接触）
      · 面积达到 const.min_contact_area_mm2
    """
    _todo("contacts_found", "核对 pair_ids 合法、每对有数据，且双向面积差 < 1%")


def joint_built(payload):
    """连接的校验：销孔是否真的在几何上成立。

    要查（从网格量，不用名义值）：
      · 销真的长在 A 上（销的体积 > 0，且与 A 连通）
      · 孔真的开在 B 上（孔的体积应使 B 的体积减少对应量）
      · 销位离边缘够远、所在处壁厚够——这两个条件不满足时，
        打印出来销会断或者孔会破
    """
    _todo("joint_built", "从网格量销与孔的体积，确认销长在 A、孔开在 B")


def joints_verified(payload):
    """连接复验的校验：实测间隙、余量、干涉。

    实测间隙 = 孔的实测内径 − 销的实测外径，两边都从网格量。
    拿名义值相减是自欺：名义 0.2 而实际 0.05 的情况，打印出来就是装不进去。
    """
    _todo("joints_verified", "从网格量销外径与孔内径，重算实际间隙")


# ------------------------------------------------------------- 按实例（交付）

def part_exported(payload):
    """导出的校验：文件在、内容与非导出态的网格一致、且单独可控。

    要查：文件存在、3MF 能被解析、STL 面数与源一致、每件单独一个文件
    （全部塞进一个文件的话就不能分开打印）。
    """
    _todo("part_exported", "核对 STL/3MF 存在、可解析、面数与源一致")


def assembled_ok(payload):
    """装配的校验：整体拼起来不互穿、没有悬空件、重心在支撑多边形内。

    这三条是「站得住」的几何条件。真正的"站着不倒"要靠物理仿真，
    这里只判几何上有没有明显不可能的地方。
    """
    _todo("assembled_ok", "重算装配交集体积、连通性、重心与支撑多边形")
