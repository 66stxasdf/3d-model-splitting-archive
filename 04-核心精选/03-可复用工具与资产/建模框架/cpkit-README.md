# ColorPartsKit —— 彩色 3D 模型拆解框架

从零参数化建模的**彩色**模型，为「3D 彩色模型拆解」项目做基准件。
颜色不是贴图，是**零件属性**——这是与图生 3D 网格路线的本质区别。

## 建模要点（本项目的设计约束）

1. **颜色即材料即实体**：一个颜色 = 一个 Solid = 一件打印件。颜色写在
   manifest 与 STEP(XCAF) 里，拆分零误差——不靠图像分类、不受 AO 阴影干扰。
2. **颜色边界必须是零件边界**：跨色处用槽/嵌件/分型面表达，禁止颜色横穿
   大曲面（那是贴图思维，拆不开）。
3. **层级命名**：`模型 / 部件组 / 零件`，id 稳定（BOLT01-HEAD），
   manifest 记录 group/joint/print_note，拆解树可机读。
4. **免胶配合**：竖直销 + 45° 锥顶孔（锥顶自支撑 → 两件都能平底打印）；
   横向装饰件压配（单边 0.1）。销在下件朝上、孔在上件底部朝下开口。
5. **打印可行内建**：单边间隙 0.2、壁厚≥2.8、最小特征≥3、每件有平底、
   无需支撑的姿态已写进 print_note。
6. **验证信号（build.py 自动跑）**：单实体 / 有效 / 正体积 / 两两布尔干涉=0 /
   装配包围盒 / STEP 回读体积对账（本件 11 件全部 0.00% 差）/ 彩色 STEP
   调色板命中率。
7. **三路同源导出**：STEP（可编辑）+ STL（打印）+ GLB（Y-up 彩色预览），
   全部由同一参数表生成，改参数不破结构。
8. **彩色 STEP（XCAF）**：AP242 + 每件 SetColor(ColorGen)，FreeCAD/
   SolidWorks/在线查看器打开即彩色。三个实测坑见 cpkit.py 注释。
9. **参数表驱动**：所有尺寸在 `models/bolt01.py` 的 P dict，改一处全联动。
10. **框架与模型分离**：`cpkit.py`（框架）不认识任何具体形状，
    新模型只需写一个 `models/xxx.py` 提供零件树。

## 首件 BOLT-01（彩色拆解基准件）

- 120mm 高，11 件 7 色，实心体积 103 cm³，装配包围盒 73×31.5×120mm
- 拆解树：头（面罩→双眼、天线）/ 躯干（胸核）/ 双臂 / 双腿
- 构建：`python colorparts/build.py bolt01`（用 D:/Mangshe/python.exe）

## 目录

```
colorparts/
├── cpkit.py             框架：Part/Model/验证/三路导出（含彩色 STEP + 回读校验）
├── build.py             构建+验证+导出入口（BOLT-01）
├── build2.py            构建+NDBG 可拆性+多路导出（MK-02）
├── separability.py      可拆性验证器：NDBG 判据（几何精确版）
├── threemf.py           3MF 导出器：装配态 + 排盘态（ISO/IEC 25422）
├── verify_step_color.py 彩色 STEP 回读校验（命令行工具）
├── models/
│   ├── bolt01.py        首件基准（11 件 7 色）
│   └── mk02.py          MK-02 哨兵（37 件 8 色，五种分色手法）
└── out/MK02/            stl/×37 + step/×37 + 彩色 STEP + GLB + 3MF×2 + manifest.json
```

## MK-02 哨兵（复杂多颜色可差分模型）

- 201.65mm 高，**37 件 8 色**，五种分色手法全部用上：
  - **A 竖直分件链**：BASE→FOOT→ANKLE→SHIN→KNEE→THIGH→HIP→WAIST→TORSO
  - **B 壳层嵌套**：PAULDRON 罩住 UPPERARM 上端（底部开口）
  - **C 表面嵌件**：VISOR / EYE_L/R 嵌进头盔前脸凹槽
  - **D 同心套筒**：CHEST_RING→CHEST_MID→CHEST_CORE 三层，强制由内向外拆
  - **E 左右拼合**：HELM_L / HELM_R 沿 X 合拢，中央留 0.15mm 缝
