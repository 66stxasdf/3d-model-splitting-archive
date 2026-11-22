# 3D模型拆割（分割/拆解）问题与 AIGC 网格修复：发展史综述

> 检索范围：中英文网络（学术论文、综述、工业工具文档、arXiv 预印本、厂商博客），覆盖 1984–2026。
> 说明：「拆割」在几何处理领域通常对应 **mesh segmentation（网格分割 / 部件拆解）**，即把三维网格按几何/语义特征分解为若干连通子网格片；与之并列的是 **shape completion / repair（形状补全 / 网格修复）**，即修复残缺、非流形或拓扑错误的网格。本文按两条主线梳理其发展脉络，并指出二者在 AIGC 时代的交汇。

---

## 一、3D模型拆割（分割 / 拆解）问题的发展史

### 1. 思想起源：感知心理学与几何基础（1980s–1990s）

- **1984 · 最小规则（Minima Rule）**：Hoffman & Richards 提出，人类对物体的认知倾向于把**最显著负曲率线**作为部件边界。这是「有意义分割（meaningful segmentation）」的理论起点——子网格需具备与应用相关的尺寸与组织结构。该方法以「体积凸性（volumetric convexity）」描述符落地。
- **1991 · 分水岭算法上三维**：Vincent 等将图像处理中的分水岭（watershed）推广到任意拓扑连接的 3D 曲面网格分割，是较早的系统性工作。
- **1992 · 曲率面片化 & 凸分解**：Falcidieno 等按曲率相近原则，把曲面分为凹面片、凸面片、马鞍面片、平面片；同年 Bajaj & Dey 提出凸分解算法（但 3D 凸分解代价高昂）。
- **1993 · 法向分组**：Maillot 等将三角片按法向分组，实现自动分割。

### 2. 几何驱动方法时代（1990s–2000s）：聚类、区域增长、谱方法

这一阶段的算法多从「图像处理」和「计算几何凸分割」两类早期工作推广而来，核心是用几何描述符 + 区域操作。

| 年份 | 代表工作 | 核心思想 |
|---|---|---|
| 1995 | Hebert | 二次曲面拟合估计曲率 + 区域增长（region growing） |
| 1995 | Chazelle 等 | 以凸性条件作为区域增长的停止准则 |
| 1996 | Krishnamurthy & Levoy | 交互式分割（动画变形中引入人工参与） |
| 1997 | Wu 等 | 基于物理：模拟电场在网格上的分布 |
| 1998/2000 | Lee；Guskov 等 | 多分辨率（简化模型）分割 |
| 1999 | Mangan & Whitaker | 分水岭用于网格，较好解决过分割（over-segmentation） |
| 1999 | Gregory 等 | 交互式：据特征点分割为变形对应片 |
| 2000 | Rossl 等 | 网格上定义面向曲率信号的数学校态学开闭操作→去噪骨架 |
| 2000 | Sander 等 | 区域增长，按「趋平、紧凑」原则合并 |
| 2001 | Werghi | 识别人体姿态，依局部形状索引分割人体扫描模型 |
| 2002 | Zuckerberger 等 | 基于凸性的区域增长 |
| 2004 | Lavoué | 针对 CAD 模型，分析曲率张量得到连续分割区 |
| 2005 | Tsuzuki 等 | 比较相邻三角面二面角 + 距离，自动分割 |
| 2005 | Katz 等（Core Extraction） | 模糊聚类逐层分割，规避过分割与锯齿边界 |
| 2007 | Lien & Amato | **近似凸分解**（低代价逼近凸部件） |
| 2008 | Lai 等 | 扩展图像随机游走（Random Walk）到网格，噪声鲁棒、支持交互 |
| 2008 | Shapira 等 | **形状直径函数（SDF）**：把体积信息映射到表面，区分厚/薄部件 |
| 2008 | Lavoué & Wolf | 基于马尔可夫随机场（MRF），用局部交互获得全局最优解 |

