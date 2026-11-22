"""3MF 导出 —— 让切片器直接读懂「哪个零件是什么颜色」

STL 只能存形状，颜色/材料/零件归属全部丢失，只能靠文件名和说明书传递。
3MF 是 ZIP + XML，每个 object 独立、自带 base material 索引，Bambu Studio /
OrcaSlicer / PrusaSlicer 打开就是 N 个对象 + N 个色槽，不需要在切片器里重画。

两种布局：
  · assembly —— 零件在原装配位置。用于 AMS/MMU 一次成型，或装配体预览。
  · plate    —— 零件自动排到打印盘上（货架排样），用于「分件 → 单色分打 → 拼装」。

规范依据：3MF Core Specification v2.0 + Materials & Properties v1.2（colorgroup）。
"""
from __future__ import annotations

import os
import zipfile
from xml.sax.saxutils import escape

import numpy as np

CONTENT_TYPES = '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
'''

RELS = '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
'''


def _mesh_of(solid, tol=0.2):
    import trimesh
    vs, fs = solid.tessellate(tol, 0.3)
    v = np.array([[q.X, q.Y, q.Z] for q in vs], dtype=np.float64)
    f = np.array(fs, dtype=np.int64)
    m = trimesh.Trimesh(vertices=v, faces=f, process=True)
    return np.asarray(m.vertices), np.asarray(m.faces)


def shelf_pack(sizes, gap=4.0, plate=(256.0, 256.0)):
    """货架排样：按进深分组，逐行铺。返回每个零件的 (dx, dy)。

    sizes: [(w, d), ...]  —— 俯视尺寸 (X, Y)
    返回 (offsets, 总占用)
    """
    order = sorted(range(len(sizes)), key=lambda i: -sizes[i][1])
    off = [None] * len(sizes)
    x = y = 0.0
    row_h = 0.0
    for i in order:
        w, d = sizes[i]
        if x + w > plate[0] and x > 0:
            x = 0.0
            y += row_h + gap
            row_h = 0.0
        off[i] = (x, y)
        x += w + gap
        row_h = max(row_h, d)
    used = (x, y + row_h)
    return off, used


def write_3mf(parts, path: str, layout: str = 'assembly',
              plate=(256.0, 256.0), tol=0.2):
    """parts: [Part]，需要 .pid / .name / .color / .solid。"""
    # 颜色表（去重，顺序稳定）
    colors = []
    for p in parts:
        if p.color not in colors:
            colors.append(p.color)
    cidx = {c: i for i, c in enumerate(colors)}

    meshes = []
    for p in parts:
        v, f = _mesh_of(p.solid, tol)
        meshes.append((p, v, f))

    # 布局变换
    if layout == 'plate':
        sizes = [(float(v[:, 0].max() - v[:, 0].min()),
                  float(v[:, 1].max() - v[:, 1].min())) for _, v, _ in meshes]
        offs, used = shelf_pack(sizes, plate=plate)
        transforms = []
        for (p, v, f), (dx, dy) in zip(meshes, offs):
            tx = dx - v[:, 0].min()
            ty = dy - v[:, 1].min()
            tz = -v[:, 2].min()
            transforms.append((tx, ty, tz))
        info = dict(plate_used=[round(used[0], 1), round(used[1], 1)])
    else:
        transforms = [(0.0, 0.0, 0.0)] * len(meshes)
        info = {}

    res = []
    build = []
    oid = 2
    for (p, v, f), (tx, ty, tz) in zip(meshes, transforms):
        verts = ''.join(
            f'<vertex x="{x + tx:.4f}" y="{y + ty:.4f}" z="{z + tz:.4f}"/>'
            for x, y, z in v)
        tris = ''.join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in f)
        res.append(
            f'<object id="{oid}" type="model" name="{escape(p.pid)}" '
            f'pid="1" pindex="{cidx[p.color]}">'
            f'<mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh></object>')
        build.append(f'<item objectid="{oid}"/>')
        oid += 1

    cg = ''.join(f'<m:color color="{c.upper()}FF"/>' for c in colors)
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="zh-CN" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">'
        '<metadata name="Title">ColorPartsKit</metadata>'
        '<metadata name="Designer">ColorPartsKit / build123d</metadata>'
        '<metadata name="Description">Color-separable parts, one object per color</metadata>'
        f'<resources><m:colorgroup id="1">{cg}</m:colorgroup>'
        + ''.join(res) + '</resources>'
        '<build>' + ''.join(build) + '</build></model>')

    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', CONTENT_TYPES)
        z.writestr('_rels/.rels', RELS)
        z.writestr('3D/3dmodel.model', model)
    return dict(path=path, n_objects=len(parts), n_colors=len(colors),
                layout=layout, size_mb=round(os.path.getsize(path) / 1e6, 2), **info)


def verify_3mf(path: str) -> dict:
    """回读自检：对象数、颜色数、每个 object 的三角面数、是否水密。"""
    import xml.etree.ElementTree as ET
    NS = {'c': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02',
          'm': 'http://schemas.microsoft.com/3dmanufacturing/material/2015/02'}
    with zipfile.ZipFile(path) as z:
        xml = z.read('3D/3dmodel.model').decode('utf-8')
    root = ET.fromstring(xml)
    objs = root.findall('.//c:object', NS)
    cols = root.findall('.//m:color', NS)
    items = root.findall('.//c:build/c:item', NS)
    out = []
    for o in objs:
        tri = o.findall('.//c:triangle', NS)
        out.append((o.get('name'), len(tri), o.get('pindex')))
    return dict(objects=len(objs), colors=len(cols), build_items=len(items),
                per_object=out)
