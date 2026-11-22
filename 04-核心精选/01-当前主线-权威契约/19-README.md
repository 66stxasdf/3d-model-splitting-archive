# 19-分件技能 · 3D 模型拆分知识包

> 这一仓只有**知识**：每个环节「什么时候该停手」「凭什么放行」「能力到哪为止」。
> 不含 Agent、不含工具实现、不含算法。
> 工具与执行在 `../18-分件Agent内核`；几何原语与闸门在 `../16-分件Agent`（只读引用）。

## 一、三十秒读懂

一件模型要变成可打印、能拼回的零件，要过 13 个工具。每个工具都可能「跑通了但结果没用」。
本仓的六份技能，就是拦这种情况的：

| 技能 | 一句话职责 | 不看会怎样 |
|---|---|---|
| `split-intake` | 接单裁定 | 在单位错了 25.4 倍的模型上跑完整条链，全程无报错 |
| `seam-admission` | 割线准入与缝位 | 准入全过，缝从脸上穿过去 |
| `cut-solidify` | 降面 / 切割 / 清碎片 | 曲面切口留一圈薄片，被当成接触面 |
| `joint-design` | 连接结构 | 销做在厚件上，「3mm 的销插 2mm」 |
| `print-proof` | 可打印性与装配证明 | 壁厚 0.8 当成能打，实物裂 |
| `part-semantics` | 语义分区裁定 | 以为校验器全过就说明部位认对了（它只查几何）|

## 二、目录

```
19-分件技能/
├── refs/
│   ├── gates.md             G0–G10 语义（唯一来源，技能只写「我引哪个 Gx」）
│   ├── process-profiles.md  FDM 档位、公差、0.8 与 1.2 的区别
│   ├── provenance.md        四类来源、comparable:false、已证伪路线（带数据）
│   └── pack-contract.md     技能 ↔ 内核：12 事实 / 13 工具 / 原因码闭集
├── skills/<六个>/SKILL.md
├── check_terms.py           术语自检（只读，<0.5s）
├── _tryout/probe.py         试用脚手架：把 split-intake 要的证据真算一遍（非交付物）
└── README.md
```

## 三、地图

```
input.model
   │
   ▼
[split-intake] probe_import ──▶ input_ok
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                           ▼
          [cut-solidify]                 （无割线时跳过）
           prep_lod ──▶ lod_ready
                    │                           │
                    ├───────────────┐           ▼
                    │               │   [seam-admission]
                    ▼               │    seam_ingest ──▶ seam_ok
        ┌───────────────────────┐   │           │
        │ 二选一（priority 高者）│   │    route_branch ──▶ route_known
        ├───────────────────────┤   │           │
        │ segment_semantic (10) │   │           │
        │ [part-semantics]      │   │           │
        │   ↓                   │   │           │
        │ cut_plane (20)        │◀──┴───────────┘
        │ [cut-solidify]        │
        └───────────┬───────────┘
                    │  parts_available
                    ▼
             clean_part ──▶ part_clean        （逐件，skip_instance）
                    │
                    ▼
             verify_part ──▶ part_verified    （逐件，skip_instance）
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
 find_contacts            export_part
   contacts_found           part_exported   （逐件）
        │                       │
        ▼                       │
  build_joint ──▶ joint_built   │          （逐对，skip_instance）
        │                       │
        ▼                       │
 verify_joint ──▶ joints_verified
        │                       │
        └───────────┬───────────┘
                    ▼
             assemble_check ──▶ assembled_ok
```

三处必须看懂的：

1. **两条路汇到同一个事实 `parts_available`**。有割线时 `cut_plane`（priority 20）赢，
   没割线时 `segment_semantic`（10）走。`part-semantics` 不是旁挂的，它就在主管线上。
2. **`curved` 分支画不出来**：`route_branch` 能判出曲面，但 0.2.0 没有对应切割工具（见第七节）。
3. **`part_*` / `joint_*` 是逐件、逐对成立的**，一件失败不影响其余（`skip_instance`）。

## 四、加载策略

**按环节加载，不要全加载。** 六份同时塞进上下文，会互相稀释，等于没有重点。

| 触发 | 加载 |
|---|---|
| 刚拿到模型 | `split-intake` |
| 要用语义部位拆 | `part-semantics` |
| 有割线要判断 | `seam-admission` |
| 要切了 | `cut-solidify` |
| 切完了要拼 | `joint-design` |
| 要导出 | `print-proof` |

