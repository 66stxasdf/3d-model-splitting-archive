# 论文爬取 · 分类索引

> 课题：AIGC 图生 3D 手办语义自动分件（可打印拆分）
> 整理时间：2026-10-30
> 说明：本目录按「研究环节」分类存放检索/下载到的 3D 分割与可打印分件论文 PDF，配套文献摘录见 `00-文献摘录/`。

---

## 目录结构

```
论文爬取/
├── 00-文献摘录/           # 两批论文正文摘录（摘要+方法+结果+课题映射）
├── 01-可打印分件与DFM/    # 可打印拆分、禁切区、装配连接件
├── 02-语义部件分割/       # 部件级语义分割（学习式/零样本/纹理路线）
├── 03-3D生成与分割抬升/   # 3D 生成、2D→3D 分割抬升
└── 04-AIGC网格修复/      # AIGC 网格修复/重拓扑（国际文献清单，PDF 待下载）
```

---

## 01-可打印分件与 DFM

| 文件 | 论文 | 年份/出处 | 与本课题的关系 |
|---|---|---|---|
| `Chopper-TOG2012.pdf` | Chopper: Partitioning Models into 3D-Printable Parts | ACM TOG (SIGGRAPH Asia) 2012，被引 ~421 | 可打印分件**基石**；4 准则（可装配/少件/缝不显眼/结构稳）+ BSP 树束搜索。本仓库 `segment.py`/`planner.py` 代价函数的对照基线 |
| `InteractivePartitioning-IEEE2018.pdf` | Interactive Partitioning of 3D Models into Printable Parts | IEEE CG&A 2018, 38(4):38-53 | **最关键**；no-go region 语义禁切（FEM 应力 + 涂抹硬约束）、切平面指定、部件合并。`semantic.py` 的 no-go 硬禁切原型 |

## 02-语义部件分割

| 文件 | 论文 | 年份/出处 | 与本课题的关系 |
|---|---|---|---|
| `PartField-ICCV2025.pdf` | PartField: Learning 3D Feature Fields for Part Segmentation | ICCV 2025 (NVIDIA) | 前馈 ~10s、mIoU 79.18%、泛化到 AIGC 网格 → `semantic.py` 语义特征场**首选** |
| `SegmentAnyMesh-arXiv2408.pdf` | Segment Any Mesh: Zero-Shot Mesh Part Segmentation | arXiv 2408.13679 | 法线/SDF 多模态渲染 + SAM 抬升，零样本；**已开源**。无纹理 AIGC 网格分件 backbone |
| `MeshSeg-SIBGRAPI2025.pdf` | MeshSeg: 3D Mesh Segmentation through Color Clustering | SIBGRAPI 2025 | UV 纹理图集 + 颜色聚类 + SAM 两路；带纹理手办零样本分件**直接基线** |

## 03-3D 生成与分割抬升

| 文件 | 论文 | 年份/出处 | 与本课题的关系 |
|---|---|---|---|
| `SA3D-NeurIPS2023.pdf` | Segment Anything in 3D with NeRFs | NeurIPS 2023，被引 ~315 | 用 NeRF 把 2D SAM 抬升到 3D，单视角 prompt 出 3D 分割；输入含多视角/NeRF 时可接入 |
| `PartCrafter-NeurIPS2025.pdf` | PartCrafter: Structured 3D Mesh Generation via Compositional Latent Diffusion | NeurIPS 2025 | 单图→≤16 个对齐部件网格 ~34s；最接近端到端「图生 3D 可拆件」管线 |

## 04-AIGC 网格修复（网格修复 / 重拓扑，国际文献）

> 课题延伸：AIGC 生成网格后的**修复 / 补洞 / 重拓扑**（与 03-3D 生成互补，侧重"生成后修"）。
> 筛选口径：仅国际论文（SIGGRAPH / CVPR / ICCV / NeurIPS / IEEE TVCG / Elsevier / arXiv），不含国内中文期刊与 CNKI。
> 状态：**清单已整理，PDF 尚未下载**。按 DOI/标题经科研通「一搜即达」→ SciHub / 微软学术获取，避开内置 CNKI。
> 正文见本目录：`01_网格修复专攻.md`、`02_生成式网格生成与精修.md`、`README.md`（含科研通下载方法）。

