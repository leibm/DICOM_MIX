# -*- coding: utf-8 -*-
"""
dicom_input.py
数据输入模块

负责 DICOM 数据的输入，提供两种来源：
1. SCP 接收端：基于 pynetdicom 实现 DICOM Storage SCP，监听指定端口，
   将接收到的 DICOM 文件按 StudyInstanceUID 存入本地临时目录。
2. 本地载入：遍历用户指定的本地文件夹，读取合法的 DICOM 文件，
   统一复制到本地临时目录中，保持与 SCP 接收的数据结构一致。

数据结构规范（临时目录组织方式）：
  temp_dicom/
    └── {StudyInstanceUID}/
          └── {SeriesInstanceUID}/
                └── {SOPInstanceUID}.dcm

技术栈：pynetdicom, pydicom, PySide6 信号（用于与 UI 线程通信）
"""

import os
import shutil
import logging
from pathlib import Path
from typing import List, Dict, Optional, Callable
from datetime import datetime

from PySide6.QtCore import QObject, QThread, Signal

# pynetdicom：DICOM 网络通信库
from pynetdicom import AE, evt, build_context, debug_logger
from pynetdicom.sop_class import Verification
from pynetdicom.status import Status

# pydicom：DICOM 文件解析与修改库
from pydicom import dcmread
from pydicom.dataset import Dataset
from pydicom.uid import ImplicitVRLittleEndian, ExplicitVRLittleEndian

# ------------------------------------------------------------------------------
# 日志配置
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dicom_input")

# ------------------------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------------------------

def ensure_dir(path: str) -> str:
    """确保目录存在，返回绝对路径。"""
    abs_path = os.path.abspath(path)
    os.makedirs(abs_path, exist_ok=True)
    return abs_path


def get_temp_base_dir() -> str:
    """获取默认的临时目录根路径（使用用户 LOCALAPPDATA）。"""
    return ensure_dir(os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "DICOM_MIX_Tools",
        "temp_dicom"
    ))


def is_valid_dicom(file_path: str) -> bool:
    """
    快速检查文件是否为合法的 DICOM 文件。
    通过尝试读取文件头（preamble + prefix）来判断，避免读取整个大文件。
    """
    try:
        # pydicom 的 dcmread 配合 stop_before_pixels=True 只读元数据，效率较高
        ds = dcmread(file_path, stop_before_pixels=True, force=True)
        # 至少包含 SOPClassUID 和 SOPInstanceUID 才认为是有效 DICOM
        return hasattr(ds, "SOPClassUID") and hasattr(ds, "SOPInstanceUID")
    except Exception as e:
        logger.debug(f"文件 {file_path} 不是合法 DICOM: {e}")
        return False


def build_storage_path(base_dir: str, ds) -> str:
    """
    根据 DICOM 数据集中的 UID 构建存储路径。
    结构：base_dir / StudyInstanceUID / SeriesInstanceUID / SOPInstanceUID.dcm
    如果 StudyInstanceUID 或 SeriesInstanceUID 缺失，使用默认值。
    """
    study_uid = getattr(ds, "StudyInstanceUID", "UNKNOWN_STUDY")
    series_uid = getattr(ds, "SeriesInstanceUID", "UNKNOWN_SERIES")
    sop_uid = getattr(ds, "SOPInstanceUID", "UNKNOWN_SOP")

    # 清理 UID 中不适合作为文件路径的字符（通常 UID 只含数字和点，但以防万一）
    def clean_uid(uid: str) -> str:
        return uid.strip().replace("/", "_").replace("\\", "_")

    study_uid = clean_uid(study_uid)
    series_uid = clean_uid(series_uid)
    sop_uid = clean_uid(sop_uid)

    storage_dir = os.path.join(base_dir, study_uid, series_uid)
    ensure_dir(storage_dir)
    return os.path.join(storage_dir, f"{sop_uid}.dcm")


