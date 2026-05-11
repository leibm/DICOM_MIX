# -*- coding: utf-8 -*-
"""
plugin_manager.py
DICOM MIX Tools 插件管理器 (V4.0)

负责插件的发现、下载、安装、加载和生命周期管理。
插件与主程序解耦，标准安装包不包含插件，运行时按需从远程仓库下载。

插件仓库地址（GitHub）:
  https://github.com/leibm/DICOM_MIX_Plugins

本地插件目录:
  %LOCALAPPDATA%/DICOM_MIX_Tools/plugins/

插件接口规范:
  每个插件是一个 Python 包，必须包含:
    - __init__.py      : 插件入口，导出 PLUGIN_INFO 和 Plugin 类
    - plugin.json      : 元数据（名称、版本、依赖、描述）
    - 业务代码文件     : 实现具体功能

  __init__.py 必须提供:
    PLUGIN_INFO = {
        "name": str,
        "version": str,
        "description": str,
        "author": str,
        "dependencies": list[str],
        "entry_point": str,  # 主类名，如 "NormalizerPlugin"
    }

    class Plugin:
        def __init__(self, app_controller, **kwargs): ...
        def activate(self): ...   # 激活插件（显示UI等）
        def deactivate(self): ... # 停用插件
        def is_available(self) -> bool: ...  # 检查运行环境是否满足
"""

import os
import sys
import json
import shutil
import logging
import importlib.util
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger("plugin_manager")

# ------------------------------------------------------------------------------
# 远程仓库配置
# ------------------------------------------------------------------------------

PLUGIN_REPO_OWNER = "leibm"
PLUGIN_REPO_NAME = "DICOM_MIX_Plugins"
PLUGIN_REPO_BRANCH = "main"

RAW_BASE_URL = f"https://raw.githubusercontent.com/{PLUGIN_REPO_OWNER}/{PLUGIN_REPO_NAME}/{PLUGIN_REPO_BRANCH}"
API_BASE_URL = f"https://api.github.com/repos/{PLUGIN_REPO_OWNER}/{PLUGIN_REPO_NAME}"

# 插件清单文件（位于仓库根目录）
PLUGIN_MANIFEST_PATH = "plugins/manifest.json"


# ------------------------------------------------------------------------------
# 异常定义
# ------------------------------------------------------------------------------

class PluginError(Exception):
    """插件相关异常基类。"""
    pass


class PluginNotFoundError(PluginError):
    """插件未找到。"""
    pass


class PluginInstallError(PluginError):
    """插件安装失败。"""
    pass


class PluginLoadError(PluginError):
    """插件加载失败。"""
    pass


# ------------------------------------------------------------------------------
# 插件管理器
# ------------------------------------------------------------------------------

