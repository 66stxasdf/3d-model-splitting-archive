"""ColorPartsKit — 彩色 3D 模型拆解框架（build123d 内核）

设计约束（与 README 的 10 条要点对应）：
  1. 一个颜色 = 一个 Solid = 一件打印件（颜色存在元数据里，不靠贴图）
  2. 颜色边界必须是零件边界（落在分型面/结构转折上）
  3. 零件树命名：model / group / part，id 稳定
  4. 拆解界面：竖销 + 锥顶孔（锥顶 45° 自支撑），横向装饰件压配
  5. 打印可行：竖直孔自支撑、每个零件有平底
  6. 元数据先行：manifest.json 记录 id/color/material/bbox/volume
  7. 三路同源：STEP（可编辑）+ STL（打印）+ GLB（彩色预览）
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field, asdict
from typing import Iterable

import numpy as np
from build123d import (
    Box, Cylinder, Sphere, Cone, Pos, Rot, Axis, Plane,
    fillet, chamfer, export_step, export_stl, Compound,
)
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.gp import gp_Pnt
from OCP.TopAbs import TopAbs_State

# ---------------------------------------------------------------- 数据模型


@dataclass
class Part:
    pid: str            # 稳定 id，如 BOLT01-TORSO
    name: str           # 中文短名
    group: str          # 部件组：head / torso / arm / leg / detail
    color: str          # #RRGGBB —— 这就是材料
    material: str       # 材料名（打印工艺里的耗材）
    solid: object = None
    print_note: str = ""    # 建议打印姿态
    joint: str = ""         # 与谁、用什么界面配合

    # 派生量在 validate 里填
    volume_mm3: float = 0.0
    bbox: tuple = ()
    bbox_min: tuple = ()
    bbox_max: tuple = ()
    n_solids: int = 0
    valid: bool = False


@dataclass
class Model:
    mid: str            # BOLT01
    name: str
    params: dict = field(default_factory=dict)
    parts: list = field(default_factory=list)

    def add(self, p: Part) -> Part:
        """加入零件并做基础自检：单实体 + 有效 + 正体积。"""
        sol = p.solid
        sols = sol.solids()
        p.n_solids = len(sols)
        _v = sol.is_valid
        p.valid = bool(_v() if callable(_v) else _v)
        p.volume_mm3 = float(abs(sol.volume))
        bb = sol.bounding_box()
        p.bbox = (round(bb.size.X, 2), round(bb.size.Y, 2), round(bb.size.Z, 2))
        p.bbox_min = (bb.min.X, bb.min.Y, bb.min.Z)
        p.bbox_max = (bb.max.X, bb.max.Y, bb.max.Z)
        if p.n_solids != 1 or not p.valid or p.volume_mm3 <= 0:
            raise ValueError(
                f'零件不合格 {p.pid}: solids={p.n_solids} valid={p.valid} vol={p.volume_mm3:.3f}')
        self.parts.append(p)
        return p

    def get(self, pid: str) -> Part:
        for p in self.parts:
            if p.pid == pid:
                return p
        raise KeyError(pid)

    # ------------------------------------------------------------ 验证
    def check_interference(self, tol_mm3: float = 0.02) -> list:
        """两两布尔求交，体积 > tol 视为干涉。拆解项目的核心判据。"""
        bad = []
        n = len(self.parts)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = self.parts[i], self.parts[j]
                ba, bb = a.solid.bounding_box(), b.solid.bounding_box()
                if not ba.intersects(bb):
                    continue
                v = _inter_volume(a.solid, b.solid)
                if v > tol_mm3:
                    bad.append((a.pid, b.pid, round(v, 3)))
        return bad

    def check_containment_gap(self) -> dict:
        """装配包围盒（用缓存的单件包围盒求并集，不构造 Compound）。"""
        mn = [min(p.bbox_min[i] for p in self.parts) for i in range(3)]
        mx = [max(p.bbox_max[i] for p in self.parts) for i in range(3)]
        return dict(size=[round(mx[i] - mn[i], 2) for i in range(3)],
                    min_z=round(mn[2], 3), max_z=round(mx[2], 2))

    def assembled(self):
        return Compound(children=[p.solid for p in self.parts])

    def colors(self) -> dict:
        d = {}
        for p in self.parts:
            d.setdefault(p.color, []).append(p.pid)
        return d

    # ------------------------------------------------------------ 导出
    def export(self, outdir: str, tess_tol: float = 0.12):
        os.makedirs(outdir, exist_ok=True)
        os.makedirs(os.path.join(outdir, 'stl'), exist_ok=True)
        os.makedirs(os.path.join(outdir, 'step'), exist_ok=True)

        for p in self.parts:
            base = p.pid                      # ASCII 文件名：OCP 的 STEP 写入器不认非 ASCII 路径
            export_stl(p.solid, os.path.join(outdir, 'stl', base + '.stl'),
                       tolerance=tess_tol, angular_tolerance=0.25)
            export_step(p.solid, os.path.join(outdir, 'step', base + '.step'))

        write_step_colored(self, os.path.join(outdir, f'{self.mid}_assembly_colored.step'))
        write_glb(self, os.path.join(outdir, f'{self.mid}.glb'), tess_tol)
        self.write_manifest(os.path.join(outdir, 'manifest.json'))
        # ⚠ Compound(children=...) 会消耗子 shape，必须放在所有单件导出之后
        export_step(self.assembled(), os.path.join(outdir, f'{self.mid}_assembly.step'))

    def write_manifest(self, path: str):
        m = dict(
            model=self.mid, name=self.name, params=self.params,
            units='mm',
            parts=[{k: v for k, v in asdict(p).items() if k != 'solid'} for p in self.parts],
            colors={c: {'parts': ps, 'count': len(ps)} for c, ps in self.colors().items()},
            stats=dict(
                n_parts=len(self.parts),
                n_colors=len(self.colors()),
                total_volume_cm3=round(sum(p.volume_mm3 for p in self.parts) / 1000, 2),
                assembly_bbox=self.check_containment_gap()['size'],
            ),
        )
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(m, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 辅助


def _inter_volume(a, b) -> float:
    """两实体的交集体积。

    坑（2026-10-28 实测）：build123d 的 `solid.intersect()` 返回的是
    **ShapeList**（即使只交出一块），直接取 `.volume` 会抛 AttributeError。
    早期版本用"异常则随机采样估体积"兜底，结果在零件只是靠得近（间隙 0.15mm）
    时也报出几千 mm³ 的假干涉 —— 全部几何都被冤枉过。改为逐 solid 累加，
    取不到就返回 0（宁漏报不误报，可拆性分析会兜住真实锁死）。
    """
    try:
        inter = a.intersect(b)
    except Exception:
        return 0.0
    if inter is None:
        return 0.0
    try:
        sols = inter.solids()
    except Exception:
        sols = []
    if sols:
        return float(sum(abs(x.volume) for x in sols))
    try:
        return float(abs(inter.volume))
    except Exception:
        return 0.0


def _sample_overlap_volume(a, b, n=4000) -> float:
    """布尔失败时的兜底：在 a 的包围盒里采样，统计同时在 b 内的比例。"""
    ba, bb = a.bounding_box(), b.bounding_box()
    lo = np.array([ba.min.X, ba.min.Y, ba.min.Z])
    hi = np.array([ba.max.X, ba.max.Y, ba.max.Z])
    pts = lo + (hi - lo) * np.random.default_rng(0).random((n, 3))
    cls = BRepClass3d_SolidClassifier(b.wrapped)
    inside = 0
    for p in pts:
        cls.Perform(gp_Pnt(*p), 1e-6)
        if cls.State() == TopAbs_State.TopAbs_IN:
            inside += 1
    vol = float(np.prod(hi - lo))
    return vol * inside / n


def hex_to_rgb(h: str):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def write_glb(model: Model, path: str, tess_tol: float = 0.12):
    """彩色预览：每个零件一个 node，PBR baseColorFactor = 该零件颜色。

    GLB 规范是 Y-up，CAD 坐标是 Z-up：导出时做 (x, y, z) -> (x, z, -y)，
    否则在 three.js / 常规查看器里模型会躺倒。
    """
    import trimesh
    scene = trimesh.Scene()
    for p in model.parts:
        vs, fs = p.solid.tessellate(tess_tol, 0.25)
        verts = np.array([[v.X, v.Z, -v.Y] for v in vs], dtype=np.float64)
        faces = np.array(fs, dtype=np.int64)
        mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=True)
        r, g, b = hex_to_rgb(p.color)
        mesh.visual = trimesh.visual.TextureVisuals(
            material=trimesh.visual.material.PBRMaterial(
                name=f'{p.pid}_{p.color}', baseColorFactor=[r, g, b, 1.0],
                metallicFactor=0.0, roughnessFactor=0.6))
        scene.add_geometry(mesh, node_name=p.pid)
    scene.export(path)
    return path


def _linear_to_hex(r: float, g: float, b: float) -> str:
    """OCC 的 Quantity_Color 内部存**线性** RGB，Red()/Green()/Blue() 拿到的也是线性值。
    直接 ×255 会得到一片暗色（#E4572E 会读成 #C61807）。这里反算回 sRGB。"""
    def f(v):
        v = min(max(v, 0.0), 1.0)
        s = v * 12.92 if v <= 0.0031308 else 1.055 * (v ** (1 / 2.4)) - 0.055
        return int(round(min(max(s, 0.0), 1.0) * 255))
    return '#%02X%02X%02X' % (f(r), f(g), f(b))


