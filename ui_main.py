# -*- coding: utf-8 -*-
"""
ui_main.py
图形界面主模块

负责构建 DICOM MIX Tools 的主窗口，包含：
1. 顶部操作栏（SCP 启停、本地文件夹载入）
2. 左侧源数据区（QTreeView 树形展示患者->序列->影像，支持复选框）
3. 右侧信息区（QTabWidget 双模式：主机查询 / 手动输入）
4. 底部输出区（发送到主机 / 导出到本地）

技术栈：PySide6
"""

import os
import sys
import shutil
import logging
import socket
from typing import List, Dict, Optional, Callable, Tuple

logger = logging.getLogger("ui_main")

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QTreeView, QTableView,
    QTabWidget, QGroupBox, QSplitter, QFileDialog, QMessageBox,
    QCheckBox, QHeaderView, QAbstractItemView, QProgressBar,
    QStatusBar, QToolBar, QFormLayout, QApplication, QSpinBox,
    QDockWidget, QSizePolicy, QScrollArea,
    QToolButton, QMenu, QComboBox, QDialog, QTextBrowser,
    QRadioButton, QTableWidget, QTableWidgetItem,
    QListWidget, QListWidgetItem,
)
from PySide6.QtCore import Qt, QThread, Signal, QObject, QAbstractItemModel, QModelIndex, QSortFilterProxyModel, QSettings, QTimer, QSize
from PySide6.QtGui import QAction, QStandardItemModel, QStandardItem, QFont, QPixmap, QIcon

# Fluent Design 图标
from qfluentwidgets import FluentIcon

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
# ------------------------------------------------------------------------------
# 自定义信号类（用于跨线程更新 UI）
# ------------------------------------------------------------------------------
class DicomInputSignals(QObject):
    """dicom_input 模块与 UI 之间的通信信号"""
    study_received = Signal(str, str)      # (StudyInstanceUID, 患者姓名)
    scp_status_changed = Signal(bool, str) # (是否运行中, 状态文本)
    local_load_progress = Signal(int, int) # (当前数量, 总数量)
    local_load_finished = Signal(int)      # (成功加载的文件数)
    scp_file_received = Signal(int)        # SCP 已接收文件总数
    error_occurred = Signal(str)           # 错误信息


class DicomNetworkSignals(QObject):
    """dicom_network 模块与 UI 之间的通信信号"""
    find_results_ready = Signal(list)      # List[dict] 查询结果
    store_progress = Signal(int, int)      # (当前, 总数)
    store_finished = Signal(int, int)      # (成功数, 总数)
    error_occurred = Signal(str)

    # PACS C-MOVE 信号
    pacs_move_progress = Signal(int, int)
    pacs_move_finished = Signal(int, int)

    # DSA 相关信号
    dsa_find_results_ready = Signal(list)  # DSA C-FIND 结果
    dsa_move_progress = Signal(int, int)   # DSA C-MOVE 进度
    dsa_move_finished = Signal(int, int)   # DSA C-MOVE 完成

    # Worklist 相关信号
    worklist_results_ready = Signal(list)  # Worklist C-FIND 结果


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
    item_checked = Signal(QModelIndex, int)  # 复选框状态变化时通知视图更新选中

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
            # 确保存储统一的 Qt.CheckState 枚举值（PySide6 传入的 value 可能是 int）
            state = Qt.CheckState(value) if isinstance(value, int) else value
            item.checked_state = state
            # 级联更新子节点
            self._set_children_check_state(item, state, index)
            # 级联更新父节点（若所有子节点同态）
            self._update_parent_check_state(item, index)
            # emit 精确范围，避免整树刷新导致折叠或选中丢失
            self.dataChanged.emit(index, index)
            # 通知视图联动更新高亮选中
            state_val = state.value if hasattr(state, 'value') else int(state)
            self.item_checked.emit(index, state_val)
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

    def _set_children_check_state(self, item: StudyTreeItem, state: Qt.CheckState,
                                   parent_index: QModelIndex = QModelIndex()):
        """递归设置所有子节点的复选状态，并为每个子节点发射 dataChanged。"""
        for row, child in enumerate(item.children):
            child.checked_state = state
            child_index = self.index(row, 0, parent_index)
            if child_index.isValid():
                self.dataChanged.emit(child_index, child_index)
                self._set_children_check_state(child, state, child_index)

    def _update_parent_check_state(self, item: StudyTreeItem, item_index: QModelIndex):
        """向上级联更新父节点的复选状态，并发射 dataChanged。"""
        parent = item.parent
        if not parent or parent == self.root_item:
            return
        states = [c.checked_state for c in parent.children]
        if all(s == Qt.CheckState.Checked for s in states):
            parent.checked_state = Qt.CheckState.Checked
        elif all(s == Qt.CheckState.Unchecked for s in states):
            parent.checked_state = Qt.CheckState.Unchecked
        else:
            parent.checked_state = Qt.CheckState.PartiallyChecked
        parent_index = self.parent(item_index)
        if parent_index.isValid():
            self.dataChanged.emit(parent_index, parent_index)
            self._update_parent_check_state(parent, parent_index)

    def clear(self):
        """清空整棵树"""
        self.beginResetModel()
        self.root_item.children.clear()
        self.endResetModel()

    def delete_studies(self, study_items: List["StudyTreeItem"]) -> int:
        """从模型中删除指定的 Study 节点及其子节点，返回删除数量。"""
        if not study_items:
            return 0
        # 去重（按对象 id）
        unique = list({id(item): item for item in study_items}.values())
        self.beginResetModel()
        for item in unique:
            if item in self.root_item.children:
                self.root_item.children.remove(item)
        self.endResetModel()
        return len(unique)

    def delete_items(self, studies: List["StudyTreeItem"], series: List["StudyTreeItem"]) -> Tuple[int, int]:
        """删除指定的 Study 和 Series 节点，返回 (删除的 Study 数, 删除的 Series 数)。
        使用 beginRemoveRows/endRemoveRows 保持视图展开状态。"""
        from collections import defaultdict
        removed_studies = 0
        removed_series = 0

        # --- 删除 Series（按 Study 分组，每组内按行号从大到小删） ---
        series_by_parent: dict = defaultdict(list)
        for s in series:
            if s.parent:
                series_by_parent[s.parent].append(s)

        for parent, children in series_by_parent.items():
            children.sort(key=lambda c: c.row(), reverse=True)
            parent_idx = (
                self.createIndex(parent.row(), 0, parent)
                if parent != self.root_item else QModelIndex()
            )
            for child in children:
                row = child.row()
                self.beginRemoveRows(parent_idx, row, row)
                parent.children.remove(child)
                self.endRemoveRows()
                removed_series += 1

        # --- 删除指定的 Study（按行号从大到小） ---
        studies_to_del = sorted(
            [s for s in studies if s in self.root_item.children],
            key=lambda s: s.row(), reverse=True
        )
        for study in studies_to_del:
            row = study.row()
            self.beginRemoveRows(QModelIndex(), row, row)
            self.root_item.children.remove(study)
            self.endRemoveRows()
            removed_studies += 1

        # --- 清理没有子节点的 Study（按行号从大到小） ---
        empty_studies = sorted(
            [s for s in self.root_item.children if not s.children],
            key=lambda s: s.row(), reverse=True
        )
        for study in empty_studies:
            row = study.row()
            self.beginRemoveRows(QModelIndex(), row, row)
            self.root_item.children.remove(study)
            self.endRemoveRows()
            removed_studies += 1

        return removed_studies, removed_series

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
                if series.checked_state == Qt.CheckState.Checked:
                    # 整个序列被勾选
                    series_data = dict(series.data)
                    series_data["selected_instances"] = list(series_data.get("instances", []))
                    results.append(series_data)
                elif series.checked_state == Qt.CheckState.PartiallyChecked:
                    # 部分 Instance 被勾选
                    selected = []
                    for inst in series.children:
                        if inst.checked_state == Qt.CheckState.Checked:
                            fpath = inst.data.get("file_path", "")
                            if fpath:
                                selected.append(fpath)
                    if selected:
                        series_data = dict(series.data)
                        series_data["selected_instances"] = selected
                        results.append(series_data)
        return results


# ------------------------------------------------------------------------------
# 主机查询结果模型
# ------------------------------------------------------------------------------
class PacsResultModel(QStandardItemModel):
    """用于右侧 Tab1 主机查询结果的表格模型"""
    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.setHorizontalHeaderLabels(["患者姓名", "患者ID", "性别", "年龄", "出生日期", "检查号", "检查UID"])

    def add_result(self, result: Dict):
        """添加一行查询结果"""
        row = [
            QStandardItem(result.get("patient_name", "")),
            QStandardItem(result.get("patient_id", "")),
            QStandardItem(result.get("patient_sex", "")),
            QStandardItem(result.get("patient_age", "")),
            QStandardItem(result.get("patient_birth_date", "")),
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
            "patient_sex": self.item(row, 2).text(),
            "patient_age": self.item(row, 3).text(),
            "patient_birth_date": self.item(row, 4).text(),
            "accession_number": self.item(row, 5).text(),
            "study_instance_uid": self.item(row, 6).text(),
        }


class PacsQueryDialog(QDialog):
    """主机查询弹窗（支持 C-MOVE 拉取）"""

    request_find = Signal(dict)   # 发出查询请求
    request_move = Signal(str, str)  # (study_uid, move_dest_ae)

    def __init__(self, scp_ae_title: str = "MIX_SCP", scp_running: bool = False, parent=None):
        super().__init__(parent)
        self.scp_ae_title = scp_ae_title
        self.scp_running = scp_running
        self.setWindowTitle("查询主机")
        self.setMinimumSize(800, 500)
        self.selected_data = None

        layout = QVBoxLayout(self)

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

        search_layout.addWidget(QLabel("日期:"))
        self.combo_date_filter = QComboBox()
        self.combo_date_filter.addItems(["今天", "最近3天", "最近7天", "最近30天", "全部"])
        self.combo_date_filter.setCurrentIndex(0)
        search_layout.addWidget(self.combo_date_filter)

        self.btn_find = QPushButton("查询")
        self.btn_find.clicked.connect(self._on_find)
        search_layout.addWidget(self.btn_find)
        layout.addLayout(search_layout)

        # 查询结果表格
        self.table_results = QTableView()
        self.table_results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table_results.setAlternatingRowColors(True)
        self.result_model = PacsResultModel(self)
        self.table_results.setModel(self.result_model)
        self.table_results.horizontalHeader().setStretchLastSection(True)
        self.table_results.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table_results)

        # 选中提示
        self.lbl_selected = QLabel("未选择目标患者")
        self.lbl_selected.setStyleSheet("color: blue;")
        layout.addWidget(self.lbl_selected)

        self.table_results.selectionModel().currentRowChanged.connect(self._on_selection_changed)

        # 进度条（拉取时显示）
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(0)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_move = QPushButton("拉取数据")
        self.btn_move.setEnabled(False)
        self.btn_move.setMinimumWidth(90)
        self.btn_move.setMinimumHeight(28)
        self.btn_move.setObjectName("success")
        self.btn_move.clicked.connect(self._on_move)
        btn_layout.addWidget(self.btn_move)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setEnabled(False)
        self.btn_ok.setMinimumWidth(70)
        self.btn_ok.setMinimumHeight(28)
        self.btn_ok.setDefault(True)
        self.btn_ok.setObjectName("success")
        self.btn_ok.clicked.connect(self.accept)
        btn_layout.addWidget(self.btn_ok)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setMinimumWidth(70)
        self.btn_cancel.setMinimumHeight(28)
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

        self._selected_study_uid = ""

    def _on_move(self):
        """拉取选中检查到本机 SCP"""
        if not self._selected_study_uid:
            QMessageBox.warning(self, "提示", "请先在表格中选择一个检查")
            return
        self.btn_move.setEnabled(False)
        self.show_move_progress()
        self.request_move.emit(self._selected_study_uid, self.scp_ae_title)

    def show_move_progress(self):
        """显示进度条"""
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(0)
        self.lbl_selected.setText("正在从主机拉取...")

    def on_move_progress(self, current: int, total: int):
        """拉取进度"""
        self.progress_bar.setVisible(True)
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(current)
        self.lbl_selected.setText(f"正在从主机拉取: {current}/{total}")

    def on_move_finished(self, success: int, total: int):
        """拉取完成"""
        self.progress_bar.setVisible(False)
        self.btn_move.setEnabled(True)
        self.lbl_selected.setText(f"主机拉取完成: 成功 {success}/{total}")
        QMessageBox.information(self, "拉取完成", f"从主机拉取完成\n成功: {success} / 总计: {total}")

    def set_scp_running(self, running: bool, ae_title: str = None):
        """SCP 状态变化时实时更新拉取按钮可用性"""
        self.scp_running = running
        if ae_title:
            self.scp_ae_title = ae_title
        # 拉取按钮的可用性受选中状态和 SCP 状态双重控制
        if self._selected_study_uid and self.scp_running:
            self.btn_move.setEnabled(True)
        else:
            self.btn_move.setEnabled(False)

    def get_state(self) -> dict:
        """获取当前状态（用于关闭后恢复）"""
        results = []
        for row in range(self.result_model.rowCount()):
            results.append(self.result_model.get_selected_data(row))
        return {
            "results": results,
            "patient_name": self.edit_find_name.text(),
            "patient_id": self.edit_find_id.text(),
            "accession_number": self.edit_find_acc.text(),
            "date_filter_idx": self.combo_date_filter.currentIndex(),
            "selected_data": self.selected_data,
            "selected_uid": self._selected_study_uid,
            "status_text": self.lbl_selected.text(),
        }

    def restore_state(self, state: dict):
        """恢复上次的状态"""
        if not state:
            return
        self.edit_find_name.setText(state.get("patient_name", ""))
        self.edit_find_id.setText(state.get("patient_id", ""))
        self.edit_find_acc.setText(state.get("accession_number", ""))
        idx = state.get("date_filter_idx", 0)
        if 0 <= idx < self.combo_date_filter.count():
            self.combo_date_filter.setCurrentIndex(idx)
        # 恢复结果
        self.result_model.clear()
        self.result_model.setHorizontalHeaderLabels(["患者姓名", "患者ID", "性别", "年龄", "出生日期", "检查号", "检查UID"])
        for r in state.get("results", []):
            self.result_model.add_result(r)
        # 恢复选中
        self.selected_data = state.get("selected_data")
        self._selected_study_uid = state.get("selected_uid", "")
        if self.selected_data:
            self.btn_ok.setEnabled(True)
            self.lbl_selected.setText(
                f"已选择: {self.selected_data.get('patient_name', '')} | "
                f"ID: {self.selected_data.get('patient_id', '')} | "
                f"Acc: {self.selected_data.get('accession_number', '')}"
            )
            self.btn_move.setEnabled(True)
        elif self._selected_study_uid:
            self.lbl_selected.setText(state.get("status_text", "未选择目标患者"))
        else:
            self.lbl_selected.setText("未选择目标患者")

    def _on_find(self):
        """点击查询按钮"""
        patient_name = self.edit_find_name.text().strip()
        patient_id = self.edit_find_id.text().strip()
        accession_number = self.edit_find_acc.text().strip()

        date_map = {
            "今天": "TODAY",
            "最近3天": "LAST3DAYS",
            "最近7天": "LAST7DAYS",
            "最近30天": "LAST30DAYS",
            "全部": "",
        }
        query = {
            "patient_name": patient_name,
            "patient_id": patient_id,
            "accession_number": accession_number,
            "study_date_range": date_map.get(self.combo_date_filter.currentText(), ""),
        }
        self.result_model.clear()
        self.result_model.setHorizontalHeaderLabels(["患者姓名", "患者ID", "性别", "年龄", "出生日期", "检查号", "检查UID"])
        self.lbl_selected.setText("未选择目标患者")
        self.btn_ok.setEnabled(False)
        self.selected_data = None
        self.request_find.emit(query)

    def _on_selection_changed(self, current: QModelIndex, previous: QModelIndex):
        """表格选中行变化"""
        if current.isValid():
            data = self.result_model.get_selected_data(current.row())
            self.lbl_selected.setText(
                f"已选择: {data['patient_name']} | ID: {data['patient_id']} | Acc: {data['accession_number']}"
            )
            self.selected_data = data
            self._selected_study_uid = data.get("study_instance_uid", "")
            self.btn_ok.setEnabled(True)
            self.btn_move.setEnabled(True)
        else:
            self.lbl_selected.setText("未选择目标患者")
            self.btn_ok.setEnabled(False)
            self.btn_move.setEnabled(False)
            self.selected_data = None
            self._selected_study_uid = ""

    def on_results_ready(self, results: list):
        """接收查询结果"""
        for r in results:
            self.result_model.add_result(r)


