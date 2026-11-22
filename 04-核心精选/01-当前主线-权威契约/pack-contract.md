# 技能 ↔ 内核 术语映射（唯一对照表）

> 内核：`D:\workbuddy产物\18-分件Agent内核`，契约真源 `packs/split3d/manifest.json`。
> **本表锚定 `agentcore.pack/v1` 的 split3d `0.2.0`（2026-11-10 21:00）。**
> 技能里出现任何工具 id / 事实 id，必须能在本表查到。自造 id = 静默失效。
> 校验脚本：`check_terms.py`。**内核升版后它第一件事就会 FAIL**，看到 FAIL 先核对本表。

## 零、两层的关系

| 层 | 管什么 | 在哪 |
|---|---|---|
| **内核** | 从目标反推顺序、预算、防环、不信自称、失败策略、留痕、续跑 | `agentcore/`，零几何名词 |
| **领域包** | 有哪些事实、工具怎么依赖、怎么实现、怎么校验 | `packs/split3d/` |
| **技能（本仓）** | 每个环节「什么时候该停手」「凭什么放行」「能力到哪为止」 | `skills/*/SKILL.md` |

技能**不实现任何工具**。工具是桩还是实现了，看 `doctor`：
它报「13 个工具是桩」，意思是「活还没干」，不是「技能没写好」。

## 一、12 个事实

| 事实 | 中文 | group | 域 | 归属技能 |
|---|---|---|---|---|
| `input_ok` | 入厂质检通过 | admission | — | `split-intake` |
| `lod_ready` | 降面模型已就绪 | prepare | — | `cut-solidify` |
| `seam_ok` | 割线准入通过 | **optional** | — | `seam-admission` |
| `route_known` | 切割分支已确定 | **optional** | — | `seam-admission` |
| `parts_available` | 零件已分出 | core | — | `part-semantics` + `cut-solidify`（**共享**）|
| `part_clean` | 单件碎块已清理 | core | part | `cut-solidify` |
| `part_verified` | 单件复验通过 | verify | part | `print-proof` |
| `contacts_found` | 接触面对已识别 | joint | — | `joint-design` |
| `joint_built` | 单对连接已生成 | joint | pair | `joint-design` |
| `joints_verified` | 连接复验通过 | joint | pair | `joint-design` |
| `part_exported` | 单件已导出 | deliver | part | `print-proof` |
| `assembled_ok` | 装配可成立 | deliver | — | `print-proof` |

三件要理解的事：

1. **`seam_ok` / `route_known` 是 `optional` 组** —— 没人画割线时这一支不参与，整件走语义分件。
   所以「没有割线」不是失败，是另一条路。
2. **`parts_available` 有两个产出者**，见下一节。它天然属于两个技能，不是归属混乱。
3. **`part_*` / `joint_*` 是域化事实**（`domain: {from: part_ids | pair_ids}`）——
   一件一件地成立，不是全局一个布尔。这是 0.2.0 最大的结构变化。

## 二、13 个工具

| 工具 | produces | requires | on_fail | prio | 归属技能 |
|---|---|---|---|---|---|
| `probe_import` | `input_ok` | — | abort | 0 | `split-intake` |
| `prep_lod` | `lod_ready` | `input_ok` | abort | 0 | `cut-solidify` |
| `segment_semantic` | **`parts_available`** | `input_ok`, `lod_ready` | abort | **10** | `part-semantics` |
| `seam_ingest` | `seam_ok` | `lod_ready` | abort | 0 | `seam-admission` |
| `route_branch` | `route_known` | `seam_ok` | abort | 0 | `seam-admission` |
| `cut_plane` | **`parts_available`** | `route_known`, `lod_ready` | abort | **20** | `cut-solidify` |
| `clean_part` | `part_clean` | `parts_available` | **skip_instance** | 0 | `cut-solidify` |
| `verify_part` | `part_verified` | `part_clean` | **skip_instance** | 0 | `print-proof` |
| `find_contacts` | `contacts_found` | `part_verified` | abort | 0 | `joint-design` |
| `build_joint` | `joint_built` | `contacts_found` | **skip_instance** | 0 | `joint-design` |
| `verify_joint` | `joints_verified` | `joint_built` | **skip_instance** | 0 | `joint-design` |
| `export_part` | `part_exported` | `part_verified` | **skip_instance** | 0 | `print-proof` |
| `assemble_check` | `assembled_ok` | `joints_verified`, `part_exported` | abort | 0 | `print-proof` |

这张表的 `produces` / `requires` / `priority` 三列由 `check_terms.py` 与 `manifest.json`
逐行对账，不一致即 FAIL。**`writes` 列不在对账范围内**（表里没有这一列），
所以「谁写了哪个值」仍需人工核。

