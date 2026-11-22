# 闸门语义（G0–G10）· 唯一来源

> 本文件是 `16-分件Agent/splitagent/gates.py` 的**语义抄本**。技能正文只写「我引哪个 Gx」，
> 判据细节一律回到这里，避免六份副本漂移。
> 代码改了这一份必须跟着改；**两者冲突时以代码为准**，并把差异报告出来。

## 零、三态与不可折叠

| 态 | 含义 | 允许的后续动作 |
|---|---|---|
| `PASS` | 全部判定项为真 | 放行到下一环 |
| `FAIL` | 任一判定项为假 | 停手、报 `reason_code` |
| `UNKNOWN` | 证据不足 / 缺字段 / 能力缺失 | **停下索取证据**，不得继续 |

**铁律一：`UNKNOWN` 不折叠成 `PASS`。** 缺字段一律 `UNKNOWN`，不是 `.get()` 拿到 `None` 当通过。
（`l19_pipeline.py:135` 就因 `all_ok` 缺失被静默吞掉而埋过隐患。）

**铁律二：三态与的运算顺序是** —— 任一 `FAIL` → `FAIL`；否则有 `UNKNOWN` → `UNKNOWN`；否则 `PASS`。
即 `FAIL` 吞掉 `UNKNOWN`，`UNKNOWN` 吞掉 `PASS`。空判定集 → `UNKNOWN`（不是 `PASS`）。

**铁律三：`advisory` 闸门永不阻断。** 它只出 `WARN`，结论上限是 `PASS_WITH_WARN`。

**铁律四：`reason_code` 必须带出来。** 它是失败策略（重试 / 回退 / 中止）的唯一依据，
吞掉它上游只能报 `UNKNOWN_FAILURE`，回退决策就失去可解释性。

---

## 一、逐门对照

### G0 入厂质检 → 事实 `input_ok`
来源：`g0_import(probe, policy)`

| 判定项 | 条件 | 可否放宽 |
|---|---|---|
| 文件可读 | `probe.ok == true` | 否 |
| 面数 > 0 | `welded.faces > 0` | 否 |
| sha256 已落账 | `source.sha256` 非空 | 否 |
| 水密 | `welded.watertight == true` | **是**（`policy.allow_nonwatertight_source`）|
| 单一连通块 | `welded.components == 1` | **是**（`policy.require_single_shell=false`）|
| 边界边为 0 | `welded.boundary_edges == 0` | **是**（同上 `allow_nonwatertight`）|

失败码：`TOOL_ERROR` / `INPUT_NOT_FOUND`
证据里必须留：`scale_resolved`、`scale_source`（**单位从哪读出来的**）

> ⚠ 三个「可放宽」项是**策略开关**，不是默认值。放宽必须显式、必须写进报告、
> 必须让人看见「本次是在非水密源上跑的」。技能侧不得自行假定已放宽。

### G1 几何与 LOD → 事实 `lod_ready`
来源：`g1_geometry(prep)`

| 判定项 | 条件 |
|---|---|
| LOD 生成成功 | `prep.ok == true` |
| LOD 非空 | `lod.faces > 0` |
| LOD 单一连通块 | `lod.components == 1` |
| **体积漂移 < 1%** | `abs(lod.volume_ratio - 1) <= 0.01` |
| 数组哈希已记录 | `arrays.arrays_sha256` 非空 |

失败码：`LOD_MISSING`
`arrays_sha256` 的作用是**复验对齐**：后续复验要证明切的是同一份几何。

### G2 —— **缺号**
`gates.py` 里没有 `g2_*`。历史遗留，**任何技能不得引用 G2**。
若看到旧文档提「G2 水密」，那是 G0 的内容被记串了。

### G3 割线准入 → 事实 `seam_ok`
来源：`g3_seam(seam_rep)`

| 判定项 | 条件 |
|---|---|
| 四级回退解析到点 | `n_points > 0` |
| `closed=true` | `declared_closed == true` |
| 无准入级错误 | `reason_codes` 为空 |

失败码：`SEAM_MISSING`，或 `reason_codes[0]`
**特殊**：`seam_rep.undecidable` 为真且未全过 → `UNKNOWN`（不是 `FAIL`）。
这一条很关键：**「判不了」和「不合格」是两回事**，不能都记成拒收。

### G4 分支路由 → 事实 `route_known`
来源：`g4_route(route)`

| 判定项 | 条件 |
|---|---|
| 分支已确定 | `branch ∈ {plane, curved}` |

失败码：`ROUTE_UNSUPPORTED`（`branch` 为 `None` 时）

### G5 切割产物 → 事实 `parts_available`
来源：`g5_cut(cut, expected_parts, run_started_at, part_names)`

