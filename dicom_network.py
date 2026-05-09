# -*- coding: utf-8 -*-
"""
dicom_network.py
网络交互模块

负责与 PACS 服务器的 DICOM 网络通信，提供两种核心功能：
1. C-FIND SCU：作为服务类用户，向 PACS 查询患者/检查信息。
2. C-STORE SCU：作为服务类用户，将处理后的 DICOM 文件推送到 PACS。

所有网络操作均在独立工作线程中执行，通过 Qt 信号与主 UI 线程通信，
避免阻塞界面响应。

技术栈：pynetdicom, PySide6
"""

import os
import logging
from typing import List, Dict, Optional

from PySide6.QtCore import QObject, QThread, Signal

from pynetdicom import AE, debug_logger
from pynetdicom.sop_class import (
    PatientRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelFind,
    Verification,
)
from pydicom.dataset import Dataset
from pydicom.uid import ImplicitVRLittleEndian, ExplicitVRLittleEndian

# ------------------------------------------------------------------------------
# 日志
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dicom_network")

# ------------------------------------------------------------------------------
# 信号定义
# ------------------------------------------------------------------------------

class DicomNetworkSignals(QObject):
    """
    网络模块向主 UI 发出的信号。
    由 main.py 将此处信号实例连接到 MainWindow 的同名信号。
    """
    # C-FIND 查询结果返回（List[Dict]）
    find_results_ready = Signal(list)
    # C-STORE 发送进度（当前数, 总数）
    store_progress = Signal(int, int)
    # C-STORE 发送完成（成功数, 总数）
    store_finished = Signal(int, int)
    # 通用错误信息
    error_occurred = Signal(str)

    # DSA C-FIND 查询结果（List[Dict]）
    dsa_find_results_ready = Signal(list)
    # DSA C-MOVE 拉取进度（当前数, 总数）
    dsa_move_progress = Signal(int, int)
    # DSA C-MOVE 拉取完成（成功数, 总数）
    dsa_move_finished = Signal(int, int)


# ------------------------------------------------------------------------------
# PACS 节点配置
# ------------------------------------------------------------------------------

class PacsNodeConfig:
    """
    PACS 节点配置数据类。

    属性：
        ae_title (str): 远端 PACS 的 AE Title
        host (str): PACS 服务器 IP 地址或主机名
        port (int): PACS 监听端口（通常为 104 或 11112）
        local_ae_title (str): 本机作为 SCU 时的 AE Title
    """
    def __init__(self, ae_title: str = "PACS", host: str = "127.0.0.1",
                 port: int = 11112, local_ae_title: str = "MIX_SCU"):
        self.ae_title = ae_title
        self.host = host
        self.port = port
        self.local_ae_title = local_ae_title

    def __repr__(self) -> str:
        return f"PacsNodeConfig({self.ae_title}@{self.host}:{self.port}, local={self.local_ae_title})"


# ------------------------------------------------------------------------------
# C-FIND 工作线程
# ------------------------------------------------------------------------------