def scan_temp_directory(base_dir: str, date_filter: Optional[str] = None) -> List[Dict]:
    """
    扫描临时目录，按 Study -> Series -> Instance 的层级返回结构化数据。
    支持按 StudyDate 进行日期过滤。

    参数：
        base_dir: 临时目录路径
        date_filter: 日期过滤条件
            - "today": 今天
            - "last3days": 最近3天
            - "last7days": 最近7天
            - "last30days": 最近30天
            - "all" 或 None: 全部

    返回：
        List[Dict] - 每个元素代表一个 Study
    """
    from datetime import datetime, timedelta

    studies = []
    if not os.path.isdir(base_dir):
        return studies

    # 计算日期过滤的截止范围
    cutoff_date = None
    if date_filter and date_filter != "all":
        today = datetime.now().date()
        if date_filter == "today":
            cutoff_date = today
        elif date_filter == "last3days":
            cutoff_date = today - timedelta(days=3)
        elif date_filter == "last7days":
            cutoff_date = today - timedelta(days=7)
        elif date_filter == "last30days":
            cutoff_date = today - timedelta(days=30)

    for study_uid in sorted(os.listdir(base_dir)):
        study_path = os.path.join(base_dir, study_uid)
        if not os.path.isdir(study_path):
            continue

        # 尝试从 Study 下的任意文件中读取患者信息（取第一个合法文件）
        patient_name = "Unknown"
        patient_id = "N/A"
        study_date = ""
        series_list = []

        for series_uid in sorted(os.listdir(study_path)):
            series_path = os.path.join(study_path, series_uid)
            if not os.path.isdir(series_path):
                continue

            instances = []
            series_description = ""
            modality = "OT"

            for fname in sorted(os.listdir(series_path)):
                fpath = os.path.join(series_path, fname)
                if not os.path.isfile(fpath):
                    continue
                instances.append(fpath)
                # 尝试从第一个文件中读取序列描述、模态和日期
                if not series_description and is_valid_dicom(fpath):
                    try:
                        ds = dcmread(fpath, stop_before_pixels=True)
                        series_description = getattr(ds, "SeriesDescription", "")
                        modality = getattr(ds, "Modality", "OT")
                        if not patient_name or patient_name == "Unknown":
                            patient_name = getattr(ds, "PatientName", "Unknown")
                        if not patient_id or patient_id == "N/A":
                            patient_id = getattr(ds, "PatientID", "N/A")
                        if not study_date:
                            study_date = getattr(ds, "StudyDate", "")
                    except Exception:
                        pass

            if instances:
                series_list.append({
                    "series_uid": series_uid,
                    "series_description": series_description or series_uid,
                    "modality": modality,
                    "instances": instances,
                })

        # 日期过滤
        if cutoff_date and study_date:
            try:
                file_date = datetime.strptime(study_date, "%Y%m%d").date()
                if file_date < cutoff_date:
                    continue
            except ValueError:
                pass

        if series_list:
            studies.append({
                "study_uid": study_uid,
                "patient_name": str(patient_name),
                "patient_id": str(patient_id),
                "study_date": study_date,
                "series_list": series_list,
            })

    return studies


# ------------------------------------------------------------------------------
# 信号定义（用于跨线程通信）
# ------------------------------------------------------------------------------

class DicomInputSignals(QObject):
    """
    数据输入模块向主 UI 发出的信号。
    由 main.py 将此处信号实例连接到 MainWindow 的同名信号。
    """
    # 接收到新的 Study（参数：StudyUID, PatientName）
    study_received = Signal(str, str)
    # SCP 状态变化（参数：是否运行中, 状态文本）
    scp_status_changed = Signal(bool, str)
    # 本地载入进度（参数：当前数, 总数）
    local_load_progress = Signal(int, int)
    # 本地载入完成（参数：成功加载的文件数）
    local_load_finished = Signal(int)
    # SCP 接收文件计数（参数：已接收文件总数）
    scp_file_received = Signal(int)
    # 通用错误信息
    error_occurred = Signal(str)


# ------------------------------------------------------------------------------
# DICOM SCP 接收端
# ------------------------------------------------------------------------------