| 判定项 | 条件 |
|---|---|
| 进程退出码为 0 | `_proc.returncode == 0` |
| 切割报告 ok | `cut.ok == true` |
| A/B 件存在 | 路径 `isfile` |
| **A/B 件新鲜** | `mtime >= run 起点` |
| A/B 件字节数 > 84 | 排除空壳 STL（84 字节 = 只有头） |
| A/B 件面数已记录 | `faces > 0` |

失败码：`CUT_MISSING` / `CUT_FAILED` / 工具自带码
> 「新鲜」这一项防的是**读到上一次运行的残留产物**。这是最容易造假成功的地方。

### G6 碎块清理 → 事实 `part_clean`
来源：`g6_cleanup(cl, total_volume)`

| 判定项 | 条件 |
|---|---|
| 清理执行成功 | `cl.ok == true` |
| A/B **未被删主块** | `after_faces > 0` |
| A/B **移除体积 < 1%** | `removed_volume / total_volume <= 0.01` |

失败码：`CLEANUP_MISSING`
> 两条方向相反：一条防「没清干净」，一条防「把主块当碎片删了」。

### G7 独立复验 → 事实 `part_verified`
来源：`g7_verify(vf)`
**checks 由复验报告动态给出**，当前 10 项，阈值默认值（`verify_tools.py:21-26`）：

| 阈值 | 默认值 |
|---|---|
| `volume_relative_error_max` | **0.001**（0.1%）|
| `bbox_dev_mm_max` | **0.01** mm |
| `interpenetration_max` | **0.01**（1%）|
| `interpenetration_warn_margin` | **0.003** |

十项性质（以报告实际键名为准）：源壳单水密 / A、B 各自水密且边界边为 0 / A、B 各自单连通 /
无非流形边 / 绕序一致 / 体积守恒 <0.1% / bbox 并集还原 <0.01mm / 两件互不嵌入 <1% / 切面共面。

失败码：`VERIFY_MISSING` / `VERIFY_FAILED`
**空 checks → `UNKNOWN`**（不是 PASS）。这条是防「校验器什么都没查就说通过」。

### G8 装配面 → 事实 `assembled_ok` · **advisory**
来源：`g8_assembly(asm)`，`advisory=True`
失败码：`ASSEMBLY_MISSING` / `ASSEMBLY_WEAK`
> 永不阻断。但「永不阻断」不等于「可以不看」——它转成 `WARN` 后必须出现在报告里。

### G9 交付导出 → 事实 `part_exported`
来源：`g9_export(ex)`

| 判定项 | 条件 |
|---|---|
| 导出成功 | `ex.ok == true` |
| 文件数 ≥ 2 | `len(files) >= 2` |
| 逐件回读一致 | 每个文件 `roundtrip_ok` 或 `faces_match` |

失败码：`EXPORT_MISSING`
证据里必须留：`unit`、`unit_confirmed`
> `unit_confirmed == false` 时，即便 G9 全过，交付也只能是 `PASS_WITH_MANUAL`。

### G10 基线对照 · **advisory（软）**
来源：`g10_baseline(cmp_rep)`
失败码：`BASELINE_MISSING` / `BASELINE_MISMATCH`

---

## 二、与内核（18-）的对应关系

18- 内核用「事实 + 校验器」表达同一件事，映射如下（详见 `pack-contract.md`）：

| 闸门 | 内核事实 | 内核工具（split3d 0.2.0）|
|---|---|---|
| G0 | `input_ok` | `probe_import` |
| G1 | `lod_ready` | `prep_lod` |
| G3 | `seam_ok`（optional 组）| `seam_ingest` |
| G4 | `route_known`（optional 组）| `route_branch` |
| G5 | `parts_available` | `cut_plane`（另：`segment_semantic` 也产出同一事实）|
| G6 | `part_clean` | `clean_part` |
| G7 | `part_verified` | `verify_part` |
| G8 | `assembled_ok` | `assemble_check` |
| G9 | `part_exported` | `export_part` |
| G10 | 无对应事实 | 无（内核未接基线对照）|

两处要注意的差异：

- **G8 的严格程度不同**：16- 的 `g8_assembly` 是 `advisory=True`（永不阻断），
  18- 的 `assemble_check` 是 `on_fail: abort`（失败即中断）。
  **以 18- 为准**：装配不成立就是不能交付，不是「告警一下」。
- **`semantic_parts` 不存在了**：0.2.0 里语义分件直接产出 `parts_available`（零件），
  不再是旁挂的独立事实。旧文档若提 `semantic_parts`，那是 0.1.0 的说法，已作废。

**内核里没有 G10**，所以「基线对照」在 18- 这条链上是缺席的，只能人工做或不做。
不要把 `part_exported` 说成「已对齐基线」。
