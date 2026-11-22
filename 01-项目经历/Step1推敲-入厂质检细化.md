# Step 1 入厂质检（Mesh Intake）· 思考推敲修订版 v2

> 配套探针：`Step1-焊接与破洞判别探针.py`（同目录，trimesh 5.2.0，5 真实资产 + 仿真可复跑）
> 上一版（v1）见文末「修订说明」——它把"非流形"这一真第三类补错成了"亚像素裂缝"，本文纠正。

---

## 0. 一句话目标

把任意来源的 3D 模型读进来，做**焊合（weld）+ 拓扑体检**，输出结构化 `mesh_health.json`，判定该模型能否直接进入 Step 2（语义感知），还是必须先走 Step 1b（网格修复）。

---

## 1. 修订说明（基于审查 verdict）

| 上一版主张 | 审查判定 | v2 更正 |
|---|---|---|
| "默认 merge 在 UV 缝处失败，必须带 `merge_tex/norm=True`" | ✅ 对，可复现 | **保留**。源码确认：仅当带完整贴图（image+uv 且 `visual.defined`）时默认才把 UV 纳入合并键；无贴图默认即可焊。一律显式传 True 最稳。 |
| "实测存在第三类**亚像素裂缝**（位置差 1e-4）" | ❌ 错，是**构造反例** | **删除为已观测类别**。该结论来自"人为把顶点挪 1e-4 再验证需要放宽容差"的循环论证，不是真实数据。5 个真实资产间隙谱呈双峰（见 §3.2），中间 1e-4 档空着。 |
| "原文档真破洞 vs UV 缝 二分不够" | ✅ 诊断对，但**补错类别** | **保留诊断，改补类别**。真正漏掉的是**非流形**（边界边=0 但仍不水密），不是"更小的缝"。 |
| "容差扫描 digits 8→3 取最细能水密档" | ✅ 方法对 | **保留**为检测/修补量化裂缝的正确工具。 |
| "上限 `tol ≤ 1e-3 × bbox_diag`" | ❌ 错，且在资产尺度恰处过焊拐点 | **删除硬上限**。粗化上限改由「非流形边数拐点」与「打印分辨率」取小（见 §4）。 |
| 术语"亚像素裂缝" | ❌ 渲染术语，网格无像素 | 改为**量化裂缝 / 亚容差裂缝（quantization crack）**。 |

---

## 2. 焊合机制（源码 + 探针，v1 已证，保留）

trimesh 5.2.0 `grouping.merge_vertices`：合并唯一键 = 坐标×`10^digits_vertex`（默认 `digits_vertex=decimal_to_digits(tol.merge)=8`，即默认容差 **1e-8**）；仅当 `merge_tex=False` 且 `visual.defined` 且 `kind=="texture"` 才追加 UV；`merge_norm=False` 才追加法线。⇒ `merge_tex=True, merge_norm=True` 把 UV/法线移出键，按位置强焊——**这是修 UV 缝的正确姿势**。

`is_watertight`（`graph.py:890-895`）只保留**恰好出现 2 次**的边，`watertight = (len(groups)*2 == len(edges))`。⇒ 一条被 ≥3 个面共享的边（非流形）会被排除出 groups，于是**边界边=0 仍可返回 `is_watertight=False`**。

---

## 3. 实证（5 个真实资产 + 仿真）

### 3.1 焊合前后拓扑（本环境实测，trimesh 5.2.0）

| 资产 | diag | 原边界边 | 焊合(d8)边界 | 焊合(d8)非流形 | 焊合(d8)水密 |
|---|---|---|---|---|---|
| astronaut_kid | 1.37 | 106,132 | **0** | 0 | True |
| minecraft_character | 1.30 | 58,828 | **0** | 0 | True |
| **astronaut_kid_upload** | 1.41 | 192,610 | **0** | **8** | **False** |
| BOLT01 | 143.94 | 55 | 55（留） | 55 | False |
| MK02 | 237.37 | 134 | 134（留） | 134 | False |

- 神经网格（astronaut_kid / minecraft）：1e-8 容差焊合后**残余边界=0** ⇒ 缝副本位置**严格重合**（float32 同一值），**不存在 1e-4 裂缝群**。
- **astronaut_kid_upload：边界=0 但非流形=8、不水密** ⇒ 这正是二分/只看边界边会漏的真第三类——**非流形**。
- BOLT01 / MK02：边界边焊不合（真开口，间隙 ≈0.035–0.05·diag），需 Step 1b 重建。

