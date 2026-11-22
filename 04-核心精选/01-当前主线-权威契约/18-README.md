# agentcore — 领域无关的 Agent 内核

给「一个目标 + 一堆工具」，自己推出该按什么顺序做、缺什么、失败了怎么办，并把每一步的理由留下。

**它不认识任何具体领域。** 不知道什么是网格、割线、定位销。能力全部来自「领域包」，包由别人写。

```bash
python -m agentcore packs                          # 有哪些领域包
python -m agentcore doctor demo                    # 契约自检（给写工具的人用）
python -m agentcore plan demo --input src=x.txt --goal deliver   # 只推演，1 秒
python -m agentcore run  demo --input src=x.txt --goal deliver   # 真跑
python -m agentcore explain last                   # 复盘：为什么走这条路
```

---

## 为什么要有这个

上一版（`../16-分件Agent/`）里，工具契约直接写在 Agent 代码里：`toolspec.py` 第二十几行就 `from ..tools import cut_tools, geom_tools`。那样看着像 Agent，实际是**焊死在几何上的流水线**——换个领域就得改内核。

这一版把边界划清了：

| | 谁负责 | 在哪 |
|---|---|---|
| 顺序怎么来 | 内核 | `agentcore/planner.py`（反向展开，零领域名词） |
| 预算、防环、失败策略、留痕 | 内核 | `agentcore/loop.py` |
| 独立校验（不信自称） | 内核调度，**校验逻辑由包提供** | `agentcore/loop.py` + `packs/*/verify` |
| 有哪些事实、工具怎么依赖、怎么实现 | **领域包作者** | `packs/*/manifest.json` + 实现模块 |

验收里有一条专门证明这件事：同一份 `Loop/Planner/World`，跑文本分块和跑分件，内核文件一个都没改（`accept.py` 的 T15）。

---

## 快速开始

```bash
cd "D:/workbuddy产物/18-分件Agent内核"

# 1 看契约是否自洽，以及哪些活儿还没人干
python -m agentcore doctor demo
python -m agentcore doctor split3d          # 骨架包：契约齐、实现全是桩

# 2 只看它打算怎么做（不落盘，秒级）
python -m agentcore plan demo --input src=fixtures/sample_small.txt --goal deliver

# 3 真跑
python -m agentcore run demo --input src=fixtures/sample_small.txt --goal full

# 4 看它为什么这么做
python -m agentcore explain last
cat runs/<run_id>/report.md
```

产物在 `runs/<run_id>/`：

| 文件 | 用途 |
|---|---|
| `run.json` | 机器读的唯一权威：判定、决策、证据、产物、被否决的备选 |
| `report.md` | 人读。**全部从 run.json 生成**，不另起一套结论 |
| `events.jsonl` | 逐条事件，追时序和耗时 |
| `task.json` | 本次任务单，报告里的复现命令直接用它 |
| `state.json` | 续跑锚点。只存世界状态，不存"第几步" |

---

## 目录

```
agentcore/            内核，约 3900 行，零领域知识
  world.py            事实集合（两种量词）+ 值 + 实例作用域 + 证据 + 阻塞/跳过/放弃
  contract.py         事实/工具的数据定义 + 严格静态校验 + 参数解析
  expr.py             条件 mini DSL（条件必须是数据，不能是代码）
  pack.py             领域包加载（按文件路径加载实现，不用先装）
  planner.py          目标反向展开 + 路线优先 + 按实例展开 + 不可达诊断
  executor.py         python / cmd 两种执行适配
  loop.py             主循环：预算/防环/不自证/失败策略/留痕/续跑/放弃级联
  journal.py          事件账本 + run.json + report.md（含按件统计矩阵）
  doctor.py           契约自检（L1 结构 / L2 语义 / L3 实物）
  llm.py              LLM 规划器：接口留着，默认拒绝，理由写在文件里
  cli.py              命令行

packs/
  demo/               能跑通的演示包：单件分块流水线 + 12 章逐章多件场景（秒级）
  split3d/            分件骨架包 0.2.0：语义分件契约齐、13 个实现待填

PACK-SPEC.md          给写工具的人 → 接口说明书（含按实例速查表、工具/技能归属判据）
DESIGN.md             为什么这么设计、踩过哪些坑
accept.py             26 项验收
```

---

## 验收：26/26，约 7 秒

```bash
python accept.py
```

