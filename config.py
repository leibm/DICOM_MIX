# -*- coding: utf-8 -*-
"""
config.py
全局配置模块

集中管理 DICOM MIX Tools 的全局常量与默认参数，
包括 SCP 监听配置、主机节点配置、临时目录路径等。

修改此文件中的默认值即可自定义工具的网络参数，
无需改动业务逻辑代码。
"""

import os

# ------------------------------------------------------------------------------
# 缓存目录配置
# ------------------------------------------------------------------------------

def get_temp_dir() -> str:
    """获取用户配置的缓存目录（优先从 QSettings 读取），若未设置则返回默认值。"""
    try:
        from PySide6.QtCore import QSettings
        settings = QSettings("MedicalSoftware", "DICOMMIXTools")
        custom = settings.value("system/temp_dir", "")
        if custom and isinstance(custom, str) and custom.strip():
            path = custom.strip()
            os.makedirs(path, exist_ok=True)
            return path
    except Exception:
        pass
    return DEFAULT_TEMP_DIR


# ------------------------------------------------------------------------------
# 版本信息
# ------------------------------------------------------------------------------
APP_NAME = "DICOM MIX Tools"
APP_VERSION = "3.0.0"
APP_BUILD_DATE = "2026-05-10"

# ------------------------------------------------------------------------------
# SCP 接收端配置（本机作为 Storage SCP）
# ------------------------------------------------------------------------------

# 本机 SCP 的 Application Entity Title
DEFAULT_SCP_AE_TITLE = "MIX_SCP"

# 本机 SCP 监听端口（默认 11112，避开标准 DICOM 端口 104 的权限问题）
DEFAULT_SCP_PORT = 11112

# ------------------------------------------------------------------------------
# 主机节点配置（本机作为 SCU 时的对端参数）
# ------------------------------------------------------------------------------

# 远端主机的 AE Title
DEFAULT_PACS_AE_TITLE = "PACS"

# 远端主机的 IP 地址或主机名
DEFAULT_PACS_HOST = "127.0.0.1"

# 远端主机的监听端口
DEFAULT_PACS_PORT = 11112

# 本机作为 SCU（C-FIND / C-STORE）时的 AE Title
DEFAULT_LOCAL_SCU_AE_TITLE = "MIX_SCU"

# ------------------------------------------------------------------------------
# DSA 主机/工作站配置（支持多节点）
# ------------------------------------------------------------------------------

# 默认 DSA 节点列表（每个节点包含名称、AE Title、IP、端口）
DEFAULT_DSA_NODES = [
    {
        "name": "DSA-1",
        "ae_title": "DSA",
        "host": "192.168.1.100",
        "port": 11112,
    },
]

# 保留单节点默认值供向后兼容（实际使用 DEFAULT_DSA_NODES）
DEFAULT_DSA_AE_TITLE = "DSA"
DEFAULT_DSA_HOST = "192.168.1.100"
DEFAULT_DSA_PORT = 11112

# ------------------------------------------------------------------------------
# 目录配置
# ------------------------------------------------------------------------------

# 临时目录根路径（使用用户 LOCALAPPDATA，避免 Program Files 权限问题）
DEFAULT_TEMP_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "DICOM_MIX_Tools",
    "temp_dicom"
)

# 处理后的临时输出目录（用于"处理并发送到主机"的中间存储）
DEFAULT_PROCESSED_DIR = os.path.join(DEFAULT_TEMP_DIR, "processed")

# ------------------------------------------------------------------------------
# 处理参数默认值
# ------------------------------------------------------------------------------

# 是否保留原始 DICOM 文件中的私有标签（Private Tags）
DEFAULT_PRESERVE_PRIVATE_TAGS = True
