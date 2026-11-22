# 多智能体工作台 · 交接说明（递给 WorkBuddy 继续创作）

> 交接时间：2026-11-05 20:00（Asia/Shanghai）
> 交接方：DeepSeek Harness 会话（工作区 `D:\deepseek`）
> 接手方：WorkBuddy 项目 `p_60b122aa9223463cad60cfeb3ed927da`
> 目的：把已跑通的工作台成果、设计决策、验证状态与未决方向完整交出，便于在 WorkBuddy 里接着创作。

---

## 1. 一句话现状

工作台已经是一个**可运行、已端到端验证**的本地 Web 应用：`http://127.0.0.1:8765`，
桌面图标「多模型工作台」一键启动。它不是演示稿，三套界面、11 个工具、3D 拆件流水线、
主从自主编排多智能体链路都跑通过并留有运行记录。

## 2. 产物清单（都在本机，可直接打开）

| 路径 | 是什么 |
|---|---|
| `D:\workbench\server.py` | 后端主程序（约 2048 行，纯 Python 标准库 + trimesh） |
| `D:\workbench\graph.html` | ComfyUI 式节点图界面（默认首页 `/` 与 `/graph`） |
| `D:\workbench\console.html` | 主从自主编排控制台（`/console`） |
| `D:\workbench\index.html` | 最早的三栏界面（`/classic`） |
| `D:\workbench\config.json` | 4 个模型槽 + 管理者模型 + 3 个子模型 + 编排预算 |
| `D:\workbench\graph.json` | 已保存的默认节点图（11 节点 14 连线，3D 拆件流程） |
| `D:\workbench\launch.vbs` / `ensure_port.py` | 启动器与端口清场脚本 |
| `D:\workbench\runs\*.json` | 每次运行的完整事件流、记忆、答案、verdict |
| `D:\workbench\previews\*.png` | 模型预览 / 多件彩色预览图 |
| `D:\workbench\out\split-*\partN.stl` | 切成 N 件的可打印 STL 产物 |
| `D:\workbench\test\*.png` | 浏览器逐步测试截图（含 `steps-contact-sheet.png` 拼版） |
| `D:\Users\Desktop\zhuomian\多模型工作台.lnk` | 桌面快捷方式（指向 `wscript.exe D:\workbench\launch.vbs`） |