class PluginManager:
    """
    DICOM MIX Tools 插件管理器。

    职责:
    1. 维护本地插件目录结构
    2. 从远程 GitHub 仓库获取可用插件清单
    3. 下载并安装插件到本地
    4. 动态导入插件模块，管理生命周期
    5. 检查并提示安装插件依赖

    Attributes
    ----------
    plugin_dir : str
        本地插件存储根目录。
    loaded_plugins : dict[str, Any]
        已加载的插件实例字典，键为插件名。
    """

    def __init__(self, plugin_dir: Optional[str] = None):
        if plugin_dir is None:
            plugin_dir = os.path.join(
                os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                "DICOM_MIX_Tools",
                "plugins"
            )
        self.plugin_dir = os.path.abspath(plugin_dir)
        os.makedirs(self.plugin_dir, exist_ok=True)

        # 将插件目录加入 sys.path，以便动态导入
        if self.plugin_dir not in sys.path:
            sys.path.insert(0, self.plugin_dir)

        self._loaded_plugins: Dict[str, Any] = {}
        self._manifest: Optional[List[Dict]] = None

    # ------------------------------------------------------------------
    # 远程清单获取
    # ------------------------------------------------------------------

    def fetch_manifest(self, timeout: int = 10) -> List[Dict]:
        """
        从远程 GitHub 仓库获取插件清单。

        清单文件位于仓库 plugins/manifest.json，格式示例:
        [
          {
            "name": "normalizer",
            "version": "1.0.0",
            "description": "DICOM 异构断层数据归一化",
            "author": "leibm",
            "files": ["__init__.py", "plugin.json", "normalizer_plugin.py"]
          },
          {
            "name": "3d_renderer",
            "version": "1.0.0",
            "description": "三维体渲染与 MPR",
            "author": "leibm",
            "dependencies": ["vtk", "pyvista"],
            "files": ["__init__.py", "plugin.json", "renderer.py"]
          }
        ]

        Returns
        -------
        List[Dict] : 可用插件列表。若获取失败返回空列表。
        """
        url = f"{RAW_BASE_URL}/{PLUGIN_MANIFEST_PATH}"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "DICOM_MIX_Tools/4.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self._manifest = data if isinstance(data, list) else []
                return self._manifest
        except urllib.error.HTTPError as e:
            logger.warning(f"获取插件清单 HTTP 错误 {e.code}: {url}")
            return []
        except Exception as e:
            logger.warning(f"获取插件清单失败: {e}")
            return []

    def get_manifest(self) -> List[Dict]:
        """返回缓存的插件清单，若未获取则先 fetch。"""
        if self._manifest is None:
            self._manifest = self.fetch_manifest()
        return self._manifest

    # ------------------------------------------------------------------
    # 本地插件查询
    # ------------------------------------------------------------------

    def list_installed(self) -> List[str]:
        """列出已安装（已下载到本地）的插件名称。"""
        if not os.path.isdir(self.plugin_dir):
            return []
        result = []
        for name in sorted(os.listdir(self.plugin_dir)):
            plugin_path = os.path.join(self.plugin_dir, name)
            if os.path.isdir(plugin_path):
                # 检查是否包含有效的 plugin.json
                if os.path.isfile(os.path.join(plugin_path, "plugin.json")):
                    result.append(name)
        return result

    def is_installed(self, name: str) -> bool:
        """检查插件是否已安装到本地。"""
        return os.path.isdir(os.path.join(self.plugin_dir, name))

    def get_local_info(self, name: str) -> Optional[Dict]:
        """读取本地已安装插件的 plugin.json 元数据。"""
        json_path = os.path.join(self.plugin_dir, name, "plugin.json")
        if not os.path.isfile(json_path):
            return None
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"读取插件 {name} 元数据失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 安装 / 卸载
    # ------------------------------------------------------------------

    def install(self, name: str, progress_callback=None) -> bool:
        """
        从远程仓库下载并安装插件。

        Parameters
        ----------
        name : str
            插件名称（对应仓库 plugins/{name}/ 目录）。
        progress_callback : callable(str), optional
            进度回调，接收描述文本。

        Returns
        -------
        bool : 安装成功返回 True，否则 False。
        """
        manifest = self.get_manifest()
        plugin_meta = None
        for p in manifest:
            if p.get("name") == name:
                plugin_meta = p
                break

        if plugin_meta is None:
            logger.error(f"插件 {name} 不在远程清单中")
            return False

        files = plugin_meta.get("files", [])
        if not files:
            logger.error(f"插件 {name} 清单中未声明文件列表")
            return False

        target_dir = os.path.join(self.plugin_dir, name)
        # 若已存在则先删除旧版本
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir, ignore_errors=True)
        os.makedirs(target_dir, exist_ok=True)

        total = len(files)
        for i, filename in enumerate(files):
            remote_url = f"{RAW_BASE_URL}/plugins/{name}/{filename}"
            local_path = os.path.join(target_dir, filename)
            if progress_callback:
                progress_callback(f"正在下载 {name}/{filename} ({i+1}/{total})...")
            try:
                req = urllib.request.Request(
                    remote_url,
                    headers={"User-Agent": "DICOM_MIX_Tools/4.0"}
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    # 确保父目录存在（支持子目录文件）
                    os.makedirs(os.path.dirname(local_path) or target_dir, exist_ok=True)
                    with open(local_path, "wb") as f:
                        f.write(resp.read())
            except Exception as e:
                logger.error(f"下载插件文件失败 {remote_url}: {e}")
                # 清理不完整安装
                shutil.rmtree(target_dir, ignore_errors=True)
                return False

        # 自动安装依赖
        deps = plugin_meta.get("dependencies", [])
        if deps:
            missing = self.check_dependencies(deps)
            if missing:
                if progress_callback:
                    progress_callback(f"正在安装依赖: {', '.join(missing)}...")
                logger.info(f"插件 {name} 缺少依赖，尝试安装: {missing}")
                if not self.install_dependencies(missing):
                    logger.warning(f"插件 {name} 依赖安装失败: {missing}")
                    # 依赖安装失败不阻止插件安装，由插件自身在 activate 时提示用户

        logger.info(f"插件 {name} 安装完成")
        return True

    def uninstall(self, name: str) -> bool:
        """卸载本地插件。"""
        target_dir = os.path.join(self.plugin_dir, name)
        if not os.path.exists(target_dir):
            return True
        try:
            # 若已加载，先卸载
            if name in self._loaded_plugins:
                self.unload(name)
            shutil.rmtree(target_dir, ignore_errors=True)
            logger.info(f"插件 {name} 已卸载")
            return True
        except Exception as e:
            logger.error(f"卸载插件 {name} 失败: {e}")
            return False

    # ------------------------------------------------------------------
    # 加载 / 卸载
    # ------------------------------------------------------------------

    def load(self, name: str, app_controller=None, **kwargs) -> Any:
        """
        动态加载插件模块并实例化。

        Parameters
        ----------
        name : str
            插件名称。
        app_controller : Any, optional
            应用控制器实例，传递给插件初始化。
        **kwargs :
            额外参数传递给插件构造函数。

        Returns
        -------
        Any : 插件实例。

        Raises
        ------
        PluginNotFoundError : 插件未安装。
        PluginLoadError : 加载或实例化失败。
        """
        if name in self._loaded_plugins:
            return self._loaded_plugins[name]

        plugin_dir = os.path.join(self.plugin_dir, name)
        if not os.path.isdir(plugin_dir):
            raise PluginNotFoundError(f"插件 {name} 未安装")

        init_path = os.path.join(plugin_dir, "__init__.py")
        if not os.path.isfile(init_path):
            raise PluginLoadError(f"插件 {name} 缺少 __init__.py")

        try:
            # 动态导入模块
            spec = importlib.util.spec_from_file_location(
                f"dicom_mix_plugin_{name}", init_path
            )
            module = importlib.util.module_from_spec(spec)
            # 将插件目录加入模块搜索路径，便于插件内部导入
            sys.path.insert(0, plugin_dir)
            spec.loader.exec_module(module)
            sys.path.pop(0)

            # 获取插件元数据
            plugin_info = getattr(module, "PLUGIN_INFO", {})
            entry_point = plugin_info.get("entry_point", "Plugin")
            plugin_class = getattr(module, entry_point, None)

            if plugin_class is None:
                raise PluginLoadError(
                    f"插件 {name} 未找到入口类 {entry_point}"
                )

            # 实例化
            instance = plugin_class(app_controller=app_controller, **kwargs)
            self._loaded_plugins[name] = instance
            logger.info(f"插件 {name} 已加载")
            return instance

        except Exception as e:
            logger.exception(f"加载插件 {name} 失败")
            raise PluginLoadError(f"加载插件 {name} 失败: {e}")

    def unload(self, name: str) -> bool:
        """卸载已加载的插件。"""
        if name not in self._loaded_plugins:
            return True
        instance = self._loaded_plugins[name]
        try:
            if hasattr(instance, "deactivate"):
                instance.deactivate()
        except Exception as e:
            logger.warning(f"插件 {name} 停用异常: {e}")
        finally:
            del self._loaded_plugins[name]
            logger.info(f"插件 {name} 已卸载")
        return True

    def is_loaded(self, name: str) -> bool:
        """检查插件是否已加载。"""
        return name in self._loaded_plugins

    def get_loaded_instance(self, name: str) -> Optional[Any]:
        """获取已加载的插件实例。"""
        return self._loaded_plugins.get(name)

    # ------------------------------------------------------------------
    # 依赖检查
    # ------------------------------------------------------------------

    @staticmethod
    def check_dependencies(deps: List[str]) -> List[str]:
        """
        检查依赖包是否已安装。

        Returns
        -------
        List[str] : 未安装的依赖包名列表。空列表表示全部已安装。
        """
        missing = []
        for dep in deps:
            try:
                importlib.import_module(dep)
            except ImportError:
                missing.append(dep)
        return missing

    def install_dependencies(self, deps: List[str]) -> bool:
        """
        尝试使用 pip 安装缺失的依赖。

        注意：此操作需要系统已安装 pip 且用户有写入 site-packages 的权限。
        对于打包后的可执行文件环境，可能无法正常工作。
        """
        import subprocess
        if not deps:
            return True
        try:
            cmd = [sys.executable, "-m", "pip", "install"] + deps
            subprocess.run(cmd, check=True, capture_output=True)
            return True
        except Exception as e:
            logger.error(f"安装依赖失败: {e}")
            return False