def read_step_colors(path: str):
    """把彩色 STEP 读回来，逐件报告带色情况。返回 (简单形状数, 带色数, 颜色直方图, 无名清单)。

    为什么必须回读（2026-10-28 踩坑）：字符串数 STYLED_ITEM / COLOUR_RGB 完全不可靠
    —— 一个零件的样式实体个数与「带色零件数」不一一对应，早先据此报 20/37 的假警。
    唯一可信的是 XCAF 读回来逐标签查色。
    """
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorType
    from OCP.Quantity import Quantity_Color
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_ChildIterator

    rd = STEPCAFControl_Reader()
    rd.SetColorMode(True)
    rd.SetNameMode(True)
    if rd.ReadFile(path) != IFSelect_RetDone:
        raise RuntimeError(f'STEP 读取失败: {path}')
    doc = TDocStd_Document(TCollection_ExtendedString('rd'))
    if not rd.Transfer(doc):
        raise RuntimeError(f'STEP 转 XCAF 失败: {path}')
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    it = TDF_ChildIterator(doc.Main(), True)
    labs = []
    while it.More():
        labs.append(it.Value())
        it.Next()

    n_simple = n_color = 0
    hist, unnamed = {}, []
    for lab in labs:
        if not (st.IsShape_s(lab) and st.IsSimpleShape_s(lab)):
            continue
        n_simple += 1
        nm = ''
        a = TDataStd_Name()
        if lab.FindAttribute(TDataStd_Name.GetID_s(), a):
            nm = a.Get().ToExtString()
        c = Quantity_Color()
        got = False
        for ctype in (XCAFDoc_ColorType.XCAFDoc_ColorGen,
                      XCAFDoc_ColorType.XCAFDoc_ColorSurf):
            if ct.GetColor(st.GetShape_s(lab), ctype, c):
                got = True
                break
        if got:
            n_color += 1
            hx = _linear_to_hex(c.Red(), c.Green(), c.Blue())
            hist[hx] = hist.get(hx, 0) + 1
        else:
            unnamed.append(nm or '?')
    return n_simple, n_color, hist, unnamed


