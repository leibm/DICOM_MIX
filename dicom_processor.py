# -*- coding: utf-8 -*-
"""
dicom_processor.py
数据处理模块

负责对选定的 DICOM 文件进行患者信息覆写与 UID 重新生成。
核心功能：
1. 覆写患者信息：PatientName, PatientID, AccessionNumber, StudyInstanceUID 等。
2. 重新生成 UID：为拆分出的每个序列和实例生成全新的 SeriesInstanceUID 和 SOPInstanceUID。
3. 自动补全：若目标信息缺少 StudyInstanceUID，系统自动生成。
4. 保留私有标签：覆写过程中不删除原始文件的 Private Tags。
5. 双输出模式：处理后的文件可导出到本地目录，或暂存到临时目录等待 C-STORE 发送。

所有处理操作在独立工作线程中执行，通过 Qt 信号回传进度与结果。

技术栈：pydicom, PySide6
"""

import os
import shutil
import logging
from typing import List, Dict, Optional, Set, Tuple
from collections import defaultdict
from datetime import datetime

from PySide6.QtCore import QObject, QThread, Signal

from pydicom import dcmread, dcmwrite
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import generate_uid, ExplicitVRLittleEndian, ImplicitVRLittleEndian

# ------------------------------------------------------------------------------
# 日志
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dicom_processor")

# ------------------------------------------------------------------------------
# 信号定义
# ------------------------------------------------------------------------------

class DicomProcessorSignals(QObject):
    """
    数据处理模块向主 UI 发出的信号。
    """
    # 处理进度（当前数, 总数）
    process_progress = Signal(int, int)
    # 处理完成（成功数, 输出目录）
    process_finished = Signal(int, str)
    # 错误信息
    error_occurred = Signal(str)


# ------------------------------------------------------------------------------
# UID 生成策略
# ------------------------------------------------------------------------------

class UidStrategy:
    """
    UID 重新生成策略。

    当用户对同一原始序列（Series）下的多个实例执行拆分时，
    需要保证：
    - 同一原始序列的所有实例共享同一个**新的** SeriesInstanceUID。
    - 每个实例拥有唯一的**新的** SOPInstanceUID。

    此类负责管理原始 UID 到新 UID 的映射，确保一致性。
    """

    def __init__(self):
        # 原始 SeriesInstanceUID -> 新生成的 SeriesInstanceUID
        self._series_uid_map: Dict[str, str] = {}
        # 原始 SOPInstanceUID -> 新生成的 SOPInstanceUID
        self._sop_uid_map: Dict[str, str] = {}

    def get_new_series_uid(self, original_series_uid: str) -> str:
        """获取（或生成）对应原始 SeriesInstanceUID 的新 UID。"""
        if original_series_uid not in self._series_uid_map:
            self._series_uid_map[original_series_uid] = generate_uid()
        return self._series_uid_map[original_series_uid]

    def get_new_sop_uid(self, original_sop_uid: str) -> str:
        """获取（或生成）对应原始 SOPInstanceUID 的新 UID。"""
        if original_sop_uid not in self._sop_uid_map:
            self._sop_uid_map[original_sop_uid] = generate_uid()
        return self._sop_uid_map[original_sop_uid]


# ------------------------------------------------------------------------------
# 目标患者信息数据结构
# ------------------------------------------------------------------------------