只加载一个的话，选 `split-intake`——它是唯一「错了整链皆错且看不出来」的门。

## 五、每份技能的结构

固定五节，顺序有意义：

```
一、立即停手的情形      ← 最重要。技能 80% 的价值在这里
二、判据（带数字与出处）
三、需要的证据
四、放行条件
五、能力状态（implemented / advisory / absent）
```

**没有「立即停手」一节的技能视为不合格**，`check_terms.py` 会直接报错。
理由：只讲流程的技能会让 agent 线性照做、永不停手。

`absent` 项一律要求写人工动作项，不许用「提醒一下」代替——
否则 agent 会拿一张截图当作完成了分析。

## 六、验证

```bash
python check_terms.py
```

五类检查：

1. **存在性** — frontmatter 里的每个 id 在 `manifest.json` 里真实存在
2. **完备性** — 全部工具、全部事实各自至少被一个技能覆盖
3. **唯一性** — 一个工具只归一个技能（事实允许共享：`parts_available` 就属于两个技能）
4. **幻觉 id** — 正文里形似内核 id 但表里没有的（如 `cut_adaptive`）
5. **契约快照** — `refs/pack-contract.md` 的工具表逐行与 manifest 对账
   （`produces` / `requires` / `priority` 三列）

第 5 类是 21:35 之后补的，因为前四类**同时失明**：两边 id 都存在，只有 `requires`
的内容不同。那次内核给 `segment_semantic` 补了 `lod_ready`，文档还写着 `input_ok`，
脚本照样 PASS——和「漂移不报错只静默失效」同一种病，只是换了个层。
补上后它第一次运行就抓出了这条：`表里 ['input_ok']，契约 ['input_ok','lod_ready']`。

`writes` 列不在对账范围（表里没有这一列），所以「谁写了哪个值」仍需人工核。

> ⚠️ **已过时（上面「五类检查」与「writes 仍需人工核」，11-10 快照 · 2026-11-12 标记）**：
> 两句原文保留不删。现为**七类检查**，`writes` 已由 `pack-contract.md` §2.2 表全量机器对账。
> 判错依据：11-11 契约新增的 `inputs.model_kind` 门与两个语义阈值不是 id，
> 前五类全看不见，技能仓零记载却照样 PASS；`scale_resolved` 幻觉也源于 writes 未对账。
> 台账 → `refs/provenance.md` §8。

6. **关键字段** — `pack-contract.md` 的常量表（9）/ 输入门表（2 门 + 12 校验器）/
   写入值表（13 工具）与 manifest 全量对账，增删一项即 FAIL
7. **未登记标识符** — 正文里所有 snake_case 自造词列成 INFO；前缀法抓不到陌生前缀
   （如 `merge_parts`），至少让人看得见

第 6 类一上就要求把 9 个常量、2 个输入门、12 个校验器、13 个工具的 `writes` 全部登记；
第 7 类顺带抓出 `scale_resolved` 这个真幻觉（契约字段是 `scale`），
已在 `split-intake` 就地标 `口径误用` 并保留原文 —— **标记被删也会 FAIL**。

正常输出还会多两行有用信息：

```
共享事实 parts_available ← cut-solidify、part-semantics
竞争产出 parts_available ← segment_semantic(p=10)、cut_plane(p=20)
```

后者直接告诉你「同一个事实有两个产出者、按 priority 选」，这在读技能时容易漏。

**为什么不靠人工审**：这种漂移**不报错、只静默失效**。
2026-11-10 21:00 内核把 split3d 从 0.1.0 升到 0.2.0（`parts_cut`→`parts_available`、
`cleanup_parts`→`clean_part`、工具 14→13），技能在 20 分钟内就过半作废，
而**没有任何一步会报错**——只有这个脚本会喊出来。

已验证有判别力：故意造一个 `covers: [cut_adaptive]`、`facts: [parts_solidified]` 的技能，
它报了 4 项（2 个不存在的 id + 2 项完备性缺失）；删掉后 PASS。

> 局限：只抓**形似内核 id** 的幻觉（按 `cut_` / `verify_` 之类前缀）。
> `merge_parts` 这种陌生前缀不会被抓，所以正文引用新概念时仍要靠人看一眼。

