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
   - 用户查询主机 → 发送 C-FIND 并展示结果
   - 用户点击"发送到主机" → 先处理（覆写信息+重新生成UID），再 C-STORE
   - 用户点击"导出到本地" → 处理并保存到用户指定目录

技术栈：PySide6, pynetdicom, pydicom
"""

import sys
import os
import logging

logger = logging.getLogger("main")

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
from PySide6.QtCore import QObject, Qt, QTimer, QThread, Signal

# Fluent Design 主题
from qfluentwidgets import setTheme, Theme, setThemeColor

FLUENT_STYLE = """
/* 全局 Fluent 风格 */
QWidget {
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 13px;
}

QMainWindow {
    background-color: #f9f9f9;
}

QGroupBox {
    background-color: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    margin-top: 8px;
    padding-top: 10px;
    padding-bottom: 8px;
    padding-left: 10px;
    padding-right: 10px;
    font-weight: 600;
    color: #1f2937;
    font-size: 13px;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 8px;
    color: #374151;
}

QPushButton {
    background-color: #ffffff;
    color: #1f2937;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 6px 14px;
    font-weight: 500;
    font-size: 13px;
}

QPushButton:hover {
    background-color: #f3f4f6;
    border-color: #9ca3af;
}

QPushButton:pressed {
    background-color: #e5e7eb;
}

QPushButton#success {
    background-color: #0078d4;
    color: white;
    border: none;
}

QPushButton#success:hover {
    background-color: #106ebe;
}

QPushButton#success:pressed {
    background-color: #005a9e;
}

QPushButton#success:checked {
    background-color: #004578;
    border: 2px solid #0078d4;
}

QPushButton#secondary {
    background-color: #f3f4f6;
    color: #374151;
    border: 1px solid #d1d5db;
}

QPushButton#secondary:hover {
    background-color: #e5e7eb;
}

QPushButton#danger {
    background-color: #fef2f2;
    color: #dc2626;
    border: 1px solid #fecaca;
}

QPushButton#danger:hover {
    background-color: #fee2e2;
}

QPushButton#panelBtn {
    background-color: transparent;
    color: #6b7280;
    border: none;
    border-radius: 6px;
    padding: 6px 12px;
    font-weight: 500;
}

QPushButton#panelBtn:hover {
    background-color: #f3f4f6;
    color: #374151;
}

QPushButton#panelBtn:checked {
    background-color: #e5e7eb;
    color: #111827;
}

QLineEdit {
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 6px 10px;
    background: #ffffff;
    font-size: 13px;
    color: #1f2937;
}

QLineEdit:focus {
    border-color: #0078d4;
}

QComboBox {
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 5px 10px;
    background: #ffffff;
    font-size: 13px;
    color: #1f2937;
    min-width: 80px;
}

QComboBox:focus {
    border-color: #0078d4;
}

QComboBox::drop-down {
    border: none;
    width: 24px;
}

QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #6b7280;
    width: 0;
    height: 0;
}

QSpinBox {
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 4px 8px;
    background: #ffffff;
    font-size: 13px;
}

QSpinBox:focus {
    border-color: #0078d4;
}

QProgressBar {
    border: none;
    border-radius: 4px;
    background-color: #e5e7eb;
    text-align: center;
    color: #374151;
    font-size: 11px;
}

QProgressBar::chunk {
    background-color: #0078d4;
    border-radius: 4px;
}

QTableView {
    border: 1px solid #e5e7eb;
    border-radius: 6px;
    background-color: #ffffff;
    gridline-color: #f3f4f6;
    font-size: 13px;
    selection-background-color: #dbeafe;
    selection-color: #1e40af;
}

QTableView::item {
    padding: 6px 8px;
    border-bottom: 1px solid #f3f4f6;
}

QTableView::item:selected {
    background-color: #dbeafe;
    color: #1e40af;
}

QHeaderView::section {
    background-color: #f9fafb;
    color: #374151;
    padding: 8px 10px;
    border: none;
    border-bottom: 1px solid #e5e7eb;
    font-weight: 600;
    font-size: 12px;
}

QTreeView {
    border: 1px solid #e5e7eb;
    border-radius: 6px;
    background-color: #ffffff;
    font-size: 13px;
    outline: none;
}

QTreeView::item {
    padding: 6px 4px;
    min-height: 28px;
    border-bottom: 1px solid #f9fafb;
}

QTreeView::item:selected {
    background-color: #dbeafe;
    color: #1e40af;
}

QTreeView::branch {
    padding: 4px;
}

QTabWidget::pane {
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    background-color: #ffffff;
    top: -1px;
}

QTabBar::tab {
    background-color: #f9fafb;
    color: #6b7280;
    padding: 8px 16px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    border: 1px solid #e5e7eb;
    border-bottom: none;
    font-weight: 500;
    font-size: 13px;
}