class TargetPatientInfo:
    """
    目标患者信息。

    属性：
        patient_name (str): 患者姓名
        patient_id (str): 患者 ID
        accession_number (str): 检查号
        study_instance_uid (str): StudyInstanceUID（可为空，空时自动生成）
    """

    def __init__(self, patient_name: str = "", patient_id: str = "",
                 accession_number: str = "", study_instance_uid: str = ""):
        self.patient_name = patient_name
        self.patient_id = patient_id
        self.accession_number = accession_number
        self.study_instance_uid = study_instance_uid

    @classmethod
    def from_dict(cls, data: Dict) -> "TargetPatientInfo":
        """从字典构建（兼容 UI 传来的参数字典）。"""
        return cls(
            patient_name=data.get("patient_name", ""),
            patient_id=data.get("patient_id", ""),
            accession_number=data.get("accession_number", ""),
            study_instance_uid=data.get("study_instance_uid", ""),
        )

    def ensure_study_uid(self) -> str:
        """
        确保 StudyInstanceUID 存在。
        如果当前为空，则自动生成并返回新的 UID。
        """
        if not self.study_instance_uid:
            self.study_instance_uid = generate_uid()
            logger.info(f"目标患者信息缺少 StudyInstanceUID，已自动生成: {self.study_instance_uid}")
        return self.study_instance_uid

    def __repr__(self) -> str:
        return (f"TargetPatientInfo({self.patient_name}, {self.patient_id}, "
                f"{self.accession_number}, StudyUID={'自动生成' if not self.study_instance_uid else self.study_instance_uid[:20]+'...'})")


# ------------------------------------------------------------------------------
# 核心处理逻辑
# ------------------------------------------------------------------------------

def _list_private_tags(ds: Dataset) -> List[Tuple[int, int, str]]:
    """
    辅助调试函数：列出数据集中所有私有标签。

    返回格式：List[(group, element, keyword_or_empty)]
    例如：[(0x0019, 0x1001, ''), (0x0043, 0x1001, '')]
    """
    private_tags = []
    for elem in ds:
        if elem.tag.is_private and not elem.tag.is_private_creator:
            private_tags.append((elem.tag.group, elem.tag.element, elem.keyword))
    return private_tags


