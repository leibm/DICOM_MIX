# -*- coding: utf-8 -*-
"""
main.py
程序入口模块

DICOM MIX Tools 的主入口，职责如下：
1. 初始化 QApplication 与主窗口 (MainWindow)。
2. 实例化并组装三大核心模块：
   - DicomInputManager（数据输入：SCP + 本地载入）
   - DicomNetworkManager（网络交互：C-FIND + C-STORE）
   - DicomProcessor（数据处理：信息覆写 + UID 重新生成）
3. 建立 UI 与各模块之间的信号/槽连接，实现完整的业务闭环：
   - 用户点击 SCP 启停 → 启动/停止 SCP 服务端
   - 用户选择本地文件夹 → 载入 DICOM 并刷新左侧树
   - 用户查询 PACS → 发送 C-FIND 并展示结果
   - 用户点击"发送到 PACS" → 先处理（覆写信息+重新生成UID），再 C-STORE
   - 用户点击"导出到本地" → 处理并保存到用户指定目录

技术栈：PySide6, pynetdicom, pydicom
"""

import sys
import os

# 确保当前目录在模块搜索路径中（PyInstaller 打包后需要）
if getattr(sys, 'frozen', False):
    # 打包后的可执行文件运行时
    application_dir = os.path.dirname(sys.executable)
else:
    # 直接运行 .py 脚本时
    application_dir = os.path.dirname(os.path.abspath(__file__))

if application_dir not in sys.path:
    sys.path.insert(0, application_dir)

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QObject, Qt

# 导入自定义模块
from ui_main import MainWindow
from dicom_input import DicomInputManager
from dicom_network import DicomNetworkManager, PacsNodeConfig
from dicom_processor import DicomProcessor
from config import (
    DEFAULT_SCP_AE_TITLE, DEFAULT_SCP_PORT,
    DEFAULT_PACS_AE_TITLE, DEFAULT_PACS_HOST, DEFAULT_PACS_PORT,
    DEFAULT_LOCAL_SCU_AE_TITLE, DEFAULT_TEMP_DIR
)


# ------------------------------------------------------------------------------
# 应用控制器（负责模块组装与业务逻辑编排）
# ------------------------------------------------------------------------------