前 16 条证的是「它算不算 Agent」；T17~T24 证的是多件场景下**它会不会安静地少做事**——
后者更要紧，因为假成功不报错。T25 证的是**能被静态判定的事不会被推给技能**；
T26 证的是**接口书里抄的契约片段不会悄悄与 manifest 漂移**（与技能侧第 5 类检查同一个思想）。

| # | 证什么 | 怎么证 |
|---|---|---|
| T1 | 最小目标不多跑 | 只要质检 → 1 步收工，零产物 |
| T2 | 路径是推出来的 | 交付路径恰好 8 步，断言依赖约束而非写死顺序 |
| T3 | 加目标自动插对位置 | 加「重新拼合」→ assemble 插在 export 之前 |
| T4 | 条件分支生效 | 指定 adaptive 模式 → 走 `cut_adaptive`，不走 `cut_fixed` |
| T5 | 缺输入主动拒收 | 源文件不存在 → REJECTED，零产物 |
| T6 | 准入失败 ≠ 执行失败 | 空文件 → 准入工具拦下，判 REJECTED 而非 FAILED |
| T7 | 预算有上限 | 3 步上限 → 停在 BUDGET，如实说目标未达成 |
| T8 | 防环 | 硬塞重复动作 → 拦截，不空转到预算耗尽 |
| T9 | 失败自己改道 | 注入清理故障 → 自动换 `cleanup_keepall`，理由进报告 |
| T10 | 不信自称 | 工具撒谎（说切好了但产物是空的）→ 校验器拦下 |
| T11 | 换实现不改内核 | 同一契约换个实现 → 产物份数从 2 变 3，内核零改动 |
| T12 | 缺能力如实报 | 骨架包跑起来 → UNDECIDABLE + NOT_IMPLEMENTED |
| T13 | 缺输入 0 步诊断 | 分件缺割线 → 0 步报出依赖链 |
| T14 | 契约错误提前暴露 | 坏契约 → doctor 一次指出未登记事实、缺校验器、成环、语法错 |
| T15 | 两个领域共用内核 | demo 与 split3d 用的是同一份 Loop/Planner/World |
| T16 | 续跑不重做 | 第二次运行 0 个动作，事实从 state.json 恢复 |
| T17 | **12 件跑满 12 次** | 38 步 SUCCESS、12 个交付产物、36 个实例动作、零重复拦截 |
| T18 | **某件谎报拦得住且说清是哪件** | 等长替换一个字（字符数仍对）→ 逐字重算抓到 `ch05` |
| T19 | **少一件不许判成功** | 第 5 件坏 → 其余 11 件做完，坏件与下游都入账，结局非成功 |
| T20 | **清单撒谎拦得住** | 报 12 章、磁盘只留 5 个 → 校验器数磁盘 |
| T21 | 三态不混 | 未知 / 空清单 / 部分 / 全部，四种状态互不等价 |
| T22 | 预览预报会跑 N 次 | 静态推演标明「实例数待运行时确定」 |
| T23 | 两条入口自动切换 | 不给割线走 `segment_semantic`，给了走 `seam_ingest → route_branch → cut_plane` |
| T24 | **全局值通道通、实例值不串台** | 按实例工具写 `values_global`，下游用 `$value` 读得到；实例私有值不污染全局 |
| T25 | **隐式依赖 doctor 静态判死** | 漏 `requires` / 漏 `writes` 各造一份坏契约 → 都被拦下，不靠技能调用前自觉 |
| T26 | **文档契约快照 ↔ manifest** | 接口书 3.11 示例改回旧值 / 删掉锚点标记 → 都 FAIL；故意写错的反例不误伤。双层负向实验 |

多件场景真跑（12 章 × 3 环节 + 2 前置 = 38 步，默认 32 步不够，要显式给预算）：

```bash
python -m agentcore run demo --input src=fixtures/sample_small.txt --goal book --budget-steps 40
```

预算不足时不会假成功，而是 `UNDECIDABLE / BUDGET_EXHAUSTED`，
并在报告里给出「建议把 budget.max_steps 提到至少 38」。

---

## 一个典型的决策日志

`python -m agentcore explain last` 里的一段：

