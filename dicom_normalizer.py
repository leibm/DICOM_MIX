# -*- coding: utf-8 -*-
"""
dicom_normalizer.py
DICOM 异构断层数据归一化模块 (V4.0)

本模块用于接收来自不同品牌 DSA/CT 设备导出的三维断层序列，
通过抹除私有协议壁垒和重构空间参数，将其转换为标准 CT Image Storage 格式，
确保能被第三方闭源工作站（特别是 GE Advantage Workstation）兼容并进行 3D/MPR 重建。

技术栈：pydicom, numpy, os, shutil
"""

import os
import shutil
import numpy as np
from pathlib import Path
from typing import List, Tuple, Optional, Dict

import pydicom
from pydicom import Dataset
from pydicom.uid import generate_uid, ExplicitVRLittleEndian, ImplicitVRLittleEndian


class DicomNormalizeError(Exception):
    """归一化过程中发生的自定义异常。"""
    pass


class DICOMNormalizer:
    """
    DICOM 异构断层数据归一化器。

    核心流程：
    1. 读取并排序切片（按 Z 轴坐标）
    2. 校验序列一致性（同一 Study + Series）
    3. 强制 IOD 降级为标准 CT Image Storage
    4. 生成全新 UID（保留 StudyInstanceUID）
    5. 厂商身份伪装（默认 GE）
    6. 重构空间几何参数（方向、层厚、层间距）
    7. 清洗私有标签
    8. 标准化灰度映射参数

    Parameters
    ----------
    target_manufacturer : str
        目标兼容厂商标识，当前仅支持 "GE"（默认）。
    verbose : bool
        是否在控制台打印处理进度，默认 True。
    """
    BUILD_ID = "V4.4-20260515-1"  # 每次更新代码时递增

    # 标准 CT Image Storage SOP Class UID
    CT_IMAGE_STORAGE_UID = "1.2.840.10008.5.1.4.1.1.2"

    # 厂商伪装配置表
    MANUFACTURER_PROFILES = {
        "GE": {
            "Manufacturer": "GE MEDICAL SYSTEMS",
            "ManufacturerModelName": "LightSpeed VCT",
            "SoftwareVersions": "Generic_Normalization_Tool_v1",
        },
        "SIEMENS": {
            "Manufacturer": "SIEMENS",
            "ManufacturerModelName": "SOMATOM Definition",
            "SoftwareVersions": "Generic_Normalization_Tool_v1",
        },
        "PHILIPS": {
            "Manufacturer": "Philips",
            "ManufacturerModelName": "Brilliance 64",
            "SoftwareVersions": "Generic_Normalization_Tool_v1",
        },
    }

    # 标准横断面方向余弦（Axial）
    STANDARD_AXIAL_ORIENTATION = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]

    def __init__(self, target_manufacturer: str = "GE", verbose: bool = True,
                 progress_callback=None):
        self.target = target_manufacturer.upper()
        self.verbose = verbose
        self._progress_callback = progress_callback
        if self.target not in self.MANUFACTURER_PROFILES:
            raise ValueError(
                f"不支持的厂商目标: {target_manufacturer}. "
                f"当前支持: {list(self.MANUFACTURER_PROFILES.keys())}"
            )

    def _report_progress(self, message: str, pct: int):
        """向 UI 报告进度（百分比 0-100）。"""
        if self._progress_callback:
            self._progress_callback(message, pct)

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_varying_axis(positions: List[list]) -> int:
        """
        检测一组 ImagePositionPatient 中变化最大的轴。
        返回轴索引: 0=X, 1=Y, 2=Z。
        适用于 CBCT 旋转采集（变化轴可能不是 Z）。
        """
        if len(positions) < 2:
            return 2  # 默认 Z
        ranges = []
        for axis in range(3):
            vals = [float(p[axis]) for p in positions]
            ranges.append(max(vals) - min(vals))
        return ranges.index(max(ranges))

    @staticmethod
    def _get_sort_position(ds: Dataset, axis: int = 2) -> float:
        """
        从 ImagePositionPatient 提取指定轴的坐标用于排序。
        """
        if "ImagePositionPatient" not in ds:
            raise DicomNormalizeError("缺少必需标签 (0020,0032) ImagePositionPatient")
        return float(ds.ImagePositionPatient[axis])

    @staticmethod
    def _read_raw_pixels_from_file(file_path: str, ds: Dataset, expected_bytes: int) -> Optional[bytes]:
        """
        当 pydicom 返回截断 PixelData 时，直接从原始文件读取像素字节。
        多策略：file_tell → 标签搜索(LE/BE) → 文件末尾回退。
        """
        try:
            with open(file_path, 'rb') as f:
                data = f.read()
        except Exception:
            return None

        file_size = len(data)
        if file_size < expected_bytes:
            return None

        # 策略 1：通过 DataElement.file_tell 定位
        try:
            pixel_elem = ds['PixelData']
            value_offset = getattr(pixel_elem, 'file_tell', None)
            if value_offset is not None and value_offset + expected_bytes <= file_size:
                return data[value_offset:value_offset + expected_bytes]
        except Exception:
            pass

        # 策略 2：搜索 PixelData 标签 (7FE0,0010)，小端优先，大端备选
        for tag_bytes in (b'\xe0\x7f\x10\x00', b'\x7f\xe0\x00\x10'):
            # rfind 找最后一个出现（PixelData 通常在文件末尾附近）
            pos = data.rfind(tag_bytes, 132)
            if pos == -1:
                continue

            after_tag = pos + 4
            vr = data[after_tag:after_tag + 2]
            if vr in (b'OB', b'OW', b'UN'):
                # Explicit VR: tag(4) + VR(2) + reserved(2) + length(4) = 12
                length = int.from_bytes(data[after_tag + 4:after_tag + 8], 'little')
                value_start = after_tag + 8
            else:
                # Implicit VR: tag(4) + length(4) = 8
                length = int.from_bytes(data[after_tag:after_tag + 4], 'little')
                value_start = after_tag + 4

            if length == 0xFFFFFFFF:
                # 未定义长度，读到文件末尾
                length = file_size - value_start

            if length >= expected_bytes and value_start + expected_bytes <= file_size:
                return data[value_start:value_start + expected_bytes]

        # 策略 3：文件末尾回退（PixelData 通常是文件最后一个元素）
        return data[file_size - expected_bytes:]

    @staticmethod
    def _safe_remove_private_tags(ds: Dataset) -> int:
        """
        遍历 Dataset 并删除所有私有标签（Group Number 为奇数）。

        pydicom 中私有标签的组号为奇数（如 0x0019, 0x0021, 0x0043 等）。
        这些标签通常包含厂商特定的原始数据、重建参数或内部状态，
        在跨厂商传输时极易导致目标工作站解析崩溃。

        Returns
        -------
        int : 被删除的私有标签数量。
        """
        removed_count = 0
        # 必须将 keys 先转为列表，因为遍历过程中会修改 dict
        for tag in list(ds.keys()):
            if tag.group % 2 == 1:  # 奇数组号 = 私有标签
                # 某些私有标签可能是嵌套序列（SQ），递归处理
                elem = ds[tag]
                if elem.VR == "SQ" and hasattr(elem, "value"):
                    # 序列内部的 Dataset 也要清洗
                    for item in elem.value:
                        removed_count += DICOMNormalizer._safe_remove_private_tags(item)
                del ds[tag]
                removed_count += 1
        return removed_count

    @staticmethod
    def _compute_slice_spacing(sorted_datasets: List[Dataset], axis: int = 2) -> float:
        """
        基于已排序的切片列表计算相邻层之间的真实间距。

        算法：
        1. 提取每层在变化轴上的坐标。
        2. 计算所有相邻层的差值。
        3. 取中位数作为最终间距（中位数对异常值更鲁棒）。

        Parameters
        ----------
        axis : int
            变化轴索引: 0=X, 1=Y, 2=Z（默认）。

        Returns
        -------
        float : 计算出的层间距，保留两位小数。
        """
        if len(sorted_datasets) < 2:
            return 1.0

        positions = [float(ds.ImagePositionPatient[axis]) for ds in sorted_datasets]
        deltas = [abs(positions[i] - positions[i - 1]) for i in range(1, len(positions))]
        spacing = float(np.median(deltas))
        return round(spacing, 2)

    # ------------------------------------------------------------------
    # 核心处理逻辑
    # ------------------------------------------------------------------

    @staticmethod
    def _expand_multiframe(ds: Dataset, file_path: str) -> List[Tuple[Dataset, str]]:
        """
        将多帧 DICOM 文件展开为多个单帧 Dataset 元组。

        对于 CBCT/断层重建的多帧数据（单个 .dcm 包含 N 张切片），
        需要将其展开为 N 个独立的单帧 Dataset，每个携带正确的 Z 轴坐标。

        Returns
        -------
        List[Tuple[Dataset, str]] : 展开后的 (dataset, filepath) 列表。
                                    单帧文件直接返回原样。
        """
        n_frames = int(getattr(ds, "NumberOfFrames", 1))
        if n_frames <= 1:
            return [(ds, file_path)]

        # 读取完整数据集（供校验及后续流水线复用，避免重复读取导致截断）
        full_ds = None
        expand_diag = {"path": file_path, "read_error": None, "pixel_len": 0, "pa_shape": None, "actual_frames": n_frames}
        try:
            # 尝试用显式文件对象读取，某些环境下比路径字符串更可靠
            with open(file_path, 'rb') as f:
                full_ds = pydicom.dcmread(f, force=True)
        except Exception as e:
            expand_diag["read_error"] = str(e)
            print(f"[!] 警告: 读取 {os.path.basename(file_path)} 完整数据集失败 — {e}")

        # 校验实际 PixelData 能支持多少帧（防止 NumberOfFrames 虚标）
        actual_frames = n_frames
        if full_ds is not None and "PixelData" in full_ds:
            try:
                pixel_data_len = len(full_ds.PixelData)
                expand_diag["pixel_len"] = pixel_data_len
                rows = int(getattr(full_ds, 'Rows', 0))
                cols = int(getattr(full_ds, 'Columns', 0))
                bits = int(getattr(full_ds, 'BitsAllocated', 16))
                samples = int(getattr(full_ds, 'SamplesPerPixel', 1))
                bytes_per_sample = bits // 8
                row_bytes = cols * samples * bytes_per_sample
                if row_bytes % 2 == 1:
                    row_bytes += 1
                bytes_per_frame = row_bytes * rows
                if bytes_per_frame > 0:
                    actual_frames_from_size = pixel_data_len // bytes_per_frame
                    # 也尝试用 pixel_array 形状确认
                    try:
                        pa = full_ds.pixel_array
                        expand_diag["pa_shape"] = str(pa.shape)
                        if pa.ndim == 2:
                            actual_frames_from_array = 1
                        elif pa.ndim == 3:
                            actual_frames_from_array = pa.shape[0]
                        elif pa.ndim == 4:
                            actual_frames_from_array = pa.shape[0]
                        else:
                            actual_frames_from_array = n_frames
                    except Exception as pa_e:
                        expand_diag["pa_shape"] = f"ERROR:{pa_e}"
                        actual_frames_from_array = n_frames

                    actual_frames = min(n_frames, actual_frames_from_size, actual_frames_from_array)
                    expand_diag["actual_frames"] = actual_frames
                    if actual_frames < n_frames:
                        print(
                            f"[!] 警告: {os.path.basename(file_path)} 声称 {n_frames} 帧，"
                            f"但实际仅 {actual_frames} 帧有效 (PixelData {pixel_data_len} 字节，"
                            f"pixel_array {getattr(full_ds, 'pixel_array', 'N/A')})"
                        )
            except Exception as e:
                expand_diag["read_error"] = str(e)
                print(f"[!] 警告: 校验 {os.path.basename(file_path)} 实际帧数失败 — {e}")

        n_frames = actual_frames
        if n_frames <= 1:
            if full_ds is not None:
                return [(full_ds, file_path)]
            return [(ds, file_path)]

        # 基础 Z 坐标与层间距
        base_ipp = list(ds.ImagePositionPatient) if "ImagePositionPatient" in ds else [0.0, 0.0, 0.0]
        spacing = 1.0
        if "SpacingBetweenSlices" in ds:
            spacing = float(ds.SpacingBetweenSlices)
        elif "SliceThickness" in ds:
            spacing = float(ds.SliceThickness)

        # 尝试从 PerFrameFunctionalGroupsSequence 读取每帧位置
        per_frame_positions = []
        pffgs = getattr(ds, "PerFrameFunctionalGroupsSequence", None)
        if pffgs:
            for item in pffgs:
                if "PlanePositionSequence" in item:
                    pps = item.PlanePositionSequence[0]
                    if "ImagePositionPatient" in pps:
                        per_frame_positions.append(list(pps.ImagePositionPatient))

        # 从 SharedFunctionalGroupsSequence 提取共享参数（在 strip 前）
        shared_orientation = None
        shared_pixel_spacing = None
        sfgs = getattr(ds, "SharedFunctionalGroupsSequence", None)
        if sfgs and len(sfgs) > 0:
            sfg_item = sfgs[0]
            if "PlaneOrientationSequence" in sfg_item:
                po = sfg_item.PlaneOrientationSequence[0]
                if "ImageOrientationPatient" in po:
                    shared_orientation = list(po.ImageOrientationPatient)
            if "PixelMeasuresSequence" in sfg_item:
                pms = sfg_item.PixelMeasuresSequence[0]
                if "PixelSpacing" in pms:
                    shared_pixel_spacing = list(pms.PixelSpacing)

        # 若无显式方向，根据实际帧位置推导方向余弦
        # （CBCT 旋转采集时变化轴可能不是 Z，需要正确的方向以匹配法线方向）
        if shared_orientation is None and len(per_frame_positions) >= 2:
            p0 = np.array(per_frame_positions[0], dtype=float)
            p1 = np.array(per_frame_positions[-1], dtype=float)
            diff = p1 - p0
            # 找到变化最大的轴作为切片法线方向
            slice_normal_axis = int(np.argmax(np.abs(diff)))
            # 法线方向单位向量
            slice_normal = np.zeros(3)
            slice_normal[slice_normal_axis] = 1.0
            # 行方向：选择与法线垂直的轴
            if slice_normal_axis == 0:
                row_dir = np.array([0.0, 1.0, 0.0])
            else:
                row_dir = np.array([1.0, 0.0, 0.0])
            # 列方向 = 法线 × 行方向（确保法线 = 行 × 列）
            col_dir = np.cross(slice_normal, row_dir)
            shared_orientation = list(row_dir) + list(col_dir)

        results = []
        for i in range(n_frames):
            frame_ds = ds.copy()
            # pydicom Dataset.copy() 不复制实例属性，必须手动复制
            frame_ds.is_little_endian = getattr(ds, "is_little_endian", True)
            frame_ds.is_implicit_VR = getattr(ds, "is_implicit_VR", False)
            if hasattr(ds, "file_meta") and ds.file_meta is not None:
                frame_ds.file_meta = ds.file_meta.copy()

            frame_ds.NumberOfFrames = 1

            # 计算该帧 Z 坐标
            if i < len(per_frame_positions):
                frame_ds.ImagePositionPatient = per_frame_positions[i]
            else:
                frame_ds.ImagePositionPatient = [
                    base_ipp[0], base_ipp[1], base_ipp[2] + i * spacing
                ]

            # 从 SharedFunctionalGroupsSequence 提取的参数写入顶层
            if shared_orientation is not None:
                frame_ds.ImageOrientationPatient = shared_orientation
            if shared_pixel_spacing is not None and "PixelSpacing" not in frame_ds:
                frame_ds.PixelSpacing = shared_pixel_spacing

            # 标记来源，便于后续像素提取
            frame_ds._multiframe_source_path = file_path
            frame_ds._multiframe_frame_index = i
            frame_ds._multiframe_total_frames = n_frames
            frame_ds._multiframe_expand_diag = expand_diag
            # 缓存完整数据集，避免 _run_pipeline 重复读取导致截断
            if full_ds is not None:
                frame_ds._multiframe_full_ds = full_ds

            # 移除仅适用于多帧的标签，避免单帧输出时造成解析问题
            for tag_name in (
                "PerFrameFunctionalGroupsSequence",
                "SharedFunctionalGroupsSequence",
                "FrameContentSequence",
                "FrameAcquisitionSequence",
                "DimensionOrganizationSequence",
                "DimensionIndexSequence",
            ):
                if tag_name in frame_ds:
                    delattr(frame_ds, tag_name)

            results.append((frame_ds, file_path))

        return results

    def _sort_and_validate_files(self, file_paths: List[str]) -> Tuple[List[Dataset], List[str]]:
        """
        步骤 1：从文件列表读取并排序校验。

        - 快速读取头信息（stop_before_pixels=True 加速）。
        - 支持单文件多帧 DICOM 的自动展开。
        - 按 Z 轴坐标升序排列。
        - 校验是否属于同一 Study / Series，剔除异常文件。

        Returns
        -------
        Tuple[List[Dataset], List[str]] : 排序后的 Dataset 列表和对应的原始文件路径列表。
        """
        if not file_paths:
            raise DicomNormalizeError("文件列表为空")

        if self.verbose:
            print(f"[步骤 1] 发现 {len(file_paths)} 个候选文件，开始读取与校验...")

        loaded = []
        for fp in file_paths:
            try:
                # 首次读取仅用于排序和校验，跳过像素数据以提升速度
                ds = pydicom.dcmread(fp, stop_before_pixels=True, force=True)

                # 多帧文件：允许没有顶层 ImagePositionPatient（位置在 PerFrameFunctionalGroupsSequence 中）
                n_frames = int(getattr(ds, "NumberOfFrames", 1))
                is_multiframe = n_frames > 1
                has_perframe_position = False
                if is_multiframe:
                    pffgs = getattr(ds, "PerFrameFunctionalGroupsSequence", None)
                    if pffgs and len(pffgs) > 0:
                        first_item = pffgs[0]
                        if "PlanePositionSequence" in first_item:
                            has_perframe_position = True

                # 校验必需标签
                if "ImagePositionPatient" not in ds and not has_perframe_position:
                    if self.verbose:
                        print(f"  跳过（缺少 ImagePositionPatient）: {os.path.basename(fp)}")
                    continue
                if "SeriesInstanceUID" not in ds:
                    if self.verbose:
                        print(f"  跳过（缺少 SeriesInstanceUID）: {os.path.basename(fp)}")
                    continue

                # 展开多帧文件
                expanded = self._expand_multiframe(ds, fp)
                loaded.extend(expanded)
            except Exception as e:
                if self.verbose:
                    print(f"  跳过（读取失败）: {os.path.basename(fp)} — {e}")
                continue

        if len(loaded) < 2:
            raise DicomNormalizeError(
                f"有效 DICOM 文件不足（仅 {len(loaded)} 个），无法构成断层序列。"
            )

        # 校验 Series 一致性：取出现次数最多的 SeriesInstanceUID 作为基准
        series_uids = [ds.SeriesInstanceUID for ds, _ in loaded]
        unique_series = {}
        for uid in series_uids:
            unique_series[uid] = unique_series.get(uid, 0) + 1
        dominant_series_uid = max(unique_series, key=unique_series.get)

        # 只保留属于主导 Series 的文件
        filtered = [
            (ds, fp) for ds, fp in loaded
            if ds.SeriesInstanceUID == dominant_series_uid
        ]

        if len(filtered) < len(loaded) and self.verbose:
            print(
                f"  剔除 {len(loaded) - len(filtered)} 个异 Series 文件，"
                f"保留 {len(filtered)} 个（Series UID: {dominant_series_uid[-12:]})"
            )

        # 检测实际变化轴（CBCT 旋转采集时变化轴可能不是 Z）
        all_positions = [list(ds.ImagePositionPatient) for ds, _ in filtered]
        self._sort_axis = self._detect_varying_axis(all_positions)
        axis_names = {0: "X", 1: "Y", 2: "Z"}

        # 按检测到的变化轴排序
        filtered.sort(key=lambda item: self._get_sort_position(item[0], self._sort_axis))

        datasets = [ds for ds, _ in filtered]
        file_paths_sorted = [fp for _, fp in filtered]

        if self.verbose:
            pos_first = self._get_sort_position(datasets[0], self._sort_axis)
            pos_last = self._get_sort_position(datasets[-1], self._sort_axis)
            print(
                f"[步骤 1] 完成。共 {len(datasets)} 张切片，"
                f"变化轴: {axis_names[self._sort_axis]}，"
                f"范围: {pos_first:.2f} ~ {pos_last:.2f} mm"
            )

        return datasets, file_paths_sorted

    def _sort_and_validate(self, input_dir: str) -> Tuple[List[Dataset], List[str]]:
        """
        步骤 1（目录版）：文件排序与校验。

        - 扫描目录下所有 .dcm 文件。
        - 委托 _sort_and_validate_files 完成实际的读取、排序与校验。

        Returns
        -------
        Tuple[List[Dataset], List[str]] : 排序后的 Dataset 列表和对应的原始文件路径列表。
        """
        input_path = Path(input_dir)
        if not input_path.is_dir():
            raise DicomNormalizeError(f"输入路径不存在或不是目录: {input_dir}")

        dcm_files = sorted(input_path.glob("*.dcm"))
        if not dcm_files:
            # 某些设备导出的 DICOM 没有 .dcm 扩展名，尝试无扩展名文件
            dcm_files = [f for f in input_path.iterdir() if f.is_file()]

        if not dcm_files:
            raise DicomNormalizeError(f"目录中未找到任何 DICOM 文件: {input_dir}")

        return self._sort_and_validate_files([str(f) for f in dcm_files])

    def _reset_uids_and_downgrade(self, datasets: List[Dataset]) -> Tuple[str, str]:
        """
        步骤 2：强制 IOD 降级与 UID 重置。

        - 保留原始 StudyInstanceUID。
        - 生成全新的 SeriesInstanceUID（整个序列共享）。
        - 为每张切片生成全新的 SOPInstanceUID。
        - 将 SOPClassUID 强制覆写为标准 CT Image Storage。

        Returns
        -------
        Tuple[str, str] : (study_uid, new_series_uid)
        """
        if self.verbose:
            print("[步骤 2] 开始 UID 重置与 IOD 降级...")

        # 取第一张切片的 StudyInstanceUID 作为基准（全部应一致）
        study_uid = datasets[0].StudyInstanceUID
        new_series_uid = generate_uid()
        # 3D Slicer 强制要求同一容积的所有切片共享 FrameOfReferenceUID
        new_frame_of_reference_uid = generate_uid()

        for idx, ds in enumerate(datasets):
            # 强制降级为 CT Image Storage
            ds.SOPClassUID = self.CT_IMAGE_STORAGE_UID
            # Modality 必须与 SOP Class 一致，否则 3D Slicer 等严格软件拒绝导入
            ds.Modality = "CT"

            # 为每张切片生成新的 SOP Instance UID
            ds.SOPInstanceUID = generate_uid()

            # 覆写 SeriesInstanceUID
            ds.SeriesInstanceUID = new_series_uid

            # 保留 StudyInstanceUID（必须保持不变以维持 Study 层级关系）
            ds.StudyInstanceUID = study_uid

            # 3D Slicer 强制校验：同一容积切片必须共享 FrameOfReferenceUID
            ds.FrameOfReferenceUID = new_frame_of_reference_uid
            # RadiAnt 依赖 InstanceNumber 排序切片
            ds.InstanceNumber = str(idx + 1)

            # 同步更新 Media Storage SOP Class / Instance UID（文件元信息层级）
            if hasattr(ds, "file_meta") and ds.file_meta is not None:
                ds.file_meta.MediaStorageSOPClassUID = self.CT_IMAGE_STORAGE_UID
                ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID

        if self.verbose:
            print(f"[步骤 2] 完成。新 Series UID: ...{new_series_uid[-12:]}")

        return study_uid, new_series_uid

    def _spoof_manufacturer(self, datasets: List[Dataset]) -> None:
        """
        步骤 3：厂商身份"伪装"。

        覆写 Manufacturer、ManufacturerModelName、SoftwareVersions 等标签，
        使目标工作站（特别是 GE）将其识别为原生兼容数据。
        """
        if self.verbose:
            print(f"[步骤 3] 开始厂商伪装（目标: {self.target}）...")

        profile = self.MANUFACTURER_PROFILES[self.target]

        for ds in datasets:
            # Manufacturer (0008,0070)
            ds.Manufacturer = profile["Manufacturer"]

            # Manufacturer's Model Name (0008,1090)
            ds.ManufacturerModelName = profile["ManufacturerModelName"]

            # Software Versions (0018,1020)
            # 注意：某些原数据可能不存在该标签，需要显式添加
            if "SoftwareVersions" in ds:
                ds.SoftwareVersions = profile["SoftwareVersions"]
            else:
                ds.add_new(
                    pydicom.tag.Tag(0x0018, 0x1020),
                    "LO",
                    profile["SoftwareVersions"],
                )

        if self.verbose:
            print(f"[步骤 3] 完成。已伪装为 {profile['Manufacturer']} {profile['ManufacturerModelName']}")

    def _reconstruct_spatial_geometry(self, datasets: List[Dataset]) -> None:
        """
        步骤 4：空间几何参数重构（极其重要）。

        各大工作站 3D 报错或图像变形往往源于此处的参数不一致。
        本方法强制标准化方向余弦并重新计算层间距。
        """
        if self.verbose:
            print("[步骤 4] 开始空间几何参数重构...")

        # 方向余弦：保留原始方向（从 SharedFunctionalGroupsSequence 提取），
        # 仅在缺失时补充标准横断面方向
        for ds in datasets:
            if "ImageOrientationPatient" not in ds:
                ds.add_new(
                    pydicom.tag.Tag(0x0020, 0x0037),
                    "DS",
                    self.STANDARD_AXIAL_ORIENTATION,
                )

        # 使用检测到的变化轴计算层间距
        sort_axis = getattr(self, '_sort_axis', 2)
        slice_spacing = self._compute_slice_spacing(datasets, axis=sort_axis)

        # 将计算出的间距同时写入 Slice Thickness 和 Spacing Between Slices
        # 某些工作站（如 GE AW）会同时校验这两个标签
        for ds in datasets:
            # Slice Thickness (0018,0050)
            if "SliceThickness" in ds:
                ds.SliceThickness = f"{slice_spacing:.2f}"
            else:
                ds.add_new(
                    pydicom.tag.Tag(0x0018, 0x0050),
                    "DS",
                    f"{slice_spacing:.2f}",
                )

            # Spacing Between Slices (0018,0088)
            if "SpacingBetweenSlices" in ds:
                ds.SpacingBetweenSlices = f"{slice_spacing:.2f}"
            else:
                ds.add_new(
                    pydicom.tag.Tag(0x0018, 0x0088),
                    "DS",
                    f"{slice_spacing:.2f}",
                )

            # 同时修正 PixelSpacing（如果存在）中的 Z 维度相关逻辑
            # PixelSpacing (0028,0030) 是 [row_spacing, column_spacing]
            # 若不存在，尝试从原有数据保留或注入默认值
            if "PixelSpacing" not in ds and "ImagerPixelSpacing" in ds:
                # 某些 XA/DSA 数据使用 ImagerPixelSpacing，需要迁移
                ds.PixelSpacing = ds.ImagerPixelSpacing
            elif "PixelSpacing" not in ds:
                # 两者都不存在时，注入默认像素尺寸以确保 3D 引擎能构建等距体素
                ds.add_new(pydicom.tag.Tag(0x0028, 0x0030), "DS", ["1.0", "1.0"])

        if self.verbose:
            print(f"[步骤 4] 完成。层间距 = {slice_spacing:.2f} mm，方向 = Axial")

    def _scrub_private_tags(self, datasets: List[Dataset]) -> int:
        """
        步骤 5：私有标签"大清洗"。

        遍历所有 Dataset，彻底删除所有非标准组号（奇数 Group Number）的标签，
        消除导致目标工作站解析崩溃的最大隐患。
        """
        if self.verbose:
            print("[步骤 5] 开始私有标签清洗...")

        total_removed = 0
        for idx, ds in enumerate(datasets):
            removed = self._safe_remove_private_tags(ds)
            total_removed += removed
            if self.verbose and removed > 0:
                print(f"  切片 {idx + 1}: 移除 {removed} 个私有标签")

        if self.verbose:
            print(f"[步骤 5] 完成。共移除 {total_removed} 个私有标签。")

        return total_removed

    def _normalize_rescale(self, datasets: List[Dataset]) -> None:
        """
        步骤 6：灰度映射标准化。

        检查 Rescale Intercept / Slope，若不存在则注入默认值，
        以满足工作站对 HU 值校验的强制要求（特别是对于 DSA 重建的类 CT 数据）。
        """
        if self.verbose:
            print("[步骤 6] 开始灰度映射标准化...")

        for ds in datasets:
            # Rescale Intercept (0028,1052)
            # 仅在原始数据已有 RescaleIntercept 时保留，否则设为 0
            # （对 8-bit XA 数据注入 -1024 无意义且会导致值域错误）
            if "RescaleIntercept" not in ds:
                ds.add_new(
                    pydicom.tag.Tag(0x0028, 0x1052),
                    "DS",
                    "0",
                )

            # Rescale Slope (0028,1053)
            if "RescaleSlope" not in ds:
                ds.add_new(
                    pydicom.tag.Tag(0x0028, 0x1053),
                    "DS",
                    "1",
                )

            # 同步更新 Rescale Type
            if "RescaleType" not in ds:
                ds.add_new(
                    pydicom.tag.Tag(0x0028, 0x1054),
                    "LO",
                    "HU",
                )
            else:
                ds.RescaleType = "HU"

        if self.verbose:
            print("[步骤 6] 完成。Rescale Intercept = -1024, Slope = 1, Type = HU")

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def _run_pipeline(self, datasets: List[Dataset], file_paths: List[str],
                        output_dir: str, source_label: str = "") -> dict:
        """
        执行归一化核心流水线（步骤 2~6 + 保存）。

        Parameters
        ----------
        datasets : List[Dataset]
            已排序校验的 Dataset 列表（头信息，不含像素）。
        file_paths : List[str]
            对应的原始文件路径列表。
        output_dir : str
            输出目录。
        source_label : str
            用于日志/摘要的输入源标识（如目录路径）。

        Returns
        -------
        dict : 处理摘要。
        """
        output_dir = os.path.abspath(output_dir)
        total_steps = 6  # 步骤 2-6 + 保存

        # 步骤 2：UID 重置与 IOD 降级
        self._report_progress("步骤 2/6: UID 重置与 IOD 降级...", 10)
        study_uid, new_series_uid = self._reset_uids_and_downgrade(datasets)

        # 步骤 3：厂商伪装
        self._report_progress("步骤 3/6: 厂商身份伪装...", 25)
        self._spoof_manufacturer(datasets)

        # 步骤 4：空间几何重构
        self._report_progress("步骤 4/6: 空间几何参数重构...", 40)
        self._reconstruct_spatial_geometry(datasets)

        # 步骤 5：私有标签清洗
        self._report_progress("步骤 5/6: 私有标签清洗...", 55)
        self._scrub_private_tags(datasets)

        # 步骤 6：灰度映射标准化
        self._report_progress("步骤 6/6: 灰度映射标准化...", 70)
        self._normalize_rescale(datasets)

        # ------------------------------------------------------------------
        # 重建输出目录并写入文件
        # ------------------------------------------------------------------
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)
        os.makedirs(output_dir, exist_ok=True)

        # 统一使用 Explicit VR Little Endian 写入（最广泛的兼容性）
        target_ts = ExplicitVRLittleEndian

        # 缓存多帧源文件的完整 Dataset，避免重复读取
        multiframe_cache: Dict[str, Dataset] = {}
        # 缓存从文件直接读取的原始像素字节（避免每次帧提取都重读 115MB 文件）
        raw_pixel_cache: Dict[str, bytes] = {}

        saved_count = 0
        failed_frames: List[Tuple[int, str, str]] = []

        for idx, (ds, src_path) in enumerate(zip(datasets, file_paths)):
            frame_idx = getattr(ds, "_multiframe_frame_index", None)
            orig_path = getattr(ds, "_multiframe_source_path", src_path)

            # 读取完整原始文件（优先使用 _expand_multiframe 缓存的 full_ds，避免重复读取截断）
            full_ds = getattr(ds, "_multiframe_full_ds", None)
            if full_ds is None:
                try:
                    full_ds = multiframe_cache.get(orig_path)
                    if full_ds is None:
                        full_ds = pydicom.dcmread(orig_path, force=True)
                        multiframe_cache[orig_path] = full_ds
                except Exception as e:
                    err_msg = f"重新读取原始文件失败: {e}"
                    failed_frames.append((idx, os.path.basename(orig_path), err_msg))
                    if self.verbose:
                        print(f"  警告：{err_msg}")
                    continue

            if frame_idx is not None:
                # ---------- 多帧展开切片 ----------
                # 诊断：仅对每文件的首帧输出像素数据读取详情
                if frame_idx == 0 and self.verbose:
                    px_len = len(full_ds.PixelData) if 'PixelData' in full_ds else 0
                    try:
                        pa_shape = full_ds.pixel_array.shape
                    except Exception as pa_e:
                        pa_shape = f"ERROR: {pa_e}"
                    ts = getattr(getattr(full_ds, 'file_meta', None), 'TransferSyntaxUID', 'N/A')
                    print(f"  [诊断] 多帧源 {os.path.basename(orig_path)}: "
                          f"PixelData={px_len} bytes, pixel_array={pa_shape}, TS={ts}")

                # 复制完整原始 Dataset，避免修改缓存中的 multi-frame 源
                output_ds = full_ds.copy()
                output_ds.is_little_endian = getattr(full_ds, "is_little_endian", True)
                output_ds.is_implicit_VR = getattr(full_ds, "is_implicit_VR", False)
                if hasattr(full_ds, "file_meta") and full_ds.file_meta is not None:
                    output_ds.file_meta = full_ds.file_meta.copy()

                # 恢复 _expand_multiframe 设置的每帧空间坐标
                # （full_ds.copy() 会覆盖为原始多帧文件的顶层值，对多帧文件通常不正确）
                if hasattr(ds, 'ImagePositionPatient'):
                    output_ds.ImagePositionPatient = ds.ImagePositionPatient
                if hasattr(ds, 'ImageOrientationPatient'):
                    output_ds.ImageOrientationPatient = ds.ImageOrientationPatient
                # 单帧输出
                output_ds.NumberOfFrames = 1

                # 提取指定帧的像素数据
                pixel_extracted = False
                extraction_log: List[str] = []
                try:
                    ts = getattr(getattr(full_ds, 'file_meta', None), 'TransferSyntaxUID', None)
                    is_uncompressed = (
                        ts in ('1.2.840.10008.1.2', '1.2.840.10008.1.2.1', '1.2.840.10008.1.2.2')
                        and isinstance(full_ds.PixelData, bytes)
                    )

                    if is_uncompressed:
                        # 未压缩数据：直接按偏移量提取原始字节（避免 pixel_array 多帧解析异常）
                        bits = int(getattr(full_ds, 'BitsAllocated', 16))
                        samples = int(getattr(full_ds, 'SamplesPerPixel', 1))
                        rows = int(getattr(full_ds, 'Rows', 0))
                        columns = int(getattr(full_ds, 'Columns', 0))
                        # 优先使用展开阶段记录的帧数（pydicom 重读可能返回错误值）
                        total_frames = int(getattr(ds, '_multiframe_total_frames',
                                                   getattr(full_ds, 'NumberOfFrames', 1)))

                        if rows == 0 or columns == 0:
                            extraction_log.append(f"uncompressed: Rows={rows}, Columns={columns} 无效")
                        else:
                            bytes_per_sample = bits // 8
                            row_bytes = columns * samples * bytes_per_sample
                            if row_bytes % 2 == 1:
                                row_bytes += 1
                            bytes_per_frame = row_bytes * rows
                            expected_total = bytes_per_frame * total_frames

                            # 优先使用原始文件缓存（绕过 pydicom 截断的 PixelData）
                            cached_raw = raw_pixel_cache.get(orig_path)
                            if cached_raw is not None and len(cached_raw) >= expected_total:
                                pixel_data = cached_raw
                            else:
                                pixel_data = full_ds.PixelData
                                actual_len = len(pixel_data)
                                # 若 pydicom 返回的数据不足期望值，从文件直接读取
                                if actual_len < expected_total:
                                    raw_pixels = self._read_raw_pixels_from_file(
                                        orig_path, full_ds, expected_total
                                    )
                                    if raw_pixels is not None and len(raw_pixels) >= expected_total:
                                        raw_pixel_cache[orig_path] = raw_pixels
                                        pixel_data = raw_pixels
                                    else:
                                        extraction_log.append(
                                            f"uncompressed: 截断 {actual_len}B != 期望 {expected_total}B，"
                                            f"raw_fallback={'成功' if raw_pixels else '失败'}"
                                        )
                                        bytes_per_frame = 0

                            if bytes_per_frame > 0:
                                start = frame_idx * bytes_per_frame
                                end = start + bytes_per_frame
                                if end <= len(pixel_data):
                                    output_ds.PixelData = pixel_data[start:end]
                                    pixel_extracted = True
                                else:
                                    extraction_log.append(f"uncompressed: 帧 {frame_idx} 越界 ({end} > {len(pixel_data)})")

                    if not pixel_extracted:
                        # 压缩数据 或 未压缩回退：使用 pixel_array 自动解压
                        try:
                            pixel_arr = full_ds.pixel_array
                        except Exception as pa_err:
                            extraction_log.append(f"pixel_array: 解码失败 — {pa_err}")
                            raise

                        # 检查 pixel_array 返回的帧数
                        if pixel_arr.ndim == 2:
                            # 单帧结果（可能多帧解码异常）
                            extraction_log.append(f"pixel_array: 返回2维数组 {pixel_arr.shape}，仅1帧可用")
                            if frame_idx == 0:
                                frame_arr = pixel_arr
                            else:
                                raise IndexError(f"帧 {frame_idx} 不可用（pixel_array 仅返回1帧）")
                        elif pixel_arr.ndim == 3:
                            if pixel_arr.shape[0] <= frame_idx:
                                raise IndexError(
                                    f"帧索引越界: {frame_idx} >= {pixel_arr.shape[0]} "
                                    f"(pixel_array 形状: {pixel_arr.shape})"
                                )
                            frame_arr = pixel_arr[frame_idx]
                        elif pixel_arr.ndim == 4:
                            if pixel_arr.shape[0] <= frame_idx:
                                raise IndexError(
                                    f"帧索引越界: {frame_idx} >= {pixel_arr.shape[0]} "
                                    f"(pixel_array 形状: {pixel_arr.shape})"
                                )
                            frame_arr = pixel_arr[frame_idx]
                        else:
                            raise ValueError(f"pixel_array 维度异常: {pixel_arr.ndim}, 形状: {pixel_arr.shape}")

                        # 保持原始 dtype
                        if frame_arr.dtype == np.uint8:
                            target_dtype = np.uint8
                        elif frame_arr.dtype == np.int16:
                            target_dtype = np.int16
                        else:
                            target_dtype = np.uint16

                        if frame_arr.dtype != target_dtype:
                            frame_arr = frame_arr.astype(target_dtype)

                        if target_dtype in (np.uint16, np.int16) and frame_arr.dtype.byteorder == '>':
                            frame_arr = frame_arr.byteswap().newbyteorder()

                        output_ds.PixelData = frame_arr.tobytes()
                        output_ds.Rows = frame_arr.shape[0]
                        output_ds.Columns = frame_arr.shape[1]
                        pixel_extracted = True

                except Exception as e:
                    err_detail = "; ".join(extraction_log) if extraction_log else str(e)
                    failed_frames.append((frame_idx if frame_idx is not None else idx, os.path.basename(orig_path), err_detail))
                    if self.verbose:
                        print(f"  [!] 提取帧 {frame_idx} 像素数据失败 ({os.path.basename(orig_path)}) — {err_detail}")
                    continue

                output_ds.NumberOfFrames = 1

                # 移除仅适用于多帧的标签
                for tag_name in (
                    "PerFrameFunctionalGroupsSequence",
                    "SharedFunctionalGroupsSequence",
                    "FrameContentSequence",
                    "FrameAcquisitionSequence",
                    "DimensionOrganizationSequence",
                    "DimensionIndexSequence",
                ):
                    if tag_name in output_ds:
                        delattr(output_ds, tag_name)

                # 合并已处理的头信息（UID、厂商、几何、rescalse 等）
                for tag in ds.keys():
                    output_ds[tag] = ds[tag]

            else:
                # ---------- 普通单帧文件 ----------
                # 拷贝已清洗干净的头部（私有标签已在步骤 5 被删除），
                # 再注入原始像素数据，避免 full_ds 中残留的私有标签被保留
                output_ds = ds.copy()
                output_ds.is_little_endian = getattr(ds, "is_little_endian", True)
                output_ds.is_implicit_VR = getattr(ds, "is_implicit_VR", False)
                if hasattr(ds, "file_meta") and ds.file_meta is not None:
                    output_ds.file_meta = ds.file_meta.copy()
                output_ds.PixelData = full_ds.PixelData

            # 确保 file_meta 存在且 Transfer Syntax 已更新
            if not hasattr(output_ds, "file_meta") or output_ds.file_meta is None:
                output_ds.file_meta = Dataset()
            output_ds.file_meta.MediaStorageSOPClassUID = self.CT_IMAGE_STORAGE_UID
            output_ds.file_meta.MediaStorageSOPInstanceUID = output_ds.SOPInstanceUID
            output_ds.file_meta.TransferSyntaxUID = target_ts

            # 标准化文件命名
            filename = f"IM{idx + 1:04d}.dcm"
            out_path = os.path.join(output_dir, filename)

            try:
                output_ds.save_as(out_path, write_like_original=False)
                saved_count += 1
                # 保存进度：75% ~ 99%
                save_pct = 75 + int(24 * (idx + 1) / len(datasets))
                self._report_progress(
                    f"正在保存切片 {idx + 1}/{len(datasets)}...", save_pct
                )
            except Exception as e:
                failed_frames.append((frame_idx if frame_idx is not None else idx, os.path.basename(orig_path), f"保存失败: {e}"))
                if self.verbose:
                    print(f"  警告：保存失败 {filename} — {e}")
                continue

        if failed_frames and not self.verbose:
            # 非 verbose 模式下也输出关键失败摘要
            unique_errors = {}
            for fidx, fname, err in failed_frames:
                key = err[:80]
                unique_errors[key] = unique_errors.get(key, 0) + 1
            print(f"\n[!] 归一化完成，但 {len(failed_frames)} 帧处理失败（成功 {saved_count}/{len(datasets)}）：")
            for err, count in list(unique_errors.items())[:5]:
                print(f"    ({count} 次) {err}")

        slice_spacing = self._compute_slice_spacing(datasets, axis=getattr(self, '_sort_axis', 2))

        # 收集多帧展开阶段的诊断信息（用于排查 EXE 截断问题）
        expand_diags = []
        for ds in datasets:
            diag = getattr(ds, "_multiframe_expand_diag", None)
            if diag is not None and diag not in expand_diags:
                expand_diags.append(diag)

        summary = {
            "input_dir": source_label,
            "output_dir": output_dir,
            "slice_count": len(datasets),
            "saved_count": saved_count,
            "failed_count": len(failed_frames),
            "failed_frames": failed_frames[:10],
            "study_uid": study_uid,
            "series_uid": new_series_uid,
            "slice_spacing_mm": slice_spacing,
            "target_manufacturer": self.target,
            "status": "success" if saved_count == len(datasets) else "partial",
            "expand_diags": expand_diags,
        }

        if self.verbose or failed_frames:
            print(f"\n{'=' * 60}")
            print(f"归一化完成！")
            print(f"  期望切片数: {len(datasets)}")
            print(f"  成功保存:   {saved_count}")
            if failed_frames:
                print(f"  处理失败:   {len(failed_frames)}")
            print(f"  层间距:     {summary['slice_spacing_mm']:.2f} mm")
            if expand_diags:
                for d in expand_diags:
                    print(f"  [展开诊断] PixelData={d.get('pixel_len')} pa={d.get('pa_shape')} "
                          f"actual={d.get('actual_frames')} err={d.get('read_error')}")
            print(f"  Study UID:  ...{study_uid[-12:]}")
            print(f"  Series UID: ...{new_series_uid[-12:]}")
            print(f"  输出目录:   {output_dir}")
            print(f"{'=' * 60}\n")

        return summary

    def normalize(self, input_dir: str, output_dir: str) -> dict:
        """
        执行完整的 DICOM 断层数据归一化流程（目录版）。

        Parameters
        ----------
        input_dir : str
            包含原始 .dcm 文件的输入目录路径。
        output_dir : str
            归一化后文件的输出目录路径。若已存在会被清空重建。

        Returns
        -------
        dict : 处理摘要信息。
        """
        input_dir = os.path.abspath(input_dir)
        output_dir = os.path.abspath(output_dir)

        if self.verbose:
            print(f"\n{'=' * 60}")
            print(f"DICOM 异构断层数据归一化开始")
            print(f"版本: {self.BUILD_ID}")
            print(f"输入: {input_dir}")
            print(f"输出: {output_dir}")
            print(f"目标厂商: {self.target}")
            print(f"{'=' * 60}\n")

        # 步骤 1：排序与校验
        datasets, file_paths = self._sort_and_validate(input_dir)

        return self._run_pipeline(datasets, file_paths, output_dir, source_label=input_dir)

    def normalize_files(self, file_paths: List[str], output_dir: str) -> dict:
        """
        执行完整的 DICOM 断层数据归一化流程（文件列表版）。

        Parameters
        ----------
        file_paths : List[str]
            原始 DICOM 文件的完整路径列表。
        output_dir : str
            归一化后文件的输出目录路径。若已存在会被清空重建。

        Returns
        -------
        dict : 处理摘要信息。
        """
        output_dir = os.path.abspath(output_dir)

        if self.verbose:
            print(f"\n{'=' * 60}")
            print(f"DICOM 异构断层数据归一化开始")
            print(f"输入文件数: {len(file_paths)}")
            print(f"输出: {output_dir}")
            print(f"目标厂商: {self.target}")
            print(f"{'=' * 60}\n")

        # 步骤 1：排序与校验
        self._report_progress("步骤 1/6: 读取并排序切片...", 0)
        datasets, sorted_paths = self._sort_and_validate_files(file_paths)

        return self._run_pipeline(datasets, sorted_paths, output_dir,
                                   source_label=f"{len(file_paths)} files")


# ------------------------------------------------------------------------------
# 命令行入口演示
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="DICOM 异构断层数据归一化工具 (V4.0)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python dicom_normalizer.py -i ./raw_dicom -o ./normalized_output
  python dicom_normalizer.py -i ./raw_dicom -o ./normalized_output --target SIEMENS
        """,
    )
    parser.add_argument(
        "-i", "--input",
        required=True,
        help="输入目录路径（包含原始 .dcm 文件）",
    )
    parser.add_argument(
        "-o", "--output",
        default="normalized_output",
        help="输出目录路径（默认: normalized_output）",
    )
    parser.add_argument(
        "--target",
        choices=["GE", "SIEMENS", "PHILIPS"],
        default="GE",
        help="目标兼容厂商（默认: GE）",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="静默模式，不打印处理进度",
    )

    args = parser.parse_args()

    normalizer = DICOMNormalizer(
        target_manufacturer=args.target,
        verbose=not args.quiet,
    )

    try:
        result = normalizer.normalize(args.input, args.output)
        if not args.quiet:
            print(f"处理状态: {result['status']}")
    except DicomNormalizeError as e:
        print(f"[错误] {e}")
        exit(1)
    except Exception as e:
        print(f"[未预期错误] {e}")
        exit(2)
