# -*- coding: utf-8 -*-
"""
ui_main.py
图形界面主模块

负责构建 DICOM MIX Tools 的主窗口，包含：
1. 顶部操作栏（SCP 启停、本地文件夹载入）
2. 左侧源数据区（QTreeView 树形展示患者->序列->影像，支持复选框）
3. 右侧信息区（QTabWidget 双模式：PACS 查询 / 手动输入）
4. 底部输出区（发送到 PACS / 导出到本地）

技术栈：PySide6
"""

import os
import shutil
import logging
import socket
from typing import List, Dict, Optional, Callable

logger = logging.getLogger("ui_main")

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QTreeView, QTableView,
    QTabWidget, QGroupBox, QSplitter, QFileDialog, QMessageBox,
    QCheckBox, QHeaderView, QAbstractItemView, QProgressBar,
    QStatusBar, QToolBar, QFormLayout, QApplication, QSpinBox
)
from PySide6.QtCore import Qt, QThread, Signal, QObject, QAbstractItemModel, QModelIndex, QSortFilterProxyModel, QSettings, QTimer
from PySide6.QtGui import QAction, QStandardItemModel, QStandardItem, QFont, QPixmap

# pydicom 用于缩略图生成
from pydicom import dcmread
import numpy as np

# 导入 DSA 图像查看器
from dicom_viewer import DSAViewerWidget


# ------------------------------------------------------------------------------
# 缩略图生成
# ------------------------------------------------------------------------------