## 七、诚实的缺口（都在技能里写死了）

| 缺口 | 后果 | 技能怎么处理 |
|---|---|---|
| **缝位选择无工具** | 只能人画线 | `seam-admission` 拒绝自动选缝（AUC 0.5185）|
| **曲面分支无工具** | 判得出 `curved`，切不了 | `cut-solidify` 写死：判成 curved 就停，不许用平面切蒙过去 |
| **语义部位正确性无法校验** | 校验器只查几何，查不出「这块是不是头」| `part-semantics` 写死：只能人确认 |
| **语义分件无任何现成实现** | 全链唯一没有参考实现的一步 | 不许用包围盒顶替（L8：93.6% 可被位置复述）|
| **表面片补实体无事实** | 语义切出的壳不能打 | `part-semantics` 标注 absent |
| **悬垂 / 支撑零代码** | 不能判断要不要支撑 | `print-proof` 强制 UNKNOWN + 人工动作项 |
| **装配稳定性无仿真** | 能证对得齐，不能证摔不散 | 结论上限 `PASS_WITH_MANUAL` |
| **单位从未标定** | 所有绝对尺寸有前提 | 每处引用都带「单位未实测标定」|

## 八、三条已知的内核缺口（技能里已写成铁律）

1. **曲面分支没有切割工具**
   `route_branch` 宣称「弯曲走贴合切割」，但 0.2.0 只登记了 `cut_plane`（半空间盒 = 平面）。
   → `cut-solidify` / `seam-admission` 写死：判成 `curved` 就停，**不得用平面切蒙过去**

2. **语义部位的正确性无法被校验**
   `verify_part` 只查水密、自交、体积、壁厚，全是几何量。
   一块从膝盖切下来的件同样全过。**校验器能证明它是零件，不能证明它是那个部位。**
   → `part-semantics` 写死：部位正确性只能人确认，上限 `PASS_WITH_MANUAL`

3. **`deliver` 目标不含任何连接事实**
   `export_part` 只需要 `part_verified`，所以「件全部导出」与「一碰就散」可以同时成立。
   → `print-proof` 写死：报告必须标明交付档位（`deliver` / `joints` / `full`），不许笼统说「全部完成」

三条都是**技能兜住内核的洞**，不是技能自己的规则。内核补齐后可以删。

### 有一条曾经在这里，已由内核修掉

**`segment_semantic` 的 `requires` 漏了 `lod_ready`**（`params.src` 读 `$value.lod_path`，
契约没声明这条隐式依赖，孤跑抛 `MISSING_PARAM`，还会伪装成「分割器没装好」）。

原技能里写的是「调用前自己确认 `lod_ready` 已成立」——**这本身就是判据的反例**：
上游是否就绪能被静态判定，归内核，不该交给提示词自觉。
21:35 内核补了 `requires` 与 `probe_import.writes`，`doctor` 静态判死，验收加了 T25；
技能侧同步撤掉自查纪律，只保留「`MISSING_PARAM` ≠ 环境坏了」这一句错误码归属。

> 留在这里当例子：**技能里每出现一条「你先自己检查 X」，都要问 X 能不能被函数判定。**
> 能判的搬回内核，判不了的才留在技能。

## 八之二、本仓维护规矩：不删除，只增加与标记（2026-11-12 用户定）

**被判错的结论是资产。** 后续任何「对齐新契约 / 更正 / 更新状态」都按三步走：

1. **保留** — 旧段落原文一字不动留在原处，不用「现已改为…」覆盖，也不因为读起来矛盾就搬走。
2. **标记** — 紧挨原文加 `⚠️ 已证伪` / `⚠️ 已过时` / `⚠️ 已修复` / `⚠️ 口径误用`，
   必须带**判错依据**（数据或反例）与**现结论位置**。
3. **抽经验** — 在 `refs/provenance.md` §8「判定变更台账」追加一行，尽量写成脱离本项目也成立的教训。

本仓**没有 git**，整篇 `Write` 是不可恢复的。0.2.0 迁移时三份 `SKILL.md` 被重写，
0.1.0 的旧断言已无法逐字回溯——这正是本规矩要防的事故。
已示范：`skills/part-semantics/SKILL.md` 把曾被删掉的「调用前自查」段**按原样恢复并标记**，
现行规程放在它后面，两者并存。