> 同期，**谱方法（spectral analysis）** 与 **Reeb 图** 也被用于网格分解；分类学上常按「聚类 / 区域增长 / 曲面拟合 / 拓扑 / 空间细分 / 谱分析 / 边界检测 / 运动」等范式组织。

### 3. 语义与学习方法的兴起（2009–2017）

- **2009 · Princeton 分割基准（PSB）**：Chen, Golovinskiy & Funkhouser 招募 80 人对 380 个网格做人工部件标注，建立「人类如何分解物体」的概率化 ground truth，成为量化评测的里程碑（对比 K-Means、Random Walks、SDF、Normalized Cuts、Core Extraction 等算法）。
- **2010 · 首个学习型方法**：Kalogerakis 等设计 **CRF 模型**，用人工标注网格学习并优化参数，在 PSB 上取得显著效果，开启「特征描述符 + 学习映射」范式。
- **2015 · 综述高峰**：ScienceDirect 发表 *A comprehensive overview of methodologies and performance evaluation frameworks in 3D mesh segmentation*，提出新方法分类并系统讨论评测框架。
- **2017 · Kalogerakis 等** 进一步提出更丰富的学习模型；同年 **PointNet（Qi 等）** 首次实现无序点云上的直接特征学习，为深度学习分割铺路。

### 4. 深度学习时代（2017–至今）：点云、Transformer、视觉基础模型

- **2017–2019**：PointNet++（层级局部特征）、Dynamic Graph CNN、MeshCNN（Hanocka 等，2019，网格边卷积）。
- **2018–2020**：SGPN、GSPN（基于 PointNet++ 的实例分割/形状提案）；**BSP-Net（2020）** 用二叉空间剖分生成紧凑网格；半监督/弱监督方法（Shu 等 2016 软密度峰聚类 + 自编码器；Xu & Lee 2020 弱监督）降低标注依赖。
- **2022 · SuperDec**：基于 Transformer 把点云分解为超二次曲面基元（superquadric primitives），从物体级泛化到整场景。
- **2022–2024 · 视觉基础模型向 3D 迁移**：
  - **SA3D**：把 SAM 扩展到 NeRF，单视角提示即可 3D 分割；
  - **PartSLIP**：把 GLIP 图像-语言知识迁移到 3D 点云，零/少样本部件分割；
  - **SAMPart3D**：蒸馏视觉基础模型特征到 3D 主干，多粒度零样本部件分割；
  - Tang 等（2024）把 SAM 扩展到 3D 做零样本实例分割；Zhu 等把 CLIP+GPT-3 用于开放世界 3D 学习。
- **2024 · 生成式 3D 外观设计综述（Oxford JCDE）**：将生成式 AI 的分割分为**实例级（instance-level，无语义标注）**与**部件级（part-level，带语义标注）**两类，并纳入多视角 / 文本驱动分割。
- **2024 · MTSegNet**：流形 Transformer 用于 3D 形状分割，系统回顾非学习（Hoffman 1984、SDF 2008、MRF 2008 等）与学习（CRF 2010 起）两条线。
- **2026 · 人机协同（Human-in-the-Loop）atlas 法**：arXiv 2606.17824 提出结合路径规划式视角选择 + SAM 2 + 交互标注平台，把多视角 2D 分割反投影到模型 UV 参数化，产出可分段着色的 2D atlas，弥补纯自动方法「用户无法定义何为有意义的段」的缺陷。

### 拆割主线小结（阶段演进）

```
感知心理学(1984) → 几何描述符+区域操作(1990s–2000s)
   → 学习型CRF+基准(2009–2017) → 深度学习点云/Transformer(2017–2022)
   → 视觉基础模型零样本+人机协同(2022–2026)
```

---

## 二、AIGC 网格修复的发展史

「网格修复」按技术动因可分为三代：**传统几何修复**（扫描/打印驱动）、**数据驱动形状补全**（深度学习补全残缺）、**AIGC 生成后修复**（针对生成式网格的结构性缺陷）。

### 1. 传统几何修复时代（1990s–2010s）：从「三角形汤」到可打印实体

修复对象是扫描重建、CAD 导出、3D 打印带来的**非流形、破洞、重复顶点、自交、法向翻转**等问题。

