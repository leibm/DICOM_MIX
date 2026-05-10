# DICOM MIX Tools v3.0

**DSA 图像路由与编辑工具**

用于 GE DSA 等血管造影设备的 DICOM 图像接收、查看、编辑和转发。支持 SCP 被动接收、本地文件导入、主机查询检索，以及从 DSA 工作站主动拉取图像。

## 功能特性

- **图像接收**：内置 DICOM SCP 服务端，可接收 DSA 设备推送的图像
- **本地导入**：批量导入本地 DICOM 文件夹
- **DSA 查看器**：多帧图像浏览、实时减影、窗宽窗位调节、循环播放
- **图像序列导出**：支持将当前序列导出为 MP4 视频或 PNG 图片序列
- **患者信息编辑**：修改患者姓名、ID、性别、年龄、出生日期、检查号等
- **UID 重置**：自动重新生成 Study/Series/SOP Instance UID
- **主机查询与发送**：C-FIND 查询、C-STORE 发送
- **主机 C-MOVE 拉取**：从主机服务器主动拉取图像到本地
- **DSA 双向连接**：C-FIND 查询 DSA 工作站、C-MOVE 主动拉取图像
- **序列快速切换**：查看器内一键切换前后序列，自动管理勾选状态
- **私有标签保护**：保留 GE DSA 私有标签（0x0019、0x0043 组等）

## 系统要求

- Windows 10/11（64 位）
- 无需安装 Python（打包为独立可执行文件）

## 快速开始

### 方式一：使用打包好的 EXE

1. 解压 `DICOM_MIX_Tools.zip`
2. 双击 `DICOM_MIX_Tools.exe` 启动

### 方式二：从源码运行

```bash
pip install PySide6 pydicom pynetdicom numpy
python main.py
```

## 界面布局

```
┌─────────────────────────────────────────────────────────────────┐
│  工具栏：[SCP 启停] [载入本地] [刷新] [清空] [进度条]            │
├──────────┬──────────────────────────────┬───────────────────────┤
│ 源数据   │     DSA 图像查看器            │ 目标患者信息          │
│          │                              │ ┌─ 主机查询 ───────┐ │
│ 检查     │  ┌────────────────────────┐  │ │ 姓名/ID/检查号     │ │
│  └ 序列  │  │                        │  │ └───────────────────┘ │
│    └ 影像│  │      图像显示区         │  │ ┌─ 手动输入 ───────┐ │
│          │  │                        │  │ │ *患者姓名         │ │
│          │  │  [▶] ═══════════ 1/30  │  │ │ *患者ID           │ │
│          │  └────────────────────────┘  │ │ *检查号           │ │
│          │                              │ │  性别/年龄/出生日期│ │
│          │  右侧控制面板：减影/窗宽窗位  │ │  住院号/影像号     │ │
│          │                              │ └───────────────────┘ │
│          │                              │ ┌─ 网络配置 ───────┐ │
│          │                              │ │ 本机IP / SCP      │ │
│          │                              │ │ 主机 / DSA 配置   │ │
│          │                              │ └───────────────────┘ │
├──────────┴──────────────────────────────┴───────────────────────┤
│  [应用拆分并发送到主机]  [应用拆分并导出到本地]  目标患者: xxx   │
├─────────────────────────────────────────────────────────────────┤
│  状态栏                                                          │
└─────────────────────────────────────────────────────────────────┘
```

## 详细使用说明

### 1. 接收 DSA 图像（SCP 模式）

1. 切换到 **网络配置** Tab，查看本机 IP 地址
2. 确认 SCP AE Title 和端口（默认 `MIX_SCP`，端口 `11112`）
3. 在 DSA 设备或工作站上配置 DICOM 发送目标：
   - **AE Title**: `MIX_SCP`
   - **IP 地址**: 本机 IP（在网络配置 Tab 中查看）
   - **端口**: `11112`