class ExportDialog(QDialog):
    """图像序列导出对话框"""

    request_export = Signal(str, dict)  # (format, params)

    def __init__(self, total_frames: int, frame_size: tuple, fps: int = 15, preset_format: str = None, parent=None):
        super().__init__(parent)
        self._total_frames = total_frames
        self._frame_size = frame_size
        self._preset_format = preset_format  # 'mp4', 'png', or None

        # 根据预设格式设置窗口标题
        if preset_format == "mp4":
            self.setWindowTitle("导出 MP4 视频")
        elif preset_format == "png":
            self.setWindowTitle("导出 PNG 图片序列")
        else:
            self.setWindowTitle("导出图像序列")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # 信息
        info = QLabel(f"帧数: {total_frames}  |  尺寸: {frame_size[0]}x{frame_size[1]}")
        info.setStyleSheet("color: #6b7280; font-size: 12px;")
        layout.addWidget(info)

        # 格式选择（仅通用模式显示）
        self.g_format = QGroupBox("导出格式")
        fmt_layout = QHBoxLayout(self.g_format)
        self.radio_mp4 = QRadioButton("MP4 视频")
        self.radio_mp4.setChecked(True)
        self.radio_png = QRadioButton("PNG 图片序列")
        fmt_layout.addWidget(self.radio_mp4)
        fmt_layout.addWidget(self.radio_png)
        layout.addWidget(self.g_format)

        # 如果有预设格式，隐藏格式选择组并锁定内部状态
        if preset_format == "mp4":
            self.g_format.setVisible(False)
            self.radio_mp4.setChecked(True)
            self.radio_png.setEnabled(False)
        elif preset_format == "png":
            self.g_format.setVisible(False)
            self.radio_png.setChecked(True)
            self.radio_mp4.setEnabled(False)

        # MP4 选项
        self.mp4_group = QGroupBox("MP4 设置")
        mp4_form = QFormLayout(self.mp4_group)
        mp4_form.setLabelAlignment(Qt.AlignRight)
        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(1, 60)
        self.spin_fps.setValue(fps)
        self.spin_fps.installEventFilter(self)
        self.spin_fps.setFocusPolicy(Qt.StrongFocus)
        mp4_form.addRow("帧率 (FPS):", self.spin_fps)
        self.edit_mp4_name = QLineEdit("output")
        self.edit_mp4_name.setPlaceholderText("输出文件名（不含扩展名）")
        mp4_form.addRow("文件名:", self.edit_mp4_name)
        layout.addWidget(self.mp4_group)

        # PNG 选项
        self.png_group = QGroupBox("PNG 设置")
        png_form = QFormLayout(self.png_group)
        png_form.setLabelAlignment(Qt.AlignRight)
        self.edit_prefix = QLineEdit("frame_")
        png_form.addRow("文件名前缀:", self.edit_prefix)
        # 间隔步长
        self.spin_step = QSpinBox()
        self.spin_step.setRange(1, total_frames)
        self.spin_step.setValue(1)
        self.spin_step.setToolTip("每隔 N 帧导出一张，1 表示全部导出")
        self.spin_step.installEventFilter(self)
        self.spin_step.setFocusPolicy(Qt.StrongFocus)
        png_form.addRow("间隔帧数:", self.spin_step)
        layout.addWidget(self.png_group)

        # 初始状态：根据选中格式显示/隐藏
        self.radio_mp4.toggled.connect(self._on_format_changed)
        self._on_format_changed()

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, total_frames)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #6b7280; font-size: 12px;")
        layout.addWidget(self.lbl_status)

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_export = QPushButton("开始导出")
        self.btn_export.setObjectName("success")
        self.btn_export.clicked.connect(self._on_export)
        btn_layout.addWidget(self.btn_export)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.Wheel and obj in (self.spin_fps, self.spin_step):
            event.ignore()
            return True
        return super().eventFilter(obj, event)

    def _on_format_changed(self):
        is_mp4 = self.radio_mp4.isChecked()
        self.mp4_group.setVisible(is_mp4)
        self.png_group.setVisible(not is_mp4)

    def _on_export(self):
        fmt = "mp4" if self.radio_mp4.isChecked() else "png"
        params = {}
        if fmt == "mp4":
            params["fps"] = self.spin_fps.value()
        else:
            params["prefix"] = self.edit_prefix.text().strip() or "frame_"
            params["step"] = self.spin_step.value()

        # 弹出保存路径选择
        if fmt == "mp4":
            default_name = self.edit_mp4_name.text().strip() or "output"
            if not default_name.lower().endswith(".mp4"):
                default_name += ".mp4"
            path, _ = QFileDialog.getSaveFileName(
                self, "保存 MP4 视频", default_name,
                "MP4 视频 (*.mp4)"
            )
        else:
            path = QFileDialog.getExistingDirectory(
                self, "选择 PNG 输出文件夹", "",
                QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks
            )
        if not path:
            return

        params["output_path"] = path
        self.btn_export.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.lbl_status.setText("正在导出...")
        self.request_export.emit(fmt, params)

    def on_progress(self, current: int, total: int):
        self.progress_bar.setValue(current)
        self.lbl_status.setText(f"导出中... {current}/{total}")

    def on_finished(self, message: str):
        self.lbl_status.setText(message)
        self.progress_bar.setValue(self._total_frames)
        self.btn_export.setEnabled(True)
        self.btn_cancel.setEnabled(True)
        QMessageBox.information(self, "导出完成", message)
        self.accept()

    def on_error(self, message: str):
        self.lbl_status.setText(f"错误: {message}")
        self.btn_export.setEnabled(True)
        self.btn_cancel.setEnabled(True)
        QMessageBox.critical(self, "导出失败", message)


class NormalizerDialog(QDialog):
    """DICOM 异构断层数据归一化对话框"""

    request_normalize = Signal(list, str, str)  # (file_paths, output_dir, target_manufacturer)

    def __init__(self, file_paths: list, patient_name: str = "", series_desc: str = "", parent=None):
        super().__init__(parent)
        self._file_paths = file_paths
        self.setWindowTitle("DICOM 异构数据归一化")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # 说明文字
        info = QLabel(
            "将来自不同品牌 DSA/CT 设备的三维断层序列归一化为标准 CT Image Storage 格式，"
            "抹除私有协议壁垒并重构空间参数，确保兼容 GE/西门子/飞利浦等第三方工作站。"
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #6b7280; font-size: 12px;")
        layout.addWidget(info)

        # 选中的序列信息
        info_group = QGroupBox("选中的数据源")
        info_layout = QFormLayout(info_group)
        info_layout.setLabelAlignment(Qt.AlignRight)
        info_layout.addRow("患者姓名:", QLabel(patient_name or "未知"))
        info_layout.addRow("序列描述:", QLabel(series_desc or "未知"))
        info_layout.addRow("影像数量:", QLabel(f"{len(file_paths)} 张"))
        layout.addWidget(info_group)

        # 输出目录
        output_layout = QHBoxLayout()
        self.edit_output_dir = QLineEdit()
        self.edit_output_dir.setPlaceholderText("归一化后的输出文件夹...")
        btn_output_browse = QPushButton("浏览...")
        btn_output_browse.setObjectName("secondary")
        btn_output_browse.clicked.connect(self._on_browse_output)
        output_layout.addWidget(QLabel("输出目录:"))
        output_layout.addWidget(self.edit_output_dir, stretch=1)
        output_layout.addWidget(btn_output_browse)
        layout.addLayout(output_layout)

        # 目标厂商
        target_layout = QHBoxLayout()
        self.combo_target = QComboBox()
        self.combo_target.addItems(["GE", "SIEMENS", "PHILIPS"])
        self.combo_target.setCurrentText("GE")
        target_layout.addWidget(QLabel("目标兼容厂商:"))
        target_layout.addWidget(self.combo_target)
        target_layout.addStretch()
        layout.addLayout(target_layout)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.lbl_status = QLabel("就绪")
        self.lbl_status.setStyleSheet("color: #6b7280; font-size: 12px;")
        layout.addWidget(self.lbl_status)

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_start = QPushButton("开始归一化")
        self.btn_start.setObjectName("success")
        self.btn_start.clicked.connect(self._on_start)
        btn_layout.addWidget(self.btn_start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

    def _on_browse_output(self):
        path = QFileDialog.getExistingDirectory(
            self, "选择输出目录", "",
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks
        )
        if path:
            self.edit_output_dir.setText(path)

    def _on_start(self):
        output_dir = self.edit_output_dir.text().strip()
        target = self.combo_target.currentText()

        if not output_dir:
            QMessageBox.warning(self, "提示", "请选择输出目录")
            return

        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.lbl_status.setText("正在归一化...")
        self.request_normalize.emit(self._file_paths, output_dir, target)

    def on_progress(self, message: str):
        """接收进度消息（文本形式，因为归一化步骤离散）"""
        self.lbl_status.setText(message)

    def on_finished(self, summary: dict):
        slice_count = summary.get("slice_count", 0)
        spacing = summary.get("slice_spacing_mm", 0.0)
        output_dir = summary.get("output_dir", "")
        msg = (
            f"归一化完成！\n\n"
            f"输出切片数: {slice_count}\n"
            f"层间距: {spacing:.2f} mm\n"
            f"输出目录: {output_dir}"
        )
        self.lbl_status.setText("归一化完成")
        self.progress_bar.setValue(100)
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(True)
        QMessageBox.information(self, "完成", msg)
        self.accept()

    def on_error(self, message: str):
        self.lbl_status.setText(f"错误: {message}")
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(True)
        QMessageBox.critical(self, "归一化失败", message)


class PluginManagerDialog(QDialog):
    """插件管理中心对话框 (V4.0)

    提供插件的浏览、安装、卸载和激活功能。
    插件列表从远程 GitHub 仓库获取，已安装插件显示在本地。
    """

    # 信号：通知外部加载/激活插件
    request_install_plugin = Signal(str)    # 插件名称
    request_uninstall_plugin = Signal(str)  # 插件名称
    request_activate_plugin = Signal(str)   # 插件名称

    def __init__(self, plugin_manager, parent=None):
        super().__init__(parent)
        self._pm = plugin_manager
        self.setWindowTitle("插件中心")
        self.setMinimumSize(640, 480)
        self.resize(720, 520)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # 顶部说明
        info = QLabel(
            "插件扩展了 DICOM MIX Tools 的核心功能。\n"
            "标准安装包不包含插件，请从远程仓库下载安装。"
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #6b7280; font-size: 12px;")
        layout.addWidget(info)

        # 主体分割
        hsplit = QHBoxLayout()

        # 左侧：插件列表
        left = QVBoxLayout()
        left.addWidget(QLabel("插件列表"))
        self.list_plugins = QListWidget()
        self.list_plugins.setMaximumWidth(260)
        self.list_plugins.currentItemChanged.connect(self._on_plugin_selected)
        left.addWidget(self.list_plugins)

        # 刷新按钮
        self.btn_refresh = QPushButton("刷新列表")
        self.btn_refresh.setObjectName("secondary")
        self.btn_refresh.clicked.connect(self._refresh_manifest)
        left.addWidget(self.btn_refresh)
        hsplit.addLayout(left)

        # 右侧：详情面板
        right = QVBoxLayout()
        right.addWidget(QLabel("插件详情"))

        self.detail_group = QGroupBox()
        detail_layout = QFormLayout(self.detail_group)
        detail_layout.setLabelAlignment(Qt.AlignRight)
        self.lbl_detail_name = QLabel("-")
        self.lbl_detail_version = QLabel("-")
        self.lbl_detail_desc = QLabel("-")
        self.lbl_detail_desc.setWordWrap(True)
        self.lbl_detail_author = QLabel("-")
        self.lbl_detail_deps = QLabel("-")
        self.lbl_detail_status = QLabel("-")

        detail_layout.addRow("名称:", self.lbl_detail_name)
        detail_layout.addRow("版本:", self.lbl_detail_version)
        detail_layout.addRow("描述:", self.lbl_detail_desc)
        detail_layout.addRow("作者:", self.lbl_detail_author)
        detail_layout.addRow("依赖:", self.lbl_detail_deps)
        detail_layout.addRow("状态:", self.lbl_detail_status)
        right.addWidget(self.detail_group)

        # 操作按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_install = QPushButton("安装")
        self.btn_install.setObjectName("success")
        self.btn_install.clicked.connect(self._on_install)
        btn_layout.addWidget(self.btn_install)

        self.btn_uninstall = QPushButton("卸载")
        self.btn_uninstall.setObjectName("danger")
        self.btn_uninstall.clicked.connect(self._on_uninstall)
        btn_layout.addWidget(self.btn_uninstall)

        self.btn_activate = QPushButton("激活")
        self.btn_activate.setObjectName("success")
        self.btn_activate.clicked.connect(self._on_activate)
        btn_layout.addWidget(self.btn_activate)
        right.addLayout(btn_layout)

        # 进度/状态
        self.lbl_plugin_status = QLabel("就绪")
        self.lbl_plugin_status.setStyleSheet("color: #6b7280; font-size: 12px;")
        right.addWidget(self.lbl_plugin_status)

        right.addStretch()
        hsplit.addLayout(right, stretch=1)
        layout.addLayout(hsplit)

        # 底部关闭按钮
        bottom = QHBoxLayout()
        bottom.addStretch()
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        bottom.addWidget(btn_close)
        layout.addLayout(bottom)

        # 内部状态
        self._manifest: List[Dict] = []
        self._current_plugin: Optional[str] = None

        # 初始化：先加载本地已安装，再尝试获取远程清单
        self._load_local_plugins()

    # ------------------------------------------------------------------
    # 列表填充
    # ------------------------------------------------------------------

    def _load_local_plugins(self):
        """加载本地已安装插件到列表。"""
        self.list_plugins.clear()
        installed = self._pm.list_installed()
        for name in installed:
            info = self._pm.get_local_info(name) or {}
            item = QListWidgetItem(f"{name}  (已安装)")
            item.setData(Qt.UserRole, {"name": name, "installed": True, "remote": False})
            self.list_plugins.addItem(item)
        if installed:
            self.lbl_plugin_status.setText(f"本地已安装 {len(installed)} 个插件")
        else:
            self.lbl_plugin_status.setText("本地未安装任何插件，点击「刷新列表」从远程获取")

    def _refresh_manifest(self):
        """从远程仓库获取插件清单并刷新列表。"""
        self.btn_refresh.setEnabled(False)
        self.lbl_plugin_status.setText("正在从 GitHub 获取插件清单...")
        # 使用 QTimer 模拟异步，避免阻塞 UI（实际 fetch_manifest 是网络请求）
        from PySide6.QtCore import QTimer
        QTimer.singleShot(100, self._do_refresh)

    def _do_refresh(self):
        manifest = self._pm.fetch_manifest()
        self._manifest = manifest
        self.list_plugins.clear()

        installed_set = set(self._pm.list_installed())

        for p in manifest:
            name = p.get("name", "")
            version = p.get("version", "")
            is_installed = name in installed_set
            if is_installed:
                display = f"{name}  (已安装 v{version})"
            else:
                display = f"{name}  (未安装 v{version})"
            item = QListWidgetItem(display)
            item.setData(Qt.UserRole, {
                "name": name,
                "installed": is_installed,
                "remote": True,
                "meta": p,
            })
            self.list_plugins.addItem(item)

        self.lbl_plugin_status.setText(f"远程清单: {len(manifest)} 个插件")
        self.btn_refresh.setEnabled(True)

    def _on_plugin_selected(self, current, previous):
        """列表选中项变化时更新详情面板。"""
        if not current:
            self._current_plugin = None
            self._clear_detail()
            return

        data = current.data(Qt.UserRole)
        name = data.get("name", "")
        self._current_plugin = name
        is_installed = data.get("installed", False)
        meta = data.get("meta", {})

        # 优先使用远程元数据，否则使用本地元数据
        if meta:
            info = meta
        else:
            info = self._pm.get_local_info(name) or {}

        self.lbl_detail_name.setText(info.get("name", name))
        self.lbl_detail_version.setText(info.get("version", "-"))
        self.lbl_detail_desc.setText(info.get("description", "-"))
        self.lbl_detail_author.setText(info.get("author", "-"))
        deps = info.get("dependencies", [])
        self.lbl_detail_deps.setText(", ".join(deps) if deps else "无")

        if is_installed:
            status_text = "已安装"
            if self._pm.is_loaded(name):
                status_text += " | 已加载"
        else:
            status_text = "未安装"
        self.lbl_detail_status.setText(status_text)

        # 更新按钮状态
        self.btn_install.setVisible(not is_installed)
        self.btn_install.setEnabled(not is_installed)
        self.btn_uninstall.setVisible(is_installed)
        self.btn_uninstall.setEnabled(is_installed)
        self.btn_activate.setVisible(is_installed)
        self.btn_activate.setEnabled(is_installed)

    def _clear_detail(self):
        for lbl in (self.lbl_detail_name, self.lbl_detail_version,
                    self.lbl_detail_desc, self.lbl_detail_author,
                    self.lbl_detail_deps, self.lbl_detail_status):
            lbl.setText("-")
        self.btn_install.setVisible(True)
        self.btn_install.setEnabled(False)
        self.btn_uninstall.setVisible(False)
        self.btn_activate.setVisible(False)

    # ------------------------------------------------------------------
    # 操作按钮
    # ------------------------------------------------------------------

    def _on_install(self):
        if not self._current_plugin:
            return
        self.btn_install.setEnabled(False)
        self.lbl_plugin_status.setText(f"正在安装 {self._current_plugin}...")
        # 使用信号让外部（main.py）处理实际安装（避免在 UI 线程做网络IO）
        self.request_install_plugin.emit(self._current_plugin)

    def _on_uninstall(self):
        if not self._current_plugin:
            return
        reply = QMessageBox.question(
            self, "确认卸载",
            f"确定要卸载插件 {self._current_plugin} 吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        self.request_uninstall_plugin.emit(self._current_plugin)
        self.lbl_plugin_status.setText(f"已卸载 {self._current_plugin}")
        self._refresh_manifest()

    def _on_activate(self):
        if not self._current_plugin:
            return
        self.request_activate_plugin.emit(self._current_plugin)
        self.lbl_plugin_status.setText(f"已激活 {self._current_plugin}")
        self.accept()

    def on_install_finished(self, name: str, success: bool):
        """由外部调用，通知安装结果。"""
        if success:
            self.lbl_plugin_status.setText(f"插件 {name} 安装成功")
            self._refresh_manifest()
        else:
            self.lbl_plugin_status.setText(f"插件 {name} 安装失败")
            self.btn_install.setEnabled(True)


class DicomTagEditorDialog(QDialog):
    """DICOM 标签编辑器对话框"""

    def __init__(self, file_path: str, parent=None):
        super().__init__(parent)
        self.file_path = file_path
        self.setWindowTitle(f"DICOM 标签编辑器 - {os.path.basename(file_path)}")
        self.setMinimumSize(700, 600)
        self.resize(800, 700)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        # 文件路径提示
        lbl_path = QLabel(f"文件: {file_path}")
        lbl_path.setStyleSheet("color: #6b7280; font-size: 11px;")
        lbl_path.setWordWrap(True)
        layout.addWidget(lbl_path)

        # 标签表格
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["标签", "名称", "值", "VR"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet(
            "QTableWidget { font-size: 13px; }"
            "QTableWidget::item { padding: 2px; }"
            "QTableWidget QLineEdit { min-height: 24px; padding: 2px; }"
        )
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setColumnWidth(0, 90)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(3, 50)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self.table)

        # 覆盖原始文件选项
        self.chk_overwrite = QCheckBox("覆盖原始文件（勾选后将直接修改源文件，请谨慎操作）")
        self.chk_overwrite.setStyleSheet("color: #b91c1c; font-size: 12px;")
        layout.addWidget(self.chk_overwrite)

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_save = QPushButton("保存修改")
        self.btn_save.setObjectName("success")
        self.btn_save.clicked.connect(self._on_save)
        btn_layout.addWidget(self.btn_save)
        self.btn_close = QPushButton("关闭")
        self.btn_close.setObjectName("secondary")
        self.btn_close.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_close)
        layout.addLayout(btn_layout)

        self._load_tags()

    def _load_tags(self):
        """加载 DICOM 标签到表格（不加载像素数据，避免内存占用）"""
        try:
            ds = dcmread(self.file_path, stop_before_pixels=True)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"无法读取 DICOM 文件:\n{e}")
            return

        self.table.setRowCount(0)
        row = 0
        for elem in ds:
            if elem.tag.is_private or elem.tag == (0x7FE0, 0x0010):
                continue
            if elem.VR == "SQ":
                continue
            self.table.insertRow(row)
            # Tag hex
            tag_item = QTableWidgetItem(str(elem.tag))
            tag_item.setFlags(tag_item.flags() & ~Qt.ItemIsEditable)
            tag_item.setData(Qt.UserRole, elem.tag)
            self.table.setItem(row, 0, tag_item)
            # Name
            name_item = QTableWidgetItem(elem.keyword or "")
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 1, name_item)
            # Value
            val_str = str(elem.value) if elem.value is not None else ""
            val_item = QTableWidgetItem(val_str)
            self.table.setItem(row, 2, val_item)
            # VR
            vr_item = QTableWidgetItem(elem.VR or "")
            vr_item.setFlags(vr_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 3, vr_item)
            row += 1

        self.table.sortItems(0, Qt.AscendingOrder)

    def _on_save(self):
        """保存修改"""
        try:
            ds = dcmread(self.file_path, stop_before_pixels=True)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"无法读取 DICOM 文件:\n{e}")
            return

        modified = 0
        errors = []
        for row in range(self.table.rowCount()):
            tag_item = self.table.item(row, 0)
            val_item = self.table.item(row, 2)
            if tag_item is None or val_item is None:
                continue
            tag = tag_item.data(Qt.UserRole)
            new_val = val_item.text()
            try:
                elem = ds[tag]
                old_val = str(elem.value) if elem.value is not None else ""
                if new_val != old_val:
                    try:
                        if elem.VR in ("US", "SS", "UL", "SL"):
                            elem.value = int(new_val)
                        elif elem.VR in ("FL", "FD"):
                            elem.value = float(new_val)
                        elif elem.VR == "IS":
                            elem.value = int(new_val)
                        elif elem.VR == "DS":
                            if "." in new_val:
                                elem.value = float(new_val)
                            else:
                                elem.value = int(new_val)
                        else:
                            elem.value = new_val
                        modified += 1
                    except Exception as e2:
                        errors.append(f"{tag}: {e2}")
            except KeyError:
                continue

        if modified == 0 and not errors:
            QMessageBox.information(self, "提示", "没有需要保存的修改")
            return

        if errors:
            reply = QMessageBox.question(
                self, "部分字段无法修改",
                f"以下 {len(errors)} 个字段修改失败:\n" + "\n".join(errors[:5]) + "\n\n是否继续保存其他修改？",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return

        # 保存文件
        output_path = self.file_path if self.chk_overwrite.isChecked() else None
        if not output_path:
            output_path, _ = QFileDialog.getSaveFileName(
                self, "保存 DICOM 文件", self.file_path,
                "DICOM 文件 (*.dcm);;所有文件 (*.*)"
            )
        if not output_path:
            return

        try:
            ds.save_as(output_path)
            QMessageBox.information(self, "保存成功", f"已保存 {modified} 处修改到:\n{output_path}")
            if output_path == self.file_path:
                self._load_tags()
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"写入文件时出错:\n{e}")