- **1995 · Barequet & Sharir**：用最小权三角化填补多面体边界的缝隙（洞填充的算法基石）。
- **1990s末 · ADMesh**（Anthony D. Martin）：最早的命令行 STL 检查/修复工具，奠定「系统化、可脚本化」的修复理念。
- **2001 · Guéziec 等**：用局部算子切割+缝合，把多边形集合转为 2-流形；**Murali & Funkhouser**：BSP 树体素法（首个体素法）。
- **2003 · Liepa**：洞填充流水线（边界识别 → 三角化 → 细分 → 光滑），影响深远；**Nooruddin & Turk**：体素化 + 形态学算子的修复与拓扑简化。
- **2004 · Ju**：八叉树鲁棒修复，把「多边形汤」转为 2-流形，保留细节与锐利特征。
- **2005 · Bischoff 等**：用八叉树同时去除组合/几何/拓扑奇异性（CAD，首篇同时解决三类奇异）；**Podolak & Rusinkiewicz**：体素图（内/外子图）网格补全。
- **2006–2008**：Patel 等（CAD 拓扑自动生成）、Sagawa & Ikeuchi（自适应分辨率下翻转 SDF 符号补洞）。
- **2007/2009/2013 三代权威综述**：Veleba & Felkel（2007）、Ju（2009）、Attene, Campen & Kobbelt（2013）。
- **2009 · Wojtan 等**：显式/隐式混合的曲面追踪拓扑变化修复（保留细节、只修缺陷区）。
- **2010 · Campen & Kobbelt**：基于平面的精确自交消解；同年 **Attene 的 MeshFix**（2010 提出，2014 扩展支持直接修自交网格）成为轻量标准工具（测试 400+ 低质量数字化模型零失败）。
- **2016 · Zhou, Grinspun, Zorin & Jacobson**：网格排列（mesh arrangements）实现精确实体修复。
- **工业侧**：Materialise **Magics** 成为服务商的「人工在环」修复控制台（彩色诊断、可编辑缺陷清单、布尔/壳操作）。

### 2. 数据驱动的形状补全（2015–2022）：点云 / 体素 / 隐式补全

随着深度学习兴起，「修复」从几何启发式转向**从数据中学形状先验**的补全（completion）。代表分三族：

- **体素派**：3D ShapeNets（Wu 等, 2015）→ VConv-DAE（Sharma 等, 2016）→ 3D-EPN（Dai 等, 2017）→ GRNet（2020）、O-CNNs（2020）、CarveNet（2021）、VE-PCN（2021）。瓶颈：O(N³) 内存，分辨率受限。
- **点云派**：AtlasNet（Groueix 等, 2018，把 2D 平面「折叠」成网格）、PCN（Yuan 等, 2018，端到端点云补全，粗→细两阶段）、FoldingNet（Yang 等, 2018）、TopNet（Tchapmi 等, 2019，树状解码）、PF-Net（Huang 等, 2020）、CRN / MSN / PoinTr（Yu 等, 2021，几何感知 Transformer）。
- **隐式派**：DeepSDF（2019）、Occupancy Networks（2019）用连续函数表达形状，支持任意分辨率；NeRF/NeuS 推动神经场。
- **针对缺陷点云的修复**：2022 *3D Mesh Generation from a Defective Point Cloud using Style Transformation* 用自编码器 + 风格迁移「修补」残缺点云再生成网格。

> 中文综述参考：《面向深度学习的三维点云补全算法综述》《基于深度学习的三维形状补全研究综述》（按体素/点云/隐式分类，列举 60+ 文献）。

### 3. AIGC 生成时代与其修复子问题（2022–至今）

生成式 3D 爆发，但**生成的网格「看起来对、用起来错」**——缺乏功能结构。这催生了「AIGC 网格修复」这一专门子领域。

