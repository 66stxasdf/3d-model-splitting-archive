# -*- coding: utf-8 -*-
"""分件领域的工具实现 —— **全部待填**。

这是给「负责做能力」的那个人看的活清单，不是示例代码。
每个函数上面写了：该做什么、必须往 values 里写什么、不合格该返回哪个 reason_code。

几条交叉约定，填写时请务必遵守，否则内核会拒收或者校验器会对不上：

1. `values` 里只放**小摘要**（路径、面数、体积、指纹）。
   几何本身写文件，路径放进 values。run.json 是给人读的，不要塞网格。
2. `artifacts` 里每个条目给 `role`，role 名会被后面的工具用来找上游产物，
   也会出现在交付报告里。改名等于改契约，要和 manifest 一起改。
3. 失败必须给 `reason_code`，而且要能归类。`UNKNOWN_FAILURE` 会让回退决策失去依据。
4. **不要自己判断"我做完了"。** 返回 `ok=True` 只是"我认为我做了"，
   真正的结论由 `split3d_verify.py` 里的校验器从磁盘重算。
5. **按实例的工具（on_fail=skip_instance）只处理一件。**
   内核会把实例参数注入进 `inp["params"]`，一次调用只管一件。
   不要在里面写 for 循环把所有件都做完——那样「第 3 件坏了」就表达不出来，
   内核会以为整批都成功了。
6. 时长是真实开销。`cost_s` 写小了，规划器会在同等条件下优先选便宜的路，进而误判。

输入 `inp` 的结构（由内核构造，只读）：
    inp["params"]     契约里声明并已解析的参数（路径已展开成绝对路径）
                      **按实例执行时，这里还有内核注入的实例参数**
    inp["instance"]   本次处理的是哪一件（一次性工具为 None）
    inp["task"]       task_id / tag / goals / inputs / constraints
    inp["world"]      当前事实与值的快照
                      · world["values"] 已经把当前实例的私有值合并进来了，
                        所以照常读 values["clean_path"] 读到的就是**这一件**的
                      · world["done_instances"] 这个环节已完成的实例
    inp["artifacts"]  已产出的产物，按 role 索引（按实例的会带 @实例名）
    inp["run_dir"]    本次运行目录（中间产物写这里）
    inp["workspace"]  工作区
    inp["step"]       当前步号

返回值的写法：
    dict(ok=True, facts=["事实id"],
         values={...},            # 小摘要；按实例执行时会落进该实例的私有值域
         values_global={...},     # 要跨实例共享的值（比如总件数）
         artifacts=[dict(role=..., path=..., bytes=...)],
         message="一句人话")
    dict(ok=False, reason_code="...", message="...")   # 失败
"""

import hashlib
import json
import os

import trimesh


def _todo(name, spec):
    raise NotImplementedError(f"{name} 尚未实现。{spec}")


# ------------------------------------------------------------------ 入厂段公共工具