**相关方案与调研文档（同目录 `D:\workbuddy产物\`）**
- `步骤说明书-AI自动分件管线.md`：原始 8 步管线方案书（Step1 入厂质检 → Step8 评估），
  两个硬约束「拆线隐蔽 / 连接方便」，锚点 `c̄ < 0.4`、水平切基线 `0.764`、水密率 100%。
- `Step1推敲-入厂质检细化.md`：Step1 的推敲产出与探针。
- `多智能体工作台-架构设计.md`（v1）：另一个 WorkBuddy 会话写的 ROS 技能 agent 架构
  （Coordinator / Blackboard / Bus / Planner），与本 Web 编排器是**同一主题的两条线**，见 §6。

## 3. 三套界面与能力

### 3.1 节点图（`/`、`/graph`）
连线即计划，模型只填参数。默认 11 节点 14 连线：
`任务 → 模型体检 → 规划者(思考推敲) → 拆解子任务 → 分派员(结合特征) → {入厂质检, 切成两件, 预览} → 代码闸门 → 结果`。
- 交互：空白拖动平移 / 滚轮缩放 / 端口拖拽连线 / 点线断开 / × 删节点 / 自动排列 / 保存 / 运行 / 中止 / `✦ AI 求解`。
- `$asset`、`$axis`、`$height` 是「上游输出喂下游参数」；工具下拉已全部中文化。
- 已修：左键平移误触发文字选中（捕获阶段 `preventDefault` 才有效，冒泡阶段已来不及）。

### 3.2 自主编排控制台（`/console`）
按 GPT Pro / Deep Research 式**主从自主编排**：
任务 → 管理者模型后台计算 → 并行子模型 → 回传 → 共享记忆 → 管理者判断继续/收尾 → 多轮迭代。
- 管理者每轮输出决策 JSON：`{thinking, status:"continue|finish", memory_note,
  subtasks:[{id,to,task,why,handoff_from,context}], answer, confidence}`。
- `handoff_from` 把某个子任务产出注入另一子模型的 prompt；共享记忆每轮所有模型都能读。
- 预算可界面按次覆盖：`max_rounds`(默认3) / `max_subtasks`(12) / `max_seconds`(180)
  / `parallel` / `step_delay`(0.45s，仅演示模式生效，用于「让我看见步骤」)。
- 界面有轮次胶囊、共享记忆面板、事件时间线、答案区。

### 3.3 后端工具箱（11 项）
`asset_list` 列出资产 / `asset_profile` 模型体检 / `mesh_qc` 入厂质检 /
`slice_two` 切成两件 / `split_n` 切成 N 件+STL导出 / `render_preview` 生成预览图 /
`python` 执行代码 / `http` 请求 / `file_write` / `file_read` / `llm`。

主要端点：`/api/graph/run`、`/api/solve`、`/api/delegate`、`/api/preview`、
`/api/abort`、`/api/save`、`/api/events`、`/api/bootstrap`、`/api/graph`。

## 4. 已验证的事实（可复跑）

**3D 拆件**
- `astronaut_kid.glb`（`D:\workbuddy产物\02-AstronautKid-宇航员\`）：305,209 顶点 /
  499,990 面；UV 缝 106,132 条边界，`merge_vertices(merge_tex=True, merge_norm=True)`
  焊成 249,969 顶点 / 0 边界 / 水密。
- 沿 Y 切两件：241,144 + 268,178 面，体积和 0.081837，**误差 2.34e-06**，全程 7.2s。
- `split_n(3)`：135,066 / 263,578 / 109,702 面，全部 `watertight`，`volume_err = 0.0`，
  STL 6.7 / 13.2 / 5.5 MB，彩色预览 `D:\workbench\previews\nparts-1793856467804-3.png`。
- 性能：一次运行 18.4s → 6.0s（`_geom` 几何缓存 + `_PREVIEW_CACHE`）；
  `asset_profile` 11.8→3.9s、`mesh_qc` 4.8→0.94s、24 个切面候选 0.39s。

**编排链路**
- 默认 3 轮 `pass`（24 事件 / 1 次 handoff / 记忆 7 条 / 答案 237 字）；
  1 轮预算 `pass`；中止 `fail`（「运行被用户中止」）；落盘 `mode=orchestration`。
- 最终回归：`/`、`/console`、`/graph`、`/classic`、`/api/bootstrap`、`/api/graph` 全 200；
  委派 smoke `run-195859-07b2` 13 事件 `verdict=pass`；3D 与 `/api/solve` 回归全 pass。
- 对抗：成环 → 人话报错「连线成环，无法确定执行顺序」；未知工具 → fail；
  只有预览节点 → fail（闸门不再空转假通过）；坏 API Key → 优雅降级且不写脏配置。

**Step1 推敲（科研侧，重要结论）**
- 5 个真实资产的边界顶点间隙谱是「0 或远大于 1e-4」的**双峰**，
  ⇒ 原文档主张的「第三类亚像素裂缝」在真实资产里不存在，其证据是自造 1e-4 扰动的合成 box（循环论证）。
  真正缺的第三类是**非流形**（`kid_upload` 焊后边界 0 却有 8 条非流形边 → 水密 False）。
- `merge_vertices` 直接读 `_cache["vertex_normals"]`：**冷/热缓存会让默认焊合结果不同**，
  所以必须显式传 `merge_tex=True, merge_norm=True`，让行为与调用历史无关。
- 过焊（放粗容差）会静默制造非流形边，使水密 True→False；上限应由「非流形边拐点」
  与「打印分辨率 0.5×喷嘴/diag」取小，而不是 `1e-3·bbox_diag`。
- 水密只能当**前提**（切片/布尔/体积/封盖/打印），不能当验收指标：
  分离或交叠的两个 box 都是 `watertight=True` 但体积双计；建议 Step1 补**自交检测**。
- AIGC 网格**无单位**，Step5/6 的全套物理量验收差 1000 倍 → 必须先做尺度归一化。

## 5. 关键限制（接手前必读）

1. **本机没有真模型凭据**：`config.json` 里 DeepSeek `api_key` 为空、Ollama
   `http://127.0.0.1:11434` 不通、环境变量无 `*_API_KEY`。
   所以决策由**离线演示规划器**（`mock_reply` 的演示分支 + `mock_plan` 规则）产生。
   链路、多轮、转交、记忆、预算、并发、中止**全都是真的**，但**不是万能 AI**，界面已明确标注。
   填入 Key 或启动 Ollama 后，同一套 `ORCH_MANAGER_SYS` 决策 JSON 契约由真实模型接管，无需改架构。