class DsaNodeEditDialog(QDialog):
    """DSA 节点编辑弹窗（添加/修改）"""

    def __init__(self, node: Optional[dict] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑 DSA 节点" if node else "添加 DSA 节点")
        self.setMinimumWidth(360)
        self._node = node or {}

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        f = QFormLayout()
        f.setLabelAlignment(Qt.AlignRight)
        f.setSpacing(8)

        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("例如: DSA-1")
        f.addRow("节点名称:", self.edit_name)

        self.edit_ae_title = QLineEdit()
        self.edit_ae_title.setPlaceholderText("例如: DSA")
        f.addRow("AE Title:", self.edit_ae_title)

        self.edit_host = QLineEdit()
        self.edit_host.setPlaceholderText("例如: 192.168.1.100")
        f.addRow("主机 IP:", self.edit_host)

        self.spin_port = QSpinBox()
        self.spin_port.setRange(1, 65535)
        self.spin_port.setValue(11112)
        self.spin_port.installEventFilter(self)
        self.spin_port.setFocusPolicy(Qt.StrongFocus)
        f.addRow("端口:", self.spin_port)

        layout.addLayout(f)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setObjectName("success")
        self.btn_ok.clicked.connect(self._on_ok)
        btn_layout.addWidget(self.btn_ok)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

        # 填充已有值
        if node:
            self.edit_name.setText(node.get("name", ""))
            self.edit_ae_title.setText(node.get("ae_title", ""))
            self.edit_host.setText(node.get("host", ""))
            self.spin_port.setValue(node.get("port", 11112))

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.Wheel and obj is self.spin_port:
            event.ignore()
            return True
        return super().eventFilter(obj, event)

    def _on_ok(self):
        name = self.edit_name.text().strip()
        ae = self.edit_ae_title.text().strip()
        host = self.edit_host.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "节点名称不能为空")
            return
        if not ae:
            QMessageBox.warning(self, "提示", "AE Title 不能为空")
            return
        if not host:
            QMessageBox.warning(self, "提示", "主机 IP 不能为空")
            return
        self._node = {
            "name": name,
            "ae_title": ae,
            "host": host,
            "port": self.spin_port.value(),
        }
        self.accept()

    def get_node(self) -> dict:
        return self._node