> 2026-11-10 21:35 内核改动：`probe_import` 补齐 `writes`（`model_path` / `src_sha` /
> `scale` / `scale_source`，它本来就写这四个键，只是契约漏声明）；
> `segment_semantic` 的 `requires` 补上 `lod_ready`——它的 `params.src` 读 `$value.lod_path`，
> 原契约没声明这条隐式依赖，孤跑会抛 `MISSING_PARAM` 并被误诊为「分割器没装好」。

### 2.1 两个产出者：谁切谁，看 priority

`parts_available` 由 `segment_semantic`（**10**）与 `cut_plane`（**20**）竞争，
规划器按 priority 选，**数值大的赢**。所以：

- 有人明确画了割线 → `cut_plane`（20）胜出，按人画的切
- 没人画线 → 只有 `segment_semantic`（10）可用

这个优先级顺序本身是一个设计决定：**不要用算法去改人的决定。**

两者产出的件命名规则不同，这是硬约束：

| 产出者 | 件名 | 依据 |
|---|---|---|
| `segment_semantic` | 语义名（head / arm / backpack）| 它认得出部位 |
| `cut_plane` | 位置名（piece_A / piece_B）| 契约原文：「它是几何块不是语义部位，命名只能按位置，不能编成 head/arm」|

### 2.2 工具写入值（`writes`，机器对账）

> 11-12 补。README 里那句「`writes` 列不在对账范围，仍需人工核」**原文保留在 README**，
> 但从此不再是真缺口：本表由 `check_terms.py` 第 6 类逐行对账，谁多写/少写一个键都 FAIL。
> 这也是 `scale_resolved` 那类幻觉的根 —— 技能读了契约里根本不存在的键名。

| 工具 | writes |
|---|---|
| `probe_import` | `model_path`, `src_sha`, `scale`, `scale_source` |
| `prep_lod` | `lod_path` |
| `segment_semantic` | `part_ids`, `parts_dir` |
| `seam_ingest` | `seam_norm` |
| `route_branch` | `branch` |
| `cut_plane` | `part_ids`, `parts_dir` |
| `clean_part` | `clean_dir` |
| `verify_part` | — |
| `find_contacts` | `pair_ids`, `contacts_dir` |
| `build_joint` | `joint_dir` |
| `verify_joint` | — |
| `export_part` | — |
| `assemble_check` | — |

## 三、7 个目标组（交付有三档，别混）

| 目标 | 需达成 | 含义 |
|---|---|---|
| `inspect` | `input_ok` | 只是能开工 |
| `parts` | `parts_available` | 件分出来了 |
| `clean` | `part_clean` | 逐件清过 |
| `verified` | `part_verified` | 逐件验过 |
| `joints` | `joints_verified` | 每对连接验过 |
| `deliver` | `part_exported` | **件导出了**（注意：不含任何连接事实）|
| `full` | `part_exported` + `joints_verified` + `assembled_ok` | **真的能拼** |

**`deliver` 不要求连接。** 所以「件全部导出成功」与「一碰就散」可以同时成立。
说「已交付」时必须指明是哪一档。

## 四、7 个常量

> ⚠️ **已过时（标题，11-10 快照 · 2026-11-12 标记）**：写「7 个」时契约确实只有 7 个常量，
> **原文保留不删**。11-11 内核阶段 1 新增 2 个语义阈值，现为 **9 个**，追加在表末两行。
> 判错依据：`manifest.json` 的 `const` 现有 9 键。本表由 `check_terms.py` 第 6 类逐项对账，
> 今后契约加常量而表里不加行 → **FAIL**。经验 → `refs/provenance.md` §8。

| 常量 | 值 | 谁用 |
|---|---|---|
| `lod_faces` | 89954 | `cut-solidify`：降面目标，面数是复验对齐的锚 |
| `coplanar_rms_mm` | 0.5 | `seam-admission`：≤0.5 走平面，否则曲面 |
| `pin_diameter_mm` | 3.0 | `joint-design`：销径 |
| `radial_clearance_mm` | 0.2 | `joint-design`：**单边**间隙 |
| `hole_depth_slack_mm` | 0.4 | `joint-design`：孔比销深的余量 |
| `min_wall_mm` | 0.8 | `cut-solidify`：**剔除阈值，非设计下限**（设计下限 1.2，见 `process-profiles.md`）|
| `min_contact_area_mm2` | 20.0 | `joint-design`：接触面**硬下限**；有效下限 28.3，见该技能第 3 条 |
| `semantic_match_floor` | 0.5 | `part-semantics`：语义匹配下限（11-11 阶段 1 新增）|
| `semantic_min_iou` | 0.6 | `part-semantics`：有 `ref_parts` 时的逐件 IoU 下限（11-11 阶段 1 新增）|

## 四之二、输入门与校验器（11-11 新增）