def _generate_series_thumbnail(file_path: str, size: int = 48) -> Optional[QPixmap]:
    """读取 DICOM 文件首帧并生成缩略图。失败时返回 None。"""
    try:
        ds = dcmread(file_path, stop_before_pixels=False)
        arr = ds.pixel_array
        if arr.ndim == 3:
            arr = arr[0]

        # 应用 rescale
        slope = float(getattr(ds, "RescaleSlope", 1))
        intercept = float(getattr(ds, "RescaleIntercept", 0))
        if slope != 1 or intercept != 0:
            arr = arr.astype(np.float32) * slope + intercept

        # 归一化到 0-255
        arr = arr.astype(np.float32)
        pmin, pmax = float(arr.min()), float(arr.max())
        arr = (arr - pmin) / max(pmax - pmin, 1.0) * 255.0
        arr = arr.astype(np.uint8)

        h, w = arr.shape
        buf = np.ascontiguousarray(arr).tobytes()
        qimg = QImage(buf, w, h, w, QImage.Format_Grayscale8)
        pixmap = QPixmap.fromImage(qimg.copy())
        return pixmap.scaled(
            size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
    except Exception:
        return None


# ------------------------------------------------------------------------------
# 全局样式表 — 现代化医疗软件风格
# ------------------------------------------------------------------------------
MAIN_STYLE = """
QMainWindow {
    background-color: #f3f4f6;
}
QGroupBox {
    background-color: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    margin-top: 10px;
    padding-top: 8px;
    font-weight: 600;
    color: #111827;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
}
QPushButton {
    background-color: #2563eb;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 7px 14px;
    font-weight: 500;
    font-size: 13px;
}
QPushButton:hover {
    background-color: #1d4ed8;
}
QPushButton:pressed {
    background-color: #1e40af;
}
QPushButton:disabled {
    background-color: #e5e7eb;
    color: #9ca3af;
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
    background-color: #dc2626;
}
QPushButton#danger:hover {
    background-color: #b91c1c;
}
QPushButton#success {
    background-color: #16a34a;
}
QPushButton#success:hover {
    background-color: #15803d;
}
QLineEdit {
    background-color: #ffffff;
    color: #111827;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
}
QLineEdit:focus {
    border: 1px solid #2563eb;
}
QTreeView, QTableView {
    background-color: #ffffff;
    color: #111827;
    border: 1px solid #e5e7eb;
    border-radius: 6px;
    alternate-background-color: #f9fafb;
    font-size: 13px;
}
QTreeView::item:selected, QTableView::item:selected {
    background-color: #dbeafe;
    color: #1e40af;
}
QTreeView::item:hover, QTableView::item:hover {
    background-color: #eff6ff;
}
QHeaderView::section {
    background-color: #f9fafb;
    color: #374151;
    padding: 6px;
    border: 1px solid #e5e7eb;
    font-weight: 600;
    font-size: 12px;
}
QSlider::groove:horizontal {
    background: #e5e7eb;
    height: 6px;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #2563eb;
    width: 16px;
    height: 16px;
    border-radius: 8px;
    margin: -5px 0;
}
QLabel {
    color: #374151;
    font-size: 13px;
}
QStatusBar {
    background-color: #ffffff;
    color: #6b7280;
    border-top: 1px solid #e5e7eb;
    font-size: 12px;
}
QTabWidget::pane {
    border: none;
    background-color: transparent;
}
QTabBar::tab {
    background-color: #f3f4f6;
    color: #6b7280;
    padding: 8px 16px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 4px;
    font-size: 13px;
}
QTabBar::tab:selected {
    background-color: #2563eb;
    color: white;
    font-weight: 500;
}
QTabBar::tab:hover:!selected {
    background-color: #e5e7eb;
}
QCheckBox {
    color: #374151;
    font-size: 13px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid #d1d5db;
    background-color: #ffffff;
}
QCheckBox::indicator:checked {
    background-color: #2563eb;
    border: 1px solid #2563eb;
}
QSpinBox {
    background-color: #ffffff;
    color: #111827;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 4px;
    font-size: 13px;
}
QProgressBar {
    border: none;
    border-radius: 4px;
    background-color: #e5e7eb;
    text-align: center;
    font-size: 11px;
    color: #374151;
}
QProgressBar::chunk {
    background-color: #2563eb;
    border-radius: 4px;
}
QToolBar {
    background-color: #ffffff;
    border-bottom: 1px solid #e5e7eb;
    padding: 4px;
    spacing: 6px;
}
QSplitter::handle {
    background-color: #e5e7eb;
}
QSplitter::handle:horizontal {
    width: 2px;
}
QMessageBox {
    background-color: #ffffff;
}
"""

# ------------------------------------------------------------------------------
# 自定义信号类（用于跨线程更新 UI）
# ------------------------------------------------------------------------------
class DicomInputSignals(QObject):
    """dicom_input 模块与 UI 之间的通信信号"""
    study_received = Signal(str, str)      # (StudyInstanceUID, 患者姓名)
    scp_status_changed = Signal(bool, str) # (是否运行中, 状态文本)
    local_load_progress = Signal(int, int) # (当前数量, 总数量)
    local_load_finished = Signal(int)      # (成功加载的文件数)
    error_occurred = Signal(str)           # 错误信息


class DicomNetworkSignals(QObject):
    """dicom_network 模块与 UI 之间的通信信号"""
    find_results_ready = Signal(list)      # List[dict] 查询结果
    store_progress = Signal(int, int)      # (当前, 总数)
    store_finished = Signal(int, int)      # (成功数, 总数)
    error_occurred = Signal(str)

    # DSA 相关信号
    dsa_find_results_ready = Signal(list)  # DSA C-FIND 结果
    dsa_move_progress = Signal(int, int)   # DSA C-MOVE 进度
    dsa_move_finished = Signal(int, int)   # DSA C-MOVE 完成


class DicomProcessorSignals(QObject):
    """dicom_processor 模块与 UI 之间的通信信号"""
    process_progress = Signal(int, int)
    process_finished = Signal(int, str)    # (成功数, 输出目录)
    error_occurred = Signal(str)


# ------------------------------------------------------------------------------
# 左侧树形模型数据项（患者 -> 序列 -> 影像文件）
# ------------------------------------------------------------------------------
class StudyTreeItem:
    """树形结构的内部数据节点"""
    def __init__(self, data: Dict, parent: Optional["StudyTreeItem"] = None):
        self.data = data                # 节点数据字典
        self.parent = parent
        self.children: List["StudyTreeItem"] = []
        self.checked_state = Qt.Unchecked  # 默认不勾选，由用户手动选择

    def append_child(self, item: "StudyTreeItem"):
        self.children.append(item)

    def child(self, row: int) -> Optional["StudyTreeItem"]:
        if 0 <= row < len(self.children):
            return self.children[row]
        return None

    def child_count(self) -> int:
        return len(self.children)

    def row(self) -> int:
        if self.parent:
            return self.parent.children.index(self)
        return 0

    def column_count(self) -> int:
        return 3  # 名称 | 类型 | 数量

    def get_display_data(self, column: int) -> str:
        mapping = {
            0: self.data.get("name", ""),
            1: self.data.get("type", ""),
            2: str(self.data.get("count", "")),
        }
        return mapping.get(column, "")


class StudyTreeModel(QAbstractItemModel):
    """
    自定义树形模型，用于左侧 QTreeView 展示：
    患者 (Patient) -> 序列 (Series) -> 影像实例 (Instance)
    支持复选框勾选，以决定哪些序列参与后续拆分处理。
    """
    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.root_item = StudyTreeItem({"name": "Root", "type": "", "count": 0})

    # ---------- QAbstractItemModel 必须实现的方法 ----------

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        parent_item = self.root_item if not parent.isValid() else parent.internalPointer()
        child_item = parent_item.child(row)
        if child_item:
            return self.createIndex(row, column, child_item)
        return QModelIndex()

    def parent(self, index: QModelIndex = QModelIndex()) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        child_item: StudyTreeItem = index.internalPointer()
        parent_item = child_item.parent
        if parent_item == self.root_item:
            return QModelIndex()
        return self.createIndex(parent_item.row(), 0, parent_item)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.column() > 0:
            return 0
        parent_item = self.root_item if not parent.isValid() else parent.internalPointer()
        return parent_item.child_count()

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 3

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        item: StudyTreeItem = index.internalPointer()
        if role == Qt.DisplayRole:
            return item.get_display_data(index.column())
        if role == Qt.CheckStateRole and index.column() == 0:
            return item.checked_state
        if role == Qt.DecorationRole and index.column() == 0:
            thumb = item.data.get("thumbnail")
            if thumb:
                return thumb
        return None

    def setData(self, index: QModelIndex, value, role: int = Qt.EditRole) -> bool:
        if not index.isValid():
            return False
        item: StudyTreeItem = index.internalPointer()
        if role == Qt.CheckStateRole and index.column() == 0:
            item.checked_state = value
            # 级联更新子节点
            self._set_children_check_state(item, value)
            # 级联更新父节点（若所有子节点同态）
            self._update_parent_check_state(item)
            self.dataChanged.emit(QModelIndex(), QModelIndex(), [Qt.CheckStateRole])
            return True
        return False

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.NoItemFlags
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == 0:
            flags |= Qt.ItemIsUserCheckable
        return flags

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            headers = ["名称 / 患者信息", "类型", "影像数"]
            return headers[section] if section < len(headers) else ""
        return None

    # ---------- 辅助方法 ----------

    def _set_children_check_state(self, item: StudyTreeItem, state: Qt.CheckState):
        """递归设置所有子节点的复选状态"""
        for child in item.children:
            child.checked_state = state
            self._set_children_check_state(child, state)

    def _update_parent_check_state(self, item: StudyTreeItem):
        """向上级联更新父节点的复选状态"""
        parent = item.parent
        if not parent or parent == self.root_item:
            return
        states = [c.checked_state for c in parent.children]
        if all(s == Qt.Checked for s in states):
            parent.checked_state = Qt.Checked
        elif all(s == Qt.Unchecked for s in states):
            parent.checked_state = Qt.Unchecked
        else:
            parent.checked_state = Qt.PartiallyChecked
        self._update_parent_check_state(parent)

    def clear(self):
        """清空整棵树"""
        self.beginResetModel()
        self.root_item.children.clear()
        self.endResetModel()

    def add_study(self, study_data: Dict):
        """
        向模型中添加一个检查(Study)节点。
        study_data 格式示例：
        {
            "study_uid": "1.2.840...",
            "patient_name": "张三",
            "patient_id": "123456",
            "series_list": [
                {
                    "series_uid": "1.2.840...",
                    "series_description": "DSA Series 1",
                    "modality": "XA",
                    "instances": ["path1", "path2", ...]
                }, ...
            ]
        }
        """
        self.beginResetModel()
        patient_name = study_data.get("patient_name", "Unknown")
        patient_id = study_data.get("patient_id", "N/A")

        study_item = StudyTreeItem({
            "name": f"{patient_name} ({patient_id})",
            "type": "检查 (Study)",
            "count": sum(len(s.get("instances", [])) for s in study_data.get("series_list", [])),
            "study_uid": study_data.get("study_uid"),
            "patient_name": patient_name,
            "patient_id": patient_id,
        }, self.root_item)
        self.root_item.append_child(study_item)

        for series in study_data.get("series_list", []):
            instances = series.get("instances", [])
            # 为序列生成缩略图（取第一张影像）
            thumbnail = _generate_series_thumbnail(instances[0]) if instances else None
            series_item = StudyTreeItem({
                "name": series.get("series_description", "未命名序列"),
                "type": "序列",
                "modality": series.get('modality', 'OT'),
                "count": len(instances),
                "series_uid": series.get("series_uid"),
                "instances": instances,
                "patient_name": patient_name,
                "patient_id": patient_id,
                "thumbnail": thumbnail,
            }, study_item)
            study_item.append_child(series_item)

            # 添加第三级：Instance 节点
            for idx, fpath in enumerate(instances, start=1):
                # 从文件名提取 SOP UID（文件名格式为 {SOPUID}.dcm）
                sop_uid = os.path.splitext(os.path.basename(fpath))[0]
                instance_item = StudyTreeItem({
                    "name": f"Image {idx}",
                    "type": "影像",
                    "count": "",
                    "file_path": fpath,
                    "instance_uid": sop_uid,
                    "patient_name": patient_name,
                    "patient_id": patient_id,
                }, series_item)
                series_item.append_child(instance_item)

        self.endResetModel()

    def get_checked_series(self) -> List[Dict]:
        """获取所有被勾选（或半勾选）的序列节点数据，用于后续处理。

        返回值中每个序列字典额外包含 "selected_instances" 字段：
        - 如果用户勾选了整个序列，selected_instances = 序列所有文件
        - 如果用户只勾选了部分 Instance，selected_instances = 被勾选的文件
        """
        results = []
        for study in self.root_item.children:
            for series in study.children:
                if series.checked_state == Qt.Checked:
                    # 整个序列被勾选
                    series_data = dict(series.data)
                    series_data["selected_instances"] = list(series_data.get("instances", []))
                    results.append(series_data)
                elif series.checked_state == Qt.PartiallyChecked:
                    # 部分 Instance 被勾选
                    selected = []
                    for inst in series.children:
                        if inst.checked_state == Qt.Checked:
                            fpath = inst.data.get("file_path", "")
                            if fpath:
                                selected.append(fpath)
                    if selected:
                        series_data = dict(series.data)
                        series_data["selected_instances"] = selected
                        results.append(series_data)
        return results


# ------------------------------------------------------------------------------
# PACS 查询结果模型
# ------------------------------------------------------------------------------
class PacsResultModel(QStandardItemModel):
    """用于右侧 Tab1 PACS 查询结果的表格模型"""
    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.setHorizontalHeaderLabels(["患者姓名", "患者ID", "检查号", "检查UID"])

    def add_result(self, result: Dict):
        """添加一行查询结果"""
        row = [
            QStandardItem(result.get("patient_name", "")),
            QStandardItem(result.get("patient_id", "")),
            QStandardItem(result.get("accession_number", "")),
            QStandardItem(result.get("study_instance_uid", "")),
        ]
        for item in row:
            item.setEditable(False)
        self.appendRow(row)

    def get_selected_data(self, row: int) -> Dict:
        """获取指定行的数据字典"""
        return {
            "patient_name": self.item(row, 0).text(),
            "patient_id": self.item(row, 1).text(),
            "accession_number": self.item(row, 2).text(),
            "study_instance_uid": self.item(row, 3).text(),
        }


# ------------------------------------------------------------------------------
# 主窗口
# ------------------------------------------------------------------------------
class MainWindow(QMainWindow):
    """
    DICOM MIX Tools 主窗口

    界面布局从上到下分为四大部分：
    - 顶部工具栏（SCP 控制、本地载入）
    - 中部主体（三栏分割：左侧源数据树、中间 DSA 图像预览、右侧患者信息双模式）
    - 底部操作栏（处理并发送 / 处理并导出）
    - 状态栏
    """

    # 信号：向外部（如 main.py 或业务线程）发出用户操作指令
    request_start_scp = Signal()           # 请求启动 SCP
    request_stop_scp = Signal()            # 请求停止 SCP
    request_load_local = Signal(str)       # 请求加载本地文件夹 (path)
    request_pacs_find = Signal(dict)       # 请求 C-FIND (参数字典)
    request_process_and_store = Signal(list, dict)  # (选中序列, 目标患者信息)
    request_process_and_export = Signal(list, dict, str)  # (选中序列, 目标患者信息, 输出目录)
    network_config_changed = Signal(dict)  # 网络配置变更通知
    request_dsa_find = Signal(dict)        # 请求 DSA C-FIND (参数字典)
    request_dsa_move = Signal(str, str)    # 请求 DSA C-MOVE (study_uid, move_dest_ae)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DICOM MIX Tools - DSA 图像路由与编辑")
        self.resize(1600, 720)

        # 信号中心（供各工作线程回传状态）
        self.input_signals = DicomInputSignals()
        self.network_signals = DicomNetworkSignals()
        self.processor_signals = DicomProcessorSignals()

        # 先初始化 UI 控件，再连接信号（避免引用未创建的属性）
        self._init_ui()
        self._connect_internal_signals()

    # ---------- 初始化界面 ----------

    def _init_ui(self):
        self.setStyleSheet(MAIN_STYLE)
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # ---- 1. 顶部工具栏 ----
        self._init_toolbar()

        # ---- 2. 中部左右分割区 ----
        self._init_central_splitter()

        # ---- 3. 底部操作栏 ----
        self._init_bottom_bar()

        # ---- 4. 状态栏 ----
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪")

    def _init_toolbar(self):
        """顶部工具栏：SCP 启停、本地文件夹载入"""
        toolbar = QToolBar("主工具栏")
        self.addToolBar(toolbar)

        # SCP 启停按钮
        self.btn_scp_toggle = QPushButton("▶ 启动 SCP")
        self.btn_scp_toggle.setCheckable(True)
        self.btn_scp_toggle.setMinimumWidth(100)
        self.btn_scp_toggle.setObjectName("success")
        self.btn_scp_toggle.toggled.connect(self._on_scp_toggle)
        toolbar.addWidget(self.btn_scp_toggle)

        toolbar.addSeparator()

        # 本地载入按钮
        self.btn_load_local = QPushButton("📁 载入本地文件夹")
        self.btn_load_local.setObjectName("secondary")
        self.btn_load_local.clicked.connect(self._on_load_local)
        toolbar.addWidget(self.btn_load_local)

        # 刷新按钮
        self.btn_refresh_tree = QPushButton("🔄 刷新")
        self.btn_refresh_tree.setObjectName("secondary")
        self.btn_refresh_tree.clicked.connect(self._on_refresh_tree)
        toolbar.addWidget(self.btn_refresh_tree)

        # 清空按钮（仅清空列表和 Viewer，不删文件）
        self.btn_clear_tree = QPushButton("🗑 清空列表")
        self.btn_clear_tree.setObjectName("secondary")
        self.btn_clear_tree.clicked.connect(self._on_clear_tree)
        toolbar.addWidget(self.btn_clear_tree)

        # 清除缓存按钮（删除 temp_dicom 文件夹中的所有数据）
        self.btn_clear_cache = QPushButton("⚠ 清除缓存")
        self.btn_clear_cache.setObjectName("danger")
        self.btn_clear_cache.setToolTip("删除临时目录中的所有 DICOM 文件，此操作不可恢复")
        self.btn_clear_cache.clicked.connect(self._on_clear_cache)
        toolbar.addWidget(self.btn_clear_cache)

        toolbar.addSeparator()

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(180)
        self.progress_bar.setValue(0)
        toolbar.addWidget(self.progress_bar)

        toolbar.addSeparator()

        # 临时目录标签
        self.lbl_temp_dir = QLabel(f"Temp: {self._get_temp_dir()}")
        self.lbl_temp_dir.setStyleSheet("color: #9ca3af; font-size: 11px;")
        toolbar.addWidget(self.lbl_temp_dir)

    def _init_central_splitter(self):
        """中部主体：三栏分割面板（左: 源数据树, 中: DSA 预览, 右: 患者信息）"""
        splitter = QSplitter(Qt.Horizontal)

        # -- 左侧：源数据树形区 --
        left_widget = self._create_left_panel()
        splitter.addWidget(left_widget)

        # -- 中间：DSA 图像查看器 --
        center_widget = self._create_viewer_panel()
        splitter.addWidget(center_widget)

        # -- 右侧：患者信息双模式区 --
        right_widget = self._create_right_panel()
        splitter.addWidget(right_widget)

        splitter.setSizes([300, 850, 400])  # 初始宽度比例
        self.centralWidget().layout().addWidget(splitter)

    def _create_left_panel(self) -> QWidget:
        """构建左侧源数据展示面板"""
        group = QGroupBox("源数据 (来自 SCP 或本地载入)")
        layout = QVBoxLayout(group)

        # 树形视图
        self.tree_view = QTreeView()
        self.tree_view.setAlternatingRowColors(True)
        self.tree_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # 增加行高，便于点击和展开
        self.tree_view.setStyleSheet("""
            QTreeView::item {
                padding: 6px 4px;
                min-height: 28px;
            }
            QTreeView::branch {
                padding: 4px;
            }
        """)
        self.tree_model = StudyTreeModel(self)
        self.tree_view.setModel(self.tree_model)
        self.tree_view.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree_view.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree_view.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self.tree_view)

        # 连接树节点选择变化信号，联动中间 DSA 查看器
        self.tree_view.selectionModel().currentChanged.connect(self._on_tree_selection_changed)

        # 左侧底部小工具栏：刷新、清空
        hbox = QHBoxLayout()
        self.btn_refresh_tree = QPushButton("刷新")
        self.btn_refresh_tree.clicked.connect(self._on_refresh_tree)
        hbox.addWidget(self.btn_refresh_tree)

        self.btn_clear_tree = QPushButton("清空列表")
        self.btn_clear_tree.clicked.connect(self._on_clear_tree)
        hbox.addWidget(self.btn_clear_tree)
        hbox.addStretch()
        layout.addLayout(hbox)

        return group

    def _create_viewer_panel(self) -> QWidget:
        """构建中间 DSA 图像预览面板"""
        group = QGroupBox("图像预览 (DSA Viewer)")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        # 嵌入 DSA 多帧查看器组件
        self.dsa_viewer = DSAViewerWidget()
        layout.addWidget(self.dsa_viewer, stretch=1)

        return group

    def _create_right_panel(self) -> QWidget:
        """构建右侧患者信息面板（双模式 TabWidget）"""
        group = QGroupBox("目标患者信息")
        layout = QVBoxLayout(group)

        self.tab_widget = QTabWidget()

        # --- Tab 1: PACS 查询 ---
        self.tab_pacs = self._create_pacs_tab()
        self.tab_widget.addTab(self.tab_pacs, "PACS 查询")

        # --- Tab 2: 手动输入 ---
        self.tab_manual = self._create_manual_tab()
        self.tab_widget.addTab(self.tab_manual, "手动输入")

        # --- Tab 3: 网络配置 ---
        self.tab_network = self._create_network_config_tab()
        self.tab_widget.addTab(self.tab_network, "网络配置")

        # Tab 切换时更新底部目标摘要
        self.tab_widget.currentChanged.connect(self._on_tab_changed)

        layout.addWidget(self.tab_widget)
        return group

    def _create_pacs_tab(self) -> QWidget:
        """PACS 查询 Tab：搜索框 + 表格结果"""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # 搜索条件行
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("患者姓名:"))
        self.edit_find_name = QLineEdit()
        self.edit_find_name.setPlaceholderText("支持模糊查询")
        search_layout.addWidget(self.edit_find_name)

        search_layout.addWidget(QLabel("患者ID:"))
        self.edit_find_id = QLineEdit()
        self.edit_find_id.setPlaceholderText("Patient ID")
        search_layout.addWidget(self.edit_find_id)

        search_layout.addWidget(QLabel("检查号:"))
        self.edit_find_acc = QLineEdit()
        self.edit_find_acc.setPlaceholderText("Accession Number")
        search_layout.addWidget(self.edit_find_acc)

        self.btn_find = QPushButton("查询 PACS")
        self.btn_find.clicked.connect(self._on_pacs_find)
        search_layout.addWidget(self.btn_find)
        layout.addLayout(search_layout)

        # 查询结果表格
        self.table_results = QTableView()
        self.table_results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table_results.setAlternatingRowColors(True)
        self.pacs_result_model = PacsResultModel(self)
        self.table_results.setModel(self.pacs_result_model)
        self.table_results.horizontalHeader().setStretchLastSection(True)
        self.table_results.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table_results)

        # 选中提示
        self.lbl_pacs_selected = QLabel("未选择目标患者")
        self.lbl_pacs_selected.setStyleSheet("color: blue;")
        layout.addWidget(self.lbl_pacs_selected)

        # 当表格选择行变化时更新提示
        self.table_results.selectionModel().currentRowChanged.connect(self._on_pacs_selection_changed)

        return widget

    def _create_manual_tab(self) -> QWidget:
        """手动输入 Tab：患者信息分组表单"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(10)
        layout.setContentsMargins(6, 6, 6, 6)

        # --- 必填项 ---
        g1 = QGroupBox("必填项（带 * 号）")
        f1 = QFormLayout(g1)
        f1.setLabelAlignment(Qt.AlignRight)
        f1.setSpacing(6)

        self.edit_manual_name = QLineEdit()
        self.edit_manual_name.setPlaceholderText("患者姓名")
        self.edit_manual_name.textChanged.connect(self._on_manual_input_changed)
        f1.addRow("患者姓名 *:", self.edit_manual_name)

        self.edit_manual_id = QLineEdit()
        self.edit_manual_id.setPlaceholderText("患者ID")
        self.edit_manual_id.textChanged.connect(self._on_manual_input_changed)
        f1.addRow("患者 ID *:", self.edit_manual_id)

        self.edit_manual_acc = QLineEdit()
        self.edit_manual_acc.setPlaceholderText("检查号")
        self.edit_manual_acc.textChanged.connect(self._on_manual_input_changed)
        f1.addRow("检查号 *:", self.edit_manual_acc)

        layout.addWidget(g1)

        # --- 患者基本信息 ---
        g2 = QGroupBox("患者信息（可选）")
        f2 = QFormLayout(g2)
        f2.setLabelAlignment(Qt.AlignRight)
        f2.setSpacing(6)

        # 性别 + 年龄 放同一行
        sex_age = QHBoxLayout()
        self.edit_manual_sex = QLineEdit()
        self.edit_manual_sex.setPlaceholderText("M / F / O")
        self.edit_manual_sex.setMaximumWidth(80)
        sex_age.addWidget(self.edit_manual_sex)
        sex_age.addWidget(QLabel("年龄:"))
        self.edit_manual_age = QLineEdit()
        self.edit_manual_age.setPlaceholderText("45Y")
        self.edit_manual_age.setMaximumWidth(80)
        sex_age.addWidget(self.edit_manual_age)
        sex_age.addStretch()
        f2.addRow("性别 / 年龄:", sex_age)

        self.edit_manual_birth = QLineEdit()
        self.edit_manual_birth.setPlaceholderText("YYYYMMDD，如 19800101")
        f2.addRow("出生日期:", self.edit_manual_birth)

        self.edit_manual_inpatient_id = QLineEdit()
        self.edit_manual_inpatient_id.setPlaceholderText("住院号")
        f2.addRow("住院号:", self.edit_manual_inpatient_id)

        layout.addWidget(g2)

        # --- 检查信息 ---
        g3 = QGroupBox("检查信息（可选）")
        f3 = QFormLayout(g3)
        f3.setLabelAlignment(Qt.AlignRight)
        f3.setSpacing(6)

        self.edit_manual_series_number = QLineEdit()
        self.edit_manual_series_number.setPlaceholderText("序列号 / 影像号")
        f3.addRow("影像号:", self.edit_manual_series_number)

        self.edit_manual_study_uid = QLineEdit()
        self.edit_manual_study_uid.setPlaceholderText("留空将自动生成")
        f3.addRow("Study UID:", self.edit_manual_study_uid)

        layout.addWidget(g3)

        # 验证提示
        self.lbl_manual_hint = QLabel("")
        self.lbl_manual_hint.setStyleSheet("color: red;")
        layout.addWidget(self.lbl_manual_hint)

        layout.addStretch()
        return widget

    @staticmethod
    def _get_local_ips() -> list:
        """获取本机所有 IPv4 地址。"""
        ips = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ip not in ips and not ip.startswith("127."):
                    ips.append(ip)
        except Exception:
            pass
        if not ips:
            ips.append("127.0.0.1")
        return ips

    def _create_network_config_tab(self) -> QWidget:
        """网络配置 Tab：本机信息、PACS、DSA、SCU/SCP 参数配置"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(10)

        # --- 本机网络信息 ---
        g0 = QGroupBox("本机网络信息")
        f0 = QFormLayout(g0)
        f0.setLabelAlignment(Qt.AlignRight)
        f0.setSpacing(6)

        local_ips = self._get_local_ips()
        self.lbl_local_ip = QLabel(", ".join(local_ips))
        self.lbl_local_ip.setStyleSheet("font-weight: bold; color: #1976D2;")
        f0.addRow("本机 IP:", self.lbl_local_ip)

        self.lbl_local_scp_info = QLabel("")
        f0.addRow("SCP 状态:", self.lbl_local_scp_info)

        layout.addWidget(g0)

        # --- PACS 节点配置 ---
        g1 = QGroupBox("PACS 节点")
        f1 = QFormLayout(g1)
        f1.setLabelAlignment(Qt.AlignRight)
        f1.setSpacing(8)

        self.edit_pacs_ae_title = QLineEdit()
        self.edit_pacs_ae_title.setPlaceholderText("例如: PACS")
        f1.addRow("PACS AE Title:", self.edit_pacs_ae_title)

        self.edit_pacs_host = QLineEdit()
        self.edit_pacs_host.setPlaceholderText("例如: 127.0.0.1")
        f1.addRow("PACS 主机:", self.edit_pacs_host)

        self.spin_pacs_port = QSpinBox()
        self.spin_pacs_port.setRange(1, 65535)
        self.spin_pacs_port.setValue(11112)
        f1.addRow("PACS 端口:", self.spin_pacs_port)

        layout.addWidget(g1)

        # --- DSA 主机/工作站配置 ---
        g_dsa = QGroupBox("DSA 主机/工作站")
        f_dsa = QFormLayout(g_dsa)
        f_dsa.setLabelAlignment(Qt.AlignRight)
        f_dsa.setSpacing(8)

        self.edit_dsa_ae_title = QLineEdit()
        self.edit_dsa_ae_title.setPlaceholderText("例如: DSA")
        f_dsa.addRow("DSA AE Title:", self.edit_dsa_ae_title)

        self.edit_dsa_host = QLineEdit()
        self.edit_dsa_host.setPlaceholderText("例如: 192.168.1.100")
        f_dsa.addRow("DSA 主机 IP:", self.edit_dsa_host)

        self.spin_dsa_port = QSpinBox()
        self.spin_dsa_port.setRange(1, 65535)
        self.spin_dsa_port.setValue(11112)
        f_dsa.addRow("DSA 端口:", self.spin_dsa_port)

        # DSA 查询 + 拉取按钮
        dsa_btn_layout = QHBoxLayout()
        self.btn_dsa_find = QPushButton("查询 DSA")
        self.btn_dsa_find.clicked.connect(self._on_dsa_find)
        dsa_btn_layout.addWidget(self.btn_dsa_find)

        self.btn_dsa_move = QPushButton("从 DSA 拉取")
        self.btn_dsa_move.setObjectName("success")
        self.btn_dsa_move.clicked.connect(self._on_dsa_move)
        self.btn_dsa_move.setEnabled(False)
        dsa_btn_layout.addWidget(self.btn_dsa_move)
        dsa_btn_layout.addStretch()
        f_dsa.addRow(dsa_btn_layout)

        layout.addWidget(g_dsa)

        # --- DSA 查询结果表格 ---
        g_dsa_result = QGroupBox("DSA 查询结果")
        dsa_result_layout = QVBoxLayout(g_dsa_result)

        self.dsa_result_model = QStandardItemModel()
        self.dsa_result_model.setHorizontalHeaderLabels(
            ["患者姓名", "患者ID", "检查号", "检查日期", "检查UID"]
        )
        self.dsa_result_table = QTableView()
        self.dsa_result_table.setModel(self.dsa_result_model)
        self.dsa_result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.dsa_result_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.dsa_result_table.horizontalHeader().setStretchLastSection(True)
        self.dsa_result_table.verticalHeader().setVisible(False)
        self.dsa_result_table.setAlternatingRowColors(True)
        self.dsa_result_table.clicked.connect(self._on_dsa_result_selected)
        dsa_result_layout.addWidget(self.dsa_result_table)

        self.lbl_dsa_status = QLabel("")
        self.lbl_dsa_status.setStyleSheet("color: #666; font-size: 12px;")
        dsa_result_layout.addWidget(self.lbl_dsa_status)

        layout.addWidget(g_dsa_result)

        # --- 本地 SCU 配置 ---
        g2 = QGroupBox("本地 SCU (查询/发送)")
        f2 = QFormLayout(g2)
        f2.setLabelAlignment(Qt.AlignRight)
        f2.setSpacing(8)

        self.edit_scu_ae_title = QLineEdit()
        self.edit_scu_ae_title.setPlaceholderText("例如: MIX_SCU")
        f2.addRow("本机 SCU AE Title:", self.edit_scu_ae_title)

        layout.addWidget(g2)

        # --- 本地 SCP 配置 ---
        g3 = QGroupBox("本地 SCP (接收)")
        f3 = QFormLayout(g3)
        f3.setLabelAlignment(Qt.AlignRight)
        f3.setSpacing(8)

        self.edit_scp_ae_title = QLineEdit()
        self.edit_scp_ae_title.setPlaceholderText("例如: MIX_SCP")
        f3.addRow("本机 SCP AE Title:", self.edit_scp_ae_title)

        self.spin_scp_port = QSpinBox()
        self.spin_scp_port.setRange(1, 65535)
        self.spin_scp_port.setValue(11112)
        f3.addRow("SCP 监听端口:", self.spin_scp_port)

        layout.addWidget(g3)

        # 保存按钮 + 提示
        hbox = QHBoxLayout()
        self.btn_save_network = QPushButton("保存配置")
        self.btn_save_network.setObjectName("success")
        self.btn_save_network.clicked.connect(self._on_save_network_config)
        hbox.addWidget(self.btn_save_network)

        self.btn_reset_network = QPushButton("恢复默认")
        self.btn_reset_network.setObjectName("secondary")
        self.btn_reset_network.clicked.connect(self._on_reset_network_config)
        hbox.addWidget(self.btn_reset_network)
        hbox.addStretch()
        layout.addLayout(hbox)

        self.lbl_network_hint = QLabel("")
        self.lbl_network_hint.setStyleSheet("color: green; font-size: 12px;")
        layout.addWidget(self.lbl_network_hint)

        layout.addStretch()

        # 加载已保存的配置（如果有）
        self._load_network_config()
        return widget

    def _load_network_config(self):
        """从 QSettings 加载网络配置，若不存在则使用 config.py 默认值。"""
        from config import (
            DEFAULT_SCP_AE_TITLE, DEFAULT_SCP_PORT,
            DEFAULT_PACS_AE_TITLE, DEFAULT_PACS_HOST, DEFAULT_PACS_PORT,
            DEFAULT_LOCAL_SCU_AE_TITLE,
            DEFAULT_DSA_AE_TITLE, DEFAULT_DSA_HOST, DEFAULT_DSA_PORT,
        )
        settings = QSettings("MedicalSoftware", "DICOMMIXTools")

        self.edit_pacs_ae_title.setText(
            settings.value("network/pacs_ae_title", DEFAULT_PACS_AE_TITLE)
        )
        self.edit_pacs_host.setText(
            settings.value("network/pacs_host", DEFAULT_PACS_HOST)
        )
        self.spin_pacs_port.setValue(
            int(settings.value("network/pacs_port", DEFAULT_PACS_PORT))
        )
        self.edit_scu_ae_title.setText(
            settings.value("network/scu_ae_title", DEFAULT_LOCAL_SCU_AE_TITLE)
        )
        self.edit_scp_ae_title.setText(
            settings.value("network/scp_ae_title", DEFAULT_SCP_AE_TITLE)
        )
        self.spin_scp_port.setValue(
            int(settings.value("network/scp_port", DEFAULT_SCP_PORT))
        )

        # DSA 配置
        self.edit_dsa_ae_title.setText(
            settings.value("network/dsa_ae_title", DEFAULT_DSA_AE_TITLE)
        )
        self.edit_dsa_host.setText(
            settings.value("network/dsa_host", DEFAULT_DSA_HOST)
        )
        self.spin_dsa_port.setValue(
            int(settings.value("network/dsa_port", DEFAULT_DSA_PORT))
        )

        # 更新 SCP 状态显示
        self._update_scp_status_label()

    def _on_save_network_config(self):
        """保存网络配置到 QSettings 并发射变更信号。"""
        settings = QSettings("MedicalSoftware", "DICOMMIXTools")
        settings.setValue("network/pacs_ae_title", self.edit_pacs_ae_title.text().strip())
        settings.setValue("network/pacs_host", self.edit_pacs_host.text().strip())
        settings.setValue("network/pacs_port", self.spin_pacs_port.value())
        settings.setValue("network/scu_ae_title", self.edit_scu_ae_title.text().strip())
        settings.setValue("network/scp_ae_title", self.edit_scp_ae_title.text().strip())
        settings.setValue("network/scp_port", self.spin_scp_port.value())
        settings.setValue("network/dsa_ae_title", self.edit_dsa_ae_title.text().strip())
        settings.setValue("network/dsa_host", self.edit_dsa_host.text().strip())
        settings.setValue("network/dsa_port", self.spin_dsa_port.value())

        self.lbl_network_hint.setText("配置已保存（重启后生效或立即应用）")
        QTimer.singleShot(3000, lambda: self.lbl_network_hint.setText(""))

        # 发射配置变更信号，供 main.py 重新初始化网络模块
        self.network_config_changed.emit(self.get_network_config())

    def _on_reset_network_config(self):
        """恢复默认配置。"""
        from config import (
            DEFAULT_SCP_AE_TITLE, DEFAULT_SCP_PORT,
            DEFAULT_PACS_AE_TITLE, DEFAULT_PACS_HOST, DEFAULT_PACS_PORT,
            DEFAULT_LOCAL_SCU_AE_TITLE,
            DEFAULT_DSA_AE_TITLE, DEFAULT_DSA_HOST, DEFAULT_DSA_PORT,
        )
        self.edit_pacs_ae_title.setText(DEFAULT_PACS_AE_TITLE)
        self.edit_pacs_host.setText(DEFAULT_PACS_HOST)
        self.spin_pacs_port.setValue(DEFAULT_PACS_PORT)
        self.edit_scu_ae_title.setText(DEFAULT_LOCAL_SCU_AE_TITLE)
        self.edit_scp_ae_title.setText(DEFAULT_SCP_AE_TITLE)
        self.spin_scp_port.setValue(DEFAULT_SCP_PORT)
        self.edit_dsa_ae_title.setText(DEFAULT_DSA_AE_TITLE)
        self.edit_dsa_host.setText(DEFAULT_DSA_HOST)
        self.spin_dsa_port.setValue(DEFAULT_DSA_PORT)
        self._on_save_network_config()

    def get_network_config(self) -> dict:
        """返回当前界面上的网络配置字典。"""
        return {
            "pacs_ae_title": self.edit_pacs_ae_title.text().strip(),
            "pacs_host": self.edit_pacs_host.text().strip(),
            "pacs_port": self.spin_pacs_port.value(),
            "scu_ae_title": self.edit_scu_ae_title.text().strip(),
            "scp_ae_title": self.edit_scp_ae_title.text().strip(),
            "scp_port": self.spin_scp_port.value(),
            "dsa_ae_title": self.edit_dsa_ae_title.text().strip(),
            "dsa_host": self.edit_dsa_host.text().strip(),
            "dsa_port": self.spin_dsa_port.value(),
        }

    def _update_scp_status_label(self):
        """更新 SCP 状态显示标签。"""
        ae = self.edit_scp_ae_title.text().strip()
        port = self.spin_scp_port.value()
        local_ips = self._get_local_ips()
        self.lbl_local_scp_info.setText(
            f"AE Title: {ae}  |  端口: {port}  |  IP: {', '.join(local_ips)}"
        )

    def _on_dsa_find(self):
        """点击"查询 DSA"按钮，向 DSA 工作站发送 C-FIND。"""
        query = {
            "patient_name": "",
            "patient_id": "",
            "accession_number": "",
        }
        self.dsa_result_model.removeRows(0, self.dsa_result_model.rowCount())
        self.lbl_dsa_status.setText("正在查询 DSA 工作站...")
        self.btn_dsa_find.setEnabled(False)
        self.request_dsa_find.emit(query)

    def on_dsa_find_results(self, results: list):
        """接收 DSA C-FIND 结果并填充表格。"""
        self.btn_dsa_find.setEnabled(True)
        self.dsa_result_model.removeRows(0, self.dsa_result_model.rowCount())

        if not results:
            self.lbl_dsa_status.setText("未找到匹配的检查")
            return

        for result in results:
            row = [
                QStandardItem(result.get("patient_name", "")),
                QStandardItem(result.get("patient_id", "")),
                QStandardItem(result.get("accession_number", "")),
                QStandardItem(result.get("study_date", "")),
                QStandardItem(result.get("study_instance_uid", "")),
            ]
            for item in row:
                item.setEditable(False)
            self.dsa_result_model.appendRow(row)

        self.lbl_dsa_status.setText(f"找到 {len(results)} 个检查")
        self.btn_dsa_move.setEnabled(True)

    def _on_dsa_result_selected(self, index: QModelIndex):
        """DSA 结果表格选中行变化。"""
        row = index.row()
        if row >= 0:
            self.btn_dsa_move.setEnabled(True)

    def _on_dsa_move(self):
        """点击"从 DSA 拉取"按钮，发起 C-MOVE 请求。"""
        selected = self.dsa_result_table.selectionModel().selectedRows()
        if not selected:
            QMessageBox.warning(self, "提示", "请先在表格中选择一个检查")
            return

        row = selected[0].row()
        study_uid = self.dsa_result_model.item(row, 4).text()
        if not study_uid:
            QMessageBox.warning(self, "提示", "选中行缺少检查 UID")
            return

        scp_ae = self.edit_scp_ae_title.text().strip()
        self.lbl_dsa_status.setText(f"正在从 DSA 拉取检查 {study_uid}...")
        self.btn_dsa_move.setEnabled(False)
        self.request_dsa_move.emit(study_uid, scp_ae)

    def on_dsa_move_finished(self, success: int, total: int):
        """DSA C-MOVE 完成回调。"""
        self.btn_dsa_move.setEnabled(True)
        self.lbl_dsa_status.setText(f"DSA 拉取完成: 成功 {success}/{total}")
        QMessageBox.information(
            self, "DSA 拉取完成",
            f"从 DSA 工作站拉取完成\n成功: {success} / 总计: {total}"
        )

    def _init_bottom_bar(self):
        """底部操作栏：处理并发送 / 处理并导出"""
        layout = QHBoxLayout()
        layout.setSpacing(12)

        # 目标患者摘要
        self.lbl_target_summary = QLabel("目标患者: [未选择]")
        self.lbl_target_summary.setStyleSheet("font-weight: 600; color: #111827;")
        layout.addWidget(self.lbl_target_summary)

        layout.addStretch()

        # 发送到 PACS 按钮
        self.btn_send_pacs = QPushButton("📤 拆分并发送到 PACS")
        self.btn_send_pacs.setMinimumHeight(38)
        self.btn_send_pacs.setObjectName("success")
        self.btn_send_pacs.clicked.connect(self._on_process_and_send)
        layout.addWidget(self.btn_send_pacs)

        # 导出到本地按钮
        self.btn_export_local = QPushButton("💾 拆分并导出到本地...")
        self.btn_export_local.setMinimumHeight(38)
        self.btn_export_local.setObjectName("secondary")
        self.btn_export_local.clicked.connect(self._on_process_and_export)
        layout.addWidget(self.btn_export_local)

        self.centralWidget().layout().addLayout(layout)

    # ---------- 内部信号连接 ----------

    def _connect_internal_signals(self):
        """连接来自工作线程的信号到 UI 更新槽函数"""
        self.input_signals.study_received.connect(self._on_study_received)
        self.input_signals.scp_status_changed.connect(self._on_scp_status_changed)
        self.input_signals.local_load_progress.connect(self._on_local_load_progress)
        self.input_signals.local_load_finished.connect(self._on_local_load_finished)
        self.input_signals.error_occurred.connect(self._show_error)

        self.network_signals.find_results_ready.connect(self._on_find_results_ready)
        self.network_signals.store_progress.connect(self._on_store_progress)
        self.network_signals.store_finished.connect(self._on_store_finished)
        self.network_signals.error_occurred.connect(self._show_error)

        # DSA 信号
        self.network_signals.dsa_find_results_ready.connect(self.on_dsa_find_results)
        self.network_signals.dsa_move_finished.connect(self.on_dsa_move_finished)

        self.processor_signals.process_progress.connect(self.progress_bar.setValue)
        self.processor_signals.process_finished.connect(self._on_process_finished)
        self.processor_signals.error_occurred.connect(self._show_error)

        # 左侧树勾选状态变化时更新底部目标摘要
        self.tree_model.dataChanged.connect(self._on_tree_check_changed)

    # ---------- 槽函数 / 事件处理 ----------

    def _on_scp_toggle(self, checked: bool):
        """SCP 启动/停止按钮切换"""
        if checked:
            self.request_start_scp.emit()
        else:
            self.request_stop_scp.emit()

    def _on_scp_status_changed(self, running: bool, message: str):
        """SCP 状态变化回调"""
        self.btn_scp_toggle.setChecked(running)
        self.btn_scp_toggle.setText("停止 SCP 接收" if running else "启动 SCP 接收")
        self.status_bar.showMessage(message)

    def _on_load_local(self):
        """弹出文件夹选择对话框，请求加载本地 DICOM 文件夹"""
        directory = QFileDialog.getExistingDirectory(
            self, "选择本地 DICOM 文件夹", "",
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks
        )
        if directory:
            self.status_bar.showMessage(f"正在载入文件夹: {directory} ...")
            self.progress_bar.setValue(0)
            self.request_load_local.emit(directory)

    def _on_local_load_progress(self, current: int, total: int):
        """本地载入进度更新"""
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(current)

    def _on_local_load_finished(self, count: int):
        """本地载入完成回调"""
        self.status_bar.showMessage(f"本地载入完成，共加载 {count} 个 DICOM 文件")
        self.progress_bar.setValue(self.progress_bar.maximum())

    def _on_study_received(self, study_uid: str, patient_name: str):
        """SCP 接收到新 Study 的回调（由外部触发更新树模型）"""
        self.status_bar.showMessage(f"接收到新检查: {patient_name} (UID: {study_uid})")
        # 注意：实际刷新树模型应由外部逻辑调用 add_study，这里仅做状态提示

    def _on_refresh_tree(self):
        """手动刷新左侧树（可由外部业务逻辑实现）"""
        # 占位：通常由 main.py 协调调用 dicom_input 扫描临时目录后重新加载
        pass

    def _on_clear_tree(self):
        """清空左侧树列表并释放 Viewer 缓存（不删除磁盘文件）"""
        self.tree_model.clear()
        if hasattr(self, 'dsa_viewer') and self.dsa_viewer:
            self.dsa_viewer.clear()
        self.status_bar.showMessage("已清空源数据列表")

    def _on_clear_cache(self):
        """
        清除缓存：删除临时目录中的所有 DICOM 文件和子目录。
        操作前弹出确认对话框，防止误删。
        """
        temp_dir = self._get_temp_dir()

        reply = QMessageBox.question(
            self, "确认清除缓存",
            f"即将删除临时目录中的所有数据：\n{temp_dir}\n\n"
            f"此操作不可恢复，是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply != QMessageBox.Yes:
            return

        try:
            # 先清空 UI
            self.tree_model.clear()
            if hasattr(self, 'dsa_viewer') and self.dsa_viewer:
                self.dsa_viewer.clear()

            # 删除临时目录下的所有文件和子目录
            deleted_count = 0
            if os.path.isdir(temp_dir):
                for entry in os.listdir(temp_dir):
                    entry_path = os.path.join(temp_dir, entry)
                    try:
                        if os.path.isfile(entry_path):
                            os.remove(entry_path)
                            deleted_count += 1
                        elif os.path.isdir(entry_path):
                            shutil.rmtree(entry_path)
                            deleted_count += 1
                    except Exception as e:
                        logger.warning(f"删除缓存项失败 {entry_path}: {e}")

            self.status_bar.showMessage(f"缓存已清除，共清理 {deleted_count} 项")
            QMessageBox.information(self, "完成", f"缓存清除完成\n共清理 {deleted_count} 项")
        except Exception as e:
            self._show_error(f"清除缓存失败: {e}")

    def _on_tree_selection_changed(self, current: QModelIndex, previous: QModelIndex):
        """
        树节点选择变化：加载图像到 DSA 查看器，并自动勾选序列（用于拆分）。

        关键修复：当用户点击选择某个序列时，自动将其复选框设为勾选状态，
        避免用户误以为"已选择"但实际上 get_checked_series() 返回空的困惑。
        """
        if not current.isValid():
            return

        item = current.internalPointer()
        if not item or not hasattr(item, 'data'):
            return

        data = item.data
        node_type = data.get("type", "")
        file_list: List[str] = []

        if "影像" in node_type:
            # Instance 节点：只加载单张图像
            fpath = data.get("file_path", "")
            if fpath and os.path.isfile(fpath):
                file_list = [fpath]
                item.checked_state = Qt.Checked
                self.tree_model.dataChanged.emit(current, current, [Qt.CheckStateRole])
        elif "序列" in node_type:
            # 自动勾选该序列（关键：让用户点击即选中）
            item.checked_state = Qt.Checked
            self.tree_model.dataChanged.emit(current, current, [Qt.CheckStateRole])
            file_list = data.get("instances", [])
        elif "检查" in node_type:
            # Study 节点：收集所有子序列文件
            if hasattr(item, 'children'):
                for child in item.children:
                    child_data = getattr(child, 'data', {})
                    file_list.extend(child_data.get("instances", []))
                    # 同时勾选所有子序列
                    child.checked_state = Qt.Checked
                    child_index = self.tree_model.createIndex(child.row(), 0, child)
                    self.tree_model.dataChanged.emit(child_index, child_index, [Qt.CheckStateRole])

        # 自动将当前选中 study 的患者信息填入底部目标摘要（作为默认值）
        self._update_target_from_tree_item(item)

        if file_list:
            self.dsa_viewer.load_series(file_list)
            self.status_bar.showMessage(
                f"已加载 {data.get('name', '')}: {len(file_list)} 个文件, "
                f"预计 {self.dsa_viewer.total_frames} 帧"
            )

    def _update_target_from_tree_item(self, item):
        """从树节点向上追溯 Study，提取患者信息更新底部目标摘要。"""
        # 向上找到 Study 节点
        target = item
        while target and target.parent and target.parent != self.tree_model.root_item:
            target = target.parent
        if not target:
            return
        data = target.data
        name = data.get("patient_name", "")
        pid = data.get("patient_id", "")
        if name and pid:
            self._update_target_summary({
                "patient_name": name,
                "patient_id": pid,
                "accession_number": "",
            })

    def _on_tree_check_changed(self):
        """树形控件复选框状态变化时，根据勾选的序列更新底部目标摘要。"""
        checked = self.tree_model.get_checked_series()
        if checked:
            data = checked[0]
            name = data.get("patient_name", "")
            pid = data.get("patient_id", "")
            if name and pid:
                self._update_target_summary({
                    "patient_name": name,
                    "patient_id": pid,
                    "accession_number": "",
                })
        else:
            self.lbl_target_summary.setText("目标患者: [未选择]")

    def _on_pacs_find(self):
        """点击 PACS 查询按钮"""
        query = {
            "patient_name": self.edit_find_name.text().strip(),
            "patient_id": self.edit_find_id.text().strip(),
            "accession_number": self.edit_find_acc.text().strip(),
        }
        self.status_bar.showMessage("正在查询 PACS ...")
        self.pacs_result_model.clear()
        self.pacs_result_model.setHorizontalHeaderLabels(["患者姓名", "患者ID", "检查号", "检查UID"])
        self.request_pacs_find.emit(query)

    def _on_find_results_ready(self, results: List[Dict]):
        """PACS 查询结果返回"""
        for r in results:
            self.pacs_result_model.add_result(r)
        self.status_bar.showMessage(f"PACS 查询完成，返回 {len(results)} 条记录")

    def _on_pacs_selection_changed(self, current: QModelIndex, previous: QModelIndex):
        """PACS 查询结果表格选中行变化"""
        if current.isValid():
            data = self.pacs_result_model.get_selected_data(current.row())
            self.lbl_pacs_selected.setText(
                f"已选择: {data['patient_name']} | ID: {data['patient_id']} | Acc: {data['accession_number']}"
            )
            self._update_target_summary(data)
        else:
            self.lbl_pacs_selected.setText("未选择目标患者")

    def _get_target_patient_info(self) -> Optional[Dict]:
        """
        获取当前界面选中的目标患者信息（根据当前 Tab）。
        若信息不完整则返回 None，并弹出提示。
        """
        if self.tab_widget.currentIndex() == 0:
            # PACS 查询模式
            index = self.table_results.selectionModel().currentIndex()
            if not index.isValid():
                QMessageBox.warning(self, "提示", "请先从 PACS 查询结果中选择一条患者记录")
                return None
            return self.pacs_result_model.get_selected_data(index.row())
        else:
            # 手动输入模式
            name = self.edit_manual_name.text().strip()
            pid = self.edit_manual_id.text().strip()
            acc = self.edit_manual_acc.text().strip()
            study_uid = self.edit_manual_study_uid.text().strip()

            if not name or not pid or not acc:
                self.lbl_manual_hint.setText("请填写所有必填项（带 * 号）")
                QMessageBox.warning(self, "提示", "患者姓名、患者ID、检查号为必填项")
                return None

            self.lbl_manual_hint.setText("")
            result = {
                "patient_name": name,
                "patient_id": pid,
                "accession_number": acc,
                "study_instance_uid": study_uid,
            }

            # 可选项：仅在非空时加入
            sex = self.edit_manual_sex.text().strip()
            if sex:
                result["patient_sex"] = sex

            birth = self.edit_manual_birth.text().strip()
            if birth:
                result["patient_birth_date"] = birth

            age = self.edit_manual_age.text().strip()
            if age:
                result["patient_age"] = age

            inpatient_id = self.edit_manual_inpatient_id.text().strip()
            if inpatient_id:
                result["inpatient_id"] = inpatient_id

            series_number = self.edit_manual_series_number.text().strip()
            if series_number:
                result["series_number"] = series_number

            return result

    def _update_target_summary(self, data: Dict):
        """更新底部目标患者摘要标签"""
        name = data.get("patient_name", "")
        pid = data.get("patient_id", "")
        acc = data.get("accession_number", "")
        self.lbl_target_summary.setText(f"目标患者: {name} | ID: {pid} | Acc: {acc}")

    def _on_manual_input_changed(self):
        """手动输入框内容变化时实时更新底部摘要。"""
        if self.tab_widget.currentIndex() != 1:
            return
        name = self.edit_manual_name.text().strip()
        pid = self.edit_manual_id.text().strip()
        acc = self.edit_manual_acc.text().strip()
        sex = self.edit_manual_sex.text().strip()
        if name or pid or acc:
            parts = [f"目标患者: {name}", f"ID: {pid}", f"Acc: {acc}"]
            if sex:
                parts.append(f"性别: {sex}")
            self.lbl_target_summary.setText(" | ".join(parts))
        else:
            self.lbl_target_summary.setText("目标患者: [未选择]")

    def _on_tab_changed(self, index: int):
        """Tab 切换时更新底部目标摘要。"""
        if index == 0:
            # PACS 查询模式：根据表格当前选中行更新
            sel = self.table_results.selectionModel().currentIndex()
            if sel.isValid():
                data = self.pacs_result_model.get_selected_data(sel.row())
                self._update_target_summary(data)
            else:
                self.lbl_target_summary.setText("目标患者: [未选择]")
        elif index == 1:
            # 手动输入模式
            self._on_manual_input_changed()
        else:
            # 网络配置 Tab
            self.lbl_target_summary.setText("目标患者: [未选择]")

    def _get_selected_series(self) -> List[Dict]:
        """获取左侧树形控件中被勾选的序列"""
        return self.tree_model.get_checked_series()

    def _on_process_and_send(self):
        """点击：应用拆分并发送到 PACS"""
        target = self._get_target_patient_info()
        if target is None:
            return
        series_list = self._get_selected_series()
        if not series_list:
            QMessageBox.warning(self, "提示", "请先在左侧勾选需要拆分的序列")
            return
        self.status_bar.showMessage("正在处理并发送到 PACS ...")
        self.progress_bar.setValue(0)
        self.request_process_and_store.emit(series_list, target)

    def _on_process_and_export(self):
        """点击：应用拆分并导出到本地"""
        target = self._get_target_patient_info()
        if target is None:
            return
        series_list = self._get_selected_series()
        if not series_list:
            QMessageBox.warning(self, "提示", "请先在左侧勾选需要拆分的序列")
            return

        # 弹出输出目录选择
        output_dir = QFileDialog.getExistingDirectory(
            self, "选择导出目标文件夹", "",
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks
        )
        if not output_dir:
            return

        self.status_bar.showMessage(f"正在处理并导出到: {output_dir} ...")
        self.progress_bar.setValue(0)
        self.request_process_and_export.emit(series_list, target, output_dir)

    def _on_store_progress(self, current: int, total: int):
        """C-STORE 发送进度"""
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(current)
            self.status_bar.showMessage(f"发送进度: {current}/{total}")

    def _on_store_finished(self, success: int, total: int):
        """C-STORE 发送完成"""
        self.status_bar.showMessage(f"发送完成: 成功 {success}/{total}")
        QMessageBox.information(self, "完成", f"成功发送 {success}/{total} 个文件到 PACS")

    def _on_process_finished(self, count: int, output_dir: str):
        """本地导出处理完成"""
        self.status_bar.showMessage(f"导出完成: 共 {count} 个文件 -> {output_dir}")
        QMessageBox.information(self, "完成", f"成功导出 {count} 个文件到:\n{output_dir}")

    def _show_error(self, message: str):
        """通用错误弹窗"""
        QMessageBox.critical(self, "错误", message)
        self.status_bar.showMessage(f"错误: {message}")

    # ---------- 工具方法 ----------

    @staticmethod
    def _get_temp_dir() -> str:
        """获取临时目录路径"""
        base = os.path.join(os.getcwd(), "temp_dicom")
        os.makedirs(base, exist_ok=True)
        return base

    def add_study_to_tree(self, study_data: Dict):
        """
        外部调用：向左侧树添加一个 Study 节点。
        通常由 main.py 在接收到 SCP 数据或本地扫描完成后调用。
        """
        self.tree_model.add_study(study_data)


# ------------------------------------------------------------------------------
# 简易运行入口（单独测试 UI 布局时使用）
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)

    # 可选：设置 Windows 风格
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()

    # 演示：添加一些假数据到树中（仅用于 UI 布局验证）
    demo_study = {
        "study_uid": "1.2.840.113619.2.55.3.604688119.868.1234567890.1",
        "patient_name": "测试患者",
        "patient_id": "P12345",
        "series_list": [
            {
                "series_uid": "1.2.840.113619.2.55.3.604688119.868.1234567890.2",
                "series_description": "DSA Run 1",
                "modality": "XA",
                "instances": ["dcm1.dcm", "dcm2.dcm", "dcm3.dcm"],
            },
            {
                "series_uid": "1.2.840.113619.2.55.3.604688119.868.1234567890.3",
                "series_description": "DSA Run 2",
                "modality": "XA",
                "instances": ["dcm4.dcm", "dcm5.dcm"],
            },
        ]
    }
    window.add_study_to_tree(demo_study)

    sys.exit(app.exec())