QTabBar::tab:selected {
    background-color: #ffffff;
    color: #0078d4;
    border-bottom: 2px solid #0078d4;
}

QTabBar::tab:hover:!selected {
    background-color: #f3f4f6;
    color: #374151;
}

QToolTip {
    background-color: #1f2937;
    color: #f9fafb;
    border: 1px solid #374151;
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
}

QScrollArea {
    border: none;
    background-color: transparent;
}

QMenu {
    background-color: #ffffff;
    color: #1f2937;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    padding: 6px;
}

QMenu::item {
    padding: 8px 24px;
    border-radius: 6px;
    font-size: 13px;
}

QMenu::item:selected {
    background-color: #eff6ff;
    color: #0078d4;
}

QMenu::separator {
    height: 1px;
    background-color: #e5e7eb;
    margin: 6px 12px;
}

QStatusBar {
    background-color: #f9fafb;
    color: #6b7280;
    border-top: 1px solid #e5e7eb;
    font-size: 12px;
}

QDockWidget {
    titlebar-close-icon: url(close.png);
    titlebar-normal-icon: url(float.png);
}

QDockWidget::title {
    background-color: #f9fafb;
    padding: 8px 12px;
    border: 1px solid #e5e7eb;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    font-weight: 600;
    color: #374151;
}

QDockWidget::close-button, QDockWidget::float-button {
    background-color: transparent;
    border-radius: 4px;
    padding: 2px;
}