**A. 生成模型演进（修复需求的根源）**
- **2022 · DreamFusion**（Poole 等）：用 SDS（Score Distillation Sampling）把 2D 扩散先验蒸馏到 NeRF，实现无 3D 数据的文本→3D；但慢、糊、有「雅努斯问题」（多角度都像正面）。
- **2023 · MeshGPT**（Siddiqui 等，CVPR 2024）：把三角网格当作 token 序列，用 decoder-only Transformer **自回归生成紧凑、带锐边的艺术家级网格**（对比神经场等值面提取的稠密网格）。前驱含 PolyGen、BSP-Net、Mesh Diffusion（DMTet）。
- **2024 · LATO / MeshCarve**：新一代「工匠级（artisan）」网格生成。
- **2025–2026 · TRELLIS / Hunyuan3D / TRELLIS.2**：原生 3D 扩散（rectified flow + 两阶段「几何→纹理」），数十秒产出游戏级 PBR 资产；TRELLIS.2 达 4B 参数、1536³、4K 纹理。

**B. 为什么需要专门修复（生成网格的失败模式）**
据 Tripo AI / Triverse 等厂商归纳，AI 网格常见缺陷：**非流形几何、开放边/破洞、UV 重叠渗色、三角面数失控、不水密（不可打印）、隐藏面「幻影几何/融合」、缺失开口、错误镂空、雅努斯问题**。

**C. 生成后修复的代表工作**
- **InstructMesh（MIT + Google + Northeastern，2024/2025）**：*Selective Refinement of Generative 3D Models for Fabrication*。实证：**120 个 Thingiverse 模型经 TRELLIS 重建后，78.3% 含多种几何缺陷（平均 2.4 个）**——多余特征 65.8%、缺失开口 48.6%、错误镂空 36.9%。方法：用户圈选问题区 + 文本指令，GPT-4 在 TRELLIS 的中间体素表示上选择 6 种几何操作（拉伸/膨胀/填充/裁剪/腐蚀/压平）重建修正网格；零 CAD 经验的 12 名用户成功修复 89.7% 标注缺陷。
- **MeshRepair（prefix-LM，自回归拓扑修复）**：把修复视为**序列到序列的「网格编辑翻译」**——把草稿网格规范化为「一面一 token」的前缀，双向注意力读取后，先「删除头」指针网络删面、再「添加头」选顶点补面。在 MeshCarve / LATO2 草稿上，quad-equivalent F1 提升 +0.046，而传统 MeshFix/Blender 清理常使 F1 下降。
- **厂商工具链（2025–2026）**：Tripo AI「Smart Mesh」/ ZRemesher / RetopoFlow 重拓扑；MeshFixAI、PolyRepair Labs 用 CNN 在百万级模型上训练，预测缺失面、重定向法向、推断合理拓扑；2026 IEEE TVCG 基准称 AI 修复相较启发式工具将 Hausdorff 误差降低 84%、速度 3×。

### 修复主线小结（阶段演进）

```
传统几何修复(1990s–2010s: ADMesh/Magics/八叉树/MeshFix)
   → 数据驱动补全(2015–2022: 体素/点云/隐式 + 深度学习先验)
   → AIGC生成后修复(2022–至今: InstructMesh指令修复 / MeshRepair自回归拓扑修复 / 厂商重拓扑与AI修复引擎)
```

---

## 三、两条主线的交汇：拆割与修复在 AIGC 时代的融合

1. **修复常先分割，再局部操作**：InstructMesh 等需「圈选问题区域」，本质是交互式分割；MeshRepair 把面视为 token，删除/添加即在点集上做结构化分割编辑。
2. **分割为生成式编辑提供手柄（handle）**：TRELLIS 的局部编辑、SAMPart3D 的零样本部件分割，都让「按部件改、按语义修」成为可能，是 AIGC 修复的上游使能技术。
3. **统一趋势：从几何优化 → 序列/翻译任务**：MeshRepair 把修复当「编辑序列翻译」、MeshGPT 把生成当「token 序列生成」，与 NLP 大模型范式对齐；而拆割也因 SAM/CLIP/Transformer 从「几何聚类」转向「语义零样本」。
4. **评测范式进化**：拆割有 PSB（2009）；修复从「几何误差（Hausdorff）」走向「quad-equivalent F1」「是否保留艺术家意图」等语义级指标。

