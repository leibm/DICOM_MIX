# -*- coding: utf-8 -*-
"""
PyInstaller 打包脚本（V3.1 优化体积版）

用法: python build_exe.py
"""

import os
import sys
import shutil
import subprocess
import zipfile
import glob


# 需要排除的模块（大幅减小体积）
EXCLUDE_MODULES = [
    "PyQt5", "PyQt6",
    "matplotlib", "scipy", "pandas", "PIL", "pillow",
    "tkinter", "_tkinter", "tcl", "tk",
    "unittest", "test", "tests", "doctest",
    "xmlrpc", "pydoc", "pdb", "profile", "cProfile",
    "lib2to3", "ensurepip", "idlelib", "distutils",
    "setuptools", "pip", "wheel", "pkg_resources",
    "requests",
    "logging.config", "logging.handlers",
    # 大型开发/文档库（程序运行时不需要）
    "babel", "sphinx", "jedi", "docutils", "pygments",
    "IPython", "ipykernel", "jupyter_client", "jupyter_core",
    "tornado", "zmq", "pyzmq",
    "black", "mypy", "packaging",
    "platformdirs", "appdirs",
    "psutil", "paramiko", "nacl", "bcrypt",
    "cryptography.hazmat.backends.openssl",
    # Qt 多余模块
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtLocation",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtNfc", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtSensors", "PySide6.QtSerialBus",
    "PySide6.QtSerialPort", "PySide6.QtShaderTools", "PySide6.QtSpatialAudio",
    "PySide6.QtSql", "PySide6.QtStateMachine",
    "PySide6.QtTest", "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools", "PySide6.QtWebChannel", "PySide6.QtWebEngine",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
    # OpenCV 多余模块
    "cv2.gapi", "cv2.mat_wrapper", "cv2.misc", "cv2.utils",
    "cv2.data", "cv2.typing",
    # numpy 多余模块
    "numpy.random._examples", "numpy.tests", "numpy.testing",
    "numpy.f2py", "numpy.distutils", "numpy.core._dtype_ctypes",
    "numpy.fft.tests", "numpy.linalg.tests", "numpy.ma.tests",
    "numpy.matrixlib.tests", "numpy.polynomial.tests",
    # pydicom 多余模块
    "pydicom.tests", "pydicom.benchmarks",
    # pynetdicom 多余模块
    "pynetdicom.apps", "pynetdicom.tests",
]


def clean():
    """清理之前的构建产物。"""
    import time
    for d in ["build", "dist"]:
        if os.path.isdir(d):
            for attempt in range(3):
                try:
                    shutil.rmtree(d)
                    print(f"已清理 {d}/")
                    break
                except PermissionError:
                    if attempt < 2:
                        print(f"{d}/ 被占用，重试...")
                        time.sleep(2)
                    else:
                        print(f"警告: 无法删除 {d}/，请关闭占用进程后重试")
    for f in os.listdir("."):
        if f.endswith(".spec") and f != "DICOM_MIX_Tools.spec":
            try:
                os.remove(f)
                print(f"已清理 {f}")
            except PermissionError:
                pass