class CFindWorker(QObject):
    """
    C-FIND SCU 工作线程。

    根据用户输入的查询条件（患者姓名、患者ID、检查号等）构建 C-FIND 请求，
    发送至 PACS 服务器，并将返回的匹配结果以列表形式发射回 UI。

    信号：
        finished(List[Dict]): 查询成功完成，携带结果列表
        error(str): 查询过程中发生错误
    """

    finished = Signal(list)
    error = Signal(str)

    def __init__(self, pacs_config: PacsNodeConfig, query_params: Dict, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.pacs = pacs_config
        self.query_params = query_params

    def run(self):
        """执行 C-FIND 查询。"""
        try:
            logger.info(f"开始 C-FIND 查询: {self.query_params} -> {self.pacs}")

            # 1. 创建 Application Entity（本机 SCU）
            ae = AE(ae_title=self.pacs.local_ae_title)

            # 2. 添加查询上下文（支持 Patient Root 和 Study Root）
            # 通常 PACS 至少支持其中一种
            ae.add_requested_context(PatientRootQueryRetrieveInformationModelFind)
            ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)

            # 3. 构建查询数据集（Query Dataset）
            # 使用 Study Root 模型进行查询，级别为 STUDY
            ds = Dataset()
            ds.QueryRetrieveLevel = "STUDY"

            # 设置查询关键字（空白表示通配/返回）
            # 注意：PACS 对模糊查询的支持取决于具体实现
            ds.PatientName = self.query_params.get("patient_name", "")
            ds.PatientID = self.query_params.get("patient_id", "")
            ds.AccessionNumber = self.query_params.get("accession_number", "")
            ds.StudyInstanceUID = ""  # 留空表示返回所有匹配的 StudyUID
            ds.StudyDate = ""
            ds.StudyTime = ""
            ds.StudyDescription = ""
            ds.NumberOfStudyRelatedSeries = ""
            ds.NumberOfStudyRelatedInstances = ""

            # 4. 建立关联（Association）
            assoc = ae.associate(self.pacs.host, self.pacs.port, ae_title=self.pacs.ae_title)

            if not assoc.is_established:
                error_msg = f"无法连接到 PACS: {self.pacs.ae_title}@{self.pacs.host}:{self.pacs.port}"
                logger.error(error_msg)
                self.error.emit(error_msg)
                return

            results = []
            try:
                # 5. 发送 C-FIND 请求
                # 优先尝试 Study Root 模型
                responses = assoc.send_c_find(ds, StudyRootQueryRetrieveInformationModelFind)

                for status, identifier in responses:
                    if status and status.Status == 0xFF00:  # Pending（匹配结果）
                        if identifier is not None:
                            result = {
                                "patient_name": getattr(identifier, "PatientName", ""),
                                "patient_id": getattr(identifier, "PatientID", ""),
                                "accession_number": getattr(identifier, "AccessionNumber", ""),
                                "study_instance_uid": getattr(identifier, "StudyInstanceUID", ""),
                                "study_date": getattr(identifier, "StudyDate", ""),
                                "study_description": getattr(identifier, "StudyDescription", ""),
                            }
                            results.append(result)
                            logger.debug(f"C-FIND 匹配结果: {result}")
                    elif status and status.Status == 0x0000:  # Success（查询完成）
                        logger.info(f"C-FIND 查询完成，共 {len(results)} 条记录")
                    elif status and status.Status & 0xF000 == 0xF000:  # 失败类状态码
                        error_msg = f"C-FIND 失败，状态码: 0x{status.Status:04X}"
                        logger.error(error_msg)
                        self.error.emit(error_msg)
                        return

            finally:
                assoc.release()

            self.finished.emit(results)

        except Exception as e:
            logger.exception("C-FIND 查询异常")
            self.error.emit(f"C-FIND 异常: {e}")


# ------------------------------------------------------------------------------
# C-STORE 工作线程
# ------------------------------------------------------------------------------