> 这两类字段**不是 id**，`check_terms.py` 前五类检查完全看不见 —— 契约加了它们，
> 技能仓一个字没提，脚本照样 PASS（11-12 实测）。本表是它们在技能仓的唯一登记处，
> 由第 6 类全量对账；契约增删一项，表里必须同步，否则 FAIL。

| 类别 | 字段 | 值 / 说明 |
|---|---|---|
| `needs_fields` | `inputs.model_kind` | `segment_semantic` 的输入门：**未声明模型类型不得触发语义分件**；人声明优先、模型提议需人确认、不确定拒收，分类依据进 `run.json` |
| `needs_fields` | `inputs.seam` | `seam_ingest` 的输入门：无人画割线则该工具不适用，规划器转语义路线 |
| `verifiers` | `input_ok` | 入厂质检 |
| `verifiers` | `lod_ready` | 降面就绪 |
| `verifiers` | `seam_ok` | 割线准入 |
| `verifiers` | `route_known` | 分支已定 |
| `verifiers` | `parts_available` | **11-11 已填真实现**：有 `ref_parts` → 表面采样 IoU / 漏分多分；无真值 → `UNKNOWN` 拒收 |
| `verifiers` | `part_clean` | 逐件清理 |
| `verifiers` | `part_verified` | 逐件几何复验 |
| `verifiers` | `contacts_found` | 接触面对 |
| `verifiers` | `joint_built` | 单对连接 |
| `verifiers` | `joints_verified` | 全部连接 |
| `verifiers` | `part_exported` | 逐件导出 |
| `verifiers` | `assembled_ok` | 装配检查 |

## 五、原因码与结局（技能报错只能从这里选）

| 码 | 含义 | 技能侧动作 |
|---|---|---|
| `NOT_IMPLEMENTED` | 能力未实现（契约已登记，实现为空）| **不得记成拒收**，结局 `UNDECIDABLE` |
| `TIMEOUT` | 工具超时 | 判断是降面不足还是几何过复杂 |
| `TOOL_ERROR` | 工具执行出错 | 区分「输入坏」与「工具坏」 |
| `PY_EXCEPTION` | 工具内部抛异常 | 同上 |
| `FACT_OUT_OF_SCOPE` | 工具声称产出了不属于它的事实 | 契约写错，停下修契约 |
| `VERIFY_FAILED` | 自称完成但校验不过 | **不重试同一路径**，先查判定 |
| `VERIFY_ERROR` | 校验器本身出错 | 校验器 bug，不是产物问题 |
| `MISSING_PARAM` | 契约参数运行时取不到 | 缺上游产物或任务单字段 |
| `UNKNOWN_FAILURE` | 失败没给原因 | 视为证据不足，`UNKNOWN` |

结局：`SUCCESS` / `SUCCESS_WITH_WARNINGS` / `REJECTED` / `FAILED` / `UNDECIDABLE`。

**`REJECTED` 与 `UNDECIDABLE` 的区别是本次任务最容易出错的地方：**
- `REJECTED` = 输入不合格，**主动不执行**
- `UNDECIDABLE` = 证据不足 / 能力未实现，**判不了**

能力没实现时判 `REJECTED` 就是撒谎——那等于说「你的模型不行」，而事实是「我还没写」。

## 六、`skip_instance` 的真实语义（六个工具在用）

`clean_part` / `verify_part` / `build_joint` / `verify_joint` / `export_part` 都是这个策略。
内核的行为（`loop.py`）：

- 只放弃**这一件 / 这一对**，其余实例继续
- **放弃不等于成功**：它留在账上，目标仍然不达成
- 会算出**连带受影响**的事实（cascade），一起写进警告
- 事件类型是 `abandon`，不进 `fail`

所以技能侧必须：

1. 逐件记账（哪件被放弃、什么原因、连带什么）
2. 不许把放弃件算进「全部通过」
3. 也不许因为一件放弃就判整批 `FAILED`

## 七、三条真实缺口（技能里写死，不是技能自己的规则）

1. **曲线分支无工具**
   `route_branch` 说「平整走半空间布尔，弯曲走贴合切割」，但 0.2.0 只登记了 `cut_plane`
   一个切割工具，它是半空间盒（平面）。**判得出 curved，切不了。**
   技能侧：`seam-admission` / `cut-solidify` 都写死「判成 curved 就停，不许用平面切蒙过去」。

2. **语义分件的正确性无法校验**
   `verify_part` 只查水密、自交、体积、壁厚——全几何量。
   一块从膝盖切下来的件，同样全部通过。**校验器能证明它是零件，不能证明它是那个部位。**
   技能侧：`part-semantics` 写死「部位正确性只能靠人确认」，且结论上限 `PASS_WITH_MANUAL`。

3. **语义分件无任何现成实现**
   契约里 `segment_semantic` 是全链唯一没有参考实现的一步（内核原话）。
   技能侧：不许用包围盒 / 位置规则顶替（L8：93.6% 可被位置复述）。