### 3.2 间隙谱双峰（综述审查方实测）
- astronaut_kid / astronaut_kid_upload / minecraft：边界副本间隙 **100% 恰为 0**（神经网格拆 UV 时副本是同一 float32，差严格为 0）。
- BOLT01：中位间隙 7.24 ≈ 0.050·diag；MK02：中位 8.40 ≈ 0.035·diag。
- **双峰，中间（1e-4）档空着** ⇒ "亚像素裂缝"在现有资产中未观测到。

### 3.3 量化裂缝机制（仿真，明确标注）
- **朴素 float16 量化**：对焊合后的 astronaut_kid 拆面→`astype(float16)`→回 float64，缝副本间距 max = 0 ⇒ **重合缝仍重合，朴素量化不产生裂缝**。
- **按 chart 独立量化仿真**（模拟 Draco / 分 chart 导出中对每个 chart 用独立网格原点）：极小合成 box 上，q≈9.8e-4 量化后，默认细焊合（1e-8）**焊不上**（残余边界=12、不水密），放粗到 1e-3 才闭壳 ⇒ **量化裂缝确实由"按 chart 独立量化"产生，且容差阶梯扫描正是检测/修补它的工具**。
- ⚠️ 该实验为**机制仿真**（pygltflib 缺失，无法跑真实 Draco 导出）。要把"量化裂缝"从防御性分支升级为"实测发现"，需：① 装 `pygltflib` 后对真实资产做 Draco 导出再读回，或 ② 取得一个真实 Draco/量化导出的资产量间隙谱。当前 5 资产未观测到，故本档**仅作防御性分支**。

---

## 4. 修订后的 Step 1 算法

### 4.1 单连通分量处理
```python
def intake_component(mesh, diag, nozzle_diam=None):
    # 0) 清退化/重复面/重复顶点（不计为破洞）
    mesh = trimesh.repair.remove_degenerate_faces(mesh)
    mesh = trimesh.repair.remove_duplicate_faces(mesh)
    base = dict(n_vertices=len(mesh.vertices), n_faces=len(mesh.faces),
                diag=diag, has_texture=bool(mesh.visual.defined and mesh.visual.kind=="texture"))
    open_pre = count_boundary(mesh)
    # 1) 容差阶梯扫描：从最细(8)到最粗(3)
    tol_cap = (0.5*nozzle_diam/diag) if nozzle_diam else 1e-3   # 上限=打印分辨率(或兜底1e-3)
    candidates = [d for d in (8,6,5,4,3) if 10**(-d) <= tol_cap]
    rows = []
    for d in candidates:
        m = mesh.copy(); m.merge_vertices(merge_tex=True, merge_norm=True, digits_vertex=d)
        rows.append(dict(d=d, boundary=count_boundary(m),
                         nonmanifold=count_nonmanifold(m),
                         winding=m.is_winding_consistent, watertight=m.is_watertight))
    # 2) 取"边界=0 ∧ 非流形=0 ∧ 绕序一致"的最细档（避免过焊）
    best = next((r for r in rows if r["boundary"]==0 and r["nonmanifold"]==0 and r["winding"]), None)
    if best:
        klass = "fake_seam" if best["d"]>=8 else "fake_crack_quant"
        chosen, mesh = best["d"], weld(mesh, best["d"])
        holes = []
    else:
        last = rows[-1]   # 最粗档
        if last["boundary"] > 0:
            klass = "real_hole"; holes = boundary_loops(mesh)
        else:  # 边界=0 但非流形>0 或绕序错
            klass = "non_manifold"
            holes = [{"nonmanifold_edges": last["nonmanifold"], "winding_ok": last["winding"]}]
        chosen = None
    mesh.fix_normals()
    flipped = mesh.volume < 0
    return {**base, "class": klass, "weld_digits": chosen,
            "open_edges_pre": open_pre, "ladder": rows,
            "flipped_normals": flipped, "holes": holes}
```
### 4.2 全局流程
1. `trimesh.load(file)` → `to_geometry()`（合并 Scene）或 `mesh.split()` 取各连通分量。
2. **逐分量**调用 §4.1（全局 `is_watertight` 会把"主体水密+附件破洞"误判，必须分量判）。
3. 汇总：所有分量 `class ∈ {fake_seam, fake_crack_quant}` → `overall_pass=True`；否则 `repair_route=step1b`，并附 `holes[]` / `nonmanifold` 信息。

