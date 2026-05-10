# -*- coding: utf-8 -*-
"""
dicom_network.py
网络交互模块

负责与主机服务器的 DICOM 网络通信，提供两种核心功能：
1. C-FIND SCU：作为服务类用户，向主机查询患者/检查信息。
2. C-STORE SCU：作为服务类用户，将处理后的 DICOM 文件推送到主机。

所有网络操作均在独立工作线程中执行，通过 Qt 信号与主 UI 线程通信，
避免阻塞界面响应。

技术栈：pynetdicom, PySide6
"""

import os
import logging
from datetime import datetime, timedelta
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

    # PACS C-MOVE 拉取进度（当前数, 总数）
    pacs_move_progress = Signal(int, int)
    # PACS C-MOVE 拉取完成（成功数, 总数）
    pacs_move_finished = Signal(int, int)


# ------------------------------------------------------------------------------
# 主机节点配置
# ------------------------------------------------------------------------------

class PacsNodeConfig:
    """
    主机节点配置数据类。

    属性：
        ae_title (str): 远端主机的 AE Title
        host (str): 主机服务器 IP 地址或主机名
        port (int): 主机监听端口（通常为 104 或 11112）
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
    发送至主机服务器，并将返回的匹配结果以列表形式发射回 UI。

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

    @staticmethod
    def _format_study_date(date_range: str) -> str:
        """将日期范围描述转换为 DICOM StudyDate 格式 (YYYYMMDD-YYYYMMDD)。"""
        today = datetime.now().date()
        today_str = today.strftime("%Y%m%d")

        mapping = {
            "TODAY": today_str,
            "LAST3DAYS": (today - timedelta(days=2)).strftime("%Y%m%d") + "-" + today_str,
            "LAST7DAYS": (today - timedelta(days=6)).strftime("%Y%m%d") + "-" + today_str,
            "LAST30DAYS": (today - timedelta(days=29)).strftime("%Y%m%d") + "-" + today_str,
        }
        return mapping.get(date_range.upper(), date_range)

    def run(self):
        """执行 C-FIND 查询。"""
        try:
            logger.info(f"开始 C-FIND 查询: {self.query_params} -> {self.pacs}")

            # 1. 创建 Application Entity（本机 SCU）
            ae = AE(ae_title=self.pacs.local_ae_title)

            # 2. 添加查询上下文（支持 Patient Root 和 Study Root）
            # 通常主机至少支持其中一种
            ae.add_requested_context(PatientRootQueryRetrieveInformationModelFind)
            ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)

            # 3. 构建查询数据集（Query Dataset）
            # 使用 Study Root 模型进行查询，级别为 STUDY
            ds = Dataset()
            ds.QueryRetrieveLevel = "STUDY"

            # 设置查询关键字（空白表示通配/返回）
            # 注意：主机对模糊查询的支持取决于具体实现
            ds.PatientName = self.query_params.get("patient_name", "")
            ds.PatientID = self.query_params.get("patient_id", "")
            ds.AccessionNumber = self.query_params.get("accession_number", "")
            ds.StudyInstanceUID = ""  # 留空表示返回所有匹配的 StudyUID
            ds.StudyDate = self._format_study_date(
                self.query_params.get("study_date_range", "")
            )
            ds.StudyTime = ""
            ds.StudyDescription = ""
            ds.NumberOfStudyRelatedSeries = ""
            ds.NumberOfStudyRelatedInstances = ""

            # 4. 建立关联（Association）
            assoc = ae.associate(self.pacs.host, self.pacs.port, ae_title=self.pacs.ae_title)

            if not assoc.is_established:
                error_msg = f"无法连接到主机: {self.pacs.ae_title}@{self.pacs.host}:{self.pacs.port}"
                logger.error(error_msg)
                self.error.emit(error_msg)
                return

            results = []
            try:
                # 5. 发送 C-FIND 请求
                # 优先尝试 Study Root 模型
                responses = assoc.send_c_find(ds, StudyRootQueryRetrieveInformationModelFind)

                for status, identifier in responses:
                    if not status:
                        continue
                    if status.Status == 0xFF00:  # Pending（匹配结果）
                        if identifier is not None:
                            result = {
                                "patient_name": str(getattr(identifier, "PatientName", "")),
                                "patient_id": str(getattr(identifier, "PatientID", "")),
                                "accession_number": str(getattr(identifier, "AccessionNumber", "")),
                                "study_instance_uid": str(getattr(identifier, "StudyInstanceUID", "")),
                                "study_date": str(getattr(identifier, "StudyDate", "")),
                                "study_description": str(getattr(identifier, "StudyDescription", "")),
                            }
                            results.append(result)
                            logger.debug(f"C-FIND 匹配结果: {result}")
                    elif status.Status == 0x0000:  # Success（查询完成）
                        logger.info(f"C-FIND 查询完成，共 {len(results)} 条记录")
                    elif status.Status in (0xFF01,):  # Pending with warning
                        logger.warning(f"C-FIND 警告状态: 0x{status.Status:04X}")
                    else:  # 覆盖所有拒绝/失败状态码 (0xAxxx, 0xBxxx, 0xCxxx, 0xFxxx)
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

    将本地 DICOM 文件列表逐个通过 C-STORE 推送到主机服务器。
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

            # 先扫描所有文件，收集唯一的 SOP Class UID 和文件信息
            from pydicom import dcmread
            file_entries = []
            unique_sop_classes = set()
            for fpath in self.file_list:
                try:
                    ds = dcmread(fpath, stop_before_pixels=True)
                    sop_class = getattr(ds, "SOPClassUID", None)
                    if sop_class:
                        file_entries.append((fpath, str(sop_class)))
                        unique_sop_classes.add(str(sop_class))
                    else:
                        logger.warning(f"文件缺少 SOPClassUID，跳过: {fpath}")
                except Exception as e:
                    logger.warning(f"读取文件头失败 {fpath}: {e}")

            if not file_entries:
                self.error.emit("没有可发送的有效 DICOM 文件")
                return

            # 只为实际需要的 SOP Classes 添加 Presentation Contexts
            # 避免协商过多上下文导致对方拒绝
            ae = AE(ae_title=self.pacs.local_ae_title)
            for sop_class in unique_sop_classes:
                ae.add_requested_context(sop_class)

            # 建立关联
            assoc = ae.associate(self.pacs.host, self.pacs.port, ae_title=self.pacs.ae_title)
            if not assoc.is_established:
                error_msg = f"无法连接到主机进行 C-STORE: {self.pacs}"
                logger.error(error_msg)
                self.error.emit(error_msg)
                return

            try:
                for idx, (fpath, sop_class) in enumerate(file_entries, start=1):
                    self.progress.emit(idx, len(file_entries))
                    try:
                        # 完整读取 DICOM 文件（包含像素数据）
                        ds = dcmread(fpath)

                        # 确保 file_meta 存在（pynetdicom 发送时需要）
                        if not hasattr(ds, "file_meta") or ds.file_meta is None:
                            ds.file_meta = Dataset()
                            ds.file_meta.MediaStorageSOPClassUID = getattr(
                                ds, "SOPClassUID", sop_class
                            )
                            ds.file_meta.MediaStorageSOPInstanceUID = getattr(
                                ds, "SOPInstanceUID", "1.2.3.4.5"
                            )
                            # 根据文件实际编码设置传输语法，避免编码不一致
                            if ds.is_little_endian and ds.is_implicit_VR:
                                ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
                            elif ds.is_little_endian and not ds.is_implicit_VR:
                                ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
                            else:
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
            finally:
                if assoc.is_established:
                    assoc.release()

            self.finished.emit(success_count, len(file_entries))
            logger.info(f"C-STORE 完成: 成功 {success_count}/{len(file_entries)}")

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
            responses = assoc.send_c_move(
                ds, self.move_dest,
                query_model=StudyRootQueryRetrieveInformationModelMove
            )

            max_completed = 0
            max_total = 0
            pending_count = 0
            final_found = False
            for status, identifier in responses:
                if not status:
                    continue
                logger.info(f"C-MOVE 响应: status=0x{status.Status:04X}")
                if status.Status == 0xFF00:
                    # Pending - operation in progress
                    pending_count += 1
                    if identifier:
                        completed = getattr(identifier, 'NumberOfCompletedSuboperations', 0) or 0
                        remaining = getattr(identifier, 'NumberOfRemainingSuboperations', 0) or 0
                        total_ops = completed + remaining
                        # 即使远端不返回 remaining，只要有 completed 就更新计数
                        max_completed = max(max_completed, completed)
                        if total_ops > 0:
                            max_total = max(max_total, total_ops)
                        elif completed > 0:
                            # 远端不返回 remaining 时，用 completed 作为总数估计
                            max_total = max(max_total, completed)
                        self.progress.emit(completed, max(total_ops, 1))
                elif status.Status in (0x0000, 0xB000):
                    # 0x0000 = 成功完成, 0xB000 = 完成但有部分警告
                    final_found = True
                    if identifier:
                        completed = getattr(identifier, 'NumberOfCompletedSuboperations', 0) or 0
                        failed = getattr(identifier, 'NumberOfFailedSuboperations', 0) or 0
                        total_ops = completed + failed
                        max_completed = max(max_completed, completed)
                        if total_ops > 0:
                            max_total = max(max_total, total_ops)
                        elif completed > 0:
                            max_total = max(max_total, completed)
                        if max_completed == 0 and pending_count > 0:
                            max_completed = pending_count
                        self.finished.emit(max_completed, max(max_total, max_completed, 1))
                    else:
                        # 最终响应通常不带 identifier，使用跟踪的最大值
                        if max_completed == 0 and pending_count > 0:
                            max_completed = pending_count
                        self.finished.emit(max_completed, max(max_total, max_completed, 1))
                    break
                elif status.Status == 0xFF01:
                    # Pending with warning，忽略
                    continue
                else:
                    # 失败状态 (0xAxxx, 0xCxxx, 0xFxxx 等)
                    error_msg = f"C-MOVE 失败，状态码: 0x{status.Status:04X}"
                    logger.error(error_msg)
                    self.error.emit(error_msg)
                    return

            # 如果循环正常结束（未 break），说明未收到明确的最终状态，兜底上报
            if not final_found:
                if max_completed == 0 and pending_count > 0:
                    max_completed = pending_count
                logger.warning(f"C-MOVE 未收到最终状态，使用跟踪计数: {max_completed}/{max_total}")
                self.finished.emit(max_completed, max(max_total, max_completed, 1))

            logger.info("C-MOVE 请求完成")

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
        pacs_config (PacsNodeConfig): 主机节点配置
        signals (DicomNetworkSignals): 统一信号接口
    """

    def __init__(self, pacs_config: Optional[PacsNodeConfig] = None,
                 dsa_configs: Optional[List[PacsNodeConfig]] = None,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.pacs_config = pacs_config or PacsNodeConfig()
        self.dsa_configs = dsa_configs or []
        self.signals = DicomNetworkSignals()

        self._find_thread: Optional[QThread] = None
        self._find_worker: Optional[CFindWorker] = None
        self._store_thread: Optional[QThread] = None
        self._store_worker: Optional[CStoreWorker] = None
        self._dsa_find_thread: Optional[QThread] = None
        self._dsa_find_worker: Optional[CFindWorker] = None
        self._dsa_move_thread: Optional[QThread] = None
        self._dsa_move_worker: Optional[CMoveWorker] = None
        self._current_dsa_index: int = 0
        self._pacs_move_thread: Optional[QThread] = None
        self._pacs_move_worker: Optional[CMoveWorker] = None

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
        """清理之前的 C-FIND 线程和信号连接。"""
        try:
            if self._find_thread:
                if self._find_thread.isRunning():
                    self._find_thread.quit()
                    self._find_thread.wait(2000)
                try:
                    self._find_thread.started.disconnect()
                except (RuntimeError, TypeError):
                    pass
                try:
                    self._find_thread.finished.disconnect()
                except (RuntimeError, TypeError):
                    pass
        except RuntimeError:
            pass
        try:
            if self._find_worker:
                try:
                    self._find_worker.finished.disconnect()
                except (RuntimeError, TypeError):
                    pass
                try:
                    self._find_worker.error.disconnect()
                except (RuntimeError, TypeError):
                    pass
        except RuntimeError:
            pass
        finally:
            self._find_thread = None
            self._find_worker = None

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
        """清理之前的 C-STORE 线程和信号连接。"""
        try:
            if self._store_thread:
                if self._store_thread.isRunning():
                    self._store_thread.quit()
                    self._store_thread.wait(2000)
                try:
                    self._store_thread.started.disconnect()
                except (RuntimeError, TypeError):
                    pass
                try:
                    self._store_thread.finished.disconnect()
                except (RuntimeError, TypeError):
                    pass
        except RuntimeError:
            pass
        try:
            if self._store_worker:
                try:
                    self._store_worker.progress.disconnect()
                except (RuntimeError, TypeError):
                    pass
                try:
                    self._store_worker.finished.disconnect()
                except (RuntimeError, TypeError):
                    pass
                try:
                    self._store_worker.error.disconnect()
                except (RuntimeError, TypeError):
                    pass
        except RuntimeError:
            pass
        finally:
            self._store_thread = None
            self._store_worker = None

    # ---------- DSA C-FIND 接口 ----------

    def query_dsa(self, query_dict: Dict, dsa_index: int = 0):
        """
        向指定 DSA 工作站发起 C-FIND 查询。

        参数：
            query_dict: 查询条件字典
            dsa_index: DSA 节点索引（在 dsa_configs 列表中的位置）
        """
        if not self.dsa_configs:
            self.signals.error_occurred.emit("未配置 DSA 节点")
            return
        if dsa_index < 0 or dsa_index >= len(self.dsa_configs):
            self.signals.error_occurred.emit(f"DSA 节点索引 {dsa_index} 无效")
            return

        dsa_config = self.dsa_configs[dsa_index]
        self._cleanup_dsa_find()

        self._dsa_find_thread = QThread(self)
        self._dsa_find_worker = CFindWorker(dsa_config, query_dict)
        self._dsa_find_worker.moveToThread(self._dsa_find_thread)

        self._dsa_find_worker.finished.connect(self.signals.dsa_find_results_ready)
        self._dsa_find_worker.error.connect(self.signals.error_occurred)

        self._dsa_find_thread.started.connect(self._dsa_find_worker.run)
        self._dsa_find_worker.finished.connect(self._dsa_find_thread.quit)
        self._dsa_find_worker.finished.connect(self._dsa_find_worker.deleteLater)
        self._dsa_find_thread.finished.connect(self._dsa_find_thread.deleteLater)

        self._dsa_find_thread.start()
        logger.info(f"已启动 DSA C-FIND (节点 {dsa_index}: {dsa_config}), 查询条件: {query_dict}")

    def _cleanup_dsa_find(self):
        """清理之前的 DSA C-FIND 线程和信号连接。"""
        try:
            if self._dsa_find_thread:
                if self._dsa_find_thread.isRunning():
                    self._dsa_find_thread.quit()
                    self._dsa_find_thread.wait(2000)
                try:
                    self._dsa_find_thread.started.disconnect()
                except (RuntimeError, TypeError):
                    pass
                try:
                    self._dsa_find_thread.finished.disconnect()
                except (RuntimeError, TypeError):
                    pass
        except RuntimeError:
            pass
        try:
            if self._dsa_find_worker:
                try:
                    self._dsa_find_worker.finished.disconnect()
                except (RuntimeError, TypeError):
                    pass
                try:
                    self._dsa_find_worker.error.disconnect()
                except (RuntimeError, TypeError):
                    pass
        except RuntimeError:
            pass
        finally:
            self._dsa_find_thread = None
            self._dsa_find_worker = None

    # ---------- DSA C-MOVE 接口 ----------

    def move_from_dsa(self, study_uid: str, move_dest_ae: str, dsa_index: int = 0):
        """
        向指定 DSA 工作站发起 C-MOVE 请求，将指定检查拉取到本机 SCP。

        参数：
            study_uid: StudyInstanceUID
            move_dest_ae: 本机 SCP 的 AE Title（C-MOVE 目标）
            dsa_index: DSA 节点索引（在 dsa_configs 列表中的位置）
        """
        if not self.dsa_configs:
            self.signals.error_occurred.emit("未配置 DSA 节点")
            return
        if dsa_index < 0 or dsa_index >= len(self.dsa_configs):
            self.signals.error_occurred.emit(f"DSA 节点索引 {dsa_index} 无效")
            return

        dsa_config = self.dsa_configs[dsa_index]
        self._cleanup_dsa_move()

        self._dsa_move_thread = QThread(self)
        self._dsa_move_worker = CMoveWorker(dsa_config, study_uid, move_dest_ae)
        self._dsa_move_worker.moveToThread(self._dsa_move_thread)

        self._dsa_move_worker.progress.connect(self.signals.dsa_move_progress)
        self._dsa_move_worker.finished.connect(self.signals.dsa_move_finished)
        self._dsa_move_worker.error.connect(self.signals.error_occurred)

        self._dsa_move_thread.started.connect(self._dsa_move_worker.run)
        self._dsa_move_worker.finished.connect(self._dsa_move_thread.quit)
        self._dsa_move_worker.finished.connect(self._dsa_move_worker.deleteLater)
        self._dsa_move_thread.finished.connect(self._dsa_move_thread.deleteLater)

        self._dsa_move_thread.start()
        logger.info(f"已启动 DSA C-MOVE (节点 {dsa_index}: {dsa_config}), study={study_uid}, 目标={move_dest_ae}")

    def _cleanup_dsa_move(self):
        """清理之前的 DSA C-MOVE 线程和信号连接。"""
        try:
            if self._dsa_move_thread:
                if self._dsa_move_thread.isRunning():
                    self._dsa_move_thread.quit()
                    self._dsa_move_thread.wait(2000)
                try:
                    self._dsa_move_thread.started.disconnect()
                except (RuntimeError, TypeError):
                    pass
                try:
                    self._dsa_move_thread.finished.disconnect()
                except (RuntimeError, TypeError):
                    pass
        except RuntimeError:
            pass
        try:
            if self._dsa_move_worker:
                try:
                    self._dsa_move_worker.progress.disconnect()
                except (RuntimeError, TypeError):
                    pass
                try:
                    self._dsa_move_worker.finished.disconnect()
                except (RuntimeError, TypeError):
                    pass
                try:
                    self._dsa_move_worker.error.disconnect()
                except (RuntimeError, TypeError):
                    pass
        except RuntimeError:
            pass
        finally:
            self._dsa_move_thread = None
            self._dsa_move_worker = None

    # ---------- PACS C-MOVE 接口 ----------

    def move_from_pacs(self, study_uid: str, move_dest_ae: str):
        """
        向主机（PACS）发起 C-MOVE 请求，将指定检查拉取到本机 SCP。

        参数：
            study_uid: StudyInstanceUID
            move_dest_ae: 本机 SCP 的 AE Title（C-MOVE 目标）
        """
        self._cleanup_pacs_move()

        self._pacs_move_thread = QThread(self)
        self._pacs_move_worker = CMoveWorker(self.pacs_config, study_uid, move_dest_ae)
        self._pacs_move_worker.moveToThread(self._pacs_move_thread)

        self._pacs_move_worker.progress.connect(self.signals.pacs_move_progress)
        self._pacs_move_worker.finished.connect(self.signals.pacs_move_finished)
        self._pacs_move_worker.error.connect(self.signals.error_occurred)

        self._pacs_move_thread.started.connect(self._pacs_move_worker.run)
        self._pacs_move_worker.finished.connect(self._pacs_move_thread.quit)
        self._pacs_move_worker.finished.connect(self._pacs_move_worker.deleteLater)
        self._pacs_move_thread.finished.connect(self._pacs_move_thread.deleteLater)

        self._pacs_move_thread.start()
        logger.info(f"已启动 PACS C-MOVE ({self.pacs_config}), study={study_uid}, 目标={move_dest_ae}")

    def _cleanup_pacs_move(self):
        """清理之前的 PACS C-MOVE 线程和信号连接。"""
        try:
            if self._pacs_move_thread:
                if self._pacs_move_thread.isRunning():
                    self._pacs_move_thread.quit()
                    self._pacs_move_thread.wait(2000)
                try:
                    self._pacs_move_thread.started.disconnect()
                except (RuntimeError, TypeError):
                    pass
                try:
                    self._pacs_move_thread.finished.disconnect()
                except (RuntimeError, TypeError):
                    pass
        except RuntimeError:
            pass
        try:
            if self._pacs_move_worker:
                try:
                    self._pacs_move_worker.progress.disconnect()
                except (RuntimeError, TypeError):
                    pass
                try:
                    self._pacs_move_worker.finished.disconnect()
                except (RuntimeError, TypeError):
                    pass
                try:
                    self._pacs_move_worker.error.disconnect()
                except (RuntimeError, TypeError):
                    pass
        except RuntimeError:
            pass
        finally:
            self._pacs_move_thread = None
            self._pacs_move_worker = None


# ------------------------------------------------------------------------------
# 独立测试入口
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    """
    独立运行此文件可进行 C-FIND 或 C-STORE 测试。
    需要先配置一个可用的主机测试节点。

    示例：
        python dicom_network.py
    """
    import sys
    from PySide6.QtCore import QCoreApplication, QTimer

    app = QCoreApplication(sys.argv)

    # 配置测试主机节点（请根据实际情况修改）
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

    # 测试查询（空条件表示查询所有，实际主机可能拒绝）
    print("启动 C-FIND 测试查询...")
    manager.find_studies({
        "patient_name": "*",
        "patient_id": "",
        "accession_number": "",
    })

    # 30 秒超时
    QTimer.singleShot(30000, app.quit)
    sys.exit(app.exec())