class CStoreWorker(QObject):
    """
    C-STORE SCU 工作线程。

    将本地 DICOM 文件列表逐个通过 C-STORE 推送到 PACS 服务器。
    优化策略：所有文件复用同一个 Association，减少频繁建连/断连开销。

    信号：
        progress(int, int): 当前发送数, 总数
        finished(int, int): 成功发送数, 总数
        error(str): 错误信息
    """

    progress = Signal(int, int)
    finished = Signal(int, int)
    error = Signal(str)

    def __init__(self, pacs_config: PacsNodeConfig, file_list: List[str],
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.pacs = pacs_config
        self.file_list = [f for f in file_list if os.path.isfile(f)]

    def run(self):
        """执行 C-STORE 发送。"""
        total = len(self.file_list)
        if total == 0:
            self.finished.emit(0, 0)
            return

        success_count = 0
        ae = None
        assoc = None

        try:
            logger.info(f"开始 C-STORE 发送 {total} 个文件到 {self.pacs}")
            ae = AE(ae_title=self.pacs.local_ae_title)

            # 为所有待发送的文件添加对应的 Storage Presentation Contexts
            # pynetdicom 提供所有标准 Storage SOP Classes
            from pynetdicom.presentation import AllStoragePresentationContexts
            ae.requested_contexts = AllStoragePresentationContexts[:100]

            # 建立关联
            assoc = ae.associate(self.pacs.host, self.pacs.port, ae_title=self.pacs.ae_title)
            if not assoc.is_established:
                error_msg = f"无法连接到 PACS 进行 C-STORE: {self.pacs}"
                logger.error(error_msg)
                self.error.emit(error_msg)
                return

            for idx, fpath in enumerate(self.file_list, start=1):
                self.progress.emit(idx, total)
                try:
                    # 读取 DICOM 文件（需要完整读取，因为要发送整个文件）
                    from pydicom import dcmread
                    ds = dcmread(fpath)

                    # 确保 file_meta 存在（pynetdicom 发送时需要）
                    if not hasattr(ds, "file_meta") or ds.file_meta is None:
                        ds.file_meta = Dataset()
                        ds.file_meta.MediaStorageSOPClassUID = getattr(
                            ds, "SOPClassUID", "1.2.840.10008.5.1.4.1.1.2"
                        )
                        ds.file_meta.MediaStorageSOPInstanceUID = getattr(
                            ds, "SOPInstanceUID", "1.2.3.4.5"
                        )
                        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
                        ds.file_meta.ImplementationClassUID = "1.2.826.0.1.3680043.9.7756.1"

                    # 发送 C-STORE
                    status = assoc.send_c_store(ds)

                    if status and status.Status == 0x0000:
                        success_count += 1
                        logger.info(f"C-STORE 成功: {os.path.basename(fpath)}")
                    else:
                        status_hex = f"0x{status.Status:04X}" if status else "None"
                        logger.warning(f"C-STORE 失败 {os.path.basename(fpath)}, 状态: {status_hex}")

                except Exception as e:
                    logger.warning(f"发送文件失败 {fpath}: {e}")

            self.finished.emit(success_count, total)
            logger.info(f"C-STORE 完成: 成功 {success_count}/{total}")

        except Exception as e:
            logger.exception("C-STORE 发送异常")
            self.error.emit(f"C-STORE 异常: {e}")
        finally:
            if assoc and assoc.is_established:
                assoc.release()


# ------------------------------------------------------------------------------
# C-MOVE 工作线程
# ------------------------------------------------------------------------------

class CMoveWorker(QObject):
    """
    C-MOVE SCU 工作线程。

    向远端 DICOM 节点（如 DSA 工作站）发起 C-MOVE 请求，
    指示远端将指定检查的图像推送到本机 SCP。

    信号：
        progress(int, int): 进度（当前数, 总数）
        finished(int, int): 完成（成功数, 总数）
        error(str): 错误信息
    """

    progress = Signal(int, int)
    finished = Signal(int, int)
    error = Signal(str)

    def __init__(self, remote_config: PacsNodeConfig, study_uid: str,
                 move_dest: str, parent: Optional[QObject] = None):
        """
        参数：
            remote_config: 远端节点配置（DSA 工作站）
            study_uid: 要拉取的 StudyInstanceUID
            move_dest: C-MOVE 目标 AE Title（本机 SCP 的 AE Title）
        """
        super().__init__(parent)
        self.remote = remote_config
        self.study_uid = study_uid
        self.move_dest = move_dest

    def run(self):
        """执行 C-MOVE 请求。"""
        ae = None
        assoc = None
        try:
            logger.info(f"开始 C-MOVE: study={self.study_uid}, 目标={self.move_dest}")
            ae = AE(ae_title=self.remote.local_ae_title)

            # 添加 C-MOVE 上下文
            from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelMove
            ae.add_requested_context(StudyRootQueryRetrieveInformationModelMove)

            assoc = ae.associate(
                self.remote.host, self.remote.port,
                ae_title=self.remote.ae_title
            )
            if not assoc.is_established:
                error_msg = f"无法连接到远端节点进行 C-MOVE: {self.remote}"
                logger.error(error_msg)
                self.error.emit(error_msg)
                return

            # 构建 C-MOVE 请求数据集
            from pydicom import Dataset
            ds = Dataset()
            ds.QueryRetrieveLevel = "STUDY"
            ds.StudyInstanceUID = self.study_uid

            # 发送 C-MOVE，move_dest 是目标 AE Title
            responses = assoc.send_c_move(ds, move_dest=self.move_dest,
                                          query_model=StudyRootQueryRetrieveInformationModelMove)

            success = 0
            total = 0
            for status, identifier in responses:
                if status:
                    status_type = status.Status
                    if status_type == 0x0000:
                        success += 1
                        total += 1
                        self.progress.emit(success, total)
                    elif status_type in (0xFF00, 0xFF01):
                        # Pending - operation in progress
                        total += 1
                        self.progress.emit(success, total)
                    else:
                        total += 1
                        logger.warning(f"C-MOVE 响应状态: 0x{status_type:04X}")

            self.finished.emit(success, max(total, 1))
            logger.info(f"C-MOVE 完成: 成功 {success}/{total}")

        except Exception as e:
            logger.exception("C-MOVE 异常")
            self.error.emit(f"C-MOVE 异常: {e}")
        finally:
            if assoc and assoc.is_established:
                assoc.release()


# ------------------------------------------------------------------------------
# 网络管理器（封装线程生命周期）
# ------------------------------------------------------------------------------

class DicomNetworkManager(QObject):
    """
    DICOM 网络通信管理器。

    封装 C-FIND 与 C-STORE 的线程调度，对外提供简单的调用接口：
        find_studies(query_dict)  -> 触发 find_results_ready 信号
        send_files(file_list)     -> 触发 store_progress / store_finished 信号

    属性：
        pacs_config (PacsNodeConfig): PACS 节点配置
        signals (DicomNetworkSignals): 统一信号接口
    """

    def __init__(self, pacs_config: Optional[PacsNodeConfig] = None,
                 dsa_config: Optional[PacsNodeConfig] = None,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.pacs_config = pacs_config or PacsNodeConfig()
        self.dsa_config = dsa_config
        self.signals = DicomNetworkSignals()

        self._find_thread: Optional[QThread] = None
        self._find_worker: Optional[CFindWorker] = None
        self._store_thread: Optional[QThread] = None
        self._store_worker: Optional[CStoreWorker] = None
        self._dsa_find_thread: Optional[QThread] = None
        self._dsa_find_worker: Optional[CFindWorker] = None
        self._dsa_move_thread: Optional[QThread] = None
        self._dsa_move_worker: Optional[CMoveWorker] = None

    # ---------- C-FIND 接口 ----------

    def find_studies(self, query_dict: Dict):
        """
        启动 C-FIND 查询任务。

        参数：
            query_dict: 查询条件字典，可包含 patient_name, patient_id, accession_number 等
        """
        self._cleanup_find()

        self._find_thread = QThread(self)
        self._find_worker = CFindWorker(self.pacs_config, query_dict)
        self._find_worker.moveToThread(self._find_thread)

        # 信号转发
        self._find_worker.finished.connect(self.signals.find_results_ready)
        self._find_worker.error.connect(self.signals.error_occurred)

        # 生命周期
        self._find_thread.started.connect(self._find_worker.run)
        self._find_worker.finished.connect(self._find_thread.quit)
        self._find_worker.finished.connect(self._find_worker.deleteLater)
        self._find_thread.finished.connect(self._find_thread.deleteLater)

        self._find_thread.start()
        logger.info(f"已启动 C-FIND 线程，查询条件: {query_dict}")

    def _cleanup_find(self):
        """清理之前的 C-FIND 线程。"""
        if self._find_thread and self._find_thread.isRunning():
            self._find_thread.quit()
            self._find_thread.wait(2000)

    # ---------- C-STORE 接口 ----------

    def send_files(self, file_list: List[str]):
        """
        启动 C-STORE 发送任务。

        参数：
            file_list: 待发送的 DICOM 文件绝对路径列表
        """
        self._cleanup_store()

        self._store_thread = QThread(self)
        self._store_worker = CStoreWorker(self.pacs_config, file_list)
        self._store_worker.moveToThread(self._store_thread)

        # 信号转发
        self._store_worker.progress.connect(self.signals.store_progress)
        self._store_worker.finished.connect(self.signals.store_finished)
        self._store_worker.error.connect(self.signals.error_occurred)

        # 生命周期
        self._store_thread.started.connect(self._store_worker.run)
        self._store_worker.finished.connect(self._store_thread.quit)
        self._store_worker.finished.connect(self._store_worker.deleteLater)
        self._store_thread.finished.connect(self._store_thread.deleteLater)

        self._store_thread.start()
        logger.info(f"已启动 C-STORE 线程，文件数: {len(file_list)}")

    def _cleanup_store(self):
        """清理之前的 C-STORE 线程。"""
        if self._store_thread and self._store_thread.isRunning():
            self._store_thread.quit()
            self._store_thread.wait(2000)

    # ---------- DSA C-FIND 接口 ----------

    def query_dsa(self, query_dict: Dict):
        """
        向 DSA 工作站发起 C-FIND 查询。

        参数：
            query_dict: 查询条件字典
        """
        if not self.dsa_config:
            self.signals.error_occurred.emit("未配置 DSA 节点")
            return

        self._cleanup_dsa_find()

        self._dsa_find_thread = QThread(self)
        self._dsa_find_worker = CFindWorker(self.dsa_config, query_dict)
        self._dsa_find_worker.moveToThread(self._dsa_find_thread)

        self._dsa_find_worker.finished.connect(self.signals.dsa_find_results_ready)
        self._dsa_find_worker.error.connect(self.signals.error_occurred)

        self._dsa_find_thread.started.connect(self._dsa_find_worker.run)
        self._dsa_find_worker.finished.connect(self._dsa_find_thread.quit)
        self._dsa_find_worker.finished.connect(self._dsa_find_worker.deleteLater)
        self._dsa_find_thread.finished.connect(self._dsa_find_thread.deleteLater)

        self._dsa_find_thread.start()
        logger.info(f"已启动 DSA C-FIND，查询条件: {query_dict}")

    def _cleanup_dsa_find(self):
        """清理之前的 DSA C-FIND 线程。"""
        if self._dsa_find_thread and self._dsa_find_thread.isRunning():
            self._dsa_find_thread.quit()
            self._dsa_find_thread.wait(2000)

    # ---------- DSA C-MOVE 接口 ----------

    def move_from_dsa(self, study_uid: str, move_dest_ae: str):
        """
        向 DSA 工作站发起 C-MOVE 请求，将指定检查拉取到本机 SCP。

        参数：
            study_uid: StudyInstanceUID
            move_dest_ae: 本机 SCP 的 AE Title（C-MOVE 目标）
        """
        if not self.dsa_config:
            self.signals.error_occurred.emit("未配置 DSA 节点")
            return

        self._cleanup_dsa_move()

        self._dsa_move_thread = QThread(self)
        self._dsa_move_worker = CMoveWorker(self.dsa_config, study_uid, move_dest_ae)
        self._dsa_move_worker.moveToThread(self._dsa_move_thread)

        self._dsa_move_worker.progress.connect(self.signals.dsa_move_progress)
        self._dsa_move_worker.finished.connect(self.signals.dsa_move_finished)
        self._dsa_move_worker.error.connect(self.signals.error_occurred)

        self._dsa_move_thread.started.connect(self._dsa_move_worker.run)
        self._dsa_move_worker.finished.connect(self._dsa_move_thread.quit)
        self._dsa_move_worker.finished.connect(self._dsa_move_worker.deleteLater)
        self._dsa_move_thread.finished.connect(self._dsa_move_thread.deleteLater)

        self._dsa_move_thread.start()
        logger.info(f"已启动 DSA C-MOVE，study={study_uid}, 目标={move_dest_ae}")

    def _cleanup_dsa_move(self):
        """清理之前的 DSA C-MOVE 线程。"""
        if self._dsa_move_thread and self._dsa_move_thread.isRunning():
            self._dsa_move_thread.quit()
            self._dsa_move_thread.wait(2000)


# ------------------------------------------------------------------------------
# 独立测试入口
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    """
    独立运行此文件可进行 C-FIND 或 C-STORE 测试。
    需要先配置一个可用的 PACS 测试节点。

    示例：
        python dicom_network.py
    """
    import sys
    from PySide6.QtCore import QCoreApplication, QTimer

    app = QCoreApplication(sys.argv)

    # 配置测试 PACS 节点（请根据实际情况修改）
    config = PacsNodeConfig(
        ae_title="TEST_PACS",
        host="127.0.0.1",
        port=11112,
        local_ae_title="MIX_TEST_SCU"
    )

    manager = DicomNetworkManager(config)

    def on_find_results(results):
        print(f"\n[C-FIND 结果] 共 {len(results)} 条:")
        for r in results:
            print(f"  - {r['patient_name']} | ID:{r['patient_id']} | Acc:{r['accession_number']} | UID:{r['study_instance_uid']}")
        app.quit()

    def on_error(msg):
        print(f"[错误] {msg}")
        app.quit()

    manager.signals.find_results_ready.connect(on_find_results)
    manager.signals.error_occurred.connect(on_error)

    # 测试查询（空条件表示查询所有，实际 PACS 可能拒绝）
    print("启动 C-FIND 测试查询...")
    manager.find_studies({
        "patient_name": "*",
        "patient_id": "",
        "accession_number": "",
    })

    # 30 秒超时
    QTimer.singleShot(30000, app.quit)
    sys.exit(app.exec())