class DicomSCP(QObject):
    """
    DICOM Storage SCP（服务端）。

    功能：
    - 启动 AE 监听指定端口，接收远程设备（如 GE DSA）推送的 DICOM C-STORE 请求。
    - 将接收到的文件按 StudyInstanceUID/SeriesInstanceUID 保存到临时目录。
    - 每接收到一个完整的 Study（或每接收一个实例），发射 study_received 信号通知 UI。

    属性：
        ae_title (str): 本机 AE Title，默认为 "MIX_SCP"
        port (int): 监听端口，默认为 11112
        temp_dir (str): 文件保存的根目录
        signals (DicomInputSignals): 信号实例
    """

    def __init__(self, ae_title: str = "MIX_SCP", port: int = 11112,
                 temp_dir: Optional[str] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.ae_title = ae_title
        self.port = port
        self.temp_dir = temp_dir or get_temp_base_dir()
        self.signals = DicomInputSignals()

        self._ae: Optional[AE] = None
        self._server = None  # pynetdicom 的 server 对象
        self._running = False
        self._received_count = 0  # 已接收文件计数

    # ---------- 公共接口 ----------

    def start(self):
        """启动 SCP 服务端。"""
        if self._running:
            logger.warning("SCP 已经在运行中")
            return

        try:
            self._ae = AE(ae_title=self.ae_title)
            # 添加 Storage Presentation Contexts：支持所有标准存储 SOP Class
            # pynetdicom 提供 AllStoragePresentationContexts 可一次性添加
            from pynetdicom.presentation import AllStoragePresentationContexts
            # 限制同时接受的上下文数量，避免过多
            self._ae.supported_contexts = AllStoragePresentationContexts[:100]
            # 同时添加 Verification（C-ECHO）支持，便于对端测试连通性
            self._ae.add_supported_context(Verification)

            # 绑定事件处理器：接收到 C-STORE 请求时调用 _on_c_store
            handlers = [(evt.EVT_C_STORE, self._on_c_store)]

            # 启动监听（非阻塞模式，通过 block=False 获取 server 对象）
            self._server = self._ae.start_server(("0.0.0.0", self.port), block=False, evt_handlers=handlers)
            self._running = True
            self._received_count = 0

            msg = f"SCP 已启动 - AE Title: [{self.ae_title}], 端口: {self.port}, 临时目录: {self.temp_dir}"
            logger.info(msg)
            self.signals.scp_status_changed.emit(True, msg)
        except Exception as e:
            logger.exception("启动 SCP 失败")
            self.signals.error_occurred.emit(f"启动 SCP 失败: {e}")
            self._running = False

    def stop(self):
        """停止 SCP 服务端。"""
        if not self._running:
            return
        try:
            if self._server:
                self._server.shutdown()
                self._server = None
            self._running = False
            msg = "SCP 已停止"
            logger.info(msg)
            self.signals.scp_status_changed.emit(False, msg)
        except Exception as e:
            logger.exception("停止 SCP 时发生异常")
            self.signals.error_occurred.emit(f"停止 SCP 异常: {e}")

    def is_running(self) -> bool:
        """返回 SCP 是否正在运行。"""
        return self._running

    # ---------- 内部事件处理 ----------

    def _on_c_store(self, event):
        """
        pynetdicom EVT_C_STORE 事件处理器。

        参数 event 包含：
            event.dataset: pydicom Dataset（接收到的 DICOM 数据）
            event.context: Presentation Context
        返回 Status：0x0000 表示成功
        """
        try:
            ds = event.dataset

            # 生成保存路径
            file_path = build_storage_path(self.temp_dir, ds)

            # pynetdicom 接收到的 dataset 可能缺少 file_meta，需要手动构造
            from pydicom.uid import ImplicitVRLittleEndian
            if not hasattr(ds, "file_meta") or ds.file_meta is None:
                ds.file_meta = Dataset()
                ds.file_meta.MediaStorageSOPClassUID = getattr(
                    ds, "SOPClassUID", "1.2.840.10008.5.1.4.1.1.2"
                )
                ds.file_meta.MediaStorageSOPInstanceUID = getattr(
                    ds, "SOPInstanceUID", "1.2.3"
                )
                ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
                ds.file_meta.ImplementationClassUID = "1.2.826.0.1.3680043.9.7756.1"

            ds.save_as(file_path, write_like_original=False)

            study_uid = getattr(ds, "StudyInstanceUID", "UNKNOWN")
            patient_name = getattr(ds, "PatientName", "Unknown")
            logger.info(f"已保存 C-STORE 文件: {file_path} | Study: {study_uid}")

            # 通知 UI 有新数据到达
            self.signals.study_received.emit(study_uid, str(patient_name))

            # 更新接收计数
            self._received_count += 1
            self.signals.scp_file_received.emit(self._received_count)

            # 返回成功状态
            return 0x0000
        except Exception as e:
            logger.exception("处理 C-STORE 请求时出错")
            self.signals.error_occurred.emit(f"C-STORE 处理失败: {e}")
            return 0xC000  # 返回失败状态码


# ------------------------------------------------------------------------------
# 本地文件夹载入工作线程
# ------------------------------------------------------------------------------

class LocalLoadWorker(QObject):
    """
    本地 DICOM 文件夹载入工作线程。

    在独立线程中执行，避免阻塞 UI。
    流程：
    1. 递归遍历用户指定的源文件夹。
    2. 对每个文件进行 DICOM 合法性校验。
    3. 将合法文件复制到临时目录，按 StudyInstanceUID/SeriesInstanceUID 组织。
    4. 发射进度信号和完成信号。

    信号：
        progress(int, int): 当前已处理数, 发现的总文件数
        finished(int): 成功复制的文件数
        error(str): 错误信息
    """

    progress = Signal(int, int)
    finished = Signal(int)
    error = Signal(str)

    def __init__(self, source_dir: str, temp_dir: Optional[str] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.source_dir = source_dir
        self.temp_dir = temp_dir or get_temp_base_dir()

    def run(self):
        """执行载入任务。"""
        try:
            if not os.path.isdir(self.source_dir):
                self.error.emit(f"无效的源文件夹: {self.source_dir}")
                return

            # 第一步：收集所有文件路径
            all_files = []
            for root, _, files in os.walk(self.source_dir):
                for fname in files:
                    all_files.append(os.path.join(root, fname))

            total = len(all_files)
            if total == 0:
                self.finished.emit(0)
                return

            success_count = 0
            # 第二步：逐个检查并复制合法 DICOM
            for idx, fpath in enumerate(all_files, start=1):
                self.progress.emit(idx, total)

                if not is_valid_dicom(fpath):
                    continue

                try:
                    ds = dcmread(fpath, stop_before_pixels=True)
                    dest_path = build_storage_path(self.temp_dir, ds)

                    # 如果目标已存在，避免覆盖（添加时间戳后缀）
                    if os.path.exists(dest_path):
                        base, ext = os.path.splitext(dest_path)
                        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
                        dest_path = f"{base}_{timestamp}{ext}"

                    shutil.copy2(fpath, dest_path)
                    success_count += 1
                    logger.info(f"本地载入: {fpath} -> {dest_path}")
                except Exception as e:
                    logger.warning(f"复制文件失败 {fpath}: {e}")

            self.finished.emit(success_count)
        except Exception as e:
            logger.exception("本地载入线程异常")
            self.error.emit(f"本地载入异常: {e}")


# ------------------------------------------------------------------------------
# 本地载入控制器（封装线程生命周期）
# ------------------------------------------------------------------------------

class LocalDicomLoader(QObject):
    """
    本地 DICOM 文件夹载入控制器。

    封装 QThread + LocalLoadWorker，提供简洁的 start(source_dir) 接口。
    使用方式：
        loader = LocalDicomLoader()
        loader.signals.local_load_progress.connect(ui.progress_bar.setValue)
        loader.signals.local_load_finished.connect(on_finished)
        loader.start("D:/RawDicomData")
    """

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.signals = DicomInputSignals()
        self._thread: Optional[QThread] = None
        self._worker: Optional[LocalLoadWorker] = None

    def start(self, source_dir: str, temp_dir: Optional[str] = None):
        """启动本地载入任务。"""
        self._cleanup()

        self._thread = QThread(self)
        self._worker = LocalLoadWorker(source_dir, temp_dir)
        self._worker.moveToThread(self._thread)

        # 连接工作线程信号到控制器信号（转发到 UI）
        self._worker.progress.connect(self.signals.local_load_progress)
        self._worker.finished.connect(self.signals.local_load_finished)
        self._worker.error.connect(self.signals.error_occurred)

        # 线程启动时执行 worker.run
        self._thread.started.connect(self._worker.run)
        # 完成后清理 C++ 对象
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        # 关键：线程彻底结束后，清空 Python 侧的引用，避免下次访问已删除的 C++ 对象
        self._thread.finished.connect(self._on_thread_finished)

        self._thread.start()

    def _on_thread_finished(self):
        """线程彻底结束后，清空 Python 引用。"""
        self._thread = None
        self._worker = None

    def _cleanup(self):
        """清理上一个线程（如果有）。"""
        try:
            if self._thread and self._thread.isRunning():
                self._thread.quit()
                self._thread.wait(2000)
        except RuntimeError:
            # C++ 对象已被 Qt 删除（deleteLater 已执行），安全忽略
            pass
        finally:
            self._thread = None
            self._worker = None


# ------------------------------------------------------------------------------
# 统一的数据输入管理器
# ------------------------------------------------------------------------------

class DicomInputManager(QObject):
    """
    数据输入管理器。

    整合 SCP 和本地载入两种输入方式，对外提供统一的信号和控制接口。
    通常由 main.py 实例化，并将其信号连接到 MainWindow。
    """

    def __init__(self, ae_title: str = "MIX_SCP", port: int = 11112,
                 temp_dir: Optional[str] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.temp_dir = temp_dir or get_temp_base_dir()
        self.signals = DicomInputSignals()

        # SCP 服务端实例
        self.scp = DicomSCP(ae_title=ae_title, port=port, temp_dir=self.temp_dir)
        # 将 SCP 的内部信号转发到管理器的信号
        self.scp.signals.study_received.connect(self.signals.study_received)
        self.scp.signals.scp_status_changed.connect(self.signals.scp_status_changed)
        self.scp.signals.error_occurred.connect(self.signals.error_occurred)

        # 本地载入控制器
        self.local_loader = LocalDicomLoader()
        self.local_loader.signals.local_load_progress.connect(self.signals.local_load_progress)
        self.local_loader.signals.local_load_finished.connect(self.signals.local_load_finished)
        self.local_loader.signals.error_occurred.connect(self.signals.error_occurred)

    # ---------- SCP 控制 ----------

    def start_scp(self):
        """启动 SCP 接收服务。"""
        self.scp.start()

    def stop_scp(self):
        """停止 SCP 接收服务。"""
        self.scp.stop()

    def is_scp_running(self) -> bool:
        """查询 SCP 运行状态。"""
        return self.scp.is_running()

    # ---------- 本地载入 ----------

    def load_local_folder(self, source_dir: str):
        """启动本地文件夹载入任务。"""
        self.local_loader.start(source_dir, self.temp_dir)

    # ---------- 目录扫描 ----------

    def scan_temp_studies(self, date_filter: Optional[str] = None) -> List[Dict]:
        """扫描临时目录，返回所有 Study 的结构化列表（供 UI 刷新树形控件）。

        参数：
            date_filter: 日期过滤条件（today/last3days/last7days/last30days/all）
        """
        return scan_temp_directory(self.temp_dir, date_filter)


# ------------------------------------------------------------------------------
# 独立测试入口
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    """
    独立运行此文件可进行 SCP 服务端测试或本地载入测试。
    例如：
        python dicom_input.py
    """
    import sys
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication(sys.argv)

    # --- 测试 1：启动 SCP 并运行 60 秒 ---
    manager = DicomInputManager(ae_title="MIX_SCP", port=11112)

    def on_study_received(study_uid, patient_name):
        print(f"[信号] 接收到新检查: {patient_name} | UID: {study_uid}")

    def on_scp_status(running, msg):
        print(f"[信号] SCP 状态: running={running}, msg={msg}")

    def on_error(msg):
        print(f"[信号] 错误: {msg}")

    manager.signals.study_received.connect(on_study_received)
    manager.signals.scp_status_changed.connect(on_scp_status)
    manager.signals.error_occurred.connect(on_error)

    print("测试模式：启动 SCP 监听 60 秒...")
    manager.start_scp()

    # 使用 QThread 做延时，保持事件循环
    from PySide6.QtCore import QTimer
    QTimer.singleShot(60000, app.quit)
    sys.exit(app.exec())