4. 点击工具栏的 **启动 SCP** 按钮
5. 从 DSA 设备推送图像，图像将自动保存到 `temp_dicom/` 目录
6. 点击 **刷新** 按钮，左侧树形控件将显示已接收的检查

> **提示**：SCP 状态会实时显示在底部状态栏。接收过程中请勿关闭程序。

### 2. 从主机（PACS）查询与拉取

1. 切换到 **网络配置** Tab
2. 在 **主机服务器** 区域填写：
   - 主机 AE Title（如 `PACS`）
   - 主机 IP 地址（如 `192.168.1.10`）
   - 主机端口（默认 `11112`）
3. 点击 **保存配置**
4. 切换到 **修改病人信息** Tab，点击 **查询主机**
5. 在弹窗中输入患者姓名、ID 或检查号，点击 **查询**
6. 从结果列表中选择目标检查，点击 **拉取数据**
7. 程序通过 C-MOVE 从主机拉取图像到本机 SCP
8. 拉取完成后，关闭弹窗并点击 **刷新** 查看已接收的检查

> **注意**：拉取前请确保 SCP 已启动，否则主机无法将图像推送到本机。

### 3. 从 DSA 工作站查询与拉取

1. 切换到 **网络配置** Tab
2. 在 **DSA 主机/工作站** 区域填写：
   - DSA AE Title（如 `AW_SERVER`）
   - DSA 主机 IP（如 `192.168.1.100`）
   - DSA 端口（默认 `11112`）
3. 点击 **保存配置**
4. 点击 **查询 DSA** 按钮，弹窗将显示 DSA 工作站上的检查列表
5. 选择目标检查，点击 **从 DSA 拉取**
6. DSA 工作站将图像推送到本机 SCP，弹窗底部显示实时进度
7. 拉取完成后点击 **刷新** 查看

> **多 DSA 节点**：支持配置多个 DSA 工作站，在弹窗中切换节点进行查询。

### 4. 本地文件导入

1. 点击工具栏的 **载入本地** 按钮
2. 选择包含 DICOM 文件的文件夹
3. 程序自动扫描、验证并复制有效 DICOM 文件到工作目录
4. 完成后自动刷新左侧树形控件

### 5. 图像查看与操作

- **左侧树形控件**显示三级结构：检查 → 序列 → 影像
  - 点击 **检查节点**：加载该检查下所有序列的全部帧
  - 点击 **序列节点**：加载该序列所有帧
  - 点击 **影像节点**：只加载单张图像
- **鼠标滚轮**：切换帧（多帧序列）
- **鼠标中键拖拽**：调节窗宽窗位（水平=窗宽，垂直=窗位）
- **右键菜单**：
  - 重置窗宽窗位
  - 设置减影蒙版帧
  - 启用/禁用减影
- **播放控制栏**（图像下方）：
  - **播放/暂停**：自动循环播放当前序列
  - **上一帧 / 下一帧**：逐帧浏览
  - **上一序列 / 下一序列**：快速切换同一检查下的前后序列
    - 切换到下一序列时，新序列自动勾选
    - 切换到上一序列时，当前序列自动取消勾选
  - **导出**：将当前序列导出为 MP4 视频或 PNG 图片序列
- **帧率调节**：右侧面板可调整播放帧率（FPS）

### 6. DSA 减影

1. 加载多帧 DSA 序列
2. 右键图像 → **设置减影蒙版帧**（通常选第 1 帧，即造影剂注入前的纯蒙片）
3. 右键 → **启用减影**
4. 图像将显示当前帧与蒙版帧的差值（血管显影）
5. 右侧面板可调节减影增益（Subtraction Gain）
6. 再次右键 → **禁用减影** 可恢复原始图像

### 7. 图像序列导出

1. 在查看器中加载目标序列
2. 点击播放控制栏的 **导出** 按钮
3. 在弹窗中选择导出格式：
   - **MP4**：设置输出帧率（默认与当前播放帧率一致），选择保存路径
   - **PNG 序列**：设置输出文件夹和文件名前缀，每帧保存为独立 PNG 文件