def build():
    """执行 PyInstaller 打包。"""
    icon_path = os.path.join(os.path.dirname(__file__), "assets", "logo.ico")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "DICOM_MIX_Tools",
        "--windowed",
        "--onedir",
        "--noconfirm",
        "--clean",
    ]
    if os.path.isfile(icon_path):
        cmd.extend(["--icon", icon_path])

    # 排除不需要的模块
    for mod in EXCLUDE_MODULES:
        cmd.extend(["--exclude-module", mod])

    # 隐藏导入
    cmd.extend([
        "--hidden-import", "pydicom",
        "--hidden-import", "pynetdicom",
        "--hidden-import", "numpy",
        "--hidden-import", "cv2",
        "--hidden-import", "PySide6.QtCore",
        "--hidden-import", "PySide6.QtGui",
        "--hidden-import", "PySide6.QtWidgets",
    ])

    # 添加 assets 目录（图标等）
    assets_src = os.path.join(os.path.dirname(__file__), "assets")
    if os.path.isdir(assets_src):
        cmd.extend(["--add-data", f"{assets_src};assets"])

    # 入口文件
    cmd.append("main.py")

    print("开始打包...")
    print(" ".join(cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print("打包失败！")
        sys.exit(1)

    print("\n打包完成。输出目录: dist/DICOM_MIX_Tools/")


def clean_dist_extras():
    """清理输出目录中不需要的文件以减小体积。"""
    dist_dir = "dist/DICOM_MIX_Tools"
    if not os.path.isdir(dist_dir):
        return

    removed = 0
    removed_size = 0

    # 删除 Qt 翻译文件（只保留中文和英文）
    qt_trans = os.path.join(dist_dir, "PySide6", "translations")
    if os.path.isdir(qt_trans):
        keep_lang = {"qt_zh_CN.qm", "qt_en.qm", "qtbase_zh_CN.qm", "qtbase_en.qm",
                     "qtbase_zh_TW.qm", "qt_zh_TW.qm"}
        for f in os.listdir(qt_trans):
            if f not in keep_lang:
                fp = os.path.join(qt_trans, f)
                removed_size += os.path.getsize(fp)
                os.remove(fp)
                removed += 1

    # 删除 .pyi 类型提示文件
    for pyi in glob.glob(os.path.join(dist_dir, "**", "*.pyi"), recursive=True):
        removed_size += os.path.getsize(pyi)
        os.remove(pyi)
        removed += 1

    # 删除 __pycache__
    for root, dirs, _ in os.walk(dist_dir):
        for d in dirs:
            if d == "__pycache__":
                p = os.path.join(root, d)
                for f in os.listdir(p):
                    removed_size += os.path.getsize(os.path.join(p, f))
                shutil.rmtree(p, ignore_errors=True)
                removed += 1

    # 删除 Qt 的 .pdb / .lib / .exp 调试文件
    for ext in (".pdb", ".lib", ".exp"):
        for f in glob.glob(os.path.join(dist_dir, "**", f"*{ext}"), recursive=True):
            removed_size += os.path.getsize(f)
            os.remove(f)
            removed += 1

    # 删除 numpy 的测试文件和文档
    for pattern in ["numpy/**/tests", "numpy/**/testing", "numpy/**/random/_examples"]:
        for d in glob.glob(os.path.join(dist_dir, pattern), recursive=True):
            if os.path.isdir(d):
                for root, _, files in os.walk(d):
                    for f in files:
                        removed_size += os.path.getsize(os.path.join(root, f))
                shutil.rmtree(d, ignore_errors=True)
                removed += 1

    # 删除 pydicom 的测试（保留 data，像素处理需要 get_palette_files）
    for sub in ["pydicom/tests", "pydicom/benchmarks"]:
        d = os.path.join(dist_dir, sub)
        if os.path.isdir(d):
            for root, _, files in os.walk(d):
                for f in files:
                    removed_size += os.path.getsize(os.path.join(root, f))
            shutil.rmtree(d, ignore_errors=True)
            removed += 1

    # 删除 pynetdicom 的测试和 apps
    for sub in ["pynetdicom/apps", "pynetdicom/tests"]:
        d = os.path.join(dist_dir, sub)
        if os.path.isdir(d):
            for root, _, files in os.walk(d):
                for f in files:
                    removed_size += os.path.getsize(os.path.join(root, f))
            shutil.rmtree(d, ignore_errors=True)
            removed += 1

    # 删除 OpenCV 的测试和示例
    for sub in ["cv2/gapi", "cv2/mat_wrapper", "cv2/misc", "cv2/utils", "cv2/data"]:
        d = os.path.join(dist_dir, sub)
        if os.path.isdir(d):
            for root, _, files in os.walk(d):
                for f in files:
                    removed_size += os.path.getsize(os.path.join(root, f))
            shutil.rmtree(d, ignore_errors=True)
            removed += 1

    # 删除 Python 标准库的测试文件
    for pattern in ["lib2to3", "ensurepip", "idlelib", "distutils", "tkinter", "tcl", "turtledemo"]:
        d = os.path.join(dist_dir, pattern)
        if os.path.isdir(d):
            for root, _, files in os.walk(d):
                for f in files:
                    removed_size += os.path.getsize(os.path.join(root, f))
            shutil.rmtree(d, ignore_errors=True)
            removed += 1

    # 删除大型开发/文档库（程序运行时不需要）
    for sub in ["babel", "sphinx", "jedi", "docutils", "pygments",
                "IPython", "ipykernel", "jupyter_client", "jupyter_core",
                "tornado", "zmq", "black", "mypy",
                "packaging", "platformdirs", "appdirs",
                "psutil", "paramiko", "nacl", "bcrypt",
                "jsonschema", "nbformat", "nbclient", "nbconvert",
                "dateutil", "pytz", "yaml", "traitlets",
                "wcwidth", "prompt_toolkit", "pexpect", "ptyprocess",
                "_pytest", "pytest", "py",
                "cffi", "pycparser", "greenlet",
                "zipp", "importlib_metadata", "importlib_resources",
                "typing_extensions", "filelock", "pathspec",
                "click", "itsdangerous", "MarkupSafe", "Jinja2",
                " soupsieve", "beautifulsoup4", "html5lib",
                "parso", "jedi", "colorama", "isort", "tomli",
                " Send2Trash", "terminado", "argon2", "async_lru"]:
        d = os.path.join(dist_dir, sub.strip())
        if os.path.isdir(d):
            for root, _, files in os.walk(d):
                for f in files:
                    removed_size += os.path.getsize(os.path.join(root, f))
            shutil.rmtree(d, ignore_errors=True)
            removed += 1

    # 删除 Qt 多余插件（如 bearer、gamepads、geometryloaders、printsupport、qmltooling 等）
    qt_plugins = os.path.join(dist_dir, "PySide6", "plugins")
    if os.path.isdir(qt_plugins):
        for plugin_dir in ["bearer", "gamepads", "geometryloaders", "printsupport",
                           "qmltooling", "sceneparsers", "sqldrivers", "texttospeech",
                           "webview", "xcbglintegrations", "wayland-graphics-integration-client",
                           "wayland-shell-integration", "wayland-decoration-client"]:
            d = os.path.join(qt_plugins, plugin_dir)
            if os.path.isdir(d):
                for root, _, files in os.walk(d):
                    for f in files:
                        removed_size += os.path.getsize(os.path.join(root, f))
                shutil.rmtree(d, ignore_errors=True)
                removed += 1

    if removed:
        print(f"已清理 {removed} 项多余文件，释放 {removed_size / 1024 / 1024:.1f} MB")


def create_archive():
    """将输出目录压缩为 zip 文件。"""
    dist_dir = "dist/DICOM_MIX_Tools"
    zip_path = "dist/DICOM_MIX_Tools_V3.1.zip"
    if not os.path.isdir(dist_dir):
        print("未找到打包目录，跳过压缩")
        return

    print(f"正在压缩 {zip_path} ...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(dist_dir):
            for f in files:
                fpath = os.path.join(root, f)
                arcname = os.path.relpath(fpath, "dist")
                zf.write(fpath, arcname)

    zip_size = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"压缩完成: {zip_path} ({zip_size:.1f} MB)")


def copy_readme():
    """复制说明文件到输出目录。"""
    dist_dir = "dist/DICOM_MIX_Tools"
    if not os.path.isdir(dist_dir):
        return

    readme = os.path.join(dist_dir, "README.txt")
    with open(readme, "w", encoding="utf-8") as f:
        f.write("""DICOM MIX Tools v3.1
====================

DSA 图像路由与编辑工具

使用说明：
1. 双击 DICOM_MIX_Tools.exe 启动
2. 支持 SCP 接收、本地导入、主机查询
3. 内置 DSA 多帧图像查看器（实时减影、窗宽窗位调节）
4. 支持图像序列导出为 MP4 / PNG
5. 支持从主机/DSA 工作站 C-MOVE 拉取图像

依赖：
- 本程序为 Windows 独立可执行文件，无需安装 Python

技术栈：PySide6, pydicom, pynetdicom, numpy, opencv-python
""")
    print(f"已创建 {readme}")


def print_size_summary():
    """打印体积摘要。"""
    dist_dir = "dist/DICOM_MIX_Tools"
    if not os.path.isdir(dist_dir):
        return
    total = 0
    for root, _, files in os.walk(dist_dir):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    print(f"\n打包目录体积: {total / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    clean()
    build()
    clean_dist_extras()
    copy_readme()
    print_size_summary()
    create_archive()
    print("\n全部完成！")