| # | 论文 | 年份/出处 | DOI / arXiv | 相关性 |
|---|---|---|---|---|
| 1 | SpaceMesh: A Continuous Representation for Learning Manifold Surface Meshes | SIGGRAPH Asia 2024 | 10.1145/3680528.3687634 / 2409.20562 | ★★★ 网格修复直接应用（重三角化缺陷区） |
| 2 | Visual-preserving mesh repair | IEEE TVCG 2024, 30(9):6586–6597 | 按标题检索 | ★★★ 缺陷网格→水密流形 |
| 3 | Learning Self-Prior for Mesh Inpainting … (SeMIGCN) | IEEE TVCG 2024 | 10.1109/TVCG.2024.3364365 | ★★★ 自监督网格补洞 |
| 4 | Neural inpainting of folded fabrics with interactive editing | Computers & Graphics 2024 | 按标题检索 | ★☆ 布料孔洞修补+交互 |
| 5 | Deep learning of curvature features for shape completion | Computers & Graphics 2023 | 按标题检索 | ★☆ 曲率图像补洞 |
| 6 | MeshGPT: Generating Triangle Meshes with Decoder-Only Transformers | NeurIPS 2023 | 按标题检索 | ★★★ 自回归生成+局部补全 |
| 7 | CraftsMan3D: … Interactive Geometry Refiner | CVPR 2025 | 2405.14979 | ★★★ 几何精修器 |
| 8 | Mesh-RFT: Enhancing Mesh Generation via Fine-Grained RL Fine-Tuning | NeurIPS 2025 | — | ★★★ 局部缺陷 RL 修复 |
| 9 | MagicClay: Sculpting Meshes With Generative Neural Fields | SIGGRAPH Asia 2024 | 10.1145/3680528.3687627 | ★★★ 局部生成式修补 |
| 10 | MeshAnything: Artist-Created Mesh Generation with Autoregressive Transformers | arXiv 2024 | 2406.10163 | ★☆ 生成后拓扑修复 |
| 11 | DeepMesh: Auto-Regressive Artist-mesh Creation with RL | ICCV 2025, 10612–10623 | 2503.15265 | ★☆ 生成完整性/拓扑 |
| 12 | TRELLIS: Structured 3D Latents for Scalable and Versatile 3D Generation | CVPR 2025 | — | ★☆ 对照基线 |

---

## 00-文献摘录

| 文件 | 内容 |
|---|---|
| `重要论文内容摘录.md` | 第一批：PartField / SA3D / PartCrafter + Interactive Partitioning 摘要 + MeshSeg·Chopper 要点 + DFM·Hi3D |
| `重要论文内容摘录-续.md` | 第二批：MeshSeg / Chopper / Interactive Partitioning / Segment Any Mesh 的摘要+方法+结果正文 |

---

## 状态 / 说明
- **全部 7 篇 PDF 已下载完成并通过校验**（`%PDF` 头 + `%%EOF` 尾；MeshSeg/Chopper 曾因境外源站限速改用 MIT/UW 镜像补齐）。
- 直链备份（源站限速时可换这些镜像）：
  - MeshSeg: `http://sibgrapi.sid.inpe.br/col/sid.inpe.br/sibgrapi/2025/09.13.03.27/doc/Artigo_Meshseg_INPE.pdf`
  - Chopper: `https://cdfg.mit.edu/pdf/journal/2012-luo-chopper-partitioning-models-into-3d-printable-parts.pdf`（MIT 镜像，较快）
  - Interactive Partitioning: `https://yongjinliu.github.io/files/2018-Interactive-Partitioning-of-3D-Models-into-Printable-Parts.pdf`
- 原始下载目录（含未分类副本）：`C:\Users\侴刍\ego\ablesci-3dseg-20261030\papers`
