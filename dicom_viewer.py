# -*- coding: utf-8 -*-
"""
dicom_viewer.py
DSA 图像查看器模块

基于 PySide6 原生 QGraphicsView + QImage 实现的多帧 DSA 图像浏览与实时减影组件。

设计原则：
- 不依赖 pyqtgraph，完全使用 Qt 原生 API，稳定性更高。
- 图像数据流：pydicom pixel_array -> numpy 运算 -> QImage -> QPixmap -> QGraphicsPixmapItem。
- 切换序列时显式释放 numpy 缓存，防止 OOM。
- 减影运算使用 float32 精确减法，动态归一化到 0-255 显示。

技术栈：PySide6, numpy, pydicom
"""

import gc
import logging
from typing import List, Optional
from collections import OrderedDict

import numpy as np
from pydicom import dcmread

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSlider,
    QLabel, QCheckBox, QGroupBox, QSpinBox, QGraphicsView,
    QGraphicsScene, QGraphicsPixmapItem, QMenu, QSizePolicy
)
from PySide6.QtCore import Qt, QTimer, Signal, QPoint, QEvent, QThread, QObject
from PySide6.QtGui import QImage, QPixmap, QPainter, QCursor

logger = logging.getLogger("dicom_viewer")

# 全局现代化样式
VIEWER_STYLE = """
QWidget {
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
}
QGroupBox {
    background-color: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
    margin-top: 8px;
    padding-top: 10px;
    padding-bottom: 8px;
    padding-left: 10px;
    padding-right: 10px;
    font-weight: 600;
    color: #1a1a1a;
    font-size: 13px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 8px;
    color: #374151;
}
QPushButton {
    background-color: #333333;
    color: white;
    border: none;
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 600;
    font-size: 13px;
}
QPushButton:hover {
    background-color: #4a4a4a;
}
QPushButton:pressed {
    background-color: #2a2a2a;
}
QPushButton#playBtn {
    background-color: #333333;
    color: white;
    font-size: 14px;
    border: none;
    border-radius: 20px;
    padding: 0px;
    min-width: 40px;
    min-height: 40px;
    max-width: 40px;
    max-height: 40px;
}
QPushButton#playBtn:hover {
    background-color: #4a4a4a;
    border: none;
}
QPushButton#playBtn:pressed {
    background-color: #2a2a2a;
    border: none;
}
QPushButton#playBtn:focus {
    border: none;
}
QToolTip {
    background-color: #1a1a1a;
    color: #f9fafb;
    border: 1px solid #374151;
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
}
QPushButton#maskBtn {
    background-color: #333333;
}
QPushButton#maskBtn:hover {
    background-color: #4a4a4a;
}
QPushButton#resetBtn {
    background-color: #f3f4f6;
    color: #374151;
    border: 1px solid #d1d5db;
}
QPushButton#resetBtn:hover {
    background-color: #e5e7eb;
}
QSlider::groove:horizontal {
    height: 6px;
    background: #c8c8c8;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    width: 16px;
    height: 16px;
    background: #10b981;
    border-radius: 8px;
    margin: -5px 0;
}
QSlider::sub-page:horizontal {
    background: #10b981;
    border-radius: 3px;
}
QCheckBox {
    font-size: 13px;
    color: #374151;
    spacing: 6px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 2px solid #d1d5db;
}
QCheckBox::indicator:checked {
    background-color: #10b981;
    border-color: #10b981;
}
QLabel {
    color: #4b5563;
    font-size: 12px;
}
QSpinBox {
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 4px 8px;
    background: #ffffff;
    font-size: 13px;
}
"""

# 右侧面板通用按钮样式（复用于多个控件）
PANEL_BTN_STYLE = """
QPushButton {
    font-size: 12px; font-weight: 500;
    padding: 4px 8px;
    border: 1px solid #d1d5db; border-radius: 6px;
    background-color: #f9fafb; color: #374151;
}
QPushButton:hover { background-color: #f3f4f6; border-color: #9ca3af; }
QPushButton:pressed { background-color: #e5e7eb; }
QPushButton:disabled { color: #d1d5db; border-color: #e5e7eb; background-color: #f9fafb; }
"""


def _fmt_patient_name(val) -> str:
    """格式化 DICOM 患者姓名字段，将 ^ 替换为空格。"""
    if not val:
        return ""
    name = str(val)
    # pydicom PersonName 可能包含 ^ 分隔符
    return name.replace("^", " ").strip()


class LoadSeriesWorker(QObject):
    """后台线程：扫描 DICOM 文件列表，构建帧元数据。"""
    progress = Signal(int, int)   # (current, total)
    finished = Signal(list, float, float, object, object, str, dict, object)
    # sources, pmin, pmax, dicom_ww, dicom_wl, modality, dicom_info, first_frame
    error = Signal(str)

    def __init__(self, file_list: List[str], parent=None):
        super().__init__(parent)
        self._file_list = file_list

    def run(self):
        sources: List[dict] = []
        pmin, pmax = float("inf"), float("-inf")
        dicom_ww: Optional[float] = None
        dicom_wl: Optional[float] = None
        modality: str = ""
        dicom_info: dict = {}
        first_frame = None

        total_files = len(self._file_list)
        for idx, fpath in enumerate(self._file_list, 1):
            self.progress.emit(idx, total_files)
            try:
                ds = dcmread(fpath, stop_before_pixels=True)
                slope = float(getattr(ds, "RescaleSlope", 1))
                intercept = float(getattr(ds, "RescaleIntercept", 0))
                nframes = int(getattr(ds, "NumberOfFrames", 1))
                rows = int(getattr(ds, "Rows", 0))
                cols = int(getattr(ds, "Columns", 0))

                if not modality and hasattr(ds, "Modality"):
                    modality = str(ds.Modality).upper().strip()
                if dicom_ww is None and hasattr(ds, "WindowWidth"):
                    ww_val = ds.WindowWidth
                    dicom_ww = float(ww_val[0] if isinstance(ww_val, (list, tuple)) else ww_val)
                if dicom_wl is None and hasattr(ds, "WindowCenter"):
                    wl_val = ds.WindowCenter
                    dicom_wl = float(wl_val[0] if isinstance(wl_val, (list, tuple)) else wl_val)
                if not dicom_info:
                    dicom_info = {
                        "patient_name": _fmt_patient_name(getattr(ds, "PatientName", "")),
                        "patient_id": str(getattr(ds, "PatientID", "")),
                        "patient_sex": str(getattr(ds, "PatientSex", "")),
                        "patient_age": str(getattr(ds, "PatientAge", "")),
                        "study_date": str(getattr(ds, "StudyDate", "")),
                        "series_desc": str(getattr(ds, "SeriesDescription", "")),
                        "institution": str(getattr(ds, "InstitutionName", "")),
                        "modality": modality,
                        "positioner_primary_angle": str(getattr(ds, "PositionerPrimaryAngle", "")),
                        "positioner_secondary_angle": str(getattr(ds, "PositionerSecondaryAngle", "")),
                        "protocol_name": str(getattr(ds, "ProtocolName", "")),
                        "sequence_name": str(getattr(ds, "SequenceName", "")),
                    }

                if nframes == 1 and rows > 0 and cols > 0:
                    sources.append({
                        "file_path": fpath,
                        "slice_idx": 0,
                        "slope": slope,
                        "intercept": intercept,
                        "shape": (rows, cols),
                    })
                    if first_frame is None:
                        ds_full = dcmread(fpath, stop_before_pixels=False)
                        arr = ds_full.pixel_array
                        if slope != 1 or intercept != 0:
                            arr = arr.astype(np.float32) * slope + intercept
                        first_frame = arr
                        pmin = min(pmin, float(arr.min()))
                        pmax = max(pmax, float(arr.max()))
                else:
                    ds_full = dcmread(fpath, stop_before_pixels=False)
                    arr = ds_full.pixel_array
                    if arr.ndim == 3:
                        for i in range(arr.shape[0]):
                            sources.append({
                                "file_path": fpath,
                                "slice_idx": i,
                                "slope": slope,
                                "intercept": intercept,
                                "shape": (rows, cols),
                            })
                            if first_frame is None:
                                frame_arr = arr[i]
                                if slope != 1 or intercept != 0:
                                    frame_arr = frame_arr.astype(np.float32) * slope + intercept
                                first_frame = frame_arr
                                pmin = min(pmin, float(frame_arr.min()))
                                pmax = max(pmax, float(frame_arr.max()))
                    elif arr.ndim == 2:
                        sources.append({
                            "file_path": fpath,
                            "slice_idx": 0,
                            "slope": slope,
                            "intercept": intercept,
                            "shape": (rows, cols),
                        })
                        if first_frame is None:
                            if slope != 1 or intercept != 0:
                                arr = arr.astype(np.float32) * slope + intercept
                            first_frame = arr
                            pmin = min(pmin, float(arr.min()))
                            pmax = max(pmax, float(arr.max()))
            except Exception as e:
                logger.warning(f"读取失败 {fpath}: {e}")

        if not sources:
            self.error.emit("没有可加载的有效帧")
            return

        self.finished.emit(sources, pmin, pmax, dicom_ww, dicom_wl, modality, dicom_info, first_frame)