---

## 四、关键来源清单（便于追溯）

**拆割 / 分割**
- 三维网格模型的分割及应用技术综述，《计算机辅助设计与图形学学报》（jcad.cn）
- Chen, Golovinskiy, Funkhouser, *A Benchmark for 3D Mesh Segmentation*, SIGGRAPH 2009（Princeton PSB, segeval.cs.princeton.edu）
- *A comprehensive overview of methodologies and performance evaluation frameworks in 3D mesh segmentation*, ScienceDirect 2015
- MTSegNet: Manifold Transformer for 3D Shape Segmentation（PDF）
- *Generative 3D appearance design: A survey of generation, segmentation and editing by AI*, Oxford JCDE 2024
- Human-in-the-Loop Atlas-Based 3D Asset Segmentation, arXiv 2606.17824 (2026)
- Katz 2005 (Core Extraction), Shapira 2008 (SDF), Lai 2008 (Random Walks), Lien & Amato 2007 (近似凸分解), Hoffman & Richards 1984 (Minima Rule)

**网格修复**
- Design Software History: From Triangle Soup to Printable Solid（NOVEDGE，传统修复史）
- Model repair / Mesh repair with topology control（Attene, Campen, Kobbelt 等综述）
- Barequet & Sharir 1995；Liepa 2003；Ju 2004；Bischoff 2005；Wojtan 2009；Campen & Kobbelt 2010；MeshFix (Attene 2010/2014)；Zhou et al. 2016
- 点云/形状补全综述：《面向深度学习的三维点云补全算法综述》《基于深度学习的三维形状补全研究综述》；PCN(2018)、AtlasNet(2018)、TopNet(2019)、PoinTr(2021)、DeepSDF/Occupancy(2019)
- AIGC 生成：DreamFusion(2022)、MeshGPT(CVPR 2024)、TRELLIS(2024/2025)、Hunyuan3D(2025)
- AIGC 修复：InstructMesh（MIT/Google/Northeastern, Fabbaloo 报道）、MeshRepair（prefix-LM, Lacuna）、Tripo AI / Triverse 厂商博客、2026 IEEE TVCG AI 修复基准

---

## 五、附录：思想起源深读（拆割问题的「为什么」）

第一节只列了年份与方法，这里补上「拆割」这一研究领域**为何会诞生、其目标为何是『有意义的分割』**的认知科学根脉。它决定了此后四十年几乎所有方法的设计动机。

### 1. 核心公理：Transversality（横向相交原理）→ 最小规则（Minima Rule）

**Hoffman & Richards (1984)** 把问题建立在一个自然规律之上：

> **当两个实心物体相交/结合时，其切平面会在交界处形成一块「凹向不连续（concave discontinuity）」区域。**

由此推出一个可操作的分割公理——**最小规则（Minima Rule）**：

> *Divide a surface into parts at loci of **negative minima of each principal curvature** along its associated family of lines of curvature.*（在每条曲率线上的「主曲率负极小值」处把表面切成部件。）

直觉上：两个物体拼在一起、或一个新部件从母体「长出来」（Leyton, 1989 的生长模型），**必然会**在连接处产生负曲率极小值（凹谷）。所以——给定一个已结合好的复合形状，**负曲率极小值天然就是「该从哪里下刀拆开」的候选点**。

这正是「拆割问题」最原初的定义来源：**分割 = 在人类感知认为「这里是两个部件交界」的地方切**。

### 2. 平行的另一条根脉：Biederman 的「部件识别理论（RBC）」(1987)

认知心理学家 **Biederman (1987)** 提出 Recognition-by-Components，与 Minima Rule 互为表里：