class ApplicationController(QObject):
    """
    应用控制器。

    作为 UI 与后端业务模块之间的协调层，负责：
    - 信号转发：将各工作线程的信号连接到 UI 的显示槽。
    - 业务编排：处理"先处理再发送"等链式操作流程。
    - 状态管理：跟踪当前是否有待完成的链式操作。
    """

    def __init__(self):
        super().__init__()

        # 1. 创建主窗口
        self.window = MainWindow()

        # 2. 读取 UI 中保存的网络配置（优先使用用户自定义配置）
        net_cfg = self.window.get_network_config()

        self.input_mgr = DicomInputManager(
            ae_title=net_cfg.get("scp_ae_title", DEFAULT_SCP_AE_TITLE),
            port=net_cfg.get("scp_port", DEFAULT_SCP_PORT),
            temp_dir=DEFAULT_TEMP_DIR,
            parent=self
        )

        pacs_config = PacsNodeConfig(
            ae_title=net_cfg.get("pacs_ae_title", DEFAULT_PACS_AE_TITLE),
            host=net_cfg.get("pacs_host", DEFAULT_PACS_HOST),
            port=net_cfg.get("pacs_port", DEFAULT_PACS_PORT),
            local_ae_title=net_cfg.get("scu_ae_title", DEFAULT_LOCAL_SCU_AE_TITLE)
        )
        self.network_mgr = DicomNetworkManager(
            pacs_config=pacs_config,
            parent=self
        )

        self.processor = DicomProcessor(
            temp_dir=os.path.join(DEFAULT_TEMP_DIR, "processed"),
            parent=self
        )

        # 3. 连接信号
        self._connect_signals()

        # 4. 状态标记：用于处理"先处理再发送"的链式操作
        self._pending_store_after_process = False

    # ---------- 信号连接 ----------

    def _connect_signals(self):
        """建立所有模块与 UI 之间的信号/槽连接。"""

        # ===== 数据输入模块 → UI =====
        self.input_mgr.signals.study_received.connect(
            self.window.input_signals.study_received
        )
        self.input_mgr.signals.scp_status_changed.connect(
            self.window.input_signals.scp_status_changed
        )
        self.input_mgr.signals.local_load_progress.connect(
            self.window.input_signals.local_load_progress
        )
        # 本地载入完成后，除了更新 UI，还需要刷新左侧树
        self.input_mgr.signals.local_load_finished.connect(
            self._on_local_load_finished
        )
        self.input_mgr.signals.error_occurred.connect(
            self.window.input_signals.error_occurred
        )

        # ===== 网络模块 → UI =====
        self.network_mgr.signals.find_results_ready.connect(
            self.window.network_signals.find_results_ready
        )
        self.network_mgr.signals.store_progress.connect(
            self.window.network_signals.store_progress
        )
        self.network_mgr.signals.store_finished.connect(
            self.window.network_signals.store_finished
        )
        self.network_mgr.signals.error_occurred.connect(
            self.window.network_signals.error_occurred
        )

        # ===== 数据处理模块 → UI =====
        self.processor.signals.process_progress.connect(
            self.window.processor_signals.process_progress
        )
        # 处理完成后，除了更新 UI，还需要判断是否是链式发送操作
        self.processor.signals.process_finished.connect(
            self._on_process_finished
        )
        self.processor.signals.error_occurred.connect(
            self.window.processor_signals.error_occurred
        )

        # ===== UI → 业务模块 =====
        self.window.request_start_scp.connect(self.input_mgr.start_scp)
        self.window.request_stop_scp.connect(self.input_mgr.stop_scp)
        self.window.request_load_local.connect(self.input_mgr.load_local_folder)
        self.window.request_pacs_find.connect(self.network_mgr.find_studies)
        self.window.request_process_and_store.connect(self._on_process_and_store)
        self.window.request_process_and_export.connect(self._on_process_and_export)

        # 网络配置变更时重新初始化网络模块
        self.window.network_config_changed.connect(self._on_network_config_changed)

        # 树刷新按钮
        self.window.btn_refresh_tree.clicked.connect(self._refresh_study_tree)

    # ---------- 业务逻辑处理 ----------

    def _extract_files_from_series(self, series_list: list) -> list:
        """
        从左侧树形控件返回的序列列表中提取所有实例文件路径。

        参数：
            series_list: StudyTreeModel.get_checked_series() 返回的序列数据字典列表
        返回：
            扁平化的 DICOM 文件绝对路径列表
        """
        files = []
        for series_data in series_list:
            instances = series_data.get("instances", [])
            for fpath in instances:
                if os.path.isfile(fpath):
                    files.append(fpath)
        return files

    def _refresh_study_tree(self):
        """扫描临时目录并刷新左侧树形控件。"""
        try:
            studies = self.input_mgr.scan_temp_studies()
            self.window.tree_model.clear()
            for study in studies:
                self.window.add_study_to_tree(study)
            self.window.status_bar.showMessage(f"已刷新，共 {len(studies)} 个检查")
        except Exception as e:
            self.window._show_error(f"刷新源数据失败: {e}")

    def _on_local_load_finished(self, count: int):
        """本地文件夹载入完成回调：更新 UI 并刷新树。"""
        self.window.input_signals.local_load_finished.emit(count)
        self._refresh_study_tree()

    def _on_process_and_store(self, series_list: list, target_info: dict):
        """
        用户点击"应用拆分并发送到 PACS"的处理流程：
        1. 从 series_list 提取文件路径
        2. 调用 processor 处理到临时目录（异步）
        3. 在处理完成的回调中，将处理后的文件通过 C-STORE 发送到 PACS
        """
        file_list = self._extract_files_from_series(series_list)
        if not file_list:
            QMessageBox.warning(self.window, "提示", "未获取到有效的 DICOM 文件路径")
            return

        # 启动异步处理，标记后续需要发送
        output_dir = self.processor.process_for_store(file_list, target_info)
        self._pending_store_after_process = True
        self._pending_store_output_dir = output_dir

    def _on_process_and_export(self, series_list: list, target_info: dict, output_dir: str):
        """
        用户点击"应用拆分并导出到本地"的处理流程：
        直接调用 processor 处理并保存到用户指定的输出目录。
        """
        file_list = self._extract_files_from_series(series_list)
        if not file_list:
            QMessageBox.warning(self.window, "提示", "未获取到有效的 DICOM 文件路径")
            return

        self._pending_store_after_process = False
        self.processor.process_and_export(file_list, target_info, output_dir)

    def _on_process_finished(self, success_count: int, output_dir: str):
        """
        数据处理完成回调。

        如果是"处理并发送"模式（_pending_store_after_process=True），
        则扫描输出目录并将处理后的文件发送到 PACS。
        否则仅更新 UI（导出模式已在 UI 信号中处理）。
        """
        # 先向 UI 通知处理完成（更新进度条和状态栏）
        self.window.processor_signals.process_finished.emit(success_count, output_dir)

        if getattr(self, '_pending_store_after_process', False):
            self._pending_store_after_process = False

            # 扫描处理后的文件
            processed_files = self.processor.get_processed_files(output_dir)
            if not processed_files:
                QMessageBox.warning(
                    self.window, "提示",
                    f"处理完成（{success_count} 个文件），但未找到可发送的文件。"
                )
                return

            # 启动 C-STORE 发送
            self.window.status_bar.showMessage(
                f"处理完成，正在发送 {len(processed_files)} 个文件到 PACS..."
            )
            self.network_mgr.send_files(processed_files)

    def _on_network_config_changed(self, cfg: dict):
        """
        网络配置变更回调。

        - PACS/SCU 配置：可直接重新初始化 NetworkManager，不影响运行中的操作。
        - SCP 配置（AE Title / Port）：如果 SCP 正在运行，提示用户先停止再启动以应用新配置；
          如果未运行，则更新 input_mgr 的端口/AE Title。
        """
        # 1. 重新初始化 NetworkManager（PACS + SCU）
        pacs_config = PacsNodeConfig(
            ae_title=cfg.get("pacs_ae_title", DEFAULT_PACS_AE_TITLE),
            host=cfg.get("pacs_host", DEFAULT_PACS_HOST),
            port=cfg.get("pacs_port", DEFAULT_PACS_PORT),
            local_ae_title=cfg.get("scu_ae_title", DEFAULT_LOCAL_SCU_AE_TITLE)
        )
        self.network_mgr = DicomNetworkManager(
            pacs_config=pacs_config,
            parent=self
        )
        # 重新连接网络模块信号到 UI
        self.network_mgr.signals.find_results_ready.connect(
            self.window.network_signals.find_results_ready
        )
        self.network_mgr.signals.store_progress.connect(
            self.window.network_signals.store_progress
        )
        self.network_mgr.signals.store_finished.connect(
            self.window.network_signals.store_finished
        )
        self.network_mgr.signals.error_occurred.connect(
            self.window.network_signals.error_occurred
        )
        # 重新连接 PACS 查询请求
        self.window.request_pacs_find.connect(self.network_mgr.find_studies)

        # 2. SCP 配置：如果未运行，直接更新；如果运行中，提示用户
        if self.input_mgr.is_scp_running():
            QMessageBox.information(
                self.window, "网络配置已更新",
                "PACS / SCU 配置已即时生效。\n"
                "SCP 接收端配置（端口/AE Title）已保存，"
                "请先停止 SCP 再重新启动以应用新配置。"
            )
        else:
            # SCP 未运行，可以安全地重新创建 input_mgr
            self.input_mgr = DicomInputManager(
                ae_title=cfg.get("scp_ae_title", DEFAULT_SCP_AE_TITLE),
                port=cfg.get("scp_port", DEFAULT_SCP_PORT),
                temp_dir=DEFAULT_TEMP_DIR,
                parent=self
            )
            # 重新连接信号
            self.input_mgr.signals.study_received.connect(
                self.window.input_signals.study_received
            )
            self.input_mgr.signals.scp_status_changed.connect(
                self.window.input_signals.scp_status_changed
            )
            self.input_mgr.signals.local_load_progress.connect(
                self.window.input_signals.local_load_progress
            )
            self.input_mgr.signals.local_load_finished.connect(
                self._on_local_load_finished
            )
            self.input_mgr.signals.error_occurred.connect(
                self.window.input_signals.error_occurred
            )
            self.window.request_start_scp.connect(self.input_mgr.start_scp)
            self.window.request_stop_scp.connect(self.input_mgr.stop_scp)
            self.window.request_load_local.connect(self.input_mgr.load_local_folder)

            self.window.status_bar.showMessage("网络配置已更新并即时生效")

    # ---------- 公共接口 ----------

    def show(self):
        """显示主窗口。"""
        self.window.show()


# ------------------------------------------------------------------------------
# 程序入口
# ------------------------------------------------------------------------------

def main():
    """主函数：初始化应用并启动事件循环。"""
    # PySide6 默认已启用高 DPI 缩放，无需手动设置
    app = QApplication(sys.argv)
    app.setStyle("Fusion")  # 使用 Fusion 风格，跨平台一致性较好

    # 设置应用信息
    app.setApplicationName("DICOM MIX Tools")
    app.setOrganizationName("MedicalSoftware")

    # 创建并显示主控制器
    controller = ApplicationController()
    controller.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
