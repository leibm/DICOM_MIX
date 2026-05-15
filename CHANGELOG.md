# DICOM MIX Tools 更新日志

## v4.4.0 (2026-05-15)

### 问题修复
- **修复 C-FIND 查询患者姓名不显示**：C-FIND 返回键缺少 PatientName / PatientID / AccessionNumber 字段，导致 PACS 不返回这些信息
- **修复 pynetdicom 传输语法协商异常**：`transfer_syntax` 属性为列表类型时直接调用 `.name` 导致 `'list' object has no attribute 'name'` 错误
- **修复 DSA 查询弹窗无法打开**：缺少 `QGridLayout` 导入导致 DsaQueryDialog 初始化崩溃

### 改进优化
- **CBCT Y 轴变化数据自动检测**：归一化时自动检测切片位置变化最大的轴（Y 轴），不再硬编码假设为 Z 轴
- **归一化输出兼容 3D 重建**：
  - Modality 设为 CT（原为 XA），兼容 RadiAnt / 3D Slicer
  - 添加 FrameOfReferenceUID，确保 3D Slicer 可正确导入序列
  - 添加 InstanceNumber，确保 RadiAnt 可正确排序
  - RescaleIntercept 改为 0（原为 -1024），适配 8-bit XA 像素数据
  - 自动推断 ImageOrientationPatient（从逐帧位置差分计算法向量）
- **归一化进度条实时反馈**：归一化弹窗进度条实时显示处理百分比
- **修复单帧输出私有标签残留**：改用 ds.copy() + 注入 PixelData 的方式，避免 full_ds 残留私有标签

## v4.3.0 (2026-05-14)

### 新增功能
- **多厂商 DICOM 协议自适应**：全面适配西门子、GE、飞利浦等不同厂商设备的 C-FIND/C-MOVE 协议差异
- **单文件多帧 CBCT 归一化**：支持将包含数百帧切片的单文件多帧 DICOM（CBCT 断层重建数据）自动展开为独立单帧输出
- **Worklist 查询功能**：支持 Modality Worklist C-FIND 查询，可查看预约检查安排（Scheduled Procedure Step）

### 改进优化
- **C-FIND 查询兼容性**：
  - 空值查询字段改为省略标签（而非发送 `*` 通配符），解决 GE 设备返回 0xA700 错误的问题
  - 同时协商 Implicit VR Little Endian 和 Explicit VR Little Endian，兼容飞利浦旧设备
  - 新增 PatientStudyOnly 查询模型作为 Study Root / Patient Root 之后的第三回退策略
  - 扩展返回键（ModalitiesInStudy、NumberOfStudyRelatedSeries 等）提升设备兼容性
- **C-MOVE 拉取兼容性**：
  - 添加 12 种常用 Storage SOP Class 协商，解决远端设备因无法确认接收能力而返回 0xC003 的问题
  - 自动从 C-FIND 查询结果缓存 PatientID，在 C-MOVE 请求数据集中附加 PatientID（GE 设备要求）
  - 支持 Study Root / Patient Root / PatientStudyOnly 三种 Move 模型自动回退
- **DSA 查询弹窗 UI 整理**：使用 QGroupBox + QGridLayout 重构查询条件布局，界面更清晰
- **安装程序语言**：安装包支持英文和简体中文（替换原有的日语）

### 问题修复
- 修复 DSA 工作站 C-FIND "No presentation context for Patient Root" 错误
- 修复单文件多帧 DICOM 归一化时"有效文件不足"的错误
- 修复 PACS/DSA 查询对话框中患者信息字段被错误设为必填的问题，现为可选条件
- 修复 DSA 设备查询时 Patient Root 和 Study Root 模型选择逻辑

## v4.2.0 (2026-05-13)

### 新增功能
- **懒加载大体积断层数据**：后台线程加载，避免 UI 卡顿
- **DSA 兼容性修复**：支持 Study Root / Patient Root 自动回退的 C-FIND 查询策略
- **查询条件可选**：PACS/DSA 查询时患者信息字段改为可选输入

### 问题修复
- 修复 C-FIND/C-MOVE 与 DSA 设备的协议兼容性问题
- 修复 plugin_manager 安装插件时自动安装缺失依赖的逻辑

## v4.1.0 (2026-05-12)

### 新增功能
- 后台线程加载 DICOM 文件，避免大体积数据导入时界面卡顿
- DSA 设备双向连接：支持 C-FIND 查询和 C-MOVE 主动拉取图像

## v4.0.0 (2026-05-10)

### 初始版本
- SCP 接收、本地导入、主机查询
- DSA/CT/MR 多帧图像查看器（实时减影、窗宽窗位调节）
- 图像序列导出为 MP4 / PNG
- 异构断层数据归一化（兼容 GE Advantage Workstation）
- 插件系统：3D 渲染（体渲染/MPR）