### 4.3 两条铁律（防过焊）
- **取最细达标档**，绝不为闭壳一味放粗。粗化一旦使非流形边数上升即停（拐点）。
- **粗化上限 = min(非流形拐点, 打印分辨率 0.5×喷嘴直径/diag)**，不做 `1e-3×diag` 硬上限（该值在 diag<1 时直接毙掉 digits=3，在 diag≈1.3–1.4 资产上恰好压在过焊拐点，会静默把模型焊坏：实测 astronaut_kid 在 d=3 非流形边暴增到 14,214、minecraft 到 29,902，水密反而丢失且不报错）。

---

## 5. `mesh_health.json` 修订 schema
```json
{
  "source_file": "...",
  "overall_pass": false,
  "components": [{
    "id": 0, "n_vertices": 12345, "n_faces": 24000, "diag": 1.37,
    "has_texture": true,
    "class": "non_manifold",          // fake_seam | fake_crack_quant | real_hole | non_manifold
    "weld_digits": null,
    "open_edges_pre": 192610, "open_edges_post": 0,
    "nonmanifold_edges": 8, "winding_consistent": true,
    "flipped_normals": false,
    "ladder": [ {"d":8,"boundary":0,"nonmanifold":8,"winding":true,"watertight":false},
                {"d":3,"boundary":0,"nonmanifold":14214,"winding":true,"watertight":false} ],
    "holes": [{"nonmanifold_edges":8,"winding_ok":true}]
  }],
  "repair_route": "step1b"
}
```
> 必须输出可读的 `class` 与 `repair_route`；`ladder` 保留每档边界/非流形/绕序，供复核与标定上限。

---

## 6. 验收标准
| 项 | 通过条件 |
|---|---|
| 重合缝识别 | astronaut_kid 焊后 `boundary==0, watertight==True, class==fake_seam` |
| 非流形识别 | astronaut_kid_upload `class==non_manifold`（边界=0 但非流形>0、不水密），**不被二分/只看边界边漏过** |
| 真破洞识别 | BOLT01/MK02 `class==real_hole`，路由 Step 1b |
| 不误判量化裂缝 | 当前资产 `class` 不出现 `fake_crack_quant`（未观测）；该档仅防御性保留 |
| 不过焊 | 阶梯取最细达标档；d=3 在 diag≈1.3–1.4 资产上必须因非流形拐点或打印分辨率被拦停 |
| 分量隔离 | 主体水密+附件破洞 → 整体 `overall_pass==false` 但主体分量 `fake_seam` |

---

## 7. 与下游接口
- **Step 1b**：`real_hole` → `trimesh.repair.fill_holes`（小环）/ 重建（大缺失）；`non_manifold` → 需**专门策略**（定位非流形边、在自接触处劈分壳层或局部重建；trimesh 无一键修复，列为待解决）。修完回 §4.1 重判。
- **Step 2 语义感知**：只吃 `class ∈ {fake_seam, fake_crack_quant}` 的水密网格；绝不喂未修 `real_hole` / `non_manifold`。
- **Step 3 代价场**：用 `flipped_normals` 决定法线方向；`nonmanifold>0` 须在 Step 1 清掉（AO/曲率对法线敏感，且 Step 5 布尔会在非流形处突然报错）。

---

## 8. 待拍板开放问题
1. **量化裂缝档坐实**：装 `pygltflib` 对真实资产做 Draco 导出再读回，或取得真实 Draco/量化资产，量间隙谱看是否出现 ~1e-4 峰。出现则升级为"实测发现"；否则维持防御性分支。
2. **非流形修复策略**：目前 Step 1b 对 `non_manifold` 无一键方案，需定"劈分壳层 vs 局部重建"的具体算法（建议先定位非流形边、在该处把自接触面分离）。
3. **打印分辨率联动**：D3（物理打印 vs 数字分件）决定 `nozzle_diam` → 决定粗化上限；数字分件可放宽为更粗上限。
4. **分量级联**：`mesh.split()` 后若某分量本身非流形，应在分量内再判定，避免跨分量误并。

---

## 附：v1 错误自陈（供追溯）
v1 的"亚像素裂缝"第三类，证据是合成 box 上"人为把顶点挪 1e-4 再验证需放宽容差"——属循环论证的构造反例，非真实数据。研究写作中"我发现真实现象"与"我假设可能存在"分量不同，v1 把后者当事实陈述，是被一眼看穿的根因。v2 以 5 真实资产实测 + 源码机制 + 机制仿真替代之。