- **可拆性已用 NDBG 判据验证**：37 件 / 9 批完全拆解，0 件锁死，
  26 件在装配态下 6 方向全被挡（互锁深度）。
- 验证：单一有效实体 ×37 / 零干涉 / 彩色 STEP 回读 37/37 带色、8 色命中
- 构建：`D:/Mangshe/python.exe colorparts/build2.py mk02`

## 可拆性判据（NDBG）

颜色可差分的本质是**颜色分区 = 可独立拆出的实体零件**，且可拆性可被机器验证：

- 固定方向 d，对每对零件 (i,j) 做平移扫掠布尔求交（`blockers_exact`），
  得阻挡表：i 沿 d 移动会撞上 j。
- 平移距离必须**多级**（2→330mm 几何级数 14 档）：单级 2mm 会漏掉
  「滑 2mm 没事、滑 6mm 撞墙」的深嵌合。
- 出度为 0 的零件可沿 d 抽出；移除后阻挡只减不增（单调性），逐批拆完。
- 全方向出度 > 0 的零件 = 结构锁死 = 设计不合格，建模阶段即报错。

## 踩坑实录（2026-10-28，OCP 7.9 / build123d 0.13）

1. **`Compound(children=[...])` 会消耗子 shape**：构造总装后再导出单件 STEP
   直接 "Failed to write STEP file"。总装 Compound 必须最后建。
2. **XCAF 彩色 STEP 五个坑**：
   - 只 `AddShape` 不建装配结构 → 颜色大量丢失（11 件只落 2 个）；
   - 用 `NewShape + AddComponent` 建装配 → 几何全丢（文件只剩 1.6KB 结构）；
     正解是 free-shape 逐件注册（每件 AddShape + SetColor(ColorGen) + UpdateAssemblies）；
   - **必须注册单 Solid**（`solid.solids()[0]`），注册 Compound 时颜色丢；
   - **AddShape 有时返回「引用标签」**（37 件出现 3 次：CHEST_CORE/EYE_L/EYE_R），
     挂在引用标签上的颜色和名字会被写出器忽略 → 那几件成无名灰模。
     必须检查 `IsSimpleShape_s`，不简单就用 `GetReferredShape_s` 找到实体标签
     **两处都写**（见 cpkit.write_step_colored 的 targets 逻辑）；
   - 颜色精确匹配红/绿/蓝等预定义名时 OCC 退化成 DRAUGHTING_PRE_DEFINED_COLOUR，
     自定义配色写真实 COLOUR_RGB；
   - **OCC 内部存线性 RGB**：`Quantity_Color.Red()` 读回是线性值，直接 ×255 全部
     偏暗（#E4572E 读成 #C61807）。回读必须做线性→sRGB 反算（`_linear_to_hex`）；
   - **自检不能数字符串**：`STYLED_ITEM`/`COLOUR_RGB` 出现次数与带色零件数不一一
     对应（曾据此误报 20/37）。唯一可信判据是 XCAF 回读逐标签查色
     （`cpkit.read_step_colors` / `verify_step_color.py`）。
3. **`Shape.is_valid` 是属性不是方法**（0.13 变更）。
4. **OCP 的 STEP 写入器不认非 ASCII 路径**：文件名用 ASCII，中文名进 manifest。
5. **GLB 是 Y-up**：CAD 的 Z-up 几何直接进 GLB 会躺倒，导出时 (x,y,z)→(x,z,-y)。
6. trimesh Scene 的 geometry 键会被重命名成 geometry_N，零件 id 在
   **graph 节点名**上；matplotlib 检查渲染要按节点取几何，且 `shade=False`
   否则 PBR 色被光照洗灰。
