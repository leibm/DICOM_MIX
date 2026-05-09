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

import numpy as np
from pydicom import dcmread

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSlider,
    QLabel, QCheckBox, QGroupBox, QSpinBox, QGraphicsView,
    QGraphicsScene, QGraphicsPixmapItem, QMenu
)
from PySide6.QtCore import Qt, QTimer, Signal, QPoint, QEvent
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
    background-color: #3b82f6;
    color: white;
    border: none;
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 600;
    font-size: 13px;
}
QPushButton:hover {
    background-color: #2563eb;
}
QPushButton:pressed {
    background-color: #1d4ed8;
}
QPushButton#playBtn {
    background-color: #10b981;
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
    background-color: #059669;
    border: none;
}
QPushButton#playBtn:pressed {
    background-color: #047857;
    border: none;
}
QPushButton#playBtn:focus {
    border: none;
}
QToolTip {
    background-color: #1f2937;
    color: #f9fafb;
    border: 1px solid #374151;
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
}
QPushButton#maskBtn {
    background-color: #8b5cf6;
}
QPushButton#maskBtn:hover {
    background-color: #7c3aed;
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
    background: #e5e7eb;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    width: 16px;
    height: 16px;
    background: #3b82f6;
    border-radius: 8px;
    margin: -5px 0;
}
QSlider::sub-page:horizontal {
    background: #3b82f6;
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
    background-color: #3b82f6;
    border-color: #3b82f6;
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


class DSAViewerWidget(QWidget):
    """
    DSA 多帧图像查看器。

    布局：左侧图像区（QGraphicsView，底部悬浮控制条），右侧控制面板。
    支持：多帧加载、循环播放、实时减影、窗宽窗位、滚轮缩放、中键调节 WW/WL。
    """

    mask_frame_changed = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._init_state()
        self._init_ui()

    # ---------- 内部状态 ----------

    def _init_state(self):
        self._raw_frames: List[np.ndarray] = []
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

        # 中键调节窗宽窗位状态
        self._middle_dragging: bool = False
        self._middle_drag_start: QPoint = QPoint()
        self._ww_drag_start: float = 4096.0
        self._wl_drag_start: float = 2048.0

        self._play_timer = QTimer(self)
        self._play_timer.timeout.connect(self._on_next_frame)

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
        # 不设置 layout，完全手动管理子控件位置

        # 图像面板：圆角深色背景，内部容纳 graphics_view 和悬浮控制条
        self.image_panel = QWidget(panel)
        self.image_panel.setStyleSheet("""
            background-color: #0f0f0f;
            border-radius: 12px;
        """)

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
        self.graphics_view.setStyleSheet("border: none; background-color: transparent;")
        self.graphics_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.graphics_view.customContextMenuRequested.connect(
            self._on_viewer_context_menu
        )

        # 在 viewport 上安装事件过滤器，捕获中键事件（用于调节 WW/WL）
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

        # 帧滑块
        self.frame_slider = QSlider(Qt.Horizontal)
        self.frame_slider.setRange(0, 0)
        self.frame_slider.setEnabled(False)
        self.frame_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                height: 6px;
                background: #4b5563;
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

        return panel

    def _create_control_panel(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet("background-color: transparent;")
        vbox = QVBoxLayout(panel)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(10)
        vbox.setAlignment(Qt.AlignTop)

        # --- 减影控制 ---
        g1 = QGroupBox("减影控制")
        b1 = QVBoxLayout(g1)
        b1.setSpacing(8)

        self.chk_sub = QCheckBox("开启实时减影")
        self.chk_sub.setStyleSheet("font-size: 13px; font-weight: 500;")
        self.chk_sub.checkStateChanged.connect(self._on_sub_toggled)
        b1.addWidget(self.chk_sub)

        self.btn_mask = QPushButton("设为蒙片 (Mask)")
        self.btn_mask.setObjectName("maskBtn")
        self.btn_mask.setToolTip("将当前帧设为减影基准 (也可右键图像)")
        self.btn_mask.setCursor(QCursor(Qt.PointingHandCursor))
        self.btn_mask.clicked.connect(self._set_mask)
        b1.addWidget(self.btn_mask)

        self.lbl_mask = QLabel("蒙片: 第 0 帧")
        self.lbl_mask.setStyleSheet(
            "color: #6b7280; font-size: 12px; padding-left: 2px;"
        )
        b1.addWidget(self.lbl_mask)

        gain_row = QHBoxLayout()
        gain_row.addWidget(QLabel("增益:"))
        self.sld_gain = QSlider(Qt.Horizontal)
        self.sld_gain.setRange(1, 20)
        self.sld_gain.setValue(4)
        self.sld_gain.valueChanged.connect(self._on_gain_changed)
        gain_row.addWidget(self.sld_gain, stretch=1)
        b1.addLayout(gain_row)

        self.lbl_gain = QLabel("4x  (范围 ±63)")
        self.lbl_gain.setStyleSheet(
            "color: #6b7280; font-size: 11px; padding-left: 2px;"
        )
        b1.addWidget(self.lbl_gain)

        vbox.addWidget(g1)

        # --- 窗宽窗位 ---
        g2 = QGroupBox("窗宽窗位")
        b2 = QVBoxLayout(g2)
        b2.setSpacing(6)

        b2.addWidget(QLabel("窗宽 (WW):"))
        self.sld_ww = QSlider(Qt.Horizontal)
        self.sld_ww.setRange(1, 8192)
        self.sld_ww.setValue(4096)
        self.sld_ww.valueChanged.connect(self._on_wwwl_changed)
        b2.addWidget(self.sld_ww)

        b2.addWidget(QLabel("窗位 (WL):"))
        self.sld_wl = QSlider(Qt.Horizontal)
        self.sld_wl.setRange(-2048, 8192)
        self.sld_wl.setValue(2048)
        self.sld_wl.valueChanged.connect(self._on_wwwl_changed)
        b2.addWidget(self.sld_wl)

        self.lbl_wwwl = QLabel("WW: 4096  WL: 2048")
        self.lbl_wwwl.setStyleSheet(
            "color: #6b7280; font-size: 12px; font-weight: 500; padding-left: 2px;"
        )
        b2.addWidget(self.lbl_wwwl)

        hint = QLabel("💡 鼠标中键拖动调节 WW/WL")
        hint.setStyleSheet("color: #8b5cf6; font-size: 11px; padding: 4px 2px;")
        hint.setWordWrap(True)
        b2.addWidget(hint)

        self.btn_reset_wwwl = QPushButton("重置窗宽窗位")
        self.btn_reset_wwwl.setObjectName("resetBtn")
        self.btn_reset_wwwl.setCursor(QCursor(Qt.PointingHandCursor))
        self.btn_reset_wwwl.clicked.connect(self._reset_wwwl)
        b2.addWidget(self.btn_reset_wwwl)

        vbox.addWidget(g2)

        # --- 播放设置 ---
        g3 = QGroupBox("播放")
        b3 = QVBoxLayout(g3)
        b3.setSpacing(6)

        fps_row = QHBoxLayout()
        fps_row.addWidget(QLabel("帧率:"))
        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(1, 60)
        self.spin_fps.setValue(15)
        self.spin_fps.setSuffix(" FPS")
        self.spin_fps.valueChanged.connect(self._on_fps_changed)
        fps_row.addWidget(self.spin_fps)
        fps_row.addStretch()
        b3.addLayout(fps_row)

        zoom_hint = QLabel("💡 滚轮缩放图像")
        zoom_hint.setStyleSheet("color: #6b7280; font-size: 11px; padding: 2px;")
        b3.addWidget(zoom_hint)

        vbox.addWidget(g3)
        vbox.addStretch()
        return panel

    # ---------- 尺寸与比例（手动布局） ----------

    def resizeEvent(self, event):
        """窗口尺寸变化时重新计算 4:3 图像区与悬浮控制条位置。"""
        super().resizeEvent(event)
        self._layout_image_area()

    def _layout_image_area(self):
        """image_panel 填满可用空间，内部控件填满/悬浮。"""
        if not hasattr(self, "image_panel") or not self.image_panel.parentWidget():
            return

        panel = self.image_panel.parentWidget()
        avail_w = panel.width()
        avail_h = panel.height()

        # 填满可用空间，不再强制 4:3
        pad = 0
        self.image_panel.setGeometry(pad, pad, avail_w - pad * 2, avail_h - pad * 2)

        w = self.image_panel.width()
        h = self.image_panel.height()

        # graphics_view 填满 image_panel
        self.graphics_view.setGeometry(0, 0, w, h)

        # overlay_bar 悬浮在底部：更薄、更沉浸
        bar_h = 52
        margin_w = 8
        margin_h = 6
        self.overlay_bar.setGeometry(
            margin_w, h - bar_h - margin_h, w - margin_w * 2, bar_h
        )

    # ---------- 事件过滤器：捕获 viewport 中键事件 ----------

    def eventFilter(self, obj, event):
        if obj is self.graphics_view.viewport():
            et = event.type()
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
        return super().eventFilter(obj, event)

    # ---------- 公共接口 ----------

    def load_series(self, file_list: List[str]):
        """加载 DICOM 序列文件。"""
        self.clear()
        if not file_list:
            return

        frames: List[np.ndarray] = []
        pmin, pmax = float("inf"), float("-inf")

        for fpath in file_list:
            try:
                ds = dcmread(fpath, stop_before_pixels=False)
                arr = ds.pixel_array

                slope = float(getattr(ds, "RescaleSlope", 1))
                intercept = float(getattr(ds, "RescaleIntercept", 0))
                if slope != 1 or intercept != 0:
                    arr = arr.astype(np.float32) * slope + intercept

                if arr.ndim == 3:
                    for i in range(arr.shape[0]):
                        frames.append(arr[i].copy())
                        pmin = min(pmin, float(arr[i].min()))
                        pmax = max(pmax, float(arr[i].max()))
                elif arr.ndim == 2:
                    frames.append(arr.copy())
                    pmin = min(pmin, float(arr.min()))
                    pmax = max(pmax, float(arr.max()))
            except Exception as e:
                logger.warning(f"读取失败 {fpath}: {e}")

        if not frames:
            return

        self._raw_frames = frames
        self._total_frames = len(frames)
        self._current_idx = 0
        self._mask_idx = 0
        self._auto_wwwl = True

        self._wl = (pmin + pmax) / 2
        self._ww = max(1, pmax - pmin)
        self.sld_wl.setValue(int(self._wl))
        self.sld_ww.setValue(int(self._ww))
        self._update_wwwl_label()

        self._global_pixel_range = max(abs(pmin), abs(pmax), 1.0)
        logger.info(f"减影范围初始化: ±{self._global_pixel_range:.0f}")

        self.frame_slider.setRange(0, max(0, self._total_frames - 1))
        self.frame_slider.setValue(0)
        self.frame_slider.setEnabled(self._total_frames > 1)
        self.lbl_frame.setText(f"1 / {self._total_frames}")

        self.update_display()
        logger.info(
            f"加载完成: {self._total_frames} 帧, 像素范围 [{pmin:.0f}, {pmax:.0f}]"
        )

    def clear(self):
        """清空缓存，释放内存。"""
        self.stop()
        self._raw_frames.clear()
        self._raw_frames = []
        self._total_frames = 0
        self._current_idx = 0
        self.pixmap_item.setPixmap(QPixmap())
        self.scene.setSceneRect(0, 0, 0, 0)
        gc.collect()

    # ---------- 显示核心 ----------

    def update_display(self):
        """刷新当前帧显示。"""
        if not self._raw_frames or self._current_idx >= len(self._raw_frames):
            return

        frame = self._raw_frames[self._current_idx]

        if self._subtraction_enabled:
            mask = self._raw_frames[self._mask_idx]
            diff = self._compute_subtraction(frame, mask)
            disp = self._apply_window(diff)
        else:
            disp = self._apply_window(frame)

        pixmap = self._array_to_pixmap(disp)
        self.pixmap_item.setPixmap(pixmap)
        self.pixmap_item.update()
        self.scene.update()
        self.graphics_view.viewport().update()

        if self._auto_wwwl:
            self.graphics_view.fitInView(self.pixmap_item, Qt.KeepAspectRatio)
            self._auto_wwwl = False

        self.lbl_frame.setText(f"{self._current_idx + 1} / {self._total_frames}")

    def _apply_window(self, frame: np.ndarray) -> np.ndarray:
        """应用窗宽窗位，返回 uint8 (h, w)。"""
        # 必须显式 copy，否则 astype 在 dtype 相同时返回原数组视图，
        # np.clip 会直接改写原始像素缓存，导致后续调节失效。
        arr = np.array(frame, dtype=np.float32, copy=True)
        vmin = self._wl - self._ww / 2
        vmax = self._wl + self._ww / 2
        arr = np.clip(arr, vmin, vmax)
        arr = (arr - vmin) / max(vmax - vmin, 1.0) * 255.0
        return arr.astype(np.uint8)

    def _compute_subtraction(self, frame: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """
        减影运算：current - mask。
        先计算差值、应用增益，再映射到 [0, 4096] 范围（中灰=2048），
        使 _apply_window 的默认参数能正确显示，同时保留窗宽窗位调节空间。
        """
        f = np.array(frame, dtype=np.float32, copy=True)
        m = np.array(mask, dtype=np.float32, copy=True)
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

    def _array_to_pixmap(self, arr: np.ndarray) -> QPixmap:
        """将 uint8 (h, w) numpy 数组转为 QPixmap。"""
        h, w = arr.shape
        arr = np.ascontiguousarray(arr)
        # 使用 strides[0] 作为 bytesPerLine，确保内存对齐正确
        qimg = QImage(arr.data, w, h, arr.strides[0], QImage.Format_Grayscale8)
        return QPixmap.fromImage(qimg.copy())

    # ---------- 播放控制 ----------

    def toggle_play(self):
        if not self._raw_frames:
            return
        if self._is_playing:
            self.stop()
        else:
            self.play()

    def play(self):
        if not self._raw_frames or self._total_frames <= 1:
            return
        self._is_playing = True
        self.btn_play.setText("⏸")
        self._play_timer.start(int(1000 / self._fps))

    def stop(self):
        self._is_playing = False
        self.btn_play.setText("▶")
        self._play_timer.stop()

    def _on_next_frame(self):
        if not self._raw_frames:
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
        if self._is_playing:
            self._play_timer.setInterval(int(1000 / self._fps))

    # ---------- 减影控制 ----------

    def _on_sub_toggled(self, state):
        self._subtraction_enabled = state == Qt.CheckState.Checked
        self.update_display()

    def _on_gain_changed(self, value: int):
        """减影增益滑块变化。"""
        self._sub_gain = float(value)
        display_range = self._global_pixel_range / max(self._sub_gain, 0.1)
        self.lbl_gain.setText(f"{value}x  (范围 ±{display_range:.0f})")
        if self._subtraction_enabled:
            self.update_display()

    def _set_mask(self):
        if not self._raw_frames:
            return
        self._mask_idx = self._current_idx
        self.lbl_mask.setText(f"蒙片: 第 {self._mask_idx + 1} 帧")
        self.mask_frame_changed.emit(self._mask_idx)
        self.update_display()

    def _on_viewer_context_menu(self, pos):
        """图像查看器右键菜单：设为蒙片。"""
        if not self._raw_frames:
            return
        menu = QMenu(self)
        menu.setStyleSheet(
            """
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
                background-color: #3b82f6;
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
        self.update_display()

    def _update_wwwl_label(self):
        self.lbl_wwwl.setText(f"WW: {int(self._ww)}  WL: {int(self._wl)}")

    def _reset_wwwl(self):
        if not self._raw_frames:
            return
        frame = self._raw_frames[self._current_idx]
        pmin, pmax = float(frame.min()), float(frame.max())
        self._wl = (pmin + pmax) / 2
        self._ww = max(1, pmax - pmin)
        self.sld_wl.setValue(int(self._wl))
        self.sld_ww.setValue(int(self._ww))
        self._update_wwwl_label()
        self.update_display()

    # ---------- 中键调节 WW/WL ----------

    def _start_wwwl_drag(self, global_pos: QPoint):
        if not self._raw_frames:
            return
        self._middle_dragging = True
        self._middle_drag_start = global_pos
        self._ww_drag_start = self._ww
        self._wl_drag_start = self._wl
        self.graphics_view.setCursor(QCursor(Qt.SizeAllCursor))

    def _update_wwwl_drag(self, global_pos: QPoint):
        if not self._middle_dragging or not self._raw_frames:
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

    # ---------- 滚轮缩放 ----------

    def wheelEvent(self, event):
        """鼠标滚轮缩放图像。"""
        factor = 1.15 if event.angleDelta().y() > 0 else 0.87
        self.graphics_view.scale(factor, factor)

    # ---------- 属性 ----------

    @property
    def total_frames(self) -> int:
        return self._total_frames