def process_single_file(
    source_path: str,
    output_dir: str,
    target_info: TargetPatientInfo,
    uid_strategy: UidStrategy,
    preserve_private_tags: bool = True
) -> Tuple[bool, str]:
    """
    处理单个 DICOM 文件：覆写患者信息、重新生成 UID、保留私有标签。

    设计原则：
    ---------------
    1. 【Series 为最小颗粒度】：调用方（UI）已确保以 Series 为单位勾选，
       本函数按 SOP Instance（单个 .dcm 文件）处理，绝不拆分多帧文件内部结构。
    2. 【不触碰像素数据】：仅修改元数据标签（Patient、Study、Series、UID 等），
       pixel_array / PixelData 保持原封不动，确保 GE DSA 等多帧文件的帧结构完整。
    3. 【私有标签保护】：pydicom 的 dcmwrite 在写入时会保留 Dataset 中的所有数据元素，
       包括私有标签（Private Tags，如 GE 的 0x0019、0x0043 组等）。
       只要不在代码中显式删除元素，私有标签即天然保留。
    4. 【强制 UID 重置】：被拆分分配给新患者的 Series，StudyInstanceUID、
       SeriesInstanceUID、SOPInstanceUID 必须全部重新生成。

    参数：
        source_path: 源 DICOM 文件路径
        output_dir: 输出目录（已按目标 Study/Series 组织好）
        target_info: 目标患者信息
        uid_strategy: UID 映射策略实例
        preserve_private_tags: 是否保留私有标签（默认 True，仅供语义说明，实际行为不受此开关影响）

    返回：
        (是否成功, 输出文件路径或错误信息)
    """
    try:
        # ------------------------------------------------------------------
        # 1. 读取 DICOM 文件（完整读取，包含 PixelData，因为需要原样写出）
        #    force=True 确保即使缺少 DICOM 前导码也能读取。
        # ------------------------------------------------------------------
        ds = dcmread(source_path, force=True)

        # 调试日志：记录处理前的私有标签数量（开发期验证用）
        if logger.isEnabledFor(logging.DEBUG):
            private_before = _list_private_tags(ds)
            logger.debug(f"处理前私有标签数量: {len(private_before)} ({source_path})")

        # ------------------------------------------------------------------
        # 2. 检查必要的 UID 标签是否存在
        # ------------------------------------------------------------------
        original_series_uid = getattr(ds, "SeriesInstanceUID", "")
        original_sop_uid = getattr(ds, "SOPInstanceUID", "")
        if not original_series_uid or not original_sop_uid:
            return False, f"文件缺少必要的 UID 标签: {source_path}"

        # ------------------------------------------------------------------
        # 3. 覆写患者信息与检查信息（0010 Patient 组、0020 Study 组、0008 AccessionNumber）
        #    注意：此处仅覆写关键标签，不删除任何其他标签，确保私有标签保留。
        # ------------------------------------------------------------------
        ds.PatientName = target_info.patient_name
        ds.PatientID = target_info.patient_id
        ds.AccessionNumber = target_info.accession_number
        ds.StudyInstanceUID = target_info.ensure_study_uid()

        # ------------------------------------------------------------------
        # 4. 强制重新生成序列级和实例级 UID
        #    同一原始 Series 的所有文件会映射到同一个新的 SeriesInstanceUID，
        #    保证拆分后的序列完整性。
        # ------------------------------------------------------------------
        new_series_uid = uid_strategy.get_new_series_uid(original_series_uid)
        new_sop_uid = uid_strategy.get_new_sop_uid(original_sop_uid)

        ds.SeriesInstanceUID = new_series_uid
        ds.SOPInstanceUID = new_sop_uid

        # ------------------------------------------------------------------
        # 5. 同步更新 file_meta 中的 MediaStorageSOPInstanceUID
        #    C-STORE 发送时，file_meta 必须与实际 SOPInstanceUID 一致。
        # ------------------------------------------------------------------
        if hasattr(ds, "file_meta") and ds.file_meta is not None:
            ds.file_meta.MediaStorageSOPInstanceUID = new_sop_uid
        else:
            # 若缺少 file_meta，创建之（C-STORE 发送时必需）
            ds.file_meta = Dataset()
            ds.file_meta.MediaStorageSOPClassUID = getattr(
                ds, "SOPClassUID", "1.2.840.10008.5.1.4.1.1.2"
            )
            ds.file_meta.MediaStorageSOPInstanceUID = new_sop_uid
            ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
            ds.file_meta.ImplementationClassUID = "1.2.826.0.1.3680043.9.7756.1"

        # ------------------------------------------------------------------
        # 6. 私有标签保护（核心兼容性要求）
        #    pydicom 的 Dataset 是一个有序字典，存储了所有 DICOM 元素。
        #    在上面的代码中，我们没有调用 del ds[tag] 或 ds.clear()，
        #    因此所有私有标签（如 GE DSA 的 0x0019xx、0x0043xx 组）
        #    仍然完整地保留在 ds 中。
        #
        #    dcmwrite 在保存时会遍历 Dataset 中的所有元素并原样写出，
        #    包括私有标签及其私有创建者（Private Creator）元素。
        #    因此，重传回 PACS 后，这些私有标签仍然可用于后续减影处理。
        # ------------------------------------------------------------------

        # ------------------------------------------------------------------
        # 7. 构建输出路径（按新的 Study/Series 层级组织）
        # ------------------------------------------------------------------
        study_dir = os.path.join(output_dir, ds.StudyInstanceUID)
        series_dir = os.path.join(study_dir, ds.SeriesInstanceUID)
        os.makedirs(series_dir, exist_ok=True)

        output_path = os.path.join(series_dir, f"{ds.SOPInstanceUID}.dcm")

        # ------------------------------------------------------------------
        # 8. 保存文件
        #    write_like_original=False：使用标准 DICOM File Format 写入，
        #    确保 file_meta 被正确编码。这不会丢失任何数据元素，
        #    所有私有标签都会被保留。
        # ------------------------------------------------------------------
        dcmwrite(output_path, ds, write_like_original=False)

        # 调试日志：验证保存后私有标签是否完整（开发期验证用）
        if logger.isEnabledFor(logging.DEBUG):
            ds_verify = dcmread(output_path, force=True)
            private_after = _list_private_tags(ds_verify)
            logger.debug(
                f"处理后私有标签数量: {len(private_after)} ({output_path}) "
                f"{'✓ 保留完整' if len(private_after) == len(private_before) else '⚠ 数量变化'}"
            )

        logger.info(f"处理成功: {source_path} -> {output_path}")
        return True, output_path

    except Exception as e:
        logger.exception(f"处理文件失败: {source_path}")
        return False, str(e)