def _sha256(path, blocksize=1 << 20):
    """按字节分块算 sha256——网格可能几十 MB，别一次读进内存。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(blocksize), b""):
            h.update(chunk)
    return h.hexdigest()


def _scene_to_mesh(scene):
    """Scene → 单个合并网格。load(process=False) 下 STL/OBJ 通常直接是 Trimesh，
    这里只为兜住罕见的多体 scene（如某些 3MF 装配体）。"""
    try:
        geoms = [g for g in scene.geometry.values() if hasattr(g, "faces")]
        if not geoms:
            return None
        if len(geoms) == 1:
            return geoms[0]
        return trimesh.util.concatenate(geoms)
    except Exception:
        return None


def _boundary_edges(mesh):
    """独立算边界边（只被一个三角形共享的边）条数。水密 ⇔ 边界边为 0。"""
    inv = mesh.edges_unique_inverse
    from collections import Counter
    cnt = Counter(int(x) for x in inv)
    return sum(1 for c in cnt.values() if c == 1)


def _scale_for(path, ext):
    """单位标定：尽量从文件读真实单位，读不到就按 mm 约定（诚实标注）。

    返回 (scale_to_mm, scale_source)。
    STL/OBJ/PLY/OFF 没有内嵌单位——AIGC 网格惯例当 mm，但这是**假设**，
    所以 scale_source 记为 "assumed_mm"，下游/校验器据此判断可信度。
    3MF 在 XML 里写 <unit>，能拿到真实单位就优先用。
    """
    if ext == ".3mf":
        try:
            import re
            import zipfile
            with zipfile.ZipFile(path) as z:
                model_name = next((n for n in z.namelist()
                                   if n.lower().endswith("3dmodel.model")), None)
                if model_name:
                    txt = z.read(model_name).decode("utf-8", "replace")
                    m = re.search(r"<unit\s*>\s*([A-Za-z]+)\s*</unit>", txt)
                    if not m:
                        m = re.search(r"unit\s*=\s*[\"']?([A-Za-z]+)", txt)
                    if m:
                        unit = m.group(1).lower()
                        table = {"millimeter": 1.0, "millimetres": 1.0,
                                 "meter": 1000.0, "metre": 1000.0,
                                 "micron": 1.0e-3, "micrometre": 1.0e-3,
                                 "inch": 25.4, "foot": 304.8,
                                 "centimeter": 10.0, "centimetre": 10.0}
                        if unit in table:
                            return table[unit], f"3mf:{unit}"
        except Exception:
            pass
    return 1.0, "assumed_mm"


# ------------------------------------------------------------------ 准入

def probe_import(inp):
    """入厂质检：读网格，判断它值不值得往下做。

    职责边界（与契约一致）：只判断"能不能做 / 单位对不对"，
    **不判断"该怎么分"**——后者是 segment_semantic 的事。

    写进 values（小摘要，几何本身留盘）：
      · model_path / src_sha（sha256，后续每步据此确认"还是同一个模型"）
      · scale / scale_source（单位标定结果）
      · faces / components / boundary_edges / watertight / bbox / dim_max_mm /
        volume_mm3（几何体检，校验器会从盘重算，这里只做展示与留痕）

    不合格 reason_code：
      · FILE_UNREADABLE            文件不存在 / 加载失败 / 空网格
      · FORMAT_UNSUPPORTED         扩展名不在支持列表
      · MESH_NOT_WATERTIGHT        边界边占比 > 0.5，疑似破碎三角汤
      · MESH_TOO_MANY_COMPONENTS   连通块 > 100，疑似三角汤

    设计选择（务必读）：input_ok 含义是"**被准入处理**"，不是"已水密"。
    AIGC 烂网格由下游 prep_lod 焊接修复，所以非水密在这里**只记录不拒收**；
    只有破到一半边都开着（三角汤）才拒。这条阈值是"判定标准"，
    属技能层决定——收紧/放开改下方两个常量即可，不要在工具里写死别的值。
    """
    # 准入闸门阈值（判定标准，技能层可调）
    BROKEN_BOUNDARY_RATIO = 0.5     # 边界边 / 总边长占比上限
    MAX_COMPONENTS = 100            # 连通块数上限

    params = inp.get("params") or {}
    model = params.get("model")
    if not model:
        return dict(ok=False, reason_code="FILE_UNREADABLE",
                    message="未提供 model 路径（inputs.model 为空）")
    model = os.path.abspath(str(model))
    if not os.path.isfile(model):
        return dict(ok=False, reason_code="FILE_UNREADABLE",
                    message=f"模型文件不存在：{model}")
    ext = os.path.splitext(model)[1].lower()
    if ext not in (".stl", ".obj", ".3mf", ".ply", ".off"):
        return dict(ok=False, reason_code="FORMAT_UNSUPPORTED",
                    message=f"不支持的格式：{ext}（支持 stl/obj/3mf/ply/off）")

    try:
        mesh = trimesh.load(model, process=False)
    except Exception as e:
        return dict(ok=False, reason_code="FILE_UNREADABLE",
                    message=f"网格加载失败：{type(e).__name__}: {e}")
    if isinstance(mesh, trimesh.Scene):
        mesh = _scene_to_mesh(mesh)
    if mesh is None or not hasattr(mesh, "faces") or len(mesh.faces) == 0:
        return dict(ok=False, reason_code="FILE_UNREADABLE",
                    message="网格为空或无法解析为三角网格")

    # 焊接重合顶点：STL 按面存顶点，不 weld 则"每条边只属于一个面"，
    # 水密性与连通性会全部误判。admission 只要拓扑真相，weld 不影响面数。
    try:
        mesh.merge_vertices()
    except Exception:
        pass

    # ---- 几何量（全部独立可复算，input_ok 校验器会重算，这里只做展示/留痕）----
    faces = int(len(mesh.faces))
    comps = len(mesh.split(only_watertight=False))
    bnd = _boundary_edges(mesh)
    total_edges = int(len(mesh.edges_unique))
    degenerate = int((mesh.area_faces <= 0).sum()) if faces else 0
    watertight = bool(mesh.is_watertight)
    bounds = mesh.bounds
    ext_vec = (bounds[1] - bounds[0]).tolist() if bounds is not None else [0.0, 0.0, 0.0]
    dim_max = float(max(ext_vec)) if ext_vec else 0.0

    scale, scale_source = _scale_for(model, ext)
    dim_max_mm = dim_max * scale

    volume_mm3 = 0.0
    if watertight:
        try:
            volume_mm3 = float(mesh.volume) * (scale ** 3)
        except Exception:
            volume_mm3 = 0.0

    # ---- 准入闸门（只拦真正坏掉的，不拦 AIGC 常见的"待修复"非水密）----
    if total_edges and (bnd / total_edges) > BROKEN_BOUNDARY_RATIO:
        return dict(ok=False, reason_code="MESH_NOT_WATERTIGHT",
                    message=f"边界边占比 {bnd}/{total_edges} 过高，疑似破碎网格（三角汤）",
                    evidence=dict(faces=faces, boundary_edges=bnd, components=comps))
    if comps > MAX_COMPONENTS:
        return dict(ok=False, reason_code="MESH_TOO_MANY_COMPONENTS",
                    message=f"连通块数 {comps} 过多，疑似三角汤而非单件模型",
                    evidence=dict(faces=faces, components=comps))

    try:
        src_sha = _sha256(model)
    except Exception as e:
        return dict(ok=False, reason_code="FILE_UNREADABLE",
                    message=f"读取文件算 sha256 失败：{e}")

    values = dict(
        model_path=model,
        src_sha=src_sha,
        scale=scale,
        scale_source=scale_source,
        faces=faces,
        components=comps,
        boundary_edges=bnd,
        watertight=watertight,
        bbox_min=[float(x) for x in bounds[0]] if bounds is not None else None,
        bbox_max=[float(x) for x in bounds[1]] if bounds is not None else None,
        dim_max_mm=dim_max_mm,
        volume_mm3=round(volume_mm3, 2),
    )
    evidence = dict(
        faces=faces, components=comps, boundary_edges=bnd,
        degenerate_faces=degenerate, watertight=watertight,
        ext_file_units=ext_vec, scale=scale, scale_source=scale_source,
        dim_max_mm=round(dim_max_mm, 3), volume_mm3=round(volume_mm3, 2),
        note="非水密只记录不拒收；修复在 prep_lod。阈值 "
             f"{BROKEN_BOUNDARY_RATIO} 边界占比 / {MAX_COMPONENTS} 连通块可调",
    )
    return dict(ok=True, facts=["input_ok"], values=values, evidence=evidence,
                message=f"入厂质检通过：{faces} 面 / {comps} 连通块 / "
                        f"水密={watertight} / 尺度={scale_source}(×{scale}) / "
                        f"最大尺寸≈{dim_max_mm:.1f}mm")


def prep_lod(inp):
    """生成降面模型。

    应做：
      · 焊点（merge by distance，容差按包围盒对角线取 1e-5）
      · 降到 values 里约定面数（默认见 const.lod_faces）
      · 写 values.lod_path
      · 降面后必须重新检查水密性；降面把网格搞漏是很常见的
    reason_code：LOD_FAILED / LOD_NOT_WATERTIGHT
    """
    _todo("prep_lod", "values 写 lod_path；降面后要重新验水密")


# ------------------------------------------------------------- 路线 A：语义分件

def segment_semantic(inp):
    """语义部位识别与分件 —— **整条链最关键、也是目前唯一没有现成实现的一步**。

    要做的事：输入一个光滑整体的公仔网格，输出「有名字的部位」清单，
    每个部位各自成为一个可打印的实体。

    为什么难，先说清楚：
      源网格是**一张连续曲面**，没有预设的缝。哪些面属于头、哪些属于背包，
      不是几何属性，是语义。纯几何方法（平面拟合、曲率、最远点）做不出这个判断——
      实测过：几何自动选缝的 AUC 只有 0.5185，等于瞎猜。

    两条可走的路：

      甲、有 NVIDIA 显卡
        · PartField（ICCV 2025）：前馈式，约 10 秒，mIoU 79.18%。
          给定一个网格直接输出逐面片的部件归属特征。
        · SAMPart3D：零样本，不需要训练，但慢（5~6 分钟），精度低不少（约 34.7%）。
        · 落地要点：模型给出的是「面片 → 部件编号」，还要自己做三件事：
            ① 按编号把面片分成若干块
            ② 每块补成**封闭实体**（表面片是零厚度的，不能打印）——
               这一步叫 "补实体"，HoloPart（ICLR 2026）做的就是这件事
            ③ 相邻块之间要切得开、不能互穿

      乙、没有显卡
        · 人工标 6~8 个关键点（头顶、左肩、右肩、腰、左手、右手、脚…），
          算法在这些点之间推边界（测地线最短路径 / 热扩散）。
        · 精度取决于标注质量，但结果可解释、可复现、不需要 GPU。
        · 这条路的产出和甲一样：part_ids + 每件网格。

    共同要求（不管走哪条路）：
      · values.part_ids 必须是**字符串列表**，且每件之间有区分度，
        比如 ["head", "arm_l", "arm_r", "bag", "shoe_l", "shoe_r", "hair"]
      · values.parts_dir 写每件网格所在目录，文件名 `{part_id}.stl`
      · **名字要有意义**。这是整件事的价值所在：
        交给下游的名字是 "head"，不是 "piece_0"。下游的连接结构、报告、
        甚至打印时的摆放方式都依赖这个名字。

    必须处理但不一定一步做完的事（可以留 TODO，但要在返回值里说明）：
      · 眼睛、纽扣这类小件：要不要单独成件？太小打印不出来，通常并入头部
      · 头发这类薄片：直接打印会碎，要么加厚要么并入头部
      · 背包带这类细长件：强度和连接点都要单独考虑

    reason_code：SEGMENT_FAILED / NO_SEMANTIC_PARTS / GPU_UNAVAILABLE
    """
    _todo("segment_semantic",
          "values 必须写 part_ids（字符串列表）和 parts_dir；"
          "每件网格写到 parts_dir/{part_id}.stl，且必须是封闭实体")


# ------------------------------------------------------------- 路线 B：人工割线

def seam_ingest(inp):
    """割线准入：人工画的缝合法吗。

    应做：
      · 读割线 JSON，取出点列
      · 检查：闭合（首尾距离 < 容差）、无回折（相邻点方向夹角）、点数足够
      · 拟合平面，算残差 rms；写入 values.seam_norm（归一化后的点列）与 values.rms_mm
    不合格的 reason_code：
      · NOT_CLOSED / LOOP_NOT_CLOSED_GEOMETRICALLY / FOLD_BACK / TOO_FEW_POINTS

    注意：割线长什么样是人的决定，这里只判断"这条线能不能用来切"。
    """
    _todo("seam_ingest", "values 写 seam_norm；不合格要给可归类的 reason_code")


def route_branch(inp):
    """按割线的平整程度选切割方式。

    应做：
      · 用 values.rms_mm 和 const.coplanar_rms_mm 比较
      · rms 小于阈值 → branch="plane"（半空间布尔，cut_plane 已支持）
      · rms 大于等于阈值 → 曲缝：但 cut_curved 工具尚未实现，
        应判 branch="unsupported" 并在 reason_code 标记，
        绝不该静默产出 branch="curved" 路由到不存在的工具
      · 写 values.branch
    """
    _todo("route_branch",
          "values 写 branch；仅 plane 已支持，曲缝判 unsupported（cut_curved 未实现）")


def _plane_frame(n):
    """由法向造一组正交基 (u, v, n)。

    种子轴取与 n 夹角最大的那个坐标轴（分量最小），叉乘才不会退化。
    """
    import numpy as np
    nn = np.asarray(n, dtype=float)
    ln = float(np.linalg.norm(nn))
    if not np.isfinite(ln) or ln < 1e-12:
        raise ValueError("法向是零向量")
    nn = nn / ln
    seed = np.eye(3)[int(np.argmin(np.abs(nn)))]
    u = np.cross(seed, nn)
    u = u / float(np.linalg.norm(u))
    v = np.cross(nn, u)
    return u, v, nn


def _fit_plane(points):
    """点集最小二乘平面：质心 + SVD 最小奇异向量为法向。返回 (n, c, rms)。"""
    import numpy as np
    P = np.asarray(points, dtype=float).reshape(-1, 3)
    if len(P) < 3:
        raise ValueError("点数不足 3，无法定面")
    c = P.mean(axis=0)
    _, _, vt = np.linalg.svd(P - c, full_matrices=False)
    nn = vt[2]
    rms = float(np.sqrt((((P - c) @ nn) ** 2).mean()))
    return nn, c, rms


def _knife_box(c, n, Ru, Rv, depth):
    """刀：以切面中心为原点，u/v 各 ±R，沿 n 只朝 +n 侧延伸 depth。

    刀是**有限方盒**：R 不够就切不透，这正是「剪切范围不超过平面」的几何表达。
    """
    import numpy as np
    u, v, nn = _plane_frame(n)
    box = trimesh.creation.box(extents=[2.0 * Ru, 2.0 * Rv, depth])
    T = np.eye(4)
    T[:3, 0], T[:3, 1], T[:3, 2] = u, v, nn
    T[:3, 3] = np.asarray(c, dtype=float) + nn * (depth / 2.0)
    box.apply_transform(T)
    return box


def _resolve_plane(inp):
    """定出这一刀的平面。

    来源优先级（显式 > 世界值 > 从割线拟合）：
      1. params["plane"]（前端/人直接给的切面：n + c，可选半宽 R）
      2. world.values["cut_plane_spec"]（上一层传下来的同一张切面，保证一致）
      3. params["seam"]（割线点集，最小二乘拟合）

    返回 (n, c, R_or_None, rms, 来源标记)。
    """
    import numpy as np
    params = inp.get("params") or {}
    world = (inp.get("world") or {}).get("values") or {}

    for tag, spec in (("params.plane", params.get("plane")),
                      ("world.cut_plane_spec", world.get("cut_plane_spec"))):
        if isinstance(spec, dict) and spec.get("n") is not None and spec.get("c") is not None:
            n = np.asarray(spec["n"], dtype=float)
            c = np.asarray(spec["c"], dtype=float)
            R = spec.get("R")
            return n, c, (None if R is None else float(R)), 0.0, tag

    seam = params.get("seam")
    if isinstance(seam, dict):
        if seam.get("n") is not None and seam.get("c") is not None:
            return (np.asarray(seam["n"], float), np.asarray(seam["c"], float),
                    seam.get("R"), 0.0, "seam.n_c")
        seam = seam.get("points")
    if isinstance(seam, (list, tuple)) and len(seam) >= 3:
        n, c, rms = _fit_plane(seam)
        return n, c, None, rms, "seam.points_fit"

    return None, None, None, 0.0, ""


def _pick_section_loop(sec, c, u, v):
    """在截面上挑出「要切的那一圈」。

    为什么不能对整个截面取最大半宽：模型身上常有穿过同一平面的飘带、衣角、
    发梢——它们离刀心很远，会把所需 R 撑到几倍大，逼着人开一张巨大的刀，
    反而失去了「刀只等于截面大小」的意义。而它们本来也不会造成漏切：
    不在刀里的东西整个归另一侧，仍是断开的两块。

    真正会造成「切不断」的，只有**包含刀心那一圈**——即那里的柱体截面。
    所以：先按点在内/最近质心两条规则挑出这一圈，再只量它。
    """
    import numpy as np

    loops = []
    try:
        for seg in sec.discrete:
            P = np.asarray(seg, dtype=float)
            if len(P) < 3:
                continue
            r = P - c
            loops.append(np.c_[r @ u, r @ v])
    except Exception:
        return None, "section_unnamed"
    if not loops:
        return None, "empty"

    def _winding(pts, p):
        """射线法：点是否在这条闭合折线内。"""
        x, y = p
        inside = False
        n = len(pts)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            if (y1 > y) != (y2 > y):
                xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1 + 1e-30)
                if xi > x:
                    inside = not inside
        return inside

    def _area(pts):
        n = len(pts)
        return 0.5 * abs(sum(pts[i][0] * pts[(i + 1) % n][1] -
                             pts[(i + 1) % n][0] * pts[i][1] for i in range(n)))

    # 1) 包含刀心的圈里取面积最小的那圈（最内层，就是柱体本身而不是外轮廓壳）
    hit = [L for L in loops if _winding(L, (0.0, 0.0))]
    if hit:
        best = min(hit, key=_area)
        return best, f"contains_center/{len(loops)}loops"
    # 2) 退一步：取质心最近的一圈
    best = min(loops, key=lambda L: float(np.hypot(L[:, 0].mean(), L[:, 1].mean())))
    return best, f"nearest_centroid/{len(loops)}loops"


def cut_plane(inp):
    """按割线切分（平面分支）。

    **产出的语义边界**：它切出来的是**几何块**，不是语义部位。
    命名只能是位置（piece_a = 法向 n 那一侧，piece_b = 其余），
    绝不在这里编 "head"/"arm"——那是猜，猜错了比不猜更糟。
    `parts_available` 校验器会拦语义命名，与此处约定互为保险。

    几何做法（有限刀片，不是无限半空间）：
      · 先量网格在该平面上的**真实截面范围**（沿 u/v 的最大半宽）
      · 刀 = 以 c 为原点、u/v 各 ±R、沿 +n 深 3×对角线 的方盒
      · piece_a = 网格 ∩ 刀，piece_b = 网格 − 刀
      · **R < 截面半宽 → 刀切不透 → 判 CUT_NOT_THROUGH，不产出**
        （这条就是「剪切范围只等于平面大小、平面过小则无效」）
      · 体积守恒 |Va+Vb−V| / V 必须 ≤ const.cut_volume_tol，
        否则说明布尔出鬼了，判 VOLUME_NOT_CONSERVED，宁可不产出

    写 values：part_ids / parts_dir / cut_plane_spec（含 n、c、R、截面范围、
    源体积、对角线——下游与校验器全靠这份 spec 复算）/ vol_pieces / vol_rel_err。
    每块落盘 `{out_dir}/{part_id}.stl`，并登记 artifacts（role=part）。

    不合格 reason_code：
      · PLANE_MISSING        既没给切面也没给可用割线
      · SEAM_NOT_PLANAR      割线拟合残差过大（相对对角线 > 1e-3）
      · SRC_NOT_WATERTIGHT   源网格不水密，布尔无意义
      · CUT_NOT_THROUGH      刀片半宽小于截面半宽，切不透
      · CUT_NOT_SINGLE_BODY  某一侧多于一块（刀没覆盖的地方整块留在另一侧）
      · CUT_EMPTY            一侧为空（平面没穿过实体）
      · VOLUME_NOT_CONSERVED 体积不守恒
      · BOOL_ENGINE_MISSING  没有 manifold 布尔引擎
    """
    import numpy as np

    params = inp.get("params") or {}
    world = (inp.get("world") or {}).get("values") or {}
    const = inp.get("const") or {}
    run_dir = inp.get("run_dir")
    out_dir = params.get("out_dir") or (os.path.join(run_dir, "parts") if run_dir else None)
    src = params.get("src") or world.get("lod_path")

    if not src or not os.path.isfile(str(src)):
        return dict(ok=False, reason_code="FILE_UNREADABLE",
                    message=f"cut_plane 拿不到源网格：{src}")
    if not out_dir:
        return dict(ok=False, reason_code="NO_OUT_DIR",
                    message="未提供 out_dir，零件无处落盘")

    n, c, R, rms, plane_from = _resolve_plane(inp)
    if n is None:
        return dict(ok=False, reason_code="PLANE_MISSING",
                    message="没有切面：params.plane / world.cut_plane_spec / "
                            "params.seam 三处都没有可用平面")

    mesh = trimesh.load(str(src), process=False)
    if isinstance(mesh, trimesh.Scene):
        mesh = _scene_to_mesh(mesh)
    if mesh is None or len(getattr(mesh, "faces", [])) == 0:
        return dict(ok=False, reason_code="FILE_UNREADABLE",
                    message=f"源网格读不出三角面：{src}")
    try:
        mesh.merge_vertices()
    except Exception:
        pass
    if not mesh.is_watertight:
        return dict(ok=False, reason_code="SRC_NOT_WATERTIGHT",
                    message="源网格不水密（边界边非 0），布尔切分不可信。"
                            "应先过 prep_lod 焊接修复")

    bounds = np.asarray(mesh.bounds, dtype=float)
    diag = float(np.linalg.norm(bounds[1] - bounds[0]))
    if diag <= 0:
        return dict(ok=False, reason_code="FILE_UNREADABLE", message="网格包围盒退化")
    vol_src = abs(float(mesh.volume))

    # 割线拟合残差按尺寸归一化，不用 const.coplanar_rms_mm（那是 mm 假设，
    # 而这里的单位可能是任意的）
    if plane_from == "seam.points_fit" and rms > diag * 1e-3:
        return dict(ok=False, reason_code="SEAM_NOT_PLANAR",
                    message=f"割线拟合平面残差 {rms:.6g} 超过对角线 {diag:.6g} 的 1e-3，"
                            f"这不是平面缝——曲缝需 cut_curved，本包未实现",
                    rms=rms, diag=diag)

    u, v, nn = _plane_frame(n)
    c = np.asarray(c, dtype=float)

    # ---- 量真实截面范围：这是「平面够不够大」的唯一依据 ----
    need_u = need_v = 0.0
    loop_src = ""
    try:
        sec = mesh.section(plane_origin=c, plane_normal=nn)
    except Exception:
        sec = None
    if sec is not None:
        loop, loop_src = _pick_section_loop(sec, c, u, v)
        if loop is not None:
            need_u = float(np.abs(loop[:, 0]).max())
            need_v = float(np.abs(loop[:, 1]).max())
    if need_u <= 0 and need_v <= 0:
        return dict(ok=False, reason_code="CUT_EMPTY",
                    message="平面没有穿过实体（截面为空）——这一刀切不到任何东西",
                    plane_from=plane_from)

    finite = R is not None
    if finite:
        if R < need_u or R < need_v:
            return dict(ok=False, reason_code="CUT_NOT_THROUGH",
                        message=f"刀片半宽 R={R:.6g} 小于该处截面半宽 "
                                f"u={need_u:.6g} / v={need_v:.6g}，刀切不透。"
                                f"要么把平面放大到 R≥{max(need_u, need_v):.6g}，"
                                f"要么换位置",
                        R=R, need_u=need_u, need_v=need_v, loop=loop_src)
        Ru, Rv = R, R
    else:
        # 未给 R：退化为半空间切（R 取 1.5 倍对角线，一定覆盖）
        Ru = Rv = 1.5 * diag

    depth = 3.0 * diag
    knife = _knife_box(c, nn, Ru, Rv, depth)

    try:
        a = trimesh.boolean.intersection([mesh, knife], engine="manifold")
        b = trimesh.boolean.difference([mesh, knife], engine="manifold")
    except Exception as e:
        return dict(ok=False, reason_code="BOOL_ENGINE_MISSING",
                    message=f"布尔失败（需要 manifold3d 引擎）：{type(e).__name__}: {e}")
    if a is None or b is None:
        return dict(ok=False, reason_code="CUT_EMPTY", message="布尔返回空")

    # 碎屑阈值：小于源体积 0.5% 的块算布尔碎屑（实测量级，见 23-归档说明 §12）
    thr = max(vol_src * 5e-3, 1e-12)

    def _tidy(m):
        """剔碎屑，并且**绝不 concatenate**。

        这里踩过坑：早期版本在「有多块」时用 `trimesh.util.concatenate` 把几块
        粘成一个 mesh，于是报告里的「1 个连通体」永远成立——而这个数字本来是
        验收项。粘起来只是把它们放进同一个网格，几何上仍然是互不相连的 3 块。
        所以：多于一块就如实报数、判失败，让人看见「刀没切透」。
        """
        if m is None or len(getattr(m, "faces", [])) == 0:
            return None, 0, 0
        allp = list(m.split(only_watertight=False))
        parts = [p for p in allp if abs(float(p.volume)) >= thr]
        dropped = len(allp) - len(parts)
        if not parts:
            return None, 0, dropped
        return parts[0], len(parts), dropped

    piece_a, n_a, drop_a = _tidy(a)
    piece_b, n_b, drop_b = _tidy(b)
    if piece_a is None or piece_b is None:
        return dict(ok=False, reason_code="CUT_EMPTY",
                    message="有一侧为空白（或只剩碎屑），这一刀没有切出两块",
                    drop_a=drop_a, drop_b=drop_b)
    if n_a != 1 or n_b != 1:
        return dict(ok=False, reason_code="CUT_NOT_SINGLE_BODY",
                    message=f"切出来不是两块整件：A 侧 {n_a} 块、B 侧 {n_b} 块。"
                            f"刀只切开了它覆盖到的部分，没被刀碰到的地方整块留在另一侧——"
                            f"模型在平面之外比刀宽时必然如此。要整件两半，"
                            f"要么把 R 放大到覆盖整个平面（半空间），要么承认这是局部切口",
                    bodies_a=n_a, bodies_b=n_b,
                    R=R, need_u=need_u, need_v=need_v, finite=finite)

    vol_a = abs(float(piece_a.volume))
    vol_b = abs(float(piece_b.volume))
    rel = abs(vol_a + vol_b - vol_src) / max(vol_src, 1e-12)
    tol = float(const.get("cut_volume_tol", 1e-3))
    if rel > tol:
        return dict(ok=False, reason_code="VOLUME_NOT_CONSERVED",
                    message=f"体积不守恒：A+B={vol_a + vol_b:.6g} vs 源 {vol_src:.6g}，"
                            f"相对误差 {rel:.3g} > {tol:g}",
                    vol_a=vol_a, vol_b=vol_b, vol_src=vol_src, vol_rel_err=rel)

    os.makedirs(str(out_dir), exist_ok=True)
    piece_a.export(os.path.join(str(out_dir), "piece_a.stl"))
    piece_b.export(os.path.join(str(out_dir), "piece_b.stl"))

    spec = dict(n=[float(x) for x in nn], c=[float(x) for x in c],
                R=(float(Ru) if finite else None), finite=finite,
                src=os.path.abspath(str(src)), vol_src=vol_src, diag=diag,
                section_extent=[need_u, need_v], section_loop=loop_src,
                tol_volume=tol,
                plane_from=plane_from)

    return dict(
        ok=True,
        facts=["parts_available"],
        values=dict(
            part_ids=["piece_a", "piece_b"],
            parts_dir=os.path.abspath(str(out_dir)),
            cut_plane_spec=spec,
            vol_pieces={"piece_a": vol_a, "piece_b": vol_b},
            faces_pieces={"piece_a": int(len(piece_a.faces)),
                          "piece_b": int(len(piece_b.faces))},
            vol_rel_err=rel,
            slivers_dropped={"piece_a": drop_a, "piece_b": drop_b},
        ),
        artifacts=[
            dict(role="part", path=os.path.abspath(os.path.join(str(out_dir), "piece_a.stl")),
                 part="piece_a", bytes=os.path.getsize(os.path.join(str(out_dir), "piece_a.stl")),
                 sha256=_sha256(os.path.join(str(out_dir), "piece_a.stl"))),
            dict(role="part", path=os.path.abspath(os.path.join(str(out_dir), "piece_b.stl")),
                 part="piece_b", bytes=os.path.getsize(os.path.join(str(out_dir), "piece_b.stl")),
                 sha256=_sha256(os.path.join(str(out_dir), "piece_b.stl"))),
        ],
        message=f"平面切分完成：piece_a={vol_a:.6g} / piece_b={vol_b:.6g}，"
                f"体积相对误差 {rel:.2g}；刀半宽 R={spec['R']}，"
                f"截面半宽 u={need_u:.4g} v={need_v:.4g}",
    )


# ------------------------------------------------------------------ 逐件（按实例）

def clean_part(inp):
    """清理一件。内核会注入 `params["part"]`，一次只处理这一件。

    应做：
      · 读 parts_dir/{part}.stl
      · 剔掉薄碎片（体积小于阈值、或最小维度小于 min_wall_mm）
      · 补洞封口，确认结果是**单个封闭体**
      · 写盘到 clean_dir/{part}.stl
      · values 写 clean_path / clean_volume / clean_faces
      · values_global 写 clean_dir（下游 find_contacts 要用整个目录）
    不合格：PART_OPEN / PART_NOT_SINGLE_BODY / PART_TOO_THIN
    """
    _todo("clean_part", "一次只处理 params['part'] 这一件；"
                        "values 写 clean_path，values_global 写 clean_dir")


def verify_part(inp):
    """复验一件。工具只负责跑检查并报数，结论由校验器重算。

    应做：
      · 从 clean_dir/{part}.stl 重新读，算：水密性、自交、体积、壁厚
      · values 写 verified_volume / wall_min_mm / watertight
      · 壁厚要用**沿法向射线投射**，不是"到最近表面的距离"——
        后者在凹处会给出错误的大值
    不合格：PART_NOT_WATERTIGHT / PART_SELF_INTERSECT / WALL_TOO_THIN
    """
    _todo("verify_part", "values 写 verified_volume / wall_min_mm / watertight")


# ------------------------------------------------------------------ 连接

def find_contacts(inp):
    """找出所有相邻零件对 —— 一次性认出全部，产出 pair_ids 清单。

    应做：
      · 两两求接触面：把 A 的顶点投到 B 的表面，落在 B 内部且距离小于容差
      · **双向交叉验证**：A 侧测出的面积和法向，与 B 侧独立测出的必须一致
        （差超过 1% 就不可信，说明是采样噪声不是真接触）
      · 面积小于 const.min_contact_area_mm2 的对不算接触
      · values.pair_ids 写字符串列表，格式 `部件A__部件B`（双下划线分隔）
      · values.contacts_dir 写接触面数据所在目录
      · 可以写 values_global 汇总：对数、总面积

    为什么一次做完而不按实例：接触关系是**成对**的，不是逐个的。
    先要知道有哪些对，才能一个个去建连接。

    不合格：NO_CONTACT_FOUND / CONTACT_TOO_SMALL
    """
    _todo("find_contacts", "values 必须写 pair_ids（格式 部件A__部件B）和 contacts_dir")


def build_joint(inp):
    """给一对零件生成连接结构。内核注入 `params["pair"]`，一次只处理这一对。

    应做：
      · 拆开 pair_id 得到 A / B 两个部件名
      · 在接触面上选销位：要离边缘够远、所在处壁厚够、两件都放得下
      · 生成销与孔：
          - **销长在薄件上，孔开在厚件上**（厚件才有余量开孔）
          - 一对至少两根销，才能约束住绕法向的旋转（免键槽）
          - 径向间隙取 const.radial_clearance_mm（FDM 收缩补偿）
          - 孔深比销长多 const.hole_depth_slack_mm（防止销顶到底）
          - 销尖与孔底、销根与外表都要留够肉厚
      · 把销 union 进 A 的网格，把孔 difference 出 B 的网格
      · 写 joint_dir/{pair}_a.stl 与 joint_dir/{pair}_b.stl
      · values 写 joint_paths（字典：a / b 两个路径）、pin_n、pin_d_mm、
        clearance_measured_mm（实测，不是名义值）
    不合格：NO_VALID_PIN_SLOT / JOINT_INTERSECTS / PIN_TOO_SHORT
    """
    _todo("build_joint", "一次只处理 params['pair']；"
                         "values 写 joint_paths / pin_n / clearance_measured_mm")


def verify_joint(inp):
    """复验一对连接。

    应做：
      · 读 joint_dir/{pair}_*.stl
      · 实测径向间隙（销的外径 vs 孔的內径，从网格量，不用名义值）
      · 检查销尖余量、孔底留厚、销根留厚
      · 检查装回去时不与对方互穿（交集体积应接近 0）
      · values 写 clearance_measured_mm / tip_slack_mm / interference_mm3
    不合格：CLEARANCE_OUT_OF_RANGE / PIN_BREAKTHROUGH / JOINT_INTERFERENCE
    """
    _todo("verify_joint", "values 写 clearance_measured_mm / tip_slack_mm / "
                          "interference_mm3")


# ------------------------------------------------------------------ 交付

def export_part(inp):
    """导出一件。内核注入 `params["part"]`，一次只处理这一件。

    应做：
      · 从 joint_dir 或 clean_dir 取这一件的最终网格
      · 写 deliver/{part}.stl 与 deliver/{part}.3mf
      · **每件单独登记 artifacts**，role 用 `part_{part_id}`
        （不分开登记的话，N 件交付物在清单里会只剩 1 件）
      · values 写 export_paths、export_bytes
    """
    _todo("export_part", "一次只处理 params['part']；每件单独登记 artifacts")


def assemble_check(inp):
    """装配可成立性：全部连接都做好之后，整体拼起来成立吗。

    应做：
      · 按各对的连接关系把零件摆到最终姿态
      · 检查：不互穿（装配交集体积接近 0）、无悬空件、
        重心落在支撑多边形内（这一条决定"站不站得住"）
      · values 写 assembly_interference_mm3 / support_polygon_mm2 /
        center_of_mass_mm / stable
    不合格：ASSEMBLY_INTERFERENCE / UNCONNECTED_PART / UNSTABLE

    注意：这一步只是几何上的可成立性检查，不是物理仿真。
    真要验证"站着不倒"，需要物理引擎跑跌落/静置实验
    （已有研究证明：无连接结构 0% 稳定，有连接结构 + 仿真 95% 稳定）。
    """
    _todo("assemble_check", "values 写 assembly_interference_mm3 / stable")