4. 点击 **确定**，等待导出完成
5. 导出过程中显示进度条，支持取消

> **注意**：导出图像会应用当前的窗宽窗位和减影设置。

### 8. 患者信息编辑与发送

#### 主机查询模式
1. 切换到 **修改病人信息** Tab
2. 输入患者姓名、ID 或检查号，点击 **查询**
3. 从结果列表中选择目标患者记录

#### 手动输入模式
1. 切换到 **手动输入** Tab
2. 填写必填项：患者姓名、患者 ID、检查号
3. 可选填写：性别（M/F/O）、年龄（如 45Y）、出生日期（YYYYMMDD）、住院号、影像号

#### 执行发送或导出
1. 在左侧树中勾选要处理的序列（勾选框支持多选，勾选父节点会自动勾选所有子序列）
2. 点击 **应用拆分并发送到主机**：覆写患者信息 + 重新生成 UID → C-STORE 发送到主机
3. 点击 **应用拆分并导出到本地**：覆写患者信息 + 重新生成 UID → 保存到指定文件夹

> **拆分机制**：每个选中的序列会生成全新的 StudyInstanceUID、SeriesInstanceUID 和 SOPInstanceUID，确保与原始数据完全隔离。

## 网络配置说明

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| 主机 AE Title | `PACS` | 主机服务器的 AE Title |
| 主机地址 | `127.0.0.1` | 主机服务器 IP 地址 |
| 主机端口 | `11112` | 主机服务器端口 |
| SCU AE Title | `MIX_SCU` | 本机作为 SCU 时的标识 |
| SCP AE Title | `MIX_SCP` | 本机作为 SCP 时的标识 |
| SCP 端口 | `11112` | 本机 SCP 监听端口 |
| DSA AE Title | `DSA` | DSA 工作站的 AE Title |
| DSA 主机 | `192.168.1.100` | DSA 工作站 IP |
| DSA 端口 | `11112` | DSA 工作站端口 |

## 目录结构

```
DICOM MIX Tools/
├── main.py              # 程序入口
├── ui_main.py           # 主窗口 UI
├── dicom_viewer.py      # DSA 图像查看器
├── dicom_input.py       # 数据输入（SCP + 本地载入）
├── dicom_network.py     # 网络通信（C-FIND / C-STORE / C-MOVE）
├── dicom_processor.py   # 数据处理（信息覆写 + UID 重置）
├── config.py            # 全局配置常量
├── build_exe.py         # PyInstaller 打包脚本
├── requirements.txt     # Python 依赖
└── temp_dicom/          # 临时工作目录（自动创建）
```

## 技术栈

- **PySide6** — Qt6 Python 绑定，GUI 框架
- **pydicom** — DICOM 文件读写
- **pynetdicom** — DICOM 网络通信（C-FIND / C-STORE / C-MOVE）
- **numpy** — 图像像素处理、减影运算
- **PyInstaller** — Windows EXE 打包

## 打包构建

```bash
python build_exe.py
```

输出位于 `dist/DICOM_MIX_Tools/`，压缩包为 `dist/DICOM_MIX_Tools_V3.0.zip`。

## 软件信息

- **软件名称**：DICOM MIX Tools
- **版本**：v3.0
- **开发日期**：2026-05-10
- **适用平台**：Windows 10/11（64 位）
- **用途**：GE DSA 等血管造影设备的 DICOM 图像接收、查看、编辑与转发

## 开源协议与版权

本项目采用 **MIT 开源协议**。

```
MIT License

Copyright (c) 2026 DICOM MIX Tools Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 联系方式

如有问题、建议或合作意向，欢迎联系：

- **邮箱**：[lbmzjz@outlook.com](mailto:lbmzjz@outlook.com)
- **微信**：lbmzjz