第二大脑的集中台账：`D:\obsidian Two brain\wiki\concepts\ErrorAsExperience（判错结论即经验）.md`。

## 九、与另两个仓的关系

| 仓 | 角色 | 本仓怎么用它 |
|---|---|---|
| `16-分件Agent` | 几何原语 + 闸门 G0–G10 + regress 11 例 | **只读**。闸门语义抄进 `refs/gates.md` |
| `18-分件Agent内核` | 无领域知识的调度内核 + `packs/split3d` 契约 | **只读**。术语与它逐字对齐 |
| 本仓 | 六份技能 | 不改前两者一个字节 |

---

## 十、怎么接入

### 方式 A：直接按需引用（推荐）

不装、不复制。每次要用时让 agent 读那一份：

```
读 D:\workbuddy产物\19-分件技能\skills\joint-design\SKILL.md，
然后按它的判据检查这次连接设计。
```

优点：改技能立刻生效，不用重装；agent 只会读到你指的那一份，不会被六份稀释。
缺点：每次要指路径（可以写进任务单模板固定下来）。

**按环节指，不要一次指六份。** 见第四节加载策略。

### 方式 B：装成个人技能（一次装好，长期可用）

```bash
cp -r "D:/workbuddy产物/19-分件技能/skills/"* ~/.workbuddy/skills/
```

装完后，这些技能会按 `description` 里的触发词自动匹配。我的 `description` 是照
**错误症状**写的（「销做在厚件上」「围盒冒充语义分区」），所以描述症状比描述任务更容易命中。

⚠ 两个注意：

1. **校验仍要在原目录跑**。`check_terms.py` 靠相对路径找内核 manifest，
   复制到别处后路径断了——那时候它报的不是「术语错」，是「找不到契约」。
2. 副本会随时间与原目录漂移。建议**以原目录为唯一真源**，改完再覆盖安装。

### 方式 C：交给另一个对话里驱动内核的 agent

内核主规划器**不接 LLM**（这是刻意的：10 个节点 2 个分支的决策树，写成代码是确定的、可审计的、零成本的）。
所以技能不是被内核「自动加载」的，而是给**驱动内核的那一层**读的。

接法是两处映射：

| 技能里的东西 | 转成什么 |
|---|---|
| 第一节「立即停手」 | 任务单的准入前置 / 运行前的人确认项 |
| 第四节「放行条件」 | 期望结论，用来判内核给的 `verdict` 是否可信 |
| 第五节「能力状态」 | `absent` 项 → 报告里必须出现的人工动作项 |

即：**技能规定「什么叫做完」，内核负责「怎么做到」**。两者不互相调用，只共享术语（靠 `refs/pack-contract.md`）。

### 方式 D：人用（审阅清单）

不想接任何自动化时，它仍然有用：每次跑完，拿对应 SKILL.md 的第一节逐条核。

最省事的用法——只核三句话：

1. 单位是量出来的还是猜的？
2. 缝是谁画的？
3. 报告里有没有把 `absent` 项写成通过？

这三条能拦住绝大部分「跑通了但没用」的结果。

### 日常用法示例

| 你要做的事 | 说什么 |
|---|---|
| 拿到新模型 | 「读 `split-intake`，体检这个 model.stl」|
| 想按身体结构拆 | 「读 `part-semantics`，判断这个分区分区能不能用」|
| 已有割线要判断 | 「读 `seam-admission`，判这条线能不能切」|
| 准备切 | 「读 `cut-solidify`，确认前提是否齐」|
| 切完要拼 | 「读 `joint-design`，设计并验销孔」|
| 要导出 | 「读 `print-proof`，给最终结论」|

### 改技能之后

```bash
python check_terms.py     # 必须 PASS 再交付
```

加新技能：建 `skills/<新名>/SKILL.md`，frontmatter 写 `name`（与目录名一致）、
`description`（写错误症状）、`whenToUse`、`covers`、`facts`，正文必须有「立即停手」一节。
然后跑上面那条命令——漏覆盖、写错 id、忘了停手节，它都会报出来。

内核契约变了（工具改名、加事实）：先改 `refs/pack-contract.md`，再改技能，最后跑自检。
**顺序反了就会漏**，因为自检只能验「技能与契约一致」，验不出「契约变了但没人改技能」。