# ------------------------------------------------------------------------------
# 处理工作线程
# ------------------------------------------------------------------------------

class ProcessWorker(QObject):
    """
    DICOM 数据处理工作线程。

    接收选中的文件列表和目标患者信息，逐个执行覆写与 UID 重新生成，
    将结果保存到指定输出目录。

    信号：
        progress(int, int): 当前处理数, 总数
        finished(int, str): 成功处理数, 输出目录路径
        error(str): 错误信息
    """

    progress = Signal(int, int)
    finished = Signal(int, str)
    error = Signal(str)

    def __init__(
        self,
        file_list: List[str],
        target_info: TargetPatientInfo,
        output_dir: str,
        preserve_private_tags: bool = True,
        parent: Optional[QObject] = None
    ):
        super().__init__(parent)
        self.file_list = [f for f in file_list if os.path.isfile(f)]
        self.target_info = target_info
        self.output_dir = output_dir
        self.preserve_private_tags = preserve_private_tags

    def run(self):
        """执行批量处理任务。"""
        total = len(self.file_list)
        if total == 0:
            self.finished.emit(0, self.output_dir)
            return

        # 确保输出目录存在
        os.makedirs(self.output_dir, exist_ok=True)

        uid_strategy = UidStrategy()
        success_count = 0
        processed_files: List[str] = []

        for idx, fpath in enumerate(self.file_list, start=1):
            self.progress.emit(idx, total)

            ok, result = process_single_file(
                source_path=fpath,
                output_dir=self.output_dir,
                target_info=self.target_info,
                uid_strategy=uid_strategy,
                preserve_private_tags=self.preserve_private_tags
            )

            if ok:
                success_count += 1
                processed_files.append(result)
            else:
                logger.warning(f"跳过文件: {fpath}, 原因: {result}")

        logger.info(f"批量处理完成: 成功 {success_count}/{total}, 输出目录: {self.output_dir}")
        self.finished.emit(success_count, self.output_dir)


# ------------------------------------------------------------------------------
# 处理器管理器（封装线程生命周期）
# ------------------------------------------------------------------------------