2. **切面仍是轴对齐平面**：`_best_cut()` 用真实 `mesh_plane` 求交周长 +
   偏心惩罚打分，还没接方案书 Step3 的 `cut_cost` 场。
3. **缺条件分支节点**：Step1b「有真破洞才修复」与 Step5↔Step6 回退循环，
   当前 DAG 只能表达直线拓扑排序，表达不了条件/回边。
4. **预览是 numpy 手写点云光栅化**（无 GPU/OpenGL），薄壁小件边缘略毛。
5. **决策 JSON 靠文本解析**（取首尾花括号），偶发不合规会回落；建议换
   function calling / JSON mode 强校验。

## 6. 端口冲突（本次刚修，务必注意）

`8765` 曾被**另一个 WorkBuddy 会话**的 `D:\Users\Desktop\zhuomian\rob\main_workbench.py`
（ROS 机器人装配 demo，`start_console(bb, port=8765)`）抢占。Windows 的 `SO_REUSEADDR`
允许多进程绑同一地址、后绑者截走请求，表现为「打不开了」/ 404。

修复三层：
- `server.py`：`WorkbenchServer(ThreadingHTTPServer)` 设 `allow_reuse_address=False`，
  `server_bind()` 在 bind **之前** `setsockopt(SO_EXCLUSIVEADDRUSE, 1)`；bind 失败重试 12×0.5s，
  仍失败明确报错退出。实测别的进程再绑得到 `[WinError 10013]`。
- 新增 `ensure_port.py`：启动前探 `/api/graph` 判断是不是本工作台，列 `netstat` 监听 PID，
  `taskkill /F` 清掉外来占端口者，日志 `D:\workbench\ensure_port.log`。
- `launch.vbs` 全 ASCII 重写：用**控制台版 `python.exe`**（不是 pythonw，`cmd /c` 才会等它）
  跑 `ensure_port`，再拉起服务，然后 40×500ms 轮询判活。

**给接手方的提醒**：只要那个 rob 会话还开着，它就可能再次抢 8765。根治要么关掉那个
WorkBuddy 会话，要么把 `main_workbench.py` 的 `start_console(bb, port=...)` 换成别的端口。
再点一次桌面图标也能自动恢复。

## 7. 建议的继续创作方向（按优先级）

1. **接真模型**：在 `/console` 左侧填 DeepSeek Key（或起 Ollama 拉 `qwen2.5:7b`），
   把决策 JSON 换成 function calling / JSON mode 强校验，让管理者真正自主规划任意问题。
2. **把 Step3 `cut_cost` 场接进编排器**：
   `cut_cost = 0.35*vis + 0.65*(1-conc)`（p2/p98 拉伸）、
   Step4 边权 `w(e) = 0.5*(c(f1)+c(f2)) − λ1*[label≠] + λ2*[禁切]`，
   让子任务能真调 PartField / 对偶图 min-cut，而不是轴对齐平面；
   评测做消融（完整 / 去禁切区 / 去混合切 / 全去≈水平切基线 `c̄=0.764`）。
3. **加条件分支节点**：Step1b「有真破洞才修复」、Step5↔Step6 封盖校验失败回退，
   以及 Step4 的 N>2 多路割（须递归二分割）。实现上应写成 stage 函数 + 调度器（带缓存），
   不是 8 步 for 循环。
4. **两条线合流**：`多智能体工作台-架构设计.md`（ROS 技能 agent：Coordinator /
   Blackboard / Bus / `execute(task, ctx)` 契约）与本机 Web 编排器是同一主题，
   可择一为主线，或把 ROS agent 作为一类「子模型/工具」注册进 `console.html` 的团队编排。
5. **补物理尺度归一化**（AIGC 网格无单位）、**自交检测**（Step1 目前缺）、
   **连接件干涉通过率**与「拆线隐蔽」的独立验收指标（当前 `c̄` 与目标函数自证，需独立指标）。