class DsaQueryDialog(QDialog):
    """DSA 查询弹窗（支持多节点选择）"""

    request_find = Signal(dict, int)     # (查询参数, dsa_index)
    request_move = Signal(str, str, int)  # (study_uid, dest_ae, dsa_index)

    def __init__(self, dsa_nodes: List[dict], scp_ae_title: str = "MIX_SCP", scp_running: bool = False, parent=None):
        super().__init__(parent)
        self.dsa_nodes = dsa_nodes or []
        self.scp_ae_title = scp_ae_title
        self.scp_running = scp_running
        self.setWindowTitle("查询 DSA")
        self.setMinimumSize(800, 500)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # SCP 状态提示
        self.lbl_scp_status = QLabel()
        self.lbl_scp_status.setAlignment(Qt.AlignCenter)
        if not self.scp_running:
            self.lbl_scp_status.setText("⚠️ SCP 服务未启动，无法拉取图像。请先启动 SCP 接收端。")
            self.lbl_scp_status.setStyleSheet(
                "background-color: #fef3c7; color: #92400e; padding: 8px 12px;"
                "border-radius: 6px; font-size: 13px; font-weight: 500;"
            )
        else:
            self.lbl_scp_status.setText(f"✅ SCP 运行中 ({self.scp_ae_title})，可以拉取图像")
            self.lbl_scp_status.setStyleSheet(
                "background-color: #fef3c7; color: #92400e; padding: 8px 12px;"
                "border-radius: 6px; font-size: 13px; font-weight: 500;"
            )
        layout.addWidget(self.lbl_scp_status)

        # ── 查询条件分组 ──
        group_search = QGroupBox("查询条件")
        grid = QGridLayout(group_search)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setColumnStretch(5, 1)
        grid.setColumnStretch(7, 1)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)

        # 第一行
        grid.addWidget(QLabel("患者姓名:"), 0, 0)
        self.edit_find_name = QLineEdit()
        self.edit_find_name.setPlaceholderText("支持模糊查询")
        grid.addWidget(self.edit_find_name, 0, 1)

        grid.addWidget(QLabel("患者 ID:"), 0, 2)
        self.edit_find_id = QLineEdit()
        self.edit_find_id.setPlaceholderText("Patient ID")
        grid.addWidget(self.edit_find_id, 0, 3)

        grid.addWidget(QLabel("检查号:"), 0, 4)
        self.edit_find_acc = QLineEdit()
        self.edit_find_acc.setPlaceholderText("Accession Number")
        grid.addWidget(self.edit_find_acc, 0, 5)

        grid.addWidget(QLabel("日期:"), 0, 6)
        self.combo_date_filter = QComboBox()
        self.combo_date_filter.addItems(["今天", "最近3天", "最近7天", "最近30天", "全部"])
        self.combo_date_filter.setCurrentIndex(0)
        grid.addWidget(self.combo_date_filter, 0, 7)

        layout.addWidget(group_search)

        # ── DSA 节点 + 查询按钮行 ──
        dsa_layout = QHBoxLayout()
        dsa_layout.addWidget(QLabel("目标 DSA:"))
        self.combo_dsa = QComboBox()
        self.combo_dsa.setMinimumWidth(280)
        for idx, node in enumerate(self.dsa_nodes):
            display = f"{node.get('name', '未命名')} ({node.get('ae_title', '')}@{node.get('host', '')}:{node.get('port', '')})"
            self.combo_dsa.addItem(display, idx)
        if not self.dsa_nodes:
            self.combo_dsa.addItem("未配置 DSA 节点", -1)
            self.combo_dsa.setEnabled(False)
        dsa_layout.addWidget(self.combo_dsa)

        dsa_layout.addStretch()

        self.btn_find = QPushButton("查询 DSA")
        self.btn_find.setObjectName("success")
        self.btn_find.setMinimumWidth(100)
        self.btn_find.clicked.connect(self._on_find)
        dsa_layout.addWidget(self.btn_find)
        layout.addLayout(dsa_layout)

        # 结果表格
        self.result_table = QTableView()
        self.result_model = QStandardItemModel()
        self.result_model.setHorizontalHeaderLabels(["患者姓名", "患者ID", "性别", "年龄", "出生日期", "检查号", "检查日期", "检查UID"])
        self.result_table.setModel(self.result_model)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.result_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setAlternatingRowColors(True)
        layout.addWidget(self.result_table)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #666; font-size: 12px;")
        layout.addWidget(self.lbl_status)

        # 进度条（C-MOVE 拉取时显示）
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(0)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_move = QPushButton("拉取选中检查")
        self.btn_move.setEnabled(False)
        self.btn_move.setObjectName("success")
        self.btn_move.clicked.connect(self._on_move)
        btn_layout.addWidget(self.btn_move)
        self.btn_close = QPushButton("关闭")
        self.btn_close.setObjectName("secondary")
        self.btn_close.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_close)
        layout.addLayout(btn_layout)

        self.result_table.clicked.connect(self._on_result_selected)
        self._selected_study_uid = ""

    def _on_find(self):
        """查询 DSA"""
        dsa_index = self.combo_dsa.currentData()
        if dsa_index is None or dsa_index < 0:
            QMessageBox.warning(self, "提示", "请先配置并选择一个 DSA 节点")
            return

        patient_name = self.edit_find_name.text().strip()
        patient_id = self.edit_find_id.text().strip()
        accession_number = self.edit_find_acc.text().strip()

        self.result_model.removeRows(0, self.result_model.rowCount())
        self.lbl_status.setText("正在查询 DSA 工作站...")
        self.btn_find.setEnabled(False)
        date_map = {
            "今天": "TODAY",
            "最近3天": "LAST3DAYS",
            "最近7天": "LAST7DAYS",
            "最近30天": "LAST30DAYS",
            "全部": "",
        }
        query = {
            "patient_name": patient_name,
            "patient_id": patient_id,
            "accession_number": accession_number,
            "study_date_range": date_map.get(self.combo_date_filter.currentText(), ""),
        }
        self.request_find.emit(query, dsa_index)

    def on_find_results(self, results: list):
        """接收查询结果"""
        self.btn_find.setEnabled(True)
        self.result_model.removeRows(0, self.result_model.rowCount())
        if not results:
            self.lbl_status.setText("未找到匹配的检查")
            return
        for result in results:
            row = [
                QStandardItem(result.get("patient_name", "")),
                QStandardItem(result.get("patient_id", "")),
                QStandardItem(result.get("patient_sex", "")),
                QStandardItem(result.get("patient_age", "")),
                QStandardItem(result.get("patient_birth_date", "")),
                QStandardItem(result.get("accession_number", "")),
                QStandardItem(result.get("study_date", "")),
                QStandardItem(result.get("study_instance_uid", "")),
            ]
            for item in row:
                item.setEditable(False)
            self.result_model.appendRow(row)
        self.lbl_status.setText(f"找到 {len(results)} 个检查")

    def _on_result_selected(self, index: QModelIndex):
        """选中行"""
        row = index.row()
        if row >= 0:
            self._selected_study_uid = self.result_model.item(row, 7).text()
            self.btn_move.setEnabled(True)

    def _on_move(self):
        """拉取选中检查"""
        if not self._selected_study_uid:
            QMessageBox.warning(self, "提示", "请先在表格中选择一个检查")
            return
        dsa_index = self.combo_dsa.currentData()
        if dsa_index is None or dsa_index < 0:
            QMessageBox.warning(self, "提示", "请先选择一个 DSA 节点")
            return
        if not self.scp_running:
            QMessageBox.warning(
                self, "SCP 未启动",
                "SCP 接收服务未运行，无法从 DSA 拉取图像。\n"
                "请先点击主窗口的「启动 SCP 接收」按钮，然后再试。"
            )
            return
        self.lbl_status.setText(f"正在从 DSA 拉取检查 {self._selected_study_uid}...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(0)
        self.btn_move.setEnabled(False)
        self.request_move.emit(self._selected_study_uid, self.scp_ae_title, dsa_index)

    def on_move_progress(self, current: int, total: int):
        """C-MOVE 拉取进度"""
        self.progress_bar.setVisible(True)
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(current)
        self.lbl_status.setText(f"正在从 DSA 拉取: {current}/{total}")

    def on_move_finished(self, success: int, total: int):
        """拉取完成"""
        self.progress_bar.setVisible(False)
        self.btn_move.setEnabled(True)
        self.lbl_status.setText(f"DSA 拉取完成: 成功 {success}/{total}")
        QMessageBox.information(self, "DSA 拉取完成", f"从 DSA 工作站拉取完成\n成功: {success} / 总计: {total}")

    def set_scp_running(self, running: bool, ae_title: str = None):
        """SCP 状态变化时实时更新提示"""
        self.scp_running = running
        if ae_title:
            self.scp_ae_title = ae_title
        if not self.scp_running:
            self.lbl_scp_status.setText("⚠️ SCP 服务未启动，无法拉取图像。请先启动 SCP 接收端。")
            self.lbl_scp_status.setStyleSheet(
                "background-color: #fef3c7; color: #92400e; padding: 8px 12px;"
                "border-radius: 6px; font-size: 13px; font-weight: 500;"
            )
        else:
            self.lbl_scp_status.setText(f"✅ SCP 运行中 ({self.scp_ae_title})，可以拉取图像")
            self.lbl_scp_status.setStyleSheet(
                "background-color: #fef3c7; color: #92400e; padding: 8px 12px;"
                "border-radius: 6px; font-size: 13px; font-weight: 500;"
            )

    def get_state(self) -> dict:
        """获取当前状态（用于关闭后恢复）"""
        results = []
        for row in range(self.result_model.rowCount()):
            results.append({
                "patient_name": self.result_model.item(row, 0).text(),
                "patient_id": self.result_model.item(row, 1).text(),
                "patient_sex": self.result_model.item(row, 2).text(),
                "patient_age": self.result_model.item(row, 3).text(),
                "patient_birth_date": self.result_model.item(row, 4).text(),
                "accession_number": self.result_model.item(row, 5).text(),
                "study_date": self.result_model.item(row, 6).text(),
                "study_instance_uid": self.result_model.item(row, 7).text(),
            })
        return {
            "results": results,
            "patient_name": self.edit_find_name.text(),
            "patient_id": self.edit_find_id.text(),
            "accession_number": self.edit_find_acc.text(),
            "date_filter_idx": self.combo_date_filter.currentIndex(),
            "selected_uid": self._selected_study_uid,
            "dsa_index": self.combo_dsa.currentIndex(),
            "status_text": self.lbl_status.text(),
        }

    def restore_state(self, state: dict):
        """恢复上次的状态"""
        if not state:
            return
        # 恢复查询条件输入框
        self.edit_find_name.setText(state.get("patient_name", ""))
        self.edit_find_id.setText(state.get("patient_id", ""))
        self.edit_find_acc.setText(state.get("accession_number", ""))
        # 恢复日期筛选
        idx = state.get("date_filter_idx", 0)
        if 0 <= idx < self.combo_date_filter.count():
            self.combo_date_filter.setCurrentIndex(idx)
        # 恢复 DSA 节点选择
        dsa_idx = state.get("dsa_index", 0)
        if 0 <= dsa_idx < self.combo_dsa.count():
            self.combo_dsa.setCurrentIndex(dsa_idx)
        # 恢复查询结果
        self.result_model.removeRows(0, self.result_model.rowCount())
        for r in state.get("results", []):
            row = [
                QStandardItem(r.get("patient_name", "")),
                QStandardItem(r.get("patient_id", "")),
                QStandardItem(r.get("patient_sex", "")),
                QStandardItem(r.get("patient_age", "")),
                QStandardItem(r.get("patient_birth_date", "")),
                QStandardItem(r.get("accession_number", "")),
                QStandardItem(r.get("study_date", "")),
                QStandardItem(r.get("study_instance_uid", "")),
            ]
            for item in row:
                item.setEditable(False)
            self.result_model.appendRow(row)
        # 恢复选中
        uid = state.get("selected_uid", "")
        if uid:
            for row in range(self.result_model.rowCount()):
                if self.result_model.item(row, 7).text() == uid:
                    self.result_table.selectRow(row)
                    self._selected_study_uid = uid
                    self.btn_move.setEnabled(True)
                    break
        # 恢复状态文本
        self.lbl_status.setText(state.get("status_text", ""))


class HelpDialog(QDialog):
    """帮助文档弹窗"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("使用帮助")
        self.setMinimumSize(800, 600)
        self.resize(900, 700)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 标题
        title = QLabel("📖 DICOM MIX Tools 使用帮助")
        title.setStyleSheet("font-size: 18px; font-weight: 600; color: #111827; padding: 8px 0;")
        layout.addWidget(title)

        # 帮助内容浏览器
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setStyleSheet("""
            QTextBrowser {
                background-color: #ffffff;
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                padding: 12px;
                font-size: 13px;
                line-height: 1.6;
            }
        """)
        browser.setHtml(self._build_help_html())
        layout.addWidget(browser)

        # 关闭按钮
        btn_close = QPushButton("关闭")
        btn_close.setObjectName("secondary")
        btn_close.clicked.connect(self.accept)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)

    def _build_help_html(self) -> str:
        return """\
        <html>
        <body style="font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; color: #374151;">
        <h2 style="color: #111827;">DSA 图像路由与编辑工具</h2>
        <p>用于 GE DSA 等血管造影设备的 DICOM 图像接收、查看、编辑和转发。</p>

        <h3 style="color: #333333;">功能特性</h3>
        <ul>
        <li><b>图像接收</b>：内置 DICOM SCP 服务端，可接收 DSA 设备推送的图像</li>
        <li><b>本地导入</b>：批量导入本地 DICOM 文件夹</li>
        <li><b>DSA 查看器</b>：多帧图像浏览、实时减影、窗宽窗位调节、循环播放</li>
        <li><b>图像序列导出</b>：支持将当前序列导出为 MP4 视频或 PNG 图片序列</li>
        <li><b>序列快速切换</b>：查看器内一键切换前后序列，自动管理勾选状态</li>
        <li><b>患者信息编辑</b>：修改患者姓名、ID、性别、年龄、出生日期、检查号等</li>
        <li><b>UID 重置</b>：自动重新生成 Study/Series/SOP Instance UID</li>
        <li><b>主机查询与发送</b>：C-FIND 查询、C-STORE 发送</li>
        <li><b>DSA 双向连接</b>：C-FIND 查询 DSA 工作站、C-MOVE 主动拉取图像，支持配置多个 DSA 节点</li>
        </ul>

        <h3 style="color: #0078d4;">详细使用说明</h3>

        <h4>1. 接收 DSA 图像（SCP 模式）</h4>
        <ol>
        <li>切换到 <b>网络配置</b> Tab，查看本机 IP 地址</li>
        <li>确认 SCP AE Title 和端口（默认 <code>MIX_SCP</code>，端口 <code>11112</code>）</li>
        <li>在 DSA 设备或工作站上配置 DICOM 发送目标：AE Title=<code>MIX_SCP</code>，IP=本机 IP，端口=<code>11112</code></li>
        <li>点击工具栏的 <b>启动 SCP</b> 按钮</li>
        <li>从 DSA 设备推送图像，图像将自动保存到 <code>temp_dicom/</code> 目录</li>
        <li>点击 <b>刷新</b> 按钮，左侧树形控件将显示已接收的检查</li>
        </ol>

        <h4>2. 从 DSA 工作站主动拉取</h4>
        <ol>
        <li>切换到 <b>网络配置</b> Tab</li>
        <li>在 <b>DSA 主机/工作站</b> 区域点击 <b>添加</b> 按钮，填写 DSA 节点名称、AE Title、IP 和端口</li>
        <li>支持添加多个 DSA 节点，可分别编辑或删除</li>
        <li>点击 <b>保存配置</b></li>
        <li>点击工具栏 <b>数据载入</b> → <b>查询 DSA</b>，在弹窗中选择目标 DSA 节点</li>
        <li>选择目标检查，点击 <b>拉取选中检查</b></li>
        <li>DSA 工作站将图像推送到本机 SCP，完成后自动刷新左侧树</li>
        </ol>

        <h4>3. 本地文件导入</h4>
        <ol>
        <li>点击工具栏的 <b>数据载入</b> → <b>载入本地文件夹</b></li>
        <li>选择包含 DICOM 文件的文件夹</li>
        <li>程序自动扫描、验证并复制有效 DICOM 文件到工作目录</li>
        <li>完成后自动刷新左侧树形控件</li>
        </ol>

        <h4>4. 图像查看</h4>
        <ul>
        <li><b>左侧树形控件</b>显示三级结构：检查 → 序列 → 影像</li>
        <li>点击节点即可加载对应图像到中间预览区</li>
        <li><b>鼠标滚轮</b>：缩放图像</li>
        <li><b>鼠标中键拖拽</b>：调节窗宽窗位（水平=窗宽，垂直=窗位）</li>
        <li><b>播放按钮</b>：自动循环播放当前序列</li>
        <li><b>◀ / ▶ 按钮</b>：手动逐帧前进/后退</li>
        <li><b>⏮ / ⏭ 按钮（右侧面板「播放」组）</b>：快速切换同一检查下的前后序列</li>
        </ul>

        <h4>5. 图像序列导出</h4>
        <ol>
        <li>在查看器中加载目标序列</li>
        <li>点击播放控制栏的 <b>导出</b> 按钮</li>
        <li>在弹窗中选择导出格式：
            <ul>
            <li><b>MP4</b>：设置输出帧率，选择保存路径</li>
            <li><b>PNG 序列</b>：设置输出文件夹和文件名前缀，每帧保存为独立 PNG 文件</li>
            </ul>
        </li>
        <li>点击 <b>确定</b>，等待导出完成</li>
        </ol>
        <p><b>注意</b>：导出图像会应用当前的窗宽窗位和减影设置。</p>

        <h4>5. DSA 减影</h4>
        <ol>
        <li>加载多帧 DSA 序列</li>
        <li>右键图像 → <b>将当前帧设为蒙片 (Mask)</b></li>
        <li>勾选右侧面板的 <b>开启实时减影</b></li>
        <li>图像将显示当前帧与蒙版帧的差值（血管显影）</li>
        <li>右侧面板可调节减影增益</li>
        </ol>

        <h4>6. 患者信息编辑与发送</h4>
        <ol>
        <li>在左侧树中勾选要处理的序列（勾选框支持多选）</li>
        <li>点击工具栏的 <b>修改病人信息</b> 按钮，打开右侧面板</li>
        <li>使用 <b>主机查询</b> 从远端主机搜索患者并自动填充，或直接在下方表单中手动输入</li>
        <li>确认必填项：患者姓名、患者 ID、检查号</li>
        <li>点击工具栏的 <b>拆分发送到主机</b>：覆写患者信息 + 重新生成 UID → C-STORE 发送</li>
        <li>或点击 <b>导出到本地</b>：保存到指定文件夹</li>
        </ol>

        <h4>7. 网络配置说明</h4>
        <table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse; border-color: #e5e7eb; font-size: 13px;">
        <tr style="background-color: #f9fafb;"><th>配置项</th><th>默认值</th><th>说明</th></tr>
        <tr><td>主机 AE Title</td><td><code>PACS</code></td><td>主机服务器的 AE Title</td></tr>
        <tr><td>主机地址</td><td><code>127.0.0.1</code></td><td>主机服务器 IP 地址</td></tr>
        <tr><td>主机端口</td><td><code>11112</code></td><td>主机服务器端口</td></tr>
        <tr><td>SCU AE Title</td><td><code>MIX_SCU</code></td><td>本机作为 SCU 时的标识</td></tr>
        <tr><td>SCP AE Title</td><td><code>MIX_SCP</code></td><td>本机作为 SCP 时的标识</td></tr>
        <tr><td>SCP 端口</td><td><code>11112</code></td><td>本机 SCP 监听端口</td></tr>
        <tr><td>DSA 节点</td><td colspan="2">支持配置多个 DSA 节点（名称、AE Title、IP、端口）</td></tr>
        </table>

        <h3 style="color: #0078d4;">快捷键</h3>
        <ul>
        <li><b>鼠标滚轮</b>：缩放图像</li>
        <li><b>鼠标中键拖拽</b>：调节窗宽窗位</li>
        <li><b>右键图像</b>：设置减影蒙版帧</li>
        </ul>

        <p style="color: #6b7280; font-size: 12px; margin-top: 20px;">
        如有问题请联系：邮箱 lbmzjz@outlook.com / 微信 lbmzjz<br>
        内部工具，仅供医疗影像工作站使用。
        </p>
        </body>
        </html>
        """


class AboutDialog(QDialog):
    """关于软件弹窗"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关于 DICOM MIX Tools")
        self.setMinimumSize(480, 480)
        self.resize(520, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # LOGO
        logo_label = QLabel()
        logo_label.setAlignment(Qt.AlignCenter)
        logo_pixmap = self._load_logo()
        if logo_pixmap:
            logo_label.setPixmap(logo_pixmap.scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(logo_label)

        # 标题
        title = QLabel("DICOM MIX Tools")
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #333333;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        # 版本
        version = QLabel("版本 V4.3")
        version.setStyleSheet("font-size: 14px; color: #6b7280;")
        version.setAlignment(Qt.AlignCenter)
        layout.addWidget(version)

        # 描述
        desc = QLabel(
            "DSA 图像路由与编辑工具\n"
            "用于 GE DSA 等血管造影设备的 DICOM 图像接收、查看、编辑和转发。"
        )
        desc.setStyleSheet("font-size: 13px; color: #374151; line-height: 1.6;")
        desc.setAlignment(Qt.AlignCenter)
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # 版权与开源协议
        license_text = QLabel(
            "<b>开源协议</b><br>"
            "本软件基于 MIT License 开源发布。<br>"
            "Copyright © 2024-2026 lbmzjz. All rights reserved.<br><br>"
            "第三方依赖：PySide6 · pydicom · pynetdicom · numpy · opencv-python"
        )
        license_text.setStyleSheet("font-size: 12px; color: #4b5563; line-height: 1.7;")
        license_text.setAlignment(Qt.AlignCenter)
        license_text.setWordWrap(True)
        layout.addWidget(license_text)

        layout.addStretch()

        # 联系方式
        contact = QLabel(
            "<b>联系方式</b><br>"
            "邮箱: lbmzjz@outlook.com<br>"
            "微信: lbmzjz"
        )
        contact.setStyleSheet("font-size: 12px; color: #6b7280; line-height: 1.6;")
        contact.setAlignment(Qt.AlignCenter)
        layout.addWidget(contact)

        # 技术栈
        tech = QLabel(
            "技术栈：PySide6 · pydicom · pynetdicom · numpy · opencv-python"
        )
        tech.setStyleSheet("font-size: 11px; color: #9ca3af;")
        tech.setAlignment(Qt.AlignCenter)
        tech.setWordWrap(True)
        layout.addWidget(tech)

        # 关闭按钮
        btn_close = QPushButton("关闭")
        btn_close.setObjectName("secondary")
        btn_close.clicked.connect(self.accept)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(btn_close)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

    @staticmethod
    def _load_logo():
        """尝试加载 LOGO 图片"""
        import os, sys
        from PySide6.QtGui import QPixmap
        candidates = [
            os.path.join(os.path.dirname(__file__), "assets", "logo.ico"),
            os.path.join(os.path.dirname(sys.executable), "assets", "logo.ico"),
            os.path.join(os.path.dirname(sys.executable), "_internal", "assets", "logo.ico"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                pix = QPixmap(path)
                if not pix.isNull():
                    return pix
        return None


# ------------------------------------------------------------------------------
# Worklist 查询弹窗
# ------------------------------------------------------------------------------
class WorklistQueryDialog(QDialog):
    """Modality Worklist 查询弹窗"""

    request_find = Signal(dict)  # 发出 Worklist 查询请求

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("查询 Worklist")
        self.setMinimumSize(900, 500)

        layout = QVBoxLayout(self)

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

        search_layout.addWidget(QLabel("日期:"))
        self.combo_date_filter = QComboBox()
        self.combo_date_filter.addItems(["今天", "最近3天", "最近7天", "最近30天", "全部"])
        self.combo_date_filter.setCurrentIndex(0)
        search_layout.addWidget(self.combo_date_filter)
        layout.addLayout(search_layout)

        # 第二行搜索条件
        search_layout2 = QHBoxLayout()
        search_layout2.addWidget(QLabel("模态:"))
        self.edit_modality = QLineEdit()
        self.edit_modality.setPlaceholderText("如 CT/MR/DSA")
        search_layout2.addWidget(self.edit_modality)

        search_layout2.addWidget(QLabel("设备 AE:"))
        self.edit_station_ae = QLineEdit()
        self.edit_station_ae.setPlaceholderText("Scheduled Station AE")
        search_layout2.addWidget(self.edit_station_ae)

        search_layout2.addStretch()
        self.btn_find = QPushButton("查询 Worklist")
        self.btn_find.setObjectName("success")
        self.btn_find.clicked.connect(self._on_find)
        search_layout2.addWidget(self.btn_find)
        layout.addLayout(search_layout2)

        # 查询结果表格
        self.table_results = QTableView()
        self.table_results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table_results.setAlternatingRowColors(True)
        self.result_model = QStandardItemModel()
        self.result_model.setHorizontalHeaderLabels([
            "患者姓名", "患者ID", "性别", "出生日期", "检查号",
            "预约日期", "预约时间", "检查描述", "设备AE", "模态"
        ])
        self.table_results.setModel(self.result_model)
        self.table_results.horizontalHeader().setStretchLastSection(True)
        self.table_results.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table_results)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #666; font-size: 12px;")
        layout.addWidget(self.lbl_status)

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_close = QPushButton("关闭")
        self.btn_close.setObjectName("secondary")
        self.btn_close.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_close)
        layout.addLayout(btn_layout)

    def _on_find(self):
        """点击查询按钮"""
        patient_name = self.edit_find_name.text().strip()
        patient_id = self.edit_find_id.text().strip()
        accession_number = self.edit_find_acc.text().strip()

        # 必须输入至少一个查询条件
        if not patient_name and not patient_id and not accession_number:
            QMessageBox.warning(
                self, "提示",
                "请输入患者姓名、患者ID或检查号中的至少一项进行查询"
            )
            return

        date_map = {
            "今天": "TODAY",
            "最近3天": "LAST3DAYS",
            "最近7天": "LAST7DAYS",
            "最近30天": "LAST30DAYS",
            "全部": "",
        }
        query = {
            "patient_name": patient_name,
            "patient_id": patient_id,
            "accession_number": accession_number,
            "study_date_range": date_map.get(self.combo_date_filter.currentText(), ""),
            "modality": self.edit_modality.text().strip(),
            "station_ae": self.edit_station_ae.text().strip(),
        }
        self.result_model.removeRows(0, self.result_model.rowCount())
        self.lbl_status.setText("正在查询 Worklist...")
        self.btn_find.setEnabled(False)
        self.request_find.emit(query)

    def on_results_ready(self, results: list):
        """接收查询结果"""
        self.btn_find.setEnabled(True)
        self.result_model.removeRows(0, self.result_model.rowCount())
        if not results:
            self.lbl_status.setText("未找到匹配的 Worklist 记录")
            return

        for r in results:
            row = [
                QStandardItem(r.get("patient_name", "")),
                QStandardItem(r.get("patient_id", "")),
                QStandardItem(r.get("patient_sex", "")),
                QStandardItem(r.get("patient_birth_date", "")),
                QStandardItem(r.get("accession_number", "")),
                QStandardItem(r.get("scheduled_date", "")),
                QStandardItem(r.get("scheduled_time", "")),
                QStandardItem(r.get("scheduled_description", "")),
                QStandardItem(r.get("scheduled_station_ae", "")),
                QStandardItem(r.get("modality", "")),
            ]
            for item in row:
                item.setEditable(False)
            self.result_model.appendRow(row)

        self.lbl_status.setText(f"查询完成，共 {len(results)} 条记录")


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
    request_pacs_move = Signal(str, str)   # 请求 PACS C-MOVE (study_uid, move_dest_ae)
    request_process_and_store = Signal(list, dict, str)  # (选中序列, 目标患者信息, 目标节点标识)
    request_process_and_export = Signal(list, dict, str)  # (选中序列, 目标患者信息, 输出目录)
    network_config_changed = Signal(dict)  # 网络配置变更通知
    temp_dir_changed = Signal(str)         # 缓存目录变更通知
    request_dsa_find = Signal(dict, int)        # 请求 DSA C-FIND (参数字典, dsa_index)
    request_dsa_move = Signal(str, str, int)    # 请求 DSA C-MOVE (study_uid, move_dest_ae, dsa_index)
    show_pacs_query_requested = Signal()   # 请求显示主机查询弹窗
    show_dsa_query_requested = Signal()    # 请求显示 DSA 查询弹窗
    show_worklist_query_requested = Signal()  # 请求显示 Worklist 查询弹窗
    request_export = Signal(str, dict)     # 请求导出 (format, params)
    request_normalize = Signal(list, str, str)  # 请求归一化 (file_paths, output_dir, target_manufacturer)
    request_worklist_find = Signal(dict)   # 请求 Worklist C-FIND (参数字典)

    # 插件管理信号 (V4.0)
    request_plugin_install = Signal(str)     # 请求安装插件 (plugin_name)
    request_plugin_uninstall = Signal(str)   # 请求卸载插件 (plugin_name)
    request_plugin_activate = Signal(str)    # 请求激活插件 (plugin_name)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DICOM MIX Tools - DSA 图像路由与编辑")
        self.resize(1600, 720)
        # 设置窗口图标
        self._set_window_icon()

        # 信号中心（供各工作线程回传状态）
        self.input_signals = DicomInputSignals()
        self.network_signals = DicomNetworkSignals()
        self.processor_signals = DicomProcessorSignals()

        # 先初始化 UI 控件，再连接信号（避免引用未创建的属性）
        self._init_ui()
        self._connect_internal_signals()

    # ---------- 初始化界面 ----------

    def _set_window_icon(self):
        """尝试设置窗口图标（运行时和打包后均兼容）"""
        app = QApplication.instance()
        if app is None:
            return
        # 尝试多个路径：源码运行时、打包后
        candidates = [
            os.path.join(os.path.dirname(__file__), "assets", "logo.ico"),
            os.path.join(os.path.dirname(sys.executable), "assets", "logo.ico"),
            os.path.join(os.path.dirname(sys.executable), "_internal", "assets", "logo.ico"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                from PySide6.QtGui import QIcon
                icon = QIcon(path)
                app.setWindowIcon(icon)
                self.setWindowIcon(icon)
                break

    def _init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # DSA 节点列表（在 _load_network_config 中从 QSettings 加载）
        self._dsa_nodes: List[dict] = []

        # SCP 运行状态（用于 DSA 查询弹窗提示）
        self._scp_running = False

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

        # 工具栏图标按钮统一尺寸
        TB_BTN_SIZE = 44
        TB_ICON_SIZE = 26

        # SCP 启停按钮（运行时变红）
        self.btn_scp_toggle = QPushButton()
        self.btn_scp_toggle.setIcon(FluentIcon.CLOUD.icon())
        self.btn_scp_toggle.setCheckable(True)
        self.btn_scp_toggle.setObjectName("panelBtn")
        self.btn_scp_toggle.setToolTip("启动 SCP 接收")
        self.btn_scp_toggle.setFixedSize(TB_BTN_SIZE, TB_BTN_SIZE)
        self.btn_scp_toggle.setIconSize(QSize(TB_ICON_SIZE, TB_ICON_SIZE))
        self.btn_scp_toggle.toggled.connect(self._on_scp_toggle)
        toolbar.addWidget(self.btn_scp_toggle)

        toolbar.addSeparator()

        # 数据载入下拉菜单（整合本地载入、主机查询、DSA查询）
        self.btn_data_load = QToolButton()
        self.btn_data_load.setIcon(FluentIcon.FOLDER.icon())
        self.btn_data_load.setObjectName("panelBtn")
        self.btn_data_load.setPopupMode(QToolButton.InstantPopup)
        self.btn_data_load.setToolTip("数据载入")
        self.btn_data_load.setFixedSize(TB_BTN_SIZE, TB_BTN_SIZE)
        self.btn_data_load.setIconSize(QSize(TB_ICON_SIZE, TB_ICON_SIZE))
        self.menu_data_load = QMenu(self.btn_data_load)
        self.menu_data_load.setStyleSheet(self._menu_style())
        self.act_load_local = self.menu_data_load.addAction("载入本地文件夹")
        self.act_load_local.triggered.connect(self._on_load_local)
        self.menu_data_load.addSeparator()
        self.act_query_pacs = self.menu_data_load.addAction("查询主机")
        self.act_query_pacs.triggered.connect(self._on_show_pacs_query)
        self.act_query_dsa = self.menu_data_load.addAction("查询 DSA")
        self.act_query_dsa.triggered.connect(self._on_show_dsa_query)
        self.act_query_worklist = self.menu_data_load.addAction("查询 Worklist")
        self.act_query_worklist.triggered.connect(self._on_show_worklist_query)
        self.btn_data_load.setMenu(self.menu_data_load)
        toolbar.addWidget(self.btn_data_load)

        # 拆分发送下拉菜单（根据网络节点动态变化）
        self.btn_send_menu = QToolButton()
        self.btn_send_menu.setIcon(FluentIcon.SEND.icon())
        self.btn_send_menu.setObjectName("panelBtn")
        self.btn_send_menu.setPopupMode(QToolButton.InstantPopup)
        self.btn_send_menu.setToolTip("拆分发送")
        self.btn_send_menu.setFixedSize(TB_BTN_SIZE, TB_BTN_SIZE)
        self.btn_send_menu.setIconSize(QSize(TB_ICON_SIZE, TB_ICON_SIZE))
        self.menu_send_target = QMenu(self.btn_send_menu)
        self.menu_send_target.setStyleSheet(self._menu_style())
        self.btn_send_menu.setMenu(self.menu_send_target)
        self.btn_send_menu.setEnabled(False)
        toolbar.addWidget(self.btn_send_menu)

        # 导出菜单
        self.btn_export_local_toolbar = QToolButton()
        self.btn_export_local_toolbar.setIcon(FluentIcon.SAVE_AS.icon())
        self.btn_export_local_toolbar.setObjectName("panelBtn")
        self.btn_export_local_toolbar.setToolTip("导出")
        self.btn_export_local_toolbar.setFixedSize(TB_BTN_SIZE, TB_BTN_SIZE)
        self.btn_export_local_toolbar.setIconSize(QSize(TB_ICON_SIZE, TB_ICON_SIZE))
        self.btn_export_local_toolbar.setPopupMode(QToolButton.InstantPopup)
        self.menu_export = QMenu(self.btn_export_local_toolbar)
        self.menu_export.setStyleSheet(self._menu_style())
        self.act_export_dicom = self.menu_export.addAction("导出处理后的 DICOM")
        self.act_export_dicom.triggered.connect(self._on_process_and_export)
        self.menu_export.addSeparator()
        self.act_export_mp4 = self.menu_export.addAction("导出序列为 MP4")
        self.act_export_mp4.triggered.connect(self._on_export_sequence_mp4)
        self.act_export_png = self.menu_export.addAction("导出序列为 PNG")
        self.act_export_png.triggered.connect(self._on_export_sequence_png)
        self.act_export_frame = self.menu_export.addAction("导出当前帧")
        self.act_export_frame.triggered.connect(self._on_export_current_frame)
        self.btn_export_local_toolbar.setMenu(self.menu_export)
        self.btn_export_local_toolbar.setEnabled(False)
        toolbar.addWidget(self.btn_export_local_toolbar)

        # 异构数据归一化按钮
        self.btn_normalize = QPushButton()
        self.btn_normalize.setIcon(FluentIcon.TILES.icon())
        self.btn_normalize.setObjectName("panelBtn")
        self.btn_normalize.setToolTip("异构数据归一化")
        self.btn_normalize.setFixedSize(TB_BTN_SIZE, TB_BTN_SIZE)
        self.btn_normalize.setIconSize(QSize(TB_ICON_SIZE, TB_ICON_SIZE))
        self.btn_normalize.clicked.connect(self._on_show_normalizer)
        toolbar.addWidget(self.btn_normalize)

        # DICOM标签编辑器按钮
        self.btn_edit_dicom = QPushButton()
        self.btn_edit_dicom.setIcon(FluentIcon.EDIT.icon())
        self.btn_edit_dicom.setObjectName("panelBtn")
        self.btn_edit_dicom.setToolTip("编辑DICOM标签")
        self.btn_edit_dicom.setFixedSize(TB_BTN_SIZE, TB_BTN_SIZE)
        self.btn_edit_dicom.setIconSize(QSize(TB_ICON_SIZE, TB_ICON_SIZE))
        self.btn_edit_dicom.clicked.connect(self._on_edit_dicom_tags)
        self.btn_edit_dicom.setEnabled(False)
        toolbar.addWidget(self.btn_edit_dicom)

        # 清除缓存按钮
        self.btn_clear_cache = QPushButton()
        self.btn_clear_cache.setIcon(FluentIcon.DELETE.icon())
        self.btn_clear_cache.setObjectName("panelBtn")
        self.btn_clear_cache.setToolTip("清除缓存")
        self.btn_clear_cache.setFixedSize(TB_BTN_SIZE, TB_BTN_SIZE)
        self.btn_clear_cache.setIconSize(QSize(TB_ICON_SIZE, TB_ICON_SIZE))
        self.btn_clear_cache.clicked.connect(self._on_clear_cache)
        toolbar.addWidget(self.btn_clear_cache)

        toolbar.addSeparator()

        # 插件中心下拉菜单 (V4.0)
        self.btn_plugins = QToolButton()
        self.btn_plugins.setIcon(FluentIcon.APPLICATION.icon())
        self.btn_plugins.setObjectName("panelBtn")
        self.btn_plugins.setPopupMode(QToolButton.InstantPopup)
        self.btn_plugins.setToolTip("插件中心")
        self.btn_plugins.setFixedSize(TB_BTN_SIZE, TB_BTN_SIZE)
        self.btn_plugins.setIconSize(QSize(TB_ICON_SIZE, TB_ICON_SIZE))
        self.menu_plugins = QMenu(self.btn_plugins)
        self.menu_plugins.setStyleSheet(self._menu_style())
        self.act_plugin_manager = self.menu_plugins.addAction("插件管理...")
        self.act_plugin_manager.triggered.connect(self._on_show_plugin_manager)
        self.menu_plugins.addSeparator()
        # 动态插件动作槽（由 PluginManager 填充）
        self._plugin_actions: Dict[str, QAction] = {}
        self.btn_plugins.setMenu(self.menu_plugins)
        toolbar.addWidget(self.btn_plugins)

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

        # 弹性空间
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        toolbar.addWidget(spacer)

        # 右侧面板切换按钮
        self.btn_panel_patient = QPushButton()
        self.btn_panel_patient.setIcon(FluentIcon.PENCIL_INK.icon())
        self.btn_panel_patient.setCheckable(True)
        self.btn_panel_patient.setObjectName("panelBtn")
        self.btn_panel_patient.setToolTip("修改病人信息")
        self.btn_panel_patient.setFixedSize(TB_BTN_SIZE, TB_BTN_SIZE)
        self.btn_panel_patient.setIconSize(QSize(TB_ICON_SIZE, TB_ICON_SIZE))
        self.btn_panel_patient.clicked.connect(lambda: self._toggle_right_panel(0))
        toolbar.addWidget(self.btn_panel_patient)

        self.btn_panel_network = QPushButton()
        self.btn_panel_network.setIcon(FluentIcon.SETTING.icon())
        self.btn_panel_network.setCheckable(True)
        self.btn_panel_network.setObjectName("panelBtn")
        self.btn_panel_network.setToolTip("网络配置")
        self.btn_panel_network.setFixedSize(TB_BTN_SIZE, TB_BTN_SIZE)
        self.btn_panel_network.setIconSize(QSize(TB_ICON_SIZE, TB_ICON_SIZE))
        self.btn_panel_network.clicked.connect(lambda: self._toggle_right_panel(1))
        toolbar.addWidget(self.btn_panel_network)

        toolbar.addSeparator()

        # 帮助按钮
        self.btn_help = QPushButton()
        self.btn_help.setIcon(FluentIcon.QUESTION.icon())
        self.btn_help.setObjectName("panelBtn")
        self.btn_help.setToolTip("使用帮助")
        self.btn_help.setFixedSize(TB_BTN_SIZE, TB_BTN_SIZE)
        self.btn_help.setIconSize(QSize(TB_ICON_SIZE, TB_ICON_SIZE))
        self.btn_help.clicked.connect(self._on_show_help)
        toolbar.addWidget(self.btn_help)

        # 关于按钮
        self.btn_about = QPushButton()
        self.btn_about.setIcon(FluentIcon.INFO.icon())
        self.btn_about.setObjectName("panelBtn")
        self.btn_about.setToolTip("关于软件")
        self.btn_about.setFixedSize(TB_BTN_SIZE, TB_BTN_SIZE)
        self.btn_about.setIconSize(QSize(TB_ICON_SIZE, TB_ICON_SIZE))
        self.btn_about.clicked.connect(self._on_show_about)
        toolbar.addWidget(self.btn_about)

    def _init_central_splitter(self):
        """中部主体：左右分割面板（左: 源数据树, 中: DSA 预览），右侧面板通过工具栏按钮弹出"""
        splitter = QSplitter(Qt.Horizontal)

        # -- 左侧：源数据树形区 --
        left_widget = self._create_left_panel()
        splitter.addWidget(left_widget)

        # -- 中间：DSA 图像查看器 --
        center_widget = self._create_viewer_panel()
        splitter.addWidget(center_widget)

        splitter.setSizes([280, 900])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.centralWidget().layout().addWidget(splitter, stretch=1)

        # -- 右侧：可停靠面板（默认隐藏） --
        self._init_right_dock()

    def _create_left_panel(self) -> QWidget:
        """构建左侧源数据展示面板"""
        group = QGroupBox("源数据")
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

        # 右键菜单
        self.tree_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(self._on_tree_context_menu)

        # 连接树节点选择变化信号，联动中间 DSA 查看器
        self.tree_view.selectionModel().currentChanged.connect(self._on_tree_selection_changed)

        # 左侧底部小工具栏：刷新 + 移除 + 清空
        hbox = QHBoxLayout()
        hbox.setSpacing(4)

        self.btn_refresh_tree = QPushButton()
        self.btn_refresh_tree.setIcon(FluentIcon.SYNC.icon())
        self.btn_refresh_tree.setObjectName("panelBtn")
        self.btn_refresh_tree.setToolTip("刷新列表")
        self.btn_refresh_tree.setFixedSize(36, 36)
        self.btn_refresh_tree.setIconSize(QSize(20, 20))
        self.btn_refresh_tree.clicked.connect(self._on_refresh_tree)
        hbox.addWidget(self.btn_refresh_tree)

        self.btn_delete_tree = QPushButton()
        self.btn_delete_tree.setIcon(FluentIcon.REMOVE.icon())
        self.btn_delete_tree.setObjectName("panelBtn")
        self.btn_delete_tree.setToolTip("从列表中移除选中的病人，不删除磁盘文件")
        self.btn_delete_tree.setFixedSize(36, 36)
        self.btn_delete_tree.setIconSize(QSize(20, 20))
        self.btn_delete_tree.clicked.connect(self._on_remove_selected_studies)
        hbox.addWidget(self.btn_delete_tree)

        self.btn_clear_tree_left = QPushButton()
        self.btn_clear_tree_left.setIcon(FluentIcon.BROOM.icon())
        self.btn_clear_tree_left.setObjectName("panelBtn")
        self.btn_clear_tree_left.setToolTip("清空列表")
        self.btn_clear_tree_left.setFixedSize(36, 36)
        self.btn_clear_tree_left.setIconSize(QSize(20, 20))
        self.btn_clear_tree_left.clicked.connect(self._on_clear_tree)
        hbox.addWidget(self.btn_clear_tree_left)
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
        """构建右侧患者信息面板（TabWidget）"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(4, 4, 4, 4)

        self.tab_widget = QTabWidget()

        # --- Tab 0: 修改病人信息（主机查询 + 手动输入） ---
        self.tab_patient_info = self._create_patient_info_tab()
        self.tab_widget.addTab(self.tab_patient_info, "修改病人信息")

        # --- Tab 1: 网络配置 ---
        self.tab_network = self._create_network_config_tab()
        self.tab_widget.addTab(self.tab_network, "网络配置")

        # Tab 切换时更新底部目标摘要
        self.tab_widget.currentChanged.connect(self._on_tab_changed)

        layout.addWidget(self.tab_widget)
        return widget

    def _init_right_dock(self):
        """创建右侧可停靠面板（带滚动条），默认隐藏"""
        self.right_dock = QDockWidget("目标患者信息", self)
        self.right_dock.setAllowedAreas(Qt.RightDockWidgetArea)
        self.right_dock.setMinimumWidth(360)
        self.right_dock.setFeatures(
            QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable
        )

        # 用 QScrollArea 包裹，内容超出时可滚动
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        right_content = self._create_right_panel()
        scroll.setWidget(right_content)
        self.right_dock.setWidget(scroll)

        self.addDockWidget(Qt.RightDockWidgetArea, self.right_dock)
        self.right_dock.hide()
        # 关闭时同步更新按钮状态
        self.right_dock.visibilityChanged.connect(lambda: self._update_panel_btn_states())

    def _toggle_right_panel(self, tab_index: int):
        """切换右侧面板显示/隐藏，并定位到指定 Tab"""
        if self.right_dock.isVisible() and self.tab_widget.currentIndex() == tab_index:
            self.right_dock.hide()
        else:
            # 记录当前窗口几何，防止 dock 展开时窗口突变
            geo = self.geometry()
            self.tab_widget.setCurrentIndex(tab_index)
            self.right_dock.show()
            self.right_dock.raise_()
            # 恢复窗口几何，让 dock 挤占中央区域而非撑大窗口
            self.setGeometry(geo)
        self._update_panel_btn_states()

    def _update_panel_btn_states(self):
        """根据右侧面板可见性和当前 Tab 更新工具栏按钮选中状态"""
        visible = self.right_dock.isVisible()
        current_tab = self.tab_widget.currentIndex() if visible else -1
        self.btn_panel_patient.setChecked(visible and current_tab == 0)
        self.btn_panel_network.setChecked(visible and current_tab == 1)

    def _create_pacs_tab(self) -> QWidget:
        """主机查询 Tab：搜索框 + 表格结果"""
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

        self.btn_find = QPushButton("查询主机")
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

    def _create_patient_info_tab(self) -> QWidget:
        """修改病人信息 Tab：整合主机查询 + 手动输入表单"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(10)
        layout.setContentsMargins(6, 6, 6, 6)

        # --- 主机查询区域 ---
        g_pacs = QGroupBox("从主机查询填充")
        pacs_layout = QVBoxLayout(g_pacs)

        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("姓名:"))
        self.edit_find_name = QLineEdit()
        self.edit_find_name.setPlaceholderText("支持模糊查询")
        search_layout.addWidget(self.edit_find_name)

        search_layout.addWidget(QLabel("ID:"))
        self.edit_find_id = QLineEdit()
        self.edit_find_id.setPlaceholderText("Patient ID")
        search_layout.addWidget(self.edit_find_id)

        search_layout.addWidget(QLabel("检查号:"))
        self.edit_find_acc = QLineEdit()
        self.edit_find_acc.setPlaceholderText("Accession Number")
        search_layout.addWidget(self.edit_find_acc)

        self.btn_find = QPushButton("查询")
        self.btn_find.clicked.connect(self._on_pacs_find)
        search_layout.addWidget(self.btn_find)
        pacs_layout.addLayout(search_layout)

        self.table_results = QTableView()
        self.table_results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table_results.setAlternatingRowColors(True)
        self.pacs_result_model = PacsResultModel(self)
        self.table_results.setModel(self.pacs_result_model)
        self.table_results.horizontalHeader().setStretchLastSection(True)
        self.table_results.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        pacs_layout.addWidget(self.table_results)

        self.lbl_pacs_selected = QLabel("未选择目标患者")
        self.lbl_pacs_selected.setStyleSheet("color: blue;")
        pacs_layout.addWidget(self.lbl_pacs_selected)

        self.table_results.selectionModel().currentRowChanged.connect(self._on_pacs_selection_changed)

        layout.addWidget(g_pacs)

        # --- 手动输入表单 ---
        g_manual = QGroupBox("手动修改 / 确认信息")
        manual_layout = QVBoxLayout(g_manual)

        f1 = QFormLayout()
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

        manual_layout.addLayout(f1)

        f2 = QFormLayout()
        f2.setLabelAlignment(Qt.AlignRight)
        f2.setSpacing(6)

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
        f2.addRow("性别:", sex_age)

        self.edit_manual_birth = QLineEdit()
        self.edit_manual_birth.setPlaceholderText("YYYYMMDD，如 19800101")
        f2.addRow("出生日期:", self.edit_manual_birth)

        self.edit_manual_inpatient_id = QLineEdit()
        self.edit_manual_inpatient_id.setPlaceholderText("住院号")
        f2.addRow("住院号:", self.edit_manual_inpatient_id)

        manual_layout.addLayout(f2)

        f3 = QFormLayout()
        f3.setLabelAlignment(Qt.AlignRight)
        f3.setSpacing(6)

        self.edit_manual_series_number = QLineEdit()
        self.edit_manual_series_number.setPlaceholderText("序列号 / 影像号")
        f3.addRow("影像号:", self.edit_manual_series_number)

        self.edit_manual_study_uid = QLineEdit()
        self.edit_manual_study_uid.setPlaceholderText("留空将自动生成")
        f3.addRow("Study UID:", self.edit_manual_study_uid)

        manual_layout.addLayout(f3)

        self.lbl_manual_hint = QLabel("")
        self.lbl_manual_hint.setStyleSheet("color: red;")
        manual_layout.addWidget(self.lbl_manual_hint)

        layout.addWidget(g_manual)
        layout.addStretch()

        scroll.setWidget(widget)
        return scroll

    def _create_manual_tab(self) -> QWidget:
        """手动输入 Tab：患者信息分组表单（保留供内部复用）"""
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
        f2.addRow("性别:", sex_age)

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
        """网络配置 Tab：本机信息、主机、DSA、SCU/SCP 参数配置"""
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
        self.lbl_local_ip.setStyleSheet("font-weight: bold; color: #333333;")
        f0.addRow("本机 IP:", self.lbl_local_ip)

        self.lbl_local_scp_info = QLabel("")
        f0.addRow("SCP 状态:", self.lbl_local_scp_info)

        layout.addWidget(g0)

        # --- 主机节点配置 ---
        g1 = QGroupBox("主机节点")
        f1 = QFormLayout(g1)
        f1.setLabelAlignment(Qt.AlignRight)
        f1.setSpacing(8)

        self.edit_pacs_ae_title = QLineEdit()
        self.edit_pacs_ae_title.setPlaceholderText("例如: HOST")
        f1.addRow("主机 AE Title:", self.edit_pacs_ae_title)

        self.edit_pacs_host = QLineEdit()
        self.edit_pacs_host.setPlaceholderText("例如: 127.0.0.1")
        f1.addRow("主机地址:", self.edit_pacs_host)

        self.spin_pacs_port = QSpinBox()
        self.spin_pacs_port.setRange(1, 65535)
        self.spin_pacs_port.setValue(11112)
        f1.addRow("主机端口:", self.spin_pacs_port)

        layout.addWidget(g1)

        # --- DSA 主机/工作站配置（多节点） ---
        g_dsa = QGroupBox("DSA 主机/工作站")
        dsa_layout = QVBoxLayout(g_dsa)
        dsa_layout.setSpacing(8)

        # DSA 节点表格
        self.dsa_nodes_model = QStandardItemModel()
        self.dsa_nodes_model.setHorizontalHeaderLabels(["名称", "AE Title", "IP 地址", "端口"])
        self.dsa_nodes_table = QTableView()
        self.dsa_nodes_table.setModel(self.dsa_nodes_model)
        self.dsa_nodes_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.dsa_nodes_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.dsa_nodes_table.horizontalHeader().setStretchLastSection(True)
        self.dsa_nodes_table.verticalHeader().setVisible(False)
        self.dsa_nodes_table.setAlternatingRowColors(True)
        self.dsa_nodes_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # 固定高度以容纳表头 + 3 行数据
        self.dsa_nodes_table.setMinimumHeight(100)
        self.dsa_nodes_table.setMaximumHeight(120)
        dsa_layout.addWidget(self.dsa_nodes_table)

        # 操作按钮
        dsa_btn_layout = QHBoxLayout()
        self.btn_dsa_add = QPushButton("添加")
        self.btn_dsa_add.setObjectName("success")
        self.btn_dsa_add.clicked.connect(self._on_dsa_add)
        dsa_btn_layout.addWidget(self.btn_dsa_add)

        self.btn_dsa_edit = QPushButton("编辑")
        self.btn_dsa_edit.setObjectName("success")
        self.btn_dsa_edit.clicked.connect(self._on_dsa_edit)
        dsa_btn_layout.addWidget(self.btn_dsa_edit)

        self.btn_dsa_delete = QPushButton("删除")
        self.btn_dsa_delete.setObjectName("danger")
        self.btn_dsa_delete.clicked.connect(self._on_dsa_delete)
        dsa_btn_layout.addWidget(self.btn_dsa_delete)
        dsa_btn_layout.addStretch()
        dsa_layout.addLayout(dsa_btn_layout)

        layout.addWidget(g_dsa)

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

        # --- 系统设置 ---
        g_sys = QGroupBox("系统设置")
        f_sys = QFormLayout(g_sys)
        f_sys.setLabelAlignment(Qt.AlignRight)
        f_sys.setSpacing(8)

        temp_hbox = QHBoxLayout()
        self.edit_temp_dir = QLineEdit()
        self.edit_temp_dir.setPlaceholderText("默认使用 LOCALAPPDATA")
        self.edit_temp_dir.setReadOnly(True)
        temp_hbox.addWidget(self.edit_temp_dir)

        self.btn_browse_temp = QPushButton("浏览...")
        self.btn_browse_temp.setObjectName("secondary")
        self.btn_browse_temp.setFixedWidth(60)
        self.btn_browse_temp.clicked.connect(self._on_browse_temp_dir)
        temp_hbox.addWidget(self.btn_browse_temp)

        self.btn_open_temp = QPushButton("打开")
        self.btn_open_temp.setObjectName("secondary")
        self.btn_open_temp.setFixedWidth(50)
        self.btn_open_temp.clicked.connect(self._on_open_temp_dir)
        temp_hbox.addWidget(self.btn_open_temp)

        f_sys.addRow("缓存目录:", temp_hbox)
        layout.addWidget(g_sys)

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

        # 禁用端口输入框的鼠标滚轮，防止误触
        for spin in (self.spin_pacs_port, self.spin_scp_port):
            spin.installEventFilter(self)
            spin.setFocusPolicy(Qt.StrongFocus)

        # 加载已保存的配置（如果有）
        self._load_network_config()
        return widget

    def eventFilter(self, obj, event):
        """事件过滤器：禁用网络配置端口框的滚轮事件"""
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.Wheel:
            if obj in (self.spin_pacs_port, self.spin_scp_port):
                event.ignore()
                return True
        return super().eventFilter(obj, event)

    def _load_network_config(self):
        """从 QSettings 加载网络配置，若不存在则使用 config.py 默认值。"""
        import json
        from config import (
            DEFAULT_SCP_AE_TITLE, DEFAULT_SCP_PORT,
            DEFAULT_PACS_AE_TITLE, DEFAULT_PACS_HOST, DEFAULT_PACS_PORT,
            DEFAULT_LOCAL_SCU_AE_TITLE,
            DEFAULT_DSA_NODES, DEFAULT_TEMP_DIR,
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

        # DSA 多节点配置
        dsa_json = settings.value("network/dsa_nodes_json", "")
        if dsa_json:
            try:
                self._dsa_nodes = json.loads(dsa_json)
            except (json.JSONDecodeError, TypeError):
                self._dsa_nodes = list(DEFAULT_DSA_NODES)
        else:
            # 兼容旧版单节点配置
            old_ae = settings.value("network/dsa_ae_title", "")
            if old_ae:
                self._dsa_nodes = [{
                    "name": "DSA-1",
                    "ae_title": old_ae,
                    "host": settings.value("network/dsa_host", "192.168.1.100"),
                    "port": int(settings.value("network/dsa_port", 11112)),
                }]
            else:
                self._dsa_nodes = list(DEFAULT_DSA_NODES)

        self._refresh_dsa_table()
        self._refresh_send_target_menu()

        # 缓存目录
        temp_dir = settings.value("system/temp_dir", "")
        self.edit_temp_dir.setText(temp_dir if temp_dir else DEFAULT_TEMP_DIR)

        # 更新 SCP 状态显示
        self._update_scp_status_label()

    def _on_save_network_config(self):
        """保存网络配置到 QSettings 并发射变更信号。"""
        import json
        settings = QSettings("MedicalSoftware", "DICOMMIXTools")
        settings.setValue("network/pacs_ae_title", self.edit_pacs_ae_title.text().strip())
        settings.setValue("network/pacs_host", self.edit_pacs_host.text().strip())
        settings.setValue("network/pacs_port", self.spin_pacs_port.value())
        settings.setValue("network/scu_ae_title", self.edit_scu_ae_title.text().strip())
        settings.setValue("network/scp_ae_title", self.edit_scp_ae_title.text().strip())
        settings.setValue("network/scp_port", self.spin_scp_port.value())

        # 保存 DSA 多节点列表为 JSON
        settings.setValue("network/dsa_nodes_json", json.dumps(self._dsa_nodes))

        # 保存缓存目录
        temp_dir = self.edit_temp_dir.text().strip()
        settings.setValue("system/temp_dir", temp_dir)

        self.lbl_network_hint.setText("配置已保存（重启后生效或立即应用）")
        QTimer.singleShot(3000, lambda: self.lbl_network_hint.setText(""))

        # 发射配置变更信号，供 main.py 重新初始化网络模块
        self.network_config_changed.emit(self.get_network_config())
        # 发射缓存目录变更信号
        self.temp_dir_changed.emit(temp_dir)
        # 更新工具栏标签
        self.lbl_temp_dir.setText(f"Temp: {temp_dir}")

    def _on_reset_network_config(self):
        """恢复默认配置。"""
        from config import (
            DEFAULT_SCP_AE_TITLE, DEFAULT_SCP_PORT,
            DEFAULT_PACS_AE_TITLE, DEFAULT_PACS_HOST, DEFAULT_PACS_PORT,
            DEFAULT_LOCAL_SCU_AE_TITLE,
            DEFAULT_DSA_NODES, DEFAULT_TEMP_DIR,
        )
        self.edit_pacs_ae_title.setText(DEFAULT_PACS_AE_TITLE)
        self.edit_pacs_host.setText(DEFAULT_PACS_HOST)
        self.spin_pacs_port.setValue(DEFAULT_PACS_PORT)
        self.edit_scu_ae_title.setText(DEFAULT_LOCAL_SCU_AE_TITLE)
        self.edit_scp_ae_title.setText(DEFAULT_SCP_AE_TITLE)
        self.spin_scp_port.setValue(DEFAULT_SCP_PORT)
        self._dsa_nodes = list(DEFAULT_DSA_NODES)
        self._refresh_dsa_table()
        self._refresh_send_target_menu()
        self.edit_temp_dir.setText(DEFAULT_TEMP_DIR)
        self._on_save_network_config()

    def _on_browse_temp_dir(self):
        """浏览选择缓存目录。"""
        from PySide6.QtWidgets import QFileDialog
        current = self.edit_temp_dir.text().strip()
        if not current or not os.path.isdir(current):
            current = self._get_temp_dir()
        path = QFileDialog.getExistingDirectory(self, "选择缓存目录", current)
        if path:
            self.edit_temp_dir.setText(path)

    def _on_open_temp_dir(self):
        """用系统文件管理器打开当前缓存目录。"""
        import subprocess
        path = self.edit_temp_dir.text().strip()
        if not path or not os.path.isdir(path):
            path = self._get_temp_dir()
        if os.path.isdir(path):
            subprocess.run(["explorer", os.path.normpath(path)])

    def get_network_config(self) -> dict:
        """返回当前界面上的网络配置字典。"""
        return {
            "pacs_ae_title": self.edit_pacs_ae_title.text().strip(),
            "pacs_host": self.edit_pacs_host.text().strip(),
            "pacs_port": self.spin_pacs_port.value(),
            "scu_ae_title": self.edit_scu_ae_title.text().strip(),
            "scp_ae_title": self.edit_scp_ae_title.text().strip(),
            "scp_port": self.spin_scp_port.value(),
            "dsa_nodes": self._dsa_nodes,
        }

    def _update_scp_status_label(self):
        """更新 SCP 状态显示标签。"""
        ae = self.edit_scp_ae_title.text().strip()
        port = self.spin_scp_port.value()
        local_ips = self._get_local_ips()
        self.lbl_local_scp_info.setText(
            f"AE Title: {ae}  |  端口: {port}  |  IP: {', '.join(local_ips)}"
        )

    # ---------- DSA 节点管理 ----------

    def _refresh_dsa_table(self):
        """刷新 DSA 节点表格显示。"""
        self.dsa_nodes_model.removeRows(0, self.dsa_nodes_model.rowCount())
        for node in self._dsa_nodes:
            row = [
                QStandardItem(node.get("name", "")),
                QStandardItem(node.get("ae_title", "")),
                QStandardItem(node.get("host", "")),
                QStandardItem(str(node.get("port", 11112))),
            ]
            for item in row:
                item.setEditable(False)
            self.dsa_nodes_model.appendRow(row)

    def _refresh_send_target_menu(self):
        """刷新发送目标下拉菜单（根据网络节点动态变化）。"""
        self.menu_send_target.clear()
        # 主机
        act_pacs = self.menu_send_target.addAction("发送到主机")
        act_pacs.triggered.connect(lambda checked=False, t="pacs": self._on_process_and_send(t))
        # DSA 节点
        for idx, node in enumerate(self._dsa_nodes):
            name = node.get("name", f"DSA-{idx+1}")
            act = self.menu_send_target.addAction(f"发送到 {name}")
            act.triggered.connect(lambda checked=False, t=f"dsa:{idx}": self._on_process_and_send(t))

    def _on_dsa_add(self):
        """添加 DSA 节点"""
        dialog = DsaNodeEditDialog(parent=self)
        if dialog.exec() == QDialog.Accepted:
            self._dsa_nodes.append(dialog.get_node())
            self._refresh_dsa_table()
            self._refresh_send_target_menu()

    def _on_dsa_edit(self):
        """编辑选中的 DSA 节点"""
        selected = self.dsa_nodes_table.selectionModel().selectedRows()
        if not selected:
            QMessageBox.warning(self, "提示", "请先选择一个 DSA 节点")
            return
        row = selected[0].row()
        if row < 0 or row >= len(self._dsa_nodes):
            return
        dialog = DsaNodeEditDialog(node=self._dsa_nodes[row], parent=self)
        if dialog.exec() == QDialog.Accepted:
            self._dsa_nodes[row] = dialog.get_node()
            self._refresh_dsa_table()
            self._refresh_send_target_menu()

    def _on_dsa_delete(self):
        """删除选中的 DSA 节点"""
        selected = self.dsa_nodes_table.selectionModel().selectedRows()
        if not selected:
            QMessageBox.warning(self, "提示", "请先选择一个 DSA 节点")
            return
        row = selected[0].row()
        if row < 0 or row >= len(self._dsa_nodes):
            return
        node = self._dsa_nodes[row]
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除 DSA 节点 \"{node.get('name', '')}\" 吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self._dsa_nodes.pop(row)
            self._refresh_dsa_table()
            self._refresh_send_target_menu()

    def _init_bottom_bar(self):
        """底部信息栏：显示当前目标患者摘要"""
        layout = QHBoxLayout()
        layout.setSpacing(12)

        # 目标患者摘要
        self.lbl_target_summary = QLabel("目标患者: [未选择]")
        self.lbl_target_summary.setStyleSheet("font-weight: 600; color: #111827;")
        layout.addWidget(self.lbl_target_summary)

        self.centralWidget().layout().addLayout(layout)

    # ---------- 内部信号连接 ----------

    def _connect_internal_signals(self):
        """连接来自工作线程的信号到 UI 更新槽函数"""
        self.input_signals.study_received.connect(self._on_study_received)
        self.input_signals.scp_status_changed.connect(self._on_scp_status_changed)
        self.input_signals.local_load_progress.connect(self._on_local_load_progress)
        self.input_signals.local_load_finished.connect(self._on_local_load_finished)
        self.input_signals.scp_file_received.connect(self._on_scp_file_received)
        self.input_signals.error_occurred.connect(self._show_error)

        self.network_signals.find_results_ready.connect(self._on_find_results_ready)
        self.network_signals.store_progress.connect(self._on_store_progress)
        self.network_signals.store_finished.connect(self._on_store_finished)
        self.network_signals.error_occurred.connect(self._show_error)

        # PACS C-MOVE 进度
        self.network_signals.pacs_move_progress.connect(self._on_pacs_move_progress)
        self.network_signals.pacs_move_finished.connect(self._on_pacs_move_finished)

        # DSA C-MOVE 进度
        self.network_signals.dsa_move_progress.connect(self._on_dsa_move_progress)
        self.network_signals.dsa_move_finished.connect(self._on_dsa_move_finished)

        # DSA 信号（查询/拉取通过弹窗临时连接，此处无需永久连接）

        self.processor_signals.process_progress.connect(self.progress_bar.setValue)
        self.processor_signals.process_finished.connect(self._on_process_finished)
        self.processor_signals.error_occurred.connect(self._show_error)

        # 左侧树勾选状态变化时更新底部目标摘要
        self.tree_model.dataChanged.connect(self._on_tree_check_changed)

        # DSA 查看器加载进度
        self.dsa_viewer.load_progress.connect(self._on_viewer_load_progress)
        self.dsa_viewer.series_loaded.connect(self._on_series_loaded)

        # DSA 查看器序列导航
        self.dsa_viewer.prev_series_requested.connect(self._on_prev_series)
        self.dsa_viewer.next_series_requested.connect(self._on_next_series)

        # DSA 查看器状态栏提示
        self.dsa_viewer.status_message.connect(self.status_bar.showMessage)

    # ---------- 槽函数 / 事件处理 ----------

    def _on_scp_toggle(self, checked: bool):
        """SCP 启动/停止按钮切换"""
        if checked:
            self.request_start_scp.emit()
        else:
            self.request_stop_scp.emit()

    def _on_scp_status_changed(self, running: bool, message: str):
        """SCP 状态变化回调"""
        self._scp_running = running
        self.btn_scp_toggle.setChecked(running)
        self.btn_scp_toggle.setIcon(FluentIcon.CLOUD_DOWNLOAD.icon() if running else FluentIcon.CLOUD.icon())
        self.btn_scp_toggle.setToolTip("停止 SCP 接收" if running else "启动 SCP 接收")
        self.status_bar.showMessage(message)
        # 实时同步到已打开的查询弹窗
        scp_ae = self.edit_scp_ae_title.text().strip() if hasattr(self, 'edit_scp_ae_title') else "MIX_SCP"
        if hasattr(self, '_dsa_query_dialog') and self._dsa_query_dialog:
            self._dsa_query_dialog.set_scp_running(running, scp_ae)
        if hasattr(self, '_pacs_query_dialog') and self._pacs_query_dialog:
            self._pacs_query_dialog.set_scp_running(running, scp_ae)

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

    def _on_scp_file_received(self, count: int):
        """SCP 接收文件进度（无总量，持续累加显示）"""
        self.progress_bar.setMaximum(max(count, 1))
        self.progress_bar.setValue(count)
        self.status_bar.showMessage(f"SCP 接收中: 已接收 {count} 个文件")

    def _on_dsa_move_progress(self, current: int, total: int):
        """DSA C-MOVE 拉取进度"""
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(current)
            self.status_bar.showMessage(f"DSA 拉取进度: {current}/{total}")

    def _on_dsa_move_finished(self, success: int, total: int):
        """DSA C-MOVE 拉取完成"""
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(success)
        self.status_bar.showMessage(f"DSA 拉取完成: {success}/{total}")

    def _on_pacs_move_progress(self, current: int, total: int):
        """PACS C-MOVE 拉取进度"""
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(current)
            self.status_bar.showMessage(f"主机拉取进度: {current}/{total}")

    def _on_pacs_move_finished(self, success: int, total: int):
        """PACS C-MOVE 拉取完成"""
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(success)
        self.status_bar.showMessage(f"主机拉取完成: {success}/{total}")

    def _on_viewer_load_progress(self, current: int, total: int):
        """DSA 查看器加载图像进度"""
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(current)
            self.status_bar.showMessage(f"正在加载图像: {current}/{total}")
            # 加载完成后恢复进度条
            if current >= total:
                QTimer.singleShot(500, lambda: self.progress_bar.setValue(0))

    def _on_series_loaded(self, total_frames: int):
        """DSA 查看器序列加载完成"""
        self.status_bar.showMessage(f"加载完成: {total_frames} 帧")

    def _on_study_received(self, study_uid: str, patient_name: str):
        """SCP 接收到新 Study 的回调（由外部触发更新树模型）"""
        self.status_bar.showMessage(f"接收到新检查: {patient_name} (UID: {study_uid})")
        # 注意：实际刷新树模型应由外部逻辑调用 add_study，这里仅做状态提示

    def _on_show_pacs_query(self):
        """显示主机查询弹窗（非模态，不阻塞主窗口）"""
        if hasattr(self, '_pacs_query_dialog') and self._pacs_query_dialog:
            self._pacs_query_dialog.raise_()
            self._pacs_query_dialog.activateWindow()
            return

        scp_ae = self.edit_scp_ae_title.text().strip() if hasattr(self, 'edit_scp_ae_title') else "MIX_SCP"
        dialog = PacsQueryDialog(scp_ae, self._scp_running, self)
        self._pacs_query_dialog = dialog
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.request_find.connect(self.request_pacs_find.emit)
        dialog.request_move.connect(self.request_pacs_move.emit)
        self.network_signals.find_results_ready.connect(dialog.on_results_ready)
        self.network_signals.pacs_move_progress.connect(dialog.on_move_progress)
        self.network_signals.pacs_move_finished.connect(dialog.on_move_finished)

        # 恢复上次状态
        if hasattr(self, '_pacs_query_state') and self._pacs_query_state:
            dialog.restore_state(self._pacs_query_state)

        def _on_pacs_accepted():
            if dialog.selected_data:
                self._pacs_selected_data = dialog.selected_data
                self._update_target_summary(dialog.selected_data)
                self.tab_widget.setCurrentIndex(0)
                self.edit_manual_name.clear()
                self.edit_manual_id.clear()
                self.edit_manual_acc.clear()
                self.status_bar.showMessage(
                    f"已选择目标患者: {dialog.selected_data['patient_name']}"
                )

        def _on_pacs_finished():
            # 保存状态
            self._pacs_query_state = dialog.get_state()
            # 断开临时信号连接
            for sig, slot in [
                (self.network_signals.find_results_ready, dialog.on_results_ready),
                (self.network_signals.pacs_move_progress, dialog.on_move_progress),
                (self.network_signals.pacs_move_finished, dialog.on_move_finished),
            ]:
                try:
                    sig.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            self._pacs_query_dialog = None

        dialog.accepted.connect(_on_pacs_accepted)
        dialog.finished.connect(_on_pacs_finished)
        dialog.show()

    def _on_show_dsa_query(self):
        """显示 DSA 查询弹窗（非模态，不阻塞主窗口）"""
        if hasattr(self, '_dsa_query_dialog') and self._dsa_query_dialog:
            self._dsa_query_dialog.raise_()
            self._dsa_query_dialog.activateWindow()
            return

        scp_ae = self.edit_scp_ae_title.text().strip() if hasattr(self, 'edit_scp_ae_title') else "MIX_SCP"
        dialog = DsaQueryDialog(self._dsa_nodes, scp_ae, self._scp_running, self)
        self._dsa_query_dialog = dialog
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.request_find.connect(self.request_dsa_find.emit)
        dialog.request_move.connect(self.request_dsa_move.emit)
        self.network_signals.dsa_find_results_ready.connect(dialog.on_find_results)
        self.network_signals.dsa_move_progress.connect(dialog.on_move_progress)
        self.network_signals.dsa_move_finished.connect(dialog.on_move_finished)

        # 恢复上次状态
        if hasattr(self, '_dsa_query_state') and self._dsa_query_state:
            dialog.restore_state(self._dsa_query_state)

        def _on_dsa_finished():
            # 保存状态
            self._dsa_query_state = dialog.get_state()
            # 断开临时信号连接
            for signal, slot in [
                (self.network_signals.dsa_find_results_ready, dialog.on_find_results),
                (self.network_signals.dsa_move_progress, dialog.on_move_progress),
                (self.network_signals.dsa_move_finished, dialog.on_move_finished),
            ]:
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            self._dsa_query_dialog = None

        dialog.finished.connect(_on_dsa_finished)
        dialog.show()

    def _on_show_worklist_query(self):
        """显示 Worklist 查询弹窗（非模态，不阻塞主窗口）"""
        if hasattr(self, '_worklist_query_dialog') and self._worklist_query_dialog:
            self._worklist_query_dialog.raise_()
            self._worklist_query_dialog.activateWindow()
            return

        dialog = WorklistQueryDialog(self)
        self._worklist_query_dialog = dialog
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.request_find.connect(self.request_worklist_find.emit)
        self.network_signals.worklist_results_ready.connect(dialog.on_results_ready)

        # 恢复上次状态
        if hasattr(self, '_worklist_query_state') and self._worklist_query_state:
            dialog.edit_find_name.setText(self._worklist_query_state.get("patient_name", ""))
            dialog.edit_find_id.setText(self._worklist_query_state.get("patient_id", ""))
            dialog.edit_find_acc.setText(self._worklist_query_state.get("accession_number", ""))
            dialog.edit_modality.setText(self._worklist_query_state.get("modality", ""))
            dialog.edit_station_ae.setText(self._worklist_query_state.get("station_ae", ""))
            idx = self._worklist_query_state.get("date_filter_idx", 0)
            if 0 <= idx < dialog.combo_date_filter.count():
                dialog.combo_date_filter.setCurrentIndex(idx)

        def _on_worklist_finished():
            # 保存状态
            self._worklist_query_state = {
                "patient_name": dialog.edit_find_name.text(),
                "patient_id": dialog.edit_find_id.text(),
                "accession_number": dialog.edit_find_acc.text(),
                "modality": dialog.edit_modality.text(),
                "station_ae": dialog.edit_station_ae.text(),
                "date_filter_idx": dialog.combo_date_filter.currentIndex(),
            }
            # 断开临时信号连接
            try:
                self.network_signals.worklist_results_ready.disconnect(dialog.on_results_ready)
            except (TypeError, RuntimeError):
                pass
            self._worklist_query_dialog = None

        dialog.finished.connect(_on_worklist_finished)
        dialog.show()

    def _on_show_help(self):
        """显示帮助文档弹窗"""
        dialog = HelpDialog(self)
        dialog.exec()

    def _on_show_about(self):
        """显示关于软件弹窗"""
        dialog = AboutDialog(self)
        dialog.exec()

    def _on_show_plugin_manager(self):
        """显示插件管理中心对话框"""
        # PluginManager 实例由 main.py 中的 ApplicationController 提供
        pm = getattr(self, '_plugin_manager', None)
        if pm is None:
            QMessageBox.warning(
                self, "插件中心",
                "插件管理器尚未初始化，请稍后再试。"
            )
            return
        self._plugin_dialog = PluginManagerDialog(pm, parent=self)
        self._plugin_dialog.request_install_plugin.connect(self.request_plugin_install.emit)
        self._plugin_dialog.request_uninstall_plugin.connect(self.request_plugin_uninstall.emit)
        self._plugin_dialog.request_activate_plugin.connect(self.request_plugin_activate.emit)
        self._plugin_dialog.exec()
        # 断开临时连接
        for signal in [
            self._plugin_dialog.request_install_plugin,
            self._plugin_dialog.request_uninstall_plugin,
            self._plugin_dialog.request_activate_plugin,
        ]:
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass
        self._plugin_dialog = None

    @staticmethod
    def _get_slice_count_for_normalization(file_list: List[str]) -> int:
        """获取用于归一化的切片数量。单文件多帧 DICOM 按帧数计算。"""
        count = len(file_list)
        if count == 1:
            try:
                from pydicom import dcmread
                ds = dcmread(file_list[0], stop_before_pixels=True)
                number_of_frames = getattr(ds, 'NumberOfFrames', 1)
                count = max(1, int(number_of_frames))
            except Exception:
                pass
        return count

    def _on_show_normalizer(self):
        """显示异构数据归一化对话框"""
        current = self.tree_view.selectionModel().currentIndex()
        if not current.isValid():
            QMessageBox.warning(self, "提示", "请先在左侧选中一个断层序列")
            return

        item = current.internalPointer()
        if not item or not hasattr(item, 'data'):
            QMessageBox.warning(self, "提示", "请先在左侧选中一个断层序列")
            return

        data = item.data
        node_type = data.get("type", "")
        file_list: List[str] = []
        patient_name = ""
        series_desc = ""

        if "影像" in node_type:
            fpath = data.get("file_path", "")
            if fpath and os.path.isfile(fpath):
                file_list = [fpath]
        elif "序列" in node_type:
            file_list = data.get("instances", [])
            series_desc = data.get("name", "")
            # 向上追溯获取患者名
            parent = item.parent
            if parent and hasattr(parent, 'data'):
                patient_name = parent.data.get("patient_name", "")
        elif "检查" in node_type:
            if hasattr(item, 'children'):
                for child in item.children:
                    child_data = getattr(child, 'data', {})
                    file_list.extend(child_data.get("instances", []))
            patient_name = data.get("patient_name", "")

        normalize_slice_count = self._get_slice_count_for_normalization(file_list)
        if normalize_slice_count < 2:
            QMessageBox.warning(
                self, "提示",
                "归一化需要至少 2 张切片的断层序列。\n"
                "请选中一个包含多张切片的 DSA/CT 重建序列（或多帧 DICOM）。"
            )
            return

        self._normalizer_dialog = NormalizerDialog(
            file_paths=file_list,
            patient_name=patient_name,
            series_desc=series_desc,
            parent=self
        )
        self._normalizer_dialog.request_normalize.connect(self.request_normalize.emit)
        self._normalizer_dialog.exec()
        # 断开临时连接
        try:
            self._normalizer_dialog.request_normalize.disconnect(self.request_normalize.emit)
        except (TypeError, RuntimeError):
            pass
        self._normalizer_dialog = None

    def _on_show_export(self, preset_format: str = None):
        """显示导出对话框"""
        checked = self._get_selected_series()
        if not checked:
            QMessageBox.warning(self, "提示", "请先在左侧勾选需要导出的序列")
            return
        viewer = self.dsa_viewer
        if not viewer or viewer.total_frames == 0:
            QMessageBox.warning(self, "提示", "当前没有加载图像序列")
            return
        self._export_dialog = ExportDialog(
            total_frames=viewer.total_frames,
            frame_size=viewer.frame_size,
            fps=viewer._fps,
            preset_format=preset_format,
            parent=self
        )
        # 连接导出请求信号到主窗口信号（由 main.py 处理实际导出）
        self._export_dialog.request_export.connect(self.request_export.emit)
        self._export_dialog.exec()

        # 断开临时连接
        try:
            self._export_dialog.request_export.disconnect(self.request_export.emit)
        except (TypeError, RuntimeError):
            pass
        self._export_dialog = None

    def _on_export_sequence_mp4(self):
        """导出序列为 MP4"""
        self._on_show_export(preset_format="mp4")

    def _on_export_sequence_png(self):
        """导出序列为 PNG"""
        self._on_show_export(preset_format="png")

    def _on_export_current_frame(self):
        """导出当前帧为单张图片"""
        checked = self._get_selected_series()
        if not checked:
            QMessageBox.warning(self, "提示", "请先在左侧勾选需要导出的序列")
            return
        viewer = self.dsa_viewer
        if not viewer or viewer.total_frames == 0:
            QMessageBox.warning(self, "提示", "当前没有加载图像序列")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "保存当前帧", "frame.png",
            "PNG 图片 (*.png);;JPEG 图片 (*.jpg *.jpeg);;所有文件 (*.*)"
        )
        if not path:
            return

        if viewer.export_current_frame(path):
            QMessageBox.information(self, "导出完成", f"已保存到:\n{path}")
        else:
            QMessageBox.critical(self, "导出失败", "保存图片时出错，请检查路径和权限。")

    def _on_edit_dicom_tags(self):
        """编辑当前选中序列的 DICOM 标签"""
        current = self.tree_view.selectionModel().currentIndex()
        if not current.isValid():
            QMessageBox.warning(self, "提示", "请先在左侧点击选中需要编辑的序列")
            return
        item = current.internalPointer()
        if not item or not hasattr(item, 'data'):
            QMessageBox.warning(self, "提示", "请先在左侧点击选中需要编辑的序列")
            return

        data = item.data
        node_type = data.get("type", "")
        file_path = ""

        if "影像" in node_type:
            file_path = data.get("file_path", "")
        elif "序列" in node_type:
            instances = data.get("instances", [])
            if instances:
                file_path = instances[0]
        elif "检查" in node_type:
            if hasattr(item, 'children'):
                for child in item.children:
                    child_data = getattr(child, 'data', {})
                    instances = child_data.get("instances", [])
                    if instances:
                        file_path = instances[0]
                        break

        if not file_path or not os.path.isfile(file_path):
            QMessageBox.warning(self, "提示", "未找到可编辑的 DICOM 文件")
            return
        dialog = DicomTagEditorDialog(file_path, parent=self)
        dialog.exec()

    def _on_refresh_tree(self):
        """手动刷新左侧树（可由外部业务逻辑实现）"""
        # 占位：通常由 main.py 协调调用 dicom_input 扫描临时目录后重新加载
        pass

    def _on_clear_tree(self):
        """清空左侧树列表并释放 Viewer 缓存（不删除磁盘文件）"""
        self.tree_model.clear()
        if hasattr(self, 'dsa_viewer') and self.dsa_viewer:
            self.dsa_viewer.clear()
        self.btn_edit_dicom.setEnabled(False)
        self.status_bar.showMessage("已清空源数据列表")

    def _on_tree_context_menu(self, position):
        """左侧树列表右键菜单。"""
        index = self.tree_view.indexAt(position)
        if not index.isValid():
            return

        # 右键点击时选中该项
        self.tree_view.selectionModel().setCurrentIndex(
            index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows
        )

        item = index.internalPointer()
        if not item:
            return

        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #1f2937;
                color: #f9fafb;
                border: 1px solid #374151;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 20px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #333333;
            }
        """)

        # 根据项级别显示不同文本
        if item.parent == self.tree_model.root_item:
            action_text = "移除该病人"
        elif item.parent and item.parent.parent == self.tree_model.root_item:
            action_text = "移除该序列"
        else:
            action_text = "移除"

        action_remove = menu.addAction(action_text)
        action_remove.triggered.connect(self._on_remove_selected_studies)
        menu.exec(self.tree_view.viewport().mapToGlobal(position))

    def _on_remove_selected_studies(self):
        """从列表中移除勾选的 Study 或 Series 节点，不删除磁盘文件。
        仅依据复选框勾选状态进行批量移除，与高亮选中（加载预览）互不干扰。"""
        # 收集所有复选框 Checked 的项
        all_items = {}
        def _collect_checked(item):
            if item.checked_state == Qt.CheckState.Checked:
                all_items[id(item)] = item
            for child in item.children:
                _collect_checked(child)
        _collect_checked(self.tree_model.root_item)

        if not all_items:
            QMessageBox.information(self, "提示", "请先勾选左侧列表中需要移除的数据")
            return

        studies_to_remove = []
        series_to_remove = []
        names = []

        for item in all_items.values():
            if item.parent == self.tree_model.root_item:
                # Study 级别
                if item not in studies_to_remove:
                    studies_to_remove.append(item)
                    names.append(item.data.get("name", "未知"))
            elif item.parent and item.parent.parent == self.tree_model.root_item:
                # Series 级别
                if item not in series_to_remove:
                    series_to_remove.append(item)
                    names.append(
                        f"{item.parent.data.get('name', '未知')} / {item.data.get('name', '未命名序列')}"
                    )
            else:
                # Instance 级别，向上追溯到 Series
                while item.parent and item.parent.parent != self.tree_model.root_item:
                    item = item.parent
                if item.parent and item.parent.parent == self.tree_model.root_item:
                    if item not in series_to_remove:
                        series_to_remove.append(item)
                        names.append(
                            f"{item.parent.data.get('name', '未知')} / {item.data.get('name', '未命名序列')}"
                        )

        # 若 Series 的父 Study 已在删除列表，则不需要单独删除该 Series
        series_to_remove = [s for s in series_to_remove if s.parent not in studies_to_remove]

        if not studies_to_remove and not series_to_remove:
            return

        names_text = "\n".join(f"  - {n}" for n in names[:10])
        if len(names) > 10:
            names_text += f"\n  ... 等共 {len(names)} 项"

        reply = QMessageBox.question(
            self, "确认移除",
            f"即将从列表中移除以下数据：\n{names_text}\n\n"
            f"磁盘文件不会被删除，刷新后仍可重新加载。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        try:
            removed_studies, removed_series = self.tree_model.delete_items(
                studies_to_remove, series_to_remove
            )
        except Exception as e:
            QMessageBox.critical(self, "移除失败", f"删除过程中发生错误：{e}")
            return

        # 如果当前查看的序列被移除，清空 Viewer
        if hasattr(self, 'dsa_viewer') and self.dsa_viewer:
            self.dsa_viewer.clear()

        parts = []
        if removed_studies:
            parts.append(f"{removed_studies} 个病人")
        if removed_series:
            parts.append(f"{removed_series} 个序列")
        msg = f"已从列表中移除 {', '.join(parts)}"
        self.status_bar.showMessage(msg)
        QMessageBox.information(self, "完成", msg)

        # 删除后清除高亮选中，避免自动跳到下一项
        self.tree_view.selectionModel().clearSelection()
        self.tree_view.selectionModel().clearCurrentIndex()

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

    def _on_prev_series(self):
        """切换到上一序列/影像：选中序列时切换序列，选中影像时切换影像。"""
        current = self.tree_view.selectionModel().currentIndex()
        if not current.isValid():
            return

        item = current.internalPointer()
        if not item:
            return

        # 判断当前是否选中影像节点
        is_instance = "影像" in item.data.get("type", "")

        # 向上找到序列节点
        series_item = item
        while series_item and series_item.parent and series_item.parent != self.tree_model.root_item:
            if series_item.data.get("type", "") == "序列":
                break
            series_item = series_item.parent
        if not series_item or series_item.data.get("type", "") != "序列":
            return

        # 只有当前选中的是影像节点时，才在同一序列下切换影像
        if is_instance:
            prev_row = item.row() - 1
            if prev_row >= 0:
                prev_instance = series_item.child(prev_row)
                if prev_instance:
                    prev_index = self.tree_model.createIndex(prev_row, 0, prev_instance)
                    self.tree_view.setCurrentIndex(prev_index)
                    self.tree_view.selectionModel().select(
                        prev_index, QAbstractItemView.ClearAndSelect | QAbstractItemView.Rows
                    )
                    return

        # 切换到上一个序列（选中序列节点本身）
        parent = series_item.parent
        if not parent:
            return
        prev_row = series_item.row() - 1
        if prev_row < 0:
            self.status_bar.showMessage("已经是第一个序列", 3000)
            return
        prev_series = parent.child(prev_row)
        if not prev_series:
            return

        prev_index = self.tree_model.createIndex(prev_row, 0, prev_series)
        self.tree_view.setCurrentIndex(prev_index)
        self.tree_view.selectionModel().select(
            prev_index, QAbstractItemView.ClearAndSelect | QAbstractItemView.Rows
        )

    def _on_next_series(self):
        """切换到下一序列/影像：选中序列时切换序列，选中影像时切换影像。"""
        current = self.tree_view.selectionModel().currentIndex()
        if not current.isValid():
            return

        item = current.internalPointer()
        if not item:
            return

        # 判断当前是否选中影像节点
        is_instance = "影像" in item.data.get("type", "")

        # 向上找到序列节点
        series_item = item
        while series_item and series_item.parent and series_item.parent != self.tree_model.root_item:
            if series_item.data.get("type", "") == "序列":
                break
            series_item = series_item.parent
        if not series_item or series_item.data.get("type", "") != "序列":
            return

        # 只有当前选中的是影像节点时，才在同一序列下切换影像
        if is_instance:
            next_row = item.row() + 1
            if next_row < series_item.child_count():
                next_instance = series_item.child(next_row)
                if next_instance:
                    next_index = self.tree_model.createIndex(next_row, 0, next_instance)
                    self.tree_view.setCurrentIndex(next_index)
                    self.tree_view.selectionModel().select(
                        next_index, QAbstractItemView.ClearAndSelect | QAbstractItemView.Rows
                    )
                    return

        # 切换到下一个序列（选中序列节点本身）
        parent = series_item.parent
        if not parent:
            return
        next_row = series_item.row() + 1
        if next_row >= parent.child_count():
            self.status_bar.showMessage("已经是最后一个序列", 3000)
            return
        next_series = parent.child(next_row)
        if not next_series:
            return

        next_index = self.tree_model.createIndex(next_row, 0, next_series)
        self.tree_view.setCurrentIndex(next_index)
        self.tree_view.selectionModel().select(
            next_index, QAbstractItemView.ClearAndSelect | QAbstractItemView.Rows
        )

    def _on_tree_selection_changed(self, current: QModelIndex, previous: QModelIndex):
        """
        树节点选择变化：加载图像到 DSA 查看器。
        高亮选中（变蓝色）仅用于加载预览，与复选框勾选（标记可移除/处理）完全独立。
        """
        has_selection = False
        can_normalize = False
        normalize_file_count = 0
        if current.isValid():
            item = current.internalPointer()
            if item and hasattr(item, 'data'):
                data = item.data
                node_type = data.get("type", "")
                file_list: List[str] = []

                if "影像" in node_type:
                    fpath = data.get("file_path", "")
                    if fpath and os.path.isfile(fpath):
                        file_list = [fpath]
                elif "序列" in node_type:
                    file_list = data.get("instances", [])
                elif "检查" in node_type:
                    if hasattr(item, 'children'):
                        for child in item.children:
                            child_data = getattr(child, 'data', {})
                            file_list.extend(child_data.get("instances", []))

                has_selection = bool(file_list)
                normalize_file_count = self._get_slice_count_for_normalization(file_list)
                # 归一化需要至少2张切片（断层图像或多帧DICOM）
                can_normalize = normalize_file_count >= 2

                # 自动将当前选中 study 的患者信息填入底部目标摘要（作为默认值）
                self._update_target_from_tree_item(item)

                if file_list:
                    self.dsa_viewer.load_series(file_list)
                    self.status_bar.showMessage(
                        f"正在加载 {data.get('name', '')}: {len(file_list)} 个文件..."
                    )

        self.btn_edit_dicom.setEnabled(has_selection)
        self.btn_normalize.setEnabled(can_normalize)
        # 更新归一化按钮 tooltip
        if can_normalize:
            self.btn_normalize.setToolTip(f"异构数据归一化 ({normalize_file_count} 张切片)")
        else:
            self.btn_normalize.setToolTip("异构数据归一化 (需选中断层序列且≥2张切片)")

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
        """树形控件复选框状态变化时，根据勾选的序列更新底部目标摘要和工具栏按钮状态。"""
        checked = self.tree_model.get_checked_series()
        has_checked = bool(checked)

        # 更新底部目标摘要
        if has_checked:
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

        # 更新工具栏按钮状态（勾选才算被选中）
        self.btn_send_menu.setEnabled(has_checked)
        self.btn_export_local_toolbar.setEnabled(has_checked)

    def _on_pacs_find(self):
        """点击主机查询按钮"""
        query = {
            "patient_name": self.edit_find_name.text().strip(),
            "patient_id": self.edit_find_id.text().strip(),
            "accession_number": self.edit_find_acc.text().strip(),
        }
        self.status_bar.showMessage("正在查询主机 ...")
        self.pacs_result_model.clear()
        self.pacs_result_model.setHorizontalHeaderLabels(["患者姓名", "患者ID", "性别", "年龄", "出生日期", "检查号", "检查UID"])
        self.request_pacs_find.emit(query)

    def _on_find_results_ready(self, results: List[Dict]):
        """主机查询结果返回"""
        for r in results:
            self.pacs_result_model.add_result(r)
        self.status_bar.showMessage(f"主机查询完成，返回 {len(results)} 条记录")

    def _on_pacs_selection_changed(self, current: QModelIndex, previous: QModelIndex):
        """主机查询结果表格选中行变化：自动填充到手动输入表单"""
        if current.isValid():
            data = self.pacs_result_model.get_selected_data(current.row())
            self.lbl_pacs_selected.setText(
                f"已选择: {data['patient_name']} | ID: {data['patient_id']} | Acc: {data['accession_number']}"
            )
            self._update_target_summary(data)
            # 自动填充到手动输入表单
            self.edit_manual_name.setText(data.get("patient_name", ""))
            self.edit_manual_id.setText(data.get("patient_id", ""))
            self.edit_manual_acc.setText(data.get("accession_number", ""))
            self.edit_manual_study_uid.setText(data.get("study_instance_uid", ""))
            self.edit_manual_sex.setText(data.get("patient_sex", ""))
            self.edit_manual_age.setText(data.get("patient_age", ""))
            self.edit_manual_birth.setText(data.get("patient_birth_date", ""))
        else:
            self.lbl_pacs_selected.setText("未选择目标患者")

    def _get_target_patient_info(self) -> Optional[Dict]:
        """
        获取当前界面选中的目标患者信息。
        从手动输入表单中读取（主机查询结果会自动填充到表单）。
        若信息不完整则返回 None，并弹出提示。
        """
        name = self.edit_manual_name.text().strip()
        pid = self.edit_manual_id.text().strip()
        acc = self.edit_manual_acc.text().strip()
        study_uid = self.edit_manual_study_uid.text().strip()

        if not name or not pid or not acc:
            self.lbl_manual_hint.setText("请填写所有必填项（带 * 号）")
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
        if self.tab_widget.currentIndex() != 0:
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
            # 手动输入模式
            self._on_manual_input_changed()
        else:
            # 网络配置 Tab
            self.lbl_target_summary.setText("目标患者: [未选择]")

    def _get_selected_series(self) -> List[Dict]:
        """获取左侧树形控件中被勾选的序列"""
        return self.tree_model.get_checked_series()

    def _on_process_and_send(self, send_target: str = "pacs"):
        """点击：应用拆分并发送"""
        target = self._get_target_patient_info()
        if target is None:
            reply = QMessageBox.question(
                self, "提示",
                "未填写目标患者信息（姓名/ID/检查号）。\n"
                "点击「是」将按原始患者信息发送，点击「否」返回填写。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
            target = {}  # 空字典表示使用原始信息
        series_list = self._get_selected_series()
        if not series_list:
            QMessageBox.warning(self, "提示", "请先在左侧勾选需要拆分的序列")
            return
        self.status_bar.showMessage("正在处理并发送 ...")
        self.progress_bar.setValue(0)
        self.request_process_and_store.emit(series_list, target, send_target)

    def _on_process_and_export(self):
        """点击：应用拆分并导出到本地"""
        target = self._get_target_patient_info()
        if target is None:
            # 必填项为空时提示用户，允许按原始信息导出
            reply = QMessageBox.question(
                self, "提示",
                "未填写目标患者信息（姓名/ID/检查号）。\n"
                "点击「是」将按原始患者信息导出，点击「否」返回填写。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
            target = {}  # 空字典表示使用原始信息
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
        QMessageBox.information(self, "完成", f"成功发送 {success}/{total} 个文件到主机")

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
    def _menu_style() -> str:
        """统一下拉菜单的 QSS 样式。"""
        return """
            QMenu {
                background-color: #ffffff;
                color: #1a2a3a;
                border: 1px solid #c8c8c8;
                border-radius: 8px;
                padding: 6px;
            }
            QMenu::item {
                padding: 8px 24px;
                border-radius: 6px;
                font-size: 13px;
            }
            QMenu::item:selected {
                background-color: #d1fae5;
                color: #333333;
            }
            QMenu::separator {
                height: 1px;
                background-color: #c8c8c8;
                margin: 6px 12px;
            }
        """

    @staticmethod
    def _get_temp_dir() -> str:
        """获取临时目录路径（优先使用用户在设置中指定的路径）。"""
        from config import get_temp_dir
        return get_temp_dir()

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