- 人识别物体，是把它解析为少量**视点不变（viewpoint-invariant）的体积基元「geons」**（广义锥：圆柱、砖块、楔形、棱锥等，< 36 种）的组合；
- 而**凹度（concavity）是「一个 geon 结束、另一个开始」的感知线索**——边缘（edges）负责视点不变性，凹处（concavities）负责部件分离；
- 实验证据（Biederman 线描实验）：擦掉轮廓的**凸部**不影响识别，但擦掉**凹处/连接点**识别暴跌 → 印证「部件边界就住在凹谷」。
- 这一理论直接赋予了「拆割」一个目标函数：**分割结果应当是「对识别/功能有意义」的部件**，而不是任意几何碎块。

> 注：RBC 的「geons」后来在计算上被 echo 为**基元分解**一脉——Schnabel 等 (2007) 的 RANSAC 基元检测、BSP-Net (2020)、SuperDec (2022) 把点云分解为超二次曲面，本质都是「用少量体积基元拼出物体」这一思想的工程化。

### 3. 实证桥接：为什么算法要对准「凹向负极小值」

心理学实验为「以凹谷为界」提供了硬性证据，这正是后来所有几何分割方法（无论是否自觉）努力逼近的目标：

- **Braunstein et al. (1989)**：记忆任务中 77% 的人记住的是「极小值部件」；被要求画部件边界时 **81% 画在局部极小值处**，仅 10% 画在极大值处。
- **Hulleman et al. (2000)**：搜索不对称——人能在一堆凸干扰中秒找凹处（pop-out），反过来不行；人类与黑猩猩都对凹度比凸度敏感得多。
- **Chen et al. (2009)（即 Princeton 分割基准 PSB）**：大规模让人类给熟悉物体画部件边界，**绝大多数边界落在主曲率的负极小值处**。→ 这把「人类怎么拆物体」量化成了可评测的 ground truth，反过来规定了算法好坏的标准。

→ 这条证据链把「思想起源」和「2009 年的 PSB 基准」直接打通：基准不是凭空定的，它只是把 1984/1987 的感知理论做成了可测量版本。

### 4. 两条「早期背景」技术线（综述原文提到的源头）

JCAD 综述指出，网格分割还有两个更早的工程动机：

- **计算几何的凸分解**：把非凸多面体拆成凸块以提升绘制/渲染效率；但实践中人们更愿「切网格边界」而非切多面体本身。
- **计算机视觉的深度图分割**：深度图拓扑简单（行列结构），其算法被推广到任意拓扑的 3D 网格。

### 5. 思想起源如何「长」出整个领域（一图串联）

```
1984 Minima Rule（认知公理：负曲率极小值=部件交界）
   └─ 目标被定为「有意义的分割」(meaningful segmentation)
        ├─ 几何方法(1990s–2000s) 都在逼近这一几何线索：
        │    Falcidieno 1992(曲率面片) / Katz 2005(核提取) /
        │    SDF 2008(体积直径) / Lavoué 2008(MRF)
        └─ 部件显著性因子(Hoffman & Singh 1997：部件相对尺寸/凸起度/边界强度)
             └─ 被学习化 → Kalogerakis CRF 2010 的特征/能量设计

1987 RBC(geons)（认知公理：物体=少量体积基元的组合）
   └─ 基元分解一脉：RANSAC基元(2007)/BSP-Net(2020)/SuperDec(2022)

1989–2009 心理物理实证（Braunstein/Hulleman/Chen-PSB）
   └─ 把「人怎么拆」量化 → 2009 PSB 基准 → 可评测、可学习
```

**一句话总结思想起源**：3D 模型拆割不是一个「纯几何」问题，它的第一性原理来自认知心理学——**人之所以能把复杂物体看成若干部件的组合，是因为自然界中『结合/生长』必然在交界处留下凹向负曲率极小值**；这一感知规律先后被形式化为 Minima Rule 与 RBC/geons，并被心理物理实验验证、被 PSB 基准量化，最终成为驱动四十年几何与学习分割方法的「北极星」。

---

*注：本综述基于公开网络检索整理，部分 2025–2026 资料来自厂商博客与预印本，正式引用前建议核对原始论文。如需进一步深挖某一阶段（如「近似凸分解的算法细节」「MeshRepair 的训练数据构造」或「PSB 评测指标」），可继续定向检索。*
