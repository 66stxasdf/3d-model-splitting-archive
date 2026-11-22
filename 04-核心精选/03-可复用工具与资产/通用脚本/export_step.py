# -*- coding: utf-8 -*-
"""把 parts_struct/ 下每个零件 STL (三角网格实体) 转成 STEP:
   - 每零件: 连通分量 -> BRepBuilderAPI_Sewing 缝合成闭合 Shell -> Solid
   - 单件 part_<name>.step + 总装 astronaut_kid_parts.step
"""
import glob, json, os, struct, sys, time
import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

OUT = 'parts_struct'
CN = {'helmet': '头盔', 'hair': '头发', 'face': '脸', 'eye_L': '左眼', 'eye_R': '右眼',
      'torso': '躯干', 'arm_L': '左臂', 'arm_R': '右臂', 'leg_L': '左腿', 'leg_R': '右腿',
      'boot_L': '左靴', 'boot_R': '右靴', 'weapon': '武器', 'backpack': '背包'}


def read_stl(p):
    d = open(p, 'rb').read()
    n = struct.unpack('<I', d[80:84])[0]
    a = np.frombuffer(d, dtype=np.uint8, count=n * 50, offset=84).reshape(n, 50)
    tris = a[:, 12:48].copy().view('<f4').reshape(n, 3, 3).astype(np.float64)
    key = np.round(tris.reshape(-1, 3) / 1e-4).astype(np.int64)
    uv, inv = np.unique(key, axis=0, return_inverse=True)
    return uv * 1e-4, inv.reshape(-1, 3)


def face_comps(faces):
    fe = np.sort(np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1)
    _, inv_e = np.unique(fe[:, 0].astype(np.int64) * (faces.max() + 1) + fe[:, 1], return_inverse=True)
    fid = np.tile(np.arange(len(faces)), 3)
    order = np.argsort(inv_e, kind='stable')
    inv_s, fid_s = inv_e[order], fid[order]
    sel = np.bincount(inv_e)[inv_s] == 2
    pairs = np.column_stack([fid_s[sel][0::2], fid_s[sel][1::2]])
    nc, comp = connected_components(coo_matrix(
        (np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(len(faces), len(faces))),
        directed=False)
    return nc, comp


def mesh_to_solid(pv, pf, tol=0.05):
    from OCP.gp import gp_Pnt
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_Sewing, BRepBuilderAPI_MakePolygon,
                                    BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeSolid)
    from OCP.BRepLib import BRepLib
    from OCP.TopoDS import TopoDS
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from build123d import Solid

    sew = BRepBuilderAPI_Sewing(tol, True, True, True, False)
    for f in pf:
        poly = BRepBuilderAPI_MakePolygon(
            gp_Pnt(*pv[f[0]]), gp_Pnt(*pv[f[1]]), gp_Pnt(*pv[f[2]]), True)
        sew.Add(BRepBuilderAPI_MakeFace(poly.Wire()).Face())
    sew.Perform()
    shape = sew.SewedShape()
    from OCP.TopAbs import TopAbs_ShapeEnum
    from build123d import Shell as BShell
    st = shape.ShapeType()
    if st == TopAbs_ShapeEnum.TopAbs_SHELL:
        shell = BShell(shape).wrapped          # downcast 到 TopoDS_Shell
    else:
        from OCP.TopExp import TopExp_Explorer
        exp = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SHELL)
        shell = BShell(exp.Current()).wrapped
    solid = BRepBuilderAPI_MakeSolid(shell).Solid()
    BRepLib.OrientClosedSolid_s(solid)
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(solid, props)
    if props.Mass() < 0:
        solid = solid.Reversed()
    return Solid(solid)


def main():
    from build123d import Compound
    from build123d import export_step
    OUT = sys.argv[1] if len(sys.argv) > 1 else 'parts_struct'
    t0 = time.time()
    man = json.load(open(os.path.join(OUT, 'manifest.json'), encoding='utf-8'))
    assembly = []
    for p in man['parts']:
        path = os.path.join(OUT, p['file'])
        verts, faces = read_stl(path)
        nc, comp = face_comps(faces)
        solids = []
        for ci in range(nc):
            Fc = faces[comp == ci]
            if len(Fc) < 4:
                continue
            used, local = np.unique(Fc, return_inverse=True)
            pv = verts[used]
            pf = local.reshape(-1, 3)
            try:
                solids.append(mesh_to_solid(pv, pf))
            except Exception as e:
                print(f"  {p['name']} piece {ci}: sew 失败 {e}")
        if not solids:
            print(f"  {p['name']}: 无有效 solid, 跳过")
            continue
        name = p['name']
        out = os.path.join(OUT, f'part_{name}.step')
        try:
            if len(solids) == 1:
                solids[0].label = name
                export_step(solids[0], out)
            else:
                comp_sh = Compound(children=solids)
                comp_sh.label = name
                export_step(comp_sh, out)
            p['step'] = f'part_{name}.step'
            p['solids'] = len(solids)
            print(f"  {p['file']} -> part_{name}.step ({len(solids)} solid, "
                  f"{os.path.getsize(out)/1e6:.1f} MB, {time.time()-t0:.0f}s)")
            assembly.extend(solids)
        except Exception as e:
            print(f"  {name}: STEP 导出失败 {e}")

    if not assembly:
        print('无任何 solid, 放弃总装')
        return
    asm = Compound(children=assembly)
    asm.label = 'astronaut_kid_parts'
    out_asm = os.path.join(OUT, 'astronaut_kid_parts.step')
    export_step(asm, out_asm)
    print(f"总装 -> {out_asm} ({os.path.getsize(out_asm)/1e6:.1f} MB, {time.time()-t0:.0f}s)")
    man['assembly_step'] = 'astronaut_kid_parts.step'
    json.dump(man, open(os.path.join(OUT, 'manifest.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