def write_step_colored(model: Model, path: str, verify: bool = True):
    """带颜色的 STEP（XCAF）：FreeCAD / SolidWorks / 在线查看器打开即是彩色的。

    实测要点（OCP 7.9，2026-10-28）：
      · 每个 Solid 用 AddShape 注册为 free shape + SetColor(ColorGen) 即可，**不要**建
        root/AddComponent 装配结构——那样只会写出结构、几何全丢（文件 1.6KB）。
      · **AddShape 有时返回的是「引用标签」而不是简单形状标签**（37 件里出现了 3 次：
        CHEST_CORE / EYE_L / EYE_R）。颜色和名字挂在引用标签上会被 STEP 写出器忽略，
        结果那 3 件写成无名 PRODUCT('SOLID') 的灰模。必须用 GetReferredShape 找到
        被引用的简单标签，两处都写一遍才算稳。
      · OCC 内部存线性 RGB，回读后要 _linear_to_hex 换算，否则颜色全偏暗。
      · 写完必须**回读**验证（read_step_colors），数字符串会给出假警。
    """
    try:
        from OCP.STEPCAFControl import STEPCAFControl_Writer, STEPCAFControl_Controller
        from OCP.STEPControl import STEPControl_AsIs
        from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorType
        from OCP.TDocStd import TDocStd_Document
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.Quantity import Quantity_Color, Quantity_TOC_sRGB
        from OCP.TDataStd import TDataStd_Name
        from OCP.TDF import TDF_Label
        from OCP.Interface import Interface_Static

        STEPCAFControl_Controller.Init_s()
        try:
            Interface_Static.SetCVal_s('write.step.schema', 'AP242DIS')
        except Exception:
            pass

        doc = TDocStd_Document(TCollection_ExtendedString('MDTV-XCAF'))
        st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
        ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
        n_ref = 0
        for p in model.parts:
            # 必须注册单 Solid 而不是 Compound：Compound 注册时颜色会被丢掉
            sols = p.solid.solids()
            shape = sols[0].wrapped if len(sols) == 1 else p.solid.wrapped
            lab = st.AddShape(shape)
            targets = [lab]
            if not st.IsSimpleShape_s(lab):
                # 退回的引用标签：真正被写进文件的几何挂在它引用的那个简单标签上
                ref = TDF_Label()
                if st.GetReferredShape_s(lab, ref) and not ref.IsNull():
                    targets.append(ref)
                    n_ref += 1
            r, g, b = hex_to_rgb(p.color)
            for t in targets:
                ct.SetColor(t, Quantity_Color(r, g, b, Quantity_TOC_sRGB),
                            XCAFDoc_ColorType.XCAFDoc_ColorGen)
                TDataStd_Name.Set_s(t, TCollection_ExtendedString(p.pid))
        st.UpdateAssemblies()

        w = STEPCAFControl_Writer()
        w.SetColorMode(True)
        w.SetNameMode(True)
        w.Transfer(doc, STEPControl_AsIs)
        ok = w.Write(path)
        if verify:
            n, nc, hist, bad = read_step_colors(path)
            print(f'  彩色 STEP: 回读 {nc}/{n} 件带色（零件 {len(model.parts)} 件，'
                  f'唯一色 {len(hist)} 种，引用标签修正 {n_ref} 件）')
            if hist:
                print('    配色 ' + '  '.join(f'{k}×{v}' for k, v in
                                             sorted(hist.items(), key=lambda kv: -kv[1])))
            if nc < len(model.parts) or len(hist) != len(model.colors()):
                print(f'  [warn] 回读不符：带色 {nc}/{len(model.parts)}，'
                      f'唯一色 {len(hist)}/{len(model.colors())}')
                if bad:
                    print(f'    缺失: {", ".join(bad[:12])}')
        return True
    except Exception as e:  # 颜色写不进就退回灰模，不让主流程失败
        print('  [warn] 彩色 STEP 写入失败，退回普通 STEP:', e)
        export_step(model.assembled(), path)
        return False


