# -*- coding: utf-8 -*-
"""
PyInstaller 打包脚本

用法: python build_exe.py
"""

import os
import sys
import shutil
import subprocess


def clean():
    """清理之前的构建产物。"""
    for d in ["build", "dist"]:
        if os.path.isdir(d):
            shutil.rmtree(d)
            print(f"已清理 {d}/")
    for f in os.listdir("."):
        if f.endswith(".spec"):
            os.remove(f)
            print(f"已清理 {f}")


def build():
    """执行 PyInstaller 打包。"""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "DICOM_MIX_Tools",
        "--windowed",
        "--onedir",
        "--noconfirm",
        "--clean",
        # 排除冲突的 Qt 绑定
        "--exclude-module", "PyQt5",
        "--exclude-module", "PyQt6",
        # 隐藏导入（PyInstaller 可能检测不到动态导入的模块）
        "--hidden-import", "pydicom",
        "--hidden-import", "pynetdicom",
        "--hidden-import", "numpy",
        "--hidden-import", "PySide6.QtCore",
        "--hidden-import", "PySide6.QtGui",
        "--hidden-import", "PySide6.QtWidgets",
        # 入口文件
        "main.py",
    ]

    print("开始打包...")
    print(" ".join(cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print("打包失败！")
        sys.exit(1)

    print("\n打包完成。输出目录: dist/DICOM_MIX_Tools/")


def copy_readme():
    """复制说明文件到输出目录。"""
    dist_dir = "dist/DICOM_MIX_Tools"
    if not os.path.isdir(dist_dir):
        return

    readme = os.path.join(dist_dir, "README.txt")
    with open(readme, "w", encoding="utf-8") as f:
        f.write("""DICOM MIX Tools
===============

DSA 图像路由与编辑工具

使用说明：
1. 双击 DICOM_MIX_Tools.exe 启动
2. 支持 SCP 接收、本地导入、PACS 查询
3. 内置 DSA 多帧图像查看器（实时减影、窗宽窗位调节）

依赖：
- 本程序为 Windows 独立可执行文件，无需安装 Python

技术栈：PySide6, pydicom, pynetdicom, numpy
""")
    print(f"已创建 {readme}")


if __name__ == "__main__":
    clean()
    build()
    copy_readme()
    print("\n全部完成！")