class DSAViewerWidget(QWidget):
    """
    DSA 多帧图像查看器。

    布局：左侧图像区（QGraphicsView，底部悬浮控制条），右侧控制面板。
    支持：多帧加载、循环播放、实时减影、窗宽窗位、滚轮缩放、中键调节 WW/WL。
    """

    mask_frame_changed = Signal(int)
    load_progress = Signal(int, int)  # (当前文件序号, 总文件数)
    series_loaded = Signal(int)       # 加载完成，参数为总帧数
    prev_series_requested = Signal()  # 切换到上一序列
    next_series_requested = Signal()  # 切换到下一序列
    status_message = Signal(str)      # 状态栏提示信息

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._init_state()
        self._init_ui()

    # ---------- 内部状态 ----------

    def _init_state(self):
        # 懒加载架构：元数据存 _frame_sources，像素数据按需加载到 _frame_cache
        self._frame_sources: List[dict] = []   # 每帧的加载信息（文件路径、切片索引、slope/intercept）
        self._frame_cache: OrderedDict = OrderedDict()  # idx -> np.ndarray（LRU 缓存）
        self._max_cache_size: int = 50         # 最大缓存帧数
        self._mask_idx: int = 0
        self._current_idx: int = 0
        self._total_frames: int = 0
        self._subtraction_enabled: bool = False
        self._is_playing: bool = False
        self._fps: int = 15

        self._ww: float = 4096.0
        self._wl: float = 2048.0
        self._auto_wwwl: bool = True

        self._global_pixel_range: float = 1024.0
        self._sub_gain: float = 4.0

        # 自动像素位移（降低减影伪影）
        self._auto_pixel_shift: bool = False
        self._pixel_shift_range: int = 5  # 搜索范围 ±像素

        # 图像反相
        self._invert_enabled: bool = False

        # 锐度/平滑调节（-5=最平滑, 0=原图, +5=最锐化）
        self._sharpness: int = 0

        # 中键调节窗宽窗位状态
        self._middle_dragging: bool = False
        self._middle_drag_start: QPoint = QPoint()
        self._ww_drag_start: float = 4096.0
        self._wl_drag_start: float = 2048.0

        # 右键拖动缩放状态
        self._right_dragging: bool = False
        self._right_drag_started: bool = False
        self._right_press_pos: QPoint = QPoint()
        self._right_drag_start: QPoint = QPoint()
        self._right_scale_start: float = 1.0

        self._play_timer = QTimer(self)
        self._play_timer.timeout.connect(self._on_next_frame)

        # DICOM 标签信息（从首个文件读取，供图像角标显示）
        self._dicom_info: dict = {}

    # ---------- 懒加载核心 ----------

    def _get_frame(self, idx: int) -> np.ndarray:
        """按需加载第 idx 帧，LRU 缓存命中时直接返回。"""
        if idx < 0 or idx >= self._total_frames:
            raise IndexError(f"帧索引越界: {idx} (总数 {self._total_frames})")

        # 缓存命中
        if idx in self._frame_cache:
            self._frame_cache.move_to_end(idx)
            return self._frame_cache[idx]

        # 缓存未命中，从磁盘加载
        source = self._frame_sources[idx]
        ds = dcmread(source["file_path"], stop_before_pixels=False)
        arr = ds.pixel_array
        if arr.ndim == 3:
            arr = arr[source["slice_idx"]]

        slope = source["slope"]
        intercept = source["intercept"]
        if slope != 1 or intercept != 0:
            arr = arr.astype(np.float32) * slope + intercept

        self._frame_cache[idx] = arr

        # LRU 淘汰
        while len(self._frame_cache) > self._max_cache_size:
            self._frame_cache.popitem(last=False)

        return arr

    def _preload_around(self, center_idx: int, radius: int = 5):
        """后台预加载 center_idx 附近的帧（供后续扩展为后台线程）。"""
        for idx in range(max(0, center_idx - radius), min(self._total_frames, center_idx + radius + 1)):
            if idx not in self._frame_cache:
                try:
                    self._get_frame(idx)
                except Exception as e:
                    logger.debug(f"预加载帧 {idx} 失败: {e}")

    # ---------- UI 构建 ----------

    def _init_ui(self):
        self.setStyleSheet(VIEWER_STYLE)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        # --- 左侧：图像区 ---
        left = self._create_image_area()
        layout.addWidget(left, stretch=5)

        # --- 右侧：控制面板 ---
        right = self._create_control_panel()
        layout.addWidget(right, stretch=1)

    def _create_image_area(self) -> QWidget:
        """
        创建图像显示区。

        采用手动布局（无 QLayout），实现：
        - image_panel 在父控件内保持 4:3 比例居中
        - graphics_view 填满 image_panel（滚动条隐藏）
        - overlay_bar 半透明悬浮在 image_panel 底部
        """
        panel = QWidget()
        panel.setStyleSheet("background-color: transparent;")
        panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        vbox = QVBoxLayout(panel)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        # 图像面板：圆角深色背景，内部容纳 graphics_view 和悬浮控制条
        self.image_panel = QWidget()
        self.image_panel.setStyleSheet("""
            background-color: #0f0f0f;
            border-radius: 12px;
        """)
        vbox.addWidget(self.image_panel, stretch=1)

        # QGraphicsView：无边框、无滚动条、抗锯齿
        self.graphics_view = QGraphicsView(self.image_panel)
        self.graphics_view.setRenderHints(
            QPainter.SmoothPixmapTransform | QPainter.Antialiasing
        )
        self.graphics_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.graphics_view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.graphics_view.setDragMode(QGraphicsView.ScrollHandDrag)
        self.graphics_view.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.graphics_view.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.graphics_view.setAlignment(Qt.AlignCenter)
        self.graphics_view.setStyleSheet("border: none; background-color: transparent;")

        # 在 viewport 上安装事件过滤器，捕获中键/右键事件
        self.graphics_view.viewport().installEventFilter(self)

        self.scene = QGraphicsScene(self)
        self.pixmap_item = QGraphicsPixmapItem()
        self.scene.addItem(self.pixmap_item)
        self.graphics_view.setScene(self.scene)

        # ---------- 悬浮控制条（叠加在图像底部） ----------
        self.overlay_bar = QWidget(self.image_panel)
        self.overlay_bar.setStyleSheet("""
            QWidget {
                background-color: rgba(15, 15, 15, 160);
                border-radius: 8px;
                border: 1px solid rgba(255, 255, 255, 12);
            }
        """)

        hbox = QHBoxLayout(self.overlay_bar)
        hbox.setContentsMargins(8, 5, 8, 5)
        hbox.setSpacing(8)

        # 上一帧按钮
        self.btn_prev = QPushButton("◀")
        self.btn_prev.setObjectName("playBtn")
        self.btn_prev.setFixedSize(32, 32)
        self.btn_prev.setToolTip("上一帧")
        self.btn_prev.setCursor(QCursor(Qt.PointingHandCursor))
        self.btn_prev.setFocusPolicy(Qt.NoFocus)
        self.btn_prev.setFlat(True)
        self.btn_prev.clicked.connect(self._on_prev_frame)
        hbox.addWidget(self.btn_prev)

        # 播放/暂停按钮（40x40 圆形，确保完整显示）
        self.btn_play = QPushButton("▶")
        self.btn_play.setObjectName("playBtn")
        self.btn_play.setFixedSize(40, 40)
        self.btn_play.setToolTip("播放 / 暂停")
        self.btn_play.setCursor(QCursor(Qt.PointingHandCursor))
        self.btn_play.setFocusPolicy(Qt.NoFocus)
        self.btn_play.setFlat(True)
        self.btn_play.clicked.connect(self.toggle_play)
        hbox.addWidget(self.btn_play)

        # 下一帧按钮
        self.btn_next = QPushButton("▶")
        self.btn_next.setObjectName("playBtn")
        self.btn_next.setFixedSize(32, 32)
        self.btn_next.setToolTip("下一帧")
        self.btn_next.setCursor(QCursor(Qt.PointingHandCursor))
        self.btn_next.setFocusPolicy(Qt.NoFocus)
        self.btn_next.setFlat(True)
        self.btn_next.clicked.connect(self._on_next_frame_click)
        hbox.addWidget(self.btn_next)

        # 分隔线
        sep = QWidget()
        sep.setFixedSize(1, 20)
        sep.setStyleSheet("background-color: rgba(255,255,255,60);")
        hbox.addWidget(sep)

        # 帧滑块
        self.frame_slider = QSlider(Qt.Horizontal)
        self.frame_slider.setRange(0, 0)
        self.frame_slider.setEnabled(False)
        self.frame_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                height: 6px;
                background: #c8c8c8;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                width: 16px;
                height: 16px;
                background: #10b981;
                border-radius: 8px;
                margin: -5px 0;
            }
            QSlider::sub-page:horizontal {
                background: #10b981;
                border-radius: 3px;
            }
        """)
        self.frame_slider.valueChanged.connect(self._on_slider_changed)
        hbox.addWidget(self.frame_slider, stretch=1)

        # 帧计数标签
        self.lbl_frame = QLabel("0 / 0")
        self.lbl_frame.setStyleSheet(
            "color: #d1d5db; font-size: 13px; font-weight: 500; min-width: 60px;"
        )
        self.lbl_frame.setAlignment(Qt.AlignCenter)
        hbox.addWidget(self.lbl_frame)

        # ---------- 四个角 DICOM 信息角标 ----------
        corner_style = """
            QLabel {
                color: rgba(255, 255, 255, 220);
                font-size: 11px;
                font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
                padding: 2px 4px;
                background-color: transparent;
            }
        """
        self.lbl_corner_tl = QLabel(self.image_panel)
        self.lbl_corner_tl.setStyleSheet(corner_style)
        self.lbl_corner_tl.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        self.lbl_corner_tr = QLabel(self.image_panel)
        self.lbl_corner_tr.setStyleSheet(corner_style)
        self.lbl_corner_tr.setAlignment(Qt.AlignRight | Qt.AlignTop)

        self.lbl_corner_bl = QLabel(self.image_panel)
        self.lbl_corner_bl.setStyleSheet(corner_style)
        self.lbl_corner_bl.setAlignment(Qt.AlignLeft | Qt.AlignBottom)

        self.lbl_corner_br = QLabel(self.image_panel)
        self.lbl_corner_br.setStyleSheet(corner_style)
        self.lbl_corner_br.setAlignment(Qt.AlignRight | Qt.AlignBottom)

        return panel

    def _create_control_panel(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet("background-color: transparent;")
        vbox = QVBoxLayout(panel)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(6)
        vbox.setAlignment(Qt.AlignTop)

        # --- 减影控制 ---
        g1 = QGroupBox("减影控制")
        b1 = QVBoxLayout(g1)
        b1.setSpacing(5)
        b1.setContentsMargins(10, 10, 10, 8)

        # 第一行：复选框 + 蒙片按钮
        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        self.chk_sub = QCheckBox("实时减影")
        self.chk_sub.setStyleSheet("font-size: 13px; font-weight: 500;")
        self.chk_sub.checkStateChanged.connect(self._on_sub_toggled)
        top_row.addWidget(self.chk_sub)

        self.btn_mask = QPushButton("设为蒙片")
        self.btn_mask.setObjectName("maskBtn")
        self.btn_mask.setToolTip("将当前帧设为减影基准 (也可右键图像)")
        self.btn_mask.setCursor(QCursor(Qt.PointingHandCursor))
        self.btn_mask.setMinimumHeight(28)
        self.btn_mask.clicked.connect(self._set_mask)
        top_row.addWidget(self.btn_mask)
        b1.addLayout(top_row)

        self.lbl_mask = QLabel("蒙片: 第 0 帧")
        self.lbl_mask.setStyleSheet("color: #6b7280; font-size: 11px; padding-left: 2px;")
        b1.addWidget(self.lbl_mask)
        self.lbl_mask_hint = QLabel("💡 右键点击图像可快速修改蒙片")
        self.lbl_mask_hint.setStyleSheet("color: #333333; font-size: 10px; padding: 2px;")
        self.lbl_mask_hint.setWordWrap(True)
        b1.addWidget(self.lbl_mask_hint)

        # 增益行：固定标签宽度，统一滑块长度
        gain_row = QHBoxLayout()
        gain_row.setSpacing(6)
        lbl_gain = QLabel("增益")
        lbl_gain.setFixedWidth(42)
        lbl_gain.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        gain_row.addWidget(lbl_gain)
        self.sld_gain = QSlider(Qt.Horizontal)
        self.sld_gain.setRange(1, 20)
        self.sld_gain.setValue(4)
        self.sld_gain.valueChanged.connect(self._on_gain_changed)
        gain_row.addWidget(self.sld_gain, stretch=1)
        self.lbl_gain = QLabel("4x")
        self.lbl_gain.setFixedWidth(36)
        self.lbl_gain.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_gain.setStyleSheet("font-size: 11px; color: #6b7280;")
        gain_row.addWidget(self.lbl_gain)
        b1.addLayout(gain_row)

        # 自动像素位移复选框单独一行
        self.chk_auto_shift = QCheckBox("自动像素位移")
        self.chk_auto_shift.setStyleSheet("font-size: 12px;")
        self.chk_auto_shift.setToolTip("自动平移蒙片对齐当前帧，降低减影伪影")
        self.chk_auto_shift.checkStateChanged.connect(self._on_auto_shift_toggled)
        b1.addWidget(self.chk_auto_shift)

        # 位移范围行
        shift_row = QHBoxLayout()
        shift_row.setSpacing(6)
        lbl_shift = QLabel("范围")
        lbl_shift.setFixedWidth(42)
        lbl_shift.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        shift_row.addWidget(lbl_shift)
        self.sld_shift = QSlider(Qt.Horizontal)
        self.sld_shift.setRange(1, 10)
        self.sld_shift.setValue(5)
        self.sld_shift.valueChanged.connect(self._on_shift_range_changed)
        shift_row.addWidget(self.sld_shift, stretch=1)
        self.lbl_shift = QLabel("±5")
        self.lbl_shift.setFixedWidth(36)
        self.lbl_shift.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_shift.setStyleSheet("font-size: 11px; color: #6b7280;")
        shift_row.addWidget(self.lbl_shift)
        b1.addLayout(shift_row)

        vbox.addWidget(g1)

        # --- 窗宽窗位 ---
        g2 = QGroupBox("窗宽窗位")
        b2 = QVBoxLayout(g2)
        b2.setSpacing(5)
        b2.setContentsMargins(10, 10, 10, 8)

        # 窗宽行
        ww_row = QHBoxLayout()
        ww_row.setSpacing(6)
        lbl_ww = QLabel("WW")
        lbl_ww.setFixedWidth(42)
        lbl_ww.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        ww_row.addWidget(lbl_ww)
        self.sld_ww = QSlider(Qt.Horizontal)
        self.sld_ww.setRange(1, 8192)
        self.sld_ww.setValue(4096)
        self.sld_ww.valueChanged.connect(self._on_wwwl_changed)
        ww_row.addWidget(self.sld_ww, stretch=1)
        self.lbl_ww_val = QLabel("4096")
        self.lbl_ww_val.setFixedWidth(36)
        self.lbl_ww_val.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_ww_val.setStyleSheet("font-size: 11px; color: #6b7280;")
        ww_row.addWidget(self.lbl_ww_val)
        b2.addLayout(ww_row)

        # 窗位行
        wl_row = QHBoxLayout()
        wl_row.setSpacing(6)
        lbl_wl = QLabel("WL")
        lbl_wl.setFixedWidth(42)
        lbl_wl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        wl_row.addWidget(lbl_wl)
        self.sld_wl = QSlider(Qt.Horizontal)
        self.sld_wl.setRange(-2048, 8192)
        self.sld_wl.setValue(2048)
        self.sld_wl.valueChanged.connect(self._on_wwwl_changed)
        wl_row.addWidget(self.sld_wl, stretch=1)
        self.lbl_wl_val = QLabel("2048")
        self.lbl_wl_val.setFixedWidth(36)
        self.lbl_wl_val.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_wl_val.setStyleSheet("font-size: 11px; color: #6b7280;")
        wl_row.addWidget(self.lbl_wl_val)
        b2.addLayout(wl_row)

        self.lbl_wwwl = QLabel("WW: 4096  WL: 2048")
        self.lbl_wwwl.setStyleSheet("color: #6b7280; font-size: 11px; font-weight: 500; padding-left: 2px;")
        b2.addWidget(self.lbl_wwwl)

        # 反相行
        invert_row = QHBoxLayout()
        invert_row.setSpacing(6)
        self.chk_invert = QCheckBox("图像反相")
        self.chk_invert.setStyleSheet("font-size: 12px;")
        self.chk_invert.setToolTip("反转图像灰度（白变黑，黑变白）")
        self.chk_invert.checkStateChanged.connect(self._on_invert_toggled)
        invert_row.addWidget(self.chk_invert)
        invert_row.addStretch()
        b2.addLayout(invert_row)

        # 锐度行（标签+滑块+数值，和 WW/WL 统一）
        sharp_row = QHBoxLayout()
        sharp_row.setSpacing(6)
        lbl_sharp = QLabel("锐度")
        lbl_sharp.setFixedWidth(42)
        lbl_sharp.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        sharp_row.addWidget(lbl_sharp)
        self.sld_sharp = QSlider(Qt.Horizontal)
        self.sld_sharp.setRange(-5, 5)
        self.sld_sharp.setValue(0)
        self.sld_sharp.valueChanged.connect(self._on_sharpness_changed)
        sharp_row.addWidget(self.sld_sharp, stretch=1)
        self.lbl_sharp = QLabel("原图")
        self.lbl_sharp.setFixedWidth(36)
        self.lbl_sharp.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_sharp.setStyleSheet("font-size: 11px; color: #6b7280;")
        sharp_row.addWidget(self.lbl_sharp)
        b2.addLayout(sharp_row)

        hint = QLabel("💡 中键拖动调节 WW/WL，右键拖动缩放")
        hint.setStyleSheet("color: #333333; font-size: 10px; padding: 2px;")
        hint.setWordWrap(True)
        b2.addWidget(hint)

        self.btn_reset_wwwl = QPushButton("重置图像")
        self.btn_reset_wwwl.setObjectName("resetBtn")
        self.btn_reset_wwwl.setCursor(QCursor(Qt.PointingHandCursor))
        self.btn_reset_wwwl.setMinimumHeight(28)
        self.btn_reset_wwwl.clicked.connect(self._reset_wwwl)
        b2.addWidget(self.btn_reset_wwwl)

        vbox.addWidget(g2)

        # --- 播放 ---
        g3 = QGroupBox("播放")
        b3 = QVBoxLayout(g3)
        b3.setSpacing(5)
        b3.setContentsMargins(10, 10, 10, 8)

        # 帧率行
        fps_row = QHBoxLayout()
        fps_row.setSpacing(6)
        lbl_fps = QLabel("FPS")
        lbl_fps.setFixedWidth(42)
        lbl_fps.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        fps_row.addWidget(lbl_fps)
        self.slider_fps = QSlider(Qt.Horizontal)
        self.slider_fps.setRange(1, 60)
        self.slider_fps.setValue(15)
        self.slider_fps.valueChanged.connect(self._on_fps_changed)
        fps_row.addWidget(self.slider_fps, stretch=1)
        self.lbl_fps = QLabel("15")
        self.lbl_fps.setFixedWidth(36)
        self.lbl_fps.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_fps.setStyleSheet("font-size: 11px; color: #374151;")
        fps_row.addWidget(self.lbl_fps)
        b3.addLayout(fps_row)

        # 播放控制按钮（等宽排列）
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(6)
        ctrl_row.addStretch()
        self.btn_panel_prev = QPushButton("◀")
        self.btn_panel_prev.setFixedSize(40, 32)
        self.btn_panel_prev.setStyleSheet(PANEL_BTN_STYLE)
        self.btn_panel_prev.setToolTip("上一帧")
        self.btn_panel_prev.setCursor(QCursor(Qt.PointingHandCursor))
        self.btn_panel_prev.clicked.connect(self._on_prev_frame)
        ctrl_row.addWidget(self.btn_panel_prev)

        self.btn_panel_play = QPushButton("▶")
        self.btn_panel_play.setFixedSize(40, 32)
        self.btn_panel_play.setStyleSheet(PANEL_BTN_STYLE)
        self.btn_panel_play.setToolTip("播放 / 暂停")
        self.btn_panel_play.setCursor(QCursor(Qt.PointingHandCursor))
        self.btn_panel_play.clicked.connect(self.toggle_play)
        ctrl_row.addWidget(self.btn_panel_play)

        self.btn_panel_next = QPushButton("▶")
        self.btn_panel_next.setFixedSize(40, 32)
        self.btn_panel_next.setStyleSheet(PANEL_BTN_STYLE)
        self.btn_panel_next.setToolTip("下一帧")
        self.btn_panel_next.setCursor(QCursor(Qt.PointingHandCursor))
        self.btn_panel_next.clicked.connect(self._on_next_frame_click)
        ctrl_row.addWidget(self.btn_panel_next)
        ctrl_row.addStretch()
        b3.addLayout(ctrl_row)

        # 序列导航
        nav_row = QHBoxLayout()
        nav_row.setSpacing(6)
        self.btn_prev_series = QPushButton("◀ 上一序列")
        self.btn_prev_series.setMinimumHeight(28)
        self.btn_prev_series.setStyleSheet(PANEL_BTN_STYLE)
        self.btn_prev_series.setToolTip("切换到上一序列")
        self.btn_prev_series.setCursor(QCursor(Qt.PointingHandCursor))
        self.btn_prev_series.clicked.connect(self.prev_series_requested.emit)
        nav_row.addWidget(self.btn_prev_series)
        self.btn_next_series = QPushButton("下一序列 ▶")
        self.btn_next_series.setMinimumHeight(28)
        self.btn_next_series.setStyleSheet(PANEL_BTN_STYLE)
        self.btn_next_series.setToolTip("切换到下一序列")
        self.btn_next_series.setCursor(QCursor(Qt.PointingHandCursor))
        self.btn_next_series.clicked.connect(self.next_series_requested.emit)
        nav_row.addWidget(self.btn_next_series)
        b3.addLayout(nav_row)

        zoom_hint = QLabel("💡 滚轮切换帧")
        zoom_hint.setStyleSheet("color: #6b7280; font-size: 10px; padding: 2px;")
        b3.addWidget(zoom_hint)

        vbox.addWidget(g3)
        vbox.addStretch()
        return panel

    # ---------- 尺寸与比例（手动布局） ----------

    def resizeEvent(self, event):
        """窗口尺寸变化时重新计算图像区与悬浮控制条位置。"""
        super().resizeEvent(event)
        # 延迟执行，确保 QHBoxLayout 已完成子控件尺寸分配
        QTimer.singleShot(10, self._layout_image_area)

    def showEvent(self, event):
        """首次显示时强制布局一次。"""
        super().showEvent(event)
        QTimer.singleShot(10, self._layout_image_area)

    def _layout_image_area(self):
        """image_panel 由 QVBoxLayout 自动填满，内部控件手动布局。"""
        if not hasattr(self, "image_panel") or not self.image_panel.parentWidget():
            return

        # 强制 layout 立即更新
        if self.layout():
            self.layout().activate()

        avail_w = self.image_panel.width()
        avail_h = self.image_panel.height()

        # graphics_view 填满 image_panel
        self.graphics_view.setGeometry(0, 0, avail_w, avail_h)

        # overlay_bar 悬浮在底部
        bar_h = 52
        margin_w = 8
        margin_h = 6
        self.overlay_bar.setGeometry(
            margin_w, avail_h - bar_h - margin_h, avail_w - margin_w * 2, bar_h
        )

        # 四个角 DICOM 角标定位（向上避开底部悬浮控制条）
        pad = 8
        cw = max(120, avail_w // 4)
        ch = 48
        bar_total_h = 52 + 6  # overlay_bar 高度 + 底部 margin
        bottom_y = avail_h - bar_total_h - pad - ch
        self.lbl_corner_tl.setGeometry(pad, pad, cw, ch)
        self.lbl_corner_tr.setGeometry(avail_w - cw - pad, pad, cw, ch)
        self.lbl_corner_bl.setGeometry(pad, bottom_y, cw, ch)
        self.lbl_corner_br.setGeometry(avail_w - cw - pad, bottom_y, cw, ch)

        # 图像自适应居中
        if self._total_frames > 0:
            self.graphics_view.fitInView(self.pixmap_item, Qt.KeepAspectRatio)
            self.graphics_view.setAlignment(Qt.AlignCenter)

    # ---------- 事件过滤器：捕获 viewport 中键事件 ----------

    def eventFilter(self, obj, event):
        if obj is self.graphics_view.viewport():
            et = event.type()
            # 中键：调节窗宽窗位
            if et == QEvent.MouseButtonPress and event.button() == Qt.MiddleButton:
                self._start_wwwl_drag(event.globalPosition().toPoint())
                return True
            if et == QEvent.MouseMove and self._middle_dragging:
                self._update_wwwl_drag(event.globalPosition().toPoint())
                return True
            if (
                et == QEvent.MouseButtonRelease
                and event.button() == Qt.MiddleButton
                and self._middle_dragging
            ):
                self._end_wwwl_drag()
                return True
            # 右键：按住拖动缩放 / 单击弹出菜单
            if et == QEvent.MouseButtonPress and event.button() == Qt.RightButton:
                self._right_press_pos = event.globalPosition().toPoint()
                self._right_dragging = True
                self._right_drag_started = False
                return True
            if et == QEvent.MouseMove and self._right_dragging:
                pos = event.globalPosition().toPoint()
                if not self._right_drag_started:
                    # 移动超过阈值才判定为拖动，否则视为待点击
                    if (pos - self._right_press_pos).manhattanLength() > 5:
                        self._right_drag_started = True
                        self._start_right_drag(self._right_press_pos)
                if self._right_drag_started:
                    self._update_right_drag(pos)
                return True
            if et == QEvent.MouseButtonRelease and event.button() == Qt.RightButton:
                if self._right_dragging:
                    if self._right_drag_started:
                        self._end_right_drag()
                    else:
                        # 未发生拖动，视为单击，弹出右键菜单
                        self._on_viewer_context_menu(event.pos())
                    self._right_dragging = False
                    self._right_drag_started = False
                return True
        return super().eventFilter(obj, event)

    # ---------- 公共接口 ----------

    def load_series(self, file_list: List[str]):
        """启动后台线程加载 DICOM 序列，不阻塞 UI。"""
        self.clear()
        if not file_list:
            return

        self._loader_thread = QThread(self)
        self._loader = LoadSeriesWorker(file_list)
        self._loader.moveToThread(self._loader_thread)

        self._loader_thread.started.connect(self._loader.run)
        self._loader.progress.connect(self.load_progress.emit)
        self._loader.finished.connect(self._on_load_finished)
        self._loader.error.connect(self._on_load_error)

        # 线程结束后清理
        self._loader.finished.connect(self._loader_thread.quit)
        self._loader.finished.connect(self._loader.deleteLater)
        self._loader.error.connect(self._loader_thread.quit)
        self._loader.error.connect(self._loader.deleteLater)
        self._loader_thread.finished.connect(self._loader_thread.deleteLater)

        self._loader_thread.start()

    def _on_load_finished(self, sources, pmin, pmax, dicom_ww, dicom_wl, modality, dicom_info, first_frame):
        """后台加载完成，在主线程更新 UI。"""
        self._frame_sources = sources
        self._total_frames = len(sources)
        self._current_idx = 0
        self._mask_idx = 0
        self._auto_wwwl = True
        self._dicom_info = dicom_info

        if first_frame is not None:
            self._frame_cache[0] = first_frame

        seq_pmin, seq_pmax = pmin, pmax

        if modality == "CT":
            if dicom_ww is not None and dicom_wl is not None:
                self._ww = max(1, dicom_ww)
                self._wl = dicom_wl
            else:
                self._ww = 400
                self._wl = 40
            self.sld_wl.blockSignals(True)
            self.sld_ww.blockSignals(True)
            self.sld_wl.setValue(int(self._wl))
            self.sld_ww.setValue(int(self._ww))
            self.sld_wl.blockSignals(False)
            self.sld_ww.blockSignals(False)
            self.lbl_ww_val.setText(str(int(self._ww)))
            self.lbl_wl_val.setText(str(int(self._wl)))
            self._update_wwwl_label()
            self.update_display()
        else:
            self._reset_wwwl()

        self._global_pixel_range = max(abs(seq_pmin), abs(seq_pmax), 1.0)
        logger.info(f"减影范围初始化: ±{self._global_pixel_range:.0f}")

        self.frame_slider.setRange(0, max(0, self._total_frames - 1))
        self.frame_slider.setValue(0)
        self.frame_slider.setEnabled(self._total_frames > 1)
        self.lbl_frame.setText(f"1 / {self._total_frames}")
        self._update_corner_labels()
        self.series_loaded.emit(self._total_frames)
        logger.info(f"加载完成: {self._total_frames} 帧")

    def _on_load_error(self, msg: str):
        """后台加载出错。"""
        self.status_message.emit(f"加载失败: {msg}")
        logger.warning(f"加载序列失败: {msg}")

    def clear(self):
        """清空缓存，释放内存。"""
        self.stop()
        self._frame_sources.clear()
        self._frame_cache.clear()
        self._total_frames = 0
        self._current_idx = 0
        self.pixmap_item.setPixmap(QPixmap())
        self.scene.setSceneRect(0, 0, 0, 0)
        # 切换序列时自动关闭实时减影、反相、锐度
        self._subtraction_enabled = False
        self.chk_sub.setChecked(False)
        self._invert_enabled = False
        self.chk_invert.setChecked(False)
        self._sharpness = 0
        self.sld_sharp.setValue(0)
        self.lbl_sharp.setText("原图")
        # 清空 DICOM 角标
        self._dicom_info = {}
        self._update_corner_labels()
        gc.collect()

    # ---------- 显示核心 ----------

    def update_display(self):
        """刷新当前帧显示。"""
        if self._total_frames == 0 or self._current_idx >= self._total_frames:
            return

        frame = self._get_frame(self._current_idx)

        if self._subtraction_enabled:
            mask = self._get_frame(self._mask_idx)
            diff = self._compute_subtraction(frame, mask)
            disp = self._apply_window(diff)
        else:
            disp = self._apply_window(frame)

        pixmap = self._array_to_pixmap(disp)
        self.pixmap_item.setPixmap(pixmap)

        # 更新 scene rect 以匹配当前图像尺寸，确保图像在视图中正确居中
        self.scene.setSceneRect(self.pixmap_item.boundingRect())

        self.pixmap_item.update()
        self.scene.update()
        self.graphics_view.viewport().update()

        if self._auto_wwwl:
            self.graphics_view.fitInView(self.pixmap_item, Qt.KeepAspectRatio)
            self._auto_wwwl = False

        self.lbl_frame.setText(f"{self._current_idx + 1} / {self._total_frames}")
        self._update_corner_labels()

    def _update_corner_labels(self):
        """更新图像四个角的 DICOM 信息角标。"""
        info = self._dicom_info
        if not info:
            self.lbl_corner_tl.setText("")
            self.lbl_corner_tr.setText("")
            self.lbl_corner_bl.setText("")
            self.lbl_corner_br.setText("")
            return

        # 左上角：患者姓名 + ID + 性别 + 年龄
        name = info.get("patient_name", "")
        pid = info.get("patient_id", "")
        sex = info.get("patient_sex", "")
        age = info.get("patient_age", "")
        tl_parts = []
        if name:
            tl_parts.append(name)
        if pid:
            tl_parts.append(f"ID: {pid}")
        if sex or age:
            tl_parts.append(f"{sex}  {age}")
        self.lbl_corner_tl.setText("\n".join(tl_parts))

        # 右上角：检查日期 + 医院
        date = info.get("study_date", "")
        inst = info.get("institution", "")
        tr_text = date if date else ""
        if inst:
            tr_text += f"\n{inst}" if tr_text else inst
        self.lbl_corner_tr.setText(tr_text)

        # 左下角：模态特定信息
        mod = info.get("modality", "")
        if mod in ("XA", "RF", "DF"):
            # DSA：使用标准 RAO/LAO + CRA/CAU 格式显示机架角度
            pa = info.get("positioner_primary_angle", "")
            sa = info.get("positioner_secondary_angle", "")
            parts = []
            if pa != "":
                try:
                    pa_f = float(pa)
                    parts.append(f"{'RAO' if pa_f >= 0 else 'LAO'} {abs(pa_f):.0f}°")
                except ValueError:
                    parts.append(str(pa))
            if sa != "":
                try:
                    sa_f = float(sa)
                    parts.append(f"{'CRA' if sa_f >= 0 else 'CAU'} {abs(sa_f):.0f}°")
                except ValueError:
                    parts.append(str(sa))
            bl_text = " / ".join(parts) if parts else ""
        elif mod == "MR":
            # MR：显示序列名称
            pn = info.get("protocol_name", "")
            sn = info.get("sequence_name", "")
            bl_text = pn if pn else (sn if sn else "")
            if not bl_text:
                bl_text = info.get("series_desc", "")
        else:
            # 其他：序列描述 + 模态
            desc = info.get("series_desc", "")
            bl_text = desc if desc else ""
            if mod:
                bl_text += f" [{mod}]" if bl_text else mod
        self.lbl_corner_bl.setText(bl_text)

        # 右下角：帧号 + 窗宽窗位
        if self._total_frames > 0:
            br_text = f"Frame {self._current_idx + 1}/{self._total_frames}"
        else:
            br_text = ""
        if self._ww > 0 or self._wl != 0:
            br_text += f"\nWW {int(self._ww)} / WL {int(self._wl)}" if br_text else f"WW {int(self._ww)} / WL {int(self._wl)}"
        self.lbl_corner_br.setText(br_text)

    def _apply_window(self, frame: np.ndarray) -> np.ndarray:
        """应用窗宽窗位、反相、锐度/平滑，返回 uint8 (h, w)。"""
        # 必须显式 copy，否则 astype 在 dtype 相同时返回原数组视图，
        # np.clip 会直接改写原始像素缓存，导致后续调节失效。
        arr = np.array(frame, dtype=np.float32, copy=True)
        vmin = self._wl - self._ww / 2
        vmax = self._wl + self._ww / 2
        arr = np.clip(arr, vmin, vmax)
        arr = (arr - vmin) / max(vmax - vmin, 1.0) * 255.0
        arr = arr.astype(np.uint8)

        # 图像反相
        if self._invert_enabled:
            arr = 255 - arr

        # 锐度/平滑调节
        if self._sharpness != 0:
            arr = self._apply_sharpness(arr)

        return arr

    def _compute_subtraction(self, frame: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """
        减影运算：current - mask。
        先计算差值、应用增益，再映射到 [0, 4096] 范围（中灰=2048），
        使 _apply_window 的默认参数能正确显示，同时保留窗宽窗位调节空间。
        若启用自动像素位移，会在小范围内搜索最佳平移量对齐蒙片。
        """
        f = np.array(frame, dtype=np.float32, copy=True)
        m = np.array(mask, dtype=np.float32, copy=True)

        # 自动像素位移：在 ±range 范围内搜索使差异最小的平移量
        if self._auto_pixel_shift and self._pixel_shift_range > 0:
            m = self._find_best_shift(f, m)

        diff = f - m

        if logger.isEnabledFor(logging.DEBUG) and self._current_idx % 10 == 0:
            logger.debug(
                f"减影统计: frame#{self._current_idx}-mask#{self._mask_idx} "
                f"diff_min={diff.min():.2f} diff_max={diff.max():.2f} "
                f"diff_mean={diff.mean():.2f}"
            )

        # 应用增益放大差异
        gain = max(self._sub_gain, 0.1)
        diff = diff * gain

        # 映射到 [0, 4096]，中灰=2048。
        # 使用固定 display_range（基于全局像素范围/增益），避免每帧闪烁。
        display_range = self._global_pixel_range / gain
        diff = np.clip(diff, -display_range, display_range)
        diff = (diff + display_range) / (2.0 * display_range) * 4096.0
        return diff

    def _find_best_shift(self, frame: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """
        在 ±pixel_shift_range 像素范围内穷举搜索最佳平移量，
        使 frame 与平移后的 mask 的绝对差值和最小。
        返回平移后的 mask。
        """
        h, w = frame.shape
        best_mse = float("inf")
        best_shifted = mask
        rng = self._pixel_shift_range

        for dy in range(-rng, rng + 1):
            for dx in range(-rng, rng + 1):
                # 使用切片平移，边界用原图填充
                shifted = np.array(mask, dtype=np.float32, copy=True)
                if dy > 0:
                    shifted[:dy, :] = frame[:dy, :]
                    shifted = shifted[dy:, :]
                    shifted = np.pad(shifted, ((0, dy), (0, 0)), mode="edge")
                elif dy < 0:
                    shifted[dy:, :] = frame[dy:, :]
                    shifted = shifted[:dy, :]
                    shifted = np.pad(shifted, ((-dy, 0), (0, 0)), mode="edge")

                if dx > 0:
                    shifted[:, :dx] = frame[:, :dx]
                    shifted = shifted[:, dx:]
                    shifted = np.pad(shifted, ((0, 0), (0, dx)), mode="edge")
                elif dx < 0:
                    shifted[:, dx:] = frame[:, dx:]
                    shifted = shifted[:, :dx]
                    shifted = np.pad(shifted, ((0, 0), (-dx, 0)), mode="edge")

                mse = np.mean(np.abs(frame - shifted))
                if mse < best_mse:
                    best_mse = mse
                    best_shifted = shifted

        return best_shifted

    @staticmethod
    def _sample_pixels(frames: List[np.ndarray]) -> np.ndarray:
        """从帧列表中采样像素用于分布统计（大序列自动降采样，避免内存爆炸）。"""
        if len(frames) <= 100:
            return np.concatenate([f.ravel() for f in frames])
        indices = np.linspace(0, len(frames) - 1, 100, dtype=int)
        return np.concatenate([frames[i].ravel() for i in indices])

    def _array_to_pixmap(self, arr: np.ndarray) -> QPixmap:
        """将 uint8 (h, w) numpy 数组转为 QPixmap。"""
        h, w = arr.shape
        arr = np.ascontiguousarray(arr)
        # 使用 strides[0] 作为 bytesPerLine，确保内存对齐正确
        qimg = QImage(arr.data, w, h, arr.strides[0], QImage.Format_Grayscale8)
        return QPixmap.fromImage(qimg.copy())

    # ---------- 播放控制 ----------

    def toggle_play(self):
        if self._total_frames == 0:
            return
        if self._is_playing:
            self.stop()
        else:
            self.play()

    def play(self):
        if self._total_frames <= 1:
            return
        self._is_playing = True
        self.btn_play.setText("⏸")
        self.btn_panel_play.setText("⏸")
        self._play_timer.start(int(1000 / self._fps))

    def stop(self):
        self._is_playing = False
        self.btn_play.setText("▶")
        self.btn_panel_play.setText("▶")
        self._play_timer.stop()

    def _on_next_frame(self):
        if self._total_frames == 0:
            return
        self._current_idx = (self._current_idx + 1) % self._total_frames
        self.update_display()
        self.frame_slider.blockSignals(True)
        self.frame_slider.setValue(self._current_idx)
        self.frame_slider.blockSignals(False)

    def _on_prev_frame(self):
        """点击：上一帧"""
        if self._total_frames <= 1:
            return
        if self._current_idx == 0:
            self.status_message.emit("已经是第一帧")
            return
        self._current_idx = (self._current_idx - 1) % self._total_frames
        self.update_display()
        self.frame_slider.blockSignals(True)
        self.frame_slider.setValue(self._current_idx)
        self.frame_slider.blockSignals(False)

    def _on_next_frame_click(self):
        """点击：下一帧（手动按钮）"""
        if self._total_frames <= 1:
            return
        if self._current_idx == self._total_frames - 1:
            self.status_message.emit("已经是最后一帧")
            return
        self._current_idx = (self._current_idx + 1) % self._total_frames
        self.update_display()
        self.frame_slider.blockSignals(True)
        self.frame_slider.setValue(self._current_idx)
        self.frame_slider.blockSignals(False)

    def _on_slider_changed(self, val: int):
        self._current_idx = val
        self.update_display()

    def _on_fps_changed(self, val: int):
        self._fps = val
        self.lbl_fps.setText(str(val))
        if self._is_playing:
            self._play_timer.setInterval(int(1000 / self._fps))

    # ---------- 减影控制 ----------

    def _on_sub_toggled(self, state):
        self._subtraction_enabled = state == Qt.CheckState.Checked
        self.update_display()

    def _on_gain_changed(self, value: int):
        """减影增益滑块变化。"""
        self._sub_gain = float(value)
        self.lbl_gain.setText(f"{value}x")
        if self._subtraction_enabled:
            self.update_display()

    def _on_auto_shift_toggled(self, state):
        """自动像素位移开关。"""
        self._auto_pixel_shift = state == Qt.CheckState.Checked
        if self._subtraction_enabled:
            self.update_display()

    def _on_shift_range_changed(self, value: int):
        """像素位移搜索范围变化。"""
        self._pixel_shift_range = value
        self.lbl_shift.setText(f"±{value}")
        if self._subtraction_enabled and self._auto_pixel_shift:
            self.update_display()

    def _on_invert_toggled(self, state):
        """图像反相开关。"""
        self._invert_enabled = state == Qt.CheckState.Checked
        self.update_display()

    def _on_sharpness_changed(self, value: int):
        """锐度/平滑滑块变化。"""
        self._sharpness = value
        labels = { -5: "最平滑", -4: "很平滑", -3: "平滑", -2: "微平滑", -1: "轻平滑",
                    0: "原图", 1: "轻锐化", 2: "微锐化", 3: "锐化", 4: "很锐化", 5: "最锐化" }
        self.lbl_sharp.setText(labels.get(value, str(value)))
        self.update_display()

    def _apply_sharpness(self, arr: np.ndarray) -> np.ndarray:
        """应用锐度/平滑滤波，输入输出均为 uint8 (h, w)。"""
        import cv2
        if self._sharpness > 0:
            # 锐化：unsharp mask
            strength = self._sharpness * 0.6
            blurred = cv2.GaussianBlur(arr, (0, 0), 1.5)
            sharpened = cv2.addWeighted(arr, 1 + strength, blurred, -strength, 0)
            return np.clip(sharpened, 0, 255).astype(np.uint8)
        else:
            # 平滑：高斯模糊
            ksize = abs(self._sharpness) * 2 + 1
            return cv2.GaussianBlur(arr, (ksize, ksize), 0)

    def _set_mask(self):
        if self._total_frames == 0:
            return
        self._mask_idx = self._current_idx
        self.lbl_mask.setText(f"蒙片: 第 {self._mask_idx + 1} 帧")
        self.mask_frame_changed.emit(self._mask_idx)
        self.update_display()

    def _on_viewer_context_menu(self, pos):
        """图像查看器右键菜单：设为蒙片。"""
        if self._total_frames == 0:
            return
        menu = QMenu(self)
        menu.setStyleSheet(
            """
            QMenu {
                background-color: #1a1a1a;
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
                background-color: #10b981;
            }
        """
        )
        action = menu.addAction(f"将第 {self._current_idx + 1} 帧设为蒙片 (Mask)")
        action.triggered.connect(self._set_mask)
        menu.exec(self.graphics_view.mapToGlobal(pos))

    # ---------- 窗宽窗位 ----------

    def _on_wwwl_changed(self):
        self._ww = float(self.sld_ww.value())
        self._wl = float(self.sld_wl.value())
        self._update_wwwl_label()
        self.lbl_ww_val.setText(str(int(self._ww)))
        self.lbl_wl_val.setText(str(int(self._wl)))
        self.update_display()

    def _update_wwwl_label(self):
        self.lbl_wwwl.setText(f"WW: {int(self._ww)}  WL: {int(self._wl)}")

    def _reset_wwwl(self):
        if self._total_frames == 0:
            return
        # 重置窗宽窗位
        frame = self._get_frame(self._current_idx)
        pmin, pmax = float(frame.min()), float(frame.max())
        self._wl = (pmin + pmax) / 2
        self._ww = max(1, pmax - pmin)
        # block 信号避免触发 _on_wwwl_changed 导致中间状态错误
        self.sld_ww.blockSignals(True)
        self.sld_wl.blockSignals(True)
        self.sld_wl.setValue(int(self._wl))
        self.sld_ww.setValue(int(self._ww))
        self.sld_ww.blockSignals(False)
        self.sld_wl.blockSignals(False)
        self.lbl_ww_val.setText(str(int(self._ww)))
        self.lbl_wl_val.setText(str(int(self._wl)))
        self._update_wwwl_label()
        # 重置反相
        self._invert_enabled = False
        self.chk_invert.setChecked(False)
        # 重置锐度
        self._sharpness = 0
        self.sld_sharp.setValue(0)
        self.lbl_sharp.setText("原图")
        self.update_display()

    # ---------- 中键调节 WW/WL ----------

    def _start_wwwl_drag(self, global_pos: QPoint):
        if self._total_frames == 0:
            return
        self._middle_dragging = True
        self._middle_drag_start = global_pos
        self._ww_drag_start = self._ww
        self._wl_drag_start = self._wl
        self.graphics_view.setCursor(QCursor(Qt.SizeAllCursor))

    def _update_wwwl_drag(self, global_pos: QPoint):
        if not self._middle_dragging or self._total_frames == 0:
            return
        delta = global_pos - self._middle_drag_start
        # 水平移动调窗宽（WW），垂直移动调窗位（WL）
        # 灵敏度：水平每像素 2 HU，垂直每像素 1 HU
        self._ww = max(1, self._ww_drag_start + delta.x() * 2)
        self._wl = self._wl_drag_start - delta.y()

        self.sld_ww.blockSignals(True)
        self.sld_wl.blockSignals(True)
        self.sld_ww.setValue(int(self._ww))
        self.sld_wl.setValue(int(self._wl))
        self.sld_ww.blockSignals(False)
        self.sld_wl.blockSignals(False)

        self._update_wwwl_label()
        self.update_display()

    def _end_wwwl_drag(self):
        self._middle_dragging = False
        self.graphics_view.unsetCursor()

    # ---------- 右键拖动缩放 ----------

    def _get_current_scale(self) -> float:
        """获取当前视图变换的 X 轴缩放比例。"""
        return self.graphics_view.transform().m11()

    def _apply_scale_limit(self, target_scale: float) -> float:
        """将目标缩放限制在 50% ~ 1000% 之间。"""
        return max(0.5, min(10.0, target_scale))

    def _start_right_drag(self, global_pos: QPoint):
        if self._total_frames == 0:
            return
        self._right_dragging = True
        self._right_drag_start = global_pos
        self._right_scale_start = self._get_current_scale()
        self.graphics_view.setCursor(QCursor(Qt.SizeVerCursor))

    def _update_right_drag(self, global_pos: QPoint):
        if not self._right_dragging or self._total_frames == 0:
            return
        delta = global_pos - self._right_drag_start
        # 向上拖动放大，向下拖动缩小；每 100 像素约 2 倍变化
        delta_y = -delta.y()
        ratio = 1.0 + (delta_y / 200.0)
        ratio = max(0.1, min(10.0, ratio))

        new_scale = self._apply_scale_limit(self._right_scale_start * ratio)
        current_scale = self._get_current_scale()
        if current_scale > 0:
            self.graphics_view.scale(new_scale / current_scale, new_scale / current_scale)

    def _end_right_drag(self):
        self._right_dragging = False
        self.graphics_view.unsetCursor()

    # ---------- 滚轮切帧 ----------

    def wheelEvent(self, event):
        """鼠标滚轮切换上下帧。"""
        if self._total_frames <= 1:
            return
        delta = event.angleDelta().y()
        if delta > 0:
            self._on_prev_frame()
        elif delta < 0:
            self._on_next_frame_click()

    # ---------- 属性 ----------

    @property
    def total_frames(self) -> int:
        return self._total_frames

    @property
    def frame_size(self) -> tuple:
        """返回帧尺寸 (width, height)。无帧时返回 (0, 0)。"""
        if self._frame_sources:
            rows = self._frame_sources[0].get("shape", (0, 0))[0]
            cols = self._frame_sources[0].get("shape", (0, 0))[1]
            return (cols, rows)
        return (0, 0)

    def get_export_frames(self):
        """生成器：逐帧输出 uint8 灰度图，应用当前窗宽窗位和减影设置。"""
        for i in range(self._total_frames):
            raw = self._get_frame(i)
            if self._subtraction_enabled:
                raw = self._compute_subtraction(raw, self._get_frame(self._mask_idx))
            yield self._apply_window(raw)

    def export_current_frame(self, path: str) -> bool:
        """导出当前显示的帧为 PNG 文件，应用当前窗宽窗位和减影设置。

        参数：
            path: 输出文件路径（建议以 .png 结尾）

        返回：
            是否成功导出
        """
        if self._total_frames == 0 or self._current_idx >= self._total_frames:
            return False
        raw = self._get_frame(self._current_idx)
        if self._subtraction_enabled:
            raw = self._compute_subtraction(raw, self._get_frame(self._mask_idx))
        frame = self._apply_window(raw)
        try:
            import cv2
            cv2.imwrite(path, frame)
            return True
        except Exception:
            return False