QDockWidget::close-button:hover, QDockWidget::float-button:hover {
    background-color: #e5e7eb;
}
"""

# 导入自定义模块
from ui_main import MainWindow, ExportDialog
from dicom_input import DicomInputManager
from dicom_network import DicomNetworkManager, PacsNodeConfig
from dicom_processor import DicomProcessor
from config import (
    DEFAULT_SCP_AE_TITLE, DEFAULT_SCP_PORT,
    DEFAULT_PACS_AE_TITLE, DEFAULT_PACS_HOST, DEFAULT_PACS_PORT,
    DEFAULT_LOCAL_SCU_AE_TITLE, DEFAULT_TEMP_DIR,
    DEFAULT_DSA_AE_TITLE, DEFAULT_DSA_HOST, DEFAULT_DSA_PORT,
    DEFAULT_DSA_NODES,
)


# ------------------------------------------------------------------------------
# 导出工作线程
# ------------------------------------------------------------------------------

class ExportWorker(QObject):
    """在后台线程中将图像序列导出为 MP4 或 PNG。"""

    progress = Signal(int, int)   # (当前帧, 总帧数)
    finished = Signal(str)        # 完成消息
    error = Signal(str)           # 错误消息

    def __init__(self, frames_iter, fmt: str, params: dict, parent=None):
        super().__init__(parent)
        self._frames = list(frames_iter)  # 预先收集所有帧
        self._fmt = fmt
        self._params = params

    def run(self):
        try:
            import cv2
            total = len(self._frames)
            if total == 0:
                self.error.emit("没有可导出的帧")
                return

            h, w = self._frames[0].shape[:2]

            if self._fmt == "mp4":
                fps = self._params.get("fps", 15)
                output_path = self._params["output_path"]
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                writer = cv2.VideoWriter(output_path, fourcc, fps, (w, h), isColor=False)
                if not writer.isOpened():
                    self.error.emit(f"无法创建视频文件: {output_path}")
                    return
                for i, frame in enumerate(self._frames):
                    writer.write(frame)
                    self.progress.emit(i + 1, total)
                writer.release()
                self.finished.emit(f"MP4 视频已导出\n{output_path}\n共 {total} 帧")

            elif self._fmt == "png":
                output_dir = self._params["output_path"]
                prefix = self._params.get("prefix", "frame_")
                for i, frame in enumerate(self._frames):
                    fname = f"{prefix}{i:04d}.png"
                    fpath = os.path.join(output_dir, fname)
                    cv2.imwrite(fpath, frame)
                    self.progress.emit(i + 1, total)
                self.finished.emit(f"PNG 图片已导出到\n{output_dir}\n共 {total} 帧")

        except Exception as e:
            logger.exception("导出异常")
            self.error.emit(f"导出异常: {e}")


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
        dsa_nodes = net_cfg.get("dsa_nodes", [])
        dsa_configs = [
            PacsNodeConfig(
                ae_title=n.get("ae_title", DEFAULT_DSA_AE_TITLE),
                host=n.get("host", DEFAULT_DSA_HOST),
                port=n.get("port", DEFAULT_DSA_PORT),
                local_ae_title=net_cfg.get("scu_ae_title", DEFAULT_LOCAL_SCU_AE_TITLE)
            )
            for n in dsa_nodes
        ]
        self.network_mgr = DicomNetworkManager(
            pacs_config=pacs_config,
            dsa_configs=dsa_configs,
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

        # 5. SCP 接收防抖刷新定时器
        # 连续接收多个文件时，只在最后一次接收后 1.5 秒刷新树，避免频繁刷新
        self._scp_refresh_timer = QTimer(self)
        self._scp_refresh_timer.setSingleShot(True)
        self._scp_refresh_timer.timeout.connect(self._refresh_study_tree)

        # 6. 启动后自动刷新数据源列表（延迟 300ms 等待 UI 渲染完成）
        QTimer.singleShot(300, self._refresh_study_tree)

    # ---------- 信号连接 ----------

    def _connect_signals(self):
        """建立所有模块与 UI 之间的信号/槽连接。"""

        # ===== 数据输入模块 → UI =====
        self.input_mgr.signals.study_received.connect(
            self.window.input_signals.study_received
        )
        # SCP 接收新文件后，防抖自动刷新左侧树
        self.input_mgr.signals.study_received.connect(
            self._on_scp_study_received
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

        # DSA 信号转发
        self.network_mgr.signals.dsa_find_results_ready.connect(
            self.window.network_signals.dsa_find_results_ready
        )
        self.network_mgr.signals.dsa_move_progress.connect(
            self.window.network_signals.dsa_move_progress
        )
        self.network_mgr.signals.dsa_move_finished.connect(
            self.window.network_signals.dsa_move_finished
        )
        # C-MOVE 完成后延迟 3 秒自动刷新左侧树（给远端推送文件的时间）
        self.network_mgr.signals.dsa_move_finished.connect(
            self._on_dsa_move_auto_refresh
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

        # DSA 请求（带 dsa_index 参数）
        self.window.request_dsa_find.connect(self._on_dsa_find)
        self.window.request_dsa_move.connect(self._on_dsa_move)

        # 导出请求
        self.window.request_export.connect(self._on_export)

        # 网络配置变更时重新初始化网络模块
        self.window.network_config_changed.connect(self._on_network_config_changed)

        # 左侧刷新按钮
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

    def _on_scp_study_received(self, study_uid: str, patient_name: str):
        """SCP 接收到新 Study 后，防抖自动刷新左侧树。"""
        # 每次接收文件时重启定时器，延迟 1.5 秒后刷新
        # 避免连续接收多个文件时频繁刷新树
        self._scp_refresh_timer.start(1500)

    def _on_dsa_move_auto_refresh(self, success: int, total: int):
        """C-MOVE 完成后延迟刷新左侧树，给远端设备推送文件的时间。"""
        QTimer.singleShot(3000, self._refresh_study_tree)

    def _on_local_load_finished(self, count: int):
        """本地文件夹载入完成回调：更新 UI 并刷新树。"""
        self.window.input_signals.local_load_finished.emit(count)
        self._refresh_study_tree()

    def _on_process_and_store(self, series_list: list, target_info: dict):
        """
        用户点击"应用拆分并发送到主机"的处理流程：
        1. 从 series_list 提取文件路径
        2. 调用 processor 处理到临时目录（异步）
        3. 在处理完成的回调中，将处理后的文件通过 C-STORE 发送到主机
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
        则扫描输出目录并将处理后的文件发送到主机。
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
                f"处理完成，正在发送 {len(processed_files)} 个文件到主机..."
            )
            self.network_mgr.send_files(processed_files)

    def _on_dsa_find(self, query_dict: dict, dsa_index: int):
        """
        DSA C-FIND 请求回调。
        向指定 DSA 工作站发起 C-FIND 查询。
        """
        self.network_mgr.query_dsa(query_dict, dsa_index)

    def _on_dsa_move(self, study_uid: str, move_dest_ae: str, dsa_index: int):
        """
        DSA C-MOVE 请求回调。
        向指定 DSA 工作站发起 C-MOVE，指示其将图像推送到本机 SCP。
        """
        self.window.status_bar.showMessage(f"正在从 DSA 拉取检查 {study_uid}...")
        self.network_mgr.move_from_dsa(study_uid, move_dest_ae, dsa_index)

    def _on_network_config_changed(self, cfg: dict):
        """
        网络配置变更回调。

        - 主机/SCU 配置：可直接重新初始化 NetworkManager，不影响运行中的操作。
        - SCP 配置（AE Title / Port）：如果 SCP 正在运行，提示用户先停止再启动以应用新配置；
          如果未运行，则更新 input_mgr 的端口/AE Title。
        """
        # 1. 重新初始化 NetworkManager（主机 + SCU + DSA）
        pacs_config = PacsNodeConfig(
            ae_title=cfg.get("pacs_ae_title", DEFAULT_PACS_AE_TITLE),
            host=cfg.get("pacs_host", DEFAULT_PACS_HOST),
            port=cfg.get("pacs_port", DEFAULT_PACS_PORT),
            local_ae_title=cfg.get("scu_ae_title", DEFAULT_LOCAL_SCU_AE_TITLE)
        )
        dsa_nodes = cfg.get("dsa_nodes", [])
        dsa_configs = [
            PacsNodeConfig(
                ae_title=n.get("ae_title", DEFAULT_DSA_AE_TITLE),
                host=n.get("host", DEFAULT_DSA_HOST),
                port=n.get("port", DEFAULT_DSA_PORT),
                local_ae_title=cfg.get("scu_ae_title", DEFAULT_LOCAL_SCU_AE_TITLE)
            )
            for n in dsa_nodes
        ]
        self.network_mgr = DicomNetworkManager(
            pacs_config=pacs_config,
            dsa_configs=dsa_configs,
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
        # DSA 信号
        self.network_mgr.signals.dsa_find_results_ready.connect(
            self.window.network_signals.dsa_find_results_ready
        )
        self.network_mgr.signals.dsa_move_progress.connect(
            self.window.network_signals.dsa_move_progress
        )
        self.network_mgr.signals.dsa_move_finished.connect(
            self.window.network_signals.dsa_move_finished
        )
        self.network_mgr.signals.dsa_move_finished.connect(
            self._on_dsa_move_auto_refresh
        )
        # 重新连接请求信号：先断开旧连接，避免重复累积
        for signal, slot in [
            (self.window.request_pacs_find, self.network_mgr.find_studies),
            (self.window.request_dsa_find, self._on_dsa_find),
            (self.window.request_dsa_move, self._on_dsa_move),
        ]:
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass
            signal.connect(slot)
        # request_dsa_move 连接的是 _on_dsa_move（ApplicationController 的方法），
        # 不需要重新连接，因为它内部通过 self.network_mgr 访问当前实例

        # 2. SCP 配置：如果未运行，直接更新；如果运行中，提示用户
        if self.input_mgr.is_scp_running():
            QMessageBox.information(
                self.window, "网络配置已更新",
                "主机 / SCU 配置已即时生效。\n"
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
            # SCP 接收新文件后，防抖自动刷新左侧树
            self.input_mgr.signals.study_received.connect(
                self._on_scp_study_received
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
            # 先断开旧连接再重新连接，避免重复累积
            for signal, slot in [
                (self.window.request_start_scp, self.input_mgr.start_scp),
                (self.window.request_stop_scp, self.input_mgr.stop_scp),
                (self.window.request_load_local, self.input_mgr.load_local_folder),
            ]:
                try:
                    signal.disconnect()
                except (TypeError, RuntimeError):
                    pass
                signal.connect(slot)

            self.window.status_bar.showMessage("网络配置已更新并即时生效")

    # ---------- 导出处理 ----------

    def _on_export(self, fmt: str, params: dict):
        """处理图像序列导出请求。"""
        viewer = self.window.dsa_viewer
        if not viewer or viewer.total_frames == 0:
            QMessageBox.warning(self.window, "提示", "当前没有加载图像序列")
            return

        self._export_thread = QThread(self)
        self._export_worker = ExportWorker(
            viewer.get_export_frames(), fmt, params
        )
        self._export_worker.moveToThread(self._export_thread)

        # 连接信号到导出对话框
        export_dialog = getattr(self.window, '_export_dialog', None)

        if export_dialog:
            self._export_worker.progress.connect(export_dialog.on_progress)
            self._export_worker.finished.connect(export_dialog.on_finished)
            self._export_worker.error.connect(export_dialog.on_error)

        self._export_worker.finished.connect(self._on_export_cleanup)
        self._export_worker.error.connect(self._on_export_cleanup)

        self._export_thread.started.connect(self._export_worker.run)
        self._export_worker.finished.connect(self._export_thread.quit)
        self._export_worker.error.connect(self._export_thread.quit)
        self._export_worker.finished.connect(self._export_worker.deleteLater)
        self._export_worker.error.connect(self._export_worker.deleteLater)
        self._export_thread.finished.connect(self._export_thread.deleteLater)

        self._export_thread.start()
        self.window.status_bar.showMessage(f"正在导出{'视频' if fmt == 'mp4' else '图片'}...")

    def _on_export_cleanup(self, *_args):
        """导出完成后更新状态栏。"""
        self.window.status_bar.showMessage("导出完成")

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
    app.setStyle("Fusion")  # 使用 Fusion 作为基础，配合 QSS 覆盖

    # 设置 Fluent Design 主题
    setTheme(Theme.LIGHT)
    setThemeColor("#0078d4")

    # 应用全局 Fluent 风格 QSS
    app.setStyleSheet(FLUENT_STYLE)

    # 设置应用信息
    app.setApplicationName("DICOM MIX Tools")
    app.setOrganizationName("MedicalSoftware")

    # 创建并显示主控制器
    controller = ApplicationController()
    controller.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