```
  2. route              → 处理模式已确定  [OK] 0.0s
      为什么：「处理模式已确定」是当前可达目标里最近的一环（深度 4），前置已齐：输入可用
      未选 ingest：离目标更远（深度 5 > 4）
  3. ingest             → 文本已分块  [OK] 0.002s
      为什么：「文本已分块」是当前可达目标里最近的一环（深度 5），前置已齐：输入可用
      （另有 2 个工具因前置未满足被排除）
```

注意第 2 步的「未选 ingest：离目标更远」——**这就是 Agent 和脚本在日志上最直观的区别**。
脚本只会告诉你它做了什么；这里还告诉你它当时还能做什么、为什么没选。

---

## 边界

**内核不做的事：**

- 不评价领域做法好坏。工具选得对不对、参数合不合理，由包作者负责。
- 不猜参数。缺了就说缺，不填默认值。
- 不替工具判断完成。必须过校验器。
- 不让 LLM 参与主规划。这条链确定性可解，模型只会带来不确定性。
  三处窄接口（任务单解析 / 失败翻译 / 报告叙事）都留了位置，都可关。

**`split3d` 契约的形状（0.2.0）：**

输入整体公仔 → `parts_available` 一次产出 `part_ids`（`head` / `arm_l` / `bag` / `hair`…）
→ `part_clean` / `part_verified` / `part_exported` **逐件**各做一遍
→ `contacts_found` 产出 `pair_ids` → `joint_built` / `joints_verified` **逐对**各做一遍
→ `assemble_check` 收尾。带 `domain` 的事实由内核按清单展开，不需要在工具里写循环。
两条入口并存：没割线走 `segment_semantic`，给了割线走 `seam_ingest → route_branch → cut_plane`，
由 `priority` 决定路线，路径自己切换。

**目前包层面还没做的（split3d 的 13 个实现全是桩）：**

- 语义部位识别（`segment_semantic`）。契约位、校验器位、两条技术路线都写在
  `split3d_impl.py` 注释里，实现是桩。这是整件事最大的缺口。
- 表面片补成可打印实体（HoloPart 那一路）。
- >2 件的连接图与销位全局分配、装配序列、打印朝向、分色、物理仿真装配验证。

这些都不需要改内核：写工具 → 在 manifest 里登记一条契约 → 规划器自动把它排进链。
按实例工具怎么写、实例参数从哪取、全局值走哪条通道，见 `PACK-SPEC.md` 第十一节速查表。

**语义识别归谁（这条别搞错）：**

它是 **Agent 侧的工具，不是技能**。判据是「能被函数判定成败 → 工具；
只能由人定判定标准 → 技能；两样都不成立 → 两边都不写」。
`segment_semantic` 有 `cost_s: 300`、`timeout_s: 900`、`priority`、`on_fail`——
技能里不会写这些字段。技能只负责三段裁定（走哪条技术路线、标签能不能采信、
`SEGMENTATION_EMPTY` 是没分出来还是模型不可拆），以及**在能力缺失时产出「拒收」**。

契约里因此没有「部位正确性」这类事实或校验器：`part_verified` 只查水密、自交、
体积、壁厚，一块从膝盖切下来的件同样全过。**校验器能证明它是零件，
不能证明它是那个部位**——这个缺口不会因工具补齐而消失，只能靠人确认边界。
把它写成代码校验，就是 L8 那条已被证伪的路（规则标签 93.6% 能被纯位置复述）。

完整归属表见 `PACK-SPEC.md` 第十二节。**判据按片段用，不按整段用**：
内核的静态检查只覆盖 `$value.*`，`$input.*` / `task` / `env` 是运行时字段，
漏传时 `MISSING_PARAM` 仍会真实发生，而「这个码不等于环境坏了」是判定标准、留在技能。
删掉技能里的「调用前自查」是对的，**把整段一起删掉是错的**（边界见 §12.1）。

---

## 下一步往哪改

跨仓的下一阶段流程（想法整理、推敲、阶段 0~4、验收与停手条件）见 `ROADMAP.md`。

| 想改什么 | 改哪 |
|---|---|
| Agent 会做什么 | `packs/<包>/manifest.json` 的契约。别改 planner |
| Agent 怎么选 | `agentcore/planner.py`，但新规划器必须走同一套 `requires + guard` 校验 |
| 加新能力 | 先写工具，再登记一条契约（`produces` 只写一个事实）。`loop.py` 一行不用动 |
| 校验规则 | 包里的 verify 模块。**别把校验写进工具内部**，那等于自己判自己 |