class DicomProcessor(QObject):
    """
    DICOM 数据处理管理器。

    对外提供统一的处理接口：
        process_and_export(file_list, target_dict, output_dir) -> 导出到本地
        process_for_store(file_list, target_dict) -> 处理到临时目录，返回文件列表

    属性：
        signals (DicomProcessorSignals): 统一信号接口
        temp_dir (str): 内部临时目录（用于 process_for_store）
    """

    def __init__(self, temp_dir: Optional[str] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.signals = DicomProcessorSignals()
        self.temp_dir = temp_dir or self._default_temp_dir()
        self._thread: Optional[QThread] = None
        self._worker: Optional[ProcessWorker] = None

    @staticmethod
    def _default_temp_dir() -> str:
        """获取默认的临时处理输出目录。"""
        base = os.path.join(os.getcwd(), "temp_dicom", "processed")
        os.makedirs(base, exist_ok=True)
        return base

    def _cleanup(self):
        """清理之前的工作线程。"""
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)

    def _start_worker(self, file_list: List[str], target_info: TargetPatientInfo, output_dir: str):
        """内部方法：启动处理工作线程。"""
        self._cleanup()

        self._thread = QThread(self)
        self._worker = ProcessWorker(file_list, target_info, output_dir)
        self._worker.moveToThread(self._thread)

        # 信号转发
        self._worker.progress.connect(self.signals.process_progress)
        self._worker.finished.connect(self.signals.process_finished)
        self._worker.error.connect(self.signals.error_occurred)

        # 生命周期
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)

        self._thread.start()

    # ---------- 公共接口 ----------

    def process_and_export(self, file_list: List[str], target_dict: Dict, output_dir: str):
        """
        处理文件并导出到指定本地目录。

        参数：
            file_list: 源 DICOM 文件路径列表
            target_dict: 目标患者信息字典（包含 patient_name, patient_id 等）
            output_dir: 用户指定的输出目录
        """
        target_info = TargetPatientInfo.from_dict(target_dict)
        logger.info(f"启动导出处理: 文件数={len(file_list)}, 目标={target_info}, 输出={output_dir}")
        self._start_worker(file_list, target_info, output_dir)

    def process_for_store(self, file_list: List[str], target_dict: Dict) -> str:
        """
        处理文件到内部临时目录，准备后续 C-STORE 发送。

        参数：
            file_list: 源 DICOM 文件路径列表
            target_dict: 目标患者信息字典

        返回：
            临时输出目录路径（处理完成后可通过扫描此目录获取文件列表）
        """
        target_info = TargetPatientInfo.from_dict(target_dict)
        # 使用时间戳创建独立的子目录，避免多次处理互相覆盖
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output_dir = os.path.join(self.temp_dir, timestamp)
        os.makedirs(output_dir, exist_ok=True)

        logger.info(f"启动发送前处理: 文件数={len(file_list)}, 目标={target_info}, 临时输出={output_dir}")
        self._start_worker(file_list, target_info, output_dir)
        return output_dir

    def get_processed_files(self, output_dir: str) -> List[str]:
        """
        扫描处理输出目录，返回所有生成的 DICOM 文件路径。
        通常用于 process_for_store 后获取待发送的文件列表。
        """
        files = []
        if not os.path.isdir(output_dir):
            return files
        for root, _, filenames in os.walk(output_dir):
            for fname in filenames:
                fpath = os.path.join(root, fname)
                if os.path.isfile(fpath):
                    files.append(fpath)
        return files


# ------------------------------------------------------------------------------
# 独立测试入口
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    """
    独立运行此文件可进行数据处理测试。
    需要一个包含合法 DICOM 文件的源目录。

    示例：
        python dicom_processor.py
    """
    import sys
    from PySide6.QtCore import QCoreApplication, QTimer

    app = QCoreApplication(sys.argv)

    # 构建测试用的目标患者信息
    target = TargetPatientInfo(
        patient_name="测试患者新",
        patient_id="NEW_PID_001",
        accession_number="NEW_ACC_001",
        study_instance_uid=""  # 留空，测试自动生成
    )

    processor = DicomProcessor()

    def on_finished(success_count, output_dir):
        print(f"\n[处理完成] 成功 {success_count} 个文件")
        print(f"[输出目录] {output_dir}")
        files = processor.get_processed_files(output_dir)
        print(f"[生成文件] {len(files)} 个:")
        for f in files:
            print(f"  - {f}")
        app.quit()

    def on_error(msg):
        print(f"[错误] {msg}")
        app.quit()

    processor.signals.process_finished.connect(on_finished)
    processor.signals.error_occurred.connect(on_error)

    # 请修改为你的测试源文件路径
    test_source_files = []
    test_source_dir = os.path.join(os.getcwd(), "temp_dicom")

    if os.path.isdir(test_source_dir):
        for root, _, files in os.walk(test_source_dir):
            for fname in files:
                if fname.endswith(".dcm"):
                    test_source_files.append(os.path.join(root, fname))

    if not test_source_files:
        print(f"未在 {test_source_dir} 找到 .dcm 测试文件，请放置一些 DICOM 文件后重试。")
        sys.exit(1)

    print(f"开始处理 {len(test_source_files)} 个测试文件...")
    output = processor.process_for_store(test_source_files, {
        "patient_name": target.patient_name,
        "patient_id": target.patient_id,
        "accession_number": target.accession_number,
        "study_instance_uid": target.study_instance_uid,
    })
    print(f"临时输出目录: {output}")

    # 30 秒超时
    QTimer.singleShot(30000, app.quit)
    sys.exit(app.exec())