def rbox(w, d, h, r=2.0):
    """圆角方盒（所有边）。r 受最小边限制自动收敛。"""
    b = Box(w, d, h)
    r = min(r, min(w, d, h) / 2 - 1e-3)
    return fillet(b.edges(), radius=r)


def pin(d, h, at):
    """竖直销：底面在 at.z，向上长 h。"""
    return Pos(at[0], at[1], at[2] + h / 2) * Cylinder(d / 2, h)


def socket(d, depth, at, clearance=0.2, cone=True):
    """竖直孔 + 45° 锥顶（自支撑，向下的开口）。返回要减去的工具体。"""
    dd = d + clearance * 2
    cyl_h = max(depth - dd / 2, 0.5)
    cut = Pos(at[0], at[1], at[2] - cyl_h / 2) * Cylinder(dd / 2, cyl_h)
    if cone:
        h = dd / 2
        tip = Pos(at[0], at[1], at[2] - cyl_h - h / 2) * Cone(dd / 2, 0, h)
        # Cone 默认底面在下、顶点在上；这里要向下收尖 -> 旋转 180
        tip = Pos(at[0], at[1], at[2] - cyl_h - h / 2) * Rot(180, 0, 0) * Cone(dd / 2, 0, h)
        cut = cut + tip
    return cut
